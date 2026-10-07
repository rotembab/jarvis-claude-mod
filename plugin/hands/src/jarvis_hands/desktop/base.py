"""The desktop the executor drives: displays, the cursor, buttons, the wheel and windows.

Coordinates are physical pixels in the virtual-screen space. ``windows.py``
implements it with ctypes; ``fake.py`` is the test double; other platforms get
``unsupported.py`` until their backend exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from ..geometry import Rect

WindowState = Literal["normal", "maximized", "minimized"]


class UnsupportedPlatform(RuntimeError):
    """No desktop backend for this operating system yet."""


class InputBlocked(RuntimeError):
    """Windows refused to let us act on a window (usually an administrator's window, UIPI)."""


@dataclass(frozen=True)
class Display:
    #: Stable within a run: 1-based, in Windows' enumeration order (primary not necessarily first).
    id: int
    #: Device name ("\\\\.\\DISPLAY1") or a readable name when one is known.
    name: str
    #: The whole monitor.
    rect: Rect
    #: The monitor minus the taskbar and docked bars.
    work: Rect
    primary: bool
    #: True for virtual / indirect displays (a virtual display driver, a streaming dummy).
    virtual: bool = False


@dataclass(frozen=True)
class Window:
    #: The native handle (an HWND as an int on Windows).
    handle: int
    title: str = ""
    #: The window class or app name, for logs.
    kind: str = ""


class Desktop(Protocol):
    def displays(self) -> list[Display]: ...

    def cursor(self) -> tuple[int, int]: ...

    def move_cursor(self, x: int, y: int) -> None: ...

    def button(self, button: Literal["left", "right"], down: bool) -> None: ...

    def scroll(self, dy: int, dx: int = 0) -> None:
        """Wheel units (120 = one notch); positive ``dy`` scrolls up, positive ``dx`` right."""
        ...

    def window_at(self, x: int, y: int) -> Window | None:
        """The top-level window under the point, or None for the desktop, taskbar, our overlay or nothing."""
        ...

    def window_rect(self, window: Window) -> Rect:
        """The window's visible frame (without the invisible resize borders)."""
        ...

    def window_state(self, window: Window) -> WindowState: ...

    def set_window_rect(self, window: Window, rect: Rect) -> None:
        """Moves/resizes so the visible frame is ``rect``. Raises InputBlocked when Windows refuses."""
        ...

    def raise_window(self, window: Window) -> None: ...

    def restore(self, window: Window) -> None: ...

    def maximize(self, window: Window) -> None: ...

    def minimize(self, window: Window) -> None: ...

    def double_click(self) -> tuple[float, int, int]:
        """The system double-click time (seconds) and rectangle (width, height in px)."""
        ...

    def close(self) -> None:
        """Releases anything the backend holds (never raises)."""
        ...
