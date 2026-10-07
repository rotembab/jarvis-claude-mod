from __future__ import annotations

import io
import json
import threading

from jarvis_hands import protocol
from jarvis_hands.events import EventSink, EventWriter, encode_line


class RecordingStream(io.BytesIO):
    def __init__(self) -> None:
        super().__init__()
        self.flushes = 0
        self.writer_threads: set[str] = set()

    def write(self, data: bytes) -> int:  # type: ignore[override]
        self.writer_threads.add(threading.current_thread().name)
        return super().write(data)

    def flush(self) -> None:
        self.flushes += 1


class Broken(io.BytesIO):
    def write(self, data: bytes) -> int:  # type: ignore[override]
        raise BrokenPipeError("gone")


def test_one_ascii_json_line_per_event_flushed_in_order() -> None:
    stream = RecordingStream()
    writer = EventWriter(stream).start()
    writer.emit(protocol.hello(1, 2, "linux", "0", []))
    for _ in range(50):
        writer.emit(protocol.gesture("click"))
    writer.emit(protocol.error("camera_blocked", "caméra — bloquée", hint="Réglages"))
    writer.close()
    raw = stream.getvalue()
    assert raw.isascii()  # non-ASCII is escaped, so pipe encodings cannot mangle it
    assert b"\r" not in raw
    lines = raw.decode().splitlines()
    assert len(lines) == 52 and stream.flushes == 52
    assert json.loads(lines[0])["type"] == "hello"
    assert all(json.loads(line) == {"v": 1, "type": "gesture", "name": "click"} for line in lines[1:51])
    assert json.loads(lines[-1])["message"] == "caméra — bloquée"
    assert stream.writer_threads == {"event-writer"}


def test_emit_copies_the_event() -> None:
    stream = RecordingStream()
    writer = EventWriter(stream)  # not started: events wait in the queue
    event = protocol.state("idle")
    writer.emit(event)
    event["state"] = "error"
    writer.start().close()
    assert json.loads(stream.getvalue()) == {"v": 1, "type": "state", "state": "idle"}


def test_events_from_many_threads_never_interleave() -> None:
    stream = RecordingStream()
    writer = EventWriter(stream).start()

    def produce(n: int) -> None:
        for _ in range(100):
            writer.emit(protocol.calibration("top_left", display=f"display {n} " + "x" * 200))

    threads = [threading.Thread(target=produce, args=(n,)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    writer.close()
    lines = stream.getvalue().decode().splitlines()
    assert len(lines) == 400
    assert all(json.loads(line)["type"] == "calibration" for line in lines)


def test_unencodable_event_is_dropped_and_the_writer_carries_on() -> None:
    stream = RecordingStream()
    writer = EventWriter(stream).start()
    writer.emit({"v": 1, "type": "ready", "fps": float("nan")})  # JSON.parse would reject NaN
    writer.emit({"v": 1, "type": "bad", "value": object()})
    writer.emit(protocol.state("active"))
    writer.close()
    assert stream.getvalue() == encode_line(protocol.state("active"))


def test_broken_pipe_requests_exit_once() -> None:
    calls: list[int] = []
    writer = EventWriter(Broken(), on_broken_pipe=lambda: calls.append(1)).start()
    writer.emit(protocol.state("idle"))
    writer.emit(protocol.state("active"))
    writer.close()
    assert calls == [1]


def test_a_failing_broken_pipe_callback_does_not_hang_close() -> None:
    def explode() -> None:
        raise RuntimeError("callback bug")

    writer = EventWriter(Broken(), on_broken_pipe=explode).start()
    writer.emit(protocol.state("idle"))
    writer.emit(protocol.state("active"))
    writer.close(timeout=5)
    assert not writer._thread.is_alive()


def test_close_without_start_and_twice_is_harmless() -> None:
    writer = EventWriter(RecordingStream())
    writer.close()
    started = EventWriter(RecordingStream()).start()
    started.close()
    started.close()


def test_encode_line_is_compact() -> None:
    assert encode_line({"v": 1, "type": "state", "state": "idle"}) == b'{"v":1,"type":"state","state":"idle"}\n'


def test_event_writer_is_an_event_sink() -> None:
    sink: EventSink = EventWriter(RecordingStream())
    assert callable(sink.emit)
