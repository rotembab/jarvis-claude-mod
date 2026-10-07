"""The Tuya driver against fakes: tinytuya (devices that record every request), its UDP scanner, and
tuya_sharing (the Tuya QR login, homes, devices, scenes and token renewal)."""

from __future__ import annotations

import ast
import importlib.util
import json
import logging
import threading
import time
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jarvis_voice.home import tuya
from jarvis_voice.home import tuya_dps as tdp
from jarvis_voice.home.base import DriverContext
from jarvis_voice.home.model import DeviceRecord, Outcome
from jarvis_voice.home.service import HomeService
from jarvis_voice.home.store import HomeStore
from jarvis_voice.home.tuya import CLOUD_SECRET, TuyaDriver

from home_fakes import ScriptedPrompter, make_store

# Made-up keys and tokens (real local keys are 16 random characters; these say what they are).
KEYS = {
    "lamp": "FAKE-KEY-LAMP-01",
    "hall": "FAKE-KEY-HALL-01",
    "plug": "FAKE-KEY-PLUG-01",
    "kitchen": "FAKE-KEY-KTCH-01",
    "curtain": "FAKE-KEY-CURT-01",
    "ac": "FAKE-KEY-AIRC-01",
    "hub": "FAKE-KEY-HUB0-01",
    "ir": "FAKE-KEY-IRBL-01",
    "sensor": "FAKE-KEY-SENS-01",
    "porch": "FAKE-KEY-PRCH-01",
    "fan": "FAKE-KEY-FAN0-01",
    "robot": "FAKE-KEY-ROBO-01",
    "lost": "FAKE-KEY-LOST-01",
}
SECRETS = [*KEYS.values(), "ACCESS-TOKEN-1", "REFRESH-TOKEN-1", "ACCESS-TOKEN-2", "REFRESH-TOKEN-2", "UC-TEST-0001"]
LOGIN = {
    "t": 1_700_000_000_000,
    "uid": "uid-1",
    "expire_time": 7200,
    "access_token": "ACCESS-TOKEN-1",
    "refresh_token": "REFRESH-TOKEN-1",
    "terminal_id": "terminal-1",
    "endpoint": "https://apigw.example.invalid",
    "username": "rotem",
}
RENEWED = {
    "t": 1_700_000_100_000,
    "uid": "uid-1",
    "expire_time": 7200,
    "access_token": "ACCESS-TOKEN-2",
    "refresh_token": "REFRESH-TOKEN-2",
}


# --------------------------------------------------------------------------- fake tinytuya


@dataclass
class Phys:
    """A device on the fake home network."""

    tuya_id: str
    address: str
    key: str
    version: float
    dps: dict[str, Any]
    children: dict[str, Phys] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)  # Err codes for the next requests
    write_errors: list[str] = field(default_factory=list)  # Err codes for the next writes only
    device22: bool = False  # answers a status query only for the DPs it is asked for


class FakeClock:
    """time.monotonic for tests that count seconds without waiting for them."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeNet:
    def __init__(self) -> None:
        self.devices: dict[str, Phys] = {}
        self.calls: list[tuple[str, str, Any]] = []
        self.opened: list[dict[str, Any]] = []
        self.closed = 0
        # When set, a request to an address where nothing answers takes its worst case on this clock: two
        # attempts, each a connect and receives of up to T (4T in all, 8T for v3.4+, as tuya.py counts).
        self.clock: FakeClock | None = None

    def add(self, phys: Phys) -> Phys:
        self.devices[phys.tuya_id] = phys
        return phys

    def writes(self, tuya_id: str) -> list[Any]:
        return [payload for device, op, payload in self.calls if device == tuya_id and op != "status"]


def _error(code: str, key: str) -> dict[str, Any]:
    # A real tinytuya error dict may carry whatever the device sent; here it carries the key itself.
    return {"Error": "fake error", "Err": code, "Payload": f"rejected with key {key}"}


class FakeDevice:
    """tinytuya.Device's interface: answers from the FakeNet, never raises, errors as dicts."""

    net: FakeNet

    def __init__(
        self,
        dev_id: str,
        address: str | None = None,
        local_key: str = "",
        dev_type: str = "default",
        connection_timeout: float = 5,
        version: float = 3.1,
        persist: bool = False,
        cid: str | None = None,
        node_id: str | None = None,
        parent: FakeDevice | None = None,
        connection_retry_limit: int = 5,
        connection_retry_delay: float = 5,
        port: int = 6668,
        max_simultaneous_dps: int = 0,
    ) -> None:
        self.id, self.address, self._key, self.parent = dev_id, address, local_key, parent
        self.version = parent.version if parent is not None else version
        self.cid = cid or node_id
        self.dev_type = dev_type
        self.timeout = connection_timeout
        self.dps_to_request: dict[str, None] = {}
        self.net.opened.append(
            {
                "id": dev_id,
                "address": address,
                "version": version,
                "timeout": connection_timeout,
                "retry_limit": connection_retry_limit,
                "persist": persist,
                "cid": self.cid,
                "parent": parent.id if parent is not None else None,
                "has_key": bool(local_key),
            }
        )

    def __repr__(self) -> str:
        raise AssertionError("a tinytuya device was repr()'d: its repr carries the local key")

    def set_version(self, version: float) -> None:
        self.version = version

    def set_socketTimeout(self, seconds: float) -> None:  # noqa: N802 - tinytuya's name
        self.timeout = seconds

    def _target(self) -> Phys | dict[str, Any]:
        gateway = self.parent or self
        phys = self.net.devices.get(gateway.id)
        if phys is None or phys.address != gateway.address:
            if self.net.clock is not None:
                self.net.clock.now += (8 if float(gateway.version) >= 3.4 else 4) * self.timeout
            return _error("905", gateway._key)
        if phys.errors:
            return _error(phys.errors.pop(0), gateway._key)
        if abs(phys.version - float(gateway.version)) > 0.001 or phys.key != gateway._key:
            return _error("914", gateway._key)
        if self.parent is not None:
            child = phys.children.get(self.cid or "")
            return child if child is not None else _error("905", gateway._key)
        return phys

    def status(self) -> Any:
        self.net.calls.append((self.id, "status", None))
        target = self._target()
        if isinstance(target, dict):
            return target
        if target.device22:
            if self.dev_type != "device22":
                # As tinytuya: the reply says "data unvalid", so it switches dialect and asks again for DP 1 only.
                self.dev_type, self.dps_to_request = "device22", {"1": None}
                self.net.calls.append((self.id, "status", None))
            return {"dps": {k: v for k, v in target.dps.items() if k in self.dps_to_request}}
        return {"dps": dict(target.dps)}

    def _write_error(self) -> dict[str, Any] | None:
        gateway = self.parent or self
        phys = self.net.devices.get(gateway.id)
        return _error(phys.write_errors.pop(0), gateway._key) if phys is not None and phys.write_errors else None

    def set_value(self, index: str, value: Any) -> Any:
        self.net.calls.append((self.id, "set_value", {str(index): value}))
        target = self._write_error() or self._target()
        if isinstance(target, dict):
            return target
        target.dps[str(index)] = value
        return {"dps": {str(index): value}}

    def set_multiple_values(self, data: dict[str, Any]) -> Any:
        self.net.calls.append((self.id, "set_multiple_values", dict(data)))
        target = self._write_error() or self._target()
        if isinstance(target, dict):
            return target
        target.dps.update({str(k): v for k, v in data.items()})
        return {"dps": dict(data)}

    def close(self) -> None:
        self.net.closed += 1


def fake_tinytuya(net: FakeNet) -> Any:
    device = type("Device", (FakeDevice,), {"net": net})
    return SimpleNamespace(Device=device, OutletDevice=device)


class FakeScanner:
    """tinytuya.scanner: answers for the devices in ``answers`` (id -> (address, version))."""

    def __init__(self) -> None:
        self.answers: dict[str, tuple[str, str]] = {}
        self.calls: list[dict[str, Any]] = []
        self.error: BaseException | None = None

    def devices(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return {
            i: {"gwId": i, "id": i, "ip": self.answers[i][0], "version": self.answers[i][1]}
            for i in kwargs["wantids"]
            if i in self.answers
        }


# --------------------------------------------------------------------------- fake tuya_sharing


class CloudError(Exception):
    """Shaped like tuya_sharing's ApiRequestException."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"network error:({code}) {message}")
        self.error_code, self.error_message = code, message


def dp(dp_id: int, code: str, typ: str, desc: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    return {"dp": dp_id, "code": code, "type": typ, "desc": desc or {}, **extra}


def dtos(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The cloud's dpStatusRelationDTOS for these points (what /v1.0/m/life/devices/{id}/status returns)."""
    return [
        {
            "dpId": p["dp"],
            "dpCode": p["code"],
            "statusCode": p["code"],
            "valueType": p["type"],
            "valueDesc": json.dumps(p["desc"]),
            "enumMappingMap": p.get("enum", {}),
            "statusFormat": json.dumps({p["code"]: "$"}),
            "valueConvert": p.get("convert", "enum" if p["type"] == "Enum" else "default"),
            "supportLocal": p.get("local", True),
        }
        for p in points
    ]


def function_spec(points: list[dict[str, Any]]) -> dict[str, SimpleNamespace]:
    return {
        p["code"]: SimpleNamespace(code=p["code"], type=p["type"], values=json.dumps(p["desc"]))
        for p in points
        if not p.get("ro")
    }


def cloud_device(
    tuya_id: str, name: str, category: str, key: str, points: list[dict[str, Any]], *, local: bool = True, **extra: Any
) -> SimpleNamespace:
    """A CustomerDevice as tuya_sharing builds it: spec, status range and (if every DP is local) its strategy."""
    strategy = {}
    for p in points:
        enum = p.get("enum", {})
        convert = p.get("convert", "enum" if p["type"] == "Enum" else "default")
        strategy[p["dp"]] = {
            "value_convert": convert,
            "status_code": p["code"],
            "config_item": {
                "statusFormat": json.dumps({p["code"]: "$"}),
                "valueDesc": json.dumps(p["desc"]),
                "valueType": p["type"],
                "enumMappingMap": enum,
                "pid": "pid",
            },
        }
    fields = {
        "id": tuya_id,
        "name": name,
        "category": category,
        "local_key": key,
        "product_name": f"{name} product",
        "sub": False,
        "online": True,
        "ip": "203.0.113.9",  # the WAN address the cloud reports: of no use locally
        "status": {},
        "function": function_spec(points),
        "status_range": {p["code"]: SimpleNamespace(code=p["code"], type=p["type"], values="{}") for p in points},
        "support_local": local,
        "local_strategy": strategy if local else {},
        "dtos": dtos(points),
    }
    fields.update(extra)
    return SimpleNamespace(**fields)


class FakeCloud:
    """The Tuya cloud behind tuya_sharing's LoginControl and Manager."""

    def __init__(self) -> None:
        self.devices: list[SimpleNamespace] = []
        self.scenes: list[SimpleNamespace] = []
        self.rooms: dict[str, str] = {}
        self.qr_response: dict[str, Any] = {"success": True, "result": {"qrcode": "QR-TOKEN-1"}}
        self.logins: deque[Any] = deque()
        self.qr_requests: list[tuple[str, str, str]] = []
        self.login_checks = 0
        self.managers: list[dict[str, Any]] = []
        self.renew_on_load: dict[str, Any] | None = None
        self.load_error: BaseException | None = None
        self.triggered: list[tuple[str, str]] = []
        self.trigger_error: BaseException | None = None
        self.renew_on_trigger: dict[str, Any] | None = None
        self.trigger_hangs: threading.Event | None = None  # the trigger waits for this (a stuck connection)
        # The cloud fallback: each device's state by code, as /v1.0/m/life/ha/devices/detail reports it.
        self.state: dict[str, dict[str, Any]] = {}
        self.offline: set[str] = set()
        self.requests: list[tuple[str, str]] = []  # (method, path) of every device request
        self.commands: list[tuple[str, list[dict[str, Any]]]] = []
        self.answers: deque[Any] = deque()  # replaces the next requests' answers: None, or an exception to raise
        self.seconds = 0.0  # each request takes this long on ``clock``
        self.clock: FakeClock | None = None
        self.hang: threading.Event | None = None  # requests wait for this (a stuck connection)

    def module(self) -> Any:
        cloud = self

        class LoginControl:
            def qr_code(self, client_id: str, schema: str, user_code: str) -> dict[str, Any]:
                cloud.qr_requests.append((client_id, schema, user_code))
                return cloud.qr_response

            def login_result(self, token: str, client_id: str, user_code: str) -> tuple[bool, dict[str, Any]]:
                cloud.login_checks += 1
                assert token == "QR-TOKEN-1" and client_id == tuya.CLIENT_ID
                if not cloud.logins:
                    return False, {"success": False, "code": 1, "msg": "not yet"}
                answer = cloud.logins.popleft()
                if isinstance(answer, BaseException):
                    raise answer
                return answer

        class Manager:
            def __init__(
                self, client_id: str, user_code: str, terminal_id: str, end_point: str, token: Any, listener: Any
            ) -> None:
                cloud.managers.append(
                    {"client_id": client_id, "user_code": user_code, "terminal_id": terminal_id, "token": token}
                )
                self.listener = listener
                self.device_map: dict[str, Any] = {}
                self.user_homes = [SimpleNamespace(id="home-1", name="Home")]
                self.customer_api = SimpleNamespace(get=self._get, post=self._post)

            def update_device_cache(self) -> None:
                if cloud.load_error is not None:
                    raise cloud.load_error
                if cloud.renew_on_load is not None:
                    self.listener.update_token(cloud.renew_on_load)
                self.device_map = {d.id: d for d in cloud.devices}

            def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
                if path == "/v1.0/m/life/ha/devices/detail":
                    assert params is not None
                    tuya_id = params["devIds"]
                    cloud.request("GET", path)
                    status = [{"code": code, "value": value} for code, value in cloud.state.get(tuya_id, {}).items()]
                    # As the real reply: it carries the local key too.
                    item = {"id": tuya_id, "online": tuya_id not in cloud.offline, "local_key": KEYS["lamp"]}
                    return cloud.answer({"success": True, "t": 1, "result": [{**item, "status": status}]})
                tuya_id = path.split("/")[-2]
                device = next(d for d in cloud.devices if d.id == tuya_id)
                return {"success": True, "result": {"productKey": "pid", "dpStatusRelationDTOS": device.dtos}}

            def _post(self, path: str, params: Any, body: dict[str, Any]) -> Any:
                assert path.startswith("/v1.1/m/thing/") and path.endswith("/commands") and params is None
                tuya_id = path.split("/")[-2]
                cloud.request("POST", path)
                cloud.commands.append((tuya_id, list(body["commands"])))
                answer = cloud.answer({"success": True, "t": 1, "result": True})
                if answer is not None:
                    cloud.state.setdefault(tuya_id, {}).update({c["code"]: c["value"] for c in body["commands"]})
                return answer

            def query_room_by_device(self, tuya_id: str) -> Any:
                name = cloud.rooms.get(tuya_id)
                return SimpleNamespace(id="room", name=name) if name else None

            def query_scenes(self) -> list[Any]:
                return list(cloud.scenes)

            def trigger_scene(self, home_id: str, scene_id: str) -> Any:
                if cloud.trigger_hangs is not None:
                    cloud.trigger_hangs.wait(10)
                if cloud.renew_on_trigger is not None:
                    self.listener.update_token(cloud.renew_on_trigger)
                if cloud.trigger_error is not None:
                    raise cloud.trigger_error
                cloud.triggered.append((home_id, scene_id))
                return True

        return SimpleNamespace(LoginControl=LoginControl, Manager=Manager)

    def request(self, method: str, path: str) -> None:
        self.requests.append((method, path))
        if self.hang is not None:
            self.hang.wait(10)
        if self.clock is not None:
            self.clock.now += self.seconds

    def answer(self, normal: Any) -> Any:
        if not self.answers:
            return normal
        answer = self.answers.popleft()
        if isinstance(answer, BaseException):
            raise answer
        return answer


# --------------------------------------------------------------------------- the home: cloud, network, store

LAMP_POINTS = [
    dp(20, "switch_led", "Boolean"),
    dp(21, "work_mode", "Enum", {"range": ["white", "colour", "scene", "music"]}),
    dp(22, "bright_value_v2", "Integer", {"min": 10, "max": 1000, "scale": 0, "step": 1}),
    dp(23, "temp_value_v2", "Integer", {"min": 0, "max": 1000, "scale": 0, "step": 1}),
    dp(24, "colour_data_v2", "Json", {"h": {"min": 0, "max": 360}}, convert="dj_v2_color_alg"),
    dp(26, "countdown", "Integer", {"min": 0, "max": 86400}),
]
HALL_POINTS = [
    dp(1, "switch_led", "Boolean"),
    dp(2, "work_mode", "Enum", {"range": ["white", "colour", "scene", "music"]}),
    dp(3, "bright_value", "Integer", {"min": 25, "max": 255, "scale": 0, "step": 1}),
    dp(4, "temp_value", "Integer", {"min": 0, "max": 255, "scale": 0, "step": 1}),
    dp(5, "colour_data", "Json", {}, convert="dj_v1_hsv_alg"),
]
PLUG_POINTS = [
    dp(1, "switch_1", "Boolean"),
    dp(9, "countdown_1", "Integer", {"min": 0, "max": 86400}),
    dp(19, "cur_power", "Integer", {"min": 0, "max": 50000, "scale": 1, "unit": "W"}, ro=True),
]
KITCHEN_POINTS = [dp(1, "switch_1", "Boolean"), dp(2, "switch_2", "Boolean")]
CURTAIN_POINTS = [
    # The motor speaks "0"/"1"/"2"; the cloud lists the standard words and maps the motor's onto them.
    dp(
        1,
        "control",
        "Enum",
        {"range": ["open", "stop", "close"]},
        enum={"0": {"code": "control", "value": "open"}, "1": {"value": "stop"}, "2": {"value": "close"}},
    ),
    dp(2, "percent_control", "Integer", {"min": 0, "max": 100, "scale": 0, "step": 1, "unit": "%"}),
    dp(3, "percent_state", "Integer", {"min": 0, "max": 100, "scale": 0, "step": 1}, ro=True),
]
AC_POINTS = [
    dp(1, "switch", "Boolean"),
    dp(2, "temp_set", "Integer", {"min": 160, "max": 300, "scale": 1, "step": 5, "unit": "℃"}),
    dp(3, "temp_current", "Integer", {"min": -200, "max": 600, "scale": 1, "step": 1}, ro=True),
    dp(4, "mode", "Enum", {"range": ["cold", "hot", "wet", "wind", "auto"]}),
    dp(5, "fan_speed_enum", "Enum", {"range": ["low", "mid", "high", "auto"]}),
    dp(103, "eco", "Boolean", local=False),
]
GARDEN_POINTS = [dp(1, "switch_1", "Boolean")]
PORCH_POINTS = [
    dp(20, "switch_led", "Boolean"),
    dp(22, "bright_value_v2", "Integer", {"min": 10, "max": 1000, "scale": 0, "step": 1}),
]
FAN_POINTS = [
    dp(1, "switch", "Boolean"),
    dp(2, "mode", "Enum", {"range": ["normal", "nature", "sleep"]}),
    dp(3, "fan_speed", "Enum", {"range": ["1", "2", "3", "4"]}),
]
# A Fingerbot: a robot finger on a wall switch. In click mode each change of "switch" is one press.
FINGER_POINTS = [
    dp(1, "switch", "Boolean"),
    dp(2, "mode", "Enum", {"range": ["click", "switch", "program"]}),
    dp(3, "click_sustain_time", "Integer", {"min": 2, "max": 10}),
    dp(9, "arm_down_percent", "Integer", {"min": 51, "max": 100}),
    dp(12, "battery_percentage", "Integer", {"min": 0, "max": 100, "unit": "%"}, ro=True),
]


@dataclass
class Home:
    store: HomeStore
    ctx: DriverContext
    net: FakeNet
    scanner: FakeScanner
    cloud: FakeCloud

    def driver(self) -> TuyaDriver:
        return TuyaDriver(self.ctx)

    def device(self, name: str) -> DeviceRecord:
        return next(d for d in self.store.load().devices if d.name == name)

    def config_text(self) -> str:
        return self.store.devices_path.read_text(encoding="utf-8")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Home]:
    net, scanner, cloud = FakeNet(), FakeScanner(), FakeCloud()
    module = fake_tinytuya(net)
    monkeypatch.setattr(tuya, "_tinytuya", lambda: module)
    monkeypatch.setattr(tuya, "_scanner", lambda: scanner)
    monkeypatch.setattr(tuya, "_sharing", cloud.module)
    monkeypatch.setattr(tuya, "LOGIN_POLL_S", 0.0)
    monkeypatch.setattr(tuya, "_CLOUD", tuya._CloudSession())  # the helper's one cloud session, fresh per test
    cloud.devices = [
        cloud_device("lamp-id", "Desk lamp", "dj", KEYS["lamp"], LAMP_POINTS),
        cloud_device("hall-id", "Hall bulb", "dj", KEYS["hall"], HALL_POINTS),
        cloud_device("plug-id", "Heater plug", "cz", KEYS["plug"], PLUG_POINTS),
        cloud_device("kitchen-id", "Kitchen switch", "kg", KEYS["kitchen"], KITCHEN_POINTS),
        cloud_device("curtain-id", "Bedroom curtain", "cl", KEYS["curtain"], CURTAIN_POINTS),
        cloud_device("ac-id", "Living room AC", "kt", KEYS["ac"], AC_POINTS, local=False),
        cloud_device("hub-id", "Zigbee hub", "wg2", KEYS["hub"], []),
        cloud_device("garden-id", "Garden plug", "cz", KEYS["hub"], GARDEN_POINTS, sub=True, node_id="a4c1-garden"),
        cloud_device("ir-id", "IR blaster", "wnykq", KEYS["ir"], [dp(201, "control", "String")]),
        cloud_device("sensor-id", "Hall sensor", "wsdcg", KEYS["sensor"], [dp(1, "va_temperature", "Integer")]),
        cloud_device("porch-id", "Porch light", "dj", KEYS["porch"], PORCH_POINTS),
        cloud_device("fan-id", "Ceiling fan", "fs", KEYS["fan"], FAN_POINTS),
    ]
    cloud.rooms = {
        "lamp-id": "Office",
        "hall-id": "Hall",
        "plug-id": "Bedroom",
        "kitchen-id": "Kitchen",
        "curtain-id": "Bedroom",
        "ac-id": "Living room",
        "garden-id": "Garden",
        "fan-id": "Living room",
    }
    cloud.scenes = [
        SimpleNamespace(scene_id="scene-ac-cool", name="AC cool 24", home_id="home-1", enabled=True),
        SimpleNamespace(scene_id="scene-ac-off", name="AC off", home_id="home-1", enabled=True),
        SimpleNamespace(scene_id="scene-old", name="Old scene", home_id="home-1", enabled=False),
    ]
    cloud.logins = deque([(False, {"success": False}), (True, dict(LOGIN))])
    net.add(Phys("lamp-id", "192.168.1.20", KEYS["lamp"], 3.5, {"20": True, "21": "white", "22": 505, "23": 0}))
    net.add(Phys("hall-id", "192.168.1.21", KEYS["hall"], 3.3, {"1": False, "2": "white", "3": 140, "4": 255}))
    net.add(Phys("plug-id", "192.168.1.22", KEYS["plug"], 3.3, {"1": True, "19": 1205}))
    net.add(Phys("kitchen-id", "192.168.1.23", KEYS["kitchen"], 3.4, {"1": False, "2": True}))
    net.add(Phys("curtain-id", "192.168.1.24", KEYS["curtain"], 3.3, {"1": "1", "2": 70, "3": 70}))
    net.add(Phys("ac-id", "192.168.1.25", KEYS["ac"], 3.3, {"1": True, "2": 245, "3": 260, "4": "cold", "5": "auto"}))
    hub = net.add(Phys("hub-id", "192.168.1.26", KEYS["hub"], 3.3, {}))
    hub.children["a4c1-garden"] = Phys("garden-id", "", KEYS["hub"], 3.3, {"1": False})
    net.add(Phys("porch-id", "192.168.1.27", KEYS["porch"], 3.3, {"20": False, "22": 10}))
    net.add(Phys("fan-id", "192.168.1.28", KEYS["fan"], 3.3, {"1": True, "2": "normal", "3": "2"}))
    scanner.answers = {
        "lamp-id": ("192.168.1.20", "3.5"),
        "hall-id": ("192.168.1.21", "3.3"),
        "plug-id": ("192.168.1.22", "3.3"),
        "kitchen-id": ("192.168.1.23", "3.4"),
        "curtain-id": ("192.168.1.24", "3.3"),
        "ac-id": ("192.168.1.25", "3.3"),
        "hub-id": ("192.168.1.26", "3.3"),
        "fan-id": ("192.168.1.28", "3.3"),
    }
    store = make_store(tmp_path)
    ctx = DriverContext(store, tmp_path)
    yield Home(store, ctx, net, scanner, cloud)
    ctx.close()


# The User Code, no to the cloud fallback, names for the kitchen switch's two gangs, then save.
LINK_ANSWERS: list[Any] = ["UC-TEST-0001", False, "Kitchen light", "", True]


def link(home: Home, *, check: bool | None = False) -> ScriptedPrompter:
    """Runs the wizard's Link your Tuya account through to the end (``check`` None: no device found, so not asked)."""
    ui = ScriptedPrompter([*LINK_ANSWERS] + ([] if check is None else [check]))
    tuya.wizard(ui, home.ctx)
    assert not ui.answers, f"unused answers: {list(ui.answers)}"
    return ui


def assert_no_secrets(*texts: str) -> None:
    for text in texts:
        for secret in SECRETS:
            assert secret not in text, f"{secret!r} leaked into {text!r}"


# --------------------------------------------------------------------------- linking the Tuya account


def test_qr_link_saves_devices_scenes_and_keys(home: Home, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    ui = link(home, check=True)

    assert home.cloud.qr_requests == [(tuya.CLIENT_ID, tuya.SCHEMA, "UC-TEST-0001")]
    assert ui.qr and ui.qr[0][0] == "tuyaSmart--qrLogin?token=QR-TOKEN-1"
    assert "Scan" in ui.qr[0][1] and "Confirm login" in ui.qr[0][1] and "Tuya Smart or Smart Life" in ui.qr[0][1]
    assert home.cloud.login_checks == 2
    assert "Account and Security" in ui.text and "Home Assistant" in ui.text
    assert "case-sensitive" in ui.text and "same app" in ui.text and "case-sensitive" in ui.asked[0]

    config = home.store.load()
    by_name = {d.name: d for d in config.devices}
    assert set(by_name) == {
        "Desk lamp",
        "Hall bulb",
        "Heater plug",
        "Kitchen light",
        "Kitchen switch 2",
        "Bedroom curtain",
        "Living room AC",
        "Garden plug",
        "Porch light",
        "Ceiling fan",
        "AC cool 24",
        "AC off",
    }
    kinds = {name: d.kind for name, d in by_name.items()}
    assert kinds["Desk lamp"] == "light" and kinds["Heater plug"] == "plug" and kinds["Kitchen light"] == "switch"
    assert kinds["Bedroom curtain"] == "cover" and kinds["Living room AC"] == "climate"
    assert kinds["Ceiling fan"] == "fan" and kinds["AC cool 24"] == "scene"
    assert all(d.id.startswith("tuya-") and d.driver == "tuya" for d in config.devices)
    assert by_name["Desk lamp"].room == "Office" and by_name["Living room AC"].room == "Living room"

    lamp = by_name["Desk lamp"].settings
    assert lamp["tuya_id"] == "lamp-id" and lamp["category"] == "dj" and lamp["product"] == "Desk lamp product"
    assert lamp["address"] == "192.168.1.20" and lamp["version"] == "3.5"
    assert lamp["dps"]["bright_value_v2"] == {"dp": 22, "type": "int", "min": 10, "max": 1000}
    assert lamp["dps"]["colour_data_v2"]["format"] == "hsv16"
    assert "countdown" not in lamp["dps"]  # only the codes Jarvis uses are kept
    assert by_name["Hall bulb"].settings["dps"]["colour_data"]["format"] == "rgb8"
    curtain = by_name["Bedroom curtain"].settings
    assert curtain["dps"]["control"]["range"] == ["open", "stop", "close"]
    assert curtain["dps"]["control"]["raw"] == {"open": "0", "stop": "1", "close": "2"}
    assert curtain["dps"]["percent_state"]["ro"] is True and curtain["invert"] is True
    ac = by_name["Living room AC"].settings["dps"]
    assert ac["temp_set"] == {"dp": 2, "type": "int", "min": 160, "max": 300, "scale": 1, "step": 5, "unit": "℃"}
    assert "eco" not in ac  # not reported locally
    assert by_name["Kitchen light"].settings["power"] == "switch_1"
    assert by_name["Kitchen switch 2"].settings["dps"] == {"switch_2": {"dp": 2, "type": "bool"}}
    garden = by_name["Garden plug"].settings
    assert garden["parent"] == "hub-id" and garden["node_id"] == "a4c1-garden"
    assert garden["address"] == "192.168.1.26"
    assert "address" not in by_name["Porch light"].settings
    assert by_name["AC cool 24"].settings == {"home_id": "home-1", "scene_id": "scene-ac-cool"}

    # Keys live only in the credential store; sub-devices use their gateway's.
    for record in config.devices:
        secret = home.store.secret(record.secret_key)
        if record.kind == "scene":
            assert secret is None
        else:
            assert secret is not None and set(secret) == {"local_key"}
    assert home.store.secret(by_name["Garden plug"].secret_key) == {"local_key": KEYS["hub"]}
    assert home.store.secret(by_name["Desk lamp"].secret_key) == {"local_key": KEYS["lamp"]}
    saved_link = home.store.secret(CLOUD_SECRET)
    assert saved_link is not None and saved_link["user_code"] == "UC-TEST-0001"
    assert saved_link["token_info"]["refresh_token"] == "REFRESH-TOKEN-1" and saved_link["endpoint"].startswith("https")

    # The scan: one listen for every device (gateway for the sub-device), with the safe arguments.
    call = home.scanner.calls[0]
    assert call["poll"] is False and call["byID"] is True and call["verbose"] is False
    assert sorted(call["wantids"]) == sorted(
        ["lamp-id", "hall-id", "plug-id", "kitchen-id", "curtain-id", "ac-id", "hub-id", "porch-id", "fan-id"]
    )
    assert all(entry["name"] == "" and entry["key"] == "" for entry in call["tuyadevices"])

    assert "IR blaster (an IR remote" in ui.text and "Hall sensor (a sensor" in ui.text
    assert "AC cool 24" in ui.text and "Tap-to-Run scene" in ui.text
    assert "Not found on your home network: Porch light" in ui.text
    assert "Old scene" not in ui.text
    # The check after saving reads each reachable device once.
    assert "The Heater plug is on, drawing 120.5 watts." in ui.text
    assert "The Garden plug is off." in ui.text

    assert_no_secrets(home.config_text(), ui.text, *(r.getMessage() for r in caplog.records))


def test_qr_link_keeps_tokens_renewed_while_loading(home: Home) -> None:
    home.cloud.renew_on_load = dict(RENEWED)
    link(home)
    saved = home.store.secret(CLOUD_SECRET)
    assert saved is not None and saved["token_info"]["access_token"] == "ACCESS-TOKEN-2"


def test_qr_not_confirmed_in_time_saves_nothing(home: Home, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tuya, "LOGIN_WAIT_S", 0.05)
    monkeypatch.setattr(tuya, "LOGIN_POLL_S", 0.01)
    home.cloud.logins = deque()
    ui = ScriptedPrompter(["UC-TEST-0001"])
    tuya.wizard(ui, home.ctx)
    assert "not confirmed in time" in ui.text and "Nothing was saved" in ui.text
    assert home.cloud.login_checks >= 2
    assert home.store.load().devices == [] and home.store.secret_keys() == []


def test_an_expired_qr_code_stops_the_wait_and_offers_a_new_one(home: Home, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tuya, "LOGIN_WAIT_S", 30.0)  # a test that waited it out would show here
    home.cloud.logins = deque([(False, {"success": False, "code": "E0020002", "msg": "expired"}), (True, dict(LOGIN))])
    ui = ScriptedPrompter(["UC-TEST-0001", True, False, "Kitchen light", "", True, False])
    started = time.monotonic()
    tuya.wizard(ui, home.ctx)
    assert time.monotonic() - started < 10.0
    assert "The QR code expired" in ui.text and "Show a new QR code" in "\n".join(ui.asked)
    assert len(ui.qr) == 2 and len(home.cloud.qr_requests) == 2 and home.cloud.login_checks == 2
    assert home.device("Desk lamp").kind == "light"


def test_a_refused_login_says_to_use_the_same_app(home: Home, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tuya, "LOGIN_WAIT_S", 30.0)
    refused = {"success": False, "code": "E0020003", "msg": "Please use the designated APP to scan the code to log in"}
    home.cloud.logins = deque([(False, refused)])
    ui = ScriptedPrompter(["UC-TEST-0001", False])  # no new QR code
    tuya.wizard(ui, home.ctx)
    assert home.cloud.login_checks == 1 and len(ui.qr) == 1
    assert "The phone refused the login" in ui.text and "same app you took the User Code from" in ui.text
    assert "designated" not in ui.text and "Nothing was saved." in ui.text
    assert home.store.load().devices == [] and home.store.secret_keys() == []
    # Any other failure keeps the wait going, as before.
    home.cloud.logins = deque([(False, {"success": False, "code": "E9999999"}), (True, dict(LOGIN))])
    link(home)
    assert home.cloud.login_checks == 3


def test_ctrl_c_while_waiting_for_the_phone_saves_nothing(home: Home) -> None:
    home.cloud.logins = deque([(False, {}), KeyboardInterrupt()])
    ui = ScriptedPrompter(["UC-TEST-0001"])
    with pytest.raises(KeyboardInterrupt):
        tuya.wizard(ui, home.ctx)
    assert home.store.load().devices == [] and home.store.secret_keys() == []


def test_declining_the_save_saves_nothing(home: Home) -> None:
    ui = ScriptedPrompter(["UC-TEST-0001", None, "", "", False])
    tuya.wizard(ui, home.ctx)
    assert "Nothing was saved." in ui.text
    assert home.store.load().devices == [] and home.store.secret_keys() == []


def test_a_wrong_user_code_or_no_internet_is_explained(home: Home) -> None:
    home.cloud.qr_response = {"success": False, "code": 1004, "msg": "invalid"}
    ui = ScriptedPrompter(["UC-TEST-0001"])
    tuya.wizard(ui, home.ctx)
    assert "did not accept that User Code (error 1004)" in ui.text and not ui.qr

    def offline(*_args: Any) -> Any:
        raise ConnectionError("no route")

    offline_sharing = SimpleNamespace(LoginControl=lambda: SimpleNamespace(qr_code=offline))
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(tuya, "_sharing", lambda: offline_sharing)
        ui = ScriptedPrompter(["UC-TEST-0001"])
        tuya.wizard(ui, home.ctx)
    assert "could not reach Tuya" in ui.text and "Nothing was saved." in ui.text
    assert home.store.secret_keys() == []


def test_an_empty_user_code_stops(home: Home) -> None:
    ui = ScriptedPrompter([""])
    tuya.wizard(ui, home.ctx)
    assert "Nothing was saved." in ui.text and not home.cloud.qr_requests


def test_a_scan_that_cannot_listen_still_saves(home: Home) -> None:
    home.scanner.error = OSError(98, "Address already in use")
    ui = link(home, check=None)
    assert "could not listen for devices" in ui.text
    lamp = home.device("Desk lamp")
    assert "address" not in lamp.settings and home.store.secret(lamp.secret_key) == {"local_key": KEYS["lamp"]}


def test_refresh_without_qr_keeps_names_and_follows_smart_life(home: Home) -> None:
    link(home)
    lamp = home.device("Desk lamp")
    home.store.update(lambda c: setattr(c.device(lamp.id), "name", "Study lamp"))
    home.cloud.devices = [d for d in home.cloud.devices if d.id != "fan-id"]
    home.cloud.scenes.append(SimpleNamespace(scene_id="scene-tv", name="TV time", home_id="home-1"))
    home.scanner.answers["lamp-id"] = ("192.168.1.40", "3.5")
    home.cloud.renew_on_load = dict(RENEWED)

    ui = ScriptedPrompter(["Refresh", True, True, False])  # remove the fan, save, skip the check
    tuya.wizard(ui, home.ctx)

    assert not ui.qr and home.cloud.qr_requests == [(tuya.CLIENT_ID, tuya.SCHEMA, "UC-TEST-0001")]
    assert home.cloud.managers[-1]["token"]["access_token"] == "ACCESS-TOKEN-1"
    names = {d.name for d in home.store.load().devices}
    assert "Study lamp" in names and "Desk lamp" not in names and "Ceiling fan" not in names
    assert "TV time" in names and "Kitchen light" in names  # gang names are not asked again
    study = home.device("Study lamp")
    assert study.id == lamp.id and study.settings["address"] == "192.168.1.40"
    assert "No longer in your Tuya account: Ceiling fan" in "\n".join(ui.asked)
    assert all(not key.endswith("ceiling-fan") for key in home.store.secret_keys())
    saved = home.store.secret(CLOUD_SECRET)
    assert saved is not None and saved["token_info"]["refresh_token"] == "REFRESH-TOKEN-2"


def test_refresh_with_an_expired_link_says_so(home: Home) -> None:
    link(home)
    home.cloud.load_error = CloudError("1010", "token invalid")
    ui = ScriptedPrompter(["Refresh"])
    tuya.wizard(ui, home.ctx)
    assert "The Tuya link has expired" in ui.text and "Nothing was changed." in ui.text


def test_find_devices_again_and_reverse_a_curtain(home: Home) -> None:
    link(home)
    home.scanner.answers["porch-id"] = ("192.168.1.27", "3.3")
    ui = ScriptedPrompter(["Find my Tuya devices"])
    tuya.wizard(ui, home.ctx)
    assert home.device("Porch light").settings["address"] == "192.168.1.27"
    assert "Porch light: at 192.168.1.27" in ui.text

    ui = ScriptedPrompter(["Reverse a curtain", 0])
    tuya.wizard(ui, home.ctx)
    assert home.device("Bedroom curtain").settings["invert"] is False
    assert "other way round" in ui.text


# --------------------------------------------------------------------------- commands and their data points


def run(home: Home, name: str, command: str, value: Any = None) -> Outcome:
    return home.driver().run(home.device(name), command, value)


def command_names(home: Home, name: str) -> list[str]:
    return [spec.name for spec in home.driver().commands(home.device(name))]


def test_commands_come_from_the_saved_map(home: Home) -> None:
    link(home)
    assert command_names(home, "Desk lamp") == [
        "turn_on",
        "turn_off",
        "toggle",
        "set_brightness",
        "set_color",
        "set_color_temp",
    ]
    assert command_names(home, "Porch light") == ["turn_on", "turn_off", "toggle", "set_brightness"]
    assert command_names(home, "Heater plug") == ["turn_on", "turn_off", "toggle"]
    assert command_names(home, "Bedroom curtain") == ["open", "close", "stop", "set_position"]
    ac = {spec.name: spec for spec in home.driver().commands(home.device("Living room AC"))}
    assert list(ac) == ["turn_on", "turn_off", "toggle", "set_temperature", "set_mode", "set_fan_speed"]
    assert (ac["set_temperature"].low, ac["set_temperature"].high) == (16.0, 30.0)
    assert ac["set_mode"].choices == ("cold", "hot", "wet", "wind", "auto")
    fan = {spec.name: spec for spec in home.driver().commands(home.device("Ceiling fan"))}
    assert fan["set_fan_speed"].choices == ("1", "2", "3", "4") and "set_mode" in fan
    assert command_names(home, "AC cool 24") == ["activate"]


def test_power_on_off_and_toggle(home: Home) -> None:
    link(home)
    assert run(home, "Heater plug", "turn_off") == Outcome.done("The Heater plug is off.")
    assert home.net.writes("plug-id")[-1] == {"1": False}
    assert run(home, "Heater plug", "toggle").text == "The Heater plug is now on."
    assert home.net.calls[-2][1] == "status" and home.net.writes("plug-id")[-1] == {"1": True}
    # Each gang of the kitchen switch is its own device on the same connection.
    assert run(home, "Kitchen switch 2", "turn_off").text == "The Kitchen switch 2 is off."
    assert run(home, "Kitchen light", "turn_on").text == "The Kitchen light is on."
    assert home.net.writes("kitchen-id")[-2:] == [{"2": False}, {"1": True}]
    assert home.net.devices["kitchen-id"].dps == {"1": True, "2": False}


def test_brightness_is_scaled_to_each_lights_range(home: Home) -> None:
    link(home)
    home.net.calls.clear()
    assert run(home, "Desk lamp", "set_brightness", 50).text == "The Desk lamp is at 50% brightness."
    # A colour light is asked for its mode first; in white mode the white brightness changes.
    assert [op for _, op, _ in home.net.calls] == ["status", "set_multiple_values"]
    assert home.net.writes("lamp-id")[-1] == {"20": True, "22": 505}
    run(home, "Hall bulb", "set_brightness", 50)
    assert home.net.writes("hall-id")[-1] == {"1": True, "3": 140}  # 25-255
    home.scanner.answers["porch-id"] = ("192.168.1.27", "3.3")
    run(home, "Porch light", "set_brightness", 100)  # found on the network first
    assert home.net.writes("porch-id")[-1] == {"20": True, "22": 1000}
    assert run(home, "Desk lamp", "set_brightness", 0).text == "The Desk lamp is off."
    assert home.net.writes("lamp-id")[-1] == {"20": False}


def test_brightness_of_a_light_in_colour_mode_keeps_its_colour(home: Home) -> None:
    link(home)
    home.net.devices["lamp-id"].dps.update({"21": "colour", "24": "00f003e803e8"})  # blue, full
    run(home, "Desk lamp", "set_brightness", 40)
    assert home.net.writes("lamp-id")[-1] == {"20": True, "24": "00f003e80190"}  # v = 400


def test_colours_in_both_local_encodings(home: Home) -> None:
    link(home)
    # v2: 12 hex characters hhhhssssvvvv, h 0-360, s and v 0-1000; brightness kept from white mode (50 %).
    assert run(home, "Desk lamp", "set_color", "red").text == "The Desk lamp is red now."
    assert home.net.writes("lamp-id")[-1] == {"20": True, "21": "colour", "24": "000003e801f4"}
    # v1: 14 hex characters rrggbb0hhhssvv, s and v 0-255; #rrggbb carries its own brightness.
    assert run(home, "Hall bulb", "set_color", "#0000ff").text == "The Hall bulb is #0000ff now."
    assert home.net.writes("hall-id")[-1] == {"1": True, "2": "colour", "5": "0000ff00f0ffff"}
    run(home, "Hall bulb", "set_color", "#800000")
    assert home.net.writes("hall-id")[-1]["5"] == "8000000000ff80"
    # White is the white LEDs, not a colour.
    assert run(home, "Desk lamp", "set_color", "warm white").text == "The Desk lamp is warm white now."
    assert home.net.writes("lamp-id")[-1] == {"20": True, "21": "white", "23": 0}
    outcome = run(home, "Desk lamp", "set_color", "plaid")
    assert outcome.code == "bad_value" and "plaid" in outcome.text


def test_colour_temperature(home: Home) -> None:
    link(home)
    assert run(home, "Desk lamp", "set_color_temp", "cool").text == "The Desk lamp is cool white now."
    assert home.net.writes("lamp-id")[-1] == {"20": True, "21": "white", "23": 1000}
    run(home, "Desk lamp", "set_color_temp", "50%")
    assert home.net.writes("lamp-id")[-1]["23"] == 500
    run(home, "Hall bulb", "set_color_temp", "2700K")
    assert home.net.writes("hall-id")[-1] == {"1": True, "2": "white", "4": 0}
    run(home, "Hall bulb", "set_color_temp", "neutral")
    assert home.net.writes("hall-id")[-1]["4"] == 128  # 0-255
    assert run(home, "Hall bulb", "set_color_temp", "purple").code == "bad_value"


def test_curtain_uses_the_motors_own_words_and_counts_the_other_way(home: Home) -> None:
    link(home)
    assert run(home, "Bedroom curtain", "open").text == "The Bedroom curtain is opening."
    assert run(home, "Bedroom curtain", "stop").text == "The Bedroom curtain has stopped."
    assert run(home, "Bedroom curtain", "close").text == "The Bedroom curtain is closing."
    assert home.net.writes("curtain-id") == [{"1": "0"}, {"1": "1"}, {"1": "2"}]
    assert run(home, "Bedroom curtain", "set_position", 30).text == "The Bedroom curtain is moving to 30% open."
    assert home.net.writes("curtain-id")[-1] == {"2": 70}  # inverted, as Home Assistant does for "cl"
    home.net.devices["curtain-id"].dps["3"] = 70
    assert home.driver().status(home.device("Bedroom curtain")).text == "The Bedroom curtain is 30% open."


def test_a_curtain_robot_counts_the_other_way_too(home: Home) -> None:
    home.cloud.devices.append(cloud_device("robot-id", "Study curtain", "jdcljqr", KEYS["robot"], CURTAIN_POINTS))
    link(home)
    robot = home.device("Study curtain")
    assert robot.kind == "cover" and robot.settings["invert"] is True  # as Home Assistant does for "jdcljqr"


def test_climate_temperature_mode_and_fan(home: Home) -> None:
    link(home)
    assert run(home, "Living room AC", "set_temperature", 24.3).text == "The Living room AC is set to 24.5 degrees."
    assert home.net.writes("ac-id")[-1] == {"2": 245}  # scale 1, step 5
    run(home, "Living room AC", "set_temperature", 18)
    assert home.net.writes("ac-id")[-1] == {"2": 180}
    assert run(home, "Living room AC", "set_mode", "cold").text == "The Living room AC is in cold mode."
    assert run(home, "Living room AC", "set_fan_speed", "high").text == "The Living room AC fan speed is high."
    assert home.net.writes("ac-id")[-2:] == [{"4": "cold"}, {"5": "high"}]
    status = home.driver().status(home.device("Living room AC")).text
    assert status == "The Living room AC is on, in cold mode, set to 18 degrees. It is 26 degrees in the room."


def test_fan_speeds_and_modes(home: Home) -> None:
    link(home)
    assert run(home, "Ceiling fan", "set_fan_speed", "3").text == "The Ceiling fan fan speed is 3."
    assert run(home, "Ceiling fan", "set_mode", "sleep").text == "The Ceiling fan is in sleep mode."
    assert home.net.writes("fan-id")[-2:] == [{"3": "3"}, {"2": "sleep"}]
    assert home.driver().status(home.device("Ceiling fan")).text == "The Ceiling fan is on, at speed 3."


def test_status_in_words(home: Home) -> None:
    link(home)
    driver = home.driver()
    assert driver.status(home.device("Desk lamp")).text == "The Desk lamp is on, at 50% brightness, warm white."
    home.net.devices["lamp-id"].dps.update({"21": "colour", "24": "007803e80258"})
    assert driver.status(home.device("Desk lamp")).text == "The Desk lamp is on, green, at 60% brightness."
    assert driver.status(home.device("Hall bulb")).text == "The Hall bulb is off."
    assert driver.status(home.device("Kitchen switch 2")).text == "The Kitchen switch 2 is on."
    assert driver.status(home.device("AC cool 24")).code == "unsupported"


def write_record(home: Home, name: str, kind: str, category: str, points: list[dict[str, Any]], **settings: Any) -> str:
    """Saves a device straight into the store (for kinds the fake account does not have)."""
    spec = {p["code"]: {"type": p["type"], "values": json.dumps(p["desc"])} for p in points if not p.get("ro")}
    dps = tdp.build_dp_map(tdp.relations_from_dtos(dtos(points)), spec, {})
    record = DeviceRecord(
        f"tuya-{name.lower().replace(' ', '-')}",
        "tuya",
        name,
        kind,
        settings={
            "tuya_id": f"{name}-id",
            "category": category,
            "address": "192.168.1.50",
            "version": "3.3",
            "dps": dps,
            **settings,
        },
    )
    home.store.update(lambda c: c.upsert(record))
    home.store.set_secret(record.secret_key, {"local_key": "FAKE-KEY-OTHER-1"})
    home.net.add(Phys(f"{name}-id", "192.168.1.50", "FAKE-KEY-OTHER-1", 3.3, {}))
    return name


def test_vacuum_humidifier_heater_and_garage(home: Home) -> None:
    write_record(
        home,
        "Robot",
        "vacuum",
        "sd",
        [
            dp(1, "power_go", "Boolean"),
            dp(2, "pause", "Boolean"),
            dp(3, "switch_charge", "Boolean"),
            dp(5, "status", "Enum", {"range": ["standby", "smart_clean", "charging"]}, ro=True),
            dp(6, "electricity_left", "Integer", {"min": 0, "max": 100}, ro=True),
        ],
    )
    assert command_names(home, "Robot") == ["start", "stop", "pause", "dock"]
    assert run(home, "Robot", "start").text == "The Robot is starting to clean."
    assert run(home, "Robot", "pause").text == "The Robot has paused."
    assert run(home, "Robot", "stop").text == "The Robot has stopped."
    assert run(home, "Robot", "dock").text == "The Robot is going back to its dock."
    assert home.net.writes("Robot-id") == [{"1": True}, {"2": True}, {"1": False}, {"3": True}]
    home.net.devices["Robot-id"].dps.update({"5": "smart_clean", "6": 80})
    assert home.driver().status(home.device("Robot")).text == "The Robot reports smart clean; its battery is at 80%."

    write_record(
        home,
        "Dehumidifier",
        "humidifier",
        "cs",
        [
            dp(1, "switch", "Boolean"),
            dp(2, "dehumidify_set_value", "Integer", {"min": 35, "max": 70, "step": 5}),
            dp(4, "fan_speed_enum", "Enum", {"range": ["low", "high"]}),
        ],
    )
    spec = {s.name: s for s in home.driver().commands(home.device("Dehumidifier"))}
    assert (spec["set_humidity"].low, spec["set_humidity"].high) == (35.0, 70.0)
    assert run(home, "Dehumidifier", "set_humidity", 52).text == "The Dehumidifier is set to 50% humidity."

    write_record(
        home,
        "Panel heater",
        "heater",
        "qn",
        [dp(1, "switch", "Boolean"), dp(2, "temp_set", "Integer", {"min": 5, "max": 35, "unit": "℃"})],
    )
    assert run(home, "Panel heater", "set_temperature", 21).text == "The Panel heater is set to 21 degrees."
    assert home.net.writes("Panel heater-id")[-1] == {"2": 21}

    write_record(
        home,
        "Garage door",
        "garage",
        "ckmkzq",
        [dp(1, "switch_1", "Boolean"), dp(3, "doorcontact_state", "Boolean", ro=True)],
    )
    garage = {s.name: s for s in home.driver().commands(home.device("Garage door"))}
    assert garage["open"].tier == "screen" and garage["close"].tier == "free" and "set_position" not in garage
    assert run(home, "Garage door", "open").text == "The Garage door is opening."
    assert home.net.writes("Garage door-id") == [{"1": True}]


def test_requests_use_short_bounded_timeouts(home: Home) -> None:
    link(home)
    home.net.opened.clear()
    run(home, "Heater plug", "turn_on")  # v3.3, one value
    opened = home.net.opened[-1]
    assert opened["retry_limit"] >= 1 and opened["persist"] is False and opened["version"] == 3.3
    assert opened["timeout"] == tuya.T_MAX and 4 * opened["timeout"] <= tuya.RUN_BUDGET_S
    run(home, "Desk lamp", "set_color_temp", "warm")  # v3.5, several values at once
    timeout = home.net.opened[-1]["timeout"]
    assert home.net.opened[-1]["version"] == 3.5
    assert timeout >= tuya.T_MIN and 2 * 8 * timeout <= tuya.RUN_BUDGET_S


def test_a_sub_device_goes_through_its_gateway(home: Home) -> None:
    link(home)
    driver = home.driver()
    garden = home.device("Garden plug")
    assert driver.lock_key(garden) == "tuya:hub-id"
    assert driver.lock_key(home.device("Kitchen light")) == driver.lock_key(home.device("Kitchen switch 2"))
    assert driver.lock_key(home.device("Desk lamp")) == "tuya:lamp-id"
    home.net.opened.clear()
    assert driver.run(garden, "turn_on", None).text == "The Garden plug is on."
    gateway, child = home.net.opened[-2:]
    assert gateway == {**gateway, "id": "hub-id", "address": "192.168.1.26", "parent": None, "has_key": True}
    assert child["id"] == "garden-id" and child["parent"] == "hub-id" and child["cid"] == "a4c1-garden"
    assert home.net.devices["hub-id"].children["a4c1-garden"].dps["1"] is True


def test_a_sub_device_without_a_node_id_is_reached_by_its_uuid(home: Home) -> None:
    home.cloud.devices += [
        cloud_device("spot-id", "Hall spot", "dj", KEYS["hub"], PORCH_POINTS, sub=True, uuid="uuid-spot"),
        cloud_device("strip-id", "Hall strip", "dj", KEYS["hub"], PORCH_POINTS, sub=True),
    ]
    home.net.devices["hub-id"].children["uuid-spot"] = Phys("spot-id", "", KEYS["hub"], 3.3, {"20": False})
    ui = link(home)
    spot = home.device("Hall spot")
    assert spot.settings["parent"] == "hub-id" and spot.settings["node_id"] == "uuid-spot"
    # Without either, commands would reach only the hub: it is left out, saying why.
    assert "Hall strip (Tuya did not say how its hub addresses it)" in ui.text
    assert all(d.name != "Hall strip" for d in home.store.load().devices)
    home.net.opened.clear()
    assert home.driver().run(spot, "turn_on", None).text == "The Hall spot is on."
    assert home.net.opened[-1]["cid"] == "uuid-spot" and home.net.devices["hub-id"].children["uuid-spot"].dps["20"]
    # A saved sub-device that lost it is not sent to the hub at all.
    home.store.update(lambda c: c.device(spot.id).settings.pop("node_id"))
    home.net.opened.clear()
    outcome = home.driver().run(home.device("Hall spot"), "turn_on", None)
    assert outcome.code == "needs_setup" and not home.net.opened


def add_fingerbots(home: Home) -> None:
    """A Fingerbot behind the Zigbee hub's Bluetooth side (reachable), and one paired to the phone only."""
    home.cloud.devices += [
        cloud_device("finger-id", "Bedroom Fingerbot", "szjqr", KEYS["hub"], FINGER_POINTS, sub=True, node_id="f1"),
        cloud_device("ble-id", "Desk Fingerbot", "szjqr", "FAKE-KEY-BLE0-01", FINGER_POINTS),
    ]
    home.cloud.rooms["finger-id"] = "Bedroom"
    child = Phys("finger-id", "", KEYS["hub"], 3.3, {"1": False, "2": "click", "3": 2, "9": 80, "12": 80})
    home.net.devices["hub-id"].children["f1"] = child


def test_a_fingerbot_is_a_button_named_for_what_it_presses(home: Home) -> None:
    add_fingerbots(home)
    ui = ScriptedPrompter(["UC-TEST-0001", False, "Kitchen light", "", "bedroom light", True, False])
    tuya.wizard(ui, home.ctx)
    assert not ui.answers
    assert "What does the Bedroom Fingerbot switch on and off? For example: bedroom light (empty to skip)" in ui.asked
    finger = home.device("Bedroom Fingerbot")
    assert finger.kind == "button" and finger.aliases == ["bedroom light"] and finger.room == "Bedroom"
    assert finger.settings["parent"] == "hub-id" and finger.settings["node_id"] == "f1"
    assert {"switch", "mode", "click_sustain_time", "arm_down_percent", "battery_percentage"} <= set(
        finger.settings["dps"]
    )
    # Bluetooth only, with no gateway: nothing on the network reaches it, so it is left out, saying why.
    assert "Desk Fingerbot (a Bluetooth device, which Jarvis can reach only through a Tuya Bluetooth" in ui.text
    assert all(d.name != "Desk Fingerbot" for d in home.store.load().devices)
    assert "ble-id" not in home.scanner.calls[0]["wantids"]
    assert_no_secrets(home.config_text(), ui.text)
    # A refresh keeps the alias and does not ask again; an empty answer skips it.
    ui = ScriptedPrompter(["Refresh", True, False])
    tuya.wizard(ui, home.ctx)
    assert home.device("Bedroom Fingerbot").aliases == ["bedroom light"] and not ui.answers
    home.store.update(lambda c: c.remove(finger.id))
    ui = ScriptedPrompter(["Refresh", "", True, False])
    tuya.wizard(ui, home.ctx)
    assert home.device("Bedroom Fingerbot").aliases == [] and not ui.answers


def test_a_button_presses_and_never_claims_the_light_is_on(home: Home) -> None:
    add_fingerbots(home)
    tuya.wizard(ScriptedPrompter(["UC-TEST-0001", False, "Kitchen light", "", "bedroom light", True, False]), home.ctx)
    finger = home.device("Bedroom Fingerbot")
    child = home.net.devices["hub-id"].children["f1"]
    assert command_names(home, "Bedroom Fingerbot") == ["press"]
    (spec,) = home.driver().commands(finger)
    assert "cannot see the light" in spec.hint
    pressed = (
        "The Bedroom Fingerbot pressed the bedroom light switch. Jarvis cannot see the light, so it cannot tell "
        "whether it is now on or off."
    )
    # Each press writes the opposite of what the switch reports, so the finger always sees a change.
    assert run(home, "Bedroom Fingerbot", "press") == Outcome.done(pressed)
    assert run(home, "Bedroom Fingerbot", "press") == Outcome.done(pressed)
    assert [payload for device, op, payload in home.net.calls if op != "status"][-2:] == [{"1": True}, {"1": False}]
    # No value to read, or a read that times out: it presses with True.
    del child.dps["1"]
    assert run(home, "Bedroom Fingerbot", "press").ok and child.dps["1"] is True
    child.dps["1"] = True
    home.net.devices["hub-id"].errors = ["902"]
    assert run(home, "Bedroom Fingerbot", "press").ok and child.dps["1"] is True
    # A hub that cannot be reached at all is not sent a press that cannot arrive either.
    writes = len(home.net.writes("finger-id"))
    home.net.devices["hub-id"].errors = ["905"]
    assert run(home, "Bedroom Fingerbot", "press").code == "unreachable"
    assert len(home.net.writes("finger-id")) == writes
    assert home.driver().run(finger, "turn_on", None).code == "unsupported"
    status = home.driver().status(finger).text
    assert status == (
        "The Bedroom Fingerbot presses the bedroom light switch. It is in click mode. Its battery is at 80%. "
        "Jarvis cannot see whether the light is on."
    )
    # Through the service: the light's name finds the button, and "click" means press.
    service = HomeService(home.ctx.data_dir, store=home.store, drivers={"tuya": TuyaDriver}, call_timeout=20.0)
    try:
        answer = service.handle({"action": "do", "device": "bedroom light", "command": "click"})
        assert answer["result"] == "done" and answer["text"] == pressed
        answer = service.handle({"action": "do", "device": "the clicker", "command": "push"})
        assert answer["result"] == "done" and " is on" not in answer["text"]
        listed = service.handle({"action": "list"})["text"]
        assert "Bedroom Fingerbot (button, Bedroom)" in listed
    finally:
        service.close()


def test_a_device_that_was_not_found_is_looked_for_again(home: Home) -> None:
    link(home)
    home.scanner.calls.clear()
    outcome = run(home, "Porch light", "turn_on")
    assert outcome.code == "unreachable" and "not been found" in outcome.text
    home.scanner.answers["porch-id"] = ("192.168.1.27", "3.3")
    driver = home.driver()
    assert driver.run(home.device("Porch light"), "turn_on", None).text == "The Porch light is on."
    assert home.scanner.calls[-1]["wantids"] == ["porch-id"]
    assert home.device("Porch light").settings["address"] == "192.168.1.27"
    # A device that stops answering is looked for once, then not again straight away.
    del home.scanner.answers["porch-id"]
    home.net.devices["porch-id"].address = "192.168.1.99"
    driver, scans = home.driver(), len(home.scanner.calls)
    assert driver.run(home.device("Porch light"), "turn_off", None).code == "unreachable"
    assert driver.run(home.device("Porch light"), "turn_off", None).code == "unreachable"
    assert len(home.scanner.calls) == scans + 1


def forget_address(home: Home, name: str) -> None:
    def mutate(config: Any) -> None:
        for key in ("address", "version"):
            config.device(home.device(name).id).settings.pop(key, None)

    home.store.update(mutate)


def test_a_rescan_leaves_room_for_the_retry(home: Home) -> None:
    link(home)
    home.scanner.answers["porch-id"] = ("192.168.1.27", "3.3")
    one = 8 * tuya.T_MIN + tuya.UNIT_SLACK_S  # one request of a device whose version is not known yet
    assert home.driver().run(home.device("Porch light"), "turn_on", None).text == "The Porch light is on."
    assert home.scanner.calls[-1]["scantime"] == tuya.RESCAN_S  # one request leaves room for a full scan
    # A toggle reads and then writes: the scan is cut so both still fit afterwards.
    forget_address(home, "Porch light")
    assert home.driver().run(home.device("Porch light"), "toggle", None).text == "The Porch light is now off."
    scantime = home.scanner.calls[-1]["scantime"]
    # Exactly the budget when no time has passed (Windows' clock often reads none): compare with a float margin.
    assert tuya.RESCAN_MIN_S <= scantime < tuya.RESCAN_S and scantime + 2 * one + 1.0 <= tuya.RUN_BUDGET_S + 1e-9
    # With less time, a toggle does not scan at all; a single command still does, within what is left.
    forget_address(home, "Porch light")
    home.ctx.call_timeout = 14.0
    scans = len(home.scanner.calls)
    assert home.driver().run(home.device("Porch light"), "toggle", None).code == "unreachable"
    assert len(home.scanner.calls) == scans
    assert home.driver().run(home.device("Porch light"), "turn_on", None).text == "The Porch light is on."
    assert home.scanner.calls[-1]["scantime"] + one + 1.0 <= 14.0 - tuya.CALL_MARGIN_S


def test_a_scan_that_could_not_listen_does_not_hold_back_the_next(home: Home) -> None:
    link(home)
    driver = home.driver()
    # The setup console's own scan holds the UDP ports (Windows does not share them).
    home.scanner.error = OSError(10048, "Only one usage of each socket address is normally permitted")
    assert driver.run(home.device("Porch light"), "turn_on", None).code == "unreachable"
    home.scanner.error = None
    home.scanner.answers["porch-id"] = ("192.168.1.27", "3.3")
    assert driver.run(home.device("Porch light"), "turn_on", None).text == "The Porch light is on."


@pytest.mark.parametrize(
    ("err", "code", "words"),
    [
        ("901", "unreachable", "Is it powered and on the home network?"),
        ("905", "unreachable", "Is it powered and on the home network?"),
        ("902", "timeout", "busy with another app"),
        ("914", "auth", "refresh your Tuya devices in home setup"),
        ("904", "auth", "busy with another app, so try again in a moment"),
        ("903", "bad_value", "refused that value"),
        ("906", "failed", "Controlling the Heater plug failed."),
    ],
)
def test_tinytuya_errors_become_plain_words(
    home: Home, caplog: pytest.LogCaptureFixture, err: str, code: str, words: str
) -> None:
    link(home)
    caplog.set_level(logging.DEBUG)
    home.net.devices["plug-id"].errors = [err, err]
    outcome = run(home, "Heater plug", "turn_on")
    assert outcome.code == code and words in outcome.text
    assert "Err" not in outcome.text and "Payload" not in outcome.text and "192.168" not in outcome.text
    assert_no_secrets(outcome.text, *(r.getMessage() for r in caplog.records))


def test_a_key_error_first_looks_for_a_new_version(home: Home) -> None:
    link(home)
    # The plug's firmware moved to 3.4: the saved 3.3 now fails like a wrong key.
    home.net.devices["plug-id"].version = 3.4
    home.scanner.answers["plug-id"] = ("192.168.1.22", "3.4")
    assert run(home, "Heater plug", "turn_off").text == "The Heater plug is off."
    assert home.device("Heater plug").settings["version"] == "3.4"
    # A truly changed key still fails, saying what to do.
    home.net.devices["plug-id"].key = "FAKE-KEY-NEW0-01"
    outcome = home.driver().run(home.device("Heater plug"), "turn_on", None)
    assert outcome.code == "auth" and "its key may have changed" in outcome.text


def test_a_device22_reply_is_sent_again_on_the_same_connection(home: Home) -> None:
    link(home)
    home.net.devices["plug-id"].errors = ["908"]
    opened = len(home.net.opened)
    assert run(home, "Heater plug", "turn_off").text == "The Heater plug is off."
    assert len(home.net.opened) == opened + 1 and home.net.writes("plug-id")[-2:] == [{"1": False}, {"1": False}]


def test_a_device22_is_asked_for_every_value_jarvis_uses(home: Home) -> None:
    write_record(home, "Old bulb", "light", "dj", PORCH_POINTS)
    bulb = home.net.devices["Old bulb-id"]
    bulb.dps, bulb.device22 = {"20": False, "22": 505}, True  # power is DP 20, not DP 1
    assert run(home, "Old bulb", "toggle").text == "The Old bulb is now on."
    assert home.net.writes("Old bulb-id") == [{"20": True}]
    home.net.calls.clear()
    assert home.driver().status(home.device("Old bulb")).text == "The Old bulb is on, at 50% brightness."
    # tinytuya's own second query (DP 1 only) after the first reply, then Jarvis's for its DPs.
    assert [op for _, op, _ in home.net.calls] == ["status", "status", "status"]


def test_missing_key_or_settings_need_setup(home: Home) -> None:
    link(home)
    lamp = home.device("Desk lamp")
    home.store.set_secret(lamp.secret_key, None)
    outcome = home.driver().run(lamp, "turn_on", None)
    assert outcome.code == "needs_setup" and "refresh your Tuya devices" in outcome.text
    assert "no key saved for Desk lamp" in (home.driver().describe() or "")
    bare = DeviceRecord("tuya-bare", "tuya", "Bare", "plug", settings={})
    assert home.driver().run(bare, "turn_on", None).code == "unsupported"
    assert home.driver().status(bare).code == "needs_setup"


# --------------------------------------------------------------------------- scenes


def test_scenes_run_in_the_cloud_and_keep_renewed_tokens(home: Home) -> None:
    link(home)
    home.cloud.renew_on_trigger = dict(RENEWED)
    assert run(home, "AC cool 24", "activate").text == "Ran AC cool 24."
    assert home.cloud.triggered == [("home-1", "scene-ac-cool")]
    assert home.cloud.managers[-1]["client_id"] == tuya.CLIENT_ID
    saved = home.store.secret(CLOUD_SECRET)
    assert saved is not None and saved["token_info"]["access_token"] == "ACCESS-TOKEN-2"


def test_an_expired_link_or_no_internet_is_said_plainly(home: Home, caplog: pytest.LogCaptureFixture) -> None:
    link(home)
    caplog.set_level(logging.DEBUG)
    home.cloud.trigger_error = CloudError("1010", "token invalid ACCESS-TOKEN-1")
    outcome = run(home, "AC off", "activate")
    assert outcome == Outcome.fail("auth", tuya.EXPIRED)
    home.cloud.trigger_error = CloudError("2008", "scene not found")
    assert run(home, "AC off", "activate") == Outcome.fail("failed", "Tuya could not run AC off.")
    home.cloud.trigger_error = ConnectionError("network down")
    assert run(home, "AC off", "activate").code == "unreachable"
    home.store.set_secret(CLOUD_SECRET, None)
    assert run(home, "AC off", "activate") == Outcome.fail("needs_setup", tuya.NOT_LINKED)
    assert "scenes cannot run" in (home.driver().describe() or "")
    assert_no_secrets(*(r.getMessage() for r in caplog.records))


def test_a_scene_that_hangs_is_answered_within_the_time_limit(home: Home) -> None:
    link(home)
    home.ctx.call_timeout = tuya.CALL_MARGIN_S + 1.0  # one second for the Tuya cloud
    home.cloud.trigger_hangs = threading.Event()
    try:
        started = time.monotonic()
        outcome = run(home, "AC off", "activate")
        assert time.monotonic() - started < 3.0
    finally:
        home.cloud.trigger_hangs.set()
    assert outcome == Outcome.fail("timeout", "Tuya did not answer in time; AC off may still run.")


def scene_tier(name: str, *aliases: str) -> str:
    record = DeviceRecord(
        "tuya-scene", "tuya", name, "scene", aliases=list(aliases), settings={"home_id": "h", "scene_id": "s"}
    )
    (spec,) = tdp.command_specs(record)
    return spec.tier


def test_scenes_that_open_up_the_house_ask_on_screen_first(home: Home) -> None:
    gated = ["Open garage", "Garage", "Front gate", "Garage door 2", "Unlock front door", "Open the front door"]
    gated += ["Disarm alarm", "פתח שער", "שער", "פתיחת חניה", "פתח את הדלת"]
    assert {name: scene_tier(name) for name in gated} == dict.fromkeys(gated, "screen")
    free = ["AC cool 24", "Close garage", "Shut the gate", "Garage lights on", "Lock front door", "Door light"]
    free += ["סגור שער", "אורות מוסך", "מזגן 24"]
    assert {name: scene_tier(name) for name in free} == dict.fromkeys(free, "free")
    assert scene_tier("Car", "open garage") == "screen"

    home.cloud.scenes.append(SimpleNamespace(scene_id="scene-garage", name="Open garage", home_id="home-1"))
    link(home)
    service = HomeService(home.ctx.data_dir, store=home.store, drivers={"tuya": TuyaDriver}, call_timeout=20.0)
    try:
        answer = service.handle({"action": "do", "device": "open garage", "command": "run"})
        assert answer["result"] == "confirm" and home.cloud.triggered == []
        confirmed = {"action": "do", "device": answer["device"]["id"], "command": "run", "confirmed": True}
        answer = service.handle(confirmed)
        assert answer["text"] == "Ran Open garage." and home.cloud.triggered == [("home-1", "scene-garage")]
    finally:
        service.close()


# --------------------------------------------------------------------------- through the service


def test_through_the_home_service(home: Home, caplog: pytest.LogCaptureFixture) -> None:
    link(home)
    caplog.set_level(logging.DEBUG)
    service = HomeService(home.ctx.data_dir, store=home.store, drivers={"tuya": TuyaDriver}, call_timeout=20.0)
    try:
        listed = service.handle({"action": "list"})
        assert (
            "Desk lamp (light, Office)" in listed["text"] and "set_color <a colour name or #rrggbb>" in listed["text"]
        )
        answer = service.handle({"action": "do", "device": "desk lamp", "command": "dim", "value": "30%"})
        assert answer["result"] == "done" and answer["text"] == "The Desk lamp is at 30% brightness."
        answer = service.handle({"action": "do", "device": "bedroom curtain", "command": "position", "value": "half"})
        assert answer["text"] == "The Bedroom curtain is moving to 50% open."
        answer = service.handle({"action": "do", "device": "AC cool 24", "command": "run"})
        assert answer["text"] == "Ran AC cool 24."
        assert service.handle({"action": "status", "device": "heater plug"})["text"].startswith("The Heater plug is")
        texts = [listed["text"], answer["text"]]
    finally:
        service.close()
    assert_no_secrets(*texts, *(r.getMessage() for r in caplog.records))


# --------------------------------------------------------------------------- the cloud fallback (opt-in)

# As LINK_ANSWERS, with yes to the cloud fallback.
CLOUD_ANSWERS: list[Any] = ["UC-TEST-0001", True, "Kitchen light", "", True]


def link_with_cloud(home: Home) -> ScriptedPrompter:
    ui = ScriptedPrompter([*CLOUD_ANSWERS, False])
    tuya.wizard(ui, home.ctx)
    assert not ui.answers, f"unused answers: {list(ui.answers)}"
    return ui


def saved_link(home: Home) -> dict[str, Any]:
    saved = home.store.secret(CLOUD_SECRET)
    assert saved is not None
    return saved


def test_the_cloud_fallback_is_off_unless_chosen(home: Home) -> None:
    ui = link(home)
    assert ui.asked.count(tuya.CLOUD_QUESTION) == 1 and saved_link(home)[tuya.FALLBACK] is False
    # Exactly as without it: the in-command search, the full time for local requests, nothing to the cloud.
    home.net.devices["plug-id"].errors = ["902"]
    assert run(home, "Heater plug", "turn_on").code == "timeout"
    outcome = run(home, "Porch light", "turn_on")
    assert outcome.code == "unreachable" and "cloud" not in outcome.text
    assert home.scanner.calls[-1]["wantids"] == ["porch-id"]
    home.net.opened.clear()
    run(home, "Desk lamp", "set_color_temp", "warm")  # v3.5, two requests' worth
    assert home.net.opened[-1]["timeout"] == pytest.approx((tuya.RUN_BUDGET_S - 2 * tuya.UNIT_SLACK_S) / 16, abs=0.01)
    # A link saved before the choice existed counts as no.
    old = saved_link(home)
    del old[tuya.FALLBACK]
    home.store.set_secret(CLOUD_SECRET, old)
    assert run(home, "Porch light", "turn_on").code == "unreachable"
    assert home.cloud.requests == [] and home.cloud.commands == []
    # On, local requests leave the cloud its share of the time.
    home.store.set_secret(CLOUD_SECRET, {**old, tuya.FALLBACK: True})
    home.net.opened.clear()
    run(home, "Desk lamp", "set_color_temp", "warm")
    local_s = tuya.RUN_BUDGET_S - tuya.CLOUD_SLICE_S
    assert home.net.opened[-1]["timeout"] == pytest.approx((local_s - 2 * tuya.UNIT_SLACK_S) / 16, abs=0.01)


def test_the_link_asks_once_and_the_menu_changes_the_choice(home: Home) -> None:
    ui = link_with_cloud(home)
    assert ui.asked.count(tuya.CLOUD_QUESTION) == 1 and "Bluetooth device with no Tuya gateway" in ui.text
    assert saved_link(home)[tuya.FALLBACK] is True and tuya.FALLBACK not in home.config_text()
    run(home, "AC cool 24", "activate")  # the helper's cloud session now holds the link as it was
    ui = ScriptedPrompter(["does not answer on the network (now on)", False])
    tuya.wizard(ui, home.ctx)
    assert saved_link(home)[tuya.FALLBACK] is False and "no longer uses Tuya's cloud" in ui.text
    # A token renewal by that session keeps the new choice.
    home.cloud.renew_on_trigger = dict(RENEWED)
    run(home, "AC cool 24", "activate")
    assert saved_link(home)[tuya.FALLBACK] is False
    assert saved_link(home)["token_info"]["refresh_token"] == "REFRESH-TOKEN-2"
    tuya.wizard(ScriptedPrompter(["(now off)", True]), home.ctx)
    assert saved_link(home)[tuya.FALLBACK] is True


def test_a_device_not_found_goes_through_the_cloud_and_is_looked_for_meanwhile(home: Home) -> None:
    link_with_cloud(home)
    managers = len(home.cloud.managers)
    home.scanner.answers["porch-id"] = ("192.168.1.27", "3.3")
    driver = home.driver()
    outcome = driver.run(home.device("Porch light"), "turn_on", None)
    assert outcome == Outcome.done("The Porch light is on, through Tuya's cloud.")
    assert home.cloud.commands == [("porch-id", [{"code": "switch_led", "value": True}])]
    assert home.net.writes("porch-id") == []
    assert driver._background is not None
    driver._background.join(5)
    assert home.device("Porch light").settings["address"] == "192.168.1.27"
    # Found in the background: the next command is local.
    assert driver.run(home.device("Porch light"), "turn_off", None) == Outcome.done("The Porch light is off.")
    assert home.net.writes("porch-id") == [{"20": False}] and len(home.cloud.commands) == 1
    assert len(home.cloud.managers) == managers + 1  # one session for the helper's cloud calls


def test_a_toggle_that_timed_out_locally_is_finished_once_through_the_cloud(home: Home) -> None:
    link_with_cloud(home)
    home.net.devices["plug-id"].write_errors = ["902"]
    outcome = run(home, "Heater plug", "toggle")
    assert outcome == Outcome.done("The Heater plug is now off, through Tuya's cloud.")
    # It read on=True locally: the cloud gets the absolute value the local write meant, once, without a read.
    assert home.net.writes("plug-id") == [{"1": False}]
    assert home.cloud.commands == [("plug-id", [{"code": "switch_1", "value": False}])]
    assert [method for method, _ in home.cloud.requests] == ["POST"]


def test_a_press_is_never_sent_twice(home: Home) -> None:
    add_fingerbots(home)
    ui = ScriptedPrompter(["UC-TEST-0001", True, "Kitchen light", "", "bedroom light", True, False])
    tuya.wizard(ui, home.ctx)
    # Even with the cloud on, a Bluetooth device with no gateway is left out: the cloud cannot reach it either.
    assert "Desk Fingerbot (a Bluetooth device" in ui.text
    hub = home.net.devices["hub-id"]
    hub.write_errors = ["902"]
    outcome = run(home, "Bedroom Fingerbot", "press")
    assert outcome.ok and outcome.text.startswith(
        "The Bedroom Fingerbot pressed the bedroom light switch, through Tuya's cloud. Jarvis cannot see"
    )
    assert home.cloud.commands == [("finger-id", [{"code": "switch", "value": True}])]
    # The local read failed too: the press it tried (True) is the one the cloud sends, not a fresh opposite.
    hub.children["f1"].dps["1"] = True
    hub.errors, hub.write_errors = ["902"], ["902"]
    assert run(home, "Bedroom Fingerbot", "press").ok
    assert home.cloud.commands[-1] == ("finger-id", [{"code": "switch", "value": True}])
    assert [method for method, _ in home.cloud.requests] == ["POST", "POST"]


def test_a_refused_value_or_command_does_not_go_to_the_cloud(home: Home) -> None:
    link_with_cloud(home)
    home.net.devices["plug-id"].write_errors = ["903"]
    assert run(home, "Heater plug", "turn_on").code == "bad_value"
    assert run(home, "Heater plug", "set_color", "red").code == "unsupported"
    assert home.cloud.requests == []


def test_cloud_failures_are_said_plainly(home: Home, caplog: pytest.LogCaptureFixture) -> None:
    link_with_cloud(home)
    caplog.set_level(logging.DEBUG)
    clock = FakeClock()
    tuya._CLOUD.clock = clock
    not_found = "The Porch light has not been found on the home network."
    texts = []

    def porch_on(answer: Any) -> Outcome:
        home.cloud.answers.append(answer)
        outcome = run(home, "Porch light", "turn_on")
        texts.append(outcome.text)
        return outcome

    outcome = porch_on(None)  # an HTTP error: the SDK logs it and returns None
    assert outcome.code == "unreachable" and outcome.text.startswith(not_found)
    assert outcome.text.endswith(" Tuya's cloud could not reach it either.")
    assert porch_on(CloudError("-9999999", "sign invalid")).text.endswith(tuya.SIGNIN_REFUSED)
    assert porch_on(CloudError("1010", "token invalid")).text.endswith(tuya.EXPIRED)
    # requests' errors carry their URL, and the token renewal's URL carries the refresh token.
    refused = ConnectionError("GET https://apigw.example.invalid/v1.0/m/token/REFRESH-TOKEN-1 failed")
    assert porch_on(refused).text.endswith("could not reach it either.")
    # Tuya says it is limiting requests: no cloud calls for a minute, scenes included.
    assert porch_on(CloudError("-9999999", "API_QPS_LIMIT_OR_DEGRADE")).text.endswith(tuya.CLOUD_BUSY)
    sent = len(home.cloud.requests)
    clock.now += tuya.CLOUD_PAUSE_S - 1
    outcome = run(home, "Porch light", "turn_on")
    assert outcome.code == "unreachable" and "cloud" not in outcome.text  # as without the cloud
    assert run(home, "AC off", "activate") == Outcome.fail("busy", tuya.CLOUD_BUSY)
    assert len(home.cloud.requests) == sent and home.cloud.triggered == []
    clock.now += 2
    assert run(home, "Porch light", "turn_on").text == "The Porch light is on, through Tuya's cloud."
    # The cloud says the device is offline: its last state is not spoken as if current.
    home.cloud.offline.add("porch-id")
    home.cloud.state["porch-id"] = {"switch_led": True}
    outcome = home.driver().status(home.device("Porch light"))
    assert outcome == Outcome.fail("unreachable", "Tuya's cloud says the Porch light is offline.")
    messages = [r.getMessage() for r in caplog.records]
    assert_no_secrets(*texts, *messages)
    assert not any("apigw" in m or "sign invalid" in m or "network error" in m for m in messages)
    assert any("porch-id" in m and "-9999999" in m for m in messages)


def test_a_device_without_a_key_goes_through_the_cloud(home: Home, monkeypatch: pytest.MonkeyPatch) -> None:
    link_with_cloud(home)
    plug = home.device("Heater plug")
    home.store.set_secret(plug.secret_key, None)
    # Only the cloud reaches it now, so its answer does not say so each time.
    assert run(home, "Heater plug", "turn_off") == Outcome.done("The Heater plug is off.")
    assert home.cloud.commands == [("plug-id", [{"code": "switch_1", "value": False}])]
    home.cloud.state["plug-id"].update({"cur_power": 1205})
    assert home.driver().status(plug).text == "The Heater plug is off."
    # A command sent that gets no answer in time may still arrive.
    monkeypatch.setattr(tuya, "CLOUD_MIN_S", 0.1)
    home.ctx.call_timeout = tuya.CALL_MARGIN_S + 1.0
    home.cloud.hang = threading.Event()
    try:
        started = time.monotonic()
        outcome = run(home, "Heater plug", "turn_on")
        assert time.monotonic() - started < 3.0
    finally:
        home.cloud.hang.set()
    assert outcome == Outcome.fail("timeout", "Tuya's cloud did not answer in time; the Heater plug may still change.")


def test_colours_and_states_through_the_cloud(home: Home) -> None:
    link_with_cloud(home)
    forget_address(home, "Desk lamp")
    forget_address(home, "Hall bulb")
    home.scanner.answers = {}  # not found in the background either
    home.cloud.state["lamp-id"] = {"switch_led": True, "work_mode": "white", "bright_value_v2": 505}
    assert run(home, "Desk lamp", "set_color", "red").text == "The Desk lamp is red now, through Tuya's cloud."
    red = json.dumps({"h": 0, "s": 1000, "v": 500})  # the brightness it read through the cloud: 50 %
    assert home.cloud.commands[-1] == (
        "lamp-id",
        [
            {"code": "switch_led", "value": True},
            {"code": "work_mode", "value": "colour"},
            {"code": "colour_data_v2", "value": red},
        ],
    )
    home.cloud.state["lamp-id"]["colour_data_v2"] = json.dumps({"h": 120, "s": 1000, "v": 600})
    status = home.driver().status(home.device("Desk lamp")).text
    assert status == "The Desk lamp is on, green, at 60% brightness, through Tuya's cloud."
    # A v1 colour: s and v up to 255.
    run(home, "Hall bulb", "set_color", "#0000ff")
    assert home.cloud.commands[-1][1][-1] == {
        "code": "colour_data",
        "value": json.dumps({"h": 240, "s": 255, "v": 255}),
    }
    # Saved before the map kept the cloud's form of a colour: on and off still work, colours say to refresh.
    lamp = home.device("Desk lamp")
    home.store.update(lambda c: c.device(lamp.id).settings["dps"]["colour_data_v2"].pop("cloud"))
    sent = len(home.cloud.commands)
    outcome = run(home, "Desk lamp", "set_color", "blue")
    assert outcome.text.startswith("The Desk lamp has not been found") and "refresh your Tuya devices" in outcome.text
    assert len(home.cloud.commands) == sent
    assert run(home, "Desk lamp", "turn_off").text == "The Desk lamp is off, through Tuya's cloud."


def test_a_dead_address_still_leaves_the_cloud_its_time(home: Home, monkeypatch: pytest.MonkeyPatch) -> None:
    link_with_cloud(home)
    clock = FakeClock()
    monkeypatch.setattr(tuya, "time", SimpleNamespace(monotonic=clock, sleep=time.sleep))
    tuya._CLOUD.clock = clock
    home.net.clock, home.cloud.clock, home.cloud.seconds = clock, clock, 1.5
    # A v3.4 device (the slowest worst case) whose address is dead and that no scan finds.
    home.net.devices["kitchen-id"].address = "192.168.1.99"
    del home.scanner.answers["kitchen-id"]
    home.cloud.state["kitchen-id"] = {"switch_1": False, "switch_2": True}
    for command, text in (
        ("turn_on", "The Kitchen light is on, through Tuya's cloud."),
        ("toggle", "The Kitchen light is now off, through Tuya's cloud."),
    ):
        started = clock.now
        assert run(home, "Kitchen light", command).text == text
        assert clock.now - started <= tuya.RUN_BUDGET_S
    assert home.cloud.commands[-1] == ("kitchen-id", [{"code": "switch_1", "value": False}])


def test_with_the_cloud_on_devices_jarvis_cannot_reach_locally_are_kept(home: Home) -> None:
    home.cloud.devices += [
        cloud_device("shared-id", "Shared plug", "cz", "", GARDEN_POINTS, online=False),
        cloud_device("lost-id", "Lost bulb", "dj", KEYS["lost"], PORCH_POINTS, sub=True, node_id="n9"),
    ]
    ui = link_with_cloud(home)
    shared, lost = home.device("Shared plug"), home.device("Lost bulb")
    assert shared.settings["cloud_only"] is True and "address" not in shared.settings
    assert lost.settings["cloud_only"] is True and "parent" not in lost.settings and "node_id" not in lost.settings
    assert home.store.secret(shared.secret_key) is None and home.store.secret(lost.secret_key) is None
    assert "Shared plug (plug): through Tuya's cloud only (Tuya's cloud says it is offline;" in ui.text
    assert "Lost bulb (light): through Tuya's cloud only" in ui.text
    assert not {"shared-id", "lost-id"} & set(home.scanner.calls[0]["wantids"])
    assert run(home, "Lost bulb", "turn_on") == Outcome.done("The Lost bulb is on.")
    assert home.cloud.commands[-1] == ("lost-id", [{"code": "switch_led", "value": True}])
    assert "Shared plug, Lost bulb work only through Tuya's cloud;" in (home.driver().describe() or "")
    assert_no_secrets(home.config_text(), ui.text)
    # Turned off in setup, they say how to turn it on again.
    ui = ScriptedPrompter(["(now on)", False])
    tuya.wizard(ui, home.ctx)
    assert "Shared plug, Lost bulb work only through it" in ui.text
    outcome = run(home, "Lost bulb", "turn_on")
    assert outcome.code == "needs_setup" and "Turn it on in home setup" in outcome.text
    assert "which is turned off" in (home.driver().describe() or "")


# --------------------------------------------------------------------------- the real libraries' interfaces


def test_the_smart_life_sdk_waits_seconds_not_a_minute(monkeypatch: pytest.MonkeyPatch) -> None:
    customerapi = pytest.importorskip("tuya_sharing.customerapi")
    user = pytest.importorskip("tuya_sharing.user")
    # Both modules bound the SDK's 60 s constant at import; each binding is what its requests use.
    monkeypatch.setattr(customerapi, "DEFAULT_TIMEOUT", customerapi.DEFAULT_TIMEOUT)
    monkeypatch.setattr(user, "DEFAULT_TIMEOUT", user.DEFAULT_TIMEOUT)
    sharing = tuya._sharing()
    assert hasattr(sharing, "LoginControl") and hasattr(sharing, "Manager")
    assert customerapi.DEFAULT_TIMEOUT == user.DEFAULT_TIMEOUT == tuya.CLOUD_TIMEOUT_S
    assert logging.getLogger("tuya_sharing").getEffectiveLevel() > logging.INFO


def test_the_scanner_takes_the_arguments_jarvis_passes() -> None:
    # Read, not imported: importing the scanner starts colorama, which tests should not do.
    spec = importlib.util.find_spec("tinytuya")
    assert spec is not None and spec.origin is not None
    tree = ast.parse((Path(spec.origin).parent / "scanner.py").read_text(encoding="utf-8"))
    devices = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "devices")
    params = {a.arg for a in devices.args.args}
    assert {"verbose", "scantime", "color", "poll", "byID", "wantids", "tuyadevices"} <= params


# --------------------------------------------------------------------------- data points on their own


def test_dp_map_from_dtos_skips_cloud_only_points_and_maps_vendor_words() -> None:
    dtos = [
        {
            "dpId": 1,
            "statusCode": "control",
            "valueType": "Enum",
            "valueDesc": '{"range":["ZZ","FZ","STOP"]}',
            "enumMappingMap": {},
            "statusFormat": '{"control":"$"}',
            "valueConvert": "enum",
            "supportLocal": True,
        },
        {"dpId": 9, "statusCode": "mode", "valueType": "Enum", "supportLocal": False},
        {"dpId": 22, "statusCode": "bright_value", "valueType": "Integer", "valueConvert": "hb_range_v1"},
    ]
    dps = tdp.build_dp_map(tdp.relations_from_dtos(dtos), {}, {})
    assert "mode" not in dps
    assert dps["bright_value"]["min"] == 25 and dps["bright_value"]["max"] == 255  # the conversion's local range
    code, actions = tdp.cover_control(dps)
    assert code == "control" and actions == {"open": "FZ", "close": "ZZ", "stop": "STOP"}


def test_enum_words_map_whichever_words_the_range_lists() -> None:
    mapping = {"0": {"value": "open"}, "1": {"value": "stop"}, "2": {"value": "close"}}
    # The cloud lists the standard words; older or hand-made maps list the motor's own.
    for listed in (["open", "stop", "close"], ["0", "1", "2"]):
        relation = {
            "dpId": 1,
            "statusCode": "control",
            "valueType": "Enum",
            "valueDesc": json.dumps({"range": listed}),
            "enumMappingMap": mapping,
            "statusFormat": '{"control":"$"}',
            "valueConvert": "enum",
        }
        dps = tdp.build_dp_map(tdp.relations_from_dtos([relation]), {}, {})
        assert dps["control"]["range"] == ["open", "stop", "close"]
        assert tdp.cover_control(dps) == ("control", {"open": "0", "close": "2", "stop": "1"})
        assert tdp.enum_std(dps["control"], "2") == "close"
    # With no range of its own, the cloud spec's (standard words) is read through the map just the same.
    relation = {"dp": 4, "code": "mode", "type": "Enum", "enum": {"m1": {"value": "cold"}, "m2": {"value": "hot"}}}
    dps = tdp.build_dp_map([relation], {"mode": {"type": "Enum", "values": '{"range":["cold","hot"]}'}}, {})
    assert dps["mode"]["range"] == ["cold", "hot"] and tdp.enum_raw(dps["mode"], "hot") == "m2"
    assert tdp.enum_std(dps["mode"], "M1") == "cold"  # matched in lower case too, as tuya_sharing does


@pytest.mark.parametrize(
    ("fmt", "hsv", "raw"),
    [
        ("hsv16", (0, 1.0, 1.0), "000003e803e8"),
        ("hsv16", (240, 0.5, 0.25), "00f001f400fa"),
        ("hsv16", (360, 1.0, 0.0), "000003e8000a"),  # v never 0: some lights read that as off
        ("rgb8", (0, 1.0, 1.0), "ff00000000ffff"),
        ("rgb8", (120, 1.0, 0.5), "0080000078ff80"),
    ],
)
def test_colour_encodings(fmt: str, hsv: tuple[float, float, float], raw: str) -> None:
    assert tdp.encode_colour(fmt, *hsv) == raw
    h, s, v = tdp.decode_colour(fmt, raw) or (0, 0, 0)
    assert len(raw) == (12 if fmt == "hsv16" else 14)
    assert round(h) % 360 == round(hsv[0]) % 360 and abs(s - hsv[1]) < 0.01 and abs(v - hsv[2]) < 0.02


def test_colour_names_and_temperatures() -> None:
    assert tdp.parse_colour("Blue") == tdp.Colour("blue", 240.0, 1.0)
    assert tdp.parse_colour("the red colour") == tdp.Colour("red", 0.0, 1.0)
    assert tdp.parse_colour("#fff") == tdp.Colour("#ffffff", white=True)
    assert tdp.parse_colour("cool white") == tdp.Colour("cool white", white=True, temp=100)
    assert tdp.parse_colour("ultraviolet") is None
    assert [tdp.parse_color_temp(v) for v in ("warm", "neutral", "cool", "25", "6500k", 100, "hot")] == [
        0,
        50,
        100,
        25,
        100,
        100,
        None,
    ]


def test_kinds_and_left_out_categories() -> None:
    light = {
        "switch_led": {"dp": 1, "type": "bool"},
        "bright_value_1": {"dp": 2, "type": "int", "min": 10, "max": 1000},
    }
    assert tdp.kind_for("tdq", light) == "light"  # a dimmer module sold as a switch
    assert tdp.kind_for("xyz", {"control": {"dp": 1, "type": "enum", "range": ["open", "close"]}}) == "cover"
    assert tdp.kind_for("wnykq", {}) is None and "Tuya Smart or Smart Life app" in tdp.skip_reason("wnykq")
    assert tdp.kind_for("infrared_ac", {}) is None and "IR" in tdp.skip_reason("infrared_ac")
    assert tdp.kind_for("wg2", {}) is None and "hub" in tdp.skip_reason("wg2")
    assert tdp.kind_for("xyz", {}) is None and "nothing Jarvis can control" in tdp.skip_reason("xyz")
    # Button pushers: by category, or by their arm's codes or a mode that clicks, even when sold as a switch.
    switch = {"switch": {"dp": 1, "type": "bool"}}
    assert tdp.kind_for("szjqr", switch) == tdp.kind_for("znjxs", switch) == "button"
    assert tdp.kind_for("kg", switch) == "switch"
    assert tdp.kind_for("kg", {**switch, "arm_down_percent": {"dp": 9, "type": "int"}}) == "button"
    assert tdp.kind_for("kg", {**switch, "click_sustain_time": {"dp": 3, "type": "int", "ro": True}}) == "button"
    assert tdp.kind_for("xyz", {**switch, "mode": {"dp": 2, "type": "enum", "range": ["click", "switch"]}}) == "button"
    assert tdp.kind_for("kg", {**switch, "mode": {"dp": 2, "type": "enum", "range": ["auto", "manual"]}}) == "switch"


def test_a_button_without_a_name_or_battery_still_never_says_on() -> None:
    record = DeviceRecord(
        "tuya-finger", "tuya", "Fingerbot", "button", settings={"dps": {"switch": {"dp": 1, "type": "bool"}}}
    )
    assert tdp.power_code(record) is None and [s.name for s in tdp.command_specs(record)] == ["press"]
    text = tdp.describe_state(record, {"1": True})
    assert text == "The Fingerbot presses a wall switch. Jarvis cannot see whether the light is on."


def test_cloud_values_round_trip() -> None:
    spec = {p["code"]: {"type": p["type"], "values": json.dumps(p["desc"])} for p in LAMP_POINTS}
    colour = tdp.build_dp_map(tdp.relations_from_dtos(dtos(LAMP_POINTS)), spec, {})["colour_data_v2"]
    assert colour["cloud"] == {"type": "json", "s_max": 1000, "v_max": 1000}
    raw = tdp.encode_colour("hsv16", 240, 0.5, 0.25)
    sent = tdp.to_cloud(colour, raw)
    assert json.loads(sent) == {"h": 240, "s": 500, "v": 250}
    assert tdp.from_cloud(colour, sent) == raw and tdp.from_cloud(colour, json.loads(sent)) == raw
    # A v1 colour (s and v up to 255), and a device that types its colour as a String (12 hex characters).
    v1 = {"dp": 5, "type": "str", "format": "rgb8", "cloud": {"type": "json", "s_max": 255, "v_max": 255}}
    raw = tdp.encode_colour("rgb8", 120, 1.0, 0.5)
    assert json.loads(tdp.to_cloud(v1, raw)) == {"h": 120, "s": 255, "v": 128}
    assert tdp.from_cloud(v1, tdp.to_cloud(v1, raw)) == raw
    text = {**colour, "cloud": {"type": "str", "s_max": 1000, "v_max": 1000}}
    assert tdp.to_cloud(text, "00f001f400fa") == "00f001f400fa" == tdp.from_cloud(text, "00f001f400fa")
    # A range the local protocol converts: hb_range_v1 is 25-255 locally and 0-100 in the cloud.
    relation = {"dp": 3, "code": "bright_value", "type": "Integer", "convert": "hb_range_v1"}
    bright = tdp.build_dp_map([relation], {}, {})["bright_value"]
    assert bright["cloud"] == {"min": 0, "max": 100}
    assert tdp.to_cloud(bright, 140) == 50 and tdp.from_cloud(bright, 50) == 140
    assert tdp.to_cloud(bright, 255) == 100 and tdp.from_cloud(bright, 0) == 25
    # Scaled numbers are the cloud's as they are; enums go by the standard words; booleans stay booleans.
    temp = {"dp": 2, "type": "int", "min": 160, "max": 300, "scale": 1}
    assert tdp.to_cloud(temp, 245) == 245 == tdp.from_cloud(temp, 245)
    control = {"dp": 1, "type": "enum", "range": ["open", "stop", "close"], "raw": {"open": "0", "close": "2"}}
    assert tdp.to_cloud(control, "2") == "close" and tdp.from_cloud(control, "close") == "2"
    assert tdp.to_cloud({"dp": 1, "type": "bool"}, False) is False
    # Saved before the map kept the cloud's form: no guessing.
    old_colour = {k: v for k, v in colour.items() if k != "cloud"}
    old_bright = {k: v for k, v in bright.items() if k != "cloud"}
    for entry, value in ((old_colour, "00f001f400fa"), (old_bright, 140)):
        with pytest.raises(ValueError):
            tdp.to_cloud(entry, value)
    assert tdp.from_cloud(old_colour, sent) is None and tdp.from_cloud(old_bright, 50) is None
