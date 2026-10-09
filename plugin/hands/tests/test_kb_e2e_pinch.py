"""The pinch press end to end: a synthetic pinch typist, the real tracker and ``PinchPress``, the session, the sink and
``FakeDesktop`` (DESIGN 5.0 to 5.3, 5.10, 5.12).

``test_kb_scenarios.py`` runs the N families through the tracker and the press alone and asks what the press fires. The
rows here ask what reaches the desktop: the same streams through the whole session, whose gates (the slow hold, the
queue, the holds, the storm breaker) and the sink sit between a press and a window, plus the rows that need a real
press to mean anything (S1 the runaway at the twelfth key, S8 foreign input, B6 other frame rates). The fuzz of S3
at session level is in ``test_kb_e2e_fuzz.py``.

The stream builders (the talking hands, a hand that appears pinching, a hand lost or stalled in mid-pinch) are those of
``test_kb_scenarios.py`` written against ``PinchScene``; the two files agree on what a stream is, not on code.
"""

from __future__ import annotations

import os
from collections.abc import Sequence

import numpy as np
import pytest

from jarvis_hands.keyboard import synth as sy
from jarvis_hands.keyboard.rig import ASPECT, PinchScene
from jarvis_hands.keyboard.tuning import Tuning
from jarvis_hands.keyboard.types import Side
from jarvis_hands.landmarks import Frame

from scripted import Blend

#: The design's rows are N1: 5 minutes, N15: 3, N9: 2. The session closes ``idle`` after ``NO_KEY_CLOSE_S`` = 300 s
#: without a key, however the hands move, so N1 here is the 280 s that the session can see. A still hand makes a
#: phantom press at a rate, not at a moment, and ``test_kb_scenarios`` runs the same detector over the same hover: the
#: default run here is 100 s and 60 s of it, ``KB_FULL=1`` the design's minutes.
FULL = os.environ.get("KB_FULL") == "1"
N1_S, N9_S, N15_S = (280.0 if FULL else 100.0), 120.0, (180.0 if FULL else 60.0)


def armed(commit: str = "direct", **kw: object) -> PinchScene:
    """A pinch session that has placed, warmed up and left the sink's 0.3 s window."""
    scene = PinchScene(commit=commit, **kw)  # type: ignore[arg-type]
    scene.arm()
    return scene


def silent(scene: PinchScene) -> None:
    """No key was delivered and nothing reached the desktop: the invariant of every N row."""
    rig = scene.rig
    assert rig.counts["keys"] == 0 and rig.desktop.key_calls == [], (dict(rig.counts), rig.desktop.key_calls[:5])


def image_at(anchor: tuple[float, float]) -> tuple[float, float]:
    """A hand's anchor in pose space as the image fractions ``SynthHand`` takes."""
    return (anchor[0], anchor[1] / ASPECT)


def hand_of(scene: PinchScene, side: Side, anchor: tuple[float, float], **kw: object) -> sy.SynthHand:
    typist = scene.typist
    return sy.SynthHand(
        side, image_at(anchor), typist.posture, jitter=typist.jitter, z_noise=typist.z_noise, rng=scene.rng, **kw
    )  # type: ignore[arg-type]


def smooth_k(x: float) -> float:
    return x * x * (3.0 - 2.0 * x)


# ----------------------------------------------------------------------------------------------------- N1, N15


#: The postures the long hover rows run on. ``relaxed`` is left out of those (a five-minute hover of a third posture
#: adds time and no case); the warm-up below arms on all three.
POSTURES = ["rest", "straight"]


@pytest.mark.parametrize("posture", POSTURES)
def test_n1_both_hands_hovering_with_jitter_for_five_minutes_press_nothing(posture: sy.Posture) -> None:
    scene = armed(posture=posture, seed=21)
    scene.typist.jitter = 0.003
    scene.feed(scene.typist.hover(N1_S))
    silent(scene)
    assert scene.rig.closed is None


@pytest.mark.parametrize("posture", POSTURES)
def test_n15_a_hover_with_jitter_of_six_thousandths_for_three_minutes_presses_nothing(posture: sy.Posture) -> None:
    scene = armed(posture=posture, seed=42)
    scene.typist.jitter = 0.006
    scene.feed(scene.typist.hover(N15_S))
    silent(scene)
    assert scene.press.rejects.get("closing_timeout", 0) <= 3


@pytest.mark.parametrize("seed", [21, 1])
@pytest.mark.parametrize("posture", ["rest", "straight", "relaxed"])
def test_the_pinch_warmup_arms_a_session_on_every_posture(posture: sy.Posture, seed: int) -> None:
    """The relaxed hand is the one a user who does not hold his fingers up has: the neighbours of a pinching finger
    stay at 0.3 to 0.5 of the thumb. An absolute floor on them (OTHER 0.45) refused every pinch of that hand but the
    first, and the pinch method, the air method's fallback, could not be used at all."""
    scene = PinchScene(commit="direct", posture=posture, seed=seed)
    scene.arm()
    silent(scene)


# --------------------------------------------------------------------------------------------------------- N7


@pytest.mark.parametrize("finger", range(4))
def test_n7_a_hand_that_appears_already_pinching_types_nothing_and_exactly_one_key_once_it_has_opened(
    finger: int,
) -> None:
    scene = armed(seed=30 + finger)
    typist, script = scene.typist, scene.script
    key = scene.rig.key("jkl'"[finger])
    scene.feed(typist.hover(0.3))
    # the right hand is out of view for longer than the tracker holds a hand, then comes back with its thumb on a finger
    scene.feed(typist.hover(0.5, hands=("left",)))
    left, right = typist.home("left"), typist.home("right")
    steps = [1.0] * 30 + [2 / 3, 1 / 3]  # a second pinching, then the thumb leaves
    scene.feed(
        [script.frame(hand_of(scene, "left", left), hand_of(scene, "right", right, pinch=(finger, k))) for k in steps]
    )
    scene.feed(typist.hover(0.5))
    silent(scene)
    scene.press_key(key)
    scene.feed(scene.typist.hover(0.5))
    assert scene.rig.counts["keys"] == 1 and scene.rig.typed == key.en
    assert scene.rig.desktop.key_calls == [("char", key.en)]


# --------------------------------------------------------------------------------------------------------- N9

#: Gestures of a talking hand (``point`` is left out: going from ``two`` to ``point`` curls the middle finger onto the
#: thumb, which is a middle-finger pinch by every measure the detector has).
GESTURES = ("palm", "fist", "two", "hover", "thumb_up")


def talking(scene: PinchScene, seconds: float, rng: np.random.Generator, poses: Sequence[str]) -> list[Frame]:
    """Both hands moving about the keys and changing between ``poses`` at random, as people do while they talk."""
    n = scene.script.count(seconds)
    tracks: dict[Side, list[Blend]] = {}
    for side in ("left", "right"):
        here = scene.typist.home(side)
        pose = "hover"
        blends: list[Blend] = []
        while len(blends) < n:
            target = poses[int(rng.integers(0, len(poses)))]
            there = (here[0] + rng.uniform(-0.12, 0.12), here[1] + rng.uniform(-0.08, 0.08))
            change = int(rng.integers(6, 22))
            for j in range(1, change + 1):
                k, far = smooth_k(j / change), smooth_k(j / change)
                at = (here[0] + far * (there[0] - here[0]), here[1] + far * (there[1] - here[1]))
                thumb = None if rng.random() < 0.7 else float(rng.uniform(0.0, 1.0))
                blends.append(Blend(pose, target, k, image_at(at), side, thumb, {"jitter": 0.0015, "rng": rng}))  # type: ignore[arg-type]
            for _ in range(int(rng.integers(3, 25))):
                blends.append(Blend(pose, target, 1.0, image_at(there), side, None, {"jitter": 0.0015, "rng": rng}))  # type: ignore[arg-type]
            here, pose = there, target
        tracks[side] = blends[:n]
    return [scene.script.frame(tracks["left"][i], tracks["right"][i]) for i in range(n)]


def test_n9_two_minutes_of_talking_with_the_hands_deliver_no_key() -> None:
    scene = armed(seed=33)
    scene.feed(talking(scene, N9_S, np.random.default_rng(33), GESTURES))
    silent(scene)
    assert scene.rig.closed in (None, "fists")  # two fists held a second are the session's own close


# ------------------------------------------------------------------------------------------------- N10, N11


def mid_pinch(scene: PinchScene, char: str, *, held: int = 24) -> tuple[list[Frame], int, int]:
    """The frames of a long press, the index of the first frame of its hold and of its first closing frame."""
    frames = scene.typist.press(scene.rig.key(char), closing=5, held=held, opening=3)
    hold = len(frames) - 3 - held
    return frames, hold, hold - 5


@pytest.mark.parametrize("lost_s", [0.1, 0.5])
@pytest.mark.parametrize("after_commit", [True, False])
def test_n10_a_hand_lost_in_the_middle_of_a_pinch_gives_no_second_key(lost_s: float, after_commit: bool) -> None:
    scene = armed(seed=34)
    scene.feed(scene.typist.hover(0.3))
    frames, hold, closing = mid_pinch(scene, "j")
    first = (hold if after_commit else closing) + 2
    lost = scene.script.count(lost_s)
    gone = [
        Frame(f.t, tuple(h for h in f.hands if h.handedness != "right"), f.width, f.height)
        for f in frames[first : first + lost]
    ]
    scene.feed(frames[:first] + gone + frames[first + lost :])
    scene.feed(scene.typist.hover(0.5, scene.sides))
    kept = after_commit or lost_s < scene.tuning.hand_hold_s
    assert scene.rig.typed == ("j" if kept else "") and scene.rig.counts["keys"] == int(kept)


@pytest.mark.parametrize("hold_s", [None, 0.45])
@pytest.mark.parametrize("after_commit", [True, False])
def test_n11_a_tracking_stall_in_the_middle_of_a_pinch_gives_no_second_key(
    hold_s: float | None, after_commit: bool
) -> None:
    scene = armed(seed=35, tuning=Tuning() if hold_s is None else Tuning(hand_hold_s=hold_s))
    scene.feed(scene.typist.hover(0.3))
    frames, hold, closing = mid_pinch(scene, "j")
    first = (hold if after_commit else closing) + 2
    stall = scene.script.count(0.4)
    scene.feed(frames[:first] + frames[first + stall :])
    scene.feed(scene.typist.hover(0.5, scene.sides))
    assert scene.rig.typed == ("j" if after_commit else "") and scene.rig.counts["keys"] == int(after_commit)


# ------------------------------------------------------------------------------------------ S1, B6, S8


def test_s1_four_fingers_rolling_at_eight_keys_a_second_close_runaway_at_the_twelfth_key() -> None:
    scene = armed(seed=3)
    scene.feed(scene.typist.type("asdf" * 5, gap_s=0.125))
    scene.feed(scene.typist.hover(2.0))
    rig = scene.rig
    assert rig.closed == "runaway" and rig.counts["keys"] == 11 and len(rig.typed) == 11
    typed = rig.typed
    scene.feed(scene.typist.type("jkl", gap_s=0.4))  # nothing resumes
    assert rig.typed == typed


@pytest.mark.parametrize("fps", [15.0, 60.0])
def test_b6_pinch_typing_at_other_frame_rates(fps: float) -> None:
    scene = armed(seed=5, fps=fps)
    scene.type("hello world", gap_s=0.4)
    assert scene.rig.typed == "hello world" and scene.rig.closed is None and scene.rig.view.hold is None


def test_b6_below_ten_frames_a_second_the_slow_hold_stops_a_real_pinch_and_above_twelve_clears_it() -> None:
    scene = armed(seed=6)
    rig = scene.rig
    scene.script.fps = 8.0  # the camera slows down: frames come 1/8 s apart from here
    scene.feed(scene.typist.hover(2.0))
    assert rig.view.hold == "slow"
    scene.press_key("a")
    scene.feed(scene.typist.hover(1.0))
    assert rig.typed == "" and rig.counts["held"] >= 1
    scene.script.fps = 13.0
    scene.feed(scene.typist.hover(2.0))
    assert rig.view.hold is None
    scene.press_key("b")
    scene.feed(scene.typist.hover(0.5))
    assert rig.typed == "b"


def test_s8_foreign_input_stops_keys_for_a_second_and_a_half_and_a_pinch_begun_in_the_hold_never_types() -> None:
    scene = armed(seed=8)
    rig = scene.rig
    scene.type("ab")
    assert rig.typed == "ab"
    rig.desktop.user_typed()
    begun = rig.t
    scene.press_key("j", closing=5, held=60, opening=3)  # a pinch that begins in the hold and outlasts it
    scene.feed(scene.typist.hover(0.5))
    assert rig.t - begun > 2.0 and rig.typed == "ab", rig.typed
    assert rig.counts["keys"] == 2
    scene.press_key("k")
    scene.feed(scene.typist.hover(0.5))
    assert rig.typed == "abk"  # typing resumes after the hold, every finger latched until it opened


def test_s8_a_pinch_that_is_only_begun_before_the_hold_is_not_finished_by_it() -> None:
    scene = armed(seed=9)
    rig = scene.rig
    frames, _, closing = mid_pinch(scene, "j", held=30)
    cut = closing + 1
    scene.feed(frames[:cut])
    rig.desktop.user_typed()
    scene.feed(frames[cut:])
    scene.feed(scene.typist.hover(2.0))
    assert rig.typed == ""


# ------------------------------------------------------------------------------- pinch typing in the review box


def test_review_mode_pinch_types_into_the_box_and_three_insert_pinches_type_it_into_the_window() -> None:
    scene = armed("review", seed=11)
    rig = scene.rig
    scene.type("hello")
    assert rig.box == "hello" and rig.desktop.key_calls == []
    insert = rig.key("insert")
    scene.feed(scene.typist.tap_n(insert, 3, 0.5))
    scene.feed(scene.typist.hover(0.5))
    done = scene.rig.t + 15.0
    while rig.counts["insert_done"] == 0 and rig.t < done:
        scene.feed(scene.typist.hover(0.5))
    assert rig.typed == "hello" and rig.box == "" and rig.closed is None
    assert rig.counts["insert_start"] == 1 and rig.counts["insert_done"] == 1


def test_review_mode_two_insert_pinches_type_nothing_into_the_window() -> None:
    scene = armed("review", seed=12)
    rig = scene.rig
    scene.type("hi")
    scene.feed(scene.typist.tap_n(rig.key("insert"), 2, 0.5))
    scene.feed(scene.typist.hover(8.0))
    assert rig.typed == "" and rig.desktop.key_calls == [] and rig.box == "hi"
