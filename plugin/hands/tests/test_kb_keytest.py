"""``keytest``: the one-minute check of the keyboard's injection path (DESIGN-KEYBOARD.md 3.13, L2).

Everything runs against ``FakeDesktop(injects_for_real=True)`` with an injected sleep and clock, so there is no real
waiting and nothing is typed anywhere. The console output is fixed text, counts and the local program name: never an
exception text (SR13).
"""

from __future__ import annotations

import argparse
import inspect
import io
import itertools
import logging
import math
from collections.abc import Sequence
from typing import Any, Literal

import pytest

from jarvis_hands import clock, desktop
from jarvis_hands.desktop.base import InputBlocked, KeyTarget, UnsupportedPlatform
from jarvis_hands.desktop.fake import FakeDesktop
from jarvis_hands.desktop.keys import ALLOWED_CHARS, KeyRefused, KeyStroke
from jarvis_hands.keyboard import keytest
from jarvis_hands.keyboard.limits import INSERT_GAP_S

LINE = "abc ABC .,'-?/ ok"
HEBREW = "שלום"
SENTINEL = "q-sentinel-q"  # inside an exception text that must never be printed or logged


def parse(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="keytest")
    keytest.add_arguments(parser)
    return parser.parse_args(list(argv))


class SpyDesktop(FakeDesktop):
    """A fake desktop that writes what happens, in order, to a shared list (sleeps and probes land there too)."""

    def __init__(self, events: list[tuple[Any, ...]], **kwargs: Any) -> None:
        super().__init__(injects_for_real=True, **kwargs)
        self.events = events
        self.reads = 0

    def key_target(self) -> KeyTarget:
        self.reads += 1
        self.events.append(("probe",))
        return super().key_target()

    def send_keys(self, strokes: Sequence[KeyStroke], *, inject: Literal["unicode", "vk"] = "unicode") -> int:
        self.events.append(("keys", inject, [(s.kind, s.value) for s in strokes]))
        return super().send_keys(strokes, inject=inject)


class Probe:
    """sleep, clock and output of one run; the clock times ``key_target`` with the durations it was given."""

    def __init__(self, durations: Sequence[float] = (0.0005,), *, events: list[tuple[Any, ...]] | None = None) -> None:
        self.events = events if events is not None else []
        self.out = io.StringIO()
        self.durations = list(durations)
        self._now = 1000.0
        self._reads = 0
        self._open = False

    def sleep(self, seconds: float) -> None:
        self.events.append(("sleep", seconds))

    def clock(self) -> float:
        # two calls per timed read: the second returns the first plus that read's duration
        if not self._open:
            self._open = True
            return self._now
        self._open = False
        step = self.durations[self._reads % len(self.durations)]
        self._reads += 1
        self._now += step + 1.0
        return self._now - 1.0

    @property
    def text(self) -> str:
        return self.out.getvalue()


def go(
    args: argparse.Namespace,
    fake: FakeDesktop,
    probe: Probe | None = None,
    **extra: Any,
) -> tuple[int, Probe]:
    probe = probe or Probe()
    code = keytest.run(args, desktop=fake, sleep=probe.sleep, clock=probe.clock, out=probe.out, **extra)
    return code, probe


def spy(probe: Probe | None = None, **kwargs: Any) -> tuple[SpyDesktop, Probe]:
    probe = probe or Probe()
    return SpyDesktop(probe.events, **kwargs), probe


def key_events(probe: Probe) -> list[tuple[Any, ...]]:
    return [e for e in probe.events if e[0] == "keys"]


def sleeps(probe: Probe) -> list[float]:
    return [e[1] for e in probe.events if e[0] == "sleep"]


def spelled(fake: FakeDesktop) -> str:
    return fake.typed_text


# --------------------------------------------------------------------------- the arguments


def test_defaults_are_five_seconds_unicode_and_no_hebrew() -> None:
    args = parse()
    assert (args.countdown, args.inject, args.hebrew) == (5.0, "unicode", False)


def test_the_options_are_read() -> None:
    args = parse("--countdown", "2.5", "--inject", "both", "--hebrew")
    assert (args.countdown, args.inject, args.hebrew) == (2.5, "both", True)
    assert parse("--inject", "vk").inject == "vk"
    assert parse("--countdown", "0").countdown == 0.0


@pytest.mark.parametrize(
    "argv",
    [
        ["--inject", "sendkeys"],
        ["--countdown", "-1"],
        ["--countdown", "nan"],
        ["--countdown", "inf"],
        ["--countdown", "61"],
        ["--countdown", "soon"],
    ],
)
def test_bad_options_are_a_usage_error(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        parse(*argv)
    assert excinfo.value.code == 2
    assert "Traceback" not in capsys.readouterr().err


def test_the_subcommand_can_be_wired_the_way_the_cli_does_it() -> None:
    parser = argparse.ArgumentParser(prog="jarvis_hands")
    sub = parser.add_subparsers(dest="command", required=True)
    keytest_parser = sub.add_parser("keytest")
    keytest.add_arguments(keytest_parser)
    keytest_parser.set_defaults(func=keytest.run)
    args = parser.parse_args(["keytest", "--hebrew"])
    assert args.func is keytest.run and args.hebrew is True


def test_run_keeps_the_pinned_signature_and_reads_the_one_clock_and_the_real_sleep() -> None:
    parameters = inspect.signature(keytest.run).parameters
    assert next(iter(parameters)) == "args"
    extras = {n: p for n, p in parameters.items() if n != "args"}
    assert all(
        p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is not inspect.Parameter.empty for p in extras.values()
    )
    assert extras["clock"].default is clock.now  # every compared time reads jarvis_hands.clock
    assert extras["sleep"].default is not None and callable(extras["sleep"].default)


# --------------------------------------------------------------------------- where it refuses


def test_it_refuses_where_the_platform_has_no_desktop(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_desktop() -> Any:
        raise UnsupportedPlatform(f"nothing here {SENTINEL}")

    monkeypatch.setattr(desktop, "create_desktop", no_desktop)
    probe = Probe()
    code = keytest.run(parse(), sleep=probe.sleep, clock=probe.clock, out=probe.out)
    assert code == keytest.EXIT_REFUSED == 2
    assert "needs Windows" in probe.text and SENTINEL not in probe.text
    assert sleeps(probe) == []  # refused before the countdown, not after it


def test_it_refuses_a_desktop_that_only_pretends_to_type() -> None:
    fake = FakeDesktop()  # injects_for_real is False: the --fake runs
    code, probe = go(parse("--countdown", "0"), fake)
    assert code == 2 and fake.key_calls == [] and sleeps(probe) == []
    assert "does not type for real" in probe.text


def test_it_refuses_a_desktop_without_the_keyboard_calls() -> None:
    class PointerOnly:
        pass

    probe = Probe()
    code = keytest.run(parse(), desktop=PointerOnly(), sleep=probe.sleep, clock=probe.clock, out=probe.out)  # type: ignore[arg-type]
    assert code == 2 and "no keyboard" in probe.text and sleeps(probe) == []


def test_a_desktop_it_cannot_open_is_an_error_without_the_exception_text(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> Any:
        raise OSError(f"the display stack said {SENTINEL}")

    monkeypatch.setattr(desktop, "create_desktop", broken)
    probe = Probe()
    code = keytest.run(parse(), sleep=probe.sleep, clock=probe.clock, out=probe.out)
    assert code == keytest.EXIT_FAILED == 1
    assert SENTINEL not in probe.text and "OSError" in probe.text


def test_a_desktop_it_made_is_closed_and_one_it_was_given_is_not(monkeypatch: pytest.MonkeyPatch) -> None:
    made = FakeDesktop(injects_for_real=True)
    monkeypatch.setattr(desktop, "create_desktop", lambda: made)
    probe = Probe()
    assert keytest.run(parse("--countdown", "0"), sleep=probe.sleep, clock=probe.clock, out=probe.out) == 0
    assert made.closed is True
    given = FakeDesktop(injects_for_real=True)
    assert go(parse("--countdown", "0"), given)[0] == 0
    assert given.closed is False


# --------------------------------------------------------------------------- the typing


def test_it_types_the_fixed_line_then_x_and_a_backspace() -> None:
    fake, probe = spy()
    code, probe = go(parse("--countdown", "0"), fake, probe)
    assert code == 0
    assert spelled(fake) == LINE + "x" + "\b"
    assert all(kind in ("char", "control") for kind, _value in fake.key_calls)


def test_every_character_of_the_line_is_on_the_air_keyboard() -> None:
    allowed = ALLOWED_CHARS | {" "}
    assert set(LINE) <= allowed and set(HEBREW) <= allowed and "x" in allowed
    assert keytest.KEYTEST_LINE == LINE and keytest.KEYTEST_HEBREW == HEBREW


def test_each_stroke_is_its_own_call_so_the_allow_list_and_the_pacing_apply_to_each() -> None:
    fake, probe = spy()
    go(parse("--countdown", "0"), fake, probe)
    calls = key_events(probe)
    assert len(calls) == len(LINE) + 2
    assert all(len(strokes) == 1 for _tag, _inject, strokes in calls)
    assert [s[0] for _t, _i, s in calls][:3] == [("char", "a"), ("char", "b"), ("char", "c")]
    assert ("control", "space") in [s[0] for _t, _i, s in calls]  # the spaces are the control, not a character
    assert [s[0] for _t, _i, s in calls][-2:] == [("char", "x"), ("control", "backspace")]


def test_with_hebrew_a_space_and_the_word_come_before_the_x() -> None:
    fake, probe = spy()
    code, _ = go(parse("--countdown", "0", "--hebrew"), fake, probe)
    assert code == 0
    assert spelled(fake) == LINE + " " + HEBREW + "x" + "\b"


@pytest.mark.parametrize("mode", ["unicode", "vk"])
def test_the_injection_mode_is_passed_to_every_stroke(mode: str) -> None:
    fake, probe = spy()
    go(parse("--countdown", "0", "--inject", mode), fake, probe)
    assert {inject for _tag, inject, _s in key_events(probe)} == {mode}
    assert spelled(fake) == LINE + "x" + "\b"


def test_both_types_the_line_in_unicode_then_a_space_then_the_line_in_vk() -> None:
    fake, probe = spy()
    code, _ = go(parse("--countdown", "0", "--inject", "both", "--hebrew"), fake, probe)
    assert code == 0
    one = LINE + " " + HEBREW + "x" + "\b"
    assert spelled(fake) == one + " " + one
    modes = [inject for _tag, inject, _s in key_events(probe)]
    boundary = len(one)  # the strokes of the first run
    assert set(modes[:boundary]) == {"unicode"} and set(modes[boundary + 1 :]) == {"vk"}
    assert modes[boundary] in ("unicode", "vk")  # the separating space belongs to neither run's check


def test_a_character_the_layout_has_no_key_for_still_arrives_in_vk_mode() -> None:
    fake, probe = spy()  # no lookup: every character falls back to Unicode inside send_keys
    go(parse("--countdown", "0", "--inject", "vk", "--hebrew"), fake, probe)
    assert HEBREW in spelled(fake)


def test_the_strokes_are_paced_at_least_the_run_lanes_gap_apart() -> None:
    probe = Probe()
    fake = SpyDesktop(probe.events)
    go(parse("--countdown", "0"), fake, probe)
    assert keytest.KEY_GAP_S >= INSERT_GAP_S
    gaps = [e[1] for e in probe.events if e[0] == "sleep"]
    assert gaps and set(gaps) == {keytest.KEY_GAP_S}
    kinds = [e[0] for e in probe.events if e[0] in ("keys", "sleep")]
    # a pause after each stroke except the last, never two pauses or two strokes in a row
    assert all(a != b for a, b in itertools.pairwise(kinds))
    assert kinds[0] == "keys" and kinds[-1] == "keys"


def test_it_stops_there_and_releases_any_key_it_still_holds() -> None:
    fake, probe = spy()
    go(parse("--countdown", "0"), fake, probe)
    assert fake.release_keys_calls >= 1


def test_the_report_says_what_to_look_for_and_stays_ascii() -> None:
    fake, probe = spy()
    code, probe = go(parse("--countdown", "0", "--hebrew"), fake, probe)
    assert code == 0
    assert LINE in probe.text  # the fixed line, so it can be compared with the window
    assert probe.text.isascii() and HEBREW not in probe.text
    assert "Hebrew" in probe.text and "Backspace" in probe.text


# --------------------------------------------------------------------------- the countdown and the timing


def test_the_countdown_counts_whole_seconds_before_the_first_key() -> None:
    fake, probe = spy()
    go(parse("--countdown", "5"), fake, probe)
    first_key = next(i for i, e in enumerate(probe.events) if e[0] == "keys")
    before = [e[1] for e in probe.events[:first_key] if e[0] == "sleep"]
    assert before == [1.0] * 5 and math.isclose(sum(before), 5.0)
    assert [line.strip() for line in probe.text.splitlines() if line.strip() in {"5", "4", "3", "2", "1"}] == [
        "5",
        "4",
        "3",
        "2",
        "1",
    ]


def test_a_fractional_countdown_ends_on_the_remainder() -> None:
    fake, probe = spy()
    go(parse("--countdown", "2.5"), fake, probe)
    first_key = next(i for i, e in enumerate(probe.events) if e[0] == "keys")
    assert [e[1] for e in probe.events[:first_key] if e[0] == "sleep"] == [1.0, 1.0, 0.5]
    counted = [line.strip() for line in probe.text.splitlines() if line.strip().isdigit()]
    assert counted == ["3", "2", "1"]  # a started second counts as a whole one: never a "0"


def test_no_countdown_means_no_wait_before_the_first_key() -> None:
    fake, probe = spy()
    go(parse("--countdown", "0"), fake, probe)
    assert probe.events.index(("probe",)) < next(i for i, e in enumerate(probe.events) if e[0] == "keys")
    assert not any(
        e[0] == "sleep" for e in probe.events[: next(i for i, e in enumerate(probe.events) if e[0] == "keys")]
    )


def test_the_median_of_one_hundred_reads_is_measured_in_the_last_second_and_printed() -> None:
    probe = Probe([0.0004] * 99 + [0.050])  # one slow read does not move the median
    fake, probe = spy(probe)
    code, probe = go(parse("--countdown", "3"), fake, probe)
    assert code == 0
    probes = [i for i, e in enumerate(probe.events) if e == ("probe",)]
    sleeps_at = [i for i, e in enumerate(probe.events) if e[0] == "sleep"][:3]
    assert len(probes) >= 100
    # the measurement sits before the countdown's last sleep, so it is on the screen while the count runs down
    assert probes[0] > sleeps_at[1] and probes[99] < sleeps_at[2]
    assert "median 0.40 ms of 100" in probe.text
    assert "within the 2 ms budget" in probe.text
    assert probe.text.index("median") < probe.text.index("Typing into")


def test_the_target_is_read_exactly_one_hundred_times_for_the_median_and_once_for_the_check() -> None:
    fake, probe = spy()
    go(parse("--countdown", "0"), fake, probe)
    assert fake.reads == 101  # not once per character: that is what the median is a price for


def test_a_slow_read_is_reported_over_budget_but_is_not_a_failure() -> None:
    probe = Probe([0.003])
    fake, probe = spy(probe)
    code, probe = go(parse("--countdown", "0"), fake, probe)
    assert code == 0
    assert "median 3.00 ms of 100" in probe.text and "OVER the 2 ms budget" in probe.text


def test_a_read_that_raises_is_a_failure_without_its_text() -> None:
    class Broken(SpyDesktop):
        def key_target(self) -> KeyTarget:
            raise OSError(f"cannot see {SENTINEL}")

    probe = Probe()
    code, probe = go(parse("--countdown", "0"), Broken(probe.events), probe)
    assert code == 1 and SENTINEL not in probe.text and "OSError" in probe.text
    assert key_events(probe) == []


# --------------------------------------------------------------------------- targets it will not type into


def blocked(**fields: Any) -> KeyTarget:
    base: dict[str, Any] = {
        "hwnd": 100,
        "pid": 200,
        "name": "Notepad",
        "lang_id": 0x0409,
        "blocked": None,
        "password": False,
        "covered": False,
    }
    return KeyTarget(**{**base, **fields})


@pytest.mark.parametrize(
    ("target", "word"),
    [
        (blocked(hwnd=0, blocked="none"), "no window"),
        (blocked(blocked="own"), "Jarvis"),
        (blocked(blocked="elevated"), "administrator"),
        (blocked(blocked="shell"), "taskbar"),
        (blocked(password=True), "password"),
        (blocked(covered=True), "full screen"),
    ],
)
def test_it_types_nothing_into_a_target_the_keyboard_would_hold_back(target: KeyTarget, word: str) -> None:
    fake, probe = spy()
    fake.target = target
    code, probe = go(parse("--countdown", "0"), fake, probe)
    assert code == 1
    assert fake.key_calls == [] and key_events(probe) == []
    assert "Nothing was typed" in probe.text and word in probe.text


def test_a_held_ctrl_alt_or_windows_key_stops_it() -> None:
    fake, probe = spy()
    fake.modifiers = True
    code, probe = go(parse("--countdown", "0"), fake, probe)
    assert code == 1 and fake.key_calls == []
    assert "Ctrl" in probe.text and "Nothing was typed" in probe.text


def test_the_program_name_and_layout_are_shown_on_the_local_screen() -> None:
    fake, probe = spy()
    fake.target = blocked(name="Notepad", lang_id=0x040D)
    go(parse("--countdown", "0"), fake, probe)
    assert "Notepad" in probe.text and "0x040D" in probe.text


def test_a_program_name_a_console_cannot_show_is_replaced_not_crashed_on() -> None:
    fake, probe = spy()
    fake.target = blocked(name="\u05e9\u05dc\u05d5\u05dd \u00e9cran")
    code, probe = go(parse("--countdown", "0"), fake, probe)
    assert code == 0 and probe.text.isascii() and "Typing into ????" in probe.text


# --------------------------------------------------------------------------- when typing goes wrong


def test_windows_taking_none_of_the_keys_is_a_failure_that_stops_at_once() -> None:
    fake, probe = spy()
    fake.fail_keys = 1
    code, probe = go(parse("--countdown", "0"), fake, probe)
    assert code == 1 and fake.key_calls == []
    assert len(key_events(probe)) == 1  # the second stroke was not tried
    assert "took none" in probe.text and fake.release_keys_calls >= 1


def test_windows_taking_part_of_a_key_is_a_failure_that_stops_and_releases() -> None:
    fake, probe = spy()
    fake.partial_keys = 1
    code, probe = go(parse("--countdown", "0"), fake, probe)
    assert code == 1
    assert len(key_events(probe)) == 1
    assert "part of a key" in probe.text and fake.release_keys_calls >= 1


def test_an_unexpected_error_is_named_by_type_only_in_the_output_and_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake, probe = spy()
    fake.raise_after = RuntimeError(f"boom {SENTINEL} q")
    with caplog.at_level(logging.DEBUG):
        code, probe = go(parse("--countdown", "0"), fake, probe)
    assert code == 1
    assert SENTINEL not in probe.text and SENTINEL not in caplog.text
    assert "RuntimeError" in probe.text
    assert fake.release_keys_calls >= 1


def test_ctrl_c_during_the_countdown_types_nothing() -> None:
    fake, probe = spy()

    def interrupted(seconds: float) -> None:
        raise KeyboardInterrupt

    code = keytest.run(parse("--countdown", "3"), desktop=fake, sleep=interrupted, clock=probe.clock, out=probe.out)
    assert code == 1 and fake.key_calls == [] and "Stopped" in probe.text
    assert fake.release_keys_calls >= 1


def test_ctrl_c_while_typing_releases_the_keys() -> None:
    fake, probe = spy()
    calls = [0]

    def interrupted(seconds: float) -> None:
        calls[0] += 1
        if calls[0] == 3:
            raise KeyboardInterrupt

    code = keytest.run(parse("--countdown", "0"), desktop=fake, sleep=interrupted, clock=probe.clock, out=probe.out)
    assert code == 1 and len(fake.key_calls) == 3 and fake.release_keys_calls >= 1


def test_nothing_is_logged_by_the_tool_itself(caplog: pytest.LogCaptureFixture) -> None:
    fake, probe = spy()
    with caplog.at_level(logging.DEBUG):
        go(parse("--countdown", "0", "--hebrew"), fake, probe)
    assert [r for r in caplog.records if r.name.startswith("jarvis_hands.keyboard.keytest")] == []


def test_the_exit_codes_are_zero_one_two() -> None:
    assert (keytest.EXIT_OK, keytest.EXIT_FAILED, keytest.EXIT_REFUSED) == (0, 1, 2)


def test_input_blocked_from_the_desktop_is_told_the_same_way_as_a_fake_refusal() -> None:
    class Blocked(SpyDesktop):
        def send_keys(self, strokes: Sequence[KeyStroke], *, inject: Literal["unicode", "vk"] = "unicode") -> int:
            raise InputBlocked(f"UIPI said {SENTINEL}")

    probe = Probe()
    code, probe = go(parse("--countdown", "0"), Blocked(probe.events), probe)
    assert code == 1 and SENTINEL not in probe.text and "took none" in probe.text


# --------------------------------------------------------------------------- what the typed text can be


@pytest.mark.parametrize("hebrew", [False, True])
@pytest.mark.parametrize("mode", ["unicode", "vk", "both"])
def test_every_mode_and_option_types_only_what_the_allow_list_permits(mode: str, hebrew: bool) -> None:
    fake, probe = spy()
    argv = ["--countdown", "0", "--inject", mode, *(["--hebrew"] if hebrew else [])]
    assert go(parse(*argv), fake, probe)[0] == 0
    for kind, value in fake.key_calls:
        KeyStroke(kind, value)  # type: ignore[arg-type]  # raises KeyRefused for anything the allow-list refuses


def test_the_text_is_built_without_the_digit_or_the_bang_a_user_might_expect() -> None:
    def forbidden(text: str) -> bool:
        return any(c.isdigit() or c in "!;" for c in text)

    assert not forbidden(keytest.KEYTEST_LINE) and not forbidden(keytest.KEYTEST_HEBREW)


def test_strokes_for_turns_spaces_into_the_space_control() -> None:
    strokes = keytest.strokes_for("a b")
    assert [(s.kind, s.value) for s in strokes] == [("char", "a"), ("control", "space"), ("char", "b")]
    with pytest.raises(KeyRefused):  # a character outside the allow-list is never made a stroke
        keytest.strokes_for("5")
