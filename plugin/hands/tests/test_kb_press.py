"""The press methods at unit level: P42, U8, U9, U10 and the rest of the pinch machine of 2.6 (DESIGN-KEYBOARD.md 3.4).

The hands are crafted ``HandSample`` streams: every ratio, reach and position is set by the test, so each rule of the
state machine has the stream that fails without it. The scenarios with synthetic hands are in ``test_kb_scenarios``.
"""

from __future__ import annotations

import numpy as np
import pytest

from jarvis_hands.keyboard.press import PressUnavailable, make_press
from jarvis_hands.keyboard.press_air import AirTapPress
from jarvis_hands.keyboard.press_pinch import PinchPress
from jarvis_hands.keyboard.tuning import Tuning
from jarvis_hands.keyboard.types import FingerSample, HandSample, PressEvent, PressQuality, Side

TUNING = Tuning()
DT = 1 / 30
OPEN = 1.0


def sample(
    t: float,
    ratios: tuple[float, float, float, float] = (OPEN,) * 4,
    *,
    reach: tuple[float, float, float, float] = (1.8,) * 4,
    curled: tuple[bool, bool, bool, bool] = (False,) * 4,
    speed: float = 0.0,
    hand: int = 1,
    side: Side = "right",
    aims: tuple[tuple[float, float], ...] | None = None,
    anchor: tuple[float, float] = (0.5, 0.4),
) -> HandSample:
    aims = aims or tuple((0.5 + 0.04 * f, 0.4) for f in range(4))
    fingers = tuple(FingerSample(f, np.array(aims[f], dtype=float), reach[f], ratios[f], curled[f]) for f in range(4))
    return HandSample(hand, side, t, 0.1, np.array(anchor), speed, fingers)


def with_finger(finger: int, ratio: float, base: float = OPEN) -> tuple[float, float, float, float]:
    out = [base] * 4
    out[finger] = ratio
    return tuple(out)  # type: ignore[return-value]


def stream(press: PinchPress, rows: list[dict], t0: float = 0.0, **kw) -> tuple[list[PressEvent], float]:
    """Feed one hand frame per row; a row is the ``sample`` keywords (ratios, reach, ...). Returns events and next t."""
    events: list[PressEvent] = []
    t = t0
    for row in rows:
        events += press.update([sample(t, **{**kw, **row})])
        t += DT
    return events, t


def settle(press: PinchPress, t0: float = 0.0, frames: int = 4, **kw) -> float:
    """Open hand frames, enough for every latched finger to open."""
    _, t = stream(press, [{}] * frames, t0, **kw)
    return t


def pinch_rows(finger: int, *, closing: int = 5, held: int = 3, opening: int = 3, floor: float = 0.12) -> list[dict]:
    """One pinch of ``finger``: the ratio falls from 1.0 to ``floor`` over ``closing`` frames, holds, and reopens."""
    rows = [{"ratios": with_finger(finger, OPEN + (floor - OPEN) * (i + 1) / closing)} for i in range(closing)]
    rows += [{"ratios": with_finger(finger, floor)}] * held
    rows += [{"ratios": with_finger(finger, floor + (OPEN - floor) * (i + 1) / opening)} for i in range(opening)]
    return rows


# --------------------------------------------------------------------------------------------------- P42: the registry


def test_p42_the_air_method_needs_the_review_box() -> None:
    with pytest.raises(PressUnavailable, match="only works with the review box"):
        make_press("air", TUNING)
    with pytest.raises(PressUnavailable) as err:
        make_press("air", TUNING, review=False)
    assert str(err.value) == "The air-tap method only works with the review box (commit: review)."


def test_p42_each_method_says_whether_it_needs_review() -> None:
    assert PinchPress.requires_review is False
    assert AirTapPress.requires_review is True
    assert isinstance(make_press("pinch", TUNING), PinchPress)
    assert isinstance(make_press("pinch", TUNING, review=True), PinchPress)
    assert isinstance(make_press("air", TUNING, review=True), AirTapPress)
    assert make_press("pinch", TUNING).name == "pinch"
    assert make_press("air", TUNING, review=True).name == "air"


def test_p42_the_system_keyboard_has_no_session_and_so_no_press() -> None:
    for review in (False, True):
        with pytest.raises(PressUnavailable):
            make_press("windows", TUNING, review=review)


def test_p42_an_unavailable_method_is_a_value_error_with_a_fixed_text() -> None:
    assert issubclass(PressUnavailable, ValueError)
    with pytest.raises(PressUnavailable) as err:
        make_press("nothing", TUNING)  # type: ignore[arg-type]
    assert "nothing" not in str(err.value)


@pytest.mark.parametrize("name", ["pinch", "air"])
def test_both_methods_offer_the_whole_protocol(name: str) -> None:
    press = make_press(name, TUNING, review=True)  # type: ignore[arg-type]
    assert press.update([]) == []
    assert press.fingers([]) == []
    assert isinstance(press.quality(), PressQuality)
    assert isinstance(press.rejects, dict)
    press.reset()
    press.set_level("degraded")
    press.set_level("ok")
    press.set_calibrating(True)
    press.set_calibrating(False)


# --------------------------------------------------------------------------------------------------- a pinch end to end


def test_a_pinch_types_once_with_the_data_of_the_hand_and_the_onset() -> None:
    press = PinchPress(TUNING)
    t = settle(press)
    aims = tuple((0.5 + 0.04 * f, 0.4) for f in range(4))
    events, _ = stream(press, pinch_rows(1), t, aims=aims)
    assert len(events) == 1
    event = events[0]
    assert (event.hand, event.side, event.finger) == (1, "right", 1)
    assert event.aim == pytest.approx(aims[1])
    assert event.ratio < 0.28
    assert event.margin == pytest.approx(OPEN - event.ratio)
    assert event.onset_t < event.t
    assert (event.depth, event.conf) == (0.0, 0.0)
    assert press.rejects == {}


def test_the_aim_is_the_mean_of_three_frames_ending_at_the_onset_and_not_the_one_at_the_commit() -> None:
    press = PinchPress(TUNING)
    t = settle(press)
    rows = []
    for k in range(10):
        # the fingertip walks away from where it was while the pinch closes
        ratio = OPEN if k < 3 else max(0.1, OPEN - 0.3 * (k - 2))
        rows.append(
            {"ratios": with_finger(0, ratio), "aims": tuple((0.5 + 0.01 * k, 0.4 + 0.02 * k) for _ in range(4))}
        )
    events, _ = stream(press, rows + pinch_rows(0)[-3:], t)
    assert len(events) == 1
    # the onset is the last open frame (k = 2); the mean of k = 0, 1, 2
    assert events[0].aim == pytest.approx((0.51, 0.42))
    assert events[0].onset_t == pytest.approx(t + 2 * DT)


def test_a_pinch_is_one_press_until_it_reopens() -> None:
    press = PinchPress(TUNING)
    t = settle(press)
    events, t = stream(press, pinch_rows(2, held=50, opening=0), t)
    assert len(events) == 1
    # it wanders between the two thresholds and squeezes again, but never opens: still one pinch
    wander = [0.35, 0.2, 0.1, 0.3, 0.1, 0.38, 0.15, 0.1] * 3
    events, t = stream(press, [{"ratios": with_finger(2, r)} for r in wander], t)
    assert events == []


def test_a_finger_that_reopened_can_press_again() -> None:
    press = PinchPress(TUNING)
    t = settle(press)
    events, t = stream(press, pinch_rows(3) + [{}] * 3 + pinch_rows(3) + [{}] * 3, t)
    assert [e.finger for e in events] == [3, 3]


def test_the_commit_needs_two_consecutive_frames_below_close() -> None:
    press = PinchPress(TUNING)
    t = settle(press)
    approach = [{"ratios": with_finger(0, r)} for r in (0.9, 0.7, 0.5, 0.3)]
    events, t = stream(press, [*approach, {"ratios": with_finger(0, 0.2)}], t)
    assert events == []
    events, t = stream(press, [{"ratios": with_finger(0, 0.2)}], t)
    assert len(events) == 1


def test_a_single_dip_below_close_between_two_high_frames_does_not_press() -> None:
    press = PinchPress(TUNING)
    t = settle(press)
    rows = [{"ratios": with_finger(0, r)} for r in (0.9, 0.6, 0.2, 0.9, 0.9)]
    events, _ = stream(press, rows, t)
    assert events == []


@pytest.mark.parametrize("closing", [3, 5, 8])
def test_the_commit_follows_the_last_closing_frame_by_the_confirm_frames(closing: int) -> None:
    press = PinchPress(TUNING)
    t = settle(press)
    rows = pinch_rows(0, closing=closing, held=4)
    events, _ = stream(press, rows, t)
    assert len(events) == 1
    # two frames below close commit: the commit is the second of them
    below = next(i for i, row in enumerate(rows) if row["ratios"][0] < TUNING.close)
    assert events[0].t == pytest.approx(t + (below + 1) * DT)


# --------------------------------------------------------------------------------------------------- U8: latch rules


def test_u8_a_hand_that_appears_already_pinching_types_nothing_until_it_opens_and_pinches_again() -> None:
    press = PinchPress(TUNING)
    events, t = stream(press, [{"ratios": with_finger(0, 0.1)}] * 20)
    assert events == []
    events, t = stream(press, pinch_rows(0)[-3:] + [{}] * 3, t)
    assert events == []
    events, t = stream(press, pinch_rows(0), t)
    assert len(events) == 1


def test_u8_a_new_hand_id_starts_latched_even_in_the_same_place() -> None:
    press = PinchPress(TUNING)
    t = settle(press)
    events, t = stream(press, [{"ratios": with_finger(0, 0.1)}] * 6, t, hand=2)
    assert events == []


def test_u8_a_gap_over_a_quarter_of_a_second_latches_every_finger() -> None:
    press = PinchPress(TUNING)
    t = settle(press)
    # a pinch in progress, then the stream stops for 0.3 s and the same hand comes back mid-pinch
    _, t = stream(press, pinch_rows(0)[:4], t)
    events, t = stream(press, [{"ratios": with_finger(0, 0.1)}] * 8, t + 0.3)
    assert events == []
    events, t = stream(press, pinch_rows(0)[-3:] + [{}] * 3 + pinch_rows(0), t)
    assert len(events) == 1


def test_u8_a_gap_under_the_limit_does_not() -> None:
    press = PinchPress(TUNING)
    t = settle(press)
    rows = pinch_rows(0, closing=6)
    _, t = stream(press, rows[:3], t)
    events, _ = stream(press, rows[3:], t + 0.2)  # a 0.2 s hole: the pinch carries on
    assert len(events) == 1


def test_u8_reset_latches_every_finger_and_keeps_the_thresholds() -> None:
    press = PinchPress(TUNING)
    t = settle(press)
    press.set_finger("right", 0, 0.33, 0.45)
    _, t = stream(press, pinch_rows(0)[:5], t)
    press.reset()
    events, t = stream(press, [{"ratios": with_finger(0, 0.1)}] * 6, t)
    assert events == []
    # the finger reopens at its own 0.45 and not at the default 0.40
    _, t = stream(press, [{"ratios": with_finger(0, 0.42)}] * 4, t)
    events, t = stream(press, [{"ratios": with_finger(0, r)} for r in (0.3, 0.1, 0.1, 0.1)], t)
    assert events == []
    _, t = stream(press, [{"ratios": with_finger(0, 0.46)}] * 3, t)
    events, t = stream(press, pinch_rows(0)[1:], t)
    assert len(events) == 1


def test_u8_arming_resets_so_the_last_warm_up_pinch_cannot_type() -> None:
    press = PinchPress(TUNING)
    t = settle(press)
    events, t = stream(press, pinch_rows(1)[:7], t)  # the warm-up pinch is pressed and still held
    assert len(events) == 1
    press.reset()
    events, t = stream(press, pinch_rows(1)[7:] + [{}] * 3, t)
    assert events == []


def test_u8_a_hand_that_left_for_good_takes_its_state_with_it() -> None:
    press = PinchPress(TUNING)
    t = settle(press)
    stream(press, pinch_rows(0)[:4], t)
    later = t + 5.0
    events, later = stream(press, [{"ratios": with_finger(0, 0.1)}] * 6, later, hand=2)
    assert events == []
    assert len(press._hands) == 1  # type: ignore[attr-defined]


# --------------------------------------------------------------------------------------------------- U9: the counters


def episode(press: PinchPress, rows: list[dict], t: float, **kw) -> float:
    _, t = stream(press, rows, t, **kw)
    return t


def closing_to(finger: int, floor: float, steps: int = 4) -> list[dict]:
    return [{"ratios": with_finger(finger, OPEN + (floor - OPEN) * (i + 1) / steps)} for i in range(steps)]


def test_u9_ambiguous_two_fingers_close_together() -> None:
    press = PinchPress(TUNING)
    t = settle(press)
    both = tuple(0.1 if f in (0, 1) else OPEN for f in range(4))
    ramp = [{"ratios": tuple(OPEN + (v - OPEN) * (i + 1) / 4 for v in both)} for i in range(4)]
    events, t = stream(press, ramp + [{"ratios": both}] * 3 + [{}] * 3, t)
    assert events == []
    assert press.rejects.get("ambiguous", 0) >= 1


def test_u9_curled_a_curled_finger_never_presses() -> None:
    press = PinchPress(TUNING)
    t = settle(press)
    events, t = stream(
        press, closing_to(0, 0.1) + [{"ratios": with_finger(0, 0.1), "curled": (True, False, False, False)}] * 3, t
    )
    assert events == []
    t = episode(press, [{}] * 3, t)
    assert press.rejects.get("curled", 0) >= 1 or press.rejects.get("aborted", 0) >= 1
    assert press.rejects.get("curled", 0) == 1


def test_u9_fist_like_three_curled_fingers_never_press() -> None:
    press = PinchPress(TUNING)
    t = settle(press)
    fist = (False, True, True, True)
    events, t = stream(press, closing_to(0, 0.1) + [{"ratios": with_finger(0, 0.1), "curled": fist}] * 3 + [{}] * 3, t)
    assert events == []
    assert press.rejects.get("fist_like", 0) == 1


def test_u9_other_finger_down_blocks_a_second_finger_while_the_first_is_held() -> None:
    press = PinchPress(TUNING)
    t = settle(press)
    events, t = stream(press, closing_to(0, 0.12) + [{"ratios": with_finger(0, 0.12)}] * 2, t)
    assert [e.finger for e in events] == [0]
    # the index slackens to 0.30 (still below its open threshold, so still pressed) while the middle finger closes
    ramp = [{"ratios": (0.30, OPEN + (0.1 - OPEN) * (i + 1) / 4, OPEN, OPEN)} for i in range(4)]
    events, t = stream(
        press, ramp + [{"ratios": (0.30, 0.1, OPEN, OPEN)}] * 3 + [{"ratios": (0.30, OPEN, OPEN, OPEN)}] * 2, t
    )
    assert events == []
    assert press.rejects == {"other_finger_down": 1}


def test_b4_legato_a_finger_already_back_above_its_open_threshold_does_not_block_the_next() -> None:
    press = PinchPress(TUNING)
    t = settle(press)
    events, t = stream(press, closing_to(0, 0.1) + [{"ratios": with_finger(0, 0.1)}] * 2, t)
    assert [e.finger for e in events] == [0]
    # the thumb slides to the middle finger: the index comes back above 0.40 (but not yet for two frames)
    slide = [
        {"ratios": (0.45, 0.6, OPEN, OPEN)},
        {"ratios": (0.5, 0.3, OPEN, OPEN)},
        {"ratios": (0.55, 0.1, OPEN, OPEN)},
    ]
    events, t = stream(press, slide + [{"ratios": (0.6, 0.1, OPEN, OPEN)}] * 3, t)
    assert [e.finger for e in events] == [1]


def test_u9_others_moving_a_hand_opening_or_closing_is_not_a_pinch() -> None:
    """The other three fingers' reach falls by half a unit while the index pinches: a hand closing, not a pinch."""
    press = PinchPress(TUNING)
    t = settle(press)
    rows = []
    for i in range(9):
        reach = 1.8 - 0.15 * (i + 1)
        ratio = OPEN + (0.12 - OPEN) * min(1.0, (i + 1) / 5)
        rows.append({"ratios": with_finger(0, ratio), "reach": (1.8, reach, reach, reach)})
    rows += [{"ratios": with_finger(0, 0.9)}] * 2  # the pinch lets go
    events, t = stream(press, rows, t)
    assert events == []
    assert press.rejects == {"others_moving": 1}


def test_u9_hand_moving_a_wave_or_a_reach_is_not_a_pinch() -> None:
    press = PinchPress(TUNING)
    t = settle(press)
    events, t = stream(press, closing_to(0, 0.1) + [{"ratios": with_finger(0, 0.1), "speed": 2.0}] * 4 + [{}] * 3, t)
    assert events == []
    assert press.rejects.get("hand_moving", 0) == 1


def test_u9_hand_moving_threshold_is_one_and_a_half_frame_widths_a_second() -> None:
    for speed, expected in ((1.4, 1), (1.6, 0)):
        press = PinchPress(TUNING)
        t = settle(press)
        events, _ = stream(press, pinch_rows(0, held=4), t, speed=speed)
        assert len(events) == expected


def test_u9_closing_timeout_a_pinch_that_never_completes() -> None:
    press = PinchPress(TUNING)
    t = settle(press)
    rows = closing_to(0, 0.5, steps=3) + [{"ratios": with_finger(0, 0.45)}] * 30
    events, _ = stream(press, rows, t)
    assert events == []
    assert press.rejects == {"closing_timeout": 1}


def test_u9_aborted_a_pinch_that_falls_back_open() -> None:
    press = PinchPress(TUNING)
    t = settle(press)
    rows = closing_to(0, 0.5, steps=3) + [{}] * 3
    events, _ = stream(press, rows, t)
    assert events == []
    assert press.rejects == {"aborted": 1}


def test_u9_an_episode_counts_once_whatever_its_length() -> None:
    press = PinchPress(TUNING)
    t = settle(press)
    fist = (False, True, True, True)
    rows = closing_to(0, 0.1) + [{"ratios": with_finger(0, 0.1), "curled": fist}] * 12 + [{}] * 4
    stream(press, rows, t)
    assert press.rejects == {"fist_like": 1}


def test_u9_a_pinch_that_presses_counts_nothing() -> None:
    press = PinchPress(TUNING)
    t = settle(press)
    stream(press, pinch_rows(0), t)
    assert press.rejects == {}


def test_u9_the_counters_survive_a_reset() -> None:
    press = PinchPress(TUNING)
    t = settle(press)
    stream(press, closing_to(0, 0.5, steps=3) + [{}] * 3, t)
    press.reset()
    assert press.rejects == {"aborted": 1}


# --------------------------------------------------------------------------------------------------- U10: the onset


def state_after(settle_ratio: float, ratios: list[float], tuning: Tuning = TUNING) -> str:
    """The index finger's state after it sat at ``settle_ratio`` for a while and then followed ``ratios``."""
    press = PinchPress(tuning)
    t = 0.0
    hands: list[HandSample] = []
    for r in [settle_ratio] * 8 + ratios:
        hands = [sample(t, ratios=with_finger(0, r))]
        press.update(hands)
        t += DT
    return press.fingers(hands)[0].state


def test_u10_the_ratio_must_have_fallen_by_the_descent_to_count_as_an_onset() -> None:
    assert state_after(0.80, [0.69]) == "closing"
    assert state_after(0.80, [0.70]) == "closing"  # exactly the descent counts
    assert state_after(0.80, [0.71]) == "open"


def test_u10_a_closing_starts_only_below_the_descent_ceiling() -> None:
    assert state_after(1.00, [0.79]) == "closing"
    assert state_after(1.00, [0.80]) == "open"
    assert state_after(1.00, [0.81]) == "open"


def test_u10_a_slow_drift_is_not_an_onset_however_far_it_goes_but_a_quick_fall_is() -> None:
    slow = [0.9 - 0.004 * i for i in range(1, 46)]  # 0.18 in 1.5 s: never 0.10 inside 0.35 s
    assert state_after(0.9, slow) == "open"
    quick = [0.9 - 0.02 * i for i in range(1, 10)]  # 0.18 in 0.3 s
    assert state_after(0.9, quick) == "closing"


def test_u10_the_onset_is_the_latest_frame_that_is_far_enough_back_and_inside_the_window() -> None:
    press = PinchPress(TUNING)
    t = 0.0
    # 0.9 for a while, then a staircase down; the aim marks which frame became the onset
    hands: list[HandSample] = []
    ratios = [0.9] * 8 + [0.85, 0.8, 0.75, 0.7]
    for k, r in enumerate(ratios):
        hands = [sample(t, ratios=with_finger(0, r), aims=tuple((0.5 + 0.01 * k, 0.4) for _ in range(4)))]
        press.update(hands)
        t += DT
    view = press.fingers(hands)[0]
    assert view.state == "closing"
    # at the frame with 0.75 the latest frame at or above 0.85 is k = 8; the onset was fixed there, with 3 frames' mean
    assert view.aim[0] == pytest.approx(0.5 + 0.01 * 7)


def test_u10_the_window_and_the_descent_are_the_tunings() -> None:
    assert state_after(0.74, [0.67]) == "open"
    assert state_after(0.74, [0.67], Tuning(descent=0.06)) == "closing"
    slow = [0.9 - 0.0065 * i for i in range(1, 30)]  # 0.117 in 0.6 s: inside a window of 0.6 s, not of 0.35 s
    assert state_after(0.9, slow) == "open"
    assert state_after(0.9, slow, Tuning(onset_window_s=0.6)) == "closing"
    assert state_after(1.0, [0.85]) == "open"
    assert state_after(1.0, [0.85], Tuning(descent_max_r=0.9)) == "closing"


def test_u10_the_view_freezes_the_aim_while_closing_and_follows_the_finger_when_open() -> None:
    press = PinchPress(TUNING)
    t = settle(press)
    _, t = stream(press, [{"aims": tuple((0.5, 0.4) for _ in range(4))}] * 3, t)
    rows = [
        {"ratios": with_finger(0, OPEN - 0.15 * (i + 1)), "aims": tuple((0.5 + 0.01 * (i + 1), 0.4) for _ in range(4))}
        for i in range(3)
    ]
    hands = []
    for row in rows:
        hands = [sample(t, **row)]
        press.update(hands)
        t += DT
    view = {v.finger: v for v in press.fingers(hands)}
    # the onset is the first row (ratio 0.85, the last frame above 0.80 before the fall): its aim and the two before it
    assert view[0].state == "closing"
    assert view[0].aim == pytest.approx((0.5 + 0.01 / 3, 0.4))
    assert view[1].state == "open"
    assert view[1].aim == pytest.approx((0.5 + 0.03, 0.4))
    assert (view[0].fill, view[0].note) == (0.0, "")


def test_the_views_name_every_finger_of_every_hand_and_start_latched() -> None:
    press = PinchPress(TUNING)
    hands = [sample(0.0, hand=1, side="left"), sample(0.0, hand=2, side="right")]
    views = press.fingers(hands)
    assert [(v.hand, v.side, v.finger, v.state) for v in views] == [
        (h, s, f, "latched") for h, s in ((1, "left"), (2, "right")) for f in range(4)
    ]
    press.update(hands)
    assert all(v.state == "latched" for v in press.fingers(hands))


# --- thresholds and the rest


def test_set_finger_gives_a_finger_its_own_thresholds() -> None:
    """A finger whose minimum ratio is 0.33 never reaches 0.28 but types after the warm-up gave it its own close."""
    floor = 0.33
    default = PinchPress(TUNING)
    t = settle(default)
    events, _ = stream(default, pinch_rows(2, floor=floor), t)
    assert events == []
    tuned = PinchPress(TUNING)
    tuned.set_finger("right", 2, 0.36, 0.48)
    t = settle(tuned)
    events, _ = stream(tuned, pinch_rows(2, floor=floor), t)
    assert len(events) == 1


def test_set_finger_is_per_side_and_finger() -> None:
    press = PinchPress(TUNING)
    press.set_finger("left", 2, 0.36, 0.48)
    t = settle(press)
    events, _ = stream(press, pinch_rows(2, floor=0.33), t)  # the right ring finger has no record
    assert events == []


def test_set_finger_needs_both_thresholds_and_holds_them_inside_the_clamps() -> None:
    press = PinchPress(TUNING)
    with pytest.raises(ValueError, match="both thresholds"):
        press.set_finger("right", 0, 0.30)
    with pytest.raises(ValueError, match="both thresholds"):
        press.set_finger("right", 0, 0.30, None)
    for bad in (float("nan"), float("inf")):
        with pytest.raises(ValueError, match="finite"):
            press.set_finger("right", 0, bad, 0.4)
    press.set_finger("right", 1, 0.90, 0.95)
    close, open_ = press._thresholds[("right", 1)]  # type: ignore[attr-defined]
    assert close == 0.36
    assert 0.34 <= open_ <= 0.50
    assert open_ >= close + 0.06 - 1e-9
    press.set_finger("right", 1, 0.0, 0.0)
    close, open_ = press._thresholds[("right", 1)]  # type: ignore[attr-defined]
    assert close == 0.22
    assert open_ >= close + 0.06 - 1e-9


def test_quality_and_the_ladder_calls_are_inert_for_a_pinch() -> None:
    press = PinchPress(TUNING)
    assert press.quality() == PressQuality(0.0, None)
    assert press.quality().gaps == 0
    press.set_level("degraded")
    press.set_calibrating(True)
    t = settle(press)
    events, _ = stream(press, pinch_rows(0), t)
    assert len(events) == 1


def test_two_hands_press_independently() -> None:
    press = PinchPress(TUNING)
    t = 0.0
    for _ in range(4):
        press.update([sample(t, hand=1, side="left"), sample(t, hand=2, side="right")])
        t += DT
    events: list[PressEvent] = []
    for left, right in zip(pinch_rows(0), pinch_rows(1), strict=True):
        events += press.update([sample(t, hand=1, side="left", **left), sample(t, hand=2, side="right", **right)])
        t += DT
    assert sorted((e.side, e.finger) for e in events) == [("left", 0), ("right", 1)]


def test_a_press_in_one_hand_does_not_block_the_other_hands_finger() -> None:
    press = PinchPress(TUNING)
    t = 0.0
    for _ in range(4):
        press.update([sample(t, hand=1, side="left"), sample(t, hand=2, side="right")])
        t += DT
    events: list[PressEvent] = []
    for row in pinch_rows(0, held=6):
        events += press.update([sample(t, hand=1, side="left", **row), sample(t, hand=2, side="right", **row)])
        t += DT
    assert len(events) == 2


def test_the_event_is_plain_data() -> None:
    press = PinchPress(TUNING)
    t = settle(press)
    events, _ = stream(press, pinch_rows(0), t)
    event = events[0]
    assert type(event.aim) is tuple
    assert all(type(x) is float for x in event.aim)
    assert type(event.ratio) is float
    assert type(event.margin) is float


# ----------------------------------------------------------------------- what the pinch adds to the rules of 2.6
#
# Four rules the real tracker's noise made necessary and the study's noise-free streams did not: a finger does not
# press twice within ``_REFRACTORY_S``, a hand that moved on while a finger closed is not read, a pinch that never
# gets below the bar a closing starts under is dropped at once, and the aim does not average in frames from before
# the hand got where it is. The scenarios of ``test_kb_scenarios`` show what each is worth on synthetic hands.


def test_a_second_commit_within_a_fifth_of_a_second_of_the_first_is_refused_as_refractory() -> None:
    press = PinchPress(TUNING)
    t = settle(press)
    quick = [{"ratios": with_finger(0, r)} for r in (0.5, 0.2, 0.15, 0.9, 0.9, 0.5, 0.2, 0.15, 0.9, 0.9, 0.9)]
    events, _ = stream(press, quick, t)
    assert len(events) == 1
    assert press.rejects == {"refractory": 1}


def test_a_second_pinch_of_the_same_finger_a_third_of_a_second_later_types() -> None:
    press = PinchPress(TUNING)
    t = settle(press)
    slow = (0.5, 0.2, 0.15, *(0.9,) * 6, 0.5, 0.2, 0.15, 0.9, 0.9)
    events, _ = stream(press, [{"ratios": with_finger(0, r)} for r in slow], t)
    assert len(events) == 2
    assert events[1].t - events[0].t == pytest.approx(0.3)
    assert press.rejects == {}


def test_a_hand_that_moves_on_while_the_finger_closes_ends_the_episode_as_hand_moving() -> None:
    """A step of 0.4 palms a frame is more than the 0.35 allowed between the onset and now, and every frame after it
    is a step away from the last, so no new onset is found either."""
    press = PinchPress(TUNING)
    t = settle(press)
    palm = 0.1
    rows = [{"ratios": with_finger(0, 0.5)}]
    rows += [
        {"ratios": with_finger(0, r), "anchor": (0.5 + 0.4 * palm * step, 0.4)}
        for step, r in enumerate((0.4, 0.2, 0.15, 0.12), start=1)
    ]
    events, _ = stream(press, rows, t)
    assert events == []
    assert press.rejects == {"hand_moving": 1}


@pytest.mark.parametrize(("shift", "typed"), [(0.0, 1), (0.5, 0)])
def test_a_fall_that_began_before_the_hand_got_here_is_no_onset(shift: float, typed: int) -> None:
    """The ratio fell from 1.0 to 0.15 at once. If the hand is where it was, that is a pinch; if it has come half a
    palm since the frame that was at 1.0, the fingertip was somewhere else then and no onset is found."""
    press = PinchPress(TUNING)
    t = settle(press)
    rows = [{"ratios": with_finger(0, r), "anchor": (0.5 + shift * 0.1, 0.4)} for r in (0.15, 0.12, 0.12, 0.12)]
    events, _ = stream(press, rows, t)
    assert len(events) == typed
    assert press.rejects == {}


def test_the_aim_leaves_out_frames_from_a_different_stance_of_the_hand() -> None:
    """The hand shifted a fifth of a palm just before the onset: the aim is where it is now, not a mean with before."""
    press = PinchPress(TUNING)
    t = settle(press)
    before = [{"aims": tuple((0.50, 0.40) for _ in range(4))}] * 2
    after = {"aims": tuple((0.52, 0.40) for _ in range(4)), "anchor": (0.52, 0.4)}
    rows = [*before, after, {**after, "ratios": with_finger(0, 0.5)}, {**after, "ratios": with_finger(0, 0.2)}]
    rows += [{**after, "ratios": with_finger(0, 0.15)}]
    events, _ = stream(press, rows, t)
    assert len(events) == 1
    assert events[0].aim == pytest.approx((0.52, 0.40))


def test_a_closing_that_stays_above_the_bar_it_starts_under_is_dropped_and_does_not_block_a_real_pinch() -> None:
    press = PinchPress(TUNING)
    t = settle(press)
    noise = [{"ratios": with_finger(0, r)} for r in (0.7, 0.85, 0.85)]
    real = [{"ratios": with_finger(0, r)} for r in (0.9, 0.5, 0.2, 0.15, 0.12)]
    events, _ = stream(press, [*noise, *real], t)
    assert len(events) == 1
    assert press.rejects == {"aborted": 1}


def test_one_frame_back_above_the_bar_does_not_drop_a_closing() -> None:
    press = PinchPress(TUNING)
    t = settle(press)
    rows = [{"ratios": with_finger(0, r)} for r in (0.7, 0.85, 0.5, 0.2, 0.15)]
    events, _ = stream(press, rows, t)
    assert len(events) == 1
    assert press.rejects == {}
