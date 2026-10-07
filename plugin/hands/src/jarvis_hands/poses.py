"""Per-hand pose classification from MediaPipe's world landmarks, with hysteresis.

Every feature is a ratio of distances in the metric, hand-centred world
landmarks, so it holds whatever the hand's distance to the camera:

- finger reach ``|tip - wrist| / |mcp - wrist|``: about 1.6 to 2.0 for a
  straight finger and 0.7 to 1.05 for a curled one on MediaPipe's own test
  photos. Extended and curled each have an enter and a leave threshold, and
  the band between them belongs to neither, so a half-bent finger never
  flickers between the two.
- pinch ``|thumb tip - finger tip| / palm size`` (palm size: wrist to the
  middle knuckle), closing below 0.25 and opening above 0.40. A fist also
  brings the thumb onto the index, so the index pinch only counts while the
  index is not curled; in a pointing pose the thumb rests on the curled middle
  finger, so the middle pinch only counts while the middle is not curled and
  the index pinch is open.

The anchor (the point that drives the cursor) is the mean of the index and
middle knuckles in the image: it barely moves when the fingers pinch, curl or
spread, which is what keeps a click from dragging the cursor along.
"""

from __future__ import annotations

from dataclasses import dataclass
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
    pinch_close: float = 0.25
    pinch_open: float = 0.40
    #: Pinch ratio shown as fully open (0) by the overlay's closing arc; ``pinch_close`` shows as 1.
    pinch_display_open: float = 0.80


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


def finger_reach(world: np.ndarray) -> tuple[float, float, float, float]:
    """Reach ratio per finger (index, middle, ring, pinky)."""
    wrist = world[WRIST]
    reach = []
    for name in FINGER_ORDER:
        mcp, tip = FINGERS[name]
        base = float(np.linalg.norm(world[mcp] - wrist))
        reach.append(float(np.linalg.norm(world[tip] - wrist)) / base if base > 1e-9 else 0.0)
    return reach[0], reach[1], reach[2], reach[3]


def palm_size(world: np.ndarray) -> float:
    return float(np.linalg.norm(world[WRIST] - world[MIDDLE_MCP]))


def pinch_ratio(world: np.ndarray, tip: int) -> float:
    """Thumb tip to ``tip`` over the palm size."""
    size = palm_size(world)
    if size < 1e-9:
        return float("inf")
    return float(np.linalg.norm(world[THUMB_TIP] - world[tip])) / size


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
        self._pinch_index = False
        self._pinch_middle = False

    def classify(self, hand: HandObservation, anchor: AnchorMode = "knuckles") -> HandPose:
        th = self.thresholds
        reach = finger_reach(hand.world)
        for i, r in enumerate(reach):
            self._extended[i] = r > th.extended_leave if self._extended[i] else r > th.extended_enter
            self._curled[i] = r < th.curled_leave if self._curled[i] else r < th.curled_enter

        index_ratio = pinch_ratio(hand.world, INDEX_TIP)
        if self._curled[0]:
            self._pinch_index = False
        else:
            self._pinch_index = self._closed(self._pinch_index, index_ratio)

        if self._curled[1] or self._pinch_index:
            self._pinch_middle = False
        else:
            self._pinch_middle = self._closed(self._pinch_middle, pinch_ratio(hand.world, MIDDLE_TIP))

        extended = (self._extended[0], self._extended[1], self._extended[2], self._extended[3])
        curled = (self._curled[0], self._curled[1], self._curled[2], self._curled[3])
        pose: PoseName
        if self._pinch_index:
            pose = "pinch"
        elif self._pinch_middle:
            pose = "pinch_middle"
        elif all(curled):
            pose = "fist"
        elif extended[0] and extended[1] and curled[2] and curled[3]:
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
