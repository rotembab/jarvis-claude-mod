"""Hardware-free audio for tests and ``--fake-audio`` mode."""

from __future__ import annotations

import threading
import time

import numpy as np

from .capture import STT_SAMPLERATE, BlockListener
from .errors import AudioError
from .playback import TTS_SAMPLERATE, FarListener, PlaybackMixer


class FakeCapture:
    """Synthesises a clip whose length matches how long recording lasted.

    ``signal`` = "tone" (speech-like 220 Hz at -10 dBFS), "silence" (exact
    zeros) or "quiet" (very low noise). ``next_clip`` overrides one clip.
    """

    input_latency = 0.0

    def __init__(self, signal: str = "tone", *, preroll_s: float = 0.3, fail_with: Exception | None = None) -> None:
        self.signal = signal
        self.preroll_s = preroll_s
        self.fail_with = fail_with
        self.next_clip: np.ndarray | None = None
        self.opened = False
        self._began_at: float | None = None
        self.begins = 0
        self.listener: BlockListener | None = None

    @property
    def device_name(self) -> str | None:
        return "Fake Microphone"

    @property
    def is_open(self) -> bool:
        return self.opened

    @property
    def level(self) -> float:
        return 0.5 if self._began_at is not None and self.signal == "tone" else 0.0

    def open(self) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.opened = True

    def close(self) -> None:
        self.opened = False

    def begin(self) -> None:
        if self._began_at is None:
            self._began_at = time.monotonic()
            self.begins += 1

    def recording_seconds(self) -> float:
        return 0.0 if self._began_at is None else time.monotonic() - self._began_at

    def set_listener(self, listener: BlockListener | None) -> None:
        self.listener = listener

    def push(self, pcm: np.ndarray, rate: int = STT_SAMPLERATE) -> None:
        """Deliver audio to the listener the way the sound card would, 10 ms at a time."""
        step = rate // 100
        for start in range(0, pcm.size, step):
            if self.listener is not None:
                self.listener(pcm[start : start + step].astype(np.float32), rate)

    def end(self) -> np.ndarray:
        seconds = self.preroll_s + self.recording_seconds()
        self._began_at = None
        if self.next_clip is not None:
            clip, self.next_clip = self.next_clip, None
            return clip.astype(np.float32)
        n = int(seconds * STT_SAMPLERATE)
        if self.signal == "silence":
            return np.zeros(n, np.float32)
        if self.signal == "quiet":
            return (np.random.default_rng(0).standard_normal(n) * 1e-4).astype(np.float32)
        t = np.arange(n, dtype=np.float32) / STT_SAMPLERATE
        return (0.3 * np.sin(2 * np.pi * 220.0 * t)).astype(np.float32)


class FakePlayback:
    """Consumes the real mixer from a thread at ``speed`` x real time."""

    samplerate = TTS_SAMPLERATE
    device_rate = TTS_SAMPLERATE
    output_latency = 0.0

    def __init__(self, *, speed: float = 1.0, block_ms: int = 10, fail_with: Exception | None = None) -> None:
        self.mixer = PlaybackMixer(TTS_SAMPLERATE)
        self.speed = speed
        self.block = int(TTS_SAMPLERATE * block_ms / 1000)
        self.fail_with = fail_with
        self.open_error: AudioError | None = None  # a failed open, or a test: the device could not reopen
        self.held = False
        self.effects_played = 0
        self.stops = 0
        self.ends = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._far_listener: FarListener | None = None

    @property
    def device_name(self) -> str | None:
        return "Fake Speakers"

    @property
    def level(self) -> float:
        return self.mixer.level

    @property
    def speech_generation(self) -> int:
        return self.mixer.generation

    @property
    def speech_frames_played(self) -> int:
        return self.mixer.frames_played

    @property
    def speech_frames_queued(self) -> int:
        return self.mixer.frames_queued

    @property
    def paused(self) -> bool:
        return self.mixer.paused

    def open(self) -> None:
        if self.fail_with is not None:
            if isinstance(self.fail_with, AudioError):
                self.open_error = self.fail_with
            raise self.fail_with
        self.open_error = None
        if self._thread is not None and self._stop.is_set():
            self._thread.join()  # closed but maybe not exited yet: let it finish before restarting
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="fake-playback", daemon=True)
            self._thread.start()

    def close(self) -> None:
        self._stop.set()

    def hold_open(self, held: bool) -> None:
        self.held = held

    def set_far_listener(self, listener: FarListener | None) -> None:
        self._far_listener = listener

    def _run(self) -> None:
        # Pace by the clock, not by wait() timeouts, the way a sound card does:
        # Windows rounds Event.wait to its ~15.6 ms timer tick and busy macOS
        # machines oversleep, which would otherwise play several times slower
        # than ``speed``.
        period = self.block / TTS_SAMPLERATE / self.speed
        started = time.monotonic()
        rendered = 0
        while not self._stop.wait(period):
            due = int((time.monotonic() - started) / period)
            while rendered < due and not self._stop.is_set():
                self._render_block()
                rendered += 1
        tap = self._far_listener
        if tap is not None:
            tap(None, TTS_SAMPLERATE)  # closed: the echo canceller hears that nothing plays until it reopens

    def _render_block(self) -> np.ndarray:
        out = self.mixer.render(self.block)
        tap = self._far_listener
        if tap is not None:
            tap(out, TTS_SAMPLERATE)
        return out

    def add_speech(self, pcm: np.ndarray, generation: int | None = None) -> None:
        self.mixer.add_speech(pcm, generation)

    def add_effect(self, pcm: np.ndarray) -> None:
        self.effects_played += 1
        self.mixer.add_effect(pcm)

    def end_speech(self, generation: int | None = None) -> None:
        self.ends += 1

    def stop_speech(self) -> None:
        self.stops += 1
        self.mixer.clear_speech()

    def reset_speech_counters(self) -> None:
        self.mixer.reset_counters()

    def speech_idle(self) -> bool:
        return self.mixer.speech_pending() == 0

    def set_paused(self, paused: bool) -> None:
        self.mixer.paused = paused
