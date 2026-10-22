"""The pinch press: a finger pinched to the thumb types its key (DESIGN-KEYBOARD.md 2.6).

Per ``(hand, finger)`` a small state machine: ``latched`` (must reopen before it can arm), ``open`` (ready),
``closing`` (a pinch has started; the aim is frozen where the fingertip was when it started) and ``pressed``. The aim
is read at the *onset*, never at the commit: during a pinch the fingertip travels one pitch or more by itself, so a
press-time aim picks the wrong key (measured 0 of 24 against 24 of 24 at onset).

Pure: no I/O, no clock (the hand sample's own time), no numpy.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Sequence
from typing import Final

from . import limits
from .tuning import RANGES, Tuning
from .types import FingerView, HandSample, PressEvent, PressLevel, PressName, PressQuality, Side, TipState

#: How long a finger's history is kept.
_HISTORY_S: Final = 0.6
#: A hand that has not been seen for this long takes its state with it.
_FORGET_S: Final = 2.0
#: A pinch whose hand has moved this far (in palms) since its onset did not start where the aim says.
_MOVED_PALM: Final = 0.35
#: Frames within this many palms of the onset frame's hand position are the same stance (the aim averages them).
_SAME_PALM: Final = 0.10
#: The same finger does not press again sooner than this after a press (a pinch that flickers open and shut under
#: noise is one press); the fastest honest repeat is 0.30 s (3.3 a second).
_REFRACTORY_S: Final = 0.20
#: The least gap between the open and the close threshold that ``set_finger`` allows (the pair relation of 3.2).
_MIN_BAND: Final = 0.06


class _Finger:
    __slots__ = (
        "aim",
        "anchor",
        "closing_since",
        "fail",
        "history",
        "last_press",
        "onset_ratio",
        "onset_t",
        "run",
        "stalled",
        "state",
    )

    def __init__(self) -> None:
        self.state: TipState = "latched"
        #: (t, aim, ratio, reach, anchor) of the last 0.6 s.
        self.history: deque[tuple[float, tuple[float, float], float, float, tuple[float, float]]] = deque()
        #: Consecutive frames: ratio at or above ``open`` (latched, pressed) or every commit condition true (closing).
        self.run = 0
        #: Frozen at the onset while closing and pressed.
        self.aim: tuple[float, float] = (0.0, 0.0)
        self.anchor: tuple[float, float] = (0.0, 0.0)
        self.onset_ratio = 0.0
        self.onset_t = 0.0
        self.closing_since = 0.0
        #: Frame time of this finger's newest press; far in the past until it has pressed.
        self.last_press = -math.inf
        #: Consecutive closing frames at or above ``descent_max_r``: a pinch that is not in progress.
        self.stalled = 0
        #: The commit condition that failed on the newest frame of this closing episode, if any.
        self.fail: str | None = None

    def latch(self) -> None:
        self.state = "latched"
        self.history.clear()
        self.run = 0
        self.fail = None


class _Hand:
    __slots__ = ("fingers", "last_t")

    def __init__(self, t: float) -> None:
        self.last_t = t
        self.fingers = [_Finger() for _ in range(4)]


class PinchPress:
    name: PressName = "pinch"
    requires_review = False
    #: Counters by reason, per closing episode that did not press (2.6); only ever added to.
    rejects: dict[str, int]

    def __init__(self, tuning: Tuning) -> None:
        self._tuning = tuning
        self._hands: dict[int, _Hand] = {}
        #: (side, finger) -> (close, open) from the warm-up; absent: the tuning's own.
        self._thresholds: dict[tuple[Side, int], tuple[float, float]] = {}
        self.rejects: dict[str, int] = {}

    # ------------------------------------------------------------------------------------------ the PressMethod

    def reset(self) -> None:
        """Every finger latched, nothing pending. The thresholds of the warm-up and the counters are kept."""
        for hand in self._hands.values():
            for finger in hand.fingers:
                finger.latch()

    def set_finger(self, side: Side, finger: int, close: float, open_: float | None = None) -> None:
        """This finger's thresholds from the warm-up (2.5). Both are needed; they are held inside the clamps."""
        if open_ is None:
            raise ValueError("a pinch needs both thresholds")
        if not (math.isfinite(close) and math.isfinite(open_)):
            raise ValueError("a threshold must be a finite number")
        low, high = RANGES["close"]
        close = min(max(close, low), high)
        open_low, open_high = RANGES["open"]
        self._thresholds[(side, finger)] = (close, min(max(open_, open_low, close + _MIN_BAND), open_high))

    def quality(self) -> PressQuality:
        return PressQuality(0.0, None)

    def set_level(self, level: PressLevel) -> None:
        """The degradation ladder belongs to the air tap; a pinch has nothing to degrade."""

    def set_calibrating(self, on: bool) -> None:
        """The warm-up rule of the air tap's threshold; a pinch warms up with its own ``set_finger`` call."""

    def fingers(self, hands: Sequence[HandSample]) -> list[FingerView]:
        views = []
        for hand in hands:
            state = self._hands.get(hand.hand)
            for finger in hand.fingers:
                one = state.fingers[finger.finger] if state is not None else None
                if one is not None and one.state in ("closing", "pressed"):
                    aim, tip_state = one.aim, one.state
                else:
                    aim, tip_state = (float(finger.aim[0]), float(finger.aim[1])), one.state if one else "latched"
                views.append(FingerView(hand.hand, hand.side, finger.finger, aim, tip_state))
        return views

    def update(self, hands: Sequence[HandSample]) -> list[PressEvent]:
        events: list[PressEvent] = []
        for hand in hands:
            state = self._hands.get(hand.hand)
            if state is None:
                state = self._hands[hand.hand] = _Hand(hand.t)  # a new hand: every finger starts latched
            elif not 0.0 <= hand.t - state.last_t <= limits.GAP_RESET_S:
                # A tracking gap, or a clock that went back: whatever was in progress is not trusted.
                for finger in state.fingers:
                    finger.latch()
            state.last_t = hand.t
            events += self._hand(hand, state)
        newest = max((hand.t for hand in hands), default=None)
        if newest is not None:
            for hand_id in [i for i, s in self._hands.items() if newest - s.last_t > _FORGET_S]:
                del self._hands[hand_id]
        return events

    # ------------------------------------------------------------------------------------------ one hand

    def _reject(self, name: str) -> None:
        self.rejects[name] = self.rejects.get(name, 0) + 1

    def _limits(self, side: Side, finger: int) -> tuple[float, float]:
        return self._thresholds.get((side, finger), (self._tuning.close, self._tuning.open))

    def _hand(self, hand: HandSample, state: _Hand) -> list[PressEvent]:
        tuning = self._tuning
        for sample in hand.fingers:
            one = state.fingers[sample.finger]
            one.history.append(
                (
                    hand.t,
                    (float(sample.aim[0]), float(sample.aim[1])),
                    sample.ratio,
                    sample.reach,
                    (float(hand.anchor[0]), float(hand.anchor[1])),
                )
            )
            while one.history and hand.t - one.history[0][0] > _HISTORY_S:
                one.history.popleft()

        by_ratio = sorted(hand.fingers, key=lambda f: f.ratio)
        best = by_ratio[0]
        margin = by_ratio[1].ratio - best.ratio
        curled = sum(1 for f in hand.fingers if f.curled)
        events: list[PressEvent] = []
        for sample in hand.fingers:
            one = state.fingers[sample.finger]
            close, open_ = self._limits(hand.side, sample.finger)
            r = sample.ratio
            if one.state == "latched":
                one.run = one.run + 1 if r >= open_ else 0
                if one.run >= tuning.confirm_frames:
                    one.state, one.run = "open", 0
            elif one.state == "open":
                self._begin(one, hand, r)
            elif one.state == "closing":
                if r >= one.onset_ratio - tuning.recover:  # the pinch fell back to where it began
                    self._end(one, "aborted", "open")
                    continue
                if math.hypot(hand.anchor[0] - one.anchor[0], hand.anchor[1] - one.anchor[1]) > _MOVED_PALM * hand.palm:
                    self._end(one, "hand_moving", "open")  # the hand went on: the frozen aim is not where it is
                    continue
                one.stalled = one.stalled + 1 if r >= tuning.descent_max_r else 0
                if one.stalled >= tuning.confirm_frames:  # back above the bar a closing starts below
                    self._end(one, "aborted", "open")
                    continue
                if hand.t - one.closing_since > tuning.closing_timeout_s:
                    self._end(one, "closing_timeout", "latched")
                    continue
                one.fail = (
                    None if r >= close else self._why_not(hand, state, sample.finger, best.finger, margin, curled)
                )
                one.run = one.run + 1 if r < close and one.fail is None else 0
                if one.run >= tuning.confirm_frames:
                    one.state, one.run, one.fail, one.last_press = "pressed", 0, None, hand.t
                    events.append(
                        PressEvent(hand.t, one.onset_t, hand.hand, hand.side, sample.finger, one.aim, r, margin)
                    )
            else:  # pressed: it must reopen before it can arm again
                one.run = one.run + 1 if r >= open_ else 0
                if one.run >= tuning.confirm_frames:
                    one.state, one.run = "open", 0
        return events

    def _begin(self, one: _Finger, hand: HandSample, r: float) -> None:
        """Find the onset: the latest frame in the window whose ratio was still ``descent`` above this one's."""
        tuning = self._tuning
        t = hand.t
        if r >= tuning.descent_max_r:
            return
        history = one.history
        for position in range(len(history) - 2, -1, -1):
            then, _aim, ratio, _reach, there = history[position]
            if t - then > tuning.onset_window_s:
                return
            if ratio >= r + tuning.descent:
                if math.hypot(hand.anchor[0] - there[0], hand.anchor[1] - there[1]) > _MOVED_PALM * hand.palm:
                    return  # a peak from before the hand arrived: the fingertip was somewhere else then
                # The aim averages the frames up to the onset, but not the ones from before the hand got here.
                near = [
                    entry[1]
                    for entry in list(history)[max(0, position - tuning.aim_frames + 1) : position + 1]
                    if math.hypot(entry[4][0] - there[0], entry[4][1] - there[1]) <= _SAME_PALM * hand.palm
                ]
                one.aim = (sum(a[0] for a in near) / len(near), sum(a[1] for a in near) / len(near))
                one.onset_ratio, one.onset_t, one.closing_since, one.anchor = ratio, then, t, there
                one.state, one.run, one.fail, one.stalled = "closing", 0, None, 0
                return

    def _end(self, one: _Finger, reason: str, state: TipState) -> None:
        """A closing episode that did not press: one counter, the commit condition that failed last, else the reason."""
        self._reject(one.fail or reason)
        one.state, one.run, one.fail = state, 0, None

    def _why_not(
        self, hand: HandSample, state: _Hand, finger: int, best: int, margin: float, curled: int
    ) -> str | None:
        """The first commit condition (2.6, items 2 to 6) that does not hold on this frame, or None."""
        tuning = self._tuning
        if hand.t - state.fingers[finger].last_press < _REFRACTORY_S:
            return "refractory"
        if best != finger or margin < tuning.margin:
            return "ambiguous"
        if hand.fingers[finger].curled:
            return "curled"
        if curled >= 3:
            return "fist_like"
        for other in hand.fingers:
            # The legato rule: a typist slides the thumb from fingertip to fingertip, and a finger whose ratio is
            # already back above its own open threshold does not block the next one.
            if (
                other.finger != finger
                and state.fingers[other.finger].state == "pressed"
                and other.ratio < self._limits(hand.side, other.finger)[1]
            ):
                return "other_finger_down"
        if self._others_moving(state, finger, hand.t):
            return "others_moving"
        if hand.speed > tuning.anchor_speed_max:
            return "hand_moving"
        return None

    def _others_moving(self, state: _Hand, finger: int, t: float) -> bool:
        """The mean reach of the other three changed by more than ``others_delta``: a hand opening or closing."""
        tuning = self._tuning
        now: list[float] = []
        before: list[float] = []
        for index, one in enumerate(state.fingers):
            if index == finger:
                continue
            newest = [entry[3] for entry in list(one.history)[-2:]]
            old = [entry[3] for entry in one.history if tuning.others_from_s <= t - entry[0] <= tuning.others_to_s]
            if not newest or not old:
                return False
            now.append(sum(newest) / len(newest))
            before.append(sum(old) / len(old))
        return abs(sum(now) / 3 - sum(before) / 3) > tuning.others_delta
