"""MediaPipe's HandLandmarker behind the Tracker protocol.

What the code below works around (MediaPipe 0.10.33's ctypes Tasks API):

- The model sees the raw camera frame, where Tasks' handedness is anatomical
  (a right hand is "Right"; flipping the frame would swap the label). The
  landmarks are mirrored afterwards (image ``x' = 1 - x``, world ``x' = -x``)
  so the cursor follows the hand like a mirror.
- ``mp.Image`` hands the array's pointer and size to C without looking at its
  strides, so a view such as ``bgr[..., ::-1]`` comes out garbled (or reads
  past the buffer). ``cv2.cvtColor`` gives a fresh contiguous RGB array.
- VIDEO mode skips the palm detector while it tracks (half the CPU of IMAGE
  mode) and refuses a timestamp that is not strictly larger than the last.
- A graph error (a bad frame) poisons the instance: every later call raises,
  ``close()`` included. The tracker throws it away and builds a new one, which
  takes 50 to 180 ms. Frames the model cannot take never reach it.
- ``HandLandmarker.close()`` frees the native landmarker before it marks
  itself closed, so a ``detect_for_video`` that started a moment earlier
  runs on freed memory and the process segfaults. One lock covers both
  calls: a close waits for the frame in flight (tens of ms), and a frame
  after the close is refused.
- The model goes in as bytes rather than a path: MediaPipe's C layer opens a
  path itself, and a user folder with non-ASCII letters is not something to
  trust it with on Windows.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

import cv2
import numpy as np

from .. import models
from ..landmarks import MIDDLE_MCP, WRIST, Frame, Handedness, HandObservation
from .base import TrackerError

log = logging.getLogger(__name__)

# mediapipe imports matplotlib.pyplot (for its drawing helpers); a GUI backend there is only start-up time.
os.environ.setdefault("MPLBACKEND", "Agg")

MIN_HAND_DETECTION_CONFIDENCE = 0.6
MIN_HAND_PRESENCE_CONFIDENCE = 0.5
MIN_TRACKING_CONFIDENCE = 0.5
#: Frames ``infer_ms`` averages over (two seconds at 30 fps).
INFER_WINDOW = 60
CLOSE_TIMEOUT_S = 1.5
#: Model failures in a row (each followed by a fresh landmarker) after which it counts as unable to run.
MAX_FAILURES_IN_A_ROW = 5

MODEL_HINT = "Run /jarvis setup hands."
TRACKER_HINT = "Run /jarvis setup hands to reinstall the hand helper, then /jarvis hands restart."


class Landmarker(Protocol):
    """One hand landmarker instance in VIDEO mode."""

    def detect(self, rgb: np.ndarray, timestamp_ms: int) -> Any:
        """A HandLandmarkerResult for a contiguous (H, W, 3) uint8 RGB frame; RuntimeError once closed."""
        ...

    def close(self) -> None:
        """Waits for a ``detect`` in progress on another thread, then frees the model."""
        ...


#: ``(model bytes, num_hands) -> Landmarker``; raises when the model cannot be loaded.
LandmarkerFactory = Callable[[bytes, int], Landmarker]


class TasksLandmarker:
    """``mediapipe.tasks.python.vision.HandLandmarker`` in VIDEO mode on the CPU."""

    def __init__(self, model: bytes, num_hands: int) -> None:
        import mediapipe as mp
        from mediapipe.tasks.python import BaseOptions, vision

        options = vision.HandLandmarkerOptions(
            base_options=BaseOptions(model_asset_buffer=model, delegate=BaseOptions.Delegate.CPU),
            running_mode=vision.RunningMode.VIDEO,
            num_hands=num_hands,
            min_hand_detection_confidence=MIN_HAND_DETECTION_CONFIDENCE,
            min_hand_presence_confidence=MIN_HAND_PRESENCE_CONFIDENCE,
            min_tracking_confidence=MIN_TRACKING_CONFIDENCE,
        )
        self._model = model  # MediaPipe copies the buffer when it builds the graph; held anyway
        self._image = mp.Image
        self._srgb = mp.ImageFormat.SRGB
        #: Held across a whole detect and a whole close, which must never overlap (see the module docstring).
        self._lock = threading.Lock()
        self._landmarker: Any = vision.HandLandmarker.create_from_options(options)

    def detect(self, rgb: np.ndarray, timestamp_ms: int) -> Any:
        with self._lock:
            landmarker = self._landmarker
            if landmarker is None:
                raise RuntimeError("the hand landmarker is closed")
            return landmarker.detect_for_video(self._image(image_format=self._srgb, data=rgb), timestamp_ms)

    def close(self) -> None:
        with self._lock:
            landmarker, self._landmarker = self._landmarker, None
            if landmarker is None:
                return
            try:
                landmarker.close()
            except (ValueError, RuntimeError) as exc:
                # A poisoned graph re-raises its error here and keeps its handle. Its __del__ would then close
                # it again when the garbage collector gets to it, and on 0.10.33 that second close never
                # returns: whichever thread collected it would hang. Drop the handle and stop its threads.
                log.debug("the hand landmarker closed with an error: %s", exc)
                _abandon(landmarker)


def _abandon(landmarker: Any) -> None:
    """Best effort on MediaPipe 0.10.33's internals; every step is optional."""
    if getattr(landmarker, "_handle", None) is not None:
        landmarker._handle = None
    for name in ("_dispatcher", "_lib"):
        close = getattr(getattr(landmarker, name, None), "close", None)
        if callable(close):
            try:
                close()
            except Exception as exc:  # noqa: BLE001 - only cleanup
                log.debug("closing %s of a poisoned landmarker: %s", name, exc)


# --------------------------------------------------------------------------- results


def _points(landmarks: Any) -> np.ndarray | None:
    try:
        points = np.array([[p.x, p.y, p.z] for p in landmarks], dtype=float)
    except (TypeError, ValueError):  # a landmark without coordinates
        return None
    return points if points.shape == (21, 3) else None


def observations_from_result(result: Any, width: int, height: int) -> tuple[HandObservation, ...]:
    """The hands in a HandLandmarkerResult, mirrored for the user's view.

    ``handedness`` is MediaPipe's label lowercased (anatomical on the raw
    frame, so it is kept as is), ``score`` its confidence. Image ``x`` becomes
    ``1 - x`` and world ``x`` becomes ``-x``. Coordinates slightly outside
    [0, 1] (a hand cut by the frame edge) are kept; the mapper clamps. The
    frame's ``width`` and ``height`` only serve to drop degenerate hands whose
    wrist and middle knuckle fall within one pixel.
    """
    hands: list[HandObservation] = []
    images: Sequence[Any] = getattr(result, "hand_landmarks", None) or []
    worlds: Sequence[Any] = getattr(result, "hand_world_landmarks", None) or []
    labels: Sequence[Any] = getattr(result, "handedness", None) or []
    for i, landmarks in enumerate(images):
        if i >= len(labels) or not labels[i]:
            continue
        category = labels[i][0]
        label = str(getattr(category, "category_name", "") or "").lower()
        if label not in ("left", "right"):
            continue
        handedness: Handedness = "left" if label == "left" else "right"
        image = _points(landmarks)
        if image is None or not np.isfinite(image).all():
            continue
        palm_px = np.hypot(
            (image[WRIST, 0] - image[MIDDLE_MCP, 0]) * width, (image[WRIST, 1] - image[MIDDLE_MCP, 1]) * height
        )
        if palm_px < 1.0:
            continue
        world = _points(worlds[i]) if i < len(worlds) else None
        if world is None or not np.isfinite(world).all():
            world = np.zeros((21, 3))
        image[:, 0] = 1.0 - image[:, 0]
        world[:, 0] = -world[:, 0]
        score = float(getattr(category, "score", 0.0) or 0.0)
        hands.append(HandObservation(handedness=handedness, score=score, image=image, world=world))
    return tuple(hands)


def _is_bgr(image: Any) -> bool:
    return (
        isinstance(image, np.ndarray)
        and image.ndim == 3
        and image.shape[2] == 3
        and image.shape[0] > 0
        and image.shape[1] > 0
        and image.dtype == np.uint8
    )


def _describe(image: Any) -> str:
    if isinstance(image, np.ndarray):
        return f"{image.shape} {image.dtype}"
    return type(image).__name__


def _close_quietly(landmarker: Landmarker, timeout: float | None = None) -> bool:
    """Close on a daemon thread, so a hung close never holds up the caller. False when it did not finish."""
    timeout = CLOSE_TIMEOUT_S if timeout is None else timeout

    def run() -> None:
        try:
            landmarker.close()
        except Exception as exc:  # noqa: BLE001 - closing must never raise
            log.debug("closing the hand landmarker failed: %s", exc)

    thread = threading.Thread(target=run, name="jarvis-tracker-close", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        log.warning("the hand landmarker did not close within %.1f s; leaving it behind", timeout)
        return False
    return True


def read_model(path: Path) -> bytes:
    """The model file's bytes. Raises TrackerError("model_missing") when it is absent or not the pinned size."""
    try:
        size = path.stat().st_size
    except OSError:
        raise TrackerError("model_missing", f"The hand model is missing ({path}).", MODEL_HINT) from None
    if size != models.MODEL_SIZE or not path.is_file():
        raise TrackerError(
            "model_missing",
            f"The hand model at {path} is incomplete ({size} bytes, expected {models.MODEL_SIZE}).",
            MODEL_HINT,
        )
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise TrackerError("model_missing", f"The hand model could not be read: {exc}", MODEL_HINT) from exc
    if len(data) != models.MODEL_SIZE:
        raise TrackerError("model_missing", f"The hand model at {path} changed while it was read.", MODEL_HINT)
    return data


def _check_num_hands(n: int) -> int:
    if n not in (1, 2) or isinstance(n, bool):
        raise ValueError(f"num_hands must be 1 or 2, got {n!r}")
    return n


# --------------------------------------------------------------------------- the tracker


class MediaPipeTracker:
    """Hands per frame through MediaPipe; ``process`` runs on one thread, the rest from any.

    ``close()`` during a ``process()`` waits for that frame (up to the 1.5 s
    close cap) before the model is freed.
    """

    def __init__(
        self, model_path: Path, *, num_hands: int = 1, landmarker_factory: LandmarkerFactory | None = None
    ) -> None:
        self.model_path = Path(model_path)
        self._wanted = _check_num_hands(num_hands)
        self._model = read_model(self.model_path)
        self._factory: LandmarkerFactory = landmarker_factory or TasksLandmarker
        self._lock = threading.Lock()
        self._closed = False
        self._last_ts = -1
        self._times: deque[float] = deque(maxlen=INFER_WINDOW)
        self._logged: set[str] = set()
        self._failures = 0
        self._built = False
        self._active = self._wanted
        self._landmarker: Landmarker | None = self._create(self._wanted)

    # -- the Tracker protocol -------------------------------------------------------------

    def process(self, image: np.ndarray, t: float) -> Frame:
        shape = getattr(image, "shape", ())
        size = {"width": int(shape[1]), "height": int(shape[0])} if len(shape) >= 2 else {}
        empty = Frame(t=t, hands=(), **size)
        if not _is_bgr(image):
            self._log_once("bad_frame", "ignoring frames that are not (H, W, 3) uint8, such as %s", _describe(image))
            return empty
        landmarker = self._current()
        if landmarker is None:
            return empty
        try:
            rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        except cv2.error as exc:
            self._log_once("convert", "could not convert a frame to RGB: %s", exc)
            return empty
        if not rgb.flags.c_contiguous:  # cvtColor allocates a fresh array; never hand MediaPipe a view
            rgb = np.ascontiguousarray(rgb)
        timestamp = max(self._last_ts + 1, int(t * 1000))
        self._last_ts = timestamp
        started = time.perf_counter()
        try:
            result = landmarker.detect(rgb, timestamp)
        except (ValueError, RuntimeError) as exc:
            if not self._closed:  # else close() took the landmarker away mid-frame: nothing to recover
                self._recover(landmarker, exc)
            return empty
        elapsed_ms = (time.perf_counter() - started) * 1000
        with self._lock:
            self._times.append(elapsed_ms)
        self._failures = 0
        return Frame(t=t, hands=observations_from_result(result, empty.width, empty.height), **size)

    @property
    def num_hands(self) -> int:
        return self._wanted

    def set_num_hands(self, n: int) -> None:
        """Takes effect on the next ``process()``, which builds a new landmarker (50 to 180 ms)."""
        with self._lock:
            self._wanted = _check_num_hands(n)

    @property
    def infer_ms(self) -> float:
        with self._lock:
            return sum(self._times) / len(self._times) if self._times else 0.0

    def close(self) -> None:
        try:
            with self._lock:
                if self._closed:
                    return
                self._closed = True
                landmarker, self._landmarker = self._landmarker, None
            if landmarker is not None:
                _close_quietly(landmarker)
        except Exception:
            log.exception("closing the hand tracker failed")

    # -- internals ------------------------------------------------------------------------

    def _create(self, num_hands: int) -> Landmarker:
        started = time.perf_counter()
        try:
            landmarker = self._factory(self._model, num_hands)
        except Exception as exc:
            log.exception("could not create the hand landmarker")
            raise TrackerError(
                "tracker_failed", f"MediaPipe could not load the hand model: {type(exc).__name__}: {exc}", TRACKER_HINT
            ) from exc
        # Rebuilt each time the runtime switches between one and two hands: say it once.
        level = logging.DEBUG if self._built else logging.INFO
        self._built = True
        log.log(
            level, "hand landmarker ready for %d hand(s) in %.0f ms", num_hands, (time.perf_counter() - started) * 1000
        )
        return landmarker

    def _current(self) -> Landmarker | None:
        """The landmarker to use now, rebuilt first when the hand count changed or the last one failed."""
        with self._lock:
            if self._closed:
                return None
            if self._landmarker is not None and self._active == self._wanted:
                return self._landmarker
            old, self._landmarker = self._landmarker, None
            wanted = self._wanted
        if old is not None:
            _close_quietly(old)
        landmarker = self._create(wanted)  # raises TrackerError
        with self._lock:
            if self._closed:  # closed while it was being built
                closed = True
            else:
                closed = False
                self._landmarker, self._active = landmarker, wanted
        if closed:
            _close_quietly(landmarker)
            return None
        return landmarker

    def _recover(self, landmarker: Landmarker, exc: Exception) -> None:
        kind = type(exc).__name__
        detail = (str(exc).strip().splitlines() or [""])[0][:200]
        self._log_once(kind, "MediaPipe failed on a frame (%s: %s); starting a new hand landmarker", kind, detail)
        self._failures += 1
        with self._lock:
            if self._landmarker is landmarker:
                self._landmarker = None
        _close_quietly(landmarker)
        if self._failures >= MAX_FAILURES_IN_A_ROW:
            raise TrackerError(
                "tracker_failed",
                f"MediaPipe failed on {self._failures} frames in a row ({kind}: {detail}).",
                TRACKER_HINT,
            ) from exc
        self._current()

    def _log_once(self, kind: str, message: str, *args: Any) -> None:
        if kind in self._logged:
            log.debug(message, *args)
            return
        self._logged.add(kind)
        log.warning(message, *args)
