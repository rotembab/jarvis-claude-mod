"""Echo cancelling: takes Jarvis's own voice out of what the microphone hears.

Through speakers (and faintly through a headset turned up loud) the
microphone picks up the reply Jarvis is playing. Left in, that echo sounds
like the user talking over him: he stops himself, or wakes himself up.
WebRTC's echo canceller (AEC3, from livekit's AudioProcessingModule) learns
the path from the speaker to the microphone and subtracts what was played.

It takes two streams, each in frames of exactly 10 ms. A frame of any other
size makes the native code panic, which kills the whole helper, so every call
goes through one size check (``_call``), against that frame's own rate.

* The far end is what went to the speaker. The output's audio callback hands
  each block to ``far()``, which only queues it with the time it was rendered.
  It goes in at the device's own rate (240 samples a frame at 24 kHz, 441 at
  44.1 kHz, 480 at 48 kHz): a streaming resampler in between would hand it
  over in bursts up to 37 ms late, behind its own echo when the speaker is
  close, and AEC3 falls apart within seconds of that.
* The near end is the microphone, at 16 kHz (160 samples a frame). The
  listener's thread calls ``near()`` with each block and the time it arrived.
  It first feeds the far blocks rendered before then, then the microphone's
  frames, so both reach the canceller in the order the sound card produced
  them, away from the sound card's threads.

AEC3 pairs one far frame with every microphone frame, and keeps the echo
path it learned from that pairing. Anything that breaks the pairing shifts
the far end against the microphone by a few milliseconds, and an AEC3 that
keeps its old filter then takes about a second, at 2-25 dB, to find the echo
again. A new AEC3 that has heard the far end for a while (silence will do)
cancels from its first quarter second instead. So the canceller starts
afresh whenever the pairing breaks. (Figures from an event simulation of
both sound card threads through the real AEC3, 20-60 ms echo paths.)

* The output stops: closed when idle, aborted and restarted by a stop. The
  playback says so with ``far(None, rate)``. From then until it plays again,
  silence stands in for the far end, one frame for every 10 ms that passes by
  the microphone's stamps, at the rate the far end last came in at (a change
  of rate would start AEC3 over at the next reply). The new AEC3 waits until
  the echo of what played last has died away (the delay hint plus 0.2 s),
  which the old one still cancels, or until the output plays a sound again.
  A reply that comes after that, once the output closed for 2-30 s or a stop
  restarted it, is at least 70 dB quieter in its first quarter second, and
  usually 64-71 dB over its first second (36-40 dB at worst; an AEC3 that
  kept its old filter gave 4-32 dB). Output callbacks that come in groups
  need nothing: nothing guesses at a closed output from a pause in callbacks.
* The microphone has a gap of more than 0.2 s while the speaker plays on (the
  device stalled, or the listener fell so far behind that it dropped blocks).
  The far audio rendered just before the next microphone block (the delay
  hint plus half a second) is what that block's echo comes from: it goes in
  as the new AEC3's history, each frame with a silent microphone frame, and
  anything older is dropped. That gives 50-71 dB from the first quarter
  second with callback jitter up to 20 ms (rebuilding the old pairing from
  their stamps gave 1-55 dB). Shorter gaps are left alone: a callback that is
  merely late looks the same until the blocks behind it arrive.

Known weak cases, from the same simulation:

* Sound within the delay hint plus 0.2 s of a stop: a reply, or the start
  chime of a barge-in, straight after it. The new AEC3 starts on that sound
  while the room still echoes the last words, and follows its start-up
  curve: about 33-39 dB for the first two seconds, 47-49 dB by the third
  (14-23 dB, then 52-57, with the old filter kept). With a 5 ms restart and
  a chime in its first blocks, the last words' echo around the stop gets only
  14-17 dB (45-47 dB with a 20 ms restart; 32-51 dB with the old filter).
* A microphone gap under 0.2 s: left alone (above), so AEC3 takes about two
  seconds to find the echo again.
* Microphone blocks held back and delivered together (0.25-1 s at once):
  they look like a gap, and the first quarter second after it gets 20-36 dB.
* A gap with a wireless headset that reports too little latency (200 ms
  missing from a 30 ms hint, at 48 kHz): 28-34 dB in the first quarter
  second and 34-39 dB in the first second, a little worse than the old
  pairing's 43-44 dB. A gap while the output's callbacks come in groups of
  120 ms: as low as 25 dB.
* A stop the playback never reports (a stream that dies without a word)
  leaves the far end silent until the output reopens and reports it then:
  about a second of weak cancelling, as before.

The delay hint is the output latency plus the input latency PortAudio
reports, clamped to the 0-500 ms WebRTC accepts. AEC3 searches about half a
second beyond it, which covers wireless headsets reporting too little.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol

import numpy as np

from .playback import TTS_SAMPLERATE

if TYPE_CHECKING:
    from .capture import Capture
    from .playback import Playback

log = logging.getLogger(__name__)

RATE = 16_000  # the microphone side
FRAME = RATE // 100  # 10 ms: the only frame length the native code survives
NEAR_GAP_S = 0.2  # microphone audio missing for longer than this: a gap, not a late callback
GAP_HISTORY_S = 0.5  # after a gap, far audio this long before the delay hint goes in again as the new AEC3's history
ECHO_TAIL_S = 0.2  # after the output stops, the room echoes what last played for about this long past the delay hint
FAR_QUEUE_BLOCKS = 4_000  # a safety net only (10 s of 2.5 ms output blocks); every call drains the queue
MAX_DELAY_MS = 500  # WebRTC refuses a delay hint outside 0-500 ms, and livekit raises on it
MIN_RATE, MAX_RATE = 8_000, 384_000  # the far-end rates WebRTC takes (outside them livekit raises, no panic)
_SILENCE = np.zeros(FRAME, np.float32)

OnFailed = Callable[[Exception], None]


class EchoCanceller(Protocol):
    def far(self, block: np.ndarray | None, rate: int) -> None:
        """Output audio thread: ``block`` just went to the speaker, or None: the output stopped. Queues it and returns.

        After None, nothing is rendered until the next block (a reopened or restarted stream).
        """
        ...

    def near(self, audio: np.ndarray, stamp: float) -> np.ndarray:
        """Listener thread: 16 kHz microphone audio that arrived at ``stamp`` (``time.perf_counter``).

        Returns it without the echo, in whole 10 ms frames: the rest waits for the next block.
        """
        ...

    def set_on_failed(self, callback: OnFailed | None) -> None:
        """Call ``callback`` once (on the listener thread) if cancelling stops working; it must return at once."""
        ...

    def close(self) -> None: ...


def delay_ms(seconds: float) -> int:
    """The delay hint WebRTC accepts: whole milliseconds in 0-500."""
    if not math.isfinite(seconds):
        return 0
    return min(MAX_DELAY_MS, max(0, round(seconds * 1000)))


def native_rate(rate: int) -> bool:
    """Whether the far end can go in at ``rate`` as it is: 10 ms must be a whole number of samples."""
    return MIN_RATE <= rate <= MAX_RATE and rate % 100 == 0


def new_livekit_frame(rate: int) -> Any:
    from livekit import rtc

    return rtc.AudioFrame.create(rate, 1, rate // 100)


class WebRtcEchoCanceller:
    """AEC3 and a high-pass filter, from livekit's WebRTC AudioProcessingModule.

    ``delay_s`` returns the output plus input latency, read before every
    frame. ``far_rate`` is the rate the output plays at, or returns it: read
    when silence first goes in, it is the rate silence goes in at until the
    output has played. ``new_apm`` (a fresh module) and
    ``new_frame`` (a mono 10 ms frame at the given rate) stand in for
    livekit's (tests). If the canceller fails, the microphone passes through
    unchanged for the rest of the session and ``set_on_failed``'s callback
    hears about it, once.
    """

    def __init__(
        self,
        delay_s: Callable[[], float] = lambda: 0.0,
        *,
        far_rate: int | Callable[[], int] = TTS_SAMPLERATE,
        noise_suppression: bool = False,
        clock: Callable[[], float] = time.perf_counter,
        new_apm: Callable[[], Any] | None = None,
        new_frame: Callable[[int], Any] | None = None,
    ) -> None:
        if new_apm is None:
            from livekit import rtc  # about 0.4 s and a pool of native threads: loaded in the background

            logging.getLogger("livekit").setLevel(logging.WARNING)  # its native log comes through this logger

            def make_apm() -> Any:  # about 0.3 ms once livekit has loaded
                return rtc.AudioProcessingModule(
                    echo_cancellation=True,
                    high_pass_filter=True,
                    noise_suppression=noise_suppression,
                    auto_gain_control=False,
                )

            new_apm, new_frame = make_apm, new_livekit_frame
        if new_frame is None:
            raise ValueError("new_frame is required with a custom apm")
        self._new_apm = new_apm
        self._apm: Any = new_apm()
        self._fresh = True  # this module has had no far audio from the output: nothing to start over
        self._new_frame = new_frame
        self._delay_s = delay_s
        self._clock = clock
        self._lock = threading.Lock()
        self._on_failed: OnFailed | None = None
        # One reusable frame per rate; frames are only ever touched under the lock.
        self._cap = new_frame(RATE)
        self._rev: dict[int, Any] = {}
        # (stamp, block or None, rate) from the output's threads. deque append/popleft are atomic: it takes no lock.
        self._far_q: deque[tuple[float, np.ndarray | None, int]] = deque(maxlen=FAR_QUEUE_BLOCKS)
        self._far_next: tuple[float, np.ndarray | None, int] | None = None  # popped, but rendered after the mic block
        self._far_in_rate = 0  # the device rate of the last far block
        self._far_rest = np.zeros(0, np.float32)  # far audio short of a whole frame, at the rate it goes in at
        self._far_resampler: Any = None  # for a device rate the native code cannot take
        self._resampled: set[int] = set()  # device rates the native code refused: resampled to 16 kHz
        self._accepted: set[int] = set()  # far rates the native code has taken
        self._far_rate = far_rate
        # The rate the far end last went in at: silence goes in at it too (0: not read from ``far_rate`` yet).
        self._rev_rate = 0
        self._playing = False  # the output is rendering: its blocks are the far end, not silence
        self._silent_since = 0.0  # while it is not: when the silence began (the microphone's clock)
        self._silent_frames = 0  # and the silent far frames fed since then
        self._pairing = False  # every far frame goes in with a silent microphone frame (the history after a gap)
        self._start_over_at = math.inf  # after a stop: when the old AEC3 has cancelled the last echo (mic clock)
        self._near_rest = np.zeros(0, np.float32)  # microphone audio short of a whole frame
        self._near_at = -math.inf  # when the last microphone block arrived
        self._failed = False

    # ------------------------------------------------------------------ audio threads

    def far(self, block: np.ndarray | None, rate: int) -> None:
        self._far_q.append((self._clock(), block, rate))

    def near(self, audio: np.ndarray, stamp: float) -> np.ndarray:
        failure: Exception | None = None
        with self._lock:
            if self._apm is None or self._failed:
                return audio
            pending = np.concatenate([self._near_rest, audio]) if self._near_rest.size else audio
            whole = pending.size - pending.size % FRAME
            self._near_rest = pending[whole:]
            out = np.empty(whole, np.float32)
            done = 0
            try:
                self._feed_far(stamp, audio.size / RATE)
                for done in range(0, whole, FRAME):
                    out[done : done + FRAME] = self._process(pending[done : done + FRAME])
            except Exception as exc:  # noqa: BLE001 - losing the canceller must not lose the microphone
                log.warning("echo cancelling failed; the microphone passes through from now on: %s", exc)
                self._failed = True
                self._near_rest = pending[:0]
                self._far_q.clear()
                self._far_next = None
                out = np.concatenate([out[:done], pending[done:]])
                failure = exc
            on_failed = self._on_failed
        if failure is not None and on_failed is not None:
            on_failed(failure)  # outside the lock; once, since a failed canceller returns above from now on
        return out

    def _delay_hint(self) -> int:
        try:
            return delay_ms(self._delay_s())
        except Exception:  # noqa: BLE001 - a latency the device cannot report: AEC3 finds the delay itself
            return 0

    def _process(self, frame: np.ndarray) -> np.ndarray:
        """One 10 ms microphone frame through the canceller, after the delay hint it forgets every frame."""
        apm = self._apm
        try:
            apm.set_stream_delay_ms(self._delay_hint())
        except RuntimeError:
            pass  # WebRTC only warns, and AEC3 finds the delay itself
        return self._call(apm.process_stream, self._cap, frame)

    def _feed_far(self, stamp: float, seconds: float) -> None:
        """Feed the far end up to ``stamp`` (a microphone block of ``seconds``): what was rendered, or silence."""
        began = stamp - seconds  # when this microphone block's audio began arriving
        if stamp >= self._start_over_at:
            self._start_over()
        if began - self._near_at > NEAR_GAP_S:  # also the first block: whatever was queued before it is unpaired
            # The far audio rendered while the microphone was away has nothing to pair with: start afresh.
            # What was rendered just before this block (the delay hint plus half a second) is what its echo
            # comes from, and the new AEC3 needs it as history: it goes in with silent microphone frames, one for
            # one, so the two streams stay in step. Anything older is dropped.
            self._drain_far(until=began - self._delay_hint() / 1000 - GAP_HISTORY_S, drop=True)
            self._start_over()  # after the drop: a stop in what was dropped needs no new AEC3 of its own
            self._silent_since, self._silent_frames = began, 0  # silence, where it stands in, starts with the block
            self._pairing = True
            try:
                self._drain_far(until=began)
            finally:
                self._pairing = False
            if not self._playing:
                self._silent_since, self._silent_frames = began, 0
        self._near_at = stamp
        self._drain_far(until=stamp)
        if not self._playing:
            self._fill_silence(until=stamp)

    def _drain_far(self, *, until: float, drop: bool = False) -> None:
        """Feed (or drop) the queued far blocks rendered by ``until``, in order; the output's stops still count."""
        queue = self._far_q
        while True:
            item, self._far_next = self._far_next, None
            if item is None:
                try:
                    item = queue.popleft()  # popped before it is looked at: a full queue can't evict it meanwhile
                except IndexError:
                    return
            at, block, rate = item[0], item[1], int(item[2])
            if at > until:
                self._far_next = item  # rendered later: it goes in after this microphone audio
                return
            if block is None:
                self._stopped(at)
            elif not drop:
                if not self._playing:  # the output plays again: silence up to where its first block begins
                    self._fill_silence(until=at)
                    self._playing = True
                block = np.asarray(block, np.float32).reshape(-1)
                if self._start_over_at < math.inf and block.any():
                    self._start_over()  # new sound after a stop: it needs the new AEC3 from its first frame
                self._fresh = False
                self._feed_block(block, rate)

    def _stopped(self, at: float) -> None:
        """The output stopped at ``at``: silence for the far end until it plays again, and a new AEC3 soon.

        The old AEC3 still cancels the echo of what played last, so the new one waits until that has died
        away (the delay hint plus ``ECHO_TAIL_S``), or until the output plays a sound again.
        """
        if not self._playing:
            return  # already silent: a second word of the same stop
        self._playing = False
        self._far_rest, self._far_resampler = self._far_rest[:0], None  # the rest of a stream that ended
        self._silent_since, self._silent_frames = at, 0
        self._start_over_at = at + self._delay_hint() / 1000 + ECHO_TAIL_S

    def _start_over(self) -> None:
        """A new AEC3, for a far end out of step with the microphone (nothing to do before the output has played)."""
        self._start_over_at = math.inf
        if not self._fresh:
            self._apm = self._new_apm()  # the old one is freed with its last reference
            self._fresh = True

    def _fill_silence(self, *, until: float) -> None:
        """Silent far frames, one for every 10 ms since the silence began that ended by ``until``."""
        due = math.floor((until - self._silent_since) * 100 + 1e-6)
        while self._silent_frames < due:
            rate = self._rev_rate or self._output_rate()
            try:
                self._reverse(rate, np.zeros(rate // 100, np.float32))
            except RuntimeError:
                if rate in self._accepted or rate == RATE:
                    raise
                self._rev_rate = RATE  # the output's rate, refused before anything played: 16 kHz, as its audio will
                continue
            self._silent_frames += 1

    def _output_rate(self) -> int:
        """The rate silence goes in at before anything has played: the output's, read the first time it is needed."""
        try:
            rate = int(self._far_rate() if callable(self._far_rate) else self._far_rate)
        except Exception:  # noqa: BLE001 - an output that cannot say: the rate it tries first
            rate = TTS_SAMPLERATE
        self._rev_rate = rate if native_rate(rate) else RATE
        return self._rev_rate

    def _feed_block(self, block: np.ndarray, rate: int) -> None:
        if rate != self._far_in_rate:  # a new device rate: what is left over belongs to the old one
            self._far_in_rate, self._far_rest, self._far_resampler = rate, self._far_rest[:0], None
        if native_rate(rate) and rate not in self._resampled:
            into, audio = rate, block
        else:
            into, audio = RATE, self._resample(block, rate)
        pending = np.concatenate([self._far_rest, audio]) if self._far_rest.size else audio
        size = into // 100
        whole = pending.size - pending.size % size
        for start in range(0, whole, size):
            try:
                self._reverse(into, pending[start : start + size])
            except RuntimeError as exc:
                if into in self._accepted or into == RATE:
                    raise
                # Refused outright (no panic) the first time: resample this rate from now on.
                log.info("echo canceller refused a %d Hz far end (%s); resampling it to 16 kHz", into, exc)
                self._resampled.add(rate)
                self._far_rest = pending[:0]
                self._feed_block(pending[start:], rate)
                return
        self._far_rest = pending[whole:]

    def _resample(self, block: np.ndarray, rate: int) -> np.ndarray:
        if self._far_resampler is None:
            import soxr

            # "QQ" hands back what it is given at once, steadily: the higher qualities hold audio back and
            # release it in bursts up to 37 ms late, which leaves the echo ahead of its reference.
            self._far_resampler = soxr.ResampleStream(rate, RATE, 1, dtype="float32", quality="QQ")
        return np.asarray(self._far_resampler.resample_chunk(block), np.float32)

    def _reverse(self, rate: int, audio: np.ndarray) -> None:
        frame = self._rev.get(rate)
        if frame is None:
            frame = self._rev[rate] = self._new_frame(rate)
        self._call(self._apm.process_reverse_stream, frame, audio)
        self._accepted.add(rate)
        self._rev_rate = rate
        if self._pairing:
            self._process(_SILENCE)

    @staticmethod
    def _call(process: Callable[[Any], None], frame: Any, audio: np.ndarray) -> np.ndarray:
        """Run one native call on ``audio`` through ``frame``; returns the frame's samples afterwards.

        The samples are read and written through ``frame.data`` on every call, never through a view
        kept from before: livekit replaces a frame's buffer when it is read-only, and a kept view would
        then write where the native code no longer reads.
        """
        pcm = np.frombuffer(frame.data, np.int16)
        size = frame.sample_rate // 100 * frame.num_channels
        if audio.size != size or pcm.size != size:
            # Anything but exactly 10 ms at the frame's own rate panics the native code, which ends the process.
            raise ValueError(f"echo canceller frames are {size} samples at {frame.sample_rate} Hz, not {audio.size}")
        pcm[:] = np.clip(np.rint(audio * 32768.0), -32768, 32767)
        process(frame)
        return np.frombuffer(frame.data, np.int16).astype(np.float32) / 32768.0

    # ------------------------------------------------------------------ lifecycle

    def set_on_failed(self, callback: OnFailed | None) -> None:
        with self._lock:
            self._on_failed = callback

    def close(self) -> None:
        with self._lock:
            # Free the native module now: left for interpreter exit, livekit's own
            # exit hook runs first and its handle complains on stderr.
            self._apm = None
            self._far_q.clear()
            self._far_next = None


def loader(playback: Playback, capture: Capture) -> Callable[[], WebRtcEchoCanceller]:
    """What the helper loads in the background: a canceller told the output plus input latency, and its rate.

    The rate is read when silence first goes in for the far end, as the listener first hears the microphone
    through the loaded canceller: by then the output has usually opened, so silence goes in at the rate the
    first reply plays at, and that reply does not start AEC3 over. (An output that only opens after that, at a
    rate other than 24 kHz, costs its first reply that reset.)
    """
    return lambda: WebRtcEchoCanceller(
        lambda: playback.output_latency + capture.input_latency, far_rate=lambda: playback.device_rate
    )


# --------------------------------------------------------------------------- self-test (doctor)


def level_db(audio: np.ndarray) -> float:
    return 10 * math.log10(float(np.mean(np.square(audio, dtype=np.float64))) + 1e-12)


def synthetic_speech(seconds: float, seed: int, rate: int = RATE, *, floor: float = 0.0) -> np.ndarray:
    """Seeded noise shaped like speech: 100 Hz up, falling above 800 Hz, four syllables a second.

    About -24 dBFS; the syllables never fall below ``floor`` (0-1) of full level.
    """
    rng = np.random.default_rng(seed)
    n = int(seconds * rate)
    freqs = np.fft.rfftfreq(n, 1 / rate)
    shape = (freqs >= 100) / np.sqrt(1 + (freqs / 800) ** 2)
    voice = np.fft.irfft(np.fft.rfft(rng.standard_normal(n)) * shape, n)
    voice /= math.sqrt(float(np.mean(voice**2))) or 1.0
    t = np.arange(n) / rate
    syllables = 0.5 + 0.5 * np.sin(2 * np.pi * 4.0 * t + rng.uniform(0, 2 * np.pi))
    return (0.1 * voice * (floor + (1 - floor) * syllables)).astype(np.float32)


def synthetic_echo(far: np.ndarray, delay_s: float = 0.06, gain: float = 0.5, rate: int = RATE) -> np.ndarray:
    """``far`` as a microphone hears it from a speaker: delayed, quieter, with a short room tail."""
    lag = int(delay_s * rate)
    tail = np.random.default_rng(7).standard_normal(400) * np.exp(-np.arange(400) / 80) * 0.3
    response = np.zeros(lag + 400)
    response[lag] = 1.0
    response[lag:] += tail
    return (gain * np.convolve(far, response)[: far.size]).astype(np.float32)


SELF_TEST_RATE = TTS_SAMPLERATE  # the rate the output opens at first


def self_test(seconds: float = 6.0) -> dict[str, Any]:
    """Cancel a synthetic echo the way the live path meets it. Plays and records nothing.

    The far end is 24 kHz in 10 ms blocks, as the output renders it; the microphone hears it 30 ms
    later at -6 dB with a room tail. Reports how much quieter the echo was over the last two seconds
    (a canceller that drifts out of step falls apart after a few seconds), and the time one 10 ms frame
    (far end, delay, microphone) took.
    """
    import soxr

    far = synthetic_speech(seconds, seed=1, rate=SELF_TEST_RATE)
    mic = synthetic_echo(np.asarray(soxr.resample(far, SELF_TEST_RATE, RATE), np.float32), delay_s=0.03)
    step = SELF_TEST_RATE // 100
    now = [0.0]
    canceller = WebRtcEchoCanceller(lambda: 0.03, far_rate=SELF_TEST_RATE, clock=lambda: now[0])
    cleaned: list[np.ndarray] = []
    started = time.perf_counter()
    try:
        for k in range(mic.size // FRAME):
            now[0] = k / 100
            canceller.far(far[k * step : (k + 1) * step], SELF_TEST_RATE)
            cleaned.append(canceller.near(mic[k * FRAME : (k + 1) * FRAME], now[0] + 0.001))
    finally:
        canceller.close()
    took = time.perf_counter() - started
    out = np.concatenate(cleaned)
    last = slice(out.size - 2 * RATE, out.size)
    return {
        "attenuationDb": round(level_db(mic[last]) - level_db(out[last]), 1),
        "usPerFrame": round(took / len(cleaned) * 1e6),
    }
