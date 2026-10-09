"""The runtime around the keyboard controller (DESIGN 3.8 RT1-RT13, 5.6) and the exception scrub of ``logs.py`` (F4).

Part one is the scrub: while a keyboard session is open no exception message reaches a log record, stderr or a report
(SR13, S22b). Part two is the runtime wiring: the loop thread, the lock, the commands, pause and resume, the
two-hands switch and the keep-awake call.
"""

from __future__ import annotations

import functools
import json
import logging
import sys
import threading
import time
import traceback
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import jsonschema
import numpy as np
import pytest

from jarvis_hands import clock, logs, protocol
from jarvis_hands.camera.base import CameraError, CameraFrame, CameraInfo
from jarvis_hands.desktop.fake import FakeDesktop
from jarvis_hands.keyboard import synth
from jarvis_hands.keyboard.layout import layout_for
from jarvis_hands.keyboard.plane import Plane
from jarvis_hands.keyboard.practice import write_marker
from jarvis_hands.keyboard.session import KeyboardSession
from jarvis_hands.keyboard.tuning import Tuning
from jarvis_hands.landmarks import Frame
from jarvis_hands.overlay.base import NullOverlay, OverlayError, OverlayState
from jarvis_hands.runtime import HandsRuntime, RuntimeOptions
from jarvis_hands.tracker.fake import FakeTracker

from scripted import H, Script

S = "zzqxjv"


@pytest.fixture(autouse=True)
def clean_hooks() -> Iterator[None]:
    """Every test starts from the interpreter's own hooks and leaves the suite's as it found them.

    The controller installs its wrappers once for the life of the process, so any test before this module may have
    left them in place; a test of "the first scrub installs them" needs the bare ones.
    """
    factory = logging.getLogRecordFactory()
    excepthook, thread_hook = sys.excepthook, threading.excepthook
    logs.keyboard_scrub(False)
    logging.setLogRecordFactory(logging.LogRecord)
    sys.excepthook, threading.excepthook = sys.__excepthook__, threading.__excepthook__
    try:
        yield
    finally:
        logs.keyboard_scrub(False)
        logging.setLogRecordFactory(factory)
        sys.excepthook, threading.excepthook = excepthook, thread_hook


def raised(exc: BaseException) -> BaseException:
    try:
        raise exc
    except BaseException as caught:  # noqa: BLE001 - the test wants the traceback attached
        return caught


def chained() -> BaseException:
    try:
        try:
            raise OSError(S)
        except OSError as inner:
            raise ValueError(f"bad stroke {S!r}") from inner
    except ValueError as outer:
        return outer


def formatted(record: logging.LogRecord) -> str:
    return logging.Formatter("%(levelname)s %(message)s").format(record)


# ------------------------------------------------------------------------------------------------ exc_text (RT12, RT13)


def test_exc_text_is_todays_text_while_no_keyboard_session_is_open() -> None:
    exc = RuntimeError("boom")
    assert logs.exc_text(exc) == f"{type(exc).__name__}: {exc}" == "RuntimeError: boom"
    assert logs.exc_text(exc, typed=False) == str(exc) == "boom"
    assert logs.exc_text(OSError()) == "OSError: "
    assert logs.exc_text(KeyError("k")) == f"KeyError: {KeyError('k')}"


def test_exc_text_is_the_type_name_only_while_a_session_is_open() -> None:
    logs.keyboard_scrub(True)
    assert logs.exc_text(RuntimeError(S)) == "RuntimeError"
    assert logs.exc_text(RuntimeError(S), typed=False) == "RuntimeError"
    logs.keyboard_scrub(False)
    assert logs.exc_text(RuntimeError(S)) == f"RuntimeError: {S}"


def test_the_scrub_is_a_switch_that_can_be_turned_on_and_off_again_and_again() -> None:
    for _ in range(3):
        logs.keyboard_scrub(True)
        logs.keyboard_scrub(True)
        assert logs.exc_text(ValueError(S)) == "ValueError"
        logs.keyboard_scrub(False)
        logs.keyboard_scrub(False)
        assert logs.exc_text(ValueError(S)) == f"ValueError: {S}"


# --------------------------------------------------------------------------------------------- the log-record wrapper


def test_the_first_scrub_on_wraps_the_record_factory_once_and_only_once() -> None:
    before = logging.getLogRecordFactory()
    logs.keyboard_scrub(True)
    wrapped = logging.getLogRecordFactory()
    assert wrapped is not before
    logs.keyboard_scrub(False)
    logs.keyboard_scrub(True)
    assert logging.getLogRecordFactory() is wrapped


def test_nothing_is_installed_by_turning_the_scrub_off() -> None:
    before = (logging.getLogRecordFactory(), sys.excepthook, threading.excepthook)
    logs.keyboard_scrub(False)
    assert (logging.getLogRecordFactory(), sys.excepthook, threading.excepthook) == before


@pytest.mark.parametrize(
    "make", [lambda: RuntimeError(S), lambda: OSError(S), lambda: ValueError(f"bad {S!r}"), chained]
)
def test_an_exception_in_the_args_of_a_record_becomes_its_type_name(
    make: object, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    logs.keyboard_scrub(True)
    logging.getLogger("t.scrub").warning("it failed (%s)", make())  # type: ignore[operator]
    (record,) = caplog.records
    assert S not in record.getMessage() and S not in caplog.text and S not in repr(record.args)
    assert record.getMessage() in ("it failed (RuntimeError)", "it failed (OSError)", "it failed (ValueError)")


def test_a_mapping_of_args_is_scrubbed_value_by_value(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    logs.keyboard_scrub(True)
    logging.getLogger("t.scrub").warning("%(why)s and %(n)d", {"why": RuntimeError(S), "n": 3})
    (record,) = caplog.records
    assert record.getMessage() == "RuntimeError and 3"


def test_a_message_that_is_itself_an_exception_becomes_its_type_name(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    logs.keyboard_scrub(True)
    logging.getLogger("t.scrub").error(RuntimeError(S))
    (record,) = caplog.records
    assert record.getMessage() == "RuntimeError" and S not in caplog.text


def test_args_that_are_not_exceptions_pass_through_untouched(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    logs.keyboard_scrub(True)
    logging.getLogger("t.scrub").info("%s %d %.1f %r", "word", 3, 2.5, (1, 2))
    assert caplog.records[0].getMessage() == "word 3 2.5 (1, 2)"


def test_log_exception_keeps_the_frames_and_the_type_name_and_drops_the_message_and_the_chain(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    logs.keyboard_scrub(True)
    try:
        raise chained()
    except ValueError:
        logging.getLogger("t.scrub").exception("the keyboard failed")
    (record,) = caplog.records
    text = formatted(record)
    assert S not in text and S not in caplog.text
    assert record.exc_info is None
    assert "ValueError" in text and "test_log_exception_keeps_the_frames" in text and "test_kb_runtime.py" in text
    assert "OSError" not in text and "direct cause" not in text and "Traceback" not in text.split("ValueError")[1]


def test_exc_info_given_as_a_tuple_or_an_exception_is_scrubbed_the_same_way(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    logs.keyboard_scrub(True)
    exc = raised(RuntimeError(S))
    log = logging.getLogger("t.scrub")
    log.warning("one", exc_info=(type(exc), exc, exc.__traceback__))
    log.warning("two", exc_info=exc)
    assert len(caplog.records) == 2
    for record in caplog.records:
        assert S not in formatted(record) and "RuntimeError" in formatted(record)


def test_a_record_without_an_exception_is_left_alone(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    logs.keyboard_scrub(True)
    logging.getLogger("t.scrub").warning("plain")
    record = caplog.records[0]
    assert (record.exc_info, record.exc_text, record.getMessage()) == (None, None, "plain")


def test_with_the_scrub_off_a_record_is_exactly_what_logging_makes_of_it(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    logs.keyboard_scrub(True)
    logs.keyboard_scrub(False)
    exc = raised(RuntimeError(S))
    try:
        raise exc
    except RuntimeError:
        logging.getLogger("t.scrub").exception("it failed (%s)", exc)
    (record,) = caplog.records
    assert record.args == (exc,) and record.exc_info is not None
    assert f"RuntimeError: {S}" in formatted(record)


def test_a_record_made_while_the_scrub_is_on_stays_scrubbed_after_it_is_off(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    logs.keyboard_scrub(True)
    logging.getLogger("t.scrub").warning("it failed (%s)", RuntimeError(S))
    logs.keyboard_scrub(False)
    assert S not in caplog.text and S not in caplog.records[0].getMessage()


def test_the_scrub_reaches_the_file_log_and_stderr_handlers(tmp_path, capfd: pytest.CaptureFixture[str]) -> None:
    log_file = logs.setup_logging(tmp_path)
    assert log_file is not None
    logs.keyboard_scrub(True)
    log = logging.getLogger("t.scrub")
    try:
        raise OSError(S)
    except OSError:
        log.exception("the overlay failed (%s)", OSError(S))
    for handler in logging.getLogger().handlers:
        handler.flush()
    err = capfd.readouterr().err
    assert "OSError" in err and S not in err
    written = log_file.read_text(encoding="utf-8")
    assert "OSError" in written and S not in written


# ------------------------------------------------------------------------------------------------------- the hooks


def test_sys_excepthook_prints_where_it_happened_and_never_what_it_carried(capsys: pytest.CaptureFixture[str]) -> None:
    seen: list[BaseException] = []
    sys.excepthook = lambda t, v, tb: seen.append(v)  # type: ignore[assignment]
    logs.keyboard_scrub(True)
    exc = raised(chained())
    sys.excepthook(type(exc), exc, exc.__traceback__)
    err = capsys.readouterr().err
    assert S not in err and "ValueError" in err and "test_kb_runtime.py" in err and "OSError" not in err
    assert seen == []  # the old hook is not run while the scrub is on
    logs.keyboard_scrub(False)
    sys.excepthook(type(exc), exc, exc.__traceback__)
    assert seen == [exc]  # off: the hook that was there before


def test_threading_excepthook_prints_where_it_happened_and_never_what_it_carried(
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: list[BaseException | None] = []
    threading.excepthook = lambda args: seen.append(args.exc_value)
    logs.keyboard_scrub(True)

    def boom() -> None:
        raise RuntimeError(S)

    thread = threading.Thread(target=boom, name="kb-test-thread")
    thread.start()
    thread.join()
    err = capsys.readouterr().err
    assert S not in err and "RuntimeError" in err and "boom" in err and "kb-test-thread" in err
    assert seen == []
    logs.keyboard_scrub(False)
    thread = threading.Thread(target=boom)
    thread.start()
    thread.join()
    assert len(seen) == 1 and isinstance(seen[0], RuntimeError)


def test_the_threading_hook_copes_with_a_missing_thread_and_a_missing_exception(
    capsys: pytest.CaptureFixture[str],
) -> None:
    logs.keyboard_scrub(True)
    args = threading.ExceptHookArgs((RuntimeError, None, None, None))
    threading.excepthook(args)
    assert "RuntimeError" in capsys.readouterr().err
    sys.excepthook(KeyboardInterrupt, None, None)  # type: ignore[arg-type]
    assert "KeyboardInterrupt" in capsys.readouterr().err


def test_the_hook_installs_are_idempotent() -> None:
    logs.keyboard_scrub(True)
    first = (sys.excepthook, threading.excepthook, logging.getLogRecordFactory())
    logs.keyboard_scrub(False)
    logs.keyboard_scrub(True)
    assert (sys.excepthook, threading.excepthook, logging.getLogRecordFactory()) == first


def test_a_traceback_without_frames_still_names_the_type(capsys: pytest.CaptureFixture[str]) -> None:
    logs.keyboard_scrub(True)
    sys.excepthook(RuntimeError, RuntimeError(S), None)
    err = capsys.readouterr().err
    assert "RuntimeError" in err and S not in err
    assert traceback.format_tb(None) == []


# ============================================================================================== the runtime around it

TIMEOUT = 10.0
BLACK = np.zeros((720, 1280, 3), dtype=np.uint8)
A = (0.5, 0.45)
SCHEMA = json.loads(protocol.schema_path().read_text(encoding="utf-8"))


@functools.cache
def validator(def_name: str) -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator({"$ref": f"#/$defs/{def_name}", "$defs": SCHEMA["$defs"]})


def validate(instance: Any, def_name: str) -> None:
    validator(def_name).validate(instance)


def eventually(condition: Callable[[], Any], what: str, timeout: float = TIMEOUT) -> Any:
    """Polls for what another thread (the executor's) does on its own clock; the frames themselves never sleep."""
    deadline = time.monotonic() + timeout
    while True:
        value = condition()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.005)


def practice_result() -> Any:
    from test_kb_controller import result

    return result()


class Writer:
    """The mod's end of the event stream."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.events: list[dict[str, Any]] = []

    def emit(self, event: dict[str, Any]) -> None:
        with self._lock:
            self.events.append(dict(event))

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self.events)

    def keyboard(self, state: str | None = None) -> list[dict[str, Any]]:
        return [e for e in self.snapshot() if e["type"] == "keyboard" and (state is None or e["state"] == state)]

    def closed(self) -> list[str]:
        return [e["reason"] for e in self.keyboard("closed")]

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [e for e in self.snapshot() if e["type"] == kind]

    def errors(self, code: str | None = None) -> list[dict[str, Any]]:
        return [e for e in self.of("error") if code is None or e["code"] == code]


class StepCamera:
    """A camera that delivers exactly the frames a test puts in, and says when the loop has finished with them.

    ``put`` queues a frame; ``settled`` waits until the loop has taken every frame put and is back at ``read`` with
    nothing queued, which is after ``_process``, the housekeeping and anything else a pass does.
    """

    def __init__(self) -> None:
        self._wake = threading.Condition()
        self._frames: deque[CameraFrame] = deque()
        self._info: CameraInfo | None = None
        self._failure: CameraError | None = None
        self._idle = False
        self.opens = 0
        self.put_count = 0
        self.taken = 0

    @property
    def info(self) -> CameraInfo | None:
        return self._info

    def open(self) -> CameraInfo:
        self._info = CameraInfo("step camera", 0, "fake", 1280, 720, 30.0)
        self.opens += 1
        return self._info

    def put(self, t: float) -> None:
        with self._wake:
            self.put_count += 1
            self._frames.append(CameraFrame(self.put_count, t, BLACK))
            self._idle = False
            self._wake.notify_all()

    def fail(self) -> None:
        with self._wake:
            self._failure = CameraError("camera_lost", "the step camera was unplugged", "Scripted by a test.")
            self._wake.notify_all()

    def settled(self, timeout: float) -> bool:
        with self._wake:
            return self._wake.wait_for(lambda: self.taken >= self.put_count and self._idle, timeout)

    def release(self) -> None:
        """Wakes a ``read`` that is waiting, so that a stop does not wait out its timeout."""
        with self._wake:
            self._wake.notify_all()

    def read(self, timeout: float) -> CameraFrame | None:
        with self._wake:
            if not self._frames and self._failure is None:
                self._idle = True
                self._wake.notify_all()
                self._wake.wait(timeout)
            if self._failure is not None:
                raise self._failure
            if not self._frames:
                return None
            self.taken += 1
            return self._frames.popleft()

    def close(self) -> None:
        self._info = None


class StepTracker(FakeTracker):
    """A tracker that returns the hands of the frame the test queued for each capture (as many as it is set to)."""

    def __init__(self) -> None:
        super().__init__(None)
        self.pending: deque[Frame] = deque()
        #: The thread each ``set_num_hands`` ran on.
        self.switched_on: list[str] = []
        self.fail: BaseException | None = None

    def process(self, image: np.ndarray, t: float) -> Frame:
        if self.fail is not None:
            raise self.fail
        queued = self.pending.popleft() if self.pending else None
        hands = queued.hands if queued is not None else ()
        return Frame(t, tuple(hands)[: self.num_hands], int(image.shape[1]), int(image.shape[0]))

    def set_num_hands(self, n: int) -> None:
        super().set_num_hands(n)
        self.switched_on.append(threading.current_thread().name)


class Screen(NullOverlay):
    """An overlay that fails on demand."""

    def __init__(self) -> None:
        self.fail: BaseException | None = None
        self.shown = 0

    def show(self, state: OverlayState) -> None:
        if self.fail is not None:
            raise self.fail
        self.shown += 1


class AwakeDesktop(FakeDesktop):
    """A fake desktop with the two optional calls of the Windows one: what ``keep_awake`` was asked, on which thread,
    and whether the input desktop is ours."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.awake: list[tuple[bool, str]] = []
        self.input_ok = True

    def keep_awake(self, on: bool) -> None:
        self.awake.append((on, threading.current_thread().name))

    def input_desktop_ok(self) -> bool:
        return self.input_ok


class Time:
    """The runtime's clock, which the test moves: the time of the frame in hand, so that nothing here sleeps."""

    def __init__(self) -> None:
        self.t = clock.now()

    def __call__(self) -> float:
        return self.t


class Lockstep:
    """A started runtime on fakes whose loop thread is given one frame at a time (K5 to K12, K43 to K49, Q4, Q5).

    The loop thread, the lock, the command path and the executor are the real ones; the camera, the tracker and the
    clock are the test's. A frame is put into the camera and the test waits until the loop is back at the camera, with
    the clock standing at that frame's time, so a 200-character run is 200 hand-offs and takes no real time.
    """

    def __init__(self, data_dir: Path, *, desktop: FakeDesktop | None = None, camera: StepCamera | None = None) -> None:
        self.data_dir = data_dir
        self.writer = Writer()
        self.camera = camera or StepCamera()
        self.tracker = StepTracker()
        self.desktop = desktop or FakeDesktop()
        self.screen = Screen()
        self.time = Time()
        self._stopped = False
        self.runtime = HandsRuntime(
            RuntimeOptions(data_dir=data_dir),
            self.writer,
            desktop_factory=lambda: self.desktop,
            camera_factory=lambda: self.camera,
            tracker_factory=lambda: self.tracker,
            overlay_factory=lambda: self.screen,
        )
        self.runtime._clock = self.time

    # -- running
    def start(self) -> Lockstep:
        self.runtime.start()
        eventually(lambda: any(e["type"] == "state" and e["state"] == "idle" for e in self.writer.snapshot()), "idle")
        return self

    def feed(self, frames: Sequence[Frame]) -> None:
        for frame in frames:
            self.time.t = frame.t
            self.tracker.pending.append(frame)
            self.camera.put(frame.t)
            if not self.camera.settled(TIMEOUT):
                raise AssertionError(f"the loop did not settle after the frame at {frame.t:.2f}")

    def poke(self) -> None:
        """One more frame the test does not wait for (the loop is about to end or has just failed)."""
        self.time.t += 1 / 30
        self.camera.put(self.time.t)

    def script(self) -> Script:
        """A script of frames that goes on where the last frame fed ended."""
        return Script(t0=self.time.t + 1 / 30)

    def hold(self, seconds: float, *hands: H) -> None:
        self.feed(self.script().hold(*hands, seconds=seconds))

    def gap(self, seconds: float) -> None:
        self.feed(self.script().gap(seconds=seconds))

    def stop(self) -> None:
        if not self._stopped:
            self._stopped = True
            self.runtime.request_stop()
            self.camera.release()
            self.runtime.stop()

    # -- commands
    def command(self, name: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        reply = self.runtime.handle_command(name, body or {})
        validate(reply, "StatusResponse" if name == "status" and reply.get("ok") else "CommandResponse")
        return reply

    def status(self) -> dict[str, Any]:
        return self.command("status")

    @property
    def keyboard(self) -> dict[str, Any]:
        block: dict[str, Any] = self.status()["keyboard"]
        return block

    def kb(self, action: str, **more: Any) -> dict[str, Any]:
        return self.command("keyboard", {"action": action, **more})

    def enable(self, press: str = "pinch", commit: str = "review") -> Lockstep:
        reply = self.kb("configure", settings={"enabled": True, "press": press, "commit": commit})
        assert reply == {"ok": True}, reply
        return self

    def practiced(self, *methods: str) -> Lockstep:
        for method in methods:
            write_marker(self.data_dir, method, practice_result())  # type: ignore[arg-type]
        return self

    # -- the keyboard in use
    def typist(self, commit: str, seed: int = 3) -> synth.Typist:
        """A synthetic typist whose hands stand where the session will put its plane (the placing phase finds it)."""
        layout, tuning = layout_for(commit), Tuning()  # type: ignore[arg-type]
        script = self.script()
        plane = Plane(
            0.5, 0.45 * script.height / script.width, tuning.pitch, tuning.pitch * tuning.pitch_y_ratio, layout.rows
        )
        return synth.Typist(script, plane, rng=np.random.default_rng(seed), layout=layout)

    def armed(self, commit: str = "review") -> synth.Typist:
        """A pinch keyboard open, placed and warmed up, and the typist that did it.

        The warm-up ends when the seven pinches that mattered are in; the last stroke of the synthetic warm-up is then
        an ordinary press (the thumb on its way to the middle finger had passed the index), so the frames stop there.
        """
        self.enable("pinch", commit)
        if commit == "direct":
            self.practiced("pinch")
        assert self.kb("start") == {"ok": True}
        typist = self.typist(commit)
        self.feed(typist.place())
        for frame in typist.warm():
            self.feed([frame])
            if self.keyboard["phase"] == "typing":
                break
        self.feed(typist.hover(0.5))  # past the sink's warm-up
        return typist

    def fill_box(self, text: str) -> None:
        """Puts ``text`` in the review box without a tap each (a test of the run, not of the typing)."""
        with self.runtime._lock:
            session = self.runtime._kb._session
            assert session is not None and session._machine is not None
            for ch in text:
                assert session._machine.buffer.append(ch, self.time.t) == "ok"

    def insert(self, typist: synth.Typist, kind: str = "insert") -> None:
        self.feed(typist.tap_n(typist.layout.find(kind=kind), 3, 0.5))  # type: ignore[arg-type]


@pytest.fixture
def lockstep(tmp_path: Path) -> Iterator[Callable[..., Lockstep]]:
    made: list[Lockstep] = []

    def make(**kwargs: Any) -> Lockstep:
        item = Lockstep(kwargs.pop("data_dir", tmp_path), **kwargs)
        made.append(item)
        return item

    yield make
    for item in made:
        item.stop()
        assert not item.runtime._kb.active
        assert not item.desktop.buttons_down, "a mouse button was left down"
        for event in item.writer.snapshot():
            validate(event, "Event")
    assert not logs._scrub_on, "a test left the exception scrub on"


class Hammer:
    """A second thread that sends commands for as long as it lives (K10, K49): the control server's threads."""

    def __init__(self, item: Lockstep, bodies: Sequence[dict[str, Any]]) -> None:
        self.item, self.bodies = item, list(bodies)
        self.replies: list[tuple[str, dict[str, Any]]] = []
        self.raised: list[BaseException] = []
        self.rounds = 0
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, name="hammer", daemon=True)

    def _run(self) -> None:
        try:
            while not self._done.is_set():
                for body in self.bodies:
                    self.replies.append(("keyboard", self.item.runtime.handle_command("keyboard", dict(body))))
                self.replies.append(("status", self.item.runtime.handle_command("status", {})))
                self.rounds += 1
                self._done.wait(0.0005)  # lets the loop thread have the GIL: the frames are what is being tested
        except BaseException as exc:  # noqa: BLE001 - whatever it is, the test reports it
            self.raised.append(exc)

    def __enter__(self) -> Hammer:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._done.set()
        self._thread.join(TIMEOUT)
        assert not self._thread.is_alive()

    def check(self) -> None:
        assert self.raised == [] and self.rounds > 0
        for kind, reply in self.replies:
            validate(reply, "StatusResponse" if kind == "status" and reply.get("ok") else "CommandResponse")


# ----------------------------------------------------------------------------------------------- P5, RT3 and RT11


def test_p5_a_keyboard_command_before_start_is_safe_configure_works_and_start_says_still_starting(
    tmp_path: Path,
) -> None:
    item = Lockstep(tmp_path)
    assert item.kb("configure", settings={"enabled": True}) == {"ok": True}
    refusal = {"ok": False, "error": {"code": "bad_request", "message": "Hand control is still starting."}}
    assert item.kb("start") == refusal
    assert item.kb("practice")["error"]["message"] == "Hand control is still starting."
    assert item.kb("stop") == {"ok": True} and item.kb("recenter") == {"ok": True}
    assert item.keyboard == {"enabled": True, "state": "closed", "practiced": False}
    item.stop()


def test_rt3_the_keyboard_command_is_routed_and_what_is_not_one_is_refused(lockstep: Callable[..., Lockstep]) -> None:
    item = lockstep().start()
    assert item.kb("configure", settings={"size": 1.2}) == {"ok": True}
    assert item.kb("explode")["error"]["code"] == "bad_request"
    assert item.kb("configure", settings={"press": "air", "commit": "direct"})["ok"] is False
    assert item.command("keyboard", {})["ok"] is False


def test_rt11_the_status_carries_the_keyboard_block_closed_then_open_then_closed(
    lockstep: Callable[..., Lockstep],
) -> None:
    item = lockstep().start()
    assert item.keyboard == {"enabled": False, "state": "closed", "practiced": False}
    item.armed("review")
    assert item.keyboard == {
        "enabled": True,
        "state": "open",
        "practiced": False,
        "phase": "typing",
        "press": "pinch",
        "commit": "review",
        "lang": "en",
        "private": False,
        "review": {"state": "composing", "chars": 0},
    }
    assert item.kb("stop") == {"ok": True}
    assert item.keyboard["state"] == "closed" and item.status()["state"] == "idle"


# ------------------------------------------------------------------------------------------------------ K1 and K5


def test_k1_an_open_lets_go_of_the_pointer_and_lifts_a_pressed_button_before_it_answers(
    lockstep: Callable[..., Lockstep],
) -> None:
    item = lockstep().start()
    assert item.command("engage") == {"ok": True}
    item.hold(0.5, H("palm", A))
    item.feed(item.script().hold(H("pinch", A), frames=8))
    eventually(lambda: "left" in item.desktop.buttons_down, "the pinch to press the button")
    assert item.status()["engaged"] is True
    item.enable()
    assert item.kb("start") == {"ok": True}
    assert item.desktop.buttons_down == set() and ("button", "left", False) in item.desktop.calls
    assert item.status()["engaged"] is False and item.runtime._kb.needs_release is False
    (opened,) = item.writer.keyboard("open")
    assert opened["phase"] == "placing" and opened["press"] == "pinch"
    item.hold(0.2, H("palm", A))
    assert item.keyboard["state"] == "open"


def test_k5_the_tracker_follows_two_hands_while_the_keyboard_is_open_and_back_on_the_loop_thread(
    lockstep: Callable[..., Lockstep],
) -> None:
    item = lockstep().start().enable()
    item.hold(0.1, H("palm", A))
    assert item.tracker.num_hands_history == []
    item.kb("start")
    item.hold(0.1, H("palm", A))
    assert item.tracker.num_hands_history == [2]
    item.kb("stop")
    item.hold(0.1, H("palm", A))
    assert item.tracker.num_hands_history == [2, 1]
    assert set(item.tracker.switched_on) == {"hands-loop"}


def test_k5_a_rebuild_of_less_than_a_quarter_second_resets_nothing(lockstep: Callable[..., Lockstep]) -> None:
    """The landmarker is rebuilt for two hands between two frames, 50 to 180 ms (3.8): no frame comes meanwhile."""
    item = lockstep().start()
    typist = item.armed("direct")
    typist.script.t += 0.2
    item.feed(typist.type("hello"))
    item.feed(typist.hover(1.0))
    assert item.desktop.typed_text == "hello" and item.keyboard["phase"] == "typing" and item.writer.closed() == []


def test_k6_the_display_is_kept_awake_while_the_keyboard_is_open_and_let_go_after(
    lockstep: Callable[..., Lockstep],
) -> None:
    desktop = AwakeDesktop()
    item = lockstep(desktop=desktop).start().enable()
    item.hold(0.2, H("palm", A))
    assert desktop.awake == []
    item.kb("start")
    item.hold(0.2, H("palm", A))
    assert desktop.awake == [(True, "hands-loop")]
    item.kb("stop")
    item.hold(0.2, H("palm", A))
    assert desktop.awake == [(True, "hands-loop"), (False, "hands-loop")]


# ------------------------------------------------------------------------------------------- K10, K49 (two threads)


def test_k10_commands_from_another_thread_while_a_thousand_frames_go_through(
    lockstep: Callable[..., Lockstep],
) -> None:
    item = lockstep().start().enable()
    bodies = [{"action": a} for a in ("start", "recenter", "private", "public", "stop")]
    bodies.append({"action": "configure", "settings": {"size": 1.1}})
    frames = item.script().hold(H("palm", (0.4, 0.45), "left"), H("palm", (0.6, 0.45), "right"), frames=1000)
    with Hammer(item, bodies) as hammer:
        item.feed(frames)
    hammer.check()
    assert item.writer.errors() == [] and item.runtime._failed is False
    # Nothing is stuck: one more stop closes whatever the commands left, and the pointer has its frames again.
    assert item.kb("stop") == {"ok": True} and item.keyboard["state"] == "closed"
    item.gap(0.8)
    item.hold(1.2, H("palm", A))
    assert item.status()["engaged"] is True


def test_k49_commands_from_another_thread_while_a_run_of_two_hundred_characters_goes_out(
    lockstep: Callable[..., Lockstep],
) -> None:
    item = lockstep().start()
    typist = item.armed("review")
    text = "abcdefghij" * 20
    item.fill_box(text)
    item.insert(typist)  # the third tap starts the run; a Priv tap or command between the taps would void the guard
    assert 0 < len(item.desktop.key_calls) < len(text)
    bodies = [{"action": a} for a in ("private", "public", "recenter")]
    bodies.append({"action": "configure", "settings": {"size": 1.2}})
    with Hammer(item, bodies) as hammer:
        item.feed(typist.hover(8.0))
    hammer.check()
    assert item.desktop.typed_text == text and item.writer.errors() == [] and item.writer.closed() == []
    assert item.keyboard["review"] == {"state": "composing", "chars": 0}


# ----------------------------------------------------------------------------------------- K11, K43, K44, Q4, Q5, K7


def test_k11_the_real_mouse_moving_during_a_session_makes_no_engine_action_and_the_sink_yields(
    lockstep: Callable[..., Lockstep],
) -> None:
    item = lockstep().start()
    typist = item.armed("review")
    calls, gestures = list(item.desktop.calls), item.writer.of("gesture")
    item.desktop.move_mouse(321, 123)
    item.desktop.user_moved_mouse()
    item.feed(typist.hover(0.2))
    assert item.keyboard["hold"] == "yield" and item.keyboard["state"] == "open"
    assert item.desktop.calls == calls and item.writer.of("gesture") == gestures
    item.feed(typist.hover(1.8))
    assert "hold" not in item.keyboard  # a yield ends by itself, and the session was never closed
    assert item.writer.closed() == []


def test_k43_a_pause_in_the_middle_of_a_run_closes_it_lets_go_of_the_keys_and_starts_the_quarantine(
    lockstep: Callable[..., Lockstep],
) -> None:
    item = lockstep().start()
    typist = item.armed("review")
    text = "abcdefghij" * 4
    item.fill_box(text)
    item.insert(typist)  # the third tap starts the run, a character a frame
    sent = len(item.desktop.key_calls)
    assert 0 < sent < len(text)
    released = item.desktop.release_keys_calls
    assert item.command("pause")["ok"] is True
    assert item.writer.closed() == ["paused"] and item.desktop.release_keys_calls > released
    assert item.command("resume")["ok"] is True
    item.feed(typist.hover(1.0))
    assert len(item.desktop.key_calls) == sent and item.keyboard["state"] == "closed"
    # The hands that were over the keys stay in view: the pointer must not engage on them (2.11) ...
    item.hold(1.2, H("palm", A))
    assert item.status()["engaged"] is False
    # ... until the camera has seen no hand for the quarantine's length; then the engine engages by its own rules.
    item.gap(0.8)
    item.hold(1.2, H("palm", A))
    assert item.status()["engaged"] is True


def test_q5_pause_closes_the_session_resume_does_not_reopen_it_and_the_executor_holds_nothing(
    lockstep: Callable[..., Lockstep],
) -> None:
    item = lockstep().start()
    item.armed("review")
    events = len(item.writer.keyboard())
    assert item.command("pause")["ok"] is True
    assert item.writer.closed() == ["paused"] and item.keyboard["state"] == "closed"
    calls = list(item.desktop.calls)
    assert item.command("resume")["ok"] is True
    item.hold(0.5, H("palm", A))
    assert item.keyboard["state"] == "closed" and len(item.writer.keyboard()) == events + 1  # the close, nothing more
    assert item.desktop.calls == calls and item.desktop.buttons_down == set()
    assert item.kb("start") == {"ok": True}  # a start after the resume opens a new one, as ever


@pytest.mark.parametrize("commit", ["direct", "review"])
def test_k44_three_slow_sends_close_input_blocked_and_nothing_goes_out_after(
    lockstep: Callable[..., Lockstep], commit: str
) -> None:
    item = lockstep().start()
    typist = item.armed(commit)
    item.desktop.after_key = lambda n: setattr(item.time, "t", item.time.t + 0.3)  # Windows takes 0.3 s for a key
    if commit == "review":
        item.fill_box("abcdefghij")
        item.insert(typist)
    else:
        item.feed(typist.type("hello"))
    item.feed(typist.hover(2.0))
    assert item.writer.closed() == ["input_blocked"] and len(item.desktop.key_calls) == 3
    (error,) = item.writer.errors("input_blocked")
    assert (
        error["fatal"] is False
        and error["message"] == "The air keyboard stopped because Windows did not take its keys."
    )
    assert item.keyboard["state"] == "closed"


@pytest.mark.parametrize(
    ("command", "body"), [("engage", {}), ("calibrate", {"action": "start"})], ids=["engage", "calibrate"]
)
def test_q4_an_explicit_engage_or_calibrate_closes_the_session_and_the_pointer_has_its_frames_at_once(
    lockstep: Callable[..., Lockstep], command: str, body: dict[str, Any]
) -> None:
    item = lockstep().start().enable()
    item.kb("start")
    item.hold(0.2, H("palm", A))
    assert item.keyboard["state"] == "open"
    assert item.command(command, body) == {"ok": True}
    assert item.writer.closed() == ["command"]
    # A hand is in view all the time: a quarantine would have kept it from the engine.
    if command == "engage":
        item.feed(item.script().move("palm", A, (0.55, 0.45), frames=10))
        assert item.status()["engaged"] is True
        eventually(lambda: item.desktop.calls_named("move_cursor"), "the cursor to move")
    else:
        corner = (0.30, 0.25)
        item.feed(item.script().move("palm", A, corner, frames=4))
        item.hold(1.4, H("palm", corner))
        assert [e["step"] for e in item.writer.of("calibration")][:2] == ["top_left", "top_right"]


def test_k7_an_exception_in_the_session_closes_it_reports_once_with_fixed_text_and_hands_the_pointer_back(
    lockstep: Callable[..., Lockstep], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    item = lockstep().start()
    item.armed("review")

    def boom(*args: object, **kwargs: object) -> None:
        raise ValueError(f"the keys {S}")

    monkeypatch.setattr(KeyboardSession, "update", boom)
    with caplog.at_level(logging.DEBUG):
        item.hold(0.3, H("palm", A))
    assert item.writer.closed() == ["error"]
    (error,) = item.writer.errors("internal")
    assert S not in json.dumps(item.writer.snapshot()) and S not in caplog.text
    item.gap(0.8)
    item.hold(1.2, H("palm", A))
    assert item.status()["engaged"] is True and len(item.writer.errors("internal")) == 1
    assert error["fatal"] is False


# ------------------------------------------------------------------------------------------------------ RT7 and RT8


def test_rt7_a_locked_desktop_closes_the_session(lockstep: Callable[..., Lockstep]) -> None:
    desktop = AwakeDesktop()
    item = lockstep(desktop=desktop).start()
    typist = item.armed("review")
    released = desktop.release_keys_calls
    desktop.input_ok = False
    item.feed(typist.hover(1.0))
    eventually(lambda: item.writer.closed() == ["desktop_locked"], "the lock screen to close the session")
    assert desktop.release_keys_calls > released and item.keyboard["state"] == "closed"


def test_rt8_an_overlay_that_fails_while_the_keyboard_is_open_closes_it_and_reports_the_type_only(
    lockstep: Callable[..., Lockstep],
) -> None:
    item = lockstep().start()
    typist = item.armed("review")
    item.screen.fail = OverlayError(f"cannot draw {S}")
    item.feed(typist.hover(0.2))
    assert item.writer.closed() == ["no_overlay"]
    (report,) = item.writer.errors("overlay_failed")
    assert (
        report["message"] == "The on-screen reticle could not be shown (OverlayError); hand control works without it."
    )
    assert S not in json.dumps(item.writer.snapshot())


def test_rt8_with_no_session_open_the_overlay_report_keeps_todays_text(lockstep: Callable[..., Lockstep]) -> None:
    item = lockstep().start()
    item.screen.fail = OverlayError(f"cannot draw {S}")
    item.hold(0.2, H("palm", A))
    (report,) = item.writer.errors("overlay_failed")
    assert (
        report["message"]
        == f"The on-screen reticle could not be shown (cannot draw {S}); hand control works without it."
    )


def test_rt8_turning_the_overlay_off_closes_the_session(lockstep: Callable[..., Lockstep]) -> None:
    item = lockstep().start()
    item.armed("review")
    assert item.command("config", {"overlay": False}) == {"ok": True}
    assert item.writer.closed() == ["no_overlay"] and item.keyboard["state"] == "closed"


# ------------------------------------------------------------------------------------------------------------ RT10


def test_rt10_stop_closes_the_session_before_anything_else_winds_down(lockstep: Callable[..., Lockstep]) -> None:
    item = lockstep().start()
    item.armed("review")
    released = item.desktop.release_keys_calls
    item.stop()
    assert item.writer.closed() == ["command"] and item.desktop.release_keys_calls > released


def test_rt10_a_lost_camera_closes_the_session(lockstep: Callable[..., Lockstep]) -> None:
    item = lockstep().start()
    item.armed("review")
    item.camera.fail()
    eventually(lambda: item.writer.closed() == ["camera"], "the lost camera to close the session")
    assert [e["code"] for e in item.writer.errors() if e["fatal"]] == ["camera_lost"]


# ------------------------------------------------------------------------------------------------------ RT12 and RT13


def test_rt12_a_command_that_raises_answers_with_todays_text_and_the_type_only_while_a_session_is_open(
    lockstep: Callable[..., Lockstep], monkeypatch: pytest.MonkeyPatch
) -> None:
    item = lockstep().start()

    def boom(body: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError(f"bad {S}")

    monkeypatch.setattr(item.runtime, "_cmd_pause", boom)
    assert item.runtime.handle_command("pause", {})["error"]["message"] == f"RuntimeError: bad {S}"
    item.armed("review")
    assert item.runtime.handle_command("pause", {})["error"] == {"code": "internal", "message": "RuntimeError"}


@pytest.mark.parametrize("open_session", [False, True], ids=["closed", "open"])
def test_rt12_a_tracker_that_raises_in_the_loop_is_reported_with_todays_text_or_the_type_only(
    lockstep: Callable[..., Lockstep], caplog: pytest.LogCaptureFixture, open_session: bool
) -> None:
    item = lockstep().start()
    if open_session:
        item.armed("review")
    item.tracker.fail = RuntimeError(f"bad {S}")
    with caplog.at_level(logging.DEBUG):
        item.poke()
        eventually(lambda: item.writer.errors("internal"), "the loop to fail")
    (fatal,) = item.writer.errors("internal")
    if open_session:
        assert fatal["message"] == "Hand tracking failed: RuntimeError" and S not in caplog.text
        assert item.writer.closed() == ["camera"]
    else:
        assert fatal["message"] == f"Hand tracking failed: RuntimeError: bad {S}"


# ------------------------------------------------------------------------------------------------------- the cli tools


def test_the_parser_wires_the_three_keyboard_tools_to_their_modules() -> None:
    from jarvis_hands import cli
    from jarvis_hands.keyboard import keyreplay, keytest, keytrace

    parser = cli.build_parser()
    args = parser.parse_args(["keytest", "--countdown", "0", "--inject", "vk"])
    assert args.command == "keytest" and args.func is keytest.run and args.countdown == 0 and args.inject == "vk"
    args = parser.parse_args(["keytrace", "--out", "x.npz", "--yes-record"])
    assert args.command == "keytrace" and args.func is keytrace.run and args.yes_record and args.out == Path("x.npz")
    args = parser.parse_args(["keyreplay", "x.npz"])
    assert args.command == "keyreplay" and args.func is keyreplay.run
    help_text = parser.format_help()
    assert all(name in help_text for name in ("keytest", "keytrace", "keyreplay", "run", "doctor"))


def test_main_dispatches_each_keyboard_tool_to_its_run(monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvis_hands import cli
    from jarvis_hands.keyboard import keyreplay, keytest, keytrace

    seen: list[str] = []
    for code, module in enumerate((keytest, keytrace, keyreplay), start=3):
        monkeypatch.setattr(module, "run", lambda args, code=code: seen.append(args.command) or code)
    assert cli.main(["keytest", "--countdown", "0"]) == 3
    assert cli.main(["keytrace", "--out", "x.npz"]) == 4
    assert cli.main(["keyreplay", "x.npz"]) == 5
    assert seen == ["keytest", "keytrace", "keyreplay"]


def test_building_the_parser_does_not_load_the_controller_or_the_session() -> None:
    """``run`` and the other commands pay nothing for the keyboard until it is opened; the tools are stdlib only."""
    import subprocess

    code = (
        "import sys\n"
        "import jarvis_hands.cli as cli\n"
        "cli.build_parser()\n"
        "import json\n"
        "print(json.dumps(sorted(m for m in sys.modules if m.startswith('jarvis_hands.keyboard'))))\n"
    )
    done = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60, check=True)
    loaded = json.loads(done.stdout)
    assert "jarvis_hands.keyboard.controller" not in loaded and "jarvis_hands.keyboard.session" not in loaded
