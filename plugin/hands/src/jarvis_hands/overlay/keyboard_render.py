"""Pure drawing of the keyboard layer: geometry, the baked key sprites and the per-frame composite.

DESIGN-KEYBOARD.md 3.11.

STUB (T4). Track T4 replaces this file; the names below are the contract. The real module is numpy plus Pillow glyph
masks and has no window code, so it is testable without a screen.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np

from ..keyboard.types import Commit, Dock, Lang
from .base import KeyboardView

STUB_OWNER = "T4"


#: The only things a draw error says (DESIGN 3.11 item 6): no font with Latin and Hebrew glyphs, no drawing surface,
#: a size that cannot be drawn, anything else.
DrawErrorCode = Literal["font", "surface", "size", "internal"]
DRAW_ERROR_CODES: tuple[DrawErrorCode, ...] = ("font", "surface", "size", "internal")


class KeyboardDrawError(RuntimeError):
    """The layer could not be drawn. Exception text is data too (F4, S22b): ``code`` is the whole message.

    Anything but one of ``DRAW_ERROR_CODES`` (a formatted exception, a piece of the box) is replaced by ``internal``
    instead of being carried. This class is real in the stub; T4 keeps it as it is.
    """

    code: DrawErrorCode

    def __init__(self, code: DrawErrorCode = "internal") -> None:
        safe: DrawErrorCode = code if type(code) is str and code in DRAW_ERROR_CODES else "internal"
        super().__init__(safe)
        self.code = safe

    def __str__(self) -> str:
        return self.code


@dataclass(frozen=True)
class KeyboardGeometry:
    #: Pixels per key unit.
    pitch: int
    #: Window size.
    width: int
    height: int
    #: Per key index (the review layout's cells too): x, y, w, h in window pixels.
    keys: tuple[tuple[int, int, int, int], ...]
    strip: tuple[int, int, int, int]
    echo: tuple[int, int, int, int]
    #: The box rect in review mode, else None.
    compose: tuple[int, int, int, int] | None


def keyboard_geometry(
    work: tuple[int, int, int, int], size: float, dpi: int, dock: Dock, commit: Commit = "direct"
) -> tuple[KeyboardGeometry, tuple[int, int]]:
    """The geometry and the window's top-left corner."""
    raise NotImplementedError("T4: overlay.keyboard_render")


def bake_base(lang: Lang, shift: bool, geometry: KeyboardGeometry, commit: Commit = "direct") -> np.ndarray:
    """Premultiplied BGRA ``(h, w, 4)``; cached by ``(lang, shift, pitch, commit)``."""
    raise NotImplementedError("T4: overlay.keyboard_render")


def compose(base: np.ndarray, geometry: KeyboardGeometry, view: KeyboardView) -> np.ndarray:
    """A new array each call; the previous array object when the view is equal apart from ``seq``."""
    raise NotImplementedError("T4: overlay.keyboard_render")
