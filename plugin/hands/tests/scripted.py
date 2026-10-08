"""Scripted frame sequences and an engine + executor rig, so gesture tests read like stories.

The hands themselves come from the kinematic model in ``jarvis_hands.synthetic``
(also used by ``run --fake``); its names are re-exported here, so tests keep
importing everything from ``scripted``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from jarvis_hands.actions import Action
from jarvis_hands.desktop.base import Display
from jarvis_hands.desktop.fake import FakeDesktop, FakeWindow
from jarvis_hands.executor import Executor
from jarvis_hands.geometry import Rect
from jarvis_hands.gestures import EngineEvent, GestureEngine
from jarvis_hands.landmarks import Frame, HandObservation
from jarvis_hands.mapping import ScreenMapper
from jarvis_hands.settings import HandsSettings
from jarvis_hands.synthetic import (
    CURLED,
    HEIGHT,
    PINCHING,
    POSES,
    RELAXED,
    SCALE,
    STRAIGHT,
    WIDTH,
    SyntheticPose,
    between,
    hand,
    world_landmarks,
)

__all__ = [
    "CURLED",
    "HEIGHT",
    "PINCHING",
    "POSES",
    "RELAXED",
    "SCALE",
    "STRAIGHT",
    "WIDTH",
    "Blend",
    "H",
    "Rig",
    "Script",
    "SyntheticPose",
    "between",
    "camera_point",
    "display",
    "gestures",
    "hand",
    "run",
    "world_landmarks",
]


@dataclass(frozen=True)
class H:
    """A hand in a scripted frame: a pose at a camera point (see ``hand``)."""

    pose: SyntheticPose = "palm"
    at: tuple[float, float] = (0.5, 0.45)
    handedness: Literal["left", "right"] = "right"
    options: dict[str, Any] = field(default_factory=dict)

    def observe(self, size: tuple[int, int]) -> HandObservation:
        return hand(self.pose, self.at, handedness=self.handedness, size=size, **self.options)


@dataclass(frozen=True)
class Blend:
    """A hand part way between two poses (``k`` of the way from ``start`` to ``end``; see ``between``)."""

    start: SyntheticPose
    end: SyntheticPose
    k: float
    at: tuple[float, float] = (0.5, 0.45)
    handedness: Literal["left", "right"] = "right"
    thumb_k: float | None = None
    #: More ``hand`` keywords (say ``jitter`` and ``rng``), as for ``H``.
    options: dict[str, Any] = field(default_factory=dict)

    def observe(self, size: tuple[int, int]) -> HandObservation:
        world = between(self.start, self.end, self.k, self.handedness, thumb_k=self.thumb_k)
        return hand(self.end, self.at, handedness=self.handedness, size=size, world=world, **self.options)


class Script:
    """Frames at a fixed rate: each call returns the next frames and advances the clock."""

    def __init__(self, t0: float = 100.0, fps: float = 30.0, width: int = WIDTH, height: int = HEIGHT) -> None:
        self.t = t0
        self.fps = fps
        self.width = width
        self.height = height

    @property
    def dt(self) -> float:
        return 1.0 / self.fps

    def frame(self, *hands: H | Blend) -> Frame:
        f = Frame(self.t, tuple(h.observe((self.width, self.height)) for h in hands), self.width, self.height)
        self.t += self.dt
        return f

    def count(self, seconds: float) -> int:
        return max(1, round(seconds * self.fps))

    def hold(self, *hands: H, seconds: float | None = None, frames: int | None = None) -> list[Frame]:
        n = frames if frames is not None else self.count(seconds or 0.0)
        return [self.frame(*hands) for _ in range(n)]

    def gap(self, seconds: float | None = None, frames: int | None = None) -> list[Frame]:
        """Frames with no hand in view."""
        return self.hold(seconds=seconds, frames=frames)

    def move(
        self,
        pose: SyntheticPose,
        start: tuple[float, float],
        end: tuple[float, float],
        *,
        seconds: float | None = None,
        frames: int | None = None,
        handedness: Literal["left", "right"] = "right",
        also: Sequence[H] = (),
    ) -> list[Frame]:
        """Moves one hand in a straight line from ``start`` (exclusive) to ``end`` (inclusive)."""
        n = frames if frames is not None else self.count(seconds or 0.0)
        out = []
        for i in range(1, n + 1):
            k = i / n
            at = (start[0] + (end[0] - start[0]) * k, start[1] + (end[1] - start[1]) * k)
            out.append(self.frame(H(pose, at, handedness), *also))
        return out

    def transition(
        self,
        start: SyntheticPose,
        end: SyntheticPose,
        at: tuple[float, float] = (0.5, 0.45),
        *,
        seconds: float | None = None,
        frames: int | None = None,
        thumb: Callable[[float], float] | None = None,
        handedness: Literal["left", "right"] = "right",
        options: dict[str, Any] | None = None,
    ) -> list[Frame]:
        """One hand changing from ``start`` (exclusive) to ``end`` (inclusive) in place, as a real hand does:
        every joint moves at once. ``thumb`` maps the fingers' progress to the thumb's (it may lead or lag);
        ``options`` are more ``hand`` keywords for every frame (say ``jitter`` and ``rng``)."""
        n = frames if frames is not None else self.count(seconds or 0.0)
        out = []
        for i in range(1, n + 1):
            k = i / n
            thumb_k = None if thumb is None else thumb(k)
            out.append(self.frame(Blend(start, end, k, at, handedness, thumb_k, dict(options or {}))))
        return out


def run(engine: GestureEngine, frames: Iterable[Frame]) -> list[Action]:
    actions: list[Action] = []
    for f in frames:
        actions.extend(engine.update(f))
    return actions


def gestures(events: Iterable[EngineEvent]) -> list[str]:
    return [e.value for e in events if e.kind == "gesture"]


def camera_point(
    u: float, v: float, box: tuple[float, float, float, float] = (0.2, 0.2, 0.8, 0.7)
) -> tuple[float, float]:
    """The camera point the default box maps to unit coordinates (u, v)."""
    x0, y0, x1, y1 = box
    return x0 + u * (x1 - x0), y0 + v * (y1 - y0)


def display(
    display_id: int,
    x: float,
    y: float,
    width: float,
    height: float,
    *,
    taskbar: float = 40,
    primary: bool = False,
    virtual: bool = False,
    name: str | None = None,
) -> Display:
    rect = Rect(x, y, width, height)
    return Display(
        display_id, name or rf"\\.\DISPLAY{display_id}", rect, Rect(x, y, width, height - taskbar), primary, virtual
    )


class Rig:
    """Engine + executor + FakeDesktop wired like the runtime, on a scripted clock."""

    def __init__(
        self,
        displays: list[Display] | None = None,
        windows: list[FakeWindow] | None = None,
        settings: HandsSettings | None = None,
        *,
        ticks_per_frame: int = 4,
    ) -> None:
        self.desktop = FakeDesktop(displays, windows)
        self.settings = settings or HandsSettings()
        self.mapper = ScreenMapper(self.desktop.displays(), self.settings)
        self.engine = GestureEngine(self.settings, self.mapper, double_click=self.desktop.double_click())
        self.errors: list[tuple[str, str]] = []
        self.executor = Executor(
            self.desktop,
            displays=lambda: self.mapper.used,
            cursor_gain=lambda: self.mapper.cursor_gain,
            on_user_input=self._user_input,
            on_error=lambda code, message: self.errors.append((code, message)),
        )
        self.ticks_per_frame = ticks_per_frame
        self.now = 0.0
        self.actions: list[Action] = []
        self.events: list[EngineEvent] = []

    def _user_input(self) -> None:
        self.executor.submit(self.engine.on_user_input(), now=self.now)

    def feed(self, frames: Iterable[Frame]) -> list[Action]:
        out: list[Action] = []
        for f in frames:
            self.now = f.t
            actions = self.engine.update(f)
            out.extend(actions)
            self.executor.submit(actions, now=f.t)
            for k in range(self.ticks_per_frame):
                self.now = f.t + k / (30.0 * self.ticks_per_frame)
                self.executor.tick(self.now)
            self.events.extend(self.engine.take_events())
        self.actions.extend(out)
        return out

    def settle(self, seconds: float = 0.1) -> None:
        """Ticks the executor on without new frames (finishes the cursor glide)."""
        end = self.now + seconds
        while self.now < end:
            self.now += 1 / 120
            self.executor.tick(self.now)

    @property
    def gestures(self) -> list[str]:
        return gestures(self.events)
