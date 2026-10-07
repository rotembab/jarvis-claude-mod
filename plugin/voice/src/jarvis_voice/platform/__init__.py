"""OS-specific bits behind one small interface.

Everything that differs between Windows, macOS and Linux (single-instance
lock, microphone privacy hints, opening the OS settings page, preferred audio
host API) is reached through ``current()``. Add a module, not ``if`` chains.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType
from typing import Protocol

from ..protocol import PlatformName


class InstanceLock(Protocol):
    def acquire(self) -> bool:
        """Take the lock. False means another instance holds it."""
        ...

    def release(self) -> None: ...


def name() -> PlatformName:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def current() -> ModuleType:
    """The platform module: ``windows``, ``macos`` or ``linux``.

    Each exposes: ``make_instance_lock(name, lock_dir)``, ``mic_blocked_hint()``,
    ``digital_silence_hint()`` (blocked or merely muted, where the OS can tell),
    ``mic_in_use_hint()``, ``no_input_device_hint()``, ``no_output_device_hint()``,
    ``ptt_hint()``, ``open_mic_settings()``, ``suppress_crash_dialogs()`` and
    ``PREFERRED_HOSTAPI``.
    """
    return importlib.import_module(f"{__name__}.{name()}")


def make_instance_lock(instance_name: str, lock_dir: Path) -> InstanceLock:
    lock: InstanceLock = current().make_instance_lock(instance_name, lock_dir)
    return lock
