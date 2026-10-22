"""Camera space -> desktop pixels across the chosen displays.

Two steps: a homography ``H`` takes the anchor (mirrored, normalized camera
coordinates) to the unit square, either the default box from the settings or
a calibrated one; the unit square then spans the chosen displays as Windows
arranges them, so a projector set up to the right of the monitor is reached
by moving the hand right. The x and y spans that no chosen display covers are
taken out first: a display left out between two chosen ones (a virtual
display Windows put between the monitor and the projector, or a ``display``
choice that skips one) costs no share of the hand's range, and the cursor
goes straight from one chosen display's edge to the next one's. Displays of
different sizes still leave gaps, so the point is clamped into the nearest
chosen display; overshooting the box therefore pins the cursor to screen
edges and corners, which is how the taskbar and the corners are reached.

The cursor's speed is a gain on the unit square around its centre, after the
homography: ``(uv - 0.5) * gain + 0.5``, so a gain of 2 crosses the screen with
half the hand travel and the centre stays put. It belongs to the cursor
(``to_target``, ``to_desktop``), not to the relative motion that scrolls and
throws (``to_region``) and not to the calibration's targets (``target_point``),
and the calibration itself fits the homography alone. The engine sets it
(``set_cursor_gain``) when nothing that is held would jump.

The runtime changes displays, selection and calibration from the control
server's thread while the engine thread maps, so one lock guards the state
and every method works on a consistent snapshot.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Iterable
from dataclasses import dataclass
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


@dataclass(frozen=True)
class _Axis:
    """One axis of the used displays with the spans no display covers taken out.

    ``spans`` are the merged ``[start, end)`` desktop intervals the displays
    cover, ascending; laid end to end they make the compact axis, which runs
    from 0 to ``length``.
    """

    spans: tuple[tuple[float, float], ...]

    @staticmethod
    def covering(intervals: Iterable[tuple[float, float]]) -> _Axis:
        merged: list[tuple[float, float]] = []
        for start, end in sorted(intervals):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        return _Axis(tuple(merged) or ((0.0, 1.0),))

    @property
    def length(self) -> float:
        return sum(end - start for start, end in self.spans)

    def to_desktop(self, c: float) -> float:
        """The desktop coordinate of compact coordinate ``c``; beyond either end it runs on from that end."""
        offset = 0.0
        for start, end in self.spans:
            if c < offset + (end - start):
                return start + (c - offset)
            offset += end - start
        return self.spans[-1][1] + (c - offset)


@dataclass(frozen=True)
class _Layout:
    """The used displays and their compact layout: one immutable snapshot for a mapping call."""

    used: list[Display]
    xs: _Axis
    ys: _Axis
    #: Top-left at the used displays' bounding rect, the compact layout's size.
    region: Rect

    @staticmethod
    def of(used: list[Display]) -> _Layout:
        if not used:
            return _Layout([], _Axis(((0.0, 1.0),)), _Axis(((0.0, 1.0),)), Rect(0, 0, 1, 1))
        xs = _Axis.covering((d.rect.left, d.rect.right) for d in used)
        ys = _Axis.covering((d.rect.top, d.rect.bottom) for d in used)
        box = bounding_rect([d.rect for d in used])
        return _Layout(list(used), xs, ys, Rect(box.x, box.y, xs.length, ys.length))

    def desktop_point(self, uv: Point) -> Point:
        """Unit-square coordinates through the compact layout to desktop pixels, clamped into the nearest display."""
        p = Point(self.xs.to_desktop(uv.x * self.xs.length), self.ys.to_desktop(uv.y * self.ys.length))
        return _clamp_into(p, self.used)

    def region_point(self, uv: Point) -> Point:
        return Point(self.region.x + uv.x * self.region.width, self.region.y + uv.y * self.region.height)


class ScreenMapper:
    def __init__(self, displays: list[Display], settings: HandsSettings, homography: np.ndarray | None = None) -> None:
        self._lock = threading.Lock()
        self._settings = settings
        self._displays = list(displays)
        self._selection: DisplaySelection = settings.displays
        self._homography: np.ndarray | None = None
        self._h = box_homography(*settings.box)
        self._gain = float(settings.cursor_speed)
        self._layout = _Layout.of([])
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

    def set_cursor_gain(self, gain: float) -> None:
        """Gain on the cursor around the centre of the unit square (1.0 maps the homography's square as it is)."""
        if not math.isfinite(gain) or gain <= 0:
            raise ValueError("the cursor gain must be a positive finite number")
        with self._lock:
            self._gain = float(gain)

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
        self._layout = _Layout.of(select_displays(self._displays, self._selection))

    # -- state ------------------------------------------------------------------------

    @property
    def displays(self) -> list[Display]:
        with self._lock:
            return list(self._displays)

    @property
    def used(self) -> list[Display]:
        with self._lock:
            return list(self._layout.used)

    @property
    def region(self) -> Rect:
        """What the unit square spans: the used displays' layout with the spans none of them covers taken out."""
        with self._lock:
            return self._layout.region

    @property
    def calibrated(self) -> bool:
        with self._lock:
            return self._homography is not None

    @property
    def cursor_gain(self) -> float:
        with self._lock:
            return self._gain

    @property
    def homography(self) -> np.ndarray:
        """The homography in use (the calibrated one or the default box's)."""
        with self._lock:
            return self._h.copy()

    # -- mapping ----------------------------------------------------------------------

    def to_target(self, anchor: Point) -> Point:
        """Unit-square coordinates of a camera point, with the cursor gain (not clamped)."""
        with self._lock:
            h, gain = self._h, self._gain
        return _gained(apply_homography(h, anchor), gain)

    def to_region(self, anchor: Point) -> Point:
        """Pixels of a camera point in the region, NOT clamped to the displays.

        For relative motion (scrolling, throw speed), which must keep counting
        when the hand overshoots an edge. In the region's compact layout, so it
        moves as many pixels as the cursor does on a display and never jumps
        where a left-out display was. Without the cursor gain: how fast the
        cursor travels does not change how far a scroll goes or how hard a
        flick must be.
        """
        with self._lock:
            h, layout = self._h, self._layout
        return layout.region_point(apply_homography(h, anchor))

    def to_desktop(self, anchor: Point) -> Point:
        """Desktop pixels of a camera point, with the cursor gain, clamped into the nearest used display."""
        with self._lock:
            h, layout, gain = self._h, self._layout, self._gain
        return layout.desktop_point(_gained(apply_homography(h, anchor), gain))

    def target_point(self, u: float, v: float) -> Point:
        """Desktop pixels of unit-square coordinates, clamped like ``to_desktop``."""
        with self._lock:
            layout = self._layout
        return layout.desktop_point(Point(u, v))

    def display_at(self, p: Point) -> Display:
        """The used display containing ``p``, else the nearest used one."""
        with self._lock:
            used = self._layout.used
        if not used:
            raise LookupError("no displays")
        return _nearest(p, used)


def _gained(uv: Point, gain: float) -> Point:
    """``uv`` scaled by ``gain`` around the centre of the unit square (exactly ``uv`` for a gain of 1)."""
    if gain == 1.0:
        return uv
    return Point(0.5 + (uv.x - 0.5) * gain, 0.5 + (uv.y - 0.5) * gain)


def _nearest(p: Point, displays: list[Display]) -> Display:
    for d in displays:
        if d.rect.contains(p):
            return d
    return min(displays, key=lambda d: d.rect.distance_to(p))


def _clamp_into(p: Point, displays: list[Display]) -> Point:
    if not displays:
        return p
    return _nearest(p, displays).rect.clamp(p)
