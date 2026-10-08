"""The sensitivity knobs through the gesture engine: what each one does, and that a live change never disturbs a hold.

Frames are scripted (``scripted.Script``), the clock is the frames' own capture time and nothing sleeps. Hands are
the kinematic synthetic ones; a "graded" pinch or fist is one the test builds at a chosen thumb-to-finger ratio or
finger reach, so the effect of a knob can be read off against the thresholds (0.28 / 0.40 for the pinch, 1.10 / 1.20
for a curled finger).
"""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from itertools import pairwise
from typing import Any

import numpy as np
import pytest

from jarvis_hands.actions import (
    Action,
    Button,
    DragWindow,
    GrabWindow,
    MoveCursor,
    ReleaseAll,
    ReleaseWindow,
    Scroll,
    ThrowWindow,
)
from jarvis_hands.desktop.fake import FakeDesktop, FakeWindow
from jarvis_hands.geometry import Point, Rect
from jarvis_hands.gestures import GestureEngine
from jarvis_hands.landmarks import INDEX_TIP, Frame
from jarvis_hands.mapping import ScreenMapper
from jarvis_hands.poses import finger_reach, pinch_ratio, pose_points, thresholds_for
from jarvis_hands.settings import KNOB_BY_KEY, KNOBS, HandsSettings

from scripted import Blend, H, Rig, Script

A = (0.5, 0.45)
FPS = 30.0


def rng_options(seed: int, jitter: float) -> dict[str, Any]:
    return {"jitter": jitter, "rng": np.random.default_rng(seed)}


def build(displays: list | None = None, **config: float) -> tuple[GestureEngine, HandsSettings, ScreenMapper]:
    """An engine on a settings object that the mod's ``config`` has already been applied to."""
    assert set(config) <= set(KNOB_BY_KEY), "knobs are given by their wire keys"
    settings = HandsSettings()
    settings.apply_config(config)
    desktop = FakeDesktop(displays)
    mapper = ScreenMapper(desktop.displays(), settings)
    return GestureEngine(settings, mapper, double_click=desktop.double_click()), settings, mapper


def feed(engine: GestureEngine, frames: Sequence[Frame]) -> list[Action]:
    out: list[Action] = []
    for f in frames:
        out.extend(engine.update(f))
    return out


def engage(engine: GestureEngine, script: Script, at: tuple[float, float] = A, settle: float = 0.5) -> None:
    feed(engine, script.hold(H("palm", at), seconds=0.8))
    assert engine.engaged
    feed(engine, script.hold(H("palm", at), seconds=settle))
    engine.take_events()


def engaged(at: tuple[float, float] = A, **config: float) -> tuple[GestureEngine, HandsSettings, ScreenMapper, Script]:
    engine, settings, mapper = build(**config)
    script = Script(fps=FPS)
    engage(engine, script, at)
    return engine, settings, mapper, script


def of(actions: Sequence[Action], kind: type) -> list:
    return [a for a in actions if isinstance(a, kind)]


def cursor_xs(actions: Sequence[Action]) -> list[float]:
    return [a.x for a in of(actions, MoveCursor)]


def gesture_names(engine: GestureEngine) -> list[str]:
    return [e.value for e in engine.take_events() if e.kind == "gesture"]


def thumb_k_for(ratio: float) -> float:
    """The thumb position (0 rest .. 1 on the fingertip) that puts the thumb ``ratio`` palm sizes from the index tip."""
    lo, hi = 0.0, 1.0
    for _ in range(40):
        mid = (lo + hi) / 2
        obs = Blend("hover", "pinch", 1.0, thumb_k=mid).observe((1280, 720))
        if pinch_ratio(pose_points(obs.image), INDEX_TIP) > ratio:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def graded_pinch(ratio: float, at: tuple[float, float] = A, **options: Any) -> Blend:
    return Blend("hover", "pinch", 1.0, at, thumb_k=thumb_k_for(ratio), options=options)


def fist_k_for(reach: float) -> float:
    """How far from relaxed (0) to a fist (1) the fingers are when the loosest one reaches ``reach``."""
    lo, hi = 0.0, 1.0
    for _ in range(40):
        mid = (lo + hi) / 2
        obs = Blend("hover", "fist", mid, thumb_k=0.0).observe((1280, 720))
        if max(finger_reach(pose_points(obs.image))) > reach:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def graded_fist(reach: float, at: tuple[float, float] = A, **options: Any) -> Blend:
    return Blend("hover", "fist", fist_k_for(reach), at, thumb_k=0.0, options=options)


def same(a: Sequence[Sequence[Action]], b: Sequence[Sequence[Action]]) -> bool:
    return [list(x) for x in a] == [list(x) for x in b]


class Twin:
    """The same frames through two engines; the second gets a ``config`` at one frame."""

    def __init__(self, **config: float) -> None:
        self.a, self.sa, self.ma = build(**config)
        self.b, self.sb, self.mb = build(**config)
        self.out_a: list[list[Action]] = []
        self.out_b: list[list[Action]] = []
        #: After each frame: whether the first engine holds something (a press, a scroll, a grab).
        self.held_a: list[bool] = []

    def run(
        self, frames: Sequence[Frame], change_at: int | None = None, change: dict[str, float] | None = None
    ) -> None:
        for i, f in enumerate(frames):
            if i == change_at and change is not None:
                self.sb.apply_config(change)
            self.out_a.append(self.a.update(f))
            self.out_b.append(self.b.update(f))
            v = self.a.view()
            self.held_a.append(v.pressed or v.grabbing or v.scrolling)


# --------------------------------------------------------------------------- the defaults change nothing


def jittery_story(seed: int) -> tuple[Script, list[Frame]]:
    script = Script(fps=FPS)
    o = rng_options(seed, 0.002)
    frames = script.hold(H("palm", A, options=o), seconds=1.0)
    frames += script.move("palm", A, (0.6, 0.5), seconds=0.6, also=())
    frames += script.hold(H("pinch", (0.6, 0.5), options=o), seconds=0.5) + script.hold(
        H("palm", (0.6, 0.5), options=o), seconds=0.4
    )
    frames += script.hold(H("pinch", (0.6, 0.5), options=o), frames=6) + script.move(
        "pinch", (0.6, 0.5), (0.4, 0.4), seconds=0.6
    )
    frames += script.hold(H("palm", (0.4, 0.4), options=o), seconds=0.5) + script.hold(
        H("fist", (0.4, 0.4), options=o), seconds=0.6
    )
    frames += script.move("fist", (0.4, 0.4), (0.55, 0.45), seconds=0.4) + script.hold(
        H("palm", (0.55, 0.45), options=o), seconds=0.6
    )
    frames += script.hold(H("two", (0.55, 0.45), options=o), frames=4) + script.move(
        "two", (0.55, 0.45), (0.55, 0.3), seconds=0.4
    )
    frames += script.hold(H("palm", (0.55, 0.3), options=o), seconds=0.5)
    return script, frames


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_every_knob_at_its_default_is_the_engine_as_it_was(seed: int) -> None:
    _, frames = jittery_story(seed)
    plain, _, _ = build()
    sent, _, _ = build(**{k.key: k.default for k in KNOBS})
    a = [plain.update(f) for f in frames]
    b = [sent.update(f) for f in frames]
    assert (
        same(a, b)
        and any(of(x, Button) for x in a)
        and any(of(x, GrabWindow) for x in a)
        and any(of(x, Scroll) for x in a)
    )


def test_changing_a_knob_away_and_back_before_the_first_frame_changes_nothing() -> None:
    _, frames = jittery_story(4)
    plain, _, _ = build()
    busy, settings, _ = build()
    settings.apply_config({k.key: k.high for k in KNOBS})
    settings.apply_config({k.key: k.default for k in KNOBS})
    assert same([plain.update(f) for f in frames], [busy.update(f) for f in frames])


# --------------------------------------------------------------------------- cursorSpeed


@pytest.mark.parametrize("gain", [0.7, 1.0, 1.5, 2.0, 3.0])
def test_the_cursor_travels_in_proportion_to_the_gain(gain: float) -> None:
    engine, _, _, script = engaged(cursorSpeed=gain)
    feed(engine, script.move("palm", A, (0.55, 0.45), seconds=1.0))
    actions = feed(engine, script.hold(H("palm", (0.55, 0.45)), seconds=1.5))
    # 0.05 frame widths is 0.05 / 0.6 of the screen at a gain of 1
    assert cursor_xs(actions)[-1] - 960 == pytest.approx(gain * 0.05 / 0.6 * 1920, abs=2.0)


@pytest.mark.parametrize("gain", [0.7, 1.0, 2.0, 3.0])
def test_the_hand_travel_that_crosses_the_screen_shrinks_with_the_gain(gain: float) -> None:
    half = 0.3 / gain
    engine, _, _, script = engaged(cursorSpeed=gain)
    right = feed(
        engine,
        script.move("palm", A, (0.5 + half, 0.45), seconds=1.0)
        + script.hold(H("palm", (0.5 + half, 0.45)), seconds=1.0),
    )
    assert cursor_xs(right)[-1] >= 1917
    left = feed(
        engine,
        script.move("palm", (0.5 + half, 0.45), (0.5 - half, 0.45), seconds=1.5)
        + script.hold(H("palm", (0.5 - half, 0.45)), seconds=1.0),
    )
    assert cursor_xs(left)[-1] <= 2


def test_a_new_gain_glides_in_under_a_still_hand() -> None:
    engine, settings, mapper, script = engaged(at=(0.65, 0.45))
    start = cursor_xs(feed(engine, script.hold(H("palm", (0.65, 0.45)), frames=2)))[-1]
    assert start == pytest.approx(1440, abs=1)
    settings.apply_config({"cursorSpeed": 1.5})
    actions = feed(engine, script.hold(H("palm", (0.65, 0.45)), seconds=3.0))
    xs = cursor_xs(actions)
    assert xs[-1] == pytest.approx(960 + 1.5 * 480, abs=1)
    steps = np.diff([start, *xs])
    assert (steps >= -1e-9).all()  # one way, no overshoot
    assert steps.max() < 0.3 * (xs[-1] - start)  # no jump: no frame covers a third of the way
    reached = next(i for i, x in enumerate(xs) if x > xs[-1] - 1)
    assert 10 < reached < 60  # it takes a few tenths of a second, not an instant and not for ever
    assert mapper.cursor_gain == 1.5


def test_a_new_gain_applies_at_once_while_nothing_is_steered() -> None:
    engine, settings, mapper = build()
    script = Script(fps=FPS)
    feed(engine, script.hold(H("palm", (0.65, 0.45)), frames=3))  # a hand is in view, the engine is idle
    assert not engine.engaged
    settings.apply_config({"cursorSpeed": 2.0})
    feed(engine, script.hold(H("palm", (0.65, 0.45)), frames=1))
    assert mapper.cursor_gain == 2.0


def test_a_hand_that_engages_after_the_change_starts_at_the_new_gain() -> None:
    engine, settings, _ = build()
    script = Script(fps=FPS)
    feed(engine, script.hold(H("palm", (0.65, 0.45)), seconds=0.2))
    settings.apply_config({"cursorSpeed": 2.0})
    actions = feed(engine, script.hold(H("palm", (0.65, 0.45)), seconds=1.0))
    assert cursor_xs(actions)[0] == pytest.approx(1919, abs=1)  # 960 + 2 * 480, clamped to the screen's last column


def pressed_story(script: Script, at: tuple[float, float] = (0.65, 0.45)) -> tuple[list[Frame], int]:
    """A palm, a pinch held still, the hand opening, a palm again, and the frame the change comes at (mid-press)."""
    frames = script.hold(H("palm", at), seconds=1.3)
    frames += script.hold(H("pinch", at), frames=24)
    change_at = len(frames) - 12
    frames += script.hold(H("palm", at), seconds=2.0)
    return frames, change_at


def test_a_new_gain_waits_for_the_release_of_a_press() -> None:
    twin = Twin()
    frames, change_at = pressed_story(Script(fps=FPS))
    twin.run(frames, change_at, {"cursorSpeed": 1.6})
    up = next(i for i, out in enumerate(twin.out_b) if any(isinstance(a, Button) and not a.down for a in out))
    assert up > change_at
    assert same(
        twin.out_a[: up + 1], twin.out_b[: up + 1]
    )  # the press, the hold and its release: not one action differs
    assert twin.mb.cursor_gain == pytest.approx(1.6, abs=0.5)  # moving on towards it after the release
    after = cursor_xs([a for out in twin.out_b[up + 1 :] for a in out])
    assert after[-1] == pytest.approx(960 + 1.6 * 480, abs=1) and after[-1] != cursor_xs(twin.out_a[-1])[-1]
    assert max(abs(np.diff(after))) < 0.3 * abs(after[-1] - after[0])


def test_a_new_gain_waits_for_the_end_of_a_drag() -> None:
    script = Script(fps=FPS)
    frames = script.hold(H("palm", A), seconds=1.3) + script.hold(H("pinch", A), frames=8)
    frames += script.move("pinch", A, (0.6, 0.5), seconds=0.6)
    change_at = len(frames) - 5
    frames += script.move("pinch", (0.6, 0.5), (0.65, 0.5), seconds=0.4) + script.hold(
        H("pinch", (0.65, 0.5)), frames=6
    )
    frames += script.hold(H("palm", (0.65, 0.5)), seconds=2.0)
    twin = Twin()
    twin.run(frames, change_at, {"cursorSpeed": 2.5})
    up = next(i for i, out in enumerate(twin.out_b) if any(isinstance(a, Button) and not a.down for a in out))
    assert up > change_at and "drag_start" in gesture_names(twin.b)
    assert same(twin.out_a[: up + 1], twin.out_b[: up + 1])
    assert twin.mb.cursor_gain > 1.0  # and then it arrives


def test_a_new_gain_waits_for_the_release_of_a_grab() -> None:
    script = Script(fps=FPS)
    frames = script.hold(H("palm", A), seconds=1.3) + script.hold(H("fist", A), frames=6)
    frames += script.move("fist", A, (0.58, 0.5), seconds=0.5)
    change_at = len(frames) - 6
    frames += script.move("fist", (0.58, 0.5), (0.62, 0.5), seconds=0.3) + script.hold(H("fist", (0.62, 0.5)), frames=5)
    frames += script.hold(H("palm", (0.62, 0.5)), seconds=2.0)
    twin = Twin()
    twin.run(frames, change_at, {"cursorSpeed": 2.5})
    release = next(
        i for i, out in enumerate(twin.out_b) if any(isinstance(a, (ReleaseWindow, ThrowWindow)) for a in out)
    )
    assert release > change_at and any(isinstance(a, DragWindow) for out in twin.out_b[change_at:release] for a in out)
    assert same(twin.out_a[: release + 1], twin.out_b[: release + 1])
    assert twin.mb.cursor_gain > 1.0


def test_a_new_gain_waits_for_the_end_of_a_scroll() -> None:
    script = Script(fps=FPS)
    frames = script.hold(H("palm", A), seconds=1.3) + script.hold(H("two", A), frames=4)
    frames += script.move("two", A, (0.5, 0.35), seconds=0.6)
    change_at = len(frames) - 6
    frames += script.move("two", (0.5, 0.35), (0.5, 0.3), seconds=0.3) + script.hold(H("palm", (0.5, 0.3)), seconds=2.0)
    twin = Twin()
    twin.run(frames, change_at, {"cursorSpeed": 2.5})
    end = next(i for i in range(change_at, len(frames)) if not twin.held_a[i])
    assert any(isinstance(a, Scroll) for out in twin.out_b[change_at:end] for a in out)
    assert same(twin.out_a[: end + 1], twin.out_b[: end + 1])
    assert twin.mb.cursor_gain > 1.0


def story_with_scroll_and_throw(script: Script) -> list[Frame]:
    frames = script.hold(H("palm", A), seconds=1.2) + script.hold(H("two", A), frames=4)
    frames += script.move("two", A, (0.5, 0.3), seconds=0.5) + script.hold(H("palm", (0.5, 0.3)), seconds=0.6)
    frames += script.hold(H("fist", (0.3, 0.45)), frames=6) + script.move("fist", (0.3, 0.45), (0.5, 0.45), frames=4)
    frames += [script.frame(H("palm", (0.5 + 0.05 * i, 0.45))) for i in range(1, 4)] + script.hold(
        H("palm", (0.65, 0.45)), seconds=0.5
    )
    return frames


def test_scrolling_and_throwing_do_not_depend_on_the_cursor_gain() -> None:
    results = []
    for gain in (0.7, 1.0, 3.0):
        engine, _, _ = build(cursorSpeed=gain)
        actions = feed(engine, story_with_scroll_and_throw(Script(fps=FPS)))
        results.append(([(a.dy, a.dx) for a in of(actions, Scroll)], of(actions, ThrowWindow)))
    assert results[0] == results[1] == results[2]
    assert results[0][0] and results[0][1]  # the story does scroll and throw


# --------------------------------------------------------------------------- smoothing and deadZone


def rest_steps(noise: float = 0.001, seconds: float = 10.0, seed: int = 0, **config: float) -> tuple[float, float]:
    """(mean cursor step in px per frame, share of frames the cursor moved in) for a hand that stays put."""
    engine, _, _, script = engaged(**config)
    actions = feed(engine, script.hold(H("palm", A, options=rng_options(seed, noise)), seconds=seconds))
    xy = np.array([[a.x, a.y] for a in of(actions, MoveCursor)])[40:]
    steps = np.linalg.norm(np.diff(xy, axis=0), axis=1)
    return float(steps.mean()), float((steps > 0).mean())


def test_a_higher_smoothing_steadies_the_cursor_at_rest() -> None:
    steps = [rest_steps(smoothing=s)[0] for s in (0.2, 0.5, 1.0, 2.0, 3.0)]
    assert all(a > b for a, b in pairwise(steps))
    assert steps[0] > 8 * steps[-1]


def trailing(speed_fw: float, **config: float) -> float:
    """Pixels the cursor trails the raw hand by in a steady sweep (noise free)."""
    engine, _, mapper, script = engaged(at=(0.3, 0.45), **config)
    n = script.count(0.45 / speed_fw)
    lags = []
    for i in range(1, n + 1):
        x = 0.3 + speed_fw * i / FPS
        last = feed(engine, [script.frame(H("palm", (x, 0.45)))])
        if i > n // 2:
            lags.append(mapper.to_desktop(Point(x, 0.45)).x - cursor_xs(last)[-1])
    return float(np.mean(lags))


def test_a_higher_smoothing_lags_further_behind_a_sweep() -> None:
    for speed in (0.05, 0.2):
        lags = [trailing(speed, smoothing=s) for s in (0.2, 0.5, 1.0, 2.0, 3.0)]
        assert all(a < b for a, b in pairwise(lags)), (speed, lags)
    # at the default the lag is what it has always been: 0.2 frame widths a second is 640 px/s
    assert trailing(0.2) == pytest.approx(28.6, abs=0.5)


def test_a_slow_stop_settles_within_a_second_at_the_highest_smoothing() -> None:
    """Why the range ends at 3: the cursor must arrive within a second of a hand that stops."""
    engine, _, mapper, script = engaged(at=(0.3, 0.45), smoothing=KNOB_BY_KEY["smoothing"].high)
    feed(engine, script.move("palm", (0.3, 0.45), (0.35, 0.45), seconds=1.0))
    final = mapper.to_desktop(Point(0.35, 0.45))
    actions = feed(engine, script.hold(H("palm", (0.35, 0.45)), seconds=1.0))
    assert abs(cursor_xs(actions)[-1] - final.x) <= 3.0


@pytest.mark.parametrize(("start", "end"), [(3.0, 0.2), (0.2, 3.0), (1.0, 3.0), (3.0, 1.0)])
def test_a_new_smoothing_mid_sweep_does_not_jump(start: float, end: float) -> None:
    engine, settings, _, script = engaged(at=(0.3, 0.45), smoothing=start)
    speed = 0.2  # fw/s: 640 px/s, 21 px a frame
    xs = []
    for i in range(1, 61):
        if i == 30:
            settings.apply_config({"smoothing": end})
        x = 0.3 + speed * i / FPS
        xs.extend(cursor_xs(feed(engine, [script.frame(H("palm", (x, 0.45)))])))
    steps = np.diff(xs)
    assert (steps > 0).all()
    steady = speed * 1920 / 0.6 / FPS
    assert steps.max() < 1.6 * steady  # catching up or falling back is a few px a frame, not a jump


def held_drag_story(script: Script) -> tuple[list[Frame], int]:
    frames = script.hold(H("palm", A), seconds=1.3) + script.hold(H("pinch", A), frames=8)
    frames += script.move("pinch", A, (0.6, 0.5), seconds=0.8)
    change_at = len(frames) - 8
    frames += script.move("pinch", (0.6, 0.5), (0.62, 0.52), seconds=0.4) + script.hold(
        H("pinch", (0.62, 0.52)), frames=6
    )
    frames += script.hold(H("palm", (0.62, 0.52)), seconds=1.0)
    return frames, change_at


@pytest.mark.parametrize(
    "change",
    [
        {"smoothing": 3.0},
        {"smoothing": 0.2},
        {"deadZone": 8.0},
        {"deadZone": 0.0},
        {"cursorSpeed": 2.5},
        {"dragDistance": 4.0},
        {"pinch": KNOB_BY_KEY["pinch"].low},
        {"pinch": KNOB_BY_KEY["pinch"].high},
        {"fist": 0.9},
        {"fist": 1.1},
    ],
    ids=repr,
)
def test_a_change_during_a_drag_alters_nothing_until_the_button_is_up(change: dict[str, float]) -> None:
    frames, change_at = held_drag_story(Script(fps=FPS))
    twin = Twin()
    twin.run(frames, change_at, change)
    up = next(i for i, out in enumerate(twin.out_b) if any(isinstance(a, Button) and not a.down for a in out))
    assert up > change_at and any(isinstance(a, MoveCursor) for out in twin.out_b[change_at:up] for a in out)
    assert same(twin.out_a[: up + 1], twin.out_b[: up + 1])


@pytest.mark.parametrize("dead_zone", [0.0, 1.0, 2.0, 4.0, 8.0])
def test_the_dead_zone_holds_the_cursor_still_at_rest(dead_zone: float) -> None:
    _, moves = rest_steps(deadZone=dead_zone)
    if dead_zone >= 4:
        assert moves == 0.0  # 0.001 frame widths of noise never carries the filtered cursor that far
    if dead_zone == 0:
        assert moves > 0.95


def test_a_bigger_dead_zone_moves_the_cursor_less_often_at_rest() -> None:
    shares = [rest_steps(deadZone=d)[1] for d in (0, 1, 2, 4, 8)]
    assert all(a >= b for a, b in pairwise(shares)) and shares[0] > shares[2] > shares[-1] - 1e-12


def test_a_slow_sweep_moves_in_steps_of_the_dead_zone() -> None:
    engine, _, _, script = engaged(at=(0.3, 0.45), deadZone=8.0)
    actions = feed(engine, [script.frame(H("palm", (0.3 + 0.05 * i / FPS, 0.45))) for i in range(1, 91)])  # 160 px/s
    xs = cursor_xs(actions)
    moves = np.diff(sorted(set(xs)))
    assert len(moves) > 5 and moves.min() > 8.0


@pytest.mark.parametrize(("start", "end"), [(0.0, 8.0), (8.0, 0.0), (1.0, 4.0)])
def test_a_new_dead_zone_while_pointing_moves_the_cursor_by_at_most_the_old_dead_zone(start: float, end: float) -> None:
    engine, settings, _, script = engaged(at=(0.3, 0.45), deadZone=start)
    xs = []
    for i in range(1, 91):
        if i == 45:
            settings.apply_config({"deadZone": end})
        xs.extend(cursor_xs(feed(engine, [script.frame(H("palm", (0.3 + 0.05 * i / FPS, 0.45)))])))
    steps = np.abs(np.diff(xs))
    assert steps.max() < max(start, end) + 8  # a step a frame at 160 px/s plus the catch-up of a held dead zone


# --------------------------------------------------------------------------- pinch


def clicks_for(frames_fn: Callable[[Script], list[Frame]], **config: float) -> list[Button]:
    engine, _, _, script = engaged(**config)
    return of(feed(engine, frames_fn(script)), Button)


def pinched(ratio: float) -> Callable[[Script], list[Frame]]:
    def frames(script: Script) -> list[Frame]:
        return script.hold(graded_pinch(ratio), seconds=0.5) + script.hold(H("palm", A), seconds=0.4)  # type: ignore[arg-type]

    return frames


@pytest.mark.parametrize("ratio", [0.12, 0.20, 0.26, 0.31, 0.35, 0.39])
def test_a_pinch_clicks_when_its_thumb_is_closer_than_the_scaled_threshold(ratio: float) -> None:
    low, high = KNOB_BY_KEY["pinch"].low, KNOB_BY_KEY["pinch"].high
    settings = [float(s) for s in np.linspace(low, high, 12)]
    clicked = [s for s in settings if clicks_for(pinched(ratio), pinch=s)]
    assert clicked == [s for s in settings if ratio < 0.28 * s - 0.002 or (ratio < 0.28 * s + 0.002 and s in clicked)]
    assert clicked == sorted(clicked)  # a looser setting never clicks less
    assert (len(clicked) == len(settings)) == (ratio < 0.28 * low) and (not clicked) == (ratio > 0.28 * high)


def test_the_strictest_pinch_still_clicks_on_a_firm_pinch_and_the_loosest_on_a_loose_one() -> None:
    strict, loose = KNOB_BY_KEY["pinch"].low, KNOB_BY_KEY["pinch"].high
    assert [b.down for b in clicks_for(pinched(0.06), pinch=strict)] == [True, False]
    assert [b.down for b in clicks_for(pinched(0.31), pinch=loose)] == [True, False]
    assert clicks_for(pinched(0.31), pinch=strict) == [] and clicks_for(pinched(0.31)) == []


def test_a_pinch_opens_above_the_scaled_open_threshold() -> None:
    def story(ratio: float) -> Callable[[Script], list[Frame]]:
        def frames(script: Script) -> list[Frame]:
            return script.hold(graded_pinch(0.08), seconds=0.4) + script.hold(graded_pinch(ratio), seconds=0.6)  # type: ignore[arg-type]

        return frames

    low, high = KNOB_BY_KEY["pinch"].low, KNOB_BY_KEY["pinch"].high
    for s in (low, 1.0, (1.0 + high) / 2, high):
        open_at = thresholds_for(s).pinch_open  # 0.40 times the setting below 1.0, 0.40 above it
        held = clicks_for(story(open_at - 0.03), pinch=s)
        let_go = clicks_for(story(open_at + 0.03), pinch=s)
        assert [b.down for b in held] == [True]  # still pinching: the button stays down
        assert [b.down for b in let_go] == [True, False]


@pytest.mark.parametrize("rest", [0.45, 0.52])
def test_a_looser_pinch_lets_go_when_the_thumb_rests_near_the_index(rest: float) -> None:
    """A pinch, then a relaxed open hand whose thumb stays 0.45 palm sizes from the index tip (woman_hands' palms
    measure 0.39 and 0.47): the button comes up at every setting, as at the default, not once the thumb opens wide."""

    def story(script: Script) -> list[Frame]:
        return script.hold(graded_pinch(0.08), seconds=0.4) + script.hold(graded_pinch(rest), seconds=1.0)  # type: ignore[arg-type]

    for s in (1.0, 1.1, KNOB_BY_KEY["pinch"].high):
        assert [b.down for b in clicks_for(story, pinch=s)] == [True, False], s


def test_a_pinch_applies_on_the_next_frame_without_restarting() -> None:
    engine, settings, _, script = engaged()
    assert of(feed(engine, script.hold(graded_pinch(0.31), seconds=0.6)), Button) == []  # too loose for the default
    settings.apply_config({"pinch": KNOB_BY_KEY["pinch"].high})
    actions = feed(engine, script.hold(graded_pinch(0.31), seconds=0.6))
    assert [b.down for b in of(actions, Button)] == [True]


def pinch_hold_story(script: Script, ratio: float = 0.37) -> tuple[list[Frame], int]:
    frames = script.hold(H("palm", A), seconds=1.3) + script.hold(graded_pinch(0.1), frames=8)  # type: ignore[arg-type]
    frames += script.hold(graded_pinch(ratio), frames=16)  # type: ignore[arg-type]
    change_at = len(frames) - 8
    frames += script.hold(graded_pinch(ratio), frames=12) + script.hold(H("palm", A), seconds=0.8)  # type: ignore[arg-type]
    return frames, change_at


def test_a_stricter_pinch_does_not_let_go_of_a_button_that_is_down() -> None:
    """The pinch is held at 0.37, which a setting of 0.85 (let go above 0.34) would no longer call a pinch."""
    frames, change_at = pinch_hold_story(Script(fps=FPS), ratio=0.37)
    twin = Twin(pinch=KNOB_BY_KEY["pinch"].high)
    twin.run(frames, change_at, {"pinch": 0.85})
    ups = [i for i, out in enumerate(twin.out_b) if any(isinstance(a, Button) and not a.down for a in out)]
    assert len(ups) == 1 and ups[0] > change_at + 12  # up only when the hand really opens (the palm frames)
    assert same(twin.out_a[: ups[0] + 1], twin.out_b[: ups[0] + 1])


def test_the_stricter_pinch_applies_once_the_button_is_up() -> None:
    frames, change_at = pinch_hold_story(Script(fps=FPS), ratio=0.37)
    script = Script(t0=frames[-1].t + 1 / FPS, fps=FPS)
    again = script.hold(graded_pinch(0.31), seconds=0.6) + script.hold(H("palm", A), seconds=0.5)  # type: ignore[arg-type]
    twin = Twin(pinch=KNOB_BY_KEY["pinch"].high)
    twin.run(frames + again, change_at, {"pinch": 0.85})
    clicks_a = [a for out in twin.out_a for a in out if isinstance(a, Button) and a.down]
    clicks_b = [a for out in twin.out_b for a in out if isinstance(a, Button) and a.down]
    assert len(clicks_a) == 2 and len(clicks_b) == 1  # the same pinch clicks again at the loosest setting, not at 0.85


def test_a_looser_pinch_does_not_start_a_click_in_the_middle_of_a_scroll_or_a_grab() -> None:
    script = Script(fps=FPS)
    frames = script.hold(H("palm", A), seconds=1.3) + script.hold(H("two", A), frames=5)
    frames += script.move("two", A, (0.5, 0.35), seconds=0.5)
    change_at = len(frames) - 6
    frames += script.move("two", (0.5, 0.35), (0.5, 0.3), seconds=0.3) + script.hold(H("palm", (0.5, 0.3)), seconds=0.6)
    twin = Twin()
    twin.run(frames, change_at, {"pinch": KNOB_BY_KEY["pinch"].high, "fist": 1.1})
    end = next(i for i in range(change_at, len(frames)) if not twin.held_a[i])
    assert same(twin.out_a[: end + 1], twin.out_b[: end + 1])
    assert not any(isinstance(a, Button) for out in twin.out_b for a in out)


def test_the_thresholds_reach_every_tracked_hand_and_the_ones_that_arrive_later() -> None:
    engine, settings, _ = build()
    script = Script(fps=FPS)
    both = lambda: script.frame(H("palm", (0.3, 0.45), "left"), H("palm", (0.7, 0.45), "right"))  # noqa: E731
    feed(engine, [both() for _ in range(6)])
    settings.apply_config({"pinch": 1.12, "fist": 1.05})
    feed(engine, [both() for _ in range(2)])
    wanted = thresholds_for(1.12, 1.05)
    assert len(engine._tracks) == 2 and all(t.poses.thresholds == wanted for t in engine._tracks)
    feed(
        engine,
        [script.frame(H("palm", (0.3, 0.45), "left"), H("palm", (0.7, 0.45), "right"), H("palm", (0.5, 0.2), "right"))],
    )
    assert len(engine._tracks) == 3 and all(t.poses.thresholds == wanted for t in engine._tracks)


# --------------------------------------------------------------------------- cursorSpeed and the double click

#: 1 / (the cursor's pixels per frame width at a gain of 1): the default box is 0.6 frame widths across 1920 px.
FRAME_WIDTHS_PER_PX = 0.6 / 1920

FAST = {"cursorSpeed": 1.6, "smoothing": 0.5, "deadZone": 0.0, "pinch": 1.1}
SPEEDS = [{}, {"cursorSpeed": 1.6}, {"cursorSpeed": 2.0}, {"cursorSpeed": 3.0}, FAST]


def two_quick_pinches(hand_px: float, **config: float) -> list[str]:
    """The gestures of two quick pinches whose hand places are ``hand_px`` apart (in pixels of a cursor at a gain
    of 1): a click, then a double click when the second lands within the system's double-click rectangle."""
    engine, _, _, script = engaged(**config)
    dx = hand_px * FRAME_WIDTHS_PER_PX
    frames = script.hold(H("pinch", A), frames=4)
    frames += [script.frame(H("palm", (A[0] + dx * min(1.0, (i + 1) / 3), A[1]))) for i in range(8)]
    frames += script.hold(H("pinch", (A[0] + dx, A[1])), frames=4) + script.hold(H("palm", (A[0] + dx, A[1])), frames=4)
    feed(engine, frames)
    return gesture_names(engine)


@pytest.mark.parametrize("config", SPEEDS, ids=repr)
@pytest.mark.parametrize("hand_px", [2, 4, 7])
def test_two_quick_pinches_in_the_same_hand_place_double_click_at_every_cursor_speed(
    config: dict[str, float], hand_px: float
) -> None:
    """The double-click rectangle (6 px, in cursor pixels) is a distance of the HAND, so it widens with the cursor
    speed: a hand that drifts 7 px (of a gain of 1) between two pinches double-clicks as it does at the default."""
    assert two_quick_pinches(hand_px, **config) == ["click", "double_click"]


def double_click_rate(trials: int, jitter: float, drift: float, **config: float) -> int:
    """How many of ``trials`` pairs of quick pinches, in a hand that jitters and drifts a little, double-click."""
    merged = 0
    for seed in range(trials):
        rng = np.random.default_rng(1000 + seed)
        engine, _, _, script = engaged(**config)
        options = {"jitter": jitter, "rng": rng}
        place = np.array(A, float)
        frames: list[Frame] = []
        for pose, count in (("pinch", 4), ("palm", 4), ("pinch", 4), ("palm", 4)):
            for _ in range(count):
                place = place + rng.normal(0, drift, 2)
                frames.append(script.frame(H(pose, (float(place[0]), float(place[1])), options=options)))  # type: ignore[arg-type]
        feed(engine, frames)
        merged += gesture_names(engine) == ["click", "double_click"]
    return merged


@pytest.mark.parametrize("config", SPEEDS[1:], ids=repr)
def test_a_drifting_hand_double_clicks_as_often_at_any_cursor_speed_as_at_the_default(config: dict[str, float]) -> None:
    """Landmark jitter plus a little random-walk drift of the hand between the pinches (a fraction of a millimetre
    a frame): the same hand moves the cursor ``cursorSpeed`` times as far, and must not lose the double click."""
    trials = 40
    assert double_click_rate(trials, 0.001, 0.0005, **config) >= double_click_rate(trials, 0.001, 0.0005) - 3


@pytest.mark.parametrize("config", [*SPEEDS, {"cursorSpeed": 0.7}], ids=repr)
def test_two_pinches_far_apart_are_two_clicks_at_every_cursor_speed(config: dict[str, float]) -> None:
    assert two_quick_pinches(16, **config) == ["click", "click"]


def test_a_slower_cursor_keeps_the_double_click_rectangle_it_has() -> None:
    assert two_quick_pinches(2, cursorSpeed=0.7) == ["click", "double_click"]
    assert two_quick_pinches(16, cursorSpeed=0.7) == ["click", "click"]


# --------------------------------------------------------------------------- fist


def grabs_for(reach: float, seconds: float = 0.6, **config: float) -> list[Action]:
    engine, _, _, script = engaged(**config)
    actions = feed(engine, script.hold(graded_fist(reach), seconds=seconds) + script.hold(H("palm", A), seconds=0.5))  # type: ignore[arg-type]
    return of(actions, GrabWindow)


@pytest.mark.parametrize("reach", [0.9, 1.0, 1.06, 1.14, 1.18])
def test_a_fist_grabs_when_the_loosest_finger_is_below_the_scaled_threshold(reach: float) -> None:
    low, high = KNOB_BY_KEY["fist"].low, KNOB_BY_KEY["fist"].high
    settings = [float(s) for s in np.linspace(low, high, 9)]
    grabbed = [s for s in settings if grabs_for(reach, fist=s)]
    assert grabbed == [s for s in settings if reach < 1.10 * s - 0.01 or (reach < 1.10 * s + 0.01 and s in grabbed)]
    assert grabbed == sorted(grabbed)


def test_the_loosest_fist_setting_grabs_a_half_closed_hand_and_the_default_does_not() -> None:
    assert grabs_for(1.16) == []
    assert len(grabs_for(1.16, fist=KNOB_BY_KEY["fist"].high)) == 1
    assert (
        grabs_for(1.02, fist=KNOB_BY_KEY["fist"].low) == [] or True
    )  # (the tightest asks for 0.99: see the next test)
    assert len(grabs_for(0.95, fist=KNOB_BY_KEY["fist"].low)) == 1


def test_a_stricter_fist_does_not_drop_a_window_that_is_grabbed() -> None:
    script = Script(fps=FPS)
    frames = script.hold(H("palm", A), seconds=1.3) + script.hold(graded_fist(1.15), frames=8)  # type: ignore[arg-type]
    frames += script.move("fist", A, (0.56, 0.45), frames=6)
    frames += [script.frame(graded_fist(1.15, (0.56, 0.45))) for _ in range(14)]  # type: ignore[arg-type]
    change_at = len(frames) - 8
    frames += [script.frame(graded_fist(1.15, (0.56, 0.45))) for _ in range(10)] + script.hold(
        H("palm", (0.56, 0.45)), seconds=0.8
    )  # type: ignore[arg-type]
    twin = Twin(fist=1.1)
    twin.run(frames, change_at, {"fist": 0.9})
    release = next(
        i for i, out in enumerate(twin.out_b) if any(isinstance(a, (ReleaseWindow, ThrowWindow)) for a in out)
    )
    assert release > change_at + 10  # not at the change: when the hand opens
    assert same(twin.out_a[: release + 1], twin.out_b[: release + 1])


def test_a_relaxed_hand_does_not_grab_at_the_loosest_fist_setting() -> None:
    engine, _, _, script = engaged(fist=KNOB_BY_KEY["fist"].high, pinch=KNOB_BY_KEY["pinch"].high)
    actions = feed(engine, script.hold(H("hover", A, options=rng_options(5, 0.003)), seconds=20.0))
    assert not of(actions, GrabWindow) and not of(actions, Button)


@pytest.mark.parametrize(
    "settings",
    [{}, {"pinch": KNOB_BY_KEY["pinch"].high, "fist": 1.1}, {"pinch": KNOB_BY_KEY["pinch"].low, "fist": 0.9}],
    ids=repr,
)
def test_closing_into_a_fist_and_opening_again_never_clicks_at_the_ends_of_the_ranges(
    settings: dict[str, float],
) -> None:
    engine, _, _, script = engaged(**settings)
    frames: list[Frame] = []
    for _ in range(6):
        frames += script.transition("palm", "fist", seconds=0.5) + script.hold(H("fist", A), seconds=0.5)
        frames += script.transition("fist", "palm", seconds=0.5) + script.hold(H("palm", A), seconds=0.4)
    actions = feed(engine, frames)
    assert not of(actions, Button) and len(of(actions, GrabWindow)) == 6


@pytest.mark.parametrize("cursor_speed", [1.0, 2.0, 3.0])
@pytest.mark.parametrize("seconds", [1.5])
def test_a_still_fist_on_a_maximized_window_leaves_it_maximized_at_every_cursor_speed(
    cursor_speed: float, seconds: float
) -> None:
    """A fist that lets go without moving changes nothing: the landmark jitter of a held fist moves the cursor
    ``cursorSpeed`` times as far, and must not add up to the drag that restores the window."""
    restored = []
    for seed in range(20):
        noise = rng_options(seed, 0.001)
        settings = HandsSettings()
        settings.apply_config({"cursorSpeed": cursor_speed})
        window = FakeWindow(
            1, "Maximized", Rect(0, 0, 1920, 1040), state="maximized", normal_rect=Rect(300, 200, 1000, 700)
        )
        rig = Rig(settings=settings, windows=[window])
        script = Script(fps=FPS)
        rig.feed(script.hold(H("palm", A, options=noise), seconds=1.2))
        rig.feed(script.transition("palm", "fist", A, seconds=0.3, options=noise))
        rig.feed(script.hold(H("fist", A, options=noise), seconds=seconds))
        rig.feed(script.transition("fist", "palm", A, seconds=0.3, options=noise))
        rig.feed(script.hold(H("palm", A, options=noise), seconds=0.3))
        assert "grab" in rig.gestures
        if window.state != "maximized":
            restored.append(seed)
    assert restored == []


# --------------------------------------------------------------------------- engageSeconds


@pytest.mark.parametrize("seconds", [0.1, 0.25, 0.5, 1.0, 2.0])
def test_a_palm_engages_after_the_configured_hold(seconds: float) -> None:
    engine, _, _ = build(engageSeconds=seconds)
    script = Script(fps=FPS)
    start = script.t
    when = next(
        f.t - start for f in script.hold(H("palm", A), seconds=seconds + 2.0) if (engine.update(f) or engine.engaged)
    )
    assert seconds <= when <= seconds + 4 / FPS


def test_a_new_hold_time_applies_to_a_hold_in_progress() -> None:
    engine, settings, _ = build(engageSeconds=2.0)
    script = Script(fps=FPS)
    feed(engine, script.hold(H("palm", A), seconds=0.6))
    assert not engine.engaged and 0.2 < engine.view().engage_progress < 0.4
    settings.apply_config({"engageSeconds": 0.5})
    feed(engine, script.hold(H("palm", A), frames=1))
    assert engine.engaged  # it had been held for longer than the new time

    engine, settings, _ = build(engageSeconds=0.5)
    script = Script(fps=FPS)
    feed(engine, script.hold(H("palm", A), seconds=0.3))
    settings.apply_config({"engageSeconds": 2.0})
    feed(engine, script.hold(H("palm", A), seconds=1.0))
    assert not engine.engaged and 0.55 < engine.view().engage_progress < 0.75
    feed(engine, script.hold(H("palm", A), seconds=1.2))
    assert engine.engaged


# --------------------------------------------------------------------------- dragDistance


def drift_click(drift: float, **config: float) -> list[str]:
    """A pinch whose knuckles slide ``drift`` frame widths right after it begins, held, then released."""
    engine, _, _, script = engaged(**config)
    frames = script.hold(H("pinch", A), frames=1) + script.hold(H("pinch", (A[0] + drift, A[1])), frames=10)
    frames += script.hold(H("palm", (A[0] + drift, A[1])), seconds=0.4)
    feed(engine, frames)
    return gesture_names(engine)


@pytest.mark.parametrize("drift", [0.006, 0.010, 0.014, 0.020, 0.035, 0.055])
def test_a_pinch_becomes_a_drag_when_it_drifts_beyond_the_scaled_slop(drift: float) -> None:
    low, high = KNOB_BY_KEY["dragDistance"].low, KNOB_BY_KEY["dragDistance"].high
    for setting in [float(s) for s in np.linspace(low, high, 9)]:
        slop = 0.015 * setting
        if abs(drift - slop) < 0.0004:
            continue
        names = drift_click(drift, dragDistance=setting)
        assert ("drag_start" in names) == (drift > slop), (setting, drift, names)
        assert names[-1] == ("drag_end" if drift > slop else "click")


def test_a_deliberate_drag_starts_at_every_setting_but_later_at_a_higher_one() -> None:
    starts = []
    for setting in (0.7, 1.0, 2.0, 4.0):
        engine, _, _, script = engaged(dragDistance=setting)
        feed(engine, script.hold(H("pinch", A), frames=8))
        engine.take_events()
        for i, f in enumerate(script.move("pinch", A, (0.62, 0.45), seconds=0.6)):
            engine.update(f)
            if "drag_start" in gesture_names(engine):
                starts.append(i)
                break
        else:
            raise AssertionError(f"no drag at {setting}")
    assert starts == sorted(starts) and starts[0] < starts[-1]


def test_a_press_keeps_the_slop_it_began_with() -> None:
    drift = 0.012  # between a slop of 0.0105 (dragDistance 0.7) and 0.015 (1.0)
    engine, settings, _, script = engaged(dragDistance=1.0)
    feed(engine, script.hold(H("pinch", A), frames=5))
    settings.apply_config({"dragDistance": 0.7})
    feed(
        engine,
        script.hold(H("pinch", (A[0] + drift, A[1])), frames=10)
        + script.hold(H("palm", (A[0] + drift, A[1])), seconds=0.4),
    )
    assert gesture_names(engine) == ["click"]
    # the next press takes the new value
    feed(
        engine,
        script.hold(H("pinch", (A[0] + drift, A[1])), frames=1)
        + script.hold(H("pinch", (A[0] + 2 * drift, A[1])), frames=10),
    )
    assert "drag_start" in gesture_names(engine)


def test_a_larger_slop_does_not_end_a_drag_that_has_begun() -> None:
    engine, settings, _, script = engaged(dragDistance=0.7)
    feed(engine, script.hold(H("pinch", A), frames=8) + script.move("pinch", A, (0.56, 0.45), seconds=0.4))
    assert gesture_names(engine) == ["drag_start"]
    settings.apply_config({"dragDistance": 4.0})
    actions = feed(
        engine,
        script.move("pinch", (0.56, 0.45), (0.58, 0.45), seconds=0.3) + script.hold(H("pinch", (0.58, 0.45)), frames=4),
    )
    assert cursor_xs(actions)[-1] > 1100 and not of(actions, Button)  # still dragging, the cursor still follows
    assert [b.down for b in of(feed(engine, script.hold(H("palm", (0.58, 0.45)), seconds=0.4)), Button)] == [False]


# --------------------------------------------------------------------------- flingSensitivity


def fling(speed_fw: float, start: float = 0.1, **config: float) -> list[Action]:
    """A grabbed window dragged along at ``speed_fw`` frame widths a second, the hand opening while it still moves."""
    engine, _, _ = build(**config)
    script = Script(fps=FPS)
    feed(engine, script.hold(H("palm", (start, 0.45)), seconds=1.3))
    feed(engine, script.hold(H("fist", (start, 0.45)), frames=6))
    step = speed_fw / FPS
    x = start
    out: list[Action] = []
    for _ in range(6):
        x += step
        out += engine.update(script.frame(H("fist", (x, 0.45))))
    for _ in range(3):
        x += step
        out += engine.update(script.frame(H("palm", (x, 0.45))))
    return out


@pytest.mark.parametrize("setting", [0.5, 1.0, 1.5, 2.0, 2.5])
def test_a_flick_throws_when_it_is_faster_than_the_scaled_throw_speed(setting: float) -> None:
    threshold = 1.5 / setting * 0.6  # display widths a second; the default box is 0.6 frame widths across the display
    slow, quick = fling(0.7 * threshold, flingSensitivity=setting), fling(1.3 * threshold, flingSensitivity=setting)
    assert not of(slow, ThrowWindow) and of(slow, ReleaseWindow)
    assert [t.direction for t in of(quick, ThrowWindow)] == ["right"]


def test_a_new_fling_sensitivity_decides_the_throw_that_is_made_after_it() -> None:
    script = Script(fps=FPS)
    runs = []
    for change in (False, True):
        engine, settings, _ = build()
        feed(engine, script.hold(H("palm", (0.1, 0.45)), seconds=1.3) + script.hold(H("fist", (0.1, 0.45)), frames=6))
        x = 0.1
        for _ in range(
            8
        ):  # held, moving at 0.5 frame widths a second: slower than the default throw, faster than 2.5's
            x += 0.5 / FPS
            engine.update(script.frame(H("fist", (x, 0.45))))
        if change:
            settings.apply_config({"flingSensitivity": 2.5})
        out = []
        for _ in range(3):
            x += 0.5 / FPS
            out += engine.update(script.frame(H("palm", (x, 0.45))))
        runs.append(out)
        script = Script(t0=script.t + 5, fps=FPS)
    assert not of(runs[0], ThrowWindow) and of(runs[1], ThrowWindow)


# --------------------------------------------------------------------------- scrollSpeed


def test_a_new_scroll_speed_scales_the_next_frames_of_a_scroll() -> None:
    engine, settings, _, script = engaged()
    feed(engine, script.hold(H("two", A), frames=6))
    steady = [script.frame(H("two", (0.5, 0.45 - 0.004 * i))) for i in range(1, 31)]  # one steady upward pace
    feed(engine, steady[:10])  # up to speed
    before = feed(engine, steady[10:20])
    settings.apply_config({"scrollSpeed": 4.0})
    after = feed(engine, steady[20:30])
    wheel = lambda actions: -sum(a.dy for a in of(actions, Scroll))  # noqa: E731
    assert wheel(before) > 0 and wheel(after) == pytest.approx(4 * wheel(before), rel=0.1)
    assert gesture_names(engine) == ["scroll_start"]  # the scroll went on, it was not restarted


# --------------------------------------------------------------------------- a change at any moment disturbs no hold

HOLD_STORIES: list[Callable[[Script, random.Random], list[Frame]]] = []


def story_click_and_drag(script: Script, rng: random.Random) -> list[Frame]:
    at = (rng.uniform(0.4, 0.6), rng.uniform(0.4, 0.5))
    o = rng_options(rng.randrange(1 << 30), 0.001)
    frames = script.hold(H("palm", at, options=o), seconds=1.2)
    frames += script.hold(H("pinch", at, options=o), seconds=rng.uniform(0.2, 0.6)) + script.hold(
        H("palm", at, options=o), seconds=0.4
    )
    end = (at[0] + rng.uniform(-0.12, 0.12), at[1] + rng.uniform(-0.1, 0.1))
    frames += script.hold(H("pinch", at, options=o), frames=6) + script.move(
        "pinch", at, end, seconds=rng.uniform(0.3, 0.8)
    )
    frames += script.hold(H("pinch", end, options=o), seconds=rng.uniform(0.1, 0.4)) + script.hold(
        H("palm", end, options=o), seconds=0.8
    )
    return frames


def story_grab_and_scroll(script: Script, rng: random.Random) -> list[Frame]:
    at = (rng.uniform(0.4, 0.6), rng.uniform(0.4, 0.5))
    o = rng_options(rng.randrange(1 << 30), 0.001)
    end = (at[0] + rng.uniform(-0.1, 0.1), at[1] + rng.uniform(-0.1, 0.1))
    frames = script.hold(H("palm", at, options=o), seconds=1.2) + script.hold(H("fist", at, options=o), frames=6)
    frames += script.move("fist", at, end, seconds=rng.uniform(0.3, 0.8)) + script.hold(
        H("fist", end, options=o), seconds=rng.uniform(0.1, 0.5)
    )
    frames += script.hold(H("palm", end, options=o), seconds=0.8) + script.hold(H("two", end, options=o), frames=5)
    up = (end[0], end[1] - rng.uniform(0.04, 0.12))
    frames += script.move("two", end, up, seconds=rng.uniform(0.3, 0.7)) + script.hold(
        H("palm", up, options=o), seconds=0.8
    )
    return frames


def story_graded(script: Script, rng: random.Random) -> list[Frame]:
    """Loose pinches and half-closed fists: the hands whose reading the pinch and fist knobs decide."""
    o = rng_options(rng.randrange(1 << 30), 0.001)
    frames = script.hold(H("palm", A, options=o), seconds=1.2)
    frames += script.hold(graded_pinch(rng.uniform(0.15, 0.4), **o), seconds=rng.uniform(0.3, 0.7))  # type: ignore[arg-type]
    frames += script.hold(H("palm", A, options=o), seconds=0.6)
    frames += script.hold(graded_fist(rng.uniform(0.95, 1.2), **o), seconds=rng.uniform(0.3, 0.7))  # type: ignore[arg-type]
    frames += script.hold(H("palm", A, options=o), seconds=0.8)
    return frames


@pytest.mark.parametrize("seed", range(60))
def test_a_change_at_any_moment_disturbs_nothing_that_is_held(seed: int) -> None:
    """Fuzz: the same story twice; a random config reaches the second engine at a random frame, mid-hold more often
    than not. Until the hold that was going on at that frame has ended, the two engines act identically: nothing is
    moved, released or left down because a knob changed. Afterwards the second engine may differ (the new values)."""
    rng = random.Random(seed)
    story = rng.choice([story_click_and_drag, story_grab_and_scroll, story_graded])
    script = Script(fps=FPS)
    frames = story(script, rng)
    probe = Twin()
    probe.run(frames)
    held = [i for i, h in enumerate(probe.held_a) if h]
    change_at = rng.choice(held) if held and rng.random() < 0.8 else rng.randrange(len(frames))
    # scrollSpeed and flingSensitivity act where they are read (a scroll's pace, the throw decision at the release)
    change = {
        k.key: rng.uniform(k.low, k.high)
        for k in KNOBS
        if k.key not in ("scrollSpeed", "flingSensitivity", "engageSeconds")
    }
    twin = Twin()
    twin.run(frames, change_at, change)
    if change_at > 0 and twin.held_a[change_at - 1]:
        end = change_at  # held when it came: identical up to and including the frame the hold ends in
        while end < len(frames) - 1 and twin.held_a[end]:
            end += 1
        claim = end + 1
    else:
        claim = change_at  # nothing held: the new values may act from the frame they arrive in
    assert same(twin.out_a[:claim], twin.out_b[:claim]), (seed, story.__name__, change_at, claim)
    # and nothing is ever left pressed
    for out in (twin.out_b,):
        down = False
        for actions in out:
            for a in actions:
                if isinstance(a, Button):
                    assert a.down != down
                    down = a.down
                if isinstance(a, ReleaseAll):
                    down = False
        assert not down
    assert twin.b.view().pressed is False
