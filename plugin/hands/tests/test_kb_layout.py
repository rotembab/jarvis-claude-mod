"""The layouts: U1, U2, U47, A9, A42 (the key part) and A81 (DESIGN-KEYBOARD.md 3.3, 5.2, Appendix B).

This is the only test file that states how many keys a layout has (P11, U47); every other test derives the count.
"""

from __future__ import annotations

import dataclasses
import itertools
import math

import pytest

from jarvis_hands.desktop.keys import ALLOWED_CHARS
from jarvis_hands.keyboard import layout as lay
from jarvis_hands.keyboard.layout import (
    KEY_COUNT,
    LAYOUT,
    LAYOUTS,
    REVIEW_ROW_TABLE,
    ROW_TABLE,
    ROWS,
    WIDTH_U,
    Key,
    char_for,
    key_at,
    layout_for,
    legend,
)

DIRECT = layout_for("direct")
REVIEW = layout_for("review")
BOTH = pytest.mark.parametrize("layout", [DIRECT, REVIEW], ids=lambda layout: layout.name)

# The English rows of the first version, typed out once here so a slip in the table cannot agree with itself.
EN_ROWS = ("qwertyuiop", "asdfghjkl'", "zxcvbnm,./")
# Appendix B: the Hebrew character on each English position.
HE_BY_EN = {
    "q": ",", "w": "'", "e": "ק", "r": "ר", "t": "א", "y": "ט", "u": "ו", "i": "ן", "o": "ם", "p": "פ",
    "a": "ש", "s": "ד", "d": "ג", "f": "כ", "g": "ע", "h": "י", "j": "ח", "k": "ל", "l": "ך", "'": "ף",
    "z": "ז", "x": "ס", "c": "ב", "v": "ה", "b": "נ", "n": "מ", "m": "צ", ",": "ת", ".": "ץ", "/": ".",
    "-": "-", "?": "?",
}  # fmt: skip


def centre(key: Key) -> tuple[float, float]:
    return key.col + key.width / 2.0, key.row + 0.5


# --- U1: the tables ---


def test_u1_every_row_of_both_tables_is_exactly_one_keyboard_wide() -> None:
    assert WIDTH_U == 11.5
    for table in (ROW_TABLE, REVIEW_ROW_TABLE):
        for row in table:
            assert math.isclose(sum(width for _, width, _, _ in row), WIDTH_U, abs_tol=1e-9)


def test_u1_the_direct_rows_are_what_the_first_version_said() -> None:
    kinds = [[cell[0] for cell in row] for row in ROW_TABLE]
    assert kinds[0] == ["char"] * 10 + ["backspace"]
    assert kinds[1] == ["char"] * 10 + ["enter"]
    assert kinds[2] == ["shift"] + ["char"] * 10
    assert kinds[3] == ["lang", "private", "home", "space", "char", "char", "close"]
    for row, letters in zip(ROW_TABLE[:3], EN_ROWS, strict=True):
        assert "".join(en for kind, _, en, _ in row if kind == "char") == letters
    assert [width for _, width, _, _ in ROW_TABLE[3]] == [1.5, 1.0, 1.0, 4.5, 1.0, 1.0, 1.5]
    assert [(en, he) for kind, _, en, he in ROW_TABLE[3] if kind == "char"] == [("-", "-"), ("?", "?")]


def test_u1_the_review_table_is_derived_from_the_direct_one() -> None:
    assert REVIEW_ROW_TABLE[0] == (*ROW_TABLE[0][:10], ("gap", 1.5, "", ""))
    assert REVIEW_ROW_TABLE[1] == (*ROW_TABLE[1][:10], ("backspace", 1.5, "", ""))
    assert REVIEW_ROW_TABLE[2] == ROW_TABLE[2]
    assert REVIEW_ROW_TABLE[3] == ROW_TABLE[3]
    assert [(kind, width) for kind, width, _, _ in REVIEW_ROW_TABLE[4]] == [
        ("clear", 1.5),
        ("enter", 1.5),
        ("gap", 0.25),
        ("chip", 2.0),
        ("chip", 2.0),
        ("chip", 2.0),
        ("gap", 0.25),
        ("insert", 2.0),
    ]


def test_u1_the_counts_are_derived_from_the_tables_and_pinned_here() -> None:
    assert sum(len(row) for row in ROW_TABLE) == KEY_COUNT
    assert len(ROW_TABLE) == ROWS
    assert DIRECT.count == KEY_COUNT == len(DIRECT.keys)
    assert REVIEW.count == len(REVIEW.keys)
    # U47: the numbers themselves, which no other test file may state.
    assert (DIRECT.count, REVIEW.count) == (40, 45)
    assert (DIRECT.rows, REVIEW.rows, ROWS) == (4, 5, 4)


def test_u1_the_old_names_are_the_direct_layout() -> None:
    assert LAYOUT is DIRECT is LAYOUTS["direct"]
    assert LAYOUTS["review"] is REVIEW
    assert set(LAYOUTS) == {"direct", "review"}
    assert layout_for("direct") is layout_for("direct")
    assert key_at(2.5, 1.5) is DIRECT.key_at(2.5, 1.5)


@BOTH
def test_u1_indices_run_from_zero_without_a_gap(layout: lay.Layout) -> None:
    assert [key.index for key in layout.keys] == list(range(layout.count))


@BOTH
def test_u1_key_at_of_every_centre_is_that_key(layout: lay.Layout) -> None:
    for key in layout.keys:
        assert layout.key_at(*centre(key)) is key
        # a chip owns only what lies below the top tolerance of its row: its corner belongs to the row above
        assert layout.key_at(key.col + 0.001, key.row + 0.6) is key
        if key.kind != "chip":
            assert layout.key_at(key.col + 0.001, key.row + 0.001) is key


@BOTH
def test_u1_keys_tile_their_rows_with_the_gaps_and_nothing_else(layout: lay.Layout) -> None:
    for row in range(layout.rows):
        cells = [(key.col, key.col + key.width) for key in layout.keys if key.row == row]
        cells += [(a, b) for r, a, b in layout.gaps if r == row]
        cells.sort()
        assert cells[0][0] == 0.0
        assert cells[-1][1] == WIDTH_U
        assert all(math.isclose(a[1], b[0], abs_tol=1e-9) for a, b in itertools.pairwise(cells))


@BOTH
def test_u1_the_characters_the_layout_can_produce_are_the_allow_list(layout: lay.Layout) -> None:
    produced = {
        char_for(key, lang, shift) for key in layout.keys for lang in ("en", "he") for shift in (False, True)
    } - {""}
    assert produced == ALLOWED_CHARS


def test_u1_english_keys_are_the_26_letters_and_six_marks() -> None:
    chars = [key.en for key in DIRECT.keys if key.kind == "char"]
    assert len(chars) == len(set(chars))
    assert set(chars) == set("abcdefghijklmnopqrstuvwxyz") | set("',./-?")
    assert all(not key.en for key in DIRECT.keys if key.kind != "char")


def test_u1_hebrew_keys_are_27_letters_and_five_marks_all_different() -> None:
    chars = [key.he for key in DIRECT.keys if key.kind == "char"]
    assert len(chars) == len(set(chars))
    letters = [c for c in chars if "א" <= c <= "ת"]
    assert len(letters) == len(chars) - 5
    assert set(chars) - set(letters) == set(",'.-?")
    assert all(not key.he for key in DIRECT.keys if key.kind != "char")


def test_u1_the_layout_is_data_nobody_can_edit() -> None:
    key = DIRECT.keys[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        key.index = 7  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        DIRECT.rows = 9  # type: ignore[misc]
    assert isinstance(DIRECT.keys, tuple)
    assert isinstance(DIRECT.gaps, tuple)


def test_u1_a_key_never_prints_what_it_types() -> None:
    """SR13: a key says which character a tap meant; no repr, str or format of it may carry that."""
    for key in REVIEW.keys:
        assert {repr(key), str(key), f"{key}", f"{key!r}"} == {"<Key>"}
        assert repr([key]) == "[<Key>]"


# --- U2: key_at edges ---


@BOTH
def test_u2_outside_the_keyboard_by_more_than_the_tolerance_is_nobody(layout: lay.Layout) -> None:
    tol = 0.35
    assert layout.key_at(-tol - 0.001, 1.5) is None
    assert layout.key_at(WIDTH_U + tol, 1.5) is None
    assert layout.key_at(5.0, -tol - 0.001) is None
    assert layout.key_at(5.0, layout.rows + tol) is None
    assert layout.key_at(math.nan, 1.5) is None
    assert layout.key_at(5.0, math.nan) is None
    assert layout.key_at(math.inf, 1.5) is None
    assert layout.key_at(5.0, -math.inf) is None


@BOTH
def test_u2_the_tolerance_band_snaps_to_the_edge_key(layout: lay.Layout) -> None:
    tol = 0.35
    assert layout.key_at(-tol, 1.5) is layout.key_at(0.1, 1.5)
    assert layout.key_at(WIDTH_U + tol - 0.001, 1.5) is layout.key_at(WIDTH_U - 0.1, 1.5)
    assert layout.key_at(2.5, -tol) is layout.key_at(2.5, 0.1)
    last = layout.key_at(2.5, layout.rows - 0.1)
    assert last is not None
    assert layout.key_at(2.5, layout.rows + tol - 0.001) is last


@BOTH
def test_u2_left_and_top_edges_belong_to_the_cell_they_open(layout: lay.Layout) -> None:
    assert layout.key_at(1.0, 1.5).en == "s"  # type: ignore[union-attr]
    assert layout.key_at(0.999, 1.5).en == "a"  # type: ignore[union-attr]
    assert layout.key_at(2.5, 1.0).kind == "char"  # type: ignore[union-attr]
    assert layout.key_at(2.5, 1.0).row == 1  # type: ignore[union-attr]
    assert layout.key_at(2.5, 0.999).row == 0  # type: ignore[union-attr]
    assert layout.key_at(1.5, 3.0).kind == "private"  # type: ignore[union-attr]
    assert layout.key_at(1.499, 3.0).kind == "lang"  # type: ignore[union-attr]


@BOTH
def test_u2_a_custom_tolerance_moves_the_band(layout: lay.Layout) -> None:
    assert layout.key_at(-0.2, 1.5, tol=0.1) is None
    assert layout.key_at(-0.2, 1.5, tol=0.25) is not None


@BOTH
def test_u2_every_point_of_the_letter_rows_is_a_key(layout: lay.Layout) -> None:
    """No dead zones among the letters: a sweep of the plane finds a key on every letter row."""
    for iv in range(30):
        v = 0.65 + iv * 0.1  # rows 1 to 3, which the dead cells never touch
        for iu in range(115):
            assert layout.key_at(iu * 0.1, v) is not None


def test_u2_the_direct_row_zero_has_no_dead_cell() -> None:
    assert DIRECT.gaps == ()
    assert DIRECT.key_at(10.75, 0.5).kind == "backspace"  # type: ignore[union-attr]
    assert DIRECT.key_at(10.75, 1.5).kind == "enter"  # type: ignore[union-attr]


# --- U47: review layout ---


def test_u47_the_direct_keys_keep_their_indices_in_both_layouts() -> None:
    for direct in DIRECT.keys:
        review = REVIEW.keys[direct.index]
        assert (review.index, review.kind, review.en, review.he, review.width) == (
            direct.index,
            direct.kind,
            direct.en,
            direct.he,
            direct.width,
        )
        if direct.kind not in ("backspace", "enter"):
            assert (review.row, review.col) == (direct.row, direct.col)


def test_u47_backspace_ends_the_home_row_and_the_enter_key_moves_to_the_bottom() -> None:
    assert DIRECT.keys[10].kind == "backspace"
    assert (DIRECT.keys[10].row, DIRECT.keys[10].col) == (0, 10.0)
    assert DIRECT.keys[21].kind == "enter"
    assert (DIRECT.keys[21].row, DIRECT.keys[21].col) == (1, 10.0)
    assert (REVIEW.keys[10].kind, REVIEW.keys[10].row, REVIEW.keys[10].col) == ("backspace", 1, 10.0)
    assert (REVIEW.keys[21].kind, REVIEW.keys[21].row, REVIEW.keys[21].col) == ("enter", 4, 1.5)
    assert REVIEW.keys[21].width == 1.5


def test_u47_the_new_keys_are_numbered_last() -> None:
    clear, insert, *chips = REVIEW.keys[40:]
    assert (clear.kind, clear.row, clear.col, clear.width) == ("clear", 4, 0.0, 1.5)
    assert (insert.kind, insert.row, insert.col, insert.width) == ("insert", 4, 9.5, 2.0)
    assert [(c.index, c.kind, c.row, c.col, c.width) for c in chips] == [
        (42, "chip", 4, 3.25, 2.0),
        (43, "chip", 4, 5.25, 2.0),
        (44, "chip", 4, 7.25, 2.0),
    ]
    for kind in ("clear", "insert"):
        assert sum(1 for key in REVIEW.keys if key.kind == kind) == 1
    assert sum(1 for key in DIRECT.keys if key.kind in ("clear", "insert", "chip")) == 0


def test_u47_the_gaps_and_the_home_row() -> None:
    assert DIRECT.gaps == ()
    assert REVIEW.gaps == ((0, 10.0, WIDTH_U), (4, 3.0, 3.25), (4, 9.25, 9.5))
    assert DIRECT.home_v == REVIEW.home_v == 1.5
    assert (DIRECT.name, REVIEW.name) == ("direct", "review")


def test_u47_a_tap_in_a_dead_cell_is_dropped_except_at_the_edge_of_a_typing_row() -> None:
    assert REVIEW.key_at(10.75, 0.2) is None
    assert REVIEW.key_at(10.0, 0.2) is None
    assert REVIEW.key_at(11.49, 0.2) is None
    assert REVIEW.key_at(10.75, 0.65).kind == "backspace"  # type: ignore[union-attr]
    assert REVIEW.key_at(10.75, 0.64) is None
    assert REVIEW.key_at(3.1, 4.5) is None
    assert REVIEW.key_at(3.1, 4.35).kind == "home"  # type: ignore[union-attr]
    assert REVIEW.key_at(3.1, 4.36) is None
    assert REVIEW.key_at(9.4, 4.2).kind == "char"  # type: ignore[union-attr]
    assert REVIEW.key_at(9.4, 4.2).en == "?"  # type: ignore[union-attr]


def test_u47_a_chip_answers_its_chip_below_the_top_tolerance_and_the_key_above_in_it() -> None:
    assert REVIEW.key_at(4.25, 4.5).index == 42  # type: ignore[union-attr]
    assert REVIEW.key_at(6.25, 4.5).index == 43  # type: ignore[union-attr]
    assert REVIEW.key_at(8.25, 4.5).index == 44  # type: ignore[union-attr]
    assert REVIEW.key_at(6.25, 4.2).kind == "space"  # type: ignore[union-attr]
    assert REVIEW.key_at(6.25, 4.36).kind == "chip"  # type: ignore[union-attr]
    assert REVIEW.key_at(6.25, 4.99).kind == "chip"  # type: ignore[union-attr]


def test_u47_clear_send_and_insert_own_their_whole_cells() -> None:
    for kind, u in (("clear", 0.75), ("enter", 2.25), ("insert", 10.5)):
        for dv in (0.0, 0.1, 0.35, 0.36, 0.5, 0.99):
            key = REVIEW.key_at(u, 4.0 + dv)
            assert key is not None
            assert key.kind == kind
    # their edges are theirs too, down to the tolerance
    assert REVIEW.key_at(0.0, 4.0).kind == "clear"  # type: ignore[union-attr]
    assert REVIEW.key_at(1.5, 4.0).kind == "enter"  # type: ignore[union-attr]
    assert REVIEW.key_at(3.0, 4.9) is None
    assert REVIEW.key_at(9.5, 4.0).kind == "insert"  # type: ignore[union-attr]


def test_u47_find_by_kind_and_character_in_both_languages() -> None:
    assert REVIEW.find("backspace").index == 10
    assert REVIEW.find("enter").index == 21
    assert REVIEW.find("clear").index == 40
    assert REVIEW.find("insert").index == 41
    assert REVIEW.find("space").width == 4.5
    assert REVIEW.find(char="a").en == "a"
    assert REVIEW.find(char="ש", lang="he").en == "a"
    assert REVIEW.find(char="'", lang="en").en == "'"
    assert REVIEW.find(char="'", lang="he").en == "w"  # Hebrew has an apostrophe on the w position and a final pe on '
    assert DIRECT.find(char=" ").kind == "space"
    for missing in (lambda: DIRECT.find("clear"), lambda: DIRECT.find(char="!"), lambda: DIRECT.find(char="7")):
        with pytest.raises(KeyError):
            missing()
    with pytest.raises(KeyError):
        DIRECT.find()


def test_u47_find_agrees_with_char_for_for_every_key() -> None:
    for layout in (DIRECT, REVIEW):
        for key in layout.keys:
            if key.kind != "char":
                continue
            assert layout.find(char=key.en, lang="en") is key
            assert layout.find(char=key.he, lang="he") is key


def test_u47_legends() -> None:
    assert legend(REVIEW.find("enter"), "en", False, "review") == "Send"
    assert legend(REVIEW.find("enter"), "he", False, "review") == "Send"
    assert legend(DIRECT.find("enter"), "en", False) == "Enter"
    assert legend(DIRECT.find("enter"), "en", False, "direct") == "Enter"
    assert legend(REVIEW.find("insert"), "en", False, "review") == "Insert"
    assert legend(REVIEW.find("clear"), "he", False, "review") == "Clear"
    assert legend(REVIEW.keys[42], "en", False, "review") == ""
    assert legend(REVIEW.keys[44], "he", True, "review") == ""
    assert legend(DIRECT.find("backspace"), "en", False) == "Bksp"
    assert legend(REVIEW.find("backspace"), "he", False, "review") == "Bksp"
    assert legend(DIRECT.find("shift"), "en", False) == "Shift"
    assert legend(DIRECT.find("lang"), "en", False) == "EN"
    assert legend(DIRECT.find("lang"), "he", False) == "HE"
    assert legend(DIRECT.find("private"), "en", False) == "Priv"
    assert legend(DIRECT.find("home"), "en", False) == "Home"
    assert legend(DIRECT.find("space"), "en", False) == " "
    assert legend(DIRECT.find("close"), "en", False) == "Close"
    assert legend(DIRECT.find(char="a"), "en", False) == "a"
    assert legend(DIRECT.find(char="a"), "en", True) == "A"
    assert legend(DIRECT.find(char="a"), "he", True) == "ש"


def test_u47_shift_capitalises_english_letters_only() -> None:
    a = DIRECT.find(char="a")
    assert (char_for(a, "en", False), char_for(a, "en", True)) == ("a", "A")
    assert (char_for(a, "he", False), char_for(a, "he", True)) == ("ש", "ש")
    comma = DIRECT.find(char=",")
    assert char_for(comma, "en", True) == ","
    assert char_for(comma, "he", True) == "ת"
    for kind in ("shift", "space", "enter", "backspace", "close", "private", "home", "lang"):
        assert char_for(DIRECT.find(kind), "en", True) == ""
    for key in REVIEW.keys[40:]:
        assert char_for(key, "en", False) == ""
        assert char_for(key, "he", True) == ""


# --- A9: Hebrew by position ---


def test_a9_every_character_key_gives_its_appendix_b_hebrew_character() -> None:
    seen = {}
    for key in DIRECT.keys:
        if key.kind != "char":
            continue
        pressed = DIRECT.key_at(*centre(key))
        assert pressed is key
        seen[key.en] = char_for(pressed, "he", False)
    assert seen == HE_BY_EN


# --- A42: dead cells ---


def test_a42_row_zero_dead_cell_and_row_four_gap_cell_edge_cases() -> None:
    top = REVIEW.key_at
    # the end of row 0: dropped in the middle, Backspace within 0.35 of its bottom edge, dropped again above
    assert top(10.75, 0.5) is None
    assert top(10.75, 1.0 - 0.2).kind == "backspace"  # type: ignore[union-attr]
    assert top(10.75, 1.0 - 0.5) is None
    # a row-4 dead cell: dropped in the middle, Home above it within 0.35 of its top edge, dropped below
    assert top(3.1, 4.5) is None
    assert top(3.1, 4.0 + 0.2).kind == "home"  # type: ignore[union-attr]
    assert top(3.1, 4.0 + 0.5) is None


def test_a42_the_second_row_four_gap_cell_gives_the_question_mark_above_it() -> None:
    assert REVIEW.key_at(9.4, 4.0 + 0.2).en == "?"  # type: ignore[union-attr]
    assert REVIEW.key_at(9.4, 4.0 + 0.5) is None
    assert REVIEW.key_at(9.4, 5.2) is None


# --- A81: chip cells ---


def test_a81_chip_cells_and_the_pinned_keys() -> None:
    mids = {42: 4.25, 43: 6.25, 44: 8.25}
    above = {42: "space", 43: "space", 44: "char"}  # the keys of row 3 at those u: Space, Space and the hyphen
    for index, u in mids.items():
        assert REVIEW.key_at(u, 4.5).index == index  # type: ignore[union-attr]
        assert REVIEW.key_at(u, 4.0 + 0.20).kind == above[index]  # type: ignore[union-attr]
        assert REVIEW.key_at(u, 4.0 + 0.50).index == index  # type: ignore[union-attr]
        assert REVIEW.key_at(u, 4.99).index == index  # type: ignore[union-attr]  # the bottom edge
        left, right = u - 0.99, u + 0.99  # the sides of the cell
        assert REVIEW.key_at(left, 4.6).index == index  # type: ignore[union-attr]
        assert REVIEW.key_at(right, 4.6).index == index  # type: ignore[union-attr]
    assert REVIEW.key_at(3.1, 4.6) is None
    assert REVIEW.key_at(9.4, 4.6) is None
    assert REVIEW.key_at(10.75, 0.3) is None
    assert REVIEW.key_at(3.3, 4.0 + 0.20).kind == "home"  # type: ignore[union-attr]  # the first chip starts over Home
    assert [REVIEW.keys[i].index for i in range(DIRECT.count)] == list(range(DIRECT.count))
    assert REVIEW.count == 45


def test_a81_the_chip_below_is_reached_below_the_bottom_tolerance_only_as_the_last_row() -> None:
    assert REVIEW.key_at(4.25, 5.0 + 0.34).index == 42  # type: ignore[union-attr]
    assert REVIEW.key_at(4.25, 5.0 + 0.35) is None


def test_the_module_names_the_stub_left_behind_are_gone() -> None:
    assert not hasattr(lay, "STUB_OWNER")
    assert not hasattr(lay, "_STUB_DATA")
    with pytest.raises(AttributeError):
        _ = lay.no_such_name_at_all  # type: ignore[attr-defined]
