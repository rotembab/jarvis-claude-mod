"""X54, the session half: a legitimate user arms the prompted warm-up.

A closed-loop synthetic user watches the strip's prompt and taps the named finger 0.6 s (plus or minus 0.15 s) after it
is named; a tap that is not taken (a weak one) is repeated 1.2 s later. The press is scripted, so a tap is taken or
refused by the warm-up's own rules (depth, margin, aim, clean second); the real detector's recall, the coupling of
neighbouring fingers (the vetoed taps of the design's measurement) and the landmark noise are the end-to-end half of
X54, at the end of this file (``AirScene.warm``: the same user on ``AirTypist`` hands, the real tracker and
``AirTapPress``). ``ordinary`` taps are 0.30 deep; ``lazy`` ones 0.12 to 0.16, at the edge of the depth rule, and a
fifth of them are too weak to count.
"""

from __future__ import annotations

import os
import random
import statistics

import pytest

from jarvis_hands.keyboard.rig import AirScene, KbRig
from jarvis_hands.keyboard.types import Side

SEEDS = range(12)
REACTION_S = (0.45, 0.75)
RETAP_S = 1.2
WEAK_DEPTH = 0.04


def arm_as_a_user(rig: KbRig, rng: random.Random, style: str, limit_s: float = 90.0) -> float:
    """Frames until armed; returns the time from the start of the warm-up to arming."""
    begun = rig.t
    shown: tuple[Side, int] | None = None
    tap_at = float("inf")
    sent = False
    while not rig.session.armed and rig.closed is None and rig.t < begun + limit_s:
        prompt = rig.session.warmup_prompt
        if prompt != shown:
            shown = prompt
            sent = False
            tap_at = rig.t + rng.uniform(*REACTION_S) if prompt is not None else float("inf")
        if prompt is not None and rig.t + rig.dt / 2 >= tap_at:
            weak = style == "lazy" and rng.random() < 0.2
            depth = WEAK_DEPTH if weak else (0.30 if style == "ordinary" else rng.uniform(0.12, 0.16))
            rig.warm_tap(*prompt, depth=depth)
            tap_at = rig.t + RETAP_S  # a tap that is taken changes the prompt; one that is not is repeated
            sent = True
        rig.run(rig.dt)
    assert sent or rig.session.armed
    return rig.t - begun


@pytest.mark.parametrize("style", ["ordinary", "lazy"])
@pytest.mark.parametrize("sides", [("left", "right"), ("right",)], ids=["two_hands", "one_hand"])
def test_x54_a_legitimate_user_arms_within_ninety_seconds_in_every_run_without_a_restart(
    sides: tuple[Side, ...], style: str
) -> None:
    times = []
    for seed in SEEDS:
        rig = KbRig(commit="review", sides=sides, keep_views=False)
        rig.place()
        times.append(arm_as_a_user(rig, random.Random(f"{style}/{seed}"), style))
        assert rig.session.armed and rig.counts["warmup_restart"] == 0, (style, seed)
        assert rig.desktop.key_calls == [] and rig.box == ""
    bar = 20.0 if len(sides) == 2 else 10.0
    if style == "ordinary":
        assert statistics.median(times) <= bar, times
    assert max(times) < 90.0


def test_x54_a_user_who_taps_the_right_finger_early_is_not_punished_and_waits_for_the_next_name() -> None:
    rig = KbRig(commit="review", keep_views=False)
    rig.place()
    rig.run(1.2)
    rig.warm_tap("right", 0)
    rig.run(0.1)
    rig.warm_tap("left", 0)  # tapped before the strip named it: early, not a stray
    rig.run(0.5)
    assert rig.counts["warmup_accepted"] == 1 and rig.counts["warmup_stray"] == 0 and rig.counts["warmup_restart"] == 0
    rig.run(1.0)
    rig.warm_tap("left", 0)
    rig.run(0.3)
    assert rig.counts["warmup_accepted"] == 2


# ------------------------------------------------------------------------------ the end-to-end half: real detector


@pytest.mark.parametrize("style", ["ordinary", "lazy"])
@pytest.mark.parametrize("sides", [("left", "right"), ("right",)], ids=["two_hands", "one_hand"])
def test_x54_a_legitimate_user_arms_the_real_detector_within_ninety_seconds_in_every_run(
    sides: tuple[Side, ...], style: str, record_property: pytest.RecordProperty
) -> None:
    """Landmark noise 0.001, 30 fps, 12 seeds: the user taps the finger the strip names 0.45 to 0.75 s after it is
    named and again 1.2 s after a tap that was not taken (``AirScene.warm``); a neighbour's coupling is in the stream.
    Every run arms with no restart; the median time is the bar of the design (measured 16.2 s with two hands and 7.5 s
    with one for ordinary taps)."""
    times = []
    for seed in SEEDS:
        scene = AirScene(seed=seed, style=style, sides=sides, keep_views=False, alpha=1.0)
        scene.place()
        begun = scene.rig.t
        assert scene.warm(), (style, seed, dict(scene.rig.counts))
        times.append(scene.rig.t - begun)
        assert scene.rig.counts["warmup_restart"] == 0, (style, seed)
        assert scene.rig.desktop.key_calls == [] and scene.rig.box == ""
    record_property("arming_times_s", [round(x, 1) for x in times])
    assert max(times) < 90.0
    if style == "ordinary":
        assert statistics.median(times) <= (20.0 if len(sides) == 2 else 10.0), times


@pytest.mark.skipif(os.environ.get("KB_FULL") != "1", reason="a measurement, not a bar: set KB_FULL=1")
@pytest.mark.parametrize("style", ["ordinary", "lazy"])
@pytest.mark.parametrize("sides", [("left", "right"), ("right",)], ids=["two_hands", "one_hand"])
def test_x54_at_landmark_noise_0_002_the_arming_times_are_reported_not_asserted(
    sides: tuple[Side, ...], style: str, record_property: pytest.RecordProperty
) -> None:
    """The design reports the 0.002 row and does not bar it: at that noise the stray taps of the detector restart a
    two-hand warm-up again and again (measured: 8 of 12 ordinary runs and 5 of 12 lazy ones did not arm in 90 s, with
    7 to 10 restarts each), and the ladder does not step in. Nothing may be typed meanwhile."""
    armed, times, restarts = 0, [], []
    for seed in SEEDS:
        scene = AirScene(seed=seed, style=style, sides=sides, sigma=0.002, keep_views=False, alpha=1.0)
        scene.place()
        begun = scene.rig.t
        if scene.warm():
            armed += 1
            times.append(scene.rig.t - begun)
        restarts.append(scene.rig.counts["warmup_restart"])
        assert scene.rig.desktop.key_calls == [] and scene.rig.box == ""
    record_property("armed_of_12", armed)
    record_property("arming_times_s", [round(x, 1) for x in times])
    record_property("restarts", restarts)
