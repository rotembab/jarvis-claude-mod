"""The key allow-list: refused in ``KeyStroke`` itself, below the layout (SR3 layers 1 and 2, P9, U41 sans layout).

Nothing the layout, the session or the sink does can widen what ``desktop/keys.py`` accepts, so these tests
look at the stroke and at ``events_for`` alone.
"""

from __future__ import annotations

import dataclasses
import random
from typing import Any

import pytest

from jarvis_hands.desktop import keys
from jarvis_hands.desktop.keys import (
    ALLOWED_CHARS,
    ALLOWED_CONTROLS,
    COMPOSE_CHARS,
    CONTROL_KEYS,
    EN_LETTERS,
    HE_LETTERS,
    KEYEVENTF_KEYUP,
    KEYEVENTF_UNICODE,
    KeyRefused,
    KeyStroke,
    events_for,
)

SENTINEL = "q"
HEBREW_SENTINEL = "ש"


def balanced(events: list[tuple[int, int, int]]) -> bool:
    """Every down has its up in the same list, and no up comes before its down."""
    held: dict[tuple[int, int, bool], int] = {}
    for vk, scan, flags in events:
        key = (vk, scan, bool(flags & KEYEVENTF_UNICODE))
        if flags & KEYEVENTF_KEYUP:
            if held.get(key, 0) <= 0:
                return False
            held[key] -= 1
        else:
            held[key] = held.get(key, 0) + 1
    return not any(held.values())


# --------------------------------------------------------------------------- the alphabet


def test_the_alphabet_is_the_letters_of_both_languages_and_six_marks() -> None:
    assert EN_LETTERS == "abcdefghijklmnopqrstuvwxyz"
    # 22 letters and 5 finals; the finals are separate keys of the Hebrew layout.
    assert len(HE_LETTERS) == 27 == len(set(HE_LETTERS))
    assert all(0x05D0 <= ord(c) <= 0x05EA for c in HE_LETTERS)
    assert {"ך", "ם", "ן", "ף", "ץ"} <= set(HE_LETTERS)
    assert frozenset(EN_LETTERS + EN_LETTERS.upper() + HE_LETTERS + "',./-?") == ALLOWED_CHARS
    assert len(ALLOWED_CHARS) == 26 + 26 + 27 + 6
    # Not a single character with a meaning of its own to Claude Code or to Windows.
    assert not ALLOWED_CHARS & set("0123456789!;\\ \t\n\r\x1b\x7f")
    assert frozenset({"space", "backspace", "enter"}) == ALLOWED_CONTROLS


def test_the_box_alphabet_is_the_allowed_characters_plus_space() -> None:
    """U41 without the layout (the layout half is U1 and U47): the box admits what a key can type, and a space."""
    assert ALLOWED_CHARS | {" "} == COMPOSE_CHARS
    assert COMPOSE_CHARS - {" "} == ALLOWED_CHARS
    assert isinstance(ALLOWED_CHARS, frozenset) and isinstance(COMPOSE_CHARS, frozenset)


# --------------------------------------------------------------------------- the allow-list in KeyStroke itself


@pytest.mark.parametrize("char", sorted(ALLOWED_CHARS))
def test_every_allowed_character_makes_a_stroke(char: str) -> None:
    stroke = KeyStroke("char", char)
    assert (stroke.kind, stroke.value) == ("char", char)


@pytest.mark.parametrize("name", sorted(ALLOWED_CONTROLS))
def test_every_allowed_control_makes_a_stroke(name: str) -> None:
    assert KeyStroke("control", name).value == name


def test_no_other_character_of_the_basic_plane_makes_a_stroke() -> None:
    for code in range(0x10000):
        char = chr(code)
        if char in ALLOWED_CHARS:
            continue
        with pytest.raises(KeyRefused):
            KeyStroke("char", char)


@pytest.mark.parametrize("code", [0x10000, 0x1F600, 0x1D400, 0x2F800, 0xE0041, 0x10FFFF])
def test_a_character_outside_the_basic_plane_is_refused(code: int) -> None:
    with pytest.raises(KeyRefused):
        KeyStroke("char", chr(code))


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        # digits, symbols, control characters
        ("char", "0"),
        ("char", "7"),
        ("char", "!"),
        ("char", ";"),
        ("char", " "),  # a space is the control "space", never a character
        ("char", "\t"),
        ("char", "\n"),
        ("char", "\r"),
        ("char", "\x1b"),
        ("char", "\x08"),
        ("char", "\x7f"),
        ("char", "\x00"),
        # more or fewer than one character: chords, names, strings
        ("char", ""),
        ("char", "ab"),
        ("char", "שלום"),
        ("char", "a "),
        ("char", "ctrl+c"),
        ("char", "alt+f4"),
        ("char", "win"),
        ("char", "esc"),
        ("char", "tab"),
        ("char", "left"),
        ("char", "delete"),
        ("char", "f1"),
        ("char", "enter"),
        # controls that are not on the list
        ("control", "tab"),
        ("control", "esc"),
        ("control", "escape"),
        ("control", "delete"),
        ("control", "left"),
        ("control", "up"),
        ("control", "home"),
        ("control", "end"),
        ("control", "pageup"),
        ("control", "insert"),
        ("control", "f1"),
        ("control", "f12"),
        ("control", "ctrl"),
        ("control", "alt"),
        ("control", "win"),
        ("control", "shift"),
        ("control", "ctrl+c"),
        ("control", "Space"),
        ("control", "ENTER"),
        ("control", " space"),
        ("control", ""),
        ("control", "a"),
        # a name that is neither
        ("key", "a"),
        ("key", "space"),
        ("CHAR", "a"),
        ("Char", "a"),
        ("", "a"),
        ("chord", "a"),
    ],
)
def test_everything_else_is_refused(kind: str, value: str) -> None:
    with pytest.raises(KeyRefused):
        KeyStroke(kind, value)  # type: ignore[arg-type]


class _Sneaky(str):
    """A string that compares equal to an allowed character but is not one."""

    def __eq__(self, other: object) -> bool:
        return True

    def __hash__(self) -> int:
        return hash("a")


@pytest.mark.parametrize(
    "value",
    [1, 97, None, b"a", ("a",), ["a"], {"a"}, 1.5, True, object(), _Sneaky("7")],
    ids=lambda v: type(v).__name__,
)
def test_a_value_that_is_not_a_plain_string_is_refused(value: Any) -> None:
    with pytest.raises(KeyRefused):
        KeyStroke("char", value)
    with pytest.raises(KeyRefused):
        KeyStroke("control", value)


@pytest.mark.parametrize("kind", [None, 1, b"char", ("char",)])
def test_a_kind_that_is_not_a_string_is_refused(kind: Any) -> None:
    with pytest.raises(KeyRefused):
        KeyStroke(kind, "a")


def test_a_stroke_cannot_be_changed_or_rebuilt_around_the_check() -> None:
    stroke = KeyStroke("char", "a")
    with pytest.raises(dataclasses.FrozenInstanceError):
        stroke.value = "7"  # type: ignore[misc]
    with pytest.raises(KeyRefused):
        dataclasses.replace(stroke, value="7")
    with pytest.raises(KeyRefused):
        dataclasses.replace(stroke, kind="control")  # "a" is not a control


def test_events_for_checks_the_stroke_again() -> None:
    """The second layer: a stroke that got around ``__post_init__`` still cannot become input."""
    stroke = KeyStroke("char", "a")
    object.__setattr__(stroke, "value", "7")
    for inject in ("unicode", "vk"):
        with pytest.raises(KeyRefused):
            events_for(stroke, inject=inject, vk_lookup=lambda c: (0x37, 0, 0x08))  # type: ignore[arg-type]
    control = KeyStroke("control", "space")
    object.__setattr__(control, "value", "delete")
    with pytest.raises(KeyRefused):
        events_for(control)
    other = KeyStroke("char", "a")
    object.__setattr__(other, "kind", "chord")
    with pytest.raises(KeyRefused):
        events_for(other)


def test_the_allow_lists_cannot_be_changed_at_run_time() -> None:
    assert isinstance(keys.ALLOWED_CHARS, frozenset) and isinstance(keys.COMPOSE_CHARS, frozenset)
    assert isinstance(keys.ALLOWED_CONTROLS, frozenset)
    with pytest.raises(AttributeError):
        keys.ALLOWED_CHARS.add("7")  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        keys.ALLOWED_CONTROLS.add("tab")  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- what a refusal says and shows


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        ("char", "Ω€"),
        ("char", "€"),
        ("char", "ب"),
        ("control", "ctrl+Ω"),
        ("kΩy", "a"),
        ("char", "Ω" * 64),
        ("char", "\x1b€"),
    ],
)
def test_a_refusal_has_a_fixed_text_that_never_holds_what_was_refused(kind: str, value: str) -> None:
    """SR13: an exception text is data too; ``KeyRefused('x')`` would put a typed character into a log or a reply."""
    assert issubclass(KeyRefused, ValueError)
    with pytest.raises(KeyRefused) as caught:
        KeyStroke(kind, value)
    for shown in (str(caught.value), repr(caught.value), repr(caught.value.args), repr(caught.value.__dict__)):
        for part in {value, kind, *value}:
            if part not in "char control":  # the fixed text may name the two kinds
                assert part not in shown
    assert caught.value.__cause__ is None and caught.value.__context__ is None


def test_a_stroke_does_not_show_its_character() -> None:
    assert SENTINEL not in repr(KeyStroke("char", SENTINEL))
    assert SENTINEL not in str(KeyStroke("char", SENTINEL))
    assert HEBREW_SENTINEL not in repr(KeyStroke("char", HEBREW_SENTINEL))
    assert "space" in repr(KeyStroke("control", "space"))  # a control key carries no typed content
    assert KeyStroke("char", SENTINEL) == KeyStroke("char", SENTINEL)
    assert KeyStroke("char", SENTINEL) != KeyStroke("char", "w")
    assert hash(KeyStroke("char", SENTINEL)) == hash(KeyStroke("char", SENTINEL))


# --------------------------------------------------------------------------- events_for


def test_pinned_input_constants() -> None:
    assert (keys.KEYEVENTF_EXTENDEDKEY, keys.KEYEVENTF_KEYUP, keys.KEYEVENTF_UNICODE, keys.KEYEVENTF_SCANCODE) == (
        0x1,
        0x2,
        0x4,
        0x8,
    )
    assert keys.INPUT_KEYBOARD == 1
    assert CONTROL_KEYS == {"space": (0x20, 0x39), "backspace": (0x08, 0x0E), "enter": (0x0D, 0x1C)}
    assert set(CONTROL_KEYS) == ALLOWED_CONTROLS
    assert (keys.VK_LSHIFT, keys.SCAN_LSHIFT) == (0xA0, 0x2A)


@pytest.mark.parametrize("char", ["a", "Z", "ש", "ם", "'", ",", ".", "/", "-", "?"])
def test_unicode_injection_is_two_events_with_no_virtual_key(char: str) -> None:
    code = ord(char)
    assert events_for(KeyStroke("char", char)) == [
        (0, code, KEYEVENTF_UNICODE),
        (0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
    ]
    assert events_for(KeyStroke("char", char), inject="unicode") == events_for(KeyStroke("char", char))


@pytest.mark.parametrize("name", sorted(ALLOWED_CONTROLS))
@pytest.mark.parametrize("inject", ["unicode", "vk"])
def test_a_control_is_a_virtual_key_with_its_scan_code_and_is_never_extended(name: str, inject: Any) -> None:
    vk, scan = CONTROL_KEYS[name]
    events = events_for(KeyStroke("control", name), inject=inject)
    assert events == [(vk, scan, 0), (vk, scan, KEYEVENTF_KEYUP)]
    assert not any(flags & keys.KEYEVENTF_EXTENDEDKEY for _, _, flags in events)


def lookup_for(vk: int, state: int, scan: int) -> Any:
    return lambda char: (vk, state, scan)


def test_vk_injection_uses_the_targets_virtual_key_and_scan_code() -> None:
    events = events_for(KeyStroke("char", "a"), inject="vk", vk_lookup=lookup_for(0x41, 0, 0x1E))
    assert events == [(0x41, 0x1E, 0), (0x41, 0x1E, KEYEVENTF_KEYUP)]


def test_vk_injection_wraps_a_shifted_character_in_our_own_shift() -> None:
    events = events_for(KeyStroke("char", "A"), inject="vk", vk_lookup=lookup_for(0x41, 1, 0x1E))
    assert events == [
        (keys.VK_LSHIFT, keys.SCAN_LSHIFT, 0),
        (0x41, 0x1E, 0),
        (0x41, 0x1E, KEYEVENTF_KEYUP),
        (keys.VK_LSHIFT, keys.SCAN_LSHIFT, KEYEVENTF_KEYUP),
    ]
    assert balanced(events)


def test_vk_injection_does_not_press_shift_that_is_already_down() -> None:
    events = events_for(KeyStroke("char", "A"), inject="vk", vk_lookup=lookup_for(0x41, 1, 0x1E), shift_down=True)
    assert events == [(0x41, 0x1E, 0), (0x41, 0x1E, KEYEVENTF_KEYUP)]


@pytest.mark.parametrize(
    "lookup",
    [
        None,  # no lookup at all
        lambda char: None,  # the layout has no key for it
        lookup_for(0x41, 2, 0x1E),  # Ctrl
        lookup_for(0x41, 4, 0x1E),  # Alt
        lookup_for(0x41, 6, 0x1E),  # AltGr is Ctrl+Alt
        lookup_for(0x41, 7, 0x1E),
        lookup_for(0x41, 8, 0x1E),  # Hankaku: not a state we know how to press
        lambda char: (0, 0, 0),  # no virtual key
        lambda char: (0x141, 0, 0x1E),  # not a virtual key
        lambda char: (0x41, 0, 0x1E, 0),  # not the pinned triple
        lambda char: ("a", 0, 0x1E),
        lambda char: (True, 0, 0x1E),
        lambda char: 0x41,
    ],
    ids=[
        "none",
        "no-key",
        "ctrl",
        "alt",
        "altgr",
        "ctrl-alt-shift",
        "hankaku",
        "no-vk",
        "big-vk",
        "pair",
        "str",
        "bool",
        "int",
    ],
)
def test_vk_injection_falls_back_to_unicode_whenever_it_cannot_be_exact(lookup: Any) -> None:
    assert events_for(KeyStroke("char", "a"), inject="vk", vk_lookup=lookup) == events_for(KeyStroke("char", "a"))


#: The virtual keys a printable character can sit on (an independent copy): the digit row (AZERTY puts "-" there),
#: the letters, the OEM punctuation keys and the extra key of the 102-key layout.
CHARACTER_KEYS = {*range(0x30, 0x3A), *range(0x41, 0x5B), *range(0xBA, 0xC1), *range(0xDB, 0xE0), 0xE2}


@pytest.mark.parametrize("state", [0, 1])
def test_vk_injection_presses_only_the_keys_of_printable_characters(state: int) -> None:
    """Defence in depth: a lookup that names Windows, Esc or VK_PACKET is not believed, whatever the character."""
    for vk in range(0x100):
        events = events_for(KeyStroke("char", "a"), inject="vk", vk_lookup=lookup_for(vk, state, 0x1E))
        if vk in CHARACTER_KEYS:
            assert all(code == vk or code == keys.VK_LSHIFT for code, _, flags in events if not flags & 4), vk
            assert events[0][2] == 0 and not any(flags & keys.KEYEVENTF_UNICODE for _, _, flags in events), vk
        else:
            assert events == events_for(KeyStroke("char", "a")), vk  # the Unicode events


@pytest.mark.parametrize("vk", [0x03, 0x08, 0x09, 0x0D, 0x1B, 0x20, 0x2E, 0x5B, 0x5C, 0x5D, 0x5F, 0x70, 0xA2, 0xE7])
def test_a_control_or_system_key_from_the_lookup_is_never_pressed(vk: int) -> None:
    for char in ("a", "A", ",", "ש"):
        events = events_for(KeyStroke("char", char), inject="vk", vk_lookup=lookup_for(vk, 0, 0x1E))
        assert events == events_for(KeyStroke("char", char))
        assert all(code == 0 and flags & keys.KEYEVENTF_UNICODE for code, _, flags in events)


def test_vk_injection_falls_back_when_a_physical_shift_is_down_and_the_character_needs_none() -> None:
    events = events_for(KeyStroke("char", "a"), inject="vk", vk_lookup=lookup_for(0x41, 0, 0x1E), shift_down=True)
    assert events == events_for(KeyStroke("char", "a"))


def test_the_injection_mode_must_be_one_of_the_two() -> None:
    for bad in ("", "scan", "VK", None, 1):
        with pytest.raises(ValueError, match="inject"):
            events_for(KeyStroke("char", "a"), inject=bad)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- P9: balanced, allowed, whatever the input


def test_random_strokes_give_balanced_lists_of_allowed_characters_only() -> None:
    """P9: any string through the stroke and ``events_for`` leaves no key down and types only the alphabet."""
    rng = random.Random(20261008)
    pool = (
        list(ALLOWED_CHARS)
        + list('0123456789!@#$%^&*()_+=[]{};:"<>\\|`~ \t\n\x1b\x7f')
        + ["😀", "\U0001d41a", "é", "ß", "ａ"]
    )
    lookups: list[Any] = [
        None,
        lambda c: None,
        lambda c: (0x41 + ord(c) % 26, rng.choice([0, 1, 2, 4, 6, 8]), 0x10 + ord(c) % 37),
        lookup_for(0x41, 0, 0x1E),
    ]
    made = refused = 0
    for _ in range(4000):
        kind = rng.choice(["char", "char", "char", "control", "control", "bogus"])
        value = rng.choice(pool) if kind == "char" else rng.choice([*ALLOWED_CONTROLS, "tab", "esc", "ctrl", ""])
        try:
            stroke = KeyStroke(kind, value)  # type: ignore[arg-type]
        except KeyRefused:
            refused += 1
            assert value not in ALLOWED_CHARS or kind != "char"
            continue
        made += 1
        for inject in ("unicode", "vk"):
            events = events_for(stroke, inject=inject, vk_lookup=rng.choice(lookups), shift_down=rng.random() < 0.3)  # type: ignore[arg-type]
            assert events and balanced(events)
            for vk, scan, flags in events:
                if flags & KEYEVENTF_UNICODE:
                    assert vk == 0 and chr(scan) in ALLOWED_CHARS and stroke.kind == "char"
                assert 0 <= vk <= 0xFF and 0 <= scan <= 0xFFFF
                assert not flags & ~(KEYEVENTF_KEYUP | KEYEVENTF_UNICODE)
    assert made > 1000 and refused > 1000
