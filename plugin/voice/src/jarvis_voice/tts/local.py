"""SpeechSynth backed by the local voice: Chatterbox-Turbo in its own process.

The local voice (``plugin/local-voice``, package ``jarvis_local_voice``) lives in
its own environment, ``<data dir>/local-voice/venv``, installed by ``/jarvis setup
local``. The helper never imports torch: it starts ``python -m jarvis_local_voice
serve`` there and talks JSON lines over the child's stdin and stdout (the protocol
is described in ``jarvis_local_voice/server.py``). Keeping torch out of this
process also keeps its CUDA libraries away from CTranslate2's, which have the
same file names but other versions.

The child loads the model once, when the helper starts, and stays up. Each reply
is a "stream" id; its sentences are queued in order and their audio comes back
as soon as each one is synthesized. Closing the child's stdin (which also happens
when the helper dies) ends it.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import subprocess
import sys
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .base import AudioCallback, SynthError

log = logging.getLogger(__name__)

SAMPLE_RATE = 24_000
READY_TIMEOUT_S = 90.0  # the first load after a reboot reads about 3 GB from disk
# Where `jarvis_local_voice download` puts Chatterbox-Turbo, and the weights it loads.
MODEL_DIRNAME = "chatterbox-turbo"
MODEL_WEIGHTS = ("t3_turbo_v1.safetensors", "s3gen_meanflow.safetensors", "ve.safetensors")
LOCAL_HINT = "Run /jarvis setup local to install the local voice, or switch back with /jarvis engine fish."


def default_python(data_dir: Path) -> Path:
    venv = data_dir / "local-voice" / "venv"
    return venv / "Scripts" / "python.exe" if sys.platform == "win32" else venv / "bin" / "python"


def model_downloaded(models_dir: Path) -> bool:
    folder = models_dir / MODEL_DIRNAME
    return all((folder / name).is_file() for name in MODEL_WEIGHTS)


@dataclass(frozen=True, slots=True)
class LocalVoiceSettings:
    python: Path
    models_dir: Path
    voice_clip: Path | None = None
    fake: bool = False  # a tone instead of the model (tests)
    extra_args: tuple[str, ...] = ()
    extra_env: Mapping[str, str] = field(default_factory=dict)
    ready_timeout: float = READY_TIMEOUT_S

    def argv(self) -> list[str]:
        argv = [str(self.python), "-m", "jarvis_local_voice", "serve", "--models-dir", str(self.models_dir)]
        if self.voice_clip is not None:
            argv += ["--voice", str(self.voice_clip)]
        if self.fake:
            argv.append("--fake")
        return [*argv, *self.extra_args]


class LocalVoiceSynth:
    """Starts and owns the local voice process; hands out one LocalStream per reply."""

    engine = "local"

    def __init__(self, settings: LocalVoiceSettings) -> None:
        self.settings = settings
        self.sample_rate = SAMPLE_RATE
        self.device: str | None = None
        self._cond = threading.Condition()
        self._send_lock = threading.Lock()
        self._proc: subprocess.Popen[bytes] | None = None
        self._state = "stopped"  # stopped | loading | ready | failed
        self._failure: str | None = None
        self._fatal = False  # the model could not load: retrying on every reply would only repeat it
        self._streams: dict[int, LocalStream] = {}
        self._next_id = 1
        self._closed = False

    @property
    def configured(self) -> bool:
        return True  # nothing to configure; a missing install is reported when a reply opens a stream

    @property
    def state(self) -> str:
        return self._state

    def start(self) -> None:
        """Start loading the model now, so the first reply doesn't wait for it."""
        with self._cond:
            self._start_locked()

    def open_stream(self, on_audio: AudioCallback, *, voice_id: str | None) -> LocalStream:
        del voice_id  # Fish voice ids mean nothing here; the voice is the reference clip
        with self._cond:
            if self._closed:
                raise SynthError("local_voice_failed", "The local voice is shut down.")
            if self._state == "stopped" or (self._state == "failed" and not self._fatal):
                self._start_locked()
            self._cond.wait_for(lambda: self._state != "loading", timeout=self.settings.ready_timeout)
            if self._state == "loading":
                raise SynthError(
                    "local_voice_failed",
                    "The local voice is still loading.",
                    "Try again in a moment: its first start loads the model onto the GPU.",
                )
            if self._state != "ready":
                raise SynthError("local_voice_failed", self._failure or "The local voice is not running.", LOCAL_HINT)
            stream = LocalStream(self, self._next_id, on_audio, self.sample_rate)
            self._streams[stream.stream_id] = stream
            self._next_id += 1
            return stream

    def close(self) -> None:
        with self._cond:
            self._closed = True
            proc = self._proc
            self._cond.notify_all()
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()  # the child exits on end of input
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()

    # -- used by LocalStream

    def send(self, request: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or self._state != "ready":
            raise SynthError("local_voice_failed", self._failure or "The local voice is not running.", LOCAL_HINT)
        line = (json.dumps(request, ensure_ascii=True) + "\n").encode("ascii")
        try:
            with self._send_lock:
                proc.stdin.write(line)
                proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise SynthError("local_voice_failed", f"Lost the local voice: {exc}", LOCAL_HINT) from exc

    def forget(self, stream_id: int) -> None:
        with self._cond:
            self._streams.pop(stream_id, None)

    # -- the child process

    def _start_locked(self) -> None:
        if self._closed or (self._proc is not None and self._proc.poll() is None):
            return
        self._state, self._failure, self.device = "loading", None, None
        python = self.settings.python
        if not python.is_file():
            self._fail_locked(f"The local voice is not installed ({python} is missing).", fatal=True)
            return
        env = {**os.environ, **self.settings.extra_env, "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1"}
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # no console window flashing on Windows
        try:
            proc = subprocess.Popen(
                self.settings.argv(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                creationflags=flags,
            )
        except OSError as exc:
            self._fail_locked(f"Could not start the local voice: {exc}", fatal=True)
            return
        self._proc = proc
        log.info("local voice starting (pid %d): %s", proc.pid, " ".join(self.settings.argv()))
        threading.Thread(target=self._read_events, args=(proc,), name="local-voice-out", daemon=True).start()
        threading.Thread(target=self._read_log, args=(proc,), name="local-voice-err", daemon=True).start()

    def _fail_locked(self, message: str, *, fatal: bool) -> None:
        self._state, self._failure = "failed", message
        self._fatal = self._fatal or fatal
        self._cond.notify_all()
        log.warning("%s", message)

    def _read_events(self, proc: subprocess.Popen[bytes]) -> None:
        assert proc.stdout is not None
        for raw in proc.stdout:
            try:
                event = json.loads(raw)
            except ValueError:
                log.warning("local voice: unreadable line %r", raw[:200])
                continue
            if isinstance(event, dict):
                self._dispatch(event)
        code = proc.wait()
        with self._cond:
            if self._proc is proc:
                self._proc = None
            if self._state != "failed" and not self._closed:
                self._fail_locked(f"The local voice stopped unexpectedly (exit code {code}).", fatal=False)
            streams = list(self._streams.values())
            self._streams.clear()
            message = self._failure or "The local voice stopped."
        for stream in streams:
            stream.fail(SynthError("local_voice_failed", message, LOCAL_HINT))

    def _dispatch(self, event: dict[str, Any]) -> None:
        kind = event.get("event")
        if kind == "ready":
            with self._cond:
                self._state = "ready"
                self.device = str(event.get("device"))
                self._cond.notify_all()
            log.info(
                "local voice ready on %s (voice %s, loaded in %s s)",
                event.get("device"),
                event.get("voice"),
                event.get("loadSeconds"),
            )
            if event.get("sampleRate") != SAMPLE_RATE:
                log.warning("local voice sample rate %s, expected %d", event.get("sampleRate"), SAMPLE_RATE)
        elif kind == "fatal":
            with self._cond:
                self._fail_locked(f"The local voice could not start: {event.get('message')}", fatal=True)
        elif kind in ("audio", "said", "error"):
            stream = self._streams.get(event.get("stream"))  # type: ignore[arg-type]
            if stream is not None:
                stream.on_event(event)
            elif kind == "error" and "stream" not in event:
                log.warning("local voice: %s", event.get("message"))

    def _read_log(self, proc: subprocess.Popen[bytes]) -> None:
        assert proc.stderr is not None
        for raw in proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                log.info("local voice: %s", line[:300])


class LocalStream:
    """One reply's sentences on the local voice."""

    def __init__(self, owner: LocalVoiceSynth, stream_id: int, on_audio: AudioCallback, sample_rate: int) -> None:
        self.sample_rate = sample_rate
        self.stream_id = stream_id
        self._owner = owner
        self._on_audio = on_audio
        self._lock = threading.Lock()
        self._seq = 0
        self._pending: set[int] = set()
        self._finishing = False
        self._done = False
        self._closed = False
        self._cancelled = False
        self._error: SynthError | None = None

    @property
    def done(self) -> bool:
        return self._done

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def error(self) -> SynthError | None:
        return self._error

    def send_text(self, text: str) -> None:
        with self._lock:
            if self._closed:
                raise SynthError("local_voice_failed", "This local voice reply is already closed.", LOCAL_HINT)
            seq = self._seq
            self._seq += 1
            self._pending.add(seq)
        try:
            self._owner.send({"op": "say", "stream": self.stream_id, "seq": seq, "text": text.strip()})
        except SynthError as exc:
            self.fail(exc)
            raise

    def finish(self) -> None:
        with self._lock:
            if self._closed or self._finishing:
                return
            self._finishing = True
            complete = not self._pending
        if complete:
            self._complete()

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            if self._closed:
                return
            self._closed = True
        self._owner.forget(self.stream_id)
        try:
            self._owner.send({"op": "cancel", "stream": self.stream_id})
        except SynthError:
            pass  # the process is gone; nothing left to cancel

    def fail(self, error: SynthError) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if not self._cancelled:
                self._error = error
        self._owner.forget(self.stream_id)

    def on_event(self, event: dict[str, Any]) -> None:
        """Called on the reader thread, in order."""
        if self._cancelled:
            return
        kind = event.get("event")
        if kind == "audio":
            try:
                pcm = base64.b64decode(event.get("pcm") or b"", validate=True)
            except (binascii.Error, ValueError):
                log.warning("local voice: bad audio in stream %d", self.stream_id)
                return
            if pcm:
                self._on_audio(pcm)
            return
        if kind == "error":
            # One sentence failed; the rest of the reply still plays.
            log.warning("local voice could not say sentence %s: %s", event.get("seq"), event.get("message"))
        with self._lock:
            self._pending.discard(event.get("seq"))  # type: ignore[arg-type]
            complete = self._finishing and not self._pending and not self._closed
        if complete:
            self._complete()

    def _complete(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._done = True
            self._closed = True
        self._owner.forget(self.stream_id)
