from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from jarvis_hands.desktop import fake as fake_module
from jarvis_hands.desktop.base import (
    KEY_DESKTOP_NAMES,
    Desktop,
    InputBlocked,
    KeyTarget,
    as_key_desktop,
)
from jarvis_hands.desktop.fake import FakeDesktop, FakeWindow, default_displays
from jarvis_hands.desktop.keys import KEYEVENTF_KEYUP, KEYEVENTF_UNICODE, KeyRefused, KeyStroke
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


# --------------------------------------------------------------------------- the keyboard side (DESIGN 3.5, W40)


def char(c: str) -> KeyStroke:
    return KeyStroke("char", c)


def control(name: str) -> KeyStroke:
    return KeyStroke("control", name)


def test_key_target_defaults() -> None:
    desktop = FakeDesktop()
    assert desktop.target == KeyTarget(100, 200, "FakeTerminal", 0x0409, None, False, False)
    assert desktop.injects_for_real is False
    assert FakeDesktop(injects_for_real=True).injects_for_real is True
    assert desktop.key_target() is desktop.target
    assert desktop.foreground_window() == 100


def test_key_target_fields_and_its_name_stays_off_the_screen_of_logs() -> None:
    names = [f.name for f in dataclasses.fields(KeyTarget)]
    assert names == ["hwnd", "pid", "name", "lang_id", "blocked", "password", "covered"]
    target = KeyTarget(1, 2, "secret-tool", 0x040D, "elevated", True, True)
    with pytest.raises(dataclasses.FrozenInstanceError):
        target.hwnd = 5  # type: ignore[misc]
    # the executable name is for the local screen only: never in a log line made with %r or %s
    assert "secret-tool" not in repr(target) and "secret-tool" not in str(target)


def test_the_constructor_still_takes_the_old_arguments_in_the_old_order() -> None:
    w = FakeWindow(1)
    desktop = FakeDesktop(default_displays(), [w], cursor=(5, 6), double_click=(0.4, 8, 6), injects_for_real=True)
    assert desktop.windows == [w] and desktop.cursor() == (5, 6) and desktop.double_click() == (0.4, 8, 6)


def test_send_keys_records_each_stroke_as_one_balanced_batch() -> None:
    desktop = FakeDesktop()
    strokes = [char("a"), control("space"), char("\u05e9"), control("enter"), control("backspace"), char("Z")]
    assert desktop.send_keys(strokes) == 6
    assert desktop.key_calls == [
        ("char", "a"),
        ("control", "space"),
        ("char", "\u05e9"),
        ("control", "enter"),
        ("control", "backspace"),
        ("char", "Z"),
    ]
    assert len(desktop.key_batches) == 6  # atomic: one batch per stroke, never one for several
    assert desktop.key_batches[0] == [
        (0, ord("a"), KEYEVENTF_UNICODE),
        (0, ord("a"), KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
    ]
    assert desktop.key_batches[1] == [(0x20, 0x39, 0), (0x20, 0x39, KEYEVENTF_KEYUP)]
    assert desktop.typed_text == "a \u05e9\n\bZ"
    assert desktop.calls == []  # the pointer log is unchanged by the keyboard (the differential tests rely on it)


def test_no_keyboard_method_writes_to_the_pointer_log() -> None:
    """``calls`` is what the pointer tests compare; every call of the key lane stays out of it, whatever it does."""
    desktop = FakeDesktop(injects_for_real=True)
    desktop.key_target()
    desktop.foreground_window()
    desktop.send_keys([char("a"), control("enter")])
    desktop.send_keys([char("b")], inject="vk")
    desktop.user_typed()
    desktop.user_moved_mouse()
    desktop.foreign_input()
    desktop.modifiers_down()
    desktop.open_os_keyboard()
    desktop.release_keys()
    desktop.fail_keys = 1
    with pytest.raises(InputBlocked):
        desktop.send_keys([char("c")])
    desktop.partial_keys = 1
    with pytest.raises(OSError, match="part of the batch"):
        desktop.send_keys([char("d")])
    desktop.raise_after = RuntimeError("fake")
    with pytest.raises(RuntimeError):
        desktop.send_keys([char("e")])
    assert desktop.typed_text == "a\nbde"  # the lane did record, so the empty log is not an unused fake
    assert desktop.calls == [] and desktop.calls_named("move_cursor") == []


def test_typed_text_is_derived_from_the_recorded_strokes() -> None:
    desktop = FakeDesktop()
    assert desktop.typed_text == ""
    desktop.send_keys([char("h"), char("i")])
    desktop.send_keys([control("space")])
    desktop.send_keys([char("?")])
    assert desktop.typed_text == "hi ?"
    desktop.key_calls.clear()
    assert desktop.typed_text == ""


def test_vk_injection_uses_the_lookup_the_test_gives() -> None:
    desktop = FakeDesktop()
    desktop.send_keys([char("a")], inject="vk")  # no lookup: falls back to the Unicode events, like Windows
    assert desktop.key_batches[-1][0][2] & KEYEVENTF_UNICODE
    desktop.vk_lookup = lambda c: (0x41, 0, 0x1E) if c == "a" else None
    desktop.send_keys([char("a")], inject="vk")
    assert desktop.key_batches[-1] == [(0x41, 0x1E, 0), (0x41, 0x1E, KEYEVENTF_KEYUP)]


def test_the_allow_list_still_applies_to_a_tampered_stroke() -> None:
    desktop = FakeDesktop()
    bad = char("a")
    object.__setattr__(bad, "value", "5")
    with pytest.raises(KeyRefused):
        desktop.send_keys([char("x"), bad, char("y")])
    assert desktop.key_calls == [("char", "x")]  # what was sent before the refused stroke stays sent, nothing after


def test_an_unbalanced_batch_is_a_failure_of_the_double() -> None:
    assert fake_module.events_balanced([(0, 97, KEYEVENTF_UNICODE), (0, 97, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)])
    assert not fake_module.events_balanced([(0, 97, KEYEVENTF_UNICODE)])
    assert not fake_module.events_balanced([(0, 97, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)])
    assert not fake_module.events_balanced(
        [(0x41, 0x1E, KEYEVENTF_KEYUP), (0x41, 0x1E, 0)]  # an up before its down
    )
    assert fake_module.events_balanced(
        [(0xA0, 0x2A, 0), (0x41, 0x1E, 0), (0x41, 0x1E, KEYEVENTF_KEYUP), (0xA0, 0x2A, KEYEVENTF_KEYUP)]
    )


def test_a_stroke_whose_events_do_not_balance_is_never_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fake_module, "events_for", lambda stroke, **kwargs: [(0, 97, KEYEVENTF_UNICODE)])
    desktop = FakeDesktop()
    with pytest.raises(AssertionError):
        desktop.send_keys([char("a")])
    assert desktop.key_calls == [] and desktop.key_batches == []


def test_fail_keys_refuses_the_next_sends_and_records_nothing() -> None:
    desktop = FakeDesktop()
    desktop.fail_keys = 2
    for _ in range(2):
        with pytest.raises(InputBlocked):
            desktop.send_keys([char("a")])
    assert desktop.key_calls == [] and desktop.key_batches == [] and desktop.fail_keys == 0
    assert desktop.send_keys([char("a")]) == 1
    assert desktop.key_calls == [("char", "a")]


def test_after_key_runs_after_every_recorded_stroke_with_the_running_count() -> None:
    desktop = FakeDesktop()
    seen: list[tuple[int, list[tuple[str, str]]]] = []
    desktop.after_key = lambda n: seen.append((n, list(desktop.key_calls)))
    desktop.send_keys([char("a"), char("b")])
    desktop.send_keys([char("c")])
    assert seen == [
        (1, [("char", "a")]),
        (2, [("char", "a"), ("char", "b")]),
        (3, [("char", "a"), ("char", "b"), ("char", "c")]),
    ]


def test_after_key_can_change_the_world_between_two_characters_of_a_run() -> None:
    desktop = FakeDesktop()

    def switch_window(n: int) -> None:
        if n == 2:
            desktop.target = dataclasses.replace(desktop.target, hwnd=999, pid=998)
            desktop.user_typed()
        if n == 3:
            desktop.modifiers = True

    desktop.after_key = switch_window
    desktop.send_keys([char("a")])
    assert desktop.foreign_input() is False and desktop.foreground_window() == 100
    desktop.send_keys([char("b")])
    assert desktop.foreground_window() == 999 and desktop.key_target().pid == 998
    assert desktop.foreign_input() is True
    assert desktop.modifiers_down() is False
    desktop.send_keys([char("c")])
    assert desktop.modifiers_down() is True


def test_after_key_is_not_called_for_a_refused_send() -> None:
    desktop = FakeDesktop()
    calls: list[int] = []
    desktop.after_key = calls.append
    desktop.fail_keys = 1
    with pytest.raises(InputBlocked):
        desktop.send_keys([char("a")])
    assert calls == []


def test_partial_keys_records_the_stroke_and_then_raises_os_error() -> None:
    desktop = FakeDesktop()
    desktop.partial_keys = 2
    for letter in "ab":
        with pytest.raises(OSError) as caught:
            desktop.send_keys([char(letter)])
        assert not isinstance(caught.value, InputBlocked)
    assert desktop.key_calls == [("char", "a"), ("char", "b")]  # a partly taken batch is still on screen
    assert desktop.partial_keys == 0
    assert desktop.send_keys([char("c")]) == 1  # the next call is normal
    assert desktop.typed_text == "abc"
    assert all(fake_module.events_balanced(batch) for batch in desktop.key_batches)


def test_partial_keys_still_calls_after_key_for_the_recorded_stroke() -> None:
    desktop = FakeDesktop()
    counts: list[int] = []
    desktop.after_key = counts.append
    desktop.partial_keys = 1
    with pytest.raises(OSError):
        desktop.send_keys([char("a")])
    assert counts == [1]


def test_raise_after_delivers_the_stroke_and_then_raises_the_given_exception() -> None:
    desktop = FakeDesktop()
    boom = RuntimeError("unexpected")
    desktop.raise_after = boom
    with pytest.raises(RuntimeError) as caught:
        desktop.send_keys([char("a")])
    assert caught.value is boom
    assert desktop.key_calls == [("char", "a")] and desktop.raise_after is None
    assert desktop.send_keys([char("b")]) == 1  # only the next call fails
    assert desktop.typed_text == "ab"


def test_fail_keys_wins_over_partial_and_raise_after() -> None:
    desktop = FakeDesktop()
    desktop.fail_keys = 1
    desktop.partial_keys = 1
    desktop.raise_after = ValueError("x")
    with pytest.raises(InputBlocked):
        desktop.send_keys([char("a")])
    assert desktop.key_calls == []
    assert desktop.partial_keys == 1 and isinstance(desktop.raise_after, ValueError)


def test_foreign_input_reports_what_happened_since_the_previous_call() -> None:
    desktop = FakeDesktop()
    assert desktop.foreign_input() is False
    desktop.user_typed()
    assert desktop.foreign_events == 1
    assert desktop.foreign_input() is True
    assert desktop.foreign_input() is False
    desktop.user_moved_mouse()
    desktop.user_typed()
    assert desktop.foreign_events == 3
    assert desktop.foreign_input() is True  # two events, one answer
    assert desktop.foreign_input() is False


def test_our_own_keys_are_not_foreign_input() -> None:
    desktop = FakeDesktop()
    desktop.foreign_input()
    desktop.send_keys([char("a")])
    assert desktop.foreign_input() is False


def test_the_other_keyboard_questions() -> None:
    desktop = FakeDesktop()
    assert desktop.modifiers_down() is False
    desktop.modifiers = True
    assert desktop.modifiers_down() is True
    assert (desktop.release_keys_calls, desktop.os_keyboard_opened) == (0, 0)
    desktop.release_keys()
    desktop.release_keys()
    assert desktop.release_keys_calls == 2
    assert desktop.open_os_keyboard() is True
    assert desktop.os_keyboard_opened == 1
    desktop.os_keyboard_starts = False
    assert desktop.open_os_keyboard() is False
    assert desktop.os_keyboard_opened == 2


def test_the_fake_has_every_name_of_the_key_desktop() -> None:
    assert KEY_DESKTOP_NAMES == (
        "injects_for_real",
        "key_target",
        "foreground_window",
        "send_keys",
        "foreign_input",
        "modifiers_down",
        "release_keys",
        "open_os_keyboard",
    )
    desktop = FakeDesktop()
    assert as_key_desktop(desktop) is desktop
    assert Desktop in type(desktop).__mro__


class PointerOnly:
    """What a desktop without keys looks like (everything the Desktop protocol has, nothing of the keyboard)."""

    def displays(self) -> list[Any]:
        return []


def test_as_key_desktop_needs_every_name() -> None:
    assert as_key_desktop(PointerOnly()) is None
    assert as_key_desktop(None) is None
    assert as_key_desktop(object()) is None
    for missing in KEY_DESKTOP_NAMES:
        partial = type("Partial", (), {name: (lambda self: None) for name in KEY_DESKTOP_NAMES if name != missing})()
        assert as_key_desktop(partial) is None, missing
    full = type("Full", (), {name: (lambda self: None) for name in KEY_DESKTOP_NAMES})()
    assert as_key_desktop(full) is full


def test_the_desktop_protocol_itself_is_not_extended() -> None:
    """WindowsDesktop and FakeDesktop subclass Desktop explicitly: a method added to it would inherit an empty body."""
    for name in KEY_DESKTOP_NAMES:
        assert not hasattr(Desktop, name), name
