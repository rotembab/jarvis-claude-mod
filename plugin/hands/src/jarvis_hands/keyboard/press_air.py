"""Press by tapping a finger in the air: the detector of DESIGN-KEYBOARD.md 2.12, the default method.

STUB (T0). Track T1 replaces this file; the names below are the contract (the ``PressMethod`` protocol of ``types.py``).
The real module imports ``types``, ``limits``, ``tuning`` and the standard library only: no numpy in the hot path.
"""

from __future__ import annotations

from collections.abc import Sequence

from .tuning import Tuning
from .types import FingerView, HandSample, PressEvent, PressLevel, PressName, PressQuality, Side

STUB_OWNER = "T1"


class AirTapPress:
    name: PressName = "air"
    requires_review: bool = True
    rejects: dict[str, int]

    def __init__(self, tuning: Tuning) -> None:
        raise NotImplementedError("T1: keyboard.press_air")

    def update(self, hands: Sequence[HandSample]) -> list[PressEvent]:
        raise NotImplementedError("T1: keyboard.press_air")

    def reset(self) -> None:
        raise NotImplementedError("T1: keyboard.press_air")

    def set_finger(self, side: Side, finger: int, close: float, open_: float | None = None) -> None:
        raise NotImplementedError("T1: keyboard.press_air")

    def fingers(self, hands: Sequence[HandSample]) -> list[FingerView]:
        raise NotImplementedError("T1: keyboard.press_air")

    def quality(self) -> PressQuality:
        raise NotImplementedError("T1: keyboard.press_air")

    def set_level(self, level: PressLevel) -> None:
        raise NotImplementedError("T1: keyboard.press_air")

    def set_calibrating(self, on: bool) -> None:
        raise NotImplementedError("T1: keyboard.press_air")
