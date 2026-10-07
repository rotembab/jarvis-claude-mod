"""Camera space -> desktop pixels across the chosen displays.

Two steps: a homography ``H`` takes the anchor (mirrored, normalized camera
coordinates) to the unit square, either the default box from the settings or
a calibrated one; the unit square then spans the bounding rect of the chosen
displays, as Windows arranges them, so a projector set up to the right of the
monitor is reached by moving the hand right. Displays of different sizes
leave gaps in that rect, so the point is clamped into the nearest chosen
display; overshooting the box therefore pins the cursor to screen edges and
corners, which is how the taskbar and the corners are reached.

The runtime changes displays, selection and calibration from the control
server's thread while the engine thread maps, so one lock guards the state
and every method works on a consistent snapshot.
"""

from __future__ import annotations

import threading
from typing import Literal

import numpy as np

from .desktop.base import Display
from .geometry import Point, Rect, apply_homography, bounding_rect, box_homography
from .settings import DisplaySelection, HandsSettings

Direction = Literal["left", "right", "up", "down"]


def select_displays(displays: list[Display], selection: DisplaySelection) -> list[Display]:
    """The displays hand control maps onto, in the given order.

    ``"all"`` means every display that is not virtual (or all of them when
    every one is virtual); a tuple of ids means those that exist, falling back
    to ``"all"`` when none does.
    """
    if selection != "all":
        wanted = set(selection)
        chosen = [d for d in displays if d.id in wanted]
        if chosen:
            return chosen
    real = [d for d in displays if not d.virtual]
    return real or list(displays)


def display_in_direction(displays: list[Display], origin: Display, direction: Direction) -> Display | None:
    """The nearest display lying beyond ``origin``'s edge in ``direction``, or None.

    Displays that overlap ``origin`` on the other axis (side by side, not
    diagonal) win; among them the nearest edge, then the nearest centre. When
    none overlaps, the nearest by centre among those beyond the edge.
    """
    o = origin.rect
    candidates: list[tuple[bool, float, float, Display]] = []
    for d in displays:
        if d == origin:
            continue
        r = d.rect
        if direction == "right":
            beyond, gap, overlaps = r.left >= o.right - 1, r.left - o.right, r.top < o.bottom and o.top < r.bottom
        elif direction == "left":
            beyond, gap, overlaps = r.right <= o.left + 1, o.left - r.right, r.top < o.bottom and o.top < r.bottom
        elif direction == "down":
            beyond, gap, overlaps = r.top >= o.bottom - 1, r.top - o.bottom, r.left < o.right and o.left < r.right
        elif direction == "up":
            beyond, gap, overlaps = r.bottom <= o.top + 1, o.top - r.bottom, r.left < o.right and o.left < r.right
        else:
            raise ValueError(f"unknown direction {direction!r}")
        if beyond:
            candidates.append((overlaps, max(gap, 0.0), r.center.distance(o.center), d))
    if not candidates:
        return None
    overlapping = [c for c in candidates if c[0]]
    if overlapping:
        return min(overlapping, key=lambda c: (c[1], c[2]))[3]
    return min(candidates, key=lambda c: c[2])[3]


class ScreenMapper:
    def __init__(self, displays: list[Display], settings: HandsSettings, homography: np.ndarray | None = None) -> None:
        self._lock = threading.Lock()
        self._settings = settings
        self._displays = list(displays)
        self._selection: DisplaySelection = settings.displays
        self._homography: np.ndarray | None = None
        self._h = box_homography(*settings.box)
        self._used: list[Display] = []
        self._region = Rect(0, 0, 1, 1)
        self._set_homography(homography)
        self._recompute()

    # -- configuration (any thread) -------------------------------------------------

    def set_displays(self, displays: list[Display]) -> None:
        with self._lock:
            self._displays = list(displays)
            self._recompute()

    def set_selection(self, selection: DisplaySelection) -> None:
        with self._lock:
            self._selection = selection
            self._recompute()

    def set_homography(self, h: np.ndarray | None) -> None:
        """A calibrated camera -> unit square homography, or None for the settings' default box."""
        with self._lock:
            self._set_homography(h)

    def _set_homography(self, h: np.ndarray | None) -> None:
        if h is None:
            self._homography = None
            self._h = box_homography(*self._settings.box)
            return
        matrix = np.array(h, dtype=float)
        if matrix.shape != (3, 3) or not np.all(np.isfinite(matrix)):
            raise ValueError("homography must be a finite 3x3 matrix")
        self._homography = matrix
        self._h = matrix

    def _recompute(self) -> None:
        self._used = select_displays(self._displays, self._selection)
        if self._used:
            self._region = bounding_rect([d.rect for d in self._used])
        else:
            self._region = Rect(0, 0, 1, 1)

    # -- state ------------------------------------------------------------------------

    @property
    def displays(self) -> list[Display]:
        with self._lock:
            return list(self._displays)

    @property
    def used(self) -> list[Display]:
        with self._lock:
            return list(self._used)

    @property
    def region(self) -> Rect:
        with self._lock:
            return self._region

    @property
    def calibrated(self) -> bool:
        with self._lock:
            return self._homography is not None

    @property
    def homography(self) -> np.ndarray:
        """The homography in use (the calibrated one or the default box's)."""
        with self._lock:
            return self._h.copy()

    # -- mapping ----------------------------------------------------------------------

    def to_target(self, anchor: Point) -> Point:
        """Unit-square coordinates of a camera point (not clamped)."""
        with self._lock:
            h = self._h
        return apply_homography(h, anchor)

    def to_region(self, anchor: Point) -> Point:
        """Desktop pixels of a camera point in the region, NOT clamped to the displays.

        For relative motion (scrolling, throw speed), which must keep counting
        when the hand overshoots an edge.
        """
        with self._lock:
            h, region = self._h, self._region
        uv = apply_homography(h, anchor)
        return Point(region.x + uv.x * region.width, region.y + uv.y * region.height)

    def to_desktop(self, anchor: Point) -> Point:
        """Desktop pixels of a camera point, clamped into the nearest used display."""
        with self._lock:
            h, region, used = self._h, self._region, self._used
        uv = apply_homography(h, anchor)
        return _clamp_into(Point(region.x + uv.x * region.width, region.y + uv.y * region.height), used)

    def target_point(self, u: float, v: float) -> Point:
        """Desktop pixels of unit-square coordinates in the region, clamped like ``to_desktop``."""
        with self._lock:
            region, used = self._region, self._used
        return _clamp_into(Point(region.x + u * region.width, region.y + v * region.height), used)

    def display_at(self, p: Point) -> Display:
        """The used display containing ``p``, else the nearest used one."""
        with self._lock:
            used = self._used
        if not used:
            raise LookupError("no displays")
        return _nearest(p, used)


def _nearest(p: Point, displays: list[Display]) -> Display:
    for d in displays:
        if d.rect.contains(p):
            return d
    return min(displays, key=lambda d: d.rect.distance_to(p))


def _clamp_into(p: Point, displays: list[Display]) -> Point:
    if not displays:
        return p
    return _nearest(p, displays).rect.clamp(p)
