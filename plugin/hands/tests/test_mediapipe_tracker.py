"""The MediaPipe tracker: result conversion and recovery with a fake landmarker, then the real model on test photos.

The real-model tests are skipped unless ``JARVIS_HANDS_MODELS_DIR`` is set
(CI sets it); conftest downloads the model and the photos there when missing,
the model checked against its sha256.
"""

from __future__ import annotations

import gc
import logging
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest

from jarvis_hands import models
from jarvis_hands.landmarks import INDEX_MCP, MIDDLE_MCP, PINKY_MCP, PINKY_TIP, THUMB_TIP, WRIST, HandObservation
from jarvis_hands.tracker import TrackerError, create_tracker
from jarvis_hands.tracker import mediapipe_tracker as mpt
from jarvis_hands.tracker.mediapipe_tracker import MediaPipeTracker, observations_from_result

from conftest import fetch_photo, real_model

# --------------------------------------------------------------------------- fake results


@dataclass
class Landmark:
    x: float
    y: float
    z: float


@dataclass
class Category:
    category_name: str
    score: float


@dataclass
class Result:
    hand_landmarks: list[list[Landmark]] = field(default_factory=list)
    hand_world_landmarks: list[list[Landmark]] = field(default_factory=list)
    handedness: list[list[Category]] = field(default_factory=list)


def hand_points(x0: float = 0.2) -> np.ndarray:
    """21 distinct points: x from ``x0`` in steps of 0.02, y from 0.3, z falling."""
    i = np.arange(21, dtype=float)
    return np.stack([x0 + 0.02 * i, 0.3 + 0.01 * i, -0.001 * i], axis=1)


def world_points() -> np.ndarray:
    i = np.arange(21, dtype=float)
    return np.stack([0.01 * i - 0.1, 0.005 * i, 0.002 * i], axis=1)


def landmarks(points: np.ndarray) -> list[Landmark]:
    return [Landmark(*map(float, p)) for p in points]


def result_with(*hands: tuple[str, float, np.ndarray]) -> Result:
    return Result(
        hand_landmarks=[landmarks(points) for _, _, points in hands],
        hand_world_landmarks=[landmarks(world_points()) for _ in hands],
        handedness=[[Category(label, score)] for label, score, _ in hands],
    )


def test_observations_mirror_x_and_keep_mediapipes_label() -> None:
    raw = hand_points()
    (hand,) = observations_from_result(result_with(("Right", 0.93, raw)), 1280, 720)
    assert hand.handedness == "right" and hand.score == pytest.approx(0.93)
    assert hand.image.shape == (21, 3) and hand.world.shape == (21, 3)
    assert np.allclose(hand.image[:, 0], 1.0 - raw[:, 0])
    assert np.allclose(hand.image[:, 1:], raw[:, 1:])
    assert np.allclose(hand.world[:, 0], -world_points()[:, 0])
    assert np.allclose(hand.world[:, 1:], world_points()[:, 1:])


def test_observations_keep_two_hands_in_order() -> None:
    hands = observations_from_result(
        result_with(("Left", 0.9, hand_points(0.1)), ("Right", 0.8, hand_points(0.5))), 640, 480
    )
    assert [h.handedness for h in hands] == ["left", "right"]
    assert hands[0].image[0, 0] == pytest.approx(0.9) and hands[1].image[0, 0] == pytest.approx(0.5)


def test_observations_keep_points_past_the_frame_edge() -> None:
    raw = hand_points(0.6)  # x reaches 1.0; MediaPipe extrapolates a cut-off hand past the edge
    raw[20, 0], raw[0, 1] = 1.034, -0.037
    (hand,) = observations_from_result(result_with(("Left", 0.9, raw)), 1280, 720)
    assert hand.image[20, 0] == pytest.approx(-0.034) and hand.image[0, 1] == pytest.approx(-0.037)


def test_observations_drop_hands_they_cannot_trust() -> None:
    good = hand_points()
    nan = good.copy()
    nan[3, 1] = np.nan
    collapsed = np.full((21, 3), 0.5)  # every point within one pixel
    result = result_with(
        ("None", 0.9, good),
        ("Right", 0.9, nan),
        ("Left", 0.9, collapsed),
        ("Right", 0.9, good[:20]),
        ("Left", 0.7, good),
    )
    result.handedness.append([])  # a hand without a label
    result.hand_landmarks.append(landmarks(good))
    hands = observations_from_result(result, 1280, 720)
    assert [(h.handedness, h.score) for h in hands] == [("left", pytest.approx(0.7))]


def test_a_hand_without_world_landmarks_gets_zeros() -> None:
    result = result_with(("Right", 0.9, hand_points()))
    result.hand_world_landmarks = []
    (hand,) = observations_from_result(result, 1280, 720)
    assert np.array_equal(hand.world, np.zeros((21, 3)))


def test_an_empty_result_has_no_hands() -> None:
    assert observations_from_result(Result(), 1280, 720) == ()
    assert observations_from_result(object(), 1280, 720) == ()


# --------------------------------------------------------------------------- the tracker with a fake landmarker


@dataclass
class FakeLandmarker:
    """Records what it was given; ``detect`` returns ``result`` or raises the next of ``errors``."""

    num_hands: int
    result: Any
    errors: list[BaseException | None] = field(default_factory=list)
    delay: float = 0.0
    close_error: BaseException | None = None
    close_blocks: threading.Event | None = None
    seen: list[tuple[tuple[int, ...], bool, Any, int]] = field(default_factory=list)
    closed: int = 0

    def detect(self, rgb: np.ndarray, timestamp_ms: int) -> Any:
        self.seen.append((rgb.shape, rgb.flags.c_contiguous, rgb.copy(), timestamp_ms))
        if self.delay:
            time.sleep(self.delay)
        error = self.errors.pop(0) if self.errors else None
        if error is not None:
            raise error
        return self.result

    def close(self) -> None:
        self.closed += 1
        if self.close_blocks is not None:
            self.close_blocks.wait(5.0)
        if self.close_error is not None:
            raise self.close_error


@dataclass
class FakeFactory:
    """Builds FakeLandmarkers; ``script`` sets up each new one (errors, delays), ``fail`` makes building raise."""

    result: Any = field(default_factory=lambda: result_with(("Right", 0.9, hand_points())))
    script: list[Callable[[FakeLandmarker], None]] = field(default_factory=list)
    fail: list[bool] = field(default_factory=list)
    made: list[FakeLandmarker] = field(default_factory=list)
    models: list[int] = field(default_factory=list)

    def __call__(self, model: bytes, num_hands: int) -> FakeLandmarker:
        self.models.append(len(model))
        if self.fail and self.fail.pop(0):
            raise RuntimeError("Service 'kGpuService' is not available")
        landmarker = FakeLandmarker(num_hands, self.result)
        if self.script:
            self.script.pop(0)(landmarker)
        self.made.append(landmarker)
        return landmarker


@pytest.fixture
def model_file(tmp_path: Path) -> Path:
    """A file of the pinned model size (sparse zeros): the fake factory never reads it as a model."""
    path = tmp_path / models.MODEL_NAME
    with path.open("wb") as fh:
        fh.truncate(models.MODEL_SIZE)
    return path


@pytest.fixture
def trackers() -> Iterator[list[MediaPipeTracker]]:
    made: list[MediaPipeTracker] = []
    yield made
    for tracker in made:
        tracker.close()


def make_tracker(
    trackers: list[MediaPipeTracker], model_file: Path, factory: FakeFactory, num_hands: int = 1
) -> MediaPipeTracker:
    tracker = MediaPipeTracker(model_file, num_hands=num_hands, landmarker_factory=factory)
    trackers.append(tracker)
    return tracker


def bgr_frame(height: int = 48, width: int = 64) -> np.ndarray:
    image = np.zeros((height, width, 3), np.uint8)
    image[..., 0], image[..., 1], image[..., 2] = 10, 20, 30  # B, G, R
    return image


def test_a_missing_model_is_model_missing(tmp_path: Path) -> None:
    with pytest.raises(TrackerError) as error:
        MediaPipeTracker(tmp_path / "nope.task", landmarker_factory=FakeFactory())
    assert error.value.code == "model_missing" and error.value.hint == "Run /jarvis setup hands."
    with pytest.raises(TrackerError) as error:
        create_tracker(tmp_path / "nope.task")
    assert error.value.code == "model_missing"


def test_a_model_of_the_wrong_size_is_model_missing(tmp_path: Path) -> None:
    path = tmp_path / models.MODEL_NAME
    path.write_bytes(b"truncated")
    with pytest.raises(TrackerError) as error:
        MediaPipeTracker(path, landmarker_factory=FakeFactory())
    assert error.value.code == "model_missing"
    with pytest.raises(TrackerError) as error:
        MediaPipeTracker(tmp_path, landmarker_factory=FakeFactory())  # a directory
    assert error.value.code == "model_missing"


def test_a_landmarker_that_cannot_be_built_is_tracker_failed(model_file: Path) -> None:
    with pytest.raises(TrackerError) as error:
        MediaPipeTracker(model_file, landmarker_factory=FakeFactory(fail=[True]))
    assert error.value.code == "tracker_failed" and "kGpuService" in error.value.message


def test_the_model_reaches_the_landmarker_as_bytes(trackers: list[MediaPipeTracker], model_file: Path) -> None:
    factory = FakeFactory()
    make_tracker(trackers, model_file, factory, num_hands=2)
    assert factory.models == [models.MODEL_SIZE] and factory.made[0].num_hands == 2


@pytest.mark.parametrize("n", [0, 3, True])
def test_only_one_or_two_hands(model_file: Path, n: int) -> None:
    with pytest.raises(ValueError):
        MediaPipeTracker(model_file, num_hands=n, landmarker_factory=FakeFactory())


def test_process_hands_mediapipe_a_contiguous_rgb_copy(trackers: list[MediaPipeTracker], model_file: Path) -> None:
    factory = FakeFactory()
    tracker = make_tracker(trackers, model_file, factory)
    image = bgr_frame()
    frame = tracker.process(image[:, ::-1], 12.5)  # a strided view must not reach MediaPipe as is
    shape, contiguous, rgb, _ = factory.made[0].seen[0]
    assert shape == (48, 64, 3) and contiguous and rgb.dtype == np.uint8
    assert rgb[0, 0].tolist() == [30, 20, 10]
    assert (frame.t, frame.width, frame.height) == (12.5, 64, 48)
    assert [h.handedness for h in frame.hands] == ["right"]
    assert np.allclose(frame.hands[0].image[:, 0], 1.0 - hand_points()[:, 0])


def test_timestamps_strictly_increase(trackers: list[MediaPipeTracker], model_file: Path) -> None:
    factory = FakeFactory()
    tracker = make_tracker(trackers, model_file, factory)
    for t in (1.0, 1.0, 0.5, 1.0005, 2.0, 2.0004):
        tracker.process(bgr_frame(), t)
    assert [ts for *_, ts in factory.made[0].seen] == [1000, 1001, 1002, 1003, 2000, 2001]


@pytest.mark.parametrize(
    "image",
    [
        np.zeros((48, 64), np.uint8),
        np.zeros((48, 64, 4), np.uint8),
        np.zeros((48, 64, 1), np.uint8),
        np.zeros((48, 64, 3), np.float32),
        np.zeros((0, 64, 3), np.uint8),
        None,
        [[0, 0, 0]],
    ],
    ids=["grey", "bgra", "one-channel", "float", "empty", "none", "list"],
)
def test_bad_frames_give_no_hands_without_touching_the_model(
    trackers: list[MediaPipeTracker], model_file: Path, image: Any
) -> None:
    factory = FakeFactory()
    tracker = make_tracker(trackers, model_file, factory)
    frame = tracker.process(image, 3.0)
    assert frame.hands == () and frame.t == 3.0
    assert factory.made[0].seen == [] and len(factory.made) == 1
    assert tracker.process(bgr_frame(), 3.1).hands  # and the next good frame is tracked


def test_a_grey_frame_keeps_its_size(trackers: list[MediaPipeTracker], model_file: Path) -> None:
    tracker = make_tracker(trackers, model_file, FakeFactory())
    frame = tracker.process(np.zeros((48, 64), np.uint8), 1.0)
    assert (frame.width, frame.height) == (64, 48)


def test_a_failing_landmarker_is_replaced(
    trackers: list[MediaPipeTracker], model_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    graph_error = ValueError("Graph has errors: \nCalculator::Process() failed")
    factory = FakeFactory(
        script=[
            lambda lm: lm.errors.extend([None, graph_error]),
            lambda lm: setattr(lm, "close_error", ValueError("Task runner is currently not running.")),
        ]
    )
    tracker = make_tracker(trackers, model_file, factory)
    with caplog.at_level(logging.DEBUG, logger=mpt.__name__):
        assert tracker.process(bgr_frame(), 1.0).hands
        assert tracker.process(bgr_frame(), 1.1).hands == ()  # the failing frame
        first, second = factory.made
        assert first.closed == 1 and second.closed == 0
        assert tracker.process(bgr_frame(), 1.2).hands  # the new landmarker works
        second.errors.append(ValueError("Graph has errors again"))
        assert tracker.process(bgr_frame(), 1.3).hands == ()
        second.errors.clear()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "MediaPipe failed" in r.message]
    assert len(warnings) == 1, "one warning per kind of error"
    assert len(factory.made) == 3 and second.closed == 1  # a close that raises is swallowed
    tracker.process(bgr_frame(), 0.0)
    assert factory.made[2].seen[0][3] > 1300, "timestamps keep rising across landmarkers"


def test_runtime_errors_are_recovered_too(trackers: list[MediaPipeTracker], model_file: Path) -> None:
    factory = FakeFactory(script=[lambda lm: lm.errors.append(RuntimeError("CalculatorGraph::Run() failed"))])
    tracker = make_tracker(trackers, model_file, factory)
    assert tracker.process(bgr_frame(), 1.0).hands == ()
    assert tracker.process(bgr_frame(), 1.1).hands and len(factory.made) == 2


def test_a_landmarker_that_fails_every_time_becomes_tracker_failed(
    trackers: list[MediaPipeTracker], model_file: Path
) -> None:
    always = [lambda lm: lm.errors.extend([ValueError("Graph has errors")] * 10)] * 10
    factory = FakeFactory(script=list(always))
    tracker = make_tracker(trackers, model_file, factory)
    for i in range(mpt.MAX_FAILURES_IN_A_ROW - 1):
        assert tracker.process(bgr_frame(), float(i)).hands == ()
    with pytest.raises(TrackerError) as error:
        tracker.process(bgr_frame(), 99.0)
    assert error.value.code == "tracker_failed"


def test_a_landmarker_that_cannot_be_rebuilt_raises_tracker_failed(
    trackers: list[MediaPipeTracker], model_file: Path
) -> None:
    factory = FakeFactory(script=[lambda lm: lm.errors.append(ValueError("Graph has errors"))], fail=[False, True])
    tracker = make_tracker(trackers, model_file, factory)
    with pytest.raises(TrackerError) as error:
        tracker.process(bgr_frame(), 1.0)
    assert error.value.code == "tracker_failed"


def test_set_num_hands_rebuilds_on_the_next_frame(trackers: list[MediaPipeTracker], model_file: Path) -> None:
    factory = FakeFactory()
    tracker = make_tracker(trackers, model_file, factory)
    tracker.set_num_hands(2)
    assert tracker.num_hands == 2 and len(factory.made) == 1  # nothing rebuilt until a frame comes
    tracker.process(bgr_frame(), 1.0)
    assert [lm.num_hands for lm in factory.made] == [1, 2] and factory.made[0].closed == 1
    assert factory.made[0].seen == [] and len(factory.made[1].seen) == 1
    tracker.set_num_hands(2)
    tracker.process(bgr_frame(), 1.1)
    assert len(factory.made) == 2
    tracker.set_num_hands(1)
    tracker.process(bgr_frame(), 1.2)
    assert [lm.num_hands for lm in factory.made] == [1, 2, 1]
    with pytest.raises(ValueError):
        tracker.set_num_hands(3)
    assert tracker.num_hands == 1


class FakeClock:
    """``time.perf_counter`` for the tracker module; detect advances it by ``step`` seconds."""

    def __init__(self) -> None:
        self.now = 100.0
        self.step = 0.0

    def perf_counter(self) -> float:
        return self.now


def test_infer_ms_is_the_mean_detect_time_of_the_last_frames(
    trackers: list[MediaPipeTracker], model_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    monkeypatch.setattr(mpt, "time", clock)
    factory = FakeFactory()
    tracker = make_tracker(trackers, model_file, factory)
    landmarker = factory.made[0]
    real_detect = landmarker.detect

    def timed_detect(rgb: np.ndarray, timestamp_ms: int) -> Any:
        clock.now += clock.step
        return real_detect(rgb, timestamp_ms)

    landmarker.detect = timed_detect  # type: ignore[method-assign]
    assert tracker.infer_ms == 0.0
    image = bgr_frame()
    for step, count in ((0.010, 60), (0.002, mpt.INFER_WINDOW)):
        clock.step = step
        for _ in range(count):
            tracker.process(image, 1.0)
    assert tracker.infer_ms == pytest.approx(2.0)
    clock.step = 0.008
    for _ in range(mpt.INFER_WINDOW // 2):
        tracker.process(image, 1.0)
    assert tracker.infer_ms == pytest.approx(5.0)


def test_close_is_idempotent_and_never_raises(trackers: list[MediaPipeTracker], model_file: Path) -> None:
    factory = FakeFactory(script=[lambda lm: setattr(lm, "close_error", ValueError("Graph has errors"))])
    tracker = make_tracker(trackers, model_file, factory)
    tracker.close()
    tracker.close()
    assert factory.made[0].closed == 1
    assert tracker.process(bgr_frame(), 1.0).hands == () and factory.made[0].seen == []
    assert len(factory.made) == 1


def test_close_during_a_frame_neither_raises_nor_rebuilds(trackers: list[MediaPipeTracker], model_file: Path) -> None:
    entered, go = threading.Event(), threading.Event()
    factory = FakeFactory()
    tracker = make_tracker(trackers, model_file, factory)

    def detect_until_closed(rgb: np.ndarray, timestamp_ms: int) -> Any:
        entered.set()
        go.wait(5.0)
        raise RuntimeError("the hand landmarker is closed")

    factory.made[0].detect = detect_until_closed  # type: ignore[method-assign]
    frames: list[Any] = []
    worker = threading.Thread(target=lambda: frames.append(tracker.process(bgr_frame(), 1.0)))
    worker.start()
    assert entered.wait(5.0)
    tracker.close()
    go.set()
    worker.join(5.0)
    assert len(frames) == 1 and frames[0].hands == ()
    assert len(factory.made) == 1 and factory.made[0].closed == 1


@dataclass
class BlockingHandLandmarker:
    """Stands in for MediaPipe's HandLandmarker: ``detect_for_video`` holds until ``go`` is set."""

    entered: threading.Event = field(default_factory=threading.Event)
    go: threading.Event = field(default_factory=threading.Event)
    events: list[str] = field(default_factory=list)

    def detect_for_video(self, image: Any, timestamp_ms: int) -> Result:
        self.events.append("detect")
        self.entered.set()
        self.go.wait(5.0)
        self.events.append("detected")
        return Result()

    def close(self) -> None:
        self.events.append("close")


def test_closing_a_tasks_landmarker_waits_for_the_frame_in_flight(monkeypatch: pytest.MonkeyPatch) -> None:
    # MediaPipe frees the native landmarker on close; a detect still on its way would run on the freed handle.
    from mediapipe.tasks.python import vision

    native = BlockingHandLandmarker()
    monkeypatch.setattr(vision.HandLandmarker, "create_from_options", staticmethod(lambda options: native))
    landmarker = mpt.TasksLandmarker(b"model", 1)
    detecting = threading.Thread(target=landmarker.detect, args=(np.zeros((48, 64, 3), np.uint8), 1))
    detecting.start()
    assert native.entered.wait(5.0)
    closing = threading.Thread(target=landmarker.close)
    closing.start()
    closing.join(0.2)
    assert closing.is_alive() and native.events == ["detect"]
    native.go.set()
    detecting.join(5.0)
    closing.join(5.0)
    assert native.events == ["detect", "detected", "close"]
    with pytest.raises(RuntimeError):
        landmarker.detect(np.zeros((48, 64, 3), np.uint8), 2)


def test_close_does_not_wait_for_a_hung_landmarker(
    trackers: list[MediaPipeTracker], model_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mpt, "CLOSE_TIMEOUT_S", 0.1)
    release = threading.Event()
    factory = FakeFactory(script=[lambda lm: setattr(lm, "close_blocks", release)])
    tracker = make_tracker(trackers, model_file, factory)
    started = time.monotonic()
    tracker.close()
    assert time.monotonic() - started < 1.0
    release.set()


# --------------------------------------------------------------------------- the real model


def photo(name: str) -> np.ndarray:
    path = fetch_photo(name)
    image = cv2.imread(str(path))
    assert image is not None, path
    return image


def chirality(hand: HandObservation) -> float:
    """z of (wrist -> middle knuckle) x (pinky knuckle -> index knuckle) in the image.

    Its sign does not change as the hand turns in the picture plane; it flips
    when the picture is mirrored or the hand shows its other side.
    """
    up = hand.image[MIDDLE_MCP, :2] - hand.image[WRIST, :2]
    across = hand.image[INDEX_MCP, :2] - hand.image[PINKY_MCP, :2]
    return float(up[0] * across[1] - up[1] * across[0])


@real_model
def test_right_hands_stay_right_with_mirrored_x(real_model_path: Path, trackers: list[MediaPipeTracker]) -> None:
    """right_hands.jpg: two right hands seen from the back, one pointing up and one down."""
    image = photo("right_hands")
    raw_lm = mpt.TasksLandmarker(real_model_path.read_bytes(), 2)
    try:
        raw = raw_lm.detect(cv2.cvtColor(image, cv2.COLOR_BGR2RGB), 1000)
    finally:
        raw_lm.close()
    tracker = MediaPipeTracker(real_model_path, num_hands=2)
    trackers.append(tracker)
    frame = tracker.process(image, 1.0)
    assert [h.handedness for h in frame.hands] == ["right", "right"]
    assert (frame.width, frame.height) == (image.shape[1], image.shape[0])
    for hand, landmarks_raw in zip(frame.hands, raw.hand_landmarks, strict=True):
        xs = np.array([p.x for p in landmarks_raw])
        assert np.allclose(hand.image[:, 0], 1.0 - xs, atol=1e-4)
        assert chirality(hand) > 0  # the back of a right hand, mirrored
    up = [h for h in frame.hands if h.image[WRIST, 1] > h.image[MIDDLE_MCP, 1]]
    assert len(up) == 1
    # The hand pointing up has its thumb on the image's right once mirrored (on the left in the photo).
    assert up[0].image[THUMB_TIP, 0] > up[0].image[PINKY_TIP, 0]
    assert tracker.infer_ms > 0

    # pointing_up.jpg: a right hand showing its palm. Same label, the other side: the other sign.
    palm = MediaPipeTracker(real_model_path, num_hands=1)
    trackers.append(palm)
    (hand,) = palm.process(photo("pointing_up"), 1.0).hands
    assert hand.handedness == "right" and chirality(hand) < 0


@real_model
def test_a_flipped_photo_gives_left_hands_mirroring_the_right(
    real_model_path: Path, trackers: list[MediaPipeTracker]
) -> None:
    image = photo("right_hands")
    right = MediaPipeTracker(real_model_path, num_hands=2)
    left = MediaPipeTracker(real_model_path, num_hands=2)
    trackers.extend([right, left])
    rights = right.process(image, 1.0).hands
    lefts = left.process(cv2.flip(image, 1), 1.0).hands
    assert [h.handedness for h in lefts] == ["left", "left"]
    assert all(chirality(h) < 0 for h in lefts)
    # Each left hand is its right twin mirrored back, to within the model's own asymmetry.
    for hand in lefts:
        twin = min(rights, key=lambda r: abs((1.0 - r.image[WRIST, 0]) - hand.image[WRIST, 0]))
        assert np.abs(hand.image[:, 0] - (1.0 - twin.image[:, 0])).mean() < 0.02
        assert np.abs(hand.image[:, 1] - twin.image[:, 1]).mean() < 0.02


@real_model
def test_bad_input_does_not_poison_later_frames(
    real_model_path: Path, trackers: list[MediaPipeTracker], monkeypatch: pytest.MonkeyPatch
) -> None:
    import mediapipe as mp

    unraisable: list[Any] = []
    monkeypatch.setattr(sys, "unraisablehook", unraisable.append)
    made: list[mpt.TasksLandmarker] = []

    def factory(model: bytes, num_hands: int) -> mpt.TasksLandmarker:
        made.append(mpt.TasksLandmarker(model, num_hands))
        return made[-1]

    image = photo("right_hands")
    tracker = MediaPipeTracker(real_model_path, num_hands=2, landmarker_factory=factory)
    trackers.append(tracker)
    assert tracker.process(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), 1.0).hands == ()
    assert tracker.process(cv2.cvtColor(image, cv2.COLOR_BGR2BGRA), 1.1).hands == ()
    assert len(tracker.process(image, 1.2).hands) == 2 and len(made) == 1

    # Poison the live landmarker the way a bad frame would (GRAY8 into the graph); the tracker never would.
    grey = np.ascontiguousarray(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY))
    with pytest.raises(ValueError):
        made[0]._landmarker.detect_for_video(mp.Image(image_format=mp.ImageFormat.GRAY8, data=grey), 1300)
    assert tracker.process(image, 1.4).hands == ()  # "Graph has errors": replaced
    assert len(made) == 2
    assert len(tracker.process(image, 1.5).hands) == 2
    # Left as it is, a poisoned HandLandmarker's __del__ closes it again, and that call never returns
    # (MediaPipe 0.10.33): whichever thread drops the last reference would hang.
    del made[0]
    collector = threading.Thread(target=gc.collect, daemon=True)
    collector.start()
    collector.join(10.0)
    assert not collector.is_alive(), "collecting the poisoned landmarker hung"
    assert unraisable == [], "the poisoned landmarker must not raise again when it is collected"


#: Closes a real tracker while ``process()`` is inside MediaPipe, pausing 0 to 8 ms in there so the
#: native close and the frame overlap. A segfault cannot be caught in-process, hence the subprocess.
CLOSE_MID_FRAME = r"""
import faulthandler, sys, threading, time
from pathlib import Path

import cv2

from jarvis_hands.tracker import mediapipe_tracker as mpt

faulthandler.enable()
model, photo = Path(sys.argv[1]), cv2.imread(sys.argv[2])
for delay_ms in (0, 1, 2, 3, 4, 5, 6, 8):
    entered = threading.Event()

    class Paused(mpt.TasksLandmarker):
        def __init__(self, model: bytes, num_hands: int) -> None:
            super().__init__(model, num_hands)
            image = self._image

            def paused(**kwargs):
                made = image(**kwargs)
                entered.set()
                time.sleep(delay_ms / 1000)
                return made

            self._image = paused

    tracker = mpt.MediaPipeTracker(model, num_hands=2, landmarker_factory=Paused)
    tracker.process(photo, 0.5)
    entered.clear()
    frames = []
    worker = threading.Thread(target=lambda: frames.append(tracker.process(photo, 1.0)))
    worker.start()
    entered.wait(5.0)
    tracker.close()
    worker.join(10.0)
    if worker.is_alive() or len(frames) != 1:
        sys.exit(f"process() did not return after close at {delay_ms} ms")
    tracker.close()
print("survived", flush=True)
"""


@real_model
def test_close_while_a_frame_is_in_mediapipe_does_not_crash(real_model_path: Path) -> None:
    path = fetch_photo("right_hands")
    run = subprocess.run(
        [sys.executable, "-c", CLOSE_MID_FRAME, str(real_model_path), str(path)],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert run.returncode == 0 and "survived" in run.stdout, (run.returncode, run.stderr[-3000:])


@real_model
def test_a_closed_landmarker_refuses_frames(real_model_path: Path) -> None:
    landmarker = mpt.TasksLandmarker(real_model_path.read_bytes(), 1)
    landmarker.close()
    landmarker.close()
    with pytest.raises(RuntimeError):
        landmarker.detect(np.zeros((48, 64, 3), np.uint8), 1)


@real_model
def test_set_num_hands_with_the_real_model(real_model_path: Path, trackers: list[MediaPipeTracker]) -> None:
    image = photo("right_hands")
    tracker = MediaPipeTracker(real_model_path, num_hands=1)
    trackers.append(tracker)
    assert len(tracker.process(image, 1.0).hands) == 1
    tracker.set_num_hands(2)
    assert len(tracker.process(image, 1.1).hands) == 2
