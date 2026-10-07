"""``jarvis_voice home ...`` without the helper: one request, then exit.

The mod uses ``home call`` when no helper of its own is running. A command
line could be typed by anyone, Claude's own shell included, so these paths
never accept a confirmation on someone's word: ``call`` drops ``confirmed``
(only the running helper, which shares a secret with the mod, takes it), and
``do`` asks at the console itself, and only when a person is at one.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

from .. import protocol
from .service import HomeService

ServiceFactory = Callable[[Path], HomeService]

CONFIRM_NEEDS_HELPER = " Confirming it needs the Jarvis voice helper running: start it with /jarvis."


def call_once(data_dir: Path, raw: str, *, service_factory: ServiceFactory = HomeService) -> dict[str, Any]:
    """Answers one JSON ``home`` command body, as the helper would, minus confirmations."""
    try:
        body = json.loads(raw)
        protocol.validate_command("home", body)
    except (ValueError, protocol.ValidationError) as exc:
        return protocol.error_response("bad_request", f"invalid home command: {exc}")
    body.pop("confirmed", None)
    service = service_factory(data_dir)
    try:
        response = service.handle(body)
    finally:
        service.close()
    if response.get("result") == "confirm":
        response["text"] = str(response.get("text", "")) + CONFIRM_NEEDS_HELPER
    return response


def run_console(
    data_dir: Path,
    body: dict[str, Any],
    *,
    stdin: TextIO,
    ask: Callable[[str], str] = input,
    service_factory: ServiceFactory = HomeService,
) -> dict[str, Any]:
    """``home info|list|status|do`` typed at a console. A command that needs
    confirming is asked about here, if stdin is a terminal, and refused otherwise."""
    body = {k: v for k, v in body.items() if k != "confirmed"}
    service = service_factory(data_dir)
    try:
        response = service.handle(body)
        if response.get("result") != "confirm":
            return response
        if not stdin.isatty():
            return {**response, "result": "failed", "text": f"{response['text']} Run it in a console to confirm it."}
        answer = ask(f"{response.get('prompt', 'Go ahead?')} [y/N]: ").strip().lower()
        if answer not in ("y", "yes"):
            return {**response, "result": "failed", "code": "refused", "text": "Not done."}
        # The yes is for the device the prompt named: it goes again by that device's id, not the words typed.
        device = response.get("device")
        if isinstance(device, dict) and isinstance(device.get("id"), str) and device["id"]:
            body = {**body, "device": device["id"]}
        return service.handle({**body, "confirmed": True})
    finally:
        service.close()
