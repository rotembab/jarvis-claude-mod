"""stdout event stream: one JSON object per line, written by a dedicated thread.

Producers (audio callbacks excepted: they never emit) call ``emit()``, which
only validates and enqueues. A single writer thread serialises, writes and
flushes each line so lines never interleave and a slow pipe never blocks
the caller.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from collections.abc import Callable
from typing import Any, BinaryIO, Protocol

from . import protocol

log = logging.getLogger(__name__)

_STOP = object()


class EventSink(Protocol):
    def emit(self, event: protocol.Event | dict[str, Any]) -> None: ...


def encode_line(payload: dict[str, Any]) -> bytes:
    # ensure_ascii keeps every line pure ASCII, so console code pages and
    # pipe encodings on Windows can never mangle it.
    return (json.dumps(payload, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")


class EventWriter:
    """Queue-backed JSON-lines writer for the helper's stdout."""

    def __init__(
        self,
        stream: BinaryIO,
        *,
        validate: bool = True,
        strict: bool = False,
        on_broken_pipe: Callable[[], None] | None = None,
    ) -> None:
        self._stream = stream
        self._validate = validate
        self._strict = strict
        self._on_broken_pipe = on_broken_pipe
        self._queue: queue.Queue[Any] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="event-writer", daemon=True)
        self._broken = False

    def start(self) -> EventWriter:
        self._thread.start()
        return self

    def emit(self, event: protocol.Event | dict[str, Any]) -> None:
        payload = event.to_wire() if isinstance(event, protocol.Event) else dict(event)
        if self._validate and payload.get("type") != "progress":
            try:
                protocol.validate_event(payload)
            except protocol.ValidationError as exc:
                if self._strict:
                    raise
                log.error("emitting an event that does not match the schema: %s (%s)", payload, exc)
        self._queue.put(payload)

    def close(self, timeout: float = 2.0) -> None:
        """Flush everything queued so far, then stop the writer thread."""
        if not self._thread.is_alive():
            return
        self._queue.put(_STOP)
        self._thread.join(timeout)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                return
            if self._broken:
                continue
            try:
                self._stream.write(encode_line(item))
                self._stream.flush()
            except (OSError, ValueError) as exc:  # broken pipe / closed stream: the mod is gone
                self._broken = True
                log.warning("stdout is gone (%s); requesting exit", exc)
                if self._on_broken_pipe is not None:
                    self._on_broken_pipe()
            except Exception:  # pragma: no cover - serialisation bug; keep the writer alive
                log.exception("failed to write event %r", item)
