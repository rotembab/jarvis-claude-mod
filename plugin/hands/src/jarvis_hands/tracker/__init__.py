"""Hand trackers: ``create_tracker`` gives MediaPipe's; ``fake.FakeTracker`` stands in for tests and ``--fake``."""

from __future__ import annotations

from pathlib import Path

from .base import Tracker, TrackerError

__all__ = ["Tracker", "TrackerError", "create_tracker"]


def create_tracker(model_path: Path, *, num_hands: int = 1) -> Tracker:
    """Loads the hand landmark model. Raises TrackerError ("model_missing" or "tracker_failed")."""
    from .mediapipe_tracker import MediaPipeTracker

    return MediaPipeTracker(model_path, num_hands=num_hands)
