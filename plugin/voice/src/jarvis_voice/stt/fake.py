"""Deterministic transcriber for tests and ``--fake-stt`` mode."""

from __future__ import annotations

import time

import numpy as np

from ..audio.levels import is_silent
from .base import SttDevice, SttError, Transcript


class FakeTranscriber:
    def __init__(
        self,
        text: str = "Hello Jarvis, this is a test.",
        *,
        name: str = "fake",
        device: SttDevice = "cpu",
        load_delay: float = 0.0,
        fail: SttError | None = None,
    ) -> None:
        self.text = text
        self._name = name
        self._device: SttDevice = device
        self.load_delay = load_delay
        self.fail = fail
        self.calls: list[tuple[int, str | None]] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def device(self) -> SttDevice:
        return self._device

    def load(self) -> None:
        if self.load_delay:
            time.sleep(self.load_delay)
        if self.fail is not None:
            raise self.fail

    def transcribe(self, pcm16k: np.ndarray, language: str | None) -> Transcript:
        self.calls.append((int(pcm16k.size), language))
        text = "" if is_silent(pcm16k) else self.text
        return Transcript(text=text, language=language or "en")
