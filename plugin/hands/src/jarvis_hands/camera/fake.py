"""A camera without hardware: frames at a steady rate, for tests and ``run --fake``.

It keeps the real camera's contract where the runtime depends on it: ``open``
returns a ``CameraInfo`` (backend ``fake``) or raises ``CameraError``,
``read`` paces frames at ``fps`` and stamps them on the helper's clock
(``clock.now()``, read on every call, as ``CameraFrame.t`` promises), hands
out only the newest one (a slow reader sees gaps in ``seq``, as with a real
device), and ``close`` is idempotent and wakes a blocked ``read``. Frames are
black unless ``frames`` gives images to cycle through.

Error paths are scripted with ``fail_open`` (the next ``open`` raises; it is
a plain attribute, so a test can make a later reopen fail) and ``fail()``
(the next ``read`` raises, e.g. ``camera_lost`` mid-run).
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Sequence

import numpy as np

from .. import clock
from .base import CameraError, CameraErrorCode, CameraFrame, CameraInfo

log = logging.getLogger(__name__)

#: How often a waiting ``read`` checks whether it was woken.
_SLEEP_SLICE_S = 0.005


class FakeCamera:
    def __init__(
        self,
        width: int = 1280,
        height: int = 720,
        fps: float = 30.0,
        frames: Sequence[np.ndarray] | np.ndarray | None = None,
        fail_open: CameraErrorCode | CameraError | None = None,
        *,
        name: str = "fake camera",
        index: int = 0,
    ) -> None:
        if width < 1 or height < 1 or not fps > 0:
            raise ValueError("a fake camera needs a positive size and frame rate")
        self.width = int(width)
        self.height = int(height)
        self.fps = float(fps)
        self.name = name
        self.index = index
        if isinstance(frames, np.ndarray):
            frames = [frames]
        self.frames: list[np.ndarray] | None = list(frames) if frames is not None else None
        #: The next ``open`` raises this (a CameraError, or one built from an error code).
        self.fail_open = fail_open
        #: How many times the camera was opened and closed (each close of an open camera counts once).
        self.opens = 0
        self.closes = 0
        self._lock = threading.Lock()
        self._wakeup = threading.Event()
        self._info: CameraInfo | None = None
        self._t0 = 0.0
        self._last = 0
        self._failure: CameraError | None = None

    @property
    def is_open(self) -> bool:
        return self._info is not None

    @property
    def info(self) -> CameraInfo | None:
        return self._info

    def open(self) -> CameraInfo:
        with self._lock:
            failure = self.fail_open
            if failure is not None:
                if isinstance(failure, CameraError):
                    raise failure
                raise CameraError(failure, f"the fake camera refused to open ({failure})", "Scripted by a test.")
            self._wakeup.clear()
            self._failure = None
            self._t0 = clock.now()
            self._last = 0
            self._info = CameraInfo(self.name, self.index, "fake", self.width, self.height, self.fps)
            self.opens += 1
            log.debug("fake camera opened (%dx%d at %.0f fps)", self.width, self.height, self.fps)
            return self._info

    def read(self, timeout: float) -> CameraFrame | None:
        deadline = clock.now() + max(0.0, timeout)
        while True:
            with self._lock:
                if self._failure is not None:
                    raise self._failure
                if self._info is None:
                    raise CameraError("camera_lost", "the fake camera is not open")
                now = clock.now()
                # Frame k (1-based) comes off the "device" at t0 + (k - 1) / fps; only the newest is kept.
                newest = math.floor((now - self._t0) * self.fps) + 1
                if newest > self._last:
                    self._last = newest
                    return CameraFrame(newest, self._t0 + (newest - 1) / self.fps, self._image(newest))
                due = self._t0 + self._last / self.fps
            if now >= deadline:
                return None
            self._sleep(min(due, deadline) - now)

    def fail(self, code: CameraErrorCode = "camera_lost", message: str = "the fake camera was unplugged") -> None:
        """Every ``read`` from now until the camera is closed raises CameraError(``code``)."""
        with self._lock:
            self._failure = CameraError(code, message, "Scripted by a test.")
        self._wakeup.set()

    def close(self) -> None:
        with self._lock:
            if self._info is not None:
                self.closes += 1
                log.debug("fake camera closed")
            self._info = None
            self._failure = None
        self._wakeup.set()

    def _sleep(self, seconds: float) -> None:
        """Sleep, waking early on ``close`` or ``fail``.

        In short ``time.sleep`` slices rather than one ``Event.wait``: on Windows
        the latter rounds up to the 15.6 ms timer tick, which would pace a 30 fps
        fake at 21 fps, while ``time.sleep`` has a high-resolution timer there.
        """
        end = clock.now() + seconds
        while not self._wakeup.is_set():
            left = end - clock.now()
            if left <= 0:
                return
            time.sleep(min(left, _SLEEP_SLICE_S))

    def _image(self, seq: int) -> np.ndarray:
        if not self.frames:
            return np.zeros((self.height, self.width, 3), dtype=np.uint8)
        return np.ascontiguousarray(self.frames[(seq - 1) % len(self.frames)], dtype=np.uint8).copy()
