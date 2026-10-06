"""Hardware-free audio for tests and ``--fake-audio`` mode."""

from __future__ import annotations

import threading
import time

import numpy as np

from .capture import STT_SAMPLERATE
from .playback import TTS_SAMPLERATE, PlaybackMixer


class FakeCapture:
    """Synthesises a clip whose length matches how long recording lasted.

    ``signal`` = "tone" (speech-like 220 Hz at -10 dBFS), "silence" (exact
    zeros) or "quiet" (very low noise). ``next_clip`` overrides one clip.
    """

    def __init__(self, signal: str = "tone", *, preroll_s: float = 0.3, fail_with: Exception | None = None) -> None:
        self.signal = signal
        self.preroll_s = preroll_s
        self.fail_with = fail_with
        self.next_clip: np.ndarray | None = None
        self.opened = False
        self._began_at: float | None = None
        self.begins = 0

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
    output_latency = 0.0

    def __init__(self, *, speed: float = 1.0, block_ms: int = 10, fail_with: Exception | None = None) -> None:
        self.mixer = PlaybackMixer(TTS_SAMPLERATE)
        self.speed = speed
        self.block = int(TTS_SAMPLERATE * block_ms / 1000)
        self.fail_with = fail_with
        self.effects_played = 0
        self.stops = 0
        self.ends = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

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
            raise self.fail_with
        if self._thread is not None and self._stop.is_set():
            self._thread.join()  # closed but maybe not exited yet: let it finish before restarting
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="fake-playback", daemon=True)
            self._thread.start()

    def close(self) -> None:
        self._stop.set()

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

    def _render_block(self) -> None:
        self.mixer.render(self.block)

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
