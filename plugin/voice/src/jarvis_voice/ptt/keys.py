"""Push-to-talk key names and hold detection (pure logic, no keyboard library).

Key names are what users type in settings: ``right ctrl``, ``f13``,
``alt+space``, ``ctrl+shift+j``, ``caps lock``. They parse to canonical tokens
that match pynput's ``Key`` names (``ctrl_r``, ``f13``, ``space``), ``char:<c>``
for printable keys, or ``vk:<n>`` for raw virtual-key codes.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from dataclasses import dataclass

_MODIFIERS = {
    "ctrl": "ctrl", "control": "ctrl", "ctl": "ctrl",
    "alt": "alt", "option": "alt", "opt": "alt",
    "shift": "shift",
    "cmd": "cmd", "command": "cmd", "win": "cmd", "windows": "cmd", "super": "cmd", "meta": "cmd",
}  # fmt: skip

_NAMED = {
    "space": "space", "spacebar": "space",
    "enter": "enter", "return": "enter",
    "tab": "tab",
    "esc": "esc", "escape": "esc",
    "backspace": "backspace",
    "delete": "delete", "del": "delete",
    "insert": "insert", "ins": "insert",
    "home": "home", "end": "end",
    "page up": "page_up", "pageup": "page_up", "pgup": "page_up",
    "page down": "page_down", "pagedown": "page_down", "pgdn": "page_down",
    "up": "up", "down": "down", "left": "left", "right": "right",
    "arrow up": "up", "arrow down": "down", "arrow left": "left", "arrow right": "right",
    "caps lock": "caps_lock", "capslock": "caps_lock",
    "num lock": "num_lock", "numlock": "num_lock",
    "scroll lock": "scroll_lock", "scrolllock": "scroll_lock",
    "pause": "pause", "break": "pause",
    "print screen": "print_screen", "printscreen": "print_screen", "prtsc": "print_screen", "prtscn": "print_screen",
    "menu": "menu", "apps": "menu", "context menu": "menu",
    "alt gr": "alt_gr", "altgr": "alt_gr",
    "media play pause": "media_play_pause", "play pause": "media_play_pause",
    "media next": "media_next", "media previous": "media_previous",
    "plus": "char:+", "minus": "char:-", "comma": "char:,", "period": "char:.", "backtick": "char:`",
}  # fmt: skip

# A spec token on the left matches any event token in its set.
_GENERIC = {
    "ctrl": {"ctrl", "ctrl_l", "ctrl_r"},
    "alt": {"alt", "alt_l", "alt_r", "alt_gr"},
    "shift": {"shift", "shift_l", "shift_r"},
    "cmd": {"cmd", "cmd_l", "cmd_r"},
    "alt_r": {"alt_r", "alt_gr"},  # Windows reports Right Alt as AltGr on many layouts
    "alt_gr": {"alt_gr", "alt_r"},
}


@dataclass(frozen=True, slots=True)
class Hotkey:
    tokens: tuple[str, ...]
    text: str

    def __str__(self) -> str:
        return self.text


def _normalise(part: str) -> str:
    part = part.strip().lower().replace("_", " ").replace("-", " ")
    return re.sub(r"\s+", " ", part)


def _parse_one(raw: str) -> str:
    if len(raw.strip()) == 1:  # a literal printable key, case-insensitive
        return "char:" + raw.strip().lower()
    part = _normalise(raw)
    if part in _NAMED:
        return _NAMED[part]
    if part in _MODIFIERS:
        return _MODIFIERS[part]
    side = re.fullmatch(r"(left|right|l|r) ?(\w+)", part)
    if side and side.group(2) in _MODIFIERS:
        return f"{_MODIFIERS[side.group(2)]}_{side.group(1)[0]}"
    side = re.fullmatch(r"(\w+) ?(left|right|l|r)", part)
    if side and side.group(1) in _MODIFIERS:
        return f"{_MODIFIERS[side.group(1)]}_{side.group(2)[0]}"
    fkey = re.fullmatch(r"f ?(\d{1,2})", part)
    if fkey and 1 <= int(fkey.group(1)) <= 24:
        return f"f{int(fkey.group(1))}"
    vk = re.fullmatch(r"vk ?:? ?(\d{1,3})", part)
    if vk:
        return f"vk:{int(vk.group(1))}"
    raise ValueError(f"unknown key name {raw.strip()!r}")


def parse_hotkey(text: str) -> Hotkey:
    """Parse ``"right ctrl"`` / ``"alt+space"`` style names. Raises ValueError."""
    if not text or not text.strip():
        raise ValueError("empty push-to-talk key")
    raw = text.strip()
    # Split on "+", but allow "+" itself as a key ("ctrl++").
    parts = re.split(r"(?<!^)\+(?!$)", raw) if raw != "+" else ["+"]
    tokens: list[str] = []
    for part in parts:
        if not part.strip():
            raise ValueError(f"malformed key combination {raw!r}")
        token = _parse_one(part)
        if token not in tokens:
            tokens.append(token)
    return Hotkey(tokens=tuple(tokens), text=raw)


def key_matches(spec_token: str, event_token: str) -> bool:
    return spec_token == event_token or event_token in _GENERIC.get(spec_token, ())


class HoldDetector:
    """Turns raw key down/up events into hotkey down/up edges.

    Auto-repeat presses are ignored; the hotkey is "down" while every token of
    the combination is held and goes "up" when any of them is released.
    Callbacks run on the caller's thread (the keyboard hook): keep them quick.
    """

    def __init__(self, hotkey: Hotkey, on_down: Callable[[], None], on_up: Callable[[], None]) -> None:
        self._hotkey = hotkey
        self._on_down = on_down
        self._on_up = on_up
        self._pressed: set[str] = set()
        self._held = False
        self._lock = threading.Lock()

    @property
    def hotkey(self) -> Hotkey:
        return self._hotkey

    @property
    def held(self) -> bool:
        return self._held

    def set_hotkey(self, hotkey: Hotkey) -> None:
        with self._lock:
            if hotkey.tokens == self._hotkey.tokens:  # same key (maybe spelled differently): keep any hold
                self._hotkey = hotkey
                return
            was_held = self._held
            self._hotkey, self._held = hotkey, False
        if was_held:
            self._on_up()

    def _combo_down(self) -> bool:
        return all(any(key_matches(t, p) for p in self._pressed) for t in self._hotkey.tokens)

    def press(self, token: str | None) -> None:
        if token is None:
            return
        with self._lock:
            self._pressed.add(token)
            fire = not self._held and self._combo_down()
            if fire:
                self._held = True
        if fire:
            self._on_down()

    def release(self, token: str | None) -> None:
        if token is None:
            return
        with self._lock:
            self._pressed.discard(token)
            fire = self._held and not self._combo_down()
            if fire:
                self._held = False
        if fire:
            self._on_up()
