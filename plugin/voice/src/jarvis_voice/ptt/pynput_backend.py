"""Global push-to-talk with pynput (Windows low-level hook, macOS event tap, X11)."""

from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Callable
from typing import Any

from .. import platform as plat
from .base import PttUnavailable
from .keys import HoldDetector, Hotkey

log = logging.getLogger(__name__)


class PynputPushToTalk:
    def __init__(self, ready_timeout: float = 3.0) -> None:
        self._ready_timeout = ready_timeout
        self._listener: Any = None
        self._detector: HoldDetector | None = None
        self._key_cls: Any = None
        self._keycode_cls: Any = None

    def start(self, hotkey: Hotkey, on_down: Callable[[], None], on_up: Callable[[], None]) -> None:
        hint = plat.current().ptt_hint()
        try:
            from pynput import keyboard
        except Exception as exc:  # ImportError on headless Linux, etc.
            raise PttUnavailable(f"Global keyboard hooks are unavailable: {exc}", hint) from exc

        if sys.platform == "darwin" and not getattr(keyboard.Listener, "IS_TRUSTED", True):
            raise PttUnavailable("macOS has not granted Input Monitoring/Accessibility to this app.", hint)

        self._key_cls, self._keycode_cls = keyboard.Key, keyboard.KeyCode
        self._detector = HoldDetector(hotkey, on_down, on_up)
        try:
            listener = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
            listener.start()
        except Exception as exc:
            raise PttUnavailable(f"Could not start the keyboard hook: {exc}", hint) from exc

        # listener.wait() blocks forever if the hook thread dies before it is
        # ready, so wait from a helper thread with a deadline.
        waiter = threading.Thread(target=listener.wait, name="ptt-wait", daemon=True)
        waiter.start()
        waiter.join(self._ready_timeout)
        if waiter.is_alive() or not listener.is_alive():
            listener.stop()
            raise PttUnavailable("The keyboard hook did not start.", hint)
        self._listener = listener
        log.info("push-to-talk armed on %r", hotkey.text)

    def set_hotkey(self, hotkey: Hotkey) -> None:
        if self._detector is not None:
            self._detector.set_hotkey(hotkey)
            log.info("push-to-talk key is now %r", hotkey.text)

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None

    # -- hook callbacks: must return quickly (Windows drops slow low-level hooks)

    def _token(self, key: Any) -> str | None:
        # Not listener.canonical(): it folds ctrl_r into ctrl and turns every
        # other named key (f13, space, caps_lock) into a bare vk KeyCode, so
        # 'right ctrl' and friends would never match. Lowercasing characters is
        # the only part of it we want.
        if key is None:
            return None
        if isinstance(key, self._key_cls):
            return str(key.name)
        if isinstance(key, self._keycode_cls):
            if key.char:
                return "char:" + key.char.lower()
            if key.vk is not None:
                return f"vk:{key.vk}"
        return None

    def _on_press(self, key: Any) -> None:
        try:
            if self._detector is not None:
                self._detector.press(self._token(key))
        except Exception:
            log.exception("push-to-talk press handler failed")

    def _on_release(self, key: Any) -> None:
        try:
            if self._detector is not None:
                self._detector.release(self._token(key))
        except Exception:
            log.exception("push-to-talk release handler failed")
