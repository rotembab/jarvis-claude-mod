from __future__ import annotations

import hashlib
import http.client
import json
import logging
import logging.handlers
import os
import subprocess
import sys
import threading
import types
import uuid
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from jarvis_hands import __version__, cli, protocol
from jarvis_hands.lifecycle import acquire_instance_lock
from jarvis_hands.logs import NOISY_LOGGERS, mask_secret, secret_filter, setup_logging

PAYLOAD = b"not really a hand model " * 4000
PAYLOAD_SHA = hashlib.sha256(PAYLOAD).hexdigest()
TIMEOUT = 60


class FileServer:
    """Serves PAYLOAD at any path on 127.0.0.1, also when asked as an HTTP proxy (absolute URLs)."""

    def __init__(self) -> None:
        self.requests: list[str] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                owner.requests.append(self.path)
                self.send_response(200)
                self.send_header("Content-Length", str(len(PAYLOAD)))
                self.end_headers()
                self.wfile.write(PAYLOAD)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_port
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def file_server() -> Iterator[FileServer]:
    srv = FileServer()
    yield srv
    srv.close()


def helper_env(**overrides: str | None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("JARVIS_TOKEN", "JARVIS_HANDS_CAMERA")}
    env.update(NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost", PYTHONUTF8="1")
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def run_helper(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "jarvis_hands", *args],
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        env=env if env is not None else helper_env(),
    )


def run_script(script: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        env=env if env is not None else helper_env(),
    )


def json_lines(stdout: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stdout.splitlines()]


def assert_progress_shape(line: dict[str, Any]) -> None:
    """What plugin/hooks/setup.ts parseProgress() reads."""
    assert line["type"] == "progress"
    assert line["step"] in ("download", "verify", "done", "error")
    assert isinstance(line["message"], str) and line["message"]
    if "pct" in line:
        assert isinstance(line["pct"], (int, float)) and 0 <= line["pct"] <= 100


def schema_validator() -> Any:
    import jsonschema

    document = json.loads(protocol.schema_path().read_text(encoding="utf-8"))

    def validate(instance: Any, def_name: str) -> None:
        jsonschema.Draft202012Validator({"$ref": f"#/$defs/{def_name}", "$defs": document["$defs"]}).validate(instance)

    return validate


@pytest.fixture
def restore_logging() -> Iterator[None]:
    """setup_logging rewires the root logger; give pytest's back afterwards."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    noisy = {name: logging.getLogger(name).level for name in NOISY_LOGGERS}
    yield
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
            handler.close()
    for handler in handlers:
        if handler not in root.handlers:
            root.addHandler(handler)
    root.setLevel(level)
    for name, noisy_level in noisy.items():
        logging.getLogger(name).setLevel(noisy_level)


# --------------------------------------------------------------------------- parser


def test_run_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("JARVIS_HANDS_CAMERA", "JARVIS_HANDS_INSTANCE_NAME", "JARVIS_LOG_LEVEL"):
        monkeypatch.delenv(name, raising=False)
    args = cli.build_parser().parse_args(["run"])
    assert args.func is cli.cmd_run
    assert (args.data_dir, args.camera, args.width, args.height, args.fps) == (None, None, 1280, 720, 30)
    assert (args.no_overlay, args.fake, args.fake_script) == (False, False, None)
    assert args.instance_name == "JarvisHands"
    assert (args.heartbeat_timeout, args.heartbeat_grace, args.log_level) == (15.0, 60.0, "INFO")


def test_run_options(tmp_path: Path) -> None:
    args = cli.build_parser().parse_args(
        [
            "run",
            "--data-dir",
            str(tmp_path),
            "--camera",
            "UGREEN",
            "--width",
            "640",
            "--height",
            "480",
            "--fps",
            "60",
            "--no-overlay",
            "--fake",
            "--fake-script",
            str(tmp_path / "frames.jsonl"),
            "--instance-name",
            "JarvisHandsTest",
            "--heartbeat-timeout",
            "3",
            "--heartbeat-grace",
            "5",
            "--log-level",
            "debug",
        ]
    )
    assert args.data_dir == tmp_path and args.camera == "UGREEN"
    assert (args.width, args.height, args.fps) == (640, 480, 60)
    assert args.no_overlay and args.fake and args.fake_script == tmp_path / "frames.jsonl"
    assert args.instance_name == "JarvisHandsTest"
    assert (args.heartbeat_timeout, args.heartbeat_grace, args.log_level) == (3.0, 5.0, "debug")


def test_run_reads_its_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_HANDS_CAMERA", "1")
    monkeypatch.setenv("JARVIS_HANDS_INSTANCE_NAME", "JarvisHandsCI")
    args = cli.build_parser().parse_args(["run"])
    assert (args.camera, args.instance_name) == ("1", "JarvisHandsCI")
    assert cli.build_parser().parse_args(["run", "--camera", "0"]).camera == "0"


def test_other_subcommands_parse(tmp_path: Path) -> None:
    parser = cli.build_parser()
    setup = parser.parse_args(
        ["setup", "--data-dir", str(tmp_path), "--model-url", "http://x/m", "--model-sha256", "ab"]
    )
    assert setup.func is cli.cmd_setup and (setup.model_url, setup.model_sha256) == ("http://x/m", "ab")
    assert parser.parse_args(["setup"]).model_url is None
    doctor = parser.parse_args(["doctor", "--no-camera"])
    assert doctor.func is cli.cmd_doctor and doctor.no_camera
    assert parser.parse_args(["doctor"]).no_camera is False
    preview = parser.parse_args(["preview", "--camera", "2", "--data-dir", str(tmp_path)])
    assert preview.func is cli.cmd_preview and preview.camera == "2" and preview.data_dir == tmp_path


@pytest.mark.parametrize("argv", [[], ["fly"], ["run", "--width", "wide"], ["doctor", "--camera"]])
def test_bad_command_lines_exit_2(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        cli.build_parser().parse_args(argv)
    assert info.value.code == 2


def test_capabilities() -> None:
    caps = ["heartbeat", "status", "config", "pause", "resume", "engage", "disengage", "calibrate", "shutdown"]
    assert cli.capabilities(False) == caps
    assert cli.capabilities(True) == [*caps, "fake"]
    assert set(caps) == protocol.COMMAND_NAMES


def test_default_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    assert cli.default_data_dir() == tmp_path
    monkeypatch.delenv("JARVIS_DATA_DIR")
    assert cli.default_data_dir() == Path.home() / ".jarvis"


@pytest.mark.parametrize("args", [["--help"], ["run", "--help"], ["setup", "--help"], ["doctor", "--help"]])
def test_help(args: list[str]) -> None:
    proc = run_helper(*args)
    assert proc.returncode == 0 and "usage" in proc.stdout
    assert "--model-url" not in proc.stdout  # test-only switches stay out of the help


def test_version() -> None:
    proc = run_helper("--version")
    assert proc.returncode == 0 and proc.stdout.strip() == f"jarvis-hands {__version__}"


# --------------------------------------------------------------------------- setup


def test_setup_downloads_the_model(file_server: FileServer, tmp_path: Path) -> None:
    proc = run_helper(
        "setup",
        "--data-dir",
        str(tmp_path),
        "--model-url",
        f"http://127.0.0.1:{file_server.port}/hand_landmarker.task",
        "--model-sha256",
        PAYLOAD_SHA,
    )
    assert proc.returncode == 0, proc.stderr
    lines = json_lines(proc.stdout)
    for line in lines:
        assert_progress_shape(line)
        assert line["v"] == 1
    steps = [line["step"] for line in lines]
    assert steps[0] == "download" and "verify" in steps and steps[-1] == "done"
    assert lines[-1]["pct"] == 100
    assert (tmp_path / "models" / "hands" / "hand_landmarker.task").read_bytes() == PAYLOAD
    assert (tmp_path / "logs" / "hands.log").is_file()

    again = run_helper(
        "setup", "--data-dir", str(tmp_path), "--model-url", "http://127.0.0.1:9/never", "--model-sha256", PAYLOAD_SHA
    )
    assert again.returncode == 0, again.stderr
    assert [line["step"] for line in json_lines(again.stdout)] == ["verify", "done"]
    assert len(file_server.requests) == 1


def test_setup_checksum_mismatch_fails(file_server: FileServer, tmp_path: Path) -> None:
    proc = run_helper(
        "setup",
        "--data-dir",
        str(tmp_path),
        "--model-url",
        f"http://127.0.0.1:{file_server.port}/hand_landmarker.task",
        "--model-sha256",
        "0" * 64,
    )
    assert proc.returncode == 1
    lines = json_lines(proc.stdout)
    for line in lines:
        assert_progress_shape(line)
    assert lines[-1]["step"] == "error" and "checksum" in lines[-1]["message"]
    assert not (tmp_path / "models" / "hands" / "hand_landmarker.task").exists()
    assert not (tmp_path / "models" / "hands" / "hand_landmarker.task.part").exists()


def test_setup_unreachable_server_fails(tmp_path: Path) -> None:
    proc = run_helper(
        "setup", "--data-dir", str(tmp_path), "--model-url", "http://127.0.0.1:9/m.task", "--model-sha256", PAYLOAD_SHA
    )
    assert proc.returncode == 1
    last = json_lines(proc.stdout)[-1]
    assert last["step"] == "error" and "could not reach" in last["message"]


def test_setup_goes_through_the_proxy_from_the_environment(file_server: FileServer, tmp_path: Path) -> None:
    proxy = f"http://127.0.0.1:{file_server.port}"
    env = helper_env(http_proxy=proxy, HTTP_PROXY=None, NO_PROXY=None, no_proxy=None)
    proc = run_helper(
        "setup",
        "--data-dir",
        str(tmp_path),
        "--model-url",
        "http://models.invalid/hand_landmarker.task",
        "--model-sha256",
        PAYLOAD_SHA,
        env=env,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert file_server.requests == ["http://models.invalid/hand_landmarker.task"]


# --------------------------------------------------------------------------- run (up to the runtime)


def test_run_refuses_to_start_without_a_token(tmp_path: Path) -> None:
    proc = run_helper("run", "--data-dir", str(tmp_path), "--instance-name", f"JarvisHandsTest-{uuid.uuid4().hex[:8]}")
    assert proc.returncode == 2
    lines = json_lines(proc.stdout)
    assert len(lines) == 1
    assert lines[0]["type"] == "error" and lines[0]["fatal"] is True and "JARVIS_TOKEN" in lines[0]["message"]
    schema_validator()(lines[0], "ErrorEvent")
    assert "JARVIS_TOKEN" in proc.stderr  # and logged


def test_run_exits_3_when_another_helper_holds_the_lock(tmp_path: Path) -> None:
    name = f"JarvisHandsTest-{uuid.uuid4().hex[:8]}"
    lock = acquire_instance_lock(name, tmp_path)
    assert lock is not None
    try:
        token = "t0ken-for-the-lock-test"
        proc = run_helper(
            "run", "--data-dir", str(tmp_path), "--instance-name", name, env=helper_env(JARVIS_TOKEN=token)
        )
    finally:
        lock.release()
    assert proc.returncode == 3
    lines = json_lines(proc.stdout)
    assert [line["code"] for line in lines] == ["already_running"]
    schema_validator()(lines[0], "Event")
    assert token not in proc.stderr
    assert token not in (tmp_path / "logs" / "hands.log").read_text(encoding="utf-8")


# A stand-in for jarvis_hands.runtime (another module's job), so the wiring of
# ``run`` is tested on its own: hello, the token, heartbeats, shutdown, signals.
STUB_RUNTIME = """
import json, os, sys, threading, time, types

mode = os.environ.get("STUB_RUNTIME_MODE", "ok")
stub = types.ModuleType("jarvis_hands.runtime")

# A handler thread that is descheduled while it writes: the control server's threads are daemon threads
# that nothing joins, so the answer must be written before anything asks the process to wind down.
delay = float(os.environ.get("STUB_SHUTDOWN_WRITE_DELAY_S", "0"))
if delay:
    from jarvis_hands import control

    write = control._Handler._send_encoded

    def slow_write(self, status, body, *, close=False):
        if self.path.startswith("/v1/shutdown"):
            time.sleep(delay)
        return write(self, status, body, close=close)

    control._Handler._send_encoded = slow_write


class RuntimeOptions:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class HandsRuntime:
    def __init__(self, options, events):
        if mode == "explode":
            raise RuntimeError("no camera backend")
        self.options, self.events = options, events
        self.stopped = threading.Event()
        self.commanded = threading.Event()
        self.started = False

    def start(self):
        if mode == "start_raises":
            raise RuntimeError("camera exploded")
        if mode == "slow_start":  # a camera that takes its time to open: done once a command came
            self.commanded.wait(10)
        self.started = True
        self.events.emit({"v": 1, "type": "state", "state": "idle"})

    def wait(self):
        while not self.stopped.wait(0.05):
            pass

    def request_stop(self):
        self.stopped.set()

    def stop(self):
        sys.stderr.write("STUB stop after start=" + str(self.started) + "\\n")
        sys.stderr.write("STUB stopped " + json.dumps(self.options.kwargs, default=str) + "\\n")
        sys.stderr.flush()

    def handle_command(self, name, body):
        sys.stderr.write("STUB command " + name + " started=" + str(self.started) + "\\n")
        sys.stderr.flush()
        self.commanded.set()
        if name == "config":
            return {"ok": False, "error": {"code": "bad_request", "message": "stub says no"}}
        return {"ok": True}


stub.HandsRuntime, stub.RuntimeOptions = HandsRuntime, RuntimeOptions
sys.modules["jarvis_hands.runtime"] = stub
from jarvis_hands.cli import main

sys.exit(main(sys.argv[1:]))
"""


def start_stub(tmp_path: Path, *args: str, env: dict[str, str]) -> subprocess.Popen[str]:
    argv = [sys.executable, "-c", STUB_RUNTIME, "run", "--data-dir", str(tmp_path)]
    argv += ["--instance-name", f"JarvisHandsTest-{uuid.uuid4().hex[:8]}", "--heartbeat-grace", "30", *args]
    return subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)


def read_hello(proc: subprocess.Popen[str]) -> dict[str, Any]:
    assert proc.stdout is not None
    line = proc.stdout.readline()
    if not line:
        _, err = proc.communicate(timeout=TIMEOUT)
        raise AssertionError(f"no hello; stderr:\n{err}")
    hello: dict[str, Any] = json.loads(line)
    return hello


def read_event(proc: subprocess.Popen[str]) -> dict[str, Any]:
    """The next event line. Tests read the runtime's first event before shutting down: a shutdown that
    arrives while cmd_run is still on its way to ``runtime.start()`` rightly means start never runs."""
    assert proc.stdout is not None
    line = proc.stdout.readline()
    if not line:
        _, err = proc.communicate(timeout=TIMEOUT)
        raise AssertionError(f"no event; stderr:\n{err}")
    event: dict[str, Any] = json.loads(line)
    return event


def command(port: int, name: str, body: Any = None, *, token: str | None) -> tuple[int, dict[str, Any]]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    conn.request("POST", f"/v1/{name}", body=json.dumps(body if body is not None else {}), headers=headers)
    resp = conn.getresponse()
    payload = json.loads(resp.read())
    conn.close()
    return resp.status, payload


def test_run_wiring_with_a_stand_in_runtime(tmp_path: Path) -> None:
    token = "run-wiring-token-0123456789"
    proc = start_stub(tmp_path, "--camera", "UGREEN", "--no-overlay", "--fps", "60", env=helper_env(JARVIS_TOKEN=token))
    try:
        hello = read_hello(proc)
        schema_validator()(hello, "HelloEvent")
        assert hello["capabilities"] == cli.capabilities(False)
        assert (hello["platform"], hello["version"]) == (cli.plat.name(), __version__)
        port = hello["port"]
        assert command(port, "heartbeat", token=None)[0] == 401
        assert command(port, "heartbeat", token="wrong")[0] == 401
        assert command(port, "heartbeat", token=token) == (200, {"ok": True})
        assert command(port, "engage", token=token) == (200, {"ok": True})
        status, payload = command(port, "config", {"engage": "always"}, token=token)
        assert status == 200 and payload["error"]["message"] == "stub says no"
        assert command(port, "config", {"engage": "never"}, token=token)[0] == 400  # validated before the runtime
        assert read_event(proc) == {"v": 1, "type": "state", "state": "idle"}
        assert command(port, "shutdown", token=token) == (200, {"ok": True})
        out, err = proc.communicate(timeout=TIMEOUT)
    finally:
        proc.kill()
    assert proc.returncode == 0, err
    assert json_lines(out) == []
    options = json.loads(err.split("STUB stopped ", 1)[1].splitlines()[0])
    assert options == {
        "data_dir": str(tmp_path),
        "camera": "UGREEN",
        "width": 1280,
        "height": 720,
        "fps": 60,
        "overlay": False,
        "fake": False,
        "fake_script": None,
    }
    assert token not in err


def test_run_forwards_commands_while_the_runtime_is_still_starting(tmp_path: Path) -> None:
    # The mod sends config the moment it reads hello, while the camera may still be opening.
    token = "slow-start-token-0123456789"
    proc = start_stub(tmp_path, env=helper_env(JARVIS_TOKEN=token, STUB_RUNTIME_MODE="slow_start"))
    try:
        port = read_hello(proc)["port"]
        status, payload = command(port, "config", {"engage": "always"}, token=token)
        assert status == 200 and payload["error"]["message"] == "stub says no"  # the runtime's own answer
        assert read_event(proc) == {"v": 1, "type": "state", "state": "idle"}
        assert command(port, "shutdown", token=token) == (200, {"ok": True})
        out, err = proc.communicate(timeout=TIMEOUT)
    finally:
        proc.kill()
    assert proc.returncode == 0, err
    assert "STUB command config started=False" in err
    assert json_lines(out) == []
    assert "STUB stop after start=True" in err


def test_run_stops_a_runtime_whose_start_failed(tmp_path: Path) -> None:
    # stop() is the runtime's chance to release the mouse and the camera, so it runs even then.
    proc = start_stub(tmp_path, env=helper_env(JARVIS_TOKEN="start-fails-token-0123", STUB_RUNTIME_MODE="start_raises"))
    try:
        out, err = proc.communicate(timeout=TIMEOUT)
    finally:
        proc.kill()
    assert proc.returncode == 1, err
    lines = json_lines(out)
    assert [line["type"] for line in lines] == ["hello", "error"]
    assert (lines[1]["code"], lines[1]["fatal"]) == ("internal", True) and "camera exploded" in lines[1]["message"]
    schema_validator()(lines[1], "ErrorEvent")
    assert "STUB stop after start=False" in err


def test_run_fake_mode_uses_the_test_token(tmp_path: Path) -> None:
    proc = start_stub(tmp_path, "--fake", env=helper_env())
    try:
        hello = read_hello(proc)
        assert hello["capabilities"][-1] == "fake"
        assert command(hello["port"], "shutdown", token=cli.FAKE_TOKEN) == (200, {"ok": True})
        _, err = proc.communicate(timeout=TIMEOUT)
    finally:
        proc.kill()
    assert proc.returncode == 0, err


def test_run_stops_when_heartbeats_never_come(tmp_path: Path) -> None:
    proc = start_stub(tmp_path, "--heartbeat-grace", "0.5", env=helper_env(JARVIS_TOKEN="watchdog-token-0123"))
    try:
        out, err = proc.communicate(timeout=TIMEOUT)
    finally:
        proc.kill()
    assert proc.returncode == 0, err
    assert [line["type"] for line in json_lines(out)] == ["hello", "state"]
    assert "no heartbeat" in err and "STUB stopped" in err


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_run_stops_on_sigterm(tmp_path: Path) -> None:
    import signal

    proc = start_stub(tmp_path, env=helper_env(JARVIS_TOKEN="signal-token-0123456"))
    try:
        read_hello(proc)
        proc.send_signal(signal.SIGTERM)
        _, err = proc.communicate(timeout=TIMEOUT)
    finally:
        proc.kill()
    assert proc.returncode == 0, err
    assert "STUB stopped" in err


def test_run_reports_a_runtime_that_cannot_be_built(tmp_path: Path) -> None:
    proc = start_stub(tmp_path, env=helper_env(JARVIS_TOKEN="explode-token-0123456", STUB_RUNTIME_MODE="explode"))
    try:
        out, err = proc.communicate(timeout=TIMEOUT)
    finally:
        proc.kill()
    assert proc.returncode == 1, err
    lines = json_lines(out)
    assert [(line["type"], line["code"], line["fatal"]) for line in lines] == [("error", "internal", True)]
    assert "no camera backend" in lines[0]["message"]
    schema_validator()(lines[0], "ErrorEvent")


def test_native_writes_to_stdout_cannot_reach_the_protocol_stream() -> None:
    """Native libraries write to file descriptor 1 directly; only protocol lines may come out of stdout."""
    script = (
        "import os, sys\n"
        "from jarvis_hands.cli import _claim_stdout\n"
        "out = _claim_stdout()\n"
        "os.write(1, b'native noise without newline')\n"  # e.g. MediaPipe's glog or a TFLite delegate
        "print('stray print')\n"
        'out.write(b\'{"type":"hello"}\\n\'); out.flush()\n'
    )
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=TIMEOUT)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == b'{"type":"hello"}\n'
    assert b"native noise without newline" in proc.stderr and b"stray print" in proc.stderr


# --------------------------------------------------------------------------- doctor / preview delegation


def test_doctor_delegates_and_prints_json(tmp_path: Path) -> None:
    script = (
        "import sys, types\n"
        "fake = types.ModuleType('jarvis_hands.doctor')\n"
        "fake.run_doctor = lambda data_dir, camera: {'ok': True, 'dataDir': str(data_dir), 'camera': camera}\n"
        "sys.modules['jarvis_hands.doctor'] = fake\n"
        "from jarvis_hands.cli import main\n"
        f"sys.exit(main(['doctor', '--data-dir', {str(tmp_path)!r}, '--no-camera']))\n"
    )
    proc = run_script(script)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"ok": True, "dataDir": str(tmp_path), "camera": False}


def test_doctor_reports_a_missing_doctor_module(tmp_path: Path) -> None:
    script = (
        "import sys\n"
        "sys.modules['jarvis_hands.doctor'] = None\n"  # makes the import fail
        "from jarvis_hands.cli import main\n"
        f"sys.exit(main(['doctor', '--data-dir', {str(tmp_path)!r}]))\n"
    )
    proc = run_script(script)
    assert proc.returncode == 1
    report = json.loads(proc.stdout)
    assert report["ok"] is False and "jarvis_hands.doctor" in report["error"]


def test_preview_delegates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restore_logging: None) -> None:
    seen: list[Any] = []
    fake = types.ModuleType("jarvis_hands.preview")
    fake.run_preview = lambda args: seen.append(args) or 0  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "jarvis_hands.preview", fake)
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    assert cli.main(["preview", "--camera", "UGREEN"]) == 0
    assert seen[0].camera == "UGREEN" and seen[0].data_dir == tmp_path


def test_preview_missing_module(monkeypatch: pytest.MonkeyPatch, restore_logging: None) -> None:
    monkeypatch.setitem(sys.modules, "jarvis_hands.preview", None)
    assert cli.main(["preview"]) == 1


# --------------------------------------------------------------------------- logs


def test_setup_logging_writes_hands_log(tmp_path: Path, restore_logging: None) -> None:
    path = setup_logging(tmp_path, "debug")
    assert path == tmp_path / "logs" / "hands.log"
    root = logging.getLogger()
    assert root.level == logging.DEBUG
    rotating = [h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert len(rotating) == 1 and rotating[0].maxBytes == 1_000_000 and rotating[0].backupCount == 3
    for name in NOISY_LOGGERS:
        assert logging.getLogger(name).level == logging.WARNING
    secret = "hands-token-0123456789abcdef"
    secret_filter.add(secret)
    logging.getLogger("jarvis_hands.test").info("token is %s", secret)
    for handler in root.handlers:
        handler.flush()
    text = path.read_text(encoding="utf-8")
    assert "token is ****cdef" in text and secret not in text


def test_setup_logging_without_a_data_dir_or_with_a_bad_level(tmp_path: Path, restore_logging: None) -> None:
    assert setup_logging(None, "INFO") is None
    assert setup_logging(tmp_path, "LOUD") == tmp_path / "logs" / "hands.log"
    assert logging.getLogger().level == logging.INFO


def test_setup_logging_survives_an_unwritable_data_dir(tmp_path: Path, restore_logging: None) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("not a folder")
    assert setup_logging(blocker, "INFO") is None


def test_mask_secret() -> None:
    assert mask_secret(None) is None and mask_secret("") is None
    assert mask_secret("short") == "****"
    assert mask_secret("abcdefghijklmnop") == "****mnop"


def test_the_shutdown_answer_arrives_even_when_its_write_is_slow(tmp_path: Path) -> None:
    """The mod waits for this answer before it starts the next helper, so it must not be cut off."""
    token = "slow-shutdown-token-0123456789"
    proc = start_stub(
        tmp_path,
        env=helper_env(JARVIS_TOKEN=token, STUB_SHUTDOWN_WRITE_DELAY_S="0.3"),
    )
    try:
        port = read_hello(proc)["port"]
        assert read_event(proc) == {"v": 1, "type": "state", "state": "idle"}
        assert command(port, "shutdown", token=token) == (200, {"ok": True})
        out, err = proc.communicate(timeout=TIMEOUT)
    finally:
        proc.kill()
    assert proc.returncode == 0, err
    assert json_lines(out) == []
