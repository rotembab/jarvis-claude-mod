from __future__ import annotations

import itertools
import json
import random
from collections.abc import Callable
from typing import Any, get_args

import pytest

from jarvis_hands import protocol
from jarvis_hands.desktop.base import Display
from jarvis_hands.events import encode_line
from jarvis_hands.geometry import Rect

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
    *((name, {}) for name in sorted(protocol.COMMAND_NAMES - {"calibrate"})),
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
