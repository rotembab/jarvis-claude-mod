"""A fake Plex for the home-control tests: plex.tv (sign-in codes, the account, its servers) and a Media Server.

Both are ThreadingHTTPServers on 127.0.0.1 answering with JSON shaped like the real ones. The library holds a
few titles: a show with a half-watched episode On Deck (ratingKey 14779), a show nobody has started, one watched
to the end, one watched to the end but for a Special, a Hebrew show, movies, and a title that sits in two library
sections. ``install`` points the driver
at them and keeps the tests off the real network: an https address reaches the fake only when ``hosts`` names it.
"""

from __future__ import annotations

import json
import re
import threading
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from jarvis_voice.home import net, plex

MACHINE_ID = "43d8ddccab40252739e7f4ecc6d134c679888c13"
OTHER_MACHINE_ID = "9f1c2e0b7a6d4c53b8e1a2d3c4b5a69788776655"
ACCOUNT_TOKEN = "fake-account-token-7Qx2mP9aLd"
SERVER_TOKEN = "fake-server-token-Zk4vB8nRtc"
NEW_SERVER_TOKEN = "fake-server-token-Hw6yU3sXe1"
TOKENS = (ACCOUNT_TOKEN, SERVER_TOKEN, NEW_SERVER_TOKEN)
CLIENT_ID_HEADER = "x-plex-client-identifier"
SHOW = "14779"  # How I Met Your Mother
LOCAL_HOST = "127-0-0-1.0123456789abcdef0123456789abcdef.plex.direct"
REMOTE_HOST = "203-0-113-7.0123456789abcdef0123456789abcdef.plex.direct"
RELAY_HOST = "47-1-2-3.0123456789abcdef0123456789abcdef.plex.direct"
SEASONS = [22, 22, 20, 24, 24, 24, 24, 24, 24]

Reply = tuple[int, Any] | None


@dataclass(slots=True)
class Seen:
    """One request a fake server answered."""

    server: str  # "tv" or "pms"
    method: str
    raw: str  # the request line's target, exactly as sent
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]  # lower-case names

    @property
    def token(self) -> str | None:
        return self.headers.get("x-plex-token")


@dataclass(slots=True)
class Attempt:
    """One request the driver made, as it left net.request (also those that never reach a fake)."""

    url: str
    scheme: str
    host: str
    path: str
    token: bool  # carried an X-Plex-Token
    bundled_roots: bool  # was sent with the extra roots


@dataclass(slots=True)
class Host:
    """What an https name does: reaches the fake's Media Server at ``port`` (None: nothing there)."""

    port: int | None
    issuer_unknown: bool = False  # the PC's own roots cannot check its certificate; the bundled ones can


def _norm(text: str) -> str:
    """Words without case, accents or punctuation, the way the server matches a search."""
    plain = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return " ".join(re.findall(r"[^\W_]+", plain.casefold()))


def _episode(key: int, show: str, show_key: str, season: int, number: int, title: str, **extra: Any) -> dict[str, Any]:
    return {
        "ratingKey": str(key),
        "key": f"/library/metadata/{key}",
        "parentRatingKey": str(int(show_key) + season),
        "grandparentRatingKey": show_key,
        "guid": f"plex://episode/5d9c0{key}",
        "type": "episode",
        "title": title,
        "grandparentKey": f"/library/metadata/{show_key}",
        "parentKey": f"/library/metadata/{int(show_key) + season}",
        "grandparentTitle": show,
        "parentTitle": "Specials" if season == 0 else f"Season {season}",
        "contentRating": "TV-14",
        "summary": "",
        "index": number,
        "parentIndex": season,
        "duration": 1320000,
        "originallyAvailableAt": "2008-10-13",
        "addedAt": 1700000000,
        **extra,
    }


def _season_of(
    key: int, show: dict[str, Any], season: int, count: int, watched: int, *, offset: int = 0
) -> list[dict[str, Any]]:
    """``count`` episodes, the first ``watched`` seen, and the next one stopped at ``offset`` milliseconds."""
    episodes = []
    for number in range(1, count + 1):
        extra: dict[str, Any] = {}
        if number <= watched:
            extra = {"viewCount": 1, "lastViewedAt": 1710000000 + number}
        elif number == watched + 1 and offset:
            extra = {"viewOffset": offset, "lastViewedAt": 1720000000}
        episodes.append(
            _episode(key + number, show["title"], show["ratingKey"], season, number, f"Episode {number}", **extra)
        )
    return episodes


def _show(key: str, title: str, year: int, leaves: int, viewed: int, **extra: Any) -> dict[str, Any]:
    return {
        "ratingKey": key,
        "key": f"/library/metadata/{key}/children",
        "guid": f"plex://show/5d9c0{key}",
        "type": "show",
        "title": title,
        "year": year,
        "contentRating": "TV-14",
        "summary": "",
        "leafCount": leaves,
        "viewedLeafCount": viewed,
        "childCount": 2,
        "addedAt": 1700000000,
        "librarySectionTitle": "TV Shows",
        "librarySectionID": 2,
        **extra,
    }


def _movie(key: str, title: str, year: int, section: str = "Movies", **extra: Any) -> dict[str, Any]:
    return {
        "ratingKey": key,
        "key": f"/library/metadata/{key}",
        "guid": f"plex://movie/5d776{key}",
        "type": "movie",
        "title": title,
        "year": year,
        "contentRating": "PG-13",
        "summary": "",
        "duration": 8160000,
        "addedAt": 1700000000,
        "librarySectionTitle": section,
        "librarySectionID": 3 if section == "4K Movies" else 1,
        **extra,
    }


def build_library() -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """(every show and movie by ratingKey, every show's episodes by the show's ratingKey)."""
    items: dict[str, dict[str, Any]] = {}
    leaves: dict[str, list[dict[str, Any]]] = {}
    himym = _show(SHOW, "How I Met Your Mother", 2005, sum(SEASONS), 48, originalTitle="")
    episodes: list[dict[str, Any]] = []
    for season, count in enumerate(SEASONS, 1):
        # Seasons 1 and 2 watched, season 3 watched up to episode 4 and episode 5 stopped at 12 minutes.
        watched = count if season < 3 else 4 if season == 3 else 0
        episodes += _season_of(14800 + 30 * season, himym, season, count, watched, offset=720000 if season == 3 else 0)
    himym["leafCount"] = len(episodes)
    items[SHOW], leaves[SHOW] = himym, episodes

    severance = _show("7001", "Severance", 2022, 21, 0)
    leaves["7001"] = (
        _season_of(7100, severance, 0, 2, 0)
        + _season_of(7110, severance, 1, 9, 0)
        + _season_of(7130, severance, 2, 10, 0)
    )
    chernobyl = _show("7002", "Chernobyl", 2019, 5, 5)
    leaves["7002"] = _season_of(7200, chernobyl, 1, 5, 5)
    fauda = _show("7003", "פאודה", 2015, 5, 1, originalTitle="Fauda")
    leaves["7003"] = _season_of(7300, fauda, 1, 3, 1) + _season_of(7310, fauda, 2, 2, 0)
    foundation = _show("7004", "Foundation", 2021, 3, 2)
    leaves["7004"] = _season_of(7400, foundation, 0, 1, 0) + _season_of(7410, foundation, 1, 2, 2)
    for show in (severance, chernobyl, fauda, foundation):
        items[show["ratingKey"]] = show

    for movie in (
        _movie("5001", "Inception", 2010, viewCount=1),
        _movie("5002", "The Matrix", 1999, viewOffset=2460000),
        _movie("5101", "Dune", 2021),
        _movie("5102", "Dune", 2021, section="4K Movies"),
        _movie("5103", "Dune", 1984),
        _movie("5201", "Blade Runner", 1982),
        _movie("5202", "Blade Runner 2049", 2017),
        _movie("5401", "Open Season", 2006),
        _movie("5301", "Amélie", 2001, originalTitle="Le Fabuleux Destin d'Amélie Poulain"),
    ):
        items[movie["ratingKey"]] = movie
    return items, leaves


def on_deck(episodes: list[dict[str, Any]]) -> dict[str, Any] | None:
    """What Plex lists as continue watching: the episode started, else the one after the last watched."""
    started = [e for e in episodes if e.get("viewOffset")]
    if started:
        return started[0]
    unseen = [e for e in episodes if not e.get("viewCount") and e["parentIndex"] != 0]
    seen = [e for e in episodes if e.get("viewCount")]
    return unseen[0] if unseen and seen else None


class FakePlex:
    """plex.tv and one Media Server. Tests set the attributes below to make either misbehave."""

    def __init__(self) -> None:
        self.items, self.leaves = build_library()
        self.log: list[Seen] = []
        self.attempts: list[Attempt] = []
        self.problems: list[str] = []
        self.lock = threading.Lock()
        self.released = threading.Event()
        # plex.tv
        self.account_token = ACCOUNT_TOKEN
        self.account_valid = True
        self.user_ok = True  # the account's own record is served; false: 401 for it alone
        self.pin_lifetime = 1800  # seconds a sign-in code lives, as plex.tv says
        self.approve_after = 1  # polls of a sign-in code before it is approved
        self.expire_pins = 0  # how many of the first codes are expired by the time they are polled
        self.rate_limit = 0  # how many polls get "429 Too Many Requests"
        self.tv_down = False  # plex.tv answers 503
        self.resources_status = 200
        self.pins: dict[int, dict[str, Any]] = {}
        self.owned = True
        self.extra_servers: list[dict[str, Any]] = []
        self.server_token = SERVER_TOKEN  # what plex.tv gives for the server
        self.local_only = False  # list the home network address only
        self.connections: list[dict[str, Any]] | None = None  # replaces the default addresses
        # the Media Server
        self.accepted = {SERVER_TOKEN}
        self.machine_id = MACHINE_ID
        self.version = "1.41.3.9314-a0bfb8370"
        self.server_name = "Home Server"
        self.sections = [("1", "movie", "Movies"), ("2", "show", "TV Shows"), ("3", "movie", "4K Movies")]
        self.ondeck = "object"  # how the show details carry it: object, list or absent
        self.garbage: set[str] = set()  # paths (prefixes) that answer with a page of HTML
        self.stall: set[str] = set()  # paths (prefixes) taken in and never answered
        self.status_for: dict[str, int] = {}  # paths (prefixes) that answer with this status
        self.page_cap: int | None = None  # the most episodes one allLeaves answer carries
        self.hub_blind = False  # the search finds nothing, as it might for a title in some scripts
        self.hosts: dict[str, Host] = {}
        self.tv_server = self._serve("tv", self._answer_tv)
        self.pms_server = self._serve("pms", self._answer_pms)
        self.tv_base = f"http://127.0.0.1:{self.tv_server.server_address[1]}"
        self.pms_port = self.pms_server.server_address[1]
        self.hosts[LOCAL_HOST] = Host(self.pms_port)
        self._threads = [
            threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
            for s in (self.tv_server, self.pms_server)
        ]

    # -- running

    def _serve(self, name: str, answer: Callable[[Seen], Reply]) -> ThreadingHTTPServer:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                self.answer()

            def do_POST(self) -> None:
                self.answer()

            def answer(self) -> None:
                parts = urlsplit(self.path)
                seen = Seen(
                    name,
                    self.command,
                    self.path,
                    parts.path,
                    parse_qs(parts.query, keep_blank_values=True),
                    {k.lower(): v for k, v in self.headers.items()},
                )
                length = int(seen.headers.get("content-length") or 0)
                if length:
                    self.rfile.read(length)
                with fake.lock:
                    fake.log.append(seen)
                    for token in TOKENS:
                        if token in self.path:
                            fake.problems.append(f"a token in the address: {parts.path}")
                    if not seen.headers.get(CLIENT_ID_HEADER):
                        fake.problems.append(f"no client identifier on {parts.path}")
                    reply = answer(seen)
                if reply is None:
                    self.close_connection = True
                    return
                if reply[0] == -1:  # taken in and never answered
                    fake.released.wait(10)
                    self.close_connection = True
                    return
                status, body = reply
                payload = (
                    body
                    if isinstance(body, bytes)
                    else body.encode()
                    if isinstance(body, str)
                    else json.dumps(body).encode()
                )
                kind = "application/json" if not isinstance(body, str | bytes) else "text/html"
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        return server

    def start(self) -> None:
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        self.released.set()
        for server in (self.tv_server, self.pms_server):
            server.shutdown()
            server.server_close()

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Points the driver at the fakes. An https name reaches the Media Server only if ``hosts`` lists it, and
        plain http only to 127.0.0.1: nothing in a test reaches another machine."""
        monkeypatch.setattr(plex, "PLEX_TV", self.tv_base)
        real = net.request

        def request(method: str, url: str, **kwargs: Any) -> net.HttpResponse:
            parts = urlsplit(url)
            headers = kwargs.get("headers") or {}
            context = kwargs.get("ssl_context")
            with self.lock:
                self.attempts.append(
                    Attempt(
                        url,
                        parts.scheme,
                        parts.hostname or "",
                        parts.path,
                        "X-Plex-Token" in headers,
                        context is not None,
                    )
                )
            where = f"{parts.hostname}:{parts.port}"
            if parts.scheme == "https":
                host = self.hosts.get(parts.hostname or "")
                if host is None or host.port is None:
                    raise net.HttpError("unreachable", f"{where} is unreachable (gaierror)")
                if host.issuer_unknown and context is None:
                    message = f"{where}: TLS failed (unable to get local issuer certificate)"
                    raise net.HttpError("tls", message, issuer_unknown=True)
                url = parts._replace(scheme="http", netloc=f"127.0.0.1:{host.port}").geturl()
            elif parts.hostname != "127.0.0.1":
                raise net.HttpError("unreachable", f"{where} is unreachable (TimeoutError)")
            return real(method, url, **kwargs)

        monkeypatch.setattr(net, "request", request)

    # -- addresses

    def servers(self) -> list[dict[str, Any]]:
        """plex.tv's list of resources: this server (the way it lists one), a player, and any extra servers."""
        connections = self.connections
        if connections is None:
            connections = [
                {
                    "protocol": "https",
                    "address": "127.0.0.1",
                    "port": self.pms_port,
                    "uri": f"https://{LOCAL_HOST}:{self.pms_port}",
                    "local": True,
                    "relay": False,
                    "IPv4": True,
                    "IPv6": False,
                },
                {
                    "protocol": "https",
                    "address": "203.0.113.7",
                    "port": 32400,
                    "uri": f"https://{REMOTE_HOST}:32400",
                    "local": False,
                    "relay": False,
                    "IPv4": True,
                    "IPv6": False,
                },
                {
                    "protocol": "https",
                    "address": "47.1.2.3",
                    "port": 8443,
                    "uri": f"https://{RELAY_HOST}:8443",
                    "local": False,
                    "relay": True,
                    "IPv4": True,
                    "IPv6": False,
                },
            ]
            if self.local_only:
                connections = connections[:1]
        server = {
            "name": self.server_name,
            "product": "Plex Media Server",
            "productVersion": "1.41.3.9314-a0bfb8370",
            "platform": "Linux",
            "clientIdentifier": MACHINE_ID,
            "provides": "server",
            "owned": self.owned,
            "accessToken": self.server_token,
            "publicAddress": "203.0.113.7",
            "httpsRequired": False,
            "connections": connections,
        }
        player = {
            "name": "Living room",
            "product": "Plex for Apple TV",
            "clientIdentifier": "6f0e5c3d-1111-4222-8333-444455556666",
            "provides": "client,player,pubsub-player",
            "owned": True,
            "connections": [],
        }
        return [server, player, *self.extra_servers]

    # -- plex.tv

    def _answer_tv(self, seen: Seen) -> Reply:
        if self.tv_down:
            return 503, "<html>Service Unavailable</html>"
        if seen.method == "POST" and seen.path == "/api/v2/pins":
            if seen.query.get("strong") != ["true"]:
                self.problems.append("a code asked for without strong=true")
            ident = 1000000 + len(self.pins) + 1
            self.pins[ident] = {
                "id": ident,
                "code": f"q8l2xk5ptf9yd3mwvc6bghn{ident % 100:02d}",
                "polls": 0,
                "expired": len(self.pins) < self.expire_pins,
            }
            return 201, self._pin_json(self.pins[ident])
        pin = re.fullmatch(r"/api/v2/pins/(\d+)", seen.path)
        if seen.method == "GET" and pin:
            found = self.pins.get(int(pin[1]))
            if found is None or found["expired"] or seen.query.get("code") != [found["code"]]:
                return 404, {"errors": [{"code": 1020, "message": "Code not found or expired", "status": 404}]}
            if self.rate_limit > 0:
                self.rate_limit -= 1
                return 429, {"errors": [{"code": 1003, "message": "Too many requests", "status": 429}]}
            found["polls"] += 1
            return 200, self._pin_json(found)
        if seen.token != self.account_token or not self.account_valid:
            return 401, {"errors": [{"code": 1001, "message": "User could not be authenticated", "status": 401}]}
        if seen.path == "/api/v2/user":
            if not self.user_ok:
                return 401, {"errors": [{"code": 1001, "message": "User could not be authenticated", "status": 401}]}
            return 200, {
                "id": 1234,
                "username": "viewer",
                "email": "viewer@example.com",
                "authToken": self.account_token,
            }
        if seen.path == "/api/v2/resources":
            if self.resources_status != 200:
                return self.resources_status, {"errors": [{"code": 1000, "message": "error"}]}
            if not {"includeHttps", "includeRelay"} <= set(seen.query):
                self.problems.append("resources asked for without https and relay addresses")
            return 200, self.servers()
        return 404, {"errors": [{"code": 404, "message": "not found"}]}

    def _pin_json(self, pin: dict[str, Any]) -> dict[str, Any]:
        approved = pin["polls"] >= self.approve_after
        return {
            "id": pin["id"],
            "code": pin["code"],
            "product": "Jarvis",
            "trusted": False,
            "qr": f"https://plex.tv/api/v2/pins/qr/{pin['code']}",
            "clientIdentifier": "client",
            "location": {"code": "IL", "country": "Israel", "time_zone": "Asia/Jerusalem"},
            "expiresIn": self.pin_lifetime,
            "createdAt": "2026-01-01T10:00:00Z",
            "expiresAt": "2026-01-01T10:30:00Z",
            "authToken": self.account_token if approved else None,
            "newRegistration": None,
        }

    # -- the Media Server

    def _answer_pms(self, seen: Seen) -> Reply:
        if seen.path == "/identity":
            if seen.token is not None:
                self.problems.append("the token was sent to /identity")
            return 200, {
                "MediaContainer": {
                    "size": 0,
                    "claimed": True,
                    "machineIdentifier": self.machine_id,
                    "version": self.version,
                }
            }
        for prefix in self.stall:
            if seen.path.startswith(prefix):
                return -1, None
        for prefix, status in self.status_for.items():
            if seen.path.startswith(prefix):
                return status, {}
        for prefix in self.garbage:
            if seen.path.startswith(prefix):
                return 200, "<html><body><h1>Plex</h1> not json</body></html>"
        if seen.token not in self.accepted:
            return 401, "<html><head><title>Unauthorized</title></head><body><h1>401 Unauthorized</h1></body></html>"
        if seen.path == "/library/sections":
            return 200, {
                "MediaContainer": {
                    "size": len(self.sections),
                    "Directory": [{"key": k, "type": t, "title": n} for k, t, n in self.sections],
                }
            }
        if seen.path == "/hubs/search":
            return self._search(seen)
        section = re.fullmatch(r"/library/sections/(\d+)/all", seen.path)
        if section:
            return self._section_all(section[1], seen)
        leaves = re.fullmatch(r"/library/metadata/(\d+)/allLeaves", seen.path)
        if leaves:
            return self._all_leaves(leaves[1], seen)
        detail = re.fullmatch(r"/library/metadata/(\d+)", seen.path)
        if detail:
            return self._detail(detail[1], seen)
        return 404, "<html><head><title>Not Found</title></head><body><h1>404 Not Found</h1></body></html>"

    def _search(self, seen: Seen) -> Reply:
        query = _norm((seen.query.get("query") or [""])[0])
        if len(query) < 2:
            return 400, "<html><head><title>Bad Request</title></head><body><h1>400 Bad Request</h1></body></html>"
        words = query.split()
        movies, shows, episodes = [], [], []
        if self.hub_blind:
            return 200, {"MediaContainer": {"size": 0, "Hub": []}}

        def hit(item: dict[str, Any]) -> bool:
            names = _norm(f"{item['title']} {item.get('originalTitle', '')}")
            return all(word in names for word in words)

        def scored(item: dict[str, Any]) -> dict[str, Any]:
            exact = query in (_norm(item["title"]), _norm(item.get("originalTitle", "")))
            return {**item, "score": "1.00000" if exact else "0.52000"}

        for item in self.items.values():
            if hit(item):
                (movies if item["type"] == "movie" else shows).append(scored(item))
        for leaves in self.leaves.values():
            episodes += [scored(e) for e in leaves if hit(e)]
        hubs = []
        if movies:
            hubs.append(
                {"title": "Movies", "type": "movie", "hubIdentifier": "movie", "size": len(movies), "Metadata": movies}
            )
        if shows:
            # Shows come back as Directory entries, which is how the server lists them.
            hubs.append(
                {"title": "Shows", "type": "show", "hubIdentifier": "show", "size": len(shows), "Directory": shows}
            )
        if episodes:
            hubs.append(
                {
                    "title": "Episodes",
                    "type": "episode",
                    "hubIdentifier": "episode",
                    "size": len(episodes),
                    "Metadata": episodes,
                }
            )
        if "leonardo" in words:
            actor = {"id": 7, "filter": "actor=7", "tag": "Leonardo DiCaprio", "tagType": 6, "type": "tag"}
            hubs.append({"title": "Actors", "type": "actor", "hubIdentifier": "actor", "size": 1, "Directory": [actor]})
        return 200, {"MediaContainer": {"size": len(hubs), "Hub": hubs}}

    def _section_all(self, section: str, seen: Seen) -> Reply:
        """A library section filtered by type and by a title it contains."""
        kind = {"1": "movie", "2": "show"}.get((seen.query.get("type") or [""])[0])
        wanted = _norm((seen.query.get("title") or [""])[0])
        if (seen.headers.get("x-plex-container-start") is None) != (seen.headers.get("x-plex-container-size") is None):
            self.problems.append("only one of the paging headers was sent")
        found = [
            dict(i)
            for i in self.items.values()
            if str(i["librarySectionID"]) == section
            and i["type"] == kind
            and wanted in _norm(f"{i['title']} {i.get('originalTitle', '')}")
        ]
        return 200, {"MediaContainer": {"size": len(found), "totalSize": len(found), "offset": 0, "Metadata": found}}

    def _detail(self, key: str, seen: Seen) -> Reply:
        item = self.items.get(key) or next(
            (e for eps in self.leaves.values() for e in eps if e["ratingKey"] == key), None
        )
        if item is None:
            return 404, "<html><head><title>Not Found</title></head><body><h1>404 Not Found</h1></body></html>"
        item = dict(item)
        deck = on_deck(self.leaves.get(key, [])) if item["type"] == "show" else None
        if deck is not None and seen.query.get("includeOnDeck") == ["1"] and self.ondeck != "absent":
            item["OnDeck"] = {"Metadata": [deck] if self.ondeck == "list" else deck}
        return 200, {"MediaContainer": {"size": 1, "allowSync": True, "Metadata": [item]}}

    def _all_leaves(self, key: str, seen: Seen) -> Reply:
        episodes = self.leaves.get(key)
        if episodes is None:
            return 404, "<html><head><title>Not Found</title></head><body><h1>404 Not Found</h1></body></html>"
        start = int(seen.headers.get("x-plex-container-start", "0"))
        size = int(seen.headers.get("x-plex-container-size", str(len(episodes))))
        if (seen.headers.get("x-plex-container-start") is None) != (seen.headers.get("x-plex-container-size") is None):
            self.problems.append("only one of the paging headers was sent")
        if self.page_cap is not None:
            size = min(size, self.page_cap)
        page = episodes[start : start + size]
        return 200, {
            "MediaContainer": {"size": len(page), "totalSize": len(episodes), "offset": start, "Metadata": page}
        }

    # -- reading what happened

    def pms_log(self) -> list[Seen]:
        return [s for s in self.log if s.server == "pms"]

    def paths(self) -> list[str]:
        return [s.path for s in self.pms_log()]

    def episode_key(self, season: int, number: int, show: str = SHOW) -> str:
        return next(e["ratingKey"] for e in self.leaves[show] if e["parentIndex"] == season and e["index"] == number)
