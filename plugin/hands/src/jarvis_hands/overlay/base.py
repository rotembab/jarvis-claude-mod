"""The on-screen reticle: where the hand points, and what it is doing.

The overlay is drawn in click-through, always-on-top windows that never take
focus, so it never gets in the way of what it points at. The runtime calls
``show`` once per tracked frame from its own thread; a backend keeps only the
newest state and draws it on its own thread.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

from ..geometry import Point
from ..keyboard.types import Commit, Dock, GuardKind, Hold, Lang, LitKind, Mode, Phase, ReviewState, Side, TipState

#: What the reticle shows:
#: ``hidden``: nothing (no hand, or overlay off).
#: ``idle``: a faint ring where a visible hand would point, not engaged.
#: ``engaging``: an open palm being held; ``progress`` fills the ring.
#: ``point``: engaged and pointing; ``pinch`` closes the inner arc.
#: ``press``, ``drag``, ``scroll``, ``grab``, ``resize``: the active interaction.
#: ``calibrate``: a target at ``cursor`` to point at; ``progress`` fills while held.
OverlayMode = Literal["hidden", "idle", "engaging", "point", "press", "drag", "scroll", "grab", "resize", "calibrate"]


class OverlayError(RuntimeError):
    """The overlay could not be created."""


@dataclass(frozen=True)
class TipView:
    #: Key units; may lie outside the keyboard.
    u: float
    v: float
    side: Side
    finger: int
    state: TipState
    #: Warm-up: this finger's tap (or pinch) is recorded.
    done: bool = True
    #: Warm-up and drill: the finger the strip names now (its ring pulses); False otherwise.
    named: bool = False
    #: 0..1: the arming ring of ``closing`` (air); 0.0 otherwise.
    fill: float = 0.0
    #: The fixed vocabulary of DESIGN-KEYBOARD.md 2.12.9; "" otherwise.
    note: str = ""


@dataclass(frozen=True)
class ComposeView:
    """The review box; ``KeyboardView.compose`` is None in direct mode."""

    #: Logical order; "" when empty; one bullet per character (spaces too) when private. Typed text: out of every repr.
    text: str = field(repr=False)
    #: The real length, also when masked.
    length: int
    state: ReviewState
    #: Characters of the current run already typed (0 outside a run).
    sent: int
    guard: GuardKind | None
    #: Taps counted so far (0 when no guard).
    guard_taps: int
    #: Taps needed.
    guard_need: int
    #: 0..1: share of the guard window left (the ring).
    guard_left: float
    #: The Send key is available now.
    can_send: bool
    #: ``length == COMPOSE_MAX``.
    full: bool
    #: Decoder hook: drawn when non-empty; step 1 always (). Candidate words are as sensitive as the box: out of every
    #: repr (SR44).
    chips: tuple[str, ...] = field(default=(), repr=False)
    chip_active: int | None = None


@dataclass(frozen=True)
class KeyboardView:
    """Everything the keyboard layer draws, in one value. All of it is hashable and cheap to compare."""

    #: Liveness counter; ignored by drawing.
    seq: int
    mode: Mode
    phase: Phase
    lang: Lang
    shift: bool
    #: No echo or box text, no per-key highlights, a whole-keyboard pulse instead.
    private: bool
    #: Private mode: a key was just accepted.
    pulse: bool
    hold: Hold | None
    #: Direct mode: the first Enter press is waiting.
    armed_enter: bool
    drift: bool
    tips: tuple[TipView, ...]
    #: Placement home rings, key units.
    homes: tuple[tuple[float, float, Side], ...]
    #: (key index, kind); empty of letters when private. Key identity: out of every repr (SR13).
    lit: tuple[tuple[int, LitKind], ...] = field(repr=False)
    #: Status, hold or prompt text; never typed text.
    strip: str
    #: Direct mode: the last <= 24 typed characters or the practice buffer; "" when private, and always "" in review
    #: mode.
    echo: str = field(repr=False)
    #: Practice phrase, drill prompt or warm-up instruction.
    prompt: str
    #: 0..1: both-fists bar, warm-up or rest progress.
    progress: float
    #: x, y, w, h of the display's work area, physical px (filled by the controller).
    work: tuple[int, int, int, int] = (0, 0, 0, 0)
    #: ``keyboardSize`` (controller).
    size: float = 1.0
    dock: Dock = "top"
    #: Private: ask Windows to keep the window out of captures (controller).
    exclude_capture: bool = False
    #: One fixed line above the status text in the strip row, "" = none (the air ladder, 2.12.7).
    banner: str = ""
    banner_level: Literal["", "info", "warn"] = ""
    #: The layout to draw (the controller fills it).
    commit: Commit = "direct"
    #: None in direct mode.
    compose: ComposeView | None = None


@dataclass(frozen=True)
class OverlayState:
    mode: OverlayMode = "hidden"
    #: Desktop pixels (physical, virtual-screen space) of the main reticle or calibration target.
    cursor: Point | None = None
    #: The second hand's point while resizing with two hands.
    helper: Point | None = None
    #: 0 open .. 1 closed (``point`` mode).
    pinch: float = 0.0
    #: 0 .. 1 (``engaging`` and ``calibrate`` modes).
    progress: float = 0.0
    #: The air keyboard; with it set the reticle is not drawn (``mode`` stays ``hidden``).
    keyboard: KeyboardView | None = None


@dataclass(frozen=True)
class OverlayHealth:
    #: The UI thread runs and its windows exist.
    alive: bool
    #: Consecutive failed draws (0 after a success).
    failures: int
    #: Seconds since the last successful draw; None = never.
    ok_age_s: float | None
    #: The keyboard layer exists and a font with Latin and Hebrew glyphs loaded.
    keyboard_ok: bool
    #: Median of the last 100 successful draws in milliseconds; None until 100.
    draw_ms: float | None = None


class Overlay(Protocol):
    def start(self) -> None:
        """Creates the windows. Raises OverlayError."""
        ...

    def show(self, state: OverlayState) -> None:
        """Thread-safe and cheap; the newest state wins."""
        ...

    def close(self) -> None:
        """Destroys the windows. Idempotent; never raises."""
        ...


class NullOverlay:
    """No reticle (overlay off, or no backend for this platform)."""

    def start(self) -> None:
        pass

    def show(self, state: OverlayState) -> None:
        pass

    def close(self) -> None:
        pass


def overlay_health(overlay: object) -> OverlayHealth | None:
    """What an overlay says about itself, or None when it says nothing or cannot be asked.

    ``health`` is not part of the ``Overlay`` protocol (the recording overlay of the tests and every other double keep
    working), so it is looked up by name. None is read by the keyboard controller as "not healthy": a probe that fails
    must not look like a healthy overlay, and must not escape as an exception into the frame loop.
    """
    try:
        probe = getattr(overlay, "health", None)
        health = probe() if callable(probe) else None
    except Exception:  # noqa: BLE001 - an unreadable probe is unknown health
        return None
    return health if isinstance(health, OverlayHealth) else None
