"""Measure time to first audio for a reply streamed to the helper in three chunks.

Run with the helper's Python (``~/.jarvis/venv/Scripts/python.exe`` on Windows):

    python plugin/voice/scripts/voice_latency.py fake
    python plugin/voice/scripts/voice_latency.py live [--model s2.1-pro-free] [--voice-id ID]

``fake``: the repo's FakeFishServer and silent fake audio, so it measures only the
helper's own overhead; the real key is never sent anywhere.
``live``: the real headset and Fish Audio. Speech plays out loud and the reply uses a
little TTS. The model is set for this run only (JARVIS_TTS_MODEL) and the voice is
switched in memory; no settings file is touched. The key is never printed.

The chunks go out at 0, 1.5 and 3.0 s, like Claude writing a reply sentence by sentence;
speech starting before the last chunk shows that playback streams.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

TESTS = Path(__file__).resolve().parents[1] / "tests"
KEY = (os.environ.get("FISH_AUDIO_API_KEY") or "").strip()
CHUNKS = [
    (0.0, "Good evening, sir.", False),
    (1.5, " All systems are online, and the workshop is at your disposal.", False),
    (3.0, " Shall I run the diagnostics?", True),
]


def redact(text: str) -> str:
    return text.replace(KEY, "<FISH_KEY>") if KEY else text


class Helper:
    """A helper process plus a timestamped log of its stdout events."""

    def __init__(self, args: list[str], env: dict[str, str]) -> None:
        self.token = uuid.uuid4().hex
        self.events: list[tuple[float, dict[str, Any]]] = []
        self._lock = threading.Lock()
        self._err = tempfile.NamedTemporaryFile(prefix="jarvis-latency-", suffix=".err", delete=False)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "jarvis_voice", "run", "--heartbeat-timeout", "300", "--heartbeat-grace", "300", *args],
            stdout=subprocess.PIPE, stderr=self._err, text=True, env={**env, "JARVIS_TOKEN": self.token},
        )
        threading.Thread(target=self._read, daemon=True).start()
        self.port: int | None = None

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            with contextlib.suppress(json.JSONDecodeError):
                event = json.loads(line)
                with self._lock:
                    self.events.append((time.perf_counter(), event))

    def wait_for(self, pred: Callable[[dict[str, Any]], bool], timeout: float) -> tuple[float, dict[str, Any]] | None:
        end = time.perf_counter() + timeout
        while time.perf_counter() < end:
            with self._lock:
                for stamp, event in self.events:
                    if pred(event):
                        return stamp, event
            time.sleep(0.005)
        return None

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())

    def start(self, timeout: float = 90) -> bool:
        hello = self.wait_for(lambda e: e.get("type") == "hello", timeout)
        ready = self.wait_for(lambda e: e.get("type") == "ready", timeout)
        if hello:
            self.port = hello[1]["port"]
        return bool(hello and ready)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            if self.port:
                self.post("/v1/shutdown", {})
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self._err.close()
        tail = Path(self._err.name).read_text(encoding="utf-8", errors="replace").splitlines()[-15:]
        os.unlink(self._err.name)
        print(f"helper exit code {self.proc.returncode}; stderr tail:")
        print(redact("\n".join(tail)))


def stream_reply(helper: Helper, label: str) -> None:
    rid = "lat-" + uuid.uuid4().hex[:6]
    t0 = time.perf_counter()
    for seq, (delay, text, final) in enumerate(CHUNKS):
        while time.perf_counter() < t0 + delay:
            time.sleep(0.002)
        helper.post("/v1/speak", {"replyId": rid, "seq": seq, "text": text, "final": final})
    started = helper.wait_for(lambda e: e.get("type") == "speech_started" and e.get("replyId") == rid, 25)
    done = helper.wait_for(lambda e: e.get("type") == "speech_done" and e.get("replyId") == rid, 60)
    first = f"{(started[0] - t0) * 1000:.0f} ms" if started else "never"
    print(f"[{label}] time to first audio after the first chunk: {first} (last chunk sent at 3000 ms)")
    if done:
        print(f"[{label}] speech_done interrupted={done[1].get('interrupted')} spokenText={done[1].get('spokenText')!r}")
    for _, event in helper.events:
        if event.get("type") == "error":
            print(redact(f"[{label}] error event: {json.dumps(event)}"))


@contextlib.contextmanager
def fake_fish() -> Iterator[str]:
    sys.path.insert(0, str(TESTS))
    from fish_fake import FakeFishServer  # noqa: PLC0415 - test helper, only importable from the repo

    with FakeFishServer(api_key="good-key", chunk_delay=0.04) as server:
        yield server.url


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser("fake", help="helper overhead only: fake Fish server, silent fake audio")
    live = sub.add_parser("live", help="real headset and Fish Audio")
    live.add_argument("--model", default="s2.1-pro-free")
    live.add_argument("--voice-id", default=None)
    live.add_argument("--data-dir", type=Path, default=Path.home() / ".jarvis")
    args = parser.parse_args()

    env = dict(os.environ)
    if args.mode == "fake":
        env["FISH_AUDIO_API_KEY"] = "good-key"  # the fake's key; the real one never leaves this process
        with fake_fish() as url, tempfile.TemporaryDirectory() as data:
            helper = Helper(["--data-dir", data, "--fake-audio", "--fake-stt", "--fake-fish", url,
                             "--instance-name", "jarvis-latency-test"], env)
            try:
                if helper.start(30):
                    stream_reply(helper, "fake Fish")
                else:
                    print("helper did not become ready")
            finally:
                helper.close()
        return

    env["JARVIS_TTS_MODEL"] = args.model
    helper = Helper(["--data-dir", str(args.data_dir)], env)
    try:
        if not helper.start():
            print("helper did not become ready")
            return
        if args.voice_id:
            helper.post("/v1/config", {"voiceId": args.voice_id})
        stream_reply(helper, f"live, model {args.model}")
    finally:
        helper.close()


if __name__ == "__main__":
    main()
