"""Chatterbox-Turbo (Resemble AI, MIT): a 350M-parameter English TTS that copies a voice from one clip.

Imported only by the real server process; torch and chatterbox are heavy.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger("jarvis_local_voice")

REPO_ID = "ResembleAI/chatterbox-turbo"
MODEL_DIRNAME = "chatterbox-turbo"
# The weights Turbo loads. The repo also holds the non-Turbo s3gen.safetensors
# (about 1 GB), which from_pretrained would download for nothing.
WEIGHTS = ("t3_turbo_v1.safetensors", "s3gen_meanflow.safetensors", "ve.safetensors")
PATTERNS = [*WEIGHTS, "conds.pt", "*.json", "*.txt", "*.model"]
MIN_CLIP_SECONDS = 5.0  # Chatterbox refuses shorter reference clips; 10 to 20 s is best


def model_dir(models_dir: Path) -> Path:
    return models_dir / MODEL_DIRNAME


def is_downloaded(models_dir: Path) -> bool:
    folder = model_dir(models_dir)
    return all((folder / name).is_file() for name in WEIGHTS) and any(folder.glob("*.json"))


def download(models_dir: Path) -> Path:
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    from huggingface_hub import snapshot_download

    folder = model_dir(models_dir)
    folder.mkdir(parents=True, exist_ok=True)
    # local_dir keeps plain files (no symlinks, which Windows only allows in developer mode).
    snapshot_download(REPO_ID, local_dir=str(folder), allow_patterns=PATTERNS)
    return folder


def pick_device(requested: str) -> str:
    import torch

    if requested != "auto":
        return requested
    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


class ChatterboxBackend:
    def __init__(self, models_dir: Path, voice: Path | None, device: str = "auto") -> None:
        import torch
        from chatterbox.tts_turbo import ChatterboxTurboTTS

        folder = model_dir(models_dir)
        if not is_downloaded(models_dir):
            raise RuntimeError(f"the local voice model is not downloaded to {folder}; run /jarvis setup local")
        self._torch: Any = torch
        self.device = pick_device(device)
        log.info("loading Chatterbox-Turbo from %s on %s", folder, self.device)
        self._model: Any = ChatterboxTurboTTS.from_local(folder, self.device)
        self.sample_rate = int(self._model.sr)
        if voice is None:
            self.voice = "built-in"
        else:
            if not voice.is_file():
                raise FileNotFoundError(f"voice clip not found: {voice}")
            try:
                self._model.prepare_conditionals(str(voice))
            except AssertionError as exc:  # Chatterbox's own check: "Audio prompt must be longer than 5 seconds!"
                raise ValueError(f"the voice clip {voice.name} is too short: {exc}") from exc
            self.voice = voice.name
        self.synthesize("Ready.")  # the first call is slow (CUDA kernels); take it now, not on the first reply

    def synthesize(self, text: str) -> bytes:
        import numpy as np

        with self._torch.inference_mode():
            wav = self._model.generate(text)
        samples = wav.squeeze(0).detach().cpu().numpy()
        return (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
