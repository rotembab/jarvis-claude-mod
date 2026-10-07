"""Home control's core: the model, the store, the async runner, the service, the command line and the wizard menu."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from jarvis_voice import protocol
from jarvis_voice.audio.fake import FakeCapture, FakePlayback
from jarvis_voice.daemon import Daemon, DaemonConfig
from jarvis_voice.home import base as home_base
from jarvis_voice.home import wizard as home_wizard
from jarvis_voice.home.commandline import CONFIRM_NEEDS_HELPER, call_once, run_console
from jarvis_voice.home.model import (
    CommandSpec,
    DeviceRecord,
    HomeConfig,
    Outcome,
    canonical_command,
    normalize,
    parse_value,
    slugify,
    unique_id,
)
from jarvis_voice.home.net import HttpError, broadcast_targets, magic_packet, normalize_mac, request
from jarvis_voice.home.runner import AsyncRunner
from jarvis_voice.home.service import HomeService
from jarvis_voice.home.store import HomeStore, PlainCodec, StoreError
from jarvis_voice.logs import secret_filter
from jarvis_voice.ptt.fake import FakePushToTalk
from jarvis_voice.stt.fake import FakeTranscriber

from conftest import RecordingSink
from fakes import FakeSynth
from home_fakes import FakeDriver, ScriptedPrompter, make_service, make_store

TV = DeviceRecord("fake-sony-tv", "fake", "Sony TV", "tv", "Living room", aliases=["telly"])
STREAMER = DeviceRecord("fake-apple-tv", "fake", "Apple TV", "media_player", "Living room")
DOOR = DeviceRecord("fake-front-door", "fake", "Front door", "lock", "Hall")
HEATER = DeviceRecord("fake-heater", "fake", "Heater plug", "plug", "Bedroom", confirm="screen")
LAMP_1 = DeviceRecord("fake-lamp-1", "fake", "Desk lamp", "light", "Office")
LAMP_2 = DeviceRecord("fake-lamp-2", "fake", "Floor lamp", "light", "Office")
ALL = [TV, STREAMER, DOOR, HEATER, LAMP_1, LAMP_2]


# --------------------------------------------------------------------------- model


@pytest.mark.parametrize(
    ("value", "expected"),
    [(50, 50), ("50", 50), ("50%", 50), (" 7.6 ", 8), ("half", 50), ("max", 100), (0, 0), (100, 100)],
)
def test_percent_values_are_coerced(value: Any, expected: int) -> None:
    assert parse_value(CommandSpec("set_volume", "percent"), value) == (expected, None)


@pytest.mark.parametrize("value", [101, -1, "loud", None, "", True])
def test_bad_percent_values_are_refused_in_words(value: Any) -> None:
    parsed, error = parse_value(CommandSpec("set_volume", "percent"), value)
    assert parsed is None
    assert error is not None and "set_volume" in error


def test_number_choice_and_text_values() -> None:
    temp = CommandSpec("set_temperature", "number", low=16, high=30)
    assert parse_value(temp, "22°C") == (22, None)
    assert parse_value(temp, "21.5") == (21.5, None)
    assert parse_value(temp, 31)[1] == "set_temperature takes 16 to 30, not 31."
    inputs = CommandSpec("set_input", "choice", choices=("HDMI 1", "HDMI 2", "TV"))
    assert parse_value(inputs, "hdmi 2") == ("HDMI 2", None)
    assert parse_value(inputs, "tv") == ("TV", None)
    assert parse_value(inputs, "hdmi")[1] == "set_input takes one of: HDMI 1, HDMI 2, TV."
    text = CommandSpec("launch_app", "text")
    assert parse_value(text, "  Netflix ") == ("Netflix", None)
    assert parse_value(text, "x" * 501)[0] is None
    assert parse_value(CommandSpec("turn_on"), "ignored") == (None, None)


def test_command_names_and_usage() -> None:
    assert canonical_command("Power Off") == "turn_off"
    assert canonical_command("on") == "turn_on"
    assert canonical_command("set-volume") == "set_volume"
    assert canonical_command("colour") == "set_color"
    assert CommandSpec("set_volume", "percent").usage() == "set_volume <0-100>"
    assert CommandSpec("set_temperature", "number", low=16, high=30).usage() == "set_temperature <16-30>"
    assert CommandSpec("launch_app", "text", hint="an app name").usage() == "launch_app <an app name>"
    assert CommandSpec("turn_on").usage() == "turn_on"


def test_names_ids_and_slugs() -> None:
    assert normalize("The Living-Room TV!") == "living room tv"
    assert normalize("Café lamp") == "cafe lamp"
    assert slugify("My Sony TV") == "sony-tv"
    assert unique_id({"bravia-sony-tv"}, "bravia", "Sony TV") == "bravia-sony-tv-2"
    assert unique_id(set(), "tuya", "!!!") == "tuya-device"


def test_config_round_trip_drops_bad_and_duplicate_entries() -> None:
    config = HomeConfig.from_json(
        {
            "version": 1,
            "devices": [TV.to_json(), {"id": "", "driver": "x", "name": "y"}, "junk", TV.to_json(), DOOR.to_json()],
            "hubs": {"homeassistant": {"url": "http://ha.local:8123"}, "bad": 3},
        }
    )
    assert [d.id for d in config.devices] == [TV.id, DOOR.id]
    assert config.hubs == {"homeassistant": {"url": "http://ha.local:8123"}}
    assert HomeConfig.from_json(config.to_json()).to_json() == config.to_json()
    assert DeviceRecord.from_json({"id": "a", "driver": "b", "name": "c", "confirm": "maybe"}).confirm is None


# --------------------------------------------------------------------------- store


def test_store_saves_devices_and_secrets_apart(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    assert store.load().devices == []
    store.update(lambda c: c.upsert(TV))
    store.set_secret(TV.secret_key, {"psk": "correct-horse-battery"})
    assert [d.name for d in store.load().devices] == ["Sony TV"]
    assert store.secret(TV.secret_key) == {"psk": "correct-horse-battery"}
    assert "correct-horse" not in store.devices_path.read_text(encoding="utf-8")
    assert store.secrets_path.parent == tmp_path / "home"
    if os.name == "posix":
        assert store.secrets_path.stat().st_mode & 0o777 == 0o600
    store.set_secret(TV.secret_key, None)
    assert store.secret(TV.secret_key) is None
    assert store.secret_keys() == []


def test_a_second_store_sees_changes_like_another_process(tmp_path: Path) -> None:
    wizard_side, helper_side = make_store(tmp_path), make_store(tmp_path)
    assert helper_side.load().devices == []
    wizard_side.update(lambda c: c.upsert(TV))
    wizard_side.set_secret("tuya_cloud", {"token": "abcdef123456"})
    assert [d.id for d in helper_side.load().devices] == [TV.id]
    assert helper_side.secret("tuya_cloud") == {"token": "abcdef123456"}
    helper_side.update(lambda c: c.upsert(DOOR))
    assert {d.id for d in wizard_side.load().devices} == {TV.id, DOOR.id}


def test_loaded_secrets_are_masked_in_logs(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    make_store(tmp_path).set_secret("bravia:x", {"psk": "s3cret-psk-value"})
    make_store(tmp_path).secret("bravia:x")  # a fresh process loading it registers it too
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "sent %s", ("s3cret-psk-value",), None)
    secret_filter.filter(record)
    assert "s3cret-psk-value" not in record.getMessage()


def test_load_returns_a_copy(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.update(lambda c: c.upsert(TV))
    store.load().devices[0].name = "changed"
    assert store.load().devices[0].name == "Sony TV"


def test_concurrent_updates_keep_every_device(tmp_path: Path) -> None:
    stores = [make_store(tmp_path) for _ in range(4)]

    def add(i: int) -> None:
        stores[i % 4].update(lambda c: c.upsert(DeviceRecord(f"fake-{i}", "fake", f"Device {i}")))

    threads = [threading.Thread(target=add, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(make_store(tmp_path).load().devices) == 16


def test_a_broken_devices_file_is_reported_not_fatal(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path)
    service.store.devices_path.parent.mkdir(parents=True, exist_ok=True)
    service.store.devices_path.write_text("{ not json", encoding="utf-8")
    reply = service.handle({"action": "list"})
    assert reply["result"] == "failed" and reply["code"] == "needs_setup"
    assert "devices.json" in reply["text"]


def test_unreadable_credentials_never_show_their_contents(tmp_path: Path) -> None:
    class Garbled(PlainCodec):
        def decrypt(self, sealed: bytes) -> bytes:
            raise StoreError("cannot decrypt")

    make_store(tmp_path).set_secret("k", {"token": "visible-token-123"})
    with pytest.raises(StoreError) as caught:
        HomeStore(tmp_path, codec=Garbled()).secret("k")
    assert "visible-token" not in str(caught.value)


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows only")
def test_dpapi_round_trip_and_file_is_not_plain(tmp_path: Path) -> None:
    store = HomeStore(tmp_path)
    assert store.codec.name == "dpapi"
    store.set_secret("appletv:x", {"companion": "deadbeef" * 8})
    assert b"deadbeef" not in store.secrets_path.read_bytes()
    assert HomeStore(tmp_path).secret("appletv:x") == {"companion": "deadbeef" * 8}


# --------------------------------------------------------------------------- net helpers


def test_wake_on_lan_packet_and_targets() -> None:
    assert normalize_mac("AA-BB-CC-DD-EE-FF") == "aa:bb:cc:dd:ee:ff"
    assert normalize_mac("aabbccddeeff") == "aa:bb:cc:dd:ee:ff"
    assert normalize_mac("aa:bb:cc-dd:ee:ff") is None
    packet = magic_packet("aa:bb:cc:dd:ee:ff")
    assert packet[:6] == b"\xff" * 6 and len(packet) == 102
    assert broadcast_targets("192.168.1.20") == ["255.255.255.255", "192.168.1.255"]
    assert broadcast_targets("tv.local") == ["255.255.255.255"]
    assert broadcast_targets(None) == ["255.255.255.255"]


def test_a_header_that_cannot_be_sent_is_not_quoted() -> None:
    # http.client quotes a bad header value in its error; a key must never reach a message that way.
    with pytest.raises(HttpError) as caught:
        request("GET", "http://127.0.0.1:9/", headers={"X-Auth-PSK": "abc\r\nnot-a-real-key"}, timeout=1)
    assert caught.value.kind == "bad_url" and "not-a-real-key" not in str(caught.value)


# --------------------------------------------------------------------------- async runner


def test_runner_runs_times_out_and_stops() -> None:
    runner = AsyncRunner()

    async def answer() -> int:
        await asyncio.sleep(0.01)
        return 42

    async def boom() -> None:
        raise ValueError("bad")

    cancelled = threading.Event()

    async def hang() -> None:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    assert runner.call(answer(), 2) == 42
    with pytest.raises(ValueError, match="bad"):
        runner.call(boom(), 2)
    with pytest.raises(TimeoutError):
        runner.call(hang(), 0.1)
    assert cancelled.wait(2)

    async def nested() -> None:
        runner.call(answer(), 1)

    with pytest.raises(RuntimeError, match="deadlock"):
        runner.call(nested(), 2)
    runner.stop()
    with pytest.raises(RuntimeError):
        runner.call(answer(), 1)


# --------------------------------------------------------------------------- service


def test_list_with_no_devices_points_to_setup(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path)
    reply = service.handle({"action": "list"})
    assert reply == {
        "ok": True,
        "result": "done",
        "code": "ok",
        "text": "No home devices are set up yet. Run /jarvis home setup, or ask me to open home setup, to add devices.",
        "count": 0,
    }


def test_list_shows_rooms_commands_and_confirmations(tmp_path: Path, reference_validator: Any) -> None:
    service, _ = make_service(tmp_path, ALL)
    reply = service.handle({"action": "list"})
    reference_validator(reply, "CommandResponse")
    text = reply["text"]
    assert text.startswith("6 home devices:")
    assert "- Sony TV (tv, Living room) id=fake-sony-tv: turn_on, turn_off, set_volume <0-100>" in text
    assert "set_input <HDMI 1|HDMI 2|TV>" in text
    assert "unlock*" in text and "self_destruct" not in text
    assert "- Heater plug (plug, Bedroom) id=fake-heater: turn_on*, turn_off*" in text
    assert text.endswith("* asks the user to confirm on screen first.")
    assert text.index("Front door") < text.index("Sony TV")  # Hall before Living room
    only = service.handle({"action": "list", "query": "office"})
    assert only["count"] == 2 and "Desk lamp" in only["text"] and "Sony TV" not in only["text"]
    assert service.handle({"action": "list", "query": "garage"})["count"] == 0


@pytest.mark.parametrize(
    "wanted",
    ["fake-sony-tv", "Sony TV", "the sony tv", "telly", "living room sony tv", "TV", "television", "sony", "Sonny TV"],
)
def test_devices_are_found_by_id_name_alias_room_kind_or_close_spelling(tmp_path: Path, wanted: str) -> None:
    service, drivers = make_service(tmp_path, ALL)
    reply = service.handle({"action": "do", "device": wanted, "command": "turn_on"})
    assert reply["result"] == "done", reply
    assert reply["device"] == {"id": TV.id, "name": "Sony TV"}
    assert drivers["fake"].calls == [(TV.id, "turn_on", None)]


def test_ambiguous_and_unknown_devices(tmp_path: Path) -> None:
    service, drivers = make_service(tmp_path, ALL)
    reply = service.handle({"action": "do", "device": "lamp", "command": "turn_on"})
    assert reply["code"] == "ambiguous"
    assert "Desk lamp (Office)" in reply["text"] and "Floor lamp (Office)" in reply["text"]
    reply = service.handle({"action": "do", "device": "lights", "command": "turn_on"})
    assert reply["code"] == "ambiguous"
    reply = service.handle({"action": "do", "device": "garage door", "command": "open"})
    assert reply["code"] == "not_found" and "Sony TV" in reply["text"]
    assert drivers.get("fake") is None or drivers["fake"].calls == []


def test_commands_values_and_synonyms(tmp_path: Path) -> None:
    service, drivers = make_service(tmp_path, ALL)
    assert service.handle({"action": "do", "device": "Sony TV", "command": "power off"})["text"] == "Sony TV: turn_off"
    assert service.handle({"action": "do", "device": "Sony TV", "command": "volume", "value": "30%"})["text"] == (
        "Sony TV: set_volume 30"
    )
    reply = service.handle({"action": "do", "device": "Sony TV", "command": "set_volume", "value": "loud"})
    assert reply["code"] == "bad_value"
    reply = service.handle({"action": "do", "device": "Sony TV", "command": "fly"})
    assert reply["code"] == "unsupported" and "It can: turn_on, turn_off" in reply["text"]
    assert service.handle({"action": "do", "device": "Sony TV"})["code"] == "bad_value"
    assert [c[1] for c in drivers["fake"].calls] == ["turn_off", "set_volume"]


def test_screen_tier_needs_confirmation_and_never_is_refused(tmp_path: Path, reference_validator: Any) -> None:
    service, drivers = make_service(tmp_path, ALL)
    reply = service.handle({"action": "do", "device": "front door", "command": "unlock"})
    reference_validator(reply, "CommandResponse")
    assert reply == {
        "ok": True,
        "result": "confirm",
        "code": "confirm",
        "text": "This needs the user's OK on screen: unlock the Front door.",
        "tier": "screen",
        "prompt": "Let Jarvis unlock the Front door?",
        "device": {"id": DOOR.id, "name": "Front door"},
    }
    assert drivers["fake"].calls == []
    assert service.handle({"action": "do", "device": "front door", "command": "unlock", "confirmed": True})[
        "result"
    ] == ("done")
    assert service.handle({"action": "do", "device": "front door", "command": "lock"})["result"] == "done"
    reply = service.handle({"action": "do", "device": "front door", "command": "self_destruct", "confirmed": True})
    assert reply["code"] == "refused"
    # A device-wide floor set in the wizard applies to every command.
    assert service.handle({"action": "do", "device": "heater", "command": "turn_on"})["result"] == "confirm"
    assert [c[1] for c in drivers["fake"].calls] == ["unlock", "lock"]


def test_status_goes_to_the_driver(tmp_path: Path) -> None:
    service, drivers = make_service(tmp_path, ALL)
    assert service.handle({"action": "status", "device": "apple tv"})["text"] == "The Apple TV is on."
    assert service.handle({"action": "do", "device": "apple tv", "command": "status"})["text"] == "The Apple TV is on."
    assert drivers["fake"].calls == [(STREAMER.id, "status", None), (STREAMER.id, "status", None)]


def test_a_slow_device_times_out_and_a_busy_one_says_so(tmp_path: Path) -> None:
    service, drivers = make_service(tmp_path, ALL, call_timeout=0.4)
    service.handle({"action": "list"})  # builds the driver
    drivers["fake"].delay = 1.0
    started = time.monotonic()
    reply = service.handle({"action": "do", "device": "Sony TV", "command": "turn_on"})
    assert reply["code"] == "timeout" and "may still act" in reply["text"]
    assert time.monotonic() - started < 0.9
    # The first call still holds the TV: a second one waits, then reports busy.
    reply = service.handle({"action": "do", "device": "Sony TV", "command": "turn_off"})
    assert reply["code"] in ("busy", "timeout")
    # Another device is not held up.
    drivers["fake"].delay = 0
    assert service.handle({"action": "do", "device": "Apple TV", "command": "turn_on"})["result"] == "done"
    service.close()


def test_driver_failures_become_spoken_failures(tmp_path: Path) -> None:
    service, drivers = make_service(tmp_path, ALL)
    service.handle({"action": "list"})
    drivers["fake"].outcome = Outcome.fail("unreachable", "The Sony TV is off or unreachable.")
    reply = service.handle({"action": "do", "device": "Sony TV", "command": "turn_on"})
    assert (reply["result"], reply["code"], reply["text"]) == (
        "failed",
        "unreachable",
        "The Sony TV is off or unreachable.",
    )

    def explode(*_: Any) -> Outcome:
        raise RuntimeError("psk=hunter2 leaked in a message")

    drivers["fake"].run = explode  # type: ignore[method-assign]
    reply = service.handle({"action": "do", "device": "Sony TV", "command": "turn_on"})
    assert reply["code"] == "failed" and reply["text"] == "Controlling the Sony TV failed (RuntimeError)."
    assert "hunter2" not in json.dumps(reply)


def test_a_driver_that_cannot_be_imported(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.update(lambda c: c.upsert(DeviceRecord("appletv-x", "appletv", "Apple TV", "media_player")))
    service = HomeService(tmp_path, store=store, drivers={"appletv": "jarvis_voice.home.no_such_module:Driver"})
    reply = service.handle({"action": "do", "device": "Apple TV", "command": "turn_on"})
    assert reply["code"] == "unsupported" and "/jarvis setup" in reply["text"]
    assert "unavailable: Apple TV control is not installed" in service.handle({"action": "list"})["text"]
    unknown = HomeService(tmp_path, store=store, drivers={})
    assert unknown.handle({"action": "status", "device": "Apple TV"})["code"] == "unsupported"


def test_hub_devices_join_the_list_and_a_failing_hub_is_noted(tmp_path: Path) -> None:
    service, drivers = make_service(tmp_path, [TV], hubs={"fakehub": {"url": "http://hub"}})
    text = service.handle({"action": "list"})["text"]
    assert "Kitchen light (light, Kitchen) id=fakehub:light.kitchen" in text and "Sony TV" in text
    reply = service.handle({"action": "do", "device": "kitchen light", "command": "turn_on"})
    assert reply["result"] == "done" and drivers["fakehub"].calls == [("fakehub:light.kitchen", "turn_on", None)]
    drivers["fakehub"].hub_error = OSError("down")
    text = service.handle({"action": "list"})["text"]
    assert "Sony TV" in text and "Fake hub could not be reached" in text.replace("fakehub", "Fake hub")
    error = OSError("refused")
    error.spoken = "The fake hub refused Jarvis's token."  # type: ignore[attr-defined]
    drivers["fakehub"].hub_error = error
    assert "The fake hub refused Jarvis's token." in service.handle({"action": "list"})["text"]


def test_info_says_what_is_set_up_and_where(tmp_path: Path, reference_validator: Any) -> None:
    service, _ = make_service(tmp_path, [TV, DOOR], hubs={"fakehub": {}})
    reply = service.handle({"action": "info"})
    reference_validator(reply, "CommandResponse")
    assert reply["count"] == 2
    assert reply["devicesFile"] == str(tmp_path / "home" / "devices.json")
    assert reply["credentialsFile"] == str(tmp_path / "home" / "credentials.dat")
    assert "Fake hub connected (2 devices)" in reply["text"]
    assert "readable only by you" in reply["text"]


def test_reload_and_close_release_drivers(tmp_path: Path) -> None:
    service, drivers = make_service(tmp_path, [TV])
    service.handle({"action": "list"})
    first = drivers["fake"]
    service.handle({"action": "reload"})
    assert first.closed
    service.handle({"action": "list"})
    assert drivers["fake"] is not first
    service.close()
    assert drivers["fake"].closed


def test_handle_never_raises(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path, [TV])
    assert service.handle({})["code"] == "bad_value"
    assert service.handle({"action": "status"})["code"] == "bad_value"
    assert service.handle({"action": "do", "device": 5, "command": None})["code"] == "bad_value"


# --------------------------------------------------------------------------- helper and command line


def test_the_daemon_answers_home_commands(tmp_path: Path) -> None:
    built: list[HomeService] = []

    def factory() -> HomeService:
        service, _ = make_service(tmp_path, [TV])
        built.append(service)
        return service

    def daemon(home_factory: Any) -> Daemon:
        return Daemon(
            DaemonConfig(),
            events=RecordingSink(),
            capture=FakeCapture(),
            playback=FakePlayback(),
            synth=FakeSynth(),
            ptt=FakePushToTalk(),
            transcriber_factory=lambda r: FakeTranscriber("x"),
            platform_name="linux",
            home_factory=home_factory,
        )

    with_home = daemon(factory)
    assert built == []  # nothing is built before the first home command
    reply = with_home.handle_command("home", {"action": "do", "device": "Sony TV", "command": "turn_on"})
    assert reply["text"] == "Sony TV: turn_on"
    with_home.handle_command("home", {"action": "list"})
    assert len(built) == 1
    with_home._shutdown_components()
    assert with_home.handle_command("home", {"action": "list"})["ok"] is False
    without = daemon(None)
    assert without.handle_command("home", {"action": "list"}) == protocol.error_response(
        "bad_request", "home control is not available in this helper"
    )


def test_call_once_never_takes_a_confirmation(tmp_path: Path) -> None:
    def factory(data_dir: Path) -> HomeService:
        return make_service(data_dir, ALL)[0]

    body = {"action": "do", "device": "front door", "command": "unlock", "confirmed": True}
    reply = call_once(tmp_path, json.dumps(body), service_factory=factory)
    assert reply["result"] == "confirm" and reply["text"].endswith(CONFIRM_NEEDS_HELPER)
    assert call_once(tmp_path, "{bad", service_factory=factory)["error"]["code"] == "bad_request"
    assert call_once(tmp_path, '{"action": "nuke"}', service_factory=factory)["error"]["code"] == "bad_request"


def test_console_do_confirms_only_at_a_terminal(tmp_path: Path) -> None:
    calls: list[HomeService] = []

    def factory(data_dir: Path) -> HomeService:
        service, _ = make_service(data_dir, ALL)
        calls.append(service)
        return service

    body = {"action": "do", "device": "front door", "command": "unlock", "confirmed": True}

    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    piped = run_console(tmp_path, body, stdin=io.StringIO(), service_factory=factory)
    assert piped["result"] == "failed" and "Run it in a console" in piped["text"]
    refused = run_console(tmp_path, body, stdin=Tty(), ask=lambda q: "n", service_factory=factory)
    assert refused["text"] == "Not done."
    asked: list[str] = []
    done = run_console(tmp_path, body, stdin=Tty(), ask=lambda q: asked.append(q) or "yes", service_factory=factory)
    assert done["result"] == "done" and asked == ["Let Jarvis unlock the Front door? [y/N]: "]


def test_the_wizard_wants_a_person_at_a_console(tmp_path: Path, monkeypatch: Any, capsys: Any) -> None:
    from jarvis_voice.home.wizard import run_wizard

    monkeypatch.setattr(sys, "stdin", io.StringIO("1234\n"))
    assert run_wizard(tmp_path) == 2
    assert "terminal window of your own" in capsys.readouterr().err


def test_home_call_runs_as_a_one_shot_process(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "jarvis_voice", "home", "call", '{"action": "info"}', "--data-dir", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    assert result.returncode == 0, result.stderr
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 1
    reply = json.loads(lines[0])
    assert reply["result"] == "done" and reply["count"] == 0
    assert reply["devicesFile"] == str(tmp_path / "home" / "devices.json")


# --------------------------------------------------------------------------- wizard menu


def test_wizard_renames_moves_and_removes_devices(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path, [TV, DOOR])
    service.store.set_secret(DOOR.secret_key, {"key": "lock-secret-1"})
    ui = ScriptedPrompter(
        [
            "Rename",  # menu: "Rename a device, set its room, or remove it"
            "Sony TV",
            "Rename",
            "Big TV",
            "Rename a device",
            "Big TV",
            "Set the room",
            "Lounge",
            "Rename a device",
            "Big TV",
            "aliases",
            "the box, telly",
            "Rename a device",
            "Front door",
            "Remove",
            True,
            "Show my devices",
            None,
        ]
    )
    assert home_wizard.run_wizard(tmp_path, ui, service=service) == 0
    devices = service.store.load().devices
    assert [(d.name, d.room, d.aliases) for d in devices] == [("Big TV", "Lounge", ["the box", "telly"])]
    assert service.store.secret(DOOR.secret_key) is None
    assert "Removed Front door." in ui.said
    assert "- Big TV (tv, Lounge)" in ui.text


def test_wizard_sets_confirmation_and_tries_a_device(tmp_path: Path) -> None:
    service, drivers = make_service(tmp_path, [TV, DOOR])
    ui = ScriptedPrompter(
        ["Ask before", "Sony TV", "Ask me on screen", "Try a device", "Front door", "unlock", "", None]
    )
    home_wizard.run_wizard(tmp_path, ui, service=service)
    assert service.store.load().device(TV.id).confirm == "screen"  # type: ignore[union-attr]
    # Trying it at the console is the user's own confirmation.
    assert (DOOR.id, "unlock", None) in drivers["fake"].calls
    assert "Front door: unlock" in ui.said


def test_wizard_driver_steps_load_lazily(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[str] = []

    def step(ui: Any, ctx: Any) -> None:
        ran.append(str(ctx.data_dir))
        ui.say("stepped")

    monkeypatch.setattr(sys.modules[__name__], "_fake_step", step, raising=False)
    monkeypatch.setattr(
        home_wizard,
        "WIZARD_STEPS",
        [
            ("fake", "Add a fake thing", f"{__name__}:_fake_step"),
            ("gone", "Add a missing thing", "jarvis_voice.home.not_there:wizard"),
        ],
    )
    service, _ = make_service(tmp_path)
    ui = ScriptedPrompter(["Add a fake thing", "Add a missing thing", None])
    home_wizard.run_wizard(tmp_path, ui, service=service)
    assert ran == [str(tmp_path)] and "stepped" in ui.said
    assert any(line.startswith("Add a missing thing is not available") for line in ui.said)


def test_every_registered_wizard_step_names_a_driver() -> None:
    assert {driver for driver, _, _ in home_base.WIZARD_STEPS} <= set(home_base.DRIVERS)
    assert set(home_base.LABELS) == set(home_base.DRIVERS)


def test_setup_window_without_a_desktop_prints_the_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if sys.platform != "linux":
        pytest.skip("opens a real window elsewhere")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    opened, text = home_wizard.launch_in_new_console(tmp_path)
    assert not opened
    assert text.startswith("Open a terminal and run: ") and "jarvis_voice home setup --data-dir" in text


def test_fake_driver_is_a_driver() -> None:
    assert issubclass(FakeDriver, home_base.Driver)
