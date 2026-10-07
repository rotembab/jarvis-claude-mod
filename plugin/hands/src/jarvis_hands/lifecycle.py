"""Process lifecycle: heartbeat watchdog, the single-instance lock and the exit hooks.

A hand helper outliving its mod would keep the webcam on and could keep moving
the mouse, so it leaves on its own when the heartbeats stop, exactly as the
voice helper does.
"""

from __future__ import annotations

import atexit
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path

from . import platform as plat

log = logging.getLogger(__name__)

DEFAULT_INSTANCE_NAME = "JarvisHands"

#: How long ``python -m jarvis_hands`` gives the atexit hooks before its hard
#: exit; well inside the mod's three-second wait after ``shutdown``.
EXIT_HOOKS_TIMEOUT = 2.0


class HeartbeatWatchdog:
    """Calls ``on_expire`` once when the mod stops sending heartbeats.

    Before the first heartbeat a longer ``grace`` applies (the mod may still be
    loading); after it, ``timeout`` seconds without a beat means the parent is
    gone (crash, hot reload, orphaned venv redirector grandchild).

    A poll that comes much later than scheduled means the machine slept (on
    Windows, Python 3.12's ``time.monotonic`` keeps counting through sleep and
    hibernation). The deadline then restarts, so the mod gets a full timeout
    after resume to send its next heartbeat.
    """

    def __init__(
        self,
        on_expire: Callable[[], None],
        *,
        timeout: float = 15.0,
        grace: float = 60.0,
        poll: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._on_expire = on_expire
        self.timeout = timeout
        self.grace = grace
        self._poll = poll
        self._clock = clock
        self._lock = threading.Lock()
        self._started_at = clock()
        self._last_beat: float | None = None
        self._expired = False
        self._last_poll: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def beat(self) -> None:
        with self._lock:
            self._last_beat = self._clock()

    @property
    def last_beat(self) -> float | None:
        return self._last_beat

    def check(self, now: float | None = None) -> bool:
        """Return True (and fire ``on_expire`` the first time) once the deadline passed."""
        now = self._clock() if now is None else now
        with self._lock:
            if self._expired:
                return True
            if self._last_beat is None:
                expired = now - self._started_at > self.grace
            else:
                expired = now - self._last_beat > self.timeout
            if not expired:
                return False
            self._expired = True
            waited = now - (self._last_beat if self._last_beat is not None else self._started_at)
        log.warning("no heartbeat for %.1fs; shutting down", waited)
        self._on_expire()
        return True

    def start(self) -> HeartbeatWatchdog:
        with self._lock:
            self._started_at = self._clock()
        self._thread = threading.Thread(target=self._run, name="heartbeat-watchdog", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def poll(self, now: float | None = None) -> bool:
        """One watchdog tick: forgive a suspend gap, then ``check``."""
        now = self._clock() if now is None else now
        with self._lock:
            gap = None if self._last_poll is None else now - self._last_poll
            self._last_poll = now
            if gap is not None and gap > max(self.timeout / 2, 10 * self._poll) and not self._expired:
                log.info("watchdog slept %.0fs (system suspend?); restarting the heartbeat deadline", gap)
                if self._last_beat is None:
                    self._started_at = now
                else:
                    self._last_beat = now
        return self.check(now)

    def _run(self) -> None:
        self.poll()
        while not self._stop.wait(self._poll):
            if self.poll():
                return


def acquire_instance_lock(instance_name: str, data_dir: Path) -> plat.InstanceLock | None:
    """Take the per-user single-instance lock; ``None`` if another helper holds it."""
    lock = plat.make_instance_lock(instance_name, data_dir / "run")
    return lock if lock.acquire() else None


def run_exit_hooks(timeout: float) -> bool:
    """Run the atexit hooks now, for at most ``timeout`` seconds; True when they all finished.

    ``python -m jarvis_hands`` ends with ``os._exit``, which skips atexit, and
    the executor's atexit hook (release any held mouse button) is the spec's
    last guard against a stuck drag. So the hooks run here first, on a daemon
    thread: one that hangs (a native library joining its threads) must not keep
    the helper, and the webcam, alive past the timeout.
    """
    run = getattr(atexit, "_run_exitfuncs", None)  # CPython's; each hook's error is reported, not raised
    if run is None:
        return True
    thread = threading.Thread(target=run, name="jarvis-hands-exit-hooks", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        log.warning("exit hooks still running after %.1f s; exiting anyway", timeout)
        return False
    return True
