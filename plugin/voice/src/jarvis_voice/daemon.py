"""The helper's state machine, wiring capture, STT, TTS, playback and push-to-talk.

States: starting -> sleeping (model loaded; ``ready``) -> listening (PTT held,
chime, capture with pre-roll) -> transcribing -> sleeping (``utterance`` if
the clip had words) ... speaking (first audio of a reply) -> sleeping.
"error" replaces sleeping while no speech model is usable.

Threads: push-to-talk callbacks and the ``listen`` command are funnelled into
one action queue served by ``run()`` (the main thread), so listening
transitions never race and the keyboard hook returns immediately (Windows
silently unhooks slow low-level hooks). Transcription runs on its own worker;
speech on the pipeline's worker.
"""

from __future__ import annotations

import concurrent.futures
import logging
import queue
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np

from . import __version__, protocol
from .audio import chimes
from .audio.capture import STT_SAMPLERATE, Capture
from .audio.errors import AudioError, digital_silence_error
from .audio.levels import is_digital_silence, is_silent
from .audio.playback import Playback
from .events import EventSink
from .lifecycle import HeartbeatWatchdog
from .ptt.base import PttUnavailable, PushToTalk
from .ptt.keys import Hotkey, parse_hotkey
from .speech import SpeechPipeline
from .stt.base import SttError, Transcriber
from .tts.base import KEY_HINT as FISH_KEY_HINT
from .tts.base import SpeechSynth

log = logging.getLogger(__name__)

DEFAULT_TEST_LINE = "Good evening, sir. Voice systems are online and at your service."
KEY_HINT = "Use a key name like 'right ctrl', 'f13' or 'alt+space' in the Jarvis plugin settings."
_EXIT = object()


@dataclass
class DaemonConfig:
    stt_model: str = "auto"
    language: str | None = "en"
    ptt_key: str = "right ctrl"
    voice_id: str | None = None
    min_clip_s: float = 0.3
    max_record_s: float = 60.0
    heartbeat_timeout: float = 15.0
    heartbeat_grace: float = 60.0
    level_hz: float = 15.0
    chimes: bool = True
    stt_wait_s: float = 180.0  # how long a clip waits for a model that is still loading


@dataclass(frozen=True, slots=True)
class _Clip:
    pcm: np.ndarray
    source: protocol.UtteranceSource
    duration_ms: int


class Daemon:
    def __init__(
        self,
        config: DaemonConfig,
        *,
        events: EventSink,
        capture: Capture,
        playback: Playback,
        synth: SpeechSynth,
        ptt: PushToTalk,
        transcriber_factory: Callable[[str], Transcriber],
        platform_name: protocol.PlatformName,
        rescan_audio: Callable[[], None] | None = None,
    ) -> None:
        self.config = config
        self._events = events
        self._capture = capture
        self._playback = playback
        self._synth = synth
        self._ptt = ptt
        self._transcriber_factory = transcriber_factory
        self._platform = platform_name
        self._rescan_audio = rescan_audio

        self._hotkey: Hotkey = parse_hotkey(config.ptt_key)
        self._voice_id = config.voice_id or None
        self._language = config.language or None

        self._lock = threading.RLock()
        self._base: protocol.HelperState = "starting"
        self._listen_source: protocol.UtteranceSource | None = None
        self._transcribing = 0
        self._speaking = False
        self._last_state: str | None = None
        self._ptt_armed = False
        self._ready_sent = False

        self._transcriber: Transcriber | None = None
        self._stt_error: SttError | None = None
        self._stt_requested = config.stt_model or "auto"
        self._stt_generation = 0
        self._stt_settled = threading.Event()

        self._actions: queue.Queue[Any] = queue.Queue()
        self._clips: queue.Queue[_Clip | None] = queue.Queue()
        self._stopping = threading.Event()
        self._exit_code: int | None = None

        self.watchdog = HeartbeatWatchdog(
            lambda: self.request_exit(0), timeout=config.heartbeat_timeout, grace=config.heartbeat_grace
        )
        self.pipeline = SpeechPipeline(
            synth,
            playback,
            events,
            voice_id=lambda: self._voice_id,
            on_speaking=self._on_speaking,
        )
        self._threads: list[threading.Thread] = []

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        """Bring components up. Failures become ``error`` events, never exceptions."""
        self._refresh_state()
        self.pipeline.start()
        self.watchdog.start()
        for target, name in ((self._stt_worker, "stt-worker"), (self._level_loop, "levels")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

        with self._lock:
            hotkey = self._hotkey
        try:
            self._ptt.start(hotkey, self._on_ptt_down, self._on_ptt_up)
            self._ptt_armed = True
        except PttUnavailable as exc:
            log.warning("push-to-talk unavailable: %s", exc.message)
            self._emit_error("ptt_unavailable", exc.message, exc.hint)
        else:
            # The mod sends config as soon as it sees hello, which can be while
            # start() is still importing pynput and before the backend can take
            # a new key: re-apply whatever is current now.
            with self._lock:
                if self._hotkey != hotkey:
                    self._ptt.set_hotkey(self._hotkey)

        try:
            self._capture.open()  # kept open so the 300 ms pre-roll is always there
        except AudioError as exc:
            self._emit_error(exc.code, exc.message, exc.hint)
        try:
            self._playback.open()
        except AudioError as exc:
            self._emit_error(exc.code, exc.message, exc.hint)

        self._load_stt(self._stt_requested)

    def request_exit(self, code: int = 0) -> None:
        with self._lock:
            if self._exit_code is not None:
                return
            self._exit_code = code
        self._actions.put(_EXIT)

    def run(self) -> int:
        """Serve the action queue until an exit is requested; returns the exit code."""
        while True:
            try:
                action = self._actions.get(timeout=0.2)
            except queue.Empty:
                action = None
            if action is _EXIT:
                break
            if action is not None:
                try:
                    action()
                except Exception as exc:
                    log.exception("daemon action failed")
                    self._emit_error("internal", f"{type(exc).__name__}: {exc}")
            self._tick()
        self._shutdown_components()
        return self._exit_code or 0

    def _shutdown_components(self) -> None:
        self._stopping.set()
        self.watchdog.stop()
        for step in (self._ptt.stop, self.pipeline.close, self._capture.close, self._playback.close):
            try:
                step()
            except Exception:
                log.debug("shutdown step failed", exc_info=True)
        self._clips.put(None)

    def _call_in_loop(self, fn: Callable[[], dict[str, Any]], timeout: float = 5.0) -> dict[str, Any]:
        future: concurrent.futures.Future[dict[str, Any]] = concurrent.futures.Future()

        def runner() -> None:
            try:
                future.set_result(fn())
            except BaseException as exc:  # noqa: BLE001 - surfaced to the caller
                future.set_exception(exc)

        self._actions.put(runner)
        return future.result(timeout)

    def _tick(self) -> None:
        if self._listen_source is not None and self._capture.recording_seconds() >= self.config.max_record_s:
            log.warning("recording hit %.0fs; stopping (was the key released?)", self.config.max_record_s)
            self._end_listening()

    # ------------------------------------------------------------------ state

    def _compute_state(self) -> protocol.HelperState:
        if self._listen_source is not None:
            return "listening"
        if self._transcribing:
            return "transcribing"
        if self._speaking:
            return "speaking"
        return self._base

    @property
    def state(self) -> protocol.HelperState:
        with self._lock:
            return self._compute_state()

    def _refresh_state(self) -> None:
        with self._lock:  # emit under the lock so state events keep their order
            state = self._compute_state()
            if state != self._last_state:
                self._last_state = state
                self._events.emit(protocol.State(state=state))

    def _on_speaking(self, speaking: bool) -> None:
        with self._lock:
            self._speaking = speaking
        self._refresh_state()

    def _emit_error(self, code: protocol.ErrorCode, message: str, hint: str | None = None, fatal: bool = False) -> None:
        self._events.emit(protocol.Error(code=code, message=message, hint=hint, fatal=fatal))

    def _chime(self, kind: str) -> None:
        if not self.config.chimes:
            return
        try:
            self._playback.open()
            sr = self._playback.samplerate
            tone = {"start": chimes.listen_start, "stop": chimes.listen_stop, "error": chimes.error}[kind](sr)
            self._playback.add_effect(tone)
        except AudioError as exc:
            log.debug("chime skipped: %s", exc.message)

    # ------------------------------------------------------------------ push-to-talk

    def _on_ptt_down(self) -> None:  # keyboard-hook thread: enqueue only
        self._actions.put(lambda: self._begin_listening("ptt"))

    def _on_ptt_up(self) -> None:
        self._actions.put(lambda: self._end_listening(only_source="ptt"))

    def _begin_listening(self, source: protocol.UtteranceSource) -> dict[str, Any]:
        with self._lock:
            if self._listen_source is not None:
                return {"ok": True, "already": True}
            stt_error = self._stt_error if self._transcriber is None else None
        if stt_error is not None:
            self._emit_error(stt_error.code, stt_error.message, stt_error.hint)
            self._chime("error")
            return protocol.error_response(stt_error.code, stt_error.message)

        # Barge-in: talking over Jarvis stops him (outside our lock: see speech.py).
        self.pipeline.stop("push-to-talk", barge_in=True)

        try:
            self._open_capture()
        except AudioError as exc:
            self._emit_error(exc.code, exc.message, exc.hint)
            self._chime("error")
            return protocol.error_response(exc.code, exc.message)

        self._playback.set_paused(True)  # hold any new speech while the user talks
        self._capture.begin()
        self._chime("start")
        with self._lock:
            self._listen_source = source
        self._refresh_state()
        return {"ok": True}

    def _open_capture(self) -> None:
        try:
            self._capture.open()
        except AudioError as exc:
            if exc.code != "no_input_device" or self._rescan_audio is None:
                raise
            # The headset may have been re-plugged: rescan devices and try once more.
            log.info("rescanning audio devices after: %s", exc.message)
            self._capture.close()
            self._playback.close()
            self._rescan_audio()
            self._capture.open()

    def _end_listening(self, only_source: protocol.UtteranceSource | None = None) -> dict[str, Any]:
        with self._lock:
            source = self._listen_source
            if source is None or (only_source is not None and source != only_source):
                return {"ok": True, "already": True}
            self._listen_source = None
        self._playback.set_paused(False)
        try:
            pcm = self._capture.end()
        except AudioError as exc:  # e.g. the headset was switched off mid-sentence
            self._emit_error(exc.code, exc.message, exc.hint)
            self._chime("error")
            self._refresh_state()
            return protocol.error_response(exc.code, exc.message)
        self._chime("stop")
        queued = False
        duration_s = pcm.size / STT_SAMPLERATE
        if duration_s < self.config.min_clip_s:
            log.info("ignoring %.0f ms clip (too short)", duration_s * 1000)
        elif is_digital_silence(pcm):
            err = digital_silence_error()
            self._emit_error(err.code, err.message, err.hint)
        elif is_silent(pcm):
            log.info("ignoring silent %.1fs clip", duration_s)
        else:
            with self._lock:
                self._transcribing += 1
            self._clips.put(_Clip(pcm, source, round(duration_s * 1000)))
            queued = True
        self._refresh_state()
        if queued:
            self.pipeline.prewarm()  # a reply is coming: connect to the voice service meanwhile
        return {"ok": True}

    # ------------------------------------------------------------------ speech-to-text

    def _load_stt(self, requested: str) -> None:
        with self._lock:
            self._stt_generation += 1
            generation = self._stt_generation
            self._stt_requested = requested
        thread = threading.Thread(target=self._stt_loader, args=(generation, requested), name="stt-load", daemon=True)
        thread.start()

    def _stt_loader(self, generation: int, requested: str) -> None:
        transcriber: Transcriber | None = None
        error: SttError | None = None
        try:
            transcriber = self._transcriber_factory(requested)
            transcriber.load()
        except SttError as exc:
            error = exc
        except Exception as exc:
            log.exception("speech model failed to load")
            error = SttError("stt_failed", f"speech model failed to load: {exc}")
        with self._lock:
            if generation != self._stt_generation:
                return  # superseded by a newer config
            if error is None:
                self._transcriber, self._stt_error, self._base = transcriber, None, "sleeping"
            else:
                self._stt_error = error
                if self._transcriber is None:
                    self._base = "error"
            self._stt_settled.set()
        if error is not None:
            self._emit_error(error.code, error.message, error.hint)
            self._refresh_state()
            return
        self._refresh_state()
        self._emit_ready()

    def _emit_ready(self, *, only_if_sent: bool = False) -> None:
        """Announce the loaded model and current key/voice (again, after config changed them)."""
        with self._lock:  # under the lock so a concurrent config change is never announced stale
            transcriber = self._transcriber
            if transcriber is None or (only_if_sent and not self._ready_sent):
                return
            self._ready_sent = True
            self._events.emit(
                protocol.Ready(
                    stt_model=transcriber.name,
                    stt_device=transcriber.device,
                    ptt_key=self._hotkey.text,
                    voice_id=self._voice_id,
                )
            )

    def _stt_worker(self) -> None:
        while True:
            clip = self._clips.get()
            if clip is None:
                return
            try:
                self._stt_settled.wait(self.config.stt_wait_s)
                with self._lock:
                    transcriber, stt_error = self._transcriber, self._stt_error
                if transcriber is None:
                    err = stt_error or SttError("stt_failed", "the speech model is still loading")
                    self._emit_error(err.code, err.message, err.hint)
                    continue
                result = transcriber.transcribe(clip.pcm, self._language)
                text = result.text.strip()
                if text:
                    self._events.emit(
                        protocol.Utterance(
                            id=f"utt-{uuid.uuid4().hex[:12]}",
                            text=text,
                            source=clip.source,
                            duration_ms=clip.duration_ms,
                            language=result.language,
                        )
                    )
                else:
                    log.info("no words in %d ms clip", clip.duration_ms)
            except SttError as exc:
                self._emit_error(exc.code, exc.message, exc.hint)
            except Exception as exc:
                log.exception("transcription crashed")
                self._emit_error("stt_failed", f"transcription crashed: {exc}")
            finally:
                with self._lock:
                    self._transcribing -= 1
                self._refresh_state()

    # ------------------------------------------------------------------ levels

    def _level_loop(self) -> None:
        period = 1.0 / max(1.0, min(self.config.level_hz, 15.0))
        while not self._stopping.wait(period):
            if self.state not in ("listening", "speaking"):
                continue
            mic = min(1.0, max(0.0, float(self._capture.level)))
            out = min(1.0, max(0.0, float(self._playback.level)))
            self._events.emit(protocol.Level(mic=round(mic, 3), out=round(out, 3)))

    # ------------------------------------------------------------------ commands

    def handle_command(self, name: str, body: dict[str, Any]) -> dict[str, Any]:
        """Entry point for the control server (already authenticated and schema-valid)."""
        try:
            return self._dispatch(name, body)
        except Exception as exc:
            log.exception("command %s failed", name)
            message = f"{name} failed: {type(exc).__name__}: {exc}"
            self._emit_error("internal", message)
            return protocol.error_response("internal", message)

    def _dispatch(self, name: str, body: dict[str, Any]) -> dict[str, Any]:
        if name == "heartbeat":
            self.watchdog.beat()
            return {"ok": True}
        if name == "speak":
            return self.pipeline.speak(body["replyId"], body["seq"], body["text"], body["final"])
        if name == "stop":
            result = self.pipeline.stop(body.get("reason"))
            return {"ok": True, "stopped": result is not None}
        if name == "listen":
            action = body["action"]
            return self._call_in_loop(
                (lambda: self._begin_listening("command")) if action == "start" else (lambda: self._end_listening())
            )
        if name == "config":
            return self._apply_config(body)
        if name == "status":
            return self._status()
        if name == "test_voice":
            if not self.pipeline.synth_configured:
                message = f"No Fish Audio API key is configured. {FISH_KEY_HINT}"
                return protocol.error_response("fish_key_missing", message)
            reply_id = self.pipeline.speak_now((body.get("text") or "").strip() or DEFAULT_TEST_LINE)
            return {"ok": True, "replyId": reply_id}
        if name == "shutdown":
            # Answer first, then exit.
            threading.Timer(0.1, self.request_exit, args=(0,)).start()
            return {"ok": True}
        return protocol.error_response("bad_request", f"unknown command {name!r}")

    def _apply_config(self, body: dict[str, Any]) -> dict[str, Any]:
        """Apply every valid field; a bad pttKey is reported but does not void the rest."""
        hotkey: Hotkey | None = None
        key_error: str | None = None
        if "pttKey" in body:
            try:
                hotkey = parse_hotkey(body["pttKey"])
            except ValueError as exc:
                key_error = f"invalid push-to-talk key {body['pttKey']!r}: {exc}"
        applied: list[str] = []
        with self._lock:
            announced = (self._hotkey, self._voice_id)
            if "voiceId" in body:
                self._voice_id = body["voiceId"].strip() or None
                applied.append("voiceId")
            if "language" in body:
                self._language = body["language"].strip() or None
                applied.append("language")
            if hotkey is not None:
                self._hotkey = hotkey
                self._ptt.set_hotkey(hotkey)  # under the lock so concurrent configs apply in order
                applied.append("pttKey")
            changed = (self._hotkey, self._voice_id) != announced
        model = (body.get("sttModel") or "").strip()
        if model and model != self._stt_requested:
            self._load_stt(model)  # announces ready again once loaded
            applied.append("sttModel")
        if changed:
            self._emit_ready(only_if_sent=True)  # the mod shows ready.pttKey: keep it current
        if key_error is not None:
            self._emit_error("bad_request", key_error, KEY_HINT)
            response = protocol.error_response("bad_request", key_error)
            response["applied"] = applied
            return response
        return {"ok": True, "applied": applied}

    def _status(self) -> dict[str, Any]:
        reply_id = self.pipeline.active_reply_id()  # before taking our lock (lock order)
        with self._lock:
            transcriber = self._transcriber
            status: dict[str, Any] = {
                "ok": True,
                "state": self._compute_state(),
                "version": __version__,
                "platform": self._platform,
                "sttModel": transcriber.name if transcriber else self._stt_requested,
                "sttDevice": transcriber.device if transcriber else None,
                "inputDevice": self._capture.device_name,
                "outputDevice": self._playback.device_name,
                "voiceId": self._voice_id,
                "pttKey": self._hotkey.text,
                "fishKeySet": self._synth.configured,
                "pttArmed": self._ptt_armed,
                "sttRequested": self._stt_requested,
                "language": self._language,
                "speakingReplyId": reply_id,
            }
        return {k: v for k, v in status.items() if v is not None}
