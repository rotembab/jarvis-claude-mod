"""The gesture state machine: tracked hands in, desktop actions out.

``GestureEngine.update(frame)`` is pure apart from the engine's own state:
the clock is the frame's capture time (or ``now``), so tests script whole
interactions frame by frame. The runtime hands the returned actions to the
executor, drains ``take_events()`` into protocol events and draws ``view()``.

How a frame flows:

1. **Tracks.** Each observed hand is matched to a track (nearest anchor within
   ``ASSOCIATE_RADIUS`` frame widths, else the same handedness). Nearest is
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
from typing import Literal

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
#: Wheel units per desktop pixel of hand motion, before ``scroll_speed``.
SCROLL_UNITS_PER_PX = 2.4
#: A throw (and a scroll axis) must be this much more along one axis than the other.
AXIS_DOMINANCE = 1.5
MAX_TRACKS = 4
ACTION_POSES: frozenset[str] = frozenset({"pinch", "pinch_middle", "fist", "two"})


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


class _Track:
    """One hand followed across frames: hysteresis, debounce and recent anchors."""

    def __init__(self, track_id: int, now: float) -> None:
        self.id = track_id
        self.poses = PoseTracker()
        self.handedness = "right"
        self.seen = False
        self.last_seen = now
        #: Image coordinates (what the mapper takes) and frame-width units (distances).
        self.anchor = Point(0.5, 0.5)
        self.anchor_fw = Point(0.5, 0.5)
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
        self.palm_since: float | None = None

    def restart(self, now: float) -> None:
        """The hand was out of view for a while: nothing seen before the gap counts any more."""
        self.poses.reset()
        self.raw, self.run, self.confirmed = None, 0, None
        self.run_start_t = self.confirmed_t = now
        self.history.clear()
        self.palm_since = None

    def observe(self, pose: HandPose, now: float, anchor_fw: Point, confirm_frames: int) -> None:
        self.hand_pose = pose
        self.anchor = pose.anchor
        self.anchor_fw = anchor_fw
        self.history.append((now, anchor_fw))
        while self.history and now - self.history[0][0] > HISTORY_S:
            self.history.popleft()
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


def _window_start(history: Iterable[tuple[float, Point]], end: float, window: float) -> tuple[float, Point]:
    """The newest sample at least ``window`` seconds before ``end``, else the oldest one."""
    start: tuple[float, Point] | None = None
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
        #: After a command or real-mouse disengage, ``engage: always`` waits for the hands to leave first.
        self._always_blocked = False
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
        aspect = frame.height / frame.width if frame.width else 1.0
        observed: list[tuple[HandObservation, Point, Point]] = []
        for hand in frame.hands:
            anchor = anchor_point(hand.image, self.settings.anchor)
            observed.append((hand, anchor, Point(anchor.x, anchor.y * aspect)))
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
            for i, (_, _, anchor_fw) in enumerate(observed):
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
                best = min(options, key=lambda i: observed[i][2].distance(track.anchor_fw))
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
            elif missed > 0 and track.palm_since is not None:
                # The hold counts the time the palm was in view, not the frames it was missed in.
                track.palm_since += missed
            hand, _, anchor_fw = observed[i]
            track.seen, track.last_seen, track.handedness = True, now, hand.handedness
            pose = track.poses.classify(hand, self.settings.anchor)
            track.observe(pose, now, anchor_fw, confirm)

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
        self._cursor = self._cursor_filter.filter(self.mapper.to_desktop(track.anchor), now)
        self._motion = self._motion_filter.filter(self.mapper.to_region(track.anchor), now)
        self._cursor_hist.clear()
        self._motion_hist.clear()
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
        target = self._cursor if filtered.distance(self._cursor) <= self.settings.dead_zone_px else filtered

        if self._latched and ptr.confirmed is not None and ptr.confirmed not in self._latched:
            self._latched = frozenset()
        pose: PoseName = ptr.confirmed or "hover"
        self._step(ptr, pose, target, now, actions)
        self._record(now)

    def _step(self, ptr: _Track, pose: PoseName, target: Point, now: float, actions: list[Action]) -> None:
        mode = self._mode
        if mode in ("press", "drag"):
            press = self._press
            assert press is not None
            if pose == press.pose:
                if ptr.raw == press.pose:
                    if mode == "press" and ptr.anchor_fw.distance(press.anchor) > self.settings.slop:
                        self._mode = "drag"
                        self._emit("gesture", "drag_start")
                    if self._mode == "drag":
                        self._move(target, actions)
                return
            self._release_press(actions)
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
        point = self._rewound(ptr.confirmed_t - self.settings.rewind_s)
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
            self._press = _Press(button, pose, point, ptr.confirmed_anchor, now, double)
            self._mode = "press"
            self._cursor = point
            actions.append(Button(button, True, point.x, point.y))
        elif pose == "two":
            self._mode = "scroll"
            self._cursor = point
            self._scroll_prev = self._motion
            self._scroll_acc = [0.0, 0.0]
            self._emit("gesture", "scroll_start")
        elif pose == "fist":
            self._mode = "grab"
            self._cursor = point
            self._helper = None
            actions.append(GrabWindow(point.x, point.y))
            self._emit("gesture", "grab")

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
        self._latched = frozenset({"fist"})

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
                self._latched = frozenset({"fist"})
        self._mode = "point"
        self._press = None
        self._helper = None
        self._helper_point = None
        self._scroll_prev = None

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
        for hist in (self._cursor_hist, self._motion_hist):
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
