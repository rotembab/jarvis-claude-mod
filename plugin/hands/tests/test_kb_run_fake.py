"""The air keyboard end to end on the fakes (DESIGN-KEYBOARD.md K12, K46) and in a real ``run --fake`` process.

K12 and K46 run the real ``HandsRuntime`` on its real threads, with a fake camera, tracker, desktop and overlay, inside
the test's process: ``--fake-script`` can only carry poses, and the keyboard needs a typist's fingers, so the frames
are ``synth.Typist``'s, handed to the loop one at a time by the ``Lockstep`` harness of ``test_kb_runtime`` (no sleeps,
a clock the test moves). The last test goes through the CLI, the HTTP control server and the stdout events, with the
scripted palm only: what it shows is that the keyboard is wired into the process the mod actually starts.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import test_kb_runtime
from test_cli import TIMEOUT, command, helper_env, schema_validator
from test_kb_runtime import Lockstep
from test_run_fake import SCRIPT, Lines

HELLO = "hello"
#: The harness' fixture, under its own name here: pytest finds a fixture by the name it has in the module.
lockstep = test_kb_runtime.lockstep


def test_k12_pinch_and_direct_warm_up_and_type_hello(
    lockstep: Callable[..., Lockstep], caplog: pytest.LogCaptureFixture
) -> None:
    item = lockstep().start()
    with caplog.at_level(logging.DEBUG):
        typist = item.armed("direct")
        assert item.desktop.typed_text == ""  # the warm-up pinches send nothing (2.5)
        item.feed(typist.type(HELLO))
        item.feed(typist.hover(1.0))
    assert item.desktop.typed_text == HELLO and [kind for kind, _ in item.desktop.key_calls] == ["char"] * len(HELLO)
    assert item.keyboard["state"] == "open" and item.writer.closed() == [] and item.writer.errors() == []
    assert item.desktop.calls_named("move_cursor") == []  # the pointer was off for all of it
    # What was typed reaches no event, no status and no log line (SR13).
    assert HELLO not in json.dumps(item.writer.snapshot()) + json.dumps(item.status()) + caplog.text
    assert item.kb("stop") == {"ok": True}
    assert item.desktop.typed_text == HELLO and item.writer.closed() == ["command"]


def test_k46_pinch_and_review_compose_hello_then_three_inserts_type_it_and_three_sends_press_enter(
    lockstep: Callable[..., Lockstep], caplog: pytest.LogCaptureFixture
) -> None:
    item = lockstep().start()
    with caplog.at_level(logging.DEBUG):
        typist = item.armed("review")
        item.feed(typist.type(HELLO))
        item.feed(typist.hover(0.5))
        assert item.desktop.typed_text == "" and item.keyboard["review"] == {"state": "composing", "chars": 5}
        insert, send = typist.layout.find(kind="insert"), typist.layout.find(kind="enter")
        item.feed(typist.tap_n(insert, 2, 0.5))
        item.feed(typist.hover(0.5))
        assert item.desktop.typed_text == ""  # two taps are not enough, and a third later still counts
        item.feed(typist.tap_n(insert, 1, 0.5))
        item.feed(typist.hover(0.5))
        assert item.desktop.typed_text == HELLO and item.keyboard["review"] == {"state": "composing", "chars": 0}
        item.feed(typist.tap_n(send, 3, 0.5))
        item.feed(typist.hover(0.5))
    assert item.desktop.typed_text == HELLO + "\n"
    assert item.writer.closed() == [] and item.writer.errors() == []
    assert HELLO not in json.dumps(item.writer.snapshot()) + json.dumps(item.status()) + caplog.text


def test_run_fake_takes_the_keyboard_commands_and_emits_its_events(tmp_path: Path) -> None:
    script = tmp_path / "frames.jsonl"
    script.write_text(SCRIPT, encoding="utf-8")
    token = "run-fake-keyboard-token-0123456789"
    argv = [sys.executable, "-m", "jarvis_hands", "run", "--fake", "--data-dir", str(tmp_path)]
    argv += ["--fake-script", str(script), "--instance-name", f"JarvisHandsTest-{uuid.uuid4().hex[:8]}"]
    proc = subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=helper_env(JARVIS_TOKEN=token)
    )
    errors: list[str] = []
    assert proc.stderr is not None
    stderr = proc.stderr
    threading.Thread(target=lambda: errors.extend(stderr), daemon=True).start()
    validate = schema_validator()

    def keyboard(body: dict[str, Any]) -> dict[str, Any]:
        status, payload = command(port, "keyboard", body, token=token)
        assert status == 200
        validate(payload, "CommandResponse")
        return payload

    def block() -> dict[str, Any]:
        _, payload = command(port, "status", token=token)
        validate(payload, "StatusResponse")
        found: dict[str, Any] = payload["keyboard"]
        return found

    try:
        lines = Lines(proc)
        hello = lines.next()
        assert hello is not None and "keyboard" in hello["capabilities"]
        port = hello["port"]
        lines.until(lambda e: e["type"] == "ready")

        assert block() == {"enabled": False, "state": "closed", "practiced": False}
        refusal = keyboard({"action": "start"})
        assert refusal["ok"] is False and refusal["error"]["code"] == "bad_request"
        assert keyboard(
            {"action": "configure", "settings": {"enabled": True, "press": "pinch", "commit": "direct"}}
        ) == {"ok": True}
        practice_first = keyboard({"action": "start"})  # direct needs a practice that has not been done here
        assert practice_first["ok"] is False and practice_first["error"]["message"].startswith("Practice first")
        assert keyboard({"action": "configure", "settings": {"commit": "review"}}) == {"ok": True}
        assert keyboard({"action": "start"}) == {"ok": True}
        opened = lines.until(lambda e: e["type"] == "keyboard" and e.get("phase") == "warmup")
        assert (opened["state"], opened["press"], opened["commit"]) == ("open", "pinch", "review")
        assert block()["state"] == "open"
        assert keyboard({"action": "stop"}) == {"ok": True}
        closed = lines.until(lambda e: e["type"] == "keyboard" and e["state"] == "closed")
        assert closed["reason"] == "command" and closed["discarded"] == 0
        assert block()["state"] == "closed"
        assert command(port, "shutdown", token=token) == (200, {"ok": True})
        proc.wait(timeout=TIMEOUT)
        lines.rest()
    finally:
        proc.kill()
        proc.wait(timeout=TIMEOUT)
    log = "".join(errors)
    assert proc.returncode == 0, log
    for event in lines.seen:
        validate(event, "Event")
    assert not [e for e in lines.seen if e["type"] == "error"]
    assert "keyboard open (live, pinch, review, en)" in log and token not in log
