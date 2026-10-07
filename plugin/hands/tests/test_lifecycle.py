from __future__ import annotations

import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import pytest

from jarvis_hands import platform as plat
from jarvis_hands.lifecycle import DEFAULT_INSTANCE_NAME, HeartbeatWatchdog, acquire_instance_lock


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# --------------------------------------------------------------------------- watchdog


def test_grace_period_before_first_heartbeat() -> None:
    clock, fired = Clock(), []
    dog = HeartbeatWatchdog(lambda: fired.append(1), timeout=15, grace=60, clock=clock)
    clock.now += 59
    assert not dog.check()
    clock.now += 2
    assert dog.check()
    assert fired == [1]


def test_timeout_after_heartbeats_stop() -> None:
    clock, fired = Clock(), []
    dog = HeartbeatWatchdog(lambda: fired.append(1), timeout=15, grace=60, clock=clock)
    for _ in range(10):  # a mod beating every 2 s keeps it alive indefinitely
        clock.now += 2
        dog.beat()
        assert not dog.check()
    assert dog.last_beat == clock.now
    clock.now += 14.9
    assert not dog.check()
    clock.now += 0.2
    assert dog.check()
    assert dog.check()  # stays expired ...
    assert fired == [1]  # ... but fires only once


def test_first_heartbeat_switches_from_grace_to_timeout() -> None:
    clock = Clock()
    dog = HeartbeatWatchdog(lambda: None, timeout=15, grace=60, clock=clock)
    clock.now += 50
    dog.beat()
    clock.now += 16  # 66 s after start but only 16 s after the beat
    assert dog.check()


def test_system_sleep_restarts_the_deadline() -> None:
    """Windows' monotonic clock (Python 3.12) counts sleep; waking up must not look like a dead parent."""
    clock, fired = Clock(), []
    dog = HeartbeatWatchdog(lambda: fired.append(1), timeout=15, grace=60, poll=0.5, clock=clock)
    dog.beat()
    for _ in range(4):
        clock.now += 0.5
        assert not dog.poll()
    clock.now += 3600  # the PC slept for an hour between two polls
    assert not dog.poll() and fired == []
    for _ in range(28):  # the mod's next heartbeat has a full timeout to arrive
        clock.now += 0.5
        assert not dog.poll()
    clock.now += 1.5
    assert dog.poll() and fired == [1]  # still exits when the mod really is gone


def test_suspend_before_the_first_heartbeat_restarts_the_grace() -> None:
    clock, fired = Clock(), []
    dog = HeartbeatWatchdog(lambda: fired.append(1), timeout=15, grace=60, poll=0.5, clock=clock)
    assert not dog.poll()
    clock.now += 600
    assert not dog.poll()
    clock.now += 59
    assert not dog.check()
    clock.now += 2
    assert dog.check() and fired == [1]


def test_watchdog_thread_fires() -> None:
    fired = threading.Event()
    # Wide margins: a busy CI machine can oversleep a short wait several times over.
    dog = HeartbeatWatchdog(fired.set, timeout=1.0, grace=2.0, poll=0.02).start()
    try:
        dog.beat()
        assert not fired.wait(0.3)
        assert fired.wait(5.0)
    finally:
        dog.stop()


def test_stopped_watchdog_never_fires() -> None:
    fired = threading.Event()
    dog = HeartbeatWatchdog(fired.set, timeout=0.1, grace=0.1, poll=0.02).start()
    dog.stop()
    assert not fired.wait(0.5)


# --------------------------------------------------------------------------- single instance


def test_default_instance_name() -> None:
    assert DEFAULT_INSTANCE_NAME == "JarvisHands"


def test_single_instance_lock_conflict(tmp_path: Path) -> None:
    name = f"JarvisHandsTest-{uuid.uuid4().hex[:8]}"
    first = acquire_instance_lock(name, tmp_path)
    assert first is not None
    try:
        assert acquire_instance_lock(name, tmp_path) is None
        other = acquire_instance_lock(name + "-other", tmp_path)
        assert other is not None
        other.release()
    finally:
        first.release()
    first.release()  # twice is harmless
    again = acquire_instance_lock(name, tmp_path)
    assert again is not None
    again.release()


def test_lock_is_held_across_processes(tmp_path: Path) -> None:
    name = f"JarvisHandsTest-{uuid.uuid4().hex[:8]}"
    lock = acquire_instance_lock(name, tmp_path)
    assert lock is not None
    script = (
        "import sys; from pathlib import Path\n"
        "from jarvis_hands.lifecycle import acquire_instance_lock\n"
        "sys.exit(0 if acquire_instance_lock(sys.argv[1], Path(sys.argv[2])) is None else 1)\n"
    )
    try:
        proc = subprocess.run([sys.executable, "-c", script, name, str(tmp_path)], timeout=60)
        assert proc.returncode == 0
    finally:
        lock.release()


# --------------------------------------------------------------------------- exit hooks

# Runs ``python -m jarvis_hands`` (runpy is what -m uses) with a main that
# registers atexit hooks the way the executor does, then returns an exit code.
PYTHON_M_WITH_HOOKS = """
import atexit, runpy, sys, time
import jarvis_hands.cli as cli
import jarvis_hands.lifecycle as lifecycle

def main(argv=None):
    atexit.register(sys.stderr.write, "FIRST HOOK RAN\\n")
    if sys.argv[1] == "hang":
        atexit.register(time.sleep, 3600)  # runs first (atexit is last in, first out)
    atexit.register(sys.stderr.write, "BUTTONS RELEASED\\n")
    return 7

lifecycle.EXIT_HOOKS_TIMEOUT = 0.5
cli.main = main
runpy.run_module("jarvis_hands", run_name="__main__", alter_sys=True)
raise SystemExit("not reached: __main__ exits hard")
"""


def test_python_m_runs_the_exit_hooks_before_its_hard_exit() -> None:
    # The executor's atexit hook releases any held mouse button; os._exit alone would skip it.
    proc = subprocess.run([sys.executable, "-c", PYTHON_M_WITH_HOOKS, "ok"], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 7, proc.stderr
    assert proc.stderr.index("BUTTONS RELEASED") < proc.stderr.index("FIRST HOOK RAN")
    assert "not reached" not in proc.stderr


def test_a_hanging_exit_hook_cannot_keep_the_helper_alive() -> None:
    proc = subprocess.run(
        [sys.executable, "-c", PYTHON_M_WITH_HOOKS, "hang"], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 7, proc.stderr
    assert "BUTTONS RELEASED" in proc.stderr and "still running after 0.5 s" in proc.stderr
    assert "FIRST HOOK RAN" not in proc.stderr  # queued behind the hanging one


def test_windows_uses_a_local_named_mutex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    lock = plat.make_instance_lock("JarvisHands", tmp_path)
    assert isinstance(lock, plat.NamedMutexLock)
    assert lock.mutex_name == "Local\\JarvisHands"


@pytest.mark.skipif(sys.platform == "win32", reason="flock is POSIX-only")
def test_posix_lock_file_lives_in_data_dir_run(tmp_path: Path) -> None:
    lock = acquire_instance_lock("JarvisHandsX", tmp_path)
    assert lock is not None
    try:
        path = tmp_path / "run" / "JarvisHandsX.lock"
        assert path.read_text().strip() == str(os.getpid())
    finally:
        lock.release()


@pytest.mark.skipif(sys.platform != "win32", reason="named mutex is Windows-only")
def test_named_mutex_conflict_on_windows() -> None:
    name = f"Local\\JarvisHandsTest-{uuid.uuid4().hex[:8]}"
    first, second = plat.NamedMutexLock(name), plat.NamedMutexLock(name)
    assert first.acquire()
    try:
        assert not second.acquire()
    finally:
        first.release()
    assert second.acquire()
    second.release()


# --------------------------------------------------------------------------- platform


@pytest.mark.parametrize(("sys_platform", "expected"), [("win32", "windows"), ("darwin", "macos"), ("linux", "linux")])
def test_platform_name(monkeypatch: pytest.MonkeyPatch, sys_platform: str, expected: str) -> None:
    monkeypatch.setattr(sys, "platform", sys_platform)
    assert plat.name() == expected


@pytest.mark.parametrize("sys_platform", ["win32", "darwin", "linux"])
def test_camera_hints_exist_for_every_os(monkeypatch: pytest.MonkeyPatch, sys_platform: str) -> None:
    monkeypatch.setattr(sys, "platform", sys_platform)
    hints = [plat.camera_blocked_hint(), plat.camera_in_use_hint(), plat.no_camera_hint()]
    assert all(isinstance(h, str) and h.strip() and h.isprintable() for h in hints)
    assert len(set(hints)) == 3


def test_windows_blocked_hint_names_the_privacy_page(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    hint = plat.camera_blocked_hint()
    assert "ms-settings:privacy-webcam" in hint and "Let desktop apps access your camera" in hint


def test_open_camera_settings_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os, "startfile", opened.append, raising=False)
    assert plat.open_camera_settings() is True
    assert opened == ["ms-settings:privacy-webcam"]

    def refuse(uri: str) -> None:
        raise OSError("no shell")

    monkeypatch.setattr(os, "startfile", refuse, raising=False)
    assert plat.open_camera_settings() is False


def test_open_camera_settings_on_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> None:
        calls.append(argv)

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert plat.open_camera_settings() is True
    assert calls == [["open", plat.MACOS_CAMERA_SETTINGS_URL]]


def test_open_camera_settings_elsewhere(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert plat.open_camera_settings() is False
    assert plat.camera_access_denied() is False


@pytest.mark.skipif(sys.platform == "win32", reason="would really change the error mode")
def test_suppress_crash_dialogs_is_a_no_op_off_windows() -> None:
    plat.suppress_crash_dialogs()


@pytest.mark.skipif(sys.platform != "win32", reason="reads the Windows registry")
def test_camera_access_denied_reads_the_registry() -> None:
    assert isinstance(plat.camera_access_denied(), bool)
