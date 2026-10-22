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
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, get_args

from .keyboard.types import CloseReason, Commit, Hold, Lang, Phase, PressName
from .keyboard.types import InsertAbort as InsertAbort
from .keyboard.types import ReviewState as ReviewState
from .settings import KNOBS

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
# The air keyboard's enums (DESIGN-KEYBOARD.md 3.9). The schema's ``$defs`` hold the same lists; a test compares them.
KeyboardState = Literal["open", "practice", "closed"]
KeyboardLevel = Literal["ok", "degraded", "off"]
KeyboardPhase = Phase
KeyboardPress = PressName
KeyboardLang = Lang
KeyboardHold = Hold
KeyboardCloseReason = CloseReason
KeyboardCommit = Commit
CommandName = Literal[
    "heartbeat", "status", "config", "pause", "resume", "engage", "disengage", "calibrate", "shutdown", "keyboard"
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
    "keyboard": "KeyboardEvent",
}

_ENGAGE_MODES = frozenset(get_args(EngageMode))
_HANDS = frozenset(get_args(HandChoice))
_ANCHORS = frozenset(get_args(AnchorChoice))
_CALIBRATE_ACTIONS = frozenset(get_args(CalibrateAction))
_CONFIG_KEYS = ("engage", "displays", "hand", "anchor", "overlay", *(knob.key for knob in KNOBS))


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


#: The cap of the review box and of every count in a keyboard event: ``limits.COMPOSE_MAX`` and the schema's maximum
#: (a test asserts the three are one number; this module may not import the limits, so the number is repeated here).
_KEYBOARD_COUNT_MAX = 200


def _count(value: Any, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError, OverflowError):  # not a number, NaN, infinity
        return low


def _measure(value: Any, high: float | None = None) -> float:
    """A finite float of at least 0.0 (and at most ``high``); anything else is 0.0, so the event stays encodable."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    number = max(0.0, number)
    return number if high is None else min(high, number)


def _review_dict(review: Mapping[str, Any]) -> dict[str, Any]:
    # Rebuilt from the known fields: whatever else the caller's dict carries never reaches the wire.
    out: dict[str, Any] = {"state": review["state"], "chars": _count(review["chars"], 0, _KEYBOARD_COUNT_MAX)}
    insert = review.get("insert")
    if insert is not None:
        built: dict[str, Any] = {
            "kind": insert["kind"],
            "outcome": insert["outcome"],
            "sent": _count(insert["sent"], 0, _KEYBOARD_COUNT_MAX),
            "of": _count(insert["of"], 1, _KEYBOARD_COUNT_MAX),
        }
        if insert.get("reason") is not None:
            built["reason"] = insert["reason"]
        out["insert"] = built
    return out


def _practice_dict(practice: Mapping[str, Any]) -> dict[str, Any]:
    out = {"hitRate": _measure(practice["hitRate"], 1.0), "phantomsPerMin": _measure(practice["phantomsPerMin"])}
    if practice.get("recallIM") is not None:
        out["recallIM"] = _measure(practice["recallIM"], 1.0)
    return out


def keyboard(
    state: KeyboardState,
    *,
    phase: KeyboardPhase | None = None,
    reason: KeyboardCloseReason | None = None,
    hold: KeyboardHold | None = None,
    lang: KeyboardLang | None = None,
    press: KeyboardPress | None = None,
    level: KeyboardLevel | None = None,
    commit: KeyboardCommit | None = None,
    private: bool | None = None,
    review: Mapping[str, Any] | None = None,
    discarded: int | None = None,
    practice: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The air keyboard's event: enums and counts only, never a character, a key, a window or a program (SR15).

    ``None`` leaves a field out. ``review`` and ``practice`` are rebuilt from their known fields, counts are clamped
    to the schema's range and a number that is not finite becomes 0.0, so no count or float can make the event
    unencodable (the mod would then never hear that the session closed). The enum arguments pass through as they are,
    like those of ``state`` and ``error`` above: their ``Literal`` types are the check.
    """
    event = _event("keyboard", state=state)
    for key, value in (
        ("phase", phase),
        ("reason", reason),
        ("hold", hold),
        ("lang", lang),
        ("press", press),
        ("level", level),
        ("commit", commit),
    ):
        if value is not None:
            event[key] = value
    if private is not None:
        event["private"] = bool(private)
    if review is not None:
        event["review"] = _review_dict(review)
    if discarded is not None:
        event["discarded"] = _count(discarded, 0, _KEYBOARD_COUNT_MAX)
    if practice is not None:
        event["practice"] = _practice_dict(practice)
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


def _check_keys(body: dict[str, Any], allowed: tuple[str, ...], *, name_it: bool = True) -> None:
    """``name_it`` False: the text does not repeat the property, for a command whose body may hold anything."""
    extra = sorted(set(body) - set(allowed))
    if extra:
        raise ValidationError(f"$: unexpected property {extra[0]!r}" if name_it else "$: unexpected property")


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
    for knob in KNOBS:
        if knob.key in body:
            value = body[knob.key]
            if not _is_number(value):
                raise ValidationError(f"$.{knob.key}: expected number")
            if not knob.low <= value <= knob.high:
                raise ValidationError(f"$.{knob.key}: must be between {knob.low:g} and {knob.high:g}")


def _check_calibrate(body: dict[str, Any]) -> None:
    if "action" not in body:
        raise ValidationError("$: missing required property 'action'")
    _check_keys(body, ("action",))
    _check_enum(body, "action", _CALIBRATE_ACTIONS)


#: The actions of the ``keyboard`` command. There is none for Insert, Send, Clear, text or the ladder: the model and the
#: mod can open, close and set the keyboard, never put text in it or send it anywhere (S55).
KEYBOARD_ACTIONS: frozenset[str] = frozenset(
    {"start", "practice", "stop", "recenter", "private", "public", "configure"}
)
_KEYBOARD_PRESSES = frozenset(get_args(KeyboardPress))
_KEYBOARD_COMMITS = frozenset(get_args(KeyboardCommit))
_KEYBOARD_LAYOUTS = frozenset({"auto", "en", "he"})
_KEYBOARD_DOCKS = frozenset({"top", "bottom"})
_KEYBOARD_INJECTS = frozenset({"unicode", "vk"})
_KEYBOARD_ENTERS = frozenset({"twice", "off"})
#: setting -> (low, high) for the numbers; ``idleS`` is an integer.
_KEYBOARD_SIZE = (0.6, 1.6)
_KEYBOARD_REACH = (0.8, 1.5)
_KEYBOARD_IDLE_S = (5, 300)
_KEYBOARD_SETTING_KEYS = ("enabled", "press", "commit", "layout", "size", "reach", "dock", "idleS", "inject", "enter")


def _check_keyboard_settings(settings: Any) -> None:
    # Messages name the setting and never the value or an unknown key: a rejected body may hold anything.
    if not isinstance(settings, dict):
        raise ValidationError("$.settings: expected an object")
    if set(settings) - set(_KEYBOARD_SETTING_KEYS):
        raise ValidationError("$.settings: unexpected property")
    if "enabled" in settings and not isinstance(settings["enabled"], bool):
        raise ValidationError("$.settings.enabled: expected boolean")
    for key, allowed in (
        ("press", _KEYBOARD_PRESSES),
        ("commit", _KEYBOARD_COMMITS),
        ("layout", _KEYBOARD_LAYOUTS),
        ("dock", _KEYBOARD_DOCKS),
        ("inject", _KEYBOARD_INJECTS),
        ("enter", _KEYBOARD_ENTERS),
    ):
        if key in settings:
            value = settings[key]
            if not isinstance(value, str) or value not in allowed:
                raise ValidationError(f"$.settings.{key}: must be one of {sorted(allowed)!r}")
    for key, (low, high) in (("size", _KEYBOARD_SIZE), ("reach", _KEYBOARD_REACH)):
        if key in settings:
            value = settings[key]
            if not _is_number(value):
                raise ValidationError(f"$.settings.{key}: expected number")
            if not low <= value <= high:
                raise ValidationError(f"$.settings.{key}: must be between {low:g} and {high:g}")
    if "idleS" in settings:
        value = settings["idleS"]
        low_s, high_s = _KEYBOARD_IDLE_S
        if not _is_integer(value) or not low_s <= value <= high_s:
            raise ValidationError(f"$.settings.idleS: must be an integer between {low_s} and {high_s}")


def _check_keyboard(body: dict[str, Any]) -> None:
    if "action" not in body:
        raise ValidationError("$: missing required property 'action'")
    action = body["action"]
    if not isinstance(action, str) or action not in KEYBOARD_ACTIONS:
        raise ValidationError(f"$.action: must be one of {sorted(KEYBOARD_ACTIONS)!r}")
    if action != "configure":
        _check_keys(body, ("action",), name_it=False)
        return
    _check_keys(body, ("action", "settings"), name_it=False)
    if "settings" not in body:
        raise ValidationError("$: missing required property 'settings'")
    _check_keyboard_settings(body["settings"])


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
    elif name == "keyboard":
        _check_keyboard(body)
    else:
        _check_keys(body, ())
