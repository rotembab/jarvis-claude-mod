"""Synthetic hands: a small kinematic hand model, for tests and ``run --fake``.

The hands come from a model in metres (MediaPipe's world landmark frame: x
right, y down, z away from the camera, centred on the palm): four fingers
with three flexion angles each and a thumb whose tip is placed where the pose
puts it (on the index tip for a pinch, tucked over the fingers for a fist).
The angles are chosen so the features land where MediaPipe's own test photos
put them: straight fingers reach 1.8 to 2.0, curled ones 0.7 to 0.95, a
relaxed hand sits in between, an open thumb is about one palm away from the
index tip.

The image landmarks are the same hand projected (orthographic, ``scale``
pixels per metre) so that the anchor, the mean of the index and middle
knuckles, lands exactly on ``at``: a pinch at ``at`` does not move the cursor
unless the script moves it. Like the tracker's output, the hands are already
in the mirrored frame, so ``handedness`` is the user's own hand.

It lives in the package rather than the tests so that ``run --fake`` can
replay a script of poses through the whole runtime without a camera or model.
"""

from __future__ import annotations

import math
from typing import Literal

import numpy as np

from .geometry import Point
from .landmarks import Frame, HandObservation

SyntheticPose = Literal["palm", "fist", "pinch", "pinch_middle", "two", "point", "hover", "thumb_up"]
POSES: tuple[SyntheticPose, ...] = ("palm", "fist", "pinch", "pinch_middle", "two", "point", "hover", "thumb_up")

#: The camera frame the synthetic hands are projected into by default.
WIDTH, HEIGHT = 1280, 720
#: Image pixels per metre for a hand about 60 cm from a 1280-wide webcam.
SCALE = 1400.0

# Right hand, palm towards the camera, in the mirrored frame: the thumb is on the image's left.
_WRIST = np.array([0.0, 0.0, 0.0])
_THUMB_CMC = np.array([-0.025, -0.025, 0.0])
_MCP = {
    "index": np.array([-0.025, -0.085, 0.0]),
    "middle": np.array([-0.003, -0.090, 0.0]),
    "ring": np.array([0.017, -0.085, 0.0]),
    "pinky": np.array([0.034, -0.075, 0.0]),
}
_BONES = {
    "index": (0.040, 0.024, 0.020),
    "middle": (0.045, 0.028, 0.021),
    "ring": (0.042, 0.027, 0.020),
    "pinky": (0.033, 0.019, 0.018),
}
_FIRST = {"index": 5, "middle": 9, "ring": 13, "pinky": 17}

#: Flexion of the MCP, PIP and DIP joints, in degrees.
STRAIGHT = (0.0, 5.0, 3.0)
CURLED = (70.0, 85.0, 50.0)
RELAXED = (50.0, 60.0, 30.0)
PINCHING = (35.0, 45.0, 25.0)

_FINGER_ANGLES: dict[str, dict[str, tuple[float, float, float]]] = {
    "palm": dict.fromkeys(_MCP, STRAIGHT),
    "fist": dict.fromkeys(_MCP, CURLED),
    "thumb_up": dict.fromkeys(_MCP, CURLED),
    "point": {"index": STRAIGHT, "middle": CURLED, "ring": CURLED, "pinky": CURLED},
    "two": {"index": STRAIGHT, "middle": STRAIGHT, "ring": CURLED, "pinky": CURLED},
    "pinch": {"index": PINCHING, "middle": RELAXED, "ring": RELAXED, "pinky": RELAXED},
    "pinch_middle": {"index": STRAIGHT, "middle": PINCHING, "ring": RELAXED, "pinky": RELAXED},
    "hover": dict.fromkeys(_MCP, RELAXED),
}


def _finger(name: str, angles: tuple[float, float, float]) -> list[np.ndarray]:
    """MCP, PIP, DIP, TIP of one finger flexed by ``angles`` (degrees) towards the palm (-z)."""
    mcp = _MCP[name]
    direction = mcp[:2] / np.linalg.norm(mcp[:2])
    points = [mcp]
    total = 0.0
    for length, angle in zip(_BONES[name], angles, strict=True):
        total += math.radians(angle)
        step = np.array([direction[0] * math.cos(total), direction[1] * math.cos(total), -math.sin(total)])
        points.append(points[-1] + length * step)
    return points


def _thumb(tip: np.ndarray) -> list[np.ndarray]:
    """CMC, MCP, IP, TIP with the tip at ``tip``, bowed slightly outwards like a real thumb."""
    cmc = _THUMB_CMC
    span = tip - cmc
    bow = np.array([-0.008, 0.004, 0.0])
    return [cmc, cmc + 0.42 * span + bow, cmc + 0.74 * span + 0.6 * bow, tip]


def world_landmarks(pose: SyntheticPose, handedness: Literal["left", "right"] = "right") -> np.ndarray:
    """(21, 3) metric landmarks for ``pose``, centred on the palm."""
    if pose not in _FINGER_ANGLES:
        raise ValueError(f"unknown synthetic pose {pose!r}; expected one of {', '.join(POSES)}")
    fingers = {name: _finger(name, angles) for name, angles in _FINGER_ANGLES[pose].items()}
    if pose == "palm" or pose == "hover":
        thumb_tip = np.array([-0.072, -0.085, -0.005])
    elif pose == "pinch":
        thumb_tip = fingers["index"][3] + np.array([-0.004, 0.003, 0.002])
    elif pose == "pinch_middle":
        thumb_tip = fingers["middle"][3] + np.array([-0.004, 0.003, 0.002])
    elif pose == "two":
        thumb_tip = fingers["ring"][2] + np.array([-0.006, 0.0, -0.012])
    elif pose == "point":
        thumb_tip = fingers["middle"][2] + np.array([-0.006, 0.0, -0.012])
    elif pose == "thumb_up":
        thumb_tip = np.array([-0.045, -0.125, -0.020])
    else:  # fist: the thumb lies across the curled index and middle fingers
        thumb_tip = (fingers["index"][2] + fingers["middle"][2]) / 2 + np.array([0.0, 0.004, -0.014])
    points = np.zeros((21, 3))
    points[0] = _WRIST
    points[1:5] = _thumb(thumb_tip)
    for name, joints in fingers.items():
        points[_FIRST[name] : _FIRST[name] + 4] = joints
    if handedness == "left":
        points[:, 0] *= -1
    return points - points[[0, 5, 9, 13, 17]].mean(axis=0)


def hand(
    pose: SyntheticPose = "palm",
    at: tuple[float, float] | Point = (0.5, 0.45),
    *,
    handedness: Literal["left", "right"] = "right",
    score: float = 0.95,
    scale: float = SCALE,
    size: tuple[int, int] = (WIDTH, HEIGHT),
    roll: float = 0.0,
    jitter: float = 0.0,
    rng: np.random.Generator | None = None,
) -> HandObservation:
    """A hand in ``pose`` whose anchor (mean of image landmarks 5 and 9) is at ``at``.

    ``size`` is the camera frame (width, height) the hand is projected into;
    ``roll`` tilts the hand in the image plane (degrees); ``jitter`` adds
    Gaussian noise of that many frame widths to the image landmarks.
    """
    world = world_landmarks(pose, handedness)
    ax, ay = (at.x, at.y) if isinstance(at, Point) else at
    c, s = math.cos(math.radians(roll)), math.sin(math.radians(roll))
    rotation = np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])
    rotated = world @ rotation.T
    knuckles = (rotated[5] + rotated[9]) / 2
    width, height = size
    image = np.empty((21, 3))
    image[:, 0] = ax + (rotated[:, 0] - knuckles[0]) * scale / width
    image[:, 1] = ay + (rotated[:, 1] - knuckles[1]) * scale / height
    image[:, 2] = (rotated[:, 2] - rotated[0, 2]) * scale / width
    if jitter:
        rng = rng or np.random.default_rng(0)
        noise = rng.normal(0.0, jitter, size=(21, 2))
        image[:, 0] += noise[:, 0]
        image[:, 1] += noise[:, 1] * width / height
    return HandObservation(handedness=handedness, score=score, image=image, world=rotated)


def frame(t: float, *hands: HandObservation, size: tuple[int, int] = (WIDTH, HEIGHT)) -> Frame:
    """A tracked frame at ``t`` holding ``hands`` (build them with the same ``size``)."""
    return Frame(t, tuple(hands), size[0], size[1])
