"""Poses on MediaPipe's own test photos, through the real hand landmarker. Optional.

Skipped unless ``JARVIS_HANDS_MODELS_DIR`` is set (CI sets it). The model and
the photos are downloaded into that directory when missing; the model is
checked against its sha256. The frames go through the same mirroring the
tracker applies, and also unmirrored, since a camera may already mirror.
"""

from __future__ import annotations

import hashlib
import os
import urllib.request
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from jarvis_hands.landmarks import HandObservation
from jarvis_hands.poses import PoseTracker

MODELS_ENV = "JARVIS_HANDS_MODELS_DIR"
MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task"
)
MODEL_SHA256 = "fbc2a30080c3c557093b5ddfc334698132eb341044ccee322ccf8bcf3607cde1"
IMAGE_URL = "https://storage.googleapis.com/mediapipe-assets/{name}.jpg"

EXPECTED = {
    "fist": ["fist"],
    "thumb_up": ["fist"],
    "pointing_up": ["hover"],
    "victory": ["two"],
    "right_hands": ["palm", "palm"],
    "left_hands": ["palm", "palm"],
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
def landmarker(models_dir: Path) -> Any:
    from mediapipe.tasks.python import BaseOptions, vision

    model = _fetch(MODEL_URL, models_dir / "hand_landmarker.task", MODEL_SHA256)
    options = vision.HandLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=str(model)),
        running_mode=vision.RunningMode.IMAGE,
        num_hands=2,
    )
    detector = vision.HandLandmarker.create_from_options(options)
    yield detector
    detector.close()


def detect(landmarker: Any, models_dir: Path, name: str, *, mirror: bool) -> list[tuple[str, HandObservation]]:
    """MediaPipe's own handedness label and the HandObservation for each hand in a test photo."""
    import cv2
    import mediapipe as mp

    path = _fetch(IMAGE_URL.format(name=name), models_dir / f"{name}.jpg")
    bgr = cv2.imread(str(path))
    assert bgr is not None, path
    if mirror:
        bgr = cv2.flip(bgr, 1)
    rgb = np.ascontiguousarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
    result = landmarker.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb))
    hands = []
    for i, world in enumerate(result.hand_world_landmarks):
        category = result.handedness[i][0]
        label = category.category_name.lower()
        # The landmarker names the hand as seen in an unmirrored photo; in a mirrored frame that is the other one.
        user_hand = ("left" if label == "right" else "right") if mirror else label
        hands.append(
            (
                label,
                HandObservation(
                    handedness=user_hand,
                    score=float(category.score),
                    image=np.array([[p.x, p.y, p.z] for p in result.hand_landmarks[i]], dtype=float),
                    world=np.array([[p.x, p.y, p.z] for p in world], dtype=float),
                ),
            )
        )
    return hands


@pytest.mark.parametrize("mirror", [True, False], ids=["mirrored", "unmirrored"])
@pytest.mark.parametrize("name", list(EXPECTED))
def test_poses_on_mediapipe_test_photos(landmarker: Any, models_dir: Path, name: str, mirror: bool) -> None:
    hands = detect(landmarker, models_dir, name, mirror=mirror)
    poses = sorted(PoseTracker().classify(obs).pose for _, obs in hands)
    assert poses == EXPECTED[name]


def test_handedness_labels_name_the_hand_as_photographed(landmarker: Any, models_dir: Path) -> None:
    """right_hands.jpg shows two right hands, photographed directly (not a selfie).

    MediaPipe Tasks labels them "right"; mirrored, as the tracker feeds frames, it labels them "left". So
    the tracker must swap the label after mirroring for ``handedness`` to name the user's own hand.
    """
    assert [label for label, _ in detect(landmarker, models_dir, "right_hands", mirror=False)] == ["right", "right"]
    assert [label for label, _ in detect(landmarker, models_dir, "right_hands", mirror=True)] == ["left", "left"]
