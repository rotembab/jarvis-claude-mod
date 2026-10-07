"""What the tracker hands the gesture engine: hands per camera frame.

The tracker mirrors the landmarks (not the frame: MediaPipe's handedness is
anatomical on the raw frame), so image x grows to the user's right and
``handedness`` names the user's own hand.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np

Handedness = Literal["left", "right"]

WRIST = 0
THUMB_CMC, THUMB_MCP, THUMB_IP, THUMB_TIP = 1, 2, 3, 4
INDEX_MCP, INDEX_PIP, INDEX_DIP, INDEX_TIP = 5, 6, 7, 8
MIDDLE_MCP, MIDDLE_PIP, MIDDLE_DIP, MIDDLE_TIP = 9, 10, 11, 12
RING_MCP, RING_PIP, RING_DIP, RING_TIP = 13, 14, 15, 16
PINKY_MCP, PINKY_PIP, PINKY_DIP, PINKY_TIP = 17, 18, 19, 20

#: (MCP, TIP) per finger, thumb excluded.
FINGERS: dict[str, tuple[int, int]] = {
    "index": (INDEX_MCP, INDEX_TIP),
    "middle": (MIDDLE_MCP, MIDDLE_TIP),
    "ring": (RING_MCP, RING_TIP),
    "pinky": (PINKY_MCP, PINKY_TIP),
}


@dataclass(frozen=True, eq=False)
class HandObservation:
    handedness: Handedness
    #: Handedness confidence, 0..1.
    score: float
    #: (21, 3): normalized x, y in the mirrored frame, z relative depth.
    image: np.ndarray
    #: (21, 3): metres, origin at the hand's centre.
    world: np.ndarray

    def __post_init__(self) -> None:
        if self.image.shape != (21, 3) or self.world.shape != (21, 3):
            raise ValueError(f"landmarks must be (21, 3), got {self.image.shape} and {self.world.shape}")


@dataclass(frozen=True, eq=False)
class Frame:
    #: Capture time, ``clock.now()`` seconds.
    t: float
    hands: tuple[HandObservation, ...]
    #: Camera frame size in pixels.
    width: int = 1280
    height: int = 720
