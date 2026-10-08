"""The review machine: taps edit the box, three taps insert it, three taps send Enter (DESIGN-KEYBOARD.md 2.13).

STUB (T2). Track T2 replaces this file; the names below are the contract, and so are the guard tables, which the design
pins in 2.13.4. The real module imports ``types``, ``limits``, ``compose``, ``desktop.keys`` and ``overlay.base`` (for
the ``ComposeView`` it builds), reaches a decoder only through the ``Decoder`` protocol, and never logs the box, a plan
or a step.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Literal

from ..desktop.keys import KeyStroke
from ..overlay.base import ComposeView
from .compose import ComposeBuffer
from .limits import GUARD_MAX_S, INSERT_TAPS, SEND_TAPS
from .types import Decoder, Hold, InsertAbort, InsertResult, KeyKind, ReviewState, Touch

STUB_OWNER = "T2"
_STUB_DATA = ("REVIEW_TEXT",)

#: Which key starts which guard, and how many taps each needs. INSERT_TAPS and SEND_TAPS are floors of 3 (limits.py).
GUARD_OF_KEY = {"insert": "insert", "clear": "clear", "enter": "send", "close": "close"}
GUARD_TAPS = {"insert": INSERT_TAPS, "clear": 2, "send": SEND_TAPS, "close": 2}
GUARD_WINDOW = {"insert": GUARD_MAX_S, "clear": GUARD_MAX_S, "send": GUARD_MAX_S, "close": GUARD_MAX_S}


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


class ReviewMachine:
    state: ReviewState
    buffer: ComposeBuffer
    #: Reasons and outcomes by name; never text.
    counts: Counter[str]

    def __init__(self, *, enter: Literal["twice", "off"], decoder: Decoder | None = None) -> None:
        raise NotImplementedError("T2: keyboard.review")

    @property
    def running(self) -> bool:
        raise NotImplementedError("T2: keyboard.review")

    def tap(self, kind: KeyKind, ch: str, t: float, *, touch: Touch | None = None) -> InsertStep | None:
        raise NotImplementedError("T2: keyboard.review")

    def tick(self, t: float, hold: Hold | None) -> InsertStep | None:
        raise NotImplementedError("T2: keyboard.review")

    def note_step(self, step: InsertStep, result: InsertResult, t: float, hold: Hold | None) -> None:
        raise NotImplementedError("T2: keyboard.review")

    def take_summary(self) -> InsertSummary | None:
        raise NotImplementedError("T2: keyboard.review")

    def disarm(self) -> None:
        raise NotImplementedError("T2: keyboard.review")

    def discard(self) -> int:
        """Drops the box, the run, the guard and the last insert; returns how many characters were dropped."""
        raise NotImplementedError("T2: keyboard.review")

    def view(self, t: float, *, private: bool) -> ComposeView:
        raise NotImplementedError("T2: keyboard.review")

    # The four seams of the decoder hook (3.16.2, H5): private, no-ops in step 1, filled in by the follow-on track.
    def _after_edit(self, kind: KeyKind, ch: str, t: float) -> None:
        raise NotImplementedError("T2: keyboard.review")

    def _poll_decoder(self, t: float, hold: Hold | None) -> None:
        raise NotImplementedError("T2: keyboard.review")

    def _chip_tap(self, index: int, t: float) -> bool:
        raise NotImplementedError("T2: keyboard.review")

    def _undo_correction(self, t: float) -> bool:
        raise NotImplementedError("T2: keyboard.review")


def __getattr__(name: str) -> object:
    if name in _STUB_DATA:
        raise NotImplementedError("T2: keyboard.review")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
