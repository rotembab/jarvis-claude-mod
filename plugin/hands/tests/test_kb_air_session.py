"""The air tap at the session's edges: the prompted warm-up (X26) and the degradation ladder (X31 to X33).

Pure classes first: ``Warmup(method="air")`` and ``AirLadder`` are driven with hand-built samples and events, so every
bound of 2.12.5 and 2.12.7 is tested at its edge. Times are written as ``k / 10`` so a bound such as 2.0 s is reached
exactly, never at 1.9999999999999998.
"""

from __future__ import annotations

import dataclasses

import pytest

from jarvis_hands.keyboard import limits
from jarvis_hands.keyboard.ladder import AirLadder
from jarvis_hands.keyboard.plane import Plane
from jarvis_hands.keyboard.rig import KbRig
from jarvis_hands.keyboard.session import SESSION_TEXT
from jarvis_hands.keyboard.tuning import Tuning
from jarvis_hands.keyboard.types import PressEvent, PressQuality, Side
from jarvis_hands.keyboard.warmup import Warmup
from jarvis_hands.landmarks import Frame

PLANE = Plane(0.5, 0.5, 0.05, 0.0625, 5)
#: What the strip says of a practice the ladder cut (Appendix F.1). No report is shown to the user, so it names the
#: way out.
NOT_USABLE = "Air tap is not usable on this camera. Use the pinch method."
TUNING = Tuning()


def homes(sides: tuple[Side, ...]) -> dict[tuple[Side, int], tuple[float, float]]:
    """Each finger's own home key: a s d f on the left, j k l ' on the right, home row (v 3.5 on the review plane)."""
    base = {"left": 2.0, "right": 8.0}
    return {(side, f): (base[side] + (f if side == "right" else -f) + 0.5, 3.5) for side in sides for f in range(4)}


BOTH = homes(("left", "right"))
ONE = homes(("left",))


def tap(
    key: tuple[Side, int],
    *,
    home: dict[tuple[Side, int], tuple[float, float]] = BOTH,
    du: float = 0.0,
    dv: float = 0.0,
    margin: float = 1.0,
    depth: float = 0.30,
    t: float = 0.0,
) -> PressEvent:
    u, v = home[key]
    return PressEvent(t, t - 0.1, 1, key[0], key[1], PLANE.pose(u + du, v + dv), 0.0, margin, depth, 0.8)


def air(home: dict[tuple[Side, int], tuple[float, float]] = BOTH) -> Warmup:
    return Warmup(TUNING, "air", home_f=home)


def step(w: Warmup, t: float, *events: PressEvent, rejects: int = 0) -> tuple[str, ...]:
    return w.update([], list(events), t=t, plane=PLANE, rejects=rejects)


ORDER = [("right", 0), ("left", 0), ("right", 1), ("left", 1), ("right", 2), ("left", 2), ("right", 3), ("left", 3)]


# ------------------------------------------------------------------------------------------------------ X26: the order


def test_x26_the_air_warmup_needs_the_home_position_of_each_finger() -> None:
    with pytest.raises(ValueError):
        Warmup(TUNING, "air")
    with pytest.raises(ValueError):
        Warmup(TUNING, "air", home_f={})
    with pytest.raises(ValueError):
        Warmup(TUNING, "windows")


def test_x26_nothing_is_named_for_a_second_after_entering() -> None:
    w = air()
    assert w.prompt is None and not w.complete and w.strays == 0
    assert step(w, 5.0) == () and w.prompt is None  # the first update is the entry
    assert step(w, 5.9) == () and w.prompt is None
    step(w, 6.0)
    assert w.prompt == ("right", 0)


def test_x26_eight_taps_in_the_fixed_order_complete_it_in_eight_seconds() -> None:
    w = air()
    step(w, 0.0)
    last = 0.0
    for i, key in enumerate(ORDER):
        t = 1.0 + i
        assert step(w, t - 0.1) == () and w.prompt is None  # nothing is named in the second after the last one
        assert step(w, t) == () and w.prompt == key
        assert step(w, t, tap(key, depth=0.2 + i / 100)) == ("accepted",)
        last = t
        assert w.prompt is None  # the wait for the next finger has begun
        assert key in w.done and w.depth[key] == pytest.approx(0.2 + i / 100)
        assert w.complete == (i == 7)
    assert w.prompt is None and len(w.done) == 8 and last == 8.0  # the shortest possible warm-up


def test_x26_one_hand_names_index_middle_ring_pinky_and_needs_four() -> None:
    for side in ("left", "right"):
        home = homes((side,))
        w = air(home)
        step(w, 0.0)
        for i in range(4):
            t = 1.0 + i
            step(w, t)
            assert w.prompt == (side, i)
            assert step(w, t, tap((side, i), home=home)) == ("accepted",)
        assert w.complete and w.prompt is None and w.required == frozenset(home)


def test_x26_events_in_the_gap_are_early_and_count_no_stray() -> None:
    w = air()
    step(w, 0.0)
    assert step(w, 0.5, tap(("right", 0)), tap(("left", 3))) == ("early", "early")  # before the first is named
    assert w.strays == 0
    step(w, 1.0)
    assert step(w, 1.0, tap(("right", 0))) == ("accepted",)
    assert step(w, 1.5, tap(("left", 0)), tap(("right", 3))) == ("early", "early")  # in the wait after an accepted tap
    assert w.strays == 0 and len(w.done) == 1
    step(w, 2.0)
    assert w.prompt == ("left", 0)


def test_x26_a_second_event_in_the_frame_of_an_accepted_tap_is_early() -> None:
    w = air()
    step(w, 0.0)
    step(w, 1.0)
    assert step(w, 1.0, tap(("right", 0)), tap(("left", 0))) == ("accepted", "early")
    assert w.strays == 0


def test_x26_another_finger_is_a_stray_and_the_third_stray_restarts() -> None:
    w = air()
    step(w, 0.0)
    step(w, 1.0)
    assert step(w, 1.0, tap(("right", 0))) == ("accepted",)
    step(w, 2.0)  # left index is named
    assert step(w, 2.1, tap(("right", 3))) == ("stray",) and w.strays == 1
    assert step(w, 2.2, tap(("left", 2))) == ("stray",) and w.strays == 2
    assert step(w, 2.3, tap(("left", 0))) == ("accepted",) and w.strays == 2  # an accepted tap keeps the count
    step(w, 3.3)
    assert w.prompt == ("right", 1)
    assert step(w, 3.4, tap(("left", 1))) == ("restart",)  # the third stray since the start
    assert w.done == frozenset() and w.depth == {} and w.strays == 0 and w.prompt is None
    assert step(w, 4.3, tap(("right", 0))) == ("early",)  # the first is named 1.0 s after the restart
    step(w, 4.4)
    assert w.prompt == ("right", 0)


def test_x26_after_a_restart_the_count_starts_again() -> None:
    w = air()
    step(w, 0.0)
    step(w, 1.0)
    assert step(w, 1.0, tap(("left", 3)), tap(("left", 3)), tap(("left", 3))) == ("stray", "stray", "restart")
    step(w, 2.0)
    assert step(w, 2.0, tap(("left", 3)), tap(("left", 3))) == ("stray", "stray") and w.strays == 2
    assert step(w, 2.0, tap(("left", 3))) == ("restart",)


def test_x26_weak_taps_are_refused_at_the_edges() -> None:
    w = air()
    step(w, 0.0)
    step(w, 1.0)
    assert step(w, 1.0, tap(("right", 0), margin=limits.AIR_WARMUP_MIN_MARGIN - 0.01)) == ("weak",)
    assert step(w, 1.0, tap(("right", 0), depth=limits.AIR_WARMUP_MIN_DEPTH - 0.01)) == ("weak",)
    assert w.strays == 0 and not w.done
    assert step(w, 1.0, tap(("right", 0), margin=limits.AIR_WARMUP_MIN_MARGIN)) == ("accepted",)
    step(w, 2.0)
    assert step(w, 2.0, tap(("left", 0), depth=limits.AIR_WARMUP_MIN_DEPTH)) == ("accepted",)


def test_x26_the_aim_must_be_on_the_named_fingers_own_key() -> None:
    for du, dv, expect in [
        (0.61, 0.0, "off_key"),
        (-0.61, 0.0, "off_key"),
        (0.0, 0.61, "off_key"),
        (0.0, -0.61, "off_key"),
        (0.59, 0.59, "accepted"),
        (-0.59, -0.59, "accepted"),
        (0.4, 0.8, "off_key"),
    ]:
        w = air()
        step(w, 0.0)
        step(w, 1.0)
        assert step(w, 1.0, tap(("right", 0), du=du, dv=dv)) == (expect,), (du, dv)
        assert w.strays == 0


def test_x26_the_aim_is_read_through_the_plane() -> None:
    w = air()
    step(w, 0.0)
    step(w, 1.0)
    u, v = BOTH[("right", 0)]
    other = Plane(0.55, 0.5, 0.05, 0.0625, 5)  # the same pose reads as a key 1 unit away on another plane
    ev = PressEvent(0.0, 0.0, 1, "right", 0, PLANE.pose(u, v), 0.0, 1.0, 0.3, 0.8)
    assert w.update([], [ev], t=1.0, plane=other, rejects=0) == ("off_key",)
    assert w.update([], [ev], t=1.0, plane=None, rejects=0) == ("off_key",)  # no plane: never accepted
    assert w.update([], [ev], t=1.0, plane=PLANE, rejects=0) == ("accepted",)


def test_x26_a_reject_total_that_rose_within_a_second_is_unclean() -> None:
    w = air()
    step(w, 0.0, rejects=7)
    step(w, 0.5, rejects=7)
    step(w, 1.0, rejects=8)  # a hand gate refused something just now
    assert step(w, 1.0, tap(("right", 0)), rejects=8) == ("unclean",)
    assert not w.done and w.strays == 0
    step(w, 1.5, rejects=8)
    assert step(w, 1.9, tap(("right", 0)), rejects=8) == ("unclean",)  # 0.9 s after the rise
    assert step(w, 2.0, tap(("right", 0)), rejects=8) == ("accepted",)  # exactly 1.0 s after it


def test_x26_the_clean_window_looks_back_a_full_second_not_one_frame() -> None:
    w = air()
    for k in range(0, 30):
        step(w, k / 10, rejects=0)
    step(w, 3.0, rejects=0)
    assert step(w, 3.0, tap(("right", 0)), rejects=0) == ("accepted",)
    w = air()
    for k in range(0, 20):
        step(w, k / 10, rejects=0)
    for k in range(20, 24):
        step(w, k / 10, rejects=1 if k >= 22 else 0)  # the rise is two frames old, the total stays flat since
    assert step(w, 2.4, tap(("right", 0)), rejects=1) == ("unclean",)


def test_x26_a_rise_before_the_warmup_began_is_not_held_against_it() -> None:
    w = air()
    step(w, 0.0, rejects=50)  # the placing phase counted these
    step(w, 1.0, rejects=50)
    assert step(w, 1.0, tap(("right", 0)), rejects=50) == ("accepted",)


def test_x26_a_falling_total_is_clean() -> None:
    w = air()
    step(w, 0.0, rejects=9)
    step(w, 1.0, rejects=3)
    assert step(w, 1.0, tap(("right", 0)), rejects=3) == ("accepted",)


def test_x26_the_checks_run_in_the_documented_order() -> None:
    # stray before weak before off-key before unclean
    w = air()
    step(w, 0.0, rejects=0)
    step(w, 1.0, rejects=1)
    bad = {"margin": 0.1, "depth": 0.05, "du": 2.0}
    assert step(w, 1.0, tap(("left", 0), **bad), rejects=1) == ("stray",)
    assert step(w, 1.0, tap(("right", 0), **bad), rejects=1) == ("weak",)
    assert step(w, 1.0, tap(("right", 0), du=2.0), rejects=1) == ("off_key",)
    assert step(w, 1.0, tap(("right", 0)), rejects=1) == ("unclean",)


def test_x26_a_refused_tap_leaves_the_prompt_and_the_wait_alone() -> None:
    w = air()
    step(w, 0.0)
    step(w, 1.0)
    step(w, 1.0, tap(("right", 0), depth=0.01))
    assert w.prompt == ("right", 0)
    step(w, 1.1)
    assert w.prompt == ("right", 0)


def test_x26_the_depth_of_the_accepted_tap_is_the_threshold_and_a_restart_forgets_it() -> None:
    w = air()
    step(w, 0.0)
    step(w, 1.0)
    step(w, 1.0, tap(("right", 0), depth=0.41))
    assert w.thresholds() == {("right", 0): (0.41, 0.41)}
    assert w.depth == {("right", 0): 0.41}
    w.depth.clear()  # a copy: the warm-up's own record is untouched
    assert w.depth == {("right", 0): 0.41}
    step(w, 2.0)
    step(w, 2.0, tap(("left", 3)), tap(("left", 3)), tap(("left", 3)))
    assert w.thresholds() == {} and w.depth == {} and w.done == frozenset()


def test_x26_a_completed_warmup_ignores_what_follows() -> None:
    w = air(ONE)
    step(w, 0.0)
    for i in range(4):
        step(w, 1.0 + i)
        step(w, 1.0 + i, tap(("left", i), home=ONE))
    assert w.complete
    assert step(w, 9.0, tap(("left", 0), home=ONE)) == ("early",)
    assert w.prompt is None and w.strays == 0 and len(w.thresholds()) == 4


def test_x26_the_hands_argument_is_not_what_decides_the_required_fingers_for_air() -> None:
    w = air(ONE)
    assert w.required == frozenset(ONE)
    w.update([object()], [], t=0.0, plane=PLANE, rejects=0)  # type: ignore[list-item]
    assert w.required == frozenset(ONE)


def test_x26_the_events_of_a_frame_are_answered_in_order_one_each() -> None:
    w = air()
    step(w, 0.0)
    step(w, 1.0)
    out = step(w, 1.0, tap(("right", 3)), tap(("right", 0), depth=0.01), tap(("right", 0)), tap(("right", 0)))
    assert out == ("stray", "weak", "accepted", "early")
    assert step(w, 1.0) == ()


# ------------------------------------------------------------------------------------------- X31 to X33: the ladder

GOOD = PressQuality(fps=30.0, noise=0.017, gaps=0)


class Ladder:
    """An ``AirLadder`` fed one reading every tenth of a second."""

    def __init__(self) -> None:
        self.ladder = AirLadder()
        self.k = 0

    @property
    def t(self) -> float:
        return self.k / 10

    def feed(self, q: PressQuality, seconds: float, *, hands: bool = True) -> list[str]:
        """The same reading for ``seconds``: from the next tenth to ``seconds`` later, both ends included, so a
        condition fed for 2.0 s has held for exactly 2.0 s at the last reading. The levels answered."""
        levels = []
        for _ in range(round(seconds * 10) + 1):
            levels.append(self.ladder.update(self.t, q, hands))
            self.k += 1
        return levels


def fps(value: float, *, noise: float = 0.017, gaps: int = 0) -> PressQuality:
    return PressQuality(fps=value, noise=noise, gaps=gaps)


def test_x31_a_fresh_ladder_is_ok_and_a_good_camera_stays_ok() -> None:
    lad = Ladder()
    assert (lad.ladder.level, lad.ladder.reason, lad.ladder.strict) == ("ok", "", False)
    assert set(lad.feed(GOOD, 60)) == {"ok"}
    assert (lad.ladder.reason, lad.ladder.strict) == ("", False)


@pytest.mark.parametrize("rate", [29.0, 26.0])
def test_x31_fps_at_or_above_the_degraded_threshold_is_fine(rate: float) -> None:
    lad = Ladder()
    assert set(lad.feed(fps(rate), 30)) == {"ok"}


def test_x31_below_the_degraded_fps_for_two_seconds_degrades_with_reason_fps_and_no_strict() -> None:
    lad = Ladder()
    levels = lad.feed(fps(24.0), 2.0)
    assert levels[:-1] == ["ok"] * 20 and levels[-1] == "degraded"
    assert lad.ladder.reason == "fps" and lad.ladder.strict is False


def test_x31_below_the_off_fps_for_two_seconds_is_off_and_off_is_terminal() -> None:
    lad = Ladder()
    levels = lad.feed(fps(10.0), 2.0)
    assert levels[:-1] == ["ok"] * 20 and levels[-1] == "off"
    assert lad.ladder.reason == "fps"
    assert set(lad.feed(GOOD, 600)) == {"off"}  # no automatic switch back
    assert lad.ladder.level == "off"


def test_x31_degraded_then_off_when_the_camera_gets_worse() -> None:
    lad = Ladder()
    lad.feed(fps(20.0), 5.0)
    assert lad.ladder.level == "degraded"
    levels = lad.feed(fps(10.0), 2.0)
    assert levels[:-1] == ["degraded"] * 20 and levels[-1] == "off"


def test_x31_thirteen_fps_is_degraded_not_off_and_twelve_is_off() -> None:
    lad = Ladder()
    lad.feed(fps(13.0), 10)
    assert lad.ladder.level == "degraded"
    lad = Ladder()
    lad.feed(fps(12.9), 2.0)
    assert lad.ladder.level == "off"


def test_x32_noise_above_the_degraded_threshold_for_three_seconds_degrades_strict() -> None:
    lad = Ladder()
    levels = lad.feed(fps(30.0, noise=0.031), 3.0)
    assert levels[:-1] == ["ok"] * 30 and levels[-1] == "degraded"
    assert lad.ladder.reason == "noise" and lad.ladder.strict is True


def test_x32_noise_above_the_off_threshold_for_three_seconds_is_off() -> None:
    lad = Ladder()
    levels = lad.feed(fps(30.0, noise=0.054), 3.0)
    assert levels[:-1] == ["ok"] * 30 and levels[-1] == "off"
    assert lad.ladder.reason == "noise"


def test_x32_the_noise_bounds_are_exclusive_and_an_unknown_noise_is_no_noise() -> None:
    lad = Ladder()
    lad.feed(fps(30.0, noise=limits.AIR_LEVEL_NOISE_DEGRADED), 20)
    assert lad.ladder.level == "ok"
    lad.feed(PressQuality(fps=30.0, noise=None), 20)
    assert lad.ladder.level == "ok"
    lad = Ladder()
    lad.feed(fps(30.0, noise=limits.AIR_LEVEL_NOISE_OFF), 20)
    assert lad.ladder.level == "degraded"


def test_x32_fps_and_noise_together_give_reason_both() -> None:
    lad = Ladder()
    lad.feed(fps(20.0, noise=0.03), 3.0)
    assert lad.ladder.level == "degraded" and lad.ladder.reason == "both" and lad.ladder.strict is True


def test_x32_the_reason_follows_what_holds_and_a_fixed_noise_keeps_the_reason_it_had_until_ok() -> None:
    lad = Ladder()
    lad.feed(fps(20.0), 2.0)
    assert lad.ladder.reason == "fps"
    lad.feed(fps(20.0, noise=0.03), 3.0)
    assert lad.ladder.reason == "both" and lad.ladder.strict
    lad.feed(fps(20.0), 1.0)  # noise settles; fps still holds
    assert lad.ladder.reason == "fps" and not lad.ladder.strict


def test_x33_a_one_second_dip_changes_nothing() -> None:
    lad = Ladder()
    lad.feed(GOOD, 5)
    assert set(lad.feed(fps(10.0), 1.0)) == {"ok"}
    assert set(lad.feed(fps(30.0, noise=0.06), 1.0)) == {"ok"}
    lad.feed(GOOD, 5)
    # and it does not add up with the next dip: the timers restart
    assert set(lad.feed(fps(10.0), 1.9)) == {"ok"}
    assert set(lad.feed(GOOD, 0.1)) == {"ok"}
    assert set(lad.feed(fps(10.0), 1.9)) == {"ok"}


def test_x33_degraded_returns_to_ok_only_after_eight_seconds_of_good_readings() -> None:
    lad = Ladder()
    lad.feed(fps(20.0), 2.0)
    assert lad.ladder.level == "degraded"
    levels = lad.feed(GOOD, limits.AIR_LEVEL_RECOVER_S)
    assert levels[:-1] == ["degraded"] * 80 and levels[-1] == "ok"
    assert (lad.ladder.reason, lad.ladder.strict) == ("", False)


def test_x33_a_bad_reading_in_the_recovery_starts_the_eight_seconds_again() -> None:
    lad = Ladder()
    lad.feed(fps(20.0), 2.0)
    lad.feed(GOOD, 7.0)
    assert lad.ladder.level == "degraded"
    lad.feed(fps(27.0), 0.0)  # above the degraded threshold, below 1.1 x it: not good
    assert set(lad.feed(GOOD, 7.9)) == {"degraded"}
    assert lad.feed(GOOD, 0.0) == ["ok"]


@pytest.mark.parametrize(
    ("q", "good"),
    [
        (fps(28.6), True),
        (fps(28.5), False),
        (fps(30.0, noise=0.0197), True),
        (fps(30.0, noise=0.0199), False),
        (fps(30.0, gaps=1), True),
        (fps(30.0, gaps=2), False),
        (PressQuality(fps=0.0, noise=0.017), True),  # unknown is not bad
        (PressQuality(fps=30.0, noise=None), True),
    ],
)
def test_x33_what_counts_as_good_for_the_return(q: PressQuality, good: bool) -> None:
    lad = Ladder()
    lad.feed(fps(20.0), 2.0)
    assert lad.ladder.level == "degraded"
    lad.feed(q, limits.AIR_LEVEL_RECOVER_S + 0.1)
    assert (lad.ladder.level == "ok") is good


def test_x33_holes_three_in_five_seconds_for_two_seconds_degrade_with_reason_gaps_and_no_strict() -> None:
    lad = Ladder()
    levels = lad.feed(fps(30.0, gaps=3), 1.9)
    assert set(levels) == {"ok"}
    levels = lad.feed(fps(30.0, gaps=3), 0.0)
    assert levels == ["degraded"]
    assert lad.ladder.reason == "gaps" and lad.ladder.strict is False


def test_x33_two_holes_never_degrade_and_fifty_never_switch_off() -> None:
    lad = Ladder()
    assert set(lad.feed(fps(30.0, gaps=2), 120)) == {"ok"}
    lad = Ladder()
    lad.feed(fps(30.0, gaps=50), 120)
    assert lad.ladder.level == "degraded" and lad.ladder.reason == "gaps"


def test_x33_with_a_slow_camera_as_well_the_reason_stays_fps() -> None:
    lad = Ladder()
    lad.feed(fps(20.0, gaps=5), 3.0)
    assert lad.ladder.level == "degraded" and lad.ladder.reason == "fps"


def test_x33_the_return_from_holes_needs_eight_seconds_with_at_most_one() -> None:
    lad = Ladder()
    lad.feed(fps(30.0, gaps=4), 2.0)
    assert lad.ladder.level == "degraded"
    lad.feed(fps(30.0, gaps=1), 7.9)
    assert lad.ladder.level == "degraded"
    lad.feed(fps(30.0, gaps=1), 0.0)
    assert lad.ladder.level == "ok"


def test_x33_no_hand_changes_nothing_and_no_timer_runs() -> None:
    lad = Ladder()
    lad.feed(fps(10.0), 1.5)
    assert set(lad.feed(fps(10.0), 30, hands=False)) == {"ok"}  # a long absence
    assert set(lad.feed(fps(10.0), 0.3)) == {"ok"}  # 1.9 s of bad readings in all
    assert lad.feed(fps(10.0), 0.0) == ["off"]  # and the 2.0 s are complete


def test_x33_an_absence_does_not_advance_the_recovery_either() -> None:
    lad = Ladder()
    lad.feed(fps(20.0), 2.0)
    lad.feed(GOOD, 5.0)
    lad.feed(GOOD, 60, hands=False)
    assert lad.ladder.level == "degraded"
    assert set(lad.feed(GOOD, 2.8)) == {"degraded"}
    assert lad.feed(GOOD, 0.0) == ["ok"]


def test_x33_off_stays_off_whatever_the_readings_and_the_hands() -> None:
    lad = Ladder()
    lad.feed(fps(5.0), 2.0)
    assert lad.ladder.level == "off"
    assert set(lad.feed(GOOD, 100, hands=False)) == {"off"}
    assert set(lad.feed(GOOD, 100)) == {"off"}


def test_x33_a_reading_of_zero_fps_is_unknown_and_not_slow() -> None:
    lad = Ladder()
    assert set(lad.feed(PressQuality(fps=0.0, noise=None), 30)) == {"ok"}


def test_x33_the_update_returns_the_level_it_holds() -> None:
    lad = AirLadder()
    assert lad.update(0.0, GOOD, True) == "ok"
    assert lad.update(0.1, GOOD, False) == lad.level


def test_x26_a_rise_in_the_first_second_after_entering_counts_too() -> None:
    w = air()
    step(w, 0.0, rejects=0)
    step(w, 0.5, rejects=1)  # less than a second of history, and the total already rose
    step(w, 1.0, rejects=1)
    assert step(w, 1.0, tap(("right", 0)), rejects=1) == ("unclean",)
    assert step(w, 1.4, tap(("right", 0)), rejects=1) == ("unclean",)
    assert step(w, 1.5, tap(("right", 0)), rejects=1) == ("accepted",)  # 1.0 s after the sample that saw the rise


def test_x33_off_does_not_slip_back_to_degraded_when_the_readings_improve() -> None:
    lad = Ladder()
    lad.feed(fps(5.0), 2.0)
    assert lad.ladder.level == "off"
    assert set(lad.feed(fps(20.0), 10.0)) == {"off"}  # bad enough for degraded, but off is final


def test_x33_a_noise_degradation_hands_back_the_threshold_multiplier_when_it_ends() -> None:
    lad = Ladder()
    lad.feed(fps(30.0, noise=0.03), 3.0)
    assert lad.ladder.strict is True
    lad.feed(GOOD, limits.AIR_LEVEL_RECOVER_S)
    assert (lad.ladder.level, lad.ladder.reason, lad.ladder.strict) == ("ok", "", False)


def test_x33_while_the_hands_are_away_the_answer_is_the_level_held() -> None:
    lad = Ladder()
    lad.feed(fps(20.0), 2.0)
    assert lad.ladder.level == "degraded"
    assert set(lad.feed(GOOD, 3.0, hands=False)) == {"degraded"}


# ======================================================================================================================
# The session around them: the keyboard rig (controller loop, sink, fake desktop) over a scripted air press.
# ======================================================================================================================

#: Appendix F.1, word for word. A change of the session's texts is a contract change and fails here.
F1 = {
    "banner_fps": "Air tap is less sure: camera at {fps} fps. Tap a little firmer.",
    "banner_noise": "Air tap is less sure: hand tracking is shaky. Tap a little firmer.",
    "banner_gaps": "Air tap is less sure: the camera is dropping frames. Tap a little firmer.",
    "off_fps": "Air tap off: camera at {fps} fps, using pinch",
    "off_noise": "Air tap off: hand tracking too shaky, using pinch",
    "tap": "Tap: {side} {finger}  {n}/{N}",
    "good": "Good  {n}/{N}",
    "restart": "Only tap the finger the strip names. Starting again.",
    "stuck_finger": "{Side} {finger}: tap a bit firmer with your fingers raised",
    "stuck_all": "Taps not showing up? Try /jarvis hands keyboard press pinch",
    "posture": "Raise your fingers a little, curved, as over a real keyboard",
    "speed": "Hold your hands steadier to type",
    "coherence": "Keep the other fingers still while one taps",
    "pinch": "Pinch each finger to your thumb once: {n}/{N}",
}


def air_rig(**kw: object) -> KbRig:
    """An armed review session on a scripted air press, 0.5 s past the sink's warm-up window."""
    rig = KbRig(commit="review", **kw)  # type: ignore[arg-type]
    rig.arm()
    rig.run(0.5)
    return rig


def set_target(rig: KbRig, **changes: object) -> None:
    rig.desktop.target = dataclasses.replace(rig.desktop.target, **changes)


def watch(rig: KbRig, seconds: float) -> list[tuple[float, str]]:
    """Frame after frame for ``seconds`` (or until the session closes): (time, strip) of each."""
    out = []
    for _ in range(round(seconds * rig.fps)):
        if rig.closed is not None:
            break
        rig.run(rig.dt)
        out.append((rig.t, rig.view.strip))
    return out


def spans(log: list[tuple[float, str]], start: str) -> list[tuple[float, float]]:
    """The (first, last) time of each unbroken run of strips that begin with ``start``."""
    found: list[tuple[float, float]] = []
    inside = False
    for t, strip in log:
        if strip.startswith(start):
            if not inside:
                found.append((t, t))
            else:
                found[-1] = (found[-1][0], t)
            inside = True
        else:
            inside = False
    return found


# -------------------------------------------------------------------------------------------- X27: arming


@pytest.mark.parametrize("sides", [("left", "right"), ("right",)])
def test_x27_arming_calibrates_off_sets_every_finger_to_its_own_depth_resets_and_starts_the_sink(
    sides: tuple[Side, ...],
) -> None:
    rig = KbRig(commit="review", sides=sides)
    rig.place()
    assert rig.session.phase == "warmup" and rig.press.calibrating_calls == [True] and rig.press.thresholds == {}
    resets = rig.press.resets
    depths: dict[tuple[Side, int], float] = {}
    end = rig.t + 60.0
    while not rig.session.armed and rig.t < end:
        prompt = rig.session.warmup_prompt
        if prompt is not None:
            depths[prompt] = 0.20 + 0.03 * prompt[1] + (0.02 if prompt[0] == "left" else 0.0)
            rig.warm_tap(*prompt, depth=depths[prompt])
        rig.run(rig.dt)
    assert rig.session.armed and rig.session.phase == "typing" and rig.view.phase == "typing"
    assert rig.press.calibrating_calls == [True, False] and not rig.press.calibrating
    assert set(rig.press.thresholds) == {(side, f) for side in sides for f in range(4)}
    for key, (close, open_) in rig.press.thresholds.items():
        assert close == pytest.approx(depths[key]) and open_ == pytest.approx(depths[key])
    assert rig.press.resets == resets + 1
    assert rig.counts["warmup_accepted"] == 4 * len(sides)
    assert rig.desktop.key_calls == [] and rig.box == "" and rig.session.compose_len == 0  # the last tap typed nothing


def test_x27_the_sink_is_started_at_arming_so_the_pointer_hand_over_is_not_foreign_input() -> None:
    rig = KbRig(commit="review")
    rig.arm()
    assert rig.sink is not None
    rig.desktop.user_typed()
    rig.run(rig.dt)  # inside the 0.3 s window after start(): only a new baseline
    assert rig.sink.counts["foreign"] == 0
    rig.run(0.6)
    rig.desktop.user_typed()
    rig.run(rig.dt)
    assert rig.sink.counts["foreign"] == 1


# ---------------------------------------------------------------------------------------- X28: a stuck warm-up


def test_x28_a_warmup_nobody_taps_names_the_finger_at_15_s_the_general_hint_at_25_and_closes_idle_at_90() -> None:
    rig = KbRig(commit="review", keep_views=False)
    rig.place()
    begun = rig.t
    log = [(t - begun, strip) for t, strip in watch(rig, 100.0)]
    first = spans(log, "Tap: right index")[0][0]
    assert first == pytest.approx(1.0, abs=0.1)  # a finger is named after the gap, not at once
    (finger,) = spans(log, "Right index: tap a bit firmer")
    assert finger[0] == pytest.approx(first + 15.0, abs=0.1) and finger[1] - finger[0] == pytest.approx(6.0, abs=0.2)
    (everything,) = spans(log, "Taps not showing up?")
    assert everything[0] == pytest.approx(25.0, abs=0.1) and everything[1] - everything[0] == pytest.approx(
        6.0, abs=0.2
    )
    assert rig.closed == "idle" and rig.t == pytest.approx(90.0, abs=0.1)  # 90 s from the open
    assert log[-1][1] != "" and rig.desktop.key_calls == []


def test_x28_the_finger_clock_restarts_when_the_next_finger_is_named_and_the_general_one_at_the_last_tap() -> None:
    rig = KbRig(commit="review", keep_views=False)
    rig.place()
    begun = rig.t
    log: list[tuple[float, str]] = []
    rig.run(1.5)
    assert rig.session.warmup_prompt == ("right", 0)
    rig.warm_tap("right", 0)  # one good tap at about 1.5 s
    log += watch(rig, 40.0)
    log = [(t - begun, strip) for t, strip in log]
    accepted = rig.counts["warmup_accepted"]
    assert accepted == 1
    named = spans(log, "Tap: left index")[0][0]
    (finger,) = spans(log, "Left index: tap a bit firmer")
    assert finger[0] == pytest.approx(named + 15.0, abs=0.1)
    (everything,) = spans(log, "Taps not showing up?")
    assert everything[0] == pytest.approx(1.5 + 25.0, abs=0.2)  # 25 s after the last accepted tap


def test_x28_a_warmup_that_accepts_some_taps_and_keeps_restarting_points_to_pinch_after_the_second_restart() -> None:
    """The 25 s clock restarts at every accepted tap, so a warm-up that accepts a finger and then restarts on three
    strays (landmark noise of 0.002, two hands) never reached it: the user saw only "Starting again" for 90 s."""
    rig = KbRig(commit="review", keep_views=False)
    rig.place()
    begun = rig.t
    log: list[tuple[float, str]] = []
    for _ in range(2):
        log += watch(rig, 1.5)
        prompt = rig.session.warmup_prompt
        assert prompt is not None
        rig.warm_tap(*prompt)  # one good tap: the clock of the general hint starts again
        log += watch(rig, 1.2)
        for _ in range(3):
            rig.warm_tap("right", 3)  # never the finger that is named next
            log += watch(rig, 0.2)
    assert rig.counts["warmup_restart"] == 2 and not rig.session.armed
    log += watch(rig, 10.0)
    log = [(t - begun, strip) for t, strip in log]
    (everything,) = spans(log, "Taps not showing up?")
    assert everything[0] < 20.0  # well before 25 s without an accepted tap could say it
    assert everything[1] - everything[0] == pytest.approx(6.0, abs=0.2)


def test_x28_one_restart_is_not_yet_a_reason_to_say_pinch() -> None:
    rig = KbRig(commit="review", keep_views=False)
    rig.place()
    log = watch(rig, 1.5)
    for _ in range(3):
        rig.warm_tap("right", 3)
        log += watch(rig, 0.2)
    assert rig.counts["warmup_restart"] == 1
    log += watch(rig, 12.0)
    assert not spans(log, "Taps not showing up?")


def test_x28_a_warmup_that_was_tapped_and_never_armed_closes_air_unreliable_not_idle() -> None:
    """The toast "nobody was using it" is false for a user who tapped for 90 s on a camera too noisy for the air tap."""
    rig = KbRig(commit="review", keep_views=False)
    rig.place()
    begun = rig.t
    while rig.closed is None and rig.t < begun + 100.0:
        rig.warm_tap("right", 3)
        rig.run(1.0)
    assert not rig.session.armed and rig.counts["warmup_tap"] > 0
    assert rig.closed == "air_unreliable" and rig.t - begun == pytest.approx(90.0, abs=1.5)
    assert rig.desktop.key_calls == []


def one_hand_rig(flip_at: int | None) -> KbRig:
    """One right hand standing still; the label of frame ``flip_at`` (counted from 1) says "left". The tracker names the
    placed hand by the label of its last placing frame, so the frame that places it is the one that matters."""
    rig = KbRig(commit="review", sides=("right",), keep_views=False)
    while rig.session.phase == "placing" and rig.t < 5.0:
        frame = rig.hands_frame(rig.t + rig.dt)
        if rig.frames + 1 == flip_at:
            hands = tuple(dataclasses.replace(h, handedness="left") for h in frame.hands)
            frame = Frame(frame.t, hands, frame.width, frame.height)
        rig.feed([frame])
    return rig


def test_x26_one_flipped_label_on_the_frame_that_places_a_lone_hand_does_not_name_the_other_hand() -> None:
    clean = one_hand_rig(None)
    placed_at = clean.frames
    assert clean.session.phase == "warmup"
    flipped = one_hand_rig(placed_at)
    assert flipped.frames == placed_at and flipped.session.phase == "warmup"  # the control: the same frame places it
    flipped.run(1.5)
    assert flipped.session.warmup_prompt == ("right", 0)


def test_x26_a_flipped_label_a_few_frames_before_the_placing_frame_is_harmless() -> None:
    clean = one_hand_rig(None)
    flipped = one_hand_rig(clean.frames - 3)
    flipped.run(1.5)
    assert flipped.session.warmup_prompt == ("right", 0)


# ----------------------------------------------------------------------------------- X29: a returning hand


def test_x29_hands_that_leave_and_return_keep_their_depths_and_nothing_is_calibrated_again() -> None:
    rig = air_rig()
    kept = dict(rig.press.thresholds)
    calls = list(rig.press.calibrating_calls)
    rig.empty(2.0)
    rig.run(2.0)
    assert rig.session.armed and rig.press.thresholds == kept and rig.press.calibrating_calls == calls
    rig.type_text("ab")
    assert rig.box == "ab"


# --------------------------------------------------------------------------------------------- X31 to X35: the ladder


def test_x35_the_session_texts_are_the_strings_of_appendix_f1_word_for_word() -> None:
    for key, text in F1.items():
        assert SESSION_TEXT[key] == text, key
    for key in ("banner_fps", "banner_noise", "banner_gaps", "off_fps", "off_noise"):
        assert "{" not in SESSION_TEXT[key].replace("{fps}", "")  # `{fps}` is the one number a banner interpolates


def test_x31_a_camera_at_thirty_frames_stays_ok_and_shows_no_banner() -> None:
    rig = air_rig()
    rig.run(12.0)
    assert rig.session.level == "ok" and rig.view.banner == "" and rig.view.banner_level == ""
    assert rig.press.levels == []


def test_x31_below_twenty_six_frames_for_two_seconds_degrades_with_a_banner_and_no_strict_level() -> None:
    rig = air_rig()
    rig.press.set_quality(24.0, 0.001)
    rig.run(1.9)
    assert rig.session.level == "ok" and rig.view.banner == ""
    rig.run(0.3)
    assert rig.session.level == "degraded"
    assert rig.view.banner == "Air tap is less sure: camera at 24 fps. Tap a little firmer."
    assert rig.view.banner_level == "warn"
    assert rig.press.levels == []  # a slow camera loses recall, not precision: the thresholds stay (X33)
    rig.run(10.0)
    assert rig.view.banner and rig.session.level == "degraded"  # the banner stays as long as the level does


@pytest.mark.parametrize(("fps", "shown"), [(23.6, "24"), (24.4, "24"), (25.5, "26")])
def test_x35_the_banner_names_the_frame_rate_as_a_whole_number(fps: float, shown: str) -> None:
    rig = air_rig()
    rig.press.set_quality(fps, 0.001)
    rig.run(2.4)
    assert rig.view.banner == f"Air tap is less sure: camera at {shown} fps. Tap a little firmer."


def test_x32_noise_degrades_after_three_seconds_asks_the_press_for_strict_thresholds_and_hands_them_back() -> None:
    rig = air_rig()
    rig.press.set_quality(30.0, 0.03)
    rig.run(2.9)
    assert rig.session.level == "ok" and rig.press.levels == []
    rig.run(0.3)
    assert rig.session.level == "degraded" and rig.press.levels == ["degraded"]
    assert rig.view.banner == "Air tap is less sure: hand tracking is shaky. Tap a little firmer."
    assert rig.view.banner_level == "warn"
    rig.press.set_quality(30.0, 0.001)
    rig.run(7.5)
    assert rig.session.level == "degraded"  # eight seconds of good readings
    rig.run(1.0)
    assert rig.session.level == "ok" and rig.press.levels == ["degraded", "ok"]
    assert rig.view.banner == "" and rig.view.banner_level == ""


def test_x33_a_one_second_dip_of_the_frame_rate_or_the_noise_changes_nothing() -> None:
    rig = air_rig()
    rig.press.set_quality(20.0, 0.001)
    rig.run(1.0)
    rig.press.set_quality(30.0, 0.04)
    rig.run(1.0)
    rig.press.set_quality(30.0, 0.017)
    rig.run(10.0)
    assert rig.session.level == "ok" and rig.view.banner == "" and rig.press.levels == []


def test_x33_holes_in_the_stream_degrade_after_two_seconds_without_strict_and_never_switch_off() -> None:
    rig = air_rig(fallback=True)
    rig.press.set_quality(30.0, 0.017, gaps=3)
    rig.run(1.9)
    assert rig.session.level == "ok"
    rig.run(0.3)
    assert rig.session.level == "degraded"
    assert rig.view.banner == "Air tap is less sure: the camera is dropping frames. Tap a little firmer."
    assert rig.press.levels == []
    rig.press.set_quality(30.0, 0.017, gaps=50)
    rig.run(20.0)
    assert rig.session.level == "degraded" and rig.session.press_name == "air"


def test_x33_no_hand_in_view_changes_nothing_and_a_bad_reading_without_hands_is_not_counted() -> None:
    rig = air_rig()
    rig.press.set_quality(10.0, 0.05)
    rig.empty(6.0)
    assert rig.session.level == "ok" and rig.view.banner == "" and rig.closed is None
    rig.run(1.9)
    assert rig.session.level == "ok"
    rig.run(0.3)
    assert rig.session.level == "off" or rig.closed == "air_unreliable"


def test_x31_below_thirteen_frames_for_two_seconds_switches_to_pinch_with_a_warmup_and_types_nothing() -> None:
    rig = air_rig(fallback=True)
    rig.fill("abc")
    t0 = rig.t + 0.1
    rig.tap(t0, "insert")
    rig.tap(t0 + 0.8, "insert")
    rig.run(1.2)
    guard = rig.view.compose
    assert guard is not None and guard.guard == "insert"
    rig.press.set_quality(10.0, 0.001)
    rig.run(1.9)
    assert rig.session.press_name == "air" and rig.session.armed
    rig.run(0.3)
    assert rig.session.level == "off" and rig.session.press_name == "pinch"
    assert rig.session.phase == "warmup" and not rig.session.armed and rig.closed is None
    assert rig.box == "abc" and rig.desktop.key_calls == []  # the box is kept, the switch types nothing
    assert len(rig.session._queue) == 0
    compose = rig.view.compose
    assert compose is not None and compose.guard is None  # disarm() was called
    assert rig.view.banner == "Air tap off: camera at 10 fps, using pinch" and rig.view.banner_level == "warn"
    assert rig.view.strip == "Pinch each finger to your thumb once: 0/8"
    rig.warm()
    assert rig.session.armed and rig.session.phase == "typing" and rig.session.press_name == "pinch"
    assert rig.fallback_press is not None and len(rig.fallback_press.thresholds) == 8
    assert rig.view.banner.startswith("Air tap off")  # it stays
    rig.run(0.5)
    rig.type_text("d")
    assert rig.box == "abcd" and rig.desktop.key_calls == []
    rig.insert(rig.t + 0.1)
    run_to_summary = 0
    while not rig.summaries and run_to_summary < 400:
        rig.run(rig.dt)
        run_to_summary += 1
    assert rig.typed == "abcd"


def test_x31_the_pinch_warmup_after_a_switch_has_its_own_ninety_seconds() -> None:
    rig = air_rig(fallback=True, keep_views=False)
    opened = 0.0
    rig.press.set_quality(10.0, 0.001)
    rig.run(2.4)
    switched = rig.t
    assert rig.session.press_name == "pinch" and rig.closed is None and switched > 5.0
    while rig.closed is None and rig.t < switched + 120.0:
        rig.run(0.5)
    assert rig.closed == "idle"
    assert rig.t > opened + 90.0 + 2.0  # past the open's own 90 s: the timeout was re-based at the switch
    assert rig.t == pytest.approx(switched + 90.0, abs=1.0)


def test_x31_off_for_noise_says_so() -> None:
    rig = air_rig(fallback=True)
    rig.press.set_quality(30.0, 0.04)
    rig.run(3.2)
    assert rig.session.press_name == "pinch"
    assert rig.view.banner == "Air tap off: hand tracking too shaky, using pinch" and rig.view.banner_level == "warn"


def test_x31_without_a_fallback_a_live_session_closes_air_unreliable_and_counts_its_box() -> None:
    rig = air_rig(fallback=False)
    rig.type_text("abc")
    rig.press.set_quality(10.0, 0.001)
    rig.run(2.4)
    assert rig.closed == "air_unreliable" and rig.session.discarded == 3 and rig.desktop.key_calls == []


def test_x31_the_ladder_does_not_read_a_camera_while_a_hold_is_set() -> None:
    rig = air_rig(fallback=True)
    set_target(rig, blocked="elevated")
    rig.press.set_quality(10.0, 0.001)
    rig.run(5.0)
    assert rig.session.level == "ok" and rig.session.press_name == "air"
    set_target(rig, blocked=None)
    rig.run(2.8)  # the hold, the focus settle, then two seconds of readings
    assert rig.session.press_name == "pinch"


def test_x38_a_practice_session_never_falls_back_it_cuts_the_drill_and_reports_off() -> None:
    rig = KbRig(commit="review", mode="practice", fallback=False)
    rig.arm()
    rig.run(0.5)
    assert not rig.session.practice_done
    rig.press.set_quality(10.0, 0.001)
    rig.run(2.4)
    assert rig.session.practice_done and rig.closed is None and rig.session.press_name == "air"
    result = rig.session.practice_result()
    assert result is not None and result.level == "off" and not result.completed
    assert rig.view.strip == NOT_USABLE
    assert rig.view.banner == ""  # the strip says it; no second line


def practice_rig(phrases: tuple[str, ...]) -> KbRig:
    """An armed air practice that cannot fall back, with short phrases (the script runs on time, not on typing)."""
    rig = KbRig(commit="review", mode="practice", fallback=False, phrases=phrases)
    rig.arm()
    return rig


def segment_of(rig: KbRig) -> str:
    script = rig.session._script
    assert script is not None
    return script.segment


def run_to_segment(rig: KbRig, segment: str, limit_s: float = 300.0) -> None:
    begun = rig.t
    while segment_of(rig) != segment and rig.t < begun + limit_s:
        rig.run(rig.dt)
    assert segment_of(rig) == segment


def test_x38_the_ladder_does_not_cut_a_practice_during_its_own_wave_and_waits_for_the_next_segment() -> None:
    """The rest asks for a wave and the talk for moving hands, which is what makes a camera read noisy: the ladder
    finding the air tap unusable there would cut the practice by its own prompt (the prompt still shows and the level
    is still reported)."""
    rig = practice_rig(("a", "b", "c", "d"))  # phrase, rest, phrase, rest, talk
    run_to_segment(rig, "rest")
    rig.press.set_quality(10.0, 0.001)
    rig.run(limits.AIR_REST_S - 1.0)
    assert rig.session.level == "off" and segment_of(rig) == "rest"
    assert not rig.session.practice_done and rig.view.strip == "Rest: do not tap. Wave, open and close your hands."
    assert rig.view.banner == ""
    rig.run(2.0)  # the rest is over; the camera is still unusable and the next phrase is not a wave
    assert segment_of(rig) == "phrase" and rig.session.practice_done
    result = rig.session.practice_result()
    assert result is not None and result.level == "off" and not result.completed
    assert rig.view.strip == NOT_USABLE


def test_x38_a_cut_that_waits_past_the_last_frame_of_the_talk_does_not_unfinish_a_finished_practice() -> None:
    rig = practice_rig(("a",))  # phrase, rest, talk
    run_to_segment(rig, "talk")
    rig.press.set_quality(10.0, 0.001)
    rig.run(limits.AIR_TALK_S - 1.0)
    assert segment_of(rig) == "talk" and not rig.session.practice_done
    assert rig.view.strip == "Talk to the camera as on a call. Keep your hands moving. Do not tap."
    rig.run(3.0)  # the talk ends, and the ladder is still off in the frames after it
    assert segment_of(rig) == "done" and rig.session.practice_done
    result = rig.session.practice_result()
    assert result is not None and result.completed and result.level == "off"
    assert rig.view.strip != NOT_USABLE


# ---------------------------------------------------------------------------------- X39 to X43: events and the phases


def test_x39_events_while_placing_are_discarded_not_armed() -> None:
    rig = KbRig(commit="review")
    rig.tap(rig.t + 0.05, "a")
    rig.run(0.3)
    assert rig.session.phase == "placing" and rig.counts["not_armed"] == 1 and rig.counts["warmup_tap"] == 0


def test_x39_events_in_the_warmup_feed_it_and_a_slow_hold_discards_them_while_the_press_keeps_running() -> None:
    rig = KbRig(commit="review")
    rig.place()
    rig.run(1.5)
    before = rig.counts["warmup_tap"]
    rig.warm_tap(*rig.session.warmup_prompt)  # type: ignore[misc]
    rig.run(0.3)
    assert rig.counts["warmup_tap"] == before + 1 and rig.counts["warmup_accepted"] == 1
    slow_camera(rig, 2.0, fps=6.0)
    updates = rig.press.updates
    taps = rig.counts["warmup_tap"]
    rig.warm_tap(*rig.session.warmup_prompt)  # type: ignore[misc]
    slow_camera(rig, 1.0, fps=6.0)
    assert rig.counts["held"] >= 1 and rig.counts["warmup_tap"] == taps
    assert rig.press.updates == updates + 6  # markers still move while the hold lasts
    assert rig.counts["warmup_accepted"] == 1


def slow_camera(rig: KbRig, seconds: float, fps: float = 6.0) -> None:
    keep = rig.fps, rig.dt
    rig.fps, rig.dt = fps, 1 / fps
    rig.run(seconds)
    rig.fps, rig.dt = keep


def test_x39_events_while_a_hold_is_set_are_discarded_and_the_press_still_runs() -> None:
    rig = air_rig()
    set_target(rig, blocked="elevated")
    rig.run(0.3)
    updates, held = rig.press.updates, rig.counts["held"]
    rig.tap(rig.t + 0.05, "a")
    rig.run(0.5)
    assert rig.box == "" and rig.counts["held"] == held + 1
    assert rig.press.updates == updates + round(0.5 * rig.fps)
    assert rig.view.strip == "Paused: that window cannot be typed into"


def test_x40_warm_up_then_hello_three_inserts_type_it_and_nothing_before() -> None:
    rig = KbRig(commit="review")
    rig.arm()
    assert rig.counts["warmup_accepted"] == 8 and rig.desktop.key_calls == []
    rig.run(0.5)
    rig.type_text("hello")
    assert rig.box == "hello" and rig.desktop.key_calls == []
    rig.tap(rig.t + 0.1, "backspace")
    rig.run(0.5)
    assert rig.box == "hell"
    rig.type_text("o")
    t0 = rig.t + 0.1
    rig.tap(t0, "insert")
    rig.tap(t0 + 1.0, "insert")
    rig.run(1.5)
    assert rig.desktop.key_calls == [] and rig.box == "hello"
    rig.tap(rig.t + 0.5, "insert")
    rig.run(1.5)
    assert rig.typed == "hello" and rig.box == "" and rig.summaries[0].outcome == "done"


def test_x41_a_hold_drops_the_taps_it_covers_the_press_is_reset_when_it_ends_and_the_next_tap_commits() -> None:
    rig = air_rig()
    resets = rig.press.resets
    rig.desktop.user_typed()
    rig.run(0.2)
    rig.tap(rig.t + 0.1, "a")  # a finger that was already closing when the hold began
    rig.run(1.2)
    assert rig.box == "" and rig.press.resets == resets
    rig.run(0.5)
    assert rig.press.resets == resets + 1 and rig.view.strip != "Paused: you used the keyboard or mouse"
    rig.tap(rig.t + 0.1, "b")
    rig.run(0.5)
    assert rig.box == "b"


@pytest.mark.parametrize(("first", "second"), [("a", "l"), ("l", "a")])
def test_x43_two_hands_tapping_in_the_same_frame_give_two_taps_typed_in_order(first: str, second: str) -> None:
    rig = air_rig()
    t = rig.t + 0.1
    rig.tap(t, first)
    rig.tap(t, second)
    rig.run(0.5)
    assert rig.box == first + second and rig.counts["stale"] == 0


def test_x43_the_queue_holds_three_and_the_fourth_of_one_frame_is_dropped_and_counted() -> None:
    rig = air_rig()
    t = rig.t + 0.1
    for ch in "asdf":
        rig.tap(t, ch)
    rig.run(0.6)
    assert rig.box == "asd" and rig.counts["queue"] == 1


# ------------------------------------------------------------------------------------------ X45: the view's words


def test_x45_private_suppresses_ring_fills_and_the_amber_key_and_pulses_instead() -> None:
    rig = air_rig()
    rig.press.view_states[("right", 0)] = ("closing", 0.8, "")
    rig.run(0.2)
    tip = next(t for t in rig.view.tips if (t.side, t.finger) == ("right", 0))
    key = rig.layout.key_at(tip.u, tip.v, rig.tuning.edge_tolerance)
    assert key is not None and tip.fill == pytest.approx(0.8) and (key.index, "target") in rig.view.lit
    rig.session.set_private(True)
    rig.run(0.2)
    tip = next(t for t in rig.view.tips if (t.side, t.finger) == ("right", 0))
    assert tip.fill == 0.0 and all(kind != "target" for _, kind in rig.view.lit)
    assert not rig.view.pulse
    rig.press.view_states.clear()
    rig.tap(rig.t + 0.05, "a")
    rig.run(0.1)
    assert rig.view.pulse and rig.view.compose is not None and rig.view.compose.text == "•"
    rig.run(0.6)
    assert not rig.view.pulse


# ------------------------------------------------------------------------------------------------- the strip's hints


def test_a_posture_note_that_lasts_a_second_and_a_half_shows_its_hint_for_four_seconds_then_at_most_once_in_five() -> (
    None
):
    rig = air_rig()
    rig.press.view_states[("right", 0)] = ("open", 0.0, "posture")
    log = watch(rig, 12.0)
    first, second = spans(log, SESSION_TEXT["posture"])[:2]
    assert first[0] - log[0][0] == pytest.approx(1.5, abs=0.15) and first[1] - first[0] == pytest.approx(4.0, abs=0.2)
    assert second[0] - first[0] == pytest.approx(5.0, abs=0.2)  # a note that goes on is said again, never faster


def test_a_note_that_comes_and_goes_inside_the_wait_shows_nothing() -> None:
    rig = air_rig()
    for _ in range(4):
        rig.press.view_states[("right", 0)] = ("open", 0.0, "speed")
        rig.run(1.0)
        rig.press.view_states.clear()
        rig.run(0.5)
    assert all(strip not in SESSION_TEXT.values() for _, strip in watch(rig, 1.0))


def test_the_hints_go_posture_then_speed_then_coherence_one_at_a_time() -> None:
    rig = air_rig()
    notes = {0: "coherence", 1: "speed", 2: "posture"}
    for finger, note in notes.items():
        rig.press.view_states[("right", finger)] = ("open", 0.0, note)
    log = watch(rig, 3.0)
    assert {s for _, s in log} & set(SESSION_TEXT.values()) == {SESSION_TEXT["posture"]}
    del rig.press.view_states[("right", 2)]
    log = watch(rig, 6.0)
    assert SESSION_TEXT["speed"] in {s for _, s in log} and SESSION_TEXT["coherence"] not in {s for _, s in log}
    del rig.press.view_states[("right", 1)]
    log = watch(rig, 6.0)
    assert SESSION_TEXT["coherence"] in {s for _, s in log}


def test_a_private_session_shows_no_hint() -> None:
    rig = air_rig()
    rig.session.set_private(True)
    rig.press.view_states[("right", 0)] = ("open", 0.0, "posture")
    assert all(strip != SESSION_TEXT["posture"] for _, strip in watch(rig, 6.0))


def test_a_weak_tap_names_the_finger_once() -> None:
    rig = air_rig()
    rig.press.view_states[("right", 1)] = ("open", 0.0, "weak")
    log = watch(rig, 4.0)
    assert any(strip == "Middle: tap a bit firmer" for _, strip in log)
    rig.press.view_states.clear()
    rig.run(1.0)
    rig.press.view_states[("right", 1)] = ("open", 0.0, "weak")
    assert all(strip != "Middle: tap a bit firmer" for _, strip in watch(rig, 6.0))


def test_the_warmup_strip_counts_and_names_and_a_restart_says_so_for_two_seconds() -> None:
    rig = KbRig(commit="review")
    rig.place()
    assert rig.view.strip == "Good  0/8"
    rig.run(1.2)
    assert rig.view.strip == "Tap: right index  0/8"
    rig.warm_tap("right", 0)
    rig.run(0.2)
    assert rig.view.strip == "Good  1/8"
    rig.run(1.0)
    assert rig.view.strip == "Tap: left index  1/8"
    for _ in range(3):
        rig.warm_tap("right", 3)
        rig.run(0.2)
    assert rig.view.strip == "Only tap the finger the strip names. Starting again."
    rig.run(1.9)
    assert rig.view.strip.startswith("Tap: right index  0/8")


def test_the_hints_of_a_private_warmup_are_suppressed() -> None:
    rig = KbRig(commit="review", keep_views=False)
    rig.place()
    rig.session.set_private(True)
    log = watch(rig, 40.0)
    assert not spans(log, "Right index: tap a bit firmer") and not spans(log, "Taps not showing up?")
