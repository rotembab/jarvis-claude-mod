"""The keyboard session: the pinch warm-up (U7) and, below it, the session's per-frame rules (DESIGN-KEYBOARD.md 2.7).

Everything here is driven by hand-built samples and scripted presses; the real detectors are other tests' business.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from itertools import pairwise

import numpy as np
import pytest

from jarvis_hands.desktop.base import KeyTarget
from jarvis_hands.keyboard.layout import char_for, layout_for
from jarvis_hands.keyboard.limits import NO_KEY_CLOSE_S
from jarvis_hands.keyboard.rig import PINCH_PROFILE, PITCH, KbRig, ScriptedPress
from jarvis_hands.keyboard.session import SESSION_TEXT
from jarvis_hands.keyboard.tuning import RANGES, Tuning
from jarvis_hands.keyboard.types import FingerSample, HandSample, PressEvent, Side
from jarvis_hands.keyboard.warmup import Warmup

TUNING = Tuning()
DT = 1 / 30
#: How many taps the storm streams send.
STREAM = 38


def hand(
    t: float,
    ratios: Sequence[float] = (0.8, 0.8, 0.8, 0.8),
    *,
    side: Side = "right",
    hid: int = 1,
    speed: float = 0.1,
    curled: Sequence[bool] = (False, False, False, False),
) -> HandSample:
    fingers = tuple(
        FingerSample(i, np.array([0.5, 0.5]), 1.0, float(r), bool(c))
        for i, (r, c) in enumerate(zip(ratios, curled, strict=True))
    )
    return HandSample(hid, side, t, 0.1, np.array([0.5, 0.5]), speed, fingers)


HOME_RIGHT = {("right", f): (8.5 + f, 3.5) for f in range(4)}
HOME_BOTH = {**HOME_RIGHT, **{("left", f): (2.5 - f, 3.5) for f in range(4)}}

#: The path of a clean pinch: open, down to a minimum, back up.
DOWN = (0.8, 0.8, 0.8, 0.55, 0.45, 0.35, 0.30, 0.30, 0.45, 0.55, 0.8, 0.8)


class Pinches:
    """Feeds a ``Warmup`` frame after frame at 30 fps, one finger's ratio path at a time."""

    def __init__(self, w: Warmup, *, side: Side = "right", hid: int = 1) -> None:
        self.w = w
        self.side = side
        self.hid = hid
        self.t = 0.0

    def frame(self, ratios: Sequence[float] = (0.8, 0.8, 0.8, 0.8), **kw: object) -> None:
        self.t += DT
        self.w.update([hand(self.t, ratios, side=self.side, hid=self.hid, **kw)])  # type: ignore[arg-type]

    def pinch(
        self,
        finger: int,
        path: Sequence[float] = DOWN,
        *,
        others: Sequence[float] = (0.8, 0.8, 0.8, 0.8),
        speed: float = 0.1,
        curled: Sequence[bool] = (False, False, False, False),
    ) -> None:
        for r in path:
            ratios = list(others)
            ratios[finger] = r
            self.frame(ratios, speed=speed, curled=curled)  # type: ignore[arg-type]


def pinch_warmup(home: dict | None = HOME_RIGHT) -> tuple[Warmup, Pinches]:
    w = Warmup(TUNING, "pinch", home_f=home)
    return w, Pinches(w)


def r_min_of(w: Warmup, finger: int, side: Side = "right") -> float | None:
    close, _open = w.thresholds()[(side, finger)]
    return None if (side, finger) not in w.done else close


# ---------------------------------------------------------------------------------------------- U7: pinch warm-up


def test_u7_a_clean_cycle_records_the_finger_and_sets_its_thresholds() -> None:
    w, p = pinch_warmup()
    assert w.done == frozenset() and not w.complete and w.required == frozenset(HOME_RIGHT)
    p.pinch(0, (0.8, 0.8, 0.55, 0.45, 0.26, 0.45, 0.55, 0.8))
    assert w.done == {("right", 0)}
    close, open_ = w.thresholds()[("right", 0)]
    assert close == pytest.approx(1.25 * 0.26) and open_ == pytest.approx(max(0.40, close + 0.12))


@pytest.mark.parametrize(
    ("r_min", "close", "open_"),
    [
        (0.30, 0.36, 0.48),  # 1.25 x 0.30 = 0.375 is clamped to the top of the range
        (0.26, 0.325, 0.445),
        (0.15, 0.22, 0.40),  # clamped to the bottom; the open threshold keeps its floor
    ],
)
def test_u7_the_thresholds_are_clamped_and_open_keeps_a_gap_above_close(
    r_min: float, close: float, open_: float
) -> None:
    w, p = pinch_warmup()
    p.pinch(1, (0.8, 0.8, 0.55, 0.45, r_min, 0.45, 0.55, 0.8))
    got = w.thresholds()[("right", 1)]
    assert got == pytest.approx((close, open_))
    low, high = RANGES["close"]
    assert low <= got[0] <= high


def test_u7_a_finger_without_a_record_keeps_the_defaults_and_every_required_finger_is_listed() -> None:
    w, p = pinch_warmup()
    p.pinch(2)
    table = w.thresholds()
    assert set(table) == set(HOME_RIGHT)
    assert table[("right", 0)] == (TUNING.close, TUNING.open)
    assert table[("right", 2)] != (TUNING.close, TUNING.open)


def test_u7_a_second_valid_cycle_replaces_the_first() -> None:
    w, p = pinch_warmup()
    p.pinch(0, (0.8, 0.55, 0.45, 0.30, 0.45, 0.55, 0.8))
    first = w.thresholds()[("right", 0)]
    p.pinch(0, (0.8, 0.55, 0.45, 0.24, 0.45, 0.55, 0.8))
    second = w.thresholds()[("right", 0)]
    assert second[0] == pytest.approx(1.25 * 0.24) and second != first


def test_u7_all_the_required_fingers_complete_it() -> None:
    w, p = pinch_warmup()
    for f in range(4):
        assert not w.complete
        p.pinch(f)
    assert w.complete and w.done == w.required


def test_u7_two_hands_need_all_eight() -> None:
    w = Warmup(TUNING, "pinch", home_f=HOME_BOTH)
    right, left = Pinches(w, side="right", hid=1), Pinches(w, side="left", hid=2)
    for f in range(4):
        right.pinch(f)
    assert not w.complete and len(w.done) == 4
    for f in range(4):
        left.pinch(f)
    assert w.complete and len(w.done) == 8


def test_u7_condition_1_the_cycle_starts_from_an_open_finger() -> None:
    w, p = pinch_warmup()
    # a finger that is already below 0.60 and never opens that far is not starting a cycle
    p.pinch(0, (0.55, 0.45, 0.30, 0.45, 0.55, 0.58))
    assert w.done == frozenset()
    p.pinch(0, (0.62, 0.55, 0.45, 0.30, 0.45, 0.55, 0.8))
    assert w.done == {("right", 0)}


def test_u7_condition_1_a_pinch_that_exists_when_the_hand_appears_waits_until_it_opens() -> None:
    w, p = pinch_warmup()
    p.pinch(0, (0.30, 0.30, 0.30, 0.45, 0.55, 0.58, 0.55, 0.45, 0.30, 0.45, 0.55))
    assert w.done == frozenset()
    p.pinch(0, (0.9, 0.55, 0.45, 0.30, 0.45, 0.55, 0.8))
    assert w.done == {("right", 0)}


def test_u7_condition_1_the_arming_ratio_is_inclusive_and_the_falling_ratio_exclusive() -> None:
    w, p = pinch_warmup()
    p.pinch(0, (0.60, 0.50, 0.50, 0.30, 0.55, 0.8))  # exactly 0.50 is not below 0.50, but 0.30 is
    assert w.done == {("right", 0)}
    w, p = pinch_warmup()
    p.pinch(0, (0.5999, 0.30, 0.45, 0.55, 0.8))
    assert w.done == frozenset()


def test_u7_condition_2_it_must_rise_back_within_three_seconds() -> None:
    w, p = pinch_warmup()
    p.pinch(0, (0.8, 0.45, 0.30) + (0.30,) * 90 + (0.45, 0.55, 0.8))  # below 0.50 for 3.1 s
    assert w.done == frozenset()
    w, p = pinch_warmup()
    p.pinch(0, (0.8, 0.45, 0.30) + (0.30,) * 80 + (0.45, 0.55, 0.8))  # 2.8 s
    assert w.done == {("right", 0)}


def test_u7_condition_2_a_cycle_that_took_too_long_leaves_the_finger_to_open_fully_again() -> None:
    w, p = pinch_warmup()
    p.pinch(0, (0.8, 0.45, 0.30) + (0.30,) * 100 + (0.45, 0.55, 0.8))
    assert w.done == frozenset()
    # it went back above 0.60 afterwards, so the next clean cycle counts
    p.pinch(0, DOWN)
    assert w.done == {("right", 0)}


def test_u7_condition_3_the_minimum_must_be_below_point_four() -> None:
    w, p = pinch_warmup()
    p.pinch(0, (0.8, 0.55, 0.45, 0.41, 0.45, 0.55, 0.8), others=(0.9, 0.9, 0.9, 0.9))
    assert w.done == frozenset()
    p.pinch(0, (0.8, 0.55, 0.45, 0.39, 0.45, 0.55, 0.8), others=(0.9, 0.9, 0.9, 0.9))
    assert w.done == {("right", 0)}


def test_u7_condition_4_the_finger_must_lead_the_other_three_by_a_margin() -> None:
    low = (0.8, 0.55, 0.45, 0.39, 0.45, 0.55, 0.8)
    w, p = pinch_warmup()
    p.pinch(0, low, others=(0.8, 0.46, 0.8, 0.8))  # 0.07 behind: not clearly the smallest
    assert w.done == frozenset()
    p.pinch(0, low, others=(0.8, 0.48, 0.8, 0.8))  # 0.09 behind
    assert w.done == {("right", 0)}


def test_u7_condition_5_a_neighbour_far_behind_a_clean_pinch_does_not_stop_it() -> None:
    """The margin of condition 4 is the whole rule: a finger that is clearly behind the pinching one is not "nearly as
    closed", however low it sits. (An absolute floor on the others used to refuse every pinch of a relaxed hand, whose
    neighbours rest at 0.3 to 0.5 of the thumb while one finger pinches it, and the warm-up never completed.)"""
    clean = (0.8, 0.55, 0.30, 0.15, 0.05, 0.15, 0.30, 0.55, 0.8)
    w, p = pinch_warmup()
    p.pinch(0, clean, others=(0.8, 0.30, 0.8, 0.8))  # 0.25 behind, and under the old floor of 0.45
    assert w.done == {("right", 0)}
    w, p = pinch_warmup()
    p.pinch(0, clean, others=(0.8, 0.12, 0.8, 0.8))  # 0.07 behind: the margin, not a floor, says no
    assert w.done == frozenset()


def test_u7_condition_6_fewer_than_three_fingers_curled() -> None:
    w, p = pinch_warmup()
    p.pinch(0, DOWN, curled=(False, True, True, True))
    assert w.done == frozenset()
    p.pinch(0, DOWN, curled=(False, True, True, False))
    assert w.done == {("right", 0)}


def test_u7_condition_7_the_hand_must_stay_slow_throughout_the_cycle() -> None:
    w, p = pinch_warmup()
    p.pinch(0, DOWN, speed=0.5)
    assert w.done == frozenset()
    p.pinch(0, DOWN, speed=0.49)
    assert w.done == {("right", 0)}


def test_u7_a_fast_hand_before_the_cycle_does_not_matter() -> None:
    w, p = pinch_warmup()
    for _ in range(5):
        p.frame(speed=2.0)
    p.pinch(0, DOWN)
    assert w.done == {("right", 0)}


def test_u7_a_tracking_gap_in_the_middle_of_a_cycle_voids_it() -> None:
    w, p = pinch_warmup()
    p.pinch(0, (0.8, 0.8, 0.55, 0.45, 0.30))
    p.t += 0.4  # a stalled camera
    p.pinch(0, (0.45, 0.55, 0.8))
    assert w.done == frozenset()


def test_u7_a_flipped_label_does_not_move_the_records() -> None:
    w, p = pinch_warmup(HOME_BOTH)
    p.pinch(0, (0.8, 0.8, 0.55, 0.45, 0.30))
    p.side = "left"  # the engine's newest guess for the same track
    p.pinch(0, (0.45, 0.55, 0.8))
    assert w.done == {("right", 0)}


def test_u7_the_records_belong_to_the_side_not_to_the_track() -> None:
    w, p = pinch_warmup()
    p.pinch(0)
    p.hid = 7  # the hand left and came back as a new track
    p.pinch(1)
    assert w.done == {("right", 0), ("right", 1)}


def test_u7_a_hand_that_is_not_one_of_the_placed_hands_records_nothing() -> None:
    w, _ = pinch_warmup(HOME_RIGHT)
    left = Pinches(w, side="left", hid=2)
    for f in range(4):
        left.pinch(f)
    assert w.done == frozenset() and w.required == frozenset(HOME_RIGHT)


def test_u7_without_home_positions_the_first_frame_with_a_hand_fixes_the_required_set() -> None:
    w = Warmup(TUNING, "pinch")
    assert w.required == frozenset() and not w.complete
    p = Pinches(w, side="left")
    p.frame()
    assert w.required == {("left", f) for f in range(4)}
    for f in range(4):
        p.pinch(f)
    assert w.complete
    late = Pinches(w, side="right", hid=9)
    late.pinch(0)  # a hand that arrived later was not placed
    assert w.required == {("left", f) for f in range(4)}


def test_u7_without_home_positions_two_hands_in_the_first_frame_are_both_required() -> None:
    w = Warmup(TUNING, "pinch")
    w.update([hand(0.0, side="left", hid=1), hand(0.0, side="right", hid=2)])
    assert len(w.required) == 8


def test_u7_two_hands_with_the_same_label_take_one_side_each() -> None:
    w = Warmup(TUNING, "pinch")
    w.update([hand(0.0, side="right", hid=1), hand(0.0, side="right", hid=2)])
    assert w.required == {(s, f) for s in ("left", "right") for f in range(4)}


def test_u7_a_third_hand_is_ignored() -> None:
    w = Warmup(TUNING, "pinch")
    w.update([hand(0.0, side="left", hid=1), hand(0.0, side="right", hid=2), hand(0.0, side="right", hid=3)])
    assert len(w.required) == 8


def test_u7_the_pinch_warmup_returns_nothing_and_has_no_air_state() -> None:
    w, _ = pinch_warmup()
    assert w.update([hand(0.0)], [object()], t=3.0, plane=None, rejects=9) == ()  # type: ignore[list-item]
    assert w.prompt is None and w.strays == 0 and w.depth == {}


def test_u7_an_empty_frame_changes_nothing() -> None:
    w, p = pinch_warmup()
    p.pinch(0)
    w.update([])
    assert w.done == {("right", 0)}


def feed_at(w: Warmup, rows: Sequence[tuple[float, float, float]], *, finger: int = 0) -> None:
    """(t, ratio of ``finger``, hand speed) rows; the other three fingers stay open."""
    for t, r, speed in rows:
        ratios = [0.8, 0.8, 0.8, 0.8]
        ratios[finger] = r
        w.update([hand(t, ratios, speed=speed)])


def test_u7_the_edges_of_the_cycle_are_exact() -> None:
    # a finger that returns to exactly 0.50 has not risen above it: a long stay there is a slow cycle
    w, p = pinch_warmup()
    p.pinch(0, (0.8, 0.45, 0.30, 0.50) + (0.50,) * 100 + (0.8,))
    assert w.done == frozenset()
    # and one that rests at exactly 0.50 has not fallen below it: the cycle starts at the real fall
    w, p = pinch_warmup()
    p.pinch(0, (0.8,) + (0.50,) * 100 + (0.45, 0.30, 0.45, 0.55, 0.8))
    assert w.done == {("right", 0)}


def test_u7_the_three_seconds_are_counted_from_the_fall_to_the_rise_even_between_sparse_frames() -> None:
    def cycle(rise_n: int) -> Warmup:
        """Frames 0.2 s apart (inside the tracking gap): the fall at 0.1, the rise at 0.3 + 0.2 * rise_n."""
        w, _ = pinch_warmup()
        feed_at(w, [(0.0, 0.8, 0.1), (0.1, 0.45, 0.1)])
        feed_at(w, [(round(0.3 + 0.2 * n, 6), 0.30, 0.1) for n in range(rise_n)])
        feed_at(w, [(round(0.3 + 0.2 * rise_n, 6), 0.8, 0.1)])
        return w

    assert cycle(13).done == {("right", 0)}  # rises at 2.9, 2.8 s after the fall
    assert cycle(15).done == frozenset()  # rises at 3.3, 3.2 s after it, with no slow low frame in between to void it


def test_u7_a_burst_of_speed_in_the_middle_of_the_cycle_spoils_it_even_if_the_hand_is_calm_at_the_end() -> None:
    w, _ = pinch_warmup()
    rows = [(k * DT, r, 0.9 if k == 5 else 0.1) for k, r in enumerate(DOWN)]
    feed_at(w, rows)
    assert w.done == frozenset()


def test_u7_another_finger_exactly_at_point_four_five_is_not_below_it() -> None:
    w, p = pinch_warmup()
    p.pinch(0, DOWN, others=(0.8, 0.45, 0.8, 0.8))
    assert w.done == {("right", 0)}


def test_u7_a_frame_without_the_hand_voids_the_cycle_in_progress() -> None:
    w, p = pinch_warmup()
    p.pinch(0, (0.8, 0.8, 0.55, 0.45, 0.30))
    w.update([])  # the tracker lost the hand for one frame
    p.pinch(0, (0.45, 0.55, 0.8))
    assert w.done == frozenset()


def test_u7_a_finger_must_open_fully_again_before_another_cycle_counts() -> None:
    w, p = pinch_warmup()
    p.pinch(0, (0.8, 0.55, 0.45, 0.30, 0.45, 0.55))
    first = w.thresholds()[("right", 0)]
    p.pinch(0, (0.45, 0.24, 0.45, 0.55, 0.58))  # it only came back to 0.55, then went down again
    assert w.thresholds()[("right", 0)] == first
    p.pinch(0, (0.8, 0.45, 0.24, 0.45, 0.55, 0.8))  # after opening fully the next cycle counts
    assert w.thresholds()[("right", 0)] != first


# ------------------------------------------------------------------------------------------------ the rig itself

HOME_CHARS = {("left", 3): "a", ("left", 2): "s", ("left", 1): "d", ("left", 0): "f"}
HOME_CHARS |= {("right", 0): "j", ("right", 1): "k", ("right", 2): "l", ("right", 3): "'"}


def direct_rig(**kw: object) -> KbRig:
    rig = KbRig(commit="direct", **kw)  # type: ignore[arg-type]
    rig.arm()
    return rig


def schedule(rig: KbRig, chars: str, *, gap: float = 0.35, start: float | None = None) -> float:
    """Taps ``chars`` one after the other from a little after now; returns the time of the first."""
    t0 = rig.t + 0.2 if start is None else start
    for i, c in enumerate(chars):
        rig.tap(t0 + i * gap, c)
    return t0


@pytest.mark.parametrize("commit", ["direct", "review"])
def test_the_rig_puts_every_fingertip_on_its_home_key(commit: str) -> None:
    rig = KbRig(commit=commit)  # type: ignore[arg-type]
    rig.place()
    layout = layout_for(commit)  # type: ignore[arg-type]
    for who, char in HOME_CHARS.items():
        u, v = rig.session.home_f[who]
        key = layout.key_at(u, v)
        assert key is not None and key.en == char
        assert abs(u - (key.col + 0.5)) < 0.1 and abs(v - 1.5) < 0.05


def test_the_rig_with_one_hand_places_that_hand_by_its_label() -> None:
    rig = KbRig(commit="direct", sides=("right",))
    rig.place()
    assert set(rig.session.home_f) == {("right", f) for f in range(4)}
    u, _ = rig.session.home_f[("right", 0)]
    assert u == pytest.approx(8.0 - 1.5, abs=0.1)


def test_scripted_press_delivers_an_event_at_the_first_update_that_sees_a_hand_at_that_time() -> None:
    press = ScriptedPress("pinch")
    event = PressEvent(1.0, 0.9, 1, "left", 0, (0.5, 0.5), 0.2, 0.2)
    press.emit_at(1.0, event)
    assert press.update([]) == []  # no hand, nothing goes out
    assert press.pending == 1

    def sample(t: float) -> HandSample:
        return hand(t, side="left")

    assert press.update([sample(0.9)]) == []
    assert press.update([sample(1.0)]) == [event]
    assert press.update([sample(1.1)]) == []
    assert press.updates == 4


def test_scripted_press_orders_events_by_time_then_by_order_of_scripting() -> None:
    press = ScriptedPress("air")
    a = PressEvent(2.0, 1.9, 1, "left", 0, (0.0, 0.0), 0.0, 1.0)
    b = PressEvent(1.0, 0.9, 1, "right", 1, (0.0, 0.0), 0.0, 1.0)
    c = PressEvent(1.0, 0.9, 1, "right", 2, (0.0, 0.0), 0.0, 1.0)
    press.emit_at(2.0, a)
    press.emit_at(1.0, b)
    press.emit_at(1.0, c)
    assert press.update([hand(3.0)]) == [b, c, a]


def test_scripted_press_follows_the_press_contract() -> None:
    air, pinch = ScriptedPress("air"), ScriptedPress("pinch")
    assert air.requires_review and not pinch.requires_review
    pinch.set_finger("left", 0, 0.3, 0.4)
    with pytest.raises(ValueError):
        pinch.set_finger("left", 1, 0.3)  # a pinch threshold needs both ends
    air.set_finger("left", 0, 0.4)  # air ignores the second
    assert pinch.thresholds[("left", 0)] == (0.3, 0.4)
    pinch.reset()
    air.set_calibrating(True)
    air.set_level("degraded")
    assert (pinch.resets, air.calibrating, air.levels) == (1, True, ["degraded"])
    assert pinch.quality().noise is None and air.quality().fps == 30.0


def test_the_rig_runs_the_controller_loop_and_types_into_the_fake_desktop() -> None:
    rig = direct_rig()
    schedule(rig, "hello")
    rig.run(2.5)
    assert rig.typed == "hello" and rig.closed is None
    assert rig.counts["keys"] == 5 and rig.desktop.key_calls == [("char", c) for c in "hello"]


def test_the_rig_starts_the_sink_when_the_session_arms_and_not_before() -> None:
    rig = KbRig(commit="direct")
    rig.place()
    assert rig.sink is not None and not rig.session.armed
    assert rig.sink.target_name == ""  # not started: it has not read the target
    rig.warm()
    assert rig.session.armed and rig.sink.target_name == "FakeTerminal"


# ------------------------------------------------------------------------------------------------------ placing (2.4)


def wave(rig: KbRig, seconds: float, *, amp: float = 3.0) -> None:
    """Hands swinging about 1 fw/s: never still."""
    for _ in range(round(seconds * rig.fps)):
        t = rig.t + rig.dt
        rig.feed([rig.hands_frame(t, offset=(amp * math.sin(2 * math.pi * t), 0.0))])


def test_there_is_no_default_plane_and_typing_is_off_before_a_hand_has_been_still() -> None:
    rig = KbRig(commit="direct")
    rig.tap(0.2, "f")
    rig.run(0.55)
    assert rig.session.plane is None and rig.session.phase == "placing" and not rig.session.armed
    assert rig.view.strip == SESSION_TEXT["place"] and rig.view.phase == "placing" and rig.view.tips == ()
    assert rig.typed == "" and rig.counts["not_armed"] == 1 and rig.counts["keys"] == 0


def test_a_hand_that_has_been_still_for_point_six_seconds_places_the_plane() -> None:
    rig = KbRig(commit="direct")
    rig.run(0.55)
    assert rig.session.plane is None
    rig.run(0.1)
    assert rig.session.plane is not None and rig.session.phase == "warmup"
    assert 0.6 <= rig.t <= 0.7  # the first frame is at 1/30 s and the hand is still from then on


def test_a_moving_hand_is_not_placed_until_it_has_stopped_and_stayed_still() -> None:
    rig = KbRig(commit="direct")
    wave(rig, 3.0)
    assert rig.session.plane is None
    rig.run(0.4)
    assert rig.session.plane is None
    rig.run(1.2)
    assert rig.session.plane is not None


def test_a_hand_with_three_curled_fingers_is_not_still() -> None:
    rig = KbRig(commit="direct", idle_s=600)
    rig.run(1.8, fist=("left",))  # one fist: the left hand is not still, the right hand is (after the grace second)
    assert rig.session.plane is not None and set(rig.session.home_f) == {("right", f) for f in range(4)}


def test_placing_waits_a_moment_for_the_second_hand_to_settle() -> None:
    """Deviation 1: one plane for both hands is worth up to a second (design: as soon as one hand qualifies)."""
    rig = KbRig(commit="direct")
    for k in range(1, 41):
        t = k * rig.dt
        # the left hand joins at 0.3 s; the right hand has been there from the start
        rig.feed([rig.hands_frame(t, sides=("left", "right") if t >= 0.3 else ("right",))])
        if rig.session.plane is not None:
            break
    assert {side for side, _ in rig.session.home_f} == {"left", "right"}
    assert 0.9 <= rig.t <= 1.0  # the left hand has been still for 0.6 s


def test_placing_goes_on_with_one_hand_when_the_other_does_not_settle() -> None:
    rig = KbRig(commit="direct", idle_s=600)
    for k in range(1, 90):
        t = k * rig.dt
        moving = rig.hands_frame(t, sides=("left",), offset=(3 * math.sin(2 * math.pi * t), 0.0))
        still = rig.hands_frame(t, sides=("right",))
        rig.feed([type(still)(t, still.hands + moving.hands, still.width, still.height)])
        if rig.session.plane is not None:
            break
    assert {side for side, _ in rig.session.home_f} == {"right"}
    assert 1.6 <= rig.t <= 1.8  # 0.6 s to qualify, then the one-second grace


@pytest.mark.parametrize("commit", ["direct", "review"])
def test_the_plane_follows_the_layout_and_the_reach(commit: str) -> None:
    rig = KbRig(commit=commit, reach=1.5)  # type: ignore[arg-type]
    rig.place()
    plane = rig.session.plane
    assert plane is not None and plane.rows == (5 if commit == "review" else 4)
    # the hands are one pitch apart at reach 1.0, which is below 0.90 of the pitch at reach 1.5: clamped to 0.90
    assert plane.px == pytest.approx(0.90 * PITCH * 1.5)


def test_placing_times_out_after_thirty_seconds_without_a_still_hand() -> None:
    rig = KbRig(commit="direct", idle_s=600)
    wave(rig, 29.8)
    assert rig.closed is None
    wave(rig, 0.4)
    assert rig.closed == "idle" and 29.99 <= rig.t <= 30.07


def test_a_placement_that_was_made_is_not_timed_out_by_the_place_limit() -> None:
    rig = KbRig(commit="direct", idle_s=600)
    rig.place()
    rig.run(31.0)
    assert rig.closed is None


def test_arming_times_out_after_ninety_seconds() -> None:
    rig = KbRig(commit="direct", idle_s=600)
    rig.place()
    rig.run(89.0 - rig.t)
    assert rig.closed is None
    rig.run(1.2)
    assert rig.closed == "idle" and 89.99 <= rig.t <= 90.07


# ----------------------------------------------------------------------------------- warm-up through the session


def test_the_pinch_warmup_strip_counts_the_fingers_recorded() -> None:
    rig = KbRig(commit="direct")
    rig.place()
    assert rig.view.strip == "Pinch each finger to your thumb once: 0/8" and rig.view.progress == 0.0
    for k in PINCH_PROFILE:
        rig.feed([rig.hands_frame(rig.t + rig.dt, pinch={"right": (0, k)})])
    assert rig.view.strip == "Pinch each finger to your thumb once: 1/8"
    assert rig.view.progress == pytest.approx(1 / 8)


def test_the_warmup_of_one_hand_needs_four_fingers() -> None:
    rig = KbRig(commit="direct", sides=("right",))
    rig.place()
    assert rig.view.strip == "Pinch each finger to your thumb once: 0/4"
    rig.warm()
    assert rig.session.armed and set(rig.press.thresholds) == {("right", f) for f in range(4)}


def test_arming_sets_every_finger_threshold_resets_the_press_and_starts_typing_in_one_frame() -> None:
    rig = KbRig(commit="direct")
    rig.place()
    session = rig.session
    for side in rig.sides:
        for finger in range(4):
            for k in PINCH_PROFILE:
                resets = rig.press.resets
                assert not session.armed
                rig.feed([rig.hands_frame(rig.t + rig.dt, pinch={side: (finger, k)})])
                if session.armed:
                    break
            if session.armed:
                break
        if session.armed:
            break
    assert session.armed and session.phase == "typing"
    assert rig.press.resets == resets + 1  # the last warm-up pinch can never type
    assert set(rig.press.thresholds) == {(s, f) for s in ("left", "right") for f in range(4)}
    for close, open_ in rig.press.thresholds.values():
        assert 0.22 <= close <= 0.36 and open_ >= max(0.40, close + 0.12) - 1e-9
    assert rig.view.phase == "typing" and rig.view.progress == 0.0


def test_a_press_in_the_warmup_is_discarded_as_not_armed() -> None:
    rig = KbRig(commit="direct")
    rig.place()
    rig.tap(rig.t + 0.1, "f")
    rig.run(0.4)
    assert rig.counts["not_armed"] == 1 and rig.typed == "" and rig.counts["keys"] == 0


def test_s19_a_session_that_was_never_warmed_up_types_nothing_whatever_is_pressed() -> None:
    rig = KbRig(commit="direct", idle_s=600)
    for i, c in enumerate("hello world, this is a perfect sequence"[:12]):
        rig.tap(0.2 + i * 0.4, c if c != " " else "space")
    rig.run(8.0)
    assert rig.typed == "" and rig.desktop.key_calls == [] and rig.counts["keys"] == 0
    assert rig.counts["not_armed"] == 12 and not rig.session.armed


# ------------------------------------------------------------------------------------------ direct keys (2.7 step 9)


def test_the_four_kinds_of_stroke_a_direct_session_makes() -> None:
    rig = direct_rig()
    rig.tap(rig.t + 0.2, "a")
    rig.tap(rig.t + 0.6, "space")
    rig.tap(rig.t + 1.0, "backspace")
    rig.run(1.5)
    assert rig.desktop.key_calls == [("char", "a"), ("control", "space"), ("control", "backspace")]


def test_u13_the_echo_is_what_was_sent_backspace_removes_one_enter_clears() -> None:
    rig = direct_rig()
    schedule(rig, "abc")
    rig.run(1.4)
    assert rig.view.echo == "abc"
    rig.tap(rig.t + 0.1, "backspace")
    rig.run(0.5)
    assert rig.view.echo == "ab"
    rig.tap(rig.t + 0.1, "space")
    rig.run(0.5)
    assert rig.view.echo == "ab "
    rig.tap(rig.t + 0.1, "enter")
    rig.tap(rig.t + 0.5, "enter")
    rig.run(1.0)
    assert rig.view.echo == "" and rig.typed.endswith("\n")


def test_u13_the_echo_keeps_the_last_twenty_four_characters() -> None:
    rig = direct_rig()
    text = "abcdefghijklmnopqrstuvwxyzabcd"
    schedule(rig, text)
    rig.run(len(text) * 0.35 + 0.3)
    assert rig.view.echo == text[-24:] and rig.typed == text


def test_u13_the_echo_shows_only_what_the_sink_took() -> None:
    rig = direct_rig()
    rig.desktop.target = KeyTarget(100, 200, "FakeTerminal", 0x0409, None, True, False)  # a password box has the focus
    schedule(rig, "abc")
    rig.run(1.4)
    assert rig.view.echo == "" and rig.typed == ""


def test_u13_a_hold_empties_the_echo_and_drops_what_was_waiting() -> None:
    rig = direct_rig()
    schedule(rig, "abc")
    rig.run(1.4)
    assert rig.view.echo == "abc"
    rig.desktop.user_typed()  # the user's own keyboard: hold `yield`
    rig.run(0.1)
    assert rig.view.hold == "yield" and rig.view.echo == ""
    rig.run(2.0)  # the hold ends; the echo does not come back
    assert rig.view.hold is None and rig.view.echo == ""


def test_u13_private_mode_has_no_echo_and_leaving_it_does_not_bring_the_old_one_back() -> None:
    rig = direct_rig()
    schedule(rig, "abc")
    rig.run(1.4)
    rig.session.set_private(True)
    rig.tap(rig.t + 0.1, "d")
    rig.run(0.5)
    assert rig.view.private and rig.view.echo == "" and rig.typed == "abcd"
    rig.session.set_private(False)
    rig.tap(rig.t + 0.1, "e")
    rig.run(0.5)
    assert rig.view.echo == "e"


def test_u14_shift_is_one_shot() -> None:
    rig = direct_rig()
    rig.tap(rig.t + 0.2, "shift")
    rig.run(0.3)
    assert rig.view.shift
    rig.tap(rig.t + 0.1, "a")
    rig.tap(rig.t + 0.6, "a")
    rig.run(1.2)
    assert rig.typed == "Aa" and not rig.view.shift


def test_u14_shift_expires_after_five_seconds() -> None:
    rig = direct_rig()
    rig.tap(rig.t + 0.2, "shift")
    rig.tap(rig.t + 4.2, "a")  # 4.0 s after the Shift
    rig.tap(rig.t + 4.4, "shift")
    rig.tap(rig.t + 10.0, "b")  # 5.6 s after the second Shift
    rig.run(10.5)
    assert rig.typed == "Ab"


def test_u14_shift_is_inert_in_hebrew() -> None:
    rig = KbRig(commit="direct", lang="he")
    rig.arm()
    rig.tap(rig.t + 0.2, "shift")
    rig.tap(rig.t + 0.6, "a")
    rig.run(1.2)
    assert rig.counts["shift_inert"] == 1 and not rig.view.shift
    assert rig.typed == char_for(rig.key("a"), "he", False)


def test_u15_lang_toggles_the_layout_and_clears_shift() -> None:
    rig = direct_rig()
    rig.tap(rig.t + 0.2, "shift")
    rig.tap(rig.t + 0.6, "lang")
    rig.tap(rig.t + 1.0, "a")
    rig.tap(rig.t + 1.4, "lang")
    rig.tap(rig.t + 1.8, "a")
    rig.run(2.4)
    key = rig.key("a")
    assert rig.typed == char_for(key, "he", False) + "a"  # Shift did not survive either toggle
    assert rig.view.lang == "en"


def test_u15_the_strip_names_the_target_and_the_layout() -> None:
    rig = direct_rig()
    assert rig.view.strip == "-> FakeTerminal   EN"
    rig.tap(rig.t + 0.2, "lang")
    rig.run(0.5)
    assert rig.view.strip == "-> FakeTerminal   HE" and rig.view.lang == "he"


def test_u16_home_returns_to_placing_with_armed_kept_and_places_again_without_a_warmup() -> None:
    rig = direct_rig()
    old = rig.session.plane
    rig.tap(rig.t + 0.2, "home")
    rig.run(0.4)
    assert rig.session.phase == "placing" and rig.session.armed and rig.view.phase == "placing"
    assert rig.view.strip == SESSION_TEXT["place"]
    resets = rig.press.resets
    rig.tap(rig.t + 0.1, "f")  # a tap while placing is discarded
    Hands(rig).go(2.0, seconds=2.0)  # the hands have moved two keys
    assert rig.session.phase == "typing" and rig.session.armed
    assert rig.press.resets == resets + 1
    assert rig.counts["not_armed"] == 1 and rig.typed == ""
    new = rig.session.plane
    assert old is not None and new is not None and new.cx - old.cx == pytest.approx(2.0 * PITCH, abs=0.003)
    rig.tap(rig.t + 0.1, "f")
    rig.run(0.5)
    assert rig.typed == "f"  # the new plane puts the finger on the same key


def test_u16_recenter_called_from_outside_does_the_same() -> None:
    rig = direct_rig()
    rig.session.recenter()
    rig.run(0.1)
    assert rig.session.phase == "placing" and rig.session.armed


def test_u16_home_before_arming_warms_up_again_from_the_new_placement() -> None:
    rig = KbRig(commit="direct")
    rig.place()
    rig.session.recenter()
    rig.run(0.1)
    assert rig.session.phase == "placing" and not rig.session.armed
    rig.place()
    assert rig.session.phase == "warmup"
    rig.warm()
    assert rig.session.armed


def test_u16_home_clears_the_drift_indicator_and_the_waiting_taps() -> None:
    rig = direct_rig()
    rig.tap(rig.t + 0.1, "a")
    rig.tap(rig.t + 0.1, "s")
    rig.tap(rig.t + 0.1, "d")
    rig.run(0.12)  # one has gone out, two wait in the queue
    rig.session.recenter()
    rig.run(2.0)
    assert rig.typed == "a" and rig.counts["not_armed"] == 2  # the other two never type
    assert not rig.view.drift


class Hands:
    """The rig's hands, moved about by key units from where the session placed them."""

    def __init__(self, rig: KbRig) -> None:
        self.rig = rig
        self.at = (0.0, 0.0)

    def go(self, du: float, dv: float = 0.0, seconds: float = 0.0, *, frames: int = 3) -> None:
        """To ``(du, dv)`` in ``frames`` frames, then stay there for ``seconds``."""
        start = self.at
        for k in range(1, frames + 1):
            f = k / frames
            self.rig.feed(
                [
                    self.rig.hands_frame(
                        self.rig.t + self.rig.dt,
                        offset=(start[0] + (du - start[0]) * f, start[1] + (dv - start[1]) * f),
                    )
                ]
            )
        self.at = (du, dv)
        self.stay(seconds)

    def stay(self, seconds: float) -> None:
        for _ in range(round(seconds * self.rig.fps)):
            self.rig.feed([self.rig.hands_frame(self.rig.t + self.rig.dt, offset=self.at)])


def test_u17_hands_that_rest_more_than_point_six_units_from_home_for_two_seconds_set_the_indicator() -> None:
    rig = direct_rig()
    hands = Hands(rig)
    hands.go(0.7, seconds=1.8)
    assert not rig.view.drift
    hands.stay(2.0)  # the hand needed about half a second to count as still: now it has rested for over two seconds
    assert rig.view.drift and rig.view.strip == SESSION_TEXT["drifted"]


def test_u17_half_a_unit_away_never_sets_it() -> None:
    rig = direct_rig()
    Hands(rig).go(0.5, seconds=6.0)
    assert not rig.view.drift


def test_u17_it_clears_below_point_four_and_not_between_point_four_and_point_six() -> None:
    rig = direct_rig()
    hands = Hands(rig)
    hands.go(0.8, seconds=4.0)
    assert rig.view.drift
    hands.go(0.5, seconds=2.5)  # between the two thresholds: unchanged
    assert rig.view.drift
    hands.go(0.3, seconds=2.0)
    assert not rig.view.drift


def test_u17_a_moving_hand_is_not_compared() -> None:
    rig = direct_rig()
    for _ in range(round(6 * rig.fps)):  # far from home all the time but never still
        t = rig.t + rig.dt
        rig.feed([rig.hands_frame(t, offset=(3.0 + 2.0 * math.sin(2 * math.pi * t), 0.0))])
    assert not rig.view.drift


def test_u17_the_indicator_never_moves_the_plane() -> None:
    rig = direct_rig()
    plane = rig.session.plane
    Hands(rig).go(0.8, seconds=4.0)
    assert rig.view.drift and rig.session.plane is plane


def test_u17_with_one_hand_the_hand_is_compared_to_its_own_home() -> None:
    rig = KbRig(commit="direct", sides=("right",))
    rig.arm()
    Hands(rig).go(0.8, seconds=4.0)
    assert rig.view.drift


def test_s16_one_enter_press_sends_nothing_and_arms() -> None:
    rig = direct_rig()
    rig.tap(rig.t + 0.2, "enter")
    rig.run(0.5)
    assert rig.typed == "" and rig.view.armed_enter and rig.view.strip == SESSION_TEXT["enter_again"]
    rig.run(1.3)  # 1.5 s after the press the arming has lapsed
    assert rig.typed == "" and not rig.view.armed_enter


def test_s16_two_enter_presses_within_one_and_a_half_seconds_send_one_enter() -> None:
    rig = direct_rig()
    rig.tap(rig.t + 0.2, "enter")
    rig.tap(rig.t + 1.4, "enter")
    rig.run(2.0)
    assert rig.desktop.key_calls == [("control", "enter")] and not rig.view.armed_enter


def test_s16_the_second_press_after_one_and_a_half_seconds_only_arms_again() -> None:
    rig = direct_rig()
    rig.tap(rig.t + 0.2, "enter")
    rig.tap(rig.t + 1.9, "enter")
    rig.run(2.4)
    assert rig.typed == "" and rig.view.armed_enter
    rig.tap(rig.t + 0.2, "enter")
    rig.run(0.6)
    assert rig.typed == "\n"


def test_s16_another_key_between_the_two_presses_disarms() -> None:
    rig = direct_rig()
    rig.tap(rig.t + 0.2, "enter")
    rig.tap(rig.t + 0.6, "a")
    rig.tap(rig.t + 1.0, "enter")
    rig.run(1.6)
    assert rig.typed == "a" and rig.view.armed_enter  # the second press is a first press


def test_s16_a_hold_between_the_two_presses_disarms() -> None:
    rig = direct_rig()
    rig.tap(rig.t + 0.2, "enter")
    rig.run(0.4)
    rig.desktop.user_typed()
    rig.run(0.1)
    assert not rig.view.armed_enter


def test_s16_with_enter_off_the_key_does_nothing() -> None:
    rig = KbRig(commit="direct", enter="off")
    rig.arm()
    rig.tap(rig.t + 0.2, "enter")
    rig.tap(rig.t + 0.6, "enter")
    rig.run(1.2)
    assert rig.typed == "" and rig.counts["enter_off"] == 2 and not rig.view.armed_enter


def test_the_close_key_closes_at_once() -> None:
    rig = direct_rig()
    rig.tap(rig.t + 0.2, "close")
    rig.run(0.5)
    assert rig.closed == "close_key" and rig.typed == ""


def test_the_home_row_key_press_of_a_private_session_pulses_instead_of_lighting_keys() -> None:
    rig = direct_rig()
    rig.session.set_private(True)
    rig.tap(rig.t + 0.2, "f")
    pulses = []
    for _ in range(round(0.6 * rig.fps)):
        rig.feed([rig.hands_frame(rig.t + rig.dt)])
        pulses.append(rig.view.pulse)
    assert any(pulses) and not pulses[-1]
    layout = rig.layout
    assert all(layout.keys[i].kind not in ("char", "space") for v in rig.views for i, _ in v.lit if v.private)


# ----------------------------------------------------------------------------------------- the queue and the pace


def strokes_at(rig: KbRig) -> list[float]:
    """The frame times at which a stroke went out."""
    return [r.t for r in rig.records if r.strokes]


def test_b8_a_fourth_resolved_event_in_one_frame_is_dropped() -> None:
    rig = direct_rig()
    t = rig.t + 0.2
    for c in "asdf":
        rig.tap(t, c)
    rig.run(1.0)
    assert rig.typed == "asd" and rig.counts["queue"] == 1 and rig.counts["keys"] == 3


def test_b8_waiting_taps_go_out_one_per_frame_at_least_point_zero_six_seconds_apart() -> None:
    rig = direct_rig()
    t = rig.t + 0.2
    for c in "asd":
        rig.tap(t, c)
    rig.run(1.0)
    times = strokes_at(rig)
    assert len(times) == 3 and all(r.strokes <= 1 for r in rig.records)
    assert all(b - a >= 0.06 - 1e-9 for a, b in pairwise(times))
    assert all(b - a <= 0.07 for a, b in pairwise(times))  # two frames at 30 fps


def test_b8_a_tap_older_than_point_three_seconds_is_dropped_as_stale() -> None:
    rig = direct_rig(fps=20.0)
    at = rig.t + 0.2
    rig.tap(at, "a", age=0.35)
    rig.run(0.6)
    assert rig.typed == "" and rig.counts["stale"] == 1


def test_b8_the_age_limit_is_inclusive() -> None:
    rig = direct_rig(fps=20.0)
    rig.tap(rig.t + 0.2, "a", age=0.30)
    rig.tap(rig.t + 0.6, "s", age=0.31)
    rig.run(1.0)
    assert rig.typed == "a" and rig.counts["stale"] == 1


def test_b8_a_tap_that_waited_in_the_queue_too_long_is_stale() -> None:
    rig = direct_rig()
    t = rig.t + 0.2
    for c in "asd":
        rig.tap(t, c, age=0.15)  # already 0.15 s old when the camera reports it
    rig.run(0.6)
    # a goes out at once; s waits 0.067 s and is 0.217 s old; d waits 0.133 s and is 0.283 s old: all inside 0.30 s
    assert rig.typed == "asd"
    rig2 = direct_rig()
    t = rig2.t + 0.2
    for c in "asd":
        rig2.tap(t, c, age=0.20)
    rig2.run(0.6)
    assert rig2.typed == "as" and rig2.counts["stale"] == 1  # d would be 0.333 s old


def test_b8_a_camera_stall_resets_the_press_and_the_waiting_tap_is_stale() -> None:
    rig = direct_rig()
    t = rig.t + 0.2
    rig.tap(t, "a")
    rig.tap(t, "s")
    while rig.typed != "a":
        rig.run(rig.dt)
    resets = rig.press.resets
    rig.feed([rig.hands_frame(rig.t + 0.5)])  # nothing for half a second
    rig.run(0.5)
    assert rig.typed == "a" and rig.counts["stale"] == 1 and rig.press.resets == resets + 1


def test_a_gap_of_exactly_a_quarter_second_is_not_a_stall() -> None:
    rig = direct_rig()
    resets = rig.press.resets
    rig.feed([rig.hands_frame(rig.t + 0.25)])
    assert rig.press.resets == resets
    rig.feed([rig.hands_frame(rig.t + 0.26)])
    assert rig.press.resets == resets + 1


@pytest.mark.parametrize("fps", [15.0, 60.0])
def test_b6_typing_at_other_frame_rates(fps: float) -> None:
    rig = direct_rig(fps=fps)
    schedule(rig, "hello world".replace(" ", "x"))
    rig.run(5.0)
    assert rig.typed == "helloxworld" and rig.view.hold is None


def test_b6_below_ten_frames_a_second_sets_the_slow_hold_and_above_twelve_clears_it() -> None:
    rig = direct_rig()
    rig.dt, rig.fps = 1 / 8, 8.0
    rig.run(1.5)
    assert rig.view.hold == "slow" and rig.view.strip == "Paused: the camera is too slow"
    rig.tap(rig.t + 0.5, "a")
    rig.run(1.0)
    assert rig.typed == "" and rig.counts["held"] == 1
    rig.dt, rig.fps = 1 / 12, 12.0  # 12 fps is still inside the hysteresis band
    rig.run(2.0)
    assert rig.view.hold == "slow"
    resets = rig.press.resets
    rig.dt, rig.fps = 1 / 13, 13.0
    rig.run(1.2)
    assert rig.view.hold is None and rig.press.resets == resets + 1
    rig.tap(rig.t + 0.1, "b")
    rig.run(0.5)
    assert rig.typed == "b"


def test_b6_ten_frames_a_second_is_not_slow() -> None:
    rig = direct_rig(fps=10.0)
    schedule(rig, "ab", gap=0.5)
    rig.run(4.0)
    assert rig.view.hold is None and rig.typed == "ab"


def test_b6_the_slow_hold_needs_a_median_not_a_single_late_frame() -> None:
    rig = direct_rig()
    rig.feed([rig.hands_frame(rig.t + 0.2)])  # one slow interval among thirty
    rig.run(0.3)
    assert rig.view.hold is None


# ---------------------------------------------------------------------------------------------------- the storm


def test_s1_four_fingers_rolling_at_eight_keys_a_second_close_runaway_at_the_twelfth_key() -> None:
    rig = direct_rig()
    t0 = rig.t + 0.2
    for i in range(STREAM):
        rig.tap(t0 + i * 0.125, "asdf"[i % 4])
    rig.run(6.0)
    assert rig.closed == "runaway" and len(rig.typed) == 11 and rig.counts["keys"] == 11


def test_s1_a_storm_is_not_survived_nothing_resumes() -> None:
    rig = direct_rig()
    t0 = rig.t + 0.2
    for i in range(14):
        rig.tap(t0 + i * 0.1, "asdf"[i % 4])
    rig.run(3.0)
    typed = rig.typed
    rig.session.update(rig.hands_frame(rig.t + 1.0), None, "")  # the session is closed: it only repeats the closing
    assert rig.session.closed == "runaway" and rig.typed == typed


def test_b7_thirty_presses_in_nine_seconds_with_one_finger_type_thirty_keys() -> None:
    rig = direct_rig()
    t0 = rig.t + 0.2
    for i in range(30):
        rig.tap(t0 + i * 0.3, "j")
    rig.run(10.0)
    assert rig.typed == "j" * 30 and rig.closed is None


def test_the_storm_breaker_looks_at_a_two_second_window() -> None:
    quiet = direct_rig()
    t0 = quiet.t + 0.2
    for i in range(STREAM):
        quiet.tap(t0 + i * 0.2, "asdf"[i % 4])  # 5 keys a second: 10 in 2 s
    quiet.run(9.0)
    assert quiet.closed is None and len(quiet.typed) == STREAM
    fast = direct_rig()
    t0 = fast.t + 0.2
    for i in range(STREAM):
        fast.tap(t0 + i * 0.15, "asdf"[i % 4])  # 12 in 1.65 s
    fast.run(8.0)
    assert fast.closed == "runaway" and len(fast.typed) == 11


# ----------------------------------------------------------------------------------------------- the holds (S8-S14)

HOLD_CASES = [
    ("yield", "Paused: you used the keyboard or mouse"),
    ("blocked", "Paused: that window cannot be typed into"),
    ("password", "Paused: that looks like a password box"),
    ("covered", "Paused: the screen is covered"),
    ("overlay", "Paused: the keyboard could not be drawn"),
    ("focus", "Paused: the window changed"),
]


def set_hold(rig: KbRig, hold: str) -> None:
    target = rig.desktop.target
    if hold == "yield":
        rig.desktop.user_typed()
    elif hold == "blocked":
        rig.desktop.target = KeyTarget(target.hwnd, target.pid, target.name, target.lang_id, "elevated", False, False)
    elif hold == "password":
        rig.desktop.target = KeyTarget(target.hwnd, target.pid, target.name, target.lang_id, None, True, False)
    elif hold == "covered":
        rig.desktop.target = KeyTarget(target.hwnd, target.pid, target.name, target.lang_id, None, False, True)
    elif hold == "overlay":
        rig.overlay_ok = False
    elif hold == "focus":
        rig.desktop.target = KeyTarget(target.hwnd + 1, target.pid, target.name, target.lang_id, None, False, False)


def clear_hold(rig: KbRig, hold: str) -> None:
    target = rig.desktop.target
    if hold in ("blocked", "password", "covered"):
        rig.desktop.target = KeyTarget(target.hwnd, target.pid, target.name, target.lang_id, None, False, False)
    elif hold == "overlay":
        rig.overlay_ok = True


@pytest.mark.parametrize(("hold", "strip"), HOLD_CASES)
def test_a_hold_discards_presses_dims_the_keyboard_and_resets_the_press_when_it_ends(hold: str, strip: str) -> None:
    rig = direct_rig()
    rig.run(0.5)  # the sink's warm-up window only re-baselines
    set_hold(rig, hold)
    rig.tap(rig.t + 0.2, "a")  # a focus change holds only half a second: both taps fall inside it
    rig.tap(rig.t + 0.35, "s")
    rig.run(0.45)
    assert rig.view.hold == hold and rig.view.strip == strip
    assert rig.typed == "" and rig.counts["held"] == 2 and rig.counts["keys"] == 0
    clear_hold(rig, hold)
    resets = rig.press.resets
    rig.run(3.0)
    assert rig.view.hold is None and rig.press.resets == resets + 1
    rig.tap(rig.t + 0.1, "d")
    rig.run(0.5)
    assert rig.typed == "d"  # nothing that happened during the hold typed, and typing resumes after it


def test_s8_foreign_input_holds_for_a_second_and_a_half() -> None:
    rig = direct_rig()
    rig.run(0.5)
    rig.desktop.user_typed()
    held_at = rig.t
    rig.tap(held_at + 1.2, "a")
    rig.tap(held_at + 1.8, "b")
    rig.run(2.5)
    assert rig.typed == "b" and rig.counts["held"] == 1


def test_s9_a_physical_modifier_holds_while_it_is_down() -> None:
    rig = direct_rig()
    rig.run(0.5)
    rig.desktop.modifiers = True
    rig.run(0.2)
    assert rig.view.hold == "yield"
    rig.run(3.0)
    assert rig.view.hold == "yield"
    rig.desktop.modifiers = False
    rig.run(0.2)
    assert rig.view.hold is None


def test_a_hold_that_begins_drops_what_was_waiting() -> None:
    rig = direct_rig()
    rig.run(0.5)
    t = rig.t + 0.2
    for c in "asd":
        rig.tap(t, c)
    while rig.typed != "a":
        rig.run(rig.dt)
    rig.desktop.user_typed()
    rig.run(2.5)
    assert rig.typed == "a" and rig.counts["held"] == 2


def test_s14_the_overlay_hold_keeps_the_session_open_and_the_press_running() -> None:
    rig = direct_rig()
    updates = rig.press.updates
    rig.overlay_ok = False
    rig.run(1.0)
    assert rig.view.hold == "overlay" and rig.closed is None
    assert rig.press.updates - updates == round(1.0 * rig.fps)  # the markers keep moving during a hold


# ----------------------------------------------------------------------------------------------------- closing


def test_s17_both_fists_for_a_second_close_in_every_phase() -> None:
    placing = KbRig(commit="direct")
    placing.run(0.9, fist=True)
    assert placing.closed is None
    placing.run(0.3, fist=True)
    assert placing.closed == "fists" and 1.0 <= placing.t <= 1.07

    warming = KbRig(commit="direct")
    warming.place()
    warming.run(2.0, fist=True)
    assert warming.closed == "fists"

    typing = direct_rig()
    typing.run(2.0, fist=True)
    assert typing.closed == "fists"


def test_s17_the_bar_fills_while_the_fists_are_held() -> None:
    rig = direct_rig()
    rig.run(0.5, fist=True)
    assert 0.4 <= rig.view.progress <= 0.6
    rig.run(0.2)  # the hands open: the bar empties
    assert rig.view.progress == 0.0


def test_s17_one_fist_never_closes() -> None:
    rig = KbRig(commit="direct", idle_s=600)
    rig.arm()
    rig.run(8.0, fist=("left",))
    assert rig.closed is None
    rig.run(3.0, fist=("right",))
    assert rig.closed is None


def test_s17_the_second_is_counted_from_the_start_of_an_unbroken_fist_pair() -> None:
    rig = direct_rig()
    rig.run(0.9, fist=True)
    rig.run(0.1)  # open for a frame or three
    rig.run(0.9, fist=True)
    assert rig.closed is None
    rig.run(0.3, fist=True)
    assert rig.closed == "fists"


def test_s17_a_stall_restarts_the_second() -> None:
    rig = direct_rig()
    rig.run(0.9, fist=True)
    rig.feed([rig.hands_frame(rig.t + 0.4, fist=True)])
    rig.run(0.9, fist=True)
    assert rig.closed is None


def test_s18_no_hand_for_thirty_seconds_closes_idle() -> None:
    rig = direct_rig()
    rig.empty(29.0)
    assert rig.closed is None
    rig.empty(1.2)
    assert rig.closed == "idle"


def test_s18_a_hand_that_comes_back_restarts_the_count() -> None:
    rig = direct_rig()
    rig.empty(25.0)
    rig.run(0.5)
    rig.empty(25.0)
    assert rig.closed is None


def test_s18_the_idle_limit_is_the_setting() -> None:
    rig = KbRig(commit="direct", idle_s=10)
    rig.arm()
    rig.empty(9.5)
    assert rig.closed is None
    rig.empty(1.0)
    assert rig.closed == "idle"


def test_s18_an_armed_session_with_no_accepted_key_for_three_hundred_seconds_closes_idle() -> None:
    rig = direct_rig(fps=10.0)
    armed_at = rig.t
    rig.run(150.0)
    rig.tap(rig.t + 0.1, "a")  # an accepted tap resets the count
    rig.run(298.0 - 0.1)
    assert rig.closed is None and rig.t - armed_at < NO_KEY_CLOSE_S + 150.0
    rig.run(3.0)
    assert rig.closed == "idle" and rig.typed == "a"


def test_close_is_idempotent_and_the_first_reason_stays() -> None:
    rig = direct_rig()
    rig.session.close("command")
    rig.session.close("idle")
    assert rig.session.closed == "command" and not rig.session.armed


def test_a_closed_session_only_repeats_its_closing() -> None:
    rig = direct_rig()
    rig.tap(rig.t + 0.1, "a")
    rig.session.close("command")
    out = rig.session.update(rig.hands_frame(rig.t + rig.dt), None, "")
    assert out.closed == "command" and out.strokes == () and out.steps == ()


def test_a_frame_that_closes_sends_no_stroke() -> None:
    rig = direct_rig()
    rig.tap(rig.t + 0.1, "a")
    rig.tap(rig.t + 0.1, "close")
    rig.run(0.5)
    assert rig.closed == "close_key" and rig.typed == "a"  # the letter had gone out, the Close key closed
