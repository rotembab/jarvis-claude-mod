"""Per-hand pose classification from MediaPipe's image landmarks, with hysteresis.

The features are computed on the image landmarks scaled to frame widths on
every axis, ``(x, y * height / width, z)`` (MediaPipe's ``z`` uses roughly the
scale of ``x``). Every feature is a ratio of distances, so it holds whatever
the hand's distance to the camera. World landmarks would be metric, but their
depth is poor: on MediaPipe's OK-sign photo the world thumb-to-index distance
is 4 to 7 cm with the tips touching, which hides the pinch.

- finger reach ``|tip - wrist| / |mcp - wrist|``: 1.5 to 2.2 for a straight
  finger, 1.3 to 1.4 for a relaxed one and 0.7 to 0.95 for a curled one on
  MediaPipe's own test photos. Extended and curled each have an enter and a
  leave threshold, and the band between them belongs to neither, so a
  half-bent finger never flickers between the two.
- pinch ``|thumb tip - finger tip| / palm size`` (palm size: wrist to the
  middle knuckle), closing below 0.28 and opening above 0.40: 0.13 and 0.22
  on the OK-sign photo, 0.39 and up for open or relaxed hands. A fist also
  brings the thumb onto the index, so the index pinch only counts while the
  index is not curled; in a pointing pose the thumb rests on the curled middle
  finger, so the middle pinch only counts while the middle is not curled and
  the index pinch is open.

The anchor (the point that drives the cursor) is the mean of the index and
middle knuckles in the image: it barely moves when the fingers pinch, curl or
spread, which is what keeps a click from dragging the cursor along.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Literal

import numpy as np

from .geometry import Point
from .landmarks import FINGERS, INDEX_MCP, INDEX_TIP, MIDDLE_MCP, MIDDLE_TIP, THUMB_TIP, WRIST, HandObservation

PoseName = Literal["hover", "palm", "pinch", "pinch_middle", "fist", "two"]
AnchorMode = Literal["knuckles", "index"]

FINGER_ORDER = ("index", "middle", "ring", "pinky")


@dataclass(frozen=True)
class PoseThresholds:
    extended_enter: float = 1.40
    extended_leave: float = 1.25
    curled_enter: float = 1.10
    curled_leave: float = 1.20
    pinch_close: float = 0.28
    pinch_open: float = 0.40
    #: Pinch ratio shown as fully open (0) by the overlay's closing arc; ``pinch_close`` shows as 1.
    pinch_display_open: float = 0.80
    #: What a finger must reach to count toward a fist (and the ring and pinky of the two-finger pose): ``None`` is the
    #: curled thresholds. Tighter than them for a fist sensitivity below 1.0, which asks more of the fist and nothing
    #: else: the curled flags also gate the pinch and tell the engine how settled one is (see ``thresholds_for``).
    fist_enter: float | None = None
    fist_leave: float | None = None


#: Between a curled threshold and the extended one above it, and between the two extended ones: the band a
#: half-bent finger sits in, which belongs to neither state (see ``thresholds_for``).
BAND = 0.04


def thresholds_for(pinch: float = 1.0, fist: float = 1.0) -> PoseThresholds:
    """The pose thresholds for the pinch and fist sensitivities (1.0 each is ``PoseThresholds()`` exactly).

    ``pinch`` scales ``pinch_close``: higher counts a looser pinch. The
    ``pinch_open`` threshold scales down with a stricter pinch but never up
    with a looser one: a pinch lets go where it always did, so a hand that
    rests its thumb near the index (0.39 to 0.47 palm sizes on a relaxed open
    palm) cannot hold the button down after a click. Where the close threshold
    climbs to within ``BAND`` of the open one (a pinch above 1.29), the open
    one is pushed up to keep that gap, which is about six times the ratio's
    noise. ``fist`` scales what counts as a fist: higher counts a fist whose
    fingers are less tightly curled. Above 1.0 it scales the curled
    thresholds, which then also gate the pinch (the thumb rests on a loose
    fist's index). Below 1.0 the curled thresholds stay where they are and
    only ``fist_enter`` and ``fist_leave`` tighten: the curled flags keep a
    pinch from starting on a curled finger and tell the engine how settled a
    pinch is, so scaling them would change when a hand that closes slowly
    into a fist presses the button. A finger must never be curled and
    extended at once, so where the curled thresholds climb into the extended
    ones those are pushed up, ``BAND`` clear of them, and the order
    ``curled_enter < curled_leave < extended_leave < extended_enter`` holds
    for every value. The extended thresholds only move for a fist
    sensitivity above the default's headroom (about 1.01), and then by little.
    """
    if not (math.isfinite(pinch) and math.isfinite(fist) and pinch > 0 and fist > 0):
        raise ValueError("sensitivities must be positive, finite numbers")
    base = PoseThresholds()
    curled_enter = base.curled_enter * max(fist, 1.0)
    curled_leave = base.curled_leave * max(fist, 1.0)
    extended_leave = max(base.extended_leave, curled_leave + BAND)
    extended_enter = max(base.extended_enter, extended_leave + BAND)
    pinch_close = base.pinch_close * pinch
    return replace(
        base,
        extended_enter=extended_enter,
        extended_leave=extended_leave,
        curled_enter=curled_enter,
        curled_leave=curled_leave,
        pinch_close=pinch_close,
        pinch_open=max(base.pinch_open * min(pinch, 1.0), pinch_close + BAND),
        pinch_display_open=max(base.pinch_display_open, pinch_close + BAND),
        fist_enter=base.curled_enter * fist if fist < 1.0 else None,
        fist_leave=base.curled_leave * fist if fist < 1.0 else None,
    )


@dataclass(frozen=True)
class HandPose:
    pose: PoseName
    #: Image coordinates (normalized, mirrored frame) of the point that drives the cursor.
    anchor: Point
    #: How closed the index pinch is: 0 open .. 1 closed (for the overlay).
    pinch: float
    #: Index, middle, ring, pinky.
    extended: tuple[bool, bool, bool, bool]
    curled: tuple[bool, bool, bool, bool]
    #: Each finger's reach ratio (index, middle, ring, pinky): how the engine tells a hand that
    #: holds a pinch from one passing through it on its way into or out of a fist.
    reach: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)


#: Height over width of the camera frame when the caller does not say (1280 x 720).
DEFAULT_ASPECT = 720 / 1280


def pose_points(image: np.ndarray, aspect: float = DEFAULT_ASPECT) -> np.ndarray:
    """Image landmarks in frame widths on every axis: ``(x, y * aspect, z)``, aspect = height / width."""
    return np.asarray(image, dtype=float) * np.array([1.0, aspect, 1.0])


def finger_reach(points: np.ndarray) -> tuple[float, float, float, float]:
    """Reach ratio per finger (index, middle, ring, pinky) of a (21, 3) landmark array."""
    wrist = points[WRIST]
    reach = []
    for name in FINGER_ORDER:
        mcp, tip = FINGERS[name]
        base = float(np.linalg.norm(points[mcp] - wrist))
        reach.append(float(np.linalg.norm(points[tip] - wrist)) / base if base > 1e-9 else 0.0)
    return reach[0], reach[1], reach[2], reach[3]


def palm_size(points: np.ndarray) -> float:
    return float(np.linalg.norm(points[WRIST] - points[MIDDLE_MCP]))


def pinch_ratio(points: np.ndarray, tip: int) -> float:
    """Thumb tip to ``tip`` over the palm size."""
    size = palm_size(points)
    if size < 1e-9:
        return float("inf")
    return float(np.linalg.norm(points[THUMB_TIP] - points[tip])) / size


def anchor_point(image: np.ndarray, mode: AnchorMode = "knuckles") -> Point:
    if mode == "index":
        return Point(float(image[INDEX_TIP, 0]), float(image[INDEX_TIP, 1]))
    return Point(
        float(image[INDEX_MCP, 0] + image[MIDDLE_MCP, 0]) / 2,
        float(image[INDEX_MCP, 1] + image[MIDDLE_MCP, 1]) / 2,
    )


class PoseTracker:
    """Hysteresis state for ONE tracked hand; ``classify`` once per frame."""

    def __init__(self, thresholds: PoseThresholds | None = None) -> None:
        self.thresholds = thresholds or PoseThresholds()
        self.reset()

    def reset(self) -> None:
        self._extended = [False] * 4
        self._curled = [False] * 4
        self._fisted = [False] * 4
        self._pinch_index = False
        self._pinch_middle = False

    def classify(
        self, hand: HandObservation, anchor: AnchorMode = "knuckles", aspect: float = DEFAULT_ASPECT
    ) -> HandPose:
        """``aspect``: the camera frame's height over its width."""
        th = self.thresholds
        points = pose_points(hand.image, aspect)
        reach = finger_reach(points)
        fist_enter = th.curled_enter if th.fist_enter is None else th.fist_enter
        fist_leave = th.curled_leave if th.fist_leave is None else th.fist_leave
        for i, r in enumerate(reach):
            self._extended[i] = r > th.extended_leave if self._extended[i] else r > th.extended_enter
            self._curled[i] = r < th.curled_leave if self._curled[i] else r < th.curled_enter
            self._fisted[i] = r < fist_leave if self._fisted[i] else r < fist_enter

        index_ratio = pinch_ratio(points, INDEX_TIP)
        if self._curled[0]:
            self._pinch_index = False
        else:
            self._pinch_index = self._closed(self._pinch_index, index_ratio)

        if self._curled[1] or self._pinch_index:
            self._pinch_middle = False
        else:
            self._pinch_middle = self._closed(self._pinch_middle, pinch_ratio(points, MIDDLE_TIP))

        extended = (self._extended[0], self._extended[1], self._extended[2], self._extended[3])
        curled = (self._curled[0], self._curled[1], self._curled[2], self._curled[3])
        pose: PoseName
        if self._pinch_index:
            pose = "pinch"
        elif self._pinch_middle:
            pose = "pinch_middle"
        elif all(self._fisted):
            pose = "fist"
        elif extended[0] and extended[1] and self._fisted[2] and self._fisted[3]:
            pose = "two"
        elif all(extended):
            pose = "palm"
        else:
            pose = "hover"
        return HandPose(
            pose=pose,
            anchor=anchor_point(hand.image, anchor),
            pinch=self._pinch_display(index_ratio, curled[0]),
            extended=extended,
            curled=curled,
            reach=reach,
        )

    def _closed(self, was_closed: bool, ratio: float) -> bool:
        if was_closed:
            return ratio <= self.thresholds.pinch_open
        return ratio < self.thresholds.pinch_close

    def _pinch_display(self, ratio: float, index_curled: bool) -> float:
        if self._pinch_index:
            return 1.0
        if index_curled:
            return 0.0
        th = self.thresholds
        span = th.pinch_display_open - th.pinch_close
        return float(min(1.0, max(0.0, (th.pinch_display_open - ratio) / span)))
