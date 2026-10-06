"""Text-to-speech interface: streaming text in, PCM chunks out, cancellable."""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal, Protocol

import numpy as np

SynthErrorCode = Literal["fish_key_missing", "fish_auth_failed", "fish_unreachable", "internal"]


class SynthError(Exception):
    def __init__(self, code: SynthErrorCode, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.code: SynthErrorCode = code
        self.message = message
        self.hint = hint


class SynthStream(Protocol):
    """One reply's synthesis session (for Fish: one WebSocket)."""

    sample_rate: int

    def send_text(self, text: str) -> None:
        """Queue one sentence and ask for audio now. Raises SynthError."""
        ...

    def finish(self) -> None:
        """No more text; audio for what was sent still arrives."""
        ...

    def cancel(self) -> None:
        """Abort immediately; no more audio callbacks after this returns."""
        ...

    @property
    def done(self) -> bool:
        """The service confirmed all audio was delivered."""
        ...

    @property
    def closed(self) -> bool:
        """The session ended (done, cancelled, failed or closed by the server)."""
        ...

    @property
    def error(self) -> SynthError | None: ...


AudioCallback = Callable[[bytes], None]


class SpeechSynth(Protocol):
    sample_rate: int

    @property
    def configured(self) -> bool:
        """Credentials are present (it may still fail at connect time)."""
        ...

    def open_stream(self, on_audio: AudioCallback, *, voice_id: str | None) -> SynthStream:
        """Connect and start a session. Raises SynthError."""
        ...


class Pcm16Decoder:
    """16-bit little-endian PCM bytes -> float32 samples, carrying odd bytes across chunks."""

    def __init__(self) -> None:
        self._carry = b""

    def feed(self, data: bytes) -> np.ndarray:
        data = self._carry + data
        usable = len(data) - (len(data) % 2)
        self._carry = data[usable:]
        if not usable:
            return np.zeros(0, dtype=np.float32)
        return np.frombuffer(data[:usable], dtype="<i2").astype(np.float32) / 32768.0
