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
* **plain "Jarvis"** (optional): a second model that hears "Jarvis" alone.
  The word turns up inside ordinary sentences, and sound-alikes ("Travis")
  score close to it, so it only starts an utterance ("jarvis") at the start
  of one: within 1.5 s of speech that began after a 0.3 s pause. It never
  interrupts Jarvis while he speaks (that stays with "Hey Jarvis" and the
  barge-in mode), and the daemon drops the clip unless its transcript
  starts with "Jarvis".
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

With an echo canceller (``set_echo``), everything after that check hears the
microphone without Jarvis's own voice: the voice detector, the wake word, the
pre-roll and the utterance itself. The check itself reads the raw audio,
since the canceller turns exact zeros into faint noise while Jarvis speaks.

Utterances start with some audio from before their start (pre-roll), so the
first syllable is kept; a plain "Jarvis" utterance keeps everything from
where the speech began, so the transcript holds the whole word (and so does
"Hey Jarvis" at the start of an utterance while plain "Jarvis" is ready and
Jarvis is quiet). Time is
measured in audio, not on the wall clock, so the decisions are the same
however the blocks arrive.
"""

from __future__ import annotations

import logging
import math
import queue
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np

from ..audio.echo import EchoCanceller
from .vad import VoiceDetector
from .wakeword import WakeScorer

log = logging.getLogger(__name__)

RATE = 16_000
VAD_FRAME_S = 512 / RATE

Kind = Literal["wake", "jarvis", "follow", "barge"]
BargeMode = Literal["speech", "wake", "off"]
BARGE_MODES: tuple[BargeMode, ...] = ("speech", "wake", "off")
Mode = Literal["idle", "follow", "record", "paused"]


@dataclass
class ListenerConfig:
    wake_threshold: float = 0.5
    plain_wake_threshold: float = 0.5  # plain "Jarvis"
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
    gate_silence_s: float = 0.3  # a pause at least this long before speech starts a new utterance...
    gate_window_s: float = 1.5  # ...and plain "Jarvis" counts only this soon after that
    plain_preroll_pad_s: float = 0.2  # kept from before the utterance's first voiced frame
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
        plain_wake: bool = False,
        echo: EchoCanceller | None = None,
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
        self._plain_enabled = plain_wake
        self._echo = echo

        self._lock = threading.Lock()
        self._mode: Mode = "idle"
        self._speaking = False
        self._t = 0.0  # seconds of audio processed
        self._follow_until = 0.0
        self._refractory_until = 0.0  # "Hey Jarvis"
        self._plain_refractory_until = 0.0
        self._reset_models = False

        self._in_speech = False
        self._voiced_run = 0.0
        self._silence_run = 0.0
        self._zero_run = 0.0
        self._silence_alarm = False
        self._heard_sound = False
        self._gap_s = math.inf  # silence before the current speech (none heard yet counts as a pause)
        self._utt_onset: float | None = None  # when the current utterance began, after a pause

        self._ring: deque[np.ndarray] = deque()
        self._ring_s = 0.0
        self._ring_max_s = 2.0
        self._kind: Kind = "wake"
        self._long_preroll = False
        # When the block being processed arrived (perf_counter): the wall time of audio time self._t.
        self._stamp: float | None = None
        self._onset_at: float | None = None
        self._clip: list[np.ndarray] = []
        self._rec_s = 0.0
        self._rec_voiced = 0.0
        self._min_speech = 0.0

        self._queue: queue.Queue[tuple[np.ndarray, int, float] | None] = queue.Queue(maxsize=1000)
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
    def plain_enabled(self) -> bool:
        """Plain "Jarvis" is switched on (it also needs the wake word on, and its model)."""
        return self._plain_enabled

    @property
    def plain_ready(self) -> bool:
        """Plain "Jarvis" is switched on and its model is loaded, beside the wake word."""
        wake = self._wake
        return wake is not None and self._wake_enabled and self._plain_enabled and wake.plain_name is not None

    @property
    def long_preroll(self) -> bool:
        """The utterance being recorded (or just handed to ``on_end``) reaches back to where its speech began.

        Read it in ``on_end``: the next utterance sets it afresh.
        """
        return self._long_preroll

    @property
    def onset_at(self) -> float | None:
        """When the voice of the utterance just started began (perf_counter seconds), from the audio's own stamps.

        The voiced run that set it off, or the utterance it belongs to; after the
        wake word, at least as far back as its pre-roll. Read it in ``on_start``:
        the next utterance sets it afresh.
        """
        return self._onset_at

    @property
    def wake_name(self) -> str | None:
        return self._wake.name if self._wake is not None else None

    @property
    def plain_name(self) -> str | None:
        return self._wake.plain_name if self._wake is not None else None

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

    def set_plain_wake(self, enabled: bool) -> None:
        with self._lock:
            self._plain_enabled = enabled
    def set_echo(self, echo: EchoCanceller | None) -> None:
        """Clean the microphone with ``echo`` from the next block on (None: raw again)."""
        self._echo = echo

    def set_barge_mode(self, mode: BargeMode) -> None:
        with self._lock:
            self._barge_mode = mode

    def set_wake_threshold(self, threshold: float) -> None:
        """The wake word sensitivity, for "Hey Jarvis" and plain "Jarvis" alike."""
        with self._lock:
            self.config.wake_threshold = self.config.plain_wake_threshold = min(0.95, max(0.05, threshold))

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
        """Audio thread: queue the block, stamped with its arrival, and return."""
        try:
            # perf_counter: on Windows, monotonic() only advances every 15.6 ms. The
            # echo canceller orders these stamps against the speaker's.
            self._queue.put_nowait((block, rate, time.perf_counter()))
        except queue.Full:
            self._dropped += 1  # the listener fell behind; losing audio beats blocking the sound card

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            try:
                self.handle(*item)
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

    def handle(self, block: np.ndarray, rate: int, stamp: float) -> None:
        """One block from the sound card (listener thread): resample, watch for a muted mic, cancel echo, process."""
        raw = self._to_16k(block, rate)
        self._watch_digital_silence(raw, raw.size / RATE)
        echo = self._echo
        # The canceller runs while paused too, so it stays converged and in step with the speaker.
        self.process(echo.near(raw, stamp) if echo is not None else raw, watch_silence=False, stamp=stamp)

    def process(self, audio: np.ndarray, *, watch_silence: bool = True, stamp: float | None = None) -> None:
        """Handle 16 kHz mono float audio (called on the listener thread, or directly by tests).

        ``stamp`` is when the block arrived (perf_counter); without one, now.
        """
        if audio.size == 0:
            return
        self._stamp = time.perf_counter() if stamp is None else stamp
        seconds = audio.size / RATE
        if watch_silence:
            self._watch_digital_silence(audio, seconds)
        with self._lock:
            self._t += seconds
            mode = self._mode
            reset, self._reset_models = self._reset_models, False
            wake = self._wake if self._wake_enabled else None
            plain = wake is not None and self._plain_enabled and wake.plain_name is not None
        if mode == "paused":
            return
        if reset:
            self._vad.reset()
            if wake is not None:
                wake.reset()
            self._ring.clear()
            self._ring_s = 0.0
            self._in_speech, self._voiced_run, self._silence_run = False, 0.0, 0.0
            self._gap_s, self._utt_onset = math.inf, None

        self._ring.append(audio)
        self._ring_s += seconds
        while self._ring and self._ring_s - self._ring[0].size / RATE >= self._ring_max_s:
            self._ring_s -= self._ring.popleft().size / RATE

        voiced_now = 0.0
        probs = self._vad.process(audio)
        # The pause before an utterance is counted in whole 32 ms frames, so
        # allow half a frame: a 0.3 s pause shows as 9 silent frames (0.288 s).
        gate_silence = self.config.gate_silence_s - VAD_FRAME_S / 2
        for i, prob in enumerate(probs):
            threshold = self.config.speech_threshold
            was_in_speech = self._in_speech
            self._in_speech = prob >= threshold or (self._in_speech and prob >= threshold - 0.15)
            if self._in_speech:
                if not was_in_speech and self._gap_s >= gate_silence:
                    self._utt_onset = self._t - (len(probs) - i) * VAD_FRAME_S  # where this frame began
                self._voiced_run += VAD_FRAME_S
                self._silence_run = self._gap_s = 0.0
                voiced_now += VAD_FRAME_S
            else:
                self._silence_run += VAD_FRAME_S
                self._gap_s += VAD_FRAME_S
                self._voiced_run = 0.0
        if wake is None:
            pairs: list[tuple[float, float | None]] = []
        elif plain:
            pairs = wake.process_pair(audio)
        else:
            pairs = [(score, None) for score in wake.process(audio)]
        best = max((score for score, _ in pairs), default=0.0)
        best_plain = max((score for _, score in pairs if score is not None), default=0.0)
        self._decide(audio, best, voiced_now, best_plain)

    def _at_utterance_start(self) -> bool:
        """The current utterance began after a pause, and not long ago (the gate for plain "Jarvis")."""
        return self._utt_onset is not None and self._t - self._utt_onset <= self.config.gate_window_s

    def _decide(self, audio: np.ndarray, wake_score: float, voiced_now: float, plain_score: float = 0.0) -> None:
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
                # Plain "Jarvis" never cuts Jarvis off: the daemon would stop him
                # before the transcript could show it was only a sound-alike.
                called = (
                    plain_score >= cfg.plain_wake_threshold
                    and self._t >= self._plain_refractory_until
                    and not self._speaking
                )
                if woke and (not self._speaking or self._barge_mode != "off"):
                    started = "wake"
                elif called and self._at_utterance_start():
                    started = "jarvis"
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
        self._long_preroll = False
        if kind in ("wake", "jarvis"):
            # "Hey Jarvis" holds off both phrases (the plain model hears its
            # "Jarvis" too). Plain "Jarvis" holds off only itself: when the daemon
            # cancels one (Jarvis just became audible), "Hey Jarvis, stop" right
            # after must still work. The cost: in that case the "Hey Jarvis" model
            # firing late on the same word can start a "wake" too.
            self._plain_refractory_until = self._t + cfg.wake_refractory_s
            if kind == "wake":
                self._refractory_until = self._plain_refractory_until
            preroll, self._min_speech, self._rec_voiced = cfg.wake_preroll_s, cfg.min_speech_s, 0.0
            # The short pre-roll cuts plain "Jarvis" off (and "Hey Jarvis" often
            # fires on it too): at the start of an utterance, keep all of it. Not
            # while Jarvis speaks: what began after a pause may be his own voice,
            # heard through speakers, so "Hey Jarvis" over him keeps its short pre-roll.
            if self._utt_onset is not None and (
                kind == "jarvis" or (self.plain_ready and not self._speaking and self._at_utterance_start())
            ):
                preroll = max(preroll, min(self._ring_max_s, self._t - self._utt_onset + cfg.plain_preroll_pad_s))
                self._long_preroll = True
        else:
            # The speech that triggered this is part of the utterance, plus a little before it.
            preroll = self._voiced_run + cfg.onset_preroll_s
            self._min_speech, self._rec_voiced = 0.0, self._voiced_run
        # Where the voice began (onset_at): never later than the voiced run that set this off, the
        # utterance it belongs to or, after the wake word, its pre-roll. Audio time self._t is now.
        began = self._t - self._voiced_run
        if self._utt_onset is not None:
            began = min(began, self._utt_onset)
        if kind in ("wake", "jarvis"):
            began = min(began, self._t - preroll)
        self._onset_at = (time.perf_counter() if self._stamp is None else self._stamp) - (self._t - began)
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
