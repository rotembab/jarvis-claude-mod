from __future__ import annotations

import io
import json
import threading

import pytest

from jarvis_voice import protocol
from jarvis_voice.events import EventWriter


class SlowStream(io.BytesIO):
    def __init__(self) -> None:
        super().__init__()
        self.flushes = 0
        self.writer_threads: set[str] = set()

    def write(self, data: bytes) -> int:  # type: ignore[override]
        self.writer_threads.add(threading.current_thread().name)
        return super().write(data)

    def flush(self) -> None:
        self.flushes += 1


def test_one_json_line_per_event_flushed_in_order() -> None:
    stream = SlowStream()
    writer = EventWriter(stream).start()
    writer.emit(protocol.Hello(port=1, pid=2, platform="linux", version="0", capabilities=()))
    for i in range(50):
        writer.emit(protocol.Level(mic=i / 100, out=0.0))
    writer.emit(protocol.Utterance(id="u", text="café — ok", source="ptt", duration_ms=3))
    writer.close()
    raw = stream.getvalue()
    assert raw.isascii()  # non-ASCII is escaped, so pipe encodings cannot mangle it
    lines = raw.decode().splitlines()
    assert len(lines) == 52 and stream.flushes == 52
    assert json.loads(lines[0])["type"] == "hello"
    assert [json.loads(line)["mic"] for line in lines[1:51]] == [i / 100 for i in range(50)]
    assert json.loads(lines[-1])["text"] == "café — ok"
    assert stream.writer_threads == {"event-writer"}
    assert b"\r" not in raw


def test_strict_mode_rejects_schema_violations() -> None:
    writer = EventWriter(io.BytesIO(), strict=True)
    with pytest.raises(protocol.ValidationError):
        writer.emit({"v": 1, "type": "state", "state": "bogus"})


def test_broken_pipe_requests_exit_once() -> None:
    class Broken(io.BytesIO):
        def write(self, data: bytes) -> int:  # type: ignore[override]
            raise BrokenPipeError("gone")

    calls: list[int] = []
    writer = EventWriter(Broken(), on_broken_pipe=lambda: calls.append(1)).start()
    writer.emit(protocol.State(state="sleeping"))
    writer.emit(protocol.State(state="listening"))
    writer.close()
    assert calls == [1]
