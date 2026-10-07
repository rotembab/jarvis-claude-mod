"""The listener: hands-free utterances from the always-open microphone.

The capture callback hands every block to ``feed()``, which only queues it.
The listener's thread resamples to 16 kHz and runs two small models on it:
the wake word (a score per 80 ms) and voice activity (a probability per
32 ms). From those it decides, on its own and without waiting for the
daemon, when an utterance starts and ends:

* **idle**: only the wake word counts. It starts an utterance ("wake"). While
  Jarvis is audibly speaking, in barge-in mode "speech", 200 ms of the user's
  voice also starts one ("barge"); in mode "wake" only the wake word does,
  and in mode "off" nothing does.
* **follow**: for a few seconds after Jarvis finished a reply, a quarter
  second of speech starts an utterance with no wake word ("follow").
* **record**: the utterance is being captured. It ends after 0.7 s of
  silence, once enough speech was heard. After the wake word, less than
  0.4 s of speech is taken for the tail of "Jarvis" itself, so a pause after
  "Hey Jarvis" doesn't end it; with no real speech within 5 s it is dropped.
* **paused**: push-to-talk owns the microphone.

A microphone that has sent nothing but exact zeros since the listener started
is reported once, after 30 s. Some headsets (a noise gate in the headset or
its software) send exact zeros whenever the user is quiet, so once any sound
has arrived, zeros mean quiet, not muted, and are never reported.

Utterances start with some audio from before their start (pre-roll), so the
first syllable is kept. Time is measured in audio, not on the wall clock, so
the decisions are the same however the blocks arrive.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np

from .vad import VoiceDetector
from .wakeword import WakeScorer

log = logging.getLogger(__name__)

RATE = 16_000
VAD_FRAME_S = 512 / RATE

Kind = Literal["wake", "follow", "barge"]
BargeMode = Literal["speech", "wake", "off"]
BARGE_MODES: tuple[BargeMode, ...] = ("speech", "wake", "off")
Mode = Literal["idle", "follow", "record", "paused"]


@dataclass
class ListenerConfig:
    wake_threshold: float = 0.5
    speech_threshold: float = 0.5  # Silero probability that counts as speech
    end_silence_s: float = 0.7
    min_speech_s: float = 0.4
    no_speech_s: float = 5.0
    max_utterance_s: float = 30.0
    follow_up_s: float = 8.0
    follow_onset_s: float = 0.25
    barge_onset_s: float = 0.2
    wake_preroll_s: float = 0.3
    onset_preroll_s: float = 0.5
    wake_refractory_s: float = 1.5
    silence_alarm_s: float = 30.0  # only exact zeros since the start for this long: muted at the source


def _noop(*_args: Any) -> None:
    return None


class Listener:
    def __init__(
        self,
        config: ListenerConfig,
        *,
        vad: VoiceDetector,
        wake: WakeScorer | None,
        on_start: Callable[[Kind], None],
        on_end: Callable[[np.ndarray | None, Kind], None],
        on_follow_up: Callable[[bool], None] = _noop,
        on_digital_silence: Callable[[bool], None] = _noop,
        audio_playing: Callable[[], bool] = lambda: False,
        wake_enabled: bool = True,
        barge_mode: BargeMode = "speech",
    ) -> None:
        self.config = config
        self._vad = vad
        self._wake = wake
        self._on_start = on_start
        self._on_end = on_end
        self._on_follow_up = on_follow_up
        self._on_digital_silence = on_digital_silence
        self._audio_playing = audio_playing
        self._wake_enabled = wake_enabled
        self._barge_mode: BargeMode = barge_mode

        self._lock = threading.Lock()
        self._mode: Mode = "idle"
        self._speaking = False
        self._t = 0.0  # seconds of audio processed
        self._follow_until = 0.0
        self._refractory_until = 0.0
        self._reset_models = False

        self._in_speech = False
        self._voiced_run = 0.0
        self._silence_run = 0.0
        self._zero_run = 0.0
        self._silence_alarm = False
        self._heard_sound = False

        self._ring: deque[np.ndarray] = deque()
        self._ring_s = 0.0
        self._ring_max_s = 2.0
        self._kind: Kind = "wake"
        self._clip: list[np.ndarray] = []
        self._rec_s = 0.0
        self._rec_voiced = 0.0
        self._min_speech = 0.0

        self._queue: queue.Queue[tuple[np.ndarray, int] | None] = queue.Queue(maxsize=1000)
        self._thread: threading.Thread | None = None
        self._resampler: Any = None
        self._resampler_rate = RATE
        self._dropped = 0

    # ------------------------------------------------------------------ state

    @property
    def mode(self) -> Mode:
        return self._mode

    @property
    def wake_ready(self) -> bool:
        """The wake word model is loaded and switched on."""
        return self._wake is not None and self._wake_enabled

    @property
    def wake_name(self) -> str | None:
        return self._wake.name if self._wake is not None else None

    @property
    def barge_mode(self) -> BargeMode:
        return self._barge_mode

    @property
    def heard_sound(self) -> bool:
        """The microphone has sent something other than exact zeros since the listener started."""
        return self._heard_sound

    def set_wake_scorer(self, wake: WakeScorer | None) -> None:
        with self._lock:
            self._wake = wake
            self._reset_models = True

    def set_wake_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._wake_enabled = enabled
            self._reset_models = True

    def set_barge_mode(self, mode: BargeMode) -> None:
        with self._lock:
            self._barge_mode = mode

    def set_wake_threshold(self, threshold: float) -> None:
        with self._lock:
            self.config.wake_threshold = min(0.95, max(0.05, threshold))

    def set_speaking(self, speaking: bool) -> None:
        with self._lock:
            self._speaking = speaking

    def open_follow_up(self) -> None:
        """Jarvis just finished a reply: listen for a follow-up without the wake word."""
        with self._lock:
            if self._mode not in ("idle", "follow") or self.config.follow_up_s <= 0:
                return
            opened = self._mode == "idle"
            self._mode = "follow"
            self._follow_until = self._t + self.config.follow_up_s
        if opened:
            self._on_follow_up(True)

    def pause(self) -> None:
        """Push-to-talk took the microphone: drop whatever was going on."""
        with self._lock:
            was = self._mode
            self._mode = "paused"
            self._clip = []
        if was == "follow":
            self._on_follow_up(False)

    def resume(self) -> None:
        with self._lock:
            if self._mode != "paused":
                return
            self._mode = "idle"
            self._reset_models = True

    def cancel(self) -> None:
        """Drop an utterance being recorded (the daemon could not take it)."""
        with self._lock:
            if self._mode == "record":
                self._mode = "idle"
                self._clip = []

    # ------------------------------------------------------------------ thread

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="listener", daemon=True)
            self._thread.start()

    def close(self) -> None:
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._thread is not None:
            self._thread.join(2.0)

    def feed(self, block: np.ndarray, rate: int) -> None:
        """Audio thread: queue the block and return."""
        try:
            self._queue.put_nowait((block, rate))
        except queue.Full:
            self._dropped += 1  # the listener fell behind; losing audio beats blocking the sound card

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            block, rate = item
            try:
                self.process(self._to_16k(block, rate))
            except Exception:
                log.exception("listener failed on a block")
            if self._dropped:
                log.warning("listener fell behind: dropped %d audio blocks", self._dropped)
                self._dropped = 0

    def _to_16k(self, block: np.ndarray, rate: int) -> np.ndarray:
        block = np.asarray(block, np.float32).reshape(-1)
        if rate == RATE:
            return block
        if self._resampler is None or self._resampler_rate != rate:
            import soxr

            self._resampler = soxr.ResampleStream(rate, RATE, 1, dtype="float32")
            self._resampler_rate = rate
        return np.asarray(self._resampler.resample_chunk(block), np.float32)

    # ------------------------------------------------------------------ audio

    def process(self, audio: np.ndarray) -> None:
        """Handle 16 kHz mono float audio (called on the listener thread, or directly by tests)."""
        if audio.size == 0:
            return
        seconds = audio.size / RATE
        self._watch_digital_silence(audio, seconds)
        with self._lock:
            self._t += seconds
            mode = self._mode
            reset, self._reset_models = self._reset_models, False
            wake = self._wake if self._wake_enabled else None
        if mode == "paused":
            return
        if reset:
            self._vad.reset()
            if wake is not None:
                wake.reset()
            self._ring.clear()
            self._ring_s = 0.0
            self._in_speech, self._voiced_run, self._silence_run = False, 0.0, 0.0

        self._ring.append(audio)
        self._ring_s += seconds
        while self._ring and self._ring_s - self._ring[0].size / RATE >= self._ring_max_s:
            self._ring_s -= self._ring.popleft().size / RATE

        voiced_now = 0.0
        for prob in self._vad.process(audio):
            threshold = self.config.speech_threshold
            self._in_speech = prob >= threshold or (self._in_speech and prob >= threshold - 0.15)
            if self._in_speech:
                self._voiced_run += VAD_FRAME_S
                self._silence_run = 0.0
                voiced_now += VAD_FRAME_S
            else:
                self._silence_run += VAD_FRAME_S
                self._voiced_run = 0.0
        scores = wake.process(audio) if wake is not None else []
        best = max(scores, default=0.0)
        self._decide(audio, best, voiced_now)

    def _decide(self, audio: np.ndarray, wake_score: float, voiced_now: float) -> None:
        cfg = self.config
        started: Kind | None = None
        ended: tuple[np.ndarray | None, Kind] | None = None
        follow_closed = False
        with self._lock:
            mode = self._mode
            if mode == "record":
                self._clip.append(audio)
                self._rec_s += audio.size / RATE
                self._rec_voiced += voiced_now
                heard = self._rec_voiced >= self._min_speech
                if heard and self._silence_run >= cfg.end_silence_s:
                    ended = (np.concatenate(self._clip), self._kind)
                elif not heard and self._rec_s >= cfg.no_speech_s:
                    ended = (None, self._kind)
                elif self._rec_s >= cfg.max_utterance_s:
                    ended = (np.concatenate(self._clip), self._kind)
                if ended is not None:
                    self._mode, self._clip = "idle", []
            elif mode in ("idle", "follow"):
                woke = wake_score >= cfg.wake_threshold and self._t >= self._refractory_until
                if woke and (not self._speaking or self._barge_mode != "off"):
                    started = "wake"
                elif mode == "follow" and self._voiced_run >= cfg.follow_onset_s:
                    started = "follow"
                elif (
                    self._speaking
                    and self._barge_mode == "speech"
                    and self._voiced_run >= cfg.barge_onset_s
                    and self._audio_playing()
                ):
                    started = "barge"
                elif mode == "follow" and self._t >= self._follow_until:
                    self._mode = "idle"
                    follow_closed = True
                if started is not None:
                    self._start_locked(started)
                    follow_closed = mode == "follow"
        if follow_closed:
            self._on_follow_up(False)
        if started is not None:
            log.info("hands-free utterance started (%s)", started)
            self._on_start(started)
        if ended is not None:
            pcm, kind = ended
            log.info("hands-free utterance %s (%s)", "ended" if pcm is not None else "dropped: no speech", kind)
            self._on_end(pcm, kind)

    def _start_locked(self, kind: Kind) -> None:
        cfg = self.config
        if kind == "wake":
            self._refractory_until = self._t + cfg.wake_refractory_s
            preroll, self._min_speech, self._rec_voiced = cfg.wake_preroll_s, cfg.min_speech_s, 0.0
        else:
            # The speech that triggered this is part of the utterance, plus a little before it.
            preroll = self._voiced_run + cfg.onset_preroll_s
            self._min_speech, self._rec_voiced = 0.0, self._voiced_run
        ring = np.concatenate(list(self._ring)) if self._ring else np.zeros(0, np.float32)
        keep = min(ring.size, int(preroll * RATE))
        self._clip = [ring[ring.size - keep :]]
        self._rec_s = 0.0
        self._silence_run = 0.0
        self._kind = kind
        self._mode = "record"

    def _watch_digital_silence(self, audio: np.ndarray, seconds: float) -> None:
        if self._heard_sound:
            return
        if np.any(audio):
            self._heard_sound = True
            if self._silence_alarm:
                self._on_digital_silence(False)
            return
        self._zero_run += seconds
        if not self._silence_alarm and self._zero_run >= self.config.silence_alarm_s:
            self._silence_alarm = True
            self._on_digital_silence(True)
