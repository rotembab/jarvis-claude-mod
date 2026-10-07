"""The helper's state machine, wiring capture, STT, TTS, playback, push-to-talk and the listener.

States: starting -> sleeping (model loaded; ``ready``) -> listening (PTT held,
or the listener heard the wake word; chime, capture with pre-roll) ->
transcribing -> sleeping (``utterance`` if the clip had words) ... speaking
(first audio of a reply) -> awake (a few seconds to follow up without the
wake word) -> sleeping. "error" replaces sleeping while no speech model is
usable.

Threads: push-to-talk callbacks, the ``listen`` command and the listener's
decisions are funnelled into one action queue served by ``run()`` (the main
thread), so listening transitions never race and the keyboard hook returns
immediately (Windows silently unhooks slow low-level hooks). Transcription
runs on its own worker; speech on the pipeline's worker; the wake word, voice
activity and echo cancelling on the listener's thread.
"""

from __future__ import annotations

import concurrent.futures
import logging
import queue
import re
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np

from . import __version__, protocol
from . import platform as plat
from .audio import chimes
from .audio.capture import STT_SAMPLERATE, Capture
from .audio.echo import EchoCanceller
from .audio.errors import AudioError, digital_silence_error
from .audio.levels import is_digital_silence, is_silent
from .audio.playback import Playback
from .events import EventSink
from .lifecycle import HeartbeatWatchdog
from .listen.listener import BARGE_MODES, BargeMode, Kind, Listener, ListenerConfig
from .listen.models import PLAIN_WAKE_PHRASE, WAKE_PHRASE
from .listen.vad import VoiceDetector
from .listen.wakeword import WakeScorer
from .ptt.base import PttUnavailable, PushToTalk
from .ptt.keys import Hotkey, parse_hotkey
from .speech import SpeechPipeline
from .stt.base import SttError, Transcriber
from .tts.base import KEY_HINT as FISH_KEY_HINT
from .tts.base import SpeechSynth

log = logging.getLogger(__name__)

EchoState = Literal["on", "off", "loading", "unavailable"]
DEFAULT_TEST_LINE = "Good evening, sir. Voice systems are online and at your service."
KEY_HINT = "Use a key name like 'right ctrl', 'f13' or 'alt+space' in the Jarvis plugin settings."
WAKE_HINT = "Run /jarvis setup to download it. Push-to-talk still works."
PLAIN_WAKE_HINT = f'Run /jarvis setup to download it (voice.log says what went wrong). "{WAKE_PHRASE}" still works.'
# The canceller loaded and ran, then failed: reinstalling (the load-failure hint) would not help.
ECHO_STOPPED_HINT = (
    "Jarvis now hears the microphone as it is. /jarvis restart tries again; meanwhile, with speakers, "
    "/jarvis bargein wake stops Jarvis interrupting himself."
)
_EXIT = object()
# "Hey Jarvis," at the start of a hands-free transcript is the wake word itself, not the request.
_WAKE_PREFIX = re.compile(r"^\W*(?:(?:hey|hi|hello|ok|okay|a)\W+)?jarvis\b\W*", re.IGNORECASE)
# A "Hey Jarvis" clip whose pre-roll reached back to where the speech began (at
# the start of an utterance, while plain "Jarvis" is on) can also keep a word or
# two from before it: "So, hey Jarvis, ...", "Um, hey Jarvis" lose those too. A
# greeting must sit between them and "Jarvis", so "Tell Jarvis to wait" stays
# whole. Every other clip keeps the narrow prefix, as before plain "Jarvis": the
# short pre-roll holds no words from before "Hey Jarvis", and in follow-ups and
# barge-ins words before "Jarvis" are the request ("No, it's ok Jarvis, I've got it.").
_WOKEN_PREFIX = re.compile(
    r"^\W*(?:"
    r"(?:[a-z']{1,7}\W+){0,2}?(?:hey|hi|hello|ok|okay)\W+"  # "So, hey Jarvis"
    r"|(?:(?:hey|hi|hello|ok|okay|a)\W+){0,2}"  # "Jarvis", "Hey Jarvis", "Okay, hey Jarvis"
    r")jarvis\b\W*",
    re.IGNORECASE,
)
# The first word of a transcript, after the same greetings.
_FIRST_WORD = re.compile(r"^\W*(?:(?:hey|hi|hello|ok|okay|a)\W+){0,2}(\w+)\b\W*", re.IGNORECASE)


def strip_wake_phrase(text: str, *, woken: bool = False) -> str:
    """Drop "Hey Jarvis" from the start of a hands-free transcript.

    ``woken``: the wake word started the clip and its pre-roll reached back to where the speech began.
    """
    return (_WOKEN_PREFIX if woken else _WAKE_PREFIX).sub("", text, count=1).strip()


def _within_one_edit(a: str, b: str) -> bool:
    if len(a) > len(b):
        a, b = b, a
    if len(b) - len(a) > 1:
        return False
    i = 0
    while i < len(a) and a[i] == b[i]:
        i += 1
    return a[i + 1 :] == b[i + 1 :] if len(a) == len(b) else a[i:] == b[i + 1 :]


def strip_plain_wake(text: str) -> str | None:
    """The request after a plain "Jarvis" that opens a transcript; None when it does not open with the name.

    Plain "Jarvis" wakes on sound-alikes too ("Travis", "service"), and the
    transcript shows which it was. A first word one slip away that still
    starts with "j" ("Jarvas") counts. "Jarvis" is deliberately not a hotword
    for Whisper: that pulls "Travis" toward it.
    """
    match = _FIRST_WORD.match(text)
    if match is None:
        return None
    word = match.group(1).lower()
    if not (word.startswith("j") and _within_one_edit(word, "jarvis")):
        return None
    return text[match.end() :].strip()


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
    wake_word: bool = True
    barge_in: BargeMode = "speech"
    wake_threshold: float = 0.5
    plain_wake: bool = False  # plain "Jarvis" as well as "Hey Jarvis"
    follow_up_s: float = 8.0


@dataclass(frozen=True, slots=True)
class _Clip:
    pcm: np.ndarray
    source: protocol.UtteranceSource
    duration_ms: int
    # What started a hands-free clip ("wake", "jarvis", "follow", "barge"); None for
    # push-to-talk. A plain "Jarvis" clip is kept only if the transcript starts with it.
    kind: Kind | None = None
    # Its pre-roll reaches back to where the speech began (Listener.long_preroll).
    long_preroll: bool = False


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
        vad: VoiceDetector | None = None,
        wake_loader: Callable[[], WakeScorer] | None = None,
        plain_loader: Callable[[WakeScorer], None] | None = None,
        echo_loader: Callable[[], EchoCanceller] | None = None,
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
        self._awake = False  # the follow-up window is open
        self._wake_loader = wake_loader
        # Attaches plain "Jarvis" to a loaded scorer that lacks it (fetching the
        # model first); raises when it cannot.
        self._plain_loader = plain_loader
        self._wake_scorer: WakeScorer | None = None
        self._plain_loading = False
        self._plain_reported: WakeScorer | None = None  # the scorer whose missing plain model was reported
        # Echo cancelling only cleans what the listener hears: without one there is nothing to load.
        self._echo_loader = echo_loader if vad is not None else None
        self._echo: EchoCanceller | None = None
        self._echo_state: EchoState = "loading" if self._echo_loader is not None else "off"

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
            on_reply_done=self._on_reply_done,
        )
        self._threads: list[threading.Thread] = []
        self.listener: Listener | None = None
        if vad is not None:
            self.listener = Listener(
                ListenerConfig(
                    wake_threshold=config.wake_threshold,
                    plain_wake_threshold=config.wake_threshold,
                    follow_up_s=config.follow_up_s,
                ),
                vad=vad,
                wake=None,  # loaded in the background by start()
                on_start=self._on_listener_start,
                on_end=self._on_listener_end,
                on_follow_up=self._on_follow_up,
                on_digital_silence=self._on_digital_silence,
                audio_playing=lambda: not self._playback.speech_idle(),
                wake_enabled=config.wake_word,
                barge_mode=config.barge_in,
                plain_wake=config.plain_wake,
            )

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

        if self.listener is not None:
            self.listener.start()
            self._capture.set_listener(self.listener.feed)
            if self._wake_loader is not None:
                thread = threading.Thread(target=self._load_wake, name="wake-load", daemon=True)
                thread.start()
            if self._echo_loader is not None:
                thread = threading.Thread(target=self._load_echo, name="aec-load", daemon=True)
                thread.start()
        try:
            self._capture.open()  # kept open: the pre-roll, and the listener, need it
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
        with self._lock:  # with the echo canceller's loader, which checks it before wiring one in
            self._stopping.set()
            echo, self._echo = self._echo, None
        self.watchdog.stop()
        steps: list[Callable[[], None]] = [self._ptt.stop]
        if echo is not None:
            steps.append(lambda: self._playback.set_far_listener(None))  # the output stops feeding it
        if self.listener is not None:
            steps.append(self.listener.close)
        if echo is not None:
            steps.append(echo.close)  # once the listener's thread, its other user, is done
        close_synth = getattr(self._synth, "close", None)  # the local voice owns a child process
        if callable(close_synth):
            steps.append(close_synth)
        steps += [self.pipeline.close, self._capture.close, self._playback.close]
        for step in steps:
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
        if self._awake and self._base == "sleeping":
            return "awake"
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
        if self.listener is not None:
            self.listener.set_speaking(speaking)
        with self._lock:
            self._speaking = speaking
        self._refresh_state()

    def _on_reply_done(self, reply_id: str, interrupted: bool, more_queued: bool) -> None:
        """A reply finished playing: give the user a moment to follow up without the wake word."""
        if self.listener is None or interrupted or more_queued or reply_id.startswith("test-"):
            return
        self.listener.open_follow_up()

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

        if self.listener is not None:
            self.listener.pause()
        self._playback.set_paused(True)  # hold any new speech while the user talks
        self._capture.begin()
        self._chime("start")
        with self._lock:
            self._listen_source = source
            self._awake = False
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
            if source is None or source == "wake" or (only_source is not None and source != only_source):
                return {"ok": True, "already": True}
            self._listen_source = None
        if self.listener is not None:
            self.listener.resume()
        self._playback.set_paused(False)
        try:
            pcm = self._capture.end()
        except AudioError as exc:  # e.g. the headset was switched off mid-sentence
            self._emit_error(exc.code, exc.message, exc.hint)
            self._chime("error")
            self._refresh_state()
            return protocol.error_response(exc.code, exc.message)
        self._chime("stop")
        return self._queue_clip(pcm, source)

    def _queue_clip(
        self,
        pcm: np.ndarray,
        source: protocol.UtteranceSource,
        *,
        kind: Kind | None = None,
        long_preroll: bool = False,
    ) -> dict[str, Any]:
        queued = False
        duration_s = pcm.size / STT_SAMPLERATE
        if duration_s < self.config.min_clip_s:
            log.info("ignoring %.0f ms clip (too short)", duration_s * 1000)
        elif is_digital_silence(pcm) and not self._mic_heard_sound():
            err = digital_silence_error()
            self._emit_error(err.code, err.message, err.hint)
        elif is_silent(pcm):
            log.info("ignoring silent %.1fs clip", duration_s)
        else:
            with self._lock:
                self._transcribing += 1
            self._clips.put(_Clip(pcm, source, round(duration_s * 1000), kind, long_preroll))
            queued = True
        self._refresh_state()
        if queued:
            self.pipeline.prewarm()  # a reply is coming: connect to the voice service meanwhile
        return {"ok": True}

    # ------------------------------------------------------------------ hands-free

    def _load_wake(self) -> None:
        assert self._wake_loader is not None and self.listener is not None
        try:
            scorer = self._wake_loader()
        except Exception as exc:
            log.warning("wake word unavailable: %s", exc, exc_info=True)
            self._emit_error("wake_unavailable", f"The wake word model could not be loaded: {exc}", WAKE_HINT)
            return
        with self._lock:
            self._wake_scorer = scorer
        self.listener.set_wake_scorer(scorer)
        log.info("wake word ready: %s (plain Jarvis: %s)", scorer.name, scorer.plain_name or "not loaded")
        self._emit_ready(only_if_sent=True)  # the mod learns that "Hey Jarvis" works now
        self._want_plain_wake()

    def _want_plain_wake(self) -> None:
        """If plain "Jarvis" is on but the loaded wake word lacks its model, fetch it, without holding anything up."""
        listener = self.listener
        with self._lock:
            scorer = self._wake_scorer
            if listener is None or scorer is None or not listener.plain_enabled or scorer.plain_name is not None:
                return
            if self._plain_loader is None:
                fetch = False
            elif self._plain_loading:
                return
            else:
                self._plain_loading = fetch = True
        if not fetch:
            self._report_missing_plain_wake(scorer)
            return
        thread = threading.Thread(target=self._load_plain_wake, args=(scorer,), name="plain-wake-load", daemon=True)
        thread.start()

    def _load_plain_wake(self, scorer: WakeScorer) -> None:
        assert self._plain_loader is not None and self.listener is not None
        try:
            reason: str | None = None
            try:
                self._plain_loader(scorer)
            except Exception as exc:
                log.warning('plain "Jarvis" wake word unavailable: %s', exc)
                reason = str(exc)
            if scorer.plain_name is not None:
                log.info("plain Jarvis ready: %s", scorer.plain_name)
                if self.listener.plain_ready:
                    self._emit_ready(only_if_sent=True)  # the mod learns that plain "Jarvis" works now
            elif self.listener.plain_enabled:
                self._report_missing_plain_wake(scorer, reason)
        finally:
            with self._lock:
                self._plain_loading = False

    def _report_missing_plain_wake(self, scorer: WakeScorer, reason: str | None = None) -> None:
        """Plain "Jarvis" is switched on, but its model could not be loaded: say so once for this wake word."""
        with self._lock:
            if self._plain_reported is scorer:
                return
            self._plain_reported = scorer
        because = f" ({reason})" if reason else ""
        message = (
            f'The plain "{PLAIN_WAKE_PHRASE}" wake word model could not be loaded{because}, '
            f'so only "{WAKE_PHRASE}" wakes Jarvis.'
        )
        self._emit_error("wake_unavailable", message, PLAIN_WAKE_HINT)

    def _load_echo(self) -> None:
        """Until this has loaded (or if it cannot), the listener hears the microphone as it is."""
        assert self._echo_loader is not None and self.listener is not None
        try:
            echo = self._echo_loader()
        except Exception as exc:
            log.warning("echo cancelling unavailable: %s", exc, exc_info=True)
            with self._lock:
                self._echo_state = "unavailable"
            message = f"Echo cancelling could not be started: {exc}"
            self._emit_error("aec_unavailable", message, plat.current().echo_cancel_hint())
            return
        with self._lock:  # the lock shutdown takes: a canceller that arrives as the helper stops is freed
            stopping = self._stopping.is_set()
            if not stopping:
                self._echo, self._echo_state = echo, "on"
                # Called on the listener's thread: enqueue only.
                echo.set_on_failed(lambda exc: self._actions.put(lambda: self._echo_failed(echo, exc)))
                self._playback.set_far_listener(echo.far)
                self.listener.set_echo(echo)
        if stopping:
            echo.close()
            return
        log.info("echo cancelling ready")

    def _echo_failed(self, echo: EchoCanceller, exc: Exception) -> None:
        """The canceller stopped working mid-session and passes the microphone through: unhook it and say so."""
        with self._lock:
            if self._echo is not echo or self._stopping.is_set():
                return
            self._echo, self._echo_state = None, "unavailable"
            self._playback.set_far_listener(None)
            if self.listener is not None:
                self.listener.set_echo(None)
        echo.close()
        message = f"Echo cancelling stopped working: {exc}"
        self._emit_error("aec_unavailable", message, ECHO_STOPPED_HINT)

    def _on_listener_start(self, kind: Kind) -> None:  # listener thread: enqueue only
        self._actions.put(lambda: self._begin_hands_free(kind))

    def _on_listener_end(self, pcm: np.ndarray | None, kind: Kind) -> None:  # listener thread
        # Read here, on the listener's thread: its next utterance sets it afresh.
        long_preroll = self.listener is not None and self.listener.long_preroll
        self._actions.put(lambda: self._end_hands_free(pcm, kind, long_preroll))

    def _begin_hands_free(self, kind: Kind) -> None:
        assert self.listener is not None
        with self._lock:
            busy = self._listen_source is not None
            stt_error = self._stt_error if self._transcriber is None else None
        if busy:
            self.listener.cancel()
            return
        if stt_error is not None:
            self.listener.cancel()
            self._emit_error(stt_error.code, stt_error.message, stt_error.hint)
            self._chime("error")
            return
        if kind == "jarvis" and not self._playback.speech_idle():
            # The listener takes plain "Jarvis" only while Jarvis is quiet; if he
            # has become audible since, a sound-alike must not cut him off.
            self.listener.cancel()
            return
        # Only cut Jarvis off when he is audible: a reply that is open but quiet
        # (Claude is running a tool) keeps going, and the mod queues the new words.
        woken = kind in ("wake", "jarvis")
        if kind == "barge" or not self._playback.speech_idle():
            self.pipeline.stop("wake word" if woken else "voice", barge_in=True)
        self._playback.set_paused(True)
        if woken:
            self._chime("start")
        with self._lock:
            self._listen_source = "wake"
            self._awake = False
        self._refresh_state()

    def _end_hands_free(self, pcm: np.ndarray | None, kind: Kind, long_preroll: bool = False) -> None:
        with self._lock:
            if self._listen_source != "wake":
                return
            self._listen_source = None
        self._playback.set_paused(False)
        self._chime("stop")
        if pcm is None:
            self._refresh_state()
            return
        self._queue_clip(pcm, "wake", kind=kind, long_preroll=long_preroll)

    def _on_follow_up(self, opened: bool) -> None:
        with self._lock:
            self._awake = opened
        self._refresh_state()

    def _mic_heard_sound(self) -> bool:
        """The microphone has delivered sound before, so exact zeros in a clip mean a noise gate, not a mute."""
        return self.listener is not None and self.listener.heard_sound

    def _on_digital_silence(self, silent: bool) -> None:
        if silent:
            err = digital_silence_error()
            self._emit_error(err.code, err.message, err.hint)
        else:
            log.info("the microphone is delivering sound again")

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
                    wake_phrase=self._wake_phrase(),
                    barge_in=self.listener.barge_mode if self.listener is not None else None,
                )
            )

    def _wake_phrase(self) -> str | None:
        listener = self.listener
        if listener is None or not listener.wake_ready:
            return None
        return PLAIN_WAKE_PHRASE if listener.plain_ready else WAKE_PHRASE

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
                if clip.kind == "jarvis":
                    request = strip_plain_wake(text)
                    if request is None:
                        log.info('dropped a %d ms clip: plain "Jarvis" woke on other words', clip.duration_ms)
                        continue
                    text = request
                elif clip.source == "wake":  # "Hey Jarvis", a follow-up or a barge-in
                    text = strip_wake_phrase(text, woken=clip.kind == "wake" and clip.long_preroll)
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
        listener = self.listener
        with self._lock:
            announced = (self._hotkey, self._voice_id, self._wake_phrase(), listener and listener.barge_mode)
        if listener is not None:
            if "wakeWord" in body:
                listener.set_wake_enabled(bool(body["wakeWord"]))
                applied.append("wakeWord")
            if "plainWake" in body:
                listener.set_plain_wake(bool(body["plainWake"]))
                applied.append("plainWake")
            if "bargeIn" in body and body["bargeIn"] in BARGE_MODES:
                listener.set_barge_mode(body["bargeIn"])
                applied.append("bargeIn")
            if "wakeThreshold" in body:
                listener.set_wake_threshold(float(body["wakeThreshold"]))
                applied.append("wakeThreshold")
        with self._lock:
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
            changed = (self._hotkey, self._voice_id, self._wake_phrase(), listener and listener.barge_mode) != announced
        model = (body.get("sttModel") or "").strip()
        if model and model != self._stt_requested:
            self._load_stt(model)  # announces ready again once loaded
            applied.append("sttModel")
        if changed:
            self._emit_ready(only_if_sent=True)  # the mod shows ready.pttKey: keep it current
        if body.get("plainWake"):
            self._want_plain_wake()
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
                "ttsEngine": getattr(self._synth, "engine", "fish"),
                "pttArmed": self._ptt_armed,
                "wakeWord": self._wake_phrase(),
                "bargeIn": self.listener.barge_mode if self.listener is not None else None,
                "echoCancel": self._echo_state,
                "sttRequested": self._stt_requested,
                "language": self._language,
                "speakingReplyId": reply_id,
            }
        return {k: v for k, v in status.items() if v is not None}
