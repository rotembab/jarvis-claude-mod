"""The Sony Bravia driver against a fake TV: a JSON-RPC and IRCC server on 127.0.0.1."""

from __future__ import annotations

import http.client
import json
import logging
import re
import socket
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import pytest

from jarvis_voice.home import bravia, net
from jarvis_voice.home.base import DriverContext
from jarvis_voice.home.bravia import KEYS, BraviaClient, BraviaDriver, discover_tvs
from jarvis_voice.home.model import DeviceRecord, Outcome, Value
from jarvis_voice.home.service import HomeService
from jarvis_voice.home.store import HomeStore, StoreError

from home_fakes import ScriptedPrompter, make_store

PSK = "fake-psk-for-tests"
WRONG_PSK = "not-the-tv-key"
MAC = "02:00:00:00:be:ef"
TV_ID = "bravia-sony-tv"
SOAP = '"urn:schemas-sony-com:service:IRCC:1#X_SendIRCC"'
# The fakes speak only plain http: an https request reaches them marked with this header.
OVER_HTTPS = "X-Test-Over-Https"
REAL_REQUEST = net.request  # before the https_ports fixture replaces it

# Sony's published IRCC codes (not secrets: every TV of a model has the same ones).
CODES = {
    "Play": "AAAAAgAAAJcAAAAaAw==",
    "Pause": "AAAAAgAAAJcAAAAZAw==",
    "Stop": "AAAAAgAAAJcAAAAYAw==",
    "Next": "AAAAAgAAAJcAAAA9Aw==",
    "Prev": "AAAAAgAAAJcAAAA8Aw==",
    "Up": "AAAAAQAAAAEAAAB0Aw==",
    "Down": "AAAAAQAAAAEAAAB1Aw==",
    "Left": "AAAAAQAAAAEAAAA0Aw==",
    "Right": "AAAAAQAAAAEAAAAzAw==",
    "Confirm": "AAAAAQAAAAEAAABlAw==",
    "Return": "AAAAAgAAAJcAAAAjAw==",
    "Home": "AAAAAQAAAAEAAABgAw==",
    "ActionMenu": "AAAAAgAAAMQAAABLAw==",
    "VolumeUp": "AAAAAQAAAAEAAAASAw==",
    "VolumeDown": "AAAAAQAAAAEAAAATAw==",
    "Mute": "AAAAAQAAAAEAAAAUAw==",
    "WakeUp": "AAAAAQAAAAEAAAAuAw==",
    "PowerOff": "AAAAAQAAAAEAAAAvAw==",
}

DMR_XML = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0" xmlns:av="urn:schemas-sony-com:av">
 <device>
  <deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
  <friendlyName>{name}</friendlyName>
  <manufacturer>Sony Corporation</manufacturer>
  <modelName>{model}</modelName>
  <av:X_ScalarWebAPI_DeviceInfo>
   <av:X_ScalarWebAPI_Version>1.0</av:X_ScalarWebAPI_Version>
   <av:X_ScalarWebAPI_ServiceList>
    <av:X_ScalarWebAPI_ServiceType>guide</av:X_ScalarWebAPI_ServiceType>
    <av:X_ScalarWebAPI_ServiceType>system</av:X_ScalarWebAPI_ServiceType>
    {extra}
   </av:X_ScalarWebAPI_ServiceList>
  </av:X_ScalarWebAPI_DeviceInfo>
 </device>
</root>"""


# --------------------------------------------------------------------------- the fake TV


class FakeBravia:
    """A Bravia's Scalar REST API and IRCC endpoint, strict about the header names.

    Only one spelling of the IRCC path exists (``ircc_path``), the key must come
    in a header named exactly ``X-Auth-PSK``, and button presses need a header
    named exactly ``SOAPACTION``.
    """

    def __init__(self, *, ircc_path: str = "/sony/IRCC") -> None:
        self.psk = PSK
        self.ircc_path = ircc_path
        self.power = "active"
        self.volume = 20
        self.max_volume = 100
        self.muted = False
        self.speaker_ok = True  # False: the sound goes to an external audio system (40800)
        self.external_volume: int | None = None  # an "audioSystem" volume entry when set
        self.external_muted = False  # that system's mute, which the remote's Mute button toggles
        self.wol: bool | None = None  # what setWolMode last set
        self.stall: set[str] = set()  # methods the TV takes in but never answers
        self.released = threading.Event()  # lets stalled requests go when the fake stops
        self.asleep = False  # True: no Remote start, the TV is off the network
        self.wake_after_drops: int | None = None  # asleep: wake up after dropping this many requests
        self.not_found = 0  # how many requests get a 404 (WebApiCore restarting)
        self.auth_shape = "http"  # how a wrong key is refused: "http" 403 or "json" error 403
        # "403", "404" or "error": a TV made since August 2025, whose plain http answers every request
        # with HTTP 403 or 404 or a JSON-RPC error (its API is only on https; see the https_ports fixture).
        self.plain_http = "api"
        self.sound_output: str | None = "speaker"  # getSoundSettings' outputTerminal (None: no such method)
        self.mac_in_info = True  # False: getSystemInformation leaves macAddr out
        self.network = [
            {"netif": "wlan0", "hwAddr": "02:00:00:00:00:01", "ipAddrV4": "192.0.2.7", "ipAddrV6": ""},
            {"netif": "eth0", "hwAddr": MAC.upper(), "ipAddrV4": "127.0.0.1", "ipAddrV6": ""},
        ]
        self.set_power_error: int | None = None
        self.playing: dict[str, Any] | int = {
            "source": "extInput:hdmi",
            "title": "HDMI 2",
            "uri": "extInput:hdmi?port=2",
        }
        self.apps = [
            {"title": "Netflix", "uri": "com.sony.dtv.com.netflix.ninja", "icon": ""},
            {"title": "YouTube", "uri": "com.sony.dtv.com.google.android.youtube.tv", "icon": ""},
            {"title": "YouTube Kids", "uri": "com.sony.dtv.com.google.android.apps.youtube.kids", "icon": ""},
            {"title": "Prime Video", "uri": "com.sony.dtv.com.amazon.amazonvideo.livingroom", "icon": ""},
        ]
        self.inputs: list[dict[str, Any]] = [
            {"uri": "extInput:hdmi?port=1", "title": "HDMI 1", "connection": False, "label": "", "status": "false"},
            {
                "uri": "extInput:hdmi?port=2",
                "title": "HDMI 2",
                "connection": True,
                "label": "Apple TV",
                "status": "true",
            },
            {"uri": "extInput:hdmi?port=3", "title": "HDMI 3/ARC", "connection": True, "label": "", "status": "false"},
        ]
        self.codes = dict(CODES)
        self.active_app: str | None = None
        self.keyboard = False  # whether the TV's own on-screen keyboard is up
        self.status_list = True  # False: an older TV without getApplicationStatusList
        self.typed: list[str] = []
        self.calls: list[tuple[str, str, Any]] = []
        self.ircc: list[str] = []
        self.paths: list[str] = []
        self.schemes: list[str] = []  # "http" or "https", for each of paths
        self.header_names: list[list[str]] = []
        self.problems: list[str] = []
        self.lock = threading.Lock()
        tv = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                pages = {
                    "/dmr.xml": DMR_XML.format(
                        name="BRAVIA KD-55X85J",
                        model="KD-55X85J",
                        extra="<av:X_ScalarWebAPI_ServiceType>videoScreen</av:X_ScalarWebAPI_ServiceType>",
                    ),
                    "/soundbar.xml": DMR_XML.format(name="HT-A7000", model="HT-A7000", extra=""),
                }
                page = pages.get(self.path)
                self._send(200 if page else 404, (page or "").encode(), "text/xml")

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                with tv.lock:
                    answer = tv.answer(self.path, list(self.headers.items()), body)
                if answer == "stall":
                    tv.released.wait(10)
                    self.close_connection = True
                    return
                if answer is None:
                    self.close_connection = True  # asleep: drop the connection unanswered
                    return
                self._send(*answer)

            def _send(self, status: int, payload: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.address = f"127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self.released.set()
        self.server.shutdown()
        self.server.server_close()

    def methods(self) -> list[str]:
        return [method for _, method, _ in self.calls]

    # -- requests

    def answer(self, path: str, headers: list[tuple[str, str]], body: bytes) -> tuple[int, bytes, str] | str | None:
        names = [name for name, _ in headers]
        secure = OVER_HTTPS in names
        self.paths.append(path)
        self.schemes.append("https" if secure else "http")
        self.header_names.append(names)
        if self.asleep:
            if self.wake_after_drops is not None:
                self.wake_after_drops -= 1
                self.asleep = self.wake_after_drops > 0
            return None
        if self.not_found > 0:
            self.not_found -= 1
            return 404, b"Not Found", "text/plain"
        for name in names:
            if name.lower() in ("x-auth-psk", "soapaction") and name not in ("X-Auth-PSK", "SOAPACTION"):
                self.problems.append(f"header spelled {name}")
        # Case-sensitive on purpose: a client that rewrites the name is refused like a wrong key.
        key_ok = [value for name, value in headers if name == "X-Auth-PSK"] == [self.psk]
        plain_refused = self.plain_http != "api" and not secure
        if path in bravia.IRCC_PATHS:
            return (403, b"", "text/plain") if plain_refused else self._ircc(path, headers, body, key_ok)
        if not path.startswith("/sony/"):
            return 404, b"", "text/plain"
        request = json.loads(body)
        method, params, ident = request.get("method"), request.get("params"), request.get("id")
        if not isinstance(params, list) or not isinstance(ident, int) or ident < 1 or "version" not in request:
            self.problems.append(f"malformed request {request}")
        self.calls.append((path.removeprefix("/sony/"), method, params))
        if method in self.stall:
            return "stall"
        if plain_refused:
            if self.plain_http == "403":
                return 403, b"Forbidden", "text/plain"
            if self.plain_http == "404":
                return 404, b"Not Found", "text/plain"
            return self._json({"error": [7, "Illegal State"], "id": ident})
        if not key_ok:
            if self.auth_shape == "http":
                return 403, b"Forbidden", "text/plain"
            return self._json({"error": [403, "Forbidden"], "id": ident})
        outcome = self._rpc(path.removeprefix("/sony/"), str(method), params or [], str(request.get("version")))
        if isinstance(outcome, int):
            return self._json({"error": [outcome, "error"], "id": ident})
        return self._json({"result": outcome, "id": ident})

    def _json(self, data: Any) -> tuple[int, bytes, str]:
        return 200, json.dumps(data).encode(), "application/json"

    def _ircc(self, path: str, headers: list[tuple[str, str]], body: bytes, key_ok: bool) -> tuple[int, bytes, str]:
        if path != self.ircc_path:
            return 404, b"", "text/plain"
        if [value for name, value in headers if name == "SOAPACTION"] != [SOAP]:
            self.problems.append("no SOAPACTION header")
            return 500, b"", "text/xml"
        if not key_ok:
            return 403, b"", "text/plain"
        match = re.search(r"<IRCCCode>(.*?)</IRCCCode>", body.decode())
        code = match.group(1) if match else None
        if code is None:
            return 500, b"", "text/xml"
        self.ircc.append(code)
        if code and code not in self.codes.values():
            return 500, b"<s:Fault>Invalid Action</s:Fault>", "text/xml"
        if code == CODES["WakeUp"]:
            self.power = "active"
        if code == CODES["PowerOff"]:
            self.power = "standby"
        if self.external_volume is not None and code in (CODES["VolumeUp"], CODES["VolumeDown"]):
            self.external_volume += 1 if code == CODES["VolumeUp"] else -1
        if code == CODES["Mute"]:
            self.external_muted = not self.external_muted
        return 200, b"", "text/xml"

    def _rpc(self, service: str, method: str, params: list[Any], version: str) -> Any:
        first = params[0] if params and isinstance(params[0], dict) else {}
        if (service, method) == ("system", "getPowerStatus"):
            return [{"status": self.power}]
        if (service, method) == ("system", "setPowerStatus"):
            if self.set_power_error is not None:
                return self.set_power_error
            if not isinstance(first.get("status"), bool):
                return 3
            self.power = "active" if first["status"] else "standby"
            return []
        if (service, method) == ("system", "setWolMode"):
            if not isinstance(first.get("enabled"), bool):
                return 3
            self.wol = first["enabled"]
            return []
        if (service, method) == ("system", "getSystemInformation"):
            info = {"product": "TV", "model": "KD-55X85J", "macAddr": MAC.upper(), "name": "BRAVIA", "serial": "0"}
            if not self.mac_in_info:
                del info["macAddr"]
            return [info]
        if (service, method) == ("system", "getNetworkSettings"):
            return [self.network] if params == [{"netif": ""}] else 3
        if (service, method) == ("audio", "getSoundSettings"):
            if self.sound_output is None or version != "1.1":
                return 12 if self.sound_output is None else 14
            if params != [{"target": "outputTerminal"}]:
                return 3
            return [[{"target": "outputTerminal", "currentValue": self.sound_output}]]
        if (service, method) == ("system", "getRemoteControllerInfo"):
            return [{"bundled": True, "type": "RM-J1100"}, [{"name": n, "value": v} for n, v in self.codes.items()]]
        if (service, method) == ("audio", "getVolumeInformation"):
            entries = [
                {"target": "speaker", "volume": self.volume, "mute": self.muted, "maxVolume": self.max_volume},
                {"target": "headphone", "volume": 5, "mute": False, "maxVolume": 100, "minVolume": 0},
            ]
            if self.external_volume is not None:
                entries.append(
                    {
                        "target": "audioSystem",
                        "volume": self.external_volume,
                        "mute": self.external_muted,
                        "maxVolume": 50,
                    }
                )
            return [entries]
        if (service, method) == ("audio", "setAudioVolume"):
            wanted = first.get("volume")
            if not isinstance(wanted, str):
                return 3
            if first.get("target") == "speaker" and not self.speaker_ok:
                return 40800
            if self.sound_output in ("audioSystem", "hdmi"):
                return [0]  # an eARC soundbar: nothing happens, and no error says so
            level = self.volume + int(wanted) if wanted[:1] in "+-" else int(wanted)
            if not 0 <= level <= self.max_volume:
                return 40801
            self.volume = level
            return [0]
        if (service, method) == ("audio", "setAudioMute"):
            if not isinstance(first.get("status"), bool):
                return 3
            if not self.speaker_ok:
                return 40800
            if self.sound_output in ("audioSystem", "hdmi"):
                return [0]
            self.muted = first["status"]
            return [0]
        if (service, method) == ("avContent", "getPlayingContentInfo"):
            if self.power != "active":
                return 40005
            return self.playing if isinstance(self.playing, int) else [self.playing]
        if (service, method) == ("avContent", "getCurrentExternalInputsStatus"):
            return [self.inputs] if version == "1.1" else 14
        if (service, method) == ("avContent", "setPlayContent"):
            found = next((i for i in self.inputs if i["uri"] == first.get("uri")), None)
            if found is None:
                return 41001
            self.playing = {"source": "extInput:hdmi", "title": found["title"], "uri": found["uri"]}
            return []
        if (service, method) == ("appControl", "getApplicationList"):
            return [self.apps]
        if (service, method) == ("appControl", "setActiveApp"):
            if all(app["uri"] != first.get("uri") for app in self.apps):
                return 41401
            self.active_app = first["uri"]
            self.playing = 7  # the API cannot say what an app is showing
            return []
        if (service, method) == ("appControl", "getApplicationStatusList"):
            if not self.status_list:
                return 12
            return [
                [
                    {"name": "textInput", "status": "on" if self.keyboard else "off"},
                    {"name": "cursorDisplay", "status": "off"},
                    {"name": "webBrowse", "status": "off"},
                ]
            ]
        if (service, method) == ("appControl", "setTextForm"):
            # Version 1.1 wants {"encKey", "text"}: a plain string is wrong there.
            if version != "1.0" or len(params) != 1 or not isinstance(params[0], str):
                return 3
            if not self.keyboard:
                return 7  # the software keyboard is not active
            self.typed.append(params[0])
            return [0]
        return 12


# --------------------------------------------------------------------------- fixtures and helpers


@pytest.fixture
def tv() -> Iterator[FakeBravia]:
    fake = FakeBravia()
    fake.start()
    yield fake
    fake.stop()
    assert fake.problems == []


@pytest.fixture(autouse=True)
def no_key_in_logs(caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    caplog.set_level(logging.DEBUG)
    yield
    assert PSK not in caplog.text
    assert WRONG_PSK not in caplog.text


@pytest.fixture(autouse=True)
def https_ports(monkeypatch: pytest.MonkeyPatch) -> set[int]:
    """Ports where a fake TV takes https. The fakes speak plain http, so an https request to one
    of them goes over http, marked with a header; https to any other port finds it closed."""
    ports: set[int] = set()
    plain = net.request

    def request(method: str, url: str, **kwargs: Any) -> net.HttpResponse:
        parts = urlsplit(url)
        if parts.scheme != "https":
            return plain(method, url, **kwargs)
        assert kwargs.get("verify_tls") is False  # nothing on the PC can check the TV's own certificate
        if parts.port not in ports:
            raise net.HttpError("unreachable", f"{parts.hostname}:443 is unreachable (ConnectionRefusedError)")
        kwargs["headers"] = {**(kwargs.get("headers") or {}), OVER_HTTPS: "1"}
        return plain(method, parts._replace(scheme="http").geturl(), **kwargs)

    monkeypatch.setattr(net, "request", request)
    return ports


@pytest.fixture
def woken(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str | None]]:
    """Wake-on-LAN calls, recorded instead of sent."""
    sent: list[tuple[str, str | None]] = []
    monkeypatch.setattr(net, "wake_on_lan", lambda mac, host=None, **_: sent.append((mac, host)) or 3)
    return sent


def quick(driver: BraviaDriver) -> BraviaDriver:
    driver.retry_wait_s = 0.05
    driver.poll_gap_s = 0.02
    driver.key_gap_s = 0.0
    driver.turn_on_budget_s = 3.0
    return driver


def add_tv(tmp_path: Path, tv: FakeBravia, *, psk: str = PSK, **settings: Any) -> tuple[BraviaDriver, HomeStore]:
    store = make_store(tmp_path)
    saved = {"address": tv.address, "mac": MAC, "ircc_path": "/sony/ircc", "scheme": "http"}
    saved.update(settings)
    device = DeviceRecord(TV_ID, "bravia", "Sony TV", "tv", "Living room", settings=saved)
    store.update(lambda c: c.upsert(device))
    store.set_secret(device.secret_key, {"psk": psk})
    return quick(BraviaDriver(DriverContext(store, tmp_path))), store


def device_of(store: HomeStore) -> DeviceRecord:
    device = store.load().device(TV_ID)
    assert device is not None
    return device


def checked(outcome: Outcome) -> Outcome:
    assert PSK not in outcome.text and WRONG_PSK not in outcome.text
    assert "127.0.0.1" not in outcome.text
    return outcome


def run(driver: BraviaDriver, store: HomeStore, command: str, value: Value = None) -> Outcome:
    return checked(driver.run(device_of(store), command, value))


def status(driver: BraviaDriver, store: HomeStore) -> Outcome:
    return checked(driver.status(device_of(store)))


def closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# --------------------------------------------------------------------------- commands from saved data


def test_commands_come_from_saved_data(tmp_path: Path, tv: FakeBravia) -> None:
    driver, store = add_tv(tmp_path, tv)
    names = [spec.name for spec in driver.commands(device_of(store))]
    assert names[:8] == ["turn_on", "turn_off", "toggle", "volume_up", "volume_down", "set_volume", "mute", "unmute"]
    assert set(KEYS) <= set(names)
    assert {"launch_app", "list_apps", "set_input", "list_inputs", "type_text"} <= set(names)
    specs = {spec.name: spec for spec in driver.commands(device_of(store))}
    assert specs["set_volume"].usage() == "set_volume <0-100>"
    assert specs["launch_app"].usage() == "launch_app <an app name or a link>"
    assert specs["type_text"].usage() == "type_text <text for the selected search box>"
    assert specs["set_input"].usage() == "set_input <an input such as HDMI 2>"
    # Once the TV's buttons and inputs are known, only real buttons are offered.
    codes = {k: v for k, v in CODES.items() if k != "Stop"}
    inputs = [{"uri": "extInput:hdmi?port=2", "title": "HDMI 2", "label": "Apple TV"}]
    driver, store = add_tv(tmp_path, tv, ircc_codes=codes, inputs=inputs)
    specs = {spec.name: spec for spec in driver.commands(device_of(store))}
    assert "stop" not in specs and "home" in specs and "menu" in specs
    assert specs["set_input"].usage() == "set_input <Apple TV>"
    assert tv.calls == []  # never the network


# --------------------------------------------------------------------------- status


def test_status_says_power_input_and_volume(tmp_path: Path, tv: FakeBravia) -> None:
    driver, store = add_tv(tmp_path, tv)
    assert status(driver, store).text == "The Sony TV is on, showing HDMI 2, volume 20%."
    # list_inputs saves the labels, so the status can name the device on the input.
    run(driver, store, "list_inputs")
    tv.muted = True
    assert status(driver, store).text == "The Sony TV is on, showing Apple TV (HDMI 2), volume 20%, muted."
    tv.muted, tv.playing = False, 7  # an app is in front: the TV cannot say which
    assert status(driver, store).text == "The Sony TV is on, volume 20%."
    tv.playing = 40005
    assert status(driver, store).text == "The Sony TV is on with the screen off, volume 20%."
    tv.playing = {"source": "tv:dvbt", "title": "BBC One", "uri": "tv:dvbt?trip=1.2.3", "dispNum": "001"}
    assert status(driver, store).text == "The Sony TV is on, showing TV channel BBC One, volume 20%."
    tv.max_volume, tv.volume = 50, 25
    assert status(driver, store).text.endswith("volume 50%.")
    tv.power = "standby"
    assert status(driver, store) == Outcome.done("The Sony TV is off.")


def test_requests_use_exact_header_names(tmp_path: Path, tv: FakeBravia) -> None:
    driver, store = add_tv(tmp_path, tv)
    assert status(driver, store).ok
    assert run(driver, store, "home").ok
    assert tv.header_names and all("X-Auth-PSK" in names for names in tv.header_names)
    ircc = [names for path, names in zip(tv.paths, tv.header_names, strict=True) if path == "/sony/IRCC"]
    assert ircc and all("SOAPACTION" in names for names in ircc)
    assert tv.problems == []
    assert PSK not in repr(BraviaClient(tv.address, PSK))


def test_the_fake_tv_refuses_a_rewritten_header_name(tv: FakeBravia) -> None:
    """What urllib would send ("X-auth-psk"): the reason the driver uses net.request."""
    connection = http.client.HTTPConnection("127.0.0.1", tv.port, timeout=5)
    body = json.dumps({"method": "getPowerStatus", "params": [], "id": 1, "version": "1.0"})
    connection.request("POST", "/sony/system", body=body, headers={"X-auth-psk": PSK})
    assert connection.getresponse().status == 403
    connection.close()
    assert tv.problems == ["header spelled X-auth-psk"]
    tv.problems.clear()


# --------------------------------------------------------------------------- power


def test_turn_on_from_standby(tmp_path: Path, tv: FakeBravia, woken: list[tuple[str, str | None]]) -> None:
    driver, store = add_tv(tmp_path, tv)
    tv.power = "standby"
    assert run(driver, store, "turn_on") == Outcome.done("Turned on the Sony TV.")
    assert tv.power == "active"
    assert woken == [(MAC, "127.0.0.1")]  # the bare host, for the /24 directed broadcast
    assert ("system", "setPowerStatus", [{"status": True}]) in tv.calls
    assert run(driver, store, "turn_on") == Outcome.done("The Sony TV is on.")


@pytest.mark.parametrize("wakes_to", ["active", "standby"])
def test_turn_on_wakes_a_tv_that_is_off_the_network(
    tmp_path: Path, tv: FakeBravia, monkeypatch: pytest.MonkeyPatch, wakes_to: str
) -> None:
    """Without Remote start the TV drops off the network; the magic packet brings it back,
    either on or with only its network up (then setPowerStatus switches the screen on)."""
    driver, store = add_tv(tmp_path, tv)
    # Its network comes up after the magic packet, once it has let two polls go unanswered (counted
    # rather than timed: on a slow runner a timer could fire before the first poll).
    tv.power, tv.asleep, tv.wake_after_drops = wakes_to, True, 2
    packets: list[tuple[bytes, tuple[str, int]]] = []

    class RecordingSocket:
        def __init__(self, *_: Any) -> None:
            pass

        def __enter__(self) -> RecordingSocket:
            return self

        def __exit__(self, *_: Any) -> None:
            pass

        def setsockopt(self, *_: Any) -> None:
            pass

        def sendto(self, data: bytes, target: tuple[str, int]) -> None:
            packets.append((data, target))

    fake_socket = SimpleNamespace(socket=RecordingSocket, AF_INET=0, SOCK_DGRAM=0, SOL_SOCKET=0, SO_BROADCAST=0)
    monkeypatch.setattr(net, "socket", fake_socket)
    assert run(driver, store, "turn_on") == Outcome.done("Turned on the Sony TV.")
    assert tv.power == "active"
    magic = b"\xff" * 6 + bytes.fromhex(MAC.replace(":", "")) * 16
    assert {target for _, target in packets} == {("255.255.255.255", 9), ("127.0.0.255", 9)}
    assert len(packets) == 6 and all(data == magic for data, _ in packets)
    assert len(tv.paths) > len(tv.calls)  # polls were dropped while it slept, then it answered
    asked_on = ("system", "setPowerStatus", [{"status": True}]) in tv.calls
    assert asked_on is (wakes_to == "standby")


def test_turn_on_gives_up_in_time_when_the_tv_never_answers(
    tmp_path: Path, tv: FakeBravia, woken: list[tuple[str, str | None]]
) -> None:
    driver, store = add_tv(tmp_path, tv, mac=None)
    driver.turn_on_budget_s = 0.8
    tv.asleep = True
    started = time.monotonic()
    outcome = run(driver, store, "turn_on")
    assert outcome == Outcome.fail(
        "unreachable", "The Sony TV did not wake up. Check that Remote start is on in the TV's settings."
    )
    assert time.monotonic() - started < 2.0
    assert woken == []  # no MAC known, so no magic packet
    assert len(tv.paths) >= 2  # it kept asking until its time was up


def test_turn_on_says_a_woken_tv_may_still_be_starting(
    tmp_path: Path, tv: FakeBravia, woken: list[tuple[str, str | None]]
) -> None:
    """The magic packet went out, or the API answered 404 while it restarted, but the TV did
    not answer before the call's time ran out: a Google TV can take 15-30 s to boot."""
    starting = Outcome.fail(
        "timeout",
        "The Sony TV has not answered yet; it may still be starting. "
        "If it stays off, check that Remote start is on in the TV's settings.",
    )
    driver, store = add_tv(tmp_path, tv)
    driver.turn_on_budget_s = 0.8
    tv.asleep = True
    assert run(driver, store, "turn_on") == starting
    assert woken == [(MAC, "127.0.0.1")]
    driver, store = add_tv(tmp_path, tv, mac=None)
    driver.turn_on_budget_s = 0.8
    tv.asleep, tv.not_found = False, 1000
    assert run(driver, store, "turn_on") == starting
    assert len(woken) == 1  # no MAC this time


def test_turn_on_falls_back_to_the_wakeup_button(
    tmp_path: Path, tv: FakeBravia, woken: list[tuple[str, str | None]]
) -> None:
    driver, store = add_tv(tmp_path, tv)
    tv.power, tv.set_power_error = "standby", 15
    assert run(driver, store, "turn_on") == Outcome.done("Turned on the Sony TV.")
    assert tv.ircc[-1] == CODES["WakeUp"] and tv.power == "active"


def test_turn_off_and_toggle(tmp_path: Path, tv: FakeBravia, woken: list[tuple[str, str | None]]) -> None:
    driver, store = add_tv(tmp_path, tv)
    assert run(driver, store, "turn_off") == Outcome.done("Turned off the Sony TV.")
    assert ("system", "setPowerStatus", [{"status": False}]) in tv.calls and tv.power == "standby"
    assert run(driver, store, "turn_off") == Outcome.done("The Sony TV is already off.")
    assert run(driver, store, "toggle") == Outcome.done("Turned on the Sony TV.")
    assert run(driver, store, "toggle") == Outcome.done("Turned off the Sony TV.")
    # A TV that refuses setPowerStatus gets the remote's PowerOff button.
    tv.power, tv.set_power_error = "active", 15
    assert run(driver, store, "turn_off") == Outcome.done("Turned off the Sony TV.")
    assert tv.ircc[-1] == CODES["PowerOff"]


# --------------------------------------------------------------------------- sound


def test_volume_steps_set_and_mute(tmp_path: Path, tv: FakeBravia) -> None:
    driver, store = add_tv(tmp_path, tv)
    assert run(driver, store, "volume_up") == Outcome.done("Turned the Sony TV up to 22%.")
    assert run(driver, store, "volume_down") == Outcome.done("Turned the Sony TV down to 20%.")
    assert ("audio", "setAudioVolume", [{"target": "speaker", "volume": "+2"}]) in tv.calls
    assert run(driver, store, "set_volume", 30) == Outcome.done("Set the Sony TV's volume to 30%.")
    assert tv.volume == 30
    tv.max_volume = 50  # the TV's own scale: 50% of 50 is 25
    assert run(driver, store, "set_volume", 50).ok and tv.volume == 25
    tv.volume = 50
    assert run(driver, store, "volume_up") == Outcome.done("The Sony TV is already at its loudest.")
    assert run(driver, store, "mute") == Outcome.done("Muted the Sony TV.")
    assert tv.muted is True
    assert run(driver, store, "unmute") == Outcome.done("Unmuted the Sony TV.")
    assert tv.muted is False


def test_volume_on_an_external_audio_system_uses_the_remote_buttons(tmp_path: Path, tv: FakeBravia) -> None:
    driver, store = add_tv(tmp_path, tv)
    tv.speaker_ok = False  # setAudioVolume for the speaker answers 40800
    assert run(driver, store, "volume_up") == Outcome.done("Turned the Sony TV's sound system up.")
    assert tv.ircc[-2:] == [CODES["VolumeUp"], CODES["VolumeUp"]]
    assert run(driver, store, "volume_down") == Outcome.done("Turned the Sony TV's sound system down.")
    assert tv.ircc[-2:] == [CODES["VolumeDown"], CODES["VolumeDown"]]
    outcome = run(driver, store, "set_volume", 40)
    assert outcome.code == "unsupported" and "only turn it up or down" in outcome.text
    # When the TV reports the audio system's level, set_volume steps to it.
    tv.external_volume = 10  # of 50
    assert run(driver, store, "set_volume", 30) == Outcome.done("Set the Sony TV's sound system to about 30%.")
    assert tv.external_volume == 15


def test_a_long_way_on_a_sound_system_says_how_far_it_got(tmp_path: Path, tv: FakeBravia) -> None:
    """One call presses a volume button at most MAX_KEY_PRESSES times, to stay within its time: a
    longer way says the level it reached, not the one asked for."""
    driver, store = add_tv(tmp_path, tv, ircc_codes=CODES)
    tv.sound_output, tv.external_volume = "hdmi", 45  # of 50
    assert run(driver, store, "set_volume", 20) == Outcome.done(
        "Turned the Sony TV's sound system down to about 40%; say it again to go further."
    )
    assert tv.external_volume == 20 and tv.ircc.count(CODES["VolumeDown"]) == bravia.MAX_KEY_PRESSES
    assert run(driver, store, "set_volume", 20) == Outcome.done("Set the Sony TV's sound system to about 20%.")
    assert tv.external_volume == 10
    tv.external_volume = 0
    assert run(driver, store, "set_volume", 100) == Outcome.done(
        "Turned the Sony TV's sound system up to about 50%; say it again to go further."
    )
    assert tv.external_volume == 25


def test_mute_on_an_external_audio_system_presses_mute_only_when_needed(tmp_path: Path, tv: FakeBravia) -> None:
    """The remote's Mute button toggles, so it is pressed only when the audio system's own
    state is not the one asked for."""
    driver, store = add_tv(tmp_path, tv, ircc_codes=CODES)
    tv.speaker_ok, tv.external_volume = False, 10  # setAudioMute answers 40800 too
    assert run(driver, store, "unmute") == Outcome.done("The Sony TV's sound system is not muted.")
    assert CODES["Mute"] not in tv.ircc and tv.external_muted is False
    assert run(driver, store, "mute") == Outcome.done("Muted the Sony TV's sound system.")
    assert tv.external_muted is True
    assert run(driver, store, "mute") == Outcome.done("The Sony TV's sound system is already muted.")
    assert run(driver, store, "unmute") == Outcome.done("Unmuted the Sony TV's sound system.")
    assert tv.external_muted is False and tv.ircc.count(CODES["Mute"]) == 2
    # Without the system's own entry its state is unknown: one press, and the answer says so.
    tv.external_volume = None
    assert run(driver, store, "unmute") == Outcome.done(
        "Pressed Mute on the Sony TV. It can't say whether its sound system is now muted."
    )
    assert tv.ircc.count(CODES["Mute"]) == 3
    del tv.codes["Mute"]
    driver, store = add_tv(tmp_path, tv, ircc_codes={"Home": CODES["Home"]})
    assert run(driver, store, "mute") == Outcome.fail("unsupported", "The Sony TV can't mute its sound system.")


@pytest.mark.parametrize("output", ["audioSystem", "hdmi"])
def test_with_a_soundbar_on_hdmi_volume_and_mute_use_the_remote_buttons(
    tmp_path: Path, tv: FakeBravia, output: str
) -> None:
    """Over eARC, setAudioVolume can do nothing without an error (a BRAVIA 8 II with a soundbar,
    for one), so when the TV says its sound goes out over HDMI, the remote's buttons go first."""
    driver, store = add_tv(tmp_path, tv, ircc_codes=CODES)
    tv.sound_output, tv.external_volume = output, 10  # of 50
    assert run(driver, store, "volume_up") == Outcome.done("Turned the Sony TV's sound system up.")
    assert tv.ircc[-2:] == [CODES["VolumeUp"], CODES["VolumeUp"]] and tv.external_volume == 12
    assert run(driver, store, "set_volume", 30) == Outcome.done("Set the Sony TV's sound system to about 30%.")
    assert tv.external_volume == 15
    assert run(driver, store, "mute") == Outcome.done("Muted the Sony TV's sound system.")
    assert tv.external_muted is True
    assert "setAudioVolume" not in tv.methods() and "setAudioMute" not in tv.methods()
    # The speakers' level is not what anyone hears, so the status leaves it out.
    assert status(driver, store) == Outcome.done("The Sony TV is on, showing HDMI 2.")
    # Asked once in each of the four calls, never kept in between.
    assert tv.methods().count("getSoundSettings") == 4
    assert ("audio", "getSoundSettings", [{"target": "outputTerminal"}]) in tv.calls


def test_a_tv_without_sound_settings_sets_the_volume_as_before(tmp_path: Path, tv: FakeBravia) -> None:
    driver, store = add_tv(tmp_path, tv)
    tv.sound_output = None  # getSoundSettings answers error 12
    assert run(driver, store, "volume_up") == Outcome.done("Turned the Sony TV up to 22%.")
    assert run(driver, store, "mute") == Outcome.done("Muted the Sony TV.")
    assert status(driver, store) == Outcome.done("The Sony TV is on, showing HDMI 2, volume 22%, muted.")
    assert tv.ircc == []


# --------------------------------------------------------------------------- remote buttons


@pytest.mark.parametrize("command", sorted(KEYS))
def test_every_button_command(tmp_path: Path, tv: FakeBravia, command: str) -> None:
    driver, store = add_tv(tmp_path, tv)
    outcome = run(driver, store, command)
    names, label = KEYS[command]
    assert outcome.ok, outcome
    assert tv.ircc[-1] == next(CODES[n] for n in names if n in CODES)
    if command == "play_pause":
        assert outcome.text == "Pressed Pause on the Sony TV; say play to resume."
    else:
        assert outcome.text == f"Pressed {label} on the Sony TV."


def test_buttons_are_fetched_once_saved_and_refreshed_when_one_is_missing(tmp_path: Path, tv: FakeBravia) -> None:
    driver, store = add_tv(tmp_path, tv)
    run(driver, store, "home")
    run(driver, store, "back")
    assert tv.methods().count("getRemoteControllerInfo") == 1
    assert device_of(store).settings["ircc_codes"] == CODES
    # A saved list without the button is fetched again.
    driver, store = add_tv(tmp_path, tv, ircc_codes={"Home": CODES["Home"]})
    assert run(driver, store, "stop").ok and tv.ircc[-1] == CODES["Stop"]
    assert "Stop" in device_of(store).settings["ircc_codes"]
    # A button this TV's remote does not have.
    del tv.codes["ActionMenu"]
    driver, store = add_tv(tmp_path, tv, ircc_codes={"Home": CODES["Home"]})
    assert run(driver, store, "menu") == Outcome.fail("unsupported", "The Sony TV's remote has no Menu button.")


def test_ircc_path_case_is_found_and_saved(tmp_path: Path, tv: FakeBravia) -> None:
    driver, store = add_tv(tmp_path, tv, ircc_path="/sony/ircc", ircc_codes=CODES)
    assert tv.ircc_path == "/sony/IRCC"
    assert run(driver, store, "home").ok
    assert tv.paths[0] == "/sony/ircc" and tv.ircc[-1] == CODES["Home"]
    assert device_of(store).settings["ircc_path"] == "/sony/IRCC"
    tv.paths.clear()
    assert run(driver, store, "up").ok
    assert "/sony/ircc" not in tv.paths
    # The other spelling, found by the button itself when the TV changed (no idle wake-up first).
    tv.ircc_path = "/sony/ircc"
    assert run(driver, store, "down").ok and device_of(store).settings["ircc_path"] == "/sony/ircc"


def test_an_empty_request_goes_first_after_ten_idle_minutes(tmp_path: Path, tv: FakeBravia) -> None:
    driver, store = add_tv(tmp_path, tv, ircc_codes=CODES, ircc_path="/sony/IRCC")
    run(driver, store, "home")
    assert tv.ircc == ["", CODES["Home"]]  # first button since the helper started
    run(driver, store, "up")
    assert tv.ircc == ["", CODES["Home"], CODES["Up"]]
    driver._state(TV_ID).last_ircc = time.monotonic() - 601
    run(driver, store, "down")
    assert tv.ircc[-2:] == ["", CODES["Down"]]


def test_a_code_the_tv_rejects_is_reported(tmp_path: Path, tv: FakeBravia) -> None:
    codes = {**CODES, "Home": "AAAAAQAAAAEAAAAAAw=="}
    driver, store = add_tv(tmp_path, tv, ircc_codes=codes)
    assert run(driver, store, "home") == Outcome.fail("failed", "The Sony TV did not accept the Home button.")


# --------------------------------------------------------------------------- apps and inputs


def test_launch_and_list_apps(tmp_path: Path, tv: FakeBravia) -> None:
    driver, store = add_tv(tmp_path, tv)
    assert run(driver, store, "launch_app", "netflix") == Outcome.done("Opened Netflix on the Sony TV.")
    assert tv.active_app == "com.sony.dtv.com.netflix.ninja"
    assert run(driver, store, "launch_app", "YouTube").text == "Opened YouTube on the Sony TV."
    assert tv.active_app == "com.sony.dtv.com.google.android.youtube.tv"
    assert run(driver, store, "launch_app", "prime").text == "Opened Prime Video on the Sony TV."
    assert run(driver, store, "launch_app", "youtube kid").text == "Opened YouTube Kids on the Sony TV."
    assert run(driver, store, "launch_app", "Netflx").text == "Opened Netflix on the Sony TV."
    missing = run(driver, store, "launch_app", "Disney")
    assert missing.code == "not_found" and '"Disney"' in missing.text
    tv.apps += [{"title": "YouTube Music", "uri": "ytm"}, {"title": "Apple Music", "uri": "am"}]
    assert run(driver, store, "launch_app", "music") == Outcome.fail(
        "ambiguous", "Which app: YouTube Music, Apple Music?"
    )
    listed = run(driver, store, "list_apps")
    assert (
        listed.text == "Apps on the Sony TV: Apple Music, Netflix, Prime Video, YouTube, YouTube Kids, YouTube Music."
    )


def test_launch_failure_is_spoken(tmp_path: Path, tv: FakeBravia, monkeypatch: pytest.MonkeyPatch) -> None:
    driver, store = add_tv(tmp_path, tv)
    original = tv._rpc
    monkeypatch.setattr(tv, "_rpc", lambda *a: 41401 if a[1] == "setActiveApp" else original(*a))
    assert run(driver, store, "launch_app", "Netflix") == Outcome.fail("failed", "The Sony TV could not open Netflix.")


# Kick's Android TV package is com.kick.mobile (Sony's uri is "com.sony.dtv.<package>.<activity>");
# the activity after it is made up for the fake.
KICK_URI = "com.sony.dtv.com.kick.mobile.com.kick.mobile.MainActivity"
NO_LINKS = "Sony's documented control API only starts apps"


def test_a_kick_link_opens_kick_and_says_the_channel_needs_typing(
    tmp_path: Path, tv: FakeBravia, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver, store = add_tv(tmp_path, tv)
    tv.apps.append({"title": "Kick", "uri": KICK_URI, "icon": ""})
    assert run(driver, store, "launch_app", "https://www.kick.com/xQc") == Outcome.done(
        f"Opened Kick on the Sony TV. {NO_LINKS}, so it can't open xqc's channel. "
        "Select Kick's search box and I'll type the name."
    )
    assert tv.calls[-1] == ("appControl", "setActiveApp", [{"uri": KICK_URI}])
    assert tv.methods().count("setActiveApp") == 1
    assert run(driver, store, "launch_app", "kick.com/xqc/videos") == Outcome.done(
        f"Opened Kick on the Sony TV. {NO_LINKS}, so it can't open that link inside Kick."
    )
    assert run(driver, store, "launch_app", "kick.com") == Outcome.done("Opened Kick on the Sony TV.")
    sent = json.dumps(tv.calls)
    assert "https://" not in sent and "localapp://" not in sent and "xqc" not in sent
    original = tv._rpc
    monkeypatch.setattr(tv, "_rpc", lambda *a: 41401 if a[1] == "setActiveApp" else original(*a))
    failed = Outcome.fail("failed", "The Sony TV could not open Kick.")
    assert run(driver, store, "launch_app", "kick.com/xqc") == failed


def test_a_kick_link_finds_kick_by_its_package_before_its_title(
    tmp_path: Path, tv: FakeBravia, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver, store = add_tv(tmp_path, tv)
    tv.apps += [
        {"title": "Kickboxing Coach", "uri": "com.sony.dtv.com.example.kickboxing.Main", "icon": ""},
        {"title": "Kick: Live Streaming", "uri": KICK_URI, "icon": ""},
    ]
    # The answer names the app the TV opened, by the TV's own title.
    assert run(driver, store, "launch_app", "kick.com/xqc") == Outcome.done(
        f"Opened Kick: Live Streaming on the Sony TV. {NO_LINKS}, so it can't open xqc's channel. "
        "Select Kick's search box and I'll type the name."
    )
    assert tv.active_app == KICK_URI
    # With no uri of Kick's package, a title must be Kick's name or start with it as a word.
    tv.apps[-1]["uri"] = "com.sony.dtv.com.example.streaming.Main"
    assert run(driver, store, "launch_app", "kick.com") == Outcome.done("Opened Kick: Live Streaming on the Sony TV.")
    assert tv.active_app == "com.sony.dtv.com.example.streaming.Main"
    tv.apps.append({"title": "Kick Boxing Workout", "uri": "com.sony.dtv.com.example.workout.Main", "icon": ""})
    assert run(driver, store, "launch_app", "kick.com/xqc") == Outcome.fail(
        "ambiguous", "Which app: Kick: Live Streaming, Kick Boxing Workout?"
    )
    # A title that is Kick's name alone wins over those that only start with it.
    tv.apps.append({"title": "KICK", "uri": "com.sony.dtv.com.example.kick.Main", "icon": ""})
    assert run(driver, store, "launch_app", "kick.com").ok
    assert tv.active_app == "com.sony.dtv.com.example.kick.Main"
    del tv.apps[-2:]
    original = tv._rpc
    monkeypatch.setattr(tv, "_rpc", lambda *a: 41401 if a[1] == "setActiveApp" else original(*a))
    assert run(driver, store, "launch_app", "kick.com/xqc") == Outcome.fail(
        "failed", "The Sony TV could not open Kick: Live Streaming."
    )


def test_a_kick_link_without_kick_installed_opens_nothing(tmp_path: Path, tv: FakeBravia) -> None:
    driver, store = add_tv(tmp_path, tv)
    missing = Outcome.fail(
        "bad_value",
        "Kick isn't installed on the Sony TV. Install it from the Google Play Store on the TV "
        "(it needs Android 8 or later), then ask again.",
    )
    assert run(driver, store, "launch_app", "kick.com/xqc") == missing
    # An app whose title only looks like Kick's is not Kick: one spelled close to it, one whose
    # title starts with or contains those letters, or one that names Kick in a later word.
    for title in ("Nick", "Kik", "Tick", "Kicker", "Kickstarter", "Kickboxing Coach", "Sidekick TV", "Power Kick"):
        tv.apps.append({"title": title, "uri": "com.sony.dtv.com.example.lookalike.Main", "icon": ""})
        assert run(driver, store, "launch_app", "kick.com/xqc") == missing, title
        del tv.apps[-1]
    tv.apps = []
    assert run(driver, store, "launch_app", "kick.com/xqc") == Outcome.fail(
        "failed", "The Sony TV did not list any apps."
    )
    assert "setActiveApp" not in tv.methods()


def test_other_links_are_refused_without_asking_the_tv(tmp_path: Path, tv: FakeBravia) -> None:
    driver, store = add_tv(tmp_path, tv)
    refused = Outcome.fail("unsupported", f"The Sony TV can't open links: {NO_LINKS}. Ask me to open the app instead.")
    for link in ("https://example.com/x", "youtube.com/watch?v=abc", "https://kick.com/go-live"):
        assert run(driver, store, "launch_app", link) == refused
    assert tv.calls == []


def test_type_text_types_into_the_on_screen_keyboard(tmp_path: Path, tv: FakeBravia) -> None:
    driver, store = add_tv(tmp_path, tv)
    tv.keyboard = True
    assert run(driver, store, "type_text", "xqc") == Outcome.done('Typed "xqc" on the Sony TV.')
    # Version 1.0 with a plain string: the fake answers anything else with error 3.
    assert tv.calls[-1] == ("appControl", "setTextForm", ["xqc"])
    assert tv.methods() == ["getApplicationStatusList", "setTextForm"]
    assert run(driver, store, "type_text", "x" * 61) == Outcome.done("Typed the text on the Sony TV.")
    service = HomeService(tmp_path, store=store, drivers={"bravia": lambda ctx: quick(BraviaDriver(ctx))})
    try:
        reply = service.handle({"action": "do", "device": "sony tv", "command": "type", "value": "trainwreck"})
        assert (reply["result"], reply["text"]) == ("done", 'Typed "trainwreck" on the Sony TV.')
    finally:
        service.close()
    assert tv.typed == ["xqc", "x" * 61, "trainwreck"]


def test_type_text_without_a_keyboard_sends_no_text(tmp_path: Path, tv: FakeBravia) -> None:
    driver, store = add_tv(tmp_path, tv)
    assert run(driver, store, "type_text", "xqc") == Outcome.fail(
        "failed", "No on-screen keyboard is open on the Sony TV. Select the search box first, then ask again."
    )
    assert "setTextForm" not in tv.methods() and tv.typed == []


def test_type_text_relies_on_error_7_when_the_tv_cannot_say(
    tmp_path: Path, tv: FakeBravia, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver, store = add_tv(tmp_path, tv)
    tv.status_list = False
    assert run(driver, store, "type_text", "xqc") == Outcome.fail(
        "failed",
        "The Sony TV has no on-screen keyboard open, so I can't type there. Select the search box first; "
        "if it is selected, this app uses a keyboard of its own, so type with the remote.",
    )
    tv.keyboard = True
    assert run(driver, store, "type_text", "xqc") == Outcome.done('Typed "xqc" on the Sony TV.')
    assert tv.typed == ["xqc"]
    # An older TV without setTextForm.
    tv.status_list = True
    original = tv._rpc
    monkeypatch.setattr(tv, "_rpc", lambda *a: 12 if a[1] == "setTextForm" else original(*a))
    assert run(driver, store, "type_text", "xqc") == Outcome.fail("unsupported", "The Sony TV does not support that.")


def test_type_text_on_a_tv_that_now_takes_only_https_types_once(
    tmp_path: Path, tv: FakeBravia, https_ports: set[int]
) -> None:
    """Its plain http answers error 7, which the keyboard check passes on so the https try can run."""
    driver, store = add_tv(tmp_path, tv)
    tv.plain_http, tv.keyboard = "error", True
    https_ports.add(tv.port)
    assert run(driver, store, "type_text", "xqc") == Outcome.done('Typed "xqc" on the Sony TV.')
    assert tv.typed == ["xqc"] and device_of(store).settings["scheme"] == "https"


def test_type_text_is_sent_once_when_the_tv_is_slow(tmp_path: Path, tv: FakeBravia) -> None:
    driver, store = add_tv(tmp_path, tv)
    driver.ctx.call_timeout = 2.5  # a 1 s budget
    tv.keyboard, tv.stall = True, {"setTextForm"}
    assert run(driver, store, "type_text", "xqc") == Outcome.fail(
        "timeout", "The Sony TV did not answer in time; it may still act on it."
    )
    assert tv.methods().count("setTextForm") == 1


def test_inputs_by_label_name_or_number(tmp_path: Path, tv: FakeBravia) -> None:
    driver, store = add_tv(tmp_path, tv)
    assert run(driver, store, "set_input", "Apple TV") == Outcome.done("Switched the Sony TV to Apple TV (HDMI 2).")
    assert tv.playing == {"source": "extInput:hdmi", "title": "HDMI 2", "uri": "extInput:hdmi?port=2"}
    assert run(driver, store, "set_input", "hdmi 3").text == "Switched the Sony TV to HDMI 3/ARC."
    assert run(driver, store, "set_input", "HDMI1").text == "Switched the Sony TV to HDMI 1."
    assert run(driver, store, "set_input", "input 2").text == "Switched the Sony TV to Apple TV (HDMI 2)."
    missing = run(driver, store, "set_input", "Xbox")
    assert missing == Outcome.fail(
        "not_found",
        'The Sony TV has no input called "Xbox". Its inputs: HDMI 1; Apple TV (HDMI 2); HDMI 3/ARC.',
    )
    # "status" is the string "false" for inputs without a signal: only HDMI 2 is active.
    assert run(driver, store, "list_inputs") == Outcome.done(
        "Inputs on the Sony TV: HDMI 1; Apple TV (HDMI 2, active); HDMI 3/ARC."
    )
    assert device_of(store).settings["inputs"][1] == {
        "uri": "extInput:hdmi?port=2",
        "title": "HDMI 2",
        "label": "Apple TV",
    }


# --------------------------------------------------------------------------- failures


@pytest.mark.parametrize("shape", ["http", "json"])
def test_a_wrong_key_is_an_auth_failure(tmp_path: Path, tv: FakeBravia, shape: str, https_ports: set[int]) -> None:
    driver, store = add_tv(tmp_path, tv, psk=WRONG_PSK)
    tv.auth_shape = shape
    # Saved on http, so https gets one try first: the TV may now take only https.
    refused = Outcome.fail(
        "auth",
        "The Sony TV refused the pre-shared key, or now needs a secure connection; add it again in home setup.",
    )
    assert status(driver, store) == refused
    assert run(driver, store, "set_volume", 10) == refused
    assert run(driver, store, "launch_app", "Netflix") == refused
    driver, store = add_tv(tmp_path, tv, psk=WRONG_PSK, ircc_codes=CODES)
    assert run(driver, store, "home") == refused
    https_ports.add(tv.port)  # https answers too, and refuses the key as well
    assert status(driver, store) == refused
    assert device_of(store).settings["scheme"] == "http"
    # Saved on https: the key is simply wrong.
    driver, store = add_tv(tmp_path, tv, psk=WRONG_PSK, scheme="https")
    assert status(driver, store) == Outcome.fail(
        "auth", "The Sony TV refused the pre-shared key; re-enter it in home setup."
    )


def test_a_tv_that_now_takes_only_https_is_switched_over(tmp_path: Path, tv: FakeBravia, https_ports: set[int]) -> None:
    """Saved on http, the TV now answers plain http only with HTTP 403: one getSystemInformation
    over https, the command again over https, and https saved for the next calls."""
    driver, store = add_tv(tmp_path, tv)
    tv.plain_http = "403"
    https_ports.add(tv.port)
    assert run(driver, store, "set_volume", 30) == Outcome.done("Set the Sony TV's volume to 30%.")
    assert tv.volume == 30 and device_of(store).settings["scheme"] == "https"
    assert tv.schemes[0] == "http" and set(tv.schemes[1:]) == {"https"}
    assert tv.methods()[1] == "getSystemInformation"
    tv.schemes.clear()
    assert status(driver, store) == Outcome.done("The Sony TV is on, showing HDMI 2, volume 30%.")
    assert run(driver, store, "home") == Outcome.done("Pressed Home on the Sony TV.")
    assert set(tv.schemes) == {"https"}


@pytest.mark.parametrize(
    ("plain", "says"),
    [
        ("error", "The Sony TV can't do that right now. If this keeps happening, add the Sony TV again in home setup."),
        ("404", "The Sony TV's control service is restarting; try again in half a minute."),
    ],
    ids=["error", "404"],
)
def test_a_tv_whose_plain_http_now_answers_only_errors_is_switched_over(
    tmp_path: Path, tv: FakeBravia, https_ports: set[int], plain: str, says: str
) -> None:
    """Saved on http, the TV now answers plain http only with a JSON-RPC error or a 404, not a
    refused key: an error before anything worked in the call gets the one try over https too."""
    driver, store = add_tv(tmp_path, tv)
    tv.plain_http = plain
    # https does not answer either: the TV's own trouble is told, and it stays on http.
    outcome = status(driver, store)
    assert not outcome.ok and outcome.text == says
    assert device_of(store).settings["scheme"] == "http"
    https_ports.add(tv.port)
    assert run(driver, store, "set_volume", 30) == Outcome.done("Set the Sony TV's volume to 30%.")
    assert tv.volume == 30 and device_of(store).settings["scheme"] == "https"
    assert status(driver, store) == Outcome.done("The Sony TV is on, showing HDMI 2, volume 30%.")


def test_an_error_after_a_good_answer_keeps_http(
    tmp_path: Path, tv: FakeBravia, https_ports: set[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The TV listed its apps over http, so its error for the next request is about that request."""
    driver, store = add_tv(tmp_path, tv)
    https_ports.add(tv.port)
    original = tv._rpc
    monkeypatch.setattr(tv, "_rpc", lambda *a: 7 if a[1] == "setActiveApp" else original(*a))
    assert run(driver, store, "launch_app", "Netflix") == Outcome.fail("failed", "The Sony TV can't do that right now.")
    assert "https" not in tv.schemes and device_of(store).settings["scheme"] == "http"


def test_the_https_try_stays_within_the_call_budget(tmp_path: Path, tv: FakeBravia, https_ports: set[int]) -> None:
    driver, store = add_tv(tmp_path, tv)
    driver.ctx.call_timeout = 2.5  # a 1 s budget
    tv.plain_http, tv.stall = "403", {"getSystemInformation"}  # https takes the request in and never answers
    https_ports.add(tv.port)
    started = time.monotonic()
    outcome = status(driver, store)
    assert time.monotonic() - started < 1.0 + 0.7
    assert outcome.code == "auth" and "now needs a secure connection" in outcome.text
    assert device_of(store).settings["scheme"] == "http"


def test_an_unreachable_tv_is_off_or_unreachable(tmp_path: Path, tv: FakeBravia) -> None:
    gone = Outcome.fail("unreachable", "The Sony TV is off or unreachable.")
    driver, store = add_tv(tmp_path, tv, address=f"127.0.0.1:{closed_port()}")
    assert status(driver, store) == gone  # a closed port (tried twice: the service may be restarting)
    driver, store = add_tv(tmp_path, tv, ircc_codes=CODES)
    tv.asleep = True  # standby without Remote start: the TV drops every connection
    assert status(driver, store) == gone
    assert run(driver, store, "home") == gone
    assert run(driver, store, "set_input", "HDMI 1") == gone


class SilentTv:
    """Takes connections in and never answers: a frozen WebApiCore, or a TV in Wi-Fi standby."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.sock.settimeout(0.05)
        self.address = f"127.0.0.1:{self.sock.getsockname()[1]}"
        self.held: list[socket.socket] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _ = self.sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self.held.append(connection)

    def close(self) -> None:
        self._stop.set()
        self._thread.join(2)
        for connection in self.held:
            connection.close()
        self.sock.close()


def test_a_silent_tv_never_holds_a_call_past_its_budget(
    tmp_path: Path, tv: FakeBravia, woken: list[tuple[str, str | None]]
) -> None:
    """Each request's timeout is clamped to what is left of the call's budget, which stays
    CALL_MARGIN_S under the service's limit. Scaled down here: a 2.5 s limit, so a 1 s budget;
    without the clamp a 2 s poll or a 10 s REST timeout would run past it."""
    silent = SilentTv()
    driver, store = add_tv(tmp_path, tv, address=silent.address, ircc_codes=CODES)
    driver.ctx.call_timeout = 2.5
    driver.turn_on_budget_s = bravia.TURN_ON_BUDGET_S
    device = device_of(store)

    def timed(command: str) -> tuple[Outcome, float]:
        started = time.monotonic()
        if command == "status":
            outcome = driver.status(device)
        else:
            outcome = driver.run(device, command, "Netflix" if command == "launch_app" else None)
        return checked(outcome), time.monotonic() - started

    commands = ["status", "turn_on", "toggle", "launch_app", "home"]
    try:
        with ThreadPoolExecutor(len(commands)) as pool:  # side by side, to keep the test short
            results = dict(zip(commands, pool.map(timed, commands), strict=True))
    finally:
        silent.close()
    assert len(silent.held) >= len(commands)  # every connection was taken in, then left unanswered
    for command, (outcome, elapsed) in results.items():
        assert elapsed < 1.0 + 0.7, command
        if command in ("turn_on", "toggle"):
            assert outcome.code == "timeout" and "may still be starting" in outcome.text
        else:
            assert outcome == Outcome.fail("unreachable", "The Sony TV is off or unreachable."), command


def test_call_budgets_stay_under_the_service_limit(tmp_path: Path, tv: FakeBravia) -> None:
    driver, store = add_tv(tmp_path, tv)
    limit = driver.ctx.call_timeout - bravia.CALL_MARGIN_S
    for budget in (bravia.CALL_BUDGET_S, bravia.TURN_ON_BUDGET_S, 60.0):
        session = driver._open(device_of(store), budget)
        assert isinstance(session, bravia._Session) and session.client.remaining() <= limit


def test_a_timeout_after_the_tv_answered_is_not_called_off(tmp_path: Path, tv: FakeBravia) -> None:
    """It listed its apps, then took too long to start one: it is on and slow, not off."""
    driver, store = add_tv(tmp_path, tv)
    driver.ctx.call_timeout = 2.5  # a 1 s budget
    tv.stall = {"setActiveApp"}
    assert run(driver, store, "launch_app", "Netflix") == Outcome.fail(
        "timeout", "The Sony TV did not answer in time; it may still act on it."
    )
    tv.stall = {"getPlayingContentInfo"}
    assert status(driver, store) == Outcome.fail("timeout", "The Sony TV did not answer in time.")


def test_a_restarting_api_gets_one_more_try(tmp_path: Path, tv: FakeBravia) -> None:
    driver, store = add_tv(tmp_path, tv)
    tv.not_found = 1
    assert status(driver, store).ok
    assert tv.paths[:2] == ["/sony/system", "/sony/system"]
    tv.not_found = 10
    assert status(driver, store) == Outcome.fail(
        "unreachable", "The Sony TV's control service is restarting; try again in half a minute."
    )


def test_missing_settings_or_key_need_setup(tmp_path: Path, tv: FakeBravia) -> None:
    driver, store = add_tv(tmp_path, tv)
    store.set_secret(f"bravia:{TV_ID}", None)
    outcome = run(driver, store, "turn_on")
    assert outcome.code == "needs_setup" and "home setup" in outcome.text
    assert driver.describe() == "Sony Bravia: no key saved for Sony TV; add it again in home setup."
    driver, store = add_tv(tmp_path, tv, address="")
    assert run(driver, store, "home").code == "needs_setup"
    assert driver.describe() is None
    driver, store = add_tv(tmp_path, tv, psk="bad\nkey")
    assert run(driver, store, "home") == Outcome.fail(
        "needs_setup", "The Sony TV's saved key cannot be used; enter it again in home setup."
    )
    assert tv.paths == []


def test_through_the_home_service(tmp_path: Path, tv: FakeBravia) -> None:
    _, store = add_tv(tmp_path, tv)
    service = HomeService(tmp_path, store=store, drivers={"bravia": lambda ctx: quick(BraviaDriver(ctx))})
    try:
        listing = service.handle({"action": "list"})["text"]
        assert "id=bravia-sony-tv: turn_on, turn_off, toggle, volume_up" in listing
        assert "launch_app <an app name or a link>" in listing
        reply = service.handle({"action": "do", "device": "the TV", "command": "volume", "value": "30%"})
        assert (reply["result"], reply["text"]) == ("done", "Set the Sony TV's volume to 30%.")
        assert tv.volume == 30
        reply = service.handle({"action": "do", "device": "sony tv", "command": "open_app", "value": "netflix"})
        assert reply["text"] == "Opened Netflix on the Sony TV."
        assert service.handle({"action": "status", "device": "telly"})["text"].startswith("The Sony TV is on")
    finally:
        service.close()


# --------------------------------------------------------------------------- discovery and setup


class FakeSsdp:
    """Answers M-SEARCH on 127.0.0.1 with a TV, a soundbar and a reply pointing elsewhere."""

    def __init__(self, tv: FakeBravia) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.05)
        self.address = self.sock.getsockname()
        self.searches: list[bytes] = []
        self.senders: list[tuple[str, int]] = []
        self.replies = [
            f"HTTP/1.1 200 OK\r\nLOCATION: http://127.0.0.1:{tv.port}/dmr.xml\r\nST: {bravia.SSDP_ST}\r\n\r\n",
            f"HTTP/1.1 200 OK\r\nLocation: http://127.0.0.1:{tv.port}/soundbar.xml\r\nST: {bravia.SSDP_ST}\r\n\r\n",
            # A description on another host is never fetched.
            f"HTTP/1.1 200 OK\r\nLOCATION: http://192.0.2.1:52323/dmr.xml\r\nST: {bravia.SSDP_ST}\r\n\r\n",
        ]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                data, sender = self.sock.recvfrom(4096)
            except (TimeoutError, ConnectionResetError):  # Windows: an ICMP error from an earlier reply
                continue
            except OSError:
                return
            self.searches.append(data)
            self.senders.append(sender)
            for reply in self.replies:
                self.sock.sendto(reply.encode(), sender)

    def close(self) -> None:
        self._stop.set()
        self._thread.join(2)
        self.sock.close()


def test_discovery_keeps_only_tvs(tv: FakeBravia) -> None:
    responder = FakeSsdp(tv)
    try:
        found = discover_tvs(0.5, target=responder.address)
    finally:
        responder.close()
    assert [(f.address, f.name, f.model) for f in found] == [("127.0.0.1", "BRAVIA KD-55X85J", "KD-55X85J")]
    assert responder.searches and b"ST: urn:schemas-sony-com:service:ScalarWebAPI:1" in responder.searches[0]


def test_discovery_also_searches_from_each_adapter(tv: FakeBravia, monkeypatch: pytest.MonkeyPatch) -> None:
    """A multicast leaves through one adapter only, so the search also goes out from each of the
    computer's private addresses (the loopback stands in for one here)."""
    responder = FakeSsdp(tv)
    monkeypatch.setattr(bravia, "SSDP_ADDRESS", responder.address)
    monkeypatch.setattr(bravia, "_lan_addresses", lambda: ["127.0.0.1"])
    try:
        found = discover_tvs(0.5)
    finally:
        responder.close()
    assert len(set(responder.senders)) == 2 and len(responder.searches) == 4
    assert [f.address for f in found] == ["127.0.0.1"]  # answered on both sockets, listed once


def wizard_ctx(tmp_path: Path) -> DriverContext:
    return DriverContext(make_store(tmp_path), tmp_path)


@pytest.fixture
def fast_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bravia, "RETRY_WAIT_S", 0.05)
    monkeypatch.setattr(bravia, "DISCOVERY_S", 0.5)
    monkeypatch.setattr(bravia, "_lan_addresses", lambda: [])  # never from the real adapters in tests


def test_wizard_adds_a_tv(tmp_path: Path, tv: FakeBravia, fast_setup: None) -> None:
    ctx = wizard_ctx(tmp_path)
    ui = ScriptedPrompter([False, tv.address, PSK, "", "Living room", True])
    bravia.wizard(ui, ctx)
    device = ctx.store.load().devices[0]
    assert (device.id, device.name, device.kind, device.room) == ("bravia-sony-tv", "Sony TV", "tv", "Living room")
    assert device.settings["address"] == tv.address
    assert device.settings["mac"] == MAC and device.settings["model"] == "KD-55X85J"
    assert device.settings["ircc_path"] == "/sony/IRCC" and device.settings["scheme"] == "http"
    assert device.settings["ircc_codes"] == CODES
    assert device.settings["inputs"][1]["label"] == "Apple TV"
    assert tv.wol is True  # Wake-on-LAN switched on, as Home Assistant does
    assert ctx.store.secret("bravia:bravia-sony-tv") == {"psk": PSK}
    assert PSK not in ctx.store.devices_path.read_text(encoding="utf-8")
    assert "Connected to the Sony KD-55X85J. It is on." in ui.said
    assert "Saved Sony TV. Jarvis can control it now." in ui.said
    # The test at the end: the status, then the Home button.
    assert ui.said[-2].startswith("The Sony TV is on") and ui.said[-1] == "Pressed Home on the Sony TV."
    assert tv.ircc[-1] == CODES["Home"]
    assert PSK not in ui.text and "IP control" in ui.text and "Remote start" in ui.text
    assert "Remote device settings > Control remotely = On" in ui.text and "Energy Mode = Increased" in ui.text


def test_wizard_with_a_wrong_key_saves_nothing(tmp_path: Path, tv: FakeBravia, fast_setup: None) -> None:
    ctx = wizard_ctx(tmp_path)
    ui = ScriptedPrompter([False, tv.address, WRONG_PSK, False])
    bravia.wizard(ui, ctx)
    assert "The TV refused that key." in ui.text and ui.said[-1] == "Nothing was saved."
    assert "Remote device settings > Control remotely is On" in ui.text
    assert ctx.store.load().devices == [] and ctx.store.secret_keys() == []
    assert WRONG_PSK not in ui.text


def test_wizard_retries_after_a_wrong_key(tmp_path: Path, tv: FakeBravia, fast_setup: None) -> None:
    ctx = wizard_ctx(tmp_path)
    tv.auth_shape = "json"
    ui = ScriptedPrompter([False, tv.address, WRONG_PSK, True, "", PSK, "Lounge TV", "", False])
    bravia.wizard(ui, ctx)
    assert "The TV refused that key." in ui.text
    device = ctx.store.load().devices[0]
    assert (device.name, device.room) == ("Lounge TV", None)
    assert ctx.store.secret(device.secret_key) == {"psk": PSK}


@pytest.mark.parametrize(
    ("trouble", "says"),
    [
        ("asleep", "No answer from"),
        ("closed", "refused the connection. Check that IP control and Control remotely are on"),
        ("not_found", "its control service did not. Check that IP control and Control remotely are on"),
    ],
)
def test_wizard_says_why_it_could_not_connect(
    tmp_path: Path, tv: FakeBravia, fast_setup: None, trouble: str, says: str
) -> None:
    ctx = wizard_ctx(tmp_path)
    address = tv.address
    if trouble == "asleep":
        tv.asleep = True
    elif trouble == "closed":
        address = f"127.0.0.1:{closed_port()}"
    else:
        tv.not_found = 100
    ui = ScriptedPrompter([False, address, PSK, False])
    bravia.wizard(ui, ctx)
    assert says in ui.text and ui.said[-1] == "Nothing was saved."
    assert ("list only Simple IP control and Control4" in ui.text) is (trouble == "closed")
    assert ctx.store.load().devices == []


@pytest.mark.parametrize("plain", ["403", "error"])
def test_wizard_adds_a_tv_that_takes_only_https(
    tmp_path: Path, tv: FakeBravia, fast_setup: None, https_ports: set[int], plain: str
) -> None:
    """TVs made since August 2025 answer plain http only with an error: an HTTP 403 that reads as
    a wrong key, or a JSON-RPC error. Setup then tries https, and saves it."""
    ctx = wizard_ctx(tmp_path)
    tv.plain_http = plain
    https_ports.add(tv.port)
    ui = ScriptedPrompter([False, tv.address, PSK, "", "", True])
    bravia.wizard(ui, ctx)
    device = ctx.store.load().devices[0]
    assert device.settings["scheme"] == "https" and device.settings["mac"] == MAC
    assert device.settings["ircc_path"] == "/sony/IRCC" and device.settings["ircc_codes"] == CODES
    assert "Connected to the Sony KD-55X85J. It is on." in ui.said and "refused" not in ui.text
    assert tv.wol is True
    assert ui.said[-1] == "Pressed Home on the Sony TV."


def test_wizard_calls_the_key_wrong_only_when_https_refuses_it_too(
    tmp_path: Path, tv: FakeBravia, fast_setup: None, https_ports: set[int]
) -> None:
    ctx = wizard_ctx(tmp_path)
    tv.plain_http = "403"
    https_ports.add(tv.port)
    ui = ScriptedPrompter([False, tv.address, WRONG_PSK, False])
    bravia.wizard(ui, ctx)
    assert "The TV refused that key." in ui.text and "https" in tv.schemes
    assert ctx.store.load().devices == []


@pytest.mark.parametrize("https", ["closed", "plain port"])
@pytest.mark.parametrize("psk", [PSK, WRONG_PSK], ids=["right key", "wrong key"])
def test_wizard_saves_nothing_when_no_answer_checks_the_key(
    tmp_path: Path, tv: FakeBravia, fast_setup: None, monkeypatch: pytest.MonkeyPatch, psk: str, https: str
) -> None:
    """Plain http answers only with error codes (as a TV made since August 2025 does) and https does
    not answer: nothing showed that the key works, so nothing is saved, whichever key was typed.
    The fake's address has a port, and https goes to that port too, so setup says to leave it out."""
    ctx = wizard_ctx(tmp_path)
    tv.plain_http = "error"  # "closed": https finds no open port there
    if https == "plain port":  # https meets the http port, as at "192.168.1.20:80": the TLS handshake fails
        monkeypatch.setattr(net, "request", REAL_REQUEST)
    ui = ScriptedPrompter([False, tv.address, psk, False])
    bravia.wizard(ui, ctx)
    assert "The TV answered only with errors, so Jarvis could not check the key." in ui.text
    assert "If you typed a port after the address, leave it out." in ui.text
    assert "Connected" not in ui.text and ui.said[-1] == "Nothing was saved."
    assert ctx.store.load().devices == [] and ctx.store.secret_keys() == []
    assert set(tv.methods()) == {"getSystemInformation", "getApplicationList"}


@pytest.mark.parametrize(
    ("plain", "secure", "shown"),
    [
        ("auth", "refused", "auth"),  # nothing on https: http's refusal stands
        ("unverified", "refused", "unverified"),
        ("refused", "auth", "auth"),
        ("auth", "restarting", "restarting"),  # https answered, so its trouble is the one to tell
        ("refused", "tls", "refused"),
        ("restarting", "timeout", "restarting"),
        ("bad_reply", "http", "http"),
    ],
)
def test_setup_tells_the_failure_that_says_more(plain: str, secure: str, shown: str) -> None:
    picked = bravia._pick_scheme(bravia.BraviaError(plain), bravia.BraviaError(secure))
    assert isinstance(picked, bravia.BraviaError) and picked.kind == shown


@pytest.mark.parametrize(
    ("address", "port"),
    [
        ("192.0.2.20", False),
        ("tv.lan", False),
        ("2001:db8::20", False),
        ("192.0.2.20:80", True),
        ("[2001:db8::20]:80", True),
    ],
)
def test_only_errors_says_to_leave_out_a_typed_port(address: str, port: bool) -> None:
    """https goes to the port typed for http, where a TV that takes only https does not serve it."""
    problem = bravia._probe_problem(bravia.BraviaError("unverified"), address)
    assert problem.startswith("The TV answered only with errors, so Jarvis could not check the key.")
    assert ("If you typed a port after the address, leave it out." in problem) is port
    assert address not in problem


def test_setup_keeps_http_unless_https_says_more() -> None:
    def answer(scheme: str, info: dict[str, Any]) -> tuple[BraviaClient, dict[str, Any]]:
        return BraviaClient("192.0.2.20", PSK, scheme=scheme), info

    plain, secure = answer("http", {}), answer("https", {})  # both took the key, without the system information
    informed = answer("https", {"model": "K-55XR70"})
    assert bravia._pick_scheme(plain, secure) is plain
    assert bravia._pick_scheme(plain, bravia.BraviaError("refused")) is plain
    assert bravia._pick_scheme(plain, informed) is informed
    assert bravia._pick_scheme(bravia.BraviaError("auth"), secure) is secure
    refused = bravia._pick_scheme(plain, bravia.BraviaError("auth"))
    assert isinstance(refused, bravia.BraviaError) and refused.kind == "auth"


def test_wizard_finds_the_mac_in_the_network_settings(tmp_path: Path, tv: FakeBravia, fast_setup: None) -> None:
    """Some TVs leave macAddr out of their system information: getNetworkSettings has it, on the
    interface with the address setup reached the TV on."""
    ctx = wizard_ctx(tmp_path)
    tv.mac_in_info = False
    bravia.wizard(ScriptedPrompter([False, tv.address, PSK, "", "", False]), ctx)
    assert ctx.store.load().devices[0].settings["mac"] == MAC
    assert ("system", "getNetworkSettings", [{"netif": ""}]) in tv.calls
    # Only another interface's MAC: none is saved, and the user hears what that means.
    tv.network = tv.network[:1]
    ui = ScriptedPrompter([False, tv.address, PSK, "", "", False])
    bravia.wizard(ui, ctx)
    assert "mac" not in ctx.store.load().devices[0].settings
    assert "did not give its network address (MAC)" in ui.text


def test_wizard_finds_the_tv_and_updates_it_the_second_time(
    tmp_path: Path, tv: FakeBravia, fast_setup: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    responder = FakeSsdp(tv)
    monkeypatch.setattr(bravia, "SSDP_ADDRESS", responder.address)
    ctx = wizard_ctx(tmp_path)
    try:
        ui = ScriptedPrompter([True, "KD-55X85J", tv.address, PSK, "", "Lounge", False])
        bravia.wizard(ui, ctx)
    finally:
        responder.close()
    assert "Which TV?" in ui.asked
    first = ctx.store.load().devices
    assert [d.name for d in first] == ["Sony TV"]
    # Setting the same TV up again (a new key, say) updates it rather than adding a second one.
    ctx.store.update(lambda c: setattr(c.devices[0], "aliases", ["telly"]))
    ui = ScriptedPrompter([False, tv.address, PSK, "", "", False])
    bravia.wizard(ui, ctx)
    assert "This TV is already set up as Sony TV; Jarvis will update it." in ui.said
    devices = ctx.store.load().devices
    assert [(d.id, d.room, d.aliases) for d in devices] == [("bravia-sony-tv", "Lounge", ["telly"])]


def test_wizard_asks_for_the_address_when_the_search_finds_nothing(
    tmp_path: Path, tv: FakeBravia, fast_setup: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A firewall may block the replies, and newer firmware may not answer the search at all."""
    monkeypatch.setattr(bravia, "discover_tvs", lambda *_args, **_kwargs: [])
    ctx = wizard_ctx(tmp_path)
    ui = ScriptedPrompter([True, tv.address, PSK, "", "", False])
    bravia.wizard(ui, ctx)
    assert any(line.startswith("No Sony TV answered.") and "newer TVs may not answer it" in line for line in ui.said)
    assert ui.asked[1].startswith("The TV's IP address") and len(ctx.store.load().devices) == 1


def test_wizard_stopped_halfway_saves_nothing(tmp_path: Path, tv: FakeBravia, fast_setup: None) -> None:
    ctx = wizard_ctx(tmp_path)
    ui = ScriptedPrompter([False, tv.address, PSK, ""])  # quits at the room question
    with pytest.raises(EOFError):
        bravia.wizard(ui, ctx)
    assert ctx.store.load().devices == [] and ctx.store.secret_keys() == []


def test_wizard_leaves_no_key_behind_when_the_tv_cannot_be_saved(
    tmp_path: Path, tv: FakeBravia, fast_setup: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = wizard_ctx(tmp_path)

    def cannot_write(_mutate: Any) -> None:
        raise StoreError("could not write the devices file")

    monkeypatch.setattr(ctx.store, "update", cannot_write)
    ui = ScriptedPrompter([False, tv.address, PSK, "", "Lounge"])
    with pytest.raises(StoreError):
        bravia.wizard(ui, ctx)
    assert ctx.store.secret_keys() == []
    # A TV set up before keeps its old key.
    record = DeviceRecord(TV_ID, "bravia", "Sony TV", "tv", settings={"address": tv.address})
    ctx.store.set_secret(record.secret_key, {"psk": "the-old-fake-psk"})
    with pytest.raises(StoreError):
        bravia._save(ctx, record, PSK)
    assert ctx.store.secret(record.secret_key) == {"psk": "the-old-fake-psk"}


def test_wizard_rejects_bad_addresses_and_keys(tmp_path: Path, tv: FakeBravia, fast_setup: None) -> None:
    ctx = wizard_ctx(tmp_path)
    ui = ScriptedPrompter([False, "not an address", "tv/path", tv.address, "kéy\u0000", "", ""])
    bravia.wizard(ui, ctx)
    assert ui.said[-1] == "Nothing was saved."
    assert ui.text.count("That does not look like an address.") == 2
    assert "That key has characters a TV key cannot have" in ui.text
    assert ctx.store.load().devices == [] and tv.paths == []
