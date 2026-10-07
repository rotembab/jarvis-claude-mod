"""How the helper loads the wake words: "Hey Jarvis" at once; plain "Jarvis" only from disk, or when asked for."""

from __future__ import annotations

import hashlib
import itertools
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from jarvis_voice import cli
from jarvis_voice.listen import models as wake_models
from jarvis_voice.listen import wakeword

Downloads = list[tuple[tuple[str, ...], float, bool]]  # files, timeout, whether a deadline was set


class FakeOpenWakeWord:
    """The scorer, without its ONNX models."""

    def __init__(
        self,
        folder: Path,
        model: wake_models.ModelFile = wake_models.HEY_JARVIS,
        plain: wake_models.ModelFile | None = None,
    ) -> None:
        self.name = model.name.removesuffix(".onnx")
        self.plain_name = plain.name.removesuffix(".onnx") if plain is not None else None

    def attach_plain(self, folder: Path, plain: wake_models.ModelFile) -> None:
        assert (folder / plain.name).is_file()
        self.plain_name = plain.name.removesuffix(".onnx")


def put(folder: Path, files: tuple[wake_models.ModelFile, ...]) -> None:
    """Model files of the right size, so they count as downloaded."""
    folder.mkdir(parents=True, exist_ok=True)
    for model in files:
        with (folder / model.name).open("wb") as out:
            out.truncate(model.size)


def names(files: tuple[wake_models.ModelFile, ...]) -> tuple[str, ...]:
    return tuple(model.name for model in files)


@pytest.fixture
def downloads(monkeypatch: pytest.MonkeyPatch) -> Iterator[Downloads]:
    calls: Downloads = []

    def download(
        folder: Path,
        on_progress: object = None,
        files: tuple[wake_models.ModelFile, ...] = wake_models.FILES,
        *,
        timeout: float = wake_models.DOWNLOAD_TIMEOUT_S,
        deadline: float | None = None,
    ) -> Path:
        if deadline is not None:
            assert 0 < deadline - time.monotonic() <= wake_models.PLAIN_FETCH_DEADLINE_S
        calls.append((names(files), timeout, deadline is not None))
        put(folder, files)
        return folder

    monkeypatch.setattr(wake_models, "download", download)
    monkeypatch.setattr(wakeword, "OpenWakeWord", FakeOpenWakeWord)
    yield calls


def test_hey_jarvis_is_returned_without_fetching_plain_jarvis(tmp_path: Path, downloads: Downloads) -> None:
    put(tmp_path, wake_models.HEY_JARVIS_FILES)
    scorer = cli.load_wake(tmp_path)
    assert (scorer.name, scorer.plain_name) == ("hey_jarvis_v0.1", None)
    assert downloads == []  # a slow or blocked host for plain "Jarvis" cannot hold "Hey Jarvis" up


def test_a_missing_hey_jarvis_is_downloaded_and_plain_jarvis_is_not(tmp_path: Path, downloads: Downloads) -> None:
    scorer = cli.load_wake(tmp_path)
    assert downloads == [(names(wake_models.HEY_JARVIS_FILES), wake_models.DOWNLOAD_TIMEOUT_S, False)]
    assert scorer.plain_name is None


def test_plain_jarvis_already_on_disk_is_loaded_with_it(tmp_path: Path, downloads: Downloads) -> None:
    put(tmp_path, (*wake_models.HEY_JARVIS_FILES, wake_models.JARVIS_V2))
    scorer = cli.load_wake(tmp_path)
    assert scorer.plain_name == "jarvis_v2" and downloads == []


def test_a_plain_model_that_will_not_load_leaves_hey_jarvis(
    tmp_path: Path, downloads: Downloads, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Broken(FakeOpenWakeWord):
        def __init__(
            self,
            folder: Path,
            model: wake_models.ModelFile = wake_models.HEY_JARVIS,
            plain: wake_models.ModelFile | None = None,
        ) -> None:
            if plain is not None:
                raise RuntimeError("not an ONNX model")
            super().__init__(folder, model)

    monkeypatch.setattr(wakeword, "OpenWakeWord", Broken)
    put(tmp_path, (*wake_models.HEY_JARVIS_FILES, wake_models.JARVIS_V2))
    assert cli.load_wake(tmp_path).plain_name is None


def test_plain_jarvis_is_fetched_with_short_limits_and_attached(tmp_path: Path, downloads: Downloads) -> None:
    put(tmp_path, wake_models.HEY_JARVIS_FILES)
    scorer = cli.load_wake(tmp_path)
    cli.attach_plain_wake(tmp_path, scorer)
    # A short wait for data, and a limit on the whole fetch.
    assert downloads == [(names(wake_models.PLAIN_JARVIS_FILES), wake_models.PLAIN_FETCH_TIMEOUT_S, True)]
    assert scorer.plain_name == "jarvis_v2"
    assert wake_models.PLAIN_FETCH_TIMEOUT_S < wake_models.DOWNLOAD_TIMEOUT_S
    cli.attach_plain_wake(tmp_path, cli.load_wake(tmp_path))  # on disk now: nothing more to fetch
    assert len(downloads) == 1


class FakeResponse:
    """An HTTP response from a fake host: ``chunks`` in order, one per read, each after ``delay`` seconds."""

    def __init__(self, chunks: Iterator[bytes], delay: float = 0.0) -> None:
        self._chunks = chunks
        self._delay = delay
        self.reads = 0

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read1(self, n: int = -1) -> bytes:
        self.reads += 1
        time.sleep(self._delay)
        return next(self._chunks, b"")


def host(monkeypatch: pytest.MonkeyPatch, response: FakeResponse) -> None:
    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout: response)


def model_file(payload: bytes) -> wake_models.ModelFile:
    return wake_models.ModelFile("plain.onnx", hashlib.sha256(payload).hexdigest(), len(payload), "http://fake/plain.onnx")


def test_a_host_that_trickles_bytes_runs_into_the_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Each byte comes well inside the wait for data, so only the deadline ends it
    # (after 200 bytes the fake gives up, so a broken deadline fails rather than hangs).
    trickle = FakeResponse(itertools.repeat(b"x", 200), delay=0.02)
    host(monkeypatch, trickle)
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="plain.onnx"):
        wake_models.download(tmp_path, files=(model_file(b"x" * 200_000),), deadline=started + 0.2)
    assert time.monotonic() - started < 2.0 and trickle.reads > 1
    assert not (tmp_path / "plain.onnx").exists()


def test_a_download_inside_the_deadline_completes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = b"onnx " * 1000
    host(monkeypatch, FakeResponse(iter([payload[:3000], payload[3000:]])))
    model = model_file(payload)
    wake_models.download(tmp_path, files=(model,), deadline=time.monotonic() + 30)
    assert (tmp_path / "plain.onnx").read_bytes() == payload and wake_models.is_downloaded(tmp_path, (model,))
