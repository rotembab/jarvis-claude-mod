"""``setup``: download (and verify) the speech model, reporting JSON-lines progress.

Each stdout line is ``{"v":1,"type":"progress","step":...,"pct":0-100,"message":...}``.
Steps: prepare, detect, download (the speech model, then the small wake word
models for "Hey Jarvis" and plain "Jarvis"), verify, then ``done`` (pct 100)
on success or ``error`` on failure (exit code 1). A wake word download failure
is reported but does not fail setup: push-to-talk works without it (and
"Hey Jarvis" without the plain "Jarvis" model). The helper retries "Hey Jarvis"
at its next start, and plain "Jarvis" when it is switched on.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .listen import models as wake_models
from .stt.base import SttDevice, SttError
from .stt.models import (
    AUTO,
    CPU_DEFAULT_MODEL,
    MODEL_FILES,
    cache_folder,
    detect_device,
    is_downloaded,
    models_dir,
    repo_id,
    resolve_model,
)

log = logging.getLogger(__name__)

ProgressFn = Callable[[float, str], None]  # (fraction 0..1, message)
Emit = Callable[[dict[str, Any]], None]


def _quiet_hub() -> None:
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")


def _dir_bytes(folder: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(folder):
        for name in files:
            path = Path(root) / name
            try:
                if not path.is_symlink():
                    total += path.stat().st_size
            except OSError:
                pass
    return total


def _expected_bytes(repo: str) -> int:
    from huggingface_hub import HfApi

    info = HfApi().model_info(repo, files_metadata=True)
    return sum(
        int(s.size or 0)
        for s in (info.siblings or [])
        if any(fnmatch.fnmatch(s.rfilename, pattern) for pattern in MODEL_FILES)
    )


def download_model(name: str, cache_dir: Path, on_progress: ProgressFn) -> Path:
    """Download a faster-whisper model into the HF cache layout under ``cache_dir``.

    Progress comes from polling the bytes on disk against the repo's file
    sizes, which works the same across huggingface_hub versions and transfer
    backends (plain HTTP or hf_xet).
    """
    _quiet_hub()
    from huggingface_hub import snapshot_download

    repo = repo_id(name)
    try:
        expected = _expected_bytes(repo)
    except Exception as exc:  # noqa: BLE001 - progress is best-effort
        log.info("could not size %s: %s", repo, exc)
        expected = 0
    folder = cache_folder(name, cache_dir)
    stop = threading.Event()

    def poll() -> None:
        while not stop.wait(0.5):
            done = _dir_bytes(folder)
            if expected:
                on_progress(
                    min(0.99, done / expected), f"Downloading {name}: {done / 1e6:.0f} of {expected / 1e6:.0f} MB"
                )
            else:
                on_progress(0.0, f"Downloading {name}: {done / 1e6:.0f} MB")

    poller = threading.Thread(target=poll, name="download-progress", daemon=True)
    poller.start()
    try:
        path = snapshot_download(repo, cache_dir=str(cache_dir), allow_patterns=MODEL_FILES)
    finally:
        stop.set()
        poller.join(1.0)
    on_progress(1.0, f"Downloaded {name}")
    return Path(path)


class WakeDownloadError(OSError):
    """A wake word model could not be downloaded; ``phrase`` says which one."""

    def __init__(self, phrase: str, cause: BaseException) -> None:
        super().__init__(f'"{phrase}": {cause}')
        self.phrase = phrase
        self.cause = cause


def download_wake(models: Path) -> None:
    folder = wake_models.wake_dir(models)
    for phrase, files in (
        (wake_models.WAKE_PHRASE, wake_models.HEY_JARVIS_FILES),
        (wake_models.PLAIN_WAKE_PHRASE, wake_models.PLAIN_JARVIS_FILES),
    ):
        if wake_models.is_downloaded(folder, files):
            continue
        try:
            wake_models.download(folder, files=files)
        except Exception as exc:
            raise WakeDownloadError(phrase, exc) from exc


def _verify(requested: str, cache_dir: Path, device: SttDevice) -> tuple[str, SttDevice]:
    from .stt.faster_whisper_engine import FasterWhisperTranscriber

    # Setup is the repair path: test the GPU afresh rather than trust a cached result.
    transcriber = FasterWhisperTranscriber(requested, cache_dir, device=device, reprobe_cuda=True)
    transcriber.load()
    return transcriber.name, transcriber.device


class _Progress:
    """Emits progress lines, skipping repeats of the same step and whole percent."""

    def __init__(self, emit: Emit) -> None:
        self._emit = emit
        self._last: tuple[str, int] | None = None
        self._lock = threading.Lock()

    def __call__(self, step: str, pct: float, message: str, *, force: bool = False) -> None:
        pct_int = max(0, min(100, round(pct)))
        with self._lock:
            if not force and self._last == (step, pct_int):
                return
            self._last = (step, pct_int)
            self._emit({"v": 1, "type": "progress", "step": step, "pct": pct_int, "message": message})


def run_setup(
    data_dir: Path,
    stt_model: str,
    emit: Emit,
    *,
    verify: bool = True,
    detect: Callable[[], SttDevice] = detect_device,
    download: Callable[[str, Path, ProgressFn], Path] = download_model,
    verifier: Callable[[str, Path, SttDevice], tuple[str, SttDevice]] = _verify,
    fetch_wake: Callable[[Path], None] = download_wake,
) -> int:
    progress = _Progress(emit)
    requested = (stt_model or AUTO).strip()
    pct = 0.0
    try:
        progress("prepare", 0, f"Preparing {data_dir}")
        cache = models_dir(data_dir)
        cache.mkdir(parents=True, exist_ok=True)
        (data_dir / "logs").mkdir(parents=True, exist_ok=True)

        progress("detect", 2, "Looking for an NVIDIA GPU")
        device = detect()
        name = resolve_model(requested, device)
        pct = 5
        progress("detect", pct, f"Using the {'GPU (CUDA)' if device == 'cuda' else 'CPU'}; speech model {name}")

        def fetch(model: str, lo: float, hi: float) -> None:
            nonlocal pct
            if is_downloaded(model, cache):
                pct = hi
                progress("download", hi, f"{model} is already downloaded")
                return

            def on_progress(frac: float, message: str) -> None:
                progress("download", lo + (hi - lo) * frac, message)

            progress("download", lo, f"Downloading {model}")
            download(model, cache, on_progress)
            pct = hi

        fetch(name, 5, 85)

        phrases = f'"{wake_models.WAKE_PHRASE}" and "{wake_models.PLAIN_WAKE_PHRASE}"'
        progress("download", 86, f"Downloading the {phrases} wake word models")  # 85 was the speech model
        try:
            fetch_wake(cache)
        except Exception as exc:
            log.warning("wake word download failed", exc_info=True)
            if isinstance(exc, WakeDownloadError) and exc.phrase == wake_models.PLAIN_WAKE_PHRASE:
                # "Hey Jarvis" comes first, so it is in place.
                message = (
                    f'Could not download the plain "{exc.phrase}" wake word model ({exc.cause}); '
                    f'"{wake_models.WAKE_PHRASE}" and push-to-talk still work'
                )
            else:
                message = f"Could not download the wake word model ({exc}); push-to-talk still works"
            progress("download", 87, message)
        else:
            progress("download", 87, "Wake word models ready")
        pct = 87

        if verify:
            pct = 88
            progress("verify", pct, f"Loading {name} to check it works")
            loaded, loaded_device = verifier(requested, cache, device)
            if device == "cuda" and loaded_device == "cpu":
                message = "The GPU could not run the model (CUDA libraries missing?); falling back to the CPU"
                progress("verify", 92, message)
                if requested.lower() == AUTO and not is_downloaded(CPU_DEFAULT_MODEL, cache):
                    fetch(CPU_DEFAULT_MODEL, 92, 98)
                    loaded, loaded_device = verifier(requested, cache, "cpu")
            name, device = loaded, loaded_device
        progress("done", 100, f"Ready: {name} on {device}", force=True)
        return 0
    except SttError as exc:
        progress("error", pct, f"{exc.message} {exc.hint or ''}".strip(), force=True)
    except Exception as exc:
        log.exception("setup failed")
        progress("error", pct, f"Setup failed: {type(exc).__name__}: {exc}", force=True)
    return 1
