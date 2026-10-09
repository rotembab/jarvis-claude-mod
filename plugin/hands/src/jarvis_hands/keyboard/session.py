"""One keyboard session, frame by frame: placing, warm-up, typing, holds and closing (DESIGN-KEYBOARD.md 2.7, 3.7).

Pure: no clock (it uses ``frame.t``), no I/O, no thread. It never sends anything itself: a tap becomes a ``KeyStroke``
(direct mode) or an ``InsertStep`` (review mode) that the controller passes to the sink, which is the independent last
gate (3.6). A decoder, when one exists, is an object the session is handed and merely polls (3.16).

Beyond the pinned API the controller and the tests need a few read-only things, all counts, enums or fixed strings and
never a typed character: ``phase``, ``plane``, ``home_f``, ``warmup_prompt``, ``lang``, ``shift``, ``level``,
``rejects``, ``take_tap_log`` (practice), ``trace_kind`` and ``trace_target`` (the trace writer), ``practice_done``, and
``hands`` (the newest tracker samples, for the rig that aims synthetic taps at where the fingers really rest).
"""

from __future__ import annotations

import math
import statistics
from collections import Counter, deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final, Literal

import numpy as np

from ..desktop.keys import KeyStroke
from ..landmarks import Frame
from ..overlay.base import ComposeView, KeyboardView, TipView
from .hands import HandTracker
from .ladder import AirLadder
from .layout import Key, Layout, char_for, layout_for
from .limits import (
    AIR_FLASH_S,
    ARM_TIMEOUT_S,
    ECHO_CHARS,
    ENTER_CONFIRM_S,
    FIST_EXIT_S,
    GAP_RESET_S,
    IDLE_WARN_S,
    MIN_GAP_S,
    NO_KEY_CLOSE_S,
    PLACE_TIMEOUT_S,
    QUEUE_AGE_S,
    QUEUE_MAX,
    REVIEW_IDLE_S,
    SHIFT_S,
    SLOW_ENTER_S,
    SLOW_EXIT_S,
    STORM_N,
    STORM_S,
)
from .plane import Plane, place_plane
from .practice import FINGER_NAMES, PRACTICE_TEXT, PracticeResult, PracticeScript
from .review import ABORT_WHY, NOTE_SHOW_S, REVIEW_TEXT, UNKNOWN_TARGET, InsertStep, InsertSummary, ReviewMachine
from .tuning import Tuning
from .types import (
    CloseReason,
    Commit,
    Decoder,
    FingerView,
    HandSample,
    Hold,
    InsertResult,
    Lang,
    LitKind,
    Mode,
    Phase,
    PressEvent,
    PressMethod,
    PressName,
    ReviewState,
    SendResult,
    Side,
    Touch,
)
from .warmup import Warmup

#: The strings of Appendix F.1 that the session owns, and the few the design leaves to it (the paused line, the direct
#: typing strip, the drift line). ``{n}``, ``{N}`` and ``{s}`` are numbers, ``{side}``, ``{finger}`` and ``{Side}`` come
#: from the closed vocabularies of the warm-up, ``{target}`` is a base name or "the active window". A change is a
#: contract change.
SESSION_TEXT: Final = MappingProxyType(
    {
        "place": "Hold your hands over the keys",
        "pinch": "Pinch each finger to your thumb once: {n}/{N}",
        "tap": "Tap: {side} {finger}  {n}/{N}",
        "good": "Good  {n}/{N}",
        "restart": "Only tap the finger the strip names. Starting again.",
        "stuck_finger": "{Side} {finger}: tap a bit firmer with your fingers raised",
        "stuck_all": (
            "Taps not showing up? Raise your fingers a little, or choose pinch: /jarvis hands keyboard press pinch"
        ),
        "posture": "Raise your fingers a little, curved, as over a real keyboard",
        "speed": "Hold your hands steadier to type",
        "coherence": "Keep the other fingers still while one taps",
        "weak": "{Finger}: tap a bit firmer",
        "banner_fps": "Air tap is less sure: camera at {fps} fps. Tap a little firmer.",
        "banner_noise": "Air tap is less sure: hand tracking is shaky. Tap a little firmer.",
        "banner_gaps": "Air tap is less sure: the camera is dropping frames. Tap a little firmer.",
        "off_fps": "Air tap off: camera at {fps} fps, using pinch",
        "off_noise": "Air tap off: hand tracking too shaky, using pinch",
        "paused": "Paused: {why}",
        "typing": "-> {target}   {lang}",
        "enter_again": "Enter again to send",
        "drifted": "Hands drifted: press Home",
    }
)
#: ``{why}`` of the paused line: the sink's holds come from ``ABORT_WHY``; ``slow`` is the session's own.
HOLD_WHY: Final = MappingProxyType(
    {
        "blocked": ABORT_WHY["blocked"],
        "password": ABORT_WHY["password"],
        "covered": ABORT_WHY["covered"],
        "overlay": ABORT_WHY["overlay"],
        "focus": ABORT_WHY["focus"],
        "yield": ABORT_WHY["yield"],
        "slow": "the camera is too slow",
    }
)
#: The note vocabulary that becomes a strip hint, in priority order.
HINT_NOTES: Final = ("posture", "speed", "coherence")
#: The review-layout kinds the session itself handles in every mode.
_SESSION_KINDS: Final = frozenset({"shift", "lang", "private", "home"})
#: Keys that do nothing in practice: there is no box, no Enter and no chip to act on.
_PRACTICE_INERT: Final = frozenset({"insert", "clear", "chip"})

#: Display and bookkeeping numbers the design leaves to the session.
_EPS: Final = 1e-9
_SLOW_WINDOW_S: Final = 1.0
_STILL_WINDOW_S: Final = 0.5
#: After the first hand qualifies, the other visible hand has this long to qualify too (one plane for both).
_PLACE_GRACE_S: Final = 1.0
_DRIFT_ENTER: Final = 0.6
_DRIFT_EXIT: Final = 0.4
_DRIFT_S: Final = 2.0
_DROP_FLASH_S: Final = 0.25
_TARGET_FILL: Final = 0.6
_RESTART_SHOW_S: Final = 2.0
_STUCK_FINGER_S: Final = 15.0
_STUCK_ALL_S: Final = 25.0
_STUCK_SHOW_S: Final = 6.0
_HINT_AFTER_S: Final = 1.5
_HINT_GAP_S: Final = 5.0
_HINT_SHOW_S: Final = 4.0
_WEAK_SHOW_S: Final = 4.0
#: The practice figures are medians over the most recent frames.
_PRACTICE_SAMPLES: Final = 4000
#: A fire record that no event claimed within this long is written as it is.
_FIRE_KEEP_S: Final = 2.0

_LIT_RANK: Final = {"ghost": 0, "on": 1, "armed": 2, "target": 3, "ok": 4, "drop": 5}
_GUARD_KEY: Final = {"insert": "insert", "clear": "clear", "send": "enter", "close": "close"}


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


@dataclass(slots=True, repr=False)
class _Tap:
    """A resolved press waiting for its turn. Where it fell is as private as the character (SR13): no repr."""

    ev: PressEvent
    key: Key
    u: float
    v: float
    touch: Touch | None

    def __repr__(self) -> str:
        return "<Tap>"


class KeyboardSession:
    closed: CloseReason | None
    armed: bool
    private: bool
    #: Keys, drops by reason, the review machine's counters and the press method's rejects by name; no characters.
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
        if press.requires_review and commit != "review":
            # SR29: a tap in the air with nothing between it and the window. Fixed text, no content.
            raise ValueError("The air-tap method only works with the review box (commit: review).")
        self._press = press
        self._tuning = tuning
        self._idle_s = idle_s
        self._enter = enter
        self._mode: Mode = mode
        self._aspect = aspect
        self._start_t = start_t
        self._reach = reach
        self._phrases = phrases
        self._commit: Commit = commit
        self._fallback = fallback
        self._layout: Layout = layout_for(commit)
        self._by_kind: dict[str, int] = {}
        for key in self._layout.keys:
            self._by_kind.setdefault(key.kind, key.index)
        self._chips = [key.index for key in self._layout.keys if key.kind == "chip"]
        self._tracker = HandTracker(tuning)
        self._ladder = AirLadder()
        self._machine = ReviewMachine(enter=enter, decoder=decoder) if commit == "review" and mode == "live" else None
        if mode == "practice":
            # A phrase with a character the layout lacks is the caller's mistake: found now, not after the warm-up.
            PracticeScript(press=press.name, lang=lang, layout=self._layout, sides=("left", "right"), phrases=phrases)
        self._script: PracticeScript | None = None
        self._warmup: Warmup | None = None
        self.lang: Lang = lang
        self.closed = None
        self.armed = False
        self.private = False
        self.counts = Counter()
        #: The press methods' rejects by name, accumulated over every method the session has used.
        self.rejects: Counter[str] = Counter()
        self.discarded = 0
        self._phase: Phase = "placing"
        self._plane: Plane | None = None
        self._home: dict[Side, tuple[float, float]] = {}
        self._home_f: dict[tuple[Side, int], tuple[float, float]] = {}
        self._ever_placed = False
        # the clock of the frame loop
        self._last_t: float | None = None
        self._dts: deque[tuple[float, float]] = deque()
        self._slow = False
        self._hold: Hold | None = None
        self._hands: list[HandSample] = []
        self._target = ""
        # placing and drift
        self._speeds: dict[int, deque[tuple[float, float]]] = {}
        self._still: dict[int, bool] = {}
        self._still_since: dict[int, float] = {}
        self._window: deque[tuple[float, list[HandSample]]] = deque()
        self._grace_since: float | None = None
        self.drift = False
        self._drift_since: float | None = None
        # timers
        self._arm_by = start_t + ARM_TIMEOUT_S
        self._last_hand_t = start_t
        self._last_key_t = start_t
        self._fist_since: float | None = None
        # taps
        self._queue: deque[_Tap] = deque()
        self._last_emit = -math.inf
        self._stamps: deque[float] = deque()
        self._shift_until = -math.inf
        self._armed_enter_until = -math.inf
        self._echo = ""
        self._stroke_key: int | None = None
        self._flash: dict[int, tuple[Literal["ok", "drop"], float]] = {}
        self._pulse_until = -math.inf
        # the ladder
        self._strict = False
        self._banner: tuple[str, str] = ("", "")
        self._banner_key: tuple[str, str] | None = None
        self._pending_fallback = False
        self._cut = False
        # warm-up display
        self._restart_until = -math.inf
        self._named: tuple[Side, int] | None = None
        self._named_since = 0.0
        self._accept_t = 0.0
        self._stuck: dict[str, float] = {}
        self._stuck_done: set[str] = set()
        self._stuck_who: tuple[Side, int] = ("right", 0)
        # hints
        self._note_since: dict[str, float] = {}
        self._hint: tuple[str, float] | None = None
        self._hint_t = -math.inf
        self._weak_done = False
        self._weak: tuple[str, float] | None = None
        self._practice_note_until = -math.inf
        # accounting
        self._mseen: Counter[str] = Counter()
        self._rseen: dict[str, int] = {}
        # practice figures and the tap log
        self._frame_dts: deque[float] = deque(maxlen=_PRACTICE_SAMPLES)
        self._noises: deque[float] = deque(maxlen=_PRACTICE_SAMPLES)
        self._log: list[dict[str, Any]] = []
        self._fires: dict[tuple[float, str, int], dict[str, Any]] = {}

    def __repr__(self) -> str:
        return f"<KeyboardSession {self._mode} {self._phase} closed={self.closed}>"

    # ---------------------------------------------------------------------------------------------- what to read

    @property
    def phase(self) -> Phase:
        return self._phase

    @property
    def plane(self) -> Plane | None:
        return self._plane

    @property
    def hands(self) -> list[HandSample]:
        """The tracker's samples of the newest frame (positions and ratios; a copy of the list)."""
        return list(self._hands)

    @property
    def home_f(self) -> dict[tuple[Side, int], tuple[float, float]]:
        """Each finger's own resting (u, v) from the latest placement; {} before one."""
        return dict(self._home_f)

    @property
    def warmup_prompt(self) -> tuple[Side, int] | None:
        """The finger the air warm-up names now, or None."""
        return self._warmup.prompt if self._warmup is not None and self._phase == "warmup" else None

    @property
    def shift(self) -> bool:
        return self._last_t is not None and self._last_t < self._shift_until

    @property
    def level(self) -> Literal["ok", "degraded", "off"]:
        """The air ladder's level; stays "off" after the switch to pinch."""
        return self._ladder.level

    @property
    def press_name(self) -> PressName:
        """The ACTIVE press method; "pinch" after a ladder fallback."""
        return self._press.name

    @property
    def review_state(self) -> ReviewState | None:
        """None in direct and practice mode."""
        return self._machine.state if self._machine is not None else None

    @property
    def compose_len(self) -> int:
        """Characters in the box; 0 outside review mode."""
        return len(self._machine.buffer) if self._machine is not None else 0

    @property
    def practice_done(self) -> bool:
        """The practice script ended, or the air tap was found unusable and the drill was cut short."""
        return self._mode == "practice" and (self._cut or (self._script is not None and self._script.done))

    @property
    def trace_kind(self) -> str:
        """The segment kind of the landmark trace for this frame (5.7)."""
        if self._phase == "placing":
            return "place"
        if self._phase == "warmup":
            return "warm"
        return self._script.trace_kind if self._script is not None else "type"

    @property
    def trace_target(self) -> int:
        """The prompted key index, or -1."""
        key = self._script.target_key if self._script is not None else None
        return -1 if key is None else key

    def practice_result(self) -> PracticeResult | None:
        if self._mode != "practice" or self._script is None:
            return None
        dts = [d for d in self._frame_dts if d > 0]
        fps = 1.0 / statistics.median(dts) if dts else 0.0
        noise = statistics.median(self._noises) if self._noises else 0.0
        level = "off" if self._cut else self._ladder.level
        return self._script.result(fps=fps, noise=noise, level=level, completed=False if self._cut else None)

    def take_tap_log(self) -> list[dict[str, Any]]:
        """The tap-log records since the last call (practice only; [] otherwise), oldest first."""
        self._drain_trace(self._last_t if self._last_t is not None else 0.0)
        out, self._log = self._log, []
        return out

    def take_summary(self) -> InsertSummary | None:
        """A run ended in this frame, or None."""
        return self._machine.take_summary() if self._machine is not None else None

    # --------------------------------------------------------------------------------------------- the controller

    def note_result(self, stroke: KeyStroke, result: SendResult, t: float) -> None:
        """Direct mode: a refused stroke flashes red; a sent one reaches the echo."""
        if result == "sent":
            if not self.private:
                self._echo_stroke(stroke)
            return
        self.counts[f"send_{result}"] += 1
        if self._stroke_key is not None:
            self._flash[self._stroke_key] = ("drop", t + _DROP_FLASH_S)

    def note_step(self, step: InsertStep, result: InsertResult, t: float, hold: Hold | None) -> None:
        """Review mode (2.13.3)."""
        if self._machine is not None:
            self._machine.note_step(step, result, t, hold)

    def recenter(self) -> None:
        """Home: back to placing with the warm-up result and the box kept (2.4). Refused while a run is in flight."""
        if self.closed is not None:
            return
        if self._machine is not None and self._machine.running:
            self.counts["busy"] += 1
            return
        self._phase = "placing"
        self._clear_queue("not_armed", self._last_t)
        if self._machine is not None:
            self._machine.disarm()
        self.drift = False
        self._drift_since = None
        self._still.clear()
        self._still_since.clear()
        self._window.clear()
        self._grace_since = None
        self._armed_enter_until = -math.inf
        if not self.armed:
            self._warmup = None  # warming up again from the new placement

    def set_private(self, on: bool) -> None:
        if self.closed is not None:
            return
        if on and not self.private:
            self._echo = ""  # what was typed before stays out of the echo for good
        self.private = on
        if self._machine is not None and not self._machine.running:
            self._machine.disarm()

    def close(self, reason: CloseReason) -> None:
        """Idempotent: the first reason stays. The box and any run are dropped; ``discarded`` counts the box."""
        if self.closed is not None:
            return
        self.closed = reason
        self._clear_queue("stale", self._last_t)
        self._echo = ""
        self._armed_enter_until = -math.inf
        if self._machine is not None:
            self.discarded = self._machine.discard()
            self._fold_machine()
        self.armed = False

    # ---------------------------------------------------------------------------------------------- the frame

    def update(self, frame: Frame, hold: Hold | None, target_name: str) -> SessionOutput:
        t = frame.t
        if self.closed is not None:
            return self._output(t, [], [])
        self._target = target_name
        gap = self._last_t is not None and t - self._last_t > GAP_RESET_S + _EPS
        if self._last_t is not None:
            dt = t - self._last_t
            self._dts.append((t, dt))
            if self._mode == "practice":
                self._frame_dts.append(dt)
        self._last_t = t
        if gap:
            self._press.reset()
            self._fist_since = None
            self._drift_since = None
        hands = self._tracker.update(frame)
        self._hands = hands
        self._update_slow(t)
        effective: Hold | None = hold if hold is not None else ("slow" if self._slow else None)
        self._hold_edge(effective, t)
        self._hold = effective
        machine = self._machine
        tick_step = machine.tick(t, effective) if machine is not None else None

        self._track_still(hands, t)
        reason = self._timers(hands, t)
        if reason is not None:
            self.close(reason)
            return self._output(t, [], [])

        if self._press.name == "air" and self._phase in ("warmup", "typing") and hold is None:
            self._ladder_step(t, hands)
        if self._pending_fallback and (machine is None or not (machine.running or machine.has_summary)):
            self._fallback_now(t)
            if self.closed is not None:
                return self._output(t, [], [])

        rejects_before = sum(v for k, v in self._press.rejects.items() if k != "veto")
        events = self._press.update(hands)
        self._fold_rejects()
        self._drain_trace(t)

        if self._phase == "placing":
            self._place(hands, events, t)
        elif self._phase == "warmup":
            self._warm(hands, events, t, effective, rejects_before)
        else:
            self._type(hands, events, t, effective)

        strokes: list[KeyStroke] = []
        steps: list[InsertStep] = []
        if tick_step is not None:
            steps.append(tick_step)
        if self._phase == "typing" and self.closed is None:
            self._emit(t, strokes, steps)
        if machine is not None:
            self._fold_machine()
        return self._output(t, strokes, steps)

    def _output(self, t: float, strokes: list[KeyStroke], steps: list[InsertStep]) -> SessionOutput:
        view = self._view(t)
        if self.closed is not None:
            return SessionOutput(strokes=(), view=view, closed=self.closed, steps=())
        return SessionOutput(strokes=tuple(strokes), view=view, closed=None, steps=tuple(steps[:1]))

    # ------------------------------------------------------------------------------------------ holds and timers

    def _update_slow(self, t: float) -> None:
        dts = self._dts
        while dts and t - dts[0][0] > _SLOW_WINDOW_S + _EPS:
            dts.popleft()
        if len(dts) < 2:
            return
        median = statistics.median(d for _, d in dts)
        if not self._slow and median > SLOW_ENTER_S + _EPS:
            self._slow = True
        elif self._slow and median < SLOW_EXIT_S - _EPS:
            self._slow = False

    def _hold_edge(self, now: Hold | None, t: float) -> None:
        before = self._hold
        if now is not None and before is None:
            # A hold starts: nothing already resolved may type later, and nothing typed before it stays on the echo.
            self._clear_queue("held", t)
            self._echo = ""
            self._armed_enter_until = -math.inf
        elif now is None and before is not None:
            self._press.reset()  # never auto-resume: a finger must reopen first (2.7)

    def _timers(self, hands: list[HandSample], t: float) -> CloseReason | None:
        machine = self._machine
        if hands or (machine is not None and machine.running):
            self._last_hand_t = t  # a run does not use the hands: its time is not idle time
        # both-fists exit (2.7 step 10), every phase
        fists = len(hands) == 2 and all(sum(f.curled for f in h.fingers) >= 3 for h in hands)
        if fists:
            if self._fist_since is None:
                self._fist_since = t
            if t - self._fist_since >= FIST_EXIT_S - _EPS:
                return "fists"
        else:
            self._fist_since = None
        if self._phase == "placing" and not self._ever_placed and t - self._start_t >= PLACE_TIMEOUT_S - _EPS:
            return "idle"
        if not self.armed and t >= self._arm_by - _EPS:
            return "idle"
        if not hands and t - self._last_hand_t >= self._idle_limit() - _EPS:
            return "idle"
        if self.armed and t - self._last_key_t >= NO_KEY_CLOSE_S - _EPS:
            return "idle"
        return None

    def _idle_limit(self) -> float:
        machine = self._machine
        if machine is not None and len(machine.buffer) > 0:
            return float(max(self._idle_s, REVIEW_IDLE_S))
        return float(self._idle_s)

    # ----------------------------------------------------------------------------------------------- the ladder

    def _ladder_step(self, t: float, hands: list[HandSample]) -> None:
        quality = self._press.quality()
        if self._mode == "practice" and quality.noise is not None:
            self._noises.append(quality.noise)
        level = self._ladder.update(t, quality, bool(hands))
        if self._ladder.strict != self._strict:
            self._strict = self._ladder.strict
            self._press.set_level("degraded" if self._strict else "ok")
        key = (level, self._ladder.reason)
        if key != self._banner_key:
            self._banner_key = key
            self._banner = self._banner_text(level, self._ladder.reason, quality.fps)
        if level == "off" and not self._cut:
            self._pending_fallback = True

    def _banner_text(self, level: str, reason: str, fps: float) -> tuple[str, str]:
        n = round(fps)
        if level == "degraded":
            key = {"fps": "banner_fps", "both": "banner_fps", "noise": "banner_noise", "gaps": "banner_gaps"}[reason]
            return SESSION_TEXT[key].format(fps=n), "warn"
        if level == "off" and self._fallback is not None:
            key = "off_noise" if reason == "noise" else "off_fps"
            return SESSION_TEXT[key].format(fps=n), "warn"
        return "", ""

    def _fallback_now(self, t: float) -> None:
        """The fallback switch (2.12.7), only ever between runs."""
        self._pending_fallback = False
        if self._fallback is None:
            if self._mode == "practice":
                self._cut = True  # the drill ends: the result says level "off"
            else:
                self.close("air_unreliable")
            return
        self._press = self._fallback()
        self._rseen = {}
        self._strict = False
        self._warmup = Warmup(self._tuning, "pinch", home_f=self._home_f)
        self._phase = "warmup"
        self.armed = False
        self._clear_queue("not_armed", t)
        self._press.reset()
        if self._machine is not None:
            self._machine.disarm()
        self._arm_by = t + ARM_TIMEOUT_S
        self._armed_enter_until = -math.inf
        self._begin_warmup_display(t)

    # ------------------------------------------------------------------------------------------------- placing

    def _track_still(self, hands: list[HandSample], t: float) -> None:
        """Which hands are still (2.4): the mean anchor speed over 0.5 s below the limit, three fingers not curled."""
        live = {h.hand for h in hands}
        for hand_id in [h for h in self._speeds if h not in live]:
            del self._speeds[hand_id]
            self._still.pop(hand_id, None)
            self._still_since.pop(hand_id, None)
        for h in hands:
            speeds = self._speeds.setdefault(h.hand, deque())
            speeds.append((t, h.speed))
            while speeds and t - speeds[0][0] > _STILL_WINDOW_S + _EPS:
                speeds.popleft()
            mean = math.fsum(s for _, s in speeds) / len(speeds)
            still = mean < self._tuning.still_speed and sum(not f.curled for f in h.fingers) >= 3
            self._still[h.hand] = still
            if still:
                self._still_since.setdefault(h.hand, t)
            else:
                self._still_since.pop(h.hand, None)

    def _place(self, hands: list[HandSample], events: list[PressEvent], t: float) -> None:
        for ev in events:
            self._drop("not_armed", ev, t)
        self._window.append((t, hands))
        while self._window and t - self._window[0][0] > 2.0 * self._tuning.still_s:
            self._window.popleft()
        ready = [h for h in hands if t - self._still_since.get(h.hand, t + 1.0) >= self._tuning.still_s - _EPS]
        if not ready:
            self._grace_since = None
            return
        if self._grace_since is None:
            self._grace_since = t
        if len(ready) < len(hands) and t - self._grace_since < _PLACE_GRACE_S - _EPS:
            return  # the other hand is nearly still: one plane for both is worth a moment
        ids = {h.hand for h in ready}
        frames = [
            [h for h in samples if h.hand in ids]
            for then, samples in self._window
            if t - then <= self._tuning.still_s + _EPS
        ]
        placement = place_plane(frames, layout=self._layout, tuning=self._tuning, reach=self._reach)
        self._plane, self._home, self._home_f = placement.plane, placement.home, placement.home_f
        self._tracker.learn_levels()
        self._ever_placed = True
        self._window.clear()
        self._grace_since = None
        self.drift = False
        self._drift_since = None
        if self.armed:
            self._phase = "typing"
            self._press.reset()
            self._last_key_t = t
            return
        self._phase = "warmup"
        method = "air" if self._press.name == "air" else "pinch"
        self._warmup = Warmup(self._tuning, method, home_f=self._home_f)
        if method == "air":
            self._press.set_calibrating(True)
        self._begin_warmup_display(t)

    def _begin_warmup_display(self, t: float) -> None:
        self._named = None
        self._named_since = t
        self._accept_t = t
        self._restart_until = -math.inf

    # ------------------------------------------------------------------------------------------------- warm-up

    def _warm(
        self, hands: list[HandSample], events: list[PressEvent], t: float, hold: Hold | None, rejects_before: int
    ) -> None:
        warmup = self._warmup
        assert warmup is not None
        if self._press.name == "air":
            if hold is None:
                outcomes = warmup.update(hands, events, t=t, plane=self._plane, rejects=rejects_before)
                for ev, outcome in zip(events, outcomes, strict=True):
                    self.counts["warmup_tap"] += 1
                    self.counts[f"warmup_{outcome}"] += 1
                    self._log_event(ev, "warmup_tap")
                    if outcome == "accepted":
                        self._accept_t = t
                    elif outcome == "restart":
                        self._restart_until = t + _RESTART_SHOW_S
                        self._stuck_done.discard("finger")
            else:
                warmup.update(hands, (), t=t, plane=self._plane, rejects=rejects_before)
                for ev in events:
                    self._drop("held", ev, t)
        else:
            warmup.update(hands)
            for ev in events:
                self._drop("not_armed", ev, t)
        prompt = warmup.prompt
        if prompt != self._named:
            self._named = prompt
            self._named_since = t
        if warmup.complete:
            self._arm(t)

    def _arm(self, t: float) -> None:
        warmup = self._warmup
        assert warmup is not None
        if self._press.name == "air":
            self._press.set_calibrating(False)
        for (side, finger), (close, open_) in warmup.thresholds().items():
            self._press.set_finger(side, finger, close, open_)
        self._press.reset()
        self.armed = True
        self._phase = "typing"
        self._last_key_t = t
        self._clear_queue("not_armed", t)
        if self._machine is not None:
            self._machine.disarm()
        if self._mode == "practice" and self._script is None:
            sides = tuple(s for s in ("left", "right") if any(key[0] == s for key in self._home_f))
            self._script = PracticeScript(
                press=self._press.name,
                lang=self.lang,
                layout=self._layout,
                sides=sides,
                phrases=self._phrases,
                seed=int(self._start_t * 1000) & 0xFFFFFFFF,
            )

    # -------------------------------------------------------------------------------------------------- typing

    def _type(self, hands: list[HandSample], events: list[PressEvent], t: float, hold: Hold | None) -> None:
        if self._script is not None:
            self._script.tick(t, bool(hands))
        self._drift_step(hands, t)
        plane = self._plane
        assert plane is not None
        for ev in events:
            if hold is not None:
                self._drop("held", ev, t, self._key_of(plane, ev)[0])
                continue
            key, u, v = self._key_of(plane, ev)
            if self._script is not None:
                self._script.event(ev.t, ev.side, ev.finger, u, v, key.index if key is not None else None)
            if key is None:
                self._drop("off", ev, t, u=u, v=v)
                continue
            if len(self._queue) >= QUEUE_MAX:
                self._drop("queue", ev, t, key, u, v)
                continue
            touch = None
            if self._machine is not None and key.kind in ("char", "space"):
                touch = Touch(u, v, ev.finger, ev.side, ev.onset_t, ev.conf if ev.conf > 0 else 1.0)
            self._queue.append(_Tap(ev, key, u, v, touch))

    def _key_of(self, plane: Plane, ev: PressEvent) -> tuple[Key | None, float, float]:
        u, v = plane.units(ev.aim)
        return self._layout.key_at(u, v, tol=self._tuning.edge_tolerance), u, v

    def _emit(self, t: float, strokes: list[KeyStroke], steps: list[InsertStep]) -> None:
        queue = self._queue
        while queue and t - queue[0].ev.t > QUEUE_AGE_S + _EPS:
            tap = queue.popleft()
            self._drop("stale", tap.ev, t, tap.key, tap.u, tap.v)
        machine = self._machine
        if not queue or t - self._last_emit < MIN_GAP_S - _EPS or (machine is not None and machine.has_summary):
            return
        tap = queue.popleft()
        self._last_emit = t
        self._last_key_t = t
        key = tap.key
        self._stroke_key = key.index
        if self._mode == "practice":
            self._deliver_practice(tap, t)
        elif machine is not None:
            step = self._deliver_review(tap, t)
            if step is not None:
                steps.append(step)
        else:
            stroke = self._deliver_direct(tap, t)
            if stroke is not None:
                strokes.append(stroke)
        if self.private:
            self._pulse_until = t + AIR_FLASH_S

    # ------------------------------------------------------------------------------------------ key semantics

    def _shift_on(self, t: float) -> bool:
        return t < self._shift_until

    def _session_key(self, kind: str, t: float) -> None:
        """Shift, Lang, Priv and Home: the session's in every mode (2.7 step 9)."""
        if kind == "shift":
            if self.lang != "en":
                self.counts["shift_inert"] += 1
            else:
                self._shift_until = -math.inf if self._shift_on(t) else t + SHIFT_S
        elif kind == "lang":
            self.lang = "he" if self.lang == "en" else "en"
            self._shift_until = -math.inf
        elif kind == "private":
            self.set_private(not self.private)
        elif kind == "home":
            self.recenter()

    def _ok(self, key: Key, t: float) -> None:
        self._flash[key.index] = ("ok", t + AIR_FLASH_S)

    def _deliver_direct(self, tap: _Tap, t: float) -> KeyStroke | None:
        stamps = self._stamps
        while stamps and t - stamps[0] > STORM_S:
            stamps.popleft()
        if len(stamps) + 1 >= STORM_N:
            self.close("runaway")  # nothing resumes: a new session warms up again (2.7 step 8)
            return None
        stamps.append(t)
        self.counts["keys"] += 1
        key = tap.key
        kind = key.kind
        self._ok(key, t)
        if kind != "enter":
            self._armed_enter_until = -math.inf  # the second Enter must follow the first at once
        if kind == "char":
            ch = char_for(key, self.lang, self._shift_on(t))
            self._shift_until = -math.inf
            return KeyStroke("char", ch)
        if kind in ("space", "backspace"):
            return KeyStroke("control", kind)
        if kind == "enter":
            return self._enter_key(t)
        if kind == "close":
            self.close("close_key")
            return None
        if kind in _SESSION_KINDS:
            self._session_key(kind, t)
        return None

    def _enter_key(self, t: float) -> KeyStroke | None:
        if self._enter != "twice":
            self.counts["enter_off"] += 1
            return None
        if t < self._armed_enter_until:
            self._armed_enter_until = -math.inf
            return KeyStroke("control", "enter")
        self._armed_enter_until = t + ENTER_CONFIRM_S
        return None

    def _deliver_review(self, tap: _Tap, t: float) -> InsertStep | None:
        machine = self._machine
        assert machine is not None
        self.counts["keys"] += 1
        key = tap.key
        kind = key.kind
        if kind in _SESSION_KINDS and not (machine.running and kind != "private"):
            self._session_key(kind, t)
            if not machine.running:
                machine.disarm()
            self._ok(key, t)
            return None
        if kind == "char":
            ch = char_for(key, self.lang, self._shift_on(t))
        elif kind == "space":
            ch = " "
        elif kind == "chip":
            ch = str(self._chips.index(key.index))
        else:
            ch = ""
        step = machine.tap(kind, ch, t, touch=tap.touch)  # type: ignore[arg-type]
        if kind == "char":
            self._shift_until = -math.inf
        if machine.flash is not None:
            self._flash[key.index] = (machine.flash, t + (AIR_FLASH_S if machine.flash == "ok" else _DROP_FLASH_S))
        if machine.take_storm():
            self._press.reset()
            self._clear_queue("held", t)
        if machine.take_close():
            self.close("close_key")
        return step

    def _deliver_practice(self, tap: _Tap, t: float) -> None:
        script = self._script
        assert script is not None
        self.counts["keys"] += 1
        key = tap.key
        kind = key.kind
        if kind != "enter":
            self._armed_enter_until = -math.inf
        inert = kind in _PRACTICE_INERT or (kind == "enter" and self._commit == "review")
        self._log_event(tap.ev, "practice_review_key" if inert else "key", tap.u, tap.v, key.index)
        script.press(t, tap.ev.side, tap.ev.finger, key.index)
        self._ok(key, t)
        if inert:
            self.counts["practice_review_key"] += 1
            self._practice_note_until = t + NOTE_SHOW_S
        elif kind == "close":
            self.close("close_key")
        elif kind == "enter":
            self._enter_key(t)  # arms and disarms like the real thing; nothing is sent in a practice
        elif kind in _SESSION_KINDS:
            self._session_key(kind, t)

    def _echo_stroke(self, stroke: KeyStroke) -> None:
        if stroke.kind == "char":
            self._echo = (self._echo + stroke.value)[-ECHO_CHARS:]
        elif stroke.value == "space":
            self._echo = (self._echo + " ")[-ECHO_CHARS:]
        elif stroke.value == "backspace":
            self._echo = self._echo[:-1]
        elif stroke.value == "enter":
            self._echo = ""

    # ----------------------------------------------------------------------------------------------- drift

    def _drift_step(self, hands: list[HandSample], t: float) -> None:
        still = [h for h in hands if self._still.get(h.hand)]
        if not still or not self._home:
            self._drift_since = None
            return
        ordered = sorted(hands, key=lambda h: float(h.anchor[0]))
        distances = []
        for h in still:
            side = self._home_side(h, ordered)
            if side is None:
                continue
            aims = np.mean([f.aim for f in h.fingers], axis=0)
            assert self._plane is not None
            u, v = self._plane.units(aims)
            hu, hv = self._home[side]
            distances.append(math.hypot(u - hu, v - hv))
        if not distances:
            self._drift_since = None
            return
        worst = max(distances)
        if worst > _DRIFT_ENTER:
            if self._drift_since is None:
                self._drift_since = t
            if t - self._drift_since >= _DRIFT_S - _EPS:
                self.drift = True
        else:
            self._drift_since = None
            if worst < _DRIFT_EXIT:
                self.drift = False

    def _home_side(self, hand: HandSample, ordered: list[HandSample]) -> Side | None:
        """Which resting position a hand is compared to: by order in the picture with two hands, by label with one."""
        sides = [s for s in ("left", "right") if s in self._home]
        if len(sides) == 1:
            return sides[0]
        if len(ordered) == 2:
            return "left" if hand.hand == ordered[0].hand else "right"
        return hand.side if hand.side in self._home else None

    # ------------------------------------------------------------------------------------ queue and accounting

    def _clear_queue(self, outcome: str, t: float | None) -> None:
        while self._queue:
            tap = self._queue.popleft()
            self.counts[outcome] += 1
            self._log_event(tap.ev, outcome, tap.u, tap.v)

    def _drop(
        self,
        reason: str,
        ev: PressEvent,
        t: float,
        key: Key | None = None,
        u: float | None = None,
        v: float | None = None,
    ) -> None:
        self.counts[reason] += 1
        if key is not None and reason in ("queue", "stale", "held"):
            self._flash[key.index] = ("drop", t + _DROP_FLASH_S)
        self._log_event(ev, reason, u, v)

    def _fold_machine(self) -> None:
        machine = self._machine
        assert machine is not None
        for name, value in machine.counts.items():
            delta = value - self._mseen[name]
            if delta:
                self.counts[name] += delta
                self._mseen[name] = value

    def _fold_rejects(self) -> None:
        for name, value in self._press.rejects.items():
            delta = value - self._rseen.get(name, 0)
            if delta > 0:
                self.counts[name] += delta
                self.rejects[name] += delta
            self._rseen[name] = value

    # ------------------------------------------------------------------------------------------------ tap log

    def _drain_trace(self, t: float) -> None:
        if self._mode != "practice":
            return
        take = getattr(self._press, "take_trace", None)
        if take is None:
            return
        for record in take():
            if record.get("k") == "fire":
                self._fires[(record["t"], record["side"], record["finger"])] = record
            else:
                self._log.append(record)
        for key in [k for k, r in self._fires.items() if t - k[0] > _FIRE_KEEP_S]:
            self._log.append(self._fires.pop(key))

    def _log_event(
        self, ev: PressEvent, outcome: str, u: float | None = None, v: float | None = None, key: int | None = None
    ) -> None:
        """Finish the tap-log record of ``ev`` with the session's verdict (2.12.10). Practice only; numbers and fixed
        words, never a character."""
        if self._mode != "practice":
            return
        record = self._fires.pop((ev.t, ev.side, ev.finger), None)
        if record is None:
            record = self._event_record(ev)
        record["outcome"] = outcome
        if u is not None and v is not None:
            record["u"] = float(u)
            record["v"] = float(v)
        target = self._script.target_key if self._script is not None else None
        if target is not None:
            record["target"] = target
            if key is not None:
                record["hit"] = key
        self._log.append(record)

    def _event_record(self, ev: PressEvent) -> dict[str, Any]:
        if self._press.name == "pinch":
            return {
                "t": ev.t,
                "side": ev.side,
                "finger": FINGER_NAMES[ev.finger],
                "ratio": ev.ratio,
                "margin": ev.margin,
                "closingMs": round((ev.t - ev.onset_t) * 1000.0),
            }
        return {
            "k": "fire",
            "t": ev.t,
            "hand": ev.hand,
            "side": ev.side,
            "finger": ev.finger,
            "onsetT": ev.onset_t,
            "depth": ev.depth,
            "margin": ev.margin,
            "conf": ev.conf,
            "aim": [float(ev.aim[0]), float(ev.aim[1])],
        }

    # ------------------------------------------------------------------------------------------------- the view

    def _view(self, t: float) -> KeyboardView:
        plane = self._plane
        hands = self._hands
        fingers: list[FingerView] = self._press.fingers(hands) if plane is not None and hands else []
        self._notes(fingers, t)
        tips = self._tips(fingers, t)
        compose = self._machine.view(t, private=self.private) if self._machine is not None else None
        script = self._script
        practice = self._mode == "practice"
        echo = ""
        if not self.private:
            if practice and script is not None:
                echo = script.echo
            elif self._machine is None and not practice:
                echo = self._echo
        homes: tuple[tuple[float, float, Side], ...] = ()
        if self._phase != "placing":
            homes = tuple((u, v, side) for side, (u, v) in self._home.items())
        banner, banner_level = self._banner
        return KeyboardView(
            seq=0,
            mode=self._mode,
            phase=self._phase,
            lang=self.lang,
            shift=self._shift_on(t) and self.lang == "en",
            private=self.private,
            pulse=self.private and t < self._pulse_until,
            hold=self._hold,
            armed_enter=t < self._armed_enter_until,
            drift=self.drift,
            tips=tips,
            homes=homes,
            lit=self._lit(t, tips, compose),
            strip=self._strip(t, compose),
            echo=echo,
            prompt=script.prompt if script is not None and self._phase != "placing" else "",
            progress=self._progress(t),
            banner=banner,
            banner_level=banner_level,  # type: ignore[arg-type]
            commit=self._commit,
            compose=compose,
        )

    def _tips(self, fingers: list[FingerView], t: float) -> tuple[TipView, ...]:
        plane = self._plane
        if plane is None:
            return ()
        warmup = self._warmup if self._phase == "warmup" else None
        script = self._script
        drill = script.named if script is not None and self._phase == "typing" else None
        named = warmup.prompt if warmup is not None else None
        tips = []
        for fv in fingers:
            u, v = plane.units(fv.aim)
            who = (fv.side, fv.finger)
            tips.append(
                TipView(
                    u,
                    v,
                    fv.side,
                    fv.finger,
                    fv.state,
                    done=who in warmup.done if warmup is not None else True,
                    named=who in (named, drill),
                    fill=0.0 if self.private else fv.fill,
                    note=fv.note,
                )
            )
        return tuple(tips)

    def _lit(self, t: float, tips: tuple[TipView, ...], compose: ComposeView | None) -> tuple[tuple[int, LitKind], ...]:
        out: dict[int, LitKind] = {}

        def put(index: int | None, kind: LitKind) -> None:
            if index is None:
                return
            current = out.get(index)
            if current is None or _LIT_RANK[kind] > _LIT_RANK[current]:
                out[index] = kind

        layout = self._layout
        tol = self._tuning.edge_tolerance
        for tip in tips:
            key = layout.key_at(tip.u, tip.v, tol)
            if key is None:
                continue
            if tip.state == "pressed":
                put(key.index, "ok")
            else:
                put(key.index, "ghost")
                if tip.state == "closing" and (tip.fill >= _TARGET_FILL or self._press.name == "pinch"):
                    put(key.index, "target")
        if self._phase == "warmup" and self._warmup is not None and self._warmup.prompt in self._home_f:
            u, v = self._home_f[self._warmup.prompt]  # type: ignore[index]
            found = layout.key_at(u, v, tol)
            put(found.index if found is not None else None, "target")
        if self._script is not None and self._phase == "typing":
            put(self._script.target_key, "target")
        if self._shift_on(t) and self.lang == "en":
            put(self._by_kind.get("shift"), "armed")
        if t < self._armed_enter_until:
            put(self._by_kind.get("enter"), "armed")
        if compose is not None:
            if compose.guard is not None:
                put(self._by_kind.get(_GUARD_KEY[compose.guard]), "armed")
            if compose.state == "inserting":
                put(self._by_kind.get("insert"), "on")
            if compose.can_send:
                put(self._by_kind.get("enter"), "on")
        for index, (kind, until) in list(self._flash.items()):
            if t < until:
                put(index, kind)
            else:
                del self._flash[index]
        if self.private:
            out = {i: k for i, k in out.items() if layout.keys[i].kind not in ("char", "space")}
        return tuple(sorted(out.items()))

    def _progress(self, t: float) -> float:
        if self._fist_since is not None:
            return min(max((t - self._fist_since) / FIST_EXIT_S, 0.0), 1.0)
        script = self._script
        if script is not None and script.progress > 0:
            return script.progress
        warmup = self._warmup
        if self._phase == "warmup" and warmup is not None and warmup.required:
            return len(warmup.done) / len(warmup.required)
        return 0.0

    # ----------------------------------------------------------------------------------------------- the strip

    def _notes(self, fingers: list[FingerView], t: float) -> None:
        """Strip hints from the press method's notes (2.12.9): after 1.5 s of one note, at most one hint per 5 s."""
        seen = {fv.note for fv in fingers}
        for note in HINT_NOTES:
            if note in seen:
                self._note_since.setdefault(note, t)
            else:
                self._note_since.pop(note, None)
        if self.private:
            self._hint = None
            return
        if self._hint is not None and t >= self._hint[1]:
            self._hint = None
        if self._hint is None and t - self._hint_t >= _HINT_GAP_S - _EPS:
            for note in HINT_NOTES:
                began = self._note_since.get(note)
                if began is not None and t - began >= _HINT_AFTER_S - _EPS:
                    self._hint = (note, t + _HINT_SHOW_S)
                    self._hint_t = t
                    break
        if not self._weak_done and self._phase == "typing":
            for fv in fingers:
                if fv.note == "weak":
                    self._weak_done = True
                    self._weak = (FINGER_NAMES[fv.finger].capitalize(), t + _WEAK_SHOW_S)
                    break

    def _strip(self, t: float, compose: ComposeView | None) -> str:
        if self._hold is not None:
            return SESSION_TEXT["paused"].format(why=HOLD_WHY[self._hold])
        left = self._idle_left(t)
        if left is not None:
            return REVIEW_TEXT["idle"].format(s=left)
        if self._hint is not None:
            return SESSION_TEXT[self._hint[0]]
        if self._weak is not None and t < self._weak[1]:
            return SESSION_TEXT["weak"].format(Finger=self._weak[0])
        if self._phase == "placing":
            return SESSION_TEXT["place"]
        if self._phase == "warmup":
            return self._warm_strip(t)
        if self._cut:
            return PRACTICE_TEXT["not_usable"]
        busy = compose is not None and (compose.guard is not None or compose.state == "inserting")
        if self.drift and not busy:
            return SESSION_TEXT["drifted"]
        if self._script is not None:
            if t < self._practice_note_until:
                return REVIEW_TEXT["practice"]
            return self._script.strip(t)
        where = self._target or UNKNOWN_TARGET
        if self._machine is not None:
            return self._machine.strip(t, self._target)
        if t < self._armed_enter_until:
            return SESSION_TEXT["enter_again"]
        return SESSION_TEXT["typing"].format(target=where, lang=self.lang.upper())

    def _idle_left(self, t: float) -> int | None:
        """Seconds left before the idle close, only while it would throw a box away (2.13.8)."""
        machine = self._machine
        if machine is None or len(machine.buffer) == 0 or self._hands or machine.running:
            return None
        left = self._idle_limit() - (t - self._last_hand_t)
        return math.ceil(left - _EPS) if left <= IDLE_WARN_S else None

    def _warm_strip(self, t: float) -> str:
        warmup = self._warmup
        assert warmup is not None
        n, total = len(warmup.done), len(warmup.required)
        if self._press.name != "air":
            return SESSION_TEXT["pinch"].format(n=n, N=total)
        if t < self._restart_until:
            return SESSION_TEXT["restart"]
        if not self.private:
            hint = self._stuck_hint(t)
            if hint is not None:
                return hint
        prompt = warmup.prompt
        if prompt is None:
            return SESSION_TEXT["good"].format(n=n, N=total)
        return SESSION_TEXT["tap"].format(side=prompt[0], finger=FINGER_NAMES[prompt[1]], n=n, N=total)

    def _stuck_hint(self, t: float) -> str | None:
        """The two hints of a warm-up that does not go on (2.12.5): each once, for a few seconds."""
        warmup = self._warmup
        assert warmup is not None
        if "all" not in self._stuck_done and t - self._accept_t >= _STUCK_ALL_S - _EPS:
            self._stuck_done.add("all")
            self._stuck["all"] = t + _STUCK_SHOW_S
        if (
            "finger" not in self._stuck_done
            and warmup.prompt is not None
            and t - self._named_since >= _STUCK_FINGER_S - _EPS
        ):
            self._stuck_done.add("finger")
            self._stuck["finger"] = t + _STUCK_SHOW_S
            self._stuck_who = warmup.prompt
        if t < self._stuck.get("all", -math.inf):
            return SESSION_TEXT["stuck_all"]
        if t < self._stuck.get("finger", -math.inf):
            side, finger = self._stuck_who
            return SESSION_TEXT["stuck_finger"].format(Side=side.capitalize(), finger=FINGER_NAMES[finger])
        return None
