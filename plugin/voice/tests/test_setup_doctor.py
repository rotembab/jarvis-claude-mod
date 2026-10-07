from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from jarvis_voice.doctor import run_doctor
from jarvis_voice.listen import models as wake_models
from jarvis_voice.logs import mask_secret
from jarvis_voice.setup_cmd import download_wake, run_setup
from jarvis_voice.stt import models
from jarvis_voice.stt.base import SttError

from fish_fake import FakeFishServer


def snapshot(cache: Path, name: str) -> None:
    snap = models.cache_folder(name, cache) / "snapshots" / "rev"
    snap.mkdir(parents=True)
    (snap / "model.bin").write_bytes(b"0" * 10)
    (snap / "config.json").write_text("{}")


def fake_download(name: str, cache: Path, on_progress: Any) -> Path:
    for frac in (0.0, 0.25, 0.5, 0.5, 0.75):
        on_progress(frac, f"Downloading {name}")
    snapshot(cache, name)
    on_progress(1.0, f"Downloaded {name}")
    return cache


def no_wake(models_dir: Path) -> None:
    pass


def test_setup_progress_lines(tmp_path: Path) -> None:
    lines: list[dict[str, Any]] = []
    wake_dirs: list[Path] = []
    code = run_setup(
        tmp_path,
        "auto",
        lines.append,
        detect=lambda: "cpu",
        download=fake_download,
        verifier=lambda req, cache, dev: ("small.en", "cpu"),
        fetch_wake=wake_dirs.append,
    )
    assert code == 0
    assert wake_dirs == [tmp_path / "models"]
    assert any(line["message"] == 'Downloading the "Hey Jarvis" and "Jarvis" wake word models' for line in lines)
    assert any(line["message"] == "Wake word models ready" for line in lines)
    assert all(set(line) == {"v", "type", "step", "pct", "message"} and line["type"] == "progress" for line in lines)
    steps = [line["step"] for line in lines]
    assert steps[0] == "prepare" and "download" in steps and "verify" in steps
    assert lines[-1]["step"] == "done" and lines[-1]["pct"] == 100
    pcts = [line["pct"] for line in lines]
    assert pcts == sorted(pcts)  # monotonic
    assert len(pcts) == len({(line["step"], line["pct"]) for line in lines})  # no duplicate lines
    assert models.is_downloaded("small.en", tmp_path / "models")
    assert (tmp_path / "logs").is_dir()


def test_a_wake_word_download_failure_does_not_fail_setup(tmp_path: Path) -> None:
    lines: list[dict[str, Any]] = []

    def offline(models_dir: Path) -> None:
        raise OSError("github.com unreachable")

    code = run_setup(
        tmp_path,
        "small.en",
        lines.append,
        detect=lambda: "cpu",
        download=fake_download,
        verifier=lambda req, cache, dev: ("small.en", "cpu"),
        fetch_wake=offline,
    )
    assert code == 0 and lines[-1]["step"] == "done"
    assert any("github.com unreachable" in line["message"] and "push-to-talk" in line["message"] for line in lines)


def test_setup_downloads_both_wake_words_and_names_the_one_that_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetched: list[tuple[str, ...]] = []

    def download(folder: Path, on_progress: Any = None, files: tuple[wake_models.ModelFile, ...] = ()) -> Path:
        fetched.append(tuple(f.name for f in files))
        if wake_models.JARVIS_V2 in files:
            raise OSError("raw.githubusercontent.com unreachable")
        return folder

    monkeypatch.setattr(wake_models, "download", download)
    with pytest.raises(OSError, match='"Jarvis": raw.githubusercontent.com unreachable') as failed:
        download_wake(tmp_path)
    assert getattr(failed.value, "phrase", None) == "Jarvis"
    assert fetched == [
        ("melspectrogram.onnx", "embedding_model.onnx", "hey_jarvis_v0.1.onnx"),
        ("melspectrogram.onnx", "embedding_model.onnx", "jarvis_v2.onnx"),
    ]


def test_setup_says_hey_jarvis_still_works_when_only_plain_jarvis_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def download(folder: Path, on_progress: Any = None, files: tuple[wake_models.ModelFile, ...] = ()) -> Path:
        if wake_models.JARVIS_V2 in files:
            raise OSError("raw.githubusercontent.com unreachable")
        return folder

    monkeypatch.setattr(wake_models, "download", download)
    lines: list[dict[str, Any]] = []
    code = run_setup(  # with the real download_wake
        tmp_path,
        "small.en",
        lines.append,
        detect=lambda: "cpu",
        download=fake_download,
        verifier=lambda req, cache, dev: ("small.en", "cpu"),
    )
    assert code == 0 and lines[-1]["step"] == "done"
    [failed] = [line["message"] for line in lines if line["message"].startswith("Could not download")]
    assert failed == (
        'Could not download the plain "Jarvis" wake word model (raw.githubusercontent.com unreachable); '
        '"Hey Jarvis" and push-to-talk still work'
    )


def serve(tmp_path: Path, payload: bytes) -> wake_models.ModelFile:
    """A model file whose own download URL is a local file."""
    import hashlib

    source = tmp_path / "source" / "plain.onnx"
    source.parent.mkdir()
    source.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    return wake_models.ModelFile("plain.onnx", digest, len(payload), source=source.as_uri())


def test_a_model_file_downloads_from_its_own_url_and_is_checked(tmp_path: Path) -> None:
    model = serve(tmp_path, b"onnx bytes")
    folder = tmp_path / "wake"
    assert not wake_models.is_downloaded(folder, (model,))
    wake_models.download(folder, files=(model,))
    assert (folder / "plain.onnx").read_bytes() == b"onnx bytes" and wake_models.is_downloaded(folder, (model,))

    tampered = wake_models.ModelFile("bad.onnx", "0" * 64, model.size, source=model.source)
    with pytest.raises(ValueError, match="failed its checksum"):
        wake_models.download(folder, files=(tampered,))
    assert sorted(f.name for f in folder.iterdir()) == ["plain.onnx"]  # nothing half-written is left


def test_the_wake_word_models_are_pinned() -> None:
    assert wake_models.HEY_JARVIS.url == f"{wake_models.RELEASE_URL}/hey_jarvis_v0.1.onnx"
    assert wake_models.JARVIS_V2.url == (
        "https://raw.githubusercontent.com/fwartner/home-assistant-wakewords-collection/"
        "1c6d6a82947ff1367d8e5e50cc999c00943ccb9c/en/jarvis/jarvis_v2.onnx"
    )
    assert wake_models.FILES == wake_models.HEY_JARVIS_FILES
    assert wake_models.PLAIN_JARVIS_FILES == (wake_models.MELSPEC, wake_models.EMBEDDING, wake_models.JARVIS_V2)


def test_setup_skips_present_model_and_falls_back_to_cpu(tmp_path: Path) -> None:
    snapshot(tmp_path / "models", "large-v3-turbo")
    lines: list[dict[str, Any]] = []
    downloads: list[str] = []

    def download(name: str, cache: Path, on_progress: Any) -> Path:
        downloads.append(name)
        return fake_download(name, cache, on_progress)

    def verifier(req: str, cache: Path, dev: str) -> tuple[str, str]:
        return ("large-v3-turbo", "cpu") if dev == "cuda" else ("small.en", "cpu")

    assert (
        run_setup(
            tmp_path,
            "auto",
            lines.append,
            detect=lambda: "cuda",
            download=download,
            verifier=verifier,
            fetch_wake=no_wake,
        )
        == 0
    )
    assert downloads == ["small.en"]  # turbo was present; small.en fetched for CPU use
    assert any("already downloaded" in line["message"] for line in lines)
    assert any("falling back to the CPU" in line["message"] for line in lines)
    assert lines[-1]["message"] == "Ready: small.en on cpu"


def test_setup_reports_errors(tmp_path: Path) -> None:
    lines: list[dict[str, Any]] = []

    def boom(name: str, cache: Path, on_progress: Any) -> Path:
        raise OSError("network down")

    assert run_setup(tmp_path, "small.en", lines.append, detect=lambda: "cpu", download=boom, fetch_wake=no_wake) == 1
    assert lines[-1]["step"] == "error" and "network down" in lines[-1]["message"]

    def bad_verify(req: str, cache: Path, dev: str) -> tuple[str, str]:
        raise SttError("stt_failed", "cannot load", "reinstall")

    lines.clear()
    assert (
        run_setup(
            tmp_path,
            "small.en",
            lines.append,
            detect=lambda: "cpu",
            download=fake_download,
            verifier=bad_verify,
            fetch_wake=no_wake,
        )
        == 1
    )
    assert lines[-1] == {"v": 1, "type": "progress", "step": "error", "pct": 88, "message": "cannot load reinstall"}


def test_mask_secret() -> None:
    assert mask_secret(None) is None
    assert mask_secret("short") == "****"
    assert mask_secret("abcdefghijklmnop") == "****mnop"


def test_doctor_report_masks_key(tmp_path: Path) -> None:
    key = "fa-0123456789abcdefSECRET"
    with FakeFishServer(api_key=key) as fish:
        report = run_doctor(tmp_path, test_mic=False, env={"FISH_AUDIO_API_KEY": key, "JARVIS_FISH_BASE_URL": fish.url})
    text = json.dumps(report)
    assert key not in text and report["fish"]["key"] == "****CRET"
    assert report["fish"]["status"] == "ok" and report["fish"]["model"] == "s2.1-pro-free"
    assert set(report) >= {"version", "platform", "python", "audio", "cuda", "models", "fish", "ptt"}
    assert report["models"]["downloaded"]["small.en"] is False
    assert "deviceCount" in report["cuda"]
    wake = report["wake"]
    assert (wake["phrase"], wake["plainPhrase"]) == ("Hey Jarvis", "Jarvis")
    assert wake["downloaded"] is False and wake["plainDownloaded"] is False


def test_doctor_without_network(tmp_path: Path) -> None:
    report = run_doctor(tmp_path, network=False, test_mic=False, env={})
    assert report["fish"] == {
        "keySet": False,
        "key": None,
        "model": "s2.1-pro-free",
        "baseUrl": "wss://api.fish.audio",
        "status": "skipped",
    }


def test_doctor_cli_prints_json(tmp_path: Path) -> None:
    env = dict(os.environ, FISH_AUDIO_API_KEY="fa-cli-secret-0000000000")
    proc = subprocess.run(
        [sys.executable, "-m", "jarvis_voice", "doctor", "--data-dir", str(tmp_path), "--no-network", "--no-mic"],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["fish"]["keySet"] is True
    assert "fa-cli-secret" not in proc.stdout + proc.stderr


@pytest.mark.parametrize("args", [["setup", "--help"], ["doctor", "--help"], ["run", "--help"]])
def test_cli_help(args: list[str]) -> None:
    proc = subprocess.run([sys.executable, "-m", "jarvis_voice", *args], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0 and "usage" in proc.stdout


def test_native_writes_to_stdout_cannot_reach_the_protocol_stream() -> None:
    """Native libraries write to file descriptor 1 directly; only protocol lines may come out of stdout."""
    script = (
        "import os, sys\n"
        "from jarvis_voice.cli import _claim_stdout\n"
        "out = _claim_stdout()\n"
        "os.write(1, b'native noise without newline')\n"  # e.g. CTranslate2 or cuDNN's loader
        "print('stray print')\n"
        'out.write(b\'{"type":"hello"}\\n\'); out.flush()\n'
    )
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == b'{"type":"hello"}\n'
    assert b"native noise without newline" in proc.stderr and b"stray print" in proc.stderr


def test_cuda_section_lists_cached_probe_results(tmp_path: Path) -> None:
    from jarvis_voice.stt.cuda_probe import CudaProbe, ProbeResult

    probe = CudaProbe(tmp_path / "models" / "cuda-probe.json", runner=lambda path, timeout: ProbeResult(False, "boom"))
    probe.check(tmp_path / "some-model")
    report = run_doctor(tmp_path, network=False, test_mic=False, env={})
    assert [(p["ok"], p["detail"]) for p in report["cuda"]["probes"]] == [(False, "boom")]
    assert report["cuda"]["probes"][0]["model"].endswith("some-model")
