"""The gesture state machine: tracked hands in, desktop actions out.

``GestureEngine.update(frame)`` is pure apart from the engine's own state:
the clock is the frame's capture time (or ``now``), so tests script whole
interactions frame by frame. The runtime hands the returned actions to the
executor, drains ``take_events()`` into protocol events and draws ``view()``.

How a frame flows:

1. **Tracks.** Each observed hand is matched to a track (nearest knuckle point
   within ``ASSOCIATE_RADIUS`` frame widths, else the same handedness). Every
   distance (that match, the engagement stillness, a press's slop) is measured
   on the knuckles, which barely move when the fingers do; only the cursor
   target follows ``anchor``, which may be the index fingertip instead (then a
   press is rewound on the knuckles, and what is held follows them: the tip
   travels as the finger bends into the pose, see ``_aimed``). Nearest is
   decided over all tracks at once, not track by track: when the pointer hand
   is missed for a frame, the other hand stays on its own track instead of
   being taken over by the pointer's. Each track keeps its own pose hysteresis
   and a debounce: a raw pose must repeat for ``confirm_frames`` frames before
   it is *confirmed*, which is what the engine acts on. The debounce remembers
   when and where the pose's run began, so a press can undo the motion that
   the pinch itself caused ("rewind") and measure its slop from where the
   pinch started. A hand back after more than ``hold_s`` out of view starts
   over (pose, debounce, stillness), and frames a hand misses never count
   towards the engagement hold.
2. **Engagement.** Disengaged, nothing moves; a still open palm held for
   ``engage_s`` (or any hand at once with ``engage: always``, or the next hand
   after a voice ``engage()``) makes that hand the pointer.
3. **Engaged.** The pointer's anchor is mapped to desktop pixels, smoothed
   with the One Euro filter and a dead zone, and the confirmed pose drives a
   small mode machine: point, press/drag (left or right), scroll, grab and
   two-hand resize. A frame whose RAW pose already left the active pose
   neither moves nor drags, so the opening hand's own drift never lands in a
   drop, a drag or a window.
4. **Pinches.** A hand closing into a fist, or opening out of one, passes
   through a pinch, which must not click, and so does one finger curling or
   uncurling on its own past the thumb. So a pinch's button goes down only
   once the other fingers have settled (``SETTLE_RATE``) and its own finger
   too (``SETTLE_FINGER_RATE``); a pinch let go
   before that is a quick tap and clicks whole as it ends, unless its finger
   curled on the way in or out (``_tap_verdict``). After a grab the pinches
   stay latched until the hand is open (``_let_go``).

A pose that ends an interaction abnormally (hand lost, calibration) is
latched: it must be let go before it can start the same interaction again,
so a fist that comes back into view does not grab whatever is under it.
"""

from __future__ import annotations

import logging
import math
import threading
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Literal, TypeVar

import numpy as np

from .actions import (
    Action,
    Button,
    DragWindow,
    GrabWindow,
    MouseButton,
    MoveCursor,
    ReleaseAll,
    ReleaseWindow,
    ResizeWindow,
    Scroll,
    ThrowWindow,
)
from .calibration import CORNER_TARGETS, CalibrationFlow
from .filters import OneEuroFilter2D
from .geometry import Point
from .landmarks import Frame, HandObservation
from .mapping import ScreenMapper
from .poses import HandPose, PoseName, PoseTracker, anchor_point
from .settings import HandsSettings

log = logging.getLogger(__name__)

EngineState = Literal["idle", "active", "calibrating"]
EventKind = Literal["gesture", "calibration", "state"]
Mode = Literal["point", "press", "drag", "scroll", "grab", "resize"]

#: Frame widths: how far a hand may move between frames and still be the same track.
ASSOCIATE_RADIUS = 0.25
#: Frame widths per second: when ranking matches, a track's last anchor counts as this much further away
#: for every second the hand has been out of view, so a hand that left a while ago does not take the
#: observation of the pointer passing over the spot where it was last seen.
STALE_FW_PER_S = 1.0
#: Seconds of cursor and motion history kept (rewind, throw speed).
HISTORY_S = 1.0
#: Seconds over which a track's anchor speed is measured (engagement stillness).
SPEED_WINDOW_S = 0.1
#: A pinch presses only once the fingers that take no part in it have settled: their mean reach moves less
#: than ``SETTLE_RATE`` (reach units per second; the least-squares slope over the last ``SETTLE_WINDOW_S``,
#: frames from before the pinch included). A hand closing into a fist, or opening out of one, passes through
#: a pinch (the thumb crosses the index tip while the index is not curled yet) with those fingers on the
#: move, while a hand that means to pinch holds them.
#: The numbers: through the real tracker, on still photos with webcam sensor noise (4 to 12 grey levels),
#: that slope has a median of 0.05 to 0.10 and a p90 of 0.12 to 0.26, so a held pinch presses within a frame
#: of the window clearing; a palm closing into a fist within 4.5 s, or a relaxed hand within 1.5 s,
#: moves faster than the rate for the whole of the pinch it passes through, so the press is never sent.
#: The window is what a held pinch's press costs: about ``SETTLE_WINDOW_S`` after the fingers stop.
#: A pinch let go before that is a quick tap, which clicks as it ends (see ``_tap_verdict``).
SETTLE_WINDOW_S = 0.1
SETTLE_RATE = 0.3
#: The pinching finger must hold still too: its own reach moves less than ``SETTLE_FINGER_RATE`` over the
#: pinch's frames within that window (frames from before the pinch left out: the finger moved to get there).
#: One finger curling into a fist, or uncurling out of one, past the thumb tucked against the curled fingers
#: (pointing into a fist, the scroll pose into pointing and back) passes through a pinch while the others hold
#: still all along.
#: The numbers: a still finger through the real tracker (the same photos and noise) has a p99 of 0.1 to 0.4,
#: the synthetic hands' with 0.001 frame widths of jitter 0.55 to 0.8; a finger moving at a real finger's pace
#: (slow, fast, slow) past a thumb resting on the curled middle finger is at 1.4 to 2.9 where it is slowest
#: in the pinch (medians over moves of 0.8 to 1.25 s, built from MediaPipe's pointing, V-sign and fist photos).
#: So a held pinch still presses within about ``SETTLE_WINDOW_S`` of its fingers stopping (now and then a
#: frame later under heavy noise), and such a move presses now and then only once it takes a second or more.
SETTLE_FINGER_RATE = 1.0
#: The slope is only taken over at least this long and three frames (two frame intervals), and the pinch
#: itself must have lasted as long: over one interval, landmark noise alone can make a closing hand look still.
SETTLE_MIN_S = 0.05
#: A pinch whose own finger was curled this shortly before it began, or curled while it lasted, is a hand
#: opening out of a fist or closing into one, never a tap (a pinch needs that finger not curled).
TRANSIT_LOOKBACK_S = 0.15
#: A tap that ends while the other fingers are still closing may be on its way into a fist: its click waits
#: this long for the fist (which drops it) before it goes out.
TAP_WAIT_S = 0.15
#: After a grab ends, the pinch a fist passes through as it opens stays latched until the hand shows a palm,
#: or another pose that is no action for this long: the hover between the fist and that pinch is shorter.
OPENING_LATCH_S = 0.2
#: With ``anchor: index``, how far back a press looks for where the finger began to bend into it, and how
#: much (reach units) the index must have straightened again, going back, to mark that start.
BEND_LOOKBACK_S = 0.8
BEND_TOLERANCE = 0.03
#: Wheel units per desktop pixel of hand motion, before ``scroll_speed``.
SCROLL_UNITS_PER_PX = 2.4
#: A throw (and a scroll axis) must be this much more along one axis than the other.
AXIS_DOMINANCE = 1.5
MAX_TRACKS = 4
ACTION_POSES: frozenset[str] = frozenset({"pinch", "pinch_middle", "fist", "two"})
#: Latched when a grab ends, until the hand shows it is open (``_let_go``): what a fist opening passes through.
_OPENING_FIST: frozenset[PoseName] = frozenset({"pinch", "pinch_middle"})


@dataclass(frozen=True)
class EngineEvent:
    kind: EventKind
    #: A GestureName (gesture), a CalibrationStep (calibration) or an engine state (state).
    value: str


@dataclass(frozen=True)
class EngineView:
    state: EngineState
    hand_visible: bool
    #: Filtered pointer target in desktop px; also while disengaged when a hand is visible (the faint ring).
    cursor: Point | None
    #: The helper hand's point while resizing.
    helper: Point | None
    pose: str
    pinch: float
    engage_progress: float
    grabbing: bool
    scrolling: bool
    pressed: bool
    calibration_target: Point | None
    calibration_progress: float


_S = TypeVar("_S")


def _finger(pose: PoseName) -> int:
    """The finger (index 0, middle 1) the thumb pinches in ``pose``."""
    return 1 if pose == "pinch_middle" else 0


def _others(pose: PoseName) -> tuple[int, ...]:
    """The fingers that take no part in ``pose``: all but ``_finger(pose)``."""
    return (0, 2, 3) if pose == "pinch_middle" else (1, 2, 3)


class _Track:
    """One hand followed across frames: hysteresis, debounce and recent anchors."""

    def __init__(self, track_id: int, now: float) -> None:
        self.id = track_id
        self.poses = PoseTracker()
        self.handedness = "right"
        self.seen = False
        self.last_seen = now
        #: The cursor anchor in image coordinates (what the mapper takes): the knuckles, or the index
        #: fingertip with ``anchor: index``.
        self.anchor = Point(0.5, 0.5)
        #: The knuckles in frame-width units, which is what every distance is measured on (see ``_associate``).
        self.anchor_fw = Point(0.5, 0.5)
        #: The knuckles in image coordinates (the cursor anchor itself, unless ``anchor: index``).
        self.knuckles = Point(0.5, 0.5)
        self.hand_pose: HandPose | None = None
        self.raw: PoseName | None = None
        self.run = 0
        self.run_start_t = now
        self.run_start_anchor = Point(0.5, 0.5)
        self.confirmed: PoseName | None = None
        #: When and where the confirmed pose's run began (its first raw frame).
        self.confirmed_t = now
        self.confirmed_anchor = Point(0.5, 0.5)
        self.history: deque[tuple[float, Point]] = deque()
        #: Each frame's finger reach (index, middle, ring, pinky), over the same span as ``history``.
        self.reach: deque[tuple[float, tuple[float, float, float, float]]] = deque()
        #: When each finger (index, middle, ring, pinky) was last seen curled.
        self.curled_t = [-math.inf] * 4
        self.palm_since: float | None = None

    def restart(self, now: float) -> None:
        """The hand was out of view for a while: nothing seen before the gap counts any more."""
        self.poses.reset()
        self.raw, self.run, self.confirmed = None, 0, None
        self.run_start_t = self.confirmed_t = now
        self.history.clear()
        self.reach.clear()
        self.curled_t = [-math.inf] * 4
        self.palm_since = None

    def observe(self, pose: HandPose, now: float, knuckles: Point, anchor_fw: Point, confirm_frames: int) -> None:
        self.hand_pose = pose
        self.anchor = pose.anchor
        self.knuckles = knuckles
        self.anchor_fw = anchor_fw
        self.history.append((now, anchor_fw))
        self.reach.append((now, pose.reach))
        for hist in (self.history, self.reach):
            while hist and now - hist[0][0] > HISTORY_S:
                hist.popleft()
        for i, curled in enumerate(pose.curled):
            if curled:
                self.curled_t[i] = now
        if pose.pose != self.raw:
            self.raw, self.run, self.run_start_t, self.run_start_anchor = pose.pose, 1, now, anchor_fw
        else:
            self.run += 1
        if self.run >= max(1, confirm_frames) and self.raw != self.confirmed:
            self.confirmed = self.raw
            self.confirmed_t, self.confirmed_anchor = self.run_start_t, self.run_start_anchor

    def speed(self) -> float:
        """Frame widths per second over the last ``SPEED_WINDOW_S``."""
        if len(self.history) < 2:
            return 0.0
        t1, p1 = self.history[-1]
        t0, p0 = _window_start(self.history, t1, SPEED_WINDOW_S)
        return p1.distance(p0) / (t1 - t0) if t1 - t0 > 1e-6 else 0.0

    def settled(self, pose: PoseName, began: float, since: float = -math.inf) -> bool:
        """Whether the fingers that take no part in ``pose`` (a pinch that ``began`` then), and its own, hold still.

        The slope over the last ``SETTLE_WINDOW_S``, frames from before the
        pinch included: a hand that closed into the pinch has to stop before
        it counts, and a frame of noise inside the pinch does not start the
        measure over. The pinch itself must have lasted three frames (two
        frame intervals) and ``SETTLE_MIN_S``, so a slow closing whose noise
        happens to average out does not press on the frame its pinch is
        confirmed. Never back past
        ``since`` (the last frame the pinching finger was curled): the way down
        into a fist and back out of it could average out to no slope at all.
        The pinching finger's own slope (``SETTLE_FINGER_RATE``) is taken over
        the pinch's frames only.
        """
        pinch = [t for t, _ in self.reach if t >= began]
        if len(pinch) < 3 or pinch[-1] - pinch[0] < SETTLE_MIN_S - 1e-6:
            return False
        slope = self._reach_slope(_others(pose), since)
        if slope is None or abs(slope) > SETTLE_RATE:
            return False
        own = self._reach_slope((_finger(pose),), max(since, began))
        return own is not None and abs(own) <= SETTLE_FINGER_RATE

    def closing(self, pose: PoseName) -> bool:
        """Whether the fingers that take no part in ``pose`` are curling faster than ``SETTLE_RATE``."""
        slope = self._reach_slope(_others(pose), -math.inf)
        return slope is not None and slope < -SETTLE_RATE

    def _reach_slope(self, fingers: tuple[int, ...], since: float) -> float | None:
        """Least-squares slope (reach units per second) of those fingers' mean reach; None without enough frames."""
        if not self.reach:
            return None
        end = self.reach[-1][0]
        window = [(t, _mean(r, fingers)) for t, r in self.reach if t >= since and end - t <= SETTLE_WINDOW_S + 1e-6]
        if len(window) < 3 or end - window[0][0] < SETTLE_MIN_S - 1e-6:
            return None
        t_mean = sum(t for t, _ in window) / len(window)
        v_mean = sum(v for _, v in window) / len(window)
        spread = sum((t - t_mean) ** 2 for t, _ in window)
        return sum((t - t_mean) * (v - v_mean) for t, v in window) / spread


def _at(history: Iterable[tuple[float, Point]], t: float) -> Point:
    """The latest sample of ``history`` not after ``t`` (else its oldest)."""
    point: Point | None = None
    for when, p in history:
        if point is not None and when > t:
            break
        point = p
    assert point is not None
    return point


def _mean(reach: tuple[float, float, float, float], fingers: tuple[int, ...]) -> float:
    return sum(reach[i] for i in fingers) / len(fingers)


def _window_start(history: Iterable[tuple[float, _S]], end: float, window: float) -> tuple[float, _S]:
    """The newest sample at least ``window`` seconds before ``end``, else the oldest one."""
    start: tuple[float, _S] | None = None
    for sample in reversed(list(history)):
        start = sample
        if end - sample[0] >= window:
            break
    assert start is not None
    return start


@dataclass(frozen=True)
class _Press:
    button: MouseButton
    pose: PoseName
    point: Point
    anchor: Point
    t: float
    double: bool
    #: Where the pinch began (the first frame of the run it was confirmed on): its settling is measured from here.
    since: float
    #: Whether the button went down: a pinch waits for the hand to settle into it (see ``SETTLE_RATE``), and
    #: one that ends before it does is either a quick tap or a hand passing through a pinch (``_tap_verdict``).
    down: bool = False
    #: When a pinch that never went down ended in a pose that is no action, while its tap waits (``TAP_WAIT_S``).
    ended: float | None = None


@dataclass(frozen=True)
class _Click:
    button: MouseButton
    point: Point
    t: float


class GestureEngine:
    def __init__(
        self,
        settings: HandsSettings,
        mapper: ScreenMapper,
        *,
        double_click: tuple[float, int, int] = (0.5, 4, 4),
    ) -> None:
        self.settings = settings
        self.mapper = mapper
        #: The system double-click time (s) and rectangle (px), from ``Desktop.double_click()``.
        self.double_click = double_click
        self._lock = threading.RLock()
        self._now = 0.0
        self._tracks: list[_Track] = []
        self._next_track_id = 1
        self._events: list[EngineEvent] = []
        self._pending: list[Action] = []
        self._state: EngineState = "idle"
        self._engaged = False
        self._pointer: _Track | None = None
        self._candidate: _Track | None = None
        self._engage_requested = False
        #: After a command or real-mouse disengage, ``engage: always`` waits for the hands to leave first (or for
        #: a break in tracking, after which they cannot be told from new ones).
        self._always_blocked = False
        #: Set by ``reset_tracks``; the next frame starts the tracks over on its own clock.
        self._tracks_broken = False
        #: Tracks whose palm hold does not count until they let go (a real-mouse or command disengage).
        self._palm_blocked: set[int] = set()
        self._engage_floor = -math.inf
        self._last_hand_t = -math.inf
        self._hand_visible = False
        self._cursor_filter = self._new_filter()
        self._motion_filter = self._new_filter()
        self._helper_filter = self._new_filter()
        self._cursor: Point | None = None
        self._cursor_hist: deque[tuple[float, Point]] = deque()
        self._motion: Point | None = None
        self._motion_hist: deque[tuple[float, Point]] = deque()
        #: With ``anchor: index``, the knuckles' filtered desktop point too: what a press is rewound on, and
        #: what a held press, drag or grab follows (at ``_held_offset`` from it), since the fingertip itself
        #: travels as the finger bends into the pose.
        self._knuckle_filter = self._new_filter()
        self._knuckle: Point | None = None
        self._knuckle_hist: deque[tuple[float, Point]] = deque()
        self._held_offset: Point | None = None
        self._reset_cursor = False
        self._mode: Mode = "point"
        #: Action poses that must be let go (confirmed as some other pose) before they act again.
        self._latched: frozenset[PoseName] = frozenset()
        self._press: _Press | None = None
        self._last_click: _Click | None = None
        self._scroll_prev: Point | None = None
        self._scroll_acc = [0.0, 0.0]
        self._helper: _Track | None = None
        self._helper_point: Point | None = None
        self._calibration = CalibrationFlow(settings)
        self._calibration_result: np.ndarray | None = None

    # -- public API -----------------------------------------------------------------------

    @property
    def engaged(self) -> bool:
        return self._engaged

    @property
    def state(self) -> EngineState:
        return self._state

    @property
    def calibration_result(self) -> np.ndarray | None:
        """The homography fitted by the last finished calibration, until ``take_calibration_result``."""
        return self._calibration_result

    def take_calibration_result(self) -> np.ndarray | None:
        with self._lock:
            result, self._calibration_result = self._calibration_result, None
            return result

    def update(self, frame: Frame, now: float | None = None) -> list[Action]:
        with self._lock:
            now = frame.t if now is None else now
            previous, self._now = self._now, now
            actions, self._pending = self._pending, []
            self._associate(frame, now, previous)
            if self._calibration.active:
                self._calibration_frame(now)
            elif self._engaged:
                self._engaged_frame(now, actions)
            else:
                self._idle_frame(now, actions)
            self._sync_state()
            return actions

    def view(self) -> EngineView:
        with self._lock:
            track = self._pointer if self._engaged else self._candidate
            hand_pose = track.hand_pose if track is not None and track.seen else None
            calibrating = self._calibration.active
            target = None
            if calibrating and self._calibration.corner is not None:
                target = self.mapper.target_point(*CORNER_TARGETS[self._calibration.corner])
            pose = "none"
            if track is not None and track.seen:
                pose = track.confirmed or "hover"
            return EngineView(
                state=self._state,
                hand_visible=self._hand_visible,
                cursor=None if calibrating else self._cursor,
                helper=self._helper_point if self._mode == "resize" else None,
                pose=pose,
                pinch=hand_pose.pinch if hand_pose is not None else 0.0,
                engage_progress=1.0 if self._engaged else self._engage_progress(self._candidate),
                grabbing=self._mode in ("grab", "resize"),
                scrolling=self._mode == "scroll",
                pressed=self._mode in ("press", "drag"),
                calibration_target=target,
                calibration_progress=self._calibration.progress,
            )

    def take_events(self) -> list[EngineEvent]:
        with self._lock:
            events, self._events = self._events, []
            return events

    def engage(self) -> None:
        """Voice ``engage``: the next visible hand (of the preferred side) becomes the pointer at once."""
        with self._lock:
            if not self._engaged:
                self._engage_requested = True
                self._always_blocked = False

    def reset_tracks(self) -> None:
        """Tracking broke: the next frame starts over, so engaging takes the full hold again.

        The camera was closed and reopened (a pause and a resume), or another
        desktop had the input (the lock screen, a UAC prompt). Either way no
        frames came for a while, which the engine cannot tell from a hand that
        never moved: without this, a palm still up when the gap began would
        engage on the first frame after it, with no hold and no stillness
        check. Called from another thread than ``update``, and the engine only
        ever keeps time with the frames, so the next frame's own capture time
        is the new floor.
        """
        with self._lock:
            self._tracks_broken = True

    def disengage(self, reason: str = "command") -> list[Action]:
        """Let go of everything and stop following the hand.

        Called from another thread than ``update``, so the batch ``update``
        returned just before (say, a press) may reach the executor after the
        ``ReleaseAll`` returned here. The next ``update`` therefore starts with
        a second ``ReleaseAll``, which lifts anything such a stale batch held.
        """
        with self._lock:
            self._engage_requested = False
            actions = self._disengage(reason)
            self._pending.extend(actions)
            self._sync_state()
            return actions

    def on_user_input(self) -> list[Action]:
        """The real mouse moved: release everything and disengage. Touching the mouse always wins."""
        with self._lock:
            self._engage_requested = False
            actions = self._disengage("user_input")
            self._pending.extend(actions)
            self._sync_state()
            return actions

    def start_calibration(self, now: float) -> None:
        with self._lock:
            if self._holding():
                self._pending.append(ReleaseAll())
            self._end_mode(latch=True)
            self._calibration_result = None
            self._calibration.start(now)
            self._emit("calibration", self._calibration.corner or "top_left")
            self._sync_state()

    def cancel_calibration(self) -> None:
        with self._lock:
            if not self._calibration.active:
                return
            self._calibration.cancel()
            self._emit("calibration", "cancelled")
            self._calibration_ended()
            self._sync_state()

    # -- tracks -------------------------------------------------------------------------

    def _associate(self, frame: Frame, now: float, previous: float) -> None:
        if self._tracks_broken:
            self._break_tracks(now)
        aspect = frame.height / frame.width if frame.width else 1.0
        observed: list[tuple[HandObservation, Point, Point]] = []
        for hand in frame.hands:
            # Distances are always measured on the knuckles, even where the cursor follows the index
            # fingertip (``anchor: index``): the fingertip is the point that travels as a pinch closes, so a
            # press measured on it would cross the slop and turn every click into a drag.
            knuckles = anchor_point(hand.image)
            observed.append((hand, Point(knuckles.x, knuckles.y * aspect), knuckles))
        for track in self._tracks:
            track.seen = False

        order = sorted(
            self._tracks,
            key=lambda t: (t is not self._pointer, t is not self._helper, -t.last_seen),
        )
        free = set(range(len(observed)))
        matches: dict[int, int] = {}

        # Nearest first over every (track, hand) pair, so a hand stays on the track it is right on top of
        # even when another track (the pointer, missed this frame) also has it within reach.
        pairs: list[tuple[float, int, int, _Track]] = []
        for rank, track in enumerate(order):
            stale = STALE_FW_PER_S * max(0.0, now - track.last_seen)
            for i, (_, anchor_fw, _) in enumerate(observed):
                distance = anchor_fw.distance(track.anchor_fw)
                if distance <= ASSOCIATE_RADIUS:
                    pairs.append((distance + stale, rank, i, track))
        for _, _, i, track in sorted(pairs, key=lambda pair: pair[:3]):
            if track.id not in matches and i in free:
                matches[track.id] = i
                free.discard(i)
        for track in order:
            options = [i for i in sorted(free) if observed[i][0].handedness == track.handedness]
            if track.id not in matches and options:
                best = min(options, key=lambda i: observed[i][1].distance(track.anchor_fw))
                matches[track.id] = best
                free.discard(best)
        for i in sorted(free):
            track = _Track(self._next_track_id, now)
            self._next_track_id += 1
            self._tracks.append(track)
            matches[track.id] = i

        confirm = self.settings.confirm_frames
        for track in self._tracks:
            i = matches.get(track.id)
            if i is None:
                continue
            # How long frames came without this hand (a stalled camera delivers no frames: that is not missing).
            missed = previous - track.last_seen
            if missed > self.settings.hold_s:
                track.restart(now)
                self._palm_blocked.discard(track.id)
            elif missed > 0 and track.palm_since is not None:
                # The hold counts the time the palm was in view, not the frames it was missed in.
                track.palm_since += missed
            hand, anchor_fw, knuckles = observed[i]
            track.seen, track.last_seen, track.handedness = True, now, hand.handedness
            pose = track.poses.classify(hand, self.settings.anchor, aspect)
            track.observe(pose, now, knuckles, anchor_fw, confirm)

        keep = [t for t in self._tracks if t.seen or t is self._pointer or now - t.last_seen <= self.settings.lost_s]
        keep.sort(key=lambda t: (t is not self._pointer, not t.seen, -t.last_seen))
        self._tracks = keep[:MAX_TRACKS]
        if self._helper is not None and self._helper not in self._tracks:
            self._helper = None
        if self._candidate is not None and self._candidate not in self._tracks:
            self._candidate = None

        self._hand_visible = bool(observed)
        if observed:
            if self._always_blocked and now - self._last_hand_t >= self.settings.hold_s:
                self._always_blocked = False
            self._last_hand_t = now

    def _break_tracks(self, now: float) -> None:
        """The first frame after a break in tracking (see ``reset_tracks``), on the frame clock."""
        self._tracks_broken = False
        for track in self._tracks:
            track.restart(now)
            track.last_seen = now
        self._candidate = None
        # The hands up when the gap began cannot be told from new ones, so none is held back: engage: always
        # takes the next hand at once again, after a pause as after the lock screen (whose disengage blocks none).
        self._palm_blocked = set()
        self._always_blocked = False
        self._engage_floor = self._last_hand_t = now
        self._cursor = None
        self._cursor_hist.clear()
        self._motion_hist.clear()
        self._knuckle_hist.clear()
        self._reset_cursor = True

    def _seen(self) -> list[_Track]:
        return [t for t in self._tracks if t.seen]

    def _allowed(self, track: _Track) -> bool:
        return self.settings.hand == "any" or track.handedness == self.settings.hand

    # -- disengaged ---------------------------------------------------------------------

    def _idle_frame(self, now: float, actions: list[Action]) -> None:
        seen = self._seen()
        allowed = [t for t in seen if self._allowed(t)]
        if allowed and (self._engage_requested or (self.settings.engage == "always" and not self._always_blocked)):
            self._engage(allowed[0], now, actions)
            return

        for track in seen:
            if track.confirmed != "palm":
                track.palm_since = None
                self._palm_blocked.discard(track.id)  # it let go: the next palm hold counts again
                continue
            if track.id in self._palm_blocked:
                track.palm_since = None
                continue
            if track.palm_since is None:
                track.palm_since = max(track.confirmed_t, self._engage_floor)
            if track.speed() > self.settings.engage_max_speed:
                track.palm_since = now
        best = max(allowed, key=self._engage_progress, default=None)
        if best is not None and self._engage_progress(best) >= 1.0:
            self._engage(best, now, actions)
            return

        candidate = best or (seen[0] if seen else None)
        if candidate is not self._candidate:
            self._candidate = candidate
            self._cursor_filter = self._new_filter()
        if candidate is None:
            self._cursor = None
            return
        self._cursor = self._cursor_filter.filter(self.mapper.to_desktop(candidate.anchor), now)

    def _engage_progress(self, track: _Track | None) -> float:
        if track is None or not track.seen or track.palm_since is None or not self._allowed(track):
            return 0.0
        if self.settings.engage_s <= 0:
            return 1.0
        return min(1.0, max(0.0, (self._now - track.palm_since) / self.settings.engage_s))

    def _engage(self, track: _Track, now: float, actions: list[Action]) -> None:
        self._engaged = True
        self._pointer = track
        self._candidate = None
        self._engage_requested = False
        self._always_blocked = False
        self._mode = "point"
        self._press = None
        # A pose already held when engaging must be let go first; a one-frame flicker of the raw pose
        # must not hide a confirmed fist, so both count.
        self._latched = frozenset(p for p in (track.raw, track.confirmed) if p is not None and p in ACTION_POSES)
        if "fist" in self._latched:
            self._latched |= _OPENING_FIST  # the fist opens through a pinch
        for t in self._tracks:
            t.palm_since = None
        self._restart_cursor(track, now)
        assert self._cursor is not None
        actions.append(MoveCursor(self._cursor.x, self._cursor.y))
        self._emit("gesture", "engage")
        log.info("engaged (%s hand)", track.handedness)

    def _restart_cursor(self, track: _Track, now: float) -> None:
        """Start the filters at the hand's current point, so nothing glides in from a stale position."""
        self._cursor_filter = self._new_filter()
        self._motion_filter = self._new_filter()
        self._knuckle_filter = self._new_filter()
        self._cursor = self._cursor_filter.filter(self.mapper.to_desktop(track.anchor), now)
        self._motion = self._motion_filter.filter(self.mapper.to_region(track.anchor), now)
        self._knuckle = None
        if self.settings.anchor != "knuckles":
            self._knuckle = self._knuckle_filter.filter(self.mapper.to_desktop(track.knuckles), now)
        self._cursor_hist.clear()
        self._motion_hist.clear()
        self._knuckle_hist.clear()
        self._record(now)
        self._reset_cursor = False

    def _disengage(self, reason: str) -> list[Action]:
        if not self._engaged:
            return []
        actions: list[Action] = [ReleaseAll()] if self._holding() else []
        self._end_mode(latch=False)
        self._engaged = False
        self._pointer = None
        self._helper = None
        self._latched = frozenset()
        self._engage_floor = self._now
        for t in self._tracks:
            t.palm_since = None
        if reason in ("command", "user_input"):
            self._always_blocked = True
            # "Touching the mouse always wins": the hand that was up must stop being a confirmed palm (or
            # leave view for hold_s, which restarts its track) before a palm hold counts again. Otherwise a
            # hand left open in view re-engages every engage_s and yanks the cursor away from the real mouse.
            self._palm_blocked = {t.id for t in self._tracks if "palm" in (t.raw, t.confirmed)}
        self._emit("gesture", "user_input" if reason == "user_input" else "disengage")
        log.info("disengaged (%s)", reason)
        return actions

    # -- engaged ------------------------------------------------------------------------

    def _engaged_frame(self, now: float, actions: list[Action]) -> None:
        ptr = self._pointer
        assert ptr is not None
        if not ptr.seen:
            gap = now - ptr.last_seen
            if gap > self.settings.lost_s:
                actions.extend(self._disengage("lost"))
            elif gap > self.settings.hold_s and self._mode != "point":
                if self._holding():
                    actions.append(ReleaseAll())
                self._end_mode(latch=True)
            return

        if self._reset_cursor or self._cursor is None:
            self._restart_cursor(ptr, now)
        filtered = self._cursor_filter.filter(self.mapper.to_desktop(ptr.anchor), now)
        self._motion = self._motion_filter.filter(self.mapper.to_region(ptr.anchor), now)
        if self.settings.anchor != "knuckles":
            self._knuckle = self._knuckle_filter.filter(self.mapper.to_desktop(ptr.knuckles), now)
            if self._holding() and self._held_offset is not None:
                filtered = self._knuckle + self._held_offset
        target = self._cursor if filtered.distance(self._cursor) <= self.settings.dead_zone_px else filtered

        if (
            self._latched
            and ptr.confirmed is not None
            and ptr.confirmed not in self._latched
            and self._let_go(ptr, now)
        ):
            self._latched = frozenset()
        pose: PoseName = ptr.confirmed or "hover"
        self._step(ptr, pose, target, now, actions)
        self._record(now)

    def _let_go(self, ptr: _Track, now: float) -> bool:
        """Whether the confirmed pose, outside the latched ones, lets go of them.

        Any such pose does, except after a fist (``_OPENING_FIST``): opening
        passes through a hover on its way to the pinch, so only a palm, another
        action or a pose held for ``OPENING_LATCH_S`` tells the hand is open.
        """
        pose = ptr.confirmed
        if not self._latched >= _OPENING_FIST or pose == "palm" or pose in ACTION_POSES:
            return True
        return now - ptr.confirmed_t >= OPENING_LATCH_S

    def _step(self, ptr: _Track, pose: PoseName, target: Point, now: float, actions: list[Action]) -> None:
        mode = self._mode
        if mode in ("press", "drag"):
            press = self._press
            assert press is not None
            if pose == press.pose and press.ended is None:
                if ptr.raw == press.pose and (press.down or self._press_down(ptr, actions)):
                    if mode == "press" and ptr.anchor_fw.distance(press.anchor) > self.settings.slop:
                        self._mode = "drag"
                        self._emit("gesture", "drag_start")
                    if self._mode == "drag":
                        self._move(target, actions)
                return
            if press.down:
                self._release_press(actions)
            else:
                verdict = self._tap_verdict(ptr, press, pose, now)
                if verdict == "wait":
                    if press.ended is None:
                        self._press = replace(press, ended=now)
                    return  # the cursor stays on the pinch's point until the tap is decided
                if verdict == "click":
                    self._press_down(ptr, actions, settled=True)
                    self._release_press(actions)
                else:  # the hand passed through a pinch on its way into or out of a fist: nothing was pressed
                    self._press = None
                    self._mode = "point"
        elif mode == "scroll":
            if pose == "two":
                if ptr.raw == "two":
                    self._scroll(actions)
                self._scroll_prev = self._motion
                return
            self._mode = "point"
        elif mode in ("grab", "resize"):
            if pose == "fist":
                self._hold_grab(ptr, target, now, actions)
                return
            self._release_grab(ptr, actions)

        if pose in ACTION_POSES and pose not in self._latched:
            self._start(ptr, pose, now, actions)
        else:
            self._move(target, actions)

    def _start(self, ptr: _Track, pose: PoseName, now: float, actions: list[Action]) -> None:
        rewind_to = ptr.confirmed_t - self.settings.rewind_s
        point = self._aimed(ptr, rewind_to)
        if pose in ("pinch", "pinch_middle"):
            button: MouseButton = "left" if pose == "pinch" else "right"
            double = False
            last = self._last_click
            limit, width, height = self.double_click
            if (
                last is not None
                and last.button == button
                and now - last.t <= limit
                and abs(point.x - last.point.x) <= 1.5 * width
                and abs(point.y - last.point.y) <= 1.5 * height
            ):
                point, double = last.point, True
            self._press = _Press(button, pose, point, ptr.confirmed_anchor, now, double, ptr.confirmed_t)
            self._mode = "press"
            self._cursor = point
            self._hold_from(point, rewind_to)
            self._press_down(ptr, actions)
        elif pose == "two":
            self._mode = "scroll"
            self._cursor = point
            self._scroll_prev = self._motion
            self._scroll_acc = [0.0, 0.0]
            self._emit("gesture", "scroll_start")
        elif pose == "fist":
            self._mode = "grab"
            self._cursor = point
            self._hold_from(point, rewind_to)
            self._helper = None
            actions.append(GrabWindow(point.x, point.y))
            self._emit("gesture", "grab")

    def _press_down(self, ptr: _Track, actions: list[Action], *, settled: bool = False) -> bool:
        """The button goes down once the hand has settled into the pinch (see ``SETTLE_RATE``).

        The settling is never measured back past the last frame the pinching
        finger was curled: a fist opening into the pinch has only just let go
        of it. ``settled`` presses regardless (a tap).
        """
        press = self._press
        assert press is not None
        if press.down:
            return True
        if not settled and (
            ptr.raw != press.pose or not ptr.settled(press.pose, press.since, ptr.curled_t[_finger(press.pose)] + 1e-6)
        ):
            return False
        self._press = replace(press, down=True)
        actions.append(Button(press.button, True, press.point.x, press.point.y))
        return True

    def _tap_verdict(self, ptr: _Track, press: _Press, pose: PoseName, now: float) -> Literal["click", "drop", "wait"]:
        """A pinch that ended before its button went down: a quick tap, or a hand passing through a pinch.

        A hand closing into a fist leaves the pinch when the index curls (or
        straight into the fist); one opening out of a fist enters it as the
        index uncurls. Either way the pinching finger was curled just before or
        during the pinch, which a tap never shows. A tap whose thumb lets go
        while the other fingers still close may yet end in a fist: its click
        waits up to ``TAP_WAIT_S`` for one. Pinching again in the meantime is
        the next tap, so this one clicks first.
        """
        if ptr.curled_t[_finger(press.pose)] >= press.since - TRANSIT_LOOKBACK_S:
            return "drop"
        if pose == press.pose:
            return "click"
        if pose in ACTION_POSES:
            return "drop"
        if pose == "palm" or not ptr.closing(press.pose):
            return "click"
        ended = now if press.ended is None else press.ended
        return "wait" if now - ended < TAP_WAIT_S else "click"

    def _release_press(self, actions: list[Action]) -> None:
        press, point = self._press, self._cursor
        assert press is not None and point is not None
        actions.append(Button(press.button, False, point.x, point.y))
        if self._mode == "drag":
            self._emit("gesture", "drag_end")
            self._last_click = None
        elif press.double:
            self._emit("gesture", "double_click" if press.button == "left" else "right_click")
            self._last_click = None
        else:
            self._emit("gesture", "click" if press.button == "left" else "right_click")
            self._last_click = _Click(press.button, press.point, press.t)
        self._press = None
        self._mode = "point"

    def _scroll(self, actions: list[Action]) -> None:
        if self._scroll_prev is None or self._motion is None or self._cursor is None:
            return
        delta = self._motion - self._scroll_prev
        dx, dy = delta.x, delta.y
        # Per frame, a minor axis under half the major one is hand wobble, not a diagonal scroll.
        if abs(dx) < 0.5 * abs(dy):
            dx = 0.0
        elif abs(dy) < 0.5 * abs(dx):
            dy = 0.0
        k = SCROLL_UNITS_PER_PX * self.settings.scroll_speed
        # Natural direction: the content follows the hand. Hand up (desktop dy < 0) moves the content up,
        # which is the wheel turned towards the user: a negative wheel dy. Sideways likewise, with the wheel's
        # positive dx scrolling right (content moving left).
        self._scroll_acc[0] += k * dy
        self._scroll_acc[1] -= k * dx
        out_dy, out_dx = math.trunc(self._scroll_acc[0]), math.trunc(self._scroll_acc[1])
        if out_dy or out_dx:
            self._scroll_acc[0] -= out_dy
            self._scroll_acc[1] -= out_dx
            actions.append(Scroll(float(out_dy), float(out_dx), self._cursor.x, self._cursor.y))

    def _hold_grab(self, ptr: _Track, target: Point, now: float, actions: list[Action]) -> None:
        helper = self._helper if self._mode == "resize" else self._helper_candidate()
        if self._mode == "grab" and helper is not None and helper.seen and helper.confirmed == "fist":
            self._mode = "resize"
            self._helper = helper
            self._helper_filter = self._new_filter()
            self._emit("gesture", "resize_start")
        if self._mode == "resize":
            assert self._helper is not None
            helper = self._helper
            if not helper.seen:
                if now - helper.last_seen > self.settings.hold_s:
                    self._end_grab(actions)
                return
            if helper.confirmed != "fist":
                self._end_grab(actions)
                return
            self._helper_point = self._helper_filter.filter(self.mapper.to_desktop(helper.anchor), now)
            if ptr.raw == "fist" and helper.raw == "fist":
                self._cursor = target
                actions.append(ResizeWindow(target.x, target.y, self._helper_point.x, self._helper_point.y))
            return
        if ptr.raw == "fist":
            self._cursor = target
            actions.append(DragWindow(target.x, target.y))

    def _helper_candidate(self) -> _Track | None:
        others = [t for t in self._tracks if t.seen and t is not self._pointer]
        return max(others, key=lambda t: t.last_seen, default=None)

    def _end_grab(self, actions: list[Action]) -> None:
        """The helper hand opened or left during a resize: the whole grab ends; the fist must open to grab again."""
        actions.append(ReleaseWindow())
        self._emit("gesture", "release")
        self._mode = "point"
        self._helper = None
        self._helper_point = None
        self._latched = frozenset({"fist"}) | _OPENING_FIST

    def _release_grab(self, ptr: _Track, actions: list[Action]) -> None:
        direction = None if self._mode == "resize" else self._throw_direction(ptr.confirmed_t)
        if direction is None:
            actions.append(ReleaseWindow())
            self._emit("gesture", "release")
        else:
            actions.append(ThrowWindow(direction))
            self._emit("gesture", f"throw_{direction}")
        self._mode = "point"
        self._helper = None
        self._helper_point = None
        # The fist is opening: the thumb crosses the index on the way, which must not click what is under it.
        self._latched = _OPENING_FIST

    def _throw_direction(self, end: float) -> Literal["left", "right", "up", "down"] | None:
        """From the motion over ``throw_window_s`` before the hand began to open."""
        hist = [(t, p) for t, p in self._motion_hist if t <= end]
        if len(hist) < 2 or self._cursor is None:
            return None
        t1, p1 = hist[-1]
        t0, p0 = _window_start(hist, t1, self.settings.throw_window_s)
        if t1 - t0 <= 1e-6:
            return None
        v = (p1 - p0).scale(1.0 / (t1 - t0))
        try:
            width = self.mapper.display_at(self._cursor).rect.width
        except LookupError:  # every display went away mid-grab: nowhere to throw to
            return None
        if v.length() <= self.settings.throw_speed * width:
            return None
        if abs(v.x) >= AXIS_DOMINANCE * abs(v.y):
            return "right" if v.x > 0 else "left"
        if abs(v.y) >= AXIS_DOMINANCE * abs(v.x):
            return "down" if v.y > 0 else "up"
        return None

    def _move(self, target: Point, actions: list[Action]) -> None:
        self._cursor = target
        actions.append(MoveCursor(target.x, target.y))

    def _holding(self) -> bool:
        """Whether a button or a window is held (what ReleaseAll undoes)."""
        return self._mode in ("press", "drag", "grab", "resize")

    def _end_mode(self, *, latch: bool) -> None:
        """Abandon the current interaction (the caller emits ReleaseAll when something was held)."""
        mode = self._mode
        if mode in ("press", "drag") and self._press is not None:
            if mode == "drag":
                self._emit("gesture", "drag_end")
            if latch:
                self._latched = frozenset({self._press.pose})
        elif mode in ("grab", "resize"):
            self._emit("gesture", "release")
            if latch:
                self._latched = frozenset({"fist"}) | _OPENING_FIST
        self._mode = "point"
        self._press = None
        self._helper = None
        self._helper_point = None
        self._scroll_prev = None

    def _aimed(self, ptr: _Track, t: float) -> Point:
        """Where the cursor pointed before the pose began: the rewind to ``t`` (``rewind_s`` before it).

        With ``anchor: index`` the rewind is on the knuckles: the fingertip
        travels for as long as the finger bends into the pose, far longer than
        ``rewind_s``. So the fingertip's aim is taken from the last frame the
        index was as straight as it got (back to ``BEND_LOOKBACK_S``, within
        ``BEND_TOLERANCE``), and only the knuckles' own motion since then counts.
        """
        if self._knuckle is None or not self._knuckle_hist:
            return self._rewound(t)
        index = [(when, reach[0]) for when, reach in ptr.reach if t - BEND_LOOKBACK_S <= when <= t]
        began, straightest = t, -math.inf
        for i in range(len(index) - 1, -1, -1):
            # Over three frames, so one frame of landmark noise does not pass for the finger straightening.
            around = index[max(0, i - 1) : i + 2]
            when, reach = index[i][0], sum(r for _, r in around) / len(around)
            if reach < straightest - BEND_TOLERANCE:
                break
            if reach > straightest:
                began, straightest = when, reach
        return self._rewound(began) + (_at(self._knuckle_hist, t) - _at(self._knuckle_hist, began))

    def _hold_from(self, point: Point, t: float) -> None:
        """With ``anchor: index``: what is held at ``point`` follows the knuckles from where they were at ``t``."""
        self._held_offset = None
        if self._knuckle is not None and self._knuckle_hist:
            self._held_offset = point - _at(self._knuckle_hist, t)

    def _rewound(self, t: float) -> Point:
        """The cursor as it was at time ``t`` (the latest recorded point not after it)."""
        point = self._cursor_hist[0][1] if self._cursor_hist else self._cursor
        for when, p in self._cursor_hist:
            if when > t:
                break
            point = p
        assert point is not None
        return point

    def _record(self, now: float) -> None:
        if self._cursor is not None:
            self._cursor_hist.append((now, self._cursor))
        if self._motion is not None:
            self._motion_hist.append((now, self._motion))
        if self._knuckle is not None:
            self._knuckle_hist.append((now, self._knuckle))
        for hist in (self._cursor_hist, self._motion_hist, self._knuckle_hist):
            while hist and now - hist[0][0] > HISTORY_S:
                hist.popleft()

    # -- calibration --------------------------------------------------------------------

    def _calibration_frame(self, now: float) -> None:
        seen = self._seen()
        track: _Track | None = None
        if self._engaged and self._pointer is not None and self._pointer.seen:
            track = self._pointer
        if track is None:
            track = next((t for t in seen if t.confirmed == "palm"), seen[0] if seen else None)
        pose = None
        if track is not None and track.hand_pose is not None:
            pose = replace(track.hand_pose, pose=track.confirmed or "hover")
        for step in self._calibration.update(pose, now):
            self._emit("calibration", step)
            if step == "done":
                result = self._calibration.result
                self.mapper.set_homography(result)
                self._calibration_result = result
                log.info("calibration done")
            if step in ("done", "cancelled"):
                self._calibration_ended()

    def _calibration_ended(self) -> None:
        # The palm that showed the last corner must not engage on the spot: a fresh hold starts now.
        self._engage_floor = self._now
        for t in self._tracks:
            t.palm_since = None
        self._reset_cursor = True

    # -- plumbing -----------------------------------------------------------------------

    def _new_filter(self) -> OneEuroFilter2D:
        s = self.settings
        return OneEuroFilter2D(s.min_cutoff, s.beta, s.d_cutoff)

    def _emit(self, kind: EventKind, value: str) -> None:
        self._events.append(EngineEvent(kind, value))

    def _sync_state(self) -> None:
        state: EngineState = "calibrating" if self._calibration.active else "active" if self._engaged else "idle"
        if state != self._state:
            self._state = state
            self._emit("state", state)
