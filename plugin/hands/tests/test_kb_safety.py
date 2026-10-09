"""The review keyboard through the session, the sink and a fake desktop: nothing is typed except by a confirmed run.

Everything here runs the controller's frame loop (DESIGN-KEYBOARD.md 3.8, ``KbRig``) over a scripted air press, so every
tap is exact. The machine's own rules (guards, windows, bounces) have their tests in ``test_kb_review.py``; these are
the S-tests of table 6.2 and the session halves of the N-tests.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import random
from collections.abc import Callable
from itertools import pairwise

import pytest

import jarvis_hands.keyboard.review as review_module
from jarvis_hands.desktop.fake import events_balanced
from jarvis_hands.keyboard.limits import (
    COMPOSE_MAX,
    STORM_FREEZE_S,
)
from jarvis_hands.keyboard.rig import ASPECT, KbRig, ScriptedPress
from jarvis_hands.keyboard.session import KeyboardSession
from jarvis_hands.keyboard.tuning import Tuning

SPACE = (0x20, 0x39, 0)
UNICODE = 0x4


def review_rig(**kw: object) -> KbRig:
    """An armed review session whose sink has left its 0.3 s warm-up window."""
    rig = KbRig(commit="review", **kw)  # type: ignore[arg-type]
    rig.arm()
    rig.run(0.5)
    return rig


def machine(rig: KbRig):
    m = rig.session._machine
    assert m is not None
    return m


def run_until(rig: KbRig, done: Callable[[], bool], limit_s: float = 10.0) -> bool:
    """Frame after frame until ``done()``; False when the limit or a close came first."""
    end = rig.t + limit_s
    while not done() and rig.t < end and rig.closed is None:
        rig.run(rig.dt)
    return done()


def start_run(rig: KbRig, text: str | None = None, *, first_tap_in: float = 0.1) -> None:
    """Box ``text`` (when given), then the valid triple, then frames until a run has begun (or ended)."""
    before = len(rig.summaries)
    if text is not None:
        rig.fill(text)
    rig.insert(rig.t + first_tap_in)
    assert run_until(rig, lambda: rig.session.review_state == "inserting" or len(rig.summaries) > before, 4.0)


def finish_run(rig: KbRig, runs: int = 1, limit_s: float = 12.0) -> None:
    """Frames until ``runs`` runs have ended in all."""
    assert run_until(rig, lambda: len(rig.summaries) >= runs, limit_s)


def insert_text(rig: KbRig, text: str) -> None:
    before = len(rig.summaries)
    start_run(rig, text)
    finish_run(rig, before + 1)


def with_target(rig: KbRig, **changes: object) -> None:
    rig.desktop.target = dataclasses.replace(rig.desktop.target, **changes)


def after(rig: KbRig, n: int, act: Callable[[], None]) -> None:
    """Do ``act`` right after the ``n``-th stroke of the desktop was recorded."""
    rig.desktop.after_key = lambda count: act() if count == n else None


# ---------------------------------------------------------------------------------------------- S41, S42: the run


def test_s41_the_happy_path_types_the_box_one_balanced_batch_per_character() -> None:
    rig = review_rig()
    rig.type_text("hello world")
    assert rig.box == "hello world" and rig.desktop.key_calls == []
    rig.insert(rig.t + 0.1)
    finish_run(rig)
    assert rig.typed == "hello world"
    batches = rig.desktop.key_batches
    assert len(batches) == 11 and all(events_balanced(b) for b in batches)
    assert [b[0][2] & UNICODE for b in batches].count(UNICODE) == 10  # the space is a key, not a Unicode character
    assert batches[5] == [(0x20, 0x39, 0), (0x20, 0x39, 2)] and batches[5][0] == SPACE
    assert rig.box == "" and rig.session.review_state == "composing"
    m = machine(rig)
    assert m.last_insert is not None and m.last_insert.chars == 11
    assert (m.counts["insert_start"], m.counts["insert_done"]) == (1, 1)
    assert [(s.kind, s.outcome, s.sent, s.of) for s in rig.summaries] == [("text", "done", 11, 11)]


def test_s41_one_character_per_frame_and_none_before_the_third_tap() -> None:
    rig = review_rig()
    rig.fill("abcdef")
    rig.insert(rig.t + 0.1)
    rig.run(1.9)
    assert rig.desktop.key_calls == [] and rig.session.review_state == "composing"
    finish_run(rig)
    assert rig.typed == "abcdef"
    per_frame = [r.steps for r in rig.records if r.steps]
    assert per_frame == [1] * 6 and all(r.strokes == 0 for r in rig.records)


def test_s42_a_focus_change_after_five_characters_leaves_the_rest_and_the_second_run_types_it() -> None:
    rig = review_rig()
    other = dataclasses.replace(rig.desktop.target, hwnd=777, pid=888)
    original = rig.desktop.target
    after(rig, 5, lambda: setattr(rig.desktop, "target", other))
    start_run(rig, "abcdefghij")
    finish_run(rig)
    assert rig.typed == "abcde"
    summary = rig.summaries[0]
    assert (summary.outcome, summary.reason, summary.sent, summary.of) == ("aborted", "focus", 5, 10)
    assert rig.box == "fghij" and rig.session.review_state == "aborted"
    rig.desktop.target = original
    rig.run(2.0)
    rig.insert(rig.t + 0.1)
    finish_run(rig, 2)
    assert rig.summaries[1].outcome == "done" and rig.summaries[1].of == 5
    assert rig.typed == "abcdefghij" and rig.box == ""


# --------------------------------------------------------------------------------------------- S43: the user acts


def test_s43_foreign_input_after_the_third_character_stops_the_run_at_three() -> None:
    rig = review_rig()
    after(rig, 3, rig.desktop.user_typed)
    start_run(rig, "abcdefghij")
    finish_run(rig)
    assert rig.typed == "abc" and rig.box == "defghij"
    assert (rig.summaries[0].outcome, rig.summaries[0].reason) == ("aborted", "yield")


def test_s43_a_physical_ctrl_after_the_second_character_stops_the_run_at_two() -> None:
    rig = review_rig()

    def ctrl() -> None:
        rig.desktop.modifiers = True

    after(rig, 2, ctrl)
    start_run(rig, "abcdefghij")
    finish_run(rig)
    assert rig.typed == "ab" and rig.box == "cdefghij"
    assert (rig.summaries[0].outcome, rig.summaries[0].reason) == ("aborted", "yield")


def test_s43_the_stop_tap_07_s_into_the_run_stops_it_at_that_moment() -> None:
    rig = review_rig()
    rig.fill("a" * COMPOSE_MAX)
    start_run(rig)
    begun = rig.t
    rig.tap(begun + 0.7, "insert")
    finish_run(rig)
    summary = rig.summaries[0]
    assert (summary.outcome, summary.reason) == ("aborted", "stopped")
    assert abs(summary.sent - 0.7 * rig.fps) <= 3 and len(rig.typed) == summary.sent
    assert len(rig.box) == COMPOSE_MAX - summary.sent


def test_s43_a_stop_tap_03_s_into_the_run_is_ignored_as_busy() -> None:
    rig = review_rig()
    rig.fill("a" * 100)
    start_run(rig)
    rig.tap(rig.t + 0.3, "insert")
    finish_run(rig)
    assert rig.summaries[0].outcome == "done" and len(rig.typed) == 100
    assert rig.counts["busy"] >= 1


# ----------------------------------------------------------------------------------------- S44: the window changes

TARGET_CHANGES = [
    pytest.param({"blocked": "elevated"}, "blocked", id="elevated"),
    pytest.param({"blocked": "shell"}, "blocked", id="shell"),
    pytest.param({"blocked": "own"}, "blocked", id="own"),
    pytest.param({"blocked": "none"}, "blocked", id="none"),
    pytest.param({"password": True}, "password", id="password"),
    pytest.param({"covered": True}, "covered", id="covered"),
    pytest.param({"pid": 4242}, "focus", id="reused_handle"),
]


@pytest.mark.parametrize(("change", "reason"), TARGET_CHANGES)
def test_s44_a_target_that_turns_unsafe_between_two_characters_stops_the_run_before_the_next(
    change: dict[str, object], reason: str
) -> None:
    rig = review_rig()
    after(rig, 3, lambda: with_target(rig, **change))
    start_run(rig, "abcdefghij")
    finish_run(rig)
    assert rig.typed == "abc" and rig.box == "defghij"
    summary = rig.summaries[0]
    assert (summary.outcome, summary.reason, summary.sent, summary.of) == ("aborted", reason, 3, 10)
    rig.run(3.0)
    assert rig.typed == "abc"


@pytest.mark.parametrize(("change", "reason"), TARGET_CHANGES[:-1])  # a new pid before the pin is just the target
def test_s44_a_gate_that_already_holds_drops_the_insert_taps_and_nothing_starts(
    change: dict[str, object], reason: str
) -> None:
    rig = review_rig()
    rig.fill("abcdefghij")
    with_target(rig, **change)
    rig.insert(rig.t + 0.1)
    rig.run(3.5)
    assert rig.desktop.key_calls == [] and rig.box == "abcdefghij" and rig.summaries == []
    assert rig.session.review_state == "composing" and rig.counts["held"] >= 1


@pytest.mark.parametrize(("change", "reason"), TARGET_CHANGES[:-1])
def test_s44_begin_run_refuses_a_target_that_turned_unsafe_after_the_gate_of_the_same_frame(
    change: dict[str, object], reason: str
) -> None:
    rig = review_rig()
    rig.fill("abcdefghij")
    t = rig.t + 0.1
    rig.tap(t, "insert")
    rig.tap(t + 1.0, "insert")
    rig.tap(t + 2.0, "insert", before=lambda: with_target(rig, **change))
    rig.run(3.5)
    assert rig.desktop.key_calls == [] and rig.box == "abcdefghij"
    summary = rig.summaries[0]
    assert (summary.outcome, summary.reason, summary.sent, summary.of) == ("aborted", reason, 0, 10)
    assert rig.answers == ["hold"] and rig.sink is not None and not rig.sink.run_active


# ------------------------------------------------------------------------------------------- S45: the overlay dies


def test_s45_an_overlay_that_stops_drawing_mid_run_aborts_it_with_overlay() -> None:
    rig = review_rig()
    start_run(rig, "abcdefghij")
    assert run_until(rig, lambda: len(rig.desktop.key_calls) >= 3)
    rig.overlay_ok = False
    rig.run(rig.dt)
    rig.overlay_ok = True  # even a single frame without a drawn keyboard stops a run: nobody could see what it types
    finish_run(rig)
    assert rig.typed == "abc" and rig.box == "defghij"
    assert (rig.summaries[0].outcome, rig.summaries[0].reason) == ("aborted", "overlay")


def test_s45_an_overlay_hold_while_composing_drops_taps_and_closes_nothing() -> None:
    rig = review_rig()
    rig.overlay_ok = False
    rig.type_text("abc")
    assert rig.box == "" and rig.closed is None and rig.counts["held"] >= 1
    assert rig.session.review_state == "composing"
    rig.overlay_ok = True
    rig.type_text("abc")
    assert rig.box == "abc"


# ----------------------------------------------------------------------------- S46: the desktop refuses or half takes


def test_s46_an_input_blocked_refusal_at_the_fourth_character_leaves_the_remainder_from_the_fourth() -> None:
    rig = review_rig()
    after(rig, 3, lambda: setattr(rig.desktop, "fail_keys", 1))
    start_run(rig, "abcdefghij")
    finish_run(rig)
    assert rig.typed == "abc" and rig.box == "defghij"
    summary = rig.summaries[0]
    assert (summary.outcome, summary.reason, summary.sent, summary.of) == ("aborted", "failed", 3, 10)
    rig.run(3.0)
    rig.insert(rig.t + 0.1)
    finish_run(rig, 2)
    assert rig.typed == "abcdefghij" and rig.summaries[1].of == 7


def test_s46_a_partly_taken_batch_at_the_fourth_character_counts_as_typed() -> None:
    rig = review_rig()
    after(rig, 3, lambda: setattr(rig.desktop, "partial_keys", 1))
    start_run(rig, "abcdefghij")
    finish_run(rig)
    assert rig.typed == "abcd" and rig.box == "efghij"
    summary = rig.summaries[0]
    assert (summary.outcome, summary.reason, summary.sent, summary.of) == ("aborted", "failed", 4, 10)
    rig.run(3.0)
    rig.insert(rig.t + 0.1)
    finish_run(rig, 2)
    assert rig.typed == "abcdefghij" and rig.summaries[1].of == 6  # the fourth is never typed twice


def test_s46b_an_exception_after_delivery_is_maybe_and_the_character_is_never_typed_again() -> None:
    rig = review_rig()
    after(rig, 4, lambda: setattr(rig.desktop, "raise_after", RuntimeError("x")))  # raised by the 4th call itself
    start_run(rig, "abcdefghij")
    finish_run(rig)
    assert rig.typed == "abcd" and rig.box == "efghij"
    summary = rig.summaries[0]
    assert (summary.outcome, summary.reason, summary.sent) == ("aborted", "failed", 4)
    assert rig.sink is not None and rig.sink.counts["maybe"] == 1


def test_s46b_a_refused_stroke_is_failed_nothing_is_recorded_and_the_character_stays_in_the_box() -> None:
    from jarvis_hands.desktop.keys import KeyRefused

    rig = review_rig()
    real = rig.desktop.send_keys
    calls: list[int] = []

    def refuse_the_fourth(strokes: object, *, inject: object = "unicode") -> int:
        calls.append(1)
        if len(calls) == 4:
            raise KeyRefused("fixed")
        return real(strokes, inject=inject)  # type: ignore[arg-type]

    rig.desktop.send_keys = refuse_the_fourth  # type: ignore[method-assign]
    start_run(rig, "abcdefghij")
    finish_run(rig)
    assert rig.typed == "abc" and rig.box == "defghij"
    assert (rig.summaries[0].reason, rig.summaries[0].sent) == ("failed", 3)
    assert rig.sink is not None and rig.sink.counts["bad_stroke"] == 1


def test_s47_three_refusals_in_a_row_across_runs_close_the_session_and_count_the_box() -> None:
    rig = review_rig()
    rig.desktop.fail_keys = 3
    rig.fill("abcdefghij")
    for i in range(3):
        rig.insert(rig.t + 0.1)
        run_until(rig, lambda n=i: len(rig.summaries) > n or rig.closed is not None, 6.0)
        if rig.closed is not None:
            break
        rig.run(2.0)
    assert rig.closed == "input_blocked" and rig.session.closed == "input_blocked"
    assert rig.typed == "" and rig.session.discarded == 10
    assert [s.reason for s in rig.summaries] == ["failed", "failed"]
    rig.run(1.0)
    assert rig.desktop.key_calls == []


# ----------------------------------------------------------------------------------------------- S47b: close paths

CLOSE_REASONS = [
    "command",
    "paused",
    "desktop_locked",
    "no_overlay",
    "camera",
    "disabled",
    "error",
    "input_blocked",
    "air_unreliable",
]


@pytest.mark.parametrize("reason", CLOSE_REASONS)
def test_s49_a_close_with_a_box_discards_it_counts_it_and_sends_nothing_after(reason: str) -> None:
    rig = review_rig()
    rig.type_text("hello")
    rig.session.close(reason)  # type: ignore[arg-type]
    assert rig.session.closed == reason and rig.session.discarded == 5 and not rig.session.armed
    rig.tap(rig.t + 0.1, "a")
    rig.insert(rig.t + 0.2)
    rig.run(3.0)
    assert rig.closed == reason and rig.desktop.key_calls == [] and rig.box == ""
    assert rig.session.close("error") is None and rig.session.closed == reason  # the first reason wins


@pytest.mark.parametrize("reason", CLOSE_REASONS)
def test_s49_a_close_with_a_run_in_flight_drops_the_rest_and_no_stroke_follows(reason: str) -> None:
    rig = review_rig()
    start_run(rig, "x" * 100)
    assert run_until(rig, lambda: len(rig.desktop.key_calls) >= 10)
    rig.session.close(reason)  # type: ignore[arg-type]
    sent = len(rig.desktop.key_calls)
    assert rig.session.discarded == 100 - sent  # what the window already has is not discarded
    rig.run(1.0)
    assert len(rig.desktop.key_calls) == sent and rig.closed == reason and rig.box == ""
    assert rig.summaries == []  # a close ends the run without a summary: the sink goes with the session


def test_s49_the_close_key_twice_closes_and_counts_the_box_a_single_tap_does_not() -> None:
    rig = review_rig()
    rig.type_text("hello")
    t = rig.t + 0.1
    rig.tap(t, "close")
    rig.run(0.5)
    assert rig.closed is None and "5 characters" in rig.view.strip
    rig.tap(rig.t + 0.1, "close")
    rig.run(0.5)
    assert rig.closed == "close_key" and rig.session.discarded == 5 and rig.desktop.key_calls == []


def test_s49_the_close_key_in_the_middle_of_a_run_is_busy_and_does_not_close() -> None:
    rig = review_rig()
    start_run(rig, "x" * 60)
    for _ in range(2):
        rig.tap(rig.t + 0.1, "close")
        rig.run(0.3)
    finish_run(rig)
    assert rig.closed is None and rig.typed == "x" * 60 and rig.counts["busy"] == 2


def test_s49_both_fists_in_the_middle_of_a_run_close_it_with_fists_and_count_the_rest() -> None:
    rig = review_rig()
    start_run(rig, "x" * 200)
    rig.run(0.3)
    sent = len(rig.desktop.key_calls)
    assert 0 < sent < 200
    rig.run(1.5, fist=True)
    assert rig.closed == "fists" and rig.session.discarded == 200 - len(rig.desktop.key_calls)
    assert len(rig.desktop.key_calls) < 200 and rig.box == ""
    after_close = len(rig.desktop.key_calls)
    rig.run(1.0)
    assert len(rig.desktop.key_calls) == after_close


def test_s49_hands_gone_for_two_minutes_with_a_box_close_idle_and_count_it() -> None:
    rig = review_rig(idle_s=30, fps=10.0)
    rig.type_text("hello")
    rig.empty(115.0)
    assert rig.closed is None and rig.box == "hello"
    rig.empty(10.0)
    assert rig.closed == "idle" and rig.session.discarded == 5 and rig.desktop.key_calls == []


def test_s49_the_last_twenty_seconds_before_an_idle_close_that_would_throw_a_box_away_are_counted_down() -> None:
    rig = review_rig(idle_s=30, fps=10.0, keep_views=False)
    rig.type_text("hello")
    rig.empty(99.0)
    assert "Closing in" not in rig.view.strip
    rig.empty(2.5)  # 101.5 s of 120 s gone: 18.5 s left reads as 19
    assert rig.view.strip == "Closing in 19 s: no hands in view. The box will be thrown away."
    rig.empty(10.0)
    assert rig.view.strip.startswith("Closing in 9 s")
    rig.run(0.5)  # the hands are back: no countdown, and the clock starts again
    assert "Closing in" not in rig.view.strip
    rig.empty(100.0)
    assert rig.closed is None
    rig.empty(21.0)
    assert rig.closed == "idle"


def test_s49_an_empty_box_has_nothing_to_throw_away_so_no_countdown() -> None:
    rig = review_rig(idle_s=30, fps=10.0, keep_views=False)
    rig.empty(25.0)
    assert "Closing in" not in rig.view.strip and rig.closed is None


def test_s49_an_empty_box_closes_idle_at_the_idle_setting() -> None:
    rig = review_rig(idle_s=30, fps=10.0)
    rig.empty(29.0)
    assert rig.closed is None
    rig.empty(2.0)
    assert rig.closed == "idle" and rig.session.discarded == 0


def test_s49_a_new_session_starts_with_an_empty_box() -> None:
    rig = review_rig()
    rig.type_text("hello")
    rig.session.close("command")
    fresh = review_rig()
    assert fresh.box == "" and fresh.session.discarded == 0 and fresh.desktop.key_calls == []


# -------------------------------------------------------------------------- S50: only a confirmed triple starts a run


def test_s50_start_text_has_exactly_one_call_site() -> None:
    tree = ast.parse(inspect.getsource(review_module))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "_start_text"
    ]
    assert len(calls) == 1
    owner = next(
        fn.name for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef) and any(c is calls[0] for c in ast.walk(fn))
    )
    assert owner == "_tap_insert"


def armed_insert_guard(rig: KbRig, text: str = "abc") -> float:
    """Two valid Insert taps on a box holding ``text``; returns the time of the first."""
    rig.fill(text)
    t0 = rig.t + 0.1
    rig.tap(t0, "insert")
    rig.tap(t0 + 0.8, "insert")
    rig.run(1.2)
    view = rig.view.compose
    assert view is not None and (view.guard, view.guard_taps) == ("insert", 2)
    return t0


def tap_key(name: str) -> Callable[[KbRig], None]:
    return lambda rig: rig.tap(rig.t + 0.05, name)


#: What clears an armed guard (N47): another key of any kind, a hold other than slow, the machine's own disarm, a
#: session event that changes what the taps mean.
CLEARING = {
    "shift": tap_key("shift"),
    "lang": tap_key("lang"),
    "home": tap_key("home"),
    "private_key": tap_key("private"),
    "chip": tap_key("chip"),
    "letter": tap_key("a"),
    "space": tap_key("space"),
    "backspace": tap_key("backspace"),
    "clear": tap_key("clear"),
    "foreign_input": lambda rig: rig.desktop.user_typed(),
    "modifier": lambda rig: setattr(rig.desktop, "modifiers", True),
    "window_change": lambda rig: with_target(rig, hwnd=555),
    "covered": lambda rig: with_target(rig, covered=True),
    "overlay": lambda rig: setattr(rig, "overlay_ok", False),
    "disarm": lambda rig: machine(rig).disarm(),
    "set_private": lambda rig: rig.session.set_private(True),
    "recenter": lambda rig: rig.session.recenter(),
}


def settle(rig: KbRig) -> None:
    """Undo whatever a clearing event left behind and wait out its hold (every hold is over within 2.5 s)."""
    rig.overlay_ok = True
    rig.desktop.modifiers = False
    rig.run(0.3)
    rig.desktop.target = dataclasses.replace(rig.desktop.target, covered=False)
    rig.run(2.2)


@pytest.mark.parametrize("name", CLEARING)
def test_n47_an_event_between_the_taps_clears_the_guard_and_the_third_tap_starts_nothing(name: str) -> None:
    rig = review_rig()
    armed_insert_guard(rig)
    CLEARING[name](rig)
    rig.run(0.2)
    view = rig.view.compose
    assert view is not None and view.guard != "insert", name  # Clear starts a guard of its own
    settle(rig)
    if name == "recenter":
        rig.place()
        rig.arm()
    box = rig.box
    rig.tap(rig.t + 0.1, "insert")
    rig.run(1.0)
    assert rig.desktop.key_calls == [] and rig.session.review_state == "composing", name
    assert rig.box == box


def slow_camera(rig: KbRig, seconds: float, fps: float = 6.0) -> None:
    """Frames at ``fps`` for ``seconds`` (the median interval over 1.0 s above 0.10 s sets the hold ``slow``)."""
    keep = rig.fps, rig.dt
    rig.fps, rig.dt = fps, 1 / fps
    rig.run(seconds)
    rig.fps, rig.dt = keep


def test_n47_a_slow_hold_clears_a_guard_like_every_hold_and_drops_the_taps_it_covers() -> None:
    rig = review_rig()
    armed_insert_guard(rig)
    slow_camera(rig, 1.5)
    rig.tap(rig.t + 0.1, "insert")  # while slow: dropped, not counted
    slow_camera(rig, 0.5)
    rig.run(1.0)
    assert rig.desktop.key_calls == [] and rig.counts["held"] >= 1
    view = rig.view.compose
    assert view is not None and view.guard is None
    rig.tap(rig.t + 0.1, "insert")
    rig.run(1.0)
    assert rig.desktop.key_calls == [] and rig.session.review_state == "composing"


def test_n47_a_slow_hold_does_not_take_the_send_opportunity_away_but_any_other_hold_does() -> None:
    for name, hold in (("slow", lambda rig: slow_camera(rig, 2.0)), ("yield", lambda rig: rig.desktop.user_typed())):
        rig = review_rig()
        insert_text(rig, "hello")
        hold(rig)
        rig.run(2.0)
        t = rig.t + 0.1
        for i in range(3):
            rig.tap(t + 1.3 * i, "enter")
        rig.run(3.0)
        sent_enter = ("control", "enter") in rig.desktop.key_calls
        assert sent_enter == (name == "slow"), name


HOLDS_DURING_SECOND_TAP = {
    "foreign_input": lambda rig: rig.desktop.user_typed(),
    "modifier": lambda rig: setattr(rig.desktop, "modifiers", True),
    "window_change": lambda rig: with_target(rig, hwnd=555),
    "password": lambda rig: with_target(rig, password=True),
}


@pytest.mark.parametrize("name", HOLDS_DURING_SECOND_TAP)
@pytest.mark.parametrize("third_in", [1.0, 3.0])
def test_n42_a_hold_set_during_the_second_tap_leaves_no_run(name: str, third_in: float) -> None:
    rig = review_rig()
    rig.fill("abc")
    t0 = rig.t + 0.1
    rig.tap(t0, "insert")
    rig.tap(t0 + 1.0, "insert", before=lambda: HOLDS_DURING_SECOND_TAP[name](rig))
    rig.tap(t0 + 1.0 + third_in, "insert")
    rig.run(1.0 + third_in + 1.0)
    assert rig.desktop.key_calls == [] and rig.session.review_state == "composing"


def test_n43_three_insert_taps_0_2_s_apart_are_bounces_and_start_nothing() -> None:
    rig = review_rig()
    rig.fill("abc")
    t0 = rig.t + 0.1
    for i in range(3):
        rig.tap(t0 + i * 0.2, "insert")
    rig.run(2.0)
    assert rig.desktop.key_calls == [] and rig.session.review_state == "composing"
    view = rig.view.compose
    assert view is not None and view.guard_taps == 2 and rig.counts["bounce"] == 1  # the middle tap bounced


@pytest.mark.parametrize("box", ["", " ", "   ", "  a"[:2]])
def test_n44_an_empty_or_blank_box_starts_no_run_however_often_insert_is_tapped(box: str) -> None:
    rig = review_rig()
    rig.fill(box)
    t0 = rig.t + 0.1
    for i in range(6):
        rig.tap(t0 + i * 1.0, "insert")
    rig.run(7.0)
    assert rig.desktop.key_calls == [] and rig.box == box and rig.summaries == []


@pytest.mark.parametrize(
    "name", ["lang", "shift", "home", "private_key", "set_private", "recenter", "hold", "hands_back"]
)
def test_s50_lang_shift_home_priv_hold_end_hands_returning_and_recenter_never_start_a_run(name: str) -> None:
    rig = review_rig()
    rig.fill("abc")
    t0 = rig.t + 0.1
    rig.tap(t0, "insert")
    rig.tap(t0 + 0.8, "insert")
    rig.run(1.2)
    events = {
        "lang": tap_key("lang"),
        "shift": tap_key("shift"),
        "home": tap_key("home"),
        "private_key": tap_key("private"),
        "set_private": lambda rig: rig.session.set_private(True),
        "recenter": lambda rig: rig.session.recenter(),
        "hold": lambda rig: rig.desktop.user_typed(),
        "hands_back": lambda rig: rig.empty(1.0),
    }
    events[name](rig)
    rig.run(4.0)
    assert rig.desktop.key_calls == [] and rig.session.review_state == "composing"
    if name != "hands_back":
        rig.session.set_private(False)
        rig.run(1.0)
        assert rig.desktop.key_calls == []


# ------------------------------------------------------------------------------------------------ S51: the Send key


def enters(rig: KbRig) -> int:
    return rig.desktop.key_calls.count(("control", "enter"))


def inserted(rig: KbRig, text: str = "hello") -> float:
    """A finished text run; returns the time of the completed Insert (the Send window starts there)."""
    insert_text(rig, text)
    last = machine(rig).last_insert
    assert last is not None
    return last.t


SEND_CASES = {
    "one_tap": ([0.5], False),
    "two_taps": ([0.5, 1.5], False),
    "three_taps_0_2_apart": ([0.5, 0.7, 0.9], False),  # the middle one bounces: two are counted
    "three_taps_0_8_apart": ([0.5, 1.3, 2.1], True),
    "third_tap_5_8_s_after_the_first": ([0.5, 3.4, 6.3], True),
    "third_tap_6_2_s_after_the_first": ([0.5, 3.6, 6.7], False),
    "third_tap_9_8_s_after_the_insert": ([4.5, 7.5, 9.8], True),
    "third_tap_10_2_s_after_the_insert": ([4.5, 7.5, 10.2], False),
    "after_11_s": ([11.0, 12.0, 13.0], False),
}


@pytest.mark.parametrize("name", SEND_CASES)
def test_s51_only_the_valid_triple_sends_one_enter(name: str) -> None:
    taps, expected = SEND_CASES[name]
    rig = review_rig()
    t_done = inserted(rig)
    for off in taps:
        rig.tap(t_done + off, "enter")
    rig.run(taps[-1] + 2.0)
    assert enters(rig) == int(expected), name
    assert rig.typed == "hello" + ("\n" if expected else "")
    if expected:
        assert rig.desktop.key_batches[-1] == [(0x0D, 0x1C, 0), (0x0D, 0x1C, 2)]
        assert [(s.kind, s.outcome) for s in rig.summaries] == [("text", "done"), ("enter", "done")]


SEND_INTERRUPTIONS = {
    "a_letter_between_the_taps": lambda rig: rig.tap(rig.t + 0.05, "a"),
    "a_chip_between_the_taps": lambda rig: rig.tap(rig.t + 0.05, "chip"),
    "backspace_between_the_taps": lambda rig: rig.tap(rig.t + 0.05, "backspace"),
    "shift_between_the_taps": lambda rig: rig.tap(rig.t + 0.05, "shift"),
    "a_window_change_between_the_taps": lambda rig: with_target(rig, hwnd=555),
    "foreign_input_between_the_taps": lambda rig: rig.desktop.user_typed(),
}


@pytest.mark.parametrize("name", SEND_INTERRUPTIONS)
def test_s51_anything_between_the_taps_takes_the_send_away(name: str) -> None:
    rig = review_rig()
    t_done = inserted(rig)
    rig.tap(t_done + 0.5, "enter")
    rig.run(0.9)
    SEND_INTERRUPTIONS[name](rig)
    rig.run(2.5)  # past every hold
    for i in range(3):
        rig.tap(rig.t + 0.1 + 1.0 * i, "enter")
    rig.run(3.5)
    assert enters(rig) == 0, name


def test_s51_a_chip_between_the_insert_and_the_first_send_tap_takes_the_send_away() -> None:
    rig = review_rig()
    t_done = inserted(rig)
    rig.tap(t_done + 0.3, "chip")
    for i in range(3):
        rig.tap(t_done + 1.0 + i, "enter")
    rig.run(5.0)
    assert enters(rig) == 0


def test_s51_a_letter_after_the_insert_is_an_edit_and_send_is_refused() -> None:
    rig = review_rig()
    t_done = inserted(rig)
    rig.tap(t_done + 0.3, "a")
    for i in range(3):
        rig.tap(t_done + 1.0 + i, "enter")
    rig.run(5.0)
    assert enters(rig) == 0 and rig.box == "a"


def test_s51_a_window_change_between_the_insert_and_the_send_refuses_it_at_the_sink() -> None:
    rig = review_rig()
    t_done = inserted(rig)
    original = rig.desktop.target
    with_target(rig, hwnd=555, pid=666)
    rig.run(1.0)
    for i in range(3):
        rig.tap(rig.t + 0.1 + i, "enter")
    rig.run(4.0)
    rig.desktop.target = original
    rig.run(1.5)
    for i in range(3):
        rig.tap(rig.t + 0.1 + i, "enter")
    rig.run(4.0)
    assert enters(rig) == 0 and rig.t - t_done < 20.0


def test_s51_a_text_starting_with_a_slash_is_never_sent() -> None:
    rig = review_rig()
    t_done = inserted(rig, "/mo")
    for i in range(3):
        rig.tap(t_done + 0.5 + i, "enter")
    rig.run(5.0)
    assert enters(rig) == 0 and machine(rig).prefix_risk
    assert rig.counts["refused_send_prefix"] >= 1


def test_s51_enter_off_removes_send() -> None:
    rig = review_rig(enter="off")
    t_done = inserted(rig)
    for i in range(3):
        rig.tap(t_done + 0.5 + i, "enter")
    rig.run(5.0)
    assert enters(rig) == 0 and rig.counts["refused_enter_off"] >= 1


def test_s51_a_second_send_is_refused_the_opportunity_is_single_shot() -> None:
    rig = review_rig()
    t_done = inserted(rig)
    for i in range(3):
        rig.tap(t_done + 0.5 + 1.0 * i, "enter")
    rig.run(4.0)
    assert enters(rig) == 1
    for i in range(3):
        rig.tap(rig.t + 0.5 + 1.0 * i, "enter")
    rig.run(5.0)
    assert enters(rig) == 1


def test_n45_send_without_an_insert_does_nothing_twice_or_ten_times() -> None:
    rig = review_rig()
    rig.type_text("abc")
    for k in range(10):
        rig.tap(rig.t + 0.3 + 0.8 * k, "enter")
    rig.run(10.0)
    assert rig.desktop.key_calls == [] and rig.box == "abc" and rig.counts["refused_send_none"] >= 1
    rig2 = review_rig()
    for k in range(10):
        rig2.tap(rig2.t + 0.3 + 0.8 * k, "enter")
    rig2.run(10.0)
    assert rig2.desktop.key_calls == [] and rig2.summaries == []


def test_n46_send_after_a_slash_clear_or_a_prefix_risk_session_is_refused_for_the_rest_of_the_session() -> None:
    rig = review_rig()
    t_done = inserted(rig, "/clear")
    for i in range(3):
        rig.tap(t_done + 0.5 + i, "enter")
    rig.run(4.0)
    assert enters(rig) == 0
    # an ordinary text after it is typed and still never sent: the session remembers a text that began with "/"
    rig.run(2.0)
    t_done = inserted(rig, "hello")
    for i in range(3):
        rig.tap(t_done + 0.5 + i, "enter")
    rig.run(4.0)
    assert enters(rig) == 0 and machine(rig).prefix_risk


def test_n46_a_text_run_that_began_with_a_slash_and_was_aborted_also_sets_prefix_risk() -> None:
    rig = review_rig()
    after(rig, 2, rig.desktop.user_typed)
    start_run(rig, "/hello")
    finish_run(rig)
    assert rig.summaries[0].outcome == "aborted" and machine(rig).prefix_risk
    rig.run(3.0)
    machine(rig).buffer.clear(rig.t)  # the remainder goes; a plain text afterwards does not lift the flag
    rig.run(0.5)
    t_done = inserted(rig, "plain")
    for i in range(3):
        rig.tap(t_done + 0.5 + i, "enter")
    rig.run(4.0)
    assert enters(rig) == 0


def test_n46_a_new_session_clears_prefix_risk() -> None:
    rig = review_rig()
    inserted(rig, "/x")
    assert machine(rig).prefix_risk
    fresh = review_rig()
    assert not machine(fresh).prefix_risk
    t_done = inserted(fresh)
    for i in range(3):
        fresh.tap(t_done + 0.5 + i, "enter")
    fresh.run(4.0)
    assert enters(fresh) == 1


# ---------------------------------------------------------------------------------------------- S56: the private box


@pytest.mark.parametrize("how", ["priv_key", "command"])
def test_s56_a_private_box_shows_bullets_only_while_composing_armed_and_inserting(how: str) -> None:
    rig = review_rig()
    rig.type_text("zzqxjv")
    if how == "priv_key":
        rig.tap(rig.t + 0.05, "private")
        rig.run(0.3)
    else:
        rig.session.set_private(True)
        rig.run(0.1)
    view = rig.view
    assert view.private and view.compose is not None
    assert view.compose.text == "•" * 6 and view.compose.length == 6
    assert "zzqxjv" not in view.strip and "6" in view.strip  # counts, never the text
    t0 = rig.t + 0.1
    rig.tap(t0, "insert")
    rig.tap(t0 + 0.8, "insert")
    rig.run(1.2)
    armed = rig.view.compose
    assert armed is not None and armed.guard == "insert" and armed.text == "•" * 6
    rig.tap(rig.t + 0.1, "insert")
    assert run_until(rig, lambda: rig.session.review_state == "inserting")
    seen: list[tuple[str, int]] = []
    while rig.session.review_state == "inserting":
        compose = rig.view.compose
        assert compose is not None
        seen.append((compose.text, compose.sent))
        assert "zzqxjv" not in rig.view.strip
        rig.run(rig.dt)
    assert rig.typed == "zzqxjv" and seen and all(text == "•" * 6 for text, _ in seen)


# ---------------------------------------------------------------------------------------------- S57: the storm


LETTERS = "qwertyuiopasdfghjklzxcvbnm"


def test_s57_thirty_taps_at_ten_a_second_freeze_once_keep_eleven_characters_and_close_nothing() -> None:
    rig = review_rig()
    resets = rig.press.resets
    t0 = rig.t + 0.1
    for i in range(30):
        rig.tap(t0 + 0.1 * i, LETTERS[i % 26])
    rig.run(3.4)  # the burst ends at t0 + 2.9; the freeze runs to t0 + 4.1
    assert rig.counts["storm_freeze"] == 1 and rig.closed is None
    assert len(rig.box) == 11 and rig.desktop.key_calls == []
    assert rig.press.resets > resets  # the fingers were re-latched
    assert rig.view.strip == "Too many taps at once. Paused for 3 s."
    rig.run(1.0)
    assert len(rig.box) == 11
    rig.tap(rig.t + 0.1, "a")
    rig.run(0.5)
    assert len(rig.box) == 12 and rig.counts["storm_freeze"] == 1


def test_s57_a_hint_does_not_take_the_strip_from_the_storm_sentence_while_the_freeze_lasts() -> None:
    """The storm sentence is the only thing that tells the user why nothing is typed; a posture or weak-tap hint that
    came due in the burst waits until the freeze is over (found end to end: ``Ring: tap a bit firmer`` over it)."""
    rig = review_rig()
    rig.press.view_states[("right", 1)] = ("open", 0.0, "weak")  # the note is there 1.5 s into the burst
    t0 = rig.t + 0.1
    for i in range(12):
        rig.tap(t0 + 0.1 * i, LETTERS[i])
    froze = rig.t
    while rig.counts["storm_freeze"] == 0 and rig.t < froze + 4.0:
        rig.run(0.1)
    assert rig.counts["storm_freeze"] == 1
    began = rig.t
    strips = []
    while rig.t < began + 2.9:
        rig.run(0.1)
        strips.append(rig.view.strip)
    assert strips and set(strips) == {"Too many taps at once. Paused for 3 s."}
    rig.run(1.0)
    assert rig.view.strip != "Too many taps at once. Paused for 3 s."


def test_s57_the_freeze_lasts_three_seconds_from_the_twelfth_tap() -> None:
    rig = review_rig()
    t0 = rig.t + 0.1
    for i in range(12):
        rig.tap(t0 + 0.1 * i, LETTERS[i])
    rig.run(1.5)
    frozen_at = rig.t
    assert rig.counts["storm_freeze"] == 1 and len(rig.box) == 11
    rig.tap(frozen_at + 1.0, "a")
    rig.tap(frozen_at + 2.0, "b")
    rig.run(2.2)
    assert len(rig.box) == 11  # both taps fell inside the 3 s
    rig.tap(rig.t + 1.2, "c")
    rig.run(1.6)
    assert len(rig.box) == 12
    assert STORM_FREEZE_S == 3.0


# ---------------------------------------------------------------------------------------------- S59: the slow hold


def test_s59_a_slow_camera_mid_run_does_not_stop_the_run() -> None:
    rig = review_rig()
    start_run(rig, "a" * 60)
    slow_camera(rig, 3.0, fps=8.0)  # a camera below 10 fps for most of the run
    assert rig.session.review_state == "inserting" or rig.summaries
    finish_run(rig, limit_s=30.0)
    assert rig.summaries[0].outcome == "done" and rig.typed == "a" * 60


def test_s59_during_composing_slow_drops_the_taps() -> None:
    rig = review_rig()
    rig.run(0.2)
    slow_camera(rig, 1.5, fps=6.0)
    before = rig.counts["held"]
    rig.fps, rig.dt = 6.0, 1 / 6.0
    rig.type_text("abc", gap_s=0.6)
    rig.fps, rig.dt = 30.0, 1 / 30.0
    assert rig.box == "" and rig.counts["held"] > before
    rig.run(2.0)  # fast again: the hold clears and taps are taken
    rig.type_text("abc")
    assert rig.box == "abc"


# -------------------------------------------------------------------------------------------- B40, B41: the pace


@pytest.mark.parametrize(("fps", "duration"), [(15.0, 13.3), (30.0, 6.6), (60.0, 6.6)])
def test_b40_a_full_box_is_typed_at_the_pace_of_the_frames_never_more_than_one_per_frame(
    fps: float, duration: float
) -> None:
    rig = review_rig(fps=fps, keep_views=False)
    text = "".join(LETTERS[i % 26] for i in range(COMPOSE_MAX))
    start_run(rig, text)
    finish_run(rig, limit_s=20.0)
    assert rig.typed == text and rig.summaries[0].outcome == "done"
    times = [r.t for r in rig.records if r.steps]
    assert len(times) == COMPOSE_MAX and all(r.steps <= 1 for r in rig.records)
    assert times[-1] - times[0] == pytest.approx(duration, abs=0.3 if fps == 15.0 else 0.2)
    gaps = [b - a for a, b in pairwise(times)]
    assert min(gaps) >= 0.030 - 1e-9


def test_b41_at_three_frames_a_second_a_long_box_times_out_with_a_remainder() -> None:
    rig = review_rig(keep_views=False)
    start_run(rig, "a" * COMPOSE_MAX)
    slow_camera(rig, 35.0, fps=3.0)
    summary = rig.summaries[0]
    assert (summary.outcome, summary.reason) == ("aborted", "timeout")
    assert 80 <= summary.sent <= 100 and len(rig.box) == COMPOSE_MAX - summary.sent
    assert rig.typed == "a" * summary.sent


def test_b41_at_seven_frames_a_second_the_whole_box_is_typed_within_thirty_seconds() -> None:
    rig = review_rig(keep_views=False)
    start_run(rig, "a" * COMPOSE_MAX)
    begun = rig.t
    slow_camera(rig, 29.0, fps=7.0)
    summary = rig.summaries[0]
    assert (summary.outcome, summary.sent) == ("done", COMPOSE_MAX) and rig.typed == "a" * COMPOSE_MAX
    assert next(r.t for r in reversed(rig.records) if r.steps) - begun < 30.0


# ------------------------------------------------------------------------------------------------ S40: nothing types


def test_s40_ten_minutes_of_every_tap_of_every_key_except_a_valid_insert_triple_type_nothing() -> None:
    rng = random.Random(7)
    rig = review_rig(fps=15.0, keep_views=False)
    keys = [k for k in rig.layout.keys if k.kind != "close"]
    guarded = {"insert", "clear", "enter", "close"}
    last_kind = ""
    end = rig.t + 600.0
    taps = 0
    while rig.t < end and rig.closed is None:
        key = rng.choice(keys)
        if key.kind in guarded and key.kind == last_kind:
            key = rng.choice(
                [k for k in keys if k.kind == "char"]
            )  # two taps of a guarded key in a row are not "except"
        if key.kind == "char" and rng.random() < 0.02:
            key = rig.key("close") if rig.box else key
        if key.kind == "close" and last_kind == "close":
            key = rng.choice([k for k in keys if k.kind == "char"])
        last_kind = key.kind
        rig.tap(rig.t + rig.dt, key)
        rig.run(rng.uniform(0.3, 1.6))
        taps += 1
    assert rig.closed is None and taps > 400
    assert rig.desktop.key_calls == [] and rig.steps == [] and rig.answers == []
    assert machine(rig).counts["insert_start"] == 0 and rig.sink is not None and not rig.sink.run_active


def test_s40_a_valid_triple_in_the_same_stream_is_the_only_thing_that_types() -> None:
    rig = review_rig()
    rng = random.Random(41)
    for _ in range(36):
        rig.tap(rig.t + rig.dt, rng.choice("abcdefgh"))
        rig.run(rng.uniform(0.3, 0.9))
    typed_box = rig.box
    assert rig.desktop.key_calls == [] and typed_box
    rig.insert(rig.t + 0.1)
    finish_run(rig)
    assert rig.typed == typed_box


# ---------------------------------------------------------------------------------------------------- S58 and touches


def test_s58_air_with_the_direct_commit_is_refused_with_the_exact_text() -> None:
    with pytest.raises(ValueError, match="only works with the review box") as err:
        KeyboardSession(
            press=ScriptedPress("air"),
            tuning=Tuning(),
            idle_s=30,
            enter="twice",
            lang="en",
            mode="live",
            aspect=ASPECT,
            start_t=0.0,
            commit="direct",
        )
    assert str(err.value) == "The air-tap method only works with the review box (commit: review)."


def test_a_review_box_keeps_the_touch_of_each_character_with_its_confidence() -> None:
    rig = review_rig()
    rig.tap(rig.t + 0.1, "h", du=0.2, dv=-0.1)
    rig.tap(rig.t + 0.6, "space")
    rig.tap(rig.t + 1.1, "i", conf=0.7)
    rig.tap(rig.t + 1.6, "backspace")
    rig.run(2.0)
    assert rig.box == "h "
    touches = machine(rig).buffer.touches()
    assert len(touches) == 2 and all(touch is not None for touch in touches)
    first, second = touches
    assert first.conf == pytest.approx(0.9) and second.conf == pytest.approx(0.9)
    key = rig.key("h")
    assert first.u == pytest.approx(key.col + key.width / 2 + 0.2, abs=0.05)
    assert first.v == pytest.approx(key.row + 0.5 - 0.1, abs=0.05)
    assert (first.side, first.finger) == rig.finger_of(key)


def test_a_pinch_press_touch_has_confidence_one_and_a_direct_session_has_no_box_to_keep_one() -> None:
    rig = review_rig(press="pinch")
    rig.tap(rig.t + 0.1, "h")
    rig.run(0.5)
    (touch,) = machine(rig).buffer.touches()
    assert touch is not None and touch.conf == 1.0
    direct = KbRig(commit="direct")
    assert direct.session._machine is None
