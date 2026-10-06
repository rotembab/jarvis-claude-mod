"""Token-protected HTTP control server on 127.0.0.1 (mod -> helper commands).

``POST /v1/<command>`` with ``Authorization: Bearer <token>`` and a JSON body.
Browsers are shut out twice over: any ``Origin`` header is rejected (CSRF) and
``Host`` must be ``127.0.0.1:<port>`` or ``localhost:<port>`` (DNS rebinding).
"""

from __future__ import annotations

import hmac
import json
import logging
import socket
import socketserver
import sys
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import __version__, protocol

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 64 * 1024

CommandHandler = Callable[[str, dict[str, Any]], dict[str, Any]]


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # SO_REUSEADDR on Windows would let another socket bind our port.
    allow_reuse_address = False

    def __init__(self, control: ControlServer, address: tuple[str, int]) -> None:
        self.control = control
        super().__init__(address, _Handler)

    def server_bind(self) -> None:
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        # Skip HTTPServer.server_bind: its socket.getfqdn() can stall for
        # seconds on Windows machines with slow reverse DNS.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name, self.server_port = str(host), int(port)


class _Handler(BaseHTTPRequestHandler):
    server: _Server
    protocol_version = "HTTP/1.1"
    server_version = f"jarvis-voice/{__version__}"
    sys_version = ""
    timeout = 10  # idle keep-alive connections and slow clients

    # -- plumbing

    def log_message(self, format: str, *args: Any) -> None:
        log.debug("http %s - %s", self.address_string(), format % args)

    def _send(self, status: int, payload: dict[str, Any], *, close: bool = False) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    def _fail(self, status: int, code: protocol.ErrorCode, message: str, *, close: bool = False) -> None:
        self._send(status, protocol.error_response(code, message), close=close)

    def _guard(self) -> bool:
        """Origin / Host / token checks shared by every method. True = allowed."""
        control = self.server.control
        if self.headers.get("Origin") is not None:
            self._fail(403, "unauthorized", "requests with an Origin header are not accepted", close=True)
            return False
        host = (self.headers.get("Host") or "").strip().lower()
        port = control.port
        if host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            self._fail(403, "unauthorized", "unexpected Host header", close=True)
            return False
        auth = self.headers.get("Authorization") or ""
        scheme, _, given = auth.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            given.strip().encode("utf-8"), control.token.encode("utf-8")
        ):
            self._fail(401, "unauthorized", "missing or invalid bearer token", close=True)
            return False
        return True

    # -- methods

    def do_POST(self) -> None:
        if not self._guard():
            return
        path = self.path.split("?", 1)[0]
        if not path.startswith("/v1/") or "/" in path[4:]:
            self._drain_and_fail(404, f"unknown path {path}")
            return
        name = path[4:]
        if self.headers.get("Transfer-Encoding"):
            self._fail(411, "bad_request", "chunked bodies are not supported; send Content-Length", close=True)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._fail(400, "bad_request", "invalid Content-Length", close=True)
            return
        if length < 0:
            self._fail(400, "bad_request", "invalid Content-Length", close=True)
            return
        if length > MAX_BODY_BYTES:
            # Do not read it: answer and drop the connection.
            self._fail(413, "bad_request", f"body larger than {MAX_BODY_BYTES} bytes", close=True)
            return
        raw = self.rfile.read(length) if length else b""
        if name not in protocol.load_schema().commands:
            self._fail(404, "bad_request", f"unknown command {name!r}")
            return
        try:
            body = json.loads(raw.decode("utf-8")) if raw.strip() else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._fail(400, "bad_request", f"body is not valid JSON: {exc}")
            return
        try:
            protocol.validate_command(name, body)
        except protocol.ValidationError as exc:
            self._fail(400, "bad_request", str(exc))
            return
        try:
            response = self.server.control.handler(name, body)
        except Exception as exc:  # the handler must never take the server down
            log.exception("command %s failed", name)
            self._fail(500, "internal", f"{type(exc).__name__}: {exc}")
            return
        # Domain-level failures are still HTTP 200 with ok:false.
        self._send(200, response)

    def _drain_and_fail(self, status: int, message: str) -> None:
        self._fail(status, "bad_request", message, close=True)

    def _method_not_allowed(self) -> None:
        if self._guard():
            self._fail(405, "bad_request", "use POST", close=True)

    do_GET = do_PUT = do_DELETE = do_PATCH = _method_not_allowed  # noqa: N815


class ControlServer:
    """Owns the listening socket and the serving thread."""

    def __init__(self, token: str, handler: CommandHandler, *, host: str = "127.0.0.1") -> None:
        if not token:
            raise ValueError("a non-empty token is required")
        self.token = token
        self.handler = handler
        self._httpd = _Server(self, (host, 0))
        self.port: int = self._httpd.server_port
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, kwargs={"poll_interval": 0.1}, name="control-server", daemon=True
        )

    def start(self) -> ControlServer:
        self._thread.start()
        log.info("control server listening on 127.0.0.1:%d", self.port)
        return self

    def stop(self) -> None:
        if self._thread.is_alive():
            self._httpd.shutdown()
        self._httpd.server_close()
