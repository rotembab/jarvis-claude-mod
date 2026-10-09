"""The review machine (DESIGN 2.13): the guards, the run, Send, the storm freeze and what the box shows.

U42, U43, U45, U82, B40 to B42 and S41 to S43, S46, S46b, S50, S51, S56, S57, S59 and N40 to N47 at machine level. The
same scenarios through the session, the sink and the fake desktop are in ``test_kb_safety.py``; here there are no
hands, no sink and no desktop, only a machine and the answers a sink would give. Every time is an argument.
"""

from __future__ import annotations

import ast
import math
import random
from pathlib import Path

import pytest

from jarvis_hands.desktop.keys import COMPOSE_CHARS, KeyStroke
from jarvis_hands.keyboard import limits, review
from jarvis_hands.keyboard.compose import ComposeBuffer
from jarvis_hands.keyboard.review import (
    ABORT_WHY,
    GUARD_OF_KEY,
    GUARD_TAPS,
    GUARD_WINDOW,
    REVIEW_TEXT,
    InsertStep,
    InsertSummary,
    LastInsert,
    ReviewMachine,
)
from jarvis_hands.keyboard.types import Hold, InsertResult, KeyKind, Touch

SENTINEL = "zzqxjv"
ALL_KINDS: tuple[KeyKind, ...] = (
    "char", "shift", "lang", "private", "home", "backspace", "space", "enter", "close", "insert", "clear", "chip",
)  # fmt: skip
#: The keys the machine sees, in the proportions of the layout (a phantom lands on a letter far more often than on a
#: corner key): thirty-one letters, then one cell each of the others and the three chips.
PHANTOM_KEYS: tuple[KeyKind, ...] = (
    ("char",) * 31 + ("space", "backspace", "enter", "close", "insert", "clear") + ("chip",) * 3
)


class Bench:
    """What the session and the controller do around one machine, frame by frame.

    ``at`` is one frame: ``tick`` first, then at most one tap, then the answer to every step that came out (never more
    than one). ``results`` scripts what the sink says to the n-th step of the bench (1-based); anything else is
    ``sent``. ``typed`` is what reached "the window".
    """

    def __init__(self, *, enter: str = "twice", fps: float = 30.0, machine: ReviewMachine | None = None) -> None:
        self.m = machine or ReviewMachine(enter=enter)  # type: ignore[arg-type]
        self.dt = 1.0 / fps
        self.t = 0.0
        self.hold: Hold | None = None
        self.last_hold: Hold | None = None
        self.typed: list[str] = []
        self.typed_at: list[float] = []
        self.steps: list[InsertStep] = []
        self.summaries: list[InsertSummary] = []
        self.results: dict[int, InsertResult] = {}
        self.hold_for_step: Hold | None = None
        self.starts = 0

    # -- time
    def at(self, t: float, tap: tuple[KeyKind, str] | None = None, *, touch: Touch | None = None) -> list[InsertStep]:
        self.t = t
        emitted: list[InsertStep] = []
        step = self.m.tick(t, self.hold)
        if step is not None:
            emitted.append(step)
        if tap is not None:
            started = self.m.tap(tap[0], tap[1], t, touch=touch)
            if started is not None:
                assert not emitted, "a tap cannot start a run while a step came out of tick"
                emitted.append(started)
                self.starts += 1
        assert len(emitted) <= 1
        for step in emitted:
            self._answer(step, t)
        summary = self.m.take_summary()
        if summary is not None:
            self.summaries.append(summary)
        return emitted

    def frame(self, tap: tuple[KeyKind, str] | None = None) -> list[InsertStep]:
        """The next frame, one frame interval after the last time used."""
        return self.at(self.t + self.dt, tap)

    def frames(self, n: int) -> None:
        for _ in range(n):
            self.frame()

    def drain(self, limit_s: float = 60.0) -> None:
        """Frames until the run is over (or the limit)."""
        end = self.t + limit_s
        while self.m.running and self.t < end:
            self.frame()

    def _answer(self, step: InsertStep, t: float) -> None:
        self.steps.append(step)
        result = self.results.get(len(self.steps), "sent")
        if result in ("sent", "maybe"):
            self.typed.append(" " if step.stroke.value == "space" else step.stroke.value)
            self.typed_at.append(t)
        self.m.note_step(step, result, t, self.hold_for_step)

    # -- the box
    def fill(self, text: str, *, gap: float = 0.3, t0: float | None = None) -> float:
        """Taps ``text`` one character per ``gap`` seconds on the bench clock; returns the time after the last."""
        t = self.t if t0 is None else t0
        for ch in text:
            self.at(t, ("space", "") if ch == " " else ("char", ch))
            t += gap
        self.t = t
        return t

    def insert3(self, t0: float, gap: float = 0.5) -> list[InsertStep]:
        out: list[InsertStep] = []
        for i in range(3):
            out += self.at(t0 + i * gap, ("insert", ""))
        return out

    @property
    def box(self) -> str:
        return self.m.buffer.text()


def start_run(text: str, *, enter: str = "twice", fps: float = 30.0) -> Bench:
    """A bench with ``text`` in the box and the third Insert tap just made (a run is in flight)."""
    b = Bench(enter=enter, fps=fps)
    b.fill(text)
    t = b.t + 0.5
    b.insert3(t)
    assert b.m.running
    return b


# ------------------------------------------------------------------------------------------------------ the strings


EXPECTED_TEXT = {
    "hint_empty": "Tap letters. Insert types them into the window in front.",
    "counter": "-> {target}   {n}/200",
    "arm_insert_1": "Insert {n} characters into {target}? Tap Insert 2 more times.",
    "arm_insert_2": "Tap Insert once more to type into {target}.",
    "arm_clear": "Clear the box? Tap Clear again.",
    "arm_close": "Close and throw away {n} characters? Tap Close again.",
    "arm_send_1": "Press Enter in {target}? Tap Send 2 more times.",
    "arm_send_2": "Tap Send once more to press Enter in {target}.",
    "inserting": "Typing {sent}/{total} into {target}. Tap Insert to stop.",
    "sending": "Pressing Enter in {target}.",
    "done": "Typed {n} characters into {target}.",
    "done_send": "Typed {n} characters into {target}. Tap Send 3 times within 10 s to press Enter.",
    "done_slash": 'Typed {n} characters. Not sent: it starts with "/". Press Enter yourself.',
    "done_bang": 'Typed {n} characters. Not sent: it starts with "!". Press Enter yourself.',
    "done_prefix": (
        'Typed {n} characters. Not sent: this session inserted text starting with "/" or "!". Press Enter yourself.'
    ),
    "sent": "Enter pressed.",
    "aborted": "Typed {sent} of {total}, then stopped ({why}). The rest is still in the box.",
    "full": "The box is full (200). Insert it or clear it.",
    "empty_insert": "The box is empty.",
    "send_none": "Nothing to send: Send works only within 10 s after Insert.",
    "send_off": "Enter is turned off.",
    "storm": "Too many taps at once. Paused for 3 s.",
    "practice": "Practice: nothing is inserted.",
    "idle": "Closing in {s} s: no hands in view. The box will be thrown away.",
}


def test_the_fixed_strings_are_exactly_appendix_f2() -> None:
    for key, text in EXPECTED_TEXT.items():
        assert REVIEW_TEXT[key] == text, key
    extra = set(REVIEW_TEXT) - set(EXPECTED_TEXT)
    assert extra <= {"send_failed"}, extra  # the one string the design leaves out (a failed Send), see DEVIATIONS


def test_every_abort_reason_has_a_plain_reason() -> None:
    assert set(ABORT_WHY) == {
        "blocked", "password", "covered", "overlay", "focus", "yield", "stopped", "timeout", "failed",
    }  # fmt: skip
    assert ABORT_WHY["focus"] == "the window changed"
    assert ABORT_WHY["yield"] == "you used the keyboard or mouse"
    assert ABORT_WHY["blocked"] == "that window cannot be typed into"
    assert ABORT_WHY["password"] == "that looks like a password box"
    assert ABORT_WHY["covered"] == "the screen is covered"
    assert ABORT_WHY["overlay"] == "the keyboard could not be drawn"
    assert ABORT_WHY["stopped"] == "you stopped it"
    assert ABORT_WHY["timeout"] == "it took too long"
    assert ABORT_WHY["failed"] == "Windows would not take the keys"


def test_the_strings_carry_numbers_and_names_only() -> None:
    allowed = {"target", "n", "sent", "total", "why", "s"}
    for key, text in REVIEW_TEXT.items():
        fields = {f for _, f, _, _ in __import__("string").Formatter().parse(text) if f}
        assert fields <= allowed, (key, fields)


def test_the_guard_tables_are_the_pinned_ones() -> None:
    assert GUARD_OF_KEY == {"insert": "insert", "clear": "clear", "enter": "send", "close": "close"}
    assert GUARD_TAPS == {"insert": limits.INSERT_TAPS, "clear": 2, "send": limits.SEND_TAPS, "close": 2}
    assert set(GUARD_WINDOW.values()) == {limits.GUARD_MAX_S}
    assert limits.INSERT_TAPS >= 3 and limits.SEND_TAPS >= 3


# ------------------------------------------------------------------------------------------------- U42 and U43: guards


def box_of(text: str = "hello") -> Bench:
    b = Bench()
    b.fill(text)
    return b


def test_u42_the_third_tap_of_three_confirms_and_starts_the_run() -> None:
    b = box_of()
    t = 10.0
    assert b.at(t, ("insert", "")) == [] and b.m.view(t, private=False).guard == "insert"
    assert b.at(t + 0.5, ("insert", "")) == []
    assert b.m.view(t + 0.5, private=False).guard_taps == 2
    out = b.at(t + 1.0, ("insert", ""))
    assert len(out) == 1 and out[0].first and out[0].kind == "text" and out[0].index == 0 and out[0].total == 5
    assert b.m.state == "inserting" and b.m.view(t + 1.0, private=False).guard is None


def test_u42_taps_closer_than_the_minimum_are_bounces_and_further_apart_confirm() -> None:
    gap = limits.GUARD_MIN_S
    for spacing, confirms in ((gap - 0.05, False), (gap + 0.05, True)):
        b = box_of()
        t = 10.0
        for i in range(3):
            b.at(t + i * spacing, ("insert", ""))
        assert b.starts == (1 if confirms else 0), spacing
        # 0.20 s apart: the second tap is a bounce, the third is 0.40 s after the last counted one and counts (n = 2)
        assert b.m.counts["bounce"] == (0 if confirms else 1)


@pytest.mark.parametrize("fps", [15.0, 30.0, 60.0])
def test_b42_the_guard_edges_hold_within_a_frame_of_quantisation(fps: float) -> None:
    """The edge of GUARD_MIN_S, plus or minus a frame (two at 60 fps): a bounce below it, a confirmation above it."""
    edge = limits.GUARD_MIN_S * fps
    slack = 1 if fps >= 60.0 else 0
    cases = (
        (math.ceil(edge) - 1 - slack, False),  # 0.20 s at 15 fps, 0.233 s at 30, 0.217 s at 60
        (math.floor(edge) + 1 + slack, True),  # 0.267 s at 15 and 30, 0.283 s at 60
    )
    for frames, confirms in cases:
        b = box_of()
        for i in range(3):
            b.at(20.0 + i * frames / fps, ("insert", ""))
        assert b.starts == (1 if confirms else 0), (fps, frames)
    # the round numbers of the design: 0.20 s apart bounce, 0.30 s apart confirm
    for spacing, confirms in ((0.20, False), (0.30, True)):
        b = box_of()
        frames = round(spacing * fps)
        for i in range(3):
            b.at(20.0 + i * frames / fps, ("insert", ""))
        assert b.starts == (1 if confirms else 0), (fps, spacing)


def test_u42_three_taps_inside_the_window_confirm_and_spread_over_more_only_rearm() -> None:
    inside = box_of()
    for t in (10.0, 12.9, 15.8):  # 5.8 s
        inside.at(t, ("insert", ""))
    assert inside.starts == 1
    spread = box_of()
    for t in (10.0, 13.1, 16.2):  # 6.2 s: the third tap is the first of a new guard
        spread.at(t, ("insert", ""))
    assert spread.starts == 0
    assert spread.m.view(16.2, private=False).guard_taps == 1
    spread.at(17.2, ("insert", ""))
    spread.at(18.2, ("insert", ""))
    assert spread.starts == 1


def test_u42_the_window_is_the_time_since_the_first_counted_tap_not_the_last() -> None:
    b = box_of()
    for t in (10.0, 12.0, 14.0, 16.5):  # taps 1..3 at 10, 12, 14 confirm before 16.5 is looked at
        b.at(t, ("insert", ""))
    assert b.starts == 1


@pytest.mark.parametrize("between", ["char", "space", "backspace", "chip", "clear", "enter", "close"])
def test_u42_another_key_between_the_taps_clears_the_guard(between: str) -> None:
    b = box_of()
    b.at(10.0, ("insert", ""))
    b.at(10.5, ("insert", ""))
    b.at(11.0, (between, "a"))  # type: ignore[arg-type]
    b.at(11.5, ("insert", ""))
    b.at(12.0, ("insert", ""))
    assert b.starts == 0, between


@pytest.mark.parametrize("act", ["disarm", "hold", "edit_outside", "discard_free"])
def test_u42_a_hold_a_disarm_or_a_change_of_the_box_clears_the_guard(act: str) -> None:
    b = box_of()
    b.at(10.0, ("insert", ""))
    b.at(10.5, ("insert", ""))
    if act == "disarm":
        b.m.disarm()
    elif act == "hold":
        b.hold = "overlay"
        b.at(10.75)
        b.hold = None
    elif act == "edit_outside":
        assert b.m.buffer.append("z", 10.7) == "ok"  # a change the machine did not make (the decoder's way in)
        b.at(10.75)
    else:
        b.hold = "slow"  # slow is a hold: it ends the sequence of taps too (2.13.4)
        b.at(10.75)
        b.hold = None
    b.at(11.0, ("insert", ""))
    assert b.starts == 0, act
    assert b.m.view(11.0, private=False).guard_taps == 1


def test_u42_a_bounce_neither_counts_nor_cancels() -> None:
    b = box_of()
    for t in (10.0, 10.1, 10.4, 10.55, 10.8):  # counted: 10.0, 10.4, 10.8 (the others are bounces of the previous)
        b.at(t, ("insert", ""))
    assert b.starts == 1
    assert b.m.counts["bounce"] == 2


def test_u42_the_version_of_the_box_is_part_of_the_guard() -> None:
    """The tap checks it by itself, with no tick in between: a guard of an older box never counts."""
    b = box_of()
    m = b.m
    m.tap("insert", "", 10.0)
    m.buffer.append("z", 10.2)  # the box changed behind the machine's back
    assert m.tap("insert", "", 10.5) is None
    assert m.view(10.5, private=False).guard_taps == 1  # this tap armed again
    m.tap("insert", "", 11.0)
    assert m.tap("insert", "", 11.5) is not None  # three taps on the new box confirm


def test_u42_the_window_is_checked_at_the_tap_itself_and_runs_from_the_first_tap() -> None:
    """With no tick in between: spread over more than GUARD_MAX_S only re-arms, whatever the spacing of the last two."""
    for times in ((10.0, 13.1, 16.2), (10.0, 15.0, 16.5), (10.0, 12.0, 17.0)):
        m = box_of().m
        outs = [m.tap("insert", "", t) for t in times]
        assert outs == [None, None, None], times
        assert m.view(times[-1], private=False).guard_taps == 1
    m = box_of().m
    assert [m.tap("insert", "", t) is not None for t in (10.0, 12.0, 14.0)] == [False, False, True]


def test_a_change_that_leaves_the_box_empty_again_still_takes_the_send_opportunity() -> None:
    """SR42: every change of the box clears ``last_insert``, also one a tap did not make (the decoder's way in)."""
    b = inserted()
    t = b.m.last_insert.t + 1.0  # type: ignore[union-attr]
    b.m.buffer.append("x", t)
    b.m.buffer.backspace(t)  # the box is empty again but its version moved
    b.at(t + 0.1)
    assert b.m.last_insert is None
    assert send_taps(b, t + 0.5) == [] and b.m.counts["refused_send_none"] == 3


def test_a_step_answered_twice_or_out_of_order_changes_nothing() -> None:
    m = ReviewMachine(enter="twice")
    for i, ch in enumerate("abc"):
        m.tap("char", ch, float(i))
    first = None
    for i in range(3):
        first = m.tap("insert", "", 10.0 + i * 0.5)
    assert first is not None
    m.note_step(first, "sent", 11.0, None)
    m.note_step(first, "sent", 11.0, None)  # the same answer again
    assert m.counts["step_stale"] == 1
    second = m.tick(11.1, None)
    assert second is not None and second.index == 1
    m.note_step(first, "sent", 11.1, None)  # an answer to the wrong step
    assert m.counts["step_stale"] == 2 and m.running
    m.note_step(second, "sent", 11.1, None)
    third = m.tick(11.2, None)
    assert third is not None
    m.note_step(third, "sent", 11.2, None)
    assert not m.running and m.take_summary() == InsertSummary("text", "done", 3, 3, None)


def test_u42_clear_and_close_need_two_taps_and_clear_empties_the_box() -> None:
    b = box_of()
    b.at(10.0, ("clear", ""))
    assert b.box == "hello" and b.m.view(10.0, private=False).guard == "clear"
    b.at(10.5, ("clear", ""))
    assert b.box == "" and b.m.state == "composing" and b.m.counts["cleared"] == 1
    c = box_of()
    c.at(10.0, ("close", ""))
    assert not c.m.take_close()
    c.at(10.5, ("close", ""))
    assert c.m.take_close() and c.box == "hello"  # the machine asks; the session closes and discards
    assert not c.m.take_close()  # once


def test_u42_clear_and_close_use_their_own_guard_not_the_inserts() -> None:
    b = box_of()
    b.at(10.0, ("insert", ""))
    b.at(10.5, ("clear", ""))  # another guarded key: this arms Clear, it does not count for Insert
    assert b.m.view(10.5, private=False).guard == "clear" and b.m.view(10.5, private=False).guard_taps == 1
    b.at(11.0, ("insert", ""))
    assert b.m.view(11.0, private=False).guard == "insert" and b.m.view(11.0, private=False).guard_taps == 1


def test_u42_close_with_an_empty_box_asks_to_close_at_once() -> None:
    b = Bench()
    b.at(1.0, ("close", ""))
    assert b.m.take_close()


def test_u43_an_empty_or_space_only_box_does_not_arm_insert() -> None:
    for text in ("", "   "):
        b = Bench()
        b.fill(text)
        for i in range(3):
            b.at(10.0 + i * 0.5, ("insert", ""))
        assert b.starts == 0 and b.m.view(11.0, private=False).guard is None
        assert b.m.counts["refused_empty"] == 3
        assert b.m.flash == "drop"
    filled = box_of("a")
    filled.at(10.0, ("insert", ""))
    assert filled.m.view(10.0, private=False).guard == "insert"
    assert filled.m.counts["refused_empty"] == 0


def test_u43_a_clear_of_an_empty_box_is_refused_the_same_way() -> None:
    b = Bench()
    b.at(10.0, ("clear", ""))
    assert b.m.counts["refused_empty"] == 1 and b.m.view(10.0, private=False).guard is None


def test_the_insert_check_runs_at_every_insert_tap() -> None:
    wide = ComposeBuffer(COMPOSE_CHARS | frozenset("!"))
    m = ReviewMachine(enter="twice")
    m.buffer = wide
    assert m.tap("char", "!", 1.0) is None  # the machine's own box is the wide one: the tap fills it
    assert wide.text() == "!"
    b = Bench(machine=m)
    for i in range(3):
        b.at(10.0 + i * 0.5, ("insert", ""))
    assert b.starts == 0 and m.counts["refused_bang_first"] == 3
    assert m.view(11.0, private=False).guard is None


# --------------------------------------------------------------------------------------------- the transition table


def test_a_character_goes_to_the_end_of_the_box_with_its_touch() -> None:
    b = Bench()
    touch = Touch(1.5, 2.5, 1, "right", 0.25)
    b.at(1.0, ("char", "h"), touch=touch)
    b.at(1.5, ("space", ""))
    b.at(2.0, ("char", "i"))
    assert b.box == "h i" and b.m.flash == "ok"
    kept = b.m.buffer.touches()
    assert kept[0] is touch and kept[1] is None and kept[2] is None


def test_the_201st_character_is_full_and_flashes_drop() -> None:
    b = Bench()
    b.fill("a" * limits.COMPOSE_MAX)
    b.at(b.t + 1.0, ("char", "b"))
    assert len(b.box) == limits.COMPOSE_MAX and b.m.counts["full"] == 1 and b.m.flash == "drop"
    assert b.m.view(b.t, private=False).full
    assert b.m.strip(b.t, "Code") == REVIEW_TEXT["full"]
    b.at(b.t + 1.0, ("space", ""))
    assert b.m.counts["full"] == 2


def test_a_character_the_box_refuses_is_dropped_and_counted() -> None:
    b = Bench()
    b.at(1.0, ("char", "1"))
    b.at(2.0, ("char", "\n"))
    b.at(3.0, ("char", ""))
    assert b.box == "" and b.m.counts["refused_char"] == 3 and b.m.flash == "drop"


def test_backspace_removes_one_and_an_empty_box_counts_empty() -> None:
    b = box_of("ab")
    b.at(10.0, ("backspace", ""))
    assert b.box == "a" and b.m.flash == "ok"
    b.at(10.5, ("backspace", ""))
    b.at(11.0, ("backspace", ""))
    assert b.box == "" and b.m.counts["empty"] == 1 and b.m.flash == "drop"


def test_a_chip_is_inert_in_step_1_and_clears_the_guard() -> None:
    b = box_of()
    b.at(10.0, ("insert", ""))
    b.at(10.5, ("chip", "1"))
    assert b.box == "hello" and b.m.counts["chip_inert"] == 1 and b.m.flash == "drop"
    assert b.m.view(10.5, private=False).guard is None


def test_a_tap_on_a_review_key_in_the_wrong_state_is_busy_during_a_run() -> None:
    b = start_run("hello world")
    for kind in ("char", "space", "backspace", "clear", "close", "enter", "chip"):
        before = b.m.counts["busy"]
        assert b.m.tap(kind, "a", b.t + 0.01) is None  # type: ignore[arg-type]
        assert b.m.counts["busy"] == before + 1 and b.m.flash == "drop"
    assert b.box == "hello world" and not b.m.take_close()


def test_the_box_state_after_an_edit_from_aborted_is_composing() -> None:
    b = start_run("abcdef")
    b.hold = "focus"
    b.frame()
    b.hold = None
    assert b.m.state == "aborted"
    b.at(b.t + 0.2, ("char", "x"))
    assert b.m.state == "composing" and b.box.endswith("x")


def test_aborted_returns_to_composing_after_the_banner_time() -> None:
    b = start_run("abcdef")
    b.hold = "focus"
    b.frame()
    b.hold = None
    t = b.t
    assert b.m.state == "aborted"
    b.at(t + limits.ABORT_SHOW_S - 0.1)
    assert b.m.state == "aborted"
    b.at(t + limits.ABORT_SHOW_S + 0.1)
    assert b.m.state == "composing"


def test_taps_in_aborted_behave_as_composing_for_the_guards() -> None:
    b = start_run("abcdef")
    b.hold = "yield"
    b.frame()
    b.hold = None
    t = b.t + 1.0
    b.insert3(t)  # re-Insert the remainder
    assert b.m.running


# ----------------------------------------------------------------------------------------------------- the run


def test_s41_the_happy_path_types_the_box_one_character_per_step() -> None:
    b = Bench()
    b.fill("hello world")
    assert b.box == "hello world"
    b.insert3(b.t + 0.5)
    b.drain()
    assert "".join(b.typed) == "hello world"
    assert len(b.steps) == 11 and [s.index for s in b.steps] == list(range(11))
    assert [s.first for s in b.steps] == [True] + [False] * 10
    assert {s.total for s in b.steps} == {11} and {s.run for s in b.steps} == {1}
    assert b.steps[5].stroke == KeyStroke("control", "space")
    assert b.steps[0].stroke == KeyStroke("char", "h")
    assert b.box == "" and b.m.state == "composing" and not b.m.running
    assert b.m.last_insert is not None and b.m.last_insert.chars == 11
    [summary] = b.summaries
    assert (summary.kind, summary.outcome, summary.sent, summary.of, summary.reason) == ("text", "done", 11, 11, None)
    assert b.m.take_summary() is None  # once
    assert b.m.counts["insert_start"] == 1 and b.m.counts["insert_done"] == 1


def test_a_second_run_gets_the_next_run_id() -> None:
    b = Bench()
    b.fill("ab")
    b.insert3(b.t + 0.5)
    b.drain()
    b.fill("cd", t0=b.t + 1.0)
    b.insert3(b.t + 0.5)
    b.drain()
    assert [s.run for s in b.steps] == [1, 1, 2, 2]


def test_the_box_is_untouched_until_the_run_ends() -> None:
    b = start_run("hello")
    assert b.box == "hello"
    b.frames(2)
    assert b.box == "hello" and b.m.view(b.t, private=False).sent >= 1
    b.drain()
    assert b.box == ""


def test_s42_a_focus_change_after_five_of_ten_keeps_the_rest_and_a_re_insert_types_only_that() -> None:
    b = Bench()
    b.fill("abcdefghij")
    b.results = {6: "hold"}
    b.hold_for_step = "focus"
    b.insert3(b.t + 0.5)
    b.drain()
    assert "".join(b.typed) == "abcde"
    assert b.box == "fghij" and b.m.state == "aborted"
    [first] = b.summaries
    assert (first.kind, first.outcome, first.sent, first.of, first.reason) == ("text", "aborted", 5, 10, "focus")
    assert b.m.last_insert is None
    b.results = {}
    b.hold_for_step = None
    b.insert3(b.t + 1.5)
    b.drain()
    assert "".join(b.typed) == "abcdefghij"
    assert b.m.state == "composing" and b.box == ""
    assert b.summaries[1].sent == 5 and b.summaries[1].of == 5


def test_s43_a_hold_during_a_run_stops_it_at_exactly_the_characters_sent() -> None:
    for reason in ("yield", "blocked", "password", "covered", "overlay", "focus"):
        b = start_run("abcdefghij")
        b.frames(2)  # characters 2 and 3 (the first came with the third tap)
        assert "".join(b.typed) == "abc", reason
        b.hold = reason  # type: ignore[assignment]
        b.frames(3)
        assert "".join(b.typed) == "abc", reason
        assert b.m.state == "aborted" and b.box == "defghij"
        assert b.summaries[-1].reason == reason and b.summaries[-1].sent == 3
        assert len(b.steps) == 3


def test_s59_slow_does_not_stop_a_run() -> None:
    b = start_run("abcdefghij")
    b.hold = "slow"
    b.drain()
    assert "".join(b.typed) == "abcdefghij" and b.box == "" and b.summaries[-1].outcome == "done"


def test_s43_the_stop_tap_is_ignored_early_and_stops_the_run_late() -> None:
    early = start_run("a" * 100)
    early.m.tap("insert", "", early.t + 0.3)
    assert early.m.running and early.m.counts["busy"] == 1
    late = start_run("a" * 100)
    late.at(late.t + limits.STOP_ARM_S + 0.2, ("insert", ""))
    assert not late.m.running and late.m.state == "aborted"
    assert late.summaries[-1].reason == "stopped"
    n = len(late.typed)
    late.frames(5)
    assert len(late.typed) == n  # nothing after the stop
    assert late.box == "a" * (100 - n)


def test_s43_a_stop_in_a_frame_that_also_emitted_a_step_lets_that_step_count() -> None:
    b = start_run("abcdefghij")
    t = b.t + 0.7
    out = b.at(t, ("insert", ""))  # tick emits a step, the Stop tap follows it in the same frame
    assert len(out) == 1
    n = len(b.typed)
    assert not b.m.running and b.summaries[-1].reason == "stopped" and b.summaries[-1].sent == n
    assert b.box == "abcdefghij"[n:]


def test_a_stop_on_the_last_character_is_a_done_run() -> None:
    b = Bench()
    b.fill("ab")
    b.insert3(b.t + 0.5)
    b.hold = None
    # the second (last) step and a Stop tap in one frame, 0.7 s into the run
    t0 = b.t
    out = b.at(t0 + 0.7, ("insert", ""))
    assert len(out) == 1 and b.summaries[-1].outcome == "done" and b.box == ""


def test_the_run_times_out_after_the_maximum_and_keeps_the_remainder() -> None:
    b = Bench(fps=3.0)
    b.fill("a" * limits.COMPOSE_MAX, gap=0.3)
    b.insert3(b.t + 0.5)
    b.drain(limit_s=60.0)
    assert b.summaries[-1].reason == "timeout" and b.summaries[-1].outcome == "aborted"
    assert 0 < len(b.typed) < limits.COMPOSE_MAX
    assert len(b.box) == limits.COMPOSE_MAX - len(b.typed)
    assert b.typed_at[-1] - b.typed_at[0] <= limits.INSERT_MAX_S


@pytest.mark.parametrize("fps", [15.0, 30.0, 60.0])
def test_b40_two_hundred_characters_at_the_frame_rates(fps: float) -> None:
    b = Bench(fps=fps)
    b.fill("ab" * (limits.COMPOSE_MAX // 2), gap=0.3)
    b.insert3(b.t + 0.5)
    b.drain()
    assert len(b.typed) == limits.COMPOSE_MAX and b.summaries[-1].outcome == "done"
    span = b.typed_at[-1] - b.typed_at[0]
    expected = (limits.COMPOSE_MAX - 1) * (2.0 / fps if fps == 60.0 else 1.0 / fps)
    assert abs(span - expected) <= 0.3
    gaps = [y - x for x, y in zip(b.typed_at, b.typed_at[1:], strict=False)]
    assert min(gaps) >= limits.INSERT_GAP_S  # and the bench asserts one step per frame at most


def test_b41_seven_fps_finishes_inside_the_maximum_and_three_fps_does_not() -> None:
    seven = Bench(fps=7.0)
    seven.fill("a" * limits.COMPOSE_MAX, gap=0.3)
    seven.insert3(seven.t + 0.5)
    seven.drain()
    assert len(seven.typed) == limits.COMPOSE_MAX and seven.summaries[-1].outcome == "done"
    assert seven.typed_at[-1] - seven.typed_at[0] <= limits.INSERT_MAX_S


def test_s46_a_failed_step_drops_nothing_and_a_maybe_counts_as_typed() -> None:
    failed = Bench()
    failed.fill("abcdefgh")
    failed.results = {4: "failed"}  # the InputBlocked case: the fourth call delivered nothing
    failed.insert3(failed.t + 0.5)
    failed.drain()
    assert "".join(failed.typed) == "abc" and failed.box == "defgh"
    assert failed.summaries[-1].reason == "failed" and failed.summaries[-1].sent == 3
    maybe = Bench()
    maybe.fill("abcdefgh")
    maybe.results = {4: "maybe"}  # the partial case: the fourth was taken in part and counts
    maybe.insert3(maybe.t + 0.5)
    maybe.drain()
    assert "".join(maybe.typed) == "abcd" and maybe.box == "efgh"
    assert maybe.summaries[-1].reason == "failed" and maybe.summaries[-1].sent == 4


def test_s46b_a_maybe_on_the_last_character_is_a_done_run() -> None:
    b = Bench()
    b.fill("abc")
    b.results = {3: "maybe"}
    b.insert3(b.t + 0.5)
    b.drain()
    assert b.summaries[-1].outcome == "done" and b.box == "" and b.m.last_insert is not None


@pytest.mark.parametrize("result", ["limited", "failed", "hold"])
def test_every_other_answer_aborts_and_does_not_advance(result: str) -> None:
    b = Bench()
    b.fill("abcdef")
    b.results = {3: result}  # type: ignore[dict-item]
    b.insert3(b.t + 0.5)
    b.drain()
    assert "".join(b.typed) == "ab" and b.box == "cdef"
    expected = "focus" if result == "hold" else "failed"
    assert b.summaries[-1].reason == expected and b.summaries[-1].sent == 2


def test_a_hold_answer_names_the_holds_reason_and_defaults_to_focus() -> None:
    for given, expected in (("password", "password"), ("blocked", "blocked"), (None, "focus"), ("slow", "focus")):
        b = Bench()
        b.fill("abcd")
        b.results = {2: "hold"}
        b.hold_for_step = given  # type: ignore[assignment]
        b.insert3(b.t + 0.5)
        b.drain()
        assert b.summaries[-1].reason == expected, given


def test_a_step_that_the_controller_never_answers_is_read_as_maybe_and_aborts() -> None:
    m = ReviewMachine(enter="twice")
    for i, ch in enumerate("abc"):
        m.tap("char", ch, float(i))
    for i in range(3):
        step = m.tap("insert", "", 10.0 + i * 0.5)
    assert step is not None
    assert m.tick(11.1, None) is None  # no answer to the first step: nothing more goes out
    assert not m.running and m.take_summary() == InsertSummary("text", "aborted", 1, 3, "failed")
    assert m.buffer.text() == "bc"  # the unanswered one is treated as delivered: never typed twice


def test_a_late_answer_to_a_step_of_a_closed_run_changes_nothing() -> None:
    m = ReviewMachine(enter="twice")
    for i, ch in enumerate("abc"):
        m.tap("char", ch, float(i))
    step = None
    for i in range(3):
        step = m.tap("insert", "", 10.0 + i * 0.5)
    assert step is not None
    assert m.discard() == 3  # the closing session drops the run before the controller answered the first step
    m.note_step(step, "sent", 11.1, None)
    assert m.buffer.text() == "" and m.take_summary() is None and m.state == "composing"
    assert m.counts["step_stale"] == 1


def test_discard_returns_the_unsent_characters_and_drops_everything() -> None:
    b = Bench()
    b.fill("hello")
    b.at(10.0, ("insert", ""))
    assert b.m.discard() == 5
    assert b.box == "" and b.m.view(10.0, private=False).guard is None and b.m.state == "composing"
    inflight = start_run("hello world")
    inflight.frames(2)
    sent = len(inflight.typed)
    assert inflight.m.discard() == 11 - sent
    assert not inflight.m.running and inflight.box == ""
    assert inflight.m.discard() == 0


def test_a_run_is_started_from_exactly_one_place() -> None:
    """S50: the code path that starts a text run has one call site, and it is reached only by the Insert tap."""
    tree = ast.parse(Path(review.__file__).read_text(encoding="utf-8"))
    sites: dict[str, list[str]] = {}
    for cls in (n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ReviewMachine"):
        for fn in (n for n in cls.body if isinstance(n, ast.FunctionDef)):
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "self"
                ):
                    sites.setdefault(node.func.attr, []).append(fn.name)
    assert sites["_start_text"] == ["_tap_insert"], sites["_start_text"]
    assert sites["_tap_insert"] == ["tap"], sites["_tap_insert"]
    assert sites["_start_enter"] == ["_tap_enter"] and sites["_tap_enter"] == ["tap"]
    # nothing outside the class calls either
    outside = [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr in ("_start_text", "_start_enter")]
    assert len(outside) == 2


def test_s50_nothing_but_a_confirming_tap_starts_a_run() -> None:
    """Lang, Shift, Home, Priv (disarm), a hold ending, a hand returning, a phase change and a close never do."""
    b = box_of()
    b.at(10.0, ("insert", ""))
    b.at(10.5, ("insert", ""))  # two of three taps: armed
    for step in (
        lambda: b.m.disarm(),
        lambda: b.m.tick(10.6, None),
        lambda: b.m.tick(10.7, "yield"),
        lambda: b.m.tick(10.8, None),
        lambda: b.m.discard(),
        lambda: b.m.view(10.9, private=True),
        lambda: b.m.strip(10.9, "Code"),
        lambda: b.m.take_summary(),
        lambda: b.m.take_close(),
    ):
        out = step()
        assert not isinstance(out, InsertStep)
    assert b.starts == 0 and not b.m.running and b.m.counts["insert_start"] == 0


# --------------------------------------------------------------------------------------------------------- Send


def inserted(text: str = "hello", *, enter: str = "twice", fps: float = 30.0) -> Bench:
    b = Bench(enter=enter, fps=fps)
    b.fill(text)
    b.insert3(b.t + 0.5)
    b.drain()
    assert b.summaries and b.summaries[-1].outcome == "done"
    return b


def send_taps(b: Bench, t0: float, gap: float = 0.5, n: int = 3) -> list[InsertStep]:
    out: list[InsertStep] = []
    for i in range(n):
        out += b.at(t0 + i * gap, ("enter", ""))
    return out


def test_s51_three_send_taps_after_a_completed_insert_send_one_enter() -> None:
    b = inserted()
    t = b.m.last_insert.t + 1.0  # type: ignore[union-attr]
    out = send_taps(b, t)
    assert len(out) == 1 and out[0].kind == "enter" and out[0].first and out[0].total == 1
    assert out[0].stroke == KeyStroke("control", "enter")
    assert b.typed[-1] == "enter" or b.summaries[-1].kind == "enter"
    assert b.summaries[-1] == InsertSummary("enter", "done", 1, 1, None)
    assert b.m.state == "composing" and b.m.last_insert is None  # single-shot
    # a second Send is refused
    assert send_taps(b, t + 3.0) == [] and b.m.counts["refused_send_none"] >= 1


def test_s51_one_tap_and_two_taps_send_nothing() -> None:
    for n in (1, 2):
        b = inserted()
        t = b.m.last_insert.t + 1.0  # type: ignore[union-attr]
        assert send_taps(b, t, n=n) == []
        assert b.starts == 1  # the Insert only


def test_s51_bounces_and_spread_taps_do_not_send() -> None:
    bounces = inserted()
    t = bounces.m.last_insert.t + 1.0  # type: ignore[union-attr]
    assert send_taps(bounces, t, gap=0.2) == []
    wide = inserted()
    t = wide.m.last_insert.t + 0.5  # type: ignore[union-attr]
    assert send_taps(wide, t, gap=3.1) == []  # 6.2 s from the first tap to the third
    slow = inserted()
    t = slow.m.last_insert.t + 0.8  # type: ignore[union-attr]
    assert len(send_taps(slow, t, gap=0.8)) == 1  # 0.8 s apart is fine


def test_u42_every_send_tap_must_be_within_ten_seconds_of_the_insert() -> None:
    b = inserted()
    t0 = b.m.last_insert.t  # type: ignore[union-attr]
    # first tap at 5 s, second at 7.5 s, the third at 10.2 s: refused
    out = []
    out += b.at(t0 + 5.0, ("enter", ""))
    out += b.at(t0 + 7.5, ("enter", ""))
    assert out == []
    out += b.at(t0 + limits.SEND_WINDOW_S + 0.2, ("enter", ""))
    assert out == [] and b.m.counts["refused_send_old"] == 1
    assert b.m.view(t0 + 11.0, private=False).guard is None
    # the same taps at 5, 7.5 and 9.9 do send
    ok = inserted()
    t0 = ok.m.last_insert.t  # type: ignore[union-attr]
    out = []
    for dt in (5.0, 7.5, 9.9):
        out += ok.at(t0 + dt, ("enter", ""))
    assert len(out) == 1


@pytest.mark.parametrize("between", ["char", "space", "backspace", "clear", "close", "chip", "insert"])
def test_s51_any_other_key_takes_the_send_opportunity_away(between: str) -> None:
    b = inserted()
    t = b.m.last_insert.t + 1.0  # type: ignore[union-attr]
    b.at(t, ("enter", ""))
    b.at(t + 0.5, (between, "a"))  # type: ignore[arg-type]
    assert b.m.last_insert is None
    assert send_taps(b, t + 1.0) == []
    assert b.m.counts["refused_send_none"] >= 1


@pytest.mark.parametrize("how", ["disarm", "hold", "chip_first"])
def test_n47_a_disarm_a_hold_or_a_chip_removes_the_opportunity(how: str) -> None:
    b = inserted()
    t = b.m.last_insert.t + 1.0  # type: ignore[union-attr]
    if how == "disarm":
        b.m.disarm()
    elif how == "hold":
        b.hold = "yield"
        b.at(t)
        b.hold = None
    else:
        b.at(t, ("chip", "0"))
    assert b.m.last_insert is None
    assert send_taps(b, t + 1.0) == []


def test_n47_slow_does_not_take_the_opportunity_but_every_other_hold_does() -> None:
    slow = inserted()
    t = slow.m.last_insert.t + 1.0  # type: ignore[union-attr]
    slow.hold = "slow"
    slow.at(t)
    assert slow.m.last_insert is not None
    for hold in ("blocked", "password", "covered", "overlay", "focus", "yield"):
        b = inserted()
        b.hold = hold  # type: ignore[assignment]
        b.at(b.m.last_insert.t + 0.5)  # type: ignore[union-attr]
        assert b.m.last_insert is None, hold


def test_n47_a_chip_tap_between_the_taps_of_an_armed_send_clears_the_guard() -> None:
    b = inserted()
    t = b.m.last_insert.t + 1.0  # type: ignore[union-attr]
    b.at(t, ("enter", ""))
    b.at(t + 0.5, ("enter", ""))
    b.at(t + 1.0, ("chip", "2"))
    assert b.at(t + 1.5, ("enter", "")) == [] and b.m.last_insert is None


def test_u45_each_condition_of_send_fails_alone_with_its_counter() -> None:
    # enter off
    off = inserted(enter="off")
    send_taps(off, off.m.last_insert.t + 1.0)  # type: ignore[union-attr]
    assert off.m.counts["refused_enter_off"] == 3 and off.m.strip(off.t, "Code") != ""
    # no insert yet
    none = box_of("")
    send_taps(none, 10.0)
    assert none.m.counts["refused_send_none"] == 3
    # box not empty (a text typed after the insert clears the opportunity first; set the state by hand)
    full = inserted()
    full.m.buffer.append("x", full.t)
    full.m.last_insert = LastInsert(full.t, "", 5)  # as if a decoder had edited without clearing it
    assert full.m.tap("enter", "", full.t + 1.0) is None  # no tick before it: the tap checks the box itself
    assert full.m.counts["refused_send_none"] == 1
    # too old
    old = inserted()
    send_taps(old, old.m.last_insert.t + limits.SEND_WINDOW_S + 1.0)  # type: ignore[union-attr]
    assert old.m.counts["refused_send_old"] == 3
    # prefix_risk
    risk = inserted()
    risk.m.prefix_risk = True
    send_taps(risk, risk.m.last_insert.t + 1.0)  # type: ignore[union-attr]
    assert risk.m.counts["refused_send_prefix"] == 3


@pytest.mark.parametrize("text", ["/clear", "  /clear", "/mo"])
def test_u45_a_text_starting_with_a_slash_is_typed_but_never_sent(text: str) -> None:
    b = inserted(text)
    assert "".join(b.typed) == text  # Insert only types
    assert b.m.prefix_risk
    send_taps(b, b.m.last_insert.t + 1.0)  # type: ignore[union-attr]
    assert b.m.counts["refused_send_prefix"] == 3 and b.summaries[-1].kind == "text"
    assert b.m.strip(b.m.last_insert.t + 1.0, "Code") == REVIEW_TEXT["done_slash"].format(n=len(text))  # type: ignore[union-attr]


def test_u45_a_leading_bang_is_refused_as_a_hook() -> None:
    b = inserted("ls")
    b.m.last_insert = LastInsert(b.m.last_insert.t, "!", 3)  # type: ignore[union-attr]
    send_taps(b, b.m.last_insert.t + 1.0)
    assert b.m.counts["refused_send_prefix"] == 3
    assert b.m.strip(b.m.last_insert.t + 1.0, "Code") == REVIEW_TEXT["done_bang"].format(n=3)


def test_n46_a_prefix_stays_for_the_session_even_across_inserts() -> None:
    b = Bench()
    b.fill("/mo")
    b.insert3(b.t + 0.5)
    b.drain()
    b.fill("del", t0=b.t + 1.0)  # a second Insert whose text does not start with a slash
    b.insert3(b.t + 0.5)
    b.drain()
    assert b.m.last_insert is not None and b.m.prefix_risk
    send_taps(b, b.m.last_insert.t + 1.0)
    assert b.m.counts["refused_send_prefix"] == 3
    assert b.m.strip(b.m.last_insert.t + 1.0, "Code") == REVIEW_TEXT["done_prefix"].format(n=3)
    fresh = ReviewMachine(enter="twice")
    assert not fresh.prefix_risk  # a new session clears it


def test_n46_an_aborted_run_that_began_with_a_slash_also_sets_it() -> None:
    b = Bench()
    b.fill("/abc")
    b.results = {2: "hold"}
    b.insert3(b.t + 0.5)
    b.drain()
    assert b.m.state == "aborted" and b.m.prefix_risk


def test_n46_spaces_before_the_slash_do_not_hide_it() -> None:
    b = Bench()
    b.fill("   /x")
    b.insert3(b.t + 0.5)
    b.drain()
    assert b.m.prefix_risk


def test_n45_send_without_an_insert_does_nothing() -> None:
    b = box_of("")
    for n in (2, 10):
        assert send_taps(b, 10.0 + n * 20, n=n) == []
    assert b.starts == 0 and not b.m.running


def test_s51_a_send_for_a_failed_attempt_returns_to_composing_with_a_strip_text() -> None:
    b = inserted()
    b.results = {len(b.steps) + 1: "hold"}
    b.hold_for_step = "focus"
    send_taps(b, b.m.last_insert.t + 1.0)  # type: ignore[union-attr]
    assert b.summaries[-1] == InsertSummary("enter", "aborted", 0, 1, "focus")
    assert b.m.state == "composing" and b.m.last_insert is None
    assert b.m.strip(b.t, "Code") == REVIEW_TEXT["send_failed"].format(why=ABORT_WHY["focus"])


# ------------------------------------------------------------------------------------------------------- the storm


def test_s57_thirty_taps_at_ten_a_second_freeze_once_and_leave_eleven_characters() -> None:
    b = Bench()
    for i in range(30):
        b.at(10.0 + i * 0.1, ("char", "a"))
    assert len(b.box) == limits.STORM_N - 1
    assert b.m.counts["storm_freeze"] == 1 and b.m.counts["frozen"] == 30 - (limits.STORM_N - 1) - 1
    assert b.m.view(10.0 + 2.9, private=False).guard is None
    assert b.m.strip(10.0 + 2.9, "Code") == REVIEW_TEXT["storm"]


def test_s57_the_freeze_lasts_three_seconds_and_taps_work_again_after_it() -> None:
    b = Bench()
    for i in range(limits.STORM_N):
        b.at(10.0 + i * 0.1, ("char", "a"))
    t_freeze = 10.0 + (limits.STORM_N - 1) * 0.1
    assert b.m.counts["storm_freeze"] == 1
    b.at(t_freeze + limits.STORM_FREEZE_S - 0.1, ("char", "b"))
    assert "b" not in b.box
    b.at(t_freeze + limits.STORM_FREEZE_S + 0.1, ("char", "c"))
    assert b.box.endswith("c")
    assert b.m.counts["storm_freeze"] == 1 and b.m.state == "composing"


def test_s57_a_second_burst_freezes_again() -> None:
    b = Bench()
    t = 10.0
    for _burst in range(3):
        for i in range(30):
            b.at(t + i * 0.1, ("char", "a"))
        t += 10.0
    assert b.m.counts["storm_freeze"] == 3


def test_s57_normal_typing_at_five_keys_a_second_never_freezes() -> None:
    b = Bench()
    for i in range(200):
        b.at(10.0 + i * 0.2, ("char", "a"))
    assert b.m.counts["storm_freeze"] == 0 and len(b.box) == 200


def test_s57_a_storm_clears_the_guard_and_the_send_opportunity() -> None:
    b = inserted()
    t = b.m.last_insert.t + 0.5  # type: ignore[union-attr]
    for i in range(limits.STORM_N + 2):
        b.at(t + i * 0.05, ("enter", ""))  # Send taps keep the opportunity until the storm takes it
    assert b.m.counts["storm_freeze"] == 1 and b.m.last_insert is None
    assert b.m.view(t + 1.0, private=False).guard is None
    assert b.m.take_storm()  # the session re-latches the fingers once per freeze
    assert not b.m.take_storm()


def test_storm_stamps_only_count_while_composing() -> None:
    b = start_run("a" * 100)
    for i in range(30):
        b.m.tap("char", "a", b.t + 0.02 * i)
    assert b.m.counts["storm_freeze"] == 0 and b.m.counts["busy"] == 30


# ------------------------------------------------------------------------------------------- views and the strip


def test_s56_private_masks_the_text_and_keeps_the_counts() -> None:
    b = Bench()
    b.fill("hi " + SENTINEL)
    plain = b.m.view(b.t, private=False)
    masked = b.m.view(b.t, private=True)
    assert plain.text == "hi " + SENTINEL and plain.length == 3 + len(SENTINEL)
    assert masked.text == "•" * (3 + len(SENTINEL)) and masked.length == plain.length
    assert SENTINEL not in repr(masked) and SENTINEL not in repr(plain)
    strip = b.m.strip(b.t, "Code")
    assert SENTINEL not in strip and f"{plain.length}/{limits.COMPOSE_MAX}" in strip
    # Insert still works while private
    b.insert3(b.t + 0.5)
    b.drain()
    assert "".join(b.typed) == "hi " + SENTINEL


def test_the_view_reports_the_guard_the_run_and_the_send_key() -> None:
    b = Bench()
    b.fill("hello")
    t = 10.0
    b.at(t, ("insert", ""))
    v = b.m.view(t + 1.5, private=False)
    assert (v.guard, v.guard_taps, v.guard_need) == ("insert", 1, limits.INSERT_TAPS)
    assert v.guard_left == pytest.approx(1.0 - 1.5 / limits.GUARD_MAX_S)
    assert not v.can_send and not v.full and v.state == "composing" and v.sent == 0
    assert v.chips == () and v.chip_active is None
    b.at(t + 0.5, ("insert", ""))
    b.at(t + 1.0, ("insert", ""))
    running = b.m.view(t + 1.0, private=False)
    assert running.state == "inserting" and running.guard is None and running.guard_taps == 0
    b.drain()
    done = b.m.view(b.t, private=False)
    assert done.can_send and done.length == 0 and done.text == ""
    later = b.m.view(b.m.last_insert.t + limits.SEND_WINDOW_S + 0.5, private=False)  # type: ignore[union-attr]
    assert not later.can_send
    b.hold = "yield"
    b.at(b.t + 0.1)
    assert not b.m.view(b.t, private=False).can_send


def test_the_guard_ring_runs_out_with_the_window() -> None:
    b = box_of()
    b.at(10.0, ("clear", ""))
    assert b.m.view(10.0, private=False).guard_left == pytest.approx(1.0)
    assert b.m.view(10.0 + limits.GUARD_MAX_S / 2, private=False).guard_left == pytest.approx(0.5)
    b.at(10.0 + limits.GUARD_MAX_S + 0.1)  # tick: the guard expired
    v = b.m.view(10.0 + limits.GUARD_MAX_S + 0.1, private=False)
    assert v.guard is None and v.guard_left == 0.0 and v.guard_taps == 0


def test_the_view_shows_the_typed_prefix_of_a_run() -> None:
    b = start_run("abcdefghij")
    b.frames(3)
    v = b.m.view(b.t, private=False)
    assert v.state == "inserting" and v.sent == len(b.typed) and v.text == "abcdefghij"


def test_the_view_is_stable_for_a_still_box() -> None:
    b = box_of()
    assert b.m.view(10.0, private=False) == b.m.view(10.0, private=False)


def test_the_strip_follows_the_state_of_the_machine() -> None:
    b = Bench()
    assert b.m.strip(0.0, "Code") == REVIEW_TEXT["hint_empty"]
    b.fill("hello")
    assert b.m.strip(b.t, "Code") == REVIEW_TEXT["counter"].format(target="Code", n=5)
    assert b.m.strip(b.t, "") == REVIEW_TEXT["counter"].format(target="the active window", n=5)
    b.at(10.0, ("insert", ""))
    assert b.m.strip(10.0, "Code") == REVIEW_TEXT["arm_insert_1"].format(n=5, target="Code")
    b.at(10.5, ("insert", ""))
    assert b.m.strip(10.5, "Code") == REVIEW_TEXT["arm_insert_2"].format(target="Code")
    b.at(11.0, ("insert", ""))
    assert b.m.strip(11.0, "Code") == REVIEW_TEXT["inserting"].format(sent=1, total=5, target="Code")
    b.drain()
    done = b.m.strip(b.t, "Code")
    assert done == REVIEW_TEXT["done_send"].format(n=5, target="Code")
    b.at(b.t + limits.SEND_WINDOW_S + 1.0)
    assert b.m.strip(b.t, "Code") == REVIEW_TEXT["hint_empty"]


def start_send_run() -> ReviewMachine:
    m = ReviewMachine(enter="twice")
    m.tap("char", "a", 0.0)
    step = [m.tap("insert", "", 10.0 + i * 0.5) for i in range(3)][-1]
    assert step is not None
    m.note_step(step, "sent", 11.0, None)
    steps = [m.tap("enter", "", 12.0 + i * 0.5) for i in range(3)]
    assert steps[-1] is not None  # not answered yet: the Send is in flight
    return m


def test_the_strip_for_clear_close_send_and_the_refusals() -> None:
    b = box_of("abc")
    b.at(10.0, ("clear", ""))
    assert b.m.strip(10.0, "X") == REVIEW_TEXT["arm_clear"]
    b.at(11.0, ("close", ""))
    assert b.m.strip(11.0, "X") == REVIEW_TEXT["arm_close"].format(n=3)
    e = Bench()
    e.at(1.0, ("insert", ""))
    assert e.m.strip(1.0, "X") == REVIEW_TEXT["empty_insert"]
    e.at(2.0, ("enter", ""))
    assert e.m.strip(2.0, "X") == REVIEW_TEXT["send_none"]
    off = Bench(enter="off")
    off.at(1.0, ("enter", ""))
    assert off.m.strip(1.0, "X") == REVIEW_TEXT["send_off"]
    s = inserted()
    t = s.m.last_insert.t + 1.0  # type: ignore[union-attr]
    s.at(t, ("enter", ""))
    assert s.m.strip(t, "X") == REVIEW_TEXT["arm_send_1"].format(target="X")
    s.at(t + 0.5, ("enter", ""))
    assert s.m.strip(t + 0.5, "X") == REVIEW_TEXT["arm_send_2"].format(target="X")
    s.at(t + 1.0, ("enter", ""))  # the bench answers the step at once: the Send is over in this frame
    assert s.m.strip(t + 1.0, "X") == REVIEW_TEXT["sent"]
    assert start_send_run().strip(13.0, "X") == REVIEW_TEXT["sending"].format(target="X")


def test_the_strip_after_an_abort_names_the_reason_for_the_banner_time() -> None:
    b = Bench()
    b.fill("abcdefghij")
    b.results = {4: "hold"}
    b.hold_for_step = "password"
    b.insert3(b.t + 0.5)
    b.drain()
    expected = REVIEW_TEXT["aborted"].format(sent=3, total=10, why=ABORT_WHY["password"])
    assert b.m.strip(b.t + 1.0, "X") == expected
    b.at(b.t + limits.ABORT_SHOW_S + 1.0)
    assert b.m.strip(b.t, "X") == REVIEW_TEXT["counter"].format(target="X", n=7)


def test_no_text_of_the_box_reaches_a_strip_a_repr_or_a_summary() -> None:
    b = Bench()
    b.fill(SENTINEL)
    pieces = [repr(b.m), repr(b.m.buffer), repr(b.m.counts), b.m.strip(b.t, "Code"), repr(b.m.view(b.t, private=False))]
    b.insert3(b.t + 0.5)
    b.hold = "focus"
    b.frames(3)
    pieces += [repr(b.steps), repr(b.summaries), repr(b.m.counts), repr(b.m.last_insert), b.m.strip(b.t, "Code")]
    pieces += [repr(b.m.view(b.t, private=False)), repr(b.m._run), repr(b.m._guard)]
    for piece in pieces:
        assert SENTINEL not in piece
    # one typed letter is as private as the word: no step or last insert prints it
    for step in b.steps:
        assert "z" not in repr(step).replace("<KeyStroke char>", "")
    assert "!" not in repr(LastInsert(1.0, "!", 3)) and "/" not in repr(LastInsert(1.0, "/", 3))


def test_the_counts_hold_names_and_numbers_never_text() -> None:
    b = Bench()
    b.fill(SENTINEL)
    b.insert3(b.t + 0.5)
    b.drain()
    for key, value in b.m.counts.items():
        assert isinstance(value, int) and SENTINEL not in key and all(c.islower() or c == "_" for c in key), key


# ----------------------------------------------------------------------------------------------------- the seams


class Spy(ReviewMachine):
    def __init__(self, **kw: object) -> None:
        super().__init__(**kw)  # type: ignore[arg-type]
        self.calls: list[str] = []

    def _after_edit(self, kind: KeyKind, ch: str, t: float) -> None:
        self.calls.append(f"after_edit:{kind}")
        super()._after_edit(kind, ch, t)

    def _poll_decoder(self, t: float, hold: Hold | None) -> None:
        self.calls.append("poll")
        super()._poll_decoder(t, hold)

    def _chip_tap(self, index: int, t: float) -> bool:
        self.calls.append(f"chip:{index}")
        return super()._chip_tap(index, t)

    def _undo_correction(self, t: float) -> bool:
        self.calls.append("undo")
        return super()._undo_correction(t)


def test_u82_the_seams_return_not_handled_and_are_called_from_the_named_places() -> None:
    m = ReviewMachine(enter="twice")
    assert m._after_edit("char", "a", 0.0) is None
    assert m._poll_decoder(0.0, None) is None
    assert m._chip_tap(0, 0.0) is False
    assert m._undo_correction(0.0) is False
    spy = Spy(enter="twice")
    b = Bench(machine=spy)
    b.at(1.0, ("char", "a"))
    b.at(1.5, ("space", ""))
    b.at(2.0, ("backspace", ""))
    b.at(2.5, ("chip", "2"))
    b.at(3.0, ("insert", ""))
    assert spy.calls[:2] == ["poll", "after_edit:char"]  # poll at the start of tick, the edit hook after the append
    assert "after_edit:space" in spy.calls and spy.calls.count("undo") == 1 and "chip:2" in spy.calls
    # an edit that is refused (full, refused character) is not an edit
    spy.calls.clear()
    b.at(4.0, ("char", "1"))
    assert "after_edit:char" not in spy.calls


def test_u82_a_machine_with_a_decoder_none_matches_a_spy_on_the_whole_scenario() -> None:
    def scenario(machine: ReviewMachine) -> tuple[object, ...]:
        b = Bench(machine=machine)
        b.fill("hello world")
        b.at(b.t + 1.0, ("backspace", ""))
        b.at(b.t + 1.0, ("chip", "0"))
        b.insert3(b.t + 1.0)
        b.drain()
        send_taps(b, b.m.last_insert.t + 1.0)  # type: ignore[union-attr]
        view = b.m.view(b.t, private=False)
        return (b.typed, b.summaries, dict(b.m.counts), view, b.m.strip(b.t, "T"), b.m.state, b.box)

    assert scenario(ReviewMachine(enter="twice")) == scenario(Spy(enter="twice"))


def test_u82_a_seam_that_handles_a_backspace_keeps_it_out_of_the_box() -> None:
    class Undo(ReviewMachine):
        def _undo_correction(self, t: float) -> bool:
            return True

    b = Bench(machine=Undo(enter="twice"))
    b.m.buffer.append("a", 0.0)
    b.at(1.0, ("backspace", ""))
    assert b.box == "a" and b.m.flash == "ok"


def test_u82_a_chip_seam_that_changed_the_box_clears_nothing_it_should_not() -> None:
    class Chips(ReviewMachine):
        def _chip_tap(self, index: int, t: float) -> bool:
            return self.buffer.append("x", t) == "ok"

    b = Bench(machine=Chips(enter="twice"))
    b.at(1.0, ("chip", "0"))
    assert b.box == "x" and b.m.counts["chip_inert"] == 0 and b.m.flash == "ok"


# ------------------------------------------------------------------------------------------- oracles and fuzz


def finish_run(m: ReviewMachine, first: InsertStep, t: float) -> float:
    """Answers every step of a run ``sent`` at 25 fps; returns the time after it."""
    step: InsertStep | None = first
    while step is not None:
        m.note_step(step, "sent", t, None)
        t += 0.04
        step = m.tick(t, None)
    assert not m.running and m.take_summary() is not None
    return t


GAPS = (0.05, 0.1, 0.2, 0.26, 0.3, 0.5, 0.8, 1.5, 3.1, 6.5)
HOLDS: tuple[Hold | None, ...] = (None,) * 7 + ("slow", "overlay", "yield")


def fuzz_sequence(rng: random.Random, kinds: tuple[KeyKind, ...]) -> int:
    """One random sequence through a fresh machine; every run start is checked against an independent oracle.

    The oracle sees only what the sequence did: it counts Insert taps, resets on any other key, on a hold, on a freeze
    and on a box that is empty or only spaces, drops a count older than the window and ignores a tap closer than the
    minimum to the last counted one. A run may start exactly when it reaches the third. Returns the runs started.
    """
    m = ReviewMachine(enter="twice")
    t = rng.uniform(0.0, 5.0)
    count, t_first, t_last = 0, 0.0, 0.0
    starts = 0
    for _ in range(rng.randrange(4, 30)):
        t += rng.choice(GAPS)
        hold = rng.choice(HOLDS)
        assert m.tick(t, hold) is None
        if hold is not None:
            count = 0  # the session drops the tap; the hold clears the guard
            continue
        kind = rng.choice(kinds)
        text = m.buffer.text()
        frozen_before = (m.counts["storm_freeze"], m.counts["frozen"])
        out = m.tap(kind, rng.choice("abc") if kind == "char" else "", t)
        if (m.counts["storm_freeze"], m.counts["frozen"]) != frozen_before:
            assert out is None
            count = 0
            continue
        if kind != "insert":
            count = 0
            if out is not None:
                assert kind == "enter" and out.kind == "enter" and m.counts["insert_done"] > 0
                t = finish_run(m, out, t)
            continue
        if not text.strip(" "):
            assert out is None
            count = 0
            continue
        if count == 0 or t - t_first > limits.GUARD_MAX_S:
            count, t_first, t_last = 1, t, t
        elif t - t_last >= limits.GUARD_MIN_S:
            count, t_last = count + 1, t
        assert (out is not None) == (count == limits.INSERT_TAPS), (count, t - t_first)
        if out is not None:
            starts += 1
            count = 0
            t = finish_run(m, out, t) + 1.0
    return starts


def test_n41_every_run_start_is_exactly_three_counted_insert_taps_in_a_valid_window() -> None:
    rng = random.Random(41)
    biased: tuple[KeyKind, ...] = ("insert",) * 6 + ("char", "space", "backspace", "chip", "clear", "close", "enter")
    starts = sum(fuzz_sequence(rng, biased) for _ in range(FUZZ_SEQUENCES))
    assert starts > 100  # the oracle saw many runs, so the "and never otherwise" half is not vacuous


FUZZ_SEQUENCES = 100_000


def test_n42_a_hold_during_the_second_tap_never_leads_to_a_run() -> None:
    for hold in ("overlay", "yield", "focus", "blocked", "password", "covered", "slow"):
        b = box_of()
        b.at(10.0, ("insert", ""))
        b.hold = hold  # type: ignore[assignment]
        b.at(10.5)  # the frame of the second tap: the session drops the tap, the machine sees only the hold
        b.hold = None
        b.at(11.0, ("insert", ""))
        assert b.starts == 0, hold
        b.at(11.5, ("insert", ""))
        assert b.starts == 0, hold
        b.at(12.0, ("insert", ""))
        assert b.starts == 1  # the guard that was armed after the hold completes on its own three taps


def test_n43_three_bounces_never_run() -> None:
    for fps in (15.0, 30.0, 60.0):
        b = Bench(fps=fps)
        b.fill("hello")
        frames = int(0.2 * fps)
        for _ in range(3):
            b.frame(("insert", ""))
            b.frames(frames - 1)
        assert b.starts == 0, fps


def test_n44_an_empty_or_space_only_box_never_runs() -> None:
    for text in ("", " ", "      "):
        b = Bench()
        b.fill(text)
        for i in range(10):
            b.at(10.0 + i * 0.6, ("insert", ""))
        assert b.starts == 0 and b.m.counts["insert_start"] == 0


def test_n40_phantom_taps_for_ten_minutes_never_run() -> None:
    rng = random.Random(0xA1)
    for rate in (20, 60):
        m = ReviewMachine(enter="twice")
        for ch in "phantom":
            m.tap("char", ch, 0.5)
        t = 100.0
        end = t + 600.0
        while t < end:
            t += rng.expovariate(rate / 60.0)
            burst = 1 if rng.random() > 0.15 else rng.randrange(2, 6)
            for _ in range(burst):
                kind = rng.choice(PHANTOM_KEYS)
                assert m.tick(t, None) is None
                out = m.tap(kind, rng.choice("abcdef") if kind == "char" else "", t)
                assert out is None
                t += rng.uniform(0.02, 0.2)
        assert m.counts["insert_start"] == 0 and m.counts["send_start"] == 0


# --------------------------------------------------------------------------- the machine as a whole, small checks


def test_construct_with_enter_off_and_the_documented_attributes() -> None:
    m = ReviewMachine(enter="off")
    assert m.state == "composing" and len(m.buffer) == 0 and not m.running
    assert m.take_summary() is None and not m.take_close() and not m.take_storm()
    assert m.last_insert is None and not m.prefix_risk and m.flash is None
    assert repr(m) == "<ReviewMachine state=composing len=0>"


def test_tap_on_an_unknown_kind_is_dropped_not_raised() -> None:
    m = ReviewMachine(enter="twice")
    assert m.tap("shift", "", 1.0) is None  # the session keeps Shift, Lang, Priv and Home
    assert m.counts["not_review_key"] == 1 and m.flash == "drop"


def test_the_last_insert_record_keeps_a_count_and_a_class_of_first_character_only() -> None:
    b = inserted("hello")
    li = b.m.last_insert
    assert li is not None and li.chars == 5 and li.first == ""
    s = inserted("/hi")
    assert s.m.last_insert is not None and s.m.last_insert.first == "/"


def test_has_summary_peeks_without_taking_it() -> None:
    """The session never starts a run in a frame whose previous run ended in ``tick`` and whose summary is untaken."""
    b = start_run("abc")
    m = b.m
    assert not m.has_summary
    b.hold = "focus"
    t = b.t + b.dt
    assert m.tick(t, "focus") is None  # the hold ends the run inside tick, as the sink's answer would not have
    assert not m.running and m.has_summary
    assert m.has_summary  # a peek: still there
    assert m.take_summary() is not None
    assert not m.has_summary and m.take_summary() is None


def test_a_new_run_replaces_nothing_because_the_session_waits_for_the_summary() -> None:
    """The machine itself drops an untaken summary when a run begins (``_begin``): the wait is the session's duty."""
    b = start_run("ab")
    m = b.m
    t = b.t + b.dt
    m.tick(t, "yield")
    assert m.has_summary
    m.buffer.append("c", t)
    b.insert3(t + 0.5)  # a second run before anyone took the summary
    assert m.running and not m.has_summary  # the first summary is gone: exactly what the session must not allow
