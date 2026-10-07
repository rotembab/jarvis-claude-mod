from __future__ import annotations

import hashlib
import re
import socket
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from jarvis_hands import models

PAYLOAD = bytes(range(256)) * 1200  # 300 KB stands in for the real 7.8 MB model
PAYLOAD_SHA = hashlib.sha256(PAYLOAD).hexdigest()


@dataclass
class Route:
    body: bytes
    status: int = 200
    #: Content-Length to announce when it differs from the body (a cut-off transfer).
    length: int | None = None
    send_length: bool = True


@dataclass
class ModelServer:
    """A plain HTTP server on 127.0.0.1 that serves fixed bytes per path (never the real network)."""

    routes: dict[str, Route] = field(default_factory=dict)
    requests: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                owner.requests.append(self.path)
                route = owner.routes.get(urlsplit(self.path).path)
                if route is None:
                    self.send_error(404, "Not Found")
                    return
                self.send_response(route.status)
                if route.send_length:
                    self.send_header("Content-Length", str(route.length or len(route.body)))
                self.end_headers()
                self.wfile.write(route.body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def url(self, path: str = "/hand_landmarker.task") -> str:
        return f"http://127.0.0.1:{self.httpd.server_port}{path}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Iterator[ModelServer]:
    for name in ("NO_PROXY", "no_proxy"):  # a proxy configured on the test machine must not see loopback
        monkeypatch.setenv(name, "127.0.0.1,localhost")
    srv = ModelServer()
    srv.routes["/hand_landmarker.task"] = Route(PAYLOAD)
    yield srv
    srv.close()


def leftovers(data_dir: Path) -> list[str]:
    folder = models.models_dir(data_dir)
    return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


# --------------------------------------------------------------------------- constants and paths


def test_pinned_model_constants() -> None:
    assert models.MODEL_URL.startswith("https://storage.googleapis.com/")
    assert models.MODEL_URL.endswith("/" + models.MODEL_NAME)
    assert re.fullmatch(r"[0-9a-f]{64}", models.MODEL_SHA256)
    assert models.MODEL_SIZE == 7_819_105


def test_paths(tmp_path: Path) -> None:
    assert models.models_dir(tmp_path) == tmp_path / "models" / "hands"
    assert models.model_path(tmp_path) == tmp_path / "models" / "hands" / "hand_landmarker.task"


def test_is_installed_checks_the_pinned_size(tmp_path: Path) -> None:
    assert not models.is_installed(tmp_path)
    path = models.model_path(tmp_path)
    path.parent.mkdir(parents=True)
    with path.open("wb") as fh:
        fh.truncate(models.MODEL_SIZE)
    assert models.is_installed(tmp_path)
    with path.open("r+b") as fh:
        fh.truncate(models.MODEL_SIZE - 1)
    assert not models.is_installed(tmp_path)


# --------------------------------------------------------------------------- download


def test_download_verifies_and_reports_progress(server: ModelServer, tmp_path: Path) -> None:
    lines: list[dict[str, Any]] = []
    path = models.ensure_model(tmp_path, url=server.url(), sha256=PAYLOAD_SHA, progress=lines.append)
    assert path == models.model_path(tmp_path)
    assert path.read_bytes() == PAYLOAD
    assert leftovers(tmp_path) == ["hand_landmarker.task"]  # no .part left behind
    downloads = [line for line in lines if line["step"] == "download"]
    assert downloads[0] == {
        "type": "progress",
        "step": "download",
        "pct": 0.0,
        "message": "downloading the hand model",
    }
    assert downloads[-1]["pct"] == 100.0
    assert lines[-1]["step"] == "verify" and "pct" not in lines[-1]
    assert all(line["type"] == "progress" and isinstance(line["message"], str) for line in lines)
    assert all(isinstance(line["pct"], float) for line in downloads)


def test_progress_is_reported_at_most_every_five_percent(
    server: ModelServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(models, "CHUNK_BYTES", 1024)  # 300 chunks of a third of a percent each
    lines: list[dict[str, Any]] = []
    models.ensure_model(tmp_path, url=server.url(), sha256=PAYLOAD_SHA, progress=lines.append)
    pcts = [line["pct"] for line in lines if line["step"] == "download"]
    assert len(pcts) <= 21
    buckets = [int(p // 5) for p in pcts]
    assert buckets == sorted(set(buckets))  # one line per 5% band at most, never backwards
    assert pcts[-1] == 100.0


def test_progress_without_content_length(server: ModelServer, tmp_path: Path) -> None:
    server.routes["/hand_landmarker.task"] = Route(PAYLOAD, send_length=False)
    lines: list[dict[str, Any]] = []
    path = models.ensure_model(tmp_path, url=server.url(), sha256=PAYLOAD_SHA, progress=lines.append)
    assert path.read_bytes() == PAYLOAD
    assert [line["pct"] for line in lines if line["step"] == "download"] == [0.0]


def test_progress_is_optional(server: ModelServer, tmp_path: Path) -> None:
    assert models.ensure_model(tmp_path, url=server.url(), sha256=PAYLOAD_SHA.upper()).read_bytes() == PAYLOAD


def test_checksum_mismatch_is_refused_and_cleaned_up(server: ModelServer, tmp_path: Path) -> None:
    with pytest.raises(models.ModelError, match="checksum"):
        models.ensure_model(tmp_path, url=server.url(), sha256="0" * 64)
    assert leftovers(tmp_path) == []


def test_default_checksum_also_checks_the_pinned_size(server: ModelServer, tmp_path: Path) -> None:
    with pytest.raises(models.ModelError, match="wrong size"):
        models.ensure_model(tmp_path, url=server.url())
    assert leftovers(tmp_path) == []


@pytest.mark.parametrize("status", [403, 404, 500])
def test_http_errors(server: ModelServer, tmp_path: Path, status: int) -> None:
    server.routes["/hand_landmarker.task"] = Route(b"nope", status=status)
    with pytest.raises(models.ModelError, match=f"HTTP {status}"):
        models.ensure_model(tmp_path, url=server.url(), sha256=PAYLOAD_SHA)
    assert leftovers(tmp_path) == []


def test_unreachable_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    with socket.socket() as sock:  # a port nobody listens on
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with pytest.raises(models.ModelError, match=r"could not reach 127\.0\.0\.1"):
        models.ensure_model(tmp_path, url=f"http://127.0.0.1:{port}/m.task", sha256=PAYLOAD_SHA, timeout=5)
    assert leftovers(tmp_path) == []


def test_cut_off_transfer(server: ModelServer, tmp_path: Path) -> None:
    server.routes["/hand_landmarker.task"] = Route(PAYLOAD[:1000], length=len(PAYLOAD))
    with pytest.raises(models.ModelError, match=r"cut short|failed"):
        models.ensure_model(tmp_path, url=server.url(), sha256=PAYLOAD_SHA, timeout=5)
    assert leftovers(tmp_path) == []


def test_oversized_download_is_stopped(server: ModelServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(models, "MAX_DOWNLOAD_BYTES", 100_000)
    with pytest.raises(models.ModelError, match="larger than expected"):
        models.ensure_model(tmp_path, url=server.url(), sha256=PAYLOAD_SHA)
    assert leftovers(tmp_path) == []


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/m.task", "hand_landmarker.task"])
def test_only_http_urls(tmp_path: Path, url: str) -> None:
    with pytest.raises(models.ModelError, match="not an http"):
        models.ensure_model(tmp_path, url=url, sha256=PAYLOAD_SHA)


def test_a_good_model_is_not_downloaded_again(server: ModelServer, tmp_path: Path) -> None:
    models.ensure_model(tmp_path, url=server.url(), sha256=PAYLOAD_SHA)
    assert len(server.requests) == 1
    lines: list[dict[str, Any]] = []
    models.ensure_model(tmp_path, url=server.url(), sha256=PAYLOAD_SHA, progress=lines.append)
    assert len(server.requests) == 1
    assert [line["step"] for line in lines] == ["verify"]


def test_a_damaged_model_is_replaced(server: ModelServer, tmp_path: Path) -> None:
    path = models.model_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"half a model")
    assert models.ensure_model(tmp_path, url=server.url(), sha256=PAYLOAD_SHA).read_bytes() == PAYLOAD
    assert len(server.requests) == 1


def test_a_failed_download_keeps_the_previous_file(server: ModelServer, tmp_path: Path) -> None:
    path = models.model_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"old")
    with pytest.raises(models.ModelError):
        models.ensure_model(tmp_path, url=server.url(), sha256="1" * 64)
    assert path.read_bytes() == b"old"  # only a verified download replaces it
    assert leftovers(tmp_path) == ["hand_landmarker.task"]
