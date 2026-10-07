"""The local voice engine: the helper's side against the real local voice process in fake mode."""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from jarvis_voice.tts.base import SynthError
from jarvis_voice.tts.local import LocalVoiceSettings, LocalVoiceSynth, default_python, model_downloaded

from conftest import wait_until

LOCAL_SRC = Path(__file__).resolve().parents[2] / "local-voice" / "src"
ENV = {"PYTHONPATH": str(LOCAL_SRC)}
WORD_BYTES = 2 * int(24_000 * 0.06)  # the fake voice: 60 ms of 16-bit audio per word


class Collector:
    def __init__(self) -> None:
        self.chunks: list[bytes] = []
        self.lock = threading.Lock()

    def __call__(self, chunk: bytes) -> None:
        with self.lock:
            self.chunks.append(chunk)

    @property
    def total(self) -> int:
        with self.lock:
            return sum(len(c) for c in self.chunks)


def settings(tmp_path: Path, *extra: str, fake: bool = True, python: Path | None = None) -> LocalVoiceSettings:
    return LocalVoiceSettings(
        python=python or Path(sys.executable),
        models_dir=tmp_path / "models",
        fake=fake,
        extra_args=tuple(extra),
        extra_env=ENV,
        ready_timeout=30.0,
    )


@pytest.fixture
def synth(tmp_path: Path) -> Iterator[LocalVoiceSynth]:
    s = LocalVoiceSynth(settings(tmp_path))
    yield s
    s.close()


def test_speaks_sentences_in_order_and_finishes(synth: LocalVoiceSynth) -> None:
    synth.start()
    audio = Collector()
    stream = synth.open_stream(audio, voice_id="ignored")
    assert synth.state == "ready" and synth.device == "fake"
    stream.send_text("Good evening, sir.")
    stream.send_text("All systems are online.")
    stream.finish()
    wait_until(lambda: stream.done, timeout=10)
    assert stream.closed and stream.error is None
    assert audio.total == 7 * WORD_BYTES


def test_each_reply_gets_its_own_stream(synth: LocalVoiceSynth) -> None:
    first, second = Collector(), Collector()
    a = synth.open_stream(first, voice_id=None)
    b = synth.open_stream(second, voice_id=None)
    b.send_text("Two words.")
    a.send_text("Three words here.")
    a.finish()
    b.finish()
    wait_until(lambda: a.done and b.done, timeout=10)
    assert (first.total, second.total) == (3 * WORD_BYTES, 2 * WORD_BYTES)


def test_cancel_stops_the_audio_and_leaves_the_voice_running(tmp_path: Path) -> None:
    synth = LocalVoiceSynth(settings(tmp_path, "--fake-delay", "0.3"))
    try:
        audio = Collector()
        stream = synth.open_stream(audio, voice_id=None)
        for n in range(5):
            stream.send_text(f"Sentence number {n}.")
        stream.cancel()
        assert stream.closed and stream.error is None and not stream.done
        time.sleep(1.0)
        assert audio.total == 0
        again = Collector()
        nxt = synth.open_stream(again, voice_id=None)
        nxt.send_text("Still here.")
        nxt.finish()
        wait_until(lambda: nxt.done, timeout=10)
        assert again.total == 2 * WORD_BYTES
    finally:
        synth.close()


def test_a_failed_sentence_is_skipped(synth: LocalVoiceSynth) -> None:
    audio = Collector()
    stream = synth.open_stream(audio, voice_id=None)
    stream.send_text("FAIL this one")
    stream.send_text("Then this.")
    stream.finish()
    wait_until(lambda: stream.done, timeout=10)
    assert stream.error is None and audio.total == 2 * WORD_BYTES


def test_not_installed(tmp_path: Path) -> None:
    synth = LocalVoiceSynth(settings(tmp_path, python=tmp_path / "missing" / "python"))
    with pytest.raises(SynthError) as info:
        synth.open_stream(Collector(), voice_id=None)
    assert info.value.code == "local_voice_failed" and "not installed" in info.value.message
    assert info.value.hint and "/jarvis setup local" in info.value.hint


def test_a_model_that_cannot_load_is_reported_once_and_not_retried(tmp_path: Path) -> None:
    synth = LocalVoiceSynth(settings(tmp_path, fake=False))  # the real backend, with no model in tmp_path
    try:
        for _ in range(2):
            with pytest.raises(SynthError) as info:
                synth.open_stream(Collector(), voice_id=None)
            assert info.value.code == "local_voice_failed" and "could not start" in info.value.message
        assert synth.state == "failed"
    finally:
        synth.close()


def test_a_crash_fails_the_open_reply_and_the_next_reply_restarts_it(synth: LocalVoiceSynth) -> None:
    stream = synth.open_stream(Collector(), voice_id=None)
    proc = synth._proc
    assert proc is not None
    proc.kill()
    wait_until(lambda: stream.closed, timeout=10)
    assert stream.error is not None and "stopped unexpectedly" in stream.error.message
    audio = Collector()
    again = synth.open_stream(audio, voice_id=None)
    assert synth._proc is not None and synth._proc.pid != proc.pid
    again.send_text("Back again.")
    again.finish()
    wait_until(lambda: again.done, timeout=10)


def test_close_ends_the_process(tmp_path: Path) -> None:
    synth = LocalVoiceSynth(settings(tmp_path))
    synth.open_stream(Collector(), voice_id=None)
    proc = synth._proc
    assert proc is not None
    synth.close()
    assert proc.wait(10) == 0
    with pytest.raises(SynthError):
        synth.open_stream(Collector(), voice_id=None)


def test_paths(tmp_path: Path) -> None:
    python = default_python(tmp_path)
    assert python.parts[-4:-2] == ("local-voice", "venv")
    assert python.name == ("python.exe" if os.name == "nt" else "python")
    assert not model_downloaded(tmp_path / "models")
    folder = tmp_path / "models" / "chatterbox-turbo"
    folder.mkdir(parents=True)
    for name in ("t3_turbo_v1.safetensors", "s3gen_meanflow.safetensors", "ve.safetensors"):
        (folder / name).write_bytes(b"x")
    assert model_downloaded(tmp_path / "models")
