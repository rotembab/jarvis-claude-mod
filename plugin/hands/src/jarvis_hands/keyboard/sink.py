"""The last gate before Windows: the key lane and the run lane (DESIGN-KEYBOARD.md 3.6).

It does not depend on the session, and a bug in the session cannot exceed its limits: at most 80 characters in 2 s, 600
characters and 12 runs in any minute, one run at a time and 0.5 s between runs; the first breaker hit sets ``runaway``
and the controller closes the session. It has two lanes, each dead in the other commit mode: the key lane (``send``,
``direct``) and the run lane (``begin_run``, ``send_run``, ``end_run``, ``review``). The lanes never share a breaker.

Pure: the desktop it is given is the only thing it talks to, time comes from the ``now`` of every call (the frame's
clock) and, for the duration of one ``send_keys`` call, from the injected ``clock``. It keeps no character in a counter,
an exception or a message: whatever a desktop raises is read for its type and dropped (SR13, SR26).
"""

from __future__ import annotations

import math
from collections import Counter, deque
from collections.abc import Callable
from typing import Final, Literal

from ..desktop.base import InputBlocked, KeyDesktop, KeyTarget
from ..desktop.keys import ALLOWED_CHARS, KeyRefused, KeyStroke
from .limits import (
    BACKSTOP_N,
    BACKSTOP_S,
    COMPOSE_MAX,
    FOCUS_SETTLE_S,
    FOREIGN_FAIL_CLOSE,
    INSERT_BACKSTOP_N,
    INSERT_BACKSTOP_S,
    RUN_COOLDOWN_S,
    RUN_MAX_CHARS_PER_MIN,
    RUN_MAX_PER_MIN,
    SEND_FAIL_CLOSE,
    SEND_SLOW_S,
    SEND_WINDOW_S,
    SINK_WARMUP_S,
    YIELD_S,
)
from .types import Commit, Hold, InsertResult, SendResult

#: The target is read again at most this often by ``gate``; a run reads it fresh before every character regardless.
TARGET_REFRESH_S: Final = 0.1
#: After a send the desktop refused, ``gate`` reports ``blocked`` for this long.
BLOCKED_AFTER_REFUSAL_S: Final = 1.0
#: The budgets of the run lane are counted over any window of this length.
BUDGET_WINDOW_S: Final = 60.0
#: How long after a completed run an ``again`` run (Send) may still begin: the Send window and a second's grace.
AGAIN_GRACE_S: Final = 1.0
#: What an unreadable target is read as: no window at all.
NO_TARGET: Final = KeyTarget(0, 0, "", 0, "none", False, False)


class SinkFailed(RuntimeError):
    """``SEND_FAIL_CLOSE`` failures in a row: the controller closes the session ``input_blocked``."""


class KeySink:
    #: Shown in the strip; "" until the first gate.
    target_name: str
    #: A backstop tripped.
    runaway: bool
    #: Why the last ``begin_run`` answered False or the last ``send_run`` answered "hold".
    last_hold: Hold | None
    run_active: bool
    #: sent, hold, limited, failed, slow_send, lane_violation, bad_stroke, no_gate, begin_no_gate, run_cooldown,
    #: run_budget (and a few more, all names of reasons): never a character.
    counts: Counter[str]

    def __init__(
        self,
        desktop: KeyDesktop,
        *,
        inject: Literal["unicode", "vk"],
        clock: Callable[[], float],
        commit: Commit = "direct",
    ) -> None:
        self._desktop = desktop
        self._inject = inject
        self._clock = clock
        self._commit = commit
        self.target_name = ""
        self.runaway = False
        self.last_hold = None
        self.run_active = False
        self.counts = Counter()
        # gate
        self._target: KeyTarget = NO_TARGET
        self._target_t = -math.inf
        self._hwnd: int | None = None
        self._warm_until = -math.inf
        self._yield_until = -math.inf
        self._focus_until = -math.inf
        self._blocked_until = -math.inf
        self._foreign_fails = 0
        self._gate_now: float | None = None
        self._gate_hold: Hold | None = None
        # both lanes: consecutive calls that failed or were slow (a fast success resets it)
        self._fails = 0
        # key lane
        self._key_sends: deque[float] = deque()
        # run lane
        self._pin: tuple[int, int] | None = None
        self._again = False
        self._run_total = 0
        self._run_sent = 0
        self._run_sends: deque[float] = deque()
        self._run_chars: deque[float] = deque()
        self._run_begins: deque[float] = deque()
        self._last_end = -math.inf
        #: (time, hwnd, pid) of the last completed text run: what a Send may follow.
        self._last_pin: tuple[float, int, int] | None = None

    def __repr__(self) -> str:
        return f"<KeySink {self._commit} run={self.run_active} runaway={self.runaway}>"

    # ------------------------------------------------------------------------------------------------------- gate

    def start(self, now: float) -> None:
        """Baselines foreign input and the target; opens the warm-up window. Called at arming, not at open.

        The opening button releases of the pointer hand-over must never read as foreign input, hence the baseline here
        and the window in which ``gate`` only re-baselines. A second call (after a ladder fallback) does the same and
        never clears a counter, a breaker or a budget.
        """
        try:
            self._desktop.foreign_input()
        except Exception:  # noqa: BLE001 - a probe that fails is read again by the first gate, which fails closed
            self.counts["foreign_fail"] += 1
        self._refresh_target(now, settle=False)
        self._warm_until = now + SINK_WARMUP_S

    def gate(self, now: float, *, overlay_ok: bool = True) -> Hold | None:
        """Once per frame. The first hold that applies, in the order of SR6, or None."""
        self._poll_foreign(now)
        try:
            modifiers = bool(self._desktop.modifiers_down())
        except Exception:  # noqa: BLE001 - unknown means down
            modifiers = True
            self.counts["modifiers_fail"] += 1
        if now - self._target_t >= TARGET_REFRESH_S:
            self._refresh_target(now, settle=True)
        target = self._target
        hold: Hold | None
        if target.blocked is not None or now < self._blocked_until:
            hold = "blocked"
        elif target.password:
            hold = "password"
        elif target.covered:
            hold = "covered"
        elif not overlay_ok:
            hold = "overlay"
        elif now < self._focus_until:
            hold = "focus"
        elif now < self._yield_until or modifiers:
            hold = "yield"
        else:
            hold = None
        self._gate_now = now
        self._gate_hold = hold
        return hold

    def _poll_foreign(self, now: float) -> None:
        foreign = False
        failed = False
        try:
            foreign = bool(self._desktop.foreign_input())
        except Exception:  # noqa: BLE001 - fail closed: input we cannot see is foreign input
            failed = True
        if failed:
            self.counts["foreign_fail"] += 1
            self._foreign_fails += 1
            self._yield_until = now + YIELD_S
        else:
            self._foreign_fails = 0
            if foreign and now >= self._warm_until:
                self.counts["foreign"] += 1
                self._yield_until = now + YIELD_S
        if self._foreign_fails >= FOREIGN_FAIL_CLOSE:
            raise SinkFailed("The keyboard could not tell whether the real keyboard or mouse was used.")

    def _read_target(self) -> KeyTarget:
        try:
            target = self._desktop.key_target()
        except Exception:  # noqa: BLE001 - a target that cannot be read is no target
            self.counts["target_fail"] += 1
            return NO_TARGET
        return target if isinstance(target, KeyTarget) else NO_TARGET

    def _refresh_target(self, now: float, *, settle: bool) -> None:
        target = self._read_target()
        if settle and self._hwnd is not None and target.hwnd != self._hwnd:
            # A dialog or a toast that took the focus must not receive the key aimed at another window.
            self._focus_until = now + FOCUS_SETTLE_S
        self._hwnd = target.hwnd
        self._target = target
        self._target_t = now
        self.target_name = target.name

    def _gate_answer(self, now: float) -> Hold | None:
        """This frame's gate result. A missing one is a hold: the controller gates every frame, the sink does not
        rely on it (F12)."""
        if self._gate_now != now:
            self.counts["no_gate"] += 1
            return "focus"
        return self._gate_hold

    def _hold(self, hold: Hold) -> Literal["hold"]:
        self.last_hold = hold
        self.counts["hold"] += 1
        return "hold"

    # ----------------------------------------------------------------------------------------------- allow-list

    @staticmethod
    def _stroke_ok(stroke: object, controls: tuple[str, ...]) -> bool:
        """The third allow-list layer: a real ``KeyStroke`` that still passes its own constructor, and a character or
        one of the controls this lane may type. Never raises, never reports what it saw."""
        if type(stroke) is not KeyStroke:
            return False
        try:
            KeyStroke(stroke.kind, stroke.value)  # a stroke forced past __post_init__ fails here
        except Exception:  # noqa: BLE001 - KeyRefused, or whatever a forged stroke does
            return False
        if stroke.kind == "char":
            return stroke.value in ALLOWED_CHARS
        return stroke.value in controls

    def _lane_violation(self) -> Literal["failed"]:
        """A session bug: the wrong lane for the commit mode. The controller closes ``runaway``."""
        self.counts["lane_violation"] += 1
        self.runaway = True
        return "failed"

    # --------------------------------------------------------------------------------------------------- key lane

    def send(self, stroke: KeyStroke, now: float) -> SendResult:
        """Key lane (``commit == "direct"``): one stroke, after every check."""
        if self._commit != "direct":
            return self._lane_violation()
        if not self._stroke_ok(stroke, ("space", "backspace", "enter")):
            self.counts["bad_stroke"] += 1
            return "failed"
        hold = self._gate_answer(now)
        if hold is None:
            try:
                moved = self._desktop.foreground_window() != self._target.hwnd
            except Exception:  # noqa: BLE001 - unknown means moved
                moved = True
            if moved:
                self._focus_until = now + FOCUS_SETTLE_S
                hold = "focus"
        if hold is not None:
            return self._hold(hold)
        sends = self._key_sends
        while sends and now - sends[0] > BACKSTOP_S:
            sends.popleft()
        if len(sends) >= BACKSTOP_N - 1:  # the BACKSTOP_N-th send within the window is the one that is refused
            self.runaway = True
            self.counts["limited"] += 1
            return "limited"
        sends.append(now)
        if self._deliver(stroke, now) == "sent":
            self.counts["sent"] += 1
            return "sent"
        self.counts["failed"] += 1
        return "failed"

    # ------------------------------------------------------------------------------------------------- the run lane

    def begin_run(self, now: float, *, total: int, again: bool = False) -> bool:
        """Run lane (``commit == "review"``). False: refused, ``last_hold`` says why and nothing is pinned."""
        if self._commit != "review":
            self._lane_violation()
            self.last_hold = None
            return False
        self.last_hold = None
        if self._gate_now != now:
            self.counts["begin_no_gate"] += 1
            return self._refuse("focus")
        if self._gate_hold is not None and self._gate_hold != "slow":
            return self._refuse(self._gate_hold)
        if type(total) is not int or not 1 <= total <= COMPOSE_MAX:
            self.counts["bad_total"] += 1
            return self._refuse(None)
        if self.run_active:
            self.counts["run_overlap"] += 1
            return self._refuse(None)
        target = self._read_target()
        if target.hwnd == 0 or target.blocked is not None:
            return self._refuse("blocked")
        if target.password:
            return self._refuse("password")
        if target.covered:
            return self._refuse("covered")
        try:
            modifiers = bool(self._desktop.modifiers_down())
        except Exception:  # noqa: BLE001 - unknown means down
            modifiers = True
        if modifiers:
            return self._refuse("yield")
        if not again and now - self._last_end < RUN_COOLDOWN_S:
            self.counts["run_cooldown"] += 1
            return self._refuse("yield")  # an Insert tapped right after an abort is refused once, not a runaway
        begins = self._run_begins
        while begins and now - begins[0] > BUDGET_WINDOW_S:
            begins.popleft()
        if len(begins) >= RUN_MAX_PER_MIN:
            self.runaway = True
            self.counts["run_budget"] += 1
            return self._refuse("yield")
        if again:
            last = self._last_pin
            if (
                total != 1
                or last is None
                or now - last[0] > SEND_WINDOW_S + AGAIN_GRACE_S
                or (target.hwnd, target.pid) != (last[1], last[2])
            ):
                self.counts["again_refused"] += 1
                return self._refuse("focus")
            self._last_pin = None  # a Send consumes it
        begins.append(now)
        self._pin = (target.hwnd, target.pid)
        self._again = again
        self._run_total = total
        self._run_sent = 0
        self.run_active = True
        return True

    def _refuse(self, hold: Hold | None) -> Literal[False]:
        self.last_hold = hold
        return False

    def send_run(self, stroke: KeyStroke, now: float) -> InsertResult:
        """One character of the active run: a fresh look at the window first, then one atomic stroke."""
        if self._commit != "review":
            return self._lane_violation()
        if not self.run_active or self._run_sent >= self._run_total:
            self.runaway = True
            self.counts["limited"] += 1
            return "limited"
        controls = ("enter",) if self._again else ("space",)
        if not self._stroke_ok(stroke, controls) or (self._again and stroke.kind != "control"):
            self.counts["bad_stroke"] += 1
            return "failed"
        target = self._read_target()  # fresh, not the 100-ms cache: a window can change between two characters
        if (target.hwnd, target.pid) != self._pin:
            self._focus_until = now + FOCUS_SETTLE_S
            return self._hold("focus")
        if target.blocked is not None:
            return self._hold("blocked")
        if target.password:
            return self._hold("password")
        if target.covered:
            return self._hold("covered")
        hold = self._gate_answer(now)
        if hold is not None and hold != "slow":
            return self._hold(hold)
        sends = self._run_sends
        while sends and now - sends[0] > INSERT_BACKSTOP_S:
            sends.popleft()
        if len(sends) >= INSERT_BACKSTOP_N:
            self.runaway = True
            self.counts["limited"] += 1
            return "limited"
        chars = self._run_chars
        while chars and now - chars[0] > BUDGET_WINDOW_S:
            chars.popleft()
        if len(chars) >= RUN_MAX_CHARS_PER_MIN:
            self.runaway = True
            self.counts["run_budget"] += 1
            self.counts["limited"] += 1
            return "limited"
        sends.append(now)
        chars.append(now)
        outcome = self._deliver(stroke, now)
        if outcome == "failed":
            self.counts["failed"] += 1
        else:
            self._run_sent += 1  # a stroke that may have been typed counts as typed: never sent twice
            self.counts["sent" if outcome == "sent" else "maybe"] += 1
        return outcome

    def end_run(self, now: float, completed: bool = False) -> None:
        """Ends the run. ``now`` is the injected-clock value of the controller: it stamps the cooldown and the pin a
        Send may follow (only a completed text run leaves one; a Send consumes it, an aborted run forfeits it)."""
        pin = self._pin
        text_run = not self._again  # a Send never leaves a pin for another Send, whatever the caller says
        self._pin = None
        self._again = False
        self._run_total = 0
        self._run_sent = 0
        self.run_active = False
        self._last_end = now
        self._last_pin = (now, pin[0], pin[1]) if completed and text_run and pin is not None else None

    # --------------------------------------------------------------------------------------------------- delivery

    def _deliver(self, stroke: KeyStroke, now: float) -> Literal["sent", "maybe", "failed"]:
        """One ``send_keys`` call, timed. ``failed``: nothing was delivered; ``maybe``: something may have been.

        Raises ``SinkFailed`` once ``SEND_FAIL_CLOSE`` calls in a row have failed or been slow. The exception is raised
        outside the ``try`` so that it carries no context: what a desktop raised (it may name a character) is dropped.
        """
        started = self._clock()
        outcome: Literal["sent", "maybe", "failed"] = "sent"
        windows_fault = True
        try:
            self._desktop.send_keys([stroke], inject=self._inject)
        except InputBlocked:
            outcome = "failed"
            self._blocked_until = now + BLOCKED_AFTER_REFUSAL_S
        except KeyRefused:
            # Raised before anything is sent, and by our own allow-list: not Windows' fault, so not a failure to count.
            outcome = "failed"
            windows_fault = False
            self.counts["bad_stroke"] += 1
        except OSError:
            outcome = "maybe"  # Windows took part of the batch
        except Exception:  # noqa: BLE001 - raised after the stroke may have been handed over: read as delivered
            outcome = "maybe"
        slow = outcome == "sent" and self._clock() - started > SEND_SLOW_S
        if slow:
            self.counts["slow_send"] += 1
        if outcome == "sent" and not slow:
            self._fails = 0
        elif windows_fault:
            self._fails += 1
        if self._fails >= SEND_FAIL_CLOSE:
            raise SinkFailed("Windows would not take the keys three times in a row.")
        return outcome
