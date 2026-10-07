"""The diagnostics: ``doctor`` (a JSON report that never raises) and ``preview``'s drawing and exits."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from jarvis_hands import camera as camera_pkg
from jarvis_hands import desktop as desktop_pkg
from jarvis_hands import doctor, models, preview
from jarvis_hands import tracker as tracker_pkg
from jarvis_hands.camera.fake import FakeCamera
from jarvis_hands.desktop.fake import FakeDesktop
from jarvis_hands.landmarks import Frame
from jarvis_hands.poses import PoseTracker
from jarvis_hands.synthetic import HEIGHT, WIDTH, hand
from jarvis_hands.tracker.base import TrackerError
from jarvis_hands.tracker.fake import FakeTracker

from scripted import display

KEYS = {
    "version",
    "platform",
    "python",
    "executable",
    "dataDir",
    "packages",
    "model",
    "tracker",
    "calibration",
    "cameraAccess",
    "cameras",
    "cameraTest",
    "displays",
    "overlay",
    "problems",
    "ok",
}


def checks(report: dict[str, Any]) -> list[str]:
    return [p["check"] for p in report["problems"]]


def test_doctor_without_the_camera_reports_every_section(tmp_path: Path) -> None:
    report = doctor.run_doctor(tmp_path, camera=False)
    assert set(report) == KEYS
    json.dumps(report, allow_nan=False)  # what cli.cmd_doctor prints
    assert report["dataDir"] == str(tmp_path) and report["python"] == sys.version.split()[0]
    assert report["packages"]["mediapipe"] == doctor.PINNED_MEDIAPIPE
    assert report["packages"]["opencv"] and report["packages"]["numpy"]
    assert report["model"] == {"installed": False, "path": str(models.model_path(tmp_path))}
    assert report["tracker"] == {"skipped": True, "reason": "no hand model"}
    assert report["cameraTest"] == {"skipped": True}
    assert report["calibration"]["calibrated"] is False
    assert report["ok"] is (report["problems"] == [])
    for problem in report["problems"]:
        assert set(problem) <= {"check", "message", "hint"} and problem["message"]
    model = next(p for p in report["problems"] if p["check"] == "model")
    assert "/jarvis setup hands" in model["hint"]


@pytest.mark.skipif(sys.platform == "win32", reason="the desktop backend exists on Windows")
def test_doctor_off_windows_says_the_desktop_is_not_supported(tmp_path: Path) -> None:
    report = doctor.run_doctor(tmp_path, camera=False)
    assert report["displays"]["supported"] is False
    assert report["overlay"] == {"supported": False, "backend": "NullOverlay"}
    assert "displays" in checks(report) and report["ok"] is False


def test_a_failing_check_is_reported_not_raised(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args: Any) -> Any:
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(doctor, "_packages", broken)
    monkeypatch.setattr(camera_pkg, "list_cameras", broken)
    report = doctor.run_doctor(tmp_path, camera=False)
    assert report["packages"] == {"ok": False, "error": "RuntimeError: probe exploded"}
    assert report["cameras"] == {"ok": False, "error": "RuntimeError: probe exploded"}
    assert {"packages", "cameras"} <= set(checks(report)) and report["ok"] is False
    assert report["model"]["installed"] is False  # the other checks still ran


def test_a_mediapipe_other_than_the_pinned_one_is_a_problem(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = doctor._version
    monkeypatch.setattr(doctor, "_version", lambda name: "1.1.0" if name == "mediapipe" else real(name))
    report = doctor.run_doctor(tmp_path, camera=False)
    problem = next(p for p in report["problems"] if p["check"] == "packages")
    assert "1.1.0" in problem["message"] and doctor.PINNED_MEDIAPIPE in problem["message"]


def test_the_camera_test_opens_reads_and_closes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    made: list[FakeCamera] = []

    def create_camera(spec: str | None, **kwargs: Any) -> FakeCamera:
        camera = FakeCamera(frames=np.full((720, 1280, 3), 90, dtype=np.uint8))
        made.append(camera)
        return camera

    monkeypatch.setattr(camera_pkg, "create_camera", create_camera)
    monkeypatch.setattr(camera_pkg, "list_cameras", lambda: [(0, "UGREEN camera")])
    monkeypatch.delenv("JARVIS_HANDS_CAMERA", raising=False)
    report = doctor.run_doctor(tmp_path, camera=True)
    test = report["cameraTest"]
    assert test["ok"] is True and (test["backend"], test["width"], test["height"]) == ("fake", 1280, 720)
    assert test["frame"] == [1280, 720] and test["meanLevel"] == 90.0 and test["fps"] == 30.0
    assert report["cameras"] == {"list": [{"index": 0, "name": "UGREEN camera"}]}
    assert not {"cameraTest", "cameras"} & set(checks(report))
    assert made[0].opens == 1 and not made[0].is_open


def test_a_black_or_blocked_camera_is_a_problem(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(camera_pkg, "create_camera", lambda spec, **kw: FakeCamera())
    monkeypatch.setattr(camera_pkg, "list_cameras", lambda: [])
    report = doctor.run_doctor(tmp_path, camera=True)
    black = next(p for p in report["problems"] if p["check"] == "cameraTest")
    assert "black" in black["message"] and "shutter" in black["hint"]
    assert "cameras" not in checks(report)  # it opened, so the empty list was the lister's fault

    monkeypatch.setattr(camera_pkg, "create_camera", lambda spec, **kw: FakeCamera(fail_open="camera_blocked"))
    report = doctor.run_doctor(tmp_path, camera=True)
    assert report["cameraTest"]["ok"] is False and report["cameraTest"]["code"] == "camera_blocked"
    assert {"cameraTest", "cameras"} <= set(checks(report))


def test_displays_are_listed_with_virtual_ones_flagged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    desktop = FakeDesktop(
        [display(1, 0, 0, 2560, 1440, primary=True), display(2, 2560, 0, 1920, 1080, virtual=True, name="VDD")]
    )
    monkeypatch.setattr(desktop_pkg, "create_desktop", lambda: desktop)
    report = doctor.run_doctor(tmp_path, camera=False)
    section = report["displays"]
    assert section["supported"] is True and section["virtual"] == [2] and "note" in section
    assert [(d["id"], d["virtual"], d["used"]) for d in section["list"]] == [(1, False, True), (2, True, False)]
    assert desktop.closed and "displays" not in checks(report)


@pytest.mark.skipif(
    not os.environ.get("JARVIS_HANDS_MODELS_DIR"), reason="set JARVIS_HANDS_MODELS_DIR to run the real model"
)
def test_doctor_runs_the_real_model_once(tmp_path: Path) -> None:
    source = Path(os.environ["JARVIS_HANDS_MODELS_DIR"]) / models.MODEL_NAME
    if not source.is_file():
        pytest.skip(f"no {models.MODEL_NAME} in JARVIS_HANDS_MODELS_DIR")
    models.models_dir(tmp_path).mkdir(parents=True)
    shutil.copy(source, models.model_path(tmp_path))
    report = doctor.run_doctor(tmp_path, camera=False)
    assert report["model"]["installed"] is True and "model" not in checks(report)
    assert report["tracker"]["ok"] is True and report["tracker"]["hands"] == 0
    assert report["tracker"]["inferMs"] > 0


def test_the_doctor_command_prints_the_report(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if k != "JARVIS_HANDS_CAMERA"}
    proc = subprocess.run(
        [sys.executable, "-m", "jarvis_hands", "doctor", "--no-camera", "--data-dir", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert set(report) == KEYS and report["cameraTest"] == {"skipped": True}


# --------------------------------------------------------------------------- preview


def test_preview_without_the_model_exits_1_before_touching_the_camera(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def no_camera(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the camera must stay off")

    monkeypatch.setattr(camera_pkg, "create_camera", no_camera)
    args = argparse.Namespace(data_dir=tmp_path, camera=None, width=1280, height=720, fps=30)
    with caplog.at_level(logging.ERROR, logger="jarvis_hands.preview"):
        assert preview.run_preview(args) == 1
    assert "hand model is not installed" in caplog.text and "/jarvis setup hands" in caplog.text


def test_a_tracker_failing_mid_preview_exits_1_and_closes_the_camera(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import cv2

    class Failing(FakeTracker):
        def process(self, image: np.ndarray, t: float) -> Frame:
            raise TrackerError("tracker_failed", "The hand model stopped working.", "Restart the preview.")

    camera, tracker = FakeCamera(), Failing()
    monkeypatch.setattr(models, "is_installed", lambda data_dir: True)
    monkeypatch.setattr(tracker_pkg, "create_tracker", lambda *args, **kwargs: tracker)
    monkeypatch.setattr(camera_pkg, "create_camera", lambda *args, **kwargs: camera)
    # No window on a test runner: everything the preview asks of HighGUI answers like an open window.
    for name in ("namedWindow", "imshow", "destroyAllWindows"):
        monkeypatch.setattr(cv2, name, lambda *args, **kwargs: None)
    monkeypatch.setattr(cv2, "waitKey", lambda *args: -1)
    monkeypatch.setattr(cv2, "getWindowProperty", lambda *args: 1.0)
    args = argparse.Namespace(data_dir=tmp_path, camera=None, width=1280, height=720, fps=30)
    with caplog.at_level(logging.ERROR, logger="jarvis_hands.preview"):
        assert preview.run_preview(args) == 1
    assert "The hand model stopped working. Restart the preview." in caplog.text
    assert camera.closes == 1 and not camera.is_open and tracker.closed


def test_hand_labels_name_each_hands_pose() -> None:
    frame = Frame(1.0, (hand("palm", (0.3, 0.5)), hand("fist", (0.7, 0.5), handedness="left")), WIDTH, HEIGHT)
    trackers: dict[str, PoseTracker] = {}
    assert preview.hand_labels(frame, trackers) == ["right palm", "left fist"]
    assert set(trackers) == {"right", "left"}
    preview.hand_labels(Frame(1.1, (hand("two"),), WIDTH, HEIGHT), trackers)
    assert set(trackers) == {"right"}  # the left hand left: its hysteresis starts over


def test_annotate_draws_the_hands_onto_the_picture() -> None:
    image = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
    frame = Frame(1.0, (hand("palm", (0.5, 0.5)),), WIDTH, HEIGHT)
    out = preview.annotate(image, frame, ["right palm"], 29.7, 12.3)
    assert out is image
    x, y = round(float(frame.hands[0].image[0, 0]) * WIDTH), round(float(frame.hands[0].image[0, 1]) * HEIGHT)
    assert image[y, x].tolist() == list(preview.HAND_COLOURS["right"])  # the wrist dot
    assert image[:60].any()  # the status line


def test_fps_meter() -> None:
    meter = preview.FpsMeter(window=2.0)
    rates = [meter.tick(10.0 + i / 30) for i in range(31)]
    assert rates[0] == 0.0 and rates[-1] == pytest.approx(30.0)


def test_preview_errors_do_not_repeat_themselves() -> None:
    assert (
        preview._explain("No camera found.", "No camera found. Check the webcam.")
        == "No camera found. Check the webcam."
    )
    assert preview._explain("Camera in use.", "Close Zoom.") == "Camera in use. Close Zoom."
    assert preview._explain("Camera in use.", None) == "Camera in use."
