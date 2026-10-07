"""Desktop backends: ``create_desktop`` gives this platform's; ``fake.FakeDesktop`` is the test double."""

from __future__ import annotations

import sys

from .base import Desktop, Display, InputBlocked, UnsupportedPlatform, Window, WindowState

__all__ = ["Desktop", "Display", "InputBlocked", "UnsupportedPlatform", "Window", "WindowState", "create_desktop"]


def create_desktop() -> Desktop:
    """Raises UnsupportedPlatform where hand control can't drive the desktop yet (everything but Windows)."""
    if sys.platform == "win32":
        from .windows import WindowsDesktop

        return WindowsDesktop()
    raise UnsupportedPlatform("Hand control can drive the mouse and windows on Windows only, for now.")
