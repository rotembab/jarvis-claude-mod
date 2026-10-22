"""Fakes for home-control tests: a driver, a hub and a scripted wizard console."""

from __future__ import annotations

import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from jarvis_voice.home.base import Driver, DriverContext
from jarvis_voice.home.model import CommandSpec, DeviceRecord, HomeConfig, Outcome, Value
from jarvis_voice.home.service import HomeService
from jarvis_voice.home.store import HomeStore, PlainCodec


class FakeDriver(Driver):
    """Records every call; ``delay`` slows ``run``, ``outcome`` replaces its answer."""

    name = "fake"
    label = "Fake"

    def __init__(self, ctx: DriverContext) -> None:
        super().__init__(ctx)
        self.calls: list[tuple[str, str, Value]] = []
        self.delay = 0.0
        self.outcome: Outcome | None = None
        self.state = "on"
        self.closed = False
        self.started = threading.Event()

    def commands(self, device: DeviceRecord) -> list[CommandSpec]:
        specs = [
            CommandSpec("turn_on"),
            CommandSpec("turn_off"),
            CommandSpec("set_volume", "percent"),
            CommandSpec("launch_app", "text", hint="an app name"),
            CommandSpec("set_input", "choice", choices=("HDMI 1", "HDMI 2", "TV")),
            CommandSpec("set_temperature", "number", low=16, high=30),
        ]
        if device.kind == "lock":
            specs = [
                CommandSpec("lock"),
                CommandSpec("unlock", tier="screen"),
                CommandSpec("self_destruct", tier="never"),
            ]
        return specs

    def run(self, device: DeviceRecord, command: str, value: Value) -> Outcome:
        self.started.set()
        self.calls.append((device.id, command, value))
        if self.delay:
            time.sleep(self.delay)
        if self.outcome is not None:
            return self.outcome
        return Outcome.done(f"{device.name}: {command}" + ("" if value is None else f" {value}"))

    def status(self, device: DeviceRecord) -> Outcome:
        self.calls.append((device.id, "status", None))
        return Outcome.done(f"The {device.name} is {self.state}.")

    def close(self) -> None:
        self.closed = True


class FakeHub(FakeDriver):
    """A hub driver (like Home Assistant) that brings its own devices."""

    name = "fakehub"
    label = "Fake hub"

    def __init__(self, ctx: DriverContext) -> None:
        super().__init__(ctx)
        self.hub_error: Exception | None = None

    def hub_devices(self) -> list[DeviceRecord]:
        if self.hub_error is not None:
            raise self.hub_error
        return [
            DeviceRecord("fakehub:light.kitchen", "fakehub", "Kitchen light", "light", "Kitchen"),
            DeviceRecord("fakehub:light.bedroom", "fakehub", "Bedroom light", "light", "Bedroom"),
        ]

    def describe(self) -> str:
        return "Fake hub connected (2 devices)"


def make_store(tmp_path: Path) -> HomeStore:
    return HomeStore(tmp_path, codec=PlainCodec())


def make_service(
    tmp_path: Path,
    devices: list[DeviceRecord] | None = None,
    *,
    hubs: dict[str, dict[str, Any]] | None = None,
    call_timeout: float = 2.0,
) -> tuple[HomeService, dict[str, FakeDriver]]:
    """A service over a temporary data folder with the fake drivers; returns it and the drivers it built."""
    store = make_store(tmp_path)
    if devices or hubs:

        def seed(config: HomeConfig) -> None:
            for device in devices or []:
                config.upsert(device)
            config.hubs.update(hubs or {})

        store.update(seed)
    built: dict[str, FakeDriver] = {}

    def factory(cls: type[FakeDriver]) -> Any:
        def build(ctx: DriverContext) -> FakeDriver:
            built[cls.name] = cls(ctx)
            return built[cls.name]

        return build

    service = HomeService(
        tmp_path,
        store=store,
        drivers={"fake": factory(FakeDriver), "fakehub": factory(FakeHub)},
        call_timeout=call_timeout,
    )
    return service, built


class ScriptedPrompter:
    """The wizard's console, answering from a script. Running out of answers raises EOFError (the user quit)."""

    def __init__(self, answers: list[Any]) -> None:
        self.answers: deque[Any] = deque(answers)
        self.said: list[str] = []
        self.asked: list[str] = []
        self.qr: list[tuple[str, str]] = []

    def _next(self, question: str, kinds: tuple[type, ...]) -> Any:
        self.asked.append(question)
        if not self.answers:
            raise EOFError(f"no scripted answer for {question!r}")
        answer = self.answers.popleft()
        if answer is not None and not isinstance(answer, kinds):
            raise AssertionError(f"scripted answer {answer!r} does not fit {question!r}")
        return answer

    def say(self, text: str) -> None:
        self.said.append(text)

    def ask(self, question: str, default: str | None = None) -> str:
        answer = self._next(question, (str,))
        return answer if answer else (default or "")

    def ask_secret(self, question: str) -> str:
        return self._next(question, (str,)) or ""

    def choose(self, question: str, options: list[str]) -> int | None:
        answer = self._next(question, (int, str))
        if isinstance(answer, str):  # pick by label, so scripts survive menu changes
            matches = [i for i, option in enumerate(options) if answer.lower() in option.lower()]
            if len(matches) != 1:
                raise AssertionError(f"{answer!r} matches {len(matches)} of {options!r}")
            return matches[0]
        if answer is not None and not 0 <= answer < len(options):
            raise AssertionError(f"choice {answer} out of range for {options!r}")
        return answer

    def confirm(self, question: str, default: bool = True) -> bool:
        answer = self._next(question, (bool,))
        return default if answer is None else answer

    def show_qr(self, data: str, caption: str) -> None:
        self.qr.append((data, caption))

    @property
    def text(self) -> str:
        return "\n".join(self.said)
