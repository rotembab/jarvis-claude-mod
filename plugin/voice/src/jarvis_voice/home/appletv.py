"""Apple TV control over Apple's Companion protocol, through pyatv.

Jarvis pairs with the Apple TV once (the TV shows a PIN, typed into the setup
wizard) and keeps the credentials in the credential store. After that it
speaks Companion only: power, the remote's buttons, apps, typing and, when
the Apple TV controls the TV's volume over HDMI-CEC, volume. The AirPlay MRP
tunnel (what is playing) stays off: it is unreliable on tvOS 26 and 27, and
pyatv drops every protocol when one of them fails.

One connection per Apple TV opens on first use, stays open for the next
command and closes after ten idle minutes. Nothing polls the Apple TV: any
request wakes it and, over HDMI-CEC, the TV with it.

pyatv logs credentials at DEBUG and puts them in ``str(config)`` and in some
exception messages, so this module never formats a pyatv config or a pyatv
exception's message: texts and log lines name the exception's type only. The
one exception is the reason the Apple TV itself gives when it turns a command
down (see ``_refusal_detail``).
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import ipaddress
import logging
import re
import secrets
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any

import pyatv
from pyatv import exceptions as atv_errors
from pyatv.const import DeviceModel, FeatureName, FeatureState, PairingRequirement, PowerState, Protocol
from pyatv.interface import DeviceListener
from pyatv.storage.memory_storage import MemoryStorage

from .base import Driver, DriverContext, Prompter
from .links import Link, parse_link
from .model import CommandSpec, DeviceRecord, HomeConfig, Outcome, Value, normalize, unique_id
from .runner import AsyncRunner
from .store import StoreError

log = logging.getLogger(__name__)

DRIVER = "appletv"
# The name the Apple TV shows for Jarvis (Settings > Remotes and Devices).
CLIENT_NAME = "Jarvis"

IDLE_CLOSE_S = 600.0  # close a connection nobody used for ten minutes
UNICAST_SCAN_S = 3  # ask the saved address (it also knocks to wake a sleeping Apple TV)
MULTICAST_SCAN_S = 4  # look for it by identifier when the address has changed
SETUP_SCAN_S = 5  # the wizard's search for Apple TVs
COMMAND_RESERVE_S = 4.0  # of a call's time, kept for the command after connecting
RETRY_MIN_S = 2.0  # time a reconnect-and-retry needs at least
CLOSE_WAIT_S = 5.5  # closing a half-open link waits ~5 s for the Apple TV's goodbye
SETTLE_S = 1.0  # how long a fresh connection may take to report power state and media flags
FRESH_S = 3.0  # a connection this young may not have reported them yet
APPS_TTL_S = 600.0  # how long the app list is reused before asking again
PIN_TRIES = 3  # more failed PINs can lock pairing until the Apple TV restarts
PIN_WAIT_S = 180.0  # an abandoned PIN dialog is closed after this long
PIN_RETRY_WAIT_S = 3.0  # after a wrong PIN the Apple TV turns new pairings away for a moment (HAP back-off)
PAIR_STEP_S = 20.0

ALLOW_ACCESS = (
    'On the Apple TV, open Settings > AirPlay and HomeKit > Allow Access and choose "Anyone on the Same Network" '
    '(or "Same Network").'
)
RESTART_HINT = "Restart the Apple TV (Settings > System > Restart), then try again."

# What a connection is when a command runs: the open pyatv object and what it knows.
Op = Callable[["_Conn", DeviceRecord, Value], Awaitable[Outcome]]


class PyatvApi:
    """The pyatv entry points the driver uses. Tests give the driver a fake with the same three methods."""

    async def scan(
        self, *, hosts: list[str] | None = None, identifier: set[str] | None = None, timeout: int = UNICAST_SCAN_S
    ) -> list[Any]:
        """The Apple TVs that answer: unicast to ``hosts``, else multicast (stopping once ``identifier`` answers)."""
        loop = asyncio.get_running_loop()
        return await pyatv.scan(
            loop,
            timeout=timeout,
            identifier=identifier or None,
            protocol={Protocol.Companion, Protocol.AirPlay},  # AirPlay adds a stable identifier
            hosts=hosts,
        )

    async def connect(self, conf: Any, *, rp_id: str | None) -> Any:
        loop = asyncio.get_running_loop()
        return await pyatv.connect(conf, loop, storage=await _identity(conf, rp_id))

    async def pair(self, conf: Any, *, rp_id: str | None) -> Any:
        loop = asyncio.get_running_loop()
        return await pyatv.pair(conf, Protocol.Companion, loop, storage=await _identity(conf, rp_id), name=CLIENT_NAME)


async def _identity(conf: Any, rp_id: str | None) -> MemoryStorage:
    """Settings that announce the same client every time: the name "Jarvis" and a fixed rp_id.

    Without them pyatv says "pyatv" and picks a new random rp_id on every connect.
    """
    storage = MemoryStorage()
    settings = await storage.get_settings(conf)
    settings.info.name = CLIENT_NAME
    if rp_id:
        settings.info.rp_id = rp_id
    return storage


# --------------------------------------------------------------------------- errors


class _NotFound(Exception):
    """No Apple TV with the saved identifiers answered, at its address or by multicast."""


class _NoRemote(Exception):
    """The Apple TV answered but did not offer the Companion (remote control) service."""


class _StillConnecting(Exception):
    """The connect is still running in the background when the caller's time is up."""


class _Closed(Exception):
    """The driver was closed (a reload, or the helper stopping) while the command waited for its connection."""


def _is_connection_loss(exc: BaseException) -> bool:
    """Whether ``exc`` means the link is gone (reconnect), rather than the Apple TV refusing a command.

    Companion wraps both in ProtocolError; a lost or half-open link shows as its cause
    (a timeout, or "not connected"). A bare TimeoutError is a volume echo that never came.
    """
    if isinstance(
        exc, atv_errors.ConnectionLostError | atv_errors.ConnectionFailedError | atv_errors.BlockedStateError
    ):
        return True
    if isinstance(exc, atv_errors.ProtocolError):
        return isinstance(exc.__cause__, TimeoutError | OSError | atv_errors.InvalidStateError)
    if isinstance(exc, TimeoutError):
        return False
    return isinstance(exc, OSError)


def _unreachable(device: DeviceRecord, command: str) -> Outcome:
    if command == "turn_on":
        return Outcome.fail(
            "unreachable",
            f"The {device.name} isn't answering, so I can't wake it. "
            "Some Apple TVs drop off the network while asleep: wake it with its remote.",
        )
    return Outcome.fail("unreachable", f"The {device.name} isn't answering. It may be asleep or off the network.")


def _failure(exc: BaseException, device: DeviceRecord, command: str) -> Outcome:
    """A spoken failure for ``exc``. Never uses its message (pyatv can put credentials in it), except the
    Apple TV's own reason for turning a command down."""
    name = device.name
    if isinstance(exc, _NotFound):
        return _unreachable(device, command)
    if isinstance(exc, _Closed):
        return Outcome.fail("failed", f"Home control was restarting, so the {name} didn't get that. Ask again.")
    if isinstance(exc, _NoRemote):
        return Outcome.fail(
            "unreachable",
            f"The {name} answered but didn't offer remote control. Try again; if it keeps happening, check that "
            "it allows access to anyone on the same network.",
        )
    if isinstance(
        exc,
        atv_errors.AuthenticationError
        | atv_errors.InvalidCredentialsError
        | atv_errors.NoCredentialsError
        | atv_errors.BackOffError,
    ):
        return Outcome.fail("auth", f"The {name} no longer accepts Jarvis. Pair it again in home setup.")
    if isinstance(exc, NotImplementedError):  # pyatv's NotSupportedError is one
        return Outcome.fail("unsupported", f"The {name} can't do that over its remote connection.")
    if isinstance(exc, atv_errors.NoServiceError | atv_errors.DeviceIdMissingError):
        return Outcome.fail(
            "needs_setup", f"The saved details of the {name} are incomplete. Add it again in home setup."
        )
    if isinstance(exc, TimeoutError):
        return Outcome.fail("timeout", f"The {name} didn't answer in time; it may still act on it.")
    if _is_connection_loss(exc):
        if isinstance(exc, atv_errors.ProtocolError | atv_errors.BlockedStateError | atv_errors.ConnectionLostError):
            return Outcome.fail("unreachable", f"The {name} stopped answering. Try again in a moment.")
        return _unreachable(device, command)
    if isinstance(exc, atv_errors.ProtocolError):
        detail = _refusal_detail(exc)
        if detail:
            log.warning("apple tv %s: %s was refused:%s", device.id, command, detail)
        if command in ("play", "pause", "next", "previous"):
            return Outcome.fail("failed", f"The {name} didn't accept that; nothing may be playing.{detail}")
        return Outcome.fail("failed", f"The {name} didn't accept that command.{detail}")
    return Outcome.fail("failed", f"Controlling the {name} failed ({type(exc).__name__}).")


# pyatv builds this message in exactly one place (Companion's reply handling), from the Apple TV's
# own error text and nothing else, so the part after it is the one pyatv message that is safe to show.
_DEVICE_SAID = "Command failed: "
_SAID_MAX = 120
# A run this long of key-like characters is never a sentence: drop the text rather than show it.
_KEY_LIKE = re.compile(r"[A-Za-z0-9+/=_-]{32,}")


def _refusal_detail(exc: BaseException) -> str:
    """`` It said: '...'.`` when the Apple TV gave a reason, else what kind of error sat under pyatv's, else nothing."""
    text = str(exc)
    if text.startswith(_DEVICE_SAID):
        said = "".join(c if c.isprintable() else " " for c in text[len(_DEVICE_SAID) :]).replace('"', "'")
        said = " ".join(said.split())
        if said and not _KEY_LIKE.search(said):
            return f' It said: "{said[: _SAID_MAX - 3] + "..." if len(said) > _SAID_MAX else said}".'
        return ""
    cause = exc.__cause__
    return f" (The error underneath was a {type(cause).__name__}.)" if cause is not None else ""


# --------------------------------------------------------------------------- one connection


class _Conn(DeviceListener):
    """One open pyatv connection with a listener of its own.

    pyatv holds listeners weakly, so the link keeps this object; and because each
    connection has its own, a late callback from an old one cannot clear a newer one.
    """

    def __init__(self, link: _Link, atv: Any, creds: str, opened_at: float) -> None:
        self.link = link
        self.atv = atv
        self.creds = creds
        self.opened_at = opened_at
        self.alive = True
        self.put_to_sleep = False  # Jarvis sent Sleep on it (and no Wake since)

    def connection_lost(self, exception: Exception) -> None:
        self._gone("lost")

    def connection_closed(self) -> None:
        self._gone("closed")

    def _gone(self, how: str) -> None:
        if self.alive:
            log.debug("apple tv %s: connection %s", self.link.device_id, how)
        self.alive = False
        self.link.forget(self)


async def _close_atv(atv: Any, wait: float) -> None:
    """Closes a pyatv connection; a half-open one waits ~5 s for the Apple TV, so give up after ``wait``."""
    try:
        tasks = atv.close()
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), wait)
    except Exception as exc:  # noqa: BLE001 - closing is best effort
        log.debug("closing an Apple TV connection: %s", type(exc).__name__)


class _Link:
    """One Apple TV's connection manager, living on the shared event loop.

    Connects lazily, keeps the connection for reuse, closes it when idle, runs one
    command at a time, and never cancels a connect midway (that leaks a Companion
    session, which makes the Apple TV drop the other one).
    """

    def __init__(self, driver: AppleTvDriver, device_id: str) -> None:
        self.driver = driver
        self.device_id = device_id
        self.lock = asyncio.Lock()
        self.conn: _Conn | None = None
        self.opening: asyncio.Task[_Conn] | None = None
        self.idle: asyncio.TimerHandle | None = None
        self.closed = False
        self.apps: tuple[float, list[tuple[str, str]]] | None = None
        self.background: set[asyncio.Future[Any]] = set()

    # -- running a command

    async def execute(
        self,
        device: DeviceRecord,
        creds: str,
        op: Op,
        value: Value,
        *,
        command: str,
        retry: bool,
        budget: float,
    ) -> Outcome:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + budget
        try:
            await asyncio.wait_for(self.lock.acquire(), budget)
        except TimeoutError:
            return Outcome.fail("busy", f"The {device.name} is still busy with the last request.")
        try:
            return await self._attempt(device, creds, op, value, command=command, retry=retry, deadline=deadline)
        finally:
            self.lock.release()
            self._arm_idle()

    async def _attempt(
        self, device: DeviceRecord, creds: str, op: Op, value: Value, *, command: str, retry: bool, deadline: float
    ) -> Outcome:
        loop = asyncio.get_running_loop()
        attempts = 2 if retry else 1
        for attempt in range(1, attempts + 1):
            try:
                conn = await self._ready(device, creds, deadline)
            except _StillConnecting:
                return Outcome.fail("timeout", f"The {device.name} is slow to connect. Ask again in a moment.")
            except Exception as exc:  # noqa: BLE001 - every failure becomes a spoken outcome
                log.info("apple tv %s: connecting failed (%s)", device.id, type(exc).__name__)
                return _failure(exc, device, command)
            try:
                return await asyncio.wait_for(op(conn, device, value), max(0.05, deadline - loop.time()))
            except TimeoutError:
                # Out of time mid-command: the link may be half-open, so start afresh next time.
                self.discard(conn)
                return Outcome.fail("timeout", f"The {device.name} didn't answer in time; it may still act on it.")
            except Exception as exc:  # noqa: BLE001 - every failure becomes a spoken outcome
                if not _is_connection_loss(exc):
                    log.info("apple tv %s: %s failed (%s)", device.id, command, type(exc).__name__)
                    return _failure(exc, device, command)
                self.discard(conn)
                if attempt < attempts and deadline - loop.time() > RETRY_MIN_S:
                    log.info("apple tv %s: the connection dropped (%s); retrying once", device.id, type(exc).__name__)
                    continue
                log.info("apple tv %s: %s lost the connection (%s)", device.id, command, type(exc).__name__)
                return _failure(exc, device, command)
        return _unreachable(device, command)  # not reached: the loop always returns

    async def _ready(self, device: DeviceRecord, creds: str, deadline: float) -> _Conn:
        """The open connection, or a new one; waits for a connect only as long as the caller can.

        Joins a connect already running and starts at most one of its own, so a link closed meanwhile,
        or an Apple TV that drops each new session, cannot set off one connect after another.
        """
        loop = asyncio.get_running_loop()
        started = False
        while True:
            if self.closed:
                raise _Closed
            conn = self.conn
            if conn is not None and conn.alive and conn.creds == creds:
                return conn
            if conn is not None:
                self.discard(conn)  # dead, or the Apple TV was paired again since it opened
            task = self.opening
            if task is None:
                if started:
                    # The connection this call opened was gone before it could be used.
                    raise atv_errors.ConnectionLostError("the new connection was dropped")
                task = loop.create_task(self._open(device, creds))
                task.add_done_callback(self._opened)
                self.opening = task
                started = True
            left = deadline - loop.time()
            wait = left - min(COMMAND_RESERVE_S, left / 4)
            if wait <= 0:
                raise _StillConnecting
            try:
                # shield: a caller that runs out of time must not cancel the connect (it would leak a session).
                conn = await asyncio.wait_for(asyncio.shield(task), wait)
            except TimeoutError:
                raise _StillConnecting from None
            if conn.alive and conn.creds == creds:
                return conn

    async def _open(self, device: DeviceRecord, creds: str) -> _Conn:
        settings = device.settings
        address = settings.get("address") if isinstance(settings.get("address"), str) else None
        identifiers = {i for i in settings.get("identifiers") or [] if isinstance(i, str) and i}
        conf = await self._find(device, address, identifiers)
        if conf.get_service(Protocol.Companion) is None:
            raise _NoRemote
        for service in conf.services:
            # Companion only: no AirPlay credentials are kept, and a failing AirPlay link would take Companion down.
            if service.protocol is not Protocol.Companion:
                service.enabled = False
        conf.set_credentials(Protocol.Companion, creds)
        rp_id = settings.get("rp_id")
        atv = await self.driver.api.connect(conf, rp_id=rp_id if isinstance(rp_id, str) and rp_id else None)
        conn = _Conn(self, atv, creds, asyncio.get_running_loop().time())
        atv.listener = conn
        return conn

    async def _find(self, device: DeviceRecord, address: str | None, identifiers: set[str]) -> Any:
        """The Apple TV's config: asked at its saved address first, then looked for by identifier."""
        api = self.driver.api
        if address:
            try:
                found = await api.scan(hosts=[address], identifier=identifiers or None, timeout=UNICAST_SCAN_S)
            except (OSError, ValueError) as exc:
                log.debug("apple tv %s: asking its address failed (%s)", device.id, type(exc).__name__)
                found = []
            if found:
                return found[0]
        if not identifiers:
            raise _NotFound
        try:
            found = await api.scan(identifier=identifiers, timeout=MULTICAST_SCAN_S)
        except (OSError, ValueError) as exc:
            log.debug("apple tv %s: searching the network failed (%s)", device.id, type(exc).__name__)
            found = []
        if not found:
            raise _NotFound
        conf = found[0]
        moved = str(conf.address)
        if moved != address:
            # Saved off the loop (the store waits on a file lock) and without holding up the connect.
            future = asyncio.get_running_loop().run_in_executor(None, self.driver.save_address, device.id, moved)
            self.background.add(future)
            future.add_done_callback(self.background.discard)
        return conf

    def _opened(self, task: asyncio.Task[_Conn]) -> None:
        """A connect finished, whether or not its caller is still waiting: keep it, or close it cleanly."""
        if self.opening is task:
            self.opening = None
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.debug("apple tv %s: connect failed (%s)", self.device_id, type(exc).__name__)
            return
        conn = task.result()
        if self.closed or not conn.alive:
            self.discard(conn)
            return
        if self.conn is not None and self.conn is not conn:
            self.discard(self.conn)
        self.conn = conn
        self._arm_idle()

    # -- keeping and dropping

    def forget(self, conn: _Conn) -> None:
        """A connection reported itself gone; only the current one is forgotten."""
        if self.conn is conn:
            self.conn = None
            self._cancel_idle()

    def discard(self, conn: _Conn) -> None:
        """Stops using ``conn`` and closes it in the background."""
        conn.alive = False
        if self.conn is conn:
            self.conn = None
            self._cancel_idle()
        task = asyncio.get_running_loop().create_task(_close_atv(conn.atv, self.driver.close_wait_s))
        self.background.add(task)
        task.add_done_callback(self.background.discard)

    def _arm_idle(self) -> None:
        self._cancel_idle()
        if self.conn is not None and not self.closed:
            self.idle = asyncio.get_running_loop().call_later(self.driver.idle_close_s, self._on_idle)

    def _cancel_idle(self) -> None:
        if self.idle is not None:
            self.idle.cancel()
            self.idle = None

    def _on_idle(self) -> None:
        self.idle = None
        if self.lock.locked() or self.conn is None:
            return  # a command is running; it re-arms the timer when it ends
        log.debug("apple tv %s: closing the idle connection", self.device_id)
        self.discard(self.conn)

    async def shutdown(self, wait: float) -> None:
        """Closes the connection; a connect still running closes its own result when it lands."""
        self.closed = True
        self._cancel_idle()
        if self.conn is not None:
            self.discard(self.conn)
        pending = set(self.background)
        if pending:
            await asyncio.wait(pending, timeout=wait)


# --------------------------------------------------------------------------- commands


async def _settled(conn: _Conn, read: Callable[[], Any], ready: Callable[[Any], bool]) -> Any:
    """A state that a fresh connection may not have reported yet: wait a moment for it."""
    loop = asyncio.get_running_loop()
    value = read()
    if ready(value) or loop.time() - conn.opened_at > FRESH_S:
        return value
    end = loop.time() + conn.link.driver.settle_s
    while not ready(value) and loop.time() < end:
        await asyncio.sleep(0.05)
        value = read()
    return value


async def _has_feature(conn: _Conn, feature: Any) -> bool:
    def read() -> Any:
        return conn.atv.features.get_feature(feature).state

    return await _settled(conn, read, lambda state: state is FeatureState.Available) is FeatureState.Available


async def _status(conn: _Conn, device: DeviceRecord, value: Value) -> Outcome:
    state = await _settled(conn, lambda: conn.atv.power.power_state, lambda s: s is not PowerState.Unknown)
    old = asyncio.get_running_loop().time() - conn.opened_at > FRESH_S
    if state is PowerState.On and old and not conn.put_to_sleep:
        await _still_answers(conn)
    if state is PowerState.On:
        return Outcome.done(f"The {device.name} is on.")
    if state is PowerState.Off:
        return Outcome.done(f"The {device.name} is asleep.")
    # tvOS 26 sometimes never reports it.
    return Outcome.done(f"I can't tell whether the {device.name} is on: it didn't report its power state.")


async def _still_answers(conn: _Conn) -> None:
    """Proves an older connection still answers before its pushed "on" is believed.

    Companion has no keepalive, so a link left half-open (the Apple TV unplugged or off the
    network) keeps the last pushed state. Only a connection loss counts: any reply, even a
    refusal, proves the link. A request wakes a sleeping Apple TV, so this runs only when it
    says "on" and Jarvis has not put it to sleep (a missed "asleep" push would leave "on").
    """
    try:
        await _app_list(conn, fresh=True)  # read-only, and what launch_app asks anyway
    except Exception as exc:
        if _is_connection_loss(exc):
            raise


async def _turn_on(conn: _Conn, device: DeviceRecord, value: Value) -> Outcome:
    await conn.atv.power.turn_on()
    conn.put_to_sleep = False
    return Outcome.done(f"The {device.name} is waking up.")


async def _turn_off(conn: _Conn, device: DeviceRecord, value: Value) -> Outcome:
    await conn.atv.power.turn_off()
    conn.put_to_sleep = True
    return Outcome.done(f"The {device.name} is going to sleep.")


def _remote(method: str, text: str) -> Op:
    """A remote-control call; ``text`` is the outcome, with ``{name}`` for the device."""

    async def press(conn: _Conn, device: DeviceRecord, value: Value) -> Outcome:
        await getattr(conn.atv.remote_control, method)()
        return Outcome.done(text.format(name=device.name))

    return press


def _volume_step(up: bool) -> Op:
    word = "up" if up else "down"

    async def step(conn: _Conn, device: DeviceRecord, value: Value) -> Outcome:
        if not await _has_feature(conn, FeatureName.Volume):
            return _no_volume(device)
        audio = conn.atv.audio
        try:
            await (audio.volume_up() if up else audio.volume_down())
        except TimeoutError:
            # The key went out; the Apple TV just didn't report a new level.
            return Outcome.done(f"Turned the volume {word} on the {device.name}; it didn't confirm the new level.")
        return Outcome.done(f"Turned the volume {word} on the {device.name}.")

    return step


def _no_volume(device: DeviceRecord) -> Outcome:
    return Outcome.fail(
        "unsupported",
        f"The {device.name} can't change the volume: it can only when it controls the TV's volume over HDMI-CEC. "
        "Use the TV's own volume instead.",
    )


async def _set_volume(conn: _Conn, device: DeviceRecord, value: Value) -> Outcome:
    level = int(value) if isinstance(value, int | float) else 0
    if not await _has_feature(conn, FeatureName.SetVolume):
        return _no_volume(device)
    try:
        await conn.atv.audio.set_volume(float(level))
    except TimeoutError:
        return Outcome.fail(
            "timeout",
            f"The {device.name} didn't confirm the new volume. "
            "It can set the volume only when it controls the TV's volume over HDMI-CEC.",
        )
    return Outcome.done(f"Set the volume on the {device.name} to {level}%.")


async def _app_list(conn: _Conn, *, fresh: bool = False) -> list[tuple[str, str]]:
    """The launchable apps as (name, bundle id), remembered for a while."""
    loop = asyncio.get_running_loop()
    cached = conn.link.apps
    if not fresh and cached is not None and loop.time() - cached[0] < APPS_TTL_S:
        return cached[1]
    apps = [(str(app.name or ""), str(app.identifier)) for app in await conn.atv.apps.app_list() if app.identifier]
    conn.link.apps = (loop.time(), apps)
    return apps


_BUNDLE_ID = re.compile(r"^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+){2,}$")
# Names people use for Apple's own apps, by bundle id.
_APP_ALIASES = {
    "apple tv": "com.apple.TVWatchList",
    "apple tv plus": "com.apple.TVWatchList",
    "tv": "com.apple.TVWatchList",
    "apple music": "com.apple.TVMusic",
    "music": "com.apple.TVMusic",
    "photos": "com.apple.TVPhotos",
    "settings": "com.apple.TVSettings",
    "app store": "com.apple.TVAppStore",
}


def _app_key(text: str) -> str:
    words = normalize(text.replace("+", " plus ")).split()
    kept = [w for w in words if w not in ("app", "application")]
    return " ".join(kept or words)


def match_app(apps: list[tuple[str, str]], wanted: str) -> tuple[tuple[str, str] | None, list[str]]:
    """The app ``wanted`` names: ``(app, [])``, or ``(None, candidates)`` when it could be several, or ``(None, [])``.

    Tries the bundle id, the exact name, a few aliases, a prefix, a part of the name, then a close spelling.
    """
    wanted = wanted.strip()
    if _BUNDLE_ID.match(wanted):
        for app in apps:
            if app[1].lower() == wanted.lower():
                return app, []
    target = _app_key(wanted)
    flat = target.replace(" ", "")
    if not flat:
        return None, []
    keyed = [(app, _app_key(app[0])) for app in apps if app[0]]
    for app, key in keyed:
        if key == target or key.replace(" ", "") == flat:
            return app, []
    alias = _APP_ALIASES.get(target)
    if alias is not None:
        for app in apps:
            if app[1] == alias:
                return app, []
    words = set(target.split())
    tests: list[Callable[[str], bool]] = [
        lambda key: key.replace(" ", "").startswith(flat),
        lambda key: flat in key.replace(" ", "") or words <= set(key.split()),
    ]
    for test in tests:
        found = [app for app, key in keyed if test(key)]
        if len(found) == 1:
            return found[0], []
        if found:
            return None, sorted({app[0] for app in found}, key=str.casefold)
    flats = {key.replace(" ", ""): app for app, key in reversed(keyed)}
    close = difflib.get_close_matches(flat, list(flats), n=3, cutoff=0.75)
    if len(close) == 1:
        return flats[close[0]], []
    return None, sorted({flats[c][0] for c in close}, key=str.casefold)


async def _launch_app(conn: _Conn, device: DeviceRecord, value: Value) -> Outcome:
    wanted = str(value or "").strip()
    # tvOS may ignore a launch without saying so (pyatv #2868), hence "asked".
    link = parse_link(wanted)
    if link is not None:
        return await _open_link(conn, device, link)
    app, candidates = match_app(await _app_list(conn), wanted)
    if app is None:
        if candidates:
            return Outcome.fail("ambiguous", f'"{wanted}" could be {", ".join(candidates[:5])}. Which one?')
        return Outcome.fail(
            "bad_value", f'There is no app called "{wanted}" on the {device.name}. Ask me to list its apps.'
        )
    await conn.atv.apps.launch_app(app[1])
    return Outcome.done(f"Asked the {device.name} to open {app[0]}.")


async def _open_link(conn: _Conn, device: DeviceRecord, link: Link) -> Outcome:
    """Hands the Apple TV a link, which opens in the app that claims it (Companion's ``_urlS``).

    A launch of an app that isn't installed fails as silently as any other, so the app a
    link belongs to is looked for first; its own host alone opens it by its bundle id.
    """
    app = link.app
    if app is None or app.apple_bundle is None:
        await conn.atv.apps.launch_app(link.url)
        return Outcome.done(f"Asked the {device.name} to open that link.")
    if await _installed(conn, app.apple_bundle) is False:
        return Outcome.fail(
            "bad_value",
            f"{app.name} isn't installed on the {device.name}. Install it from the App Store on the {device.name} "
            f"(it needs {app.apple_needs}), then ask again.",
        )
    if link.bare:
        await conn.atv.apps.launch_app(app.apple_bundle)
        return Outcome.done(f"Asked the {device.name} to open {app.name}.")
    await conn.atv.apps.launch_app(link.url)
    if link.channel:
        return Outcome.done(
            f"Asked the {device.name} to open {link.channel} on {app.name}. If {app.name} opens on its home screen "
            "instead, select its search box and I'll type the name."
        )
    return Outcome.done(f"Asked the {device.name} to open that {app.name} link.")


async def _installed(conn: _Conn, bundle: str) -> bool | None:
    """Whether the app is on the Apple TV, or None when its list can't be read (a lost connection is raised).

    A remembered list without it is asked for afresh before saying no: it may have been installed since.
    """
    try:
        remembered = conn.link.apps
        apps = await _app_list(conn)
        if conn.link.apps is remembered and not _has_bundle(apps, bundle):
            apps = await _app_list(conn, fresh=True)
    except Exception as exc:
        if _is_connection_loss(exc):
            raise
        log.debug("apple tv %s: the app list could not be read (%s)", conn.link.device_id, type(exc).__name__)
        return None
    return _has_bundle(apps, bundle)


def _has_bundle(apps: list[tuple[str, str]], bundle: str) -> bool:
    return any(ident.lower() == bundle.lower() for _, ident in apps)


async def _list_apps(conn: _Conn, device: DeviceRecord, value: Value) -> Outcome:
    names = sorted({name for name, _ in await _app_list(conn, fresh=True) if name}, key=str.casefold)
    if not names:
        return Outcome.done(f"The {device.name} didn't list any apps.")
    shown = names[:60]
    more = f", and {len(names) - len(shown)} more" if len(names) > len(shown) else ""
    return Outcome.done(f"Apps on the {device.name}: {', '.join(shown)}{more}.")


async def _type_text(conn: _Conn, device: DeviceRecord, value: Value) -> Outcome:
    text = str(value or "")
    keyboard = conn.atv.keyboard
    # text_set does nothing at all when no field is focused; text_get says so (None).
    if await keyboard.text_get() is None:
        return Outcome.fail(
            "failed", f"No text field is selected on the {device.name}. Open a search box on it first, then ask again."
        )
    await keyboard.text_set(text)
    shown = f'"{text}"' if len(text) <= 60 else "the text"
    return Outcome.done(f"Typed {shown} on the {device.name}.")


# name -> (what the model sees, safe to send twice, what it does). Relative commands
# (a key press, a volume step, a toggle) are never retried: a press must not land twice.
_COMMANDS: dict[str, tuple[CommandSpec, bool, Op]] = {
    "turn_on": (CommandSpec("turn_on"), True, _turn_on),
    "turn_off": (CommandSpec("turn_off"), True, _turn_off),
    "play": (CommandSpec("play"), True, _remote("play", "Pressed play on the {name}.")),
    "pause": (CommandSpec("pause"), True, _remote("pause", "Paused the {name}.")),
    "play_pause": (CommandSpec("play_pause"), False, _remote("play_pause", "Pressed play/pause on the {name}.")),
    "next": (CommandSpec("next"), False, _remote("next", "Skipped to the next item on the {name}.")),
    "previous": (CommandSpec("previous"), False, _remote("previous", "Went back to the previous item on the {name}.")),
    "up": (CommandSpec("up"), False, _remote("up", "Pressed up on the {name}.")),
    "down": (CommandSpec("down"), False, _remote("down", "Pressed down on the {name}.")),
    "left": (CommandSpec("left"), False, _remote("left", "Pressed left on the {name}.")),
    "right": (CommandSpec("right"), False, _remote("right", "Pressed right on the {name}.")),
    "select": (CommandSpec("select"), False, _remote("select", "Pressed select on the {name}.")),
    "menu": (CommandSpec("menu"), False, _remote("menu", "Pressed Menu on the {name}.")),
    "back": (CommandSpec("back"), False, _remote("menu", "Pressed Back on the {name}.")),
    "home": (CommandSpec("home"), False, _remote("home", "Pressed Home on the {name}.")),
    "volume_up": (CommandSpec("volume_up"), False, _volume_step(True)),
    "volume_down": (CommandSpec("volume_down"), False, _volume_step(False)),
    "set_volume": (CommandSpec("set_volume", "percent"), True, _set_volume),
    "launch_app": (CommandSpec("launch_app", "text", hint="an app name or a link"), True, _launch_app),
    "list_apps": (CommandSpec("list_apps"), True, _list_apps),
    "type_text": (CommandSpec("type_text", "text", hint="text for the selected search box"), True, _type_text),
}


# --------------------------------------------------------------------------- the driver


class AppleTvDriver(Driver):
    name = "appletv"
    label = "Apple TV"

    def __init__(self, ctx: DriverContext, *, api: Any | None = None) -> None:
        super().__init__(ctx)
        self.api: Any = api if api is not None else PyatvApi()
        # Instance settings so tests can shorten them.
        self.idle_close_s = IDLE_CLOSE_S
        self.settle_s = SETTLE_S
        self.close_wait_s = CLOSE_WAIT_S
        self._mutex = threading.Lock()
        self._links: dict[str, _Link] = {}
        self._runner: AsyncRunner | None = None

    def commands(self, device: DeviceRecord) -> list[CommandSpec]:
        return [spec for spec, _, _ in _COMMANDS.values()]

    def run(self, device: DeviceRecord, command: str, value: Value) -> Outcome:
        entry = _COMMANDS.get(command)
        if entry is None:
            return Outcome.fail("unsupported", f"The {device.name} can't {command.replace('_', ' ')}.")
        _, retry, op = entry
        return self._call(device, op, value, command=command, retry=retry)

    def status(self, device: DeviceRecord) -> Outcome:
        # With no open connection this connects, and the Apple TV wakes for any request (and the TV
        # with it over HDMI-CEC): there is no passive way to ask. Hence on demand only, never polled.
        return self._call(device, _status, None, command="status", retry=True)

    def describe(self) -> str | None:
        config = self.ctx.store.load()
        devices = [d for d in config.devices if d.driver == self.name]
        if not devices:
            return None
        try:
            paired = set(self.ctx.store.secret_keys())
        except StoreError:
            paired = set()
        unpaired = [d.name for d in devices if d.secret_key not in paired]
        line = f"Apple TV: {len(devices)} set up"
        if unpaired:
            line += f"; not paired: {', '.join(unpaired)} (pair it in home setup)"
        return line

    def close(self) -> None:
        with self._mutex:
            links = list(self._links.values())
            self._links = {}
            runner = self._runner
        if not links or runner is None:
            return
        wait = self.close_wait_s

        async def close_all() -> None:
            await asyncio.gather(*(link.shutdown(wait) for link in links), return_exceptions=True)

        try:
            runner.call(close_all(), wait + 1.0)
        except Exception as exc:  # noqa: BLE001 - never hold up the helper's shutdown
            log.debug("closing the Apple TV connections: %s", type(exc).__name__)

    def save_address(self, device_id: str, address: str) -> None:
        """Remembers where a moved Apple TV is now (runs on a worker thread: the store may wait on its lock)."""

        def move(config: HomeConfig) -> None:
            device = config.device(device_id)
            if device is not None:
                device.settings = {**device.settings, "address": address}

        try:
            self.ctx.store.update(move)
            log.info("apple tv %s: found at a new address; saved it", device_id)
        except (StoreError, OSError) as exc:
            log.warning("apple tv %s: could not save its new address (%s)", device_id, type(exc).__name__)

    # -- helpers

    def _credentials(self, device: DeviceRecord) -> str | Outcome:
        try:
            secret = self.ctx.store.secret(device.secret_key)
        except StoreError:
            return Outcome.fail(
                "needs_setup", f"Jarvis couldn't read the pairing of the {device.name}. Pair it again in home setup."
            )
        creds = secret.get("companion") if secret else None
        if not isinstance(creds, str) or not creds.strip():
            return Outcome.fail("needs_setup", f"The {device.name} isn't paired with Jarvis. Pair it in home setup.")
        return creds

    def _link(self, device_id: str) -> tuple[_Link, AsyncRunner]:
        runner = self.ctx.runner()
        with self._mutex:
            if runner is not self._runner:
                # A new event loop: the connections of the old one went with it.
                self._links = {}
                self._runner = runner
            link = self._links.get(device_id)
            if link is None:
                link = self._links[device_id] = _Link(self, device_id)
            return link, runner

    def _call(self, device: DeviceRecord, op: Op, value: Value, *, command: str, retry: bool) -> Outcome:
        creds = self._credentials(device)
        if isinstance(creds, Outcome):
            return creds
        # Within the service's limit (20 s), so the answer is ours, not the service's "timeout".
        budget = max(1.0, self.ctx.call_timeout * 0.85)
        try:
            link, runner = self._link(device.id)
            return runner.call(
                link.execute(device, creds, op, value, command=command, retry=retry, budget=budget), budget + 1.0
            )
        except TimeoutError:
            return Outcome.fail("timeout", f"The {device.name} didn't answer in time; it may still act on it.")
        except RuntimeError as exc:  # the event loop has been stopped: the helper is shutting down
            log.debug("apple tv %s: %s", device.id, type(exc).__name__)
            return Outcome.fail("failed", "Home control is shutting down.")


# --------------------------------------------------------------------------- the setup wizard


def wizard(ui: Prompter, ctx: DriverContext, *, api: Any | None = None) -> None:
    """Finds an Apple TV, pairs Jarvis with it (a PIN on the TV), names it and saves it."""
    api = api if api is not None else PyatvApi()
    runner = ctx.runner()
    ui.say("Add an Apple TV")
    ui.say(ALLOW_ACCESS)
    ui.say("Also give the Apple TV a fixed address on your router (a DHCP reservation), so Jarvis finds it every time.")
    conf = _pick_apple_tv(ui, runner, api)
    if conf is None:
        return
    companion = conf.get_service(Protocol.Companion)
    if companion is None or companion.pairing in (PairingRequirement.Unsupported, PairingRequirement.NotNeeded):
        ui.say("That device can't be paired as a remote. Pick an Apple TV.")
        return
    if companion.pairing is PairingRequirement.Disabled:
        ui.say(f"{conf.name} doesn't allow pairing yet. {ALLOW_ACCESS} Then add it again.")
        return

    existing = _already_set_up(ctx.store.load(), conf)
    if existing is not None:
        ui.say(f"This Apple TV is already set up as {existing.name}. Pairing again replaces its old pairing.")
        if not ui.confirm("Pair it again?", default=True):
            return
        name, room = existing.name, existing.room
        rp_id = existing.settings.get("rp_id") if isinstance(existing.settings.get("rp_id"), str) else None
    else:
        name = ui.ask("Name for this Apple TV", conf.name or "Apple TV").strip() or conf.name or "Apple TV"
        room = ui.ask("Room (empty for none)").strip() or None
        rp_id = None
    rp_id = rp_id or secrets.token_hex(6)

    creds = _pair(ui, runner, api, conf, rp_id)
    if creds is None:
        return
    record = _save(ctx, conf, creds, rp_id, name, room, existing)
    ui.say(f"Paired and saved {record.name}. Jarvis can control it now.")
    if ui.confirm(f"Check it now by asking whether {record.name} is on?", default=True):
        driver = AppleTvDriver(ctx, api=api)
        try:
            ui.say(driver.status(record).text)
        finally:
            driver.close()


def _pick_apple_tv(ui: Prompter, runner: AsyncRunner, api: Any) -> Any:
    ui.say("Looking for Apple TVs on your network (a few seconds)...")
    try:
        found = runner.call(api.scan(timeout=SETUP_SCAN_S), SETUP_SCAN_S + 10)
    except Exception as exc:  # noqa: BLE001 - offer the address instead
        log.info("apple tv setup: searching the network failed (%s)", type(exc).__name__)
        ui.say("Searching the network didn't work. You can type the Apple TV's address instead.")
        found = []
    tvs = sorted((c for c in found if _looks_like_apple_tv(c)), key=lambda c: str(c.name).casefold())
    if not tvs:
        ui.say("No Apple TV answered. Check it is on, then type its address.")
    options = [_found_label(c) for c in tvs] + ["Type the Apple TV's address"]
    index = ui.choose("Which Apple TV?", options)
    if index is None:
        return None
    if index < len(tvs):
        return tvs[index]
    return _ask_address(ui, runner, api)


def _ask_address(ui: Prompter, runner: AsyncRunner, api: Any) -> Any:
    ui.say("The Apple TV shows its address under Settings > Network (for example 192.168.1.20).")
    for _ in range(3):
        text = ui.ask("Apple TV address (empty to go back)").strip()
        if not text:
            return None
        try:
            ipaddress.IPv4Address(text)
        except ValueError:
            ui.say("That isn't an address like 192.168.1.20.")
            continue
        try:
            found = runner.call(api.scan(hosts=[text], timeout=SETUP_SCAN_S), SETUP_SCAN_S + 10)
        except Exception as exc:  # noqa: BLE001 - said in words below
            log.info("apple tv setup: asking an address failed (%s)", type(exc).__name__)
            found = []
        tvs = [c for c in found if c.get_service(Protocol.Companion) is not None]
        if tvs:
            return tvs[0]
        ui.say(f"No Apple TV answered at {text}. Check it is on and on the same network as this computer.")
    return None


def _looks_like_apple_tv(conf: Any) -> bool:
    """Apple TVs offer Companion pairing; HomePods, Macs and iPads list the service but can't pair."""
    companion = conf.get_service(Protocol.Companion)
    if companion is None:
        return False
    if conf.device_info.model in (DeviceModel.HomePod, DeviceModel.HomePodMini, DeviceModel.HomePodGen2):
        return False
    return companion.pairing in (PairingRequirement.Mandatory, PairingRequirement.Optional, PairingRequirement.Disabled)


def _model_name(conf: Any) -> str | None:
    info = conf.device_info
    return info.model_str if info.model is not DeviceModel.Unknown else None


def _found_label(conf: Any) -> str:
    model = _model_name(conf)
    off = "; pairing is not allowed yet" if _pairing(conf) is PairingRequirement.Disabled else ""
    return f"{conf.name} ({model + ', ' if model else ''}{conf.address}{off})"


def _pairing(conf: Any) -> Any:
    companion = conf.get_service(Protocol.Companion)
    return companion.pairing if companion is not None else None


def _already_set_up(config: HomeConfig, conf: Any) -> DeviceRecord | None:
    ids = set(conf.all_identifiers)
    for device in config.devices:
        if device.driver == DRIVER and ids & set(device.settings.get("identifiers") or []):
            return device
    return None


def _pair(ui: Prompter, runner: AsyncRunner, api: Any, conf: Any, rp_id: str) -> str | None:
    """Pairs Companion: the TV shows a PIN, the user types it. Returns the credentials, or None."""
    companion = conf.get_service(Protocol.Companion)
    for attempt in range(1, PIN_TRIES + 1):
        if attempt > 1:
            # Started straight after a wrong PIN, a new pairing meets the Apple TV's back-off and is refused.
            time.sleep(PIN_RETRY_WAIT_S)
        # Stale credentials would make pairing start with a verify that fails.
        companion.credentials = None
        try:
            handler = runner.call(api.pair(conf, rp_id=rp_id), PAIR_STEP_S)
        except Exception as exc:  # noqa: BLE001 - said in words
            log.info("apple tv setup: pairing could not start (%s)", type(exc).__name__)
            ui.say("Pairing could not start. Check the Apple TV is on, then try again.")
            return None
        try:
            try:
                runner.call(handler.begin(), PAIR_STEP_S)
            except Exception as exc:  # noqa: BLE001 - said in words
                log.info("apple tv setup: the Apple TV did not start pairing (%s)", type(exc).__name__)
                ui.say(_begin_failure(exc, after_wrong_pin=attempt > 1))
                return None
            ui.say(
                "The TV now shows a 4-digit code. If no code appears, press Enter to stop, "
                "then restart the Apple TV (Settings > System > Restart) and try again."
            )
            watchdog = runner.submit(_close_later(handler, PIN_WAIT_S))
            try:
                pin = _ask_pin(ui)
            finally:
                expired = not watchdog.cancel()  # cancel fails only when it already fired
            if expired:
                ui.say("The code expired. Add the Apple TV again when you are ready.")
                return None
            if pin is None:
                ui.say("Stopped. Nothing was saved.")
                return None
            handler.pin(int(pin))
            try:
                runner.call(handler.finish(), PAIR_STEP_S)
            except (atv_errors.PairingError, atv_errors.AuthenticationError):
                if attempt < PIN_TRIES:
                    ui.say(
                        "That code didn't work. The TV will show a new one in a few seconds. Careful: several "
                        "wrong codes can block pairing until the Apple TV restarts."
                    )
                    continue
                ui.say(
                    f"That code didn't work either. Stopping after {PIN_TRIES} tries: more failed tries can lock "
                    f"pairing until the Apple TV restarts. {RESTART_HINT}"
                )
                return None
            except Exception as exc:  # noqa: BLE001 - said in words
                log.info("apple tv setup: pairing failed (%s)", type(exc).__name__)
                ui.say("Pairing failed: the Apple TV stopped answering. Try again.")
                return None
            creds = handler.service.credentials
            if handler.has_paired and isinstance(creds, str) and creds:
                return creds
            ui.say("Pairing didn't finish. Try again.")
            return None
        finally:
            _close_pairing(runner, handler)
    return None


def _begin_failure(exc: BaseException, *, after_wrong_pin: bool = False) -> str:
    if isinstance(exc, atv_errors.ConnectionFailedError | OSError):
        return "Couldn't reach the Apple TV to pair. Check it is on and on the same network, then try again."
    if after_wrong_pin:
        # Its back-off after a wrong PIN grows with each one; pyatv reports it as a plain refusal.
        return (
            "The Apple TV is holding off pairing after the wrong code. Wait a minute, then add it again. "
            f"If it still refuses: {RESTART_HINT}"
        )
    return f"The Apple TV refused to start pairing. {ALLOW_ACCESS} If it still fails: {RESTART_HINT}"


def _ask_pin(ui: Prompter) -> str | None:
    for _ in range(3):
        text = ui.ask("Type the 4-digit code shown on the TV (Enter to stop)").strip().replace(" ", "")
        if not text:
            return None
        if text.isdigit() and len(text) == 4:
            return text
        ui.say("The code is the 4 digits on the TV screen.")
    return None


async def _close_later(handler: Any, delay: float) -> bool:
    """Closes a pairing whose PIN never came, so the TV does not keep its dialog and connection open."""
    await asyncio.sleep(delay)
    with contextlib.suppress(Exception):
        await handler.close()
    return True


def _close_pairing(runner: AsyncRunner, handler: Any) -> None:
    try:
        runner.call(handler.close(), 5.0)
    except Exception as exc:  # noqa: BLE001 - closing is best effort
        log.debug("apple tv setup: closing the pairing: %s", type(exc).__name__)


def _save(
    ctx: DriverContext,
    conf: Any,
    creds: str,
    rp_id: str,
    name: str,
    room: str | None,
    existing: DeviceRecord | None,
) -> DeviceRecord:
    """Saves the device and its credentials together: if either fails, a new device is taken out again."""
    settings: dict[str, Any] = {
        "address": str(conf.address),
        "identifiers": sorted(set(conf.all_identifiers)),
        "rp_id": rp_id,
    }
    model = _model_name(conf)
    if model:
        settings["model"] = model
    saved: list[DeviceRecord] = []

    def add(config: HomeConfig) -> None:
        if existing is not None:
            current = config.device(existing.id) or existing
            current.settings = {**current.settings, **settings}
            record = current
        else:
            record = DeviceRecord(unique_id(config.ids(), DRIVER, name), DRIVER, name, "media_player", room)
            record.settings = settings
        config.upsert(record)
        saved.append(record)

    try:
        ctx.store.update(add)
        ctx.store.set_secret(saved[0].secret_key, {"companion": creds})
    except BaseException:
        if saved and existing is None:
            with contextlib.suppress(Exception):
                ctx.store.update(lambda c: c.remove(saved[0].id))
        raise
    return saved[0]
