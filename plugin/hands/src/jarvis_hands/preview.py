"""``preview``: the camera picture with the tracked hands drawn on it, for checking the setup by eye.

It shows what the gesture engine sees: the picture mirrored (the tracker
mirrors its landmarks, so they line up with a mirrored picture and the
picture reads like a mirror), each hand's skeleton and anchor, the pose the
classifier gives it with the frame's aspect (as the engine does), and the
tracked frame rate and model time. Two hands are tracked, so both show.

The model is checked before the camera is opened, so a missing model never
turns the webcam light on for nothing. ``q``, Esc or closing the window ends
it; the camera and the model are closed on every way out.
"""

from __future__ import annotations

import argparse
import logging
from collections import deque
from pathlib import Path
from typing import Any

import numpy as np

from . import clock
from .landmarks import INDEX_MCP, MIDDLE_MCP, Frame
from .poses import PoseTracker

log = logging.getLogger(__name__)

WINDOW = "Jarvis hands preview"
#: MediaPipe's hand skeleton.
HAND_CONNECTIONS: tuple[tuple[int, int], ...] = (
    (0, 1), (1, 2), (2, 3), (3, 4),
    (0, 5), (5, 6), (6, 7), (7, 8),
    (5, 9), (9, 10), (10, 11), (11, 12),
    (9, 13), (13, 14), (14, 15), (15, 16),
    (13, 17), (0, 17), (17, 18), (18, 19), (19, 20),
)  # fmt: skip
#: BGR: the user's right hand cyan, the left one amber.
HAND_COLOURS = {"right": (255, 200, 0), "left": (0, 170, 255)}
TEXT_COLOUR = (255, 255, 255)
SHADOW_COLOUR = (0, 0, 0)
KEYS_TO_QUIT = frozenset({ord("q"), ord("Q"), 27})  # 27: Esc


class FpsMeter:
    """Frames per second over the last ``window`` seconds."""

    def __init__(self, window: float = 2.0) -> None:
        self.window = window
        self._times: deque[float] = deque()

    def tick(self, now: float) -> float:
        self._times.append(now)
        while self._times and now - self._times[0] > self.window:
            self._times.popleft()
        span = self._times[-1] - self._times[0]
        return (len(self._times) - 1) / span if span > 0 else 0.0


def annotate(image: np.ndarray, frame: Frame, labels: list[str], fps: float, infer_ms: float) -> np.ndarray:
    """Draw ``frame``'s hands (and one label per hand) onto the mirrored ``image``, in place; returns it."""
    import cv2

    height, width = image.shape[:2]
    scale = max(0.5, width / 1280)
    thickness = max(1, round(2 * scale))
    for hand, label in zip(frame.hands, labels, strict=False):
        colour = HAND_COLOURS.get(hand.handedness, TEXT_COLOUR)
        points = [(round(float(x) * width), round(float(y) * height)) for x, y in hand.image[:, :2]]
        for a, b in HAND_CONNECTIONS:
            cv2.line(image, points[a], points[b], colour, thickness, cv2.LINE_AA)
        for p in points:
            cv2.circle(image, p, thickness + 2, colour, -1, cv2.LINE_AA)
        anchor = (
            round(float(hand.image[INDEX_MCP, 0] + hand.image[MIDDLE_MCP, 0]) / 2 * width),
            round(float(hand.image[INDEX_MCP, 1] + hand.image[MIDDLE_MCP, 1]) / 2 * height),
        )
        cv2.circle(image, anchor, round(10 * scale), TEXT_COLOUR, thickness, cv2.LINE_AA)
        wrist = points[0]
        _text(image, label, (wrist[0] - round(40 * scale), wrist[1] + round(30 * scale)), scale)
    status = f"{fps:4.1f} fps  model {infer_ms:4.1f} ms  hands {len(frame.hands)}  (q or Esc to close)"
    _text(image, status, (round(12 * scale), round(30 * scale)), scale)
    return image


def _text(image: np.ndarray, text: str, origin: tuple[int, int], scale: float) -> None:
    import cv2

    size = 0.7 * scale
    cv2.putText(
        image, text, origin, cv2.FONT_HERSHEY_SIMPLEX, size, SHADOW_COLOUR, max(2, round(4 * scale)), cv2.LINE_AA
    )
    cv2.putText(image, text, origin, cv2.FONT_HERSHEY_SIMPLEX, size, TEXT_COLOUR, max(1, round(2 * scale)), cv2.LINE_AA)


def hand_labels(frame: Frame, trackers: dict[str, PoseTracker]) -> list[str]:
    """A label like "right palm" per hand, with one hysteresis state per handedness."""
    aspect = frame.height / frame.width if frame.width else 1.0
    labels = []
    seen = set()
    for hand in frame.hands:
        seen.add(hand.handedness)
        pose = trackers.setdefault(hand.handedness, PoseTracker()).classify(hand, aspect=aspect)
        labels.append(f"{hand.handedness} {pose.pose}")
    for side in set(trackers) - seen:  # a hand that left starts over when it comes back
        del trackers[side]
    return labels


def run_preview(args: argparse.Namespace) -> int:
    """0 when the window was closed, 1 (with the reason logged) when the model, camera or window is missing."""
    from . import models

    data_dir = _data_dir(args)
    if not models.is_installed(data_dir):
        log.error(
            "The hand model is not installed in %s. Run /jarvis setup hands (or python -m jarvis_hands setup).",
            models.models_dir(data_dir),
        )
        return 1
    try:
        import cv2
    except ImportError as exc:
        log.error("OpenCV is not available (%s); reinstall the hand helper with /jarvis setup hands.", exc)
        return 1

    from .camera import CameraError, create_camera
    from .tracker import TrackerError, create_tracker

    tracker: Any = None
    camera: Any = None
    try:
        try:
            tracker = create_tracker(models.model_path(data_dir), num_hands=2)
        except TrackerError as exc:
            log.error("%s", _explain(exc.message, exc.hint))
            return 1
        camera = create_camera(args.camera, width=args.width, height=args.height, fps=args.fps)
        try:
            info = camera.open()
        except CameraError as exc:
            log.error("%s", _explain(exc.message, exc.hint))
            return 1
        log.info("preview of %s (%s, %dx%d); q or Esc closes it", info.name, info.backend, info.width, info.height)
        try:
            return _show(cv2, camera, tracker)
        except (CameraError, TrackerError) as exc:  # the camera unplugged, the model unable to run at all
            log.error("%s", _explain(exc.message, exc.hint))
            return 1
        except cv2.error as exc:  # no GUI (a headless OpenCV, no desktop session)
            log.error("cannot open the preview window: %s", exc)
            return 1
    finally:
        if tracker is not None:
            tracker.close()
        if camera is not None:
            camera.close()
        try:
            cv2.destroyAllWindows()
        except cv2.error:
            pass


def _explain(message: str, hint: str | None) -> str:
    """The message and its hint as one line, without saying the same thing twice."""
    if not hint or hint.startswith(message.rstrip(".")):
        return hint or message
    return f"{message} {hint}"


def _show(cv2: Any, camera: Any, tracker: Any) -> int:
    cv2.namedWindow(WINDOW, cv2.WINDOW_NORMAL)
    trackers: dict[str, PoseTracker] = {}
    meter = FpsMeter()
    shown = False
    while True:
        captured = camera.read(0.5)
        if captured is not None:
            frame = tracker.process(captured.image, captured.t)
            fps = meter.tick(clock.now())
            view = cv2.flip(captured.image, 1)
            annotate(view, frame, hand_labels(frame, trackers), fps, tracker.infer_ms)
            cv2.imshow(WINDOW, view)
            shown = True
        key = cv2.waitKey(1) & 0xFF
        if key in KEYS_TO_QUIT:
            return 0
        # Closed with the window's X (only meaningful once something was shown).
        if shown and cv2.getWindowProperty(WINDOW, cv2.WND_PROP_VISIBLE) < 1:
            return 0


def _data_dir(args: argparse.Namespace) -> Path:
    data_dir = getattr(args, "data_dir", None)
    if data_dir:
        return Path(data_dir).expanduser()
    from .cli import default_data_dir

    return default_data_dir()
