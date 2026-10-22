# ruff: noqa: N802 - the fake's methods carry the Win32 names they stand in for
"""The Windows desktop backend.

Two layers: ``FakeWin32``, a stand-in for the DLLs, so the Windows code paths
(window enumeration, SendInput, raw COM vtables, GDI, the clipboard) run on
any OS; and a few read-only checks of the real thing on Windows. Nothing here
presses a real key, locks the screen or touches the real clipboard.
"""

from __future__ import annotations

import ctypes
import json
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from ctypes import wintypes as wt
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from jarvis_voice.desktop import windows as win
from jarvis_voice.desktop.base import Failed, Refused, StartApp, Unsupported, encode_png
from jarvis_voice.desktop.service import DesktopService
from jarvis_voice.desktop.windows import GUID, WindowsDesktop

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="needs Windows")

E_FAIL = 0x80004005
E_NOTIMPL = 0x80004001
REGDB_E_CLASSNOTREG = 0x80040154
ERROR_ACCESS_DENIED = 5
PREVIOUS_DPI_CONTEXT = 0x12


def signed(hresult: int) -> int:
    """An HRESULT as a C long callback returns it."""
    return hresult - (1 << 32) if hresult & 0x80000000 else hresult


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# --------------------------------------------------------------------------- COM objects in memory


class FakeCom:
    """A COM object in memory: a real vtable of C callbacks, so the backend's calls by slot run for real."""

    def __init__(self, slots: int, methods: dict[int, tuple[Any, tuple[Any, ...], Callable[..., int]]]) -> None:
        self.released = 0
        self._keep: list[Any] = []
        self.vtable = (ctypes.c_void_p * slots)()
        default = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(lambda _this: signed(E_NOTIMPL))
        self._keep.append(default)
        every = {win.RELEASE: (ctypes.c_ulong, (), self._release), **methods}
        for slot in range(slots):
            callback = default
            if slot in every:
                restype, argtypes, function = every[slot]
                callback = ctypes.CFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(function)
                self._keep.append(callback)
            self.vtable[slot] = ctypes.cast(callback, ctypes.c_void_p).value
        self.object = ctypes.c_void_p(ctypes.addressof(self.vtable))  # an object starts with its vtable pointer
        self.ptr = ctypes.addressof(self.object)

    def _release(self, this: int) -> int:
        assert this == self.ptr
        self.released += 1
        return 0


class FakeAudio:
    """MMDeviceEnumerator -> the default speakers (IMMDevice) -> IAudioEndpointVolume."""

    def __init__(self) -> None:
        self.level, self.muted = 0.5, False
        self.endpoint_hr = 0  # E_NOTFOUND: no speakers
        self.fail_slot: int | None = None
        self.sets: list[tuple[str, Any]] = []
        c_long, c_void_p, out_p = ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
        self.volume = FakeCom(
            21,
            {
                win.SET_MASTER_VOLUME_LEVEL_SCALAR: (c_long, (ctypes.c_float, c_void_p), self._set_level),
                win.GET_MASTER_VOLUME_LEVEL_SCALAR: (c_long, (ctypes.POINTER(ctypes.c_float),), self._get_level),
                win.SET_MUTE: (c_long, (wt.BOOL, c_void_p), self._set_mute),
                win.GET_MUTE: (c_long, (ctypes.POINTER(wt.BOOL),), self._get_mute),
            },
        )
        self.device = FakeCom(
            7, {win.ACTIVATE: (c_long, (ctypes.POINTER(GUID), wt.DWORD, c_void_p, out_p), self._activate)}
        )
        self.enumerator = FakeCom(
            8, {win.GET_DEFAULT_AUDIO_ENDPOINT: (c_long, (ctypes.c_int, ctypes.c_int, out_p), self._default)}
        )

    def _fails(self, slot: int) -> bool:
        return self.fail_slot == slot

    def _default(self, this: int, flow: int, role: int, out: Any) -> int:
        assert this == self.enumerator.ptr and (flow, role) == (win.E_RENDER, win.E_MULTIMEDIA)
        if self.endpoint_hr:
            return signed(self.endpoint_hr)
        out[0] = self.device.ptr
        return 0

    def _activate(self, this: int, iid: Any, context: int, params: Any, out: Any) -> int:
        assert this == self.device.ptr and str(iid.contents) == str(win.IID_IAUDIO_ENDPOINT_VOLUME)
        assert context == win.CLSCTX_ALL and params is None
        out[0] = self.volume.ptr
        return 0

    def _set_level(self, this: int, level: float, context: Any) -> int:
        assert this == self.volume.ptr and context is None
        if self._fails(win.SET_MASTER_VOLUME_LEVEL_SCALAR):
            return signed(E_FAIL)
        self.sets.append(("level", round(level, 4)))
        self.level = level
        return 0

    def _get_level(self, this: int, out: Any) -> int:
        out[0] = self.level
        return 0

    def _set_mute(self, this: int, on: int, context: Any) -> int:
        self.sets.append(("mute", on))
        self.muted = bool(on)
        return 0

    def _get_mute(self, this: int, out: Any) -> int:
        if self._fails(win.GET_MUTE):
            return signed(E_FAIL)
        out[0] = int(self.muted)
        return 0

    def all_released(self) -> bool:
        return (self.enumerator.released, self.device.released, self.volume.released) == (1, 1, 1)


# --------------------------------------------------------------------------- the fake DLLs


@dataclass
class FakeWin:
    hwnd: int
    title: str
    exe: str = r"C:\Program Files\App\app.exe"
    pid: int = 100
    visible: bool = True
    owner: int = 0
    cloaked: bool = False
    iconic: bool = False
    cls: str = "AppWindow"
    protected: bool = False  # OpenProcess is denied (an elevated app)


class FakeWin32:
    """The functions WindowsDesktop calls, over a small in-memory Windows."""

    COMFUNCTYPE = staticmethod(ctypes.CFUNCTYPE)  # the C convention here; WINFUNCTYPE is the same on x64 Windows

    def __init__(self, tmp_path: Path) -> None:
        self.error = 0
        self.started: list[str] = []
        self.startfile_error: OSError | None = None
        self.start_apps: str | Exception = json.dumps(
            [
                {"Name": "Spotify", "AppID": "SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify"},
                {"Name": "Notepad", "AppID": "Microsoft.WindowsNotepad_8wekyb3d8bbwe!App"},
            ]
        )
        self.powershell_calls: list[float] = []
        self.coinit: list[tuple[int, str]] = []
        self.coinit_hr = 0
        self.co_created: list[tuple[str, str, int, str]] = []
        self.cocreate_hr = 0
        self.audio = FakeAudio()
        # Keys.
        self.keys: list[tuple[int, int]] = []
        self.send_limit: int | None = None
        # Windows.
        self.windows: list[FakeWin] = []
        self.foreground = 0x9999
        self.focus_policy = "allow"  # or "after_alt", "never"
        self.alt_tapped = False
        self.shown: list[tuple[int, int]] = []
        self.foreground_calls: list[int] = []
        self.open_processes: set[int] = set()
        # The screen.
        self.metrics = {76: -2, 77: -1, 78: 5, 79: 3}
        rng = np.random.default_rng(3)
        self.screen = rng.integers(0, 256, size=(3, 5, 4), dtype=np.uint8)  # BGRA, top-down
        self.dpi_calls: list[int] = []
        self.next_handle = 0x100
        self.dcs: set[int] = set()
        self.memory_dcs: set[int] = set()
        self.bitmaps: set[int] = set()
        self.selected: dict[int, int] = {}
        self.bitblt_ok = True
        self.blits: list[tuple[int, ...]] = []
        self.screenshots: Path | None = tmp_path / "Known Screenshots"
        self.freed: list[int | None] = []
        self._keep: list[Any] = []
        # The session.
        self.locks = 0
        self.lock_ok = True
        # The clipboard.
        self.clipboard: str | None = None
        self.clipboard_busy = 0
        self.clipboard_open = False
        self.clipboard_owner: int | None = None
        self.open_calls: list[int | None] = []
        self.set_clipboard_ok = True
        self.memory: dict[int, Any] = {}
        self.unlocked: list[int] = []
        self.windows_made: list[int] = []
        self.windows_destroyed: list[int] = []

    def _handle(self) -> int:
        self.next_handle += 1
        return self.next_handle

    def last_error(self) -> int:
        return self.error

    def startfile(self, target: str) -> None:
        if self.startfile_error is not None:
            raise self.startfile_error
        self.started.append(target)

    def start_apps_json(self, timeout: float) -> str:
        self.powershell_calls.append(timeout)
        if isinstance(self.start_apps, Exception):
            raise self.start_apps
        return self.start_apps

    # -- COM and the shell

    def CoInitializeEx(self, reserved: Any, flags: int) -> int:
        assert reserved is None
        self.coinit.append((flags, threading.current_thread().name))
        return signed(self.coinit_hr)

    def CoCreateInstance(self, clsid: Any, outer: Any, context: int, iid: Any, out: Any) -> int:
        self.co_created.append((str(clsid._obj), str(iid._obj), context, threading.current_thread().name))
        if self.cocreate_hr:
            return signed(self.cocreate_hr)
        out._obj.value = self.audio.enumerator.ptr
        return 0

    def SHGetKnownFolderPath(self, folder: Any, flags: int, token: Any, out: Any) -> int:
        assert str(folder._obj) == str(win.FOLDERID_SCREENSHOTS) and flags == win.KF_FLAG_CREATE and token is None
        if self.screenshots is None:
            return signed(E_FAIL)
        text = ctypes.create_unicode_buffer(str(self.screenshots))
        self._keep.append(text)
        out._obj.value = ctypes.addressof(text)
        return 0

    def CoTaskMemFree(self, pointer: Any) -> None:
        self.freed.append(pointer.value)

    # -- keys and the session

    def SendInput(self, count: int, inputs: Any, size: int) -> int:
        assert size == ctypes.sizeof(win.INPUT) and count == len(inputs)
        accepted = count if self.send_limit is None else min(count, self.send_limit)
        for item in list(inputs)[:accepted]:
            assert item.type == win.INPUT_KEYBOARD
            self.keys.append((item.ki.wVk, item.ki.dwFlags))
            if item.ki.wVk == win.VK_MENU and item.ki.dwFlags & win.KEYEVENTF_KEYUP:
                self.alt_tapped = True
        return accepted

    def LockWorkStation(self) -> int:
        self.locks += 1
        if not self.lock_ok:
            self.error = ERROR_ACCESS_DENIED
        return int(self.lock_ok)

    # -- windows

    def WNDENUMPROC(self, function: Callable[..., bool]) -> Callable[..., bool]:
        return function

    def _win(self, hwnd: int) -> FakeWin:
        return next(w for w in self.windows if w.hwnd == hwnd)

    def EnumWindows(self, callback: Callable[[int, int], bool], param: int) -> int:
        for window in list(self.windows):
            if not callback(window.hwnd, param):
                break
        return 1

    def IsWindowVisible(self, hwnd: int) -> int:
        return int(self._win(hwnd).visible)

    def GetWindow(self, hwnd: int, command: int) -> int | None:
        assert command == win.GW_OWNER
        return self._win(hwnd).owner or None

    def GetWindowTextLengthW(self, hwnd: int) -> int:
        return len(self._win(hwnd).title)

    def GetWindowTextW(self, hwnd: int, buffer: Any, size: int) -> int:
        buffer.value = self._win(hwnd).title[: size - 1]
        return len(buffer.value)

    def GetClassNameW(self, hwnd: int, buffer: Any, size: int) -> int:
        buffer.value = self._win(hwnd).cls
        return len(buffer.value)

    def GetWindowThreadProcessId(self, hwnd: int, pid: Any) -> int:
        pid._obj.value = self._win(hwnd).pid
        return 1

    def DwmGetWindowAttribute(self, hwnd: int, attribute: int, value: Any, size: int) -> int:
        assert attribute == win.DWMWA_CLOAKED and size == ctypes.sizeof(wt.DWORD)
        value._obj.value = int(self._win(hwnd).cloaked)
        return 0

    def OpenProcess(self, access: int, inherit: bool, pid: int) -> int | None:
        assert access == win.PROCESS_QUERY_LIMITED_INFORMATION and not inherit
        if any(w.pid == pid and w.protected for w in self.windows):
            self.error = ERROR_ACCESS_DENIED
            return None
        handle = 0x5000 + pid
        self.open_processes.add(handle)
        return handle

    def QueryFullProcessImageNameW(self, process: int, flags: int, buffer: Any, size: Any) -> int:
        exe = next(w.exe for w in self.windows if 0x5000 + w.pid == process)
        buffer.value = exe
        size._obj.value = len(exe)
        return 1

    def CloseHandle(self, handle: int) -> int:
        self.open_processes.discard(handle)
        return 1

    def IsIconic(self, hwnd: int) -> int:
        return int(self._win(hwnd).iconic)

    def ShowWindow(self, hwnd: int, command: int) -> int:
        self.shown.append((hwnd, command))
        if command == win.SW_RESTORE:
            self._win(hwnd).iconic = False
        return 1

    def SetForegroundWindow(self, hwnd: int) -> int:
        self.foreground_calls.append(hwnd)
        if self.focus_policy == "allow" or (self.focus_policy == "after_alt" and self.alt_tapped):
            self.foreground = hwnd
            return 1
        return 0  # Windows flashes the taskbar button instead

    def GetForegroundWindow(self) -> int:
        return self.foreground

    # -- the screen

    def SetThreadDpiAwarenessContext(self, context: Any) -> int:
        self.dpi_calls.append(context.value if isinstance(context, ctypes.c_void_p) else context)
        return PREVIOUS_DPI_CONTEXT

    def GetSystemMetrics(self, index: int) -> int:
        return self.metrics.get(index, 0)

    def GetDC(self, hwnd: Any) -> int:
        assert hwnd is None  # the whole screen
        handle = self._handle()
        self.dcs.add(handle)
        return handle

    def ReleaseDC(self, hwnd: Any, dc: int) -> int:
        self.dcs.remove(dc)
        return 1

    def CreateCompatibleDC(self, dc: int) -> int:
        assert dc in self.dcs
        handle = self._handle()
        self.memory_dcs.add(handle)
        return handle

    def CreateCompatibleBitmap(self, dc: int, width: int, height: int) -> int:
        assert (width, height) == (self.metrics[78], self.metrics[79])
        handle = self._handle()
        self.bitmaps.add(handle)
        return handle

    def SelectObject(self, dc: int, obj: int) -> int:
        previous = self.selected.get(dc, 0x77)  # the DC's stock bitmap
        self.selected[dc] = obj
        return previous

    def BitBlt(self, dest: int, x: int, y: int, w: int, h: int, source: int, sx: int, sy: int, rop: int) -> int:
        self.blits.append((dest, x, y, w, h, source, sx, sy, rop))
        if not self.bitblt_ok:
            self.error = ERROR_ACCESS_DENIED
        return int(self.bitblt_ok)

    def GetDIBits(self, dc: int, bitmap: int, start: int, lines: int, buffer: Any, info: Any, usage: int) -> int:
        assert bitmap not in self.selected.values(), "GetDIBits on a bitmap still selected into a DC"
        header = info._obj.bmiHeader
        height, width = self.screen.shape[:2]
        assert header.biSize == ctypes.sizeof(win.BITMAPINFOHEADER)
        assert (header.biWidth, header.biHeight, header.biBitCount) == (width, -height, 32)
        assert (start, lines, usage) == (0, height, win.DIB_RGB_COLORS)
        data = self.screen.tobytes()
        assert ctypes.sizeof(buffer) == len(data)
        ctypes.memmove(buffer, data, len(data))
        return lines

    def DeleteObject(self, obj: int) -> int:
        self.bitmaps.remove(obj)
        return 1

    def DeleteDC(self, dc: int) -> int:
        self.memory_dcs.remove(dc)
        self.selected.pop(dc, None)
        return 1

    def gdi_clean(self) -> bool:
        return not (self.dcs or self.memory_dcs or self.bitmaps)

    # -- the clipboard

    def OpenClipboard(self, owner: int | None) -> int:
        self.open_calls.append(owner)
        if self.clipboard_busy > 0:
            self.clipboard_busy -= 1
            return 0
        assert not self.clipboard_open
        self.clipboard_open, self.clipboard_owner = True, owner
        return 1

    def CloseClipboard(self) -> int:
        assert self.clipboard_open
        self.clipboard_open = False
        return 1

    def EmptyClipboard(self) -> int:
        assert self.clipboard_open
        self.clipboard = None
        return 1

    def GetClipboardData(self, fmt: int) -> int | None:
        assert self.clipboard_open and fmt == win.CF_UNICODETEXT
        if self.clipboard is None:
            return None
        handle = self._handle()
        self.memory[handle] = ctypes.create_unicode_buffer(self.clipboard)
        return handle

    def SetClipboardData(self, fmt: int, memory: int) -> int | None:
        assert self.clipboard_open and fmt == win.CF_UNICODETEXT and self.clipboard_owner
        if not self.set_clipboard_ok:
            return None
        self.clipboard = ctypes.wstring_at(ctypes.addressof(self.memory[memory]))
        return memory

    def GlobalAlloc(self, flags: int, size: int) -> int:
        assert flags == win.GMEM_MOVEABLE
        handle = self._handle()
        self.memory[handle] = ctypes.create_string_buffer(size)
        return handle

    def GlobalLock(self, memory: int) -> int:
        return ctypes.addressof(self.memory[memory])

    def GlobalUnlock(self, memory: int) -> int:
        self.unlocked.append(memory)
        return 0  # the last unlock: not an error

    def GlobalSize(self, memory: int) -> int:
        return ctypes.sizeof(self.memory[memory])

    def GlobalFree(self, memory: int) -> None:
        del self.memory[memory]

    def CreateWindowExW(self, ex_style: int, cls: str, title: Any, style: int, *rest: Any) -> int:
        assert cls == "STATIC" and style == 0  # hidden
        handle = self._handle()
        self.windows_made.append(handle)
        return handle

    def DestroyWindow(self, hwnd: int) -> int:
        self.windows_destroyed.append(hwnd)
        return 1


@pytest.fixture
def api(tmp_path: Path) -> FakeWin32:
    return FakeWin32(tmp_path)


@pytest.fixture
def desk(api: FakeWin32) -> WindowsDesktop:
    return WindowsDesktop(api=api)


@pytest.fixture
def service(desk: WindowsDesktop) -> Iterator[DesktopService]:
    svc = DesktopService(desk, action_timeout=5.0)
    yield svc
    svc.close()


def answer(svc: DesktopService, body: dict[str, Any]) -> tuple[str, str]:
    reply = svc.handle(body)["desktop"]
    return reply["result"], reply["text"]


# --------------------------------------------------------------------------- basics


def test_guids_round_trip() -> None:
    assert ctypes.sizeof(GUID) == 16
    assert str(win.CLSID_MMDEVICE_ENUMERATOR) == "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
    assert str(GUID.parse("{5cdf2c82-841e-4546-9722-0cf74078229a}")) == "{5CDF2C82-841E-4546-9722-0CF74078229A}"
    assert bytes(win.IID_IAUDIO_ENDPOINT_VOLUME) == uuid.UUID("5CDF2C82-841E-4546-9722-0CF74078229A").bytes_le


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has the DLLs")
def test_needs_windows_without_a_stand_in() -> None:
    with pytest.raises(Unsupported):
        WindowsDesktop()


def test_the_worker_thread_is_a_single_threaded_apartment(api: FakeWin32, service: DesktopService) -> None:
    assert answer(service, {"action": "lock"})[0] == "done"
    flags = win.COINIT_APARTMENTTHREADED | win.COINIT_DISABLE_OLE1DDE
    assert api.coinit == [(flags, "jarvis-desktop")]


def test_a_com_failure_on_the_thread_is_logged(
    api: FakeWin32, desk: WindowsDesktop, caplog: pytest.LogCaptureFixture
) -> None:
    api.coinit_hr = E_FAIL
    with caplog.at_level("WARNING", logger="jarvis_voice.desktop.windows"):
        desk.prepare_thread()
    assert "HRESULT 0x80004005" in caplog.text
    api.coinit_hr = win.RPC_E_CHANGED_MODE  # COM was already up on that thread: fine
    caplog.clear()
    desk.prepare_thread()
    assert caplog.text == ""


# --------------------------------------------------------------------------- open


def test_links_and_folders_go_to_the_shell(api: FakeWin32, service: DesktopService, tmp_path: Path) -> None:
    assert answer(service, {"action": "open", "target": "spotify:playlist:abc"}) == (
        "done",
        "Opened spotify:playlist:abc.",
    )
    assert answer(service, {"action": "open", "target": str(tmp_path)}) == ("done", f"Opened the folder {tmp_path}.")
    assert api.started == ["spotify:playlist:abc", str(tmp_path)]
    api.startfile_error = OSError(2, "The system cannot find the file specified")
    assert answer(service, {"action": "open", "target": "ms-settings:sound"}) == (
        "failed",
        "Windows could not open ms-settings:sound: The system cannot find the file specified.",
    )


def test_refused_targets_never_reach_the_shell(api: FakeWin32, service: DesktopService, tmp_path: Path) -> None:
    (tmp_path / "evil.bat").write_text("echo")
    for target in ("file:///C:/Windows/System32/calc.exe", str(tmp_path / "evil.bat"), "\\\\evil\\share"):
        assert answer(service, {"action": "open", "target": target})[0] == "refused"
    assert api.started == []


def test_apps_start_from_the_apps_folder(api: FakeWin32, service: DesktopService) -> None:
    assert answer(service, {"action": "open", "target": "spotify"}) == ("done", "Opened Spotify.")
    assert answer(service, {"action": "open", "target": "notepad"}) == ("done", "Opened Notepad.")
    assert api.started == [
        "shell:AppsFolder\\SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify",
        "shell:AppsFolder\\Microsoft.WindowsNotepad_8wekyb3d8bbwe!App",
    ]
    assert api.powershell_calls == [win.POWERSHELL_TIMEOUT_S]  # listed once, then cached


def test_warm_lists_the_apps_off_the_worker(api: FakeWin32) -> None:
    desk = WindowsDesktop(api=api)
    desk.warm()
    assert desk._apps.get(wait=5) is not None
    assert api.powershell_calls == [win.POWERSHELL_TIMEOUT_S]


def test_a_miss_reloads_a_list_older_than_five_seconds(api: FakeWin32) -> None:
    clock = Clock()
    desk = WindowsDesktop(api=api, clock=clock)
    service = DesktopService(desk, action_timeout=5.0)
    assert answer(service, {"action": "open", "target": "Discord"})[0] == "failed"
    assert len(api.powershell_calls) == 1  # just listed: no reload
    api.start_apps = json.dumps({"Name": "Discord", "AppID": "com.squirrel.Discord.Discord"})  # installed since
    clock.now += win.APP_REFRESH_AFTER_S + 1
    assert answer(service, {"action": "open", "target": "Discord"}) == ("done", "Opened Discord.")
    assert len(api.powershell_calls) == 2 and api.started[-1] == "shell:AppsFolder\\com.squirrel.Discord.Discord"
    service.close()


def test_without_powershell_the_start_menu_shortcuts_are_used(
    api: FakeWin32, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    programs = tmp_path / "AppData" / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    (programs / "Spotify").mkdir(parents=True)
    (programs / "Spotify" / "Spotify.lnk").write_bytes(b"")
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData"))
    monkeypatch.delenv("PROGRAMDATA", raising=False)
    api.start_apps = OSError("powershell.exe is missing")
    service = DesktopService(WindowsDesktop(api=api), action_timeout=5.0)  # warms with the failing PowerShell
    assert answer(service, {"action": "open", "target": "Spotify"}) == ("done", "Opened Spotify.")
    assert api.started == [str(programs / "Spotify" / "Spotify.lnk")]
    service.close()


def test_an_empty_get_start_apps_also_falls_back(
    api: FakeWin32, desk: WindowsDesktop, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.delenv("PROGRAMDATA", raising=False)
    api.start_apps = ""
    assert desk._load_apps() == []


def test_an_app_list_still_loading_says_so(api: FakeWin32, desk: WindowsDesktop) -> None:
    gate = threading.Event()
    original = api.start_apps_json

    def slow(timeout: float) -> str:
        gate.wait(5)
        return original(timeout)

    api.start_apps_json = slow  # type: ignore[method-assign]
    with pytest.raises(Failed, match="has not listed the Start-menu apps yet"):
        desk.open_app("Spotify", deadline=time.monotonic() + 0.05)
    gate.set()
    assert desk.open_app("Spotify", deadline=time.monotonic() + 5) == "Opened Spotify."


# --------------------------------------------------------------------------- focus


def test_only_windows_a_person_would_name_are_listed(api: FakeWin32, desk: WindowsDesktop) -> None:
    api.windows = [
        FakeWin(1, "Spotify Premium", r"C:\Users\me\AppData\Roaming\Spotify\Spotify.exe", pid=1),
        FakeWin(2, "Hidden", visible=False),
        FakeWin(3, "Owned dialog", owner=1),
        FakeWin(4, "Cloaked Store app", cloaked=True),
        FakeWin(5, ""),
        FakeWin(6, "Program Manager", cls="Progman"),
        FakeWin(7, "Task Manager", r"C:\Windows\System32\Taskmgr.exe", pid=7, protected=True),
    ]
    assert [(w.handle, w.title, w.exe) for w in desk.top_windows()] == [
        (1, "Spotify Premium", "Spotify.exe"),
        (7, "Task Manager", ""),  # elevated: no program name, still found by title
    ]
    assert api.open_processes == set()  # every process handle closed


def test_focus_restores_and_raises(api: FakeWin32, service: DesktopService) -> None:
    api.windows = [
        FakeWin(1, "README.md - Visual Studio Code", r"C:\VS Code\Code.exe", pid=1),
        FakeWin(2, "Spotify Premium", r"C:\Spotify\Spotify.exe", pid=2, iconic=True),
    ]
    assert answer(service, {"action": "focus", "target": "spotify"}) == (
        "done",
        'Brought "Spotify Premium" to the front.',
    )
    assert api.shown == [(2, win.SW_RESTORE)] and api.foreground == 2
    assert api.keys == []  # no Alt tap needed


def test_focus_taps_alt_once_when_windows_refuses(api: FakeWin32, service: DesktopService) -> None:
    api.windows = [FakeWin(2, "Spotify Premium", r"C:\Spotify\Spotify.exe", pid=2)]
    api.focus_policy = "after_alt"
    assert answer(service, {"action": "focus", "target": "Spotify"})[0] == "done"
    assert api.keys == [(win.VK_MENU, 0), (win.VK_MENU, win.KEYEVENTF_KEYUP)]
    assert api.foreground_calls == [2, 2]


def test_focus_says_when_windows_will_not(api: FakeWin32, service: DesktopService) -> None:
    api.windows = [FakeWin(2, "Spotify Premium", r"C:\Spotify\Spotify.exe", pid=2)]
    api.focus_policy = "never"
    assert answer(service, {"action": "focus", "target": "Spotify"}) == (
        "refused",
        'Windows would not bring "Spotify Premium" to the front; its taskbar button is flashing.',
    )
    assert answer(service, {"action": "focus", "target": "Discord"}) == ("failed", "Discord is not open; use open.")


# --------------------------------------------------------------------------- keys


@pytest.mark.parametrize(("key", "vk"), [("play_pause", 0xB3), ("next", 0xB0), ("previous", 0xB1), ("stop", 0xB2)])
def test_media_keys(api: FakeWin32, service: DesktopService, key: str, vk: int) -> None:
    assert answer(service, {"action": "media", "key": key})[0] == "done"
    extended = win.KEYEVENTF_EXTENDEDKEY
    assert api.keys == [(vk, extended), (vk, extended | win.KEYEVENTF_KEYUP)]


def test_blocked_keys_are_refused(api: FakeWin32, service: DesktopService) -> None:
    api.send_limit = 0  # a UAC prompt or the lock screen has the input
    result, text = answer(service, {"action": "media", "key": "play_pause"})
    assert result == "refused" and "blocked the key press" in text


# --------------------------------------------------------------------------- volume


def test_volume_over_com(api: FakeWin32, service: DesktopService) -> None:
    audio = api.audio
    assert answer(service, {"action": "volume", "level": 30}) == ("done", "Volume is 30%.")
    assert audio.sets == [("level", 0.3)] and audio.all_released()
    assert api.co_created == [
        (
            str(win.CLSID_MMDEVICE_ENUMERATOR),
            str(win.IID_IMMDEVICE_ENUMERATOR),
            win.CLSCTX_ALL,
            "jarvis-desktop",
        )
    ]
    audio.sets.clear()
    assert answer(service, {"action": "volume", "change": "mute"}) == ("done", "Muted; the volume stays at 30%.")
    assert answer(service, {"action": "volume"}) == ("done", "Volume is 30%, muted.")
    assert answer(service, {"action": "volume", "change": "up"}) == ("done", "Volume is 40%.")  # and unmuted
    assert answer(service, {"action": "volume", "change": "down"}) == ("done", "Volume is 30%.")
    assert audio.sets == [("mute", 1), ("level", 0.4), ("mute", 0), ("level", 0.3)]
    assert api.keys == []  # no key was pressed


def test_no_speakers_is_unsupported(api: FakeWin32, service: DesktopService) -> None:
    api.audio.endpoint_hr = win.E_NOTFOUND
    assert answer(service, {"action": "volume", "level": 30}) == ("unsupported", "This PC has no audio output device.")
    assert api.audio.enumerator.released == 1 and api.keys == []


def test_a_com_failure_part_way_is_a_failure_and_everything_is_released(
    api: FakeWin32, service: DesktopService
) -> None:
    api.audio.fail_slot = win.GET_MUTE
    result, text = answer(service, {"action": "volume", "level": 30})
    assert result == "failed" and "HRESULT 0x80004005" in text
    assert api.audio.all_released()


def test_without_com_the_volume_keys_are_used(api: FakeWin32, service: DesktopService) -> None:
    api.cocreate_hr = REGDB_E_CLASSNOTREG
    up, down, mute = win.VK_VOLUME_UP, win.VK_VOLUME_DOWN, win.VK_VOLUME_MUTE
    assert answer(service, {"action": "volume", "change": "up"}) == (
        "done",
        "Turned the volume up about 10 points with the volume keys.",
    )
    assert [vk for vk, flags in api.keys if not flags & win.KEYEVENTF_KEYUP] == [up] * 5
    api.keys.clear()
    assert answer(service, {"action": "volume", "change": "mute"})[1].startswith("Pressed the mute key")
    assert [vk for vk, flags in api.keys if not flags & win.KEYEVENTF_KEYUP] == [mute]
    api.keys.clear()
    assert answer(service, {"action": "volume", "level": 30}) == (
        "done",
        "Set the volume to about 30% with the volume keys.",
    )
    assert [vk for vk, flags in api.keys if not flags & win.KEYEVENTF_KEYUP] == [down] * 50 + [up] * 15
    api.keys.clear()
    assert answer(service, {"action": "volume"}) == ("failed", "Windows would not say what the volume is.")
    assert api.keys == []


# --------------------------------------------------------------------------- screenshot


def test_screenshot_of_the_whole_virtual_screen(api: FakeWin32, service: DesktopService) -> None:
    reply = service.handle({"action": "screenshot"})["desktop"]
    path = Path(reply["path"])
    assert reply["result"] == "done" and reply["text"].startswith(f"Saved a 5x3 screenshot to {path}.")
    assert path.parent == api.screenshots and path.name.startswith("Jarvis ")
    assert path.read_bytes() == encode_png(api.screen[:, :, 2::-1])  # BGRA -> RGB (the encoder's own test decodes)
    blit = api.blits[0]
    assert blit[1:5] == (0, 0, 5, 3) and blit[6:] == (-2, -1, win.SRCCOPY | win.CAPTUREBLT)
    per_monitor = ctypes.c_void_p(win.DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2).value
    assert api.dpi_calls == [per_monitor, PREVIOUS_DPI_CONTEXT]  # set for the capture, then put back
    assert api.gdi_clean() and api.freed and api.freed[-1] is not None


def test_screenshots_fall_back_to_pictures(
    api: FakeWin32, desk: WindowsDesktop, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    api.screenshots = None
    api.SetThreadDpiAwarenessContext = None  # type: ignore[assignment]  # before Windows 10 1607
    shot = desk.screenshot()
    assert shot.path.parent == tmp_path / "home" / "Pictures" / "Screenshots"
    assert api.freed == [None]  # freed after a failure too


def test_a_refused_capture_releases_everything(api: FakeWin32, service: DesktopService) -> None:
    api.bitblt_ok = False
    result, text = answer(service, {"action": "screenshot"})
    assert result == "failed" and "would not copy the screen (error 5)" in text
    assert api.gdi_clean() and api.dpi_calls[-1] == PREVIOUS_DPI_CONTEXT
    assert api.screenshots is not None and not api.screenshots.exists()  # nothing written


def test_no_screen_is_unsupported(api: FakeWin32, service: DesktopService) -> None:
    api.metrics[78] = 0
    assert answer(service, {"action": "screenshot"}) == ("unsupported", "Windows reports no screen to capture.")


# --------------------------------------------------------------------------- lock


def test_lock(api: FakeWin32, service: DesktopService) -> None:
    assert answer(service, {"action": "lock"}) == ("done", "Locked the screen.")
    api.lock_ok = False
    assert answer(service, {"action": "lock"}) == ("failed", "Windows would not lock the screen (error 5).")
    assert api.locks == 2


# --------------------------------------------------------------------------- clipboard


def test_clipboard_round_trip(api: FakeWin32, service: DesktopService) -> None:
    text = "Hello, sir. Caf\u00e9 \u2615 \U0001f600"
    assert answer(service, {"action": "clipboard_write", "text": text}) == (
        "done",
        f"Copied {len(text)} characters to the clipboard.",
    )
    assert api.clipboard == text
    owner = api.windows_made[0]
    assert api.open_calls == [owner] and api.windows_destroyed == [owner]  # a hidden owner, destroyed after
    assert len(api.memory) == 1  # the clipboard owns the text now; nothing was freed
    reply = service.handle({"action": "clipboard_read"})["desktop"]
    assert reply["clipboard"] == text and api.open_calls[-1] is None
    assert not api.clipboard_open


def test_an_empty_clipboard(api: FakeWin32, service: DesktopService) -> None:
    assert answer(service, {"action": "clipboard_read"}) == ("done", "The clipboard holds no text.")
    assert not api.clipboard_open


def test_a_busy_clipboard_is_retried(api: FakeWin32, desk: WindowsDesktop) -> None:
    api.clipboard = "x"
    api.clipboard_busy = win.CLIPBOARD_TRIES - 1
    assert desk.clipboard_read() == "x"
    api.clipboard_busy = win.CLIPBOARD_TRIES
    with pytest.raises(Failed, match="Another app is holding the clipboard"):
        desk.clipboard_read()
    assert len(api.open_calls) == 2 * win.CLIPBOARD_TRIES


def test_a_refused_write_frees_its_memory(api: FakeWin32, desk: WindowsDesktop) -> None:
    api.set_clipboard_ok = False
    with pytest.raises(Failed, match="would not fill the clipboard"):
        desk.clipboard_write("secret")
    assert api.memory == {} and not api.clipboard_open and api.windows_destroyed == api.windows_made


# --------------------------------------------------------------------------- the real thing (read-only)


@windows_only
def test_real_prototypes_and_structures() -> None:
    real = win._Win32()
    assert callable(real.SendInput) and callable(real.CoCreateInstance) and callable(real.GetDIBits)
    assert ctypes.sizeof(win.INPUT) == (40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)
    assert ctypes.sizeof(win.BITMAPINFOHEADER) == 40


@windows_only
def test_real_windows_and_apps_are_listed() -> None:
    desk = WindowsDesktop()
    windows = desk.top_windows()  # may be empty on a runner without an interactive desktop
    assert all(w.title.strip() for w in windows)
    apps = desk._load_apps()  # Get-StartApps, or the Start Menu's shortcuts
    assert all(isinstance(a, StartApp) and a.name for a in apps)


@windows_only
def test_real_volume_is_read_without_changing_it() -> None:
    outcome: list[object] = []

    def read() -> None:
        desk = WindowsDesktop()
        desk.prepare_thread()
        try:
            outcome.append(desk.volume(None, None))  # a read: never sets, never presses a key
        except (Unsupported, Failed, Refused) as exc:
            outcome.append(exc)

    thread = threading.Thread(target=read)  # its own apartment
    thread.start()
    thread.join(10)
    assert outcome and (isinstance(outcome[0], Exception) or str(outcome[0]).startswith("Volume is "))
