# ruff: noqa: N802 - the fake's methods carry the Win32 names they stand in for
"""The Windows desktop backend.

Three layers: the pure helpers (every OS); the methods driven through
``FakeWin32``, a stand-in for the DLLs, so the Windows code paths run on any
OS; and the real thing on Windows (structure sizes, DPI awareness, monitors,
a cursor round trip, windows of this test process). The Windows-only tests
skip, with the reason, on a runner without an interactive desktop.
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterator
from ctypes import wintypes as wt
from dataclasses import dataclass, field
from typing import Any

import pytest

from jarvis_hands.actions import Button, MoveCursor, ReleaseAll
from jarvis_hands.desktop import windows as win
from jarvis_hands.desktop.base import InputBlocked, UnsupportedPlatform, Window
from jarvis_hands.desktop.windows import (
    MonitorRecord,
    TargetRecord,
    WindowsDesktop,
    build_displays,
    button_flags,
    integrity_name,
    integrity_rid,
    is_shell_window,
    normalize_absolute,
    outer_rect,
    wheel_data,
)
from jarvis_hands.executor import Executor
from jarvis_hands.geometry import Rect
from jarvis_hands.mapping import select_displays

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="needs Windows")

MOVE_ABS = win.MOUSEEVENTF_MOVE | win.MOUSEEVENTF_ABSOLUTE | win.MOUSEEVENTF_VIRTUALDESK
RAISE = win.SWP_NOMOVE | win.SWP_NOSIZE | win.SWP_NOACTIVATE
HDMI, DISPLAYPORT = 5, 10
MEDIUM, HIGH = 0x2000, 0x3000
WS_OVERLAPPEDWINDOW = 0x00CF0000  # includes WS_THICKFRAME
WS_POPUP, WS_CAPTION, WS_SYSMENU, WS_VISIBLE = 0x80000000, 0x00C00000, 0x00080000, 0x10000000
#: A message box or properties dialog: a caption and a close button, no sizing border.
DIALOG_STYLE = WS_POPUP | WS_CAPTION | WS_SYSMENU


def _sid(rid: int) -> bytes:
    """The mandatory-label SID S-1-16-<rid>."""
    return bytes([1, 1, 0, 0, 0, 0, 0, 16]) + rid.to_bytes(4, "little")


# --------------------------------------------------------------------------- pure helpers


def test_normalization_hits_the_edge_pixels_and_never_zero() -> None:
    screen = (0, 0, 1920, 1080)
    assert normalize_absolute(0, 0, screen) == (17, 30)
    assert normalize_absolute(1919, 1079, screen) == (65518, 65505)
    assert normalize_absolute(-500, -5, screen) == normalize_absolute(0, 0, screen)
    assert normalize_absolute(5000, 2000, screen) == normalize_absolute(1919, 1079, screen)


@pytest.mark.parametrize("left, width", [(0, 1), (0, 3), (0, 1366), (-1920, 3840), (-2560, 7680), (-3840, 11520)])
def test_normalization_round_trips_every_pixel_under_both_windows_mappings(left: int, width: int) -> None:
    for x in range(left, left + width):
        n, _ = normalize_absolute(x, 0, (left, 0, width, 1))
        assert 0 < n <= 65535
        assert left + n * width // 65536 == x
        assert left + n * width // 65535 == x


def test_normalization_with_a_monitor_above_and_left_of_the_primary() -> None:
    screen = (-1280, -1024, 3200, 2104)  # a 1280 x 1024 monitor up-left of a 1920 x 1080 primary
    nx, ny = normalize_absolute(0, 0, screen)  # the primary's top-left corner
    assert (-1280 + nx * 3200 // 65536, -1024 + ny * 2104 // 65536) == (0, 0)
    assert normalize_absolute(-1280, -1024, screen) == (10, 15)


def test_an_empty_virtual_screen_is_an_error() -> None:
    with pytest.raises(ValueError):
        normalize_absolute(0, 0, (0, 0, 0, 0))


def test_wheel_data_encodes_negative_amounts_as_a_dword() -> None:
    assert wheel_data(120) == 120
    assert wheel_data(-120) == 0xFFFFFF88
    assert wheel_data(-1) == 0xFFFFFFFF
    assert wheel_data(0) == 0
    assert wheel_data(-(2**40)) == 0x80000000  # clamped to 32 bits
    assert wheel_data(2**40) == 0x7FFFFFFF


def test_left_is_the_primary_button_also_when_the_buttons_are_swapped() -> None:
    assert button_flags("left", True, swapped=False) == win.MOUSEEVENTF_LEFTDOWN
    assert button_flags("left", False, swapped=False) == win.MOUSEEVENTF_LEFTUP
    assert button_flags("right", True, swapped=False) == win.MOUSEEVENTF_RIGHTDOWN
    assert button_flags("right", False, swapped=False) == win.MOUSEEVENTF_RIGHTUP
    assert button_flags("left", True, swapped=True) == win.MOUSEEVENTF_RIGHTDOWN
    assert button_flags("left", False, swapped=True) == win.MOUSEEVENTF_RIGHTUP
    assert button_flags("right", True, swapped=True) == win.MOUSEEVENTF_LEFTDOWN
    with pytest.raises(ValueError):
        button_flags("middle", True, swapped=False)  # type: ignore[arg-type]


def test_outer_rect_adds_the_invisible_borders_back() -> None:
    frame = Rect.from_ltrb(100, 100, 900, 700)
    window = Rect.from_ltrb(93, 100, 907, 707)  # 7 px left, right and bottom; none at the top
    assert outer_rect(Rect(0, 0, 1000, 500), window, frame) == Rect.from_ltrb(-7, 0, 1007, 507)
    scaled = Rect.from_ltrb(89, 100, 911, 711)  # 11 px at 150 %
    assert outer_rect(Rect(-1920, 20, 640, 480), scaled, frame) == Rect.from_ltrb(-1931, 20, -1269, 511)


def test_outer_rect_without_borders_or_with_incomparable_rects_is_the_target() -> None:
    target = Rect(10, 20, 300, 200)
    same = Rect(100, 100, 800, 600)
    assert outer_rect(target, same, same) == target
    # DPI-virtualized window rect against physical frame bounds: margins make no sense.
    assert outer_rect(target, Rect(66, 66, 533, 400), Rect(100, 100, 800, 600)) == target
    assert outer_rect(target, Rect(100, 100, 800, 600), Rect(0, 0, 2000, 2000)) == target


def test_shell_windows_are_never_grabbed() -> None:
    for cls in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "NotifyIconOverflowWindow"):
        assert is_shell_window(cls)
    assert is_shell_window("Windows.UI.Core.CoreWindow")
    assert is_shell_window("XamlExplorerHostIslandWindow")
    for cls in ("Notepad", "CabinetWClass", "Chrome_WidgetWin_1", "ApplicationFrameWindow", "progman"):
        assert not is_shell_window(cls)


def _monitor(device: str, x: int, y: int, w: int, h: int, *, primary: bool = False, taskbar: int = 48) -> MonitorRecord:
    return MonitorRecord(device, Rect(x, y, w, h), Rect(x, y, w, h - taskbar), primary)


def test_displays_rotems_pc_monitor_and_the_virtual_display_driver() -> None:
    monitors = [
        _monitor(r"\\.\DISPLAY1", 0, 0, 2560, 1440, primary=True),
        _monitor(r"\\.\DISPLAY2", 2560, 0, 1920, 1080, taskbar=0),
    ]
    targets = {
        r"\\.\DISPLAY1": [TargetRecord("DELL S2721DGF", DISPLAYPORT)],
        r"\\.\DISPLAY2": [TargetRecord("VDD by MTT", HDMI)],  # the virtual driver claims HDMI
    }
    adapters = {r"\\.\DISPLAY1": "NVIDIA GeForce RTX 4070 Ti", r"\\.\DISPLAY2": "Virtual Display Driver"}
    first, second = build_displays(monitors, targets, adapters)
    assert (first.id, first.name, first.primary, first.virtual) == (1, "DELL S2721DGF", True, False)
    assert first.rect == Rect(0, 0, 2560, 1440) and first.work == Rect(0, 0, 2560, 1392)
    assert (second.id, second.name, second.primary, second.virtual) == (2, "VDD by MTT", False, True)


def test_indirect_virtual_outputs_and_names_that_say_virtual_are_virtual() -> None:
    monitors = [
        _monitor(r"\\.\DISPLAY1", 0, 0, 1920, 1080, primary=True),
        _monitor(r"\\.\DISPLAY3", 1920, 0, 1920, 1080),
    ]
    for target in (
        TargetRecord("", win.DISPLAYCONFIG_OUTPUT_TECHNOLOGY_INDIRECT_VIRTUAL),
        TargetRecord("Parsec Virtual Display", HDMI),
        TargetRecord("Virtual Dummy", win.DISPLAYCONFIG_OUTPUT_TECHNOLOGY_INDIRECT_WIRED),
    ):
        displays = build_displays(monitors, {r"\\.\DISPLAY3": [target]}, {})
        assert [d.virtual for d in displays] == [False, True], target
    displays = build_displays(monitors, {}, {r"\\.\display3": "IddSampleDriver Device"})  # keys in any case
    assert [d.virtual for d in displays] == [False, True]


def test_a_projector_on_a_usb_display_adapter_is_a_real_display() -> None:
    # DisplayLink docks and USB display adapters are indirect *wired* outputs: a physical screen hand control
    # must reach under "all". Only the names (and the indirect *virtual* technology) make a display virtual.
    monitors = [
        _monitor(r"\\.\DISPLAY1", 0, 0, 1920, 1080, primary=True),
        _monitor(r"\\.\DISPLAY5", 1920, 0, 1920, 1080, taskbar=0),
    ]
    targets = {
        r"\\.\DISPLAY1": [TargetRecord("DELL S2721DGF", HDMI)],
        r"\\.\DISPLAY5": [TargetRecord("EPSON PJ", win.DISPLAYCONFIG_OUTPUT_TECHNOLOGY_INDIRECT_WIRED)],
    }
    adapters = {r"\\.\DISPLAY1": "NVIDIA GeForce RTX 4070 Ti", r"\\.\DISPLAY5": "DisplayLink USB Device"}
    displays = build_displays(monitors, targets, adapters)
    assert [(d.name, d.virtual) for d in displays] == [("DELL S2721DGF", False), ("EPSON PJ", False)]
    assert [d.name for d in select_displays(displays, "all")] == ["DELL S2721DGF", "EPSON PJ"]
    # Rotem's Virtual Display Driver stays virtual whatever output it claims: its adapter says so.
    adapters[r"\\.\DISPLAY5"] = "Virtual Display Driver"
    assert [d.virtual for d in build_displays(monitors, targets, adapters)] == [False, True]


def test_a_display_duplicated_onto_a_real_monitor_is_not_virtual() -> None:
    monitors = [_monitor(r"\\.\DISPLAY1", 0, 0, 1920, 1080, primary=True)]
    targets = {r"\\.\DISPLAY1": [TargetRecord("LG ULTRAGEAR", DISPLAYPORT), TargetRecord("EPSON PJ", HDMI)]}
    (display,) = build_displays(monitors, targets, {})
    assert display.name == "LG ULTRAGEAR + EPSON PJ" and not display.virtual
    targets[r"\\.\DISPLAY1"].append(TargetRecord("", 17))
    (display,) = build_displays(monitors, targets, {})
    assert not display.virtual


def test_displays_without_display_config_fall_back_to_device_names() -> None:
    monitors = [
        _monitor(r"\\.\DISPLAY1", 0, 0, 1920, 1080),
        _monitor(r"\\.\DISPLAY2", -1920, 0, 1920, 1080, primary=True),
    ]
    displays = build_displays(monitors, {}, {})
    assert [(d.id, d.name, d.primary, d.virtual) for d in displays] == [
        (1, r"\\.\DISPLAY1", False, False),
        (2, r"\\.\DISPLAY2", True, False),
    ]
    # Monitors without a friendly name (common for laptop panels) keep the device name too.
    displays = build_displays(monitors, {r"\\.\DISPLAY1": [TargetRecord("", 0x80000000)]}, {})
    assert displays[0].name == r"\\.\DISPLAY1"


def test_two_identical_monitors_get_their_device_names() -> None:
    monitors = [
        _monitor(r"\\.\DISPLAY1", 0, 0, 1920, 1080, primary=True),
        _monitor(r"\\.\DISPLAY2", 1920, 0, 1920, 1080),
    ]
    targets = {device.device: [TargetRecord("DELL P2419H", DISPLAYPORT)] for device in monitors}
    assert [d.name for d in build_displays(monitors, targets, {})] == [
        "DELL P2419H (DISPLAY1)",
        "DELL P2419H (DISPLAY2)",
    ]


def test_integrity_levels_from_mandatory_label_sids() -> None:
    assert integrity_rid(_sid(MEDIUM)) == MEDIUM
    assert integrity_rid(_sid(HIGH)) == HIGH
    assert (
        integrity_rid(bytes([1, 2, 0, 0, 0, 0, 0, 16]) + (1).to_bytes(4, "little") + (0x4000).to_bytes(4, "little"))
        == 0x4000
    )
    for bad in (b"", b"\x02\x01\x00\x00\x00\x00\x00\x10\x00\x20\x00\x00", bytes([1, 3, 0, 0, 0, 0, 0, 16]) + bytes(4)):
        with pytest.raises(ValueError):
            integrity_rid(bad)
    assert [integrity_name(r) for r in (0x1000, MEDIUM, HIGH, 0x4000, None)] == [
        "low",
        "medium",
        "high",
        "system",
        "unknown",
    ]


@pytest.mark.skipif(sys.platform == "win32", reason="checks the behaviour off Windows")
def test_off_windows_there_is_no_backend_and_dpi_awareness_is_unsupported() -> None:
    with pytest.raises(UnsupportedPlatform):
        WindowsDesktop()
    assert win.make_dpi_aware() == "unsupported"
    assert win.make_dpi_aware() == "unsupported"


# --------------------------------------------------------------------------- the methods, through a fake of the DLLs

OUR_PID = os.getpid()
OTHER_PID = 4242
SELF_PROCESS = 0xFFFFFFFFFFFFFFFF  # GetCurrentProcess()'s pseudo handle


@dataclass
class FakeWin:
    hwnd: int
    pid: int = OTHER_PID
    cls: str = "Notepad"
    title: str = "notes.txt - Notepad"
    #: The visible frame (ltrb) and the invisible borders around it (left, top, right, bottom).
    frame: tuple[int, int, int, int] = (100, 100, 900, 700)
    borders: tuple[int, int, int, int] = (7, 0, 7, 7)
    visible: bool = True
    cloaked: bool = False
    zoomed: bool = False
    iconic: bool = False
    hung: bool = False
    #: GWL_STYLE, or None for a GetWindowLongPtrW that fails.
    style: int | None = WS_OVERLAPPEDWINDOW
    #: SetWindowPos and ShowWindowAsync fail with ERROR_ACCESS_DENIED.
    deny: bool = False
    normal_frame: tuple[int, int, int, int] | None = None
    #: IsZoomed reads before a queued restore takes effect.
    restore_lag: int = 0
    pending: list[int] = field(default_factory=list)
    #: The id of the thread that owns the window (GetWindowThreadProcessId); the keyboard layout is per thread.
    tid: int = 77

    @property
    def outer(self) -> tuple[int, int, int, int]:
        (left, top, right, bottom), (bl, bt, br, bb) = self.frame, self.borders
        return left - bl, top - bt, right + br, bottom + bb


#: The keys of a US layout, as VkKeyScanExW answers: the virtual key in the low byte, the shift state in the high byte
#: (1 Shift). Letters are their upper-case ASCII code; the marks sit on the OEM keys.
_US_OEM = {",": 0xBC, ".": 0xBE, "/": 0xBF, "-": 0xBD, "'": 0xDE}
US_LAYOUT: dict[str, int] = {
    **{c: ord(c.upper()) for c in "abcdefghijklmnopqrstuvwxyz"},
    **{c: ord(c) | 0x100 for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"},
    **_US_OEM,
    "?": 0xBF | 0x100,
}
#: The Hebrew layout puts its letters on the keys that carry Latin letters on a US one: the home row is enough.
HE_LAYOUT: dict[str, int] = {"ש": 0x41, "ד": 0x53, "ג": 0x44, "כ": 0x46, "ע": 0x47, "י": 0x48, ",": 0xBC}
#: Scan codes (set 1) of the keys above, as MapVirtualKeyExW(MAPVK_VK_TO_VSC) answers on any layout.
SCAN_CODES: dict[int, int] = {
    **dict(zip(map(ord, "QWERTYUIOP"), range(0x10, 0x1A), strict=True)),
    **dict(zip(map(ord, "ASDFGHJKL"), range(0x1E, 0x27), strict=True)),
    **dict(zip(map(ord, "ZXCVBNM"), range(0x2C, 0x33), strict=True)),
    0xBC: 0x33,
    0xBE: 0x34,
    0xBF: 0x35,
    0xBD: 0x0C,
    0xDE: 0x28,
}
US_HKL, HE_HKL = 0x04090409, 0x040D040D  # the language id is the low word, the keyboard id the high one


class FakeWin32:
    """The DLL functions WindowsDesktop calls, over a small in-memory Windows.

    Every call to one of its Win32 names is counted in ``calls`` and can be made to raise with ``broken`` (name ->
    exception), so a test can say "this API fails" without a method per API.
    """

    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()
        self.broken: dict[str, BaseException] = {}
        self.last_error = 0
        self.metrics: dict[int, int] = {
            win.SM_XVIRTUALSCREEN: 0,
            win.SM_YVIRTUALSCREEN: 0,
            win.SM_CXVIRTUALSCREEN: 1920,
            win.SM_CYVIRTUALSCREEN: 1080,
            win.SM_SWAPBUTTON: 0,
            win.SM_CXDOUBLECLK: 4,
            win.SM_CYDOUBLECLK: 4,
        }
        self.cursor = (100, 100)
        self.cursor_ok = True
        self.cursor_reads = 0
        #: GetCursorPos reads before an injected move shows (0: at once; -1: never).
        self.move_lag = 0
        self._pending_move: tuple[int, int] | None = None
        self._lag_left = 0
        self.sent: list[tuple[int, int, int, int, int]] = []
        self.send_limit: int | None = None
        self.monitors: list[tuple[str, tuple[int, int, int, int], tuple[int, int, int, int], bool]] = [
            (r"\\.\DISPLAY1", (0, 0, 1920, 1080), (0, 0, 1920, 1040), True)
        ]
        self.adapters: list[tuple[str, str]] = [(r"\\.\DISPLAY1", "NVIDIA GeForce RTX 4070 Ti")]
        #: (source id, target id, output technology, GDI name, friendly name or None for a failing lookup)
        self.paths: list[tuple[int, int, int, str, str | None]] = [(0, 100, DISPLAYPORT, r"\\.\DISPLAY1", "DELL")]
        self.display_config_rc = 0
        self.display_config_calls = 0
        self.windows: dict[int, FakeWin] = {}
        self.hits: dict[tuple[int, int], int] = {}
        self.roots: dict[int, int] = {}
        self.shell: int | None = 0x10010
        self.levels: dict[int | str, Any] = {"self": MEDIUM, OTHER_PID: MEDIUM}
        self.open_process_calls = 0
        self.closed_handles: list[int] = []
        self.set_window_pos: list[tuple[int, Any, int, int, int, int, int]] = []
        self.shown: list[tuple[int, int]] = []
        self.show_result = 1
        self.foreground: list[int] = []
        self.input_desktop: str | None = "Default"
        self.desktops_closed = 0
        self.execution_states: list[int] = []
        # -- the keyboard side
        #: Per SendInput call with keyboard input: the (wVk, wScan, dwFlags) of the events Windows took.
        self.sent_keys: list[list[tuple[int, int, int]]] = []
        #: (time, dwExtraInfo) of every keyboard event taken, in order.
        self.key_tags: list[tuple[int, int]] = []
        #: How many events each of those calls asked for.
        self.key_requests: list[int] = []
        #: Per-call limits, consumed first (a call takes at most that many events); then ``send_limit`` applies.
        self.send_script: list[int] = []
        #: GetForegroundWindow, and the window with the keyboard focus (None: the foreground window itself).
        self.foreground_hwnd = 0
        self.focus_hwnd: int | None = None
        self.gui_info_ok = True
        #: thread id -> HKL; GetKeyboardLayout asks for the foreground thread, never for ours (0).
        self.layouts: dict[int, int] = {}
        self.default_layout = US_HKL
        self.layout_queries: list[int] = []
        #: HKL -> {character: VkKeyScanExW answer}; a character it does not hold is -1 (no such key).
        self.key_maps: dict[int, dict[str, int]] = {US_HKL: dict(US_LAYOUT), HE_HKL: dict(HE_LAYOUT)}
        self.scan_codes: dict[int, int] = dict(SCAN_CODES)
        self.vk_queries: list[tuple[str, int]] = []
        #: Virtual keys physically down, and toggled on (Caps Lock).
        self.keys_down: set[int] = set()
        self.toggled: set[int] = set()
        #: GetLastInputInfo's tick; every injected batch moves it on, and so does ``user_input``.
        self.tick = 100_000
        self.tick_step = 7
        self.last_input_ok = True
        #: pid -> the executable's full path, for QueryFullProcessImageNameW.
        self.images: dict[int, str] = {}
        self.notification_state = win.QUNS_ACCEPTS_NOTIFICATIONS
        self.notification_hr = 0
        self.shell_executes: list[tuple[Any, ...]] = []
        self.shell_result: int | None = 42

    def __getattribute__(self, name: str) -> Any:
        value = object.__getattribute__(self, name)
        if name[:1].isupper() and callable(value):
            broken = object.__getattribute__(self, "broken")
            object.__getattribute__(self, "calls")[name] += 1
            if name in broken:
                failure = broken[name]

                def fail(*args: Any, **kwargs: Any) -> Any:
                    raise failure

                return fail
        return value

    def user_input(self, ms: int = 11) -> None:
        """The USER touches the real keyboard or mouse: the last-input tick moves on."""
        self.tick += ms

    def set_last_error(self, value: int) -> int:
        old, self.last_error = self.last_error, value
        return old

    def _fail(self, error: int, result: Any = 0) -> Any:
        self.last_error = error
        return result

    def add(self, window: FakeWin) -> FakeWin:
        self.windows[window.hwnd] = window
        return window

    # -- input and metrics

    def GetSystemMetrics(self, index: int) -> int:
        return self.metrics.get(index, 0)

    def GetCursorPos(self, ref: Any) -> int:
        self.cursor_reads += 1
        if not self.cursor_ok:
            return self._fail(win.ERROR_ACCESS_DENIED)
        if self._pending_move is not None and self._lag_left >= 0:
            if self._lag_left == 0:
                self.cursor, self._pending_move = self._pending_move, None
            else:
                self._lag_left -= 1
        ref._obj.x, ref._obj.y = self.cursor
        return 1

    def SendInput(self, count: int, inputs: Any, size: int) -> int:
        assert size == ctypes.sizeof(win.INPUT)
        limit = self.send_script.pop(0) if self.send_script else self.send_limit
        accepted = count if limit is None else min(count, limit)
        left, top, width, height = (self.metrics[i] for i in range(76, 80))
        asked = list(inputs)[:count]
        keyboard = any(item.type == win.INPUT_KEYBOARD for item in asked)
        if keyboard:
            self.key_requests.append(count)
            self.sent_keys.append([])
        for item in asked[:accepted]:
            if item.type == win.INPUT_KEYBOARD:
                ki = item.ki
                self.sent_keys[-1].append((ki.wVk, ki.wScan, ki.dwFlags))
                self.key_tags.append((ki.time, ki.dwExtraInfo))
                continue
            assert item.type == win.INPUT_MOUSE
            mi = item.mi
            self.sent.append((mi.dwFlags, mi.dx, mi.dy, mi.mouseData, mi.dwExtraInfo))
            if mi.dwFlags & MOVE_ABS == MOVE_ABS:
                target = (left + mi.dx * width // 65536, top + mi.dy * height // 65536)
                if self.move_lag:
                    self._pending_move, self._lag_left = target, self.move_lag
                else:
                    self.cursor = target
        if accepted:
            self.tick += self.tick_step  # injected input counts as input for GetLastInputInfo
        return accepted if accepted == count else self._fail(win.ERROR_ACCESS_DENIED, accepted)

    def GetDoubleClickTime(self) -> int:
        return 500

    # -- monitors

    def MONITORENUMPROC(self, function: Callable[..., bool]) -> Callable[..., bool]:
        return function

    def EnumDisplayMonitors(self, hdc: Any, clip: Any, callback: Callable[..., bool], data: int) -> int:
        assert hdc is None and clip is None
        for index in range(len(self.monitors)):
            if not callback(1000 + index, None, None, data):
                break
        return 1

    def GetMonitorInfoW(self, hmonitor: int, ref: Any) -> int:
        info = ref._obj
        assert info.cbSize == ctypes.sizeof(win.MONITORINFOEXW)
        device, rect, work, primary = self.monitors[hmonitor - 1000]
        info.rcMonitor, info.rcWork = wt.RECT(*rect), wt.RECT(*work)
        info.dwFlags, info.szDevice = (win.MONITORINFOF_PRIMARY if primary else 0), device
        return 1

    def EnumDisplayDevicesW(self, device: Any, index: int, ref: Any, flags: int) -> int:
        assert device is None and ref._obj.cb == ctypes.sizeof(win.DISPLAY_DEVICEW)
        if index >= len(self.adapters):
            return 0
        ref._obj.DeviceName, ref._obj.DeviceString = self.adapters[index]
        return 1

    def GetDisplayConfigBufferSizes(self, flags: int, npaths: Any, nmodes: Any) -> int:
        assert flags == win.QDC_ONLY_ACTIVE_PATHS
        self.display_config_calls += 1
        if self.display_config_rc:
            return self.display_config_rc
        npaths._obj.value, nmodes._obj.value = len(self.paths), 2 * len(self.paths)
        return 0

    def QueryDisplayConfig(self, flags: int, npaths: Any, paths: Any, nmodes: Any, modes: Any, topology: Any) -> int:
        assert topology is None and len(paths) == npaths._obj.value and len(modes) == nmodes._obj.value
        if npaths._obj.value < len(self.paths):
            return win.ERROR_INSUFFICIENT_BUFFER
        for path, (source_id, target_id, technology, _gdi, _name) in zip(paths, self.paths, strict=False):
            path.sourceInfo.adapterId.LowPart, path.sourceInfo.id = 7, source_id
            path.targetInfo.adapterId.LowPart, path.targetInfo.id = 7, target_id
            path.targetInfo.outputTechnology = technology
        npaths._obj.value = len(self.paths)
        return 0

    def DisplayConfigGetDeviceInfo(self, ref: Any) -> int:
        header = ref._obj
        assert header.adapterId.LowPart == 7
        address = ctypes.addressof(header)
        if header.type == win.DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME:
            assert header.size == ctypes.sizeof(win.DISPLAYCONFIG_SOURCE_DEVICE_NAME)
            source = win.DISPLAYCONFIG_SOURCE_DEVICE_NAME.from_address(address)
            source.viewGdiDeviceName = next(p[3] for p in self.paths if p[0] == header.id)
            return 0
        assert header.type == win.DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME
        assert header.size == ctypes.sizeof(win.DISPLAYCONFIG_TARGET_DEVICE_NAME)
        name = next(p[4] for p in self.paths if p[1] == header.id)
        if name is None:
            return 31  # ERROR_GEN_FAILURE
        win.DISPLAYCONFIG_TARGET_DEVICE_NAME.from_address(address).monitorFriendlyDeviceName = name
        return 0

    # -- windows

    def WindowFromPoint(self, point: Any) -> int | None:
        return self.hits.get((point.x, point.y))

    def GetAncestor(self, hwnd: int, flags: int) -> int | None:
        assert flags == win.GA_ROOT
        return self.roots.get(hwnd, hwnd)

    def GetShellWindow(self) -> int | None:
        return self.shell

    def GetWindowThreadProcessId(self, hwnd: int, ref: Any) -> int:
        window = self.windows.get(hwnd)
        if window is None:
            return 0
        ref._obj.value = window.pid
        return window.tid

    def IsWindow(self, hwnd: int) -> int:
        return int(hwnd in self.windows)

    def IsWindowVisible(self, hwnd: int) -> int:
        return int(self.windows[hwnd].visible)

    def IsIconic(self, hwnd: int) -> int:
        return int(hwnd in self.windows and self.windows[hwnd].iconic)

    def IsZoomed(self, hwnd: int) -> int:
        window = self.windows.get(hwnd)
        if window is None:
            return 0
        if window.pending:
            if window.restore_lag > 0:
                window.restore_lag -= 1
            else:
                for command in window.pending:
                    self._apply_show(window, command)
                window.pending.clear()
        return int(window.zoomed)

    def IsHungAppWindow(self, hwnd: int) -> int:
        return int(self.windows[hwnd].hung)

    def GetWindowLongPtrW(self, hwnd: int, index: int) -> int:
        assert index == win.GWL_STYLE
        window = self.windows.get(hwnd)
        if window is None or window.style is None:
            return self._fail(1400)  # ERROR_INVALID_WINDOW_HANDLE
        style = window.style
        return style - 2**32 if style & 0x80000000 else style  # a LONG, sign-extended like Windows does

    def GetClassNameW(self, hwnd: int, buffer: Any, size: int) -> int:
        if hwnd not in self.windows:
            return self._fail(1400)  # ERROR_INVALID_WINDOW_HANDLE
        buffer.value = self.windows[hwnd].cls[: size - 1]
        return len(buffer.value)

    def GetWindowTextW(self, hwnd: int, buffer: Any, size: int) -> int:
        buffer.value = self.windows[hwnd].title[: size - 1]
        return len(buffer.value)

    def GetWindowRect(self, hwnd: int, ref: Any) -> int:
        if hwnd not in self.windows:
            return self._fail(1400)  # ERROR_INVALID_WINDOW_HANDLE
        ref._obj.left, ref._obj.top, ref._obj.right, ref._obj.bottom = self.windows[hwnd].outer
        return 1

    def DwmGetWindowAttribute(self, hwnd: int, attribute: int, ref: Any, size: int) -> int:
        window = self.windows.get(hwnd)
        if window is None:
            return -2147024890  # E_HANDLE
        if attribute == win.DWMWA_EXTENDED_FRAME_BOUNDS:
            assert size == ctypes.sizeof(wt.RECT)
            ref._obj.left, ref._obj.top, ref._obj.right, ref._obj.bottom = window.frame
            return 0
        assert attribute == win.DWMWA_CLOAKED and size == ctypes.sizeof(wt.DWORD)  # 4 on Windows
        ref._obj.value = 2 if window.cloaked else 0  # DWM_CLOAKED_SHELL
        return 0

    def SetWindowPos(self, hwnd: int, after: Any, x: int, y: int, cx: int, cy: int, flags: int) -> int:
        self.set_window_pos.append((hwnd, after, x, y, cx, cy, flags))
        window = self.windows.get(hwnd)
        if window is None:
            return self._fail(1400)
        if window.deny:
            return self._fail(win.ERROR_ACCESS_DENIED)
        left, top, right, bottom = window.outer
        if not flags & win.SWP_NOMOVE:
            left, top, right, bottom = x, y, x + right - left, y + bottom - top
        if not flags & win.SWP_NOSIZE:
            right, bottom = left + cx, top + cy
        bl, bt, br, bb = window.borders
        window.frame = (left + bl, top + bt, right - br, bottom - bb)
        return 1

    def ShowWindowAsync(self, hwnd: int, command: int) -> int:
        self.shown.append((hwnd, command))
        window = self.windows[hwnd]
        if window.deny:
            return self._fail(win.ERROR_ACCESS_DENIED)
        if window.restore_lag:
            window.pending.append(command)
        else:
            self._apply_show(window, command)
        return self.show_result

    def _apply_show(self, window: FakeWin, command: int) -> None:
        if command == win.SW_MAXIMIZE:
            window.normal_frame, window.zoomed, window.frame = window.frame, True, (0, 0, 1920, 1040)
        elif command == win.SW_MINIMIZE:
            window.iconic = True
        elif command == win.SW_RESTORE:
            if window.zoomed and window.normal_frame is not None:
                window.frame = window.normal_frame
            window.zoomed = window.iconic = False

    def SetForegroundWindow(self, hwnd: int) -> int:
        self.foreground.append(hwnd)
        return 0  # refused: must not matter

    # -- the session

    def OpenInputDesktop(self, flags: int, inherit: bool, access: int) -> int | None:
        assert access == win.DESKTOP_SWITCHDESKTOP
        return 0x77 if self.input_desktop is not None else self._fail(win.ERROR_ACCESS_DENIED, None)

    def CloseDesktop(self, desk: int) -> int:
        self.desktops_closed += 1
        return 1

    def GetUserObjectInformationW(self, desk: int, index: int, buffer: Any, size: int, needed: Any) -> int:
        assert index == win.UOI_NAME and size == ctypes.sizeof(buffer)
        buffer.value = self.input_desktop
        return 1

    def SetThreadExecutionState(self, flags: int) -> int:
        self.execution_states.append(flags)
        return win.ES_CONTINUOUS

    # -- integrity levels

    def GetCurrentProcess(self) -> int:
        return SELF_PROCESS

    def OpenProcess(self, access: int, inherit: bool, pid: int) -> int | None:
        assert access == win.PROCESS_QUERY_LIMITED_INFORMATION and not inherit
        self.open_process_calls += 1
        level = self.levels.get(pid)
        if level == "denied":
            return self._fail(win.ERROR_ACCESS_DENIED, None)
        if level is None:
            return self._fail(87, None)  # ERROR_INVALID_PARAMETER: no such process
        return 50000 + pid

    def OpenProcessToken(self, process: int, access: int, ref: Any) -> int:
        assert access == win.TOKEN_QUERY
        level = self.levels["self"] if process == SELF_PROCESS else self.levels[process - 50000]
        if level == "no-token":
            return self._fail(win.ERROR_ACCESS_DENIED)
        ref._obj.value = 90000 + level
        return 1

    def GetTokenInformation(self, token: Any, kind: int, buffer: Any, size: int, needed: Any) -> int:
        assert kind == win.TOKEN_INTEGRITY_LEVEL
        sid = _sid(token.value - 90000)
        total = 16 + len(sid)  # TOKEN_MANDATORY_LABEL, then the SID it points at
        needed._obj.value = total
        if buffer is None or size < total:
            return self._fail(win.ERROR_INSUFFICIENT_BUFFER)
        base = ctypes.addressof(buffer)
        ctypes.memmove(base + 16, sid, len(sid))
        ctypes.c_void_p.from_buffer(buffer).value = base + 16
        return 1

    # -- the keyboard side

    def GetForegroundWindow(self) -> int | None:
        return self.foreground_hwnd or None  # a NULL handle comes back as None

    def GetGUIThreadInfo(self, tid: int, ref: Any) -> int:
        assert tid == 0, "the foreground thread is asked for with 0, not with a thread id"
        info = ref._obj
        assert info.cbSize == ctypes.sizeof(win.GUITHREADINFO)
        if not self.gui_info_ok:
            return self._fail(win.ERROR_ACCESS_DENIED)
        focus = self.focus_hwnd if self.focus_hwnd is not None else self.foreground_hwnd
        info.hwndActive, info.hwndFocus = self.foreground_hwnd or None, focus or None
        return 1

    def GetKeyboardLayout(self, tid: int) -> int | None:
        self.layout_queries.append(tid)
        return self.layouts.get(tid, self.default_layout)

    def VkKeyScanExW(self, char: str, hkl: int) -> int:
        self.vk_queries.append((char, hkl))
        return self.key_maps.get(hkl, {}).get(char, -1)

    def MapVirtualKeyExW(self, code: int, kind: int, hkl: int) -> int:
        assert kind == win.MAPVK_VK_TO_VSC
        return self.scan_codes.get(code, 0)

    def GetLastInputInfo(self, ref: Any) -> int:
        info = ref._obj
        assert info.cbSize == ctypes.sizeof(win.LASTINPUTINFO)
        if not self.last_input_ok:
            return self._fail(win.ERROR_ACCESS_DENIED)
        info.dwTime = self.tick
        return 1

    def GetAsyncKeyState(self, vk: int) -> int:
        return -32768 if vk in self.keys_down else 0  # the high bit of a SHORT: down now

    def GetKeyState(self, vk: int) -> int:
        return 1 if vk in self.toggled else 0

    def QueryFullProcessImageNameW(self, process: int, flags: int, buffer: Any, size: Any) -> int:
        assert flags == 0 and 0 < size._obj.value <= 32768
        path = self.images.get(process - 50000)
        if path is None or len(path) >= size._obj.value:
            return self._fail(win.ERROR_INSUFFICIENT_BUFFER if path else 31)
        buffer.value = path
        size._obj.value = len(path)
        return 1

    def SHQueryUserNotificationState(self, ref: Any) -> int:
        ref._obj.value = self.notification_state
        return self.notification_hr

    def ShellExecuteW(self, hwnd: Any, operation: Any, file: Any, params: Any, directory: Any, show: int) -> int | None:
        self.shell_executes.append((hwnd, operation, file, params, directory, show))
        return self.shell_result

    def CloseHandle(self, handle: Any) -> int:
        self.closed_handles.append(getattr(handle, "value", handle))
        return 1


def install_fake_api(monkeypatch: pytest.MonkeyPatch) -> FakeWin32:
    """A FakeWin32, with ctypes' last-error functions standing in for it (also used by ``test_kb_desktop.py``)."""
    fake = FakeWin32()
    win.make_dpi_aware()  # the real, process-wide call (on Windows), made before last-error is faked
    # Off Windows ctypes has no last-error functions; on Windows these stand in for the real ones.
    monkeypatch.setattr(ctypes, "get_last_error", lambda: fake.last_error, raising=False)
    monkeypatch.setattr(ctypes, "set_last_error", fake.set_last_error, raising=False)
    return fake


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> FakeWin32:
    return install_fake_api(monkeypatch)


@pytest.fixture
def wdesk(api: FakeWin32) -> Iterator[WindowsDesktop]:
    desktop = WindowsDesktop(api=api)
    yield desktop
    desktop.close()


def test_construction_reads_our_integrity_level(api: FakeWin32) -> None:
    assert WindowsDesktop(api=api).integrity == MEDIUM
    api.levels["self"] = HIGH
    assert WindowsDesktop(api=api).integrity == HIGH
    api.levels["self"] = "no-token"
    assert WindowsDesktop(api=api).integrity is None
    assert 90000 + MEDIUM in api.closed_handles  # the token was closed


def test_displays_names_and_virtual_flag_from_windows(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    api.monitors.append((r"\\.\DISPLAY2", (2560, 0, 4480, 1080), (2560, 0, 4480, 1080), False))
    api.monitors[0] = (r"\\.\DISPLAY1", (0, 0, 2560, 1440), (0, 0, 2560, 1392), True)
    api.adapters.append((r"\\.\DISPLAY2", "Virtual Display Driver"))
    api.paths.append((1, 101, HDMI, r"\\.\DISPLAY2", "VDD by MTT"))
    first, second = wdesk.displays()
    assert (first.id, first.name, first.rect, first.work, first.primary, first.virtual) == (
        1,
        "DELL",
        Rect(0, 0, 2560, 1440),
        Rect(0, 0, 2560, 1392),
        True,
        False,
    )
    assert (second.id, second.name, second.rect, second.primary, second.virtual) == (
        2,
        "VDD by MTT",
        Rect(2560, 0, 1920, 1080),
        False,
        True,
    )


def test_displays_are_named_again_only_when_the_layout_changes(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    assert [d.name for d in wdesk.displays()] == ["DELL"]
    assert [d.name for d in wdesk.displays()] == ["DELL"]
    assert api.display_config_calls == 1
    api.monitors.append((r"\\.\DISPLAY2", (1920, 0, 3200, 720), (1920, 0, 3200, 720), False))
    api.paths.append((1, 101, HDMI, r"\\.\DISPLAY2", "EPSON PJ"))
    assert [d.name for d in wdesk.displays()] == ["DELL", "EPSON PJ"]
    assert api.display_config_calls == 2


def test_displays_survive_failing_display_config_calls(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    api.monitors.append((r"\\.\DISPLAY2", (1920, 0, 3840, 1080), (1920, 0, 3840, 1080), False))
    api.adapters.append((r"\\.\DISPLAY2", "Virtual Display Driver"))
    api.display_config_rc = 87
    displays = wdesk.displays()
    assert [(d.name, d.virtual) for d in displays] == [(r"\\.\DISPLAY1", False), (r"\\.\DISPLAY2", True)]


def test_display_config_that_failed_is_asked_again_until_windows_answers(
    api: FakeWin32, wdesk: WindowsDesktop, caplog: pytest.LogCaptureFixture
) -> None:
    # An indirect display whose adapter name doesn't say "virtual": only the output technology gives it away.
    api.monitors.append((r"\\.\DISPLAY2", (1920, 0, 3840, 1080), (1920, 0, 3840, 1080), False))
    api.adapters.append((r"\\.\DISPLAY2", "IDD HDR Monitor Adapter"))
    api.paths.append((1, 101, win.DISPLAYCONFIG_OUTPUT_TECHNOLOGY_INDIRECT_VIRTUAL, r"\\.\DISPLAY2", "Some screen"))
    api.display_config_rc = win.ERROR_ACCESS_DENIED  # hand control started while the console was locked
    with caplog.at_level("DEBUG", logger=win.__name__):
        for _ in range(3):
            assert [(d.name, d.virtual) for d in wdesk.displays()] == [
                (r"\\.\DISPLAY1", False),
                (r"\\.\DISPLAY2", False),
            ]
    assert api.display_config_calls == 3  # asked on every poll while it fails...
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1  # ...but warned about once
    api.display_config_rc = 0
    assert [(d.name, d.virtual) for d in wdesk.displays()] == [("DELL", False), ("Some screen", True)]
    assert [(d.name, d.virtual) for d in wdesk.displays()] == [("DELL", False), ("Some screen", True)]
    assert api.display_config_calls == 4  # then cached again


def test_no_active_display_paths_is_asked_again_too(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    paths, api.paths = api.paths, []
    assert [d.name for d in wdesk.displays()] == [r"\\.\DISPLAY1"]
    api.paths = paths
    assert [d.name for d in wdesk.displays()] == ["DELL"]


def test_a_failing_target_name_keeps_the_device_name(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    api.paths[0] = (0, 100, DISPLAYPORT, r"\\.\DISPLAY1", None)
    assert [d.name for d in wdesk.displays()] == [r"\\.\DISPLAY1"]


def test_cursor_raises_when_windows_wont_say(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    assert wdesk.cursor() == (100, 100)
    api.cursor_ok = False
    with pytest.raises(OSError):
        wdesk.cursor()


def test_move_cursor_injects_a_tagged_absolute_move_onto_the_exact_pixel(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    api.metrics.update({76: -1920, 77: -200, 78: 1920 + 2560, 79: 1440 + 200})
    for target in ((0, 0), (-1920, -200), (2559, 1239), (-1, 700), (1234, -17)):
        wdesk.move_cursor(*target)
        assert wdesk.cursor() == target
    flags, _dx, _dy, data, extra = api.sent[-1]
    assert flags == MOVE_ABS and data == 0 and extra == win.EXTRA_INFO_TAG == 0x4A525653
    wdesk.move_cursor(99999, -99999)  # off the virtual screen: its corner
    assert wdesk.cursor() == (2559, -200)


class StepClock:
    """``clock.now`` for the backend: every reading is ``step`` later, so a wait is counted in polls, not in
    milliseconds of a machine that may be busy."""

    def __init__(self, step: float = 0.0002, start: float = 1000.0) -> None:
        self.step, self.t = step, start

    def __call__(self) -> float:
        self.t += self.step
        return self.t

    def skip(self, seconds: float) -> None:
        self.t += seconds


def test_move_cursor_waits_for_a_move_windows_has_not_applied_yet(
    api: FakeWin32, wdesk: WindowsDesktop, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(win, "clock_now", StepClock())  # the wait lasts as many polls as the fake clock allows
    api.move_lag = 3  # Windows shows the move on the fourth read
    wdesk.move_cursor(500, 400)
    assert wdesk.cursor() == (500, 400)


def test_a_move_the_cursor_never_makes_is_not_waited_out(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    api.move_lag = -1  # it never gets there: another app confines the cursor, or the point clamps back
    started = time.perf_counter()
    wdesk.move_cursor(600, 400)
    assert time.perf_counter() - started < 0.5  # real time, a loose bound on MOVE_SETTLE_S
    assert wdesk.cursor() == (100, 100)


def test_a_cursor_stuck_where_a_wait_ran_out_costs_each_move_only_a_short_wait(
    api: FakeWin32, wdesk: WindowsDesktop, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 120 moves a second each polling out the whole MOVE_SETTLE_S would cost half a core for nothing.
    clock = StepClock()
    monkeypatch.setattr(win, "clock_now", clock)
    api.move_lag = -1
    wdesk.move_cursor(500, 400)
    full = api.cursor_reads
    assert full >= win.MOVE_SETTLE_S / clock.step - 2, "the first move waits for the cursor"
    for x in range(501, 621):  # the executor's next second of moves, the cursor still stuck
        before = api.cursor_reads
        wdesk.move_cursor(x, 400)
        assert api.cursor_reads - before <= win.STUCK_SETTLE_S / clock.step + 3
    assert api.cursor_reads - full <= 120 * (win.STUCK_SETTLE_S / clock.step + 3) < 120 * full / 2


def test_after_the_cursor_was_stuck_a_free_move_that_lags_is_still_read_where_it_went(
    api: FakeWin32, wdesk: WindowsDesktop, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The executor reads the cursor right after each move: a move Windows has not applied yet would read back as
    # the old spot, and on the next tick the cursor at the new one would look like the user's hand on the mouse.
    monkeypatch.setattr(win, "clock_now", StepClock())
    api.move_lag = -1  # confined for a moment (ClipCursor): the wait runs out
    wdesk.move_cursor(500, 400)
    wdesk.move_cursor(510, 400)
    api.move_lag = 1  # free again; Windows applies the move a moment later
    wdesk.move_cursor(520, 400)
    assert wdesk.cursor() == (520, 400)
    api.move_lag = 3  # once it has moved, a move gets the whole wait again
    wdesk.move_cursor(530, 400)
    assert wdesk.cursor() == (530, 400)


def test_a_cursor_that_got_away_from_where_it_was_stuck_gets_the_whole_wait(
    api: FakeWin32, wdesk: WindowsDesktop, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(win, "clock_now", StepClock())
    api.move_lag = -1
    wdesk.move_cursor(500, 400)
    api.move_lag = 0
    api.cursor = (300, 300)  # the user's mouse, or a move that landed after its wait
    api.move_lag = 3
    wdesk.move_cursor(600, 400)
    assert wdesk.cursor() == (600, 400)


def test_a_short_pin_does_not_make_the_executor_take_its_own_moves_for_the_mouse(
    api: FakeWin32, wdesk: WindowsDesktop, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(win, "clock_now", StepClock())
    let_go: list[int] = []
    t = [0.0]
    ex = Executor(wdesk, displays=wdesk.displays, clock=lambda: t[0], on_user_input=lambda: let_go.append(1))

    def frame(x: float) -> None:
        ex.submit([MoveCursor(x, 400)], now=t[0])
        for _ in range(4):  # a camera frame's worth of 120 Hz ticks
            ex.tick(t[0])
            t[0] += 1 / 120

    frame(500)
    frame(520)
    api.move_lag = -1  # confined for a frame
    frame(560)
    api.move_lag = 1  # free again, each move applied a moment after it is sent
    for x in (600, 640, 680, 720):
        frame(x)
    assert let_go == [], "our own move was taken for the user's mouse"
    assert api.cursor[0] > 640


def test_a_cursor_that_would_not_move_is_reported_once_per_spell(
    api: FakeWin32, wdesk: WindowsDesktop, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(win, "clock_now", StepClock())
    api.move_lag = -1
    with caplog.at_level("DEBUG", logger=win.__name__):
        for x in (500, 600, 700):
            wdesk.move_cursor(x, 400)
        notices = [r for r in caplog.records if "stayed at" in r.message]
        assert [r.levelname for r in notices] == ["INFO"]  # stuck three times, said once
        api.move_lag = 0  # the cursor is free again
        wdesk.move_cursor(700, 400)
        api.move_lag = -1
        wdesk.move_cursor(800, 400)
    assert [r.levelname for r in caplog.records if "stayed at" in r.message] == ["INFO", "INFO"]


def test_a_move_that_settles_keeps_waiting_for_the_next_ones(
    api: FakeWin32, wdesk: WindowsDesktop, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(win, "clock_now", StepClock())
    api.move_lag = 2
    for x in (300, 400, 500):
        wdesk.move_cursor(x, 400)
        assert wdesk.cursor() == (x, 400)


def test_move_cursor_raises_when_input_is_blocked_or_there_is_no_screen(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    api.send_limit = 0
    with pytest.raises(OSError):
        wdesk.move_cursor(10, 10)
    api.send_limit = None
    api.metrics[win.SM_CXVIRTUALSCREEN] = 0
    with pytest.raises(OSError):
        wdesk.move_cursor(10, 10)


def test_buttons_press_the_primary_and_release_what_they_pressed(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    wdesk.button("left", True)
    wdesk.button("left", False)
    api.metrics[win.SM_SWAPBUTTON] = 1
    wdesk.button("left", True)
    api.metrics[win.SM_SWAPBUTTON] = 0  # swapped back while held: the release must match the press
    wdesk.button("left", False)
    wdesk.button("right", True)
    assert [s[0] for s in api.sent] == [
        win.MOUSEEVENTF_LEFTDOWN,
        win.MOUSEEVENTF_LEFTUP,
        win.MOUSEEVENTF_RIGHTDOWN,
        win.MOUSEEVENTF_RIGHTUP,
        win.MOUSEEVENTF_RIGHTDOWN,
    ]
    assert all(s[4] == win.EXTRA_INFO_TAG for s in api.sent)
    wdesk.close()  # releases the right button still held
    wdesk.close()
    assert [s[0] for s in api.sent[5:]] == [win.MOUSEEVENTF_RIGHTUP]


def test_a_failed_press_is_not_remembered_as_held(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    api.send_limit = 0
    with pytest.raises(OSError):
        wdesk.button("left", True)
    api.send_limit = None
    wdesk.close()
    assert api.sent == []


def _ups(api: FakeWin32) -> list[int]:
    return [s[0] for s in api.sent if s[0] in (win.MOUSEEVENTF_LEFTUP, win.MOUSEEVENTF_RIGHTUP)]


def test_a_release_windows_missed_is_sent_once_the_desktop_is_back(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    wdesk.button("left", True)
    api.send_limit, api.input_desktop = 0, "Winlogon"  # a UAC prompt takes the input mid-pinch
    with pytest.raises(OSError):
        wdesk.button("left", False)
    assert not wdesk.input_desktop_ok()
    assert _ups(api) == []
    api.send_limit, api.input_desktop = None, "Default"  # answered: back on the normal desktop
    assert wdesk.input_desktop_ok()
    assert _ups(api) == [win.MOUSEEVENTF_LEFTUP]
    assert api.sent[-1][4] == win.EXTRA_INFO_TAG
    assert wdesk.input_desktop_ok()
    wdesk.close()
    assert _ups(api) == [win.MOUSEEVENTF_LEFTUP]  # exactly once


def test_a_missed_release_goes_out_before_the_next_input_as_it_was_pressed(
    api: FakeWin32, wdesk: WindowsDesktop
) -> None:
    api.metrics[win.SM_SWAPBUTTON] = 1
    wdesk.button("left", True)  # the primary button is the physical right one
    api.metrics[win.SM_SWAPBUTTON] = 0
    api.send_limit = 0
    with pytest.raises(OSError):
        wdesk.button("left", False)
    api.send_limit = None
    wdesk.move_cursor(400, 300)
    assert [s[0] for s in api.sent] == [win.MOUSEEVENTF_RIGHTDOWN, win.MOUSEEVENTF_RIGHTUP, MOVE_ABS]
    wdesk.scroll(120)
    wdesk.close()
    assert _ups(api) == [win.MOUSEEVENTF_RIGHTUP]


def test_a_new_press_after_a_missed_release_releases_first(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    wdesk.button("left", True)
    api.send_limit = 0
    with pytest.raises(OSError):
        wdesk.button("left", False)
    api.send_limit = None
    wdesk.button("left", True)
    assert [s[0] for s in api.sent] == [win.MOUSEEVENTF_LEFTDOWN, win.MOUSEEVENTF_LEFTUP, win.MOUSEEVENTF_LEFTDOWN]
    wdesk.close()  # the second press is still held
    assert _ups(api) == [win.MOUSEEVENTF_LEFTUP, win.MOUSEEVENTF_LEFTUP]


def test_close_sends_a_missed_release_once(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    wdesk.button("left", True)
    wdesk.button("right", True)
    api.send_limit = 0
    for button in ("left", "right"):
        with pytest.raises(OSError):
            wdesk.button(button, False)  # type: ignore[arg-type]
    api.send_limit = None
    wdesk.close()
    wdesk.close()
    assert sorted(_ups(api)) == [win.MOUSEEVENTF_LEFTUP, win.MOUSEEVENTF_RIGHTUP]


def test_the_executor_forgetting_a_failed_release_leaves_no_button_down(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    ex = Executor(wdesk, displays=wdesk.displays, clock=lambda: 0.0)
    ex.submit([Button("left", True, 100, 100)], now=0.0)
    ex.tick(0.0)
    assert ex.held == {"left"}
    api.send_limit, api.cursor_ok, api.input_desktop = 0, False, "Winlogon"  # locked while pinching
    ex.submit([ReleaseAll()], now=0.0)
    ex.tick(0.0)
    assert ex.held == frozenset()  # the executor let go; Windows never heard the release
    api.send_limit, api.cursor_ok, api.input_desktop = None, True, "Default"
    for _ in range(3):
        ex.tick(0.0)
    assert wdesk.input_desktop_ok()  # the runtime asks twice a second
    assert _ups(api) == [win.MOUSEEVENTF_LEFTUP]


def test_scroll_sends_both_wheels_in_one_call(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    wdesk.scroll(-120, 30)
    wdesk.scroll(0, 0)
    wdesk.scroll(45)
    assert [(s[0], s[3]) for s in api.sent] == [
        (win.MOUSEEVENTF_WHEEL, 0xFFFFFF88),
        (win.MOUSEEVENTF_HWHEEL, 30),
        (win.MOUSEEVENTF_WHEEL, 45),
    ]


def test_double_click_metrics(wdesk: WindowsDesktop) -> None:
    assert wdesk.double_click() == (0.5, 4, 4)


def test_window_at_returns_the_top_level_window_under_the_point(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    api.add(FakeWin(0x500))
    api.add(FakeWin(0x501, cls="Edit", title=""))  # a child control
    api.roots[0x501] = 0x500
    api.hits[(300, 300)] = 0x501
    assert wdesk.window_at(300, 300) == Window(0x500, "notes.txt - Notepad", "Notepad")
    assert wdesk.window_at(5, 5) is None


@pytest.mark.parametrize(
    "case",
    ["ours", "shell window", "taskbar", "start menu", "cloaked", "hidden", "gone"],
)
def test_window_at_never_offers_the_overlay_the_shell_or_hidden_windows(
    api: FakeWin32, wdesk: WindowsDesktop, case: str
) -> None:
    window = api.add(FakeWin(0x600))
    api.hits[(10, 10)] = 0x600
    if case == "ours":
        window.pid = OUR_PID
    elif case == "shell window":
        api.shell = 0x600
    elif case == "taskbar":
        window.cls = "Shell_TrayWnd"
    elif case == "start menu":
        window.cls = "Windows.UI.Core.CoreWindow"
    elif case == "cloaked":
        window.cloaked = True
    elif case == "hidden":
        window.visible = False
    elif case == "gone":
        del api.windows[0x600]
    assert wdesk.window_at(10, 10) is None


def test_window_rect_is_the_visible_frame(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    window = api.add(FakeWin(0x700)).hwnd
    assert wdesk.window_rect(Window(window)) == Rect.from_ltrb(100, 100, 900, 700)
    api.DwmGetWindowAttribute = lambda *args: -2147467259  # type: ignore[method-assign]  # E_FAIL: no DWM
    assert wdesk.window_rect(Window(window)) == Rect.from_ltrb(93, 100, 907, 707)
    with pytest.raises(OSError):
        wdesk.window_rect(Window(0x999))


def test_set_window_rect_puts_the_visible_frame_on_the_rect(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    window = api.add(FakeWin(0x710))
    wdesk.set_window_rect(Window(0x710), Rect(-1500, 40, 640, 480))
    assert window.frame == (-1500, 40, -860, 520)
    hwnd, after, x, y, cx, cy, flags = api.set_window_pos[-1]
    assert (hwnd, after, x, y, cx, cy) == (0x710, None, -1507, 40, 654, 487)
    # A resize is synchronous (the app paces it) and never activates or reorders.
    assert flags == win.SWP_NOZORDER | win.SWP_NOACTIVATE | win.SWP_NOOWNERZORDER


def test_a_pure_move_is_asynchronous_and_keeps_the_size(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    window = api.add(FakeWin(0x720))
    wdesk.set_window_rect(Window(0x720), Rect(300, 200, 800, 600))
    assert window.frame == (300, 200, 1100, 800)
    flags = api.set_window_pos[-1][-1]
    assert flags & win.SWP_ASYNCWINDOWPOS and flags & win.SWP_NOSIZE and flags & win.SWP_NOACTIVATE


def test_a_hung_app_is_resized_asynchronously(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    api.add(FakeWin(0x730, hung=True))
    wdesk.set_window_rect(Window(0x730), Rect(0, 0, 500, 400))
    flags = api.set_window_pos[-1][-1]
    assert flags & win.SWP_ASYNCWINDOWPOS and not flags & win.SWP_NOSIZE


def test_a_window_without_a_sizing_border_is_moved_but_never_stretched(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    window = api.add(FakeWin(0x735, style=DIALOG_STYLE, cls="#32770", title="Properties"))  # 800 x 600
    wdesk.set_window_rect(Window(0x735), Rect(0, 0, 500, 400))  # a two-hand resize (or a snap) asks for 500 x 400
    assert window.frame == (-150, -100, 650, 500)  # same size, centred where the resize would have put it
    flags = api.set_window_pos[-1][-1]
    assert flags & win.SWP_NOSIZE and flags & win.SWP_ASYNCWINDOWPOS and flags & win.SWP_NOACTIVATE
    wdesk.set_window_rect(Window(0x735), Rect(40, 30, 800, 600))  # a plain move still lands exactly
    assert window.frame == (40, 30, 840, 630)


def test_a_window_whose_style_windows_wont_tell_is_still_resized(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    window = api.add(FakeWin(0x736, style=None))
    wdesk.set_window_rect(Window(0x736), Rect(0, 0, 500, 400))
    assert window.frame == (0, 0, 500, 400)


def test_an_elevated_window_is_blocked_before_any_call(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    api.add(FakeWin(0x740, pid=777, title="Task Manager"))
    api.levels[777] = HIGH
    calls = api.open_process_calls
    for attempt in (
        lambda: wdesk.set_window_rect(Window(0x740, "Task Manager"), Rect(0, 0, 500, 400)),
        lambda: wdesk.raise_window(Window(0x740)),
        lambda: wdesk.maximize(Window(0x740)),
        lambda: wdesk.restore(Window(0x740)),
        lambda: wdesk.minimize(Window(0x740)),
    ):
        with pytest.raises(InputBlocked):
            attempt()
    assert api.set_window_pos == [] and api.shown == []
    assert api.open_process_calls == calls + 1  # the verdict is cached per window
    assert 50777 in api.closed_handles
    # Reading its state and rect is still allowed.
    assert wdesk.window_state(Window(0x740)) == "normal"
    assert wdesk.window_rect(Window(0x740)) == Rect.from_ltrb(100, 100, 900, 700)


@pytest.mark.parametrize(("level", "blocked"), [("denied", True), ("no-token", True), (None, False), (0x1000, False)])
def test_what_counts_as_elevated(api: FakeWin32, wdesk: WindowsDesktop, level: Any, blocked: bool) -> None:
    api.add(FakeWin(0x750, pid=778))
    api.levels[778] = level  # denied: OpenProcess refused; None: the process is gone; 0x1000: a low (sandboxed) app
    if blocked:
        with pytest.raises(InputBlocked):
            wdesk.set_window_rect(Window(0x750), Rect(0, 0, 500, 400))
    else:
        wdesk.set_window_rect(Window(0x750), Rect(0, 0, 500, 400))


def test_without_our_own_level_only_the_calls_errors_count(api: FakeWin32) -> None:
    api.levels["self"] = "no-token"
    desktop = WindowsDesktop(api=api)
    api.add(FakeWin(0x760, pid=779))
    api.levels[779] = HIGH
    desktop.set_window_rect(Window(0x760), Rect(0, 0, 500, 400))
    assert len(api.set_window_pos) == 1


def test_access_denied_from_windows_raises_input_blocked_and_is_remembered(
    api: FakeWin32, wdesk: WindowsDesktop
) -> None:
    api.add(FakeWin(0x770, deny=True))  # same integrity level, but Windows says no (UIPI after all)
    with pytest.raises(InputBlocked):
        wdesk.set_window_rect(Window(0x770), Rect(0, 0, 500, 400))
    with pytest.raises(InputBlocked):
        wdesk.raise_window(Window(0x770))
    assert len(api.set_window_pos) == 1


def test_a_window_that_is_gone_raises_oserror(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    gone = Window(0x780)
    for attempt in (
        lambda: wdesk.set_window_rect(gone, Rect(0, 0, 500, 400)),
        lambda: wdesk.window_state(gone),
        lambda: wdesk.raise_window(gone),
        lambda: wdesk.restore(gone),
        lambda: wdesk.maximize(gone),
        lambda: wdesk.minimize(gone),
    ):
        with pytest.raises(OSError):
            attempt()


def test_raise_window_raises_without_activating_then_asks_for_the_foreground(
    api: FakeWin32, wdesk: WindowsDesktop
) -> None:
    api.add(FakeWin(0x790))
    wdesk.raise_window(Window(0x790))  # SetForegroundWindow refuses in the fake: not an error
    assert api.set_window_pos == [(0x790, None, 0, 0, 0, 0, RAISE | win.SWP_ASYNCWINDOWPOS)]
    assert api.foreground == [0x790]


def test_raise_window_is_posted_so_a_hung_app_cannot_stall_a_grab(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    # The executor raises at every grab, holding its lock: a synchronous SetWindowPos would wait for the app.
    api.add(FakeWin(0x791, hung=True, title="(Not Responding)"))
    wdesk.raise_window(Window(0x791))
    flags = api.set_window_pos[-1][-1]
    assert flags & win.SWP_ASYNCWINDOWPOS and flags & RAISE == RAISE


def test_show_commands_and_states(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    window = api.add(FakeWin(0x7A0))
    handle = Window(0x7A0)
    api.show_result = 0  # ShowWindowAsync's 0 is not a failure
    wdesk.maximize(handle)
    assert wdesk.window_state(handle) == "maximized"
    wdesk.restore(handle)
    assert wdesk.window_state(handle) == "normal" and window.frame == (100, 100, 900, 700)
    wdesk.minimize(handle)
    assert wdesk.window_state(handle) == "minimized"
    assert api.shown == [(0x7A0, win.SW_MAXIMIZE), (0x7A0, win.SW_RESTORE), (0x7A0, win.SW_MINIMIZE)]


def test_restore_waits_until_the_window_has_left_the_maximized_state(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    window = api.add(FakeWin(0x7B0, zoomed=True, frame=(0, 0, 1920, 1040), normal_frame=(200, 150, 1000, 750)))
    window.restore_lag = 3
    wdesk.restore(Window(0x7B0))
    # The executor reads the rect right after restoring: it must already be the restored one.
    assert wdesk.window_rect(Window(0x7B0)) == Rect.from_ltrb(200, 150, 1000, 750)


def test_restore_gives_up_waiting_on_a_window_that_never_restores(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    window = api.add(FakeWin(0x7C0, zoomed=True, frame=(0, 0, 1920, 1040)))
    window.restore_lag = 10**9
    started = time.monotonic()
    wdesk.restore(Window(0x7C0))
    assert time.monotonic() - started < win.RESTORE_WAIT_S + 0.5
    assert wdesk.window_state(Window(0x7C0)) == "maximized"


def test_input_desktop_is_ok_only_on_the_default_desktop(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    assert wdesk.input_desktop_ok()
    api.input_desktop = "Winlogon"  # lock screen, UAC prompt, Ctrl+Alt+Del
    assert not wdesk.input_desktop_ok()
    assert api.desktops_closed == 2
    api.input_desktop = None  # OpenInputDesktop refused
    assert not wdesk.input_desktop_ok()


def test_keep_awake_and_close_turns_it_off(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    assert wdesk.keep_awake(True)
    assert api.execution_states == [win.ES_CONTINUOUS | win.ES_DISPLAY_REQUIRED | win.ES_SYSTEM_REQUIRED]
    assert api.execution_states[0] == 0x80000003
    wdesk.close()
    wdesk.close()
    assert api.execution_states[1:] == [win.ES_CONTINUOUS]


def test_close_never_raises(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    wdesk.button("left", True)
    wdesk.keep_awake(True)
    api.send_limit = 0

    def broken(flags: int) -> int:
        raise RuntimeError("boom")

    api.SetThreadExecutionState = broken  # type: ignore[method-assign]
    wdesk.close()


# --------------------------------------------------------------------------- the real thing (Windows only)

WS_EX_TOPMOST = 0x00000008
PM_REMOVE = 0x0001
ERROR_CLASS_ALREADY_EXISTS = 1410
TEST_CLASS = "JarvisHandsTestWindow"


class WNDCLASSEXW(ctypes.Structure):  # 80 on 64-bit
    _fields_ = [
        ("cbSize", wt.UINT),
        ("style", wt.UINT),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE),
        ("hIcon", wt.HICON),
        ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HBRUSH),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
        ("hIconSm", wt.HICON),
    ]


class OwnWindows:
    """Top-level windows of this test process, on this thread, with DefWindowProcW as their window procedure."""

    def __init__(self) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        fn, c_int = win._fn, ctypes.c_int
        self.instance = fn(kernel32, "GetModuleHandleW", wt.HMODULE, wt.LPCWSTR)(None)
        default_proc = fn(user32, "DefWindowProcW", ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
        register = fn(user32, "RegisterClassExW", wt.ATOM, ctypes.POINTER(WNDCLASSEXW))
        self._create = fn(
            user32,
            "CreateWindowExW",
            wt.HWND,
            wt.DWORD,
            wt.LPCWSTR,
            wt.LPCWSTR,
            wt.DWORD,
            c_int,
            c_int,
            c_int,
            c_int,
            wt.HWND,
            wt.HMENU,
            wt.HINSTANCE,
            wt.LPVOID,
        )
        self._destroy = fn(user32, "DestroyWindow", wt.BOOL, wt.HWND)
        self._peek = fn(user32, "PeekMessageW", wt.BOOL, ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT)
        self._translate = fn(user32, "TranslateMessage", wt.BOOL, ctypes.POINTER(wt.MSG))
        self._dispatch = fn(user32, "DispatchMessageW", ctypes.c_ssize_t, ctypes.POINTER(wt.MSG))
        cls = WNDCLASSEXW()
        cls.cbSize = ctypes.sizeof(WNDCLASSEXW)
        cls.lpfnWndProc = ctypes.cast(default_proc, ctypes.c_void_p).value
        cls.hInstance = self.instance
        cls.lpszClassName = TEST_CLASS
        if not register(ctypes.byref(cls)):
            err = ctypes.get_last_error()  # type: ignore[attr-defined]
            if err != ERROR_CLASS_ALREADY_EXISTS:
                raise OSError(None, "RegisterClassExW failed", None, err)
        self.handles: list[int] = []

    def create(
        self, rect: Rect, title: str = "jarvis-hands test window", style: int = WS_OVERLAPPEDWINDOW | WS_VISIBLE
    ) -> int:
        x, y, width, height = rect.rounded()
        hwnd = self._create(
            WS_EX_TOPMOST,
            TEST_CLASS,
            title,
            style,
            x,
            y,
            width,
            height,
            None,
            None,
            self.instance,
            None,
        )
        if not hwnd:
            raise OSError(None, "CreateWindowExW failed", None, ctypes.get_last_error())  # type: ignore[attr-defined]
        self.handles.append(hwnd)
        self.pump()
        return hwnd

    def destroy(self, hwnd: int) -> None:
        if hwnd in self.handles:
            self.handles.remove(hwnd)
            self._destroy(hwnd)
            self.pump()

    def pump(self) -> None:
        msg = wt.MSG()
        while self._peek(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
            self._translate(ctypes.byref(msg))
            self._dispatch(ctypes.byref(msg))

    def close(self) -> None:
        for hwnd in list(self.handles):
            self.destroy(hwnd)


def _eventually(windows: OwnWindows, check: Callable[[], bool], timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        windows.pump()
        if check():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)


@pytest.fixture
def real() -> Iterator[WindowsDesktop]:
    desktop = WindowsDesktop()
    yield desktop
    desktop.close()


@pytest.fixture
def own_windows() -> Iterator[OwnWindows]:
    windows = OwnWindows()
    yield windows
    windows.close()


def _primary_work(desktop: WindowsDesktop) -> Rect:
    displays = desktop.displays()
    if not displays:
        pytest.skip("Windows reports no monitors (no interactive desktop?)")
    return next(d for d in displays if d.primary).work


@windows_only
def test_structure_sizes_on_64_bit_windows() -> None:
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        pytest.skip("64-bit Python only")
    sizes = {
        win.MONITORINFOEXW: 104,
        win.DISPLAY_DEVICEW: 840,
        win.MOUSEINPUT: 32,
        win.KEYBDINPUT: 24,
        win.HARDWAREINPUT: 8,
        win.INPUT: 40,
        win.GUITHREADINFO: 72,
        win.LASTINPUTINFO: 8,
        wt.POINT: 8,
        wt.RECT: 16,
        win.DISPLAYCONFIG_PATH_INFO: 72,
        win.DISPLAYCONFIG_MODE_INFO: 64,
        win.DISPLAYCONFIG_SOURCE_DEVICE_NAME: 84,
        win.DISPLAYCONFIG_TARGET_DEVICE_NAME: 420,
        WNDCLASSEXW: 80,
    }
    assert {t.__name__: ctypes.sizeof(t) for t in sizes} == {t.__name__: n for t, n in sizes.items()}
    assert win.INPUT.u.offset == 8 and win.MOUSEINPUT.dwExtraInfo.offset == 24
    # The union holds its largest member (the mouse's), so INPUT is the size SendInput's cbSize check wants.
    assert ctypes.sizeof(win._INPUTUNION) == max(ctypes.sizeof(m) for _n, m in win._INPUTUNION._fields_) == 32
    assert win.KEYBDINPUT.dwExtraInfo.offset == 16 and win.GUITHREADINFO.rcCaret.offset == 56
    assert win.LASTINPUTINFO.dwTime.offset == 4


@windows_only
def test_the_process_becomes_per_monitor_dpi_aware() -> None:
    awareness = win.make_dpi_aware()
    assert awareness in ("per_monitor_v2", "per_monitor_v1")
    assert win.make_dpi_aware() == awareness


@windows_only
def test_construct_and_close_twice() -> None:
    for _ in range(2):
        desktop = WindowsDesktop()
        assert desktop.dpi_awareness.startswith("per_monitor")
        desktop.close()
        desktop.close()


@windows_only
def test_real_displays_have_exactly_one_primary(real: WindowsDesktop) -> None:
    displays = real.displays()
    if not displays:
        pytest.skip("Windows reports no monitors (no interactive desktop?)")
    assert [d.id for d in displays] == list(range(1, len(displays) + 1))
    (primary,) = [d for d in displays if d.primary]
    assert (primary.rect.x, primary.rect.y) == (0, 0)  # the primary monitor is the origin of the virtual screen
    for d in displays:
        assert d.name and d.rect.width > 0 and d.rect.height > 0
        assert d.rect.left <= d.work.left and d.work.right <= d.rect.right
    assert real.displays() == displays


@windows_only
def test_cursor_round_trip(real: WindowsDesktop) -> None:
    if not real.input_desktop_ok():
        pytest.skip("the input desktop is not ours (locked, or a service session)")
    try:
        start = real.cursor()
    except OSError as exc:
        pytest.skip(f"GetCursorPos failed: no interactive desktop? ({exc})")
    work = _primary_work(real)
    target = (round(work.center.x) + 37, round(work.center.y) + 23)
    try:
        try:
            real.move_cursor(*target)
        except OSError as exc:
            pytest.skip(f"SendInput failed: no interactive desktop? ({exc})")
        got = real.cursor()
        if got == start:
            pytest.skip("SendInput did not move the cursor (no interactive desktop?)")
        assert abs(got[0] - target[0]) <= 1 and abs(got[1] - target[1]) <= 1
    finally:
        try:
            real.move_cursor(*start)
        except OSError:
            pass


@windows_only
def test_window_at_skips_a_window_of_this_process(real: WindowsDesktop, own_windows: OwnWindows) -> None:
    work = _primary_work(real)
    hwnd = own_windows.create(Rect(work.x + 80, work.y + 80, 420, 300))
    x, y = real.window_rect(Window(hwnd)).center.rounded()
    api = win._win32()
    hit = api.WindowFromPoint(wt.POINT(x, y))
    if not hit or (api.GetAncestor(hit, win.GA_ROOT) or hit) != hwnd:
        pytest.skip("the test window is not under its own centre (no interactive desktop?)")
    assert real.window_at(x, y) is None


@windows_only
def test_real_window_rects_round_trip(real: WindowsDesktop, own_windows: OwnWindows) -> None:
    work = _primary_work(real)
    hwnd = own_windows.create(Rect(work.x + 50, work.y + 50, 300, 200))
    window = Window(hwnd)
    assert real.window_state(window) == "normal"
    target = Rect(work.x + 120, work.y + 90, 520, 360)
    real.set_window_rect(window, target)  # a resize
    assert _eventually(own_windows, lambda: real.window_rect(window) == target), real.window_rect(window)
    moved = target.moved_to(target.x + 40, target.y + 25)
    real.set_window_rect(window, moved)  # a pure move
    assert _eventually(own_windows, lambda: real.window_rect(window) == moved), real.window_rect(window)
    real.raise_window(window)


@windows_only
def test_a_real_window_without_a_sizing_border_keeps_its_size(real: WindowsDesktop, own_windows: OwnWindows) -> None:
    work = _primary_work(real)
    window = Window(
        own_windows.create(Rect(work.x + 60, work.y + 60, 400, 300), style=WS_CAPTION | WS_SYSMENU | WS_VISIBLE)
    )
    before = real.window_rect(window)
    real.set_window_rect(window, Rect(before.x + 30, before.y + 20, before.width + 200, before.height + 100))
    expected = Rect(before.x + 130, before.y + 70, before.width, before.height)
    assert _eventually(own_windows, lambda: real.window_rect(window) == expected), real.window_rect(window)


@windows_only
def test_real_show_commands(real: WindowsDesktop, own_windows: OwnWindows) -> None:
    work = _primary_work(real)
    window = Window(own_windows.create(Rect(work.x + 60, work.y + 60, 480, 320)))
    for command, state in (
        (real.maximize, "maximized"),
        (real.restore, "normal"),
        (real.minimize, "minimized"),
        (real.restore, "normal"),
    ):
        command(window)
        assert _eventually(own_windows, lambda state=state: real.window_state(window) == state), state


@windows_only
def test_a_destroyed_real_window_raises_oserror(real: WindowsDesktop, own_windows: OwnWindows) -> None:
    work = _primary_work(real)
    hwnd = own_windows.create(Rect(work.x + 70, work.y + 70, 400, 300))
    own_windows.destroy(hwnd)
    with pytest.raises(OSError):
        real.set_window_rect(Window(hwnd), Rect(0, 0, 400, 300))
    with pytest.raises(OSError):
        real.window_rect(Window(hwnd))


@windows_only
def test_real_double_click_and_session_checks(real: WindowsDesktop) -> None:
    seconds, width, height = real.double_click()
    assert 0.0 < seconds <= 5.0 and width >= 1 and height >= 1
    assert isinstance(real.input_desktop_ok(), bool)
    assert real.keep_awake(True)
    assert real.keep_awake(False)


# --- the keyboard side on the real thing: reads only. Nothing here types, and nothing starts a program.


@windows_only
def test_the_key_probes_answer_on_the_real_desktop(real: WindowsDesktop) -> None:
    target = real.key_target()  # a CI runner may have no foreground window: any answer, but never an exception
    assert isinstance(target.hwnd, int) and isinstance(target.pid, int) and isinstance(target.lang_id, int)
    assert target.hwnd == 0 or target.pid > 0 or target.blocked == "none"
    assert isinstance(real.foreground_window(), int)
    assert isinstance(real.modifiers_down(), bool)
    assert real.foreign_input() is False  # the first call after construction only baselines
    assert isinstance(real.foreign_input(), bool)
    real.release_keys()  # nothing to release: no exception, nothing sent
    assert real.injects_for_real is True


@windows_only
def test_the_real_key_target_of_a_window_of_this_process_is_own(real: WindowsDesktop, own_windows: OwnWindows) -> None:
    work = _primary_work(real)
    hwnd = own_windows.create(Rect(work.x + 90, work.y + 90, 360, 240))
    if not win._win32().SetForegroundWindow(hwnd) or real.foreground_window() != hwnd:
        pytest.skip("this process could not take the foreground (no interactive desktop?)")
    target = real.key_target()
    assert (target.hwnd, target.pid, target.blocked) == (hwnd, os.getpid(), "own")
