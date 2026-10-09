"""overlay/text.py: wrapping the box's text and the order a mixed Hebrew / English line is painted in (U46, O46).

Pure strings: no font, no image. The painter relies on three promises tested here, because a wrong one would show a
different text in the box than Insert types (SR9): wrapping loses no character but the spaces it breaks at, the display
string is a rearrangement of the logical one and nothing else, and no input can make either raise.
"""

from __future__ import annotations

import random
import re
from itertools import pairwise

import pytest

from jarvis_hands.overlay import text as ot

HEBREW = "אבגדהוזחטיכלמנסעפצקרשת"
FINALS = "ךםןףץ"
ALPHABET = "abcdefghijklmnopqrstuvwxyz" + HEBREW + FINALS + " '.,/-?"


# --------------------------------------------------------------------------- the bidi table of 3.11 (O46, U46)

#: DESIGN 3.11, "Required results": the string to paint left to right.
BIDI_TABLE = [
    ("שלום", "םולש"),
    ("שלום עולם", "םלוע םולש"),
    ("hello שלום", "hello םולש"),
    ("שלום hello", "hello םולש"),  # Hebrew on the right of a right-to-left line
    ("שלום?", "?םולש"),
    ("ג'ק", "ק'ג"),
    ("hello world", "hello world"),
]


@pytest.mark.parametrize(("logical", "painted"), BIDI_TABLE)
def test_o46_the_required_bidi_results(logical: str, painted: str) -> None:
    assert ot.bidi_display(logical) == painted


def test_u46_ascii_is_painted_as_it_is() -> None:
    for sample in ["", "a", "hello world", "what? it's 1-2, ok.", "a  b", " lead", "trail ", "x/y"]:
        assert ot.bidi_display(sample) == sample
        assert ot.base_direction(sample) == "L"


def test_the_empty_string_and_a_neutral_only_string_paint_unchanged() -> None:
    assert ot.bidi_display("") == ""
    assert ot.bidi_display("?? ..") == "?? .."
    assert ot.base_direction("") == "L"
    assert ot.base_direction("?? ..") == "L"  # no strong character: left to right


def test_the_base_direction_is_the_first_strong_characters() -> None:
    assert ot.base_direction("שלום hello") == "R"
    assert ot.base_direction("hello שלום") == "L"
    assert ot.base_direction("?? שלום") == "R"  # neutrals do not count
    assert ot.base_direction("'א") == "R"


def test_a_neutral_between_two_strong_characters_of_one_direction_takes_it_and_otherwise_the_base() -> None:
    # R ' R: the apostrophe goes with the Hebrew run, so the whole word reverses together
    assert ot.bidi_display("ג'ק") == "ק'ג"
    # L - R in a left-to-right line: the hyphen differs from both sides' agreement, so it takes the base (L)
    assert ot.bidi_display("ab-שב") == "ab-בש"
    # R - L in a right-to-left line: the base (R) takes it; runs: R "שב-" (reversed "-בש"), L "ab"; run order reversed
    assert ot.bidi_display("שב-ab") == "ab-בש"
    # Neutrals at either edge take the base direction (a leading "?" in a Hebrew line)
    assert ot.bidi_display("?שלום") == "םולש?"


def test_an_explicit_base_overrides_the_detected_one() -> None:
    assert ot.bidi_display("hello שלום", base="R") == "םולש hello"
    assert ot.bidi_display("שלום", base="L") == "םולש"  # a lone R run reverses in either
    assert ot.bidi_display("hello", base="R") == "hello"


def test_the_order_is_a_permutation_that_spells_the_display_string() -> None:
    for logical, painted in BIDI_TABLE:
        order = ot.bidi_order(logical)
        assert sorted(order) == list(range(len(logical)))
        assert "".join(logical[i] for i in order) == painted


def test_the_order_maps_a_logical_prefix_to_the_painted_positions() -> None:
    """The painter dims the first ``sent`` characters; in a Hebrew word they are the rightmost glyphs."""
    order = ot.bidi_order("שלום")
    assert order == [3, 2, 1, 0]  # the first logical character (the one typed first) is painted last, at the right


# --------------------------------------------------------------------------- properties over random strings


def _random_texts(seed: int, n: int = 400, alphabet: str = ALPHABET) -> list[str]:
    rng = random.Random(seed)
    return ["".join(rng.choices(alphabet, k=rng.randint(0, 39))) for _ in range(n)]


def test_the_display_string_is_a_rearrangement_and_never_changes_a_character() -> None:
    for sample in _random_texts(1):
        painted = ot.bidi_display(sample)
        assert sorted(painted) == sorted(sample)
        order = ot.bidi_order(sample)
        assert sorted(order) == list(range(len(sample)))
        assert "".join(sample[i] for i in order) == painted
        # an explicit base never breaks that either
        for base in ("L", "R"):
            assert sorted(ot.bidi_display(sample, base=base)) == sorted(sample)  # type: ignore[arg-type]


def test_a_pure_left_to_right_string_is_never_rearranged_whatever_its_neutrals() -> None:
    for sample in _random_texts(2, alphabet="abcdefg '.,/-?"):
        assert ot.bidi_display(sample) == sample


def test_any_string_at_all_is_accepted_and_none_raises_with_it() -> None:
    """Text drawing must never raise with the text: lone surrogates, controls, non-BMP and other scripts included."""
    weird = ["\ud800", "\x00\x01\x1f", "😀 a 😀", "é à ü", "٣٤ مرحبا", "日本語 text", "‮‏", "\n\t a"]
    for sample in weird:
        assert sorted(ot.bidi_display(sample)) == sorted(sample)
        assert ot.wrap_lines(sample, 3)
        assert ot.base_direction(sample) in ("L", "R")
    rng = random.Random(3)
    for _ in range(300):
        sample = "".join(chr(rng.randint(0, 0x2FFF)) for _ in range(rng.randint(0, 30)))
        assert sorted(ot.bidi_display(sample)) == sorted(sample)
        assert "".join(ot.wrap_lines(sample, 7)).replace(" ", "") == sample.replace(" ", "")


# --------------------------------------------------------------------------- wrap_lines (U46)


def test_u46_wrap_lines_cuts_at_spaces_in_logical_order() -> None:
    assert ot.wrap_lines("aa bb cc", 5) == ["aa bb", "cc"]
    assert ot.wrap_lines("aa bb cc", 8) == ["aa bb cc"]
    assert ot.wrap_lines("aa bb cc", 4) == ["aa", "bb", "cc"]
    assert ot.wrap_lines("hello world foo", 11) == ["hello world", "foo"]


def test_u46_a_word_longer_than_the_line_is_cut_and_twelve_characters_at_five_are_three_lines() -> None:
    assert ot.wrap_lines("x" * 12, 5) == ["xxxxx", "xxxxx", "xx"]
    # the cut word starts on a fresh line, and its tail goes on with the next words
    assert ot.wrap_lines("ab " + "x" * 12, 5) == ["ab", "xxxxx", "xxxxx", "xx"]
    assert ot.wrap_lines("x" * 7 + " yy zz", 5) == ["xxxxx", "xx yy", "zz"]


def test_wrap_lines_of_nothing_is_one_empty_line_for_the_caret() -> None:
    assert ot.wrap_lines("", 10) == [""]
    assert ot.wrap_lines("", 1) == [""]


def test_leading_spaces_are_kept_on_the_first_line_and_dropped_where_they_would_not_fit() -> None:
    assert ot.wrap_lines("  ab", 10) == ["  ab"]
    assert ot.wrap_lines("   ab", 4) == ["ab"]  # the indentation and the word do not fit together: the word wins
    assert ot.wrap_lines(" a b", 3) == [" a", "b"]


def test_trailing_spaces_stay_while_they_fit_and_otherwise_open_the_line_the_caret_is_on() -> None:
    assert ot.wrap_lines("hello ", 6) == ["hello "]
    assert ot.wrap_lines("hello ", 5) == ["hello", ""]
    assert ot.wrap_lines("hello   ", 6) == ["hello ", ""]  # what fits stays; the rest is the break
    assert ot.wrap_lines("   ", 5) == ["   "]


def test_runs_of_spaces_inside_a_line_are_kept_and_at_a_break_dropped() -> None:
    assert ot.wrap_lines("a  b", 4) == ["a  b"]
    assert ot.wrap_lines("a    b", 4) == ["a", "b"]
    assert ot.wrap_lines("ab  cd", 4) == ["ab", "cd"]


def test_a_non_positive_width_is_one_character_wide_not_an_error() -> None:
    assert ot.wrap_lines("abc", 0) == ["a", "b", "c"]
    assert ot.wrap_lines("abc", -4) == ["a", "b", "c"]


def test_wrap_properties_over_random_texts() -> None:
    for sample in _random_texts(4, n=500):
        for width in (1, 3, 8, 20):
            spans = ot.wrap_spans(sample, width)
            lines = ot.wrap_lines(sample, width)
            assert lines == [sample[a:b] for a, b in spans]
            assert lines
            # nothing but the spaces at a break is lost, and nothing is reordered
            assert "".join(lines).replace(" ", "") == sample.replace(" ", "")
            assert all(len(line) <= width for line in lines)
            # spans ascend without overlap and only spaces lie between two lines
            for (a0, b0), (a1, b1) in pairwise(spans):
                assert a0 <= b0 <= a1 <= b1
                assert set(sample[b0:a1]) <= {" "}
            assert spans[0][0] == 0 or set(sample[: spans[0][0]]) <= {" "}
            # greedy: the first word of a line would not have fitted on the line before it
            for (a0, b0), (a1, b1) in pairwise(spans):
                if b0 < a1 and a1 < b1:  # a break at spaces, and a non-empty next line
                    word = re.match(r"\S+", sample[a1:])
                    assert word is not None
                    assert (b0 - a0) + 1 + min(len(word.group()), width) > width or b0 == a0


def test_wrap_spans_measures_with_the_callers_ruler() -> None:
    """The painter wraps by pixels, not characters: any ruler works, and a coarser one gives the same break points."""
    sample = "the quick brown fox jumps over the lazy dog"
    assert ot.wrap_spans(sample, 10 * 2, measure=lambda s: 2 * len(s)) == ot.wrap_spans(sample, 10)
    # a ruler that makes `m` three times as wide as the rest breaks a line of m's earlier
    wide_m = lambda s: sum(3 if c == "m" else 1 for c in s)  # noqa: E731
    assert ot.wrap_lines("mm mm mm", 8) == ["mm mm mm"]
    assert [(a, b) for a, b in ot.wrap_spans("mm mm mm", 8, measure=wide_m)] == [(0, 2), (3, 5), (6, 8)]
    # a single glyph wider than the line still gets a line of its own (the cut cannot go below one character)
    assert ot.wrap_spans("abc", 1, measure=lambda s: 5 * len(s)) == [(0, 1), (1, 2), (2, 3)]


def test_wrapping_then_painting_keeps_a_hebrew_line_in_one_piece() -> None:
    """Wrapping is done in logical order before bidi: each line is reordered on its own (3.11)."""
    lines = ot.wrap_lines("שלום עולם טוב", 9)
    assert lines == ["שלום עולם", "טוב"]
    assert [ot.bidi_display(line) for line in lines] == ["םלוע םולש", "בוט"]
