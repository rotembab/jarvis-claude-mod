"""Sony Bravia TVs (Android TV and Google TV) over the home network.

The TV offers two interfaces, both over HTTP with the pre-shared key in an
``X-Auth-PSK`` header (HTTPS on TVs made since August 2025, whose plain HTTP
only ever answers with an error):

- the Scalar REST API, JSON-RPC at ``/sony/<service>``: power, volume, apps,
  inputs and what is on;
- IRCC-IP, remote-control buttons as a SOAP call to ``/sony/ircc`` (some
  models only answer ``/sony/IRCC``: the path is case-sensitive).

The quirks follow pybravia (MIT), the library Home Assistant uses: the IRCC
path case swap, an empty IRCC request before the first button after ten idle
minutes (some TVs silently drop it otherwise), 401/403 as a refused key, and
404 as the TV's WebApiCore service restarting (it does that by itself for
about 30 s). The client is synchronous and stdlib-only on ``net.request``,
which keeps the header names exactly as written.

Nothing here polls: the TV is asked only when Jarvis is asked, because
polling keeps its network awake in standby.
"""

from __future__ import annotations

import contextlib
import difflib
import functools
import html
import ipaddress
import logging
import re
import select
import socket
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar
from urllib.parse import urlsplit
from xml.sax.saxutils import escape as xml_escape

from . import net
from .base import Driver, DriverContext, Prompter
from .model import CommandSpec, DeviceRecord, HomeConfig, Outcome, Value, normalize, unique_id
from .store import StoreError

log = logging.getLogger(__name__)

T = TypeVar("T")

# Timings. A driver call must end within the service's limit (20 s), so each
# request's timeout is also clamped to what is left of the call's budget.
CALL_BUDGET_S = 17.0
CALL_MARGIN_S = 1.5  # kept free below ctx.call_timeout
TURN_ON_BUDGET_S = 15.0
TURN_ON_RESERVE_S = 3.5  # left after polling, for setPowerStatus and the WakeUp button
PROBE_BUDGET_S = 20.0
# pybravia's default: a Google TV can take several seconds to list its apps or to start one.
REST_TIMEOUT_S = 10.0
STATUS_TIMEOUT_S = 3.0
POLL_TIMEOUT_S = 2.0
POLL_GAP_S = 0.5
KEY_TIMEOUT_S = 3.0
WAKE_TIMEOUT_S = 2.0
RETRY_WAIT_S = 2.0
KEY_GAP_S = 0.25  # Sony asks for about 200 ms between button presses
MIN_REQUEST_S = 0.2
IDLE_WAKE_S = 600.0
VOLUME_STEP = 2
MAX_KEY_PRESSES = 25
LIST_MAX = 60
DISCOVERY_S = 3.0
MAX_SETUP_TRIES = 5

IRCC_PATHS = ("/sony/ircc", "/sony/IRCC")
SOAP_ACTION = '"urn:schemas-sony-com:service:IRCC:1#X_SendIRCC"'
_ENVELOPE = (
    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
    ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
    "<s:Body>"
    '<u:X_SendIRCC xmlns:u="urn:schemas-sony-com:service:IRCC:1">'
    "<IRCCCode>{code}</IRCCCode>"
    "</u:X_SendIRCC>"
    "</s:Body>"
    "</s:Envelope>"
)
# The remote's power-on button, for TVs whose code list lacks it (pybravia's CODE_POWER_ON).
WAKE_UP_CODE = "AAAAAQAAAAEAAAAuAw=="

# Remote buttons: command -> (the TV's IRCC names to try, in order; the button's spoken name).
# play_pause: the API cannot tell whether anything is playing and Sony's IP
# remote has separate Play and Pause buttons, so it sends Pause (what "pause
# it" means far more often); "play" resumes.
KEYS: dict[str, tuple[tuple[str, ...], str]] = {
    "play": (("Play",), "Play"),
    "pause": (("Pause",), "Pause"),
    "play_pause": (("Pause",), "Pause"),
    "stop": (("Stop",), "Stop"),
    "next": (("Next",), "Next"),
    "previous": (("Prev", "Previous"), "Previous"),
    "up": (("Up",), "Up"),
    "down": (("Down",), "Down"),
    "left": (("Left",), "Left"),
    "right": (("Right",), "Right"),
    "select": (("Confirm", "DpadCenter"), "OK"),
    "back": (("Return", "Back"), "Back"),
    "home": (("Home",), "Home"),
    "menu": (("ActionMenu", "Options"), "Menu"),
}

# JSON-RPC error codes from Sony's tables.
ERR_ILLEGAL_STATE = 7
ERR_NO_METHOD = 12
ERR_VERSION = 14
ERR_UNSUPPORTED = 15
ERR_DISPLAY_OFF = 40005
ERR_TARGET = 40800  # e.g. speaker volume while the sound goes to an external audio system
ERR_VOLUME_RANGE = 40801
ERR_APP_BUSY = 41400
ERR_APP_FAILED = 41401
ERR_APP_UNDECIDED = 41402

# Failures where the TV gave no HTTP answer at all.
GONE = frozenset({"unreachable", "timeout", "refused", "tls"})
# Failures worth one more try after a short wait (WebApiCore restarting).
RETRYABLE = frozenset({"restarting", "refused"})
# What plain http can answer a TV that takes only https: setup tries https after any of these.
HTTPS_HINTS = frozenset({"refused", "auth", "http", "restarting", "bad_reply", "off"})

# audio.getSoundSettings outputTerminal values for a soundbar or receiver over HDMI (eARC).
# There setAudioVolume can do nothing without an error, so the remote's buttons are used.
EXTERNAL_OUTPUTS = frozenset({"audioSystem", "hdmi"})


# --------------------------------------------------------------------------- the client


class BraviaError(Exception):
    """A TV call that did not succeed. The message names the method or host, never the key.

    ``kind``: auth (key refused), unreachable / timeout / refused / tls (no
    HTTP answer), restarting (HTTP 404), http (another status), rpc (a
    JSON-RPC error, see ``code``), off ("not power-on"), bad_reply, budget
    (no time left in this call), bad_key (the saved key cannot be sent).
    ``late``: the TV had already answered an earlier request of this call, so
    a timeout means it is slow, not off.
    """

    def __init__(
        self,
        kind: str,
        message: str = "",
        *,
        code: int | None = None,
        status: int | None = None,
        late: bool = False,
    ) -> None:
        super().__init__(message or kind)
        self.kind = kind
        self.code = code
        self.status = status
        self.late = late


def usable_key(psk: str) -> bool:
    """A key an HTTP header can carry: printable ASCII. Anything else would make
    http.client raise an error that quotes the header value."""
    return 0 < len(psk) <= 128 and psk == psk.strip() and all(32 <= ord(c) < 127 for c in psk)


def _netloc(address: str) -> str:
    """``192.168.1.20``, ``tv.lan:8080``, or a bare IPv6 address in brackets."""
    return f"[{address}]" if ":" in address and net.is_ip_address(address) else address


def host_of(address: str) -> str | None:
    """The host part of a saved address (no port), e.g. for the Wake-on-LAN broadcast."""
    try:
        return urlsplit(f"//{_netloc(address)}").hostname
    except ValueError:
        return None


def _failure_kind(exc: net.HttpError) -> str:
    if exc.kind == "timeout":
        return "timeout"
    if exc.kind == "tls":
        return "tls"
    # net.request raises "from None", which keeps the original in __context__.
    if isinstance(exc.__context__, ConnectionRefusedError) or "ConnectionRefusedError" in str(exc):
        return "refused"
    return "unreachable"


class BraviaClient:
    """JSON-RPC and IRCC requests to one TV, all within one time budget."""

    def __init__(
        self,
        address: str,
        psk: str,
        *,
        scheme: str = "http",
        budget: float = CALL_BUDGET_S,
        retry_wait: float = RETRY_WAIT_S,
    ) -> None:
        if not usable_key(psk):
            raise BraviaError("bad_key", "the saved key has characters an HTTP header cannot carry")
        self.address = address
        self.scheme = "https" if scheme == "https" else "http"
        self.base = f"{self.scheme}://{_netloc(address)}"
        # TVs on https use a certificate of their own, which nothing on the PC can check.
        self.verify_tls = False
        self.deadline = time.monotonic() + budget
        self.retry_wait = retry_wait
        self.retry_kinds = RETRYABLE
        self.answered = False  # any HTTP answer yet in this call
        self._psk = psk
        self._id = 0

    def __repr__(self) -> str:
        return f"BraviaClient({self.base})"

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def over_https(self) -> BraviaClient:
        """The same TV and key over https, within what is left of this client's budget."""
        client = BraviaClient(self.address, self._psk, scheme="https", budget=0.0, retry_wait=self.retry_wait)
        client.deadline = self.deadline
        return client

    def call(
        self,
        service: str,
        method: str,
        params: list[Any] | None = None,
        *,
        version: str = "1.0",
        timeout: float = REST_TIMEOUT_S,
        retry: bool = True,
    ) -> list[Any]:
        """One JSON-RPC call; returns its ``result`` list or raises BraviaError."""
        self._id = self._id % 2_147_483_646 + 1  # 1..2147483647; 0 is reserved
        payload = {"method": method, "params": list(params or []), "id": self._id, "version": version}
        headers = {"Content-Type": "application/json; charset=UTF-8", "Cache-Control": "no-cache"}

        def send() -> list[Any]:
            response = self._post(f"/sony/{service}", headers=headers, json_body=payload, timeout=timeout)
            return _rpc_result(response, method)

        return self.retrying(send) if retry else send()

    def ircc(self, code: str, path: str, *, timeout: float = KEY_TIMEOUT_S) -> None:
        """Presses one remote button (an empty code only wakes the IRCC service)."""
        body = _ENVELOPE.format(code=xml_escape(code)).encode("utf-8")
        headers = {"Content-Type": "text/xml; charset=UTF-8", "SOAPACTION": SOAP_ACTION}
        self._post(path, headers=headers, body=body, timeout=timeout)

    def retrying(self, send: Callable[[], T]) -> T:
        """``send()``, once more after a short wait if the TV's service was restarting."""
        try:
            return send()
        except BraviaError as err:
            if err.kind not in self.retry_kinds or self.remaining() < self.retry_wait + MIN_REQUEST_S:
                raise
            log.debug("Sony TV %s: %s; trying once more", self.base, err.kind)
        time.sleep(self.retry_wait)
        return send()

    def _post(
        self,
        path: str,
        *,
        headers: dict[str, str],
        body: bytes | None = None,
        json_body: Any = None,
        timeout: float,
    ) -> net.HttpResponse:
        remaining = self.remaining()
        if remaining < MIN_REQUEST_S:
            raise BraviaError("budget", "no time left for another request")
        sent = {**headers, "X-Auth-PSK": self._psk}
        try:
            response = net.request(
                "POST",
                self.base + path,
                headers=sent,
                body=body,
                json_body=json_body,
                timeout=min(timeout, remaining),
                verify_tls=self.verify_tls,
            )
        except net.HttpError as exc:
            raise BraviaError(_failure_kind(exc), str(exc), late=self.answered) from None
        self.answered = True
        if response.status in (401, 403):
            raise BraviaError("auth", f"{path}: HTTP {response.status}", status=response.status)
        if response.status == 404:
            raise BraviaError("restarting", f"{path}: HTTP 404", status=404)
        if response.status != 200:
            raise BraviaError("http", f"{path}: HTTP {response.status}", status=response.status)
        return response


def _error_parts(error: Any) -> tuple[int | None, str]:
    """``[code, "message"]`` (Sony's shape), or a dict with code and message."""
    if isinstance(error, dict):
        raw_code, message = error.get("code"), error.get("message")
    elif isinstance(error, list | tuple):
        raw_code = error[0] if error else None
        message = error[1] if len(error) > 1 else ""
    else:
        raw_code, message = error, ""
    try:
        code = int(raw_code) if raw_code is not None and not isinstance(raw_code, bool) else None
    except (TypeError, ValueError):
        code = None
    return code, str(message or "")


def _rpc_result(response: net.HttpResponse, method: str) -> list[Any]:
    try:
        data = response.json()
    except ValueError:
        raise BraviaError("bad_reply", f"{method}: the answer is not JSON") from None
    if not isinstance(data, dict):
        raise BraviaError("bad_reply", f"{method}: unexpected answer")
    error = data.get("error")
    if error is not None:
        code, message = _error_parts(error)
        if code in (401, 403):
            raise BraviaError("auth", f"{method}: error {code}", code=code)
        if "not power-on" in message.lower():
            raise BraviaError("off", f"{method}: the TV is not on", code=code)
        raise BraviaError("rpc", f"{method}: error {code} {message}"[:200], code=code)
    result = data.get("result")
    return result if isinstance(result, list) else []


def _first(result: list[Any]) -> dict[str, Any]:
    return result[0] if result and isinstance(result[0], dict) else {}


def _first_list(result: list[Any]) -> list[Any]:
    return result[0] if result and isinstance(result[0], list) else []


def _swap(path: str) -> str:
    return IRCC_PATHS[1] if path == IRCC_PATHS[0] else IRCC_PATHS[0]


def _parse_codes(result: list[Any]) -> dict[str, str]:
    """getRemoteControllerInfo: ``[{bundled, type}, [{name, value}, ...]]`` -> {name: code}."""
    entries = result[1] if len(result) > 1 and isinstance(result[1], list) else []
    codes: dict[str, str] = {}
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("name"), str) and isinstance(entry.get("value"), str):
            codes[entry["name"]] = entry["value"]
    return codes


def _volume_entries(result: list[Any]) -> list[dict[str, Any]]:
    return [
        e
        for e in _first_list(result)
        if isinstance(e, dict) and isinstance(e.get("volume"), int) and not isinstance(e.get("volume"), bool)
    ]


def _speaker(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    for entry in entries:
        if entry.get("target") == "speaker":
            return entry
    return next((e for e in entries if e.get("target") != "headphone"), None)


def _external(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    """A volume entry for an output that is neither the TV's speakers nor headphones."""
    return next((e for e in entries if e.get("target") not in ("speaker", "headphone")), None)


def _range(entry: dict[str, Any] | None) -> tuple[int, int]:
    """The TV's own volume scale (often 0-100, not always)."""
    if entry is None:
        return 0, 100
    low, high = entry.get("minVolume"), entry.get("maxVolume")
    low = low if isinstance(low, int) and not isinstance(low, bool) else 0
    high = high if isinstance(high, int) and not isinstance(high, bool) and high > low else max(low + 1, 100)
    return low, high


def _percent(entry: dict[str, Any]) -> int:
    low, high = _range(entry)
    return max(0, min(100, round((int(entry["volume"]) - low) * 100 / (high - low))))


def _no_speaker(err: BraviaError) -> bool:
    """The TV's speakers cannot take this (the sound goes to an external audio system)."""
    return err.kind == "rpc" and err.code in (ERR_TARGET, ERR_UNSUPPORTED)


def _output_terminal(result: list[Any]) -> str | None:
    """getSoundSettings 1.1 for outputTerminal: ``[[{target, currentValue}]]`` -> currentValue."""
    for entry in _first_list(result):
        if isinstance(entry, dict) and entry.get("target", "outputTerminal") == "outputTerminal":
            value = entry.get("currentValue")
            return value if isinstance(value, str) else None
    return None


def _has_signal(entry: dict[str, Any]) -> bool:
    # getCurrentExternalInputsStatus 1.1 sends "status" as the STRING "true" or "false".
    status = entry.get("status")
    return status is True or (isinstance(status, str) and status.strip().lower() == "true")


def _input_cache(entries: list[dict[str, Any]]) -> list[dict[str, str]]:
    """What the device record keeps about inputs: uri, title and the user's label."""
    cache = []
    for entry in entries:
        item = {"uri": str(entry.get("uri") or ""), "title": str(entry.get("title") or "")}
        label = str(entry.get("label") or "").strip()
        if label:
            item["label"] = label
        if item["uri"]:
            cache.append(item)
    return cache


def _input_name(entry: dict[str, Any]) -> str:
    """ "Apple TV (HDMI 2)" when the user labelled the input, else "HDMI 2"."""
    title = str(entry.get("title") or "").strip()
    label = str(entry.get("label") or "").strip()
    if label and title and normalize(label) != normalize(title):
        return f"{label} ({title})"
    return label or title or str(entry.get("uri") or "an input")


def _hdmi_port(text: str) -> int | None:
    compact = normalize(text).replace(" ", "")
    match = re.fullmatch(r"(?:hdmi|input|port)?(\d{1,2})", compact)
    return int(match.group(1)) if match else None


def _port_of(uri: str) -> int | None:
    match = re.search(r"[?&]port=(\d+)", uri)
    return int(match.group(1)) if match and "hdmi" in uri.lower() else None


def _best(names: list[str], wanted: str) -> list[int]:
    """Indices of the names that best match ``wanted``: exact, then prefix, then
    the words or letters contained, then a close spelling."""
    target = normalize(wanted)
    if not target:
        return []
    plain = [normalize(n) for n in names]
    compact_target = target.replace(" ", "")
    steps: list[Callable[[str], bool]] = [
        lambda n: n == target or n.replace(" ", "") == compact_target,
        lambda n: n.startswith(target),
        lambda n: set(target.split()) <= set(n.split()) or target in n,
        lambda n: bool(n) and (n in target or n.replace(" ", "") in compact_target),
    ]
    for matches in steps:
        found = [i for i, n in enumerate(plain) if matches(n)]
        if found:
            return found
    close = set(difflib.get_close_matches(target, plain, n=3, cutoff=0.75))
    return [i for i, n in enumerate(plain) if n in close]


def _match_input(entries: list[dict[str, Any]], wanted: str) -> list[dict[str, Any]]:
    names: list[str] = []
    owners: list[int] = []
    for i, entry in enumerate(entries):
        for name in (entry.get("label"), entry.get("title")):
            if isinstance(name, str) and name.strip():
                names.append(name)
                owners.append(i)
    chosen = sorted({owners[i] for i in _best(names, wanted)})
    if not chosen:
        port = _hdmi_port(wanted)
        chosen = [i for i, e in enumerate(entries) if port is not None and _port_of(str(e.get("uri", ""))) == port]
    return [entries[i] for i in chosen]


# --------------------------------------------------------------------------- the driver


@dataclass
class _TvState:
    """What the driver remembers about one TV between calls, in memory only."""

    last_ircc: float | None = None


class _Session:
    """One driver call to one TV: the client, the device record and its saved settings."""

    def __init__(self, driver: BraviaDriver, device: DeviceRecord, client: BraviaClient) -> None:
        self.driver = driver
        self.device = device
        self.client = client
        self.name = device.name
        self.state = driver._state(device.id)
        self.mac = net.normalize_mac(str(device.settings.get("mac") or ""))
        self.host = host_of(str(device.settings.get("address") or ""))
        self._codes_fetched = False
        self._output: str | None = None
        self._output_asked = False

    def remember(self, **changes: Any) -> None:
        self.driver._remember(self.device, changes)

    # -- REST

    def power(self, timeout: float = STATUS_TIMEOUT_S, *, retry: bool = True) -> str:
        """``active`` or ``standby`` (or whatever else the TV says)."""
        status = _first(self.client.call("system", "getPowerStatus", timeout=timeout, retry=retry)).get("status")
        return status if isinstance(status, str) else "unknown"

    def volume_entries(self) -> list[dict[str, Any]]:
        try:
            return _volume_entries(self.client.call("audio", "getVolumeInformation", timeout=STATUS_TIMEOUT_S))
        except BraviaError as err:
            if err.kind in ("rpc", "bad_reply", "off"):
                return []
            raise

    def external_sound(self) -> bool:
        """Whether the sound goes to a soundbar or receiver over HDMI. Asked once per
        call, since it can change between calls; False when the TV cannot say."""
        if not self._output_asked:
            self._output_asked = True
            try:
                result = self.client.call(
                    "audio", "getSoundSettings", [{"target": "outputTerminal"}], version="1.1", timeout=STATUS_TIMEOUT_S
                )
                self._output = _output_terminal(result)
            except BraviaError as err:
                if err.kind not in ("rpc", "bad_reply", "off"):
                    raise
        return self._output in EXTERNAL_OUTPUTS

    def apps(self) -> list[tuple[str, str]]:
        """(title, uri) of each app, in the TV's order, without repeats."""
        seen: set[str] = set()
        apps = []
        for entry in _first_list(self.client.call("appControl", "getApplicationList")):
            if not isinstance(entry, dict):
                continue
            title, uri = entry.get("title"), entry.get("uri")
            if isinstance(title, str) and title.strip() and isinstance(uri, str) and uri and title not in seen:
                seen.add(title)
                apps.append((title.strip(), uri))
        return apps

    def inputs(self) -> list[dict[str, Any]]:
        """The external inputs, and the saved copy refreshed (labels can change on the TV)."""
        try:
            result = self.client.call("avContent", "getCurrentExternalInputsStatus", version="1.1")
        except BraviaError as err:
            if err.kind != "rpc" or err.code not in (ERR_NO_METHOD, ERR_VERSION, ERR_UNSUPPORTED):
                raise
            result = self.client.call("avContent", "getCurrentExternalInputsStatus", version="1.0")
        entries = [e for e in _first_list(result) if isinstance(e, dict) and isinstance(e.get("uri"), str)]
        if entries:
            self.remember(inputs=_input_cache(entries))
        return entries

    # -- IRCC

    @property
    def ircc_path(self) -> str:
        path = self.device.settings.get("ircc_path")
        return path if path in IRCC_PATHS else IRCC_PATHS[0]

    def codes(self, *, refresh: bool = False) -> dict[str, str]:
        saved = self.device.settings.get("ircc_codes")
        saved = saved if isinstance(saved, dict) else {}
        if saved and not refresh:
            return saved
        self._codes_fetched = True
        try:
            fetched = _parse_codes(self.client.call("system", "getRemoteControllerInfo"))
        except BraviaError as err:
            if err.kind in ("rpc", "bad_reply"):
                return saved
            raise
        if fetched:
            self.remember(ircc_codes=fetched)
        return fetched or saved

    def code_for(self, names: tuple[str, ...]) -> str | None:
        """The button's code, fetching the TV's list again when the saved one lacks it."""
        codes = self.codes()
        found = next((codes[n] for n in names if n in codes), None)
        if found is None and not self._codes_fetched:
            codes = self.codes(refresh=True)
            found = next((codes[n] for n in names if n in codes), None)
        return found

    def press(self, names: tuple[str, ...], *, times: int = 1, gap: float = KEY_GAP_S) -> bool:
        """Presses a button ``times`` times; False when the TV's remote has no such button."""
        code = self.code_for(names)
        if code is None:
            return False
        for i in range(times):
            if i:
                time.sleep(gap)
            self.send_code(code)
        return True

    def send_code(self, code: str) -> None:
        self._wake_if_idle()

        def attempt() -> None:
            path = self.ircc_path
            try:
                self.client.ircc(code, path)
            except BraviaError as err:
                if err.kind != "restarting":
                    raise
                # 404: this TV may want the other spelling of the path.
                other = _swap(path)
                self.client.ircc(code, other)
                self.remember(ircc_path=other)

        self.client.retrying(attempt)
        self.state.last_ircc = time.monotonic()

    def _wake_if_idle(self) -> None:
        """After ten idle minutes some TVs drop the first button without an error,
        so an empty request goes first (it also finds the path's spelling)."""
        last = self.state.last_ircc
        if last is not None and time.monotonic() - last <= IDLE_WAKE_S:
            return
        path = self.ircc_path
        for candidate in (path, _swap(path)):
            try:
                self.client.ircc("", candidate, timeout=WAKE_TIMEOUT_S)
            except BraviaError as err:
                if err.kind in ("unreachable", "timeout", "tls", "budget"):
                    raise  # the button itself would only wait as long again
                if err.kind == "restarting":
                    continue
                if err.kind == "refused":
                    return  # the button's own request retries this
                # Any other answer (a SOAP fault for the empty code, say) means the path exists.
            if candidate != path:
                self.remember(ircc_path=candidate)
            return


class BraviaDriver(Driver):
    name = "bravia"
    label = "Sony Bravia"

    # Timings, as attributes so tests can shorten them.
    retry_wait_s = RETRY_WAIT_S
    poll_gap_s = POLL_GAP_S
    key_gap_s = KEY_GAP_S
    turn_on_budget_s = TURN_ON_BUDGET_S

    def __init__(self, ctx: DriverContext) -> None:
        super().__init__(ctx)
        self._lock = threading.Lock()
        self._states: dict[str, _TvState] = {}
        self._handlers: dict[str, Callable[[_Session, Value], Outcome]] = {
            "turn_on": self._turn_on,
            "turn_off": self._turn_off,
            "toggle": self._toggle,
            "volume_up": functools.partial(self._volume_step, up=True),
            "volume_down": functools.partial(self._volume_step, up=False),
            "set_volume": self._set_volume,
            "mute": functools.partial(self._mute, on=True),
            "unmute": functools.partial(self._mute, on=False),
            "launch_app": self._launch_app,
            "list_apps": self._list_apps,
            "set_input": self._set_input,
            "list_inputs": self._list_inputs,
        }
        for command in KEYS:
            self._handlers[command] = functools.partial(self._key, command)

    # ------------------------------------------------------------------ Driver

    def commands(self, device: DeviceRecord) -> list[CommandSpec]:
        saved = device.settings.get("ircc_codes")
        known = set(saved) if isinstance(saved, dict) and saved else None
        specs = [CommandSpec(name) for name in ("turn_on", "turn_off", "toggle", "volume_up", "volume_down")]
        specs += [CommandSpec("set_volume", "percent"), CommandSpec("mute"), CommandSpec("unmute")]
        # Buttons the TV's remote lacks are left out; before the list is known, all are offered.
        specs += [
            CommandSpec(command) for command, (names, _) in KEYS.items() if known is None or known.intersection(names)
        ]
        specs += [
            CommandSpec("launch_app", "text", hint="an app name"),
            CommandSpec("list_apps"),
            CommandSpec("set_input", "text", hint=_input_hint(device)),
            CommandSpec("list_inputs"),
        ]
        return specs

    def run(self, device: DeviceRecord, command: str, value: Value) -> Outcome:
        handler = self._handlers.get(command)
        if handler is None:
            return Outcome.fail("unsupported", f"The {device.name} can't {command.replace('_', ' ')}.")
        budget = self.turn_on_budget_s if command in ("turn_on", "toggle") else CALL_BUDGET_S
        return self._attempt(device, budget, lambda s: handler(s, value))

    def status(self, device: DeviceRecord) -> Outcome:
        return self._attempt(device, CALL_BUDGET_S, self._status, action=False)

    def describe(self) -> str | None:
        try:
            config = self.ctx.store.load()
            missing = [
                d.name
                for d in config.devices
                if d.driver == self.name and not (self.ctx.store.secret(d.secret_key) or {}).get("psk")
            ]
        except StoreError:
            return "Sony Bravia: the saved TV keys could not be read; add the TV again in home setup."
        if missing:
            return f"Sony Bravia: no key saved for {', '.join(missing)}; add it again in home setup."
        return None

    # ------------------------------------------------------------------ plumbing

    def _state(self, device_id: str) -> _TvState:
        with self._lock:
            return self._states.setdefault(device_id, _TvState())

    def _open(self, device: DeviceRecord, budget: float) -> _Session | Outcome:
        address = device.settings.get("address")
        if not isinstance(address, str) or not address.strip():
            return Outcome.fail("needs_setup", f"The {device.name} has no address saved; add it again in home setup.")
        try:
            secret = self.ctx.store.secret(device.secret_key) or {}
        except StoreError:
            return Outcome.fail(
                "needs_setup", f"Jarvis could not read the {device.name}'s key; add it again in home setup."
            )
        psk = secret.get("psk")
        if not isinstance(psk, str) or not psk:
            return Outcome.fail(
                "needs_setup", f"The {device.name} has no pre-shared key saved; add it again in home setup."
            )
        budget = max(1.0, min(budget, self.ctx.call_timeout - CALL_MARGIN_S))
        scheme = "https" if device.settings.get("scheme") == "https" else "http"
        try:
            client = BraviaClient(address.strip(), psk, scheme=scheme, budget=budget, retry_wait=self.retry_wait_s)
        except BraviaError as err:
            return self._failure(device, err)
        return _Session(self, device, client)

    def _attempt(
        self, device: DeviceRecord, budget: float, work: Callable[[_Session], Outcome], *, action: bool = True
    ) -> Outcome:
        """``work`` on a new session, and once more over https when a TV saved on http
        refuses the key: it may now take only https."""
        session = self._open(device, budget)
        if isinstance(session, Outcome):
            return session
        try:
            return work(session)
        except BraviaError as err:
            if err.kind != "auth" or session.client.scheme != "http":
                return self._failure(device, err, action=action)
        secure = self._over_https(session)
        if secure is None:
            return Outcome.fail(
                "auth",
                f"The {device.name} refused the pre-shared key, or now needs a secure connection; "
                "add it again in home setup.",
            )
        try:
            return work(secure)
        except BraviaError as err:
            return self._failure(device, err, action=action)

    def _over_https(self, s: _Session) -> _Session | None:
        """One getSystemInformation over https, in what is left of the call's time. TVs made
        since August 2025 answer plain http only with an error, and an older one can be
        switched to https. When it answers, the TV is saved as https from now on."""
        client = s.client.over_https()
        try:
            info = _first(client.call("system", "getSystemInformation", timeout=STATUS_TIMEOUT_S, retry=False))
        except BraviaError as err:
            log.debug("%s: no answer over https either (%s)", s.device.id, err.kind)
            return None
        if not info:
            return None
        log.info("%s: the TV now answers over https; saved", s.device.id)
        self._remember(s.device, {"scheme": "https"})
        return _Session(self, s.device, client)

    def _remember(self, device: DeviceRecord, changes: dict[str, Any]) -> None:
        """Saves what the TV taught us (the IRCC path, its buttons, input labels) in its record."""
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
            log.warning("could not save what the %s reported: %s", device.id, type(exc).__name__)

    def _failure(self, device: DeviceRecord, err: BraviaError, *, action: bool = True) -> Outcome:
        log.debug("%s: %s (%s)", device.id, err.kind, err)
        name, kind = device.name, err.kind
        if kind == "auth":
            return Outcome.fail("auth", f"The {name} refused the pre-shared key; re-enter it in home setup.")
        if kind == "timeout" and err.late:
            # It answered a moment ago, so it is on and slow (starting an app, say), not off.
            return Outcome.fail(
                "timeout", f"The {name} did not answer in time" + ("; it may still act on it." if action else ".")
            )
        if kind in ("unreachable", "timeout", "refused"):
            return Outcome.fail("unreachable", f"The {name} is off or unreachable.")
        if kind == "restarting":
            return Outcome.fail(
                "unreachable", f"The {name}'s control service is restarting; try again in half a minute."
            )
        if kind == "tls":
            return Outcome.fail("unreachable", f"The secure connection to the {name} failed; set it up again.")
        if kind == "off":
            return Outcome.fail("failed", f"The {name} is off; turn it on first.")
        if kind == "budget":
            return Outcome.fail("timeout", f"The {name} did not answer in time; it may still act on it.")
        if kind == "bad_key":
            return Outcome.fail("needs_setup", f"The {name}'s saved key cannot be used; enter it again in home setup.")
        if kind == "bad_reply":
            return Outcome.fail("failed", f"The {name} gave an answer Jarvis could not read.")
        if kind == "http":
            return Outcome.fail("failed", f"The {name} answered with an error (HTTP {err.status}).")
        if err.code == ERR_DISPLAY_OFF:
            return Outcome.fail("failed", f"The {name}'s screen is off; turn it on first.")
        if err.code == ERR_ILLEGAL_STATE:
            return Outcome.fail("failed", f"The {name} can't do that right now.")
        if err.code in (ERR_NO_METHOD, ERR_VERSION, ERR_UNSUPPORTED):
            return Outcome.fail("unsupported", f"The {name} does not support that.")
        return Outcome.fail("failed", f"The {name} refused that (error {err.code}).")

    # ------------------------------------------------------------------ status

    def _status(self, s: _Session) -> Outcome:
        if s.power() != "active":
            return Outcome.done(f"The {s.name} is off.")
        text = f"The {s.name} is on"
        showing, screen_off = self._on_screen(s)
        if screen_off:
            text += " with the screen off"
        elif showing:
            text += f", showing {showing}"
        speaker = _speaker(s.volume_entries())
        # With a soundbar the speakers' level is not what anyone hears, and the soundbar's is unreliable.
        if speaker is not None and not s.external_sound():
            text += f", volume {_percent(speaker)}%"
            if speaker.get("mute") is True:
                text += ", muted"
        return Outcome.done(text + ".")

    def _on_screen(self, s: _Session) -> tuple[str | None, bool]:
        """What is on (an input's label, a channel), and whether the screen is off.
        The TV cannot say which app is in front: it answers error 7 then."""
        try:
            info = _first(s.client.call("avContent", "getPlayingContentInfo", timeout=STATUS_TIMEOUT_S))
        except BraviaError as err:
            if err.kind == "rpc" and err.code == ERR_DISPLAY_OFF:
                return None, True
            if err.kind in ("rpc", "bad_reply", "off"):
                return None, False
            raise
        uri = str(info.get("uri") or "")
        title = str(info.get("title") or "").strip()
        if uri.startswith("extInput:"):
            saved = s.device.settings.get("inputs")
            known = next((e for e in saved or [] if isinstance(e, dict) and e.get("uri") == uri), None)
            entry = {"title": title, "label": (known or {}).get("label", "")}
            return _input_name(entry) if title or entry["label"] else None, False
        if uri.startswith("tv:"):
            channel = title or str(info.get("dispNum") or "").strip()
            return (f"TV channel {channel}" if channel else "TV"), False
        return title or None, False

    # ------------------------------------------------------------------ power

    def _turn_on(self, s: _Session, _value: Value = None) -> Outcome:
        """Wake-on-LAN, then wait for the API, then setPowerStatus, with the remote's
        WakeUp button as the last resort (after Sony's own procedure)."""
        # Whether the TV may be booting when the polling below runs out: a magic
        # packet went out, or its web server answered while its API restarted.
        # A Google TV can take 15-30 s to answer after a magic packet, longer than a call may last.
        stirring = False
        if s.mac:
            try:
                stirring = net.wake_on_lan(s.mac, s.host) > 0
            except (OSError, ValueError) as exc:
                log.debug("%s: Wake-on-LAN failed: %s", s.device.id, type(exc).__name__)
        reserve = min(TURN_ON_RESERVE_S, (s.client.deadline - time.monotonic()) / 4)
        poll_until = s.client.deadline - reserve
        power: str | None = None
        missed = 0
        while True:
            try:
                power = s.power(POLL_TIMEOUT_S, retry=False)
                break
            except BraviaError as err:
                if err.kind == "auth":
                    raise
                if err.kind not in GONE | {"restarting", "budget"}:
                    power = "unknown"  # it answers, if oddly ("not power-on"): ask it to turn on
                    break
                stirring = stirring or err.kind == "restarting"
            missed += 1
            if time.monotonic() + self.poll_gap_s >= poll_until:
                break
            time.sleep(self.poll_gap_s)
        if power is None and stirring:
            return Outcome.fail(
                "timeout",
                f"The {s.name} has not answered yet; it may still be starting. "
                "If it stays off, check that Remote start is on in the TV's settings.",
            )
        if power is None:
            return Outcome.fail(
                "unreachable", f"The {s.name} did not wake up. Check that Remote start is on in the TV's settings."
            )
        if power == "active":
            # Answering only after the magic packet means it was asleep: the packet turned it on.
            return Outcome.done(f"Turned on the {s.name}." if missed else f"The {s.name} is on.")
        set_ok = False
        try:
            s.client.call("system", "setPowerStatus", [{"status": True}], timeout=KEY_TIMEOUT_S, retry=False)
            set_ok = True
        except BraviaError as err:
            if err.kind == "auth":
                raise
            log.debug("%s: setPowerStatus failed (%s); trying the WakeUp button", s.device.id, err.kind)
        if set_ok and self._became_active(s):
            return Outcome.done(f"Turned on the {s.name}.")
        pressed = False
        try:
            saved = s.device.settings.get("ircc_codes")
            code = saved.get("WakeUp") if isinstance(saved, dict) else None
            s.send_code(code if isinstance(code, str) else WAKE_UP_CODE)
            pressed = True
        except BraviaError as err:
            if err.kind == "auth":
                raise
        if set_ok or pressed:
            return Outcome.done(f"Turned on the {s.name}.")
        return Outcome.fail("failed", f"The {s.name} answered but did not turn on.")

    def _became_active(self, s: _Session, wait: float = 2.0) -> bool:
        until = time.monotonic() + wait
        while True:
            try:
                if s.power(POLL_TIMEOUT_S, retry=False) == "active":
                    return True
            except BraviaError as err:
                if err.kind == "auth":
                    raise
            if time.monotonic() + self.poll_gap_s >= min(until, s.client.deadline - MIN_REQUEST_S):
                return False
            time.sleep(self.poll_gap_s)

    def _turn_off(self, s: _Session, _value: Value = None) -> Outcome:
        if s.power(POLL_TIMEOUT_S) == "standby":
            return Outcome.done(f"The {s.name} is already off.")
        try:
            s.client.call("system", "setPowerStatus", [{"status": False}])
        except BraviaError as err:
            if err.kind == "off" or (err.kind == "rpc" and err.code == ERR_DISPLAY_OFF):
                return Outcome.done(f"The {s.name} is already off.")
            if err.kind not in ("rpc", "http") or not s.press(("PowerOff",)):
                raise
        return Outcome.done(f"Turned off the {s.name}.")

    def _toggle(self, s: _Session, _value: Value = None) -> Outcome:
        try:
            power = s.power(POLL_TIMEOUT_S, retry=False)
        except BraviaError as err:
            if err.kind not in GONE | {"restarting"}:
                raise
            power = "unreachable"  # probably asleep without Remote start: try to wake it
        return self._turn_off(s) if power == "active" else self._turn_on(s)

    # ------------------------------------------------------------------ sound

    def _volume_step(self, s: _Session, _value: Value = None, *, up: bool) -> Outcome:
        direction = "up" if up else "down"
        if not s.external_sound():
            try:
                step = f"{'+' if up else '-'}{VOLUME_STEP}"
                s.client.call("audio", "setAudioVolume", [{"target": "speaker", "volume": step}])
            except BraviaError as err:
                if err.kind == "rpc" and err.code == ERR_VOLUME_RANGE:
                    return Outcome.done(f"The {s.name} is already at its {'loudest' if up else 'quietest'}.")
                if not _no_speaker(err):
                    raise
            else:
                speaker = _speaker(s.volume_entries())
                if speaker is None:
                    return Outcome.done(f"Turned the {s.name} {direction}.")
                return Outcome.done(f"Turned the {s.name} {direction} to {_percent(speaker)}%.")
        # The sound goes to an external audio system: press the remote's volume button instead.
        if not s.press(("VolumeUp",) if up else ("VolumeDown",), times=VOLUME_STEP, gap=self.key_gap_s):
            return Outcome.fail("unsupported", f"The {s.name} can't change its sound system's volume.")
        return Outcome.done(f"Turned the {s.name}'s sound system {direction}.")

    def _set_volume(self, s: _Session, value: Value) -> Outcome:
        percent = _as_percent(value)
        entries = s.volume_entries()
        if s.external_sound():
            return self._set_volume_by_keys(s, percent, entries)
        low, high = _range(_speaker(entries))
        level = low + round(percent * (high - low) / 100)
        try:
            s.client.call("audio", "setAudioVolume", [{"target": "speaker", "volume": str(level)}])
        except BraviaError as err:
            if not _no_speaker(err):
                raise
            return self._set_volume_by_keys(s, percent, entries)
        return Outcome.done(f"Set the {s.name}'s volume to {percent}%.")

    def _set_volume_by_keys(self, s: _Session, percent: int, entries: list[dict[str, Any]]) -> Outcome:
        """The sound goes to an external audio system: step its volume with the remote's
        buttons, which needs its current level; without one only up and down work."""
        other = _external(entries)
        if other is None:
            return Outcome.fail(
                "unsupported", f"The {s.name}'s sound goes to another audio device, so I can only turn it up or down."
            )
        low, high = _range(other)
        steps = low + round(percent * (high - low) / 100) - int(other["volume"])
        if steps and not s.press(
            ("VolumeUp",) if steps > 0 else ("VolumeDown",), times=min(abs(steps), MAX_KEY_PRESSES), gap=self.key_gap_s
        ):
            return Outcome.fail("unsupported", f"The {s.name} can't change its sound system's volume.")
        return Outcome.done(f"Set the {s.name}'s sound system to about {percent}%.")

    def _mute(self, s: _Session, _value: Value = None, *, on: bool) -> Outcome:
        if s.external_sound():
            return self._mute_by_key(s, on)
        try:
            s.client.call("audio", "setAudioMute", [{"status": on}])
        except BraviaError as err:
            if not _no_speaker(err):
                raise
            return self._mute_by_key(s, on)
        return Outcome.done(f"{'Muted' if on else 'Unmuted'} the {s.name}.")

    def _mute_by_key(self, s: _Session, on: bool) -> Outcome:
        """The sound goes to an external audio system. The remote's Mute button
        toggles, so it is pressed only when that system's reported state is not
        the one asked for. The speakers' entry says nothing about it, so without
        the system's own entry the press is a guess, and the answer says so."""
        external = _external(s.volume_entries())
        muted = external.get("mute") if external is not None else None
        if isinstance(muted, bool) and muted == on:
            return Outcome.done(f"The {s.name}'s sound system is {'already' if on else 'not'} muted.")
        if not s.press(("Mute",)):
            return Outcome.fail("unsupported", f"The {s.name} can't mute its sound system.")
        if isinstance(muted, bool):
            return Outcome.done(f"{'Muted' if on else 'Unmuted'} the {s.name}'s sound system.")
        return Outcome.done(f"Pressed Mute on the {s.name}. It can't say whether its sound system is now muted.")

    # ------------------------------------------------------------------ buttons

    def _key(self, command: str, s: _Session, _value: Value = None) -> Outcome:
        names, label = KEYS[command]
        try:
            if not s.press(names):
                return Outcome.fail("unsupported", f"The {s.name}'s remote has no {label} button.")
        except BraviaError as err:
            if err.kind == "http":  # a SOAP fault: the TV did not take that code
                return Outcome.fail("failed", f"The {s.name} did not accept the {label} button.")
            raise
        if command == "play_pause":
            return Outcome.done(f"Pressed Pause on the {s.name}; say play to resume.")
        return Outcome.done(f"Pressed {label} on the {s.name}.")

    # ------------------------------------------------------------------ apps and inputs

    def _launch_app(self, s: _Session, value: Value) -> Outcome:
        wanted = str(value or "").strip()
        apps = s.apps()
        if not apps:
            return Outcome.fail("failed", f"The {s.name} did not list any apps.")
        chosen = [apps[i] for i in _best([title for title, _ in apps], wanted)]
        if not chosen:
            return Outcome.fail(
                "not_found", f'The {s.name} has no app called "{wanted}". Ask me to list its apps to see them.'
            )
        if len(chosen) > 1:
            return Outcome.fail("ambiguous", f"Which app: {', '.join(t for t, _ in chosen[:6])}?")
        title, uri = chosen[0]
        try:
            s.client.call("appControl", "setActiveApp", [{"uri": uri}])
        except BraviaError as err:
            if err.kind != "rpc" or err.code not in (ERR_APP_BUSY, ERR_APP_FAILED, ERR_APP_UNDECIDED):
                raise
            if err.code == ERR_APP_BUSY:
                return Outcome.fail("busy", f"The {s.name} is busy opening another app; try again in a moment.")
            if err.code == ERR_APP_FAILED:
                return Outcome.fail("failed", f"The {s.name} could not open {title}.")
            # 41402: it is starting, the TV just cannot say when it is done.
        return Outcome.done(f"Opened {title} on the {s.name}.")

    def _list_apps(self, s: _Session, _value: Value = None) -> Outcome:
        titles = sorted({title for title, _ in s.apps()}, key=str.casefold)
        if not titles:
            return Outcome.fail("failed", f"The {s.name} did not list any apps.")
        more = len(titles) - LIST_MAX
        tail = f", and {more} more" if more > 0 else ""
        return Outcome.done(f"Apps on the {s.name}: {', '.join(titles[:LIST_MAX])}{tail}.")

    def _set_input(self, s: _Session, value: Value) -> Outcome:
        wanted = str(value or "").strip()
        if wanted.lower().startswith("extinput:"):
            uri, shown = wanted, wanted
        else:
            try:
                entries = s.inputs()
            except BraviaError as err:
                if err.kind != "rpc":
                    raise
                entries = []
            chosen = _match_input(entries, wanted)
            port = _hdmi_port(wanted)
            if not chosen and not entries and port is not None:
                # The TV would not list its inputs, but HDMI ports have fixed addresses.
                uri, shown = f"extInput:hdmi?port={port}", f"HDMI {port}"
            elif not chosen:
                names = "; ".join(_input_name(e) for e in entries[:10])
                listed = f" Its inputs: {names}." if names else ""
                return Outcome.fail("not_found", f'The {s.name} has no input called "{wanted}".{listed}')
            elif len(chosen) > 1:
                return Outcome.fail("ambiguous", f"Which input: {'; '.join(_input_name(e) for e in chosen[:6])}?")
            else:
                uri, shown = str(chosen[0]["uri"]), _input_name(chosen[0])
        s.client.call("avContent", "setPlayContent", [{"uri": uri}])
        return Outcome.done(f"Switched the {s.name} to {shown}.")

    def _list_inputs(self, s: _Session, _value: Value = None) -> Outcome:
        entries = s.inputs()
        if not entries:
            return Outcome.fail("failed", f"The {s.name} did not list any inputs.")
        shown = []
        for entry in entries:
            title = str(entry.get("title") or "").strip()
            label = str(entry.get("label") or "").strip()
            notes = []
            base = title or label or str(entry["uri"])
            if label and title and normalize(label) != normalize(title):
                base, notes = label, [title]
            if _has_signal(entry):
                notes.append("active")
            shown.append(f"{base} ({', '.join(notes)})" if notes else base)
        return Outcome.done(f"Inputs on the {s.name}: {'; '.join(shown)}.")


def _as_percent(value: Value) -> int:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        number = 0.0
    return max(0, min(100, round(number)))


def _input_hint(device: DeviceRecord) -> str:
    saved = device.settings.get("inputs")
    names = []
    for entry in saved if isinstance(saved, list) else []:
        if isinstance(entry, dict):
            name = str(entry.get("label") or entry.get("title") or "").strip()
            if name and name not in names:
                names.append(name)
    if not names:
        return "an input such as HDMI 2"
    return "|".join(names[:6]) + ("|..." if len(names) > 6 else "")


# --------------------------------------------------------------------------- discovery

SSDP_ADDRESS = ("239.255.255.250", 1900)
SSDP_ST = "urn:schemas-sony-com:service:ScalarWebAPI:1"


@dataclass(slots=True)
class FoundTv:
    address: str
    name: str
    model: str


def discover_tvs(timeout: float = DISCOVERY_S, *, target: tuple[str, int] | None = None) -> list[FoundTv]:
    """Sony TVs that answer an SSDP search within ``timeout`` seconds.

    Soundbars answer the same search, so only devices whose description lists
    the ``videoScreen`` service count. Firewalls often block the replies, and
    newer firmware may not answer this search at all, which is why typing the
    address stays the main path. A multicast leaves
    through one network adapter only, which on a PC with a VPN, WSL, Hyper-V
    or Docker may not be the home network's, so the search also goes out from
    each of the computer's private addresses.
    """
    where = target or SSDP_ADDRESS
    message = (
        f'M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: "ssdp:discover"\r\nMX: 2\r\nST: {SSDP_ST}\r\n\r\n'
    ).encode("ascii")
    locations: dict[str, str] = {}
    with contextlib.ExitStack() as stack:
        sockets: list[socket.socket] = []
        for local in [None, *(_lan_addresses() if target is None else [])]:
            sock = _ssdp_socket(local)
            if sock is None:
                continue
            stack.enter_context(sock)
            try:
                for _ in range(2):  # UDP gets lost; ask twice
                    sock.sendto(message, where)
            except OSError as exc:
                log.debug("SSDP search failed: %s", type(exc).__name__)
                continue
            sock.setblocking(False)  # select() can wake for a datagram that then proves bad
            sockets.append(sock)
        deadline = time.monotonic() + timeout
        while sockets and (left := deadline - time.monotonic()) > 0:
            try:
                readable, _, _ = select.select(sockets, [], [], left)
            except (OSError, ValueError):
                break
            if not readable:
                break  # the time is up
            for sock in readable:
                try:
                    data, sender = sock.recvfrom(4096)
                except (BlockingIOError, ConnectionResetError):
                    continue  # nothing after all, or (Windows) an earlier ICMP "port unreachable"
                except OSError:
                    sockets.remove(sock)
                    continue
                location = _ssdp_header(data, "location")
                # Only descriptions on the device that answered: nothing else on the network gets fetched.
                if location and urlsplit(location).hostname == sender[0] and len(locations) < 16:
                    locations.setdefault(location, sender[0])
    found: list[FoundTv] = []
    for location, host in locations.items():
        tv = _describe_tv(location, host)
        if tv is not None and all(f.address != tv.address for f in found):
            found.append(tv)
    return found


def _ssdp_socket(local: str | None) -> socket.socket | None:
    """A UDP socket for the search; with ``local``, bound to that adapter's
    address and sending its multicast through that adapter."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    except OSError as exc:
        log.debug("SSDP search failed: %s", type(exc).__name__)
        return None
    try:
        if local is not None:
            sock.bind((local, 0))
            with contextlib.suppress(OSError):
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local))
    except OSError:
        sock.close()
        return None
    with contextlib.suppress(OSError):  # the default TTL of 1 reaches the TV's own network too
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    return sock


def _lan_addresses() -> list[str]:
    """This computer's private IPv4 addresses, one per network adapter. Only on
    Windows, which answers its own host name with every adapter's address and
    without asking the network; elsewhere the default search is all there is."""
    if sys.platform != "win32":
        return []
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET, socket.SOCK_DGRAM)
    except (OSError, UnicodeError):
        return []
    found: list[str] = []
    for info in infos:
        address = str(info[4][0])
        try:
            ip = ipaddress.IPv4Address(address)
        except ValueError:
            continue
        if ip.is_private and not ip.is_loopback and not ip.is_link_local and address not in found:
            found.append(address)
    return found[:8]


def _ssdp_header(data: bytes, name: str) -> str | None:
    for line in data.decode("latin-1", errors="replace").split("\r\n")[1:]:
        key, _, value = line.partition(":")
        if key.strip().lower() == name:
            return value.strip()
    return None


def _describe_tv(location: str, host: str) -> FoundTv | None:
    if urlsplit(location).scheme != "http":
        return None
    try:
        response = net.request("GET", location, timeout=2.0)
    except net.HttpError:
        return None
    if response.status != 200:
        return None
    text = response.text
    services = re.findall(r"<(?:[\w-]+:)?X_ScalarWebAPI_ServiceType>\s*([^<]*?)\s*</", text)
    if "videoScreen" not in services:
        return None  # a soundbar or another Sony device
    return FoundTv(host, _xml_value(text, "friendlyName") or "Sony TV", _xml_value(text, "modelName"))


def _xml_value(text: str, tag: str) -> str:
    match = re.search(rf"<(?:[\w-]+:)?{tag}>\s*([^<]*?)\s*</", text)
    return html.unescape(match.group(1)).strip() if match else ""


# --------------------------------------------------------------------------- setup


TV_STEPS = """\
First, once, on the TV (keep it switched on while you do this):
  Google TV (2021 and later), under Settings > Network & Internet:
    1. IP control (on some models inside Local network setup or Home network setup):
       Authentication = Pre-Shared Key (Normal and Pre-Shared Key also works), and a
       Pre-Shared Key of 16 to 20 letters and digits. TVs made since August 2025 have no
       Authentication item and need at least 16: just set the key.
    2. Remote device settings > Control remotely = On.
    3. Remote start = On (or On (Powered on by apps), or On (Networked standby)),
       so Jarvis can switch the TV on.
    4. After the Android 14 update, also Settings > System > Power and Energy >
       Energy Mode = Increased (Low turns the network off in standby).
  Older Android TV: Settings > Network > Home network setup > IP control: Authentication =
    Pre-Shared Key (or Normal and Pre-Shared Key) and set the key; then Remote start = On.
  Choose a key you use nowhere else: on older TVs it travels unencrypted on your home network.
Tip: give the TV a fixed address in your router (a DHCP reservation) so it keeps it."""


@dataclass
class TvProbe:
    """What setup learned about the TV, or ``problem``: why it did not work, in plain words."""

    scheme: str = "http"
    info: dict[str, Any] = field(default_factory=dict)
    mac: str | None = None
    power: str | None = None
    ircc_path: str = IRCC_PATHS[0]
    codes: dict[str, str] = field(default_factory=dict)
    inputs: list[dict[str, str]] = field(default_factory=list)
    problem: str | None = None


def _probe_problem(err: BraviaError, address: str) -> str:
    kind = err.kind
    if kind == "auth":
        return (
            "The TV refused that key. Check the Pre-Shared Key under IP control on the TV "
            "(Authentication, if the TV has it, must be Pre-Shared Key), and that Settings > Network & Internet > "
            "Remote device settings > Control remotely is On. Then type the key again."
        )
    if kind in ("unreachable", "timeout"):
        return (
            f"No answer from {address}. Check the address, that the TV is switched on, "
            "and that this computer is on the same network as the TV."
        )
    if kind == "refused":
        # Neither http nor https answered: setup tries both before saying this.
        return (
            f"The TV at {address} refused the connection. Check that IP control and "
            "Control remotely are on in the TV's network settings. Some models (BRAVIA 2 II, some BRAVIA 3) "
            "list only Simple IP control and Control4 under IP control, and cannot be controlled this way."
        )
    if kind == "restarting":
        return (
            "The TV answered, but its control service did not. Check that IP control and Control remotely "
            "are on. If they are, the TV may be restarting the service: wait half a minute and try again."
        )
    if kind == "tls":
        return f"Could not make a secure connection to {address}."
    if kind == "bad_reply":
        return f"Something at {address} answered, but it does not look like a Sony TV."
    if kind == "bad_key":
        return "That key has characters a TV key cannot have; use letters, digits and common symbols."
    if kind == "http":
        return f"The TV at {address} answered with an error (HTTP {err.status}). Wait a moment and try again."
    if kind == "budget":
        return "The TV took too long to answer. Try again."
    return f"The TV answered with an error ({err.code}). Wait a moment and try again."


_Contact = tuple[BraviaClient, dict[str, Any]] | BraviaError


def _first_contact(address: str, psk: str, scheme: str, retry_wait: float) -> _Contact:
    """A client and the TV's system information over ``scheme`` (empty when it
    answered with an error code), or why that failed."""
    try:
        client = BraviaClient(address, psk, scheme=scheme, budget=PROBE_BUDGET_S, retry_wait=retry_wait)
    except BraviaError as err:
        return err
    # A closed port is not worth waiting for here: setup tries https next, and the user can try again.
    client.retry_kinds = frozenset({"restarting"})
    info: dict[str, Any] = {}
    try:
        info = _first(client.call("system", "getSystemInformation", timeout=5.0))
    except BraviaError as err:
        if err.kind != "rpc":  # an error code still means a Sony TV answered
            return err
    # The app list needs the key's full rights: the surest check that the key is right.
    try:
        client.call("appControl", "getApplicationList")
    except BraviaError as err:
        if err.kind == "auth":
            return err
    return client, info


def _pick_scheme(plain: _Contact, secure: _Contact) -> _Contact:
    """What setup goes on with when plain http did not give the TV's system information."""
    if not isinstance(secure, BraviaError):
        # https gave it, or at least got further than http
        return secure if secure[1] or isinstance(plain, BraviaError) else plain
    if secure.kind == "auth":
        return secure  # the key is wrong only when https refuses it too
    if not isinstance(plain, BraviaError):
        return plain  # http answered as a Sony TV does, if with an error code: as before https was tried
    # Both failed: https's own answer says more, unless it gave none.
    return secure if secure.kind not in GONE | {"budget"} else plain


def _network_mac(client: BraviaClient, address: str) -> str | None:
    """The MAC from getNetworkSettings, for TVs whose system information lacks it: the
    hwAddr of the interface with the address setup reached the TV on."""
    host = host_of(address)
    try:
        result = client.call("system", "getNetworkSettings", [{"netif": ""}], timeout=5.0)
    except BraviaError:
        return None
    for entry in _first_list(result):
        if isinstance(entry, dict) and host and str(entry.get("ipAddrV4") or "").strip() == host:
            return net.normalize_mac(str(entry.get("hwAddr") or ""))
    return None


def probe_tv(address: str, psk: str, *, retry_wait: float | None = None) -> TvProbe:
    """Checks the address and key the way the driver will use them, and collects
    the model, MAC, power state, IRCC path, button codes and inputs.

    http goes first. TVs made since August 2025 take only https and answer plain
    http with an error (often as if the key were wrong), so https is tried
    whenever http answered without the TV's system information. The key is
    called wrong only when https refuses it too, or does not answer at all."""
    retry_wait = RETRY_WAIT_S if retry_wait is None else retry_wait
    probe = TvProbe()
    contact = _first_contact(address, psk, "http", retry_wait)
    if isinstance(contact, BraviaError) and contact.kind not in HTTPS_HINTS:
        probe.problem = _probe_problem(contact, address)  # no answer at all, or a key it cannot send
        return probe
    if isinstance(contact, BraviaError) or not contact[1]:
        contact = _pick_scheme(contact, _first_contact(address, psk, "https", retry_wait))
    if isinstance(contact, BraviaError):
        probe.problem = _probe_problem(contact, address)
        return probe
    client, probe.info = contact
    probe.scheme = client.scheme
    with contextlib.suppress(BraviaError):
        probe.power = str(_first(client.call("system", "getPowerStatus")).get("status") or "") or None
    probe.mac = net.normalize_mac(str(probe.info.get("macAddr") or "")) or _network_mac(client, address)
    # Home Assistant does this too: with the TV's Wake-on-LAN mode off, it ignores the
    # magic packet that turn_on sends when the TV is off the network.
    with contextlib.suppress(BraviaError):
        client.call("system", "setWolMode", [{"enabled": True}], timeout=5.0, retry=False)
    probe.ircc_path = _find_ircc_path(client)
    with contextlib.suppress(BraviaError):
        probe.codes = _parse_codes(client.call("system", "getRemoteControllerInfo"))
    with contextlib.suppress(BraviaError):
        result = client.call("avContent", "getCurrentExternalInputsStatus", version="1.1")
        probe.inputs = _input_cache([e for e in _first_list(result) if isinstance(e, dict)])
    return probe


def _find_ircc_path(client: BraviaClient) -> str:
    """Which spelling of the IRCC path this TV answers, found with an empty (harmless) request."""
    for path in IRCC_PATHS:
        try:
            client.ircc("", path, timeout=WAKE_TIMEOUT_S)
        except BraviaError as err:
            if err.kind == "restarting":
                continue
            if err.kind in GONE or err.kind == "budget":
                break
        return path  # answered (even with a fault for the empty code): the path exists
    return IRCC_PATHS[0]


def _clean_address(text: str) -> str | None:
    """``192.168.1.20`` or ``tv.lan`` (an optional port), from what the user typed."""
    text = text.strip().removeprefix("http://").removeprefix("https://").rstrip("/")
    if not text or any(c.isspace() for c in text):
        return None
    try:
        parts = urlsplit(f"//{_netloc(text)}")
        host, _port = parts.hostname, parts.port
    except ValueError:
        return None
    if not host or parts.path or parts.query or not re.fullmatch(r"[A-Za-z0-9.\-:]+", host):
        return None
    return text


def _ask_discovered(ui: Prompter) -> str | None:
    if not ui.confirm("Search your home network for Sony TVs? It takes about 3 seconds.", default=True):
        return None
    ui.say("Searching...")
    found = discover_tvs(DISCOVERY_S)
    if not found:
        ui.say(
            "No Sony TV answered. That is common (a firewall may block the search, and newer TVs may not "
            "answer it): type its address instead."
        )
        return None
    options = [f"{tv.name} ({tv.model}) at {tv.address}" if tv.model else f"{tv.name} at {tv.address}" for tv in found]
    index = ui.choose("Which TV?", [*options, "Type the address myself"])
    return found[index].address if index is not None and index < len(found) else None


def _connect(ui: Prompter, suggested: str | None) -> tuple[str, str, TvProbe] | None:
    address = suggested
    for _ in range(MAX_SETUP_TRIES):
        typed = ui.ask("The TV's IP address (the TV shows it under Settings > Network & Internet)", address)
        if not typed.strip():
            return None
        cleaned = _clean_address(typed)
        if cleaned is None:
            ui.say("That does not look like an address. Type something like 192.168.1.20.")
            continue
        address = cleaned
        psk = ui.ask_secret("The TV's pre-shared key")
        if not psk:
            return None
        if not usable_key(psk):
            ui.say("That key has characters a TV key cannot have; use letters, digits and common symbols.")
            continue
        ui.say(f"Checking the TV at {address}...")
        probe = probe_tv(address, psk)
        if probe.problem is None:
            return address, psk, probe
        ui.say(probe.problem)
        if not ui.confirm("Try again?", default=True):
            return None
    return None


def _existing(config: HomeConfig, address: str, mac: str | None) -> DeviceRecord | None:
    for device in config.devices:
        if device.driver != BraviaDriver.name:
            continue
        if device.settings.get("address") == address or (mac and device.settings.get("mac") == mac):
            return device
    return None


def _save(ctx: DriverContext, record: DeviceRecord, psk: str) -> None:
    """The key, then the device; if anything stops in between, the key goes back to what it was."""
    key = record.secret_key
    previous = ctx.store.secret(key)
    saved = False
    try:
        ctx.store.set_secret(key, {"psk": psk})
        ctx.store.update(lambda config: config.upsert(record))
        saved = True
    finally:
        if not saved:
            try:
                ctx.store.set_secret(key, previous)
            except (StoreError, OSError):
                log.warning("could not undo the half-saved key for %s", record.id)


def wizard(ui: Prompter, ctx: DriverContext) -> None:
    """Adds a Sony Bravia TV, or updates one added before: address, key, name and room."""
    ui.say("Add a Sony Bravia TV")
    ui.say(TV_STEPS)
    connected = _connect(ui, _ask_discovered(ui))
    if connected is None:
        ui.say("Nothing was saved.")
        return
    address, psk, probe = connected
    model = str(probe.info.get("model") or "").strip()
    mac = probe.mac
    state = {"active": " It is on.", "standby": " It is in standby."}.get(probe.power or "", "")
    ui.say(f"Connected to the Sony {model}.{state}" if model else f"Connected to the TV.{state}")
    if mac is None:
        ui.say(
            "The TV did not give its network address (MAC), so Jarvis can switch it on only while Remote start is on."
        )
    config = ctx.store.load()
    existing = _existing(config, address, mac)
    if existing is not None:
        ui.say(f"This TV is already set up as {existing.name}; Jarvis will update it.")
        default_name = existing.name
    else:
        taken = {normalize(d.name) for d in config.devices}
        default_name = f"Sony {model}" if "sony tv" in taken and model else "Sony TV"
    name = ui.ask("A name for this TV", default_name).strip() or default_name
    room = ui.ask("Room (empty for none)", (existing.room if existing else None) or "").strip() or None
    settings: dict[str, Any] = {"address": address, "scheme": probe.scheme, "ircc_path": probe.ircc_path}
    if mac:
        settings["mac"] = mac
    if model:
        settings["model"] = model
    if probe.codes:
        settings["ircc_codes"] = probe.codes
    if probe.inputs:
        settings["inputs"] = probe.inputs
    record = DeviceRecord(
        existing.id if existing else unique_id(config.ids(), BraviaDriver.name, name),
        BraviaDriver.name,
        name,
        "tv",
        room,
        aliases=list(existing.aliases) if existing else [],
        confirm=existing.confirm if existing else None,
        settings=settings,
    )
    _save(ctx, record, psk)
    ui.say(f"Saved {name}. Jarvis can control it now.")
    if ui.confirm("Try it now? The TV will show its Home screen.", default=True):
        driver = BraviaDriver(ctx)
        ui.say(driver.status(record).text)
        ui.say(driver.run(record, "home", None).text)
