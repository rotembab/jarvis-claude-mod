"""The ``desktop`` command: checks a request, runs it on the one desktop worker
thread with a time limit, and answers in plain words. Never raises.

Every answer is ``{"ok": true, "desktop": {"result", "text", "path"?,
"clipboard"?}}``: ``ok`` says the helper handled the request and ``result``
how it went (done, failed, refused or unsupported), so a desktop failure needs
no protocol error code. ``text`` is what Claude reads and may speak.

Why one worker thread: the shell and UI calls want a single-threaded COM
apartment, and two actions at once (a volume change during a screenshot) gain
nothing. An action that is still queued when its caller gives up is skipped,
so a late "lock" never happens after the mod was told it failed.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .base import (
    ACTIONS,
    CLIPBOARD_READ_LIMIT,
    CLIPBOARD_WRITE_LIMIT,
    MEDIA_KEYS,
    VOLUME_CHANGES,
    DesktopBackend,
    Failed,
    Refused,
    Result,
    Unsupported,
    classify_target,
)

log = logging.getLogger(__name__)

#: How long one action may take, waiting in line included. The mod waits 8 s for the answer.
ACTION_TIMEOUT_S = 6.0
UNSUPPORTED_TEXT = "Desktop actions work on Windows only for now."
TARGET_LIMIT = 400

#: The body fields each action takes besides ``action``.
FIELDS: dict[str, frozenset[str]] = {
    "open": frozenset({"target"}),
    "focus": frozenset({"target"}),
    "media": frozenset({"key"}),
    "volume": frozenset({"level", "change"}),
    "screenshot": frozenset(),
    "lock": frozenset(),
    "clipboard_read": frozenset(),
    "clipboard_write": frozenset({"text"}),
}

_Reply = dict[str, Any]


def reply(result: Result, text: str, **extra: Any) -> _Reply:
    return {
        "ok": True,
        "desktop": {"result": result, "text": text, **{k: v for k, v in extra.items() if v is not None}},
    }


class _BadRequest(ValueError):
    pass


class _Job:
    """One action in line for the worker; abandoned (skipped) when its caller stops waiting before it starts."""

    def __init__(self, action: str, run: Callable[[], _Reply]) -> None:
        self.action = action
        self.run = run
        self.done = threading.Event()
        self.answer: _Reply | None = None
        self._lock = threading.Lock()
        self._state = "queued"  # -> running -> finished, or queued -> abandoned

    def start(self) -> bool:
        with self._lock:
            if self._state == "abandoned":
                return False
            self._state = "running"
            return True

    def finish(self, answer: _Reply) -> None:
        with self._lock:
            self.answer, self._state = answer, "finished"
        self.done.set()

    def abandon(self) -> str:
        """Gives up on the job; returns the state it was in (only a queued job is actually dropped)."""
        with self._lock:
            state = self._state
            if state == "queued":
                self._state = "abandoned"
            return state


class DesktopService:
    """Answers ``desktop`` commands with ``backend``; None (no backend for this OS) answers "unsupported"."""

    def __init__(self, backend: DesktopBackend | None, *, action_timeout: float = ACTION_TIMEOUT_S) -> None:
        self.backend = backend
        self._timeout = action_timeout
        self._jobs: queue.Queue[_Job | None] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._lock = threading.Lock()
        self._closed = False
        if backend is not None:
            try:
                backend.warm()
            except Exception:  # warming is an optimisation
                log.exception("could not warm the desktop backend")

    # ------------------------------------------------------------------ entry point

    def handle(self, body: Mapping[str, Any]) -> _Reply:
        """Answers one ``desktop`` command; never raises."""
        action = body.get("action")
        try:
            if self.backend is None:
                return reply("unsupported", UNSUPPORTED_TEXT)
            if action not in ACTIONS:
                raise _BadRequest(f"Unknown desktop action {action!r}.")
            run = self._prepare(self.backend, str(action), body)
            return self._submit(str(action), run)
        except _BadRequest as exc:
            return reply("failed", str(exc))
        except Exception:
            log.exception("desktop %s failed", action)
            return reply("failed", f"The {action} action hit an internal error; the helper's log has the details.")

    def close(self) -> None:
        """Stops the worker after the action in hand. The helper never needs this (the worker is a daemon)."""
        with self._lock:
            self._closed = True
            worker = self._worker
        if worker is not None:
            self._jobs.put(None)

    # ------------------------------------------------------------------ checking a request

    def _prepare(self, backend: DesktopBackend, action: str, body: Mapping[str, Any]) -> Callable[[], _Reply]:
        """Checks the body for ``action`` and returns the call that performs it on the worker."""
        extra = sorted(set(body) - {"action"} - FIELDS[action])
        if extra:
            raise _BadRequest(f"The {action} action takes no {', '.join(extra)}.")
        if action in ("open", "focus"):
            target = body.get("target")
            if not isinstance(target, str) or not target.strip():
                raise _BadRequest(f"The {action} action needs a target.")
            if len(target) > TARGET_LIMIT:
                raise _BadRequest(f"The target is longer than {TARGET_LIMIT} characters.")
            if action == "focus":
                return lambda: reply("done", backend.focus(target.strip()))
            return lambda: self._open(backend, target)
        if action == "media":
            key = body.get("key")
            if key not in MEDIA_KEYS:
                raise _BadRequest(f"The media action needs key: {', '.join(MEDIA_KEYS)}.")
            return lambda: reply("done", backend.media(key))
        if action == "volume":
            level, change = body.get("level"), body.get("change")
            if level is not None and change is not None:
                raise _BadRequest("Give the volume a level or a change, not both.")
            if level is not None and (isinstance(level, bool) or not isinstance(level, int) or not 0 <= level <= 100):
                raise _BadRequest("The volume level is a whole number from 0 to 100.")
            if change is not None and change not in VOLUME_CHANGES:
                raise _BadRequest(f"The volume change is one of {', '.join(VOLUME_CHANGES)}.")
            return lambda: reply("done", backend.volume(level, change))
        if action == "screenshot":
            return lambda: self._screenshot(backend)
        if action == "lock":
            return lambda: reply("done", backend.lock())
        if action == "clipboard_read":
            return lambda: self._clipboard_read(backend)
        text = body.get("text")
        if not isinstance(text, str) or not text:
            raise _BadRequest("The clipboard_write action needs text.")
        if len(text) > CLIPBOARD_WRITE_LIMIT:
            raise _BadRequest(f"Clipboard text is limited to {CLIPBOARD_WRITE_LIMIT:,} characters.")
        return lambda: reply("done", backend.clipboard_write(text))

    def _open(self, backend: DesktopBackend, target: str) -> _Reply:
        kind, value = classify_target(target)
        if kind == "uri":
            return reply("done", backend.open_uri(value))
        if kind == "folder":
            return reply("done", backend.open_folder(Path(value)))
        return reply("done", backend.open_app(value, deadline=time.monotonic() + self._timeout - 0.5))

    @staticmethod
    def _screenshot(backend: DesktopBackend) -> _Reply:
        shot = backend.screenshot()
        text = (
            f"Saved a {shot.width}x{shot.height} screenshot to {shot.path}. "
            "Look at it with Read only if the user asked you to see the screen."
        )
        return reply("done", text, path=str(shot.path))

    @staticmethod
    def _clipboard_read(backend: DesktopBackend) -> _Reply:
        text = backend.clipboard_read()
        if not text:
            return reply("done", "The clipboard holds no text.")
        if len(text) > CLIPBOARD_READ_LIMIT:
            note = f"The clipboard holds {len(text):,} characters of text; here are the first {CLIPBOARD_READ_LIMIT:,}."
            return reply("done", note, clipboard=text[:CLIPBOARD_READ_LIMIT])
        return reply("done", f"The clipboard holds {len(text):,} characters of text.", clipboard=text)

    # ------------------------------------------------------------------ the worker

    def _submit(self, action: str, run: Callable[[], _Reply]) -> _Reply:
        job = _Job(action, run)
        with self._lock:
            if self._closed:
                return reply("failed", "Desktop actions have stopped; the helper is shutting down.")
            if self._worker is None:
                self._worker = threading.Thread(target=self._work, name="jarvis-desktop", daemon=True)
                self._worker.start()
            self._jobs.put(job)
        if job.done.wait(self._timeout):
            assert job.answer is not None
            return job.answer
        state = job.abandon()
        if state == "finished":  # it finished as we gave up
            assert job.answer is not None
            return job.answer
        if state == "queued":
            log.warning("desktop %s dropped: the worker is still busy", action)
            return reply("failed", f"The desktop is still busy with an earlier action, so {action} was not done.")
        log.warning("desktop %s is taking longer than %g s", action, self._timeout)
        return reply("failed", f"The {action} action is taking longer than {self._timeout:g} s; it may still finish.")

    def _work(self) -> None:
        backend = self.backend
        assert backend is not None
        try:
            backend.prepare_thread()
        except Exception:  # each action reports its own failure
            log.exception("could not prepare the desktop thread")
        while True:
            job = self._jobs.get()
            if job is None:
                return
            if job.start():
                job.finish(self._perform(job))

    @staticmethod
    def _perform(job: _Job) -> _Reply:
        try:
            answer = job.run()
        except Refused as exc:
            answer = reply("refused", str(exc))
        except Unsupported as exc:
            answer = reply("unsupported", str(exc))
        except Failed as exc:
            answer = reply("failed", str(exc))
        except Exception as exc:
            log.exception("desktop %s failed", job.action)
            answer = reply("failed", f"The {job.action} action failed: {type(exc).__name__}: {exc}")
        log.info("desktop %s: %s", job.action, answer["desktop"]["result"])
        return answer
