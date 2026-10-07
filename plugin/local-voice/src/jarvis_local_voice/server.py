"""The local voice's protocol: JSON lines on stdin and stdout.

The voice helper starts this process and writes one request per line to its stdin:

    {"op": "say", "stream": 3, "seq": 0, "text": "Good evening, sir."}
    {"op": "cancel", "stream": 3}

It reads one event per line from stdout:

    {"event": "ready", "device": "cuda", "voice": "jarvis.wav", "sampleRate": 24000, "loadSeconds": 9.1}
    {"event": "audio", "stream": 3, "seq": 0, "pcm": "<base64 16-bit little-endian mono PCM>"}
    {"event": "said", "stream": 3, "seq": 0}
    {"event": "error", "stream": 3, "seq": 0, "message": "..."}
    {"event": "fatal", "message": "..."}

Sentences are synthesized one at a time in the order they arrive; each ends with
``said`` (or ``error``). ``cancel`` drops a stream's queued sentences, and the audio
of one already being synthesized is not sent. Closing stdin ends the process, so
the helper exiting for any reason never leaves the model loaded on the GPU.

This module uses only the standard library, so it can be tested without the model.
"""

from __future__ import annotations

import base64
import json
import math
import os
import re
import struct
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, BinaryIO, Protocol

MAX_TEXT = 300  # Chatterbox-Turbo degrades on longer inputs
CHUNK_SECONDS = 0.5  # audio is sent in pieces of this length


class Backend(Protocol):
    sample_rate: int
    device: str
    voice: str

    def synthesize(self, text: str) -> bytes:
        """One sentence -> 16-bit little-endian mono PCM at ``sample_rate``."""
        ...


@dataclass(frozen=True, slots=True)
class Say:
    stream: int
    seq: int
    text: str


_QUOTES = re.compile(r"[\"“”„«»]")


def clean_text(text: str) -> str:
    """Text Chatterbox reads well: no double quotes (read as a sigh), one line, bounded length."""
    text = " ".join(_QUOTES.sub("", text).split())
    if len(text) > MAX_TEXT:
        cut = text.rfind(" ", 0, MAX_TEXT)
        text = text[: cut if cut > MAX_TEXT // 2 else MAX_TEXT]
    return text


class Server:
    def __init__(self, out: BinaryIO) -> None:
        self._out = out
        self._out_lock = threading.Lock()
        self._cond = threading.Condition()
        self._queue: deque[Say] = deque()
        self._cancelled: set[int] = set()
        self._eof = False
        self.loaded = False

    def emit(self, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n"
        with self._out_lock:
            self._out.write(line.encode("ascii"))
            self._out.flush()

    # -- requests (reader thread)

    def read_requests(self, stdin: BinaryIO) -> None:
        try:
            for raw in stdin:
                self.handle_line(raw)
        except (OSError, ValueError):
            pass
        finally:
            with self._cond:
                self._eof = True
                self._cond.notify_all()
            if not self.loaded:
                # The helper went away while the model was still loading: don't finish loading for nobody.
                os._exit(0)

    def handle_line(self, raw: bytes) -> None:
        if not raw.strip():
            return
        try:
            request = json.loads(raw)
            op = request["op"]
            stream = request["stream"]
            if not isinstance(stream, int) or isinstance(stream, bool):
                raise TypeError("stream must be an integer")
            if op == "say":
                seq, text = request["seq"], request["text"]
                if not isinstance(seq, int) or isinstance(seq, bool) or not isinstance(text, str):
                    raise TypeError("seq must be an integer and text a string")
                with self._cond:
                    if stream not in self._cancelled:
                        self._queue.append(Say(stream, seq, text))
                        self._cond.notify_all()
            elif op == "cancel":
                with self._cond:
                    self._cancelled.add(stream)
                    self._queue = deque(say for say in self._queue if say.stream != stream)
            else:
                raise ValueError(f"unknown op {op!r}")
        except (ValueError, KeyError, TypeError) as exc:
            self.emit({"event": "error", "message": f"bad request: {exc}"})

    # -- synthesis (main thread)

    def next_say(self) -> Say | None:
        with self._cond:
            self._cond.wait_for(lambda: bool(self._queue) or self._eof)
            return self._queue.popleft() if self._queue else None

    def run(self, backend: Backend) -> int:
        chunk_bytes = max(2, int(backend.sample_rate * CHUNK_SECONDS) * 2)
        while (say := self.next_say()) is not None:
            if say.stream in self._cancelled:
                continue
            text = clean_text(say.text)
            pcm = b""
            if text:
                try:
                    pcm = backend.synthesize(text)
                except Exception as exc:  # noqa: BLE001 - one bad sentence must not end the voice
                    self.emit(
                        {
                            "event": "error",
                            "stream": say.stream,
                            "seq": say.seq,
                            "message": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    continue
            if say.stream in self._cancelled:
                continue
            for start in range(0, len(pcm), chunk_bytes):
                piece = pcm[start : start + chunk_bytes]
                self.emit(
                    {"event": "audio", "stream": say.stream, "seq": say.seq, "pcm": base64.b64encode(piece).decode()}
                )
            self.emit({"event": "said", "stream": say.stream, "seq": say.seq})
        return 0


def serve(load: Callable[[], Backend], stdin: BinaryIO, out: BinaryIO) -> int:
    """Loads the backend, says ``ready``, then answers requests until stdin closes."""
    server = Server(out)
    threading.Thread(target=server.read_requests, args=(stdin,), name="requests", daemon=True).start()
    started = time.monotonic()
    try:
        backend = load()
    except Exception as exc:  # noqa: BLE001 - reported to the helper, which shows it to the user
        server.emit({"event": "fatal", "message": f"{type(exc).__name__}: {exc}"})
        return 1
    server.loaded = True
    server.emit(
        {
            "event": "ready",
            "device": backend.device,
            "voice": backend.voice,
            "sampleRate": backend.sample_rate,
            "loadSeconds": round(time.monotonic() - started, 2),
        }
    )
    return server.run(backend)


class FakeBackend:
    """A quiet tone instead of speech: 60 ms per word, for tests and for checking the wiring."""

    sample_rate = 24_000
    device = "fake"
    voice = "fake"

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay

    def synthesize(self, text: str) -> bytes:
        if self.delay:
            time.sleep(self.delay)
        if text.startswith("FAIL"):
            raise RuntimeError("fake synthesis failure")
        frames = int(self.sample_rate * 0.06 * max(1, len(text.split())))
        step = 2 * math.pi * 220 / self.sample_rate
        return b"".join(struct.pack("<h", int(1000 * math.sin(i * step))) for i in range(frames))
