from __future__ import annotations

import json
import signal
import sys
from pathlib import Path
from typing import ClassVar

import numpy as np
import pytest

from jarvis_voice.stt import models
from jarvis_voice.stt.base import SttError
from jarvis_voice.stt.cuda_probe import CudaProbe, ProbeResult, _probe_child, describe_exit
from jarvis_voice.stt.fake import FakeTranscriber
from jarvis_voice.stt.faster_whisper_engine import FasterWhisperTranscriber


def fake_snapshot(cache: Path, name: str) -> Path:
    snap = models.cache_folder(name, cache) / "snapshots" / "abc123"
    snap.mkdir(parents=True)
    for f in ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt", "preprocessor_config.json"):
        (snap / f).write_text("{}")
    return snap


@pytest.mark.parametrize(
    ("requested", "device", "resolved"),
    [
        ("auto", "cuda", "large-v3-turbo"),
        ("auto", "cpu", "small.en"),
        ("AUTO", "cpu", "small.en"),
        (None, "cuda", "large-v3-turbo"),
        ("medium", "cuda", "medium"),
        ("base.en", "cpu", "base.en"),
    ],
)
def test_resolve_model(requested: str | None, device: str, resolved: str) -> None:
    assert models.resolve_model(requested, device) == resolved  # type: ignore[arg-type]


def test_compute_type_per_device() -> None:
    assert models.compute_type("cuda") == "float16"
    assert models.compute_type("cpu") == "int8"


def test_every_user_config_model_has_a_repo() -> None:
    for name in ("base.en", "small.en", "small", "medium", "large-v3-turbo"):
        assert "/" in models.repo_id(name)
    assert models.repo_id("org/custom-model") == "org/custom-model"
    with pytest.raises(ValueError):
        models.repo_id("gigantic")


def test_local_model_detection(tmp_path: Path) -> None:
    assert not models.is_downloaded("small.en", tmp_path)
    snap = fake_snapshot(tmp_path, "small.en")
    assert models.local_model_path("small.en", tmp_path) == snap
    assert models.cache_folder("small.en", tmp_path).name == "models--Systran--faster-whisper-small.en"
    assert models.local_model_path(str(snap), tmp_path) == snap  # a plain directory works too
    assert not models.is_downloaded("not-a-model", tmp_path)


def test_dll_registration_is_a_noop_off_windows() -> None:
    if sys.platform != "win32":
        assert models.register_cuda_dll_dirs() == []


def test_missing_model_raises_stt_model_missing_without_network(tmp_path: Path) -> None:
    t = FasterWhisperTranscriber("small.en", tmp_path, device="cpu")
    with pytest.raises(SttError) as info:
        t.load()
    assert info.value.code == "stt_model_missing" and "/jarvis setup" in (info.value.hint or "")


def test_broken_model_raises_stt_failed(tmp_path: Path) -> None:
    fake_snapshot(tmp_path, "small.en")  # files exist but are not a real model
    with pytest.raises(SttError) as info:
        FasterWhisperTranscriber("small.en", tmp_path, device="cpu").load()
    assert info.value.code == "stt_failed"


def test_auto_on_cuda_falls_back_to_cpu_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    t = FasterWhisperTranscriber("auto", tmp_path)
    assert t._attempts("cuda") == [("cuda", "large-v3-turbo"), ("cpu", "small.en"), ("cpu", "large-v3-turbo")]
    assert t._attempts("cpu") == [("cpu", "small.en")]
    assert FasterWhisperTranscriber("medium", tmp_path)._attempts("cuda") == [("cuda", "medium"), ("cpu", "medium")]


def test_fake_transcriber() -> None:
    t = FakeTranscriber("hi")
    assert t.transcribe(np.full(1600, 0.2, np.float32), "en").text == "hi"
    assert t.transcribe(np.zeros(1600, np.float32), "en").text == ""


# --------------------------------------------------------------------------- CUDA probe


class FakeWhisperModel:
    """Records where the transcriber loads models instead of loading them."""

    loads: ClassVar[list[tuple[str, str]]] = []

    def __init__(self, path: str, *, device: str, compute_type: str, cpu_threads: int = 0) -> None:
        FakeWhisperModel.loads.append((Path(path).parts[-3], device))


class Runner:
    """Stands in for the probe-cuda child process."""

    def __init__(self, result: ProbeResult) -> None:
        self.result = result
        self.calls: list[Path] = []

    def __call__(self, model_path: Path, timeout: float) -> ProbeResult:
        self.calls.append(model_path)
        return self.result


@pytest.fixture
def fake_whisper(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    import faster_whisper

    from jarvis_voice.stt import faster_whisper_engine

    FakeWhisperModel.loads = []
    monkeypatch.setattr(faster_whisper, "WhisperModel", FakeWhisperModel)
    monkeypatch.setattr(faster_whisper_engine, "warm_up", lambda model: None)
    return FakeWhisperModel.loads


TURBO_REPO = "models--mobiuslabsgmbh--faster-whisper-large-v3-turbo"


def test_gpu_that_fails_the_child_probe_is_never_loaded_in_process(
    tmp_path: Path, fake_whisper: list[tuple[str, str]]
) -> None:
    """A missing cuDNN aborts the process, so a failed probe must keep the GPU load out of it."""
    fake_snapshot(tmp_path, "large-v3-turbo")
    runner = Runner(ProbeResult(False, "crashed with exit code 0xC0000409"))
    probe = CudaProbe(tmp_path / "cuda-probe.json", runner=runner)
    t = FasterWhisperTranscriber("auto", tmp_path, device="cuda", cuda_probe=probe)
    t.load()
    assert (t.name, t.device) == ("large-v3-turbo", "cpu")
    assert fake_whisper == [(TURBO_REPO, "cpu")]  # no in-process CUDA attempt at all
    assert len(runner.calls) == 1

    # The verdict is cached: the next start does not run the child again ...
    FasterWhisperTranscriber("auto", tmp_path, device="cuda", cuda_probe=probe).load()
    assert len(runner.calls) == 1
    # ... but setup re-tests (e.g. after installing the cuDNN wheel).
    FasterWhisperTranscriber("auto", tmp_path, device="cuda", cuda_probe=probe, reprobe_cuda=True).load()
    assert len(runner.calls) == 2


def test_gpu_that_passes_the_probe_is_used(tmp_path: Path, fake_whisper: list[tuple[str, str]]) -> None:
    fake_snapshot(tmp_path, "large-v3-turbo")
    cache = tmp_path / "cuda-probe.json"
    runner = Runner(ProbeResult(True, "ok in 2.0 s"))
    t = FasterWhisperTranscriber("auto", tmp_path, device="cuda", cuda_probe=CudaProbe(cache, runner=runner))
    t.load()
    assert (t.name, t.device) == ("large-v3-turbo", "cuda") and fake_whisper == [(TURBO_REPO, "cuda")]
    data = json.loads(cache.read_text())
    assert "inflight" not in data  # the in-process load finished
    assert [entry["ok"] for entry in data["results"].values()] == [True]


def test_transient_probe_failures_are_not_cached(tmp_path: Path) -> None:
    runner = Runner(ProbeResult(False, "did not finish", transient=True))
    probe = CudaProbe(tmp_path / "cuda-probe.json", runner=runner)
    assert not probe.check(tmp_path).ok
    assert not probe.check(tmp_path).ok
    assert len(runner.calls) == 2


def test_helper_that_died_during_its_gpu_load_probes_again(tmp_path: Path) -> None:
    cache = tmp_path / "cuda-probe.json"
    runner = Runner(ProbeResult(True, "ok"))
    probe = CudaProbe(cache, runner=runner)
    assert probe.check(tmp_path).ok and len(runner.calls) == 1
    assert probe.check(tmp_path).cached

    # Simulate a process that died inside the in-process load (the marker stays behind).
    marker = probe.in_process_load(tmp_path)
    marker.__enter__()
    assert json.loads(cache.read_text())["inflight"] == CudaProbe.key(tmp_path)
    result = probe.check(tmp_path)
    assert not result.cached and len(runner.calls) == 2
    assert "inflight" not in json.loads(cache.read_text())


def test_probe_cuda_child_reports_failure_cleanly(tmp_path: Path) -> None:
    """The real ``probe-cuda`` child: no usable GPU or model here, so it must say so and exit non-zero."""
    snap = fake_snapshot(tmp_path, "small.en")
    result = _probe_child(snap, 120)
    assert not result.ok and not result.transient
    assert "GPU test failed" in result.detail or "crashed" in result.detail, result.detail


def test_describe_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    assert describe_exit(-int(signal.SIGABRT)) == "killed by SIGABRT"  # SIGABRT is 22 on Windows
    assert describe_exit(1) == "exit code 1"
    monkeypatch.setattr(sys, "platform", "win32")
    assert describe_exit(0xC0000409) == "crashed with exit code 0xC0000409"


def test_setup_verify_retests_the_gpu(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvis_voice import setup_cmd
    from jarvis_voice.stt import faster_whisper_engine

    seen: dict[str, object] = {}

    class Recorder:
        name, device = "large-v3-turbo", "cuda"

        def __init__(self, requested: str, cache_dir: Path, **kwargs: object) -> None:
            seen.update(kwargs)

        def load(self) -> None:
            pass

    monkeypatch.setattr(faster_whisper_engine, "FasterWhisperTranscriber", Recorder)
    assert setup_cmd._verify("auto", tmp_path, "cuda") == ("large-v3-turbo", "cuda")
    assert seen == {"device": "cuda", "reprobe_cuda": True}
