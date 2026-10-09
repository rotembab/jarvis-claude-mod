"""The review box (DESIGN 2.13.1): U40, U44, U81.

The text is as private as anything the keyboard holds, so half of these tests are about what the object refuses to
show; the other half are about the alphabet and the end-only editing that make a stray tap harmless.
"""

from __future__ import annotations

import random

import pytest

from jarvis_hands.desktop.keys import ALLOWED_CHARS, COMPOSE_CHARS
from jarvis_hands.keyboard import limits
from jarvis_hands.keyboard.compose import ComposeBuffer, insert_check
from jarvis_hands.keyboard.types import Touch

SENTINEL = "zzqxjv"


def touch(n: int = 0) -> Touch:
    return Touch(1.0 + n, 2.0, 1, "right", float(n))


# ----------------------------------------------------------------------------------------------------------- U40


def test_the_default_alphabet_is_the_compose_alphabet() -> None:
    buf = ComposeBuffer()
    for ch in sorted(COMPOSE_CHARS):
        assert buf.append(ch, 0.0) == "ok", ord(ch)
    # a box capped at its size would say "full" for the ones that did not fit; the alphabet has more than the cap
    assert COMPOSE_CHARS - {" "} == ALLOWED_CHARS


@pytest.mark.parametrize(
    "ch",
    [
        "1",
        "0",
        "9",
        "!",
        ";",
        "\n",
        "\r",
        "\t",
        "\x00",
        "\x1b",
        "\x7f",
        "@",
        "#",
        "(",
        "é",
        "\U0001f600",
        "",
        "ab",
        "  ",
    ],
)
def test_a_character_outside_the_alphabet_is_refused_and_changes_nothing(ch: str) -> None:
    buf = ComposeBuffer()
    buf.append("a", 1.0)
    before = (buf.text(), buf.version, buf.last_edit_t)
    assert buf.append(ch, 2.0) == "refused"
    assert (buf.text(), buf.version, buf.last_edit_t) == before


def test_a_non_string_is_refused() -> None:
    buf = ComposeBuffer()
    for bad in (1, None, b"a", ["a"], ord("a")):
        assert buf.append(bad, 0.0) == "refused"  # type: ignore[arg-type]
    assert len(buf) == 0


def test_a_string_subclass_cannot_pose_as_an_allowed_character() -> None:
    class Liar(str):
        def __contains__(self, other: object) -> bool:
            return True

        def __eq__(self, other: object) -> bool:
            return True

        __hash__ = str.__hash__

    buf = ComposeBuffer()
    assert buf.append(Liar("1"), 0.0) == "refused"
    assert len(buf) == 0


def test_the_cap_is_two_hundred_and_the_next_character_is_full() -> None:
    assert limits.COMPOSE_MAX == 200
    buf = ComposeBuffer()
    for i in range(limits.COMPOSE_MAX):
        assert buf.append("a", float(i)) == "ok"
    version = buf.version
    assert buf.append("b", 999.0) == "full"
    assert (
        len(buf) == limits.COMPOSE_MAX and buf.version == version and buf.last_edit_t == float(limits.COMPOSE_MAX - 1)
    )
    # a character that does not belong is refused even when the box is full: the reason is the character, not the room
    assert buf.append("1", 999.0) == "refused"
    # room again after a backspace
    assert buf.backspace(1000.0)
    assert buf.append("b", 1001.0) == "ok"


def test_the_cap_and_alphabet_are_arguments() -> None:
    small = ComposeBuffer(frozenset("ab "), cap=3)
    assert [small.append(c, 0.0) for c in "abc "] == ["ok", "ok", "refused", "ok"]
    assert small.append("a", 0.0) == "full"
    assert small.text() == "ab "
    # a widened alphabet admits a digit but never a newline
    wide = ComposeBuffer(COMPOSE_CHARS | frozenset("1!\n"))
    assert wide.append("1", 0.0) == "ok" and wide.append("!", 0.0) == "ok"
    assert wide.append("\n", 0.0) == "refused"


def test_space_is_a_character_of_the_box() -> None:
    buf = ComposeBuffer()
    for ch in "hi there":
        assert buf.append(ch, 0.0) == "ok"
    assert buf.text() == "hi there"


def test_backspace_removes_one_and_an_empty_box_says_so() -> None:
    buf = ComposeBuffer()
    assert buf.backspace(1.0) is False
    assert buf.version == 0
    for ch in "ab":
        buf.append(ch, 1.0)
    assert buf.backspace(2.0) is True
    assert buf.text() == "a" and buf.last_edit_t == 2.0
    assert buf.backspace(3.0) and not buf.backspace(4.0)
    assert buf.last_edit_t == 3.0


def test_clear_returns_the_number_dropped() -> None:
    buf = ComposeBuffer()
    assert buf.clear(1.0) == 0 and buf.version == 0
    for ch in "hello":
        buf.append(ch, 1.0)
    version = buf.version
    assert buf.clear(2.0) == 5
    assert len(buf) == 0 and buf.version == version + 1 and buf.last_edit_t == 2.0
    assert buf.touches() == ()


def test_consume_drops_the_typed_prefix_with_its_touches() -> None:
    buf = ComposeBuffer()
    for i, ch in enumerate("abcdef"):
        buf.append(ch, 1.0, touch(i))
    version = buf.version
    buf.consume(2, 5.0)
    assert buf.text() == "cdef" and buf.version == version + 1 and buf.last_edit_t == 5.0
    assert [tc.u for tc in buf.touches() if tc is not None] == [3.0, 4.0, 5.0, 6.0]
    buf.consume(0, 6.0)
    buf.consume(-3, 6.0)
    assert buf.text() == "cdef" and buf.version == version + 1  # nothing changed, nothing counted
    buf.consume(99, 7.0)  # more than there is: the box is empty, no error
    assert len(buf) == 0 and buf.touches() == ()


def test_every_mutator_bumps_the_version_once() -> None:
    buf = ComposeBuffer()
    steps = [
        lambda: buf.append("a", 1.0),
        lambda: buf.append("b", 2.0),
        lambda: buf.backspace(3.0),
        lambda: buf.replace_span(0, 1, "xy", 4.0),
        lambda: buf.consume(1, 5.0),
        lambda: buf.clear(6.0),
    ]
    for i, step in enumerate(steps, start=1):
        step()
        assert buf.version == i and buf.last_edit_t == float(i)


def test_the_text_never_shows_up_by_accident() -> None:
    buf = ComposeBuffer()
    for ch in SENTINEL:
        buf.append(ch, 0.0, touch())
    assert repr(buf) == f"<ComposeBuffer len={len(SENTINEL)}>"
    assert SENTINEL not in repr(buf) and SENTINEL not in str(buf) and SENTINEL not in f"{buf}"
    for operation in (iter, lambda b: b[0], lambda b: "a" in b, list, tuple, set, sorted):
        with pytest.raises(TypeError):
            operation(buf)
    assert "__iter__" not in dir(buf) and "__getitem__" not in dir(buf) and "__contains__" not in dir(buf)
    assert type(buf).__eq__ is object.__eq__
    assert buf != ComposeBuffer() and buf == buf  # identity, never content
    # the touches are as sensitive as the text: their repr is a fixed word
    assert all(repr(tc) == "<Touch>" for tc in buf.touches())


def test_the_text_is_a_fresh_string_each_time() -> None:
    buf = ComposeBuffer()
    buf.append("a", 0.0)
    first = buf.text()
    buf.append("b", 1.0)
    assert first == "a" and buf.text() == "ab"


def test_property_no_sequence_makes_a_newline_or_goes_over_the_cap() -> None:
    rng = random.Random(7)
    pool = [*COMPOSE_CHARS, "\n", "1", "!", "\t", "é", "x"]
    for _ in range(30):
        buf = ComposeBuffer()
        for step in range(500):
            roll = rng.random()
            t = float(step)
            if roll < 0.70:
                buf.append(rng.choice(pool), t)
            elif roll < 0.82:
                buf.backspace(t)
            elif roll < 0.84:
                buf.clear(t)
            elif roll < 0.90:
                buf.consume(rng.randrange(0, 8), t)
            else:
                lo = rng.randrange(0, max(len(buf), 1))
                buf.replace_span(lo, lo + rng.randrange(0, 5), "".join(rng.choices(pool, k=rng.randrange(0, 6))), t)
            text = buf.text()
            assert len(text) <= limits.COMPOSE_MAX
            assert "\n" not in text and all(c in COMPOSE_CHARS for c in text)
            assert len(buf.touches()) == len(text) == len(buf)


# ----------------------------------------------------------------------------------------------------------- U44


@pytest.mark.parametrize("text", ["", " ", "   "])
def test_insert_check_empty(text: str) -> None:
    assert insert_check(text) == "empty"


def test_insert_check_too_long_and_bad_characters() -> None:
    assert insert_check("a" * limits.COMPOSE_MAX) is None
    assert insert_check("a" * (limits.COMPOSE_MAX + 1)) == "too_long"
    assert insert_check("hello\nworld") == "bad_char"
    assert insert_check("\n") == "bad_char"  # not "empty": only spaces are empty
    assert insert_check("ab1") == "bad_char"
    assert insert_check("ab!") == "bad_char"  # not in the step-1 alphabet at all
    assert insert_check("é") == "bad_char"
    # the order of the answers: empty, then too long, then a bad character
    assert insert_check("1" * (limits.COMPOSE_MAX + 1)) == "too_long"


def test_a_leading_bang_is_refused_when_the_alphabet_is_widened() -> None:
    wide = COMPOSE_CHARS | frozenset("!1")
    assert insert_check("!ls", alphabet=wide) == "bang_first"
    assert insert_check(" !ls", alphabet=wide) == "bang_first"
    assert insert_check("a!ls", alphabet=wide) is None
    assert insert_check("1ls", alphabet=wide) is None
    # digits are still outside the step-1 alphabet
    assert insert_check("1ls") == "bad_char"


def test_a_leading_slash_is_allowed_because_typing_does_not_run_it() -> None:
    assert insert_check("/clear") is None
    assert insert_check("  /clear") is None


def test_insert_check_hebrew_and_punctuation_pass() -> None:
    assert insert_check("shalom שלום, ok? it's a-b/c.") is None


# ----------------------------------------------------------------------------------------------------------- U81


def build(text: str) -> ComposeBuffer:
    buf = ComposeBuffer()
    for i, ch in enumerate(text):
        buf.append(ch, 0.0, touch(i))
    return buf


@pytest.mark.parametrize(
    ("start", "end", "text", "touches"),
    [
        (2, 2, "x", None),  # an empty span
        (3, 2, "x", None),  # a reversed span
        (-1, 2, "x", None),
        (0, 7, "x", None),  # past the end
        (0, 1, "1", None),  # outside the alphabet
        (0, 1, "a\nb", None),  # a newline
        (0, 1, "xy", [None]),  # touches of the wrong length
    ],
)
def test_replace_span_refuses_and_changes_nothing(
    start: int, end: int, text: str, touches: list[object] | None
) -> None:
    buf = build("abcdef")
    before = (buf.text(), buf.version, buf.last_edit_t, buf.touches())
    assert buf.replace_span(start, end, text, 9.0, touches) is False  # type: ignore[arg-type]
    assert (buf.text(), buf.version, buf.last_edit_t, buf.touches()) == before


def test_replace_span_refuses_a_result_over_the_cap() -> None:
    buf = build("a" * 199)
    assert buf.replace_span(0, 1, "bb", 1.0) is True  # 200: allowed
    version = buf.version
    assert buf.replace_span(0, 1, "cc", 2.0) is False  # 201
    assert len(buf) == 200 and buf.version == version


def test_replace_span_succeeds_once_and_new_characters_have_no_touch() -> None:
    buf = build("abcdef")
    version = buf.version
    assert buf.replace_span(1, 3, "XYZ", 9.0)
    assert buf.text() == "aXYZdef" and buf.version == version + 1 and buf.last_edit_t == 9.0
    touches = buf.touches()
    assert len(touches) == 7 and touches[1:4] == (None, None, None)
    assert touches[0] is not None and touches[4] is not None


def test_replace_span_stores_the_given_touches() -> None:
    buf = build("abcdef")
    given = [touch(70), touch(71)]
    assert buf.replace_span(0, 3, "uv", 9.0, given)
    assert buf.text() == "uvdef" and buf.touches()[:2] == tuple(given)


def test_replace_span_can_delete_a_span() -> None:
    buf = build("abcdef")
    assert buf.replace_span(1, 3, "", 9.0)
    assert buf.text() == "adef" and len(buf.touches()) == 4


def test_touches_stay_aligned_with_the_text_through_random_operations() -> None:
    rng = random.Random(81)
    buf = ComposeBuffer()
    marks: list[int | None] = []  # a shadow of the touches: the value of Touch.u of each character, None if absent
    for step in range(10_000):
        t = float(step)
        roll = rng.random()
        if roll < 0.45:
            ch = rng.choice("abcdefgh ")
            if rng.random() < 0.5:
                assert buf.append(ch, t, Touch(float(step), 0.0, 0, "left", t)) == (
                    "ok" if len(marks) < 200 else "full"
                )
                if len(marks) < 200:
                    marks.append(step)
            elif buf.append(ch, t) == "ok":
                marks.append(None)
        elif roll < 0.60:
            if buf.backspace(t):
                marks.pop()
        elif roll < 0.62:
            buf.clear(t)
            marks.clear()
        elif roll < 0.72:
            n = rng.randrange(0, 6)
            buf.consume(n, t)
            del marks[:n]
        else:
            lo = rng.randrange(0, len(marks) + 1)
            hi = lo + rng.randrange(0, 5)
            new = "".join(rng.choices("abc ", k=rng.randrange(0, 5)))
            with_touches = rng.random() < 0.5
            given = [Touch(float(-i - 1), 0.0, 0, "left", t) for i in range(len(new))] if with_touches else None
            done = buf.replace_span(lo, hi, new, t, given)
            if lo < hi <= len(marks) and len(marks) - (hi - lo) + len(new) <= 200:
                assert done
                marks[lo:hi] = [-i - 1 for i in range(len(new))] if with_touches else [None] * len(new)
            else:
                assert not done
        got = [None if tc is None else int(tc.u) for tc in buf.touches()]
        assert got == marks
        assert len(buf) == len(marks)
