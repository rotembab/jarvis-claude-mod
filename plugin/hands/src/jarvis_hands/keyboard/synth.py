"""Synthetic hands that type, for tests and ``run --fake`` (DESIGN-KEYBOARD.md 5.1).

STUB (T0). Track T1 replaces this file; the names below are the contract. ``script`` is duck-typed: the ``Script`` of
``tests/scripted.py`` (which a package module cannot import) whose ``frame()`` calls ``.observe(size)`` on a hand.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np

from ..landmarks import Frame, HandObservation
from .layout import Key, Layout
from .plane import Plane
from .types import Lang, Side

STUB_OWNER = "T1"
_STUB_DATA = ("STYLES",)

#: (MCP, PIP, DIP) degrees: the natural-feel typing posture.
REST = (12, 18, 9)


@dataclass(frozen=True)
class SynthHand:
    side: Side = "right"
    #: Hand position, image fractions.
    at: tuple[float, float] = (0.5, 0.5)
    posture: Literal["rest", "relaxed", "straight"] = "relaxed"
    #: (finger 0..3, k): k 0 open .. 1 touching; the thumb tip travels to that tip and the finger dips with it.
    pinch: tuple[int, float] | None = None
    #: The other fingers dip by this share of the pinching finger's dip.
    coactivation: float = 0.0
    #: Frame widths, xy, per landmark per frame.
    jitter: float = 0.0015
    z_noise: float = 0.004
    rng: np.random.Generator | None = None

    def observe(self, size: tuple[int, int]) -> HandObservation:
        raise NotImplementedError("T1: keyboard.synth")


@dataclass(frozen=True)
class Noise:
    sigma: float = 0.001
    ar: float = 0.6
    blur: float = 4.0
    glitch_p: float = 0.003
    glitch_fw: float = 0.03
    amp_gain: tuple[float, float] = (0.7, 1.1)
    coh_deg: float = 0.0
    coh_ar: float = 0.7


class Typist:
    """Builds frame lists on a ``Script``."""

    def __init__(
        self,
        script: Any,
        plane: Plane,
        *,
        rng: np.random.Generator,
        jitter: float = 0.0015,
        z_noise: float = 0.004,
        posture: Literal["rest", "relaxed", "straight"] = "relaxed",
        layout: Layout | None = None,
    ) -> None:
        raise NotImplementedError("T1: keyboard.synth")

    def hover(self, seconds: float, hands: Sequence[Side] = ("left", "right")) -> list[Frame]:
        raise NotImplementedError("T1: keyboard.synth")

    def place(self) -> list[Frame]:
        raise NotImplementedError("T1: keyboard.synth")

    def warm(self) -> list[Frame]:
        raise NotImplementedError("T1: keyboard.synth")

    def press(
        self,
        key: Key,
        *,
        finger: int | None = None,
        side: Side | None = None,
        closing: int = 5,
        held: int = 3,
        opening: int = 3,
        drift: float = 0.0,
    ) -> list[Frame]:
        raise NotImplementedError("T1: keyboard.synth")

    def type(self, text: str, *, gap_s: float = 0.35, lang: Lang = "en") -> list[Frame]:
        raise NotImplementedError("T1: keyboard.synth")

    def tap_n(self, key: Key, n: int, gap_s: float) -> list[Frame]:
        raise NotImplementedError("T1: keyboard.synth")


class AirTypist:
    """An air-tap hand: scripted taps over a noisy landmark model."""

    def __init__(
        self,
        side: Side,
        home_at: tuple[float, float],
        events: Sequence[object],
        rng: np.random.Generator,
        noise: Noise,
        alpha: float = 0.65,
        lead_s: float = 0.08,
        rest: tuple[int, int, int] = REST,
        coupling_p: float = 0.4,
        coupling_share: tuple[float, float] = (0.10, 0.35),
    ) -> None:
        raise NotImplementedError("T1: keyboard.synth")

    def observe(self, t: float, dt: float) -> HandObservation:
        raise NotImplementedError("T1: keyboard.synth")


class Burst:
    """Wraps an ``AirTypist`` and adds correlated landmark noise inside each ``(t0, t1)`` window."""

    def __init__(
        self, hand: AirTypist, windows: Sequence[tuple[float, float]], sigma_b: float, rng: np.random.Generator
    ) -> None:
        raise NotImplementedError("T1: keyboard.synth")

    def observe(self, t: float, dt: float) -> HandObservation:
        raise NotImplementedError("T1: keyboard.synth")


def __getattr__(name: str) -> object:
    if name in _STUB_DATA:
        raise NotImplementedError("T1: keyboard.synth")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
