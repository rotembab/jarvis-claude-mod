from __future__ import annotations

import http.client
import json
import socket
from collections.abc import Iterator
from typing import Any

import pytest

from jarvis_voice.control import MAX_BODY_BYTES, ControlServer

TOKEN = "s3cret-token-0123456789"


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, name: str, body: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, body))
        if name == "status":
            raise RuntimeError("boom")
        return {"ok": True, "echo": name}


@pytest.fixture
def server() -> Iterator[tuple[ControlServer, Recorder]]:
    recorder = Recorder()
    srv = ControlServer(TOKEN, recorder).start()
    yield srv, recorder
    srv.stop()


def request(
    port: int,
    path: str = "/v1/heartbeat",
    body: bytes | None = b"{}",
    *,
    method: str = "POST",
    token: str | None = TOKEN,
    host: str | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    conn.putheader("Host", host if host is not None else f"127.0.0.1:{port}")
    if token is not None:
        conn.putheader("Authorization", f"Bearer {token}")
    for k, v in (headers or {}).items():
        conn.putheader(k, v)
    if body is not None:
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(len(body)))
    conn.endheaders(body)
    resp = conn.getresponse()
    payload = json.loads(resp.read() or b"{}")
    conn.close()
    return resp.status, payload


def test_binds_loopback_with_os_assigned_port(server: tuple[ControlServer, Recorder]) -> None:
    srv, _ = server
    assert srv.port > 0
    assert srv._httpd.socket.getsockname()[0] == "127.0.0.1"


def test_authorised_command_reaches_handler(server: tuple[ControlServer, Recorder]) -> None:
    srv, rec = server
    status, payload = request(
        srv.port, "/v1/speak", json.dumps({"replyId": "r", "seq": 0, "text": "Hi.", "final": False}).encode()
    )
    assert (status, payload) == (200, {"ok": True, "echo": "speak"})
    assert rec.calls == [("speak", {"replyId": "r", "seq": 0, "text": "Hi.", "final": False})]


def test_localhost_host_header_accepted(server: tuple[ControlServer, Recorder]) -> None:
    srv, _ = server
    assert request(srv.port, host=f"localhost:{srv.port}")[0] == 200


def test_empty_body_is_an_empty_object(server: tuple[ControlServer, Recorder]) -> None:
    srv, rec = server
    assert request(srv.port, body=b"")[0] == 200
    assert rec.calls == [("heartbeat", {})]


@pytest.mark.parametrize("token", [None, "", "wrong", TOKEN + "x", TOKEN[:-1]])
def test_bad_or_missing_token_rejected(server: tuple[ControlServer, Recorder], token: str | None) -> None:
    srv, rec = server
    status, payload = request(srv.port, token=token)
    assert status == 401 and payload["error"]["code"] == "unauthorized"
    assert rec.calls == []


def test_wrong_scheme_rejected(server: tuple[ControlServer, Recorder]) -> None:
    srv, _ = server
    status, _ = request(srv.port, token=None, headers={"Authorization": f"Basic {TOKEN}"})
    assert status == 401


def test_origin_header_rejected_even_with_token(server: tuple[ControlServer, Recorder]) -> None:
    srv, rec = server
    status, payload = request(srv.port, headers={"Origin": "https://evil.example"})
    assert status == 403 and payload["error"]["code"] == "unauthorized"
    status, _ = request(srv.port, headers={"Origin": "null"})
    assert status == 403
    assert rec.calls == []


@pytest.mark.parametrize("host", ["evil.example", "127.0.0.1", "127.0.0.1:1", "attacker.localhost:{port}", ""])
def test_unexpected_host_rejected(server: tuple[ControlServer, Recorder], host: str) -> None:
    srv, rec = server
    status, _ = request(srv.port, host=host.format(port=srv.port))
    assert status == 403
    assert rec.calls == []


def test_body_limit(server: tuple[ControlServer, Recorder]) -> None:
    srv, rec = server
    big = json.dumps({"text": "x" * MAX_BODY_BYTES}).encode()
    status, payload = request(srv.port, "/v1/test_voice", big)
    assert status == 413 and payload["error"]["code"] == "bad_request"
    assert rec.calls == []


def test_body_limit_checked_before_reading(server: tuple[ControlServer, Recorder]) -> None:
    # Claim a huge body but send none: the server must answer without waiting for it.
    srv, _ = server
    with socket.create_connection(("127.0.0.1", srv.port), timeout=5) as sock:
        sock.sendall(
            (
                f"POST /v1/heartbeat HTTP/1.1\r\nHost: 127.0.0.1:{srv.port}\r\nAuthorization: Bearer {TOKEN}\r\n"
                f"Content-Length: {10 * 1024 * 1024}\r\n\r\n"
            ).encode()
        )
        assert b" 413 " in sock.recv(4096).split(b"\r\n", 1)[0]


def test_chunked_bodies_refused(server: tuple[ControlServer, Recorder]) -> None:
    srv, _ = server
    status, _ = request(srv.port, body=None, headers={"Transfer-Encoding": "chunked"})
    assert status == 411


def test_invalid_json_and_schema_violations(server: tuple[ControlServer, Recorder]) -> None:
    srv, rec = server
    status, payload = request(srv.port, "/v1/speak", b"{not json")
    assert status == 400 and payload["error"]["code"] == "bad_request"
    status, payload = request(srv.port, "/v1/speak", json.dumps({"seq": 0, "text": "x", "final": False}).encode())
    assert status == 400 and "replyId" in payload["error"]["message"]
    status, _ = request(srv.port, "/v1/listen", json.dumps({"action": "dance"}).encode())
    assert status == 400
    status, _ = request(srv.port, "/v1/heartbeat", b"[1, 2]")
    assert status == 400
    assert rec.calls == []


def test_unknown_command_and_paths(server: tuple[ControlServer, Recorder]) -> None:
    srv, _ = server
    assert request(srv.port, "/v1/format_disk")[0] == 404
    assert request(srv.port, "/v2/heartbeat")[0] == 404
    assert request(srv.port, "/v1/heartbeat/extra")[0] == 404


def test_get_not_allowed(server: tuple[ControlServer, Recorder]) -> None:
    srv, _ = server
    assert request(srv.port, method="GET", body=None)[0] == 405
    assert request(srv.port, method="GET", body=None, token="nope")[0] == 401


def test_handler_exception_becomes_internal_error(server: tuple[ControlServer, Recorder]) -> None:
    srv, _ = server
    status, payload = request(srv.port, "/v1/status")
    assert status == 500 and payload["error"]["code"] == "internal"
    # The server keeps serving afterwards.
    assert request(srv.port)[0] == 200


def test_keep_alive_connection_serves_several_requests(server: tuple[ControlServer, Recorder]) -> None:
    srv, rec = server
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    for _ in range(3):
        conn.request("POST", "/v1/heartbeat", body=b"{}", headers={"Authorization": f"Bearer {TOKEN}"})
        resp = conn.getresponse()
        assert resp.status == 200
        resp.read()
    conn.close()
    assert len(rec.calls) == 3


def test_empty_token_refused() -> None:
    with pytest.raises(ValueError):
        ControlServer("", lambda n, b: {"ok": True})
