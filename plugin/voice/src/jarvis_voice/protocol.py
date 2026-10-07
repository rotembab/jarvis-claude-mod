"""Helper <-> mod protocol: event dataclasses and schema validation.

``plugin/protocol/schema.json`` is the authoritative contract. In a source
checkout we read it directly; installed (non-editable) packages fall back to
the copy vendored next to this module (``protocol_schema.json``, kept identical
by a test).

Validation uses a small built-in validator for the JSON Schema subset the
contract uses, so the helper needs no ``jsonschema`` at runtime. It refuses to
load a schema that uses keywords it does not understand, so a future schema
change cannot be silently ignored. The test suite cross-checks it against the
reference ``jsonschema`` implementation.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, fields
from functools import lru_cache
from pathlib import Path
from typing import Any, ClassVar, Literal

PROTOCOL_VERSION = 1

HelperState = Literal["starting", "sleeping", "listening", "transcribing", "speaking", "error"]
PlatformName = Literal["windows", "macos", "linux"]
ErrorCode = Literal[
    "already_running",
    "fish_key_missing",
    "fish_auth_failed",
    "fish_unreachable",
    "local_voice_failed",
    "mic_blocked",
    "mic_in_use",
    "no_input_device",
    "no_output_device",
    "stt_model_missing",
    "stt_failed",
    "ptt_unavailable",
    "bad_request",
    "unauthorized",
    "internal",
]
UtteranceSource = Literal["ptt", "command", "wake"]

_VENDORED_NAME = "protocol_schema.json"


class SchemaError(Exception):
    """The schema file is missing or uses unsupported constructs."""


class ValidationError(ValueError):
    """An instance does not conform to the schema."""


# --------------------------------------------------------------------------- schema loading


def _candidate_schema_paths() -> list[Path]:
    here = Path(__file__).resolve().parent
    candidates: list[Path] = []
    # Source checkout: plugin/voice/src/jarvis_voice -> plugin/protocol/schema.json.
    project_dir = here.parents[1]
    if here.parent.name == "src" and (project_dir / "pyproject.toml").is_file():
        candidates.append((project_dir.parent / "protocol" / "schema.json").resolve())
    candidates.append(here / _VENDORED_NAME)
    return candidates


@lru_cache(maxsize=1)
def schema_path() -> Path:
    """Path of the schema in use (authoritative file when present, else vendored copy)."""
    for path in _candidate_schema_paths():
        if path.is_file():
            return path
    looked = ", ".join(map(str, _candidate_schema_paths()))
    raise SchemaError(f"protocol schema not found (looked in: {looked})")


@lru_cache(maxsize=1)
def load_schema() -> Schema:
    path = schema_path()
    with path.open("r", encoding="utf-8") as fh:
        return Schema(json.load(fh), source=path)


# --------------------------------------------------------------------------- validator

# Keywords the validator implements, plus annotation-only keywords it ignores.
_ASSERTION_KEYWORDS = {
    "type",
    "properties",
    "required",
    "additionalProperties",
    "enum",
    "const",
    "minimum",
    "maximum",
    "minLength",
    "maxLength",
    "items",
    "$ref",
    "oneOf",
}
_ANNOTATION_KEYWORDS = {"$schema", "$id", "title", "description", "$comment", "default", "examples"}
_TYPE_NAMES = {"object", "array", "string", "integer", "number", "boolean", "null"}


def _type_ok(value: Any, type_name: str) -> bool:
    if type_name == "object":
        return isinstance(value, dict)
    if type_name == "array":
        return isinstance(value, list)
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "null":
        return value is None
    if isinstance(value, bool):  # bool is an int subclass in Python, not in JSON
        return False
    if type_name == "integer":
        return isinstance(value, int) or (isinstance(value, float) and value.is_integer())
    if type_name == "number":
        return isinstance(value, (int, float))
    return False


def _json_equal(a: Any, b: Any) -> bool:
    # JSON equality: 1 == 1.0 but True != 1.
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    return a == b


class Schema:
    """A loaded protocol schema with a validator for the subset it uses."""

    def __init__(self, document: dict[str, Any], source: Path | None = None) -> None:
        self.document = document
        self.source = source
        self.defs: dict[str, Any] = document.get("$defs", {})
        self.commands: dict[str, str] = {
            name: ref.rsplit("/", 1)[-1] for name, ref in document.get("x-commands", {}).items()
        }
        for name, sub in self.defs.items():
            self._check_supported(sub, f"#/$defs/{name}")

    # -- structure checks

    def _check_supported(self, node: Any, where: str) -> None:
        if not isinstance(node, dict):
            raise SchemaError(f"{where}: schema node must be an object")
        for key, value in node.items():
            if key in _ANNOTATION_KEYWORDS:
                continue
            if key not in _ASSERTION_KEYWORDS:
                raise SchemaError(f"{where}: unsupported schema keyword {key!r}")
            if key == "properties":
                for prop, sub in value.items():
                    self._check_supported(sub, f"{where}/properties/{prop}")
            elif key == "items":
                self._check_supported(value, f"{where}/items")
            elif key == "oneOf":
                for i, sub in enumerate(value):
                    self._check_supported(sub, f"{where}/oneOf/{i}")
            elif key == "additionalProperties" and not isinstance(value, bool):
                raise SchemaError(f"{where}: only boolean additionalProperties is supported")
            elif key == "$ref":
                self._resolve(value)
            elif key == "type":
                names = value if isinstance(value, list) else [value]
                if not all(n in _TYPE_NAMES for n in names):
                    raise SchemaError(f"{where}: unknown type {value!r}")

    def _resolve(self, ref: str) -> Any:
        match = re.fullmatch(r"#/\$defs/([A-Za-z0-9_]+)", ref)
        if not match or match.group(1) not in self.defs:
            raise SchemaError(f"unsupported or dangling $ref {ref!r}")
        return self.defs[match.group(1)]

    # -- validation

    def validate(self, instance: Any, def_name: str) -> None:
        """Raise ValidationError unless ``instance`` matches ``#/$defs/<def_name>``."""
        if def_name not in self.defs:
            raise SchemaError(f"unknown schema definition {def_name!r}")
        errors = self._errors(instance, self.defs[def_name], "$")
        if errors:
            raise ValidationError(errors[0])

    def is_valid(self, instance: Any, def_name: str) -> bool:
        return not self._errors(instance, self.defs[def_name], "$")

    def _errors(self, value: Any, node: dict[str, Any], path: str) -> list[str]:
        if "$ref" in node:
            errs = self._errors(value, self._resolve(node["$ref"]), path)
            if errs:
                return errs
        if "type" in node:
            names = node["type"] if isinstance(node["type"], list) else [node["type"]]
            if not any(_type_ok(value, n) for n in names):
                return [f"{path}: expected {'/'.join(names)}"]
        if "const" in node and not _json_equal(value, node["const"]):
            return [f"{path}: must be {node['const']!r}"]
        if "enum" in node and not any(_json_equal(value, opt) for opt in node["enum"]):
            return [f"{path}: must be one of {node['enum']!r}"]
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in node and value < node["minimum"]:
                return [f"{path}: must be >= {node['minimum']}"]
            if "maximum" in node and value > node["maximum"]:
                return [f"{path}: must be <= {node['maximum']}"]
        if isinstance(value, str):
            # JSON Schema counts code points, which is what len() does.
            if "minLength" in node and len(value) < node["minLength"]:
                return [f"{path}: shorter than {node['minLength']}"]
            if "maxLength" in node and len(value) > node["maxLength"]:
                return [f"{path}: longer than {node['maxLength']}"]
        if isinstance(value, dict):
            props: dict[str, Any] = node.get("properties", {})
            for req in node.get("required", []):
                if req not in value:
                    return [f"{path}: missing required property {req!r}"]
            if node.get("additionalProperties") is False:
                extra = sorted(set(value) - set(props))
                if extra:
                    return [f"{path}: unexpected property {extra[0]!r}"]
            for key, sub in props.items():
                if key in value:
                    errs = self._errors(value[key], sub, f"{path}.{key}")
                    if errs:
                        return errs
        if isinstance(value, list) and "items" in node:
            for i, item in enumerate(value):
                errs = self._errors(item, node["items"], f"{path}[{i}]")
                if errs:
                    return errs
        if "oneOf" in node:
            matches = [sub for sub in node["oneOf"] if not self._errors(value, sub, path)]
            if len(matches) != 1:
                return [f"{path}: matches {len(matches)} of the oneOf alternatives (expected exactly 1)"]
        return []


# --------------------------------------------------------------------------- events


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.title() for part in rest)


@dataclass(frozen=True, slots=True)
class Event:
    """Base class: subclasses set ``type`` and declare snake_case fields.

    ``to_wire()`` produces the JSON object: ``v`` and ``type`` first, field
    names in camelCase, optional fields left out when ``None``.
    """

    type: ClassVar[str] = ""

    def to_wire(self) -> dict[str, Any]:
        out: dict[str, Any] = {"v": PROTOCOL_VERSION, "type": self.type}
        for f in fields(self):
            value = getattr(self, f.name)
            if value is None:
                continue
            out[_camel(f.name)] = list(value) if isinstance(value, tuple) else value
        return out


@dataclass(frozen=True, slots=True)
class Hello(Event):
    type: ClassVar[str] = "hello"
    port: int
    pid: int
    platform: PlatformName
    version: str
    capabilities: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class State(Event):
    type: ClassVar[str] = "state"
    state: HelperState


@dataclass(frozen=True, slots=True)
class Level(Event):
    type: ClassVar[str] = "level"
    mic: float
    out: float


@dataclass(frozen=True, slots=True)
class Utterance(Event):
    type: ClassVar[str] = "utterance"
    id: str
    text: str
    source: UtteranceSource
    duration_ms: int
    language: str | None = None


@dataclass(frozen=True, slots=True)
class SpeechStarted(Event):
    type: ClassVar[str] = "speech_started"
    reply_id: str


@dataclass(frozen=True, slots=True)
class SpeechDone(Event):
    type: ClassVar[str] = "speech_done"
    reply_id: str
    interrupted: bool
    spoken_text: str


@dataclass(frozen=True, slots=True)
class BargeIn(Event):
    type: ClassVar[str] = "barge_in"
    spoken_text: str
    reply_id: str | None = None


@dataclass(frozen=True, slots=True)
class Error(Event):
    type: ClassVar[str] = "error"
    code: ErrorCode
    message: str
    hint: str | None = None
    fatal: bool = False


@dataclass(frozen=True, slots=True)
class Ready(Event):
    type: ClassVar[str] = "ready"
    stt_model: str
    stt_device: Literal["cuda", "cpu"]
    ptt_key: str
    voice_id: str | None = None


EVENT_DEFS: dict[str, str] = {
    "hello": "HelloEvent",
    "state": "StateEvent",
    "level": "LevelEvent",
    "utterance": "UtteranceEvent",
    "speech_started": "SpeechStartedEvent",
    "speech_done": "SpeechDoneEvent",
    "barge_in": "BargeInEvent",
    "error": "ErrorEvent",
    "ready": "ReadyEvent",
}


def validate_event(event: dict[str, Any]) -> None:
    """Validate an encoded event (dispatching on ``type`` for clearer errors)."""
    def_name = EVENT_DEFS.get(event.get("type", "")) if isinstance(event, dict) else None
    if def_name is None:
        raise ValidationError(f"unknown event type {event.get('type') if isinstance(event, dict) else event!r}")
    load_schema().validate(event, def_name)


def command_names() -> list[str]:
    return sorted(load_schema().commands)


def validate_command(name: str, body: Any) -> None:
    """Validate a command body. Raises KeyError for unknown commands."""
    schema = load_schema()
    if name not in schema.commands:
        raise KeyError(name)
    schema.validate(body, schema.commands[name])


def error_response(code: ErrorCode, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}
