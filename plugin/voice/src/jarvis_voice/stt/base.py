"""Speech-to-text interface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

import numpy as np

SttDevice = Literal["cuda", "cpu"]


@dataclass(frozen=True, slots=True)
class Transcript:
    text: str
    language: str | None = None


class SttError(Exception):
    def __init__(self, code: Literal["stt_model_missing", "stt_failed"], message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.code: Literal["stt_model_missing", "stt_failed"] = code
        self.message = message
        self.hint = hint


class Transcriber(Protocol):
    """A loaded speech-to-text model. ``transcribe`` is called from one worker thread."""

    @property
    def name(self) -> str:
        """Resolved model name (never "auto")."""
        ...

    @property
    def device(self) -> SttDevice: ...

    def load(self) -> None:
        """Load (and warm up) the model. Raises SttError."""
        ...

    def transcribe(self, pcm16k: np.ndarray, language: str | None) -> Transcript: ...
