"""Small geometry types shared by mapping, the gesture engine and the desktop backends.

Desktop coordinates are physical pixels in the virtual-screen space (the helper
is per-monitor DPI aware), so they are integers there; the engine works in
floats and the executor rounds at the last moment.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Point:
    x: float
    y: float

    def __add__(self, other: Point) -> Point:
        return Point(self.x + other.x, self.y + other.y)

    def __sub__(self, other: Point) -> Point:
        return Point(self.x - other.x, self.y - other.y)

    def scale(self, k: float) -> Point:
        return Point(self.x * k, self.y * k)

    def length(self) -> float:
        return math.hypot(self.x, self.y)

    def distance(self, other: Point) -> float:
        return math.hypot(self.x - other.x, self.y - other.y)

    def rounded(self) -> tuple[int, int]:
        return round(self.x), round(self.y)


@dataclass(frozen=True)
class Rect:
    """An axis-aligned rectangle: left/top inclusive, width/height in the same units."""

    x: float
    y: float
    width: float
    height: float

    @staticmethod
    def from_ltrb(left: float, top: float, right: float, bottom: float) -> Rect:
        return Rect(left, top, right - left, bottom - top)

    @property
    def left(self) -> float:
        return self.x

    @property
    def top(self) -> float:
        return self.y

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height

    @property
    def center(self) -> Point:
        return Point(self.x + self.width / 2, self.y + self.height / 2)

    def contains(self, p: Point) -> bool:
        return self.left <= p.x < self.right and self.top <= p.y < self.bottom

    def clamp(self, p: Point) -> Point:
        """The point of this rect nearest ``p`` (right/bottom edges are exclusive by one unit)."""
        return Point(
            min(max(p.x, self.left), self.right - 1),
            min(max(p.y, self.top), self.bottom - 1),
        )

    def distance_to(self, p: Point) -> float:
        return self.clamp(p).distance(p)

    def union(self, other: Rect) -> Rect:
        return Rect.from_ltrb(
            min(self.left, other.left),
            min(self.top, other.top),
            max(self.right, other.right),
            max(self.bottom, other.bottom),
        )

    def intersects(self, other: Rect) -> bool:
        return (
            self.left < other.right and other.left < self.right and self.top < other.bottom and other.top < self.bottom
        )

    def moved_to(self, x: float, y: float) -> Rect:
        return Rect(x, y, self.width, self.height)

    def rounded(self) -> tuple[int, int, int, int]:
        return round(self.x), round(self.y), round(self.width), round(self.height)


def bounding_rect(rects: list[Rect]) -> Rect:
    if not rects:
        raise ValueError("bounding_rect of no rects")
    box = rects[0]
    for rect in rects[1:]:
        box = box.union(rect)
    return box


def apply_homography(h: np.ndarray, p: Point) -> Point:
    """Maps ``p`` through the 3x3 homography ``h`` (projective division included)."""
    vec = h @ np.array([p.x, p.y, 1.0])
    w = vec[2] if abs(vec[2]) > 1e-12 else 1e-12
    return Point(float(vec[0] / w), float(vec[1] / w))


def box_homography(x0: float, y0: float, x1: float, y1: float) -> np.ndarray:
    """The homography taking the box (x0, y0)-(x1, y1) onto the unit square."""
    sx, sy = 1.0 / (x1 - x0), 1.0 / (y1 - y0)
    return np.array([[sx, 0.0, -x0 * sx], [0.0, sy, -y0 * sy], [0.0, 0.0, 1.0]])
