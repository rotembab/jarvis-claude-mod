from __future__ import annotations

import numpy as np
import pytest

from jarvis_hands.geometry import Point
from jarvis_hands.landmarks import FINGERS, INDEX_TIP, MIDDLE_TIP, THUMB_TIP, WRIST, HandObservation
from jarvis_hands.poses import (
    BAND,
    HandPose,
    PoseThresholds,
    PoseTracker,
    anchor_point,
    finger_reach,
    palm_size,
    pinch_ratio,
    thresholds_for,
)
from jarvis_hands.settings import KNOB_BY_KEY

from scripted import HEIGHT, POSES, SCALE, WIDTH, hand

EXPECTED = {
    "palm": "palm",
    "fist": "fist",
    "pinch": "pinch",
    "pinch_middle": "pinch_middle",
    "two": "two",
    "point": "hover",
    "hover": "hover",
    "thumb_up": "fist",
}

# World landmarks (metres) MediaPipe 1.1.0's hand landmarker gives for its own test photos
# (mediapipe-assets: fist, pointing_up, victory, thumb_up, right_hands, left_hands). "fist_mirrored" is
# fist.jpg flipped left-right, as the tracker feeds frames; its thumb sits closer to the index tip.
_MEDIAPIPE_WORLD = {
    "fist": """
        -0.0086 +0.0817 +0.0060   +0.0272 +0.0620 -0.0089   +0.0498 +0.0353 -0.0168
        +0.0502 +0.0053 -0.0289   +0.0155 -0.0062 -0.0316   +0.0291 -0.0024 +0.0011
        +0.0346 -0.0176 -0.0207   +0.0341 -0.0060 -0.0257   +0.0287 +0.0112 -0.0098
        +0.0015 -0.0048 +0.0057   +0.0105 -0.0197 -0.0271   +0.0132 +0.0072 -0.0349
        +0.0138 +0.0119 -0.0042   -0.0199 -0.0007 -0.0003   -0.0109 -0.0085 -0.0288
        -0.0054 +0.0129 -0.0342   -0.0029 +0.0219 -0.0118   -0.0388 +0.0119 -0.0076
        -0.0309 +0.0011 -0.0227   -0.0184 +0.0140 -0.0328   -0.0243 +0.0225 -0.0235
    """,
    "fist_mirrored": """
        -0.0042 +0.0868 +0.0199   -0.0308 +0.0635 -0.0003   -0.0418 +0.0361 -0.0032
        -0.0474 +0.0074 -0.0154   -0.0274 -0.0075 -0.0209   -0.0302 -0.0060 +0.0042
        -0.0316 -0.0162 -0.0201   -0.0330 -0.0077 -0.0294   -0.0295 +0.0088 -0.0169
        -0.0046 -0.0067 +0.0053   -0.0077 -0.0184 -0.0323   -0.0169 +0.0098 -0.0438
        -0.0115 +0.0134 -0.0103   +0.0200 +0.0021 -0.0018   +0.0106 -0.0069 -0.0378
        +0.0039 +0.0183 -0.0429   +0.0083 +0.0268 -0.0181   +0.0321 +0.0196 -0.0094
        +0.0304 +0.0047 -0.0287   +0.0229 +0.0186 -0.0378   +0.0246 +0.0276 -0.0255
    """,
    "pointing_up": """
        +0.0170 +0.0864 +0.0360   +0.0420 +0.0567 +0.0197   +0.0504 +0.0318 +0.0025
        +0.0433 +0.0091 -0.0249   +0.0160 +0.0051 -0.0369   +0.0245 -0.0135 +0.0042
        +0.0248 -0.0414 -0.0029   +0.0258 -0.0620 -0.0103   +0.0230 -0.0797 -0.0317
        +0.0007 -0.0061 +0.0049   +0.0077 -0.0169 -0.0297   +0.0176 +0.0092 -0.0369
        +0.0145 +0.0175 -0.0110   -0.0181 +0.0062 -0.0027   -0.0102 +0.0053 -0.0346
        +0.0044 +0.0284 -0.0329   +0.0038 +0.0360 -0.0074   -0.0318 +0.0299 -0.0089
        -0.0240 +0.0216 -0.0276   -0.0085 +0.0319 -0.0323   -0.0129 +0.0387 -0.0171
    """,
    "victory": """
        +0.0129 +0.0918 +0.0105   +0.0372 +0.0639 -0.0105   +0.0398 +0.0372 -0.0292
        +0.0189 +0.0124 -0.0485   -0.0127 +0.0010 -0.0443   +0.0256 -0.0080 -0.0056
        +0.0281 -0.0381 -0.0102   +0.0301 -0.0600 -0.0143   +0.0272 -0.0781 -0.0323
        +0.0014 -0.0051 +0.0056   -0.0024 -0.0446 -0.0037   -0.0091 -0.0664 -0.0215
        -0.0091 -0.0894 -0.0370   -0.0176 +0.0038 +0.0046   -0.0219 -0.0101 -0.0237
        -0.0128 +0.0083 -0.0377   -0.0000 +0.0268 -0.0372   -0.0348 +0.0223 -0.0006
        -0.0358 +0.0106 -0.0175   -0.0235 +0.0185 -0.0286   -0.0136 +0.0334 -0.0262
    """,
    "thumb_up": """
        +0.0675 +0.0311 +0.0552   +0.0632 -0.0038 +0.0209   +0.0547 -0.0387 +0.0109
        +0.0355 -0.0685 +0.0026   +0.0192 -0.0872 +0.0068   +0.0045 -0.0276 -0.0043
        -0.0031 -0.0241 -0.0340   +0.0080 -0.0189 -0.0327   +0.0255 -0.0145 -0.0045
        -0.0045 -0.0040 +0.0025   -0.0108 -0.0031 -0.0362   +0.0168 +0.0030 -0.0362
        +0.0199 -0.0033 +0.0043   -0.0057 +0.0171 +0.0037   -0.0105 +0.0173 -0.0288
        +0.0147 +0.0194 -0.0262   +0.0212 +0.0142 +0.0012   +0.0011 +0.0436 +0.0068
        -0.0102 +0.0389 -0.0156   +0.0072 +0.0361 -0.0287   +0.0130 +0.0391 -0.0125
    """,
    "open_right": """
        +0.0129 +0.0605 +0.0396   -0.0134 +0.0439 +0.0310   -0.0244 +0.0263 +0.0280
        -0.0404 +0.0043 +0.0191   -0.0624 -0.0088 +0.0097   -0.0140 +0.0000 -0.0004
        -0.0216 -0.0198 -0.0041   -0.0291 -0.0399 +0.0005   -0.0375 -0.0625 +0.0088
        -0.0022 -0.0014 -0.0009   -0.0030 -0.0292 -0.0029   -0.0075 -0.0565 -0.0018
        -0.0165 -0.0855 +0.0070   +0.0071 -0.0016 -0.0012   +0.0105 -0.0198 -0.0004
        +0.0141 -0.0490 +0.0006   +0.0120 -0.0721 +0.0070   +0.0136 +0.0087 +0.0098
        +0.0261 -0.0088 +0.0088   +0.0329 -0.0318 +0.0072   +0.0330 -0.0485 +0.0109
    """,
    "open_left": """
        -0.0137 +0.0431 +0.0315   +0.0054 +0.0234 +0.0255   +0.0205 +0.0106 +0.0205
        +0.0332 +0.0074 +0.0175   +0.0501 -0.0055 +0.0043   -0.0006 -0.0120 -0.0004
        +0.0068 -0.0110 -0.0020   +0.0166 -0.0295 +0.0041   +0.0232 -0.0511 +0.0054
        -0.0024 -0.0024 -0.0008   -0.0030 -0.0111 -0.0005   -0.0032 -0.0422 +0.0036
        +0.0034 -0.0677 +0.0160   +0.0009 +0.0055 -0.0007   -0.0084 -0.0034 +0.0020
        -0.0139 -0.0284 +0.0077   -0.0109 -0.0520 +0.0111   -0.0068 +0.0131 +0.0076
        -0.0156 +0.0015 +0.0084   -0.0228 -0.0198 +0.0111   -0.0259 -0.0378 +0.0118
    """,
}

REAL_EXPECTED = {
    "fist": "fist",
    "fist_mirrored": "fist",
    "pointing_up": "hover",
    "victory": "two",
    "thumb_up": "fist",
    "open_right": "palm",
    "open_left": "palm",
}


def real_world(name: str) -> np.ndarray:
    return np.array(_MEDIAPIPE_WORLD[name].split(), dtype=float).reshape(21, 3)


def observation(world: np.ndarray, base: HandObservation | None = None) -> HandObservation:
    """A hand whose image landmarks are ``world`` projected like ``scripted.hand`` does (1280 x 720)."""
    base = base or hand("palm")
    k = SCALE / WIDTH
    image = np.empty((21, 3))
    image[:, 0] = 0.5 + world[:, 0] * k
    image[:, 1] = 0.45 + world[:, 1] * SCALE / HEIGHT
    image[:, 2] = (world[:, 2] - world[0, 2]) * k
    return HandObservation(base.handedness, base.score, image, world)


def with_reach(world: np.ndarray, finger: str, ratio: float) -> np.ndarray:
    """``world`` with the finger's tip moved along the wrist->tip line so its reach is ``ratio``."""
    out = world.copy()
    mcp, tip = FINGERS[finger]
    wrist = out[WRIST]
    direction = (out[tip] - wrist) / np.linalg.norm(out[tip] - wrist)
    out[tip] = wrist + direction * ratio * np.linalg.norm(out[mcp] - wrist)
    return out


def with_pinch(world: np.ndarray, ratio: float, tip: int = INDEX_TIP) -> np.ndarray:
    """``world`` with the thumb tip ``ratio`` palm sizes from ``tip``, on the thumb's side."""
    out = world.copy()
    side = out[THUMB_TIP] - out[tip]
    side /= np.linalg.norm(side)
    out[THUMB_TIP] = out[tip] + side * ratio * palm_size(out)
    return out


@pytest.mark.parametrize("pose", POSES)
@pytest.mark.parametrize("handedness", ["right", "left"])
@pytest.mark.parametrize("roll", [0.0, -25.0, 30.0])
def test_synthetic_poses_classify(pose: str, handedness: str, roll: float) -> None:
    obs = hand(pose, (0.4, 0.5), handedness=handedness, roll=roll)  # type: ignore[arg-type]
    assert PoseTracker().classify(obs).pose == EXPECTED[pose]


@pytest.mark.parametrize("name", list(REAL_EXPECTED))
def test_mediapipe_landmarks_of_the_test_photos_classify(name: str) -> None:
    assert PoseTracker().classify(observation(real_world(name))).pose == REAL_EXPECTED[name]


def test_measured_features_of_the_test_photos() -> None:
    """The ranges SPEC-hands.md quotes, so threshold changes are checked against real hands."""
    for name in ("open_right", "open_left"):
        assert all(1.55 < r < 2.4 for r in finger_reach(real_world(name)))
    for name in ("fist", "thumb_up"):
        assert all(r < 1.06 for r in finger_reach(real_world(name)))
    index, middle, ring, pinky = finger_reach(real_world("victory"))
    assert index > 1.6 and middle > 1.6 and ring < 0.95 and pinky < 0.95
    # A fist brings the thumb onto the index: a pinch distance only the "index not curled" rule tells apart.
    assert pinch_ratio(real_world("fist_mirrored"), INDEX_TIP) < 0.28


def test_poses_do_not_depend_on_hand_size() -> None:
    for name, expected in REAL_EXPECTED.items():
        assert PoseTracker().classify(observation(real_world(name) * 0.6)).pose == expected


def test_extended_hysteresis() -> None:
    tracker = PoseTracker()
    base = hand("palm").world

    def index_extended(ratio: float) -> bool:
        return tracker.classify(observation(with_reach(base, "index", ratio))).extended[0]

    assert not index_extended(1.35)  # fresh: must pass 1.40 to enter
    assert index_extended(1.45)
    assert index_extended(1.30)  # stays until below 1.25
    assert not index_extended(1.20)
    assert not index_extended(1.35)


def test_curled_hysteresis() -> None:
    tracker = PoseTracker()
    base = hand("fist").world

    def index_curled(ratio: float) -> bool:
        return tracker.classify(observation(with_reach(base, "index", ratio))).curled[0]

    assert not index_curled(1.15)  # fresh: must drop below 1.10 to enter
    assert index_curled(1.05)
    assert index_curled(1.15)  # stays until above 1.20
    assert not index_curled(1.25)
    assert not index_curled(1.15)


def test_pinch_hysteresis_and_display_value() -> None:
    tracker = PoseTracker()
    base = hand("palm").world
    open_hand = tracker.classify(observation(base))
    assert open_hand.pose == "palm" and open_hand.pinch == pytest.approx(0.0, abs=0.2)
    near = tracker.classify(observation(with_pinch(base, 0.30)))
    assert near.pose != "pinch" and 0.8 < near.pinch < 1.0
    assert tracker.classify(observation(with_pinch(base, 0.20))).pose == "pinch"
    closed = tracker.classify(observation(with_pinch(base, 0.35)))
    assert closed.pose == "pinch" and closed.pinch == 1.0  # stays closed until above 0.40
    assert tracker.classify(observation(with_pinch(base, 0.45))).pose == "palm"


def test_pinch_display_grows_as_the_thumb_closes() -> None:
    base = hand("palm").world
    values = [PoseTracker().classify(observation(with_pinch(base, r))).pinch for r in (0.9, 0.7, 0.5, 0.3)]
    assert values == sorted(values) and values[0] == 0.0 and values[-1] > 0.8


def test_a_curled_index_never_pinches() -> None:
    fist = hand("fist").world
    pose = PoseTracker().classify(observation(with_pinch(fist, 0.05)))
    assert pose.pose == "fist"
    assert pose.pinch == 0.0


def test_a_closed_pinch_opens_when_the_index_curls() -> None:
    tracker = PoseTracker()
    assert tracker.classify(hand("pinch")).pose == "pinch"
    assert tracker.classify(observation(with_pinch(hand("fist").world, 0.05))).pose == "fist"


def test_middle_pinch_only_while_the_middle_finger_is_not_curled() -> None:
    point = hand("point").world  # index straight, the others curled
    pose = PoseTracker().classify(observation(with_pinch(point, 0.05, MIDDLE_TIP)))
    assert pose.pose == "hover"


def test_middle_pinch_yields_to_the_index_pinch() -> None:
    world = hand("pinch").world.copy()
    world[MIDDLE_TIP] = world[INDEX_TIP] + np.array([0.004, 0.0, 0.0])
    assert pinch_ratio(world, MIDDLE_TIP) < 0.25
    assert PoseTracker().classify(observation(world)).pose == "pinch"


def test_middle_pinch_beats_two() -> None:
    two = hand("two").world
    assert PoseTracker().classify(observation(with_pinch(two, 0.05, MIDDLE_TIP))).pose == "pinch_middle"


def test_reach_ratio_band_is_hover() -> None:
    tracker = PoseTracker()
    world = hand("palm").world
    for finger in FINGERS:
        world = with_reach(world, finger, 1.3)
    pose = tracker.classify(observation(world))
    assert pose.pose == "hover"
    assert pose.extended == (False, False, False, False) and pose.curled == (False, False, False, False)


def test_anchor_modes() -> None:
    obs = hand("palm", (0.3, 0.6))
    knuckles = PoseTracker().classify(obs).anchor
    assert knuckles == Point(pytest.approx(0.3), pytest.approx(0.6))
    assert knuckles == anchor_point(obs.image, "knuckles")
    index = PoseTracker().classify(obs, "index").anchor
    assert index == Point(float(obs.image[INDEX_TIP, 0]), float(obs.image[INDEX_TIP, 1]))
    assert index.y < knuckles.y  # the fingertip is above the knuckles


def test_the_anchor_does_not_move_between_poses() -> None:
    anchors = {PoseTracker().classify(hand(p, (0.42, 0.37))).anchor for p in POSES}
    assert len({(round(a.x, 9), round(a.y, 9)) for a in anchors}) == 1


def test_reset_forgets_the_hysteresis() -> None:
    tracker = PoseTracker()
    base = hand("palm").world
    tracker.classify(observation(with_reach(base, "index", 1.45)))
    tracker.reset()
    assert not tracker.classify(observation(with_reach(base, "index", 1.30))).extended[0]


def test_thresholds_are_the_spec_values() -> None:
    th = PoseThresholds()
    assert (th.extended_enter, th.extended_leave, th.curled_enter, th.curled_leave) == (1.40, 1.25, 1.10, 1.20)
    assert (th.pinch_close, th.pinch_open) == (0.28, 0.40)
    with pytest.raises(AttributeError):
        th.pinch_close = 0.3  # type: ignore[misc]


def test_custom_thresholds() -> None:
    strict = PoseTracker(PoseThresholds(extended_enter=2.5, extended_leave=2.4))
    assert strict.classify(hand("palm")).pose == "hover"


def test_hand_pose_is_frozen() -> None:
    pose = PoseTracker().classify(hand("palm"))
    assert isinstance(pose, HandPose)
    with pytest.raises(AttributeError):
        pose.pose = "fist"  # type: ignore[misc]


# -- the pinch and fist sensitivity knobs (thresholds_for) -----------------------------------------------------

#: The pinned contract's ranges; the helper's own (settings.KNOBS) are narrower where the measurements said so.
CONTRACT_PINCH, CONTRACT_FIST = (0.6, 1.6), (0.8, 1.2)
PINCH_RANGE = (KNOB_BY_KEY["pinch"].low, KNOB_BY_KEY["pinch"].high)
FIST_RANGE = (KNOB_BY_KEY["fist"].low, KNOB_BY_KEY["fist"].high)


def grid(low: float, high: float, n: int = 17) -> list[float]:
    return [float(v) for v in np.linspace(low, high, n)]


def test_the_knob_ranges_sit_inside_the_contract_and_around_the_default() -> None:
    for (low, high), (c_low, c_high) in ((PINCH_RANGE, CONTRACT_PINCH), (FIST_RANGE, CONTRACT_FIST)):
        assert c_low <= low < 1.0 < high <= c_high


def test_the_default_sensitivities_are_exactly_the_default_thresholds() -> None:
    assert thresholds_for() == PoseThresholds()
    assert thresholds_for(1.0, 1.0) == PoseThresholds()
    assert thresholds_for(1, 1) == PoseThresholds()


@pytest.mark.parametrize("pinch", grid(0.5, 2.0, 7))
@pytest.mark.parametrize("fist", grid(0.5, 1.6, 12))
def test_the_threshold_order_holds_everywhere(pinch: float, fist: float) -> None:
    """Far beyond the ranges too: a finger is never curled and extended at once, a pinch closes before it opens."""
    th = thresholds_for(pinch, fist)
    assert th.curled_enter < th.curled_leave < th.extended_leave < th.extended_enter
    fist_enter, fist_leave = fist_thresholds(th)
    assert 0 < fist_enter < fist_leave < th.extended_leave  # a fist is never an extended finger either
    assert fist_enter <= th.curled_enter and fist_leave <= th.curled_leave  # and it implies curled fingers
    assert 0 < th.pinch_close < th.pinch_open
    assert th.pinch_close < th.pinch_display_open


def test_pinch_sensitivity_scales_the_close_threshold_and_never_raises_the_open_one() -> None:
    """A looser pinch closes at a larger ratio, but a hand lets go where it always did.

    The hand that rests its thumb against the index (woman_hands' relaxed palms: 0.39 to 0.47) must not hold a
    pinch, so the open threshold stays at the default 0.40 above a pinch sensitivity of 1.0 (and a strict pinch
    lowers both, in proportion), with the noise-proof gap above the close threshold in any case.
    """
    base = PoseThresholds()
    for s in grid(*PINCH_RANGE):
        th = thresholds_for(s, 1.0)
        assert th.pinch_close == pytest.approx(base.pinch_close * s)
        assert th.pinch_open == pytest.approx(max(base.pinch_open * min(s, 1.0), th.pinch_close + BAND))
        assert th.pinch_open >= th.pinch_close + BAND - 1e-12  # the hysteresis, about six times the ratio noise
        if s >= 1.0:
            assert th.pinch_open == pytest.approx(base.pinch_open)  # nothing in the knob's range raises it
        assert (th.curled_enter, th.curled_leave, th.extended_leave, th.extended_enter) == (
            base.curled_enter,
            base.curled_leave,
            base.extended_leave,
            base.extended_enter,
        )


def fist_thresholds(th: PoseThresholds) -> tuple[float, float]:
    """What a finger must reach to count toward a fist (enter, leave): the curled thresholds unless a tighter fist."""
    return (
        th.curled_enter if th.fist_enter is None else th.fist_enter,
        th.curled_leave if th.fist_leave is None else th.fist_leave,
    )


def test_a_looser_fist_scales_the_curled_thresholds_and_not_the_pinch() -> None:
    base = PoseThresholds()
    previous = thresholds_for(1.0, 1.0)
    for s in grid(1.0, FIST_RANGE[1], 9)[1:]:
        th = thresholds_for(1.0, s)
        assert th.curled_enter == pytest.approx(base.curled_enter * s)
        assert th.curled_leave == pytest.approx(base.curled_leave * s)
        assert fist_thresholds(th) == (th.curled_enter, th.curled_leave)  # the fist and the pinch gate go together
        assert (th.pinch_close, th.pinch_open) == (base.pinch_close, base.pinch_open)
        assert th.curled_enter > previous.curled_enter and th.curled_leave > previous.curled_leave
        assert th.extended_leave >= previous.extended_leave and th.extended_enter >= previous.extended_enter
        previous = th


def test_a_stricter_fist_leaves_the_curled_flags_that_gate_the_pinch_alone() -> None:
    """A stricter fist asks more of the fist and nothing else: the curled flags (which gate the pinch and tell the
    engine how settled a pinch is) stay at the default, or a slow closing into a fist stops being judged as before."""
    base = PoseThresholds()
    for s in grid(FIST_RANGE[0], 1.0, 6)[:-1]:
        th = thresholds_for(1.0, s)
        assert (th.curled_enter, th.curled_leave) == (base.curled_enter, base.curled_leave)
        assert th.fist_enter == pytest.approx(base.curled_enter * s)
        assert th.fist_leave == pytest.approx(base.curled_leave * s)
        assert th.fist_enter < th.fist_leave <= th.curled_leave
        assert th.fist_enter < th.curled_enter  # a fist implies curled fingers, never the other way round
        assert (th.pinch_close, th.pinch_open) == (base.pinch_close, base.pinch_open)
    assert thresholds_for(1.0, 1.0).fist_enter is None and thresholds_for(1.0, 1.0).fist_leave is None


@pytest.mark.parametrize("reach", [1.0, 1.04, 1.07])
def test_fingers_curled_short_of_a_stricter_fist_make_no_fist_but_stay_curled(reach: float) -> None:
    world = hand("fist").world
    for finger in FINGERS:
        world = with_reach(world, finger, reach)
    default = PoseTracker().classify(observation(world))
    strict = PoseTracker(thresholds_for(1.0, FIST_RANGE[0])).classify(observation(world))
    assert default.pose == "fist" and strict.pose != "fist"
    assert strict.curled == default.curled == (True, True, True, True)  # the same curled flags


def test_a_tighter_fist_leaves_the_extended_thresholds_alone() -> None:
    base = PoseThresholds()
    for s in grid(FIST_RANGE[0], 1.0, 6):
        th = thresholds_for(1.0, s)
        assert (th.extended_leave, th.extended_enter) == (base.extended_leave, base.extended_enter)


@pytest.mark.parametrize("bad", [0, -1.0, float("nan"), float("inf")])
def test_a_sensitivity_that_is_not_a_positive_number_is_refused(bad: float) -> None:
    with pytest.raises(ValueError):
        thresholds_for(bad, 1.0)
    with pytest.raises(ValueError):
        thresholds_for(1.0, bad)


@pytest.mark.parametrize("ratio", [0.14, 0.2, 0.26, 0.3, 0.34, 0.38, 0.45, 0.52])
def test_a_pinch_fires_below_the_scaled_close_threshold(ratio: float) -> None:
    base = hand("palm").world
    fired = [
        s
        for s in grid(*PINCH_RANGE, 29)
        if PoseTracker(thresholds_for(s)).classify(observation(with_pinch(base, ratio))).pose == "pinch"
    ]
    assert fired == [
        s for s in grid(*PINCH_RANGE, 29) if ratio < 0.28 * s
    ]  # every setting above ratio / 0.28, none below
    assert fired == sorted(fired)


def test_a_closed_pinch_lets_go_above_the_open_threshold() -> None:
    base = hand("palm").world
    for s in grid(*PINCH_RANGE, 9):
        th = thresholds_for(s)
        tracker = PoseTracker(th)
        assert tracker.classify(observation(with_pinch(base, 0.05))).pose == "pinch"
        assert tracker.classify(observation(with_pinch(base, th.pinch_open - 0.01))).pose == "pinch"
        assert tracker.classify(observation(with_pinch(base, th.pinch_open + 0.01))).pose == "palm"


@pytest.mark.parametrize("rest", [0.42, 0.45, 0.5, 0.6])
def test_a_closed_pinch_lets_go_into_a_hand_that_rests_its_thumb_near_the_index(rest: float) -> None:
    """At every setting, as at the default: a looser pinch must not hold a click until the thumb opens wide."""
    base = hand("palm").world
    for s in grid(*PINCH_RANGE, 9):
        tracker = PoseTracker(thresholds_for(s))
        assert tracker.classify(observation(with_pinch(base, 0.05))).pose == "pinch"
        assert tracker.classify(observation(with_pinch(base, rest))).pose == "palm", s


@pytest.mark.parametrize("reach", [0.9, 1.0, 1.05, 1.12, 1.17, 1.22, 1.28])
def test_a_fist_forms_below_the_scaled_curled_threshold(reach: float) -> None:
    fist = hand("fist").world
    world = fist
    for finger in FINGERS:
        world = with_reach(world, finger, reach)
    found = [
        s
        for s in grid(*FIST_RANGE, 21)
        if PoseTracker(thresholds_for(1.0, s)).classify(observation(world)).pose == "fist"
    ]
    assert found == [s for s in grid(*FIST_RANGE, 21) if reach < 1.10 * s]
    assert found == sorted(found)


def test_a_fist_lets_go_above_the_scaled_leave_threshold() -> None:
    fist = hand("fist").world

    def loosened(reach: float) -> np.ndarray:
        world = fist
        for finger in FINGERS:
            world = with_reach(world, finger, reach)
        return world

    for s in grid(*FIST_RANGE, 9):
        th = thresholds_for(1.0, s)
        leave = fist_thresholds(th)[1]
        tracker = PoseTracker(th)
        assert tracker.classify(observation(loosened(0.9))).pose == "fist"
        assert tracker.classify(observation(loosened(leave - 0.02))).pose == "fist"
        assert tracker.classify(observation(loosened(leave + 0.02))).pose != "fist"


#: The thumbs-up is left out of the tightest fist setting. These landmarks come from MediaPipe's WORLD output, whose
#: depth is poor: its pinky reaches 1.05 here, against 0.88 at most in the image landmarks the helper really
#: classifies (160 variants of the photo in test_poses_real), so a fist this loose needs a sensitivity of 0.96 or more.
RANGE_ENDS = [
    (name, pinch, fist)
    for name in REAL_EXPECTED
    for pinch in PINCH_RANGE
    for fist in FIST_RANGE
    if not (name == "thumb_up" and fist < 0.96)
]


@pytest.mark.parametrize(("name", "pinch", "fist"), RANGE_ENDS)
def test_mediapipes_hands_classify_the_same_at_the_ends_of_both_ranges(name: str, pinch: float, fist: float) -> None:
    pose = PoseTracker(thresholds_for(pinch, fist)).classify(observation(real_world(name))).pose
    assert pose == REAL_EXPECTED[name]


@pytest.mark.parametrize("seed", range(5))
def test_a_relaxed_synthetic_hand_never_pinches_or_grabs_at_the_loosest_settings(seed: int) -> None:
    rng = np.random.default_rng(seed)
    tracker = PoseTracker(thresholds_for(PINCH_RANGE[1], FIST_RANGE[1]))
    poses = {tracker.classify(hand("hover", (0.5, 0.45), jitter=0.002, rng=rng)).pose for _ in range(300)}
    assert poses == {"hover"}


@pytest.mark.parametrize("name", ["palm", "two", "point", "thumb_up"])
def test_the_other_synthetic_poses_keep_their_name_at_the_loosest_settings(name: str) -> None:
    tracker = PoseTracker(thresholds_for(PINCH_RANGE[1], FIST_RANGE[1]))
    assert tracker.classify(hand(name)).pose == EXPECTED[name]


@pytest.mark.parametrize("name", ["pinch", "pinch_middle", "fist"])
def test_the_synthetic_pinches_and_fist_hold_at_the_strictest_settings(name: str) -> None:
    tracker = PoseTracker(thresholds_for(PINCH_RANGE[0], FIST_RANGE[0]))
    assert tracker.classify(hand(name)).pose == EXPECTED[name]
