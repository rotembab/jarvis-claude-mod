"""The review machine: taps edit the box, three taps insert it, three taps send Enter (DESIGN-KEYBOARD.md 2.13).

Pure: no I/O, no clock (every time is an argument), no thread. A tap can only change the box. The only way a stroke
leaves this module is the first step of a run, returned from the one ``tap`` that confirms a guard (SR21, SR27); every
later step comes out of ``tick``, one per frame, until the run is done, a hold, a timeout, a Stop tap or a failed step
ends it. The box is untouched while a run is active and the typed prefix leaves it when the run ends, so a re-Insert
types exactly the remainder (SR24).

Beyond the pinned API the session needs six things the design leaves to it, all counters, flags or strings and never
the text: ``flash`` (the verdict on the latest tap, for the key's colour), ``take_close`` (a confirmed Close),
``take_storm`` (a freeze began: the session resets the press method), ``strip`` (the fixed sentence for the strip,
Appendix F.2), ``last_insert`` and ``prefix_risk`` (the Send opportunity, 2.13.6). Every step the machine returns must
be answered by exactly one ``note_step``; the controller does that in the frame it gets the step (3.8).

The module imports ``types``, ``limits``, ``compose``, ``desktop.keys`` and ``overlay.base`` (for the ``ComposeView`` it
builds), reaches a decoder only through the ``Decoder`` protocol, and never logs, raises with or prints the box, a plan
or a step.
"""

from __future__ import annotations

import math
from collections import Counter, deque
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final, Literal

from ..desktop.keys import KeyStroke
from ..overlay.base import ComposeView
from .compose import ComposeBuffer, insert_check
from .limits import (
    ABORT_SHOW_S,
    COMPOSE_MAX,
    GUARD_MAX_S,
    GUARD_MIN_S,
    INSERT_GAP_S,
    INSERT_MAX_S,
    INSERT_TAPS,
    SEND_REFUSE_FIRST,
    SEND_TAPS,
    SEND_WINDOW_S,
    STOP_ARM_S,
    STORM_FREEZE_S,
    STORM_N,
    STORM_S,
)
from .types import Decoder, GuardKind, Hold, InsertAbort, InsertResult, KeyKind, ReviewState, RunKind, Touch

#: Which key starts which guard, and how many taps each needs. INSERT_TAPS and SEND_TAPS are floors of 3 (limits.py).
GUARD_OF_KEY = {"insert": "insert", "clear": "clear", "enter": "send", "close": "close"}
GUARD_TAPS = {"insert": INSERT_TAPS, "clear": 2, "send": SEND_TAPS, "close": 2}
GUARD_WINDOW = {"insert": GUARD_MAX_S, "clear": GUARD_MAX_S, "send": GUARD_MAX_S, "close": GUARD_MAX_S}

#: The fixed sentences of the strip (Appendix F.2). Nothing interpolates the box, a key or a window title: ``{n}``,
#: ``{sent}``, ``{total}`` and ``{s}`` are numbers, ``{target}`` is a base name or "the active window", ``{why}`` comes
#: from ``ABORT_WHY``. A change is a contract change. ``send_failed`` is the one sentence the design names a place for
#: (a failed Send "shows a strip text") and not the words.
REVIEW_TEXT: Final = MappingProxyType(
    {
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
        "send_failed": "Enter not pressed ({why}).",
    }
)

#: ``{why}`` of the aborted sentence: the table of 3.12.
ABORT_WHY: Final = MappingProxyType(
    {
        "focus": "the window changed",
        "yield": "you used the keyboard or mouse",
        "blocked": "that window cannot be typed into",
        "password": "that looks like a password box",
        "covered": "the screen is covered",
        "overlay": "the keyboard could not be drawn",
        "stopped": "you stopped it",
        "timeout": "it took too long",
        "failed": "Windows would not take the keys",
    }
)

#: What the strip says when the sink could not name the window yet (Appendix F: ``{target}``).
UNKNOWN_TARGET: Final = "the active window"
#: The holds that end a run. ``slow`` is the session's own and does not: a run does not use the hands.
ABORT_HOLDS: Final[frozenset[str]] = frozenset({"blocked", "password", "covered", "overlay", "focus", "yield"})
#: How long a refusal or an outcome stays on the strip. Display only: the design pins no number.
NOTE_SHOW_S: Final = 4.0
#: One per character, spaces too, in a masked view (the overlay's own bullet).
BULLET: Final = "•"


@dataclass(frozen=True)
class InsertStep:
    #: ``KeyStroke("char", c)`` or ``KeyStroke("control", "space" | "enter")``.
    stroke: KeyStroke
    #: 0-based position in the run.
    index: int
    #: Run length, 1..COMPOSE_MAX.
    total: int
    #: The controller must call ``sink.begin_run`` before sending this step.
    first: bool
    #: Run id, increments per run.
    run: int
    kind: Literal["text", "enter"]


@dataclass(frozen=True)
class InsertSummary:
    """What ended a run; counts and enums only."""

    kind: Literal["text", "enter"]
    outcome: Literal["done", "aborted"]
    sent: int
    of: int
    reason: InsertAbort | None = None


@dataclass(frozen=True)
class LastInsert:
    """The finished text run a Send hangs on (2.13.6). ``first`` is the one thing about the text that is kept."""

    t: float
    #: "/" or "!" when the first non-space character was one of them, else "". A character of the text: out of the repr.
    first: str = field(repr=False)
    chars: int


@dataclass
class _Guard:
    kind: GuardKind
    t_first: float
    t_last: float
    n: int
    #: The box version when armed: any change of the box since voids the guard.
    version: int


@dataclass(repr=False)
class _Run:
    id: int
    kind: RunKind
    plan: tuple[KeyStroke, ...]
    total: int
    t0: float
    #: When the latest step went out: the pace.
    last: float
    #: "/" or "!" when the text began with one of them (``LastInsert.first``).
    first: str
    sent: int = 0
    #: The step handed out and not yet answered; at most one.
    outstanding: InsertStep | None = None
    #: A Stop tap arrived while a step was out: it takes effect once that step is answered.
    stop: bool = False

    def __repr__(self) -> str:
        return f"<run {self.id} {self.sent}/{self.total}>"


class ReviewMachine:
    state: ReviewState
    buffer: ComposeBuffer
    #: Reasons and outcomes by name; never text.
    counts: Counter[str]
    #: A text run finished ``done`` and nothing but Send has happened since (2.13.6); None otherwise.
    last_insert: LastInsert | None
    #: A text run of this session began with "/" or "!": Send is refused for the rest of the session.
    prefix_risk: bool
    #: The verdict on the latest tap: "ok" it was taken, "drop" it was refused, None it was a bounce.
    flash: Literal["ok", "drop"] | None

    def __init__(self, *, enter: Literal["twice", "off"], decoder: Decoder | None = None) -> None:
        self.state = "composing"
        self.buffer = ComposeBuffer()
        self.counts = Counter()
        self.last_insert = None
        self.prefix_risk = False
        self.flash = None
        self._enter = enter
        # Step 1 never reads it: the decoder's four seams below are no-ops and nothing else touches it (3.16).
        self._decoder = decoder
        self._guard: _Guard | None = None
        self._run: _Run | None = None
        self._runs = 0
        self._summary: InsertSummary | None = None
        self._close = False
        self._storm = False
        self._stamps: deque[float] = deque()
        self._frozen_until = -math.inf
        self._aborted_at = -math.inf
        #: (sent, total, reason) of the latest aborted text run: the banner after it.
        self._abort: tuple[int, int, InsertAbort] | None = None
        #: (key, until, why) of a refusal or an outcome the strip shows for a few seconds.
        self._note: tuple[str, float, str] | None = None
        #: The box version the guard and the opportunity were last checked against.
        self._seen = 0

    def __repr__(self) -> str:
        return f"<ReviewMachine state={self.state} len={len(self.buffer)}>"

    @property
    def running(self) -> bool:
        return self._run is not None

    # ------------------------------------------------------------------------------------------------------- taps

    def tap(self, kind: KeyKind, ch: str, t: float, *, touch: Touch | None = None) -> InsertStep | None:
        """Precondition: no hold, session phase is typing. Returns the first step when this tap starts a run."""
        self.flash = None
        self._note = None
        if self._run is not None:
            return self._tap_busy(kind, t)
        # One rule for the Send opportunity (2.13.5): every tap that reaches the machine on a key other than Send takes
        # it away, whatever else the tap does. A guard survives only taps on its own key.
        if kind != "enter":
            self.last_insert = None
        if self._guard is not None and self._guard.kind != GUARD_OF_KEY.get(kind):
            self._guard = None
        if t < self._frozen_until:
            self.counts["frozen"] += 1
            self.flash = "drop"
            self._guard = None
            return None
        if self._storm_begins(t):
            return None
        if kind in ("char", "space"):
            self._tap_text(kind, " " if kind == "space" else ch, t, touch)
        elif kind == "backspace":
            self._tap_backspace(t)
        elif kind == "clear":
            self._tap_clear(t)
        elif kind == "insert":
            return self._tap_insert(t)
        elif kind == "enter":
            return self._tap_enter(t)
        elif kind == "close":
            self._tap_close(t)
        elif kind == "chip":
            self._tap_chip(ch, t)
        else:
            # Shift, Lang, Priv and Home are the session's; they reach the machine only through disarm().
            self.counts["not_review_key"] += 1
            self.flash = "drop"
        return None

    def _tap_busy(self, kind: KeyKind, t: float) -> None:
        """While a run is active every tap is discarded, bar a Stop on Insert once STOP_ARM_S has passed."""
        run = self._run
        assert run is not None
        if kind == "insert" and t - run.t0 >= STOP_ARM_S:
            self.counts["stop_tap"] += 1
            self.flash = "ok"
            if run.outstanding is not None:
                run.stop = True  # that step is already out: let its answer count, then stop
            else:
                self._finish(t, "aborted", "stopped")
            return None
        self.counts["busy"] += 1
        self.flash = "drop"
        return None

    def _storm_begins(self, t: float) -> bool:
        """The STORM_N-th tap within STORM_S, and every tap for STORM_FREEZE_S after it, are dropped (R12)."""
        stamps = self._stamps
        while stamps and t - stamps[0] > STORM_S:
            stamps.popleft()
        if len(stamps) + 1 < STORM_N:
            stamps.append(t)
            return False
        stamps.clear()
        self._frozen_until = t + STORM_FREEZE_S
        self._guard = None
        self.last_insert = None
        self._storm = True
        self.counts["storm_freeze"] += 1
        self.flash = "drop"
        return True

    def _edited(self) -> None:
        """An accepted change of the box: back to composing, nothing pending."""
        self.state = "composing"
        self._guard = None
        self._seen = self.buffer.version

    def _tap_text(self, kind: KeyKind, c: str, t: float, touch: Touch | None) -> None:
        verdict = self.buffer.append(c, t, touch)
        if verdict == "ok":
            self._edited()
            self.flash = "ok"
            self._after_edit(kind, c, t)
        elif verdict == "full":
            self.counts["full"] += 1
            self.flash = "drop"
            self._set_note("full", t)
        else:
            self.counts["refused_char"] += 1
            self.flash = "drop"

    def _tap_backspace(self, t: float) -> None:
        # The decoder's undo comes first: a Backspace it consumed is not an edit of the end of the box.
        if self._undo_correction(t) or self.buffer.backspace(t):
            self._edited()
            self.flash = "ok"
        else:
            self.counts["empty"] += 1
            self.flash = "drop"

    def _tap_clear(self, t: float) -> None:
        if len(self.buffer) == 0:
            self.counts["refused_empty"] += 1
            self.flash = "drop"
            self._guard = None
            return
        if self._count_guard("clear", t):
            self.buffer.clear(t)
            self.counts["cleared"] += 1
            self._edited()

    def _tap_insert(self, t: float) -> InsertStep | None:
        why = insert_check(self.buffer.text(), alphabet=self.buffer.alphabet)
        if why is not None:
            self.counts[f"refused_{why}"] += 1
            self.flash = "drop"
            self._guard = None
            if why == "empty":
                self._set_note("empty_insert", t)
            return None
        if self._count_guard("insert", t):
            return self._start_text(t)
        return None

    def _tap_enter(self, t: float) -> InsertStep | None:
        why = self._send_refusal(t)
        if why is not None:
            self.counts[why] += 1
            self.flash = "drop"
            self._guard = None
            if why == "refused_enter_off":
                self._set_note("send_off", t)
            elif why != "refused_send_prefix":  # a prefix refusal is already said by the strip's `done_*` sentence
                self._set_note("send_none", t)
            return None
        if self._count_guard("send", t):
            self.last_insert = None  # single-shot: an attempt that reaches the sink consumes the opportunity
            return self._start_enter(t)
        return None

    def _tap_close(self, t: float) -> None:
        if len(self.buffer) == 0 or self._count_guard("close", t):
            self._guard = None
            self._close = True
            self.counts["close_key"] += 1
            self.flash = "ok"

    def _tap_chip(self, ch: str, t: float) -> None:
        index = int(ch) if ch.isascii() and ch.isdigit() else 0
        if self._chip_tap(index, t):
            self._edited()
            self.flash = "ok"
        else:
            self.counts["chip_inert"] += 1
            self.flash = "drop"

    # ----------------------------------------------------------------------------------------------------- guards

    def _count_guard(self, kind: GuardKind, t: float) -> bool:
        """One tap on a guarded key (2.13.4): True when it confirms. Sets ``flash``: a bounce has none."""
        g = self._guard
        if g is None or g.kind != kind or t - g.t_first > GUARD_WINDOW[kind] or g.version != self.buffer.version:
            self._guard = _Guard(kind, t, t, 1, self.buffer.version)
            self.flash = "ok"
            return False
        if t - g.t_last < GUARD_MIN_S:
            self.counts["bounce"] += 1
            return False
        g.n += 1
        g.t_last = t
        self.flash = "ok"
        if g.n < GUARD_TAPS[kind]:
            return False
        self._guard = None
        return True

    def _live_guard(self, t: float) -> _Guard | None:
        g = self._guard
        if g is None or g.version != self.buffer.version or t - g.t_first > GUARD_WINDOW[g.kind]:
            return None
        return g

    def disarm(self) -> None:
        """Shift, Lang, Priv, Home taps; phase changes: clears the guard and the Send opportunity, never a run (F14)."""
        self._guard = None
        self.last_insert = None

    # --------------------------------------------------------------------------------------------------- the run

    def _start_text(self, t: float) -> InsertStep | None:
        """The only way a text run begins; called from exactly one place, the confirming Insert tap (SR27, S50)."""
        text = self.buffer.text()
        if insert_check(text, alphabet=self.buffer.alphabet) is not None:
            return None
        plan = tuple(KeyStroke("control", "space") if c == " " else KeyStroke("char", c) for c in text)
        stripped = text.lstrip(" ")
        first = stripped[0] if stripped[0] in SEND_REFUSE_FIRST else ""
        self.prefix_risk = self.prefix_risk or bool(first)
        self.last_insert = None
        self.counts["insert_start"] += 1
        return self._begin("text", plan, t, first)

    def _start_enter(self, t: float) -> InsertStep | None:
        """A Send: one step, the Enter key, ``first`` so that the controller begins an ``again`` run (3.6.1)."""
        self.counts["send_start"] += 1
        return self._begin("enter", (KeyStroke("control", "enter"),), t, "")

    def _begin(self, kind: RunKind, plan: tuple[KeyStroke, ...], t: float, first: str) -> InsertStep:
        self._runs += 1
        self._guard = None
        self._summary = None
        self.state = "inserting"
        step = InsertStep(plan[0], 0, len(plan), True, self._runs, kind)
        self._run = _Run(self._runs, kind, plan, len(plan), t, t, first, outstanding=step)
        return step

    def tick(self, t: float, hold: Hold | None) -> InsertStep | None:
        """Once per frame, in every phase, right after the hold is known and before ``tap`` (2.7 step 3)."""
        self._poll_decoder(t, hold)
        if self.buffer.version != self._seen:  # the box changed behind a tap's back (the decoder's way in)
            self._guard = None
            self.last_insert = None
            self._seen = self.buffer.version
        if hold is not None:
            self._guard = None
            if hold != "slow":
                self.last_insert = None
        g = self._guard
        if g is not None and t - g.t_first > GUARD_WINDOW[g.kind]:
            self._guard = None
        if self.state == "aborted" and t - self._aborted_at >= ABORT_SHOW_S:
            self.state = "composing"
        run = self._run
        if run is None:
            return None
        if run.outstanding is not None:
            # The controller never answered the last step: it may have been typed, so it counts as sent (never typed
            # twice) and the run stops.
            self.counts["step_unanswered"] += 1
            run.outstanding = None
            run.sent += 1
            self._finish(t, "done" if run.sent >= run.total else "aborted", None if run.sent >= run.total else "failed")
            return None
        if hold in ABORT_HOLDS:
            self._finish(t, "aborted", hold)  # type: ignore[arg-type]
            return None
        if t - run.t0 > INSERT_MAX_S:
            self._finish(t, "aborted", "timeout")
            return None
        if t - run.last < INSERT_GAP_S:
            return None
        step = InsertStep(run.plan[run.sent], run.sent, run.total, False, run.id, run.kind)
        run.last = t
        run.outstanding = step
        return step

    def note_step(self, step: InsertStep, result: InsertResult, t: float, hold: Hold | None) -> None:
        """The sink's answer to ``step``. ``hold`` is why a ``hold`` answer happened (``sink.last_hold``)."""
        run = self._run
        if run is None or run.outstanding is None or step.run != run.id or step.index != run.sent:
            self.counts["step_stale"] += 1  # a step of a run that a close already dropped
            return
        run.outstanding = None
        if result in ("sent", "maybe"):
            run.sent += 1
            if run.sent >= run.total:
                self._finish(t, "done", None)
            elif result == "maybe":
                self._finish(t, "aborted", "failed")  # a stroke that may have been typed counts; the run stops
            elif run.stop:
                self._finish(t, "aborted", "stopped")
        elif result == "hold":
            self._finish(t, "aborted", hold if hold in ABORT_HOLDS else "focus")  # type: ignore[arg-type]
        else:
            self._finish(t, "aborted", "failed")

    def _finish(self, t: float, outcome: Literal["done", "aborted"], reason: InsertAbort | None) -> None:
        run = self._run
        assert run is not None
        self._run = None
        sent = run.sent
        if run.kind == "text":
            if outcome == "done":
                self.buffer.consume(run.total, t)
                self._seen = self.buffer.version
                self.last_insert = LastInsert(t, run.first, run.total)
                self.state = "composing"
                self.counts["insert_done"] += 1
            else:
                self.buffer.consume(sent, t)  # the typed prefix leaves the box, the remainder stays
                self._seen = self.buffer.version
                self.last_insert = None
                self.state = "aborted"
                self._aborted_at = t
                assert reason is not None
                self._abort = (sent, run.total, reason)
                self.counts["insert_aborted"] += 1
                self.counts[f"abort_{reason}"] += 1
        else:
            self.state = "composing"
            self.last_insert = None
            if outcome == "done":
                self.counts["send_done"] += 1
                self._set_note("sent", t)
            else:
                assert reason is not None
                self.counts["send_aborted"] += 1
                self._set_note("send_failed", t, reason)
        self._summary = InsertSummary(run.kind, outcome, sent, run.total, reason)

    def take_summary(self) -> InsertSummary | None:
        """The run that ended since the last call, once."""
        summary, self._summary = self._summary, None
        return summary

    @property
    def has_summary(self) -> bool:
        """A run ended and nobody has taken its summary: the controller does that after the frame (3.8). A new run
        replaces an untaken summary, so the session starts none, and runs no fallback switch, until it is taken."""
        return self._summary is not None

    def take_close(self) -> bool:
        """A Close was confirmed (immediately with an empty box): the session closes ``close_key``. Once."""
        asked, self._close = self._close, False
        return asked

    def take_storm(self) -> bool:
        """A storm freeze began: the session re-latches every finger (``press.reset()``). Once per freeze."""
        began, self._storm = self._storm, False
        return began

    def frozen(self, t: float) -> bool:
        """The storm freeze is on at ``t``: the session keeps its sentence on the strip over any hint (2.7 step 8)."""
        return t < self._frozen_until

    def discard(self) -> int:
        """Drops the box, the run, the guard and the last insert; returns how many characters were dropped.

        Characters a run already typed are in the window and are not counted: only what the box still owed.
        """
        run = self._run
        dropped = len(self.buffer) - (run.sent if run is not None else 0)
        self.buffer.clear(self.buffer.last_edit_t)
        self._seen = self.buffer.version
        self._run = None
        self._guard = None
        self.last_insert = None
        self._summary = None
        self._close = False
        self.state = "composing"
        return max(dropped, 0)

    # ------------------------------------------------------------------------------------------------- Send rules

    def _send_refusal(self, t: float) -> str | None:
        """The counter that names why Send is not available now (2.13.6), or None. Checked at every Send tap."""
        if self._enter != "twice":
            return "refused_enter_off"
        last = self.last_insert
        if last is None or self._run is not None or len(self.buffer) != 0:
            return "refused_send_none"
        if t - last.t > SEND_WINDOW_S:
            return "refused_send_old"
        if self.prefix_risk or last.first in SEND_REFUSE_FIRST:
            return "refused_send_prefix"
        return None

    # --------------------------------------------------------------------------------------------- view and strip

    def view(self, t: float, *, private: bool) -> ComposeView:
        text = self.buffer.text()
        run = self._run
        g = self._live_guard(t)
        return ComposeView(
            text=BULLET * len(text) if private else text,
            length=len(text),
            state=self.state,
            sent=run.sent if run is not None else 0,
            guard=g.kind if g is not None else None,
            guard_taps=g.n if g is not None else 0,
            guard_need=GUARD_TAPS[g.kind] if g is not None else 0,
            guard_left=max(0.0, 1.0 - (t - g.t_first) / GUARD_WINDOW[g.kind]) if g is not None else 0.0,
            can_send=self._send_refusal(t) is None,
            full=len(text) >= COMPOSE_MAX,
            chips=(),
            chip_active=None,
        )

    def _set_note(self, key: str, t: float, why: str = "") -> None:
        self._note = (key, t + NOTE_SHOW_S, why)

    def strip(self, t: float, target: str) -> str:
        """The fixed sentence for the strip now (F.2): numbers and the target name only, never a box character."""
        where = target or UNKNOWN_TARGET
        text = REVIEW_TEXT
        if t < self._frozen_until:
            return text["storm"]
        g = self._live_guard(t)
        if g is not None:
            return self._guard_text(g, where)
        run = self._run
        if run is not None:
            if run.kind == "enter":
                return text["sending"].format(target=where)
            return text["inserting"].format(sent=run.sent, total=run.total, target=where)
        if self._note is not None and t <= self._note[1]:
            key, _, why = self._note
            return text[key].format(why=ABORT_WHY.get(why, ""), target=where)
        if self.state == "aborted" and self._abort is not None:
            sent, total, reason = self._abort
            return text["aborted"].format(sent=sent, total=total, why=ABORT_WHY[reason])
        last = self.last_insert
        if last is not None and t - last.t <= SEND_WINDOW_S:
            return self._done_text(last, where)
        n = len(self.buffer)
        if n == 0:
            return text["hint_empty"]
        return text["counter"].format(target=where, n=n)

    def _guard_text(self, g: _Guard, where: str) -> str:
        text = REVIEW_TEXT
        n = len(self.buffer)
        if g.kind == "insert":
            key = "arm_insert_1" if g.n == 1 else "arm_insert_2"
            return text[key].format(n=n, target=where)
        if g.kind == "send":
            key = "arm_send_1" if g.n == 1 else "arm_send_2"
            return text[key].format(target=where)
        if g.kind == "clear":
            return text["arm_clear"]
        return text["arm_close"].format(n=n)

    def _done_text(self, last: LastInsert, where: str) -> str:
        text = REVIEW_TEXT
        why = self._send_refusal(last.t)
        if why is None:
            return text["done_send"].format(n=last.chars, target=where)
        if why == "refused_send_prefix":
            key = {"/": "done_slash", "!": "done_bang"}.get(last.first, "done_prefix")
            return text[key].format(n=last.chars)
        return text["done"].format(n=last.chars, target=where)

    # ---------------------------------------------------- the decoder's four seams (3.16.2, H5): inert in step 1

    def _after_edit(self, kind: KeyKind, ch: str, t: float) -> None:
        """End of ``tap`` for char and space, after the append and the guard reset."""

    def _poll_decoder(self, t: float, hold: Hold | None) -> None:
        """Start of ``tick``, before the guard expiry."""

    def _chip_tap(self, index: int, t: float) -> bool:
        """A tap on chip ``index``: True when it changed the box. Step 1 has no chips."""
        return False

    def _undo_correction(self, t: float) -> bool:
        """First thing a Backspace does: True when it was consumed by an undo of a correction."""
        return False
