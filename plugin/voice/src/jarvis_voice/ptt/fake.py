"""Scriptable push-to-talk for tests and fake mode (no global hooks)."""

from __future__ import annotations

from collections.abc import Callable

from .base import PttUnavailable
from .keys import HoldDetector, Hotkey


class FakePushToTalk:
    def __init__(self, *, unavailable: str | None = None) -> None:
        self.unavailable = unavailable
        self.detector: HoldDetector | None = None
        self.started = False

    def start(self, hotkey: Hotkey, on_down: Callable[[], None], on_up: Callable[[], None]) -> None:
        if self.unavailable:
            raise PttUnavailable(self.unavailable, "use /jarvis talk")
        self.detector = HoldDetector(hotkey, on_down, on_up)
        self.started = True

    def set_hotkey(self, hotkey: Hotkey) -> None:
        if self.detector is not None:
            self.detector.set_hotkey(hotkey)

    def stop(self) -> None:
        self.started = False

    # Test drivers: feed key tokens like the real hook would.
    def key_down(self, token: str) -> None:
        assert self.detector is not None
        self.detector.press(token)

    def key_up(self, token: str) -> None:
        assert self.detector is not None
        self.detector.release(token)

    def press(self) -> None:
        assert self.detector is not None
        for token in self.detector.hotkey.tokens:
            self.detector.press(token)

    def release(self) -> None:
        assert self.detector is not None
        for token in self.detector.hotkey.tokens:
            self.detector.release(token)
