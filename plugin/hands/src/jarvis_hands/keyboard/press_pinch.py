"""Press by pinching a finger to the thumb: the state machine of DESIGN-KEYBOARD.md 2.6.

STUB (T0). Track T1 replaces this file; the names below are the contract (the ``PressMethod`` protocol of ``types.py``).
"""

from __future__ import annotations

from collections.abc import Sequence

from .tuning import Tuning
from .types import FingerView, HandSample, PressEvent, PressLevel, PressName, PressQuality, Side

STUB_OWNER = "T1"


class PinchPress:
    name: PressName = "pinch"
    requires_review: bool = False
    rejects: dict[str, int]

    def __init__(self, tuning: Tuning) -> None:
        raise NotImplementedError("T1: keyboard.press_pinch")

    def update(self, hands: Sequence[HandSample]) -> list[PressEvent]:
        raise NotImplementedError("T1: keyboard.press_pinch")

    def reset(self) -> None:
        raise NotImplementedError("T1: keyboard.press_pinch")

    def set_finger(self, side: Side, finger: int, close: float, open_: float | None = None) -> None:
        raise NotImplementedError("T1: keyboard.press_pinch")

    def fingers(self, hands: Sequence[HandSample]) -> list[FingerView]:
        raise NotImplementedError("T1: keyboard.press_pinch")

    def quality(self) -> PressQuality:
        raise NotImplementedError("T1: keyboard.press_pinch")

    def set_level(self, level: PressLevel) -> None:
        raise NotImplementedError("T1: keyboard.press_pinch")

    def set_calibrating(self, on: bool) -> None:
        raise NotImplementedError("T1: keyboard.press_pinch")
