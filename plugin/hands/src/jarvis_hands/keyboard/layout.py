"""The keys of the air keyboard: the direct layout, the review layout (chip cells too) and the lookups on them.

STUB (T0). Track T1 replaces this file; the names, fields and signatures below are the contract of
DESIGN-KEYBOARD.md 3.3 and stay as they are. The data tables are filled in by T1.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .types import Commit, KeyKind, Lang

STUB_OWNER = "T1"
_STUB_DATA = ("ROW_TABLE", "REVIEW_ROW_TABLE", "LAYOUTS", "LAYOUT", "KEY_COUNT", "ROWS")

WIDTH_U: float = 11.5
CellKind = Literal[
    "char", "shift", "lang", "private", "home", "backspace", "space", "enter", "close", "insert", "clear", "chip", "gap"
]


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

    def key_at(self, u: float, v: float, tol: float = 0.35) -> Key | None:
        raise NotImplementedError("T1: keyboard.layout")

    def find(self, kind: KeyKind | None = None, char: str | None = None, lang: Lang = "en") -> Key:
        raise NotImplementedError("T1: keyboard.layout")

    @property
    def count(self) -> int:
        raise NotImplementedError("T1: keyboard.layout")


def layout_for(commit: Commit) -> Layout:
    raise NotImplementedError("T1: keyboard.layout")


def key_at(u: float, v: float, tol: float = 0.35) -> Key | None:
    """The direct layout's lookup, kept under its first-version name."""
    raise NotImplementedError("T1: keyboard.layout")


def char_for(key: Key, lang: Lang, shift: bool) -> str:
    raise NotImplementedError("T1: keyboard.layout")


def legend(key: Key, lang: Lang, shift: bool, commit: Commit = "direct") -> str:
    raise NotImplementedError("T1: keyboard.layout")


def __getattr__(name: str) -> object:
    if name in _STUB_DATA:
        raise NotImplementedError("T1: keyboard.layout")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
