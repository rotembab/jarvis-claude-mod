"""The air keyboard's settings: what the mod sends with ``keyboard configure`` (DESIGN-KEYBOARD.md 3.10).

A separate record owned by the controller; ``HandsSettings`` is not touched. Everything here is in memory only: the mod
re-sends the settings at every ``hello``, so nothing persists in the helper and ``enabled`` is always the mod's current
option. ``apply`` is all-or-nothing, and its error texts name the setting but never what was sent: a rejected value may
be anything, and an exception text is data too.

``protocol.py`` keeps its own hand-written copy of these ranges (it must stay importable without this package's
records); ``tests/test_kb_settings.py`` checks that the two accept the same bodies.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal

from .types import Commit, Dock, PressName

#: Wire key (camelCase, as the mod sends it) -> field name.
WIRE_KEYS = MappingProxyType(
    {
        "enabled": "enabled",
        "press": "press",
        "commit": "commit",
        "layout": "layout",
        "size": "size",
        "reach": "reach",
        "dock": "dock",
        "idleS": "idle_s",
        "inject": "inject",
        "enter": "enter",
    }
)

_CHOICES = MappingProxyType(
    {
        "press": ("air", "pinch", "windows"),
        "commit": ("review", "direct"),
        "layout": ("auto", "en", "he"),
        "dock": ("top", "bottom"),
        "inject": ("unicode", "vk"),
        "enter": ("twice", "off"),
    }
)
_SIZE = (0.6, 1.6)
_REACH = (0.8, 1.5)
_IDLE_S = (5, 300)

AIR_NEEDS_REVIEW = "The air-tap method only works with the review box (commit: review)."


def _bad(wire: str) -> ValueError:
    return ValueError(f"The keyboard setting {wire} is missing or out of range.")


def _real(value: object, low: float, high: float) -> float | None:
    """A finite number inside the range. A bool is not a number here, and a string that looks like one is not either."""
    if type(value) is not int and type(value) is not float:
        return None
    try:
        number = float(value)
    except OverflowError:  # an integer too large for a float
        return None
    return number if math.isfinite(number) and low <= number <= high else None


def _whole(value: object, low: int, high: int) -> int | None:
    """An integer in the range; 30.0 counts (JSON Schema calls it an integer), 30.5 and bools do not."""
    if type(value) is int:
        whole = value
    elif type(value) is float and value.is_integer():
        whole = int(value)
    else:
        return None
    return whole if low <= whole <= high else None


def _clean(wire: str, value: object) -> Any:
    """The value to store for one wire key; raises when the body's value is not acceptable."""
    if wire == "enabled":
        if type(value) is not bool:
            raise _bad(wire)
        return value
    if wire in _CHOICES:
        if type(value) is not str or value not in _CHOICES[wire]:
            raise _bad(wire)
        return value
    if wire == "idleS":
        whole = _whole(value, *_IDLE_S)
        if whole is None:
            raise _bad(wire)
        return whole
    number = _real(value, *(_SIZE if wire == "size" else _REACH))
    if number is None:
        raise _bad(wire)
    return number


@dataclass
class KeyboardSettings:
    enabled: bool = False
    #: Changed from ``pinch`` in the first version: the air tap is the default method.
    press: PressName = "air"
    #: The air tap works with the review box only.
    commit: Commit = "review"
    layout: Literal["auto", "en", "he"] = "auto"
    size: float = 1.0
    reach: float = 1.0
    dock: Dock = "top"
    idle_s: int = 30
    inject: Literal["unicode", "vk"] = "unicode"
    #: ``twice`` is the first version's name: two presses in direct mode, the guarded three-tap Send in review mode.
    enter: Literal["twice", "off"] = "twice"

    def apply(self, body: Mapping[str, Any]) -> None:
        """Apply a ``settings`` body. All or nothing: ValueError, and nothing changed, on anything outside the table."""
        if not isinstance(body, Mapping):
            raise ValueError("The keyboard settings must be an object.")
        clean: dict[str, Any] = {}
        for wire, value in body.items():
            if wire not in WIRE_KEYS:
                # Not echoed: the key may be anything, of any length.
                raise ValueError("The keyboard settings hold an unknown setting.")
            clean[WIRE_KEYS[wire]] = _clean(wire, value)
        # The merged result, not the body alone: `press` may arrive in one message and `commit` in another.
        press = clean.get("press", self.press)
        commit = clean.get("commit", self.commit)
        if press == "air" and commit == "direct":
            raise ValueError(AIR_NEEDS_REVIEW)
        for name, value in clean.items():
            setattr(self, name, value)
