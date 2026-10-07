"""The MediaPipe hand landmark model: where it lives, and downloading it.

``hand_landmarker.task`` (about 7.8 MB, Apache-2.0) comes from Google's model
bucket and is pinned by size and SHA-256, so a changed or truncated file is
refused rather than handed to MediaPipe. It downloads to a ``.part`` file
that is only renamed into place once its checksum matches, so an interrupted
setup never leaves a half model behind. urllib's default opener honours
``HTTPS_PROXY``, which is all a proxied network needs.
"""

from __future__ import annotations

import hashlib
import http.client
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import __version__

log = logging.getLogger(__name__)

MODEL_NAME = "hand_landmarker.task"
MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task"
)
MODEL_SHA256 = "fbc2a30080c3c557093b5ddfc334698132eb341044ccee322ccf8bcf3607cde1"
MODEL_SIZE = 7_819_105

CHUNK_BYTES = 64 * 1024
#: A misbehaving server must not fill the disk.
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
DOWNLOAD_MESSAGE = "downloading the hand model"

Progress = Callable[[dict[str, Any]], None]


class ModelError(Exception):
    """The model could not be downloaded or failed its checks; ``str()`` is readable as is."""


def models_dir(data_dir: Path) -> Path:
    return data_dir / "models" / "hands"


def model_path(data_dir: Path) -> Path:
    return models_dir(data_dir) / MODEL_NAME


def is_installed(data_dir: Path) -> bool:
    """The model file is in place at its pinned size (its checksum was checked when it was downloaded)."""
    path = model_path(data_dir)
    try:
        return path.is_file() and path.stat().st_size == MODEL_SIZE
    except OSError:
        return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _report(progress: Progress | None, step: str, message: str, pct: float | None = None) -> None:
    if progress is None:
        return
    payload: dict[str, Any] = {"type": "progress", "step": step}
    if pct is not None:
        payload["pct"] = float(round(pct, 1))
    payload["message"] = message
    progress(payload)


def _download(url: str, partial: Path, expected: int | None, progress: Progress | None, timeout: float) -> None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("https", "http"):
        raise ModelError(f"refusing to download the hand model from {url!r}: not an http(s) URL")
    host = parts.hostname or url
    request = urllib.request.Request(url, headers={"User-Agent": f"jarvis-hands/{__version__}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response, partial.open("wb") as out:
            header = response.headers.get("Content-Length")
            total = int(header) if header and header.isdigit() else expected
            received = 0
            next_report = 5.0
            _report(progress, "download", DOWNLOAD_MESSAGE, 0.0)
            while chunk := response.read(CHUNK_BYTES):
                received += len(chunk)
                if received > MAX_DOWNLOAD_BYTES:
                    raise ModelError(f"the hand model download from {host} is far larger than expected; stopped")
                out.write(chunk)
                if total:
                    pct = min(100.0, 100.0 * received / total)
                    if pct >= next_report:
                        _report(progress, "download", DOWNLOAD_MESSAGE, pct)
                        next_report = (pct // 5 + 1) * 5
    except urllib.error.HTTPError as exc:
        raise ModelError(f"the model server answered HTTP {exc.code} {exc.reason} for {url}") from exc
    except urllib.error.URLError as exc:
        raise ModelError(
            f"could not reach {host} to download the hand model ({exc.reason}). "
            "Check the internet connection (and HTTPS_PROXY if you use a proxy), then try again."
        ) from exc
    except TimeoutError as exc:
        raise ModelError(f"the hand model download from {host} timed out; try again") from exc
    except http.client.HTTPException as exc:  # a garbled or truncated response
        raise ModelError(f"the hand model download from {host} failed: {type(exc).__name__}: {exc}") from exc
    except OSError as exc:  # a reset connection mid-transfer, or the disk refused the write
        raise ModelError(f"the hand model download failed: {exc}") from exc
    if header and header.isdigit() and received != int(header):
        raise ModelError(f"the hand model download was cut short ({received} of {header} bytes); try again")


def ensure_model(
    data_dir: Path,
    *,
    url: str = MODEL_URL,
    sha256: str = MODEL_SHA256,
    progress: Progress | None = None,
    timeout: float = 30.0,
) -> Path:
    """Return the model's path, downloading it first unless a file with the right checksum is already there.

    ``progress`` gets ``{"type": "progress", "step": "download", "pct": ..., "message": ...}``
    dicts at most every 5 percent, then one ``verify`` step. Raises ModelError.
    """
    sha256 = sha256.lower()
    target = model_path(data_dir)
    if target.is_file():
        _report(progress, "verify", "checking the installed hand model")
        try:
            if _sha256(target) == sha256:
                return target
        except OSError as exc:
            log.warning("cannot read %s (%s); downloading it again", target, exc)
        else:
            log.warning("%s does not match its checksum; downloading it again", target)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ModelError(f"cannot create {target.parent}: {exc}") from exc
    partial = target.with_name(target.name + ".part")
    expected = MODEL_SIZE if sha256 == MODEL_SHA256 else None
    done = False
    try:
        log.info("downloading %s to %s", url, target)
        _download(url, partial, expected, progress, timeout)
        size = partial.stat().st_size
        if expected is not None and size != expected:
            raise ModelError(f"the hand model has the wrong size ({size} bytes, expected {expected}); try again")
        _report(progress, "verify", "checking the hand model")
        got = _sha256(partial)
        if got != sha256:
            raise ModelError(
                f"the downloaded hand model failed its checksum (got {got[:12]}..., expected {sha256[:12]}...)"
            )
        os.replace(partial, target)
        done = True
    except OSError as exc:
        raise ModelError(f"could not save the hand model in {target.parent}: {exc}") from exc
    finally:
        if not done:
            try:
                partial.unlink(missing_ok=True)
            except OSError:
                log.warning("could not remove %s", partial)
    log.info("hand model ready: %s", target)
    return target
