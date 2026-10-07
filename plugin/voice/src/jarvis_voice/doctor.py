"""``doctor``: a JSON health report (devices, mic access, CUDA, models, Fish, PTT).

Every section is independent and catches its own failures, so the report is
always produced. The Fish key is masked; the control token never appears.
"""

from __future__ import annotations

import math
import os
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np

from . import __version__
from . import platform as plat
from .logs import mask_secret


def _audio_section(env: Mapping[str, str], test_mic: bool) -> dict[str, Any]:
    from .audio.errors import AudioError
    from .audio.levels import is_digital_silence
    from .audio.sd_backend import SoundDeviceCapture, load_sounddevice, query_devices, resolve_device

    section: dict[str, Any] = {}
    try:
        sd = load_sounddevice()
        devices, _defaults = query_devices(sd)
        hostapi = plat.current().PREFERRED_HOSTAPI
        section["hostApi"] = hostapi or "default"
        section["inputs"] = [d.name for d in devices if d.supports("input") and (not hostapi or d.hostapi == hostapi)]
        section["outputs"] = [d.name for d in devices if d.supports("output") and (not hostapi or d.hostapi == hostapi)]
        section["input"] = resolve_device(sd, "input", env.get("JARVIS_INPUT_DEVICE")).name
        section["output"] = resolve_device(sd, "output", env.get("JARVIS_OUTPUT_DEVICE")).name
        section["ok"] = True
    except AudioError as exc:
        return {"ok": False, "code": exc.code, "message": exc.message, "hint": exc.hint}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "code": "internal", "message": f"{type(exc).__name__}: {exc}"}

    if test_mic:
        capture = SoundDeviceCapture(env.get("JARVIS_INPUT_DEVICE"))
        try:
            capture.open()
            time.sleep(0.4)  # fill the pre-roll
            capture.begin()
            time.sleep(0.6)
            clip = capture.end()
            peak = float(np.max(np.abs(clip))) if clip.size else 0.0
            section["mic"] = {
                "ok": not is_digital_silence(clip),
                "peakDb": round(20 * math.log10(peak), 1) if peak > 0 else None,
                "digitalSilence": bool(is_digital_silence(clip)),
            }
            if is_digital_silence(clip):
                section["mic"]["hint"] = plat.current().digital_silence_hint()
        except AudioError as exc:
            section["mic"] = {"ok": False, "code": exc.code, "message": exc.message, "hint": exc.hint}
        finally:
            capture.close()
    return section


def _cuda_section(data_dir: Path) -> dict[str, Any]:
    from .stt.cuda_probe import CACHE_FILE, read_cache
    from .stt.models import cuda_device_count, models_dir, register_cuda_dll_dirs

    section: dict[str, Any] = {"dllDirs": register_cuda_dll_dirs()}
    # What the last GPU tests (run in a child process by run/setup) concluded.
    results = read_cache(models_dir(data_dir) / CACHE_FILE).get("results")
    if isinstance(results, dict):
        section["probes"] = [
            {"model": key.split("|", 1)[0], **entry} for key, entry in results.items() if isinstance(entry, dict)
        ]
    try:
        import ctranslate2

        section["ctranslate2"] = ctranslate2.__version__
        section["deviceCount"] = cuda_device_count()
        if section["deviceCount"]:
            section["computeTypes"] = sorted(ctranslate2.get_supported_compute_types("cuda"))
    except Exception as exc:  # noqa: BLE001
        section["deviceCount"] = 0
        section["error"] = f"{type(exc).__name__}: {exc}"
    return section


def _models_section(data_dir: Path) -> dict[str, Any]:
    from .stt.models import is_downloaded, models_dir

    cache = models_dir(data_dir)
    names = ["base.en", "small.en", "small", "medium", "large-v3-turbo"]
    try:
        return {"dir": str(cache), "downloaded": {n: is_downloaded(n, cache) for n in names}}
    except Exception as exc:  # noqa: BLE001
        return {"dir": str(cache), "error": f"{type(exc).__name__}: {exc}"}


def _fish_section(env: Mapping[str, str], network: bool) -> dict[str, Any]:
    from .tts.fish import FishSettings, probe

    settings = FishSettings.from_env(env)
    section: dict[str, Any] = {
        "keySet": bool(settings.api_key),
        "key": mask_secret(settings.api_key),
        "model": settings.model,
        "baseUrl": settings.base_url,
    }
    if network:
        section.update(probe(settings))
    else:
        section["status"] = "skipped"
    return section


def _local_voice_section(data_dir: Path, env: Mapping[str, str]) -> dict[str, Any]:
    from .stt.models import models_dir
    from .tts.local import default_python, model_downloaded

    python = Path(env.get("JARVIS_LOCAL_PYTHON", "").strip() or default_python(data_dir))
    clip = env.get("JARVIS_LOCAL_VOICE", "").strip()
    section: dict[str, Any] = {
        "engine": env.get("JARVIS_TTS_ENGINE", "").strip() or "fish",
        "installed": python.is_file(),
        "python": str(python),
        "modelDownloaded": model_downloaded(models_dir(data_dir)),
        "voiceClip": clip or None,
    }
    if clip:
        section["voiceClipFound"] = Path(clip).expanduser().is_file()
    return section


def _ptt_section() -> dict[str, Any]:
    try:
        from pynput import keyboard  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "message": f"{type(exc).__name__}: {exc}", "hint": plat.current().ptt_hint()}
    return {"available": True}


def run_doctor(
    data_dir: Path,
    *,
    network: bool = True,
    test_mic: bool = True,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    env = os.environ if env is None else env
    report: dict[str, Any] = {
        "version": __version__,
        "platform": plat.name(),
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "dataDir": str(data_dir),
    }
    for key, build in (
        ("audio", lambda: _audio_section(env, test_mic)),
        ("cuda", lambda: _cuda_section(data_dir)),
        ("models", lambda: _models_section(data_dir)),
        ("fish", lambda: _fish_section(env, network)),
        ("localVoice", lambda: _local_voice_section(data_dir, env)),
        ("ptt", _ptt_section),
    ):
        try:
            report[key] = build()
        except Exception as exc:  # noqa: BLE001 - one broken probe must not hide the rest
            report[key] = {"error": f"{type(exc).__name__}: {exc}"}
    return report
