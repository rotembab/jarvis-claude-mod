"""The key sink (DESIGN 3.6): the gate, the key lane, the run lane and the budgets, over a fake desktop.

S2, S4, S8 to S15 and S10 at the sink; S43 to S48, S47b, S47c and S3's sink half. The sink is the independent last gate:
nothing here involves a session, a machine or a camera. Every time is an argument or the injected clock.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from typing import Literal

import pytest

from jarvis_hands.desktop.base import KeyTarget
from jarvis_hands.desktop.fake import FakeDesktop, events_balanced
from jarvis_hands.desktop.keys import ALLOWED_CHARS, KeyRefused, KeyStroke
from jarvis_hands.keyboard import limits
from jarvis_hands.keyboard.sink import KeySink, SinkFailed
from jarvis_hands.keyboard.types import Commit

SENTINEL = "zzqxjv"
OK = KeyTarget(100, 200, "FakeTerminal", 0x0409, None, False, False)


def char(c: str) -> KeyStroke:
    return KeyStroke("char", c)


SPACE = KeyStroke("control", "space")
ENTER = KeyStroke("control", "enter")
BACKSPACE = KeyStroke("control", "backspace")


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class Env:
    """A sink over a fake desktop with a clock, started and past its warm-up window."""

    def __init__(self, commit: Commit = "review", *, inject: Literal["unicode", "vk"] = "unicode") -> None:
        self.desktop = FakeDesktop()
        self.clock = Clock()
        self.sink = KeySink(self.desktop, inject=inject, clock=self.clock, commit=commit)
        self.now = 0.0
        self.sink.start(self.now)
        self.advance(limits.SINK_WARMUP_S + 0.2)

    def advance(self, dt: float) -> float:
        self.now += dt
        self.clock.t = self.now
        return self.now

    def gate(self, **kw: bool) -> object:
        return self.sink.gate(self.now, **kw)

    def frame(self, dt: float = 1 / 30, **kw: bool) -> object:
        self.advance(dt)
        return self.gate(**kw)

    def begin(self, total: int = 5, *, again: bool = False) -> bool:
        self.gate()
        return self.sink.begin_run(self.now, total=total, again=again)

    def run(self, text: str) -> list[str]:
        """Begins a run for ``text`` and sends it one character per frame; returns the answers."""
        assert self.begin(len(text))
        answers: list[str] = []
        for c in text:
            self.advance(1 / 30)
            self.gate()
            answers.append(self.sink.send_run(SPACE if c == " " else char(c), self.now))
        return answers


# ------------------------------------------------------------------------------------------------------------- gate


def test_a_quiet_desktop_has_no_hold() -> None:
    env = Env()
    assert env.gate() is None
    assert env.sink.target_name == "FakeTerminal"


def test_s8_foreign_input_holds_yield_for_a_second_and_a_half() -> None:
    env = Env()
    env.desktop.user_typed()
    assert env.frame() == "yield"
    env.advance(limits.YIELD_S - 0.1)
    assert env.gate() == "yield"
    env.advance(0.2)
    assert env.gate() is None


def test_foreign_input_during_the_warm_up_window_is_only_a_baseline() -> None:
    desktop = FakeDesktop()
    sink = KeySink(desktop, inject="unicode", clock=Clock(), commit="review")
    desktop.user_typed()  # before start: the baseline swallows it
    sink.start(10.0)
    assert sink.gate(10.1) is None
    desktop.user_moved_mouse()  # the pointer hand-over's button releases
    assert sink.gate(10.2) is None  # inside the window: only a new baseline
    assert sink.gate(10.0 + limits.SINK_WARMUP_S + 0.05) is None  # and it is not remembered afterwards
    desktop.user_typed()
    assert sink.gate(10.0 + limits.SINK_WARMUP_S + 0.1) == "yield"


def test_a_second_start_rebaselines_and_keeps_every_budget() -> None:
    env = Env()
    env.run("ab")
    env.sink.end_run(env.now, completed=True)
    before = (dict(env.sink.counts), len(env.sink._run_begins), len(env.sink._run_chars))
    env.desktop.user_typed()
    env.advance(0.05)
    env.sink.start(env.now)  # the ladder fallback re-arms the session
    env.advance(0.1)
    assert env.gate() is None  # the foreign event before start() is gone
    assert dict(env.sink.counts) == before[0]
    assert (len(env.sink._run_begins), len(env.sink._run_chars)) == before[1:]


def test_s9_a_physical_modifier_holds_yield_while_it_is_down() -> None:
    env = Env()
    env.desktop.modifiers = True
    for _ in range(5):
        assert env.frame(1.0) == "yield"
    env.desktop.modifiers = False
    assert env.frame() is None


def test_s10_a_probe_that_raises_is_foreign_input_and_ten_in_a_row_fail_the_sink() -> None:
    env = Env()

    def boom() -> bool:
        raise OSError(SENTINEL)

    env.desktop.foreign_input = boom  # type: ignore[method-assign]
    for _ in range(limits.FOREIGN_FAIL_CLOSE - 1):
        assert env.frame() == "yield"
    with pytest.raises(SinkFailed) as info:
        env.frame()
    assert SENTINEL not in str(info.value) and info.value.__context__ is None


def test_s10_a_good_answer_resets_the_streak() -> None:
    env = Env()
    answers = iter([OSError] * (limits.FOREIGN_FAIL_CLOSE - 1) + [False] + [OSError] * (limits.FOREIGN_FAIL_CLOSE - 1))

    def probe() -> bool:
        step = next(answers)
        if step is OSError:
            raise OSError
        return step

    env.desktop.foreign_input = probe  # type: ignore[method-assign]
    for _ in range(limits.FOREIGN_FAIL_CLOSE * 2 - 1):
        env.frame()  # never raises: the streak never reaches the limit


@pytest.mark.parametrize("blocked", ["elevated", "shell", "own", "none"])
def test_s11_a_blocked_window_holds_blocked(blocked: str) -> None:
    env = Env()
    env.desktop.target = KeyTarget(100, 200, "x", 0x0409, blocked, False, False)  # type: ignore[arg-type]
    assert env.frame(0.2) == "blocked"
    assert not env.begin()


def test_s12_a_password_edit_holds_password() -> None:
    env = Env()
    env.desktop.target = KeyTarget(100, 200, "x", 0x0409, None, True, False)
    assert env.frame(0.2) == "password"
    assert not env.begin() and env.sink.last_hold == "password"


def test_s13_a_covered_desktop_holds_covered() -> None:
    env = Env()
    env.desktop.target = KeyTarget(100, 200, "x", 0x0409, None, False, True)
    assert env.frame(0.2) == "covered"
    assert not env.begin() and env.sink.last_hold == "covered"


def test_the_holds_have_the_order_of_sr6() -> None:
    env = Env()
    env.desktop.target = KeyTarget(101, 200, "x", 0x0409, "shell", True, True)  # a new hwnd too: focus settle
    env.desktop.modifiers = True
    assert env.frame(0.2, overlay_ok=False) == "blocked"
    env.desktop.target = KeyTarget(101, 200, "x", 0x0409, None, True, True)
    assert env.frame(0.2, overlay_ok=False) == "password"
    env.desktop.target = KeyTarget(101, 200, "x", 0x0409, None, False, True)
    assert env.frame(0.2, overlay_ok=False) == "covered"
    env.desktop.target = KeyTarget(101, 200, "x", 0x0409, None, False, False)
    assert env.frame(0.2, overlay_ok=False) == "overlay"
    assert env.frame(0.2, overlay_ok=True) == "yield"  # the focus settle (0.5 s from 100 to 101) is over by now
    env.desktop.target = KeyTarget(102, 200, "x", 0x0409, None, False, False)
    assert env.frame(0.2) == "focus"  # a window change outranks the modifier that is still down
    env.desktop.modifiers = False
    assert env.frame(0.2) == "focus"
    assert env.frame(limits.FOCUS_SETTLE_S) is None


def test_s14_an_overlay_that_is_not_healthy_holds_overlay_and_recovers() -> None:
    env = Env()
    assert env.frame(overlay_ok=False) == "overlay"
    assert env.frame(overlay_ok=True) is None


def test_s15_a_window_change_settles_for_half_a_second() -> None:
    env = Env()
    env.desktop.target = KeyTarget(555, 200, "dialog", 0x0409, None, False, False)
    assert env.frame(0.2) == "focus"
    assert env.frame(0.4) == "focus"
    assert env.frame(0.2) is None


def test_the_target_is_read_every_hundred_milliseconds_not_every_frame() -> None:
    env = Env()
    reads: list[float] = []
    original = env.desktop.key_target

    def counting() -> KeyTarget:
        reads.append(env.now)
        return original()

    env.desktop.key_target = counting  # type: ignore[method-assign]
    for _ in range(10):
        env.frame(0.02)
    assert 1 <= len(reads) <= 3


def test_a_target_that_cannot_be_read_is_a_blocked_one() -> None:
    env = Env()

    def boom() -> KeyTarget:
        raise RuntimeError(SENTINEL)

    env.desktop.key_target = boom  # type: ignore[method-assign]
    assert env.frame(0.2) == "blocked"
    assert env.sink.counts["target_fail"] >= 1


def test_after_a_refused_send_the_gate_reports_blocked_for_a_second() -> None:
    env = Env("direct")
    env.gate()
    env.desktop.fail_keys = 1
    assert env.sink.send(char("a"), env.now) == "failed"
    assert env.frame(0.5) == "blocked"
    assert env.frame(0.4) == "blocked"
    assert env.frame(0.2) is None


# --------------------------------------------------------------------------------------------------------- key lane


def test_the_key_lane_sends_one_stroke_and_records_it_balanced() -> None:
    env = Env("direct")
    env.gate()
    assert env.sink.send(char("h"), env.now) == "sent"
    env.advance(0.1)
    env.gate()
    assert env.sink.send(SPACE, env.now) == "sent"
    env.advance(0.1)
    env.gate()
    assert env.sink.send(BACKSPACE, env.now) == "sent"
    assert env.desktop.key_calls == [("char", "h"), ("control", "space"), ("control", "backspace")]
    assert all(events_balanced(batch) for batch in env.desktop.key_batches)  # S4
    assert env.sink.counts["sent"] == 3


def test_the_key_lane_passes_the_injection_mode_to_the_desktop() -> None:
    env = Env("direct", inject="vk")
    seen: list[str] = []
    original = env.desktop.send_keys

    def spy(strokes: Sequence[KeyStroke], *, inject: Literal["unicode", "vk"] = "unicode") -> int:
        seen.append(inject)
        return original(strokes, inject=inject)

    env.desktop.send_keys = spy  # type: ignore[method-assign]
    env.gate()
    env.sink.send(char("a"), env.now)
    assert seen == ["vk"]


def test_s48_each_lane_is_dead_in_the_other_mode() -> None:
    review = Env("review")
    review.gate()
    assert review.sink.send(char("a"), review.now) == "failed"
    assert review.sink.runaway and review.sink.counts["lane_violation"] == 1
    assert review.desktop.key_calls == []
    direct = Env("direct")
    direct.gate()
    assert direct.sink.begin_run(direct.now, total=1) is False
    assert direct.sink.runaway and direct.sink.counts["lane_violation"] == 1
    assert direct.sink.send_run(char("a"), direct.now) == "failed"
    assert direct.desktop.key_calls == []


def test_a_hold_stops_the_key_lane_and_nothing_is_queued() -> None:
    env = Env("direct")
    env.desktop.target = KeyTarget(100, 200, "x", 0x0409, "elevated", False, False)
    env.advance(0.2)
    assert env.gate() == "blocked"
    assert env.sink.send(char("a"), env.now) == "hold"
    assert env.sink.last_hold == "blocked" and env.desktop.key_calls == []
    env.desktop.target = OK
    env.advance(0.2)
    assert env.gate() is None  # nothing was queued: the next frame types nothing by itself
    assert env.desktop.key_calls == []


def test_a_missing_gate_is_a_hold_in_the_key_lane_too() -> None:
    env = Env("direct")
    env.gate()
    assert env.sink.send(char("a"), env.now + 0.01) == "hold"  # gate() was not called with this time
    assert env.sink.counts["no_gate"] == 1 and env.desktop.key_calls == []


def test_s15_the_key_is_dropped_when_the_foreground_changed_between_resolving_and_sending() -> None:
    env = Env("direct")
    env.gate()
    env.desktop.target = KeyTarget(999, 200, "x", 0x0409, None, False, False)  # the window changed after the gate read
    assert env.sink.send(char("a"), env.now) == "hold"
    assert env.sink.last_hold == "focus" and env.desktop.key_calls == []
    env.desktop.target = OK
    assert env.frame(0.2) == "focus"  # the settle was started


@pytest.mark.parametrize("bad", ["a", 5, None, ("char", "a"), b"a", object()])
def test_a_stroke_that_is_not_a_stroke_never_reaches_the_desktop(bad: object) -> None:
    env = Env("direct")
    env.gate()
    assert env.sink.send(bad, env.now) == "failed"  # type: ignore[arg-type]
    assert env.desktop.key_calls == [] and env.sink.counts["bad_stroke"] == 1


@pytest.mark.parametrize(
    ("kind", "value"), [("char", "1"), ("char", "!"), ("char", "\n"), ("control", "tab"), ("control", "esc")]
)
def test_a_stroke_forced_past_the_constructor_is_refused_by_the_sink(kind: str, value: str) -> None:
    stroke = char("a")
    object.__setattr__(stroke, "kind", kind)
    object.__setattr__(stroke, "value", value)
    for env in (Env("direct"), Env("review")):
        env.gate()
        if env.sink._commit == "direct":
            assert env.sink.send(stroke, env.now) == "failed"
        else:
            assert env.begin(1)
            assert env.sink.send_run(stroke, env.now) == "failed"
        assert env.desktop.key_calls == []


def test_s2_thirty_strokes_in_a_frame_stop_at_the_backstop_and_set_runaway() -> None:
    env = Env("direct")
    env.gate()
    results = [env.sink.send(char("a"), env.now) for _ in range(30)]
    assert results.count("sent") == limits.BACKSTOP_N - 1 and results.count("limited") == 30 - (limits.BACKSTOP_N - 1)
    assert len(env.desktop.key_calls) <= limits.BACKSTOP_N - 1
    assert env.sink.runaway


def test_the_key_lane_backstop_is_a_sliding_window() -> None:
    env = Env("direct")
    for i in range(60):  # 15 sends in each 2.1 s window: never 16 within 2 s
        env.advance(0.14)
        env.gate()
        assert env.sink.send(char("a"), env.now) == "sent", i
    assert not env.sink.runaway


def test_s47_three_refusals_in_a_row_fail_the_sink_and_a_success_resets_the_streak() -> None:
    env = Env("direct")
    for _ in range(2):
        env.advance(1.2)
        env.gate()
        env.desktop.fail_keys = 1
        assert env.sink.send(char("a"), env.now) == "failed"
    env.advance(1.2)
    env.gate()
    assert env.sink.send(char("a"), env.now) == "sent"  # reset
    for _ in range(2):
        env.advance(1.2)
        env.gate()
        env.desktop.fail_keys = 1
        assert env.sink.send(char("a"), env.now) == "failed"
    env.advance(1.2)
    env.gate()
    env.desktop.fail_keys = 1
    with pytest.raises(SinkFailed):
        env.sink.send(char("a"), env.now)


def test_a_partly_taken_batch_is_a_failure_of_the_key_lane() -> None:
    env = Env("direct")
    env.gate()
    env.desktop.partial_keys = 1
    assert env.sink.send(char("a"), env.now) == "failed"
    assert env.sink.counts["failed"] == 1


@pytest.mark.parametrize("lane", ["direct", "review"])
def test_s47c_slow_successes_count_as_failures_without_resetting_the_streak(lane: Commit) -> None:
    env = Env(lane)

    def slow_once(count: int) -> None:
        env.clock.t += limits.SEND_SLOW_S + 0.05

    def one(slow: bool) -> str:
        env.advance(1 / 30)
        env.gate()
        env.desktop.after_key = slow_once if slow else None
        if lane == "direct":
            return env.sink.send(char("a"), env.now)
        return env.sink.send_run(char("a"), env.now)

    if lane == "review":
        env.advance(1.0)
        assert env.begin(60)
    # slow, slow, fast, slow, slow: the fast one resets, so no SinkFailed
    assert [one(s) for s in (True, True, False, True, True)] == ["sent"] * 5
    # slow, slow, slow with a reset first: SinkFailed on the third
    assert one(False) == "sent"
    assert one(True) == "sent" and one(True) == "sent"
    with pytest.raises(SinkFailed):
        one(True)
    assert env.sink.counts["slow_send"] >= 3


def test_s47c_slow_a_blocked_slow_is_the_third_failure() -> None:
    env = Env("direct")

    def slow(count: int) -> None:
        env.clock.t += limits.SEND_SLOW_S + 0.05

    env.gate()
    env.desktop.after_key = slow
    assert env.sink.send(char("a"), env.now) == "sent"
    env.advance(1.2)
    env.gate()
    env.desktop.after_key = None
    env.desktop.fail_keys = 1
    assert env.sink.send(char("a"), env.now) == "failed"
    env.advance(1.2)
    env.gate()
    env.desktop.after_key = slow
    with pytest.raises(SinkFailed):
        env.sink.send(char("a"), env.now)


def test_nothing_the_desktop_raises_is_kept() -> None:
    env = Env("direct")
    env.gate()
    env.desktop.raise_after = RuntimeError(SENTINEL)
    assert env.sink.send(char("a"), env.now) == "failed"
    pieces = [repr(env.sink), repr(env.sink.counts), repr(env.sink.last_hold)]
    for _ in range(3):
        env.advance(1.2)
        env.gate()
        env.desktop.raise_after = ValueError(SENTINEL)
        try:
            env.sink.send(char("a"), env.now)
        except SinkFailed as exc:
            pieces += [str(exc), repr(exc.__context__), repr(exc.__cause__)]
    assert all(SENTINEL not in p for p in pieces)


# ----------------------------------------------------------------------------------------------------- run lane: begin


def test_begin_run_pins_the_window_and_returns_true() -> None:
    env = Env()
    assert env.begin(3)
    assert env.sink.run_active and env.sink.last_hold is None
    assert env.sink._pin == (100, 200)


def test_begin_run_needs_a_gate_of_this_frame() -> None:
    env = Env()
    env.gate()
    assert env.sink.begin_run(env.now + 0.01, total=3) is False
    assert env.sink.counts["begin_no_gate"] == 1 and env.sink.last_hold == "focus" and not env.sink.run_active


def test_begin_run_refuses_what_the_gate_refused_and_says_why() -> None:
    env = Env()
    env.desktop.modifiers = True
    assert env.frame() == "yield"
    assert not env.sink.begin_run(env.now, total=3) and env.sink.last_hold == "yield"
    env.desktop.modifiers = False
    assert env.frame(limits.YIELD_S + 0.1) is None
    assert env.begin(3)


@pytest.mark.parametrize("total", [0, -1, limits.COMPOSE_MAX + 1, 3.0, "3", None])
def test_begin_run_refuses_a_bad_length(total: object) -> None:
    env = Env()
    env.gate()
    assert env.sink.begin_run(env.now, total=total) is False  # type: ignore[arg-type]
    assert not env.sink.run_active and env.desktop.key_calls == []


def test_two_runs_are_never_active_at_once() -> None:
    env = Env()
    assert env.begin(3)
    env.advance(1.0)
    env.gate()
    assert env.sink.begin_run(env.now, total=3) is False
    assert env.sink.counts["run_overlap"] == 1 and env.sink._pin == (100, 200)


@pytest.mark.parametrize("blocked", ["elevated", "shell", "own", "none"])
def test_begin_run_reads_a_fresh_target_and_refuses_each_blocked_one(blocked: str) -> None:
    env = Env()
    env.gate()  # the cached target says all is well
    env.desktop.target = KeyTarget(100, 200, "x", 0x0409, blocked, False, False)  # type: ignore[arg-type]
    assert env.sink.begin_run(env.now, total=3) is False and env.sink.last_hold == "blocked"


def test_begin_run_refuses_no_window_a_password_a_cover_and_a_modifier() -> None:
    for target, hold in (
        (KeyTarget(0, 0, "", 0, None, False, False), "blocked"),
        (KeyTarget(100, 200, "x", 0x0409, None, True, False), "password"),
        (KeyTarget(100, 200, "x", 0x0409, None, False, True), "covered"),
    ):
        env = Env()
        env.gate()
        env.desktop.target = target
        assert env.sink.begin_run(env.now, total=3) is False and env.sink.last_hold == hold
    env = Env()
    env.gate()
    env.desktop.modifiers = True
    assert env.sink.begin_run(env.now, total=3) is False and env.sink.last_hold == "yield"


def test_begin_run_survives_a_target_that_raises() -> None:
    env = Env()
    env.gate()

    def boom() -> KeyTarget:
        raise RuntimeError(SENTINEL)

    env.desktop.key_target = boom  # type: ignore[method-assign]
    assert env.sink.begin_run(env.now, total=3) is False and env.sink.last_hold == "blocked"


# ---------------------------------------------------------------------------------------------------- run lane: send


def test_a_run_types_its_characters_one_batch_each() -> None:
    env = Env()
    assert env.run("hello world") == ["sent"] * 11
    assert env.desktop.typed_text == "hello world"
    assert len(env.desktop.key_batches) == 11 and all(events_balanced(b) for b in env.desktop.key_batches)
    env.sink.end_run(env.now, completed=True)
    assert not env.sink.run_active and env.sink._pin is None


def test_send_run_without_a_run_is_limited_and_a_runaway() -> None:
    env = Env()
    env.gate()
    assert env.sink.send_run(char("a"), env.now) == "limited"
    assert env.sink.runaway and env.desktop.key_calls == []


def test_a_run_cannot_send_more_than_its_total() -> None:
    env = Env()
    assert env.begin(2)
    results = []
    for _ in range(3):
        env.advance(0.05)
        env.gate()
        results.append(env.sink.send_run(char("a"), env.now))
    assert results == ["sent", "sent", "limited"] and env.sink.runaway and len(env.desktop.key_calls) == 2


def test_the_run_lane_types_space_as_the_space_control_and_only_that() -> None:
    env = Env()
    assert env.begin(3)
    for stroke in (char("a"), SPACE, char("b")):
        env.advance(0.05)
        env.gate()
        assert env.sink.send_run(stroke, env.now) == "sent"
    assert env.desktop.key_calls == [("char", "a"), ("control", "space"), ("char", "b")]


@pytest.mark.parametrize("stroke", [BACKSPACE, ENTER])
def test_the_run_lane_refuses_backspace_and_an_enter_outside_a_send(stroke: KeyStroke) -> None:
    env = Env()
    assert env.begin(2)
    env.advance(0.05)
    env.gate()
    assert env.sink.send_run(stroke, env.now) == "failed"
    assert env.desktop.key_calls == [] and env.sink.counts["bad_stroke"] == 1


@pytest.mark.parametrize("bad", ["a", 5, None, object()])
def test_the_run_lane_refuses_what_is_not_a_stroke(bad: object) -> None:
    env = Env()
    assert env.begin(2)
    env.advance(0.05)
    env.gate()
    assert env.sink.send_run(bad, env.now) == "failed"  # type: ignore[arg-type]
    assert env.desktop.key_calls == []


def test_a_fresh_target_is_read_before_every_character_not_the_cache() -> None:
    env = Env()
    reads = 0
    original = env.desktop.key_target

    def counting() -> KeyTarget:
        nonlocal reads
        reads += 1
        return original()

    env.desktop.key_target = counting  # type: ignore[method-assign]
    assert env.begin(10)
    start = reads
    for _ in range(10):
        env.advance(0.01)  # well inside the 100-ms refresh of the gate
        env.gate()
        env.sink.send_run(char("a"), env.now)
    assert reads - start >= 10


def mutate_after(env: Env, nth: int, target: KeyTarget) -> None:
    def hook(count: int) -> None:
        if count == nth:
            env.desktop.target = target

    env.desktop.after_key = hook


@pytest.mark.parametrize(
    ("target", "reason"),
    [
        (KeyTarget(100, 200, "x", 0x0409, "elevated", False, False), "blocked"),
        (KeyTarget(100, 200, "x", 0x0409, "shell", False, False), "blocked"),
        (KeyTarget(100, 200, "x", 0x0409, "own", False, False), "blocked"),
        (KeyTarget(100, 200, "x", 0x0409, "none", False, False), "blocked"),
        (KeyTarget(100, 200, "x", 0x0409, None, True, False), "password"),
        (KeyTarget(100, 200, "x", 0x0409, None, False, True), "covered"),
        (KeyTarget(100, 201, "x", 0x0409, None, False, False), "focus"),  # the same handle, another process
        (KeyTarget(101, 200, "x", 0x0409, None, False, False), "focus"),
    ],
)
def test_s44_a_change_of_window_between_two_characters_stops_the_run_before_the_next(
    target: KeyTarget, reason: str
) -> None:
    env = Env()
    assert env.begin(10)
    mutate_after(env, 3, target)  # after the third character, while the gate's 100-ms refresh is not due
    answers = []
    for c in "abcdefghij":
        env.advance(0.02)
        env.gate()
        answers.append(env.sink.send_run(char(c), env.now))
        if answers[-1] != "sent":
            break
    assert answers == ["sent"] * 3 + ["hold"]
    assert env.sink.last_hold == reason and env.desktop.typed_text == "abc"


def test_s44_a_password_edit_inside_the_pinned_window_stops_the_run() -> None:
    env = Env()
    assert env.begin(10)
    mutate_after(env, 2, KeyTarget(100, 200, "x", 0x0409, None, True, False))  # same hwnd and pid
    answers = []
    for c in "abcd":
        env.advance(0.02)
        env.gate()
        answers.append(env.sink.send_run(char(c), env.now))
    assert answers[:3] == ["sent", "sent", "hold"] and env.sink.last_hold == "password"
    assert env.desktop.typed_text == "ab"


def test_s43_foreign_input_or_a_modifier_after_a_character_holds_the_next() -> None:
    env = Env()
    assert env.begin(10)
    for c in "abc":
        env.advance(1 / 30)
        env.gate()
        assert env.sink.send_run(char(c), env.now) == "sent"
    env.desktop.user_typed()
    env.advance(1 / 30)
    assert env.gate() == "yield"
    assert env.sink.send_run(char("d"), env.now) == "hold" and env.sink.last_hold == "yield"
    assert env.desktop.typed_text == "abc"
    mod = Env()
    assert mod.begin(10)
    for c in "ab":
        mod.advance(1 / 30)
        mod.gate()
        assert mod.sink.send_run(char(c), mod.now) == "sent"
    mod.desktop.modifiers = True
    mod.advance(1 / 30)
    assert mod.gate() == "yield" and mod.sink.send_run(char("c"), mod.now) == "hold"
    assert mod.desktop.typed_text == "ab"


def test_s45_an_overlay_that_stops_drawing_holds_the_run() -> None:
    env = Env()
    assert env.begin(5)
    env.advance(0.03)
    assert env.sink.gate(env.now, overlay_ok=False) == "overlay"
    assert env.sink.send_run(char("a"), env.now) == "hold" and env.sink.last_hold == "overlay"


def test_a_missing_gate_is_a_hold_in_the_run_lane() -> None:
    env = Env()
    assert env.begin(5)
    env.advance(0.03)  # no gate() for this time
    assert env.sink.send_run(char("a"), env.now) == "hold"
    assert env.sink.last_hold == "focus" and env.sink.counts["no_gate"] == 1 and env.desktop.key_calls == []


def test_slow_is_not_a_hold_of_the_sink() -> None:
    env = Env()
    assert env.begin(2)
    env.advance(0.03)
    env.gate()
    env.sink._gate_hold = "slow"  # the session's own hold, if one were ever passed through
    assert env.sink.send_run(char("a"), env.now) == "sent"


def test_s46_blocked_partial_and_unexpected_failures_in_a_run() -> None:
    # InputBlocked at the fourth character: three typed, the fourth not counted
    blocked = Env()
    assert blocked.begin(8)
    out = []
    for i, c in enumerate("abcdefgh"):
        blocked.advance(1 / 30)
        blocked.gate()
        if i == 3:
            blocked.desktop.fail_keys = 1
        out.append(blocked.sink.send_run(char(c), blocked.now))
        if out[-1] != "sent":
            break
    assert out == ["sent"] * 3 + ["failed"] and blocked.desktop.typed_text == "abc"
    assert blocked.sink._run_sent == 3
    # a partly taken batch at the fourth: the stroke counts
    partial = Env()
    assert partial.begin(8)
    out = []
    for i, c in enumerate("abcdefgh"):
        partial.advance(1 / 30)
        partial.gate()
        if i == 3:
            partial.desktop.partial_keys = 1
        out.append(partial.sink.send_run(char(c), partial.now))
        if out[-1] != "sent":
            break
    assert out == ["sent"] * 3 + ["maybe"] and partial.desktop.typed_text == "abcd"
    assert partial.sink._run_sent == 4


def test_s46b_an_exception_after_delivery_is_maybe_and_a_refused_stroke_is_failed() -> None:
    late = Env()
    assert late.begin(8)
    out = []
    for i, c in enumerate("abcdefgh"):
        late.advance(1 / 30)
        late.gate()
        if i == 2:
            late.desktop.raise_after = RuntimeError(SENTINEL)
        out.append(late.sink.send_run(char(c), late.now))
        if out[-1] != "sent":
            break
    assert out == ["sent", "sent", "maybe"] and late.desktop.typed_text == "abc"  # delivered once, never again
    assert late.sink.counts["maybe"] == 1

    class Refusing(FakeDesktop):
        def send_keys(self, strokes: Sequence[KeyStroke], *, inject: Literal["unicode", "vk"] = "unicode") -> int:
            if len(self.key_calls) == 2:
                raise KeyRefused("fixed")
            return super().send_keys(strokes, inject=inject)

    refused = Env()
    refused.desktop = Refusing()
    refused.sink = KeySink(refused.desktop, inject="unicode", clock=refused.clock, commit="review")
    refused.sink.start(refused.now)
    refused.advance(0.5)
    assert refused.begin(8)
    out = []
    for c in "abcdefgh":
        refused.advance(1 / 30)
        refused.gate()
        out.append(refused.sink.send_run(char(c), refused.now))
        if out[-1] != "sent":
            break
    assert out == ["sent", "sent", "failed"] and refused.desktop.typed_text == "ab"
    assert refused.sink.counts["bad_stroke"] == 1 and refused.sink._fails == 0


def test_s47_three_failures_in_a_row_across_runs_fail_the_sink() -> None:
    env = Env()
    for i in range(3):
        env.advance(1.0)
        assert env.begin(2)
        env.advance(0.05)
        env.gate()
        env.desktop.fail_keys = 1
        if i < 2:
            assert env.sink.send_run(char("a"), env.now) == "failed"
            env.sink.end_run(env.now)
        else:
            with pytest.raises(SinkFailed):
                env.sink.send_run(char("a"), env.now)
    assert env.desktop.key_calls == []


# ------------------------------------------------------------------------------------------------ budgets and breaker


def test_s48_two_hundred_steps_in_one_frame_stop_at_the_run_breaker() -> None:
    env = Env()
    assert env.begin(limits.COMPOSE_MAX)
    env.advance(0.03)
    env.gate()
    results = [env.sink.send_run(char("a"), env.now) for _ in range(limits.COMPOSE_MAX)]
    assert results.count("sent") == limits.INSERT_BACKSTOP_N
    assert results.count("limited") == limits.COMPOSE_MAX - limits.INSERT_BACKSTOP_N
    assert env.sink.runaway and len(env.desktop.key_calls) == limits.INSERT_BACKSTOP_N


def test_the_run_breaker_is_a_sliding_window_at_the_pace_of_a_run() -> None:
    env = Env()
    assert env.begin(limits.COMPOSE_MAX)
    for _ in range(limits.COMPOSE_MAX):
        env.advance(limits.INSERT_GAP_S)
        env.gate()
        assert env.sink.send_run(char("a"), env.now) == "sent"
    assert not env.sink.runaway


def run_all(env: Env, n: int, *, spacing: float = limits.INSERT_GAP_S) -> list[str]:
    """One full run of ``n`` characters at ``spacing``; the answers."""
    assert env.begin(n)
    out = []
    for _ in range(n):
        env.advance(spacing)
        env.gate()
        out.append(env.sink.send_run(char("a"), env.now))
    env.sink.end_run(env.now)
    return out


def test_s47b_the_character_budget_stops_the_601st_in_any_minute() -> None:
    env = Env()
    typed = 0
    refused_at = None
    for _ in range(5):
        env.advance(limits.RUN_COOLDOWN_S + 0.1)
        env.gate()
        if not env.sink.begin_run(env.now, total=limits.COMPOSE_MAX):
            break
        for _ in range(limits.COMPOSE_MAX):
            env.advance(limits.INSERT_GAP_S)
            env.gate()
            result = env.sink.send_run(char("a"), env.now)
            if result == "limited":
                refused_at = typed
                break
            assert result == "sent"
            typed += 1
        env.sink.end_run(env.now)
        if refused_at is not None:
            break
    assert typed == limits.RUN_MAX_CHARS_PER_MIN and refused_at == limits.RUN_MAX_CHARS_PER_MIN
    assert env.sink.runaway and env.sink.counts["run_budget"] == 1


def test_s47b_the_run_budget_stops_the_thirteenth_begin_in_a_minute() -> None:
    env = Env()
    results = []
    for _ in range(limits.RUN_MAX_PER_MIN + 1):
        env.advance(limits.RUN_COOLDOWN_S + 0.1)
        env.gate()
        began = env.sink.begin_run(env.now, total=1)
        results.append(began)
        if began:
            env.advance(0.05)
            env.gate()
            assert env.sink.send_run(char("a"), env.now) == "sent"
            env.sink.end_run(env.now)
    assert results == [True] * limits.RUN_MAX_PER_MIN + [False]
    assert env.sink.runaway and env.sink.counts["run_budget"] == 1 and env.sink.last_hold == "yield"


def test_s47b_the_budgets_are_forgotten_after_a_minute() -> None:
    env = Env()
    for _ in range(limits.RUN_MAX_PER_MIN):
        env.advance(limits.RUN_COOLDOWN_S + 0.1)
        env.gate()
        assert env.sink.begin_run(env.now, total=1)
        env.sink.end_run(env.now)
    env.advance(61.0)
    env.gate()
    assert env.sink.begin_run(env.now, total=1) and not env.sink.runaway


def test_s47b_a_begin_too_soon_after_an_end_is_refused_once_and_is_not_a_runaway() -> None:
    env = Env()
    assert env.begin(1)
    env.advance(0.05)
    env.gate()
    env.sink.send_run(char("a"), env.now)
    env.sink.end_run(env.now)
    env.advance(limits.RUN_COOLDOWN_S - 0.2)
    env.gate()
    assert env.sink.begin_run(env.now, total=1) is False
    assert env.sink.last_hold == "yield" and env.sink.counts["run_cooldown"] == 1 and not env.sink.runaway
    env.advance(0.3)
    env.gate()
    assert env.sink.begin_run(env.now, total=1)


def test_the_cooldown_does_not_apply_to_a_send_but_the_budget_does() -> None:
    env = Env()
    assert env.begin(2)
    for c in "ab":
        env.advance(0.05)
        env.gate()
        env.sink.send_run(char(c), env.now)
    env.sink.end_run(env.now, completed=True)
    env.advance(0.1)  # well inside the cooldown
    env.gate()
    assert env.sink.begin_run(env.now, total=1, again=True)
    assert len(env.sink._run_begins) == 2  # a Send counts toward the twelve


# ----------------------------------------------------------------------------------------------------------- again


def finished_text_run(env: Env, text: str = "ab") -> float:
    assert env.begin(len(text))
    for c in text:
        env.advance(0.05)
        env.gate()
        assert env.sink.send_run(char(c), env.now) == "sent"
    env.sink.end_run(env.now, completed=True)
    return env.now


def test_a_send_run_types_one_enter_in_the_window_of_the_last_text_run() -> None:
    env = Env()
    finished_text_run(env)
    env.advance(1.0)
    assert env.begin(1, again=True)
    env.advance(0.05)
    env.gate()
    assert env.sink.send_run(ENTER, env.now) == "sent"
    assert env.desktop.typed_text == "ab\n"
    env.sink.end_run(env.now)
    # single shot: a second Send finds no pin
    env.advance(1.0)
    assert not env.begin(1, again=True)


def test_a_send_run_refuses_a_char_and_a_text_run_refuses_enter() -> None:
    env = Env()
    finished_text_run(env)
    env.advance(1.0)
    assert env.begin(1, again=True)
    env.advance(0.05)
    env.gate()
    assert env.sink.send_run(char("a"), env.now) == "failed"
    assert env.desktop.typed_text == "ab"


@pytest.mark.parametrize("why", ["old", "other_window", "other_pid", "aborted", "two_chars", "none"])
def test_a_send_run_is_refused_unless_it_follows_a_completed_run_in_the_same_window(why: str) -> None:
    env = Env()
    if why == "aborted":
        assert env.begin(2)
        env.sink.end_run(env.now, completed=False)
    elif why != "none":
        finished_text_run(env)
    env.advance(limits.SEND_WINDOW_S + 1.5 if why == "old" else 1.0)
    if why == "other_window":
        env.desktop.target = KeyTarget(101, 200, "x", 0x0409, None, False, False)
        env.advance(1.0)
    if why == "other_pid":
        env.desktop.target = KeyTarget(100, 201, "x", 0x0409, None, False, False)
        env.advance(1.0)
    env.gate()
    env.advance(1.0)
    assert env.gate() in (None, "focus")
    total = 2 if why == "two_chars" else 1
    assert env.sink.begin_run(env.now, total=total, again=True) is False
    assert env.sink.counts["again_refused"] == 1


def test_a_send_inside_the_grace_second_after_the_window_is_still_allowed_by_the_sink() -> None:
    env = Env()
    finished_text_run(env)
    env.advance(limits.SEND_WINDOW_S + 0.5)
    assert env.begin(1, again=True)  # the machine's window is the strict one; the sink adds a second of grace


# -------------------------------------------------------------------------------------------------------- fuzz S3


def test_s3_only_allowed_strokes_ever_reach_the_desktop() -> None:
    rng = random.Random(3)
    pool = [*"abcxyzABC", *"',./-?", "א", "ת", "1", "9", "!", ";", "\n", "\t", "\x1b", "\x7f", "‮", "\U0001f600"]
    for commit in ("direct", "review"):
        env = Env(commit)  # type: ignore[arg-type]
        for _ in range(3_000):
            env.advance(rng.choice((0.02, 0.03, 0.05, 0.2, 0.7)))
            if rng.random() < 0.02:
                env.desktop.user_typed()
            if rng.random() < 0.03:
                env.desktop.modifiers = not env.desktop.modifiers
            env.gate(overlay_ok=rng.random() > 0.05)
            kind = rng.choice(("char", "control", "int", "str", "none"))
            value = rng.choice(pool)
            stroke: object
            if kind == "char":
                try:
                    stroke = KeyStroke("char", value)
                except KeyRefused:
                    stroke = KeyStroke("char", "a")
                    object.__setattr__(stroke, "value", value)  # forced past the constructor
            elif kind == "control":
                stroke = KeyStroke("control", rng.choice(("space", "backspace", "enter")))
            elif kind == "int":
                stroke = rng.randrange(-5, 300)
            elif kind == "str":
                stroke = value
            else:
                stroke = None
            try:
                if commit == "direct":
                    env.sink.send(stroke, env.now)  # type: ignore[arg-type]
                else:
                    if not env.sink.run_active and rng.random() < 0.3:
                        env.sink.begin_run(env.now, total=rng.choice((1, 5, 200)), again=rng.random() < 0.2)
                    env.sink.send_run(stroke, env.now)  # type: ignore[arg-type]
                    if rng.random() < 0.05:
                        env.sink.end_run(env.now, completed=rng.random() < 0.5)
            except SinkFailed:
                break
        for kind_, value_ in env.desktop.key_calls:
            if kind_ == "char":
                assert value_ in ALLOWED_CHARS
            else:
                assert value_ in ({"space", "backspace", "enter"} if commit == "direct" else {"space", "enter"})
        assert all(events_balanced(b) for b in env.desktop.key_batches)


def test_s22_the_sentinel_stays_out_of_counts_and_reprs() -> None:
    env = Env()
    assert env.begin(len(SENTINEL))
    for c in SENTINEL:
        env.advance(0.05)
        env.gate()
        env.sink.send_run(char(c), env.now)
    pieces = [repr(env.sink), repr(env.sink.counts), env.sink.target_name, repr(env.sink.last_hold)]
    assert all(SENTINEL not in p for p in pieces)
    assert all(isinstance(v, int) for v in env.sink.counts.values())
    assert env.desktop.typed_text == SENTINEL


# ----------------------------------------------------------------------------- the layers, one by one (mutation gaps)


def test_a_modifier_probe_that_raises_reads_as_a_key_down() -> None:
    env = Env()

    def boom() -> bool:
        raise OSError(SENTINEL)

    env.desktop.modifiers_down = boom  # type: ignore[method-assign]
    assert env.frame() == "yield"
    assert env.sink.counts["modifiers_fail"] == 1


def test_begin_run_reads_a_modifier_probe_that_raises_as_a_key_down() -> None:
    env = Env()
    env.gate()  # the gate's own probe answered False
    env.desktop.modifiers_down = lambda: (_ for _ in ()).throw(OSError(SENTINEL))  # type: ignore[method-assign]
    assert env.sink.begin_run(env.now, total=3) is False and env.sink.last_hold == "yield"


def test_a_second_start_is_a_new_baseline_for_the_target_too() -> None:
    env = Env()
    env.desktop.target = KeyTarget(777, 200, "other", 0x0409, None, False, False)
    env.advance(0.01)
    env.sink.start(env.now)  # the ladder fallback re-arms in whatever window is in front now
    env.advance(limits.SINK_WARMUP_S + 0.05)
    assert env.gate() is None  # not "focus": a baseline is not a window change


def test_foreign_input_before_the_first_start_is_not_held_against_the_user_after_the_window() -> None:
    desktop = FakeDesktop()
    sink = KeySink(desktop, inject="unicode", clock=Clock(), commit="review")
    desktop.user_typed()
    sink.start(10.0)
    assert sink.gate(10.0 + limits.SINK_WARMUP_S + 0.5) is None


class Duck:
    kind = "char"
    value = "a"


def test_a_duck_that_looks_like_a_stroke_is_not_one() -> None:
    for env in (Env("direct"), Env("review")):
        env.gate()
        if env.sink._commit == "direct":
            assert env.sink.send(Duck(), env.now) == "failed"  # type: ignore[arg-type]
        else:
            assert env.begin(1)
            assert env.sink.send_run(Duck(), env.now) == "failed"  # type: ignore[arg-type]
        assert env.desktop.key_calls == []


def test_a_stroke_of_an_unknown_kind_is_refused_even_with_an_allowed_value() -> None:
    stroke = char("a")
    object.__setattr__(stroke, "kind", "bogus")
    object.__setattr__(stroke, "value", "space")
    env = Env("direct")
    calls: list[int] = []
    env.desktop.send_keys = lambda *a, **k: calls.append(1)  # type: ignore[method-assign, assignment, return-value]
    env.gate()
    assert env.sink.send(stroke, env.now) == "failed" and calls == []  # refused by the sink, not by the desktop


def test_the_sink_checks_the_alphabet_itself_not_only_the_stroke() -> None:
    from jarvis_hands.keyboard import sink as sink_module

    narrow = frozenset("a")
    original = sink_module.ALLOWED_CHARS
    sink_module.ALLOWED_CHARS = narrow
    try:
        env = Env("direct")
        env.gate()
        assert env.sink.send(char("a"), env.now) == "sent"
        env.advance(0.1)
        env.gate()
        assert env.sink.send(char("b"), env.now) == "failed" and env.desktop.typed_text == "a"
    finally:
        sink_module.ALLOWED_CHARS = original


def test_begin_run_refuses_on_the_gate_holds_that_only_the_gate_knows() -> None:
    env = Env()
    env.desktop.user_typed()
    env.advance(0.05)
    env.gate()
    assert env.sink.begin_run(env.now, total=2) is False and env.sink.last_hold == "yield"
    env = Env()
    env.advance(0.05)
    env.sink.gate(env.now, overlay_ok=False)
    assert env.sink.begin_run(env.now, total=2) is False and env.sink.last_hold == "overlay"
    env = Env()
    env.desktop.target = KeyTarget(900, 200, "x", 0x0409, None, False, False)
    env.advance(0.2)
    env.gate()
    # the window is still the new one, so every fresh read agrees; only the settle window says no
    assert env.sink.begin_run(env.now, total=2) is False and env.sink.last_hold == "focus"


def test_a_send_cannot_follow_a_send() -> None:
    env = Env()
    finished_text_run(env)
    env.advance(1.0)
    assert env.begin(1, again=True)
    env.sink.end_run(env.now, completed=True)  # a controller that wrongly called the Send "completed"
    env.advance(1.0)
    assert not env.begin(1, again=True)


def test_an_aborted_text_run_forfeits_the_pin_of_the_one_before() -> None:
    env = Env()
    finished_text_run(env)
    env.advance(1.0)
    assert env.begin(3)
    env.sink.end_run(env.now, completed=False)
    env.advance(1.0)
    assert not env.begin(1, again=True)


def test_after_a_pin_mismatch_the_window_must_settle_even_when_only_the_process_changed() -> None:
    env = Env()
    assert env.begin(5)
    env.desktop.target = KeyTarget(100, 201, "x", 0x0409, None, False, False)
    env.advance(0.02)
    env.gate()
    assert env.sink.send_run(char("a"), env.now) == "hold"
    env.desktop.target = OK
    assert env.frame(0.2) == "focus"  # the handle never changed, so only the hold itself started the settle


def test_the_character_budget_is_forgotten_after_a_minute() -> None:
    env = Env()
    for _ in range(3):  # the full minute's worth of characters
        env.advance(limits.RUN_COOLDOWN_S + 0.1)
        env.gate()
        assert env.sink.begin_run(env.now, total=limits.COMPOSE_MAX)
        for _ in range(limits.COMPOSE_MAX):
            env.advance(limits.INSERT_GAP_S)
            env.gate()
            assert env.sink.send_run(char("a"), env.now) == "sent"
        env.sink.end_run(env.now)
    env.advance(75.0)  # more than a minute, far less than five
    env.gate()
    assert env.sink.begin_run(env.now, total=limits.COMPOSE_MAX)
    for _ in range(limits.COMPOSE_MAX):
        env.advance(limits.INSERT_GAP_S)
        env.gate()
        assert env.sink.send_run(char("a"), env.now) == "sent"
    assert not env.sink.runaway


def test_a_stroke_that_may_have_been_typed_is_a_failure_too_and_a_fast_success_resets_the_streak() -> None:
    env = Env()
    assert env.begin(60)

    def one(fault: str | None) -> str:
        env.advance(1 / 30)
        env.gate()
        if fault == "partial":
            env.desktop.partial_keys = 1
        return env.sink.send_run(char("a"), env.now)

    assert [one(f) for f in ("partial", "partial", None, "partial", "partial", None)] == [
        "maybe",
        "maybe",
        "sent",
        "maybe",
        "maybe",
        "sent",
    ]
    assert one("partial") == "maybe" and one("partial") == "maybe"
    with pytest.raises(SinkFailed):
        one("partial")


@pytest.mark.parametrize("answer", [None, 0, "x", (1, 2)])
def test_a_target_that_is_not_a_target_is_no_window(answer: object) -> None:
    env = Env()
    env.desktop.key_target = lambda: answer  # type: ignore[method-assign, return-value]
    assert env.frame(0.2) == "blocked"
    assert env.sink.target_name == ""


def test_a_hold_of_the_key_lane_is_counted() -> None:
    env = Env("direct")
    env.desktop.modifiers = True
    env.frame()
    assert env.sink.send(char("a"), env.now) == "hold" and env.sink.counts["hold"] == 1
