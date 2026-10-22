"""The air keyboard's accuracy numbers, and the one file they may be tuned from.

``Tuning`` holds the numbers a recording may retune (``keyreplay --write``): thresholds, windows, the plane's pitch.
Every one has a default and a clamp, and a value outside its clamp is not pulled to the edge but replaced by the
default, so a forged file can never land just above a floor. A rule that guards against a false press, a runaway or a
leak is not in here at all: those are code constants in ``limits.py``, and no name of the file reaches them. The low
edge of a clamp that has a floor there is the larger of the two, so editing a clamp below cannot weaken the floor.

The file is data from outside: it may be corrupt, huge, wrongly typed or written by another process. Loading never
raises, never writes, logs at most one line, and that line names our own fields and a fixed reason, never what the file
said (an exception text is data too). DESIGN-KEYBOARD.md 3.2 and 3.14.

The file is ``{"version": 1, "pinch": {...}, "plane": {...}, "hands": {...}, "air": {...}}``; ``GROUPS`` below says
which group holds which field, and is the one list of it. A field is read from its own group only, and a name that is
unknown or in another group is skipped without a word (it is the file's text, which no log line may repeat).
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import limits

log = logging.getLogger(__name__)

#: A tuning file is a few hundred bytes; anything near this is not one.
MAX_FILE_BYTES = 65536
FILE_VERSION = 1
#: Slack for the relations between two fields, so 0.30 + 0.06 compared with 0.36 is not a float accident.
_EPSILON = 1e-9


@dataclass(frozen=True)
class Tuning:
    # --- pinch (2.6) -------------------------------------------------------------------------------------------------
    close: float = 0.28
    open: float = 0.40
    descent: float = 0.10
    descent_max_r: float = 0.80
    onset_window_s: float = 0.35
    recover: float = 0.05
    closing_timeout_s: float = 0.8
    margin: float = 0.10
    confirm_frames: int = 2
    curled_enter: float = 1.10
    curled_leave: float = 1.20
    others_delta: float = 0.20
    others_from_s: float = 0.18
    others_to_s: float = 0.30
    anchor_speed_max: float = 1.5
    aim_frames: int = 3
    warm_factor: float = 1.25
    # --- plane (2.3, 2.4) --------------------------------------------------------------------------------------------
    pitch: float = 0.0495
    pitch_y_ratio: float = 1.25
    edge_tolerance: float = 0.35
    still_speed: float = 0.15
    still_s: float = 0.6
    # --- hands (2.1, 2.2) --------------------------------------------------------------------------------------------
    level_palm: tuple[float, float, float, float] = (0.0, 0.10, 0.04, -0.15)
    associate_radius: float = 0.25
    hand_hold_s: float = 0.20
    z_scale: float = 1.0
    # --- air (2.12) --------------------------------------------------------------------------------------------------
    air_theta_k: float = 5.0
    air_theta_min: float = 0.10
    air_theta_max: float = 0.25
    air_depth_frac: float = 0.5
    air_back_s: float = 0.20
    air_rise_win_s: float = 0.16
    air_fall_win_s: float = 0.12
    air_return_frac: float = 0.5
    air_width_min_s: float = 0.04
    air_width_max_s: float = 0.30
    air_speed_gate: float = 0.5
    air_vmax_gate: float = 0.5
    air_veto_ratio: float = 0.7
    air_aim: str = "auto"
    air_aim_speed: float = 0.05


#: Fields that are whole numbers; a float such as 2.0 is refused for them (a count is not a measurement).
INT_FIELDS = frozenset({"confirm_frames", "aim_frames"})

#: name -> (low, high), both ends accepted. A field with a floor in ``limits`` takes the larger low edge.
RANGES: dict[str, tuple[float, float]] = {
    "close": (0.22, 0.36),
    "open": (0.34, 0.50),
    "descent": (0.06, 0.20),
    "descent_max_r": (0.60, 1.00),
    "onset_window_s": (0.20, 0.60),
    "recover": (0.02, 0.10),
    "closing_timeout_s": (0.40, 1.50),
    "margin": (0.08, 0.25),
    "confirm_frames": (2, 3),
    "curled_enter": (1.00, 1.20),
    "curled_leave": (1.05, 1.35),
    "others_delta": (0.10, 0.40),
    "others_from_s": (0.10, 0.20),
    "others_to_s": (0.20, 0.40),
    "anchor_speed_max": (1.0, 3.0),
    "aim_frames": (2, 5),
    "warm_factor": (1.10, 1.50),
    "pitch": (0.035, 0.075),
    "pitch_y_ratio": (1.0, 1.6),
    "edge_tolerance": (0.20, 0.50),
    "associate_radius": (0.15, 0.35),
    "hand_hold_s": (0.10, 0.30),
    "z_scale": (0.3, 1.0),
    "still_speed": (0.08, 0.30),
    "still_s": (0.4, 1.5),
    "air_theta_k": (max(4.0, limits.AIR_THETA_K_FLOOR), 8.0),
    "air_theta_min": (max(0.08, limits.AIR_THETA_FLOOR), 0.20),
    "air_theta_max": (0.18, 0.40),
    "air_depth_frac": (0.4, 0.7),
    "air_back_s": (0.15, 0.30),
    "air_rise_win_s": (0.10, 0.25),
    "air_fall_win_s": (0.08, 0.20),
    "air_return_frac": (0.35, 0.65),
    "air_width_min_s": (0.03, 0.08),
    "air_width_max_s": (0.20, 0.45),
    "air_speed_gate": (0.3, 1.0),
    "air_vmax_gate": (0.3, 1.0),
    "air_veto_ratio": (max(0.5, limits.AIR_VETO_FLOOR), 0.85),
    "air_aim_speed": (0.02, 0.15),
}
#: Each of the four numbers of ``level_palm``.
LEVEL_PALM_RANGE = (-0.30, 0.30)
AIR_AIM_CHOICES = ("auto", "onset", "commit")

#: The layout of the file: a field is read from its own group only, so a name in the wrong group is an unknown key.
GROUPS: dict[str, tuple[str, ...]] = {
    "pinch": (
        "close",
        "open",
        "descent",
        "descent_max_r",
        "onset_window_s",
        "recover",
        "closing_timeout_s",
        "margin",
        "confirm_frames",
        "curled_enter",
        "curled_leave",
        "others_delta",
        "others_from_s",
        "others_to_s",
        "anchor_speed_max",
        "aim_frames",
        "warm_factor",
    ),
    "plane": ("pitch", "pitch_y_ratio", "edge_tolerance", "still_speed", "still_s"),
    "hands": ("level_palm", "associate_radius", "hand_hold_s", "z_scale"),
    "air": (
        "air_theta_k",
        "air_theta_min",
        "air_theta_max",
        "air_depth_frac",
        "air_back_s",
        "air_rise_win_s",
        "air_fall_win_s",
        "air_return_frac",
        "air_width_min_s",
        "air_width_max_s",
        "air_speed_gate",
        "air_vmax_gate",
        "air_veto_ratio",
        "air_aim",
        "air_aim_speed",
    ),
}

#: (lower field, upper field, least gap): where the pair breaks the relation, both fall back to the default.
_RELATIONS = (
    ("close", "open", 0.06),
    ("curled_enter", "curled_leave", 0.05),
    ("air_theta_min", "air_theta_max", 0.0),
)

# What the one log line says when the whole file is set aside. Fixed words: nothing the file held can be in them.
_NOT_A_FILE = "it is not a file"
_UNREADABLE = "it cannot be read"
_TOO_LARGE = "it is too large"
_NOT_JSON = "it is not valid JSON"
_NOT_AN_OBJECT = "it is not a JSON object"
_UNKNOWN_VERSION = "its version is not 1"

_MISSING = object()


def wire_name(name: str) -> str:
    """``pitch_y_ratio`` -> ``pitchYRatio``: the camelCase spelling the protocol uses; the file accepts both."""
    head, *rest = name.split("_")
    return head + "".join(part.capitalize() for part in rest)


def tuning_path(data_dir: Path) -> Path:
    return Path(data_dir) / "hands" / "keyboard-tuning.json"


def _number(value: object) -> float | None:
    """A finite float from a JSON number. A bool is not one, and neither is a string that looks like one."""
    if type(value) is not int and type(value) is not float:
        return None
    try:
        number = float(value)
    except OverflowError:  # an integer too large for a float
        return None
    return number if math.isfinite(number) else None


def _scalar(name: str, raw: object) -> float | int | None:
    low, high = RANGES[name]
    if name in INT_FIELDS:
        return raw if type(raw) is int and low <= raw <= high else None
    number = _number(raw)
    return number if number is not None and low <= number <= high else None


def _level_palm(raw: object) -> tuple[float, ...] | None:
    if not isinstance(raw, list | tuple) or len(raw) != 4:
        return None
    numbers = [_number(x) for x in raw]
    low, high = LEVEL_PALM_RANGE
    if any(n is None or not low <= n <= high for n in numbers):
        return None
    return tuple(n for n in numbers if n is not None)


def _accept(name: str, raw: object) -> Any:
    """The clean value for ``name``, or None when the file's value is not usable."""
    if name == "level_palm":
        return _level_palm(raw)
    if name == "air_aim":
        return raw if type(raw) is str and raw in AIR_AIM_CHOICES else None
    return _scalar(name, raw)


def _parse(data: object) -> tuple[Tuning, list[str]]:
    """The tuning a document gives and the names (ours) of what fell back to a default."""
    if not isinstance(data, Mapping):
        return Tuning(), []
    version = data.get("version", FILE_VERSION)
    if type(version) is not int or version != FILE_VERSION:
        return Tuning(), ["version"]
    accepted: dict[str, Any] = {}
    fell_back: list[str] = []
    for group, names in GROUPS.items():
        body = data.get(group, _MISSING)
        if body is _MISSING:
            continue
        if not isinstance(body, Mapping):
            fell_back.append(group)
            continue
        for name in names:
            raw = body[name] if name in body else body.get(wire_name(name), _MISSING)
            if raw is _MISSING:
                continue
            value = _accept(name, raw)
            if value is None:
                fell_back.append(name)
            else:
                accepted[name] = value
    defaults = Tuning()
    for low_name, high_name, gap in _RELATIONS:
        low = accepted.get(low_name, getattr(defaults, low_name))
        high = accepted.get(high_name, getattr(defaults, high_name))
        if high < low + gap - _EPSILON:
            # Neither can be trusted alone: a closer pair would let a press and a release be confused.
            for name in (low_name, high_name):
                if accepted.pop(name, None) is not None:
                    fell_back.append(name)
    return Tuning(**accepted), fell_back


def parse_tuning(data: object) -> Tuning:
    """The tuning a decoded document gives. Anything unusable falls back to the default for that value; never raises."""
    return _parse(data)[0]


def _ignored(reason: str) -> Tuning:
    log.warning("keyboard tuning file ignored: %s; the built-in values are used", reason)
    return Tuning()


def load_tuning(data_dir: Path) -> Tuning:
    """Read ``<data_dir>/hands/keyboard-tuning.json`` once. No file is the normal case and says nothing."""
    path = tuning_path(data_dir)
    try:
        if not path.exists():
            return Tuning()
        if not path.is_file():
            return _ignored(_NOT_A_FILE)
        if path.stat().st_size > MAX_FILE_BYTES:
            return _ignored(_TOO_LARGE)
        raw = path.read_bytes()
    except OSError:
        return _ignored(_UNREADABLE)
    if len(raw) > MAX_FILE_BYTES:  # grew between the size check and the read
        return _ignored(_TOO_LARGE)
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, RecursionError):  # bad bytes, bad JSON, a number too long, nesting too deep
        return _ignored(_NOT_JSON)
    if not isinstance(data, Mapping):
        return _ignored(_NOT_AN_OBJECT)
    tuning, fell_back = _parse(data)
    if fell_back == ["version"]:
        return _ignored(_UNKNOWN_VERSION)
    if fell_back:
        names = ", ".join(fell_back)
        log.warning("keyboard tuning: the built-in value is used for: %s", names)
    return tuning
