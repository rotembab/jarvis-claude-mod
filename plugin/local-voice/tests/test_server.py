"""The local voice protocol, driven through a real ``serve --fake`` process (no model needed)."""

from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from jarvis_local_voice.server import MAX_TEXT, FakeBackend, Server, clean_text

SRC = Path(__file__).resolve().parents[1] / "src"


class Proc:
    def __init__(self, *extra: str) -> None:
        env = {**os.environ, "PYTHONPATH": str(SRC)}
        self.p = subprocess.Popen(
            [sys.executable, "-m", "jarvis_local_voice", "serve", "--fake", *extra],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        assert self.p.stdin is not None and self.p.stdout is not None
        self.stdin = self.p.stdin
        self.stdout = self.p.stdout

    def send(self, **request: Any) -> None:
        self.stdin.write((json.dumps(request) + "\n").encode())
        self.stdin.flush()

    def event(self) -> dict[str, Any]:
        line = self.stdout.readline()
        assert line, "the process closed stdout"
        return json.loads(line)

    def until(self, kind: str) -> list[dict[str, Any]]:
        seen = []
        while True:
            event = self.event()
            seen.append(event)
            if event["event"] == kind:
                return seen

    def close(self) -> int:
        self.stdin.close()
        return self.p.wait(timeout=10)


@pytest.fixture
def proc() -> Any:
    p = Proc()
    yield p
    if p.p.poll() is None:
        p.p.kill()
        p.p.wait()


def test_ready_then_audio_for_each_sentence_in_order(proc: Proc) -> None:
    ready = proc.event()
    assert ready["event"] == "ready" and ready["device"] == "fake" and ready["sampleRate"] == 24_000
    proc.send(op="say", stream=1, seq=0, text="Good evening, sir.")
    proc.send(op="say", stream=1, seq=1, text="All systems are online.")
    first = proc.until("said")
    second = proc.until("said")
    assert [e["seq"] for e in first] == [0] * len(first) and [e["seq"] for e in second] == [1] * len(second)
    pcm = b"".join(base64.b64decode(e["pcm"]) for e in first if e["event"] == "audio")
    assert len(pcm) == 2 * int(24_000 * 0.06 * 3)  # three words of fake speech, 16-bit
    assert proc.close() == 0


def test_cancel_drops_queued_sentences_of_that_stream_only() -> None:
    p = Proc("--fake-delay", "0.3")
    try:
        assert p.event()["event"] == "ready"
        p.send(op="say", stream=1, seq=0, text="One.")
        p.send(op="say", stream=1, seq=1, text="Two.")
        p.send(op="say", stream=2, seq=0, text="Three.")
        p.send(op="cancel", stream=1)
        events = p.until("said")
        # Stream 1's first sentence was already being synthesized: its audio is dropped too.
        assert {(e["stream"], e["seq"]) for e in events} == {(2, 0)}
        assert p.close() == 0
    finally:
        if p.p.poll() is None:
            p.p.kill()


def test_a_failed_sentence_reports_an_error_and_the_next_one_still_plays(proc: Proc) -> None:
    proc.event()
    proc.send(op="say", stream=4, seq=0, text="FAIL on purpose")
    proc.send(op="say", stream=4, seq=1, text="Still here.")
    error = proc.event()
    assert error == {"event": "error", "stream": 4, "seq": 0, "message": "RuntimeError: fake synthesis failure"}
    assert proc.until("said")[-1] == {"event": "said", "stream": 4, "seq": 1}


def test_bad_requests_are_reported_not_fatal(proc: Proc) -> None:
    proc.event()
    proc.stdin.write(b"not json\n")
    proc.send(op="sing", stream=1)
    proc.send(op="say", stream="x", seq=0, text="hi")
    for _ in range(3):
        event = proc.event()
        assert event["event"] == "error" and event["message"].startswith("bad request")
    proc.send(op="say", stream=1, seq=0, text="Fine.")
    assert proc.until("said")[-1]["event"] == "said"


def test_a_load_failure_is_fatal(tmp_path: Path) -> None:
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    # No --fake: the real backend. Without torch, or without the model in tmp_path, loading fails.
    p = subprocess.Popen(
        [sys.executable, "-m", "jarvis_local_voice", "serve", "--models-dir", str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    try:
        assert p.stdout is not None
        event = json.loads(p.stdout.readline())
        assert event["event"] == "fatal" and event["message"]
        assert p.wait(timeout=30) == 1
    finally:
        if p.poll() is None:
            p.kill()


def test_empty_text_is_said_without_audio() -> None:
    out = io.BytesIO()
    server = Server(out)
    server.loaded = True
    server.handle_line(b'{"op": "say", "stream": 1, "seq": 0, "text": "  \\"  "}\n')
    thread = threading.Thread(target=server.run, args=(FakeBackend(),))
    thread.start()
    server.read_requests(io.BytesIO(b""))
    thread.join(5)
    assert [json.loads(line) for line in out.getvalue().splitlines()] == [{"event": "said", "stream": 1, "seq": 0}]


def test_clean_text() -> None:
    assert clean_text('He said "fine"  then\nleft.') == "He said fine then left."
    long = "word " * 100
    cleaned = clean_text(long)
    assert len(cleaned) <= MAX_TEXT and not cleaned.endswith(" ") and cleaned.startswith("word word")
