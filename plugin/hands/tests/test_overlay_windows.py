"""The Windows overlay: its logic against a fake Win32 on every OS, the real windows on Windows.

The fake is just enough of user32, gdi32, kernel32 and shcore for the overlay,
behaving like Windows where the overlay depends on it: a real message queue
(``GetMessageW`` blocks, ``PostQuitMessage`` ends it), the window procedure
called through the class's function pointer (so the real ctypes callback
path runs), ``DestroyWindow`` sending ``WM_DESTROY``, a DIB section whose
memory the overlay writes and ``UpdateLayeredWindow`` reads, and
``WindowFromPoint`` hit-testing as the layered-window docs describe.

Every call the overlay makes goes through the overlay's own ``_Api``
prototypes first (``Marshalled``): the count and conversion of each argument
are checked as ctypes would on Windows, and the result comes back as ctypes
would return it. The fake's handles are wider than 32 bits on 64-bit, so a
32-bit parameter or return type anywhere shows up here, not only on a Windows
desktop. Anything the overlay does wrong is collected in ``problems`` rather
than raised (it runs on the overlay's thread), and every test ends by
checking that no fake collected any.
"""

# The fake's methods carry the Win32 names the overlay calls.
# ruff: noqa: N802

from __future__ import annotations

import contextlib
import ctypes
import functools
import inspect
import itertools
import logging
import queue
import re
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

import numpy as np
import pytest

from jarvis_hands.geometry import Point
from jarvis_hands.overlay import NullOverlay, OverlayError, OverlayState, create_overlay
from jarvis_hands.overlay import windows as win
from jarvis_hands.overlay.render import MODES, render, render_helper, reticle_size

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="layered windows exist on Windows only")
SIXTY_FOUR_BIT = ctypes.sizeof(ctypes.c_void_p) == 8

WM_CREATE = 0x0001
WM_NCCREATE = 0x0081
E_INVALIDARG = -0x7FF8FFA9  # 0x80070057 as an HRESULT
#: Message parameters that only survive a window procedure declared with pointer-sized WPARAM and LPARAM.
WIDE_WPARAM = 2**63 + 5 if SIXTY_FOUR_BIT else 2**31 + 5
WIDE_LPARAM = -(2**62) - 7 if SIXTY_FOUR_BIT else -(2**30) - 7
_QUIT = object()

_callback_type = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
#: The window procedure as Win32 documents it: LRESULT CALLBACK (HWND, UINT, WPARAM, LPARAM).
DOCUMENTED_WNDPROC = _callback_type(
    ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_size_t, ctypes.c_ssize_t
)


# --------------------------------------------------------------------------- the overlay's prototypes, off Windows too


class _PrototypeCheckDll:
    """Stands in for a WinDLL off Windows: every function is a real ctypes pointer (libc's ``abs``), so
    assigning ``argtypes`` / ``restype`` validates them as ctypes does on Windows; nothing is called."""

    def __init__(self, name: str, use_last_error: bool = False) -> None:
        if not use_last_error:
            raise AssertionError(f"{name} loaded without use_last_error")
        self._libc = ctypes.CDLL(None)
        self.functions: dict[str, Any] = {}

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        fn = self.functions.get(name)
        if fn is None:
            fn = self.functions[name] = self._libc._FuncPtr(("abs", self._libc))
            fn.__name__ = name
        return fn


def declared_api(proto: Callable[..., Any] | None = None, **aliases: Any) -> win._Api:
    """A fresh ``_Api``: on the real DLLs on Windows, on prototype-checking stand-ins elsewhere.

    ``proto`` (in place of ``windows._proto``) and ``aliases`` (the module's ctypes aliases, such as
    ``_LPARAM``) break its prototypes on purpose, to show that the checks below notice.
    """
    with pytest.MonkeyPatch.context() as mp:
        if sys.platform != "win32":
            mp.setattr(ctypes, "WinDLL", _PrototypeCheckDll, raising=False)
            mp.setattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE, raising=False)
        if proto is not None:
            mp.setattr(win, "_proto", proto)
        for name, value in aliases.items():
            mp.setattr(win, name, value)
        return win._Api()


@functools.cache
def overlay_api() -> win._Api:
    """The prototypes the overlay declares, which every fake checks its calls against."""
    return declared_api()


_INTEGER_CODES = "bBhHiIlLqQ"


def _conversion_problem(argtype: Any, value: Any) -> str | None:
    """Why ``value`` would not reach C intact through ``argtype``, or None.

    ``from_param`` catches a wrong type, but ctypes truncates integers silently, so a value that does not
    fit is checked too (a pointer-sized handle may be negative: ``HWND_TOPMOST`` is ``(HWND)-1``).
    """
    if isinstance(argtype, type) and issubclass(argtype, ctypes.Structure | ctypes.Union):
        # Passed by value. Not through from_param: given a byref() it crashes CPython 3.12.
        return None if isinstance(value, argtype) else f"{value!r} is not a {argtype.__name__} (passed by value)"
    try:
        argtype.from_param(value)
    except Exception as exc:  # noqa: BLE001 - ctypes raises TypeError or ArgumentError
        return f"{value!r} does not convert to {argtype.__name__} ({exc})"
    code = getattr(argtype, "_type_", None)
    if not isinstance(value, int) or not isinstance(code, str) or code not in _INTEGER_CODES + "P":
        return None
    bits = 8 * ctypes.sizeof(argtype)
    if code == "P":
        low, high = -(1 << (bits - 1)), 1 << bits
    elif argtype(-1).value < 0:
        low, high = -(1 << (bits - 1)), 1 << (bits - 1)
    else:
        low, high = 0, 1 << bits
    if low <= value < high:
        return None
    return f"{value:#x} does not fit {argtype.__name__} (ctypes would truncate it)"


def _returned(restype: Any, value: Any) -> Any:
    """What ctypes hands back for a C function that returned ``value`` through ``restype``."""
    if restype is None:
        return None
    if value is None:
        value = 0
    if restype is ctypes.c_void_p:
        return ctypes.c_void_p(value).value  # None for NULL
    code = getattr(restype, "_type_", None)
    if isinstance(code, str) and code in _INTEGER_CODES:
        return restype(value).value  # truncated to the declared width
    return value


class Marshalled:
    """One of the fake's DLLs as the overlay sees it through its ``_Api`` prototypes.

    Each call is checked the way ctypes treats it on Windows: the function has a prototype on that DLL,
    with as many parameters as Win32 documents (the fake method's signature), the call passes that many
    arguments, and each one converts to its declared type without being truncated. The result comes back
    as ctypes returns it for the declared ``restype``: NULL handles as None, and a 32-bit ``restype``
    cutting a 64-bit handle short.
    """

    def __init__(self, fake: FakeWin32, dll_name: str, dll: Any) -> None:
        self._fake, self._dll_name, self._dll = fake, dll_name, dll

    def __getattr__(self, name: str) -> Callable[..., Any]:
        if name.startswith("_"):
            raise AttributeError(name)
        method = getattr(type(self._fake), name, None)
        if method is None:
            raise AttributeError(f"the fake Win32 has no {name}")
        fake, where = self._fake, f"{self._dll_name}.{name}"
        target = method.__get__(fake)
        documented = len(inspect.signature(target).parameters)
        proto = getattr(self._dll, name, None) if self._dll is not None else None

        def call(*args: Any) -> Any:
            fake.exercised.add(name)
            if proto is None:
                fake.problems.append(f"{where} does not exist")
                return target(*args)
            argtypes = proto.argtypes
            if argtypes is None:
                fake.problems.append(f"{where} is called without a prototype")
                return _returned(ctypes.c_int, target(*args))  # ctypes' default: a C int
            if len(argtypes) != documented:
                fake.problems.append(f"{where}: the prototype has {len(argtypes)} parameters, Win32 {documented}")
            if len(args) != len(argtypes):
                fake.problems.append(f"{where}: called with {len(args)} arguments for {len(argtypes)} parameters")
            for i, (argtype, value) in enumerate(zip(argtypes, args, strict=False)):  # a count mismatch is noted above
                problem = _conversion_problem(argtype, value)
                if problem is not None:
                    fake.problems.append(f"{where} argument {i + 1}: {problem}")
            if len(args) < len(argtypes):
                raise TypeError(f"this function takes at least {len(argtypes)} arguments ({len(args)} given)")
            return _returned(proto.restype, target(*args))

        setattr(self, name, call)
        return call


# --------------------------------------------------------------------------- the fake Win32


@dataclass
class FakeWindow:
    class_name: str
    ex_style: int
    style: int
    #: The thread that created it, which alone may destroy it.
    thread: int
    visible: bool = False
    rect: tuple[int, int, int, int] = (0, 0, 1, 1)
    image: np.ndarray | None = None
    updates: int = 0
    topmost_raises: int = 0


@dataclass
class Monitor:
    left: int
    top: int
    right: int
    bottom: int
    dpi: int = 96

    def distance(self, x: int, y: int) -> int:
        dx = max(self.left - x, 0, x - (self.right - 1))
        dy = max(self.top - y, 0, y - (self.bottom - 1))
        return dx * dx + dy * dy


@dataclass
class FakeWin32:
    """The fake's methods take the parameters Win32 documents for each function, in order."""

    monitors: list[Monitor] = field(default_factory=lambda: [Monitor(0, 0, 1920, 1080)])
    #: Function names that fail (return 0 / NULL).
    fail: set[str] = field(default_factory=set)
    problems: list[str] = field(default_factory=list)
    calls: list[tuple[Any, ...]] = field(default_factory=list)
    #: The ``_Api`` whose prototypes the overlay's calls go through (None: the overlay's own).
    declared: Any = None
    #: Every fake made, so each test can end by checking that none saw Win32 misused.
    instances: ClassVar[list[FakeWin32]] = []

    def __post_init__(self) -> None:
        FakeWin32.instances.append(self)
        #: The functions the overlay called (through their prototypes).
        self.exercised: set[str] = set()
        declared = self.declared if self.declared is not None else overlay_api()
        self.user32, self.gdi32, self.kernel32, self.shcore = (
            Marshalled(self, name, getattr(declared, name)) for name in ("user32", "gdi32", "kernel32", "shcore")
        )
        self.WNDPROC = declared.WNDPROC
        # The optional functions, where _Api finds them.
        self.SetThreadDpiAwarenessContext = self.user32.SetThreadDpiAwarenessContext
        self.GetWindowLongPtrW = self.user32.GetWindowLongPtrW
        self.GetDpiForMonitor = self.shcore.GetDpiForMonitor
        # Above 4 GiB on 64-bit, so a 32-bit parameter or return type anywhere truncates them and is caught.
        high = 1 << 36 if SIXTY_FOUR_BIT else 0
        self._handles = itertools.count(high + 0x10010, 0x10)
        self.module = high + 0x400000
        self.screen_dc = high + 0x7000
        self.stock_bitmap = high + 0x5000
        self.monitor_base = high + 0x9000
        #: What is under the overlay, and the window that has the focus.
        self.desktop, self.foreground = high + 0x10, high + 0x20
        self.queue: queue.Queue[Any] = queue.Queue()
        self.classes: dict[str, Callable[..., int]] = {}
        self.windows: dict[int, FakeWindow] = {}
        self.dcs: dict[int, int] = {}
        self.bitmaps: dict[int, tuple[Any, int]] = {}
        self.screen_dcs = 0
        self.ui_thread = -1
        self.thread_awareness: list[int] = []

    def _record(self, *call: Any) -> None:
        self.calls.append(call)

    def named(self, name: str) -> list[tuple[Any, ...]]:
        return [c for c in self.calls if c[0] == name]

    @staticmethod
    def _thread() -> int:
        return threading.get_native_id() & 0xFFFFFFFF

    def _expect_screen_dc(self, dc: Any, what: str) -> None:
        if dc != self.screen_dc:
            self.problems.append(f"{what} got {dc!r}, not the screen DC")

    # ----- kernel32

    def GetCurrentThreadId(self) -> int:
        return self._thread()

    def GetModuleHandleW(self, module_name: str | None) -> int:
        return self.module

    # ----- user32: classes and windows

    def SetThreadDpiAwarenessContext(self, context: int) -> int:
        self.thread_awareness.append(context)
        return 0x11

    def GetWindowLongPtrW(self, hwnd: int, index: int) -> int:
        return self.windows[hwnd].ex_style

    def RegisterClassExW(self, wc_ref: Any) -> int:
        wc = wc_ref._obj
        if wc.cbSize != ctypes.sizeof(win.WNDCLASSEXW):
            self.problems.append(f"WNDCLASSEXW.cbSize {wc.cbSize}")
        if wc.hInstance != self.module:
            self.problems.append(f"RegisterClassExW: hInstance {wc.hInstance!r} is not the module")
        if not wc.lpfnWndProc or wc.lpszClassName in self.classes:
            return 0
        self.classes[wc.lpszClassName] = DOCUMENTED_WNDPROC(wc.lpfnWndProc)
        self.ui_thread = self._thread()
        self._record("RegisterClassExW", wc.lpszClassName)
        return 0xC001

    def UnregisterClassW(self, class_name: str, instance: int) -> int:
        if any(w.class_name == class_name for w in self.windows.values()):
            self.problems.append("class unregistered while its windows exist")
        if instance != self.module:
            self.problems.append(f"UnregisterClassW: hInstance {instance!r} is not the module")
        self._record("UnregisterClassW", class_name)
        return int(self.classes.pop(class_name, None) is not None)

    def CreateWindowExW(
        self,
        ex_style: int,
        class_name: str,
        window_name: str,
        style: int,
        x: int,
        y: int,
        width: int,
        height: int,
        parent: Any,
        menu: Any,
        instance: int,
        param: Any,
    ) -> int | None:
        if "CreateWindowExW" in self.fail and len(self.windows) >= 1:
            return None
        if instance != self.module:
            self.problems.append(f"CreateWindowExW: hInstance {instance!r} is not the module")
        hwnd = next(self._handles)
        self.windows[hwnd] = FakeWindow(class_name, ex_style, style, thread=self._thread())
        proc = self.classes[class_name]
        # 64-bit WPARAM / LPARAM values must pass through the window procedure into DefWindowProcW unchanged.
        seen = len(self.calls)
        if proc(hwnd, WM_NCCREATE, WIDE_WPARAM, WIDE_LPARAM) != 1 or proc(hwnd, WM_CREATE, 0, 0) != 0:
            self.windows.pop(hwnd)
            return None
        forwarded = [c[3:] for c in self.calls[seen:] if c[:3] == ("DefWindowProcW", hwnd, WM_NCCREATE)]
        if forwarded != [(WIDE_WPARAM, WIDE_LPARAM)]:
            self.problems.append(f"WM_NCCREATE reached DefWindowProcW as {forwarded}")
        self._record("CreateWindowExW", hwnd, window_name)
        return hwnd

    def DefWindowProcW(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        self._record("DefWindowProcW", hwnd, msg, wparam, lparam)
        return 1 if msg == WM_NCCREATE else 0

    def DestroyWindow(self, hwnd: int) -> int:
        window = self.windows.get(hwnd)
        if window is None:
            return 0
        if self._thread() != window.thread:
            self.problems.append("DestroyWindow off the window's thread")
        self.classes[window.class_name](hwnd, win.WM_DESTROY, 0, 0)
        del self.windows[hwnd]
        self._record("DestroyWindow", hwnd)
        return 1

    def SetTimer(self, hwnd: int, timer_id: int, elapse_ms: int, timer_proc: Any) -> int:
        self._record("SetTimer", self._thread(), hwnd, timer_id, elapse_ms)
        return timer_id

    # ----- user32: messages

    def GetMessageW(self, msg_ref: Any, hwnd: Any, filter_min: int, filter_max: int) -> int:
        item = self.queue.get()
        if item is _QUIT:
            return 0
        msg = msg_ref._obj
        msg.hwnd, msg.message, msg.wParam, msg.lParam = item
        return 1

    def DispatchMessageW(self, msg_ref: Any) -> int:
        msg = msg_ref._obj
        window = self.windows.get(msg.hwnd) if msg.hwnd else None
        if window is None:
            return 0
        return int(self.classes[window.class_name](msg.hwnd, msg.message, msg.wParam, msg.lParam))

    def PostMessageW(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        if hwnd not in self.windows or "PostMessageW" in self.fail:
            return 0
        self._record("PostMessageW", hwnd, msg)
        self.queue.put((hwnd, msg, wparam, lparam))
        return 1

    def PostThreadMessageW(self, thread_id: int, msg: int, wparam: int, lparam: int) -> int:
        if thread_id != self.ui_thread:
            self.problems.append(f"PostThreadMessageW to thread {thread_id}, not the UI thread")
            return 0
        self._record("PostThreadMessageW", msg)
        self.queue.put((None, msg, wparam, lparam))
        return 1

    def PostQuitMessage(self, exit_code: int) -> None:
        self.queue.put(_QUIT)

    # ----- user32: drawing, placement and hit-testing

    def GetDC(self, hwnd: Any) -> int:
        self.screen_dcs += 1
        return self.screen_dc

    def ReleaseDC(self, hwnd: Any, dc: int) -> int:
        self._expect_screen_dc(dc, "ReleaseDC")
        self.screen_dcs -= 1
        return 1

    def MonitorFromPoint(self, pt: Any, flags: int) -> int:
        if flags != win.MONITOR_DEFAULTTONEAREST:
            self.problems.append(f"MonitorFromPoint flags {flags}")
        nearest = min(range(len(self.monitors)), key=lambda i: self.monitors[i].distance(pt.x, pt.y))
        return self.monitor_base + nearest

    def GetDpiForMonitor(self, monitor: int, dpi_type: int, x_ref: Any, y_ref: Any) -> int:
        index = monitor - self.monitor_base
        if not 0 <= index < len(self.monitors):
            self.problems.append(f"GetDpiForMonitor on {monitor!r}, not a monitor")
            return E_INVALIDARG
        x_ref._obj.value = y_ref._obj.value = self.monitors[index].dpi
        return 0

    def UpdateLayeredWindow(
        self, hwnd: int, screen: Any, dst: Any, size: Any, src_dc: int, src: Any, key: int, blend: Any, flags: int
    ) -> int:
        if "UpdateLayeredWindow" in self.fail:
            return 0
        self._expect_screen_dc(screen, "UpdateLayeredWindow")
        d, s, p, b = dst._obj, size._obj, src._obj, blend._obj
        if flags != win.ULW_ALPHA or (p.x, p.y) != (0, 0) or key != 0:
            self.problems.append(f"UpdateLayeredWindow flags={flags} src=({p.x},{p.y}) key={key}")
        if (b.BlendOp, b.BlendFlags, b.SourceConstantAlpha, b.AlphaFormat) != (0, 0, 255, 1):
            self.problems.append("UpdateLayeredWindow blend is not AC_SRC_OVER / 255 / AC_SRC_ALPHA")
        buffer, bitmap_size = self.bitmaps[self.dcs[src_dc]]
        if (s.cx, s.cy) != (bitmap_size, bitmap_size):
            self.problems.append(f"UpdateLayeredWindow size {s.cx}x{s.cy} for a {bitmap_size} px DIB")
        window = self.windows[hwnd]
        window.rect = (d.x, d.y, s.cx, s.cy)
        window.image = np.frombuffer(buffer, np.uint8).reshape(bitmap_size, bitmap_size, 4).copy()
        window.updates += 1
        self._record("UpdateLayeredWindow", hwnd, d.x, d.y, s.cx)
        return 1

    def SetWindowPos(self, hwnd: int, insert_after: Any, x: int, y: int, cx: int, cy: int, flags: int) -> int:
        self._record("SetWindowPos", hwnd, insert_after, flags)
        if not flags & win.SWP_NOACTIVATE:
            self.problems.append("SetWindowPos without SWP_NOACTIVATE")
            self.foreground = hwnd
        window = self.windows[hwnd]
        if flags & win.SWP_SHOWWINDOW:
            window.visible = True
        if flags & win.SWP_HIDEWINDOW:
            window.visible = False
        if insert_after == win.HWND_TOPMOST:
            window.topmost_raises += 1
        return 1

    def IsWindowVisible(self, hwnd: int) -> int:
        return int(self.windows[hwnd].visible)

    def GetWindowRect(self, hwnd: int, rect_ref: Any) -> int:
        window = self.windows.get(hwnd)
        if window is None:
            return 0
        x, y, width, height = window.rect
        r = rect_ref._obj
        r.left, r.top, r.right, r.bottom = x, y, x + width, y + height
        return 1

    def GetForegroundWindow(self) -> int:
        return self.foreground

    def WindowFromPoint(self, pt: Any) -> int:
        """Hit-testing as documented: hidden windows and layered windows with ``WS_EX_TRANSPARENT`` are
        skipped, and another layered window catches only the pixels it draws (alpha above zero)."""
        for hwnd, window in reversed(self.windows.items()):  # the newest on top
            x, y, width, height = window.rect
            if not window.visible or not (x <= pt.x < x + width and y <= pt.y < y + height):
                continue
            if window.ex_style & win.WS_EX_LAYERED:
                if window.ex_style & win.WS_EX_TRANSPARENT:
                    continue
                if window.image is None or not window.image[pt.y - y, pt.x - x, 3]:
                    continue
            return hwnd
        return self.desktop

    # ----- gdi32

    def CreateCompatibleDC(self, dc: Any) -> int:
        self._expect_screen_dc(dc, "CreateCompatibleDC")
        memdc = next(self._handles)
        self.dcs[memdc] = self.stock_bitmap
        return memdc

    def CreateDIBSection(self, dc: Any, info_ref: Any, usage: int, bits_ref: Any, section: Any, offset: int) -> int:
        self._expect_screen_dc(dc, "CreateDIBSection")
        h = info_ref._obj.bmiHeader
        if (h.biSize, h.biPlanes, h.biBitCount, h.biCompression, usage) != (40, 1, 32, win.BI_RGB, 0):
            self.problems.append("CreateDIBSection: not a 32-bit BI_RGB DIB")
        if h.biHeight != -h.biWidth:
            self.problems.append("CreateDIBSection: not a square top-down DIB")
        buffer = (ctypes.c_uint8 * (h.biWidth * h.biWidth * 4))()
        bits_ref._obj.value = ctypes.addressof(buffer)
        bitmap = next(self._handles)
        self.bitmaps[bitmap] = (buffer, h.biWidth)
        return bitmap

    def SelectObject(self, dc: int, obj: int) -> int:
        previous, self.dcs[dc] = self.dcs[dc], obj
        return previous

    def DeleteObject(self, obj: int) -> int:
        if obj in self.dcs.values():
            self.problems.append("DeleteObject on a bitmap still selected into a DC")
            return 0
        self._record("DeleteObject", obj)
        return int(self.bitmaps.pop(obj, None) is not None)

    def DeleteDC(self, dc: int) -> int:
        self._record("DeleteDC", dc)
        return int(self.dcs.pop(dc, None) is not None)

    def GdiFlush(self) -> int:
        return 1


@pytest.fixture(autouse=True)
def _no_fake_saw_win32_misused() -> Iterator[None]:
    FakeWin32.instances.clear()
    yield
    problems = [p for fake in FakeWin32.instances for p in fake.problems]
    FakeWin32.instances.clear()
    assert problems == [], problems[:10]


@pytest.fixture
def fake() -> FakeWin32:
    return FakeWin32()


@pytest.fixture
def started(fake: FakeWin32) -> Iterator[win.WindowsOverlay]:
    overlay = win.WindowsOverlay(api=fake)
    overlay.start()
    yield overlay
    overlay.close()


def show(overlay: win.WindowsOverlay, mode: str, x: float = 500, y: float = 400, **kw: Any) -> OverlayState:
    state = OverlayState(mode=mode, cursor=Point(x, y), **kw)  # type: ignore[arg-type]
    overlay.show(state)
    assert overlay.wait_drawn(2.0)
    return state


def wait_for(predicate: Callable[[], bool], timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def _called_in_source() -> set[str]:
    """The DLL functions ``overlay/windows.py`` calls."""
    source = Path(win.__file__).read_text(encoding="utf-8")
    return set(re.findall(r"\b(?:user32|gdi32|kernel32)\.([A-Z]\w+)\(", source))


def run_cycle(fake: FakeWin32) -> str | None:
    """A whole run on ``fake``: start, every mode on two monitors (and the second hand), the timer, display
    and DPI changes, then close with the window no longer postable (so through the thread message).
    Returns the first draw error, if any."""
    overlay = win.WindowsOverlay(api=fake)
    overlay.start()
    try:
        main, _ = overlay.hwnds
        for x, y in ((-1200, 300), (960, 540)):
            for mode in MODES:
                helper = Point(x + 150, y + 80) if mode == "resize" else None
                show(overlay, mode, x, y, helper=helper, pinch=0.5, progress=0.5)
        for message, wparam in ((win.WM_TIMER, 1), (win.WM_DISPLAYCHANGE, 32), (win.WM_DPICHANGED, 0)):
            fake.PostMessageW(main, message, wparam, 0)
        show(overlay, "point", 10, 10)
        fake.fail.add("PostMessageW")
        return overlay.draw_error
    finally:
        overlay.close()


def _check_shown_at(api: Any, overlay: win.WindowsOverlay, hwnd: int, point: Point) -> None:
    """``hwnd`` shows centred on ``point``, is not the foreground window, and hit-testing passes over it: a
    click or a file drop there reaches the window under the reticle. ``api``: the real Win32 or a fake."""
    user32 = api.user32
    assert user32.IsWindowVisible(hwnd)
    r = win.RECT()
    assert user32.GetWindowRect(hwnd, ctypes.byref(r))
    width, height = r.right - r.left, r.bottom - r.top
    assert width == height
    assert (r.left + width // 2, r.top + height // 2) == point.rounded()
    assert user32.GetForegroundWindow() != hwnd
    # WindowFromPoint runs the hit test that mouse input and OLE drag and drop use to find their target.
    under = user32.WindowFromPoint(win.POINT(*point.rounded()))
    assert under not in overlay.hwnds, f"a click at {point.rounded()} would land on the reticle ({under:#x})"


# --------------------------------------------------------------------------- everywhere: layouts and pure parts


@pytest.mark.skipif(not SIXTY_FOUR_BIT, reason="the sizes below are the Win64 layouts")
def test_structures_have_the_win64_sizes() -> None:
    assert ctypes.sizeof(win.POINT) == 8
    assert ctypes.sizeof(win.SIZE) == 8
    assert ctypes.sizeof(win.RECT) == 16
    assert ctypes.sizeof(win.MSG) == 48
    assert ctypes.sizeof(win.WNDCLASSEXW) == 80
    assert win.WNDCLASSEXW.lpfnWndProc.offset == 8 and win.WNDCLASSEXW.hInstance.offset == 24
    assert ctypes.sizeof(win.BITMAPINFOHEADER) == 40
    assert ctypes.sizeof(win.BITMAPINFO) == 44
    assert ctypes.sizeof(win.BLENDFUNCTION) == 4
    assert win.MSG.wParam.offset == 16 and win.MSG.lParam.offset == 24 and win.MSG.pt.offset == 36


@pytest.mark.skipif(sys.platform == "win32", reason="the real DLLs are checked by the Windows tests")
def test_every_win32_function_the_overlay_calls_has_a_valid_prototype() -> None:
    api = declared_api()  # every prototype assignment is validated here
    called = _called_in_source()
    assert {"CreateWindowExW", "UpdateLayeredWindow", "DefWindowProcW", "CreateDIBSection"} <= called
    for dll in (api.user32, api.gdi32, api.kernel32):
        for name, fn in dll.functions.items():
            assert fn.argtypes is not None, f"{name} has no argtypes"
    declared = {name for dll in (api.user32, api.gdi32, api.kernel32) for name in dll.functions}
    assert called <= declared, f"called without a prototype: {sorted(called - declared)}"
    for optional in (api.SetThreadDpiAwarenessContext, api.GetDpiForMonitor, api.GetWindowLongPtrW):
        assert optional is not None and optional.argtypes is not None
    # Handles, WPARAM and LPARAM are pointer-sized everywhere they appear.
    assert api.user32.DefWindowProcW.argtypes == [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_size_t, ctypes.c_ssize_t]
    assert api.user32.DefWindowProcW.restype is ctypes.c_ssize_t
    assert api.user32.CreateWindowExW.restype is ctypes.c_void_p
    assert api.gdi32.SelectObject.restype is ctypes.c_void_p


def test_every_win32_call_in_a_whole_run_matches_its_declared_prototype() -> None:
    fake = FakeWin32(monitors=[Monitor(0, 0, 1920, 1080, 96), Monitor(-2560, -200, 0, 1240, 144)])
    assert run_cycle(fake) is None
    assert fake.problems == []
    expected = _called_in_source() | {"SetThreadDpiAwarenessContext", "GetDpiForMonitor"}
    assert expected <= fake.exercised, f"never exercised: {sorted(expected - fake.exercised)}"


_original_proto = win._proto


def _redeclare(name: str, change: Callable[[Any, tuple[Any, ...]], tuple[Any, tuple[Any, ...]]]) -> dict[str, Any]:
    """``declared_api`` arguments that declare ``name`` as ``change(restype, argtypes)`` instead."""

    def proto(fn: Any, restype: Any, *argtypes: Any) -> Any:
        if fn.__name__ == name:
            restype, argtypes = change(restype, argtypes)
        return _original_proto(fn, restype, *argtypes)

    return {"proto": proto}


#: Prototypes that disagree with Win32 or with their call sites (bugs only a Windows desktop would show
#: otherwise), each with what the fake reports.
BROKEN_PROTOTYPES = [
    pytest.param(
        _redeclare("CreateWindowExW", lambda r, a: (r, a[:-1])),
        "user32.CreateWindowExW: called with 12 arguments for 11 parameters",
        id="CreateWindowExW-missing-lpParam",
    ),
    pytest.param(
        _redeclare("SetWindowPos", lambda r, a: (r, (*a, win._UINT))),
        "user32.SetWindowPos: the prototype has 8 parameters, Win32 7",
        id="SetWindowPos-one-parameter-too-many",
    ),
    pytest.param(
        _redeclare("CreateDIBSection", lambda r, a: (r, (ctypes.c_int32, *a[1:]))),
        "gdi32.CreateDIBSection argument 1: 0x",
        id="CreateDIBSection-hdc-as-int32",
    ),
    pytest.param(
        _redeclare("UpdateLayeredWindow", lambda r, a: (r, (*a[:3], win.SIZE, *a[4:]))),
        "user32.UpdateLayeredWindow argument 4:",
        id="UpdateLayeredWindow-size-by-value",
    ),
    pytest.param(
        _redeclare("GetModuleHandleW", lambda r, a: (ctypes.c_int32, a)),
        "RegisterClassExW: hInstance",
        id="GetModuleHandleW-returns-int32",
    ),
    pytest.param(
        _redeclare("MonitorFromPoint", lambda r, a: (ctypes.c_int32, a)),
        "GetDpiForMonitor on",
        id="MonitorFromPoint-returns-int32",
    ),
    pytest.param({"_LPARAM": ctypes.c_int32}, "WM_NCCREATE reached DefWindowProcW as", id="LPARAM-as-int32"),
]


@pytest.mark.skipif(not SIXTY_FOUR_BIT, reason="several of these only truncate 64-bit values")
@pytest.mark.parametrize(("breakage", "reported"), BROKEN_PROTOTYPES)
def test_a_prototype_that_disagrees_with_win32_or_its_call_site_is_caught(
    breakage: dict[str, Any], reported: str
) -> None:
    fake = FakeWin32(declared=declared_api(**breakage))
    with contextlib.suppress(OverlayError):
        run_cycle(fake)
    assert any(p.startswith(reported) for p in fake.problems), fake.problems[:5]
    fake.problems.clear()  # expected here


def test_the_window_styles_are_click_through_topmost_tool_windows_that_never_activate() -> None:
    assert win.OVERLAY_EX_STYLE == 0x00080000 | 0x20 | 0x8 | 0x80 | 0x08000000
    assert win.WS_POPUP == 0x80000000


def test_the_mailbox_posts_one_wake_up_per_burst_and_hands_over_the_newest_state() -> None:
    box = win.Mailbox()
    a, b, c = (OverlayState("point", Point(i, i)) for i in range(3))
    assert box.put(a) is True
    assert box.put(b) is False and box.put(c) is False
    assert not box.wait_drawn(0.0)
    state, seq = box.take()
    assert state == c
    assert box.put(a) is True  # arrived while drawing: posts again
    box.drawn(seq)
    assert not box.wait_drawn(0.0)
    state, seq = box.take()
    box.drawn(seq)
    assert state == a and box.wait_drawn(0.0)


def test_the_mailbox_retries_a_failed_wake_up_and_ignores_states_after_close() -> None:
    box = win.Mailbox()
    assert box.put(OverlayState()) is True
    box.wake_failed()
    assert box.put(OverlayState("idle")) is True
    box.close()
    assert box.put(OverlayState("point")) is False
    assert box.state == OverlayState("idle")
    assert box.wait_drawn(5.0) is False  # returns at once once closed


@pytest.mark.skipif(sys.platform == "win32", reason="checks the behaviour off Windows")
def test_off_windows_start_raises_overlay_error_and_the_factory_gives_null() -> None:
    overlay = win.WindowsOverlay()
    with pytest.raises(OverlayError):
        overlay.start()
    overlay.show(OverlayState("point", Point(1, 1)))
    overlay.close()
    assert isinstance(create_overlay(), NullOverlay)


def test_show_and_close_without_start_do_nothing() -> None:
    overlay = win.WindowsOverlay(api=FakeWin32())
    overlay.show(OverlayState("point", Point(1, 1)))
    overlay.close()
    overlay.close()
    assert overlay.hwnds == () and overlay.draw_error is None and not overlay.wait_drawn(0.0)


# --------------------------------------------------------------------------- everywhere: the overlay on a fake Win32


def test_start_makes_two_hidden_layered_windows_on_a_per_monitor_aware_thread(
    fake: FakeWin32, started: win.WindowsOverlay
) -> None:
    main, helper = started.hwnds
    for hwnd in (main, helper):
        window = fake.windows[hwnd]
        assert window.ex_style == win.OVERLAY_EX_STYLE and window.style == win.WS_POPUP
        assert not window.visible
    assert fake.thread_awareness == [win.DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2]
    (timer,) = fake.named("SetTimer")
    assert timer[2:] == (main, 1, win.TOPMOST_INTERVAL_MS)
    # The 64-bit message parameters reached DefWindowProcW intact through the callback.
    assert ("DefWindowProcW", main, WM_NCCREATE, WIDE_WPARAM, WIDE_LPARAM) in fake.calls
    assert fake.problems == []


def test_close_destroys_the_windows_then_frees_gdi_and_the_class(fake: FakeWin32) -> None:
    overlay = win.WindowsOverlay(api=fake)
    overlay.start()
    show(overlay, "point")
    show(overlay, "resize", helper=Point(900, 500))
    thread = overlay._ui.thread  # type: ignore[union-attr]
    overlay.close()
    assert not thread.is_alive()
    assert fake.windows == {} and fake.classes == {}
    assert fake.bitmaps == {} and fake.dcs == {}
    assert fake.screen_dcs == 0
    names = [c[0] for c in fake.calls]
    assert names.index("UnregisterClassW") > max(i for i, n in enumerate(names) if n == "DestroyWindow")
    assert overlay.hwnds == ()
    overlay.close()  # again: nothing to do, nothing raised
    assert fake.problems == []


def test_show_draws_the_reticle_centred_on_the_point(fake: FakeWin32, started: win.WindowsOverlay) -> None:
    main, helper = started.hwnds
    state = show(started, "point", 500, 400, pinch=0.5)
    window = fake.windows[main]
    assert window.visible
    assert window.rect == (452, 352, 96, 96)
    assert np.array_equal(window.image, render(state, 96))
    assert not fake.windows[helper].visible
    _check_shown_at(fake, started, main, Point(500, 400))
    assert fake.problems == []


def test_a_click_on_the_reticle_reaches_the_window_under_it(fake: FakeWin32, started: win.WindowsOverlay) -> None:
    main, helper = started.hwnds
    show(started, "resize", 500, 400, helper=Point(900, 600))  # opaque centres, both windows shown
    _check_shown_at(fake, started, main, Point(500, 400))
    _check_shown_at(fake, started, helper, Point(900, 600))
    # The check notices a reticle that would catch the click (here: without WS_EX_TRANSPARENT).
    fake.windows[main].ex_style &= ~win.WS_EX_TRANSPARENT
    with pytest.raises(AssertionError, match="would land on the reticle"):
        _check_shown_at(fake, started, main, Point(500, 400))


@pytest.mark.parametrize("mode", [m for m in MODES if m != "hidden"])
def test_every_mode_shows(fake: FakeWin32, started: win.WindowsOverlay, mode: str) -> None:
    main, _ = started.hwnds
    state = show(started, mode, 300, 200, pinch=0.25, progress=0.75)
    size = reticle_size(mode, 96)  # type: ignore[arg-type]
    assert fake.windows[main].rect == (300 - size // 2, 200 - size // 2, size, size)
    assert np.array_equal(fake.windows[main].image, render(state, size))
    _check_shown_at(fake, started, main, Point(300, 200))
    assert started.draw_error is None and fake.problems == []


def test_hidden_or_no_cursor_hides_both_windows(fake: FakeWin32, started: win.WindowsOverlay) -> None:
    main, helper = started.hwnds
    show(started, "resize", helper=Point(800, 300))
    assert fake.windows[main].visible and fake.windows[helper].visible
    show(started, "hidden")
    assert not fake.windows[main].visible and not fake.windows[helper].visible
    show(started, "point")
    started.show(OverlayState("point", cursor=None))
    assert started.wait_drawn(2.0)
    assert not fake.windows[main].visible
    hides = [c for c in fake.named("SetWindowPos") if c[3] & win.SWP_HIDEWINDOW]
    assert len(hides) == 3 and fake.problems == []


def test_the_helper_marker_shows_only_with_a_helper_point(fake: FakeWin32, started: win.WindowsOverlay) -> None:
    main, helper = started.hwnds
    show(started, "resize", 500, 400, helper=Point(900, 600))
    marker = fake.windows[helper]
    assert marker.visible and marker.rect == (876, 576, 48, 48)
    assert np.array_equal(marker.image, render_helper(48))
    show(started, "grab", 500, 400)
    assert not fake.windows[helper].visible and fake.windows[main].visible


def test_sizes_follow_the_dpi_of_the_monitor_under_each_point() -> None:
    fake = FakeWin32(monitors=[Monitor(0, 0, 1920, 1080, 96), Monitor(-2560, -200, 0, 1240, 144)])
    overlay = win.WindowsOverlay(api=fake)
    overlay.start()
    try:
        main, helper = overlay.hwnds
        show(overlay, "point", -1280, 500)
        assert fake.windows[main].rect == (-1280 - 72, 500 - 72, 144, 144)
        show(overlay, "point", 960, 540)
        assert fake.windows[main].rect == (960 - 48, 540 - 48, 96, 96)
        show(overlay, "resize", 100, 100, helper=Point(-100, -100))
        assert fake.windows[main].rect[2:] == (96, 96)
        assert fake.windows[helper].rect == (-100 - 36, -100 - 36, 72, 72)
        show(overlay, "calibrate", 0, 0, progress=0.5)
        assert fake.windows[main].rect == (-72, -72, 144, 144)
    finally:
        overlay.close()
    assert fake.bitmaps == {} and fake.problems == []


def test_an_unchanged_state_does_not_redraw(fake: FakeWin32, started: win.WindowsOverlay) -> None:
    main, _ = started.hwnds
    show(started, "point", 500, 400, pinch=0.5)
    show(started, "point", 500, 400, pinch=0.5 + 0.1 / 32)  # the same quantized image
    assert fake.windows[main].updates == 1
    show(started, "point", 501, 400, pinch=0.5)
    assert fake.windows[main].updates == 2


def test_a_burst_of_shows_draws_the_newest_state(fake: FakeWin32, started: win.WindowsOverlay) -> None:
    main, _ = started.hwnds
    for x in range(100, 400, 3):
        started.show(OverlayState("point", Point(x, 300)))
    started.show(OverlayState("drag", Point(777, 333)))
    assert started.wait_drawn(2.0)
    assert fake.windows[main].rect[:2] == (777 - 48, 333 - 48)
    wakes = [c for c in fake.named("PostMessageW") if c[2] == win.WM_OVERLAY_WAKE]
    assert len(wakes) < 101  # coalesced (how much depends on the threads' timing)


def test_the_timer_keeps_visible_windows_topmost(fake: FakeWin32, started: win.WindowsOverlay) -> None:
    main, helper = started.hwnds
    show(started, "point")
    before_main, before_helper = fake.windows[main].topmost_raises, fake.windows[helper].topmost_raises
    fake.PostMessageW(main, win.WM_TIMER, 1, 0)
    assert wait_for(lambda: fake.windows[main].topmost_raises == before_main + 1)
    assert fake.windows[helper].topmost_raises == before_helper  # hidden: left alone
    flags = fake.named("SetWindowPos")[-1][3]
    assert flags == win.SWP_NOMOVE | win.SWP_NOSIZE | win.SWP_NOACTIVATE


def test_a_display_change_redraws_at_the_new_dpi(fake: FakeWin32, started: win.WindowsOverlay) -> None:
    main, _ = started.hwnds
    show(started, "point", 500, 400)
    fake.monitors[0].dpi = 120
    fake.PostMessageW(main, win.WM_DISPLAYCHANGE, 32, 0)
    assert wait_for(lambda: fake.windows[main].rect == (500 - 60, 400 - 60, 120, 120))
    fake.monitors[0].dpi = 144
    fake.PostMessageW(main, win.WM_DPICHANGED, 0, 0)
    assert wait_for(lambda: fake.windows[main].rect[2] == 144)
    assert fake.problems == []


def test_a_failing_draw_is_logged_once_and_never_reaches_the_caller(
    fake: FakeWin32, started: win.WindowsOverlay, caplog: pytest.LogCaptureFixture
) -> None:
    fake.fail.add("UpdateLayeredWindow")
    with caplog.at_level(logging.WARNING, logger="jarvis_hands.overlay.windows"):
        for x in (100, 200, 300):
            show(started, "point", x, 100)
    assert started.draw_error is not None and "UpdateLayeredWindow" in started.draw_error
    assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 1
    fake.fail.clear()
    show(started, "point", 400, 100)
    main, _ = started.hwnds
    assert fake.windows[main].visible and fake.windows[main].rect[:2] == (352, 52)


def test_a_failed_wake_up_is_retried_by_the_next_show(fake: FakeWin32, started: win.WindowsOverlay) -> None:
    main, _ = started.hwnds
    fake.fail.add("PostMessageW")
    started.show(OverlayState("point", Point(10, 10)))
    fake.fail.clear()
    show(started, "point", 600, 600)
    assert fake.windows[main].rect[:2] == (552, 552)


def test_start_failure_raises_overlay_error_and_cleans_up(fake: FakeWin32) -> None:
    fake.fail.add("CreateWindowExW")  # the second window fails
    overlay = win.WindowsOverlay(api=fake)
    with pytest.raises(OverlayError, match="CreateWindowExW"):
        overlay.start()
    assert fake.windows == {} and fake.classes == {} and fake.dcs == {}
    assert overlay.hwnds == ()
    overlay.show(OverlayState("point", Point(1, 1)))
    overlay.close()


def test_start_and_close_twice_with_a_new_class_each_time(fake: FakeWin32) -> None:
    overlay = win.WindowsOverlay(api=fake)
    for _ in range(2):
        overlay.start()
        overlay.start()  # already started: nothing new
        show(overlay, "point")
        overlay.close()
    classes = [c[1] for c in fake.named("RegisterClassExW")]
    assert len(classes) == 2 and len(set(classes)) == 2
    assert fake.windows == {} and fake.classes == {} and fake.problems == []
    overlay.show(OverlayState("point", Point(1, 1)))  # after close: ignored, no raise


# --------------------------------------------------------------------------- Windows: the real windows


def _user32() -> Any:
    return win._win32().user32


@pytest.fixture
def per_monitor_aware_thread() -> Iterator[None]:
    """This test thread reads rects in physical pixels, like the overlay's thread."""
    api = win._win32()
    if api.SetThreadDpiAwarenessContext is None:
        yield
        return
    previous = api.SetThreadDpiAwarenessContext(win.DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
    try:
        yield
    finally:
        if previous:
            api.SetThreadDpiAwarenessContext(previous)


@pytest.fixture
def desktop(per_monitor_aware_thread: None) -> None:
    user32 = _user32()
    win._proto(user32.GetCursorPos, ctypes.c_int32, ctypes.POINTER(win.POINT))
    if not user32.GetCursorPos(ctypes.byref(win.POINT())):
        pytest.skip(f"no interactive desktop here (GetCursorPos failed with error {win._last_error()})")


@pytest.fixture
def real() -> Iterator[win.WindowsOverlay]:
    overlay = win.WindowsOverlay()
    overlay.start()
    yield overlay
    overlay.close()


def _monitor_rects() -> list[tuple[int, int, int, int]]:
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
    proc_type = ctypes.WINFUNCTYPE(  # type: ignore[attr-defined]
        wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM
    )
    user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), proc_type, wintypes.LPARAM]
    user32.EnumDisplayMonitors.restype = wintypes.BOOL
    rects: list[tuple[int, int, int, int]] = []

    def collect(monitor: Any, dc: Any, rect: Any, data: Any) -> bool:
        r = rect.contents
        rects.append((r.left, r.top, r.right, r.bottom))
        return True

    callback = proc_type(collect)
    assert user32.EnumDisplayMonitors(None, None, callback, 0)
    return rects


@windows_only
def test_real_overlay_shows_every_mode_and_hides(desktop: None, real: win.WindowsOverlay) -> None:
    main, helper = real.hwnds
    rects = _monitor_rects()
    left, top, right, bottom = rects[0]
    points = [Point((left + right) / 2, (top + bottom) / 2), Point(left + 40, top + 40)]
    for point in points:
        for mode in MODES:
            other = Point(point.x + 150, point.y + 80) if mode == "resize" else None
            real.show(OverlayState(mode=mode, cursor=point, helper=other, pinch=0.5, progress=0.5))
            assert real.wait_drawn(2.0)
            assert real.draw_error is None
            if mode == "hidden":
                assert not _user32().IsWindowVisible(main)
                continue
            _check_shown_at(win._win32(), real, main, point)
            if other is not None:
                _check_shown_at(win._win32(), real, helper, other)
            else:
                assert not _user32().IsWindowVisible(helper)
    real.show(OverlayState())
    assert real.wait_drawn(2.0)
    assert not _user32().IsWindowVisible(main) and not _user32().IsWindowVisible(helper)


@windows_only
def test_real_overlay_works_at_negative_coordinates(desktop: None, real: win.WindowsOverlay) -> None:
    negative = [r for r in _monitor_rects() if r[0] < 0 or r[1] < 0]
    if not negative:
        pytest.skip("no display left of or above the primary")
    left, top, right, bottom = negative[0]
    point = Point((left + right) // 2, (top + bottom) // 2)
    main, _ = real.hwnds
    for mode in ("point", "grab", "calibrate"):
        real.show(OverlayState(mode=mode, cursor=point))  # type: ignore[arg-type]
        assert real.wait_drawn(2.0) and real.draw_error is None
        _check_shown_at(win._win32(), real, main, point)


@windows_only
def test_real_windows_are_layered_click_through_topmost_tool_windows(desktop: None, real: win.WindowsOverlay) -> None:
    api = win._win32()
    assert api.GetWindowLongPtrW is not None
    for hwnd in real.hwnds:
        ex_style = api.GetWindowLongPtrW(hwnd, win.GWL_EXSTYLE)
        for bit in (
            win.WS_EX_LAYERED,
            win.WS_EX_TRANSPARENT,
            win.WS_EX_TOPMOST,
            win.WS_EX_TOOLWINDOW,
            win.WS_EX_NOACTIVATE,
        ):
            assert ex_style & bit, f"missing extended style 0x{bit:08X}"


@windows_only
def test_real_overlay_starts_and_closes_twice(desktop: None) -> None:
    for _ in range(2):
        overlay = win.WindowsOverlay()
        overlay.start()
        assert len(overlay.hwnds) == 2
        overlay.show(OverlayState("point", Point(200, 200)))
        assert overlay.wait_drawn(2.0)
        overlay.close()
        overlay.close()
        overlay.show(OverlayState("point", Point(300, 300)))  # after close: ignored
    overlay = win.WindowsOverlay()
    for _ in range(2):  # the same instance again
        overlay.start()
        overlay.close()
