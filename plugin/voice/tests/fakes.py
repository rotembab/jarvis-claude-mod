"""In-memory SpeechSynth for pipeline/daemon tests."""

from __future__ import annotations

import threading

import numpy as np

from jarvis_voice.tts.base import AudioCallback, SynthError


def tone_bytes(text: str, ms_per_char: float, sample_rate: int = 24_000) -> bytes:
    n = int(len(text) * ms_per_char / 1000 * sample_rate)
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
            data = tone_bytes(text, self._synth.ms_per_char)
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
        self, *, ms_per_char: float = 5.0, fail_open: SynthError | None = None, fail_after: int | None = None
    ) -> None:
        self.ms_per_char = ms_per_char
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
