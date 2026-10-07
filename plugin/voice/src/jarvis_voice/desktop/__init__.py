"""Desktop actions: open, focus, media, volume, screenshot, lock and the clipboard.

The mod's ``mcp__jarvis__desktop`` tool sends ``desktop`` commands, which
``route_desktop`` hands to a ``DesktopService`` and every other command to the
daemon unchanged. The mod checks the user's own permission rules and asks
before anything risky; the helper keeps the actions narrow and typed: no
commands, no files opened, no typing or clicking, links on an allowlist only.

Windows has a backend (``windows.py``); other systems answer "unsupported"
and advertise no ``desktop.*`` capability. Fake mode (``fake.py``) records
actions instead of performing them, so tests never press a key, lock a
screen or touch a clipboard.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from typing import Any

from .base import ACTIONS, DesktopBackend, Failed, Refused, Unsupported
from .service import DesktopService

__all__ = [
    "ACTIONS",
    "DesktopBackend",
    "DesktopService",
    "Failed",
    "Refused",
    "Unsupported",
    "create_service",
    "desktop_capabilities",
    "route_desktop",
]

log = logging.getLogger(__name__)

CommandHandler = Callable[[str, dict[str, Any]], dict[str, Any]]


def desktop_capabilities(fake: bool) -> list[str]:
    """The hello capabilities: ``desktop.<action>`` on Windows or in fake mode (plus ``fake.desktop``), else none."""
    if fake:
        return [*(f"desktop.{action}" for action in ACTIONS), "fake.desktop"]
    if sys.platform == "win32":
        return [f"desktop.{action}" for action in ACTIONS]
    return []


def create_service(fake: bool) -> DesktopService:
    """This system's service: the fake desktop, the Windows one, or none (every action "unsupported")."""
    if fake:
        from .fake import FakeDesktop

        return DesktopService(FakeDesktop())
    backend: DesktopBackend | None = None
    if sys.platform == "win32":
        try:
            from .windows import WindowsDesktop

            backend = WindowsDesktop()
        except Exception:  # the voice still works without desktop actions
            log.exception("desktop actions are unavailable")
    return DesktopService(backend)


def route_desktop(handler: CommandHandler, service: DesktopService) -> CommandHandler:
    """The control server's handler: ``desktop`` to ``service``, everything else to ``handler`` unchanged.

    The control server has already checked the token and the command name and
    validated the body against the schema.
    """

    def route(name: str, body: dict[str, Any]) -> dict[str, Any]:
        if name == "desktop":
            return service.handle(body)
        return handler(name, body)

    return route
