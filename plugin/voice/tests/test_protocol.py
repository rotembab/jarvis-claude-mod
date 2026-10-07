from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from jarvis_voice import protocol
from jarvis_voice.protocol import Schema, SchemaError

from conftest import REPO_SCHEMA

VENDORED = Path(protocol.__file__).with_name("protocol_schema.json")


@pytest.mark.skipif(
    "site-packages" in Path(protocol.__file__).parts, reason="installed wheel: only the vendored copy exists"
)
def test_source_checkout_uses_authoritative_schema() -> None:
    assert protocol.schema_path() == REPO_SCHEMA.resolve()


def test_installed_package_falls_back_to_vendored_schema() -> None:
    if "site-packages" in Path(protocol.__file__).parts:
        assert protocol.schema_path() == VENDORED
    else:
        assert VENDORED.is_file()  # what the wheel will ship


def test_vendored_copy_matches_authoritative_schema() -> None:
    # The wheel ships protocol_schema.json; it must never drift from plugin/protocol/schema.json.
    assert json.loads(VENDORED.read_text("utf-8")) == json.loads(REPO_SCHEMA.read_text("utf-8"))


def all_events() -> list[protocol.Event]:
    return [
        protocol.Hello(port=4242, pid=1, platform="windows", version="0.1.0", capabilities=("speak", "ptt")),
        protocol.State(state="listening"),
        protocol.Level(mic=0.25, out=1.0),
        protocol.Utterance(id="utt-1", text="what time is it", source="ptt", duration_ms=1200, language="en"),
        protocol.Utterance(id="utt-2", text="hi", source="command", duration_ms=400),
        protocol.SpeechStarted(reply_id="r1"),
        protocol.SpeechDone(reply_id="r1", interrupted=True, spoken_text="Very good, sir."),
        protocol.BargeIn(spoken_text="", reply_id="r1"),
        protocol.BargeIn(spoken_text="partial"),
        protocol.Error(code="mic_blocked", message="blocked", hint="Settings > Privacy", fatal=False),
        protocol.Error(code="already_running", message="busy", fatal=True),
        protocol.Ready(stt_model="large-v3-turbo", stt_device="cuda", ptt_key="right ctrl", voice_id="abc"),
        protocol.Ready(stt_model="small.en", stt_device="cpu", ptt_key="f13"),
    ]


@pytest.mark.parametrize("event", all_events(), ids=lambda e: e.type)
def test_events_encode_to_schema_valid_wire_format(event: protocol.Event, reference_validator: Any) -> None:
    wire = event.to_wire()
    assert wire["v"] == 1 and wire["type"] == event.type
    assert list(wire)[:2] == ["v", "type"]
    protocol.validate_event(wire)
    reference_validator(wire, "Event")  # exactly one oneOf branch matches
    reference_validator(wire, protocol.EVENT_DEFS[event.type])


def test_camel_case_and_optional_fields() -> None:
    wire = protocol.Utterance(id="u", text="x", source="ptt", duration_ms=5).to_wire()
    assert wire == {"v": 1, "type": "utterance", "id": "u", "text": "x", "source": "ptt", "durationMs": 5}
    assert protocol.SpeechDone(reply_id="r", interrupted=False, spoken_text="").to_wire()["spokenText"] == ""


INVALID_EVENTS = [
    {"v": 2, "type": "state", "state": "sleeping"},
    {"v": 1, "type": "state", "state": "napping"},
    {"v": 1, "type": "state"},
    {"v": 1, "type": "state", "state": "sleeping", "extra": 1},
    {"v": 1, "type": "level", "mic": 1.5, "out": 0},
    {"v": 1, "type": "level", "mic": -0.1, "out": 0},
    {"v": 1, "type": "utterance", "id": "u", "text": "", "source": "ptt", "durationMs": 1},
    {"v": 1, "type": "utterance", "id": "u", "text": "x", "source": "ptt", "durationMs": 1.5},
    {"v": 1, "type": "utterance", "id": "u", "text": "x", "source": "keyboard", "durationMs": 1},
    {"v": 1, "type": "speech_done", "replyId": "r", "interrupted": 0, "spokenText": ""},
    {"v": 1, "type": "error", "code": "nope", "message": "m", "fatal": False},
    {"v": 1, "type": "hello", "port": 0, "pid": 1, "platform": "linux", "version": "x", "capabilities": []},
    {"v": 1, "type": "hello", "port": 1, "pid": 1, "platform": "linux", "version": "x", "capabilities": [1]},
    {"v": 1, "type": "ready", "sttModel": "m", "sttDevice": "tpu", "pttKey": "k"},
    {"v": True, "type": "state", "state": "sleeping"},
]


@pytest.mark.parametrize("event", INVALID_EVENTS)
def test_invalid_events_rejected_like_jsonschema(event: dict[str, Any], reference_validator: Any) -> None:
    import jsonschema

    with pytest.raises(protocol.ValidationError):
        protocol.validate_event(event)
    with pytest.raises(jsonschema.ValidationError):
        reference_validator(event, "Event")


COMMAND_CASES: list[tuple[str, Any]] = [
    ("heartbeat", {}),
    ("heartbeat", {"x": 1}),
    ("speak", {"replyId": "r", "seq": 0, "text": "Hello.", "final": False}),
    ("speak", {"replyId": "r", "seq": 3, "text": "", "final": True}),
    ("speak", {"replyId": "", "seq": 0, "text": "x", "final": False}),
    ("speak", {"replyId": "r", "seq": -1, "text": "x", "final": False}),
    ("speak", {"replyId": "r", "seq": 1.0, "text": "x", "final": False}),
    ("speak", {"replyId": "r", "seq": True, "text": "x", "final": False}),
    ("speak", {"replyId": "r", "seq": 0, "text": "x" * 4001, "final": False}),
    ("speak", {"replyId": "r", "seq": 0, "text": "x" * 4000, "final": False}),
    ("speak", {"replyId": "r", "seq": 0, "text": "x"}),
    ("speak", {"replyId": "r", "seq": 0, "text": "x", "final": "yes"}),
    ("stop", {}),
    ("stop", {"reason": "user"}),
    ("stop", {"reason": 5}),
    ("listen", {"action": "start"}),
    ("listen", {"action": "toggle"}),
    ("listen", {}),
    ("config", {"voiceId": "abc", "pttKey": "f13", "sttModel": "small.en", "language": "en"}),
    ("config", {"volume": 3}),
    ("status", {}),
    ("desktop", {"action": "open", "target": "Spotify"}),
    ("desktop", {"action": "volume", "level": 30}),
    ("desktop", {"action": "media", "key": "play_pause"}),
    ("desktop", {"action": "volume", "level": 101}),
    ("desktop", {"action": "type"}),
    ("desktop", {"action": "lock", "x": 1}),
    ("test_voice", {"text": "hi"}),
    ("test_voice", {"text": "x" * 1001}),
    ("shutdown", {}),
    ("shutdown", []),
    ("speak", "not an object"),
]


@pytest.mark.parametrize(("name", "body"), COMMAND_CASES)
def test_command_validation_agrees_with_jsonschema(name: str, body: Any, reference_validator: Any) -> None:
    import jsonschema

    schema = protocol.load_schema()
    try:
        reference_validator(body, schema.commands[name])
        expected = True
    except jsonschema.ValidationError:
        expected = False
    assert schema.is_valid(body, schema.commands[name]) is expected


def test_unknown_command_raises_key_error() -> None:
    with pytest.raises(KeyError):
        protocol.validate_command("format_disk", {})


def test_status_response_shape(reference_validator: Any) -> None:
    status = {"ok": True, "state": "sleeping", "version": "0.1.0", "platform": "windows", "fishKeySet": False}
    reference_validator(status, "StatusResponse")
    assert protocol.load_schema().is_valid(status, "StatusResponse")
    reference_validator(protocol.error_response("bad_request", "nope"), "CommandResponse")


def test_unsupported_keyword_is_refused() -> None:
    with pytest.raises(SchemaError, match="pattern"):
        Schema({"$defs": {"X": {"type": "string", "pattern": "^a"}}})
    with pytest.raises(SchemaError, match="dangling"):
        Schema({"$defs": {"X": {"$ref": "#/$defs/Missing"}}})
