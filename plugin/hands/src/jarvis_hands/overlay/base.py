"""The on-screen reticle: where the hand points, and what it is doing.

The overlay is drawn in click-through, always-on-top windows that never take
focus, so it never gets in the way of what it points at. The runtime calls
``show`` once per tracked frame from its own thread; a backend keeps only the
newest state and draws it on its own thread.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from ..geometry import Point

#: What the reticle shows:
#: ``hidden``: nothing (no hand, or overlay off).
#: ``idle``: a faint ring where a visible hand would point, not engaged.
#: ``engaging``: an open palm being held; ``progress`` fills the ring.
#: ``point``: engaged and pointing; ``pinch`` closes the inner arc.
#: ``press``, ``drag``, ``scroll``, ``grab``, ``resize``: the active interaction.
#: ``calibrate``: a target at ``cursor`` to point at; ``progress`` fills while held.
OverlayMode = Literal["hidden", "idle", "engaging", "point", "press", "drag", "scroll", "grab", "resize", "calibrate"]


class OverlayError(RuntimeError):
    """The overlay could not be created."""


@dataclass(frozen=True)
class OverlayState:
    mode: OverlayMode = "hidden"
    #: Desktop pixels (physical, virtual-screen space) of the main reticle or calibration target.
    cursor: Point | None = None
    #: The second hand's point while resizing with two hands.
    helper: Point | None = None
    #: 0 open .. 1 closed (``point`` mode).
    pinch: float = 0.0
    #: 0 .. 1 (``engaging`` and ``calibrate`` modes).
    progress: float = 0.0


class Overlay(Protocol):
    def start(self) -> None:
        """Creates the windows. Raises OverlayError."""
        ...

    def show(self, state: OverlayState) -> None:
        """Thread-safe and cheap; the newest state wins."""
        ...

    def close(self) -> None:
        """Destroys the windows. Idempotent; never raises."""
        ...


class NullOverlay:
    """No reticle (overlay off, or no backend for this platform)."""

    def start(self) -> None:
        pass

    def show(self, state: OverlayState) -> None:
        pass

    def close(self) -> None:
        pass
