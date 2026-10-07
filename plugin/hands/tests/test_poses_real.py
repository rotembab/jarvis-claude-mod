"""Poses on MediaPipe's own test photos, through the real tracker. Optional.

Skipped unless ``JARVIS_HANDS_MODELS_DIR`` is set (CI sets it). The model and
the photos are downloaded into that directory when missing; the model is
checked against its sha256. Each photo goes through ``MediaPipeTracker`` raw
and unflipped, like a camera frame, and its poses through ``PoseTracker``
with the photo's aspect, exactly as the runtime does. A still photo is one
frame, so every photo gets a tracker of its own.
"""

from __future__ import annotations

import hashlib
import os
import urllib.request
from pathlib import Path

import cv2
import pytest

from jarvis_hands import models
from jarvis_hands.landmarks import Frame
from jarvis_hands.poses import PoseTracker
from jarvis_hands.tracker.mediapipe_tracker import MediaPipeTracker

MODELS_ENV = "JARVIS_HANDS_MODELS_DIR"
IMAGE_URL = "https://storage.googleapis.com/mediapipe-assets/{name}.jpg"

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

pytestmark = pytest.mark.skipif(not os.environ.get(MODELS_ENV), reason=f"set {MODELS_ENV} to run the real model")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fetch(url: str, path: Path, sha256: str | None = None) -> Path:
    if path.exists() and (sha256 is None or _sha256(path) == sha256):
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".part")
    with urllib.request.urlopen(url, timeout=120) as response:
        partial.write_bytes(response.read())
    if sha256 is not None and _sha256(partial) != sha256:
        partial.unlink()
        raise AssertionError(f"{url} does not match its sha256")
    os.replace(partial, path)
    return path


@pytest.fixture(scope="module")
def models_dir() -> Path:
    return Path(os.environ[MODELS_ENV])


@pytest.fixture(scope="module")
def model(models_dir: Path) -> Path:
    return _fetch(models.MODEL_URL, models_dir / models.MODEL_NAME, models.MODEL_SHA256)


def track(model: Path, models_dir: Path, name: str) -> Frame:
    """One test photo, as the camera would hand it over (BGR, unflipped), through a fresh tracker."""
    path = _fetch(IMAGE_URL.format(name=name), models_dir / f"{name}.jpg")
    image = cv2.imread(str(path))
    assert image is not None, path
    tracker = MediaPipeTracker(model, num_hands=2)
    try:
        return tracker.process(image, 1.0)
    finally:
        tracker.close()


@pytest.mark.parametrize("name", list(EXPECTED))
def test_poses_on_mediapipe_test_photos(model: Path, models_dir: Path, name: str) -> None:
    frame = track(model, models_dir, name)
    aspect = frame.height / frame.width
    poses = sorted(PoseTracker().classify(hand, aspect=aspect).pose for hand in frame.hands)
    assert poses == EXPECTED[name]


def test_handedness_names_the_hand_as_photographed(model: Path, models_dir: Path) -> None:
    """right_hands.jpg shows two right hands, photographed directly (not a selfie); left_hands.jpg is its mirror.

    MediaPipe Tasks labels the raw frame anatomically, and the tracker keeps that label while it mirrors
    the landmarks, so ``handedness`` names the user's own hand.
    """
    assert [h.handedness for h in track(model, models_dir, "right_hands").hands] == ["right", "right"]
    assert [h.handedness for h in track(model, models_dir, "left_hands").hands] == ["left", "left"]
