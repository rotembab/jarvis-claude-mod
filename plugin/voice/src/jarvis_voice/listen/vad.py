"""Voice activity: Silero VAD, streamed 32 ms at a time.

The model is the ONNX file faster-whisper ships for its own VAD filter
(``faster_whisper/assets``), so nothing extra is downloaded. Each call takes
512 new samples at 16 kHz plus the last 64 of the previous call, and carries
the LSTM state forward, which is how faster-whisper's batch wrapper chains
its windows too.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

import numpy as np

FRAME = 512  # 32 ms at 16 kHz
CONTEXT = 64


class VoiceDetector(Protocol):
    def process(self, audio: np.ndarray) -> list[float]:
        """16 kHz float audio -> one speech probability per completed 32 ms frame."""
        ...

    def reset(self) -> None: ...


def silero_model_path() -> Path:
    import faster_whisper

    assets = Path(faster_whisper.__file__).resolve().parent / "assets"
    found = sorted(assets.glob("silero_vad*.onnx"))
    if not found:
        raise FileNotFoundError(f"no Silero VAD model in {assets}")
    return found[-1]


class SileroVad:
    def __init__(self, path: Path | None = None) -> None:
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 1
        options.log_severity_level = 3
        self._session: Any = ort.InferenceSession(
            str(path or silero_model_path()), sess_options=options, providers=["CPUExecutionProvider"]
        )
        self.reset()

    def reset(self) -> None:
        self._h = np.zeros((1, 1, 128), np.float32)
        self._c = np.zeros((1, 1, 128), np.float32)
        self._context = np.zeros(CONTEXT, np.float32)
        self._pending = np.zeros(0, np.float32)

    def process(self, audio: np.ndarray) -> list[float]:
        self._pending = np.concatenate([self._pending, np.asarray(audio, np.float32).reshape(-1)])
        probs: list[float] = []
        while self._pending.size >= FRAME:
            frame, self._pending = self._pending[:FRAME], self._pending[FRAME:]
            x = np.concatenate([self._context, frame])[None, :]
            out, self._h, self._c = self._session.run(None, {"input": x, "h": self._h, "c": self._c})
            self._context = frame[-CONTEXT:]
            probs.append(float(np.asarray(out).reshape(-1)[0]))
        return probs
