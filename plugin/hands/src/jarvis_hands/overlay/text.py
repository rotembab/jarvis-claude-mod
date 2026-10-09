"""Text for the keyboard layer: wrapping, and the left-to-right display order of mixed Hebrew and English.

DESIGN-KEYBOARD.md 3.11. Pure strings: no font, no image, no logging, and nothing here raises for any text, because
the review box's text is as sensitive as what Insert would type (SR9, SR13): a drawing error must never be able to
carry it.

Step-1 alphabet only: Hebrew letters (U+05D0..U+05EA) are strong right-to-left, ASCII letters strong left-to-right and
everything else (space, ``' , . / - ?``, the private-mode bullet) is neutral. Pillow's basic layout paints left to
right, so no shaping library is used: ``bidi_order`` says which logical character goes at each painted position and the
painter draws the characters in that order. Digits are not in the alphabet; when step 2 adds them the rule must be
extended (weak numbers) in the same change.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Literal

Direction = Literal["L", "R"]

_WORD = re.compile(r"[^ ]+")


def _strong(c: str) -> str:
    if "א" <= c <= "ת":
        return "R"
    if c.isascii() and c.isalpha():
        return "L"
    return "N"


def base_direction(text: str) -> Direction:
    """The direction of the first strong character, "L" if none."""
    for c in text:
        kind = _strong(c)
        if kind != "N":
            return kind  # type: ignore[return-value]
    return "L"


# --------------------------------------------------------------------------- wrapping


def wrap_spans(text: str, limit: float, measure: Callable[[str], float] = len) -> list[tuple[int, int]]:
    """Where the lines of ``text`` are: ``(start, end)`` slices of the logical text, greedy at spaces.

    ``measure`` is the ruler (the number of characters by default; the painter passes the font's pixel width, because
    a proportional font fits fewer ``m`` than ``i`` and a line wider than the box would hide characters that Insert
    still types). A line holds as many words as fit; the spaces at a break belong to no line; a word wider than a
    line is cut at the longest prefix that fits (never below one character, so every input terminates). Leading
    spaces stay on the first line while they fit with their word. Trailing spaces stay while they fit, and what does
    not fit is the break, after which the text ends on an empty line: the one the caret is on. An empty text is one
    empty line. The lines are in order, do not overlap, and only spaces lie between two of them.
    """
    n = len(text)

    def fits(a: int, b: int) -> bool:
        return measure(text[a:b]) <= limit

    spans: list[tuple[int, int]] = []
    start = end = 0  # the current line; `end` is where its last word ends (== start while it has none)
    have_word = False
    for m in _WORD.finditer(text):
        ws, we = m.span()
        if have_word:
            if fits(start, we):
                end = we
                continue
            spans.append((start, end))
            start = ws
        elif start < ws and not fits(start, we):
            start = ws  # the indentation and the word do not fit together: the word wins
        while we - start > 1 and not fits(start, we):  # one character is a line whatever its width
            p = start + 1
            while p < we and fits(start, p + 1):
                p += 1
            spans.append((start, p))
            start = p
        end, have_word = we, True
    extra, tail = 0, n - end
    while extra < tail and fits(start, end + extra + 1):
        extra += 1
    spans.append((start, end + extra))
    if extra < tail:
        spans.append((n, n))
    return spans


def wrap_lines(text: str, max_chars: int) -> list[str]:
    """Greedy at spaces in logical order; a word longer than ``max_chars`` is cut. ``[""]`` for an empty text."""
    return [text[a:b] for a, b in wrap_spans(text, max_chars)]


# --------------------------------------------------------------------------- bidi


def bidi_directions(text: str, base: Direction | None = None) -> list[Direction]:
    """The direction each character is painted in: strong ones their own, neutrals by the rule of ``bidi_order``."""
    n = len(text)
    if base not in ("L", "R"):
        base = base_direction(text)
    kinds = [_strong(c) for c in text]
    resolved = list(kinds)
    i = 0
    while i < n:
        if kinds[i] != "N":
            i += 1
            continue
        j = i
        while j < n and kinds[j] == "N":
            j += 1
        left = kinds[i - 1] if i > 0 else base
        right = kinds[j] if j < n else base
        direction = left if left == right else base
        for k in range(i, j):
            resolved[k] = direction
        i = j
    return resolved  # type: ignore[return-value]


def bidi_order(text: str, base: Direction | None = None) -> list[int]:
    """The logical index of the character painted at each position, left to right.

    A run of neutrals between two strong characters of the same direction takes that direction, otherwise the base
    direction (a neutral at either edge has the base direction on that side); the text splits into maximal runs of one
    direction; every right-to-left run is reversed; for a right-to-left base the runs themselves go in reverse order.
    The painter dims the first ``sent`` logical characters of a run through this order.
    """
    if base not in ("L", "R"):
        base = base_direction(text)
    runs: list[tuple[Direction, list[int]]] = []
    for k, direction in enumerate(bidi_directions(text, base)):
        if runs and runs[-1][0] == direction:
            runs[-1][1].append(k)
        else:
            runs.append((direction, [k]))
    if base == "R":
        runs.reverse()
    order: list[int] = []
    for direction, indices in runs:
        order.extend(reversed(indices) if direction == "R" else indices)
    return order


def bidi_display(text: str, base: Direction | None = None) -> str:
    """The string to paint left to right (``bidi_order`` applied to ``text``)."""
    return "".join(text[i] for i in bidi_order(text, base))
