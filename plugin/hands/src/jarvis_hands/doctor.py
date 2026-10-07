"""``doctor``: a JSON health report for hand control.

What can stop hand control from working on a given PC, checked one by one:
the packages (MediaPipe pinned to the telemetry-free 0.10.33), the hand
model, whether the model actually runs, the cameras and a short camera test,
the Windows camera privacy switches, the displays (virtual ones flagged,
since they are left out by default) and the overlay.

Every check catches its own failure and reports it as a problem with a hint,
so the report is always produced and one broken probe never hides the rest.
``ok`` is true when there are no problems. Off Windows the desktop check
reports one, by design: hand control drives the desktop on Windows only.
"""

from __future__ import annotations

import importlib.metadata
import logging
import math
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import __version__, clock, models
from . import platform as plat
from .settings import calibration_path, load_calibration

log = logging.getLogger(__name__)

#: The MediaPipe the helper is pinned to: the newest without usage telemetry.
PINNED_MEDIAPIPE = "0.10.33"
OPENCV_DISTRIBUTIONS = (
    "opencv-contrib-python",
    "opencv-python",
    "opencv-contrib-python-headless",
    "opencv-python-headless",
)
#: How long the camera test waits for its first picture.
CAMERA_FRAME_TIMEOUT_S = 5.0
#: A mean pixel level below this (of 255) is a black picture: a closed shutter, or a camera sending nothing.
BLACK_LEVEL = 2.0
SETUP_HINT = "Run /jarvis setup hands (or python -m jarvis_hands setup)."


class _Problems:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def add(self, check: str, message: str, hint: str | None = None) -> None:
        item: dict[str, Any] = {"check": check, "message": message}
        if hint:
            item["hint"] = hint
        self.items.append(item)


def run_doctor(data_dir: Path, camera: bool = True) -> dict[str, Any]:
    """The report; never raises. ``camera``: also open the camera for a moment (its light comes on)."""
    data_dir = Path(data_dir)
    problems = _Problems()
    report: dict[str, Any] = {
        "version": __version__,
        "platform": plat.name(),
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "dataDir": str(data_dir),
    }
    checks: tuple[tuple[str, Callable[[], dict[str, Any]]], ...] = (
        ("packages", lambda: _packages(problems)),
        ("model", lambda: _model(data_dir, problems)),
        ("tracker", lambda: _tracker(data_dir, problems)),
        ("calibration", lambda: _calibration(data_dir)),
        ("cameraAccess", lambda: _camera_access(problems)),
        ("cameras", _cameras),
        ("cameraTest", lambda: _camera_test(problems) if camera else {"skipped": True}),
        ("displays", lambda: _displays(problems)),
        ("overlay", _overlay),
    )
    for key, check in checks:
        try:
            report[key] = check()
        except Exception as exc:  # noqa: BLE001 - one broken probe must not hide the rest
            message = f"{type(exc).__name__}: {exc}"
            report[key] = {"ok": False, "error": message}
            problems.add(key, f"The {key} check failed: {message}")

    listed = report.get("cameras", {}).get("list")
    tested = report.get("cameraTest", {})
    if listed == [] and not tested.get("ok"):
        # An empty list alone proves nothing (listing can fail where opening works), hence the test's say.
        message = "The system lists no camera" + (
            " (the camera test was skipped)." if tested.get("skipped") else ", and opening one failed."
        )
        problems.add("cameras", message, plat.no_camera_hint())

    report["problems"] = problems.items
    report["ok"] = not problems.items
    return report


def _version(distribution: str) -> str | None:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return None


def _packages(problems: _Problems) -> dict[str, Any]:
    mediapipe = _version("mediapipe")
    opencv = next(((d, v) for d in OPENCV_DISTRIBUTIONS if (v := _version(d)) is not None), None)
    section: dict[str, Any] = {
        "mediapipe": mediapipe,
        "opencv": opencv[1] if opencv else None,
        "opencvPackage": opencv[0] if opencv else None,
        "numpy": _version("numpy"),
    }
    if mediapipe is None:
        problems.add("packages", "MediaPipe is not installed in the hand helper's Python.", SETUP_HINT)
    elif mediapipe != PINNED_MEDIAPIPE:
        problems.add(
            "packages",
            f"MediaPipe {mediapipe} is installed; the helper is pinned to {PINNED_MEDIAPIPE} "
            "(0.10.35 and later send usage data to Google).",
            SETUP_HINT,
        )
    if opencv is None:
        problems.add("packages", "OpenCV is not installed in the hand helper's Python.", SETUP_HINT)
    return section


def _model(data_dir: Path, problems: _Problems) -> dict[str, Any]:
    path = models.model_path(data_dir)
    installed = models.is_installed(data_dir)
    section: dict[str, Any] = {"installed": installed, "path": str(path)}
    if not installed:
        if path.exists():
            problems.add("model", f"The hand model at {path} is damaged (wrong size).", SETUP_HINT)
        else:
            problems.add("model", "The hand model is not installed.", SETUP_HINT)
    return section


def _tracker(data_dir: Path, problems: _Problems) -> dict[str, Any]:
    """Load the model and run it once on a blank frame: proves MediaPipe works on this PC."""
    if not models.is_installed(data_dir):
        return {"skipped": True, "reason": "no hand model"}
    import numpy as np

    from .tracker import TrackerError, create_tracker

    started = time.perf_counter()
    try:
        tracker = create_tracker(models.model_path(data_dir))
    except TrackerError as exc:
        problems.add("tracker", exc.message, exc.hint)
        return {"ok": False, "code": exc.code, "message": exc.message, "hint": exc.hint}
    try:
        load_ms = (time.perf_counter() - started) * 1000
        frame = tracker.process(np.zeros((720, 1280, 3), dtype=np.uint8), clock.now())
        return {"ok": True, "loadMs": _round(load_ms), "inferMs": _round(tracker.infer_ms), "hands": len(frame.hands)}
    except TrackerError as exc:
        problems.add("tracker", exc.message, exc.hint)
        return {"ok": False, "code": exc.code, "message": exc.message, "hint": exc.hint}
    finally:
        tracker.close()


def _calibration(data_dir: Path) -> dict[str, Any]:
    return {"calibrated": load_calibration(data_dir) is not None, "path": str(calibration_path(data_dir))}


def _camera_access(problems: _Problems) -> dict[str, Any]:
    denied = plat.camera_access_denied()
    if denied:
        problems.add(
            "cameraAccess", "Windows privacy settings block desktop apps from the camera.", plat.camera_blocked_hint()
        )
    return {"denied": denied}


def _cameras() -> dict[str, Any]:
    from .camera import list_cameras

    return {"list": [{"index": int(index), "name": str(name)} for index, name in list_cameras()]}


def _camera_test(problems: _Problems) -> dict[str, Any]:
    from .camera import CameraError, create_camera

    spec = os.environ.get("JARVIS_HANDS_CAMERA", "").strip() or None
    camera = create_camera(spec)
    started = time.perf_counter()
    try:
        info = camera.open()
        open_ms = (time.perf_counter() - started) * 1000
        captured = camera.read(CAMERA_FRAME_TIMEOUT_S)
        info = camera.info or info
        section: dict[str, Any] = {
            "ok": captured is not None,
            "camera": spec,
            "name": info.name,
            "index": info.index,
            "backend": info.backend,
            "width": info.width,
            "height": info.height,
            "fps": _round(info.fps),
            "openMs": _round(open_ms),
        }
        if captured is None:
            problems.add(
                "cameraTest",
                f"{info.name} opened but sent no picture within {CAMERA_FRAME_TIMEOUT_S:.0f} s.",
                plat.camera_in_use_hint(),
            )
            return section
        level = float(captured.image.mean()) if captured.image.size else 0.0
        section["frame"] = [int(captured.image.shape[1]), int(captured.image.shape[0])]
        section["meanLevel"] = _round(level)
        if level < BLACK_LEVEL:
            problems.add(
                "cameraTest",
                f"{info.name} sends a black picture.",
                "Open the webcam's privacy shutter (or switch it on), and check the room is not dark.",
            )
        return section
    except CameraError as exc:
        problems.add("cameraTest", exc.message, exc.hint)
        return {"ok": False, "camera": spec, "code": exc.code, "message": exc.message, "hint": exc.hint}
    finally:
        camera.close()


def _displays(problems: _Problems) -> dict[str, Any]:
    from .desktop import UnsupportedPlatform, create_desktop
    from .mapping import select_displays
    from .protocol import display_dict

    try:
        desktop = create_desktop()
    except UnsupportedPlatform as exc:
        problems.add("displays", str(exc), "Hand control needs Windows 10 or 11 for now.")
        return {"supported": False, "message": str(exc)}
    try:
        displays = desktop.displays()
    finally:
        desktop.close()
    used = {d.id for d in select_displays(displays, "all")}
    section: dict[str, Any] = {
        "supported": True,
        "list": [display_dict(d, d.id in used) for d in displays],
        "virtual": [d.id for d in displays if d.virtual],
    }
    if section["virtual"]:
        section["note"] = (
            "Virtual displays (a virtual display driver, a streaming dummy) are left out by default; "
            "/jarvis hands display <n> chooses one."
        )
    if not displays:
        problems.add("displays", "Windows reports no displays.", None)
    return section


def _overlay() -> dict[str, Any]:
    from .overlay import NullOverlay, create_overlay

    overlay = create_overlay()  # not started: no window appears
    try:
        return {"supported": not isinstance(overlay, NullOverlay), "backend": type(overlay).__name__}
    finally:
        overlay.close()


def _round(value: float, digits: int = 1) -> float:
    value = float(value)
    return round(value, digits) if math.isfinite(value) else 0.0
