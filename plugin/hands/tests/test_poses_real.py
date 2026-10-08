"""Poses on MediaPipe's own test photos, through the real tracker. Optional.

Skipped unless ``JARVIS_HANDS_MODELS_DIR`` is set (CI sets it). Conftest
downloads the model and the photos into that directory when missing; the
model is checked against its sha256. Each photo goes through ``MediaPipeTracker`` raw
and unflipped, like a camera frame, and its poses through ``PoseTracker``
with the photo's aspect, exactly as the runtime does. A still photo is one
frame, so every photo gets a tracker of its own.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np
import pytest

from jarvis_hands.actions import Button
from jarvis_hands.desktop.fake import FakeDesktop
from jarvis_hands.gestures import GestureEngine
from jarvis_hands.landmarks import INDEX_TIP, MIDDLE_TIP, Frame, HandObservation
from jarvis_hands.mapping import ScreenMapper
from jarvis_hands.poses import PoseThresholds, PoseTracker, finger_reach, pinch_ratio, pose_points, thresholds_for
from jarvis_hands.settings import KNOB_BY_KEY, HandsSettings
from jarvis_hands.tracker.mediapipe_tracker import MediaPipeTracker

from conftest import fetch_photo, real_model
from scripted import display
from scripted import hand as synthetic_hand

#: The poses of the hands in each photo, sorted.
EXPECTED = {
    "fist": ["fist"],
    "thumb_up": ["fist"],  # the thumb does not count: four curled fingers
    "pointing_up": ["hover"],
    "victory": ["two"],
    "right_hands": ["palm", "palm"],
    "left_hands": ["palm", "palm"],
    "man-woman-okay": ["pinch", "pinch"],  # OK signs: thumb and index tips touching
    "woman_hands": ["palm", "palm"],
    "hand-woman-man": ["hover", "palm"],  # one open hand, one relaxed (fingers neither straight nor curled)
}

pytestmark = real_model


def track(model: Path, name: str) -> Frame:
    """One test photo, as the camera would hand it over (BGR, unflipped), through a fresh tracker."""
    path = fetch_photo(name)
    image = cv2.imread(str(path))
    assert image is not None, path
    tracker = MediaPipeTracker(model, num_hands=2)
    try:
        return tracker.process(image, 1.0)
    finally:
        tracker.close()


@pytest.mark.parametrize("name", list(EXPECTED))
def test_poses_on_mediapipe_test_photos(real_model_path: Path, name: str) -> None:
    frame = track(real_model_path, name)
    aspect = frame.height / frame.width
    poses = sorted(PoseTracker().classify(hand, aspect=aspect).pose for hand in frame.hands)
    assert poses == EXPECTED[name]


def test_handedness_names_the_hand_as_photographed(real_model_path: Path) -> None:
    """right_hands.jpg shows two right hands, photographed directly (not a selfie); left_hands.jpg is its mirror.

    MediaPipe Tasks labels the raw frame anatomically, and the tracker keeps that label while it mirrors
    the landmarks, so ``handedness`` names the user's own hand.
    """
    assert [h.handedness for h in track(real_model_path, "right_hands").hands] == ["right", "right"]
    assert [h.handedness for h in track(real_model_path, "left_hands").hands] == ["left", "left"]


# -- the pinch and fist sensitivity knobs on the real model ---------------------------------------------------

PINCH_LOW, PINCH_HIGH = KNOB_BY_KEY["pinch"].low, KNOB_BY_KEY["pinch"].high
FIST_LOW, FIST_HIGH = KNOB_BY_KEY["fist"].low, KNOB_BY_KEY["fist"].high


def poses_at(model: Path, name: str, pinch: float, fist: float) -> list[str]:
    frame = track(model, name)
    aspect = frame.height / frame.width
    return sorted(PoseTracker(thresholds_for(pinch, fist)).classify(hand, aspect=aspect).pose for hand in frame.hands)


@pytest.mark.parametrize(
    ("pinch", "fist"), [(PINCH_LOW, FIST_LOW), (PINCH_HIGH, FIST_HIGH), (PINCH_LOW, FIST_HIGH), (PINCH_HIGH, FIST_LOW)]
)
@pytest.mark.parametrize("name", list(EXPECTED))
def test_the_photos_read_the_same_at_the_ends_of_both_ranges(
    real_model_path: Path, name: str, pinch: float, fist: float
) -> None:
    """The OK signs still pinch at the strictest pinch setting; nothing else pinches or grabs at the loosest."""
    assert poses_at(real_model_path, name, pinch, fist) == EXPECTED[name]


def features(model: Path, name: str) -> list[tuple[tuple[float, ...], float, float]]:
    frame = track(model, name)
    out = []
    for hand in frame.hands:
        points = pose_points(hand.image, frame.height / frame.width)
        out.append((finger_reach(points), pinch_ratio(points, INDEX_TIP), pinch_ratio(points, MIDDLE_TIP)))
    return out


def test_the_margins_that_set_the_ends_of_the_ranges(real_model_path: Path) -> None:
    """Why the ranges end where they do, as numbers measured on the photos (see settings.KNOBS)."""
    base = thresholds_for()
    # the tightest pinch must still fire on both OK signs, with a margin: their thumb-to-tip ratios are 0.14 and 0.22
    okay = [pi for _, pi, _ in features(real_model_path, "man-woman-okay")]
    assert max(okay) < base.pinch_close * PINCH_LOW * 0.97
    # the loosest pinch must stay well short of the closest thumb-to-tip ratio of a hand that is not pinching: every
    # hand in the photo set that EXPECTED calls a palm, a relaxed hand, a point or a fist-less pose (woman_hands'
    # relaxed palms rest the thumb against the index, at 0.39 and 0.47: the closest of all)
    not_pinching = ("hand-woman-man", "victory", "right_hands", "left_hands", "woman_hands")
    others = [pm for name in not_pinching for _, _, pm in features(real_model_path, name)]
    others += [pi for name in (*not_pinching, "pointing_up") for _, pi, _ in features(real_model_path, name)]
    assert min(others) < 0.45  # (the hand that sets the margin is woman_hands' 0.39)
    assert base.pinch_close * PINCH_HIGH < 0.85 * min(others)
    # the tightest fist must still take the loosest finger of the real fists,
    # the loosest must leave an OK sign's index alone
    loosest_fist = max(max(reach) for name in ("fist", "thumb_up") for reach, _, _ in features(real_model_path, name))
    assert loosest_fist < base.curled_enter * FIST_LOW * 0.95
    okay_index = min(reach[0] for reach, _, _ in features(real_model_path, "man-woman-okay"))
    assert okay_index > base.curled_enter * FIST_HIGH * 1.02


def variant(image: np.ndarray, angle: float, scale: float, gain: float, noise: float, seed: int) -> np.ndarray:
    h, w = image.shape[:2]
    matrix = cv2.getRotationMatrix2D((w / 2, h / 2), angle, scale)
    out = cv2.warpAffine(image, matrix, (w, h), borderMode=cv2.BORDER_REFLECT).astype(float) * gain
    out += np.random.default_rng(seed).normal(0, noise, out.shape)
    return np.clip(out, 0, 255).astype(np.uint8)


VARIANTS = [(-12.0, 0.9, 1.0, 6.0), (10.0, 1.0, 0.85, 9.0), (0.0, 1.1, 1.1, 4.0), (18.0, 0.8, 1.0, 12.0)]


@pytest.mark.parametrize("variant_args", VARIANTS, ids=str)
@pytest.mark.parametrize(
    ("name", "wanted"),
    [
        ("fist", {"fist"}),
        ("thumb_up", {"fist"}),
        ("pointing_up", {"hover"}),
        ("victory", {"two"}),
        ("right_hands", {"palm"}),
        ("woman_hands", {"palm"}),  # relaxed palms with the thumb against the index: the closest to a pinch
        ("man-woman-okay", {"pinch"}),
    ],
)
def test_rotated_scaled_and_noisy_photos_keep_their_pose_at_the_ends_of_the_ranges(
    real_model_path: Path, name: str, wanted: set[str], variant_args: tuple[float, float, float, float]
) -> None:
    """A little of what a webcam does to the photo (tilt, distance, light, sensor noise).

    The tilted photos can be missed or read a little worse than the plain one, so this only checks that no setting
    in the ranges turns what the model sees into a pinch or a fist that is not there, or the other way round at the
    tightest end where the photo read the same at the default.
    """
    angle, scale, gain, noise = variant_args
    image = variant(cv2.imread(str(fetch_photo(name))), angle, scale, gain, noise, seed=3)
    tracker = MediaPipeTracker(real_model_path, num_hands=2)
    try:
        frame = tracker.process(image, 1.0)
    finally:
        tracker.close()
    aspect = frame.height / frame.width

    def poses(pinch: float, fist: float) -> set[str]:
        return {PoseTracker(thresholds_for(pinch, fist)).classify(hand, aspect=aspect).pose for hand in frame.hands}

    if not frame.hands:
        pytest.skip("the model found no hand in this variant")
    read_at_default = poses(1.0, 1.0)
    for pinch, fist in ((PINCH_LOW, FIST_LOW), (PINCH_HIGH, FIST_HIGH)):
        read = poses(pinch, fist)
        if name != "man-woman-okay" and name not in ("fist", "thumb_up"):
            assert not read & {"pinch", "pinch_middle", "fist"}  # nothing here pinches or grabs, at any setting
        if name in ("fist", "thumb_up") and (pinch, fist) == (PINCH_HIGH, FIST_HIGH):
            assert read == {"fist"} or read_at_default != {"fist"}
        if name == "man-woman-okay" and (pinch, fist) == (PINCH_HIGH, FIST_HIGH) and "pinch" in read_at_default:
            assert "pinch" in read


def grid(low: float, high: float, n: int) -> list[float]:
    return [float(v) for v in np.linspace(low, high, n)]


@pytest.mark.parametrize("name", ["woman_hands", "right_hands", "left_hands", "hand-woman-man"])
def test_a_held_pinch_lets_go_into_every_real_open_hand_at_every_pinch_setting(
    real_model_path: Path, name: str
) -> None:
    """The pinch ends when the thumb leaves the index, wherever a real hand rests it.

    woman_hands' relaxed palms rest the thumb 0.39 and 0.47 palm sizes from the index tip. If a looser pinch
    setting raised the pinch's open threshold with its close one, the 0.47 hand would hold a pinch (a button)
    forever. (The 0.39 hand sits under the default open threshold, 0.40, on main as well: it only lets go with
    the noise of a live hand, or at a stricter setting, so it is left out here.)
    """
    frame = track(real_model_path, name)
    aspect = frame.height / frame.width
    hands = [
        obs
        for obs, (_, ratio, _) in zip(frame.hands, features(real_model_path, name), strict=True)
        if ratio > PoseThresholds().pinch_open * 1.02
    ]
    assert hands, name
    for pinch in grid(PINCH_LOW, PINCH_HIGH, 9):
        for obs in hands:
            tracker = PoseTracker(thresholds_for(pinch, 1.0))
            assert tracker.classify(synthetic_hand("pinch")).pose == "pinch"
            read = tracker.classify(obs, aspect=aspect).pose
            assert read in ("palm", "hover"), (name, pinch, read)


def jittered_frames(frame: Frame, base: HandObservation, jitter: float) -> Iterator[Frame]:
    """One real hand of ``frame`` again and again at 30 fps, with the landmark noise of a live hand."""
    rng = np.random.default_rng(7)
    aspect = frame.height / frame.width
    t = 100.0
    while True:
        image = base.image.copy()
        image[:, 0] += rng.normal(0, jitter, 21)
        image[:, 1] += rng.normal(0, jitter / aspect, 21)
        t += 1 / 30
        yield Frame(t, (replace(base, image=image),), frame.width, frame.height)


@pytest.mark.parametrize("jitter", [0.0, 0.001])
@pytest.mark.parametrize("hand_index", [0, 1])
def test_a_real_relaxed_open_palm_engages_and_never_clicks_at_the_loosest_pinch(
    real_model_path: Path, hand_index: int, jitter: float
) -> None:
    """woman_hands' palms, jittered like a live hand, through the engine at the loosest pinch setting.

    They rest the thumb against the index (0.47 and 0.39 palm sizes): a pinch range that reached 1.4 read the
    0.39 one as a pinch (no engagement from cold, and a left button pressed and never released once engaged).
    """
    frame = track(real_model_path, "woman_hands")
    for engaged_first in (False, True):
        frames = jittered_frames(frame, frame.hands[hand_index], jitter)
        settings = HandsSettings()
        desktop = FakeDesktop([display(1, 0, 0, 1920, 1080, primary=True)])
        engine = GestureEngine(
            settings, ScreenMapper(desktop.displays(), settings), double_click=desktop.double_click()
        )
        if engaged_first:  # engage at the default, then raise the setting (the live-apply path)
            for _ in range(45):
                engine.update(next(frames))
            assert engine.engaged
            settings.apply_config({"pinch": PINCH_HIGH})
        else:  # engage from cold at the loosest setting
            settings.apply_config({"pinch": PINCH_HIGH})
            for _ in range(75):
                engine.update(next(frames))
            assert engine.engaged
        buttons = [a for _ in range(150) for a in engine.update(next(frames)) if isinstance(a, Button)]
        assert buttons == []
