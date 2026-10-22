"""A hand changing pose must not act on what it passes through on the way.

The frames come from ``jarvis_hands.synthetic.between`` (``Script.transition``):
every joint moves at once, as a real hand's do, so closing a palm into a fist
goes through frames where the thumb is already on the index tip while the index
is not curled yet, which read as a pinch. Those frames must leave the mouse
buttons alone, before a grab and after a release or a throw, while a deliberate
pinch still clicks, drags and double-clicks.

``thumb`` profiles stand for hands that close the thumb with the fingers, before
them or after them (the thumb's timing is what makes the pinch window wide or
narrow); the duration sweeps cover 30 and 60 fps cameras.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

import numpy as np
import pytest

from jarvis_hands.actions import Button, GrabWindow, ReleaseWindow, ThrowWindow
from jarvis_hands.desktop.fake import FakeWindow
from jarvis_hands.geometry import Rect
from jarvis_hands.landmarks import Frame
from jarvis_hands.poses import PoseTracker, thresholds_for
from jarvis_hands.settings import KNOB_BY_KEY, HandsSettings

from scripted import HEIGHT, WIDTH, Blend, H, Rig, Script, SyntheticPose, camera_point, display

A = (0.5, 0.45)

#: The fist sensitivity at its default and at the two ends of its range.
FISTS = [1.0, KNOB_BY_KEY["fist"].low, KNOB_BY_KEY["fist"].high]


def fist_settings(fist: float) -> HandsSettings:
    settings = HandsSettings()
    settings.apply_config({"fist": fist})
    return settings


#: The thumb's progress as a function of the fingers' (closing); the mirror image when opening.
THUMB_PROFILES: dict[str, Callable[[float], float] | None] = {
    "with the fingers": None,
    "lagging": lambda k: k * k,
    "leading": lambda k: k**0.5,
    "last": lambda k: max(0.0, (k - 0.5) * 2),
    "first": lambda k: min(1.0, 2 * k),
}


def engaged(fps: float = 30.0, **kwargs: object) -> tuple[Rig, Script]:
    rig = Rig(**kwargs)  # type: ignore[arg-type]
    script = Script(fps=fps)
    rig.feed(script.hold(H("palm", A), seconds=1.0))
    assert rig.engine.engaged
    return rig, script


def buttons(rig: Rig) -> list[Button]:
    return [a for a in rig.actions if isinstance(a, Button)]


@pytest.mark.parametrize("fps", [30.0, 60.0])
@pytest.mark.parametrize("seconds", [0.3, 0.4, 0.5])
@pytest.mark.parametrize("thumb", list(THUMB_PROFILES))
def test_closing_a_palm_into_a_fist_and_opening_it_again_clicks_nothing(fps: float, seconds: float, thumb: str) -> None:
    rig, script = engaged(fps, windows=[FakeWindow(1, "Explorer", Rect(500, 300, 900, 600))])
    closing = THUMB_PROFILES[thumb]
    opening = None if closing is None else (lambda k: 1 - closing(1 - k))
    rig.feed(script.transition("palm", "fist", A, seconds=seconds, thumb=closing))
    rig.feed(script.hold(H("fist", A), seconds=0.3))
    rig.feed(script.transition("fist", "palm", A, seconds=seconds, thumb=opening))
    rig.feed(script.hold(H("palm", A), seconds=0.5))
    assert buttons(rig) == []
    assert rig.gestures == ["engage", "grab", "release"]
    assert [type(a).__name__ for a in rig.actions if isinstance(a, GrabWindow | ReleaseWindow)] == [
        "GrabWindow",
        "ReleaseWindow",
    ]


@pytest.mark.parametrize("fps", [30.0, 60.0])
@pytest.mark.parametrize("hold", [0.0, 0.1, 0.2])
def test_a_quick_grab_and_let_go_is_never_a_double_click(fps: float, hold: float) -> None:
    """Two stray clicks at one spot would be a double-click, which opens the file under the hand."""
    rig, script = engaged(fps, windows=[FakeWindow(1, "Explorer", Rect(500, 300, 900, 600))])
    rig.feed(script.transition("palm", "fist", A, seconds=0.4))
    if hold:
        rig.feed(script.hold(H("fist", A), seconds=hold))
    rig.feed(script.transition("fist", "palm", A, seconds=0.4))
    rig.feed(script.hold(H("palm", A), seconds=0.5))
    assert buttons(rig) == []
    assert "click" not in rig.gestures and "double_click" not in rig.gestures


@pytest.mark.parametrize("fps", [30.0, 60.0])
def test_a_relaxed_hand_closing_into_a_fist_clicks_nothing(fps: float) -> None:
    """The hand need not start from a palm: a relaxed hand over a window closes the same way."""
    rig, script = engaged(fps, windows=[FakeWindow(1, "Explorer", Rect(500, 300, 900, 600))])
    rig.feed(script.hold(H("hover", A), seconds=0.4))
    rig.feed(script.transition("hover", "fist", A, seconds=0.4, thumb=lambda k: min(1.0, 2 * k)))
    rig.feed(script.hold(H("fist", A), seconds=0.3))
    rig.feed(script.transition("fist", "hover", A, seconds=0.4))
    rig.feed(script.hold(H("palm", A), seconds=0.4))
    assert buttons(rig) == []
    assert rig.gestures == ["engage", "grab", "release"]


@pytest.mark.parametrize("frames", [4, 6, 8, 10])
def test_the_hand_opening_after_a_throw_clicks_nothing(frames: int) -> None:
    """The window flies to the projector and the hand opens over the window that was behind it."""
    primary = display(1, 0, 0, 1920, 1080, primary=True)
    projector = display(2, 1920, 0, 1920, 1080)
    rig = Rig(
        displays=[primary, projector],
        windows=[FakeWindow(1, "Notes", Rect(400, 200, 1000, 700)), FakeWindow(2, "Explorer", Rect(0, 0, 1920, 1040))],
    )
    script = Script()
    start = camera_point(0.3, 0.4)
    rig.feed(script.hold(H("palm", start), seconds=1.0))
    rig.feed(script.hold(H("fist", start), seconds=0.3))
    thrown = (start[0] + 0.06, start[1])
    rig.feed(script.move("fist", start, thrown, frames=3))
    # The hand keeps flinging while it opens: the throw reads the motion before the fist began to open.
    opening = [
        script.frame(Blend("fist", "palm", i / frames, (thrown[0] + 0.02 * i, thrown[1]))) for i in range(1, frames + 1)
    ]
    rig.feed(opening)
    rig.feed(script.hold(H("palm", (thrown[0] + 0.02 * frames, thrown[1])), seconds=0.4))
    assert rig.gestures == ["engage", "grab", "throw_right"]
    assert buttons(rig) == []
    assert any(isinstance(a, ThrowWindow) for a in rig.actions)
    assert rig.desktop.window(1).rect.center.x > 1920  # the window did land on the projector


@pytest.mark.parametrize("fps", [30.0, 60.0])
@pytest.mark.parametrize("seconds", [0.0, 0.2, 0.4])
def test_a_deliberate_pinch_still_clicks_within_a_frame_interval_of_the_hand_holding_still(
    fps: float, seconds: float
) -> None:
    rig, script = engaged(fps)
    if seconds:
        rig.feed(script.transition("palm", "pinch", A, seconds=seconds))
    still_from = script.t  # the hand stops moving here; the press may wait one settle window for it
    down: list[float] = []
    for frame in script.hold(H("pinch", A), seconds=0.4) + script.hold(H("palm", A), seconds=0.3):
        for action in rig.feed([frame]):
            if isinstance(action, Button) and action.down:
                down.append(frame.t)
    assert rig.gestures == ["engage", "click"]
    assert len(down) == 1
    assert down[0] - still_from <= 0.1 + 1 / fps


@pytest.mark.parametrize("fps", [30.0, 60.0])
def test_a_deliberate_pinch_drag_still_presses_carries_and_releases(fps: float) -> None:
    rig, script = engaged(fps)
    rig.feed(script.transition("palm", "pinch", A, seconds=0.2))
    rig.feed(script.hold(H("pinch", A), seconds=0.2))
    assert rig.executor.held == {"left"}
    rig.feed(script.move("pinch", A, (0.6, 0.45), seconds=0.3))
    assert rig.executor.held == {"left"}
    carried = rig.desktop.cursor()
    rig.feed(script.transition("pinch", "palm", (0.6, 0.45), seconds=0.2))
    rig.feed(script.hold(H("palm", (0.6, 0.45)), seconds=0.2))
    assert rig.executor.held == frozenset()
    assert rig.gestures == ["engage", "drag_start", "drag_end"]
    assert carried[0] > 960  # the cursor followed the hand while the button was down


@pytest.mark.parametrize("fps", [30.0, 60.0])
def test_two_deliberate_pinches_still_double_click(fps: float) -> None:
    rig, script = engaged(fps)
    for _ in range(2):
        rig.feed(script.transition("palm", "pinch", A, seconds=0.1))
        rig.feed(script.hold(H("pinch", A), seconds=0.15))
        rig.feed(script.transition("pinch", "palm", A, seconds=0.1))
        rig.feed(script.hold(H("palm", A), seconds=0.05))
    assert rig.gestures == ["engage", "click", "double_click"]
    downs = [(a.x, a.y) for a in buttons(rig) if a.down]
    assert len(downs) == 2 and downs[0] == downs[1]  # both at the first click's point


def test_a_deliberate_middle_pinch_still_right_clicks() -> None:
    rig, script = engaged()
    rig.feed(script.transition("palm", "pinch_middle", A, seconds=0.2))
    rig.feed(script.hold(H("pinch_middle", A), seconds=0.3))
    rig.feed(script.hold(H("palm", A), seconds=0.3))
    assert rig.gestures == ["engage", "right_click"]
    assert [(a.button, a.down) for a in buttons(rig)] == [("right", True), ("right", False)]


def test_a_pinch_the_hand_never_settles_into_presses_nothing() -> None:
    """A hand that keeps closing right through the pinch is on its way somewhere else."""
    rig, script = engaged()
    rig.feed(script.transition("palm", "fist", A, seconds=1.0))
    assert buttons(rig) == []
    assert "click" not in rig.gestures


def test_the_pinch_that_ends_a_grab_is_held_back_until_the_hand_opens() -> None:
    """A confirmed pinch while grabbing releases the window; the press waits for a pose that is no action."""
    rig, script = engaged(windows=[FakeWindow(1, "Explorer", Rect(500, 300, 900, 600))])
    rig.feed(script.hold(H("fist", A), seconds=0.3))
    assert rig.executor.grabbing
    rig.feed(script.hold(H("pinch", A), seconds=0.3))
    assert rig.gestures == ["engage", "grab", "release"]
    assert buttons(rig) == []
    # Once the hand has opened, a fresh pinch clicks again.
    rig.feed(script.hold(H("palm", A), seconds=0.2))
    rig.feed(script.hold(H("pinch", A), seconds=0.3))
    rig.feed(script.hold(H("palm", A), seconds=0.2))
    assert rig.gestures == ["engage", "grab", "release", "click"]
    assert [(a.button, a.down) for a in buttons(rig)] == [("left", True), ("left", False)]


def test_a_held_pinch_presses_once_the_other_fingers_held_still_for_the_settle_window() -> None:
    """With ``confirm_frames`` 1: the other fingers jumped from straight to relaxed, so the press waits for the
    window to hold only the pinch (``SETTLE_WINDOW_S``, three frames at 30 fps), and goes down then."""
    settings = HandsSettings()
    settings.confirm_frames = 1
    rig, script = engaged(settings=settings)
    rig.feed(script.hold(H("pinch", A), frames=3))
    assert rig.executor.held == frozenset()
    rig.feed(script.hold(H("pinch", A), frames=1))
    assert rig.executor.held == {"left"}


def test_at_60_fps_a_pinch_presses_no_sooner_than_it_lasted_the_settle_minimum() -> None:
    """Three frames are only 33 ms at 60 fps: the pinch must also last ``SETTLE_MIN_S`` (the fourth frame)."""
    settings = HandsSettings()
    settings.confirm_frames = 1
    rig, script = engaged(60.0, settings=settings)
    rig.feed(script.hold(H("hover", A), seconds=0.3))
    rig.feed(script.hold(H("pinch", A), frames=3))
    assert rig.executor.held == frozenset()
    rig.feed(script.hold(H("pinch", A), frames=1))
    assert rig.executor.held == {"left"}


def test_a_pinch_from_a_relaxed_hand_presses_once_it_lasted_two_frame_intervals() -> None:
    """The other fingers already rest where the pinch holds them, so only the pinch's own length is waited for:
    ``SETTLE_MIN_S`` and three frames (``confirm_frames`` 1)."""
    settings = HandsSettings()
    settings.confirm_frames = 1
    rig, script = engaged(settings=settings)
    rig.feed(script.hold(H("hover", A), seconds=0.3))
    rig.feed(script.hold(H("pinch", A), frames=2))
    assert rig.executor.held == frozenset()
    rig.feed(script.hold(H("pinch", A), frames=1))
    assert rig.executor.held == {"left"}


# -- one finger moving on its own past the thumb tucked against the curled ones ------------------------------

#: Pointing into a fist, the scroll pose folding back into pointing, and pointing opening into the scroll pose:
#: what the hand does next (``two`` is a scroll, ``point`` none). The other fingers hold still all along.
ONE_FINGER: dict[tuple[SyntheticPose, SyntheticPose], list[str]] = {
    ("point", "fist"): ["grab"],
    ("two", "point"): [],
    ("point", "two"): ["scroll_start"],
}


def min_jerk(k: float) -> float:
    """How far a real finger is along a move ``k`` of the way through its time: slow, fast, slow."""
    return 10 * k**3 - 15 * k**4 + 6 * k**5


def one_finger(
    start: SyntheticPose,
    end: SyntheticPose,
    seconds: float,
    fps: float,
    profile: Callable[[float], float] = lambda k: k,
    options: dict[str, object] | None = None,
) -> tuple[list[Button], list[str]]:
    """``start`` held, then one finger moving it into ``end`` (``profile`` maps time to progress): the buttons
    and the gestures that move made. ``options`` are more ``hand`` keywords for every frame, as for ``H``."""
    noise = dict(options or {})
    rig, script = engaged(fps, windows=[FakeWindow(1, "Explorer", Rect(500, 300, 900, 600))])
    rig.feed(script.transition("palm", start, A, seconds=0.3, options=noise))
    rig.feed(script.hold(H(start, A, options=noise), seconds=0.5))
    before = len(rig.gestures)
    n = script.count(seconds)
    rig.feed([script.frame(Blend(start, end, profile(i / n), A, options=noise)) for i in range(1, n + 1)])
    rig.feed(script.hold(H(end, A, options=noise), seconds=0.5))
    return buttons(rig), rig.gestures[before:]


@pytest.mark.parametrize("fps", [30.0, 60.0])
@pytest.mark.parametrize("seconds", [0.45, 0.6, 0.8, 1.0])
@pytest.mark.parametrize(("start", "end"), list(ONE_FINGER))
def test_one_finger_curling_or_uncurling_past_the_tucked_thumb_clicks_nothing(
    start: SyntheticPose, end: SyntheticPose, seconds: float, fps: float
) -> None:
    """The finger passes through a pinch while the other fingers are already still: only its own motion tells
    that pinch from a held one (a left click before the grab, a right-click before or after the scroll)."""
    assert one_finger(start, end, seconds, fps) == ([], ONE_FINGER[start, end])


@pytest.mark.parametrize("fps", [30.0, 60.0])
@pytest.mark.parametrize("seconds", [0.6, 0.8, 1.0, 1.5])
@pytest.mark.parametrize(("start", "end"), list(ONE_FINGER))
def test_one_finger_moving_at_a_real_fingers_pace_past_the_tucked_thumb_clicks_nothing(
    start: SyntheticPose, end: SyntheticPose, seconds: float, fps: float
) -> None:
    """A real finger speeds up and slows down (``min_jerk``) instead of moving at one speed throughout."""
    assert one_finger(start, end, seconds, fps, min_jerk) == ([], ONE_FINGER[start, end])


# -- quick taps: a pinch let go before the hand settled into it is still a click ----------------------------


@pytest.mark.parametrize("fps", [30.0, 60.0])
@pytest.mark.parametrize("close_s", [0.1, 0.15, 0.2])
@pytest.mark.parametrize("hold", [0.05, 0.1, 0.15])
def test_a_quick_tap_clicks_even_when_it_ends_before_the_hand_settles(fps: float, close_s: float, hold: float) -> None:
    """Thumb to the index and straight back: the click goes out whole, at the point the pinch began.

    (The pinch must still show on ``confirm_frames`` frames, hence the shortest hold.)
    """
    rig, script = engaged(fps)
    before = rig.engine.view().cursor
    rig.feed(script.transition("palm", "pinch", A, seconds=close_s))
    rig.feed(script.hold(H("pinch", A), seconds=hold))
    rig.feed(script.transition("pinch", "palm", A, seconds=close_s))
    rig.feed(script.hold(H("palm", A), seconds=0.4))
    assert rig.gestures == ["engage", "click"]
    assert [(a.button, a.down) for a in buttons(rig)] == [("left", True), ("left", False)]
    assert before is not None
    assert {(a.x, a.y) for a in buttons(rig)} == {(before.x, before.y)}


@pytest.mark.parametrize("fps", [30.0, 60.0])
def test_two_quick_taps_double_click(fps: float) -> None:
    rig, script = engaged(fps)
    for _ in range(2):
        rig.feed(script.transition("palm", "pinch", A, seconds=0.1))
        rig.feed(script.hold(H("pinch", A), seconds=0.05))
        rig.feed(script.transition("pinch", "palm", A, seconds=0.1))
        rig.feed(script.hold(H("palm", A), seconds=0.05))
    rig.feed(script.hold(H("palm", A), seconds=0.3))
    assert rig.gestures == ["engage", "click", "double_click"]
    downs = [(a.x, a.y) for a in buttons(rig) if a.down]
    assert len(downs) == 2 and downs[0] == downs[1]


def test_a_quick_middle_tap_right_clicks() -> None:
    rig, script = engaged()
    rig.feed(script.transition("palm", "pinch_middle", A, seconds=0.1))
    rig.feed(script.hold(H("pinch_middle", A), seconds=0.05))
    rig.feed(script.transition("pinch_middle", "palm", A, seconds=0.1))
    rig.feed(script.hold(H("palm", A), seconds=0.3))
    assert rig.gestures == ["engage", "right_click"]
    assert [(a.button, a.down) for a in buttons(rig)] == [("right", True), ("right", False)]


def test_a_tap_whose_thumb_leaves_while_the_hand_keeps_closing_into_a_fist_clicks_nothing() -> None:
    """The pinch may end by the thumb sliding off the index tip on its way into a fist: the fist follows."""
    rig, script = engaged(60.0, windows=[FakeWindow(1, "Explorer", Rect(500, 300, 900, 600))])
    rig.feed(script.transition("palm", "pinch", A, seconds=0.15))
    # The thumb slides off (a relaxed hand, fingers still closing), and the index curls a few frames later.
    rig.feed([script.frame(Blend("pinch", "hover", k / 4, A)) for k in range(1, 5)])
    rig.feed(script.transition("hover", "fist", A, seconds=0.15))
    rig.feed(script.hold(H("fist", A), seconds=0.2))
    assert buttons(rig) == []
    assert rig.gestures == ["engage", "grab"]


# -- tracker noise: a flicker must not undo what keeps a passing pinch from clicking --------------------------


def raw_pose(blend: Blend) -> str:
    script = Script()
    return PoseTracker().classify(blend.observe((script.width, script.height)), "knuckles", HEIGHT / WIDTH).pose


@pytest.mark.parametrize("seconds", [0.4, 0.5])
@pytest.mark.parametrize("thumb", ["with the fingers", "leading", "first"])
def test_one_noisy_frame_inside_the_pinch_a_closing_hand_passes_through_clicks_nothing(
    seconds: float, thumb: str
) -> None:
    """Pinch, pinch, hover, pinch: one frame of noise must not start the hand's settling over."""
    closing = THUMB_PROFILES[thumb]
    n = round(seconds * 60)
    blends = [
        Blend("palm", "fist", i / n, A, "right", None if closing is None else closing(i / n)) for i in range(1, n + 1)
    ]
    pinch = [i for i, b in enumerate(blends) if raw_pose(b) == "pinch"]
    assert len(pinch) >= 4
    noisy = blends[pinch[2]]
    tk = noisy.k if noisy.thumb_k is None else noisy.thumb_k
    blends[pinch[2]] = replace(noisy, thumb_k=tk * 0.5)  # the thumb lags for one frame: it reads as no pinch
    assert raw_pose(blends[pinch[2]]) != "pinch"
    rig, script = engaged(60.0, windows=[FakeWindow(1, "Explorer", Rect(500, 300, 900, 600))])
    rig.feed([script.frame(b) for b in blends])
    rig.feed(script.hold(H("fist", A), seconds=0.3))
    rig.feed(script.transition("fist", "palm", A, seconds=seconds))
    rig.feed(script.hold(H("palm", A), seconds=0.5))
    assert buttons(rig) == []
    assert rig.gestures == ["engage", "grab", "release"]


def raw_poses(blends: list[Blend], fist: float = 1.0) -> list[str]:
    """What one hand's tracker (hysteresis and all) reads for ``blends`` in a row."""
    tracker = PoseTracker(thresholds_for(1.0, fist))
    script = Script()
    return [tracker.classify(b.observe((script.width, script.height)), "knuckles", HEIGHT / WIDTH).pose for b in blends]


def opening_with_pauses(script: Script, hover_s: float, pinch_s: float) -> list[Frame]:
    """A fist opening into a palm that lingers on the hover, then on the pinch, a fist opening passes through."""
    opening = [Blend("fist", "palm", i / 20, A) for i in range(1, 21)]
    raws = raw_poses(opening)
    hover, pinch = opening[raws.index("hover")], opening[raws.index("pinch")]
    frames = [script.frame(hover) for _ in range(script.count(hover_s))]
    frames += [script.frame(pinch) for _ in range(script.count(pinch_s))]
    assert raw_poses([Blend("fist", "palm", 0.0, A), hover, pinch]) == ["fist", "hover", "pinch"]
    return frames + [script.frame(b) for b in opening if b.k > pinch.k]


@pytest.mark.parametrize("fps", [30.0, 60.0])
def test_a_fist_that_pauses_on_its_way_open_clicks_nothing(fps: float) -> None:
    """After a release the hand may linger on the hover and on the pinch: the hover does not end the latch."""
    rig, script = engaged(fps, windows=[FakeWindow(1, "Explorer", Rect(500, 300, 900, 600))])
    rig.feed(script.hold(H("fist", A), seconds=0.3))
    rig.feed(opening_with_pauses(script, hover_s=0.1, pinch_s=0.3))
    rig.feed(script.hold(H("palm", A), seconds=0.4))
    assert buttons(rig) == []
    assert rig.gestures == ["engage", "grab", "release"]


def test_a_hand_engaged_while_it_makes_a_fist_does_not_click_as_the_fist_opens() -> None:
    """With ``engage: always`` the hand may come into view closed: its fist is latched, and so is its pinch."""
    settings = HandsSettings()
    settings.engage = "always"
    rig = Rig(settings=settings, windows=[FakeWindow(1, "Explorer", Rect(500, 300, 900, 600))])
    script = Script()
    rig.feed(script.hold(H("fist", A), seconds=0.3))
    assert rig.engine.engaged
    rig.feed(opening_with_pauses(script, hover_s=0.1, pinch_s=0.3))
    rig.feed(script.hold(H("palm", A), seconds=0.4))
    assert buttons(rig) == []
    assert rig.gestures == ["engage"]


def flick(frames: int, fist: float = 1.0) -> list[Blend]:
    """A palm closing over ``frames`` frames up to the first one that reads as a fist, and straight back open."""
    closing = [Blend("palm", "fist", i / frames, A) for i in range(1, frames + 1)]
    shut = raw_poses(closing, fist).index("fist")
    return closing[: shut + 1] + closing[shut - 1 :: -1]


#: (the fist sensitivity the flick is built for, the one the engine runs at): a looser fist stays closed longer on the
#: way back, which makes it no flick.
FLICKS = [(1.0, 1.0), (FISTS[1], FISTS[1]), (1.0, FISTS[1])]


@pytest.mark.parametrize(("built_for", "fist"), FLICKS)
@pytest.mark.parametrize(("fps", "frames", "confirm"), [(30.0, 12, 2), (60.0, 24, 3)])
def test_a_fist_too_quick_to_grab_clicks_nothing_on_its_way_back_open(
    fps: float, frames: int, confirm: int, built_for: float, fist: float
) -> None:
    """The fist shows on too few frames to be confirmed, so the pinch on either side of it is one long pinch.

    Its way down and back up must not average out to fingers holding still either; the fist sensitivity, which
    only decides what counts as a fist, must not change that.
    """
    settings = fist_settings(fist)
    settings.confirm_frames = confirm
    rig, script = engaged(fps, settings=settings, windows=[FakeWindow(1, "Explorer", Rect(500, 300, 900, 600))])
    blends = flick(frames, built_for)
    raws = raw_poses(blends, built_for)
    shut = raws.index("fist")
    assert raws.count("fist") < confirm and "pinch" in raws[:shut] and "pinch" in raws[shut:]
    rig.feed([script.frame(b) for b in blends])
    rig.feed(script.hold(H("palm", A), seconds=0.4))
    assert buttons(rig) == []
    assert rig.gestures == ["engage"]


def test_after_a_release_a_relaxed_hand_held_a_moment_clicks_again() -> None:
    """The latch on the opening fist's pinch ends once the hand rests in a pose that is no action."""
    rig, script = engaged(windows=[FakeWindow(1, "Explorer", Rect(500, 300, 900, 600))])
    rig.feed(script.hold(H("fist", A), seconds=0.3))
    rig.feed(script.transition("fist", "hover", A, seconds=0.3))
    rig.feed(script.hold(H("hover", A), seconds=0.3))
    rig.feed(script.transition("hover", "pinch", A, seconds=0.15))
    rig.feed(script.hold(H("pinch", A), seconds=0.2))
    rig.feed(script.transition("pinch", "hover", A, seconds=0.15))
    rig.feed(script.hold(H("hover", A), seconds=0.3))
    assert rig.gestures == ["engage", "grab", "release", "click"]
    assert [(a.button, a.down) for a in buttons(rig)] == [("left", True), ("left", False)]


def jittered(level: float, seed: int) -> dict[str, object]:
    return {"jitter": level, "rng": np.random.default_rng(seed)}


#: Image-landmark noise, in frame widths per landmark and frame, independent from frame to frame: about what
#: the real tracker shows on a still hand under heavy webcam sensor noise, and where the first settle rule (two
#: frames of the pinch, compared end to end) let clicks through at 60 fps.
JITTER = [0.0015, 0.002]


@pytest.mark.parametrize("level", JITTER)
@pytest.mark.parametrize("seconds", [0.4, 0.5])
@pytest.mark.parametrize("thumb", ["with the fingers", "leading", "first"])
def test_noisy_hands_closing_into_a_fist_and_opening_again_click_nothing(
    level: float, seconds: float, thumb: str
) -> None:
    closing = THUMB_PROFILES[thumb]
    opening = None if closing is None else (lambda k: 1 - closing(1 - k))
    clicked = []
    for seed in [*range(8), 12, 14]:  # 1, 3, 12 and 14 clicked under the first rule
        noise = jittered(level, seed)
        rig = Rig(windows=[FakeWindow(1, "Explorer", Rect(500, 300, 900, 600))])
        script = Script(fps=60.0)
        rig.feed(script.hold(H("palm", A, options=noise), seconds=1.0))
        assert rig.engine.engaged
        rig.feed(script.transition("palm", "fist", A, seconds=seconds, thumb=closing, options=noise))
        rig.feed(script.hold(H("fist", A, options=noise), seconds=0.3))
        rig.feed(script.transition("fist", "palm", A, seconds=seconds, thumb=opening, options=noise))
        rig.feed(script.hold(H("palm", A, options=noise), seconds=0.5))
        if buttons(rig):
            clicked.append((seed, rig.gestures))
    assert clicked == []


def slow_closing_clicks(fps: float, closing: float, fist: float) -> set[int]:
    """The seeds (of ten) in which a noisy relaxed hand, closing into a fist over ``closing`` seconds, clicked."""
    clicked = set()
    for seed in range(10):
        noise = jittered(0.0015, seed)
        rig = Rig(settings=fist_settings(fist), windows=[FakeWindow(1, "Explorer", Rect(500, 300, 900, 600))])
        script = Script(fps=fps)
        rig.feed(script.hold(H("palm", A, options=noise), seconds=1.0))
        rig.feed(script.transition("palm", "hover", A, seconds=0.3, options=noise))
        rig.feed(script.hold(H("hover", A, options=noise), seconds=0.4))
        rig.feed(script.transition("hover", "fist", A, seconds=closing, thumb=THUMB_PROFILES["first"], options=noise))
        rig.feed(script.hold(H("fist", A, options=noise), seconds=0.3))
        if buttons(rig):
            clicked.add(seed)
    return clicked


@pytest.mark.parametrize("fist", FISTS)
@pytest.mark.parametrize("fps", [30.0, 60.0])
def test_a_noisy_relaxed_hand_closing_slowly_into_a_fist_clicks_nothing(fps: float, fist: float) -> None:
    """Slow closings move the other fingers barely faster than a still hand's noise: the settle window must
    take in the frames before the pinch, not just its first two or three."""
    assert slow_closing_clicks(fps, 0.8, fist) == set()


@pytest.mark.parametrize("fist", FISTS[1:])
@pytest.mark.parametrize("closing", [0.8, 1.0, 1.2])
@pytest.mark.parametrize("fps", [30.0, 60.0])
def test_the_fist_sensitivity_adds_no_clicks_to_a_slow_closing(fps: float, closing: float, fist: float) -> None:
    """The fist sensitivity decides what counts as a fist, not how the pinch before it is judged: a closing slower
    than a second clicks at the default too (the settle window is a second wide), and must click no more
    (a fist of 0.9 once pressed in 8 of 10 seeds a closing of 1.0 s at 30 fps, the default in none)."""
    assert slow_closing_clicks(fps, closing, fist) <= slow_closing_clicks(fps, closing, 1.0)


def closing_frame(script: Script, k: float, profile: Callable[[float], float] | None, noise: dict) -> Frame:
    thumb = None if profile is None else profile(k)
    return script.frame(Blend("hover", "fist", k, A, "right", thumb, dict(noise)))


@pytest.mark.parametrize("fist", FISTS[:2])
@pytest.mark.parametrize("halfway", [0.6, 0.8])
def test_a_noisy_hand_that_hesitates_halfway_into_a_fist_clicks_nothing(halfway: float, fist: float) -> None:
    """Closing, a pause with the fingers half curled (0.4 s), closing on: the fingers rest long enough to count as
    settled, but a pinch under a stricter fist sensitivity must be judged as at the default (it once pressed in a
    third of the runs at 0.9)."""
    clicked = []
    for thumb, profile in THUMB_PROFILES.items():
        for seed in range(6):
            noise = jittered(0.001, seed)
            rig = Rig(settings=fist_settings(fist), windows=[FakeWindow(1, "Explorer", Rect(500, 300, 900, 600))])
            script = Script(fps=30.0)
            rig.feed(script.hold(H("palm", A, options=noise), seconds=1.0))
            rig.feed(script.transition("palm", "hover", A, seconds=0.3, options=noise))
            rig.feed(script.hold(H("hover", A, options=noise), seconds=0.4))

            closing = script.count(0.3)
            ks = [halfway * i / closing for i in range(1, closing + 1)]
            ks += [halfway] * script.count(0.4)
            ks += [halfway + (1 - halfway) * i / closing for i in range(1, closing + 1)]
            rig.feed([closing_frame(script, k, profile, noise) for k in ks])
            rig.feed(script.hold(H("fist", A, options=noise), seconds=0.3))
            if buttons(rig):
                clicked.append((thumb, seed, rig.gestures))
    assert clicked == []


@pytest.mark.parametrize("fps", [30.0, 60.0])
@pytest.mark.parametrize("seconds", [0.45, 0.8])
@pytest.mark.parametrize(("start", "end"), list(ONE_FINGER))
def test_a_noisy_finger_moving_on_its_own_past_the_tucked_thumb_clicks_nothing(
    start: SyntheticPose, end: SyntheticPose, seconds: float, fps: float
) -> None:
    """The finger's own motion is measured over the pinch's few frames only: noise must not make it look still."""
    clicked = []
    for seed in range(8):
        pressed, made = one_finger(start, end, seconds, fps, options=jittered(0.001, seed))
        if pressed or made != ONE_FINGER[start, end]:
            clicked.append((seed, made))
    assert clicked == []


@pytest.mark.parametrize("level", [0.001, 0.0015])
@pytest.mark.parametrize("fps", [30.0, 60.0])
@pytest.mark.parametrize("hold", [0.05, 0.1])
def test_noisy_quick_taps_still_click(level: float, fps: float, hold: float) -> None:
    missed = []
    for seed in range(10):
        noise = jittered(level, seed)
        rig = Rig()
        script = Script(fps=fps)
        rig.feed(script.hold(H("palm", A, options=noise), seconds=1.0))
        assert rig.engine.engaged
        rig.feed(script.transition("palm", "pinch", A, seconds=0.1, options=noise))
        rig.feed(script.hold(H("pinch", A, options=noise), seconds=hold))
        rig.feed(script.transition("pinch", "palm", A, seconds=0.1, options=noise))
        rig.feed(script.hold(H("palm", A, options=noise), seconds=0.4))
        if rig.gestures != ["engage", "click"] or len(buttons(rig)) != 2:
            missed.append((seed, rig.gestures))
    assert missed == []


@pytest.mark.parametrize("fps", [30.0, 60.0])
@pytest.mark.parametrize("start", ["palm", "hover", "point"])
def test_a_noisy_held_pinch_presses_within_a_settle_window_of_the_hand_holding_still(
    start: SyntheticPose, fps: float
) -> None:
    """What the settle rule costs a drag: the press waits about ``SETTLE_WINDOW_S`` after the fingers stop.

    From a relaxed or a pointing hand the other fingers are still already: there the index's own settling
    (``SETTLE_FINGER_RATE``) is what the press waits for, and the press may even beat the hand's stop."""
    late = []
    for seed in range(10):
        noise = jittered(0.0015, seed)
        rig = Rig()
        script = Script(fps=fps)
        rig.feed(script.hold(H("palm", A, options=noise), seconds=1.0))
        if start != "palm":
            rig.feed(script.transition("palm", start, A, seconds=0.3, options=noise))
            rig.feed(script.hold(H(start, A, options=noise), seconds=0.4))
        closing = script.transition(start, "pinch", A, seconds=0.2, options=noise)
        still = script.t
        down = None
        for frame in closing + script.hold(H("pinch", A, options=noise), seconds=0.3):
            if down is None and any(isinstance(a, Button) and a.down for a in rig.feed([frame])):
                down = frame.t - still
        if down is None or down > 0.1 + 1.5 / fps:
            late.append((seed, down))
    assert late == []
