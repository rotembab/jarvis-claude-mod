"""An in-memory desktop for tests and ``--fake`` runs: displays, a cursor, buttons, a wheel and windows.

It behaves like Windows where the executor depends on it: the cursor is
clamped into the nearest monitor, ``window_at`` returns the topmost window
under a point (``raise_window`` changes the order), maximize fills the work
area of the window's display and restore brings back the rect it had, and a
``blocked`` window refuses to be moved (``InputBlocked``), like an
administrator's window does. ``move_mouse`` stands for the user moving the
real mouse: it changes the cursor without being logged as one of our moves.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from ..geometry import Point, Rect
from .base import Desktop, Display, InputBlocked, Window, WindowState

DEFAULT_WINDOW_RECT = Rect(100, 100, 800, 600)


def default_displays() -> list[Display]:
    """One 1920 x 1080 primary monitor at the origin with a 40 px taskbar at the bottom."""
    return [Display(1, r"\\.\DISPLAY1", Rect(0, 0, 1920, 1080), Rect(0, 0, 1920, 1040), primary=True)]


@dataclass
class FakeWindow:
    handle: int
    title: str = ""
    rect: Rect = DEFAULT_WINDOW_RECT
    state: WindowState = "normal"
    #: Refuses moves and resizes (an elevated window under UIPI).
    blocked: bool = False
    kind: str = "FakeWindow"
    #: The rect to come back to from maximized.
    normal_rect: Rect | None = None
    #: The state to come back to from minimized.
    before_minimize: WindowState = "normal"
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def window(self) -> Window:
        return Window(self.handle, self.title, self.kind)


class FakeDesktop(Desktop):
    def __init__(
        self,
        displays: list[Display] | None = None,
        windows: list[FakeWindow] | None = None,
        *,
        cursor: tuple[int, int] | None = None,
        double_click: tuple[float, int, int] = (0.5, 4, 4),
    ) -> None:
        self._displays = list(displays) if displays is not None else default_displays()
        #: Z-order: index 0 is the topmost window.
        self.windows: list[FakeWindow] = list(windows or [])
        primary = next((d for d in self._displays if d.primary), self._displays[0])
        self.cursor_pos: tuple[int, int] = cursor if cursor is not None else primary.rect.center.rounded()
        self.buttons_down: set[str] = set()
        self.scroll_dy = 0
        self.scroll_dx = 0
        self.calls: list[tuple[Any, ...]] = []
        self.double_click_metrics = double_click
        self.closed = False

    # -- test hooks ---------------------------------------------------------------------

    def move_mouse(self, x: int, y: int) -> None:
        """The USER moves the real mouse (not logged as a move_cursor call)."""
        self.cursor_pos = self._clamp(x, y)

    def window(self, handle: int) -> FakeWindow:
        for w in self.windows:
            if w.handle == handle:
                return w
        raise KeyError(handle)

    def calls_named(self, name: str) -> list[tuple[Any, ...]]:
        return [c for c in self.calls if c[0] == name]

    # -- Desktop ------------------------------------------------------------------------

    def displays(self) -> list[Display]:
        return list(self._displays)

    def cursor(self) -> tuple[int, int]:
        return self.cursor_pos

    def move_cursor(self, x: int, y: int) -> None:
        self.calls.append(("move_cursor", x, y))
        self.cursor_pos = self._clamp(x, y)

    def button(self, button: Literal["left", "right"], down: bool) -> None:
        self.calls.append(("button", button, down))
        if down:
            self.buttons_down.add(button)
        else:
            self.buttons_down.discard(button)

    def scroll(self, dy: int, dx: int = 0) -> None:
        self.calls.append(("scroll", dy, dx))
        self.scroll_dy += dy
        self.scroll_dx += dx

    def window_at(self, x: int, y: int) -> Window | None:
        p = Point(x, y)
        for w in self.windows:
            if w.state != "minimized" and w.rect.contains(p):
                return w.window
        return None

    def window_rect(self, window: Window) -> Rect:
        return self.window(window.handle).rect

    def window_state(self, window: Window) -> WindowState:
        return self.window(window.handle).state

    def set_window_rect(self, window: Window, rect: Rect) -> None:
        w = self.window(window.handle)
        if w.blocked:
            raise InputBlocked(f"access denied moving window {window.handle}")
        self.calls.append(("set_window_rect", window.handle, rect))
        w.rect = rect

    def raise_window(self, window: Window) -> None:
        w = self.window(window.handle)
        self.calls.append(("raise_window", window.handle))
        self.windows.remove(w)
        self.windows.insert(0, w)

    def restore(self, window: Window) -> None:
        w = self.window(window.handle)
        if w.blocked:
            raise InputBlocked(f"access denied restoring window {window.handle}")
        self.calls.append(("restore", window.handle))
        if w.state == "minimized":
            w.state = w.before_minimize
        elif w.state == "maximized":
            w.state = "normal"
            if w.normal_rect is not None:
                w.rect = w.normal_rect

    def maximize(self, window: Window) -> None:
        w = self.window(window.handle)
        if w.blocked:
            raise InputBlocked(f"access denied maximizing window {window.handle}")
        self.calls.append(("maximize", window.handle))
        if w.state != "maximized":
            w.normal_rect = w.rect
        w.state = "maximized"
        w.rect = self._display_of(w.rect).work

    def minimize(self, window: Window) -> None:
        w = self.window(window.handle)
        if w.blocked:
            raise InputBlocked(f"access denied minimizing window {window.handle}")
        self.calls.append(("minimize", window.handle))
        if w.state != "minimized":
            w.before_minimize = w.state
        w.state = "minimized"

    def double_click(self) -> tuple[float, int, int]:
        return self.double_click_metrics

    def close(self) -> None:
        self.calls.append(("close",))
        self.closed = True

    # -- helpers ------------------------------------------------------------------------

    def _clamp(self, x: int, y: int) -> tuple[int, int]:
        p = Point(x, y)
        if any(d.rect.contains(p) for d in self._displays):
            return x, y
        nearest = min(self._displays, key=lambda d: d.rect.distance_to(p))
        return nearest.rect.clamp(p).rounded()

    def _display_of(self, rect: Rect) -> Display:
        centre = rect.center
        for d in self._displays:
            if d.rect.contains(centre):
                return d
        return min(self._displays, key=lambda d: d.rect.distance_to(centre))
