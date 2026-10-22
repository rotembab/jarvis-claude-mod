"""The desktop the executor drives: displays, the cursor, buttons, the wheel and windows.

Coordinates are physical pixels in the virtual-screen space. ``windows.py``
implements it with ctypes; ``fake.py`` is the test double; other platforms get
``unsupported.py`` until their backend exists.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

from ..geometry import Rect
from .keys import KeyStroke

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


# --------------------------------------------------------------------------- the keyboard side (air keyboard)


@dataclass(frozen=True)
class KeyTarget:
    """The window a typed key would land in, as far as the desktop can tell without looking inside it."""

    #: Foreground window; 0 = none.
    hwnd: int
    pid: int
    #: Executable base name without ".exe"; "" unknown. Local screen only: never in an event or a log.
    name: str = field(repr=False)
    #: Keyboard layout language id of the foreground thread (0x040D Hebrew); 0 unknown.
    lang_id: int
    #: Why keys must not be sent there: an elevated window, the shell (Start, Alt+Tab, the taskbar), our own window,
    #: or none.
    blocked: Literal["elevated", "shell", "own", "none"] | None
    #: A classic password edit control (ES_PASSWORD) has the focus.
    password: bool
    #: The shell says the user is busy: D3D full screen or a presentation.
    covered: bool


class KeyDesktop(Protocol):
    """What the keyboard needs of a desktop.

    ``WindowsDesktop`` and ``FakeDesktop`` offer it; ``Desktop`` itself is not extended.
    """

    injects_for_real: bool

    def key_target(self) -> KeyTarget: ...

    def foreground_window(self) -> int:
        """Cheap; 0 when none."""
        ...

    def send_keys(self, strokes: Sequence[KeyStroke], *, inject: Literal["unicode", "vk"] = "unicode") -> int:
        """Types the strokes in order, each as ONE atomic input batch (all its downs and ups together).

        Returns the number of strokes sent in full. Raises InputBlocked when Windows took none of the first,
        OSError when it took some, KeyRefused for a stroke the allow-list refuses.
        """
        ...

    def foreign_input(self) -> bool:
        """Input other than ours since the previous call; True when it cannot tell."""
        ...

    def modifiers_down(self) -> bool:
        """A physical Ctrl, Alt or Win key is down."""
        ...

    def release_keys(self) -> None:
        """Sends any key-up Windows refused earlier; never raises."""
        ...

    def open_os_keyboard(self) -> bool:
        """Launches osk.exe by absolute path; False if it did not start."""
        ...


#: The names a desktop must have to be a KeyDesktop (the Protocol's own attributes, in its order).
KEY_DESKTOP_NAMES = (
    "injects_for_real",
    "key_target",
    "foreground_window",
    "send_keys",
    "foreign_input",
    "modifiers_down",
    "release_keys",
    "open_os_keyboard",
)


def as_key_desktop(desktop: object) -> KeyDesktop | None:
    """The desktop if it has every name of ``KeyDesktop``, else None.

    The ``Desktop`` protocol itself is not extended: ``WindowsDesktop`` and ``FakeDesktop`` subclass it explicitly,
    so a method added to it would be inherited with an empty body and a desktop without keys would look like one with.
    """
    if desktop is not None and all(hasattr(desktop, name) for name in KEY_DESKTOP_NAMES):
        return desktop  # type: ignore[return-value]
    return None
