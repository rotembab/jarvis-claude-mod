"""Hand tracking: camera frames in, ``Frame`` (tracked hands) out.

Coordinates follow the user's view, like a mirror: the backend runs the model
on the raw camera frame (MediaPipe's handedness is anatomical there) and then
mirrors the landmarks (image ``x' = 1 - x``, world ``x' = -x``), so moving the
hand to the right moves the cursor right and a right hand stays "right".
"""

from __future__ import annotations

from typing import Literal, Protocol

import numpy as np

from ..landmarks import Frame

TrackerErrorCode = Literal["model_missing", "tracker_failed"]


class TrackerError(RuntimeError):
    """The tracker cannot run; ``code`` is the protocol's ErrorCode for it."""

    def __init__(self, code: TrackerErrorCode, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.code: TrackerErrorCode = code
        self.message = message
        self.hint = hint


class Tracker(Protocol):
    def process(self, image: np.ndarray, t: float) -> Frame:
        """The hands in one (H, W, 3) uint8 BGR frame captured at ``t`` (``time.monotonic()``).

        Never raises for a bad frame: it returns an empty Frame and recovers.
        Raises TrackerError only when the model cannot run at all.
        """
        ...

    @property
    def num_hands(self) -> int: ...

    def set_num_hands(self, n: int) -> None:
        """How many hands to look for (1 or 2). Two costs about twice the CPU while one hand is in view."""
        ...

    @property
    def infer_ms(self) -> float:
        """Mean model time per frame over the last few seconds (0 before the first frame)."""
        ...

    def close(self) -> None:
        """Frees the model. Idempotent; never raises; returns within about two seconds."""
        ...
