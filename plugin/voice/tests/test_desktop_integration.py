"""Desktop actions through the real control server: ``python -m jarvis_voice run`` in fake mode, driven like the mod.

Fake mode records the actions: nothing is opened, pressed, locked or copied.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from test_integration import Helper

from jarvis_voice.desktop.base import ACTIONS

from fish_fake import FakeFishServer


@pytest.fixture
def fish() -> Iterator[FakeFishServer]:
    with FakeFishServer(api_key="fake-key") as server:
        yield server


def start(tmp_path: Path, fish: FakeFishServer, *extra: str) -> Helper:
    helper = Helper(tmp_path, fish.url, extra_args=list(extra))
    hello = helper.next_event(30)
    assert hello["type"] == "hello", hello
    helper.port = hello["port"]
    return helper


def test_desktop_actions_through_the_control_server(
    tmp_path: Path, fish: FakeFishServer, reference_validator: Any
) -> None:
    helper = start(tmp_path, fish, "--fake-desktop")
    try:
        capabilities = helper.events[0]["capabilities"]
        assert [c for c in capabilities if c.startswith("desktop.")] == [f"desktop.{a}" for a in ACTIONS]
        assert "fake.desktop" in capabilities

        def desktop(body: dict[str, Any]) -> dict[str, Any]:
            status, payload = helper.post("desktop", body)
            assert status == 200, payload
            reference_validator(payload, "CommandResponse")
            assert payload["ok"] is True
            return dict(payload["desktop"])

        # The done-criterion path: open Spotify, open a playlist, press play.
        assert desktop({"action": "open", "target": "Spotify"}) == {"result": "done", "text": "Opened Spotify."}
        playlist = "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M"
        assert desktop({"action": "open", "target": playlist}) == {"result": "done", "text": f"Opened {playlist}."}
        assert desktop({"action": "media", "key": "play_pause"}) == {
            "result": "done",
            "text": "Pressed the play/pause media key.",
        }
        assert desktop({"action": "volume", "level": 30}) == {"result": "done", "text": "Volume is 30%."}
        assert desktop({"action": "focus", "target": "code"})["result"] == "done"
        assert desktop({"action": "lock"}) == {"result": "done", "text": "Locked the screen."}
        assert desktop({"action": "clipboard_write", "text": "a private note"})["result"] == "done"
        assert desktop({"action": "clipboard_read"})["clipboard"] == "a private note"
        shot = desktop({"action": "screenshot"})
        assert shot["result"] == "done" and shot["path"].endswith(".png")

        # Jarvis's own limits answer as results...
        refused = desktop({"action": "open", "target": "file:///C:/Windows/System32/calc.exe"})
        assert refused["result"] == "refused"
        assert desktop({"action": "open", "target": "Uninstall Spotify"})["result"] == "failed"
        # ...and the schema keeps everything else from the desktop altogether.
        assert helper.post("desktop", {"action": "type", "text": "format c:"})[0] == 400
        assert helper.post("desktop", {"action": "volume", "level": 101})[0] == 400
        assert helper.post("desktop", {"action": "lock", "command": "shutdown"})[0] == 400
        assert helper.post("desktop", {"action": "lock"}, token="nope")[0] == 401
        assert helper.post("desktop", {"action": "lock"}, headers={"Origin": "https://evil.example"})[0] == 403

        # Every other command still reaches the daemon.
        assert helper.post("heartbeat") == (200, {"ok": True})
        assert helper.post("status")[1]["ok"] is True
        assert helper.post("shutdown") == (200, {"ok": True})
        assert helper.proc.wait(10) == 0
        log = (tmp_path / "logs" / "voice.log").read_text(encoding="utf-8")
        assert "desktop lock: done" in log and "a private note" not in log
    finally:
        helper.stop()


def test_fake_audio_alone_fakes_the_desktop_too(tmp_path: Path, fish: FakeFishServer) -> None:
    helper = start(tmp_path, fish)  # test runs never press keys or lock a CI runner
    try:
        assert "fake.desktop" in helper.events[0]["capabilities"]
        status, payload = helper.post("desktop", {"action": "lock"})
        assert (status, payload) == (200, {"ok": True, "desktop": {"result": "done", "text": "Locked the screen."}})
    finally:
        helper.stop()
