"""The press-method registry: which method a setting names (DESIGN-KEYBOARD.md 3.4).

STUB (T0). Track T1 replaces this file; the names below are the contract.
"""

from __future__ import annotations

from .tuning import Tuning
from .types import PressMethod, PressName

STUB_OWNER = "T1"


class PressUnavailable(ValueError):
    """The named method cannot run here (air without the review box, or the system keyboard, which has no session)."""


def make_press(name: PressName, tuning: Tuning, *, review: bool = False) -> PressMethod:
    """pinch -> PinchPress; air -> AirTapPress when ``review`` is True, else unavailable; windows -> unavailable."""
    raise NotImplementedError("T1: keyboard.press")
