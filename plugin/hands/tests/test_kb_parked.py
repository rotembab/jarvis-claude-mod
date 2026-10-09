"""N48: the parked-hand acceptance (DESIGN 5.10): a hand that rests one finger on Insert or on Send while it makes the
phantom taps of a still, talking or fidgeting hand, through the real tracker, ``AirTapPress`` and the review machine.

The long run is the design's: ``still``, ``talk_hands`` and ``fidget`` at landmark noise 0.001 and 0.002, 12 seeds of
120 s, with the right pinky, ring or index finger on ``Insert`` and the left ring, middle, pinky or index finger on
``Send``. It runs only with ``KB_FULL=1`` (about half an hour) and is skipped otherwise. What it asks:

* a run of 120 s never completes an Insert (and so never a Send);
* a deliberate first Insert tap at a random moment of the stream is completed by phantoms alone in at most 0.5% of 300
  trials per seed;
* a random completed Insert is followed by an accidental Send within its 10 s in at most 0.5% of the trials of a cell.

The trials are a replay: the taps the session delivered to the review machine in the 120 s run are fed to a fresh
machine after a deliberate first Insert tap (or a deliberate completed Insert) at a random moment. That is the study's
Monte Carlo on the phantoms the real detector made, not on a model of them.

The ladder is out of this test: the question is what the detector and the machine do with a hand that never stops
gesturing, and the ladder would end the session (to pinch or ``air_unreliable``) within seconds of a talking hand. Its
noise thresholds are set out of reach for the run (``ladder`` is monkeypatched), which makes the exposure the worst
case, 120 s of every stream.

Result at the time of writing (the end-to-end step, T1's detector as ported): the cells of ``fidget`` and
``talk_hands`` with a pinky or ring finger on Insert do not meet the bars, with or without the ladder: the pinky makes
about 20 phantoms a minute in ``fidget`` and a third of all the taps of the hand land on the one key, so three in a row
are not rare (``test_n48_repro_...`` below is the smallest repro, xfail so that a fix shows as XPASS).

A reduced version of the same pipeline runs in every suite (one seed, 30 s, one cell of each key) so that the parking,
the recording and the replay stay tested.
"""

from __future__ import annotations

import math
import os
import random
from collections.abc import Callable

import pytest

from jarvis_hands.keyboard import ladder
from jarvis_hands.keyboard.limits import GUARD_MAX_S, INSERT_TAPS, SEND_TAPS, SEND_WINDOW_S
from jarvis_hands.keyboard.review import ReviewMachine
from jarvis_hands.keyboard.rig import AirScene
from jarvis_hands.keyboard.types import KeyKind, Side

FULL = os.environ.get("KB_FULL") == "1"
SEEDS = range(12) if FULL else range(1)
RUN_S = 120.0 if FULL else 30.0
TRIALS = 300
#: The 0.5% bar of the design, as a count of trials.
BAR = 0.005
SCENARIOS = ("still", "talk_hands", "fidget")
NOISES = (0.001, 0.002)
#: (side, finger) that rests on the key: Appendix C's fingers are 0 index, 1 middle, 2 ring, 3 pinky.
ON_INSERT: tuple[tuple[Side, int], ...] = (("right", 3), ("right", 2), ("right", 0))
ON_SEND: tuple[tuple[Side, int], ...] = (("left", 2), ("left", 1), ("left", 3), ("left", 0))

Tap = tuple[float, KeyKind, str]


@pytest.fixture
def no_ladder(monkeypatch: pytest.MonkeyPatch) -> None:
    """The noise levels of the ladder out of reach: the run is 120 s of the detector, not of its fallback."""
    monkeypatch.setattr(ladder, "AIR_LEVEL_NOISE_OFF", math.inf)
    monkeypatch.setattr(ladder, "AIR_LEVEL_NOISE_DEGRADED", math.inf)


class Tapped:
    """Every tap the review machine of the scene received, as ``(time, kind, character)``."""

    def __init__(self, scene: AirScene) -> None:
        self.taps: list[Tap] = []
        machine = scene.session._machine
        assert machine is not None
        original = machine.tap

        def tap(kind: KeyKind, ch: str, t: float, **kw: object) -> object:
            self.taps.append((t, kind, ch))
            return original(kind, ch, t, **kw)  # type: ignore[arg-type]

        machine.tap = tap  # type: ignore[method-assign]


def parked_run(
    name: str, sigma: float, seed: int, who: tuple[Side, int], key: str, seconds: float
) -> tuple[AirScene, list[Tap], float, float]:
    """A warmed-up session with some text in the box, then ``seconds`` of scenario ``name`` by a hand that rests finger
    ``who`` on ``key``. Returns the scene, the taps the machine got and the times the recording began and ended.

    The box is typed with the default landmark noise (a user who types is not shaking); ``sigma`` is the scenario's."""
    scene = AirScene(seed=seed, alpha=1.0)
    scene.arm()
    scene.run(0.5)
    assert scene.user_type("hello"), scene.shown
    scene.negative(name, sigma=sigma, seconds=seconds + 20.0)
    scene.park(*who, key)
    scene.run(1.5)  # the jump onto the key and the detector settling on it
    tapped = Tapped(scene)
    begun = scene.t
    scene.run(seconds)
    return scene, tapped.taps, begun, scene.t


class Replay:
    """A fresh review machine and the frames around it: the controller's loop with a sink that takes everything."""

    DT = 1 / 30

    def __init__(self) -> None:
        self.m = ReviewMachine(enter="twice")
        self.t = 0.0

    def tick(self, t: float) -> None:
        self.t = t
        step = self.m.tick(t, None)
        if step is not None:
            self.m.note_step(step, "sent", t, None)
        self.m.take_summary()

    def tap(self, t: float, kind: KeyKind, ch: str = "") -> None:
        self.tick(t)
        step = self.m.tap(kind, ch, t)
        if step is not None:
            self.m.note_step(step, "sent", t, None)
        self.m.take_summary()

    def drain(self, limit_s: float = 60.0) -> float:
        """Frames until the run (if any) is over; the time after."""
        t, end = self.t, self.t + limit_s
        while self.m.running and t < end:
            t += self.DT
            self.tick(t)
        return t

    def fill(self, text: str, t: float, gap: float = 0.3) -> float:
        for ch in text:
            self.tap(t, "space" if ch == " " else "char", "" if ch == " " else ch)
            t += gap
        return t


def feed(replay: Replay, taps: list[Tap], after: float, until: float) -> None:
    for t, kind, ch in taps:
        if after < t <= until:
            replay.tap(t, kind, ch)
    replay.tick(until)


def insert_trial(taps: list[Tap], t0: float) -> bool:
    """A deliberate first Insert tap at ``t0``; True when the phantoms after it completed the Insert."""
    replay = Replay()
    replay.fill("hello", t0 - 3.0)
    replay.tap(t0, "insert")
    assert replay.m.counts["insert_start"] == 0
    feed(replay, taps, t0 + 1e-6, t0 + GUARD_MAX_S + 1.0)
    return replay.m.counts["insert_start"] > 0


def send_trial(taps: list[Tap], t0: float) -> bool:
    """A deliberate Insert that completes about ``t0``; True when the phantoms in the ``SEND_WINDOW_S`` after it began a
    Send."""
    replay = Replay()
    replay.fill("hello", t0 - 6.0)
    for i in range(INSERT_TAPS):
        replay.tap(t0 - 2.0 + i * 0.5, "insert")
    assert replay.m.counts["insert_start"] == 1
    done = replay.drain()
    assert replay.m.counts["insert_done"] == 1 and replay.m.last_insert is not None and done <= t0 + 3.0
    feed(replay, taps, max(done, t0), max(done, t0) + SEND_WINDOW_S + 0.5)
    return replay.m.counts["send_start"] > 0


def completions(
    taps: list[Tap], trial: Callable[[list[Tap], float], bool], seed: int, span: tuple[float, float]
) -> int:
    """How many of ``TRIALS`` random starting moments in ``span`` end in the accident ``trial`` looks for."""
    rng = random.Random(seed)
    lo, hi = span
    return sum(trial(taps, rng.uniform(lo, hi)) for _ in range(TRIALS))


# ----------------------------------------------------------------------------------------------- the machinery


def test_the_replay_counts_a_completion_when_three_insert_taps_follow_and_not_when_a_key_breaks_them() -> None:
    assert insert_trial([(10.5, "insert", ""), (11.0, "insert", "")], 10.0)
    assert not insert_trial([(10.5, "insert", ""), (10.8, "char", "e"), (11.0, "insert", "")], 10.0)
    assert not insert_trial([(10.5, "insert", ""), (10.55, "insert", ""), (10.6, "insert", "")], 10.0)  # bounces
    assert not insert_trial([(10.5, "insert", "")], 10.0)
    assert not insert_trial([(10.5, "insert", ""), (10.0 + GUARD_MAX_S + 0.5, "insert", "")], 10.0)  # too slow


def test_the_replay_counts_a_send_only_after_a_completed_insert_and_inside_its_window() -> None:
    def sends(t_first: float, kinds: tuple[KeyKind, ...] = ("enter",) * SEND_TAPS) -> list[Tap]:
        return [(t_first + i * 0.5, kind, "x" if kind == "char" else "") for i, kind in enumerate(kinds)]

    assert send_trial(sends(20.0), 18.5)  # the Insert completes, its text is typed, and then three Send taps
    assert not send_trial(sends(20.0)[:2], 18.5)
    assert not send_trial(sends(28.5), 18.5)  # the first Send tap comes after the 10 s
    assert not send_trial(sends(20.0, ("enter", "char", "enter", "enter")), 18.5)  # a letter takes Send away


@pytest.mark.parametrize(
    ("who", "key"), [(("right", 3), "insert"), (("right", 0), "insert"), (("left", 2), "enter"), (("left", 0), "enter")]
)
def test_a_parked_hand_has_the_named_finger_over_the_key(who: tuple[Side, int], key: str) -> None:
    scene = AirScene(seed=1, alpha=1.0)
    scene.arm()
    scene.run(0.5)
    scene.negative("still", seconds=20.0)
    scene.park(*who, key)
    scene.run(1.0)
    plane = scene.session.plane
    assert plane is not None
    target = scene.rig.key(key)
    hand = next(h for h in scene.session.hands if h.side == who[0])
    finger = next(f for f in hand.fingers if f.finger == who[1])
    u, v = plane.units(finger.aim)
    assert abs(u - (target.col + target.width / 2)) < 0.3 and abs(v - (target.row + 0.5)) < 0.3, (u, v)


def spans(begun: float, ended: float) -> tuple[tuple[float, float], tuple[float, float]]:
    """Where a trial may start so that its whole window lies inside the recording: (insert, send)."""
    return (begun + 1.0, ended - GUARD_MAX_S - 1.0), (begun + 1.0, ended - SEND_WINDOW_S - 1.0)


@pytest.mark.usefixtures("no_ladder")
@pytest.mark.parametrize(("who", "key"), [(("right", 3), "insert"), (("left", 2), "enter")])
def test_a_reduced_parked_run_has_phantoms_to_replay_and_the_replays_count_their_accidents(
    who: tuple[Side, int], key: str
) -> None:
    """One seed of a fidgeting hand, 30 s: the machinery of the long run, not its verdict (the repro below is the
    verdict of one cell). The run must have made taps, else the replays below would be vacuous."""
    _, taps, begun, ended = parked_run("fidget", 0.001, 0, who, key, RUN_S)
    assert len(taps) >= 3, len(taps)
    by_insert, by_send = spans(begun, ended)
    for trial, span in ((insert_trial, by_insert), (send_trial, by_send)):
        done = completions(taps, trial, 0, span)
        assert 0 <= done <= TRIALS
    assert completions([], insert_trial, 0, by_insert) == 0 and completions([], send_trial, 0, by_send) == 0


@pytest.mark.usefixtures("no_ladder")
@pytest.mark.xfail(
    strict=False,
    reason=(
        "N48 not met by the detector as ported: a fidgeting hand with the right pinky on Insert makes the pinky's "
        "phantoms (about 20 a minute, a third of all its taps on that one key) and three in a row complete an Insert "
        "(seeds 0 and 2: 2 and 1 in 60 s; 15 in 24 min over 12 seeds). T1's detector or synth, or the design's bar."
    ),
)
@pytest.mark.parametrize("seed", [0, 2])
def test_n48_repro_a_fidgeting_hand_with_the_pinky_on_insert_completes_no_insert(seed: int) -> None:
    scene, _, _, _ = parked_run("fidget", 0.001, seed, ("right", 3), "insert", 60.0)
    assert scene.rig.counts["insert_start"] == 0 and scene.rig.desktop.key_calls == []


# --------------------------------------------------------------------------------------------- the long run (N48)

full_only = pytest.mark.skipif(not FULL, reason="the 12 x 120 s run of N48: set KB_FULL=1")


@full_only
@pytest.mark.usefixtures("no_ladder")
@pytest.mark.parametrize("sigma", NOISES)
@pytest.mark.parametrize("name", SCENARIOS)
@pytest.mark.parametrize("who", ON_INSERT, ids=lambda w: f"{w[0]}{w[1]}")
def test_n48_a_hand_parked_on_insert_completes_no_insert_and_phantoms_do_not_finish_a_deliberate_first_tap(
    who: tuple[Side, int], name: str, sigma: float
) -> None:
    for seed in SEEDS:
        scene, taps, begun, ended = parked_run(name, sigma, seed, who, "insert", RUN_S)
        assert scene.rig.counts["insert_start"] == 0 and scene.rig.desktop.key_calls == [], (seed, name, sigma)
        done = completions(taps, insert_trial, seed, spans(begun, ended)[0])
        assert done <= BAR * TRIALS, (seed, done, len(taps))


@full_only
@pytest.mark.usefixtures("no_ladder")
@pytest.mark.parametrize("sigma", NOISES)
@pytest.mark.parametrize("name", SCENARIOS)
@pytest.mark.parametrize("who", ON_SEND, ids=lambda w: f"{w[0]}{w[1]}")
def test_n48_a_hand_parked_on_send_does_not_send_after_a_random_completed_insert(
    who: tuple[Side, int], name: str, sigma: float
) -> None:
    done = trials = 0
    for seed in SEEDS:
        scene, taps, begun, ended = parked_run(name, sigma, seed, who, "enter", RUN_S)
        assert scene.rig.counts["insert_start"] == 0 and scene.rig.counts["send_start"] == 0, (seed, name, sigma)
        assert scene.rig.desktop.key_calls == []
        done += completions(taps, send_trial, seed, spans(begun, ended)[1])
        trials += TRIALS
    assert done <= BAR * trials, (done, trials)
