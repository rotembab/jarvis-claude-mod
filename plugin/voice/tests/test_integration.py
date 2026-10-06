"""Spawn ``python -m jarvis_voice run`` in fake mode and drive it exactly like the mod does."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from jarvis_voice import protocol

from fish_fake import FakeFishServer

TOKEN = "integration-token-" + uuid.uuid4().hex


class Helper:
    def __init__(
        self,
        data_dir: Path,
        fish_url: str,
        *,
        extra_args: list[str] | None = None,
        env: dict[str, str] | None = None,
        instance: str | None = None,
    ) -> None:
        self.instance = instance or f"JarvisVoiceIT-{uuid.uuid4().hex[:8]}"
        full_env = dict(os.environ)
        full_env.pop("FISH_AUDIO_API_KEY", None)
        full_env.update(
            PYTHONUNBUFFERED="1",
            PYTHONUTF8="1",
            JARVIS_TOKEN=TOKEN,
            JARVIS_PARENT="claude-code",
            JARVIS_INSTANCE_NAME=self.instance,
            NO_PROXY="127.0.0.1,localhost",
        )
        full_env.update(env or {})
        argv = [
            sys.executable,
            "-m",
            "jarvis_voice",
            "run",
            "--data-dir",
            str(data_dir),
            "--fake-audio",
            "--fake-stt",
            "--fake-fish",
            fish_url,
            *(extra_args or []),
        ]
        self.proc = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=full_env
        )
        self.lines: queue.Queue[dict[str, Any]] = queue.Queue()
        self.events: list[dict[str, Any]] = []
        self.raw: list[bytes] = []
        self.stderr: list[bytes] = []
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=lambda: self.stderr.extend(self.proc.stderr), daemon=True).start()  # type: ignore[arg-type]
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never via a proxy
        self.port: int | None = None

    def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.raw.append(line)
            event = json.loads(line)
            self.events.append(event)
            self.lines.put(event)

    def next_event(self, timeout: float = 10.0, *, skip_levels: bool = True) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            try:
                event = self.lines.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                raise AssertionError(
                    f"no event in time; stderr:\n{b''.join(self.stderr).decode(errors='replace')[-3000:]}"
                ) from None
            if skip_levels and event["type"] == "level":
                continue
            return event

    def wait_for(self, kind: str, timeout: float = 10.0, **fields: Any) -> list[dict[str, Any]]:
        """Collect non-level events until one of ``kind`` (with ``fields``) arrives; return them all."""
        seen: list[dict[str, Any]] = []
        deadline = time.monotonic() + timeout
        while True:
            event = self.next_event(max(0.01, deadline - time.monotonic()))
            seen.append(event)
            if event["type"] == kind and all(event.get(k) == v for k, v in fields.items()):
                return seen

    def post(
        self,
        name: str,
        body: dict[str, Any] | None = None,
        *,
        token: str = TOKEN,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/{name}",
            data=json.dumps(body or {}).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", **(headers or {})},
            method="POST",
        )
        try:
            with self.opener.open(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(10)


@pytest.fixture
def fish() -> Iterator[FakeFishServer]:
    with FakeFishServer(api_key="fake-key") as server:  # --fake-fish supplies "fake-key"
        yield server


@pytest.fixture
def helper(tmp_path: Path, fish: FakeFishServer) -> Iterator[Helper]:
    h = Helper(tmp_path, fish.url)
    hello = h.next_event(30)
    assert hello["type"] == "hello", hello  # hello is the very first stdout line
    h.port = hello["port"]
    yield h
    h.stop()


def test_full_session_like_the_mod(helper: Helper, fish: FakeFishServer, tmp_path: Path) -> None:
    hello = helper.events[0]
    assert hello["pid"] == helper.proc.pid or hello["pid"] > 0
    assert hello["platform"] in ("windows", "macos", "linux") and hello["version"]
    assert "fake.audio" in hello["capabilities"]

    seen = helper.wait_for("ready")
    assert [e["type"] for e in seen] == ["state", "state", "ready"]
    assert [e["state"] for e in seen[:2]] == ["starting", "sleeping"]

    assert helper.post("heartbeat") == (200, {"ok": True})
    status = helper.post("status")[1]
    assert status["state"] == "sleeping" and status["fishKeySet"] is True
    protocol.load_schema().validate(status, "StatusResponse")

    # Security: wrong token, Origin header.
    assert helper.post("heartbeat", token="nope")[0] == 401
    assert helper.post("heartbeat", headers={"Origin": "https://evil.example"})[0] == 403
    assert helper.post("speak", {"replyId": "r"})[0] == 400

    # Command-driven listening -> utterance.
    assert helper.post("listen", {"action": "start"}) == (200, {"ok": True})
    time.sleep(0.4)
    assert helper.post("listen", {"action": "stop"}) == (200, {"ok": True})
    seen = helper.wait_for("utterance")
    assert [e.get("state") for e in seen if e["type"] == "state"] == ["listening", "transcribing"]
    utterance = seen[-1]
    assert utterance["text"] == "Hello Jarvis, this is a test." and utterance["source"] == "command"
    assert helper.next_event()["state"] == "sleeping"

    # A streamed reply, posted out of order like concurrent fetches can be.
    helper.post("speak", {"replyId": "reply-1", "seq": 1, "text": "Second sentence.", "final": False})
    helper.post("speak", {"replyId": "reply-1", "seq": 0, "text": "First sentence.", "final": False})
    helper.post("speak", {"replyId": "reply-1", "seq": 2, "text": "", "final": True})
    seen = helper.wait_for("speech_done", replyId="reply-1")
    assert [e["type"] for e in seen] == ["speech_started", "state", "speech_done"]
    assert seen[-1]["interrupted"] is False and seen[-1]["spokenText"] == "First sentence. Second sentence."
    assert helper.next_event()["state"] == "sleeping"
    assert fish.sessions[-1].texts() == ["First sentence. ", "Second sentence. "]

    # Stop mid-reply.
    helper.post("speak", {"replyId": "reply-2", "seq": 0, "text": "A long sentence " * 20, "final": False})
    helper.wait_for("speech_started", replyId="reply-2")
    time.sleep(0.3)
    assert helper.post("stop", {"reason": "user"}) == (200, {"ok": True, "stopped": True})
    done = helper.wait_for("speech_done", replyId="reply-2")[-1]
    assert done["interrupted"] is True and done["spokenText"]
    assert helper.post("speak", {"replyId": "reply-2", "seq": 1, "text": "late", "final": True})[1]["dropped"] is True

    # Clean shutdown.
    assert helper.post("shutdown") == (200, {"ok": True})
    assert helper.proc.wait(10) == 0

    # Every stdout line is one schema-valid JSON object; nothing else leaked onto stdout.
    for raw in helper.raw:
        assert raw.endswith(b"\n") and raw.isascii()
        protocol.validate_event(json.loads(raw))
    assert any(e["type"] == "level" for e in helper.events)
    assert (tmp_path / "logs" / "voice.log").read_text(encoding="utf-8").count("control server listening") == 1
    assert TOKEN not in (tmp_path / "logs" / "voice.log").read_text(encoding="utf-8")


def test_second_instance_reports_already_running(helper: Helper, fish: FakeFishServer, tmp_path: Path) -> None:
    second = Helper(tmp_path, fish.url, instance=helper.instance)
    try:
        event = second.next_event(30)
        assert event["type"] == "error" and event["code"] == "already_running" and event["fatal"] is True
        assert event["hint"] == "Close the other Claude Code window (or its Jarvis), then run /jarvis here."
        assert second.proc.wait(30) == 3
    finally:
        second.stop()


def test_exits_when_heartbeats_stop(tmp_path: Path, fish: FakeFishServer) -> None:
    h = Helper(tmp_path, fish.url, extra_args=["--heartbeat-timeout", "0.5", "--heartbeat-grace", "1.5"])
    try:
        hello = h.next_event(30)
        h.port = hello["port"]
        h.post("heartbeat")
        t0 = time.monotonic()
        assert h.proc.wait(15) == 0
        assert time.monotonic() - t0 < 10
    finally:
        h.stop()


def test_refuses_to_start_without_token(tmp_path: Path) -> None:
    env = dict(os.environ)
    env.pop("JARVIS_TOKEN", None)
    proc = subprocess.run(
        [sys.executable, "-m", "jarvis_voice", "run", "--data-dir", str(tmp_path)],
        capture_output=True,
        timeout=60,
        env=env,
        stdin=subprocess.DEVNULL,
    )
    assert proc.returncode == 2
    event = json.loads(proc.stdout.splitlines()[0])
    assert event["type"] == "error" and event["fatal"] is True and "JARVIS_TOKEN" in event["message"]
