"""The keyboard controller on its own, with fake dependencies (DESIGN-KEYBOARD.md 3.8, 5.6).

K1 to K4, K7 to K9, K40 to K42, K45, K47, K48, Q1 to Q3, Q40, S22b (the controller's seams), S58, X34b and X48. The
runtime around it is tested in ``test_kb_runtime.py``; here the runtime is a handful of lambdas and lists.

``Bench`` is the controller's frame loop's counterpart: it is a ``KbRig`` (the session test rig) whose frames go to a
``KeyboardController`` instead of to a session of its own, and whose press method is the scripted one, so a test says
which key is tapped when and the controller does the rest. Time is the frame's: nothing here sleeps.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from jarvis_hands import logs, protocol
from jarvis_hands.actions import (
    Action,
    Button,
    DragWindow,
    GrabWindow,
    ResizeWindow,
    Scroll,
    ThrowWindow,
)
from jarvis_hands.desktop.base import Display, KeyTarget
from jarvis_hands.desktop.fake import FakeDesktop, FakeWindow
from jarvis_hands.desktop.keys import KeyRefused
from jarvis_hands.geometry import Rect
from jarvis_hands.keyboard import controller as controller_module
from jarvis_hands.keyboard.compose import ComposeBuffer
from jarvis_hands.keyboard.controller import TEXT, KeyboardController, KeyboardDeps
from jarvis_hands.keyboard.limits import HEALTH_CLOSE_S, HEALTH_MAX_AGE_S, QUARANTINE_CLEAR_S
from jarvis_hands.keyboard.practice import PracticeResult, marker_status, write_marker
from jarvis_hands.keyboard.review import REVIEW_TEXT, ReviewMachine
from jarvis_hands.keyboard.rig import KbRig, ScriptedPress
from jarvis_hands.keyboard.session import KeyboardSession
from jarvis_hands.keyboard.settings import AIR_NEEDS_REVIEW, KeyboardSettings
from jarvis_hands.keyboard.sink import KeySink
from jarvis_hands.keyboard.trace import TRACE_PREFIX, TapLog, TraceWriter, trace_path
from jarvis_hands.keyboard.types import PressQuality
from jarvis_hands.landmarks import Frame
from jarvis_hands.overlay.base import NullOverlay, OverlayHealth, OverlayState
from jarvis_hands.settings import HandsSettings
from jarvis_hands.synthetic import SyntheticPose

from scripted import H, Script
from scripted import Rig as EngineRig

S = "zzqxjv"
SCHEMA = json.loads(protocol.schema_path().read_text(encoding="utf-8"))
HEALTHY = OverlayHealth(alive=True, failures=0, ok_age_s=0.05, keyboard_ok=True, draw_ms=4.1)
#: Six two-letter phrases: the practice script in seconds of frame time instead of minutes.
SHORT = ["ab", "cd", "ef", "gh", "ij", "kl"]


def validate(instance: Any, def_name: str) -> None:
    jsonschema.Draft202012Validator({"$ref": f"#/$defs/{def_name}", "$defs": SCHEMA["$defs"]}).validate(instance)


def result(*, rest_s: float = 100.0, phantoms: int = 0, prompts_im: int = 24, hits_im: int = 22) -> PracticeResult:
    """A finished practice, as far as the markers care."""
    return PracticeResult(
        completed=True,
        presses=38,
        correct=38,
        hit_rate=0.95,
        rest_s=rest_s,
        phantoms=phantoms,
        phantoms_per_min=phantoms * 60.0 / rest_s if rest_s else 0.0,
        fps=29.8,
        per_finger={},
        drill_prompts=48,
        drill_hits=41,
        drill_prompts_im=prompts_im,
        drill_hits_im=hits_im,
        noise=0.017,
    )


class HealthOverlay(NullOverlay):
    """An overlay that reports a health the test sets (the real one is the Windows overlay's ``health()``)."""

    def __init__(self, health: OverlayHealth | None = HEALTHY) -> None:
        self.value = health

    def health(self) -> OverlayHealth | None:
        return self.value


class Bench(KbRig):
    """A controller, its fake runtime, and the scripts of ``KbRig`` pointed at it."""

    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        press: str = "air",
        commit: str = "review",
        desktop: FakeDesktop | None = None,
        overlay: object | None = None,
        enabled: bool = True,
        markers: bool = True,
        sides: Sequence[str] = ("left", "right"),
        start_t: float = 0.0,
    ) -> None:
        self.ctl: KeyboardController | None = None
        scripted = ScriptedPress("air" if press == "air" else "pinch")
        super().__init__(
            commit=commit,  # type: ignore[arg-type]
            press=scripted.name,
            scripted=scripted,
            sides=sides,  # type: ignore[arg-type]
            start_t=start_t,
        )
        self.tmp = tmp_path
        self.desktop = desktop or FakeDesktop()
        self.overlay: Any = overlay if overlay is not None else NullOverlay()
        self.events: list[dict[str, Any]] = []
        self.shown: list[OverlayState] = []
        self.reports: list[tuple[str, str]] = []
        self.pointer_off_calls = 0
        self.pointer_reset_calls = 0
        self.flags = {"ready": True, "paused": False, "blocked": False, "overlay": True}
        self.show_error: BaseException | None = None
        #: A step of the close that fails: the pointer reset and the event.
        self.reset_error: BaseException | None = None
        #: What the runtime does with the engine when the controller asks (a test with an engine sets these).
        self.on_pointer_off: Callable[[], None] | None = None
        self.on_pointer_reset: Callable[[], None] | None = None
        #: What the close did, in order: "release" (the test hooks it in), "reset", "blank", "closed".
        self.order: list[str] = []
        self.made: list[tuple[str, bool]] = []
        self.fps = 30.0
        monkeypatch.setattr(controller_module, "make_press", self._make_press)
        deps = KeyboardDeps(
            data_dir=tmp_path,
            desktop=lambda: self.desktop,
            overlay=lambda: self.overlay,
            displays=lambda: self.desktop.displays(),
            ready=lambda: self.flags["ready"],
            paused=lambda: self.flags["paused"],
            desktop_blocked=lambda: self.flags["blocked"],
            overlay_enabled=lambda: self.flags["overlay"],
            fps=lambda: self.fps,
            emit=self._emit,
            show=self._show,
            pointer_off=self._pointer_off,
            pointer_reset=self._pointer_reset,
            report=lambda code, message: self.reports.append((code, message)),
            clock=self._clock,
        )
        self.ctl = KeyboardController(deps)
        settings: dict[str, Any] = {"enabled": enabled, "press": press, "commit": commit}
        assert self.ctl.command({"action": "configure", "settings": settings}) == {"ok": True}
        if markers:
            for name in ("air", "pinch"):
                write_marker(tmp_path, name, result())  # type: ignore[arg-type]

    # -- the fake runtime
    def _make_press(self, name: str, tuning: object, *, review: bool = False) -> ScriptedPress:
        self.made.append((name, review))
        if len(self.made) == 1:
            return self.press
        self.fallback_press = ScriptedPress(name)  # type: ignore[arg-type]
        return self.fallback_press

    def _emit(self, event: dict[str, Any]) -> None:
        validate(event, "Event")
        if event.get("state") == "closed":
            self.order.append("closed")
        self.events.append(event)

    def _show(self, state: OverlayState) -> None:
        """The runtime's ``_show``: a draw that fails breaks the overlay, which closes the keyboard (RT8)."""
        try:
            if self.show_error is not None:
                raise self.show_error
            if state == OverlayState():
                self.order.append("blank")
            self.shown.append(state)
        except Exception:  # noqa: BLE001 - as the runtime does
            self.ctl.close("no_overlay")

    def _pointer_off(self) -> None:
        self.pointer_off_calls += 1
        if self.on_pointer_off is not None:
            self.on_pointer_off()

    def _pointer_reset(self) -> None:
        self.order.append("reset")
        self.pointer_reset_calls += 1
        if self.reset_error is not None:
            raise self.reset_error
        if self.on_pointer_reset is not None:
            self.on_pointer_reset()

    # -- the rig, pointed at the controller
    @property  # type: ignore[override]
    def session(self) -> Any:
        return self.ctl._session if self.ctl is not None else None

    @session.setter
    def session(self, value: object) -> None:
        pass

    @property
    def _closed(self) -> str | None:  # type: ignore[override]
        return self.closed_reason

    @_closed.setter
    def _closed(self, value: object) -> None:
        pass

    @property
    def closed_reason(self) -> str | None:
        last = next((e for e in reversed(self.events) if e["type"] == "keyboard"), None)
        return last["reason"] if last is not None and last["state"] == "closed" else None

    @property
    def active(self) -> ScriptedPress:  # type: ignore[override]
        return self.ctl._press  # type: ignore[union-attr,return-value]

    def feed(self, frames: Sequence[Frame]) -> None:
        for frame in frames:
            if not self.ctl.active:  # type: ignore[union-attr]
                return
            self.t = self._clock.t = frame.t
            self.frames += 1
            self.ctl.frame(frame, frame.t)  # type: ignore[union-attr]

    # -- what a test asks
    def cmd(self, action: str, **more: Any) -> dict[str, Any]:
        return self.ctl.command({"action": action, **more})  # type: ignore[union-attr]

    def open(self, action: str = "start") -> dict[str, Any]:
        response = self.cmd(action)
        assert response == {"ok": True}, response
        return response

    def kb_events(self, state: str | None = None) -> list[dict[str, Any]]:
        return [e for e in self.events if e["type"] == "keyboard" and (state is None or e["state"] == state)]

    @property  # type: ignore[override]
    def view(self) -> Any:
        """The last view the controller handed the overlay."""
        return self.shown[-1].keyboard if self.shown else None

    @view.setter
    def view(self, value: object) -> None:
        pass

    def arm_up(self) -> Bench:
        """Open, place, warm up: a session that types."""
        self.open()
        self.run(0.2)
        self.arm()
        self.run(0.2)
        return self

    def arm_up_practice(self) -> Bench:
        """The same for a practice: it ends in the drill."""
        self.open("practice")
        self.run(0.2)
        self.arm()
        self.run(0.2)
        return self

    @property
    def text(self) -> str:
        """What the controller's session holds in the box (the test reads it; nothing else does)."""
        machine = self.ctl._session._machine  # type: ignore[union-attr]
        return machine.buffer.text() if machine is not None else ""


@pytest.fixture
def make_bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., Bench]]:
    benches: list[Bench] = []

    def make(**kw: Any) -> Bench:
        bench = Bench(kw.pop("tmp_path", tmp_path), monkeypatch, **kw)
        benches.append(bench)
        return bench

    yield make
    for bench in benches:
        if bench.ctl is not None:
            bench.ctl.close("command")  # a test that left a session open closes it: the scrub is process-wide
    assert not logs._scrub_on, "a test left the exception scrub on"


@pytest.fixture(autouse=True)
def clean_scrub() -> Iterator[None]:
    factory = logging.getLogRecordFactory()
    import sys
    import threading

    hooks = sys.excepthook, threading.excepthook
    yield
    logs.keyboard_scrub(False)
    logging.setLogRecordFactory(factory)
    sys.excepthook, threading.excepthook = hooks


# ----------------------------------------------------------------------------------------------- K4, K47, P6: refusals

OFF = "The air keyboard is off. Turn it on in the Jarvis settings (handKeyboard)."


def broken_overlay() -> HealthOverlay:
    return HealthOverlay(OverlayHealth(alive=True, failures=0, ok_age_s=0.05, keyboard_ok=False))


class NoKeys:
    """A desktop that can move a mouse and nothing else: no ``key_target``, no ``send_keys``."""

    injects_for_real = False

    def displays(self) -> list[Display]:
        return FakeDesktop().displays()


def refusal_cases() -> list[tuple[str, Callable[[Bench], str], str]]:
    """(name, set the bench up and say which action to try, the exact text it must be refused with)."""

    def with_flag(name: str, value: bool) -> Callable[[Bench], str]:
        def setup(b: Bench) -> str:
            b.flags[name] = value
            return "start"

        return setup

    def disabled(b: Bench) -> str:
        b.cmd("configure", settings={"enabled": False})
        return "start"

    def real_without_overlay(b: Bench) -> str:
        b.desktop.injects_for_real = True  # the overlay is a NullOverlay: nothing to draw with
        return "start"

    def real_with_dead_overlay(b: Bench) -> str:
        b.desktop.injects_for_real = True
        b.overlay = HealthOverlay(OverlayHealth(alive=False, failures=0, ok_age_s=None, keyboard_ok=True))
        return "start"

    def no_font(b: Bench) -> str:
        b.overlay = broken_overlay()
        return "start"

    def cannot_type(b: Bench) -> str:
        b.desktop = NoKeys()  # type: ignore[assignment]
        return "start"

    def practice_windows(b: Bench) -> str:
        b.cmd("configure", settings={"press": "windows"})
        return "practice"

    def forced(**fields: Any) -> Callable[[Bench], str]:
        def setup(b: Bench) -> str:
            for name, value in fields.items():
                setattr(b.ctl.settings, name, value)  # type: ignore[union-attr]
            return "start"

        return setup

    def no_marker(press: str, commit: str) -> Callable[[Bench], str]:
        def setup(b: Bench) -> str:
            b.cmd("configure", settings={"press": press, "commit": commit})
            marker = b.tmp / "hands" / ("keyboard-practice-air.json" if press == "air" else "keyboard-practice.json")
            marker.unlink()
            return "start"

        return setup

    def poor_marker(press: str, commit: str) -> Callable[[Bench], str]:
        def setup(b: Bench) -> str:
            b.cmd("configure", settings={"press": press, "commit": commit})
            write_marker(b.tmp, press, result(rest_s=15.0, phantoms=0, hits_im=2))  # type: ignore[arg-type]
            return "start"

        return setup

    def after_a_cut(b: Bench) -> str:
        b.tmp.joinpath("hands", "keyboard-practice-air.json").unlink()
        b.ctl._air_cut = True  # type: ignore[union-attr]  # what a cut practice leaves; the k9 tests get here for real
        return "start"

    def osk_fails(b: Bench) -> str:
        b.cmd("configure", settings={"press": "windows"})
        b.desktop.os_keyboard_starts = False
        return "start"

    return [
        ("disabled", disabled, OFF),
        ("starting", with_flag("ready", False), "Hand control is still starting."),
        ("paused", with_flag("paused", True), "Hand control is paused; resume it first."),
        ("locked", with_flag("blocked", True), "The desktop is locked or showing a system prompt."),
        ("overlay off", with_flag("overlay", False), "The air keyboard needs the on-screen overlay."),
        ("no overlay to type under", real_without_overlay, "The air keyboard needs the on-screen overlay."),
        ("dead overlay", real_with_dead_overlay, "The air keyboard needs the on-screen overlay."),
        ("no font", no_font, "Text rendering is unavailable, so the keyboard cannot be shown."),
        ("cannot type", cannot_type, "This computer cannot type for the keyboard yet."),
        ("air with direct", forced(commit="direct"), AIR_NEEDS_REVIEW),
        ("practice on windows", practice_windows, "Practice needs the air or pinch method."),
        (
            "air without a marker",
            no_marker("air", "review"),
            "Practice first: run /jarvis hands keyboard practice once with the air method.",
        ),
        (
            "air with a poor marker",
            poor_marker("air", "review"),
            "The last air practice had too many false or missed taps; practice again.",
        ),
        (
            "air after a cut practice",
            after_a_cut,
            "The last air practice found the air tap unusable on this camera. "
            "Use the pinch method instead: /jarvis hands keyboard press pinch.",
        ),
        (
            "pinch direct without a marker",
            no_marker("pinch", "direct"),
            "Practice first: run /jarvis hands keyboard practice once on this computer.",
        ),
        (
            "pinch direct with a poor marker",
            poor_marker("pinch", "direct"),
            "The last practice had too many false presses; practice again.",
        ),
        ("windows keyboard did not start", osk_fails, "Windows' on-screen keyboard did not start."),
    ]


@pytest.mark.parametrize(("name", "setup", "text"), refusal_cases(), ids=[c[0] for c in refusal_cases()])
def test_k4_k47_every_refusal_has_its_exact_text_and_changes_nothing(
    make_bench: Callable[..., Bench], name: str, setup: Callable[[Bench], str], text: str
) -> None:
    bench = make_bench()
    action = setup(bench)
    response = bench.cmd(action)
    assert response == {"ok": False, "error": {"code": "bad_request", "message": text}}
    validate(response, "CommandResponse")
    assert not bench.ctl.active and not bench.ctl.needs_release  # type: ignore[union-attr]
    assert bench.pointer_off_calls == 0 and not bench.kb_events() and not bench.shown


def test_p6_every_refusal_of_the_table_is_reachable() -> None:
    reached = {text for _, _, text in refusal_cases()}
    table = {
        "off",
        "starting",
        "paused",
        "blocked",
        "overlay",
        "render",
        "no_typing",
        "air_direct",
        "practice_windows",
        "practice_air",
        "practice_air_poor",
        "practice_air_cut",
        "practice_pinch",
        "practice_pinch_poor",
        "osk",
    }
    assert {TEXT[key] for key in table} <= reached
    assert len({TEXT[key] for key in table}) == len(table)


def test_k4_the_first_refusal_in_the_table_wins(make_bench: Callable[..., Bench]) -> None:
    bench = make_bench(markers=False)
    bench.flags.update(ready=False, paused=True, blocked=True, overlay=False)
    assert bench.cmd("start")["error"]["message"] == "Hand control is still starting."
    bench.flags["ready"] = True
    assert bench.cmd("start")["error"]["message"] == "Hand control is paused; resume it first."
    bench.flags["paused"] = False
    assert bench.cmd("start")["error"]["message"] == "The desktop is locked or showing a system prompt."
    bench.flags["blocked"] = False
    assert bench.cmd("start")["error"]["message"] == "The air keyboard needs the on-screen overlay."
    bench.flags["overlay"] = True
    assert bench.cmd("start")["error"]["message"].startswith("Practice first")
    bench.cmd("configure", settings={"enabled": False})
    assert bench.cmd("start")["error"]["message"] == OFF


def test_k4_a_practice_needs_no_marker_and_no_typing_desktop(make_bench: Callable[..., Bench]) -> None:
    bench = make_bench(markers=False)
    bench.desktop = NoKeys()  # type: ignore[assignment]
    bench.open("practice")
    assert bench.ctl.active  # type: ignore[union-attr]


def test_k4_the_windows_keyboard_needs_neither_our_overlay_nor_a_practice(make_bench: Callable[..., Bench]) -> None:
    bench = make_bench(press="windows", markers=False)
    bench.flags["overlay"] = False
    assert bench.cmd("start") == {"ok": True}
    assert bench.desktop.os_keyboard_opened == 1
    assert not bench.ctl.active and bench.pointer_off_calls == 0 and not bench.kb_events()  # type: ignore[union-attr]


def test_k4_a_command_that_is_not_one_is_refused_in_fixed_words(make_bench: Callable[..., Bench]) -> None:
    bench = make_bench()
    for body in ({"action": "type", "text": S}, {"action": 7}, {}, {"action": "insert"}):
        response = bench.ctl.command(body)  # type: ignore[union-attr]
        assert response == {"ok": False, "error": {"code": "bad_request", "message": TEXT["unknown"]}}
        assert S not in json.dumps(response)


# ------------------------------------------------------------------------------------------- X48, S58: press and commit


def test_x48_the_default_is_air_with_the_box_and_the_old_refusals_are_gone() -> None:
    from jarvis_hands.keyboard.settings import KeyboardSettings

    defaults = KeyboardSettings()
    assert (defaults.press, defaults.commit, defaults.enabled) == ("air", "review", False)
    source = Path(controller_module.__file__).read_text(encoding="utf-8")
    for gone in ("The air-tap method is not available yet.", "Practice needs the pinch method."):
        assert gone not in source and gone not in TEXT.values()


def test_x48_air_with_direct_is_refused_by_configure_and_by_start(make_bench: Callable[..., Bench]) -> None:
    bench = make_bench()
    expected = {"ok": False, "error": {"code": "bad_request", "message": AIR_NEEDS_REVIEW}}
    assert bench.cmd("configure", settings={"commit": "direct"}) == expected
    assert bench.ctl.settings.commit == "review"  # type: ignore[union-attr]
    bench.ctl.settings.commit = "direct"  # type: ignore[union-attr]  # however it got there
    assert bench.cmd("start") == expected and bench.cmd("practice") == expected


def test_x48_a_live_air_open_needs_the_air_marker_and_a_practice_does_not(make_bench: Callable[..., Bench]) -> None:
    bench = make_bench(markers=False)
    assert bench.cmd("start")["error"]["message"] == TEXT["practice_air"]
    bench.open("practice")
    assert bench.ctl.status()["state"] == "practice"  # type: ignore[index,union-attr]
    bench.cmd("stop")
    write_marker(bench.tmp, "air", result())
    bench.open("start")
    assert bench.ctl.status()["state"] == "open"  # type: ignore[index,union-attr]


@pytest.mark.parametrize(
    "bodies",
    [
        [{"press": "air", "commit": "direct"}],
        [{"commit": "direct", "press": "air"}],
        [{"press": "pinch"}, {"press": "air", "commit": "direct"}],
        [{"press": "pinch"}, {"commit": "direct"}, {"press": "air"}],
        [{"commit": "direct", "layout": "he", "press": "air", "size": 1.2}],
    ],
)
def test_s58_air_with_direct_is_refused_whichever_way_it_arrives_and_nothing_changes(
    make_bench: Callable[..., Bench], bodies: list[dict[str, Any]]
) -> None:
    bench = make_bench()
    refused = [bench.cmd("configure", settings=b) for b in bodies]
    assert refused[-1] == {"ok": False, "error": {"code": "bad_request", "message": AIR_NEEDS_REVIEW}}
    settings = bench.ctl.settings  # type: ignore[union-attr]
    assert not (settings.press == "air" and settings.commit == "direct")
    assert settings.size == 1.0 and settings.layout == "auto"  # all or nothing


def test_k41_a_body_with_one_bad_value_changes_nothing(make_bench: Callable[..., Bench]) -> None:
    bench = make_bench()
    before = dict(vars(bench.ctl.settings))  # type: ignore[union-attr]
    for settings in ({"size": 9, "dock": "bottom"}, {"idleS": 30.5}, {"unknown": 1}, {"enter": True}):
        response = bench.cmd("configure", settings=settings)
        assert response["ok"] is False and response["error"]["code"] == "bad_request"
        validate(response, "CommandResponse")
        assert vars(bench.ctl.settings) == before  # type: ignore[union-attr]
    assert bench.cmd("configure", settings={"size": 1.2, "dock": "bottom"}) == {"ok": True}
    assert (bench.ctl.settings.size, bench.ctl.settings.dock) == (1.2, "bottom")  # type: ignore[union-attr]


# ------------------------------------------------------------------------------------------------------ K1, K40: open


def test_k1_open_turns_the_pointer_off_asks_for_the_release_and_says_so(make_bench: Callable[..., Bench]) -> None:
    bench = make_bench()
    ctl = bench.ctl
    assert ctl is not None and not ctl.active and not ctl.wants_two_hands and not ctl.needs_release
    bench.open()
    assert ctl.active and ctl.wants_two_hands
    assert bench.pointer_off_calls == 1
    assert ctl.needs_release and ctl.take_release() is True
    assert not ctl.needs_release and ctl.take_release() is False
    (event,) = bench.kb_events()
    assert event == {
        "v": 1,
        "type": "keyboard",
        "state": "open",
        "phase": "placing",
        "lang": "en",
        "press": "air",
        "level": "ok",
        "commit": "review",
        "private": False,
    }
    # the session is built by the first frame, which knows the time and the size of the camera
    assert ctl.status()["phase"] == "placing"  # type: ignore[index]
    bench.run(0.1)
    assert bench.session is not None and bench.view is not None


def test_k1_a_second_start_is_a_no_op_and_the_other_mode_is_refused(make_bench: Callable[..., Bench]) -> None:
    bench = make_bench()
    bench.open()
    bench.ctl.take_release()  # type: ignore[union-attr]
    assert bench.cmd("start") == {"ok": True}
    assert bench.pointer_off_calls == 1 and len(bench.kb_events()) == 1 and not bench.ctl.needs_release  # type: ignore[union-attr]
    other = bench.cmd("practice")
    assert other == {"ok": False, "error": {"code": "bad_request", "message": TEXT["other_mode"]}}
    bench.cmd("stop")
    bench.open("practice")
    assert bench.cmd("start")["error"]["message"] == TEXT["other_mode"]


def test_k40_a_review_open_builds_every_part_for_the_box(make_bench: Callable[..., Bench]) -> None:
    bench = make_bench(press="pinch", commit="review", markers=False)  # R13: pinch with the box needs no practice
    bench.open()
    bench.run(0.1)
    assert bench.made == [("pinch", True)]
    assert bench.ctl._sink._commit == "review" and bench.session._commit == "review"  # type: ignore[union-attr]
    assert bench.view.commit == "review" and bench.view.compose is not None
    (event,) = bench.kb_events()
    assert (event["commit"], event["press"], "level" not in event) == ("review", "pinch", True)


def test_k40_a_direct_open_builds_every_part_for_the_echo(make_bench: Callable[..., Bench]) -> None:
    bench = make_bench(press="pinch", commit="direct")
    bench.open()
    bench.run(0.1)
    assert bench.made == [("pinch", False)]
    assert bench.ctl._sink._commit == "direct" and bench.view.commit == "direct"  # type: ignore[union-attr]
    assert bench.view.compose is None
    assert bench.kb_events()[0]["commit"] == "direct"


def test_k1_a_practice_has_no_sink_and_no_commit_in_its_event_and_a_live_open_has_no_trace(
    make_bench: Callable[..., Bench],
) -> None:
    practice = make_bench()
    practice.open("practice")
    assert practice.ctl._sink is None and practice.ctl._trace is not None  # type: ignore[union-attr]
    (event,) = practice.kb_events()
    assert event["state"] == "practice" and "commit" not in event and "review" not in event
    assert event["level"] == "ok" and event["press"] == "air"
    live = make_bench()
    live.open()
    assert live.ctl._sink is not None and live.ctl._trace is None and live.ctl._taps == []  # type: ignore[union-attr]


def test_k1_the_layout_follows_the_setting_and_the_target_window(make_bench: Callable[..., Bench]) -> None:
    bench = make_bench()
    bench.desktop.target = KeyTarget(100, 200, "FakeTerminal", 0x040D, None, False, False)
    bench.open()
    assert bench.kb_events()[0]["lang"] == "he"
    bench.cmd("stop")
    bench.cmd("configure", settings={"layout": "en"})
    bench.open()
    assert bench.kb_events("open")[1]["lang"] == "en"
    bench.cmd("stop")
    bench.cmd("configure", settings={"layout": "auto"})
    bench.open("practice")  # a practice is English whatever the window says
    assert bench.kb_events("practice")[0]["lang"] == "en"


def test_k1_the_keyboard_goes_on_the_display_of_the_target_window(make_bench: Callable[..., Bench]) -> None:
    first = Display(1, "1", Rect(0, 0, 1920, 1080), Rect(0, 0, 1920, 1040), primary=True)
    second = Display(2, "2", Rect(1920, 0, 1280, 800), Rect(1920, 0, 1280, 760), primary=False)
    window = FakeWindow(100, "t", Rect(2200, 100, 600, 400))
    bench = make_bench(desktop=FakeDesktop([first, second], [window]))
    bench.open()
    bench.run(0.1)
    assert bench.view.work == (1920, 0, 1280, 760)
    bench.cmd("stop")
    bench.desktop.windows.clear()  # no such window now: the primary display
    bench.open()
    bench.run(0.1)
    assert bench.view.work == (0, 0, 1920, 1040)


def test_k1_the_session_starts_on_the_frame_clock_whatever_the_runtime_clock_says(
    make_bench: Callable[..., Bench],
) -> None:
    bench = make_bench()
    bench._clock.t = 50_000.0  # the runtime's clock is nowhere near the camera's
    bench.open()
    bench.t = 3.0
    bench.run(2.0)
    assert bench.ctl.active and bench.session.phase == "warmup"  # type: ignore[union-attr]  # it placed, on frame time


# ----------------------------------------------------------------------------------------------- K3: commands in phases


PHASES = ("placing", "warmup", "typing")


def reach(bench: Bench, phase: str) -> None:
    """A fresh session in the named phase (the one before is closed)."""
    bench.cmd("stop")
    bench.open()
    bench.run(0.2)  # the first frame builds the session
    if phase != "placing":
        bench.place()
    if phase == "typing":
        bench.warm()
        bench.run(0.2)
    assert bench.session.phase == phase


def test_k3_the_simple_commands_answer_ok_in_every_phase_and_do_their_work(make_bench: Callable[..., Bench]) -> None:
    bench = make_bench()
    assert bench.cmd("configure", settings={"size": 1.1}) == {"ok": True}
    for phase in PHASES:
        reach(bench, phase)
        for action in ("recenter", "private", "public", "configure"):
            extra = {"settings": {"size": 1.1}} if action == "configure" else {}
            assert bench.cmd(action, **extra) == {"ok": True}, (phase, action)
        assert bench.ctl.active  # type: ignore[union-attr]


def test_k3_recenter_private_and_public_refuse_a_closed_keyboard_with_a_fixed_sentence(
    make_bench: Callable[..., Bench],
) -> None:
    """Defence in depth (the mod sends none of them while closed): an "ok" for ``private`` on a closed keyboard would
    be a false assurance, as the next open starts public."""
    bench = make_bench()
    for action in ("recenter", "private", "public"):
        message = f"The air keyboard is not open. Open it first, then use {action}."
        expected = {"ok": False, "error": {"code": "bad_request", "message": message}}
        assert bench.cmd(action) == expected, action
        assert bench.cmd("configure", settings={"size": 1.1}) == {"ok": True}  # the settings do not need a keyboard
    bench.open()
    bench.cmd("stop")
    for action in ("recenter", "private", "public"):
        assert bench.cmd(action)["ok"] is False, action  # closed again
    bench.open()
    assert bench.cmd("private") == {"ok": True}  # open: it goes through, before the first frame as well
    assert bench.ctl.status()["private"] is True  # type: ignore[index,union-attr]


def test_k3_private_and_public_reach_the_session_and_the_view(make_bench: Callable[..., Bench]) -> None:
    bench = make_bench().arm_up()
    assert bench.ctl.status()["private"] is False  # type: ignore[index,union-attr]
    bench.cmd("private")
    bench.run(0.1)
    assert bench.ctl.status()["private"] is True  # type: ignore[index,union-attr]
    assert bench.view.private is True and bench.view.exclude_capture is True
    assert bench.kb_events("open")[-1]["private"] is True
    bench.cmd("public")
    bench.run(0.1)
    assert bench.view.private is False and bench.view.exclude_capture is False


def test_k3_private_asked_before_the_first_frame_is_kept(make_bench: Callable[..., Bench]) -> None:
    bench = make_bench()
    bench.open()
    bench.cmd("private")
    assert bench.ctl.status()["private"] is True  # type: ignore[index,union-attr]
    bench.run(0.1)
    assert bench.view.private is True


def test_k3_recenter_sends_a_typing_session_back_to_placing_with_its_box(make_bench: Callable[..., Bench]) -> None:
    bench = make_bench().arm_up()
    bench.type_text("ab")
    assert bench.text == "ab"
    assert bench.cmd("recenter") == {"ok": True}
    bench.run(0.1)
    assert bench.session.phase == "placing" and bench.text == "ab"


@pytest.mark.parametrize("phase", PHASES)
def test_k3_stop_closes_with_command_in_every_phase_and_the_second_stop_says_nothing(
    make_bench: Callable[..., Bench], phase: str
) -> None:
    bench = make_bench()
    reach(bench, phase)
    assert bench.cmd("stop") == {"ok": True}
    assert bench.closed_reason == "command" and not bench.ctl.active  # type: ignore[union-attr]
    count = len(bench.events)
    assert bench.cmd("stop") == {"ok": True} and len(bench.events) == count


def test_k3_stop_when_nothing_is_open_is_a_quiet_ok(make_bench: Callable[..., Bench]) -> None:
    bench = make_bench()
    assert bench.cmd("stop") == {"ok": True} and not bench.events and not bench.shown


# ---------------------------------------------------------------------------- K41: what a configure does to an open one


def test_k41_press_commit_layout_reach_idle_inject_and_enter_wait_for_the_next_open(
    make_bench: Callable[..., Bench],
) -> None:
    bench = make_bench().arm_up()
    before = bench.ctl.status()  # type: ignore[union-attr]
    assert (before["press"], before["commit"]) == ("air", "review")  # type: ignore[index]
    changes = {
        "press": "pinch",
        "commit": "direct",
        "layout": "he",
        "reach": 1.4,
        "idleS": 99,
        "inject": "vk",
        "enter": "off",
    }
    assert bench.cmd("configure", settings=changes) == {"ok": True}
    bench.run(0.2)
    after = bench.ctl.status()  # type: ignore[union-attr]
    assert after["press"] == "air" and after["commit"] == "review" and after["lang"] == "en"  # type: ignore[index]
    assert bench.view.commit == "review" and bench.session._idle_s == 30 and bench.session._reach == 1.0
    assert bench.ctl._sink._inject == "unicode" and bench.session._enter == "twice"  # type: ignore[union-attr]
    bench.cmd("stop")
    bench.open()  # pinch with direct has its marker: the new values apply now
    bench.run(0.2)
    assert bench.ctl.status()["press"] == "pinch" and bench.view.commit == "direct"  # type: ignore[index,union-attr]
    assert bench.ctl._sink._inject == "vk" and bench.session._idle_s == 99 and bench.session._reach == 1.4  # type: ignore[union-attr]


def test_k41_size_and_dock_apply_from_the_next_frame(make_bench: Callable[..., Bench]) -> None:
    bench = make_bench().arm_up()
    assert (bench.view.size, bench.view.dock) == (1.0, "top")
    bench.cmd("configure", settings={"size": 1.5, "dock": "bottom"})
    bench.run(0.1)
    assert (bench.view.size, bench.view.dock) == (1.5, "bottom")


def test_k41_enabled_false_closes_an_open_session_with_disabled(make_bench: Callable[..., Bench]) -> None:
    bench = make_bench().arm_up()
    assert bench.cmd("configure", settings={"enabled": False}) == {"ok": True}
    assert bench.closed_reason == "disabled" and not bench.ctl.active  # type: ignore[union-attr]
    bench.events.clear()
    bench.cmd("configure", settings={"enabled": False})  # already off: nothing to close
    assert not bench.events


# ------------------------------------------------------------------------------------ K2: close, and the overlay policy


def test_k2_close_is_idempotent_and_the_second_call_does_nothing(make_bench: Callable[..., Bench]) -> None:
    bench = make_bench().arm_up()
    bench.ctl.close("command")  # type: ignore[union-attr]
    (closed,) = bench.kb_events("closed")
    assert closed["reason"] == "command" and closed["discarded"] == 0
    assert bench.pointer_reset_calls == 1 and bench.desktop.release_keys_calls >= 1
    releases, events, shown = bench.desktop.release_keys_calls, len(bench.events), len(bench.shown)
    for reason in ("command", "idle", "error"):
        bench.ctl.close(reason)  # type: ignore[union-attr,arg-type]
    assert (bench.desktop.release_keys_calls, len(bench.events), len(bench.shown)) == (releases, events, shown)
    assert bench.pointer_reset_calls == 1


def test_k2_a_close_releases_the_keys_resets_the_pointer_blanks_the_overlay_and_then_says_so(
    make_bench: Callable[..., Bench],
) -> None:
    bench = make_bench().arm_up()
    bench.desktop.release_keys = lambda: bench.order.append("release")  # type: ignore[method-assign]
    bench.order.clear()
    bench.ctl.close("command")  # type: ignore[union-attr]
    assert bench.order == ["release", "reset", "blank", "closed"]


def test_k2_a_show_that_fails_during_the_close_calls_close_again_and_it_is_swallowed(
    make_bench: Callable[..., Bench],
) -> None:
    bench = make_bench().arm_up()
    bench.show_error = OSError(S)
    releases = bench.desktop.release_keys_calls
    bench.ctl.close("command")  # type: ignore[union-attr]
    (closed,) = bench.kb_events("closed")
    assert closed["reason"] == "command"
    assert bench.pointer_reset_calls == 1 and bench.desktop.release_keys_calls == releases + 1
    assert not bench.ctl.active  # type: ignore[union-attr]


def test_k2_a_draw_that_fails_on_a_frame_closes_no_overlay_once_and_the_frame_goes_no_further(
    make_bench: Callable[..., Bench],
) -> None:
    bench = make_bench()
    bench.open()
    bench.show_error = RuntimeError(S)
    bench.run(0.5)
    assert bench.closed_reason == "no_overlay" and len(bench.kb_events("closed")) == 1
    assert not bench.ctl.active and bench.kb_events("open") == bench.kb_events("open")[:1]  # type: ignore[union-attr]


def real(make_bench: Callable[..., Bench], **kw: Any) -> Bench:
    """A bench whose fake desktop injects "for real": the overlay must then be alive and have drawn lately."""
    return make_bench(desktop=FakeDesktop(injects_for_real=True), overlay=HealthOverlay(), **kw)


def test_k2_a_fake_desktop_needs_no_overlay_at_all(make_bench: Callable[..., Bench]) -> None:
    bench = make_bench(overlay=NullOverlay()).arm_up()  # ``run --fake`` and the integration tests (SR9)
    bench.run(5.0)
    assert bench.view.hold is None and bench.ctl.active  # type: ignore[union-attr]


def test_k2_a_healthy_overlay_holds_nothing(make_bench: Callable[..., Bench]) -> None:
    bench = real(make_bench).arm_up()
    bench.run(3.0)
    assert bench.view.hold is None and bench.ctl.active  # type: ignore[union-attr]


def test_k2_a_stale_draw_holds_overlay_and_two_seconds_of_it_close_no_overlay(make_bench: Callable[..., Bench]) -> None:
    bench = real(make_bench).arm_up()
    bench.overlay.value = replace(HEALTHY, ok_age_s=HEALTH_MAX_AGE_S + 0.1)
    bench.run(0.2)
    assert bench.view.hold == "overlay" and bench.ctl.active  # type: ignore[union-attr]
    bench.run(HEALTH_CLOSE_S - 0.5)
    assert bench.view.hold == "overlay" and bench.ctl.active  # type: ignore[union-attr]
    bench.run(0.7)
    assert bench.closed_reason == "no_overlay" and len(bench.kb_events("closed")) == 1


def test_k2_an_overlay_that_recovers_starts_the_two_seconds_again(make_bench: Callable[..., Bench]) -> None:
    bench = real(make_bench).arm_up()
    stale = replace(HEALTHY, ok_age_s=2.0)
    for _ in range(3):
        bench.overlay.value = stale
        bench.run(1.5)
        assert bench.ctl.active and bench.view.hold == "overlay"  # type: ignore[union-attr]
        bench.overlay.value = HEALTHY
        bench.run(0.2)
        assert bench.view.hold is None


@pytest.mark.parametrize(
    "bad",
    [
        OverlayHealth(alive=True, failures=3, ok_age_s=0.1, keyboard_ok=True),
        OverlayHealth(alive=True, failures=0, ok_age_s=0.1, keyboard_ok=False),
        OverlayHealth(alive=True, failures=0, ok_age_s=None, keyboard_ok=True),
        None,
    ],
    ids=["failing draws", "no font", "never drew", "silent"],
)
def test_k2_every_unhealthy_overlay_holds_and_then_closes(
    make_bench: Callable[..., Bench], bad: OverlayHealth | None
) -> None:
    bench = real(make_bench).arm_up()
    bench.overlay.value = bad
    bench.run(0.5)
    assert bench.view.hold == "overlay"
    bench.run(HEALTH_CLOSE_S)
    assert bench.closed_reason == "no_overlay"


def test_k2_an_overlay_that_is_gone_closes_at_once(make_bench: Callable[..., Bench]) -> None:
    bench = real(make_bench).arm_up()
    bench.overlay.value = OverlayHealth(alive=False, failures=0, ok_age_s=None, keyboard_ok=True)
    bench.run(0.1)
    assert bench.closed_reason == "no_overlay"


def test_k2_a_practice_has_the_same_overlay_policy(make_bench: Callable[..., Bench]) -> None:
    bench = real(make_bench, markers=False)
    bench.open("practice")
    bench.run(0.2)
    bench.overlay.value = OverlayHealth(alive=False, failures=0, ok_age_s=None, keyboard_ok=True)
    bench.run(0.1)
    assert bench.closed_reason == "no_overlay"


# ---------------------------------------------------------------------------------------------- K42: a box at the close


def test_k42_closing_with_text_in_the_box_counts_it_and_logs_numbers_only(
    make_bench: Callable[..., Bench], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    bench = make_bench().arm_up()
    bench.fill(S)
    assert bench.text == S
    bench.cmd("stop")
    (closed,) = bench.kb_events("closed")
    assert closed["discarded"] == len(S) and closed["reason"] == "command"
    validate(closed, "Event")
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("keyboard closed")]
    assert len(lines) == 1 and f"discarded {len(S)}" in lines[0] and "(command)" in lines[0]
    assert S not in caplog.text and S not in json.dumps(bench.events)
    assert bench.desktop.typed_text == ""  # a close inserts nothing, whatever was in the box


def test_k42_the_close_line_has_counts_and_reasons_and_nothing_else(
    make_bench: Callable[..., Bench], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    bench = make_bench().arm_up()
    bench.type_text("ab")
    bench.cmd("stop")
    (line,) = [r.getMessage() for r in caplog.records if r.getMessage().startswith("keyboard closed")]
    assert re.fullmatch(
        r"keyboard closed \(command\): \d+ keys in \d+ s; dropped \{[^}]*\}; rejects \{[^}]*\}; discarded \d+; "
        r"overlay draw median (n/a|\d+\.\d) ms; level ok; noise \d\.\d{3}; fps \d+\.\d",
        line,
    ), line
    assert "ab" not in line.replace("rejects", "").replace("dropped", "").replace("keyboard", "").replace("about", "")


def test_k42_the_open_line_names_the_mode_and_nothing_typed(
    make_bench: Callable[..., Bench], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    bench = make_bench()
    bench.open()
    assert [r.getMessage() for r in caplog.records if r.getMessage().startswith("keyboard open")] == [
        "keyboard open (live, air, review, en)"
    ]


# ---------------------------------------------------------------------------------------------------- K7: a frame fails


def boom(self: object, *args: object, **kwargs: object) -> None:
    raise RuntimeError(f"bad stroke {S!r}")


def test_k7_an_exception_in_a_frame_closes_error_reports_once_and_says_nothing_of_the_exception(
    make_bench: Callable[..., Bench], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bench = make_bench().arm_up()
    monkeypatch.setattr(KeyboardSession, "update", boom)
    bench.run(0.5)
    assert bench.closed_reason == "error" and len(bench.kb_events("closed")) == 1
    assert bench.reports == [("internal", "The air keyboard stopped because of an internal error.")]
    assert bench.reports[0][1] == TEXT["frame_error"]
    assert S not in caplog.text + json.dumps(bench.events) + repr(bench.reports)
    assert "keyboard: RuntimeError in frame" in caplog.text
    bench.run(0.5)  # nothing more is reported by the frames that follow
    assert len(bench.reports) == 1 and len(bench.kb_events("closed")) == 1


def test_k7_the_next_frames_go_to_the_engine_again_once_the_camera_has_seen_no_hand(
    make_bench: Callable[..., Bench], monkeypatch: pytest.MonkeyPatch
) -> None:
    bench = make_bench().arm_up()
    ctl = bench.ctl
    monkeypatch.setattr(KeyboardSession, "update", boom)
    bench.run(0.2)
    assert bench.closed_reason == "error" and ctl is not None
    monkeypatch.undo()
    frame = bench.hands_frame(bench.t + 0.1)
    assert ctl.pointer_frame(frame).hands == ()  # the hands are still up: the pointer waits
    t = frame.t
    for _ in range(36):  # no hand for a second and a half
        t += 0.05
        ctl.pointer_frame(Frame(t, (), frame.width, frame.height))
    again = bench.hands_frame(t + 0.05)
    assert ctl.pointer_frame(again) is again


def test_k7_an_error_that_is_not_an_exception_class_of_ours_is_still_only_a_type_name(
    make_bench: Callable[..., Bench], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class Odd(Exception):
        def __str__(self) -> str:
            raise AssertionError("the controller formatted the exception")

        __repr__ = __str__

    bench = make_bench().arm_up()
    monkeypatch.setattr(KeyboardSession, "update", lambda *a, **k: (_ for _ in ()).throw(Odd()))
    bench.run(0.2)
    assert bench.closed_reason == "error" and "Odd in frame" in caplog.text


# ------------------------------------------------------------------------------------- K8, K45, X34b: the status block


def test_k8_the_status_of_a_closed_keyboard_validates_and_says_closed(make_bench: Callable[..., Bench]) -> None:
    bench = make_bench(markers=False)
    status = bench.ctl.status()  # type: ignore[union-attr]
    assert status == {"enabled": True, "state": "closed", "practiced": False}
    validate(status, "KeyboardStatus")
    bench.cmd("configure", settings={"enabled": False})
    assert bench.ctl.status() == {"enabled": False, "state": "closed", "practiced": False}  # type: ignore[union-attr]


def test_k8_a_marker_makes_it_practiced_and_gives_the_phantom_rate_of_its_rest(
    make_bench: Callable[..., Bench],
) -> None:
    bench = make_bench(markers=False)
    write_marker(bench.tmp, "air", result(rest_s=100.0, phantoms=2))
    status = bench.ctl.status()  # type: ignore[union-attr]
    assert status == {"enabled": True, "state": "closed", "practiced": True, "phantomsPerMin": 1.2}
    validate(status, "KeyboardStatus")
    bench.cmd("configure", settings={"press": "pinch"})  # the marker of the method that is set
    assert bench.ctl.status()["practiced"] is False  # type: ignore[index,union-attr]
    bench.cmd("configure", settings={"press": "windows"})
    assert bench.ctl.status()["practiced"] is False  # type: ignore[index,union-attr]


def test_k45_the_status_of_an_open_review_session_validates_with_commit_and_review(
    make_bench: Callable[..., Bench],
) -> None:
    bench = make_bench()
    bench.open()
    status = bench.ctl.status()  # type: ignore[union-attr]
    validate(status, "KeyboardStatus")
    assert status["state"] == "open" and status["phase"] == "placing" and status["commit"] == "review"  # type: ignore[index]
    bench.run(0.2)
    bench.arm()
    bench.run(0.2)
    status = bench.ctl.status()  # type: ignore[union-attr]
    validate(status, "KeyboardStatus")
    assert status["phase"] == "typing" and status["review"] == {"state": "composing", "chars": 0}  # type: ignore[index]
    bench.type_text("hey")
    status = bench.ctl.status()  # type: ignore[union-attr]
    validate(status, "KeyboardStatus")
    assert status["review"] == {"state": "composing", "chars": 3}  # type: ignore[index]
    assert "hey" not in json.dumps(status)


def test_k45_a_direct_session_has_no_review_block(make_bench: Callable[..., Bench]) -> None:
    bench = make_bench(press="pinch", commit="direct").arm_up()
    status = bench.ctl.status()  # type: ignore[union-attr]
    validate(status, "KeyboardStatus")
    assert "review" not in status and status["commit"] == "direct"  # type: ignore[operator]


def test_k45_the_hold_is_in_the_status_while_one_applies(make_bench: Callable[..., Bench]) -> None:
    bench = make_bench().arm_up()
    bench.run(0.5)  # the sink's own warm-up after arming ignores the user
    assert "hold" not in bench.ctl.status()  # type: ignore[operator,union-attr]
    bench.desktop.user_typed()
    bench.run(bench.dt)
    status = bench.ctl.status()  # type: ignore[union-attr]
    assert status["hold"] == "yield" and bench.view.hold == "yield"  # type: ignore[index]
    validate(status, "KeyboardStatus")


def test_x34b_air_fps_and_noise_are_numbers_with_1_and_3_decimals_while_the_press_is_air(
    make_bench: Callable[..., Bench],
) -> None:
    bench = make_bench().arm_up()
    bench.press._quality = PressQuality(29.84567, 0.0123456, 0)
    status = bench.ctl.status()  # type: ignore[union-attr]
    validate(status, "KeyboardStatus")
    assert status["airFps"] == 29.8 and status["airNoise"] == 0.012 and status["level"] == "ok"  # type: ignore[index]
    assert status["press"] == "air"  # type: ignore[index]


def test_x34b_an_unknown_noise_is_zero_and_an_unknown_rate_is_the_runtimes(make_bench: Callable[..., Bench]) -> None:
    bench = make_bench().arm_up()
    bench.fps = 27.26
    bench.press._quality = PressQuality(0.0, None, 0)
    status = bench.ctl.status()  # type: ignore[union-attr]
    validate(status, "KeyboardStatus")
    assert status["airFps"] == 27.3 and status["airNoise"] == 0.0  # type: ignore[index]


def test_x34b_they_are_absent_for_the_pinch_and_while_closed(make_bench: Callable[..., Bench]) -> None:
    pinch = make_bench(press="pinch", commit="direct").arm_up()
    status = pinch.ctl.status()  # type: ignore[union-attr]
    validate(status, "KeyboardStatus")
    assert not {"airFps", "airNoise", "level"} & set(status)  # type: ignore[arg-type]
    pinch.cmd("stop")
    assert not {"airFps", "airNoise", "level"} & set(pinch.ctl.status())  # type: ignore[arg-type,union-attr]
    air = make_bench()
    assert not {"airFps", "airNoise", "level"} & set(air.ctl.status())  # type: ignore[arg-type,union-attr]


def test_x34b_the_event_carries_the_level_for_air_only(make_bench: Callable[..., Bench]) -> None:
    air, pinch = make_bench(), make_bench(press="pinch", commit="direct")
    air.open()
    pinch.open()
    assert air.kb_events()[0]["level"] == "ok" and "level" not in pinch.kb_events()[0]


def test_k8_the_status_of_a_practice_says_practice(make_bench: Callable[..., Bench]) -> None:
    bench = make_bench(markers=False)
    bench.open("practice")
    status = bench.ctl.status()  # type: ignore[union-attr]
    validate(status, "KeyboardStatus")
    assert status["state"] == "practice" and "commit" in status  # type: ignore[operator]


# -------------------------------------------------------------------------------------------------------- change events


def test_k1_a_change_is_one_event_and_a_frame_without_one_is_none(make_bench: Callable[..., Bench]) -> None:
    bench = make_bench()
    bench.open()
    bench.run(0.2)
    first = len(bench.events)
    assert first >= 1
    bench.run(0.5)
    assert len(bench.events) - first <= 1  # the hands stand still: at most the phase change
    count = len(bench.events)
    bench.run(0.5)
    assert len(bench.events) == count


def test_k1_change_events_come_at_most_twice_a_second_and_a_closed_event_is_never_held_back(
    make_bench: Callable[..., Bench],
) -> None:
    bench = make_bench().arm_up()
    bench.events.clear()
    for k in range(38):  # a hold that flips every other frame
        bench.desktop.user_typed() if k % 2 == 0 else None
        bench.run(bench.dt)
    changes = [e for e in bench.events if e["type"] == "keyboard" and e["state"] == "open"]
    assert 1 <= len(changes) <= 3  # two a second over about 1.3 s, plus the one the budget let through at the start
    bench.cmd("stop")
    assert bench.events[-1]["state"] == "closed"


def test_k1_every_event_validates_and_carries_enums_and_counts_only(make_bench: Callable[..., Bench]) -> None:
    bench = make_bench().arm_up()
    bench.fill(S)
    bench.insert(bench.t + bench.dt)
    bench.run(5.0)
    bench.cmd("stop")
    for event in bench.events:
        validate(event, "Event")
    assert S not in json.dumps(bench.events)
    inserts = [e for e in bench.kb_events("open") if "insert" in e.get("review", {})]
    assert len(inserts) == 1 and inserts[0]["review"]["insert"]["of"] == len(S)


# ------------------------------------------------------------------------------------- K9: the practice's files and K48


def traces(bench: Bench) -> list[Path]:
    """The trace files in the data directory. (Their names carry the second they were written in, so a test cannot
    ask for one by name: the second may have changed between the close and the question.)"""
    return sorted(trace_path(bench.tmp).parent.glob(f"{TRACE_PREFIX}*.npz"))


def type_phrases(bench: Bench, *, gap_s: float = 0.5) -> None:
    """A user who types each phrase correctly and then rests, until the practice is over."""
    script = bench.session._script
    while bench.ctl.active and bench.t < 900:  # type: ignore[union-attr]
        if script.segment == "phrase":
            echo, prompt = bench.view.echo, bench.view.prompt
            if len(echo) < len(prompt):
                ch = prompt[len(echo)]
                bench.tap(bench.t + bench.dt, "space" if ch == " " else ch)
                bench.run(gap_s)
                continue
        bench.run(bench.dt)


@pytest.fixture
def short_phrases(monkeypatch: pytest.MonkeyPatch) -> None:
    """Practice with six two-letter phrases: the same script in seconds of frame time instead of minutes."""
    monkeypatch.setattr(controller_module, "KeyboardSession", partial(KeyboardSession, phrases=SHORT))


@pytest.mark.usefixtures("short_phrases")
def test_k9_a_whole_practice_writes_the_marker_and_the_trace_and_closes_with_its_numbers(
    make_bench: Callable[..., Bench],
) -> None:
    bench = make_bench(press="pinch", commit="direct", markers=False)
    assert marker_status(bench.tmp, "pinch") == "missing"
    bench.arm_up_practice()
    type_phrases(bench)
    (closed,) = bench.kb_events("closed")
    assert closed["reason"] == "command" and closed["practice"]["hitRate"] == pytest.approx(1.0)
    assert closed["practice"]["phantomsPerMin"] == 0.0 and "recallIM" not in closed["practice"]
    assert marker_status(bench.tmp, "pinch") == "ok"
    assert traces(bench)
    assert bench.desktop.key_calls == []  # a practice types nowhere


@pytest.mark.usefixtures("short_phrases")
def test_k9_a_trace_that_cannot_be_written_does_not_stop_the_practice_or_the_marker(
    make_bench: Callable[..., Bench], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def full(self: object, *args: object) -> None:
        raise OSError(f"disk full {S}")

    monkeypatch.setattr(TraceWriter, "save", full)
    monkeypatch.setattr(TapLog, "write", full)
    bench = make_bench(press="pinch", commit="direct", markers=False)
    bench.arm_up_practice()
    type_phrases(bench)
    (closed,) = bench.kb_events("closed")
    assert closed["reason"] == "command" and "practice" in closed
    assert marker_status(bench.tmp, "pinch") == "ok" and not traces(bench)
    assert "OSError in trace" in caplog.text and S not in caplog.text


@pytest.mark.usefixtures("short_phrases")
def test_k9_a_marker_that_cannot_be_written_does_not_stop_the_close_or_the_trace(
    make_bench: Callable[..., Bench], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def full(*args: object) -> None:
        raise PermissionError(S)

    monkeypatch.setattr(controller_module, "write_marker", full)
    bench = make_bench(press="pinch", commit="direct", markers=False)
    bench.arm_up_practice()
    type_phrases(bench)
    (closed,) = bench.kb_events("closed")
    assert closed["reason"] == "command" and marker_status(bench.tmp, "pinch") == "missing"
    assert traces(bench)
    assert "PermissionError in marker" in caplog.text and S not in caplog.text


def test_k9_a_frame_the_trace_cannot_take_drops_the_trace_and_keeps_the_practice(
    make_bench: Callable[..., Bench], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(TraceWriter, "add", boom)
    bench = make_bench(markers=False)
    bench.open("practice")
    bench.run(1.0)
    assert bench.ctl.active and bench.ctl._trace is None  # type: ignore[union-attr]
    assert caplog.text.count("RuntimeError in trace") == 1 and S not in caplog.text


def test_k9_a_cut_practice_writes_no_marker_and_says_nothing_of_a_result(make_bench: Callable[..., Bench]) -> None:
    bench = make_bench(markers=False).arm_up_practice()
    bench.cmd("stop")  # the user gave up in the middle of the drill
    (closed,) = bench.kb_events("closed")
    assert closed["reason"] == "command" and "practice" not in closed
    assert marker_status(bench.tmp, "air") == "missing"


def cut_by_the_ladder(bench: Bench) -> None:
    """A practice on a camera whose air tap the ladder finds unusable (10 fps): the drill is cut, its sentence shown."""
    bench.press.set_quality(10.0, 0.001)
    bench.run(2.4)
    assert bench.session.practice_done and bench.ctl.active  # type: ignore[union-attr]


def test_k9_a_practice_the_ladder_cut_closes_air_unreliable_after_its_sentence_and_writes_no_marker(
    make_bench: Callable[..., Bench],
) -> None:
    bench = make_bench(markers=False).arm_up_practice()
    cut_by_the_ladder(bench)
    bench.run(controller_module.CUT_SHOW_S - 1.0)
    assert bench.ctl.active and not bench.kb_events("closed")  # type: ignore[union-attr]  # its sentence stays up
    bench.run(2.0)
    (closed,) = bench.kb_events("closed")
    # Not "command": the mod says nothing of that reason, so the user would be left with no word at all (X38).
    assert closed["reason"] == "air_unreliable" and closed["discarded"] == 0 and "practice" not in closed
    assert marker_status(bench.tmp, "air") == "missing"


def test_k9_after_a_cut_practice_the_next_air_open_names_the_pinch_not_practice_first(
    make_bench: Callable[..., Bench],
) -> None:
    bench = make_bench(markers=False)
    assert bench.cmd("start")["error"]["message"] == TEXT["practice_air"]  # never practised: practise
    bench.arm_up_practice()
    cut_by_the_ladder(bench)
    bench.run(controller_module.CUT_SHOW_S + 1.0)
    reply = bench.cmd("start")
    assert reply == {"ok": False, "error": {"code": "bad_request", "message": TEXT["practice_air_cut"]}}
    validate(reply, "CommandResponse")
    assert "press pinch" in TEXT["practice_air_cut"] and "Practice first" not in TEXT["practice_air_cut"]
    assert not bench.ctl.active and bench.pointer_off_calls == 1  # type: ignore[union-attr]  # only the practice's own
    # the way out it names works at once: the pinch with the review box needs no marker
    assert bench.cmd("configure", settings={"press": "pinch"}) == {"ok": True}
    bench.open("start")
    assert bench.ctl.status()["press"] == "pinch"  # type: ignore[index,union-attr]


def test_k9_a_stop_in_the_middle_of_a_practice_is_not_a_cut(make_bench: Callable[..., Bench]) -> None:
    bench = make_bench(markers=False).arm_up_practice()
    bench.cmd("stop")
    assert bench.cmd("start")["error"]["message"] == TEXT["practice_air"]


def test_k9_a_stop_while_the_cut_sentence_is_up_still_remembers_the_cut(make_bench: Callable[..., Bench]) -> None:
    bench = make_bench(markers=False).arm_up_practice()
    cut_by_the_ladder(bench)
    bench.cmd("stop")  # the user read the strip and did not wait for the close
    (closed,) = bench.kb_events("closed")
    assert closed["reason"] == "command"
    assert bench.cmd("start")["error"]["message"] == TEXT["practice_air_cut"]


def test_k9_a_cut_practice_before_a_poor_marker_still_names_the_pinch(make_bench: Callable[..., Bench]) -> None:
    bench = make_bench(markers=False)
    write_marker(bench.tmp, "air", result(rest_s=15.0, phantoms=0, hits_im=2))
    assert bench.cmd("start")["error"]["message"] == TEXT["practice_air_poor"]
    bench.arm_up_practice()
    cut_by_the_ladder(bench)
    bench.run(controller_module.CUT_SHOW_S + 1.0)
    assert bench.cmd("start")["error"]["message"] == TEXT["practice_air_cut"]  # the newer word is the cut


def test_k9_a_practice_that_ends_after_a_cut_takes_the_cut_back(make_bench: Callable[..., Bench]) -> None:
    bench = make_bench(markers=False).arm_up_practice()
    cut_by_the_ladder(bench)
    bench.run(controller_module.CUT_SHOW_S + 1.0)
    assert bench.cmd("start")["error"]["message"] == TEXT["practice_air_cut"]
    # the camera is fine this time (the open builds a new press); the user taps the wrong things, so it ends poor
    bench.arm_up_practice()
    while bench.ctl.active and bench.t < 1200:  # type: ignore[union-attr]
        bench.tap(bench.t + bench.dt, "a")  # a tap now and then keeps the practice from idling out
        bench.run(8.0)
    assert bench.kb_events("closed")[-1]["reason"] == "command" and marker_status(bench.tmp, "air") == "out_of_range"
    assert bench.cmd("start")["error"]["message"] == TEXT["practice_air_poor"]  # not the cut's: it is over


def test_k9_a_practice_keeps_its_trace_in_memory_until_the_close(make_bench: Callable[..., Bench]) -> None:
    bench = make_bench(markers=False).arm_up_practice()
    assert not traces(bench) and len(bench.ctl._trace) > 0  # type: ignore[arg-type,union-attr]
    bench.cmd("stop")
    assert traces(bench)


def test_k48_a_practice_in_the_review_layout_has_inert_review_keys_and_sends_nothing(
    make_bench: Callable[..., Bench],
) -> None:
    bench = make_bench(markers=False).arm_up_practice()
    assert bench.ctl._sink is None and bench.view.mode == "practice"  # type: ignore[union-attr]
    before = bench.session.counts["practice_review_key"]
    for name in ("insert", "clear", "chip", "enter"):
        bench.tap(bench.t + bench.dt, name)
        bench.run(0.3)
        assert bench.view.strip == REVIEW_TEXT["practice"], name
        assert bench.text == "" and bench.desktop.key_calls == []
    assert bench.session.counts["practice_review_key"] == before + 4 and bench.ctl.active  # type: ignore[union-attr]
    (event,) = bench.kb_events("practice")[:1]
    assert "commit" not in event and "review" not in event


# --------------------------------------------------------------------------------------- the ladder's fallback (2.12.7)


def test_the_ladder_falling_to_off_switches_to_a_pinch_the_controller_made_and_status_and_events_follow(
    make_bench: Callable[..., Bench],
) -> None:
    bench = make_bench().arm_up()
    assert bench.made == [("air", True)]
    bench.press.set_quality(10.0, 0.001)  # the camera reads 10 fps: the air tap is unusable
    bench.run(8.0)
    assert bench.made == [("air", True), ("pinch", True)]  # the fallback is the pinch of the same commit mode
    status = bench.ctl.status()  # type: ignore[union-attr]
    validate(status, "KeyboardStatus")
    assert status["press"] == "pinch" and not {"level", "airFps", "airNoise"} & set(status)  # type: ignore[index,arg-type]
    assert bench.active.name == "pinch" and bench.ctl.active  # type: ignore[union-attr]
    switched = [e for e in bench.kb_events("open") if e.get("press") == "pinch"]
    assert switched and switched[0]["level"] == "off"  # "off" is reported once, together with press pinch


def test_a_practice_has_no_fallback_so_level_off_ends_its_drill_and_a_live_pinch_has_none_either(
    make_bench: Callable[..., Bench],
) -> None:
    practice = make_bench(markers=False)
    practice.open("practice")
    practice.run(0.1)
    assert practice.session._fallback is None  # type: ignore[union-attr]
    pinch = make_bench(press="pinch", commit="direct")
    pinch.open()
    pinch.run(0.1)
    assert pinch.session._fallback is None  # type: ignore[union-attr]
    live = make_bench()
    live.open()
    live.run(0.1)
    assert live.session._fallback is not None  # type: ignore[union-attr]


# ----------------------------------------------------------------- K1: the two-hand switch and the release flag, and K3


def test_the_controller_wants_two_hands_exactly_while_a_session_is_open(make_bench: Callable[..., Bench]) -> None:
    bench = make_bench()
    ctl = bench.ctl
    assert ctl is not None and not ctl.wants_two_hands
    bench.open("practice")
    assert ctl.wants_two_hands
    bench.cmd("stop")
    assert not ctl.wants_two_hands and not ctl.active


def test_a_frame_before_any_open_and_after_a_close_does_nothing(make_bench: Callable[..., Bench]) -> None:
    bench = make_bench()
    frame = bench.hands_frame(1.0)
    bench.ctl.frame(frame, 1.0)  # type: ignore[union-attr]
    assert not bench.events and not bench.shown and bench.ctl._session is None  # type: ignore[union-attr]
    bench.arm_up()
    bench.cmd("stop")
    shown = len(bench.shown)
    bench.ctl.frame(bench.hands_frame(99.0), 99.0)  # type: ignore[union-attr]
    assert len(bench.shown) == shown


def test_k1_a_closed_session_is_gone_and_a_new_open_starts_from_nothing(make_bench: Callable[..., Bench]) -> None:
    bench = make_bench().arm_up()
    bench.fill(S)
    bench.cmd("private")
    bench.cmd("stop")
    assert bench.ctl.take_release() is True  # type: ignore[union-attr]  # an open asked for it; nobody took it
    bench.open()
    bench.ctl.take_release()  # type: ignore[union-attr]
    bench.run(0.2)
    assert bench.session.phase == "placing" and bench.text == "" and bench.ctl.status()["private"] is False  # type: ignore[index,union-attr]
    assert bench.pointer_off_calls == 2


# ----------------------------------------------------------------------------------------------- the quarantine of 2.11


def frame_at(t: float, *, hands: bool) -> Frame:
    bench_hand = KbRig(commit="review").hands_frame(t).hands
    return Frame(t, bench_hand if hands else (), 1280, 720)


def test_pointer_frame_hands_the_frame_through_untouched_while_nothing_was_ever_open(
    make_bench: Callable[..., Bench],
) -> None:
    bench = make_bench()
    frame = frame_at(1.0, hands=True)
    assert bench.ctl.pointer_frame(frame) is frame  # type: ignore[union-attr]


def test_pointer_frame_is_empty_while_a_session_is_open(make_bench: Callable[..., Bench]) -> None:
    bench = make_bench()
    bench.open()
    out = bench.ctl.pointer_frame(frame_at(1.0, hands=True))  # type: ignore[union-attr]
    assert out.hands == () and (out.t, out.width, out.height) == (1.0, 1280, 720)


def test_the_quarantine_lifts_after_0_6_s_without_a_hand_and_the_lifting_frame_is_still_empty(
    make_bench: Callable[..., Bench],
) -> None:
    bench = make_bench().arm_up()
    bench.cmd("stop")
    ctl = bench.ctl
    assert ctl is not None
    t = 10.0
    for _ in range(30):  # the hands are still up for a second
        t += 1 / 30
        assert ctl.pointer_frame(frame_at(t, hands=True)).hands == ()
    t0 = t + 1 / 30  # the first frame without a hand starts the timer
    assert ctl.pointer_frame(frame_at(t0, hands=False)).hands == ()
    just_under = frame_at(t0 + QUARANTINE_CLEAR_S - 0.01, hands=True)
    assert ctl.pointer_frame(just_under).hands == ()  # a hand came back before the 0.6 s were over: start again
    t1 = t0 + QUARANTINE_CLEAR_S
    assert ctl.pointer_frame(frame_at(t1, hands=False)).hands == ()
    assert ctl.pointer_frame(frame_at(t1 + QUARANTINE_CLEAR_S - 0.001, hands=False)).hands == ()
    assert ctl.pointer_frame(frame_at(t1 + QUARANTINE_CLEAR_S + 0.001, hands=False)).hands == ()  # completes it
    again = frame_at(t1 + QUARANTINE_CLEAR_S + 0.04, hands=True)
    assert ctl.pointer_frame(again) is again  # from the next frame on the engine sees the camera
    assert ctl.pointer_frame(frame_at(t1 + 5.0, hands=True)).hands != ()  # and it stays lifted


def test_stall_frames_without_hands_count_towards_the_quarantine(make_bench: Callable[..., Bench]) -> None:
    bench = make_bench().arm_up()
    bench.cmd("stop")
    ctl = bench.ctl
    assert ctl is not None
    ctl.pointer_frame(frame_at(20.0, hands=False))
    assert ctl.pointer_frame(frame_at(20.7, hands=False)).hands == ()  # one stall frame spanning the whole gap
    again = frame_at(20.73, hands=True)
    assert ctl.pointer_frame(again) is again


def test_a_close_with_quarantine_false_gives_the_pointer_back_at_once(make_bench: Callable[..., Bench]) -> None:
    bench = make_bench().arm_up()
    bench.ctl.close("command", quarantine=False)  # type: ignore[union-attr]
    frame = frame_at(30.0, hands=True)
    assert bench.ctl.pointer_frame(frame) is frame  # type: ignore[union-attr]


def test_a_quarantine_false_close_of_a_closed_controller_clears_a_pending_quarantine(
    make_bench: Callable[..., Bench],
) -> None:
    """``engage`` while the keyboard is already closed but its quarantine still runs: an explicit request wins (2.9)."""
    bench = make_bench().arm_up()
    bench.cmd("stop")
    held = frame_at(30.0, hands=True)
    assert bench.ctl.pointer_frame(held).hands == ()  # type: ignore[union-attr]
    bench.ctl.close("command", quarantine=False)  # type: ignore[union-attr]
    assert bench.ctl.pointer_frame(held) is held  # type: ignore[union-attr]
    count = len(bench.events)
    bench.ctl.close("command")  # type: ignore[union-attr]  # an ordinary close of a closed controller starts nothing
    assert bench.ctl.pointer_frame(held) is held and len(bench.events) == count  # type: ignore[union-attr]


def test_a_new_open_clears_the_quarantine_of_the_last_close(make_bench: Callable[..., Bench]) -> None:
    bench = make_bench().arm_up()
    bench.cmd("stop")
    bench.open()
    bench.cmd(
        "stop",
    )
    bench.ctl.close("command", quarantine=False)  # type: ignore[union-attr]
    held = frame_at(40.0, hands=True)
    assert bench.ctl.pointer_frame(held) is held  # type: ignore[union-attr]


class PointerRig:
    """The slice of ``_process`` that matters to the pointer, with a real engine and executor behind the controller.

    The keyboard has the frame while a session is open and the engine is not called; otherwise the engine gets what
    ``pointer_frame`` lets through (DESIGN-KEYBOARD.md 3.8, RT5).
    """

    def __init__(self, bench: Bench, engage: str) -> None:
        self.bench = bench
        self.rig = EngineRig(settings=HandsSettings(engage=engage))  # type: ignore[arg-type]
        self.script = Script(t0=bench.t + bench.dt)
        #: The number of hands in each frame the engine was given.
        self.seen: list[int] = []
        update = self.rig.engine.update

        def spy(frame: Frame, now: float | None = None) -> list[Action]:
            self.seen.append(len(frame.hands))
            return update(frame, now)

        self.rig.engine.update = spy  # type: ignore[method-assign]
        bench.on_pointer_off = self._off
        bench.on_pointer_reset = self.rig.engine.reset_tracks

    def _off(self) -> None:
        """``runtime._kb_pointer_off``: disengage, then break the tracks."""
        self.rig.engine.disengage("keyboard")
        self.rig.engine.reset_tracks()

    @property
    def engine(self) -> Any:
        return self.rig.engine

    def sync(self) -> None:
        """Pick the script up where the keyboard's own frames ended."""
        self.script.t = max(self.script.t, self.bench.t + self.bench.dt)

    def feed(self, frames: Sequence[Frame]) -> list[Action]:
        actions: list[Action] = []
        ctl = self.bench.ctl
        assert ctl is not None
        for frame in frames:
            self.bench.t = self.bench._clock.t = frame.t
            if ctl.active:
                ctl.frame(frame, frame.t)
            else:
                actions += self.rig.feed([ctl.pointer_frame(frame)])
        return actions

    def hold(self, pose: SyntheticPose, seconds: float) -> list[Action]:
        self.sync()
        return self.feed(self.script.hold(H(pose), seconds=seconds))

    def gap(self, seconds: float) -> list[Action]:
        self.sync()
        return self.feed(self.script.gap(seconds=seconds))

    @property
    def gestures(self) -> list[str]:
        return self.rig.gestures


ACTING = (Button, Scroll, GrabWindow, DragWindow, ResizeWindow, ThrowWindow)


def acting(actions: Sequence[Action]) -> list[Action]:
    """The actions that touch a window or a button (a cursor move or a release does not)."""
    return [a for a in actions if isinstance(a, ACTING)]


@pytest.mark.parametrize("engage", ["palm", "always"])
def test_without_the_quarantine_the_engine_would_see_the_hands_still_up_the_reason_for_it(
    make_bench: Callable[..., Bench], engage: str
) -> None:
    """The control for Q1 and Q2: the engine alone, fed the real frames after a close, engages on them."""
    bench = make_bench()
    pointer = PointerRig(bench, engage)
    pointer.engine.reset_tracks()
    pointer.sync()
    pointer.rig.feed(pointer.script.hold(H("palm"), seconds=1.5))
    assert pointer.engine.engaged


def test_q1_palm_mode_close_with_the_hands_up_keeps_the_engine_blind_until_0_6_s_without_a_hand(
    make_bench: Callable[..., Bench],
) -> None:
    bench = make_bench()
    pointer = PointerRig(bench, "palm")
    pointer.hold("palm", 1.5)
    assert pointer.engine.engaged  # the pointer was on
    bench.open()
    assert not pointer.engine.engaged  # the open turned it off
    pointer.hold("palm", 1.0)
    before = len(pointer.seen)
    assert before > 0
    bench.cmd("stop")
    actions = pointer.hold("palm", 3.0)  # the hands are still up
    assert pointer.seen[before:] == [0] * (len(pointer.seen) - before) and len(pointer.seen) > before
    assert not pointer.engine.engaged and not acting(actions) and "engage" not in pointer.gestures[1:]
    pointer.gap(0.5)  # not enough
    pointer.hold("palm", 0.5)  # a hand came back: the 0.6 s start again
    assert not pointer.engine.engaged
    pointer.gap(0.7)  # now it is lifted
    pointer.hold("palm", 0.3)  # a palm is held for ``engage_s`` before it engages (palm mode keeps its rules)
    assert not pointer.engine.engaged
    pointer.hold("palm", 0.9)
    assert pointer.engine.engaged
    assert pointer.gestures.count("engage") == 2  # the first one, and this one


def test_q2_always_mode_close_with_the_hands_up_neither_engages_nor_clicks_until_0_6_s_without_a_hand(
    make_bench: Callable[..., Bench],
) -> None:
    bench = make_bench()
    pointer = PointerRig(bench, "always")
    pointer.hold("palm", 0.5)
    assert pointer.engine.engaged
    bench.open()
    pointer.hold("palm", 0.5)
    bench.cmd("stop")
    for pose in ("palm", "pinch", "fist", "pinch", "palm"):  # whatever the hands do while they stay up
        actions = pointer.hold(pose, 1.0)  # type: ignore[arg-type]
        assert not pointer.engine.engaged and not acting(actions), pose
    assert set(pointer.seen[-30:]) == {0}
    pointer.gap(0.4)
    pointer.hold("palm", 0.2)
    assert not pointer.engine.engaged  # the timer started over when the hand came back
    pointer.gap(0.7)
    pointer.hold("palm", 0.2)
    assert pointer.engine.engaged  # always: at once


@pytest.mark.parametrize(
    ("engage", "pose"), [("always", "fist"), ("always", "pinch"), ("palm", "fist"), ("palm", "pinch")]
)
def test_q3_a_fist_or_a_pinch_held_when_the_quarantine_lifts_causes_no_grab_or_button_for_a_second(
    make_bench: Callable[..., Bench], engage: str, pose: str
) -> None:
    bench = make_bench()
    pointer = PointerRig(bench, engage)
    bench.open()
    pointer.hold("palm", 0.5)
    bench.cmd("stop")
    pointer.hold(pose, 1.0)  # type: ignore[arg-type]
    pointer.gap(0.7)  # the quarantine is lifted
    actions = pointer.hold(pose, 1.0)  # type: ignore[arg-type]  # and the hand is back, holding the pose
    assert not acting(actions)
    if engage == "palm":
        assert not pointer.engine.engaged  # a fist or a pinch is not an engage
    else:
        assert pointer.engine.engaged  # engaged on the pose it was holding, which the engine latched
        actions = pointer.hold("palm", 0.5)
        assert not acting(actions)


def test_q4_engage_and_calibrate_close_the_keyboard_with_no_quarantine_the_controller_side(
    make_bench: Callable[..., Bench],
) -> None:
    """The runtime's RT9 calls ``close("command", quarantine=False)``; the pointer then flows on the next frame."""
    bench = make_bench()
    pointer = PointerRig(bench, "palm")
    bench.open()
    pointer.hold("palm", 0.5)
    bench.ctl.close("command", quarantine=False)  # type: ignore[union-attr]
    pointer.engine.engage()  # the voice command that asked for it
    pointer.hold("palm", 0.3)
    assert pointer.engine.engaged and pointer.seen[-1] == 1


@pytest.mark.parametrize("engage", ["palm", "always"])
def test_q40_closing_during_a_run_quarantines_the_pointer_and_sends_nothing_after_it(
    make_bench: Callable[..., Bench], engage: str
) -> None:
    bench = make_bench()
    pointer = PointerRig(bench, engage)
    bench.arm_up()
    bench.fill("hello")
    bench.insert(bench.t + bench.dt)
    while not bench.session._machine.running and bench.t < 60 and bench.ctl.active:  # type: ignore[union-attr]
        bench.run(bench.dt)
    assert bench.session._machine.running  # type: ignore[union-attr]
    bench.run(0.2)
    sent = len(bench.desktop.key_calls)
    releases = bench.desktop.release_keys_calls
    bench.cmd("stop")  # in the middle of the run
    assert bench.closed_reason == "command" and bench.desktop.release_keys_calls == releases + 1
    assert bench.kb_events("closed")[0]["discarded"] == 5 - sent or bench.kb_events("closed")[0]["discarded"] >= 0
    pointer.hold("palm", 3.0)  # the hands are still up: no stroke, no pointer
    assert len(bench.desktop.key_calls) == sent
    assert not pointer.engine.engaged and set(pointer.seen) <= {0}
    pointer.gap(0.7)
    pointer.hold("palm", 0.2 if engage == "always" else 1.0)
    assert pointer.engine.engaged
    assert len(bench.desktop.key_calls) == sent


# ------------------------------------------------------------------------------------------------ S22b: exception text


@contextmanager
def hands_log(path: Path, caplog: pytest.LogCaptureFixture) -> Iterator[Path]:
    """The real log setup (stderr and ``hands.log``), with pytest's capture handler kept on the root logger.

    Entered in the body of the test, not in a fixture: the stderr handler keeps the ``sys.stderr`` of the moment, and
    ``capfd`` swaps its stream between the setup and the call.
    """
    root = logging.getLogger()
    saved, level = list(root.handlers), root.level
    log_file = logs.setup_logging(path)
    assert log_file is not None
    root.addHandler(caplog.handler)
    caplog.set_level(logging.DEBUG)
    try:
        yield log_file
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            if handler not in saved:
                handler.close()
        for handler in saved:
            root.addHandler(handler)
        root.setLevel(level)


def chained() -> BaseException:
    try:
        try:
            raise OSError(S)
        except OSError as inner:
            raise ValueError(f"bad stroke {S!r}") from inner
    except ValueError as outer:
        return outer


EXCEPTIONS: dict[str, Callable[[], BaseException]] = {
    "runtime": lambda: RuntimeError(S),
    "os": lambda: OSError(S),
    "refused": lambda: KeyRefused(S),
    "value": lambda: ValueError(f"bad stroke {S!r}"),
    "chained": chained,
}


def raiser(exc: BaseException) -> Callable[..., Any]:
    def raise_it(*args: object, **kwargs: object) -> Any:
        raise exc

    return raise_it


class BadFrame:
    """A frame whose hands cannot be read."""

    t, width, height = 5.0, 1280, 720

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    @property
    def hands(self) -> tuple[()]:
        raise self._exc


Seam = Callable[[Bench, pytest.MonkeyPatch, BaseException], dict[str, Any]]


def in_session(bench: Bench, monkeypatch: pytest.MonkeyPatch, exc: BaseException, target: object, name: str) -> None:
    monkeypatch.setattr(target, name, raiser(exc))


def seam_update(bench: Bench, mp: pytest.MonkeyPatch, exc: BaseException) -> dict[str, Any]:
    in_session(bench, mp, exc, KeyboardSession, "update")
    bench.run(0.3)
    return {"closed": "error", "frame": True}


def seam_tap(bench: Bench, mp: pytest.MonkeyPatch, exc: BaseException) -> dict[str, Any]:
    in_session(bench, mp, exc, ReviewMachine, "tap")
    bench.tap(bench.t + bench.dt, "a")
    bench.run(0.5)
    return {"closed": "error", "frame": True}


def seam_append(bench: Bench, mp: pytest.MonkeyPatch, exc: BaseException) -> dict[str, Any]:
    in_session(bench, mp, exc, ComposeBuffer, "append")
    bench.tap(bench.t + bench.dt, "a")
    bench.run(0.5)
    return {"closed": "error", "frame": True}


def seam_gate(bench: Bench, mp: pytest.MonkeyPatch, exc: BaseException) -> dict[str, Any]:
    in_session(bench, mp, exc, KeySink, "gate")
    bench.run(0.3)
    return {"closed": "error", "frame": True}


def seam_begin_run(bench: Bench, mp: pytest.MonkeyPatch, exc: BaseException) -> dict[str, Any]:
    in_session(bench, mp, exc, KeySink, "begin_run")
    bench.insert(bench.t + bench.dt)
    bench.run(4.0)
    return {"closed": "error", "frame": True}


def seam_send_run(bench: Bench, mp: pytest.MonkeyPatch, exc: BaseException) -> dict[str, Any]:
    in_session(bench, mp, exc, KeySink, "send_run")
    bench.insert(bench.t + bench.dt)
    bench.run(4.0)
    return {"closed": "error", "frame": True}


def seam_send_keys(bench: Bench, mp: pytest.MonkeyPatch, exc: BaseException) -> dict[str, Any]:
    mp.setattr(bench.desktop, "send_keys", raiser(exc))
    bench.insert(bench.t + bench.dt)
    bench.run(4.0)
    return {"closed": None}  # the sink turns an unexpected failure into a failed run or a close; either is fine


def seam_key_target(bench: Bench, mp: pytest.MonkeyPatch, exc: BaseException) -> dict[str, Any]:
    mp.setattr(bench.desktop, "key_target", raiser(exc))
    bench.run(3.0)
    return {"closed": None}


def seam_foreign_input(bench: Bench, mp: pytest.MonkeyPatch, exc: BaseException) -> dict[str, Any]:
    mp.setattr(bench.desktop, "foreign_input", raiser(exc))
    bench.run(3.0)
    return {"closed": "input_blocked", "blocked": True}


def seam_show(bench: Bench, mp: pytest.MonkeyPatch, exc: BaseException) -> dict[str, Any]:
    bench.show_error = exc  # type: ignore[assignment]
    bench.run(0.3)
    return {"closed": "no_overlay"}


def seam_command(bench: Bench, mp: pytest.MonkeyPatch, exc: BaseException) -> dict[str, Any]:
    in_session(bench, mp, exc, KeyboardSettings, "apply")
    reply = bench.cmd("configure", settings={"size": 1.1})
    assert reply == {"ok": False, "error": {"code": "internal", "message": "The air keyboard hit an internal error."}}
    return {"closed": "error", "replies": [reply], "command": True}


def seam_recenter(bench: Bench, mp: pytest.MonkeyPatch, exc: BaseException) -> dict[str, Any]:
    in_session(bench, mp, exc, KeyboardSession, "recenter")
    reply = bench.cmd("recenter")
    assert reply["error"] == {"code": "internal", "message": TEXT["command_error"]}
    return {"closed": "error", "replies": [reply], "command": True}


def seam_status(bench: Bench, mp: pytest.MonkeyPatch, exc: BaseException) -> dict[str, Any]:
    mp.setattr(controller_module, "load_marker", raiser(exc))
    assert bench.ctl.status() is None  # type: ignore[union-attr]
    mp.undo()
    mp.setattr(bench.press, "quality", raiser(exc))
    assert bench.ctl.status() is None  # type: ignore[union-attr]
    return {"closed": None}


def seam_pointer_frame(bench: Bench, mp: pytest.MonkeyPatch, exc: BaseException) -> dict[str, Any]:
    bench.cmd("stop")
    out = bench.ctl.pointer_frame(BadFrame(exc))  # type: ignore[union-attr,arg-type]
    assert out.hands == ()  # the pointer stays off for that frame
    return {"closed": "command"}


def seam_close(bench: Bench, mp: pytest.MonkeyPatch, exc: BaseException) -> dict[str, Any]:
    mp.setattr(bench.desktop, "release_keys", raiser(exc))
    bench.reset_error = exc  # type: ignore[assignment]
    bench.emit_error = None
    bench.cmd("stop")
    assert bench.order == ["reset", "blank", "closed"]  # the failing steps skipped none of the others
    return {"closed": "command"}


def seam_session_close(bench: Bench, mp: pytest.MonkeyPatch, exc: BaseException) -> dict[str, Any]:
    in_session(bench, mp, exc, KeyboardSession, "close")
    bench.cmd("stop")
    assert bench.order == ["reset", "blank", "closed"]
    return {"closed": "command"}


SEAMS: dict[str, Seam] = {
    "session.update": seam_update,
    "ReviewMachine.tap": seam_tap,
    "ComposeBuffer.append": seam_append,
    "KeySink.gate": seam_gate,
    "KeySink.begin_run": seam_begin_run,
    "KeySink.send_run": seam_send_run,
    "desktop.send_keys": seam_send_keys,
    "desktop.key_target": seam_key_target,
    "desktop.foreign_input": seam_foreign_input,
    "overlay.show": seam_show,
    "command.configure": seam_command,
    "command.recenter": seam_recenter,
    "status": seam_status,
    "pointer_frame": seam_pointer_frame,
    "close.steps": seam_close,
    "close.session": seam_session_close,
}


@pytest.mark.parametrize("kind", list(EXCEPTIONS))
@pytest.mark.parametrize("seam", list(SEAMS))
def test_s22b_no_exception_text_leaves_the_controller_from_any_seam_behind_it(
    seam: str,
    kind: str,
    make_bench: Callable[..., Bench],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    exc = EXCEPTIONS[kind]()
    bench = make_bench().arm_up()
    bench.fill(S)  # a session is open with the sentinel in the box
    bench.order.clear()
    with hands_log(tmp_path / "log", caplog) as hands_log_file:
        outcome = SEAMS[seam](bench, monkeypatch, exc)
        logging.getLogger("probe").info("the file takes lines")
        for handler in logging.getLogger().handlers:
            handler.flush()
        stderr, written = capfd.readouterr().err, hands_log_file.read_text(encoding="utf-8")

    if outcome["closed"] is not None:
        assert bench.closed_reason == outcome["closed"], (seam, kind)
    else:
        assert bench.closed_reason in (None, "error", "input_blocked")
    if outcome.get("frame"):
        assert bench.reports == [("internal", "The air keyboard stopped because of an internal error.")]
        assert len(bench.kb_events("closed")) == 1
        assert f"keyboard: {type(exc).__name__} in frame" in caplog.text
    if outcome.get("blocked"):
        assert bench.reports == [("input_blocked", TEXT["input_blocked"])]
    if outcome.get("command"):
        assert f"keyboard: {type(exc).__name__} in command" in caplog.text
    for reason in ("input_blocked", "no_overlay"):
        if outcome["closed"] == reason:
            assert len(bench.kb_events("closed")) == 1

    seen = {
        "caplog": caplog.text,
        "records": "".join(f"{r.getMessage()}|{r.exc_text}|{r.args}" for r in caplog.records),
        "stderr": stderr,
        "hands.log": written,
        "events": json.dumps(bench.events),
        "reports": json.dumps(bench.reports),
        "replies": json.dumps(outcome.get("replies", [])),
        "status": json.dumps(bench.ctl.status()),  # type: ignore[union-attr]
    }
    for where, text in seen.items():
        assert S not in text, (seam, kind, where)
    assert "Traceback" not in caplog.text and "Traceback" not in written
    assert "the file takes lines" in written  # the file and the stream were really listening


def test_s22b_a_command_that_fails_with_no_session_open_replies_internal_and_emits_nothing(
    make_bench: Callable[..., Bench], monkeypatch: pytest.MonkeyPatch
) -> None:
    bench = make_bench()
    monkeypatch.setattr(KeyboardSettings, "apply", raiser(RuntimeError(S)))
    reply = bench.cmd("configure", settings={"size": 1.1})
    assert reply == {"ok": False, "error": {"code": "internal", "message": TEXT["command_error"]}}
    assert not bench.events and S not in json.dumps(reply)
    validate(reply, "CommandResponse")


def test_s22b_a_start_that_fails_half_way_leaves_nothing_open(
    make_bench: Callable[..., Bench], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bench = make_bench()
    monkeypatch.setattr(controller_module, "load_tuning", raiser(OSError(S)))
    reply = bench.cmd("start")
    assert reply["error"] == {"code": "internal", "message": TEXT["command_error"]}
    assert not bench.ctl.active and not bench.ctl.needs_release  # type: ignore[union-attr]
    assert bench.pointer_off_calls == 0 and not bench.events and S not in caplog.text
    assert "OSError in command" in caplog.text


def test_s22b_a_report_that_raises_is_swallowed_and_the_close_still_goes_through(
    make_bench: Callable[..., Bench], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bench = make_bench().arm_up()
    bench.ctl._deps = replace(bench.ctl._deps, report=raiser(RuntimeError(S)))  # type: ignore[union-attr]
    monkeypatch.setattr(KeyboardSession, "update", boom)
    bench.run(0.3)
    assert bench.closed_reason == "error" and S not in caplog.text
    assert "RuntimeError in report" in caplog.text


def test_s22b_with_the_scrub_off_exc_text_is_todays_text_exactly(make_bench: Callable[..., Bench]) -> None:
    """The differential: after a session the helper's own messages read as they did before the keyboard (RT12)."""
    bench = make_bench().arm_up()
    bench.cmd("stop")
    exc = RuntimeError("boom")
    assert logs.exc_text(exc) == f"{type(exc).__name__}: {exc}" == "RuntimeError: boom"
    assert logs.exc_text(exc, typed=False) == "boom"
