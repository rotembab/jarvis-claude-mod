"""The driver interface, the context drivers get, and the registry that loads them lazily.

A driver controls one family of devices (Apple TV, Sony Bravia, Plex, Tuya, Home
Assistant). The service calls it from a worker thread with the device's lock
held, so a driver call may block, but must finish within the time the
service allows (``DriverContext.call_timeout``); async libraries run on the
shared ``AsyncRunner``.

Each driver module also offers a setup step for the wizard
(``wizard(ui, ctx)``), which runs in its own console window: that is where
PINs, keys and tokens are typed, so they never pass through Claude Code.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Protocol

from .model import CommandSpec, DeviceRecord, Outcome, Value
from .runner import AsyncRunner
from .store import HomeStore

log = logging.getLogger(__name__)

# Third-party loggers that print keys, tokens or credentials at DEBUG (pyatv,
# tinytuya) or whole response bodies at ERROR (tuya_sharing). Kept quiet
# whatever the helper's own log level is.
QUIET_LOGGERS = {
    "pyatv": logging.WARNING,
    "tinytuya": logging.WARNING,
    "tuya_sharing": logging.CRITICAL,
    "tuya_iot": logging.CRITICAL,
    "zeroconf": logging.WARNING,
    "aiohttp": logging.WARNING,
    "paho": logging.WARNING,
    "srptools": logging.WARNING,
    "websockets": logging.WARNING,
    # The Tuya sharing SDK's token refresh puts the refresh token in a URL path, which urllib3 logs at DEBUG.
    "urllib3": logging.WARNING,
}


def quiet_device_loggers() -> None:
    for name, level in QUIET_LOGGERS.items():
        logging.getLogger(name).setLevel(level)


@dataclass
class DriverContext:
    """What a driver may use: the store (devices and credentials), the async runner, the data folder."""

    store: HomeStore
    data_dir: Path
    # How long one driver call may take before the service answers "timeout".
    call_timeout: float = 20.0
    _runner: AsyncRunner | None = field(default=None, repr=False)

    def runner(self) -> AsyncRunner:
        if self._runner is None:
            self._runner = AsyncRunner()
        return self._runner

    def close(self) -> None:
        if self._runner is not None:
            self._runner.stop()
            self._runner = None


class Driver:
    """Base class. Subclasses set ``name`` and ``label`` and implement ``commands`` and ``run``."""

    name: ClassVar[str] = ""
    label: ClassVar[str] = ""

    def __init__(self, ctx: DriverContext) -> None:
        self.ctx = ctx

    def commands(self, device: DeviceRecord) -> list[CommandSpec]:
        """What this device can do. Called often: answer from saved data, never the network."""
        raise NotImplementedError

    def run(self, device: DeviceRecord, command: str, value: Value) -> Outcome:
        """Performs ``command`` (one of ``commands(device)``, its value already checked)."""
        raise NotImplementedError

    def status(self, device: DeviceRecord) -> Outcome:
        """A short spoken description of the device's state."""
        return Outcome.fail("unsupported", f"I can't read the state of the {device.name}.")

    def lock_key(self, device: DeviceRecord) -> str:
        """Commands with the same key never run at once (a Tuya hub and its children share one)."""
        return device.id

    def hub_devices(self) -> list[DeviceRecord]:
        """Devices a hub brings with it (Home Assistant's entities); plain drivers have none."""
        return []

    def describe(self) -> str | None:
        """One line for ``/jarvis home``: what this driver has set up, or what is wrong."""
        return None

    def close(self) -> None:
        """Releases connections; the service calls it at shutdown and on reload."""


class Prompter(Protocol):
    """The wizard's console (a scripted fake in tests)."""

    def say(self, text: str) -> None: ...

    def ask(self, question: str, default: str | None = None) -> str:
        """A line of text; the default when the answer is empty. Raises EOFError when the user quits."""
        ...

    def ask_secret(self, question: str) -> str:
        """A line typed without echo (a PIN, a key, a token)."""
        ...

    def choose(self, question: str, options: list[str]) -> int | None:
        """The index of the chosen option, or None to go back."""
        ...

    def confirm(self, question: str, default: bool = True) -> bool: ...

    def show_qr(self, data: str, caption: str) -> None:
        """Shows a QR code to scan with a phone (in the console, and as an image)."""
        ...


# --------------------------------------------------------------------------- registry

# Driver classes by name, imported only when a device of theirs is used.
DRIVERS: dict[str, str] = {
    "appletv": "jarvis_voice.home.appletv:AppleTvDriver",
    "bravia": "jarvis_voice.home.bravia:BraviaDriver",
    "plex": "jarvis_voice.home.plex:PlexDriver",
    "tuya": "jarvis_voice.home.tuya:TuyaDriver",
    "homeassistant": "jarvis_voice.home.homeassistant:HomeAssistantDriver",
}

# The wizard's setup steps, in menu order: (driver, menu label, "module:function").
WIZARD_STEPS: list[tuple[str, str, str]] = [
    ("appletv", "Add an Apple TV", "jarvis_voice.home.appletv:wizard"),
    ("bravia", "Add a Sony Bravia TV", "jarvis_voice.home.bravia:wizard"),
    ("plex", "Connect Plex", "jarvis_voice.home.plex:wizard"),
    ("tuya", "Link Tuya devices (Tuya Smart or Smart Life)", "jarvis_voice.home.tuya:wizard"),
    ("homeassistant", "Connect Home Assistant", "jarvis_voice.home.homeassistant:wizard"),
]

# The driver's display name, for messages when it cannot even be imported.
LABELS = {
    "appletv": "Apple TV",
    "bravia": "Sony Bravia",
    "plex": "Plex",
    "tuya": "Tuya",
    "homeassistant": "Home Assistant",
}


def load_object(path: str) -> Any:
    """Imports ``"package.module:attribute"``."""
    module_name, _, attribute = path.partition(":")
    module = importlib.import_module(module_name)
    return getattr(module, attribute)
