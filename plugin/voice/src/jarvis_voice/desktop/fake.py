"""A desktop that records actions instead of performing them (tests and fake mode).

Nothing is opened, pressed, locked or copied: test runs and CI runners keep
their screen, keyboard and clipboard. It still matches app and window names
like the real backend, so fake runs answer the way a PC would.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from .base import (
    MEDIA_TEXT,
    MediaKey,
    Shot,
    StartApp,
    TopWindow,
    VolumeChange,
    encode_png,
    pick_app,
    pick_window,
    plan_volume,
    screenshot_name,
    volume_text,
    window_label,
    write_new_file,
)

DEFAULT_APPS: tuple[StartApp, ...] = (
    StartApp("Spotify", app_id="SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify"),
    StartApp("Visual Studio Code", app_id="Microsoft.VisualStudioCode"),
    StartApp("Google Chrome", app_id="Chrome"),
    StartApp("File Explorer", app_id="Microsoft.Windows.Explorer"),
    StartApp("Uninstall Spotify", link=r"C:\fake\Uninstall Spotify.lnk"),
)
DEFAULT_WINDOWS: tuple[TopWindow, ...] = (
    TopWindow(0x1001, "README.md - jarvis - Visual Studio Code", "Code.exe"),
    TopWindow(0x1002, "Spotify Premium", "Spotify.exe"),
)
FAKE_SIZE = (4, 3)  # width, height of the fake screen


class FakeDesktop:
    def __init__(
        self,
        *,
        apps: Sequence[StartApp] = DEFAULT_APPS,
        windows: Sequence[TopWindow] = DEFAULT_WINDOWS,
        screenshots: Path | None = None,
    ) -> None:
        #: Every action, in order: ("open_app", "Spotify"), ("media", "play_pause"), ...
        self.calls: list[tuple[Any, ...]] = []
        self.apps = list(apps)
        self.windows = list(windows)
        self.level, self.muted = 50, False
        self.clipboard: str | None = None
        #: Where screenshots are written; None records them without writing a file.
        self.screenshots = screenshots
        #: The thread prepare_thread ran on.
        self.thread: str | None = None
        #: Called with the action name before each action (tests: raise, or sleep).
        self.hook: Callable[[str], None] | None = None
        self.warmed = False
        self._lock = threading.Lock()

    def _record(self, *call: Any) -> None:
        if self.hook is not None:
            self.hook(str(call[0]))
        with self._lock:
            self.calls.append(call)

    def prepare_thread(self) -> None:
        self.thread = threading.current_thread().name

    def warm(self) -> None:
        self.warmed = True

    def open_uri(self, uri: str) -> str:
        self._record("open_uri", uri)
        return f"Opened {uri}."

    def open_folder(self, path: Path) -> str:
        self._record("open_folder", str(path))
        return f"Opened the folder {path}."

    def open_app(self, name: str, deadline: float) -> str:
        app = pick_app(name, self.apps)
        self._record("open_app", app.name)
        return f"Opened {app.name}."

    def focus(self, name: str) -> str:
        window = pick_window(name, self.windows)
        self._record("focus", window.title)
        return f"Brought {window_label(window)} to the front."

    def media(self, key: MediaKey) -> str:
        self._record("media", key)
        return MEDIA_TEXT[key]

    def volume(self, level: int | None, change: VolumeChange | None) -> str:
        self._record("volume", level, change)
        self.level, self.muted = plan_volume(self.level, self.muted, level, change)
        return volume_text(self.level, self.muted, change)

    def screenshot(self) -> Shot:
        self._record("screenshot")
        width, height = FAKE_SIZE
        name = screenshot_name(time.localtime())
        if self.screenshots is None:
            return Shot(Path("fake-screenshots") / name, width, height)
        rgb = np.zeros((height, width, 3), dtype=np.uint8)
        rgb[..., 0] = 255  # a small red screen
        return Shot(write_new_file(self.screenshots, name, encode_png(rgb)), width, height)

    def lock(self) -> str:
        self._record("lock")
        return "Locked the screen."

    def clipboard_read(self) -> str | None:
        self._record("clipboard_read")
        return self.clipboard

    def clipboard_write(self, text: str) -> str:
        self._record("clipboard_write", len(text))  # the length only: clipboards hold private things
        self.clipboard = text
        return f"Copied {len(text):,} characters to the clipboard."
