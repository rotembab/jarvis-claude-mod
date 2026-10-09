"""X53, the session half: phantoms must not arm the prompted warm-up.

Seeded streams of taps that nobody meant (a still hand, a hand opening and closing, a hand reaching, talking with the
hands) are fed to the session in its warm-up through a scripted press, frame by frame for 90 s, with one hand and with
two. A tap of a stream passes the gates a real detector would have passed (depth, margin, an aim near the finger's
own key), and every burst is preceded by the reject counters (``coherence``, ``tremor``, ``speed``) that a real
detector raises on the movement that makes it, because the warm-up reads their total before each update (its clean
second). So what is under test is the session's wiring of the warm-up's own rules: the named finger only, once per
naming, no strays, a clean second before it.

What a scripted press cannot say is how many phantoms a real detector makes of a still hand or a fidgeting finger:
that is the end-to-end half of X53, at the end of this file (the repo's negative scenarios as landmarks, through the
real tracker, ``AirTapPress`` in calibrating mode, ``Warmup`` and the session). Note what the scripted half means:
phantoms that pass every gate and never raise a counter, one hand, at a rate high enough to hit the named finger by
chance, can arm a warm-up of four fingers (twelve one-hand runs of one tap every 1.1 to 2 s armed some of them when
this was tried); nothing in the warm-up itself tells such a tap from the user, so the scripted half is not a proof for
them.

The streams are 90 s of frames each (a second or two of CPU for each cell, and 48 cells made 80 s of a suite that has
to run on a Windows runner), so by default only a diagonal of the grid runs: seed 0 and, for each scenario, one hand
count and one landmark noise, varied from one scenario to the next. ``KB_FULL=1`` runs every cell of seeds 0 to 11.
"""

from __future__ import annotations

import os
import random
from collections.abc import Callable, Iterator

import pytest

from jarvis_hands.keyboard.rig import AirScene, KbRig
from jarvis_hands.keyboard.types import Side

#: ("tap", t, side, finger, du, dv) or ("reject", t, counter name); t counts from the start of the warm-up.
Item = tuple[object, ...]
Stream = Callable[[random.Random, tuple[Side, ...], float], Iterator[Item]]

FULL = os.environ.get("KB_FULL") == "1"
SEEDS = range(12) if FULL else range(1)
RUN_S = 90.0
ONE = ("right",)
TWO = ("left", "right")


def fingers_of(sides: tuple[Side, ...]) -> list[tuple[Side, int]]:
    return [(side, f) for side in sides for f in range(4)]


def jitter(rng: random.Random, spread: float = 0.3) -> tuple[float, float]:
    return rng.gauss(0.0, spread), rng.gauss(0.0, spread)


def still(rng: random.Random, sides: tuple[Side, ...], total: float) -> Iterator[Item]:
    """The rare false tap of a hand that is not moving: three a minute, any finger, near its own key."""
    t = rng.uniform(0.5, 3.0)
    while t < total:
        side, finger = rng.choice(fingers_of(sides))
        yield ("tap", t, side, finger, *jitter(rng))
        t += rng.expovariate(3 / 60)


def open_close(rng: random.Random, sides: tuple[Side, ...], total: float) -> Iterator[Item]:
    """A hand opening and closing: all four fingers go down within 0.2 s, every three seconds."""
    t = rng.uniform(1.0, 3.0)
    while t < total:
        side = rng.choice(sides)
        order = list(range(4))
        rng.shuffle(order)
        yield ("reject", t - 0.2, "coherence")
        for k, finger in enumerate(order):
            yield ("tap", t + 0.05 * k, side, finger, *jitter(rng, 0.2))
        t += rng.uniform(2.5, 3.5)


def reach(rng: random.Random, sides: tuple[Side, ...], total: float) -> Iterator[Item]:
    """A hand reaching across the keyboard: the fingers are three or four keys from their own, and it is moving."""
    t = rng.uniform(1.0, 3.0)
    while t < total:
        side, finger = rng.choice(fingers_of(sides))
        far = rng.choice([-1, 1]) * rng.uniform(3.0, 4.0)
        yield ("reject", t - 0.2, "speed")
        yield ("tap", t, side, finger, far, rng.choice([-1.0, 0.0, 1.0]))
        t += rng.uniform(0.4, 2.5)


def talk_hands(rng: random.Random, sides: tuple[Side, ...], total: float) -> Iterator[Item]:
    """Gesturing while talking: bursts of three to six taps by any fingers within 0.4 s, every few seconds."""
    t = rng.uniform(1.0, 3.0)
    while t < total:
        yield ("reject", t - 0.2, "coherence")
        for _ in range(rng.randint(3, 6)):
            side, finger = rng.choice(fingers_of(sides))
            yield ("tap", t + rng.uniform(0.0, 0.4), side, finger, *jitter(rng, 0.4))
        t += rng.uniform(2.0, 5.0)


def talking(rng: random.Random, sides: tuple[Side, ...], total: float) -> Iterator[Item]:
    """One finger drumming: five taps a second for a second, then a pause."""
    t = rng.uniform(1.0, 3.0)
    while t < total:
        side, finger = rng.choice(fingers_of(sides))
        yield ("reject", t - 0.2, "tremor")
        for k in range(5):
            yield ("tap", t + 0.2 * k, side, finger, *jitter(rng, 0.2))
        t += rng.uniform(3.0, 5.0)


SCENARIOS: dict[str, Stream] = {
    "still": still,
    "open_close": open_close,
    "reach": reach,
    "talk_hands": talk_hands,
    "talking": talking,
}


def play(rig: KbRig, items: list[Item], begun: float, seconds: float, rng: random.Random) -> None:
    """Script the taps, apply the reject counters at their times, run frame by frame; the session must not arm."""
    rejects = sorted((float(item[1]), str(item[2])) for item in items if item[0] == "reject")
    for item in items:
        if item[0] == "tap":
            _, t, side, finger, du, dv = item
            rig.finger_tap(
                begun + float(t),  # type: ignore[arg-type]
                side,  # type: ignore[arg-type]
                finger,  # type: ignore[arg-type]
                du=float(du),  # type: ignore[arg-type]
                dv=float(dv),  # type: ignore[arg-type]
                depth=rng.uniform(0.12, 0.45),
                margin=rng.uniform(0.5, 1.5),
            )
    end = begun + seconds
    while rig.closed is None and rig.t < end:
        while rejects and begun + rejects[0][0] <= rig.t + rig.dt:
            rig.press.reject(rejects.pop(0)[1])
        rig.run(rig.dt)
        assert not rig.session.armed


#: (scenario, hands) of the scripted half by default: each scenario once, with one hand or with two.
SCRIPTED_DIAGONAL = [("still", TWO), ("open_close", ONE), ("reach", TWO), ("talk_hands", ONE), ("talking", TWO)]
SCRIPTED_CELLS = [(name, seed, sides) for sides in (TWO, ONE) for seed in SEEDS for name in SCENARIOS]


@pytest.mark.parametrize(
    ("name", "seed", "sides"),
    SCRIPTED_CELLS if FULL else [(name, 0, sides) for name, sides in SCRIPTED_DIAGONAL],
    ids=lambda v: "two_hands" if v == TWO else "one_hand" if v == ONE else None,
)
def test_x53_no_stream_of_phantoms_arms_the_warmup_in_ninety_seconds(
    name: str, seed: int, sides: tuple[Side, ...]
) -> None:
    rig = KbRig(commit="review", sides=sides, keep_views=False)
    rig.place()
    rng = random.Random(f"{name}/{seed}/{len(sides)}")
    items = list(SCENARIOS[name](rng, sides, RUN_S))
    play(rig, items, rig.t, RUN_S + 5.0, rng)
    # the 90 s of the warm-up ran out: "idle" for a stream that made no tap, "air_unreliable" for one that did
    assert not rig.session.armed and rig.closed in ("idle", "air_unreliable")
    assert rig.desktop.key_calls == [] and rig.box == ""


def test_x53_a_chord_of_all_four_fingers_never_completes_it_and_restarts_it() -> None:
    rig = KbRig(commit="review", keep_views=False)
    rig.place()
    begun = rig.t
    for k in range(20):
        for finger in range(4):
            for side in ("left", "right"):
                rig.finger_tap(begun + 2.0 + 3.0 * k, side, finger)  # type: ignore[arg-type]
    rig.run(65.0)
    assert not rig.session.armed and rig.counts["warmup_restart"] >= 1


def test_x53_a_tap_in_the_second_after_a_reject_counter_rose_is_not_taken() -> None:
    rig = KbRig(commit="review", keep_views=False)
    rig.place()
    rig.run(1.5)
    assert rig.session.warmup_prompt == ("right", 0)
    rig.press.reject("coherence")
    rig.run(rig.dt)
    rig.warm_tap("right", 0)
    rig.run(0.3)
    assert rig.counts["warmup_accepted"] == 0 and rig.counts["warmup_unclean"] == 1
    rig.run(1.0)
    rig.warm_tap("right", 0)
    rig.run(0.3)
    assert rig.counts["warmup_accepted"] == 1


# ------------------------------------------------------------------------------ the end-to-end half: real detector

#: The negative scenarios of ``synth.scenario`` that X53 names (``AirHand.scenario`` of the study).
NEGATIVES = ("still", "open_close", "reach", "talk_hands", "talking", "fidget")
NOISES = (0.001, 0.002)


#: (scenario, noise, hands) of the real-detector half by default: each scenario once, the fidget (the one the study
#: could not tell from tapping) with two hands at the higher noise.
REAL_DIAGONAL = [
    ("still", 0.001, TWO),
    ("open_close", 0.002, ONE),
    ("reach", 0.002, TWO),
    ("talk_hands", 0.001, ONE),
    ("talking", 0.001, TWO),
    ("fidget", 0.002, TWO),
]
REAL_CELLS = [
    (name, seed, sigma, sides) for sides in (TWO, ONE) for sigma in NOISES for seed in SEEDS for name in NEGATIVES
]


@pytest.mark.parametrize(
    ("name", "seed", "sigma", "sides"),
    REAL_CELLS if FULL else [(name, 0, sigma, sides) for name, sigma, sides in REAL_DIAGONAL],
    ids=lambda v: "two_hands" if v == TWO else "one_hand" if v == ONE else None,
)
def test_x53_hands_that_mean_nothing_do_not_arm_the_warmup_of_the_real_detector(
    name: str, seed: int, sigma: float, sides: tuple[Side, ...]
) -> None:
    """The hands are placed as a still pair, then become the scenario for the 90 s of the warm-up. Every frame goes
    through the real tracker, ``AirTapPress(calibrating=True)`` and ``Warmup`` exactly as the session wires them (the
    reject total is read before each update); the session may close ``idle`` at the end of its 90 s or
    ``air_unreliable`` when the ladder finds the stream unusable, and it must never arm."""
    scene = AirScene(seed=100 * seed + len(sides), sigma=sigma, sides=sides, keep_views=False, alpha=1.0)
    scene.place()
    assert scene.session.phase == "warmup"
    scene.negative(name, sigma=sigma, seconds=RUN_S + 10.0)
    rig = scene.rig
    scene.run_until(lambda: rig.session.armed, RUN_S + 5.0)
    assert not rig.session.armed and rig.closed in ("idle", "air_unreliable")
    assert rig.desktop.key_calls == [] and rig.box == ""
