"""Helper <-> mod protocol for hands: event builders and command validation.

``plugin/protocol/hands.schema.json`` is the authoritative contract. Events
are plain dicts built here (``v`` and ``type`` first), so the runtime never
hand-writes one. Command bodies are checked by hand-written rules equal to the
schema: the contract is small and stable, and the helper then needs no
``jsonschema`` at runtime. The test suite checks every builder against the
schema and cross-checks ``validate_command`` with the reference implementation
on many bodies, so the two cannot drift silently.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, get_args

if TYPE_CHECKING:
    from .desktop.base import Display

PROTOCOL_VERSION = 1

HandsState = Literal["starting", "idle", "active", "paused", "calibrating", "error"]
PlatformName = Literal["windows", "macos", "linux"]
ErrorCode = Literal[
    "already_running",
    "unsupported_platform",
    "no_camera",
    "camera_blocked",
    "camera_in_use",
    "camera_lost",
    "model_missing",
    "tracker_failed",
    "input_blocked",
    "overlay_failed",
    "bad_request",
    "unauthorized",
    "internal",
]
GestureName = Literal[
    "engage",
    "disengage",
    "click",
    "double_click",
    "right_click",
    "drag_start",
    "drag_end",
    "scroll_start",
    "grab",
    "release",
    "throw_left",
    "throw_right",
    "throw_up",
    "throw_down",
    "resize_start",
    "user_input",
]
CalibrationStep = Literal["top_left", "top_right", "bottom_right", "bottom_left", "done", "cancelled"]
EngageMode = Literal["palm", "always"]
HandChoice = Literal["right", "left", "any"]
AnchorChoice = Literal["knuckles", "index"]
CalibrateAction = Literal["start", "cancel"]
CommandName = Literal[
    "heartbeat", "status", "config", "pause", "resume", "engage", "disengage", "calibrate", "shutdown"
]

COMMAND_NAMES: frozenset[str] = frozenset(get_args(CommandName))

#: Event ``type`` -> its definition in the schema's ``$defs``.
EVENT_DEFS: dict[str, str] = {
    "hello": "HelloEvent",
    "state": "StateEvent",
    "ready": "ReadyEvent",
    "gesture": "GestureEvent",
    "calibration": "CalibrationEvent",
    "error": "ErrorEvent",
}

SCROLL_SPEED_MIN, SCROLL_SPEED_MAX = 0.1, 10.0

_ENGAGE_MODES = frozenset(get_args(EngageMode))
_HANDS = frozenset(get_args(HandChoice))
_ANCHORS = frozenset(get_args(AnchorChoice))
_CALIBRATE_ACTIONS = frozenset(get_args(CalibrateAction))
_CONFIG_KEYS = ("engage", "displays", "hand", "anchor", "overlay", "scrollSpeed")


class ValidationError(ValueError):
    """A command name or body does not conform to the schema."""


def schema_path() -> Path:
    """``plugin/protocol/hands.schema.json`` of the source checkout this module lives in (tests only).

    An installed package has no copy: the helper never reads the schema at runtime.
    """
    return Path(__file__).resolve().parents[3] / "protocol" / "hands.schema.json"


# --------------------------------------------------------------------------- events


def _event(kind: str, **fields: Any) -> dict[str, Any]:
    return {"v": PROTOCOL_VERSION, "type": kind, **fields}


def hello(port: int, pid: int, platform: PlatformName, version: str, capabilities: list[str]) -> dict[str, Any]:
    return _event(
        "hello", port=int(port), pid=int(pid), platform=platform, version=version, capabilities=list(capabilities)
    )


def state(state: HandsState) -> dict[str, Any]:
    return _event("state", state=state)


def ready(camera: str | int, width: int, height: int, fps: float, displays: list[dict[str, Any]]) -> dict[str, Any]:
    """``camera`` is the camera's name, or its index when the name is unknown.

    Camera backends report what they like (OpenCV gives -1 for an unknown
    frame rate, a mean over no frames is NaN). A NaN would make the whole
    event unencodable and the mod would never hear the camera is ready, so an
    unknown rate becomes 0 and sizes are at least one pixel, as the schema asks.
    """
    rate = float(fps)
    return _event(
        "ready",
        camera=str(camera),
        width=max(1, int(width)),
        height=max(1, int(height)),
        fps=rate if math.isfinite(rate) and rate > 0 else 0.0,
        displays=list(displays),
    )


def gesture(name: GestureName) -> dict[str, Any]:
    return _event("gesture", name=name)


def calibration(step: CalibrationStep, display: str | None = None) -> dict[str, Any]:
    event = _event("calibration", step=step)
    if display is not None:
        event["display"] = display
    return event


def error(code: ErrorCode, message: str, hint: str | None = None, fatal: bool = False) -> dict[str, Any]:
    event = _event("error", code=code, message=message)
    if hint is not None:
        event["hint"] = hint
    event["fatal"] = bool(fatal)
    return event


def display_dict(display: Display, used: bool) -> dict[str, Any]:
    """A desktop display as the protocol's ``Display`` (whole pixels)."""
    x, y, width, height = display.rect.rounded()
    return {
        "id": int(display.id),
        "name": display.name,
        "x": x,
        "y": y,
        "width": max(0, width),
        "height": max(0, height),
        "primary": bool(display.primary),
        "virtual": bool(display.virtual),
        "used": bool(used),
    }


# --------------------------------------------------------------------------- responses


def ok_response(*, pending: bool = False) -> dict[str, Any]:
    """``pending``: the camera change the command asked for was still going on when it answered.

    ``pause`` and ``resume`` wait for the loop thread to close or reopen the
    camera, and a camera can take longer to open than they wait. The mod then
    says the camera is still starting or still stopping, instead of claiming
    it is already off (whose light would still be on) or already back.
    """
    return {"ok": True, "pending": True} if pending else {"ok": True}


def error_response(code: ErrorCode, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


# --------------------------------------------------------------------------- commands


def _is_number(value: Any) -> bool:
    # bool is an int subclass in Python, not a number in JSON. NaN and the
    # infinities are not JSON at all (the control server refuses them too).
    # An int is never converted to float: one with hundreds of digits is valid
    # JSON and would overflow, and int/float comparisons are exact anyway.
    if isinstance(value, bool):
        return False
    return isinstance(value, int) or (isinstance(value, float) and math.isfinite(value))


def _is_integer(value: Any) -> bool:
    # JSON Schema counts 2.0 as an integer, like the reference validator.
    return _is_number(value) and (isinstance(value, int) or float(value).is_integer())


def _check_enum(body: dict[str, Any], key: str, allowed: frozenset[str]) -> None:
    if key in body:
        value = body[key]
        if not isinstance(value, str) or value not in allowed:
            raise ValidationError(f"$.{key}: must be one of {sorted(allowed)!r}")


def _check_keys(body: dict[str, Any], allowed: tuple[str, ...]) -> None:
    extra = sorted(set(body) - set(allowed))
    if extra:
        raise ValidationError(f"$: unexpected property {extra[0]!r}")


def _check_config(body: dict[str, Any]) -> None:
    _check_keys(body, _CONFIG_KEYS)
    _check_enum(body, "engage", _ENGAGE_MODES)
    if "displays" in body:
        displays = body["displays"]
        if displays != "all" and not (isinstance(displays, list) and all(_is_integer(d) and d >= 1 for d in displays)):
            raise ValidationError("$.displays: must be 'all' or a list of display numbers (integers >= 1)")
    _check_enum(body, "hand", _HANDS)
    _check_enum(body, "anchor", _ANCHORS)
    if "overlay" in body and not isinstance(body["overlay"], bool):
        raise ValidationError("$.overlay: expected boolean")
    if "scrollSpeed" in body:
        speed = body["scrollSpeed"]
        if not _is_number(speed):
            raise ValidationError("$.scrollSpeed: expected number")
        if not SCROLL_SPEED_MIN <= speed <= SCROLL_SPEED_MAX:
            raise ValidationError(f"$.scrollSpeed: must be between {SCROLL_SPEED_MIN} and {SCROLL_SPEED_MAX:g}")


def _check_calibrate(body: dict[str, Any]) -> None:
    if "action" not in body:
        raise ValidationError("$: missing required property 'action'")
    _check_keys(body, ("action",))
    _check_enum(body, "action", _CALIBRATE_ACTIONS)


def validate_command(name: str, body: Any) -> None:
    """Raise ValidationError unless ``name`` is a command and ``body`` matches its schema."""
    if name not in COMMAND_NAMES:
        raise ValidationError(f"unknown command {name!r}")
    if not isinstance(body, dict):
        raise ValidationError("$: the body must be a JSON object")
    if name == "config":
        _check_config(body)
    elif name == "calibrate":
        _check_calibrate(body)
    else:
        _check_keys(body, ())
