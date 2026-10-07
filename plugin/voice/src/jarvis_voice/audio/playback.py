"""Speaker output: the Playback interface and the queue-fed mixer behind it."""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from typing import Protocol

import numpy as np

from .errors import AudioError
from .levels import meter

TTS_SAMPLERATE = 24_000

# Receives every block sent to the speaker, with the device's sample rate, on the audio thread; and None
# (with the rate) when the stream stops: closed, aborted or restarted. Nothing is rendered until the next block.
FarListener = Callable[[np.ndarray | None, int], None]


class Playback(Protocol):
    """Speech + UI-effect output.

    Speech is queued per reply and can be stopped instantly; effects (chimes)
    play even while speech is paused (e.g. during push-to-talk).
    """

    samplerate: int  # rate expected by add_speech()/add_effect()

    @property
    def device_rate(self) -> int:
        """The rate the device plays at, and the far listener hears (``samplerate`` unless the device refused it)."""
        ...

    @property
    def device_name(self) -> str | None: ...

    @property
    def level(self) -> float: ...

    @property
    def speech_generation(self) -> int: ...

    @property
    def speech_frames_played(self) -> int: ...

    @property
    def speech_frames_queued(self) -> int: ...

    @property
    def paused(self) -> bool:
        """Speech is held (push-to-talk is down); its frames do not advance."""
        ...

    @property
    def open_error(self) -> AudioError | None:
        """Why the last attempt to open the device failed; None once it opened."""
        ...

    @property
    def output_latency(self) -> float:
        """Seconds from rendering a block to the speaker playing it, as the device reports it (0 if unknown)."""
        ...

    def open(self) -> None:
        """Open the device (idempotent). Raises AudioError."""
        ...

    def close(self) -> None: ...

    def hold_open(self, held: bool) -> None:
        """A reply is open (True) or every reply has finished (False).

        While held, silences (Claude running a tool between sentences) do not
        close the device, up to a cap; afterwards it may close when idle.
        """
        ...

    def set_far_listener(self, listener: FarListener | None) -> None:
        """Also hand every block sent to the device to ``listener`` (the echo canceller), which must return at once.

        ``listener(None, rate)`` follows the last block whenever the stream stops (closed, aborted or restarted).
        """
        ...

    def add_speech(self, pcm: np.ndarray, generation: int | None = None) -> None: ...

    def add_effect(self, pcm: np.ndarray) -> None: ...

    def end_speech(self, generation: int | None = None) -> None:
        """The current reply's audio is complete: flush anything held back (resampler tail)."""
        ...

    def stop_speech(self) -> None:
        """Silence speech now: abort the device buffer and drop everything queued."""
        ...

    def reset_speech_counters(self) -> None: ...

    def speech_idle(self) -> bool: ...

    def set_paused(self, paused: bool) -> None: ...


class _Lane:
    """FIFO of float32 arrays read in arbitrary block sizes."""

    def __init__(self) -> None:
        self.chunks: deque[np.ndarray] = deque()
        self.offset = 0
        self.pending = 0

    def push(self, pcm: np.ndarray) -> None:
        if pcm.size:
            self.chunks.append(pcm)
            self.pending += pcm.size

    def clear(self) -> None:
        self.chunks.clear()
        self.offset = 0
        self.pending = 0

    def pull_into(self, out: np.ndarray, *, add: bool) -> int:
        """Copy (or mix) up to len(out) frames into out; return frames taken."""
        filled = 0
        while filled < out.size and self.chunks:
            head = self.chunks[0]
            take = min(out.size - filled, head.size - self.offset)
            piece = head[self.offset : self.offset + take]
            if add:
                out[filled : filled + take] += piece
            else:
                out[filled : filled + take] = piece
            filled += take
            self.offset += take
            if self.offset >= head.size:
                self.chunks.popleft()
                self.offset = 0
        self.pending -= filled
        return filled


class PlaybackMixer:
    """Thread-safe mixer: ``render()`` is called from the audio thread.

    ``generation`` increases on every ``clear_speech()``; audio tagged with an
    older generation is dropped, so a TTS chunk that was in flight when the
    user hit stop can never sneak out afterwards.
    """

    def __init__(self, samplerate: int = TTS_SAMPLERATE) -> None:
        self.samplerate = samplerate
        self._lock = threading.Lock()
        self._speech = _Lane()
        self._effects = _Lane()
        self.paused = False
        self.generation = 0
        self.frames_played = 0
        self.frames_queued = 0
        self.level = 0.0

    def add_speech(self, pcm: np.ndarray, generation: int | None = None) -> bool:
        pcm = np.asarray(pcm, dtype=np.float32).reshape(-1)
        with self._lock:
            if generation is not None and generation != self.generation:
                return False
            self._speech.push(pcm)
            self.frames_queued += pcm.size
            return True

    def add_effect(self, pcm: np.ndarray) -> None:
        with self._lock:
            self._effects.push(np.asarray(pcm, dtype=np.float32).reshape(-1))

    def clear_speech(self) -> int:
        with self._lock:
            self._speech.clear()
            self.generation += 1
            return self.generation

    def clear_all(self) -> None:
        with self._lock:
            self._speech.clear()
            self._effects.clear()
            self.generation += 1

    def reset_counters(self) -> None:
        with self._lock:
            self.frames_played = 0
            self.frames_queued = 0

    def speech_pending(self) -> int:
        with self._lock:
            return self._speech.pending

    def idle(self) -> bool:
        """Nothing queued at all (speech, even paused, or effects)."""
        with self._lock:
            return not self._speech.pending and not self._effects.pending

    def render(self, frames: int) -> np.ndarray:
        out = np.zeros(frames, dtype=np.float32)
        with self._lock:
            if not self.paused:
                self.frames_played += self._speech.pull_into(out, add=False)
            self._effects.pull_into(out, add=True)
        np.clip(out, -1.0, 1.0, out=out)
        self.level = meter(out)
        return out
