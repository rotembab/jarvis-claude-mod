"""The pointer path is what it was: P7 and P45 (DESIGN-KEYBOARD.md SR17, 6.4).

Each story of ``tests/test_gestures.py`` (click, drag, scroll, grab, fling, resize, calibration, engage always and
palm) is played twice with the very same frames, to the very same times: through ``HandsRuntime._process`` on its real
loop thread with the keyboard closed, and through a bare ``GestureEngine`` and ``Executor`` (``scripted.Rig``) that has
never heard of a keyboard. What the runtime hands the executor, frame by frame, must be the list the bare engine
produced, and the runtime's two side calls on the engine's view (the tracker's hand count and the display's keep-awake)
must be what that view asks for. P45 plays them again after a keyboard session was opened and closed and the
quarantine of 2.11 has lifted: a closed keyboard leaves nothing behind.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from test_gestures import PRESS, QUAD, fling
from test_kb_runtime import A, AwakeDesktop, Lockstep

from jarvis_hands.actions import Action
from jarvis_hands.desktop.fake import FakeWindow
from jarvis_hands.geometry import Rect
from jarvis_hands.landmarks import Frame
from jarvis_hands.settings import HandsSettings

from scripted import H, Rig, Script, camera_point

NOTES = (1, "Notes", Rect(560, 290, 800, 500))


class Live:
    """The runtime on its loop thread: what ``_process`` submits, what it asks of the tracker and the display."""

    def __init__(self, directory: Path, settings: dict[str, Any], windows: bool) -> None:
        self.desktop = AwakeDesktop(windows=[FakeWindow(*NOTES)] if windows else None)
        self.item = Lockstep(directory, desktop=self.desktop)
        self.item.start()
        if settings:
            assert self.item.command("config", settings) == {"ok": True}
        self.batches: list[list[Action]] = []
        self._events = 0
        executor = self.item.runtime._executor
        assert executor is not None
        submit = executor.submit

        def spy(actions: list[Action], now: float | None = None) -> None:
            if threading.current_thread().name == "hands-loop":  # a command's own releases are not the loop's
                self.batches.append(list(actions))
            submit(actions, now)

        executor.submit = spy  # type: ignore[method-assign]

    def mark(self) -> float:
        """The story starts here: what happened before (a session opened and closed) is not compared."""
        self.batches.clear()
        self._events = len(self.item.writer.snapshot())
        self._awake = len(self.desktop.awake)
        self._hands = len(self.item.tracker.num_hands_history)
        return self.item.script().t

    @property
    def gestures(self) -> list[str]:
        return [e["name"] for e in self.item.writer.snapshot()[self._events :] if e["type"] == "gesture"]

    @property
    def awake(self) -> list[bool]:
        return [on for on, _ in self.desktop.awake[self._awake :]]

    @property
    def hands(self) -> list[int]:
        return self.item.tracker.num_hands_history[self._hands :]


class Bare:
    """The bare engine and executor of ``scripted.Rig``, the frames of one story, and what the view asked for."""

    def __init__(self, settings: dict[str, Any], windows: bool) -> None:
        self.rig = Rig(windows=[FakeWindow(*NOTES)] if windows else None, settings=HandsSettings(**settings))
        self.batches: list[list[Action]] = []
        self.grabbing: list[bool] = []
        self.busy: list[bool] = []
        self.last_t = 0.0
        #: How many hands the tracker was asked for: a tracker reports no more (the runtime asks for two while a grab
        #: is on, and for one otherwise).
        self._asked = 1

    def feed(self, frames: list[Frame]) -> None:
        for frame in frames:
            seen = Frame(frame.t, frame.hands[: self._asked], frame.width, frame.height)
            self.batches.append(list(self.rig.feed([seen])))
            view = self.rig.engine.view()
            self._asked = 2 if view.grabbing else 1
            self.grabbing.append(view.grabbing)
            self.busy.append(self.rig.engine.engaged or view.state == "calibrating")
            self.last_t = frame.t

    @property
    def gestures(self) -> list[str]:
        return self.rig.gestures

    @staticmethod
    def changes(wanted: list[Any], start: Any) -> list[Any]:
        """The values a setter that ignores no-change calls is given, for what is wanted after each frame."""
        out, last = [], start
        for value in wanted:
            if value != last:
                out.append(value)
                last = value
        return out

    @property
    def hands(self) -> list[int]:
        return self.changes([2 if g else 1 for g in self.grabbing], 1)

    @property
    def awake(self) -> list[bool]:
        return self.changes(self.busy, False)


class Player:
    """One story's view of a backend: frames in, and the two commands the stories use."""

    def __init__(self, backend: Live | Bare, t0: float) -> None:
        self.backend, self.script = backend, Script(t0=t0)

    def feed(self, frames: list[Frame]) -> None:
        if isinstance(self.backend, Live):
            self.backend.item.feed(frames)
        else:
            self.backend.feed(frames)

    def engage(self) -> None:
        """The ``engage`` command."""
        if isinstance(self.backend, Live):
            assert self.backend.item.command("engage") == {"ok": True}
        else:
            self.backend.rig.engine.engage()

    def calibrate(self) -> None:
        """``calibrate start`` after a frame: the engine starts it on the time of the last frame."""
        if isinstance(self.backend, Live):
            assert self.backend.item.command("calibrate", {"action": "start"}) == {"ok": True}
        else:
            self.backend.rig.engine.start_calibration(self.backend.last_t)


# ------------------------------------------------------------------------------------------------------------ stories


def engaged(p: Player, at: tuple[float, float] = A) -> None:
    p.feed(p.script.hold(H("palm", at), seconds=0.8))
    p.feed(p.script.hold(H("palm", at), seconds=0.5))


def click(p: Player) -> None:
    engaged(p)
    p.feed(p.script.hold(H("pinch", A), frames=PRESS))
    p.feed(p.script.hold(H("palm", A), frames=4))
    p.feed(p.script.hold(H("pinch", A), frames=PRESS) + p.script.hold(H("palm", A), frames=2))  # and again: a double
    p.feed(p.script.hold(H("two", A), frames=PRESS) + p.script.hold(H("palm", A), seconds=0.5))  # a right click


def drag(p: Player) -> None:
    engaged(p)
    p.feed(p.script.hold(H("pinch", A), frames=PRESS))
    p.feed(p.script.move("pinch", A, (0.55, 0.45), frames=10))
    p.feed(p.script.hold(H("pinch", (0.55, 0.45)), seconds=1.0))
    p.feed(p.script.move("palm", (0.55, 0.45), (0.56, 0.45), frames=3))


def scroll(p: Player) -> None:
    engaged(p)
    end = (0.5, 0.40)
    p.feed(p.script.hold(H("two", A), frames=3))
    p.feed(p.script.move("two", A, end, seconds=0.5))
    p.feed(p.script.hold(H("two", end), seconds=1.5))
    p.feed(p.script.hold(H("palm", end), frames=3))


def grab(p: Player) -> None:
    p.feed(p.script.hold(H("palm", A), seconds=1.0))
    p.feed(p.script.hold(H("fist", A), frames=3))
    p.feed(p.script.move("fist", A, (0.55, 0.50), seconds=0.5))
    p.feed(p.script.hold(H("fist", (0.55, 0.50)), seconds=1.0))
    p.feed(p.script.hold(H("palm", (0.55, 0.50)), frames=3))


def throw(p: Player) -> None:
    engaged(p)
    p.feed(p.script.hold(H("fist", A), seconds=0.3))
    p.feed(fling(p.script, A, (0.12, 0.0)))
    p.feed(p.script.hold(H("palm", A), seconds=0.5))


def resize(p: Player) -> None:
    left = (0.4, 0.45)
    p.feed(p.script.hold(H("palm", A), seconds=1.0))
    p.feed(p.script.hold(H("fist", A), frames=3))
    p.feed(p.script.hold(H("fist", A), H("fist", left, "left"), frames=3))
    for i in range(1, 16):  # both hands pull apart
        k = i / 15
        p.feed([p.script.frame(H("fist", (0.5 + 0.05 * k, 0.45)), H("fist", (0.4 - 0.05 * k, 0.45), "left"))])
    p.feed(p.script.hold(H("fist", (0.55, 0.45)), H("palm", (0.35, 0.45), "left"), frames=6))


def calibration(p: Player) -> None:
    p.feed(p.script.hold(H("palm", A), seconds=0.2))  # the runtime starts a calibration once a frame has been seen
    p.calibrate()
    previous = (0.5, 0.5)
    for corner in QUAD:
        p.feed(p.script.move("palm", previous, corner, frames=4))
        p.feed(p.script.hold(H("palm", corner), seconds=1.3))
        previous = corner
    engaged(p, QUAD[0])  # the new mapping drives the cursor
    p.feed(p.script.move("palm", QUAD[0], A, frames=10))


def engage_always(p: Player) -> None:
    p.feed(p.script.hold(H("pinch", A), frames=10))  # arrives pinching: no click
    p.feed(p.script.hold(H("palm", A), frames=3) + p.script.hold(H("pinch", A), frames=PRESS))
    p.feed(p.script.hold(H("palm", camera_point(0.25, 0.5)), seconds=0.5))


def engage_palm(p: Player) -> None:
    p.feed(p.script.hold(H("hover", A), seconds=0.5))  # not an engaging pose
    p.feed(p.script.hold(H("palm", (0.7, 0.45), "right"), seconds=1.0))  # the right hand is not the one that points
    p.feed(p.script.hold(H("palm", (0.3, 0.45), "left"), seconds=1.0))
    p.feed(p.script.gap(seconds=1.7))  # the hand is gone for good: it disengages


def engage_command(p: Player) -> None:
    p.feed(p.script.hold(H("palm", A), frames=2))
    p.engage()
    p.feed(p.script.hold(H("pinch", A), frames=PRESS) + p.script.hold(H("palm", A), frames=4))


@dataclass(frozen=True)
class Story:
    play: Callable[[Player], None]
    settings: dict[str, Any]
    windows: bool = False

    def __repr__(self) -> str:
        return self.play.__name__


STORIES = {
    "click": Story(click, {}),
    "drag": Story(drag, {}),
    "scroll": Story(scroll, {}),
    "grab": Story(grab, {}, windows=True),
    "throw": Story(throw, {}, windows=True),
    "resize": Story(resize, {}, windows=True),
    "calibration": Story(calibration, {}),
    "engage_always": Story(engage_always, {"engage": "always"}),
    "engage_palm": Story(engage_palm, {"hand": "left"}),
    "engage_command": Story(engage_command, {}),
}


# --------------------------------------------------------------------------------------------------------- the proofs


@pytest.fixture
def live(tmp_path: Path) -> Iterator[Callable[[str], Live]]:
    made: list[Live] = []

    def make(name: str) -> Live:
        story = STORIES[name]
        item = Live(tmp_path / name, story.settings, story.windows)
        made.append(item)
        return item

    yield make
    for item in made:
        item.item.stop()
        assert not item.desktop.buttons_down, "a mouse button was left down"


def play_both(live: Live, name: str) -> Bare:
    t0 = live.mark()
    story = STORIES[name]
    story.play(Player(live, t0))
    bare = Bare(story.settings, story.windows)
    story.play(Player(bare, t0))
    return bare


def same(live: Live, bare: Bare) -> None:
    assert any(bare.batches), "the story did nothing"
    assert len(live.batches) == len(bare.batches)
    assert live.batches == bare.batches
    assert live.gestures == bare.gestures and live.hands == bare.hands and live.awake == bare.awake


@pytest.mark.parametrize("name", list(STORIES))
def test_p7_with_the_keyboard_closed_the_runtime_submits_what_the_bare_engine_produces(
    live: Callable[[str], Live], name: str
) -> None:
    item = live(name)
    bare = play_both(item, name)
    same(item, bare)
    runtime = item.item.runtime
    assert runtime._kb._session is None and not runtime._kb.active and item.item.writer.keyboard() == []


@pytest.mark.parametrize("name", list(STORIES))
def test_p45_after_a_session_was_opened_and_closed_and_the_quarantine_lifted_it_is_the_same(
    live: Callable[[str], Live], name: str
) -> None:
    item = live(name)
    run = item.item
    run.enable("pinch", "review")
    assert run.kb("start") == {"ok": True}
    run.hold(0.4, H("palm", A))  # a hand over the keys: the pointer is off for all of it
    assert run.kb("stop") == {"ok": True}
    run.gap(0.8)  # QUARANTINE_CLEAR_S of frames with no hand
    assert run.writer.closed() == ["command"]
    bare = play_both(item, name)
    same(item, bare)
    assert not run.runtime._kb.active
