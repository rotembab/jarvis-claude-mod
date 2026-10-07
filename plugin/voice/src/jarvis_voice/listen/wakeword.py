"""The "Hey Jarvis" wake word: openWakeWord's streaming pipeline on onnxruntime.

Every 80 ms of 16 kHz audio becomes a score between 0 and 1:

    audio -> melspectrogram.onnx -> 8 new mel frames (x/10 + 2)
          -> embedding_model.onnx over the last 76 mel frames -> one 96-value embedding
          -> hey_jarvis_v0.1.onnx over the last 16 embeddings -> score

This is a port of ``openwakeword.utils.AudioFeatures`` and ``Model.predict``
(openWakeWord, Apache License 2.0, by David Scripka), reduced to the one
streaming path the helper uses. The package itself is not a dependency: on
Linux it requires tflite-runtime, which has no Python 3.12 wheels, and it
pulls in scipy and scikit-learn for features Jarvis does not use.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

import numpy as np

from .models import EMBEDDING, HEY_JARVIS, MELSPEC, ModelFile

CHUNK = 1280  # 80 ms at 16 kHz: one score per chunk
MEL_CONTEXT = 480  # the melspectrogram model needs three 10 ms hops of history
WINDOW = 76  # mel frames per embedding
WARMUP_CHUNKS = 5  # openWakeWord reports 0 for the first five scores


class WakeScorer(Protocol):
    name: str

    def process(self, audio: np.ndarray) -> list[float]:
        """16 kHz float audio in -1..1 -> one score per completed 80 ms chunk."""
        ...

    def reset(self) -> None: ...


def _session(path: Path) -> Any:
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.inter_op_num_threads = 1
    options.intra_op_num_threads = 1
    options.log_severity_level = 3
    return ort.InferenceSession(str(path), sess_options=options, providers=["CPUExecutionProvider"])


def to_int16_range(audio: np.ndarray) -> np.ndarray:
    """Float audio -> the int16 sample values the models were trained on, as float32."""
    return (np.clip(audio, -1.0, 1.0) * 32767.0).astype(np.int16).astype(np.float32)


class OpenWakeWord:
    def __init__(self, folder: Path, model: ModelFile = HEY_JARVIS) -> None:
        self._mel = _session(folder / MELSPEC.name)
        self._emb = _session(folder / EMBEDDING.name)
        self._clf = _session(folder / model.name)
        first = self._clf.get_inputs()[0]
        self._clf_input = first.name
        self._n_features = int(first.shape[1])
        self.name = model.name.removesuffix(".onnx")
        self._seed = self._noise_features()
        self.reset()

    def _melspec(self, samples: np.ndarray) -> np.ndarray:
        out = self._mel.run(None, {"input": samples[None, :]})[0]
        return np.squeeze(out).reshape(-1, 32) / 10.0 + 2.0

    def _embed(self, windows: np.ndarray) -> np.ndarray:
        out = self._emb.run(None, {"input_1": windows[..., None].astype(np.float32)})[0]
        return out.reshape(windows.shape[0], -1)

    def _noise_features(self) -> np.ndarray:
        # Like openWakeWord, start from the embeddings of 4 s of faint noise, so
        # the first scores already see a full window of (meaningless) features.
        noise = np.random.default_rng(0).integers(-1000, 1000, 16_000 * 4).astype(np.float32)
        spec = self._melspec(noise)
        windows = np.array([spec[i : i + WINDOW] for i in range(0, spec.shape[0] - WINDOW + 1, 8)])
        return self._embed(windows)

    def reset(self) -> None:
        self._raw = np.zeros(0, np.float32)
        self._pending = np.zeros(0, np.float32)
        self._mels = np.ones((WINDOW, 32), np.float32)
        self._features = self._seed[-self._n_features :].copy()
        self._chunks = 0

    def process(self, audio: np.ndarray) -> list[float]:
        self._pending = np.concatenate([self._pending, to_int16_range(np.asarray(audio, np.float32).reshape(-1))])
        scores: list[float] = []
        while self._pending.size >= CHUNK:
            chunk, self._pending = self._pending[:CHUNK], self._pending[CHUNK:]
            self._raw = np.concatenate([self._raw, chunk])[-(CHUNK + MEL_CONTEXT) :]
            self._mels = np.vstack([self._mels, self._melspec(self._raw)])[-WINDOW:]
            embedding = self._embed(self._mels[None, :, :])
            self._features = np.vstack([self._features, embedding])[-self._n_features :]
            out = self._clf.run(None, {self._clf_input: self._features[None, :, :].astype(np.float32)})[0]
            self._chunks += 1
            scores.append(float(np.asarray(out).reshape(-1)[0]) if self._chunks > WARMUP_CHUNKS else 0.0)
        return scores
