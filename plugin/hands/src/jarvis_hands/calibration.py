"""Four-corner calibration: where the hand is when the cursor should be in each corner.

The overlay shows a target in one corner of the target region at a time
(top-left, top-right, bottom-right, bottom-left). The user holds an open palm
with the anchor (knuckles by default) where that corner should be; once the
palm has stayed still for ``hold_s`` the mean anchor over the hold is that
corner's camera point. The four points give the camera -> unit square
homography (``cv2.getPerspectiveTransform``) that replaces the default box.

A hand still resting on the corner just captured must not capture the next
corner at the same spot, so a hold only starts ``min_separation`` away from
every corner already taken. Corners that fit no usable mapping start the
round over; ``timeout_s`` without progress (a corner further along than any
reached before) cancels, so a round that keeps failing cannot loop forever.
"""

from __future__ import annotations

import logging
from collections import deque

import cv2
import numpy as np

from .geometry import Point
from .poses import HandPose
from .settings import HandsSettings

log = logging.getLogger(__name__)

CORNERS = ("top_left", "top_right", "bottom_right", "bottom_left")
CORNER_TARGETS: dict[str, tuple[float, float]] = {
    "top_left": (0.0, 0.0),
    "top_right": (1.0, 0.0),
    "bottom_right": (1.0, 1.0),
    "bottom_left": (0.0, 1.0),
}

#: Smallest quad accepted, as a fraction of the camera frame's area.
MIN_AREA = 0.01
#: Smallest corner angle accepted (its sine), so three nearly collinear corners are refused.
MIN_CORNER_SINE = 0.1


def fit_homography(camera_points: list[Point]) -> np.ndarray:
    """The homography taking the four camera points (TL, TR, BR, BL) onto the unit square's corners.

    The quad may run either way round: a camera that already mirrors its
    image (so the tracker's flip un-mirrors it) puts the user's top-left on
    the image's right, and the mirror-image fit is exactly what corrects that.
    Raises ValueError when the points cannot define a sensible mapping: not
    four of them, (nearly) collinear, folded (self-intersecting or not
    convex) or covering less than ``MIN_AREA`` of the frame.
    """
    if len(camera_points) != 4:
        raise ValueError(f"need 4 corner points, got {len(camera_points)}")
    src = np.array([[p.x, p.y] for p in camera_points], dtype=np.float64)
    if not np.all(np.isfinite(src)):
        raise ValueError("corner points must be finite")
    x, y = src[:, 0], src[:, 1]
    area = 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(np.roll(x, -1), y))
    if abs(area) < MIN_AREA:
        raise ValueError(f"the corners cover {abs(area):.4f} of the frame; spread them wider")
    # Convex and not folded: every corner turns the same way as the whole quad (the area's sign), sharply enough.
    turn = 1.0 if area > 0 else -1.0
    for i in range(4):
        prev_edge = src[i] - src[i - 1]
        next_edge = src[(i + 1) % 4] - src[i]
        lengths = float(np.linalg.norm(prev_edge) * np.linalg.norm(next_edge))
        cross = float(prev_edge[0] * next_edge[1] - prev_edge[1] * next_edge[0])
        if lengths < 1e-12 or turn * cross / lengths < MIN_CORNER_SINE:
            raise ValueError("the corners are collinear or folded")
    dst = np.array([CORNER_TARGETS[c] for c in CORNERS], dtype=np.float32)
    h = cv2.getPerspectiveTransform(src.astype(np.float32), dst)
    if not np.all(np.isfinite(h)):
        raise ValueError("the corners give no usable mapping")
    return np.asarray(h, dtype=float)


class CalibrationFlow:
    def __init__(
        self,
        settings: HandsSettings,
        *,
        hold_s: float = 1.0,
        still_speed: float = 0.15,
        timeout_s: float = 30.0,
        min_separation: float = 0.08,
        speed_window_s: float = 0.15,
        gap_s: float = 0.3,
    ) -> None:
        self.settings = settings
        self.hold_s = hold_s
        #: Normalized frame units per second, measured over ``speed_window_s``.
        self.still_speed = still_speed
        self.timeout_s = timeout_s
        self.min_separation = min_separation
        self.speed_window_s = speed_window_s
        #: A hand missing for longer than this restarts the hold (a dropped frame does not).
        self.gap_s = gap_s
        self._active = False
        self._points: list[Point] = []
        self._hold: deque[tuple[float, Point]] = deque()
        self._last_seen = 0.0
        self._last_progress = 0.0
        #: The most corners captured in one round since ``start``; only beating it is progress.
        self._furthest = 0
        self._progress = 0.0
        self._result: np.ndarray | None = None

    @property
    def active(self) -> bool:
        return self._active

    @property
    def corner(self) -> str | None:
        """The corner being waited for, or None when not calibrating."""
        return CORNERS[len(self._points)] if self._active else None

    @property
    def progress(self) -> float:
        """Hold progress on the current corner, 0..1."""
        return self._progress if self._active else 0.0

    @property
    def result(self) -> np.ndarray | None:
        """The fitted homography once the flow is done."""
        return self._result

    @property
    def points(self) -> list[Point]:
        """Camera points captured so far (TL, TR, BR, BL order)."""
        return list(self._points)

    def start(self, now: float) -> None:
        self._active = True
        self._points = []
        self._hold.clear()
        self._progress = 0.0
        self._result = None
        self._last_progress = now
        self._furthest = 0
        self._last_seen = now

    def cancel(self) -> None:
        self._active = False
        self._hold.clear()
        self._progress = 0.0

    def update(self, pose: HandPose | None, now: float) -> list[str]:
        """Feed the calibrating hand's pose (None when no hand). Returns the steps reached this frame."""
        if not self._active:
            return []
        if now - self._last_progress > self.timeout_s:
            log.info("calibration timed out on %s", self.corner)
            self.cancel()
            return ["cancelled"]
        if pose is None:
            if self._hold and now - self._last_seen > self.gap_s:
                self._restart_hold()
            return []
        self._last_seen = now
        anchor = pose.anchor
        if pose.pose != "palm" or any(anchor.distance(p) < self.min_separation for p in self._points):
            self._restart_hold()
            return []
        self._hold.append((now, anchor))
        if self._moving(now):
            self._restart_hold()
            self._hold.append((now, anchor))
        held = now - self._hold[0][0]
        self._progress = min(1.0, held / self.hold_s) if self.hold_s > 0 else 1.0
        if held < self.hold_s:
            return []
        return self._capture(now)

    def _moving(self, now: float) -> bool:
        newest_t, newest = self._hold[-1]
        reference: tuple[float, Point] | None = None
        for t, p in reversed(self._hold):
            reference = (t, p)
            if newest_t - t >= self.speed_window_s:
                break
        if reference is None or newest_t - reference[0] <= 1e-6:
            return False
        return newest.distance(reference[1]) / (newest_t - reference[0]) > self.still_speed

    def _restart_hold(self) -> None:
        self._hold.clear()
        self._progress = 0.0

    def _capture(self, now: float) -> list[str]:
        n = len(self._hold)
        mean = Point(sum(p.x for _, p in self._hold) / n, sum(p.y for _, p in self._hold) / n)
        self._points.append(mean)
        self._restart_hold()
        if len(self._points) > self._furthest:
            self._furthest = len(self._points)
            self._last_progress = now
        if len(self._points) < len(CORNERS):
            return [CORNERS[len(self._points)]]
        try:
            self._result = fit_homography(self._points)
        except ValueError as exc:
            log.warning("calibration corners rejected (%s); starting over", exc)
            self._points = []
            return [CORNERS[0]]
        self._active = False
        return ["done"]
