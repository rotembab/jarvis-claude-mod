"""X54, the session half: a legitimate user arms the prompted warm-up.

A closed-loop synthetic user watches the strip's prompt and taps the named finger 0.6 s (plus or minus 0.15 s) after it
is named; a tap that is not taken (a weak one) is repeated 1.2 s later. The press is scripted, so a tap is taken or
refused by the warm-up's own rules (depth, margin, aim, clean second); the real detector's recall, the coupling of
neighbouring fingers (the vetoed taps of the design's measurement) and the landmark noise are the end-to-end half of
X54 and are not in this file. ``ordinary`` taps are 0.30 deep; ``lazy`` ones 0.12 to 0.16, at the edge of the depth
rule, and a fifth of them are too weak to count.
"""

from __future__ import annotations

import random
import statistics

import pytest

from jarvis_hands.keyboard.rig import KbRig
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
