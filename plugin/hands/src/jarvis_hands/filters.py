"""The One Euro filter (Casiez, Roussel and Vogel, CHI 2012) for the cursor.

A low-pass filter whose cutoff rises with speed: a still hand gets heavy
smoothing (no jitter on a button), a fast one little (no lag on a sweep).
``min_cutoff`` (Hz) sets the jitter at rest, ``beta`` how fast the cutoff
grows with speed (per unit/s), ``d_cutoff`` (Hz) smooths the speed estimate.

The speed estimate differentiates the RAW samples (as the authors' reference
implementation does), so it is the low-passed true speed; differentiating
against the lagging filtered value would overstate it during a steady sweep.

Timestamps come from the caller (camera capture times), so tests drive the
clock. A repeated or backwards timestamp (a duplicate frame) returns the last
output unchanged instead of dividing by zero.
"""

from __future__ import annotations

import math

from .geometry import Point


def smoothing_factor(cutoff: float, dt: float) -> float:
    """Exponential smoothing factor for a first-order low-pass at ``cutoff`` Hz over ``dt`` seconds."""
    tau = 1.0 / (2.0 * math.pi * cutoff)
    return 1.0 / (1.0 + tau / dt)


class OneEuroFilter:
    def __init__(self, min_cutoff: float = 1.0, beta: float = 0.0, d_cutoff: float = 1.0) -> None:
        if min_cutoff <= 0 or d_cutoff <= 0:
            raise ValueError("cutoff frequencies must be positive")
        self.min_cutoff = min_cutoff
        self.beta = beta
        self.d_cutoff = d_cutoff
        self._x: float | None = None
        self._raw = 0.0
        self._dx = 0.0
        self._t = 0.0

    @property
    def value(self) -> float | None:
        return self._x

    def reset(self) -> None:
        self._x = None
        self._dx = 0.0

    def filter(self, value: float, t: float) -> float:
        if self._x is None:
            self._x, self._raw, self._dx, self._t = value, value, 0.0, t
            return value
        dt = t - self._t
        if dt <= 0:
            return self._x
        a_d = smoothing_factor(self.d_cutoff, dt)
        self._dx = a_d * ((value - self._raw) / dt) + (1 - a_d) * self._dx
        self._raw = value
        cutoff = self.min_cutoff + self.beta * abs(self._dx)
        a = smoothing_factor(cutoff, dt)
        self._x = a * value + (1 - a) * self._x
        self._t = t
        return self._x


class OneEuroFilter2D:
    """One Euro on a point, with one cutoff for both axes from the speed of the point.

    Driving both axes from the same speed keeps a diagonal sweep straight
    (per-axis filters lag the slower axis more and bend the path).
    """

    def __init__(self, min_cutoff: float = 1.0, beta: float = 0.0, d_cutoff: float = 1.0) -> None:
        if min_cutoff <= 0 or d_cutoff <= 0:
            raise ValueError("cutoff frequencies must be positive")
        self.min_cutoff = min_cutoff
        self.beta = beta
        self.d_cutoff = d_cutoff
        self._p: Point | None = None
        self._raw = Point(0.0, 0.0)
        self._v = Point(0.0, 0.0)
        self._t = 0.0

    @property
    def value(self) -> Point | None:
        return self._p

    @property
    def velocity(self) -> Point:
        """Units per second (low-passed derivative), zero before the second sample."""
        return self._v

    @property
    def speed(self) -> float:
        """Units per second: the magnitude of the last filtered derivative."""
        return self._v.length()

    def reset(self) -> None:
        self._p = None
        self._v = Point(0.0, 0.0)

    def filter(self, p: Point, t: float) -> Point:
        if self._p is None:
            self._p, self._raw, self._v, self._t = p, p, Point(0.0, 0.0), t
            return p
        dt = t - self._t
        if dt <= 0:
            return self._p
        a_d = smoothing_factor(self.d_cutoff, dt)
        raw_v = (p - self._raw).scale(1.0 / dt)
        self._v = raw_v.scale(a_d) + self._v.scale(1 - a_d)
        self._raw = p
        cutoff = self.min_cutoff + self.beta * self._v.length()
        a = smoothing_factor(cutoff, dt)
        self._p = p.scale(a) + self._p.scale(1 - a)
        self._t = t
        return self._p
