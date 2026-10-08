"""Text for the keyboard layer: wrapping, and the left-to-right display order of mixed Hebrew and English.

DESIGN-KEYBOARD.md 3.11.

STUB (T4). Track T4 replaces this file; the names below are the contract. Step-1 alphabet only: Hebrew letters are
strong right-to-left, ASCII letters strong left-to-right, everything else neutral.
"""

from __future__ import annotations

from typing import Literal

STUB_OWNER = "T4"


def base_direction(text: str) -> Literal["L", "R"]:
    """The direction of the first strong character, "L" if none."""
    raise NotImplementedError("T4: overlay.text")


def wrap_lines(text: str, max_chars: int) -> list[str]:
    """Greedy at spaces in logical order; a word longer than ``max_chars`` is cut."""
    raise NotImplementedError("T4: overlay.text")


def bidi_display(text: str, base: Literal["L", "R"] | None = None) -> str:
    """The string to paint left to right."""
    raise NotImplementedError("T4: overlay.text")
