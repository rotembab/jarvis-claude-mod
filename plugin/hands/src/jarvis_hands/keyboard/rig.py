"""Test support shipped in the package like ``synthetic.py``: a session, a sink and a fake desktop wired together.

DESIGN-KEYBOARD.md 5.1.

STUB (T2). Track T2 replaces this file; the names below are the contract. ``ScriptedPress`` is a ``PressMethod`` that
emits the ``PressEvent`` objects a test scripts, so the session can be tested without hands.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence

from ..landmarks import Frame
from .layout import Key
from .tuning import Tuning
from .types import (
    Commit,
    Decoder,
    FingerView,
    HandSample,
    PressEvent,
    PressLevel,
    PressName,
    PressQuality,
    Side,
)

STUB_OWNER = "T2"


class ScriptedPress:
    name: PressName
    requires_review: bool
    rejects: dict[str, int]

    def __init__(self) -> None:
        raise NotImplementedError("T2: keyboard.rig")

    def update(self, hands: Sequence[HandSample]) -> list[PressEvent]:
        raise NotImplementedError("T2: keyboard.rig")

    def reset(self) -> None:
        raise NotImplementedError("T2: keyboard.rig")

    def set_finger(self, side: Side, finger: int, close: float, open_: float | None = None) -> None:
        raise NotImplementedError("T2: keyboard.rig")

    def fingers(self, hands: Sequence[HandSample]) -> list[FingerView]:
        raise NotImplementedError("T2: keyboard.rig")

    def quality(self) -> PressQuality:
        raise NotImplementedError("T2: keyboard.rig")

    def set_level(self, level: PressLevel) -> None:
        raise NotImplementedError("T2: keyboard.rig")

    def set_calibrating(self, on: bool) -> None:
        raise NotImplementedError("T2: keyboard.rig")


class KbRig:
    def __init__(
        self, *, commit: Commit = "direct", tuning: Tuning | None = None, decoder: Decoder | None = None
    ) -> None:
        raise NotImplementedError("T2: keyboard.rig")

    def feed(self, frames: Sequence[Frame]) -> None:
        raise NotImplementedError("T2: keyboard.rig")

    def tap(
        self,
        t: float,
        key_or_char: Key | str,
        du: float = 0.0,
        dv: float = 0.0,
        finger: int | None = None,
        side: Side | None = None,
    ) -> None:
        """Taps the key's centre plus an offset in key units."""
        raise NotImplementedError("T2: keyboard.rig")

    def insert(self, t: float) -> None:
        """Three taps on Insert, 1.0 s apart."""
        raise NotImplementedError("T2: keyboard.rig")

    def send(self, t: float) -> None:
        """Three taps on Send."""
        raise NotImplementedError("T2: keyboard.rig")

    @property
    def typed(self) -> str:
        """``FakeDesktop.typed_text``."""
        raise NotImplementedError("T2: keyboard.rig")

    @property
    def closed(self) -> str | None:
        raise NotImplementedError("T2: keyboard.rig")

    @property
    def counts(self) -> Counter[str]:
        raise NotImplementedError("T2: keyboard.rig")

    @property
    def box(self) -> str:
        """Test-only accessor of ``buffer.text()``."""
        raise NotImplementedError("T2: keyboard.rig")

    @property
    def summary_log(self) -> list[object]:
        raise NotImplementedError("T2: keyboard.rig")

    @property
    def chips(self) -> tuple[str, ...]:
        raise NotImplementedError("T2: keyboard.rig")

    @property
    def chip_active(self) -> int | None:
        raise NotImplementedError("T2: keyboard.rig")
