"""The keys of the air keyboard: the direct layout, the review layout (chip cells too) and the lookups on them.

Two layouts share one plane (DESIGN-KEYBOARD.md 3.3). ``ROW_TABLE`` is the single source of truth for the direct
layout and ``REVIEW_ROW_TABLE`` is derived from it, so a letter cannot be on two different cells of the two. Nothing
here states how many keys there are: ``Layout.count`` and ``KEY_COUNT`` come from the tables (P11), and only the
layout's own test pins the numbers.

Pure: no I/O, no clock, no state.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

from .types import Commit, KeyKind, Lang

WIDTH_U: float = 11.5
CellKind = Literal[
    "char", "shift", "lang", "private", "home", "backspace", "space", "enter", "close", "insert", "clear", "chip", "gap"
]
#: A cell of a row: (kind, width in key units, English character, Hebrew character).
Cell = tuple[CellKind, float, str, str]


@dataclass(frozen=True)
class Key:
    #: 0 .. count-1, the same in every language; the direct layout's keys keep their indices in both layouts.
    index: int
    kind: KeyKind
    #: The same in both layouts, except Backspace (row 1 in review) and the Enter key (row 4 in review).
    row: int
    #: Left edge in key units.
    col: float
    width: float
    #: Typed in English (kind == "char").
    en: str = ""
    #: Typed in Hebrew.
    he: str = ""

    def __repr__(self) -> str:
        # Which key a tap landed on is as sensitive as the character it types (SR13): a key never prints its fields.
        return "<Key>"


def _row(*cells: tuple[CellKind, float, str, str]) -> tuple[Cell, ...]:
    return cells


def _letters(en: str, he: tuple[str, ...]) -> tuple[Cell, ...]:
    return tuple(("char", 1.0, e, h) for e, h in zip(en, he, strict=True))


#: (kind, width, en, he) per key; Appendix B has the Hebrew column. The Hebrew apostrophe stands on the English w and
#: the Hebrew full stop on the English slash, which is why the Hebrew letters are not a plain reordering of the English.
ROW_TABLE: tuple[tuple[Cell, ...], ...] = (
    (*_letters("qwertyuiop", (",", "'", "ק", "ר", "א", "ט", "ו", "ן", "ם", "פ")), ("backspace", 1.5, "", "")),
    (*_letters("asdfghjkl'", ("ש", "ד", "ג", "כ", "ע", "י", "ח", "ל", "ך", "ף")), ("enter", 1.5, "", "")),
    (("shift", 1.5, "", ""), *_letters("zxcvbnm,./", ("ז", "ס", "ב", "ה", "נ", "מ", "צ", "ת", "ץ", "."))),
    _row(
        ("lang", 1.5, "", ""),
        ("private", 1.0, "", ""),
        ("home", 1.0, "", ""),
        ("space", 4.5, "", ""),
        ("char", 1.0, "-", "-"),
        ("char", 1.0, "?", "?"),
        ("close", 1.5, "", ""),
    ),
)

#: The five rows of the review layout. Row 0 ends in a dead 1.5 cell instead of Backspace, row 1 ends in Backspace
#: instead of Enter, rows 2 and 3 are unchanged, and row 4 is new: Clear, Send (the Enter key), a dead 0.25 cell, three
#: inert chip cells, a dead 0.25 cell and Insert. Every row is 11.5 wide.
REVIEW_ROW_TABLE: tuple[tuple[Cell, ...], ...] = (
    (*ROW_TABLE[0][:10], ("gap", 1.5, "", "")),
    (*ROW_TABLE[1][:10], ("backspace", 1.5, "", "")),
    ROW_TABLE[2],
    ROW_TABLE[3],
    (
        ("clear", 1.5, "", ""),
        ("enter", 1.5, "", ""),
        ("gap", 0.25, "", ""),
        ("chip", 2.0, "", ""),
        ("chip", 2.0, "", ""),
        ("chip", 2.0, "", ""),
        ("gap", 0.25, "", ""),
        ("insert", 2.0, "", ""),
    ),
)


@dataclass(frozen=True)
class Layout:
    #: "direct" | "review".
    name: Commit
    #: 4 | 5.
    rows: int
    #: v of the row the resting fingertips sit on: 1.5 in both layouts.
    home_v: float
    #: In index order; ``count`` is their number, derived from the tables and never a literal (P11).
    keys: tuple[Key, ...]
    #: (row, col_from, col_to): dead cells; empty in the direct layout.
    gaps: tuple[tuple[int, float, float], ...]
    #: Per row, left to right: (left edge, key or None for a dead cell). Built once from the other fields.
    _cells: tuple[tuple[tuple[float, Key | None], ...], ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        rows: list[list[tuple[float, Key | None]]] = [[] for _ in range(self.rows)]
        for key in self.keys:
            rows[key.row].append((key.col, key))
        for row, col_from, _col_to in self.gaps:
            rows[row].append((col_from, None))
        object.__setattr__(self, "_cells", tuple(tuple(sorted(r, key=lambda cell: cell[0])) for r in rows))

    def _key_in_row(self, row: int, u: float) -> Key | None:
        """The cell of ``row`` holding ``u`` (``u`` is already inside the keyboard); None for a dead cell."""
        hit: Key | None = None
        for left, key in self._cells[row]:
            if left > u:
                break
            hit = key
        return hit

    def key_at(self, u: float, v: float, tol: float = 0.35) -> Key | None:
        """The key under plane position ``(u, v)``, or None (the tap is dropped, counter ``off``).

        Outside the keyboard by more than ``tol`` is nobody; inside the band it is clamped to the edge key. Left and
        top edges belong to the cell they open. A dead cell answers None except within ``tol`` of its edge on a typing
        row, and a chip answers its chip except within ``tol`` of the row above: no typing row loses accuracy to the
        cells the review layout adds (DESIGN 2.3, 3.3).
        """
        if not (-tol <= u < WIDTH_U + tol and -tol <= v < self.rows + tol):
            return None  # also every NaN, which fails both comparisons
        u = min(max(u, 0.0), WIDTH_U)
        row = min(max(math.floor(v), 0), self.rows - 1)
        hit = self._key_in_row(row, u)
        if hit is not None and hit.kind != "chip":
            return hit
        # A dead cell or a chip: the part near the typing row beside it belongs to that row.
        if row == 0 and hit is None and (row + 1) - v <= tol:
            return self._key_in_row(row + 1, u)
        if row == self.rows - 1 and row > 0 and v - row <= tol:
            return self._key_in_row(row - 1, u)
        return hit

    def find(self, kind: KeyKind | None = None, char: str | None = None, lang: Lang = "en") -> Key:
        """The key of a kind, or the character key typing ``char`` in ``lang`` (" " is the Space key).

        For tests and the synthetic typist. A key that is not there is a ``KeyError`` with a fixed text: the character
        asked for is never part of it.
        """
        if char == " ":
            kind, char = "space", None
        for key in self.keys:
            if char is not None:
                if key.kind == "char" and (key.he if lang == "he" else key.en) == char:
                    return key
            elif kind is not None and key.kind == kind:
                return key
        raise KeyError("no such key")

    @property
    def count(self) -> int:
        return len(self.keys)


def _build(name: Commit, table: tuple[tuple[Cell, ...], ...], direct_index: dict[tuple[str, str], int]) -> Layout:
    """Cells to keys. A key of the direct layout keeps its index wherever it stands; the new ones are numbered after.

    The index of a review key comes from ``(kind, en)`` in the direct table and never from the flattened order of the
    review table (3.3), so traces, practice targets and the overlay's sprites compare between the layouts. New cells
    are numbered in reading order, chips last.
    """
    #: (row, col, kind, width, en, he) of every key cell, and the dead cells apart.
    cells: list[tuple[int, float, CellKind, float, str, str]] = []
    gaps: list[tuple[int, float, float]] = []
    for row, line in enumerate(table):
        col = 0.0
        for kind, width, en, he in line:
            if kind == "gap":
                gaps.append((row, col, col + width))
            else:
                cells.append((row, col, kind, width, en, he))
            col += width
    new = [c for c in cells if (c[2], c[4]) not in direct_index]
    numbered = [c for c in new if c[2] != "chip"] + [c for c in new if c[2] == "chip"]
    fresh = {c: len(direct_index) + n for n, c in enumerate(numbered)}
    keys = [
        Key(direct_index[(kind, en)] if (kind, en) in direct_index else fresh[c], kind, row, col, width, en, he)  # type: ignore[arg-type]
        for c in cells
        for row, col, kind, width, en, he in (c,)
    ]
    keys.sort(key=lambda key: key.index)
    return Layout(name, len(table), 1.5, tuple(keys), tuple(gaps))


_DIRECT_INDEX: dict[tuple[str, str], int] = {
    (kind, en): index for index, (kind, _w, en, _he) in enumerate(cell for row in ROW_TABLE for cell in row)
}

LAYOUTS: dict[Commit, Layout] = {
    "direct": _build("direct", ROW_TABLE, _DIRECT_INDEX),
    "review": _build("review", REVIEW_ROW_TABLE, _DIRECT_INDEX),
}

# The first version's names stay valid as aliases of the direct layout.
LAYOUT: Layout = LAYOUTS["direct"]
KEY_COUNT: int = sum(len(row) for row in ROW_TABLE)
ROWS: int = len(ROW_TABLE)

_LEGENDS: dict[str, str] = {
    "backspace": "Bksp",
    "enter": "Enter",
    "shift": "Shift",
    "private": "Priv",
    "home": "Home",
    "space": " ",
    "close": "Close",
    "insert": "Insert",
    "clear": "Clear",
}


def layout_for(commit: Commit) -> Layout:
    return LAYOUTS[commit]


def key_at(u: float, v: float, tol: float = 0.35) -> Key | None:
    """The direct layout's lookup, kept under its first-version name."""
    return LAYOUT.key_at(u, v, tol)


def char_for(key: Key, lang: Lang, shift: bool) -> str:
    """The character a character key types; "" for every other key. Shift capitalises English only."""
    if key.kind != "char":
        return ""
    if lang == "he":
        return key.he
    return key.en.upper() if shift else key.en


def legend(key: Key, lang: Lang, shift: bool, commit: Commit = "direct") -> str:
    """What to draw on a key. The Enter key reads ``Send`` in review mode; a chip reads nothing (step 1)."""
    if key.kind == "char":
        return char_for(key, lang, shift)
    if key.kind == "lang":
        return lang.upper()
    if key.kind == "enter" and commit == "review":
        return "Send"
    return _LEGENDS.get(key.kind, "")
