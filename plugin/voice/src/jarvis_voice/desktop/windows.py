"""The Windows desktop: the shell, user32, GDI, the clipboard and the audio endpoint (ctypes only, no pywin32).

How each action works:

- **open**: links and folders go to the shell's open verb (``os.startfile``,
  which is ShellExecuteW); the service has already checked them. Apps come
  from Get-StartApps (PowerShell, no window, cached ten minutes, loaded when
  the helper starts, reloaded on a miss) or, failing that, the Start Menu's
  shortcuts, and start as ``shell:AppsFolder\\<AppID>``, which works for Store
  and desktop apps alike. Only a Start-menu entry can be started this way.
- **focus**: EnumWindows for the visible, unowned, uncloaked top-level
  windows, matched by program then title; restored if minimized, then
  SetForegroundWindow, checked with GetForegroundWindow, and tried once more
  after an Alt tap. Windows may still refuse a background process; the answer
  says so rather than pretending.
- **media and the volume keys**: SendInput. The media keys are global, so no
  window needs the focus. Typing or clicking is never offered.
- **volume**: IAudioEndpointVolume through raw COM vtables (no comtypes or
  pycaw), with the volume keys as the fallback when COM fails.
- **screenshot**: GDI (BitBlt with CAPTUREBLT, GetDIBits) of the whole virtual
  screen in physical pixels, a PNG written with zlib, saved to the
  Screenshots known folder.
- **lock**: LockWorkStation. **clipboard**: the Win32 clipboard, Unicode text only.

Every module-level name imports on any OS. The DLLs are private
``ctypes.WinDLL`` instances (never the shared ``ctypes.windll``), loaded on
first use on Windows only, with a prototype for every function called: the
default ``int`` would truncate 64-bit handles. ``WindowsDesktop(api=...)``
takes a stand-in for them, so tests run these code paths on every OS.
"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Sequence
from ctypes import wintypes as wt
from pathlib import Path, PureWindowsPath
from typing import Any

import numpy as np

from .base import (
    MEDIA_TEXT,
    VOLUME_STEP,
    AppList,
    Failed,
    MediaKey,
    Refused,
    Shot,
    StartApp,
    TopWindow,
    Unsupported,
    VolumeChange,
    clamp_level,
    encode_png,
    parse_start_apps,
    pick_app,
    pick_window,
    plan_volume,
    screenshot_name,
    start_menu_links,
    volume_text,
    window_label,
    write_new_file,
)

log = logging.getLogger(__name__)

ULONG_PTR = ctypes.c_size_t
HRESULT = ctypes.c_long

S_OK, S_FALSE = 0x0, 0x1
RPC_E_CHANGED_MODE = 0x80010106
E_POINTER = 0x80004003
E_NOTFOUND = 0x80070490  # HRESULT_FROM_WIN32(ERROR_NOT_FOUND): no audio endpoint
COINIT_APARTMENTTHREADED = 0x2
COINIT_DISABLE_OLE1DDE = 0x4
CLSCTX_ALL = 0x17
E_RENDER, E_MULTIMEDIA = 0, 1

INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x1
KEYEVENTF_KEYUP = 0x2
VK_MENU = 0x12
VK_VOLUME_MUTE, VK_VOLUME_DOWN, VK_VOLUME_UP = 0xAD, 0xAE, 0xAF
MEDIA_VK: dict[MediaKey, int] = {"next": 0xB0, "previous": 0xB1, "stop": 0xB2, "play_pause": 0xB3}
#: Windows' volume keys move 2 points a press.
KEY_STEP = 2

SW_RESTORE = 9
GW_OWNER = 4
DWMWA_CLOAKED = 14
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
#: The desktop and the taskbar: never "focused".
SHELL_CLASSES = frozenset({"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"})
FOREGROUND_WAIT_S = 0.25

SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 76, 77, 78, 79
SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000  # include layered windows
BI_RGB = 0
DIB_RGB_COLORS = 0
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
KF_FLAG_CREATE = 0x00008000

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
CLIPBOARD_TRIES = 5
CLIPBOARD_RETRY_S = 0.05

POWERSHELL_TIMEOUT_S = 10.0
#: An open that matches nothing reloads the app list when it is older than this (an app installed since).
APP_REFRESH_AFTER_S = 5.0
GET_START_APPS = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-StartApps | ConvertTo-Json -Compress"


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def parse(cls, text: str) -> GUID:
        return cls.from_buffer_copy(uuid.UUID(text).bytes_le)

    def __str__(self) -> str:
        return "{" + str(uuid.UUID(bytes_le=bytes(self))).upper() + "}"


CLSID_MMDEVICE_ENUMERATOR = GUID.parse("BCDE0395-E52F-467C-8E3D-C4579291692E")
IID_IMMDEVICE_ENUMERATOR = GUID.parse("A95664D2-9614-4F35-A746-DE8DB63617E6")
IID_IAUDIO_ENDPOINT_VOLUME = GUID.parse("5CDF2C82-841E-4546-9722-0CF74078229A")
FOLDERID_SCREENSHOTS = GUID.parse("B7BEDE81-DF94-4682-A7D8-57A52620B86F")

# Vtable slots (IUnknown takes 0-2).
RELEASE = 2
GET_DEFAULT_AUDIO_ENDPOINT = 4  # IMMDeviceEnumerator
ACTIVATE = 3  # IMMDevice
SET_MASTER_VOLUME_LEVEL_SCALAR = 7  # IAudioEndpointVolume
GET_MASTER_VOLUME_LEVEL_SCALAR = 9
SET_MUTE = 14
GET_MUTE = 15


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wt.LONG),
        ("dy", wt.LONG),
        ("mouseData", wt.DWORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wt.WORD),
        ("wScan", wt.WORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]  # the mouse sets the size


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD),
        ("biWidth", wt.LONG),
        ("biHeight", wt.LONG),
        ("biPlanes", wt.WORD),
        ("biBitCount", wt.WORD),
        ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD),
        ("biXPelsPerMeter", wt.LONG),
        ("biYPelsPerMeter", wt.LONG),
        ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


# --------------------------------------------------------------------------- the DLLs


def _fn(dll: Any, name: str, restype: Any, *argtypes: Any) -> Any:
    """``dll.<name>`` with its prototype set (the default int would truncate 64-bit handles)."""
    function = getattr(dll, name)
    function.restype = restype
    function.argtypes = list(argtypes)
    return function


def _powershell() -> str:
    # The full path: a powershell.exe earlier on PATH or in the current folder is never run.
    root = os.environ.get("SYSTEMROOT") or r"C:\Windows"
    return os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")


class _Win32:
    """Private DLL instances with every prototype this module calls, plus the shell's open verb and PowerShell."""

    def __init__(self) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)  # type: ignore[attr-defined]
        ole32 = ctypes.WinDLL("ole32", use_last_error=True)  # type: ignore[attr-defined]
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)  # type: ignore[attr-defined]
        dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)  # type: ignore[attr-defined]
        c_int, c_void_p, guid_p = ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(GUID)

        self.startfile = os.startfile  # type: ignore[attr-defined]
        self.last_error = ctypes.get_last_error  # type: ignore[attr-defined]
        self.COMFUNCTYPE = ctypes.WINFUNCTYPE  # type: ignore[attr-defined]
        self.WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)  # type: ignore[attr-defined]

        # COM and the shell.
        self.CoInitializeEx = _fn(ole32, "CoInitializeEx", HRESULT, c_void_p, wt.DWORD)
        self.CoCreateInstance = _fn(
            ole32, "CoCreateInstance", HRESULT, guid_p, c_void_p, wt.DWORD, guid_p, ctypes.POINTER(c_void_p)
        )
        self.CoTaskMemFree = _fn(ole32, "CoTaskMemFree", None, c_void_p)
        self.SHGetKnownFolderPath = _fn(
            shell32, "SHGetKnownFolderPath", HRESULT, guid_p, wt.DWORD, wt.HANDLE, ctypes.POINTER(c_void_p)
        )
        # Keys, the session.
        self.SendInput = _fn(user32, "SendInput", wt.UINT, wt.UINT, ctypes.POINTER(INPUT), c_int)
        self.LockWorkStation = _fn(user32, "LockWorkStation", wt.BOOL)
        # Windows.
        self.EnumWindows = _fn(user32, "EnumWindows", wt.BOOL, self.WNDENUMPROC, wt.LPARAM)
        self.IsWindowVisible = _fn(user32, "IsWindowVisible", wt.BOOL, wt.HWND)
        self.GetWindow = _fn(user32, "GetWindow", wt.HWND, wt.HWND, wt.UINT)
        self.GetWindowTextLengthW = _fn(user32, "GetWindowTextLengthW", c_int, wt.HWND)
        self.GetWindowTextW = _fn(user32, "GetWindowTextW", c_int, wt.HWND, wt.LPWSTR, c_int)
        self.GetClassNameW = _fn(user32, "GetClassNameW", c_int, wt.HWND, wt.LPWSTR, c_int)
        self.GetWindowThreadProcessId = _fn(user32, "GetWindowThreadProcessId", wt.DWORD, wt.HWND, wt.LPDWORD)
        self.IsIconic = _fn(user32, "IsIconic", wt.BOOL, wt.HWND)
        self.ShowWindow = _fn(user32, "ShowWindow", wt.BOOL, wt.HWND, c_int)
        self.SetForegroundWindow = _fn(user32, "SetForegroundWindow", wt.BOOL, wt.HWND)
        self.GetForegroundWindow = _fn(user32, "GetForegroundWindow", wt.HWND)
        self.DwmGetWindowAttribute = _fn(
            dwmapi, "DwmGetWindowAttribute", HRESULT, wt.HWND, wt.DWORD, c_void_p, wt.DWORD
        )
        self.OpenProcess = _fn(kernel32, "OpenProcess", wt.HANDLE, wt.DWORD, wt.BOOL, wt.DWORD)
        self.QueryFullProcessImageNameW = _fn(
            kernel32, "QueryFullProcessImageNameW", wt.BOOL, wt.HANDLE, wt.DWORD, wt.LPWSTR, wt.PDWORD
        )
        self.CloseHandle = _fn(kernel32, "CloseHandle", wt.BOOL, wt.HANDLE)
        # The screen.
        try:  # Windows 10 1607+
            self.SetThreadDpiAwarenessContext: Any = _fn(user32, "SetThreadDpiAwarenessContext", wt.HANDLE, wt.HANDLE)
        except AttributeError:
            self.SetThreadDpiAwarenessContext = None
        self.GetSystemMetrics = _fn(user32, "GetSystemMetrics", c_int, c_int)
        self.GetDC = _fn(user32, "GetDC", wt.HDC, wt.HWND)
        self.ReleaseDC = _fn(user32, "ReleaseDC", c_int, wt.HWND, wt.HDC)
        self.CreateCompatibleDC = _fn(gdi32, "CreateCompatibleDC", wt.HDC, wt.HDC)
        self.CreateCompatibleBitmap = _fn(gdi32, "CreateCompatibleBitmap", wt.HBITMAP, wt.HDC, c_int, c_int)
        self.SelectObject = _fn(gdi32, "SelectObject", wt.HGDIOBJ, wt.HDC, wt.HGDIOBJ)
        self.BitBlt = _fn(gdi32, "BitBlt", wt.BOOL, wt.HDC, c_int, c_int, c_int, c_int, wt.HDC, c_int, c_int, wt.DWORD)
        self.GetDIBits = _fn(
            gdi32,
            "GetDIBits",
            c_int,
            wt.HDC,
            wt.HBITMAP,
            wt.UINT,
            wt.UINT,
            c_void_p,
            ctypes.POINTER(BITMAPINFO),
            wt.UINT,
        )
        self.DeleteObject = _fn(gdi32, "DeleteObject", wt.BOOL, wt.HGDIOBJ)
        self.DeleteDC = _fn(gdi32, "DeleteDC", wt.BOOL, wt.HDC)
        # The clipboard.
        self.OpenClipboard = _fn(user32, "OpenClipboard", wt.BOOL, wt.HWND)
        self.CloseClipboard = _fn(user32, "CloseClipboard", wt.BOOL)
        self.EmptyClipboard = _fn(user32, "EmptyClipboard", wt.BOOL)
        self.GetClipboardData = _fn(user32, "GetClipboardData", wt.HANDLE, wt.UINT)
        self.SetClipboardData = _fn(user32, "SetClipboardData", wt.HANDLE, wt.UINT, wt.HANDLE)
        self.GlobalAlloc = _fn(kernel32, "GlobalAlloc", wt.HGLOBAL, wt.UINT, ctypes.c_size_t)
        self.GlobalLock = _fn(kernel32, "GlobalLock", c_void_p, wt.HGLOBAL)
        self.GlobalUnlock = _fn(kernel32, "GlobalUnlock", wt.BOOL, wt.HGLOBAL)
        self.GlobalSize = _fn(kernel32, "GlobalSize", ctypes.c_size_t, wt.HGLOBAL)
        self.GlobalFree = _fn(kernel32, "GlobalFree", wt.HGLOBAL, wt.HGLOBAL)
        self.CreateWindowExW = _fn(
            user32,
            "CreateWindowExW",
            wt.HWND,
            wt.DWORD,  # extended style
            wt.LPCWSTR,  # class
            wt.LPCWSTR,  # title
            wt.DWORD,  # style
            c_int,
            c_int,
            c_int,
            c_int,
            wt.HWND,  # parent
            wt.HMENU,
            wt.HINSTANCE,
            wt.LPVOID,
        )
        self.DestroyWindow = _fn(user32, "DestroyWindow", wt.BOOL, wt.HWND)

    @staticmethod
    def start_apps_json(timeout: float) -> str:
        """Get-StartApps as JSON, from a PowerShell without a window."""
        done = subprocess.run(
            [_powershell(), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", GET_START_APPS],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # no console window flashing
            check=False,
        )
        if done.returncode != 0:
            detail = done.stderr.decode("utf-8", errors="replace").strip()[:200]
            raise OSError(f"Get-StartApps exited with code {done.returncode}: {detail}")
        return done.stdout.decode("utf-8", errors="replace")


def start_menu_folders() -> list[Path]:
    """The current user's and everyone's Start Menu\\Programs folders."""
    folders = []
    for variable in ("APPDATA", "PROGRAMDATA"):
        root = os.environ.get(variable)
        if root:
            folders.append(Path(root) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    return folders


# --------------------------------------------------------------------------- raw COM


class _ComError(OSError):
    def __init__(self, hresult: int, what: str) -> None:
        super().__init__(f"{what} failed (HRESULT 0x{hresult:08X})")
        self.hresult = hresult


class _ComObject:
    """An interface pointer whose methods are called by vtable slot; released once."""

    def __init__(self, api: Any, ptr: int) -> None:
        self._api = api
        self.ptr = ptr

    def _method(self, slot: int, restype: Any, argtypes: Sequence[Any]) -> Any:
        vtable = ctypes.cast(ctypes.c_void_p(self.ptr), ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        return self._api.COMFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtable[slot])

    def call(self, slot: int, argtypes: Sequence[Any], *args: Any) -> None:
        hr = self._method(slot, HRESULT, argtypes)(self.ptr, *args) & 0xFFFFFFFF
        if hr & 0x80000000:
            raise _ComError(hr, f"vtable slot {slot}")

    def out(self, slot: int, argtypes: Sequence[Any], *args: Any) -> _ComObject:
        """Calls a method whose last parameter receives an interface pointer, and wraps that pointer."""
        ptr = ctypes.c_void_p()
        self.call(slot, (*argtypes, ctypes.POINTER(ctypes.c_void_p)), *args, ctypes.byref(ptr))
        if not ptr.value:
            raise _ComError(E_POINTER, f"vtable slot {slot}")
        return _ComObject(self._api, ptr.value)

    def release(self) -> None:
        if self.ptr:
            release = self._method(RELEASE, ctypes.c_ulong, ())
            ptr, self.ptr = self.ptr, 0
            release(ptr)


class _EndpointVolume:
    """IAudioEndpointVolume: the default speakers' master volume (0.0 to 1.0) and mute."""

    def __init__(self, com: _ComObject) -> None:
        self._com = com

    def level(self) -> float:
        value = ctypes.c_float()
        self._com.call(GET_MASTER_VOLUME_LEVEL_SCALAR, (ctypes.POINTER(ctypes.c_float),), ctypes.byref(value))
        return float(value.value)

    def set_level(self, scalar: float) -> None:
        self._com.call(SET_MASTER_VOLUME_LEVEL_SCALAR, (ctypes.c_float, ctypes.c_void_p), scalar, None)

    def muted(self) -> bool:
        value = wt.BOOL()
        self._com.call(GET_MUTE, (ctypes.POINTER(wt.BOOL),), ctypes.byref(value))
        return bool(value.value)

    def set_muted(self, muted: bool) -> None:
        self._com.call(SET_MUTE, (wt.BOOL, ctypes.c_void_p), int(muted), None)

    def release(self) -> None:
        self._com.release()


# --------------------------------------------------------------------------- the desktop


class WindowsDesktop:
    """The DesktopBackend on Windows. Its actions run on the service's one STA worker thread."""

    def __init__(self, api: Any = None, *, clock: Callable[[], float] = time.monotonic) -> None:
        """``api`` stands in for the DLLs (tests, on any OS); by default they are loaded, on Windows only.
        ``clock`` ages the app list (tests)."""
        if api is None and sys.platform != "win32":
            raise Unsupported("The Windows desktop needs Windows.")
        self._w: Any = api if api is not None else _Win32()
        self._apps = AppList(self._load_apps, clock=clock)

    def prepare_thread(self) -> None:
        # The shell and UI calls want a single-threaded apartment (platform.windows.ensure_com is the
        # multithreaded one, for audio threads); OLE1 DDE off, as ShellExecute's documentation asks.
        hr = self._w.CoInitializeEx(None, COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE) & 0xFFFFFFFF
        if hr not in (S_OK, S_FALSE, RPC_E_CHANGED_MODE):
            log.warning("could not initialise COM on the desktop thread (HRESULT 0x%08X)", hr)

    def warm(self) -> None:
        self._apps.warm()

    # -- open ---------------------------------------------------------------------------

    def _load_apps(self) -> list[StartApp]:
        try:
            apps = parse_start_apps(self._w.start_apps_json(POWERSHELL_TIMEOUT_S))
        except Exception as exc:  # noqa: BLE001 - PowerShell missing or slow, Get-StartApps absent: use the shortcuts
            log.warning("Get-StartApps failed (%s); listing the Start Menu's shortcuts instead", exc)
            apps = []
        if not apps:
            apps = start_menu_links(start_menu_folders())
        log.info("desktop: %d Start-menu apps", len(apps))
        return apps

    def _start(self, target: str, what: str) -> None:
        try:
            self._w.startfile(target)
        except OSError as exc:
            raise Failed(f"Windows could not open {what}: {exc.strerror or exc}.") from exc

    def open_uri(self, uri: str) -> str:
        self._start(uri, uri)
        return f"Opened {uri}."

    def open_folder(self, path: Path) -> str:
        self._start(str(path), str(path))
        return f"Opened the folder {path}."

    def open_app(self, name: str, deadline: float) -> str:
        apps = self._apps.get(wait=deadline - time.monotonic())
        if apps is None:
            raise Failed("Windows has not listed the Start-menu apps yet; try again in a moment.")
        try:
            app = pick_app(name, apps)
        except Failed:
            if self._apps.age() < APP_REFRESH_AFTER_S:
                raise
            fresh = self._apps.get(wait=deadline - time.monotonic(), max_age=APP_REFRESH_AFTER_S)
            if fresh is None or fresh is apps:
                raise
            app = pick_app(name, fresh)
        self._start(app.launch_target, app.name)
        return f"Opened {app.name}."

    # -- focus --------------------------------------------------------------------------

    def top_windows(self) -> list[TopWindow]:
        """The windows a person would call by name, topmost first."""
        found: list[TopWindow] = []

        def visit(hwnd: int | None, _param: int) -> bool:
            try:
                window = self._describe(hwnd)
            except Exception:  # an exception must not cross the callback: skip that window
                log.debug("could not describe window %r", hwnd, exc_info=True)
                window = None
            if window is not None:
                found.append(window)
            return True

        self._w.EnumWindows(self._w.WNDENUMPROC(visit), 0)
        return found

    def _describe(self, hwnd: int | None) -> TopWindow | None:
        w = self._w
        if not hwnd or not w.IsWindowVisible(hwnd) or w.GetWindow(hwnd, GW_OWNER) or self._cloaked(hwnd):
            return None
        title = self._title(hwnd)
        if not title.strip() or self._class_name(hwnd) in SHELL_CLASSES:
            return None
        return TopWindow(int(hwnd), title, self._exe(hwnd))

    def _cloaked(self, hwnd: int) -> bool:
        # Suspended Store apps and windows on other virtual desktops are "visible" but cloaked.
        value = wt.DWORD()
        hr = self._w.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(value), ctypes.sizeof(value))
        return hr == S_OK and value.value != 0

    def _title(self, hwnd: int) -> str:
        length = self._w.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buffer = ctypes.create_unicode_buffer(length + 1)
        self._w.GetWindowTextW(hwnd, buffer, length + 1)
        return buffer.value

    def _class_name(self, hwnd: int) -> str:
        buffer = ctypes.create_unicode_buffer(256)
        self._w.GetClassNameW(hwnd, buffer, 256)
        return buffer.value

    def _exe(self, hwnd: int) -> str:
        pid = wt.DWORD()
        self._w.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        process = self._w.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not process:
            return ""  # an elevated or protected process
        try:
            buffer = ctypes.create_unicode_buffer(1024)
            size = wt.DWORD(1024)
            if not self._w.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
                return ""
            return PureWindowsPath(buffer.value).name
        finally:
            self._w.CloseHandle(process)

    def focus(self, name: str) -> str:
        window = pick_window(name, self.top_windows())
        if not self._bring_to_front(window.handle):
            raise Refused(
                f"Windows would not bring {window_label(window)} to the front; its taskbar button is flashing."
            )
        return f"Brought {window_label(window)} to the front."

    def _bring_to_front(self, hwnd: int) -> bool:
        if self._w.IsIconic(hwnd):
            self._w.ShowWindow(hwnd, SW_RESTORE)
        for attempt in range(2):
            if attempt:
                # Windows lets the process that sent the last input take the foreground.
                self._press(VK_MENU, extended=False, check=False)
            self._w.SetForegroundWindow(hwnd)
            if self._became_foreground(hwnd):
                return True
        return False

    def _became_foreground(self, hwnd: int) -> bool:
        # Another thread's window is activated asynchronously: give it a moment.
        deadline = time.monotonic() + FOREGROUND_WAIT_S
        while True:
            if self._w.GetForegroundWindow() == hwnd:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)

    # -- keys ---------------------------------------------------------------------------

    def _press(self, vk: int, *, times: int = 1, extended: bool = True, check: bool = True) -> None:
        """Presses and releases one virtual key ``times`` times, in one SendInput call."""
        flags = KEYEVENTF_EXTENDEDKEY if extended else 0
        inputs = (INPUT * (2 * times))()
        for i, item in enumerate(inputs):
            item.type = INPUT_KEYBOARD
            item.ki.wVk = vk
            item.ki.dwFlags = flags | (KEYEVENTF_KEYUP if i % 2 else 0)
        sent = self._w.SendInput(len(inputs), inputs, ctypes.sizeof(INPUT))
        if check and sent != len(inputs):
            raise Refused("Windows blocked the key press; a UAC prompt or the lock screen may have the keyboard.")

    def media(self, key: MediaKey) -> str:
        self._press(MEDIA_VK[key])
        return MEDIA_TEXT[key]

    # -- volume -------------------------------------------------------------------------

    def volume(self, level: int | None, change: VolumeChange | None) -> str:
        try:
            endpoint = self._endpoint_volume()
        except _ComError as exc:
            log.warning("the volume over COM failed (%s); using the volume keys", exc)
            return self._volume_keys(level, change)
        try:
            current, muted = clamp_level(endpoint.level() * 100), endpoint.muted()
            wanted, wanted_muted = plan_volume(current, muted, level, change)
            if wanted != current:
                endpoint.set_level(wanted / 100)
            if wanted_muted != muted:
                endpoint.set_muted(wanted_muted)
            return volume_text(clamp_level(endpoint.level() * 100), endpoint.muted(), change)
        except _ComError as exc:
            raise Failed(f"Windows would not change the volume ({exc}).") from exc
        finally:
            endpoint.release()

    def _endpoint_volume(self) -> _EndpointVolume:
        """The default speakers' IAudioEndpointVolume. Raises Unsupported when there are no speakers."""
        pointer = ctypes.c_void_p()
        hr = (
            self._w.CoCreateInstance(
                ctypes.byref(CLSID_MMDEVICE_ENUMERATOR),
                None,
                CLSCTX_ALL,
                ctypes.byref(IID_IMMDEVICE_ENUMERATOR),
                ctypes.byref(pointer),
            )
            & 0xFFFFFFFF
        )
        if hr & 0x80000000 or not pointer.value:
            raise _ComError(hr or E_POINTER, "CoCreateInstance(MMDeviceEnumerator)")
        enumerator = _ComObject(self._w, pointer.value)
        try:
            device = enumerator.out(GET_DEFAULT_AUDIO_ENDPOINT, (ctypes.c_int, ctypes.c_int), E_RENDER, E_MULTIMEDIA)
        except _ComError as exc:
            if exc.hresult == E_NOTFOUND:
                raise Unsupported("This PC has no audio output device.") from exc
            raise
        finally:
            enumerator.release()
        try:
            com = device.out(
                ACTIVATE,
                (ctypes.POINTER(GUID), wt.DWORD, ctypes.c_void_p),
                ctypes.byref(IID_IAUDIO_ENDPOINT_VOLUME),
                CLSCTX_ALL,
                None,
            )
        finally:
            device.release()
        return _EndpointVolume(com)

    def _volume_keys(self, level: int | None, change: VolumeChange | None) -> str:
        presses = VOLUME_STEP // KEY_STEP
        if change == "up":
            self._press(VK_VOLUME_UP, times=presses)
            return f"Turned the volume up about {VOLUME_STEP} points with the volume keys."
        if change == "down":
            self._press(VK_VOLUME_DOWN, times=presses)
            return f"Turned the volume down about {VOLUME_STEP} points with the volume keys."
        if change in ("mute", "unmute"):
            self._press(VK_VOLUME_MUTE)
            return "Pressed the mute key; it both mutes and unmutes, so check the speaker icon."
        if level is not None:
            self._press(VK_VOLUME_DOWN, times=100 // KEY_STEP)  # to zero, then up
            if level >= KEY_STEP:
                self._press(VK_VOLUME_UP, times=round(level / KEY_STEP))
            return f"Set the volume to about {level}% with the volume keys."
        raise Failed("Windows would not say what the volume is.")

    # -- screenshot ---------------------------------------------------------------------

    def screenshot(self) -> Shot:
        setter = self._w.SetThreadDpiAwarenessContext
        # Physical pixels on every monitor, for this thread only.
        previous = setter(ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)) if setter else None
        try:
            metrics = (SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN)
            left, top, width, height = (self._w.GetSystemMetrics(index) for index in metrics)
            if width <= 0 or height <= 0:
                raise Unsupported("Windows reports no screen to capture.")
            bgra = self._capture(left, top, width, height)
        finally:
            if previous:
                setter(previous)
        rgb = bgra.reshape(height, width, 4)[:, :, 2::-1]  # BGRA -> RGB
        folder = self._screenshots_folder()
        return Shot(write_new_file(folder, screenshot_name(time.localtime()), encode_png(rgb)), width, height)

    def _capture(self, left: int, top: int, width: int, height: int) -> Any:
        w = self._w
        screen = w.GetDC(None)
        if not screen:
            raise Failed("Windows would not let Jarvis read the screen.")
        memory = bitmap = selected = None
        try:
            memory = w.CreateCompatibleDC(screen)
            bitmap = w.CreateCompatibleBitmap(screen, width, height)
            if not memory or not bitmap:
                raise Failed("Windows could not make room for the screenshot.")
            selected = w.SelectObject(memory, bitmap)
            if not w.BitBlt(memory, 0, 0, width, height, screen, left, top, SRCCOPY | CAPTUREBLT):
                raise Failed(
                    f"Windows would not copy the screen (error {w.last_error()}); "
                    "a UAC prompt or the lock screen may be showing."
                )
            w.SelectObject(memory, selected)  # GetDIBits wants the bitmap out of any DC
            selected = None
            info = BITMAPINFO()
            header = info.bmiHeader
            header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            header.biWidth, header.biHeight = width, -height  # negative: top-down rows
            header.biPlanes, header.biBitCount, header.biCompression = 1, 32, BI_RGB
            pixels = ctypes.create_string_buffer(width * height * 4)
            if w.GetDIBits(memory, bitmap, 0, height, pixels, ctypes.byref(info), DIB_RGB_COLORS) != height:
                raise Failed("Windows would not hand over the screen's pixels.")
            return np.frombuffer(pixels, dtype=np.uint8, count=width * height * 4)
        finally:
            if selected:
                w.SelectObject(memory, selected)
            if bitmap:
                w.DeleteObject(bitmap)
            if memory:
                w.DeleteDC(memory)
            w.ReleaseDC(None, screen)

    def _screenshots_folder(self) -> Path:
        pointer = ctypes.c_void_p()
        hr = (
            self._w.SHGetKnownFolderPath(
                ctypes.byref(FOLDERID_SCREENSHOTS), KF_FLAG_CREATE, None, ctypes.byref(pointer)
            )
            & 0xFFFFFFFF
        )
        try:
            if not hr & 0x80000000 and pointer.value:
                return Path(ctypes.wstring_at(pointer.value))
        finally:
            self._w.CoTaskMemFree(pointer)  # also after a failure, as its documentation asks
        log.debug("no Screenshots known folder (HRESULT 0x%08X); using Pictures\\Screenshots", hr)
        return Path.home() / "Pictures" / "Screenshots"

    # -- lock ---------------------------------------------------------------------------

    def lock(self) -> str:
        if not self._w.LockWorkStation():
            raise Failed(f"Windows would not lock the screen (error {self._w.last_error()}).")
        return "Locked the screen."

    # -- clipboard ----------------------------------------------------------------------

    def _open_clipboard(self, owner: int | None) -> None:
        for attempt in range(CLIPBOARD_TRIES):
            if attempt:
                time.sleep(CLIPBOARD_RETRY_S)
            if self._w.OpenClipboard(owner):
                return
        raise Failed("Another app is holding the clipboard; try again in a moment.")

    def clipboard_read(self) -> str | None:
        w = self._w
        self._open_clipboard(None)
        try:
            handle = w.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return None
            pointer = w.GlobalLock(handle)
            if not pointer:
                raise Failed(f"Windows would not hand over the clipboard (error {w.last_error()}).")
            try:
                units = w.GlobalSize(handle) // ctypes.sizeof(ctypes.c_wchar)
                text = ctypes.wstring_at(pointer, units) if units else ""
            finally:
                w.GlobalUnlock(handle)
        finally:
            w.CloseClipboard()
        return text.split("\0", 1)[0]

    def clipboard_write(self, text: str) -> str:
        w = self._w
        data = ctypes.create_unicode_buffer(text)
        size = ctypes.sizeof(data)
        # A hidden window owns the new contents: with no owner, Windows may refuse SetClipboardData.
        owner = w.CreateWindowExW(0, "STATIC", None, 0, 0, 0, 0, 0, None, None, None, None)
        try:
            self._open_clipboard(owner)
            try:
                if not w.EmptyClipboard():
                    raise Failed(f"Windows would not clear the clipboard (error {w.last_error()}).")
                memory = w.GlobalAlloc(GMEM_MOVEABLE, size)
                if not memory:
                    raise Failed("Windows had no memory for the clipboard text.")
                try:
                    pointer = w.GlobalLock(memory)
                    if not pointer:
                        raise Failed(f"Windows would not fill the clipboard (error {w.last_error()}).")
                    try:
                        ctypes.memmove(pointer, data, size)
                    finally:
                        w.GlobalUnlock(memory)
                    if not w.SetClipboardData(CF_UNICODETEXT, memory):
                        raise Failed(f"Windows would not fill the clipboard (error {w.last_error()}).")
                    memory = None  # the clipboard owns it now
                finally:
                    if memory:
                        w.GlobalFree(memory)
            finally:
                w.CloseClipboard()
        finally:
            if owner:
                w.DestroyWindow(owner)
        return f"Copied {len(text):,} characters to the clipboard."
