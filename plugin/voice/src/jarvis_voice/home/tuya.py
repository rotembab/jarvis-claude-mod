"""Tuya devices over the home network (tinytuya), linked once through the Tuya Smart or Smart Life app.

Setup (``wizard``) signs in to the user's Tuya account the way Home Assistant's
Tuya integration does: the user types the User Code shown in the app (Tuya
Smart or Smart Life: separate accounts, so the same app throughout), Jarvis
shows a QR code, and the app scans and confirms it. tuya_sharing
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

If the user turns it on in setup (it is off unless they do), a command a device
does not answer on the home network goes through Tuya's cloud instead, and so do
devices Jarvis has no local key or hub for. The handlers stay the same: a
``_CloudLink`` reads and writes DP numbers like ``_Link`` and turns them into the
cloud's codes and values. Every cloud call, scenes included, goes through one
shared session, one call at a time.
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
from .model import Code, CommandSpec, DeviceRecord, HomeConfig, Outcome, Value, normalize, unique_id
from .store import HomeStore, StoreError
from .tuya_dps import DpMap

log = logging.getLogger(__name__)

# Home Assistant's Tuya app registration (see the module docstring: unofficial).
CLIENT_ID = "HA_3y9q4ak7g4ephrvke"
SCHEMA = "haauthorize"
QR_PREFIX = "tuyaSmart--qrLogin?token="
# The Tuya sign-in, in the credential store: user_code, terminal_id, endpoint, token_info, and the user's
# choice (true or false, not a secret) whether commands may fall back to the cloud (FALLBACK; missing = no).
CLOUD_SECRET = "tuya:cloud"
TOKEN_FIELDS = ("t", "uid", "expire_time", "access_token", "refresh_token")
FALLBACK = "cloud_fallback"

# Tuya cloud calls: requests' (connect, read) timeouts instead of the SDK's 60 s.
CLOUD_TIMEOUT_S = (3.5, 5.0)
LOGIN_WAIT_S = 180.0
LOGIN_POLL_S = 2.0
# login_result's failure codes that end the wait at once (seen in user reports, not documented): the QR
# code expired, or the phone refused the login ("use the designated APP": the code came from the other app).
QR_EXPIRED = "E0020002"
LOGIN_REFUSED = "E0020003"
# How long the setup scan listens (tinytuya's own default; it stops early once every device answered).
SCAN_S = 18.0
# A short scan from the helper when a device stops answering at its saved address, at most once per gap;
# never shorter than RESCAN_MIN_S (less hears too little), and only when the retry still fits after it.
RESCAN_S = 6.0
RESCAN_MIN_S = 3.0
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
# Commands that make two requests' worth of work (a read and a write, or several values at once).
TWO_REQUESTS = frozenset({"toggle", "press", "set_brightness", "set_color", "set_color_temp"})

# tinytuya's error numbers (core/error_helper.py).
ERR_UNREACHABLE = frozenset({"901", "905"})
ERR_TIMEOUT = "902"
ERR_RANGE = "903"
ERR_KEY = frozenset({"904", "914"})
ERR_DEVTYPE = "908"
RESCAN_ERRORS = ERR_UNREACHABLE | ERR_KEY | {ERR_TIMEOUT}

# Tuya cloud error codes that mean the sign-in is no longer valid (not documented; seen in practice).
TOKEN_ERRORS = frozenset({"1010", "1011", "1012", "1013"})
# Codes Tuya's developer docs give other meanings (1100 param is empty, 1106 permission deny) but that were
# seen with a failing sign-in, and the gateway's "sign invalid": worded softly, as maybe the sign-in.
SIGNIN_ERRORS = frozenset({"1100", "1106"})
# Tuya's cloud is limiting requests or failing: no cloud calls for CLOUD_PAUSE_S after either.
BUSY_ERRORS = ("API_QPS_LIMIT_OR_DEGRADE", "SYSTEM_ERROR")

# The cloud fallback: the time kept for it out of the run budget when it is on (a token renewal, a read and a
# command), and the least time worth starting a cloud call with.
CLOUD_SLICE_S = 6.0
CLOUD_MIN_S = 2.5
CLOUD_PAUSE_S = 60.0
# Local failures after which a command is tried again through the cloud; never after a refused value (903).
FALLBACK_ERRORS = ERR_UNREACHABLE | ERR_KEY | {ERR_TIMEOUT, "error", "nodata", "noaddress", "budget"}

EXPIRED = "The Tuya link has expired. Open home setup and choose Link your Tuya account to scan a new code."
NOT_LINKED = "Your Tuya account is not linked, so Jarvis cannot run its scenes. Link it in home setup."
SIGNIN_REFUSED = (
    "Tuya's cloud turned Jarvis's sign-in away. If it keeps happening, link your Tuya account again in home setup."
)
CLOUD_BUSY = "Tuya's cloud is limiting requests; try again in a minute."
CLOUD_QUESTION = "Also use Tuya's cloud when a device does not answer on your home network?"
CLOUD_EXPLAINED = (
    "Jarvis controls your Tuya devices over your home network. It can also send a command through Tuya's cloud, "
    "over the internet, when a device does not answer there or Jarvis cannot reach it locally at all. A Bluetooth "
    "device with no Tuya gateway cannot be reached either way."
)

LINK_STEPS = """\
Jarvis signs in once to the app your Tuya devices are in, Tuya Smart or Smart Life, to read
each device's local key and its scenes. After that it controls the devices directly over your
home network. Use the same app for every step: the two apps have separate accounts.

1. In the app, open Me > the gear (top right) > Account and Security. Your User Code is
   at the bottom. It is case-sensitive.
2. Type it here. Jarvis then shows a QR code.
3. In the same app, go to the Home tab and tap + (top right) > Scan. Scan the code, then
   tap Confirm login. Use the app's scanner, not the phone's camera.
   Your phone may say the login is for Home Assistant: Jarvis signs in the same way
   Home Assistant's Tuya integration does."""

IR_ADVICE = (
    "Jarvis cannot drive an air conditioner or TV through an IR blaster directly. In the Tuya Smart or Smart "
    'Life app, make a Tap-to-Run scene for each thing you want, such as "AC cool 24" and "AC off", then '
    "choose Refresh devices and scenes here: Jarvis can run those scenes."
)
# Why a button pusher that is not paired to a gateway is left out: it talks Bluetooth, to the phone only.
BLUETOOTH_ONLY = (
    "a Bluetooth device, which Jarvis can reach only through a Tuya Bluetooth or multi-mode gateway: "
    "add one in the Tuya Smart or Smart Life app, then refresh here"
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
        # For a replay through the cloud: what was read, and whether a write was tried.
        self.last_read: dict[str, Any] | None = None
        self.wrote = False

    def affordable(self, units: int, reserve: int = 0) -> bool:
        return self.budget.timeout(units, self.factor, reserve) is not None

    def seed(self) -> dict[str, Any] | None:
        """What a replay of the command must read: what this link read, or nothing at all once it tried to write
        (so the replay writes the same absolute values and never flips a toggle twice); None to read afresh."""
        if self.wrote:
            return dict(self.last_read or {})
        return None if self.last_read is None else dict(self.last_read)

    def read(self, reserve: int = 0) -> dict[str, Any]:
        """The device's DPs, by number as text."""
        result = self._request(1, reserve, lambda device: self._status(device, reserve))
        values = result.get("dps") if isinstance(result, dict) else None
        if not isinstance(values, dict):
            raise _Failure("nodata")
        self.last_read = {str(k): v for k, v in values.items()}
        return dict(self.last_read)

    def _status(self, device: Any, reserve: int) -> Any:
        result = device.status()
        # A "device22" reports only the DPs it is asked for. tinytuya spots the dialect in the first reply
        # and asks again for DP 1 alone, so ask once more for the DPs Jarvis uses (power may be DP 20).
        wanted = {str(dp): None for dp in self.conn.dps}
        if (
            _error_code(result) is not None
            or getattr(device, "dev_type", None) != "device22"
            or not wanted
            or getattr(device, "dps_to_request", None) == wanted
        ):
            return result
        timeout = self.budget.timeout(1, self.factor, reserve)  # a request of its own, sized to what is left
        if timeout is None:
            return result
        for item in (device, getattr(device, "parent", None)):
            if item is not None:
                item.set_socketTimeout(timeout)
        device.dps_to_request = wanted
        return device.status()

    def write(self, values: Mapping[int, Any], reserve: int = 0) -> None:
        data = {str(dp): value for dp, value in values.items()}
        self.wrote = self.wrote or bool(data)
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


# --------------------------------------------------------------------------- the Tuya cloud


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
                # Onto the saved link, so a choice made in setup since (the cloud fallback) is kept; not at all
                # when the account was linked again since, as these tokens belong to the old sign-in.
                saved = self.store.secret(CLOUD_SECRET)
                if isinstance(saved, dict) and saved.get("terminal_id") != self.link.get("terminal_id"):
                    return
                record = {**(saved if isinstance(saved, dict) else self.link), "token_info": self.link["token_info"]}
                self.store.set_secret(CLOUD_SECRET, record)
            except (StoreError, OSError):
                log.warning("could not save the renewed Tuya sign-in")


class _Late(Exception):
    """A cloud call that did not finish within its time. ``started``: it was sent, so it may still take effect
    (not when it only waited for another call to finish)."""

    def __init__(self, started: bool = False) -> None:
        super().__init__()
        self.started = started


def _within(seconds: float, call: Callable[[], Any]) -> Any:
    """``call()`` on a thread of its own, waited for at most ``seconds`` (then _Late). A blocking HTTP request
    cannot be stopped, so a late call carries on in the background and its result is dropped."""
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["result"] = call()
        except BaseException as exc:  # noqa: BLE001 - handed to the waiting caller
            box["error"] = exc

    thread = threading.Thread(target=target, name="tuya-cloud", daemon=True)
    thread.start()
    thread.join(max(0.0, seconds))
    if thread.is_alive():
        raise _Late()
    if "error" in box:
        raise box["error"]
    return box.get("result")


def _usable_link(link: Any) -> dict[str, Any] | None:
    if not isinstance(link, dict):
        return None
    token = link.get("token_info")
    fields = ("user_code", "terminal_id", "endpoint")
    if not all(isinstance(link.get(name), str) and link.get(name) for name in fields) or not isinstance(token, dict):
        return None
    return link if token.get("access_token") and token.get("refresh_token") else None


def _issued(link: Mapping[str, Any]) -> float:
    """When Tuya issued a link's tokens (its own clock, in ms), to tell the newer of two; 0 when not known."""
    token = link.get("token_info")
    t = token.get("t") if isinstance(token, Mapping) else None
    return t if isinstance(t, (int, float)) and not isinstance(t, bool) else 0


def _manager(sharing: Any, link: dict[str, Any], keeper: _TokenKeeper) -> Any:
    return sharing.Manager(
        CLIENT_ID, link["user_code"], link["terminal_id"], link["endpoint"], dict(link["token_info"]), keeper
    )


def _expired(exc: BaseException) -> bool:
    code = str(getattr(exc, "error_code", "") or "")
    message = str(getattr(exc, "error_message", "") or "").lower()
    return code in TOKEN_ERRORS or any(word in message for word in ("token", "expire", "login", "auth"))


def _busy(exc: BaseException) -> bool:
    words = f"{getattr(exc, 'error_code', '') or ''} {getattr(exc, 'error_message', '') or ''}".upper()
    return getattr(exc, "error_code", None) is not None and any(word in words for word in BUSY_ERRORS)


def _cloud_kind(exc: BaseException) -> str:
    """What a failed cloud call came to: busy, signin, expired, refused, unreachable or odd (an answer Jarvis
    could not read). Only the exception's type and Tuya's code are looked at, never logged beyond those."""
    if getattr(exc, "error_code", None) is not None:
        code = str(getattr(exc, "error_code", "") or "")
        message = str(getattr(exc, "error_message", "") or "").lower()
        if "API_QPS_LIMIT_OR_DEGRADE" in f"{code} {message}".upper():
            return "busy"
        if code in SIGNIN_ERRORS or "sign invalid" in message:
            return "signin"  # as Home Assistant does: the sign-in, maybe
        return "expired" if _expired(exc) else "refused"
    if isinstance(exc, ValueError):  # a reply that is not JSON (requests' JSONDecodeError is an OSError too)
        return "odd"
    if isinstance(exc, OSError):  # requests' errors are OSErrors
        return "unreachable"
    return "odd"


def _cloud_failure(exc: Exception, doing: str) -> Outcome:
    """An Outcome for a failed Tuya cloud call; the reply itself never reaches the text."""
    kind = _cloud_kind(exc)
    if kind in ("busy", "signin", "refused"):
        log.warning("Tuya refused a request (error %s)", getattr(exc, "error_code", "?"))
    if kind == "busy":
        return Outcome.fail("busy", CLOUD_BUSY)
    if kind == "signin":
        return Outcome.fail("auth", SIGNIN_REFUSED)
    if kind == "expired":
        return Outcome.fail("auth", EXPIRED)
    if kind == "refused":
        return Outcome.fail("failed", f"Tuya could not {doing}.")
    if kind == "unreachable":
        return Outcome.fail("unreachable", f"Jarvis could not reach Tuya to {doing}. Is the internet working?")
    log.warning("Tuya gave an unexpected answer: %s", type(exc).__name__)
    return Outcome.fail("failed", f"Tuya gave an unexpected answer, so Jarvis could not {doing}.")


class _CloudFailure(_Failure):
    """A cloud request that did not work: offline, late, busy, signin, expired, refused, unreachable, odd,
    refresh (the saved map lacks the cloud's form of a value) or budget (too little time to start)."""


class _Paused(Exception):
    """Tuya's cloud said it is limiting requests or failing, a short while ago: no cloud calls for now."""


class _CloudSession:
    """The one Tuya cloud session of the helper: one Manager built from the saved link (built again when the link
    changes), so tokens are renewed once and never twice at the same time; one call at a time, scenes included;
    and no calls for CLOUD_PAUSE_S after Tuya said it is limiting requests or failing."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.clock: Callable[[], float] = time.monotonic  # for the pause only; tests replace it
        self.paused_until = float("-inf")
        self._keeper: _TokenKeeper | None = None
        self._manager: Any = None

    def paused(self) -> bool:
        return self.clock() < self.paused_until

    def call(self, store: HomeStore, link: dict[str, Any], seconds: float, action: Callable[[Any], Any]) -> Any:
        """``action(manager)`` within ``seconds`` (then _Late), waiting for the lock within them too."""
        if self.paused():
            raise _Paused()
        deadline = time.monotonic() + seconds
        # Whether the action started and whether the caller gave up are settled under one lock, so a late call
        # either never starts or is known to have started.
        guard = threading.Lock()
        state = {"started": False, "gave_up": False}

        def locked() -> Any:
            if not self.lock.acquire(timeout=max(0.0, seconds)):
                raise _Late()
            try:
                with guard:
                    if state["gave_up"] or time.monotonic() >= deadline:
                        raise _Late()  # the caller has given up: do not send it now
                    if self.paused():
                        raise _Paused()
                    state["started"] = True
                try:
                    return action(self._manager_for(store, link))
                except Exception as exc:
                    if _busy(exc):
                        log.warning("Tuya's cloud is limiting requests: no cloud calls for %d s", CLOUD_PAUSE_S)
                        self.paused_until = self.clock() + CLOUD_PAUSE_S
                    raise
            finally:
                self.lock.release()

        try:
            return _within(seconds, locked)
        except _Late:
            with guard:
                state["gave_up"] = True
                raise _Late(state["started"]) from None

    def _manager_for(self, store: HomeStore, link: dict[str, Any]) -> Any:
        def identity(item: Mapping[str, Any]) -> tuple[Any, ...]:
            return (item.get("user_code"), item.get("terminal_id"), item.get("endpoint"))

        def refresh_token(item: Mapping[str, Any]) -> Any:
            token = item.get("token_info")
            return token.get("refresh_token") if isinstance(token, Mapping) else None

        # The keeper's copy follows the session's own renewals, so only a new link (or a renewal by the setup
        # window) builds a new Manager.
        keeper = self._keeper
        if keeper is not None and keeper.store is store and identity(link) == identity(keeper.link):
            if refresh_token(link) == refresh_token(keeper.link):
                return self._manager
            if _issued(link) < _issued(keeper.link):
                # Older tokens than the session's own: its renewal could not be saved, or was saved over. The old
                # refresh token may no longer work, so the session stays and its tokens are saved again.
                keeper.update_token(keeper.link["token_info"])
                return self._manager
        keeper = _TokenKeeper(store, dict(link))
        self._manager = _manager(_sharing(), keeper.link, keeper)
        self._keeper = keeper
        return self._manager


_CLOUD = _CloudSession()


def _cloud_send(manager: Any, tuya_id: str, commands: list[dict[str, Any]]) -> None:
    """One command list for one device. Not ``manager.send_commands``: that drops the answer (so a failure looks
    like success) and skips a list it sent in the last 10 s."""
    response = manager.customer_api.post(f"/v1.1/m/thing/{tuya_id}/commands", None, {"commands": commands})
    if not isinstance(response, Mapping) or response.get("success") is not True:
        raise _CloudFailure("odd")  # None: an HTTP error, which the SDK logs and swallows


def _cloud_status(manager: Any, tuya_id: str) -> tuple[bool, dict[str, Any]]:
    """(online, code -> value) for one device, in one request (the SDK's own query makes five per device).
    The reply also carries the device's local key: it is read here and never kept, printed or logged."""
    response = manager.customer_api.get("/v1.0/m/life/ha/devices/detail", {"devIds": tuya_id})
    result = response.get("result") if isinstance(response, Mapping) and response.get("success") is True else None
    item = next(
        (r for r in result if isinstance(r, Mapping) and r.get("id") == tuya_id) if isinstance(result, list) else (),
        None,
    )
    if item is None:
        raise _CloudFailure("odd")
    status = {
        s["code"]: s.get("value")
        for s in item.get("status") or []
        if isinstance(s, Mapping) and isinstance(s.get("code"), str) and "value" in s
    }
    return item.get("online") is not False, status


class _CloudLink:
    """One command's requests to one device through Tuya's cloud, with _Link's interface: values go in and come
    out by DP number in the device's local form, so the handlers and describe_state work unchanged.

    ``seed`` is what the local attempt read (see _Link.seed): read() answers from it without asking the cloud,
    so a replay writes the same absolute values the local attempt meant to."""

    def __init__(
        self,
        store: HomeStore,
        link: dict[str, Any],
        tuya_id: str,
        dps: DpMap,
        budget: _Budget,
        seed: dict[str, Any] | None = None,
    ) -> None:
        self.store = store
        self.link = link
        self.tuya_id = tuya_id
        self.dps = dps
        self.budget = budget
        self.values: dict[str, Any] | None = seed
        self.sent = False

    def __repr__(self) -> str:  # never the link's tokens
        return f"_CloudLink({self.tuya_id!r})"

    def affordable(self, units: int, reserve: int = 0) -> bool:
        return self.values is not None or self.budget.left() - reserve * CLOUD_MIN_S >= CLOUD_MIN_S

    def read(self, reserve: int = 0) -> dict[str, Any]:
        if self.values is None:
            online, status = self._call(reserve, lambda manager: _cloud_status(manager, self.tuya_id))
            if not online:
                raise _CloudFailure("offline")
            values: dict[str, Any] = {}
            for code, entry in self.dps.items():
                value = tdp.from_cloud(entry, status.get(code))
                if value is not None:
                    values[str(entry["dp"])] = value
            self.values = values
        return dict(self.values)

    def write(self, values: Mapping[int, Any], reserve: int = 0) -> None:
        by_dp = {entry["dp"]: (code, entry) for code, entry in self.dps.items() if not entry.get("ro")}
        commands = []
        for dp, value in values.items():
            if dp not in by_dp:
                raise _CloudFailure("refresh")
            code, entry = by_dp[dp]
            try:
                commands.append({"code": code, "value": tdp.to_cloud(entry, value)})
            except ValueError:
                raise _CloudFailure("refresh") from None
        if commands:
            # Every value in one request, as Home Assistant sends a light's power, mode and colour together.
            self._call(reserve, lambda manager: _cloud_send(manager, self.tuya_id, commands), sending=True)

    def _call(self, reserve: int, action: Callable[[Any], Any], *, sending: bool = False) -> Any:
        seconds = self.budget.left() - reserve * CLOUD_MIN_S
        if seconds < CLOUD_MIN_S:
            raise _CloudFailure("budget")
        try:
            result = _CLOUD.call(self.store, self.link, seconds, action)
        except _Late as late:
            self.sent = self.sent or (sending and late.started)  # not a write that waited for another call in vain
            log.warning("Tuya's cloud did not answer in time for device %s", self.tuya_id)
            raise _CloudFailure("late") from None
        except _Paused:
            raise _CloudFailure("busy") from None
        except _CloudFailure as failure:
            log.warning("Tuya's cloud gave an unexpected answer for device %s", self.tuya_id)
            raise _CloudFailure(failure.err) from None
        except Exception as exc:  # noqa: BLE001 - every cloud failure becomes a plain sentence
            kind = _cloud_kind(exc)
            # The code or the type only: a requests error's text carries its URL, which may hold the refresh token.
            code = getattr(exc, "error_code", None)
            log.warning("Tuya's cloud failed for device %s (%s)", self.tuya_id, code or type(exc).__name__)
            raise _CloudFailure(kind) from None
        self.sent = self.sent or sending
        return result


# --------------------------------------------------------------------------- the driver

_AnyLink = _Link | _CloudLink
Handler = Callable[[DeviceRecord, _AnyLink, Value], Outcome]


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
        self._background: threading.Thread | None = None  # the last scan started from a cloud fallback
        self._handlers: dict[str, Handler] = {
            "turn_on": functools.partial(self._power, on=True),
            "turn_off": functools.partial(self._power, on=False),
            "toggle": self._toggle,
            "press": self._press,
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
            return Outcome.fail("unsupported", f"{device.name} is a Tuya scene; it can only be run.")
        if command not in {spec.name for spec in self.commands(device)}:
            return Outcome.fail("unsupported", f"The {device.name} can't {command.replace('_', ' ')}.")
        handler = self._handlers.get(command)
        if command == "stop":
            handler = functools.partial(self._vacuum, action="stop") if device.kind == "vacuum" else self._stop
        if handler is None:
            return Outcome.fail("unsupported", f"The {device.name} can't {command.replace('_', ' ')}.")
        units = 2 if command in TWO_REQUESTS else 1
        return self._with_link(device, lambda link: handler(device, link, value), units)

    def status(self, device: DeviceRecord) -> Outcome:
        if device.kind == "scene":
            return Outcome.fail("unsupported", f"{device.name} is a Tuya scene; it has no state to read.")
        return self._with_link(device, lambda link: Outcome.done(tdp.describe_state(device, link.read())))

    def describe(self) -> str | None:
        try:
            config = self.ctx.store.load()
            mine = [d for d in config.devices if d.driver == self.name]
            cloud_only = [d.name for d in mine if d.kind != "scene" and d.settings.get("cloud_only") is True]
            keyless = [
                d.name
                for d in mine
                if d.kind != "scene"
                and d.name not in cloud_only
                and not (self.ctx.store.secret(d.secret_key) or {}).get("local_key")
            ]
            link = _usable_link(self.ctx.store.secret(CLOUD_SECRET))
        except StoreError:
            return "Tuya: the saved keys could not be read; refresh your Tuya devices in home setup."
        fallback = link is not None and link.get(FALLBACK) is True
        notes = []
        if keyless:
            notes.append(f"no key saved for {', '.join(keyless[:5])}; refresh your Tuya devices in home setup")
        if cloud_only:
            names = ", ".join(cloud_only[:5])
            notes.append(
                f"{names} work only through Tuya's cloud"
                + ("" if fallback else ", which is turned off; turn it on in home setup")
            )
        if fallback:
            notes.append("devices that do not answer on the home network are tried through Tuya's cloud")
        if any(d.kind == "scene" for d in mine) and link is None:
            notes.append("your Tuya account is not linked, so its scenes cannot run; link it in home setup")
        return f"Tuya: {'; '.join(notes)}." if notes else None

    # ------------------------------------------------------------------ plumbing

    def _budget_s(self) -> float:
        return max(1.0, min(RUN_BUDGET_S, self.ctx.call_timeout - CALL_MARGIN_S))

    def _conn(self, device: DeviceRecord) -> _Conn | Outcome:
        settings = device.settings
        tuya_id = settings.get("tuya_id")
        if not isinstance(tuya_id, str) or not tuya_id:
            return Outcome.fail(
                "needs_setup",
                f"The {device.name} is missing its Tuya details; refresh your Tuya devices in home setup.",
            )
        if settings.get("cloud_only") is True:
            return Outcome.fail(
                "needs_setup",
                f"The {device.name} works only through Tuya's cloud, which is turned off for Jarvis. Turn it on in "
                "home setup, under Tuya devices.",
            )
        try:
            secret = self.ctx.store.secret(device.secret_key) or {}
        except StoreError:
            return Outcome.fail(
                "needs_setup",
                f"Jarvis could not read the {device.name}'s key; refresh your Tuya devices in home setup.",
            )
        key = secret.get("local_key")
        if not isinstance(key, str) or not key:
            return Outcome.fail(
                "needs_setup", f"Jarvis has no key for the {device.name}; refresh your Tuya devices in home setup."
            )
        address = settings.get("address")
        parent, node_id = settings.get("parent"), settings.get("node_id")
        parent = parent if isinstance(parent, str) and parent else None
        node_id = str(node_id) if isinstance(node_id, (str, int)) and str(node_id) else None
        if parent is not None and node_id is None:
            # Sent without the sub-device's node id, a command would reach only the hub.
            return Outcome.fail(
                "needs_setup",
                f"The {device.name} is missing its Tuya details; refresh your Tuya devices in home setup.",
            )
        return _Conn(
            tuya_id,
            address if isinstance(address, str) and address else None,
            _version(settings.get("version")),
            key,
            parent,
            node_id,
            tuple(sorted({entry["dp"] for entry in tdp.dp_map(device).values()})),
        )

    def _with_link(self, device: DeviceRecord, work: Callable[[_AnyLink], Outcome], units: int = 1) -> Outcome:
        """Runs ``work`` (``units`` requests at most); when the device does not answer at its saved address,
        looks for it once and retries. With the cloud fallback on, it goes to the cloud instead (see
        _with_cloud)."""
        conn = self._conn(device)
        cloud = self._cloud_route(device)
        if cloud is not None:
            if isinstance(conn, Outcome):
                # No key, a hub Jarvis could not match, or saved as reachable only through the cloud.
                return self._through_cloud(device, work, cloud, _Budget(self._budget_s()))
            if not _CLOUD.paused() and self._budget_s() - CLOUD_SLICE_S >= CLOUD_MIN_S:
                return self._with_cloud(device, conn, work, cloud, units)
        # Without the cloud (off, paused, or too little time for it), exactly as before it existed.
        if isinstance(conn, Outcome):
            return conn
        budget = _Budget(self._budget_s())
        rescanned = False
        if conn.address is None or conn.version is None:
            rescanned = True
            found = self._rediscover(conn, budget, units)
            if found is None:
                return self._failure(device, _Failure("noaddress"))
            conn = found
        try:
            return work(_Link(conn, budget, _open_device))
        except _Failure as failure:
            if rescanned or failure.err not in RESCAN_ERRORS:
                return self._failure(device, failure)
            fresh = self._rediscover(conn, budget, units)
            if fresh is None or (fresh.address, fresh.version) == (conn.address, conn.version):
                return self._failure(device, failure)
            try:
                return work(_Link(fresh, budget, _open_device))
            except _Failure as again:
                return self._failure(device, again)

    def _cloud_route(self, device: DeviceRecord) -> dict[str, Any] | None:
        """The saved Tuya link when the user turned the cloud fallback on; None otherwise."""
        tuya_id = device.settings.get("tuya_id")
        if not isinstance(tuya_id, str) or not tuya_id:
            return None
        try:
            link = _usable_link(self.ctx.store.secret(CLOUD_SECRET))
        except StoreError:
            return None
        return link if link is not None and link.get(FALLBACK) is True else None

    def _with_cloud(
        self, device: DeviceRecord, conn: _Conn, work: Callable[[_AnyLink], Outcome], cloud: dict[str, Any], units: int
    ) -> Outcome:
        """Local first, on a budget that leaves CLOUD_SLICE_S for the cloud; then, after a failure the cloud may get
        round (FALLBACK_ERRORS), the same command through the cloud. A device with no known address goes to the
        cloud at once. Either way the device is looked for in the background, not during the command, so the next
        command can be local again."""
        budget = _Budget(self._budget_s())
        if conn.address is None or conn.version is None:
            self._rediscover_later(conn, units)
            log.info("Tuya device %s has no address: cloud", conn.tuya_id)
            missing = self._failure(device, _Failure("noaddress"))
            return self._through_cloud(device, work, cloud, budget, local=missing)
        link = _Link(conn, _Budget(self._budget_s() - CLOUD_SLICE_S), _open_device)
        try:
            return work(link)
        except _Failure as failure:
            if failure.err not in FALLBACK_ERRORS:
                return self._failure(device, failure)
            if failure.err in RESCAN_ERRORS:
                self._rediscover_later(conn, units)
            log.info("Tuya device %s failed locally (%s): cloud", conn.tuya_id, failure.err)
            local = self._failure(device, failure)
            return self._through_cloud(device, work, cloud, budget, local=local, seed=link.seed())

    def _through_cloud(
        self,
        device: DeviceRecord,
        work: Callable[[_AnyLink], Outcome],
        cloud: dict[str, Any],
        budget: _Budget,
        local: Outcome | None = None,
        seed: dict[str, Any] | None = None,
    ) -> Outcome:
        """``work`` through Tuya's cloud. ``local`` is what the local attempt came to, when there was one: its
        sentence leads if the cloud fails too, and a success then says it went through the cloud."""
        link = _CloudLink(self.ctx.store, cloud, str(device.settings["tuya_id"]), tdp.dp_map(device), budget, seed)
        try:
            outcome = work(link)
        except _Failure as failure:
            return self._cloud_outcome(device, failure, local, link.sent)
        if not outcome.ok or local is None:
            return outcome
        # After the first sentence, which holds the user's own words: past them, as a name may have a full stop
        # in it ("St. Mary lamp").
        text = outcome.text
        start = max((text.find(w) + len(w) for w in (device.name, *device.aliases) if w and w in text), default=0)
        cut = text.find(". ", start)
        if cut < 0:
            return Outcome.done(f"{text.rstrip('.')}, through Tuya's cloud.")
        return Outcome.done(f"{text[:cut]}, through Tuya's cloud.{text[cut + 1 :]}")

    def _cloud_outcome(self, device: DeviceRecord, failure: _Failure, local: Outcome | None, sent: bool) -> Outcome:
        the, err = f"the {device.name}", failure.err
        if err == "offline":
            return Outcome.fail("unreachable", f"Tuya's cloud says {the} is offline.")
        if err == "late" and sent:
            return Outcome.fail("timeout", f"Tuya's cloud did not answer in time; {the} may still change.")
        notes: dict[str, tuple[Code, str]] = {
            "busy": ("busy", CLOUD_BUSY),
            "signin": ("auth", SIGNIN_REFUSED),
            "expired": ("auth", EXPIRED),
            "refresh": (
                "needs_setup",
                f"Jarvis needs fresher details to control {the} through Tuya's cloud; refresh your Tuya devices in "
                "home setup.",
            ),
        }
        if err in notes:
            code, note = notes[err]
            return Outcome.fail(local.code, f"{local.text} {note}") if local else Outcome.fail(code, note)
        if local is not None:
            return Outcome.fail(local.code, f"{local.text} Tuya's cloud could not reach it either.")
        if err == "unreachable":
            return Outcome.fail(
                "unreachable", f"Jarvis could not reach Tuya's cloud to control {the}. Is the internet working?"
            )
        if err in ("late", "budget"):
            return Outcome.fail("timeout", f"Tuya's cloud did not answer in time about {the}.")
        return Outcome.fail("failed", f"Tuya's cloud could not control {the}.")

    def _rediscover_later(self, conn: _Conn, units: int) -> None:
        """_rediscover on a thread of its own, given the time a full scan needs."""
        budget = _Budget(self.rescan_s + units * (8 * T_MIN + UNIT_SLACK_S) + 1.5)
        self._background = threading.Thread(
            target=self._rediscover, args=(conn, budget, units), name="tuya-rescan", daemon=True
        )
        self._background.start()

    def _rediscover(self, conn: _Conn, budget: _Budget, units: int = 1) -> _Conn | None:
        """A short scan for the device (its gateway for a sub-device); saves a new address or version.

        The scan is cut short so that the retry's ``units`` requests still fit at T_MIN afterwards (an unknown
        version counts as v3.4+), and skipped when that leaves too little to hear anything."""
        target = conn.parent or conn.tuya_id
        factor = 8 if (conn.version or 3.4) >= 3.4 else 4
        scan_s = min(self.rescan_s, budget.left() - units * (factor * T_MIN + UNIT_SLACK_S) - 1.0)
        if scan_s < min(RESCAN_MIN_S, self.rescan_s):
            return None
        if not self._scan_lock.acquire(blocking=False):
            return None
        try:
            last = self._scanned.get(target)
            if last is not None and time.monotonic() - last < self.rescan_gap_s:
                return None
            found = discover([target], scan_s)
            if found is not None:
                # Only a scan that listened counts: one that could not (the setup console's scan holds the
                # UDP ports, and Windows does not share them) must not stop the next request from looking.
                self._scanned[target] = time.monotonic()
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
            # 904/914 also come from a device that took the connection and dropped it, because another app
            # (the Tuya app on a phone, a hub) holds its one local connection, or that was too slow to answer.
            return Outcome.fail(
                "auth",
                f"{the} turned Jarvis away. It may be busy with another app, so try again in a moment. "
                "If it keeps happening, its key may have changed: refresh your Tuya devices in home setup.",
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

    def _power(self, device: DeviceRecord, link: _AnyLink, _value: Value = None, *, on: bool) -> Outcome:
        dps = tdp.dp_map(device)
        link.write({self._dp(dps, tdp.power_code(device, dps)): on})
        return Outcome.done(f"The {device.name} is {'on' if on else 'off'}.")

    def _toggle(self, device: DeviceRecord, link: _AnyLink, _value: Value = None) -> Outcome:
        dps = tdp.dp_map(device)
        dp = self._dp(dps, tdp.power_code(device, dps))
        current = link.read(reserve=1).get(str(dp))
        if not isinstance(current, bool):
            return Outcome.fail("failed", f"The {device.name} did not say whether it is on, so Jarvis left it alone.")
        link.write({dp: not current})
        return Outcome.done(f"The {device.name} is now {'off' if current else 'on'}.")

    def _press(self, device: DeviceRecord, link: _AnyLink, _value: Value = None) -> Outcome:
        dps = tdp.dp_map(device)
        dp = self._dp(dps, tdp.press_code(dps))
        # In click mode each change of the switch value is one press, so write the opposite of what it reports
        # (as ha_tuya_ble does), or True when it does not say. A device that cannot be reached at all fails
        # here, so the command looks for it again instead of sending a write that cannot arrive either; so does
        # any cloud read that failed (an offline device, say).
        current = None
        if link.affordable(1, reserve=1):
            try:
                current = link.read(reserve=1).get(str(dp))
            except _Failure as failure:
                if failure.err in ERR_UNREACHABLE | ERR_KEY or isinstance(failure, _CloudFailure):
                    raise
        link.write({dp: not current if isinstance(current, bool) else True})
        what = tdp.presses(device)
        return Outcome.done(
            f"The {device.name} pressed {f'the {what} switch' if what else 'its switch'}. Jarvis cannot see the "
            "light, so it cannot tell whether it is now on or off."
        )

    def _light_now(
        self, link: _AnyLink, dps: DpMap, parts: tdp.LightParts, reserve: int
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

    def _set_brightness(self, device: DeviceRecord, link: _AnyLink, value: Value) -> Outcome:
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

    def _set_color(self, device: DeviceRecord, link: _AnyLink, value: Value) -> Outcome:
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

    def _set_color_temp(self, device: DeviceRecord, link: _AnyLink, value: Value) -> Outcome:
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

    def _move(self, device: DeviceRecord, link: _AnyLink, _value: Value = None, *, action: str) -> Outcome:
        dps = tdp.dp_map(device)
        code, actions = tdp.cover_control(dps)
        link.write({self._dp(dps, code): actions[action]})
        return Outcome.done(f"The {device.name} is {'opening' if action == 'open' else 'closing'}.")

    def _stop(self, device: DeviceRecord, link: _AnyLink, _value: Value = None) -> Outcome:
        dps = tdp.dp_map(device)
        code, actions = tdp.cover_control(dps)
        link.write({self._dp(dps, code): actions["stop"]})
        return Outcome.done(f"The {device.name} has stopped.")

    def _set_position(self, device: DeviceRecord, link: _AnyLink, value: Value) -> Outcome:
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

    def _set_number(self, device: DeviceRecord, link: _AnyLink, value: Value, code: str | None, unit: str) -> Outcome:
        dps = tdp.dp_map(device)
        assert code is not None and isinstance(value, (int, float))
        raw = tdp.number_to_raw(dps[code], float(value))
        link.write({self._dp(dps, code): raw})
        return Outcome.done(f"The {device.name} is set to {tdp.number_words(tdp.scaled(dps[code], raw) or 0)}{unit}.")

    def _set_temperature(self, device: DeviceRecord, link: _AnyLink, value: Value) -> Outcome:
        return self._set_number(device, link, value, tdp.temperature_target(tdp.dp_map(device)), " degrees")

    def _set_humidity(self, device: DeviceRecord, link: _AnyLink, value: Value) -> Outcome:
        return self._set_number(device, link, value, tdp.humidity_target(tdp.dp_map(device)), "% humidity")

    def _set_mode(self, device: DeviceRecord, link: _AnyLink, value: Value) -> Outcome:
        dps = tdp.dp_map(device)
        code = tdp.mode_code(dps)
        link.write({self._dp(dps, code): tdp.enum_raw(dps[code or ""], str(value))})
        return Outcome.done(f"The {device.name} is in {tdp.words(str(value))} mode.")

    def _set_fan_speed(self, device: DeviceRecord, link: _AnyLink, value: Value) -> Outcome:
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

    def _vacuum(self, device: DeviceRecord, link: _AnyLink, _value: Value = None, *, action: str) -> Outcome:
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
            return Outcome.fail("needs_setup", f"{device.name} is missing its Tuya details; refresh it in home setup.")
        try:
            link = _usable_link(self.ctx.store.secret(CLOUD_SECRET))
        except StoreError:
            link = None
        if link is None:
            return Outcome.fail("needs_setup", NOT_LINKED)
        try:
            # A token renewal and the trigger, each with only per-step timeouts (and DNS with none): bound the whole.
            result = _CLOUD.call(
                self.ctx.store,
                link,
                self._budget_s(),
                lambda manager: manager.trigger_scene(str(home_id), str(scene_id)),
            )
        except _Late as late:
            log.warning("Tuya did not answer a scene request in time")
            if not late.started:  # it waited for another cloud call and was never sent
                return Outcome.fail("timeout", f"Tuya did not answer in time, so Jarvis did not run {device.name}.")
            return Outcome.fail("timeout", f"Tuya did not answer in time; {device.name} may still run.")
        except _Paused:
            return Outcome.fail("busy", CLOUD_BUSY)
        except Exception as exc:  # noqa: BLE001 - every cloud failure becomes a plain sentence
            return _cloud_failure(exc, f"run {device.name}")
        if result is False:
            return Outcome.fail("failed", f"Tuya did not run {device.name}.")
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


# --------------------------------------------------------------------------- reading the Tuya account


@dataclass(repr=False)
class _Candidate:
    """A device as the Tuya cloud describes it, on its way to becoming one or more DeviceRecords."""

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
    # Reached only through Tuya's cloud (no local key, or a hub Jarvis could not match): with the fallback on.
    cloud_only: bool = False
    online: bool = True

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
    """Names the user gave each switch in the Tuya app, when the cloud passes them on (not documented)."""
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
        log.debug("no Tuya room for a device: %s", type(exc).__name__)
    parent_hint = next(
        (
            _text(getattr(device, attribute, None))
            for attribute in ("parent_id", "gateway_id", "parentId", "gatewayId")
            if _text(getattr(device, attribute, None))
        ),
        None,
    )
    sub = bool(getattr(device, "sub", False))
    node_id = _text(getattr(device, "node_id", None)) or None
    if sub and node_id is None:
        # The SDK does not always pass node_id on; the hub knows a sub-device by its uuid then (as tuya-local).
        node_id = _text(getattr(device, "uuid", None)) or None
    return _Candidate(
        tuya_id=tuya_id,
        name=_text(getattr(device, "name", None)) or "Tuya device",
        category=category,
        product=_text(getattr(device, "product_name", None)),
        key=_text(getattr(device, "local_key", None)),
        dps=dps,
        sub=sub or node_id is not None,
        node_id=node_id,
        parent_hint=parent_hint,
        room=room,
        # As Home Assistant: curtains and curtain robots count the other way unless their motor says "back".
        invert=category in ("cl", "jdcljqr") or (category == "clkg" and status.get("control_back_mode") != "back"),
        gang_names=_gang_names(device, dps),
        online=getattr(device, "online", True) is not False,
    )


def _read_cloud(manager: Any, cloud: bool = False) -> tuple[list[_Candidate], list[_Scene], bool]:
    """Every device and enabled scene, and whether there is an IR remote (whose devices need scenes). With
    ``cloud`` (the fallback is on), devices Jarvis cannot reach locally are kept, to be reached through the cloud."""
    manager.update_device_cache()
    candidates = [c for c in (_read_device(manager, d) for d in list(manager.device_map.values())) if c]
    scenes = []
    for scene in manager.query_scenes() or []:
        scene_id = _text(getattr(scene, "scene_id", None) or getattr(scene, "id", None))
        name = _text(getattr(scene, "name", None))
        if scene_id and name and getattr(scene, "enabled", True) is not False:
            scenes.append(_Scene(scene_id, name, _text(getattr(scene, "home_id", None))))
    has_ir = any(c.category in ("wnykq", "ykq") or c.category.startswith("infrared") for c in candidates)
    _settle(candidates, cloud)
    return candidates, scenes, has_ir


def _settle(candidates: list[_Candidate], cloud: bool = False) -> None:
    """Finds each sub-device's gateway (it shares the gateway's key) and decides every device's kind. With
    ``cloud``, a device with no key or no usable hub is kept as reachable only through the cloud."""
    by_id = {c.tuya_id: c for c in candidates}
    gateways = [c for c in candidates if not c.sub and c.key]
    for candidate in candidates:
        if not candidate.sub and tdp.kind_for(candidate.category, candidate.dps) == "button":
            # A button pusher talks Bluetooth: with no gateway to pass commands on, nothing on the network
            # reaches it (tuya_sharing says nothing more about its radio than sub and node_id).
            candidate.reason = BLUETOOTH_ONLY
            continue
        if candidate.sub:
            parent = by_id.get(candidate.parent_hint or "")
            if parent is None or parent.sub:
                same_key = [g for g in gateways if candidate.key and g.key == candidate.key]
                parent = same_key[0] if len(same_key) == 1 else None
            if parent is None:
                candidate.reason = "paired to a hub Jarvis could not find"
            elif candidate.node_id is None:
                # Without it, commands would go to the hub itself and the device would never hear them.
                candidate.reason = "Tuya did not say how its hub addresses it"
            else:
                candidate.parent, candidate.key = parent.tuya_id, parent.key
        if candidate.reason is None and not candidate.key:
            candidate.reason = "Tuya gave no local key for it (shared devices sometimes have none)"
        if candidate.reason is not None:
            if not cloud:
                continue
            # The cloud reaches it by its own id, through its hub when it has one.
            candidate.reason, candidate.cloud_only, candidate.parent = None, True, None
        candidate.kind = tdp.kind_for(candidate.category, candidate.dps)
        if candidate.kind is None:
            candidate.reason = tdp.skip_reason(candidate.category)


# --------------------------------------------------------------------------- the wizard


def wizard(ui: Prompter, ctx: DriverContext) -> None:
    """Links the Tuya account, refreshes devices and scenes, turns the cloud fallback on or off, finds devices on
    the network, or reverses a curtain."""
    ui.say("Tuya devices (Tuya Smart or Smart Life app)")
    mine = [d for d in ctx.store.load().devices if d.driver == TuyaDriver.name]
    actions: list[tuple[str, Callable[[Prompter, DriverContext], None]]] = [
        ("Link your Tuya account (scan a QR code with your phone)", _link_flow)
    ]
    saved = _saved_link(ctx)
    if saved is not None:
        actions.append(("Refresh devices and scenes from your Tuya account (no QR code)", _refresh_flow))
        now = "on" if saved.get(FALLBACK) is True else "off"
        actions.append((f"Use Tuya's cloud when a device does not answer on the network (now {now})", _fallback_flow))
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
    """What went wrong with the Tuya cloud, for the setup console (codes, never replies)."""
    kind = _cloud_kind(exc)
    code = _text(getattr(exc, "error_code", "")) or "?"
    if kind == "expired":
        return "The Tuya link has expired: choose Link your Tuya account to scan a new code."
    if kind == "busy":
        return "Tuya's cloud is limiting requests. Try again in a minute."
    if kind == "signin":
        return (
            f"Tuya turned Jarvis's sign-in away (error {code}). Try again; if it keeps happening, choose Link your "
            "Tuya account to scan a new code."
        )
    if kind == "refused":
        return f"Tuya refused the request (error {code})."
    if kind == "unreachable":
        return "Jarvis could not reach Tuya. Check the internet connection and try again."
    log.warning("Tuya gave an unexpected answer: %s", type(exc).__name__)
    return "Tuya gave an answer Jarvis did not understand."


def _link_flow(ui: Prompter, ctx: DriverContext) -> None:
    ui.say(LINK_STEPS)
    user_code = ui.ask("Your User Code (from the Tuya Smart or Smart Life app; it is case-sensitive)").strip()
    if not user_code:
        ui.say("Nothing was saved.")
        return
    sharing = _sharing()
    control = sharing.LoginControl()
    while True:
        token = _qr_token(ui, control, user_code)
        if token is None:
            return
        ui.show_qr(
            QR_PREFIX + token,
            "Scan this with the same app (Tuya Smart or Smart Life): Home tab > + (top right) > Scan, then tap "
            "Confirm login on the phone.",
        )
        ui.say(f"Waiting for you to confirm on the phone (up to {int(LOGIN_WAIT_S // 60)} minutes; Ctrl+C stops)...")
        info = _wait_for_login(control, token, user_code)
        if isinstance(info, dict):
            break
        if info is None:
            ui.say(
                "The code was not confirmed in time. Nothing was saved; choose Link your Tuya account for a new code."
            )
            return
        if info == QR_EXPIRED:
            ui.say("The QR code expired before the phone confirmed it.")
        else:
            ui.say(
                "The phone refused the login. Scan with the same app you took the User Code from (Tuya Smart or "
                "Smart Life): the two apps have separate accounts."
            )
        if not ui.confirm("Show a new QR code to scan?", default=True):
            ui.say("Nothing was saved.")
            return
    link = {
        "user_code": user_code,
        "terminal_id": info["terminal_id"],
        "endpoint": info["endpoint"],
        "token_info": {name: info.get(name) for name in TOKEN_FIELDS},
    }
    ui.say("Signed in to your Tuya account.")
    ui.say(CLOUD_EXPLAINED)
    link[FALLBACK] = ui.confirm(CLOUD_QUESTION, default=False)
    ui.say("Reading your homes, devices and scenes...")
    _import(ui, ctx, sharing, link, keeper=_TokenKeeper(None, link))


def _qr_token(ui: Prompter, control: Any, user_code: str) -> str | None:
    """A fresh QR token for the User Code; None (and why, said) when Tuya gives none."""
    try:
        response = control.qr_code(CLIENT_ID, SCHEMA, user_code)
    except Exception as exc:  # noqa: BLE001
        ui.say(_cloud_problem(exc))
        ui.say("Nothing was saved.")
        return None
    result = response.get("result") if isinstance(response, Mapping) else None
    token = result.get("qrcode") if isinstance(result, Mapping) else None
    if not (isinstance(response, Mapping) and response.get("success")) or not isinstance(token, str) or not token:
        code = _text(response.get("code")) if isinstance(response, Mapping) else ""
        ui.say(
            f"Tuya did not accept that User Code{f' (error {code})' if code else ''}. Check it in the app, letter "
            "for letter (Me > the gear > Account and Security > User Code), and try again."
        )
        return None
    return token


def _wait_for_login(control: Any, token: str, user_code: str) -> dict[str, Any] | str | None:
    """Asks every couple of seconds whether the phone confirmed: the sign-in when it did, QR_EXPIRED or
    LOGIN_REFUSED as soon as Tuya answers with one, None after LOGIN_WAIT_S. Ctrl+C goes through."""
    deadline = time.monotonic() + LOGIN_WAIT_S
    while True:
        try:
            ok, info = control.login_result(token, CLIENT_ID, user_code)
        except (OSError, ValueError) as exc:  # a network hiccup or a non-JSON reply: keep waiting
            log.debug("Tuya login check: %s", type(exc).__name__)
            ok, info = False, None
        code = _text(info.get("code")) if not ok and isinstance(info, Mapping) else ""
        if code in (QR_EXPIRED, LOGIN_REFUSED):
            log.info("the Tuya QR login ended (error %s)", code)
            return code
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


def _fallback_flow(ui: Prompter, ctx: DriverContext) -> None:
    link = _saved_link(ctx)
    if link is None:
        ui.say("Your Tuya account is not linked yet: choose Link your Tuya account first.")
        return
    ui.say(CLOUD_EXPLAINED)
    on = ui.confirm(CLOUD_QUESTION, default=link.get(FALLBACK) is True)
    # Read again just before saving: the helper may have renewed the sign-in meanwhile.
    link = _saved_link(ctx) or link
    link[FALLBACK] = on
    try:
        ctx.store.set_secret(CLOUD_SECRET, link)
    except (StoreError, OSError):
        ui.say("Jarvis could not save that choice. Nothing was changed.")
        return
    cloud_only = [
        d.name for d in ctx.store.load().devices if d.driver == TuyaDriver.name and d.settings.get("cloud_only") is True
    ]
    if on:
        ui.say(
            "Saved: a command a device does not answer on your home network now goes through Tuya's cloud. "
            "Choose Refresh devices and scenes to add devices only the cloud can reach."
        )
    elif cloud_only:
        ui.say(
            f"Saved: Jarvis no longer uses Tuya's cloud for commands. {', '.join(cloud_only)} work only through it, "
            "so Jarvis cannot control them until you turn this on again."
        )
    else:
        ui.say("Saved: Jarvis no longer uses Tuya's cloud for commands. Scenes still run there.")


def _refresh_flow(ui: Prompter, ctx: DriverContext) -> None:
    link = _saved_link(ctx)
    if link is None:
        ui.say("Your Tuya account is not linked yet: choose Link your Tuya account first.")
        return
    ui.say("Reading your homes, devices and scenes from your Tuya account...")
    # Renewed tokens are saved at once: the old refresh token may stop working after a renewal.
    _import(ui, ctx, _sharing(), link, keeper=_TokenKeeper(ctx.store, link))


def _import(ui: Prompter, ctx: DriverContext, sharing: Any, link: dict[str, Any], keeper: _TokenKeeper) -> None:
    try:
        candidates, scenes, has_ir = _read_cloud(_manager(sharing, link, keeper), link.get(FALLBACK) is True)
    except Exception as exc:  # noqa: BLE001
        ui.say(_cloud_problem(exc))
        ui.say("Nothing was changed.")
        return
    usable = [c for c in candidates if c.kind is not None]
    targets = sorted({c.parent or c.tuya_id for c in usable if not c.cloud_only})
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
    offline = [c.tuya_id for c in usable if c.cloud_only and not c.online]
    _report(ui, records, [c for c in candidates if c.kind is None], has_ir, offline)
    if not records:
        ui.say("Your Tuya account has no devices or scenes Jarvis can use. Nothing was changed.")
        return
    if removed and not ui.confirm(
        f"No longer in your Tuya account: {', '.join(d.name for d in removed)}. Remove them from Jarvis?", default=True
    ):
        removed = []
    device_count = sum(1 for r, _ in records if r.kind != "scene")
    scene_count = len(records) - device_count
    if not ui.confirm(f"Save {_count(device_count, 'device')} and {_count(scene_count, 'scene')}?", default=True):
        ui.say("Nothing was saved.")
        return
    if keeper.store is not None:
        # Read again just before saving: the helper may have renewed the sign-in meanwhile, and the refresh token
        # read at the start may no longer work.
        saved = _saved_link(ctx)
        if saved is not None and saved.get("terminal_id") == link.get("terminal_id") and _issued(saved) > _issued(link):
            link["token_info"] = saved["token_info"]
    _save_all(ctx, records, removed, link)
    ui.say(
        "Saved. Names and rooms come from the Tuya Smart or Smart Life app; to change them, choose Rename a device "
        "in the main menu."
    )
    reachable = [r for r, _ in records if r.kind != "scene" and r.settings.get("address")]
    if reachable and ui.confirm("Check now that Jarvis can reach each device? It takes a few seconds.", default=True):
        _check(ui, ctx, reachable)


def _gone(
    config: HomeConfig, records: list[tuple[DeviceRecord, dict[str, Any] | None]], seen: set[str]
) -> list[DeviceRecord]:
    """Saved Tuya devices and scenes the account no longer has, or now has in another shape (e.g. per switch).
    A device the account still lists but Jarvis now leaves out (say, its key is missing) is kept."""
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
            if candidate.cloud_only:
                settings["cloud_only"] = True
            elif address and version:
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
            if candidate.kind == "button" and old is None:
                # Saved as an alias, so "turn on the bedroom light" finds the button that presses its switch.
                presses = ui.ask(
                    f"What does the {name} switch on and off? For example: bedroom light (empty to skip)"
                ).strip()
                if presses:
                    record.aliases.append(presses)
            records.append((record, None if candidate.cloud_only else {"local_key": candidate.key}))
    for scene in scenes:
        old = existing.get((scene.scene_id,))
        name = old.name if old else fresh_name(scene.name)
        record = add(old, name, "scene", None, {"home_id": scene.home_id, "scene_id": scene.scene_id})
        records.append((record, None))
    return records


def _report(
    ui: Prompter,
    records: list[tuple[DeviceRecord, dict[str, Any] | None]],
    left_out: list[_Candidate],
    has_ir: bool,
    offline: Iterable[str] = (),
) -> None:
    """What the import found. ``offline``: the Tuya ids of cloud-only devices the cloud says are offline."""
    devices = [r for r, _ in records if r.kind != "scene"]
    scenes = [r for r, _ in records if r.kind == "scene"]
    offline = set(offline)
    if devices:
        ui.say(f"{_count(len(devices), 'device')}:")
        for record in devices:
            if record.settings.get("cloud_only"):
                where = "through Tuya's cloud only"
                if record.settings.get("tuya_id") in offline:
                    # A Bluetooth device the cloud lists but cannot reach has no gateway (Tuya's support says so).
                    where += " (Tuya's cloud says it is offline; Bluetooth devices need a Tuya gateway to be reached)"
            elif record.settings.get("address"):
                where = f"at {record.settings['address']}"
            else:
                where = "not found on the network"
            room = f", {record.room}" if record.room else ""
            ui.say(f"  {record.name} ({record.kind}{room}): {where}")
    if scenes:
        ui.say(f"{_count(len(scenes), 'scene')}: {', '.join(r.name for r in scenes)}")
    if left_out:
        ui.say("Not added: " + "; ".join(f"{c.name} ({c.reason})" for c in left_out))
    missing = [r.name for r in devices if not r.settings.get("address") and not r.settings.get("cloud_only")]
    if missing:
        ui.say(
            f"Not found on your home network: {', '.join(missing)}. They are saved anyway: battery devices sleep "
            "and others may be switched off. Jarvis looks for them again when they are used."
        )
    if has_ir:
        ui.say(IR_ADVICE)
    elif not scenes:
        ui.say(
            "Tip: Tap-to-Run scenes you make in the Tuya Smart or Smart Life app appear here after Refresh devices "
            "and scenes."
        )


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
