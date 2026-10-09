"""The plane the keys lie on, and where it is placed (DESIGN-KEYBOARD.md 2.3, 2.4, 3.3).

Everything is in pose space (frame widths) on the way in and in key units on the way out; the two meet only in
``Plane``. Pure: no I/O, no clock, no state.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from .layout import WIDTH_U, Layout
from .tuning import Tuning
from .types import HandSample, Side

#: Where the four fingertips of a hand rest, in key units along the home row: a s d f and j k l '.
_LEFT_HOME_U = 2.0
_RIGHT_HOME_U = 8.0
#: The pitch measured from two hands may differ from the base pitch by this much either way.
_PITCH_CLAMP = (0.90, 1.15)
#: The cluster centres are six units apart, and the plane's centre lies this many pitches right of their midpoint.
_CLUSTER_UNITS = _RIGHT_HOME_U - _LEFT_HOME_U
_CENTRE_SHIFT_UNITS = WIDTH_U / 2 - (_LEFT_HOME_U + _RIGHT_HOME_U) / 2


@dataclass(frozen=True)
class Plane:
    #: Centre, pose space (frame widths).
    cx: float
    cy: float
    #: Key pitch along a row and between rows, frame widths.
    px: float
    py: float
    #: 4 for the direct layout, 5 for the review layout.
    rows: int = 4

    def units(self, p: Sequence[float]) -> tuple[float, float]:
        """Pose space to key units: ``((x - cx) / px + W/2, (y - cy) / py + rows/2)``."""
        return (
            float((p[0] - self.cx) / self.px + WIDTH_U / 2),
            float((p[1] - self.cy) / self.py + self.rows / 2),
        )

    def pose(self, u: float, v: float) -> tuple[float, float]:
        """The exact inverse of ``units`` (hook H7: a test or the practice drill puts a fingertip on a key)."""
        return (float((u - WIDTH_U / 2) * self.px + self.cx), float((v - self.rows / 2) * self.py + self.cy))


@dataclass(frozen=True)
class Placement:
    plane: Plane
    #: Mean (u, v) of each hand's four aims, for the drift indicator.
    home: dict[Side, tuple[float, float]]
    #: Mean (u, v) of each finger's own aim, for the air warm-up.
    home_f: dict[tuple[Side, int], tuple[float, float]]


def place_plane(
    window: Sequence[Sequence[HandSample]], *, layout: Layout, tuning: Tuning, reach: float = 1.0
) -> Placement:
    """The plane that puts the resting fingertips of the hands in ``window`` on the home row (2.4).

    ``window`` holds the frames of the still period, each the samples of the hands that were still. Two hands are
    ordered by their position in the picture, never by their label (the label is the engine's newest guess); they set
    the pitch from their distance, within 0.90 to 1.15 of the base pitch. One hand uses the base pitch and its own
    label (the one most of the window carried) to know which cluster it is. The base pitch ``tuning.pitch * reach`` and
    the layout's ``home_v`` and ``rows`` are not in the window, so they come in as arguments. A window without a hand is
    a caller's bug and raises.
    """
    tracks: dict[int, list[HandSample]] = {}
    for frame in window:
        for sample in frame:
            tracks.setdefault(sample.hand, []).append(sample)
    if not tracks:
        raise ValueError("no hand in the window")
    # At most two hands type; if a third track flickered through the window, the two that stayed are the hands.
    kept = sorted(tracks.values(), key=len, reverse=True)[:2]
    kept.sort(key=lambda samples: float(np.mean([s.anchor[0] for s in samples])))

    aims = [
        np.mean([[s.fingers[f].aim for s in samples] for f in range(4)], axis=1)  # (4 fingers, 2) mean aim per finger
        for samples in kept
    ]
    px0 = tuning.pitch * reach
    if len(kept) == 2:
        x_left, x_right = (float(np.mean(a[:, 0])) for a in aims)
        px = min(max((x_right - x_left) / _CLUSTER_UNITS, _PITCH_CLAMP[0] * px0), _PITCH_CLAMP[1] * px0)
        cx = (x_left + x_right) / 2 + _CENTRE_SHIFT_UNITS * px
        sides: tuple[Side, ...] = ("left", "right")
    else:
        px = px0
        # The label the hand carried for most of the window: one flipped frame (the last one included) must not pick
        # the other cluster, which would make every warm-up and pinch tap a stray.
        sides = (Counter(s.side for s in kept[0]).most_common(1)[0][0],)
        x_mean = float(np.mean(aims[0][:, 0]))
        home_u = _LEFT_HOME_U if sides[0] == "left" else _RIGHT_HOME_U
        cx = x_mean - (home_u - WIDTH_U / 2) * px
    py = tuning.pitch_y_ratio * px
    mean_y = float(np.mean([a[:, 1] for a in aims]))
    # The resting fingertips sit on the home row of either layout, so the plane's centre lies below them.
    cy = mean_y - (layout.home_v - layout.rows / 2) * py
    plane = Plane(cx, cy, px, py, layout.rows)

    home: dict[Side, tuple[float, float]] = {}
    home_f: dict[tuple[Side, int], tuple[float, float]] = {}
    for side, hand_aims in zip(sides, aims, strict=True):
        home[side] = plane.units(np.mean(hand_aims, axis=0))
        for finger in range(4):
            home_f[(side, finger)] = plane.units(hand_aims[finger])
    return Placement(plane, home, home_f)
