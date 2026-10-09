"""The hand tracker of the keyboard: U3, U4, U5, X1 and the levelling of A8 (DESIGN-KEYBOARD.md 2.1, 2.2, 2.12.1).

The hands here are built from the repo's frozen ``synthetic`` module and from crafted landmark sets, never from
``keyboard.synth``, so a slip in the keyboard's own typist cannot agree with a slip in the tracker.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from jarvis_hands import poses
from jarvis_hands import synthetic as syn
from jarvis_hands.keyboard.hands import HandTracker
from jarvis_hands.keyboard.tuning import Tuning
from jarvis_hands.landmarks import (
    INDEX_MCP,
    MIDDLE_MCP,
    THUMB_TIP,
    WRIST,
    Frame,
    HandObservation,
)

ASPECT = syn.HEIGHT / syn.WIDTH
REST = (12.0, 18.0, 9.0)
TUNING = Tuning()
FPS = 30.0


def posture(
    side: str = "right",
    at: tuple[float, float] = (0.5, 0.45),
    angles: tuple[float, float, float] = REST,
    *,
    roll: float = 0.0,
    scale: float = syn.SCALE,
    score: float = 0.95,
    label: str | None = None,
) -> HandObservation:
    """A hand with all four fingers at ``angles`` (MCP, PIP, DIP degrees), built from the frozen model."""
    fingers = {name: syn._finger(name, angles) for name in syn._MCP}
    world = syn._assemble(fingers, syn._thumb_tip("palm", fingers), side)  # type: ignore[arg-type]
    obs = syn.hand("palm", at, handedness=side, world=world, roll=roll, scale=scale, score=score)  # type: ignore[arg-type]
    if label is not None and label != side:
        obs = HandObservation(handedness=label, score=score, image=obs.image, world=obs.world)  # type: ignore[arg-type]
    return obs


def frames(*per_frame: tuple[HandObservation, ...], t0: float = 0.0, fps: float = FPS) -> list[Frame]:
    return [syn.frame(t0 + i / fps, *hands) for i, hands in enumerate(per_frame)]


def run(tracker: HandTracker, observed: list[Frame]) -> list[list]:
    return [tracker.update(f) for f in observed]


def crafted(reach: tuple[float, float, float, float], thumb_ratio: float = 1.0, palm: float = 0.1) -> HandObservation:
    """Landmarks set directly in pose space: the finger reaches and the thumb-to-index ratio are what is given."""
    p = np.zeros((21, 3))
    p[WRIST] = (0.5, 0.3, 0.0)
    mcp_offsets = {5: (-0.03, -0.095), 9: (0.0, -palm), 13: (0.03, -0.095), 17: (0.06, -0.085)}
    for finger, (mcp, offset) in enumerate(mcp_offsets.items()):
        p[mcp] = p[WRIST] + (*offset, 0.0)
        ray = p[mcp] - p[WRIST]
        for joint in range(1, 4):
            p[mcp + joint] = p[WRIST] + ray * (1.0 + (reach[finger] - 1.0) * joint / 3.0)
    p[THUMB_TIP] = p[5 + 3] + (-thumb_ratio * palm, 0.0, 0.0)
    p[1:4] = p[WRIST] + (-0.03, -0.03, 0.0)
    image = p / np.array([1.0, ASPECT, 1.0])
    return HandObservation("right", 0.95, image, image.copy())


def pitched(obs: HandObservation, degrees: float) -> HandObservation:
    """The hand turned toward (or away from) the camera about the horizontal line through its knuckles, in pose space.

    At 90 degrees the hand points straight at the lens: the wrist and the middle knuckle land on one picture point,
    and what is left of the axis between them is the depth difference of two landmarks, not a direction.
    """
    p = poses.pose_points(obs.image, ASPECT)
    anchor = (p[INDEX_MCP] + p[MIDDLE_MCP]) / 2
    q = p - anchor
    c, s = math.cos(math.radians(degrees)), math.sin(math.radians(degrees))
    turned = np.stack([q[:, 0], q[:, 1] * c - q[:, 2] * s, q[:, 1] * s + q[:, 2] * c], axis=1) + anchor
    image = turned / np.array([1.0, ASPECT, 1.0])
    return HandObservation(obs.handedness, obs.score, image, image.copy())


def axis_share(obs: HandObservation) -> float:
    """How much of the 3D palm (wrist to middle knuckle) the picture shows: the length of the 2D axis over the palm."""
    p = poses.pose_points(obs.image, ASPECT)
    return float(np.hypot(*(p[MIDDLE_MCP, :2] - p[WRIST, :2])) / np.linalg.norm(p[MIDDLE_MCP] - p[WRIST]))


# --------------------------------------------------------------------------------------------------- U3: identity


def test_u3_a_hand_that_stays_in_view_keeps_its_id() -> None:
    tracker = HandTracker(TUNING)
    out = run(tracker, frames(*[(posture(),)] * 12))
    assert len({s.hand for samples in out for s in samples}) == 1
    assert all(len(samples) == 1 for samples in out)


def test_u3_two_hands_keep_their_own_ids() -> None:
    tracker = HandTracker(TUNING)
    both = (posture("left", (0.3, 0.5)), posture("right", (0.7, 0.5)))
    out = run(tracker, frames(*[both] * 10))
    ids = {(s.side, s.hand) for samples in out for s in samples}
    assert len(ids) == 2
    assert {side for side, _ in ids} == {"left", "right"}
    assert all(len(samples) == 2 for samples in out)


def test_u3_labels_are_never_used_to_tell_the_hands_apart() -> None:
    """The engine overwrites handedness every frame; the track follows the wrist."""
    tracker = HandTracker(TUNING)
    per_frame = []
    for i in range(12):
        flip = i % 2 == 1
        per_frame.append(
            (
                posture("left", (0.3, 0.5), label="right" if flip else "left"),
                posture("right", (0.7, 0.5), label="left" if flip else "right"),
            )
        )
    out = run(tracker, frames(*per_frame))
    by_place: dict[bool, set[int]] = {True: set(), False: set()}
    for samples in out:
        for s in samples:
            by_place[bool(s.anchor[0] < 0.5)].add(s.hand)
    assert len(by_place[True]) == 1
    assert len(by_place[False]) == 1
    assert by_place[True] != by_place[False]
    # ... and the label of a sample is the newest one
    assert out[0][0].side == "left"
    assert out[1][0].side == "right"


def test_u3_fingers_are_found_by_landmark_not_by_label() -> None:
    same = run(HandTracker(TUNING), frames((posture("right", label="right"),)))[0][0]
    flipped = run(HandTracker(TUNING), frames((posture("right", label="left"),)))[0][0]
    assert flipped.side == "left"
    for a, b in zip(same.fingers, flipped.fingers, strict=True):
        np.testing.assert_allclose(a.aim, b.aim)
        assert (a.reach, a.ratio, a.lift) == (b.reach, b.ratio, b.lift)


def test_u3_the_association_radius_is_a_quarter_of_the_frame() -> None:
    near = run(HandTracker(TUNING), frames((posture(at=(0.40, 0.5)),), (posture(at=(0.40 + 0.24, 0.5)),)))
    far = run(HandTracker(TUNING), frames((posture(at=(0.40, 0.5)),), (posture(at=(0.40 + 0.26, 0.5)),)))
    assert near[0][0].hand == near[1][0].hand
    assert far[0][0].hand != far[1][0].hand


def test_u3_a_hand_gone_for_less_than_the_hold_time_comes_back_as_itself() -> None:
    tracker = HandTracker(TUNING)
    first = tracker.update(syn.frame(1.0, posture()))[0]
    assert tracker.update(syn.frame(1.05)) == []
    back = tracker.update(syn.frame(1.15, posture()))[0]
    assert back.hand == first.hand


def test_u3_a_hand_gone_longer_than_the_hold_time_comes_back_new() -> None:
    tracker = HandTracker(TUNING)
    first = tracker.update(syn.frame(1.0, posture()))[0]
    assert tracker.update(syn.frame(1.12)) == []
    back = tracker.update(syn.frame(1.25, posture()))[0]
    assert back.hand != first.hand


def test_u3_a_stall_between_two_frames_is_the_same_as_an_absence() -> None:
    tracker = HandTracker(TUNING)
    first = tracker.update(syn.frame(1.0, posture()))[0]
    assert tracker.update(syn.frame(1.40, posture()))[0].hand != first.hand


def test_u3_the_nearest_pair_is_matched_first() -> None:
    """Greedy by ascending distance: the second hand's track is not stolen by the first hand's nearer observation."""
    tracker = HandTracker(TUNING)
    a, b = tracker.update(syn.frame(0.0, posture(at=(0.40, 0.5)), posture(at=(0.60, 0.5))))
    # both move 0.05 to the right; each is nearest to its own track
    c, d = tracker.update(syn.frame(0.033, posture(at=(0.45, 0.5)), posture(at=(0.65, 0.5))))
    assert (c.hand, d.hand) == (a.hand, b.hand)
    # the observation order is not the track order
    e, f = tracker.update(syn.frame(0.066, posture(at=(0.70, 0.5)), posture(at=(0.50, 0.5))))
    assert (e.hand, f.hand) == (b.hand, a.hand)


def test_u3_a_third_hand_is_a_third_track() -> None:
    tracker = HandTracker(TUNING)
    out = tracker.update(syn.frame(0.0, posture(at=(0.2, 0.5)), posture(at=(0.5, 0.5)), posture(at=(0.8, 0.5))))
    assert len({s.hand for s in out}) == 3


def test_u3_new_ids_are_never_reused() -> None:
    tracker = HandTracker(TUNING)
    seen = []
    for k in range(5):
        seen.append(tracker.update(syn.frame(k * 1.0, posture()))[0].hand)  # each one a second after the last
    assert len(set(seen)) == len(seen)
    tracker.reset()
    assert tracker.update(syn.frame(10.0, posture()))[0].hand not in seen


def test_u3_the_palm_gate_ignores_hands_too_small_or_too_large() -> None:
    tracker = HandTracker(TUNING)
    assert tracker.update(syn.frame(0.0, posture(scale=400.0))) == []  # palm about 0.028 frame widths
    assert tracker.update(syn.frame(0.1, posture(scale=6000.0))) == []  # palm about 0.42
    assert len(tracker.update(syn.frame(0.2, posture(scale=1400.0)))) == 1
    sample = tracker.update(syn.frame(0.3, posture(scale=1400.0)))[0]
    assert 0.03 <= sample.palm <= 0.40
    assert math.isclose(sample.palm, 0.098, abs_tol=0.01)


def test_u3_garbage_landmarks_are_ignored_not_raised() -> None:
    tracker = HandTracker(TUNING)
    obs = posture()
    bad = obs.image.copy()
    bad[WRIST, 0] = np.nan
    assert tracker.update(syn.frame(0.0, HandObservation("right", 0.9, bad, obs.world))) == []
    bad[WRIST, 0] = np.inf
    assert tracker.update(syn.frame(0.1, HandObservation("right", 0.9, bad, obs.world))) == []
    assert tracker.update(syn.frame(0.2)) == []


def test_u3_time_that_runs_backwards_starts_the_hand_over() -> None:
    tracker = HandTracker(TUNING)
    first = tracker.update(syn.frame(5.0, posture()))[0]
    again = tracker.update(syn.frame(1.0, posture()))[0]
    assert again.hand != first.hand
    assert again.speed == 0.0


def test_u3_reset_forgets_every_track() -> None:
    tracker = HandTracker(TUNING)
    first = tracker.update(syn.frame(0.0, posture()))[0]
    tracker.reset()
    assert tracker.update(syn.frame(0.033, posture()))[0].hand != first.hand


def test_u3_the_aspect_of_the_frame_is_taken_from_the_frame() -> None:
    """The same hand in a 4:3 and a 16:9 frame has the same palm in frame widths (A15 at the tracker level)."""
    wide = syn.frame(0.0, syn.hand("palm", (0.5, 0.45), size=(1280, 720)), size=(1280, 720))
    boxy = syn.frame(0.0, syn.hand("palm", (0.5, 0.45), size=(1280, 960), scale=syn.SCALE), size=(1280, 960))
    a = HandTracker(TUNING).update(wide)[0]
    b = HandTracker(TUNING).update(boxy)[0]
    assert math.isclose(a.palm, b.palm, rel_tol=1e-6)
    for fa, fb in zip(a.fingers, b.fingers, strict=True):
        assert math.isclose(fa.lift, fb.lift, rel_tol=1e-6)


# --------------------------------------------------------------------------------------------------- U4: the ratio


@pytest.mark.parametrize("pose", ["palm", "fist", "pinch", "pinch_middle", "two", "point", "hover"])
def test_u4_the_ratio_is_the_poses_pinch_ratio_at_z_scale_one(pose: str) -> None:
    obs = syn.hand(pose)  # type: ignore[arg-type]
    sample = HandTracker(Tuning(z_scale=1.0)).update(syn.frame(0.0, obs))[0]
    points = poses.pose_points(obs.image, ASPECT)
    for finger, tip in enumerate((8, 12, 16, 20)):
        assert math.isclose(sample.fingers[finger].ratio, poses.pinch_ratio(points, tip), rel_tol=1e-9)
    assert sample.fingers[0].reach == pytest.approx(poses.finger_reach(points)[0])
    assert [f.reach for f in sample.fingers] == pytest.approx(list(poses.finger_reach(points)))
    assert math.isclose(sample.palm, poses.palm_size(points), rel_tol=1e-9)


def test_u4_z_scale_shrinks_the_depth_in_the_numerator_and_in_the_palm() -> None:
    obs = syn.hand("pinch")
    sample = HandTracker(Tuning(z_scale=0.5)).update(syn.frame(0.0, obs))[0]
    points = poses.pose_points(obs.image, ASPECT) * np.array([1.0, 1.0, 0.5])
    assert math.isclose(sample.fingers[0].ratio, poses.pinch_ratio(points, 8), rel_tol=1e-9)
    assert math.isclose(sample.palm, poses.palm_size(points), rel_tol=1e-9)


# --------------------------------------------------------------------------------------------------- U5: curled


def curled_sequence(reaches: list[float]) -> list[bool]:
    tracker = HandTracker(TUNING)
    return [
        tracker.update(syn.frame(i / FPS, crafted((r, 1.8, 1.8, 1.8))))[0].fingers[0].curled
        for i, r in enumerate(reaches)
    ]


def test_u5_a_finger_curls_below_one_ten_and_stays_curled_until_one_twenty() -> None:
    assert curled_sequence([1.30, 1.15, 1.09, 1.15, 1.19, 1.21, 1.15, 1.11, 1.09]) == [
        False,
        False,
        True,
        True,
        True,
        False,
        False,
        False,
        True,
    ]


def test_u5_the_thresholds_are_the_tunings() -> None:
    tight = Tuning(curled_enter=1.00, curled_leave=1.05)
    tracker = HandTracker(tight)
    reaches = [1.20, 1.04, 1.06, 0.99, 1.04, 1.06]
    out = [
        tracker.update(syn.frame(i / FPS, crafted((r, 1.8, 1.8, 1.8))))[0].fingers[0].curled
        for i, r in enumerate(reaches)
    ]
    assert out == [False, False, False, True, True, False]


def test_u5_the_four_fingers_each_have_their_own_state() -> None:
    tracker = HandTracker(TUNING)
    out = tracker.update(syn.frame(0.0, crafted((1.0, 1.05, 1.5, 1.8))))[0]
    assert [f.curled for f in out.fingers] == [True, True, False, False]
    assert [round(f.reach, 6) for f in out.fingers] == [1.0, 1.05, 1.5, 1.8]


def test_u5_a_new_track_starts_uncurled_at_a_reach_in_the_band() -> None:
    tracker = HandTracker(TUNING)
    assert tracker.update(syn.frame(0.0, crafted((1.15, 1.15, 1.15, 1.15))))[0].fingers[0].curled is False


# --------------------------------------------------------------------------------------------------- anchor and speed


def test_the_anchor_is_the_knuckle_mean_in_pose_space() -> None:
    obs = posture(at=(0.4, 0.5))
    sample = HandTracker(TUNING).update(syn.frame(0.0, obs))[0]
    points = poses.pose_points(obs.image, ASPECT)
    np.testing.assert_allclose(sample.anchor, (points[INDEX_MCP, :2] + points[MIDDLE_MCP, :2]) / 2)
    np.testing.assert_allclose(sample.anchor, (0.4, 0.5 * ASPECT), atol=1e-9)


def test_a_new_hand_has_no_speed_and_a_still_hand_stays_at_zero() -> None:
    tracker = HandTracker(TUNING)
    out = run(tracker, frames(*[(posture(),)] * 12))
    assert out[0][0].speed == 0.0
    assert all(s[0].speed < 1e-9 for s in out)


@pytest.mark.parametrize("v", [0.3, 1.0, 2.5])
def test_speed_is_the_anchor_travel_over_the_last_hundred_and_fifty_milliseconds(v: float) -> None:
    tracker = HandTracker(TUNING)
    moving = [(posture(at=(0.2 + v * i / FPS, 0.5)),) for i in range(20)]
    out = run(tracker, frames(*moving))
    assert out[-1][0].speed == pytest.approx(v, rel=0.02)


def test_speed_is_smoothed_over_three_frames() -> None:
    tracker = HandTracker(TUNING)
    still = [(posture(at=(0.2, 0.5)),)] * 10
    moving = [(posture(at=(0.2 + 1.0 * i / FPS, 0.5)),) for i in range(1, 10)]
    out = run(tracker, frames(*still, *moving))
    after = [o[0].speed for o in out[10:]]
    assert after[0] < after[-1]  # it builds up, it does not jump to the final value in one frame
    assert max(after) < 1.1
    assert after[-1] == pytest.approx(1.0, rel=0.05)


def test_the_speed_of_a_hand_that_stops_falls_to_zero() -> None:
    tracker = HandTracker(TUNING)
    moving = [(posture(at=(0.2 + 1.0 * i / FPS, 0.5)),) for i in range(10)]
    stopped = [(posture(at=(0.2 + 1.0 * 9 / FPS, 0.5)),)] * 15
    out = run(tracker, frames(*moving, *stopped))
    assert out[9][0].speed > 0.7
    assert out[-1][0].speed < 0.02


# --------------------------------------------------------------------------------------------------- aim: the levelling


def test_the_default_levelling_moves_the_aim_down_the_hand_by_level_times_palm() -> None:
    obs = posture()
    sample = HandTracker(TUNING).update(syn.frame(0.0, obs))[0]
    p = poses.pose_points(obs.image, ASPECT)
    up = (p[MIDDLE_MCP, :2] - p[WRIST, :2]) / np.linalg.norm(p[MIDDLE_MCP, :2] - p[WRIST, :2])
    for finger, (tip, level) in enumerate(zip((8, 12, 16, 20), (0.0, 0.10, 0.04, -0.15), strict=True)):
        expected = p[tip, :2] - up * level * sample.palm
        np.testing.assert_allclose(sample.fingers[finger].aim, expected, atol=1e-12)


@pytest.mark.parametrize("roll", [-45.0, 30.0, 60.0, 90.0])
def test_a_rolled_hand_is_levelled_along_its_own_axis(roll: float) -> None:
    flat = HandTracker(TUNING).update(syn.frame(0.0, posture(roll=0.0)))[0]
    rolled = HandTracker(TUNING).update(syn.frame(0.0, posture(roll=roll)))[0]
    for a, b in zip(flat.fingers, rolled.fingers, strict=True):
        # the same finger, offset from its tip by the same distance, only turned: the lift (a length) is unchanged
        assert math.isclose(a.lift, b.lift, abs_tol=1e-9)
    # the middle finger's levelling offset has the same length at any roll
    for level, finger in ((0.10, 1), (-0.15, 3)):
        obs = posture(roll=roll)
        p = poses.pose_points(obs.image, ASPECT)
        tip = p[(8, 12, 16, 20)[finger], :2]
        sample = HandTracker(TUNING).update(syn.frame(0.0, obs))[0]
        assert math.isclose(
            float(np.linalg.norm(sample.fingers[finger].aim - tip)), abs(level) * sample.palm, rel_tol=1e-9
        )


def test_the_levels_of_the_tuning_are_the_defaults() -> None:
    zero = Tuning(level_palm=(0.0, 0.0, 0.0, 0.0))
    obs = posture()
    sample = HandTracker(zero).update(syn.frame(0.0, obs))[0]
    p = poses.pose_points(obs.image, ASPECT)
    for finger, tip in enumerate((8, 12, 16, 20)):
        np.testing.assert_allclose(sample.fingers[finger].aim, p[tip, :2], atol=1e-12)


def shifted_arc(side: str = "right", at: tuple[float, float] = (0.5, 0.45)) -> HandObservation:
    """A hand whose arc differs from the model's: the middle finger is bent a good deal, the pinky straight."""
    fingers = {
        "index": syn._finger("index", REST),
        "middle": syn._finger("middle", (40.0, 40.0, 20.0)),
        "ring": syn._finger("ring", REST),
        "pinky": syn._finger("pinky", (0.0, 5.0, 3.0)),
    }
    world = syn._assemble(fingers, syn._thumb_tip("palm", fingers), side)  # type: ignore[arg-type]
    return syn.hand("palm", at, handedness=side, world=world)  # type: ignore[arg-type]


def line_spread(sample, obs: HandObservation) -> float:
    """How far apart the four aims are along the hand's own down axis (zero: the tips lie on one line)."""
    p = poses.pose_points(obs.image, ASPECT)
    axis = p[MIDDLE_MCP, :2] - p[WRIST, :2]
    heights = [float(f.aim @ (-axis / np.linalg.norm(axis))) for f in sample.fingers]
    return max(heights) - min(heights)


def test_a8_per_user_levelling_puts_the_tips_of_a_shifted_arc_on_one_line() -> None:
    tracker = HandTracker(TUNING)
    arc = shifted_arc()
    still = [(arc,)] * 20
    before = run(tracker, frames(*still))[-1][0]
    learned = tracker.learn_levels()
    assert set(learned) == {"right"}
    after = run(tracker, frames(*still, t0=1.0))[-1][0]
    assert line_spread(after, arc) < 1e-9
    assert line_spread(before, arc) > 0.005
    # the bent middle finger stands lower than the model's does (a smaller level than 0.10), the straight pinky higher
    assert learned["right"][1] < 0.10
    assert learned["right"][3] > -0.15


def test_a8_the_learned_levels_are_clamped_to_thirty_percent_of_the_palm() -> None:
    fingers = {n: syn._finger(n, REST) for n in syn._MCP}
    fingers["pinky"] = syn._finger("pinky", (0.0, 0.0, 0.0))
    fingers["middle"] = syn._finger("middle", (85.0, 90.0, 60.0))
    world = syn._assemble(fingers, syn._thumb_tip("palm", fingers), "right")
    tracker = HandTracker(TUNING)
    run(tracker, frames(*[(syn.hand("palm", handedness="right", world=world),)] * 10))
    levels = tracker.learn_levels()["right"]
    assert all(-0.30 <= x <= 0.30 for x in levels)
    assert max(abs(x) for x in levels) == pytest.approx(0.30)


def test_a8_levels_are_per_side_and_a_returning_hand_gets_its_sides_levels() -> None:
    tracker = HandTracker(TUNING)
    arc = shifted_arc("right", (0.7, 0.45))
    both = (arc, posture("left", (0.3, 0.45)))
    run(tracker, frames(*[both] * 20))
    learned = tracker.learn_levels()
    assert set(learned) == {"left", "right"}
    assert tracker.levels["right"] == learned["right"]
    # the right hand is lost for a second and comes back as a new track: it still has its own levels
    out = run(tracker, frames(*[(arc,)] * 5, t0=5.0))
    assert line_spread(out[-1][0], arc) < 1e-9


def test_a8_without_learning_nothing_changes() -> None:
    tracker = HandTracker(TUNING)
    assert tracker.learn_levels() == {}
    assert dict(tracker.levels) == {}
    run(tracker, frames(*[(posture(),)] * 2))
    assert tracker.learn_levels() == {}  # too few frames to measure anything


def test_a8_reset_forgets_the_learned_levels() -> None:
    tracker = HandTracker(TUNING)
    arc = shifted_arc()
    run(tracker, frames(*[(arc,)] * 20))
    tracker.learn_levels()
    tracker.reset()
    out = run(tracker, frames(*[(arc,)] * 5, t0=9.0))
    assert line_spread(out[-1][0], arc) > 0.005


# --------------------------------------------------------------------------------------------------- X1: lift and score

# airfeat.extract (the reference simulator's feature code) on the model's postures; the lift is the same at every roll.
LIFT = {
    "STRAIGHT": (0.70807002, 0.82441257, 0.75777649, 0.54085669),
    "REST": (0.65514652, 0.76325610, 0.70092624, 0.49993310),
    "RELAXED": (0.14436717, 0.16999721, 0.15073151, 0.10774865),
}
ANGLES = {"STRAIGHT": syn.STRAIGHT, "REST": REST, "RELAXED": syn.RELAXED}


@pytest.mark.parametrize("roll", [0.0, 30.0, 60.0, -45.0])
@pytest.mark.parametrize("name", ["STRAIGHT", "REST", "RELAXED"])
@pytest.mark.parametrize("side", ["right", "left"])
def test_x1_lift_equals_the_reference_feature_at_every_roll(name: str, roll: float, side: str) -> None:
    sample = HandTracker(TUNING).update(syn.frame(0.0, posture(side, angles=ANGLES[name], roll=roll)))[0]
    assert [f.lift for f in sample.fingers] == pytest.approx(LIFT[name], abs=1e-6)


def test_x1_the_lift_of_the_resting_posture_is_unchanged_by_rolling_the_hand() -> None:
    flat = HandTracker(TUNING).update(syn.frame(0.0, posture(angles=REST)))[0]
    for roll in (15.0, 30.0, 45.0, 60.0, -60.0):
        rolled = HandTracker(TUNING).update(syn.frame(0.0, posture(angles=REST, roll=roll)))[0]
        assert max(abs(a.lift - b.lift) for a, b in zip(flat.fingers, rolled.fingers, strict=True)) < 0.02


def test_x1_the_lift_ranges_that_make_the_posture_gate_work() -> None:
    rest = HandTracker(TUNING).update(syn.frame(0.0, posture(angles=REST)))[0]
    relaxed = HandTracker(TUNING).update(syn.frame(0.0, posture(angles=syn.RELAXED)))[0]
    assert all(0.499 <= f.lift <= 0.765 for f in rest.fingers)
    assert all(0.10 <= f.lift <= 0.171 for f in relaxed.fingers)


def test_x1_bending_a_finger_lowers_its_lift_and_only_its_own() -> None:
    flat = HandTracker(TUNING).update(syn.frame(0.0, posture(angles=REST)))[0]
    fingers = {n: syn._finger(n, REST) for n in syn._MCP}
    fingers["middle"] = syn._finger("middle", (REST[0] + 38.0, REST[1] + 11.4, REST[2] + 5.7))
    world = syn._assemble(fingers, syn._thumb_tip("palm", fingers), "right")
    bent = HandTracker(TUNING).update(syn.frame(0.0, syn.hand("palm", handedness="right", world=world)))[0]
    assert bent.fingers[1].lift < 0.6 * flat.fingers[1].lift
    for f in (0, 2, 3):
        assert math.isclose(bent.fingers[f].lift, flat.fingers[f].lift, abs_tol=1e-9)


@pytest.mark.parametrize("score", [0.3, 0.6, 0.95, 1.0])
def test_x1_the_score_is_the_handedness_confidence(score: float) -> None:
    assert HandTracker(TUNING).update(syn.frame(0.0, posture(score=score)))[0].score == score


def test_the_lift_does_not_depend_on_the_depth_axis() -> None:
    """The lift uses x and y only: the noisiest axis stays out of the detector's signal (2.12)."""
    obs = posture()
    deeper = obs.image.copy()
    deeper[:, 2] *= 3.0
    deeper[8, 2] += 0.2
    a = HandTracker(TUNING).update(syn.frame(0.0, obs))[0]
    b = HandTracker(TUNING).update(syn.frame(0.0, HandObservation("right", 0.95, deeper, obs.world)))[0]
    assert [f.lift for f in a.fingers] == pytest.approx([f.lift for f in b.fingers], abs=1e-12)


@pytest.mark.parametrize("degrees", [80.0, 84.0, 88.0, 90.0, -90.0, 92.0])
def test_a_hand_that_points_at_the_camera_has_no_axis_so_no_lift_and_no_levelling(degrees: float) -> None:
    """The picture shows less than a quarter of the palm: the axis is smaller than the landmark noise, so the lift
    and the levelling (both divide by it) would be noise over noise. The hand takes the no-axis branch (2.2)."""
    obs = pitched(posture(), degrees)
    assert axis_share(obs) < 0.2  # the premise: a hand within a dozen degrees of the line of sight
    sample = HandTracker(TUNING).update(syn.frame(0.0, obs))[0]
    points = poses.pose_points(obs.image, ASPECT)
    assert [f.lift for f in sample.fingers] == [0.0, 0.0, 0.0, 0.0]
    for f, tip in zip(sample.fingers, (8, 12, 16, 20), strict=True):
        np.testing.assert_allclose(f.aim, points[tip, :2], atol=1e-12)  # the raw tip: no levelling offset


@pytest.mark.parametrize("degrees", [0.0, 45.0, 60.0, 70.0, -70.0])
def test_a_hand_whose_axis_is_over_a_quarter_of_the_palm_keeps_its_lift_and_levelling(degrees: float) -> None:
    obs = pitched(posture(), degrees)
    assert axis_share(obs) > 0.3
    sample = HandTracker(TUNING).update(syn.frame(0.0, obs))[0]
    points = poses.pose_points(obs.image, ASPECT)
    assert any(f.lift != 0.0 for f in sample.fingers)
    # the middle, ring and pinky levels are not zero: their aims sit off their tips
    for f, tip in zip(sample.fingers[1:], (12, 16, 20), strict=True):
        assert float(np.linalg.norm(f.aim - points[tip, :2])) > 0.01 * sample.palm


def test_the_axis_threshold_is_a_quarter_of_the_palm() -> None:
    """Either side of 0.25: the pitches are found on the share the picture shows, not on a guessed angle."""
    shares = {deg: axis_share(pitched(posture(), deg)) for deg in np.arange(60.0, 90.0, 0.25)}
    inside = max(deg for deg, share in shares.items() if share > 0.26)
    outside = min(deg for deg, share in shares.items() if share < 0.24)
    for degrees, live in ((inside, True), (outside, False)):
        sample = HandTracker(TUNING).update(syn.frame(0.0, pitched(posture(), float(degrees))))[0]
        assert any(f.lift != 0.0 for f in sample.fingers) == live, degrees


def test_a_sample_is_the_documented_record() -> None:
    sample = HandTracker(TUNING).update(syn.frame(0.5, posture("left")))[0]
    assert sample.t == 0.5
    assert sample.side == "left"
    assert len(sample.fingers) == 4
    assert [f.finger for f in sample.fingers] == [0, 1, 2, 3]
    assert sample.anchor.shape == (2,)
    assert all(f.aim.shape == (2,) for f in sample.fingers)
    assert isinstance(sample.palm, float)
    assert isinstance(sample.speed, float)
    assert all(isinstance(f.curled, bool) for f in sample.fingers)


def test_the_thumb_matters_for_the_ratio_only() -> None:
    obs = posture()
    image = obs.image.copy()
    image[[1, 2, 3, THUMB_TIP], :2] += 0.1  # the thumb wanders
    moved = HandTracker(TUNING).update(syn.frame(0.0, HandObservation("right", 0.9, image, obs.world)))[0]
    base = HandTracker(TUNING).update(syn.frame(0.0, obs))[0]
    assert [f.lift for f in moved.fingers] == pytest.approx([f.lift for f in base.fingers])
    assert [f.reach for f in moved.fingers] == pytest.approx([f.reach for f in base.fingers])
    assert [f.ratio for f in moved.fingers] != pytest.approx([f.ratio for f in base.fingers])
