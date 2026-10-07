"""The wake word models: where they live, and downloading them.

openWakeWord's "Hey Jarvis" needs three small ONNX files (about 3.7 MB in all):
two shared feature models and the phrase model itself. They come from the
openWakeWord v0.5.1 release on GitHub and are checked against fixed SHA-256
sums. The feature models are Apache-2.0; the "Hey Jarvis" model is licensed
CC BY-NC-SA 4.0 (personal, non-commercial use), which is why they are
downloaded on the user's machine rather than shipped in the repository.

Plain "Jarvis" is a second phrase model (about 200 KB) on the same two
feature models: ``jarvis_v2.onnx`` from the community collection
github.com/fwartner/home-assistant-wakewords-collection (``en/jarvis``),
pinned to one commit and checked against its SHA-256 sum like the others. That
repository is MIT-licensed, but the model was likely trained with openWakeWord's
tooling and CC BY-NC-SA data, so treat it as personal, non-commercial use too;
it is downloaded on the user's machine for the same reason: by ``/jarvis
setup``, or by the helper when plain "Jarvis" is switched on.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

RELEASE_URL = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1"
WAKE_PHRASE = "Hey Jarvis"
PLAIN_WAKE_PHRASE = "Jarvis"
DOWNLOAD_TIMEOUT_S = 60.0
# The running helper's own try for plain "Jarvis" (about 200 KB): short, so a
# blocked or silent host costs seconds, not a minute. /jarvis setup uses the long one.
PLAIN_FETCH_TIMEOUT_S = 10.0
# ...and a limit on the whole fetch, so a host that trickles bytes cannot hold it forever.
PLAIN_FETCH_DEADLINE_S = 60.0


@dataclass(frozen=True, slots=True)
class ModelFile:
    name: str
    sha256: str
    size: int
    source: str | None = None  # where to download it; None: the openWakeWord release

    @property
    def url(self) -> str:
        return self.source or f"{RELEASE_URL}/{self.name}"


MELSPEC = ModelFile(
    "melspectrogram.onnx", "ba2b0e0f8b7b875369a2c89cb13360ff53bac436f2895cced9f479fa65eb176f", 1_087_958
)
EMBEDDING = ModelFile(
    "embedding_model.onnx", "70d164290c1d095d1d4ee149bc5e00543250a7316b59f31d056cff7bd3075c1f", 1_326_578
)
HEY_JARVIS = ModelFile(
    "hey_jarvis_v0.1.onnx", "94a13cfe60075b132f6a472e7e462e8123ee70861bc3fb58434a73712ee0d2cb", 1_271_370
)
JARVIS_V2 = ModelFile(
    "jarvis_v2.onnx",
    "dae408c0fa69ec888bf8e3a8b41a41f97677522be3b8163821e4105fa754b988",
    208_134,
    source=(
        "https://raw.githubusercontent.com/fwartner/home-assistant-wakewords-collection/"
        "1c6d6a82947ff1367d8e5e50cc999c00943ccb9c/en/jarvis/jarvis_v2.onnx"
    ),
)
HEY_JARVIS_FILES = (MELSPEC, EMBEDDING, HEY_JARVIS)
PLAIN_JARVIS_FILES = (MELSPEC, EMBEDDING, JARVIS_V2)
FILES = HEY_JARVIS_FILES


def wake_dir(models_dir: Path) -> Path:
    return models_dir / "wake"


def is_downloaded(folder: Path, files: tuple[ModelFile, ...] = FILES) -> bool:
    return all((folder / f.name).is_file() and (folder / f.name).stat().st_size == f.size for f in files)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(
    folder: Path,
    on_progress: Callable[[str], None] | None = None,
    files: tuple[ModelFile, ...] = FILES,
    *,
    timeout: float = DOWNLOAD_TIMEOUT_S,
    deadline: float | None = None,
) -> Path:
    """Fetch any missing model file into ``folder``. Raises OSError (or ValueError on a bad checksum).

    ``timeout`` bounds each wait for data; ``deadline`` (a ``time.monotonic()``
    value) bounds the whole download: past it, TimeoutError.
    """
    folder.mkdir(parents=True, exist_ok=True)
    for model in files:
        target = folder / model.name
        if target.is_file() and target.stat().st_size == model.size:
            continue
        if on_progress is not None:
            on_progress(f"Downloading {model.name}")
        partial = target.with_name(target.name + ".part")
        request = urllib.request.Request(model.url, headers={"User-Agent": "jarvis-voice"})
        with urllib.request.urlopen(request, timeout=timeout) as response, partial.open("wb") as out:
            # read1: whatever has arrived, so a trickle comes back here to meet the deadline.
            while chunk := response.read1(1 << 16):
                out.write(chunk)
                if deadline is not None and time.monotonic() > deadline:
                    raise TimeoutError(f"{model.name} was still downloading when time ran out")
        got = _sha256(partial)
        if got != model.sha256:
            partial.unlink(missing_ok=True)
            raise ValueError(f"{model.name} failed its checksum (got {got[:12]}..., expected {model.sha256[:12]}...)")
        os.replace(partial, target)
        log.info("downloaded %s", target)
    return folder
