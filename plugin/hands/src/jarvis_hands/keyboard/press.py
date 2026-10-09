"""The press-method registry: which method a setting names (DESIGN-KEYBOARD.md 3.4)."""

from __future__ import annotations

from .press_air import AirTapPress
from .press_pinch import PinchPress
from .tuning import Tuning
from .types import PressMethod, PressName


class PressUnavailable(ValueError):
    """The named method cannot run here (air without the review box, or the system keyboard, which has no session)."""


def make_press(name: PressName, tuning: Tuning, *, review: bool = False) -> PressMethod:
    """pinch -> PinchPress; air -> AirTapPress when ``review`` is True, else unavailable; windows -> unavailable.

    The text of every refusal is fixed: the caller shows it as it is.
    """
    if name == "pinch":
        return PinchPress(tuning)
    if name == "air":
        if not review:
            raise PressUnavailable("The air-tap method only works with the review box (commit: review).")
        return AirTapPress(tuning)
    if name == "windows":
        raise PressUnavailable("The system keyboard has no press method of its own.")
    raise PressUnavailable("That press method does not exist.")
