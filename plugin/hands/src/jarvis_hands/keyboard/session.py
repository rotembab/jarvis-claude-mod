"""One keyboard session, frame by frame: placing, warm-up, typing, holds and closing (DESIGN-KEYBOARD.md 2.7, 3.7).

STUB (T2). Track T2 replaces this file; the names below are the contract. The real module is pure: no clock (it uses
``frame.t``), no I/O, no thread. A decoder, when one exists, is an object it is handed and merely polls (3.16).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from ..desktop.keys import KeyStroke
from ..landmarks import Frame
from ..overlay.base import KeyboardView
from .practice import PracticeResult
from .review import InsertStep, InsertSummary
from .tuning import Tuning
from .types import (
    CloseReason,
    Commit,
    Decoder,
    Hold,
    InsertResult,
    Lang,
    Mode,
    PressMethod,
    PressName,
    ReviewState,
    SendResult,
)

STUB_OWNER = "T2"


@dataclass(frozen=True, kw_only=True)
class SessionOutput:
    #: 0 or 1 per frame; direct mode only; always empty in review mode, in practice mode and before arming.
    strokes: tuple[KeyStroke, ...]
    #: ``work=(0, 0, 0, 0)``, ``size=1.0``, ``dock="top"``, ``seq=0``, ``exclude_capture=False``: the controller fills
    #: them.
    view: KeyboardView
    closed: CloseReason | None
    #: 0 or 1 per frame; review mode only; always empty in direct mode.
    steps: tuple[InsertStep, ...] = ()


class KeyboardSession:
    closed: CloseReason | None
    armed: bool
    private: bool
    #: Keys, drops by reason, rejects by name; no characters.
    counts: Counter[str]
    #: Characters thrown away by close (0 until closed or outside review mode).
    discarded: int

    def __init__(
        self,
        *,
        press: PressMethod,
        tuning: Tuning,
        idle_s: int,
        enter: Literal["twice", "off"],
        lang: Lang,
        mode: Mode,
        aspect: float,
        start_t: float,
        reach: float = 1.0,
        phrases: Sequence[str] | None = None,
        commit: Commit = "direct",
        fallback: Callable[[], PressMethod] | None = None,
        decoder: Decoder | None = None,
    ) -> None:
        raise NotImplementedError("T2: keyboard.session")

    def update(self, frame: Frame, hold: Hold | None, target_name: str) -> SessionOutput:
        raise NotImplementedError("T2: keyboard.session")

    def note_result(self, stroke: KeyStroke, result: SendResult, t: float) -> None:
        """Direct mode: a refused stroke flashes red."""
        raise NotImplementedError("T2: keyboard.session")

    def note_step(self, step: InsertStep, result: InsertResult, t: float, hold: Hold | None) -> None:
        """Review mode (2.13.3)."""
        raise NotImplementedError("T2: keyboard.session")

    def take_summary(self) -> InsertSummary | None:
        """A run ended in this frame, or None."""
        raise NotImplementedError("T2: keyboard.session")

    def recenter(self) -> None:
        raise NotImplementedError("T2: keyboard.session")

    def set_private(self, on: bool) -> None:
        raise NotImplementedError("T2: keyboard.session")

    def close(self, reason: CloseReason) -> None:
        raise NotImplementedError("T2: keyboard.session")

    @property
    def press_name(self) -> PressName:
        """The ACTIVE press method; "pinch" after a ladder fallback."""
        raise NotImplementedError("T2: keyboard.session")

    @property
    def review_state(self) -> ReviewState | None:
        """None in direct and practice mode."""
        raise NotImplementedError("T2: keyboard.session")

    @property
    def compose_len(self) -> int:
        """Characters in the box; 0 outside review mode."""
        raise NotImplementedError("T2: keyboard.session")

    def practice_result(self) -> PracticeResult | None:
        raise NotImplementedError("T2: keyboard.session")
