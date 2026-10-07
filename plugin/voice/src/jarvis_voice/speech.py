"""Speech pipeline: ordered sentences per reply -> one TTS stream per reply -> playback.

The mod POSTs ``speak`` per sentence, possibly concurrently and so out of
order; sentences are released strictly by ``seq``. Replies play one after the
other. ``stop()`` silences the speakers first, then tears down the TTS
streams, and every reply always ends with exactly one ``speech_done``.

A reply whose ``final`` never arrives (lost POST, or a sentence that landed
after a stop) must not block the replies behind it: once a newer reply is
waiting and the open one has had no sentence for ``stale_after`` seconds, it
is closed as interrupted. A lone open reply is left alone, because a voice
turn may run tools for minutes between sentences. If queued audio stops
playing (dead output device), the reply ends with ``no_output_device``.

``prewarm()`` (called when the user has just spoken) opens the output and a
TTS stream ahead of the reply, so the first sentence skips the handshake; an
unused pre-opened stream is closed after ``warm_ttl`` seconds.

Locking: all reply state and every event this module emits are guarded by
``self._cond`` so events come out in a consistent order. Callbacks into the
daemon (``on_speaking``, ``on_reply_done``) run under that lock; the daemon must therefore never
call into the pipeline while holding its own lock.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import platform as plat
from . import protocol
from .audio.errors import AudioError
from .audio.playback import Playback
from .events import EventSink
from .tts.base import Pcm16Decoder, SpeechSynth, SynthError, SynthStream

log = logging.getLogger(__name__)

DEFAULT_CHARS_PER_SECOND = 14.0  # typical TTS speaking rate, used until the real rate is known
MAX_PENDING_PER_REPLY = 2000
MAX_QUEUED_REPLIES = 16
WARM_TTL_S = 20.0
WARM_CONNECT_WAIT_S = 10.0


def estimate_spoken_text(sentences: list[str], played_s: float, chars_per_second: float) -> str:
    """Sentences that had started playing after ``played_s`` seconds of audio.

    Audio arrives without sentence boundaries, so we place each sentence on
    the timeline by its length at the given speaking rate. A sentence counts
    once ~150 ms of it has played (the user has heard it begin).
    """
    spoken: list[str] = []
    start = 0.0
    rate = max(chars_per_second, 1e-3)
    for sentence in sentences:
        if played_s - start < 0.15:
            break
        spoken.append(sentence)
        start += len(sentence) / rate
    return " ".join(spoken)


class _AudioRelay:
    """Audio callback of a pre-opened stream: forwards to the reply that adopts it."""

    def __init__(self) -> None:
        self.target: Callable[[bytes], None] | None = None

    def __call__(self, chunk: bytes) -> None:
        target = self.target
        if target is not None:
            target(chunk)


@dataclass(eq=False)
class _Warm:
    stream: SynthStream
    relay: _AudioRelay
    voice_id: str | None
    opened_at: float


@dataclass(eq=False)
class _Reply:
    id: str
    next_seq: int = 0
    pending: dict[int, tuple[str, bool]] = field(default_factory=dict)
    ready: deque[tuple[str, bool]] = field(default_factory=deque)
    final_queued: bool = False
    final_seen: bool = False
    closed_early: bool = False  # closed without its final (see _close_if_stale_locked)
    last_input: float = field(default_factory=time.monotonic)  # when a sentence last arrived
    gap_since: float | None = None
    sentences: list[str] = field(default_factory=list)
    stream: SynthStream | None = None
    generation: int = 0
    started: bool = False
    finished: bool = False
    cancelled: bool = False
    reported: set[str] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class StopResult:
    reply_id: str
    spoken_text: str


class SpeechPipeline:
    def __init__(
        self,
        synth: SpeechSynth,
        playback: Playback,
        events: EventSink,
        *,
        voice_id: Callable[[], str | None] = lambda: None,
        on_speaking: Callable[[bool], None] = lambda speaking: None,
        on_reply_done: Callable[[str, bool, bool], None] = lambda reply_id, interrupted, more_queued: None,
        gap_timeout: float = 3.0,
        stale_after: float = 3.0,
        stall_timeout: float = 5.0,
        warm_ttl: float = WARM_TTL_S,
        poll: float = 0.01,
    ) -> None:
        self._synth = synth
        self._playback = playback
        self._events = events
        self._voice_id = voice_id
        self._on_speaking = on_speaking
        self._on_reply_done = on_reply_done
        self._gap_timeout = gap_timeout
        self._stale_after = stale_after
        self._stall_timeout = stall_timeout
        self._warm_ttl = warm_ttl
        self._poll = poll
        self._cond = threading.Condition()
        self._replies: dict[str, _Reply] = {}
        self._order: deque[str] = deque()
        self._closed_ids: OrderedDict[str, None] = OrderedDict()
        self._shutdown = False
        self._warm: _Warm | None = None
        self._warming = False
        self._thread = threading.Thread(target=self._worker, name="speech", daemon=True)

    # ------------------------------------------------------------------ public API

    def start(self) -> SpeechPipeline:
        self._thread.start()
        return self

    def close(self) -> None:
        self.stop("shutdown")
        with self._cond:
            self._shutdown = True
            warm, self._warm = self._warm, None
            self._cond.notify_all()
        if warm is not None:
            warm.stream.cancel()
        if self._thread.is_alive():
            self._thread.join(2.0)

    def busy(self) -> bool:
        with self._cond:
            return bool(self._order)

    def active_reply_id(self) -> str | None:
        with self._cond:
            return self._order[0] if self._order else None

    def speak(self, reply_id: str, seq: int, text: str, final: bool) -> dict[str, Any]:
        with self._cond:
            if reply_id in self._closed_ids:
                return {"ok": True, "dropped": True}
            reply = self._replies.get(reply_id)
            if reply is None:
                if len(self._order) >= MAX_QUEUED_REPLIES:
                    return protocol.error_response("bad_request", "too many queued replies")
                reply = _Reply(reply_id)
                self._replies[reply_id] = reply
                self._order.append(reply_id)
            if reply.final_queued or seq < reply.next_seq or seq in reply.pending:
                return {"ok": True, "dropped": True}  # duplicate, or after the final sentence
            if len(reply.pending) >= MAX_PENDING_PER_REPLY:
                return protocol.error_response("bad_request", "too many out-of-order sentences")
            reply.pending[seq] = (text, final)
            reply.last_input = time.monotonic()
            self._advance(reply)
            self._cond.notify_all()
        return {"ok": True}

    @property
    def synth_configured(self) -> bool:
        return bool(self._synth.configured)

    def prewarm(self) -> None:
        """A reply is probably coming: open the output and a TTS stream now, in the background."""
        if not self.synth_configured:
            return
        with self._cond:
            if self._shutdown or self._warming or self._warm is not None:
                return
            self._warming = True
        threading.Thread(target=self._open_warm, name="speech-prewarm", daemon=True).start()

    def speak_now(self, text: str) -> str:
        """Speak a one-off line (test_voice) as its own reply; returns the replyId."""
        reply_id = f"test-{uuid.uuid4().hex[:8]}"
        self.speak(reply_id, 0, text, True)
        return reply_id

    def stop(self, reason: str | None = None, *, barge_in: bool = False) -> StopResult | None:
        """Stop all speech now. Emits barge_in (if asked) then speech_done per reply."""
        with self._cond:
            if not self._order:
                return None
            replies = [self._replies[rid] for rid in self._order if rid in self._replies]
            for reply in replies:
                reply.cancelled = True
            # Silence first: the user must not wait for sockets to close.
            self._playback.stop_speech()
            active = replies[0]
            spoken = self._spoken_text(active, interrupted=True)
            log.info("speech stopped (%s); reply %s had spoken %r", reason or "stop", active.id, spoken[:80])
            if barge_in:
                self._events.emit(protocol.BargeIn(spoken_text=spoken, reply_id=active.id))
            for reply in replies:
                self._finish_locked(reply, interrupted=True, spoken=spoken if reply is active else "")
            self._cond.notify_all()
        for reply in replies:
            if reply.stream is not None:
                reply.stream.cancel()
        return StopResult(active.id, spoken)

    # ------------------------------------------------------------------ ordering

    def _advance(self, reply: _Reply) -> None:
        while reply.next_seq in reply.pending and not reply.final_queued:
            text, final = reply.pending.pop(reply.next_seq)
            reply.ready.append((text, final))
            reply.next_seq += 1
            if final:
                reply.final_queued = True
                reply.pending.clear()  # anything numbered after the final sentence is ignored
        if reply.pending:
            reply.gap_since = reply.gap_since or time.monotonic()
        else:
            reply.gap_since = None

    def _close_if_stale_locked(self, reply: _Reply) -> None:
        """Close an open reply whose final is evidently not coming because a newer one is waiting."""
        if reply.final_queued or reply.pending or reply.ready or len(self._order) < 2:
            return
        if time.monotonic() - reply.last_input < self._stale_after:
            return
        log.warning("reply %s: no final sentence and reply %s is waiting; closing it", reply.id, self._order[1])
        reply.final_queued = reply.closed_early = True
        reply.ready.append(("", True))  # plays out what is queued, then finishes

    def _skip_gap(self, reply: _Reply) -> None:
        if reply.pending and reply.gap_since is not None and time.monotonic() - reply.gap_since > self._gap_timeout:
            missing = reply.next_seq
            reply.next_seq = min(reply.pending)
            reply.gap_since = None
            log.warning("reply %s: sentence %d never arrived; skipping to %d", reply.id, missing, reply.next_seq)
            self._advance(reply)

    # ------------------------------------------------------------------ pre-opened stream

    def _open_warm(self) -> None:
        warm: _Warm | None = None
        try:
            try:
                self._playback.open()
            except AudioError:
                pass  # the reply reports it
            relay = _AudioRelay()
            voice = self._voice_id()
            try:
                stream = self._synth.open_stream(relay, voice_id=voice)
            except SynthError as exc:
                log.debug("could not pre-open the TTS stream: %s", exc.message)  # the reply reports it
                return
            warm = _Warm(stream, relay, voice, time.monotonic())
        finally:
            with self._cond:
                self._warming = False
                if warm is not None and not self._shutdown and self._warm is None:
                    self._warm, warm = warm, None
                self._cond.notify_all()
        if warm is not None:  # shut down meanwhile
            warm.stream.cancel()

    def _take_warm(self, on_audio: Callable[[bytes], None]) -> SynthStream | None:
        """The pre-opened stream, now feeding ``on_audio``; None when there is no fresh one."""
        with self._cond:
            # One still connecting gets there no later than a new connection would.
            self._cond.wait_for(lambda: not self._warming or self._shutdown, timeout=WARM_CONNECT_WAIT_S)
            warm, self._warm = self._warm, None
        if warm is None:
            return None
        fresh = time.monotonic() - warm.opened_at < self._warm_ttl
        if not fresh or warm.stream.closed or warm.stream.error is not None or warm.voice_id != self._voice_id():
            warm.stream.cancel()
            return None
        warm.relay.target = on_audio
        return warm.stream

    def _expire_warm(self) -> None:
        with self._cond:
            warm = self._warm
            if warm is None or (time.monotonic() - warm.opened_at < self._warm_ttl and not warm.stream.closed):
                return
            self._warm = None
        log.debug("closing an unused pre-opened TTS stream")
        warm.stream.cancel()

    # ------------------------------------------------------------------ worker

    def _worker(self) -> None:
        while True:
            self._expire_warm()
            with self._cond:
                if not self._shutdown and not self._order:
                    self._cond.wait(0.5)
                    continue
                if self._shutdown:
                    return
                reply = self._replies.get(self._order[0])
                if reply is None:  # defensive: order and map out of sync
                    self._order.popleft()
                    continue
            try:
                self._run_reply(reply)
            except Exception as exc:
                log.exception("speech worker failed on reply %s", reply.id)
                with self._cond:
                    self._events.emit(protocol.Error(code="internal", message=f"speech failed: {exc}"))
                    self._finish_locked(reply, interrupted=True)
                if reply.stream is not None:
                    reply.stream.cancel()

    def _run_reply(self, reply: _Reply) -> None:
        pb = self._playback
        with self._cond:
            if reply.cancelled or reply.finished:
                return
            pb.reset_speech_counters()
            reply.generation = pb.speech_generation
        idle_since: float | None = None
        flushed = False
        failure: SynthError | AudioError | None = None
        stalled = False
        last_played, stall_since = -1, 0.0
        while True:
            item: tuple[str, bool] | None = None
            with self._cond:
                if reply.cancelled or reply.finished:
                    return
                self._skip_gap(reply)
                self._close_if_stale_locked(reply)
                if not reply.ready:
                    self._cond.wait(self._poll)
                    if reply.cancelled or reply.finished:
                        return
                if reply.ready:
                    item = reply.ready.popleft()
            if item is not None:
                text, final = item
                if text.strip():
                    failure = self._send(reply, text.strip())
                    if failure is not None:
                        break
                if final:
                    reply.final_seen = True
                    if reply.stream is not None and not reply.stream.closed:
                        reply.stream.finish()
            # Progress checks.
            if not reply.started and pb.speech_frames_played > 0:
                with self._cond:
                    if not reply.cancelled and not reply.finished:
                        reply.started = True
                        self._events.emit(protocol.SpeechStarted(reply_id=reply.id))
                        self._on_speaking(True)
            stream = reply.stream
            if stream is not None and stream.error is not None:
                failure = stream.error
                break
            # Audio is queued and not held, yet nothing plays: the output device died.
            played = pb.speech_frames_played
            if played != last_played or pb.speech_idle() or pb.paused:
                last_played, stall_since = played, time.monotonic()
            elif time.monotonic() - stall_since > self._stall_timeout:
                failure = AudioError(
                    "no_output_device",
                    "The audio output stopped playing (was the headset disconnected?).",
                    plat.current().no_output_device_hint(),
                )
                stalled = True
                break
            if reply.final_seen and not reply.ready and (stream is None or stream.closed):
                if not flushed:
                    flushed = True
                    pb.end_speech(reply.generation)  # all audio is in: release resampler tails
                if pb.speech_idle():
                    idle_since = idle_since or time.monotonic()
                    # Let the device play out what it already buffered.
                    if time.monotonic() - idle_since >= float(getattr(pb, "output_latency", 0.0) or 0.0):
                        break
                else:
                    idle_since = None
        with self._cond:
            if reply.cancelled or reply.finished:
                return
            if failure is not None:
                self._report_locked(reply, failure)
                reply.cancelled = True
                pb.stop_speech()
            # A reply closed early is reported as interrupted, but it did play out all it was given.
            spoken = self._spoken_text(reply, interrupted=failure is not None)
            self._finish_locked(reply, interrupted=failure is not None or reply.closed_early, spoken=spoken)
        if failure is not None and reply.stream is not None:
            reply.stream.cancel()
        if stalled:
            pb.close()  # the next reply reopens the output, on whatever device is the default now

    def _send(self, reply: _Reply, text: str) -> SynthError | AudioError | None:
        stream = reply.stream
        if stream is None or (stream.closed and stream.error is None):
            # First sentence (or the service closed an idle socket): open a stream.
            try:
                self._playback.open()
            except AudioError as exc:
                return exc
            decoder = Pcm16Decoder()
            generation = reply.generation

            def on_audio(chunk: bytes) -> None:
                if not reply.cancelled:
                    self._playback.add_speech(decoder.feed(chunk), generation)

            stream = self._take_warm(on_audio)
            if stream is None:
                try:
                    stream = self._synth.open_stream(on_audio, voice_id=self._voice_id())
                except SynthError as exc:
                    return exc
            with self._cond:
                cancelled = reply.cancelled
                if not cancelled:
                    reply.stream = stream
            if cancelled:  # stop() raced with the connect
                stream.cancel()
                return None
        try:
            stream.send_text(text)
        except SynthError as exc:
            return exc
        with self._cond:
            reply.sentences.append(text)
        return None

    # ------------------------------------------------------------------ completion

    def _report_locked(self, reply: _Reply, error: SynthError | AudioError) -> None:
        if error.code in reply.reported:
            return
        reply.reported.add(error.code)
        log.warning("reply %s: %s (%s)", reply.id, error.message, error.code)
        self._events.emit(protocol.Error(code=error.code, message=error.message, hint=error.hint, fatal=False))

    def _spoken_text(self, reply: _Reply, *, interrupted: bool) -> str:
        if not interrupted:
            return " ".join(reply.sentences)
        if not reply.started:
            return ""
        pb = self._playback
        played_s = pb.speech_frames_played / pb.samplerate
        rate = DEFAULT_CHARS_PER_SECOND
        queued_s = pb.speech_frames_queued / pb.samplerate
        if reply.stream is not None and reply.stream.done and queued_s > 0:
            rate = sum(len(s) for s in reply.sentences) / queued_s  # exact: all audio has arrived
        return estimate_spoken_text(reply.sentences, played_s, rate)

    def _finish_locked(self, reply: _Reply, *, interrupted: bool, spoken: str | None = None) -> None:
        if reply.finished:
            return
        reply.finished = True
        if spoken is None:
            spoken = self._spoken_text(reply, interrupted=interrupted)
        self._replies.pop(reply.id, None)
        try:
            self._order.remove(reply.id)
        except ValueError:
            pass
        self._closed_ids[reply.id] = None
        while len(self._closed_ids) > 256:
            self._closed_ids.popitem(last=False)
        self._events.emit(protocol.SpeechDone(reply_id=reply.id, interrupted=interrupted, spoken_text=spoken))
        if reply.started:
            self._on_speaking(False)
            self._on_reply_done(reply.id, interrupted, bool(self._order))
        self._cond.notify_all()
