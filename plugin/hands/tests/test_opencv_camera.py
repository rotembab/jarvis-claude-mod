"""The OpenCV camera against a fake capture (no camera here), and Media Foundation's listing against fake COM objects.

The fakes record which thread did what, the backends and parameters the
camera asked for, and every release, so the Windows-only paths (MSMF then
DirectShow, the error classification, the ctypes enumeration) run on any OS.
"""

from __future__ import annotations

import ctypes
import math
import os
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from itertools import islice, pairwise
from typing import Any

import cv2
import numpy as np
import pytest

from jarvis_hands import platform as plat
from jarvis_hands.camera import CameraError, create_camera
from jarvis_hands.camera import opencv_camera as oc
from jarvis_hands.camera.base import CameraFrame
from jarvis_hands.camera.opencv_camera import FrameRate, OpenCVCamera, as_bgr, resolve_camera

MSMF_PARAMS = [
    cv2.CAP_PROP_FRAME_WIDTH,
    1280,
    cv2.CAP_PROP_FRAME_HEIGHT,
    720,
    cv2.CAP_PROP_FPS,
    30,
    cv2.CAP_PROP_HW_ACCELERATION,
    cv2.VIDEO_ACCELERATION_NONE,
]


#: How long a test polls for what the camera's reader thread does before it fails. A hang guard, not a claim about
#: speed: it costs nothing while the thread is quick, and a runner that oversleeps by hundreds of ms must not fail it.
PATIENCE = 30.0
#: How long a test sleeps between two polls of a camera.
POLL_S = 0.005

#: The device interface classes one camera appears under: KSCATEGORY_VIDEO_CAMERA for Media Foundation's
#: symbolic link, KSCATEGORY_CAPTURE for DirectShow's DevicePath.
INTERFACE = {"mf": "e5323777-f976-4f5b-9b55-b94699c46e44", "dshow": "65e8773d-8f56-11d0-a3b9-00a0c9223196"}


def interface_path(name: str, kind: str) -> str:
    """A device interface path for ``name``: the same device as Media Foundation or as DirectShow names it."""
    slug = name.lower().replace(" ", "_")
    return rf"\\?\usb#vid_2bdf&pid_0287&mi_00#7&{slug}&0&0000#{{{INTERFACE[kind]}}}\global"


LINKS = {name: interface_path(name, "mf") for name in ("UGREEN Camera", "Caméra intégrée", "Integrated Webcam")}


def picture(n: int, shape: tuple[int, ...] = (4, 6, 3)) -> np.ndarray:
    """Frame ``n`` of a fake stream: every pixel is ``n % 256``, so a frame shows which read made it."""
    return np.full(shape, n % 256, np.uint8)


def fails_after(count: int, offset: int = 0) -> Callable[[int], np.ndarray | None]:
    return lambda n: picture(offset + n) if n < count else None


@dataclass
class FakeCapture:
    """A ``cv2.VideoCapture`` double: read ``n`` returns ``frames(n)`` (None is a failed read) after ``delay``."""

    opened: bool = True
    frames: Callable[[int], Any] = picture
    delay: float = 0.002
    backend_name: str = "V4L2"
    events: list[str] | None = None
    reads: int = 0
    sets: list[tuple[int, float]] = field(default_factory=list)
    released: int = 0
    threads: set[int] = field(default_factory=set)

    def _touch(self) -> None:
        self.threads.add(threading.get_ident())

    def isOpened(self) -> bool:  # noqa: N802 - OpenCV's name
        self._touch()
        return self.opened

    def read(self) -> tuple[bool, Any]:
        self._touch()
        if self.delay:
            time.sleep(self.delay)
        n, self.reads = self.reads, self.reads + 1
        image = self.frames(n)
        if image is None and isinstance(oc.time, ReadClock):  # nothing to return: the read waited, on that clock
            oc.time.advance(oc.time.step)
        return image is not None, image

    def set(self, prop: int, value: float) -> bool:
        self.sets.append((prop, value))
        return True

    def release(self) -> None:
        self._touch()
        self.released += 1
        if self.events is not None:
            self.events.append("release")

    def getBackendName(self) -> str:  # noqa: N802 - OpenCV's name
        return self.backend_name


@dataclass
class FakeFactory:
    """Hands out ``captures`` in order, then captures that do not open (an unplugged camera)."""

    captures: list[FakeCapture] = field(default_factory=list)
    calls: list[tuple[int, int, list[int]]] = field(default_factory=list)
    made: list[FakeCapture] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    #: ``ReadClock.now`` at each call, when the camera runs on one: when, in simulated seconds, each open was asked for.
    at: list[float] = field(default_factory=list)

    def __call__(self, index: int, api: int, params: Any) -> FakeCapture:
        self.calls.append((index, api, list(params)))
        self.events.append(f"open {index} {api}")
        if isinstance(oc.time, ReadClock):
            self.at.append(oc.time.now)
        capture = self.captures.pop(0) if self.captures else FakeCapture(opened=False)
        capture.events = self.events
        self.made.append(capture)
        return capture


def silent_capture() -> FakeCapture:
    """A camera that opens and then sends nothing: what MSMF does on the webcams the DirectShow fallback is for."""
    return FakeCapture(frames=lambda n: None, delay=0)


class ReadClock:
    """The camera module's ``time``, on simulated seconds that pass only while a fake read blocks.

    The camera counts its waits on ``time.monotonic``: the reader gives up on
    a backend ``first_frame_timeout`` after it began waiting, and the caller of
    ``open()`` has a safety net at twice that, for a read begun just before
    the reader's deadline that blocks about as long again. On the wall clock
    the two race: a runner that wakes the reader tens of ms late on every
    read, as GitHub's macOS runners do, has the net fire while the reader is
    still on the first backend. Here a wait is a number of reads instead, so
    nothing a slow machine does moves either deadline.

    A read with nothing to return takes ``step`` seconds (``FakeCapture``
    does that); ``advance`` is for a read that blocks longer, and
    ``watching`` makes the thread that waits on the camera look at every
    moment a read ends at. The clock
    starts up to a second ahead of the real ``monotonic``, which
    ``_Session.phase_started`` still defaults to (bound at import), so an
    opening deadline counted from that default is still ahead of it.

    A test that waits for frames on it polls with ``new_frames``: a
    ``read(timeout)`` is counted on this clock, which stands still while the
    fake streams.
    """

    def __init__(self, step: float = 1 / 16) -> None:  # exact in binary: sums of steps are exact
        self.step = step
        self.now = float(int(time.monotonic()) + 1)
        self._lock = threading.Lock()
        self._caller = threading.get_ident()  # the thread that opens the camera
        self._looked = threading.Event()
        self._camera: OpenCVCamera | None = None

    def monotonic(self) -> float:
        with self._lock:
            if threading.get_ident() == self._caller:
                self._looked.set()
            return self.now

    def advance(self, seconds: float) -> None:
        with self._lock:
            self.now += seconds
            self._looked.clear()
            camera = self._camera
        session = camera._session if camera is not None else None
        if session is not None:
            with session.cond:
                session.cond.notify_all()
            self._looked.wait(10.0)

    @contextmanager
    def watching(self, camera: OpenCVCamera) -> Iterator[None]:
        """After every advance, wake whoever waits on ``camera`` (``open()`` or ``read()``) and hold the reader until
        the calling thread has looked at the new time. What it decides from that time, such as the safety net of
        ``open()``, is then judged against every moment a read ends at, not whichever one it happens to catch."""
        self._camera = camera
        try:
            yield
        finally:
            with self._lock:
                self._camera = None
            self._looked.set()  # a reader still held goes on


@pytest.fixture
def read_clock(monkeypatch: pytest.MonkeyPatch) -> ReadClock:
    """The camera module on a ReadClock, for a test that waits on a silent camera: see ``ReadClock``."""
    clock = ReadClock()
    monkeypatch.setattr(oc, "time", clock)
    return clock


def new_frames(camera: OpenCVCamera, seconds: float = PATIENCE) -> Iterator[CameraFrame]:
    """Each new frame the camera has, polled, until ``seconds`` of real time have passed.

    For a test on a ReadClock, where ``read(timeout)`` must not be used to wait: its timeout is counted on the
    camera module's ``time``, which moves only while a fake read fails. Once the fake streams, that clock stands
    still, the read waits on the camera's condition with the same time left for ever, and a reader that has
    stalled hangs the test (and the CI job) where it should fail it. So this asks with ``read(0.0)`` and sleeps
    between the asks, and counts ``seconds`` on the real clock: this module's ``time`` is the real one, only
    ``oc.time`` is replaced.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        frame = camera.read(0.0)
        if frame is not None:
            yield frame
        time.sleep(POLL_S)


def assert_streaming(camera: OpenCVCamera) -> None:
    """The camera delivers new frames: two, so the one picture a stream that stopped left unread does not count."""
    assert len(list(islice(new_frames(camera), 2))) == 2, "the camera is not delivering frames"


def assert_took(read_clock: ReadClock, seconds: float, took: float, what: str) -> None:
    """``what`` took ``seconds`` of simulated time, as the camera counts it: it acts on the first read that ends at or
    after ``seconds``, so never sooner and never more than one read of the clock (a step) later."""
    assert seconds <= took <= seconds + read_clock.step, f"{what} took {took} simulated seconds, not {seconds}"


@pytest.fixture(autouse=True)
def no_real_devices(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never enumerate or read the privacy switches of the machine running the tests.

    Media Foundation cannot be asked (as off Windows, or on Windows N), so a
    test that lists nothing opens DirectShow by its own number.
    """
    monkeypatch.setattr(oc, "list_mf_devices", lambda: None)
    monkeypatch.setattr(oc, "list_dshow_devices", lambda: [])
    monkeypatch.setattr(plat, "camera_access_denied", lambda: False)


def listed(monkeypatch: pytest.MonkeyPatch, *names: str, links: bool = False) -> None:
    """What Media Foundation lists, by name; ``list_cameras`` follows from it.

    ``links`` gives each device its symbolic link, as Windows does; without
    one only the friendly names can tell the devices apart.
    """
    devices = [
        oc.MfDevice(index, name, interface_path(name, "mf") if links else None) for index, name in enumerate(names)
    ]
    monkeypatch.setattr(oc, "list_mf_devices", lambda: devices)


def dshow_listed(monkeypatch: pytest.MonkeyPatch, *names: str, paths: bool = True) -> None:
    """What DirectShow's own moniker enumeration lists, by name, with each device's path unless turned off."""
    devices = [
        oc.DshowDevice(index, name, interface_path(name, "dshow") if paths else None)
        for index, name in enumerate(names)
    ]
    monkeypatch.setattr(oc, "list_dshow_devices", lambda: devices)


def make_camera(factory: FakeFactory, spec: str | None = None, **overrides: Any) -> OpenCVCamera:
    options: dict[str, Any] = {
        "backends": ("msmf", "dshow"),
        "open_timeout": 2.0,
        "first_frame_timeout": 1.0,
        "lost_after": 0.2,
        "reopen_delays": (0.01, 0.01, 0.01),
        "fps_window": 0.2,
        "join_timeout": 1.0,
    }
    options.update(overrides)
    return OpenCVCamera(spec, width=1280, height=720, fps=30, capture_factory=factory, **options)


@pytest.fixture
def cameras() -> Iterator[list[OpenCVCamera]]:
    """Cameras to close after the test, whatever it did."""
    made: list[OpenCVCamera] = []
    yield made
    for camera in made:
        camera.close()


def opened(
    cameras: list[OpenCVCamera], factory: FakeFactory, spec: str | None = None, **overrides: Any
) -> OpenCVCamera:
    camera = make_camera(factory, spec, **overrides)
    cameras.append(camera)
    camera.open()
    return camera


# --------------------------------------------------------------------------- spec and names


@pytest.mark.parametrize(("spec", "index"), [(None, 0), ("", 0), ("  ", 0), ("0", 0), ("2", 2), (" 12 ", 12)])
def test_empty_spec_is_the_first_camera_and_digits_are_an_index(spec: str | None, index: int) -> None:
    assert resolve_camera(spec, []) == index


def test_names_match_case_insensitively_exact_name_first() -> None:
    names = [(0, "Integrated Webcam"), (1, "USB Camera 2"), (2, "USB Camera"), (3, "UGREEN Camera")]
    assert resolve_camera("ugreen", names) == 3
    assert resolve_camera("usb camera", names) == 2  # exact beats the earlier partial match
    assert resolve_camera("USB", names) == 1  # partial: the lowest index
    assert resolve_camera("  webcam ", names) == 0


def test_a_name_that_matches_nothing_lists_the_cameras_found() -> None:
    with pytest.raises(CameraError) as error:
        resolve_camera("logitech", [(0, "Integrated Webcam"), (1, "UGREEN Camera")])
    assert error.value.code == "no_camera"
    assert "'UGREEN Camera'" in (error.value.hint or "") and "1 " in (error.value.hint or "")


def test_a_name_with_nothing_listed_gives_the_no_camera_hint() -> None:
    with pytest.raises(CameraError) as error:
        resolve_camera("ugreen", [])
    assert error.value.code == "no_camera" and error.value.hint == plat.no_camera_hint()


def test_non_ascii_digits_are_a_name_not_an_index() -> None:
    with pytest.raises(CameraError):
        resolve_camera("²", [])


def test_a_named_camera_opens_its_index(monkeypatch: pytest.MonkeyPatch, cameras: list[OpenCVCamera]) -> None:
    listed(monkeypatch, "Integrated Webcam", "UGREEN Camera")
    factory = FakeFactory([FakeCapture()])
    info = opened(cameras, factory, "ugreen").info
    assert factory.calls[0][0] == 1
    assert info is not None and (info.index, info.name) == (1, "UGREEN Camera")


def test_a_name_that_matches_nothing_never_touches_a_device(monkeypatch: pytest.MonkeyPatch) -> None:
    listed(monkeypatch, "Integrated Webcam")
    factory = FakeFactory([FakeCapture()])
    with pytest.raises(CameraError) as error:
        make_camera(factory, "ugreen").open()
    assert error.value.code == "no_camera" and factory.calls == []


def test_unknown_names_get_a_numbered_label(cameras: list[OpenCVCamera]) -> None:
    info = opened(cameras, FakeFactory([FakeCapture()]), "3").info
    assert info is not None and (info.index, info.name) == (3, "camera 3")


# --------------------------------------------------------------------------- backends


def test_msmf_opens_first_with_only_the_parameters_it_can_always_set(cameras: list[OpenCVCamera]) -> None:
    capture = FakeCapture(frames=lambda n: picture(n, (8, 12, 3)))
    factory = FakeFactory([capture])
    info = opened(cameras, factory).info
    assert factory.calls == [(0, cv2.CAP_MSMF, MSMF_PARAMS)]
    assert cv2.CAP_PROP_FOURCC not in MSMF_PARAMS[::2] and cv2.CAP_PROP_BUFFERSIZE not in MSMF_PARAMS[::2]
    assert capture.sets == []
    assert info is not None and (info.backend, info.width, info.height, info.fps) == ("msmf", 12, 8, 30.0)


def test_directshow_is_the_fallback_and_gets_mjpg_last(cameras: list[OpenCVCamera]) -> None:
    msmf, dshow = FakeCapture(opened=False), FakeCapture()
    factory = FakeFactory([msmf, dshow])
    info = opened(cameras, factory).info
    assert factory.calls == [(0, cv2.CAP_MSMF, MSMF_PARAMS), (0, cv2.CAP_DSHOW, [])]
    assert msmf.released == 1
    assert dshow.sets == [
        (cv2.CAP_PROP_FPS, 30),
        (cv2.CAP_PROP_FRAME_WIDTH, 1280),
        (cv2.CAP_PROP_FRAME_HEIGHT, 720),
        (cv2.CAP_PROP_FOURCC, cv2.VideoWriter.fourcc(*"MJPG")),
    ]
    assert info is not None and info.backend == "dshow"


@pytest.mark.usefixtures("read_clock")
def test_directshow_is_tried_when_msmf_opens_but_sends_nothing(cameras: list[OpenCVCamera]) -> None:
    # Some UVC webcams open on MSMF and then fail every grab, yet stream on DirectShow.
    msmf, dshow = silent_capture(), FakeCapture()
    factory = FakeFactory([msmf, dshow])
    info = opened(cameras, factory, first_frame_timeout=0.2).info
    assert info is not None and info.backend == "dshow"
    assert [c[:2] for c in factory.calls] == [(0, cv2.CAP_MSMF), (0, cv2.CAP_DSHOW)]
    # The silent MSMF capture is let go before DirectShow asks for the device.
    assert factory.events[:3] == [f"open 0 {cv2.CAP_MSMF}", "release", f"open 0 {cv2.CAP_DSHOW}"]
    assert msmf.released == 1 and dshow.released == 0
    assert next(new_frames(cameras[0]), None) is not None


def test_a_reopen_tries_the_backend_that_streamed_first(read_clock: ReadClock, cameras: list[OpenCVCamera]) -> None:
    factory = FakeFactory(
        [
            silent_capture(),  # MSMF: open but silent
            FakeCapture(frames=fails_after(3)),  # DirectShow streams, then the camera goes away
            FakeCapture(frames=lambda n: picture(100 + n)),  # DirectShow again after the reopen
        ]
    )
    camera = opened(cameras, factory, first_frame_timeout=0.2, lost_after=0.1, reopen_delays=(0.01,))
    for frame in new_frames(camera):
        if frame.image[0, 0, 0] >= 100:
            break
    else:
        pytest.fail("no frame from the reopened camera")
    assert [c[1] for c in factory.calls] == [cv2.CAP_MSMF, cv2.CAP_DSHOW, cv2.CAP_DSHOW]
    info = camera.info
    assert info is not None and info.backend == "dshow"
    # Counted in failed reads: MSMF had its whole first_frame_timeout, and DirectShow, once its stream stopped, its
    # lost_after of silence before the reopen: not a read sooner and not a read later.
    msmf_opened, dshow_opened, reopened = factory.at
    assert_took(read_clock, camera.first_frame_timeout, dshow_opened - msmf_opened, "the wait on MSMF's silence")
    assert_took(read_clock, camera.lost_after, reopened - dshow_opened, "the reopen after the stream stopped")


# -- DirectShow counts devices in its own list ---------------------------------------------


def test_the_two_interfaces_of_one_device_have_the_same_device_instance() -> None:
    assert oc.device_instance(interface_path("UGREEN Camera", "mf")) == oc.device_instance(
        interface_path("UGREEN Camera", "dshow")
    )
    assert oc.device_instance(interface_path("UGREEN Camera", "mf")).startswith("usb#vid_2bdf&pid_0287")
    assert oc.device_instance(interface_path("Integrated Webcam", "mf")) != oc.device_instance(
        interface_path("UGREEN Camera", "dshow")
    )
    # Windows writes these paths in either case, with either prefix, and the trailing \global is optional.
    plain = r"USB#VID_2BDF&PID_0287&MI_00#7&x&0&0000#{65E8773D-8F56-11D0-A3B9-00A0C9223196}"
    assert oc.device_instance(plain) == oc.device_instance(rf"\\.\{plain.lower()}\GLOBAL".replace("GLOBAL", "global"))


@pytest.mark.parametrize("path", [None, "", "   ", "#{65e8773d-8f56-11d0-a3b9-00a0c9223196}"])
def test_a_path_that_names_no_device_is_none(path: str | None) -> None:
    assert oc.device_instance(path) is None


def test_the_device_path_wins_over_the_names() -> None:
    device = oc.MfDevice(1, "UGREEN Camera", interface_path("UGREEN Camera", "mf"))
    dshow = [
        oc.DshowDevice(0, "UGREEN Camera", interface_path("Integrated Webcam", "dshow")),  # same name, other device
        oc.DshowDevice(1, "USB2.0 PC CAMERA", interface_path("UGREEN Camera", "dshow")),
    ]
    assert oc.dshow_position(device, dshow) == 1


def test_the_name_decides_when_only_one_device_has_it() -> None:
    device = oc.MfDevice(0, "UGREEN  camera ")  # no symbolic link, and Windows pads names
    dshow = [oc.DshowDevice(0, "OBS Virtual Camera"), oc.DshowDevice(1, "ugreen  CAMERA")]
    assert oc.dshow_position(device, dshow) == 1
    assert oc.dshow_position(oc.MfDevice(0, "UGREEN Camera"), dshow) is None  # a different name entirely


@pytest.mark.parametrize(
    "dshow",
    [
        [],
        [oc.DshowDevice(0, "USB Camera"), oc.DshowDevice(1, "USB Camera")],  # two of that name: a guess either way
        [oc.DshowDevice(0, ""), oc.DshowDevice(1, "")],  # monikers whose property bag would not open
    ],
)
def test_without_an_unambiguous_match_there_is_no_position(dshow: list[oc.DshowDevice]) -> None:
    assert oc.dshow_position(oc.MfDevice(0, "USB Camera"), dshow) is None
    assert oc.dshow_position(oc.MfDevice(0, ""), dshow) is None


def test_one_device_behind_two_monikers_is_not_chosen_between() -> None:
    path = interface_path("UGREEN Camera", "dshow")
    device = oc.MfDevice(0, "UGREEN Camera", interface_path("UGREEN Camera", "mf"))
    assert oc.dshow_position(device, [oc.DshowDevice(0, "UGREEN Camera", path), oc.DshowDevice(1, "x", path)]) is None


def silent_then_streaming() -> FakeFactory:
    """MSMF opens the camera and sends nothing (the webcams this fallback exists for); DirectShow streams."""
    return FakeFactory([silent_capture(), FakeCapture()])


@pytest.mark.usefixtures("read_clock")
def test_directshow_opens_the_device_where_directshow_counts_it(
    monkeypatch: pytest.MonkeyPatch, cameras: list[OpenCVCamera]
) -> None:
    # DirectShow's list also holds filter-based virtual cameras (OBS and the like) that Media Foundation never
    # lists, so the UGREEN is camera 1 for Media Foundation and camera 2 for DirectShow.
    listed(monkeypatch, "Integrated Webcam", "UGREEN Camera", links=True)
    dshow_listed(monkeypatch, "Integrated Webcam", "OBS Virtual Camera", "UGREEN Camera")
    factory = silent_then_streaming()
    info = opened(cameras, factory, "ugreen", first_frame_timeout=0.2).info
    assert [c[:2] for c in factory.calls] == [(1, cv2.CAP_MSMF), (2, cv2.CAP_DSHOW)]
    # The camera keeps the name and number the user chose it by, which are Media Foundation's.
    assert info is not None and (info.name, info.index, info.backend) == ("UGREEN Camera", 1, "dshow")


@pytest.mark.usefixtures("read_clock")
def test_the_device_path_matches_the_camera_even_when_directshow_calls_it_something_else(
    monkeypatch: pytest.MonkeyPatch, cameras: list[OpenCVCamera]
) -> None:
    listed(monkeypatch, "UGREEN Camera", links=True)
    devices = [
        oc.DshowDevice(0, "OBS Virtual Camera"),
        oc.DshowDevice(1, "USB2.0 PC CAMERA", interface_path("UGREEN Camera", "dshow")),
    ]
    monkeypatch.setattr(oc, "list_dshow_devices", lambda: devices)
    factory = silent_then_streaming()
    info = opened(cameras, factory, "ugreen", first_frame_timeout=0.2).info
    assert [c[:2] for c in factory.calls] == [(0, cv2.CAP_MSMF), (1, cv2.CAP_DSHOW)]
    assert info is not None and info.backend == "dshow"


@pytest.mark.usefixtures("read_clock")
def test_a_unique_friendly_name_is_enough_when_there_is_no_device_path(
    monkeypatch: pytest.MonkeyPatch, cameras: list[OpenCVCamera]
) -> None:
    listed(monkeypatch, "Integrated Webcam", "UGREEN Camera")  # no symbolic link from Media Foundation
    dshow_listed(monkeypatch, "OBS Virtual Camera", "Integrated Webcam", "UGREEN Camera", paths=False)
    factory = silent_then_streaming()
    info = opened(cameras, factory, "ugreen", first_frame_timeout=0.2).info
    assert [c[:2] for c in factory.calls] == [(1, cv2.CAP_MSMF), (2, cv2.CAP_DSHOW)]
    assert info is not None and info.backend == "dshow"


@pytest.mark.usefixtures("read_clock")
def test_directshow_is_skipped_when_nothing_tells_the_device_apart(monkeypatch: pytest.MonkeyPatch) -> None:
    # Two DirectShow devices of that name and no paths to tell them apart: opening one of them would be a guess.
    listed(monkeypatch, "USB Camera")
    dshow_listed(monkeypatch, "USB Camera", "USB Camera", paths=False)
    factory = silent_then_streaming()
    camera = make_camera(factory, "usb camera", first_frame_timeout=0.2)
    with pytest.raises(CameraError) as error:
        camera.open()
    assert [c[1] for c in factory.calls] == [cv2.CAP_MSMF]
    assert error.value.code == "camera_in_use" and "msmf" in error.value.message
    camera.close()


@pytest.mark.usefixtures("read_clock")
def test_directshow_is_skipped_when_it_does_not_list_the_camera_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    listed(monkeypatch, "UGREEN Camera", links=True)
    dshow_listed(monkeypatch, "OBS Virtual Camera")
    factory = silent_then_streaming()
    camera = make_camera(factory, "ugreen", first_frame_timeout=0.2)
    with pytest.raises(CameraError):
        camera.open()
    assert [c[1] for c in factory.calls] == [cv2.CAP_MSMF]
    camera.close()


@pytest.mark.usefixtures("read_clock")
def test_a_directshow_listing_that_fails_skips_directshow_instead_of_crashing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    listed(monkeypatch, "UGREEN Camera", links=True)

    def broken() -> list[oc.DshowDevice]:
        raise OSError("CoCreateInstance failed with HRESULT 0x80004005")

    monkeypatch.setattr(oc, "list_dshow_devices", broken)
    factory = silent_then_streaming()
    camera = make_camera(factory, "ugreen", first_frame_timeout=0.2)
    with caplog.at_level("WARNING", logger=oc.__name__), pytest.raises(CameraError):
        camera.open()
    assert [c[1] for c in factory.calls] == [cv2.CAP_MSMF]
    assert "could not list the DirectShow cameras" in caplog.text
    camera.close()


@pytest.mark.usefixtures("read_clock")
def test_when_media_foundation_cannot_be_asked_directshow_keeps_the_number_it_was_given(
    monkeypatch: pytest.MonkeyPatch, cameras: list[OpenCVCamera]
) -> None:
    # Windows N has no Media Foundation at all: DirectShow's own numbering is all there is, so it is tried as
    # before rather than skipped.
    monkeypatch.setattr(oc, "list_mf_devices", lambda: None)
    asked: list[int] = []

    def never_listed() -> list[oc.DshowDevice]:
        asked.append(1)
        return []

    factory = silent_then_streaming()
    info = opened(cameras, factory, "1", first_frame_timeout=0.2, dshow_devices=never_listed).info
    assert [c[:2] for c in factory.calls] == [(1, cv2.CAP_MSMF), (1, cv2.CAP_DSHOW)]
    assert info is not None and info.backend == "dshow" and asked == []


UGREEN, OBS, WEBCAM = "UGREEN Camera", "OBS Virtual Camera", "Integrated Webcam"


class Machine:
    """Cameras as both backends see them, by name: each backend opens whatever its own list holds at the number
    it is given, the way OpenCV does, so opening the wrong device shows up in ``opened``.

    The UGREEN opens on MSMF and sends nothing (the webcams the DirectShow fallback exists for); everything
    else streams. OBS Virtual Camera is a DirectShow filter: Media Foundation never lists it.
    """

    def __init__(self, mf: list[str], dshow: list[str]) -> None:
        self.mf, self.dshow = list(mf), list(dshow)
        #: ``(name, backend)`` of every device a backend opened.
        self.opened: list[tuple[str, str]] = []
        self.unplugged: set[str] = set()
        #: Media Foundation listings left before ``plug_back`` puts the UGREEN back (None: it stays out).
        self._back_after: int | None = None
        self._back: tuple[int, int] = (0, 0)

    def list_mf(self) -> list[oc.MfDevice]:
        if self._back_after is not None:
            if self._back_after == 0:
                self.plug(UGREEN, *self._back)
                self._back_after = None
            else:
                self._back_after -= 1
        return [oc.MfDevice(i, name, interface_path(name, "mf")) for i, name in enumerate(self.mf)]

    def list_dshow(self) -> list[oc.DshowDevice]:
        return [
            oc.DshowDevice(i, name, None if name == OBS else interface_path(name, "dshow"))
            for i, name in enumerate(self.dshow)
        ]

    def unplug(self, name: str) -> None:
        self.unplugged.add(name)
        self.mf = [n for n in self.mf if n != name]
        self.dshow = [n for n in self.dshow if n != name]

    def plug(self, name: str, mf_at: int, dshow_at: int) -> None:
        self.unplugged.discard(name)
        self.mf.insert(mf_at, name)
        self.dshow.insert(dshow_at, name)

    def plug_back_after(self, listings: int, mf_at: int, dshow_at: int) -> None:
        self._back_after, self._back = listings, (mf_at, dshow_at)

    def factory(self, index: int, api: int, params: Any) -> FakeCapture:
        backend = "msmf" if api == cv2.CAP_MSMF else "dshow"
        names = self.mf if backend == "msmf" else self.dshow
        if index >= len(names):
            return FakeCapture(opened=False)
        name = names[index]
        self.opened.append((name, backend))
        if backend == "msmf" and name == UGREEN:
            return silent_capture()
        return FakeCapture(frames=lambda n: None if name in self.unplugged else picture(n))

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(oc, "list_mf_devices", self.list_mf)
        monkeypatch.setattr(oc, "list_dshow_devices", self.list_dshow)


def machine_camera(machine: Machine, spec: str | None, **overrides: Any) -> OpenCVCamera:
    options: dict[str, Any] = {
        "backends": ("msmf", "dshow"),
        "first_frame_timeout": 0.2,
        "lost_after": 0.1,
        "reopen_delays": (0.01, 0.01, 0.01),
        "join_timeout": 1.0,
    }
    options.update(overrides)
    return OpenCVCamera(spec, capture_factory=machine.factory, **options)


def read_until(camera: OpenCVCamera, done: Callable[[], bool], seconds: float = PATIENCE) -> CameraError | None:
    """Read frames until ``done()`` or the camera raises (returned); fails the test after ``seconds`` of real time.

    Polls with ``read(0.0)`` for the reason given at ``new_frames``: these tests run on a ReadClock.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            camera.read(0.0)
        except CameraError as exc:
            return exc
        if done():
            return None
        time.sleep(POLL_S)
    pytest.fail("the camera neither got there nor gave up")


@pytest.mark.parametrize("spec", [None, "0", "ugreen"])
@pytest.mark.usefixtures("read_clock")
def test_a_reopen_after_the_webcam_is_unplugged_never_opens_another_device_on_directshow(
    monkeypatch: pytest.MonkeyPatch, cameras: list[OpenCVCamera], spec: str | None
) -> None:
    # The webcam streams on DirectShow (MSMF sends it nothing), then it is unplugged: Media Foundation lists no
    # camera and DirectShow's list is down to the OBS filter, now at the number the webcam had. Opening that
    # would stream OBS's placeholder as "UGREEN Camera" for good, and hand control would never see a hand.
    machine = Machine(mf=[UGREEN], dshow=[UGREEN, OBS])
    machine.install(monkeypatch)
    camera = machine_camera(machine, spec)
    cameras.append(camera)
    info = camera.open()
    assert (info.name, info.backend) == (UGREEN, "dshow")
    machine.unplug(UGREEN)
    error = read_until(camera, lambda: (OBS, "dshow") in machine.opened)
    assert (OBS, "dshow") not in machine.opened, "the reopen streams OBS's placeholder as the user's camera"
    assert error is not None and error.code == "camera_lost"
    assert machine.opened == [(UGREEN, "msmf"), (UGREEN, "dshow")]


@pytest.mark.parametrize("spec", [None, "0"])
@pytest.mark.usefixtures("read_clock")
def test_a_webcam_plugged_back_in_is_found_where_directshow_now_counts_it(
    monkeypatch: pytest.MonkeyPatch, cameras: list[OpenCVCamera], spec: str | None
) -> None:
    machine = Machine(mf=[UGREEN], dshow=[UGREEN, OBS])
    machine.install(monkeypatch)
    camera = machine_camera(machine, spec, reopen_delays=(0.01, 0.01, 0.01, 0.01))
    cameras.append(camera)
    camera.open()
    machine.plug_back_after(2, mf_at=0, dshow_at=1)  # two reopens find nothing, then it is back, after OBS
    machine.unplug(UGREEN)
    error = read_until(camera, lambda: (OBS, "dshow") in machine.opened or machine.opened[2:] == [(UGREEN, "dshow")])
    assert (OBS, "dshow") not in machine.opened, "a reopen streamed OBS's placeholder as the user's camera"
    assert error is None and machine.opened == [(UGREEN, "msmf"), (UGREEN, "dshow"), (UGREEN, "dshow")]
    assert_streaming(camera)  # the camera it found streams
    info = camera.info
    assert info is not None and (info.name, info.index, info.backend) == (UGREEN, 0, "dshow")


def test_with_no_camera_plugged_in_directshow_does_not_open_a_virtual_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Media Foundation answers and lists nothing; DirectShow still has the OBS filter at 0. Opening it would
    # report hand control running on "camera 0" instead of saying no camera was found.
    machine = Machine(mf=[], dshow=[OBS])
    machine.install(monkeypatch)
    camera = machine_camera(machine, None)
    with pytest.raises(CameraError) as error:
        camera.open()
    camera.close()
    assert (error.value.code, error.value.message) == ("no_camera", "No camera found.")
    assert machine.opened == []


def test_a_number_media_foundation_does_not_list_is_not_opened_on_directshow(monkeypatch: pytest.MonkeyPatch) -> None:
    machine = Machine(mf=[WEBCAM], dshow=[WEBCAM, OBS])
    machine.install(monkeypatch)
    camera = machine_camera(machine, "1")
    with pytest.raises(CameraError) as error:
        camera.open()
    camera.close()
    assert error.value.code == "no_camera" and "There is no camera 1" in error.value.message
    assert machine.opened == []


@pytest.mark.parametrize("spec", ["ugreen", "1"])
@pytest.mark.usefixtures("read_clock")
def test_a_reopen_while_media_foundation_will_not_answer_maps_the_camera_it_had(
    monkeypatch: pytest.MonkeyPatch, cameras: list[OpenCVCamera], spec: str
) -> None:
    # Media Foundation listed the cameras at open, then fails (COM trouble) when the reopen asks again: the
    # camera is still the device it listed, so DirectShow is matched against that device, never opened at
    # the Media Foundation number (1, which is OBS for DirectShow).
    machine = Machine(mf=[WEBCAM, UGREEN], dshow=[WEBCAM, OBS, UGREEN])
    listings = iter([machine.list_mf()])
    monkeypatch.setattr(oc, "list_mf_devices", lambda: next(listings, None))
    monkeypatch.setattr(oc, "list_dshow_devices", machine.list_dshow)
    capture = FakeCapture(frames=fails_after(3))  # the stream DirectShow opens first, which then stops
    opens: list[tuple[int, int]] = []

    def factory(index: int, api: int, params: Any) -> FakeCapture:
        opens.append((index, api))
        if len(opens) == 2:
            return capture
        return machine.factory(index, api, params)

    camera = OpenCVCamera(
        spec,
        capture_factory=factory,
        backends=("msmf", "dshow"),
        first_frame_timeout=0.2,
        lost_after=0.1,
        reopen_delays=(0.01,),
        join_timeout=1.0,
    )
    cameras.append(camera)
    camera.open()
    error = read_until(camera, lambda: len(opens) > 2)
    assert error is None
    assert opens == [(1, cv2.CAP_MSMF), (2, cv2.CAP_DSHOW), (2, cv2.CAP_DSHOW)]
    assert (OBS, "dshow") not in machine.opened
    assert_streaming(camera)  # the device it reopened streams


def test_a_reopen_maps_the_directshow_index_against_the_current_listing(
    monkeypatch: pytest.MonkeyPatch, read_clock: ReadClock, cameras: list[OpenCVCamera]
) -> None:
    mf = [
        [
            oc.MfDevice(0, "Integrated Webcam", LINKS["Integrated Webcam"]),
            oc.MfDevice(1, "UGREEN Camera", LINKS["UGREEN Camera"]),
        ]
    ]
    dshow = [
        [
            oc.DshowDevice(0, "Integrated Webcam", interface_path("Integrated Webcam", "dshow")),
            oc.DshowDevice(1, "OBS Virtual Camera"),
            oc.DshowDevice(2, "UGREEN Camera", interface_path("UGREEN Camera", "dshow")),
        ]
    ]
    # The integrated webcam is unplugged while hand control runs: both lists lose it and the UGREEN moves up.
    later_mf = [oc.MfDevice(0, "UGREEN Camera", LINKS["UGREEN Camera"])]
    later_dshow = [
        oc.DshowDevice(0, "OBS Virtual Camera"),
        oc.DshowDevice(1, "UGREEN Camera", interface_path("UGREEN Camera", "dshow")),
    ]
    mf_listings, dshow_listings = iter(mf), iter(dshow)
    monkeypatch.setattr(oc, "list_mf_devices", lambda: next(mf_listings, later_mf))
    monkeypatch.setattr(oc, "list_dshow_devices", lambda: next(dshow_listings, later_dshow))
    factory = FakeFactory(
        [
            silent_capture(),  # MSMF: open but silent
            FakeCapture(frames=fails_after(3)),  # DirectShow streams, then the webcam is unplugged
            FakeCapture(frames=lambda n: picture(100 + n)),  # the reopen, DirectShow first: its new position
        ]
    )
    camera = opened(cameras, factory, "ugreen", first_frame_timeout=0.2, lost_after=0.1, reopen_delays=(0.01,))
    for frame in new_frames(camera):
        if frame.image[0, 0, 0] >= 100:
            break
    else:
        pytest.fail("no frame from the reopened camera")
    # The reopen tries the backend that streamed first, and maps the camera onto DirectShow's new numbering.
    assert [c[:2] for c in factory.calls] == [(1, cv2.CAP_MSMF), (2, cv2.CAP_DSHOW), (1, cv2.CAP_DSHOW)]
    info = camera.info
    assert info is not None and (info.index, info.backend) == (0, "dshow")
    _, dshow_opened, reopened = factory.at  # the reopen came lost_after of failed reads after DirectShow's stream began
    assert_took(read_clock, camera.lost_after, reopened - dshow_opened, "the reopen after the stream stopped")


def test_a_backend_that_raises_counts_as_not_opening(cameras: list[OpenCVCamera]) -> None:
    calls: list[int] = []

    def factory(index: int, api: int, params: Any) -> FakeCapture:
        calls.append(api)
        if api == cv2.CAP_MSMF:
            raise cv2.error("MSMF exploded")
        return FakeCapture()

    camera = OpenCVCamera(None, capture_factory=factory, backends=("msmf", "dshow"), first_frame_timeout=1.0)
    cameras.append(camera)
    assert camera.open().backend == "dshow" and calls == [cv2.CAP_MSMF, cv2.CAP_DSHOW]


def test_other_systems_use_cap_any_and_set_the_size(cameras: list[OpenCVCamera]) -> None:
    capture = FakeCapture(backend_name="V4L2")
    factory = FakeFactory([capture])
    info = opened(cameras, factory, backends=("any",)).info
    assert factory.calls == [(0, cv2.CAP_ANY, [])]
    assert capture.sets == [(cv2.CAP_PROP_FRAME_WIDTH, 1280), (cv2.CAP_PROP_FRAME_HEIGHT, 720), (cv2.CAP_PROP_FPS, 30)]
    assert info is not None and info.backend == "v4l2"


@pytest.mark.parametrize(("sys_platform", "backends"), [("win32", ("msmf", "dshow")), ("linux", ("any",))])
def test_default_backends_follow_the_platform(
    monkeypatch: pytest.MonkeyPatch, sys_platform: str, backends: tuple[str, ...]
) -> None:
    monkeypatch.setattr(sys, "platform", sys_platform)
    assert oc.default_backends() == backends


def test_module_import_turns_msmf_hardware_transforms_off() -> None:
    # setdefault: a value the parent process chose wins, so only its presence is certain.
    assert "OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS" in os.environ


def test_create_camera_builds_a_closed_opencv_camera() -> None:
    camera = create_camera("ugreen", width=640, height=480, fps=15)
    assert isinstance(camera, OpenCVCamera)
    assert (camera.spec, camera.width, camera.height, camera.fps) == ("ugreen", 640, 480, 15)
    assert camera.info is None and camera.read(0.0) is None
    camera.close()


# --------------------------------------------------------------------------- frames


def test_reads_skip_to_the_newest_frame(cameras: list[OpenCVCamera]) -> None:
    # The reader is let through a few pictures at a time while the consumer is away, so how many pass between two
    # reads is decided here and not by how fast the runner sleeps: a macOS runner wakes a 2 ms sleep 25 ms late,
    # and a reader paced by sleeps then no longer gets two pictures ahead of a consumer that sleeps 30 ms.
    permits = threading.Semaphore(1)  # the first picture, which open() waits for
    closing = threading.Event()

    def gated(n: int) -> np.ndarray | None:
        while not permits.acquire(timeout=0.05):
            if closing.is_set():
                return None
        return picture(n)

    capture = FakeCapture(frames=gated, delay=0)
    camera = opened(cameras, FakeFactory([capture]))
    frames = [camera.read(1.0)]
    made = 1  # pictures the reader has been let through
    try:
        for let_through in (3, 1, 4):
            for _ in range(let_through):
                permits.release()
            made += let_through
            deadline = time.monotonic() + PATIENCE
            while capture.reads <= made and time.monotonic() < deadline:  # until it waits on the next picture
                time.sleep(0.001)
            frames.append(camera.read(1.0))
    finally:
        closing.set()
    assert all(f is not None for f in frames)
    # Three pictures went by unread, then one, then four: a read returns the newest, and no picture twice.
    assert [f.seq for f in frames if f is not None] == [1, 4, 5, 9]
    for f in frames:
        assert f is not None and int(f.image[0, 0, 0]) == (f.seq - 1) % 256  # the frame that seq names
    assert all(a is not None and b is not None and a.t < b.t for a, b in pairwise(frames))


def test_each_frame_is_returned_once_and_a_read_times_out(
    monkeypatch: pytest.MonkeyPatch, read_clock: ReadClock, cameras: list[OpenCVCamera]
) -> None:
    # On simulated time, which moves as the reader's failed reads do, so "times out" is counted in reads: on a wall
    # clock a runner that oversleeps the wait by hundreds of ms made a read of 0.05 s take half a second.
    camera = opened(cameras, FakeFactory([FakeCapture(frames=fails_after(1), delay=0)]), lost_after=10.0)
    first = camera.read(0.0)  # open() has returned, so its first frame is there: no waiting for it
    assert first is not None and first.seq == 1
    session = camera._session
    assert session is not None
    waits: list[float | None] = []
    wait = session.cond.wait

    def recording_wait(timeout: float | None = None) -> bool:
        waits.append(timeout)
        return wait(timeout)

    monkeypatch.setattr(session.cond, "wait", recording_wait)
    with read_clock.watching(camera):
        started = read_clock.monotonic()
        assert camera.read(0.05) is None
        passed = read_clock.monotonic() - started
    # It answered once its 0.05 s had passed, a few failed reads later, not when the reader gave up (10 s).
    assert 0.05 <= passed <= 5 * read_clock.step
    # watching() wakes the read at every moment a read ends at, so the simulated deadline alone ends it whatever the
    # read waits for. What it waits for must itself be the time it has left: a wait with no timeout, or a hundred
    # times too long, would keep a read waiting on a reader that has gone quiet instead of ending it on its timeout.
    assert all(t is not None and math.isfinite(t) and t <= 0.05 + read_clock.step for t in waits), waits


def test_frames_are_contiguous_bgr_uint8(cameras: list[OpenCVCamera]) -> None:
    camera = opened(cameras, FakeFactory([FakeCapture()]))
    frame = camera.read(1.0)
    assert frame is not None
    assert frame.image.dtype == np.uint8 and frame.image.shape == (4, 6, 3) and frame.image.flags.c_contiguous


def test_grey_and_bgra_frames_become_bgr() -> None:
    grey = np.arange(12, dtype=np.uint8).reshape(3, 4)
    for image in (grey, grey[:, :, None]):
        bgr = as_bgr(image)
        assert bgr is not None and bgr.shape == (3, 4, 3) and bgr.flags.c_contiguous
        assert np.array_equal(bgr[:, :, 0], grey) and np.array_equal(bgr[:, :, 2], grey)
    bgra = np.zeros((3, 4, 4), np.uint8)
    bgra[..., 0], bgra[..., 1], bgra[..., 2], bgra[..., 3] = 10, 20, 30, 255
    bgr = as_bgr(bgra)
    assert bgr is not None and bgr.shape == (3, 4, 3) and bgr[0, 0].tolist() == [10, 20, 30]
    view = np.zeros((4, 6, 3), np.uint8)[:, ::-1]
    bgr = as_bgr(view)
    assert bgr is not None and bgr.flags.c_contiguous


@pytest.mark.parametrize(
    "image",
    [np.zeros((3, 4, 3), np.float32), np.zeros((3, 4, 5), np.uint8), np.zeros((0, 4, 3), np.uint8), None, b"jpeg"],
)
def test_frames_that_cannot_be_bgr_are_refused(image: Any) -> None:
    assert as_bgr(image) is None


def test_a_grey_camera_delivers_bgr(cameras: list[OpenCVCamera]) -> None:
    camera = opened(cameras, FakeFactory([FakeCapture(frames=lambda n: picture(n, (4, 6)))]))
    frame = camera.read(1.0)
    assert frame is not None and frame.image.shape == (4, 6, 3) and frame.image.flags.c_contiguous
    info = camera.info
    assert info is not None and (info.width, info.height) == (6, 4)


def test_the_frame_rate_is_measured_over_its_window() -> None:
    meter = FrameRate(window=2.0)
    assert meter.rate() is None
    for i in range(30):  # just under 1 s at 30 fps: less than half the window
        meter.add(i / 30)
    assert meter.rate() is None
    for i in range(30, 90):
        meter.add(i / 30)
    assert meter.rate() == pytest.approx(30.0)
    t = 89 / 30
    for i in range(1, 61):  # the camera drops to 15 fps; after a full window only those frames count
        meter.add(t + i / 15)
    assert meter.rate() == pytest.approx(15.0)


class FakeClock:
    """``clock.now`` for the camera module: each call is one frame interval at ``fps`` later, from 5000 s."""

    def __init__(self, fps: float = 30) -> None:
        self.fps = fps
        self._lock = threading.Lock()
        self.given: list[float] = []

    def __call__(self) -> float:
        with self._lock:
            self.given.append(5000.0 + len(self.given) / self.fps)
            return self.given[-1]


def test_frames_are_stamped_with_the_helpers_clock(
    monkeypatch: pytest.MonkeyPatch, cameras: list[OpenCVCamera]
) -> None:
    # clock.now (perf_counter), not time.monotonic: on Windows the latter moves in 15.6 ms steps.
    clock = FakeClock()
    monkeypatch.setattr(oc, "clock_now", clock, raising=False)
    camera = opened(cameras, FakeFactory([FakeCapture(delay=0.002)]), fps_window=0.2)
    frames = [camera.read(1.0) for _ in range(3)]
    assert all(f is not None and f.t in clock.given for f in frames), [f and f.t for f in frames]
    time.sleep(0.1)
    info = camera.info
    # The rate comes from the same stamps: one fake frame interval per frame is exactly 30 fps.
    assert info is not None and info.fps == pytest.approx(30.0)


def test_info_reports_the_requested_rate_until_it_is_measured(
    monkeypatch: pytest.MonkeyPatch, cameras: list[OpenCVCamera]
) -> None:
    # Frames are stamped by a simulated 120 fps clock, and the fake reads without sleeping, so the measured rate
    # is exact however slowly the runner runs: a macOS runner oversleeps a 4 ms read by 20-30 ms, which a wall
    # clock measures as ~36 fps, and the ~20 reads it takes to measure a rate then take as long as the runner
    # makes them.
    monkeypatch.setattr(oc, "clock_now", FakeClock(fps=120))
    more = threading.Event()  # only the first picture until open() has answered: nothing measured yet
    capture = FakeCapture(frames=lambda n: picture(n) if n == 0 or more.wait(10.0) else None, delay=0)
    camera = make_camera(FakeFactory([capture]), fps_window=0.3)
    cameras.append(camera)
    assert camera.open().fps == 30.0
    more.set()
    deadline = time.monotonic() + PATIENCE  # measured once about 20 frames span half the window: a few ms of reads
    while (info := camera.info) is not None and info.fps == 30.0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert info is not None and info.fps == 120.0


# --------------------------------------------------------------------------- failures


def test_no_picture_in_time_means_the_camera_is_in_use(read_clock: ReadClock) -> None:
    capture = silent_capture()
    camera = make_camera(FakeFactory([capture]), first_frame_timeout=0.2)
    started = read_clock.now
    with pytest.raises(CameraError) as error:
        camera.open()
    assert read_clock.now - started < camera.open_timeout  # the first_frame_timeout ended it, not the open timeout
    assert error.value.code == "camera_in_use" and error.value.hint == plat.camera_in_use_hint()
    assert error.value.message.endswith("within 0.2 s on msmf.")  # the reader's own verdict, not the caller's net
    assert capture.released == 1 and camera.info is None
    camera.close()


def test_two_silent_backends_mean_the_camera_is_in_use(read_clock: ReadClock) -> None:
    # On simulated time: a macOS runner can wake the reader hundreds of ms late, and on a wall clock that left it
    # still waiting on MSMF when the caller's safety net (twice the wait) gave up, before DirectShow was tried.
    wait = 0.25  # exactly four reads of the clock's 1/16 s
    msmf, dshow = silent_capture(), silent_capture()
    factory = FakeFactory([msmf, dshow])
    camera = make_camera(factory, first_frame_timeout=wait)
    with pytest.raises(CameraError) as error:
        camera.open()
    assert error.value.code == "camera_in_use" and "msmf" in error.value.message and "dshow" in error.value.message
    assert [c[1] for c in factory.calls] == [cv2.CAP_MSMF, cv2.CAP_DSHOW]
    for capture in (msmf, dshow):
        # Each backend had its whole first_frame_timeout, and the reader moved on within one read of it.
        assert wait <= capture.reads * read_clock.step <= wait + read_clock.step
    assert msmf.released == 1 and dshow.released == 1
    camera.close()


def test_a_read_that_blocks_as_long_again_does_not_trip_the_callers_safety_net(read_clock: ReadClock) -> None:
    # The reader gives up on a backend once first_frame_timeout has passed, but a read begun just before that can
    # block about as long again (an MSMF grab waits up to 10 s), so open() allows twice the wait before it decides
    # the reader is stuck. Here MSMF's last read begins one read before the reader's deadline and blocks 0.9 waits
    # more, and open() looks at the clock after every read: a net shorter than that ends open() on MSMF's silence
    # alone, before DirectShow is tried.
    wait = 1.0
    last = int(wait / read_clock.step) - 1  # the read that begins a hair before the reader's deadline

    def msmf_read(n: int) -> None:
        if n == last:
            read_clock.advance(0.9 * wait)

    msmf, dshow = FakeCapture(frames=msmf_read, delay=0), silent_capture()
    factory = FakeFactory([msmf, dshow])
    camera = make_camera(factory, first_frame_timeout=wait)
    with read_clock.watching(camera), pytest.raises(CameraError) as error:
        camera.open()
    assert msmf.reads == last + 1, "MSMF's long read never happened: this test proves nothing"
    assert [c[1] for c in factory.calls] == [cv2.CAP_MSMF, cv2.CAP_DSHOW], "DirectShow was never tried"
    assert error.value.code == "camera_in_use" and "msmf" in error.value.message and "dshow" in error.value.message
    camera.close()


def test_nothing_listed_and_nothing_opening_is_no_camera() -> None:
    factory = FakeFactory([FakeCapture(opened=False), FakeCapture(opened=False)])
    with pytest.raises(CameraError) as error:
        make_camera(factory).open()
    assert error.value.code == "no_camera" and error.value.hint == plat.no_camera_hint()
    assert len(factory.calls) == 2 and all(c.released == 1 for c in factory.made)


def test_an_index_past_the_listed_cameras_is_no_camera(monkeypatch: pytest.MonkeyPatch) -> None:
    listed(monkeypatch, "UGREEN Camera")
    with pytest.raises(CameraError) as error:
        make_camera(FakeFactory(), "3").open()
    assert error.value.code == "no_camera" and "'UGREEN Camera'" in (error.value.hint or "")


def test_a_listed_camera_that_no_backend_opens_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    listed(monkeypatch, "UGREEN Camera")
    with pytest.raises(CameraError) as error:
        make_camera(FakeFactory()).open()
    assert error.value.code == "camera_blocked" and error.value.hint == plat.camera_blocked_hint()


def test_the_privacy_switch_reports_blocked_even_when_nothing_is_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plat, "camera_access_denied", lambda: True)
    with pytest.raises(CameraError) as error:
        make_camera(FakeFactory()).open()
    assert error.value.code == "camera_blocked"


def test_a_failing_privacy_check_does_not_hide_the_real_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> bool:
        raise OSError("registry unreadable")

    monkeypatch.setattr(plat, "camera_access_denied", broken)
    with pytest.raises(CameraError) as error:
        make_camera(FakeFactory()).open()
    assert error.value.code == "no_camera"


def test_a_run_of_failed_reads_reopens_the_camera(read_clock: ReadClock, cameras: list[OpenCVCamera]) -> None:
    first = FakeCapture(frames=fails_after(3))
    second = FakeCapture(frames=lambda n: picture(100 + n))
    factory = FakeFactory([first, second])
    camera = opened(cameras, factory, lost_after=0.1, reopen_delays=(0.01,))
    seqs = []
    for frame in new_frames(camera):
        seqs.append(frame.seq)
        if frame.image[0, 0, 0] >= 100:
            break
    else:
        pytest.fail("no frame from the reopened camera")
    assert seqs == sorted(set(seqs)) and seqs[-1] > 3  # numbering goes on across the reopen
    assert [c[:2] for c in factory.calls] == [(0, cv2.CAP_MSMF), (0, cv2.CAP_MSMF)]
    # Released before it was opened again (one device, one owner).
    assert factory.events[:3] == [f"open 0 {cv2.CAP_MSMF}", "release", f"open 0 {cv2.CAP_MSMF}"]
    assert first.released == 1 and camera.info is not None
    # The simulated clock moves only on failed reads: the camera was reopened once those had added up to lost_after.
    opened_first, reopened = factory.at
    assert_took(read_clock, camera.lost_after, reopened - opened_first, "reopening a camera that stopped sending")


def test_repeated_reopen_failures_lose_the_camera(read_clock: ReadClock, cameras: list[OpenCVCamera]) -> None:
    factory = FakeFactory([FakeCapture(frames=fails_after(2))])
    camera = opened(cameras, factory, lost_after=0.1)
    with pytest.raises(CameraError) as error:
        for _ in new_frames(camera):
            pass
    assert error.value.code == "camera_lost" and error.value.hint
    assert len(factory.calls) == 1 + 3 * 2  # three reopens, each trying MSMF then DirectShow
    # The reopens read nothing, so the clock stopped at the failed read that made the camera give up on the stream:
    # it did so once those had added up to lost_after, not sooner and not later.
    assert_took(read_clock, camera.lost_after, read_clock.now - factory.at[0], "losing a camera that stopped sending")
    assert all(c.released == 1 for c in factory.made)
    assert camera.info is None
    with pytest.raises(CameraError):
        camera.read(0.0)


def test_a_read_waiting_while_the_reader_gives_up_raises_instead_of_going_quiet(read_clock: ReadClock) -> None:
    # A read that started before the reader gave up must learn why: otherwise it returns None for ever, the
    # runtime keeps asking for frames that never come and nothing tells the user why the camera stopped.
    capture = silent_capture()
    camera = make_camera(FakeFactory([capture]), backends=("msmf",), first_frame_timeout=0.3)
    errors: list[CameraError] = []
    reads: list[CameraFrame | None] = []

    def open_it() -> None:
        try:
            camera.open()
        except CameraError as exc:
            errors.append(exc)

    opener = threading.Thread(target=open_it)
    with read_clock.watching(camera):  # the reader goes on only as this thread looks: it is in read() all the while
        opener.start()
        deadline = time.monotonic() + PATIENCE
        while camera._session is None and time.monotonic() < deadline:  # until open() has started the reader
            time.sleep(0.001)
        try:
            reads.append(camera.read(3.0))
        except CameraError as exc:
            errors.append(exc)
    opener.join(PATIENCE)
    assert not opener.is_alive()
    assert reads == [], "the read must not report 'no frame yet' when the camera is not coming back"
    assert len(errors) == 2 and {e.code for e in errors} == {"camera_in_use"}
    assert all(e.hint == plat.camera_in_use_hint() for e in errors)
    camera.close()


def test_a_reader_that_gave_up_without_saying_why_is_still_camera_lost(cameras: list[OpenCVCamera]) -> None:
    camera = opened(cameras, FakeFactory([FakeCapture(frames=fails_after(2))]), lost_after=0.1)
    deadline = time.monotonic() + PATIENCE
    with pytest.raises(CameraError):
        while time.monotonic() < deadline:
            camera.read(0.2)
    session = camera._session  # a reader that ended without recording an error must still raise
    assert session is not None
    with session.cond:
        session.error = None
    with pytest.raises(CameraError) as error:
        camera.read(0.0)
    assert error.value.code == "camera_lost" and error.value.hint == oc.LOST_HINT


def test_a_camera_chosen_by_name_is_looked_up_again_on_reopen(
    monkeypatch: pytest.MonkeyPatch, cameras: list[OpenCVCamera]
) -> None:
    first = [oc.MfDevice(0, "Integrated Webcam"), oc.MfDevice(1, "UGREEN Camera")]
    later = [oc.MfDevice(0, "UGREEN Camera")]  # the integrated webcam is gone: the UGREEN moved to 0
    listings = iter([first])
    monkeypatch.setattr(oc, "list_mf_devices", lambda: next(listings, later))
    factory = FakeFactory([FakeCapture(frames=fails_after(2)), FakeCapture(frames=lambda n: picture(100 + n))])
    camera = opened(cameras, factory, "ugreen", lost_after=0.1, reopen_delays=(0.01,))
    deadline = time.monotonic() + PATIENCE
    while time.monotonic() < deadline:
        frame = camera.read(0.2)
        if frame is not None and frame.image[0, 0, 0] >= 100:
            break
    assert [c[0] for c in factory.calls] == [1, 0]
    info = camera.info
    assert info is not None and info.index == 0


# --------------------------------------------------------------------------- lifecycle


def test_close_is_idempotent_and_releases_the_device_on_the_reader_thread(cameras: list[OpenCVCamera]) -> None:
    capture = FakeCapture()
    camera = opened(cameras, FakeFactory([capture]))
    assert camera.read(1.0) is not None
    camera.close()
    camera.close()
    assert capture.released == 1
    assert camera.info is None and camera.read(0.01) is None
    # Opened, read and released on one thread of its own, which has ended.
    assert len(capture.threads) == 1 and threading.get_ident() not in capture.threads
    assert not any(t.ident in capture.threads for t in threading.enumerate())


def test_open_twice_keeps_the_running_camera(cameras: list[OpenCVCamera]) -> None:
    factory = FakeFactory([FakeCapture()])
    camera = opened(cameras, factory)
    assert camera.open().backend == "msmf" and len(factory.calls) == 1


def test_open_after_close_starts_numbering_again(cameras: list[OpenCVCamera]) -> None:
    camera = opened(cameras, FakeFactory([FakeCapture(), FakeCapture()]))
    frame = camera.read(1.0)
    time.sleep(0.02)
    frame = camera.read(1.0)
    assert frame is not None and frame.seq > 1
    camera.close()
    camera.open()
    frame = camera.read(1.0)
    assert frame is not None and frame.seq == 1


def test_close_while_opening_returns_promptly() -> None:
    capture = silent_capture()
    camera = make_camera(FakeFactory([capture]), first_frame_timeout=5.0)
    errors: list[BaseException] = []

    def open_it() -> None:
        try:
            camera.open()
        except CameraError as exc:
            errors.append(exc)

    opener = threading.Thread(target=open_it)
    opener.start()
    deadline = time.monotonic() + PATIENCE
    while capture.reads == 0 and time.monotonic() < deadline:  # until the reader is waiting for a first picture
        time.sleep(0.001)
    started = time.monotonic()
    camera.close()
    opener.join(PATIENCE)
    assert not opener.is_alive() and time.monotonic() - started < 2.0  # not the 5 s first_frame_timeout
    assert len(errors) == 1 and capture.released == 1


def test_close_never_raises(cameras: list[OpenCVCamera]) -> None:
    capture = FakeCapture()

    def bad_release() -> None:
        raise RuntimeError("driver gone")

    capture.release = bad_release  # type: ignore[method-assign]
    camera = opened(cameras, FakeFactory([capture]))
    camera.close()
    camera.close()


# --------------------------------------------------------------------------- Media Foundation listing

HR = ctypes.c_int32
E_FAIL = 0x80004005 - (1 << 32)
SOURCE_TYPE = "c60ac5fe-252a-478f-a0ef-bc8fa5f7cad3"
VIDCAP = "8ac3587a-4ae7-42d8-99e0-0a6013eef90f"
FRIENDLY_NAME = "60d0e559-52f8-4fa2-bbce-acdb34a8ec01"
SYMBOLIC_LINK = "58f0aad8-22bf-4f8a-bb3d-d2c4978c6e2f"


class FakeMediaFoundation:
    """MF's enumeration calls as C callbacks over COM objects laid out like real ones (a vtable pointer first).

    The enumeration code runs unchanged against it: slot arithmetic, GUIDs by
    reference, out-pointers, CoTaskMem strings and reference counts are real
    memory operations, so a wrong slot or a leak shows up here, not on Windows.
    """

    def __init__(self, names: list[str], *, init_hr: int = 0, enum_hr: int = 0) -> None:
        self.names, self.init_hr, self.enum_hr = names, init_hr, enum_hr
        self.calls: list[str] = []
        self.refs: dict[int, int] = {}
        self.allocated: dict[int, Any] = {}
        self.set_guids: list[tuple[bytes, bytes]] = []
        self.wrong_slots: list[int] = []
        #: Exceptions inside callbacks (ctypes would only print them) and frees of memory never handed out.
        self.errors: list[str] = []
        self._names: dict[int, str] = {}
        self._keep: list[Any] = []
        self._vtable = (ctypes.c_void_p * 36)()
        for slot in range(36):
            self._vtable[slot] = self._fn(ctypes.CFUNCTYPE(HR, ctypes.c_void_p), self._trap(slot))
        self._vtable[oc.SLOT_RELEASE] = self._fn(ctypes.CFUNCTYPE(ctypes.c_uint32, ctypes.c_void_p), self._release)
        self._vtable[oc.SLOT_SET_GUID] = self._fn(
            ctypes.CFUNCTYPE(HR, ctypes.c_void_p, ctypes.POINTER(oc.GUID), ctypes.POINTER(oc.GUID)), self._set_guid
        )
        self._vtable[oc.SLOT_GET_ALLOCATED_STRING] = self._fn(
            ctypes.CFUNCTYPE(
                HR,
                ctypes.c_void_p,
                ctypes.POINTER(oc.GUID),
                ctypes.POINTER(ctypes.c_void_p),
                ctypes.POINTER(ctypes.c_uint32),
            ),
            self._get_allocated_string,
        )

    def _guard(self, function: Callable[..., Any], default: Any = E_FAIL) -> Callable[..., Any]:
        def guarded(*args: Any) -> Any:
            try:
                return function(*args)
            except Exception as exc:  # noqa: BLE001 - reported through self.errors
                self.errors.append(f"{function.__name__}: {exc!r}")
                return default

        return guarded

    def _fn(self, prototype: Any, function: Callable[..., Any]) -> int:
        thunk = prototype(self._guard(function, 0 if function == self._release else E_FAIL))
        self._keep.append(thunk)
        return ctypes.cast(thunk, ctypes.c_void_p).value or 0

    def _trap(self, slot: int) -> Callable[[int], int]:
        def trap(this: int) -> int:
            self.wrong_slots.append(slot)
            return E_FAIL

        return trap

    def _new_object(self, name: str | None = None) -> int:
        obj = (ctypes.c_void_p * 1)(ctypes.addressof(self._vtable))
        self._keep.append(obj)
        address = ctypes.addressof(obj)
        self.refs[address] = 1
        if name is not None:
            self._names[address] = name
        return address

    # -- methods
    def _release(self, this: int) -> int:
        self.refs[this] -= 1
        return self.refs[this]

    def _set_guid(self, this: int, key: Any, value: Any) -> int:
        self.set_guids.append((bytes(key.contents), bytes(value.contents)))
        return 0

    def _get_allocated_string(self, this: int, key: Any, out: Any, length: Any) -> int:
        name = self._names[this]
        if bytes(key.contents) == uuid.UUID(SYMBOLIC_LINK).bytes_le:
            name = LINKS.get(name, "")
            if not name:
                return E_FAIL  # a device whose symbolic link Media Foundation will not give
        elif bytes(key.contents) != uuid.UUID(FRIENDLY_NAME).bytes_le:
            return E_FAIL
        buffer = ctypes.create_unicode_buffer(name)
        self.allocated[ctypes.addressof(buffer)] = buffer
        out[0] = ctypes.addressof(buffer)
        length[0] = len(name)
        return 0

    # -- functions
    def api(self) -> oc.MediaFoundation:
        c = ctypes.CFUNCTYPE
        g = self._guard
        functions = {
            "co_initialize_ex": c(HR, ctypes.c_void_p, ctypes.c_uint32)(g(self._co_initialize_ex)),
            "co_uninitialize": c(None)(g(self._co_uninitialize, None)),
            "co_task_mem_free": c(None, ctypes.c_void_p)(g(self._co_task_mem_free, None)),
            "mf_startup": c(HR, ctypes.c_uint32, ctypes.c_uint32)(g(self._mf_startup)),
            "mf_shutdown": c(HR)(g(self._mf_shutdown)),
            "mf_create_attributes": c(HR, ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint32)(g(self._create_attributes)),
            "mf_enum_device_sources": c(
                HR, ctypes.c_void_p, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)), ctypes.POINTER(ctypes.c_uint32)
            )(g(self._enum_device_sources)),
        }
        self._keep.extend(functions.values())
        return oc.MediaFoundation(functype=ctypes.CFUNCTYPE, **functions)

    def _co_initialize_ex(self, reserved: int | None, mode: int) -> int:
        self.calls.append(f"CoInitializeEx {mode}")
        return self.init_hr

    def _co_uninitialize(self) -> None:
        self.calls.append("CoUninitialize")

    def _co_task_mem_free(self, address: int | None) -> None:
        self.calls.append("CoTaskMemFree")
        if address is not None and self.allocated.pop(address, None) is None:
            self.errors.append(f"CoTaskMemFree of {address:#x}, which was never allocated")

    def _mf_shutdown(self) -> int:
        self.calls.append("MFShutdown")
        return 0

    def _mf_startup(self, version: int, flags: int) -> int:
        self.calls.append(f"MFStartup {version:#x} {flags}")
        return 0

    def _create_attributes(self, out: Any, size: int) -> int:
        out[0] = self._new_object()
        return 0

    def _enum_device_sources(self, attributes: int, out: Any, count: Any) -> int:
        self.calls.append("MFEnumDeviceSources")
        if self.enum_hr:
            return self.enum_hr
        objects = [self._new_object(name) for name in self.names]
        array = (ctypes.c_void_p * max(1, len(objects)))(*objects)
        self.allocated[ctypes.addressof(array)] = array
        out[0] = ctypes.cast(array, ctypes.POINTER(ctypes.c_void_p))
        count[0] = len(objects)
        return 0


def test_guids_have_the_windows_memory_layout() -> None:
    assert ctypes.sizeof(oc.GUID) == 16
    for text, guid in [
        (SOURCE_TYPE, oc.MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE),
        (VIDCAP, oc.MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE_VIDCAP_GUID),
        (FRIENDLY_NAME, oc.MF_DEVSOURCE_ATTRIBUTE_FRIENDLY_NAME),
    ]:
        assert bytes(guid) == uuid.UUID(text).bytes_le


def test_media_foundation_lists_names_in_order_and_frees_everything() -> None:
    fake = FakeMediaFoundation(["UGREEN Camera", "Caméra intégrée"])
    assert oc.enumerate_msmf(fake.api()) == [
        oc.MfDevice(0, "UGREEN Camera", LINKS["UGREEN Camera"]),
        oc.MfDevice(1, "Caméra intégrée", LINKS["Caméra intégrée"]),
    ]
    assert fake.set_guids == [(uuid.UUID(SOURCE_TYPE).bytes_le, uuid.UUID(VIDCAP).bytes_le)]
    assert fake.wrong_slots == [] and fake.errors == []
    assert fake.allocated == {}, "every string and the device array go back to CoTaskMemFree"
    assert set(fake.refs.values()) == {0}, "the attributes and every activation object are released"
    assert fake.calls[:2] == ["CoInitializeEx 0", "MFStartup 0x20070 1"]
    assert fake.calls[-2:] == ["MFShutdown", "CoUninitialize"]


def test_media_foundation_with_no_cameras_is_an_empty_list() -> None:
    fake = FakeMediaFoundation([])
    assert oc.enumerate_msmf(fake.api()) == []
    assert fake.allocated == {} and set(fake.refs.values()) == {0} and fake.errors == []


def test_a_failed_enumeration_still_shuts_everything_down() -> None:
    fake = FakeMediaFoundation(["UGREEN Camera"], enum_hr=E_FAIL)
    with pytest.raises(OSError, match="MFEnumDeviceSources"):
        oc.enumerate_msmf(fake.api())
    assert set(fake.refs.values()) == {0} and fake.errors == []
    assert fake.calls[-2:] == ["MFShutdown", "CoUninitialize"]


def test_com_set_up_by_someone_else_is_left_alone() -> None:
    fake = FakeMediaFoundation(["UGREEN Camera"], init_hr=oc.RPC_E_CHANGED_MODE)
    assert [d.name for d in oc.enumerate_msmf(fake.api())] == ["UGREEN Camera"]
    assert "CoUninitialize" not in fake.calls and fake.errors == []


@pytest.mark.skipif(sys.platform == "win32", reason="Windows lists its cameras")
def test_list_cameras_is_empty_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()  # the real function, not the autouse stand-in
    assert oc.list_cameras() == []
    assert oc.list_mf_devices() is None, "off Windows Media Foundation cannot be asked; it did not say 'none'"


@pytest.mark.skipif(sys.platform != "win32", reason="Media Foundation is Windows-only")
def test_list_cameras_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()
    cameras = oc.list_cameras()  # a CI runner has none; it must still not raise
    assert isinstance(cameras, list)
    assert all(isinstance(i, int) and isinstance(name, str) and name for i, name in cameras)
    assert [i for i, _ in cameras] == list(range(len(cameras)))


# --------------------------------------------------------------------------- DirectShow's device monikers

S_FALSE = 1
DEVICE_ENUM_CLSID = "62be5d10-60eb-11d0-bd3b-00a0c911ce86"
CREATE_DEV_ENUM_IID = "29840822-5b84-11d0-bd3b-00a0c911ce86"
VIDEO_INPUT_CATEGORY = "860bb310-5d01-11d0-bd3b-00a0c911ce86"
PROPERTY_BAG_IID = "55272a00-42cb-11ce-8135-00aa004bb851"
VT_I4 = 3


@dataclass
class FakeMoniker:
    """One device moniker: the properties its bag holds (an int value is a non-string property)."""

    properties: dict[str, str | int] = field(default_factory=dict)
    #: False for a moniker whose BindToStorage fails, as a filter that is registered but broken does.
    bindable: bool = True


class FakeDirectShow:
    """DirectShow's moniker enumeration as C callbacks over COM objects laid out like real ones.

    Like ``FakeMediaFoundation``: one vtable per interface with every other
    slot trapped, GUIDs by reference, out-pointers, BSTRs that have to go back
    through VariantClear and reference counts. A wrong slot, a leak or a
    missed release shows up here instead of on Windows.
    """

    SLOTS = 24

    def __init__(
        self,
        monikers: list[FakeMoniker],
        *,
        init_hr: int = 0,
        instance_hr: int = 0,
        enumerator_hr: int = 0,
        endless: bool = False,
    ) -> None:
        self.monikers, self.init_hr = monikers, init_hr
        self.instance_hr, self.enumerator_hr = instance_hr, enumerator_hr
        self.endless = endless
        self.calls: list[str] = []
        self.guids: list[str] = []
        self.refs: dict[int, int] = {}
        #: BSTRs handed out and not yet freed through VariantClear.
        self.allocated: dict[int, Any] = {}
        self.wrong_slots: list[tuple[str, int]] = []
        self.errors: list[str] = []
        self.bound = 0
        self._of: dict[int, FakeMoniker] = {}
        self._handed: list[FakeMoniker] = []
        self._keep: list[Any] = []
        c, p = ctypes.CFUNCTYPE, ctypes.POINTER
        self._vtables = {
            "devenum": self._vtable(
                "devenum",
                {
                    oc.SLOT_CREATE_CLASS_ENUMERATOR: (
                        c(HR, ctypes.c_void_p, p(oc.GUID), p(ctypes.c_void_p), ctypes.c_uint32),
                        self._create_class_enumerator,
                    )
                },
            ),
            "enum": self._vtable(
                "enum",
                {
                    oc.SLOT_ENUM_NEXT: (
                        c(HR, ctypes.c_void_p, ctypes.c_uint32, p(ctypes.c_void_p), p(ctypes.c_uint32)),
                        self._next,
                    )
                },
            ),
            "moniker": self._vtable(
                "moniker",
                {
                    oc.SLOT_BIND_TO_STORAGE: (
                        c(HR, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, p(oc.GUID), p(ctypes.c_void_p)),
                        self._bind_to_storage,
                    )
                },
            ),
            "bag": self._vtable(
                "bag",
                {
                    oc.SLOT_PROPERTY_BAG_READ: (
                        c(HR, ctypes.c_void_p, ctypes.c_wchar_p, p(oc.VARIANT), ctypes.c_void_p),
                        self._read,
                    )
                },
            ),
        }

    # -- the C side
    def _guard(self, function: Callable[..., Any], default: Any = E_FAIL) -> Callable[..., Any]:
        def guarded(*args: Any) -> Any:
            try:
                return function(*args)
            except Exception as exc:  # noqa: BLE001 - ctypes would only print it; reported through self.errors
                self.errors.append(f"{function.__name__}: {exc!r}")
                return default

        return guarded

    def _thunk(self, prototype: Any, function: Callable[..., Any], default: Any = E_FAIL) -> int:
        made = prototype(self._guard(function, default))
        self._keep.append(made)
        return ctypes.cast(made, ctypes.c_void_p).value or 0

    def _vtable(self, kind: str, methods: dict[int, tuple[Any, Callable[..., Any]]]) -> Any:
        table = (ctypes.c_void_p * self.SLOTS)()
        for slot in range(self.SLOTS):
            table[slot] = self._thunk(ctypes.CFUNCTYPE(HR, ctypes.c_void_p), self._trap(kind, slot))
        table[oc.SLOT_RELEASE] = self._thunk(
            ctypes.CFUNCTYPE(ctypes.c_uint32, ctypes.c_void_p), self._release, default=0
        )
        for slot, (prototype, function) in methods.items():
            table[slot] = self._thunk(prototype, function)
        self._keep.append(table)
        return table

    def _trap(self, kind: str, slot: int) -> Callable[[int], int]:
        def trap(this: int) -> int:
            self.wrong_slots.append((kind, slot))
            return E_FAIL

        return trap

    def _new_object(self, kind: str, of: FakeMoniker | None = None) -> int:
        obj = (ctypes.c_void_p * 1)(ctypes.addressof(self._vtables[kind]))
        self._keep.append(obj)
        address = ctypes.addressof(obj)
        self.refs[address] = 1
        if of is not None:
            self._of[address] = of
        return address

    def _release(self, this: int) -> int:
        self.refs[this] -= 1
        return self.refs[this]

    # -- the interfaces
    def _create_class_enumerator(self, this: int, category: Any, out: Any, flags: int) -> int:
        self.guids.append(str(uuid.UUID(bytes_le=bytes(category.contents))))
        self.calls.append(f"CreateClassEnumerator {flags}")
        if self.enumerator_hr:
            return self.enumerator_hr
        if not self.monikers and not self.endless:
            return S_FALSE  # the category is empty: no capture device at all
        out[0] = self._new_object("enum")
        return 0

    def _next(self, this: int, count: int, out: Any, fetched: Any) -> int:
        assert count == 1, "one moniker at a time, as videoInput does"
        if self.endless:
            moniker = FakeMoniker({"FriendlyName": f"camera {len(self._handed)}"})
        elif len(self._handed) >= len(self.monikers):
            fetched[0] = 0
            return S_FALSE
        else:
            moniker = self.monikers[len(self._handed)]
        self._handed.append(moniker)
        out[0] = self._new_object("moniker", moniker)
        fetched[0] = 1
        return 0

    def _bind_to_storage(self, this: int, context: int | None, left: int | None, iid: Any, out: Any) -> int:
        assert context is None and left is None
        self.guids.append(str(uuid.UUID(bytes_le=bytes(iid.contents))))
        self.bound += 1
        if not self._of[this].bindable:
            return E_FAIL
        out[0] = self._new_object("bag", self._of[this])
        return 0

    def _read(self, this: int, name: str, variant: Any, errors: int | None) -> int:
        assert errors is None
        value = self._of[this].properties.get(name)
        if value is None:
            return E_FAIL
        if isinstance(value, int):
            variant.contents.vt, variant.contents.value = VT_I4, value
            return 0
        buffer = ctypes.create_unicode_buffer(value)
        self._keep.append(buffer)
        self.allocated[ctypes.addressof(buffer)] = buffer
        variant.contents.vt, variant.contents.value = oc.VT_BSTR, ctypes.addressof(buffer)
        return 0

    # -- the functions
    def api(self) -> oc.DirectShow:
        c, g = ctypes.CFUNCTYPE, self._guard
        functions = {
            "co_initialize_ex": c(HR, ctypes.c_void_p, ctypes.c_uint32)(g(self._co_initialize_ex)),
            "co_uninitialize": c(None)(g(self._co_uninitialize, None)),
            "co_create_instance": c(
                HR,
                ctypes.POINTER(oc.GUID),
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.POINTER(oc.GUID),
                ctypes.POINTER(ctypes.c_void_p),
            )(g(self._co_create_instance)),
            "variant_clear": c(HR, ctypes.POINTER(oc.VARIANT))(g(self._variant_clear)),
        }
        self._keep.extend(functions.values())
        return oc.DirectShow(functype=ctypes.CFUNCTYPE, **functions)

    def _co_initialize_ex(self, reserved: int | None, mode: int) -> int:
        self.calls.append(f"CoInitializeEx {mode}")
        return self.init_hr

    def _co_uninitialize(self) -> None:
        self.calls.append("CoUninitialize")

    def _co_create_instance(self, clsid: Any, outer: int | None, context: int, iid: Any, out: Any) -> int:
        assert outer is None and context == oc.CLSCTX_INPROC_SERVER
        self.guids.extend(str(uuid.UUID(bytes_le=bytes(g.contents))) for g in (clsid, iid))
        self.calls.append("CoCreateInstance")
        if self.instance_hr:
            return self.instance_hr
        out[0] = self._new_object("devenum")
        return 0

    def _variant_clear(self, variant: Any) -> int:
        address = variant.contents.value
        if variant.contents.vt == oc.VT_BSTR and self.allocated.pop(address, None) is None:
            self.errors.append(f"VariantClear of {address:#x}, which was never allocated")
        variant.contents.vt, variant.contents.value = 0, None
        return 0


def dshow_moniker(name: str | None, *, path: str | None = None, description: str | None = None) -> FakeMoniker:
    properties: dict[str, str | int] = {}
    if name is not None:
        properties["FriendlyName"] = name
    if description is not None:
        properties["Description"] = description
    if path is not None:
        properties["DevicePath"] = path
    return FakeMoniker(properties)


def test_the_directshow_guids_and_the_variant_layout_match_windows() -> None:
    for text, guid in [
        (DEVICE_ENUM_CLSID, oc.CLSID_SYSTEM_DEVICE_ENUM),
        (CREATE_DEV_ENUM_IID, oc.IID_CREATE_DEV_ENUM),
        (VIDEO_INPUT_CATEGORY, oc.CLSID_VIDEO_INPUT_DEVICE_CATEGORY),
        (PROPERTY_BAG_IID, oc.IID_PROPERTY_BAG),
    ]:
        assert bytes(guid) == uuid.UUID(text).bytes_le
    wide = ctypes.sizeof(ctypes.c_void_p) == 8
    assert ctypes.sizeof(oc.VARIANT) == (24 if wide else 16)
    assert oc.VARIANT.value.offset == 8


def test_directshow_lists_the_monikers_in_order_and_frees_everything() -> None:
    webcam, ugreen = interface_path("Integrated Webcam", "dshow"), interface_path("UGREEN Camera", "dshow")
    fake = FakeDirectShow(
        [
            dshow_moniker("Integrated Webcam", path=webcam),
            dshow_moniker("OBS Virtual Camera"),  # a software filter: a name, no device path
            dshow_moniker(None, description="UGREEN Camera", path=ugreen),  # no FriendlyName: the Description
        ]
    )
    assert oc.enumerate_dshow(fake.api()) == [
        oc.DshowDevice(0, "Integrated Webcam", webcam),
        oc.DshowDevice(1, "OBS Virtual Camera", None),
        oc.DshowDevice(2, "UGREEN Camera", ugreen),
    ]
    assert fake.wrong_slots == [] and fake.errors == []
    assert fake.allocated == {}, "every BSTR goes back through VariantClear"
    assert set(fake.refs.values()) == {0}, "the enumerator, every moniker and every property bag are released"
    assert fake.guids == [
        DEVICE_ENUM_CLSID,
        CREATE_DEV_ENUM_IID,
        VIDEO_INPUT_CATEGORY,
        *[PROPERTY_BAG_IID] * 3,
    ]
    assert fake.calls[:2] == ["CoInitializeEx 0", "CoCreateInstance"] and fake.calls[-1] == "CoUninitialize"


def test_a_moniker_whose_property_bag_will_not_open_still_takes_its_number() -> None:
    # videoInput::getDevice binds the n-th moniker it walks past, counting the ones it cannot read too.
    path = interface_path("UGREEN Camera", "dshow")
    fake = FakeDirectShow([FakeMoniker(bindable=False), dshow_moniker("UGREEN Camera", path=path)])
    assert oc.enumerate_dshow(fake.api()) == [oc.DshowDevice(0, ""), oc.DshowDevice(1, "UGREEN Camera", path)]
    assert fake.bound == 2 and set(fake.refs.values()) == {0} and fake.errors == []


def test_a_property_that_is_not_a_string_is_no_name() -> None:
    fake = FakeDirectShow([FakeMoniker({"FriendlyName": 42, "DevicePath": 7})])
    assert oc.enumerate_dshow(fake.api()) == [oc.DshowDevice(0, "", None)]
    assert fake.allocated == {} and fake.errors == []


def test_an_empty_video_input_category_is_an_empty_list() -> None:
    fake = FakeDirectShow([])
    assert oc.enumerate_dshow(fake.api()) == []
    assert set(fake.refs.values()) == {0} and fake.errors == []
    assert fake.calls[-1] == "CoUninitialize"


def test_the_number_of_monikers_read_is_bounded() -> None:
    # A filter that keeps answering Next must not spin: OpenCV's DirectShow backend cannot reach past
    # VI_MAX_CAMERAS anyway.
    fake = FakeDirectShow([], endless=True)
    assert len(oc.enumerate_dshow(fake.api())) == oc.MAX_DSHOW_DEVICES
    assert set(fake.refs.values()) == {0} and fake.errors == []


@pytest.mark.parametrize("failure", ["instance_hr", "enumerator_hr"])
def test_a_failed_directshow_enumeration_raises_and_undoes_com(failure: str) -> None:
    fake = FakeDirectShow([dshow_moniker("UGREEN Camera")], **{failure: E_FAIL})
    with pytest.raises(OSError, match=r"CoCreateInstance|CreateClassEnumerator"):
        oc.enumerate_dshow(fake.api())
    assert set(fake.refs.values()) <= {0} and fake.errors == []
    assert fake.calls[-1] == "CoUninitialize"


def test_com_already_set_up_by_someone_else_is_left_alone_by_the_directshow_listing() -> None:
    fake = FakeDirectShow([dshow_moniker("UGREEN Camera")], init_hr=oc.RPC_E_CHANGED_MODE)
    assert [d.name for d in oc.enumerate_dshow(fake.api())] == ["UGREEN Camera"]
    assert "CoUninitialize" not in fake.calls and fake.errors == []


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has DirectShow")
def test_list_dshow_devices_is_empty_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()  # the real function, not the autouse stand-in
    assert oc.list_dshow_devices() == []


@pytest.mark.skipif(sys.platform != "win32", reason="DirectShow is Windows-only")
def test_list_dshow_devices_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()
    devices = oc.list_dshow_devices()  # a CI runner has no camera; it must still not raise
    assert isinstance(devices, list)
    assert [d.index for d in devices] == list(range(len(devices)))
    assert all(isinstance(d.name, str) and (d.path is None or d.path) for d in devices)
