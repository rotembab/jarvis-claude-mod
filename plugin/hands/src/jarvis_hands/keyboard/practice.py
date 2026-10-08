"""Practice: the phrases, the drill, the scoring and the markers that gate a first live session.

DESIGN-KEYBOARD.md 5.7 and 2.12.6.

STUB (T2). Track T2 replaces this file; the names below are the contract. ``PracticeResult`` is a plain record and is
real here so that the session, the controller and the replay tool can build and read it before T2 lands.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .ladder import Level
from .types import PressName

STUB_OWNER = "T2"


@dataclass(frozen=True)
class PracticeResult:
    #: Six phrases finished or timed out, both rests done.
    completed: bool
    presses: int
    correct: int
    hit_rate: float
    #: Seconds of REST with a hand in view.
    rest_s: float
    phantoms: int
    phantoms_per_min: float
    #: Median frame rate over the session.
    fps: float
    #: "left.ring" -> (presses, correct); no key identity.
    per_finger: dict[str, tuple[int, int]]
    # Air only (2.12.6): reported, never gated unless the marker rules say so. Everything below defaults to 0.
    talk_s: float = 0.0
    talk_phantoms: int = 0
    drill_prompts: int = 0
    drill_hits: int = 0
    #: Index and middle fingers only.
    drill_prompts_im: int = 0
    drill_hits_im: int = 0
    drill_prompts_reach: int = 0
    drill_hits_reach: int = 0
    #: Standard deviation of ``aim - target centre`` in key units, kept for the decoder track.
    aim_sd_u: float = 0.0
    aim_sd_v: float = 0.0
    #: Median depth noise.
    noise: float = 0.0
    #: The ladder level at the end.
    level: Level = "ok"


def load_marker(data_dir: Path, press: PressName) -> dict[str, Any] | None:
    """The practice marker of ``press``: present, parsable, for that method, not dated in the future, in range."""
    raise NotImplementedError("T2: keyboard.practice")
