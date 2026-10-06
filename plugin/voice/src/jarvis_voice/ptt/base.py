"""Push-to-talk backend interface."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from .keys import Hotkey


class PttUnavailable(Exception):
    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


class PushToTalk(Protocol):
    """Global hold-to-talk: calls ``on_down``/``on_up`` from its own thread, even without focus."""

    def start(self, hotkey: Hotkey, on_down: Callable[[], None], on_up: Callable[[], None]) -> None:
        """Arm the hook. Raises PttUnavailable."""
        ...

    def set_hotkey(self, hotkey: Hotkey) -> None: ...

    def stop(self) -> None: ...
