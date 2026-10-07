"""Small network helpers the drivers share: plain HTTP to LAN devices, and Wake-on-LAN.

HTTP goes through ``http.client`` directly, never ``urllib``: urllib applies
the system proxy (on Windows from the registry, and it treats any dotted
LAN address as "not local"), and it rewrites header names (``X-Auth-PSK``
becomes ``X-auth-psk``), which some TVs may not accept.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import re
import socket
import ssl
import time
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlsplit

HttpFailure = Literal["unreachable", "timeout", "tls", "bad_url"]


class HttpError(Exception):
    """The request never got an HTTP answer. ``kind`` says why; the message names the host, never a header."""

    def __init__(self, kind: HttpFailure, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(slots=True)
class HttpResponse:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        """The body as JSON; ValueError when it is not."""
        return json.loads(self.body.decode("utf-8"))


def request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    json_body: Any = None,
    timeout: float = 5.0,
    verify_tls: bool = True,
) -> HttpResponse:
    """One HTTP request with no proxy, header names sent exactly as given."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise HttpError("bad_url", f"not an http(s) address: {parts.scheme}://{parts.hostname or ''}")
    host = parts.hostname
    port = parts.port
    path = parts.path or "/"
    if parts.query:
        path += f"?{parts.query}"
    sent = dict(headers or {})
    if json_body is not None:
        body = json.dumps(json_body, separators=(",", ":")).encode("utf-8")
        sent.setdefault("Content-Type", "application/json")
    connection: http.client.HTTPConnection
    if parts.scheme == "https":
        context = ssl.create_default_context()
        if not verify_tls:
            # Home hubs and TVs often use self-signed certificates on the LAN.
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        connection = http.client.HTTPSConnection(host, port, timeout=timeout, context=context)
    else:
        connection = http.client.HTTPConnection(host, port, timeout=timeout)
    where = f"{host}:{port}" if port else host
    try:
        connection.request(method, path, body=body, headers=sent)
        response = connection.getresponse()
        data = response.read()
        return HttpResponse(response.status, data, {k.lower(): v for k, v in response.getheaders()})
    except TimeoutError:
        raise HttpError("timeout", f"{where} did not answer within {timeout:g} s") from None
    except ssl.SSLError as exc:
        raise HttpError("tls", f"{where}: TLS failed ({exc.reason or type(exc).__name__})") from None
    except (OSError, http.client.HTTPException) as exc:
        raise HttpError("unreachable", f"{where} is unreachable ({type(exc).__name__})") from None
    finally:
        connection.close()


# --------------------------------------------------------------------------- Wake-on-LAN

_MAC = re.compile(r"^[0-9a-f]{2}([:-]?)[0-9a-f]{2}(\1[0-9a-f]{2}){4}$", re.IGNORECASE)


def normalize_mac(mac: str) -> str | None:
    """``aa:bb:cc:dd:ee:ff`` for any common spelling, else None."""
    text = mac.strip()
    if not _MAC.match(text):
        return None
    digits = re.sub(r"[^0-9a-f]", "", text.lower())
    return ":".join(digits[i : i + 2] for i in range(0, 12, 2))


def magic_packet(mac: str) -> bytes:
    normalized = normalize_mac(mac)
    if normalized is None:
        raise ValueError("not a MAC address")
    raw = bytes.fromhex(normalized.replace(":", ""))
    return b"\xff" * 6 + raw * 16


def broadcast_targets(host: str | None) -> list[str]:
    """Where a magic packet goes: the limited broadcast, plus the device's /24
    directed broadcast. On Windows the limited broadcast leaves through one
    adapter only, which with Hyper-V, WSL or a VPN may be the wrong one."""
    targets = ["255.255.255.255"]
    if host:
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return targets
        if isinstance(address, ipaddress.IPv4Address) and address.is_private:
            network = ipaddress.ip_network(f"{address}/24", strict=False)
            targets.append(str(network.broadcast_address))
    return targets


def wake_on_lan(mac: str, host: str | None = None, *, port: int = 9, repeat: int = 3, gap_s: float = 0.1) -> int:
    """Sends the magic packet a few times to each target; returns how many sends worked."""
    packet = magic_packet(mac)
    sent = 0
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for attempt in range(repeat):
            for target in broadcast_targets(host):
                try:
                    sock.sendto(packet, (target, port))
                    sent += 1
                except OSError:
                    continue
            if attempt < repeat - 1:
                time.sleep(gap_s)
    return sent


def is_ip_address(text: str) -> bool:
    try:
        ipaddress.ip_address(text.strip())
    except ValueError:
        return False
    return True
