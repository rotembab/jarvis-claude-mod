"""Tuya data points: what a device's numbered values mean, and how Jarvis's commands become them.

A Tuya device is a set of data points (DPs), numbered values such as 20 for
power and 22 for brightness. Smart Life knows each device's map from DP number
to a standard code (``switch_led``, ``bright_value_v2``...) with its type and
range, and, for the local protocol, how the device's own raw values differ from
the cloud's (``valueConvert`` and ``enumMappingMap``). The wizard keeps a compact
copy of that map in the device's settings, and everything here works from that
copy alone: no network and no third-party import.

Local values are not always the cloud's: a v2 colour is 12 hex characters
``hhhhssssvvvv`` (h 0-360, s and v 0-1000), a v1 colour is 14, ``rrggbb0hhhssvv``
(s and v 0-255); enums may use the vendor's own words; many curtains count
their position the other way round.
"""

from __future__ import annotations

import colorsys
import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .model import CommandSpec, DeviceRecord, normalize

DpEntry = dict[str, Any]
DpMap = dict[str, DpEntry]

# --------------------------------------------------------------------------- categories

# Tuya's product categories (see Home Assistant's tuya/const.py) and the kind Jarvis gives them.
LIGHT_CATEGORIES = frozenset(
    {"dj", "dc", "dd", "xdd", "fwd", "dsd", "gyd", "tyndj", "tgq", "tgkg", "mbd", "hcdd", "tyd", "qjdcz", "hxd"}
)
CATEGORY_KINDS: dict[str, str] = {
    **dict.fromkeys(LIGHT_CATEGORIES, "light"),
    "kg": "switch",
    "tdq": "switch",
    "dlq": "switch",
    "znjdq": "switch",
    "wkcz": "switch",
    "cz": "plug",
    "pc": "plug",
    "fs": "fan",
    "fsd": "fan",
    "fskg": "fan",
    "ks": "fan",
    "kj": "fan",
    "xfj": "fan",
    "cl": "cover",
    "clkg": "cover",
    "jdcljqr": "cover",
    "ckmkzq": "garage",
    "kt": "climate",
    "ktkzq": "climate",
    "wk": "climate",
    "wkf": "climate",
    "ntq": "climate",
    "qn": "heater",
    "rs": "heater",
    "dbl": "heater",
    "bgl": "heater",
    "dr": "heater",
    "yb": "heater",
    "jsq": "humidifier",
    "cs": "humidifier",
    "sd": "vacuum",
}
# Categories the wizard leaves out, with the reason it gives.
SKIP_CATEGORIES: dict[str, str] = {
    "wnykq": "ir",
    "ykq": "ir",
    "wg2": "hub",
    "wg": "hub",
    "wfcon": "hub",
    **dict.fromkeys(
        (
            "wsdcg",
            "mcs",
            "pir",
            "ywbj",
            "sj",
            "rqbj",
            "cobj",
            "co2bj",
            "ldcg",
            "hps",
            "pm2.5",
            "jwbj",
            "zd",
            "sos",
            "wxkg",
            "cjkg",
            "qxj",
            "voc",
            "hjjcy",
            "ylcg",
            "ywcgq",
            "zwjcy",
            "jqbj",
        ),
        "sensor",
    ),
    **dict.fromkeys(("ms", "jtmsbh", "jtmspro", "videolock", "photolock", "gyms", "hotelms", "bxx"), "lock"),
    **dict.fromkeys(("sp", "dghsxj"), "camera"),
}
SKIP_REASONS: dict[str, str] = {
    "ir": "an IR remote: run what it does through Smart Life scenes",
    "hub": "a hub: the devices paired to it are added on their own",
    "sensor": "a sensor, with nothing to switch",
    "lock": "a lock, which Tuya does not open over the home network",
    "camera": "a camera",
    "none": "nothing Jarvis can control on it over the home network",
}

# --------------------------------------------------------------------------- the saved map

_TYPES = {
    "boolean": "bool",
    "bool": "bool",
    "integer": "int",
    "value": "int",
    "int": "int",
    "enum": "enum",
    "json": "str",
    "string": "str",
    "str": "str",
    "raw": "raw",
    "bitmap": "bitmap",
    "fault": "bitmap",
}

# The codes Jarvis uses; the rest of a device's DPs stay out of devices.json.
KNOWN_CODES = re.compile(
    r"switch(?:_led)?(?:_\d+)?|switch_usb\d+|switch_fan|fan_switch|light|power|power_go|pause|switch_charge"
    r"|switch_spray|work_mode|bright_value(?:_v2|_\d)?|temp_value(?:_v2|_\d)?|colour_data(?:_v2)?"
    r"|control|mach_operate|percent_control|percent_state|position|control_back_mode|situation_set"
    r"|doorcontact_state|fan_speed(?:_percent|_enum)?|speed|level|windspeed|mode|temp_set|temp_current"
    r"|humidity_set|dehumidify_set_value|humidity_current|cur_power|status|electricity_left|battery_percentage"
)

# Tuya's standard ranges, for devices whose description leaves them out.
DEFAULT_RANGES: dict[str, tuple[float, float]] = {
    "bright_value": (25, 255),
    "bright_value_v2": (10, 1000),
    "bright_value_1": (10, 1000),
    "temp_value": (0, 255),
    "temp_value_v2": (0, 1000),
    "temp_value_1": (0, 1000),
    "percent_control": (0, 100),
    "percent_state": (0, 100),
    "position": (0, 100),
    "fan_speed_percent": (1, 100),
}
# Local conversions (tuya_sharing's strategy_repo) whose raw range is fixed whatever the cloud says.
CONVERT_RANGES: dict[str, tuple[float, float]] = {"hb_range_v1": (25, 255), "hb_range_v2": (0, 255)}
# Local colour encodings named by the conversion.
COLOUR_FORMATS = {"dj_v2_color_alg": "hsv16", "dj_v1_hsv_alg": "rgb8"}


def norm_type(value: Any) -> str | None:
    return _TYPES.get(str(value).strip().lower()) if value else None


def _json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return json.loads(value)
        except ValueError:
            return None
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    if isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return None
        return number if math.isfinite(number) else None
    return None


def _int(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None and number.is_integer() else None


def _whole(value: float) -> int | float:
    return int(value) if float(value).is_integer() else value


def status_key(status_format: Any) -> str | None:
    """The standard code a ``statusFormat`` such as ``{"switch_led":"$"}`` reports."""
    data = _json(status_format)
    if isinstance(data, dict) and data:
        key = next(iter(data))
        return key if isinstance(key, str) and key else None
    return None


def relations_from_strategy(strategy: Mapping[Any, Any]) -> list[dict[str, Any]]:
    """Plain DP relations from tuya_sharing's ``device.local_strategy`` (dpId -> conversion)."""
    relations = []
    for dp_id, item in strategy.items():
        dp = _int(dp_id)
        if dp is None or not isinstance(item, Mapping):
            continue
        config = item.get("config_item") if isinstance(item.get("config_item"), Mapping) else {}
        code = status_key(config.get("statusFormat")) or item.get("status_code")
        if isinstance(code, str) and code:
            relations.append(
                {
                    "dp": dp,
                    "code": code,
                    "type": config.get("valueType"),
                    "desc": config.get("valueDesc"),
                    "enum": config.get("enumMappingMap"),
                    "convert": item.get("value_convert"),
                }
            )
    return relations


def relations_from_dtos(dtos: Iterable[Any]) -> list[dict[str, Any]]:
    """Plain DP relations from the ``dpStatusRelationDTOS`` list, keeping the DPs the device reports locally."""
    relations = []
    for entry in dtos:
        if not isinstance(entry, Mapping) or entry.get("supportLocal") is False:
            continue
        dp = _int(entry.get("dpId"))
        code = status_key(entry.get("statusFormat")) or entry.get("statusCode") or entry.get("dpCode")
        if dp is None or not isinstance(code, str) or not code:
            continue
        relations.append(
            {
                "dp": dp,
                "code": code,
                "type": entry.get("valueType"),
                "desc": entry.get("valueDesc"),
                "enum": entry.get("enumMappingMap"),
                "convert": entry.get("valueConvert"),
            }
        )
    return relations


def build_dp_map(
    relations: Iterable[Mapping[str, Any]],
    functions: Mapping[str, Mapping[str, Any]],
    status: Mapping[str, Mapping[str, Any]],
) -> DpMap:
    """The compact map saved in a device's settings: code -> {dp, type, range...}.

    ``relations`` come from the local strategy (DP numbers and local encodings);
    ``functions`` (writable codes) and ``status`` (readable codes) are the cloud
    spec, code -> {"type", "values", "dp_id"}, used for ranges the relations
    lack and for codes only the spec numbers.
    """
    result: DpMap = {}
    for relation in relations:
        code, dp = relation.get("code"), _int(relation.get("dp"))
        if not isinstance(code, str) or dp is None or code in result or not KNOWN_CODES.fullmatch(code):
            continue
        cloud = functions.get(code) or status.get(code) or {}
        entry = _entry(code, dp, relation, cloud)
        if entry is not None:
            if functions and code not in functions:
                entry["ro"] = True
            result[code] = entry
    for code, cloud in {**status, **functions}.items():
        dp = _int(cloud.get("dp_id"))
        if code in result or dp is None or not KNOWN_CODES.fullmatch(code):
            continue
        entry = _entry(code, dp, {}, cloud)
        if entry is not None:
            if functions and code not in functions:
                entry["ro"] = True
            result[code] = entry
    return result


def _entry(code: str, dp: int, relation: Mapping[str, Any], cloud: Mapping[str, Any]) -> DpEntry | None:
    kind = norm_type(relation.get("type")) or norm_type(cloud.get("type"))
    if kind is None:
        return None
    entry: DpEntry = {"dp": dp, "type": kind}
    desc = _json(relation.get("desc"))
    desc = desc if isinstance(desc, dict) else {}
    spec = _json(cloud.get("values"))
    spec = spec if isinstance(spec, dict) else {}
    convert = relation.get("convert") if isinstance(relation.get("convert"), str) else None
    if kind == "int":
        low, high = CONVERT_RANGES.get(convert or "", (_number(desc.get("min")), _number(desc.get("max"))))
        if low is None or high is None or high <= low:
            low, high = _number(spec.get("min")), _number(spec.get("max"))
        if (low is None or high is None or high <= low) and code in DEFAULT_RANGES:
            low, high = DEFAULT_RANGES[code]
        if low is not None and high is not None and high > low:
            entry["min"], entry["max"] = _whole(low), _whole(high)
        scale = _int(desc.get("scale", spec.get("scale")))
        if scale:
            entry["scale"] = scale
        step = _number(desc.get("step", spec.get("step")))
        if step and step > 0 and step != 1:
            entry["step"] = _whole(step)
        unit = desc.get("unit") or spec.get("unit")
        if isinstance(unit, str) and unit.strip():
            entry["unit"] = unit.strip()[:8]
    elif kind == "enum":
        raw_range = desc.get("range") or spec.get("range") or []
        mapping = relation.get("enum") if isinstance(relation.get("enum"), Mapping) else {}
        standard: dict[str, str] = {}
        for raw, target in mapping.items():
            value = target.get("value") if isinstance(target, Mapping) else None
            if isinstance(value, str) and value:
                standard[str(raw)] = value
        values: list[str] = []
        raw_of: dict[str, str] = {}
        for raw in [str(r) for r in raw_range if isinstance(r, (str, int))] or list(standard):
            std = standard.get(raw, standard.get(raw.lower(), raw))
            if std not in values:
                values.append(std)
            if std != raw:
                raw_of[std] = raw
        if values:
            entry["range"] = values
        if raw_of:
            entry["raw"] = raw_of
    elif code in ("colour_data", "colour_data_v2"):
        entry["format"] = COLOUR_FORMATS.get(convert or "", "hsv16" if code.endswith("_v2") else "rgb8")
    if convert and convert not in ("default", "enum"):
        entry["convert"] = convert
    return entry


def dp_map(device: DeviceRecord) -> DpMap:
    """The device's saved map, minus anything malformed (devices.json may be hand-edited)."""
    saved = device.settings.get("dps")
    if not isinstance(saved, dict):
        return {}
    return {
        code: entry
        for code, entry in saved.items()
        if isinstance(code, str)
        and isinstance(entry, dict)
        and isinstance(entry.get("dp"), int)
        and entry.get("type") in set(_TYPES.values())
    }


def find(dps: DpMap, codes: Iterable[str], kind: str | None = None, *, writable: bool = True) -> str | None:
    """The first of ``codes`` the device has (of type ``kind``, and writable unless asked otherwise)."""
    for code in codes:
        entry = dps.get(code)
        if entry is None or (kind and entry["type"] != kind) or (writable and entry.get("ro")):
            continue
        return code
    return None


def kind_for(category: str, dps: DpMap) -> str | None:
    """The Jarvis kind for a Tuya category, or from the DPs when the category is unknown; None to leave it out."""
    if category in SKIP_CATEGORIES or category.startswith("infrared"):
        return None
    kind = CATEGORY_KINDS.get(category)
    has_light = find(dps, ("bright_value", "bright_value_v2", "bright_value_1", "colour_data", "colour_data_v2"))
    if kind == "switch" and has_light:
        return "light"
    if kind is not None:
        return kind
    if has_light or find(dps, ("switch_led", "switch_led_1"), "bool"):
        return "light"
    if find(dps, ("control", "percent_control", "mach_operate")):
        return "cover"
    if find(dps, ("temp_set",), "int"):
        return "climate"
    if find(dps, ("switch", "switch_1"), "bool"):
        return "switch"
    return None


def skip_reason(category: str) -> str:
    """Why a device of this category is left out, in words."""
    key = "ir" if category.startswith("infrared") else SKIP_CATEGORIES.get(category, "none")
    return SKIP_REASONS[key]


def gang_codes(dps: DpMap) -> list[str]:
    """The separate switches of a multi-gang switch or power strip, in order (one or none means a single switch)."""

    def order(code: str) -> tuple[int, int]:
        usb = code.startswith("switch_usb")
        return (1 if usb else 0, int(re.sub(r"\D", "", code) or 0))

    gangs = [
        code
        for code, entry in dps.items()
        if re.fullmatch(r"switch_\d+|switch_usb\d+", code) and entry["type"] == "bool" and not entry.get("ro")
    ]
    return sorted(gangs, key=order)


# --------------------------------------------------------------------------- values


def has_range(entry: DpEntry) -> bool:
    return isinstance(entry.get("min"), (int, float)) and isinstance(entry.get("max"), (int, float))


def _snap(entry: DpEntry, raw: float) -> int:
    low, high = float(entry["min"]), float(entry["max"])
    step = float(entry.get("step") or 1)
    snapped = low + round((raw - low) / step) * step
    return round(min(high, max(low, snapped)))


def percent_to_raw(entry: DpEntry, percent: float) -> int:
    """0-100 onto the DP's own range (bright_value_v2: 50 % -> 505)."""
    low, high = float(entry["min"]), float(entry["max"])
    return _snap(entry, low + (high - low) * max(0.0, min(100.0, percent)) / 100.0)


def raw_to_percent(entry: DpEntry, raw: Any) -> int | None:
    number = _number(raw)
    if number is None or not has_range(entry):
        return None
    low, high = float(entry["min"]), float(entry["max"])
    return round(max(0.0, min(100.0, (number - low) * 100.0 / (high - low))))


def scaled(entry: DpEntry, raw: Any) -> float | None:
    """A raw integer in real units: temp_set 245 with scale 1 is 24.5 degrees."""
    number = _number(raw)
    return None if number is None else number / (10 ** int(entry.get("scale") or 0))


def number_range(entry: DpEntry) -> tuple[float, float] | None:
    if not has_range(entry):
        return None
    factor = 10 ** int(entry.get("scale") or 0)
    return float(entry["min"]) / factor, float(entry["max"]) / factor


def number_to_raw(entry: DpEntry, value: float) -> int:
    return _snap(entry, value * 10 ** int(entry.get("scale") or 0))


def enum_raw(entry: DpEntry, value: str) -> str:
    """The device's own word for a standard enum value."""
    raw = entry.get("raw")
    return str(raw.get(value, value)) if isinstance(raw, dict) else value


def enum_std(entry: DpEntry, raw: Any) -> str | None:
    if raw is None:
        return None
    mapping = entry.get("raw") if isinstance(entry.get("raw"), dict) else {}
    inverse = {str(v): k for k, v in mapping.items()}
    return inverse.get(str(raw), str(raw))


def choices(entry: DpEntry) -> tuple[str, ...]:
    values = entry.get("range")
    return tuple(str(v) for v in values) if isinstance(values, list) else ()


def words(value: str) -> str:
    """An enum value in words: "fan_only" -> "fan only"."""
    return value.replace("_", " ").strip()


def number_words(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.1f}".rstrip("0").rstrip(".")


# --------------------------------------------------------------------------- colours


@dataclass(frozen=True)
class Colour:
    """A requested light colour. ``v`` None keeps the light's brightness; ``white`` means the white LEDs."""

    name: str
    h: float = 0.0  # degrees, 0-360
    s: float = 1.0  # 0-1
    v: float | None = None  # 0-1
    white: bool = False
    temp: int | None = None  # with white: colour temperature, 0 = warmest, 100 = coolest


# Hue in degrees and saturation 0-1 of the colours people name.
COLOURS: dict[str, tuple[float, float]] = {
    "red": (0, 1.0),
    "crimson": (348, 0.9),
    "salmon": (6, 0.55),
    "coral": (16, 0.7),
    "orange": (30, 1.0),
    "amber": (45, 1.0),
    "gold": (51, 1.0),
    "yellow": (60, 1.0),
    "lime": (90, 1.0),
    "green": (120, 1.0),
    "mint": (150, 0.45),
    "turquoise": (174, 0.7),
    "teal": (180, 1.0),
    "cyan": (180, 1.0),
    "aqua": (180, 1.0),
    "light blue": (200, 0.45),
    "sky blue": (200, 0.6),
    "blue": (240, 1.0),
    "indigo": (265, 1.0),
    "violet": (275, 0.6),
    "lavender": (270, 0.35),
    "purple": (285, 1.0),
    "magenta": (300, 1.0),
    "fuchsia": (300, 1.0),
    "pink": (330, 0.45),
    "hot pink": (330, 0.7),
    "rose": (345, 0.6),
}
WHITES: dict[str, int | None] = {
    "white": None,
    "warm white": 0,
    "soft white": 15,
    "neutral white": 50,
    "natural white": 50,
    "cool white": 100,
    "cold white": 100,
    "daylight": 100,
}
# Names for a hue when describing a light, in hue order.
_HUE_NAMES = (
    (15, "red"),
    (45, "orange"),
    (70, "yellow"),
    (160, "green"),
    (200, "cyan"),
    (260, "blue"),
    (300, "purple"),
    (345, "pink"),
    (361, "red"),
)


def parse_colour(text: str) -> Colour | None:
    """A colour name ("blue", "warm white") or ``#rrggbb`` / ``#rgb``."""
    cleaned = text.strip().lower()
    match = re.fullmatch(r"#?([0-9a-f]{6}|[0-9a-f]{3})", cleaned)
    if match and (cleaned.startswith("#") or len(match.group(1)) == 6):
        digits = match.group(1)
        if len(digits) == 3:
            digits = "".join(c * 2 for c in digits)
        r, g, b = (int(digits[i : i + 2], 16) / 255.0 for i in (0, 2, 4))
        h, s, v = colorsys.rgb_to_hsv(r, g, b)
        if s < 0.08:
            return Colour(f"#{digits}", white=True)
        return Colour(f"#{digits}", h * 360.0, s, max(v, 0.01))
    name = normalize(cleaned.replace("colour", "").replace("color", ""))
    if name in WHITES:
        return Colour(name, white=True, temp=WHITES[name])
    if name in COLOURS:
        hue, saturation = COLOURS[name]
        return Colour(name, float(hue), saturation)
    return None


def encode_colour(fmt: str, h: float, s: float, v: float) -> str:
    """The local DP value: ``hsv16`` hhhhssssvvvv (s, v 0-1000) or ``rgb8`` rrggbb0hhhssvv (s, v 0-255)."""
    hue = round(h) % 360
    s, v = max(0.0, min(1.0, s)), max(0.01, min(1.0, v))
    if fmt == "rgb8":
        r, g, b = (round(c * 255) for c in colorsys.hsv_to_rgb(hue / 360.0, s, v))
        return f"{r:02x}{g:02x}{b:02x}{hue:04x}{round(s * 255):02x}{round(v * 255):02x}"
    return f"{hue:04x}{round(s * 1000):04x}{max(10, round(v * 1000)):04x}"


def decode_colour(fmt: str, raw: Any) -> tuple[float, float, float] | None:
    """(h degrees, s 0-1, v 0-1) from a local colour value, or None when it does not parse."""
    if not isinstance(raw, str):
        return None
    try:
        if fmt == "rgb8" and len(raw) == 14:
            return float(int(raw[6:10], 16)), int(raw[10:12], 16) / 255.0, int(raw[12:14], 16) / 255.0
        if fmt == "hsv16" and len(raw) == 12:
            return float(int(raw[0:4], 16)), int(raw[4:8], 16) / 1000.0, int(raw[8:12], 16) / 1000.0
    except ValueError:
        return None
    return None


def colour_word(h: float, s: float) -> str:
    if s < 0.15:
        return "white"
    return next(name for limit, name in _HUE_NAMES if h % 360 < limit)


TEMP_WORDS = {"warm": 0, "warmest": 0, "soft": 15, "neutral": 50, "natural": 50, "cool": 100, "cold": 100}


def parse_color_temp(value: Any) -> int | None:
    """warm / neutral / cool, 0-100 (0 = warmest), or kelvin ("2700K"): a percent, or None."""
    text = normalize(str(value)).replace(" white", "")
    if text in TEMP_WORDS:
        return TEMP_WORDS[text]
    if text == "daylight":
        return 100
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(k|kelvin)?", str(value).strip().lower().rstrip("%").strip())
    if not match:
        return None
    number = float(match.group(1))
    if match.group(2) or number >= 1000:
        return round(max(0.0, min(100.0, (number - 2700.0) * 100.0 / (6500.0 - 2700.0))))
    return round(number) if 0 <= number <= 100 else None


def temp_words(percent: int) -> str:
    if percent <= 20:
        return "warm white"
    if percent >= 80:
        return "cool white"
    return "neutral white"


# --------------------------------------------------------------------------- parts of a device

LIGHT_POWER = ("switch_led", "switch_led_1", "switch", "switch_1", "light")
POWER_CODES: dict[str, tuple[str, ...]] = {
    "light": LIGHT_POWER,
    "fan": ("switch_fan", "fan_switch", "switch", "switch_1"),
    "humidifier": ("switch", "switch_spray", "power", "switch_1"),
    "climate": ("switch", "power", "switch_1"),
    "heater": ("switch", "power", "switch_1"),
}
DEFAULT_POWER = ("switch", "switch_1", "switch_led", "power")
NO_POWER = frozenset({"cover", "garage", "vacuum", "scene"})


def power_code(device: DeviceRecord, dps: DpMap | None = None) -> str | None:
    """The DP that switches this device (or this gang of it) on and off."""
    if device.kind in NO_POWER:
        return None
    dps = dp_map(device) if dps is None else dps
    chosen = device.settings.get("power")
    if isinstance(chosen, str):
        return find(dps, (chosen,), "bool")
    return find(dps, POWER_CODES.get(device.kind, DEFAULT_POWER), "bool")


@dataclass(frozen=True)
class LightParts:
    bright: str | None
    temp: str | None
    colour: str | None
    mode: str | None


def light_parts(dps: DpMap) -> LightParts:
    bright = find(dps, ("bright_value_v2", "bright_value", "bright_value_1"), "int")
    temp = find(dps, ("temp_value_v2", "temp_value", "temp_value_1"), "int")
    colour = find(dps, ("colour_data_v2", "colour_data"))
    mode = find(dps, ("work_mode",), "enum")
    return LightParts(
        bright if bright and has_range(dps[bright]) else None,
        temp if temp and has_range(dps[temp]) else None,
        colour if colour and dps[colour].get("format") in ("hsv16", "rgb8") else None,
        mode,
    )


# Open, close and stop in the vocabularies curtain motors use (tinytuya's CoverDevice; FZ/ZZ as Home Assistant).
COVER_WORDS: tuple[tuple[str, str, str | None], ...] = (
    ("open", "close", "stop"),
    ("on", "off", "stop"),
    ("up", "down", "stop"),
    ("fz", "zz", "stop"),
    ("fopen", "fclose", None),
    ("1", "2", "0"),
    ("01", "02", "00"),
)


def cover_control(dps: DpMap) -> tuple[str | None, dict[str, Any]]:
    """The cover's motion DP and the raw value for each of open, close and stop it has."""
    code = find(dps, ("control", "mach_operate", "switch_1"))
    if code is None:
        return None, {}
    entry = dps[code]
    if entry["type"] == "bool":
        return code, {"open": True, "close": False}
    if entry["type"] != "enum":
        return None, {}
    values = {v.lower(): v for v in choices(entry)}
    for open_word, close_word, stop_word in COVER_WORDS:
        if open_word in values and close_word in values:
            actions = {"open": enum_raw(entry, values[open_word]), "close": enum_raw(entry, values[close_word])}
            if stop_word and stop_word in values:
                actions["stop"] = enum_raw(entry, values[stop_word])
            return code, actions
    return None, {}


def cover_position(dps: DpMap) -> tuple[str | None, str | None]:
    """(the DP to set a position, the DP that reports it)."""
    target = find(dps, ("percent_control", "position"), "int")
    state = find(dps, ("percent_state", "percent_control", "position"), "int", writable=False)
    target = target if target and has_range(dps[target]) else None
    state = state if state and has_range(dps[state]) else None
    return target, state


def fan_speed(dps: DpMap, kind: str) -> str | None:
    if kind == "fan":
        percent = find(dps, ("fan_speed_percent", "fan_speed", "speed"), "int")
        if percent and has_range(dps[percent]):
            return percent
        return find(dps, ("fan_speed_enum", "fan_speed", "speed", "level", "windspeed"), "enum")
    if kind == "climate":
        return find(dps, ("fan_speed_enum", "windspeed", "level", "fan_speed"), "enum")
    if kind == "humidifier":
        return find(dps, ("fan_speed_enum", "fan_speed", "level"), "enum")
    return None


def humidity_target(dps: DpMap) -> str | None:
    code = find(dps, ("humidity_set", "dehumidify_set_value"), "int")
    return code if code and has_range(dps[code]) else None


def temperature_target(dps: DpMap) -> str | None:
    code = find(dps, ("temp_set",), "int")
    return code if code and has_range(dps[code]) else None


def mode_code(dps: DpMap) -> str | None:
    code = find(dps, ("mode",), "enum")
    return code if code and choices(dps[code]) else None


# --------------------------------------------------------------------------- commands


def command_specs(device: DeviceRecord) -> list[CommandSpec]:
    """What the device can do, from its saved map alone."""
    kind = device.kind
    if kind == "scene":
        return [CommandSpec("activate")] if device.settings.get("scene_id") else []
    dps = dp_map(device)
    specs: list[CommandSpec] = []
    if kind in ("cover", "garage"):
        _, actions = cover_control(dps)
        if "open" in actions:
            specs.append(CommandSpec("open", tier="screen" if kind == "garage" else "free"))
        if "close" in actions:
            specs.append(CommandSpec("close"))
        if "stop" in actions:
            specs.append(CommandSpec("stop"))
        target, _ = cover_position(dps)
        if target and kind == "cover":
            specs.append(CommandSpec("set_position", "percent", hint="0 closed, 100 open"))
        return specs
    if kind == "vacuum":
        if find(dps, ("power_go",), "bool"):
            specs += [CommandSpec("start"), CommandSpec("stop")]
        if find(dps, ("pause", "power_go"), "bool"):
            specs.append(CommandSpec("pause"))
        mode = mode_code(dps)
        if find(dps, ("switch_charge",), "bool") or (mode and "chargego" in choices(dps[mode])):
            specs.append(CommandSpec("dock"))
        return specs
    if power_code(device, dps):
        specs += [CommandSpec("turn_on"), CommandSpec("turn_off"), CommandSpec("toggle")]
    if kind == "light":
        parts = light_parts(dps)
        if parts.bright or parts.colour:
            specs.append(CommandSpec("set_brightness", "percent"))
        if parts.colour:
            specs.append(CommandSpec("set_color", "text", hint="a colour name or #rrggbb"))
        if parts.temp:
            specs.append(CommandSpec("set_color_temp", "text", hint="warm, neutral, cool or 0-100 (0 = warmest)"))
    if kind in ("climate", "heater"):
        target = temperature_target(dps)
        limits = number_range(dps[target]) if target else None
        if target and limits:
            specs.append(CommandSpec("set_temperature", "number", low=limits[0], high=limits[1], hint="degrees"))
    if kind == "humidifier":
        target = humidity_target(dps)
        limits = number_range(dps[target]) if target else None
        if target and limits:
            specs.append(CommandSpec("set_humidity", "number", low=limits[0], high=limits[1], hint="percent"))
    if kind in ("climate", "heater", "fan", "humidifier"):
        mode = mode_code(dps)
        if mode:
            specs.append(CommandSpec("set_mode", "choice", choices=choices(dps[mode])))
    speed = fan_speed(dps, kind)
    if speed:
        entry = dps[speed]
        if entry["type"] == "int":
            specs.append(CommandSpec("set_fan_speed", "percent"))
        elif choices(entry):
            specs.append(CommandSpec("set_fan_speed", "choice", choices=choices(entry)))
    return specs


# --------------------------------------------------------------------------- state in words


def position_of(device: DeviceRecord, dps: DpMap, values: Mapping[str, Any]) -> int | None:
    """The cover's position as people count it: 0 closed, 100 open."""
    _, state = cover_position(dps)
    if state is None:
        return None
    percent = raw_to_percent(dps[state], values.get(str(dps[state]["dp"])))
    if percent is None:
        return None
    return 100 - percent if device.settings.get("invert") else percent


def describe_state(device: DeviceRecord, values: Mapping[str, Any]) -> str:
    """One or two short sentences about the state the device reported (``values``: DP number -> raw)."""
    dps = dp_map(device)

    def raw(code: str | None) -> Any:
        return None if code is None or code not in dps else values.get(str(dps[code]["dp"]))

    the = f"The {device.name}"
    kind = device.kind
    if kind in ("cover", "garage"):
        if kind == "garage" and raw("doorcontact_state") is not None:
            return f"{the} is {'open' if raw('doorcontact_state') else 'closed'}."
        position = position_of(device, dps, values)
        if position is None:
            return f"{the} answered, but did not say where it is."
        if position <= 1:
            return f"{the} is closed."
        if position >= 99:
            return f"{the} is open."
        return f"{the} is {position}% open."
    if kind == "vacuum":
        state = enum_std(dps["status"], raw("status")) if "status" in dps else None
        battery = raw("electricity_left") if raw("electricity_left") is not None else raw("battery_percentage")
        level = _number(battery)
        charge = f"battery is at {number_words(level)}%" if level is not None else ""
        if state:
            return f"{the} reports {words(state)}" + (f"; its {charge}." if charge else ".")
        if charge:
            return f"{the}'s {charge}."
        return f"{the} answered, but did not say what it is doing."
    power = power_code(device, dps) or find(dps, POWER_CODES.get(kind, DEFAULT_POWER), "bool", writable=False)
    on = raw(power)
    details: list[str] = []
    if kind == "light" and on is not False:
        details += _light_details(dps, values)
    if kind in ("climate", "heater"):
        mode = mode_code(dps) or find(dps, ("mode",), "enum", writable=False)
        if mode and raw(mode) is not None:
            details.append(f"in {words(enum_std(dps[mode], raw(mode)) or '')} mode")
        target = find(dps, ("temp_set",), "int", writable=False)
        if target and raw(target) is not None:
            details.append(f"set to {number_words(scaled(dps[target], raw(target)) or 0)} degrees")
    if kind == "fan":
        speed = fan_speed(dps, kind)
        if speed and raw(speed) is not None:
            entry = dps[speed]
            if entry["type"] == "int":
                details.append(f"at {raw_to_percent(entry, raw(speed))}% speed")
            else:
                details.append(f"at speed {words(enum_std(entry, raw(speed)) or '')}")
    if kind == "humidifier":
        target = find(dps, ("humidity_set", "dehumidify_set_value"), "int", writable=False)
        if target and raw(target) is not None:
            details.append(f"set to {number_words(scaled(dps[target], raw(target)) or 0)}% humidity")
    if kind in ("plug", "switch") and on and raw("cur_power") is not None and "cur_power" in dps:
        watts = scaled(dps["cur_power"], raw("cur_power"))
        if watts is not None:
            details.append(f"drawing {number_words(round(watts, 1))} watts")
    if on is None and not details:
        return f"{the} answered, but did not report whether it is on."
    state = "" if on is None else ("on" if on else "off")
    sentence = f"{the} is " + ", ".join(p for p in [state, *details] if p) + "."
    current = find(dps, ("temp_current",), "int", writable=False)
    if kind in ("climate", "heater") and current and raw(current) is not None:
        sentence += f" It is {number_words(scaled(dps[current], raw(current)) or 0)} degrees in the room."
    humidity = find(dps, ("humidity_current",), "int", writable=False)
    if kind == "humidifier" and humidity and raw(humidity) is not None:
        sentence += f" The room is at {number_words(scaled(dps[humidity], raw(humidity)) or 0)}% humidity."
    return sentence


def _light_details(dps: DpMap, values: Mapping[str, Any]) -> list[str]:
    parts = light_parts(dps)

    def raw(code: str | None) -> Any:
        return None if code is None else values.get(str(dps[code]["dp"]))

    mode = enum_std(dps[parts.mode], raw(parts.mode)) if parts.mode else None
    if mode == "colour" and parts.colour:
        hsv = decode_colour(dps[parts.colour]["format"], raw(parts.colour))
        if hsv is not None:
            h, s, v = hsv
            return [colour_word(h, s), f"at {max(1, round(v * 100))}% brightness"]
    details = []
    if parts.bright and raw(parts.bright) is not None:
        details.append(f"at {max(1, raw_to_percent(dps[parts.bright], raw(parts.bright)) or 0)}% brightness")
    if parts.temp and raw(parts.temp) is not None and mode in (None, "white"):
        percent = raw_to_percent(dps[parts.temp], raw(parts.temp))
        if percent is not None:
            details.append(temp_words(percent))
    return details
