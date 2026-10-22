from __future__ import annotations

import inspect
import itertools
import json
import math
import random
from collections.abc import Callable
from typing import Any, get_args

import pytest

from jarvis_hands import protocol
from jarvis_hands.desktop.base import Display
from jarvis_hands.events import encode_line
from jarvis_hands.geometry import Rect
from jarvis_hands.keyboard import limits
from jarvis_hands.settings import HandsSettings

Validate = Callable[[Any, str], None]


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(protocol.schema_path().read_text(encoding="utf-8"))
    return document


@pytest.fixture(scope="module")
def reference_validator(schema: dict[str, Any]) -> Validate:
    """jsonschema over the authoritative schema (the reference implementation)."""
    import jsonschema

    jsonschema.Draft202012Validator.check_schema(schema)

    def validate(instance: Any, def_name: str) -> None:
        wrapper = {"$ref": f"#/$defs/{def_name}", "$defs": schema["$defs"]}
        jsonschema.Draft202012Validator(wrapper).validate(instance)

    return validate


def reference_ok(validate: Validate, instance: Any, def_name: str) -> bool:
    import jsonschema

    try:
        validate(instance, def_name)
    except jsonschema.ValidationError:
        return False
    return True


def test_schema_path_points_at_the_authoritative_file() -> None:
    path = protocol.schema_path()
    assert path.is_file()
    assert path.parts[-2:] == ("protocol", "hands.schema.json")


# --------------------------------------------------------------------------- literals vs schema


@pytest.mark.parametrize(
    ("literal", "def_name"),
    [
        (protocol.HandsState, "HandsState"),
        (protocol.PlatformName, "Platform"),
        (protocol.ErrorCode, "ErrorCode"),
        (protocol.GestureName, "GestureName"),
        (protocol.CalibrationStep, "CalibrationStep"),
        (protocol.EngageMode, "EngageMode"),
        (protocol.HandChoice, "HandChoice"),
        (protocol.AnchorChoice, "AnchorChoice"),
    ],
)
def test_literal_types_match_schema_enums(literal: Any, def_name: str, schema: dict[str, Any]) -> None:
    assert list(get_args(literal)) == schema["$defs"][def_name]["enum"]


def test_command_names_match_schema(schema: dict[str, Any]) -> None:
    assert set(schema["$defs"]["Commands"]["properties"]) == protocol.COMMAND_NAMES
    assert set(get_args(protocol.CalibrateAction)) == set(
        schema["$defs"]["CalibrateCommand"]["properties"]["action"]["enum"]
    )


def test_event_defs_cover_the_event_union(schema: dict[str, Any]) -> None:
    refs = {branch["$ref"].rsplit("/", 1)[-1] for branch in schema["$defs"]["Event"]["oneOf"]}
    assert set(protocol.EVENT_DEFS.values()) == refs
    for kind, def_name in protocol.EVENT_DEFS.items():
        assert schema["$defs"][def_name]["properties"]["type"]["const"] == kind


# --------------------------------------------------------------------------- events

DISPLAY = Display(
    id=2, name="\\\\.\\DISPLAY2", rect=Rect(-1920.4, 0, 1920, 1080.6), work=Rect(-1920, 0, 1920, 1040), primary=False
)


def all_events() -> list[dict[str, Any]]:
    displays = [protocol.display_dict(DISPLAY, used=True), protocol.display_dict(DISPLAY, used=False)]
    return [
        protocol.hello(4242, 1234, "windows", "0.1.0", ["heartbeat", "status"]),
        protocol.hello(1, 1, "linux", "0.1.0", []),
        *(protocol.state(s) for s in get_args(protocol.HandsState)),
        protocol.ready("UGREEN Camera", 1280, 720, 30, displays),
        protocol.ready(0, 640, 480, 29.97, []),
        *(protocol.gesture(g) for g in get_args(protocol.GestureName)),
        *(protocol.calibration(step) for step in get_args(protocol.CalibrationStep)),
        protocol.calibration("top_left", display="all displays"),
        protocol.error("camera_blocked", "blocked", hint="Settings > Privacy", fatal=False),
        protocol.error("already_running", "busy", fatal=True),
        protocol.error("internal", "boom"),
    ]


@pytest.mark.parametrize("event", all_events(), ids=lambda e: f"{e['type']}")
def test_builders_produce_schema_valid_events(event: dict[str, Any], reference_validator: Validate) -> None:
    assert list(event)[:2] == ["v", "type"]
    assert event["v"] == protocol.PROTOCOL_VERSION == 1
    reference_validator(event, "Event")  # exactly one oneOf branch matches
    reference_validator(event, protocol.EVENT_DEFS[event["type"]])
    json.dumps(event, allow_nan=False)  # plain JSON all the way down


def test_optional_fields_are_left_out() -> None:
    assert protocol.error("internal", "m") == {
        "v": 1,
        "type": "error",
        "code": "internal",
        "message": "m",
        "fatal": False,
    }
    assert protocol.calibration("done") == {"v": 1, "type": "calibration", "step": "done"}
    assert protocol.ready(3, 1280, 720, 30, [])["camera"] == "3"


def test_hello_copies_capabilities() -> None:
    caps = ["heartbeat"]
    event = protocol.hello(1, 2, "macos", "x", caps)
    caps.append("later")
    assert event["capabilities"] == ["heartbeat"]


@pytest.mark.parametrize("fps", [float("nan"), float("inf"), float("-inf"), -1, -0.5, 0])
def test_ready_with_an_unknown_frame_rate_is_still_sent(fps: float, reference_validator: Validate) -> None:
    # OpenCV reports -1 for an unknown rate; a mean over no frames is NaN.
    event = protocol.ready("cam", 1280, 720, fps, [])
    assert event["fps"] == 0.0
    reference_validator(event, "ReadyEvent")
    encode_line(event)  # would raise on NaN, and the writer would drop the event


def test_ready_sizes_are_at_least_one_pixel(reference_validator: Validate) -> None:
    event = protocol.ready("cam", 0, -5, 30, [])
    assert (event["width"], event["height"], event["fps"]) == (1, 1, 30.0)
    reference_validator(event, "ReadyEvent")


def test_display_dict_has_whole_pixels(reference_validator: Validate) -> None:
    wire = protocol.display_dict(DISPLAY, used=True)
    assert wire == {
        "id": 2,
        "name": "\\\\.\\DISPLAY2",
        "x": -1920,
        "y": 0,
        "width": 1920,
        "height": 1081,
        "primary": False,
        "virtual": False,
        "used": True,
    }
    assert all(type(wire[k]) is int for k in ("id", "x", "y", "width", "height"))
    reference_validator(wire, "Display")


@pytest.mark.parametrize(
    "response",
    [
        protocol.ok_response(),
        protocol.ok_response(pending=True),
        protocol.error_response("bad_request", "nope"),
        protocol.error_response("internal", ""),
    ],
)
def test_responses_match_schema(response: dict[str, Any], reference_validator: Validate) -> None:
    reference_validator(response, "CommandResponse")
    reference_validator(response, "OkResponse" if response["ok"] else "ErrorResponse")


def test_an_ok_response_only_carries_pending_when_something_is_pending() -> None:
    """``pause`` and ``resume`` say ``pending`` while the camera change is still going on; nothing else does."""
    assert protocol.ok_response() == {"ok": True}
    assert protocol.ok_response(pending=False) == {"ok": True}
    assert protocol.ok_response(pending=True) == {"ok": True, "pending": True}


# --------------------------------------------------------------------------- commands

COMMAND_CASES: list[tuple[str, Any]] = [
    *((name, {}) for name in sorted(protocol.COMMAND_NAMES - {"calibrate", "keyboard"})),
    ("heartbeat", {"x": 1}),
    ("status", {"verbose": True}),
    ("shutdown", []),
    ("pause", None),
    ("resume", "now"),
    ("engage", 1),
    (
        "config",
        {"engage": "palm", "displays": "all", "hand": "any", "anchor": "knuckles", "overlay": True, "scrollSpeed": 1},
    ),
    ("config", {"engage": "always"}),
    ("config", {"engage": "never"}),
    ("config", {"engage": None}),
    ("config", {"engage": ["palm"]}),
    ("config", {"displays": [1, 2]}),
    ("config", {"displays": []}),
    ("config", {"displays": [0]}),
    ("config", {"displays": [-1]}),
    ("config", {"displays": [1.0]}),
    ("config", {"displays": [1.5]}),
    ("config", {"displays": [True]}),
    ("config", {"displays": ["1"]}),
    ("config", {"displays": 1}),
    ("config", {"displays": "All"}),
    ("config", {"displays": "primary"}),
    ("config", {"displays": None}),
    ("config", {"displays": {"1": True}}),
    ("config", {"displays": [[1]]}),
    # Valid JSON integers far beyond a float's range must not overflow the checks.
    ("config", {"displays": [10**400]}),
    ("config", {"displays": [1, 10**400]}),
    ("config", {"displays": [-(10**400)]}),
    ("config", {"scrollSpeed": 10**400}),
    ("config", {"scrollSpeed": -(10**400)}),
    ("config", {"scrollSpeed": 1e308}),
    ("config", {"hand": "left"}),
    ("config", {"hand": "both"}),
    ("config", {"anchor": "index"}),
    ("config", {"anchor": "thumb"}),
    ("config", {"overlay": False}),
    ("config", {"overlay": 0}),
    ("config", {"overlay": "true"}),
    ("config", {"scrollSpeed": 0.1}),
    ("config", {"scrollSpeed": 10}),
    ("config", {"scrollSpeed": 10.0}),
    ("config", {"scrollSpeed": 2.5}),
    ("config", {"scrollSpeed": 0.09}),
    ("config", {"scrollSpeed": 10.01}),
    ("config", {"scrollSpeed": 0}),
    ("config", {"scrollSpeed": -1}),
    ("config", {"scrollSpeed": True}),
    ("config", {"scrollSpeed": "2"}),
    ("config", {"scrollSpeed": None}),
    ("config", {"speed": 2}),
    ("config", {"engage": "palm", "extra": 1}),
    ("config", []),
    ("config", "palm"),
    ("calibrate", {"action": "start"}),
    ("calibrate", {"action": "cancel"}),
    ("calibrate", {"action": "stop"}),
    ("calibrate", {"action": None}),
    ("calibrate", {"action": 1}),
    ("calibrate", {}),
    ("calibrate", {"action": "start", "display": 1}),
    ("calibrate", {"display": 1}),
    ("calibrate", []),
    ("keyboard", {}),
    ("keyboard", {"action": "start"}),
    ("keyboard", {"action": "practice"}),
    ("keyboard", {"action": "stop"}),
    ("keyboard", {"action": "recenter"}),
    ("keyboard", {"action": "private"}),
    ("keyboard", {"action": "public"}),
    ("keyboard", {"action": "configure", "settings": {}}),
    ("keyboard", {"action": "configure", "settings": {"enabled": True, "commit": "review", "idleS": 30}}),
    ("keyboard", {"action": "configure"}),
    ("keyboard", {"action": "configure", "settings": {"commit": "auto"}}),
    ("keyboard", {"action": "start", "settings": {"enabled": True}}),
    ("keyboard", {"action": "insert"}),
    ("keyboard", {"action": "send"}),
    ("keyboard", {"action": "clear"}),
    ("keyboard", {"action": "type", "text": "x"}),
    ("keyboard", {"action": "start", "text": "x"}),
    ("keyboard", {"action": None}),
    ("keyboard", {"action": ["start"]}),
    ("keyboard", {"settings": {"enabled": True}}),
    ("keyboard", []),
    ("keyboard", "start"),
    ("keyboard", None),
]


def command_ok(name: str, body: Any) -> bool:
    try:
        protocol.validate_command(name, body)
    except protocol.ValidationError:
        return False
    return True


def command_def(name: str, schema: dict[str, Any]) -> str:
    ref: str = schema["$defs"]["Commands"]["properties"][name]["$ref"]
    return ref.rsplit("/", 1)[-1]


@pytest.mark.parametrize(("name", "body"), COMMAND_CASES)
def test_command_validation_agrees_with_jsonschema(
    name: str, body: Any, schema: dict[str, Any], reference_validator: Validate
) -> None:
    expected = reference_ok(reference_validator, body, command_def(name, schema))
    assert command_ok(name, body) is expected


def random_config_bodies(count: int, seed: int = 20261007) -> list[Any]:
    """Mixtures of good and bad values for every config key, plus stray keys."""
    rng = random.Random(seed)
    pool: dict[str, list[Any]] = {
        "engage": ["palm", "always", "PALM", "", None, 1],
        "displays": ["all", [1], [1, 3], [], [0], [2.0], [2.5], ["2"], "none", 3, [True]],
        "hand": ["right", "left", "any", "both", False],
        "anchor": ["knuckles", "index", "wrist", 0],
        "overlay": [True, False, 1, "yes", None],
        "scrollSpeed": [0.1, 1, 5.5, 10, 0.05, 11, -3, "1", False, 1e9],
        "extra": [1],
        "Engage": ["palm"],
    }
    bodies: list[Any] = []
    keys = list(pool)
    for _ in range(count):
        chosen = rng.sample(keys, rng.randint(0, 4))
        bodies.append({key: rng.choice(pool[key]) for key in chosen})
    return bodies


def test_random_config_bodies_agree_with_jsonschema(schema: dict[str, Any], reference_validator: Validate) -> None:
    bodies = random_config_bodies(600)
    outcomes = [reference_ok(reference_validator, body, "ConfigCommand") for body in bodies]
    assert any(outcomes) and not all(outcomes)  # the sample has both kinds
    for body, expected in zip(bodies, outcomes, strict=True):
        assert command_ok("config", body) is expected, body


def test_every_command_with_every_simple_body_agrees(schema: dict[str, Any], reference_validator: Validate) -> None:
    bodies: list[Any] = [{}, {"action": "start"}, {"action": "cancel"}, {"engage": "palm"}, [], "x", 0, None, True]
    for name, body in itertools.product(sorted(protocol.COMMAND_NAMES), bodies):
        expected = reference_ok(reference_validator, body, command_def(name, schema))
        assert command_ok(name, body) is expected, (name, body)


def test_unknown_command_is_rejected() -> None:
    with pytest.raises(protocol.ValidationError, match="unknown command"):
        protocol.validate_command("format_disk", {})
    with pytest.raises(protocol.ValidationError):
        protocol.validate_command("", {})


def test_validation_errors_name_the_field() -> None:
    with pytest.raises(protocol.ValidationError, match="scrollSpeed"):
        protocol.validate_command("config", {"scrollSpeed": 50})
    with pytest.raises(protocol.ValidationError, match="displays"):
        protocol.validate_command("config", {"displays": [0]})
    with pytest.raises(protocol.ValidationError, match="'action'"):
        protocol.validate_command("calibrate", {})
    with pytest.raises(protocol.ValidationError, match="'nope'"):
        protocol.validate_command("pause", {"nope": 1})
    assert issubclass(protocol.ValidationError, ValueError)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers_are_refused(value: float) -> None:
    # Deliberately stricter than the schema validator: NaN and infinity are not JSON.
    assert not command_ok("config", {"scrollSpeed": value})
    assert not command_ok("config", {"displays": [value]})


# --------------------------------------------------------------------------- the sensitivity knobs


def knob_values(low: float, high: float) -> list[Any]:
    """The edges, just inside and just outside them, the middle, and what is no number."""
    return [
        low,
        high,
        (low + high) / 2,
        low + (high - low) * 0.001,
        high - (high - low) * 0.001,
        low - 1e-9,
        high + 1e-9,
        low - 1,
        high + 1,
        int(high) if high == int(high) else high,
        10**400,
        -(10**400),
        True,
        False,
        "1",
        None,
        [low],
        {"v": low},
    ]


KNOB_CASES = [(knob.key, value) for knob in protocol.KNOBS for value in knob_values(knob.low, knob.high)]


@pytest.mark.parametrize(("key", "value"), KNOB_CASES, ids=[f"{k}={v!r:.20}" for k, v in KNOB_CASES])
def test_knob_validation_agrees_with_jsonschema(
    key: str, value: Any, schema: dict[str, Any], reference_validator: Validate
) -> None:
    body = {key: value}
    assert command_ok("config", body) is reference_ok(reference_validator, body, "ConfigCommand")


def test_every_knob_in_one_body_and_unknown_keys(schema: dict[str, Any], reference_validator: Validate) -> None:
    everything = {knob.key: knob.default for knob in protocol.KNOBS}
    for body in (everything, {**everything, "extra": 1}, {**everything, "pinch": 99}, {**everything, "fist": None}):
        assert command_ok("config", body) is reference_ok(reference_validator, body, "ConfigCommand")
    assert command_ok("config", everything) and not command_ok("config", {**everything, "extra": 1})


def test_random_bodies_over_every_knob_agree_with_jsonschema(
    schema: dict[str, Any], reference_validator: Validate
) -> None:
    rng = random.Random(20261008)
    pool = {knob.key: knob_values(knob.low, knob.high) for knob in protocol.KNOBS}
    pool["engage"] = ["palm", "always", "PALM"]
    pool["overlay"] = [True, False, 1]
    outcomes = []
    for _ in range(800):
        body = {key: rng.choice(pool[key]) for key in rng.sample(list(pool), rng.randint(0, 5))}
        expected = reference_ok(reference_validator, body, "ConfigCommand")
        outcomes.append(expected)
        assert command_ok("config", body) is expected, body
    assert any(outcomes) and not all(outcomes)


def test_a_knob_error_names_the_field_and_the_range() -> None:
    for knob in protocol.KNOBS:
        with pytest.raises(protocol.ValidationError, match=knob.key) as caught:
            protocol.validate_command("config", {knob.key: knob.high + 1})
        assert f"{knob.low:g}" in str(caught.value) and f"{knob.high:g}" in str(caught.value)
        with pytest.raises(protocol.ValidationError, match=f"{knob.key}: expected number"):
            protocol.validate_command("config", {knob.key: "fast"})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_knob_values_are_refused(value: float) -> None:
    for knob in protocol.KNOBS:
        assert not command_ok("config", {knob.key: value})


# --------------------------------------------------------------------------- the keyboard command, event and status

KEYBOARD_ENUMS = [
    (protocol.KeyboardState, "KeyboardState"),
    (protocol.KeyboardPhase, "KeyboardPhase"),
    (protocol.KeyboardPress, "KeyboardPress"),
    (protocol.KeyboardLevel, "KeyboardLevel"),
    (protocol.KeyboardCommit, "KeyboardCommit"),
    (protocol.KeyboardLang, "KeyboardLang"),
    (protocol.KeyboardHold, "KeyboardHold"),
    (protocol.KeyboardCloseReason, "KeyboardCloseReason"),
    (protocol.ReviewState, "ReviewState"),
    (protocol.InsertAbort, "InsertAbort"),
]


@pytest.mark.parametrize(("literal", "def_name"), KEYBOARD_ENUMS, ids=[name for _, name in KEYBOARD_ENUMS])
def test_keyboard_literals_match_schema_enums(literal: Any, def_name: str, schema: dict[str, Any]) -> None:
    """P1: the Literals the helper uses and the enums the mod reads are one list."""
    assert list(get_args(literal)) == schema["$defs"][def_name]["enum"]


def test_keyboard_actions_match_the_schema(schema: dict[str, Any]) -> None:
    command = schema["$defs"]["KeyboardCommand"]["oneOf"]
    plain = next(branch for branch in command if "settings" not in branch["properties"])
    assert sorted(protocol.KEYBOARD_ACTIONS) == sorted([*plain["properties"]["action"]["enum"], "configure"])
    assert sorted(protocol.KEYBOARD_ACTIONS) == [
        "configure",
        "practice",
        "private",
        "public",
        "recenter",
        "start",
        "stop",
    ]


def test_the_keyboard_command_and_event_are_in_the_contract(schema: dict[str, Any]) -> None:
    assert "keyboard" in protocol.COMMAND_NAMES
    assert protocol.EVENT_DEFS["keyboard"] == "KeyboardEvent"
    assert command_def("keyboard", schema) == "KeyboardCommand"
    assert "keyboard" in get_args(protocol.CommandName)


def test_keyboard_settings_in_the_schema_are_the_ten_wire_keys(schema: dict[str, Any]) -> None:
    settings = schema["$defs"]["KeyboardSettings"]
    assert settings["additionalProperties"] is False and "required" not in settings
    assert sorted(settings["properties"]) == sorted(
        ["enabled", "press", "commit", "layout", "size", "reach", "dock", "idleS", "inject", "enter"]
    )


# ---- P40: the examples and the cases of the review reference model (as parsed objects, never as strings)

EXAMPLE_EVENTS: list[tuple[dict[str, Any], dict[str, Any]]] = [
    (
        {
            "state": "open",
            "phase": "placing",
            "press": "air",
            "level": "ok",
            "commit": "review",
            "lang": "en",
            "private": False,
        },
        {
            "v": 1,
            "type": "keyboard",
            "state": "open",
            "phase": "placing",
            "press": "air",
            "level": "ok",
            "commit": "review",
            "lang": "en",
            "private": False,
        },
    ),
    (
        {
            "state": "open",
            "phase": "typing",
            "press": "air",
            "level": "ok",
            "commit": "review",
            "lang": "en",
            "private": False,
            "review": {"state": "composing", "chars": 37},
        },
        {
            "v": 1,
            "type": "keyboard",
            "state": "open",
            "phase": "typing",
            "press": "air",
            "level": "ok",
            "commit": "review",
            "lang": "en",
            "private": False,
            "review": {"state": "composing", "chars": 37},
        },
    ),
    (
        {
            "state": "open",
            "phase": "typing",
            "press": "air",
            "level": "degraded",
            "commit": "review",
            "lang": "he",
            "private": False,
            "hold": "yield",
            "review": {"state": "inserting", "chars": 37},
        },
        {
            "v": 1,
            "type": "keyboard",
            "state": "open",
            "phase": "typing",
            "press": "air",
            "level": "degraded",
            "commit": "review",
            "lang": "he",
            "private": False,
            "hold": "yield",
            "review": {"state": "inserting", "chars": 37},
        },
    ),
    (
        {
            "state": "open",
            "phase": "typing",
            "press": "air",
            "level": "ok",
            "commit": "review",
            "lang": "en",
            "private": False,
            "review": {
                "state": "composing",
                "chars": 0,
                "insert": {"kind": "text", "outcome": "done", "sent": 37, "of": 37},
            },
        },
        {
            "v": 1,
            "type": "keyboard",
            "state": "open",
            "phase": "typing",
            "press": "air",
            "level": "ok",
            "commit": "review",
            "lang": "en",
            "private": False,
            "review": {
                "state": "composing",
                "chars": 0,
                "insert": {"kind": "text", "outcome": "done", "sent": 37, "of": 37},
            },
        },
    ),
    (
        {
            "state": "open",
            "phase": "typing",
            "press": "air",
            "level": "ok",
            "commit": "review",
            "lang": "he",
            "private": False,
            "review": {
                "state": "aborted",
                "chars": 25,
                "insert": {"kind": "text", "outcome": "aborted", "sent": 12, "of": 37, "reason": "focus"},
            },
        },
        {
            "v": 1,
            "type": "keyboard",
            "state": "open",
            "phase": "typing",
            "press": "air",
            "level": "ok",
            "commit": "review",
            "lang": "he",
            "private": False,
            "review": {
                "state": "aborted",
                "chars": 25,
                "insert": {"kind": "text", "outcome": "aborted", "sent": 12, "of": 37, "reason": "focus"},
            },
        },
    ),
    (
        {
            "state": "open",
            "phase": "typing",
            "press": "air",
            "level": "ok",
            "commit": "review",
            "lang": "en",
            "private": False,
            "review": {
                "state": "composing",
                "chars": 0,
                "insert": {"kind": "enter", "outcome": "done", "sent": 1, "of": 1},
            },
        },
        {
            "v": 1,
            "type": "keyboard",
            "state": "open",
            "phase": "typing",
            "press": "air",
            "level": "ok",
            "commit": "review",
            "lang": "en",
            "private": False,
            "review": {
                "state": "composing",
                "chars": 0,
                "insert": {"kind": "enter", "outcome": "done", "sent": 1, "of": 1},
            },
        },
    ),
    (
        {
            "state": "open",
            "phase": "warmup",
            "press": "pinch",
            "level": "off",
            "commit": "review",
            "lang": "en",
            "private": False,
            "review": {"state": "composing", "chars": 12},
        },
        {
            "v": 1,
            "type": "keyboard",
            "state": "open",
            "phase": "warmup",
            "press": "pinch",
            "level": "off",
            "commit": "review",
            "lang": "en",
            "private": False,
            "review": {"state": "composing", "chars": 12},
        },
    ),
    (
        {"state": "open", "phase": "typing", "press": "pinch", "commit": "direct", "lang": "en", "private": False},
        {
            "v": 1,
            "type": "keyboard",
            "state": "open",
            "phase": "typing",
            "press": "pinch",
            "commit": "direct",
            "lang": "en",
            "private": False,
        },
    ),
    (
        {"state": "practice", "phase": "placing", "press": "air", "level": "ok", "lang": "en", "private": False},
        {
            "v": 1,
            "type": "keyboard",
            "state": "practice",
            "phase": "placing",
            "press": "air",
            "level": "ok",
            "lang": "en",
            "private": False,
        },
    ),
    (
        {"state": "closed", "reason": "fists", "discarded": 25},
        {"v": 1, "type": "keyboard", "state": "closed", "reason": "fists", "discarded": 25},
    ),
    (
        {"state": "closed", "reason": "air_unreliable", "discarded": 0},
        {"v": 1, "type": "keyboard", "state": "closed", "reason": "air_unreliable", "discarded": 0},
    ),
    (
        {
            "state": "closed",
            "reason": "command",
            "discarded": 0,
            "practice": {"hitRate": 0.93, "phantomsPerMin": 0.0, "recallIM": 0.92},
        },
        {
            "v": 1,
            "type": "keyboard",
            "state": "closed",
            "reason": "command",
            "discarded": 0,
            "practice": {"hitRate": 0.93, "phantomsPerMin": 0.0, "recallIM": 0.92},
        },
    ),
]


@pytest.mark.parametrize(("args", "wire"), EXAMPLE_EVENTS, ids=[str(i + 1) for i in range(len(EXAMPLE_EVENTS))])
def test_the_design_examples_validate_and_are_what_the_builder_makes(
    args: dict[str, Any], wire: dict[str, Any], reference_validator: Validate
) -> None:
    reference_validator(wire, "Event")  # exactly one branch of the union
    reference_validator(wire, "KeyboardEvent")
    built = protocol.keyboard(**args)
    assert built == wire  # as parsed objects: key order is the next test's business
    reference_validator(built, "Event")


def test_the_builder_key_order_is_pinned() -> None:
    """P3 and P41: the order of the keys on the wire, and None fields left out."""
    full = protocol.keyboard(
        "closed",
        phase="typing",
        reason="fists",
        hold="yield",
        lang="en",
        press="air",
        level="ok",
        commit="review",
        private=False,
        review={
            "insert": {"reason": "focus", "of": 3, "sent": 1, "outcome": "aborted", "kind": "text"},
            "chars": 2,
            "state": "aborted",
        },
        discarded=2,
        practice={"recallIM": 0.9, "phantomsPerMin": 0.5, "hitRate": 0.8},
    )
    assert list(full) == [
        "v",
        "type",
        "state",
        "phase",
        "reason",
        "hold",
        "lang",
        "press",
        "level",
        "commit",
        "private",
        "review",
        "discarded",
        "practice",
    ]
    assert list(full["review"]) == ["state", "chars", "insert"]
    assert list(full["review"]["insert"]) == ["kind", "outcome", "sent", "of", "reason"]
    assert list(full["practice"]) == ["hitRate", "phantomsPerMin", "recallIM"]
    assert list(protocol.keyboard("closed")) == ["v", "type", "state"]
    assert protocol.keyboard("closed") == {"v": 1, "type": "keyboard", "state": "closed"}


def test_the_builder_takes_only_the_state_positionally() -> None:
    params = inspect.signature(protocol.keyboard).parameters
    kinds = [p.kind for p in params.values()]
    assert next(iter(params)) == "state"
    assert all(k is inspect.Parameter.KEYWORD_ONLY for k in kinds[1:])


def test_the_builder_has_a_parameter_for_every_event_field_and_no_other(schema: dict[str, Any]) -> None:
    """No parameter can carry text: the builder's names are the schema's (the event is enums and numbers)."""
    event_fields = set(schema["$defs"]["KeyboardEvent"]["properties"]) - {"v", "type"}
    assert set(inspect.signature(protocol.keyboard).parameters) - {"state"} == event_fields - {"state"}


def test_review_and_practice_are_rebuilt_from_known_fields_only() -> None:
    event = protocol.keyboard(
        "open",
        review={
            "state": "composing",
            "chars": 3,
            "text": "abc",
            "insert": {"kind": "text", "outcome": "done", "sent": 1, "of": 1, "title": "x"},
        },
        practice={"hitRate": 0.5, "phantomsPerMin": 0.1, "keys": "abc"},
    )
    assert event["review"] == {
        "state": "composing",
        "chars": 3,
        "insert": {"kind": "text", "outcome": "done", "sent": 1, "of": 1},
    }
    assert event["practice"] == {"hitRate": 0.5, "phantomsPerMin": 0.1}
    assert "abc" not in json.dumps(event)


def test_the_builder_clamps_counts_and_keeps_the_event_encodable(reference_validator: Validate) -> None:
    event = protocol.keyboard(
        "open",
        review={
            "state": "composing",
            "chars": 999,
            "insert": {"kind": "text", "outcome": "aborted", "sent": -4, "of": 0, "reason": "failed"},
        },
        discarded=10**6,
    )
    assert event["review"]["chars"] == 200
    assert (event["review"]["insert"]["sent"], event["review"]["insert"]["of"]) == (0, 1)
    assert event["discarded"] == 200
    reference_validator(event, "KeyboardEvent")
    hostile = protocol.keyboard(
        "closed",
        discarded=-3,
        practice={"hitRate": float("nan"), "phantomsPerMin": float("inf"), "recallIM": float("-inf")},
    )
    assert hostile["discarded"] == 0
    assert hostile["practice"] == {"hitRate": 0.0, "phantomsPerMin": 0.0, "recallIM": 0.0}
    reference_validator(hostile, "KeyboardEvent")
    encode_line(hostile)  # NaN would make the event unencodable and the mod would never hear the session closed
    over = protocol.keyboard("closed", practice={"hitRate": 7.0, "phantomsPerMin": -2.0})
    assert over["practice"] == {"hitRate": 1.0, "phantomsPerMin": 0.0}


def test_the_builder_does_not_alias_its_arguments() -> None:
    review = {"state": "composing", "chars": 1}
    event = protocol.keyboard("open", review=review)
    review["chars"] = 99
    assert event["review"]["chars"] == 1


def test_private_is_a_real_boolean() -> None:
    assert protocol.keyboard("open", private=1)["private"] is True
    assert protocol.keyboard("open", private=0)["private"] is False
    assert "private" not in protocol.keyboard("open")


GOOD_REVIEW_EVENTS = [
    {
        "v": 1,
        "type": "keyboard",
        "state": "open",
        "phase": "placing",
        "press": "air",
        "commit": "review",
        "lang": "en",
        "private": False,
    },
    {
        "v": 1,
        "type": "keyboard",
        "state": "open",
        "phase": "typing",
        "press": "air",
        "commit": "review",
        "lang": "en",
        "private": False,
        "review": {"state": "composing", "chars": 37},
    },
    {
        "v": 1,
        "type": "keyboard",
        "state": "open",
        "phase": "typing",
        "press": "air",
        "commit": "review",
        "lang": "en",
        "private": False,
        "review": {"state": "inserting", "chars": 37},
    },
    {
        "v": 1,
        "type": "keyboard",
        "state": "open",
        "phase": "typing",
        "press": "air",
        "commit": "review",
        "lang": "en",
        "private": False,
        "review": {
            "state": "composing",
            "chars": 0,
            "insert": {"kind": "text", "outcome": "done", "sent": 37, "of": 37},
        },
    },
    {
        "v": 1,
        "type": "keyboard",
        "state": "open",
        "phase": "typing",
        "press": "air",
        "commit": "review",
        "lang": "he",
        "private": False,
        "review": {
            "state": "aborted",
            "chars": 25,
            "insert": {"kind": "text", "outcome": "aborted", "sent": 12, "of": 37, "reason": "focus"},
        },
    },
    {
        "v": 1,
        "type": "keyboard",
        "state": "open",
        "phase": "typing",
        "press": "air",
        "commit": "review",
        "lang": "en",
        "private": False,
        "review": {
            "state": "composing",
            "chars": 0,
            "insert": {"kind": "enter", "outcome": "done", "sent": 1, "of": 1},
        },
    },
    {"v": 1, "type": "keyboard", "state": "closed", "reason": "fists", "discarded": 25},
    {"v": 1, "type": "keyboard", "state": "closed", "reason": "command", "discarded": 0},
    {
        "v": 1,
        "type": "keyboard",
        "state": "open",
        "phase": "typing",
        "press": "pinch",
        "commit": "direct",
        "lang": "en",
        "private": False,
    },
]
BAD_REVIEW_EVENTS = [
    # the three required fields, each missing alone
    {"v": 1, "type": "keyboard"},
    {"v": 1, "state": "open"},
    {"type": "keyboard", "state": "open"},
    {"v": 1, "type": "keyboard", "state": "ajar"},
    {"v": 1, "type": "keyboard", "state": "open", "review": {"state": "composing", "chars": 3, "text": "abc"}},
    {"v": 1, "type": "keyboard", "state": "open", "text": "abc"},
    {"v": 1, "type": "keyboard", "state": "open", "review": {"state": "typing", "chars": 3}},
    {"v": 1, "type": "keyboard", "state": "open", "review": {"state": "composing", "chars": 201}},
    {"v": 1, "type": "keyboard", "state": "open", "review": {"state": "composing", "chars": -1}},
    {"v": 1, "type": "keyboard", "state": "open", "commit": "auto"},
    {"v": 1, "type": "keyboard", "state": "closed", "reason": "fists", "discarded": "25"},
    {
        "v": 1,
        "type": "keyboard",
        "state": "open",
        "review": {
            "state": "aborted",
            "chars": 1,
            "insert": {"kind": "text", "outcome": "aborted", "sent": 1, "of": 2, "reason": "window"},
        },
    },
    {
        "v": 1,
        "type": "keyboard",
        "state": "open",
        "review": {
            "state": "aborted",
            "chars": 1,
            "insert": {"kind": "text", "outcome": "aborted", "sent": 1, "of": 2, "reason": "focus", "title": "x"},
        },
    },
]
GOOD_REVIEW_COMMANDS = [
    {"action": "start"},
    {"action": "practice"},
    {"action": "stop"},
    {"action": "recenter"},
    {"action": "private"},
    {"action": "public"},
    {
        "action": "configure",
        "settings": {
            "enabled": True,
            "press": "air",
            "commit": "review",
            "layout": "auto",
            "size": 1.0,
            "reach": 1.0,
            "dock": "top",
            "idleS": 30,
            "inject": "unicode",
            "enter": "twice",
        },
    },
    {"action": "configure", "settings": {"commit": "direct"}},
]
BAD_REVIEW_COMMANDS = [
    {"action": "insert"},
    {"action": "send"},
    {"action": "clear"},
    {"action": "type", "text": "x"},
    {"action": "start", "text": "x"},
    {"action": "configure", "settings": {"commit": "auto"}},
    {"action": "configure", "settings": {"text": "x"}},
    {"action": "configure"},
]


@pytest.mark.parametrize("event", GOOD_REVIEW_EVENTS, ids=lambda e: json.dumps(e)[40:90])
def test_review_reference_events_validate(event: dict[str, Any], reference_validator: Validate) -> None:
    reference_validator(event, "Event")


def bad_event_id(event: dict[str, Any]) -> str:
    text = json.dumps(event)
    return text if len(text) < 60 else text[28:100]  # the long ones share their first words


@pytest.mark.parametrize("event", BAD_REVIEW_EVENTS, ids=bad_event_id)
def test_review_reference_bad_events_are_rejected(event: dict[str, Any], reference_validator: Validate) -> None:
    assert not reference_ok(reference_validator, event, "KeyboardEvent")
    assert not reference_ok(reference_validator, event, "Event")


@pytest.mark.parametrize("body", GOOD_REVIEW_COMMANDS, ids=lambda b: json.dumps(b)[:50])
def test_review_reference_commands_validate_in_both_validators(
    body: dict[str, Any], reference_validator: Validate
) -> None:
    reference_validator(body, "KeyboardCommand")
    protocol.validate_command("keyboard", body)


@pytest.mark.parametrize("body", BAD_REVIEW_COMMANDS, ids=lambda b: json.dumps(b)[:50])
def test_review_reference_bad_commands_are_rejected_by_both_validators(
    body: dict[str, Any], reference_validator: Validate
) -> None:
    assert not reference_ok(reference_validator, body, "KeyboardCommand")
    assert not command_ok("keyboard", body)


# ---- S55: no command, field or action can carry text, or insert, send or clear anything

TEXT_CARRIERS = [
    {"action": "insert"},
    {"action": "send"},
    {"action": "clear"},
    {"action": "type"},
    {"action": "text"},
    {"action": "enter"},
    {"action": "insert", "text": "x"},
    {"action": "start", "text": "x"},
    {"action": "start", "insert": True},
    {"action": "stop", "send": True},
    {"action": "configure", "settings": {"text": "x"}},
    {"action": "configure", "settings": {"insert": True}},
    {"action": "configure", "settings": {"send": True}},
    {"action": "configure", "settings": {"clear": True}},
    {"action": "configure", "settings": {"type": "x"}},
    {"action": "configure", "settings": {"enabled": True}, "text": "x"},
    {"action": "configure", "settings": {"decoder": "off"}},  # the follow-on's key: not in step 1
    {"text": "x"},
    {"insert": 1},
]


@pytest.mark.parametrize("body", TEXT_CARRIERS, ids=lambda b: json.dumps(b)[:60])
def test_no_keyboard_body_can_insert_send_clear_or_carry_text(
    body: dict[str, Any], reference_validator: Validate
) -> None:
    assert not command_ok("keyboard", body)
    assert not reference_ok(reference_validator, body, "KeyboardCommand")


def test_the_keyboard_defs_hold_enums_and_numbers_only(schema: dict[str, Any]) -> None:
    """SR15: nothing typed, no key name, window title or executable name has a place in an event or the status."""
    forbidden = {
        "text",
        "typed",
        "key",
        "keys",
        "title",
        "window",
        "exe",
        "name",
        "word",
        "buffer",
        "char",
        "chars_text",
    }
    defs = schema["$defs"]

    def walk(node: Any, where: str) -> None:
        if isinstance(node, dict):
            if node.get("type") == "string":
                raise AssertionError(f"free string at {where}")
            for key, value in node.items():
                if key == "properties":
                    assert not forbidden & set(value), where
                    for prop, sub in value.items():
                        walk(sub, f"{where}.{prop}")
                else:
                    walk(value, f"{where}.{key}")
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, f"{where}[{i}]")

    for name in defs:
        if name.startswith("Keyboard") or name in ("ReviewState", "InsertAbort"):
            walk(defs[name], name)


def test_compose_max_is_the_schema_maximum(schema: dict[str, Any]) -> None:
    """P41: the cap of the box and the cap the mod is told about are one number."""
    defs = schema["$defs"]
    assert defs["KeyboardReview"]["properties"]["chars"]["maximum"] == limits.COMPOSE_MAX
    assert defs["KeyboardReviewStatus"]["properties"]["chars"]["maximum"] == limits.COMPOSE_MAX
    assert defs["KeyboardInsert"]["properties"]["sent"]["maximum"] == limits.COMPOSE_MAX
    assert defs["KeyboardInsert"]["properties"]["of"]["maximum"] == limits.COMPOSE_MAX
    assert defs["KeyboardEvent"]["properties"]["discarded"]["maximum"] == limits.COMPOSE_MAX


# ---- P2 and P41: the hand-written validator equals jsonschema


SETTINGS_POOL: dict[str, list[Any]] = {
    "enabled": [True, False, 1, "true", None],
    "press": ["air", "pinch", "windows", "osk", None, 1],
    "commit": ["review", "direct", "auto", None],
    "layout": ["auto", "en", "he", "fr", None],
    "size": [0.6, 1.0, 1.6, 1, 0.59, 1.61, True, "1", None, 1e9, 10**400],
    "reach": [0.8, 1.0, 1.5, 0.79, 1.51, False, "1", None],
    "dock": ["top", "bottom", "left", None],
    "idleS": [5, 30, 300, 4, 301, 30.0, 30.5, True, "30", None, 10**400],
    "inject": ["unicode", "vk", "both", None],
    "enter": ["twice", "off", "once", None],
    "extra": [1],
    "text": ["x"],
}


def random_keyboard_bodies(count: int, seed: int = 20261008) -> list[Any]:
    rng = random.Random(seed)
    actions: list[Any] = [
        "start",
        "practice",
        "stop",
        "recenter",
        "private",
        "public",
        "configure",
        "configure",
        "configure",
    ]
    wrong: list[Any] = ["insert", "send", "clear", "type", "Start", "", None, 1, ["start"], True]
    bodies: list[Any] = []
    for _ in range(count):
        body: dict[str, Any] = {}
        if rng.random() < 0.93:
            body["action"] = rng.choice(actions) if rng.random() < 0.85 else rng.choice(wrong)
        if (body.get("action") == "configure" and rng.random() < 0.9) or rng.random() < 0.08:
            if rng.random() < 0.9:
                keys = rng.sample(list(SETTINGS_POOL), rng.randint(0, 5))
                body["settings"] = {key: rng.choice(SETTINGS_POOL[key]) for key in keys}
            else:
                body["settings"] = rng.choice([None, [], "x", 5, True])
        if rng.random() < 0.05:
            body["text"] = "x"
        bodies.append(body)
    return bodies


def test_random_keyboard_bodies_agree_with_jsonschema(schema: dict[str, Any], reference_validator: Validate) -> None:
    bodies = random_keyboard_bodies(500)
    outcomes = [reference_ok(reference_validator, body, "KeyboardCommand") for body in bodies]
    assert sum(outcomes) > 50 and not all(outcomes)  # both kinds are well represented
    for body, expected in zip(bodies, outcomes, strict=True):
        assert command_ok("keyboard", body) is expected, body


def test_more_random_keyboard_bodies_with_other_seeds(schema: dict[str, Any], reference_validator: Validate) -> None:
    for seed in (1, 2, 3):
        for body in random_keyboard_bodies(300, seed):
            assert command_ok("keyboard", body) is reference_ok(reference_validator, body, "KeyboardCommand"), body


@pytest.mark.parametrize("key", [k for k in SETTINGS_POOL if k not in ("extra", "text")])
def test_every_setting_value_agrees_with_jsonschema(
    key: str, schema: dict[str, Any], reference_validator: Validate
) -> None:
    edges: list[Any] = [*SETTINGS_POOL[key], [], {}, 0, -1, 2**63, float("nan"), float("inf")]
    for value in edges:
        body = {"action": "configure", "settings": {key: value}}
        if isinstance(value, float) and not math.isfinite(value):
            # Deliberately stricter than the reference: NaN and infinity are not JSON.
            assert not command_ok("keyboard", body)
            continue
        assert command_ok("keyboard", body) is reference_ok(reference_validator, body, "KeyboardCommand"), body


def test_keyboard_validation_errors_name_the_field_and_never_echo_the_value() -> None:
    sentinel = "zzqxjv"
    with pytest.raises(protocol.ValidationError, match=r"settings\.size"):
        protocol.validate_command("keyboard", {"action": "configure", "settings": {"size": 5}})
    with pytest.raises(protocol.ValidationError, match=r"settings\.idleS"):
        protocol.validate_command("keyboard", {"action": "configure", "settings": {"idleS": 3}})
    with pytest.raises(protocol.ValidationError, match="'action'"):
        protocol.validate_command("keyboard", {})
    with pytest.raises(protocol.ValidationError, match="settings"):
        protocol.validate_command("keyboard", {"action": "configure"})
    for body in (
        {"action": sentinel},
        {"action": "configure", "settings": {"press": sentinel}},
        {"action": "configure", "settings": {"size": sentinel}},
        {"action": "configure", "settings": {"enabled": sentinel}},
        {"action": "configure", "settings": sentinel},
        {"action": "configure", "settings": {sentinel: 1}},
        {sentinel: 1},
    ):
        with pytest.raises(protocol.ValidationError) as caught:
            protocol.validate_command("keyboard", body)
        assert sentinel not in str(caught.value)


def test_a_keyboard_refusal_does_not_repeat_the_name_of_an_unexpected_property() -> None:
    """The control server returns the text as it is; a body may hold anything, so the text names no key of it."""
    sentinel = "zzqxjv"
    for body in (
        {"action": "start", sentinel: 1},
        {"action": "stop", sentinel: {sentinel: sentinel}},
        {"action": "configure", "settings": {"commit": "review"}, sentinel: 1},
    ):
        with pytest.raises(protocol.ValidationError) as caught:
            protocol.validate_command("keyboard", body)
        assert str(caught.value) == "$: unexpected property"
    # the other commands keep the text they have on main, which the mod's older-helper check reads
    with pytest.raises(protocol.ValidationError, match="unexpected property 'nope'"):
        protocol.validate_command("pause", {"nope": 1})


def test_bools_and_fractions_are_not_integers_for_idle_seconds() -> None:
    ok = {"action": "configure", "settings": {"idleS": 30}}
    assert command_ok("keyboard", ok)
    assert command_ok("keyboard", {"action": "configure", "settings": {"idleS": 30.0}})
    for bad in (True, 30.5, "30", None):
        assert not command_ok("keyboard", {"action": "configure", "settings": {"idleS": bad}})


# ---- P4, X34a: the status and the old and new events


def test_status_keyboard_is_optional_and_settings_are_unchanged(schema: dict[str, Any]) -> None:
    status = schema["$defs"]["StatusResponse"]
    assert "keyboard" in status["properties"] and "keyboard" not in status["required"]
    assert status["properties"]["keyboard"] == {"$ref": "#/$defs/KeyboardStatus"}
    # the pointer settings of the status are exactly the thirteen they are on main (C8)
    assert status["properties"]["settings"]["required"] == [
        "engage",
        "hand",
        "anchor",
        "overlay",
        "cursorSpeed",
        "smoothing",
        "pinch",
        "fist",
        "engageSeconds",
        "dragDistance",
        "flingSensitivity",
        "scrollSpeed",
        "deadZone",
    ]
    assert sorted(HandsSettings().status()) == sorted(status["properties"]["settings"]["properties"])


def a_status(**keyboard: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "ok": True,
        "state": "active",
        "version": "0.1.0",
        "platform": "windows",
        "engaged": False,
        "fps": 30.0,
        "inferMs": 8.0,
        "displays": [],
        "settings": HandsSettings().status(),
    }
    if keyboard:
        body["keyboard"] = keyboard
    return body


STATUS_KEYBOARD = [
    {"enabled": False, "state": "closed", "practiced": False},
    {"enabled": True, "state": "closed", "practiced": True, "press": "air", "commit": "review"},
    {
        "enabled": True,
        "state": "open",
        "phase": "typing",
        "press": "air",
        "level": "ok",
        "airFps": 29.8,
        "airNoise": 0.017,
        "commit": "review",
        "lang": "en",
        "hold": "yield",
        "private": False,
        "practiced": True,
        "phantomsPerMin": 0.0,
        "review": {"state": "composing", "chars": 12},
    },
]


@pytest.mark.parametrize("keyboard", STATUS_KEYBOARD, ids=["minimal", "closed", "full"])
def test_status_with_a_keyboard_object_validates(keyboard: dict[str, Any], reference_validator: Validate) -> None:
    reference_validator(a_status(**keyboard), "StatusResponse")
    reference_validator(a_status(**keyboard), "CommandResponse")
    reference_validator(keyboard, "KeyboardStatus")


def test_status_without_a_keyboard_object_still_validates(reference_validator: Validate) -> None:
    reference_validator(a_status(), "StatusResponse")


@pytest.mark.parametrize(
    "bad",
    [
        {"enabled": True, "state": "open"},  # practiced is required
        {"state": "open", "practiced": True},
        {"enabled": True, "state": "open", "practiced": True, "text": "x"},
        {"enabled": True, "state": "idle", "practiced": True},
        {"enabled": True, "state": "open", "practiced": True, "airFps": -1},
        {"enabled": True, "state": "open", "practiced": True, "airNoise": -0.1},
        {
            "enabled": True,
            "state": "open",
            "practiced": True,
            "review": {"state": "composing", "chars": 3, "insert": {}},
        },
        {"enabled": True, "state": "open", "practiced": True, "review": {"state": "composing", "chars": 201}},
        {"enabled": 1, "state": "open", "practiced": True},
    ],
    ids=lambda b: json.dumps(b)[:60],
)
def test_bad_keyboard_status_is_rejected(bad: dict[str, Any], reference_validator: Validate) -> None:
    assert not reference_ok(reference_validator, bad, "KeyboardStatus")


def test_status_review_carries_no_insert(schema: dict[str, Any]) -> None:
    review = schema["$defs"]["KeyboardReviewStatus"]
    assert sorted(review["properties"]) == ["chars", "state"]


def test_air_unreliable_and_the_air_fields_are_optional_for_an_older_reader(reference_validator: Validate) -> None:
    """X34a: the schema accepts the old events and the new ones; an extra text field is not part of either."""
    new = {"v": 1, "type": "keyboard", "state": "closed", "reason": "air_unreliable", "discarded": 0}
    old_open = {
        "v": 1,
        "type": "keyboard",
        "state": "open",
        "phase": "typing",
        "press": "pinch",
        "lang": "en",
        "private": False,
    }
    with_level = {**old_open, "press": "air", "level": "degraded"}
    old_close = {"v": 1, "type": "keyboard", "state": "closed", "reason": "fists"}
    for event in (new, old_open, with_level, old_close):
        reference_validator(event, "Event")
    assert protocol.keyboard("closed", reason="air_unreliable", discarded=0) == new
    assert protocol.keyboard("open", phase="typing", press="air", level="degraded", lang="en", private=False) == {
        **with_level
    }
    assert not reference_ok(reference_validator, {**new, "text": "abc"}, "KeyboardEvent")
    assert not reference_ok(reference_validator, {**old_open, "keys": "abc"}, "KeyboardEvent")
