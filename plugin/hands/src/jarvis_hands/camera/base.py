"""The camera the runtime reads: always the newest frame, never a queue of stale ones.

A backend owns the device on a thread of its own and keeps only the latest
frame, so a slow tracker drops frames instead of falling behind. ``close()``
releases the device, which also turns the camera's light off; the runtime
closes the camera whenever hand control is paused.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

import numpy as np

CameraErrorCode = Literal["no_camera", "camera_blocked", "camera_in_use", "camera_lost"]


class CameraError(RuntimeError):
    """The camera cannot be used; ``code`` is the protocol's ErrorCode for it."""

    def __init__(self, code: CameraErrorCode, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.code: CameraErrorCode = code
        self.message = message
        self.hint = hint


@dataclass(frozen=True)
class CameraInfo:
    #: The device's readable name, or "camera <index>" when the name is unknown.
    name: str
    index: int
    #: "msmf", "dshow", "v4l2", "avfoundation", "any" or "fake".
    backend: str
    width: int
    height: int
    #: Measured once frames flow; the requested rate until then.
    fps: float


@dataclass(frozen=True)
class CameraFrame:
    #: 1, 2, 3 ... per open; a gap means frames were dropped.
    seq: int
    #: ``clock.now()`` (time.perf_counter) when the frame came off the device.
    t: float
    #: (height, width, 3) uint8 BGR, C-contiguous. The caller owns it.
    image: np.ndarray


class Camera(Protocol):
    def open(self) -> CameraInfo:
        """Opens the device and waits for its first frame. Raises CameraError."""
        ...

    def read(self, timeout: float) -> CameraFrame | None:
        """The newest frame after the last one returned, waiting up to ``timeout`` seconds.

        None when no new frame came in time. Raises CameraError ("camera_lost")
        once the device is gone and reopening it failed.
        """
        ...

    @property
    def info(self) -> CameraInfo | None:
        """What is open now (fps measured), or None when closed."""
        ...

    def close(self) -> None:
        """Releases the device. Idempotent; never raises."""
        ...
