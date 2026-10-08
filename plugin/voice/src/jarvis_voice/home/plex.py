"""Plex Media Server: find a movie, a show or its next episode by title, and answer with a link that opens it.

Setup signs in the way a TV app does (plex.tv's PIN flow): plex.tv opens in the browser, the user approves
there, and Plex hands the helper a sign-in token. The password is never typed into Jarvis. The token goes
into the credential store and, after that, only ever into an ``X-Plex-Token`` header: never a URL, a log
line, an exception message or an outcome text. It is the older kind of token (valid until revoked); Plex's
newer 7-day JWT tokens are a follow-up. What counts as watched, and so which episode is next, follows the
account whose token is saved.

``find`` searches the user's own server (``/hubs/search``), works out which episode a show is up to, and ends
its answer with a ``plex://`` link. The model gives that link, unchanged, to the Apple TV's ``launch_app``
command, which hands it to the Plex app (see plex_find.delivery, the one place that decides how an item reaches
the Apple TV). Nothing here talks to the Apple TV or plays anything.

The server is reached through the addresses plex.tv lists for it, nearest first: the home network, then the
internet, then Plex's relay. An address is trusted only after its ``/identity`` (asked without the token)
names this server's machine identifier, so the token never goes to another machine that inherited an
address. (That identifier is public, so on plain http it keeps out a stranger's machine that inherited an
address, not someone on the home network who copies it.) https is tried before plain http, which is only
used on the home network, for a private address, and then the token crosses the home network unencrypted,
as it does for Plex's own apps. Certificates are always checked; ``plex_roots.pem`` adds roots for the one
case where the PC's own store lacks the one Plex's ``*.plex.direct`` certificates chain to.

The account's token is for the account's own server only: a server shared with the account is reached with
the token plex.tv gives for that server, and with none if plex.tv gives none.

Nothing polls the server: it is asked only when Jarvis is asked.
"""

from __future__ import annotations

import concurrent.futures
import functools
import ipaddress
import logging
import platform
import socket
import ssl
import time
import uuid
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import quote, urlencode, urlsplit

from .. import __version__
from ..logs import secret_filter
from . import net
from .base import Driver, DriverContext, Prompter
from .model import Code, CommandSpec, DeviceRecord, HomeConfig, Outcome, Value, unique_id
from .plex_find import (
    DEFAULT_LINK_KIND,
    MIN_TITLE,
    Choice,
    Item,
    Query,
    choose,
    delivery,
    detail_of,
    episode_at,
    first_episode,
    hits_from_hubs,
    items_from,
    label,
    latest_episode,
    next_unwatched,
    parse_query,
    seasons_of,
    stopped_at,
    tidy,
    unique,
)
from .store import StoreError

log = logging.getLogger(__name__)

DRIVER = "plex"
PLEX_TV = "https://plex.tv"  # tests point this at a fake
AUTH_URL = "https://app.plex.tv/auth"
PRODUCT = "Jarvis"  # what the Plex account lists under its authorized devices
PLEX_DIRECT = ".plex.direct"

# Timings. A driver call must end within the service's limit, so each request's timeout is also
# clamped to what is left of the call's budget.
CALL_BUDGET_S = 17.0
CALL_MARGIN_S = 1.5  # kept free below ctx.call_timeout
REQUEST_TIMEOUT_S = 5.0
PLEX_TV_TIMEOUT_S = 8.0
MIN_REQUEST_S = 0.2
REFRESH_MIN_S = 4.0  # asking plex.tv for new addresses needs at least this much of the call left
# How long each kind of address gets to answer /identity: close by is quick, far away is not.
PROBE_S = {"local": 1.5, "remote": 4.0, "relay": 6.0}
MAX_ROUTES = {"local": 8, "remote": 4, "relay": 2}  # per kind: many home adapters must not push out the rest
HITS = 10  # search results asked for
PAGE = 1000  # episodes asked for at a time
MAX_PAGES = 20
PIN_WAIT_S = 600.0  # module attributes, so tests can shorten them
PIN_POLL_S = 2.0  # Plex asks native apps to poll about once a second
PIN_PROGRESS_S = 30.0
PIN_TRIES = 3  # new codes offered when one expires
PIN_FAILS = 3  # polls in a row that may fail before setup gives up
LIST_MAX = 20
NAME_MAX = 60  # a server's or a library's name, as shown

WHERE = {
    "local": "on your home network",
    "remote": "over the internet",
    "relay": "through Plex's relay",
}


# --------------------------------------------------------------------------- errors


class PlexError(Exception):
    """A Plex call that did not succeed. The message is the kind and the status, never a URL, address, header or body.

    ``kind``: auth (HTTP 401: the sign-in is refused or revoked), denied (HTTP 403, or the account is fine but
    the server refuses it), unverified (the server refused the token and plex.tv could not say whether the
    sign-in is still good), unreachable / timeout / tls (no answer), http (another status), bad_reply, budget
    (no time left in this call), not_found (404), expired (a sign-in code), limited (plex.tv asks to slow down).
    """

    def __init__(self, kind: str, status: int | None = None) -> None:
        super().__init__(f"{kind} (HTTP {status})" if status else kind)
        self.kind = kind
        self.status = status


def _failure_of(exc: net.HttpError) -> PlexError:
    # Not str(exc): it names the host and port.
    return PlexError(exc.kind if exc.kind in ("timeout", "tls") else "unreachable")


# --------------------------------------------------------------------------- requests


def _ascii(text: str) -> str:
    """Header values must be ASCII: http.client encodes them as latin-1, and a Hebrew computer name would fail."""
    return text.encode("ascii", "replace").decode("ascii")


def _headers(client_id: str, token: str | None = None) -> dict[str, str]:
    """What Plex asks every client to send. The token, when there is one, goes in its header and nowhere else."""
    headers = {
        "Accept": "application/json",
        "X-Plex-Client-Identifier": client_id,
        "X-Plex-Product": PRODUCT,
        "X-Plex-Version": __version__,
        "X-Plex-Platform": _ascii(platform.system() or "Windows"),
        "X-Plex-Platform-Version": _ascii(platform.version()),
        "X-Plex-Device": _ascii(platform.system() or "Windows"),
        "X-Plex-Device-Name": _ascii(f"{PRODUCT} ({socket.gethostname()})"),
    }
    if token:
        headers["X-Plex-Token"] = token
    return headers


@functools.cache
def _roots_pem() -> str:
    return Path(__file__).with_name("plex_roots.pem").read_text(encoding="ascii")


def _roots_context() -> ssl.SSLContext:
    """The PC's own roots plus the bundled ones: added to, never replacing, so every check stays on."""
    context = ssl.create_default_context()
    context.load_verify_locations(cadata=_roots_pem())
    return context


class Transport:
    """Sends requests and turns a failure into a PlexError.

    A ``*.plex.direct`` server whose certificate chain this PC cannot complete (Python on Windows does not fetch
    a missing issuer the way the browser does) is tried again with the bundled roots, and remembered for next time.
    """

    def __init__(self) -> None:
        self._bundled: set[str] = set()

    def send(
        self, method: str, url: str, *, headers: dict[str, str], timeout: float, body: bytes | None = None
    ) -> net.HttpResponse:
        host = urlsplit(url).hostname or ""
        try:
            return self._once(method, url, headers, timeout, body, host in self._bundled)
        except net.HttpError as exc:
            if not (exc.issuer_unknown and host.endswith(PLEX_DIRECT) and host not in self._bundled):
                raise _failure_of(exc) from None
        try:
            response = self._once(method, url, headers, timeout, body, True)
        except net.HttpError as exc:
            raise _failure_of(exc) from None
        self._bundled.add(host)
        return response

    @staticmethod
    def _once(
        method: str, url: str, headers: dict[str, str], timeout: float, body: bytes | None, bundled: bool
    ) -> net.HttpResponse:
        context = _roots_context() if bundled else None
        return net.request(method, url, headers=headers, body=body, timeout=timeout, ssl_context=context)


def _json(response: net.HttpResponse, *, status: tuple[int, ...] = (200,)) -> Any:
    """The body as JSON when the status is one of ``status``; otherwise the PlexError for that status."""
    code = response.status
    if code == 401:
        raise PlexError("auth", code)
    if code == 403:
        raise PlexError("denied", code)  # not "auth": a 403 is no proof that the sign-in itself is gone
    if code == 404:
        raise PlexError("not_found", 404)
    if code == 429:
        raise PlexError("limited", 429)
    if code not in status:
        raise PlexError("http", code)
    try:
        return response.json()
    except (ValueError, RecursionError):  # not JSON, or nested too deep to read
        raise PlexError("bad_reply") from None


def _container(data: Any) -> dict[str, Any]:
    box = data.get("MediaContainer") if isinstance(data, dict) else None
    if not isinstance(box, dict):
        raise PlexError("bad_reply")
    return box


# --------------------------------------------------------------------------- plex.tv


@dataclass(frozen=True, slots=True)
class Route:
    """One address a server answers at: ``where`` is local (home network), remote (the internet) or relay."""

    uri: str
    where: str

    def to_json(self) -> dict[str, str]:
        return {"uri": self.uri, "where": self.where}

    @classmethod
    def from_json(cls, data: Any) -> Route | None:
        """A saved route, or None when it is not one this driver would use (devices.json can be edited by hand)."""
        if not isinstance(data, dict) or data.get("where") not in PROBE_S:
            return None
        uri = data.get("uri")
        return cls(uri, data["where"]) if isinstance(uri, str) and _usable_uri(uri, data["where"]) else None


def _usable_uri(uri: str, where: str) -> bool:
    """https anywhere, plain http only to a private address on the home network: the token travels in clear on http."""
    try:
        parts = urlsplit(uri)
        host = parts.hostname or ""
        parts.port  # noqa: B018 - raises ValueError for a bad port
    except ValueError:
        return False
    if parts.scheme == "https":
        return bool(host)
    return parts.scheme == "http" and where == "local" and _private(host)


def _private(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return False


@dataclass(frozen=True, slots=True)
class Server:
    """A Plex Media Server on the account, as plex.tv lists it."""

    id: str  # the machine identifier
    name: str
    owned: bool
    access_token: str = field(repr=False)  # for this server; empty when plex.tv gave none
    routes: tuple[Route, ...]


def routes_of(connections: Any, owned: bool) -> tuple[Route, ...]:
    """A server's addresses in the order to try them: home network, internet, relay; https before http in each."""
    found: list[tuple[int, int, bool, Route]] = []
    for c in connections if isinstance(connections, list) else []:
        if not isinstance(c, dict):
            continue
        where = "relay" if c.get("relay") else "local" if c.get("local") else "remote"
        if where == "local" and not owned:
            continue  # on a stranger's home network
        ipv6 = c.get("IPv6") is True
        uris = []
        if isinstance(c.get("uri"), str) and str(c["uri"]).startswith("https://"):
            uris.append(str(c["uri"]))
        address, port = c.get("address"), c.get("port")
        if where == "local" and isinstance(address, str) and isinstance(port, int) and _private(address):
            uris.append(f"http://{address}:{port}")
        for uri in uris:
            if _usable_uri(uri, where):
                found.append((list(PROBE_S).index(where), 0 if uri.startswith("https") else 1, ipv6, Route(uri, where)))
    # IPv6 only when nothing else is listed.
    wanted = [f for f in found if not f[2]] or found
    ordered = sorted(wanted, key=lambda f: f[:2])
    kept: list[Route] = []
    for route in dict.fromkeys(f[3] for f in ordered):
        if sum(r.where == route.where for r in kept) < MAX_ROUTES[route.where]:
            kept.append(route)
    return tuple(kept)


@dataclass(frozen=True, slots=True)
class Pin:
    """A sign-in code from plex.tv: the user approves it in the browser, and polling it then yields the token."""

    id: int
    code: str
    expires_in: float  # seconds


def auth_url(pin: Pin, client_id: str) -> str:
    """Where the user approves a code. Plex wants the parameters in the fragment, after ``#?``."""
    query = urlencode({"clientID": client_id, "code": pin.code, "context[device][product]": PRODUCT})
    return f"{AUTH_URL}#?{query}"


def _token_like(value: Any) -> bool:
    """Something an HTTP header can carry: Plex's tokens are letters, digits and a few symbols."""
    return (
        isinstance(value, str)
        and 8 <= len(value) <= 4096
        and all(c.isascii() and (c.isalnum() or c in "-_.~+/=") for c in value)
    )


class PlexTv:
    """The few plex.tv calls Jarvis needs: sign in with a code, check a token, list the account's servers."""

    def __init__(self, client_id: str, transport: Transport | None = None, *, deadline: float | None = None) -> None:
        self.client_id = client_id
        self.transport = transport or Transport()
        self.deadline = deadline

    def _call(self, method: str, path: str, *, token: str | None = None, ok: tuple[int, ...] = (200,)) -> Any:
        timeout = PLEX_TV_TIMEOUT_S
        if self.deadline is not None:
            timeout = min(timeout, self.deadline - time.monotonic())
            if timeout < MIN_REQUEST_S:
                raise PlexError("budget")
        response = self.transport.send(method, PLEX_TV + path, headers=_headers(self.client_id, token), timeout=timeout)
        return _json(response, status=ok)

    def create_pin(self) -> Pin:
        data = self._call("POST", "/api/v2/pins?strong=true", ok=(200, 201))
        if not isinstance(data, dict):
            raise PlexError("bad_reply")
        pin_id, code, lifetime = data.get("id"), data.get("code"), data.get("expiresIn")
        if not isinstance(pin_id, int) or isinstance(pin_id, bool) or not isinstance(code, str):
            raise PlexError("bad_reply")
        if not code.isalnum() or not code.isascii():
            raise PlexError("bad_reply")  # it goes into a URL
        return Pin(pin_id, code, float(lifetime) if isinstance(lifetime, int | float) and lifetime > 0 else PIN_WAIT_S)

    def poll_pin(self, pin: Pin) -> str | None:
        """The token once the user has approved the code, None until then. An expired code is PlexError("expired")."""
        try:
            data = self._call("GET", f"/api/v2/pins/{pin.id}?{urlencode({'code': pin.code})}")
        except PlexError as err:
            if err.kind == "not_found":
                raise PlexError("expired") from None
            raise
        token = data.get("authToken") if isinstance(data, dict) else None
        if token is None or token == "":
            return None
        if not _token_like(token):
            raise PlexError("bad_reply")
        secret_filter.add(token)
        return str(token)

    def check(self, token: str) -> bool:
        """Whether plex.tv accepts the token. False only for a refusal; other trouble proves nothing and is raised."""
        try:
            self._call("GET", "/api/v2/user", token=token)
        except PlexError as err:
            if err.kind == "auth":
                return False
            raise
        return True

    def servers(self, token: str) -> list[Server]:
        data = self._call("GET", "/api/v2/resources?includeHttps=1&includeRelay=1&includeIPv6=1", token=token)
        if not isinstance(data, list):
            raise PlexError("bad_reply")
        found = []
        for r in data:
            if not isinstance(r, dict) or "server" not in str(r.get("provides") or "").split(","):
                continue
            ident, name = r.get("clientIdentifier"), r.get("name")
            if not isinstance(ident, str) or not isinstance(name, str) or not tidy(name, NAME_MAX):
                continue
            owned = r.get("owned") is True
            access = r.get("accessToken")
            if _token_like(access):
                secret_filter.add(access)
            found.append(
                Server(
                    ident,
                    tidy(name, NAME_MAX),
                    owned,
                    str(access) if _token_like(access) else "",
                    routes_of(r.get("connections"), owned),
                )
            )
        return found


# --------------------------------------------------------------------------- the server


def _remaining(deadline: float | None) -> float:
    return float("inf") if deadline is None else deadline - time.monotonic()


def _probe(
    route: Route, server_id: str, client_id: str, transport: Transport, deadline: float | None
) -> tuple[str | None, str]:
    """(the server's version, "") when the address answers /identity as this server, else (None, why).

    Asked without the token: /identity needs none, and an address nobody has vouched for gets none. Nor does it
    learn this PC's name or system: the machine that answers may be a stranger's.
    """
    timeout = min(PROBE_S[route.where], _remaining(deadline))
    if timeout < MIN_REQUEST_S:
        return None, "budget"
    headers = {"Accept": "application/json", "X-Plex-Client-Identifier": client_id}
    try:
        box = _container(_json(transport.send("GET", route.uri + "/identity", headers=headers, timeout=timeout)))
    except PlexError as err:
        return None, err.kind
    if box.get("machineIdentifier") != server_id:
        return None, "other"  # an address that now belongs to a different machine
    version = box.get("version")
    return (tidy(version, 40) if isinstance(version, str) else ""), ""


def select_route(
    routes: tuple[Route, ...],
    *,
    server_id: str,
    client_id: str,
    transport: Transport,
    last: Route | None = None,
    deadline: float | None = None,
) -> tuple[Route, str]:
    """The first address that answers as this server, and its version: the one that worked last time, then
    the home network, the internet and Plex's relay in turn (the addresses of a stage are asked together).
    Raises PlexError with why the nearest of them failed."""
    stages = [[last]] if last is not None else []
    stages += [[r for r in routes if r.where == where and r != last] for where in PROBE_S]
    reasons: list[str] = []

    def ask(route: Route) -> tuple[str | None, str]:
        return _probe(route, server_id, client_id, transport, deadline)

    for stage in filter(None, stages):
        if len(stage) == 1:
            results = [ask(stage[0])]
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(stage)) as pool:
                results = list(pool.map(ask, stage))
        for route, (version, why) in zip(stage, results, strict=True):
            if version is not None:
                log.debug("plex: server %s answered %s", version or "(no version)", route.where)
                return route, version
            reasons.append(why)
    for kind in ("budget", "tls", "timeout"):
        if kind in reasons:
            raise PlexError(kind)
    raise PlexError("unreachable")


class PlexServer:
    """One server at one address, with the token for it. Every call is within the budget."""

    def __init__(
        self, route: Route, client_id: str, token: str, transport: Transport, deadline: float, version: str = ""
    ) -> None:
        self.route = route
        self.version = version
        self._client_id = client_id
        self._token = token
        self._transport = transport
        self._deadline = deadline

    def __repr__(self) -> str:
        return f"PlexServer({self.route.where})"

    def get(
        self, path: str, params: dict[str, Any] | None = None, *, page: tuple[int, int] | None = None
    ) -> dict[str, Any]:
        """A ``MediaContainer`` from ``path``. ``page`` is (start, size) of the items wanted."""
        timeout = min(REQUEST_TIMEOUT_S, _remaining(self._deadline))
        if timeout < MIN_REQUEST_S:
            raise PlexError("budget")
        url = self.route.uri + path + (f"?{urlencode(params, quote_via=quote)}" if params else "")
        headers = _headers(self._client_id, self._token)
        if page is not None:
            # Both headers or neither: Plex ignores a lone one.
            headers["X-Plex-Container-Start"], headers["X-Plex-Container-Size"] = str(page[0]), str(page[1])
        return _container(_json(self._transport.send("GET", url, headers=headers, timeout=timeout)))


# --------------------------------------------------------------------------- one call


def _text_of(err: PlexError) -> tuple[Code, str]:
    """(code, spoken text) for a failure."""
    kind = err.kind
    if kind == "auth":
        return "auth", "Plex no longer accepts Jarvis's sign-in. Connect Plex again in home setup."
    if kind == "denied":
        return (
            "auth",
            "The Plex server doesn't accept the account Jarvis is signed in with. Check that the account still has "
            "access to the server, or connect Plex again in home setup.",
        )
    if kind == "unverified":
        return (
            "failed",
            "The Plex server refused Jarvis's sign-in, and plex.tv could not be reached to check it. "
            "Try again in a minute.",
        )
    if kind == "tls":
        return (
            "unreachable",
            "Jarvis reached the Plex server but this PC doesn't trust its secure certificate. At home, set Secure "
            "connections to Preferred in Plex's server settings under Network, and Jarvis will use the plain "
            "address instead. Away from home that doesn't help.",
        )
    if kind in ("unreachable", "other"):
        return "unreachable", "The Plex server isn't answering. Check that it is on and that this PC can reach it."
    if kind in ("timeout", "budget"):
        return "timeout", "Plex didn't answer in time."
    if kind == "not_found":
        return "failed", "Plex no longer has that. Ask again."
    if kind == "limited":
        return "busy", "Plex asked Jarvis to slow down. Try again in a minute."
    if kind == "http":
        return "failed", f"Plex answered with an error (HTTP {err.status})."
    return "failed", "Plex gave an answer Jarvis could not read."


class _Session:
    """One call to a server: the saved sign-in and addresses, and the server once reached, within one budget."""

    def __init__(self, driver: PlexDriver, device: DeviceRecord, secret: dict[str, Any], deadline: float) -> None:
        settings = device.settings
        self.driver = driver
        self.device = device
        self.token: str = secret["token"]
        self.server_token: str = secret["server_token"] if _token_like(secret.get("server_token")) else ""
        self.owned: bool = settings.get("owned") is True
        self.deadline = deadline
        self.client_id: str = settings["client_id"]
        self.server_id: str = settings["server_id"]
        saved = settings.get("connections")
        self.routes = tuple(r for r in (Route.from_json(c) for c in (saved if isinstance(saved, list) else [])) if r)
        self.server: PlexServer | None = None
        self._refreshed = False

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    # -- reaching the server

    def connected(self) -> PlexServer:
        if self.server is not None:
            return self.server
        last = Route.from_json(self.device.settings.get("last"))
        try:
            route, version = self._select(last)
        except PlexError as err:
            if err.kind == "budget" or not self._refresh():
                raise
            route, version = self._select(last)
        token = self._server_token()
        if not token and self._refresh():
            token = self._server_token()  # plex.tv has one for it now
        if not token:
            raise PlexError("denied")
        self.server = PlexServer(route, self.client_id, token, self.driver.transport, self.deadline, version)
        if route != last:
            self.driver._remember(self.device, {"last": route.to_json()})
        return self.server

    def _server_token(self) -> str:
        """The token for the server: its own, or the account's for a server the account owns. A server shared
        with the account and given no token of its own gets none: the account's is not for someone else's machine."""
        return self.server_token or (self.token if self.owned else "")

    def _select(self, last: Route | None) -> tuple[Route, str]:
        return select_route(
            self.routes,
            server_id=self.server_id,
            client_id=self.client_id,
            transport=self.driver.transport,
            last=last,
            deadline=self.deadline,
        )

    def get(
        self, path: str, params: dict[str, Any] | None = None, *, page: tuple[int, int] | None = None
    ) -> dict[str, Any]:
        """A container from the server. A refused token gets one chance: plex.tv may have given the server a new one."""
        while True:
            server = self.connected()
            try:
                return server.get(path, params, page=page)
            except PlexError as err:
                if err.kind != "auth":
                    raise
                if not self._refresh():
                    raise self._auth_failure() from None
                self.server = None  # reached again with the new token

    def _refresh(self) -> bool:
        """New addresses and a new server token from plex.tv, once per call; whether either changed."""
        if self._refreshed or self.remaining() < REFRESH_MIN_S:
            return False
        self._refreshed = True
        try:
            servers = PlexTv(self.client_id, self.driver.transport, deadline=self.deadline).servers(self.token)
        except PlexError as err:
            if err.kind == "auth":
                self._revoke()
            return False
        found = next((s for s in servers if s.id == self.server_id), None)
        if found is None:
            raise PlexError("denied")
        changed = False
        if found.owned != self.owned:
            self.owned, changed = found.owned, True
            self.driver._remember(self.device, {"owned": found.owned})
        if found.routes and found.routes != self.routes:
            self.routes, changed = found.routes, True
            self.driver._remember(self.device, {"connections": [r.to_json() for r in found.routes]})
        if found.access_token and found.access_token != self.server_token:
            self.server_token, changed = found.access_token, True
            self.driver._save_secret(self.device, {"token": self.token, "server_token": found.access_token})
        return changed

    def _auth_failure(self) -> PlexError:
        """The server refused the token twice: ask plex.tv whether the account's sign-in itself is still good."""
        try:
            valid = PlexTv(self.client_id, self.driver.transport, deadline=self.deadline).check(self.token)
        except PlexError:
            return PlexError("unverified")  # nothing proved the sign-in is bad: keep it
        if not valid:
            self._revoke()
        return PlexError("denied")

    def _revoke(self) -> NoReturn:
        self.driver._signed_out(self.device)
        raise PlexError("auth")

    # -- what Plex knows

    def details(self, key: str, **params: Any) -> dict[str, Any]:
        return self.get(f"/library/metadata/{key}", params or None)

    def episodes(self, show_key: str) -> list[Item]:
        """All the episodes of a show, a page at a time."""
        found: list[Item] = []
        taken = 0  # entries the server has sent, which is where the next page starts
        for _ in range(MAX_PAGES):
            box = self.get(f"/library/metadata/{show_key}/allLeaves", page=(taken, PAGE))
            sent = box.get("Metadata")
            found += items_from(box)
            taken += len(sent) if isinstance(sent, list) else 0
            total = box.get("totalSize")
            if not sent or not isinstance(total, int) or taken >= total:
                break
        return found


# --------------------------------------------------------------------------- the driver


class PlexDriver(Driver):
    name = DRIVER
    label = "Plex"

    def __init__(self, ctx: DriverContext) -> None:
        super().__init__(ctx)
        self.transport = Transport()

    # ------------------------------------------------------------------ Driver

    def commands(self, device: DeviceRecord) -> list[CommandSpec]:
        hint = (
            "a title, then for a show s2e5, next or latest after it. A Play link in the answer goes to launch_app "
            "on the Apple TV"
        )
        return [CommandSpec("find", "text", hint=hint)]

    def run(self, device: DeviceRecord, command: str, value: Value) -> Outcome:
        if command != "find":
            return Outcome.fail("unsupported", f"The {device.name} can't {command.replace('_', ' ')}.")
        return self._attempt(device, lambda s: self._find(s, str(value or "")))

    def status(self, device: DeviceRecord) -> Outcome:
        return self._attempt(device, self._status)

    def describe(self) -> str | None:
        try:
            config = self.ctx.store.load()
            missing = [
                d.name
                for d in config.devices
                if d.driver == self.name
                and (d.settings.get("needs_signin") or not (self.ctx.store.secret(d.secret_key) or {}).get("token"))
            ]
        except StoreError:
            return "Plex: the saved sign-in could not be read; connect Plex again in home setup."
        if missing:
            return f"Plex: no working sign-in for {', '.join(missing)}; connect Plex again in home setup."
        return None

    # ------------------------------------------------------------------ plumbing

    def _open(self, device: DeviceRecord) -> _Session | Outcome:
        settings = device.settings
        if settings.get("needs_signin"):
            return Outcome.fail("auth", _text_of(PlexError("auth"))[1])
        try:
            secret = self.ctx.store.secret(device.secret_key) or {}
        except StoreError:
            return Outcome.fail(
                "needs_setup", "Jarvis could not read the saved Plex sign-in. Connect Plex again in home setup."
            )
        if not _token_like(secret.get("token")):
            return Outcome.fail("needs_setup", "Plex isn't connected. Connect Plex in home setup.")
        if not all(isinstance(settings.get(k), str) and settings[k] for k in ("server_id", "client_id")):
            return Outcome.fail(
                "needs_setup", "The saved Plex details are incomplete. Connect Plex again in home setup."
            )
        budget = max(1.0, min(CALL_BUDGET_S, self.ctx.call_timeout - CALL_MARGIN_S))
        return _Session(self, device, secret, time.monotonic() + budget)

    def _attempt(self, device: DeviceRecord, work: Callable[[_Session], Outcome]) -> Outcome:
        session = self._open(device)
        if isinstance(session, Outcome):
            return session
        try:
            return work(session)
        except PlexError as err:
            log.debug("plex %s: %s", device.id, err)
            return Outcome.fail(*_text_of(err))

    def _remember(self, device: DeviceRecord, changes: dict[str, Any]) -> None:
        """Saves what a call learned (the address that worked, new addresses) in the device's record."""
        if all(device.settings.get(k) == v for k, v in changes.items()):
            return
        device.settings.update(changes)

        def mutate(config: HomeConfig) -> None:
            saved = config.device(device.id)
            if saved is not None:
                saved.settings.update(changes)

        try:
            self.ctx.store.update(mutate)
        except (StoreError, OSError) as exc:
            log.warning("could not save what Plex reported for %s: %s", device.id, type(exc).__name__)

    def _save_secret(self, device: DeviceRecord, secret: dict[str, Any]) -> None:
        try:
            self.ctx.store.set_secret(device.secret_key, secret)
        except (StoreError, OSError) as exc:
            log.warning("could not save the new Plex server token for %s: %s", device.id, type(exc).__name__)

    def _signed_out(self, device: DeviceRecord) -> None:
        """plex.tv refused the token: it is dropped, and no Plex call is tried until the user connects Plex again."""
        log.info("plex %s: plex.tv no longer accepts the saved sign-in", device.id)
        self._remember(device, {"needs_signin": True})
        try:
            self.ctx.store.set_secret(device.secret_key, None)
        except (StoreError, OSError) as exc:
            log.warning("could not remove the refused Plex sign-in for %s: %s", device.id, type(exc).__name__)

    # ------------------------------------------------------------------ status

    def _status(self, s: _Session) -> Outcome:
        server = s.connected()
        names = [
            library
            for d in s.get("/library/sections").get("Directory") or []
            if isinstance(d, dict) and isinstance(d.get("title"), str) and (library := tidy(d["title"], NAME_MAX))
        ]
        name = s.device.settings.get("server_name") or s.device.name
        shown = ", ".join(names[:LIST_MAX]) + (f", and {len(names) - LIST_MAX} more" if len(names) > LIST_MAX else "")
        libraries = f" Libraries: {shown}." if names else " It has no libraries yet."
        return Outcome.done(f'Plex server "{name}" is reachable {WHERE[server.route.where]}.{libraries}')

    # ------------------------------------------------------------------ find

    def _found(self, s: _Session, head: str, item: Item) -> Outcome:
        """The answer for an item to play: ``head`` says what was found, and the Play link comes last."""
        kind = s.device.settings.get("link")
        try:
            link = delivery(item, s.server_id, kind if isinstance(kind, str) else DEFAULT_LINK_KIND)
        except ValueError:
            raise PlexError("bad_reply") from None
        stopped = stopped_at(item)
        text = head + (f" (you stopped at {stopped})" if stopped else "") + "."
        return Outcome.done(" ".join(part for part in (text, link.note) if part) + f" Play link: {link.link}")

    @staticmethod
    def _choice(s: _Session, query: Query) -> Choice:
        return choose(hits_from_hubs(s.get("/hubs/search", {"query": query.title, "limit": HITS})), query)

    @staticmethod
    def _by_title(s: _Session, query: Query) -> Choice:
        """The libraries' own title filter, section by section: for a search that found nothing (a title in a
        script the server's search handles badly, say) that a title filter may still find."""
        found: list[Item] = []
        for section in s.get("/library/sections").get("Directory") or []:
            if not isinstance(section, dict):
                continue
            kind, key = {"movie": 1, "show": 2}.get(str(section.get("type"))), section.get("key")
            if kind is not None and isinstance(key, str) and key.isdigit():
                box = s.get(f"/library/sections/{key}/all", {"type": kind, "title": query.title}, page=(0, HITS))
                found += items_from(box)
        return choose(unique(found), query)

    def _find(self, s: _Session, value: str) -> Outcome:
        query, spoken = parse_query(value, spoken=False), parse_query(value)
        if min(len(query.title), len(spoken.title)) < MIN_TITLE:
            return Outcome.fail("bad_value", "Which movie or show? Say its title.")
        choice = self._choice(s, query)
        if choice.item is None and not choice.close and spoken.title != query.title:
            # "play Please Like Me" asked as it came; a title that starts like a request ("Please Like Me") was
            # tried first, and only a miss makes the front of it a request.
            query = spoken
            choice = self._choice(s, query)
        if choice.item is None and not choice.close:
            choice = self._by_title(s, query)
        if choice.item is None:
            if choice.close:
                names = ", ".join(label(i) for i in choice.close)
                return Outcome.fail("ambiguous", f'"{query.title}" could be: {names}. Which one?')
            return Outcome.fail(
                "failed",
                f'Plex has nothing matching "{query.title}". Check the spelling, or it may not be on this server.',
            )
        if choice.item.kind == "show":
            return self._show(s, query, choice.item)
        # A movie or an episode: its details say where the user stopped.
        item = detail_of(s.details(choice.item.key))[0] or choice.item
        return self._found(s, f"Found {label(item)}", item)

    def _show(self, s: _Session, query: Query, show: Item) -> Outcome:
        head = f"Found {label(show)}"
        season, number = query.season, query.episode
        if season is None and query.mode in ("auto", "next"):
            # What Plex lists as continue watching: the episode started, or the one after the last watched.
            deck = detail_of(s.details(show.key, includeOnDeck=1))[1]
            if number is not None:
                season = deck.season if deck is not None and deck.season is not None else 1
            elif deck is not None:
                return self._found(s, f"{head}. Next to watch: {numbered(deck)}", deck)
            elif show.leaves and show.viewed_leaves >= show.leaves:
                return self._all_watched(show, [])
        episodes = s.episodes(show.key)
        if number is not None:
            wanted_season = season if season is not None else 1
            found = episode_at(episodes, wanted_season, number)
            if found is None:
                return self._missing(show, episodes, f"season {wanted_season} episode {number}")
            return self._found(s, f"Found {label(found)}", found)
        if season is not None:
            wanted = next_unwatched(episodes, season)
            if wanted is not None:
                return self._found(
                    s, f'{head}. Next in season {season}: episode {wanted.number}, "{wanted.title}"', wanted
                )
            wanted = first_episode(episodes, season)
            if wanted is None:
                return self._missing(show, episodes, f"season {season}")
            return self._found(
                s, f'{head}. Season {season} is all watched, so its first episode: "{wanted.title}"', wanted
            )
        if query.mode == "latest":
            wanted, what = latest_episode(episodes), "Latest episode"
        elif query.mode == "first":
            wanted, what = first_episode(episodes), "First episode"
        else:
            wanted, what = next_unwatched(episodes), "Next to watch"
        if wanted is not None:
            return self._found(s, f"{head}. {what}: {numbered(wanted)}", wanted)
        if episodes and query.mode not in ("latest", "first"):
            return self._all_watched(show, episodes)
        return Outcome.fail("failed", f'"{show.title}" has no episodes in this library.')

    @staticmethod
    def _all_watched(show: Item, episodes: list[Item]) -> Outcome:
        """Every episode is watched, as far as the ones listed show (none listed: the show's own counts said so)."""
        left = any(e.season == 0 and not e.viewed for e in episodes)  # next skips Specials
        every = "every regular episode" if left else "every episode"
        return Outcome.done(
            f'You have watched {every} of "{show.title}". '
            "Name a season and episode to play one, or say first to start again."
        )

    @staticmethod
    def _missing(show: Item, episodes: list[Item], wanted: str) -> Outcome:
        last = latest_episode(episodes)
        seasons = seasons_of(episodes)
        if last is not None and "episode" in wanted:
            tail = f"; its last is season {last.season} episode {last.number}"
        elif seasons:
            tail = f"; its seasons are {', '.join(str(n) for n in seasons)}"
        else:
            tail = ""
        return Outcome.fail("failed", f'"{show.title}" has no {wanted}{tail}.')


def numbered(episode: Item) -> str:
    """``season 4 episode 12, "Intervention"``."""
    if episode.season is None or episode.number is None:
        return f'"{episode.title}"'
    return f'season {episode.season} episode {episode.number}, "{episode.title}"'


# --------------------------------------------------------------------------- setup

PLEX_STEPS = """\
Jarvis signs in to Plex the way a TV app does: plex.tv opens in your browser, you sign in there if it asks and
approve Jarvis, and Plex gives Jarvis a sign-in. Jarvis never sees your Plex password, and keeps the sign-in
encrypted on this PC.
Use the Plex account you watch with: what you have watched, and so which episode is next, follows that account.
Titles open in the Plex app on your Apple TV, so set the Apple TV up too (Add an Apple TV) and sign in to Plex there."""


def _open_browser(url: str) -> bool:
    try:
        return webbrowser.open(url)
    except webbrowser.Error:
        return False


def _trouble(err: PlexError) -> str:
    if err.kind == "limited":
        return "Plex asked Jarvis to slow down. Wait a minute, then try again."
    if err.kind in ("unreachable", "timeout", "tls", "budget"):
        return "Jarvis could not reach plex.tv. Check the internet connection, then try again."
    return "plex.tv gave an answer Jarvis could not use. Try again in a minute."


def _wait_for_approval(ui: Prompter, tv: PlexTv, pin: Pin) -> str | None:
    """Polls until the user approves the code. Returns the token, or None when the code has expired."""
    started = last_said = time.monotonic()
    limit = min(PIN_WAIT_S, pin.expires_in)
    delay, fails = PIN_POLL_S, 0
    while time.monotonic() - started < limit:
        try:
            token = tv.poll_pin(pin)
        except PlexError as err:
            if err.kind == "expired":
                return None
            if err.kind == "limited":
                delay = min(delay * 2, 30.0)  # asked to slow down
            else:
                fails += 1
                if fails >= PIN_FAILS:
                    raise
        else:
            fails = 0
            if token is not None:
                return token
        if time.monotonic() - last_said >= PIN_PROGRESS_S:
            last_said = time.monotonic()
            ui.say("Still waiting for you to approve in the browser (Ctrl+C stops)...")
        time.sleep(delay)
    return None


def _sign_in(ui: Prompter, tv: PlexTv) -> str | None:
    """Shows the sign-in page, waits for the user to approve it, returns the token. None: nothing to save."""
    for attempt in range(PIN_TRIES):
        try:
            pin = tv.create_pin()
        except PlexError as err:
            ui.say(_trouble(err))
            return None
        url = auth_url(pin, tv.client_id)
        if _open_browser(url):
            ui.say("Jarvis opened plex.tv in your browser. Sign in to Plex there if it asks, then approve Jarvis.")
        else:
            # The address holds the sign-in code, so it is shown only when there is no other way to it.
            ui.say(
                "Jarvis could not open a browser here. Open this address in a browser on any device and approve Jarvis:"
            )
            ui.say(url)
        try:
            token = _wait_for_approval(ui, tv, pin)
        except PlexError as err:
            ui.say(_trouble(err))
            return None
        if token is not None:
            return token
        ui.say("That sign-in link expired.")
        if attempt + 1 == PIN_TRIES or not ui.confirm("Get a new link?", default=True):
            return None
    return None


def _existing(config: HomeConfig, server_id: str) -> DeviceRecord | None:
    return next((d for d in config.devices if d.driver == DRIVER and d.settings.get("server_id") == server_id), None)


def _save(ctx: DriverContext, record: DeviceRecord, secret: dict[str, str]) -> None:
    """The sign-in, then the device; if anything stops in between, the sign-in goes back to what it was."""
    key = record.secret_key
    previous = ctx.store.secret(key)
    saved = False
    try:
        ctx.store.set_secret(key, secret)
        ctx.store.update(lambda config: config.upsert(record))
        saved = True
    finally:
        if not saved:
            try:
                ctx.store.set_secret(key, previous)
            except (StoreError, OSError):
                log.warning("could not undo the half-saved Plex sign-in for %s", record.id)


def wizard(ui: Prompter, ctx: DriverContext) -> None:
    """Signs in to Plex, finds the server, names it and saves it, or signs in again for one saved before."""
    ui.say("Connect Plex")
    ui.say(PLEX_STEPS)
    config = ctx.store.load()
    known = [d for d in config.devices if d.driver == DRIVER]
    if known:
        ui.say(f"Plex is already connected ({', '.join(d.name for d in known)}). Connecting again signs in once more.")
        if not ui.confirm("Sign in again?", default=True):
            return
    # One client id for every sign-in, so the account lists Jarvis once under its authorized devices.
    client_id = next((str(d.settings["client_id"]) for d in known if d.settings.get("client_id")), str(uuid.uuid4()))
    tv = PlexTv(client_id)
    token = _sign_in(ui, tv)
    if token is None:
        ui.say("Nothing was saved.")
        return
    try:
        if not tv.check(token):
            ui.say("plex.tv did not accept the sign-in. Nothing was saved.")
            return
        servers = tv.servers(token)
    except PlexError as err:
        ui.say(_trouble(err))
        ui.say("Nothing was saved.")
        return
    ui.say("Signed in to Plex.")
    if not servers:
        ui.say("This Plex account has no Plex Media Server. Nothing was saved.")
        return
    if len(servers) == 1:
        server = servers[0]
    else:
        names = [s.name + ("" if s.owned else " (shared with you)") for s in servers]
        index = ui.choose("Which Plex server?", names)
        if index is None:
            ui.say("Nothing was saved.")
            return
        server = servers[index]
    if not server.owned and not server.access_token:
        # The account's own token is not for someone else's server, so without one of its own there is no way in.
        ui.say(f"plex.tv gave Jarvis no access to {server.name}, which is shared with you. Nothing was saved.")
        return
    existing = _existing(config, server.id)
    if existing is not None:
        ui.say(f"This server is already set up as {existing.name}; Jarvis will update it.")
    reached: tuple[Route, str] | None = None
    ui.say(f"Looking for {server.name} (a few seconds)...")
    try:
        reached = select_route(
            server.routes,
            server_id=server.id,
            client_id=client_id,
            transport=tv.transport,
            deadline=time.monotonic() + 25.0,
        )
    except PlexError as err:
        log.info("plex setup: the server could not be reached (%s)", err.kind)
    if reached is not None:
        route, version = reached
        ui.say(f"Reached {server.name}{f' (Plex Media Server {version})' if version else ''} {WHERE[route.where]}.")
    else:
        ui.say(
            "Jarvis signed in but cannot reach the server from this PC. Is it on, and is this PC on the same network?"
        )
        if not ui.confirm("Save the sign-in anyway?", default=False):
            ui.say("Nothing was saved.")
            return
    taken = {d.name.lower() for d in config.devices}
    default_name = existing.name if existing else "Plex" if "plex" not in taken else server.name
    name = ui.ask("A name for this server", default_name).strip() or default_name
    settings: dict[str, Any] = {
        **(existing.settings if existing else {}),
        "server_id": server.id,
        "server_name": server.name,
        "owned": server.owned,
        "client_id": client_id,
        "connections": [r.to_json() for r in server.routes],
    }
    settings.pop("needs_signin", None)
    if reached is not None:
        settings["last"] = reached[0].to_json()
    else:
        settings.pop("last", None)
    record = DeviceRecord(
        existing.id if existing else unique_id(config.ids(), DRIVER, name),
        DRIVER,
        name,
        "media_server",
        existing.room if existing else None,
        aliases=list(existing.aliases) if existing else ["Plex"],
        confirm=existing.confirm if existing else None,
        settings=settings,
    )
    secret = {"token": token}
    if server.access_token and server.access_token != token:
        secret["server_token"] = server.access_token
    _save(ctx, record, secret)
    if reached is None:
        ui.say(
            f"Saved {name}. Jarvis can't reach the server from this PC yet; it will try again when you ask for a title."
        )
        return
    ui.say(f"Saved {name}. Jarvis can search it now.")
    if ui.confirm("Try it now? Jarvis will read your library names.", default=True):
        ui.say(PlexDriver(ctx).status(record).text)
