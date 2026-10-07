from __future__ import annotations

import itertools
import logging
import math
import time
from collections.abc import Callable

import pytest

from jarvis_hands import clock
from jarvis_hands import executor as executor_module
from jarvis_hands.actions import (
    Button,
    DragWindow,
    GrabWindow,
    MoveCursor,
    ReleaseAll,
    ReleaseWindow,
    ResizeWindow,
    Scroll,
    ThrowWindow,
)
from jarvis_hands.desktop.base import Display
from jarvis_hands.desktop.fake import FakeDesktop, FakeWindow
from jarvis_hands.executor import Executor
from jarvis_hands.geometry import Rect

from scripted import display

PRIMARY = display(1, 0, 0, 1920, 1080, primary=True)
PROJECTOR = display(2, 1920, 0, 1280, 720)


class Harness:
    def __init__(self, displays: list[Display] | None = None, windows: list[FakeWindow] | None = None) -> None:
        self.desktop = FakeDesktop(displays, windows, cursor=(0, 0))
        self.user_inputs = 0
        self.errors: list[tuple[str, str]] = []
        self.ex = Executor(
            self.desktop,
            displays=self.desktop.displays,
            on_user_input=self._user_input,
            on_error=lambda code, message: self.errors.append((code, message)),
        )

    def _user_input(self) -> None:
        self.user_inputs += 1

    def do(self, *actions: object, at: float = 0.0) -> None:
        self.ex.submit(list(actions), now=at)  # type: ignore[arg-type]
        self.ex.tick(at)

    def window(self, handle: int = 1) -> FakeWindow:
        return self.desktop.window(handle)


def test_cursor_glides_to_the_target_over_one_frame_interval() -> None:
    h = Harness()
    h.ex.submit([MoveCursor(300, 30)], now=10.0)
    h.ex.tick(10.0)
    assert h.desktop.cursor() == (0, 0)
    h.ex.tick(10.0 + 1 / 60)
    assert h.desktop.cursor() == (150, 15)
    h.ex.tick(10.0 + 1 / 30)
    assert h.desktop.cursor() == (300, 30)
    h.ex.tick(10.2)
    assert h.desktop.calls_named("move_cursor")[-1] == ("move_cursor", 300, 30)
    assert len(h.desktop.calls_named("move_cursor")) == 2


def test_a_new_target_glides_on_from_where_the_cursor_is() -> None:
    h = Harness()
    h.do(MoveCursor(300, 0), at=0.0)
    h.ex.tick(1 / 60)
    h.do(MoveCursor(300, 300), at=1 / 60)
    assert h.desktop.cursor() == (150, 0)
    h.ex.tick(1 / 60 + 1 / 60)
    assert h.desktop.cursor() == (225, 150)


def test_actions_with_a_point_snap_the_cursor_first() -> None:
    h = Harness()
    h.do(MoveCursor(500, 500), Button("left", True, 40, 50), at=0.0)
    assert h.desktop.calls == [("move_cursor", 40, 50), ("button", "left", True)]
    h.ex.tick(0.1)  # the glide towards 500, 500 was cancelled by the snap
    assert h.desktop.cursor() == (40, 50)
    assert h.ex.held == {"left"}


def test_buttons_are_tracked_and_never_doubled() -> None:
    h = Harness()
    h.do(Button("right", False, 10, 10))  # up without down: nothing to release
    h.do(Button("right", True, 10, 10), Button("right", True, 10, 10))
    assert h.desktop.calls_named("button") == [("button", "right", True)]
    h.do(Button("right", False, 12, 10))
    assert h.ex.held == frozenset()
    assert h.desktop.calls[-2:] == [("move_cursor", 12, 10), ("button", "right", False)]


def test_release_all_lifts_every_button_and_ends_the_grab() -> None:
    h = Harness(windows=[FakeWindow(1, rect=Rect(0, 0, 800, 600))])
    h.do(Button("left", True, 10, 10), GrabWindow(20, 20))
    assert h.ex.grabbing and h.ex.held == {"left"}
    h.do(ReleaseAll())
    assert h.ex.held == frozenset() and not h.ex.grabbing
    assert h.desktop.buttons_down == set()
    h.do(DragWindow(300, 300))
    assert h.window().rect == Rect(0, 0, 800, 600)


def test_stop_releases_and_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    registered: list[object] = []
    unregistered: list[object] = []
    monkeypatch.setattr(executor_module.atexit, "register", registered.append)
    monkeypatch.setattr(executor_module.atexit, "unregister", unregistered.append)
    h = Harness()
    h.ex.start()
    assert registered == [h.ex.stop]
    h.ex.submit([Button("left", True, 5, 5)])
    deadline = time.monotonic() + 2
    while h.ex.held != {"left"} and time.monotonic() < deadline:
        time.sleep(0.005)
    assert h.desktop.buttons_down == {"left"}
    h.ex.stop()
    assert h.desktop.buttons_down == set()
    assert unregistered == [h.ex.stop]
    h.ex.stop()
    assert h.desktop.calls_named("button") == [("button", "left", True), ("button", "left", False)]


def test_the_thread_glides_the_cursor_in_real_time() -> None:
    h = Harness()
    h.ex.frame_interval = 0.4  # long enough for several ticks even where sleeps overshoot by tens of ms
    h.ex.start()
    try:
        h.ex.submit([MoveCursor(400, 300)])
        deadline = time.perf_counter() + 3
        while h.desktop.cursor() != (400, 300) and time.perf_counter() < deadline:
            time.sleep(0.005)
        assert h.desktop.cursor() == (400, 300)
        assert len(h.desktop.calls_named("move_cursor")) >= 2  # interpolated, not one jump
    finally:
        h.ex.stop()


class FlakyDesktop(FakeDesktop):
    def __init__(self) -> None:
        super().__init__(cursor=(0, 0))
        self.fail_scroll = True

    def scroll(self, dy: int, dx: int = 0) -> None:
        if self.fail_scroll:
            raise OSError("SendInput failed")
        super().scroll(dy, dx)


def test_an_exception_in_a_tick_releases_everything_and_does_not_raise() -> None:
    desktop = FlakyDesktop()
    ex = Executor(desktop, displays=desktop.displays)
    ex.submit([Button("left", True, 10, 10), Scroll(-240, 0, 10, 10)], now=0.0)
    ex.tick(0.0)
    assert ex.held == frozenset()
    assert desktop.calls_named("button") == [("button", "left", True), ("button", "left", False)]
    desktop.fail_scroll = False
    ex.submit([Scroll(-240, 0, 10, 10)], now=0.1)
    ex.tick(0.1)
    assert desktop.scroll_dy == -240


def test_the_real_mouse_wins() -> None:
    h = Harness()
    h.do(MoveCursor(200, 200), at=0.0)
    h.ex.tick(0.05)
    h.do(Button("left", True, 200, 200), at=0.06)
    h.desktop.move_mouse(260, 230)
    h.ex.tick(0.07)
    assert h.user_inputs == 1
    assert h.ex.held == frozenset() and h.desktop.buttons_down == set()
    # Stale actions computed before the engine heard about it are dropped for a moment...
    h.do(MoveCursor(900, 900), Button("left", True, 900, 900), at=0.08)
    h.ex.tick(0.2)
    assert h.desktop.cursor() == (260, 230) and h.ex.held == frozenset()
    h.desktop.move_mouse(300, 300)
    h.ex.tick(0.25)
    assert h.user_inputs == 1  # we do not own the cursor any more: the user's own moves are not reported
    # ...and a later re-engagement works again.
    h.do(MoveCursor(400, 400), at=1.0)
    h.ex.tick(1.1)
    assert h.desktop.cursor() == (400, 400)


def test_small_wobbles_of_the_real_mouse_are_tolerated() -> None:
    h = Harness()
    h.do(MoveCursor(200, 200), at=0.0)
    h.ex.tick(0.05)
    h.desktop.move_mouse(204, 203)  # 5 px
    h.ex.tick(0.06)
    assert h.user_inputs == 0


def test_the_real_mouse_wins_while_the_hand_moves_the_cursor() -> None:
    h = Harness()
    t = 0.0
    for frame in range(30):  # the hand moves the cursor right, 4 px a frame
        h.ex.submit([MoveCursor(100 + 4 * frame, 100)], now=t)
        for k in range(4):
            if frame >= 10:  # the user takes the mouse and moves it down slowly: 2 px a tick, 240 px/s
                x, y = h.desktop.cursor()
                h.desktop.move_mouse(x, y + 2)
            h.ex.tick(t + 0.004 + k / 120)
            if h.user_inputs:
                break
        if h.user_inputs:
            break
        t += 1 / 30
    assert h.user_inputs == 1
    assert t < 0.4  # within a few ticks of the user starting
    assert h.desktop.cursor()[1] > 100  # what the user did stays done


def test_a_gliding_cursor_on_a_rattling_desk_is_not_the_user() -> None:
    h = Harness()
    t = 0.0
    for frame in range(60):  # a fast sweep while the mouse rattles a pixel to and fro under it
        h.ex.submit([MoveCursor(100 + 30 * frame, 300)], now=t)
        for k in range(4):
            x, y = h.desktop.cursor()
            h.desktop.move_mouse(x, y + (1 if k % 2 else -1))
            h.ex.tick(t + 0.004 + k / 120)
        t += 1 / 30
    assert h.user_inputs == 0


def test_a_press_lands_on_its_point_after_a_nudge_too_small_to_be_the_user() -> None:
    h = Harness()
    h.do(MoveCursor(500, 500), at=0.0)
    h.ex.tick(0.05)
    h.desktop.move_mouse(504, 503)  # 5 px: a bumped desk, optical drift
    h.do(Button("left", True, 500, 500), at=0.1)
    assert h.user_inputs == 0
    assert h.desktop.calls[-2:] == [("move_cursor", 500, 500), ("button", "left", True)]
    assert h.desktop.cursor() == (500, 500)


class LockedDesktop(FakeDesktop):
    """GetCursorPos fails with access denied while the secure desktop is up (Win+L, a UAC prompt)."""

    locked = False

    def cursor(self) -> tuple[int, int]:
        if self.locked:
            raise OSError(5, "Access is denied")
        return super().cursor()


def test_a_tick_that_keeps_failing_logs_once_a_minute_and_replays_nothing(caplog: pytest.LogCaptureFixture) -> None:
    desktop = LockedDesktop(cursor=(0, 0))
    ex = Executor(desktop, displays=desktop.displays)
    ex.submit([MoveCursor(300, 300)], now=0.0)
    for k in range(10):
        ex.tick(k / 120)
    desktop.locked = True
    ex.submit([Button("left", True, 300, 300)], now=1.0)
    with caplog.at_level(logging.INFO, logger="jarvis_hands.executor"):
        for k in range(120 * 90):  # 90 s locked
            ex.tick(1.0 + k / 120)
        desktop.locked = False
        ex.tick(100.0)
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 2  # the first failure with its traceback, then one reminder a minute later
    assert errors[0].exc_info and not errors[1].exc_info
    assert "works again" in caplog.text
    # What was queued while it failed is not replayed on unlock.
    assert desktop.buttons_down == set() and desktop.calls_named("button") == []


class SimulatedTime:
    """A clock and a sleep for ``Executor._run``: each sleep takes ``oversleep(requested)`` simulated seconds."""

    def __init__(self, oversleep: Callable[[float], float] = lambda d: d) -> None:
        self.t = 0.0
        self.oversleep = oversleep

    def clock(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        assert seconds > 0
        self.t += self.oversleep(seconds)


def tick_times(sim: SimulatedTime, count: int = 200, tick_s: Callable[[int], float] = lambda i: 0.0) -> list[float]:
    """Runs the loop on this thread for ``count`` ticks; tick ``i`` takes ``tick_s(i)`` seconds."""
    ex = Executor(FakeDesktop(), displays=list, clock=sim.clock, sleep=sim.sleep)
    times: list[float] = []

    def tick(now: float | None = None) -> None:
        times.append(sim.t)
        sim.t += tick_s(len(times))
        if len(times) == count:
            ex._stop_event.set()

    ex.tick = tick  # type: ignore[method-assign]
    ex._run()
    return times


PERIOD = 1 / 120


def gaps_of(times: list[float]) -> list[float]:
    return [b - a for a, b in itertools.pairwise(times)]


def test_the_loop_ticks_at_its_rate() -> None:
    gaps = gaps_of(tick_times(SimulatedTime()))
    assert all(gap == pytest.approx(PERIOD) for gap in gaps)


def test_sleeps_that_overshoot_a_little_do_not_slow_the_rate() -> None:
    gaps = gaps_of(tick_times(SimulatedTime(lambda d: d + 0.003)))
    assert sum(gaps) / len(gaps) == pytest.approx(PERIOD, rel=0.01)
    assert min(gaps) > PERIOD / 2


@pytest.mark.parametrize(
    "oversleep",
    [
        pytest.param(lambda d: math.ceil(d / 0.0156) * 0.0156, id="windows-timer-tick"),
        pytest.param(lambda d: d + 0.040, id="tens-of-ms-late"),
    ],
)
def test_after_a_long_oversleep_missed_ticks_are_dropped_not_sent_back_to_back(
    oversleep: Callable[[float], float],
) -> None:
    gaps = gaps_of(tick_times(SimulatedTime(oversleep)))
    assert min(gaps) >= PERIOD / 2 - 1e-12


def test_a_slow_tick_is_not_followed_by_catch_up_ticks() -> None:
    gaps = gaps_of(tick_times(SimulatedTime(), tick_s=lambda i: 0.050 if i % 20 == 0 else 0.0))
    assert min(gaps) >= PERIOD / 2 - 1e-12
    assert max(gaps) == pytest.approx(0.050)


def test_the_thread_waits_with_sleep_not_a_timed_event_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows rounds Event and lock wait timeouts up to its 15.6 ms timer tick (64 Hz); time.sleep is precise."""
    h = Harness()
    stop_event = h.ex._stop_event
    timed_waits: list[float] = []
    real_wait = stop_event.wait

    def wait(timeout: float | None = None) -> bool:
        if timeout:
            timed_waits.append(timeout)
        return real_wait(timeout)

    monkeypatch.setattr(stop_event, "wait", wait)
    ticks: list[float] = []
    real_tick = h.ex.tick

    def counting_tick(now: float | None = None) -> None:
        ticks.append(time.perf_counter())
        real_tick(now)

    monkeypatch.setattr(h.ex, "tick", counting_tick)
    h.ex.start()
    try:
        time.sleep(0.5)
    finally:
        h.ex.stop()
    assert timed_waits == []
    assert len(ticks) >= 5  # loose: CI machines oversleep by tens of ms; the rate itself is tested above
    assert min(gaps_of(ticks)) > PERIOD / 4  # never back to back


def test_the_clock_is_fine_grained() -> None:
    """Python 3.12's time.monotonic moves in 15.6 ms steps on Windows; everything times itself with clock.now."""
    assert clock.now is time.perf_counter
    assert time.get_clock_info("perf_counter").resolution < 1e-4
    assert Executor(FakeDesktop(), displays=list)._clock is clock.now


def test_os_clamping_between_monitors_is_not_the_user() -> None:
    h = Harness([PRIMARY, PROJECTOR])
    h.do(MoveCursor(1800, 1000), at=0.0)
    h.ex.tick(0.05)
    h.do(MoveCursor(2600, 1000), at=0.1)  # the straight path crosses the gap under the projector
    for k in range(1, 6):
        h.ex.tick(0.1 + k / 120)
    assert h.user_inputs == 0
    assert h.desktop.cursor() == (2600, 719)


def test_scroll_accumulates_fractions() -> None:
    h = Harness()
    h.do(Scroll(-0.6, 0.0, 100, 100), Scroll(-0.6, 0.4, 100, 100), Scroll(-0.6, 0.7, 100, 100))
    assert h.desktop.calls_named("scroll") == [("scroll", -1, 0), ("scroll", 0, 1)]
    assert h.desktop.cursor() == (100, 100)


# -- windows ---------------------------------------------------------------------------


def test_grab_and_drag_moves_the_window_by_the_cursor_delta() -> None:
    w = FakeWindow(1, "Notes", Rect(100, 100, 800, 600))
    other = FakeWindow(2, "Other", Rect(50, 50, 400, 300))
    h = Harness(windows=[other, w])
    h.do(GrabWindow(600, 500))
    assert h.desktop.calls_named("raise_window") == [("raise_window", 1)]
    for i, (x, y) in enumerate([(650, 520), (700, 560), (720, 580)]):
        h.do(DragWindow(x, y), at=0.1 * (i + 1))
    assert w.rect == Rect(220, 180, 800, 600)
    assert len(h.desktop.calls_named("raise_window")) == 1
    h.do(ReleaseWindow())
    h.do(DragWindow(900, 900))
    assert w.rect == Rect(220, 180, 800, 600)


def test_grabbing_the_desktop_does_nothing() -> None:
    w = FakeWindow(1, rect=Rect(100, 100, 400, 300))
    h = Harness(windows=[w])
    h.do(GrabWindow(1500, 900), DragWindow(1600, 950), ThrowWindow("up"), ReleaseWindow())
    assert w.rect == Rect(100, 100, 400, 300) and w.state == "normal"
    assert not [c for c in h.desktop.calls if c[0] != "move_cursor"]


def test_a_maximized_window_is_restored_on_the_first_real_drag_like_windows_does() -> None:
    w = FakeWindow(1, rect=PRIMARY.work, state="maximized", normal_rect=Rect(300, 200, 1000, 700))
    h = Harness([PRIMARY], [w])
    h.do(GrabWindow(1440, 300))
    h.do(DragWindow(1443, 301))  # a tremor: nothing yet
    assert w.state == "maximized"
    h.do(DragWindow(1460, 320))
    assert w.state == "normal"
    # The cursor keeps its relative x (3/4 across) and sits at most 40 px below the top edge.
    assert w.rect == Rect(1460 - 750, 320 - 40, 1000, 700)
    h.do(DragWindow(1500, 340))
    assert w.rect == Rect(750, 300, 1000, 700)


def test_releasing_a_maximized_window_without_moving_changes_nothing() -> None:
    w = FakeWindow(1, rect=PRIMARY.work, state="maximized", normal_rect=Rect(300, 200, 1000, 700))
    h = Harness([PRIMARY], [w])
    h.do(GrabWindow(900, 500), DragWindow(902, 501), ReleaseWindow())
    assert w.state == "maximized" and w.rect == PRIMARY.work


def test_two_hand_resize() -> None:
    w = FakeWindow(1, rect=Rect(500, 300, 800, 500))
    h = Harness([PRIMARY], [w])
    h.do(GrabWindow(800, 500))
    h.do(ResizeWindow(800, 500, 1000, 500))
    assert w.rect == Rect(500, 300, 800, 500)  # the first one only starts the resize
    h.do(ResizeWindow(750, 480, 1150, 540))  # 200 px further apart sideways, 60 px vertically, midpoint +50, +10
    assert w.rect == Rect(500 - 100 + 50, 300 - 30 + 10, 1000, 560)


def test_resize_keeps_the_minimum_size_and_stays_on_the_displays() -> None:
    w = FakeWindow(1, rect=Rect(500, 300, 800, 500))
    h = Harness([PRIMARY], [w])
    h.do(GrabWindow(800, 500), ResizeWindow(600, 500, 1000, 500))
    h.do(ResizeWindow(790, 500, 810, 500))
    assert (w.rect.width, w.rect.height) == (420, 500)
    h.do(ResizeWindow(800, 500, 800, 500))
    assert w.rect.width == 400
    h.do(ResizeWindow(-2000, -1000, 3000, 2000))
    assert w.rect == PRIMARY.work


def test_resize_minimum_size() -> None:
    w = FakeWindow(1, rect=Rect(500, 300, 300, 200))
    h = Harness([PRIMARY], [w])
    h.do(GrabWindow(600, 400), ResizeWindow(500, 300, 700, 500), ResizeWindow(600, 400, 600, 400))
    assert (w.rect.width, w.rect.height) == (240, 160)


def test_throw_right_lands_on_the_next_display_scaled_to_its_work_area() -> None:
    w = FakeWindow(1, rect=Rect(960, 520, 480, 260))  # half way across, half way down the primary's work area
    h = Harness([PRIMARY, PROJECTOR], [w])
    h.do(GrabWindow(1000, 600), ThrowWindow("right"))
    sx, sy = 1280 / 1920, 680 / 1040
    assert w.rect == Rect(*Rect(1920 + 960 * sx, 520 * sy, 480 * sx, 260 * sy).rounded())
    assert not h.ex.grabbing


def test_throw_left_from_the_projector_comes_back() -> None:
    w = FakeWindow(1, rect=Rect(2000, 100, 640, 340))
    h = Harness([PRIMARY, PROJECTOR], [w])
    h.do(GrabWindow(2100, 200), ThrowWindow("left"))
    assert PRIMARY.work.contains(w.rect.center)
    assert w.rect.width == pytest.approx(640 * 1920 / 1280, abs=1)


def test_throw_with_no_display_that_way_snaps_to_that_half() -> None:
    w = FakeWindow(1, rect=Rect(300, 200, 700, 500))
    h = Harness([PRIMARY], [w])
    h.do(GrabWindow(500, 300), ThrowWindow("right"))
    assert w.rect == Rect(960, 0, 960, 1040)
    h.do(GrabWindow(1200, 300), ThrowWindow("left"))
    assert w.rect == Rect(0, 0, 960, 1040)


def test_throw_up_maximizes_and_down_minimizes() -> None:
    w = FakeWindow(1, rect=Rect(300, 200, 700, 500))
    h = Harness([PRIMARY], [w])
    h.do(GrabWindow(500, 300), ThrowWindow("up"))
    assert w.state == "maximized" and w.rect == PRIMARY.work
    h.do(GrabWindow(500, 300), ThrowWindow("down"))
    assert w.state == "minimized"


def test_a_maximized_window_thrown_sideways_is_maximized_on_the_other_display() -> None:
    w = FakeWindow(1, rect=PRIMARY.work, state="maximized", normal_rect=Rect(300, 200, 1000, 700))
    h = Harness([PRIMARY, PROJECTOR], [w])
    h.do(GrabWindow(900, 20), DragWindow(1000, 40), ThrowWindow("right"))
    assert w.state == "maximized" and w.rect == PROJECTOR.work


def test_a_blocked_window_reports_once_and_the_grab_becomes_a_no_op() -> None:
    admin = FakeWindow(5, "Task Manager", Rect(100, 100, 600, 400), blocked=True)
    h = Harness([PRIMARY], [admin])
    h.do(GrabWindow(300, 300), DragWindow(350, 350), DragWindow(400, 400))
    assert admin.rect == Rect(100, 100, 600, 400)
    assert len(h.errors) == 1 and h.errors[0][0] == "input_blocked" and "Task Manager" in h.errors[0][1]
    h.do(ReleaseWindow(), GrabWindow(300, 300), DragWindow(350, 350), ThrowWindow("down"))
    assert len(h.errors) == 1
    assert admin.state == "normal"
