from __future__ import annotations

import sys
import threading
import uuid
from pathlib import Path

import pytest

from jarvis_voice import platform as plat
from jarvis_voice.lifecycle import HeartbeatWatchdog, acquire_instance_lock


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


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
    dog = HeartbeatWatchdog(fired.set, timeout=0.2, grace=0.3, poll=0.02).start()
    try:
        dog.beat()
        assert not fired.wait(0.1)
        assert fired.wait(2.0)
    finally:
        dog.stop()


def test_single_instance_lock_conflict(tmp_path: Path) -> None:
    name = f"JarvisVoiceTest-{uuid.uuid4().hex[:8]}"
    first = acquire_instance_lock(name, tmp_path)
    assert first is not None
    try:
        assert acquire_instance_lock(name, tmp_path) is None
        other = acquire_instance_lock(name + "-other", tmp_path)
        assert other is not None
        other.release()
    finally:
        first.release()
    again = acquire_instance_lock(name, tmp_path)
    assert again is not None
    again.release()


@pytest.mark.skipif(sys.platform != "win32", reason="named mutex is Windows-only")
def test_windows_uses_local_named_mutex(tmp_path: Path) -> None:
    lock = plat.make_instance_lock("JarvisVoice", tmp_path)
    assert lock.mutex_name == "Local\\JarvisVoice"  # type: ignore[attr-defined]


@pytest.mark.skipif(sys.platform == "win32", reason="flock is POSIX-only")
def test_posix_lock_file_lives_in_data_dir(tmp_path: Path) -> None:
    lock = acquire_instance_lock("JarvisVoiceX", tmp_path)
    assert lock is not None
    try:
        path = tmp_path / "run" / "JarvisVoiceX.lock"
        assert path.read_text().strip().isdigit()
    finally:
        lock.release()
