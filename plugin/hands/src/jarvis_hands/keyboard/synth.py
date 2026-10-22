"""Synthetic hands that type, for tests and ``run --fake`` (DESIGN-KEYBOARD.md 5.1).

Two families, both on the kinematic hand of ``jarvis_hands.synthetic`` (which this module does not touch):

* ``SynthHand`` and ``Typist`` make the frames of a pinch typist: hands that hover over the home row, move so a chosen
  fingertip is over a key, and close the thumb on that fingertip.
* ``AirTypist`` (with ``Noise``, ``Burst``, ``NegHand`` and ``scenario``) is a port of the air-tap study's simulator:
  taps are scripted as ``Event`` s and become finger flexion over a landmark noise model, and the negative scenarios
  are hands that move, roll, fidget and close without ever meaning a key.

``script`` is duck-typed: the ``Script`` of ``tests/scripted.py`` (which a package module cannot import) whose
``frame()`` calls ``.observe(size)`` on a hand. Pure: numpy and the hand model, no I/O, no clock.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

import numpy as np

from .. import synthetic as syn
from ..landmarks import Frame, HandObservation
from .hands import HandTracker
from .layout import Key, Layout, layout_for
from .plane import Plane
from .tuning import Tuning
from .types import Lang, Side

#: (MCP, PIP, DIP) degrees: the natural-feel typing posture, fingers up and slightly curved.
REST = (12, 18, 9)
#: The four fingers in the order of ``FingerSample.finger``.
FNAMES = ("index", "middle", "ring", "pinky")
#: Image height over width of the frame the hands are projected into (the pose space of ``y`` scales by this).
ASPECT = syn.HEIGHT / syn.WIDTH
_DEFAULT = Tuning()

Angles = tuple[float, float, float]
Posture = Literal["rest", "relaxed", "straight"]
_POSTURES: dict[str, Angles] = {
    "rest": (float(REST[0]), float(REST[1]), float(REST[2])),
    "relaxed": syn.RELAXED,
    "straight": syn.STRAIGHT,
}


def smooth(x: float) -> float:
    """Smoothstep of ``x`` clamped to 0..1: no jump in value or in speed at either end."""
    x = min(1.0, max(0.0, x))
    return x * x * (3 - 2 * x)


def lerp(a: Sequence[float], b: Sequence[float], k: float) -> tuple[float, ...]:
    return tuple(x + k * (y - x) for x, y in zip(a, b, strict=True))


# --- the air-tap simulator


@dataclass
class Event:
    """One scripted tap."""

    side: Side
    finger: int
    #: Tap start.
    t: float
    #: (du, dv) in key units from the finger's rest aim to the key centre.
    target: tuple[float, float]
    #: MCP flexion in degrees.
    amp: float
    #: Stroke, seconds.
    dur: float
    #: Truth, for the scoring of a run.
    key: int = -1
    ch: str = ""
    #: (neighbour finger, share) pairs, drawn once at construction of the hand from the seeded generator.
    cpl: tuple[tuple[int, float], ...] = ()
    #: Movement time of the hand and the finger to the key (Fitts-like, set by the planner).
    mt: float = 0.22


@dataclass(frozen=True)
class Noise:
    #: Frame widths per axis per landmark, independent AR(1).
    sigma: float = 0.001
    ar: float = 0.6
    #: Extra noise factor per frame width a second of hand speed.
    blur: float = 4.0
    #: Per hand-frame probability of one outlier landmark, and its size.
    glitch_p: float = 0.003
    glitch_fw: float = 0.03
    #: Per-tap "squeeze" gain on the measured dip.
    amp_gain: tuple[float, float] = (0.7, 1.1)
    #: Finger-coherent flexion noise in degrees (MCP; PIP gets the same), AR(1): MediaPipe predicts a finger as a unit.
    coh_deg: float = 0.0
    coh_ar: float = 0.7


#: Per style, MCP flexion degrees (mean, std) for index and middle (amps) and for ring and pinky (weak), and the stroke.
STYLES: dict[str, dict[str, tuple[float, float]]] = {
    "lazy": dict(amps=(28.0, 6.0), weak=(22.0, 6.0), dur=(0.20, 0.32)),
    "ordinary": dict(amps=(38.0, 8.0), weak=(30.0, 8.0), dur=(0.16, 0.28)),
    "decisive": dict(amps=(48.0, 6.0), weak=(40.0, 6.0), dur=(0.12, 0.20)),
    "tiny": dict(amps=(18.0, 4.0), weak=(14.0, 4.0), dur=(0.22, 0.34)),
}

_Segment = tuple[float, float, np.ndarray, np.ndarray]


class _Landmarks:
    """The landmark noise model both air hands share: AR(1) per landmark, speed blur and rare outliers."""

    def __init__(self, noise: Noise, rng: np.random.Generator, home: np.ndarray) -> None:
        self.noise = noise
        self.rng = rng
        self._ar = np.zeros((21, 2))
        self._coh = np.zeros((4, 2))
        self._prev = home.copy()

    def coherent(self, angles: dict[str, list[float]]) -> None:
        """Add the finger-coherent flexion noise to ``angles`` in place (one draw per frame, only when it is on)."""
        nz = self.noise
        if nz.coh_deg <= 0:
            return
        self._coh = nz.coh_ar * self._coh + math.sqrt(1 - nz.coh_ar**2) * self.rng.normal(0, 1, (4, 2))
        for i, name in enumerate(FNAMES):
            a0, a1, a2 = angles[name]
            angles[name] = [a0 + self._coh[i, 0] * nz.coh_deg, a1 + self._coh[i, 1] * nz.coh_deg, a2]

    def jitter(self, img: np.ndarray, at: np.ndarray, dt: float) -> None:
        """Noise on the image landmarks in place, scaled by how fast the hand moved since the last frame."""
        nz, rng = self.noise, self.rng
        speed = float(np.hypot(*((at - self._prev) * np.array([1.0, ASPECT])))) / dt if dt > 0 else 0.0
        self._prev = at.copy()
        sig = nz.sigma * (1 + nz.blur * speed)
        self._ar = nz.ar * self._ar + math.sqrt(1 - nz.ar**2) * rng.normal(0, 1, (21, 2))
        img[:, 0] += self._ar[:, 0] * sig
        img[:, 1] += self._ar[:, 1] * sig / ASPECT
        if rng.random() < nz.glitch_p:
            k = rng.integers(0, 21)
            img[k, :2] += rng.normal(0, nz.glitch_fw, 2) / np.array([1.0, ASPECT])


class AirTypist:
    """An air-tap hand: scripted taps over a noisy landmark model.

    The hand follows each event's target with a smoothstep of the movement time (``alpha`` of it as the whole hand, the
    rest as the finger alone), holds ``lead_s`` over the key, then flexes the finger by ``amp`` degrees with a
    sin-squared stroke. A neighbour follows the stroke by a seeded share (the coupling). Every draw comes from ``rng``
    in the order the study used, so a seed reproduces its streams.
    """

    def __init__(
        self,
        side: Side,
        home_at: tuple[float, float],
        events: Sequence[Event],
        rng: np.random.Generator,
        noise: Noise,
        alpha: float = 0.65,
        lead_s: float = 0.08,
        rest: Sequence[float] = REST,
        coupling_p: float = 0.4,
        coupling_share: tuple[float, float] = (0.10, 0.35),
        drift_fw_per_sqrt_s: float = 0.0,
    ) -> None:
        self.side, self.home = side, np.array(home_at, float)
        self.events = sorted(events, key=lambda e: e.t)
        self.noise, self.alpha, self.lead, self.rest = noise, alpha, lead_s, tuple(float(x) for x in rest)
        self.rng = rng
        self.drift_sigma = drift_fw_per_sqrt_s
        self.pitch = _DEFAULT.pitch
        self.gain_y = _DEFAULT.pitch_y_ratio
        self._lm = _Landmarks(noise, rng, self.home)
        self._drift = np.zeros(2)
        self._amp = [rng.uniform(*noise.amp_gain) for _ in self.events]
        for e in self.events:
            cp = []
            for nb in (e.finger - 1, e.finger + 1):
                if 0 <= nb < 4 and rng.random() < coupling_p:
                    cp.append((nb, float(rng.uniform(*coupling_share))))
            e.cpl = tuple(cp)
        self._build()

    def _build(self) -> None:
        """Hand displacement ``D(t)`` and per-finger local reach ``L_f(t)``: piecewise smoothsteps, no jumps."""
        self.segs: list[_Segment] = []
        self.fsegs: dict[int, list[_Segment]] = {f: [] for f in range(4)}
        for e in self.events:
            tgt = np.array(e.target, float)
            t_end_move = e.t - self.lead
            hold_to = e.t + e.dur + 0.02
            t0 = t_end_move - e.mt
            self._move(self.segs, t0, t_end_move, tgt)
            self.segs.append((t_end_move, hold_to, tgt, tgt))
            fs = self.fsegs[e.finger]
            self._move(fs, t0, t_end_move, tgt)
            fs.append((t_end_move, hold_to, tgt, tgt))
            fs.append((hold_to, hold_to + 0.18, tgt, np.zeros(2)))

    def _move(self, segs: list[_Segment], t0: float, t1: float, tgt: np.ndarray) -> None:
        """Append a move from the path's value at ``t0`` to ``tgt``; planned segments that start after ``t0`` go."""
        segs[:] = [sg for sg in segs if sg[0] <= t0]
        segs.append((t0, t1, self._eval(segs, t0), tgt))

    @staticmethod
    def _eval(segs: list[_Segment], t: float) -> np.ndarray:
        """Value of the path at ``t``: the segment that started last (ties: the later one) wins."""
        best = None
        for sg in segs:
            if sg[0] <= t and (best is None or sg[0] >= best[0]):
                best = sg
        if best is None:
            return np.zeros(2)
        t0, t1, a, b = best
        return a + (b - a) * smooth((t - t0) / max(t1 - t0, 1e-6))

    def displacement(self, t: float) -> np.ndarray:
        """The hand's planned displacement from home at ``t``, key units."""
        return self._eval(self.segs, t)

    def observe(self, t: float, dt: float) -> HandObservation:
        rng = self.rng
        d_hand = self.displacement(t)
        pitch_vec = np.array([self.pitch, self.pitch * self.gain_y])
        trans = self.alpha * d_hand * pitch_vec
        self._drift += rng.normal(0, self.drift_sigma * math.sqrt(dt), 2)
        at = self.home + (trans + self._drift) / np.array([1.0, ASPECT])
        angles = {n: list(self.rest) for n in FNAMES}
        for k, e in enumerate(self.events):
            if e.t <= t <= e.t + e.dur:
                b = math.sin(math.pi * (t - e.t) / e.dur) ** 2
                a = e.amp * self._amp[k] * b
                angles[FNAMES[e.finger]] = [self.rest[0] + a, self.rest[1] + 0.3 * a, self.rest[2] + 0.15 * a]
                for nb, share in e.cpl:
                    aa = a * share
                    if aa > angles[FNAMES[nb]][0] - self.rest[0]:
                        angles[FNAMES[nb]] = [self.rest[0] + aa, self.rest[1] + 0.3 * aa, self.rest[2] + 0.15 * aa]
        self._lm.coherent(angles)
        fingers = {n: syn._finger(n, (angles[n][0], angles[n][1], angles[n][2])) for n in FNAMES}
        world = syn._assemble(fingers, syn._thumb_tip("palm", fingers), self.side)
        obs = syn.hand("palm", (float(at[0]), float(at[1])), handedness=self.side, world=world)
        img = obs.image.copy()
        for f in range(4):
            if not self.fsegs[f]:
                continue
            reach = self._eval(self.fsegs[f], t)
            if not reach.any():
                continue
            v = ((1 - self.alpha) * reach * pitch_vec) / np.array([1.0, ASPECT])
            first = syn._FIRST[FNAMES[f]]
            img[first + 3, :2] += v
            img[first + 2, :2] += 0.6 * v
            img[first + 1, :2] += 0.25 * v
        self._lm.jitter(img, at, dt)
        return HandObservation(handedness=self.side, score=0.95, image=img, world=obs.world)


class Burst:
    """Wraps an ``AirTypist`` and adds correlated landmark noise inside each ``(t0, t1)`` window.

    A MediaPipe jitter burst (motion blur, partial occlusion, an exposure change): AR(1) 0.6 noise of ``sigma_b`` frame
    widths on a ``frac`` of the landmarks, chosen once per burst.
    """

    def __init__(
        self,
        hand: AirTypist,
        windows: Sequence[tuple[float, float]],
        sigma_b: float,
        rng: np.random.Generator,
        frac: float = 1.0,
    ) -> None:
        self.h, self.w, self.sb, self.rng, self.frac = hand, windows, sigma_b, rng, frac
        self._ar = np.zeros((21, 2))
        self._sel: tuple[int, np.ndarray] | None = None

    def observe(self, t: float, dt: float) -> HandObservation:
        o = self.h.observe(t, dt)
        act = next((i for i, (a, b) in enumerate(self.w) if a <= t < b), None)
        if act is None:
            self._sel = None
            return o
        if self._sel is None or self._sel[0] != act:
            self._sel = (act, self.rng.random(21) < self.frac)
        self._ar = 0.6 * self._ar + math.sqrt(1 - 0.36) * self.rng.normal(0, 1, (21, 2))
        img = o.image.copy()
        m = self._sel[1]
        img[m, 0] += self._ar[m, 0] * self.sb
        img[m, 1] += self._ar[m, 1] * self.sb / ASPECT
        return HandObservation(handedness=o.handedness, score=o.score, image=img, world=o.world)


# --- negative scenarios (no key is meant)


class NegHand:
    """Pose, position and roll from functions of ``t``; the same landmark noise model as ``AirTypist``."""

    def __init__(
        self,
        side: Side,
        home: tuple[float, float],
        rng: np.random.Generator,
        noise: Noise,
        angles: Callable[[float, str], Sequence[float]] | None = None,
        at: Callable[[float], tuple[float, float]] | None = None,
        roll: Callable[[float], float] | None = None,
        thumb: Callable[[float], float] | None = None,
        rest: Sequence[float] = REST,
    ) -> None:
        self.side, self.home, self.rng, self.nz, self.rest = side, np.array(home, float), rng, noise, rest
        self.angles = angles or (lambda t, n: rest)
        self.at = at or (lambda t: (0.0, 0.0))
        self.roll = roll or (lambda t: 0.0)
        self.thumb = thumb or (lambda t: 0.0)
        self._lm = _Landmarks(noise, rng, self.home)

    def observe(self, t: float, dt: float) -> HandObservation:
        ang = {n: list(self.angles(t, n)) for n in FNAMES}
        self._lm.coherent(ang)
        fingers = {n: syn._finger(n, (ang[n][0], ang[n][1], ang[n][2])) for n in FNAMES}
        tip0 = syn._thumb_tip("palm", fingers)
        tip = tip0 + self.thumb(t) * (np.array([-0.03, -0.085, -0.01]) - tip0)
        world = syn._assemble(fingers, tip, self.side)
        at = self.home + np.array(self.at(t))
        obs = syn.hand("palm", (float(at[0]), float(at[1])), handedness=self.side, world=world, roll=self.roll(t))
        img = obs.image.copy()
        self._lm.jitter(img, at, dt)
        return HandObservation(handedness=self.side, score=0.95, image=img, world=obs.world)


def ou_series(rng: np.random.Generator, n: int, tau: float, sigma: float) -> Callable[[float], float]:
    """A smooth random process sampled at 0.01 s (Ornstein-Uhlenbeck): a function of ``t``, linearly interpolated."""
    x = np.zeros(int(n))
    a = math.exp(-0.01 / tau)
    for i in range(1, len(x)):
        x[i] = a * x[i - 1] + math.sqrt(1 - a * a) * sigma * rng.normal()

    def series(t: float) -> float:
        k = min(max(t / 0.01, 0), len(x) - 2)
        i = int(k)
        w = k - i
        return float(x[i] * (1 - w) + x[i + 1] * w)

    return series


#: The negative scenarios by the four categories of the study; ``scenario`` builds each one.
NEG_CATEGORIES = {
    "rest": ["still"],
    "move": ["drift", "reach", "talk_hands"],
    "wave": ["wave", "wave_fast"],
    "open_close": ["open_close", "open_close_fast"],
    "stress": ["fidget", "finger_wiggle"],
}


def scenario(
    name: str, side: Side, rng: np.random.Generator, noise: Noise, rest: Sequence[float] = REST, duration: float = 62.0
) -> AirTypist | NegHand:
    """A hand that never means a key. ``reach`` is the one that moves the fingertips as if aiming (no flexion)."""
    home = (0.36, 0.55) if side == "left" else (0.64, 0.55)
    ph = rng.uniform(0, 6.28)
    if name == "still":
        return NegHand(side, home, rng, noise, rest=rest)
    if name == "drift":
        return NegHand(
            side,
            home,
            rng,
            noise,
            rest=rest,
            at=lambda t: (0.08 * math.sin(2 * math.pi * 0.1 * t + ph), 0.04 * math.sin(2 * math.pi * 0.07 * t)),
        )
    if name == "reach":
        events: list[Event] = []
        t = 1.2
        while t < duration:
            f = int(rng.integers(0, 4))
            events.append(Event(side, f, t, (rng.normal(0, 2.0), rng.normal(0, 1.0)), 0.0, 0.2))
            t += rng.uniform(0.35, 0.9)
        return AirTypist(side, home, events, rng, noise, rest=rest, coupling_p=0.0)
    if name == "wave":
        return NegHand(
            side,
            home,
            rng,
            noise,
            rest=rest,
            at=lambda t: (0.12 * math.sin(2 * math.pi * 0.8 * t + ph), 0.05 * math.sin(2 * math.pi * 0.4 * t)),
        )
    if name == "wave_fast":
        return NegHand(
            side,
            home,
            rng,
            noise,
            rest=rest,
            at=lambda t: (0.20 * math.sin(2 * math.pi * 1.6 * t + ph), 0.06 * math.sin(2 * math.pi * 0.8 * t)),
            roll=lambda t: 25 * math.sin(2 * math.pi * 1.6 * t + ph),
        )
    if name == "open_close":

        def slow(t: float, n: str) -> tuple[float, ...]:
            u = t % 3.0
            i = FNAMES.index(n) * 0.04
            k = smooth((u - 0.2 - i) / 0.35) - smooth((u - 1.4 - i) / 0.35)
            return lerp(rest, syn.CURLED, 0.75 * k)

        return NegHand(side, home, rng, noise, angles=slow, rest=rest)
    if name == "open_close_fast":

        def fast(t: float, n: str) -> tuple[float, ...]:
            u = t % 0.7
            i = FNAMES.index(n) * 0.03
            k = smooth((u - 0.05 - i) / 0.15) - smooth((u - 0.38 - i) / 0.15)
            return lerp(rest, syn.CURLED, 1.0 * k)

        return NegHand(side, home, rng, noise, angles=fast, rest=rest)
    if name == "finger_wiggle":

        def wiggle(t: float, n: str) -> tuple[float, ...]:
            idx = FNAMES.index(n)
            u = (t - idx * 1.0) % 4.0
            k = smooth((u - 0.2) / 0.5) - smooth((u - 1.2) / 0.5)
            return lerp(rest, (rest[0] + 30, rest[1] + 10, rest[2] + 5), max(0.0, k))

        return NegHand(side, home, rng, noise, angles=wiggle, rest=rest)
    if name == "roll":
        return NegHand(side, home, rng, noise, rest=rest, roll=lambda t: 18 * math.sin(2 * math.pi * 0.5 * t + ph))
    if name == "thumb":
        return NegHand(
            side,
            home,
            rng,
            noise,
            rest=rest,
            thumb=lambda t: 0.5 * (0.5 + 0.5 * math.sin(2 * math.pi * 0.3 * t + ph)),
        )
    if name in ("talk_hands", "talking", "fidget"):
        n = int(duration / 0.01) + 10
        ax = [ou_series(rng, n, 0.8, 0.07), ou_series(rng, n, 0.8, 0.04)]
        rl = ou_series(rng, n, 0.6, 12.0)
        if name == "talk_hands":
            return NegHand(side, home, rng, noise, rest=rest, at=lambda t: (ax[0](t), ax[1](t)), roll=rl)
        fl = [[ou_series(rng, n, tau, 14.0) for tau in (0.25, 0.8)] for _ in range(4)]

        def flex(t: float, name_: str) -> tuple[float, float, float]:
            i = FNAMES.index(name_)
            a = max(0.0, fl[i][0](t)) * 0.6 + max(0.0, fl[i][1](t)) * 0.8  # degrees of extra MCP flexion, 0 to about 30
            return (rest[0] + a, rest[1] + 0.3 * a, rest[2] + 0.15 * a)

        return NegHand(side, home, rng, noise, angles=flex, rest=rest, at=lambda t: (ax[0](t), ax[1](t)), roll=rl)
    raise KeyError(name)


# --- the pinch typist

#: The thumb tip of an open hand and where it ends on a fingertip it touches (the study's model of a pinch).
_OPEN_THUMB = np.array([-0.072, -0.085, -0.005])
_TOUCH = np.array([-0.004, 0.003, 0.002])
#: The thumb leads the finger: it has arrived when the finger is three quarters of the way.
_THUMB_LEAD = 1.3
#: The farthest the anchor moves in one frame during an approach (frame widths): the pinch's own speed gate allows 0.05.
_STEP = 0.03
#: Still frames between the end of an approach and the first closing frame.
_SETTLE = 4
#: Appendix C by the English legend: finger 0 index .. 3 pinky.
_LEFT_HAND = {
    **dict.fromkeys("qaz", 3),
    **dict.fromkeys("wsx", 2),
    **dict.fromkeys("edc", 1),
    **dict.fromkeys("rfvtgb", 0),
}
_RIGHT_HAND = {
    **dict.fromkeys("yhnujm", 0),
    **dict.fromkeys("ik,", 1),
    **dict.fromkeys("ol.", 2),
    **dict.fromkeys("p'/-?", 3),
}
#: Special keys: (side, finger); the review layout moves Backspace to the index and Enter becomes Send.
_SPECIAL: dict[str, tuple[Side, int]] = {
    "space": ("right", 0),
    "backspace": ("right", 3),
    "enter": ("right", 3),
    "close": ("right", 3),
    "insert": ("right", 3),
    "shift": ("left", 3),
    "lang": ("left", 3),
    "private": ("left", 3),
    "home": ("left", 3),
    "clear": ("left", 3),
}


def finger_of(key: Key, layout: Layout) -> tuple[Side, int]:
    """The standard fingering of Appendix C: the hand and finger (0 index .. 3 pinky) that types ``key``."""
    if layout.name == "review" and key.kind == "backspace":
        return ("right", 0)
    if layout.name == "review" and key.kind == "enter":
        return ("left", 2)
    if key.kind == "char":
        if key.en in _LEFT_HAND:
            return ("left", _LEFT_HAND[key.en])
        if key.en in _RIGHT_HAND:
            return ("right", _RIGHT_HAND[key.en])
    elif key.kind in _SPECIAL:
        return _SPECIAL[key.kind]
    raise KeyError("no fingering")


def _mix(a: tuple[float, float], b: tuple[float, float], k: float) -> tuple[float, float]:
    return (a[0] + k * (b[0] - a[0]), a[1] + k * (b[1] - a[1]))


def _hand_world(side: Side, posture: Posture, pinch: tuple[int, float] | None, coactivation: float) -> np.ndarray:
    base = _POSTURES[posture]
    angles = {name: base for name in FNAMES}
    thumb = _OPEN_THUMB
    fingers: dict[str, list[np.ndarray]]
    if pinch is None:
        fingers = {name: syn._finger(name, base) for name in FNAMES}
    else:
        finger, k = pinch
        k = min(1.0, max(0.0, float(k)))
        for name in FNAMES:
            share = 1.0 if name == FNAMES[finger] else coactivation
            a, b, c = (x + k * share * (y - x) for x, y in zip(base, syn.PINCHING, strict=True))
            angles[name] = (a, b, c)
        fingers = {name: syn._finger(name, angles[name]) for name in FNAMES}
        touching = fingers[FNAMES[finger]][3] + _TOUCH
        thumb = _OPEN_THUMB + min(1.0, _THUMB_LEAD * k) * (touching - _OPEN_THUMB)
    return syn._assemble(fingers, thumb, side)


@dataclass(frozen=True)
class SynthHand:
    """One hand of a pinch typist; ``Script.frame`` calls ``observe``."""

    side: Side = "right"
    #: Hand position, image fractions.
    at: tuple[float, float] = (0.5, 0.5)
    posture: Posture = "relaxed"
    #: (finger 0..3, k): k 0 open .. 1 touching; the thumb tip travels to that tip and the finger dips with it.
    pinch: tuple[int, float] | None = None
    #: The other fingers dip by this share of the pinching finger's dip.
    coactivation: float = 0.0
    #: Frame widths, xy, per landmark per frame.
    jitter: float = 0.0015
    z_noise: float = 0.004
    #: Without one, every call draws the same noise (a fixed offset, not a flicker).
    rng: np.random.Generator | None = None

    def observe(self, size: tuple[int, int]) -> HandObservation:
        world = _hand_world(self.side, self.posture, self.pinch, self.coactivation)
        rng = self.rng if self.rng is not None else np.random.default_rng(0)
        obs = syn.hand("palm", self.at, handedness=self.side, size=size, world=world, jitter=self.jitter, rng=rng)
        if self.z_noise:
            obs.image[:, 2] += rng.normal(0.0, self.z_noise, 21)
        return obs


def _fit(closing: int, held: int, opening: int, room: int) -> tuple[int, int, int]:
    """The phases of a press, shortened (the hold first) until the press fits in ``room`` frames or cannot shrink."""
    while closing + held + opening > room:
        if held > 1:
            held -= 1
        elif opening > 2:
            opening -= 1
        elif closing > 2:
            closing -= 1
        else:
            break
    return closing, held, opening


@dataclass(frozen=True)
class _Stroke:
    """One press: the finger, where the hand's anchor is while it presses, and the frame it starts closing."""

    side: Side
    finger: int
    anchor: tuple[float, float]
    onset: int
    closing: int
    held: int
    opening: int
    drift: tuple[float, float] = (0.0, 0.0)
    #: How far the pinch goes (1.0: the tips touch).
    reach: float = 1.0

    @property
    def cycle(self) -> int:
        return self.closing + self.held + self.opening


class Typist:
    """Builds frame lists on a ``Script``.

    Hands sit in pose space (frame widths, y scaled by the frame's aspect) and are moved so that the levelled tip of
    the chosen finger is over the key, which is what the tracker's default levelling reads. A press is an approach
    (a few frames, smooth, never faster than the pinch's own motion gate), a still pause, then the thumb closing on
    the finger, holding and opening. ``layout`` defaults to the plane's own rows (4: direct, 5: review).

    ``levels`` are the four levels (``Tuning.level_palm``) the hand is aimed with, that is the arc of this user's
    resting fingertips; the default is the tracker's own, so that the presses test the press and not the levelling.
    ``touch`` maps ``(side, finger)`` to how far that finger's pinch goes (1.0: the tips touch; less: they never meet).
    ``coactivation`` is the share of the pinching finger's dip that the other three follow.
    The default posture is ``rest``: fingers up and slightly curved, which is how the study measured the pinch. In the
    curled ``relaxed`` posture the tips lie 0.3 palms apart and the thumb on its way to one passes the next.
    """

    def __init__(
        self,
        script: Any,
        plane: Plane,
        *,
        rng: np.random.Generator,
        jitter: float = 0.0015,
        z_noise: float = 0.004,
        posture: Posture = "rest",
        layout: Layout | None = None,
        levels: Sequence[float] | None = None,
        touch: Mapping[tuple[Side, int], float] | None = None,
        coactivation: float = 0.0,
    ) -> None:
        self.script, self.plane, self.rng = script, plane, rng
        self.jitter, self.z_noise, self.posture, self.coactivation = jitter, z_noise, posture, coactivation
        self._levels = tuple(levels) if levels is not None else _DEFAULT.level_palm
        self._touch = dict(touch or {})
        self.layout = layout if layout is not None else layout_for("review" if plane.rows == 5 else "direct")
        self._aspect = script.height / script.width
        self._offsets: dict[Side, np.ndarray] = {}
        #: Every stroke rendered so far: (side, finger, t of its first closing frame), the truth a test scores against.
        self.closings: list[tuple[Side, int, float]] = []
        #: The hands in view, and each one's anchor.
        self._present: tuple[Side, ...] = ("left", "right")
        self._pos: dict[Side, tuple[float, float]] = {side: self.home(side) for side in ("left", "right")}

    # ------------------------------------------------------------------------------------------ geometry

    def offsets(self, side: Side) -> np.ndarray:
        """(4, 2): where each levelled tip of an open hand lies relative to its anchor, pose space."""
        if side not in self._offsets:
            size = (self.script.width, self.script.height)
            hand = SynthHand(side, (0.5, 0.5), self.posture, jitter=0.0, z_noise=0.0)
            tracker = HandTracker(replace(_DEFAULT, level_palm=self._levels))  # type: ignore[arg-type]
            sample = tracker.update(Frame(0.0, (hand.observe(size),), size[0], size[1]))[0]
            origin = np.array([0.5, 0.5 * self._aspect])
            self._offsets[side] = np.array([finger.aim for finger in sample.fingers]) - origin
        return self._offsets[side]

    def anchor_for(self, side: Side, finger: int, u: float, v: float) -> tuple[float, float]:
        """The anchor that puts ``finger``'s levelled tip at key position ``(u, v)``."""
        x, y = self.plane.pose(u, v)
        off = self.offsets(side)[finger]
        return (float(x - off[0]), float(y - off[1]))

    def home(self, side: Side) -> tuple[float, float]:
        """The anchor that puts the mean of the four levelled tips on the home row's cluster centre."""
        u = 2.0 if side == "left" else 8.0
        x, y = self.plane.pose(u, self.layout.home_v)
        mean = self.offsets(side).mean(axis=0)
        return (float(x - mean[0]), float(y - mean[1]))

    def position(self, side: Side) -> tuple[float, float]:
        """Where the hand's anchor is now, pose space."""
        return self._pos[side]

    def _hand(self, side: Side, anchor: tuple[float, float], pinch: tuple[int, float] | None = None) -> SynthHand:
        at = (anchor[0], anchor[1] / self._aspect)
        return SynthHand(side, at, self.posture, pinch, self.coactivation, self.jitter, self.z_noise, self.rng)

    def _frame(self, anchors: dict[Side, tuple[float, float]], pinch: dict[Side, tuple[int, float]]) -> Frame:
        return self.script.frame(*[self._hand(s, anchors[s], pinch.get(s)) for s in self._present])

    # ------------------------------------------------------------------------------------------ still and moving

    def hover(self, seconds: float, hands: Sequence[Side] = ("left", "right")) -> list[Frame]:
        """The ``hands`` (and only they, from now on) held still for ``seconds``."""
        self._present = tuple(hands)
        return [self._frame(self._pos, {}) for _ in range(self.script.count(seconds))]

    def glide(self, targets: dict[Side, tuple[float, float]]) -> list[Frame]:
        """The hands in ``targets`` move to their anchors together, smoothly, and stop."""
        start = {side: self._pos[side] for side in targets}
        frames = max(
            (math.ceil(math.hypot(a[0] - start[s][0], a[1] - start[s][1]) / _STEP) for s, a in targets.items()),
            default=0,
        )
        out = []
        for j in range(frames):
            k = smooth((j + 1) / frames)
            now = {**self._pos, **{s: _mix(start[s], a, k) for s, a in targets.items()}}
            out.append(self._frame(now, {}))
        self._pos.update(targets)
        return out

    def place(self) -> list[Frame]:
        """Both hands over the home row, still for one second (the placing phase of 2.4)."""
        self._present = ("left", "right")
        frames = self.glide({"left": self.home("left"), "right": self.home("right")})
        return frames + self.hover(1.0)

    def warm(self) -> list[Frame]:
        """One slow pinch per finger of the hands in view, left hand first, each held a moment (the warm-up of 2.5).

        The pinky goes first and the index last: the thumb on its way to a finger passes the ones nearer to it, and a
        record of the warm-up is the newest valid cycle, so each finger's own pinch has to come after the passes.
        """
        strokes: list[_Stroke] = []
        onset = _SETTLE
        for side in self._present:
            for finger in (3, 2, 1, 0):
                reach = self._touch.get((side, finger), 1.0)
                strokes.append(_Stroke(side, finger, self._pos[side], onset, 8, 5, 6, reach=reach))
                onset += 8 + 5 + 6 + 4
        return self._render(strokes)

    # ------------------------------------------------------------------------------------------ strokes

    def _stroke_for(self, key: Key, finger: int | None, side: Side | None) -> tuple[Side, int]:
        standard_side, standard_finger = finger_of(key, self.layout)
        return (side if side is not None else standard_side, finger if finger is not None else standard_finger)

    def _anchor_of(self, side: Side, finger: int, key: Key, bias: tuple[float, float]) -> tuple[float, float]:
        return self.anchor_for(side, finger, key.col + key.width / 2 + bias[0], key.row + 0.5 + bias[1])

    def _need(self, side: Side, anchor: tuple[float, float], start: tuple[float, float] | None = None) -> int:
        here = start if start is not None else self._pos[side]
        dist = math.hypot(anchor[0] - here[0], anchor[1] - here[1])
        return math.ceil(dist / _STEP) if dist > 1e-9 else 0

    def press(
        self,
        key: Key,
        *,
        finger: int | None = None,
        side: Side | None = None,
        closing: int = 5,
        held: int = 3,
        opening: int = 3,
        drift: float = 0.0,
        bias: tuple[float, float] = (0.0, 0.0),
    ) -> list[Frame]:
        """Move the chosen finger (the standard one by default) over ``key``, close on it, hold, reopen.

        ``drift`` is how far the hand slides sideways while the finger closes, in key pitches; ``bias`` offsets the
        aim from the key's centre, in key units (u, v).
        """
        who, which = self._stroke_for(key, finger, side)
        if who not in self._present:
            raise ValueError("that hand is not in view")
        anchor = self._anchor_of(who, which, key, bias)
        onset = self._need(who, anchor) + _SETTLE
        stroke = _Stroke(
            who,
            which,
            anchor,
            onset,
            closing,
            held,
            opening,
            (drift * self.plane.px, 0.0),
            self._touch.get((who, which), 1.0),
        )
        return self._render([stroke])

    def _strokes(self, keys: Sequence[Key], gap_s: float, closing: int, held: int, opening: int) -> list[_Stroke]:
        """Strokes of ``keys`` in the standard fingering, their closings ``gap_s`` apart where a hand can keep up."""
        step = max(1, round(gap_s * self.script.fps))
        closing, held, opening = _fit(closing, held, opening, step)
        strokes: list[_Stroke] = []
        last: dict[Side, _Stroke] = {}
        position = dict(self._pos)
        for key in keys:
            side, finger = finger_of(key, self.layout)
            if side not in self._present:
                raise ValueError("that hand is not in view")
            anchor = self._anchor_of(side, finger, key, (0.0, 0.0))
            need = self._need(side, anchor, position[side])
            previous = last.get(side)
            if previous is None:
                earliest = need + _SETTLE
            else:
                free_at = previous.onset + previous.closing + previous.held
                earliest = max(previous.onset + previous.cycle, free_at + (need + 1) // 2 + 1)
            onset = max(earliest, strokes[-1].onset + step if strokes else 0)
            stroke = _Stroke(
                side, finger, anchor, onset, closing, held, opening, reach=self._touch.get((side, finger), 1.0)
            )
            strokes.append(stroke)
            last[side] = stroke
            position[side] = anchor
        return strokes

    def type(
        self,
        text: str,
        *,
        gap_s: float = 0.35,
        lang: Lang = "en",
        closing: int = 5,
        held: int = 3,
        opening: int = 3,
    ) -> list[Frame]:
        """``text`` in the standard fingering (Appendix C), a press every ``gap_s`` seconds or as fast as a hand can.

        A press that would start before the same hand has finished the one before waits for it. The finger movements
        of the two hands overlap, as a typist's do.
        """
        return self._render(
            self._strokes([self.layout.find(char=ch, lang=lang) for ch in text], gap_s, closing, held, opening)
        )

    def tap_n(self, key: Key, n: int, gap_s: float) -> list[Frame]:
        """``n`` presses of ``key`` by its standard finger, ``gap_s`` apart (the three taps of Insert)."""
        return self._render(self._strokes([key] * n, gap_s, 5, 3, 3))

    # ------------------------------------------------------------------------------------------ frames

    def _render(self, strokes: Sequence[_Stroke]) -> list[Frame]:
        total = max((s.onset + s.cycle for s in strokes), default=0)
        self.closings += [(s.side, s.finger, self.script.t + s.onset * self.script.dt) for s in strokes]
        anchors: dict[Side, list[tuple[float, float]]] = {}
        pinches: dict[Side, list[tuple[int, float] | None]] = {}
        for side in self._present:
            anchors[side], pinches[side] = self._track(side, [s for s in strokes if s.side == side], total)
        frames = [
            self._frame(
                {s: anchors[s][n] for s in self._present}, {s: p for s in self._present if (p := pinches[s][n])}
            )
            for n in range(total)
        ]
        return frames

    def _track(
        self, side: Side, strokes: Sequence[_Stroke], total: int
    ) -> tuple[list[tuple[float, float]], list[tuple[int, float] | None]]:
        """The anchor and the pinch of ``side`` in each of ``total`` frames; the hand's last place is kept."""
        here = self._pos[side]
        anchors: list[tuple[float, float]] = [here] * total
        pinches: list[tuple[int, float] | None] = [None] * total
        free = 0
        for s in strokes:
            need = self._need(side, s.anchor, here)
            end = max(s.onset - _SETTLE, free)
            if need and end - free < 1:
                end = min(s.onset, free + 1)  # no room: the approach eats into the pause
            start = max(free, end - need)
            for n in range(free, start):
                anchors[n] = here
            for n in range(start, end):
                anchors[n] = _mix(here, s.anchor, smooth((n - start + 1) / (end - start)))
            for n in range(end, s.onset):
                anchors[n] = s.anchor
            for j in range(s.closing):
                anchors[s.onset + j] = (
                    s.anchor[0] + s.drift[0] * (j + 1) / s.closing,
                    s.anchor[1] + s.drift[1] * (j + 1) / s.closing,
                )
                pinches[s.onset + j] = (s.finger, s.reach * (j + 1) / s.closing)
            here = (s.anchor[0] + s.drift[0], s.anchor[1] + s.drift[1])
            for n in range(s.onset + s.closing, s.onset + s.closing + s.held):
                anchors[n] = here
                pinches[n] = (s.finger, s.reach)
            for j in range(s.opening):
                pinches[s.onset + s.closing + s.held + j] = (s.finger, s.reach * (1.0 - (j + 1) / s.opening))
            free = s.onset + s.closing + s.held
        for n in range(free, total):
            anchors[n] = here
        self._pos[side] = here
        return anchors, pinches
