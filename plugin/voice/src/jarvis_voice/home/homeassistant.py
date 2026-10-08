"""Home Assistant as a hub: the devices it controls join Jarvis's own.

Jarvis uses a long-lived access token, kept in the credential store under
``homeassistant:hub``:

- REST does the work. ``GET /api/states`` lists the entities (a non-admin
  token may read it), ``POST /api/services/<domain>/<service>`` acts and
  ``GET /api/states/<entity_id>`` checks the result. A call on a missing or
  unavailable entity answers 200 with nothing done, so every command reads
  the entity first and its state afterwards.
- The WebSocket API tells what REST cannot: whether the token's user is an
  administrator (``auth/current_user``), which entities are exposed to Assist
  (``homeassistant/expose_entity/list``, for administrators only) and the
  rooms (the entity, device and area registries; a child device without an
  area takes its parent's).

Home Assistant counts every 401 as a failed login: it posts a notification
and, when configured to, bans the address after a few. So a refused token is
not used again until a new one is saved, and nothing probes an
administrator-only endpoint to find out what the token may do.

Error bodies vary between versions: up to 2026.9 a device error is aiohttp's
plain-text 500, from 2026.10 a JSON ``{"message"}``. Both are handled, and
plain-text bodies are never spoken.

The entity list is cached for 30 seconds and fetched only when Jarvis is
asked something: nothing polls. Every request lists the hubs first, so a
listing that failed (Home Assistant off or hung) is not tried again for 20
seconds, rather than costing each request for another device its timeout.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import logging
import re
import ssl
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from websockets.exceptions import WebSocketException
from websockets.sync.client import connect as ws_connect

from . import net
from .base import Driver, DriverContext, Prompter
from .model import CommandSpec, DeviceRecord, HomeConfig, Outcome, Value, normalize
from .store import StoreError

log = logging.getLogger(__name__)
# websockets logs every frame at DEBUG, the auth message with the token
# included, so the WebSocket client gets a logger that stays at WARNING.
_ws_log = logging.getLogger(f"{__name__}.websocket")
_ws_log.setLevel(logging.WARNING)

DRIVER = "homeassistant"
SECRET_KEY = "homeassistant:hub"
ID_PREFIX = "ha:"
DEFAULT_URL = "http://homeassistant.local:8123"
DEFAULT_PORT = 8123

# Timings. A listing runs on the service's worker within its 20 s limit, and
# so does a command: the request timeouts below add up to less than that.
CACHE_S = 30.0
# A listing that failed is not tried again for this long (a new address or token is tried at once): every
# request lists the hubs first, so a Home Assistant that is off would cost each one its whole timeout.
FAILED_HOLD_S = 20.0
LIST_BUDGET_S = 12.0
STATES_TIMEOUT_S = 6.0
WS_BUDGET_S = 6.0
WS_OPEN_TIMEOUT_S = 3.0
CALL_BUDGET_S = 17.0
CALL_MARGIN_S = 1.5  # kept free below ctx.call_timeout
STATE_TIMEOUT_S = 4.0
# Home Assistant answers a service call only when the service has finished.
SERVICE_TIMEOUT_S = 10.0
RECHECK_WAIT_S = 0.5
MIN_REQUEST_S = 0.2
PROBE_TIMEOUT_S = 4.0
# How long a refused token stays unused (a new token is tried at once).
REFUSED_HOLD_S = 600.0
WS_MAX_MESSAGE = 32 * 1024 * 1024  # the registries of a large home run to megabytes
MAX_CHOICES = 40
# Inputs and apps (an Apple TV lists every installed app) and light effects (WLED has some 180): a value
# left out could never be chosen, so these keep far more than the modes do.
MAX_SOURCES = 300
MAX_NAME = 80
MAX_DETAIL = 160
MAX_SETUP_TRIES = 5
MAX_TOKEN_TRIES = 3
PREVIEW_NAMES = 6

ENTITY_ID = re.compile(r"[a-z0-9_]+\.[a-z0-9_]+")  # used with fullmatch: "$" lets a newline through
# A long-lived token is a JWT; anything else that fits in a header line is let through too.
TOKEN_SHAPE = re.compile(r"[A-Za-z0-9._~+/=-]{20,4096}")

# The domains Jarvis controls, and the kind each one is listed as.
KIND_BY_DOMAIN = {
    "light": "light",
    "switch": "switch",
    "input_boolean": "switch",
    "fan": "fan",
    "cover": "cover",
    "climate": "climate",
    "media_player": "media_player",
    "scene": "scene",
    "script": "scene",
    "lock": "lock",
    "vacuum": "vacuum",
    "humidifier": "humidifier",
    "water_heater": "heater",
    "alarm_control_panel": "alarm",
    "button": "other",
}
DOMAINS = frozenset(KIND_BY_DOMAIN)
# Covers whose opening lets someone in: opening them asks on screen first.
GATED_COVERS = frozenset({"garage", "gate", "door"})

# supported_features bits, from Home Assistant's *EntityFeature enums.
MP_PAUSE = 1
MP_VOLUME_SET = 4
MP_VOLUME_MUTE = 8
MP_PREVIOUS = 16
MP_NEXT = 32
MP_TURN_ON = 128
MP_TURN_OFF = 256
MP_VOLUME_STEP = 1024
MP_SELECT_SOURCE = 2048
MP_STOP = 4096
MP_PLAY = 16384
COVER_OPEN = 1
COVER_CLOSE = 2
COVER_SET_POSITION = 4
COVER_STOP = 8
CLIMATE_TARGET_TEMPERATURE = 1
CLIMATE_FAN_MODE = 8
CLIMATE_TURN_OFF = 128
CLIMATE_TURN_ON = 256
FAN_SET_SPEED = 1
FAN_PRESET_MODE = 8
LIGHT_EFFECT = 4
VACUUM_PAUSE = 4
VACUUM_STOP = 8
VACUUM_RETURN_HOME = 16
VACUUM_FAN_SPEED = 32
VACUUM_START = 8192
ALARM_ARM_HOME = 1
ALARM_ARM_AWAY = 2
ALARM_ARM_NIGHT = 4
ALARM_ARM_VACATION = 32
HUMIDIFIER_MODES = 1
WATER_TARGET_TEMPERATURE = 1
WATER_OPERATION_MODE = 2
WATER_ON_OFF = 8

# Light colour modes (supported_color_modes).
DIMMABLE_MODES = frozenset({"brightness", "color_temp", "hs", "xy", "rgb", "rgbw", "rgbww", "white"})
COLOUR_MODES = frozenset({"hs", "xy", "rgb", "rgbw", "rgbww"})
KELVIN_RANGE = (2000, 6500)  # Home Assistant's defaults when a light gives none

# How "arm" arms an alarm panel: the first mode it supports.
ARM_MODES = (
    (ALARM_ARM_AWAY, "alarm_arm_away", "armed_away"),
    (ALARM_ARM_HOME, "alarm_arm_home", "armed_home"),
    (ALARM_ARM_NIGHT, "alarm_arm_night", "armed_night"),
    (ALARM_ARM_VACATION, "alarm_arm_vacation", "armed_vacation"),
)

# Commands that need no value: command -> (service, what Jarvis asked for, the state that means it is done).
_ON_OFF = {
    "turn_on": ("turn_on", "turn on {the}", "on"),
    "turn_off": ("turn_off", "turn off {the}", "off"),
    "toggle": ("toggle", "toggle {the}", None),
}
SIMPLE: dict[str, dict[str, tuple[str, str, str | None]]] = {
    "light": _ON_OFF,
    "switch": _ON_OFF,
    "input_boolean": _ON_OFF,
    "fan": _ON_OFF,
    "humidifier": _ON_OFF,
    "media_player": {
        "turn_on": ("turn_on", "turn on {the}", None),
        "turn_off": ("turn_off", "turn off {the}", "off"),
        "toggle": ("toggle", "toggle {the}", None),
        "play": ("media_play", "play {the}", "playing"),
        "pause": ("media_pause", "pause {the}", "paused"),
        "play_pause": ("media_play_pause", "play or pause {the}", None),
        "stop": ("media_stop", "stop {the}", None),
        "next": ("media_next_track", "skip to the next track on {the}", None),
        "previous": ("media_previous_track", "go back a track on {the}", None),
        "volume_up": ("volume_up", "turn {the} up", None),
        "volume_down": ("volume_down", "turn {the} down", None),
    },
    "cover": {
        "open": ("open_cover", "open {the}", "open"),
        "close": ("close_cover", "close {the}", "closed"),
        "stop": ("stop_cover", "stop {the}", None),
    },
    "lock": {
        "lock": ("lock", "lock {the}", "locked"),
        "unlock": ("unlock", "unlock {the}", "unlocked"),
    },
    "vacuum": {
        "start": ("start", "start {the}", "cleaning"),
        "pause": ("pause", "pause {the}", "paused"),
        "stop": ("stop", "stop {the}", None),
        "dock": ("return_to_base", "send {the} back to its dock", None),
    },
    "climate": {"turn_on": ("turn_on", "turn on {the}", None)},
    "water_heater": {
        "turn_on": ("turn_on", "turn on {the}", None),
        "turn_off": ("turn_off", "turn off {the}", "off"),
    },
    "alarm_control_panel": {"disarm": ("alarm_disarm", "disarm {the}", "disarmed")},
}

# Spoken texts.
TOKEN_REFUSED = "Home Assistant rejected Jarvis's token; make a new one in home setup."
BANNED = (
    "Home Assistant refused Jarvis; it may have blocked this computer after failed sign-ins. Check its notifications."
)
STARTING = "Home Assistant is starting or stopping; try again in a minute."
UNREACHABLE = "Home Assistant is unreachable; check that it is running and on this network."
TLS_FAILED = "The secure connection to Home Assistant failed; run home setup again to check its certificate settings."
BAD_REPLY = "Home Assistant gave an answer Jarvis could not read."
NO_ADDRESS = "Home Assistant has no address saved; connect it in home setup."
NO_TOKEN = "Home Assistant has no token saved; connect it again in home setup."
BAD_TOKEN = "The saved Home Assistant token cannot be used; make a new one in home setup."
NOT_ADMIN_ANY_MORE = (
    "Jarvis may only use the Home Assistant devices exposed to Assist, but the token's user is no longer an "
    "administrator, so Jarvis cannot read that list. Run home setup again."
)
EXPOSURE_UNREADABLE = (
    "Jarvis could not read which Home Assistant devices are exposed to Assist, so it leaves them all out for now."
)
NOTHING_SAVED = "Nothing was saved."


# --------------------------------------------------------------------------- errors and refused tokens


class HaError(Exception):
    """A Home Assistant request that did not succeed. The message is for the log and never holds the token.

    ``kind``: setup (nothing usable saved), auth (the token was refused),
    forbidden (403: this computer is banned), unreachable / timeout / tls /
    bad_url (no HTTP answer), starting (503: starting or stopping), http
    (another status: see ``status`` and ``detail``, Home Assistant's own
    message when it sent JSON), bad_reply, ws (the WebSocket failed), refused
    (a WebSocket command was refused), exposure (the Assist list could not be
    read), budget (no time left). ``spoken`` says it to the user; ``during``
    is "service" when the service call itself failed.
    """

    def __init__(
        self,
        kind: str,
        message: str = "",
        *,
        status: int | None = None,
        detail: str | None = None,
        spoken: str | None = None,
    ) -> None:
        super().__init__(message or kind)
        self.kind = kind
        self.status = status
        self.detail = detail
        self.spoken = spoken
        self.during = ""

    def again(self) -> HaError:
        """The same failure, to raise anew: re-raising one object would grow its traceback each time."""
        copied = HaError(self.kind, str(self), status=self.status, detail=self.detail, spoken=self.spoken)
        copied.during = self.during
        return copied


_refused: dict[str, float] = {}
_refused_lock = threading.Lock()


def _fingerprint(token: str) -> str:
    """A short hash that tells tokens apart without keeping them."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def _refused_recently(key: str) -> bool:
    with _refused_lock:
        at = _refused.get(key)
        if at is None:
            return False
        if time.monotonic() - at > REFUSED_HOLD_S:
            _refused.pop(key, None)
            return False
        return True


def _remember_refusal(key: str) -> None:
    with _refused_lock:
        _refused[key] = time.monotonic()


def usable_token(token: str) -> bool:
    """A token an HTTP header can carry. Anything else would make http.client
    raise an error that quotes the header, token and all."""
    return bool(TOKEN_SHAPE.fullmatch(token))


@dataclass(frozen=True, slots=True)
class HubSettings:
    """Where Home Assistant is and how Jarvis signs in. ``to_json`` is what devices.json keeps (no token)."""

    url: str
    token: str = field(repr=False)
    verify_tls: bool = True
    admin: bool = False
    exposed_only: bool = False

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.token)

    @property
    def cache_key(self) -> tuple[Any, ...]:
        return (self.url, self.verify_tls, self.exposed_only, self.fingerprint)

    def to_json(self) -> dict[str, Any]:
        return {"url": self.url, "verify_tls": self.verify_tls, "admin": self.admin, "exposed_only": self.exposed_only}


def hub_from_saved(saved: dict[str, Any], secret: dict[str, Any]) -> HubSettings:
    """The hub settings from devices.json and the credential store; HaError("setup") when unusable."""
    raw_url = saved.get("url")
    url = clean_url(raw_url) if isinstance(raw_url, str) else None
    if url is None:
        raise HaError("setup", "no address saved", spoken=NO_ADDRESS)
    token = secret.get("token")
    if not isinstance(token, str) or not token:
        raise HaError("setup", "no token saved", spoken=NO_TOKEN)
    if not usable_token(token):
        raise HaError("setup", "the saved token cannot go in a header", spoken=BAD_TOKEN)
    admin = saved.get("admin") is True
    exposed = saved.get("exposed_only")
    return HubSettings(
        url,
        token,
        verify_tls=saved.get("verify_tls") is not False,
        admin=admin,
        exposed_only=admin if exposed is None else exposed is True,
    )


# --------------------------------------------------------------------------- REST


def _json_message(response: net.HttpResponse) -> str | None:
    """Home Assistant's ``{"message"}``, or None for a plain-text body (aiohttp's own pages)."""
    try:
        data = response.json()
    except ValueError:
        return None
    message = data.get("message") if isinstance(data, dict) else None
    return message.strip() if isinstance(message, str) and message.strip() else None


class HaClient:
    """REST requests to one Home Assistant, all within one time budget."""

    def __init__(self, hub: HubSettings, *, budget: float) -> None:
        self.base = hub.url.rstrip("/")
        self.verify_tls = hub.verify_tls
        self.deadline = time.monotonic() + budget
        self._token = hub.token

    def __repr__(self) -> str:
        return f"HaClient({self.base})"

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def _request(self, method: str, path: str, *, timeout: float, body: Any = None) -> net.HttpResponse:
        wait = min(timeout, self.remaining())
        if wait < MIN_REQUEST_S:
            raise HaError("budget", f"no time left for {method} {path}")
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        try:
            return net.request(
                method, f"{self.base}{path}", headers=headers, json_body=body, timeout=wait, verify_tls=self.verify_tls
            )
        except net.HttpError as exc:
            raise HaError(exc.kind, f"{method} {path}: {exc}") from None

    @staticmethod
    def _check(response: net.HttpResponse, what: str) -> None:
        """Raises for anything but 2xx. A 401 is never retried: Home Assistant counts each one as a failed login."""
        status = response.status
        if 200 <= status < 300:
            return
        if status == 401:
            raise HaError("auth", f"{what}: HTTP 401", status=status)
        if status == 403:
            raise HaError("forbidden", f"{what}: HTTP 403", status=status)
        if status == 503:
            raise HaError("starting", f"{what}: HTTP 503", status=status)
        if status in (502, 504):
            raise HaError("unreachable", f"{what}: HTTP {status} from a proxy", status=status)
        raise HaError("http", f"{what}: HTTP {status}", status=status, detail=_json_message(response))

    @staticmethod
    def _parse(response: net.HttpResponse, what: str) -> Any:
        try:
            return response.json()
        except ValueError:
            raise HaError("bad_reply", f"{what}: not JSON") from None

    def api_running(self) -> None:
        """``GET /api/``: checks the address and the token in one go."""
        response = self._request("GET", "/api/", timeout=PROBE_TIMEOUT_S)
        self._check(response, "GET /api/")
        data = self._parse(response, "GET /api/")
        if not isinstance(data, dict) or data.get("message") != "API running.":
            raise HaError("bad_reply", "GET /api/: not Home Assistant's answer")

    def states(self) -> list[dict[str, Any]]:
        response = self._request("GET", "/api/states", timeout=STATES_TIMEOUT_S)
        self._check(response, "GET /api/states")
        data = self._parse(response, "GET /api/states")
        if not isinstance(data, list):
            raise HaError("bad_reply", "GET /api/states: not a list")
        return [state for state in data if isinstance(state, dict)]

    def state(self, entity_id: str) -> dict[str, Any] | None:
        """The entity's state, or None when Home Assistant has no such entity."""
        path = f"/api/states/{entity_id}"
        response = self._request("GET", path, timeout=STATE_TIMEOUT_S)
        if response.status == 404:
            return None
        self._check(response, f"GET {path}")
        data = self._parse(response, f"GET {path}")
        if not isinstance(data, dict) or data.get("entity_id") != entity_id:
            raise HaError("bad_reply", f"GET {path}: not a state")
        return data

    def call_service(self, domain: str, service: str, data: dict[str, Any]) -> list[dict[str, Any]]:
        """Calls the service; returns the states it changed (possibly none, even when it worked)."""
        path = f"/api/services/{domain}/{service}"
        response = self._request("POST", path, timeout=SERVICE_TIMEOUT_S, body=data)
        self._check(response, f"POST {path}")
        try:
            changed = response.json()
        except ValueError:
            return []  # the call went through; what it returned does not matter
        if isinstance(changed, dict):  # the shape with ?return_response
            changed = changed.get("changed_states")
        return [state for state in changed if isinstance(state, dict)] if isinstance(changed, list) else []


# --------------------------------------------------------------------------- WebSocket


def websocket_url(base_url: str) -> str:
    """``ws://host:8123/api/websocket`` for ``http://host:8123`` (``wss`` for https)."""
    parts = urlsplit(base_url)
    scheme = "wss" if parts.scheme == "https" else "ws"
    return f"{scheme}://{parts.netloc}/api/websocket"


class HaSocket:
    """One short WebSocket session: sign in, ask a few things, close."""

    def __init__(self, hub: HubSettings, *, budget: float | None = None) -> None:
        self.url = websocket_url(hub.url)
        self._token = hub.token
        self._verify = hub.verify_tls
        self._deadline = time.monotonic() + (WS_BUDGET_S if budget is None else budget)
        self._last_id = 0
        self._conn: Any = None
        # connect() is becoming a context manager (websockets 17 warns otherwise); the
        # stack keeps the connection open for this session and closes it in close().
        self._stack = contextlib.ExitStack()

    def __repr__(self) -> str:
        return f"HaSocket({self.url})"

    def _left(self) -> float:
        return self._deadline - time.monotonic()

    def _tls(self, *, newest: bool) -> ssl.SSLContext | None:
        if not self.url.startswith("wss://"):
            return None
        context = ssl.create_default_context()
        if not self._verify:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        if not newest:
            # websockets' sync client reads on one thread while it writes on another. Over TLS 1.3 a
            # session ticket (most servers send two after the handshake) read while the upgrade request is
            # written stalls that request until the open times out: about one open in twenty, in tests.
            # TLS 1.2 sends no such message after the handshake; a server that refuses 1.2 gets 1.3.
            context.maximum_version = ssl.TLSVersion.TLSv1_2
        return context

    def _open(self, context: ssl.SSLContext | None) -> Any:
        return self._stack.enter_context(
            ws_connect(
                self.url,
                ssl=context,
                proxy=None,  # a hub on the home network: never through a proxy
                open_timeout=max(MIN_REQUEST_S, min(WS_OPEN_TIMEOUT_S, self._left())),
                close_timeout=1.0,
                ping_interval=None,
                max_size=WS_MAX_MESSAGE,
                logger=_ws_log,
            )
        )

    def __enter__(self) -> HaSocket:
        try:
            try:
                self._conn = self._open(self._tls(newest=False))
            except (ssl.SSLError, ConnectionError) as exc:  # a refusal may come as a reset, not an alert
                if isinstance(exc, ssl.SSLCertVerificationError) or not self.url.startswith("wss://"):
                    raise
                log.debug("Home Assistant's WebSocket refused TLS 1.2 (%s); trying TLS 1.3", type(exc).__name__)
                self._conn = self._open(self._tls(newest=True))
        except (OSError, TimeoutError, WebSocketException) as exc:  # ssl.SSLError is an OSError
            raise HaError("ws", f"{self.url}: {type(exc).__name__}") from None
        try:
            if self._receive().get("type") != "auth_required":
                raise HaError("ws", f"{self.url}: no auth_required")
            self._send({"type": "auth", "access_token": self._token})
            answer = self._receive().get("type")
            if answer == "auth_invalid":
                raise HaError("auth", f"{self.url}: auth_invalid")
            if answer != "auth_ok":
                raise HaError("ws", f"{self.url}: unexpected answer to auth")
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._conn = None
        try:
            self._stack.close()
        except Exception as exc:  # noqa: BLE001 - closing is best effort
            log.debug("closing the Home Assistant WebSocket: %s", type(exc).__name__)

    def ask(self, kind: str) -> Any:
        """The result of one command; HaError("refused") when Home Assistant says no."""
        self._last_id += 1  # ids must increase within a connection
        ident = self._last_id
        self._send({"id": ident, "type": kind})
        while True:
            message = self._receive()
            if message.get("id") != ident or message.get("type") != "result":
                continue
            if message.get("success") is True:
                return message.get("result")
            error = message.get("error") if isinstance(message.get("error"), dict) else {}
            raise HaError("refused", f"{kind}: {error.get('code') or 'error'}")

    def _send(self, message: dict[str, Any]) -> None:
        try:
            self._conn.send(json.dumps(message))
        except (OSError, WebSocketException) as exc:
            raise HaError("ws", f"{self.url}: {type(exc).__name__}") from None

    def _receive(self) -> dict[str, Any]:
        left = self._left()
        if left <= 0:
            raise HaError("timeout", f"{self.url}: no time left")
        try:
            raw = self._conn.recv(timeout=left)
        except TimeoutError:
            raise HaError("timeout", f"{self.url}: no answer in time") from None
        except (OSError, WebSocketException) as exc:
            raise HaError("ws", f"{self.url}: {type(exc).__name__}") from None
        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            raise HaError("bad_reply", f"{self.url}: not JSON") from None
        if not isinstance(message, dict):
            raise HaError("bad_reply", f"{self.url}: not an object")
        return message


@dataclass(slots=True)
class HubView:
    """What the WebSocket API told; None where it could not say."""

    is_admin: bool | None = None
    exposed: set[str] | None = None
    rooms: dict[str, str] = field(default_factory=dict)
    # Configuration, diagnostic and hidden entities: Assist leaves them out by default, and so does Jarvis.
    hidden: set[str] = field(default_factory=set)


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def exposed_to_assist(result: Any) -> set[str]:
    """The entities exposed to Assist ("conversation"), from ``homeassistant/expose_entity/list``."""
    entities = result.get("exposed_entities") if isinstance(result, dict) else None
    if not isinstance(entities, dict):
        raise HaError("bad_reply", "expose_entity/list: no exposed_entities")
    return {eid for eid, to in entities.items() if isinstance(to, dict) and to.get("conversation") is True}


def rooms_from_registries(entities: Any, devices: Any, areas: Any) -> tuple[dict[str, str], set[str]]:
    """Each entity's room (its own area, else its device's, else the parent device's), and the hidden ones."""
    names = {
        area["area_id"]: area["name"].strip()
        for area in _dicts(areas)
        if isinstance(area.get("area_id"), str) and isinstance(area.get("name"), str) and area["name"].strip()
    }
    by_id = {device["id"]: device for device in _dicts(devices) if isinstance(device.get("id"), str)}

    def device_area(device_id: Any) -> str | None:
        seen: set[str] = set()
        while isinstance(device_id, str) and device_id not in seen:
            seen.add(device_id)
            device = by_id.get(device_id)
            if device is None:
                return None
            area = device.get("area_id")
            if isinstance(area, str) and area:
                return area
            # A child device without an area of its own is where its parent is.
            # (via_device_id is a different link: a Zigbee bulb is not where its coordinator is.)
            device_id = device.get("parent_device_id")
        return None

    listing = entities.get("entities") if isinstance(entities, dict) else entities
    rooms: dict[str, str] = {}
    hidden: set[str] = set()
    for entry in _dicts(listing):
        entity_id = entry.get("ei")
        if not isinstance(entity_id, str):
            continue
        if entry.get("ec") is not None or entry.get("hb"):
            hidden.add(entity_id)
        own = entry.get("ai")
        area = own if isinstance(own, str) and own else device_area(entry.get("di"))
        if area is not None and area in names:
            rooms[entity_id] = names[area]
    return rooms, hidden


def read_view(hub: HubSettings, *, want_exposed: bool) -> HubView:
    """Asks the WebSocket API who the token's user is, what is exposed to Assist (when wanted and
    allowed) and where everything is. Never raises: what it could not learn stays None or empty."""
    view = HubView()
    ws_key = f"ws:{hub.fingerprint}"
    if _refused_recently(ws_key):
        return view
    try:
        with HaSocket(hub) as ws:
            user = ws.ask("auth/current_user")
            view.is_admin = isinstance(user, dict) and user.get("is_admin") is True
            # Only an administrator may read the Assist list: asking otherwise is refused.
            if want_exposed and view.is_admin:
                try:
                    view.exposed = exposed_to_assist(ws.ask("homeassistant/expose_entity/list"))
                except HaError as err:
                    if err.kind not in ("refused", "bad_reply"):
                        raise
                    log.info("Home Assistant's Assist list: %s", err)
            try:
                entities = ws.ask("config/entity_registry/list_for_display")
                devices = ws.ask("config/device_registry/list")
                areas = ws.ask("config/area_registry/list")
            except HaError as err:
                if err.kind != "refused":
                    raise
                log.info("Home Assistant's registries: %s; rooms are left out", err)
            else:
                view.rooms, view.hidden = rooms_from_registries(entities, devices, areas)
    except HaError as err:
        if err.kind == "auth":
            _remember_refusal(ws_key)  # each refused sign-in is a failed-login notice in Home Assistant
        log.info("Home Assistant's WebSocket API: %s (%s); rooms are left out", err.kind, err)
    return view


# --------------------------------------------------------------------------- entities


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _num(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def _text(value: Any) -> str:
    return " ".join(value.split())[:MAX_NAME] if isinstance(value, str) else ""


def _strings(value: Any, limit: int = MAX_CHOICES) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()][:limit]


def _display_name(friendly: Any, entity_id: str) -> str:
    name = _text(friendly)
    if name:
        return name
    return entity_id.partition(".")[2].replace("_", " ").strip().capitalize() or entity_id


def entity_kind(domain: str, device_class: str | None) -> str:
    if domain == "cover":
        return "garage" if device_class in ("garage", "gate") else "cover"
    if domain == "media_player":
        return {"tv": "tv", "speaker": "speaker"}.get(device_class or "", "media_player")
    if domain == "switch":
        return "plug" if device_class == "outlet" else "switch"
    return KIND_BY_DOMAIN.get(domain, "other")


def _domain_settings(domain: str, attributes: dict[str, Any]) -> dict[str, Any]:
    """What ``commands`` needs from the entity's attributes."""
    found: dict[str, Any] = {}

    def keep(key: str, value: Any) -> None:
        if value not in (None, [], ""):
            found[key] = value

    if domain == "light":
        keep("color_modes", _strings(attributes.get("supported_color_modes")))
        keep("effects", _strings(attributes.get("effect_list"), MAX_SOURCES))
        keep("min_kelvin", _number(attributes.get("min_color_temp_kelvin")))
        keep("max_kelvin", _number(attributes.get("max_color_temp_kelvin")))
    elif domain == "media_player":
        keep("sources", _strings(attributes.get("source_list"), MAX_SOURCES))
    elif domain == "climate":
        keep("hvac_modes", _strings(attributes.get("hvac_modes")))
        keep("fan_modes", _strings(attributes.get("fan_modes")))
        keep("min_temp", _number(attributes.get("min_temp")))
        keep("max_temp", _number(attributes.get("max_temp")))
    elif domain == "fan":
        keep("preset_modes", _strings(attributes.get("preset_modes")))
    elif domain == "humidifier":
        keep("modes", _strings(attributes.get("available_modes")))
    elif domain == "water_heater":
        keep("operation_modes", _strings(attributes.get("operation_list")))
        keep("min_temp", _number(attributes.get("min_temp")))
        keep("max_temp", _number(attributes.get("max_temp")))
    elif domain == "vacuum":
        keep("fan_speeds", _strings(attributes.get("fan_speed_list")))
    elif domain == "lock":
        if attributes.get("code_format"):
            found["needs_code"] = True
    elif domain == "alarm_control_panel" and attributes.get("code_format"):
        found["needs_code"] = True
        found["code_to_arm"] = attributes.get("code_arm_required") is not False
    return found


def entity_record(state: dict[str, Any], room: str | None = None) -> DeviceRecord | None:
    """A device for one entity of a domain Jarvis controls, else None."""
    entity_id = state.get("entity_id")
    if not isinstance(entity_id, str) or not ENTITY_ID.fullmatch(entity_id):
        return None
    domain = entity_id.partition(".")[0]
    if domain not in DOMAINS:
        return None
    attributes = state.get("attributes") if isinstance(state.get("attributes"), dict) else {}
    assert isinstance(attributes, dict)
    device_class = _text(attributes.get("device_class")) or None
    settings: dict[str, Any] = {
        "entity_id": entity_id,
        "domain": domain,
        "supported_features": _int(attributes.get("supported_features")),
    }
    if device_class:
        settings["device_class"] = device_class
    settings.update(_domain_settings(domain, attributes))
    name = _display_name(attributes.get("friendly_name"), entity_id)
    return DeviceRecord(
        f"{ID_PREFIX}{entity_id}", DRIVER, name, entity_kind(domain, device_class), room, settings=settings
    )


# --------------------------------------------------------------------------- commands


def entity_commands(device: DeviceRecord) -> list[CommandSpec]:
    """What the entity can do, from its saved domain, features and attributes."""
    s = device.settings
    domain = s.get("domain")
    features = _int(s.get("supported_features"))

    def has(bit: int) -> bool:
        return bool(features & bit)

    power = [CommandSpec("turn_on"), CommandSpec("turn_off"), CommandSpec("toggle")]
    specs: list[CommandSpec] = []
    if domain == "light":
        modes = set(_strings(s.get("color_modes")))
        specs += power
        if modes & DIMMABLE_MODES:
            specs.append(CommandSpec("set_brightness", "percent"))
        if modes & COLOUR_MODES:
            specs.append(CommandSpec("set_color", "text", hint="a colour name or #rrggbb"))
        if "color_temp" in modes:
            specs.append(CommandSpec("set_color_temp", "text", hint="warm|neutral|cool, or 0-100 (0 = warmest)"))
        effects = _strings(s.get("effects"), MAX_SOURCES)
        if has(LIGHT_EFFECT) and effects:
            specs.append(CommandSpec("set_mode", "choice", choices=tuple(effects)))
    elif domain in ("switch", "input_boolean"):
        specs += power
    elif domain == "fan":
        specs += power
        if has(FAN_SET_SPEED):
            specs.append(CommandSpec("set_fan_speed", "percent"))
        presets = _strings(s.get("preset_modes"))
        if has(FAN_PRESET_MODE) and presets:
            specs.append(CommandSpec("set_mode", "choice", choices=tuple(presets)))
    elif domain == "cover":
        gated = "screen" if s.get("device_class") in GATED_COVERS else "free"
        if has(COVER_OPEN):
            specs.append(CommandSpec("open", tier=gated))
        if has(COVER_CLOSE):
            specs.append(CommandSpec("close"))
        if has(COVER_STOP):
            specs.append(CommandSpec("stop"))
        if has(COVER_SET_POSITION):
            # Setting a garage door to 30% opens it too.
            specs.append(CommandSpec("set_position", "percent", tier=gated))
    elif domain == "climate":
        modes = _strings(s.get("hvac_modes"))
        if has(CLIMATE_TURN_ON):
            specs.append(CommandSpec("turn_on"))
        if has(CLIMATE_TURN_OFF) or "off" in modes:
            specs.append(CommandSpec("turn_off"))
        if has(CLIMATE_TARGET_TEMPERATURE):
            low, high = _number(s.get("min_temp")), _number(s.get("max_temp"))
            specs.append(CommandSpec("set_temperature", "number", low=low, high=high, hint="degrees"))
        if modes:
            specs.append(CommandSpec("set_mode", "choice", choices=tuple(modes)))
        fans = _strings(s.get("fan_modes"))
        if has(CLIMATE_FAN_MODE) and fans:
            specs.append(CommandSpec("set_fan_speed", "choice", choices=tuple(fans)))
    elif domain == "media_player":
        specs += _media_commands(device, has)
    elif domain in ("scene", "script", "button"):
        specs.append(CommandSpec("activate"))
    elif domain == "lock":
        specs += [CommandSpec("lock"), CommandSpec("unlock", tier="screen")]
    elif domain == "vacuum":
        for bit, command in ((VACUUM_START, "start"), (VACUUM_PAUSE, "pause"), (VACUUM_STOP, "stop")):
            if has(bit):
                specs.append(CommandSpec(command))
        if has(VACUUM_RETURN_HOME):
            specs.append(CommandSpec("dock"))
        speeds = _strings(s.get("fan_speeds"))
        if has(VACUUM_FAN_SPEED) and speeds:
            specs.append(CommandSpec("set_fan_speed", "choice", choices=tuple(speeds)))
    elif domain == "humidifier":
        specs += power
        modes = _strings(s.get("modes"))
        if has(HUMIDIFIER_MODES) and modes:
            specs.append(CommandSpec("set_mode", "choice", choices=tuple(modes)))
    elif domain == "water_heater":
        if has(WATER_ON_OFF):
            specs += [CommandSpec("turn_on"), CommandSpec("turn_off")]
        if has(WATER_TARGET_TEMPERATURE):
            low, high = _number(s.get("min_temp")), _number(s.get("max_temp"))
            specs.append(CommandSpec("set_temperature", "number", low=low, high=high, hint="degrees"))
        modes = _strings(s.get("operation_modes"))
        if has(WATER_OPERATION_MODE) and modes:
            specs.append(CommandSpec("set_mode", "choice", choices=tuple(modes)))
    elif domain == "alarm_control_panel":
        if any(has(bit) for bit, _, _ in ARM_MODES):
            specs.append(CommandSpec("arm"))
        specs.append(CommandSpec("disarm", tier="screen"))
    return specs


def _media_commands(device: DeviceRecord, has: Any) -> list[CommandSpec]:
    specs: list[CommandSpec] = []
    for bit, command in ((MP_TURN_ON, "turn_on"), (MP_TURN_OFF, "turn_off")):
        if has(bit):
            specs.append(CommandSpec(command))
    if has(MP_TURN_ON) and has(MP_TURN_OFF):
        specs.append(CommandSpec("toggle"))
    if has(MP_PLAY):
        specs.append(CommandSpec("play"))
    if has(MP_PAUSE):
        specs.append(CommandSpec("pause"))
    if has(MP_PLAY) and has(MP_PAUSE):
        specs.append(CommandSpec("play_pause"))
    for bit, command in ((MP_STOP, "stop"), (MP_NEXT, "next"), (MP_PREVIOUS, "previous")):
        if has(bit):
            specs.append(CommandSpec(command))
    if has(MP_VOLUME_SET) or has(MP_VOLUME_STEP):
        specs += [CommandSpec("volume_up"), CommandSpec("volume_down")]
    if has(MP_VOLUME_SET):
        specs.append(CommandSpec("set_volume", "percent"))
    if has(MP_VOLUME_MUTE):
        specs += [CommandSpec("mute"), CommandSpec("unmute")]
    if has(MP_SELECT_SOURCE):
        sources = _strings(device.settings.get("sources"), MAX_SOURCES)
        if sources:
            specs += [CommandSpec("set_input", "choice", choices=tuple(sources)), CommandSpec("list_inputs")]
        else:
            specs.append(CommandSpec("set_input", "text", hint="an input or app name"))
    return specs


@dataclass(frozen=True, slots=True)
class Action:
    """One service call. ``phrase`` completes "Asked Home Assistant to ..."; ``expect`` is the state that
    means it is done; ``done`` is the whole answer for entities whose state says nothing (scenes, buttons)."""

    service: str
    data: dict[str, Any]
    phrase: str
    expect: str | None = None
    done: str | None = None


def _the(device: DeviceRecord) -> str:
    """ "the Kitchen light", but "Movie night" for a scene (it reads as a name)."""
    return device.name if device.kind == "scene" else f"the {device.name}"


def _capital(text: str) -> str:
    return text[:1].upper() + text[1:]


def _words(value: str) -> str:
    return value.replace("_", " ")


_HEX_COLOUR = re.compile(r"#?([0-9a-fA-F]{6})")
_RGB_COLOUR = re.compile(r"\(?\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*\)?")


def colour_data(value: str) -> dict[str, Any] | None:
    """``{"rgb_color": [r, g, b]}`` for #rrggbb or r,g,b; ``{"color_name": ...}`` for a name; else None."""
    text = value.strip()
    if match := _HEX_COLOUR.fullmatch(text):
        digits = match.group(1)
        return {"rgb_color": [int(digits[i : i + 2], 16) for i in (0, 2, 4)]}
    if match := _RGB_COLOUR.fullmatch(text):
        rgb = [int(part) for part in match.groups()]
        return {"rgb_color": rgb} if all(0 <= c <= 255 for c in rgb) else None
    # Home Assistant knows the CSS colour names, written without spaces ("dark blue" is "darkblue").
    name = re.sub(r"[\s_-]+", "", text.lower())
    return {"color_name": name} if re.fullmatch(r"[a-z]{3,30}", name) else None


_WARMTH = {
    "warm": 0.0,
    "warmest": 0.0,
    "warm white": 0.0,
    "soft": 0.0,
    "soft white": 0.0,
    "neutral": 50.0,
    "natural": 50.0,
    "medium": 50.0,
    "white": 50.0,
    "cool": 100.0,
    "cool white": 100.0,
    "cold": 100.0,
    "coolest": 100.0,
    "daylight": 100.0,
}


def kelvin_for(device: DeviceRecord, value: str) -> int | None:
    """The colour temperature for warm|neutral|cool, a percent (0 = warmest) or a kelvin figure like 2700K."""
    low = _number(device.settings.get("min_kelvin")) or KELVIN_RANGE[0]
    high = _number(device.settings.get("max_kelvin")) or KELVIN_RANGE[1]
    text = normalize(value)
    if text in _WARMTH:
        percent = _WARMTH[text]
    else:
        match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(k|kelvin|%)?", value.strip().lower())
        if match is None:
            return None
        number = float(match.group(1))
        if match.group(2) in ("k", "kelvin") or number > 100:
            return round(min(max(number, low), high))
        percent = number
    return round(low + (high - low) * percent / 100)


def plan_action(device: DeviceRecord, command: str, value: Value) -> Action | Outcome:
    """The service call for ``command`` (an Outcome when there is nothing to call, or it cannot be done)."""
    if command not in {spec.name for spec in entity_commands(device)}:
        return Outcome.fail("unsupported", f"{_capital(_the(device))} can't {command.replace('_', ' ')}.")
    s = device.settings
    domain = str(s.get("domain"))
    the = _the(device)
    simple = SIMPLE.get(domain, {}).get(command)
    if simple is not None:
        service, phrase, expect = simple
        return Action(service, {}, phrase.format(the=the), expect)
    if command == "activate":
        if domain == "button":
            return Action("press", {}, f"press {the}", done=f"Pressed {the}.")
        if domain == "script":
            return Action("turn_on", {}, f"start {the}", done=f"Started {the}.")
        return Action("turn_on", {}, f"activate {the}", done=f"Activated {the}.")
    if domain == "light":
        if command == "set_brightness":
            return Action("turn_on", {"brightness_pct": value}, f"set {the} to {value}%")
        if command == "set_color":
            data = colour_data(str(value))
            if data is None:
                return Outcome.fail("bad_value", "set_color takes a colour name, #rrggbb or r,g,b.")
            return Action("turn_on", data, f"make {the} {value}")
        if command == "set_color_temp":
            kelvin = kelvin_for(device, str(value))
            if kelvin is None:
                return Outcome.fail("bad_value", "set_color_temp takes warm, neutral, cool or 0-100 (0 = warmest).")
            return Action("turn_on", {"color_temp_kelvin": kelvin}, f"set {the} to {value} white")
        return Action("turn_on", {"effect": value}, f"set {the} to the {value} effect")
    if domain == "fan":
        if command == "set_fan_speed":
            return Action("set_percentage", {"percentage": value}, f"set {the} to {value}%")
        return Action("set_preset_mode", {"preset_mode": value}, f"set {the} to {value}")
    if domain == "cover":
        return Action("set_cover_position", {"position": value}, f"set {the} to {value}% open")
    if domain == "climate":
        if command == "turn_off":
            if _int(s.get("supported_features")) & CLIMATE_TURN_OFF:
                return Action("turn_off", {}, f"turn off {the}", "off")
            return Action("set_hvac_mode", {"hvac_mode": "off"}, f"turn off {the}", "off")
        if command == "set_temperature":
            return Action("set_temperature", {"temperature": value}, f"set {the} to {_num(float(value or 0))} degrees")
        if command == "set_mode":
            return Action("set_hvac_mode", {"hvac_mode": value}, f"set {the} to {_words(str(value))}", str(value))
        return Action("set_fan_mode", {"fan_mode": value}, f"set {the}'s fan to {value}")
    if domain == "media_player":
        if command == "set_volume":
            level = round(int(value or 0) / 100, 2)
            return Action("volume_set", {"volume_level": level}, f"set {the}'s volume to {value}%")
        if command in ("mute", "unmute"):
            return Action("volume_mute", {"is_volume_muted": command == "mute"}, f"{command} {the}")
        if command == "set_input":
            return Action("select_source", {"source": value}, f"switch {the} to {value}")
        sources = _strings(s.get("sources"), MAX_SOURCES)
        return Outcome.done(f"{_capital(the)} can switch to: {', '.join(sources)}.")
    if domain == "vacuum":
        return Action("set_fan_speed", {"fan_speed": value}, f"set {the}'s suction to {value}")
    if domain == "humidifier":
        return Action("set_mode", {"mode": value}, f"set {the} to {value} mode")
    if domain == "water_heater":
        if command == "set_temperature":
            return Action("set_temperature", {"temperature": value}, f"set {the} to {_num(float(value or 0))} degrees")
        return Action("set_operation_mode", {"operation_mode": value}, f"set {the} to {_words(str(value))}", str(value))
    if domain == "alarm_control_panel":
        features = _int(s.get("supported_features"))
        for bit, service, armed in ARM_MODES:
            if features & bit:
                return Action(service, {}, f"arm {the}", armed)
    return Outcome.fail("unsupported", f"{_capital(the)} can't {command.replace('_', ' ')}.")


def _needs_code(device: DeviceRecord, command: str) -> bool:
    s = device.settings
    if not s.get("needs_code"):
        return False
    if s.get("domain") == "lock":
        return command in ("lock", "unlock")
    return command == "disarm" or (command == "arm" and s.get("code_to_arm") is not False)


# --------------------------------------------------------------------------- states in words

_LOCK_WORDS = {"jammed": "jammed (it could not finish)"}
_ALARM_WORDS = {
    "armed_away": "armed (away)",
    "armed_home": "armed (home)",
    "armed_night": "armed (night)",
    "armed_vacation": "armed (vacation)",
    "armed_custom_bypass": "armed (custom bypass)",
    "pending": "about to go off",
}
_VACUUM_WORDS = {"returning": "returning to its dock", "error": "reporting an error"}


def _percent_of(value: Any, scale: float) -> int | None:
    number = _number(value)
    return None if number is None else round(number * 100 / scale)


def state_words(device: DeviceRecord, state: dict[str, Any]) -> str:
    """The entity's state in a sentence: "The Kitchen light is on at 40%."."""
    the = _the(device)
    subject = _capital(the)
    value = str(state.get("state") or "unknown")
    attributes = state.get("attributes") if isinstance(state.get("attributes"), dict) else {}
    assert isinstance(attributes, dict)
    domain = device.settings.get("domain")
    if value == "unavailable":
        return f"Home Assistant says {the} is unavailable."
    if domain == "scene":
        return f"{subject} is a Home Assistant scene; activate runs it."
    if domain == "button":
        return f"{subject} is a button; activate presses it."
    if domain == "script":
        return f"{subject} is {'running' if value == 'on' else 'not running'}."
    if value == "unknown":
        return f"Home Assistant does not know the state of {the}."
    if domain == "light" and value == "on":
        brightness = _percent_of(attributes.get("brightness"), 255)
        return f"{subject} is on" + (f" at {brightness}%" if brightness is not None else "") + "."
    if domain == "fan" and value == "on":
        speed = _number(attributes.get("percentage"))
        preset = _text(attributes.get("preset_mode"))
        detail = f" at {_num(speed)}%" if speed else (f" on {preset}" if preset else "")
        return f"{subject} is on{detail}."
    if domain == "humidifier" and value == "on":
        target = _number(attributes.get("humidity"))
        return f"{subject} is on" + (f", aiming for {_num(target)}% humidity" if target is not None else "") + "."
    if domain == "cover":
        position = _number(attributes.get("current_position"))
        if value == "open" and position is not None and 0 < position < 100:
            return f"{subject} is open at {_num(position)}%."
        return f"{subject} is {_words(value)}."
    if domain == "climate":
        return _climate_words(subject, value, attributes)
    if domain == "water_heater":
        target = _number(attributes.get("temperature"))
        if value == "off":
            return f"{subject} is off."
        return f"{subject} is on {_words(value)}" + (f" at {_num(target)} degrees" if target is not None else "") + "."
    if domain == "media_player":
        return _media_words(subject, value, attributes)
    if domain == "lock":
        return f"{subject} is {_LOCK_WORDS.get(value, _words(value))}."
    if domain == "alarm_control_panel":
        return f"{subject} is {_ALARM_WORDS.get(value, _words(value))}."
    if domain == "vacuum":
        battery = _number(attributes.get("battery_level"))
        extra = f" (battery {_num(battery)}%)" if battery is not None else ""
        return f"{subject} is {_VACUUM_WORDS.get(value, _words(value))}{extra}."
    return f"{subject} is {_words(value)}."


def _climate_words(subject: str, mode: str, attributes: dict[str, Any]) -> str:
    target = _number(attributes.get("temperature"))
    current = _number(attributes.get("current_temperature"))
    if mode == "off":
        text = f"{subject} is off"
    else:
        text = f"{subject} is set to {_words(mode)}" + (f" at {_num(target)} degrees" if target is not None else "")
    if current is not None:
        text += f"; it is {_num(current)} degrees now"
    return text + "."


def _media_words(subject: str, value: str, attributes: dict[str, Any]) -> str:
    if value in ("off", "standby"):
        return f"{subject} is {'off' if value == 'off' else 'in standby'}."
    title = _text(attributes.get("media_title"))
    artist = _text(attributes.get("media_artist"))
    app = _text(attributes.get("app_name")) or _text(attributes.get("source"))
    if value == "playing":
        head = f"{subject} is playing" + (f" {title}" if title else "") + (f" by {artist}" if title and artist else "")
    elif value == "paused":
        head = f"{subject} is paused" + (f" on {title}" if title else "")
    elif value == "buffering":
        head = f"{subject} is loading"
    else:
        head = f"{subject} is on"
    if app:
        head += f" ({app})"
    parts = [head]
    volume = _percent_of(attributes.get("volume_level"), 1.0)
    if volume is not None:
        parts.append(f"volume {volume}%")
    if attributes.get("is_volume_muted") is True:
        parts.append("muted")
    return ", ".join(parts) + "."


# --------------------------------------------------------------------------- failures in words


def _clean_detail(detail: str | None, device: DeviceRecord | None, token: str | None) -> str | None:
    """Home Assistant's own error message, if it is fit to speak: short, with no address, id or secret."""
    if not detail:
        return None
    text = " ".join(detail.split())
    if token and token in text:
        return None
    if device is not None:
        entity_id = device.settings.get("entity_id")
        if isinstance(entity_id, str) and entity_id:
            text = text.replace(entity_id, _the(device))
    if len(text) > MAX_DETAIL or re.search(r"https?://|\d{1,3}(?:\.\d{1,3}){3}|\b[a-z_]+\.[a-z0-9_]+\b", text):
        return None
    return text.rstrip(".")


def hub_problem(err: HaError) -> str:
    """What is wrong with the hub, in a sentence (for /jarvis home and the device list)."""
    if err.spoken:
        return err.spoken
    return {
        "auth": TOKEN_REFUSED,
        "forbidden": BANNED,
        "starting": STARTING,
        "unreachable": UNREACHABLE,
        "timeout": "Home Assistant did not answer in time.",
        "budget": "Home Assistant did not answer in time.",
        "tls": TLS_FAILED,
        "bad_url": "Home Assistant's saved address is not valid; connect it again in home setup.",
        "exposure": EXPOSURE_UNREADABLE,
    }.get(err.kind) or (
        f"Home Assistant answered with an error (HTTP {err.status})." if err.status is not None else BAD_REPLY
    )


def failure_outcome(
    err: HaError,
    device: DeviceRecord | None = None,
    action: Action | None = None,
    *,
    command: str = "",
    token: str | None = None,
) -> Outcome:
    """A failed request as a spoken Outcome: what went wrong and what to do."""
    kind = err.kind
    the = _the(device) if device is not None else "it"
    if kind == "setup":
        return Outcome.fail("needs_setup", err.spoken or NO_ADDRESS)
    if kind == "auth":
        return Outcome.fail("auth", TOKEN_REFUSED)
    if kind == "forbidden":
        return Outcome.fail("auth", BANNED)
    if kind == "starting":
        return Outcome.fail("unreachable", STARTING)
    if kind in ("timeout", "budget"):
        if action is not None and err.during == "service":
            return Outcome.fail("timeout", f"Home Assistant did not answer in time; it may still {action.phrase}.")
        return Outcome.fail("timeout", "Home Assistant did not answer in time.")
    if kind == "unreachable":
        return Outcome.fail("unreachable", UNREACHABLE)
    if kind == "tls":
        return Outcome.fail("unreachable", TLS_FAILED)
    if kind == "bad_url":
        return Outcome.fail(
            "needs_setup", "Home Assistant's saved address is not valid; connect it again in home setup."
        )
    if kind != "http":
        return Outcome.fail("failed", BAD_REPLY)
    status = err.status or 0
    if device is not None and status in (400, 500) and _needs_code(device, command):
        return Outcome.fail(
            "unsupported",
            f"Home Assistant needs a code to {command} {the}, and Jarvis does not send codes. "
            "Set a default code for it in Home Assistant, or use the Home Assistant app.",
        )
    detail = _clean_detail(err.detail, device, token)
    if status == 400:
        return Outcome.fail(
            "bad_value", f"Home Assistant did not accept that for {the}" + (f": {detail}." if detail else ".")
        )
    if status == 500:
        doing = action.phrase if action is not None else f"read {the}"
        if detail:
            return Outcome.fail("failed", f"Home Assistant could not {doing}: {detail}.")
        return Outcome.fail("failed", f"Home Assistant could not {doing}; the device may be offline.")
    if status == 404:
        return Outcome.fail(
            "needs_setup", "Home Assistant's API did not answer at the saved address; connect it again in home setup."
        )
    return Outcome.fail("failed", f"Home Assistant answered with an error (HTTP {status}).")


# --------------------------------------------------------------------------- the driver


@dataclass(slots=True)
class _Listing:
    key: tuple[Any, ...]
    at: float
    devices: list[DeviceRecord]
    exposed_only: bool


@dataclass(slots=True)
class _Failed:
    key: tuple[Any, ...]
    at: float
    error: HaError


class HomeAssistantDriver(Driver):
    name = DRIVER
    label = "Home Assistant"

    # As attributes so tests can shorten them.
    cache_s = CACHE_S
    failed_hold_s = FAILED_HOLD_S
    recheck_wait_s = RECHECK_WAIT_S

    def __init__(self, ctx: DriverContext) -> None:
        super().__init__(ctx)
        self._lock = threading.Lock()
        self._listing: _Listing | None = None
        self._failed: _Failed | None = None

    # ------------------------------------------------------------------ Driver

    def hub_devices(self) -> list[DeviceRecord]:
        """Home Assistant's controllable entities, read at most every 30 seconds (and only when asked).
        After a failure the same settings are not tried again for 20 seconds: the failure is raised again."""
        with self._lock:
            try:
                hub = self.settings()
            except HaError as err:
                self._listing_failed(err)
                raise
            now = time.monotonic()
            listing = self._listing
            if listing is not None and listing.key == hub.cache_key and now - listing.at < self.cache_s:
                return copy.deepcopy(listing.devices)
            failed = self._failed
            if failed is not None and failed.key == hub.cache_key and now - failed.at < self.failed_hold_s:
                # Raised again as it was; asking again within the hold does not extend it.
                log.debug("Home Assistant devices: %s a moment ago; not asked again yet", failed.error.kind)
                raise failed.error.again()
            try:
                devices = self.fetch(hub)
            except HaError as err:
                self._listing_failed(err)
                self._failed = _Failed(hub.cache_key, time.monotonic(), err.again())
                raise
            self._listing = _Listing(hub.cache_key, time.monotonic(), devices, hub.exposed_only)
            self._failed = None
            return copy.deepcopy(devices)

    def _listing_failed(self, err: HaError) -> None:
        self._listing = None
        err.spoken = hub_problem(err)
        log.info("Home Assistant devices: %s (%s)", err.kind, err)

    def commands(self, device: DeviceRecord) -> list[CommandSpec]:
        return entity_commands(device)

    def run(self, device: DeviceRecord, command: str, value: Value) -> Outcome:
        entity_id = device.settings.get("entity_id")
        if not isinstance(entity_id, str) or not ENTITY_ID.fullmatch(entity_id):
            return Outcome.fail(
                "needs_setup", f"Jarvis lost track of {_the(device)}; ask me to list the devices again."
            )
        action = plan_action(device, command, value)
        if isinstance(action, Outcome):
            return action
        hub: HubSettings | None = None
        try:
            hub = self.settings()
            client = self._client(hub)
            before = client.state(entity_id)
            if before is None:
                return self._gone(device)
            if before.get("state") == "unavailable":
                # Home Assistant would skip it and still answer 200.
                return Outcome.fail("unreachable", f"Home Assistant says {_the(device)} is unavailable right now.")
            data = {"entity_id": entity_id, **action.data}
            try:
                changed = client.call_service(str(device.settings.get("domain")), action.service, data)
            except HaError as err:
                err.during = "service"
                raise
        except HaError as err:
            return self._failure(hub, err, device, action, command)
        return self._confirm(client, device, action, before, changed)

    def status(self, device: DeviceRecord) -> Outcome:
        entity_id = device.settings.get("entity_id")
        if not isinstance(entity_id, str) or not ENTITY_ID.fullmatch(entity_id):
            return Outcome.fail(
                "needs_setup", f"Jarvis lost track of {_the(device)}; ask me to list the devices again."
            )
        hub: HubSettings | None = None
        try:
            hub = self.settings()
            state = self._client(hub).state(entity_id)
        except HaError as err:
            return self._failure(hub, err, device)
        if state is None:
            return self._gone(device)
        return Outcome.done(state_words(device, state))

    def describe(self) -> str | None:
        try:
            devices = self.hub_devices()
        except HaError as err:
            return hub_problem(err)
        except Exception as exc:
            log.warning("describing Home Assistant failed: %s", type(exc).__name__, exc_info=True)
            return "Home Assistant could not be read; the helper's log has the details."
        listing = self._listing
        exposed = " exposed to Assist" if listing is not None and listing.exposed_only else ""
        count = len(devices)
        return f"Home Assistant connected ({count} device{'s' if count != 1 else ''}{exposed})"

    def close(self) -> None:
        self._listing = None
        self._failed = None

    # ------------------------------------------------------------------ plumbing

    def settings(self) -> HubSettings:
        """The saved hub settings and token; HaError("setup") when something is missing."""
        try:
            saved = self.ctx.store.load().hubs.get(DRIVER) or {}
            secret = self.ctx.store.secret(SECRET_KEY) or {}
        except StoreError:
            raise HaError(
                "setup",
                "the store could not be read",
                spoken="Jarvis could not read the Home Assistant settings; connect it again in home setup.",
            ) from None
        return hub_from_saved(saved, secret)

    def fetch(self, hub: HubSettings) -> list[DeviceRecord]:
        """The hub's devices, read now: the states over REST, then who, what is exposed and where over the WebSocket."""
        if _refused_recently(hub.fingerprint):
            raise HaError("auth", "the token was refused before")
        client = HaClient(hub, budget=LIST_BUDGET_S)
        try:
            states = client.states()
        except HaError as err:
            if err.kind == "auth":
                _remember_refusal(hub.fingerprint)
            raise
        view = read_view(hub, want_exposed=hub.exposed_only)
        if hub.exposed_only:
            # The user chose to limit Jarvis to what Assist sees: without that list, show nothing rather than all.
            if view.is_admin is False:
                raise HaError("exposure", "the token's user is not an administrator", spoken=NOT_ADMIN_ANY_MORE)
            if view.exposed is None:
                raise HaError("exposure", "the Assist list could not be read", spoken=EXPOSURE_UNREADABLE)
        devices: list[DeviceRecord] = []
        for state in states:
            entity_id = state.get("entity_id")
            if not isinstance(entity_id, str):
                continue
            if hub.exposed_only:
                if view.exposed is None or entity_id not in view.exposed:
                    continue
            elif entity_id in view.hidden:
                continue
            record = entity_record(state, view.rooms.get(entity_id))
            if record is not None:
                devices.append(record)
        devices.sort(key=lambda d: (normalize(d.name), d.id))
        return devices

    def _client(self, hub: HubSettings) -> HaClient:
        if _refused_recently(hub.fingerprint):
            raise HaError("auth", "the token was refused before")
        budget = max(1.0, min(CALL_BUDGET_S, self.ctx.call_timeout - CALL_MARGIN_S))
        return HaClient(hub, budget=budget)

    def _failure(
        self,
        hub: HubSettings | None,
        err: HaError,
        device: DeviceRecord,
        action: Action | None = None,
        command: str = "",
    ) -> Outcome:
        log.debug("%s: %s (%s)", device.id, err.kind, err)
        if err.kind == "auth" and hub is not None:
            _remember_refusal(hub.fingerprint)
            self._listing = None
        return failure_outcome(err, device, action, command=command, token=hub.token if hub else None)

    def _gone(self, device: DeviceRecord) -> Outcome:
        self._listing = None  # the next list reads the entities again
        return Outcome.fail(
            "not_found", f"Home Assistant no longer has {_the(device)}; ask me to list the devices again."
        )

    def _confirm(
        self,
        client: HaClient,
        device: DeviceRecord,
        action: Action,
        before: dict[str, Any],
        changed: list[dict[str, Any]],
    ) -> Outcome:
        """Says what came of the call: the new state when it changed, else what Jarvis asked for."""
        if action.done is not None:
            return Outcome.done(action.done)
        entity_id = device.settings.get("entity_id")
        before_words = state_words(device, before)
        after = next((state for state in changed if state.get("entity_id") == entity_id), None)
        try:
            if after is None:
                after = client.state(str(entity_id))
                if (
                    after is not None
                    and state_words(device, after) == before_words
                    and client.remaining() > self.recheck_wait_s + 1.0
                ):
                    # Some integrations report the new state a moment after the call returns.
                    time.sleep(self.recheck_wait_s)
                    after = client.state(str(entity_id))
        except HaError as err:
            log.debug("checking %s after the call: %s", entity_id, err.kind)
        if after is not None:
            words = state_words(device, after)
            if words != before_words or (action.expect is not None and after.get("state") == action.expect):
                return Outcome.done(words)
        return Outcome.done(f"Asked Home Assistant to {action.phrase}.")


# --------------------------------------------------------------------------- setup


TOKEN_STEPS = """\
Jarvis needs a long-lived access token from Home Assistant:
  1. Open Home Assistant in your browser and select your name at the bottom left (your profile).
  2. Open the Security tab. Under Long-lived access tokens, select Create token.
  3. Name it Jarvis and copy the token: Home Assistant shows it only once.
The token acts as that Home Assistant user. Jarvis keeps it in its credential store."""

EXPOSE_HINT = (
    "You choose those in Home Assistant under Settings > Voice assistants > Expose. "
    "Jarvis picks up changes within a minute."
)
SELF_SIGNED = (
    "Home Assistant answered, but this computer cannot check its certificate. "
    "That is normal when Home Assistant uses a certificate of its own (self-signed)."
)


def clean_url(text: str) -> str | None:
    """``http://host:8123`` from what the user typed: a bare host gets http:// and :8123, and a path is dropped."""
    text = text.strip()
    if not text or any(c.isspace() for c in text):
        return None
    bare = "://" not in text
    if bare and ":" in text and net.is_ip_address(text):
        text = f"[{text}]"  # a bare IPv6 address
    try:
        parts = urlsplit(f"http://{text}" if bare else text)
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not host or parts.username or parts.password:
        return None
    if not re.fullmatch(r"[A-Za-z0-9.\-:]+", host):
        return None
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc += f":{port}"
    elif bare:
        netloc += f":{DEFAULT_PORT}"
    return f"{parts.scheme}://{netloc}"


def address_candidates(typed: str) -> list[str]:
    """Where to look for what the user typed: a bare host is tried on 8123, then on 80 (some HAOS installs)."""
    url = clean_url(typed)
    if url is None:
        return []
    text = typed.strip()
    if "://" in text:
        return [url]
    try:
        port = urlsplit(f"http://{text}").port
    except ValueError:
        return [url]
    return [url] if port is not None else [url, url.removesuffix(f":{DEFAULT_PORT}")]


def reach(url: str, verify_tls: bool) -> str | None:
    """None when something answers HTTP at ``url`` (no token is sent: a 401 would count as a failed login),
    else why not: unreachable, timeout, tls or bad_url."""
    try:
        net.request("GET", f"{url}/manifest.json", timeout=PROBE_TIMEOUT_S, verify_tls=verify_tls)
    except net.HttpError as exc:
        return exc.kind
    return None


def _address_problem(kind: str | None, url: str) -> str:
    if kind == "tls":
        return f"Could not make a secure connection to {url}."
    if kind == "bad_url":
        return "That does not look like an address. Type something like http://homeassistant.local:8123."
    return (
        f"No answer from {url}. Check the address (with https:// if you open it that way), "
        "that Home Assistant is running, and that this computer is on the same network."
    )


def check_token(hub: HubSettings) -> tuple[str | None, bool]:
    """(None, False) when the address and token work; else (what is wrong, whether it is the token)."""
    try:
        HaClient(hub, budget=PROBE_TIMEOUT_S + 1.0).api_running()
    except HaError as err:
        if err.kind == "auth":
            return (
                "Home Assistant did not accept that token. Copy all of it, or make a new one. "
                "(Home Assistant shows each refused try as a failed login; you can dismiss those notices.)",
                True,
            )
        if err.kind == "forbidden":
            return (
                "Home Assistant refused this computer: it may have blocked it after failed logins. Remove its "
                "address from ip_bans.yaml in Home Assistant's configuration folder, restart Home Assistant, "
                "and try again.",
                False,
            )
        if err.kind == "bad_reply" or (err.kind == "http" and err.status == 404):
            return f"Something answered at {hub.url}, but not Home Assistant's API. Check the address and port.", False
        if err.kind in ("unreachable", "timeout", "budget", "tls"):
            return f"Home Assistant stopped answering at {hub.url}. Check that it is running, then try again.", False
        return hub_problem(err), False
    return None, False


def is_admin(hub: HubSettings) -> bool | None:
    """Whether the token's user is an administrator (``auth/current_user``); None when the WebSocket fails."""
    try:
        with HaSocket(hub) as ws:
            user = ws.ask("auth/current_user")
    except HaError as err:
        log.info("Home Assistant's WebSocket API: %s (%s)", err.kind, err)
        return None
    return isinstance(user, dict) and user.get("is_admin") is True


def preview(devices: list[DeviceRecord], exposed_only: bool) -> str:
    if not devices:
        text = "Jarvis sees no devices it can control in Home Assistant yet."
        return f"{text} Expose some to Assist: {EXPOSE_HINT}" if exposed_only else text
    shown = [f"{d.name} ({d.room})" if d.room else d.name for d in devices[:PREVIEW_NAMES]]
    more = f", and {len(devices) - PREVIEW_NAMES} more" if len(devices) > PREVIEW_NAMES else ""
    count = len(devices)
    return f"Jarvis will see {count} device{'s' if count != 1 else ''}: {', '.join(shown)}{more}."


def _ask_address(ui: Prompter, saved: dict[str, Any]) -> tuple[str, bool] | None:
    saved_url = clean_url(str(saved.get("url") or ""))
    default = saved_url or DEFAULT_URL
    for _ in range(MAX_SETUP_TRIES):
        typed = ui.ask("Home Assistant's address, as you open it in your browser", default)
        if not typed.strip():
            return None
        candidates = address_candidates(typed)
        if not candidates:
            ui.say("That does not look like an address. Type something like http://homeassistant.local:8123.")
            continue
        default = candidates[0]
        verify = not (saved.get("verify_tls") is False and saved_url == candidates[0])
        ui.say(f"Looking for Home Assistant at {candidates[0]}...")
        problem: str | None = "unreachable"
        for url in candidates:
            problem = reach(url, verify)
            if problem == "tls" and verify and url.startswith("https://"):
                ui.say(SELF_SIGNED)
                if not ui.confirm(
                    "Skip the certificate check for this address? Do this only for your own Home Assistant "
                    "on your home network: anyone who can tamper with your network could then read the token.",
                    default=False,
                ):
                    problem = "declined"
                    break
                verify = False
                problem = reach(url, verify)
            if problem is None:
                if url != candidates[0]:
                    ui.say(f"Found it at {url}.")
                return url, verify
        if problem == "declined":
            ui.say("Then Jarvis cannot connect to that address.")
        else:
            ui.say(_address_problem(problem, candidates[0]))
        if not ui.confirm("Try another address?", default=True):
            return None
    return None


def _ask_token(ui: Prompter, url: str, verify: bool, kept: str | None) -> str | None:
    if kept is not None and ui.confirm("Keep the token Jarvis already has for it?", default=True):
        problem, token_trouble = check_token(HubSettings(url, kept, verify_tls=verify))
        if problem is None:
            return kept
        ui.say(problem)
        if not token_trouble:
            return None
    ui.say(TOKEN_STEPS)
    for _ in range(MAX_TOKEN_TRIES):
        token = ui.ask_secret("Paste the token").strip()
        if not token:
            return None
        if not usable_token(token):
            ui.say(
                "That does not look like a Home Assistant token: it is one long line of letters, digits, "
                "dots, dashes and underscores. Paste it again."
            )
            continue
        ui.say("Checking the token...")
        problem, token_trouble = check_token(HubSettings(url, token, verify_tls=verify))
        if problem is None:
            return token
        ui.say(problem)
        if not token_trouble or not ui.confirm("Try another token?", default=True):
            return None
    return None


def _set_hub(config: HomeConfig, settings: dict[str, Any]) -> None:
    config.hubs[DRIVER] = settings


def _save(ctx: DriverContext, hub: HubSettings) -> None:
    """The token, then the settings; if anything stops in between, the token goes back to what it was."""
    previous = ctx.store.secret(SECRET_KEY)
    saved = False
    try:
        ctx.store.set_secret(SECRET_KEY, {"token": hub.token})
        ctx.store.update(lambda config: _set_hub(config, hub.to_json()))
        saved = True
    finally:
        if not saved:
            try:
                ctx.store.set_secret(SECRET_KEY, previous)
            except (StoreError, OSError):
                log.warning("could not undo the half-saved Home Assistant token")


def _disconnect(ui: Prompter, ctx: DriverContext) -> None:
    if not ui.confirm("Disconnect Home Assistant? Jarvis forgets its address and token.", default=False):
        ui.say("Nothing was changed.")
        return
    # The settings first: a token left without settings is never used.
    ctx.store.update(lambda config: config.hubs.pop(DRIVER, None))
    ctx.store.set_secret(SECRET_KEY, None)
    ui.say(
        "Home Assistant is disconnected. The token still works until you delete it in Home Assistant "
        "(your profile > Security)."
    )


def wizard(ui: Prompter, ctx: DriverContext) -> None:
    """Connects Home Assistant (its address, a token, which devices Jarvis sees), or changes or removes it."""
    ui.say("Connect Home Assistant")
    saved = dict(ctx.store.load().hubs.get(DRIVER) or {})
    if saved:
        where = clean_url(str(saved.get("url") or "")) or "an unknown address"
        index = ui.choose(
            f"Home Assistant is connected at {where}.",
            ["Change its address, token or which devices Jarvis sees", "Disconnect it"],
        )
        if index is None:
            return
        if index == 1:
            _disconnect(ui, ctx)
            return
    address = _ask_address(ui, saved)
    if address is None:
        ui.say(NOTHING_SAVED)
        return
    url, verify = address
    kept: str | None = None
    if saved and clean_url(str(saved.get("url") or "")) == url:
        try:
            token = (ctx.store.secret(SECRET_KEY) or {}).get("token")
        except StoreError:
            token = None
        kept = token if isinstance(token, str) and usable_token(token) else None
    token = _ask_token(ui, url, verify, kept)
    if token is None:
        ui.say(NOTHING_SAVED)
        return
    admin = is_admin(HubSettings(url, token, verify_tls=verify))
    exposed_only = False
    if admin:
        ui.say("This token belongs to a Home Assistant administrator.")
        exposed_only = ui.confirm(
            "Let Jarvis use only the devices exposed to Assist? (Recommended.)",
            default=saved.get("exposed_only") is not False,
        )
        if exposed_only:
            ui.say(EXPOSE_HINT)
    elif admin is False:
        ui.say(
            "This token belongs to a user who is not an administrator. Home Assistant lets only administrators "
            "read which devices are exposed to Assist, so Jarvis will see every device that user can control."
        )
    else:
        ui.say(
            "Jarvis could not open Home Assistant's live connection (its WebSocket API), so it cannot tell the "
            "rooms or whether the token belongs to an administrator. It will see every device the token can control."
        )
    hub = HubSettings(url, token, verify_tls=verify, admin=admin is True, exposed_only=exposed_only)
    ui.say("Reading the devices...")
    try:
        devices = HomeAssistantDriver(ctx).fetch(hub)
    except HaError as err:
        ui.say(hub_problem(err))
        if not ui.confirm("Save these settings anyway?", default=False):
            ui.say(NOTHING_SAVED)
            return
    else:
        ui.say(preview(devices, exposed_only))
    _save(ctx, hub)
    ui.say(
        "Saved. Jarvis can use Home Assistant now. Unlocking, disarming an alarm and opening a garage door "
        "or gate always ask you on screen first."
    )
