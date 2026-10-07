"""The reticle: ``create_overlay`` gives the platform's windows, or ``NullOverlay`` where there are none."""

from __future__ import annotations

import sys

from .base import NullOverlay, Overlay, OverlayError, OverlayMode, OverlayState

__all__ = ["NullOverlay", "Overlay", "OverlayError", "OverlayMode", "OverlayState", "create_overlay"]


def create_overlay() -> Overlay:
    """Not yet shown: ``start()`` creates the windows."""
    if sys.platform == "win32":
        from .windows import WindowsOverlay

        return WindowsOverlay()
    return NullOverlay()
