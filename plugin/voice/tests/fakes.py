"""In-memory SpeechSynth for pipeline/daemon tests."""

from __future__ import annotations

import threading
from collections.abc import Callable

import numpy as np

from jarvis_voice.tts.base import AudioCallback, SynthError


def tone_bytes(
    text: str, ms_per_char: float, sample_rate: int = 24_000, voice: Callable[[int], np.ndarray] | None = None
) -> bytes:
    """PCM for ``text``: a 300 Hz tone, or ``voice(samples)`` (float, -1..1)."""
    n = int(len(text) * ms_per_char / 1000 * sample_rate)
    if voice is not None:
        return (np.clip(voice(n), -1.0, 1.0) * 32767).astype("<i2").tobytes()
    t = np.arange(n) / sample_rate
    return (np.sin(2 * np.pi * 300 * t) * 0.3 * 32767).astype("<i2").tobytes()


class FakeStream:
    def __init__(self, synth: FakeSynth, on_audio: AudioCallback, voice_id: str | None) -> None:
        self.sample_rate = 24_000
        self._synth = synth
        self._on_audio = on_audio
        self.voice_id = voice_id
        self.sentences: list[str] = []
        self._done = False
        self._closed = False
        self._error: SynthError | None = None
        self.cancelled = False

    @property
    def done(self) -> bool:
        return self._done

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def error(self) -> SynthError | None:
        return self._error

    def send_text(self, text: str) -> None:
        if self._closed:
            raise SynthError("fish_unreachable", "closed")
        self.sentences.append(text)
        self._synth.log.append(text)
        if self._synth.fail_after is not None and len(self._synth.log) > self._synth.fail_after:
            self._error = SynthError("fish_unreachable", "simulated network drop")
            self._closed = True
            return
        if not self.cancelled:
            # Deliver in two odd-sized pieces, as a network would.
            data = tone_bytes(text, self._synth.ms_per_char, voice=self._synth.voice)
            cut = len(data) // 3 + 1
            self._on_audio(data[:cut])
            self._on_audio(data[cut:])

    def finish(self) -> None:
        self._done = True
        self._closed = True

    def cancel(self) -> None:
        self.cancelled = True
        self._closed = True


class FakeSynth:
    sample_rate = 24_000

    def __init__(
        self,
        *,
        ms_per_char: float = 5.0,
        fail_open: SynthError | None = None,
        fail_after: int | None = None,
        voice: Callable[[int], np.ndarray] | None = None,
    ) -> None:
        self.ms_per_char = ms_per_char
        self.voice = voice
        self.fail_open = fail_open
        self.fail_after = fail_after
        self.streams: list[FakeStream] = []
        self.log: list[str] = []
        self.lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return self.fail_open is None or self.fail_open.code != "fish_key_missing"

    def open_stream(self, on_audio: AudioCallback, *, voice_id: str | None) -> FakeStream:
        if self.fail_open is not None:
            raise self.fail_open
        stream = FakeStream(self, on_audio, voice_id)
        with self.lock:
            self.streams.append(stream)
        return stream


class LoudnessVad:
    """VoiceDetector stand-in: a 32 ms frame is speech when it is louder than -30 dBFS."""

    def __init__(self) -> None:
        self._pending = np.zeros(0, np.float32)

    def reset(self) -> None:
        self._pending = np.zeros(0, np.float32)

    def process(self, audio: np.ndarray) -> list[float]:
        self._pending = np.concatenate([self._pending, audio])
        probs = []
        while self._pending.size >= 512:
            frame, self._pending = self._pending[:512], self._pending[512:]
            probs.append(0.95 if float(np.sqrt(np.mean(frame**2))) > 0.03 else 0.02)
        return probs


class ScriptedWake:
    """WakeScorer stand-in: scores 0.9 on the next 80 ms chunk after ``say()``.

    With ``plain=True`` it also has a plain "Jarvis" model, which scores 0.9
    on the next chunk after ``say_plain()``. Either can wait instead for the
    chunk that reaches ``at`` seconds of audio since the last reset (for audio
    a thread delivers).
    """

    name = "scripted"

    def __init__(self, *, plain: bool = False) -> None:
        self.plain_name = "scripted_plain" if plain else None
        self._pending = 0
        self._heard = 0
        self._armed = False
        self._at = 0
        self._plain_armed = False
        self._plain_at = 0
        self.resets = 0

    def say(self, at: float | None = None) -> None:
        self._armed = True
        self._at = 0 if at is None else int(at * 16_000)

    def attach_plain(self) -> None:
        """The plain model, loaded after the scorer went live."""
        self.plain_name = "scripted_plain"

    def say_plain(self, at: float | None = None) -> None:
        self._plain_armed = True
        self._plain_at = 0 if at is None else int(at * 16_000)

    def reset(self) -> None:
        self._pending = 0
        self._heard = 0
        self.resets += 1

    def process(self, audio: np.ndarray) -> list[float]:
        return [score for score, _plain in self.process_pair(audio)]

    def process_pair(self, audio: np.ndarray) -> list[tuple[float, float | None]]:
        self._pending += audio.size
        scores: list[tuple[float, float | None]] = []
        while self._pending >= 1280:
            self._pending -= 1280
            self._heard += 1280
            woke = self._armed and self._heard >= self._at
            called = self._plain_armed and self._heard >= self._plain_at
            plain = None if self.plain_name is None else (0.9 if called else 0.01)
            scores.append((0.9 if woke else 0.01, plain))
            self._armed = self._armed and not woke
            self._plain_armed = self._plain_armed and not called
        return scores
