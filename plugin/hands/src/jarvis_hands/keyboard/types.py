"""The names and shapes the air keyboard's modules share: enums, records that cross a module boundary, protocols.

A leaf: standard library and numpy only, so it can be imported alone and everything else may import it. Two kinds of
record are as sensitive as the text they came from and print as a fixed word (``Touch``, ``DecodeRequest``,
``DecodeResult``): where a tap fell reveals the key, so it must never reach a log, an event, a status or a file (SR26).

Every field added by the air tap, the review box and the decoder hook has a default and sits at the end of its record,
so the first version's positional constructor calls keep compiling. DESIGN-KEYBOARD.md 3.1 and 3.16.1.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

import numpy as np

Side = Literal["left", "right"]
Lang = Literal["en", "he"]
#: ``insert`` and ``clear`` exist in the review layout only; ``chip`` is an inert dead cell in step 1 (the decoder
#: hook, 3.16).
KeyKind = Literal[
    "char", "shift", "lang", "private", "home", "backspace", "space", "enter", "close", "insert", "clear", "chip"
]
#: In priority order (2.7 step 3): the first that applies is the hold.
Hold = Literal["blocked", "password", "covered", "overlay", "focus", "yield", "slow"]
Phase = Literal["placing", "warmup", "typing"]
Mode = Literal["live", "practice"]
Commit = Literal["review", "direct"]
CloseReason = Literal[
    "command",
    "close_key",
    "fists",
    "idle",
    "paused",
    "desktop_locked",
    "runaway",
    "no_overlay",
    "camera",
    "disabled",
    "error",
    "input_blocked",
    "air_unreliable",
]
#: The key lane's answer (3.6) and the run lane's: ``maybe`` is a partly taken batch or an unexpected failure.
SendResult = Literal["sent", "hold", "limited", "failed"]
InsertResult = Literal["sent", "maybe", "hold", "limited", "failed"]
PressName = Literal["pinch", "air", "windows"]
#: What the session may tell a press method; "off" is the session's, never the method's.
PressLevel = Literal["ok", "degraded"]
TipState = Literal["open", "closing", "pressed", "latched"]
LitKind = Literal["ghost", "target", "ok", "drop", "armed", "on"]
Dock = Literal["top", "bottom"]
ReviewState = Literal["composing", "inserting", "aborted"]
GuardKind = Literal["insert", "clear", "send", "close"]
InsertAbort = Literal["blocked", "password", "covered", "overlay", "focus", "yield", "stopped", "timeout", "failed"]
RunKind = Literal["text", "enter"]


@dataclass(frozen=True)
class FingerSample:
    #: 0 index, 1 middle, 2 ring, 3 pinky.
    finger: int
    #: (2,) levelled tip, pose space (frame widths).
    aim: np.ndarray
    reach: float
    #: Thumb tip to this tip over the palm size (z scaled by ``z_scale``).
    ratio: float
    curled: bool
    #: Air only: 0.25*PIP + 0.35*DIP + 0.40*TIP along the hand axis, in xy palm lengths (2.12.1); 0.0 = not computed.
    lift: float = 0.0


@dataclass(frozen=True)
class HandSample:
    #: Track id, stable while the hand stays in view.
    hand: int
    #: The newest label; used only by one-hand placement.
    side: Side
    #: Frame time.
    t: float
    #: Frame widths.
    palm: float
    #: (2,) knuckle mean, pose space.
    anchor: np.ndarray
    #: Anchor speed, frame widths per second.
    speed: float
    #: Exactly 4.
    fingers: tuple[FingerSample, ...]
    #: ``HandObservation.score``, the handedness confidence; 1.0 = not computed.
    score: float = 1.0


@dataclass(frozen=True)
class PressEvent:
    #: Frame time of the commit.
    t: float
    #: Frame time of the physical press (pinch onset; air: the left base of the dip).
    onset_t: float
    hand: int
    side: Side
    finger: int
    #: Pose space. Pinch: captured at onset. Air: the aim rule of 2.12.3 (onset or commit, never the peak).
    aim: tuple[float, float]
    #: At commit.
    ratio: float
    #: Pinch: second-best ratio minus best. Air: (depth - runner_up) / theta.
    margin: float
    #: Air: depth of the dip, a fraction of the finger's rest lift (pinch: 0.0).
    depth: float = 0.0
    #: Air: 0.2 .. 1.0 (pinch: 0.0 = not computed); reaches the compose buffer as ``Touch.conf`` (2.12.11).
    conf: float = 0.0


@dataclass(frozen=True)
class FingerView:
    """What the overlay draws per fingertip."""

    hand: int
    side: Side
    finger: int
    #: Pose space; the frozen aim while closing or pressed.
    aim: tuple[float, float]
    state: TipState
    #: 0..1: how far the arming ring is filled (air ``closing``); 0.0 for every other state and for pinch.
    fill: float = 0.0
    #: "" or one of the fixed vocabulary of 2.12.9.
    note: str = ""


@dataclass(frozen=True)
class PressQuality:
    """What the session's degradation ladder reads (2.12.7)."""

    #: Tracked hand frame rate; 0.0 = unknown.
    fps: float
    #: Median depth noise over the fingers that have at least 12 quiet records; None = unknown.
    noise: float | None
    #: Holes in the hand sample stream in the last 5 s; 0 = none or unknown; pinch: 0.
    gaps: int = 0


class PressMethod(Protocol):
    name: PressName
    #: True: may only run when the review commit mode is selected (air).
    requires_review: bool
    rejects: dict[str, int]

    def update(self, hands: Sequence[HandSample]) -> list[PressEvent]: ...

    def reset(self) -> None:
        """Every finger latched, nothing pending."""
        ...

    def set_finger(self, side: Side, finger: int, close: float, open_: float | None = None) -> None:
        """Pinch: thresholds (``open_`` is required: None raises ValueError).

        Air: ``close`` is D_f (the warm-up depth, 0.10..0.80); ``open_`` is ignored and may be omitted.
        """
        ...

    def fingers(self, hands: Sequence[HandSample]) -> list[FingerView]: ...

    def quality(self) -> PressQuality:
        """Pinch: ``PressQuality(0.0, None)``."""
        ...

    def set_level(self, level: PressLevel) -> None:
        """Pinch: a no-op."""
        ...

    def set_calibrating(self, on: bool) -> None:
        """Air: the warm-up threshold rule of S6. Pinch: a no-op."""
        ...


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class Touch:
    """The tap record kept next to each character of the review box (2.12.11, 3.16). As sensitive as the text (SR26)."""

    #: Where the aim fell, horizontal, in key units of the layout in effect, unrounded, unclamped.
    u: float
    #: Vertical, same units.
    v: float
    #: 0 index, 1 middle, 2 ring, 3 pinky; 4 and above are read as a thumb (no step-1 press method reports one).
    finger: int
    side: Side
    #: ``PressEvent.onset_t``: frame time of the physical tap, not of its detection.
    t: float
    #: ``PressEvent.conf`` when above 0, else 1.0 (pinch events have conf 0.0); the decoder reads five fields and
    #: ignores this.
    conf: float = 1.0

    def __repr__(self) -> str:
        return "<Touch>"


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class DecodeRequest:
    #: 1, 2, 3 ... per session.
    seq: int
    #: ``ComposeBuffer.version`` after the edit that triggered the request.
    version: int
    #: The head's span in the box, end exclusive; ``box[end:]`` is the trailing mark and the space.
    start: int
    end: int
    #: Exactly what was tapped (case kept).
    head: str
    #: ``len(touches) == len(head)``; never None.
    touches: tuple[Touch, ...]
    #: Selects the channel numbers.
    press: PressName
    #: A space trigger in mode "auto".
    apply: bool

    def __repr__(self) -> str:
        return f"<DecodeRequest seq={self.seq} n={len(self.touches)}>"


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class DecodeResult:
    #: Copied from the request.
    seq: int
    version: int
    start: int
    end: int
    #: Up to 3 (text, posterior), best first, case restored.
    cands: tuple[tuple[str, float], ...]
    #: The policy's verdict on ``cands[0]`` against the head.
    verdict: Literal["keep", "correct"]
    p_top: float
    ms: float

    def __repr__(self) -> str:
        return f"<DecodeResult seq={self.seq}>"


class Decoder(Protocol):
    """The follow-on word decoder. Asynchronous and outside the session; step 1 passes None everywhere."""

    name: str

    def submit(self, req: DecodeRequest) -> None:
        """Non-blocking, O(1); the newest unstarted request wins."""
        ...

    def poll(self) -> DecodeResult | None:
        """Non-blocking, O(1); each result returned once, in order."""
        ...

    def keep(self, word: str) -> None:
        """The user rejected a correction of ``word``: never alter it again this session."""
        ...

    def close(self) -> None:
        """Idempotent; joins the worker for at most 0.5 s."""
        ...
