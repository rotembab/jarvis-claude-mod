"""The one object the runtime talks to: opens, drives and closes the keyboard (DESIGN-KEYBOARD.md 3.8).

STUB (T5). Track T5 replaces this file; the names below are the contract. This is the only keyboard module that may
import ``protocol`` and ``overlay.base.OverlayState``, and the only module that formats nothing it caught: an exception
is never put into a log line or a reply, only its type name (F4).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..desktop.base import Display
from ..landmarks import Frame
from ..overlay.base import OverlayState
from .settings import KeyboardSettings
from .types import CloseReason

STUB_OWNER = "T5"


@dataclass(frozen=True)
class KeyboardDeps:
    data_dir: Path
    #: The runtime's desktop; ``as_key_desktop()`` decides what it can do.
    desktop: Callable[[], object | None]
    #: May be a NullOverlay; ``overlay_health()`` decides.
    overlay: Callable[[], object | None]
    displays: Callable[[], Sequence[Display]]
    #: The engine exists and the camera is open.
    ready: Callable[[], bool]
    paused: Callable[[], bool]
    #: Lock screen or UAC.
    desktop_blocked: Callable[[], bool]
    #: ``HandsSettings.overlay``.
    overlay_enabled: Callable[[], bool]
    fps: Callable[[], float]
    #: Protocol event out (``runtime._emit``).
    emit: Callable[[dict[str, Any]], None]
    #: ``runtime._show``.
    show: Callable[[OverlayState], None]
    #: ``runtime._disengage_all("keyboard")`` then ``engine.reset_tracks()``.
    pointer_off: Callable[[], None]
    #: ``engine.reset_tracks()``.
    pointer_reset: Callable[[], None]
    #: ``runtime._report(code, message)``, throttled by the runtime.
    report: Callable[[str, str], None]
    clock: Callable[[], float]


class KeyboardController:
    settings: KeyboardSettings

    def __init__(self, deps: KeyboardDeps) -> None:
        raise NotImplementedError("T5: keyboard.controller")

    @property
    def active(self) -> bool:
        """A session is open (the pointer is off)."""
        raise NotImplementedError("T5: keyboard.controller")

    @property
    def wants_two_hands(self) -> bool:
        raise NotImplementedError("T5: keyboard.controller")

    @property
    def needs_release(self) -> bool:
        """Set by an open; the runtime releases everything outside its lock, then clears it via ``take_release``."""
        raise NotImplementedError("T5: keyboard.controller")

    def take_release(self) -> bool:
        raise NotImplementedError("T5: keyboard.controller")

    def command(self, body: Mapping[str, Any]) -> dict[str, Any]:
        """The protocol ``keyboard`` command; the runtime lock is held by the caller."""
        raise NotImplementedError("T5: keyboard.controller")

    def frame(self, frame: Frame, now: float) -> None:
        """Runtime lock held, loop thread, only while active."""
        raise NotImplementedError("T5: keyboard.controller")

    def pointer_frame(self, frame: Frame) -> Frame:
        """The frame the engine gets: empty while the pointer is off or quarantined (2.11)."""
        raise NotImplementedError("T5: keyboard.controller")

    def close(self, reason: CloseReason, *, quarantine: bool = True) -> None:
        """Idempotent and re-entrant."""
        raise NotImplementedError("T5: keyboard.controller")

    def status(self) -> dict[str, Any] | None:
        """``StatusResponse.keyboard``."""
        raise NotImplementedError("T5: keyboard.controller")
