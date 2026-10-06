"""Decide in a child process whether CUDA can run a speech model.

A broken CUDA install does not always raise a Python exception. When a cuDNN
sub-library is missing, CTranslate2 aborts the whole process during the
encoder's first GPU convolution (SIGABRT, or exit code 0xC0000409 on Windows),
so no ``try``/``except`` around the load can fall back to the CPU. The first
CUDA load of a model therefore happens in ``python -m jarvis_voice
probe-cuda``, and the helper only uses the GPU when that child exits cleanly.

Results are cached per model and installed GPU-library versions in
``<models dir>/cuda-probe.json``. A marker is written around the in-process
GPU load; if a helper dies during that load, the next start probes again
instead of trusting the cached success.
"""

from __future__ import annotations

import contextlib
import importlib.metadata
import json
import logging
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

CACHE_FILE = "cuda-probe.json"
PROBE_TIMEOUT_S = 300.0  # a cold disk plus first-run CUDA kernel caching can be slow
_GPU_PACKAGES = ("ctranslate2", "nvidia-cublas-cu12", "nvidia-cudnn-cu12")
_MAX_ENTRIES = 16
_SECRETS = ("JARVIS_TOKEN", "FISH_AUDIO_API_KEY")


@dataclass(frozen=True, slots=True)
class ProbeResult:
    ok: bool
    detail: str
    cached: bool = False
    transient: bool = False  # timeouts and spawn failures are not cached


def _version(package: str) -> str:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return "-"


def describe_exit(code: int) -> str:
    if code < 0:  # POSIX: killed by a signal
        try:
            return f"killed by {signal.Signals(-code).name}"
        except ValueError:
            return f"killed by signal {-code}"
    if sys.platform == "win32" and code > 0xFFFF:  # an NTSTATUS such as 0xC0000409
        return f"crashed with exit code 0x{code & 0xFFFFFFFF:08X}"
    return f"exit code {code}"


def _reported_ok(stdout: str) -> bool:
    """True when the child's last JSON line says ``"ok": true`` (exit code 0 alone is not enough)."""
    for line in reversed(stdout.splitlines()):
        try:
            report = json.loads(line)
        except ValueError:
            continue
        return isinstance(report, dict) and report.get("ok") is True
    return False


def _probe_child(model_path: Path, timeout: float) -> ProbeResult:
    argv = [sys.executable, "-m", "jarvis_voice", "probe-cuda", "--model", str(model_path)]
    env = {k: v for k, v in os.environ.items() if k not in _SECRETS}  # the child needs none of them
    # No console window flashes up when the helper itself has none (desktop app).
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    started = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            env=env,
            creationflags=flags,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ProbeResult(False, f"the GPU test did not finish within {timeout:.0f} s", transient=True)
    except OSError as exc:
        return ProbeResult(False, f"could not start the GPU test: {exc}", transient=True)
    if proc.returncode == 0 and _reported_ok(proc.stdout.decode("utf-8", "replace")):
        return ProbeResult(True, f"ok in {time.monotonic() - started:.1f} s")
    tail = [line.strip() for line in proc.stderr.decode("utf-8", "replace").splitlines() if line.strip()][-3:]
    detail = describe_exit(proc.returncode) + (": " + " | ".join(tail) if tail else "")
    return ProbeResult(False, detail[:600])


class CudaProbe:
    """Child-process CUDA check with a small JSON cache (``cache_file=None``: no cache)."""

    def __init__(
        self,
        cache_file: Path | None,
        *,
        timeout: float = PROBE_TIMEOUT_S,
        runner: Callable[[Path, float], ProbeResult] = _probe_child,
    ) -> None:
        self._cache_file = cache_file
        self._timeout = timeout
        self._runner = runner

    @staticmethod
    def key(model_path: Path) -> str:
        """Identifies what the probe tested: the model, the interpreter and the GPU library versions."""
        versions = ",".join(f"{name}={_version(name)}" for name in _GPU_PACKAGES)
        return f"{Path(model_path).resolve()}|{sys.executable}|{versions}"

    def check(self, model_path: Path, *, force: bool = False) -> ProbeResult:
        key = self.key(model_path)
        cache = self._read()
        results = cache.get("results")
        entry = results.get(key) if isinstance(results, dict) else None
        crashed = cache.get("inflight") == key
        if crashed:
            log.warning("the last GPU load of %s never finished; testing the GPU again", model_path)
        if isinstance(entry, dict) and not force and not crashed:
            return ProbeResult(bool(entry.get("ok")), str(entry.get("detail", "")), cached=True)
        log.info("testing %s on the GPU in a child process", model_path)
        result = self._runner(Path(model_path), self._timeout)
        log.log(logging.INFO if result.ok else logging.WARNING, "GPU test for %s: %s", model_path, result.detail)

        def store(data: dict[str, Any]) -> None:
            if not isinstance(data.get("results"), dict):
                data["results"] = {}
            stored = data["results"]
            stored.pop(key, None)  # re-insert so the oldest entries are the ones dropped
            stored[key] = {"ok": result.ok, "detail": result.detail, "at": int(time.time())}
            while len(stored) > _MAX_ENTRIES:
                stored.pop(next(iter(stored)))
            if data.get("inflight") == key:
                data.pop("inflight")

        if not result.transient:
            self._update(store)
        return result

    @contextlib.contextmanager
    def in_process_load(self, model_path: Path) -> Iterator[None]:
        """Marks an in-process GPU load; the marker survives only if the process dies inside it."""
        key = self.key(model_path)

        def clear(data: dict[str, Any]) -> None:
            if data.get("inflight") == key:
                del data["inflight"]

        self._update(lambda data: data.__setitem__("inflight", key))
        try:
            yield
        finally:
            self._update(clear)

    # -- cache file

    def _read(self) -> dict[str, Any]:
        return {} if self._cache_file is None else read_cache(self._cache_file)

    def _update(self, change: Callable[[dict[str, Any]], object]) -> None:
        if self._cache_file is None:
            return
        data = self._read()
        change(data)
        try:
            self._cache_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
            os.replace(tmp, self._cache_file)
        except OSError as exc:
            log.warning("cannot write %s: %s", self._cache_file, exc)


def read_cache(cache_file: Path) -> dict[str, Any]:
    """The cache file's contents; empty when it is missing or unreadable."""
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}
