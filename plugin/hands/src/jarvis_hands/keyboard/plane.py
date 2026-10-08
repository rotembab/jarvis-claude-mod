"""The plane the keys lie on, and where it is placed (DESIGN-KEYBOARD.md 2.3, 2.4, 3.3).

STUB (T0). Track T1 replaces this file; the names below are the contract. The tuple arguments of ``units`` and ``pose``
are pose-space (x, y) pairs; T1 may widen them.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .layout import Layout
from .tuning import Tuning
from .types import HandSample, Side

STUB_OWNER = "T1"


@dataclass(frozen=True)
class Plane:
    #: Centre, pose space (frame widths).
    cx: float
    cy: float
    #: Key pitch along a row and between rows, frame widths.
    px: float
    py: float
    #: 4 for the direct layout, 5 for the review layout.
    rows: int = 4

    def units(self, p: tuple[float, float]) -> tuple[float, float]:
        """Pose space to key units: ``((x - cx) / px + W/2, (y - cy) / py + rows/2)``."""
        raise NotImplementedError("T1: keyboard.plane")

    def pose(self, u: float, v: float) -> tuple[float, float]:
        """The exact inverse of ``units``."""
        raise NotImplementedError("T1: keyboard.plane")


@dataclass(frozen=True)
class Placement:
    plane: Plane
    #: Mean (u, v) of each hand's four aims, for the drift indicator.
    home: dict[Side, tuple[float, float]]
    #: Mean (u, v) of each finger's own aim, for the air warm-up.
    home_f: dict[tuple[Side, int], tuple[float, float]]


def place_plane(
    window: Sequence[Sequence[HandSample]], *, layout: Layout, tuning: Tuning, reach: float = 1.0
) -> Placement:
    """The signature to build to; DESIGN 2.4 and 3.3 write ``place_plane(window)`` and ``(window, *, layout)``.

    The base pitch ``px0 = tuning.pitch * reach`` (2.3, 3.2) and the layout's ``home_v`` and ``rows`` (``cy``) are not
    in the window, so they come in as arguments; ``home`` and ``home_f`` of 2.4 are the other half of the answer.
    """
    raise NotImplementedError("T1: keyboard.plane")
