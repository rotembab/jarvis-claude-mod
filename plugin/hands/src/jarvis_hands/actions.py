"""What the gesture engine asks the executor to do, in order.

Points are desktop pixels (floats; the executor rounds). An action with a
point moves the cursor exactly there before it acts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

MouseButton = Literal["left", "right"]
Direction = Literal["left", "right", "up", "down"]


@dataclass(frozen=True)
class MoveCursor:
    x: float
    y: float


@dataclass(frozen=True)
class Button:
    button: MouseButton
    down: bool
    x: float
    y: float


@dataclass(frozen=True)
class Scroll:
    """Wheel units (120 = one notch). Positive ``dy`` scrolls up (content moves down), positive ``dx`` right."""

    dy: float
    dx: float
    x: float
    y: float


@dataclass(frozen=True)
class GrabWindow:
    x: float
    y: float


@dataclass(frozen=True)
class DragWindow:
    x: float
    y: float


@dataclass(frozen=True)
class ResizeWindow:
    """Both hands' points: ``a`` the pointer hand, ``b`` the helper hand. The first one starts the resize."""

    ax: float
    ay: float
    bx: float
    by: float


@dataclass(frozen=True)
class ReleaseWindow:
    pass


@dataclass(frozen=True)
class ThrowWindow:
    direction: Direction


@dataclass(frozen=True)
class ReleaseAll:
    """Every held button up and any window grab ended (hand lost, disengage, shutdown)."""


Action = (
    MoveCursor | Button | Scroll | GrabWindow | DragWindow | ResizeWindow | ReleaseWindow | ThrowWindow | ReleaseAll
)
