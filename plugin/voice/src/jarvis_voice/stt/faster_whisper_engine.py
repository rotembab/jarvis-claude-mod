"""faster-whisper (CTranslate2) transcriber: CUDA float16 when available, else CPU int8."""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import Any

import numpy as np

from .base import SttDevice, SttError, Transcript
from .cuda_probe import CACHE_FILE, CudaProbe
from .models import (
    AUTO,
    CPU_DEFAULT_MODEL,
    CUDA_DEFAULT_MODEL,
    compute_type,
    detect_device,
    local_model_path,
    register_cuda_dll_dirs,
    resolve_model,
)

log = logging.getLogger(__name__)

SETUP_HINT = "Run /jarvis setup to download the speech model."


class FasterWhisperTranscriber:
    def __init__(
        self,
        requested_model: str,
        cache_dir: Path,
        *,
        device: str = "auto",
        cpu_threads: int = 0,
        cuda_probe: CudaProbe | None = None,
        reprobe_cuda: bool = False,
    ) -> None:
        self.requested_model = requested_model or AUTO
        self._cache_dir = cache_dir
        self._device_pref = device
        self._cpu_threads = cpu_threads
        self._probe = cuda_probe or CudaProbe(cache_dir / CACHE_FILE)
        self._reprobe = reprobe_cuda  # setup re-tests the GPU instead of trusting the cache
        self._model: Any = None
        self._name = resolve_model(self.requested_model, "cpu")
        self._device: SttDevice = "cpu"

    @property
    def name(self) -> str:
        return self._name

    @property
    def device(self) -> SttDevice:
        return self._device

    def _attempts(self, device: SttDevice) -> list[tuple[SttDevice, str]]:
        wanted = resolve_model(self.requested_model, device)
        if device == "cpu":
            return [("cpu", wanted)]
        # If the GPU path fails (driver, missing cuBLAS DLLs, out of memory) fall back to CPU.
        if self.requested_model.lower() == AUTO:
            return [("cuda", CUDA_DEFAULT_MODEL), ("cpu", CPU_DEFAULT_MODEL), ("cpu", CUDA_DEFAULT_MODEL)]
        return [("cuda", wanted), ("cpu", wanted)]

    def load(self) -> None:
        register_cuda_dll_dirs()
        device: SttDevice = self._device_pref if self._device_pref in ("cuda", "cpu") else detect_device()  # type: ignore[assignment]
        try:
            from faster_whisper import WhisperModel
        except Exception as exc:  # broken install
            raise SttError("stt_failed", f"faster-whisper is not importable: {exc}", SETUP_HINT) from exc

        missing: list[str] = []
        last_exc: Exception | None = None
        for dev, name in self._attempts(device):
            path = local_model_path(name, self._cache_dir)
            if path is None:
                missing.append(name)
                continue
            if dev == "cuda":
                # A broken CUDA setup can abort the process, so a child tries it first.
                probe = self._probe.check(path, force=self._reprobe)
                if not probe.ok:
                    log.warning("the GPU cannot run %s (%s)", name, probe.detail)
                    last_exc = RuntimeError(f"GPU test failed: {probe.detail}")
                    continue
            guard = self._probe.in_process_load(path) if dev == "cuda" else contextlib.nullcontext()
            try:
                with guard:
                    model = WhisperModel(
                        str(path), device=dev, compute_type=compute_type(dev), cpu_threads=self._cpu_threads
                    )
                    warm_up(model)
            except Exception as exc:  # noqa: BLE001 - try the next option
                log.warning("loading %s on %s failed: %s", name, dev, exc)
                last_exc = exc
                continue
            self._model, self._name, self._device = model, name, dev
            if dev != device:
                log.warning("speech model running on %s instead of %s", dev, device)
            log.info("speech model ready: %s on %s (%s)", name, dev, compute_type(dev))
            return
        if last_exc is None:
            raise SttError("stt_model_missing", f"Speech model {missing[0]!r} is not downloaded.", SETUP_HINT)
        raise SttError("stt_failed", f"Could not load the speech model: {last_exc}", SETUP_HINT)

    def transcribe(self, pcm16k: np.ndarray, language: str | None) -> Transcript:
        if self._model is None:
            raise SttError("stt_failed", "speech model is not loaded")
        lang = language.strip().lower() if language else None
        if lang in ("", AUTO):
            lang = None
        try:
            segments, info = self._model.transcribe(
                np.asarray(pcm16k, dtype=np.float32),
                language=lang,
                beam_size=1,  # greedy: lowest latency, negligible accuracy cost for commands
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 500},
                condition_on_previous_text=False,
                without_timestamps=True,
            )
            text = " ".join(seg.text.strip() for seg in segments).strip()
        except Exception as exc:
            raise SttError("stt_failed", f"transcription failed: {exc}") from exc
        return Transcript(text=text, language=getattr(info, "language", None))


def warm_up(model: Any) -> None:
    """One tiny decode: proves the device works now (cuBLAS/cuDNN load lazily) and primes caches."""
    noise = (np.random.default_rng(0).standard_normal(16_000) * 1e-3).astype(np.float32)
    segments, _ = model.transcribe(noise, language="en", beam_size=1, vad_filter=False, without_timestamps=True)
    for _ in segments:  # the result is a lazy generator
        pass


def load_on_cuda(model_path: Path) -> None:
    """Body of ``probe-cuda``: load and warm up a model on the GPU in this process (may abort it)."""
    register_cuda_dll_dirs()
    from faster_whisper import WhisperModel

    warm_up(WhisperModel(str(model_path), device="cuda", compute_type=compute_type("cuda")))
