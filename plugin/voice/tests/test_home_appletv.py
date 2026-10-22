"""The Apple TV driver against a fake pyatv: its connection manager, commands, spoken failures and setup wizard.

Nothing here touches the network: the driver's pyatv seam (scan, connect, pair) is a fake
that hands out duck-typed Apple TV objects, while configs, enums and exceptions are pyatv's own.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import re
import threading
import time
from collections.abc import Callable, Iterator
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any

import pytest
from pyatv import exceptions as atv_errors
from pyatv.conf import AppleTV, ManualService
from pyatv.const import DeviceModel, FeatureName, FeatureState, PairingRequirement, PowerState, Protocol
from pyatv.interface import App, DeviceInfo, FeatureInfo

from jarvis_voice.home import appletv
from jarvis_voice.home.appletv import AppleTvDriver, match_app
from jarvis_voice.home.base import DriverContext
from jarvis_voice.home.model import DeviceRecord, Outcome
from jarvis_voice.home.service import HomeService
from jarvis_voice.home.store import HomeStore

from home_fakes import ScriptedPrompter, make_store

# Obviously fake values (TEST-NET addresses, made-up ids and credentials).
CREDS = "not-a-real-companion-credential"
OLD_ADDRESS = "192.0.2.10"
NEW_ADDRESS = "192.0.2.77"
COMPANION_ID = "fake-companion-id"
AIRPLAY_ID = "AA:BB:CC:00:00:01"
RP_ID = "0123456789ab"
DEVICE_ID = "appletv-living-room"

APPS = [
    ("Netflix", "com.netflix.Netflix"),
    ("YouTube", "com.google.ios.youtube"),
    ("YouTube Kids", "com.google.ios.youtubekids"),
    ("TV", "com.apple.TVWatchList"),
    ("Music", "com.apple.TVMusic"),
    ("Disney+", "com.disney.disneyplus"),
    ("Prime Video", "com.amazon.aiv.AIVApp"),
    ("Plex", "com.plexapp.plex"),
]


# --------------------------------------------------------------------------- the fake pyatv


def make_conf(
    address: str = OLD_ADDRESS,
    *,
    name: str = "Living Room",
    pairing: PairingRequirement = PairingRequirement.Mandatory,
    model: DeviceModel = DeviceModel.AppleTV4KGen3,
    credentials: str | None = None,
) -> AppleTV:
    conf = AppleTV(IPv4Address(address), name, device_info=DeviceInfo({DeviceInfo.MODEL: model}))
    conf.add_service(
        ManualService(COMPANION_ID, Protocol.Companion, 49153, {}, credentials, pairing_requirement=pairing)
    )
    conf.add_service(ManualService(AIRPLAY_ID, Protocol.AirPlay, 7000, {}))
    return conf


def lost() -> atv_errors.ProtocolError:
    """How Companion reports a half-open or closed link: a ProtocolError caused by a timeout."""
    exc = atv_errors.ProtocolError("Command _hidC failed")
    exc.__cause__ = TimeoutError()
    return exc


def refused() -> atv_errors.ProtocolError:
    """How Companion reports the Apple TV turning a command down (an _em reply): no cause."""
    return atv_errors.ProtocolError("Command failed: not now")


class FakePyatv:
    """The driver's pyatv seam: devices by address, scripted failures and delays, and a log of what happened."""

    def __init__(self) -> None:
        self.devices: dict[str, AppleTV] = {OLD_ADDRESS: make_conf()}
        self.multicast_hidden = False
        self.scans: list[tuple[str, list[str], set[str]]] = []
        self.connects = 0
        self.connect_done = 0
        self.connect_cancelled = 0
        self.connect_delay = 0.0
        self.connect_errors: list[BaseException] = []
        self.connect_log: list[dict[str, Any]] = []
        self.connected: list[FakeAtv] = []
        self.calls: list[str] = []
        self.failures: dict[str, list[BaseException]] = {}
        self.power_state = PowerState.On
        self.features: dict[FeatureName, FeatureState] = {}
        self.later: list[tuple[float, Callable[[], None]]] = []  # run after each connect
        self.apps = list(APPS)
        self.app_lists = 0
        self.focused = True
        self.close_delay = 0.0
        self.pin = "1234"
        self.pairings: list[FakePairing] = []
        self.pair_rp_ids: list[str | None] = []
        self.begin_error: BaseException | None = None
        self.backoff_s = 0.0  # how long a wrong PIN makes the TV refuse a new pairing
        self.backoff_until = 0.0
        self.drop_new = False  # the Apple TV drops each new session as soon as it opens

    async def scan(
        self, *, hosts: list[str] | None = None, identifier: set[str] | None = None, timeout: int = 3
    ) -> list[AppleTV]:
        kind = "unicast" if hosts else "multicast"
        self.scans.append((kind, list(hosts or []), set(identifier or ())))
        if kind == "multicast" and self.multicast_hidden:
            return []
        confs = [copy.deepcopy(c) for address, c in self.devices.items() if not hosts or address in hosts]
        if identifier:
            confs = [c for c in confs if set(identifier) & set(c.all_identifiers)]
        return confs

    async def connect(self, conf: AppleTV, *, rp_id: str | None) -> FakeAtv:
        self.connects += 1
        companion = conf.get_service(Protocol.Companion)
        self.connect_log.append(
            {
                "credentials": companion.credentials if companion else None,
                "rp_id": rp_id,
                "enabled": {s.protocol for s in conf.services if s.enabled},
                "address": str(conf.address),
            }
        )
        try:
            if self.connect_delay:
                await asyncio.sleep(self.connect_delay)
        except asyncio.CancelledError:
            self.connect_cancelled += 1
            raise
        if self.connect_errors:
            raise self.connect_errors.pop(0)
        atv = FakeAtv(self)
        self.connected.append(atv)
        self.connect_done += 1
        loop = asyncio.get_running_loop()
        if self.drop_new:
            loop.call_soon(atv.drop)
        for delay, action in self.later:
            loop.call_later(delay, action)
        return atv

    async def pair(self, conf: AppleTV, *, rp_id: str | None) -> FakePairing:
        self.pair_rp_ids.append(rp_id)
        handler = FakePairing(self, conf.get_service(Protocol.Companion))
        self.pairings.append(handler)
        return handler

    async def hit(self, atv: FakeAtv, name: str, *args: Any) -> None:
        atv.calls.append((name, *args))
        self.calls.append(name)
        if atv.dropped:
            raise lost()
        queue = self.failures.get(name)
        if queue:
            raise queue.pop(0)


class FakeAtv:
    """What pyatv.connect returns, as far as the driver uses it."""

    def __init__(self, world: FakePyatv) -> None:
        self.world = world
        self.calls: list[tuple[Any, ...]] = []
        self.closed = False
        self.dropped = False
        self.listener: Any = None
        self.power = _Power(self)
        self.remote_control = _Remote(self)
        self.audio = _Audio(self)
        self.apps = _Apps(self)
        self.keyboard = _Keyboard(self)
        self.features = _Features(self)

    def close(self) -> set[asyncio.Task[None]]:
        self.closed = True

        async def goodbye() -> None:
            await asyncio.sleep(self.world.close_delay)

        return {asyncio.get_running_loop().create_task(goodbye())}

    def drop(self) -> None:
        """The Apple TV ends the session: pyatv tells the listener."""
        self.dropped = True
        if self.listener is not None:
            self.listener.connection_lost(ConnectionResetError())


class _Part:
    def __init__(self, atv: FakeAtv) -> None:
        self.atv = atv

    async def _do(self, name: str, *args: Any) -> None:
        await self.atv.world.hit(self.atv, name, *args)


class _Power(_Part):
    @property
    def power_state(self) -> PowerState:
        return self.atv.world.power_state

    async def turn_on(self) -> None:
        await self._do("turn_on")

    async def turn_off(self) -> None:
        await self._do("turn_off")


_BUTTONS = {"up", "down", "left", "right", "select", "menu", "home", "play", "pause", "play_pause", "next", "previous"}


class _Remote(_Part):
    def __getattr__(self, name: str) -> Callable[[], Any]:
        if name not in _BUTTONS:
            raise AttributeError(name)

        async def press() -> None:
            await self._do(name)

        return press


class _Audio(_Part):
    async def set_volume(self, level: float) -> None:
        await self._do("set_volume", level)

    async def volume_up(self) -> None:
        await self._do("volume_up")

    async def volume_down(self) -> None:
        await self._do("volume_down")


class _Apps(_Part):
    async def app_list(self) -> list[App]:
        self.atv.world.app_lists += 1
        await self._do("app_list")
        return [App(name, ident) for name, ident in self.atv.world.apps]

    async def launch_app(self, bundle_id_or_url: str) -> None:
        await self._do("launch_app", bundle_id_or_url)


class _Keyboard(_Part):
    async def text_get(self) -> str | None:
        await self._do("text_get")
        return "" if self.atv.world.focused else None

    async def text_set(self, text: str) -> None:
        await self._do("text_set", text)


class _Features(_Part):
    def get_feature(self, name: FeatureName) -> FeatureInfo:
        return FeatureInfo(self.atv.world.features.get(name, FeatureState.Available))


class FakePairing:
    """A Companion pairing handler: the TV "shows" ``world.pin``."""

    def __init__(self, world: FakePyatv, service: Any) -> None:
        self.world = world
        self.service = service
        self.creds_at_start = service.credentials
        self.began = False
        self.closed = False
        self.pin_code: str | None = None
        self.has_paired = False

    async def begin(self) -> None:
        if self.world.begin_error is not None:
            raise self.world.begin_error
        if time.monotonic() < self.world.backoff_until:
            # How pyatv reports the TV's TLV back-off: an AuthenticationError re-raised as PairingError.
            raise atv_errors.PairingError("Error=BackOff, BackOff=1s")
        self.began = True

    def pin(self, pin: int) -> None:
        self.pin_code = str(pin).zfill(4)

    async def finish(self) -> None:
        if self.closed or self.pin_code != self.world.pin:
            self.world.backoff_until = time.monotonic() + self.world.backoff_s
            raise atv_errors.PairingError("pairing failed")
        self.service.credentials = CREDS
        self.has_paired = True

    async def close(self) -> None:
        self.closed = True


# --------------------------------------------------------------------------- helpers


def wait_until(condition: Callable[[], bool], timeout: float = 3.0) -> None:
    end = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > end:
            raise AssertionError("timed out waiting for the condition")
        time.sleep(0.01)


def save_device(store: HomeStore, *, creds: str | None = CREDS, address: str = OLD_ADDRESS) -> DeviceRecord:
    record = DeviceRecord(
        DEVICE_ID,
        "appletv",
        "Apple TV",
        "media_player",
        "Living room",
        settings={"address": address, "identifiers": [AIRPLAY_ID, COMPANION_ID], "rp_id": RP_ID},
    )
    store.update(lambda c: c.upsert(record))
    if creds:
        store.set_secret(record.secret_key, {"companion": creds})
    return record


class Rig:
    """A driver over a temporary store with one saved Apple TV, timings shortened for tests."""

    def __init__(self, tmp_path: Path, world: FakePyatv, *, call_timeout: float = 4.0, creds: str | None = CREDS):
        self.world = world
        self.store = make_store(tmp_path)
        self.ctx = DriverContext(self.store, tmp_path, call_timeout)
        self.driver = AppleTvDriver(self.ctx, api=world)
        self.driver.idle_close_s = 30.0
        self.driver.settle_s = 0.05
        self.driver.close_wait_s = 1.0
        save_device(self.store, creds=creds)

    @property
    def device(self) -> DeviceRecord:
        found = self.store.load().device(DEVICE_ID)
        assert found is not None
        return found

    def run(self, command: str, value: Any = None) -> Outcome:
        return self.driver.run(self.device, command, value)

    def status(self) -> Outcome:
        return self.driver.status(self.device)

    def on_loop(self, action: Callable[[], None]) -> None:
        """Calls ``action`` on the event loop thread, where pyatv calls listeners."""

        async def go() -> None:
            action()

        self.ctx.runner().call(go(), 2)

    def close(self) -> None:
        self.driver.close()
        self.ctx.close()


def age(rig: Rig, atv: FakeAtv, seconds: float = 60.0) -> None:
    """Makes the connection to ``atv`` look ``seconds`` older."""
    conn = atv.listener
    rig.on_loop(lambda: setattr(conn, "opened_at", conn.opened_at - seconds))


@pytest.fixture(autouse=True)
def quick_pin_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(appletv, "PIN_RETRY_WAIT_S", 0.01)


@pytest.fixture
def world() -> FakePyatv:
    return FakePyatv()


@pytest.fixture
def rig(tmp_path: Path, world: FakePyatv) -> Iterator[Rig]:
    made = Rig(tmp_path, world)
    try:
        yield made
    finally:
        made.close()


# --------------------------------------------------------------------------- commands and the connection


def test_commands_come_from_saved_data_without_the_network(rig: Rig, world: FakePyatv) -> None:
    specs = {spec.name: spec for spec in rig.driver.commands(rig.device)}
    assert list(specs) == [
        "turn_on",
        "turn_off",
        "play",
        "pause",
        "play_pause",
        "next",
        "previous",
        "up",
        "down",
        "left",
        "right",
        "select",
        "menu",
        "back",
        "home",
        "volume_up",
        "volume_down",
        "set_volume",
        "launch_app",
        "list_apps",
        "type_text",
    ]
    assert specs["set_volume"].value == "percent"
    assert specs["launch_app"].usage() == "launch_app <an app name or a link>"
    assert specs["type_text"].value == "text"
    assert all(spec.tier == "free" for spec in specs.values())
    assert world.scans == [] and world.connects == 0
    assert rig.driver.run(rig.device, "set_input", "HDMI 1").code == "unsupported"


def test_connects_lazily_reuses_the_connection_and_announces_one_client(rig: Rig, world: FakePyatv) -> None:
    assert world.connects == 0
    first = rig.run("play")
    assert first == Outcome.done("Pressed play on the Apple TV.")
    assert rig.run("pause") == Outcome.done("Paused the Apple TV.")
    assert world.connects == 1
    atv = world.connected[0]
    assert atv.calls == [("play",), ("pause",)]
    assert world.scans == [("unicast", [OLD_ADDRESS], {AIRPLAY_ID, COMPANION_ID})]
    # Companion only, with the saved credentials and the saved client identity.
    assert world.connect_log == [
        {"credentials": CREDS, "rp_id": RP_ID, "enabled": {Protocol.Companion}, "address": OLD_ADDRESS}
    ]
    assert isinstance(atv.listener, appletv._Conn)
    assert not atv.closed


def test_an_idle_connection_closes_and_the_next_command_reconnects(rig: Rig, world: FakePyatv) -> None:
    rig.driver.idle_close_s = 0.5
    for _ in range(4):  # use keeps it open
        assert rig.run("play").ok
        time.sleep(0.1)
    first = world.connected[0]
    assert not first.closed
    wait_until(lambda: first.closed)
    assert rig.run("pause").ok
    assert world.connects == 2
    assert world.connected[1].calls == [("pause",)]


def test_idempotent_commands_retry_once_after_a_lost_connection(rig: Rig, world: FakePyatv) -> None:
    assert rig.run("play").ok
    world.failures["turn_off"] = [lost()]
    assert rig.run("turn_off") == Outcome.done("The Apple TV is going to sleep.")
    assert world.calls.count("turn_off") == 2
    assert world.connects == 2
    wait_until(lambda: world.connected[0].closed)

    world.failures["turn_on"] = [lost(), lost()]
    out = rig.run("turn_on")
    assert out.code == "unreachable" and out.text == "The Apple TV stopped answering. Try again in a moment."
    assert world.calls.count("turn_on") == 2  # once, then one retry: never a third time
    assert world.connects == 3


@pytest.mark.parametrize(
    ("command", "call"),
    [
        ("up", "up"),
        ("select", "select"),
        ("back", "menu"),
        ("home", "home"),
        ("play_pause", "play_pause"),
        ("next", "next"),
        ("volume_up", "volume_up"),
    ],
)
def test_relative_commands_are_never_retried(rig: Rig, world: FakePyatv, command: str, call: str) -> None:
    assert rig.run("pause").ok
    world.failures[call] = [lost()]
    out = rig.run(command)
    assert not out.ok and out.code == "unreachable"
    assert world.calls.count(call) == 1
    assert world.connects == 1
    # The broken link was dropped, so the next request starts afresh.
    assert rig.run(command).ok
    assert world.connects == 2


def test_a_refusal_is_not_mistaken_for_a_lost_connection(rig: Rig, world: FakePyatv) -> None:
    world.failures["launch_app"] = [refused()]
    out = rig.run("launch_app", "Netflix")
    assert out == Outcome.fail("failed", 'The Apple TV didn\'t accept that command. It said: "not now".')
    assert world.calls.count("launch_app") == 1
    world.failures["play"] = [refused()]
    assert rig.run("play").text == 'The Apple TV didn\'t accept that; nothing may be playing. It said: "not now".'
    assert world.connects == 1 and not world.connected[0].closed


def test_a_dropped_connection_is_replaced_and_a_late_callback_keeps_the_new_one(rig: Rig, world: FakePyatv) -> None:
    assert rig.run("play").ok
    first = world.connected[0]
    old_listener = first.listener
    rig.on_loop(lambda: old_listener.connection_lost(ConnectionResetError()))
    assert rig.run("pause").ok
    assert world.connects == 2
    second = world.connected[1]
    # The old connection's close echoes back late: it must not clear the new connection.
    rig.on_loop(old_listener.connection_closed)
    assert rig.run("play").ok
    assert world.connects == 2
    assert second.calls == [("pause",), ("play",)]
    assert second.listener is not old_listener


def test_a_slow_connect_is_never_cancelled_and_is_reused(tmp_path: Path, world: FakePyatv) -> None:
    rig = Rig(tmp_path, world, call_timeout=2.0)  # the driver keeps ~1.7 s of it, ~1.3 s for connecting
    try:
        world.connect_delay = 1.9
        started = time.monotonic()
        out = rig.run("play")
        assert out == Outcome.fail("timeout", "The Apple TV is slow to connect. Ask again in a moment.")
        assert time.monotonic() - started < 1.8
        # The next request joins the connect still running instead of starting another.
        assert rig.run("pause").ok
        assert world.connects == 1 and world.connect_done == 1 and world.connect_cancelled == 0
        assert world.connected[0].calls == [("pause",)]
        assert not world.connected[0].closed
    finally:
        rig.close()


def test_a_connect_finishing_after_its_caller_gave_up_is_kept_for_later(tmp_path: Path, world: FakePyatv) -> None:
    rig = Rig(tmp_path, world, call_timeout=1.2)
    try:
        world.connect_delay = 1.3
        assert rig.run("play").code == "timeout"
        wait_until(lambda: world.connect_done == 1)
        time.sleep(0.05)
        assert rig.run("pause").ok
        assert world.connects == 1 and world.connect_cancelled == 0
    finally:
        rig.close()


def test_a_moved_apple_tv_is_found_by_multicast_and_its_new_address_saved(rig: Rig, world: FakePyatv) -> None:
    world.devices = {NEW_ADDRESS: make_conf(NEW_ADDRESS)}
    rig.driver.idle_close_s = 0.05
    assert rig.run("play").ok
    assert [scan[0] for scan in world.scans] == ["unicast", "multicast"]
    assert world.scans[1][2] == {AIRPLAY_ID, COMPANION_ID}
    wait_until(lambda: rig.device.settings["address"] == NEW_ADDRESS)
    assert rig.device.settings["identifiers"] == [AIRPLAY_ID, COMPANION_ID]
    assert rig.device.settings["rp_id"] == RP_ID
    # The next connection asks the new address straight away.
    wait_until(lambda: world.connected[0].closed)
    world.scans.clear()
    assert rig.run("pause").ok
    assert world.scans == [("unicast", [NEW_ADDRESS], {AIRPLAY_ID, COMPANION_ID})]


@pytest.mark.parametrize(
    ("setup", "code", "text"),
    [
        (
            lambda w: w.devices.clear(),
            "unreachable",
            "The Apple TV isn't answering. It may be asleep or off the network.",
        ),
        (
            lambda w: w.connect_errors.append(atv_errors.AuthenticationError(f"pair-verify failed {CREDS}")),
            "auth",
            "The Apple TV no longer accepts Jarvis. Pair it again in home setup.",
        ),
        (
            lambda w: w.connect_errors.append(atv_errors.InvalidCredentialsError(f"invalid credentials: {CREDS}")),
            "auth",
            "The Apple TV no longer accepts Jarvis. Pair it again in home setup.",
        ),
        (
            lambda w: w.connect_errors.append(atv_errors.ConnectionFailedError(CREDS)),
            "unreachable",
            "The Apple TV isn't answering. It may be asleep or off the network.",
        ),
        (
            lambda w: w.connect_errors.append(ConnectionRefusedError(CREDS)),
            "unreachable",
            "The Apple TV isn't answering. It may be asleep or off the network.",
        ),
        (
            lambda w: w.failures.setdefault("pause", []).append(atv_errors.NotSupportedError(CREDS)),
            "unsupported",
            "The Apple TV can't do that over its remote connection.",
        ),
        (
            lambda w: w.failures.setdefault("pause", []).append(KeyError(CREDS)),
            "failed",
            "Controlling the Apple TV failed (KeyError).",
        ),
    ],
)
def test_failures_become_codes_and_plain_words_without_secrets(
    rig: Rig,
    world: FakePyatv,
    caplog: pytest.LogCaptureFixture,
    setup: Callable[[FakePyatv], Any],
    code: str,
    text: str,
) -> None:
    caplog.set_level(logging.DEBUG)
    setup(world)
    out = rig.run("pause")
    assert out == Outcome.fail(code, text)  # type: ignore[arg-type]
    assert CREDS not in caplog.text


def test_turn_on_says_how_to_wake_an_apple_tv_that_is_off_the_network(rig: Rig, world: FakePyatv) -> None:
    world.devices.clear()
    out = rig.run("turn_on")
    assert out.code == "unreachable"
    assert "wake it with its remote" in out.text


def test_an_unpaired_apple_tv_needs_setup_and_is_not_contacted(tmp_path: Path, world: FakePyatv) -> None:
    rig = Rig(tmp_path, world, creds=None)
    try:
        out = rig.run("play")
        assert out == Outcome.fail("needs_setup", "The Apple TV isn't paired with Jarvis. Pair it in home setup.")
        assert rig.status().code == "needs_setup"
        assert world.scans == [] and world.connects == 0
        assert rig.driver.describe() == "Apple TV: 1 set up; not paired: Apple TV (pair it in home setup)"
    finally:
        rig.close()


@pytest.mark.parametrize(
    ("state", "text"),
    [
        (PowerState.On, "The Apple TV is on."),
        (PowerState.Off, "The Apple TV is asleep."),
        (PowerState.Unknown, "I can't tell whether the Apple TV is on: it didn't report its power state."),
    ],
)
def test_status_says_the_power_state_in_words(rig: Rig, world: FakePyatv, state: PowerState, text: str) -> None:
    world.power_state = state
    assert rig.status() == Outcome.done(text)


def test_status_waits_a_moment_for_a_fresh_connection_to_report_power(rig: Rig, world: FakePyatv) -> None:
    rig.driver.settle_s = 2.0
    world.power_state = PowerState.Unknown
    world.later.append((0.15, lambda: setattr(world, "power_state", PowerState.Off)))
    assert rig.status() == Outcome.done("The Apple TV is asleep.")


def test_status_on_an_older_connection_first_checks_that_it_still_answers(rig: Rig, world: FakePyatv) -> None:
    assert rig.status() == Outcome.done("The Apple TV is on.")
    assert world.calls == []  # the connect has just proved the link
    age(rig, world.connected[0])
    assert rig.status() == Outcome.done("The Apple TV is on.")
    world.failures["app_list"] = [refused()]  # a refusal is still an answer
    assert rig.status() == Outcome.done("The Apple TV is on.")
    assert world.calls == ["app_list", "app_list"] and world.connects == 1
    # Unplugged without a goodbye: the link is half-open and the pushed "on" is stale.
    world.devices.clear()
    world.failures["app_list"] = [lost()]
    out = rig.status()
    assert out == Outcome.fail("unreachable", "The Apple TV isn't answering. It may be asleep or off the network.")
    wait_until(lambda: world.connected[0].closed)


def test_status_sends_nothing_to_a_sleeping_apple_tv(rig: Rig, world: FakePyatv) -> None:
    world.power_state = PowerState.Off
    assert rig.status() == Outcome.done("The Apple TV is asleep.")
    age(rig, world.connected[0])
    assert rig.status() == Outcome.done("The Apple TV is asleep.")
    assert world.calls == [] and world.connects == 1
    # Put to sleep by Jarvis, but tvOS never pushed "asleep": the stale "on" is not checked with a request.
    world.power_state = PowerState.On
    assert rig.run("turn_off").ok
    assert rig.status() == Outcome.done("The Apple TV is on.")
    assert world.calls == ["turn_off"]
    assert rig.run("turn_on").ok
    assert rig.status().ok
    assert world.calls == ["turn_off", "turn_on", "app_list"]


def test_app_names_are_matched_loosely() -> None:
    def name(wanted: str) -> str | None:
        app, _ = match_app(APPS, wanted)
        return app[0] if app else None

    assert name("netflix") == "Netflix"
    assert name("the Netflix app") == "Netflix"
    assert name("you tube") == "YouTube"
    assert name("YouTube") == "YouTube"
    assert name("kids") == "YouTube Kids"
    assert name("Disney plus") == "Disney+"
    assert name("prime") == "Prime Video"
    assert name("apple tv") == "TV"
    assert name("Netflx") == "Netflix"
    assert name("com.plexapp.plex") == "Plex"
    assert match_app(APPS, "you") == (None, ["YouTube", "YouTube Kids"])
    assert match_app(APPS, "spotify") == (None, [])
    assert match_app(APPS, "  ") == (None, [])


def test_launch_app_opens_the_matching_app_and_says_it_asked(rig: Rig, world: FakePyatv) -> None:
    assert rig.run("launch_app", "netflix") == Outcome.done("Asked the Apple TV to open Netflix.")
    assert rig.run("launch_app", "Disney plus") == Outcome.done("Asked the Apple TV to open Disney+.")
    launched = [c[1] for c in world.connected[0].calls if c[0] == "launch_app"]
    assert launched == ["com.netflix.Netflix", "com.disney.disneyplus"]
    assert world.app_lists == 1  # the list is remembered between launches
    out = rig.run("launch_app", "you")
    assert out == Outcome.fail("ambiguous", '"you" could be YouTube, YouTube Kids. Which one?')
    out = rig.run("launch_app", "Spotify")
    assert out.code == "bad_value" and "list its apps" in out.text
    assert rig.run("launch_app", "https://www.netflix.com/title/1").ok
    assert world.connected[0].calls[-1] == ("launch_app", "https://www.netflix.com/title/1")


KICK = ("Kick", "com.kick.mobile")
KICK_CHANNEL = (
    "Asked the Apple TV to open xqc on Kick. If Kick opens on its home screen instead, "
    "select its search box and I'll type the name."
)


def test_a_link_without_a_scheme_gets_one_so_it_is_not_sent_as_a_bundle_id(rig: Rig, world: FakePyatv) -> None:
    assert rig.run("launch_app", "youtube.com/watch?v=abc") == Outcome.done("Asked the Apple TV to open that link.")
    assert world.calls == ["launch_app"]
    assert world.connected[0].calls[-1] == ("launch_app", "https://youtube.com/watch?v=abc")


def test_a_plex_link_reaches_the_apple_tv_unchanged(rig: Rig, world: FakePyatv) -> None:
    link = "plex://preplay/?metadataKey=%2Flibrary%2Fmetadata%2F14779&server=43d8ddccab40252739e7f4ecc6d134c679888c13"
    assert rig.run("launch_app", link) == Outcome.done("Asked the Apple TV to open that link.")
    assert world.calls == ["launch_app"]  # no app list is needed: the scheme says which app
    assert world.connected[0].calls[-1] == ("launch_app", link)


def test_a_kick_link_opens_the_channel_and_says_it_only_asked(rig: Rig, world: FakePyatv) -> None:
    world.apps.append(KICK)
    assert rig.run("launch_app", "kick.com/xQc") == Outcome.done(KICK_CHANNEL)
    assert world.connected[0].calls[-1] == ("launch_app", "https://kick.com/xqc")
    assert world.calls.count("launch_app") == 1
    out = rig.run("launch_app", "https://www.kick.com/xqc/videos")
    assert out == Outcome.done("Asked the Apple TV to open that Kick link.")
    assert world.connected[0].calls[-1] == ("launch_app", "https://kick.com/xqc/videos")
    assert world.app_lists == 1  # the remembered list has Kick


def test_a_kick_link_is_not_sent_when_kick_is_not_installed(rig: Rig, world: FakePyatv) -> None:
    missing = Outcome.fail(
        "bad_value",
        "Kick isn't installed on the Apple TV. Install it from the App Store on the Apple TV "
        "(it needs tvOS 26 or later), then ask again.",
    )
    assert rig.run("launch_app", "kick.com/xqc") == missing
    assert world.app_lists == 1  # nothing was remembered, so the list just read is fresh
    assert rig.run("launch_app", "kick.com/xqc") == missing
    assert world.app_lists == 2  # the remembered list, then one fresh list before saying no
    assert "launch_app" not in world.calls


def test_kick_installed_since_the_list_was_remembered_is_found(rig: Rig, world: FakePyatv) -> None:
    assert rig.run("launch_app", "netflix").ok  # remembers a list without Kick
    world.apps.append(KICK)
    assert rig.run("launch_app", "kick.com/xqc") == Outcome.done(KICK_CHANNEL)
    assert world.connected[0].calls[-1] == ("launch_app", "https://kick.com/xqc")
    assert world.app_lists == 2


def test_a_kick_link_is_still_sent_when_the_app_list_cannot_be_read(rig: Rig, world: FakePyatv) -> None:
    world.failures["app_list"] = [refused()]
    assert rig.run("launch_app", "kick.com/xqc") == Outcome.done(KICK_CHANNEL)
    assert world.calls == ["app_list", "launch_app"]
    # A lost connection is not "can't read": it reconnects, and the link still goes out once.
    world.apps.append(KICK)
    world.calls.clear()
    world.failures["app_list"] = [lost()]
    assert rig.run("launch_app", "kick.com/xqc") == Outcome.done(KICK_CHANNEL)
    assert world.calls == ["app_list", "app_list", "launch_app"] and world.connects == 2


def test_the_kick_home_page_opens_the_app_by_its_bundle_id(rig: Rig, world: FakePyatv) -> None:
    world.apps.append(KICK)
    assert rig.run("launch_app", "https://kick.com/") == Outcome.done("Asked the Apple TV to open Kick.")
    assert world.connected[0].calls[-1] == ("launch_app", "com.kick.mobile")


def test_a_refused_link_is_reported_and_not_sent_again(rig: Rig, world: FakePyatv) -> None:
    world.apps.append(KICK)
    world.failures["launch_app"] = [refused()]
    out = rig.run("launch_app", "kick.com/xqc")
    assert out == Outcome.fail("failed", 'The Apple TV didn\'t accept that command. It said: "not now".')
    assert world.calls.count("launch_app") == 1


def device_said(text: str) -> atv_errors.ProtocolError:
    return atv_errors.ProtocolError(f"Command failed: {text}")


@pytest.mark.parametrize(
    ("error", "detail"),
    [
        (
            device_said("The operation couldn't be completed.\n\t(FBSOpenApplicationErrorDomain error 4.)"),
            ' It said: "The operation couldn\'t be completed. (FBSOpenApplicationErrorDomain error 4.)".',
        ),
        (device_said('He said "no"'), " It said: \"He said 'no'\"."),
        (device_said("word " * 60), f' It said: "{("word " * 60)[:117]}...".'),
        # Nothing readable, or something shaped like a key: no reason is shown.
        (device_said(""), ""),
        (device_said("   "), ""),
        (device_said("token " + "A1b2C3d4" * 5), ""),
        # Every other pyatv message may carry a credential, so only the kind of error underneath is named.
        (atv_errors.ProtocolError(f"Command _launchApp failed {CREDS}"), ""),
        (atv_errors.ProtocolError(f"Received unexpected type {CREDS}"), ""),
    ],
)
def test_a_refusal_says_only_what_the_apple_tv_itself_said(
    rig: Rig, world: FakePyatv, caplog: pytest.LogCaptureFixture, error: Exception, detail: str
) -> None:
    caplog.set_level(logging.DEBUG)
    world.failures["launch_app"] = [error]
    out = rig.run("launch_app", "Netflix")
    assert out == Outcome.fail("failed", f"The Apple TV didn't accept that command.{detail}")
    assert CREDS not in out.text and CREDS not in caplog.text
    assert ("It said" in caplog.text) == ("It said" in detail)


def test_a_refusal_names_the_kind_of_error_underneath_when_the_apple_tv_gave_no_reason(
    rig: Rig, world: FakePyatv
) -> None:
    error = atv_errors.ProtocolError(f"Command _launchApp failed {CREDS}")
    error.__cause__ = KeyError(CREDS)
    world.failures["launch_app"] = [error]
    out = rig.run("launch_app", "Netflix")
    assert out == Outcome.fail(
        "failed", "The Apple TV didn't accept that command. (The error underneath was a KeyError.)"
    )
    assert CREDS not in out.text


def test_list_apps_names_them_in_order_and_asks_afresh(rig: Rig, world: FakePyatv) -> None:
    out = rig.run("list_apps")
    assert out.ok
    assert out.text == ("Apps on the Apple TV: Disney+, Music, Netflix, Plex, Prime Video, TV, YouTube, YouTube Kids.")
    world.apps.append(("Apple Arcade", "com.apple.Arcade"))
    assert "Apple Arcade" in rig.run("list_apps").text
    assert world.app_lists == 2


def test_set_volume_works_only_when_the_apple_tv_controls_the_tv_volume(rig: Rig, world: FakePyatv) -> None:
    world.features[FeatureName.SetVolume] = FeatureState.Unavailable
    out = rig.run("set_volume", 30)
    assert out.code == "unsupported" and "HDMI-CEC" in out.text
    assert "set_volume" not in world.calls

    world.features[FeatureName.SetVolume] = FeatureState.Available
    world.failures["set_volume"] = [TimeoutError()]
    out = rig.run("set_volume", 30)
    assert out.code == "timeout" and "HDMI-CEC" in out.text
    assert world.connects == 1  # a missing volume echo is not a lost connection

    assert rig.run("set_volume", 30) == Outcome.done("Set the volume on the Apple TV to 30%.")
    assert world.connected[0].calls[-1] == ("set_volume", 30.0)


def test_volume_flags_that_arrive_just_after_connecting_are_waited_for(rig: Rig, world: FakePyatv) -> None:
    rig.driver.settle_s = 2.0
    world.features[FeatureName.SetVolume] = FeatureState.Unavailable
    world.later.append((0.15, lambda: world.features.update({FeatureName.SetVolume: FeatureState.Available})))
    assert rig.run("set_volume", 55).ok


def test_volume_steps(rig: Rig, world: FakePyatv) -> None:
    world.features[FeatureName.Volume] = FeatureState.Unavailable
    assert rig.run("volume_up").code == "unsupported"
    assert "volume_up" not in world.calls
    world.features[FeatureName.Volume] = FeatureState.Available
    assert rig.run("volume_down") == Outcome.done("Turned the volume down on the Apple TV.")
    world.failures["volume_up"] = [TimeoutError()]
    assert rig.run("volume_up") == Outcome.done(
        "Turned the volume up on the Apple TV; it didn't confirm the new level."
    )


def test_type_text_needs_a_selected_text_field(rig: Rig, world: FakePyatv) -> None:
    world.focused = False
    out = rig.run("type_text", "breaking bad")
    assert out.code == "failed" and "No text field is selected" in out.text
    assert "text_set" not in world.calls
    world.focused = True
    assert rig.run("type_text", "breaking bad") == Outcome.done('Typed "breaking bad" on the Apple TV.')
    assert world.connected[0].calls[-1] == ("text_set", "breaking bad")


def test_navigation_power_and_media_commands_reach_the_remote(rig: Rig, world: FakePyatv) -> None:
    expected = {
        "turn_on": "The Apple TV is waking up.",
        "up": "Pressed up on the Apple TV.",
        "left": "Pressed left on the Apple TV.",
        "menu": "Pressed Menu on the Apple TV.",
        "back": "Pressed Back on the Apple TV.",
        "home": "Pressed Home on the Apple TV.",
        "play_pause": "Pressed play/pause on the Apple TV.",
        "previous": "Went back to the previous item on the Apple TV.",
    }
    for command, text in expected.items():
        assert rig.run(command) == Outcome.done(text)
    assert world.calls == ["turn_on", "up", "left", "menu", "menu", "home", "play_pause", "previous"]


def test_close_closes_connections_without_hanging(rig: Rig, world: FakePyatv) -> None:
    assert rig.run("play").ok
    atv = world.connected[0]
    world.close_delay = 30.0  # a half-open link that never says goodbye
    rig.driver.close_wait_s = 0.3
    started = time.monotonic()
    rig.driver.close()
    assert time.monotonic() - started < 2.0
    assert atv.closed


def test_a_connect_landing_after_close_is_closed_too(tmp_path: Path, world: FakePyatv) -> None:
    rig = Rig(tmp_path, world, call_timeout=1.2)
    try:
        world.connect_delay = 1.0
        assert rig.run("play").code == "timeout"
        rig.driver.close()
        wait_until(lambda: world.connect_done == 1)
        wait_until(lambda: world.connected[0].closed)
        assert world.connect_cancelled == 0
    finally:
        rig.close()


def test_a_close_while_a_command_waits_for_its_connect_starts_no_more(tmp_path: Path, world: FakePyatv) -> None:
    rig = Rig(tmp_path, world, call_timeout=4.0)
    try:
        world.connect_delay = 0.5
        result: dict[str, Outcome] = {}
        worker = threading.Thread(target=lambda: result.update(out=rig.run("play")))
        worker.start()
        wait_until(lambda: world.connects == 1)
        rig.driver.close()  # a reload, or the helper stopping
        worker.join(5.0)
        assert result["out"] == Outcome.fail(
            "failed", "Home control was restarting, so the Apple TV didn't get that. Ask again."
        )
        wait_until(lambda: world.connected[0].closed)
        assert world.connects == 1 and world.calls == []
    finally:
        rig.close()


def test_an_apple_tv_dropping_each_new_session_is_not_reconnected_over_and_over(rig: Rig, world: FakePyatv) -> None:
    world.drop_new = True
    started = time.monotonic()
    out = rig.run("play")
    assert out == Outcome.fail("unreachable", "The Apple TV stopped answering. Try again in a moment.")
    assert world.connects <= 2  # its own connect, and at most one for the single retry
    assert time.monotonic() - started < 2.0
    wait_until(lambda: all(atv.closed for atv in world.connected))


def test_close_without_any_use_starts_nothing(tmp_path: Path, world: FakePyatv) -> None:
    ctx = DriverContext(make_store(tmp_path), tmp_path)
    AppleTvDriver(ctx, api=world).close()
    assert ctx._runner is None


def test_through_the_service(tmp_path: Path, world: FakePyatv) -> None:
    store = make_store(tmp_path)
    save_device(store)
    service = HomeService(tmp_path, store=store, drivers={"appletv": lambda ctx: AppleTvDriver(ctx, api=world)})
    try:
        reply = service.handle({"action": "do", "device": "apple tv", "command": "open", "value": "netflix"})
        assert reply["code"] == "unsupported"  # "open" is for covers; the service says what it can do
        reply = service.handle({"action": "do", "device": "apple tv", "command": "launch", "value": "netflix"})
        assert reply["result"] == "done" and reply["text"] == "Asked the Apple TV to open Netflix."
        reply = service.handle({"action": "status", "device": "living room apple tv"})
        assert reply["text"] == "The Apple TV is on."
        listed = service.handle({"action": "list"})["text"]
        assert "set_volume <0-100>" in listed and "launch_app <an app name or a link>" in listed
        assert "Apple TV: 1 set up" in service.handle({"action": "info"})["text"]
        assert world.connects == 1
    finally:
        service.close()
    assert world.connected[0].closed


def test_the_real_pyatv_calls_use_companion_and_announce_one_client(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def scan(loop: Any, **kwargs: Any) -> list[Any]:
        seen["scan"] = kwargs
        return []

    async def connect(conf: AppleTV, loop: Any, **kwargs: Any) -> str:
        seen["connect"] = await kwargs["storage"].get_settings(conf)
        return "atv"

    async def pair(conf: AppleTV, protocol: Protocol, loop: Any, **kwargs: Any) -> str:
        seen["pair"] = (protocol, kwargs["name"], await kwargs["storage"].get_settings(conf))
        return "handler"

    monkeypatch.setattr(appletv.pyatv, "scan", scan)
    monkeypatch.setattr(appletv.pyatv, "connect", connect)
    monkeypatch.setattr(appletv.pyatv, "pair", pair)
    api = appletv.PyatvApi()
    conf = make_conf(credentials=CREDS)

    async def go() -> None:
        assert await api.scan(hosts=[OLD_ADDRESS], identifier={COMPANION_ID}, timeout=3) == []
        assert await api.connect(conf, rp_id=RP_ID) == "atv"
        assert await api.pair(make_conf(), rp_id=None) == "handler"

    asyncio.run(go())
    assert seen["scan"] == {
        "timeout": 3,
        "identifier": {COMPANION_ID},
        "protocol": {Protocol.Companion, Protocol.AirPlay},
        "hosts": [OLD_ADDRESS],
    }
    settings = seen["connect"]
    assert (settings.info.name, settings.info.rp_id) == ("Jarvis", RP_ID)
    assert settings.protocols.companion.credentials == CREDS
    protocol, name, pair_settings = seen["pair"]
    assert (protocol, name, pair_settings.info.name) == (Protocol.Companion, "Jarvis", "Jarvis")
    assert pair_settings.protocols.companion.credentials is None


# --------------------------------------------------------------------------- the setup wizard


def wizard_ctx(tmp_path: Path) -> DriverContext:
    return DriverContext(make_store(tmp_path), tmp_path, 4.0)


def run_wizard(ctx: DriverContext, world: FakePyatv, answers: list[Any]) -> ScriptedPrompter:
    ui = ScriptedPrompter(answers)
    try:
        appletv.wizard(ui, ctx, api=world)
    finally:
        ctx.close()
    return ui


def test_wizard_finds_pairs_names_and_saves_an_apple_tv(tmp_path: Path, world: FakePyatv) -> None:
    ctx = wizard_ctx(tmp_path)
    ui = run_wizard(ctx, world, [0, "", "Living room", "1234", True])
    [device] = ctx.store.load().devices
    assert (device.driver, device.kind, device.name, device.room) == (
        "appletv",
        "media_player",
        "Living Room",
        "Living room",
    )
    assert device.settings["address"] == OLD_ADDRESS
    assert device.settings["identifiers"] == [AIRPLAY_ID, COMPANION_ID]
    assert device.settings["model"] == "Apple TV 4K (gen 3)"
    assert re.fullmatch(r"[0-9a-f]{12}", device.settings["rp_id"])
    assert ctx.store.secret(device.secret_key) == {"companion": CREDS}
    # The same client identity pairs and then connects.
    assert world.pair_rp_ids == [device.settings["rp_id"]]
    assert world.connect_log[-1]["rp_id"] == device.settings["rp_id"]
    assert [p.closed for p in world.pairings] == [True]
    assert "Allow Access" in ui.text and "DHCP reservation" in ui.text
    assert "The Living Room is on." in ui.text
    assert CREDS not in ui.text
    assert CREDS not in ctx.store.devices_path.read_text(encoding="utf-8")


def test_wizard_lets_a_wrong_pin_be_retyped(tmp_path: Path, world: FakePyatv) -> None:
    ctx = wizard_ctx(tmp_path)
    ui = run_wizard(ctx, world, [0, "Apple TV", "", "0000", "12", "1234", False])
    assert "That code didn't work. The TV will show a new one in a few seconds." in ui.text
    assert "The code is the 4 digits on the TV screen." in ui.said
    assert len(world.pairings) == 2 and all(p.closed for p in world.pairings)
    [device] = ctx.store.load().devices
    assert device.name == "Apple TV" and device.room is None
    assert ctx.store.secret(device.secret_key) == {"companion": CREDS}


def test_wizard_waits_out_the_back_off_after_a_wrong_pin(
    tmp_path: Path, world: FakePyatv, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.backoff_s = 0.2
    monkeypatch.setattr(appletv, "PIN_RETRY_WAIT_S", 0.4)
    ctx = wizard_ctx(tmp_path)
    ui = run_wizard(ctx, world, [0, "", "", "0000", "1234", False])
    assert "refused" not in ui.text and "holding off" not in ui.text
    assert len(world.pairings) == 2 and all(p.closed for p in world.pairings)
    [device] = ctx.store.load().devices
    assert ctx.store.secret(device.secret_key) == {"companion": CREDS}


def test_wizard_says_to_wait_when_the_back_off_outlasts_the_pause(tmp_path: Path, world: FakePyatv) -> None:
    world.backoff_s = 60.0
    ctx = wizard_ctx(tmp_path)
    ui = run_wizard(ctx, world, [0, "", "", "0000"])
    assert "The Apple TV is holding off pairing after the wrong code. Wait a minute" in ui.text
    assert "refused to start pairing" not in ui.text
    assert len(world.pairings) == 2 and all(p.closed for p in world.pairings)
    assert ctx.store.load().devices == [] and ctx.store.secret_keys() == []


def test_wizard_stops_after_three_wrong_pins_and_saves_nothing(tmp_path: Path, world: FakePyatv) -> None:
    ctx = wizard_ctx(tmp_path)
    ui = run_wizard(ctx, world, [0, "", "", "1111", "2222", "3333"])
    assert len(world.pairings) == 3 and all(p.closed for p in world.pairings)
    assert "until the Apple TV restarts" in ui.text and "Settings > System > Restart" in ui.text
    assert ctx.store.load().devices == []
    assert ctx.store.secret_keys() == []


class InterruptingPrompter(ScriptedPrompter):
    """Answers from a script, but the PIN question raises (Ctrl+C, or the window closing)."""

    def __init__(self, answers: list[Any], error: BaseException) -> None:
        super().__init__(answers)
        self.error = error

    def ask(self, question: str, default: str | None = None) -> str:
        if "code" in question:
            raise self.error
        return super().ask(question, default)


@pytest.mark.parametrize("error", [KeyboardInterrupt(), EOFError()])
def test_wizard_cancelled_at_the_pin_saves_nothing_and_closes_the_pairing(
    tmp_path: Path, world: FakePyatv, error: BaseException
) -> None:
    ctx = wizard_ctx(tmp_path)
    ui = InterruptingPrompter([0, "", "Living room"], error)
    with pytest.raises(type(error)):
        appletv.wizard(ui, ctx, api=world)
    ctx.close()
    assert [p.closed for p in world.pairings] == [True]
    assert ctx.store.load().devices == []
    assert ctx.store.secret_keys() == []


def test_wizard_stops_when_no_pin_is_typed(tmp_path: Path, world: FakePyatv) -> None:
    ctx = wizard_ctx(tmp_path)
    ui = run_wizard(ctx, world, [0, "", "", ""])
    assert "Stopped. Nothing was saved." in ui.said
    assert ctx.store.load().devices == [] and world.pairings[0].closed


def test_wizard_closes_a_pin_dialog_left_waiting(
    tmp_path: Path, world: FakePyatv, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(appletv, "PIN_WAIT_S", 0.05)

    class SlowPrompter(ScriptedPrompter):
        def ask(self, question: str, default: str | None = None) -> str:
            if "code" in question:
                time.sleep(0.4)
            return super().ask(question, default)

    ctx = wizard_ctx(tmp_path)
    ui = SlowPrompter([0, "", "", "1234"])
    try:
        appletv.wizard(ui, ctx, api=world)
    finally:
        ctx.close()
    assert "The code expired. Add the Apple TV again when you are ready." in ui.said
    assert world.pairings[0].closed
    assert ctx.store.load().devices == []


def test_wizard_takes_a_typed_address(tmp_path: Path, world: FakePyatv) -> None:
    world.multicast_hidden = True
    ctx = wizard_ctx(tmp_path)
    ui = run_wizard(ctx, world, ["address", "not an address", OLD_ADDRESS, "", "", "1234", False])
    assert "No Apple TV answered. Check it is on, then type its address." in ui.said
    assert "That isn't an address like 192.168.1.20." in ui.said
    assert ("unicast", [OLD_ADDRESS], set()) in world.scans
    [device] = ctx.store.load().devices
    assert device.settings["address"] == OLD_ADDRESS


def test_wizard_says_when_nothing_answers_at_a_typed_address(tmp_path: Path, world: FakePyatv) -> None:
    world.devices.clear()
    ctx = wizard_ctx(tmp_path)
    ui = run_wizard(ctx, world, ["address", NEW_ADDRESS, ""])
    assert f"No Apple TV answered at {NEW_ADDRESS}." in ui.text
    assert world.pairings == [] and ctx.store.load().devices == []


def test_wizard_pairs_an_existing_apple_tv_again_in_place(tmp_path: Path, world: FakePyatv) -> None:
    ctx = wizard_ctx(tmp_path)
    save_device(ctx.store, creds="old-fake-credential")
    # The device answers with credentials still on its config: pairing must start without them.
    world.devices = {NEW_ADDRESS: make_conf(NEW_ADDRESS, credentials="stale-fake-credential")}
    ui = run_wizard(ctx, world, [0, True, "1234", False])
    assert "already set up as Apple TV" in ui.text
    assert world.pairings[0].creds_at_start is None
    assert world.pair_rp_ids == [RP_ID]  # the same client identity as before
    [device] = ctx.store.load().devices
    assert (device.id, device.name, device.room) == (DEVICE_ID, "Apple TV", "Living room")
    assert device.settings["address"] == NEW_ADDRESS and device.settings["rp_id"] == RP_ID
    assert ctx.store.secret(device.secret_key) == {"companion": CREDS}


def test_wizard_explains_when_the_apple_tv_does_not_allow_pairing(tmp_path: Path, world: FakePyatv) -> None:
    world.devices = {OLD_ADDRESS: make_conf(pairing=PairingRequirement.Disabled)}
    ctx = wizard_ctx(tmp_path)
    ui = run_wizard(ctx, world, [0])
    assert "Living Room doesn't allow pairing yet." in ui.text
    assert "Anyone on the Same Network" in ui.text
    assert world.pairings == [] and ctx.store.load().devices == []


def test_wizard_lists_only_apple_tvs(tmp_path: Path, world: FakePyatv) -> None:
    world.devices["192.0.2.30"] = make_conf(
        "192.0.2.30", name="Kitchen", model=DeviceModel.HomePodMini, pairing=PairingRequirement.Unsupported
    )

    class Recording(ScriptedPrompter):
        def __init__(self, answers: list[Any]) -> None:
            super().__init__(answers)
            self.options: list[str] = []

        def choose(self, question: str, options: list[str]) -> int | None:
            self.options = list(options)
            return super().choose(question, options)

    ctx = wizard_ctx(tmp_path)
    ui = Recording([None])
    try:
        appletv.wizard(ui, ctx, api=world)
    finally:
        ctx.close()
    assert ui.options == ["Living Room (Apple TV 4K (gen 3), 192.0.2.10)", "Type the Apple TV's address"]


def test_wizard_says_why_pairing_did_not_start(tmp_path: Path, world: FakePyatv) -> None:
    world.begin_error = atv_errors.PairingError(CREDS)
    ctx = wizard_ctx(tmp_path)
    ui = run_wizard(ctx, world, [0, "", ""])
    assert "The Apple TV refused to start pairing." in ui.text
    assert CREDS not in ui.text
    assert world.pairings[0].closed and ctx.store.load().devices == []
