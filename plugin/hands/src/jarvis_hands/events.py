"""stdout event stream: one JSON object per line, written by a dedicated thread.

The camera and tracker threads emit at frame rate, so ``emit()`` only
enqueues. A single writer thread serialises, writes and flushes each line, so
lines never interleave and a slow pipe never stalls tracking. Same design as
``jarvis_voice.events``.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from collections.abc import Callable
from typing import Any, BinaryIO, Protocol

log = logging.getLogger(__name__)

_STOP = object()


class EventSink(Protocol):
    def emit(self, event: dict[str, Any]) -> None: ...


def encode_line(payload: dict[str, Any]) -> bytes:
    """One protocol line. Raises ValueError for NaN or infinity, which JSON.parse would choke on."""
    # ensure_ascii keeps every line pure ASCII, so console code pages and
    # pipe encodings on Windows can never mangle it.
    return (json.dumps(payload, separators=(",", ":"), ensure_ascii=True, allow_nan=False) + "\n").encode("ascii")


class EventWriter:
    """Queue-backed JSON-lines writer for the helper's stdout."""

    def __init__(self, stream: BinaryIO, *, on_broken_pipe: Callable[[], None] | None = None) -> None:
        self._stream = stream
        self._on_broken_pipe = on_broken_pipe
        self._queue: queue.Queue[Any] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="event-writer", daemon=True)
        self._broken = False

    def start(self) -> EventWriter:
        self._thread.start()
        return self

    def emit(self, event: dict[str, Any]) -> None:
        self._queue.put(dict(event))

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
                line = encode_line(item)
            except (TypeError, ValueError):  # a bug in the caller (a numpy scalar, a NaN): drop it, keep going
                log.exception("cannot encode event %r", item)
                continue
            try:
                self._stream.write(line)
                self._stream.flush()
            except (OSError, ValueError) as exc:  # broken pipe / closed stream: the mod is gone
                self._broken = True
                log.warning("stdout is gone (%s); requesting exit", exc)
                if self._on_broken_pipe is not None:
                    try:
                        self._on_broken_pipe()
                    except Exception:  # the writer must still drain the queue so close() returns
                        log.exception("broken pipe callback failed")
