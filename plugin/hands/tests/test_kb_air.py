"""The air-tap detector (DESIGN-KEYBOARD.md 2.12, tests X2 to X25, X44, X52 and X56 to X61).

The two golden fixtures under ``tests/data`` are recorded landmark-derived streams with the exact output of the
reference detector; the port has to reproduce them, so a change to a number or an order in ``press_air.py`` shows
here first. The rest of the file is of three kinds: crafted streams (``Rig``: every lift, position and ratio is a
function of time written in the test, so each gate has the stream that fails without it), the simulated typist of
``keyboard.synth`` through the real ``HandTracker`` (the streams the design's measurements were made on), and the
statistical rows, pooled over many seeds with bounds a few standard errors below the measurement.

Every compared time is a frame's own ``t``: the tests never sleep and never read a clock (X52 reads one on purpose,
and is skipped on a slow machine).
"""

from __future__ import annotations

import ast
import dataclasses
import functools
import json
import math
import multiprocessing
import os
import statistics
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from itertools import pairwise
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from jarvis_hands import synthetic as syn
from jarvis_hands.keyboard import limits
from jarvis_hands.keyboard import synth as sy
from jarvis_hands.keyboard.hands import HandTracker
from jarvis_hands.keyboard.layout import layout_for
from jarvis_hands.keyboard.plane import Plane, place_plane
from jarvis_hands.keyboard.press_air import AirTapPress
from jarvis_hands.keyboard.press_pinch import PinchPress
from jarvis_hands.keyboard.tuning import GROUPS, RANGES, Tuning, parse_tuning
from jarvis_hands.keyboard.types import FingerSample, HandSample, PressEvent, Side
from jarvis_hands.landmarks import Frame

DATA = Path(__file__).parent / "data"
SRC = Path(__file__).parent.parent / "src" / "jarvis_hands" / "keyboard"
DEFAULTS = Tuning()


# --------------------------------------------------------------------------------------------- the golden fixtures


def load(name: str) -> dict:
    with (DATA / name).open() as handle:
        return json.load(handle)


def sample(record: dict) -> HandSample:
    """A recorded frame record as the tracker would have made it (curled false, reach 1.0, palm 0.3: as recorded)."""
    fingers = tuple(
        FingerSample(i, np.array(record["aim"][i]), 1.0, record["ratio"][i], False, record["lift"][i]) for i in range(4)
    )
    return HandSample(
        record["hand"], record["side"], record["t"], 0.3, np.array(record["anchor"]), 0.0, fingers, record["score"]
    )


def replay(fixture: dict, *, trace: bool = True) -> tuple[AirTapPress, list[dict]]:
    """Feed every recorded frame; each event is returned with the theta the detector held when it committed."""
    press = AirTapPress(DEFAULTS, trace=trace)
    for key, depth in fixture["depths"].items():
        side, finger = key.split(".")
        press.set_finger(side, int(finger), depth)
    out = []
    for frame in fixture["frames"]:
        for event in press.update([sample(record) for record in frame]):
            out.append(
                dict(
                    t=event.t,
                    onset_t=event.onset_t,
                    hand=event.hand,
                    side=event.side,
                    finger=event.finger,
                    aim=list(event.aim),
                    ratio=event.ratio,
                    margin=event.margin,
                    depth=event.depth,
                    theta=press.theta(event.hand, event.finger),
                    conf=event.conf,
                )
            )
    return press, out


def check_golden(fixture: dict) -> None:
    press, got = replay(fixture)
    want = fixture["events"]
    assert len(got) == len(want)
    for have, expected in zip(got, want, strict=True):
        for exact in ("hand", "side", "finger"):
            assert have[exact] == expected[exact]
        for time_key in ("t", "onset_t"):
            assert round(have[time_key], 6) == expected[time_key]
        for close in ("ratio", "margin", "depth", "theta", "conf"):
            assert have[close] == pytest.approx(expected[close], abs=1e-6)
        assert have["aim"] == pytest.approx(expected["aim"], abs=1e-6)
    assert press.rejects == fixture["rejects"]


def test_x2_golden_typing_fixture():
    fixture = load("air_golden_typing.json")
    assert len(fixture["frames"]) == 536
    assert len(fixture["events"]) == 27
    check_golden(fixture)
    assert fixture["rejects"] == {"g_speed": 18, "plateau": 5, "gate_speed": 1, "motion": 1}


def test_x3_golden_negative_fixture():
    fixture = load("air_golden_neg.json")
    assert len(fixture["events"]) == 9
    check_golden(fixture)
    assert fixture["rejects"] == {
        "coherence_raw": 5,
        "g_hold": 35,
        "g_speed": 89,
        "gate_hold": 3,
        "motion": 23,
        "plateau": 22,
        "jump": 6,
        "gate_speed": 4,
    }


@pytest.mark.parametrize("name", ["air_golden_typing.json", "air_golden_neg.json"])
def test_x2_x3_tap_log_matches_the_recorded_trace(name):
    """The tap log of 2.12.10 is the reference's, record for record (the fields are numbers rounded as recorded)."""
    fixture = load(name)
    press, _ = replay(fixture)
    got = press.take_trace()
    want = fixture["trace"]
    assert len(got) == len(want)
    for have, expected in zip(got, want, strict=True):
        assert json.loads(json.dumps(have)) == expected


def test_a_tap_log_is_kept_only_when_asked_for():
    press, _ = replay(load("air_golden_typing.json"), trace=False)
    assert press.take_trace() == []


def test_the_tap_log_is_handed_over_once():
    press, _ = replay(load("air_golden_typing.json"))
    assert press.take_trace()
    assert press.take_trace() == []


# ------------------------------------------------------------------------------------------------ crafted streams

#: The lift of each finger at rest; the posture gate wants three of them at 0.35 or more.
REST_LIFT = (0.60, 0.66, 0.62, 0.56)
LEFT: Side = "left"
RIGHT: Side = "right"


def pulse(start: float, depth: float, width: float = 0.2) -> Callable[[float], float]:
    """A dip of ``depth`` (a fraction of the finger's rest lift): a raised cosine that lasts ``width`` seconds."""

    def shape(t: float) -> float:
        u = (t - start) / width
        return depth * 0.5 * (1 - math.cos(2 * math.pi * u)) if 0.0 <= u <= 1.0 else 0.0

    return shape


def step(start: float, depth: float, hold: float, ramp: float = 0.1) -> Callable[[float], float]:
    """A dip that goes down in ``ramp`` seconds, stays down for ``hold`` and comes back in ``ramp``."""

    def shape(t: float) -> float:
        if t < start or t > start + hold + 2 * ramp:
            return 0.0
        return depth * min(1.0, (t - start) / ramp, (start + hold + 2 * ramp - t) / ramp)

    return shape


class Rig:
    """Hands whose every feature is a function of time: the samples the tracker would hand the detector.

    ``dip`` and ``shape`` write a finger's depth, ``path`` the knuckle anchor (the fingertips follow it), ``ratio``
    the thumb-to-tip distance over the palm, ``visible`` when the hand is in view, ``score`` the handedness
    confidence. Noise is Gaussian on the depth of each finger.
    """

    def __init__(
        self,
        press: AirTapPress,
        *,
        fps: float = 30.0,
        sides: Sequence[Side] = (RIGHT,),
        noise: float = 0.0,
        seed: int = 0,
        rest: Sequence[float] = REST_LIFT,
    ) -> None:
        self.press, self.fps, self.sides, self.noise = press, fps, tuple(sides), noise
        self.rng = np.random.default_rng(seed)
        self.rest = {side: tuple(rest) for side in self.sides}
        self.shapes: dict[tuple[Side, int], list[Callable[[float], float]]] = {}
        self.noise_of: dict[tuple[Side, int], float] = {}
        self.tip: dict[tuple[Side, int], Callable[[float], tuple[float, float]]] = {}
        self.path: dict[Side, Callable[[float], tuple[float, float]]] = {s: lambda t: (0.5, 0.4) for s in self.sides}
        self.score: dict[Side, float] = dict.fromkeys(self.sides, 0.9)
        self.ratio: dict[Side, Callable[[float, int], float]] = {s: lambda t, f: 1.5 for s in self.sides}
        self.visible: dict[Side, Callable[[float], bool]] = {s: lambda t: True for s in self.sides}
        self.frames = 0
        self.hands: list[HandSample] = []
        self.events: list[PressEvent] = []

    @property
    def t(self) -> float:
        """The time of the next frame."""
        return self.frames / self.fps

    def dip(self, side: Side, finger: int, start: float, depth: float = 0.4, width: float = 0.2) -> None:
        self.shape(side, finger, pulse(start, depth, width))

    def shape(self, side: Side, finger: int, fn: Callable[[float], float]) -> None:
        self.shapes.setdefault((side, finger), []).append(fn)

    def make(self, t: float) -> list[HandSample]:
        hands = []
        for number, side in enumerate(self.sides, start=1):
            if not self.visible[side](t):
                continue
            ax, ay = self.path[side](t)
            fingers = []
            for f in range(4):
                depth = sum(fn(t) for fn in self.shapes.get((side, f), ()))
                sigma = self.noise_of.get((side, f), self.noise)
                if sigma:
                    depth += sigma * float(self.rng.standard_normal())
                dx, dy = self.tip[side, f](t) if (side, f) in self.tip else (0.0, 0.0)
                aim = np.array([0.30 + 0.05 * f + ax - 0.5 + dx, 0.5 + ay - 0.4 + dy])
                lift = self.rest[side][f] * (1 - depth)
                fingers.append(FingerSample(f, aim, 1.0, self.ratio[side](t, f), False, lift))
            hands.append(HandSample(number, side, t, 0.3, np.array([ax, ay]), 0.0, tuple(fingers), self.score[side]))
        return hands

    def run(self, until: float) -> list[PressEvent]:
        """Feed frames up to (not including) ``until``; returns the events of this call."""
        out = []
        while self.t < until - 1e-9:
            self.hands = self.make(self.t)
            events = self.press.update(self.hands)
            out += events
            self.events += events
            self.frames += 1
        return out

    def view(self, finger: int, side: Side = RIGHT):
        """The overlay view of one finger after the last frame."""
        return next(v for v in self.press.fingers(self.hands) if v.side == side and v.finger == finger)


def armed(
    *, depth: float | None = 0.4, calibrating: bool = False, trace: bool = False, **kw: Any
) -> tuple[AirTapPress, Rig]:
    """A detector (every finger given the warm-up depth ``depth``) and a rig of one right hand on it."""
    press = AirTapPress(DEFAULTS, trace=trace, calibrating=calibrating)
    if depth is not None:
        for side in kw.get("sides", (RIGHT,)):
            for f in range(4):
                press.set_finger(side, f, depth)
    return press, Rig(press, **kw)


def fingers_of(events: Sequence[PressEvent]) -> list[int]:
    return [e.finger for e in events]


def times_of(events: Sequence[PressEvent]) -> list[float]:
    return [round(e.t, 2) for e in events]


# ---------------------------------------------------------------------------------------------------- X4: no clock


def test_x4_the_same_frames_twice_give_identical_events():
    fixture = load("air_golden_typing.json")
    presses = [AirTapPress(DEFAULTS), AirTapPress(DEFAULTS)]
    outs: list[list[PressEvent]] = [[], []]
    for press, out in zip(presses, outs, strict=True):
        for key, depth in fixture["depths"].items():
            side, finger = key.split(".")
            press.set_finger(side, int(finger), depth)
        for frame in fixture["frames"]:
            out += press.update([sample(record) for record in frame])
    assert outs[0] == outs[1]
    assert len(outs[0]) == len(fixture["events"])
    assert presses[0].rejects == presses[1].rejects


def test_x4_press_air_imports_no_clock_no_randomness_no_numpy_no_io():
    tree = ast.parse((SRC / "press_air.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])
    banned = {"time", "random", "numpy", "os", "threading", "datetime", "sys", "pathlib", "json", "logging", "asyncio"}
    assert not imported & banned
    assert imported <= {"__future__", "math", "collections", "dataclasses", "typing"}


def test_x4_the_detector_never_reads_a_clock_it_is_not_given(monkeypatch):
    """Frame time is the only time: with the module clocks broken, the golden stream still gives its events."""

    def refuse(*args, **kwargs):
        raise AssertionError("the detector read a clock")

    monkeypatch.setattr(time, "time", refuse)
    monkeypatch.setattr(time, "monotonic", refuse)
    monkeypatch.setattr(time, "perf_counter", refuse)
    press, events = replay(load("air_golden_typing.json"), trace=False)
    assert len(events) == 27
    assert press.rejects


# ----------------------------------------------------------------------------------- X5, X6, X18: resets and gaps


def test_x5_reset_in_the_middle_of_a_dip_gives_no_event_and_keeps_the_estimates():
    press, rig = armed()
    rig.dip(RIGHT, 1, 1.0)
    rig.dip(RIGHT, 1, 2.2)
    assert rig.run(1.1) == []
    assert rig.view(1).state == "closing"  # the dip is under way when the hold ends
    quality, depth = press.quality(), press.depth_of(RIGHT, 1)
    press.reset()
    assert [v.state for v in press.fingers(rig.hands)] == ["latched"] * 4
    assert [v.fill for v in press.fingers(rig.hands)] == [0.0] * 4
    assert rig.run(2.0) == []  # the rest of that dip is not a tap
    assert press.rejects == {}
    assert [v.state for v in press.fingers(rig.hands)] == ["open"] * 4  # two quiet frames reopened every finger
    after = press.quality()
    assert (after.fps, after.noise, after.gaps) == pytest.approx((quality.fps, quality.noise, quality.gaps), abs=1e-6)
    assert press.depth_of(RIGHT, 1) == depth == 0.4
    events = rig.run(3.5)
    assert fingers_of(events) == [1]
    assert events[0].t == pytest.approx(2.4)


def test_x5_a_finger_that_is_down_when_the_hold_ends_does_not_tap_when_it_comes_up():
    press, rig = armed()
    rig.shape(RIGHT, 2, step(1.0, 0.4, 0.6))
    rig.dip(RIGHT, 2, 3.0)
    rig.run(1.5)
    press.reset()
    assert rig.run(2.8) == []
    assert fingers_of(rig.run(3.8)) == [2]


def test_x6_a_gap_longer_than_a_quarter_second_discards_the_hand():
    press, rig = armed()
    rig.visible[RIGHT] = lambda t: not 2.0 <= t < 2.4
    rig.dip(RIGHT, 1, 1.0)
    rig.dip(RIGHT, 1, 2.5)  # 0.1 s after the hand is back: it is new, and warming up
    rig.dip(RIGHT, 1, 3.5)
    assert times_of(rig.run(5.0)) == [1.2, 3.7]
    assert press.rejects == {"gap_reset": 1}


def test_x6_a_gap_just_under_the_limit_is_not_a_gap():
    press, rig = armed()
    assert limits.GAP_RESET_S == 0.25
    rig.visible[RIGHT] = lambda t: not 2.0 <= t < 2.2  # 0.23 s between frames
    rig.dip(RIGHT, 1, 2.3)
    assert fingers_of(rig.run(3.5)) == [1]
    assert "gap_reset" not in press.rejects


@pytest.mark.parametrize(
    ("jump", "counted"), [(0.059, 0), (0.061, 1), (0.30, 1)], ids=["just_under", "just_over", "far"]
)
def test_x6_a_jump_of_the_anchor_over_the_limit_is_counted_and_restarts_the_hand(jump, counted):
    press, rig = armed()
    assert limits.AIR_JUMP_FW == 0.06
    rig.path[RIGHT] = lambda t: (0.5, 0.4) if t < 2.0 else (0.5 + jump, 0.4)
    rig.dip(RIGHT, 1, 1.0)
    rig.dip(RIGHT, 1, 3.0)
    assert times_of(rig.run(2.15)) == [1.2]
    assert press.rejects.get("jump", 0) == counted
    # a hand that was restarted is warming up again, so its fingers are not ready; one that only moved is
    assert [v.state for v in press.fingers(rig.hands)] == (["latched"] if counted else ["open"]) * 4
    assert times_of(rig.run(4.5)) == [3.2]


def test_x18_a_hand_that_appears_in_the_middle_of_a_tap_gives_no_event():
    _press, rig = armed()
    rig.visible[RIGHT] = lambda t: t >= 1.07
    rig.dip(RIGHT, 1, 1.0)
    rig.dip(RIGHT, 1, 2.2)
    assert times_of(rig.run(3.5)) == [2.4]


def test_x18_a_finger_that_is_mid_tap_when_the_hand_becomes_ready_does_not_tap():
    press, rig = armed()
    rig.dip(RIGHT, 1, 0.25, 0.4, 0.3)  # under way when the warm-up gate lifts at 0.35 s
    rig.dip(RIGHT, 1, 1.5)
    assert rig.run(1.0) == []
    assert press.rejects.get("g_warm", 0) == 0  # warming up is not a reason anyone is told about
    assert times_of(rig.run(2.5)) == [1.7]


def test_x18_a_finger_opens_only_after_two_quiet_frames():
    press, rig = armed()
    rig.shape(RIGHT, 1, lambda t: 0.3 if 1.4 <= t < 1.9 else 0.0)
    rig.run(1.5)  # the dip is under way
    press.reset()
    states = []
    while rig.t < 2.3:
        rig.run(rig.t + 1e-3)
        states.append((round(rig.t, 3), rig.view(1).state))
    opened = next(t for t, state in states if state == "open")
    assert all(state == "latched" for t, state in states if t < opened)
    # raw lift is back at 1.9; the median of three still sees the dip at 1.9 and is quiet from 1.933: two frames
    assert opened == pytest.approx(1.967 + 1 / 30, abs=1e-3)
    assert press.rejects == {}


# ------------------------------------------------------------------------------------------ the simulated hands

HOME: dict[Side, tuple[float, float]] = {LEFT: (0.36, 0.55), RIGHT: (0.64, 0.55)}


def run_sims(
    sims: Mapping[Side, Any],
    press: AirTapPress,
    seconds: float,
    *,
    fps: float = 30.0,
    drop: Callable[[int, float], bool] | None = None,
    ts_jitter: float = 0.0,
    jitter_rng: np.random.Generator | None = None,
    watch: Callable[[list[HandSample]], None] | None = None,
    tracking: Tuning = DEFAULTS,
) -> list[PressEvent]:
    """The frame loop: observe every hand, track it, detect. ``drop(k, t)`` loses that camera frame for all hands;
    ``watch`` sees the tracked hands after each frame the detector has taken; ``tracking`` is the tracker's tuning."""
    tracker = HandTracker(tracking)
    dt = 1.0 / fps
    events: list[PressEvent] = []
    for k in range(int(seconds / dt)):
        t = k * dt
        observed = tuple(sims[side].observe(t, dt) for side in sims)
        if drop is not None and drop(k, t):
            continue
        stamp = t + (float(jitter_rng.normal(0, ts_jitter)) if ts_jitter and jitter_rng is not None else 0.0)
        hands = tracker.update(Frame(stamp, observed, 1280, 720))
        events += press.update(hands)
        if watch is not None:
            watch(hands)
    return events


def tap(side: Side, finger: int, t: float, amp: float = 38.0, dur: float = 0.22, mt: float = 0.2) -> sy.Event:
    return sy.Event(side, finger, float(t), (0.0, 0.0), float(amp), float(dur), mt=mt)


def typists(
    events: dict[Side, list[sy.Event]], rng: np.random.Generator, *, noise: sy.Noise | None = None, **kw: Any
) -> dict[Side, sy.AirTypist]:
    noise = noise or sy.Noise(sigma=0.001, glitch_p=0.003, amp_gain=(1.0, 1.0))
    return {side: sy.AirTypist(side, HOME[side], events.get(side, []), rng, noise, **kw) for side in (LEFT, RIGHT)}


def warmed(depth: float = 0.40, tuning: Tuning = DEFAULTS) -> AirTapPress:
    press = AirTapPress(tuning)
    for side in (LEFT, RIGHT):
        for f in range(4):
            press.set_finger(side, f, depth)
    return press


# ------------------------------------------------------------------------- X9, X10, X11, X17: fingers against fingers


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_x9_a_tap_with_both_neighbours_at_a_third_of_its_depth_is_one_event(seed):
    events = [tap(RIGHT, 1 + k % 2, 4.0 + k) for k in range(20)]
    sims = typists({RIGHT: events}, np.random.default_rng(seed), coupling_p=1.0, coupling_share=(0.3, 0.3))
    got = run_sims(sims, warmed(), 25.0)
    assert fingers_of(got) == [1 + k % 2 for k in range(20)]  # the tapper each time, nobody else


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_x10_two_fingers_at_depths_one_to_a_half_give_the_deeper_one(seed):
    events = []
    for k in range(10):
        events += [tap(RIGHT, 1, 4.0 + 1.5 * k, amp=40.0, dur=0.2), tap(RIGHT, 2, 4.05 + 1.5 * k, amp=20.0, dur=0.2)]
    press = warmed()
    got = run_sims(typists({RIGHT: events}, np.random.default_rng(seed), coupling_p=0.0), press, 20.0)
    assert fingers_of(got) == [1] * 10
    assert press.rejects["veto"] >= 8  # the other finger is counted, not dropped quietly


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_x10_two_fingers_at_nearly_equal_depths_never_type_within_the_hand_exclusion(seed):
    events = []
    for k in range(10):
        events += [tap(RIGHT, 1, 4.0 + 1.5 * k, amp=40.0, dur=0.2), tap(RIGHT, 2, 4.05 + 1.5 * k, amp=38.0, dur=0.2)]
    press = warmed()
    got = run_sims(typists({RIGHT: events}, np.random.default_rng(seed), coupling_p=0.0), press, 20.0)
    assert [b.t - a.t >= limits.AIR_HAND_EXCL_S - 1e-9 for a, b in pairwise(got)] == [True] * (len(got) - 1)
    for chord in range(10):
        mine = [e for e in got if 4.0 + 1.5 * chord <= e.t - 0.1 < 4.0 + 1.5 * chord + 1.0]
        assert mine, "a chord produced nothing at all"
        assert len(mine) <= 1 or mine[1].t - mine[0].t <= 0.16 + 1e-9  # the other one is typed soon, or it was vetoed


def chord_run(delay: float, fps: float, seed: int = 1) -> tuple[list[PressEvent], dict[str, int]]:
    events = []
    for k in range(10):
        events += [tap(RIGHT, 0, 4.0 + 1.5 * k, 40.0, 0.2), tap(RIGHT, 1, 4.0 + 1.5 * k + delay, 40.0, 0.2)]
    press = warmed()
    sims = typists({RIGHT: events}, np.random.default_rng(seed), coupling_p=0.0, lead_s=0.08)
    return run_sims(sims, press, 20.0, fps=fps), press.rejects


@pytest.mark.parametrize("fps", [30.0, 60.0])
@pytest.mark.parametrize("delay", [0.0, 0.017, 0.033, 0.050])
def test_x10_chords_every_tap_has_an_event_or_a_counted_reason(delay, fps):
    got, rejects = chord_run(delay, fps)
    assert len(got) + sum(rejects.get(name, 0) for name in ("excl", "veto", "coherence_raw")) == 20
    assert all(b.t - a.t >= limits.AIR_HAND_EXCL_S - 1e-9 for a, b in pairwise(got))


@pytest.mark.parametrize("fps", [30.0, 60.0])
@pytest.mark.parametrize("delay", [0.0, 0.017])
def test_x10_chords_that_are_simultaneous_give_exactly_one_event_each(delay, fps):
    got, rejects = chord_run(delay, fps)
    assert len(got) == 10
    assert rejects.get("excl", 0) + rejects.get("veto", 0) == 10


def test_x10_chords_far_enough_apart_in_time_are_both_typed():
    got, _ = chord_run(0.050, 60.0)
    assert len(got) == 20
    assert min(b.t - a.t for a, b in pairwise(got)) >= limits.AIR_HAND_EXCL_S - 1e-9


def test_x11_a_hand_closing_to_a_fist_and_opening_gives_nothing():
    rng = np.random.default_rng(0)
    noise = sy.Noise(sigma=0.001)
    sims = {s: sy.scenario("open_close_fast", s, rng, noise, duration=14.0) for s in (LEFT, RIGHT)}
    press = warmed()
    assert run_sims(sims, press, 12.0) == []
    assert press.rejects.get("coherence_raw", 0) + press.rejects.get("coherence", 0) > 0
    assert press.rejects.get("g_hold", 0) > 0  # and the hand was held while it happened


def test_x11_a_coherent_move_of_three_fingers_holds_the_hand_for_a_third_of_a_second():
    press, rig = armed(trace=True)
    for finger, (start, depth) in enumerate([(1.0, 0.30), (1.03, 0.35), (1.06, 0.30)]):
        rig.dip(RIGHT, finger, start, depth, 0.25)
    rig.dip(RIGHT, 3, 1.4)  # alone, but the hold of the three has not run out
    rig.dip(RIGHT, 3, 2.5)
    assert times_of(rig.run(4.0)) == [2.7]
    assert press.rejects.get("coherence_raw", 0) + press.rejects.get("coherence", 0) >= 1
    log = [r for r in press.take_trace() if r["k"] == "gate"]
    assert log
    assert all(r["dur"] == limits.AIR_COHERENCE_HOLD_S for r in log)
    assert {r["gate"] for r in log} <= {"coherence", "coherence_raw"}


def test_x17_five_commits_of_a_hand_inside_half_a_second_are_discarded_and_hold_the_hand():
    events = []
    t = 4.0
    for k in range(14):
        events.append(tap(RIGHT, (1, 3)[k % 2], t, amp=42.0, dur=0.20, mt=0.1))
        t += 0.07
    press = warmed()
    got = run_sims(typists({RIGHT: events}, np.random.default_rng(0), coupling_p=0.0), press, t + 3.0)
    assert press.rejects.get("tremor", 0) >= 1
    ts = [e.t for e in got]
    assert max(sum(1 for x in ts if a <= x < a + 0.5) for a in ts) <= 4


@pytest.mark.parametrize("fps", [30.0, 60.0])
@pytest.mark.parametrize("spacing", [0.07, 0.09, 0.11])
def test_x17_a_fast_trill_of_two_fingers_trips_the_guard_in_every_trill(spacing, fps):
    tremors = 0
    most = 0
    for seed in range(5):
        events = []
        t = 4.0
        for _ in range(3):
            for k in range(14):
                events.append(tap(RIGHT, (1, 3)[k % 2], t, amp=42.0, dur=0.20, mt=0.1))
                t += spacing
            t += 2.0
        press = warmed()
        got = run_sims(typists({RIGHT: events}, np.random.default_rng(seed), coupling_p=0.0), press, t + 1.0, fps=fps)
        ts = [e.t for e in got]
        most = max([most] + [sum(1 for x in ts if a <= x < a + 0.5) for a in ts])
        tremors += press.rejects.get("tremor", 0)
    assert tremors >= 13  # of 15 trills (the design measured 15 in all six cells)
    assert most <= 4


def test_x17_a_roll_of_four_fingers_never_trips_the_guard():
    events = []
    t = 4.0
    for _ in range(5):
        for finger in range(4):
            events.append(tap(RIGHT, finger, t, amp=40.0, dur=0.2, mt=0.1))
            t += 0.12
        t += 1.5
    press = warmed()
    got = run_sims(typists({RIGHT: events}, np.random.default_rng(1), coupling_p=0.0), press, t + 1.0)
    assert "tremor" not in press.rejects
    ts = [e.t for e in got]
    assert max(sum(1 for x in ts if a <= x < a + 0.5) for a in ts) <= 4


# ------------------------------------------------------------------------------------ X12, X13, X14: the hand gates


def smoothstep(u: float) -> float:
    u = min(1.0, max(0.0, u))
    return u * u * (3 - 2 * u)


def test_x12_taps_while_the_hand_moves_are_rejected_and_the_one_after_it_stops_commits():
    press, rig = armed()
    rig.path[RIGHT] = lambda t: (0.2 + 0.6 * min(max(t - 2.0, 0.0), 1.0), 0.4)  # 0.6 fw/s for one second
    rig.dip(RIGHT, 1, 2.4)
    rig.dip(RIGHT, 1, 3.3)  # 0.3 s after the hand has stopped
    assert times_of(rig.run(5.0)) == [3.5]
    assert press.rejects["g_speed"] > 0  # counted once per frame the gate was closed
    assert press.rejects["gate_speed"] >= 1  # and once per tap it stopped


def test_x12_a_hand_that_moves_slower_than_the_gate_still_types():
    press, rig = armed()
    rig.path[RIGHT] = lambda t: (0.2 + 0.4 * min(max(t - 2.0, 0.0), 1.0), 0.4)  # 0.4 fw/s
    rig.dip(RIGHT, 1, 2.4)
    assert times_of(rig.run(4.0)) == [2.6]
    assert "g_speed" not in press.rejects


@pytest.mark.parametrize("start", [4.95, 5.0, 5.1, 5.2])
def test_x12_a_tap_that_starts_as_the_hand_speeds_up_is_rejected(start):
    """The hand jerks 0.12 fw in 0.15 s at 5.0 (1.2 fw/s at the fastest): the tap is not the finger's."""
    press, rig = armed()
    rig.path[RIGHT] = lambda t: (0.5 + 0.12 * smoothstep((t - 5.0) / 0.15), 0.4)
    rig.dip(RIGHT, 1, start)
    assert rig.run(7.0) == []
    assert press.rejects.get("motion", 0) + press.rejects.get("gate_speed", 0) >= 1


@pytest.mark.parametrize("start", [5.0, 5.1])
def test_x12_the_speed_in_the_window_around_the_onset_is_counted_motion(start):
    press, rig = armed()
    rig.path[RIGHT] = lambda t: (0.5 + 0.12 * smoothstep((t - 5.0) / 0.15), 0.4)
    rig.dip(RIGHT, 1, start)
    rig.run(7.0)
    assert press.rejects["motion"] >= 1


@pytest.mark.parametrize(("start", "commit"), [(4.85, 5.07), (5.3, 5.5)])
def test_x12_the_same_tap_a_moment_before_or_after_the_jerk_is_typed(start, commit):
    _press, rig = armed()
    rig.path[RIGHT] = lambda t: (0.5 + 0.12 * smoothstep((t - 5.0) / 0.15), 0.4)
    rig.dip(RIGHT, 1, start)
    assert times_of(rig.run(7.0)) == [commit]


RELAXED_LIFT = (0.144, 0.170, 0.151, 0.108)  # the lift of the RELAXED posture (X1 pins these)


def test_x13_a_relaxed_hand_is_gated_posture_and_says_so_after_half_a_second():
    press, rig = armed(rest=RELAXED_LIFT)
    for finger in (1, 2):
        rig.dip(RIGHT, finger, 1.5 + finger)
    notes = {}
    while rig.t < 4.5:
        rig.run(rig.t + 1e-3)
        notes[round(rig.t, 3)] = (press.gate_of(1), rig.view(1).note, rig.view(1).state)
    assert rig.events == []
    assert press.rejects["g_posture"] > 100
    assert {gate for t, (gate, _, _) in notes.items() if t > 0.5} == {"posture"}
    assert {state for _, _, state in notes.values()} == {"latched"}
    first = min(t for t, (_, note, _) in notes.items() if note == "posture")
    gate_closed = min(t for t, (gate, _, _) in notes.items() if gate == "posture")
    assert first - gate_closed == pytest.approx(0.5, abs=1 / 30 + 1e-6)  # never earlier, so it cannot flicker
    assert all(note == "posture" for t, (_, note, _) in notes.items() if t > first)


@pytest.mark.parametrize("name", ["REST", "STRAIGHT"])
def test_x13_the_rest_and_straight_postures_are_not_gated(name):
    lift = {"REST": REST_LIFT, "STRAIGHT": (0.90, 0.95, 0.92, 0.85)}[name]
    press, rig = armed(rest=lift)
    rig.dip(RIGHT, 1, 2.0)
    assert fingers_of(rig.run(3.0)) == [1]
    assert press.gate_of(1) == ""
    assert "g_posture" not in press.rejects


def test_x13_the_gate_opens_again_when_the_fingers_are_raised():
    press, rig = armed()
    rig.rest[RIGHT] = RELAXED_LIFT
    rig.run(2.0)
    assert press.gate_of(1) == "posture"
    rig.rest[RIGHT] = REST_LIFT  # the user lifts the fingers: the rest scale follows within the quantile window
    rig.dip(RIGHT, 1, 4.5)
    assert times_of(rig.run(6.0)) == [4.7]
    assert press.gate_of(1) == ""


@pytest.mark.parametrize("ratio", [0.20, 0.29, 0.31, 0.35])
def test_x14_a_finger_dipping_with_the_thumb_on_its_tip_is_rejected_pinched(ratio):
    """AIR_PINCH_GAP is 0.30 palms: a finger that dips to touch the thumb is a pinch, not a tap."""
    press, rig = armed()
    assert limits.AIR_PINCH_GAP == 0.30
    rig.ratio[RIGHT] = lambda t, f: ratio if (f == 1 and 2.0 <= t < 2.6) else 1.5
    rig.dip(RIGHT, 1, 2.0)
    rig.dip(RIGHT, 1, 3.0)  # the same dip with the thumb away
    events = rig.run(5.0)
    if ratio < 0.30:
        assert times_of(events) == [3.2]
        assert press.rejects["pinched"] >= 1
    else:
        assert times_of(events) == [2.2, 3.2]
        assert "pinched" not in press.rejects


# --------------------------------------------------------------------------------- X15, X16: shapes that are not taps


def held(start: float, depth: float, hold: float, sag: float = 0.0, ramp: float = 0.1) -> Callable[[float], float]:
    """A dip that goes down in ``ramp`` seconds, stays for ``hold`` while it sags a little, comes back in ``ramp``."""

    def shape(t: float) -> float:
        if t < start or t > start + hold + 2 * ramp:
            return 0.0
        slope = min(1.0, (t - start) / ramp, (start + hold + 2 * ramp - t) / ramp)
        return depth * slope - sag * min(1.0, max(0.0, (t - start - ramp) / hold))

    return shape


def test_x15_a_finger_that_dips_and_stays_down_is_a_plateau_not_a_tap():
    press, rig = armed()
    rig.shape(RIGHT, 1, held(2.0, 0.4, 1.0, sag=0.04))
    rig.dip(RIGHT, 1, 4.5)
    assert rig.run(4.4) == []  # not while it is down, and not when it comes back up at 3.2
    assert press.rejects["plateau"] >= 1
    assert times_of(rig.run(6.0)) == [4.7]  # the next tap commits: nothing was left latched or half-counted
    assert fingers_of(rig.events) == [1]


@pytest.mark.parametrize("seed", range(6))
def test_x15_noise_on_a_flat_plateau_does_not_make_a_tap_at_the_top_or_at_the_release(seed):
    _press, rig = armed(noise=0.004, seed=seed)
    rig.shape(RIGHT, 1, step(2.0, 0.4, 1.0))
    rig.dip(RIGHT, 1, 4.5)
    assert rig.run(4.4) == []
    assert times_of(rig.run(6.0)) == [4.7]


@pytest.mark.parametrize("fps", [24.0, 30.0, 60.0])
def test_x16_a_one_frame_spike_is_removed_by_the_median_of_the_smoothing(fps):
    press, rig = armed(fps=fps)
    when = round(4.0 * fps) / fps
    rig.shape(RIGHT, 1, lambda t: 0.5 if abs(t - when) < 0.5 / fps else 0.0)
    assert rig.run(6.0) == []
    assert press.rejects == {}  # not even a candidate


@pytest.mark.parametrize("fps", [15.0, 19.0])
def test_x16_below_twenty_fps_nothing_is_smoothed_and_a_single_frame_is_a_short_tap(fps):
    _press, rig = armed(fps=fps)
    when = round(4.0 * fps) / fps
    rig.shape(RIGHT, 1, lambda t: 0.5 if abs(t - when) < 0.5 / fps else 0.0)
    assert fingers_of(rig.run(6.0)) == [1]


def test_x16_the_narrow_gate_rejects_it_when_the_minimum_width_is_raised():
    """A frame at 15 fps lasts 0.067 s; the tuning's largest minimum width, 0.08 s, calls that narrow."""
    tuning = dataclasses.replace(DEFAULTS, air_width_min_s=0.08)
    press = AirTapPress(tuning)
    press.set_finger(RIGHT, 1, 0.4)
    rig = Rig(press, fps=15.0)
    when = round(4.0 * 15.0) / 15.0
    rig.shape(RIGHT, 1, lambda t: 0.5 if abs(t - when) < 0.5 / 15.0 else 0.0)
    assert rig.run(6.0) == []
    assert press.rejects == {"narrow": 1}


@pytest.mark.parametrize(("width", "typed"), [(0.04, False), (0.05, False), (0.08, True)])
def test_x16_a_pulse_shorter_than_the_minimum_width_is_narrow_at_a_high_frame_rate(width, typed):
    press, rig = armed(fps=90.0)
    rig.dip(RIGHT, 1, 4.0, 0.4, width)
    events = rig.run(6.0)
    assert bool(events) == typed
    assert press.rejects.get("narrow", 0) == (0 if typed else 1)


@pytest.mark.parametrize("fps", [15.0, 30.0, 60.0])
@pytest.mark.parametrize("depth", [0.3, 0.4])
def test_x16_a_slow_dip_a_second_long_is_not_a_tap(fps, depth):
    """The rise is read over 0.16 s: a movement that slow never rises by a threshold in that window."""
    _press, rig = armed(fps=fps)
    rig.dip(RIGHT, 1, 3.0, depth, 1.0)
    assert rig.run(5.0) == []


WIDE = dataclasses.replace(
    DEFAULTS, air_width_max_s=0.20, air_back_s=0.30, air_rise_win_s=0.25, air_fall_win_s=0.20
)  # the windows at their clamps: the longest a stroke can look, against the shortest width the tuning accepts


@pytest.mark.parametrize("fps", [30.0, 60.0])
@pytest.mark.parametrize("width", [0.4, 0.5])
def test_x16_a_broad_dip_is_wide_when_the_windows_are_long_enough_to_see_its_width(fps, width):
    press = AirTapPress(WIDE, trace=False)
    press.set_finger(RIGHT, 1, 0.5)
    rig = Rig(press, fps=fps)
    rig.dip(RIGHT, 1, 4.0, 0.5, width)
    assert rig.run(6.0) == []
    assert press.rejects.get("wide", 0) >= 1


@pytest.mark.parametrize("fps", [15.0, 30.0, 60.0])
def test_x16_with_the_default_windows_the_steep_part_of_a_half_second_dip_is_a_tap(fps):
    """Where this differs from the design's X16 sentence: the detector reads a stroke through 0.16 s and 0.12 s
    windows, so the width it can measure never reaches 0.30 s and the wide gate is the tuning's to tighten."""
    press, rig = armed(fps=fps, depth=0.5)
    rig.dip(RIGHT, 1, 4.0, 0.5, 0.5)
    assert fingers_of(rig.run(6.0)) == [1]
    assert "wide" not in press.rejects


# ------------------------------------------------------------------------------- X19, X22: what quality() reports


def test_x22_nothing_is_reported_before_anything_was_measured():
    quality = AirTapPress(DEFAULTS).quality()
    assert (quality.fps, quality.noise, quality.gaps) == (0.0, None, 0)


@pytest.mark.parametrize("fps", [15.0, 24.0, 30.0, 60.0])
def test_x22_the_frame_rate_is_the_injected_one_within_five_percent(fps):
    press, rig = armed(fps=fps)
    rig.run(20.0)  # the tracked rate is a slow average that starts at 30
    assert press.quality().fps == pytest.approx(fps, rel=0.05)


def test_x19_the_noise_estimate_follows_a_change_of_noise_within_three_seconds():
    """The estimate is the median of the last 3 s of quiet differences; after the change it is a mix, then the new."""
    steady, rig_steady = armed(noise=0.03, seed=3)
    changing, rig = armed(noise=0.01, seed=3)
    rig_steady.run(20.0)
    rig.run(10.0)
    before = changing.quality().noise
    rig.noise = 0.03
    rig.run(11.0)
    soon = changing.quality().noise
    rig.run(13.2)
    after = changing.quality().noise
    target = steady.quality().noise
    assert before < 0.6 * target
    assert soon < after  # still on its way a second after the change
    assert after == pytest.approx(target, rel=0.25)


def estimate_with_taps(rate: float, seed: int, noise: float = 0.002) -> float:
    """The noise estimate after forty seconds of both hands tapping ``rate`` times a second each, fingers in turn."""
    events: dict[Side, list[sy.Event]] = {LEFT: [], RIGHT: []}
    if rate:
        for side in (LEFT, RIGHT):
            t, k = 3.0 + (0.1 if side == LEFT else 0.0), 0
            while t < 39.0:
                events[side].append(tap(side, k % 4, t, amp=38.0, dur=0.2, mt=0.1))
                k, t = k + 1, t + 1.0 / rate
    sims = typists(events, np.random.default_rng(seed), noise=sy.Noise(sigma=noise, glitch_p=0.003), coupling_p=0.0)
    press = warmed()
    run_sims(sims, press, 40.0)
    quality = press.quality().noise
    assert quality is not None
    return quality


def test_x19_taps_inside_the_stream_hardly_move_the_median_estimate():
    """Four taps a second over the two hands: a mean-based estimate rises about 1.95 times (measured with the
    estimator swapped), the median one by a sixth on average (seeds spread from 0.9 to 1.4: the bound is the mean's)."""
    ratios = [estimate_with_taps(2.0, seed) / estimate_with_taps(0.0, seed) for seed in range(8)]
    assert np.mean(ratios) <= 1.35
    assert max(ratios) <= 1.8


# ------------------------------------------------------------------- X23, X24, X25: thresholds, levels, the tuning


def test_x23_degraded_multiplies_every_threshold_and_ok_restores_them():
    press, rig = armed()
    rig.run(2.0)
    base = [press.theta(1, f) for f in range(4)]
    assert limits.AIR_THETA_MULT_DEGRADED == 1.3
    press.set_level("degraded")
    rig.run(2.2)
    assert [press.theta(1, f) for f in range(4)] == pytest.approx([b * 1.3 for b in base])
    press.set_level("ok")
    rig.run(2.4)
    assert [press.theta(1, f) for f in range(4)] == pytest.approx(base)


@pytest.mark.parametrize(("level", "typed"), [("ok", True), ("degraded", False)])
def test_x23_a_tap_between_the_two_thresholds_is_typed_only_at_the_ok_level(level, typed):
    press, rig = armed()
    press.set_level(level)
    # The median of three leaves 0.75 of the top of a pulse this short: 0.1125 is above this finger's 0.10, below 0.13.
    rig.dip(RIGHT, 1, 2.0, 0.15, 0.2)
    assert bool(rig.run(3.5)) == typed


def test_x23_a_tap_in_progress_keeps_the_threshold_it_began_with():
    press, rig = armed()
    rig.dip(RIGHT, 1, 2.0, 0.15, 0.2)
    rig.dip(RIGHT, 1, 3.0, 0.15, 0.2)
    rig.run(1.0)
    while rig.view(1).state != "closing":
        rig.run(rig.t + 1e-3)
    assert rig.t < 2.3
    theta = press.theta(1, 1)
    press.set_level("degraded")
    rig.run(rig.t + 1e-3)
    assert press.theta(1, 1) == theta  # the stroke is not judged by a new number half way through
    assert times_of(rig.run(2.8)) == [2.2]  # and it is typed
    rig.run(2.9)
    assert press.theta(1, 1) == pytest.approx(theta * 1.3)  # the next peak meets the new one
    assert rig.run(4.0) == []


@pytest.mark.parametrize("given", [0.05, 0.10, 0.45, 0.80, 3.0, -1.0, 0.0])
def test_x24_the_warm_up_depth_is_held_between_a_tenth_and_eight_tenths(given):
    press = AirTapPress(DEFAULTS)
    press.set_finger(RIGHT, 2, given)
    assert press.depth_of(RIGHT, 2) == min(0.80, max(0.10, given))


@pytest.mark.parametrize("given", [math.nan, math.inf, -math.inf])
def test_x24_a_depth_that_is_not_a_number_is_refused(given):
    with pytest.raises(ValueError, match="finite"):
        AirTapPress(DEFAULTS).set_finger(RIGHT, 2, given)


def test_x24_the_second_threshold_is_ignored_and_may_be_left_out():
    one, two = AirTapPress(DEFAULTS), AirTapPress(DEFAULTS)
    one.set_finger(RIGHT, 0, 0.3)
    two.set_finger(RIGHT, 0, 0.3, 0.9)
    assert one.depth_of(RIGHT, 0) == two.depth_of(RIGHT, 0) == 0.3


def test_x24_the_pinch_method_needs_both_thresholds():
    press = PinchPress(DEFAULTS)
    with pytest.raises(ValueError, match="both"):
        press.set_finger(RIGHT, 0, 0.3)
    press.set_finger(RIGHT, 0, 0.3, 0.5)


def expected_theta(sigma: float, depth: float) -> tuple[float, float, float]:
    """(calibrated threshold, nominal, lower bound) of 2.12.2 S6 for a noise estimate and a warm-up depth."""
    nominal = min(0.25, max(0.10, 5.0 * sigma))
    lo = max(0.07, 4.0 * sigma, 0.75 * nominal)
    return max(lo, min(nominal, 0.5 * min(0.80, max(0.10, depth)))), nominal, lo


@pytest.mark.parametrize("noise", [0.0, 0.01, 0.03])
@pytest.mark.parametrize("depth", [0.10, 0.16, 0.20, 0.30, 0.80])
def test_x24_the_calibrated_threshold_is_half_the_depth_held_between_three_quarters_of_nominal_and_nominal(
    noise, depth
):
    press, rig = armed(depth=depth, noise=noise)
    rig.run(8.0)
    sigma = press.sigma(1, 2)
    assert sigma is not None
    theta, nominal, lo = expected_theta(sigma, depth)
    assert press.theta(1, 2) == pytest.approx(theta)
    assert press.theta(1, 2) >= 0.75 * nominal - 1e-12  # a lazy warm-up tap cannot make the finger too sensitive
    assert press.theta(1, 2) <= max(nominal, lo) + 1e-12  # and a deep one cannot make it less sensitive than nominal


def test_x24_without_a_warm_up_depth_the_threshold_is_the_nominal_one_and_while_calibrating_the_sensitive_one():
    nominal, rig = armed(depth=None)
    rig.run(4.0)
    assert nominal.theta(1, 0) == pytest.approx(0.10)
    calibrating, rig = armed(depth=None, calibrating=True)
    rig.run(4.0)
    sigma = calibrating.sigma(1, 0)
    assert sigma is not None
    assert calibrating.theta(1, 0) == pytest.approx(max(0.07, 4.0 * sigma))
    calibrating.set_calibrating(False)
    rig.run(4.2)
    assert calibrating.theta(1, 0) == pytest.approx(0.10)


AIR_FIELDS = {name for name in GROUPS["air"]}


def test_x25_a_bad_air_value_falls_back_for_that_field_only():
    good = {"air_theta_k": 6.0, "air_back_s": 0.25, "air_aim": "onset"}
    bad = {
        "air_theta_min": 0.05,  # under its floor
        "air_veto_ratio": 0.3,  # under its floor
        "air_width_max_s": 0.9,  # over its clamp
        "air_speed_gate": math.nan,
        "air_vmax_gate": math.inf,
        "air_return_frac": "0.5",
        "air_depth_frac": True,
        "air_rise_win_s": None,
        "air_fall_win_s": [0.1],
        "air_theta_max": {"x": 1},
    }
    tuning = parse_tuning({"version": 1, "air": {**good, **bad}})
    defaults = Tuning()
    assert (tuning.air_theta_k, tuning.air_back_s, tuning.air_aim) == (6.0, 0.25, "onset")
    for name in bad:
        assert getattr(tuning, name) == getattr(defaults, name), name


def test_x25_an_aim_rule_the_detector_does_not_know_falls_back_and_an_integer_is_a_number():
    assert parse_tuning({"air": {"air_aim": "dwell"}}).air_aim == Tuning().air_aim
    assert parse_tuning({"air": {"air_aim": 3}}).air_aim == Tuning().air_aim
    assert parse_tuning({"air": {"air_back_s": 1}}).air_back_s == Tuning().air_back_s  # a number, but out of its clamp
    assert parse_tuning({"air": {"air_speed_gate": 1}}).air_speed_gate == 1.0  # a number inside it


def test_x25_unknown_keys_and_air_keys_in_another_group_are_ignored():
    tuning = parse_tuning(
        {
            "version": 1,
            "air": {"air_nonsense": 5, "air_min_score": 0.0, "air_theta_k": 5.5},
            "pinch": {"air_theta_k": 7.0},
        }
    )
    assert tuning == dataclasses.replace(Tuning(), air_theta_k=5.5)


def test_x25_no_field_of_the_file_names_a_safety_constant():
    constants = {name.lower() for name in dir(limits) if name.startswith("AIR_")}
    assert not constants & {field.name for field in dataclasses.fields(Tuning)}
    assert not constants & set(RANGES)


def test_x25_a_tuning_built_in_code_below_the_floors_is_held_at_them_by_the_detector():
    """The loader never makes one, but code can: the detector applies the floors itself."""
    low = dataclasses.replace(Tuning(), air_theta_k=0.5, air_theta_min=0.0, air_veto_ratio=0.1)
    floored = dataclasses.replace(
        Tuning(), air_theta_k=limits.AIR_THETA_K_FLOOR, air_theta_min=limits.AIR_THETA_FLOOR, air_veto_ratio=0.5
    )
    results = []
    for tuning in (low, floored):
        press = AirTapPress(tuning)
        for f in range(4):
            press.set_finger(RIGHT, f, 0.8)
        rig = Rig(press, noise=0.02, seed=5)
        for k in range(10):
            rig.dip(RIGHT, 1 + k % 2, 2.0 + 1.2 * k, 0.12, 0.2)
            rig.dip(RIGHT, 2 - k % 2, 2.05 + 1.2 * k, 0.07, 0.2)
        events = rig.run(16.0)
        results.append(([(e.t, e.finger) for e in events], dict(press.rejects), press.theta(1, 1)))
    assert results[0] == results[1]
    assert results[0][2] >= limits.AIR_THETA_FLOOR


# ----------------------------------------------------------------------------------- X44: what the overlay is shown


def frames_of(rig: Rig, until: float, finger: int = 1) -> list[tuple[float, Any]]:
    """The view of ``finger`` after each frame up to ``until``."""
    out = []
    while rig.t < until:
        rig.run(rig.t + 1e-3)
        out.append((rig.t, rig.view(finger)))
    return out


def test_x44_a_finger_goes_latched_open_closing_pressed_open():
    _press, rig = armed()
    rig.dip(RIGHT, 1, 2.0, 0.4, 0.25)
    states = [view.state for _, view in frames_of(rig, 3.0)]
    order = [s for k, s in enumerate(states) if k == 0 or s != states[k - 1]]
    assert order == ["latched", "open", "closing", "pressed", "open"]


def test_x44_pressed_is_shown_for_the_flash_time():
    _press, rig = armed()
    rig.dip(RIGHT, 1, 2.0, 0.4, 0.25)
    seen = [t for t, view in frames_of(rig, 3.0) if view.state == "pressed"]
    assert limits.AIR_FLASH_S == 0.18
    assert (seen[-1] - seen[0]) + 1 / 30 == pytest.approx(0.18, abs=1 / 30 + 1e-6)


def test_x44_the_aim_stays_where_the_tip_was_at_the_left_base_while_the_tip_travels_to_its_key():
    _press, rig = armed()
    rig.tip[RIGHT, 1] = lambda t: (0.04 * smoothstep((t - 2.0) / 0.25), 0.02 * smoothstep((t - 2.0) / 0.25))
    rig.dip(RIGHT, 1, 2.0, 0.4, 0.25)
    rows = frames_of(rig, 3.0)
    base = np.array([0.35, 0.5])  # the tip at the start of the stroke
    closing = [(t, view) for t, view in rows if view.state == "closing"]
    assert closing
    for _, view in closing:
        assert np.allclose(view.aim, base, atol=0.003)
    assert all(np.allclose(view.aim, closing[0][1].aim) for _, view in closing)
    live = np.array([0.39, 0.52])  # where it is by the end of the stroke
    assert np.allclose([float(x) for x in rig.hands[0].fingers[1].aim], live, atol=0.01)
    pressed = [view for _, view in rows if view.state == "pressed"]
    assert pressed
    assert all(np.allclose(view.aim, rig.events[0].aim) for view in pressed)
    open_after = [view for t, view in rows if view.state == "open" and t > 2.4]
    assert np.allclose(open_after[-1].aim, live, atol=0.01)  # an open finger shows where it is


def test_x44_while_the_hand_is_still_moving_the_provisional_aim_follows_the_live_tip():
    _press, rig = armed()
    rig.path[RIGHT] = lambda t: (0.3 + 0.3 * (t - 1.0), 0.4)  # 0.3 fw/s: under the speed gate, over the aim speed
    rig.dip(RIGHT, 1, 2.0, 0.4, 0.25)
    closing = [view for _, view in frames_of(rig, 3.0) if view.state == "closing"]
    assert len(closing) >= 2
    assert len({round(float(view.aim[0]), 4) for view in closing}) == len(closing)


def test_x44_the_ring_fills_with_the_rise_and_empties_after_the_stroke():
    _press, rig = armed()
    rig.dip(RIGHT, 1, 2.0, 0.3, 0.5)
    fills = [(t, view.fill) for t, view in frames_of(rig, 3.2)]
    before = [f for t, f in fills if t < 2.0]
    assert before and set(before) == {0.0}
    rising = [f for t, f in fills if 2.0 <= t <= 2.25]
    assert rising == sorted(rising)
    assert max(rising) == 1.0
    assert len({f for f in rising if 0.0 < f < 1.0}) >= 2  # a ring that fills, not one that blinks
    assert all(0.0 <= f <= 1.0 for _, f in fills)
    assert [f for t, f in fills if t > 2.8] == [0.0] * len([1 for t, _ in fills if t > 2.8])


def test_x44_a_closed_hand_gate_reads_latched_and_cannot_be_pressed():
    press, rig = armed()
    rig.path[RIGHT] = lambda t: (0.2 + 0.7 * (t - 1.0), 0.4) if 1.0 <= t < 2.0 else (0.2 if t < 1.0 else 0.9, 0.4)
    rig.run(1.6)
    assert press.gate_of(1) == "speed"
    assert {view.state for view in press.fingers(rig.hands)} == {"latched"}
    assert {view.fill for view in press.fingers(rig.hands)} == {0.0}


def test_x44_a_hand_the_detector_has_not_seen_is_all_latched():
    rig = Rig(AirTapPress(DEFAULTS))
    views = rig.press.fingers(rig.make(0.0))
    assert [(v.finger, v.state, v.fill, v.note) for v in views] == [(f, "latched", 0.0, "") for f in range(4)]
    assert rig.press.gate_of(1) is None


def test_x44_a_weak_finger_is_marked_weak_and_the_others_are_not():
    press, rig = armed()
    press.set_finger(RIGHT, 2, 0.10)
    rig.run(1.0)
    assert [rig.view(f).note for f in range(4)] == ["", "", "weak", ""]


def test_x44_a_finger_much_noisier_than_the_hand_is_marked_noisy_once_it_has_been_measured():
    _press, rig = armed(noise=0.004)
    rig.noise_of[RIGHT, 0] = 0.03
    notes = [(t, view.note) for t, view in frames_of(rig, 6.0, finger=0)]
    first = next(t for t, note in notes if note == "noisy")
    assert 0.3 < first < 1.5  # not before a dozen quiet samples
    assert notes[-1][1] == "noisy"
    assert [rig.view(f).note for f in (1, 2, 3)] == ["", "", ""]


def test_x44_the_finger_that_lost_to_a_neighbour_says_veto_for_a_third_of_a_second():
    _press, rig = armed()
    rig.dip(RIGHT, 1, 2.0, 0.4)
    rig.dip(RIGHT, 2, 2.05, 0.2)  # half the depth, 0.05 s later
    notes = [(t, view.note) for t, view in frames_of(rig, 3.2, finger=2)]
    shown = [t for t, note in notes if note == "veto"]
    assert fingers_of(rig.events) == [1]
    assert 0.25 < shown[-1] - shown[0] + 1 / 30 < 0.4
    assert {note for t, note in notes if t > shown[-1]} == {""}


def test_x44_a_hand_gate_shows_as_a_note_only_after_it_has_held_for_half_a_second():
    press, rig = armed()
    rig.path[RIGHT] = lambda t: (0.2 + 0.7 * (t - 1.0), 0.4) if 1.0 <= t < 3.0 else (0.2 if t < 1.0 else 1.6, 0.4)
    rows = []
    while rig.t < 3.0:
        rig.run(rig.t + 1e-3)
        rows.append((rig.t, press.gate_of(1), rig.view(1).note))
    closed = min(t for t, gate, _ in rows if gate == "speed")
    shown = min(t for t, _, note in rows if note == "speed")
    assert shown - closed == pytest.approx(0.5, abs=1 / 30 + 1e-6)
    assert all(note == "" for t, _, note in rows if t < shown)


@pytest.mark.parametrize("score", [0.4, 0.59])
def test_x44_a_doubtful_hand_is_gated_but_the_user_is_not_told(score):
    press, rig = armed()
    rig.score[RIGHT] = score
    rig.dip(RIGHT, 1, 2.0)
    rows = frames_of(rig, 3.5)
    assert press.gate_of(1) == "score"
    assert rig.events == []
    assert {view.note for _, view in rows} == {""}


def test_x44_the_notes_of_a_stream_are_from_the_fixed_vocabulary():
    seen: set[str] = set()
    press = warmed()

    def watch(hands: list[HandSample]) -> None:
        seen.update(view.note for view in press.fingers(hands))

    events = []
    t = 4.0
    for k in range(14):  # a trill: it trips the tremor guard, which holds the hand
        events.append(tap(RIGHT, (1, 3)[k % 2], t, amp=42.0, dur=0.20, mt=0.1))
        t += 0.07
    sims = typists({RIGHT: events}, np.random.default_rng(0), coupling_p=0.0)
    run_sims(sims, press, t + 3.0, watch=watch)
    assert seen <= {"", "weak", "noisy", "veto", "speed", "posture", "coherence", "hold"}
    assert "hold" in seen


# ------------------------------------------------------------------------------------------------------- X52: the cost


@pytest.mark.skipif(bool(os.environ.get("CI_SLOW")), reason="CI_SLOW: a timing bound says nothing on a slow machine")
def test_x52_update_costs_under_a_millisecond_a_hand_frame_at_sixty_fps_with_two_hands():
    events = {side: [tap(side, k % 4, 3.0 + 0.5 * k) for k in range(30)] for side in (LEFT, RIGHT)}
    sims = typists(events, np.random.default_rng(0))
    tracker = HandTracker(DEFAULTS)
    frames = [
        tracker.update(Frame(k / 60.0, tuple(sims[s].observe(k / 60.0, 1 / 60) for s in sims), 1280, 720))
        for k in range(60 * 20)
    ]
    press = warmed()
    costs = []
    for hands in frames:
        began = time.perf_counter()
        press.update(hands)
        costs.append((time.perf_counter() - began) / len(hands))
    assert len(frames[0]) == 2
    assert statistics.median(costs) < 1e-3


# ------------------------------------------------------------------------------------------ the typing runs (X7, ...)
#
# A drill of ``n`` taps by the two simulated hands on the placed plane: balanced over the eight fingers, a random key
# of the finger's own, the finger's aim a motor-noise draw around the key centre, each tap no sooner than the hand
# can get there (Fitts). The detector sees it through the real ``HandTracker``; a tap counts as found when its own
# finger fires within -0.05 to +0.45 s of it. These are the streams the design's numbers were measured on.

FINGERING: dict[tuple[Side, int], str] = {
    (LEFT, 3): "qaz",
    (LEFT, 2): "wsx",
    (LEFT, 1): "edc",
    (LEFT, 0): "rfvtgb",
    (RIGHT, 0): "yhnujm",
    (RIGHT, 1): "ik,",
    (RIGHT, 2): "ol.",
    (RIGHT, 3): "p'/",
}
DIRECT = layout_for("direct")


def rest_geometry(
    rest: Sequence[float] = sy.REST, sides: Sequence[Side] = (LEFT, RIGHT)
) -> tuple[Plane, dict[tuple[Side, int], np.ndarray]]:
    """The plane placed on noise-free resting hands, and where each fingertip rests in pose space."""
    observed = [
        sy.AirTypist(
            side, HOME[side], [], np.random.default_rng(0), sy.Noise(sigma=0.0, glitch_p=0.0), rest=rest
        ).observe(0.0, 1 / 30)
        for side in sides
    ]
    samples = HandTracker(DEFAULTS).update(Frame(0.0, tuple(observed), 1280, 720))
    plane = place_plane([samples], layout=DIRECT, tuning=DEFAULTS).plane
    return plane, {(s.side, f.finger): f.aim for s in samples for f in s.fingers}


def movement_time(distance_units: float) -> float:
    """Fitts-like: 0.10 s plus 0.09 s a bit of log distance, in key units."""
    return 0.10 + 0.09 * math.log2(1.0 + distance_units)


def plan_drill(
    rng: np.random.Generator,
    plane: Plane,
    aims: Mapping[tuple[Side, int], np.ndarray],
    n_taps: int,
    style: str,
    *,
    mean_gap: float = 0.45,
    motor: tuple[float, float] = (0.20, 0.28),
    t0: float = 1.6,
    lead: float = 0.08,
) -> tuple[dict[Side, list[sy.Event]], list[dict[str, Any]]]:
    style_of = sy.STYLES[style]
    events: dict[Side, list[sy.Event]] = {LEFT: [], RIGHT: []}
    truth = []
    prev = {s: {"t": -9.0, "dur": 0.0, "tgt": np.zeros(2)} for s in (LEFT, RIGHT)}
    t = t0
    sides_fingers = [(s, f) for s in (LEFT, RIGHT) for f in range(4)]
    for _ in range(n_taps):
        if rng.random() < 0.12:  # the space bar, by the right index
            side, finger, key = RIGHT, 0, DIRECT.find(kind="space")
        else:
            side, finger = sides_fingers[int(rng.integers(0, 8))]
            chars = FINGERING[side, finger]
            key = DIRECT.find(char=chars[int(rng.integers(0, len(chars)))])
        u, v = plane.units(aims[side, finger])
        centre_u, centre_v = key.col + key.width / 2, key.row + 0.5
        if key.kind == "space":
            target_u = float(rng.uniform(key.col + 0.15 * key.width, key.col + 0.85 * key.width))
        else:
            target_u = centre_u + rng.normal(0, motor[0])
        target_v = centre_v + rng.normal(0, motor[1])
        amps = style_of["amps"] if finger < 2 else style_of["weak"]
        amp = rng.normal(*amps)
        target = np.array([target_u - u, target_v - v])
        last = prev[side]
        moving = movement_time(float(np.hypot(*(target - last["tgt"]))))
        event = sy.Event(
            side, finger, 0.0, (float(target[0]), float(target[1])), float(np.clip(amp, 10, 60)),
            float(rng.uniform(*style_of["dur"])), key=key.index, ch=key.en or " ", mt=moving,
        )  # fmt: skip
        t += max(0.18, rng.lognormal(math.log(mean_gap), 0.25))
        t = max(t, last["t"] + 0.5 * last["dur"] + 0.04 + moving + lead)
        event.t = t
        prev[side] = {"t": t, "dur": event.dur, "tgt": target}
        events[side].append(event)
        truth.append({"t": t, "side": side, "finger": finger, "key": key.index, "u": target_u, "v": target_v})
    return events, truth


def plan_warmup(rng: np.random.Generator, style: str) -> tuple[dict[Side, list[sy.Event]], float]:
    """Tap each finger once, the hands in turn, a second apart, at the finger's rest position."""
    style_of = sy.STYLES[style]
    events: dict[Side, list[sy.Event]] = {LEFT: [], RIGHT: []}
    t = 1.5
    for finger in range(4):
        for side in (LEFT, RIGHT):
            amp = rng.normal(*(style_of["amps"] if finger < 2 else style_of["weak"]))
            events[side].append(
                sy.Event(side, finger, t, (0.0, 0.0), float(np.clip(amp, 10, 60)), float(rng.uniform(*style_of["dur"])))
            )
            t += 1.0
    return events, t


def score(
    truth: Sequence[Mapping[str, Any]], fired: Sequence[PressEvent]
) -> tuple[dict[int, int], list[int], list[int]]:
    """(truth index -> fired index) of the taps that were found, and the fired indices left over (false taps)."""
    used: set[int] = set()
    found: dict[int, int] = {}
    for n, tr in enumerate(truth):
        for i, e in enumerate(fired):
            if (
                i not in used
                and e.side == tr["side"]
                and e.finger == tr["finger"]
                and tr["t"] - 0.05 <= e.t <= tr["t"] + 0.45
            ):
                used.add(i)
                found[n] = i
                break
    return found, [i for i in range(len(fired)) if i not in used], sorted(used)


def run_typing(
    seed: int,
    *,
    fps: float = 30.0,
    sigma: float = 0.001,
    warmup: bool = False,
    motor: tuple[float, float] = (0.20, 0.28),
    n_taps: int = 100,
    aim: str = "auto",
    peaks: bool = False,
    mean_gap: float = 0.45,
    lead: float = 0.08,
    style: str = "ordinary",
) -> dict[str, Any]:
    """One typing run; the counts the rows below pool. ``peaks`` also scores the aim at the peak of the stroke."""
    rng = np.random.default_rng(seed)
    plane, aims = rest_geometry()
    t_switch, warm_events = 0.0, None
    if warmup:
        warm_events, t_end = plan_warmup(rng, style)
        t_switch = t_end + 0.6
    events, truth = plan_drill(
        rng, plane, aims, n_taps, style, mean_gap=mean_gap, motor=motor, t0=t_switch + 1.6, lead=lead
    )
    if warm_events:
        for side in events:
            events[side] = warm_events[side] + events[side]
    noise = sy.Noise(sigma=sigma, glitch_p=0.003)
    sims = {
        s: sy.AirTypist(s, HOME[s], events[s], rng, noise, alpha=0.65, lead_s=lead, coupling_p=0.4)
        for s in (LEFT, RIGHT)
    }
    tuning = dataclasses.replace(DEFAULTS, air_aim=aim)
    press = AirTapPress(tuning, trace=peaks, calibrating=warmup)
    tracker = HandTracker(DEFAULTS)
    dt = 1.0 / fps
    fired: list[PressEvent] = []
    calibration: dict[tuple[Side, int], list[float]] = {}
    switched = not warmup
    aim_at: dict[tuple[int, int], dict[int, np.ndarray]] = {}
    noise_log: list[float] = []
    for k in range(int((truth[-1]["t"] + 1.5) / dt)):
        t = k * dt
        if not switched and t >= t_switch:
            for (side, finger), depths in calibration.items():
                press.set_finger(side, finger, float(np.mean(depths[:2])))
            press.set_calibrating(False)
            press.reset()
            switched = True
        hands = tracker.update(Frame(t, tuple(sims[s].observe(t, dt) for s in (LEFT, RIGHT)), 1280, 720))
        out = press.update(hands)
        if not switched:
            for e in out:
                if e.margin >= 0.5:
                    calibration.setdefault((e.side, e.finger), []).append(e.depth)
            continue
        fired += out
        if peaks:
            for h in hands:
                for f in h.fingers:
                    aim_at.setdefault((h.hand, f.finger), {})[k] = f.aim
        if k % int(fps) == 0 and t > 5.0:
            noise_now = press.quality().noise
            if noise_now is not None:
                noise_log.append(noise_now)
    found, false, _ = score(truth, fired)
    per: dict[tuple[Side, int], list[int]] = {(s, f): [0, 0] for s in (LEFT, RIGHT) for f in range(4)}
    for n, tr in enumerate(truth):
        per[tr["side"], tr["finger"]][0] += 1
        per[tr["side"], tr["finger"]][1] += n in found
    im = [(s, f) for s in (LEFT, RIGHT) for f in (0, 1)]
    right_key = {"aim": 0, "peak": 0}
    if found:
        for n, i in found.items():
            tr, e = truth[n], fired[i]
            key = DIRECT.key_at(*plane.units(e.aim))
            right_key["aim"] += key is not None and key.index == tr["key"]
        if peaks:
            records = {(r["hand"], r["finger"], round(r["t"], 3)): r for r in press.take_trace() if r["k"] == "fire"}
            for n, i in found.items():
                e = fired[i]
                r = records[e.hand, e.finger, round(e.t, 3)]
                point = aim_at[e.hand, e.finger][round(r["pkT"] * fps)]
                key = DIRECT.key_at(*plane.units(point))
                right_key["peak"] += key is not None and key.index == truth[n]["key"]
    return {
        "n_im": sum(per[k][0] for k in im),
        "h_im": sum(per[k][1] for k in im),
        "n": sum(v[0] for v in per.values()),
        "h": sum(v[1] for v in per.values()),
        "false_im": sum(1 for i in false if fired[i].finger < 2),
        "false": len(false),
        "hit": len(found),
        "key_aim": right_key["aim"],
        "key_peak": right_key["peak"],
        "noise": float(np.median(noise_log)) if noise_log else None,
        "fps": press.quality().fps,
    }


def pooled(fn: Callable[[Any], Any], jobs: Sequence[Any]) -> list[Any]:
    """``fn`` over the jobs, in a process pool when the machine has one (these rows are minutes of pure Python).

    The workers come from a fork server, not from a fork of the test process: that process has threads by now (other
    tests start them), and a fork with threads about can hand the child a lock nobody will release.
    """
    workers = min(len(jobs), os.cpu_count() or 1)
    try:
        context = multiprocessing.get_context("forkserver")
    except ValueError:  # no fork server here (Windows): the same answer, in a loop
        return [fn(job) for job in jobs]
    if workers < 2:
        return [fn(job) for job in jobs]
    with ProcessPoolExecutor(max_workers=workers, mp_context=context) as pool:
        return list(pool.map(fn, jobs, chunksize=1))


def typing_job(arg: tuple[int, dict[str, Any]]) -> dict[str, Any]:
    seed, kw = arg
    return run_typing(seed, **kw)


@functools.cache
def pooled_typing(seeds: int = 24, **kw: Any) -> dict[str, float]:
    """Counts summed over ``seeds`` runs (seeds 0 up) of the given configuration."""
    rows = pooled(typing_job, [(s, kw) for s in range(seeds)])
    total = {
        k: float(sum(r[k] for r in rows))
        for k in ("n_im", "h_im", "n", "h", "false_im", "false", "hit", "key_aim", "key_peak")
    }
    levels = [r["noise"] for r in rows if r["noise"] is not None]
    total["noise"] = float(np.median(levels)) if levels else float("nan")
    return total


# --------------------------------------------------------------------------------- X7, X8, X20, X21, X22: pooled runs
#
# Every bound is at least three standard errors from the pooled 24-seed measurement the design records, so a correct
# port passes with any other random draws and a regression of a few points does not.


def test_x7_recall_and_false_taps_after_a_simulated_warm_up():
    c = pooled_typing(24, warmup=True)  # 30 fps, landmark noise 0.001, ordinary taps, 100 taps a seed
    assert c["n_im"] > 1200
    assert c["h_im"] / c["n_im"] >= 0.90  # index and middle (measured 0.931)
    assert c["h"] / c["n"] >= 0.84  # all fingers (0.860)
    assert c["false_im"] / c["n_im"] <= 0.055  # false taps on index and middle (0.035)


@pytest.mark.parametrize("fps", [15.0, 24.0, 30.0, 60.0])
def test_x20_recall_holds_at_every_frame_rate(fps):
    c = pooled_typing(24, fps=fps)
    assert c["h_im"] / c["n_im"] >= 0.88  # measured 0.919, 0.903, 0.932, 0.947


@pytest.mark.parametrize(
    ("fps", "shortest"),
    [(15.0, 1), (19.0, 1), (20.0, 2), (24.0, 2), (30.0, 2), (39.0, 2), (40.0, 2), (41.0, 3), (45.0, 3), (60.0, 3)],
)
def test_x20_the_smoothing_window_is_one_three_or_five_samples_by_frame_rate(fps, shortest):
    """A pulse of ``k`` frames survives the median of ``n`` samples only if ``k > n // 2``: the shortest pulse that is
    typed at a frame rate is 1, 2 or 3 frames for a window of 1, 3 or 5 samples. The rate the window follows is an
    average that starts at 30 and approaches the true one from that side, so at exactly 20 fps and exactly 40 fps
    it never crosses the switch (the 40 is not a key count): the design's 5 samples at 40 fps is 3 here, and 41 fps
    is the first that gets 5."""

    def typed(frames: int) -> bool:
        _press, rig = armed(fps=fps)
        start = round(4.0 * fps) / fps
        rig.shape(RIGHT, 1, lambda t: 0.5 if start - 0.5 / fps < t < start + (frames - 0.5) / fps else 0.0)
        return bool(rig.run(6.0))

    assert typed(shortest)
    if shortest > 1:
        assert not typed(shortest - 1)


def test_x21_the_aim_at_the_onset_lands_on_the_key_the_aim_at_the_peak_does_not():
    c = pooled_typing(24, motor=(0.0, 0.0), aim="onset", peaks=True)  # lead 0.08 s, no motor noise
    assert c["hit"] > 1800
    assert c["key_aim"] / c["hit"] >= 0.97  # measured 0.999
    assert c["key_peak"] / c["hit"] <= 0.80  # 0.759: the fingertip has travelled on by then


def test_x21_the_aim_at_the_commit_is_nearly_as_good_when_asked_for():
    c = pooled_typing(24, motor=(0.0, 0.0), aim="commit")
    assert c["key_aim"] / c["hit"] >= 0.95  # measured 0.966


@pytest.mark.parametrize(("sigma", "expected"), [(0.001, 0.017), (0.002, 0.030), (0.004, 0.052)])
def test_x22_the_noise_estimate_of_a_typing_stream_follows_the_landmark_noise(sigma, expected):
    assert pooled_typing(6, warmup=True, sigma=sigma)["noise"] == pytest.approx(expected, rel=0.25)


def neg_job(arg: tuple[str, int]) -> int:
    """Events a minute of two hands that never mean a key (seed ``s`` is the reference's 10000 + s)."""
    name, seed = arg
    rng = np.random.default_rng(10_000 + seed)
    noise = sy.Noise(sigma=0.001, glitch_p=0.003)
    sims = {s: sy.scenario(name, s, rng, noise, duration=62.0) for s in (LEFT, RIGHT)}
    return len(run_sims(sims, AirTapPress(DEFAULTS), 60.0))


NEG_BOUNDS = {
    "still": 0.6,
    "roll": 0.6,
    "thumb": 0.6,
    "drift": 0.6,
    "wave": 2.0,
    "open_close": 2.5,
    "reach": 4.0,
    "talk_hands": 15.0,
    "fidget": 42.0,  # a documented stress: it cannot be told from tapping
}


@functools.cache
def neg_means() -> dict[str, float]:
    jobs = [(name, seed) for name in NEG_BOUNDS for seed in range(24)]
    counts = pooled(neg_job, jobs)
    return {name: float(np.mean(counts[24 * k : 24 * (k + 1)])) for k, name in enumerate(NEG_BOUNDS)}


@pytest.mark.parametrize("name", list(NEG_BOUNDS))
def test_x8_a_hand_that_is_not_typing_types_at_most_so_many_keys_a_minute(name):
    assert neg_means()[name] <= NEG_BOUNDS[name]  # measured: 0.12 0.08 0.12 0.25 1.17 1.46 2.54 12.2 36.2


# ---------------------------------------------------------------------------- X13: a hand that slowly lets go (sag)


def sag_job(arg: tuple[int, float, bool]) -> int:
    """Both hands, fingers drooping from REST towards RELAXED over forty seconds: the taps invented in 60 s."""
    seed, sigma, degraded = arg
    rng = np.random.default_rng(20_000 + seed)
    noise = sy.Noise(sigma=sigma, glitch_p=0.003)

    def angles(t: float, name: str) -> tuple[float, ...]:
        k = min(1.0, t / 40.0) * (0.6 + 0.4 * (sy.FNAMES.index(name) / 3))
        return sy.lerp(sy.REST, syn.RELAXED, k)

    sims = {s: sy.NegHand(s, HOME[s], rng, noise, angles=angles) for s in (LEFT, RIGHT)}
    press = AirTapPress(DEFAULTS)
    if degraded:
        press.set_level("degraded")
    return len(run_sims(sims, press, 60.0))


@pytest.mark.parametrize(("sigma", "degraded"), [(0.001, False), (0.002, True)], ids=["quiet", "noisy_degraded"])
def test_x13_a_drooping_hand_invents_at_most_two_taps_a_minute(sigma, degraded):
    counts = pooled(sag_job, [(seed, sigma, degraded) for seed in range(8)])
    assert np.mean(counts) <= 2.0  # measured 1.2 and 1.0


# ------------------------------------------------------------------------- X56: reach recall by displacement (layout)

STYLE = sy.STYLES["ordinary"]
#: Taps per seed of the reach rows (X56).
REACH_TAPS = 40  # not a key count


def reach_job(arg: tuple[Side, int, float, float, int]) -> tuple[int, int]:
    """Forty taps 1.3 s apart of one finger at a key ``(du, dv)`` key units from its own rest key, then the recall."""
    side, finger, du, dv, seed = arg
    rng = np.random.default_rng(seed)
    events: dict[Side, list[sy.Event]] = {LEFT: [], RIGHT: []}
    truth = []
    t = 4.0
    for _ in range(REACH_TAPS):
        amp = float(np.clip(rng.normal(*STYLE["amps" if finger < 2 else "weak"]), 10, 60))
        events[side].append(sy.Event(side, finger, t, (du, dv), amp, float(rng.uniform(*STYLE["dur"])), mt=0.3))
        truth.append({"t": t, "side": side, "finger": finger})
        t += 1.3
    noise = sy.Noise(sigma=0.001, glitch_p=0.003)
    sims = {s: sy.AirTypist(s, HOME[s], events[s], rng, noise, alpha=0.65, lead_s=0.08) for s in (LEFT, RIGHT)}
    got = run_sims(sims, warmed(), t + 1.0)
    return len(score(truth, got)[0]), len(truth)


#: (name, side, finger, du, dv, bound): the design's cells, bound = the measurement minus a margin (alpha 0.65).
REACH_CELLS = [
    ("index_to_backspace", RIGHT, 0, 4.25, 0.0, 0.90),
    ("index_to_insert", RIGHT, 0, 4.0, 3.0, 0.90),
    ("pinky_to_insert", RIGHT, 3, 1.0, 3.0, 0.78),
    ("left_pinky_to_clear", LEFT, 3, 0.25, 3.0, 0.78),
    ("ring_to_insert", RIGHT, 2, 2.0, 3.0, 0.80),
    ("left_ring_to_send", LEFT, 2, 0.75, 3.0, 0.80),
    ("right_index_rest", RIGHT, 0, 0.0, 0.0, 0.90),
    ("right_ring_rest", RIGHT, 2, 0.0, 0.0, 0.90),
    ("right_pinky_rest", RIGHT, 3, 0.0, 0.0, 0.90),
    ("left_ring_rest", LEFT, 2, 0.0, 0.0, 0.90),
    ("left_pinky_rest", LEFT, 3, 0.0, 0.0, 0.90),
]


@functools.cache
def reach_recalls() -> dict[str, float]:
    jobs = [(side, finger, du, dv, seed) for _, side, finger, du, dv, _ in REACH_CELLS for seed in range(6)]
    rows = pooled(reach_job, jobs)
    out = {}
    for k, (name, *_rest) in enumerate(REACH_CELLS):
        cell = rows[6 * k : 6 * (k + 1)]
        out[name] = sum(h for h, _ in cell) / sum(n for _, n in cell)
    return out


@pytest.mark.parametrize(("name", "bound"), [(c[0], c[5]) for c in REACH_CELLS], ids=[c[0] for c in REACH_CELLS])
def test_x56_a_finger_reaching_to_a_key_is_still_seen_tapping(name, bound):
    assert reach_recalls()[name] >= bound  # measured 1.00 0.99 0.87 0.86 0.91 0.93 | 1.00 0.96 0.95 0.97 0.95


def test_x56_no_reach_key_lies_above_the_finger_that_types_it():
    """An upward reach collapses air detection (index one row up 0.51, pinky 0.12 at alpha 0.65): the four keys the
    drill asks for are level with or below the resting fingertip, at the displacements the rows above measured."""
    review = layout_for("review")
    homes = {("right", 0): "j", ("right", 3): "'", ("left", 3): "a", ("left", 2): "s"}
    want = {
        "backspace": (RIGHT, 0, 4.25, 0),
        "insert": (RIGHT, 3, 1.0, 3),
        "clear": (LEFT, 3, 0.25, 3),
        "enter": (LEFT, 2, 0.75, 3),
    }
    names = ("index", "middle", "ring", "pinky")
    assert {(k, s, f) for k, (s, f, _, _) in want.items() for s, f in [(s, names[f])]} == set(
        limits.AIR_DRILL_REACH_KEYS
    )
    for kind, (side, finger, du, dv) in want.items():
        key, home = review.find(kind=kind), review.find(char=homes[side, finger])
        assert (key.col + key.width / 2 - (home.col + home.width / 2), key.row - home.row) == (du, dv)
        assert key.row - home.row >= 0


# ---------------------------------------------------------------------------------- X57: holes in the stream (gaps)


def hole_counts(
    fps: float, hole_s: float, every: float, *, jitter: float = 0.0, seconds: float = 60.0, both: bool = True
) -> tuple[int, float, int]:
    """(least, median, most) ``gaps()`` after 6 s of still hands that lose ``hole_s`` of the stream every ``every``."""
    sides = (LEFT, RIGHT) if both else (RIGHT,)
    press, rig = armed(depth=None, fps=fps, sides=sides)
    jitter_rng = np.random.default_rng(1)
    windows = [(k * every + 0.37, k * every + 0.37 + hole_s) for k in range(int(seconds / every) + 1)] if hole_s else []
    seen = []
    k = 0
    while k / fps < seconds:
        t = k / fps
        k += 1
        hands = rig.make(t + (float(jitter_rng.normal(0, jitter)) if jitter else 0.0))
        if any(a <= t < b for a, b in windows):
            continue
        press.update(hands)
        if t > 6.0:
            seen.append(press.gaps())
    return min(seen), float(np.median(seen)), max(seen)


@pytest.mark.parametrize(
    ("fps", "hole", "every", "jitter", "least", "most"),
    [
        (30.0, 0.0, 1.0, 0.0, 0, 0),
        (30.0, 0.0, 1.0, 0.004, 0, 0),  # timestamp jitter of 4 ms is not a hole
        (30.0, 1 / 30, 1.0, 0.0, 0, 0),  # one frame lost a second is not a hole
        (30.0, 0.10, 3.0, 0.0, 1, 2),
        (30.0, 0.10, 1.5, 0.0, 3, 4),
        (30.0, 0.10, 1.0, 0.0, 5, 6),
        (30.0, 0.20, 3.0, 0.0, 1, 2),  # above GAP_RESET_S, and counted once, not twice
        (15.0, 0.0, 1.0, 0.0, 0, 0),
        (15.0, 0.0, 1.0, 0.004, 0, 0),
        (15.0, 1 / 15, 1.0, 0.0, 0, 1),
        (15.0, 0.10, 3.0, 0.0, 1, 2),
        (15.0, 0.10, 1.5, 0.0, 1, 3),  # 0.1 s removes one frame or two, and counts only when it removes two
        (15.0, 0.20, 3.0, 0.0, 1, 2),
    ],
)
def test_x57_holes_in_the_stream_are_counted_once_each(fps, hole, every, jitter, least, most):
    low, _, high = hole_counts(fps, hole, every, jitter=jitter)
    assert (low, high) == (least, most)


def test_x57_two_hands_that_see_one_hole_count_it_once():
    both = hole_counts(30.0, 0.10, 3.0, both=True)
    one = hole_counts(30.0, 0.10, 3.0, both=False)
    assert both == one


@pytest.mark.parametrize(
    ("fps", "absence", "counted"), [(30.0, 1.2, 0), (15.0, 1.2, 0), (30.0, 0.5, 1), (15.0, 0.5, 1)]
)
def test_x57_a_hand_that_leaves_for_a_while_is_not_a_hole(fps, absence, counted):
    """An absence over a second is the user's hands going away; one of half a second is a camera that stalled."""
    press, rig = armed(depth=None, fps=fps, sides=(LEFT, RIGHT))
    for side in (LEFT, RIGHT):
        rig.visible[side] = lambda t: not 6.0 <= t < 6.0 + absence
    most = 0
    while rig.t < 12.0:
        rig.run(rig.t + 1e-3)
        most = max(most, press.gaps())
    assert most == counted


def test_x57_a_hole_is_forgotten_after_five_seconds():
    press, rig = armed(depth=None, sides=(LEFT, RIGHT))
    for side in (LEFT, RIGHT):
        rig.visible[side] = lambda t: not 6.0 <= t < 6.1
    rig.run(6.3)
    assert press.gaps() == 1
    rig.run(11.0)
    assert press.gaps() == 1
    rig.run(11.6)
    assert press.gaps() == 0


def random_holes(seed: int, fps: float, every: float, ms: float) -> set[int]:
    """The frame numbers lost to holes of ``ms`` milliseconds that begin at exponentially spaced times."""
    frames = max(1, round(ms / (1000 / fps)))
    starts = np.cumsum(np.random.default_rng(seed + 7).exponential(every, 400)) + 4.0
    return {int(s0 * fps) + j for s0 in starts for j in range(frames)}


def holes_share(arg: tuple[int, float, float, float]) -> float:
    """The share of time (after 8 s) on which ``gaps() >= 3``, still hands, holes of 100 ms. ``hold`` is the tracker's
    ``hand_hold_s``: the hand has to be the same hand on the far side of a hole for the hole to be counted. At 15 fps
    a hole of two frames is 0.2 s, exactly the default hold, and a track that is dropped there comes back as a new
    hand (nothing counted, a fresh warm-up); the design's figures were measured with ids that never expire, which a
    hold of 0.30 reproduces."""
    seed, fps, every, hold = arg
    rng = np.random.default_rng(seed)
    noise = sy.Noise(sigma=0.001, glitch_p=0.003)
    sims = {s: sy.scenario("still", s, rng, noise, duration=62.0) for s in (LEFT, RIGHT)}
    lost = random_holes(seed, fps, every, 100)
    press = AirTapPress(DEFAULTS)
    shares = []

    def watch(hands: list[HandSample]) -> None:
        if hands[0].t > 8.0:
            shares.append(press.gaps() >= limits.AIR_LEVEL_GAPS_DEGRADED)

    tracking = dataclasses.replace(DEFAULTS, hand_hold_s=hold)
    run_sims(sims, press, 60.0, fps=fps, drop=lambda k, t: k in lost, watch=watch, tracking=tracking)
    return float(np.mean(shares))


@pytest.mark.parametrize("fps", [30.0, 15.0])
def test_x57_holes_a_second_apart_keep_the_count_at_three_or_more_most_of_the_time(fps):
    assert np.mean(pooled(holes_share, [(s, fps, 1.0, 0.30) for s in range(8)])) >= 0.70  # measured 0.84


@pytest.mark.parametrize("fps", [30.0, 15.0])
def test_x57_holes_three_seconds_apart_rarely_do(fps):
    assert np.mean(pooled(holes_share, [(s, fps, 3.0, 0.30) for s in range(8)])) <= 0.30  # measured 0.17


def holed_typing(arg: tuple[int, float, float]) -> tuple[int, int]:
    """(found, taps) of index and middle for the X7 stream with holes of 100 ms at exponential spacing ``every``."""
    seed, fps, every = arg
    rng = np.random.default_rng(seed)
    plane, aims = rest_geometry()
    events, truth = plan_drill(rng, plane, aims, 100, "ordinary", t0=4.0)
    noise = sy.Noise(sigma=0.001, glitch_p=0.003)
    sims = {s: sy.AirTypist(s, HOME[s], events[s], rng, noise, lead_s=0.08) for s in (LEFT, RIGHT)}
    lost = random_holes(seed, fps, every, 100) if every else set()
    tracking = dataclasses.replace(DEFAULTS, hand_hold_s=0.30)  # see holes_share
    got = run_sims(sims, warmed(), truth[-1]["t"] + 1.5, fps=fps, drop=lambda k, t: k in lost, tracking=tracking)
    found = score(truth, got)[0]
    index_middle = [n for n, tr in enumerate(truth) if tr["finger"] < 2]
    return sum(n in found for n in index_middle), len(index_middle)


@pytest.mark.parametrize(
    ("fps", "every", "bound"),
    [(30.0, 3.0, 0.85), (30.0, 1.0, 0.68), (15.0, 1.0, 0.52)],
    ids=["30fps_3s", "30fps_1s", "15fps_1s"],
)
def test_x57_typing_through_holes_keeps_its_recall(fps, every, bound):
    rows = pooled(holed_typing, [(s, fps, every) for s in range(8)])
    assert sum(h for h, _ in rows) / sum(n for _, n in rows) >= bound  # measured 0.90 0.75 0.59


# ----------------------------------------------------------------------------- X58: bursts are disclosed, not defended


def burst_job(arg: tuple[int, float]) -> tuple[int, float]:
    seed, sigma_b = arg
    rng = np.random.default_rng(seed)
    noise = sy.Noise(sigma=0.001, glitch_p=0.003)
    windows = [(k * 4.0 + 1.0, k * 4.0 + 1.5) for k in range(15)]
    sims = {
        s: sy.Burst(sy.scenario("still", s, rng, noise, duration=60.0), windows, sigma_b, rng) for s in (LEFT, RIGHT)
    }
    press = AirTapPress(DEFAULTS)
    events = run_sims(sims, press, 60.0)
    quality = press.quality().noise
    assert quality is not None
    return len(events), quality


@pytest.mark.parametrize("sigma_b", [0.004, 0.010])
def test_x58_half_a_second_of_jitter_every_four_seconds_is_a_few_phantoms_and_the_ladder_does_not_see_it(sigma_b):
    """Pins today's behaviour as a regression guard and a disclosure; a burst gate would change it (7.4)."""
    rows = pooled(burst_job, [(s, sigma_b) for s in range(6)])
    assert np.mean([n for n, _ in rows]) <= 30  # events a minute; measured 20 and 12
    assert max(q for _, q in rows) < limits.AIR_LEVEL_NOISE_DEGRADED  # measured 0.0169 and 0.0167 at the worst


# -------------------------------------------------------------------------- X59: the refractory spacing, the score gate


def refractory_pairs(spacing: float, fps: float, *, refractory: float | None = None) -> int:
    """Pairs of events of one finger closer than AIR_REFRACTORY_S, over 10 double taps ``spacing`` s apart."""
    events = []
    t = 4.0
    for _ in range(10):
        events += [tap(RIGHT, 1, t, 40.0, 0.16), tap(RIGHT, 1, t + spacing, 40.0, 0.16)]
        t += 1.5
    press = warmed()
    if refractory is not None:
        press._c = dataclasses.replace(press._c, refractory_s=refractory)
    sims = typists({RIGHT: events}, np.random.default_rng(2), coupling_p=0.0, lead_s=0.08)
    ts = sorted(e.t for e in run_sims(sims, press, t + 1.0, fps=fps) if e.finger == 1)
    return sum(1 for a, b in pairwise(ts) if b - a < limits.AIR_REFRACTORY_S - 1e-9)


@pytest.mark.parametrize("fps", [30.0, 60.0])
@pytest.mark.parametrize("spacing", [0.07, 0.10])
def test_x59_one_finger_never_types_twice_inside_the_refractory_spacing(spacing, fps):
    assert refractory_pairs(spacing, fps) == 0


def test_x59_the_double_tap_stimulus_does_violate_the_spacing_without_the_refractory():
    assert (
        sum(
            refractory_pairs(0.07, fps, refractory=0.0) + refractory_pairs(0.10, fps, refractory=0.0)
            for fps in (30.0, 60.0)
        )
        > 0
    )


def low_score_events(score_of: float, *, min_score: float | None = None) -> tuple[int, dict[str, int]]:
    events = []
    for rep in range(6):
        events += [tap(RIGHT, 0, 4.0 + 1.0 * rep, 40.0, 0.2), tap(RIGHT, 1, 4.5 + 1.0 * rep, 40.0, 0.2)]
    rng = np.random.default_rng(4)
    sim = typists({RIGHT: events}, rng, coupling_p=0.0)[RIGHT]

    class Unsure:
        side = RIGHT

        @staticmethod
        def observe(t: float, dt: float):
            seen = sim.observe(t, dt)
            return (
                dataclasses.replace(seen, score=score_of)
                if dataclasses.is_dataclass(seen)
                else _with_score(seen, score_of)
            )

    press = warmed()
    if min_score is not None:
        press._c = dataclasses.replace(press._c, min_score=min_score)
    got = run_sims({RIGHT: Unsure()}, press, 11.0)
    return len(got), dict(press.rejects)


def _with_score(observation: Any, value: float) -> Any:
    from jarvis_hands.landmarks import HandObservation

    return HandObservation(
        handedness=observation.handedness, score=value, image=observation.image, world=observation.world
    )


def test_x59_a_hand_the_labeller_doubts_types_nothing_and_says_g_score():
    count, rejects = low_score_events(0.4)
    assert count == 0
    assert rejects["g_score"] > 0


def test_x59_the_same_taps_are_typed_when_the_score_is_trusted():
    assert low_score_events(0.4, min_score=0.0)[0] >= 6  # the gate removed: the stimulus has teeth
    assert low_score_events(0.9)[0] >= 6


# ------------------------------------------------------------------------------- X60, X61: one hand, and the pace

TEXT = (
    "can you check why the failing test in the utils folder passes locally but not on the build server please "
    "also tell me what the second error message means and then fix it so that we can merge this today thanks "
    "i would like a short summary of the change first and then the full diff after that we can ship it"
)
FINGER_OF: dict[str, tuple[Side, int]] = {c: sf for sf, chars in FINGERING.items() for c in chars}
FINGER_OF[" "] = (RIGHT, 0)


def text_key(ch: str):
    return DIRECT.find(kind="space") if ch == " " else DIRECT.find(char=ch)


def plan_text(
    rng: np.random.Generator,
    plane: Plane,
    aims: Mapping[tuple[Side, int], np.ndarray],
    n_taps: int,
    mean_gap: float,
    *,
    one_hand: bool = False,
    lead: float = 0.08,
    motor: tuple[float, float] = (0.20, 0.28),
    t0: float = 4.0,
) -> tuple[dict[Side, list[sy.Event]], list[dict[str, Any]]]:
    """English text typed with the standard fingering, or (``one_hand``) by the right index finger alone, with
    lognormal gaps between keys (sd 0.25); a finger starts no new stroke before its last one ended."""
    events: dict[Side, list[sy.Event]] = {LEFT: [], RIGHT: []}
    truth = []
    prev = {s: {"t": -9.0, "dur": 0.0, "tgt": np.zeros(2)} for s in (LEFT, RIGHT)}
    chars = list(TEXT) if one_hand else list(TEXT)[: n_taps * 3]
    start = int(rng.integers(0, 80))
    sequence = (chars[start:] + chars[:start])[:n_taps]
    t = t0
    for i, ch in enumerate(sequence):
        side, finger = (RIGHT, 0) if one_hand else FINGER_OF[ch]
        key = text_key(ch)
        u, v = plane.units(aims[side, finger])
        if key.kind == "space":
            target_u = float(rng.uniform(key.col + 0.15 * key.width, key.col + 0.85 * key.width))
        else:
            target_u = key.col + key.width / 2 + rng.normal(0, motor[0])
        target_v = key.row + 0.5 + rng.normal(0, motor[1])
        amp = rng.normal(*(STYLE["amps"] if finger < 2 else STYLE["weak"]))
        target = np.array([target_u - u, target_v - v])
        last = prev[side]
        moving = movement_time(float(np.hypot(*(target - last["tgt"]))))
        event = sy.Event(
            side, finger, 0.0, (float(target[0]), float(target[1])), float(np.clip(amp, 10, 60)),
            float(rng.uniform(*STYLE["dur"])), key=key.index, ch=ch, mt=moving,
        )  # fmt: skip
        if one_hand:
            if i:
                t += max(0.18, rng.lognormal(math.log(mean_gap), 0.25))
            t = max(t, last["t"] + 0.5 * last["dur"] + 0.04 + moving + lead)
        else:
            if i:
                t += max(0.10, rng.lognormal(math.log(mean_gap), 0.25))
            earlier = [e for e in events[side] if e.finger == finger]
            if earlier:
                t = max(t, earlier[-1].t + earlier[-1].dur + 0.04)
        event.t = t
        prev[side] = {"t": t, "dur": event.dur, "tgt": target}
        events[side].append(event)
        truth.append({"t": t, "side": side, "finger": finger, "key": key.index})
    return events, truth


def text_job(arg: tuple[int, float, bool, float, float]) -> tuple[int, int]:
    """(found, keys) of one text run: ``alpha`` is the hand's share of the fingertip's travel, ``lead`` how long the
    fingertip waits over the key before the tap."""
    seed, mean_gap, one_hand, alpha, lead = arg
    rng = np.random.default_rng(seed)
    plane, aims = rest_geometry(sides=(RIGHT,) if one_hand else (LEFT, RIGHT))
    events, truth = plan_text(rng, plane, aims, 100 if one_hand else 120, mean_gap, one_hand=one_hand, lead=lead)
    noise = sy.Noise(sigma=0.001, glitch_p=0.003)
    sides = (RIGHT,) if one_hand else (LEFT, RIGHT)
    sims = {s: sy.AirTypist(s, HOME[s], events[s], rng, noise, alpha=alpha, lead_s=lead) for s in sides}
    got = run_sims(sims, warmed(), truth[-1]["t"] + 1.5)
    return len(score(truth, got)[0]), len(truth)


def recall_of(jobs: Sequence[tuple[int, float, bool, float, float]]) -> float:
    rows = pooled(text_job, jobs)
    return sum(h for h, _ in rows) / sum(n for _, n in rows)


@pytest.mark.parametrize(
    ("alpha", "lead", "gap", "bound"),
    [(0.65, 0.2, 1.8, 0.87), (1.0, 0.2, 0.9, 0.90), (1.0, 0.2, 1.8, 0.96)],
    ids=["moving_hand_slow", "whole_hand_fast", "whole_hand_slow"],
)
def test_x60_one_hand_typing_the_whole_keyboard_with_the_index_finger(alpha, lead, gap, bound):
    assert recall_of([(s, gap, True, alpha, lead) for s in range(8)]) >= bound  # measured 0.91 0.94 0.99


@pytest.mark.parametrize(("gap", "bound"), [(1.0, 0.89), (0.6, 0.84), (0.4, 0.74)])
def test_x61_recall_falls_with_the_pace_of_the_typist(gap, bound):
    assert recall_of([(s, gap, False, 0.65, 0.08) for s in range(8)]) >= bound  # measured 0.92 0.88 0.79
