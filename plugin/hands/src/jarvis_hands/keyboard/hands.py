"""Hand tracking for the keyboard: identity across frames, finger features and levelling.

DESIGN-KEYBOARD.md 2.1, 2.2 and 2.12.1. A ``Frame`` goes in, a ``HandSample`` per usable hand comes out. The tracker
reads landmarks only: a hand's label is copied to ``side`` for one-hand placement and is never used to tell the hands
or the fingers apart, because the engine overwrites it every frame.

Pure: no I/O, no clock (the frame's own time), numpy for the geometry.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Mapping
from types import MappingProxyType

import numpy as np

from ..landmarks import FINGERS, MIDDLE_MCP, THUMB_TIP, WRIST, Frame
from ..poses import pose_points
from .tuning import Tuning
from .types import FingerSample, HandSample, Side

_NAMES = ("index", "middle", "ring", "pinky")
_MCP = np.array([FINGERS[name][0] for name in _NAMES])
_TIP = np.array([FINGERS[name][1] for name in _NAMES])
#: The two joints between a knuckle and its tip sit on the next landmark numbers (landmarks.py).
_PIP, _DIP = _MCP + 1, _MCP + 2
#: Weights of the PIP, DIP and TIP positions in the lift (2.12.1).
_LIFT_WEIGHTS = (0.25, 0.35, 0.40)
#: A hand is ignored whose palm (wrist to middle knuckle) is outside this, in frame widths.
_PALM_RANGE = (0.03, 0.40)
#: A hand whose axis (wrist to middle knuckle) shows less than this share of its 3D palm in the picture has no usable
#: axis: it points within about 14 degrees of the line of sight, where the axis is smaller than the landmark noise and
#: the lift and the levelling, which divide by it, are noise over noise (phantom taps by the dozen at noise 0.002).
_MIN_AXIS = 0.25
#: The anchor speed is read over this long, and over at least this long while the hand is new.
_SPEED_SPAN_S = 0.15
_SPEED_MIN_SPAN_S = 0.05
_SPEED_FRAMES = 3
#: The longest anchor history that is kept, and the longest levelling history.
_ANCHOR_KEEP_S = 0.5
_LEVEL_KEEP_S = 2.0
#: Per-user levels are clamped to this many palms, and need at least this many frames to be measured.
_LEVEL_CLAMP = 0.30
_LEVEL_MIN_FRAMES = 5


class _Track:
    """One hand while it stays in view."""

    __slots__ = ("anchors", "curled", "id", "last_t", "levels", "rel", "side", "speeds", "wrist")

    def __init__(self, hand_id: int, t: float, wrist: np.ndarray, side: Side, levels: tuple[float, ...]) -> None:
        self.id = hand_id
        self.last_t = t
        self.wrist = wrist
        self.side = side
        #: Hysteresis state per finger.
        self.curled = [False] * 4
        self.anchors: deque[tuple[float, np.ndarray]] = deque()
        self.speeds: deque[float] = deque(maxlen=_SPEED_FRAMES)
        #: (t, how far each tip sits above the mean tip along the hand's down axis, in palms): the levelling input.
        self.rel: deque[tuple[float, np.ndarray]] = deque()
        self.levels = levels


class HandTracker:
    def __init__(self, tuning: Tuning) -> None:
        self._tuning = tuning
        self._tracks: list[_Track] = []
        self._next_id = 1
        self._t = 0.0
        self._levels: dict[Side, tuple[float, ...]] = {}

    @property
    def levels(self) -> Mapping[Side, tuple[float, ...]]:
        """The per-user levels learned so far, by side; empty until ``learn_levels`` has measured a hand."""
        return MappingProxyType(self._levels)

    def reset(self) -> None:
        """Forget every track and every learned level. Ids keep counting up: a new hand is never an old id."""
        self._tracks = []
        self._levels = {}

    def update(self, frame: Frame) -> list[HandSample]:
        t = frame.t
        self._t = t
        tuning = self._tuning
        aspect = frame.height / frame.width
        # A track older than the hold time is gone, and so is one from the future (a clock that ran backwards).
        self._tracks = [tr for tr in self._tracks if 0.0 <= t - tr.last_t <= tuning.hand_hold_s]

        seen = []
        for obs in frame.hands:
            points = pose_points(obs.image, aspect)
            points[:, 2] *= tuning.z_scale
            palm = float(np.linalg.norm(points[WRIST] - points[MIDDLE_MCP]))
            # Also every NaN: a comparison with one is False.
            if _PALM_RANGE[0] <= palm <= _PALM_RANGE[1] and np.isfinite(points).all():
                seen.append((obs, points, palm))

        matched = self._match([points[WRIST, :2] for _, points, _ in seen])
        samples = []
        for index, (obs, points, palm) in enumerate(seen):
            track = matched.get(index)
            if track is None:
                track = _Track(
                    self._next_id,
                    t,
                    points[WRIST, :2].copy(),
                    obs.handedness,
                    self._levels.get(obs.handedness, tuning.level_palm),
                )
                self._next_id += 1
                self._tracks.append(track)
            track.last_t = t
            track.wrist = points[WRIST, :2].copy()
            track.side = obs.handedness
            samples.append(self._sample(track, points, palm, float(obs.score), t))
        return samples

    def _match(self, wrists: list[np.ndarray]) -> dict[int, _Track]:
        """Observation index to track: by wrist distance, nearest pair first, within the association radius."""
        pairs = sorted(
            (float(np.hypot(*(wrist - track.wrist))), i, j)
            for i, wrist in enumerate(wrists)
            for j, track in enumerate(self._tracks)
        )
        matched: dict[int, _Track] = {}
        used: set[int] = set()
        for distance, i, j in pairs:
            if distance > self._tuning.associate_radius:
                break
            if i not in matched and j not in used:
                matched[i] = self._tracks[j]
                used.add(j)
        return matched

    def _sample(self, track: _Track, points: np.ndarray, palm: float, score: float, t: float) -> HandSample:
        tuning = self._tuning
        axis = points[MIDDLE_MCP, :2] - points[WRIST, :2]
        length = float(np.hypot(axis[0], axis[1]))
        tips = points[_TIP]

        # The levelled tip (2.2). The design writes the hand's down direction as (-sin t, cos t) with
        # t = atan2(d.x, -d.y), which is the unit vector opposite to d: it rotates with the hand, so tilting the hand
        # does not shear the correction. A hand pointing at the camera has no axis in the picture: no levelling or lift.
        if length > _MIN_AXIS * palm:
            up = axis / length
            down = -up
            offset = np.outer(np.array(track.levels), down) * palm
            heights = tips[:, :2] @ down
            track.rel.append((t, (heights.mean() - heights) / palm))
            while track.rel and t - track.rel[0][0] > _LEVEL_KEEP_S:
                track.rel.popleft()
            along = [((points[joint, :2] - points[_MCP, :2]) @ up) / length for joint in (_PIP, _DIP, _TIP)]
            lifts = sum(w * a for w, a in zip(_LIFT_WEIGHTS, along, strict=True))
        else:
            offset = np.zeros((4, 2))
            lifts = np.zeros(4)
        aims = tips[:, :2] + offset

        wrist = points[WRIST]
        bases = np.linalg.norm(points[_MCP] - wrist, axis=1)
        reaches = np.linalg.norm(tips - wrist, axis=1) / np.where(bases > 1e-9, bases, np.inf)
        ratios = np.linalg.norm(points[THUMB_TIP] - tips, axis=1) / palm
        for f in range(4):
            r = float(reaches[f])
            track.curled[f] = r < tuning.curled_leave if track.curled[f] else r < tuning.curled_enter

        anchor = (points[_MCP[0], :2] + points[_MCP[1], :2]) / 2
        fingers = tuple(
            FingerSample(f, aims[f].copy(), float(reaches[f]), float(ratios[f]), track.curled[f], float(lifts[f]))
            for f in range(4)
        )
        return HandSample(track.id, track.side, t, palm, anchor, self._speed(track, t, anchor), fingers, score)

    @staticmethod
    def _speed(track: _Track, t: float, anchor: np.ndarray) -> float:
        """Anchor travel over the last 0.15 s (the newest sample at least that old), smoothed over 3 frames.

        A hand with less history than that is read over what it has, once that is 0.05 s; before then it is still. Every
        motion gate in the design uses this or ``reach``, never the fingertip's own travel (a finger travels a pitch or
        more by itself in a normal press).
        """
        history = track.anchors
        if not history or t > history[-1][0]:
            history.append((t, anchor))
        elif t < history[-1][0]:  # a clock that went back inside the hold time: the hand starts over
            history.clear()
            track.speeds.clear()
            history.append((t, anchor))
        while len(history) > 2 and t - history[1][0] >= _ANCHOR_KEEP_S:
            history.popleft()
        speed = 0.0
        reference = None
        for then, there in reversed(history):
            if t - then >= _SPEED_SPAN_S:
                reference = (then, there)
                break
        if reference is None and t - history[0][0] >= _SPEED_MIN_SPAN_S:
            reference = history[0]
        if reference is not None:
            speed = float(np.hypot(*(anchor - reference[1]))) / (t - reference[0])
        track.speeds.append(speed)
        return math.fsum(track.speeds) / len(track.speeds)

    def learn_levels(self, window_s: float = 0.6) -> dict[Side, tuple[float, float, float, float]]:
        """Per-user levelling (2.2): measure each hand in view over the last ``window_s`` seconds and keep the result.

        ``level_f = (mean over the four tips of the tip's height along the hand's down axis - this tip's) / palm``,
        averaged over the window, clamped to +-0.30 palms. The levels replace the tuning's defaults for that side from
        the next frame on (and for the hand's track at once), so call it when placing ends, while the hands are still.
        Aims inside the window were made with the old levels; the mean of a hand's four aims, and so the plane, is the
        same either way (the levels sum to about zero), only a finger's own home position moves by a fraction of a key.
        Returns what it set, by side; a hand with fewer than 5 frames in the window is left alone.
        """
        learned: dict[Side, tuple[float, ...]] = {}
        for track in self._tracks:
            recent = [rel for then, rel in track.rel if then >= self._t - window_s]
            if len(recent) < _LEVEL_MIN_FRAMES:
                continue
            mean = np.mean(recent, axis=0)
            levels = tuple(float(min(max(x, -_LEVEL_CLAMP), _LEVEL_CLAMP)) for x in mean)
            track.levels = levels
            learned[track.side] = levels
        self._levels.update(learned)
        return {side: (a, b, c, d) for side, (a, b, c, d) in learned.items()}
