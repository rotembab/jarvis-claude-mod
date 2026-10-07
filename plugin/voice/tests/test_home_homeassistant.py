"""The Home Assistant hub driver against a fake Home Assistant: its REST API and WebSocket API on 127.0.0.1."""

from __future__ import annotations

import copy
import datetime
import json
import logging
import re
import socket
import ssl
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from websockets.exceptions import ConnectionClosed
from websockets.sync.server import ServerConnection, serve

from jarvis_voice.home import homeassistant as ha_mod
from jarvis_voice.home.base import DriverContext
from jarvis_voice.home.homeassistant import (
    BANNED,
    EXPOSURE_UNREADABLE,
    NO_ADDRESS,
    NO_TOKEN,
    NOT_ADMIN_ANY_MORE,
    STARTING,
    TOKEN_REFUSED,
    UNREACHABLE,
    HaClient,
    HaError,
    HomeAssistantDriver,
    HubSettings,
    address_candidates,
    clean_url,
    colour_data,
    kelvin_for,
    read_view,
    websocket_url,
)
from jarvis_voice.home.model import DeviceRecord, Outcome, Value
from jarvis_voice.home.service import HomeService
from jarvis_voice.home.store import HomeStore

from home_fakes import ScriptedPrompter, make_store

TOKEN = "fake-ha-token-for-jarvis-tests"
WRONG_TOKEN = "not-the-home-assistant-token"
HA_VERSION = "2026.9.4"
AIOHTTP_500 = b"500 Internal Server Error\n\nServer got itself in trouble"

MEDIA_ALL = 1 | 4 | 8 | 16 | 32 | 128 | 256 | 1024 | 2048 | 4096 | 16384


def _state(entity_id: str, state: str, **attributes: Any) -> dict[str, Any]:
    stamp = "2026-10-07T08:00:00+00:00"
    return {
        "entity_id": entity_id,
        "state": state,
        "attributes": attributes,
        "last_changed": stamp,
        "last_reported": stamp,
        "last_updated": stamp,
        "context": {"id": "01J0000000000000000000000", "parent_id": None, "user_id": None},
    }


def initial_states() -> list[dict[str, Any]]:
    return [
        _state(
            "light.kitchen",
            "off",
            friendly_name="Kitchen light",
            supported_color_modes=["color_temp", "hs"],
            supported_features=4,
            effect_list=["Rainbow", "Candle"],
            min_color_temp_kelvin=2200,
            max_color_temp_kelvin=6500,
            brightness=None,
        ),
        _state("light.desk", "on", friendly_name="Desk lamp", supported_color_modes=["brightness"], brightness=128),
        _state("light.porch", "off", friendly_name="Porch light", supported_color_modes=["onoff"]),
        _state("light.attic", "unavailable", friendly_name="Attic light", supported_color_modes=["brightness"]),
        _state("switch.kettle", "off", friendly_name="Kettle", device_class="outlet"),
        _state("switch.hidden_relay", "off", friendly_name="Hidden relay"),
        _state("input_boolean.guest_mode", "off", friendly_name="Guest mode"),
        _state(
            "fan.bedroom",
            "off",
            friendly_name="Bedroom fan",
            supported_features=1 | 8 | 16 | 32,
            preset_modes=["sleep", "breeze"],
            percentage=0,
        ),
        _state(
            "cover.blinds",
            "closed",
            friendly_name="Blinds",
            device_class="blind",
            supported_features=1 | 2 | 4 | 8,
            current_position=0,
        ),
        _state(
            "cover.garage_door",
            "closed",
            friendly_name="Garage door",
            device_class="garage",
            supported_features=1 | 2 | 8,
        ),
        _state(
            "cover.driveway_gate",
            "closed",
            friendly_name="Driveway gate",
            device_class="gate",
            supported_features=1 | 2 | 4 | 8,
            current_position=0,
        ),
        _state(
            "climate.hall",
            "heat",
            friendly_name="Hall thermostat",
            hvac_modes=["off", "heat", "cool", "auto"],
            fan_modes=["auto", "low", "high"],
            min_temp=7,
            max_temp=35,
            temperature=20,
            current_temperature=19.5,
            supported_features=1 | 8 | 128 | 256,
        ),
        _state(
            "climate.bedroom_ac",
            "cool",
            friendly_name="Bedroom AC",
            hvac_modes=["off", "cool"],
            min_temp=16,
            max_temp=30,
            temperature=23,
            supported_features=1,
        ),
        _state(
            "media_player.lounge_tv",
            "on",
            friendly_name="Lounge TV",
            device_class="tv",
            supported_features=MEDIA_ALL,
            source_list=["HDMI 1", "Netflix", "YouTube"],
            source="HDMI 1",
            volume_level=0.2,
            is_volume_muted=False,
        ),
        _state("scene.movie_night", "2026-10-06T20:00:00+00:00", friendly_name="Movie night"),
        _state("script.bedtime", "off", friendly_name="Bedtime"),
        _state("button.doorbell_chime", "unknown", friendly_name="Doorbell chime"),
        _state("button.restart_hub", "unknown", friendly_name="Restart hub", device_class="restart"),
        _state("lock.front_door", "locked", friendly_name="Front door"),
        _state("lock.shed", "locked", friendly_name="Shed lock", code_format="^\\d{4}$"),
        _state(
            "alarm_control_panel.home",
            "disarmed",
            friendly_name="House alarm",
            supported_features=1 | 2 | 4,
            code_format="number",
            code_arm_required=False,
        ),
        _state(
            "vacuum.robi",
            "docked",
            friendly_name="Robi",
            supported_features=4 | 8 | 16 | 32 | 8192,
            fan_speed_list=["quiet", "max"],
            battery_level=90,
        ),
        _state(
            "humidifier.bedroom",
            "off",
            friendly_name="Bedroom humidifier",
            supported_features=1,
            available_modes=["normal", "sleep"],
            humidity=45,
        ),
        _state(
            "water_heater.boiler",
            "eco",
            friendly_name="Boiler",
            supported_features=1 | 2 | 8,
            operation_list=["off", "eco", "performance"],
            min_temp=40,
            max_temp=65,
            temperature=55,
        ),
        # Domains Jarvis does not control.
        _state("sensor.outdoor_temperature", "14.2", friendly_name="Outdoor temperature", unit_of_measurement="°C"),
        _state("binary_sensor.front_door", "off", friendly_name="Front door sensor"),
        _state("remote.lounge_tv", "on", friendly_name="Lounge TV remote"),
        _state("person.rotem", "home", friendly_name="Rotem"),
        _state("automation.night_lights", "on", friendly_name="Night lights"),
    ]


CONTROLLABLE = {
    "light.kitchen",
    "light.desk",
    "light.porch",
    "light.attic",
    "switch.kettle",
    "input_boolean.guest_mode",
    "fan.bedroom",
    "cover.blinds",
    "cover.garage_door",
    "cover.driveway_gate",
    "climate.hall",
    "climate.bedroom_ac",
    "media_player.lounge_tv",
    "scene.movie_night",
    "script.bedtime",
    "button.doorbell_chime",
    "lock.front_door",
    "lock.shed",
    "alarm_control_panel.home",
    "vacuum.robi",
    "humidifier.bedroom",
    "water_heater.boiler",
}

AREAS = [
    {"area_id": area_id, "name": name, "aliases": [], "floor_id": None, "icon": None, "labels": [], "picture": None}
    for area_id, name in [
        ("kitchen", "Kitchen"),
        ("living_room", "Living room"),
        ("hall", "Hall"),
        ("garage", "Garage"),
        ("bedroom", "Bedroom"),
    ]
]


def _device(ident: str, area: str | None, *, parent: str | None = None, via: str | None = None) -> dict[str, Any]:
    return {
        "id": ident,
        "area_id": area,
        "name": ident,
        "name_by_user": None,
        "parent_device_id": parent,
        "via_device_id": via,
        "manufacturer": "Fake",
        "model": "Fake",
    }


DEVICES = [
    _device("dev-bridge", "hall"),
    _device("dev-kitchen-bulb", "kitchen", via="dev-bridge"),
    _device("dev-tv", "living_room"),
    _device("dev-lock-hub", "hall"),
    _device("dev-front-lock", None, parent="dev-lock-hub"),  # a child device: in its parent's room
    _device("dev-zigbee-plug", None, via="dev-bridge"),  # reached through the bridge: not in its room
    _device("dev-loop-a", None, parent="dev-loop-b"),
    _device("dev-loop-b", None, parent="dev-loop-a"),
]

REGISTRY = [
    {"ei": "light.kitchen", "di": "dev-kitchen-bulb", "pl": "hue", "hn": True, "en": "Light"},
    {"ei": "light.desk", "ai": "living_room", "di": "dev-kitchen-bulb", "pl": "hue"},  # its own area wins
    {"ei": "media_player.lounge_tv", "di": "dev-tv", "pl": "braviatv"},
    {"ei": "lock.front_door", "di": "dev-front-lock", "pl": "nuki"},
    {"ei": "switch.kettle", "di": "dev-zigbee-plug", "pl": "zha"},
    {"ei": "cover.garage_door", "ai": "garage", "pl": "ratgdo"},
    {"ei": "fan.bedroom", "ai": "bedroom", "pl": "dyson"},
    {"ei": "button.restart_hub", "di": "dev-lock-hub", "pl": "nuki", "ec": 0},
    {"ei": "switch.hidden_relay", "pl": "shelly", "hb": True},
    {"ei": "vacuum.robi", "di": "dev-loop-a", "pl": "roborock"},
]

EXPOSED = {
    "light.kitchen": {"conversation": True},
    "light.desk": {"conversation": True, "cloud.alexa": True},
    "media_player.lounge_tv": {"conversation": True},
    "cover.garage_door": {"conversation": True},
    "lock.front_door": {"conversation": True},
    "climate.hall": {"conversation": True},
    "scene.movie_night": {"conversation": True},
    "sensor.outdoor_temperature": {"conversation": True},
    "switch.kettle": {"cloud.google_assistant": True},
}
EXPOSED_NAMES = [
    "Desk lamp",
    "Front door",
    "Garage door",
    "Hall thermostat",
    "Kitchen light",
    "Lounge TV",
    "Movie night",
]

# Every service the fake knows; any other answers 400 like Home Assistant.
SERVICES = {
    "light": {"turn_on", "turn_off", "toggle"},
    "switch": {"turn_on", "turn_off", "toggle"},
    "input_boolean": {"turn_on", "turn_off", "toggle", "reload"},
    "fan": {"turn_on", "turn_off", "toggle", "set_percentage", "set_preset_mode", "oscillate", "set_direction"},
    "cover": {"open_cover", "close_cover", "stop_cover", "set_cover_position", "toggle"},
    "climate": {"turn_on", "turn_off", "toggle", "set_temperature", "set_hvac_mode", "set_fan_mode", "set_preset_mode"},
    "media_player": {
        "turn_on",
        "turn_off",
        "toggle",
        "volume_up",
        "volume_down",
        "volume_set",
        "volume_mute",
        "media_play",
        "media_pause",
        "media_play_pause",
        "media_stop",
        "media_next_track",
        "media_previous_track",
        "select_source",
    },
    "scene": {"turn_on", "apply", "reload"},
    "script": {"turn_on", "turn_off", "toggle", "reload"},
    "button": {"press"},
    "lock": {"lock", "unlock", "open"},
    "alarm_control_panel": {"alarm_arm_away", "alarm_arm_home", "alarm_arm_night", "alarm_disarm", "alarm_trigger"},
    "vacuum": {"start", "pause", "stop", "return_to_base", "set_fan_speed", "locate"},
    "humidifier": {"turn_on", "turn_off", "toggle", "set_mode", "set_humidity"},
    "water_heater": {"turn_on", "turn_off", "set_temperature", "set_operation_mode"},
}


def _needs_code(domain: str, service: str, attributes: dict[str, Any]) -> bool:
    if not attributes.get("code_format"):
        return False
    if domain == "lock":
        return True
    return service == "alarm_disarm" or attributes.get("code_arm_required") is not False


def apply_service(domain: str, service: str, state: dict[str, Any], data: dict[str, Any]) -> None:
    """What the service does to the entity, roughly as Home Assistant's integrations would."""
    a = state["attributes"]
    if domain in ("light", "switch", "input_boolean", "fan", "humidifier") and service in (
        "turn_on",
        "turn_off",
        "toggle",
    ):
        new = {"turn_on": "on", "turn_off": "off"}.get(service) or ("off" if state["state"] == "on" else "on")
        state["state"] = new
        if domain == "light":
            if new == "on":
                if "brightness_pct" in data:
                    a["brightness"] = round(data["brightness_pct"] * 255 / 100)
                elif a.get("brightness") is None:
                    a["brightness"] = 255
                for key in ("rgb_color", "color_name", "color_temp_kelvin", "effect"):
                    if key in data:
                        a[key] = data[key]
                if data.get("brightness_pct") == 0:
                    state["state"] = "off"
            else:
                a["brightness"] = None
        return
    if domain == "fan":
        if service == "set_percentage":
            a["percentage"] = data["percentage"]
            state["state"] = "on" if data["percentage"] else "off"
        elif service == "set_preset_mode":
            a["preset_mode"] = data["preset_mode"]
            state["state"] = "on"
    elif domain == "cover":
        if service == "open_cover":
            state["state"], a["current_position"] = "open", 100
        elif service == "close_cover":
            state["state"], a["current_position"] = "closed", 0
        elif service == "set_cover_position":
            a["current_position"] = data["position"]
            state["state"] = "open" if data["position"] else "closed"
    elif domain == "climate":
        if service == "set_temperature":
            a["temperature"] = data["temperature"]
        elif service == "set_hvac_mode":
            state["state"] = data["hvac_mode"]
        elif service == "set_fan_mode":
            a["fan_mode"] = data["fan_mode"]
        elif service == "turn_off":
            state["state"] = "off"
        elif service == "turn_on":
            state["state"] = "heat"
    elif domain == "media_player":
        simple = {"media_play": "playing", "media_pause": "paused", "media_stop": "idle", "turn_on": "on"}
        if service in simple:
            state["state"] = simple[service]
        elif service == "turn_off":
            state["state"] = "off"
        elif service == "media_play_pause":
            state["state"] = "paused" if state["state"] == "playing" else "playing"
        elif service == "volume_set":
            a["volume_level"] = data["volume_level"]
        elif service == "volume_up":
            a["volume_level"] = round(min(1.0, a["volume_level"] + 0.1), 2)
        elif service == "volume_down":
            a["volume_level"] = round(max(0.0, a["volume_level"] - 0.1), 2)
        elif service == "volume_mute":
            a["is_volume_muted"] = data["is_volume_muted"]
        elif service == "select_source":
            a["source"] = data["source"]
        # media_next_track and media_previous_track change nothing here: nothing is playing.
    elif domain == "lock":
        state["state"] = {"lock": "locked", "unlock": "unlocked", "open": "open"}[service]
    elif domain == "alarm_control_panel":
        state["state"] = {
            "alarm_arm_away": "armed_away",
            "alarm_arm_home": "armed_home",
            "alarm_arm_night": "armed_night",
            "alarm_disarm": "disarmed",
            "alarm_trigger": "triggered",
        }[service]
    elif domain == "vacuum":
        moves = {"start": "cleaning", "pause": "paused", "stop": "idle", "return_to_base": "returning"}
        if service in moves:
            state["state"] = moves[service]
        elif service == "set_fan_speed":
            a["fan_speed"] = data["fan_speed"]
    elif domain == "humidifier" and service == "set_mode":
        a["mode"] = data["mode"]
    elif domain == "water_heater":
        if service == "set_temperature":
            a["temperature"] = data["temperature"]
        elif service == "set_operation_mode":
            state["state"] = data["operation_mode"]
        elif service in ("turn_on", "turn_off"):
            state["state"] = "eco" if service == "turn_on" else "off"
    elif domain in ("scene", "button"):
        state["state"] = datetime.datetime.now(datetime.UTC).isoformat()


# --------------------------------------------------------------------------- the fake Home Assistant


class FakeHomeAssistant:
    """Home Assistant's REST API (``/api/...``) and WebSocket API, as 2026.9 answers them.

    A wrong token gets a plain-text 401 (and counts as a failed login), a
    service call on an unknown or unavailable entity answers 200 [], a device
    error is aiohttp's plain-text 500 unless ``service_error`` says otherwise,
    and everything answers 503 while ``stopping``.
    """

    def __init__(self, *, admin: bool = True, tls: ssl.SSLContext | None = None) -> None:
        self.token = TOKEN
        self.admin = admin
        self.tls = tls
        self.states = {s["entity_id"]: s for s in initial_states()}
        self.exposed = copy.deepcopy(EXPOSED)
        self.registry = copy.deepcopy(REGISTRY)
        self.devices = copy.deepcopy(DEVICES)
        self.areas = copy.deepcopy(AREAS)
        self.stopping = False
        self.service_error: tuple[int, str, bytes] | None = None
        self.code_error: tuple[int, str, bytes] = (500, "text/plain; charset=utf-8", AIOHTTP_500)
        self.service_delay = 0.0
        self.lazy = 0.0  # >0: the state changes this long after the call answers []
        self.ws_down = False
        self.ws_noise = False  # send an unrelated event before each result
        self.requests: list[tuple[str, str]] = []
        self.service_calls: list[tuple[str, str, dict[str, Any]]] = []
        self.ws_types: list[str] = []
        self.ws_sessions = 0
        self.auth_failures = 0
        self.ws_auth_failures = 0
        self.problems: list[str] = []
        self.lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                fake.http(self, "GET")

            def do_POST(self) -> None:
                fake.http(self, "POST")

        self._http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._http.daemon_threads = True
        if tls is not None:
            self._http.socket = tls.wrap_socket(self._http.socket, server_side=True)
        quiet = logging.getLogger("fake_home_assistant.websocket")
        quiet.setLevel(logging.CRITICAL)  # the server would log the auth frame, token and all
        self._ws = serve(self.ws_session, "127.0.0.1", 0, ssl=tls, logger=quiet)
        scheme = "https" if tls is not None else "http"
        self.url = f"{scheme}://127.0.0.1:{self._http.server_address[1]}"
        ws_port = self._ws.socket.getsockname()[1]
        self.ws_url = f"{'wss' if tls is not None else 'ws'}://127.0.0.1:{ws_port}/api/websocket"

    def start(self) -> None:
        threading.Thread(target=self._http.serve_forever, args=(0.05,), daemon=True).start()
        threading.Thread(target=self._ws.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self._http.shutdown()
        self._http.server_close()
        self._ws.shutdown()

    def websocket_url(self, base_url: str) -> str:
        """Stands in for the driver's own: Home Assistant serves both on one port, the fake on two."""
        if base_url.rstrip("/") != self.url:
            self.problems.append(f"WebSocket asked for {base_url}")
        return self.ws_url

    def paths(self) -> list[str]:
        with self.lock:
            return [path for _, path in self.requests]

    # ------------------------------------------------------------------ REST

    def http(self, handler: BaseHTTPRequestHandler, method: str) -> None:
        length = int(handler.headers.get("Content-Length") or 0)
        body = handler.rfile.read(length) if length else b""
        path = handler.path
        with self.lock:
            self.requests.append((method, path))
        if path == "/manifest.json":  # the frontend's, served without a token
            return self.send(handler, 200, {"name": "Home Assistant", "short_name": "Home Assistant"})
        if self.stopping:
            return self.send_raw(handler, 503, b"", "application/octet-stream")
        if handler.headers.get("Authorization") != f"Bearer {self.token}":
            with self.lock:
                self.auth_failures += 1  # a "Login attempt failed" notification in Home Assistant
            return self.send_raw(handler, 401, b"401: Unauthorized", "text/plain; charset=utf-8")
        if method == "GET" and path == "/api/":
            return self.send(handler, 200, {"message": "API running."})
        if method == "GET" and path == "/api/config":
            return self.send(handler, 200, {"version": HA_VERSION, "location_name": "Home"})
        if method == "GET" and path == "/api/states":
            with self.lock:
                return self.send(handler, 200, list(self.states.values()))
        if method == "GET" and path.startswith("/api/states/"):
            with self.lock:
                state = self.states.get(path.removeprefix("/api/states/"))
            if state is None:
                return self.send(handler, 404, {"message": "Entity not found."})
            return self.send(handler, 200, state)
        if method == "POST" and path.startswith("/api/services/"):
            domain, _, service = path.removeprefix("/api/services/").partition("/")
            return self.service(handler, domain, service, json.loads(body or b"{}"))
        if method == "POST" and path == "/api/template":  # @require_admin
            if not self.admin:
                with self.lock:
                    self.auth_failures += 1
                return self.send_raw(handler, 401, b"401: Unauthorized", "text/plain; charset=utf-8")
            return self.send_raw(handler, 200, b"[]", "text/plain; charset=utf-8")
        return self.send_raw(handler, 404, b"404: Not Found", "text/plain; charset=utf-8")

    def service(self, handler: BaseHTTPRequestHandler, domain: str, service: str, data: dict[str, Any]) -> None:
        with self.lock:
            self.service_calls.append((domain, service, data))
        if self.service_delay:
            time.sleep(self.service_delay)
        if self.service_error is not None:
            status, content_type, body = self.service_error
            return self.send_raw(handler, status, body, content_type)
        if service not in SERVICES.get(domain, set()):
            return self.send_raw(handler, 400, b"400: Bad Request", "text/plain; charset=utf-8")
        entity_id = data.get("entity_id")
        with self.lock:
            state = self.states.get(entity_id) if isinstance(entity_id, str) else None
            if state is None or state["state"] == "unavailable":
                # Home Assistant skips it with a warning in its own log, and still answers 200.
                return self.send(handler, 200, [])
            if _needs_code(domain, service, state["attributes"]) and "code" not in data:
                status, content_type, body = self.code_error
                return self.send_raw(handler, status, body, content_type)
            new = copy.deepcopy(state)
            apply_service(domain, service, new, data)
            if new == state:
                return self.send(handler, 200, [])
            if self.lazy:

                def later() -> None:
                    with self.lock:
                        self.states[new["entity_id"]] = new

                threading.Timer(self.lazy, later).start()
                return self.send(handler, 200, [])
            self.states[new["entity_id"]] = new
        return self.send(handler, 200, [new])

    @staticmethod
    def send(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
        FakeHomeAssistant.send_raw(handler, status, json.dumps(payload).encode(), "application/json")

    @staticmethod
    def send_raw(handler: BaseHTTPRequestHandler, status: int, body: bytes, content_type: str) -> None:
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    # ------------------------------------------------------------------ WebSocket

    def ws_session(self, conn: ServerConnection) -> None:
        with self.lock:
            self.ws_sessions += 1
        if self.ws_down:
            conn.close()
            return
        try:
            conn.send(json.dumps({"type": "auth_required", "ha_version": HA_VERSION}))
            hello = json.loads(conn.recv(timeout=5))
            if hello.get("type") != "auth" or hello.get("access_token") != self.token:
                with self.lock:
                    self.ws_auth_failures += 1
                conn.send(json.dumps({"type": "auth_invalid", "message": "Invalid access token or password"}))
                return
            conn.send(json.dumps({"type": "auth_ok", "ha_version": HA_VERSION}))
            last = 0
            for raw in conn:
                message = json.loads(raw)
                ident = message.get("id")
                if not isinstance(ident, int) or ident <= last:
                    error = {"code": "id_reuse", "message": "Identifier values have to increase."}
                    conn.send(json.dumps({"id": ident, "type": "result", "success": False, "error": error}))
                    continue
                last = ident
                kind = str(message.get("type"))
                with self.lock:
                    self.ws_types.append(kind)
                if self.ws_noise:
                    conn.send(json.dumps({"id": ident + 1000, "type": "event", "event": {"event_type": "noise"}}))
                ok, result = self.ws_answer(kind)
                answer: dict[str, Any] = {"id": ident, "type": "result", "success": ok}
                answer["result" if ok else "error"] = result
                conn.send(json.dumps(answer))
        except (ConnectionClosed, TimeoutError, ValueError):
            pass

    def ws_answer(self, kind: str) -> tuple[bool, Any]:
        if kind == "auth/current_user":
            user = {"id": "user-1", "name": "Rotem", "is_owner": self.admin, "is_admin": self.admin}
            return True, {**user, "credentials": [], "mfa_modules": []}
        if kind == "homeassistant/expose_entity/list":  # @require_admin
            if not self.admin:
                return False, {"code": "unauthorized", "message": "Unauthorized"}
            return True, {"exposed_entities": self.exposed}
        if kind == "config/entity_registry/list_for_display":
            return True, {"entity_categories": {"0": "config", "1": "diagnostic"}, "entities": self.registry}
        if kind == "config/device_registry/list":
            return True, self.devices
        if kind == "config/area_registry/list":
            return True, self.areas
        return False, {"code": "unknown_command", "message": "Unknown command."}


# --------------------------------------------------------------------------- fixtures and helpers


@pytest.fixture
def ha(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeHomeAssistant]:
    fake = FakeHomeAssistant()
    fake.start()
    monkeypatch.setattr(ha_mod, "websocket_url", fake.websocket_url)
    yield fake
    fake.stop()
    assert fake.problems == []


@pytest.fixture(autouse=True)
def fresh_and_no_token_in_logs(caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    ha_mod._refused.clear()
    caplog.set_level(logging.DEBUG)
    yield
    ha_mod._refused.clear()
    assert TOKEN not in caplog.text
    assert WRONG_TOKEN not in caplog.text


def save_hub(store: HomeStore, url: str, *, token: str | None = TOKEN, **settings: Any) -> None:
    hub = {"url": url, "verify_tls": True, "admin": True, "exposed_only": False, **settings}
    store.update(lambda c: c.hubs.__setitem__("homeassistant", hub))
    if token is not None:
        store.set_secret("homeassistant:hub", {"token": token})


def connect(
    tmp_path: Path, ha: FakeHomeAssistant, *, token: str = TOKEN, **settings: Any
) -> tuple[HomeAssistantDriver, HomeStore]:
    store = make_store(tmp_path)
    save_hub(store, ha.url, token=token, **settings)
    driver = HomeAssistantDriver(DriverContext(store, tmp_path))
    driver.recheck_wait_s = 0.05
    return driver, store


def device(driver: HomeAssistantDriver, entity_id: str) -> DeviceRecord:
    found = [d for d in driver.hub_devices() if d.id == f"ha:{entity_id}"]
    assert found, f"{entity_id} is not listed"
    return found[0]


def checked(outcome: Outcome) -> Outcome:
    """Spoken texts carry no token, no address and no entity id."""
    assert TOKEN not in outcome.text and WRONG_TOKEN not in outcome.text
    assert "127.0.0.1" not in outcome.text and "ha:" not in outcome.text
    assert not re.search(r"\b[a-z_]+\.[a-z0-9_]+\b", outcome.text), outcome.text
    return outcome


def run(driver: HomeAssistantDriver, entity_id: str, command: str, value: Value = None) -> Outcome:
    return checked(driver.run(device(driver, entity_id), command, value))


def status(driver: HomeAssistantDriver, entity_id: str) -> Outcome:
    return checked(driver.status(device(driver, entity_id)))


def closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def names(devices: list[DeviceRecord]) -> list[str]:
    return sorted(d.name for d in devices)


# --------------------------------------------------------------------------- listing


def test_entities_of_controllable_domains_become_devices(tmp_path: Path, ha: FakeHomeAssistant) -> None:
    driver, _ = connect(tmp_path, ha)
    devices = {d.id.removeprefix("ha:"): d for d in driver.hub_devices()}
    # Sensors, remotes, people and automations are dropped, and so are the hidden and configuration entities.
    assert set(devices) == CONTROLLABLE
    kinds = {eid: d.kind for eid, d in devices.items()}
    assert kinds["switch.kettle"] == "plug" and kinds["input_boolean.guest_mode"] == "switch"
    assert kinds["cover.garage_door"] == "garage" and kinds["cover.driveway_gate"] == "garage"
    assert kinds["cover.blinds"] == "cover" and kinds["media_player.lounge_tv"] == "tv"
    assert kinds["script.bedtime"] == "scene" and kinds["water_heater.boiler"] == "heater"
    assert kinds["alarm_control_panel.home"] == "alarm" and kinds["button.doorbell_chime"] == "other"
    kitchen = devices["light.kitchen"]
    assert (kitchen.id, kitchen.driver, kitchen.name) == ("ha:light.kitchen", "homeassistant", "Kitchen light")
    assert kitchen.settings == {
        "entity_id": "light.kitchen",
        "domain": "light",
        "supported_features": 4,
        "color_modes": ["color_temp", "hs"],
        "effects": ["Rainbow", "Candle"],
        "min_kelvin": 2200.0,
        "max_kelvin": 6500.0,
    }
    tv = devices["media_player.lounge_tv"].settings
    assert tv["device_class"] == "tv" and tv["sources"] == ["HDMI 1", "Netflix", "YouTube"]
    assert devices["climate.hall"].settings["hvac_modes"] == ["off", "heat", "cool", "auto"]
    assert (
        devices["lock.shed"].settings["needs_code"] is True and "needs_code" not in devices["lock.front_door"].settings
    )
    assert TOKEN not in json.dumps([d.to_json() for d in devices.values()])


def test_rooms_come_from_the_registries_with_child_devices_in_their_parents_room(
    tmp_path: Path, ha: FakeHomeAssistant
) -> None:
    ha.ws_noise = True  # events and other answers in between are skipped
    driver, _ = connect(tmp_path, ha)
    rooms = {d.id.removeprefix("ha:"): d.room for d in driver.hub_devices()}
    assert rooms["light.kitchen"] == "Kitchen"  # from its device
    assert rooms["light.desk"] == "Living room"  # its own area beats its device's
    assert rooms["lock.front_door"] == "Hall"  # a child device without an area: its parent's
    assert rooms["cover.garage_door"] == "Garage" and rooms["fan.bedroom"] == "Bedroom"
    assert rooms["switch.kettle"] is None  # via a bridge is not the bridge's room
    assert rooms["vacuum.robi"] is None  # a parent loop ends
    assert rooms["climate.hall"] is None  # not in the registry at all


def test_an_administrator_sees_only_what_is_exposed_to_assist(tmp_path: Path, ha: FakeHomeAssistant) -> None:
    driver, _ = connect(tmp_path, ha, exposed_only=True)
    assert names(driver.hub_devices()) == EXPOSED_NAMES
    assert "homeassistant/expose_entity/list" in ha.ws_types
    assert driver.describe() == "Home Assistant connected (7 devices exposed to Assist)"
    # An administrator without a saved choice gets the Assist list by default.
    store = make_store(tmp_path / "default")
    store.update(lambda c: c.hubs.__setitem__("homeassistant", {"url": ha.url, "admin": True}))
    store.set_secret("homeassistant:hub", {"token": TOKEN})
    assert names(HomeAssistantDriver(DriverContext(store, tmp_path)).hub_devices()) == EXPOSED_NAMES


def test_a_non_admin_token_never_touches_admin_endpoints(tmp_path: Path, ha: FakeHomeAssistant) -> None:
    ha.admin = False
    driver, _ = connect(tmp_path, ha, admin=False, exposed_only=False)
    devices = driver.hub_devices()
    assert {d.id.removeprefix("ha:") for d in devices} == CONTROLLABLE
    assert ha.ws_types == [
        "auth/current_user",
        "config/entity_registry/list_for_display",
        "config/device_registry/list",
        "config/area_registry/list",
    ]
    assert "/api/template" not in ha.paths()
    assert ha.auth_failures == 0 and ha.ws_auth_failures == 0
    assert driver.describe() == "Home Assistant connected (22 devices)"


def test_exposed_only_shows_nothing_when_the_assist_list_cannot_be_read(tmp_path: Path, ha: FakeHomeAssistant) -> None:
    # The user was an administrator at setup, and is not any more: asking would be refused, so it is not asked.
    ha.admin = False
    driver, _ = connect(tmp_path, ha, admin=True, exposed_only=True)
    with pytest.raises(HaError) as caught:
        driver.hub_devices()
    assert caught.value.kind == "exposure" and caught.value.spoken == NOT_ADMIN_ANY_MORE
    assert "homeassistant/expose_entity/list" not in ha.ws_types
    assert driver.describe() == NOT_ADMIN_ANY_MORE
    # The WebSocket is down: no list, so no devices rather than all of them.
    ha.admin, ha.ws_down = True, True
    with pytest.raises(HaError) as caught:
        driver.hub_devices()
    assert caught.value.spoken == EXPOSURE_UNREADABLE


def test_without_the_websocket_the_devices_come_without_rooms(tmp_path: Path, ha: FakeHomeAssistant) -> None:
    ha.ws_down = True
    driver, _ = connect(tmp_path, ha)
    devices = driver.hub_devices()
    assert all(d.room is None for d in devices)
    # Without the registries Jarvis cannot tell the hidden ones.
    assert "ha:switch.hidden_relay" in {d.id for d in devices}


def test_the_list_is_cached_and_read_again_when_stale_or_changed(tmp_path: Path, ha: FakeHomeAssistant) -> None:
    driver, store = connect(tmp_path, ha)
    first = driver.hub_devices()
    first[0].name = "changed by a caller"
    second = driver.hub_devices()
    assert ha.paths().count("/api/states") == 1 and ha.ws_sessions == 1
    assert second[0].name != "changed by a caller"
    driver.cache_s = 0.0
    driver.hub_devices()
    assert ha.paths().count("/api/states") == 2
    driver.cache_s = 300.0
    # New settings (here, exposure) are not served from the old list.
    save_hub(store, ha.url, exposed_only=True)
    assert names(driver.hub_devices()) == EXPOSED_NAMES
    assert ha.paths().count("/api/states") == 3
    driver.close()
    driver.hub_devices()
    assert ha.paths().count("/api/states") == 4


# --------------------------------------------------------------------------- commands


def test_commands_follow_the_saved_features_and_attributes(tmp_path: Path, ha: FakeHomeAssistant) -> None:
    driver, _ = connect(tmp_path, ha)

    def usage(entity_id: str) -> list[str]:
        return [spec.usage() for spec in driver.commands(device(driver, entity_id))]

    assert usage("light.porch") == ["turn_on", "turn_off", "toggle"]
    assert usage("light.desk") == ["turn_on", "turn_off", "toggle", "set_brightness <0-100>"]
    assert usage("light.kitchen") == [
        "turn_on",
        "turn_off",
        "toggle",
        "set_brightness <0-100>",
        "set_color <a colour name or #rrggbb>",
        "set_color_temp <warm|neutral|cool, or 0-100 (0 = warmest)>",
        "set_mode <Rainbow|Candle>",
    ]
    assert usage("media_player.lounge_tv") == [
        "turn_on",
        "turn_off",
        "toggle",
        "play",
        "pause",
        "play_pause",
        "stop",
        "next",
        "previous",
        "volume_up",
        "volume_down",
        "set_volume <0-100>",
        "mute",
        "unmute",
        "set_input <HDMI 1|Netflix|YouTube>",
        "list_inputs",
    ]
    assert usage("climate.hall") == [
        "turn_on",
        "turn_off",
        "set_temperature <7-35>",
        "set_mode <off|heat|cool|auto>",
        "set_fan_speed <auto|low|high>",
    ]
    assert usage("climate.bedroom_ac") == ["turn_off", "set_temperature <16-30>", "set_mode <off|cool>"]
    assert usage("cover.garage_door") == ["open", "close", "stop"]
    assert usage("vacuum.robi") == ["start", "pause", "stop", "dock", "set_fan_speed <quiet|max>"]
    assert usage("water_heater.boiler") == [
        "turn_on",
        "turn_off",
        "set_temperature <40-65>",
        "set_mode <off|eco|performance>",
    ]
    assert usage("alarm_control_panel.home") == ["arm", "disarm"]
    assert usage("scene.movie_night") == usage("script.bedtime") == usage("button.doorbell_chime") == ["activate"]
    assert usage("fan.bedroom") == ["turn_on", "turn_off", "toggle", "set_fan_speed <0-100>", "set_mode <sleep|breeze>"]
    assert usage("humidifier.bedroom") == ["turn_on", "turn_off", "toggle", "set_mode <normal|sleep>"]
    assert not any(path.startswith("/api/states/") for path in ha.paths())  # commands never ask the hub


def test_unlocking_disarming_and_opening_a_garage_or_gate_ask_on_screen(tmp_path: Path, ha: FakeHomeAssistant) -> None:
    driver, _ = connect(tmp_path, ha)

    def tiers(entity_id: str) -> dict[str, str]:
        return {spec.name: spec.tier for spec in driver.commands(device(driver, entity_id))}

    assert tiers("lock.front_door") == {"lock": "free", "unlock": "screen"}
    assert tiers("alarm_control_panel.home") == {"arm": "free", "disarm": "screen"}
    assert tiers("cover.garage_door") == {"open": "screen", "close": "free", "stop": "free"}
    gate = tiers("cover.driveway_gate")
    assert gate["open"] == gate["set_position"] == "screen" and gate["close"] == "free"
    assert set(tiers("cover.blinds").values()) == {"free"}


CALLS: list[tuple[str, str, Value, tuple[str, str, dict[str, Any]]]] = [
    ("light.kitchen", "turn_on", None, ("light", "turn_on", {})),
    ("light.kitchen", "turn_off", None, ("light", "turn_off", {})),
    ("light.kitchen", "toggle", None, ("light", "toggle", {})),
    ("light.kitchen", "set_brightness", 40, ("light", "turn_on", {"brightness_pct": 40})),
    ("light.kitchen", "set_color", "#FF8800", ("light", "turn_on", {"rgb_color": [255, 136, 0]})),
    ("light.kitchen", "set_color", "Dark Blue", ("light", "turn_on", {"color_name": "darkblue"})),
    ("light.kitchen", "set_color_temp", "warm", ("light", "turn_on", {"color_temp_kelvin": 2200})),
    ("light.kitchen", "set_color_temp", "50", ("light", "turn_on", {"color_temp_kelvin": 4350})),
    ("light.kitchen", "set_mode", "Candle", ("light", "turn_on", {"effect": "Candle"})),
    ("switch.kettle", "turn_on", None, ("switch", "turn_on", {})),
    ("input_boolean.guest_mode", "toggle", None, ("input_boolean", "toggle", {})),
    ("fan.bedroom", "set_fan_speed", 60, ("fan", "set_percentage", {"percentage": 60})),
    ("fan.bedroom", "set_mode", "sleep", ("fan", "set_preset_mode", {"preset_mode": "sleep"})),
    ("cover.blinds", "open", None, ("cover", "open_cover", {})),
    ("cover.blinds", "close", None, ("cover", "close_cover", {})),
    ("cover.blinds", "stop", None, ("cover", "stop_cover", {})),
    ("cover.blinds", "set_position", 30, ("cover", "set_cover_position", {"position": 30})),
    ("cover.garage_door", "open", None, ("cover", "open_cover", {})),
    ("climate.hall", "turn_on", None, ("climate", "turn_on", {})),
    ("climate.hall", "turn_off", None, ("climate", "turn_off", {})),
    ("climate.bedroom_ac", "turn_off", None, ("climate", "set_hvac_mode", {"hvac_mode": "off"})),
    ("climate.hall", "set_temperature", 21.5, ("climate", "set_temperature", {"temperature": 21.5})),
    ("climate.hall", "set_mode", "cool", ("climate", "set_hvac_mode", {"hvac_mode": "cool"})),
    ("climate.hall", "set_fan_speed", "low", ("climate", "set_fan_mode", {"fan_mode": "low"})),
    ("media_player.lounge_tv", "turn_off", None, ("media_player", "turn_off", {})),
    ("media_player.lounge_tv", "play", None, ("media_player", "media_play", {})),
    ("media_player.lounge_tv", "pause", None, ("media_player", "media_pause", {})),
    ("media_player.lounge_tv", "play_pause", None, ("media_player", "media_play_pause", {})),
    ("media_player.lounge_tv", "stop", None, ("media_player", "media_stop", {})),
    ("media_player.lounge_tv", "next", None, ("media_player", "media_next_track", {})),
    ("media_player.lounge_tv", "previous", None, ("media_player", "media_previous_track", {})),
    ("media_player.lounge_tv", "volume_up", None, ("media_player", "volume_up", {})),
    ("media_player.lounge_tv", "volume_down", None, ("media_player", "volume_down", {})),
    ("media_player.lounge_tv", "set_volume", 30, ("media_player", "volume_set", {"volume_level": 0.3})),
    ("media_player.lounge_tv", "mute", None, ("media_player", "volume_mute", {"is_volume_muted": True})),
    ("media_player.lounge_tv", "unmute", None, ("media_player", "volume_mute", {"is_volume_muted": False})),
    ("media_player.lounge_tv", "set_input", "Netflix", ("media_player", "select_source", {"source": "Netflix"})),
    ("scene.movie_night", "activate", None, ("scene", "turn_on", {})),
    ("script.bedtime", "activate", None, ("script", "turn_on", {})),
    ("button.doorbell_chime", "activate", None, ("button", "press", {})),
    ("lock.front_door", "unlock", None, ("lock", "unlock", {})),
    ("lock.front_door", "lock", None, ("lock", "lock", {})),
    ("alarm_control_panel.home", "arm", None, ("alarm_control_panel", "alarm_arm_away", {})),
    ("vacuum.robi", "start", None, ("vacuum", "start", {})),
    ("vacuum.robi", "pause", None, ("vacuum", "pause", {})),
    ("vacuum.robi", "stop", None, ("vacuum", "stop", {})),
    ("vacuum.robi", "dock", None, ("vacuum", "return_to_base", {})),
    ("vacuum.robi", "set_fan_speed", "max", ("vacuum", "set_fan_speed", {"fan_speed": "max"})),
    ("humidifier.bedroom", "turn_on", None, ("humidifier", "turn_on", {})),
    ("humidifier.bedroom", "set_mode", "sleep", ("humidifier", "set_mode", {"mode": "sleep"})),
    ("water_heater.boiler", "turn_off", None, ("water_heater", "turn_off", {})),
    ("water_heater.boiler", "set_temperature", 60, ("water_heater", "set_temperature", {"temperature": 60})),
    (
        "water_heater.boiler",
        "set_mode",
        "performance",
        ("water_heater", "set_operation_mode", {"operation_mode": "performance"}),
    ),
]


@pytest.mark.parametrize(("entity_id", "command", "value", "expected"), CALLS, ids=[f"{c[0]}-{c[1]}" for c in CALLS])
def test_each_command_calls_its_service(
    tmp_path: Path,
    ha: FakeHomeAssistant,
    entity_id: str,
    command: str,
    value: Value,
    expected: tuple[str, str, dict[str, Any]],
) -> None:
    driver, _ = connect(tmp_path, ha)
    outcome = run(driver, entity_id, command, value)
    assert outcome.ok, outcome
    domain, service, data = expected
    assert ha.service_calls == [(domain, service, {"entity_id": entity_id, **data})]
    # Home Assistant knew the service, and the entity was read before and after.
    assert ha.paths().count(f"/api/states/{entity_id}") >= 1


def test_answers_name_the_new_state_or_what_was_asked(tmp_path: Path, ha: FakeHomeAssistant) -> None:
    driver, _ = connect(tmp_path, ha)
    assert run(driver, "light.kitchen", "turn_on").text == "The Kitchen light is on at 100%."
    assert run(driver, "light.kitchen", "set_brightness", 40).text == "The Kitchen light is on at 40%."
    # Already on: nothing changes, and that is what was wanted.
    assert run(driver, "light.kitchen", "turn_on").text == "The Kitchen light is on at 40%."
    assert run(driver, "media_player.lounge_tv", "set_volume", 35).text == "The Lounge TV is on (HDMI 1), volume 35%."
    assert run(driver, "climate.hall", "set_temperature", 21.5).text == (
        "The Hall thermostat is set to heat at 21.5 degrees; it is 19.5 degrees now."
    )
    assert run(driver, "cover.blinds", "set_position", 30).text == "The Blinds is open at 30%."
    # Nothing to show for it: say what was asked, not that it happened.
    assert run(driver, "media_player.lounge_tv", "next").text == (
        "Asked Home Assistant to skip to the next track on the Lounge TV."
    )
    assert run(driver, "scene.movie_night", "activate").text == "Activated Movie night."
    assert run(driver, "script.bedtime", "activate").text == "Started Bedtime."
    assert run(driver, "button.doorbell_chime", "activate").text == "Pressed the Doorbell chime."
    listed = run(driver, "media_player.lounge_tv", "list_inputs")
    assert listed.text == "The Lounge TV can switch to: HDMI 1, Netflix, YouTube."


def test_a_state_reported_after_the_call_is_still_seen(tmp_path: Path, ha: FakeHomeAssistant) -> None:
    driver, _ = connect(tmp_path, ha)
    driver.recheck_wait_s = 0.4
    ha.lazy = 0.05  # the call answers [] and the lock reports a moment later
    assert run(driver, "lock.front_door", "unlock").text == "The Front door is unlocked."


def test_a_missing_or_unavailable_entity_is_never_called(tmp_path: Path, ha: FakeHomeAssistant) -> None:
    driver, _ = connect(tmp_path, ha)
    gone = device(driver, "light.desk")
    del ha.states["light.desk"]
    outcome = checked(driver.run(gone, "turn_on", None))
    assert (outcome.code, outcome.text) == (
        "not_found",
        "Home Assistant no longer has the Desk lamp; ask me to list the devices again.",
    )
    outcome = run(driver, "light.attic", "turn_on")
    assert (outcome.code, outcome.text) == (
        "unreachable",
        "Home Assistant says the Attic light is unavailable right now.",
    )
    assert ha.service_calls == []
    # Which is why: Home Assistant answers a call on an unknown entity with 200 and an empty list.
    hub = HubSettings(ha.url, TOKEN)
    assert HaClient(hub, budget=5).call_service("light", "turn_on", {"entity_id": "light.nowhere"}) == []
    # The next list reads the hub again.
    assert "ha:light.desk" not in {d.id for d in driver.hub_devices()}


# --------------------------------------------------------------------------- failures


KITCHEN_OFFLINE = "Home Assistant could not turn on the Kitchen light; the device may be offline."
KITCHEN_REFUSED = "Home Assistant did not accept that for the Kitchen light"
PLAIN = "text/plain; charset=utf-8"
JSON = "application/json"


@pytest.mark.parametrize(
    ("status", "content_type", "body", "code", "text"),
    [
        # Up to 2026.9 a device error is aiohttp's plain page: never spoken.
        (500, PLAIN, AIOHTTP_500, "failed", KITCHEN_OFFLINE),
        # From 2026.10, Home Assistant's own message: spoken when it is fit to.
        (500, JSON, b'{"message": "Bulb did not respond"}', "failed",
         "Home Assistant could not turn on the Kitchen light: Bulb did not respond."),
        (500, JSON, b'{"message": "Failed to reach light.kitchen at 192.168.1.50"}', "failed", KITCHEN_OFFLINE),
        (500, JSON, b'{"message": "light.kitchen did not respond"}', "failed",
         "Home Assistant could not turn on the Kitchen light: the Kitchen light did not respond."),
        (400, PLAIN, b"400: Bad Request", "bad_value", f"{KITCHEN_REFUSED}."),
        (400, JSON, b'{"message": "Brightness is out of range"}', "bad_value",
         f"{KITCHEN_REFUSED}: Brightness is out of range."),
        (502, "text/html", b"<html>Bad Gateway</html>", "unreachable", UNREACHABLE),
        (418, PLAIN, b"teapot", "failed", "Home Assistant answered with an error (HTTP 418)."),
    ],
)  # fmt: skip
def test_service_errors_in_plain_words(
    tmp_path: Path, ha: FakeHomeAssistant, status: int, content_type: str, body: bytes, code: str, text: str
) -> None:
    driver, _ = connect(tmp_path, ha)
    ha.service_error = (status, content_type, body)
    outcome = run(driver, "light.kitchen", "turn_on")
    assert (outcome.ok, outcome.code, outcome.text) == (False, code, text)
    assert "Server got itself" not in outcome.text


def test_a_refused_token_is_never_used_again(tmp_path: Path, ha: FakeHomeAssistant) -> None:
    driver, store = connect(tmp_path, ha, token=WRONG_TOKEN)
    with pytest.raises(HaError) as caught:
        driver.hub_devices()
    assert caught.value.kind == "auth" and caught.value.spoken == TOKEN_REFUSED
    assert WRONG_TOKEN not in str(caught.value)
    assert driver.describe() == TOKEN_REFUSED
    record = DeviceRecord("ha:light.kitchen", "homeassistant", "Kitchen light", "light", settings={
        "entity_id": "light.kitchen", "domain": "light", "color_modes": ["brightness"]
    })  # fmt: skip
    outcome = checked(driver.run(record, "turn_on", None))
    assert (outcome.code, outcome.text) == ("auth", TOKEN_REFUSED)
    assert checked(driver.status(record)).text == TOKEN_REFUSED
    # One failed login in Home Assistant, however often Jarvis is asked.
    assert ha.auth_failures == 1 and ha.ws_auth_failures == 0
    # A new token is used straight away.
    store.set_secret("homeassistant:hub", {"token": TOKEN})
    assert run(driver, "light.kitchen", "turn_on").ok


def test_a_token_refused_mid_way_is_not_retried(tmp_path: Path, ha: FakeHomeAssistant) -> None:
    driver, _ = connect(tmp_path, ha)
    kitchen = device(driver, "light.kitchen")
    ha.token = "a-token-made-later-in-ha"  # the user deleted Jarvis's token in Home Assistant
    assert checked(driver.run(kitchen, "turn_on", None)).text == TOKEN_REFUSED
    assert checked(driver.run(kitchen, "turn_off", None)).text == TOKEN_REFUSED
    with pytest.raises(HaError):
        driver.hub_devices()
    assert ha.auth_failures == 1


def test_a_refused_websocket_login_is_not_retried(tmp_path: Path, ha: FakeHomeAssistant) -> None:
    hub = HubSettings(ha.url, WRONG_TOKEN, admin=True, exposed_only=True)
    assert read_view(hub, want_exposed=True).is_admin is None
    assert read_view(hub, want_exposed=True).exposed is None
    assert ha.ws_auth_failures == 1 and ha.ws_sessions == 1


def test_home_assistant_stopping_or_gone(
    tmp_path: Path, ha: FakeHomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver, store = connect(tmp_path, ha)
    kitchen = device(driver, "light.kitchen")
    ha.stopping = True
    outcome = checked(driver.run(kitchen, "turn_on", None))
    assert (outcome.code, outcome.text) == ("unreachable", STARTING)
    driver.close()
    with pytest.raises(HaError) as caught:
        driver.hub_devices()
    assert caught.value.spoken == STARTING
    ha.stopping = False
    # A slow service: Home Assistant may still do it.
    monkeypatch.setattr(ha_mod, "SERVICE_TIMEOUT_S", 0.3)
    ha.service_delay = 1.0
    outcome = checked(driver.run(kitchen, "turn_on", None))
    assert (outcome.code, outcome.text) == (
        "timeout",
        "Home Assistant did not answer in time; it may still turn on the Kitchen light.",
    )
    ha.service_delay = 0.0
    save_hub(store, f"http://127.0.0.1:{closed_port()}")
    outcome = checked(driver.run(kitchen, "turn_on", None))
    assert (outcome.code, outcome.text) == ("unreachable", UNREACHABLE)
    assert driver.describe() == UNREACHABLE


def test_banned_computer(tmp_path: Path, ha: FakeHomeAssistant) -> None:
    driver, _ = connect(tmp_path, ha)
    ha.service_error = (403, "text/plain", b"403: Forbidden")
    outcome = run(driver, "light.kitchen", "turn_on")
    assert (outcome.code, outcome.text) == ("auth", BANNED)


def test_codes_are_never_sent_and_saying_so_is_the_answer(tmp_path: Path, ha: FakeHomeAssistant) -> None:
    driver, _ = connect(tmp_path, ha)
    outcome = run(driver, "lock.shed", "unlock")
    assert (outcome.code, outcome.text) == (
        "unsupported",
        "Home Assistant needs a code to unlock the Shed lock, and Jarvis does not send codes. "
        "Set a default code for it in Home Assistant, or use the Home Assistant app.",
    )
    ha.code_error = (400, "application/json", b'{"message": "Code is required to disarm"}')  # 2026.10 and later
    assert run(driver, "alarm_control_panel.home", "disarm").text.startswith(
        "Home Assistant needs a code to disarm the House alarm"
    )
    # This panel arms without a code.
    assert run(driver, "alarm_control_panel.home", "arm").text == "The House alarm is armed (away)."
    assert all("code" not in data for _, _, data in ha.service_calls)


def test_bad_values_are_refused_before_calling(tmp_path: Path, ha: FakeHomeAssistant) -> None:
    driver, _ = connect(tmp_path, ha)
    assert run(driver, "light.kitchen", "set_color", "not a colour!").code == "bad_value"
    assert run(driver, "light.kitchen", "set_color_temp", "hot").code == "bad_value"
    outcome = run(driver, "light.porch", "set_brightness", 50)
    assert (outcome.code, outcome.text) == ("unsupported", "The Porch light can't set brightness.")
    assert ha.service_calls == []


def test_status_in_words(tmp_path: Path, ha: FakeHomeAssistant) -> None:
    driver, _ = connect(tmp_path, ha)
    ha.states["media_player.lounge_tv"].update(state="playing")
    ha.states["media_player.lounge_tv"]["attributes"].update(
        media_title="Dark", app_name="Netflix", is_volume_muted=True
    )
    expected = {
        "light.desk": "The Desk lamp is on at 50%.",
        "light.porch": "The Porch light is off.",
        "light.attic": "Home Assistant says the Attic light is unavailable.",
        "climate.hall": "The Hall thermostat is set to heat at 20 degrees; it is 19.5 degrees now.",
        "media_player.lounge_tv": "The Lounge TV is playing Dark (Netflix), volume 20%, muted.",
        "cover.garage_door": "The Garage door is closed.",
        "lock.front_door": "The Front door is locked.",
        "alarm_control_panel.home": "The House alarm is disarmed.",
        "vacuum.robi": "The Robi is docked (battery 90%).",
        "scene.movie_night": "Movie night is a Home Assistant scene; activate runs it.",
        "script.bedtime": "Bedtime is not running.",
        "button.doorbell_chime": "The Doorbell chime is a button; activate presses it.",
        "water_heater.boiler": "The Boiler is on eco at 55 degrees.",
        "humidifier.bedroom": "The Bedroom humidifier is off.",
    }
    assert {eid: status(driver, eid).text for eid in expected} == expected
    gone = device(driver, "light.porch")
    del ha.states["light.porch"]
    assert checked(driver.status(gone)).code == "not_found"


def test_missing_settings_need_setup_and_send_nothing(tmp_path: Path, ha: FakeHomeAssistant) -> None:
    store = make_store(tmp_path)
    driver = HomeAssistantDriver(DriverContext(store, tmp_path))
    record = DeviceRecord("ha:light.desk", "homeassistant", "Desk lamp", "light", settings={
        "entity_id": "light.desk", "domain": "light"
    })  # fmt: skip
    store.update(lambda c: c.hubs.__setitem__("homeassistant", {}))
    with pytest.raises(HaError) as caught:
        driver.hub_devices()
    assert caught.value.spoken == NO_ADDRESS
    save_hub(store, ha.url, token=None)
    assert (driver.run(record, "turn_on", None).code, driver.describe()) == ("needs_setup", NO_TOKEN)
    # A token http.client would refuse, quoting it in the error: never sent.
    for bad in ("line\nbreak-in-a-token-value", f"{TOKEN}\n", f"{TOKEN}\r\nX-Injected: 1"):
        store.set_secret("homeassistant:hub", {"token": bad})
        outcome = driver.run(record, "turn_on", None)
        assert outcome.code == "needs_setup" and "make a new one" in outcome.text
    # An entity id that is not one is not put in a path either.
    odd = DeviceRecord("ha:x", "homeassistant", "Odd", "light", settings={"entity_id": "light.x\n", "domain": "light"})
    store.set_secret("homeassistant:hub", {"token": TOKEN})
    assert driver.run(odd, "turn_on", None).code == "needs_setup"
    assert driver.status(odd).code == "needs_setup"
    assert ha.requests == []


# --------------------------------------------------------------------------- through the service


def test_through_the_home_service(tmp_path: Path, ha: FakeHomeAssistant) -> None:
    store = make_store(tmp_path)
    save_hub(store, ha.url)

    def build(ctx: DriverContext) -> HomeAssistantDriver:
        driver = HomeAssistantDriver(ctx)
        driver.recheck_wait_s = 0.05
        return driver

    service = HomeService(tmp_path, store=store, drivers={"homeassistant": build})
    try:
        text = service.handle({"action": "list"})["text"]
        assert text.startswith("22 home devices:")
        assert (
            "- Kitchen light (light, Kitchen) id=ha:light.kitchen: turn_on, turn_off, toggle, set_brightness <0-100>"
        ) in text
        assert "- Front door (lock, Hall) id=ha:lock.front_door: lock, unlock*" in text
        assert "Outdoor temperature" not in text
        reply = service.handle({"action": "do", "device": "front door", "command": "unlock"})
        assert reply["result"] == "confirm" and ha.service_calls == []
        reply = service.handle({"action": "do", "device": "front door", "command": "unlock", "confirmed": True})
        assert (reply["result"], reply["text"]) == ("done", "The Front door is unlocked.")
        assert service.handle({"action": "do", "device": "garage door", "command": "open"})["result"] == "confirm"
        reply = service.handle({"action": "do", "device": "kitchen light", "command": "brightness", "value": "40%"})
        assert reply["text"] == "The Kitchen light is on at 40%."
        reply = service.handle({"action": "status", "device": "hall thermostat"})
        assert reply["text"].startswith("The Hall thermostat is set to heat")
        assert "Home Assistant connected (22 devices)" in service.handle({"action": "info"})["text"]
        ha.token = "a-token-made-later-in-ha"
        service.handle({"action": "reload"})
        text = service.handle({"action": "list"})["text"]
        assert "No home devices are set up yet." in text and "Home Assistant rejected Jarvis's token" in text
    finally:
        service.close()


# --------------------------------------------------------------------------- helpers


def test_addresses_typed_by_the_user() -> None:
    assert clean_url("homeassistant.local") == "http://homeassistant.local:8123"
    assert clean_url(" 192.168.1.10 ") == "http://192.168.1.10:8123"
    assert clean_url("192.168.1.10:8124") == "http://192.168.1.10:8124"
    assert clean_url("http://ha.local") == "http://ha.local"
    assert clean_url("https://ha.example.com/lovelace/0") == "https://ha.example.com"
    assert clean_url("HTTP://HA.local:8123/") == "http://ha.local:8123"
    assert clean_url("fe80::1") == "http://[fe80::1]:8123"
    for bad in ("", "not an address", "ftp://ha.local", "http://user:pw@ha.local", "http://", "ha.local:99999"):
        assert clean_url(bad) is None, bad
    assert address_candidates("ha.local") == ["http://ha.local:8123", "http://ha.local"]
    assert address_candidates("ha.local:8123") == ["http://ha.local:8123"]
    assert address_candidates("https://ha.local") == ["https://ha.local"]
    assert websocket_url("http://ha.local:8123") == "ws://ha.local:8123/api/websocket"
    assert websocket_url("https://ha.example.com") == "wss://ha.example.com/api/websocket"


def test_colours_and_colour_temperatures() -> None:
    assert colour_data("#ff8800") == {"rgb_color": [255, 136, 0]}
    assert colour_data("255, 0, 10") == {"rgb_color": [255, 0, 10]}
    assert colour_data("Warm-White") == {"color_name": "warmwhite"}
    assert colour_data("300,0,0") is None and colour_data("#12") is None and colour_data("r2d2") is None
    lamp = DeviceRecord("ha:light.x", "homeassistant", "X", "light", settings={"min_kelvin": 2000, "max_kelvin": 6000})
    assert [kelvin_for(lamp, v) for v in ("warm", "neutral", "Cool", "25", "25%", "2700K", "9000", "1500 kelvin")] == [
        2000,
        4000,
        6000,
        3000,
        3000,
        2700,
        6000,
        2000,
    ]
    assert kelvin_for(lamp, "hot") is None


# --------------------------------------------------------------------------- setup


def wizard_ctx(tmp_path: Path) -> DriverContext:
    return DriverContext(make_store(tmp_path), tmp_path)


def saved(ctx: DriverContext) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    return ctx.store.load().hubs.get("homeassistant"), ctx.store.secret("homeassistant:hub")


def test_wizard_connects_an_administrator_to_the_exposed_devices(tmp_path: Path, ha: FakeHomeAssistant) -> None:
    ctx = wizard_ctx(tmp_path)
    ui = ScriptedPrompter([ha.url, TOKEN, None])  # None: the default (yes, only exposed devices)
    ha_mod.wizard(ui, ctx)
    assert saved(ctx) == (
        {"url": ha.url, "verify_tls": True, "admin": True, "exposed_only": True},
        {"token": TOKEN},
    )
    assert "This token belongs to a Home Assistant administrator." in ui.said
    assert any(
        line.startswith("Jarvis will see 7 devices: Desk lamp (Living room), Front door (Hall)") for line in ui.said
    )
    assert "Long-lived access tokens" in ui.text and "Create token" in ui.text
    assert TOKEN not in ui.text and TOKEN not in ctx.store.devices_path.read_text(encoding="utf-8")
    assert ha.auth_failures == 0 and "/api/template" not in ha.paths()
    assert ui.said[-1].startswith("Saved. Jarvis can use Home Assistant now.")
    # What the wizard saved is what the helper uses.
    assert names(HomeAssistantDriver(ctx).hub_devices()) == EXPOSED_NAMES


def test_wizard_for_a_user_who_is_not_an_administrator(tmp_path: Path, ha: FakeHomeAssistant) -> None:
    ha.admin = False
    ctx = wizard_ctx(tmp_path)
    ui = ScriptedPrompter([ha.url, TOKEN])  # no question about Assist: it cannot be read
    ha_mod.wizard(ui, ctx)
    assert saved(ctx)[0] == {"url": ha.url, "verify_tls": True, "admin": False, "exposed_only": False}
    assert "is not an administrator" in ui.text and "Jarvis will see 22 devices" in ui.text
    assert "homeassistant/expose_entity/list" not in ha.ws_types and "/api/template" not in ha.paths()


def test_wizard_with_a_wrong_token_saves_nothing(tmp_path: Path, ha: FakeHomeAssistant) -> None:
    ctx = wizard_ctx(tmp_path)
    ui = ScriptedPrompter([ha.url, WRONG_TOKEN, False])
    ha_mod.wizard(ui, ctx)
    assert "Home Assistant did not accept that token." in ui.text and ui.said[-1] == "Nothing was saved."
    assert saved(ctx) == (None, None)
    assert ha.auth_failures == 1 and WRONG_TOKEN not in ui.text


def test_wizard_takes_a_second_token(tmp_path: Path, ha: FakeHomeAssistant) -> None:
    ctx = wizard_ctx(tmp_path)
    ui = ScriptedPrompter([ha.url, WRONG_TOKEN, True, TOKEN, False])
    ha_mod.wizard(ui, ctx)
    assert saved(ctx) == ({"url": ha.url, "verify_tls": True, "admin": True, "exposed_only": False}, {"token": TOKEN})
    assert "Jarvis will see 22 devices" in ui.text


def test_wizard_says_when_nothing_answers(tmp_path: Path, ha: FakeHomeAssistant) -> None:
    ctx = wizard_ctx(tmp_path)
    address = f"http://127.0.0.1:{closed_port()}"
    ui = ScriptedPrompter([address, False])
    ha_mod.wizard(ui, ctx)
    assert f"No answer from {address}." in ui.text and ui.said[-1] == "Nothing was saved."
    assert "Paste the token" not in ui.asked and saved(ctx) == (None, None)


def test_wizard_rejects_bad_addresses_and_tokens(tmp_path: Path, ha: FakeHomeAssistant) -> None:
    ctx = wizard_ctx(tmp_path)
    ui = ScriptedPrompter(["not an address", ha.url, "short", "two words but long enough to pass", ""])
    ha_mod.wizard(ui, ctx)
    assert ui.text.count("That does not look like an address.") == 1
    assert ui.text.count("That does not look like a Home Assistant token") == 2
    assert ui.said[-1] == "Nothing was saved." and saved(ctx) == (None, None)
    assert "/api/" not in ha.paths()  # no token was worth sending


def test_wizard_stopped_halfway_saves_nothing(tmp_path: Path, ha: FakeHomeAssistant) -> None:
    ctx = wizard_ctx(tmp_path)
    ui = ScriptedPrompter([ha.url, TOKEN])  # quits at the Assist question
    with pytest.raises(EOFError):
        ha_mod.wizard(ui, ctx)
    assert saved(ctx) == (None, None)


def test_wizard_changes_the_settings_then_disconnects(tmp_path: Path, ha: FakeHomeAssistant) -> None:
    ctx = wizard_ctx(tmp_path)
    ha_mod.wizard(ScriptedPrompter([ha.url, TOKEN, True]), ctx)
    # Again: keep the address (the default) and the token, and see everything.
    ui = ScriptedPrompter(["Change", "", True, False])
    ha_mod.wizard(ui, ctx)
    assert f"Home Assistant is connected at {ha.url}." in ui.asked
    assert "Paste the token" not in ui.asked
    assert saved(ctx)[0] == {"url": ha.url, "verify_tls": True, "admin": True, "exposed_only": False}
    # The kept token was deleted in Home Assistant: a new one is asked for.
    ha.token = "a-token-made-later-in-ha"
    ui = ScriptedPrompter(["Change", "", True, "a-token-made-later-in-ha", None])
    ha_mod.wizard(ui, ctx)
    assert "Home Assistant did not accept that token." in ui.text and "Paste the token" in ui.asked
    assert saved(ctx)[1] == {"token": "a-token-made-later-in-ha"}
    assert saved(ctx)[0] == {"url": ha.url, "verify_tls": True, "admin": True, "exposed_only": False}
    ui = ScriptedPrompter(["Disconnect", True])
    ha_mod.wizard(ui, ctx)
    assert saved(ctx) == (None, None)
    assert ui.said[-1].startswith("Home Assistant is disconnected.")
    ui = ScriptedPrompter([])  # nothing connected: straight to the address (then the user quits)
    with pytest.raises(EOFError):
        ha_mod.wizard(ui, ctx)
    assert ui.asked == ["Home Assistant's address, as you open it in your browser"]


def self_signed(tmp_path: Path) -> ssl.SSLContext:
    """A server context with a certificate made up on the spot (nothing a client trusts)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "homeassistant.local")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("homeassistant.local")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "ha-cert.pem", tmp_path / "ha-cert-key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    return context


@pytest.fixture
def ha_tls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeHomeAssistant]:
    fake = FakeHomeAssistant(tls=self_signed(tmp_path))
    fake.start()
    monkeypatch.setattr(ha_mod, "websocket_url", fake.websocket_url)
    yield fake
    fake.stop()


def test_wizard_offers_to_skip_the_check_of_a_self_signed_certificate(
    tmp_path: Path, ha_tls: FakeHomeAssistant
) -> None:
    ctx = wizard_ctx(tmp_path / "data")
    ui = ScriptedPrompter([ha_tls.url, False, False])  # do not skip; no other address
    ha_mod.wizard(ui, ctx)
    assert "self-signed" in ui.text and ui.said[-1] == "Nothing was saved."
    ui = ScriptedPrompter([ha_tls.url, True, TOKEN, True])
    ha_mod.wizard(ui, ctx)
    assert saved(ctx)[0] == {"url": ha_tls.url, "verify_tls": False, "admin": True, "exposed_only": True}
    # The helper then talks to it over https and wss without checking the certificate.
    driver = HomeAssistantDriver(ctx)
    driver.recheck_wait_s = 0.05
    assert names(driver.hub_devices()) == EXPOSED_NAMES
    assert run(driver, "light.kitchen", "turn_on").text == "The Kitchen light is on at 100%."


def test_a_certificate_that_cannot_be_checked_is_refused_by_default(tmp_path: Path, ha_tls: FakeHomeAssistant) -> None:
    store = make_store(tmp_path)
    save_hub(store, ha_tls.url)  # verify_tls true
    driver = HomeAssistantDriver(DriverContext(store, tmp_path))
    with pytest.raises(HaError) as caught:
        driver.hub_devices()
    assert caught.value.kind == "tls"
    assert ha_tls.service_calls == [] and ha_tls.auth_failures == 0


def test_timing_budget_fits_the_service_limit() -> None:
    # A listing and a command must each end inside the service's 20 s.
    assert ha_mod.STATES_TIMEOUT_S + ha_mod.WS_BUDGET_S <= ha_mod.LIST_BUDGET_S < 20
    assert ha_mod.CALL_BUDGET_S + ha_mod.CALL_MARGIN_S <= 20
    assert ha_mod.STATE_TIMEOUT_S * 2 + ha_mod.SERVICE_TIMEOUT_S <= ha_mod.CALL_BUDGET_S + 1
