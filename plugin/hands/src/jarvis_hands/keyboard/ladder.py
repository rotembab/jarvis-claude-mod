"""The air tap's degradation ladder: ok, degraded, off (DESIGN-KEYBOARD.md 2.12.7, 3.7).

STUB (T2 replaces this file; the names below are the contract). The real module is pure: no clock (times are arguments),
no numpy, and it imports ``types``, ``limits``, ``tuning`` and the standard library only.
"""

from __future__ import annotations

from typing import Literal

from .types import PressQuality

STUB_OWNER = "T2"

Level = Literal["ok", "degraded", "off"]


class AirLadder:
    level: Level
    reason: Literal["", "fps", "noise", "both", "gaps"]
    #: True while a noise condition holds: the session then tells the press method to be strict.
    strict: bool

    def __init__(self) -> None:
        raise NotImplementedError("T2: keyboard.ladder")

    def update(self, t: float, q: PressQuality, hands_present: bool) -> Level:
        """``off`` is terminal for the session."""
        raise NotImplementedError("T2: keyboard.ladder")
