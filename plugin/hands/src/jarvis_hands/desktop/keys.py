"""What the air keyboard may type, and the input events that type it.

The allow-list lives here, below the layout, and is enforced when a ``KeyStroke`` is
*constructed*: nothing the layout, the session or the sink does can widen it, and a
stroke that exists is by construction one of the 85 characters or three controls
below. ``events_for`` checks the stroke again (the second layer; the sink and the
Windows desktop are the third and fourth), so a stroke that was forced past
``__post_init__`` with ``object.__setattr__`` still cannot become input.

No digit, no ``!`` or ``;``, no Esc, Tab, arrow, Delete or function key, no chord and
no character outside the basic plane: those are the keys with a meaning of their own
to Claude Code or to Windows (permission prompts take digits, a leading ``!`` is shell
mode, Esc interrupts). Everything refused raises ``KeyRefused`` with a fixed text,
because an exception text is data too and must never hold a typed character.

Pure: standard library only, no I/O.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

EN_LETTERS = "abcdefghijklmnopqrstuvwxyz"
#: The 22 letters and the 5 finals, which are keys of their own on the Hebrew layout.
HE_LETTERS = "".join(chr(c) for c in range(0x05D0, 0x05EB))
ALLOWED_CHARS: frozenset[str] = frozenset(EN_LETTERS + EN_LETTERS.upper() + HE_LETTERS + "',./-?")
ALLOWED_CONTROLS: frozenset[str] = frozenset({"space", "backspace", "enter"})
#: The review box's alphabet: what a key can type, and the space that the run lane types as the ``space`` control.
COMPOSE_CHARS: frozenset[str] = ALLOWED_CHARS | frozenset({" "})

KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE, KEYEVENTF_SCANCODE = 0x1, 0x2, 0x4, 0x8
INPUT_KEYBOARD = 1
#: (virtual key, scan code) of the three controls. None is an extended key, so no flag is ever needed.
CONTROL_KEYS = MappingProxyType({"space": (0x20, 0x39), "backspace": (0x08, 0x0E), "enter": (0x0D, 0x1C)})
#: Our own Shift, pressed around a shifted character in ``vk`` mode so a physical Shift is never borrowed.
VK_LSHIFT, SCAN_LSHIFT = 0xA0, 0x2A

#: char -> (virtual key, shift state bits, scan code) on the target window's keyboard layout, or None for "no such key".
VkLookup = Callable[[str], tuple[int, int, int] | None]

_KINDS = ("char", "control")
_INJECTIONS = ("unicode", "vk")
_SHIFT_STATE = 1
#: The virtual keys a printable character can sit on, on any layout: the digit row (AZERTY puts "-" there), the
#: letters, the OEM punctuation keys and the extra key of the 102-key layout. A lookup that names anything else (Win,
#: Esc, Delete, a function key, VK_PACKET) is not believed, and the character goes in as Unicode.
_CHARACTER_KEYS = frozenset((*range(0x30, 0x3A), *range(0x41, 0x5B), *range(0xBA, 0xC1), *range(0xDB, 0xE0), 0xE2))


class KeyRefused(ValueError):
    """A stroke outside the allow-list. The text is fixed: what was refused is never part of it."""


def _refuse() -> KeyRefused:
    return KeyRefused("That key is not on the air keyboard.")


def _check(kind: object, value: object) -> None:
    # Exact types, not isinstance: a str subclass could answer every comparison with True.
    if type(kind) is not str or type(value) is not str:
        raise _refuse()
    if kind == "char":
        if value not in ALLOWED_CHARS:
            raise _refuse()
    elif kind == "control":
        if value not in ALLOWED_CONTROLS:
            raise _refuse()
    else:
        raise _refuse()


@dataclass(frozen=True, slots=True, repr=False)
class KeyStroke:
    kind: Literal["char", "control"]
    #: char: exactly one allowed character; control: one of ``ALLOWED_CONTROLS``.
    value: str

    def __post_init__(self) -> None:
        _check(self.kind, self.value)

    def __repr__(self) -> str:
        # A typed character is as private as the text (SR13); a control key carries no content.
        return f"<KeyStroke control {self.value}>" if self.kind == "control" else "<KeyStroke char>"


def _unicode_events(char: str) -> list[tuple[int, int, int]]:
    code = ord(char)
    return [(0, code, KEYEVENTF_UNICODE), (0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)]


def _usable(found: object) -> tuple[int, int, int] | None:
    """The lookup's answer when it names a plain key, else None (the caller then falls back to Unicode)."""
    if not (isinstance(found, tuple) and len(found) == 3 and all(type(x) is int for x in found)):
        return None
    vk, state, scan = found
    # Only "no modifier" and "Shift" can be pressed exactly: Ctrl and Alt bits are AltGr or a chord, bit 3 is Hankaku.
    if not (vk in _CHARACTER_KEYS and 0 <= scan <= 0xFFFF and state in (0, _SHIFT_STATE)):
        return None
    return found


def events_for(
    stroke: KeyStroke,
    *,
    inject: Literal["unicode", "vk"] = "unicode",
    vk_lookup: VkLookup | None = None,
    shift_down: bool = False,
) -> list[tuple[int, int, int]]:
    """``(wVk, wScan, dwFlags)`` triples that type the stroke. Every list is balanced: each down has its up in it."""
    _check(stroke.kind, stroke.value)
    if inject not in _INJECTIONS:
        raise ValueError("inject must be 'unicode' or 'vk'")
    if stroke.kind == "control":
        vk, scan = CONTROL_KEYS[stroke.value]
        return [(vk, scan, 0), (vk, scan, KEYEVENTF_KEYUP)]
    if inject == "unicode" or vk_lookup is None:
        return _unicode_events(stroke.value)
    key = _usable(vk_lookup(stroke.value))
    if key is None:
        return _unicode_events(stroke.value)
    vk, state, scan = key
    needs_shift = state == _SHIFT_STATE
    if shift_down and not needs_shift:
        # A physical Shift would change the character this layout gives; Unicode input ignores it.
        return _unicode_events(stroke.value)
    press = [(vk, scan, 0), (vk, scan, KEYEVENTF_KEYUP)]
    if needs_shift and not shift_down:
        return [(VK_LSHIFT, SCAN_LSHIFT, 0), *press, (VK_LSHIFT, SCAN_LSHIFT, KEYEVENTF_KEYUP)]
    return press
