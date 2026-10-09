"""Press by tapping a finger in the air: the detector of DESIGN-KEYBOARD.md 2.12, the default method.

A finger is tapped downward over the key. The depth signal is the finger's lift (how far the PIP, DIP and tip stand
along the hand's own axis) against its own rest value, with what the whole hand did removed; a tap is a *completed peak*
of that signal, found when the finger is already coming back, and it fires only if every gate on the hand and on the
candidate agrees. The key is the one under that finger's tip *before* the dip (2.12.3), never at the peak.

This is the port of ``/tmp/claude-0/kbd/sim-air/airtap_ref.py``, step for step: the comments S0 to S13 are the design's
steps, and tests X2 and X3 pin the port against the reference's recorded events and counters. Change a number or an
order here and those two tests are the ones to read first.

Pure Python: ``math`` and ``collections`` only. No clock (``HandSample.t`` is the only time), no I/O, no numpy: the hot
path costs about 0.1 ms per hand-frame. Hand samples are read, never kept.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from . import limits
from .tuning import Tuning
from .types import FingerView, HandSample, PressEvent, PressLevel, PressName, PressQuality, Side, TipState

LATCHED: Final = "latched"
OPEN: Final = "open"
CLOSING: Final = "closing"
PRESSED: Final = "pressed"

# Layout of a history record: time, depth with the common mode removed, raw depth, aim x, aim y.
T_, DL_, D_, AX_, AY_ = 0, 1, 2, 3, 4

#: The noise estimate of a finger before it has twelve quiet records: a threshold of 0.15 for the first samples.
_SIGMA_INIT = 0.03
#: A finger gate shows as a ring note only after it has been closed for this long, so it never flickers.
_NOTE_AFTER_S = 0.5
#: ``veto`` shows this long after a peak was lost to a neighbour.
_VETO_NOTE_S = 0.3
#: The hand gates the overlay may show (the others, ``warm`` and ``score``, are not for the user).
_NOTE_GATES = ("speed", "posture", "coherence", "hold")
#: A finger is ``noisy`` when its noise is this many times the hand's median.
_NOISY_X = 1.6


@dataclass(frozen=True)
class _Cfg:
    """The detector's constants: ``Tuning`` for the accuracy numbers, ``limits`` for the defences, code for the rest.

    The names are the reference's ``AirCfg`` fields, so the two files can be read side by side. The floors of the
    tuning clamps are applied again here, so a ``Tuning`` built in code cannot go below them either.
    """

    # fixed in code
    smooth_fps_3: float = 20.0
    smooth_fps_5: float = 40.0
    qwin_s: float = 1.5
    quantile: float = 0.85
    e_fall_per_s: float = 0.15
    sigma_lag_s: float = 0.20
    sigma_win_s: float = 3.0
    sigma_min_n: int = 12
    quiet_speed: float = 0.12
    lo_k: float = 4.0
    lo_min: float = 0.07
    floor_frac: float = 0.75
    speed_span_s: float = 0.10
    gap_long_x: float = 2.5
    gap_win_s: float = 5.0
    gap_absent_s: float = 1.0
    vmax_pre_s: float = 0.10
    coupled_onset_s: float = 0.08
    wide_onset_s: float = 0.16
    wide_ratio: float = 0.6
    hist_s: float = 0.60
    # tuning
    theta_k: float = 5.0
    theta_min: float = 0.10
    theta_max: float = 0.25
    depth_frac: float = 0.5
    back_s: float = 0.20
    rise_win_s: float = 0.16
    fall_win_s: float = 0.12
    return_frac: float = 0.5
    width_min_s: float = 0.04
    width_max_s: float = 0.30
    speed_gate: float = 0.5
    vmax_gate: float = 0.5
    veto_ratio: float = 0.7
    aim_rule: str = "auto"
    aim_speed: float = 0.05
    # limits
    sigma_floor: float = limits.AIR_SIGMA_FLOOR
    min_visible_s: float = limits.AIR_MIN_VISIBLE_S
    min_samples: int = limits.AIR_MIN_SAMPLES
    min_score: float = limits.AIR_MIN_SCORE
    min_posture_lift: float = limits.AIR_MIN_POSTURE_LIFT
    posture_fingers: int = limits.AIR_POSTURE_FINGERS
    coherence_n: int = limits.AIR_COHERENCE_N
    coherence_ratio: float = limits.AIR_COHERENCE_RATIO
    coherence_peer: float = limits.AIR_COHERENCE_PEER
    coherence_hold_s: float = limits.AIR_COHERENCE_HOLD_S
    coh_back_s: float = limits.AIR_COH_BACK_S
    gap_reset_s: float = limits.GAP_RESET_S
    jump_fw: float = limits.AIR_JUMP_FW
    jump_hold_s: float = limits.AIR_JUMP_HOLD_S
    settle_frames: int = limits.AIR_SETTLE_FRAMES
    pinch_gap: float = limits.AIR_PINCH_GAP
    refractory_s: float = limits.AIR_REFRACTORY_S
    hand_excl_s: float = limits.AIR_HAND_EXCL_S
    tremor_n: int = limits.AIR_TREMOR_N
    tremor_window_s: float = limits.AIR_TREMOR_WINDOW_S
    tremor_hold_s: float = limits.AIR_TREMOR_HOLD_S
    flash_s: float = limits.AIR_FLASH_S
    #: What ``set_level("degraded")`` multiplies every threshold by.
    degraded_mult: float = limits.AIR_THETA_MULT_DEGRADED


def _config(tuning: Tuning) -> _Cfg:
    return _Cfg(
        theta_k=max(tuning.air_theta_k, limits.AIR_THETA_K_FLOOR),
        theta_min=max(tuning.air_theta_min, limits.AIR_THETA_FLOOR),
        theta_max=tuning.air_theta_max,
        depth_frac=tuning.air_depth_frac,
        back_s=tuning.air_back_s,
        rise_win_s=tuning.air_rise_win_s,
        fall_win_s=tuning.air_fall_win_s,
        return_frac=tuning.air_return_frac,
        width_min_s=tuning.air_width_min_s,
        width_max_s=tuning.air_width_max_s,
        speed_gate=tuning.air_speed_gate,
        vmax_gate=tuning.air_vmax_gate,
        veto_ratio=max(tuning.air_veto_ratio, limits.AIR_VETO_FLOOR),
        aim_rule={"onset": "onset3", "commit": "fire"}.get(tuning.air_aim, "auto"),
        aim_speed=tuning.air_aim_speed,
    )


def _med(xs: Sequence[float]) -> float:
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def _med2(pts: Sequence[tuple[float, float]]) -> tuple[float, float]:
    return (_med([p[0] for p in pts]), _med([p[1] for p in pts]))


def _quant(xs: Sequence[float], q: float) -> float:
    s = sorted(xs)
    pos = q * (len(s) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


@dataclass
class _Hand:
    """Everything the detector knows about one hand. A reset builds a fresh one and carries only ``fps``."""

    hid: int
    side: Side
    first_seen: float
    fps: float = 30.0
    last_t: float | None = None
    raw: deque[list[float]] = field(default_factory=lambda: deque(maxlen=5))
    vals: list[deque[tuple[float, float]]] = field(default_factory=lambda: [deque() for _ in range(4)])
    #: The rest scale of each finger's lift; ``seeded`` once the first frame has set it.
    E: list[float] = field(default_factory=lambda: [0.0] * 4)
    seeded: bool = False
    sigma: list[float] = field(default_factory=lambda: [_SIGMA_INIT] * 4)
    qd: list[deque[tuple[float, float]]] = field(default_factory=lambda: [deque() for _ in range(4)])
    theta: list[float] = field(default_factory=lambda: [0.1] * 4)
    delta: list[float] = field(default_factory=lambda: [0.0] * 4)
    d: list[float] = field(default_factory=lambda: [0.0] * 4)
    cm: float = 0.0
    hist: list[deque[tuple[float, float, float, float, float]]] = field(
        default_factory=lambda: [deque() for _ in range(4)]
    )
    anchors: deque[tuple[float, tuple[float, float]]] = field(default_factory=deque)
    spd: deque[tuple[float, float]] = field(default_factory=deque)
    speed: float = 0.0
    state: list[TipState] = field(default_factory=lambda: [LATCHED] * 4)
    settle: list[int] = field(default_factory=lambda: [0] * 4)
    last_up: list[float] = field(default_factory=lambda: [-9.0] * 4)
    done_pk: list[float] = field(default_factory=lambda: [-9.0] * 4)
    flash_until: list[float] = field(default_factory=lambda: [-9.0] * 4)
    cand: list[dict[str, Any] | None] = field(default_factory=lambda: [None] * 4)
    #: The provisional aim while a rise is in progress: frozen at the left base, or the live tip while the hand moves.
    prov: list[tuple[float, float] | None] = field(default_factory=lambda: [None] * 4)
    fill: list[float] = field(default_factory=lambda: [0.0] * 4)
    #: What the finger's ring shows (2.12.9), decided once per frame.
    note: list[str] = field(default_factory=lambda: [""] * 4)
    #: The aim of the commit, shown while the finger reads ``pressed``.
    fired_aim: list[tuple[float, float]] = field(default_factory=lambda: [(0.0, 0.0)] * 4)
    veto_until: list[float] = field(default_factory=lambda: [-9.0] * 4)
    suppress_until: float = -9.0
    last_fire: float = -9.0
    fires: deque[float] = field(default_factory=deque)
    #: (commit time, peak time, finger, depth) of the last 0.6 s: the coupling veto looks back at these.
    recent: deque[tuple[float, float, int, float]] = field(default_factory=deque)
    gate: str = "warm"
    #: The level multiplier this hand's thresholds use now; it follows ``set_level`` between taps, never during one.
    mult: float = 1.0
    #: When the gate that is closed now first closed (None while open); for the notes that wait 0.5 s.
    gate_since: float | None = None


class AirTapPress:
    name: PressName = "air"
    requires_review = True
    #: Counters by reason (2.12.10): gates as ``g_<gate>``, candidate gates as ``gate_<name>`` and the rest by name.
    rejects: dict[str, int]

    def __init__(self, tuning: Tuning, *, trace: bool = False, calibrating: bool = False) -> None:
        self._c = _config(tuning)
        self._hands: dict[int, _Hand] = {}
        self.rejects: dict[str, int] = {}
        #: (side, finger) -> the warm-up tap depth D, 0.10 to 0.80.
        self._depth: dict[tuple[Side, int], float] = {}
        self._calibrating = calibrating
        self._mult = 1.0
        self._trace_on = trace
        #: Tap-log records (``take_trace``): fires, rejects and gates, in the shape of 2.12.10.
        self._trace: list[dict[str, Any]] = []
        self._fps = 30.0
        self._fps_seen = False
        #: Times of holes in the sample stream (``PressQuality.gaps``).
        self._holes: deque[float] = deque()
        self._now = 0.0

    # ------------------------------------------------------------------------------------------ the PressMethod

    def reset(self) -> None:
        """Every finger latched, nothing pending. The noise and rest estimates and the warm-up depths are kept."""
        for st in self._hands.values():
            for i in range(4):
                st.state[i] = LATCHED
                st.settle[i] = 0
                st.cand[i] = None
                st.prov[i] = None
                st.flash_until[i] = -9.0
                st.fill[i] = 0.0
                st.veto_until[i] = -9.0
                st.last_up[i] = st.last_t if st.last_t is not None else -9.0
                st.done_pk[i] = st.last_t if st.last_t is not None else -9.0
            st.recent.clear()

    def set_finger(self, side: Side, finger: int, close: float, open_: float | None = None) -> None:
        """The warm-up result. For the air tap ``close`` is the finger's tap depth D (clamped to 0.10 to 0.80)."""
        if not math.isfinite(close):
            raise ValueError("a depth must be a finite number")
        self._depth[(side, finger)] = min(0.80, max(0.10, close))

    def set_level(self, level: PressLevel) -> None:
        """``degraded`` multiplies every threshold. A hand takes the new level at its next frame with no tap closing: a
        tap in progress finishes at the threshold it began with, and the next peak meets the new one."""
        self._mult = self._c.degraded_mult if level == "degraded" else 1.0

    def set_calibrating(self, on: bool) -> None:
        """The warm-up threshold rule of S6: the nominal threshold, without the depth and the ladder's multiplier."""
        self._calibrating = on

    def quality(self) -> PressQuality:
        """Tracked fps (0.0 until an interval has been measured), the median noise estimate, and the holes of 5 s."""
        sig = [st.sigma[i] for st in self._hands.values() for i in range(4) if len(st.qd[i]) >= self._c.sigma_min_n]
        return PressQuality(self._fps if self._fps_seen else 0.0, _med(sig) if sig else None, self.gaps())

    def gaps(self) -> int:
        """Holes in the sample stream in the last 5 s (an interval over 2.5 nominal ones, under 1 s). Counting only."""
        while self._holes and self._now - self._holes[0] > self._c.gap_win_s:
            self._holes.popleft()
        return len(self._holes)

    def fingers(self, hands: Sequence[HandSample]) -> list[FingerView]:
        """One view per finger (2.12.9). A closed hand gate reads as ``latched`` (nothing can commit now); the arming
        ring (``fill``) is the rise so far over the threshold, so it fills before the state turns to ``closing``."""
        views = []
        for hand in hands:
            st = self._hands.get(hand.hand)
            for f in hand.fingers:
                i = f.finger
                tip = (float(f.aim[0]), float(f.aim[1]))
                if st is None:
                    views.append(FingerView(hand.hand, hand.side, i, tip, LATCHED))
                    continue
                state = st.state[i]
                note = self._note(st, i, hand.t)
                if st.gate and state != PRESSED:
                    views.append(FingerView(hand.hand, hand.side, i, tip, LATCHED, 0.0, note))
                    continue
                if state == CLOSING and st.prov[i] is not None:
                    tip = st.prov[i]
                elif state == PRESSED:
                    tip = st.fired_aim[i]
                views.append(FingerView(hand.hand, hand.side, i, tip, state, st.fill[i], note))
        return views

    def gate_of(self, hand: int) -> str | None:
        """The hand gate that is closed now (``warm``, ``score``, ``hold``, ``speed``, ``posture``, ``coherence``), ""
        when none is, None for a hand the detector has not seen."""
        st = self._hands.get(hand)
        return st.gate if st is not None else None

    def take_trace(self) -> list[dict[str, Any]]:
        """The tap-log records since the last call (only when built with ``trace=True``), oldest first."""
        out, self._trace = self._trace, []
        return out

    def theta(self, hand: int, finger: int) -> float | None:
        """The finger's current threshold, depth units; None for a hand the detector has not seen."""
        st = self._hands.get(hand)
        return st.theta[finger] if st is not None else None

    def sigma(self, hand: int, finger: int) -> float | None:
        """The finger's current noise estimate; None for a hand the detector has not seen."""
        st = self._hands.get(hand)
        return st.sigma[finger] if st is not None else None

    def depth_of(self, side: Side, finger: int) -> float | None:
        """The warm-up depth D this finger was given, if any."""
        return self._depth.get((side, finger))

    def update(self, hands: Sequence[HandSample]) -> list[PressEvent]:
        events: list[PressEvent] = []
        live = {h.hand for h in hands}
        if hands:
            tmax = max(h.t for h in hands)
            for hid in [
                k for k, s in self._hands.items() if k not in live and s.last_t is not None and tmax - s.last_t > 0.20
            ]:
                del self._hands[hid]
        for hs in hands:
            st = self._hands.get(hs.hand)
            if st is None:
                st = self._hands[hs.hand] = _Hand(hs.hand, hs.side, hs.t, self._fps)
            events += self._hand(st, hs)
        return events

    # ------------------------------------------------------------------------------------------ bookkeeping

    def _rej(self, why: str) -> None:
        self.rejects[why] = self.rejects.get(why, 0) + 1

    def _tr(self, **kw: Any) -> None:
        if self._trace_on:
            self._trace.append(kw)

    def _hole(self, t: float) -> None:
        if not self._holes or t - self._holes[-1] > 0.30:
            self._holes.append(t)

    def _note(self, st: _Hand, i: int, t: float) -> str:
        """The ring note of finger ``i`` (2.12.9): a hand gate once it has held 0.5 s, else veto, weak, noisy."""
        gate = st.gate
        if gate in _NOTE_GATES and st.gate_since is not None and t - st.gate_since >= _NOTE_AFTER_S:
            return gate
        if t < st.veto_until[i]:
            return "veto"
        side_d = self._depth.get((st.side, i))
        sg = st.sigma[i]
        if side_d is not None:
            nominal = min(self._c.theta_max, max(self._c.theta_min, self._c.theta_k * sg))
            lo = max(self._c.lo_min, self._c.lo_k * sg, self._c.floor_frac * nominal)
            if side_d < 2 * lo:
                return "weak"
        if sg > _NOISY_X * _med(st.sigma) and len(st.qd[i]) >= self._c.sigma_min_n:
            return "noisy"
        return ""

    def theta_of(self, st: _Hand, side: Side, i: int) -> float:
        c = self._c
        sg = st.sigma[i]
        nominal = min(c.theta_max, max(c.theta_min, c.theta_k * sg))
        if self._calibrating:
            # Never more sensitive than typing: the warm-up asks for depth 0.10 at least (AIR_WARMUP_MIN_DEPTH, the
            # default theta_min), so a lower bar here only turns the resting fingers' noise into strays that restart
            # the sequence (at landmark noise 0.002 a 4 sigma bar armed 4 of 12 two-hand runs, the typing bar 12 of
            # 12). The depth and the ladder's multiplier belong to the typing threshold below.
            return nominal
        depth = self._depth.get((side, i))
        if depth is not None:
            lo = max(c.lo_min, c.lo_k * sg, c.floor_frac * nominal)
            nominal = max(lo, min(nominal, c.depth_frac * depth))
        return nominal * st.mult

    # ------------------------------------------------------------------------------------------ one hand, one frame

    def _hand(self, st: _Hand, s: HandSample) -> list[PressEvent]:
        c = self._c
        out: list[PressEvent] = []
        t = s.t
        # S0 resets
        if st.last_t is not None:
            if t - st.last_t > c.gap_reset_s:
                self._rej("gap_reset")
                if t - st.last_t < c.gap_absent_s:
                    self._hole(t)
                keep = st.fps
                self._hands[st.hid] = st = _Hand(st.hid, s.side, t, keep)
            elif st.anchors:
                a0 = st.anchors[-1][1]
                if math.hypot(s.anchor[0] - a0[0], s.anchor[1] - a0[1]) > c.jump_fw:
                    self._rej("jump")
                    keep, hold = st.fps, st.suppress_until
                    self._hands[st.hid] = st = _Hand(st.hid, s.side, t, keep)
                    st.suppress_until = max(hold, t + c.jump_hold_s)
        dt = (t - st.last_t) if st.last_t is not None else 1 / 30
        # S1 frame rate and holes
        if st.last_t is not None and c.gap_long_x / st.fps < dt <= c.gap_reset_s:
            self._hole(t)  # a hole in the stream (a longer one was counted at S0)
        self._now = max(self._now, t)
        if st.last_t is not None and 0.004 < dt < 0.25:
            st.fps += 0.05 * (1.0 / dt - st.fps)
            self._fps += 0.02 * (1.0 / dt - self._fps)
            self._fps_seen = True
        st.last_t = t
        st.side = s.side
        st.anchors.append((t, (float(s.anchor[0]), float(s.anchor[1]))))
        while st.anchors and t - st.anchors[0][0] > 1.2:
            st.anchors.popleft()
        # S2 smoothing: the causal median of the last n raw lifts, n by frame rate
        st.raw.append([f.lift for f in s.fingers])
        n = 1 if st.fps < c.smooth_fps_3 else (3 if st.fps < c.smooth_fps_5 else 5)
        n = min(n, len(st.raw))
        rows = list(st.raw)[-n:]
        vals = [_med([r[i] for r in rows]) for i in range(4)]
        for i in range(4):
            st.vals[i].append((t, vals[i]))
            while st.vals[i] and t - st.vals[i][0][0] > c.qwin_s:
                st.vals[i].popleft()
        warm = (t - st.first_seen) >= c.min_visible_s and len(st.anchors) >= c.min_samples
        # S3 rest scale E and depth, the common mode removed
        for i in range(4):
            eq = _quant([v for _, v in st.vals[i]], c.quantile)
            st.E[i] = max(eq, st.E[i] * (1.0 - c.e_fall_per_s * dt)) if st.seeded else eq
        st.seeded = True
        d = [min(1.5, max(-0.5, 1.0 - vals[i] / max(st.E[i], 1e-3))) for i in range(4)]
        cm = _med(d)
        st.cm, st.d = cm, d
        for i in range(4):
            st.delta[i] = d[i] - cm
            st.hist[i].append((t, st.delta[i], d[i], float(s.fingers[i].aim[0]), float(s.fingers[i].aim[1])))
            while st.hist[i] and t - st.hist[i][0][0] > c.hist_s:
                st.hist[i].popleft()
        # S4 hand speed (knuckle anchor) over speed_span_s, and its short history for the motion gate
        speed = 0.0
        if len(st.anchors) >= 2:
            ref = st.anchors[0]
            for a in reversed(st.anchors):
                ref = a
                if t - a[0] >= c.speed_span_s:
                    break
            if t - ref[0] > 1e-6:
                speed = math.hypot(s.anchor[0] - ref[1][0], s.anchor[1] - ref[1][1]) / (t - ref[0])
        st.speed = speed
        st.spd.append((t, speed))
        while st.spd and t - st.spd[0][0] > c.hist_s:
            st.spd.popleft()
        # S5 noise (quiet samples only) and S6 threshold
        if CLOSING not in st.state:
            st.mult = self._mult
        quiet = speed < c.quiet_speed and all(abs(st.delta[i]) < 0.5 * st.theta[i] + 0.02 for i in range(4))
        lag = max(1, round(c.sigma_lag_s * st.fps))
        for i in range(4):
            h = st.hist[i]
            if quiet and len(h) > lag:
                st.qd[i].append((t, abs(h[-1][DL_] - h[-1 - lag][DL_])))
                while st.qd[i] and t - st.qd[i][0][0] > c.sigma_win_s:
                    st.qd[i].popleft()
                if len(st.qd[i]) >= c.sigma_min_n:
                    st.sigma[i] = max(c.sigma_floor, _med([v for _, v in st.qd[i]]) / 0.954)
            st.theta[i] = self.theta_of(st, s.side, i)
        # S7 hand gates: the first that fails is the hand's gate; the fingers keep updating but nothing commits
        ok, gate = True, ""
        if not warm:
            ok, gate = False, "warm"
        elif s.score < c.min_score:
            ok, gate = False, "score"
        elif t < st.suppress_until:
            ok, gate = False, "hold"
        if ok and speed > c.speed_gate:
            ok, gate = False, "speed"
        if ok and sum(1 for i in range(4) if st.E[i] >= c.min_posture_lift) < c.posture_fingers:
            ok, gate = False, "posture"
        dmax = max(st.delta)
        if ok and (
            sum(
                1
                for i in range(4)
                if st.delta[i] >= c.coherence_ratio * st.theta[i] and st.delta[i] >= c.coherence_peer * dmax
            )
            >= c.coherence_n
        ):
            st.suppress_until = t + c.coherence_hold_s
            ok, gate = False, "coherence"
            self._tr(k="gate", t=t, hand=st.hid, side=st.side, gate="coherence", dur=c.coherence_hold_s)
        if gate != st.gate or st.gate_since is None:
            st.gate_since = t if gate else None
        st.gate = gate
        if gate and gate != "warm":
            self._rej("g_" + gate)
        # S8 and S9, per finger
        cands = []
        for i in range(4):
            if st.state[i] == LATCHED:
                if ok and st.delta[i] < 0.5 * st.theta[i]:
                    st.settle[i] += 1
                    if st.settle[i] >= c.settle_frames:
                        st.state[i] = OPEN
                        st.last_up[i] = t  # a tap in progress when the finger opens is ignored
                        st.done_pk[i] = t
                else:
                    st.settle[i] = 0
                st.fill[i], st.prov[i] = 0.0, None
                continue
            if self._finger(st, i, s, t, ok, gate):
                cands.append(i)
        # S10 commit filter
        if cands:
            cands = self._filter(st, cands, t)
        # S11 winner takes all
        if cands and (t - st.last_fire) < c.hand_excl_s:
            for j in cands:  # inside hand_excl_s of this hand's last commit: lost, counted
                self._excl(st, j, t)
        if cands and (t - st.last_fire) >= c.hand_excl_s:
            cands.sort(key=lambda j: -self._dep(st, j))
            w = cands[0]
            dw = self._dep(st, w)
            second = 0.0
            for j in cands[1:]:
                dj = self._dep(st, j)
                second = max(second, dj)
                if dj < c.veto_ratio * dw:
                    self._veto(st, j, t)
                else:
                    self._excl(st, j, t)  # a comparable second candidate of the same frame: lost, counted
            cr = st.cand[w]
            assert cr is not None
            st.cand[w] = None
            st.state[w] = PRESSED
            st.flash_until[w] = t + c.flash_s
            st.last_up[w] = t
            st.last_fire = t
            st.fires.append(t)
            st.recent.append((t, cr["t_pk"], w, dw))
            conf = min(1.0, max(0.2, 0.5 * (dw / st.theta[w] - 1.0) + 0.5)) * (1.0 if speed < 0.25 else 0.7)
            # S12 the aim rule (2.12.3): the median of three aim samples around the left base when the hand was still
            # there (speed over the 0.10 s ending at the left base), else the aim of the commit frame
            onset = cr["a_on"]
            fire = (float(s.fingers[w].aim[0]), float(s.fingers[w].aim[1]))
            sp_l = self._spd_at(st, cr["t_l"])
            if c.aim_rule == "onset3":
                aim, rule = onset, "onset"
            elif c.aim_rule == "fire":
                aim, rule = fire, "commit"
            else:
                aim, rule = (fire, "commit") if sp_l > c.aim_speed else (onset, "onset")
            st.fired_aim[w] = aim
            margin = (dw - second) / st.theta[w] if cands[1:] else dw / st.theta[w]
            out.append(
                PressEvent(
                    t, cr["t_l"], st.hid, st.side, w, aim, float(s.fingers[w].ratio), margin, depth=dw, conf=conf
                )
            )
            if self._trace_on:
                hw = list(st.hist[w])
                self._trace.append(
                    dict(
                        k="fire", t=t, hand=st.hid, side=st.side, finger=w, onsetT=round(cr["t_l"], 3),
                        pkT=round(cr["t_pk"], 3), depth=round(dw, 3), theta=round(st.theta[w], 3),
                        sigma=round(st.sigma[w], 4), margin=round(margin, 2), conf=round(conf, 2),
                        widthMs=round(1000 * cr["width"]), riseMs=round(1000 * (cr["t_pk"] - cr["t_l"])),
                        fallMs=round(1000 * (t - cr["t_pk"])), speed=round(speed, 3), vmax=round(cr["vmax"], 3),
                        nPeers=cr["npeers"], E=[round(x, 3) for x in st.E],
                        delta=[round(x, 3) for x in st.delta],
                        win=[[round(1000 * (x[T_] - t)), round(x[DL_], 3), round(x[D_], 3)] for x in hw[-14:]],
                        aim=(round(aim[0], 4), round(aim[1], 4)), aimRule=rule, vl=round(sp_l, 3), fps=round(st.fps, 1),
                    )
                )  # fmt: skip
        # S13 tremor guard
        while st.fires and t - st.fires[0] > c.tremor_window_s:
            st.fires.popleft()
        if len(st.fires) >= c.tremor_n:
            st.suppress_until = t + c.tremor_hold_s
            st.fires.clear()
            out = []
            self._rej("tremor")
            self._tr(k="gate", t=t, hand=st.hid, side=st.side, gate="tremor", dur=c.tremor_hold_s)
        return out

    # ------------------------------------------------------------------------------------------ S8

    @staticmethod
    def _spd_at(st: _Hand, tt: float) -> float:
        """Knuckle-anchor speed (fw/s) over the 0.10 s that end at time ``tt`` (nearest samples)."""
        if not st.anchors:
            return 0.0
        a1 = min(st.anchors, key=lambda a: abs(a[0] - tt))[1]
        a0 = min(st.anchors, key=lambda a: abs(a[0] - (tt - 0.10)))[1]
        return math.hypot(a1[0] - a0[0], a1[1] - a0[1]) / 0.10

    def _stats(self, st: _Hand, i: int, t: float) -> dict[str, Any] | None:
        """Most recent peak of finger ``i``'s depth: peak value and time, left base (minimum before it), rise, fall."""
        c = self._c
        arm_from = max(t - c.back_s, st.last_up[i] + 0.02)
        full = list(st.hist[i])
        seg = [k for k, w in enumerate(full) if w[T_] >= arm_from]
        if len(seg) < 3:
            return None
        kp = max(seg, key=lambda k: (full[k][DL_], k))
        t_pk, d_pk = full[kp][T_], full[kp][DL_]
        lo = [k for k in range(kp + 1) if full[k][T_] >= t_pk - c.rise_win_s and full[k][T_] >= st.last_up[i] + 0.02]
        if not lo:
            return None
        kl = min(lo, key=lambda k: (full[k][DL_], -k))
        return dict(
            pk=d_pk, t_pk=t_pk, dl=full[kl][DL_], t_l=full[kl][T_], rise=d_pk - full[kl][DL_], fall=d_pk - st.delta[i],
            full=full, kl=kl, kp=kp,
        )  # fmt: skip

    def _finger(self, st: _Hand, i: int, s: HandSample, t: float, ok: bool, gate: str) -> bool:
        c = self._c
        th = st.theta[i]
        if t < st.flash_until[i]:
            st.state[i] = PRESSED
        elif st.state[i] == PRESSED:
            st.state[i] = OPEN
        b = self._stats(st, i, t)
        if b is None:
            st.fill[i], st.prov[i] = 0.0, None
            if st.state[i] == CLOSING:
                st.state[i] = OPEN
            return False
        rise, fall = b["rise"], b["fall"]
        full, kl = b["full"], b["kl"]
        age = t - b["t_pk"]
        st.fill[i] = min(1.0, max(0.0, rise / th)) if age <= 0.3 else 0.0
        fresh = b["t_pk"] > st.done_pk[i] + 1e-9
        if not fresh or rise < th:
            if st.state[i] == CLOSING:
                st.state[i] = OPEN
            st.prov[i] = None if st.state[i] != PRESSED else st.prov[i]
            return False
        if fall < c.return_frac * rise:
            if age <= 0.06 and st.state[i] == OPEN:
                st.state[i] = CLOSING
            if st.state[i] == CLOSING:
                if self._spd_at(st, b["t_l"]) <= c.aim_speed or c.aim_rule == "onset3":
                    st.prov[i] = (full[kl][AX_], full[kl][AY_])  # the aim is frozen at the left base during the rise
                else:
                    # the hand was still moving: the aim will be read at the commit frame, so show it live
                    st.prov[i] = (float(s.fingers[i].aim[0]), float(s.fingers[i].aim[1]))
            if age > c.fall_win_s:  # a plateau, not a tap
                st.done_pk[i] = b["t_pk"]
                st.last_up[i] = t - 0.02  # the history before now no longer counts as a base
                self._reject(st, i, t, "plateau", rise, th)
                if st.state[i] == CLOSING:
                    st.state[i] = OPEN
            return False
        # a completed peak
        st.done_pk[i] = b["t_pk"]
        if st.state[i] == CLOSING:
            st.state[i] = OPEN
        half = b["dl"] + 0.5 * rise
        k0 = b["kp"]
        while k0 > 0 and full[k0 - 1][DL_] >= half and full[k0 - 1][T_] >= st.last_up[i]:
            k0 -= 1
        k1 = b["kp"]
        while k1 < len(full) - 1 and full[k1 + 1][DL_] >= half:
            k1 += 1
        dts = [full[k][T_] - full[k - 1][T_] for k in range(1, len(full))]
        width = (k1 - k0 + 1) * (_med(dts) if dts else 1 / 30)
        # S9 candidate gates
        if width < c.width_min_s:
            return self._reject(st, i, t, "narrow", rise, th, width=width)
        if width > c.width_max_s:
            return self._reject(st, i, t, "wide", rise, th, width=width)
        if not ok or t - st.last_up[i] < c.refractory_s:
            return self._reject(st, i, t, "gate_" + (gate or "refractory"), rise, th)
        if s.fingers[i].ratio < c.pinch_gap:
            return self._reject(st, i, t, "pinched", rise, th)
        vmax = max([v for (tt, v) in st.spd if b["t_l"] - c.vmax_pre_s <= tt <= t] or [0.0])
        if vmax > c.vmax_gate:
            return self._reject(st, i, t, "motion", rise, th, vmax=vmax)
        a_on = _med2([(w[AX_], w[AY_]) for w in full[max(0, kl - 1) : kl + 2]])
        st.cand[i] = dict(
            t=t, t_pk=b["t_pk"], t_l=b["t_l"], pk=b["pk"], dl=b["dl"], width=width, vmax=vmax, a_on=a_on, npeers=0
        )
        return True

    def _reject(self, st: _Hand, i: int, t: float, why: str, rise: float, th: float, **kw: float) -> bool:
        self._rej(why)
        self._tr(
            k="reject", t=t, hand=st.hid, side=st.side, finger=i, why=why, rise=round(rise, 3), theta=round(th, 3),
            **{k: round(v, 3) for k, v in kw.items()},
        )  # fmt: skip
        return False

    def _dep(self, st: _Hand, i: int) -> float:
        cr = st.cand[i]
        assert cr is not None
        return cr["pk"] - cr["dl"]

    def _veto(self, st: _Hand, j: int, t: float) -> None:
        self._rej("veto")
        st.last_up[j] = t  # the vetoed stroke is consumed like a commit (refractory, new base), see _excl
        st.veto_until[j] = t + _VETO_NOTE_S
        self._tr(
            k="reject", t=t, hand=st.hid, side=st.side, finger=j, why="veto", rise=round(self._dep(st, j), 3),
            theta=round(st.theta[j], 3),
        )  # fmt: skip
        st.cand[j] = None
        st.state[j] = OPEN

    def _excl(self, st: _Hand, j: int, t: float) -> None:
        """The candidate is lost and counted. Its peak was consumed by S8 and S9 (``done_pk``) and its stroke is
        consumed like a commit (``last_up = t``: refractory, and the new base starts after it), so the tail of the same
        stroke is not found again as a second peak when the consumed one leaves the ``back_s`` window (without this
        line, at 60 fps the second finger of an exact chord was typed 0.18 s late)."""
        self._rej("excl")
        st.last_up[j] = t
        st.veto_until[j] = t + _VETO_NOTE_S
        self._tr(
            k="reject", t=t, hand=st.hid, side=st.side, finger=j, why="excl", rise=round(self._dep(st, j), 3),
            theta=round(st.theta[j], 3),
        )  # fmt: skip

    # ------------------------------------------------------------------------------------------ S10

    def _filter(self, st: _Hand, cands: list[int], t: float) -> list[int]:
        c = self._c
        while st.recent and t - st.recent[0][0] > 0.6:
            st.recent.popleft()
        # (a) whole-hand motion look-back: the peak-to-peak of the RAW depth (closing and opening) of every finger
        pp = []
        for j in range(4):
            h = [w[D_] for w in st.hist[j] if t - w[T_] <= c.coh_back_s]
            pp.append(max(h) - min(h) if h else 0.0)
        mx = max(pp)
        if (
            sum(1 for j in range(4) if pp[j] >= c.coherence_ratio * st.theta[j] and pp[j] >= c.coherence_peer * mx)
            >= c.coherence_n
        ):
            st.suppress_until = t + c.coherence_hold_s
            self._tr(k="gate", t=t, hand=st.hid, side=st.side, gate="coherence_raw", dur=c.coherence_hold_s)
            for j in cands:
                self._reject(st, j, t, "coherence_raw", self._dep(st, j), st.theta[j])
                st.cand[j] = None
            for j in range(4):
                if st.state[j] == CLOSING:
                    st.state[j] = OPEN
            return []
        # (b) the coupling veto
        keep = []
        for j in cands:
            dj = self._dep(st, j)
            cj = st.cand[j]
            assert cj is not None
            tk = cj["t_pk"]
            veto = False
            peers = 0
            for k in range(4):
                if k == j:
                    continue
                pb = self._stats(st, k, t)
                if (
                    pb is not None
                    and pb["rise"] > 0.5 * st.theta[k]
                    and abs(pb["t_pk"] - tk) <= max(c.coupled_onset_s, c.wide_onset_s)
                ):
                    dtk = abs(pb["t_pk"] - tk)
                    if dtk <= c.coupled_onset_s:
                        peers += 1
                        if dj < c.veto_ratio * pb["rise"]:
                            veto = True
                    elif dj < c.wide_ratio * pb["rise"]:
                        veto = True
            for _tc, tpk, k, dk in st.recent:
                if k == j:
                    continue
                dtk = abs(tpk - tk)
                near = dtk <= c.coupled_onset_s and dj < c.veto_ratio * dk
                wide = dtk <= c.wide_onset_s and dj < c.wide_ratio * dk
                if near or wide:
                    veto = True
            cj["npeers"] = peers
            if veto:
                self._veto(st, j, t)
            else:
                keep.append(j)
        return keep
