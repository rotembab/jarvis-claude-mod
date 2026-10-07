"""A local stand-in for Fish Audio's live TTS WebSocket (MessagePack frames).

It speaks the same frames as the real service: it expects ``start`` first,
buffers ``text``, synthesises a tone for the buffer on ``flush`` and on
``stop``, sends ``audio`` frames, then ``finish`` and closes.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any

import numpy as np
import ormsgpack
from websockets.sync.server import Server, ServerConnection, serve


@dataclass
class Session:
    headers: dict[str, str]
    frames: list[dict[str, Any]] = field(default_factory=list)
    audio_bytes: int = 0
    closed: threading.Event = field(default_factory=threading.Event)

    @property
    def start(self) -> dict[str, Any] | None:
        return self.frames[0] if self.frames and self.frames[0].get("event") == "start" else None

    def events(self) -> list[str]:
        return [f.get("event", "?") for f in self.frames]

    def texts(self) -> list[str]:
        return [f["text"] for f in self.frames if f.get("event") == "text"]


class FakeFishServer:
    """``mode``: "ok", "finish_error" (finish reason error), "drop" (abort mid-stream)."""

    def __init__(
        self,
        *,
        api_key: str = "good-key",
        mode: str = "ok",
        ms_per_char: float = 12.0,
        chunk_ms: float = 40.0,
        chunk_delay: float = 0.0,
        odd_chunks: bool = False,
        no_credit: bool = False,
    ) -> None:
        self.api_key = api_key
        self.no_credit = no_credit  # a valid key on an account without API credit: HTTP 402
        self.mode = mode
        self.ms_per_char = ms_per_char
        self.chunk_ms = chunk_ms
        self.chunk_delay = chunk_delay
        self.odd_chunks = odd_chunks  # split audio at odd byte offsets to test reassembly
        self.sessions: list[Session] = []
        self.rejected = 0
        self._server: Server | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle

    def __enter__(self) -> FakeFishServer:
        self._server = serve(self._handler, "127.0.0.1", 0, process_request=self._process_request, compression=None)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        assert self._server is not None
        self._server.shutdown()

    @property
    def url(self) -> str:
        assert self._server is not None
        host, port = self._server.socket.getsockname()[:2]
        return f"ws://{host}:{port}"

    # -- protocol

    def _process_request(self, connection: ServerConnection, request: Any) -> Any:
        if request.path != "/v1/tts/live":
            return connection.respond(HTTPStatus.NOT_FOUND, "not found\n")
        if request.headers.get("Authorization") != f"Bearer {self.api_key}":
            self.rejected += 1
            return connection.respond(HTTPStatus.UNAUTHORIZED, "invalid api key\n")
        if self.no_credit:
            self.rejected += 1
            body = '{"status": 402, "message": "Insufficient API credit. API credit is managed independently."}'
            return connection.respond(HTTPStatus.PAYMENT_REQUIRED, body)
        return None

    def pcm_for(self, text: str, sample_rate: int) -> bytes:
        n = int(len(text) * self.ms_per_char / 1000 * sample_rate)
        t = np.arange(n) / sample_rate
        return (np.sin(2 * np.pi * 330 * t) * 0.25 * 32767).astype("<i2").tobytes()

    def _send_audio(self, ws: ServerConnection, session: Session, pcm: bytes, sample_rate: int) -> None:
        step = int(self.chunk_ms / 1000 * sample_rate) * 2
        if self.odd_chunks:
            step += 1
        for i in range(0, len(pcm), max(step, 1)):
            chunk = pcm[i : i + step]
            ws.send(ormsgpack.packb({"event": "audio", "audio": chunk}))
            session.audio_bytes += len(chunk)
            if self.chunk_delay:
                time.sleep(self.chunk_delay)

    def _handler(self, ws: ServerConnection) -> None:
        session = Session(headers={k: v for k, v in ws.request.headers.raw_items()})
        self.sessions.append(session)
        buffer = ""
        sample_rate = 44_100
        try:
            for message in ws:
                frame = ormsgpack.unpackb(message)
                session.frames.append(frame)
                event = frame.get("event")
                if event == "start":
                    sample_rate = int(frame["request"].get("sample_rate") or 44_100)
                elif event == "text":
                    buffer += frame["text"]
                elif event == "flush":
                    if self.mode == "drop" and len(session.texts()) >= 2:
                        ws.close(code=1011, reason="internal error")
                        return
                    self._send_audio(ws, session, self.pcm_for(buffer, sample_rate), sample_rate)
                    buffer = ""
                elif event == "stop":
                    self._send_audio(ws, session, self.pcm_for(buffer, sample_rate), sample_rate)
                    reason = "error" if self.mode == "finish_error" else "stop"
                    ws.send(ormsgpack.packb({"event": "finish", "reason": reason}))
                    ws.close()
                    return
        except Exception:  # noqa: BLE001 - client went away
            pass
        finally:
            session.closed.set()
