"""The warm-up that decides each finger's threshold and arms the keyboard (DESIGN-KEYBOARD.md 2.5, 2.12.5, 3.7).

STUB (T0). Track T2 replaces this file; the names below are the contract.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from .plane import Plane
from .tuning import Tuning
from .types import HandSample, PressEvent, PressName, Side

STUB_OWNER = "T2"


class Warmup:
    #: Air: the finger the strip names now; None during the wait between fingers, when complete, and for pinch.
    prompt: tuple[Side, int] | None
    #: Air: strays since the sequence began or last restarted.
    strays: int
    required: frozenset[tuple[Side, int]]
    done: frozenset[tuple[Side, int]]
    complete: bool
    #: Air: D_f per finger (the depth of its accepted tap); {} for pinch.
    depth: dict[tuple[Side, int], float]

    def __init__(
        self,
        tuning: Tuning,
        method: PressName = "pinch",
        *,
        home_f: Mapping[tuple[Side, int], tuple[float, float]] | None = None,
    ) -> None:
        raise NotImplementedError("T2: keyboard.warmup")

    def update(
        self,
        hands: Sequence[HandSample],
        events: Sequence[PressEvent] = (),
        *,
        t: float = 0.0,
        plane: Plane | None = None,
        rejects: int = 0,
    ) -> tuple[str, ...]:
        """Pinch: as 2.5, the other arguments are ignored and () is returned. Air: one outcome per event (2.12.5)."""
        raise NotImplementedError("T2: keyboard.warmup")

    def thresholds(self) -> dict[tuple[Side, int], tuple[float, float]]:
        """Pinch: (close, open). Air: (D_f, D_f)."""
        raise NotImplementedError("T2: keyboard.warmup")
