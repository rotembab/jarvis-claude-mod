"""Microphone capture: the Capture interface and the pre-roll recording buffer."""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from typing import Protocol

import numpy as np

from .levels import meter

STT_SAMPLERATE = 16_000

# Receives every block the microphone delivers, with its sample rate, on the audio thread.
BlockListener = Callable[[np.ndarray, int], None]


class Capture(Protocol):
    """A microphone that is kept open and records on demand.

    ``begin()`` starts a clip that already contains the last ``preroll``
    seconds (so the first syllable is not lost to key-press latency);
    ``end()`` returns the clip as 16 kHz mono float32.
    """

    @property
    def device_name(self) -> str | None: ...

    @property
    def is_open(self) -> bool: ...

    @property
    def level(self) -> float: ...

    def open(self) -> None:
        """Open the device (idempotent). Raises AudioError."""
        ...

    def close(self) -> None: ...

    def begin(self) -> None: ...

    def end(self) -> np.ndarray:
        """Finish the clip. Raises AudioError if the device stopped delivering audio."""
        ...

    def recording_seconds(self) -> float: ...

    def set_listener(self, listener: BlockListener | None) -> None:
        """Also hand every block to ``listener`` (which must return at once)."""
        ...


class CaptureBuffer:
    """Thread-safe pre-roll ring + clip accumulator fed by an audio callback.

    ``feed()`` is called from the real-time audio thread, so it only appends
    references and updates counters under a short lock.
    """

    def __init__(self, samplerate: int, *, preroll_s: float = 0.3, max_record_s: float = 60.0) -> None:
        self.samplerate = samplerate
        self._preroll_frames = int(preroll_s * samplerate)
        self._max_frames = int(max_record_s * samplerate)
        self._lock = threading.Lock()
        self._ring: deque[np.ndarray] = deque()
        self._ring_frames = 0
        self._clip: list[np.ndarray] | None = None
        self._clip_frames = 0
        self._preroll_taken = 0
        self.level = 0.0

    @property
    def recording(self) -> bool:
        return self._clip is not None

    @property
    def clip_frames(self) -> int:
        return self._clip_frames

    @property
    def recorded_frames(self) -> int:
        """Frames captured since begin(), excluding the pre-roll."""
        return self._clip_frames - self._preroll_taken

    @property
    def full(self) -> bool:
        return self._clip is not None and self._clip_frames >= self._max_frames

    def feed(self, block: np.ndarray) -> None:
        block = np.asarray(block, dtype=np.float32).reshape(-1)
        level = meter(block)
        with self._lock:
            self.level = level
            if self._clip is not None:
                if self._clip_frames < self._max_frames:
                    take = block[: self._max_frames - self._clip_frames]
                    self._clip.append(take)
                    self._clip_frames += take.size
                return
            self._ring.append(block)
            self._ring_frames += block.size
            # Keep just enough whole blocks to cover the pre-roll.
            while self._ring and self._ring_frames - self._ring[0].size >= self._preroll_frames:
                self._ring_frames -= self._ring.popleft().size

    def begin(self) -> None:
        with self._lock:
            if self._clip is not None:
                return
            preroll = np.concatenate(list(self._ring)) if self._ring else np.zeros(0, np.float32)
            preroll = preroll[-self._preroll_frames :] if self._preroll_frames else preroll[:0]
            self._clip = [preroll]
            self._clip_frames = preroll.size
            self._preroll_taken = preroll.size
            self._ring.clear()
            self._ring_frames = 0

    def end(self) -> np.ndarray:
        with self._lock:
            parts, self._clip, self._clip_frames = self._clip or [], None, 0
            self._preroll_taken = 0
        return np.concatenate(parts).astype(np.float32, copy=False) if parts else np.zeros(0, np.float32)


def to_stt_rate(pcm: np.ndarray, samplerate: int) -> np.ndarray:
    """Resample a mono clip to 16 kHz (soxr, high quality) when needed."""
    if samplerate == STT_SAMPLERATE or pcm.size == 0:
        return pcm.astype(np.float32, copy=False)
    import soxr

    return np.asarray(soxr.resample(pcm, samplerate, STT_SAMPLERATE), dtype=np.float32)
