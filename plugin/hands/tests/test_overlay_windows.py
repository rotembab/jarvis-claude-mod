"""The Windows overlay: its logic against a fake Win32 on every OS, the real windows on Windows.

The fake is just enough of user32, gdi32, kernel32 and shcore for the overlay,
behaving like Windows where the overlay depends on it: a real message queue
(``GetMessageW`` blocks, ``PostQuitMessage`` ends it), the window procedure
called through the class's function pointer (so the real ctypes callback
path runs), ``DestroyWindow`` sending ``WM_DESTROY``, a DIB section whose
memory the overlay writes and ``UpdateLayeredWindow`` reads, and
``WindowFromPoint`` hit-testing as the layered-window docs describe.

The keyboard layer is tested here as a window (rectangular DIBs, the third layer, health, capture exclusion) with a
stand-in painter; ``test_kb_render.py`` has the pixels.

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
import statistics
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, ClassVar

import numpy as np
import pytest

from jarvis_hands import clock, logs
from jarvis_hands.geometry import Point
from jarvis_hands.overlay import NullOverlay, OverlayError, OverlayState, create_overlay
from jarvis_hands.overlay import keyboard_render as kr
from jarvis_hands.overlay import windows as win
from jarvis_hands.overlay.base import ComposeView, KeyboardView, OverlayHealth
from jarvis_hands.overlay.render import MODES, render, render_helper, reticle_size

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="layered windows exist on Windows only")
needs_font = pytest.mark.skipif(not kr.font_available(), reason="no font with Latin and Hebrew glyphs on this machine")
SIXTY_FOUR_BIT = ctypes.sizeof(ctypes.c_void_p) == 8

WM_CREATE = 0x0001
WM_NCCREATE = 0x0081
E_INVALIDARG = -0x7FF8FFA9  # 0x80070057 as an HRESULT
#: Message parameters that only survive a window procedure declared with pointer-sized WPARAM and LPARAM.
WIDE_WPARAM = 2**63 + 5 if SIXTY_FOUR_BIT else 2**31 + 5
WIDE_LPARAM = -(2**62) - 7 if SIXTY_FOUR_BIT else -(2**30) - 7
#: ``WDA_EXCLUDEFROMCAPTURE``: the window is left out of screenshots and screen sharing (Windows 10 2004 and later).
WDA_EXCLUDEFROMCAPTURE = 0x11
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
    #: What ``SetWindowDisplayAffinity`` last set (0 = ``WDA_NONE``).
    affinity: int = 0


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
        #: Per DIB: its memory and its width and height in pixels.
        self.bitmaps: dict[int, tuple[Any, int, int]] = {}
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
        buffer, bitmap_width, bitmap_height = self.bitmaps[self.dcs[src_dc]]
        if (s.cx, s.cy) != (bitmap_width, bitmap_height):
            self.problems.append(f"UpdateLayeredWindow size {s.cx}x{s.cy} for a {bitmap_width}x{bitmap_height} DIB")
        window = self.windows[hwnd]
        window.rect = (d.x, d.y, s.cx, s.cy)
        window.image = np.frombuffer(buffer, np.uint8).reshape(bitmap_height, bitmap_width, 4).copy()
        window.updates += 1
        self._record("UpdateLayeredWindow", hwnd, d.x, d.y, s.cx, s.cy)
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

    def SetWindowDisplayAffinity(self, hwnd: int, affinity: int) -> int:
        self._record("SetWindowDisplayAffinity", hwnd, affinity)
        if "SetWindowDisplayAffinity" in self.fail:
            return 0
        if affinity not in (0, WDA_EXCLUDEFROMCAPTURE):
            self.problems.append(
                f"SetWindowDisplayAffinity({affinity:#x}): neither WDA_NONE nor WDA_EXCLUDEFROMCAPTURE"
            )
        self.windows[hwnd].affinity = affinity
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
        if "CreateDIBSection" in self.fail:
            return 0
        h = info_ref._obj.bmiHeader
        if (h.biSize, h.biPlanes, h.biBitCount, h.biCompression, usage) != (40, 1, 32, win.BI_RGB, 0):
            self.problems.append("CreateDIBSection: not a 32-bit BI_RGB DIB")
        if h.biHeight >= 0:
            self.problems.append("CreateDIBSection: not a top-down DIB")
        width, height = h.biWidth, abs(h.biHeight)
        buffer = (ctypes.c_uint8 * (width * height * 4))()
        bits_ref._obj.value = ctypes.addressof(buffer)
        bitmap = next(self._handles)
        self.bitmaps[bitmap] = (buffer, width, height)
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


WORK = (0, 0, 1920, 1080)


def kb_view(**kw: Any) -> KeyboardView:
    fields: dict[str, Any] = {
        "seq": 0,
        "mode": "live",
        "phase": "typing",
        "lang": "en",
        "shift": False,
        "private": False,
        "pulse": False,
        "hold": None,
        "armed_enter": False,
        "drift": False,
        "tips": (),
        "homes": (),
        "lit": (),
        "strip": "",
        "echo": "",
        "prompt": "",
        "progress": 0.0,
        "work": WORK,
    }
    fields.update(kw)
    return KeyboardView(**fields)


def show_keyboard(overlay: win.WindowsOverlay, **kw: Any) -> OverlayState:
    state = OverlayState(keyboard=kb_view(**kw))
    overlay.show(state)
    assert overlay.wait_drawn(2.0)
    return state


class FakeClock:
    """The helper's clock, set by the test: ``clock.now`` is replaced by one of these."""

    def __init__(self) -> None:
        self.t = 5000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def ticking(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    stand_in = FakeClock()
    monkeypatch.setattr(clock, "now", stand_in)
    return stand_in


class StubPainter:
    """The keyboard painter without pixels or a font: a flat frame of the geometry's size.

    Like the real ``compose`` it hands back the previous array when the view is equal apart from ``seq`` (the layer
    relies on that to skip a push), and it takes ``cost`` seconds of the injected clock for every new frame.
    """

    def __init__(self) -> None:
        self.font = True
        self.cost = 0.0
        self.clock: FakeClock | None = None
        self.composes = 0
        self.cleared = 0
        self._last: tuple[Any, np.ndarray] | None = None

    def bake_base(self, lang: str, shift: bool, geometry: kr.KeyboardGeometry, commit: str = "direct") -> np.ndarray:
        return np.zeros((geometry.height, geometry.width, 4), np.uint8)

    def compose(self, base: np.ndarray, geometry: kr.KeyboardGeometry, view: KeyboardView) -> np.ndarray:
        self.composes += 1
        key = (geometry, replace(view, seq=0))
        if self._last is not None and self._last[0] == key:
            return self._last[1]
        if self.clock is not None:
            self.clock.t += self.cost
        image = np.full((geometry.height, geometry.width, 4), self.composes % 200 + 1, np.uint8)
        self._last = (key, image)
        return image

    def font_available(self) -> bool:
        return self.font

    def clear_cache(self) -> None:
        self.cleared += 1
        self._last = None


@pytest.fixture
def painted(monkeypatch: pytest.MonkeyPatch) -> StubPainter:
    """The windows are tested here, not the pixels: ``test_kb_render.py`` has those."""
    painter = StubPainter()
    monkeypatch.setattr(kr, "bake_base", painter.bake_base)
    monkeypatch.setattr(kr, "compose", painter.compose)
    monkeypatch.setattr(kr, "font_available", painter.font_available)
    monkeypatch.setattr(kr, "clear_cache", painter.clear_cache)
    return painter


def _called_in_source() -> set[str]:
    """The DLL functions ``overlay/windows.py`` calls."""
    source = Path(win.__file__).read_text(encoding="utf-8")
    return set(re.findall(r"\b(?:user32|gdi32|kernel32)\.([A-Z]\w+)\(", source))


def run_cycle(fake: FakeWin32) -> str | None:
    """A whole run on ``fake``: start, every mode on two monitors (and the second hand), the keyboard with capture
    exclusion turned on and off, the timer, display and DPI changes, then close with the window no longer postable
    (so through the thread message). Returns the first draw error, if any. Needs the ``painted`` stand-in."""
    overlay = win.WindowsOverlay(api=fake)
    overlay.start()
    try:
        main, _ = overlay.hwnds
        for x, y in ((-1200, 300), (960, 540)):
            for mode in MODES:
                helper = Point(x + 150, y + 80) if mode == "resize" else None
                show(overlay, mode, x, y, helper=helper, pinch=0.5, progress=0.5)
        for step, exclude in enumerate((False, True, False)):
            show_keyboard(overlay, progress=0.1 * (step + 1), exclude_capture=exclude)
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


def test_every_win32_call_in_a_whole_run_matches_its_declared_prototype(painted: StubPainter) -> None:
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
    breakage: dict[str, Any], reported: str, painted: StubPainter
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


# --------------------------------------------------------------------------- everywhere: rectangular layers (O4)


def layer_on(fake: FakeWin32) -> win._Layer:
    """A layer on a window of the fake that nothing else owns, to drive ``show`` directly."""
    hwnd = next(fake._handles)
    fake.windows[hwnd] = FakeWindow("test", win.OVERLAY_EX_STYLE, win.WS_POPUP, thread=FakeWin32._thread())
    return win._Layer(fake, hwnd)


def picture(width: int, height: int, seed: int = 0) -> np.ndarray:
    return np.random.default_rng(seed).integers(0, 256, (height, width, 4), dtype=np.uint8)


def test_o4_a_surface_takes_a_width_and_a_height(fake: FakeWin32) -> None:
    surface = win._Surface(fake, 8, 5)
    assert surface.pixels is not None and surface.pixels.shape == (5, 8, 4)
    assert (surface.width, surface.height) == (8, 5)
    ((buffer, width, height),) = fake.bitmaps.values()
    assert (width, height) == (8, 5) and len(buffer) == 8 * 5 * 4
    surface.pixels[4, 7] = (1, 2, 3, 4)  # the last pixel is the last four bytes: top-down rows of the stated width
    assert list(buffer[-4:]) == [1, 2, 3, 4]
    surface.destroy()
    surface.destroy()  # twice: nothing more to free, nothing raised
    assert fake.bitmaps == {} and fake.dcs == {} and fake.screen_dcs == 0


def test_o4_a_square_surface_is_made_from_one_size_as_before(fake: FakeWin32) -> None:
    surface = win._Surface(fake, 6)
    assert surface.pixels is not None and surface.pixels.shape == (6, 6, 4)
    assert surface.size == 6 and (surface.width, surface.height) == (6, 6)
    surface.destroy()
    assert fake.bitmaps == {} and fake.problems == []


def test_o4_a_failing_surface_frees_what_it_made(fake: FakeWin32) -> None:
    fake.fail.add("CreateDIBSection")
    with pytest.raises(OSError, match="CreateDIBSection"):
        win._Surface(fake, 8, 5)
    assert fake.bitmaps == {} and fake.dcs == {} and fake.screen_dcs == 0


def test_o4_a_layer_pushes_images_of_any_size_and_rebuilds_its_surface_when_the_size_changes(fake: FakeWin32) -> None:
    layer = layer_on(fake)
    window = fake.windows[layer.hwnd]
    seen = None
    for width, height in ((8, 5), (5, 8), (8, 6), (7, 7), (7, 7), (96, 96)):
        image = picture(width, height, seed=width * 100 + height)
        layer.show(image, (3, 4))
        assert window.rect == (3, 4, width, height)
        assert np.array_equal(window.image, image)
        assert layer.surface is not None and (layer.surface.width, layer.surface.height) == (width, height)
        assert len(fake.bitmaps) == 1  # the previous DIB went when the size changed
        if (width, height) == (7, 7):
            seen = seen or layer.surface  # the same size again: the same surface
            assert layer.surface is seen
    assert window.visible
    layer.release_surface()
    assert fake.bitmaps == {} and fake.dcs == {} and fake.screen_dcs == 0 and fake.problems == []


def test_o4_a_wide_image_and_a_tall_image_of_the_same_area_are_not_confused(fake: FakeWin32) -> None:
    layer = layer_on(fake)
    layer.show(picture(12, 3), (0, 0))
    first = layer.surface
    layer.show(picture(3, 12), (0, 0))
    assert layer.surface is not first and fake.windows[layer.hwnd].rect == (0, 0, 3, 12)
    layer.release_surface()


def test_o4_the_fake_notes_a_bottom_up_dib_and_a_wrong_sized_update(fake: FakeWin32) -> None:
    info = win.BITMAPINFO()
    header = info.bmiHeader
    header.biSize, header.biWidth, header.biHeight = ctypes.sizeof(win.BITMAPINFOHEADER), 8, 5  # positive: bottom-up
    header.biPlanes, header.biBitCount, header.biCompression = 1, 32, win.BI_RGB
    bits = ctypes.c_void_p()
    screen = fake.user32.GetDC(None)
    fake.gdi32.CreateDIBSection(screen, ctypes.byref(info), win.DIB_RGB_COLORS, ctypes.byref(bits), None, 0)
    fake.user32.ReleaseDC(None, screen)
    assert any("not a top-down DIB" in p for p in fake.problems)
    fake.problems.clear()
    layer = layer_on(fake)
    layer.show(picture(8, 5), (0, 0))
    fake.user32.UpdateLayeredWindow(
        layer.hwnd, fake.screen_dc, ctypes.byref(win.POINT(0, 0)), ctypes.byref(win.SIZE(5, 8)),
        layer.surface.memdc, ctypes.byref(win.POINT(0, 0)), 0,
        ctypes.byref(win.BLENDFUNCTION(win.AC_SRC_OVER, 0, 255, win.AC_SRC_ALPHA)), win.ULW_ALPHA,
    )  # fmt: skip
    assert any("UpdateLayeredWindow size 5x8 for a 8x5 DIB" in p for p in fake.problems)
    fake.problems.clear()
    layer.release_surface()


# --------------------------------------------------------------------------- everywhere: the keyboard layer (O5)


def test_o5_start_makes_a_third_hidden_window_for_the_keyboard(fake: FakeWin32, started: win.WindowsOverlay) -> None:
    main, helper = started.hwnds  # the reticle's two windows, as before
    keyboard = started.keyboard_hwnd
    assert keyboard is not None and keyboard not in (main, helper) and len(fake.windows) == 3
    window = fake.windows[keyboard]
    assert window.ex_style == win.OVERLAY_EX_STYLE and window.style == win.WS_POPUP and not window.visible
    # the same window class: a click never activates it either
    assert fake.classes[window.class_name](keyboard, win.WM_MOUSEACTIVATE, 0, 0) == win.MA_NOACTIVATE


def test_o5_a_keyboard_state_shows_the_keyboard_layer_and_hides_the_reticle_layers(
    fake: FakeWin32, started: win.WindowsOverlay, painted: StubPainter
) -> None:
    main, helper = started.hwnds
    keyboard = started.keyboard_hwnd
    assert keyboard is not None
    show(started, "resize", 500, 400, helper=Point(900, 600))
    assert fake.windows[main].visible and fake.windows[helper].visible
    show_keyboard(started)
    geometry, origin = kr.keyboard_geometry(WORK, 1.0, 96, "top", "direct")
    window = fake.windows[keyboard]
    assert window.visible and window.rect == (*origin, geometry.width, geometry.height)
    assert window.rect[2] > window.rect[3]  # a wide window: not the reticle's square
    assert window.image is not None and window.image.shape == (geometry.height, geometry.width, 4)
    assert not fake.windows[main].visible and not fake.windows[helper].visible
    # a state that has a keyboard never draws the reticle, whatever else it says
    started.show(OverlayState("point", Point(500, 400), keyboard=kb_view(seq=1)))
    assert started.wait_drawn(2.0)
    assert not fake.windows[main].visible and window.visible
    assert fake.problems == [] and started.draw_error is None


@pytest.mark.parametrize("dock", ["top", "bottom"])
def test_o5_the_keyboard_window_sits_where_the_geometry_says(
    fake: FakeWin32, started: win.WindowsOverlay, painted: StubPainter, dock: str
) -> None:
    work = (100, 50, 1600, 900)
    show_keyboard(started, work=work, dock=dock, size=0.6, commit="review")
    geometry, origin = kr.keyboard_geometry(work, 0.6, 96, dock, "review")  # type: ignore[arg-type]
    assert started.keyboard_hwnd is not None
    assert fake.windows[started.keyboard_hwnd].rect == (*origin, geometry.width, geometry.height)


def test_o5_the_keyboard_is_sized_for_the_monitor_under_the_middle_of_its_work_area() -> None:
    fake = FakeWin32(monitors=[Monitor(0, 0, 1920, 1080, 96), Monitor(-2560, -200, 0, 1240, 144)])
    overlay = win.WindowsOverlay(api=fake)
    overlay.start()
    painter = StubPainter()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(kr, "bake_base", painter.bake_base)
        mp.setattr(kr, "compose", painter.compose)
        try:
            work = (-2560, -200, 2560, 1440)  # its middle, (-1280, 520), is on the 144 DPI monitor
            show_keyboard(overlay, work=work)
            geometry, origin = kr.keyboard_geometry(work, 1.0, 144, "top", "direct")
            assert overlay.keyboard_hwnd is not None
            assert fake.windows[overlay.keyboard_hwnd].rect == (*origin, geometry.width, geometry.height)
            assert origin[0] < 0  # and at negative coordinates, where the left monitor is
            show_keyboard(overlay, work=WORK)
            assert fake.windows[overlay.keyboard_hwnd].rect[2] == kr.keyboard_geometry(WORK, 1.0, 96, "top")[0].width
            # the middle decides, not the corner: this one starts on the 144 DPI monitor and is centred on the other
            straddling = (-1000, 0, 3000, 1000)
            show_keyboard(overlay, work=straddling)
            expected = kr.keyboard_geometry(straddling, 1.0, 96, "top", "direct")[0]
            assert fake.windows[overlay.keyboard_hwnd].rect[2:] == (expected.width, expected.height)
        finally:
            overlay.close()
    assert fake.problems == []


def test_o5_a_view_equal_apart_from_seq_is_not_pushed_again(
    fake: FakeWin32, started: win.WindowsOverlay, painted: StubPainter
) -> None:
    assert started.keyboard_hwnd is not None
    window = fake.windows[started.keyboard_hwnd]
    show_keyboard(started, seq=1)
    show_keyboard(started, seq=2)
    show_keyboard(started, seq=3)
    assert window.updates == 1
    show_keyboard(started, seq=4, strip="Review")
    assert window.updates == 2


def test_o5_going_back_to_the_reticle_hides_the_keyboard_and_frees_its_surface(
    fake: FakeWin32, started: win.WindowsOverlay, painted: StubPainter
) -> None:
    main, _ = started.hwnds
    keyboard = started.keyboard_hwnd
    assert keyboard is not None
    show_keyboard(started)
    assert fake.windows[keyboard].visible and len(fake.bitmaps) == 1
    geometry = kr.keyboard_geometry(WORK, 1.0, 96, "top", "direct")[0]
    show(started, "point", 500, 400)
    assert not fake.windows[keyboard].visible and fake.windows[main].visible
    assert [(w, h) for _, w, h in fake.bitmaps.values()] == [(96, 96)]  # only the reticle's own DIB is left
    show_keyboard(started)
    show(started, "hidden")  # no hand and no keyboard: nothing at all shows
    assert not any(w.visible for w in fake.windows.values())
    assert (geometry.width, geometry.height) not in [(w, h) for _, w, h in fake.bitmaps.values()]


def test_o5_the_painters_sprites_are_cleared_when_the_keyboard_layer_hides_and_when_it_closes(
    fake: FakeWin32, painted: StubPainter
) -> None:
    overlay = win.WindowsOverlay(api=fake)
    overlay.start()
    show(overlay, "point")
    assert painted.cleared == 0  # a reticle-only run has nothing to forget
    show_keyboard(overlay)
    assert painted.cleared == 0
    show(overlay, "point")
    assert painted.cleared == 1
    show(overlay, "point", 600, 600)
    assert painted.cleared == 1  # once, not on every reticle frame
    show_keyboard(overlay, seq=9)
    overlay.close()
    assert painted.cleared == 2
    overlay.close()
    assert painted.cleared == 2


def test_o5_the_one_second_timer_keeps_the_visible_keyboard_on_top(
    fake: FakeWin32, started: win.WindowsOverlay, painted: StubPainter
) -> None:
    main, helper = started.hwnds
    keyboard = started.keyboard_hwnd
    assert keyboard is not None
    show(started, "point")
    show_keyboard(started)
    before = {h: fake.windows[h].topmost_raises for h in (main, helper, keyboard)}
    fake.PostMessageW(main, win.WM_TIMER, 1, 0)
    assert wait_for(lambda: fake.windows[keyboard].topmost_raises == before[keyboard] + 1)
    assert fake.windows[main].topmost_raises == before[main]  # hidden: left alone
    assert fake.windows[helper].topmost_raises == before[helper]


def test_o5_a_hidden_keyboard_is_not_raised_by_the_timer(fake: FakeWin32, started: win.WindowsOverlay) -> None:
    main, _ = started.hwnds
    keyboard = started.keyboard_hwnd
    assert keyboard is not None
    show(started, "point")
    fake.PostMessageW(main, win.WM_TIMER, 1, 0)
    fake.PostMessageW(main, win.WM_TIMER, 1, 0)
    assert wait_for(lambda: fake.windows[main].topmost_raises >= 2)
    assert fake.windows[keyboard].topmost_raises == 0


@pytest.mark.parametrize("message", [win.WM_DISPLAYCHANGE, win.WM_DPICHANGED, win.WM_SETTINGCHANGE])
def test_o5_a_display_change_pushes_the_keyboard_again(
    fake: FakeWin32, started: win.WindowsOverlay, painted: StubPainter, message: int
) -> None:
    main, _ = started.hwnds
    assert started.keyboard_hwnd is not None
    window = fake.windows[started.keyboard_hwnd]
    show_keyboard(started)
    assert window.updates == 1
    fake.PostMessageW(main, message, 32, 0)
    assert wait_for(lambda: window.updates == 2)  # the same image, shown again: Windows may have dropped it


def test_o5_a_dpi_change_redraws_the_keyboard_at_the_new_size(
    fake: FakeWin32, started: win.WindowsOverlay, painted: StubPainter
) -> None:
    main, _ = started.hwnds
    assert started.keyboard_hwnd is not None
    window = fake.windows[started.keyboard_hwnd]
    show_keyboard(started)
    fake.monitors[0].dpi = 192
    fake.PostMessageW(main, win.WM_DPICHANGED, 0, 0)
    geometry, origin = kr.keyboard_geometry(WORK, 1.0, 192, "top", "direct")
    assert wait_for(lambda: window.rect == (*origin, geometry.width, geometry.height))
    assert fake.problems == []


def test_o5_close_destroys_all_three_windows_and_frees_the_keyboard_surface(
    fake: FakeWin32, painted: StubPainter
) -> None:
    overlay = win.WindowsOverlay(api=fake)
    overlay.start()
    show(overlay, "resize", helper=Point(900, 500))
    show_keyboard(overlay)
    thread = overlay._ui.thread  # type: ignore[union-attr]
    overlay.close()
    assert not thread.is_alive()
    assert fake.windows == {} and fake.classes == {} and fake.bitmaps == {} and fake.dcs == {}
    assert fake.screen_dcs == 0 and len(fake.named("DestroyWindow")) == 3
    assert overlay.hwnds == () and overlay.keyboard_hwnd is None
    assert fake.problems == []


def test_o5_a_failed_keyboard_push_is_retried_and_the_window_shows_after_it(
    fake: FakeWin32, started: win.WindowsOverlay, painted: StubPainter
) -> None:
    assert started.keyboard_hwnd is not None
    window = fake.windows[started.keyboard_hwnd]
    fake.fail.add("UpdateLayeredWindow")
    show_keyboard(started, seq=1)
    assert not window.visible and started.draw_error is not None and "UpdateLayeredWindow" in started.draw_error
    fake.fail.clear()
    show_keyboard(started, seq=2)  # equal apart from seq: the same image, which never reached the screen
    assert window.visible and window.updates == 1


@needs_font
def test_o5_the_real_painters_frame_reaches_the_window_unchanged(fake: FakeWin32, started: win.WindowsOverlay) -> None:
    assert started.keyboard_hwnd is not None
    view = kb_view(strip="Typing into Notepad   EN", echo="hello")
    started.show(OverlayState(keyboard=view))
    assert started.wait_drawn(2.0) and started.draw_error is None
    geometry, origin = kr.keyboard_geometry(WORK, 1.0, 96, "top", "direct")
    expected = kr.compose(kr.bake_base("en", False, geometry, "direct"), geometry, view)
    window = fake.windows[started.keyboard_hwnd]
    assert window.rect == (*origin, 660, geometry.height)
    assert np.array_equal(window.image, expected)  # premultiplied BGRA goes through the DIB as it is
    assert expected[..., 3].any()


@needs_font
def test_o5_a_real_frame_in_either_layout_fits_its_window(fake: FakeWin32, started: win.WindowsOverlay) -> None:
    assert started.keyboard_hwnd is not None
    for commit in ("direct", "review"):
        view = kb_view(commit=commit, strip="Review")
        if commit == "review":
            box = ComposeView("abc", 3, "composing", 0, None, 0, 0, 0.0, False, False)
            view = replace(view, compose=box)
        started.show(OverlayState(keyboard=view))
        assert started.wait_drawn(2.0) and started.draw_error is None, commit
        geometry = kr.keyboard_geometry(WORK, 1.0, 96, "top", commit)[0]  # type: ignore[arg-type]
        assert fake.windows[started.keyboard_hwnd].rect[2:] == (geometry.width, geometry.height)
    assert fake.problems == []


# --------------------------------------------------------------------------- everywhere: health (O6)


def test_o6_an_overlay_that_is_not_started_says_it_is_not_alive() -> None:
    overlay = win.WindowsOverlay(api=FakeWin32())
    assert overlay.health() == OverlayHealth(alive=False, failures=0, ok_age_s=None, keyboard_ok=False, draw_ms=None)


def test_o6_a_started_overlay_that_has_not_drawn_yet(
    fake: FakeWin32, started: win.WindowsOverlay, painted: StubPainter
) -> None:
    assert started.health() == OverlayHealth(alive=True, failures=0, ok_age_s=None, keyboard_ok=True, draw_ms=None)


def test_o6_ok_age_counts_from_the_last_good_draw_on_the_helper_clock(
    fake: FakeWin32, started: win.WindowsOverlay, painted: StubPainter, ticking: FakeClock
) -> None:
    show(started, "point", 500, 400)
    assert started.health().ok_age_s == 0.0
    ticking.t += 0.3
    assert started.health().ok_age_s == pytest.approx(0.3)
    show(started, "point", 500, 400)  # nothing changed: the early return counts as a good draw
    assert started.health().ok_age_s == 0.0
    ticking.t += 2.0
    show(started, "hidden")  # hiding is a good draw too
    assert started.health().ok_age_s == 0.0
    ticking.t -= 10.0  # a clock that stepped back never makes a negative age
    assert started.health().ok_age_s == 0.0


def test_o6_a_swallowed_update_failure_raises_failures_and_a_good_draw_clears_them(
    fake: FakeWin32, started: win.WindowsOverlay, painted: StubPainter, ticking: FakeClock
) -> None:
    show_keyboard(started, progress=0.1)
    assert started.health().failures == 0
    ticking.t += 2.0
    fake.fail.add("UpdateLayeredWindow")
    show_keyboard(started, progress=0.2)
    assert started.health().failures == 1
    show_keyboard(started, progress=0.3)
    health = started.health()
    assert health.failures == 2 and health.alive
    assert health.ok_age_s == pytest.approx(2.0)  # still counted from the last good draw
    fake.fail.clear()
    show_keyboard(started, progress=0.4)
    health = started.health()
    assert health.failures == 0 and health.ok_age_s == 0.0
    assert started.draw_error is not None  # the first failure's text stays, as before


def test_o6_the_reticles_failures_count_the_same_way(
    fake: FakeWin32, started: win.WindowsOverlay, ticking: FakeClock
) -> None:
    fake.fail.add("UpdateLayeredWindow")
    show(started, "point", 100, 100)
    assert started.health().failures == 1 and started.health().ok_age_s is None


def test_o6_a_wake_up_that_cannot_be_posted_counts_as_a_failure(fake: FakeWin32, started: win.WindowsOverlay) -> None:
    fake.fail.add("PostMessageW")
    started.show(OverlayState("point", Point(10, 10)))
    assert started.health().failures == 1
    fake.fail.clear()
    show(started, "point", 600, 600)
    assert started.health().failures == 0


def test_o6_alive_ends_when_the_windows_are_gone_even_before_close(fake: FakeWin32, painted: StubPainter) -> None:
    overlay = win.WindowsOverlay(api=fake)
    overlay.start()
    main, _ = overlay.hwnds
    assert overlay.health().alive
    fake.PostMessageW(main, win.WM_CLOSE, 0, 0)  # destroyed behind the overlay's back
    assert wait_for(lambda: not overlay.health().alive)
    overlay.close()
    assert not overlay.health().alive


def test_o6_a_live_thread_without_windows_is_not_alive(fake: FakeWin32, started: win.WindowsOverlay) -> None:
    ui = started._ui
    assert ui is not None and started.health().alive
    saved, ui._windows = ui._windows, []  # the thread still runs, but there is nothing to draw on
    try:
        assert ui.thread.is_alive() and not started.health().alive
    finally:
        ui._windows = saved


def test_o6_keyboard_ok_needs_a_font(fake: FakeWin32, started: win.WindowsOverlay, painted: StubPainter) -> None:
    assert started.health().keyboard_ok
    painted.font = False
    health = started.health()
    assert not health.keyboard_ok and health.alive  # the reticle still works; only the keyboard is refused
    painted.font = True
    assert started.health().keyboard_ok


def test_o6_keyboard_ok_is_false_when_the_painter_cannot_be_imported(
    fake: FakeWin32, started: win.WindowsOverlay, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken() -> bool:
        raise ImportError("no Pillow")

    monkeypatch.setattr(kr, "font_available", broken)
    health = started.health()
    assert health.alive and not health.keyboard_ok  # and the probe itself did not raise


def test_o6_draw_ms_is_the_median_of_the_last_hundred_draws_that_pushed_an_image(
    fake: FakeWin32, started: win.WindowsOverlay, painted: StubPainter, ticking: FakeClock
) -> None:
    painted.clock = ticking
    costs = [0.001 * (i + 1) for i in range(100)]
    for i, cost in enumerate(costs):
        assert started.health().draw_ms is None  # until the hundredth
        painted.cost = cost
        show_keyboard(started, seq=i, progress=(i + 1) / 1000)
    assert started.health().draw_ms == pytest.approx(statistics.median(costs) * 1000)
    painted.cost = 0.2
    show_keyboard(started, seq=100, progress=0.5)
    window = [*costs[1:], 0.2]  # the oldest fell out
    assert started.health().draw_ms == pytest.approx(statistics.median(window) * 1000)
    median = started.health().draw_ms
    # draws that push nothing (an unchanged image, a hide) say nothing about the cost of a push: sixty of each would
    # empty the window of real samples if they were counted
    for seq in range(101, 161):
        show_keyboard(started, seq=seq, progress=0.5)
    assert started.health().draw_ms == median
    for _ in range(60):
        show(started, "hidden")
    assert started.health().draw_ms == median
    # nor do failed ones
    fake.fail.add("UpdateLayeredWindow")
    painted.cost = 5.0
    show_keyboard(started, seq=102, progress=0.6)
    assert started.health().draw_ms == median and started.health().failures == 1


def test_o6_every_time_the_overlay_compares_comes_from_the_helper_clock() -> None:
    source = Path(win.__file__).read_text(encoding="utf-8")
    assert re.search(r"\btime\.(monotonic|perf_counter|time|process_time)\b", source) is None
    assert "clock.now()" in source


# --------------------------------------------------------------------------- everywhere: capture exclusion (O7)


def affinity_calls(fake: FakeWin32) -> list[tuple[Any, ...]]:
    return [c[1:] for c in fake.named("SetWindowDisplayAffinity")]


def test_o7_the_keyboard_is_excluded_from_capture_when_asked_and_not_before_it_shows(
    fake: FakeWin32, started: win.WindowsOverlay, painted: StubPainter, caplog: pytest.LogCaptureFixture
) -> None:
    keyboard = started.keyboard_hwnd
    assert keyboard is not None
    caplog.set_level(logging.DEBUG, logger="jarvis_hands.overlay.windows")
    show_keyboard(started)
    assert affinity_calls(fake) == []  # never asked: never called
    show_keyboard(started, exclude_capture=True)
    assert affinity_calls(fake) == [(keyboard, 0x11)]
    assert fake.windows[keyboard].affinity == 0x11 and started.capture_excluded is True
    show_keyboard(started, exclude_capture=True, progress=0.5)
    show_keyboard(started, exclude_capture=True, progress=0.6)
    assert affinity_calls(fake) == [(keyboard, 0x11)]  # once per change, not once per frame
    show_keyboard(started, exclude_capture=False, progress=0.7)
    assert affinity_calls(fake) == [(keyboard, 0x11), (keyboard, 0)]
    assert fake.windows[keyboard].affinity == 0 and started.capture_excluded is False
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []  # nothing to complain about


def test_o7_the_exclusion_is_in_place_before_the_first_frame_is_pushed(
    fake: FakeWin32, started: win.WindowsOverlay, painted: StubPainter
) -> None:
    keyboard = started.keyboard_hwnd
    show_keyboard(started, exclude_capture=True)
    names = [(c[0], c[1]) for c in fake.calls if c[1:2] == (keyboard,)]
    assert names.index(("SetWindowDisplayAffinity", keyboard)) < names.index(("UpdateLayeredWindow", keyboard))
    shown = next(i for i, c in enumerate(fake.calls) if c[0] == "SetWindowPos" and c[1] == keyboard)
    asked = next(i for i, c in enumerate(fake.calls) if c[0] == "SetWindowDisplayAffinity")
    assert asked < shown  # a frame of private text is never visible to a capture


def test_o7_only_the_keyboard_window_is_touched(
    fake: FakeWin32, started: win.WindowsOverlay, painted: StubPainter
) -> None:
    show(started, "resize", helper=Point(900, 500))
    show_keyboard(started, exclude_capture=True)
    show(started, "point")
    show_keyboard(started, exclude_capture=False)
    assert {hwnd for hwnd, _ in affinity_calls(fake)} == {started.keyboard_hwnd}


def test_o7_a_refusal_is_logged_once_and_never_stops_the_keyboard(
    fake: FakeWin32, started: win.WindowsOverlay, painted: StubPainter, caplog: pytest.LogCaptureFixture
) -> None:
    fake.fail.add("SetWindowDisplayAffinity")
    with caplog.at_level(logging.DEBUG, logger="jarvis_hands.overlay.windows"):
        show_keyboard(started, exclude_capture=True, progress=0.1)
        show_keyboard(started, exclude_capture=False, progress=0.2)
        show_keyboard(started, exclude_capture=True, progress=0.3)
    assert started.keyboard_hwnd is not None and fake.windows[started.keyboard_hwnd].visible
    assert started.draw_error is None and started.health().failures == 0  # best effort: not a failed draw
    assert started.capture_excluded is False  # and it says so
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1 and "capture" in warnings[0].getMessage()


def test_o7_the_exclusion_can_succeed_after_a_refusal(
    fake: FakeWin32, started: win.WindowsOverlay, painted: StubPainter
) -> None:
    fake.fail.add("SetWindowDisplayAffinity")
    show_keyboard(started, exclude_capture=True, progress=0.1)
    fake.fail.clear()
    show_keyboard(started, exclude_capture=False, progress=0.2)
    show_keyboard(started, exclude_capture=True, progress=0.3)
    assert started.capture_excluded is True


# --------------------------------------------------------------------------- everywhere: exception text (F4, S22b)

SENTINEL = "Zq9-sentinel-text-Zq9"


def test_the_exception_text_of_a_failed_draw_goes_through_logs_exc_text_when_it_exists(
    fake: FakeWin32, started: win.WindowsOverlay, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[bool] = []

    def exc_text(exc: BaseException, *, typed: bool = True) -> str:
        calls.append(typed)
        return f"<{type(exc).__name__}>"

    monkeypatch.setattr(logs, "exc_text", exc_text, raising=False)
    fake.fail.add("UpdateLayeredWindow")
    show(started, "point", 100, 100)
    assert started.draw_error == "drawing the reticle failed: <OSError>"
    assert calls == [False]  # an exception that is never the typed text itself


def test_the_exception_text_of_a_failed_message_handler_goes_through_logs_exc_text_too(
    fake: FakeWin32, started: win.WindowsOverlay, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(logs, "exc_text", lambda exc, *, typed=True: f"<{type(exc).__name__}>", raising=False)

    def broken(self: Any) -> None:
        raise RuntimeError(SENTINEL)

    monkeypatch.setattr(win._Ui, "_on_wake", broken)
    started.show(OverlayState("point", Point(5, 5)))
    assert wait_for(lambda: started.draw_error is not None)
    assert win.WM_OVERLAY_WAKE == 0x8001
    assert started.draw_error == "handling message 0x8001 failed: <RuntimeError>"
    assert SENTINEL not in started.draw_error


def test_without_logs_exc_text_the_text_is_what_it_always_was(
    fake: FakeWin32, started: win.WindowsOverlay, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delattr(logs, "exc_text", raising=False)
    fake.fail.add("UpdateLayeredWindow")
    show(started, "point", 100, 100)
    assert started.draw_error is not None and started.draw_error.startswith("drawing the reticle failed: ")
    assert "UpdateLayeredWindow failed with Windows error" in started.draw_error


def test_s22b_a_draw_hook_that_raises_the_sentinel_never_puts_it_in_a_log_or_an_error(
    fake: FakeWin32, started: win.WindowsOverlay, painted: StubPainter, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    def boom(*args: Any) -> Any:
        try:
            raise OSError(SENTINEL)
        except OSError as inner:
            raise RuntimeError(f"bad stroke {SENTINEL!r}") from inner

    monkeypatch.setattr(kr, "compose", boom)
    with caplog.at_level(logging.DEBUG, logger="jarvis_hands.overlay.windows"):
        show_keyboard(started)
        show_keyboard(started, progress=0.5)
    assert started.draw_error == "drawing the keyboard failed: internal"  # the fixed code, not what was raised
    assert started.keyboard_error == "internal"
    logged = caplog.text + "".join(r.getMessage() + str(r.exc_text) + str(r.args) for r in caplog.records)
    assert SENTINEL not in logged and SENTINEL not in str(started.draw_error)
    assert started.health().failures == 2


def test_a_keyboard_draw_error_is_recorded_by_its_code(
    fake: FakeWin32, started: win.WindowsOverlay, painted: StubPainter, monkeypatch: pytest.MonkeyPatch
) -> None:
    good = painted.compose

    def refuse(*args: Any) -> Any:
        raise kr.KeyboardDrawError("font")

    monkeypatch.setattr(kr, "compose", refuse)
    assert started.keyboard_error is None
    show_keyboard(started)
    assert started.keyboard_error == "font" and started.draw_error is not None and "font" in started.draw_error
    monkeypatch.setattr(kr, "compose", good)
    show_keyboard(started, progress=0.5)
    assert started.keyboard_error is None  # the next good frame clears it
    assert started.health().failures == 0


def test_a_keyboard_state_without_a_work_area_is_a_size_error_not_a_crash(
    fake: FakeWin32, started: win.WindowsOverlay, painted: StubPainter
) -> None:
    show_keyboard(started, work=(0, 0, 0, 0))
    assert started.keyboard_error == "size" and started.health().failures == 1
    assert started.keyboard_hwnd is not None and not fake.windows[started.keyboard_hwnd].visible


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
    assert real.keyboard_hwnd is not None
    for hwnd in (*real.hwnds, real.keyboard_hwnd):  # the keyboard's window is one of them
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
@needs_font
def test_real_keyboard_layer_shows_at_its_geometry_and_lets_clicks_through(
    desktop: None, real: win.WindowsOverlay
) -> None:
    left, top, right, bottom = _monitor_rects()[0]
    work = (left, top, right - left, bottom - top)
    keyboard = real.keyboard_hwnd
    assert keyboard is not None
    real.show(OverlayState(keyboard=kb_view(work=work, strip="Typing into Notepad   EN")))
    assert real.wait_drawn(2.0) and real.draw_error is None and real.keyboard_error is None
    user32 = win._win32().user32
    assert user32.IsWindowVisible(keyboard) and not any(user32.IsWindowVisible(h) for h in real.hwnds)
    rect = win.RECT()
    assert user32.GetWindowRect(keyboard, ctypes.byref(rect))
    assert (rect.right - rect.left) > (rect.bottom - rect.top) > 0  # wide, not the reticle's square
    assert user32.GetForegroundWindow() != keyboard
    # the middle of the layer is a click-through window: the click reaches what is under the keyboard
    middle = win.POINT((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
    assert user32.WindowFromPoint(middle) not in (keyboard, *real.hwnds)
    health = real.health()
    assert health.alive and health.keyboard_ok and health.failures == 0
    real.show(OverlayState())
    assert real.wait_drawn(2.0) and not user32.IsWindowVisible(keyboard)


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
