"""The OpenCV camera against a fake capture (no camera here), and Media Foundation's listing against fake COM objects.

The fakes record which thread did what, the backends and parameters the
camera asked for, and every release, so the Windows-only paths (MSMF then
DirectShow, the error classification, the ctypes enumeration) run on any OS.
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any

import cv2
import numpy as np
import pytest

from jarvis_hands import platform as plat
from jarvis_hands.camera import CameraError, create_camera
from jarvis_hands.camera import opencv_camera as oc
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

    def __call__(self, index: int, api: int, params: Any) -> FakeCapture:
        self.calls.append((index, api, list(params)))
        self.events.append(f"open {index} {api}")
        capture = self.captures.pop(0) if self.captures else FakeCapture(opened=False)
        capture.events = self.events
        self.made.append(capture)
        return capture


@pytest.fixture(autouse=True)
def no_real_devices(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never enumerate or read the privacy switches of the machine running the tests."""
    monkeypatch.setattr(oc, "list_cameras", lambda: [])
    monkeypatch.setattr(plat, "camera_access_denied", lambda: False)


def listed(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    monkeypatch.setattr(oc, "list_cameras", lambda: list(enumerate(names)))


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


def test_directshow_is_tried_when_msmf_opens_but_sends_nothing(cameras: list[OpenCVCamera]) -> None:
    # Some UVC webcams open on MSMF and then fail every grab, yet stream on DirectShow.
    msmf, dshow = FakeCapture(frames=lambda n: None, delay=0.01), FakeCapture()
    factory = FakeFactory([msmf, dshow])
    info = opened(cameras, factory, first_frame_timeout=0.2).info
    assert info is not None and info.backend == "dshow"
    assert [c[:2] for c in factory.calls] == [(0, cv2.CAP_MSMF), (0, cv2.CAP_DSHOW)]
    # The silent MSMF capture is let go before DirectShow asks for the device.
    assert factory.events[:3] == [f"open 0 {cv2.CAP_MSMF}", "release", f"open 0 {cv2.CAP_DSHOW}"]
    assert msmf.released == 1 and dshow.released == 0
    assert cameras[0].read(1.0) is not None


def test_a_reopen_tries_the_backend_that_streamed_first(cameras: list[OpenCVCamera]) -> None:
    factory = FakeFactory(
        [
            FakeCapture(frames=lambda n: None, delay=0.01),  # MSMF: open but silent
            FakeCapture(frames=fails_after(3)),  # DirectShow streams, then the camera goes away
            FakeCapture(frames=lambda n: picture(100 + n)),  # DirectShow again after the reopen
        ]
    )
    camera = opened(cameras, factory, first_frame_timeout=0.2, lost_after=0.1, reopen_delays=(0.01,))
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        frame = camera.read(0.2)
        if frame is not None and frame.image[0, 0, 0] >= 100:
            break
    else:
        pytest.fail("no frame from the reopened camera")
    assert [c[1] for c in factory.calls] == [cv2.CAP_MSMF, cv2.CAP_DSHOW, cv2.CAP_DSHOW]
    info = camera.info
    assert info is not None and info.backend == "dshow"


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
    camera = opened(cameras, FakeFactory([FakeCapture(delay=0.002)]))
    frames = []
    for _ in range(5):
        frame = camera.read(1.0)
        assert frame is not None
        frames.append(frame)
        time.sleep(0.03)  # a slow tracker
    seqs = [f.seq for f in frames]
    assert seqs == sorted(set(seqs)), seqs
    assert any(b - a > 1 for a, b in pairwise(seqs)), seqs
    for f in frames:
        assert int(f.image[0, 0, 0]) == (f.seq - 1) % 256  # the frame that seq names, not an older one
    assert all(a.t < b.t for a, b in pairwise(frames))


def test_each_frame_is_returned_once_and_a_read_times_out(cameras: list[OpenCVCamera]) -> None:
    camera = opened(cameras, FakeFactory([FakeCapture(frames=fails_after(1), delay=0.01)]), lost_after=10.0)
    first = camera.read(1.0)
    assert first is not None and first.seq == 1
    started = time.monotonic()
    assert camera.read(0.05) is None
    assert time.monotonic() - started < 0.5


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
    """``clock.now`` for the camera module: each call is one 30 fps frame interval later, from 5000 s."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.given: list[float] = []

    def __call__(self) -> float:
        with self._lock:
            self.given.append(5000.0 + len(self.given) / 30)
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


def test_info_reports_the_requested_rate_until_it_is_measured(cameras: list[OpenCVCamera]) -> None:
    camera = make_camera(FakeFactory([FakeCapture(delay=0.004)]), fps_window=0.3)
    cameras.append(camera)
    assert camera.open().fps == 30.0
    time.sleep(0.5)
    info = camera.info
    assert info is not None and info.fps != 30.0 and 40.0 < info.fps < 1000.0


# --------------------------------------------------------------------------- failures


def test_no_picture_in_time_means_the_camera_is_in_use() -> None:
    capture = FakeCapture(frames=lambda n: None, delay=0.01)
    camera = make_camera(FakeFactory([capture]), first_frame_timeout=0.2)
    started = time.monotonic()
    with pytest.raises(CameraError) as error:
        camera.open()
    assert time.monotonic() - started < 1.5
    assert error.value.code == "camera_in_use" and error.value.hint == plat.camera_in_use_hint()
    assert capture.released == 1 and camera.info is None
    camera.close()


def test_two_silent_backends_mean_the_camera_is_in_use() -> None:
    msmf, dshow = FakeCapture(frames=lambda n: None, delay=0.01), FakeCapture(frames=lambda n: None, delay=0.01)
    factory = FakeFactory([msmf, dshow])
    camera = make_camera(factory, first_frame_timeout=0.2)
    with pytest.raises(CameraError) as error:
        camera.open()
    assert error.value.code == "camera_in_use" and "msmf" in error.value.message and "dshow" in error.value.message
    assert [c[1] for c in factory.calls] == [cv2.CAP_MSMF, cv2.CAP_DSHOW]
    assert msmf.released == 1 and dshow.released == 1
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


def test_a_run_of_failed_reads_reopens_the_camera(cameras: list[OpenCVCamera]) -> None:
    first = FakeCapture(frames=fails_after(3))
    second = FakeCapture(frames=lambda n: picture(100 + n))
    factory = FakeFactory([first, second])
    camera = opened(cameras, factory, lost_after=0.1, reopen_delays=(0.01,))
    deadline = time.monotonic() + 3.0
    seqs = []
    while time.monotonic() < deadline:
        frame = camera.read(0.2)
        if frame is None:
            continue
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


def test_repeated_reopen_failures_lose_the_camera(cameras: list[OpenCVCamera]) -> None:
    factory = FakeFactory([FakeCapture(frames=fails_after(2))])
    camera = opened(cameras, factory, lost_after=0.1)
    deadline = time.monotonic() + 3.0
    with pytest.raises(CameraError) as error:
        while time.monotonic() < deadline:
            camera.read(0.2)
    assert error.value.code == "camera_lost" and error.value.hint
    assert len(factory.calls) == 1 + 3 * 2  # three reopens, each trying MSMF then DirectShow
    assert all(c.released == 1 for c in factory.made)
    assert camera.info is None
    with pytest.raises(CameraError):
        camera.read(0.0)


def test_a_camera_chosen_by_name_is_looked_up_again_on_reopen(
    monkeypatch: pytest.MonkeyPatch, cameras: list[OpenCVCamera]
) -> None:
    listings = iter([[(0, "Integrated Webcam"), (1, "UGREEN Camera")]])
    monkeypatch.setattr(oc, "list_cameras", lambda: next(listings, [(0, "UGREEN Camera")]))
    factory = FakeFactory([FakeCapture(frames=fails_after(2)), FakeCapture(frames=lambda n: picture(100 + n))])
    camera = opened(cameras, factory, "ugreen", lost_after=0.1, reopen_delays=(0.01,))
    deadline = time.monotonic() + 3.0
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
    capture = FakeCapture(frames=lambda n: None, delay=0.01)
    camera = make_camera(FakeFactory([capture]), first_frame_timeout=5.0)
    errors: list[BaseException] = []

    def open_it() -> None:
        try:
            camera.open()
        except CameraError as exc:
            errors.append(exc)

    opener = threading.Thread(target=open_it)
    started = time.monotonic()
    opener.start()
    time.sleep(0.1)
    camera.close()
    opener.join(3.0)
    assert not opener.is_alive() and time.monotonic() - started < 2.0
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
        if bytes(key.contents) != uuid.UUID(FRIENDLY_NAME).bytes_le:
            return E_FAIL
        name = self._names[this]
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
    assert oc.enumerate_msmf(fake.api()) == [(0, "UGREEN Camera"), (1, "Caméra intégrée")]
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
    assert oc.enumerate_msmf(fake.api()) == [(0, "UGREEN Camera")]
    assert "CoUninitialize" not in fake.calls and fake.errors == []


@pytest.mark.skipif(sys.platform == "win32", reason="Windows lists its cameras")
def test_list_cameras_is_empty_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()  # the real function, not the autouse stand-in
    assert oc.list_cameras() == []


@pytest.mark.skipif(sys.platform != "win32", reason="Media Foundation is Windows-only")
def test_list_cameras_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()
    cameras = oc.list_cameras()  # a CI runner has none; it must still not raise
    assert isinstance(cameras, list)
    assert all(isinstance(i, int) and isinstance(name, str) and name for i, name in cameras)
    assert [i for i, _ in cameras] == list(range(len(cameras)))
