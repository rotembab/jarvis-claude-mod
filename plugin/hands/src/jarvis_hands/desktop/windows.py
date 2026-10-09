"""The Windows desktop: SendInput for the mouse, user32 and DWM for monitors and windows (ctypes only, no pywin32).

Why it is built this way:

- **Physical pixels everywhere.** The process is made per-monitor DPI aware
  (v2) before any other call, so the cursor, monitor and window rects and the
  virtual-screen metrics are all physical pixels and a projector at another
  scale lines up with the monitor. ``make_dpi_aware`` is public so the
  overlay and the doctor make the same call; only the first one in a process
  can change anything, and it is idempotent.
- **SendInput, not SetCursorPos.** Injected events go through the real input
  pipeline (hover, drag detection, OLE drag and drop, raw input, the idle
  timer). Moves are absolute over the whole virtual desktop
  (``MOUSEEVENTF_VIRTUALDESK``) and aim at the centre of the target pixel,
  which lands on exactly that pixel whichever way Windows rounds back and
  never sends 0. Every event carries ``EXTRA_INFO_TAG`` in ``dwExtraInfo`` so
  hooks can tell our input from other injectors. "left" is always the
  primary button, also with swapped buttons.
- **No button is left down behind the executor's back.** When a release
  fails (a UAC prompt or the lock screen took the input mid-pinch) the
  executor forgets the button, but Windows still has it down. The backend
  remembers it and sends the release again, as it was pressed, ahead of the
  next input that gets through or as soon as ``input_desktop_ok`` sees the
  normal desktop again.
- **Windows are placed by their visible frame.** Windows 10/11 windows have
  invisible resize borders: ``window_rect`` reports the DWM extended frame
  bounds and ``set_window_rect`` adds the borders back. Pure moves and
  raises are asynchronous (``SWP_ASYNCWINDOWPOS``) so a busy or hung app
  cannot stall the 120 Hz executor; resizes are synchronous, so the app
  paces them, unless the app is already hung. A window without a sizing
  border (a dialog, a fixed tool window) is only ever moved: its controls
  would not follow a new size.
- **UIPI.** A medium-integrity process may not move an elevated window
  (Task Manager, an administrator's console), and with an asynchronous move
  Windows may not even say so. So the window's process integrity level is
  checked first (cached per window) and ``InputBlocked`` raised, as it is for
  an ``ERROR_ACCESS_DENIED`` from the call itself.
- **The executor's reads stay true.** It reads the cursor right after a move
  and a window's rect right after a restore. A move waits (a few
  milliseconds at most) until the cursor has left its old spot, and a
  restore, which like every show command is only queued to the app, waits up
  to a quarter of a second for the window to leave the maximized state. When
  the cursor cannot get there at all (another app has it confined with
  ClipCursor, or the target is in a gap between monitors and clamps back to
  where the cursor already is) the wait would burn its whole budget on every
  move, 120 times a second. So after a wait that ran out, moves from that
  same spot wait half a millisecond instead of four: still long enough to
  read a freed cursor's next move where it went, which skipping the wait
  would not (the executor would take that stale read for the user's mouse).
- **The air keyboard types through the same pipe.** One ``SendInput`` call per
  stroke, so a physical key cannot fall between its down and its up (or between
  our Shift and the letter). Characters go in as Unicode events by default (the
  layout of the window is then irrelevant); ``inject="vk"`` looks the key up on
  the foreground thread's layout and falls back to Unicode for anything a plain
  key cannot type. A key-up Windows refused is never forgotten: it is kept and
  sent ahead of the next key, by ``release_keys``, ``input_desktop_ok`` and
  ``close``, because a key left down repeats in whatever has the focus.
- **Keys are held back unless the target is known to be fine.** ``key_target``
  fails closed on every probe, and its elevation rule is absolute (an elevated
  window is refused whatever our own level is, and every window when our own
  level is unknown), where the mouse path's ``_is_blocked`` is relative to us.
  ``foreign_input`` compares the last-input tick with the one our own batch
  left, so a person typing or moving the mouse is told from us.

Every module-level name imports on any OS; the DLLs are loaded on first use,
on Windows only, with a prototype for every function called (the default
``int`` would truncate 64-bit handles). The pure helpers (normalization,
wheel encoding, button mapping, frame margins, fixed-size placement, display
naming, SID parsing) are plain functions tested everywhere.
"""

from __future__ import annotations

import ctypes
import logging
import ntpath
import os
import sys
import threading
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from ctypes import wintypes as wt
from dataclasses import dataclass
from typing import Any, Literal

from ..clock import now as clock_now
from ..geometry import Rect
from .base import Desktop, Display, InputBlocked, KeyTarget, UnsupportedPlatform, Window, WindowState
from .keys import (
    INPUT_KEYBOARD,
    KEYEVENTF_KEYUP,
    KEYEVENTF_UNICODE,
    KeyStroke,
    VkLookup,
    events_for,
)

log = logging.getLogger(__name__)

_Button = Literal["left", "right"]
_KeyEvent = tuple[int, int, int]  # (wVk, wScan, dwFlags), as desktop/keys.py makes them
_KeyBlock = Literal["elevated", "shell", "own", "none"]

#: ``dwExtraInfo`` on every event we inject ("JRVS"), so hooks and GetMessageExtraInfo can tell it is ours.
EXTRA_INFO_TAG = 0x4A525653

DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
PROCESS_PER_MONITOR_DPI_AWARE = 2
DPI_AWARENESS_NAMES = {0: "unaware", 1: "system", 2: "per_monitor_v1"}

ERROR_ACCESS_DENIED = 5
ERROR_INSUFFICIENT_BUFFER = 122
S_OK = 0
E_ACCESSDENIED = 0x80070005

SM_SWAPBUTTON = 23
SM_CXDOUBLECLK = 36
SM_CYDOUBLECLK = 37
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_ABSOLUTE = 0x8000

MONITORINFOF_PRIMARY = 0x1
QDC_ONLY_ACTIVE_PATHS = 0x2
DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME = 1
DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME = 2
DISPLAYCONFIG_OUTPUT_TECHNOLOGY_INDIRECT_WIRED = 16
DISPLAYCONFIG_OUTPUT_TECHNOLOGY_INDIRECT_VIRTUAL = 17
#: Output technologies that are virtual displays (streaming dummies, virtual display drivers). Not INDIRECT_WIRED:
#: that is a physical screen on a USB display adapter or a DisplayLink dock, which may well be the projector.
VIRTUAL_OUTPUT_TECHNOLOGIES = frozenset({DISPLAYCONFIG_OUTPUT_TECHNOLOGY_INDIRECT_VIRTUAL})
#: An adapter or monitor whose name contains one of these is virtual. "Virtual Display Driver" and most others
#: say so; older builds of it kept the name of Microsoft's sample driver, and often report HDMI as their output.
VIRTUAL_NAME_HINTS = ("virtual", "iddsampledriver")

GA_ROOT = 2
GWL_STYLE = -16
WS_THICKFRAME = 0x00040000  # WS_SIZEBOX: the window has a sizing border
DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14
HWND_TOP = None  # (HWND)0
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_NOOWNERZORDER = 0x0200
SWP_ASYNCWINDOWPOS = 0x4000
SW_MAXIMIZE = 3
SW_MINIMIZE = 6
SW_RESTORE = 9

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
#: TokenIntegrityLevel in TOKEN_INFORMATION_CLASS.
TOKEN_INTEGRITY_LEVEL = 25
#: Mandatory-label RIDs (the last sub-authority of the S-1-16-x integrity SID).
INTEGRITY_NAMES = {0x0000: "untrusted", 0x1000: "low", 0x2000: "medium", 0x2100: "medium+", 0x3000: "high"}
SECURITY_MANDATORY_SYSTEM_RID = 0x4000
#: The first integrity level the keyboard refuses whatever the helper's own level is (an elevated administrator).
INTEGRITY_HIGH_RID = 0x3000

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

DESKTOP_SWITCHDESKTOP = 0x0100
UOI_NAME = 2

# The keyboard side (the air keyboard). INPUT_KEYBOARD and KEYEVENTF_* are the pure layer's (desktop/keys.py).
ES_PASSWORD = 0x20  # an edit control that shows bullets
VK_SHIFT, VK_CONTROL, VK_MENU, VK_CAPITAL, VK_LWIN, VK_RWIN = 0x10, 0x11, 0x12, 0x14, 0x5B, 0x5C
#: The keys that turn a typed letter into a shortcut: a physical one of these holds the keyboard back.
SHORTCUT_MODIFIERS = (VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN)
KEY_DOWN_BIT = 0x8000  # GetAsyncKeyState: the key is down now
MAPVK_VK_TO_VSC = 0
SW_SHOWNORMAL = 1
#: SHQueryUserNotificationState answers.
QUNS_NOT_PRESENT, QUNS_BUSY, QUNS_RUNNING_D3D_FULL_SCREEN, QUNS_PRESENTATION_MODE = 1, 2, 3, 4
QUNS_ACCEPTS_NOTIFICATIONS, QUNS_QUIET_TIME, QUNS_APP = 5, 6, 7
#: Those in which the user is busy with something full screen or presenting: nothing may be typed over it.
COVERED_STATES = frozenset({QUNS_BUSY, QUNS_RUNNING_D3D_FULL_SCREEN, QUNS_PRESENTATION_MODE})
PROCESS_NAME_WIN32 = 0  # QueryFullProcessImageNameW: the path in Win32 form
#: Longest path QueryFullProcessImageNameW is asked for (long paths are rare; a longer one just has no name).
IMAGE_NAME_CHARS = 1024

#: Top-level windows a grab must never pick up: the desktop, taskbars, tray overflow, Start and Search, the
#: Windows 11 XAML shell surfaces (taskbar thumbnails, Alt+Tab, Snap Layouts), classic thumbnails and the task
#: switcher, and open menus.
SHELL_WINDOW_CLASSES = frozenset(
    {
        "Progman",
        "WorkerW",
        "Shell_TrayWnd",
        "Shell_SecondaryTrayWnd",
        "NotifyIconOverflowWindow",
        "TopLevelWindowForOverflowXamlIsland",
        "Windows.UI.Core.CoreWindow",
        "XamlExplorerHostIslandWindow",
        "TaskListThumbnailWnd",
        "MultitaskingViewFrame",
        "TaskSwitcherWnd",
        "ForegroundStaging",
        "#32768",
    }
)

#: Invisible borders wider than this (or negative) mean the two rects are not in the same coordinates (DPI
#: virtualization when per-monitor awareness failed): then the window rect is used as it is.
MAX_BORDER_PX = 64
#: How long a move waits for the cursor to leave its old spot.
MOVE_SETTLE_S = 0.004
#: How long a move waits instead while the cursor is still where a whole wait ran out (confined, or clamped back
#: to where it was): the executor moves 120 times a second, so this costs about 6% of a core rather than half.
STUCK_SETTLE_S = 0.0005
#: How long a restore waits for the window to leave the maximized (or minimized) state.
RESTORE_WAIT_S = 0.25
#: Per-window UIPI verdicts kept before the cache starts over.
BLOCKED_CACHE_SIZE = 512

#: What ``key_target`` answers when it cannot read the foreground window at all: no window anything may be typed into.
_NO_KEY_TARGET = KeyTarget(0, 0, "", 0, "none", False, False)

# ctypes.wintypes lacks these.
ULONG_PTR = ctypes.c_size_t
HRESULT = ctypes.c_long


# --------------------------------------------------------------------------- structures (64-bit sizes in comments)


class MONITORINFOEXW(ctypes.Structure):  # 104
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("rcMonitor", wt.RECT),
        ("rcWork", wt.RECT),
        ("dwFlags", wt.DWORD),
        ("szDevice", wt.WCHAR * 32),
    ]


class DISPLAY_DEVICEW(ctypes.Structure):  # noqa: N801 - 840
    _fields_ = [
        ("cb", wt.DWORD),
        ("DeviceName", wt.WCHAR * 32),
        ("DeviceString", wt.WCHAR * 128),
        ("StateFlags", wt.DWORD),
        ("DeviceID", wt.WCHAR * 128),
        ("DeviceKey", wt.WCHAR * 128),
    ]


class MOUSEINPUT(ctypes.Structure):  # 32
    _fields_ = [
        ("dx", wt.LONG),
        ("dy", wt.LONG),
        ("mouseData", wt.DWORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ULONG_PTR),  # pointer-sized: a 4-byte field breaks the union's alignment and INPUT's size
    ]


class KEYBDINPUT(ctypes.Structure):  # 24
    _fields_ = [
        ("wVk", wt.WORD),
        ("wScan", wt.WORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):  # 8
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):  # 32
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):  # 40: type, 4 bytes of padding, the union
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


class GUITHREADINFO(ctypes.Structure):  # 72
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("flags", wt.DWORD),
        ("hwndActive", wt.HWND),
        ("hwndFocus", wt.HWND),
        ("hwndCapture", wt.HWND),
        ("hwndMenuOwner", wt.HWND),
        ("hwndMoveSize", wt.HWND),
        ("hwndCaret", wt.HWND),
        ("rcCaret", wt.RECT),
    ]


class LASTINPUTINFO(ctypes.Structure):  # 8
    _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]


class LUID(ctypes.Structure):  # 8
    _fields_ = [("LowPart", wt.DWORD), ("HighPart", wt.LONG)]


class DISPLAYCONFIG_RATIONAL(ctypes.Structure):  # noqa: N801 - 8
    _fields_ = [("Numerator", ctypes.c_uint32), ("Denominator", ctypes.c_uint32)]


class DISPLAYCONFIG_PATH_SOURCE_INFO(ctypes.Structure):  # noqa: N801 - 20
    _fields_ = [
        ("adapterId", LUID),
        ("id", ctypes.c_uint32),
        ("modeInfoIdx", ctypes.c_uint32),  # a union with clone-group bit fields; same size
        ("statusFlags", ctypes.c_uint32),
    ]


class DISPLAYCONFIG_PATH_TARGET_INFO(ctypes.Structure):  # noqa: N801 - 48
    _fields_ = [
        ("adapterId", LUID),
        ("id", ctypes.c_uint32),
        ("modeInfoIdx", ctypes.c_uint32),
        ("outputTechnology", ctypes.c_uint32),
        ("rotation", ctypes.c_uint32),
        ("scaling", ctypes.c_uint32),
        ("refreshRate", DISPLAYCONFIG_RATIONAL),
        ("scanLineOrdering", ctypes.c_uint32),
        ("targetAvailable", wt.BOOL),
        ("statusFlags", ctypes.c_uint32),
    ]


class DISPLAYCONFIG_PATH_INFO(ctypes.Structure):  # noqa: N801 - 72
    _fields_ = [
        ("sourceInfo", DISPLAYCONFIG_PATH_SOURCE_INFO),
        ("targetInfo", DISPLAYCONFIG_PATH_TARGET_INFO),
        ("flags", ctypes.c_uint32),
    ]


class DISPLAYCONFIG_MODE_INFO(ctypes.Structure):  # noqa: N801 - 64
    # The union (target mode, source mode, desktop image) is never read: 48 opaque bytes, 8-byte aligned like the
    # UINT64 pixel rate inside the target mode.
    _fields_ = [
        ("infoType", ctypes.c_uint32),
        ("id", ctypes.c_uint32),
        ("adapterId", LUID),
        ("_mode", ctypes.c_uint64 * 6),
    ]


class DISPLAYCONFIG_DEVICE_INFO_HEADER(ctypes.Structure):  # noqa: N801 - 20
    _fields_ = [
        ("type", ctypes.c_uint32),
        ("size", ctypes.c_uint32),
        ("adapterId", LUID),
        ("id", ctypes.c_uint32),
    ]


class DISPLAYCONFIG_SOURCE_DEVICE_NAME(ctypes.Structure):  # noqa: N801 - 84
    _fields_ = [("header", DISPLAYCONFIG_DEVICE_INFO_HEADER), ("viewGdiDeviceName", wt.WCHAR * 32)]


class DISPLAYCONFIG_TARGET_DEVICE_NAME(ctypes.Structure):  # noqa: N801 - 420
    _fields_ = [
        ("header", DISPLAYCONFIG_DEVICE_INFO_HEADER),
        ("flags", ctypes.c_uint32),
        ("outputTechnology", ctypes.c_uint32),
        ("edidManufactureId", ctypes.c_uint16),
        ("edidProductCodeId", ctypes.c_uint16),
        ("connectorInstance", ctypes.c_uint32),
        ("monitorFriendlyDeviceName", wt.WCHAR * 64),
        ("monitorDevicePath", wt.WCHAR * 128),
    ]


# --------------------------------------------------------------------------- pure helpers


def clamp_to_screen(x: float, y: float, screen: tuple[int, int, int, int]) -> tuple[int, int]:
    """The pixel of the virtual screen ``(left, top, width, height)`` nearest ``(x, y)``."""
    left, top, width, height = screen
    return min(max(round(x), left), left + width - 1), min(max(round(y), top), top + height - 1)


def normalize_absolute(x: float, y: float, screen: tuple[int, int, int, int]) -> tuple[int, int]:
    """SendInput's 0..65535 absolute coordinates (with ``MOUSEEVENTF_VIRTUALDESK``) for the pixel ``(x, y)``.

    ``screen`` is the virtual screen as ``(left, top, width, height)``; points
    outside it go to its edge. Aiming at the centre of the pixel lands on
    exactly that pixel whether Windows maps back with /65536 or /65535, and
    never yields 0, which does not work since Windows 10 1709 (Chromium).
    """
    left, top, width, height = screen
    if width <= 0 or height <= 0:
        raise ValueError(f"empty virtual screen {screen}")
    cx, cy = clamp_to_screen(x, y, screen)
    nx = ((2 * (cx - left) + 1) * 65536) // (2 * width)
    ny = ((2 * (cy - top) + 1) * 65536) // (2 * height)
    return min(max(nx, 0), 65535), min(max(ny, 0), 65535)


def wheel_data(delta: int) -> int:
    """``mouseData`` for a wheel event: the signed amount as a DWORD (two's complement), clamped to 32 bits."""
    delta = min(max(int(delta), -(2**31)), 2**31 - 1)
    return delta & 0xFFFFFFFF


def button_flags(button: _Button, down: bool, swapped: bool) -> int:
    """The SendInput flag that presses or releases the primary ("left") or secondary ("right") button.

    SendInput's LEFT flags mean the physical left button, which is the
    secondary one when the user swapped the buttons (``SM_SWAPBUTTON``).
    """
    if button not in ("left", "right"):
        raise ValueError(f"unknown mouse button {button!r}")
    if (button == "left") != swapped:
        return MOUSEEVENTF_LEFTDOWN if down else MOUSEEVENTF_LEFTUP
    return MOUSEEVENTF_RIGHTDOWN if down else MOUSEEVENTF_RIGHTUP


def outer_rect(visible_target: Rect, window_rect: Rect, frame_bounds: Rect) -> Rect:
    """The window rect that puts the visible frame on ``visible_target``.

    ``window_rect`` (GetWindowRect) includes the invisible resize borders
    that ``frame_bounds`` (the DWM extended frame bounds) leaves out, about
    7 px times the scale on the left, right and bottom. Margins outside
    0..MAX_BORDER_PX mean the two are not comparable, and the target is used
    as it is.
    """
    left = frame_bounds.left - window_rect.left
    top = frame_bounds.top - window_rect.top
    right = window_rect.right - frame_bounds.right
    bottom = window_rect.bottom - frame_bounds.bottom
    if any(not 0 <= margin <= MAX_BORDER_PX for margin in (left, top, right, bottom)):
        return visible_target
    return Rect.from_ltrb(
        visible_target.left - left,
        visible_target.top - top,
        visible_target.right + right,
        visible_target.bottom + bottom,
    )


def centred_on(target: Rect, width: float, height: float) -> Rect:
    """A ``width`` x ``height`` rect with the centre of ``target``: where a window that can't be resized goes."""
    centre = target.center
    return Rect(centre.x - width / 2, centre.y - height / 2, width, height)


def is_shell_window(class_name: str) -> bool:
    """Whether a top-level window of this class belongs to the shell (desktop, taskbar, Start, thumbnails...)."""
    return class_name in SHELL_WINDOW_CLASSES


def is_edit_class(class_name: str) -> bool:
    """Whether a focused control of this class can hide what is typed with ES_PASSWORD.

    The classic ``Edit``, the RichEdit family and the WinForms TextBox (an Edit under a longer class name). A browser,
    Electron or a terminal draws its own text box and cannot be told from outside: the docs say so.
    """
    lowered = class_name.lower()
    return lowered == "edit" or lowered.startswith(("richedit", "windowsforms10.edit."))


def missing_ups(events: Sequence[_KeyEvent]) -> list[_KeyEvent]:
    """The key-ups that balance the downs of ``events`` that have no up in it, newest first (as a hand lets go).

    A SendInput call Windows cut short leaves exactly these keys down; the Unicode and the virtual-key form of a key
    are different keys, as in ``desktop.fake.events_balanced``.
    """
    held: list[_KeyEvent] = []
    for vk, scan, flags in events:
        if not flags & KEYEVENTF_KEYUP:
            held.append((vk, scan, flags))
            continue
        for index in range(len(held) - 1, -1, -1):
            same_form = (held[index][2] ^ flags) & KEYEVENTF_UNICODE == 0
            if held[index][:2] == (vk, scan) and same_form:
                del held[index]
                break
    return [(vk, scan, flags | KEYEVENTF_KEYUP) for vk, scan, flags in reversed(held)]


def has_virtual_hint(name: str) -> bool:
    lowered = name.lower()
    return any(hint in lowered for hint in VIRTUAL_NAME_HINTS)


@dataclass(frozen=True)
class MonitorRecord:
    """One monitor as EnumDisplayMonitors and GetMonitorInfoW report it."""

    device: str  # the GDI device name, \\.\DISPLAY1
    rect: Rect
    work: Rect
    primary: bool


@dataclass(frozen=True)
class TargetRecord:
    """One active display-config target (a monitor, projector or virtual screen) showing a GDI source."""

    name: str  # the EDID friendly name, "" when there is none
    technology: int  # DISPLAYCONFIG_VIDEO_OUTPUT_TECHNOLOGY

    @property
    def virtual(self) -> bool:
        return self.technology in VIRTUAL_OUTPUT_TECHNOLOGIES or has_virtual_hint(self.name)


def _device_key(device: str) -> str:
    return device.strip().upper()


def _short_device(device: str) -> str:
    return device.strip().rsplit("\\", 1)[-1] or device


def build_displays(
    monitors: Sequence[MonitorRecord],
    targets: Mapping[str, Sequence[TargetRecord]],
    adapters: Mapping[str, str],
) -> list[Display]:
    """Displays with readable names and the virtual flag, from the raw records.

    ``targets`` maps a GDI device name to the targets showing it (two in
    Win+P "Duplicate"), ``adapters`` maps it to its adapter's name
    (EnumDisplayDevicesW's DeviceString); either may be empty when Windows
    would not say. A display is named after its monitors (the device name
    when they have none; two displays with one name get their device name
    added) and is virtual when its adapter's name says so, or when every
    monitor showing it is virtual (an indirect virtual output, or a name that
    says so): a source duplicated onto one real monitor is still visible. A
    screen on a USB display adapter (an indirect wired output) is real.
    """
    targets_by_key = {_device_key(k): list(v) for k, v in targets.items()}
    adapters_by_key = {_device_key(k): v for k, v in adapters.items()}
    names: list[str] = []
    for monitor in monitors:
        shown_on = targets_by_key.get(_device_key(monitor.device), [])
        friendly = list(dict.fromkeys(t.name.strip() for t in shown_on if t.name.strip()))
        names.append(" + ".join(friendly) if friendly else monitor.device)
    repeats = Counter(names)
    displays: list[Display] = []
    for index, (monitor, name) in enumerate(zip(monitors, names, strict=True), start=1):
        key = _device_key(monitor.device)
        if repeats[name] > 1 and name != monitor.device:
            name = f"{name} ({_short_device(monitor.device)})"
        shown_on = targets_by_key.get(key, [])
        virtual = has_virtual_hint(adapters_by_key.get(key, "")) or (
            bool(shown_on) and all(t.virtual for t in shown_on)
        )
        displays.append(Display(index, name, monitor.rect, monitor.work, monitor.primary, virtual))
    return displays


def integrity_rid(sid: bytes) -> int:
    """The integrity level of a mandatory-label SID (S-1-16-x): its last sub-authority, x.

    ``sid`` is the binary SID: revision 1, the sub-authority count, a 6-byte
    authority, then little-endian 32-bit sub-authorities.
    """
    if len(sid) < 8 or sid[0] != 1:
        raise ValueError("not a SID")
    count = sid[1]
    if not 1 <= count <= 15 or len(sid) < 8 + 4 * count:
        raise ValueError(f"bad SID sub-authority count {count}")
    return int.from_bytes(sid[4 + 4 * count : 8 + 4 * count], "little")


def integrity_name(rid: int | None) -> str:
    if rid is None:
        return "unknown"
    if rid >= SECURITY_MANDATORY_SYSTEM_RID:
        return "system"
    return INTEGRITY_NAMES.get(rid, f"{rid:#x}")


# --------------------------------------------------------------------------- the DLLs


def _fn(dll: Any, name: str, restype: Any, *argtypes: Any) -> Any:
    """``dll.<name>`` with its prototype set (the default int would truncate 64-bit handles)."""
    function = getattr(dll, name)
    function.restype = restype
    function.argtypes = list(argtypes)
    return function


class _Win32:
    """Private DLL instances (never the shared ``ctypes.windll``) with every prototype this module calls."""

    def __init__(self) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)  # type: ignore[attr-defined]
        dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)  # type: ignore[attr-defined]
        c_int, c_uint32, c_void_p, rect_p = ctypes.c_int, ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(wt.RECT)
        u32p = ctypes.POINTER(c_uint32)

        self.MONITORENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HMONITOR, wt.HDC, rect_p, wt.LPARAM)  # type: ignore[attr-defined]

        # Input and metrics.
        self.GetSystemMetrics = _fn(user32, "GetSystemMetrics", c_int, c_int)
        self.GetCursorPos = _fn(user32, "GetCursorPos", wt.BOOL, ctypes.POINTER(wt.POINT))
        self.SendInput = _fn(user32, "SendInput", wt.UINT, wt.UINT, ctypes.POINTER(INPUT), c_int)
        self.GetDoubleClickTime = _fn(user32, "GetDoubleClickTime", wt.UINT)
        # Monitors.
        self.EnumDisplayMonitors = _fn(
            user32, "EnumDisplayMonitors", wt.BOOL, wt.HDC, rect_p, self.MONITORENUMPROC, wt.LPARAM
        )
        self.GetMonitorInfoW = _fn(user32, "GetMonitorInfoW", wt.BOOL, wt.HMONITOR, ctypes.POINTER(MONITORINFOEXW))
        self.EnumDisplayDevicesW = _fn(
            user32, "EnumDisplayDevicesW", wt.BOOL, wt.LPCWSTR, wt.DWORD, ctypes.POINTER(DISPLAY_DEVICEW), wt.DWORD
        )
        self.GetDisplayConfigBufferSizes = _fn(user32, "GetDisplayConfigBufferSizes", wt.LONG, c_uint32, u32p, u32p)
        self.QueryDisplayConfig = _fn(
            user32,
            "QueryDisplayConfig",
            wt.LONG,
            c_uint32,
            u32p,
            ctypes.POINTER(DISPLAYCONFIG_PATH_INFO),
            u32p,
            ctypes.POINTER(DISPLAYCONFIG_MODE_INFO),
            c_void_p,  # DISPLAYCONFIG_TOPOLOGY_ID*, NULL with QDC_ONLY_ACTIVE_PATHS
        )
        self.DisplayConfigGetDeviceInfo = _fn(
            user32, "DisplayConfigGetDeviceInfo", wt.LONG, ctypes.POINTER(DISPLAYCONFIG_DEVICE_INFO_HEADER)
        )
        # Windows.
        self.WindowFromPoint = _fn(user32, "WindowFromPoint", wt.HWND, wt.POINT)  # POINT by value
        self.GetAncestor = _fn(user32, "GetAncestor", wt.HWND, wt.HWND, wt.UINT)
        self.GetShellWindow = _fn(user32, "GetShellWindow", wt.HWND)
        self.GetWindowThreadProcessId = _fn(user32, "GetWindowThreadProcessId", wt.DWORD, wt.HWND, wt.LPDWORD)
        self.IsWindow = _fn(user32, "IsWindow", wt.BOOL, wt.HWND)
        self.IsWindowVisible = _fn(user32, "IsWindowVisible", wt.BOOL, wt.HWND)
        self.IsIconic = _fn(user32, "IsIconic", wt.BOOL, wt.HWND)
        self.IsZoomed = _fn(user32, "IsZoomed", wt.BOOL, wt.HWND)
        self.IsHungAppWindow = _fn(user32, "IsHungAppWindow", wt.BOOL, wt.HWND)
        try:  # LONG_PTR; 32-bit user32 has no such export (it is a macro for GetWindowLongW there)
            self.GetWindowLongPtrW = _fn(user32, "GetWindowLongPtrW", ctypes.c_ssize_t, wt.HWND, c_int)
        except AttributeError:
            self.GetWindowLongPtrW = _fn(user32, "GetWindowLongW", wt.LONG, wt.HWND, c_int)
        self.GetClassNameW = _fn(user32, "GetClassNameW", c_int, wt.HWND, wt.LPWSTR, c_int)
        self.GetWindowTextW = _fn(user32, "GetWindowTextW", c_int, wt.HWND, wt.LPWSTR, c_int)
        self.GetWindowRect = _fn(user32, "GetWindowRect", wt.BOOL, wt.HWND, rect_p)
        self.SetWindowPos = _fn(user32, "SetWindowPos", wt.BOOL, wt.HWND, wt.HWND, c_int, c_int, c_int, c_int, wt.UINT)
        self.ShowWindowAsync = _fn(user32, "ShowWindowAsync", wt.BOOL, wt.HWND, c_int)
        self.SetForegroundWindow = _fn(user32, "SetForegroundWindow", wt.BOOL, wt.HWND)
        self.DwmGetWindowAttribute = _fn(
            dwmapi, "DwmGetWindowAttribute", HRESULT, wt.HWND, wt.DWORD, c_void_p, wt.DWORD
        )
        # The session: which desktop has the input, staying awake.
        self.OpenInputDesktop = _fn(user32, "OpenInputDesktop", wt.HDESK, wt.DWORD, wt.BOOL, wt.DWORD)
        self.CloseDesktop = _fn(user32, "CloseDesktop", wt.BOOL, wt.HDESK)
        self.GetUserObjectInformationW = _fn(
            user32, "GetUserObjectInformationW", wt.BOOL, wt.HANDLE, c_int, c_void_p, wt.DWORD, wt.LPDWORD
        )
        self.SetThreadExecutionState = _fn(kernel32, "SetThreadExecutionState", wt.DWORD, wt.DWORD)
        # Integrity levels (UIPI).
        self.GetCurrentProcess = _fn(kernel32, "GetCurrentProcess", wt.HANDLE)
        self.OpenProcess = _fn(kernel32, "OpenProcess", wt.HANDLE, wt.DWORD, wt.BOOL, wt.DWORD)
        self.CloseHandle = _fn(kernel32, "CloseHandle", wt.BOOL, wt.HANDLE)
        self.OpenProcessToken = _fn(
            advapi32, "OpenProcessToken", wt.BOOL, wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)
        )
        self.GetTokenInformation = _fn(
            advapi32, "GetTokenInformation", wt.BOOL, wt.HANDLE, c_int, c_void_p, wt.DWORD, wt.LPDWORD
        )
        # The keyboard: where keys would land, what is down, what the last input was, and the shell's say.
        self.GetForegroundWindow = _fn(user32, "GetForegroundWindow", wt.HWND)
        self.GetGUIThreadInfo = _fn(user32, "GetGUIThreadInfo", wt.BOOL, wt.DWORD, ctypes.POINTER(GUITHREADINFO))
        self.GetKeyboardLayout = _fn(user32, "GetKeyboardLayout", c_void_p, wt.DWORD)  # an HKL: pointer-sized
        self.VkKeyScanExW = _fn(user32, "VkKeyScanExW", ctypes.c_short, wt.WCHAR, c_void_p)
        self.MapVirtualKeyExW = _fn(user32, "MapVirtualKeyExW", wt.UINT, wt.UINT, wt.UINT, c_void_p)
        self.GetLastInputInfo = _fn(user32, "GetLastInputInfo", wt.BOOL, ctypes.POINTER(LASTINPUTINFO))
        self.GetAsyncKeyState = _fn(user32, "GetAsyncKeyState", ctypes.c_short, c_int)
        self.GetKeyState = _fn(user32, "GetKeyState", ctypes.c_short, c_int)
        self.QueryFullProcessImageNameW = _fn(
            kernel32, "QueryFullProcessImageNameW", wt.BOOL, wt.HANDLE, wt.DWORD, wt.LPWSTR, wt.LPDWORD
        )
        self.ShellExecuteW = _fn(
            shell32, "ShellExecuteW", c_void_p, wt.HWND, wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, c_int
        )
        self.SHQueryUserNotificationState = _fn(shell32, "SHQueryUserNotificationState", HRESULT, ctypes.POINTER(c_int))


_api_lock = threading.Lock()
_api: _Win32 | None = None


def _win32() -> _Win32:
    global _api
    with _api_lock:
        if _api is None:
            _api = _Win32()
        return _api


def _win_error(code: int, what: str) -> OSError:
    """An OSError carrying the Windows error code (``winerror``) and its text."""
    format_error = getattr(ctypes, "FormatError", None)  # Windows only
    text = format_error(code).strip() if format_error is not None and code else ""
    return OSError(None, f"{what} failed: {text}" if text else f"{what} failed (error {code})", None, code)


def _last_error(what: str) -> OSError:
    return _win_error(ctypes.get_last_error(), what)  # type: ignore[attr-defined]


def _gone(hwnd: int) -> OSError:
    return OSError(f"window {hwnd:#x} no longer exists")


# --------------------------------------------------------------------------- DPI awareness

_dpi_lock = threading.Lock()
_dpi_awareness: str | None = None


def make_dpi_aware() -> str:
    """Make this process per-monitor DPI aware (v2) and say what it is now. Idempotent; never raises.

    Call it before any other Windows call that deals in coordinates and
    before creating any window. Returns ``"per_monitor_v2"``,
    ``"per_monitor_v1"``, ``"system"`` (coordinates are virtualized on
    monitors at another scale: degraded), ``"unaware"``, ``"unknown"``, or
    ``"unsupported"`` off Windows. When the awareness was set already (by a
    manifest or an earlier call) it reports that one.
    """
    global _dpi_awareness
    with _dpi_lock:
        if _dpi_awareness is None:
            if sys.platform != "win32":
                _dpi_awareness = "unsupported"
            else:
                try:
                    _dpi_awareness = _set_dpi_awareness()
                except Exception:
                    log.exception("could not set the DPI awareness")
                    _dpi_awareness = "unknown"
        return _dpi_awareness


def _set_dpi_awareness() -> str:
    user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
    try:
        set_context = _fn(user32, "SetProcessDpiAwarenessContext", wt.BOOL, wt.HANDLE)  # Windows 10 1703+
    except AttributeError:
        set_context = None
    if set_context is not None:
        if set_context(ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)):
            log.info("DPI awareness: per-monitor v2 (SetProcessDpiAwarenessContext)")
            return "per_monitor_v2"
        err = ctypes.get_last_error()  # type: ignore[attr-defined]
        if err == ERROR_ACCESS_DENIED:
            return _already_set(user32)
        log.warning("SetProcessDpiAwarenessContext failed (error %d); trying SetProcessDpiAwareness", err)
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)  # type: ignore[attr-defined]
        set_awareness = _fn(shcore, "SetProcessDpiAwareness", HRESULT, ctypes.c_int)  # Windows 8.1+
    except (OSError, AttributeError):
        set_awareness = None
    if set_awareness is not None:
        hr = set_awareness(PROCESS_PER_MONITOR_DPI_AWARE) & 0xFFFFFFFF
        if hr == S_OK:
            log.info("DPI awareness: per-monitor v1 (SetProcessDpiAwareness)")
            return "per_monitor_v1"
        if hr == E_ACCESSDENIED:
            return _already_set(user32)
        log.warning("SetProcessDpiAwareness failed (HRESULT %#x); trying SetProcessDPIAware", hr)
    try:
        if _fn(user32, "SetProcessDPIAware", wt.BOOL)():
            log.warning("DPI awareness: system only; coordinates are off on monitors at another scale")
            return "system"
    except AttributeError:
        pass
    log.warning("could not make the process DPI aware; coordinates are scaled on every scaled monitor")
    return "unaware"


def _already_set(user32: Any) -> str:
    current = _current_dpi_awareness(user32)
    if current.startswith("per_monitor"):
        log.info("DPI awareness was already set: %s", current)
    else:
        log.warning("DPI awareness was already set to %s; coordinates may be off on monitors at another scale", current)
    return current


def _current_dpi_awareness(user32: Any) -> str:
    try:
        get_context = _fn(user32, "GetThreadDpiAwarenessContext", wt.HANDLE)
        equal = _fn(user32, "AreDpiAwarenessContextsEqual", wt.BOOL, wt.HANDLE, wt.HANDLE)
        awareness = _fn(user32, "GetAwarenessFromDpiAwarenessContext", ctypes.c_int, wt.HANDLE)
    except AttributeError:
        return "unknown"
    context = get_context()
    if equal(context, ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)):
        return "per_monitor_v2"
    return DPI_AWARENESS_NAMES.get(awareness(context), "unknown")


# --------------------------------------------------------------------------- integrity levels


def _token_integrity(w: _Win32, process: int) -> int | None:
    """The integrity RID of a process (a handle with query rights), or None when its token can't be read."""
    token = wt.HANDLE()
    if not w.OpenProcessToken(process, TOKEN_QUERY, ctypes.byref(token)):
        return None
    try:
        needed = wt.DWORD(0)
        w.GetTokenInformation(token, TOKEN_INTEGRITY_LEVEL, None, 0, ctypes.byref(needed))
        size = needed.value
        if not 16 <= size <= 1024:
            return None
        buffer = ctypes.create_string_buffer(size)
        if not w.GetTokenInformation(token, TOKEN_INTEGRITY_LEVEL, buffer, size, ctypes.byref(needed)):
            return None
        # TOKEN_MANDATORY_LABEL: {PSID Sid; DWORD Attributes}, the SID itself after it in the same buffer.
        sid = ctypes.c_void_p.from_buffer(buffer).value
        base = ctypes.addressof(buffer)
        if not sid or not base <= sid <= base + size - 8:
            return None
        count = ctypes.string_at(sid + 1, 1)[0]
        if sid + 8 + 4 * count > base + size:
            return None
        return integrity_rid(ctypes.string_at(sid, 8 + 4 * count))
    except ValueError:
        return None
    finally:
        w.CloseHandle(token)


def _process_integrity(w: _Win32, pid: int) -> tuple[bool, int | None]:
    """(could we tell?, its integrity RID). Access denied counts as told: it is protected or elevated."""
    process = w.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not process:
        # Anything but access denied (the process exited, say) is not UIPI's doing.
        return ctypes.get_last_error() == ERROR_ACCESS_DENIED, None  # type: ignore[attr-defined]
    try:
        return True, _token_integrity(w, process)
    finally:
        w.CloseHandle(process)


# --------------------------------------------------------------------------- the desktop


def _rect(r: wt.RECT) -> Rect:
    return Rect.from_ltrb(r.left, r.top, r.right, r.bottom)


def _label(window: Window) -> str:
    return f'"{window.title}"' if window.title else f"window {window.handle:#x}"


def _describe(d: Display) -> str:
    x, y, width, height = d.rect.rounded()
    flags = "".join((" primary" if d.primary else "", " virtual" if d.virtual else ""))
    return f"{d.id} {d.name!r} {width}x{height} at {x},{y}{flags}"


class WindowsDesktop(Desktop):
    """The Desktop protocol on Windows. Safe to call from several threads; construct it before any window."""

    #: The keyboard really types (the KeyDesktop flag a fake leaves False): the sink arms its real-input guards on it.
    injects_for_real = True

    def __init__(self, api: Any = None) -> None:
        """``api`` stands in for the DLLs (tests, on any OS); by default they are loaded, on Windows only."""
        if api is None and sys.platform != "win32":
            raise UnsupportedPlatform("WindowsDesktop needs Windows")
        self.dpi_awareness = make_dpi_aware()
        self._w: Any = api if api is not None else _win32()
        self._pid = os.getpid()
        #: Our own integrity RID; None (unknown) turns the up-front UIPI check off.
        self.integrity = _token_integrity(self._w, self._w.GetCurrentProcess())
        #: (hwnd, pid) -> whether UIPI keeps us from acting on it.
        self._blocked: dict[tuple[int, int], bool] = {}
        #: Buttons Windows has down from us -> whether the buttons were swapped when pressed (the release must
        #: match). Guarded by _input_lock, as is _unreleased: the executor's thread and the runtime's both send.
        self._pressed: dict[_Button, bool] = {}
        #: Those of them whose release Windows refused: sent again ahead of the next input that gets through.
        self._unreleased: set[_Button] = set()
        self._input_lock = threading.RLock()
        #: The monitors, the displays made from them, and whether Windows told us everything (else ask again).
        self._display_cache: tuple[tuple[MonitorRecord, ...], list[Display], bool] | None = None
        #: The display-config lookups that failed last time (warned about once, until they work again).
        self._names_failing: set[str] = set()
        self._awake_thread: int | None = None
        #: Where the cursor stayed after a move's whole wait ran out, until it is seen anywhere else. A plain
        #: attribute: the executor's thread is the one that moves the cursor, and a lost update costs one wait.
        self._stuck_at: tuple[int, int] | None = None
        # -- the keyboard. The ledger and the ticks are guarded by _input_lock, like the buttons' state.
        #: Key-ups Windows refused (the down went in, the up did not), oldest first: sent again ahead of the next key.
        self._keys_unreleased: list[_KeyEvent] = []
        #: (hwnd, pid) -> whether keys are refused there (the absolute rule); only answers Windows gave are kept.
        self._key_blocked: dict[tuple[int, int], bool] = {}
        #: pid -> executable base name, for the strip on the local screen; a failed read is not kept.
        self._exe_names: dict[int, str] = {}
        #: GetLastInputInfo's tick right after our own last batch, and the last tick ``foreign_input`` saw.
        self._own_tick: int | None = None
        self._seen_tick: int | None = None
        #: Said once until a read works again, not once per character.
        self._target_unreadable = False
        #: Counts of things that went wrong quietly, so a failing tick read shows without a log line per key.
        self.key_counts: Counter[str] = Counter()
        log.info(
            "Windows desktop: DPI awareness %s, integrity %s, pid %d",
            self.dpi_awareness,
            integrity_name(self.integrity),
            self._pid,
        )
        if self.integrity is None:
            log.warning("our own integrity level is unknown: no key is sent to any window")

    # -- displays -----------------------------------------------------------------------

    def displays(self) -> list[Display]:
        monitors = tuple(self._monitors())
        cached = self._display_cache
        if cached is not None and cached[0] == monitors and cached[2]:
            return list(cached[1])
        # Names and the virtual flag cost a few calls into the display stack: only when the layout changed, or
        # when Windows would not say last time (the console was locked, say). An indirect display whose adapter
        # name doesn't say "virtual" is only known to be virtual from those calls.
        targets, adapters = self._display_targets(), self._adapter_names()
        result = build_displays(monitors, targets or {}, adapters or {})
        self._display_cache = (monitors, result, targets is not None and adapters is not None)
        if cached is None or cached[0] != monitors or cached[1] != result:
            log.info("displays: %s", "; ".join(_describe(d) for d in result) or "none")
        return list(result)

    def _monitors(self) -> list[MonitorRecord]:
        records: list[MonitorRecord] = []
        failures: list[str] = []

        def on_monitor(hmonitor: int | None, _hdc: int | None, _clip: Any, _data: int) -> bool:
            # An exception must not unwind into Windows: note it and carry on.
            try:
                info = MONITORINFOEXW()
                info.cbSize = ctypes.sizeof(MONITORINFOEXW)
                if self._w.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
                    primary = bool(info.dwFlags & MONITORINFOF_PRIMARY)
                    records.append(MonitorRecord(info.szDevice, _rect(info.rcMonitor), _rect(info.rcWork), primary))
                else:
                    failures.append(f"GetMonitorInfoW error {ctypes.get_last_error()}")  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001 - reported below
                failures.append(repr(exc))
            return True

        callback = self._w.MONITORENUMPROC(on_monitor)  # referenced until EnumDisplayMonitors returns
        if not self._w.EnumDisplayMonitors(None, None, callback, 0):
            raise _last_error("EnumDisplayMonitors")
        if failures:
            log.warning("skipped monitors: %s", "; ".join(failures))
        if not records:
            log.warning("Windows reports no monitors")
        return records

    def _display_targets(self) -> dict[str, list[TargetRecord]] | None:
        """GDI device name -> the active targets showing it; None when Windows won't say (never raises)."""
        try:
            targets = self._query_display_config()
        except Exception as exc:  # noqa: BLE001 - never fail displays() over names
            return self._names_failed("monitor names (QueryDisplayConfig)", exc)
        if not targets:
            return self._names_failed("monitor names (QueryDisplayConfig)", "no active display paths")
        self._names_ok("monitor names (QueryDisplayConfig)")
        return targets

    def _names_failed(self, what: str, why: object) -> None:
        if what in self._names_failing:
            log.debug("still cannot read %s: %s", what, why)
        else:
            self._names_failing.add(what)
            log.warning("could not read %s: %s; asking again with the next display check", what, why)
        return None

    def _names_ok(self, what: str) -> None:
        if what in self._names_failing:
            self._names_failing.discard(what)
            log.info("%s readable again", what)

    def _query_display_config(self) -> dict[str, list[TargetRecord]]:
        w = self._w
        for _attempt in range(5):
            npaths, nmodes = ctypes.c_uint32(0), ctypes.c_uint32(0)
            rc = w.GetDisplayConfigBufferSizes(QDC_ONLY_ACTIVE_PATHS, ctypes.byref(npaths), ctypes.byref(nmodes))
            if rc != 0:
                raise _win_error(rc, "GetDisplayConfigBufferSizes")
            if npaths.value == 0:
                return {}
            paths = (DISPLAYCONFIG_PATH_INFO * npaths.value)()
            modes = (DISPLAYCONFIG_MODE_INFO * max(1, nmodes.value))()
            nmodes.value = len(modes)
            rc = w.QueryDisplayConfig(
                QDC_ONLY_ACTIVE_PATHS, ctypes.byref(npaths), paths, ctypes.byref(nmodes), modes, None
            )
            if rc == 0:
                break
            if rc != ERROR_INSUFFICIENT_BUFFER:  # the layout changed between the two calls: ask again
                raise _win_error(rc, "QueryDisplayConfig")
        else:
            raise OSError("QueryDisplayConfig kept asking for bigger buffers")
        targets: dict[str, list[TargetRecord]] = {}
        for path in paths[: npaths.value]:
            source = DISPLAYCONFIG_SOURCE_DEVICE_NAME()
            source.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME
            source.header.size = ctypes.sizeof(source)
            source.header.adapterId = path.sourceInfo.adapterId
            source.header.id = path.sourceInfo.id
            if w.DisplayConfigGetDeviceInfo(ctypes.byref(source.header)) != 0:
                continue
            target = DISPLAYCONFIG_TARGET_DEVICE_NAME()
            target.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME
            target.header.size = ctypes.sizeof(target)
            target.header.adapterId = path.targetInfo.adapterId
            target.header.id = path.targetInfo.id
            name = (
                target.monitorFriendlyDeviceName
                if w.DisplayConfigGetDeviceInfo(ctypes.byref(target.header)) == 0
                else ""
            )
            record = TargetRecord(name, int(path.targetInfo.outputTechnology))
            targets.setdefault(_device_key(source.viewGdiDeviceName), []).append(record)
        return targets

    def _adapter_names(self) -> dict[str, str] | None:
        """GDI device name -> its adapter's name ("NVIDIA GeForce RTX 4070 Ti", "Virtual Display Driver").

        None when Windows won't say (never raises).
        """
        names: dict[str, str] = {}
        try:
            for index in range(64):
                device = DISPLAY_DEVICEW()
                device.cb = ctypes.sizeof(DISPLAY_DEVICEW)
                if not self._w.EnumDisplayDevicesW(None, index, ctypes.byref(device), 0):
                    break
                names[_device_key(device.DeviceName)] = device.DeviceString
        except Exception as exc:  # noqa: BLE001 - never fail displays() over names
            return self._names_failed("display adapter names (EnumDisplayDevicesW)", exc)
        self._names_ok("display adapter names (EnumDisplayDevicesW)")
        return names

    # -- the mouse ----------------------------------------------------------------------

    def cursor(self) -> tuple[int, int]:
        """Raises OSError when Windows won't say (the lock screen, a UAC prompt: our desktop lost the input)."""
        point = wt.POINT()
        if not self._w.GetCursorPos(ctypes.byref(point)):
            raise _last_error("GetCursorPos")
        return point.x, point.y

    def move_cursor(self, x: int, y: int) -> None:
        screen = self._virtual_screen()
        if screen[2] <= 0 or screen[3] <= 0:
            raise OSError("the virtual screen is empty (no interactive desktop?)")
        nx, ny = normalize_absolute(x, y, screen)
        before = self._cursor_or_none()
        self._send([(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, nx, ny, 0)])
        if before is not None and before != clamp_to_screen(x, y, screen):
            self._settle(before)

    def button(self, button: _Button, down: bool) -> None:
        """A press uses the current button swap; a release matches its press.

        A release that fails still raises, and is then sent again ahead of the
        next input that gets through, or when ``input_desktop_ok`` sees the
        normal desktop again: by then the executor has let go of the button,
        but Windows would keep it down.
        """
        with self._input_lock:
            if down:
                swapped = bool(self._w.GetSystemMetrics(SM_SWAPBUTTON))
                self._send([(button_flags(button, True, swapped), 0, 0, 0)])
                self._pressed[button] = swapped
                return
            held = button in self._pressed
            swapped = self._pressed[button] if held else bool(self._w.GetSystemMetrics(SM_SWAPBUTTON))
            self._unreleased.discard(button)  # this call is the retry
            try:
                self._send([(button_flags(button, False, swapped), 0, 0, 0)])
            except BaseException:
                if held:
                    self._unreleased.add(button)
                raise
            self._pressed.pop(button, None)

    def scroll(self, dy: int, dx: int = 0) -> None:
        events = []
        if dy:
            events.append((MOUSEEVENTF_WHEEL, 0, 0, wheel_data(dy)))
        if dx:
            events.append((MOUSEEVENTF_HWHEEL, 0, 0, wheel_data(dx)))
        if events:
            self._send(events)

    def double_click(self) -> tuple[float, int, int]:
        w = self._w
        return w.GetDoubleClickTime() / 1000.0, w.GetSystemMetrics(SM_CXDOUBLECLK), w.GetSystemMetrics(SM_CYDOUBLECLK)

    def _virtual_screen(self) -> tuple[int, int, int, int]:
        metric = self._w.GetSystemMetrics
        return (
            metric(SM_XVIRTUALSCREEN),
            metric(SM_YVIRTUALSCREEN),
            metric(SM_CXVIRTUALSCREEN),
            metric(SM_CYVIRTUALSCREEN),
        )

    def _cursor_or_none(self) -> tuple[int, int] | None:
        point = wt.POINT()
        return (point.x, point.y) if self._w.GetCursorPos(ctypes.byref(point)) else None

    def _settle(self, before: tuple[int, int]) -> None:
        """Wait, MOVE_SETTLE_S at most, until the cursor has left ``before``.

        The executor reads the cursor right after a move to learn where Windows
        put it; if the injected move were still queued it would read the old
        spot, and then take our own move for the user's hand on the mouse.

        A cursor that cannot get there never leaves ``before``, and polling that
        out would cost the whole budget on every move. So once a wait has run
        out, a move from that same spot waits STUCK_SETTLE_S only: enough for a
        move Windows applies at once when the cursor is free again, whose stale
        read-back the executor would take for the user's mouse. The cursor seen
        anywhere else, after a move or before one, ends the spell.
        """
        stuck = before == self._stuck_at
        deadline = clock_now() + (STUCK_SETTLE_S if stuck else MOVE_SETTLE_S)
        while True:
            now = self._cursor_or_none()
            if now is None or now != before:
                self._stuck_at = None
                return
            if clock_now() >= deadline:
                break
            time.sleep(0)
        if not stuck:  # said once per spell, not 120 times a second
            self._stuck_at = before
            log.info(
                "the cursor stayed at (%d, %d) after a move; while it is there, moves wait %g ms for it",
                *before,
                STUCK_SETTLE_S * 1000,
            )

    def _send(self, events: list[tuple[int, int, int, int]]) -> None:
        """One SendInput call of mouse events ``(flags, dx, dy, mouseData)``: Windows inserts them back to back.

        Releases Windows refused earlier go first, each as its button was pressed.
        """
        with self._input_lock:
            retries = sorted(b for b in self._unreleased if b in self._pressed)
            batch = [(button_flags(b, False, self._pressed[b]), 0, 0, 0) for b in retries] + events
            if not batch:
                return
            inputs = (INPUT * len(batch))()
            for item, (flags, dx, dy, data) in zip(inputs, batch, strict=True):
                item.type = INPUT_MOUSE
                item.mi.dx, item.mi.dy, item.mi.mouseData = dx, dy, data
                item.mi.dwFlags, item.mi.time, item.mi.dwExtraInfo = flags, 0, EXTRA_INFO_TAG
            sent = self._w.SendInput(len(batch), inputs, ctypes.sizeof(INPUT))
            # Zero: blocked (UIPI, the secure desktop) or a bad INPUT size. GetLastError is not always set.
            error = _last_error(f"SendInput ({sent} of {len(batch)} events)") if sent != len(batch) else None
            for button in retries[: max(0, sent)]:  # Windows inserts them in order: the first `sent` went in
                self._unreleased.discard(button)
                self._pressed.pop(button, None)
                log.info("released the %s button, whose release Windows had refused earlier", button)
            if error is not None:
                raise error

    # -- windows ------------------------------------------------------------------------

    def window_at(self, x: int, y: int) -> Window | None:
        w = self._w
        hit = w.WindowFromPoint(wt.POINT(int(x), int(y)))
        if not hit:
            return None
        root = w.GetAncestor(hit, GA_ROOT) or hit
        if root == w.GetShellWindow():
            return None
        pid = self._pid_of(root)
        if pid in (0, self._pid):  # gone, or ours (the overlay)
            return None
        if not w.IsWindowVisible(root) or self._cloaked(root):
            return None
        kind = self._class_name(root)
        if is_shell_window(kind):
            return None
        return Window(int(root), self._title(root), kind)

    def window_rect(self, window: Window) -> Rect:
        frame = self._frame_bounds(window.handle)
        return frame if frame is not None else self._outer(window.handle)

    def window_state(self, window: Window) -> WindowState:
        hwnd = self._existing(window)
        if self._w.IsIconic(hwnd):
            return "minimized"
        if self._w.IsZoomed(hwnd):
            return "maximized"
        return "normal"

    def set_window_rect(self, window: Window, rect: Rect) -> None:
        """Puts the visible frame on ``rect``; a window without a sizing border keeps its size, centred on it."""
        hwnd = self._existing(window)
        self._check_allowed(window, hwnd)
        current = self._outer(hwnd)
        frame = self._frame_bounds(hwnd) or current
        target = Rect(*rect.rounded())
        resizable = self._resizable(hwnd)
        if not resizable:
            # No sizing border (a dialog, a fixed tool window): its controls would not follow a new size, so it
            # keeps its own, centred where the resize or snap would have put it.
            target = Rect(*centred_on(target, frame.width, frame.height).rounded())
        x, y, cx, cy = outer_rect(target, current, frame).rounded()
        flags = SWP_NOZORDER | SWP_NOACTIVATE | SWP_NOOWNERZORDER
        if not resizable or (cx, cy) == current.rounded()[2:]:
            # A pure move: posted to the app's thread, so a hung app can't stall us. NOSIZE also leaves alone
            # the size a per-monitor aware app picks for itself when it lands on a monitor at another scale.
            flags |= SWP_NOSIZE | SWP_ASYNCWINDOWPOS
        elif self._w.IsHungAppWindow(hwnd):
            flags |= SWP_ASYNCWINDOWPOS  # a synchronous resize waits for the app
        ctypes.set_last_error(0)  # type: ignore[attr-defined]
        if not self._w.SetWindowPos(hwnd, None, x, y, cx, cy, flags):
            err = ctypes.get_last_error()  # type: ignore[attr-defined]
            if err == ERROR_ACCESS_DENIED:
                self._mark_blocked(hwnd)
                raise InputBlocked(f"Windows refused to move {_label(window)} (access denied)")
            if not self._w.IsWindow(hwnd):
                raise _gone(hwnd)
            raise _win_error(err, f"SetWindowPos({hwnd:#x})")

    def raise_window(self, window: Window) -> None:
        hwnd = self._existing(window)
        self._check_allowed(window, hwnd)
        # Posted to the app's thread like a move: the executor raises at every grab, under its lock, and a
        # synchronous call would wait for a busy or hung app to answer.
        flags = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_ASYNCWINDOWPOS
        ctypes.set_last_error(0)  # type: ignore[attr-defined]
        if not self._w.SetWindowPos(hwnd, HWND_TOP, 0, 0, 0, 0, flags):
            err = ctypes.get_last_error()  # type: ignore[attr-defined]
            if err == ERROR_ACCESS_DENIED:
                self._mark_blocked(hwnd)
                raise InputBlocked(f"Windows refused to raise {_label(window)} (access denied)")
            log.debug("could not raise window %#x (error %d)", hwnd, err)
        # Allowed while we sent the last input (our own moves); otherwise Windows flashes its taskbar button.
        # For another thread's window it posts the activation rather than waiting for the app.
        if not self._w.SetForegroundWindow(hwnd):
            log.debug("SetForegroundWindow(%#x) was refused", hwnd)

    def restore(self, window: Window) -> None:
        hwnd = self._existing(window)
        w = self._w
        before = self._outer(hwnd) if w.IsZoomed(hwnd) or w.IsIconic(hwnd) else None
        self._show(window, hwnd, SW_RESTORE)
        if before is not None:
            self._wait_restored(hwnd, before)

    def maximize(self, window: Window) -> None:
        self._show(window, self._existing(window), SW_MAXIMIZE)

    def minimize(self, window: Window) -> None:
        self._show(window, self._existing(window), SW_MINIMIZE)

    def _show(self, window: Window, hwnd: int, command: int) -> None:
        """ShowWindowAsync: posted to the app's thread, so a hung app can't stall us.

        Its result is not a plain success flag (for a window of our own thread
        it is the previous visibility), so only an access-denied error counts.
        """
        self._check_allowed(window, hwnd)
        ctypes.set_last_error(0)  # type: ignore[attr-defined]
        if not self._w.ShowWindowAsync(hwnd, command):
            err = ctypes.get_last_error()  # type: ignore[attr-defined]
            if err == ERROR_ACCESS_DENIED:
                self._mark_blocked(hwnd)
                raise InputBlocked(f"Windows refused to change the state of {_label(window)} (access denied)")
            if err:
                log.debug("ShowWindowAsync(%#x, %d) returned 0 with error %d", hwnd, command, err)

    def _wait_restored(self, hwnd: int, before: Rect) -> None:
        """The restore is only queued; the executor reads the restored rect next, so wait (bounded) for it."""
        w = self._w
        deadline = clock_now() + RESTORE_WAIT_S
        while True:
            if not w.IsWindow(hwnd):
                return
            if not w.IsZoomed(hwnd) and not w.IsIconic(hwnd):
                now = wt.RECT()
                if not w.GetWindowRect(hwnd, ctypes.byref(now)) or _rect(now) != before:
                    return
            if clock_now() >= deadline:
                log.debug("window %#x not restored after %d ms; carrying on", hwnd, round(RESTORE_WAIT_S * 1000))
                return
            time.sleep(0.005)

    def _existing(self, window: Window) -> int:
        hwnd = int(window.handle)
        if not self._w.IsWindow(hwnd):
            raise _gone(hwnd)
        return hwnd

    def _outer(self, hwnd: int) -> Rect:
        """GetWindowRect: the frame with the invisible resize borders."""
        rect = wt.RECT()
        if not self._w.GetWindowRect(hwnd, ctypes.byref(rect)):
            if not self._w.IsWindow(hwnd):
                raise _gone(hwnd)
            raise _last_error(f"GetWindowRect({hwnd:#x})")
        return _rect(rect)

    def _frame_bounds(self, hwnd: int) -> Rect | None:
        """The DWM extended frame bounds (the visible frame), or None when DWM won't say."""
        rect = wt.RECT()
        hr = self._w.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect))
        if hr != S_OK or rect.right <= rect.left or rect.bottom <= rect.top:
            return None
        return _rect(rect)

    def _resizable(self, hwnd: int) -> bool:
        """Whether the window has a sizing border (WS_THICKFRAME); True when Windows won't say."""
        ctypes.set_last_error(0)  # type: ignore[attr-defined]
        style = self._w.GetWindowLongPtrW(hwnd, GWL_STYLE)
        if not style and ctypes.get_last_error():  # type: ignore[attr-defined]
            return True
        return bool(style & WS_THICKFRAME)

    def _cloaked(self, hwnd: int) -> bool:
        """Cloaked by DWM: on another virtual desktop, or a suspended or hidden store app."""
        value = wt.DWORD(0)
        hr = self._w.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(value), ctypes.sizeof(value))
        return hr == S_OK and value.value != 0

    def _class_name(self, hwnd: int) -> str:
        buffer = ctypes.create_unicode_buffer(256)
        return buffer.value if self._w.GetClassNameW(hwnd, buffer, len(buffer)) else ""

    def _title(self, hwnd: int) -> str:
        # For another process's window this reads the stored caption and sends no message, so it can't hang.
        buffer = ctypes.create_unicode_buffer(512)
        return buffer.value if self._w.GetWindowTextW(hwnd, buffer, len(buffer)) else ""

    def _pid_of(self, hwnd: int) -> int:
        pid = wt.DWORD(0)
        self._w.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value

    # -- UIPI ---------------------------------------------------------------------------

    def _check_allowed(self, window: Window, hwnd: int) -> None:
        if self._is_blocked(hwnd):
            raise InputBlocked(f"{_label(window)} runs with higher rights than hand control (elevated)")

    def _is_blocked(self, hwnd: int) -> bool:
        """Whether the window's process runs at a higher integrity level than ours (UIPI then refuses us)."""
        pid = self._pid_of(hwnd)
        if pid in (0, self._pid):
            return False
        key = (hwnd, pid)
        blocked = self._blocked.get(key)
        if blocked is None:
            if self.integrity is None:
                return False  # our own level unknown: rely on the calls' own errors
            told, level = _process_integrity(self._w, pid)
            # A token we may not read is treated as elevated, as UIPI would treat it.
            blocked = told and (level is None or level > self.integrity)
            if len(self._blocked) >= BLOCKED_CACHE_SIZE:
                self._blocked.clear()
            self._blocked[key] = blocked
            if blocked:
                log.info(
                    "window %#x (pid %d) runs at %s integrity, above ours (%s): Windows won't let us move it",
                    hwnd,
                    pid,
                    integrity_name(level),
                    integrity_name(self.integrity),
                )
        return blocked

    def _mark_blocked(self, hwnd: int) -> None:
        pid = self._pid_of(hwnd)
        if pid:
            self._blocked[(hwnd, pid)] = True

    # -- the keyboard (the KeyDesktop protocol of desktop/base.py) -------------------------

    def foreground_window(self) -> int:
        """The foreground window's handle, 0 when there is none or Windows won't say. Cheap; never raises."""
        try:
            return int(self._w.GetForegroundWindow() or 0)
        except Exception:  # noqa: BLE001 - "no window" is the answer that holds keys back
            return 0

    def key_target(self) -> KeyTarget:
        """Where a key typed now would land, read afresh on every call.

        Fails closed: every probe that cannot answer makes the target unusable (``blocked`` "none", or "elevated",
        or ``covered``) rather than clear, and a probe that raises gives the target of no window at all. Only the
        name of the program and the layout are cosmetic, and losing them blocks nothing.
        """
        try:
            found = self._read_key_target()
        except Exception as exc:  # noqa: BLE001 - never raises into the sink; the type only: a text may name a window
            if not self._target_unreadable:
                log.warning(
                    "could not read the foreground window (%s): keys are held back until it can be read",
                    type(exc).__name__,
                )
            self._target_unreadable = True
            return _NO_KEY_TARGET
        self._target_unreadable = False
        return found

    def _read_key_target(self) -> KeyTarget:
        hwnd = self.foreground_window()
        if not hwnd:
            return _NO_KEY_TARGET
        thread, pid = self._owner_of(hwnd)
        if not thread or not pid:
            return KeyTarget(hwnd, 0, "", 0, "none", False, False)  # the owner can't be resolved: not a safe target
        own = pid == self._pid
        name = "" if own else self._exe_name(pid)
        lang_id = self._layout_id(thread)
        blocked = self._key_block(hwnd, pid)
        if blocked is not None:
            return KeyTarget(hwnd, pid, name, lang_id, blocked, False, False)
        password = self._password_focus()
        if password is None:
            return KeyTarget(hwnd, pid, name, lang_id, "none", False, False)
        return KeyTarget(hwnd, pid, name, lang_id, None, password, self._covered())

    def _owner_of(self, hwnd: int) -> tuple[int, int]:
        """(thread id, process id) of the window's owner; zeros when Windows won't say."""
        pid = wt.DWORD(0)
        thread = self._w.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(thread), pid.value

    def _key_block(self, hwnd: int, pid: int) -> _KeyBlock | None:
        """Why no key may go to this window, or None. Our own window first, then the shell, then the rights."""
        if pid == self._pid:
            return "own"
        class_name = self._class_name(hwnd)
        if not class_name:
            return "none"  # a window whose class can't be read is gone or odd: not a place to type
        if is_shell_window(class_name):
            return "shell"
        return "elevated" if self._key_elevated(hwnd, pid) else None

    def _key_elevated(self, hwnd: int, pid: int) -> bool:
        """The absolute rule: clear only when our level is known, the target's token was read, it is below High and
        not above ours. ``_is_blocked`` (the mouse's) is relative to our level and lets everything through when that
        is unknown, which for a keyboard would type into an administrator's console from an elevated helper.

        Only what Windows told is remembered: a process that could not be opened (it exited) is asked again.
        """
        if self.integrity is None:
            return True
        key = (hwnd, pid)
        known = self._key_blocked.get(key)
        if known is not None:
            return known
        told, level = _process_integrity(self._w, pid)
        if not told:
            return True
        # The exact RID, not its band: SendInput returns the full count when UIPI drops the keys (the docs say so),
        # so this is the only protection there is, and a UIAccess (0x2010) or Medium+ (0x2100) window is above a
        # Medium helper even though both share its band. Whether Windows really refuses them can't be tested here.
        elevated = level is None or level >= INTEGRITY_HIGH_RID or level > self.integrity
        if len(self._key_blocked) >= BLOCKED_CACHE_SIZE:
            self._key_blocked.clear()
        self._key_blocked[key] = elevated
        if elevated:
            log.info(
                "window %#x (pid %d) runs at %s integrity (ours: %s): keys are never sent to it",
                hwnd,
                pid,
                integrity_name(level),
                integrity_name(self.integrity),
            )
        return elevated

    def _password_focus(self) -> bool | None:
        """Whether the control with the keyboard focus is a classic password box; None when that can't be read."""
        info = GUITHREADINFO()
        info.cbSize = ctypes.sizeof(GUITHREADINFO)
        if not self._w.GetGUIThreadInfo(0, ctypes.byref(info)):  # 0: the foreground thread
            return None
        focus = info.hwndFocus
        if not focus:
            return False  # nothing has the focus: a key goes nowhere
        class_name = self._class_name(focus)
        if not class_name:
            return None
        if not is_edit_class(class_name):
            return False
        ctypes.set_last_error(0)  # type: ignore[attr-defined]
        style = self._w.GetWindowLongPtrW(focus, GWL_STYLE)
        if not style and ctypes.get_last_error():  # type: ignore[attr-defined]
            return None
        return bool(style & ES_PASSWORD)

    def _covered(self) -> bool:
        """The shell says the user is busy (a full-screen game, a presentation); True when it won't say."""
        state = ctypes.c_int(0)
        if self._w.SHQueryUserNotificationState(ctypes.byref(state)) != S_OK:
            return True
        return state.value in COVERED_STATES

    def _layout_id(self, thread: int) -> int:
        """The language id of the keyboard layout of the foreground thread (never our own: it is not typed into)."""
        try:
            return int(self._w.GetKeyboardLayout(thread) or 0) & 0xFFFF
        except Exception:  # noqa: BLE001 - the strip loses the language, nothing is blocked
            return 0

    def _exe_name(self, pid: int) -> str:
        """The program's base name without ".exe", for the strip on the local screen; "" when it can't be read."""
        name = self._exe_names.get(pid)
        if name is None:
            try:
                name = self._read_exe_name(pid)
            except Exception:  # noqa: BLE001 - cosmetic
                return ""
            if name is None:
                return ""  # could not tell (the process exited, say): asked again next time
            if len(self._exe_names) >= BLOCKED_CACHE_SIZE:
                self._exe_names.clear()
            self._exe_names[pid] = name
        return name

    def _read_exe_name(self, pid: int) -> str | None:
        """The name, "" for a process that will never say (access denied), None when it could not be read now."""
        w = self._w
        process = w.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not process:
            # A protected process stays protected: not worth a failed OpenProcess before every character.
            return "" if ctypes.get_last_error() == ERROR_ACCESS_DENIED else None  # type: ignore[attr-defined]
        try:
            buffer = ctypes.create_unicode_buffer(IMAGE_NAME_CHARS)
            size = wt.DWORD(IMAGE_NAME_CHARS)
            if not w.QueryFullProcessImageNameW(process, PROCESS_NAME_WIN32, buffer, ctypes.byref(size)):
                return None
            path = buffer.value
        finally:
            w.CloseHandle(process)
        base = ntpath.basename(path)
        stem, extension = ntpath.splitext(base)
        return stem if extension.lower() == ".exe" else base

    def send_keys(self, strokes: Sequence[KeyStroke], *, inject: Literal["unicode", "vk"] = "unicode") -> int:
        """Types the strokes in order, one SendInput call each. The number sent in full.

        Every stroke is checked and turned into events before the first is sent, so a refused one sends nothing.
        InputBlocked when Windows took none of the first stroke (the secure desktop, BlockInput); OSError when it
        took part of one (its missing key-ups are sent at once, or kept and retried). A later stroke Windows
        refuses whole ends the call with the count so far. SendInput does not report a UIPI block (it returns the
        full count and the keys vanish), so ``key_target``'s elevation rule is the protection against that, not
        this. Nothing in an exception or a log names a typed character.
        """
        if inject not in ("unicode", "vk"):
            raise ValueError("inject must be 'unicode' or 'vk'")
        batches = self._key_batches(strokes, inject)
        with self._input_lock:
            for index, events in enumerate(batches):
                self._drain_key_ledger()  # a key-up owed from before goes first, in a call of its own
                taken = self._send_events(events)
                if taken == len(events):
                    continue
                code = ctypes.get_last_error()  # type: ignore[attr-defined]  # before the rescue's calls change it
                if taken == 0:
                    if index == 0:
                        raise InputBlocked(f"Windows took none of the keys (error {code})")
                    return index
                self._release_cut_short(events[:taken])
                raise _win_error(code, f"SendInput ({taken} of {len(events)} events)")
            return len(batches)

    def _key_batches(self, strokes: Sequence[KeyStroke], inject: Literal["unicode", "vk"]) -> list[list[_KeyEvent]]:
        lookup, shift_down = None, False
        if inject == "vk" and any(s.kind == "char" for s in strokes):
            lookup, shift_down = self._key_lookup()  # asked once per call, not per character
        return [events_for(s, inject=inject, vk_lookup=lookup, shift_down=shift_down) for s in strokes]

    def _key_lookup(self) -> tuple[VkLookup | None, bool]:
        """(char -> key on the foreground thread's layout, whether the user holds Shift); no lookup, no foreground."""
        hwnd = self.foreground_window()
        thread = self._owner_of(hwnd)[0] if hwnd else 0
        if not thread:
            return None, False  # nothing to ask: the characters go in as Unicode
        w = self._w
        layout = w.GetKeyboardLayout(thread)
        if not layout:
            return None, False  # NULL would mean "our own thread's layout" to VkKeyScanExW: the wrong one
        shift_down = bool(w.GetAsyncKeyState(VK_SHIFT) & KEY_DOWN_BIT)
        caps_lock = bool(w.GetKeyState(VK_CAPITAL) & 1)

        def lookup(value: str) -> tuple[int, int, int] | None:
            answer = w.VkKeyScanExW(value, layout)
            if answer == -1:
                return None  # this layout has no key for it
            vk, state = answer & 0xFF, (answer >> 8) & 0xFF
            scan = w.MapVirtualKeyExW(vk, MAPVK_VK_TO_VSC, layout)
            if not scan:
                return None
            if caps_lock and value.lower() != value.upper():
                state ^= 1  # Caps Lock turns the Shift a letter needs around; a mark or a Hebrew letter has no case
            return vk, state, scan

        return lookup, shift_down

    def _send_events(self, events: list[_KeyEvent]) -> int:
        """One SendInput call; the number of events Windows took, in order. Notes the tick our own input leaves."""
        if not events:
            return 0
        inputs = (INPUT * len(events))()
        for item, (vk, scan, flags) in zip(inputs, events, strict=True):
            item.type = INPUT_KEYBOARD
            item.ki.wVk, item.ki.wScan, item.ki.dwFlags = vk, scan, flags
            item.ki.time, item.ki.dwExtraInfo = 0, EXTRA_INFO_TAG  # time 0: the system stamps it
        ctypes.set_last_error(0)  # type: ignore[attr-defined]
        taken = max(0, int(self._w.SendInput(len(events), inputs, ctypes.sizeof(INPUT))))
        if taken:
            self._note_own_input()
        return taken

    def _release_cut_short(self, taken: list[_KeyEvent]) -> None:
        """Let go of the keys a cut-short call left down. What can't be sent now is kept, never raised."""
        owed = missing_ups(taken)
        sent = 0
        try:
            sent = self._send_events(owed)
        except Exception as exc:  # noqa: BLE001 - the caller already hears of the cut-short call; this is kept
            log.debug("could not release the keys of a cut-short batch (%s)", type(exc).__name__)
        if sent < len(owed):
            self._keys_unreleased.extend(owed[sent:])
            log.warning("Windows left %d key-up(s) unsent; they go out ahead of the next key", len(owed) - sent)

    def _drain_key_ledger(self) -> None:
        """Send the key-ups Windows refused earlier; what is refused again stays. Never raises."""
        with self._input_lock:
            if not self._keys_unreleased:
                return
            try:
                sent = self._send_events(list(self._keys_unreleased))
            except Exception as exc:  # noqa: BLE001 - tried again with the next key or check
                log.debug("the refused key-ups did not get through yet (%s)", type(exc).__name__)
                return
            del self._keys_unreleased[:sent]
            if sent:
                log.info("released %d key(s) whose key-up Windows had refused earlier", sent)

    def release_keys(self) -> None:
        """Send any key-up Windows refused earlier. Never raises."""
        self._drain_key_ledger()

    def _note_own_input(self) -> None:
        """Remember the tick our own batch left, so ``foreign_input`` does not take it for the user's.

        Never changes what a stroke returns: the characters are typed already (retyping them would double them),
        so a failed read only means the next ``foreign_input`` says "foreign" once, which fails safe.
        """
        tick = self._last_input_tick()
        if tick is None:
            self.key_counts["own_tick_failed"] += 1
        else:
            self._own_tick = tick

    def _last_input_tick(self) -> int | None:
        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(LASTINPUTINFO)
        try:
            ok = self._w.GetLastInputInfo(ctypes.byref(info))
        except Exception:  # noqa: BLE001 - "can't tell" is an answer
            return None
        return int(info.dwTime) if ok else None

    def foreign_input(self) -> bool:
        """Whether input other than ours came since the last call (a key or the mouse); True when it can't tell.

        The first call only takes a baseline. The tick is the machine's last input of any kind, so our own batch
        moves it too: the tick our last batch left is not news.
        """
        tick = self._last_input_tick()
        if tick is None:
            return True
        with self._input_lock:
            seen = self._seen_tick
            self._seen_tick = tick
            return seen is not None and tick != seen and tick != self._own_tick

    def modifiers_down(self) -> bool:
        """A physical Ctrl, Alt or Windows key is down (a typed letter would be a shortcut). True when it can't tell.

        Shift and Caps Lock are no shortcut: a capital letter is fine.
        """
        try:
            return any(self._w.GetAsyncKeyState(vk) & KEY_DOWN_BIT for vk in SHORTCUT_MODIFIERS)
        except Exception:  # noqa: BLE001 - unknown counts as down
            return True

    def open_os_keyboard(self) -> bool:
        """Start the Windows on-screen keyboard by its absolute path (never found through PATH or the cwd)."""
        root = os.environ.get("SYSTEMROOT") or r"C:\Windows"
        path = ntpath.join(root, "System32", "osk.exe")
        try:
            handle = self._w.ShellExecuteW(None, "open", path, None, None, SW_SHOWNORMAL)
        except Exception as exc:  # noqa: BLE001 - the caller shows "could not open it"
            log.warning("could not start the on-screen keyboard (%s)", type(exc).__name__)
            return False
        started = int(handle or 0) > 32  # ShellExecute answers a value above 32 on success, an error code below
        if not started:
            log.warning("the on-screen keyboard did not start (ShellExecute answered %d)", int(handle or 0))
        return started

    # -- extras (not in the protocol; the runtime looks for them) ------------------------

    def input_desktop_ok(self) -> bool:
        """Whether the normal desktop has the input.

        False on the lock screen, a UAC prompt or the Ctrl+Alt+Del screen
        (the Winlogon secure desktop), where injected input goes nowhere.
        """
        ok = self._input_desktop_name().lower() == "default"
        if ok:
            self._retry_releases()
        return ok

    def _input_desktop_name(self) -> str:
        w = self._w
        desk = w.OpenInputDesktop(0, False, DESKTOP_SWITCHDESKTOP)
        if not desk:
            return ""
        try:
            name = ctypes.create_unicode_buffer(64)
            needed = wt.DWORD(0)
            if not w.GetUserObjectInformationW(desk, UOI_NAME, name, ctypes.sizeof(name), ctypes.byref(needed)):
                return ""
            return name.value
        finally:
            w.CloseDesktop(desk)

    def _retry_releases(self) -> None:
        """Send the releases Windows refused while another desktop had the input. Never raises."""
        self._drain_key_ledger()
        if not self._unreleased:
            return
        try:
            self._send([])
        except Exception as exc:  # noqa: BLE001 - tried again with the next input or check
            log.debug("the refused release did not get through yet: %s", exc)

    def keep_awake(self, on: bool) -> bool:
        """Keep the display and the system awake (a projector!) while hand control runs, or let them sleep again.

        SetThreadExecutionState belongs to the calling thread: the request
        lasts until that thread clears it or exits. So call this from one
        long-lived thread for both on and off (``close`` turns it off, which
        only clears it from that same thread). False when Windows refused.
        """
        flags = ES_CONTINUOUS | (ES_DISPLAY_REQUIRED | ES_SYSTEM_REQUIRED if on else 0)
        if not self._w.SetThreadExecutionState(flags):
            log.warning("SetThreadExecutionState(%#x) failed", flags)
            return False
        me = threading.get_ident()
        if on:
            self._awake_thread = me
        else:
            if self._awake_thread not in (None, me):
                log.debug(
                    "keep_awake(False) on another thread than keep_awake(True): that request lasts until it exits"
                )
            self._awake_thread = None
        return True

    def close(self) -> None:
        """Releases any button or key still held, turns keep_awake off. Idempotent; never raises."""
        with self._input_lock:
            for button in sorted(self._pressed):
                if button not in self._pressed:
                    continue  # went out ahead of an earlier one, as a refused release
                try:
                    self.button(button, False)
                except Exception as exc:  # noqa: BLE001 - closing must not raise
                    log.warning("could not release the %s button: %s", button, exc)
            self.release_keys()
        try:
            if self._awake_thread is not None:
                self.keep_awake(False)
        except Exception:
            log.exception("could not turn keep_awake off")
        self._blocked.clear()
        self._key_blocked.clear()
        self._exe_names.clear()
        self._display_cache = None
