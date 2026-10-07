from __future__ import annotations

import pytest

from jarvis_hands.desktop.base import Desktop, InputBlocked
from jarvis_hands.desktop.fake import FakeDesktop, FakeWindow, default_displays
from jarvis_hands.geometry import Rect

from scripted import display


def test_default_display_has_a_taskbar() -> None:
    (d,) = default_displays()
    assert d.primary and not d.virtual
    assert d.rect == Rect(0, 0, 1920, 1080) and d.work == Rect(0, 0, 1920, 1040)
    desktop = FakeDesktop()
    assert desktop.displays() == [d]
    assert desktop.cursor() == (960, 540)
    assert Desktop in type(desktop).__mro__


def test_cursor_is_clamped_into_the_nearest_display() -> None:
    desktop = FakeDesktop([display(1, 0, 0, 1920, 1080, primary=True), display(2, 1920, 0, 1280, 720)])
    desktop.move_cursor(2500, 900)  # below the shorter display
    assert desktop.cursor() == (2500, 719)
    desktop.move_cursor(-50, 500)
    assert desktop.cursor() == (0, 500)
    assert desktop.calls == [("move_cursor", 2500, 900), ("move_cursor", -50, 500)]


def test_move_mouse_is_the_user_and_is_not_logged() -> None:
    desktop = FakeDesktop()
    desktop.move_mouse(10, 20)
    assert desktop.cursor() == (10, 20)
    assert desktop.calls == []


def test_buttons_and_wheel() -> None:
    desktop = FakeDesktop()
    desktop.button("left", True)
    assert desktop.buttons_down == {"left"}
    desktop.button("left", False)
    desktop.scroll(-120)
    desktop.scroll(30, 12)
    assert desktop.buttons_down == set()
    assert (desktop.scroll_dy, desktop.scroll_dx) == (-90, 12)
    assert desktop.calls_named("scroll") == [("scroll", -120, 0), ("scroll", 30, 12)]


def test_window_at_returns_the_topmost_window_and_raise_reorders() -> None:
    back = FakeWindow(1, "back", Rect(0, 0, 1000, 800))
    front = FakeWindow(2, "front", Rect(500, 400, 800, 600))
    desktop = FakeDesktop(windows=[front, back])
    assert desktop.window_at(600, 500).handle == 2  # type: ignore[union-attr]
    assert desktop.window_at(100, 100).handle == 1  # type: ignore[union-attr]
    assert desktop.window_at(1800, 1000) is None
    desktop.raise_window(back.window)
    assert desktop.window_at(600, 500).handle == 1  # type: ignore[union-attr]
    assert desktop.calls == [("raise_window", 1)]


def test_window_rects_states_and_blocked_windows() -> None:
    w = FakeWindow(7, "Notes", Rect(100, 100, 800, 600))
    admin = FakeWindow(8, "Task Manager", Rect(1000, 100, 600, 500), blocked=True)
    desktop = FakeDesktop(windows=[w, admin])
    desktop.set_window_rect(w.window, Rect(200, 150, 700, 500))
    assert desktop.window_rect(w.window) == Rect(200, 150, 700, 500)
    with pytest.raises(InputBlocked):
        desktop.set_window_rect(admin.window, Rect(0, 0, 100, 100))
    assert desktop.window_rect(admin.window) == Rect(1000, 100, 600, 500)


def test_maximize_fills_the_work_area_and_restore_brings_the_rect_back() -> None:
    projector = display(2, 1920, 0, 1280, 720)
    w = FakeWindow(1, rect=Rect(2000, 100, 800, 500))
    desktop = FakeDesktop([display(1, 0, 0, 1920, 1080, primary=True), projector], [w])
    desktop.maximize(w.window)
    assert desktop.window_state(w.window) == "maximized"
    assert w.rect == projector.work
    desktop.restore(w.window)
    assert desktop.window_state(w.window) == "normal" and w.rect == Rect(2000, 100, 800, 500)


def test_minimize_hides_the_window_and_restore_returns_to_the_previous_state() -> None:
    w = FakeWindow(1, rect=Rect(100, 100, 800, 600))
    desktop = FakeDesktop(windows=[w])
    desktop.maximize(w.window)
    desktop.minimize(w.window)
    assert desktop.window_state(w.window) == "minimized"
    assert desktop.window_at(500, 500) is None
    desktop.restore(w.window)
    assert desktop.window_state(w.window) == "maximized"


def test_double_click_metrics_and_close() -> None:
    desktop = FakeDesktop(double_click=(0.4, 8, 6))
    assert desktop.double_click() == (0.4, 8, 6)
    desktop.close()
    assert desktop.closed and desktop.calls[-1] == ("close",)


def test_unknown_window_raises() -> None:
    with pytest.raises(KeyError):
        FakeDesktop().window(99)
