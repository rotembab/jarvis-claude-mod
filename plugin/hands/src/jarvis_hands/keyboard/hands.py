"""Hand tracking for the keyboard: identity across frames, finger features and levelling.

DESIGN-KEYBOARD.md 2.1, 2.2 and 2.12.1.

STUB (T0). Track T1 replaces this file; the names below are the contract.
"""

from __future__ import annotations

from ..landmarks import Frame
from .tuning import Tuning
from .types import HandSample

STUB_OWNER = "T1"


class HandTracker:
    def __init__(self, tuning: Tuning) -> None:
        raise NotImplementedError("T1: keyboard.hands")

    def update(self, frame: Frame) -> list[HandSample]:
        raise NotImplementedError("T1: keyboard.hands")

    def reset(self) -> None:
        raise NotImplementedError("T1: keyboard.hands")
