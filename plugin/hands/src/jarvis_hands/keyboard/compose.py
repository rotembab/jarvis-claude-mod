"""The review box: a 200-character buffer edited at its end only (DESIGN-KEYBOARD.md 2.13.1).

STUB (T2). Track T2 replaces this file; the names below are the contract. The real module is pure (no I/O, no clock),
imports only ``types``, ``limits`` and ``desktop.keys``, and never prints, logs or reprs the text it holds.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from ..desktop.keys import COMPOSE_CHARS
from .limits import COMPOSE_MAX
from .types import Touch

STUB_OWNER = "T2"

InsertRefusal = Literal["empty", "too_long", "bad_char", "bang_first"]


class ComposeBuffer:
    #: +1 on every change.
    version: int
    last_edit_t: float

    def __init__(self, alphabet: frozenset[str] = COMPOSE_CHARS, cap: int = COMPOSE_MAX) -> None:
        raise NotImplementedError("T2: keyboard.compose")

    def __len__(self) -> int:
        raise NotImplementedError("T2: keyboard.compose")

    def __repr__(self) -> str:
        """``<ComposeBuffer len=N>``."""
        raise NotImplementedError("T2: keyboard.compose")

    def text(self) -> str:
        """The logical text. Called only by review.py (plan, view) and by tests."""
        raise NotImplementedError("T2: keyboard.compose")

    def touches(self) -> tuple[Touch | None, ...]:
        raise NotImplementedError("T2: keyboard.compose")

    def append(self, ch: str, t: float, touch: Touch | None = None) -> Literal["ok", "full", "refused"]:
        raise NotImplementedError("T2: keyboard.compose")

    def backspace(self, t: float) -> bool:
        raise NotImplementedError("T2: keyboard.compose")

    def clear(self, t: float) -> int:
        """How many characters were dropped."""
        raise NotImplementedError("T2: keyboard.compose")

    def consume(self, n: int, t: float) -> None:
        """Drops the first ``n`` characters (they were typed into a window)."""
        raise NotImplementedError("T2: keyboard.compose")

    def replace_span(
        self, start: int, end: int, text: str, t: float, touches: Sequence[Touch | None] | None = None
    ) -> bool:
        raise NotImplementedError("T2: keyboard.compose")


def insert_check(text: str, *, alphabet: frozenset[str] = COMPOSE_CHARS) -> InsertRefusal | None:
    raise NotImplementedError("T2: keyboard.compose")
