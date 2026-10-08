"""The last gate before Windows: the key lane and the run lane (DESIGN-KEYBOARD.md 3.6).

STUB (T2). Track T2 replaces this file; the names below are the contract. The real module does not depend on the
session, takes its clock as an argument and keeps no character in a counter, a log line or an exception text.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from typing import Literal

from ..desktop.base import KeyDesktop
from ..desktop.keys import KeyStroke
from .types import Commit, Hold, InsertResult, SendResult

STUB_OWNER = "T2"


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
    #: run_budget.
    counts: Counter[str]

    def __init__(
        self,
        desktop: KeyDesktop,
        *,
        inject: Literal["unicode", "vk"],
        clock: Callable[[], float],
        commit: Commit = "direct",
    ) -> None:
        raise NotImplementedError("T2: keyboard.sink")

    def start(self, now: float) -> None:
        """Baselines foreign input and the target; opens the warm-up window. Called at arming, not at open."""
        raise NotImplementedError("T2: keyboard.sink")

    def gate(self, now: float, *, overlay_ok: bool = True) -> Hold | None:
        """Once per frame."""
        raise NotImplementedError("T2: keyboard.sink")

    def send(self, stroke: KeyStroke, now: float) -> SendResult:
        """Key lane (``commit == "direct"``)."""
        raise NotImplementedError("T2: keyboard.sink")

    def begin_run(self, now: float, *, total: int, again: bool = False) -> bool:
        """Run lane (``commit == "review"``). False: refused, ``last_hold`` says why."""
        raise NotImplementedError("T2: keyboard.sink")

    def send_run(self, stroke: KeyStroke, now: float) -> InsertResult:
        raise NotImplementedError("T2: keyboard.sink")

    def end_run(self, now: float, completed: bool = False) -> None:
        raise NotImplementedError("T2: keyboard.sink")
