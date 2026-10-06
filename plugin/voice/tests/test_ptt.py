from __future__ import annotations

import enum

import pytest

from jarvis_voice.ptt.keys import HoldDetector, key_matches, parse_hotkey
from jarvis_voice.ptt.pynput_backend import PynputPushToTalk


@pytest.mark.parametrize(
    ("text", "tokens"),
    [
        ("right ctrl", ("ctrl_r",)),
        ("Right Ctrl", ("ctrl_r",)),
        ("rctrl", ("ctrl_r",)),
        ("ctrl_r", ("ctrl_r",)),
        ("right control", ("ctrl_r",)),
        ("left alt", ("alt_l",)),
        ("right alt", ("alt_r",)),
        ("alt gr", ("alt_gr",)),
        ("f13", ("f13",)),
        ("F24", ("f24",)),
        ("alt+space", ("alt", "space")),
        ("Ctrl + Shift + J", ("ctrl", "shift", "char:j")),
        ("win+`", ("cmd", "char:`")),
        ("caps lock", ("caps_lock",)),
        ("scroll-lock", ("scroll_lock",)),
        ("pause", ("pause",)),
        ("ctrl++", ("ctrl", "char:+")),
        ("vk:124", ("vk:124",)),
        ("cmd", ("cmd",)),
    ],
)
def test_parse_hotkey(text: str, tokens: tuple[str, ...]) -> None:
    hotkey = parse_hotkey(text)
    assert hotkey.tokens == tokens
    assert str(hotkey) == text.strip()


@pytest.mark.parametrize("text", ["", "  ", "hyper", "ctrl+", "f25", "ctrl++shift+", "+ctrl+"])
def test_parse_hotkey_rejects_garbage(text: str) -> None:
    with pytest.raises(ValueError):
        parse_hotkey(text)


def test_generic_modifiers_match_either_side() -> None:
    assert key_matches("ctrl", "ctrl_r") and key_matches("ctrl", "ctrl_l")
    assert not key_matches("ctrl_r", "ctrl_l")
    assert key_matches("alt_r", "alt_gr")  # Windows reports Right Alt as AltGr
    assert not key_matches("space", "char: ")


class Edges:
    def __init__(self) -> None:
        self.log: list[str] = []

    def down(self) -> None:
        self.log.append("down")

    def up(self) -> None:
        self.log.append("up")


def test_hold_single_key_ignores_autorepeat_and_other_keys() -> None:
    e = Edges()
    d = HoldDetector(parse_hotkey("right ctrl"), e.down, e.up)
    d.press("ctrl_l")
    d.release("ctrl_l")
    d.press("ctrl_r")
    d.press("ctrl_r")  # auto-repeat
    d.press("ctrl_r")
    d.press("char:a")
    d.release("char:a")
    assert e.log == ["down"]
    d.release("ctrl_r")
    assert e.log == ["down", "up"]


def test_hold_combo_requires_all_keys_and_releases_on_any() -> None:
    e = Edges()
    d = HoldDetector(parse_hotkey("alt+space"), e.down, e.up)
    d.press("space")
    assert e.log == []
    d.press("alt_l")
    assert e.log == ["down"]
    d.release("space")
    assert e.log == ["down", "up"]
    d.press("space")
    assert e.log == ["down", "up", "down"]
    d.release("alt_l")
    d.release("space")
    assert e.log == ["down", "up", "down", "up"]


def test_changing_hotkey_while_held_releases() -> None:
    e = Edges()
    d = HoldDetector(parse_hotkey("f13"), e.down, e.up)
    d.press("f13")
    d.set_hotkey(parse_hotkey("f14"))
    assert e.log == ["down", "up"]
    d.release("f13")
    d.press("f14")
    assert e.log == ["down", "up", "down"]


def test_unknown_tokens_are_ignored() -> None:
    e = Edges()
    d = HoldDetector(parse_hotkey("f13"), e.down, e.up)
    d.press(None)
    d.release(None)
    assert e.log == []


def test_pynput_backend_reports_unavailable_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    """On a headless box (or when the import fails) we get PttUnavailable, never a crash."""
    import builtins

    from jarvis_voice.ptt.base import PttUnavailable
    from jarvis_voice.ptt.pynput_backend import PynputPushToTalk

    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name.startswith("pynput"):
            raise ImportError("this platform is not supported")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(PttUnavailable) as info:
        PynputPushToTalk().start(parse_hotkey("f13"), lambda: None, lambda: None)
    assert info.value.hint


# --------------------------------------------------------------------------- pynput key events -> tokens


class WinKeyCode:
    """Mimics pynput's KeyCode."""

    def __init__(self, vk: int | None = None, char: str | None = None) -> None:
        self.vk, self.char = vk, char

    @classmethod
    def from_vk(cls, vk: int) -> WinKeyCode:
        return cls(vk=vk)

    @classmethod
    def from_char(cls, char: str) -> WinKeyCode:
        return cls(char=char)


class WinKey(enum.Enum):
    """pynput's Key on Windows: each value is a KeyCode carrying the virtual-key code (pynput/_win32.py)."""

    ctrl = WinKeyCode(0x11)
    ctrl_l = WinKeyCode(0xA2)
    ctrl_r = WinKeyCode(0xA3)
    f13 = WinKeyCode(0x7C)
    caps_lock = WinKeyCode(0x14)
    space = WinKeyCode(0x20)


class CanonicalListener:
    """Has pynput's Listener.canonical(), which must not be applied to push-to-talk keys."""

    def canonical(self, key: object) -> object:
        if isinstance(key, WinKeyCode) and key.char is not None:
            return WinKeyCode.from_char(key.char.lower())
        if key in (WinKey.ctrl_l, WinKey.ctrl_r):
            return WinKey.ctrl
        if isinstance(key, WinKey):
            return WinKeyCode.from_vk(key.value.vk)
        return key


def windows_like_backend() -> PynputPushToTalk:
    backend = PynputPushToTalk()
    backend._key_cls, backend._keycode_cls = WinKey, WinKeyCode
    backend._listener = CanonicalListener()  # as after start()
    return backend


@pytest.mark.parametrize(
    ("key", "token"),
    [
        (WinKey.ctrl_r, "ctrl_r"),
        (WinKey.ctrl_l, "ctrl_l"),
        (WinKey.f13, "f13"),
        (WinKey.caps_lock, "caps_lock"),
        (WinKey.space, "space"),
        (WinKeyCode.from_char("J"), "char:j"),
        (WinKeyCode.from_vk(0xE8), "vk:232"),
    ],
)
def test_key_events_keep_their_side_and_name(key: object, token: str) -> None:
    assert windows_like_backend()._token(key) == token


@pytest.mark.parametrize(
    ("setting", "key"), [("right ctrl", WinKey.ctrl_r), ("f13", WinKey.f13), ("caps lock", WinKey.caps_lock)]
)
def test_default_and_named_keys_fire_through_the_hook_callbacks(setting: str, key: WinKey) -> None:
    e = Edges()
    backend = windows_like_backend()
    backend._detector = HoldDetector(parse_hotkey(setting), e.down, e.up)
    backend._on_press(WinKey.ctrl_l)  # the other Ctrl: nothing
    backend._on_release(WinKey.ctrl_l)
    assert e.log == []
    backend._on_press(key)
    backend._on_release(key)
    assert e.log == ["down", "up"]


def test_real_pynput_keys_map_to_setting_tokens() -> None:
    """Same check with pynput's own classes and listener, where it can be imported (Windows, macOS, X11)."""
    try:
        from pynput import keyboard

        listener = keyboard.Listener()  # not started: only canonical() is reachable
    except Exception as exc:  # noqa: BLE001 - headless Linux has no backend
        pytest.skip(f"pynput is unavailable here: {exc}")
    backend = PynputPushToTalk()
    backend._key_cls, backend._keycode_cls, backend._listener = keyboard.Key, keyboard.KeyCode, listener
    assert backend._token(keyboard.Key.ctrl_r) == "ctrl_r"
    assert backend._token(keyboard.Key.f13) == "f13"
    assert backend._token(keyboard.Key.space) == "space"
    assert backend._token(keyboard.KeyCode.from_char("J")) == "char:j"


def test_same_key_respelled_keeps_the_hold() -> None:
    e = Edges()
    d = HoldDetector(parse_hotkey("right ctrl"), e.down, e.up)
    d.press("ctrl_r")
    d.set_hotkey(parse_hotkey("Right Ctrl"))  # e.g. config re-sent with the same key
    assert e.log == ["down"] and d.held and d.hotkey.text == "Right Ctrl"
    d.release("ctrl_r")
    assert e.log == ["down", "up"]
