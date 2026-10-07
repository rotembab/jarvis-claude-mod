"""The gesture engine, as stories: scripted hands in, actions and events out.

One 1920 x 1080 display unless a test says otherwise; the default box maps
the camera point (0.5, 0.45) to the centre of the screen (960, 540), and
0.01 of the frame's width is 32 px sideways (0.01 of its height 21.6 px).

A scripted hand jumps from one pose to the next, so the other fingers jump
too: a pinch straight after a palm presses once the settle window holds only
pinch frames (``SETTLE_WINDOW_S``, the fourth frame at 30 fps), and one let go
before that clicks as it ends (a tap). Hence ``PRESS`` frames for a press.
"""

from __future__ import annotations

import itertools
import math

import numpy as np
import pytest

from jarvis_hands.actions import (
    Action,
    Button,
    GrabWindow,
    MoveCursor,
    ReleaseAll,
    ReleaseWindow,
    ResizeWindow,
    Scroll,
    ThrowWindow,
)
from jarvis_hands.calibration import CORNER_TARGETS, CORNERS
from jarvis_hands.desktop.fake import FakeWindow
from jarvis_hands.geometry import Point, Rect, apply_homography
from jarvis_hands.gestures import EngineEvent, GestureEngine
from jarvis_hands.mapping import ScreenMapper
from jarvis_hands.settings import HandsSettings

from scripted import H, Rig, Script, camera_point, display, gestures, run

A = (0.5, 0.45)
CENTRE = Point(960, 540)
PRIMARY = display(1, 0, 0, 1920, 1080, primary=True)
PROJECTOR = display(2, 1920, 0, 1280, 720)
VIRTUAL = display(3, 3200, 0, 1920, 1080, virtual=True)
#: Frames of a pinch after a palm until its button is down (see the module docstring).
PRESS = 4


def of(actions: list[Action], kind: type) -> list:
    return [a for a in actions if isinstance(a, kind)]


def engage(engine: GestureEngine, script: Script, at: tuple[float, float] = A, settle: float = 0.5) -> list[Action]:
    """Holds a still palm until engaged, then lets the filter settle."""
    actions = run(engine, script.hold(H("palm", at), seconds=0.8))
    assert engine.engaged
    actions += run(engine, script.hold(H("palm", at), seconds=settle))
    engine.take_events()
    return actions


def btn(button: str, down: bool, x: float = 960, y: float = 540) -> Button:
    return Button(button, down, pytest.approx(x), pytest.approx(y))  # type: ignore[arg-type]


def near(p: Point | Action, q: Point, tol: float = 1.0) -> bool:
    return abs(p.x - q.x) <= tol and abs(p.y - q.y) <= tol  # type: ignore[union-attr]


def fling(script: Script, start: tuple[float, float], delta: tuple[float, float], frames: int = 3) -> list:
    """A fist sweeping by ``delta`` over ``frames`` frames that opens while still moving."""
    end = (start[0] + delta[0], start[1] + delta[1])
    out = script.move("fist", start, end, frames=frames)
    step = (delta[0] / frames, delta[1] / frames)
    for i in range(1, 4):
        out.append(script.frame(H("palm", (end[0] + step[0] * i, end[1] + step[1] * i))))
    return out


# -- engagement -------------------------------------------------------------------------


def test_a_still_open_palm_engages_after_half_a_second(engine: GestureEngine, script: Script) -> None:
    actions = run(engine, script.hold(H("palm", A), frames=12))
    assert not engine.engaged and actions == []
    assert 0.5 < engine.view().engage_progress < 1.0
    assert engine.view().state == "idle"
    actions = run(engine, script.hold(H("palm", A), frames=6))
    assert engine.engaged
    assert actions[0] == MoveCursor(pytest.approx(960), pytest.approx(540))
    assert engine.take_events() == [EngineEvent("gesture", "engage"), EngineEvent("state", "active")]
    assert engine.state == "active" and engine.view().engage_progress == 1.0


def test_a_moving_palm_does_not_engage(engine: GestureEngine, script: Script) -> None:
    run(engine, script.move("palm", (0.3, 0.45), (0.8, 0.45), seconds=1.5))  # 0.33 frame widths per second
    assert not engine.engaged
    run(engine, script.hold(H("palm", (0.8, 0.45)), seconds=0.6))
    assert engine.engaged


@pytest.mark.parametrize("pose", ["fist", "pinch", "two", "hover"])
def test_other_poses_do_not_engage(engine: GestureEngine, script: Script, pose: str) -> None:
    assert run(engine, script.hold(H(pose, A), seconds=2.0)) == []  # type: ignore[arg-type]
    assert not engine.engaged


def test_the_faint_ring_follows_a_visible_hand_while_disengaged(engine: GestureEngine, script: Script) -> None:
    assert engine.view().cursor is None and not engine.view().hand_visible
    run(engine, script.hold(H("hover", camera_point(0.25, 0.5)), frames=40))
    view = engine.view()
    assert view.hand_visible and view.state == "idle" and view.pose == "hover"
    assert near(view.cursor, Point(480, 540), 2)  # type: ignore[arg-type]
    run(engine, script.gap(frames=2))
    assert engine.view().cursor is None and not engine.view().hand_visible


def test_engage_always_follows_any_hand_at_once(settings: HandsSettings, engine: GestureEngine, script: Script) -> None:
    settings.engage = "always"
    actions = run(engine, [script.frame(H("hover", A))])
    assert engine.engaged and actions == [MoveCursor(pytest.approx(960), pytest.approx(540))]


def test_a_hand_that_arrives_pinching_does_not_click(
    settings: HandsSettings, engine: GestureEngine, script: Script
) -> None:
    settings.engage = "always"
    actions = run(engine, script.hold(H("pinch", A), frames=10))
    assert engine.engaged and of(actions, Button) == []
    actions = run(engine, script.hold(H("palm", A), frames=3) + script.hold(H("pinch", A), frames=PRESS))
    assert [b.down for b in of(actions, Button)] == [True]


def test_hand_preference(settings: HandsSettings, engine: GestureEngine, script: Script) -> None:
    settings.hand = "left"
    run(engine, script.hold(H("palm", A, "right"), seconds=1.0))
    assert not engine.engaged
    run(engine, script.hold(H("palm", (0.3, 0.45), "left"), H("palm", (0.7, 0.45), "right"), seconds=0.7))
    assert engine.engaged
    assert near(engine.view().cursor, Point(320, 540), 2)  # type: ignore[arg-type]


def test_voice_engage_takes_the_next_hand_at_once(engine: GestureEngine, script: Script) -> None:
    engine.engage()
    assert not engine.engaged
    run(engine, script.gap(frames=3))
    actions = run(engine, script.hold(H("fist", A), frames=5))  # the fist is latched: no grab
    assert engine.engaged and of(actions, GrabWindow) == []
    assert gestures(engine.take_events()) == ["engage"]


def test_time_out_of_view_does_not_count_towards_engaging(engine: GestureEngine, script: Script) -> None:
    run(engine, script.hold(H("palm", A), frames=4))  # a palm passes by for 0.13 s
    run(engine, script.gap(seconds=0.6))
    run(engine, script.hold(H("palm", (0.6, 0.5)), frames=2))  # back somewhere else
    assert not engine.engaged and engine.view().engage_progress < 0.2
    run(engine, script.hold(H("palm", (0.6, 0.5)), seconds=0.6))
    assert engine.engaged


def test_a_hand_waved_through_the_view_twice_does_not_engage(engine: GestureEngine, script: Script) -> None:
    run(engine, script.move("palm", (0.2, 0.45), (0.25, 0.45), frames=4))
    run(engine, script.gap(seconds=0.5))
    run(engine, script.move("palm", (0.3, 0.45), (0.33, 0.45), frames=2))
    assert not engine.engaged


def test_missed_frames_keep_the_hold_but_do_not_count_towards_it(engine: GestureEngine, script: Script) -> None:
    run(engine, script.hold(H("palm", A), frames=9))  # 0.3 s
    run(engine, script.gap(frames=4))  # a few missed detections
    run(engine, script.hold(H("palm", A), frames=4))  # 0.53 s since the palm came, 0.4 s of it in view
    assert not engine.engaged and 0.7 < engine.view().engage_progress < 0.9
    run(engine, script.hold(H("palm", A), frames=4))
    assert engine.engaged


@pytest.mark.parametrize("flicker", ["hover", "pinch"])
def test_voice_engage_while_a_fist_flickers_does_not_grab(engine: GestureEngine, script: Script, flicker: str) -> None:
    run(engine, script.hold(H("fist", A), frames=5))  # a confirmed fist while idle
    engine.engage()
    actions = run(engine, [script.frame(H(flicker, A))])  # type: ignore[arg-type]  # misread just then
    actions += run(engine, script.hold(H("fist", A), frames=5))
    assert engine.engaged and of(actions, GrabWindow) == [] and of(actions, Button) == []
    actions = run(engine, script.hold(H("palm", A), frames=3) + script.hold(H("fist", A), frames=3))
    assert len(of(actions, GrabWindow)) == 1  # once opened, a fist grabs


def test_disengage_command_releases_what_is_held(engine: GestureEngine, script: Script) -> None:
    assert engine.disengage() == []
    engage(engine, script)
    run(engine, script.hold(H("pinch", A), frames=3))
    assert engine.disengage() == [ReleaseAll()]
    assert not engine.engaged
    assert engine.take_events()[-2:] == [EngineEvent("gesture", "disengage"), EngineEvent("state", "idle")]
    # The next frame repeats the release once (for a batch that raced the command), then nothing.
    assert run(engine, script.hold(H("pinch", A), frames=5)) == [ReleaseAll()]


def test_a_disengage_that_races_a_press_still_lets_go() -> None:
    rig = Rig()
    rig.feed(Script().hold(H("palm", A), seconds=1.0))
    script = Script(t0=rig.now + 1 / 30)
    for _ in range(PRESS):
        f = script.frame(H("pinch", A))
        batch = rig.engine.update(f)  # the engine thread presses...
        if of(batch, Button):
            break
        rig.executor.submit(batch, now=f.t)
    assert [b.down for b in of(batch, Button)] == [True]
    rig.executor.submit(rig.engine.disengage(), now=f.t)  # ...a spoken "disengage" is submitted first...
    rig.executor.submit(batch, now=f.t)  # ...and the press lands after it
    rig.feed(script.hold(H("pinch", A), frames=2))
    assert rig.desktop.buttons_down == set() and not rig.engine.engaged


# -- two hands in view ------------------------------------------------------------------------


def test_a_missed_pointer_frame_does_not_hand_the_cursor_to_the_other_hand(
    engine: GestureEngine, script: Script
) -> None:
    right, left = (0.55, 0.45), (0.40, 0.45)  # 0.15 frame widths apart
    engage(engine, script, right)
    run(engine, script.hold(H("palm", right), H("hover", left, "left"), seconds=0.5))
    assert near(engine.view().cursor, Point(1120, 540), 2)  # type: ignore[arg-type]
    run(engine, [script.frame(H("hover", left, "left"))])  # the tracker misses the pointer hand once
    run(engine, script.hold(H("palm", right), H("hover", left, "left"), seconds=0.5))
    assert near(engine.view().cursor, Point(1120, 540), 2)  # type: ignore[arg-type]


def test_the_pointer_keeps_its_hand_passing_where_the_other_hand_left(engine: GestureEngine, script: Script) -> None:
    engage(engine, script, (0.3, 0.45))
    run(engine, script.hold(H("palm", (0.3, 0.45)), H("hover", (0.6, 0.45), "left"), seconds=0.5))
    run(engine, script.hold(H("palm", (0.3, 0.45)), seconds=0.5))  # the other hand leaves; its track lingers
    run(engine, script.move("palm", (0.3, 0.45), (0.6, 0.45), seconds=0.5))  # over the spot it left
    run(engine, script.hold(H("palm", (0.6, 0.45)), seconds=0.5))
    assert engine.engaged and near(engine.view().cursor, Point(1280, 540), 2)  # type: ignore[arg-type]


# -- pointing and clicking ----------------------------------------------------------------


def test_the_cursor_follows_the_hand(engine: GestureEngine, script: Script) -> None:
    engage(engine, script)
    actions = run(engine, script.move("palm", A, (0.65, 0.45), seconds=0.5))
    xs = [a.x for a in of(actions, MoveCursor)]
    assert len(xs) == 15 and xs == sorted(xs) and xs[-1] > 1300
    actions = run(engine, script.hold(H("palm", (0.65, 0.45)), seconds=1.5))
    assert near(actions[-1], Point(1440, 540), 1.0)


def test_a_click_lands_where_the_cursor_was_before_the_pinch(engine: GestureEngine, script: Script) -> None:
    engage(engine, script)
    # Closing the pinch drags the knuckles a little to the right (within the slop).
    frames = [script.frame(H("pinch", (0.5 + 0.002 * k, 0.45))) for k in range(1, 6)]
    frames += script.hold(H("palm", (0.51, 0.45)), frames=4)
    actions = run(engine, frames)
    buttons = of(actions, Button)
    assert buttons == [btn("left", True), btn("left", False)]
    down, up = actions.index(buttons[0]), actions.index(buttons[1])
    assert of(actions[down:up], MoveCursor) == []  # frozen: no drift while pressed
    assert gestures(engine.take_events()) == ["click"]


def test_pinch_and_move_drags(engine: GestureEngine, script: Script) -> None:
    engage(engine, script)
    actions = run(engine, script.hold(H("pinch", A), frames=PRESS))
    assert of(actions, Button) == [btn("left", True)]
    actions = run(engine, script.move("pinch", A, (0.55, 0.45), frames=10))
    assert gestures(engine.take_events()) == ["drag_start"]
    assert of(actions, Button) == [] and of(actions, MoveCursor)[-1].x > 1050
    run(engine, script.hold(H("pinch", (0.55, 0.45)), seconds=1.0))
    # The hand opens while drifting on: the drop happens where the pinch was, not where the opening hand went.
    actions = run(engine, script.move("palm", (0.55, 0.45), (0.56, 0.45), frames=3))
    (up,) = of(actions, Button)
    assert not up.down and near(up, Point(1120, 540), 1.0)
    assert gestures(engine.take_events()) == ["drag_end"]


def test_no_drag_within_the_slop(engine: GestureEngine, script: Script) -> None:
    engage(engine, script)
    run(engine, script.hold(H("pinch", A), frames=3))
    run(engine, script.move("pinch", A, (0.512, 0.45), frames=8))
    assert gestures(engine.take_events()) == []
    actions = run(engine, script.hold(H("palm", (0.512, 0.45)), frames=2))
    assert of(actions, Button) == [btn("left", False)]


def test_two_quick_pinches_double_click_at_the_first_point(engine: GestureEngine, script: Script) -> None:
    engage(engine, script)
    second = (0.5012, 0.45)  # about 4 px to the right
    frames = script.hold(H("pinch", A), frames=3) + script.hold(H("palm", A), frames=2)
    frames += script.hold(H("palm", second), frames=2) + script.hold(H("pinch", second), frames=3)
    frames += script.hold(H("palm", second), frames=3)
    buttons = of(run(engine, frames), Button)
    assert buttons == [btn("left", True), btn("left", False)] * 2
    assert gestures(engine.take_events()) == ["click", "double_click"]


def test_a_slow_second_pinch_is_another_click(engine: GestureEngine, script: Script) -> None:
    engage(engine, script)
    frames = script.hold(H("pinch", A), frames=3) + script.hold(H("palm", A), seconds=0.6)
    frames += script.hold(H("pinch", A), frames=3) + script.hold(H("palm", A), frames=3)
    run(engine, frames)
    assert gestures(engine.take_events()) == ["click", "click"]


def test_a_distant_second_pinch_is_another_click(engine: GestureEngine, script: Script) -> None:
    engage(engine, script)
    frames = script.hold(H("pinch", A), frames=3) + script.hold(H("palm", A), frames=2)
    frames += script.move("palm", A, (0.52, 0.45), frames=6) + script.hold(H("palm", (0.52, 0.45)), frames=3)
    frames += script.hold(H("pinch", (0.52, 0.45)), frames=3)
    frames += script.hold(H("palm", (0.52, 0.45)), frames=3)
    buttons = of(run(engine, frames), Button)
    assert buttons[2].x > 990
    assert gestures(engine.take_events()) == ["click", "click"]


def test_three_quick_pinches_are_a_double_click_then_a_click(engine: GestureEngine, script: Script) -> None:
    engage(engine, script)
    one = script.hold(H("pinch", A), frames=3) + script.hold(H("palm", A), frames=3)
    run(engine, one + script.hold(H("pinch", A), frames=3) + script.hold(H("palm", A), frames=3) + one)
    assert gestures(engine.take_events()) == ["click", "double_click", "click"]


def test_middle_pinch_right_clicks(engine: GestureEngine, script: Script) -> None:
    engage(engine, script)
    actions = run(engine, script.hold(H("pinch_middle", A), frames=4) + script.hold(H("palm", A), frames=3))
    assert of(actions, Button) == [btn("right", True), btn("right", False)]
    assert gestures(engine.take_events()) == ["right_click"]


def test_a_one_frame_flicker_is_not_a_pose(engine: GestureEngine, script: Script) -> None:
    engage(engine, script)
    frames = []
    for _ in range(5):
        frames += [script.frame(H("pinch", A)), script.frame(H("palm", A))]
    assert of(run(engine, frames), Button) == []


def test_confirm_frames_one_acts_at_once(settings: HandsSettings, engine: GestureEngine, script: Script) -> None:
    """A fist grabs on its first frame. (A pinch's button also waits for the hand to settle into it.)"""
    settings.confirm_frames = 1
    engage(engine, script)
    assert of(run(engine, [script.frame(H("fist", A))]), GrabWindow) == [
        GrabWindow(pytest.approx(960), pytest.approx(540))
    ]


# -- scrolling ----------------------------------------------------------------------------


def scroll_story(engine: GestureEngine, script: Script, end: tuple[float, float]) -> list[Action]:
    engage(engine, script)
    actions = run(engine, script.hold(H("two", A), frames=3))
    actions += run(engine, script.move("two", A, end, seconds=0.5))
    actions += run(engine, script.hold(H("two", end), seconds=1.5))
    return actions


def test_moving_two_fingers_up_scrolls_the_content_up(engine: GestureEngine, script: Script) -> None:
    actions = scroll_story(engine, script, (0.5, 0.40))  # the hand rises 108 px on the desktop
    scrolls = of(actions, Scroll)
    assert gestures(engine.take_events()) == ["scroll_start"]
    assert scrolls and all(s.dy < 0 and s.dx == 0 for s in scrolls)  # wheel towards the user: content up
    assert sum(s.dy for s in scrolls) == pytest.approx(-2.4 * 108, abs=2)
    assert all(s.dy == int(s.dy) and near(s, CENTRE, 1e-6) for s in scrolls)
    assert of(actions[actions.index(scrolls[0]) :], MoveCursor) == []  # the cursor stays put


def test_moving_two_fingers_down_scrolls_the_content_down(engine: GestureEngine, script: Script) -> None:
    scrolls = of(scroll_story(engine, script, (0.5, 0.48)), Scroll)
    assert sum(s.dy for s in scrolls) == pytest.approx(2.4 * 64.8, abs=2)


def test_sideways_scroll_follows_the_hand_too(engine: GestureEngine, script: Script) -> None:
    scrolls = of(scroll_story(engine, script, (0.53, 0.45)), Scroll)  # 96 px to the right
    assert all(s.dy == 0 for s in scrolls)
    assert sum(s.dx for s in scrolls) == pytest.approx(-2.4 * 96, abs=2)  # content moves right: wheel left


def test_scroll_speed_and_wobble(settings: HandsSettings, engine: GestureEngine, script: Script) -> None:
    settings.scroll_speed = 2.0
    scrolls = of(scroll_story(engine, script, (0.503, 0.40)), Scroll)  # mostly up, a little sideways
    assert sum(s.dy for s in scrolls) == pytest.approx(-4.8 * 108, abs=3)
    assert sum(s.dx for s in scrolls) == 0


def test_the_cursor_moves_again_after_scrolling(engine: GestureEngine, script: Script) -> None:
    scroll_story(engine, script, (0.5, 0.40))
    actions = run(engine, script.hold(H("palm", (0.5, 0.40)), frames=3))
    assert of(actions, MoveCursor) and not engine.view().scrolling


# -- windows ------------------------------------------------------------------------------


def test_grab_and_drag_a_window() -> None:
    notes = FakeWindow(1, "Notes", Rect(560, 290, 800, 500))
    rig = Rig(windows=[FakeWindow(2, "Other", Rect(0, 0, 300, 300)), notes])
    rig.feed(Script().hold(H("palm", A), seconds=1.0))
    script = Script(t0=rig.now + 1 / 30)
    actions = rig.feed(script.hold(H("fist", A), frames=3))
    assert (
        of(actions, GrabWindow) == [GrabWindow(pytest.approx(960), pytest.approx(540))] and rig.engine.view().grabbing
    )
    rig.feed(script.move("fist", A, (0.55, 0.50), seconds=0.5))
    rig.feed(script.hold(H("fist", (0.55, 0.50)), seconds=1.0))
    rig.feed(script.hold(H("palm", (0.55, 0.50)), frames=3))
    assert rig.gestures == ["engage", "grab", "release"]
    assert near(Point(notes.rect.x, notes.rect.y), Point(560 + 160, 290 + 108), 2)
    assert rig.desktop.calls_named("raise_window") == [("raise_window", 1)]


def test_a_new_grab_needs_the_fist_opened_first(engine: GestureEngine, script: Script) -> None:
    engage(engine, script)
    run(engine, script.hold(H("fist", A), frames=3))
    run(engine, script.gap(seconds=0.4))  # lost: the grab ends
    actions = run(engine, script.hold(H("fist", A), frames=5))
    assert of(actions, GrabWindow) == []
    actions = run(engine, script.hold(H("palm", A), frames=3) + script.hold(H("fist", A), frames=3))
    assert len(of(actions, GrabWindow)) == 1


def test_two_hand_resize() -> None:
    notes = FakeWindow(1, "Notes", Rect(560, 290, 800, 500))
    rig = Rig(windows=[notes])
    rig.feed(Script().hold(H("palm", A), seconds=1.0))
    script = Script(t0=rig.now + 1 / 30)
    rig.feed(script.hold(H("fist", A), frames=3))
    left = (0.4, 0.45)
    actions = rig.feed(script.hold(H("fist", A), H("fist", left, "left"), frames=3))
    assert "resize_start" in rig.gestures and of(actions, ResizeWindow)
    assert rig.engine.view().helper is not None
    for i in range(1, 16):  # both hands pull apart: 0.05 each way, 320 px wider in all
        k = i / 15
        rig.feed([script.frame(H("fist", (0.5 + 0.05 * k, 0.45)), H("fist", (0.4 - 0.05 * k, 0.45), "left"))])
    rig.feed(script.hold(H("fist", (0.55, 0.45)), H("fist", (0.35, 0.45), "left"), seconds=1.0))
    assert notes.rect.width == pytest.approx(1120, abs=3) and notes.rect.height == 500
    assert notes.rect.center.x == pytest.approx(960, abs=3)
    # The helper hand opens: the whole grab ends, and the pointer's fist does not grab again.
    actions = rig.feed(script.hold(H("fist", (0.55, 0.45)), H("palm", (0.35, 0.45), "left"), frames=6))
    assert of(actions, ReleaseWindow) == [ReleaseWindow()] and of(actions, GrabWindow) == []
    assert rig.gestures == ["engage", "grab", "resize_start", "release"]
    assert not rig.engine.view().grabbing


def test_a_resize_survives_a_missed_pointer_frame() -> None:
    notes = FakeWindow(1, "Notes", Rect(560, 290, 800, 500))
    rig = Rig(windows=[notes])
    rig.feed(Script().hold(H("palm", A), seconds=1.0))
    script = Script(t0=rig.now + 1 / 30)
    left = (0.4, 0.45)
    rig.feed(script.hold(H("fist", A), frames=3) + script.hold(H("fist", A), H("fist", left, "left"), seconds=0.6))
    assert "resize_start" in rig.gestures
    widths = []
    frames = [script.frame(H("fist", left, "left"))]  # the pointer hand is missed once
    frames += script.hold(H("fist", A), H("fist", left, "left"), seconds=1.0)
    for f in frames:
        rig.feed([f])
        widths.append(notes.rect.width)
    assert min(widths) >= 797 and max(widths) <= 803


def test_resize_ends_when_the_helper_hand_leaves(engine: GestureEngine, script: Script) -> None:
    engage(engine, script)
    run(engine, script.hold(H("fist", A), frames=3))
    run(engine, script.hold(H("fist", A), H("fist", (0.3, 0.45), "left"), frames=3))
    actions = run(engine, script.hold(H("fist", A), frames=5))  # within hold_s: nothing ends
    assert of(actions, ReleaseWindow) == []
    actions = run(engine, script.hold(H("fist", A), frames=6))
    assert of(actions, ReleaseWindow) == [ReleaseWindow()]
    assert gestures(engine.take_events()) == ["grab", "resize_start", "release"]


def throw_story(engine: GestureEngine, script: Script, delta: tuple[float, float], at=A) -> list[Action]:
    engage(engine, script, at)
    run(engine, script.hold(H("fist", at), seconds=0.3))
    engine.take_events()
    return run(engine, fling(script, at, delta))


@pytest.mark.parametrize(
    ("delta", "direction"),
    [((0.12, 0.0), "right"), ((-0.12, 0.0), "left"), ((0.0, -0.2), "up"), ((0.0, 0.2), "down")],
)
def test_fling_throws(engine: GestureEngine, script: Script, delta: tuple[float, float], direction: str) -> None:
    actions = throw_story(engine, script, delta, at=(0.5, 0.5) if direction != "down" else (0.5, 0.25))
    assert of(actions, ThrowWindow) == [ThrowWindow(direction)]  # type: ignore[arg-type]
    assert of(actions, ReleaseWindow) == []
    assert gestures(engine.take_events()) == [f"throw_{direction}"]


def test_a_slow_release_just_lets_go(engine: GestureEngine, script: Script) -> None:
    engage(engine, script)
    run(engine, script.hold(H("fist", A), frames=3))
    actions = run(
        engine, script.move("fist", A, (0.6, 0.45), seconds=1.0) + script.hold(H("palm", (0.6, 0.45)), frames=3)
    )
    assert of(actions, ReleaseWindow) == [ReleaseWindow()] and of(actions, ThrowWindow) == []


def test_a_diagonal_fling_is_not_a_throw(engine: GestureEngine, script: Script) -> None:
    actions = throw_story(engine, script, (0.12, 0.2), at=(0.4, 0.3))
    assert of(actions, ReleaseWindow) == [ReleaseWindow()] and of(actions, ThrowWindow) == []


def test_a_fling_after_every_display_went_away_just_lets_go(
    engine: GestureEngine, mapper: ScreenMapper, script: Script
) -> None:
    engage(engine, script)
    run(engine, script.hold(H("fist", A), seconds=0.3))
    mapper.set_displays([])
    actions = run(engine, fling(script, A, (0.12, 0.0)))
    assert of(actions, ThrowWindow) == [] and of(actions, ReleaseWindow) == [ReleaseWindow()]


def test_throw_onto_the_projector() -> None:
    notes = FakeWindow(1, "Notes", Rect(1200, 290, 800, 500))
    rig = Rig([PRIMARY, PROJECTOR], [notes])
    rig.feed(Script().hold(H("palm", A), seconds=1.0))  # (1600, 540) on the primary
    script = Script(t0=rig.now + 1 / 30)
    rig.feed(script.hold(H("fist", A), seconds=0.3))
    rig.feed(fling(script, A, (0.1, 0.0)))
    assert "throw_right" in rig.gestures
    assert PROJECTOR.work.contains(notes.rect.center)
    assert notes.rect.right <= PROJECTOR.work.right and notes.rect.bottom <= PROJECTOR.work.bottom


def test_throw_right_without_a_display_there_snaps_to_the_right_half() -> None:
    notes = FakeWindow(1, "Notes", Rect(560, 290, 800, 500))
    rig = Rig(windows=[notes])
    rig.feed(Script().hold(H("palm", A), seconds=1.0))
    script = Script(t0=rig.now + 1 / 30)
    rig.feed(script.hold(H("fist", A), seconds=0.3))
    rig.feed(fling(script, A, (0.12, 0.0)))
    assert notes.rect == Rect(960, 0, 960, 1040)


# -- losing the hand, the real mouse ------------------------------------------------------


def test_hand_lost_during_a_drag(engine: GestureEngine, script: Script) -> None:
    engage(engine, script)
    run(engine, script.hold(H("pinch", A), frames=3) + script.move("pinch", A, (0.53, 0.45), frames=6))
    assert run(engine, script.gap(seconds=0.2)) == []  # a dropout shorter than hold_s changes nothing
    actions = run(engine, script.move("pinch", (0.53, 0.45), (0.54, 0.45), frames=3))
    assert of(actions, MoveCursor) and of(actions, Button) == []
    actions = run(engine, script.gap(seconds=0.4))
    assert actions == [ReleaseAll()]
    assert gestures(engine.take_events()) == ["drag_start", "drag_end"]
    assert engine.engaged
    # Back within lost_s, still pinching: the pinch must open before it presses again.
    actions = run(engine, script.hold(H("pinch", (0.54, 0.45)), frames=4))
    assert of(actions, Button) == [] and of(actions, MoveCursor)
    actions = run(
        engine, script.hold(H("palm", (0.54, 0.45)), frames=3) + script.hold(H("pinch", (0.54, 0.45)), frames=PRESS)
    )
    assert [b.down for b in of(actions, Button)] == [True]


def test_hand_gone_for_lost_s_disengages(engine: GestureEngine, script: Script) -> None:
    engage(engine, script)
    assert run(engine, script.gap(seconds=1.4)) == []
    assert engine.engaged
    assert run(engine, script.gap(seconds=0.2)) == []
    assert not engine.engaged
    assert engine.take_events() == [EngineEvent("gesture", "disengage"), EngineEvent("state", "idle")]


def test_hand_gone_while_pressed_releases_once_then_disengages(engine: GestureEngine, script: Script) -> None:
    engage(engine, script)
    run(engine, script.hold(H("pinch", A), frames=3))
    actions = run(engine, script.gap(seconds=2.0))
    assert actions == [ReleaseAll()]
    assert gestures(engine.take_events()) == ["disengage"]


def test_touching_the_mouse_lets_go_and_disengages() -> None:
    rig = Rig()
    rig.feed(Script().hold(H("palm", A), seconds=1.0))
    script = Script(t0=rig.now + 1 / 30)
    rig.feed(script.hold(H("pinch", A), frames=3) + script.move("pinch", A, (0.55, 0.45), frames=6))
    assert rig.desktop.buttons_down == {"left"}
    rig.desktop.move_mouse(200, 900)
    actions = rig.feed(script.hold(H("pinch", (0.55, 0.45)), frames=10))
    assert rig.desktop.buttons_down == set()
    assert not rig.engine.engaged and rig.gestures[-1] == "user_input"
    # Only the frame computed before the executor noticed carried a move, and it was dropped.
    assert len(of(actions, MoveCursor)) <= 1
    assert rig.desktop.cursor() == (200, 900)
    # A deliberate palm engages again.
    rig.feed(script.hold(H("palm", (0.55, 0.45)), seconds=1.0))
    assert rig.engine.engaged
    rig.settle()
    assert near(Point(*rig.desktop.cursor()), Point(1120, 540), 2)


def test_engage_always_waits_for_the_hands_to_leave_after_the_mouse_was_touched(
    settings: HandsSettings, engine: GestureEngine, script: Script
) -> None:
    settings.engage = "always"
    run(engine, script.hold(H("hover", A), frames=3))
    assert engine.engaged
    engine.on_user_input()
    assert gestures(engine.take_events()) == ["engage", "user_input"]
    run(engine, script.hold(H("hover", A), seconds=1.0))
    assert not engine.engaged
    run(engine, script.gap(seconds=0.5) + script.hold(H("hover", A), frames=2))
    assert engine.engaged


# -- mapping across displays ----------------------------------------------------------------


def test_the_hand_spans_side_by_side_displays_but_not_a_virtual_one(settings: HandsSettings, script: Script) -> None:
    mapper = ScreenMapper([PRIMARY, PROJECTOR, VIRTUAL], settings)
    engine = GestureEngine(settings, mapper)
    engage(engine, script, camera_point(0.25, 0.5))
    assert near(engine.view().cursor, Point(800, 540), 1)  # type: ignore[arg-type]
    run(engine, script.move("palm", camera_point(0.25, 0.5), camera_point(0.8, 0.3), seconds=0.5))
    run(engine, script.hold(H("palm", camera_point(0.8, 0.3)), seconds=1.5))
    assert near(engine.view().cursor, Point(2560, 324), 1)  # type: ignore[arg-type]
    run(engine, script.move("palm", camera_point(0.8, 0.3), (0.99, 0.95), seconds=0.3))
    run(engine, script.hold(H("palm", (0.99, 0.95)), seconds=1.5))
    cursor = engine.view().cursor
    assert cursor is not None and PROJECTOR.rect.contains(cursor)
    assert cursor.x == pytest.approx(3199, abs=1) and cursor.y == pytest.approx(719, abs=1)


def test_the_index_anchor_setting(settings: HandsSettings, engine: GestureEngine, script: Script) -> None:
    settings.anchor = "index"
    actions = run(engine, script.hold(H("palm", A), seconds=0.7))
    assert engine.engaged
    (move,) = of(actions, MoveCursor)[-1:]
    assert move.y < 540 - 100  # the fingertip is well above the knuckles


# -- calibration ---------------------------------------------------------------------------

QUAD = [(0.30, 0.25), (0.75, 0.30), (0.70, 0.75), (0.25, 0.70)]


def calibrate(engine: GestureEngine, script: Script, quad=QUAD) -> list[Action]:
    actions: list[Action] = []
    previous = (0.5, 0.5)
    for corner in quad:
        actions += run(engine, script.move("palm", previous, corner, frames=4))
        actions += run(engine, script.hold(H("palm", corner), seconds=1.3))
        previous = corner
    return actions


def test_calibration_end_to_end(engine: GestureEngine, mapper: ScreenMapper, script: Script) -> None:
    engine.start_calibration(script.t)
    assert engine.take_events() == [EngineEvent("calibration", "top_left"), EngineEvent("state", "calibrating")]
    view = engine.view()
    assert view.state == "calibrating" and view.calibration_target == mapper.target_point(0, 0)
    run(engine, script.hold(H("palm", QUAD[0]), seconds=0.5))
    assert 0.3 < engine.view().calibration_progress < 0.7
    actions = calibrate(engine, script)
    assert actions == []
    events = engine.take_events()
    assert [e.value for e in events if e.kind == "calibration"] == ["top_right", "bottom_right", "bottom_left", "done"]
    assert events[-1] == EngineEvent("state", "idle")
    assert mapper.calibrated
    h = engine.take_calibration_result()
    assert h is not None and engine.take_calibration_result() is None
    for p, corner in zip(QUAD, CORNERS, strict=True):
        u = apply_homography(h, Point(*p))
        assert (u.x, u.y) == pytest.approx(CORNER_TARGETS[corner], abs=1e-4)
    # The new mapping drives the cursor: the top-left corner shown is the screen's top-left corner.
    engage(engine, script, QUAD[0], settle=0.2)
    assert near(engine.view().cursor, Point(0, 0), 1)  # type: ignore[arg-type]


def test_calibration_targets_follow_the_corners(engine: GestureEngine, mapper: ScreenMapper, script: Script) -> None:
    engine.start_calibration(script.t)
    targets = [engine.view().calibration_target]
    for corner in QUAD[:3]:
        run(engine, script.move("palm", (0.5, 0.5), corner, frames=3) + script.hold(H("palm", corner), seconds=1.3))
        targets.append(engine.view().calibration_target)
    assert targets == [mapper.target_point(*CORNER_TARGETS[c]) for c in CORNERS]


def test_calibrating_while_engaged_releases_and_freezes_the_pointer(
    engine: GestureEngine, mapper: ScreenMapper, script: Script
) -> None:
    engage(engine, script)
    run(engine, script.hold(H("pinch", A), frames=3))
    engine.start_calibration(script.t)
    actions = run(engine, [script.frame(H("pinch", A))])
    assert actions == [ReleaseAll()]
    assert run(engine, script.move("palm", A, (0.6, 0.6), frames=10)) == []
    engine.cancel_calibration()
    events = engine.take_events()
    assert EngineEvent("calibration", "cancelled") in events and events[-1] == EngineEvent("state", "active")
    assert not mapper.calibrated and engine.take_calibration_result() is None
    actions = run(engine, script.hold(H("palm", (0.6, 0.6)), frames=2))
    assert near(of(actions, MoveCursor)[-1], mapper.to_desktop(Point(0.6, 0.6)), 1)


def test_calibration_times_out(engine: GestureEngine, mapper: ScreenMapper, script: Script) -> None:
    engine.start_calibration(script.t)
    run(engine, script.gap(seconds=31))
    assert EngineEvent("calibration", "cancelled") in engine.take_events()
    assert engine.state == "idle" and not mapper.calibrated


def test_any_hand_calibrates(
    settings: HandsSettings, engine: GestureEngine, mapper: ScreenMapper, script: Script
) -> None:
    settings.hand = "right"
    engine.start_calibration(script.t)
    previous = (0.5, 0.5)
    for corner in QUAD:
        run(engine, script.move("palm", previous, corner, frames=4, handedness="left"))
        run(engine, script.hold(H("palm", corner, "left"), seconds=1.3))
        previous = corner
    assert mapper.calibrated


# -- view and state --------------------------------------------------------------------------


def test_view_reports_the_interaction(engine: GestureEngine, script: Script) -> None:
    engage(engine, script)
    run(engine, script.hold(H("pinch", A), frames=3))
    view = engine.view()
    assert view.pressed and view.pose == "pinch" and view.pinch == 1.0 and not view.grabbing
    run(engine, script.hold(H("palm", A), frames=3) + script.hold(H("two", A), frames=3))
    assert engine.view().scrolling and engine.view().pose == "two"
    run(engine, script.hold(H("palm", A), frames=3) + script.hold(H("fist", A), frames=3))
    assert engine.view().grabbing and engine.view().helper is None
    assert engine.view().calibration_target is None and engine.view().calibration_progress == 0.0


def test_state_events_only_on_change(engine: GestureEngine, script: Script) -> None:
    engage(engine, script)
    run(engine, script.gap(seconds=2.0))
    run(engine, script.hold(H("palm", A), seconds=0.7))
    states = [e.value for e in engine.take_events() if e.kind == "state"]
    assert states == ["idle", "active"]


def test_engine_drives_the_desktop_end_to_end() -> None:
    rig = Rig()
    rig.feed(Script().hold(H("palm", A), seconds=1.0))
    script = Script(t0=rig.now + 1 / 30)
    rig.feed(script.move("palm", A, (0.6, 0.45), seconds=0.4) + script.hold(H("palm", (0.6, 0.45)), seconds=1.0))
    rig.settle()
    assert near(Point(*rig.desktop.cursor()), Point(1280, 540), 2)
    rig.feed(script.hold(H("pinch", (0.6, 0.45)), frames=3) + script.hold(H("palm", (0.6, 0.45)), frames=3))
    assert rig.desktop.calls_named("button") == [("button", "left", True), ("button", "left", False)]
    assert rig.desktop.buttons_down == set()


def test_every_event_value_is_in_the_protocol() -> None:
    import json
    from pathlib import Path

    schema = json.loads((Path(__file__).resolve().parents[2] / "protocol" / "hands.schema.json").read_text("utf-8"))
    allowed = {
        "gesture": set(schema["$defs"]["GestureName"]["enum"]),
        "calibration": set(schema["$defs"]["CalibrationStep"]["enum"]),
        "state": set(schema["$defs"]["HandsState"]["enum"]),
    }
    rig = Rig([PRIMARY, PROJECTOR], [FakeWindow(1, "Notes", Rect(1200, 290, 800, 500))])
    script = Script()
    rig.feed(script.hold(H("palm", A), seconds=1.0))
    one_click = script.hold(H("pinch", A), frames=3) + script.hold(H("palm", A), frames=3)
    rig.feed(one_click + one_click)
    rig.feed(script.hold(H("pinch_middle", A), frames=3) + script.hold(H("palm", A), frames=3))
    rig.feed(script.hold(H("pinch", A), frames=3) + script.move("pinch", A, (0.55, 0.45), frames=5))
    rig.feed(script.hold(H("palm", (0.55, 0.45)), frames=3) + script.hold(H("two", (0.55, 0.45)), frames=5))
    rig.feed(script.move("palm", (0.55, 0.45), A, frames=5) + script.hold(H("fist", A), frames=3))
    rig.feed(script.hold(H("fist", A), H("fist", (0.3, 0.45), "left"), frames=4))
    rig.feed(script.hold(H("palm", A), frames=3) + script.hold(H("fist", A), seconds=0.3) + fling(script, A, (0.1, 0)))
    rig.engine.start_calibration(script.t)
    rig.engine.cancel_calibration()
    rig.desktop.move_mouse(5, 5)
    rig.feed(script.hold(H("palm", A), frames=2))
    # The palm that was up when the mouse took over must let go first (see the re-engage tests below).
    rig.feed(script.gap(seconds=0.4))
    rig.feed(script.hold(H("palm", A), seconds=1.0))
    rig.feed(script.gap(seconds=2.0))
    seen = {e.value for e in rig.events if e.kind == "gesture"}
    assert seen >= {"engage", "click", "double_click", "right_click", "drag_start", "drag_end", "scroll_start"}
    assert seen >= {"grab", "resize_start", "release", "throw_right", "user_input", "disengage"}
    for event in rig.events:
        assert event.value in allowed[event.kind], event


# -- a break in tracking, and the real mouse ---------------------------------------------


def test_a_palm_up_across_a_break_in_tracking_must_hold_again(engine: GestureEngine, script: Script) -> None:
    """No frames come while the camera is closed or another desktop has the input (runtime.reset_tracks)."""
    engage(engine, script)
    engine.disengage("command")
    engine.take_events()
    engine.reset_tracks()
    # The hand never left: on the frame clock the gap did not happen at all.
    run(engine, script.hold(H("palm", (0.55, 0.5)), frames=1))
    assert not engine.engaged and engine.view().engage_progress == 0.0
    run(engine, script.hold(H("palm", (0.55, 0.5)), seconds=0.4))
    assert not engine.engaged
    run(engine, script.hold(H("palm", (0.55, 0.5)), seconds=0.3))
    assert engine.engaged  # the full hold, and no sooner
    assert gestures(engine.take_events()) == ["engage"]


def test_a_moving_palm_after_a_break_in_tracking_does_not_engage(engine: GestureEngine, script: Script) -> None:
    """The stillness check comes back too: a palm waving past the camera must not take the cursor."""
    engine.reset_tracks()
    t0 = script.t
    frames = [script.frame(H("palm", (0.5 + 0.12 * math.sin((script.t - t0) * 6.0), 0.45))) for _ in range(90)]
    run(engine, frames)  # about 0.7 frame widths a second, over three seconds
    assert not engine.engaged


def test_a_break_in_tracking_does_not_keep_an_engaged_hand_from_working(engine: GestureEngine, script: Script) -> None:
    """reset_tracks only ever comes with a disengage, but a stray one must not strand the pointer."""
    engage(engine, script)
    engine.reset_tracks()
    run(engine, script.hold(H("palm", A), frames=2))
    assert engine.engaged


def test_a_palm_left_in_view_does_not_re_engage_after_the_mouse_took_over(
    engine: GestureEngine, script: Script
) -> None:
    """'Touching the mouse always wins': the free hand resting open in view must not take the cursor back."""
    engage(engine, script)
    engine.on_user_input()
    assert gestures(engine.take_events()) == ["user_input"]
    for _ in range(6):  # six times the engage hold
        run(engine, script.hold(H("palm", A), seconds=0.5))
        assert not engine.engaged, "re-engaged while the user was using the mouse"
    assert engine.view().engage_progress == 0.0


def test_a_palm_that_lets_go_after_a_takeover_engages_again(engine: GestureEngine, script: Script) -> None:
    engage(engine, script)
    engine.on_user_input()
    engine.take_events()
    run(engine, script.hold(H("palm", A), seconds=0.8))
    assert not engine.engaged
    run(engine, script.hold(H("hover", A), seconds=0.2))  # the hand relaxes: it let go
    run(engine, script.hold(H("palm", A), seconds=0.8))
    assert engine.engaged


def test_a_hand_that_leaves_view_after_a_takeover_engages_again(engine: GestureEngine, script: Script) -> None:
    engage(engine, script)
    engine.on_user_input()
    engine.take_events()
    run(engine, script.gap(seconds=0.4))  # the hand drops out of view for longer than hold_s
    run(engine, script.hold(H("palm", A), seconds=0.8))
    assert engine.engaged


def test_the_other_hand_may_still_engage_after_a_takeover(engine: GestureEngine, script: Script) -> None:
    """Only the hand that was up is held back; the user's other hand is a deliberate new palm."""
    engage(engine, script)
    engine.on_user_input()
    engine.take_events()
    run(engine, script.hold(H("palm", A), H("palm", (0.25, 0.5), "left"), seconds=0.8))
    assert engine.engaged


def test_a_palm_left_in_view_does_not_re_engage_after_the_disengage_command(
    engine: GestureEngine, script: Script
) -> None:
    engage(engine, script)
    engine.disengage("command")
    engine.take_events()
    run(engine, script.hold(H("palm", A), seconds=1.5))
    assert not engine.engaged


def test_a_hand_lost_mid_gesture_may_engage_again_without_letting_go(engine: GestureEngine, script: Script) -> None:
    """Only a command or the real mouse holds a palm back; losing the hand is not the user's doing."""
    engage(engine, script)
    run(engine, script.gap(seconds=2.0))
    assert not engine.engaged
    engine.take_events()
    run(engine, script.hold(H("palm", A), seconds=0.8))
    assert engine.engaged


def test_the_voice_engage_command_works_right_after_a_takeover(engine: GestureEngine, script: Script) -> None:
    engage(engine, script)
    engine.on_user_input()
    engine.take_events()
    engine.engage()
    run(engine, script.hold(H("palm", A), frames=1))
    assert engine.engaged


# -- the cursor anchor ------------------------------------------------------------------


def test_with_the_index_anchor_the_cursor_follows_the_fingertip(
    settings: HandsSettings, engine: GestureEngine, script: Script
) -> None:
    settings.anchor = "index"
    knuckles = GestureEngine(HandsSettings(), engine.mapper)
    run(knuckles, script.hold(H("palm", A), seconds=1.0))
    engine_script = Script()
    actions = run(engine, engine_script.hold(H("palm", A), seconds=1.0))
    assert engine.engaged
    index_cursor = of(actions, MoveCursor)[-1]
    knuckle_cursor = of(run(knuckles, script.hold(H("palm", A), frames=1)), MoveCursor)[-1]
    assert not near(Point(index_cursor.x, index_cursor.y), Point(knuckle_cursor.x, knuckle_cursor.y), 20)


def test_with_the_index_anchor_a_pinch_is_still_a_click_not_a_drag(
    settings: HandsSettings, engine: GestureEngine, script: Script
) -> None:
    """The fingertip is what travels as the pinch closes; the slop is measured on the knuckles."""
    settings.anchor = "index"
    engage(engine, script)
    before = engine.view().cursor
    assert before is not None
    actions = run(engine, script.transition("palm", "pinch", A, seconds=0.2))
    actions += run(engine, script.hold(H("pinch", A), seconds=0.3))
    actions += run(engine, script.transition("pinch", "palm", A, seconds=0.2))
    actions += run(engine, script.hold(H("palm", A), seconds=0.2))
    assert gestures(engine.take_events()) == ["click"]
    presses = of(actions, Button)
    assert [(b.button, b.down) for b in presses] == [("left", True), ("left", False)]
    assert (presses[0].x, presses[0].y) == (presses[1].x, presses[1].y)
    # The press lands where the cursor was before the pinch began (the rewind), not where the tip ended up.
    assert near(Point(presses[0].x, presses[0].y), before, 6)


def test_with_the_index_anchor_a_deliberate_drag_still_drags(
    settings: HandsSettings, engine: GestureEngine, script: Script
) -> None:
    settings.anchor = "index"
    engage(engine, script)
    run(engine, script.transition("palm", "pinch", A, seconds=0.2))
    run(engine, script.hold(H("pinch", A), seconds=0.2))
    run(engine, script.move("pinch", A, (0.62, 0.45), seconds=0.3))
    run(engine, script.hold(H("palm", (0.62, 0.45)), seconds=0.3))
    assert gestures(engine.take_events()) == ["drag_start", "drag_end"]


def test_with_the_index_anchor_a_hand_keeps_its_track_through_a_pinch(
    settings: HandsSettings, engine: GestureEngine, script: Script
) -> None:
    """Association is on the knuckles, so a closing pinch cannot look like a new hand."""
    settings.anchor = "index"
    engage(engine, script)
    pointer = engine._pointer
    run(engine, script.transition("palm", "pinch", A, seconds=0.3))
    run(engine, script.hold(H("pinch", A), seconds=0.2))
    assert engine._pointer is pointer and len([t for t in engine._tracks if t.seen]) == 1


@pytest.mark.parametrize("fps", [30.0, 60.0])
def test_with_the_index_anchor_a_quick_pinch_is_a_click_not_a_drag(fps: float) -> None:
    """Five frames in, four held, five out (at 30 fps): the fingertip travels most of the slop on its own."""
    settings = HandsSettings()
    settings.anchor = "index"
    rig = Rig(settings=settings)
    script = Script(fps=fps)
    rig.feed(script.hold(H("palm", A), seconds=1.5))
    frames = round(5 * fps / 30)
    rig.feed(script.transition("palm", "pinch", A, frames=frames))
    rig.feed(script.hold(H("pinch", A), frames=round(4 * fps / 30)))
    rig.feed(script.transition("pinch", "palm", A, frames=frames))
    rig.feed(script.hold(H("palm", A), frames=round(10 * fps / 30)))
    assert rig.gestures[1:] == ["click"]
    down, up = of(rig.actions, Button)
    assert (down.x, down.y) == (up.x, up.y)


@pytest.mark.parametrize("fps", [30.0, 60.0])
@pytest.mark.parametrize("seconds", [0.1, 0.2, 0.3, 0.4, 0.6])
def test_with_the_index_anchor_the_click_lands_where_the_fingertip_pointed_before_the_pinch(
    fps: float, seconds: float
) -> None:
    """The rewind is on the knuckles: the fingertip's own travel as the finger bends into the pinch is undone,
    however long the pinch takes to close (``rewind_s`` alone covers only its last 70 ms)."""
    settings = HandsSettings()
    settings.anchor = "index"
    rig = Rig(settings=settings)
    script = Script(fps=fps)
    rig.feed(script.hold(H("palm", A), seconds=1.0))
    before = rig.engine.view().cursor
    assert before is not None
    rig.feed(script.transition("palm", "pinch", A, seconds=seconds))
    rig.feed(script.hold(H("pinch", A), seconds=0.3))
    rig.feed(script.transition("pinch", "palm", A, seconds=seconds))
    rig.feed(script.hold(H("palm", A), seconds=0.3))
    assert rig.gestures[1:] == ["click"]
    down = next(b for b in of(rig.actions, Button) if b.down)
    assert near(Point(down.x, down.y), before, 6)


def test_with_the_index_anchor_a_drag_carries_on_from_the_press_point() -> None:
    """While pinched, the fingertip sits where the bent finger put it: the drag follows the hand from the press
    point, just as far as it would with the knuckles anchor, and never jumps by that offset."""
    paths = {}
    for anchor in ("knuckles", "index"):
        settings = HandsSettings()
        settings.anchor = anchor  # type: ignore[assignment]
        rig = Rig(settings=settings)
        script = Script()
        rig.feed(script.hold(H("palm", A), seconds=1.0))
        rig.feed(script.transition("palm", "pinch", A, seconds=0.4))
        rig.feed(script.hold(H("pinch", A), seconds=0.3))
        down = next(b for b in of(rig.actions, Button) if b.down)
        mark = len(rig.actions)
        rig.feed(script.move("pinch", A, (0.53, 0.45), seconds=0.3))
        assert "drag_start" in rig.gestures
        paths[anchor] = [Point(down.x, down.y)] + [Point(m.x, m.y) for m in of(rig.actions[mark:], MoveCursor)]
    index, knuckles = paths["index"], paths["knuckles"]
    assert max(a.distance(b) for a, b in itertools.pairwise(index)) < 40
    assert near(index[-1] - index[0], knuckles[-1] - knuckles[0], 3)


@pytest.mark.parametrize("fps", [30.0, 60.0])
def test_with_the_index_anchor_a_noisy_pinch_still_lands_where_the_fingertip_pointed(fps: float) -> None:
    """Landmark noise must not end the look back for where the finger began to bend (``BEND_TOLERANCE``)."""
    misses = []
    for seed in range(10):
        noise = {"jitter": 0.0015, "rng": np.random.default_rng(seed)}
        settings = HandsSettings()
        settings.anchor = "index"
        rig = Rig(settings=settings)
        script = Script(fps=fps)
        rig.feed(script.hold(H("palm", A, options=noise), seconds=1.0))
        before = rig.engine.view().cursor
        assert before is not None
        rig.feed(script.transition("palm", "pinch", A, seconds=0.3, options=noise))
        rig.feed(script.hold(H("pinch", A, options=noise), seconds=0.3))
        rig.feed(script.transition("pinch", "palm", A, seconds=0.3, options=noise))
        rig.feed(script.hold(H("palm", A, options=noise), seconds=0.3))
        down = next((b for b in of(rig.actions, Button) if b.down), None)
        if rig.gestures[1:] != ["click"] or down is None or not near(Point(down.x, down.y), before, 12):
            misses.append((seed, rig.gestures, down))
    assert misses == []


def test_with_the_index_anchor_a_grab_takes_the_window_the_fingertip_pointed_at() -> None:
    """A fist curls the index all the way: the grab is where it pointed before, and the window does not jump."""
    settings = HandsSettings()
    settings.anchor = "index"
    rig = Rig(settings=settings, windows=[FakeWindow(1, "Notes", Rect(300, 100, 1300, 900))])
    script = Script()
    rig.feed(script.hold(H("palm", A), seconds=1.0))
    before = rig.engine.view().cursor
    assert before is not None
    rig.feed(script.transition("palm", "fist", A, seconds=0.4))
    rig.feed(script.hold(H("fist", A), seconds=0.2))
    (grab,) = of(rig.actions, GrabWindow)
    assert near(Point(grab.x, grab.y), before, 6)
    start = rig.desktop.window(1).rect
    rig.feed(script.hold(H("fist", A), seconds=0.3))
    held = rig.desktop.window(1).rect
    assert abs(held.x - start.x) < 6 and abs(held.y - start.y) < 6  # a still fist leaves the window where it was
