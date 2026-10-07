"""Poses on MediaPipe's own test photos, through the real tracker. Optional.

Skipped unless ``JARVIS_HANDS_MODELS_DIR`` is set (CI sets it). Conftest
downloads the model and the photos into that directory when missing; the
model is checked against its sha256. Each photo goes through ``MediaPipeTracker`` raw
and unflipped, like a camera frame, and its poses through ``PoseTracker``
with the photo's aspect, exactly as the runtime does. A still photo is one
frame, so every photo gets a tracker of its own.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import pytest

from jarvis_hands.landmarks import Frame
from jarvis_hands.poses import PoseTracker
from jarvis_hands.tracker.mediapipe_tracker import MediaPipeTracker

from conftest import fetch_photo, real_model

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
