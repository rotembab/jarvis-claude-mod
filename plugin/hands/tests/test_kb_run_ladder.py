"""X55: a run in flight against the degradation ladder and the phase keys (F5, F14).

A 200-character Insert at 30 fps is the longest run there is (6.7 s). While it types, the ladder can fall to ``off``,
and the user can tap Home, Lang, Shift or Priv or send a ``recenter`` command: none of that may cut the run, change
the layout or leave a character out. The switch to the pinch fallback waits for the run's summary and happens in the
next frame; with no fallback a live session closes ``air_unreliable`` then.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable

from jarvis_hands.keyboard.limits import COMPOSE_MAX
from jarvis_hands.keyboard.rig import KbRig

TEXT = "".join("abcdefghij"[i % 10] for i in range(COMPOSE_MAX))
OFF_TEXT = "Air tap off: camera at 10 fps, using pinch"


def hooks(rig: KbRig, schedule: dict[int, Callable[[], None]]) -> None:
    """Run each action right after the desktop recorded the n-th character of the run."""
    rig.desktop.after_key = lambda n: schedule[n]() if n in schedule else None


def ladder_rig(**kw: object) -> KbRig:
    rig = KbRig(commit="review", **kw)  # type: ignore[arg-type]
    rig.arm()
    rig.run(0.5)
    rig.fill(TEXT)
    return rig


def start(rig: KbRig, low_fps_before_run_s: float | None = None) -> None:
    """The Insert triple; the camera reads 10 fps from ``low_fps_before_run_s`` before the third tap."""
    t0 = rig.t + 0.1
    rig.tap(t0, "insert")
    rig.tap(t0 + 1.0, "insert")
    rig.tap(t0 + 2.0, "insert")
    if low_fps_before_run_s is not None:
        rig.run(2.0 - low_fps_before_run_s + 0.1)
        rig.press.set_quality(10.0, 0.001)


def until_summary(rig: KbRig, limit_s: float = 15.0) -> None:
    end = rig.t + limit_s
    while not rig.summaries and rig.t < end and rig.closed is None:
        rig.run(rig.dt)
    assert rig.summaries


def test_x55_the_run_completes_while_the_ladder_falls_and_the_phase_keys_are_busy() -> None:
    rig = ladder_rig(fallback=True)
    banner_at: list[int] = []
    seen: dict[str, object] = {}

    def watch_banner() -> None:
        if rig.view is not None and rig.view.banner == OFF_TEXT and not banner_at:
            banner_at.append(len(rig.desktop.key_calls))

    def recenter() -> None:
        seen["recenter"] = rig.session.recenter()

    schedule: dict[int, Callable[[], None]] = {
        80: lambda: rig.tap(rig.t + rig.dt, "home"),
        90: lambda: rig.tap(rig.t + rig.dt, "lang"),
        100: lambda: rig.tap(rig.t + rig.dt, "shift"),
        110: recenter,
        120: lambda: rig.tap(rig.t + rig.dt, "private"),
    }
    hooks(rig, schedule)
    start(rig, low_fps_before_run_s=0.4)  # the ladder reads "off" some 50 characters into the run
    while not rig.summaries and rig.closed is None and rig.t < 60.0:
        rig.run(rig.dt)
        watch_banner()
        if not rig.summaries:
            assert rig.session.phase == "typing" and rig.session.armed and rig.session.press_name == "air"
    summary = rig.summaries[0]
    assert (summary.outcome, summary.sent, summary.of) == ("done", 200, 200)
    assert rig.typed == TEXT and len(rig.desktop.key_batches) == 200 and rig.box == ""  # no gap, no repeat
    assert banner_at and 41 <= banner_at[0] <= 70  # the banner said off from about character 50
    assert rig.counts["busy"] == 4  # Home, Lang, Shift and the recenter command
    assert rig.session.lang == "en" and not rig.session.shift and rig.session.private
    assert rig.session.phase == "typing" and rig.layout is rig.session._layout
    # the switch happens in the first frame after the summary, and nothing is typed after it
    assert rig.session.press_name == "air"
    rig.run(rig.dt)
    assert rig.session.press_name == "pinch" and rig.session.phase == "warmup" and not rig.session.armed
    assert len(rig.session._queue) == 0 and rig.box == ""
    assert rig.sink is not None and not rig.sink.run_active
    rig.run(3.0)
    assert len(rig.desktop.key_calls) == 200


def test_x55_a_focus_hold_before_the_ladder_is_off_aborts_the_run_keeps_the_rest_and_then_switches() -> None:
    rig = ladder_rig(fallback=True)
    original = rig.desktop.target
    other = dataclasses.replace(original, hwnd=777, pid=888)
    hooks(
        rig,
        {
            41: lambda: rig.press.set_quality(10.0, 0.001),
            70: lambda: setattr(rig.desktop, "target", other),
        },
    )
    start(rig)
    until_summary(rig)
    summary = rig.summaries[0]
    assert (summary.outcome, summary.reason, summary.sent) == ("aborted", "focus", 70)
    assert rig.typed == TEXT[:70] and rig.box == TEXT[70:] and rig.session.press_name == "air"
    rig.desktop.target = original
    rig.run(4.0)
    assert rig.session.press_name == "pinch" and rig.session.phase == "warmup" and not rig.session.armed
    assert rig.box == TEXT[70:] and len(rig.desktop.key_calls) == 70  # the remainder is kept, nothing more is typed


def test_x55_tick_goes_on_in_the_warmup_so_the_aborted_banner_ends_there_too() -> None:
    rig = ladder_rig(fallback=True)
    original = rig.desktop.target
    other = dataclasses.replace(original, hwnd=777, pid=888)
    hooks(rig, {31: lambda: rig.press.set_quality(10.0, 0.001), 42: lambda: setattr(rig.desktop, "target", other)})
    start(rig)
    until_summary(rig)
    rig.desktop.target = original
    rig.run(4.0)
    assert rig.session.phase == "warmup" and rig.session.review_state == "aborted"
    rig.run(5.0)
    assert rig.session.phase == "warmup" and rig.session.review_state == "composing"  # ABORT_SHOW_S in any phase


def test_x55_with_no_fallback_a_live_session_closes_air_unreliable_after_the_run_has_ended() -> None:
    rig = ladder_rig(fallback=False)
    start(rig, low_fps_before_run_s=0.4)
    while not rig.summaries and rig.closed is None and rig.t < 60.0:
        rig.run(rig.dt)
        assert rig.closed is None or rig.summaries
    assert rig.summaries[0].outcome == "done" and rig.typed == TEXT and rig.closed is None
    rig.run(rig.dt)
    assert rig.closed == "air_unreliable" and rig.session.discarded == 0
    assert len(rig.desktop.key_calls) == 200


def test_x55_with_no_fallback_an_aborted_run_leaves_its_remainder_to_be_counted_at_the_close() -> None:
    rig = ladder_rig(fallback=False)
    original = rig.desktop.target
    other = dataclasses.replace(original, hwnd=777, pid=888)
    hooks(rig, {41: lambda: rig.press.set_quality(10.0, 0.001), 70: lambda: setattr(rig.desktop, "target", other)})
    start(rig)
    until_summary(rig)
    rig.desktop.target = original
    rig.run(5.0)
    assert rig.closed == "air_unreliable" and rig.session.discarded == COMPOSE_MAX - 70
    assert rig.typed == TEXT[:70]
