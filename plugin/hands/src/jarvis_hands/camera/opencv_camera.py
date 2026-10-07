"""The webcam through OpenCV: Media Foundation first on Windows, DirectShow as the fallback.

Why it is built this way (OpenCV 5.0's backends, read from their source):

- One reader thread owns the ``VideoCapture``: it opens, reads and releases it
  (DirectShow's COM state belongs to the thread that made it). It keeps only
  the newest frame. Neither Windows backend honours ``CAP_PROP_BUFFERSIZE``:
  MSMF queues up to three frames and hands out the oldest, DirectShow drops new
  frames until the last one is read, so reading continuously is the only way
  to keep the tracker on fresh pictures.
- MSMF takes its settings as constructor parameters, and one parameter it
  cannot apply fails the whole open, so the list holds only size, rate and
  ``VIDEO_ACCELERATION_NONE`` (no D3D11 device for a webcam): never
  ``FOURCC`` or ``BUFFERSIZE``. It picks the native format nearest the
  request, which is how 1280 x 720 at 30 fps lands on a webcam's MJPG mode.
  DirectShow restarts the device on each ``set`` and loses the FOURCC when the
  rate changes, so the order there is rate, width, height, then MJPG last.
- DirectShow is also tried when MSMF opens the camera but sends no picture:
  some UVC webcams fail every MSMF grab (format negotiation, MJPG decoding)
  and stream fine on DirectShow. A reopen tries the backend that last
  streamed first, so such a webcam does not wait out MSMF again.
- The two backends count devices in different lists, so the camera is looked
  up by device before DirectShow opens it: DirectShow walks the monikers of
  CLSID_VideoInputDeviceCategory, which also holds filter-based virtual
  cameras (OBS Virtual Camera and the like) that MFEnumDeviceSources does not
  list, so the same number can be another device. Media Foundation's symbolic
  link and DirectShow's DevicePath name the same device interface, which is
  what they are matched on (a friendly name only one of them has will do);
  when neither tells them apart, DirectShow is skipped rather than opening
  something else and calling it the user's camera.
- ``isOpened()`` stays True after an unplug on both backends, and an MSMF read
  on a busy or vanished device blocks up to 10 s, so health comes from reads
  alone: about 2 s without a frame releases and reopens the device with
  backoff, and when that keeps failing ``read()`` raises ``camera_lost``.
  Every later read raises too, and so does a read that was waiting when the
  reader gave up: a read that returned None would look like a slow camera and
  the caller would wait for frames that are not coming.
- Windows privacy settings let the camera be listed but not opened; another
  app holding it lets it open but sends no frames on any backend. That is how
  ``camera_blocked`` and ``camera_in_use`` are told apart.
- Frames are stamped with ``clock.now()`` (``perf_counter``), the clock the
  engine's filters run on; deadlines inside this module use ``monotonic``.
- ``list_mf_devices`` asks Media Foundation directly (ctypes, no pywin32),
  the same enumeration OpenCV's MSMF backend uses, so its indices are
  CAP_MSMF's; ``list_dshow_devices`` walks DirectShow's own device monikers
  the way OpenCV's ``videoInput::getDevice`` does. The camera is the device
  Media Foundation listed at that number; a reopen keeps matching that
  device (or the one now at that number) against DirectShow's list, so an
  unplugged webcam is never replaced by the filter camera that moved up to
  its DirectShow number. DirectShow is opened by its own number only while
  Media Foundation has never answered (a Windows N install has none); once
  it has, a camera it does not list is not opened on DirectShow at all, and
  neither is one DirectShow's list does not tell apart.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import cv2
import numpy as np

from .. import platform as plat
from ..clock import now as clock_now
from .base import CameraError, CameraFrame, CameraInfo

log = logging.getLogger(__name__)

# Read by the MSMF backend when it builds its first capture (not at import):
# hardware transforms slow the camera's open down for nothing a webcam needs.
os.environ.setdefault("OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS", "0")
try:
    # OpenCV's own warnings (MSMF HRESULTs on every failed grab) would flood the log.
    cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
except AttributeError:  # OpenCV 4 had cv2.setLogLevel instead; OpenCV 5 is pinned
    pass

#: Our backend names (``CameraInfo.backend``) and OpenCV's API ids.
BACKENDS: dict[str, int] = {"msmf": cv2.CAP_MSMF, "dshow": cv2.CAP_DSHOW, "any": cv2.CAP_ANY}
MJPG = cv2.VideoWriter.fourcc(*"MJPG")

DEFAULT_OPEN_TIMEOUT_S = 20.0
#: An MSMF grab on a busy device blocks this long before it fails.
DEFAULT_FIRST_FRAME_TIMEOUT_S = 10.0
DEFAULT_LOST_AFTER_S = 2.0
DEFAULT_REOPEN_DELAYS_S = (0.5, 1.0, 2.0)
DEFAULT_FPS_WINDOW_S = 2.0
DEFAULT_JOIN_TIMEOUT_S = 3.0
#: Pause after a read that failed at once, so a dead device is not polled in a hot loop.
RETRY_PAUSE_S = 0.02

LOST_HINT = (
    "The camera stopped sending pictures: it was unplugged, or another app took it over. "
    "Plug it back in or close the other app, then run /jarvis hands restart."
)


class Capture(Protocol):
    """The part of ``cv2.VideoCapture`` the reader uses (and test doubles provide)."""

    def isOpened(self) -> bool: ...  # noqa: N802 - OpenCV's name

    def read(self) -> tuple[bool, np.ndarray | None]: ...

    def set(self, prop: int, value: float) -> bool: ...

    def release(self) -> None: ...


#: ``(index, OpenCV API id, open parameters) -> capture``; the parameters are empty except for MSMF.
CaptureFactory = Callable[[int, int, Sequence[int]], Capture]


def open_capture(index: int, api: int, params: Sequence[int]) -> Capture:
    """The real ``cv2.VideoCapture``."""
    if params:
        return cv2.VideoCapture(index, api, list(params))
    return cv2.VideoCapture(index, api)


def default_backends() -> tuple[str, ...]:
    if sys.platform == "win32":
        return ("msmf", "dshow")
    return ("any",)


def msmf_params(width: int, height: int, fps: int) -> list[int]:
    """MSMF's open parameters: only ones it can always apply, since one it can't fails the open."""
    return [
        cv2.CAP_PROP_FRAME_WIDTH,
        width,
        cv2.CAP_PROP_FRAME_HEIGHT,
        height,
        cv2.CAP_PROP_FPS,
        fps,
        cv2.CAP_PROP_HW_ACCELERATION,
        cv2.VIDEO_ACCELERATION_NONE,
    ]


# --------------------------------------------------------------------------- pure helpers


def _named(devices: Sequence[MfDevice]) -> list[tuple[int, str]]:
    """``(index, name)`` of each device, the form the name resolution and the hints work on."""
    return [(d.index, d.name) for d in devices]


def _at(devices: Sequence[MfDevice], index: int) -> MfDevice | None:
    return next((d for d in devices if d.index == index), None)


def cameras_hint(cameras: Sequence[tuple[int, str]]) -> str:
    listed = ", ".join(f"{index} {name!r}" for index, name in cameras)
    return f"Cameras found: {listed}. Choose one by its number or part of its name."


def names_a_camera(spec: str | None) -> bool:
    """True when ``spec`` is part of a camera's name rather than an index (or nothing)."""
    text = (spec or "").strip()
    return bool(text) and not (text.isascii() and text.isdigit())


def resolve_camera(spec: str | None, cameras: Sequence[tuple[int, str]]) -> int:
    """The camera index ``spec`` names: None or "" is 0, digits are an index, anything else part of a name.

    Names match case-insensitively, an exact name before a partial one, the
    lowest index first. Raises CameraError("no_camera") when no name matches.
    """
    text = (spec or "").strip()
    if not names_a_camera(text):
        return int(text) if text else 0
    needle = text.casefold()
    for index, name in cameras:
        if name.casefold() == needle:
            return index
    for index, name in cameras:
        if needle in name.casefold():
            return index
    if cameras:
        raise CameraError("no_camera", f"No camera's name contains {text!r}.", cameras_hint(cameras))
    raise CameraError("no_camera", f"No camera named {text!r} was found.", plat.no_camera_hint())


@dataclass(frozen=True)
class MfDevice:
    """One video capture device as Media Foundation lists it; ``index`` is CAP_MSMF's."""

    index: int
    name: str
    #: MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE_VIDCAP_SYMBOLIC_LINK: the device interface path it streams from,
    #: None when Media Foundation would not say.
    link: str | None = None


@dataclass(frozen=True)
class DshowDevice:
    """One video input device as DirectShow's moniker enumeration lists it; ``index`` is CAP_DSHOW's."""

    index: int
    name: str
    #: The moniker's DevicePath property: the same device interface path, under another interface class.
    path: str | None = None


def device_instance(path: str | None) -> str | None:
    r"""The part of a device interface path that names the device itself, or None when there is none.

    A path is ``\\?\usb#vid_2bdf&pid_0287&mi_00#7&1e3b1b2&0&0000#{<interface class>}\global``. Media
    Foundation's symbolic link and DirectShow's DevicePath are two interfaces of one device, so their class
    GUIDs differ while everything before them is the same.
    """
    if not path:
        return None
    text = path.strip().lower()
    for prefix in ("\\\\?\\", "\\\\.\\"):
        text = text.removeprefix(prefix)
    return text.split("#{", 1)[0].rstrip("\\") or None


def dshow_position(device: MfDevice, dshow: Sequence[DshowDevice]) -> int | None:
    """Where DirectShow counts ``device``, or None when nothing tells it apart from the others.

    Its own interface path first, then a friendly name only one DirectShow
    device has. None means DirectShow must not be opened for this camera:
    its numbering holds devices Media Foundation never listed.
    """
    wanted = device_instance(device.link)
    if wanted is not None:
        same = [d.index for d in dshow if device_instance(d.path) == wanted]
        if len(same) == 1:
            return same[0]
        if same:
            return None  # two monikers for one device: nothing to choose between them
    named = device.name.strip().casefold()
    by_name = [d.index for d in dshow if d.name.strip().casefold() == named] if named else []
    return by_name[0] if len(by_name) == 1 else None


def as_bgr(image: Any) -> np.ndarray | None:
    """A C-contiguous (H, W, 3) uint8 BGR copy or view of a captured frame; None when it can't be one."""
    if not isinstance(image, np.ndarray) or image.dtype != np.uint8 or image.size == 0:
        return None
    if image.ndim == 2 or (image.ndim == 3 and image.shape[2] == 1):
        image = cv2.cvtColor(image.reshape(image.shape[:2]), cv2.COLOR_GRAY2BGR)
    elif image.ndim == 3 and image.shape[2] == 4:
        image = cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
    elif image.ndim != 3 or image.shape[2] != 3:
        return None
    return np.ascontiguousarray(image)


class FrameRate:
    """Frames per second over the last ``window`` seconds of frame times."""

    def __init__(self, window: float = DEFAULT_FPS_WINDOW_S) -> None:
        self.window = window
        self._times: deque[float] = deque()

    def add(self, t: float) -> None:
        self._times.append(t)
        while self._times and self._times[0] < t - self.window:
            self._times.popleft()

    def rate(self) -> float | None:
        """None until the frames span half the window."""
        if len(self._times) < 3:
            return None
        span = self._times[-1] - self._times[0]
        if span < self.window / 2:
            return None
        return (len(self._times) - 1) / span


# --------------------------------------------------------------------------- the camera


@dataclass(eq=False)
class _Session:
    """One ``open()``: its reader thread and what that thread shares with ``read()``.

    A reader that outlives ``close()`` (stuck in a 10 s MSMF grab) only ever
    touches its own session, never the next one's.
    """

    index: int
    name: str
    #: What Media Foundation listed when the session started, refreshed on a reopen ([] when it would not say).
    devices: list[MfDevice]
    meter: FrameRate
    #: The camera as Media Foundation listed it, what DirectShow's list is matched against; None when it was not
    #: listed. Kept across a reopen that finds nothing at the camera's number (an unplugged webcam).
    device: MfDevice | None = None
    #: Whether Media Foundation has answered in this session: until it has, DirectShow's numbering is all there is.
    mf_answered: bool = False
    cond: threading.Condition = field(default_factory=threading.Condition)
    stop: threading.Event = field(default_factory=threading.Event)
    #: opening <-> starting (a backend opened; waiting for its first picture) -> streaming <-> reopening;
    #: then failed, lost or stopped.
    state: str = "opening"
    #: time.monotonic() when ``state`` last went to opening or starting: the caller's safety net counts from it.
    phase_started: float = field(default_factory=time.monotonic)
    error: CameraError | None = None
    #: CameraInfo.backend; ``streamed_on`` is the BACKENDS key that produced it.
    backend: str = ""
    streamed_on: str = ""
    #: Backends that opened the device but sent no picture, in the last attempt to start it.
    silent: list[str] = field(default_factory=list)
    width: int = 0
    height: int = 0
    frame: CameraFrame | None = None
    returned: int = 0
    bad_format_logged: bool = False
    thread: threading.Thread | None = None


def _copy(error: CameraError) -> CameraError:
    return CameraError(error.code, error.message, error.hint)


class OpenCVCamera:
    """A webcam through ``cv2.VideoCapture``; see the module docstring. Thread-safe."""

    def __init__(
        self,
        spec: str | None,
        *,
        width: int = 1280,
        height: int = 720,
        fps: int = 30,
        capture_factory: CaptureFactory | None = None,
        backends: Sequence[str] | None = None,
        open_timeout: float = DEFAULT_OPEN_TIMEOUT_S,
        first_frame_timeout: float = DEFAULT_FIRST_FRAME_TIMEOUT_S,
        lost_after: float = DEFAULT_LOST_AFTER_S,
        reopen_delays: Sequence[float] = DEFAULT_REOPEN_DELAYS_S,
        fps_window: float = DEFAULT_FPS_WINDOW_S,
        join_timeout: float = DEFAULT_JOIN_TIMEOUT_S,
        dshow_devices: Callable[[], Sequence[DshowDevice]] | None = None,
    ) -> None:
        if width <= 0 or height <= 0 or fps <= 0:
            raise ValueError(f"camera size and rate must be positive, got {width} x {height} at {fps}")
        self.spec = spec
        self.width, self.height, self.fps = int(width), int(height), int(fps)
        self.backends = tuple(backends) if backends is not None else default_backends()
        unknown = [b for b in self.backends if b not in BACKENDS]
        if unknown or not self.backends:
            raise ValueError(f"unknown camera backends {unknown or self.backends}")
        self._factory: CaptureFactory = capture_factory or open_capture
        #: DirectShow's own device list, for mapping the camera's Media Foundation index onto it.
        self._dshow_devices = dshow_devices
        self.open_timeout = open_timeout
        self.first_frame_timeout = first_frame_timeout
        self.lost_after = lost_after
        self.reopen_delays = tuple(reopen_delays)
        self.fps_window = fps_window
        self.join_timeout = join_timeout
        self._lock = threading.Lock()
        self._session: _Session | None = None

    # -- the Camera protocol --------------------------------------------------------------

    def open(self) -> CameraInfo:
        with self._lock:
            current = self._session
            if current is not None and current.state in ("streaming", "reopening"):
                return self._info(current)
            if current is not None:  # lost, or a failed open: start over
                self._session = None
                self._stop(current)
            # Resolved here, so a name that matches nothing fails at once on the caller's thread.
            listing = list_mf_devices()
            devices = listing or []
            index = resolve_camera(self.spec, _named(devices))
            device = _at(devices, index)
            session = _Session(
                index=index,
                name=device.name if device is not None else f"camera {index}",
                devices=devices,
                meter=FrameRate(self.fps_window),
                device=device,
                mf_answered=listing is not None,
            )
            session.thread = threading.Thread(target=self._run, args=(session,), name="jarvis-camera", daemon=True)
            self._session = session
            session.thread.start()
        try:
            self._wait_until_streaming(session)
        except CameraError:
            with self._lock:
                if self._session is session:
                    self._session = None
            self._stop(session)
            raise
        with session.cond:
            info = self._info(session)
        log.info(
            "camera %d (%s) open on %s: %d x %d, %d fps requested",
            info.index,
            info.name,
            info.backend,
            info.width,
            info.height,
            self.fps,
        )
        return info

    def read(self, timeout: float) -> CameraFrame | None:
        session = self._session
        if session is None:
            return None
        deadline = time.monotonic() + max(0.0, timeout)
        with session.cond:
            while True:
                frame = session.frame
                if frame is not None and frame.seq > session.returned:
                    session.returned = frame.seq
                    return frame
                if session.state in ("failed", "lost"):
                    # The reader has given up: say why, every time, instead of looking like a slow camera.
                    raise self._gave_up(session)
                if session.state == "stopped":  # closed: not an error
                    return None
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                session.cond.wait(left)

    @property
    def info(self) -> CameraInfo | None:
        session = self._session
        if session is None:
            return None
        with session.cond:
            if session.state not in ("streaming", "reopening"):
                return None
            return self._info(session)

    def close(self) -> None:
        try:
            with self._lock:
                session, self._session = self._session, None
            if session is not None:
                self._stop(session)
        except Exception:
            log.exception("closing the camera failed")

    # -- caller side ----------------------------------------------------------------------

    def _info(self, s: _Session) -> CameraInfo:
        measured = s.meter.rate()
        return CameraInfo(
            name=s.name,
            index=s.index,
            backend=s.backend,
            width=s.width,
            height=s.height,
            fps=round(measured, 1) if measured is not None else float(self.fps),
        )

    def _wait_until_streaming(self, s: _Session) -> None:
        """Until the reader has a first frame or an error; a safety net for a reader stuck in a driver call.

        The reader decides when a backend has had its chance and moves on to
        the next one; these deadlines only catch a call that never returns.
        """
        with s.cond:
            while True:
                if s.state in ("streaming", "reopening"):  # a first frame came (and maybe more went wrong since)
                    return
                if s.error is not None:
                    raise _copy(s.error)
                if s.stop.is_set() or s.state == "stopped":
                    raise CameraError("camera_lost", "The camera was closed while it was opening.", LOST_HINT)
                now = time.monotonic()
                if s.state == "starting":
                    # The reader gives up at first_frame_timeout, but a read begun just before can block about
                    # as long again (an MSMF grab waits 10 s, the default timeout), so allow twice that.
                    deadline = s.phase_started + 2 * self.first_frame_timeout
                    if now >= deadline:
                        raise self._no_picture(s)
                else:
                    deadline = s.phase_started + self.open_timeout
                    if now >= deadline:
                        raise CameraError(
                            "camera_in_use",
                            f"{s.name} did not open within {self.open_timeout:g} s.",
                            plat.camera_in_use_hint(),
                        )
                s.cond.wait(deadline - now)

    def _stop(self, s: _Session) -> None:
        s.stop.set()
        with s.cond:
            if s.state not in ("failed", "lost"):
                s.state = "stopped"
            s.cond.notify_all()
        thread = s.thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(self.join_timeout)
            if thread.is_alive():
                log.warning("the camera is still busy in a read; it is released as soon as that read returns")

    def _gave_up(self, s: _Session) -> CameraError:
        """Why the reader stopped: the error it ended with (its code and hint), else a plain ``camera_lost``."""
        if s.error is not None:
            return _copy(s.error)
        return CameraError("camera_lost", f"{s.name} stopped sending pictures.", LOST_HINT)

    def _no_picture(self, s: _Session) -> CameraError:
        tried = f" on {' or '.join(s.silent)}" if s.silent else ""
        return CameraError(
            "camera_in_use",
            f"{s.name} opened but sent no picture within {self.first_frame_timeout:g} s{tried}.",
            plat.camera_in_use_hint(),
        )

    # -- the reader thread ----------------------------------------------------------------

    def _run(self, s: _Session) -> None:
        cap: Capture | None = None
        try:
            started = self._start_device(s, s.index, s.device, self.backends, announce=True)
            if started is None:
                if not s.stop.is_set():
                    # Opened somewhere but never a picture: something else has the camera.
                    self._end(s, "failed", self._no_picture(s) if s.silent else self._open_error(s))
                return
            cap, key = started
            with s.cond:
                s.backend, s.streamed_on = self._backend_name(cap, key), key
            self._set_state(s, "streaming")
            last_good = time.monotonic()
            while not s.stop.is_set():
                image = self._read(s, cap)
                if image is not None:
                    self._publish(s, image, clock_now())
                    last_good = time.monotonic()
                    continue
                now = time.monotonic()
                if now - last_good < self.lost_after:
                    s.stop.wait(RETRY_PAUSE_S)
                    continue
                log.warning("%s sent no picture for %.1f s; reopening it", s.name, now - last_good)
                self._release(cap)
                cap = None
                self._set_state(s, "reopening")
                cap = self._reopen(s)
                if cap is None:
                    if not s.stop.is_set():
                        self._end(
                            s, "lost", CameraError("camera_lost", f"{s.name} stopped sending pictures.", LOST_HINT)
                        )
                    return
                self._set_state(s, "streaming")
                last_good = time.monotonic()
        except Exception as exc:
            log.exception("the camera reader failed")
            self._end(
                s, "lost", CameraError("camera_lost", f"The camera failed: {type(exc).__name__}: {exc}", LOST_HINT)
            )
        finally:
            if cap is not None:
                self._release(cap)
            with s.cond:
                if s.state not in ("failed", "lost"):
                    s.state = "stopped"
                s.cond.notify_all()

    def _set_state(self, s: _Session, state: str) -> None:
        with s.cond:
            if s.state != "stopped":
                s.state = state
                if state in ("opening", "starting"):
                    s.phase_started = time.monotonic()
            s.cond.notify_all()

    def _end(self, s: _Session, state: str, error: CameraError) -> None:
        log.warning("camera: %s: %s", error.code, error.message)
        with s.cond:
            s.error = error
            s.state = state
            s.cond.notify_all()

    def _start_device(
        self, s: _Session, index: int, device: MfDevice | None, order: Sequence[str], *, announce: bool = False
    ) -> tuple[Capture, str] | None:
        """Camera ``index`` on the first backend in ``order`` that opens it and sends a picture.

        ``index`` is Media Foundation's and ``device`` what it listed there;
        each backend is asked for the device where that backend counts it,
        and a backend that cannot be told which device that is (see
        ``_device_index``) is skipped.

        Returns the capture and its BACKENDS key, with that first picture
        published; None when no backend got that far (``s.silent`` then lists
        the ones that opened without a picture) or the camera was closed.
        ``announce`` moves ``s.state`` through opening and starting, which the
        caller of ``open()`` watches.
        """
        with s.cond:
            s.silent = []
        for key in order:
            if s.stop.is_set():
                return None
            at = self._device_index(s, index, device, key)
            if at is None:
                continue
            if announce:
                self._set_state(s, "opening")
            cap = self._open_backend(s, at, key)
            if cap is None:
                continue
            if announce:
                with s.cond:
                    s.backend = self._backend_name(cap, key)
                self._set_state(s, "starting")
            if self._first_frame(s, cap, time.monotonic() + self.first_frame_timeout):
                return cap, key
            self._release(cap)
            if s.stop.is_set():
                return None
            with s.cond:
                s.silent.append(key)
            log.info("camera %d opened with %s but sent no picture within %g s", index, key, self.first_frame_timeout)
        return None

    def _device_index(self, s: _Session, index: int, device: MfDevice | None, backend: str) -> int | None:
        """Where ``backend`` counts the camera, or None when it must be skipped.

        Only DirectShow counts differently: its list also holds filter-based
        virtual cameras Media Foundation never listed, so ``device`` is looked
        up in it. With no device to look up, DirectShow's own numbering is
        used only while Media Foundation has never answered (a Windows N
        install has none): once it has, a camera it does not list is not there
        (unplugged, or a number past its list), and whatever DirectShow counts
        at that number is some other device, typically a filter camera such
        as OBS Virtual Camera that "streams" a placeholder picture.
        """
        if backend != "dshow":
            return index
        if device is None:
            if s.mf_answered:
                log.info("not trying DirectShow for camera %d: Media Foundation does not list it", index)
                return None
            return index
        at = dshow_position(device, self._dshow_list())
        if at is None:
            log.info("not trying DirectShow for %s: DirectShow's device list does not say which device it is", s.name)
        elif at != index:
            log.info("%s is camera %d for Media Foundation and camera %d for DirectShow", s.name, index, at)
        return at

    def _dshow_list(self) -> list[DshowDevice]:
        """DirectShow's devices; [] when they cannot be listed, which means DirectShow is skipped."""
        lister = self._dshow_devices or list_dshow_devices
        try:
            return list(lister())
        except Exception as exc:  # noqa: BLE001 - a listing we only use to choose a device: never fatal
            log.warning("could not list the DirectShow cameras: %s", exc)
            return []

    def _open_backend(self, s: _Session, index: int, backend: str) -> Capture | None:
        """Camera ``index`` opened and configured on ``backend``; None when it does not open."""
        api = BACKENDS[backend]
        params = msmf_params(self.width, self.height, self.fps) if backend == "msmf" else []
        started = time.monotonic()
        try:
            cap = self._factory(index, api, params)
        except Exception as exc:  # noqa: BLE001 - cv2.error, or a driver's own failure: try the next backend
            log.info("camera %d did not open with %s: %s", index, backend, exc)
            return None
        try:
            opened = bool(cap.isOpened())
        except Exception as exc:  # noqa: BLE001 - as above
            log.info("camera %d: isOpened failed with %s: %s", index, backend, exc)
            opened = False
        if not opened:
            log.info("camera %d did not open with %s", index, backend)
            self._release(cap)
            return None
        if s.stop.is_set():  # closed while the backend took its time
            self._release(cap)
            return None
        if backend == "dshow":
            # Each set restarts the device; FPS drops the FOURCC, so MJPG goes last.
            self._configure(cap, [(cv2.CAP_PROP_FPS, self.fps), *self._size(), (cv2.CAP_PROP_FOURCC, MJPG)])
        elif backend == "any":
            self._configure(cap, [*self._size(), (cv2.CAP_PROP_FPS, self.fps)])
        log.info("camera %d opened with %s in %.2f s", index, backend, time.monotonic() - started)
        return cap

    def _size(self) -> list[tuple[int, float]]:
        return [(cv2.CAP_PROP_FRAME_WIDTH, self.width), (cv2.CAP_PROP_FRAME_HEIGHT, self.height)]

    @staticmethod
    def _configure(cap: Capture, props: Sequence[tuple[int, float]]) -> None:
        for prop, value in props:
            try:
                ok = cap.set(prop, value)
            except Exception as exc:  # noqa: BLE001 - a property the driver refuses is not fatal
                log.debug("camera set(%d, %s) raised %s", prop, value, exc)
                continue
            if not ok:
                log.debug("camera refused set(%d, %s)", prop, value)

    @staticmethod
    def _backend_name(cap: Capture, backend: str) -> str:
        if backend != "any":
            return backend
        try:
            name = str(cap.getBackendName()).lower()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - only a label
            return "any"
        return name if name in ("v4l2", "avfoundation", "msmf", "dshow") else "any"

    @staticmethod
    def _release(cap: Capture) -> None:
        try:
            cap.release()
        except Exception as exc:  # noqa: BLE001 - nothing more can be done for this device
            log.warning("releasing the camera failed: %s", exc)

    def _read(self, s: _Session, cap: Capture) -> np.ndarray | None:
        try:
            ok, image = cap.read()
        except Exception as exc:  # noqa: BLE001 - cv2.error from a dying device counts as a failed read
            log.debug("camera read raised %s", exc)
            return None
        if not ok or image is None:
            return None
        bgr = as_bgr(image)
        if bgr is None and not s.bad_format_logged:
            s.bad_format_logged = True
            log.warning(
                "camera frames have an unusable format: %s %s",
                getattr(image, "shape", None),
                getattr(image, "dtype", type(image).__name__),
            )
        return bgr

    def _publish(self, s: _Session, image: np.ndarray, now: float) -> None:
        height, width = image.shape[:2]
        with s.cond:
            seq = (s.frame.seq if s.frame is not None else 0) + 1
            s.frame = CameraFrame(seq=seq, t=now, image=image)
            s.width, s.height = width, height
            s.meter.add(now)
            s.cond.notify_all()

    def _first_frame(self, s: _Session, cap: Capture, deadline: float) -> bool:
        while not s.stop.is_set():
            image = self._read(s, cap)
            if image is not None:
                self._publish(s, image, clock_now())
                return True
            if time.monotonic() >= deadline:
                return False
            s.stop.wait(RETRY_PAUSE_S)
        return False

    def _reopen(self, s: _Session) -> Capture | None:
        for attempt, delay in enumerate(self.reopen_delays, 1):
            if s.stop.wait(delay):
                return None
            index, device = s.index, s.device
            # Indices shift when devices come and go. When Media Foundation will not say this time, the camera
            # is still the device it listed before.
            listing = list_mf_devices()
            if listing is not None:
                with s.cond:
                    s.devices, s.mf_answered = listing, True
                if names_a_camera(self.spec):
                    # A camera chosen by name is looked up again, in case its index moved.
                    try:
                        index = resolve_camera(self.spec, _named(listing))
                    except CameraError:
                        log.info("reopen %d: no camera matches %r yet", attempt, self.spec)
                        continue
                # What Media Foundation now lists at that number, which is what MSMF opens; with nothing there
                # (the webcam is unplugged) DirectShow still looks only for the camera it was.
                device = _at(listing, index) or device
            # The backend that streamed before goes first: a webcam MSMF cannot read is not waited out again.
            order = sorted(self.backends, key=lambda backend: backend != s.streamed_on)
            started = self._start_device(s, index, device, order)
            if started is None:
                if s.silent:
                    log.info("reopen %d: the camera opened on %s but sent no picture", attempt, " and ".join(s.silent))
                else:
                    log.info("reopen %d of %d failed", attempt, len(self.reopen_delays))
                continue
            cap, key = started
            with s.cond:
                s.index, s.backend, s.streamed_on = index, self._backend_name(cap, key), key
                if device is not None:
                    s.device, s.name = device, device.name
            log.info("%s reopened on %s", s.name, key)
            return cap
        return None

    def _open_error(self, s: _Session) -> CameraError:
        """Why no backend opened the camera, as well as it can be told."""
        cameras = _named(s.devices)
        if cameras and s.index not in dict(cameras):
            return CameraError(
                "no_camera", f"There is no camera {s.index}; {len(cameras)} found.", cameras_hint(cameras)
            )
        try:
            denied = plat.camera_access_denied()
        except Exception as exc:  # noqa: BLE001 - a diagnostic only
            log.debug("could not read the camera privacy switches: %s", exc)
            denied = False
        if denied:
            return CameraError(
                "camera_blocked", "The camera is turned off in the privacy settings.", plat.camera_blocked_hint()
            )
        if cameras:
            # Listed but no backend could open it: what Windows does when privacy settings block desktop apps.
            return CameraError("camera_blocked", f"{s.name} is there but would not open.", plat.camera_blocked_hint())
        return CameraError("no_camera", "No camera found.", plat.no_camera_hint())


# --------------------------------------------------------------------------- listing cameras (Windows)


def list_mf_devices() -> list[MfDevice] | None:
    """The video capture devices Media Foundation lists, in CAP_MSMF order; None elsewhere or when it won't say.

    None and [] differ: [] is Media Foundation saying there is no camera, so
    DirectShow is not opened at a number it does not list; None leaves
    DirectShow's own numbering as all there is.
    """
    if sys.platform != "win32":
        return None
    try:
        return enumerate_msmf(_load_media_foundation())
    except Exception as exc:  # noqa: BLE001 - no Media Foundation (Windows N), COM trouble: just no names
        log.warning("could not list the cameras: %s", exc)
        return None


def list_cameras() -> list[tuple[int, str]]:
    """(index, name) of the video capture devices Media Foundation lists, in CAP_MSMF order; [] elsewhere."""
    return _named(list_mf_devices() or [])


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def parse(cls, text: str) -> GUID:
        u = uuid.UUID(text)
        return cls(u.time_low, u.time_mid, u.time_hi_version, (ctypes.c_ubyte * 8)(*u.bytes[8:]))


MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE = GUID.parse("c60ac5fe-252a-478f-a0ef-bc8fa5f7cad3")
MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE_VIDCAP_GUID = GUID.parse("8ac3587a-4ae7-42d8-99e0-0a6013eef90f")
MF_DEVSOURCE_ATTRIBUTE_FRIENDLY_NAME = GUID.parse("60d0e559-52f8-4fa2-bbce-acdb34a8ec01")
MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE_VIDCAP_SYMBOLIC_LINK = GUID.parse("58f0aad8-22bf-4f8a-bb3d-d2c4978c6e2f")

MF_VERSION = 0x00020070  # MF_SDK_VERSION << 16 | MF_API_VERSION
MFSTARTUP_LITE = 0x1  # no sockets: we only enumerate
COINIT_MULTITHREADED = 0x0
S_OK, S_FALSE = 0, 1
RPC_E_CHANGED_MODE = 0x80010106 - (1 << 32)  # COM already set up as STA on this thread: usable, not ours to undo

#: HRESULT is a 32-bit LONG (ctypes.c_long is 64-bit off Windows, where the tests fake these calls).
HRESULT = ctypes.c_int32
# Vtable slots: IUnknown 0-2, IMFAttributes 3-32 (mfobjects.h order), IMFActivate 33-35.
SLOT_RELEASE = 2
SLOT_GET_ALLOCATED_STRING = 13
SLOT_SET_GUID = 24


@dataclass(frozen=True)
class MediaFoundation:
    """The few ole32, mfplat and mf functions the enumeration calls, prototyped; tests pass fakes."""

    functype: Callable[..., Any]
    co_initialize_ex: Callable[..., int]
    co_uninitialize: Callable[[], None]
    co_task_mem_free: Callable[[Any], None]
    mf_startup: Callable[[int, int], int]
    mf_shutdown: Callable[[], int]
    mf_create_attributes: Callable[..., int]
    mf_enum_device_sources: Callable[..., int]


def _load_media_foundation() -> MediaFoundation:
    from ctypes import wintypes

    ole32 = ctypes.WinDLL("ole32", use_last_error=True)  # type: ignore[attr-defined]
    mfplat = ctypes.WinDLL("mfplat", use_last_error=True)  # type: ignore[attr-defined]
    mf = ctypes.WinDLL("mf", use_last_error=True)  # type: ignore[attr-defined]
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole32.CoInitializeEx.restype = HRESULT
    ole32.CoUninitialize.argtypes = []
    ole32.CoUninitialize.restype = None
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole32.CoTaskMemFree.restype = None
    mfplat.MFStartup.argtypes = [wintypes.ULONG, wintypes.DWORD]
    mfplat.MFStartup.restype = HRESULT
    mfplat.MFShutdown.argtypes = []
    mfplat.MFShutdown.restype = HRESULT
    mfplat.MFCreateAttributes.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint32]
    mfplat.MFCreateAttributes.restype = HRESULT
    mf.MFEnumDeviceSources.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)),
        ctypes.POINTER(ctypes.c_uint32),
    ]
    mf.MFEnumDeviceSources.restype = HRESULT
    return MediaFoundation(
        functype=ctypes.WINFUNCTYPE,  # type: ignore[attr-defined]
        co_initialize_ex=ole32.CoInitializeEx,
        co_uninitialize=ole32.CoUninitialize,
        co_task_mem_free=ole32.CoTaskMemFree,
        mf_startup=mfplat.MFStartup,
        mf_shutdown=mfplat.MFShutdown,
        mf_create_attributes=mfplat.MFCreateAttributes,
        mf_enum_device_sources=mf.MFEnumDeviceSources,
    )


def _check(hr: int, what: str) -> None:
    if hr < 0:
        raise OSError(f"{what} failed with HRESULT 0x{hr & 0xFFFFFFFF:08X}")


def _com_method(
    api: MediaFoundation | DirectShow, this: int, slot: int, restype: Any, *argtypes: Any
) -> Callable[..., Any]:
    """Method ``slot`` of the COM object at address ``this``, bound to it."""
    vtable = ctypes.cast(this, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    function = api.functype(restype, ctypes.c_void_p, *argtypes)(vtable[slot])
    return lambda *args: function(this, *args)


def _com_release(api: MediaFoundation | DirectShow, this: int) -> None:
    _com_method(api, this, SLOT_RELEASE, ctypes.c_uint32)()


def _allocated_string(api: MediaFoundation, this: int, key: GUID) -> str | None:
    get = _com_method(
        api,
        this,
        SLOT_GET_ALLOCATED_STRING,
        HRESULT,
        ctypes.POINTER(GUID),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_uint32),
    )
    text, length = ctypes.c_void_p(), ctypes.c_uint32()
    if get(ctypes.byref(key), ctypes.byref(text), ctypes.byref(length)) < 0 or not text.value:
        return None
    try:
        return ctypes.wstring_at(text.value, length.value)
    finally:
        api.co_task_mem_free(text.value)


def enumerate_msmf(api: MediaFoundation) -> list[MfDevice]:
    """MFEnumDeviceSources for video capture devices, the call OpenCV's MSMF backend makes. Raises OSError."""
    hr = api.co_initialize_ex(None, COINIT_MULTITHREADED)
    if hr < 0 and hr != RPC_E_CHANGED_MODE:
        _check(hr, "CoInitializeEx")
    try:
        _check(api.mf_startup(MF_VERSION, MFSTARTUP_LITE), "MFStartup")
        try:
            return _enumerate_sources(api)
        finally:
            api.mf_shutdown()
    finally:
        if hr in (S_OK, S_FALSE):
            api.co_uninitialize()


def _enumerate_sources(api: MediaFoundation) -> list[MfDevice]:
    attributes = ctypes.c_void_p()
    _check(api.mf_create_attributes(ctypes.byref(attributes), 1), "MFCreateAttributes")
    if not attributes.value:
        raise OSError("MFCreateAttributes returned no object")
    try:
        set_guid = _com_method(
            api, attributes.value, SLOT_SET_GUID, HRESULT, ctypes.POINTER(GUID), ctypes.POINTER(GUID)
        )
        _check(
            set_guid(
                ctypes.byref(MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE),
                ctypes.byref(MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE_VIDCAP_GUID),
            ),
            "IMFAttributes::SetGUID",
        )
        devices = ctypes.POINTER(ctypes.c_void_p)()
        count = ctypes.c_uint32()
        _check(
            api.mf_enum_device_sources(attributes, ctypes.byref(devices), ctypes.byref(count)),
            "MFEnumDeviceSources",
        )
        activates = [devices[i] for i in range(count.value)] if devices else []
        try:
            cameras = []
            for index, activate in enumerate(activates):
                name = link = None
                if activate:
                    name = _allocated_string(api, activate, MF_DEVSOURCE_ATTRIBUTE_FRIENDLY_NAME)
                    # The device interface this source streams from: what the DirectShow mapping matches on.
                    link = _allocated_string(api, activate, MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE_VIDCAP_SYMBOLIC_LINK)
                cameras.append(MfDevice(index, name or f"camera {index}", link))
            return cameras
        finally:
            for activate in activates:
                if activate:
                    _com_release(api, activate)
            api.co_task_mem_free(ctypes.cast(devices, ctypes.c_void_p).value)
    finally:
        _com_release(api, attributes.value)


# --------------------------------------------------------------------------- listing DirectShow devices (Windows)

CLSID_SYSTEM_DEVICE_ENUM = GUID.parse("62be5d10-60eb-11d0-bd3b-00a0c911ce86")
IID_CREATE_DEV_ENUM = GUID.parse("29840822-5b84-11d0-bd3b-00a0c911ce86")
CLSID_VIDEO_INPUT_DEVICE_CATEGORY = GUID.parse("860bb310-5d01-11d0-bd3b-00a0c911ce86")
IID_PROPERTY_BAG = GUID.parse("55272a00-42cb-11ce-8135-00aa004bb851")

CLSCTX_INPROC_SERVER = 0x1
VT_BSTR = 8
#: Devices past this are not even reachable through OpenCV's DirectShow backend (VI_MAX_CAMERAS).
MAX_DSHOW_DEVICES = 20

# Vtable slots after IUnknown's 0-2: ICreateDevEnum has one method; IEnumMoniker's Next is its first;
# IMoniker is an IPersistStream (GetClassID 3, IsDirty 4, Load 5, Save 6, GetSizeMax 7), so BindToStorage
# is 9; IPropertyBag::Read is its first.
SLOT_CREATE_CLASS_ENUMERATOR = 3
SLOT_ENUM_NEXT = 3
SLOT_BIND_TO_STORAGE = 9
SLOT_PROPERTY_BAG_READ = 3


class VARIANT(ctypes.Structure):
    """Only as much of VARIANT as a BSTR property needs; the union is 16 bytes wide (DECIMAL, BRECORD)."""

    _fields_ = [
        ("vt", ctypes.c_uint16),
        ("reserved", ctypes.c_uint16 * 3),
        ("value", ctypes.c_void_p),
        ("union_tail", ctypes.c_void_p),
    ]


@dataclass(frozen=True)
class DirectShow:
    """The ole32 and oleaut32 functions the moniker enumeration calls, prototyped; tests pass fakes."""

    functype: Callable[..., Any]
    co_initialize_ex: Callable[..., int]
    co_uninitialize: Callable[[], None]
    co_create_instance: Callable[..., int]
    variant_clear: Callable[[Any], int]


def _load_directshow() -> DirectShow:
    from ctypes import wintypes

    ole32 = ctypes.WinDLL("ole32", use_last_error=True)  # type: ignore[attr-defined]
    oleaut32 = ctypes.WinDLL("oleaut32", use_last_error=True)  # type: ignore[attr-defined]
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole32.CoInitializeEx.restype = HRESULT
    ole32.CoUninitialize.argtypes = []
    ole32.CoUninitialize.restype = None
    ole32.CoCreateInstance.argtypes = [
        ctypes.POINTER(GUID),
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(GUID),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    ole32.CoCreateInstance.restype = HRESULT
    oleaut32.VariantClear.argtypes = [ctypes.POINTER(VARIANT)]
    oleaut32.VariantClear.restype = HRESULT
    return DirectShow(
        functype=ctypes.WINFUNCTYPE,  # type: ignore[attr-defined]
        co_initialize_ex=ole32.CoInitializeEx,
        co_uninitialize=ole32.CoUninitialize,
        co_create_instance=ole32.CoCreateInstance,
        variant_clear=oleaut32.VariantClear,
    )


def list_dshow_devices() -> list[DshowDevice]:
    """The devices DirectShow counts, in CAP_DSHOW order; [] elsewhere or when the enumeration fails.

    [] means no device can be matched, so DirectShow is skipped for a listed
    camera: better than opening whatever sits at that number.
    """
    if sys.platform != "win32":
        return []
    try:
        return enumerate_dshow(_load_directshow())
    except Exception as exc:  # noqa: BLE001 - COM trouble, a filter that misbehaves: just no DirectShow
        log.warning("could not list the DirectShow cameras: %s", exc)
        return []


def enumerate_dshow(api: DirectShow) -> list[DshowDevice]:
    """The monikers of CLSID_VideoInputDeviceCategory, the list OpenCV's DirectShow backend counts.

    ``videoInput::getDevice`` binds the n-th moniker of that category, counting
    every moniker it walks past, so this counts them the same way: a moniker
    whose property bag will not open still takes its number (with no name).
    Raises OSError.
    """
    hr = api.co_initialize_ex(None, COINIT_MULTITHREADED)
    if hr < 0 and hr != RPC_E_CHANGED_MODE:
        _check(hr, "CoInitializeEx")
    try:
        return _enumerate_monikers(api)
    finally:
        if hr in (S_OK, S_FALSE):
            api.co_uninitialize()


def _enumerate_monikers(api: DirectShow) -> list[DshowDevice]:
    device_enum = ctypes.c_void_p()
    _check(
        api.co_create_instance(
            ctypes.byref(CLSID_SYSTEM_DEVICE_ENUM),
            None,
            CLSCTX_INPROC_SERVER,
            ctypes.byref(IID_CREATE_DEV_ENUM),
            ctypes.byref(device_enum),
        ),
        "CoCreateInstance(CLSID_SystemDeviceEnum)",
    )
    if not device_enum.value:
        raise OSError("CoCreateInstance gave no system device enumerator")
    try:
        create_enumerator = _com_method(
            api,
            device_enum.value,
            SLOT_CREATE_CLASS_ENUMERATOR,
            HRESULT,
            ctypes.POINTER(GUID),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_uint32,
        )
        monikers = ctypes.c_void_p()
        hr = create_enumerator(ctypes.byref(CLSID_VIDEO_INPUT_DEVICE_CATEGORY), ctypes.byref(monikers), 0)
        _check(hr, "ICreateDevEnum::CreateClassEnumerator")
        if hr != S_OK or not monikers.value:  # S_FALSE: the category is empty (no capture device at all)
            return []
        try:
            return _read_monikers(api, monikers.value)
        finally:
            _com_release(api, monikers.value)
    finally:
        _com_release(api, device_enum.value)


def _read_monikers(api: DirectShow, enumerator: int) -> list[DshowDevice]:
    next_moniker = _com_method(
        api,
        enumerator,
        SLOT_ENUM_NEXT,
        HRESULT,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_uint32),
    )
    devices: list[DshowDevice] = []
    for index in range(MAX_DSHOW_DEVICES):
        moniker, fetched = ctypes.c_void_p(), ctypes.c_uint32()
        if next_moniker(1, ctypes.byref(moniker), ctypes.byref(fetched)) != S_OK:
            break
        if not moniker.value:
            break
        try:
            devices.append(_moniker_device(api, index, moniker.value))
        finally:
            _com_release(api, moniker.value)
    return devices


def _moniker_device(api: DirectShow, index: int, moniker: int) -> DshowDevice:
    """One moniker's device: its friendly name and device path, as far as the property bag gives them."""
    bind = _com_method(
        api,
        moniker,
        SLOT_BIND_TO_STORAGE,
        HRESULT,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(GUID),
        ctypes.POINTER(ctypes.c_void_p),
    )
    bag = ctypes.c_void_p()
    if bind(None, None, ctypes.byref(IID_PROPERTY_BAG), ctypes.byref(bag)) < 0 or not bag.value:
        log.debug("DirectShow device %d has no property bag", index)
        return DshowDevice(index, "")
    try:
        name = _bag_string(api, bag.value, "FriendlyName") or _bag_string(api, bag.value, "Description")
        return DshowDevice(index, name or "", _bag_string(api, bag.value, "DevicePath"))
    finally:
        _com_release(api, bag.value)


def _bag_string(api: DirectShow, bag: int, name: str) -> str | None:
    """``IPropertyBag::Read`` of a string property, or None when it is absent or not a string."""
    read = _com_method(
        api,
        bag,
        SLOT_PROPERTY_BAG_READ,
        HRESULT,
        ctypes.c_wchar_p,
        ctypes.POINTER(VARIANT),
        ctypes.c_void_p,
    )
    variant = VARIANT()  # zeroed: VT_EMPTY, which is what VariantInit leaves
    if read(name, ctypes.byref(variant), None) < 0:
        return None
    try:
        if variant.vt != VT_BSTR or not variant.value:
            return None
        return ctypes.wstring_at(variant.value) or None
    finally:
        api.variant_clear(ctypes.byref(variant))  # frees the BSTR
