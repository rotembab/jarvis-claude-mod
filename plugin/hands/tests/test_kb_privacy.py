"""What was typed stays where it was typed (SR13, SR26, S22, S22b, S52, S80).

A sentinel word is tapped through the whole stack (the session, its review machine, the sink, a fake desktop) and every
place it could leak to is searched: log records, reprs, counters, the views, the tap log, summaries, steps, exceptions.
It may appear in exactly two places: the overlay's ``echo`` of a direct session and ``ComposeView.text`` of a review
session, and in neither when the session is private.
"""

from __future__ import annotations

import dataclasses
import logging
from collections import Counter
from collections.abc import Iterator
from typing import Any

import pytest

from jarvis_hands.desktop.keys import KeyRefused
from jarvis_hands.keyboard.rig import KbRig
from jarvis_hands.keyboard.sink import SinkFailed

S = "zzqxjv"


def walk(obj: object, path: str = "") -> Iterator[tuple[str, str]]:
    """Every string reachable from ``obj`` through dataclass fields (repr-hidden ones too), containers and attributes
    that are plain data, as (path, text)."""
    if isinstance(obj, str):
        yield path, obj
    elif dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        for f in dataclasses.fields(obj):
            yield from walk(getattr(obj, f.name), f"{path}.{f.name}")
    elif isinstance(obj, dict | Counter):
        for k, v in obj.items():
            yield from walk(k, f"{path}[key]")
            yield from walk(v, f"{path}[{k!r}]" if isinstance(k, str) else f"{path}[]")
    elif isinstance(obj, tuple | list | set | frozenset):
        for i, v in enumerate(obj):
            yield from walk(v, f"{path}[{i}]")


def leaks(obj: object, *, allow: tuple[str, ...] = ()) -> list[str]:
    return [p for p, text in walk(obj) if S in text and not any(p.endswith(a) for a in allow)]


def spell(rig: KbRig, word: str = S, gap_s: float = 0.4) -> None:
    t = rig.t + rig.dt
    for i, ch in enumerate(word):
        rig.tap(t + i * gap_s, ch)
    rig.run(len(word) * gap_s + 0.4)


def everything_but_the_two_places(rig: KbRig) -> list[str]:
    """The sentinel wherever it must not be, over the whole session so far."""
    found: list[str] = []
    for i, view in enumerate(rig.views):
        found += [f"view{i}{p}" for p in leaks(view, allow=(".echo", ".compose.text"))]
    found += leaks(dict(rig.counts)) + leaks(dict(rig.session.rejects))
    found += leaks(rig.summaries) + leaks(rig.steps) + leaks(rig.answers)
    found += leaks(rig.sink.counts) if rig.sink is not None else []
    for name, obj in (("session", rig.session), ("sink", rig.sink), ("press", rig.press)):
        if S in repr(obj):
            found.append(f"repr({name})")
    machine = rig.session._machine
    if machine is not None:
        for name, obj in (
            ("machine", machine),
            ("buffer", machine.buffer),
            ("touches", machine.buffer.touches()),
            ("last_insert", machine.last_insert),
            ("counts", machine.counts),
        ):
            if S in repr(obj):
                found.append(f"repr({name})")
        found += leaks(dict(machine.counts))
    for step in rig.steps:
        if S in repr(step) or S in repr(step.stroke):
            found.append("repr(step)")
    return found


def echoes(rig: KbRig) -> list[str]:
    return [v.echo for v in rig.views]


def composes(rig: KbRig) -> list[str]:
    return [v.compose.text for v in rig.views if v.compose is not None]


# ------------------------------------------------------------------------------------------------------ S22: direct


def test_s22_a_direct_session_shows_the_word_in_its_echo_and_nowhere_else(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    rig = KbRig(commit="direct")
    rig.arm()
    rig.run(0.5)
    spell(rig)
    assert rig.typed == S and any(S in e or S[-6:] in e for e in echoes(rig))
    assert not everything_but_the_two_places(rig)
    assert S not in caplog.text and all(S not in r.getMessage() for r in caplog.records)
    assert S not in rig.session.take_tap_log().__repr__()


def test_s22_in_private_mode_the_echo_is_empty_and_the_word_is_still_nowhere() -> None:
    rig = KbRig(commit="direct")
    rig.arm()
    rig.run(0.5)
    spell(rig, S[:3])
    assert any(e for e in echoes(rig))
    rig.session.set_private(True)
    rig.run(0.2)
    start = len(rig.views)
    spell(rig, S[3:])
    assert rig.typed == S
    assert all(e == "" for e in echoes(rig)[start:]) and rig.view.echo == ""
    assert not everything_but_the_two_places(rig)
    assert all(S[3:] not in e for e in echoes(rig)[start - 1 :])


# ------------------------------------------------------------------------------------------- S52 and S80: review


def test_s52_the_box_holds_the_word_only_in_the_compose_text_and_bullets_when_private(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    rig = KbRig(commit="review")
    rig.arm()
    rig.run(0.5)
    spell(rig)
    assert rig.box == S and composes(rig)[-1] == S
    rig.session.set_private(True)
    rig.run(0.2)
    assert composes(rig)[-1] == "•" * len(S) and rig.view.compose is not None and rig.view.compose.length == len(S)
    rig.session.set_private(False)
    rig.run(0.2)
    assert composes(rig)[-1] == S
    assert not everything_but_the_two_places(rig)
    assert S not in caplog.text


def test_s52_the_word_inserted_and_sent_leaves_no_trace_but_the_window() -> None:
    rig = KbRig(commit="review")
    rig.arm()
    rig.run(0.5)
    spell(rig)
    rig.insert(rig.t + 0.1)
    while not rig.summaries:
        rig.run(rig.dt)
    t_done = rig.t
    rig.send(t_done + 0.5)
    rig.run(4.0)
    assert rig.typed == S + "\n"
    assert not everything_but_the_two_places(rig)
    assert all(S not in repr(s) for s in rig.summaries) and all(S not in repr(s) for s in rig.steps)


def test_s52_the_word_aborted_and_discarded_is_gone_with_the_session() -> None:
    rig = KbRig(commit="review")
    rig.arm()
    rig.run(0.5)
    spell(rig)
    rig.desktop.after_key = lambda n: rig.desktop.user_typed() if n == 3 else None
    rig.insert(rig.t + 0.1)
    while not rig.summaries:
        rig.run(rig.dt)
    assert rig.typed == S[:3] and rig.box == S[3:]
    rig.session.close("command")
    rig.run(0.2)
    assert rig.session.discarded == 3 and rig.box == ""
    assert not everything_but_the_two_places(rig)
    assert S not in repr(rig.session) and S not in repr(rig.sink)


def test_s80_tapped_edited_with_backspace_and_closed_the_touches_and_the_buffer_do_not_leak() -> None:
    rig = KbRig(commit="review")
    rig.arm()
    rig.run(0.5)
    spell(rig, S + "x")
    rig.tap(rig.t + rig.dt, "backspace")
    rig.run(0.5)
    machine = rig.session._machine
    assert machine is not None and rig.box == S
    touches = machine.buffer.touches()
    assert len(touches) == len(S) and all(touch is not None and S not in repr(touch) for touch in touches)
    assert repr(touches[0]) == "<Touch>"
    assert S not in repr(machine.buffer) and S not in repr(machine) and S not in repr(rig.session)
    assert composes(rig)[-1] == S  # the one place
    rig.session.close("command")
    rig.run(0.2)
    assert not everything_but_the_two_places(rig)
    assert S not in repr(machine.buffer) and machine.buffer.text() == ""


def test_the_practice_tap_log_holds_numbers_and_fixed_words_but_not_the_characters_tapped() -> None:
    rig = KbRig(commit="review", mode="practice", phrases=[S, S, S, S, S, S], keep_views=False)
    rig.arm()
    rig.run(0.5)
    spell(rig)
    rig.run(1.0)
    log = rig.session.take_tap_log()
    assert log and not leaks(log)
    result = rig.session.practice_result()
    assert result is not None and not leaks(result)
    assert S not in repr(rig.session) and rig.session.counts and not leaks(dict(rig.session.counts))


# ----------------------------------------------------------------------------------------- S22b: exceptions are data

SEAMS = ("key_target", "send_keys", "foreign_input", "modifiers_down")


def raising(rig: KbRig, seam: str, exc: Exception) -> list[int]:
    """Make the desktop's ``seam`` raise ``exc`` from now on; the list counts the calls."""
    calls: list[int] = []

    def raise_it(*args: object, **kwargs: object) -> Any:
        calls.append(1)
        raise exc

    setattr(rig.desktop, seam, raise_it)
    return calls


def chained() -> Exception:
    try:
        try:
            raise OSError(S)
        except OSError as inner:
            raise ValueError(f"bad stroke {S!r}") from inner
    except ValueError as outer:
        return outer


EXCEPTIONS = {
    "runtime": lambda: RuntimeError(S),
    "os": lambda: OSError(S),
    "refused": lambda: KeyRefused(S),
    "value": lambda: ValueError(f"bad stroke {S!r}"),
    "chained": chained,
}


@pytest.mark.parametrize("seam", SEAMS)
@pytest.mark.parametrize("exc", EXCEPTIONS)
def test_s22b_an_exception_carrying_the_word_from_the_desktop_leaves_nothing_behind(
    seam: str, exc: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    rig = KbRig(commit="review")
    rig.arm()
    rig.run(0.5)
    spell(rig)
    calls = raising(rig, seam, EXCEPTIONS[exc]())
    rig.insert(rig.t + 0.1)
    raised: BaseException | None = None
    try:
        rig.run(14.0)
    except SinkFailed as err:
        raised = err
    except Exception as err:  # noqa: BLE001 - whatever escapes must still be clean
        raised = err
    assert calls, "the seam was never reached"
    if raised is not None:
        assert S not in str(raised) and S not in repr(raised)
        assert raised.__cause__ is None and (raised.__context__ is None or S not in repr(raised.__context__))
    assert not everything_but_the_two_places(rig)
    assert S not in caplog.text and S not in repr(rig.sink)
    assert rig.closed in (None, "input_blocked", "runaway", "error") or rig.session.closed is not None


def test_the_scanner_finds_the_word_where_it_is_so_an_empty_result_means_something() -> None:
    rig = KbRig(commit="review")
    rig.arm()
    rig.run(0.5)
    spell(rig)
    assert leaks(rig.view) == [".compose.text"]
    assert leaks(rig.view, allow=(".compose.text",)) == []
    assert leaks({"counts": S}) and leaks([S]) and not leaks({"counts": "zz"})
    direct = KbRig(commit="direct")
    direct.arm()
    direct.run(0.5)
    spell(direct)
    assert ".echo" in leaks(direct.view)
