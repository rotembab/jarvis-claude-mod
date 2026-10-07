"""The reticle on Windows: two click-through layered windows, drawn from ``render.py`` on their own thread.

Layered windows (``UpdateLayeredWindow`` with per-pixel alpha) show the soft,
anti-aliased reticle over whatever is under it. The extended styles do the
rest: ``WS_EX_LAYERED | WS_EX_TRANSPARENT`` lets every click through to the
window below, ``WS_EX_NOACTIVATE`` (and showing only with ``SWP_NOACTIVATE``)
means it never takes focus, ``WS_EX_TOOLWINDOW`` keeps it out of the taskbar
and Alt+Tab, and ``WS_EX_TOPMOST`` keeps it above normal windows. Topmost is
re-asserted every second while it shows, since a window that became topmost
later would cover it. (Nothing we can do puts it above Start, Search or
Alt+Tab: those live in higher window bands.)

A window belongs to the thread that created it and its messages are
dispatched there, so one thread creates both windows and runs their message
loop. ``show()`` only stores the newest state and posts a wake-up, at most one
in flight: the camera loop never waits on drawing, and a slow frame drops
states instead of queueing them. The UI thread draws the newest state only.

The thread is per-monitor DPI aware (v2), so every coordinate is in physical
pixels, like the rest of the helper's, and the reticle is sized for the DPI of
the monitor under it (96 px at 100 %, 144 px at 150 %). The second window is
the helper hand's marker during a two-hand resize.

ctypes: private ``WinDLL`` handles with ``use_last_error``, a prototype on
every function (handles, WPARAM and LPARAM are 64-bit; the default ``int``
would truncate them), structures from fixed-width fields that match the Win64
layouts (so their sizes are tested on any OS) and the window procedure kept
referenced for as long as the windows exist (a collected callback crashes the
process).

``start()`` raises ``OverlayError`` when the windows cannot be made. After
that nothing here raises into the caller: a failing draw is logged once and
the next state tries again, and ``close()`` never raises.
"""

from __future__ import annotations

import ctypes
import itertools
import logging
import math
import os
import sys
import threading
from typing import Any

import numpy as np

from ..geometry import Point
from .base import OverlayError, OverlayState
from .render import helper_size, render, render_helper, reticle_size, top_left

log = logging.getLogger(__name__)

#: Seconds ``start()`` waits for the UI thread to report its windows.
START_TIMEOUT_S = 3.0
#: Seconds ``close()`` waits for the UI thread to finish.
STOP_TIMEOUT_S = 2.0
#: How often topmost is re-asserted (and the per-monitor DPI re-read) while the reticle shows.
TOPMOST_INTERVAL_MS = 1000

# --------------------------------------------------------------------------- Win32 types and constants

# Fixed widths, as on Win64 (LONG and DWORD are 32-bit there; handles, WPARAM, LPARAM pointer-sized).
_HANDLE = ctypes.c_void_p
_UINT = ctypes.c_uint32
_DWORD = ctypes.c_uint32
_BOOL = ctypes.c_int32
_INT = ctypes.c_int32
_LONG = ctypes.c_int32
_WORD = ctypes.c_uint16
_BYTE = ctypes.c_uint8
_ATOM = ctypes.c_uint16
_HRESULT = ctypes.c_int32
_WPARAM = ctypes.c_size_t
_LPARAM = ctypes.c_ssize_t
_LRESULT = ctypes.c_ssize_t
_UINT_PTR = ctypes.c_size_t
_LPCWSTR = ctypes.c_wchar_p

WS_POPUP = 0x80000000
WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
OVERLAY_EX_STYLE = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
GWL_EXSTYLE = -20

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_SETTINGCHANGE = 0x001A
WM_MOUSEACTIVATE = 0x0021
WM_DISPLAYCHANGE = 0x007E
WM_TIMER = 0x0113
WM_DPICHANGED = 0x02E0
WM_APP = 0x8000
#: Posted to the main window by ``show()``: draw the newest state.
WM_OVERLAY_WAKE = WM_APP + 1
#: Posted to the UI thread when its window can no longer be posted to: destroy the windows and quit.
WM_OVERLAY_STOP = WM_APP + 2
MA_NOACTIVATE = 3

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
SWP_HIDEWINDOW = 0x0080
HWND_TOPMOST = -1

ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
BI_RGB = 0
DIB_RGB_COLORS = 0
MONITOR_DEFAULTTONEAREST = 2
MDT_EFFECTIVE_DPI = 0
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
ERROR_CLASS_ALREADY_EXISTS = 1410
_TIMER_ID = 1


class POINT(ctypes.Structure):
    _fields_ = (("x", _LONG), ("y", _LONG))


class SIZE(ctypes.Structure):
    _fields_ = (("cx", _LONG), ("cy", _LONG))


class RECT(ctypes.Structure):
    _fields_ = (("left", _LONG), ("top", _LONG), ("right", _LONG), ("bottom", _LONG))


class MSG(ctypes.Structure):
    _fields_ = (
        ("hwnd", _HANDLE),
        ("message", _UINT),
        ("wParam", _WPARAM),
        ("lParam", _LPARAM),
        ("time", _DWORD),
        ("pt", POINT),
        ("lPrivate", _DWORD),
    )


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = (
        ("cbSize", _UINT),
        ("style", _UINT),
        # A WNDPROC; declared as a plain pointer so this layout exists (and is tested) off Windows too.
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", _INT),
        ("cbWndExtra", _INT),
        ("hInstance", _HANDLE),
        ("hIcon", _HANDLE),
        ("hCursor", _HANDLE),
        ("hbrBackground", _HANDLE),
        ("lpszMenuName", _LPCWSTR),
        ("lpszClassName", _LPCWSTR),
        ("hIconSm", _HANDLE),
    )


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = (
        ("biSize", _DWORD),
        ("biWidth", _LONG),
        ("biHeight", _LONG),
        ("biPlanes", _WORD),
        ("biBitCount", _WORD),
        ("biCompression", _DWORD),
        ("biSizeImage", _DWORD),
        ("biXPelsPerMeter", _LONG),
        ("biYPelsPerMeter", _LONG),
        ("biClrUsed", _DWORD),
        ("biClrImportant", _DWORD),
    )


class BITMAPINFO(ctypes.Structure):
    _fields_ = (("bmiHeader", BITMAPINFOHEADER), ("bmiColors", _DWORD * 1))


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = (
        ("BlendOp", _BYTE),
        ("BlendFlags", _BYTE),
        ("SourceConstantAlpha", _BYTE),
        ("AlphaFormat", _BYTE),
    )


def _proto(fn: Any, restype: Any, *argtypes: Any) -> Any:
    fn.restype = restype
    fn.argtypes = list(argtypes)
    return fn


class _Api:
    """user32, gdi32, kernel32 and shcore with a prototype on every function this module calls (Windows only)."""

    def __init__(self) -> None:
        win_dll = ctypes.WinDLL  # type: ignore[attr-defined]
        self.user32 = user32 = win_dll("user32", use_last_error=True)
        self.gdi32 = gdi32 = win_dll("gdi32", use_last_error=True)
        self.kernel32 = kernel32 = win_dll("kernel32", use_last_error=True)
        try:
            self.shcore: Any = win_dll("shcore", use_last_error=True)
        except OSError:
            self.shcore = None  # before Windows 8.1: every monitor counts as 96 DPI
        self.WNDPROC = ctypes.WINFUNCTYPE(_LRESULT, _HANDLE, _UINT, _WPARAM, _LPARAM)  # type: ignore[attr-defined]

        _proto(user32.RegisterClassExW, _ATOM, ctypes.POINTER(WNDCLASSEXW))
        _proto(user32.UnregisterClassW, _BOOL, _LPCWSTR, _HANDLE)
        _proto(
            user32.CreateWindowExW,
            _HANDLE,
            _DWORD,  # dwExStyle
            _LPCWSTR,  # lpClassName
            _LPCWSTR,  # lpWindowName
            _DWORD,  # dwStyle
            _INT,  # X
            _INT,  # Y
            _INT,  # nWidth
            _INT,  # nHeight
            _HANDLE,  # hWndParent
            _HANDLE,  # hMenu
            _HANDLE,  # hInstance
            ctypes.c_void_p,  # lpParam
        )
        _proto(user32.DestroyWindow, _BOOL, _HANDLE)
        _proto(user32.DefWindowProcW, _LRESULT, _HANDLE, _UINT, _WPARAM, _LPARAM)
        _proto(user32.GetMessageW, _BOOL, ctypes.POINTER(MSG), _HANDLE, _UINT, _UINT)
        _proto(user32.DispatchMessageW, _LRESULT, ctypes.POINTER(MSG))
        _proto(user32.PostMessageW, _BOOL, _HANDLE, _UINT, _WPARAM, _LPARAM)
        _proto(user32.PostThreadMessageW, _BOOL, _DWORD, _UINT, _WPARAM, _LPARAM)
        _proto(user32.PostQuitMessage, None, _INT)
        _proto(user32.SetWindowPos, _BOOL, _HANDLE, _HANDLE, _INT, _INT, _INT, _INT, _UINT)
        _proto(
            user32.UpdateLayeredWindow,
            _BOOL,
            _HANDLE,  # hWnd
            _HANDLE,  # hdcDst
            ctypes.POINTER(POINT),  # pptDst
            ctypes.POINTER(SIZE),  # psize
            _HANDLE,  # hdcSrc
            ctypes.POINTER(POINT),  # pptSrc
            _DWORD,  # crKey (COLORREF)
            ctypes.POINTER(BLENDFUNCTION),  # pblend
            _DWORD,  # dwFlags
        )
        _proto(user32.GetDC, _HANDLE, _HANDLE)
        _proto(user32.ReleaseDC, _INT, _HANDLE, _HANDLE)
        _proto(user32.MonitorFromPoint, _HANDLE, POINT, _DWORD)
        _proto(user32.SetTimer, _UINT_PTR, _HANDLE, _UINT_PTR, _UINT, ctypes.c_void_p)
        # Not called here: for tests and diagnostics (is it shown, where, is it foreground, what a click hits).
        _proto(user32.IsWindowVisible, _BOOL, _HANDLE)
        _proto(user32.GetWindowRect, _BOOL, _HANDLE, ctypes.POINTER(RECT))
        _proto(user32.GetForegroundWindow, _HANDLE)
        _proto(user32.WindowFromPoint, _HANDLE, POINT)
        # Windows 10 1607+ and 64-bit only (on 32-bit Windows these are macros); optional here.
        self.SetThreadDpiAwarenessContext = _optional(
            user32, "SetThreadDpiAwarenessContext", ctypes.c_void_p, ctypes.c_void_p
        )
        self.GetWindowLongPtrW = _optional(user32, "GetWindowLongPtrW", ctypes.c_ssize_t, _HANDLE, _INT)

        _proto(gdi32.CreateCompatibleDC, _HANDLE, _HANDLE)
        _proto(
            gdi32.CreateDIBSection,
            _HANDLE,
            _HANDLE,  # hdc
            ctypes.POINTER(BITMAPINFO),
            _UINT,  # usage
            ctypes.POINTER(ctypes.c_void_p),  # ppvBits
            _HANDLE,  # hSection
            _DWORD,  # offset
        )
        _proto(gdi32.SelectObject, _HANDLE, _HANDLE, _HANDLE)
        _proto(gdi32.DeleteObject, _BOOL, _HANDLE)
        _proto(gdi32.DeleteDC, _BOOL, _HANDLE)
        _proto(gdi32.GdiFlush, _BOOL)

        _proto(kernel32.GetModuleHandleW, _HANDLE, _LPCWSTR)
        _proto(kernel32.GetCurrentThreadId, _DWORD)

        self.GetDpiForMonitor = None
        if self.shcore is not None:
            self.GetDpiForMonitor = _optional(
                self.shcore, "GetDpiForMonitor", _HRESULT, _HANDLE, _INT, ctypes.POINTER(_UINT), ctypes.POINTER(_UINT)
            )


def _optional(dll: Any, name: str, restype: Any, *argtypes: Any) -> Any:
    try:
        fn = getattr(dll, name)
    except AttributeError:
        return None
    return _proto(fn, restype, *argtypes)


_api_lock = threading.Lock()
_api: _Api | None = None


def _win32() -> _Api:
    global _api
    with _api_lock:
        if _api is None:
            _api = _Api()
        return _api


def _last_error() -> int:
    """The calling thread's last Win32 error as ctypes saved it (0 off Windows, where tests fake the API)."""
    get = getattr(ctypes, "get_last_error", None)
    return int(get()) if get is not None else 0


def _fail(what: str) -> OSError:
    err = _last_error()
    return OSError(err, f"{what} failed with Windows error {err}")


def _finite(p: Point) -> bool:
    return math.isfinite(p.x) and math.isfinite(p.y)


# --------------------------------------------------------------------------- the newest state (pure)


class Mailbox:
    """The newest state for the UI thread, with at most one wake-up in flight.

    ``put`` says whether the caller must post a wake-up: only when none is
    pending, so a burst of states costs one message and the UI thread then
    draws the newest. ``take`` (on the UI thread) clears the pending flag
    before drawing, so a state that arrives during the draw posts again.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._state = OverlayState()
        self._submitted = 0
        self._drawn = 0
        self._wake_pending = False
        self._closed = False

    @property
    def state(self) -> OverlayState:
        with self._cond:
            return self._state

    def put(self, state: OverlayState) -> bool:
        with self._cond:
            if self._closed:
                return False
            self._state = state
            self._submitted += 1
            if self._wake_pending:
                return False
            self._wake_pending = True
            return True

    def wake_failed(self) -> None:
        """The wake-up could not be posted: let the next ``put`` try again."""
        with self._cond:
            self._wake_pending = False

    def take(self) -> tuple[OverlayState, int]:
        with self._cond:
            self._wake_pending = False
            return self._state, self._submitted

    def drawn(self, seq: int) -> None:
        with self._cond:
            self._drawn = max(self._drawn, seq)
            self._cond.notify_all()

    def wait_drawn(self, timeout: float) -> bool:
        """True once every state put so far has been drawn (or superseded by one that was)."""
        with self._cond:
            self._cond.wait_for(lambda: self._closed or self._drawn >= self._submitted, timeout)
            return self._drawn >= self._submitted

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()


# --------------------------------------------------------------------------- GDI surfaces and windows


class _Surface:
    """A top-down 32-bit DIB section selected into a memory DC; ``pixels`` is its memory as (size, size, 4)."""

    def __init__(self, api: _Api, size: int) -> None:
        self.api = api
        self.size = size
        self.memdc: int | None = None
        self.bitmap: int | None = None
        self.previous: int | None = None
        self.pixels: np.ndarray | None = None
        user32, gdi32 = api.user32, api.gdi32
        screen = user32.GetDC(None)
        if not screen:
            raise _fail("GetDC")
        try:
            self.memdc = gdi32.CreateCompatibleDC(screen)
            if not self.memdc:
                raise _fail("CreateCompatibleDC")
            info = BITMAPINFO()
            header = info.bmiHeader
            header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            header.biWidth = size
            header.biHeight = -size  # negative: rows run top-down, like the numpy image
            header.biPlanes = 1
            header.biBitCount = 32
            header.biCompression = BI_RGB
            bits = ctypes.c_void_p()
            self.bitmap = gdi32.CreateDIBSection(
                screen, ctypes.byref(info), DIB_RGB_COLORS, ctypes.byref(bits), None, 0
            )
            if not self.bitmap or not bits.value:
                raise _fail("CreateDIBSection")
            self.previous = gdi32.SelectObject(self.memdc, self.bitmap)
            if not self.previous:
                raise _fail("SelectObject")
            buffer = (ctypes.c_uint8 * (size * size * 4)).from_address(bits.value)
            self.pixels = np.ctypeslib.as_array(buffer).reshape(size, size, 4)
        except BaseException:
            self.destroy()
            raise
        finally:
            user32.ReleaseDC(None, screen)

    def destroy(self) -> None:
        """Never raises; safe to call twice."""
        gdi32 = self.api.gdi32
        self.pixels = None  # the view points into the DIB's memory: drop it before the DIB goes
        if self.memdc and self.previous:
            gdi32.SelectObject(self.memdc, self.previous)
        self.previous = None
        if self.bitmap and not gdi32.DeleteObject(self.bitmap):
            log.debug("DeleteObject(DIB) failed with Windows error %d", _last_error())
        self.bitmap = None
        if self.memdc and not gdi32.DeleteDC(self.memdc):
            log.debug("DeleteDC failed with Windows error %d", _last_error())
        self.memdc = None


class _Layer:
    """One layered window: its surface, the image it shows and where (all touched on the UI thread only)."""

    def __init__(self, api: _Api, hwnd: int) -> None:
        self.api = api
        self.hwnd = hwnd
        self.surface: _Surface | None = None
        self.image: np.ndarray | None = None
        self.origin: tuple[int, int] | None = None
        self.visible = False

    def show(self, image: np.ndarray, origin: tuple[int, int]) -> None:
        size = image.shape[0]
        if self.surface is None or self.surface.size != size:
            self.release_surface()
            self.surface = _Surface(self.api, size)
        if image is self.image and origin == self.origin and self.visible:
            return
        if image is not self.image:
            assert self.surface.pixels is not None
            self.api.gdi32.GdiFlush()  # no GDI drawing may be pending on the DIB while we write it
            np.copyto(self.surface.pixels, image)
            self.image = image
        self.origin = None  # until the update below succeeds
        dst, src = POINT(*origin), POINT(0, 0)
        extent = SIZE(size, size)
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        user32 = self.api.user32
        screen = user32.GetDC(None)
        try:
            ok = user32.UpdateLayeredWindow(
                self.hwnd,
                screen,
                ctypes.byref(dst),
                ctypes.byref(extent),
                self.surface.memdc,
                ctypes.byref(src),
                0,
                ctypes.byref(blend),
                ULW_ALPHA,
            )
            if not ok:
                raise _fail("UpdateLayeredWindow")
        finally:
            if screen:
                user32.ReleaseDC(None, screen)
        self.origin = origin
        if not self.visible:
            # Shown only after its first update (a layered window shows nothing before one), never activated.
            flags = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW
            if not user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, flags):
                raise _fail("SetWindowPos(show)")
            self.visible = True

    def hide(self) -> None:
        if not self.visible:
            return
        # SetWindowPos rather than ShowWindow(SW_HIDE): its NOACTIVATE guarantees no other window is activated.
        flags = SWP_HIDEWINDOW | SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE
        if not self.api.user32.SetWindowPos(self.hwnd, None, 0, 0, 0, 0, flags):
            raise _fail("SetWindowPos(hide)")
        self.visible = False

    def keep_on_top(self) -> None:
        if not self.visible:
            return
        flags = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
        if not self.api.user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, flags):
            raise _fail("SetWindowPos(topmost)")

    def invalidate(self) -> None:
        """Push the image again on the next draw (after a display change)."""
        self.origin = None

    def release_surface(self) -> None:
        if self.surface is not None:
            self.surface.destroy()
            self.surface = None
        self.image = None
        self.origin = None


def _make_thread_dpi_aware(api: _Api) -> None:
    """Per-monitor v2 for this thread, so its windows and coordinates are in physical pixels."""
    try:
        from ..desktop.windows import make_dpi_aware  # type: ignore[attr-defined]
    except ImportError:
        pass
    except Exception:
        log.debug("importing desktop.windows failed", exc_info=True)
    else:
        try:
            log.debug("process DPI awareness: %s", make_dpi_aware())
        except Exception:
            log.debug("make_dpi_aware failed", exc_info=True)
    # Per thread, whatever the process default: windows take their awareness from the thread that creates them.
    if api.SetThreadDpiAwarenessContext is None:
        log.info("this Windows has no per-thread DPI awareness; the reticle may be scaled on high-DPI monitors")
        return
    if not api.SetThreadDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2):
        log.warning("could not make the overlay thread per-monitor DPI aware (v2)")


# --------------------------------------------------------------------------- the UI thread

_class_ids = itertools.count(1)


class _Ui:
    """One run of the overlay: the UI thread, its window class, two windows and their surfaces."""

    def __init__(self, api: _Api) -> None:
        self.api = api
        self.class_name = f"JarvisHandsReticle.{os.getpid()}.{next(_class_ids)}"
        self.mailbox = Mailbox()
        self.ready = threading.Event()
        #: Why setup failed (set before ``ready``), or None.
        self.error: str | None = None
        #: The first drawing failure since start, for diagnostics; later ones are only logged at debug.
        self.draw_error: str | None = None
        self.thread = threading.Thread(target=self._run, name="jarvis-hands-overlay", daemon=True)
        self.thread_id = 0
        self._lock = threading.Lock()
        #: Where wake-ups and WM_CLOSE go; None once closing.
        self._post_hwnd: int | None = None
        self._abandoned = False
        self._hinstance: int | None = None
        self._class_registered = False
        self._proc: Any = None
        self._main: _Layer | None = None
        self._helper: _Layer | None = None
        #: Windows not yet destroyed (UI thread only).
        self._windows: list[int] = []
        self._dpi: dict[int, int] = {}

    @property
    def hwnds(self) -> tuple[int, ...]:
        return tuple(layer.hwnd for layer in (self._main, self._helper) if layer is not None)

    # ----- any thread

    def submit(self, state: OverlayState) -> None:
        if not self.mailbox.put(state):
            return
        with self._lock:
            hwnd = self._post_hwnd
        if hwnd is None:
            self.mailbox.wake_failed()
            return
        if not self.api.user32.PostMessageW(hwnd, WM_OVERLAY_WAKE, 0, 0):
            self.mailbox.wake_failed()
            self._failed(f"PostMessageW failed with Windows error {_last_error()}")

    def stop(self, timeout: float) -> None:
        with self._lock:
            hwnd, self._post_hwnd = self._post_hwnd, None
            self._abandoned = True
        self.mailbox.close()
        user32 = self.api.user32
        posted = bool(hwnd) and bool(user32.PostMessageW(hwnd, WM_CLOSE, 0, 0))
        if not posted and self.thread_id:
            posted = bool(user32.PostThreadMessageW(self.thread_id, WM_OVERLAY_STOP, 0, 0))
        if threading.current_thread() is self.thread or not self.thread.is_alive():
            return
        self.thread.join(timeout)
        if self.thread.is_alive():
            log.warning("the overlay thread did not stop within %.1f s (posted: %s)", timeout, posted)

    def _failed(self, message: str, *, exc_info: bool = False) -> None:
        if self.draw_error is None:
            self.draw_error = message
            log.warning("overlay: %s (the reticle may not show; logged once)", message, exc_info=exc_info)
        else:
            log.debug("overlay: %s", message, exc_info=exc_info)

    # ----- the UI thread

    def _run(self) -> None:
        try:
            self._setup()
        except Exception as exc:
            self.error = str(exc) or type(exc).__name__
            log.debug("overlay setup failed", exc_info=True)
            self._teardown()
            self.ready.set()
            return
        with self._lock:
            abandoned = self._abandoned
            if not abandoned:
                self._post_hwnd = self._main.hwnd if self._main is not None else None
        self.ready.set()
        try:
            if not abandoned:
                self._loop()
        except Exception:
            log.exception("the overlay's message loop failed")
        finally:
            self._teardown()

    def _setup(self) -> None:
        api = self.api
        self.thread_id = int(api.kernel32.GetCurrentThreadId())
        _make_thread_dpi_aware(api)
        self._hinstance = api.kernel32.GetModuleHandleW(None)
        if not self._hinstance:
            raise _fail("GetModuleHandleW")
        self._proc = api.WNDPROC(self._wndproc)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = ctypes.cast(self._proc, ctypes.c_void_p).value
        wc.hInstance = self._hinstance
        wc.lpszClassName = self.class_name
        if not api.user32.RegisterClassExW(ctypes.byref(wc)) and _last_error() != ERROR_CLASS_ALREADY_EXISTS:
            raise _fail("RegisterClassExW")
        self._class_registered = True
        self._main = _Layer(api, self._create_window("Jarvis hands reticle"))
        self._helper = _Layer(api, self._create_window("Jarvis hands reticle (second hand)"))
        if not api.user32.SetTimer(self._main.hwnd, _TIMER_ID, TOPMOST_INTERVAL_MS, None):
            raise _fail("SetTimer")

    def _create_window(self, title: str) -> int:
        hwnd = self.api.user32.CreateWindowExW(
            OVERLAY_EX_STYLE, self.class_name, title, WS_POPUP, 0, 0, 1, 1, None, None, self._hinstance, None
        )
        if not hwnd:
            raise _fail("CreateWindowExW")
        self._windows.append(hwnd)
        return int(hwnd)

    def _loop(self) -> None:
        user32 = self.api.user32
        msg = MSG()
        while True:
            got = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if got == 0:  # WM_QUIT
                return
            if got == -1:
                raise _fail("GetMessageW")
            if not msg.hwnd and msg.message == WM_OVERLAY_STOP:
                self._destroy_windows()
                continue
            user32.DispatchMessageW(ctypes.byref(msg))

    def _wndproc(self, hwnd: int | None, msg: int, wparam: int, lparam: int) -> int:
        # Exceptions must not escape a ctypes callback: they would only reach stderr (none under pythonw).
        try:
            if msg == WM_OVERLAY_WAKE:
                self._on_wake()
                return 0
            if msg == WM_TIMER and wparam == _TIMER_ID:
                self._dpi.clear()
                for layer in (self._main, self._helper):
                    if layer is not None:
                        layer.keep_on_top()
                return 0
            if msg in (WM_DISPLAYCHANGE, WM_DPICHANGED, WM_SETTINGCHANGE):
                # Only note it and redraw from the queue: WM_DPICHANGED can arrive inside our own update.
                self._dpi.clear()
                for layer in (self._main, self._helper):
                    if layer is not None:
                        layer.invalidate()
                self.submit(self.mailbox.state)
                if msg == WM_DPICHANGED:
                    return 0  # we size ourselves; do not take the suggested rect
            elif msg == WM_MOUSEACTIVATE:
                return MA_NOACTIVATE
            elif msg == WM_CLOSE:
                self._destroy_windows()
                return 0
            elif msg == WM_DESTROY and self._main is not None and hwnd == self._main.hwnd:
                self.api.user32.PostQuitMessage(0)
                return 0
        except Exception as exc:  # noqa: BLE001 - _failed logs it; the windows must keep working
            self._failed(f"handling message 0x{msg:04X} failed: {exc}", exc_info=True)
        return int(self.api.user32.DefWindowProcW(hwnd, msg, wparam, lparam))

    def _on_wake(self) -> None:
        state, seq = self.mailbox.take()
        try:
            self._draw(state)
        except Exception as exc:  # noqa: BLE001 - _failed logs it; the next state tries again
            self._failed(f"drawing the reticle failed: {exc}", exc_info=True)
        finally:
            self.mailbox.drawn(seq)

    def _draw(self, state: OverlayState) -> None:
        main, helper = self._main, self._helper
        if main is None or helper is None:
            return
        cursor = state.cursor
        if state.mode == "hidden" or cursor is None or not _finite(cursor):
            main.hide()
            helper.hide()
            return
        size = reticle_size(state.mode, self._dpi_at(cursor))
        main.show(render(state, size), top_left(cursor, size))
        if state.helper is not None and _finite(state.helper):
            marker = helper_size(self._dpi_at(state.helper))
            helper.show(render_helper(marker), top_left(state.helper, marker))
        else:
            helper.hide()

    def _dpi_at(self, p: Point) -> int:
        """The effective DPI of the monitor nearest ``p`` (cached per monitor; cleared every second)."""
        x, y = p.rounded()
        monitor = self.api.user32.MonitorFromPoint(POINT(x, y), MONITOR_DEFAULTTONEAREST)
        if not monitor:
            return 96
        dpi = self._dpi.get(monitor)
        if dpi is None:
            dpi = self._dpi[monitor] = self._monitor_dpi(monitor)
        return dpi

    def _monitor_dpi(self, monitor: int) -> int:
        get_dpi = self.api.GetDpiForMonitor
        if get_dpi is None:
            return 96
        dpi_x, dpi_y = _UINT(), _UINT()
        hr = get_dpi(monitor, MDT_EFFECTIVE_DPI, ctypes.byref(dpi_x), ctypes.byref(dpi_y))
        if hr != 0 or not dpi_x.value:
            log.debug("GetDpiForMonitor failed (HRESULT 0x%08X)", hr & 0xFFFFFFFF)
            return 96
        return int(dpi_x.value)

    def _destroy_windows(self) -> None:
        """On the UI thread: destroys the windows (the main one last, whose WM_DESTROY ends the loop)."""
        with self._lock:
            self._post_hwnd = None
        user32 = self.api.user32
        main = self._main.hwnd if self._main is not None else None
        for hwnd in sorted(self._windows, key=lambda h: h == main):
            if not user32.DestroyWindow(hwnd):
                log.debug("DestroyWindow failed with Windows error %d", _last_error())
            self._windows.remove(hwnd)
        user32.PostQuitMessage(0)  # also when the main window was already gone

    def _teardown(self) -> None:
        """On the UI thread, after the loop (or a failed setup). Never raises."""
        try:
            with self._lock:
                self._post_hwnd = None
            self.mailbox.close()
            if self._windows:
                self._destroy_windows()
            for layer in (self._main, self._helper):
                if layer is not None:
                    layer.release_surface()
            if self._class_registered:
                if not self.api.user32.UnregisterClassW(self.class_name, self._hinstance):
                    log.debug("UnregisterClassW failed with Windows error %d", _last_error())
                self._class_registered = False
        except Exception:
            log.debug("overlay teardown failed", exc_info=True)
        # self._proc stays referenced (with this object): DefWindowProc may still be running a callback.


# --------------------------------------------------------------------------- the Overlay


class WindowsOverlay:
    """The ``Overlay`` on Windows. ``start()`` and ``close()`` may repeat; ``show()`` never raises."""

    def __init__(
        self, *, start_timeout: float = START_TIMEOUT_S, stop_timeout: float = STOP_TIMEOUT_S, api: Any = None
    ) -> None:
        self.start_timeout = start_timeout
        self.stop_timeout = stop_timeout
        #: Tests pass a fake Win32 here (the same attributes as ``_Api``) to run every path on any OS.
        self._api = api
        self._lock = threading.Lock()
        self._ui: _Ui | None = None
        self._show_failed = False

    def start(self) -> None:
        with self._lock:
            if self._ui is not None:
                return
            api = self._api
            if api is None:
                if sys.platform != "win32":
                    raise OverlayError("the on-screen reticle needs Windows")
                try:
                    api = _win32()
                except (OSError, AttributeError) as exc:
                    raise OverlayError(f"could not load the Windows APIs for the reticle: {exc}") from exc
            ui = _Ui(api)
            ui.thread.start()
            if not ui.ready.wait(self.start_timeout):
                ui.stop(self.stop_timeout)
                raise OverlayError(f"the reticle's windows were not ready after {self.start_timeout:.0f} s")
            if ui.error is not None:
                ui.thread.join(self.stop_timeout)
                raise OverlayError(f"could not create the reticle's windows: {ui.error}")
            self._ui = ui
            log.debug("overlay started (class %s, windows %s)", ui.class_name, ui.hwnds)

    def show(self, state: OverlayState) -> None:
        ui = self._ui  # no lock: show() must stay cheap, and a stale reference only posts to a closing window
        if ui is None:
            return
        try:
            ui.submit(state)
        except Exception:
            if not self._show_failed:
                self._show_failed = True
                log.warning("overlay: show() failed (logged once)", exc_info=True)

    def close(self) -> None:
        with self._lock:
            ui, self._ui = self._ui, None
        if ui is None:
            return
        try:
            ui.stop(self.stop_timeout)
        except Exception:
            log.debug("overlay close failed", exc_info=True)

    # ----- for tests and diagnostics

    @property
    def hwnds(self) -> tuple[int, ...]:
        """The main and helper windows' handles while started, else ()."""
        ui = self._ui
        return ui.hwnds if ui is not None else ()

    @property
    def draw_error(self) -> str | None:
        """The first drawing failure since ``start()``, or None."""
        ui = self._ui
        return ui.draw_error if ui is not None else None

    def wait_drawn(self, timeout: float = 1.0) -> bool:
        """True once the UI thread has drawn the newest state shown (False after close or on timeout)."""
        ui = self._ui
        return ui.mailbox.wait_drawn(timeout) if ui is not None else False
