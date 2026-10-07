"""HandsRuntime end to end with the fakes: a fake camera, a tracker the test steers, the fake desktop.

Everything runs on the real threads and the real clock (the engine times a
palm hold in seconds), so the tests wait for events rather than sleeping a
fixed time. Every event the runtime emits and every command answer is
checked against plugin/protocol/hands.schema.json.
"""

from __future__ import annotations

import dataclasses
import json
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import jsonschema
import numpy as np
import pytest

from jarvis_hands import cli, clock, protocol
from jarvis_hands import runtime as runtime_module
from jarvis_hands.camera.base import CameraError, CameraFrame, CameraInfo
from jarvis_hands.camera.fake import FakeCamera
from jarvis_hands.desktop.base import Display, UnsupportedPlatform
from jarvis_hands.desktop.fake import FakeDesktop, FakeWindow
from jarvis_hands.geometry import Point, Rect
from jarvis_hands.gestures import EngineView
from jarvis_hands.overlay.base import NullOverlay, OverlayError, OverlayState
from jarvis_hands.runtime import (
    EXIT_FATAL,
    GESTURES_PER_S,
    HandsRuntime,
    RateLimiter,
    RuntimeOptions,
    Throttle,
    overlay_state,
)
from jarvis_hands.settings import calibration_path, load_calibration
from jarvis_hands.tracker.base import TrackerError
from jarvis_hands.tracker.fake import FakeTracker

from scripted import camera_point, display

TIMEOUT = 8.0
SCHEMA = json.loads(protocol.schema_path().read_text(encoding="utf-8"))


def validate(instance: Any, def_name: str) -> None:
    jsonschema.Draft202012Validator({"$ref": f"#/$defs/{def_name}", "$defs": SCHEMA["$defs"]}).validate(instance)


def eventually(condition: Callable[[], Any], timeout: float = TIMEOUT, what: str = "condition") -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = condition()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.01)


class CapturingWriter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.events: list[dict[str, Any]] = []

    def emit(self, event: dict[str, Any]) -> None:
        with self._lock:
            self.events.append(dict(event))

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self.events)

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [e for e in self.snapshot() if e["type"] == kind]

    def states(self) -> list[str]:
        return [e["state"] for e in self.of("state")]

    def gestures(self) -> list[str]:
        return [e["name"] for e in self.of("gesture")]

    def wait_for(self, kind: str, timeout: float = TIMEOUT, after: int = 0, **fields: Any) -> dict[str, Any]:
        """The first event of ``kind`` (with ``fields``) at index ``after`` or later."""

        def find() -> dict[str, Any] | None:
            for event in self.snapshot()[after:]:
                if event["type"] == kind and all(event.get(k) == v for k, v in fields.items()):
                    return event
            return None

        found: dict[str, Any] = eventually(find, timeout, f"{kind} {fields}")
        return found


class Puppet:
    """A FakeTracker script the test steers: the hands it holds now are the hands in view."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hands: list[Any] = []

    def set(self, *hands: Any) -> None:
        with self._lock:
            self._hands = list(hands)

    def __call__(self, elapsed: float) -> list[Any]:
        with self._lock:
            return list(self._hands)


class RecordingOverlay(NullOverlay):
    def __init__(self) -> None:
        self.started = False
        self.closed = False
        self._lock = threading.Lock()
        self.states: list[OverlayState] = []

    def start(self) -> None:
        self.started = True

    def show(self, state: OverlayState) -> None:
        with self._lock:
            self.states.append(state)

    def close(self) -> None:
        self.closed = True

    def modes(self) -> list[str]:
        with self._lock:
            return [s.mode for s in self.states]

    @property
    def mode(self) -> str:
        with self._lock:
            return self.states[-1].mode if self.states else "none"


class Rig:
    def __init__(
        self,
        data_dir: Path,
        *,
        desktop: FakeDesktop | None = None,
        overlay: RecordingOverlay | Callable[[], Any] | None = None,
        tracker: Callable[[Puppet], Any] = FakeTracker,
        real_tracker: bool = False,
        camera: Callable[[], Any] | None = None,
        camera_type: type[FakeCamera] = FakeCamera,
        desktop_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.writer = CapturingWriter()
        self.camera_type = camera_type
        self.desktop = desktop or FakeDesktop()
        self.puppet = Puppet()
        self.tracker = tracker(self.puppet)
        self.cameras: list[FakeCamera] = []
        #: The next camera the factory makes refuses to open with this code.
        self.next_camera_fails: str | None = None
        self.overlay = overlay
        overlay_factory: Callable[[], Any]
        if overlay is None:
            overlay_factory = NullOverlay
        elif isinstance(overlay, RecordingOverlay):
            overlay_factory = lambda: overlay  # noqa: E731
        else:
            overlay_factory = overlay
        self.runtime = HandsRuntime(
            RuntimeOptions(data_dir=data_dir, overlay=overlay is not None),
            self.writer,
            desktop_factory=desktop_factory or (lambda: self.desktop),
            camera_factory=camera or self._camera,
            tracker_factory=None if real_tracker else lambda: self.tracker,
            overlay_factory=overlay_factory,
        )
        self.responses: list[tuple[str, dict[str, Any]]] = []

    def _camera(self) -> FakeCamera:
        camera = self.camera_type(fail_open=self.next_camera_fails)  # type: ignore[arg-type]
        self.next_camera_fails = None
        self.cameras.append(camera)
        return camera

    def start(self) -> Rig:
        self.runtime.start()
        return self

    def started(self) -> Rig:
        """Started, with the camera up and the engine idle."""
        self.start()
        self.writer.wait_for("state", state="idle")
        return self

    def command(self, name: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self.runtime.handle_command(name, body or {})
        self.responses.append((name, response))
        return response

    def engaged(self, at: tuple[float, float] = (0.5, 0.45)) -> Rig:
        """Holds a still open palm until the engine engages."""
        self.puppet.set({"pose": "palm", "at": list(at)})
        self.writer.wait_for("gesture", name="engage")
        self.writer.wait_for("state", state="active")
        return self

    def pressed(self, at: tuple[float, float] = (0.5, 0.45)) -> Rig:
        self.puppet.set({"pose": "pinch", "at": list(at)})
        eventually(lambda: "left" in self.desktop.buttons_down, what="the left button to go down")
        return self

    def check_protocol(self) -> None:
        for event in self.writer.snapshot():
            validate(event, "Event")
        for name, response in self.responses:
            validate(response, "StatusResponse" if name == "status" and response.get("ok") else "CommandResponse")


@pytest.fixture
def make_rig(tmp_path: Path) -> Iterator[Callable[..., Rig]]:
    rigs: list[Rig] = []

    def make(**kwargs: Any) -> Rig:
        rig = Rig(kwargs.pop("data_dir", tmp_path), **kwargs)
        rigs.append(rig)
        return rig

    yield make
    for rig in rigs:
        rig.runtime.stop()
        rig.check_protocol()
        assert not rig.desktop.buttons_down, "a mouse button was left down"


# --------------------------------------------------------------------------- pure helpers


def test_exit_code_for_fatal_errors_is_the_clis_error_code() -> None:
    assert EXIT_FATAL == cli.EXIT_ERROR


def test_rate_limiter_allows_ten_gestures_in_any_second() -> None:
    limit = RateLimiter(GESTURES_PER_S, 1.0)
    assert [limit.allow(10.0 + i * 0.01) for i in range(12)] == [True] * 10 + [False] * 2
    assert limit.allow(11.0) is True  # the first one left the window
    assert limit.allow(11.005) is False


def test_throttle_lets_each_key_through_once_per_interval() -> None:
    throttle = Throttle(10.0)
    assert throttle.allow("input_blocked", 0.0) and not throttle.allow("input_blocked", 9.9)
    assert throttle.allow("overlay_failed", 1.0)
    assert throttle.allow("input_blocked", 10.0)


def view(**overrides: Any) -> EngineView:
    fields: dict[str, Any] = {
        "state": "idle",
        "hand_visible": True,
        "cursor": Point(100, 200),
        "helper": None,
        "pose": "hover",
        "pinch": 0.25,
        "engage_progress": 0.0,
        "grabbing": False,
        "scrolling": False,
        "pressed": False,
        "calibration_target": None,
        "calibration_progress": 0.0,
    }
    fields.update(overrides)
    return EngineView(**fields)


@pytest.mark.parametrize(
    ("overrides", "dragging", "mode"),
    [
        ({"hand_visible": False}, False, "hidden"),
        ({"cursor": None}, False, "hidden"),
        ({}, False, "idle"),
        ({"engage_progress": 0.4}, False, "engaging"),
        ({"state": "active", "engage_progress": 1.0}, False, "point"),
        ({"state": "active", "pressed": True}, False, "press"),
        ({"state": "active", "pressed": True}, True, "drag"),
        ({"state": "active", "scrolling": True}, False, "scroll"),
        ({"state": "active", "grabbing": True}, False, "grab"),
        ({"state": "active", "grabbing": True, "helper": Point(300, 200)}, False, "resize"),
    ],
)
def test_overlay_state_follows_the_engine_view(overrides: dict[str, Any], dragging: bool, mode: str) -> None:
    state = overlay_state(view(**overrides), dragging=dragging)
    assert state.mode == mode
    if mode != "hidden":
        assert state.cursor == overrides.get("cursor", Point(100, 200))
    assert state.helper == (Point(300, 200) if mode == "resize" else None)
    assert state.progress == (0.4 if mode == "engaging" else 0.0)


def test_overlay_state_shows_the_calibration_target_even_without_a_hand() -> None:
    state = overlay_state(
        view(
            state="calibrating",
            hand_visible=False,
            cursor=None,
            calibration_target=Point(0, 0),
            calibration_progress=0.5,
        )
    )
    assert (state.mode, state.cursor, state.progress) == ("calibrate", Point(0, 0), 0.5)


def test_overlay_state_is_hidden_when_the_overlay_is_off() -> None:
    assert overlay_state(view(state="active"), overlay=False).mode == "hidden"


# --------------------------------------------------------------------------- start


def test_start_announces_starting_then_ready_then_idle(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig().started()
    events = rig.writer.snapshot()
    assert [e["type"] for e in events[:3]] == ["state", "ready", "state"]
    assert (events[0]["state"], events[2]["state"]) == ("starting", "idle")
    ready = events[1]
    assert (ready["camera"], ready["width"], ready["height"], ready["fps"]) == ("fake camera", 1280, 720, 30.0)
    assert ready["displays"] == [
        {
            "id": 1,
            "name": r"\\.\DISPLAY1",
            "x": 0,
            "y": 0,
            "width": 1920,
            "height": 1080,
            "primary": True,
            "virtual": False,
            "used": True,
        }
    ]
    assert rig.cameras[0].is_open


def test_a_still_palm_engages_and_a_pinch_clicks(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig().started().engaged()
    rig.pressed()
    rig.puppet.set({"pose": "palm"})
    rig.writer.wait_for("gesture", name="click")
    eventually(lambda: not rig.desktop.buttons_down, what="the button to come up")
    assert rig.desktop.calls_named("button") == [("button", "left", True), ("button", "left", False)]
    assert rig.writer.gestures() == ["engage", "click"]


def test_status_answers_before_start_and_while_running(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    before = rig.command("status")
    validate(before, "StatusResponse")
    assert (before["state"], before["engaged"], before["displays"], before["fps"]) == ("starting", False, [], 0.0)
    assert before["calibrated"] is False and "camera" not in before
    assert before["settings"] == {
        "engage": "palm",
        "hand": "any",
        "anchor": "knuckles",
        "overlay": True,
        "scrollSpeed": 1,
    }

    rig.started().engaged()
    during = rig.command("status")
    validate(during, "StatusResponse")
    assert (during["state"], during["engaged"], during["camera"]) == ("active", True, "fake camera")
    assert 10 <= during["fps"] <= 40  # the fake camera runs at 30
    assert during["inferMs"] >= 0 and [d["id"] for d in during["displays"]] == [1]


def test_config_sent_before_start_is_applied_once_running(make_rig: Callable[..., Rig]) -> None:
    desktop = FakeDesktop([display(1, 0, 0, 1920, 1080, primary=True), display(2, 1920, 0, 1280, 720)])
    rig = make_rig(desktop=desktop)
    assert rig.command("config", {"engage": "always", "scrollSpeed": 2.5, "displays": [2]}) == {"ok": True}
    assert rig.command("status")["settings"]["engage"] == "always"

    rig.started()
    ready = rig.writer.of("ready")[0]
    assert [(d["id"], d["used"]) for d in ready["displays"]] == [(1, False), (2, True)]
    rig.puppet.set({"pose": "hover"})  # engage: always needs no palm hold
    rig.writer.wait_for("gesture", name="engage", timeout=2.0)
    status = rig.command("status")
    assert status["settings"]["scrollSpeed"] == 2.5
    assert eventually(lambda: desktop.cursor_pos[0] >= 1920, what="the cursor on display 2")


def test_config_while_running_changes_the_displays_used(make_rig: Callable[..., Rig]) -> None:
    desktop = FakeDesktop([display(1, 0, 0, 1920, 1080, primary=True), display(2, 1920, 0, 1280, 720)])
    rig = make_rig(desktop=desktop).started()
    assert [d["used"] for d in rig.command("status")["displays"]] == [True, True]
    assert rig.command("config", {"displays": [1]}) == {"ok": True}
    assert [d["used"] for d in rig.command("status")["displays"]] == [True, False]


def test_a_config_value_the_settings_refuse_is_a_bad_request(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    response = rig.command("config", {"scrollSpeed": 50})
    assert response["ok"] is False and response["error"]["code"] == "bad_request"
    assert rig.command("status")["settings"]["scrollSpeed"] == 1


def test_unknown_commands_are_bad_requests(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    assert rig.command("fly")["error"]["code"] == "bad_request"
    assert rig.command("calibrate", {"action": "dance"})["error"]["code"] == "bad_request"


# --------------------------------------------------------------------------- commands


def test_pause_lets_go_and_closes_the_camera_and_resume_reopens_it(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig().started().engaged().pressed()
    mark = len(rig.writer.snapshot())
    assert rig.command("pause") == {"ok": True}
    assert not rig.desktop.buttons_down
    assert not rig.cameras[0].is_open and rig.cameras[0].closes == 1
    rig.writer.wait_for("state", state="paused", after=mark)
    assert rig.writer.wait_for("gesture", name="disengage", after=mark)
    assert rig.command("status")["state"] == "paused"
    assert rig.command("status")["engaged"] is False
    assert rig.command("pause") == {"ok": True}  # again: nothing more to do

    mark = len(rig.writer.snapshot())
    assert rig.command("resume") == {"ok": True}
    assert len(rig.cameras) == 2 and rig.cameras[1].is_open
    rig.writer.wait_for("ready", after=mark)
    rig.writer.wait_for("state", state="idle", after=mark)
    assert rig.command("status")["state"] == "idle"
    rig.puppet.set({"pose": "palm"})
    rig.writer.wait_for("gesture", name="engage", after=mark)


def test_a_failed_resume_answers_with_the_camera_error_and_stays_paused(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig().started()
    rig.command("pause")
    rig.next_camera_fails = "camera_in_use"
    response = rig.command("resume")
    assert response["ok"] is False and response["error"]["code"] == "camera_in_use"
    assert rig.command("status")["state"] == "paused"
    assert rig.runtime.exit_code is None  # not fatal: the user can try again
    assert rig.command("resume") == {"ok": True}
    assert rig.command("status")["state"] == "idle"


def test_pause_before_start_keeps_the_camera_off_until_resume(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    assert rig.command("pause") == {"ok": True}
    assert rig.command("status")["state"] == "paused"
    rig.start()
    rig.writer.wait_for("state", state="paused")
    assert rig.cameras == [] and rig.writer.of("ready") == []
    assert rig.command("resume") == {"ok": True}
    rig.writer.wait_for("ready")
    rig.writer.wait_for("state", state="idle")
    # The first camera open after a pause is a start like any other.
    assert rig.writer.states() == ["starting", "paused", "starting", "idle"]


def test_engage_and_disengage_commands(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig().started()
    rig.puppet.set({"pose": "hover"})  # a hand in view, but no palm hold
    time.sleep(0.3)
    assert rig.command("status")["engaged"] is False
    assert rig.command("engage") == {"ok": True}
    rig.writer.wait_for("gesture", name="engage")
    rig.writer.wait_for("state", state="active")
    assert rig.command("status")["engaged"] is True

    mark = len(rig.writer.snapshot())
    assert rig.command("disengage") == {"ok": True}
    rig.writer.wait_for("gesture", name="disengage", after=mark)
    rig.writer.wait_for("state", state="idle", after=mark)
    assert rig.command("status")["engaged"] is False


def test_engage_before_start_engages_the_first_hand(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    assert rig.command("engage") == {"ok": True}
    rig.started()
    rig.puppet.set({"pose": "hover"})
    rig.writer.wait_for("gesture", name="engage", timeout=2.0)


def test_calibration_saves_the_corners_and_status_reports_it(make_rig: Callable[..., Rig], tmp_path: Path) -> None:
    overlay = RecordingOverlay()
    rig = make_rig(overlay=overlay).started()
    assert rig.command("status")["calibrated"] is False
    assert rig.command("calibrate", {"action": "start"}) == {"ok": True}
    rig.writer.wait_for("calibration", step="top_left")
    rig.writer.wait_for("state", state="calibrating")
    eventually(lambda: overlay.mode == "calibrate", what="the calibration target")

    corners = {"top_left": (0.3, 0.3), "top_right": (0.7, 0.3), "bottom_right": (0.7, 0.65), "bottom_left": (0.3, 0.65)}
    following = {
        "top_left": "top_right",
        "top_right": "bottom_right",
        "bottom_right": "bottom_left",
        "bottom_left": "done",
    }
    for corner, at in corners.items():
        rig.puppet.set({"pose": "palm", "at": list(at)})
        rig.writer.wait_for("calibration", step=following[corner])
    rig.puppet.set()
    eventually(lambda: rig.writer.states()[-1] == "idle", what="idle after calibrating")
    eventually(lambda: calibration_path(tmp_path).is_file(), what="calibration.json")
    saved = load_calibration(tmp_path)
    assert saved is not None
    corner = saved @ np.array([0.3, 0.3, 1.0])
    assert corner[:2] / corner[2] == pytest.approx([0.0, 0.0], abs=0.05)
    assert rig.command("status")["calibrated"] is True


def test_calibrate_cancel_and_calibrate_while_paused(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig().started()
    rig.command("calibrate", {"action": "start"})
    rig.writer.wait_for("state", state="calibrating")
    mark = len(rig.writer.snapshot())
    assert rig.command("calibrate", {"action": "cancel"}) == {"ok": True}
    rig.writer.wait_for("calibration", step="cancelled", after=mark)
    rig.writer.wait_for("state", state="idle", after=mark)
    rig.command("pause")
    response = rig.command("calibrate", {"action": "start"})
    assert response["ok"] is False and response["error"]["code"] == "bad_request"


def test_commands_from_many_threads_at_once(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig().started().engaged()
    failures: list[BaseException] = []
    responses: list[tuple[str, dict[str, Any]]] = []
    lock = threading.Lock()

    def hammer(seed: int) -> None:
        names = ["status", "config", "engage", "disengage", "status"]
        try:
            for i in range(40):
                name = names[(i + seed) % len(names)]
                body = {"scrollSpeed": 1.0 + (i % 5)} if name == "config" else {}
                response = rig.runtime.handle_command(name, body)
                with lock:
                    responses.append((name, response))
        except BaseException as exc:  # noqa: BLE001 - handed to the main thread's assert
            failures.append(exc)

    threads = [threading.Thread(target=hammer, args=(seed,)) for seed in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(TIMEOUT)
    assert not failures
    assert len(responses) == 240
    for name, response in responses:
        assert response["ok"] is True, response
        validate(response, "StatusResponse" if name == "status" else "OkResponse")


# --------------------------------------------------------------------------- stopping


def test_request_stop_before_start_starts_nothing(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.runtime.request_stop()
    rig.runtime.start()
    started = time.monotonic()
    rig.runtime.wait()
    assert time.monotonic() - started < 1.0
    assert rig.writer.snapshot() == [] and rig.cameras == []
    rig.runtime.stop()
    assert rig.runtime.exit_code is None


def test_request_stop_during_start_stops_before_the_loop_runs(make_rig: Callable[..., Rig]) -> None:
    holder: list[Rig] = []

    def camera() -> FakeCamera:
        holder[0].runtime.request_stop()  # e.g. a signal while the camera opens
        return holder[0]._camera()

    rig = make_rig(camera=camera)
    holder.append(rig)
    rig.runtime.start()
    rig.runtime.wait()
    assert rig.runtime._loop_thread is None
    rig.runtime.stop()
    assert len(rig.cameras) == 1 and not rig.cameras[0].is_open
    assert rig.tracker.closed and rig.desktop.closed


def test_request_stop_from_another_thread_while_the_camera_opens(make_rig: Callable[..., Rig]) -> None:
    opening, release = threading.Event(), threading.Event()
    holder: list[Rig] = []

    def slow_camera() -> FakeCamera:
        opening.set()
        release.wait(TIMEOUT)
        return holder[0]._camera()

    rig = make_rig(camera=slow_camera)
    holder.append(rig)
    starter = threading.Thread(target=rig.runtime.start)
    starter.start()
    assert opening.wait(TIMEOUT)
    rig.runtime.request_stop()
    waited = threading.Thread(target=rig.runtime.wait)
    waited.start()
    waited.join(1.0)
    assert not waited.is_alive()  # wait() returns while start() is still busy
    release.set()
    starter.join(TIMEOUT)
    rig.runtime.stop()
    assert rig.runtime._loop_thread is None
    assert all(not c.is_open for c in rig.cameras)


def test_stop_is_idempotent_and_safe_without_start(make_rig: Callable[..., Rig]) -> None:
    never = make_rig()
    never.runtime.stop()
    never.runtime.stop()
    assert not never.desktop.closed  # nothing was made, nothing to close

    rig = make_rig().started().engaged().pressed()
    rig.runtime.request_stop()
    rig.runtime.wait()
    rig.runtime.stop()
    rig.runtime.stop()
    assert not rig.desktop.buttons_down
    assert rig.desktop.calls_named("close") == [("close",)]
    assert rig.cameras[0].closes == 1 and rig.tracker.closed
    assert rig.command("status")["ok"] is True  # still answers
    assert rig.command("engage")["error"]["code"] == "internal"  # but does nothing more


# --------------------------------------------------------------------------- errors


def test_a_camera_that_will_not_open_is_fatal(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.next_camera_fails = "camera_in_use"
    rig.runtime.start()
    rig.runtime.wait()
    error = rig.writer.wait_for("error")
    assert (error["code"], error["fatal"]) == ("camera_in_use", True)
    assert error["message"] and error["hint"]
    assert rig.writer.states() == ["starting", "error"]
    assert rig.runtime.exit_code == EXIT_FATAL
    assert rig.command("status")["state"] == "error"


def test_an_unsupported_platform_is_fatal(make_rig: Callable[..., Rig]) -> None:
    def no_desktop() -> Any:
        raise UnsupportedPlatform("no desktop backend here")

    rig = make_rig(desktop_factory=no_desktop)
    rig.runtime.start()
    rig.runtime.wait()
    error = rig.writer.wait_for("error")
    assert (error["code"], error["fatal"]) == ("unsupported_platform", True)
    assert rig.runtime.exit_code == EXIT_FATAL and rig.cameras == []


def test_a_missing_model_is_fatal_with_its_hint(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig(real_tracker=True)  # the real factory, and no model in the data dir
    rig.runtime.start()
    rig.runtime.wait()
    error = rig.writer.wait_for("error")
    assert (error["code"], error["fatal"]) == ("model_missing", True)
    assert "/jarvis setup hands" in error["hint"]
    assert rig.cameras == []  # the camera never came on


def test_a_tracker_error_is_fatal_with_its_code(make_rig: Callable[..., Rig]) -> None:
    class Broken(FakeTracker):
        def process(self, image: np.ndarray, t: float) -> Any:
            raise TrackerError("tracker_failed", "the model stopped", "Restart hand control.")

    rig = make_rig(tracker=lambda _: Broken())
    rig.runtime.start()
    rig.runtime.wait()
    error = rig.writer.wait_for("error")
    assert (error["code"], error["fatal"], error["hint"]) == ("tracker_failed", True, "Restart hand control.")


def test_losing_the_camera_mid_run_is_fatal_and_lets_go_of_the_mouse(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig().started().engaged().pressed()
    rig.cameras[0].fail("camera_lost")
    rig.runtime.wait()
    error = rig.writer.wait_for("error")
    assert (error["code"], error["fatal"]) == ("camera_lost", True)
    assert not rig.desktop.buttons_down
    assert rig.writer.states()[-1] == "error"
    assert rig.runtime.exit_code == EXIT_FATAL


class StallingCamera(FakeCamera):
    """Stops sending frames without failing, as the real camera does while it reopens a hung device."""

    stalled = False

    def read(self, timeout: float) -> CameraFrame | None:
        if self.stalled:
            self._sleep(timeout)
            return None
        return super().read(timeout)


def test_a_camera_that_stops_sending_frames_lets_go_of_the_mouse(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig(camera_type=StallingCamera).started().engaged().pressed()
    camera = rig.cameras[0]
    assert isinstance(camera, StallingCamera)
    mark = len(rig.writer.snapshot())
    camera.stalled = True
    # No frames is no hand: the button comes up a read timeout into the stall, not when frames come back.
    eventually(lambda: not rig.desktop.buttons_down, timeout=2.0, what="the button released during the stall")
    rig.writer.wait_for("gesture", name="disengage", after=mark, timeout=4.0)  # lost_s later
    rig.writer.wait_for("state", state="idle", after=mark)
    status = rig.command("status")
    assert (status["state"], status["engaged"]) == ("idle", False)
    assert rig.runtime.exit_code is None  # a stall is not an error: the camera may come back

    mark = len(rig.writer.snapshot())
    rig.puppet.set({"pose": "palm"})
    camera.stalled = False
    rig.writer.wait_for("gesture", name="engage", after=mark)


class ImpatientCamera(StallingCamera):
    """Gives up on a read at once while stalled, as the real one does once its device failed."""

    def read(self, timeout: float) -> CameraFrame | None:
        return None if self.stalled else super().read(timeout)


def test_a_camera_that_answers_at_once_without_frames_is_not_spun_on(make_rig: Callable[..., Rig]) -> None:
    overlay = RecordingOverlay()
    rig = make_rig(camera_type=ImpatientCamera, overlay=overlay).started().engaged().pressed()
    rig.cameras[0].stalled = True  # type: ignore[attr-defined]
    eventually(lambda: not rig.desktop.buttons_down, timeout=2.0, what="the button released")
    shown = len(overlay.states)
    time.sleep(0.5)
    assert len(overlay.states) - shown < 50  # one empty frame per backoff, not thousands


class SkewedCamera(FakeCamera):
    """Stamps its frames ``skew`` seconds off ``clock.now()``: a camera on a clock of its own."""

    skew = 0.0

    def read(self, timeout: float) -> CameraFrame | None:
        frame = super().read(timeout)
        return None if frame is None else CameraFrame(frame.seq, frame.t + self.skew, frame.image)


@pytest.mark.parametrize("skew", [45.0, -45.0])
def test_calibration_keeps_time_with_the_frames(make_rig: Callable[..., Rig], skew: float) -> None:
    camera_type = type("Skewed", (SkewedCamera,), {"skew": skew})
    rig = make_rig(camera_type=camera_type).started()
    engine = rig.runtime._engine
    assert engine is not None
    engine._calibration.timeout_s = 1.5  # the real 30 s is too long to wait for here
    mark = len(rig.writer.snapshot())
    assert rig.command("calibrate", {"action": "start"}) == {"ok": True}
    rig.writer.wait_for("calibration", step="top_left", after=mark)
    time.sleep(0.6)
    # Started on another clock than the frames', it is either cancelled at once or never times out.
    assert [e["step"] for e in rig.writer.snapshot()[mark:] if e["type"] == "calibration"] == ["top_left"]
    rig.writer.wait_for("calibration", step="cancelled", after=mark, timeout=3.0)


def test_calibrate_sent_before_the_first_frame_starts_on_the_frame_clock(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig(camera_type=type("Skewed", (SkewedCamera,), {"skew": 45.0}))
    assert rig.command("calibrate", {"action": "start"}) == {"ok": True}
    rig.start()
    rig.writer.wait_for("calibration", step="top_left")
    rig.writer.wait_for("state", state="calibrating")
    time.sleep(0.6)
    assert [e["step"] for e in rig.writer.of("calibration")] == ["top_left"]
    assert rig.command("status")["state"] == "calibrating"


def test_the_fake_camera_stamps_frames_on_the_helpers_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    # Off by a minute from time.monotonic, as perf_counter is from GetTickCount64 on Windows.
    monkeypatch.setattr(clock, "now", lambda: time.perf_counter() + 60.0)
    camera = FakeCamera(fps=60.0)
    camera.open()
    try:
        frame = camera.read(1.0)
        assert frame is not None
        assert 0.0 <= clock.now() - frame.t < 0.5
    finally:
        camera.close()


class DimRoomCamera(FakeCamera):
    """Says the requested rate when it opens, as the real one does, and the rate it measures later."""

    measured: float | None = None

    @property
    def info(self) -> CameraInfo | None:
        info = self._info
        if info is None or self.measured is None:
            return info
        return dataclasses.replace(info, fps=self.measured)


def test_the_measured_camera_rate_reaches_the_executor_and_ready(
    make_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime_module, "DISPLAY_REFRESH_S", 0.1)
    rig = make_rig(camera_type=DimRoomCamera).started()
    camera, executor = rig.cameras[0], rig.runtime._executor
    assert isinstance(camera, DimRoomCamera) and executor is not None
    assert executor.frame_interval == pytest.approx(1 / 30)
    mark = len(rig.writer.snapshot())
    camera.measured = 15.0  # a dim room: the exposure halves the rate
    ready = rig.writer.wait_for("ready", after=mark, timeout=3.0)
    assert ready["fps"] == 15.0 and ready["camera"] == "fake camera"
    # The executor glides each move over one real frame interval, not the requested one.
    assert executor.frame_interval == pytest.approx(1 / 15)
    camera.measured = 14.5  # jitter: followed, but not announced again
    eventually(lambda: executor.frame_interval == pytest.approx(1 / 14.5), what="the executor following the rate")
    time.sleep(0.3)
    assert len(rig.writer.of("ready")) == 2


def test_a_crash_in_the_loop_is_a_fatal_internal_error(make_rig: Callable[..., Rig]) -> None:
    class Exploding(FakeTracker):
        explode = False

        def process(self, image: np.ndarray, t: float) -> Any:
            if self.explode:
                raise RuntimeError("boom")
            return super().process(image, t)

    rig = make_rig(tracker=Exploding)
    rig.started().engaged().pressed()
    rig.tracker.explode = True
    rig.runtime.wait()
    error = rig.writer.wait_for("error")
    assert (error["code"], error["fatal"]) == ("internal", True) and "boom" in error["message"]
    assert not rig.desktop.buttons_down


def test_an_overlay_that_fails_to_start_is_reported_once_and_hands_carry_on(make_rig: Callable[..., Rig]) -> None:
    attempts: list[int] = []

    class Broken(NullOverlay):
        def start(self) -> None:
            attempts.append(1)
            raise OverlayError("no layered windows today")

    rig = make_rig(overlay=Broken).started().engaged()
    errors = rig.writer.of("error")
    assert [(e["code"], e["fatal"]) for e in errors] == [("overlay_failed", False)]
    assert "no layered windows today" in errors[0]["message"]
    assert attempts == [1]  # not retried every frame
    assert rig.runtime.exit_code is None


def test_the_overlay_follows_the_hand(make_rig: Callable[..., Rig]) -> None:
    overlay = RecordingOverlay()
    rig = make_rig(overlay=overlay).started()
    assert overlay.started
    eventually(lambda: overlay.mode == "hidden", what="a hidden reticle with no hand")
    rig.engaged()
    assert "engaging" in overlay.modes()
    eventually(lambda: overlay.mode == "point", what="the pointing reticle")
    rig.pressed()
    eventually(lambda: overlay.mode == "press", what="the pressed reticle")
    rig.puppet.set({"pose": "palm"})
    rig.command("config", {"overlay": False})
    eventually(lambda: overlay.mode == "hidden", what="the reticle hidden by config")
    time.sleep(0.2)
    assert overlay.mode == "hidden"
    rig.runtime.stop()
    assert overlay.closed


def test_a_grab_asks_the_tracker_for_two_hands(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig().started().engaged()
    assert rig.tracker.num_hands == 1
    rig.puppet.set({"pose": "fist"})
    rig.writer.wait_for("gesture", name="grab")
    eventually(lambda: rig.tracker.num_hands == 2, what="two hands while grabbing")
    rig.puppet.set({"pose": "palm"})
    rig.writer.wait_for("gesture", name="release")
    eventually(lambda: rig.tracker.num_hands == 1, what="one hand again")
    assert rig.tracker.num_hands_history == [2, 1]


def test_the_real_mouse_takes_over(make_rig: Callable[..., Rig]) -> None:
    # Off the screen centre (where the cursor starts): the executor only owns a cursor it has moved.
    rig = make_rig().started().engaged((0.35, 0.35))
    eventually(lambda: rig.desktop.calls_named("move_cursor"), what="hand control moving the cursor")
    time.sleep(0.2)
    mark = len(rig.writer.snapshot())
    rig.desktop.move_mouse(50, 50)
    rig.writer.wait_for("gesture", name="user_input", after=mark)
    rig.writer.wait_for("state", state="idle", after=mark)
    assert rig.command("status")["engaged"] is False


def test_a_window_that_refuses_to_move_is_reported_once(make_rig: Callable[..., Rig]) -> None:
    admin = FakeWindow(7, "Task Manager", Rect(0, 0, 1920, 1040), blocked=True)
    rig = make_rig(desktop=FakeDesktop(windows=[admin])).started().engaged(camera_point(0.5, 0.5))
    for x in (0.5, 0.6, 0.4, 0.55):
        rig.puppet.set({"pose": "fist", "at": list(camera_point(x, 0.5))})
        time.sleep(0.25)
    error = rig.writer.wait_for("error")
    assert (error["code"], error["fatal"]) == ("input_blocked", False) and "Task Manager" in error["message"]
    assert len(rig.writer.of("error")) == 1
    assert admin.rect == Rect(0, 0, 1920, 1040)


class LockableDesktop(FakeDesktop):
    def __init__(self) -> None:
        super().__init__()
        self.locked = False
        self.awake: list[tuple[bool, str]] = []

    def input_desktop_ok(self) -> bool:
        return not self.locked

    def keep_awake(self, on: bool) -> bool:
        self.awake.append((on, threading.current_thread().name))
        return True


def test_a_locked_desktop_lets_go_until_it_is_back(make_rig: Callable[..., Rig]) -> None:
    desktop = LockableDesktop()
    rig = make_rig(desktop=desktop).started().engaged()
    eventually(lambda: desktop.awake == [(True, "hands-loop")], what="keep_awake on the loop thread")
    rig.pressed()
    mark = len(rig.writer.snapshot())
    desktop.locked = True
    rig.writer.wait_for("gesture", name="disengage", after=mark)
    eventually(lambda: not desktop.buttons_down, what="the button released on the lock screen")
    eventually(lambda: desktop.awake[-1] == (False, "hands-loop"), what="keep_awake off")
    rig.puppet.set({"pose": "palm"})
    time.sleep(1.0)
    assert rig.writer.gestures().count("engage") == 1  # nothing happens while locked
    desktop.locked = False
    rig.writer.wait_for("gesture", name="engage", after=mark)


def test_pausing_while_engaged_lets_the_display_sleep(make_rig: Callable[..., Rig]) -> None:
    desktop = LockableDesktop()
    rig = make_rig(desktop=desktop).started().engaged()
    eventually(lambda: desktop.awake == [(True, "hands-loop")], what="keep_awake on the loop thread")
    assert rig.command("pause") == {"ok": True}
    # A pause can last all night (the projector): it must not keep the display on or the PC awake.
    eventually(lambda: desktop.awake[-1] == (False, "hands-loop"), timeout=2.0, what="keep_awake off while paused")
    mark = len(rig.writer.snapshot())
    assert rig.command("resume") == {"ok": True}
    rig.writer.wait_for("gesture", name="engage", after=mark)  # the palm is still up
    eventually(lambda: desktop.awake[-1] == (True, "hands-loop"), what="keep_awake on again once engaged")


class ReconfigurableDesktop(FakeDesktop):
    def __init__(self) -> None:
        super().__init__()
        self.layout = super().displays()

    def displays(self) -> list[Display]:
        return list(self.layout)


def test_a_new_display_layout_is_picked_up(make_rig: Callable[..., Rig]) -> None:
    desktop = ReconfigurableDesktop()
    rig = make_rig(desktop=desktop).started()
    mark = len(rig.writer.snapshot())
    desktop.layout = [display(1, 0, 0, 1920, 1080, primary=True), display(2, 1920, 0, 1920, 1080)]
    eventually(lambda: len(rig.command("status")["displays"]) == 2, timeout=5.0, what="the projector")
    ready = rig.writer.wait_for("ready", after=mark)  # the mod names displays from the last ready
    assert [(d["id"], d["used"]) for d in ready["displays"]] == [(1, True), (2, True)]


# --------------------------------------------------------------------------- fake mode


def test_fake_mode_replays_the_fake_script(tmp_path: Path) -> None:
    script = tmp_path / "frames.jsonl"
    script.write_text('{"t": 0, "hands": [{"pose": "palm", "at": [0.5, 0.45], "handedness": "right"}]}\n')
    writer = CapturingWriter()
    runtime = HandsRuntime(RuntimeOptions(data_dir=tmp_path, fake=True, fake_script=script), writer)
    try:
        runtime.start()
        writer.wait_for("gesture", name="engage")
        status = runtime.handle_command("status", {})
        assert status["camera"] == "fake camera" and status["engaged"] is True
    finally:
        runtime.stop()
    for event in writer.snapshot():
        validate(event, "Event")


def test_fake_mode_with_a_broken_script_is_fatal(tmp_path: Path) -> None:
    script = tmp_path / "frames.jsonl"
    script.write_text('{"t": 0, "hands": [{"pose": "moonwalk"}]}\n')
    writer = CapturingWriter()
    runtime = HandsRuntime(RuntimeOptions(data_dir=tmp_path, fake=True, fake_script=script), writer)
    runtime.start()
    runtime.wait()
    runtime.stop()
    error = writer.wait_for("error")
    assert (error["code"], error["fatal"]) == ("tracker_failed", True)
    assert "frames.jsonl:1" in error["message"] and "moonwalk" in error["message"]


def test_camera_errors_carry_the_protocol_code() -> None:
    camera = FakeCamera(fail_open="camera_blocked")
    with pytest.raises(CameraError) as info:
        camera.open()
    assert info.value.code == "camera_blocked"
