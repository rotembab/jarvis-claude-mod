from __future__ import annotations

import contextlib
import http.client
import json
import socket
import sys
from collections.abc import Iterator
from typing import Any

import pytest

from jarvis_hands import control as control_module
from jarvis_hands import protocol
from jarvis_hands.control import MAX_BODY_BYTES, ControlServer

TOKEN = "s3cret-token-0123456789"


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, name: str, body: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, body))
        if name == "status":
            raise RuntimeError("boom")
        if name == "pause":
            return {"ok": True, "fps": float("nan")}  # not JSON: must not reach the mod
        if name == "resume":
            return protocol.error_response("camera_in_use", "busy")
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


def post(port: int, name: str, body: Any) -> tuple[int, dict[str, Any]]:
    return request(port, f"/v1/{name}", json.dumps(body).encode())


def test_binds_loopback_with_os_assigned_port(server: tuple[ControlServer, Recorder]) -> None:
    srv, _ = server
    assert srv.port > 0
    assert srv._httpd.socket.getsockname()[0] == "127.0.0.1"


@pytest.mark.skipif(sys.platform != "win32", reason="SO_EXCLUSIVEADDRUSE is Windows-only")
def test_windows_socket_is_exclusive(server: tuple[ControlServer, Recorder]) -> None:
    srv, _ = server
    assert srv._httpd.socket.getsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE) != 0  # type: ignore[attr-defined]


def test_authorised_command_reaches_handler(server: tuple[ControlServer, Recorder]) -> None:
    srv, rec = server
    status, payload = post(srv.port, "config", {"engage": "always", "displays": [2], "scrollSpeed": 1.5})
    assert (status, payload) == (200, {"ok": True, "echo": "config"})
    assert rec.calls == [("config", {"engage": "always", "displays": [2], "scrollSpeed": 1.5})]
    assert post(srv.port, "calibrate", {"action": "start"})[0] == 200


def test_domain_errors_are_http_200(server: tuple[ControlServer, Recorder]) -> None:
    srv, _ = server
    status, payload = post(srv.port, "resume", {})
    assert status == 200 and payload == {"ok": False, "error": {"code": "camera_in_use", "message": "busy"}}


@pytest.mark.parametrize("name", sorted(protocol.COMMAND_NAMES - {"calibrate", "status", "pause", "resume"}))
def test_every_command_is_routed(server: tuple[ControlServer, Recorder], name: str) -> None:
    srv, rec = server
    assert post(srv.port, name, {}) == (200, {"ok": True, "echo": name})
    assert rec.calls == [(name, {})]


def test_localhost_host_header_accepted(server: tuple[ControlServer, Recorder]) -> None:
    srv, _ = server
    assert request(srv.port, host=f"localhost:{srv.port}")[0] == 200
    assert request(srv.port, host=f"LOCALHOST:{srv.port}")[0] == 200


def test_empty_body_is_an_empty_object(server: tuple[ControlServer, Recorder]) -> None:
    srv, rec = server
    assert request(srv.port, body=b"")[0] == 200
    assert request(srv.port, body=None)[0] == 200
    assert rec.calls == [("heartbeat", {}), ("heartbeat", {})]


@pytest.mark.parametrize("token", [None, "", "wrong", TOKEN + "x", TOKEN[:-1], TOKEN.upper()])
def test_bad_or_missing_token_rejected(server: tuple[ControlServer, Recorder], token: str | None) -> None:
    srv, rec = server
    status, payload = request(srv.port, token=token)
    assert status == 401 and payload["error"]["code"] == "unauthorized"
    assert rec.calls == []


def test_wrong_scheme_rejected(server: tuple[ControlServer, Recorder]) -> None:
    srv, _ = server
    status, _ = request(srv.port, token=None, headers={"Authorization": f"Basic {TOKEN}"})
    assert status == 401
    status, _ = request(srv.port, token=None, headers={"Authorization": TOKEN})
    assert status == 401


def test_origin_header_rejected_even_with_token(server: tuple[ControlServer, Recorder]) -> None:
    srv, rec = server
    status, payload = request(srv.port, headers={"Origin": "https://evil.example"})
    assert status == 403 and payload["error"]["code"] == "unauthorized"
    assert request(srv.port, headers={"Origin": "null"})[0] == 403
    assert request(srv.port, headers={"Origin": f"http://127.0.0.1:{srv.port}"})[0] == 403
    assert request(srv.port, headers={"Origin": ""})[0] == 403
    assert rec.calls == []


@pytest.mark.parametrize(
    "host", ["evil.example", "127.0.0.1", "127.0.0.1:1", "attacker.localhost:{port}", "[::1]:{port}", ""]
)
def test_unexpected_host_rejected(server: tuple[ControlServer, Recorder], host: str) -> None:
    srv, rec = server
    status, _ = request(srv.port, host=host.format(port=srv.port))
    assert status == 403
    assert rec.calls == []


def test_body_limit(server: tuple[ControlServer, Recorder]) -> None:
    srv, rec = server
    big = json.dumps({"engage": "palm", "pad": "x" * MAX_BODY_BYTES}).encode()
    status, payload = request(srv.port, "/v1/config", big)
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


def test_chunked_and_bad_lengths_refused(server: tuple[ControlServer, Recorder]) -> None:
    srv, _ = server
    assert request(srv.port, body=None, headers={"Transfer-Encoding": "chunked"})[0] == 411
    assert request(srv.port, body=None, headers={"Content-Length": "abc"})[0] == 400
    assert request(srv.port, body=None, headers={"Content-Length": "-5"})[0] == 400


def test_invalid_json_and_schema_violations(server: tuple[ControlServer, Recorder]) -> None:
    srv, rec = server
    status, payload = request(srv.port, "/v1/config", b"{not json")
    assert status == 400 and payload["error"]["code"] == "bad_request"
    status, payload = post(srv.port, "config", {"scrollSpeed": 99})
    assert status == 400 and "scrollSpeed" in payload["error"]["message"]
    status, payload = post(srv.port, "calibrate", {})
    assert status == 400 and "action" in payload["error"]["message"]
    assert post(srv.port, "calibrate", {"action": "dance"})[0] == 400
    assert post(srv.port, "config", {"displays": [0]})[0] == 400
    assert request(srv.port, "/v1/heartbeat", b"[1, 2]")[0] == 400
    assert request(srv.port, "/v1/heartbeat", b"\xff\xfe")[0] == 400
    assert rec.calls == []


@pytest.mark.parametrize("literal", [b"NaN", b"Infinity", b"-Infinity"])
def test_nan_and_infinity_are_not_json(server: tuple[ControlServer, Recorder], literal: bytes) -> None:
    srv, rec = server
    status, payload = request(srv.port, "/v1/config", b'{"scrollSpeed": ' + literal + b"}")
    assert status == 400 and payload["error"]["code"] == "bad_request"
    assert rec.calls == []


def test_deeply_nested_body_gets_a_400(server: tuple[ControlServer, Recorder]) -> None:
    # Well under the size cap, but deeper than the JSON decoder recurses.
    srv, rec = server
    status, payload = request(srv.port, "/v1/config", b"[" * 60000)
    assert status == 400 and payload["error"]["code"] == "bad_request"
    assert request(srv.port)[0] == 200  # still serving
    assert rec.calls == [("heartbeat", {})]


def test_integers_beyond_float_range_are_checked_not_crashed(server: tuple[ControlServer, Recorder]) -> None:
    srv, rec = server
    huge = b"1" + b"0" * 400
    assert request(srv.port, "/v1/config", b'{"displays":[' + huge + b"]}")[0] == 200  # valid per the schema
    status, payload = request(srv.port, "/v1/config", b'{"scrollSpeed":' + huge + b"}")
    assert status == 400 and "scrollSpeed" in payload["error"]["message"]
    # Past Python's int-parsing digit limit: refused, not dropped.
    assert request(srv.port, "/v1/config", b'{"displays":[1' + b"0" * 5000 + b"]}")[0] == 400
    assert [name for name, _ in rec.calls] == ["config"]


def test_a_validator_crash_still_gets_a_400(
    server: tuple[ControlServer, Recorder], monkeypatch: pytest.MonkeyPatch
) -> None:
    srv, rec = server

    def broken(name: str, body: Any) -> None:
        raise TypeError("unhashable")

    monkeypatch.setattr(protocol, "validate_command", broken)
    status, payload = post(srv.port, "config", {"engage": "palm"})
    assert status == 400 and payload["error"]["code"] == "bad_request"
    assert rec.calls == []


def test_unknown_command_and_paths(server: tuple[ControlServer, Recorder]) -> None:
    srv, rec = server
    status, payload = request(srv.port, "/v1/format_disk")
    assert status == 404 and payload["error"]["code"] == "bad_request"
    assert request(srv.port, "/v2/heartbeat")[0] == 404
    assert request(srv.port, "/v1/heartbeat/extra")[0] == 404
    assert request(srv.port, "/heartbeat")[0] == 404
    assert request(srv.port, "/v1/")[0] == 404
    assert request(srv.port, "/v1/heartbeat?x=1")[0] == 200  # a query string is ignored
    assert rec.calls == [("heartbeat", {})]


def test_get_not_allowed(server: tuple[ControlServer, Recorder]) -> None:
    srv, _ = server
    assert request(srv.port, method="GET", body=None)[0] == 405
    assert request(srv.port, method="GET", body=None, token="nope")[0] == 401
    assert request(srv.port, method="DELETE", body=None)[0] == 405


def test_handler_exception_becomes_internal_error(server: tuple[ControlServer, Recorder]) -> None:
    srv, _ = server
    status, payload = post(srv.port, "status", {})
    assert status == 500 and payload["error"]["code"] == "internal"
    # The server keeps serving afterwards.
    assert request(srv.port)[0] == 200


def test_unencodable_response_becomes_internal_error(server: tuple[ControlServer, Recorder]) -> None:
    srv, _ = server
    status, payload = post(srv.port, "pause", {})
    assert status == 500 and payload["error"]["code"] == "internal"


def test_keep_alive_connection_serves_several_requests(server: tuple[ControlServer, Recorder]) -> None:
    srv, rec = server
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    for name in ("heartbeat", "format_disk", "heartbeat"):  # an unknown command still reads its body
        conn.request("POST", f"/v1/{name}", body=b'{"a": 1}', headers={"Authorization": f"Bearer {TOKEN}"})
        resp = conn.getresponse()
        resp.read()
    conn.request("POST", "/v1/engage", body=b"{}", headers={"Authorization": f"Bearer {TOKEN}"})
    resp = conn.getresponse()
    assert resp.status == 200
    resp.read()
    conn.close()
    assert rec.calls == [("engage", {})]


def test_server_header_names_the_hands_helper(server: tuple[ControlServer, Recorder]) -> None:
    srv, _ = server
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    conn.request("POST", "/v1/heartbeat", body=b"{}", headers={"Authorization": f"Bearer {TOKEN}"})
    resp = conn.getresponse()
    resp.read()
    conn.close()
    assert (resp.getheader("Server") or "").startswith("jarvis-hands/")
    assert resp.getheader("Cache-Control") == "no-store"


def test_empty_token_refused() -> None:
    with pytest.raises(ValueError):
        ControlServer("", lambda n, b: {"ok": True})


def test_stop_frees_the_port() -> None:
    srv = ControlServer(TOKEN, lambda n, b: {"ok": True}).start()
    port = srv.port
    srv.stop()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1).close()


# -- what happens after the answer is written -------------------------------------------


def test_on_sent_runs_after_the_answer_is_on_the_wire() -> None:
    """``shutdown`` tears this server down: the answer must be written before the helper winds down."""
    order: list[str] = []
    recorder = Recorder()

    def handler(name: str, body: dict[str, Any]) -> dict[str, Any]:
        order.append(f"handled {name}")
        return recorder(name, body)

    written: list[float] = []
    original = control_module._Handler._send_encoded

    def record_write(self: Any, status: int, body: bytes, *, close: bool = False) -> None:
        order.append("answered")
        written.append(status)
        original(self, status, body, close=close)

    srv = ControlServer(TOKEN, handler, on_sent=lambda name: order.append(f"sent {name}"))
    srv._httpd.RequestHandlerClass._send_encoded = record_write  # type: ignore[attr-defined]
    srv.start()
    try:
        assert post(srv.port, "shutdown", {}) == (200, {"ok": True, "echo": "shutdown"})
    finally:
        srv._httpd.RequestHandlerClass._send_encoded = original  # type: ignore[attr-defined]
        srv.stop()
    assert order == ["handled shutdown", "answered", "sent shutdown"]
    assert written == [200]


def test_on_sent_runs_even_when_the_answer_could_not_be_written() -> None:
    """Otherwise a lost answer would leave the helper running with nobody to stop it."""
    sent: list[str] = []
    original = control_module._Handler._send_encoded

    def refuse(self: Any, status: int, body: bytes, *, close: bool = False) -> None:
        raise OSError("the client went away")

    srv = ControlServer(TOKEN, Recorder(), on_sent=sent.append)
    srv._httpd.RequestHandlerClass._send_encoded = refuse  # type: ignore[attr-defined]
    srv.start()
    try:
        with contextlib.suppress(Exception):
            post(srv.port, "shutdown", {})
    finally:
        srv._httpd.RequestHandlerClass._send_encoded = original  # type: ignore[attr-defined]
        srv.stop()
    assert sent == ["shutdown"]


def test_a_failing_on_sent_hook_does_not_break_the_answer() -> None:
    def boom(name: str) -> None:
        raise RuntimeError("nope")

    srv = ControlServer(TOKEN, Recorder(), on_sent=boom).start()
    try:
        assert post(srv.port, "heartbeat", {}) == (200, {"ok": True, "echo": "heartbeat"})
    finally:
        srv.stop()
