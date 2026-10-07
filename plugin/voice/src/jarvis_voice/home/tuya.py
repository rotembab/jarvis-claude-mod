"""Tuya / Smart Life devices over the home network (tinytuya), linked once through the Smart Life app.

Setup (``wizard``) signs in to the user's Smart Life account the way Home
Assistant's Tuya integration does: the user types the User Code shown in the
app, Jarvis shows a QR code, and the app scans and confirms it. tuya_sharing
then lists the homes, the devices (each with its local key and data-point map)
and the tap-to-run scenes. A UDP scan (tinytuya's scanner) finds each device's
address and protocol version on the home network, which the cloud does not know.

The sign-in reuses Home Assistant's own Tuya app registration (client id
HA_3y9q4ak7g4ephrvke, schema haauthorize), as tuya-local and other tools do.
Tuya has not said that others may do this, so it is unofficial and may stop
working one day; the devices keep working locally if it does, and only scenes
and refreshing need it.

Control is local: every request builds a fresh tinytuya device (most Tuya
devices accept one connection at a time, so Jarvis never holds one open), with
timeouts sized so the worst case fits the service's time limit. Scenes run in
the cloud. Keys and tokens live only in the credential store, and tinytuya
objects and replies are never printed or logged: their repr carries the key.
"""

from __future__ import annotations

import functools
import logging
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from . import tuya_dps as tdp
from .base import Driver, DriverContext, Prompter, quiet_device_loggers
from .model import CommandSpec, DeviceRecord, HomeConfig, Outcome, Value, normalize, unique_id
from .store import HomeStore, StoreError
from .tuya_dps import DpMap

log = logging.getLogger(__name__)

# Home Assistant's Tuya app registration (see the module docstring: unofficial).
CLIENT_ID = "HA_3y9q4ak7g4ephrvke"
SCHEMA = "haauthorize"
QR_PREFIX = "tuyaSmart--qrLogin?token="
# The Smart Life sign-in, in the credential store: user_code, terminal_id, endpoint, token_info.
CLOUD_SECRET = "tuya:cloud"
TOKEN_FIELDS = ("t", "uid", "expire_time", "access_token", "refresh_token")

# Smart Life calls: requests' (connect, read) timeouts instead of the SDK's 60 s.
CLOUD_TIMEOUT_S = (3.5, 5.0)
LOGIN_WAIT_S = 180.0
LOGIN_POLL_S = 2.0
# How long the setup scan listens (tinytuya's own default; it stops early once every device answered).
SCAN_S = 18.0
# A short scan from the helper when a device stops answering at its saved address, at most once per gap.
RESCAN_S = 6.0
RESCAN_GAP_S = 120.0

# Timing of local requests. With connection_retry_limit=1, one tinytuya request makes at most two
# attempts (it retries once after a timeout), each a connect and a receive of up to T seconds; v3.4
# and v3.5 add a session-key exchange of up to two more receives per connect. So one request takes
# at most 4T (v3.1-3.3) or 8T (v3.4+), and set_multiple_values counts twice: after any error it
# sends the first value again on its own. T is chosen per request from what is left of the budget.
RUN_BUDGET_S = 17.0
CALL_MARGIN_S = 2.5  # kept free below the service's limit (ctx.call_timeout)
T_MAX = 2.5
T_MIN = 0.6
UNIT_SLACK_S = 0.3  # tinytuya's own short sleeps, per request
RETRY_LIMIT = 1  # 0 would never connect at all (tinytuya returns 905 straight away)
RETRY_DELAY_S = 0.5

# tinytuya's error numbers (core/error_helper.py).
ERR_UNREACHABLE = frozenset({"901", "905"})
ERR_TIMEOUT = "902"
ERR_RANGE = "903"
ERR_KEY = frozenset({"904", "914"})
ERR_DEVTYPE = "908"
RESCAN_ERRORS = ERR_UNREACHABLE | ERR_KEY | {ERR_TIMEOUT}

# Tuya cloud error codes that mean the sign-in is no longer valid (not documented; seen in practice).
TOKEN_ERRORS = frozenset({"1010", "1011", "1012", "1013", "1100", "1106"})

EXPIRED = "The Smart Life link has expired. Open home setup and choose Link Smart Life to scan a new code."
NOT_LINKED = "Smart Life is not linked, so Jarvis cannot run its scenes. Link it in home setup."

LINK_STEPS = """\
Jarvis signs in to your Smart Life account once, to read each device's local key and its scenes.
After that it controls the devices directly over your home network.

1. Find your User Code in the Smart Life app: Me > Settings (the gear, top right) >
   Account and Security > User Code.
2. Type it here. Jarvis then shows a QR code.
3. In Smart Life, tap + (top right), then Scan, scan the code and confirm the login.
   Your phone may say the login is for Home Assistant: Jarvis signs in the same way
   Home Assistant's Tuya integration does."""

IR_ADVICE = (
    "Jarvis cannot drive an air conditioner or TV through an IR blaster directly. In Smart Life, make a "
    'tap-to-run scene for each thing you want, such as "AC cool 24" and "AC off", then choose Refresh '
    "devices and scenes from Smart Life here: Jarvis can run those scenes."
)
FIREWALL_HINT = "If your firewall asks about Python, allow it on private (home) networks only."


# --------------------------------------------------------------------------- library seams (tests replace these)


def _tinytuya() -> Any:
    """The tinytuya module, imported on first use."""
    import tinytuya

    return tinytuya


def _scanner() -> Any:
    # ``import tinytuya`` alone does not load the scanner submodule.
    from tinytuya import scanner

    return scanner


def _sharing() -> Any:
    """tuya_sharing, with its 60 s HTTP timeouts lowered (the constant is bound in two modules)."""
    import tuya_sharing
    from tuya_sharing import customerapi, user

    customerapi.DEFAULT_TIMEOUT = CLOUD_TIMEOUT_S  # type: ignore[assignment]
    user.DEFAULT_TIMEOUT = CLOUD_TIMEOUT_S  # type: ignore[assignment]
    quiet_device_loggers()  # its logger has a handler of its own and logs whole replies
    return tuya_sharing


# --------------------------------------------------------------------------- discovery


@dataclass(frozen=True)
class Found:
    """A device that answered the scan: its address on the home network and protocol version."""

    address: str
    version: str


def _version(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if 3.0 <= number < 4.0 else None


def discover(ids: Iterable[str], scantime: float) -> dict[str, Found] | None:
    """Listens for the given Tuya devices' broadcasts. None when the scan could not run at all."""
    wanted = sorted({i for i in ids if isinstance(i, str) and i})
    if not wanted:
        return {}
    try:
        scanner = _scanner()
        # Entries need "name" and "key" (the scanner reads both), and an empty list would make it
        # load ./devices.json; wantids lets it stop as soon as every device has answered.
        result = scanner.devices(
            verbose=False,
            scantime=scantime,
            color=False,
            poll=False,
            byID=True,
            wantids=list(wanted),
            tuyadevices=[{"id": i, "name": "", "key": ""} for i in wanted],
        )
    except OSError as exc:
        # Another program (or a second scan) holds UDP 6666/6667/7000.
        log.warning("the Tuya scan could not listen: %s", exc.strerror or type(exc).__name__)
        return None
    except Exception as exc:  # noqa: BLE001 - a scanner bug must not end setup
        log.warning("the Tuya scan failed: %s", type(exc).__name__)
        return None
    found: dict[str, Found] = {}
    for device_id, info in (result or {}).items():
        if device_id not in wanted or not isinstance(info, dict):
            continue
        address, version = info.get("ip"), info.get("version")
        if isinstance(address, str) and address and _version(version) is not None:
            found[device_id] = Found(address, str(version))
    return found


# --------------------------------------------------------------------------- local requests


@dataclass(frozen=True, repr=False)
class _Conn:
    """Where and how to reach a device (sub-devices go through their gateway's address and key)."""

    tuya_id: str
    address: str | None
    version: float | None
    key: str
    parent: str | None = None
    node_id: str | None = None
    dps: tuple[int, ...] = ()

    def __repr__(self) -> str:  # never the key
        return f"_Conn({self.tuya_id!r})"


class _Failure(Exception):
    """A request that did not work. ``err`` is tinytuya's error number, or one of Jarvis's own words."""

    def __init__(self, err: str) -> None:
        super().__init__(err)
        self.err = err


class _Budget:
    """The time one command may spend on the network, shared out request by request."""

    def __init__(self, seconds: float) -> None:
        self._end = time.monotonic() + seconds

    def left(self) -> float:
        return self._end - time.monotonic()

    def timeout(self, units: int, factor: int, reserve: int = 0) -> float | None:
        """T for a request of ``units`` that still leaves room for ``reserve`` more, or None when it cannot."""
        spare = self.left() - reserve * (factor * T_MIN + UNIT_SLACK_S) - units * UNIT_SLACK_S
        seconds = spare / (units * factor)
        return min(T_MAX, seconds) if seconds >= T_MIN else None


def _open_device(conn: _Conn, timeout: float) -> Any:
    """A fresh tinytuya device, never persistent, with the version always given (else it assumes 3.1)."""
    tinytuya = _tinytuya()
    assert conn.address is not None and conn.version is not None
    options = {
        "connection_timeout": timeout,
        "connection_retry_limit": RETRY_LIMIT,
        "connection_retry_delay": RETRY_DELAY_S,
        "persist": False,
    }
    gateway_id = conn.parent or conn.tuya_id
    if conn.version == 3.2:
        # set_version(3.2) probes the DPs over the network unless it knows which to ask for.
        gateway = tinytuya.Device(gateway_id, conn.address, conn.key, version=3.3, **options)
        gateway.dps_to_request = {str(dp): None for dp in conn.dps} or {"1": None}
        gateway.set_version(3.2)
    else:
        gateway = tinytuya.Device(gateway_id, conn.address, conn.key, version=conn.version, **options)
    if conn.parent is None:
        return gateway
    return tinytuya.Device(conn.tuya_id, cid=conn.node_id, parent=gateway, **options)


def _close(device: Any) -> None:
    for item in (device, getattr(device, "parent", None)):
        if item is not None:
            try:
                item.close()
            except Exception:  # noqa: BLE001 - closing is best effort
                pass


def _error_code(result: Any) -> str | None:
    """tinytuya answers errors with a dict (never raised): {"Error", "Err": "9xx", "Payload"}."""
    if isinstance(result, dict) and "Err" in result:
        return str(result.get("Err") or "unknown")
    return None


class _Link:
    """One command's requests to one device, each on a fresh tinytuya object, within the command's budget."""

    def __init__(self, conn: _Conn, budget: _Budget, open_device: Callable[[_Conn, float], Any]) -> None:
        self.conn = conn
        self.budget = budget
        self._open = open_device
        self.factor = 8 if (conn.version or 3.3) >= 3.4 else 4

    def affordable(self, units: int, reserve: int = 0) -> bool:
        return self.budget.timeout(units, self.factor, reserve) is not None

    def read(self, reserve: int = 0) -> dict[str, Any]:
        """The device's DPs, by number as text."""
        result = self._request(1, reserve, lambda device: device.status())
        values = result.get("dps") if isinstance(result, dict) else None
        if not isinstance(values, dict):
            raise _Failure("nodata")
        return {str(k): v for k, v in values.items()}

    def write(self, values: Mapping[int, Any], reserve: int = 0) -> None:
        data = {str(dp): value for dp, value in values.items()}
        if len(data) == 1:
            ((dp, value),) = data.items()
            self._request(1, reserve, lambda device: device.set_value(dp, value))
        elif data:
            self._request(2, reserve, lambda device: device.set_multiple_values(data))

    def _request(self, units: int, reserve: int, action: Callable[[Any], Any]) -> Any:
        timeout = self.budget.timeout(units, self.factor, reserve)
        if timeout is None:
            raise _Failure("budget")
        device = self._open(self.conn, timeout)
        try:
            result = action(device)
            if _error_code(result) == ERR_DEVTYPE and self.affordable(units, reserve):
                # The device turned out to speak the "device22" dialect; tinytuya has switched: send again.
                result = action(device)
        except Exception as exc:  # noqa: BLE001 - a library error must not take the command down
            log.warning("a request to Tuya device %s raised %s", self.conn.tuya_id, type(exc).__name__)
            raise _Failure("error") from None
        finally:
            _close(device)
        err = _error_code(result)
        if err is not None:
            raise _Failure(err)
        return result


# --------------------------------------------------------------------------- the Smart Life cloud


class _TokenKeeper:
    """tuya_sharing calls ``update_token`` when it renews the sign-in; the new tokens must be kept, or the
    saved refresh token goes stale. With a store they are saved at once; during setup, with the rest."""

    def __init__(self, store: HomeStore | None, link: dict[str, Any]) -> None:
        self.store = store
        self.link = link

    def update_token(self, token_info: Mapping[str, Any]) -> None:
        self.link["token_info"] = {name: token_info.get(name) for name in TOKEN_FIELDS}
        if self.store is not None:
            try:
                self.store.set_secret(CLOUD_SECRET, self.link)
            except (StoreError, OSError):
                log.warning("could not save the renewed Smart Life sign-in")


def _usable_link(link: Any) -> dict[str, Any] | None:
    if not isinstance(link, dict):
        return None
    token = link.get("token_info")
    fields = ("user_code", "terminal_id", "endpoint")
    if not all(isinstance(link.get(name), str) and link.get(name) for name in fields) or not isinstance(token, dict):
        return None
    return link if token.get("access_token") and token.get("refresh_token") else None


def _manager(sharing: Any, link: dict[str, Any], keeper: _TokenKeeper) -> Any:
    return sharing.Manager(
        CLIENT_ID, link["user_code"], link["terminal_id"], link["endpoint"], dict(link["token_info"]), keeper
    )


def _expired(exc: BaseException) -> bool:
    code = str(getattr(exc, "error_code", "") or "")
    message = str(getattr(exc, "error_message", "") or "").lower()
    return code in TOKEN_ERRORS or any(word in message for word in ("token", "expire", "login", "auth"))


def _cloud_failure(exc: Exception, doing: str) -> Outcome:
    """An Outcome for a failed Smart Life call; the reply itself never reaches the text."""
    if getattr(exc, "error_code", None) is not None:
        if _expired(exc):
            return Outcome.fail("auth", EXPIRED)
        log.warning("Smart Life refused a request (error %s)", getattr(exc, "error_code", "?"))
        return Outcome.fail("failed", f"Smart Life could not {doing}.")
    if isinstance(exc, OSError):  # requests' errors are OSErrors
        return Outcome.fail("unreachable", f"Jarvis could not reach Smart Life to {doing}. Is the internet working?")
    log.warning("Smart Life gave an unexpected answer: %s", type(exc).__name__)
    return Outcome.fail("failed", f"Smart Life gave an unexpected answer, so Jarvis could not {doing}.")


# --------------------------------------------------------------------------- the driver

Handler = Callable[[DeviceRecord, _Link, Value], Outcome]


class TuyaDriver(Driver):
    name = "tuya"
    label = "Tuya"

    # Timings, as attributes so tests can shorten them.
    rescan_s = RESCAN_S
    rescan_gap_s = RESCAN_GAP_S

    def __init__(self, ctx: DriverContext) -> None:
        super().__init__(ctx)
        self._scan_lock = threading.Lock()
        self._scanned: dict[str, float] = {}
        self._handlers: dict[str, Handler] = {
            "turn_on": functools.partial(self._power, on=True),
            "turn_off": functools.partial(self._power, on=False),
            "toggle": self._toggle,
            "set_brightness": self._set_brightness,
            "set_color": self._set_color,
            "set_color_temp": self._set_color_temp,
            "open": functools.partial(self._move, action="open"),
            "close": functools.partial(self._move, action="close"),
            "set_position": self._set_position,
            "set_temperature": self._set_temperature,
            "set_humidity": self._set_humidity,
            "set_mode": self._set_mode,
            "set_fan_speed": self._set_fan_speed,
            "start": functools.partial(self._vacuum, action="start"),
            "pause": functools.partial(self._vacuum, action="pause"),
            "dock": functools.partial(self._vacuum, action="dock"),
        }

    # ------------------------------------------------------------------ Driver

    def commands(self, device: DeviceRecord) -> list[CommandSpec]:
        return tdp.command_specs(device)

    def lock_key(self, device: DeviceRecord) -> str:
        """A sub-device talks through its gateway's one connection, so it shares the gateway's lock."""
        if device.kind == "scene":
            return device.id
        target = device.settings.get("parent") or device.settings.get("tuya_id")
        return f"tuya:{target}" if isinstance(target, str) and target else device.id

    def run(self, device: DeviceRecord, command: str, value: Value) -> Outcome:
        if device.kind == "scene":
            if command == "activate":
                return self._activate(device)
            return Outcome.fail("unsupported", f"{device.name} is a Smart Life scene; it can only be run.")
        if command not in {spec.name for spec in self.commands(device)}:
            return Outcome.fail("unsupported", f"The {device.name} can't {command.replace('_', ' ')}.")
        handler = self._handlers.get(command)
        if command == "stop":
            handler = functools.partial(self._vacuum, action="stop") if device.kind == "vacuum" else self._stop
        if handler is None:
            return Outcome.fail("unsupported", f"The {device.name} can't {command.replace('_', ' ')}.")
        return self._with_link(device, lambda link: handler(device, link, value))

    def status(self, device: DeviceRecord) -> Outcome:
        if device.kind == "scene":
            return Outcome.fail("unsupported", f"{device.name} is a Smart Life scene; it has no state to read.")
        return self._with_link(device, lambda link: Outcome.done(tdp.describe_state(device, link.read())))

    def describe(self) -> str | None:
        try:
            config = self.ctx.store.load()
            mine = [d for d in config.devices if d.driver == self.name]
            keyless = [
                d.name
                for d in mine
                if d.kind != "scene" and not (self.ctx.store.secret(d.secret_key) or {}).get("local_key")
            ]
            linked = _usable_link(self.ctx.store.secret(CLOUD_SECRET)) is not None
        except StoreError:
            return "Tuya: the saved keys could not be read; refresh from Smart Life in home setup."
        notes = []
        if keyless:
            notes.append(f"no key saved for {', '.join(keyless[:5])}; refresh from Smart Life in home setup")
        if any(d.kind == "scene" for d in mine) and not linked:
            notes.append("Smart Life is not linked, so its scenes cannot run; link it in home setup")
        return f"Tuya: {'; '.join(notes)}." if notes else None

    # ------------------------------------------------------------------ plumbing

    def _budget_s(self) -> float:
        return max(1.0, min(RUN_BUDGET_S, self.ctx.call_timeout - CALL_MARGIN_S))

    def _conn(self, device: DeviceRecord) -> _Conn | Outcome:
        settings = device.settings
        tuya_id = settings.get("tuya_id")
        if not isinstance(tuya_id, str) or not tuya_id:
            return Outcome.fail(
                "needs_setup", f"The {device.name} is missing its Tuya details; refresh from Smart Life in home setup."
            )
        try:
            secret = self.ctx.store.secret(device.secret_key) or {}
        except StoreError:
            return Outcome.fail(
                "needs_setup", f"Jarvis could not read the {device.name}'s key; refresh from Smart Life in home setup."
            )
        key = secret.get("local_key")
        if not isinstance(key, str) or not key:
            return Outcome.fail(
                "needs_setup", f"Jarvis has no key for the {device.name}; refresh from Smart Life in home setup."
            )
        address = settings.get("address")
        parent, node_id = settings.get("parent"), settings.get("node_id")
        return _Conn(
            tuya_id,
            address if isinstance(address, str) and address else None,
            _version(settings.get("version")),
            key,
            parent if isinstance(parent, str) and parent else None,
            str(node_id) if isinstance(node_id, (str, int)) and str(node_id) else None,
            tuple(sorted({entry["dp"] for entry in tdp.dp_map(device).values()})),
        )

    def _with_link(self, device: DeviceRecord, work: Callable[[_Link], Outcome]) -> Outcome:
        """Runs ``work``; when the device does not answer at its saved address, looks for it once and retries."""
        conn = self._conn(device)
        if isinstance(conn, Outcome):
            return conn
        budget = _Budget(self._budget_s())
        rescanned = False
        if conn.address is None or conn.version is None:
            rescanned = True
            found = self._rediscover(conn, budget)
            if found is None:
                return self._failure(device, _Failure("noaddress"))
            conn = found
        try:
            return work(_Link(conn, budget, _open_device))
        except _Failure as failure:
            if rescanned or failure.err not in RESCAN_ERRORS:
                return self._failure(device, failure)
            fresh = self._rediscover(conn, budget)
            if fresh is None or (fresh.address, fresh.version) == (conn.address, conn.version):
                return self._failure(device, failure)
            try:
                return work(_Link(fresh, budget, _open_device))
            except _Failure as again:
                return self._failure(device, again)

    def _rediscover(self, conn: _Conn, budget: _Budget) -> _Conn | None:
        """A short scan for the device (its gateway for a sub-device); saves a new address or version."""
        target = conn.parent or conn.tuya_id
        # Only when a retry still fits afterwards (8 x T_MIN covers one request of any version).
        if budget.left() - (8 * T_MIN + UNIT_SLACK_S) - 1.0 < self.rescan_s:
            return None
        if not self._scan_lock.acquire(blocking=False):
            return None
        try:
            last = self._scanned.get(target)
            if last is not None and time.monotonic() - last < self.rescan_gap_s:
                return None
            self._scanned[target] = time.monotonic()
            found = discover([target], self.rescan_s)
        finally:
            self._scan_lock.release()
        hit = (found or {}).get(target)
        version = _version(hit.version) if hit else None
        if hit is None or version is None:
            return None
        if (hit.address, version) != (conn.address, conn.version):
            log.info("Tuya device %s answered at a new address or protocol version", target)
            _save_found(self.ctx.store, {target: hit})
        return replace(conn, address=hit.address, version=version)

    def _failure(self, device: DeviceRecord, failure: _Failure) -> Outcome:
        the, err = f"The {device.name}", failure.err
        if err in ERR_UNREACHABLE:
            return Outcome.fail("unreachable", f"{the} did not answer. Is it powered and on the home network?")
        if err == "noaddress":
            return Outcome.fail(
                "unreachable",
                f"{the} has not been found on the home network. If it is powered, choose Find my Tuya devices "
                "in home setup; battery devices sleep and cannot be reached.",
            )
        if err == ERR_TIMEOUT:
            return Outcome.fail(
                "timeout", f"{the} did not answer in time; it may be busy with another app. Try again in a moment."
            )
        if err == "budget":
            return Outcome.fail("timeout", f"{the} did not answer in time.")
        if err in ERR_KEY:
            return Outcome.fail(
                "auth",
                f"{the} did not accept Jarvis's key. The key may have changed after re-pairing: "
                "refresh from Smart Life in home setup.",
            )
        if err == ERR_RANGE:
            return Outcome.fail("bad_value", f"{the} refused that value.")
        if err == "nodata":
            return Outcome.fail("failed", f"{the} answered without saying what state it is in.")
        return Outcome.fail("failed", f"Controlling the {device.name} failed.")

    # ------------------------------------------------------------------ commands

    @staticmethod
    def _dp(dps: DpMap, code: str | None) -> int:
        assert code is not None
        return int(dps[code]["dp"])

    def _power(self, device: DeviceRecord, link: _Link, _value: Value = None, *, on: bool) -> Outcome:
        dps = tdp.dp_map(device)
        link.write({self._dp(dps, tdp.power_code(device, dps)): on})
        return Outcome.done(f"The {device.name} is {'on' if on else 'off'}.")

    def _toggle(self, device: DeviceRecord, link: _Link, _value: Value = None) -> Outcome:
        dps = tdp.dp_map(device)
        dp = self._dp(dps, tdp.power_code(device, dps))
        current = link.read(reserve=1).get(str(dp))
        if not isinstance(current, bool):
            return Outcome.fail("failed", f"The {device.name} did not say whether it is on, so Jarvis left it alone.")
        link.write({dp: not current})
        return Outcome.done(f"The {device.name} is now {'off' if current else 'on'}.")

    def _light_now(
        self, link: _Link, dps: DpMap, parts: tdp.LightParts, reserve: int
    ) -> tuple[str | None, tuple[float, float, float] | None, int | None]:
        """(work mode, colour, white brightness %) when there is time to ask; Nones otherwise."""
        if not (parts.mode and parts.colour) or not link.affordable(1, reserve):
            return None, None, None
        try:
            values = link.read(reserve=reserve)
        except _Failure as failure:
            if failure.err != "nodata":
                raise
            return None, None, None

        def raw(code: str | None) -> Any:
            return None if code is None else values.get(str(dps[code]["dp"]))

        mode = tdp.enum_std(dps[parts.mode], raw(parts.mode))
        colour = tdp.decode_colour(dps[parts.colour]["format"], raw(parts.colour))
        bright = tdp.raw_to_percent(dps[parts.bright], raw(parts.bright)) if parts.bright else None
        return mode, colour, bright

    def _set_brightness(self, device: DeviceRecord, link: _Link, value: Value) -> Outcome:
        percent = int(value) if isinstance(value, (int, float)) else 0
        dps = tdp.dp_map(device)
        parts, power = tdp.light_parts(dps), tdp.power_code(device, dps)
        if percent <= 0 and power:
            link.write({self._dp(dps, power): False})
            return Outcome.done(f"The {device.name} is off.")
        percent = max(1, percent)
        writes: dict[int, Any] = {self._dp(dps, power): True} if power else {}
        # In colour mode the brightness is the colour's own value, so a colour light is asked first.
        mode, colour, _ = self._light_now(link, dps, parts, reserve=2)
        if parts.colour and (mode == "colour" or parts.bright is None):
            h, s, _v = colour or (0.0, 0.0, 1.0)
            writes[self._dp(dps, parts.colour)] = tdp.encode_colour(dps[parts.colour]["format"], h, s, percent / 100)
        else:
            assert parts.bright is not None
            writes[self._dp(dps, parts.bright)] = tdp.percent_to_raw(dps[parts.bright], percent)
        link.write(writes)
        return Outcome.done(f"The {device.name} is at {percent}% brightness.")

    def _set_color(self, device: DeviceRecord, link: _Link, value: Value) -> Outcome:
        colour = tdp.parse_colour(str(value or ""))
        if colour is None:
            return Outcome.fail(
                "bad_value",
                f'Jarvis does not know the colour "{str(value)[:40]}". Use a name such as blue or warm white, '
                "or #rrggbb.",
            )
        dps = tdp.dp_map(device)
        parts, power = tdp.light_parts(dps), tdp.power_code(device, dps)
        assert parts.colour is not None
        fmt = dps[parts.colour]["format"]
        writes: dict[int, Any] = {self._dp(dps, power): True} if power else {}
        if colour.white and (parts.mode or parts.temp):
            if parts.mode:
                writes[self._dp(dps, parts.mode)] = tdp.enum_raw(dps[parts.mode], "white")
            if colour.temp is not None and parts.temp:
                writes[self._dp(dps, parts.temp)] = tdp.percent_to_raw(dps[parts.temp], colour.temp)
            link.write(writes)
            said = colour.name if colour.name in tdp.WHITES else "white"
            return Outcome.done(f"The {device.name} is {said} now.")
        v = colour.v
        if v is None:
            mode, now, bright = self._light_now(link, dps, parts, reserve=2)
            if mode == "colour" and now is not None:
                v = now[2]
            elif bright is not None:
                v = bright / 100
        if parts.mode:
            writes[self._dp(dps, parts.mode)] = tdp.enum_raw(dps[parts.mode], "colour")
        saturation = 0.0 if colour.white else colour.s
        writes[self._dp(dps, parts.colour)] = tdp.encode_colour(fmt, colour.h, saturation, v or 1.0)
        link.write(writes)
        return Outcome.done(f"The {device.name} is {colour.name} now.")

    def _set_color_temp(self, device: DeviceRecord, link: _Link, value: Value) -> Outcome:
        percent = tdp.parse_color_temp(value)
        if percent is None:
            return Outcome.fail("bad_value", "set_color_temp takes warm, neutral, cool, or 0 to 100 (0 = warmest).")
        dps = tdp.dp_map(device)
        parts, power = tdp.light_parts(dps), tdp.power_code(device, dps)
        assert parts.temp is not None
        writes: dict[int, Any] = {self._dp(dps, power): True} if power else {}
        if parts.mode:
            writes[self._dp(dps, parts.mode)] = tdp.enum_raw(dps[parts.mode], "white")
        writes[self._dp(dps, parts.temp)] = tdp.percent_to_raw(dps[parts.temp], percent)
        link.write(writes)
        return Outcome.done(f"The {device.name} is {tdp.temp_words(percent)} now.")

    def _move(self, device: DeviceRecord, link: _Link, _value: Value = None, *, action: str) -> Outcome:
        dps = tdp.dp_map(device)
        code, actions = tdp.cover_control(dps)
        link.write({self._dp(dps, code): actions[action]})
        return Outcome.done(f"The {device.name} is {'opening' if action == 'open' else 'closing'}.")

    def _stop(self, device: DeviceRecord, link: _Link, _value: Value = None) -> Outcome:
        dps = tdp.dp_map(device)
        code, actions = tdp.cover_control(dps)
        link.write({self._dp(dps, code): actions["stop"]})
        return Outcome.done(f"The {device.name} has stopped.")

    def _set_position(self, device: DeviceRecord, link: _Link, value: Value) -> Outcome:
        percent = int(value) if isinstance(value, (int, float)) else 0
        dps = tdp.dp_map(device)
        target, _ = tdp.cover_position(dps)
        assert target is not None
        raw_percent = 100 - percent if device.settings.get("invert") else percent
        link.write({self._dp(dps, target): tdp.percent_to_raw(dps[target], raw_percent)})
        if percent <= 0:
            return Outcome.done(f"The {device.name} is closing.")
        if percent >= 100:
            return Outcome.done(f"The {device.name} is opening.")
        return Outcome.done(f"The {device.name} is moving to {percent}% open.")

    def _set_number(self, device: DeviceRecord, link: _Link, value: Value, code: str | None, unit: str) -> Outcome:
        dps = tdp.dp_map(device)
        assert code is not None and isinstance(value, (int, float))
        raw = tdp.number_to_raw(dps[code], float(value))
        link.write({self._dp(dps, code): raw})
        return Outcome.done(f"The {device.name} is set to {tdp.number_words(tdp.scaled(dps[code], raw) or 0)}{unit}.")

    def _set_temperature(self, device: DeviceRecord, link: _Link, value: Value) -> Outcome:
        return self._set_number(device, link, value, tdp.temperature_target(tdp.dp_map(device)), " degrees")

    def _set_humidity(self, device: DeviceRecord, link: _Link, value: Value) -> Outcome:
        return self._set_number(device, link, value, tdp.humidity_target(tdp.dp_map(device)), "% humidity")

    def _set_mode(self, device: DeviceRecord, link: _Link, value: Value) -> Outcome:
        dps = tdp.dp_map(device)
        code = tdp.mode_code(dps)
        link.write({self._dp(dps, code): tdp.enum_raw(dps[code or ""], str(value))})
        return Outcome.done(f"The {device.name} is in {tdp.words(str(value))} mode.")

    def _set_fan_speed(self, device: DeviceRecord, link: _Link, value: Value) -> Outcome:
        dps = tdp.dp_map(device)
        code = tdp.fan_speed(dps, device.kind)
        assert code is not None
        entry = dps[code]
        if entry["type"] == "int":
            percent = int(value) if isinstance(value, (int, float)) else 0
            link.write({self._dp(dps, code): tdp.percent_to_raw(entry, percent)})
            return Outcome.done(f"The {device.name} fan is at {percent}%.")
        link.write({self._dp(dps, code): tdp.enum_raw(entry, str(value))})
        return Outcome.done(f"The {device.name} fan speed is {tdp.words(str(value))}.")

    def _vacuum(self, device: DeviceRecord, link: _Link, _value: Value = None, *, action: str) -> Outcome:
        dps = tdp.dp_map(device)
        if action in ("start", "stop"):
            link.write({self._dp(dps, "power_go"): action == "start"})
            return Outcome.done(f"The {device.name} {'is starting to clean' if action == 'start' else 'has stopped'}.")
        if action == "pause":
            if tdp.find(dps, ("pause",), "bool"):
                link.write({self._dp(dps, "pause"): True})
            else:
                link.write({self._dp(dps, "power_go"): False})
            return Outcome.done(f"The {device.name} has paused.")
        if tdp.find(dps, ("switch_charge",), "bool"):
            link.write({self._dp(dps, "switch_charge"): True})
        else:
            mode = tdp.mode_code(dps)
            link.write({self._dp(dps, mode): tdp.enum_raw(dps[mode or ""], "chargego")})
        return Outcome.done(f"The {device.name} is going back to its dock.")

    # ------------------------------------------------------------------ scenes (cloud)

    def _activate(self, device: DeviceRecord) -> Outcome:
        home_id, scene_id = device.settings.get("home_id"), device.settings.get("scene_id")
        if not home_id or not scene_id:
            return Outcome.fail(
                "needs_setup", f"{device.name} is missing its Smart Life details; refresh it in home setup."
            )
        try:
            link = _usable_link(self.ctx.store.secret(CLOUD_SECRET))
        except StoreError:
            link = None
        if link is None:
            return Outcome.fail("needs_setup", NOT_LINKED)
        try:
            manager = _manager(_sharing(), link, _TokenKeeper(self.ctx.store, link))
            result = manager.trigger_scene(str(home_id), str(scene_id))
        except Exception as exc:  # noqa: BLE001 - every cloud failure becomes a plain sentence
            return _cloud_failure(exc, f"run {device.name}")
        if result is False:
            return Outcome.fail("failed", f"Smart Life did not run {device.name}.")
        return Outcome.done(f"Ran {device.name}.")


# --------------------------------------------------------------------------- saving


def _save_found(store: HomeStore, found: Mapping[str, Found]) -> None:
    """Saves new addresses: a device's own, and its gateway's on every sub-device of it."""

    def mutate(config: HomeConfig) -> None:
        for device in config.devices:
            if device.driver != TuyaDriver.name:
                continue
            target = device.settings.get("parent") or device.settings.get("tuya_id")
            hit = found.get(target) if isinstance(target, str) else None
            if hit is not None:
                device.settings["address"] = hit.address
                device.settings["version"] = hit.version

    try:
        store.update(mutate)
    except (StoreError, OSError):
        log.warning("could not save the new addresses of Tuya devices")


def _save_all(
    ctx: DriverContext,
    records: list[tuple[DeviceRecord, dict[str, Any] | None]],
    removed: list[DeviceRecord],
    link: dict[str, Any] | None,
) -> None:
    """Keys and the sign-in first, then devices.json in one write; if anything stops before that, the keys
    go back to what they were, so nothing is left half saved."""
    keys = [record.secret_key for record, secret in records if secret is not None]
    if link is not None:
        keys.append(CLOUD_SECRET)
    previous = {key: ctx.store.secret(key) for key in keys}
    committed = False
    try:
        for record, secret in records:
            if secret is not None:
                ctx.store.set_secret(record.secret_key, secret)
        if link is not None:
            ctx.store.set_secret(CLOUD_SECRET, link)

        def mutate(config: HomeConfig) -> None:
            for record, _ in records:
                config.upsert(record)
            for gone in removed:
                config.remove(gone.id)

        ctx.store.update(mutate)
        committed = True
    finally:
        if not committed:
            for key, value in previous.items():
                try:
                    ctx.store.set_secret(key, value)
                except (StoreError, OSError):
                    log.warning("could not undo a half-saved Tuya key")
    for gone in removed:
        try:
            ctx.store.set_secret(gone.secret_key, None)
        except (StoreError, OSError):
            log.warning("could not delete the key of a removed Tuya device")


# --------------------------------------------------------------------------- reading Smart Life


@dataclass(repr=False)
class _Candidate:
    """A device as Smart Life describes it, on its way to becoming one or more DeviceRecords."""

    tuya_id: str
    name: str
    category: str
    product: str
    key: str
    dps: DpMap
    sub: bool = False
    node_id: str | None = None
    parent: str | None = None
    parent_hint: str | None = None
    room: str | None = None
    invert: bool = False
    gang_names: dict[str, str] = field(default_factory=dict)
    kind: str | None = None
    reason: str | None = None

    def __repr__(self) -> str:  # never the key
        return f"_Candidate({self.tuya_id!r}, {self.name!r})"


@dataclass(frozen=True)
class _Scene:
    scene_id: str
    name: str
    home_id: str


def _spec(table: Any) -> dict[str, dict[str, Any]]:
    """The cloud spec (``device.function`` or ``device.status_range``) as plain dicts."""
    if not isinstance(table, Mapping):
        return {}
    spec: dict[str, dict[str, Any]] = {}
    for code, item in table.items():
        get = item.get if isinstance(item, Mapping) else functools.partial(getattr, item)
        if isinstance(code, str):
            spec[code] = {"type": get("type", None), "values": get("values", None), "dp_id": get("dp_id", None)}
    return spec


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else (str(value) if isinstance(value, int) else "")


def _gang_names(device: Any, dps: DpMap) -> dict[str, str]:
    """Names the user gave each switch in Smart Life, when the cloud passes them on (not documented)."""
    for attribute in ("dp_name", "dpName", "dp_names"):
        table = getattr(device, attribute, None)
        if not isinstance(table, Mapping):
            continue
        names = {}
        for code in tdp.gang_codes(dps):
            name = table.get(code) or table.get(str(dps[code]["dp"]))
            if isinstance(name, str) and name.strip():
                names[code] = name.strip()
        return names
    return {}


def _read_device(manager: Any, device: Any) -> _Candidate | None:
    tuya_id = _text(getattr(device, "id", None))
    if not tuya_id:
        return None
    strategy = getattr(device, "local_strategy", None)
    relations = tdp.relations_from_strategy(strategy) if isinstance(strategy, Mapping) else []
    if not relations:
        # The SDK keeps the local map only when every DP works locally; ask for it and keep those that do.
        try:
            response = manager.customer_api.get(f"/v1.0/m/life/devices/{tuya_id}/status")
            result = response.get("result") if isinstance(response, Mapping) else None
            dtos = result.get("dpStatusRelationDTOS") if isinstance(result, Mapping) else None
            relations = tdp.relations_from_dtos(dtos or [])
        except Exception as exc:  # noqa: BLE001 - one device's map must not end setup
            log.debug("no local DP map for a Tuya device: %s", type(exc).__name__)
    dps = tdp.build_dp_map(
        relations, _spec(getattr(device, "function", None)), _spec(getattr(device, "status_range", None))
    )
    category = _text(getattr(device, "category", None))
    status = getattr(device, "status", None)
    status = status if isinstance(status, Mapping) else {}
    room = None
    try:
        found = manager.query_room_by_device(tuya_id)
        room = _text(getattr(found, "name", None)) or None
    except Exception as exc:  # noqa: BLE001 - the room is a nicety
        log.debug("no Smart Life room for a device: %s", type(exc).__name__)
    parent_hint = next(
        (
            _text(getattr(device, attribute, None))
            for attribute in ("parent_id", "gateway_id", "parentId", "gatewayId")
            if _text(getattr(device, attribute, None))
        ),
        None,
    )
    node_id = _text(getattr(device, "node_id", None)) or None
    return _Candidate(
        tuya_id=tuya_id,
        name=_text(getattr(device, "name", None)) or "Tuya device",
        category=category,
        product=_text(getattr(device, "product_name", None)),
        key=_text(getattr(device, "local_key", None)),
        dps=dps,
        sub=bool(getattr(device, "sub", False)) or node_id is not None,
        node_id=node_id,
        parent_hint=parent_hint,
        room=room,
        # As Home Assistant: curtains count the other way unless their motor says "back".
        invert=category == "cl" or (category == "clkg" and status.get("control_back_mode") != "back"),
        gang_names=_gang_names(device, dps),
    )


def _read_cloud(manager: Any) -> tuple[list[_Candidate], list[_Scene], bool]:
    """Every device and enabled scene, and whether there is an IR remote (whose devices need scenes)."""
    manager.update_device_cache()
    candidates = [c for c in (_read_device(manager, d) for d in list(manager.device_map.values())) if c]
    scenes = []
    for scene in manager.query_scenes() or []:
        scene_id = _text(getattr(scene, "scene_id", None) or getattr(scene, "id", None))
        name = _text(getattr(scene, "name", None))
        if scene_id and name and getattr(scene, "enabled", True) is not False:
            scenes.append(_Scene(scene_id, name, _text(getattr(scene, "home_id", None))))
    has_ir = any(c.category in ("wnykq", "ykq") or c.category.startswith("infrared") for c in candidates)
    _settle(candidates)
    return candidates, scenes, has_ir


def _settle(candidates: list[_Candidate]) -> None:
    """Finds each sub-device's gateway (it shares the gateway's key) and decides every device's kind."""
    by_id = {c.tuya_id: c for c in candidates}
    gateways = [c for c in candidates if not c.sub and c.key]
    for candidate in candidates:
        if candidate.sub:
            parent = by_id.get(candidate.parent_hint or "")
            if parent is None or parent.sub:
                same_key = [g for g in gateways if candidate.key and g.key == candidate.key]
                parent = same_key[0] if len(same_key) == 1 else None
            if parent is None:
                candidate.reason = "paired to a hub Jarvis could not find"
                continue
            candidate.parent, candidate.key = parent.tuya_id, parent.key
        if not candidate.key:
            candidate.reason = "Smart Life gave no local key for it (shared devices sometimes have none)"
            continue
        candidate.kind = tdp.kind_for(candidate.category, candidate.dps)
        if candidate.kind is None:
            candidate.reason = tdp.skip_reason(candidate.category)


# --------------------------------------------------------------------------- the wizard


def wizard(ui: Prompter, ctx: DriverContext) -> None:
    """Links Smart Life, refreshes devices and scenes, finds devices on the network, or reverses a curtain."""
    ui.say("Smart Life / Tuya devices")
    mine = [d for d in ctx.store.load().devices if d.driver == TuyaDriver.name]
    actions: list[tuple[str, Callable[[Prompter, DriverContext], None]]] = [
        ("Link Smart Life (scan a QR code with your phone)", _link_flow)
    ]
    if _saved_link(ctx) is not None:
        actions.append(("Refresh devices and scenes from Smart Life (no QR code)", _refresh_flow))
    if any(d.kind != "scene" for d in mine):
        actions.append(("Find my Tuya devices on the network again", _find_flow))
    if any(d.kind == "cover" for d in mine):
        actions.append(("Reverse a curtain's position", _reverse_flow))
    if len(actions) == 1:
        _link_flow(ui, ctx)
        return
    choice = ui.choose("What would you like to do?", [label for label, _ in actions])
    if choice is not None:
        actions[choice][1](ui, ctx)


def _saved_link(ctx: DriverContext) -> dict[str, Any] | None:
    try:
        return _usable_link(ctx.store.secret(CLOUD_SECRET))
    except StoreError:
        return None


def _cloud_problem(exc: Exception) -> str:
    """What went wrong with Smart Life, for the setup console (codes, never replies)."""
    if getattr(exc, "error_code", None) is not None:
        if _expired(exc):
            return "The Smart Life link has expired: choose Link Smart Life to scan a new code."
        return f"Smart Life refused the request (error {_text(getattr(exc, 'error_code', '')) or '?'})."
    if isinstance(exc, OSError):
        return "Jarvis could not reach Smart Life. Check the internet connection and try again."
    log.warning("Smart Life gave an unexpected answer: %s", type(exc).__name__)
    return "Smart Life gave an answer Jarvis did not understand."


def _link_flow(ui: Prompter, ctx: DriverContext) -> None:
    ui.say(LINK_STEPS)
    user_code = ui.ask("Your Smart Life User Code").strip()
    if not user_code:
        ui.say("Nothing was saved.")
        return
    sharing = _sharing()
    control = sharing.LoginControl()
    try:
        response = control.qr_code(CLIENT_ID, SCHEMA, user_code)
    except Exception as exc:  # noqa: BLE001
        ui.say(_cloud_problem(exc))
        ui.say("Nothing was saved.")
        return
    result = response.get("result") if isinstance(response, Mapping) else None
    token = result.get("qrcode") if isinstance(result, Mapping) else None
    if not (isinstance(response, Mapping) and response.get("success")) or not isinstance(token, str) or not token:
        code = _text(response.get("code")) if isinstance(response, Mapping) else ""
        ui.say(
            f"Smart Life did not accept that User Code{f' (error {code})' if code else ''}. "
            "Check it in the app (Me > Settings > Account and Security) and try again."
        )
        return
    ui.show_qr(
        QR_PREFIX + token,
        "Scan this with the Smart Life app: tap + (top right), then Scan, and confirm the login on the phone.",
    )
    ui.say(f"Waiting for you to confirm on the phone (up to {int(LOGIN_WAIT_S // 60)} minutes; Ctrl+C stops)...")
    info = _wait_for_login(control, token, user_code)
    if info is None:
        ui.say("The code was not confirmed in time. Nothing was saved; choose Link Smart Life for a new code.")
        return
    link = {
        "user_code": user_code,
        "terminal_id": info["terminal_id"],
        "endpoint": info["endpoint"],
        "token_info": {name: info.get(name) for name in TOKEN_FIELDS},
    }
    ui.say("Signed in to Smart Life. Reading your homes, devices and scenes...")
    _import(ui, ctx, sharing, link, keeper=_TokenKeeper(None, link))


def _wait_for_login(control: Any, token: str, user_code: str) -> dict[str, Any] | None:
    """Asks every couple of seconds whether the phone confirmed; None after LOGIN_WAIT_S. Ctrl+C goes through."""
    deadline = time.monotonic() + LOGIN_WAIT_S
    while True:
        try:
            ok, info = control.login_result(token, CLIENT_ID, user_code)
        except (OSError, ValueError) as exc:  # a network hiccup or a non-JSON reply: keep waiting
            log.debug("Smart Life login check: %s", type(exc).__name__)
            ok, info = False, None
        if (
            ok
            and isinstance(info, dict)
            and all(isinstance(info.get(name), str) and info.get(name) for name in ("terminal_id", "endpoint"))
            and info.get("access_token")
            and info.get("refresh_token")
        ):
            return info
        if time.monotonic() >= deadline:
            return None
        time.sleep(LOGIN_POLL_S)


def _refresh_flow(ui: Prompter, ctx: DriverContext) -> None:
    link = _saved_link(ctx)
    if link is None:
        ui.say("Smart Life is not linked yet: choose Link Smart Life first.")
        return
    ui.say("Reading your homes, devices and scenes from Smart Life...")
    # Renewed tokens are saved at once: the old refresh token may stop working after a renewal.
    _import(ui, ctx, _sharing(), link, keeper=_TokenKeeper(ctx.store, link))


def _import(ui: Prompter, ctx: DriverContext, sharing: Any, link: dict[str, Any], keeper: _TokenKeeper) -> None:
    try:
        candidates, scenes, has_ir = _read_cloud(_manager(sharing, link, keeper))
    except Exception as exc:  # noqa: BLE001
        ui.say(_cloud_problem(exc))
        ui.say("Nothing was changed.")
        return
    usable = [c for c in candidates if c.kind is not None]
    targets = sorted({c.parent or c.tuya_id for c in usable})
    found: dict[str, Found] = {}
    if targets:
        ui.say(f"Looking for your devices on the home network (up to {int(SCAN_S)} seconds). {FIREWALL_HINT}")
        scanned = discover(targets, SCAN_S)
        if scanned is None:
            ui.say(
                "Jarvis could not listen for devices on the network (another program may be using the ports). "
                "They are saved without addresses; choose Find my Tuya devices here later."
            )
        found = scanned or {}
    config = ctx.store.load()
    records = _plan(ui, config, usable, scenes, found)
    removed = _gone(config, records, {c.tuya_id for c in candidates})
    _report(ui, records, [c for c in candidates if c.kind is None], has_ir)
    if not records:
        ui.say("Smart Life has no devices or scenes Jarvis can use. Nothing was changed.")
        return
    if removed and not ui.confirm(
        f"No longer in Smart Life: {', '.join(d.name for d in removed)}. Remove them from Jarvis?", default=True
    ):
        removed = []
    device_count = sum(1 for r, _ in records if r.kind != "scene")
    scene_count = len(records) - device_count
    if not ui.confirm(f"Save {_count(device_count, 'device')} and {_count(scene_count, 'scene')}?", default=True):
        ui.say("Nothing was saved.")
        return
    _save_all(ctx, records, removed, link)
    ui.say("Saved. Names and rooms come from Smart Life; to change them, choose Rename a device in the main menu.")
    reachable = [r for r, _ in records if r.kind != "scene" and r.settings.get("address")]
    if reachable and ui.confirm("Check now that Jarvis can reach each device? It takes a few seconds.", default=True):
        _check(ui, ctx, reachable)


def _gone(
    config: HomeConfig, records: list[tuple[DeviceRecord, dict[str, Any] | None]], seen: set[str]
) -> list[DeviceRecord]:
    """Saved Tuya devices and scenes Smart Life no longer has, or now has in another shape (e.g. per switch).
    A device Smart Life still lists but Jarvis now leaves out (say, its key is missing) is kept."""
    planned = {(r.settings.get("tuya_id"), r.settings.get("power")) for r, _ in records if r.kind != "scene"}
    planned_ids = {key[0] for key in planned}
    scenes = {r.settings.get("scene_id") for r, _ in records if r.kind == "scene"}
    gone = []
    for device in config.devices:
        if device.driver != TuyaDriver.name:
            continue
        tuya_id = device.settings.get("tuya_id")
        if device.kind == "scene":
            if device.settings.get("scene_id") not in scenes:
                gone.append(device)
        elif (tuya_id, device.settings.get("power")) not in planned and (tuya_id in planned_ids or tuya_id not in seen):
            gone.append(device)
    return gone


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def _plan(
    ui: Prompter, config: HomeConfig, usable: list[_Candidate], scenes: list[_Scene], found: Mapping[str, Found]
) -> list[tuple[DeviceRecord, dict[str, Any] | None]]:
    """The records to save (with each device's secret): existing ones keep their id, name, room and aliases."""
    existing = {
        (d.settings.get("scene_id"),) if d.kind == "scene" else (d.settings.get("tuya_id"), d.settings.get("power")): d
        for d in config.devices
        if d.driver == TuyaDriver.name
    }
    ids = config.ids()
    names = {normalize(d.name) for d in config.devices}
    records: list[tuple[DeviceRecord, dict[str, Any] | None]] = []

    def fresh_name(wanted: str) -> str:
        name, n = wanted, 2
        while normalize(name) in names:
            name, n = f"{wanted} {n}", n + 1
        names.add(normalize(name))
        return name

    def add(old: DeviceRecord | None, name: str, kind: str, room: str | None, settings: dict[str, Any]) -> DeviceRecord:
        if old is not None:
            record = replace(old, kind=kind, settings=settings)
        else:
            record = DeviceRecord(unique_id(ids, TuyaDriver.name, name), TuyaDriver.name, name, kind, room)
            record.settings = settings
        ids.add(record.id)
        return record

    for candidate in usable:
        assert candidate.kind is not None
        target = candidate.parent or candidate.tuya_id
        hit = found.get(target)
        gangs = tdp.gang_codes(candidate.dps) if candidate.kind in ("switch", "plug") else []
        parts: list[tuple[str | None, DpMap]] = (
            [(code, {code: candidate.dps[code]}) for code in gangs] if len(gangs) > 1 else [(None, candidate.dps)]
        )
        for index, (power, dps) in enumerate(parts, 1):
            old = existing.get((candidate.tuya_id, power))
            settings: dict[str, Any] = {
                "tuya_id": candidate.tuya_id,
                "category": candidate.category,
                "product": candidate.product,
            }
            address = hit.address if hit else (old.settings.get("address") if old else None)
            version = hit.version if hit else (old.settings.get("version") if old else None)
            if address and version:
                settings["address"], settings["version"] = address, version
            if candidate.parent:
                settings["parent"] = candidate.parent
                if candidate.node_id:
                    settings["node_id"] = candidate.node_id
            if power:
                settings["power"] = power
            if candidate.kind == "cover":
                settings["invert"] = bool(old.settings.get("invert")) if old else candidate.invert
            settings["dps"] = dps
            if old is not None:
                name = old.name
            elif power:
                label = "USB" if power.startswith("switch_usb") else str(index)
                suggested = candidate.gang_names.get(power) or f"{candidate.name} {label}"
                if power in candidate.gang_names:
                    name = fresh_name(suggested)
                else:
                    asked = ui.ask(f"A name for switch {label} of the {candidate.name}", suggested).strip()
                    name = fresh_name(asked or suggested)
            else:
                name = fresh_name(candidate.name)
            record = add(old, name, candidate.kind, candidate.room, settings)
            records.append((record, {"local_key": candidate.key}))
    for scene in scenes:
        old = existing.get((scene.scene_id,))
        name = old.name if old else fresh_name(scene.name)
        record = add(old, name, "scene", None, {"home_id": scene.home_id, "scene_id": scene.scene_id})
        records.append((record, None))
    return records


def _report(
    ui: Prompter, records: list[tuple[DeviceRecord, dict[str, Any] | None]], left_out: list[_Candidate], has_ir: bool
) -> None:
    devices = [r for r, _ in records if r.kind != "scene"]
    scenes = [r for r, _ in records if r.kind == "scene"]
    if devices:
        ui.say(f"{_count(len(devices), 'device')}:")
        for record in devices:
            where = f"at {record.settings['address']}" if record.settings.get("address") else "not found on the network"
            room = f", {record.room}" if record.room else ""
            ui.say(f"  {record.name} ({record.kind}{room}): {where}")
    if scenes:
        ui.say(f"{_count(len(scenes), 'scene')}: {', '.join(r.name for r in scenes)}")
    if left_out:
        ui.say("Not added: " + "; ".join(f"{c.name} ({c.reason})" for c in left_out))
    missing = [r.name for r in devices if not r.settings.get("address")]
    if missing:
        ui.say(
            f"Not found on your home network: {', '.join(missing)}. They are saved anyway: battery devices sleep "
            "and others may be switched off. Jarvis looks for them again when they are used."
        )
    if has_ir:
        ui.say(IR_ADVICE)
    elif not scenes:
        ui.say("Tip: tap-to-run scenes you make in Smart Life appear here after Refresh devices and scenes.")


def _check(ui: Prompter, ctx: DriverContext, records: list[DeviceRecord]) -> None:
    driver = TuyaDriver(ctx)
    checked: set[str] = set()
    for record in records:
        device_id = str(record.settings.get("tuya_id"))
        if device_id in checked:
            continue
        checked.add(device_id)
        ui.say(f"  {driver.status(record).text}")


def _find_flow(ui: Prompter, ctx: DriverContext) -> None:
    devices = [d for d in ctx.store.load().devices if d.driver == TuyaDriver.name and d.kind != "scene"]
    targets = {str(d.settings.get("parent") or d.settings.get("tuya_id")) for d in devices}
    ui.say(f"Looking for your devices on the home network (up to {int(SCAN_S)} seconds). {FIREWALL_HINT}")
    found = discover(targets, SCAN_S)
    if found is None:
        ui.say("Jarvis could not listen for devices on the network (another program may be using the ports).")
        return
    _save_found(ctx.store, found)
    for device in devices:
        hit = found.get(str(device.settings.get("parent") or device.settings.get("tuya_id")))
        ui.say(f"  {device.name}: {f'at {hit.address}' if hit else 'not found'}")
    if any(str(d.settings.get("parent") or d.settings.get("tuya_id")) not in found for d in devices):
        ui.say("Devices not found may be switched off, asleep (battery devices) or on another network.")


def _reverse_flow(ui: Prompter, ctx: DriverContext) -> None:
    covers = [d for d in ctx.store.load().devices if d.driver == TuyaDriver.name and d.kind == "cover"]
    index = ui.choose(
        "Which curtain goes the wrong way when Jarvis sets its position?",
        [
            f"{d.name} ({'counted the other way' if d.settings.get('invert') else 'as the motor counts'})"
            for d in covers
        ],
    )
    if index is None:
        return
    chosen = covers[index]

    def flip(config: HomeConfig) -> None:
        device = config.device(chosen.id)
        if device is not None:
            device.settings["invert"] = not device.settings.get("invert")

    ctx.store.update(flip)
    ui.say(f"Saved. Jarvis now counts the {chosen.name}'s position the other way round.")


__all__ = ["CLIENT_ID", "CLOUD_SECRET", "SCHEMA", "Found", "TuyaDriver", "discover", "wizard"]
