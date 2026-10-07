"""The shared vocabulary of home control: devices, commands, tiers and outcomes.

Everything here is plain data with no network or third-party imports, so the
service, the drivers, the wizard and the tests can all share it cheaply.
Texts in an ``Outcome`` reach Claude and may be spoken: they name devices and
states in plain words and never carry a secret, an address or a key.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Literal

# How much asking a command needs, after PLAN.md's permission tiers. "Ask by
# voice" has no safe mechanism yet (a TV can say "yes"), so home control uses
# the stricter on-screen confirmation wherever it would have asked.
Tier = Literal["free", "screen", "never"]
TIERS: tuple[Tier, ...] = ("free", "screen", "never")

# What a command's value is: nothing, 0-100, a number in a range, free text, or one of a list.
ValueKind = Literal["none", "percent", "number", "text", "choice"]

# Why a request did not simply succeed. "confirm" is not a failure: the mod
# asks the user on screen, then repeats the request with confirmed=true.
Code = Literal[
    "ok",
    "confirm",
    "not_found",
    "ambiguous",
    "unsupported",
    "bad_value",
    "unreachable",
    "auth",
    "needs_setup",
    "timeout",
    "busy",
    "refused",
    "failed",
]

Value = str | int | float | bool | None

# Device kinds, for listing and for "the TV" style requests.
KINDS = (
    "tv",
    "media_player",
    "speaker",
    "light",
    "switch",
    "button",
    "plug",
    "fan",
    "cover",
    "climate",
    "heater",
    "humidifier",
    "lock",
    "alarm",
    "garage",
    "vacuum",
    "scene",
    "remote",
    "sensor",
    "other",
)

TEXT_MAX = 500


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """One thing a device can do, as the model sees it in ``list``."""

    name: str
    value: ValueKind = "none"
    choices: tuple[str, ...] = ()
    tier: Tier = "free"
    # How to phrase the value when "percent" or "choice" does not say it, e.g. "an app name".
    hint: str = ""
    low: float | None = None
    high: float | None = None

    def usage(self) -> str:
        """``set_volume <0-100>``, ``launch_app <an app name>``, ``turn_on``, ``press (what it does)``."""
        if self.value == "none":
            return f"{self.name} ({self.hint})" if self.hint else self.name
        if self.value == "percent":
            what = "0-100"
        elif self.value == "choice" and self.choices:
            shown = list(self.choices[:8])
            what = "|".join(shown) + ("|..." if len(self.choices) > 8 else "")
        elif self.value == "number" and self.low is not None and self.high is not None:
            what = f"{_num(self.low)}-{_num(self.high)}"
        else:
            what = self.hint or self.value
        return f"{self.name} <{what}>"


@dataclass(slots=True)
class Outcome:
    """What a driver call came to. ``text`` is speakable and secret-free."""

    ok: bool
    text: str
    code: Code = "ok"

    @classmethod
    def done(cls, text: str) -> Outcome:
        return cls(True, text, "ok")

    @classmethod
    def fail(cls, code: Code, text: str) -> Outcome:
        return cls(False, text, code)


@dataclass(slots=True)
class DeviceRecord:
    """A device Jarvis knows. ``settings`` holds the driver's own non-secret data
    (address, identifiers, data-point map); secrets live in the credential store
    under the key ``"<driver>:<id>"``."""

    id: str
    driver: str
    name: str
    kind: str = "other"
    room: str | None = None
    aliases: list[str] = field(default_factory=list)
    # A floor for every command of this device (e.g. "screen" for a heater's plug).
    confirm: Tier | None = None
    settings: dict[str, Any] = field(default_factory=dict)

    @property
    def secret_key(self) -> str:
        return f"{self.driver}:{self.id}"

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {"id": self.id, "driver": self.driver, "name": self.name, "kind": self.kind}
        if self.room:
            data["room"] = self.room
        if self.aliases:
            data["aliases"] = list(self.aliases)
        if self.confirm is not None:
            data["confirm"] = self.confirm
        if self.settings:
            data["settings"] = self.settings
        return data

    @classmethod
    def from_json(cls, data: Any) -> DeviceRecord:
        if not isinstance(data, dict):
            raise ValueError("a device entry must be an object")
        ident, driver, name = data.get("id"), data.get("driver"), data.get("name")
        if not all(isinstance(v, str) and v.strip() for v in (ident, driver, name)):
            raise ValueError("a device entry needs a non-empty id, driver and name")
        assert isinstance(ident, str) and isinstance(driver, str) and isinstance(name, str)
        kind = data.get("kind") if isinstance(data.get("kind"), str) else "other"
        room = data.get("room") if isinstance(data.get("room"), str) and data.get("room") else None
        aliases = [a for a in data.get("aliases") or [] if isinstance(a, str) and a.strip()]
        confirm = data.get("confirm") if data.get("confirm") in TIERS else None
        settings = data.get("settings") if isinstance(data.get("settings"), dict) else {}
        return cls(ident.strip(), driver.strip(), name.strip(), kind or "other", room, aliases, confirm, settings)


@dataclass(slots=True)
class HomeConfig:
    """The contents of ``<dataDir>/home/devices.json`` (no secrets)."""

    devices: list[DeviceRecord] = field(default_factory=list)
    # Hubs that bring their own devices, by driver name, e.g. {"homeassistant": {"url": ...}}.
    hubs: dict[str, dict[str, Any]] = field(default_factory=dict)

    VERSION = 1

    def device(self, device_id: str) -> DeviceRecord | None:
        return next((d for d in self.devices if d.id == device_id), None)

    def ids(self) -> set[str]:
        return {d.id for d in self.devices}

    def upsert(self, device: DeviceRecord) -> None:
        """Adds the device, or replaces the one with the same id."""
        for i, existing in enumerate(self.devices):
            if existing.id == device.id:
                self.devices[i] = device
                return
        self.devices.append(device)

    def remove(self, device_id: str) -> DeviceRecord | None:
        found = self.device(device_id)
        if found is not None:
            self.devices = [d for d in self.devices if d.id != device_id]
        return found

    def to_json(self) -> dict[str, Any]:
        return {
            "version": self.VERSION,
            "devices": [d.to_json() for d in self.devices],
            "hubs": self.hubs,
        }

    @classmethod
    def from_json(cls, data: Any) -> HomeConfig:
        """Reads a saved file; entries that do not parse are dropped, not fatal."""
        if not isinstance(data, dict):
            raise ValueError("devices.json must hold an object")
        devices: list[DeviceRecord] = []
        seen: set[str] = set()
        for entry in data.get("devices") or []:
            try:
                device = DeviceRecord.from_json(entry)
            except ValueError:
                continue
            if device.id not in seen:
                seen.add(device.id)
                devices.append(device)
        hubs = {k: v for k, v in (data.get("hubs") or {}).items() if isinstance(k, str) and isinstance(v, dict)}
        return cls(devices, hubs)


# --------------------------------------------------------------------------- names

_FILLER = {"the", "my", "a", "an", "our"}


def normalize(text: str) -> str:
    """Lowercase words without accents or punctuation: "The Living-Room TV!" -> "living room tv"."""
    decomposed = unicodedata.normalize("NFKD", text)
    plain = "".join(c for c in decomposed if not unicodedata.combining(c)).lower()
    words = re.findall(r"[a-z0-9֐-׿]+", plain)
    kept = [w for w in words if w not in _FILLER]
    return " ".join(kept or words)


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", normalize(text)).strip("-")
    return slug[:40].strip("-") or "device"


def unique_id(existing: set[str], driver: str, name: str) -> str:
    """``<driver>-<name-slug>``, with ``-2``, ``-3``... when taken."""
    base = f"{driver}-{slugify(name)}"
    candidate, n = base, 2
    while candidate in existing:
        candidate, n = f"{base}-{n}", n + 1
    return candidate


# --------------------------------------------------------------------------- values


def _num(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def parse_value(spec: CommandSpec, value: Value) -> tuple[Value, str | None]:
    """Coerces a tool argument for ``spec``: returns (value, None) or (None, a spoken error).

    Models send numbers as strings and say "50%" or "half"; both are accepted.
    """
    if spec.value == "none":
        return None, None
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, f"{spec.name} needs a value ({spec.usage()})."
    if spec.value == "text":
        text = str(value).strip()
        if len(text) > TEXT_MAX:
            return None, f"That is too long for {spec.name} (at most {TEXT_MAX} characters)."
        return text, None
    if spec.value == "choice":
        text = str(value).strip()
        if not spec.choices:
            return text, None
        wanted = normalize(text)
        for choice in spec.choices:
            if normalize(choice) == wanted:
                return choice, None
        matches = [c for c in spec.choices if wanted and wanted in normalize(c)]
        if len(matches) == 1:
            return matches[0], None
        return None, f"{spec.name} takes one of: {', '.join(spec.choices)}."
    number = _to_number(value)
    if number is None:
        return None, f"{spec.name} needs a number ({spec.usage()})."
    if spec.value == "percent":
        low, high = 0.0, 100.0
    else:
        low = spec.low if spec.low is not None else float("-inf")
        high = spec.high if spec.high is not None else float("inf")
    if not low <= number <= high:
        return None, f"{spec.name} takes {_num(low)} to {_num(high)}, not {_num(number)}."
    if spec.value == "percent":
        return round(number), None
    return (int(number) if number.is_integer() else number), None


_WORD_NUMBERS = {"zero": 0.0, "half": 50.0, "full": 100.0, "max": 100.0, "maximum": 100.0, "min": 0.0, "minimum": 0.0}


def _to_number(value: Value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().lower().rstrip("%").replace(",", ".").strip()
    if text in _WORD_NUMBERS:
        return _WORD_NUMBERS[text]
    match = re.fullmatch(r"[+-]?\d+(?:\.\d+)?", text.removesuffix("°c").removesuffix("°").removesuffix("c").strip())
    return float(match.group(0)) if match else None


# Words people (and models) use for the canonical command names.
COMMAND_SYNONYMS: dict[str, str] = {
    "on": "turn_on",
    "power_on": "turn_on",
    "switch_on": "turn_on",
    "wake": "turn_on",
    "off": "turn_off",
    "power_off": "turn_off",
    "switch_off": "turn_off",
    "sleep": "turn_off",
    "standby": "turn_off",
    "playpause": "play_pause",
    "toggle_play": "play_pause",
    "resume": "play",
    "skip": "next",
    "next_track": "next",
    "previous_track": "previous",
    "prev": "previous",
    "volume": "set_volume",
    "louder": "volume_up",
    "quieter": "volume_down",
    "launch": "launch_app",
    "open_app": "launch_app",
    "start_app": "launch_app",
    "apps": "list_apps",
    "input": "set_input",
    "source": "set_input",
    "inputs": "list_inputs",
    "type": "type_text",
    "brightness": "set_brightness",
    "dim": "set_brightness",
    "color": "set_color",
    "colour": "set_color",
    "set_colour": "set_color",
    "color_temp": "set_color_temp",
    "temperature": "set_temperature",
    "mode": "set_mode",
    "fan_speed": "set_fan_speed",
    "position": "set_position",
    "run": "activate",
    "trigger": "activate",
    "return_home": "dock",
    "push": "press",
    "click": "press",
    "tap": "press",
    "ok": "select",
    "enter": "select",
}


def canonical_command(text: str) -> str:
    name = re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")
    return COMMAND_SYNONYMS.get(name, name)
