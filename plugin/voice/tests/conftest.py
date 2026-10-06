from __future__ import annotations

import json
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent))  # for the fish_fake helper module

from jarvis_voice import protocol

REPO_SCHEMA = Path(__file__).resolve().parents[2] / "protocol" / "schema.json"


@pytest.fixture(scope="session")
def reference_validator() -> Any:
    """jsonschema validator over the authoritative schema (the reference implementation)."""
    import jsonschema

    document = json.loads(REPO_SCHEMA.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(document)

    def validate(instance: Any, def_name: str) -> None:
        schema = {"$ref": f"#/$defs/{def_name}", "$defs": document["$defs"]}
        jsonschema.Draft202012Validator(schema).validate(instance)

    return validate


class RecordingSink:
    """EventSink that keeps every event (validated) and lets tests wait for them."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self._cond = threading.Condition()

    def emit(self, event: protocol.Event | dict[str, Any]) -> None:
        payload = event.to_wire() if isinstance(event, protocol.Event) else dict(event)
        protocol.validate_event(payload)  # every emitted event must match the contract
        with self._cond:
            self.events.append(payload)
            self._cond.notify_all()

    def types(self, *, skip_levels: bool = True) -> list[str]:
        with self._cond:
            return [e["type"] for e in self.events if not (skip_levels and e["type"] == "level")]

    def of_type(self, kind: str) -> list[dict[str, Any]]:
        with self._cond:
            return [e for e in self.events if e["type"] == kind]

    def states(self) -> list[str]:
        return [e["state"] for e in self.of_type("state")]

    def wait_for(
        self, predicate: Callable[[dict[str, Any]], bool], timeout: float = 5.0, *, after: int = 0
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                for event in self.events[after:]:
                    if predicate(event):
                        return event
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(f"timed out; events so far: {self.types()}")
                self._cond.wait(remaining)

    def wait_type(self, kind: str, timeout: float = 5.0, *, after: int = 0, **fields: Any) -> dict[str, Any]:
        return self.wait_for(
            lambda e: e["type"] == kind and all(e.get(k) == v for k, v in fields.items()), timeout, after=after
        )

    def mark(self) -> int:
        with self._cond:
            return len(self.events)


@pytest.fixture
def sink() -> RecordingSink:
    return RecordingSink()


def wait_until(predicate: Callable[[], bool], timeout: float = 5.0, interval: float = 0.01) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(interval)
