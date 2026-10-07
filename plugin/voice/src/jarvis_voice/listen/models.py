"""The wake word models: where they live, and downloading them.

openWakeWord's "Hey Jarvis" needs three small ONNX files (about 3.7 MB in all):
two shared feature models and the phrase model itself. They come from the
openWakeWord v0.5.1 release on GitHub and are checked against fixed SHA-256
sums. The feature models are Apache-2.0; the "Hey Jarvis" model is licensed
CC BY-NC-SA 4.0 (personal, non-commercial use), which is why they are
downloaded on the user's machine rather than shipped in the repository.
"""

from __future__ import annotations

import hashlib
import logging
import os
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

RELEASE_URL = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1"
WAKE_PHRASE = "Hey Jarvis"
DOWNLOAD_TIMEOUT_S = 60.0


@dataclass(frozen=True, slots=True)
class ModelFile:
    name: str
    sha256: str
    size: int

    @property
    def url(self) -> str:
        return f"{RELEASE_URL}/{self.name}"


MELSPEC = ModelFile(
    "melspectrogram.onnx", "ba2b0e0f8b7b875369a2c89cb13360ff53bac436f2895cced9f479fa65eb176f", 1_087_958
)
EMBEDDING = ModelFile(
    "embedding_model.onnx", "70d164290c1d095d1d4ee149bc5e00543250a7316b59f31d056cff7bd3075c1f", 1_326_578
)
HEY_JARVIS = ModelFile(
    "hey_jarvis_v0.1.onnx", "94a13cfe60075b132f6a472e7e462e8123ee70861bc3fb58434a73712ee0d2cb", 1_271_370
)
FILES = (MELSPEC, EMBEDDING, HEY_JARVIS)


def wake_dir(models_dir: Path) -> Path:
    return models_dir / "wake"


def is_downloaded(folder: Path) -> bool:
    return all((folder / f.name).is_file() and (folder / f.name).stat().st_size == f.size for f in FILES)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(folder: Path, on_progress: Callable[[str], None] | None = None) -> Path:
    """Fetch any missing model file into ``folder``. Raises OSError (or ValueError on a bad checksum)."""
    folder.mkdir(parents=True, exist_ok=True)
    for model in FILES:
        target = folder / model.name
        if target.is_file() and target.stat().st_size == model.size:
            continue
        if on_progress is not None:
            on_progress(f"Downloading {model.name}")
        partial = target.with_name(target.name + ".part")
        request = urllib.request.Request(model.url, headers={"User-Agent": "jarvis-voice"})
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_S) as response, partial.open("wb") as out:
            while chunk := response.read(1 << 16):
                out.write(chunk)
        got = _sha256(partial)
        if got != model.sha256:
            partial.unlink(missing_ok=True)
            raise ValueError(f"{model.name} failed its checksum (got {got[:12]}..., expected {model.sha256[:12]}...)")
        os.replace(partial, target)
        log.info("downloaded %s", target)
    return folder
