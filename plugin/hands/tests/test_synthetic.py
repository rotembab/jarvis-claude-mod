"""The no-hardware pieces behind ``run --fake``: the synthetic hand model, FakeCamera and FakeTracker."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import numpy as np
import pytest

from jarvis_hands import synthetic
from jarvis_hands.camera.base import CameraError
from jarvis_hands.camera.fake import FakeCamera
from jarvis_hands.landmarks import Frame
from jarvis_hands.poses import PoseTracker, anchor_point
from jarvis_hands.tracker.fake import FakeTracker, build_hand, load_script

import scripted

BLACK = np.zeros((720, 1280, 3), dtype=np.uint8)

# --------------------------------------------------------------------------- synthetic hands


def test_scripted_re_exports_the_package_model() -> None:
    assert scripted.hand is synthetic.hand and scripted.world_landmarks is synthetic.world_landmarks
    assert (scripted.WIDTH, scripted.HEIGHT, scripted.SCALE, scripted.POSES) == (
        synthetic.WIDTH,
        synthetic.HEIGHT,
        synthetic.SCALE,
        synthetic.POSES,
    )


@pytest.mark.parametrize("size", [(1280, 720), (640, 480)])
def test_a_synthetic_hand_has_its_anchor_where_asked(size: tuple[int, int]) -> None:
    observation = synthetic.hand("pinch", (0.3, 0.6), size=size, handedness="left")
    anchor = anchor_point(observation.image)
    assert (anchor.x, anchor.y) == pytest.approx((0.3, 0.6))
    assert observation.handedness == "left" and observation.world.shape == (21, 3)


@pytest.mark.parametrize("pose", ["palm", "fist", "pinch", "pinch_middle", "two"])
def test_synthetic_poses_classify_as_themselves(pose: synthetic.SyntheticPose) -> None:
    observation = synthetic.hand(pose)
    assert PoseTracker().classify(observation, aspect=synthetic.HEIGHT / synthetic.WIDTH).pose == pose


def test_an_unknown_synthetic_pose_is_refused() -> None:
    with pytest.raises(ValueError, match="moonwalk"):
        synthetic.world_landmarks("moonwalk")  # type: ignore[arg-type]


def test_synthetic_frame() -> None:
    frame = synthetic.frame(2.5, synthetic.hand(), size=(640, 480))
    assert (frame.t, len(frame.hands), frame.width, frame.height) == (2.5, 1, 640, 480)


# --------------------------------------------------------------------------- FakeCamera


def test_fake_camera_paces_frames_at_its_rate() -> None:
    camera = FakeCamera(width=320, height=240, fps=50.0)
    info = camera.open()
    assert (info.name, info.backend, info.width, info.height, info.fps) == ("fake camera", "fake", 320, 240, 50.0)
    started = time.monotonic()
    read = [camera.read(1.0) for _ in range(10)]
    elapsed = time.monotonic() - started
    frames = [f for f in read if f is not None]
    assert len(frames) == 10
    seqs = [f.seq for f in frames]
    assert seqs[0] == 1 and seqs == sorted(set(seqs))
    # Ten frames at 50 fps: about 0.18 s; a busy machine may skip one, never deliver early.
    assert 0.17 <= elapsed < 0.4 and 10 <= seqs[-1] <= 12
    assert all(f.t == pytest.approx(frames[0].t + (f.seq - 1) * 0.02) for f in frames)
    assert frames[0].image.shape == (240, 320, 3) and not frames[0].image.any()
    camera.close()


def test_fake_camera_read_times_out_between_frames() -> None:
    camera = FakeCamera(fps=2.0)
    camera.open()
    assert camera.read(1.0) is not None
    started = time.monotonic()
    assert camera.read(0.05) is None  # the next frame is half a second away
    assert time.monotonic() - started == pytest.approx(0.05, abs=0.04)
    camera.close()


def test_a_slow_reader_gets_only_the_newest_frame() -> None:
    camera = FakeCamera(fps=100.0)
    camera.open()
    first = camera.read(1.0)
    time.sleep(0.1)
    newest = camera.read(1.0)
    assert first is not None and newest is not None and newest.seq - first.seq >= 8
    camera.close()


def test_fake_camera_cycles_the_given_frames_and_hands_out_copies() -> None:
    red, green = np.zeros((4, 6, 3), np.uint8), np.zeros((4, 6, 3), np.uint8)
    red[..., 2], green[..., 1] = 255, 255
    camera = FakeCamera(width=6, height=4, fps=200.0, frames=[red, green])
    camera.open()
    seen = []
    for _ in range(3):
        frame = camera.read(1.0)
        assert frame is not None
        seen.append((frame.seq, int(frame.image[0, 0, 2])))
        frame.image[:] = 7  # the caller owns its copy
    assert all(value == (255 if seq % 2 else 0) for seq, value in seen)
    assert red[0, 0].tolist() == [0, 0, 255]


def test_fake_camera_scripted_failures() -> None:
    refusing = FakeCamera(fail_open="camera_in_use")
    with pytest.raises(CameraError) as info:
        refusing.open()
    assert info.value.code == "camera_in_use" and info.value.hint
    refusing.fail_open = None
    refusing.open()

    refusing.fail("camera_lost")
    for _ in range(2):  # lost stays lost
        with pytest.raises(CameraError) as info:
            refusing.read(0.1)
        assert info.value.code == "camera_lost"
    refusing.close()
    refusing.close()
    assert (refusing.opens, refusing.closes, refusing.is_open, refusing.info) == (1, 1, False, None)
    with pytest.raises(CameraError):
        refusing.read(0.1)  # closed


def test_closing_wakes_a_blocked_read() -> None:
    camera = FakeCamera(fps=0.5)
    camera.open()
    camera.read(1.0)  # frame 1; frame 2 is two seconds away
    outcome: list[object] = []

    def reader() -> None:
        try:
            outcome.append(camera.read(5.0))
        except CameraError as exc:
            outcome.append(exc.code)

    thread = threading.Thread(target=reader)
    thread.start()
    time.sleep(0.1)
    started = time.monotonic()
    camera.close()
    thread.join(2.0)
    assert outcome == ["camera_lost"] and time.monotonic() - started < 0.5


# --------------------------------------------------------------------------- FakeTracker


def test_fake_tracker_replays_steps_from_its_first_frame() -> None:
    tracker = FakeTracker([(0.5, [{"pose": "fist"}]), (0.0, [("palm", (0.4, 0.4))]), (1.0, [])])
    assert [h.handedness for h in tracker.process(BLACK, 100.0).hands] == ["right"]
    palm = tracker.process(BLACK, 100.2).hands[0]
    assert anchor_point(palm.image).x == pytest.approx(0.4)
    fist = tracker.process(BLACK, 100.7)
    assert PoseTracker().classify(fist.hands[0], aspect=720 / 1280).pose == "fist"
    assert tracker.process(BLACK, 103.0).hands == ()  # the last step holds
    assert tracker.calls == 4 and tracker.infer_ms >= 0


def test_fake_tracker_before_the_first_step_sees_no_hands() -> None:
    tracker = FakeTracker([(1.0, [{"pose": "palm"}])])
    assert tracker.process(BLACK, 5.0).hands == ()
    assert len(tracker.process(BLACK, 6.0).hands) == 1


def test_fake_tracker_reports_at_most_num_hands_like_mediapipe() -> None:
    two = [{"pose": "fist", "at": [0.3, 0.5]}, {"pose": "fist", "at": [0.7, 0.5], "handedness": "left"}]
    tracker = FakeTracker([(0.0, two)])
    assert len(tracker.process(BLACK, 1.0).hands) == 1
    tracker.set_num_hands(2)
    assert [h.handedness for h in tracker.process(BLACK, 1.1).hands] == ["right", "left"]
    assert tracker.num_hands == 2 and tracker.num_hands_history == [2]
    with pytest.raises(ValueError):
        tracker.set_num_hands(3)


def test_fake_tracker_takes_a_function_of_elapsed_time() -> None:
    elapsed: list[float] = []

    def script(t: float) -> object:
        elapsed.append(t)
        return Frame(t, (synthetic.hand("two", size=(640, 480)),), 640, 480)

    tracker = FakeTracker(script)  # type: ignore[arg-type]
    image = np.zeros((480, 640, 3), np.uint8)
    tracker.process(image, 50.0)
    frame = tracker.process(image, 50.25)
    assert elapsed == [0.0, 0.25]
    assert (frame.t, frame.width, frame.height) == (50.25, 640, 480)  # the capture's time and size


def test_fake_tracker_projects_into_the_images_size() -> None:
    tracker = FakeTracker([(0.0, [{"pose": "palm", "at": [0.5, 0.5]}])])
    frame = tracker.process(np.zeros((480, 640, 3), np.uint8), 1.0)
    assert (frame.width, frame.height) == (640, 480)
    expected = synthetic.hand("palm", (0.5, 0.5), size=(640, 480)).image
    assert np.allclose(frame.hands[0].image, expected)


def test_a_closed_fake_tracker_sees_nothing() -> None:
    tracker = FakeTracker([(0.0, [{"pose": "palm"}])])
    tracker.close()
    tracker.close()
    assert tracker.process(BLACK, 1.0).hands == ()


def test_build_hand_accepts_observations_mappings_and_tuples() -> None:
    observation = synthetic.hand("fist")
    assert build_hand(observation, (1280, 720)) is observation
    assert build_hand({"pose": "two", "score": 0.5}, (1280, 720)).score == 0.5
    assert build_hand(("pinch", (0.2, 0.3), "left"), (1280, 720)).handedness == "left"
    for bad in ({"pose": "wave"}, {"at": [0.5]}, {"handedness": "both"}, {"score": 2}, {"colour": "red"}, "palm"):
        with pytest.raises(ValueError):
            build_hand(bad, (1280, 720))  # type: ignore[arg-type]


# --------------------------------------------------------------------------- --fake-script


def test_load_script_reads_json_lines(tmp_path: Path) -> None:
    path = tmp_path / "frames.jsonl"
    path.write_text(
        '﻿{"t": 0, "hands": [{"pose": "palm", "at": [0.5, 0.45], "handedness": "right"}]}\n'
        "\n"
        '{"t": 1.5, "hands": []}\n'
        '{"t": 1}\n',
        encoding="utf-8",
    )
    steps = load_script(path)
    assert steps == [(0.0, [{"pose": "palm", "at": [0.5, 0.45], "handedness": "right"}]), (1.5, []), (1.0, [])]
    tracker = FakeTracker(steps)
    assert len(tracker.process(BLACK, 0.0).hands) == 1
    assert tracker.process(BLACK, 1.2).hands == ()  # steps are sorted by time


@pytest.mark.parametrize(
    ("line", "error"),
    [
        ("not json", "line"),
        ("[1, 2]", "JSON object"),
        ('{"t": -1, "hands": []}', "'t'"),
        ('{"t": "soon", "hands": []}', "'t'"),
        ('{"t": 0, "hands": {}}', "'hands'"),
        ('{"t": 0, "hands": [{"pose": "wave"}]}', "wave"),
        ('{"t": 0, "hands": [{"at": [2]}]}', "'at'"),
        ('{"t": 0, "frames": []}', "frames"),
    ],
)
def test_load_script_errors_name_the_line(tmp_path: Path, line: str, error: str) -> None:
    path = tmp_path / "frames.jsonl"
    path.write_text('{"t": 0, "hands": []}\n' + line + "\n", encoding="utf-8")
    with pytest.raises(ValueError) as info:
        load_script(path)
    assert f"{path}:2:" in str(info.value) and error in str(info.value)
