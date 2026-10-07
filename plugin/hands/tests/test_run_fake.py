"""``python -m jarvis_hands run --fake`` as the mod runs it: a real process, ``hello`` on stdout, commands over HTTP.

The scripted hands come from ``--fake-script``; the camera, tracker and desktop
are the fakes, so this runs on any OS with no hardware and no model.
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from test_cli import TIMEOUT, command, helper_env, schema_validator

SCRIPT = '{"t": 0, "hands": []}\n{"t": 0.3, "hands": [{"pose": "palm", "at": [0.5, 0.45], "handedness": "right"}]}\n'


class Lines:
    """Reads a process's stdout on a thread, so a test can wait for a line with a timeout."""

    def __init__(self, proc: subprocess.Popen[str]) -> None:
        self.seen: list[dict[str, Any]] = []
        self._queue: queue.Queue[str | None] = queue.Queue()
        assert proc.stdout is not None
        stdout = proc.stdout
        threading.Thread(target=self._pump, args=(stdout,), daemon=True).start()

    def _pump(self, stream: Any) -> None:
        for line in stream:
            self._queue.put(line)
        self._queue.put(None)

    def next(self, timeout: float = TIMEOUT) -> dict[str, Any] | None:
        line = self._queue.get(timeout=timeout)
        if line is None:
            return None
        event: dict[str, Any] = json.loads(line)
        self.seen.append(event)
        return event

    def until(self, predicate: Any, timeout: float = TIMEOUT) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        for event in self.seen:
            if predicate(event):
                return event
        while True:
            event = self.next(max(0.01, deadline - time.monotonic()))
            if event is None:
                raise AssertionError(f"stdout closed; saw {self.seen}")
            if predicate(event):
                return event

    def rest(self) -> None:
        while self.next() is not None:
            pass


def test_run_fake_answers_commands_and_replays_the_script(tmp_path: Path) -> None:
    script = tmp_path / "frames.jsonl"
    script.write_text(SCRIPT, encoding="utf-8")
    token = "run-fake-token-0123456789abcdef"
    argv = [sys.executable, "-m", "jarvis_hands", "run", "--fake", "--data-dir", str(tmp_path)]
    argv += ["--fake-script", str(script), "--instance-name", f"JarvisHandsTest-{uuid.uuid4().hex[:8]}"]
    proc = subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=helper_env(JARVIS_TOKEN=token)
    )
    errors: list[str] = []
    assert proc.stderr is not None
    stderr = proc.stderr
    threading.Thread(target=lambda: errors.extend(stderr), daemon=True).start()
    validate = schema_validator()
    try:
        lines = Lines(proc)
        hello = lines.next()
        assert hello is not None and hello["type"] == "hello", errors
        validate(hello, "HelloEvent")
        assert hello["capabilities"][-1] == "fake"
        port = hello["port"]

        status, payload = command(port, "status", token=token)
        assert status == 200
        validate(payload, "StatusResponse")
        assert command(port, "config", {"scrollSpeed": 2}, token=token) == (200, {"ok": True})
        assert command(port, "status", token="wrong")[0] == 401

        ready = lines.until(lambda e: e["type"] == "ready")
        assert ready["camera"] == "fake camera" and ready["displays"][0]["used"] is True
        lines.until(lambda e: e == {"v": 1, "type": "gesture", "name": "engage"})
        lines.until(lambda e: e == {"v": 1, "type": "state", "state": "active"})

        status, payload = command(port, "status", token=token)
        validate(payload, "StatusResponse")
        assert (payload["state"], payload["engaged"], payload["settings"]["scrollSpeed"]) == ("active", True, 2)
        assert payload["fps"] > 0
        assert command(port, "heartbeat", token=token) == (200, {"ok": True})
        assert command(port, "shutdown", token=token) == (200, {"ok": True})
        proc.wait(timeout=TIMEOUT)
        lines.rest()
    finally:
        proc.kill()
        proc.wait(timeout=TIMEOUT)
    log = "".join(errors)
    assert proc.returncode == 0, log
    for event in lines.seen:
        validate(event, "Event")
    states = [e["state"] for e in lines.seen if e["type"] == "state"]
    assert states[:3] == ["starting", "idle", "active"]
    assert not [e for e in lines.seen if e["type"] == "error"]
    assert token not in log
    assert "hand control stopped" in log


def test_run_fake_with_a_broken_script_exits_with_a_fatal_error(tmp_path: Path) -> None:
    script = tmp_path / "frames.jsonl"
    script.write_text('{"t": "soon", "hands": []}\n', encoding="utf-8")
    argv = [sys.executable, "-m", "jarvis_hands", "run", "--fake", "--data-dir", str(tmp_path)]
    argv += ["--fake-script", str(script), "--instance-name", f"JarvisHandsTest-{uuid.uuid4().hex[:8]}"]
    proc = subprocess.run(
        argv, capture_output=True, text=True, timeout=TIMEOUT, env=helper_env(JARVIS_TOKEN="t0ken-123456")
    )
    assert proc.returncode == 1, proc.stderr
    events = [json.loads(line) for line in proc.stdout.splitlines()]
    validate = schema_validator()
    for event in events:
        validate(event, "Event")
    assert [e["type"] for e in events] == ["hello", "state", "error", "state"]
    assert (events[2]["code"], events[2]["fatal"]) == ("tracker_failed", True)
    assert events[3]["state"] == "error"
