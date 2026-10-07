"""The wake words: openWakeWord's streaming pipeline on onnxruntime.

Every 80 ms of 16 kHz audio becomes a score between 0 and 1:

    audio -> melspectrogram.onnx -> 8 new mel frames (x/10 + 2)
          -> embedding_model.onnx over the last 76 mel frames -> one 96-value embedding
          -> hey_jarvis_v0.1.onnx over the last 16 embeddings -> score

A second phrase model (plain "Jarvis") can run on the same embeddings, so it
costs one small classifier per chunk and no second feature pipeline.

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
    plain_name: str | None  # the plain "Jarvis" model, when one is loaded

    def process(self, audio: np.ndarray) -> list[float]:
        """16 kHz float audio in -1..1 -> one score per completed 80 ms chunk."""
        ...

    def process_pair(self, audio: np.ndarray) -> list[tuple[float, float | None]]:
        """Like ``process``, with the plain model's score beside each one (None without that model)."""
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


class _Classifier:
    """One phrase model: the last ``n_features`` embeddings in, a score out."""

    def __init__(self, path: Path) -> None:
        self._session = _session(path)
        first = self._session.get_inputs()[0]
        self._input = first.name
        self.n_features = int(first.shape[1])

    def score(self, features: np.ndarray) -> float:
        window = features[-self.n_features :][None, :, :].astype(np.float32)
        out = self._session.run(None, {self._input: window})[0]
        return float(np.asarray(out).reshape(-1)[0])


class OpenWakeWord:
    def __init__(self, folder: Path, model: ModelFile = HEY_JARVIS, plain: ModelFile | None = None) -> None:
        self._mel = _session(folder / MELSPEC.name)
        self._emb = _session(folder / EMBEDDING.name)
        self._clf = _Classifier(folder / model.name)
        self._plain = _Classifier(folder / plain.name) if plain is not None else None
        self._n_features = max(self._clf.n_features, self._plain.n_features if self._plain else 0)
        self.name = model.name.removesuffix(".onnx")
        self.plain_name = plain.name.removesuffix(".onnx") if plain is not None else None
        self._seed = self._noise_features()
        self.reset()

    def attach_plain(self, folder: Path, plain: ModelFile) -> None:
        """Load the plain "Jarvis" model beside the running one, while another thread may be scoring.

        Each scoring pass reads the model once, and ``plain_name`` is set last,
        so a caller that sees it also gets that model's scores.
        """
        clf = _Classifier(folder / plain.name)
        if clf.n_features > self._n_features:
            raise ValueError(f"{plain.name} needs {clf.n_features} embeddings; this pipeline keeps {self._n_features}")
        self._plain = clf
        self.plain_name = plain.name.removesuffix(".onnx")

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
        return [score for score, _plain in self._run(audio, with_plain=False)]

    def process_pair(self, audio: np.ndarray) -> list[tuple[float, float | None]]:
        return self._run(audio, with_plain=True)

    def _run(self, audio: np.ndarray, *, with_plain: bool) -> list[tuple[float, float | None]]:
        self._pending = np.concatenate([self._pending, to_int16_range(np.asarray(audio, np.float32).reshape(-1))])
        plain = self._plain if with_plain else None
        scores: list[tuple[float, float | None]] = []
        while self._pending.size >= CHUNK:
            chunk, self._pending = self._pending[:CHUNK], self._pending[CHUNK:]
            self._raw = np.concatenate([self._raw, chunk])[-(CHUNK + MEL_CONTEXT) :]
            self._mels = np.vstack([self._mels, self._melspec(self._raw)])[-WINDOW:]
            embedding = self._embed(self._mels[None, :, :])
            self._features = np.vstack([self._features, embedding])[-self._n_features :]
            self._chunks += 1
            warm = self._chunks > WARMUP_CHUNKS
            score = self._clf.score(self._features) if warm else 0.0
            plain_score = None if plain is None else (plain.score(self._features) if warm else 0.0)
            scores.append((score, plain_score))
        return scores
