"""The review box: a 200-character buffer edited at its end only (DESIGN-KEYBOARD.md 2.13.1).

Pure: no I/O, no clock (times are arguments). Taps fill the box and nothing reaches another window until three taps on
Insert; the text is therefore as private as anything the keyboard holds. It is kept as a list of single characters,
``repr`` shows a length, and there is no ``__str__``, ``__iter__``, ``__getitem__``, ``__contains__`` or ``__eq__``: the
only ways to read the text are ``text()`` (the review machine's plan and view, and tests) and ``touches()``.

There is no caret, no selection and no newline. The mutators are ``append``, ``backspace``, ``clear``, ``consume`` (the
review machine, at the end of a run) and ``replace_span`` (the decoder hook); each bumps ``version`` and sets
``last_edit_t``, which is what clears a pending guard, so no change of the box can slip past a guard (R19, SR42).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Literal

from ..desktop.keys import COMPOSE_CHARS
from .limits import COMPOSE_MAX
from .types import Touch

InsertRefusal = Literal["empty", "too_long", "bad_char", "bang_first"]

#: The basic plane only: a code point above it would need a surrogate pair, which the input path does not type.
_LAST_BMP = 0xFFFF


def _admits(ch: object, alphabet: frozenset[str]) -> bool:
    """One printable code point of the basic plane that is in the alphabet. A newline is never printable."""
    return type(ch) is str and len(ch) == 1 and ord(ch) <= _LAST_BMP and ch.isprintable() and ch in alphabet


class ComposeBuffer:
    #: +1 on every change; a refused or empty operation changes nothing and leaves it alone.
    version: int
    last_edit_t: float

    __slots__ = ("_alphabet", "_cap", "_chars", "_touches", "last_edit_t", "version")

    def __init__(self, alphabet: frozenset[str] = COMPOSE_CHARS, cap: int = COMPOSE_MAX) -> None:
        self._alphabet = alphabet
        self._cap = cap
        self._chars: list[str] = []
        self._touches: list[Touch | None] = []
        self.version = 0
        self.last_edit_t = -math.inf

    def __len__(self) -> int:
        return len(self._chars)

    def __repr__(self) -> str:
        return f"<ComposeBuffer len={len(self._chars)}>"

    @property
    def alphabet(self) -> frozenset[str]:
        """What the box admits; the review machine asks ``insert_check`` against the same set."""
        return self._alphabet

    # There is deliberately no ``__str__`` (``str(buffer)`` falls back to the length-only repr), ``__iter__``,
    # ``__getitem__``, ``__contains__`` or ``__eq__``: reading the text goes through ``text()`` only, so a stray
    # ``list(buffer)`` or ``buffer[0]`` cannot put it in a log line or an assertion message by accident.

    def text(self) -> str:
        """The logical text. Called only by review.py (plan, view) and by tests."""
        return "".join(self._chars)

    def touches(self) -> tuple[Touch | None, ...]:
        """One entry per character, aligned with ``text()``: the decoder hook (3.16)."""
        return tuple(self._touches)

    def append(self, ch: str, t: float, touch: Touch | None = None) -> Literal["ok", "full", "refused"]:
        if not _admits(ch, self._alphabet):
            return "refused"
        if len(self._chars) >= self._cap:
            return "full"
        self._chars.append(ch)
        self._touches.append(touch)
        self._changed(t)
        return "ok"

    def backspace(self, t: float) -> bool:
        if not self._chars:
            return False
        self._chars.pop()
        self._touches.pop()
        self._changed(t)
        return True

    def clear(self, t: float) -> int:
        """How many characters were dropped."""
        n = len(self._chars)
        if n:
            self._chars.clear()
            self._touches.clear()
            self._changed(t)
        return n

    def consume(self, n: int, t: float) -> None:
        """Drops the first ``n`` characters (they were typed into a window)."""
        n = min(n, len(self._chars))
        if n <= 0:
            return
        del self._chars[:n]
        del self._touches[:n]
        self._changed(t)

    def replace_span(
        self, start: int, end: int, text: str, t: float, touches: Sequence[Touch | None] | None = None
    ) -> bool:
        """The decoder hook: replace ``[start, end)``. Refuses, and changes nothing (``version`` included), on a bad
        span, a result over the cap, a character outside the alphabet or a newline, or touches of the wrong length."""
        if not (isinstance(start, int) and isinstance(end, int)) or not 0 <= start < end <= len(self._chars):
            return False
        if type(text) is not str or len(self._chars) - (end - start) + len(text) > self._cap:
            return False
        if not all(_admits(c, self._alphabet) for c in text):
            return False
        if touches is not None and len(touches) != len(text):
            return False
        self._chars[start:end] = list(text)
        self._touches[start:end] = list(touches) if touches is not None else [None] * len(text)
        self._changed(t)
        return True

    def _changed(self, t: float) -> None:
        self.version += 1
        self.last_edit_t = t


def insert_check(text: str, *, alphabet: frozenset[str] = COMPOSE_CHARS) -> InsertRefusal | None:
    """The first reason Insert must not start a run, or None.

    In step 1 only ``empty`` can occur in practice (the box admits nothing else); the other three are defensive and are
    tested with a widened alphabet. A first non-space ``/`` is allowed: typing ``/clear`` does not run it, and Send
    refuses it instead (2.13.6).
    """
    if not text.strip(" "):
        return "empty"
    if len(text) > COMPOSE_MAX:
        return "too_long"
    if not all(_admits(c, alphabet) for c in text):
        return "bad_char"
    if text.lstrip(" ")[0] == "!":
        return "bang_first"
    return None
