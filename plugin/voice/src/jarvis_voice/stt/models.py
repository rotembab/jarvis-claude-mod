"""Model names, device detection and the CUDA DLL setup for faster-whisper."""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
from pathlib import Path
from typing import Any

from .base import SttDevice

log = logging.getLogger(__name__)

AUTO = "auto"
CUDA_DEFAULT_MODEL = "large-v3-turbo"
CPU_DEFAULT_MODEL = "small.en"

# The files faster-whisper needs from a CTranslate2 model repo (mirrors faster_whisper.utils.download_model).
MODEL_FILES = ["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*"]

_dll_handles: list[Any] = []  # keep os.add_dll_directory handles alive for the process lifetime
_dll_dirs: list[str] | None = None


def register_cuda_dll_dirs() -> list[str]:
    """On Windows, make the DLLs from the nvidia-* pip wheels (``cuda`` extra) loadable.

    The wheels put ``cublas64_12.dll`` and friends in ``site-packages/nvidia/<lib>/bin``,
    which is on no search path. We add each such directory with
    ``os.add_dll_directory`` (used by extension-module imports) and prepend it to
    PATH (used by libraries that call LoadLibrary themselves). Must run before
    ctranslate2 is imported. No-op elsewhere; idempotent.
    """
    global _dll_dirs
    if _dll_dirs is not None:
        return _dll_dirs
    found: list[str] = []
    if sys.platform == "win32":
        spec = importlib.util.find_spec("nvidia")
        roots = list(spec.submodule_search_locations or []) if spec else []
        for root in roots:
            for bin_dir in sorted(Path(root).glob("*/bin")):
                if not any(bin_dir.glob("*.dll")):
                    continue
                path = str(bin_dir)
                try:
                    _dll_handles.append(os.add_dll_directory(path))  # type: ignore[attr-defined]
                except OSError as exc:
                    log.warning("cannot add DLL directory %s: %s", path, exc)
                    continue
                found.append(path)
        if found:
            os.environ["PATH"] = os.pathsep.join([*found, os.environ.get("PATH", "")])
            log.info("registered CUDA DLL directories: %s", found)
    _dll_dirs = found
    return found


def cuda_device_count() -> int:
    """Number of CUDA devices CTranslate2 can see (0 when none or on error)."""
    register_cuda_dll_dirs()
    try:
        import ctranslate2

        return int(ctranslate2.get_cuda_device_count())
    except Exception as exc:  # noqa: BLE001 - any failure means "no usable GPU"
        log.info("CUDA not available: %s", exc)
        return 0


def detect_device() -> SttDevice:
    return "cuda" if cuda_device_count() > 0 else "cpu"


def resolve_model(requested: str | None, device: SttDevice) -> str:
    """'auto' -> large-v3-turbo on CUDA, small.en on CPU; anything else verbatim."""
    name = (requested or AUTO).strip()
    if name.lower() == AUTO:
        return CUDA_DEFAULT_MODEL if device == "cuda" else CPU_DEFAULT_MODEL
    return name


def compute_type(device: SttDevice) -> str:
    return "float16" if device == "cuda" else "int8"


def models_dir(data_dir: Path) -> Path:
    return data_dir / "models"


def repo_id(name: str) -> str:
    """Hugging Face repo for a faster-whisper size name (or the name if it is already a repo id)."""
    if "/" in name:
        return name
    register_cuda_dll_dirs()  # importing faster_whisper imports ctranslate2
    from faster_whisper.utils import _MODELS

    try:
        return str(_MODELS[name])
    except KeyError:
        raise ValueError(f"unknown faster-whisper model {name!r}; expected one of {', '.join(_MODELS)}") from None


def cache_folder(name: str, cache_dir: Path) -> Path:
    """The huggingface_hub cache folder for a model inside ``cache_dir``."""
    return cache_dir / ("models--" + repo_id(name).replace("/", "--"))


def local_model_path(name: str, cache_dir: Path) -> Path | None:
    """Path of a fully downloaded snapshot, or None. Local directories are accepted as-is."""
    as_path = Path(name)
    if as_path.is_dir() and (as_path / "model.bin").is_file():
        return as_path
    try:
        folder = cache_folder(name, cache_dir)
    except ValueError:
        return None
    snapshots = folder / "snapshots"
    if not snapshots.is_dir():
        return None
    for snap in sorted(snapshots.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if (snap / "model.bin").is_file() and (snap / "config.json").is_file():
            return snap
    return None


def is_downloaded(name: str, cache_dir: Path) -> bool:
    return local_model_path(name, cache_dir) is not None
