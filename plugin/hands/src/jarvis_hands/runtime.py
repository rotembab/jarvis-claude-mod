"""The running helper: camera -> tracker -> gesture engine -> executor + overlay, and the command handler.

Threads, and who owns what:

- The **main thread** (``cli.cmd_run``) calls ``start()``, ``wait()`` and
  ``stop()``. ``start()`` builds everything in the spec's order (desktop,
  settings and mapper, engine, executor, overlay, tracker, camera) and then
  hands over to the loop thread.
- The **loop thread** owns the camera and the tracker: read the newest
  frame, track it, feed the engine, hand its actions to the executor, turn
  its events into protocol events, draw the overlay. Opening and closing the
  camera for ``pause`` / ``resume`` happens here too: the commands only set
  the wanted state and wait for the loop to reconcile, so the camera is
  never used by two threads at once. Per-thread Windows state
  (``keep_awake``) is only touched from here.
- The **executor thread** applies actions at 120 Hz and calls back when the
  real mouse moved (the engine lets go) or a window refused to move.
- The **control server's threads** call ``handle_command``, several at once,
  before, during and after ``start()``.

One runtime lock serialises every engine call together with the submit of
the actions it returned, and the drain of its events, so a command's
disengage can never land between a frame's batch and its submit, and state
events go out in the order the engine produced them. Nothing slow happens
under it: camera reads, inference, file writes and overlay drawing stay
outside, and it is never held while waiting on the executor's own lock
(the executor calls back into the runtime while holding that one).

Two clocks. The runtime's own intervals (frame rate, throttles, the stall
timer) read ``clock.now()``. The engine keeps time with the frames: it only
ever sees ``frame.t``, so whatever the runtime hands it besides a tracked
frame (the time a calibration starts, the empty frames of a stall) is put on
the frame clock as "the last frame's ``t`` plus the time since it arrived".
A camera stamping on another clock than the contract's then still calibrates
and times out correctly.

A camera that stops sending frames without failing (the real one returns
nothing while it reopens a hung device) is a hand out of view: once ``hold_s``
has passed without a frame, every read that comes back empty hands the engine
an empty frame, so its own hand-loss rules let go of the buttons and, after
``lost_s``, disengage. A calibration asked for before the first frame starts
with that frame.

Errors: what makes hand control impossible (no desktop backend, no model,
no camera, the camera lost, a bug in the loop) is one fatal ``error`` event,
then the ``error`` state and a stop with ``exit_code`` set. What only
degrades it (the overlay, a window that refuses to move) is a non-fatal
``error``, at most once per code every ``ERROR_REPEAT_S``. Every way out
releases the mouse buttons first.
"""

from __future__ import annotations

import logging
import math
import sys
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast, get_args

import numpy as np

from . import __version__, clock, protocol
from . import platform as plat
from .actions import Action, ReleaseAll
from .camera.base import Camera, CameraError, CameraInfo
from .desktop.base import Desktop, UnsupportedPlatform
from .events import EventSink
from .executor import Executor
from .gestures import EngineView, GestureEngine
from .landmarks import Frame
from .logs import exc_text
from .mapping import ScreenMapper
from .overlay.base import NullOverlay, Overlay, OverlayMode, OverlayState
from .settings import HandsSettings, load_calibration, save_calibration
from .tracker.base import Tracker, TrackerError

log = logging.getLogger(__name__)

#: The process exit code after a fatal error (``cli.EXIT_ERROR``).
EXIT_FATAL = 1
#: How long one camera read waits for a frame; also the longest the loop takes to notice a command.
READ_TIMEOUT_S = 0.5
#: A read that gave up well before its timeout (a closed or failed device) is retried after this, not at once.
NO_FRAME_BACKOFF_S = 0.02
#: How often the loop re-reads the display layout (a projector plugged in, a resolution change)
#: and the camera's measured frame rate.
DISPLAY_REFRESH_S = 2.0
#: A measured camera rate this much (relative) off the last announced one is announced in a new ``ready``.
RATE_ANNOUNCE_CHANGE = 0.1
#: How often the loop asks whether the input desktop is ours (lock screen, UAC prompt).
INPUT_DESKTOP_CHECK_S = 0.5
#: The window ``status`` measures the tracked frame rate over.
FPS_WINDOW_S = 3.0
#: The protocol's cap on gesture events.
GESTURES_PER_S = 10
#: A non-fatal error code is reported at most once in this many seconds.
ERROR_REPEAT_S = 10.0
#: How long ``pause`` and ``resume`` wait for the loop to close or reopen the camera (the mod waits 5 and 10 s).
PAUSE_WAIT_S = 4.0
RESUME_WAIT_S = 9.0
#: How long ``stop`` waits for the loop thread, and for a ``start`` running on another thread.
STOP_JOIN_S = 3.0
START_WAIT_S = 10.0
#: How often a paused loop looks for work.
PAUSED_POLL_S = 0.25
#: MediaPipe releases the GIL during inference, but a busy Python thread then gets it back only every
#: switch interval: at the default 5 ms, 15 ms of inference stretched to 48 ms (research-mediapipe.md).
SWITCH_INTERVAL_S = 0.001
#: Without ``Desktop.double_click()``: Windows' defaults (500 ms, 4 x 4 px).
DEFAULT_DOUBLE_CLICK = (0.5, 4, 4)

GESTURE_NAMES: frozenset[str] = frozenset(get_args(protocol.GestureName))
CALIBRATION_STEPS: frozenset[str] = frozenset(get_args(protocol.CalibrationStep))
ERROR_CODES: frozenset[str] = frozenset(get_args(protocol.ErrorCode))

#: The message for a platform with no desktop backend, when the backend itself gave none.
UNSUPPORTED_MESSAGE = "Hand control drives the mouse and windows on Windows only, for now."
#: The answer to a resume that a pause undid while the camera was still opening.
COUNTERMANDED: tuple[protocol.ErrorCode, str] = (
    "bad_request",
    "Hand control was paused again while the camera was starting.",
)


@dataclass(frozen=True)
class RuntimeOptions:
    """What ``cli.cmd_run`` passes from the command line."""

    data_dir: Path
    camera: str | None = None
    width: int = 1280
    height: int = 720
    fps: int = 30
    overlay: bool = True
    #: Scripted camera, tracker and desktop: no hardware, no model (tests, ``run --fake``).
    fake: bool = False
    #: JSON lines of scripted hands for ``fake`` (see ``tracker.fake``); None means no hands.
    fake_script: Path | None = None


class RateLimiter:
    """At most ``limit`` events in any ``window`` seconds."""

    def __init__(self, limit: int, window: float = 1.0) -> None:
        self.limit = limit
        self.window = window
        self._times: deque[float] = deque()

    def allow(self, now: float) -> bool:
        while self._times and now - self._times[0] >= self.window:
            self._times.popleft()
        if len(self._times) >= self.limit:
            return False
        self._times.append(now)
        return True


class Throttle:
    """Each key at most once every ``interval`` seconds."""

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self._last: dict[str, float] = {}

    def allow(self, key: str, now: float) -> bool:
        last = self._last.get(key)
        if last is not None and now - last < self.interval:
            return False
        self._last[key] = now
        return True


def overlay_state(view: EngineView, *, overlay: bool = True, dragging: bool = False) -> OverlayState:
    """What the reticle shows for the engine's ``view()``.

    ``dragging``: a press has turned into a drag (the view only says "pressed").
    """
    if not overlay:
        return OverlayState()
    if view.state == "calibrating":
        if view.calibration_target is None:
            return OverlayState()
        return OverlayState("calibrate", cursor=view.calibration_target, progress=view.calibration_progress)
    if not view.hand_visible or view.cursor is None:
        return OverlayState()
    mode: OverlayMode
    if view.grabbing:
        mode = "resize" if view.helper is not None else "grab"
    elif view.scrolling:
        mode = "scroll"
    elif view.pressed:
        mode = "drag" if dragging else "press"
    elif view.state == "active":
        mode = "point"
    elif view.engage_progress > 0:
        mode = "engaging"
    else:
        mode = "idle"
    return OverlayState(
        mode,
        cursor=view.cursor,
        helper=view.helper if mode == "resize" else None,
        pinch=float(view.pinch),
        progress=float(view.engage_progress) if mode == "engaging" else 0.0,
    )


@dataclass(frozen=True)
class _LastFrame:
    """The last tracked frame: its time on the frame clock, when it arrived on the runtime's, its size."""

    t: float
    arrived: float
    width: int
    height: int


class _CameraRequest:
    """A pause or resume waiting for the loop thread to close or reopen the camera."""

    def __init__(self, *, resume: bool) -> None:
        self.done = threading.Event()
        #: A resume: its answer says why the camera could not open (a pause's answer never does).
        self.resume = resume
        #: (error code, message) when reopening the camera failed.
        self.error: tuple[protocol.ErrorCode, str] | None = None
        #: The command stopped waiting (it answered ``pending``), so a failure has to go out as an event.
        self.abandoned = False

    @property
    def awaits_error(self) -> bool:
        """A resume still waiting: a camera that fails to open is in its answer, not an event."""
        return self.resume and not self.abandoned

    def finish(self, error: tuple[protocol.ErrorCode, str] | None) -> None:
        self.error = error
        self.done.set()


class _PausedWhileOpening(Exception):
    """A pause arrived while the camera was opening; the camera that opened was closed again."""


class HandsRuntime:
    """See the module docstring. ``cli.cmd_run`` builds it with ``(options, writer)``.

    The keyword-only factories are test hooks; by default they make the real
    camera, tracker, overlay and desktop, or their fakes when ``options.fake``.
    """

    def __init__(
        self,
        options: RuntimeOptions,
        writer: EventSink,
        *,
        desktop_factory: Callable[[], Desktop] | None = None,
        camera_factory: Callable[[], Camera] | None = None,
        tracker_factory: Callable[[], Tracker] | None = None,
        overlay_factory: Callable[[], Overlay] | None = None,
    ) -> None:
        self.options = options
        self._writer = writer
        self._desktop_factory = desktop_factory or self._default_desktop
        self._camera_factory = camera_factory or self._default_camera
        self._tracker_factory = tracker_factory or self._default_tracker
        self._overlay_factory = overlay_factory or self._default_overlay
        self._clock: Callable[[], float] = clock.now
        #: Set on a fatal error; ``cli.cmd_run`` exits with it.
        self.exit_code: int | None = None

        self._lock = threading.RLock()
        self._lifecycle = threading.Lock()
        self._stop_event = threading.Event()
        self._wake = threading.Event()
        self._start_done = threading.Event()
        self._started = False
        self._stopped = False

        self._settings = HandsSettings()
        self._desktop: Desktop | None = None
        self._mapper: ScreenMapper | None = None
        self._engine: GestureEngine | None = None
        self._executor: Executor | None = None
        #: None until started; a NullOverlay after it failed.
        self._overlay: Overlay | None = None
        self._overlay_failed = False
        self._tracker: Tracker | None = None
        self._camera: Camera | None = None
        self._camera_info: CameraInfo | None = None
        self._loop_thread: threading.Thread | None = None
        self._loop_alive = False
        #: ``start()`` is opening the camera (before the loop runs): a pause or resume waits for that open.
        self._start_opening = False

        # The protocol state is derived: error > paused > starting (no camera yet) > the engine's.
        self._failed = False
        self._paused = False
        self._ready = False
        self._engine_state: protocol.HandsState = "idle"
        self._published: protocol.HandsState | None = None
        # Commands that came before the engine existed; a calibration also waits for the first frame (its clock).
        self._wish_engage = False
        self._wish_calibrate = False
        self._requests: list[_CameraRequest] = []

        self._gesture_limit = RateLimiter(GESTURES_PER_S, 1.0)
        self._error_throttle = Throttle(ERROR_REPEAT_S)
        self._frame_times: deque[float] = deque()
        self._dragging = False
        self._last_visible: tuple[float, OverlayState] | None = None
        #: None until the open camera's first frame was tracked.
        self._last_frame: _LastFrame | None = None
        #: The frame rate the last ``ready`` gave.
        self._announced_fps: float | None = None
        # Loop-thread state.
        self._stall_logged = False
        self._num_hands = 1
        self._awake = False
        self._desktop_blocked = False
        self._next_display_check = 0.0
        self._next_desktop_check = 0.0
        self._display_error_logged = False

        from .keyboard.controller import KeyboardController, KeyboardDeps  # lazy: with the keyboard off nobody loads it

        # The air keyboard (DESIGN-KEYBOARD.md 3.8). It is only ever called with ``self._lock`` held and reaches back
        # through these, so it holds no reference to the runtime and a test can run it with none.
        self._kb = KeyboardController(
            KeyboardDeps(
                data_dir=options.data_dir,
                desktop=lambda: self._desktop,
                overlay=lambda: self._overlay,
                displays=lambda: self._mapper.displays if self._mapper is not None else [],
                ready=self._kb_ready,
                paused=lambda: self._paused,
                desktop_blocked=lambda: self._desktop_blocked,
                overlay_enabled=lambda: self.options.overlay and self._settings.overlay,
                fps=lambda: self._tracked_fps(self._clock()),
                emit=self._emit,
                show=self._show,
                pointer_off=self._kb_pointer_off,
                pointer_reset=self._kb_pointer_reset,
                report=self._report,
                clock=lambda: self._clock(),
            )
        )

    # -- default factories ----------------------------------------------------------------

    def _default_desktop(self) -> Desktop:
        if self.options.fake:
            from .desktop.fake import FakeDesktop

            return FakeDesktop()
        from .desktop import create_desktop

        return create_desktop()

    def _default_camera(self) -> Camera:
        o = self.options
        if o.fake:
            from .camera.fake import FakeCamera

            return FakeCamera(o.width, o.height, float(o.fps) if o.fps > 0 else 30.0)
        from .camera import create_camera

        return create_camera(o.camera, width=o.width, height=o.height, fps=o.fps)

    def _default_tracker(self) -> Tracker:
        o = self.options
        if o.fake:
            from .tracker.fake import FakeTracker, load_script

            if o.fake_script is None:
                return FakeTracker()
            try:
                return FakeTracker(load_script(o.fake_script))
            except (OSError, ValueError) as exc:
                raise TrackerError("tracker_failed", f"cannot use the fake script: {exc}") from exc
        from . import models
        from .tracker import create_tracker

        if not models.is_installed(o.data_dir):
            raise TrackerError(
                "model_missing",
                f"The hand model is not installed ({models.model_path(o.data_dir)}).",
                "Run /jarvis setup hands to download it.",
            )
        return create_tracker(models.model_path(o.data_dir))

    def _default_overlay(self) -> Overlay:
        if self.options.fake:
            return NullOverlay()
        from .overlay import create_overlay

        return create_overlay()

    # -- lifecycle --------------------------------------------------------------------------

    def start(self) -> None:
        """Bring hand control up, then return with the loop running (or after a fatal error)."""
        with self._lifecycle:
            if self._started or self._stopped or self._stop_event.is_set():
                self._start_done.set()
                return
            self._started = True
        try:
            self._start()
        except Exception as exc:
            log.exception("hand control failed to start")
            self._release_everything()
            self._fatal("internal", f"Hand control failed to start: {exc_text(exc)}")
        finally:
            self._start_done.set()
            if self._stopped:  # stop() ran while we were starting: let go of what was made since
                self._release_resources()

    def request_stop(self) -> None:
        """Ask everything to wind down; any thread, any time. ``wait()`` then returns."""
        if not self._stop_event.is_set():
            self._stop_event.set()
            log.info("hand control stopping")
        self._wake.set()

    def wait(self) -> None:
        """Until ``request_stop`` (a command, the watchdog, a signal) or a fatal error."""
        while not self._stop_event.wait(0.2):
            pass

    def stop(self) -> None:
        """Release the mouse, the camera, the model, the overlay and the desktop. Idempotent; never raises."""
        with self._lifecycle:
            if self._stopped:
                return
            self._stopped = True
            started = self._started
        self.request_stop()
        with self._lock:
            self._kb.close("command")  # no key goes out while the loop winds down
        try:
            if started and not self._start_done.wait(START_WAIT_S):
                log.warning("start() is still busy; stopping anyway")
            thread = self._loop_thread
            if thread is not None and thread is not threading.current_thread():
                thread.join(STOP_JOIN_S)
                if thread.is_alive():
                    log.warning("the hand loop did not stop within %.0f s", STOP_JOIN_S)
        except Exception:
            log.exception("waiting for the hand loop failed")
        self._release_resources()
        log.info("hand control stopped")

    def _start(self) -> None:
        sys.setswitchinterval(min(sys.getswitchinterval(), SWITCH_INTERVAL_S))
        with self._lock:
            self._published = "starting"
            self._emit(protocol.state("starting"))

        try:
            desktop = self._desktop_factory()
        except UnsupportedPlatform as exc:
            # No hint: the message already says it, and no restart or setup step can change the platform.
            self._fatal("unsupported_platform", str(exc) or UNSUPPORTED_MESSAGE)
            return
        with self._lock:
            self._desktop = desktop
        if self._stop_event.is_set():
            return

        displays = desktop.displays()
        calibration = load_calibration(self.options.data_dir)
        with self._lock:
            mapper = ScreenMapper(displays, self._settings, calibration)
            engine = GestureEngine(self._settings, mapper, double_click=_double_click(desktop))
            self._mapper, self._engine = mapper, engine
            if self._wish_engage:
                engine.engage()
            self._drain(engine)
        log.info(
            "displays: %s; %s",
            ", ".join(f"{d.id} {d.name} {d.rect.rounded()}{' (virtual)' if d.virtual else ''}" for d in displays),
            "calibrated" if calibration is not None else "default box",
        )
        self._next_display_check = self._clock() + DISPLAY_REFRESH_S

        fps = self.options.fps if self.options.fps > 0 else 30
        executor = Executor(
            desktop,
            displays=lambda: mapper.used,
            cursor_gain=lambda: mapper.cursor_gain,
            on_user_input=self._on_user_input,
            on_error=self._on_executor_error,
            frame_interval=1.0 / fps,
        )
        with self._lock:
            self._executor = executor
        executor.start()

        self._maybe_start_overlay()
        if self._stop_event.is_set():
            return

        try:
            tracker = self._tracker_factory()
        except TrackerError as exc:
            self._fatal(exc.code, exc.message, exc.hint)
            return
        except Exception as exc:  # e.g. the tracker's module or MediaPipe failed to import
            log.exception("the hand tracker could not be created")
            self._fatal("tracker_failed", f"The hand tracker could not start: {exc_text(exc)}")
            return
        with self._lock:
            self._tracker = tracker
            self._num_hands = _num_hands(tracker)
        if self._stop_event.is_set():
            return

        with self._lock:
            paused = self._paused
            self._start_opening = not paused
        try:
            if not paused:
                self._open_camera()
        except _PausedWhileOpening:
            # "Pause" came while the camera opened (seconds, on some webcams): start paused, not failed. The
            # loop answers the commands that waited, as it does for every camera change after this one.
            log.info("paused while the camera was opening: it is off again")
            self._countermand_resumes()
        except CameraError as exc:
            with self._lock:
                # A pause came while the camera opened, then the open failed: the camera is off, as the pause
                # wanted. Start paused, not failed, as the loop does after a reopen that fails.
                paused = self._paused
                requests, self._requests = self._requests, []
                if paused and not any(request.awaits_error for request in requests):
                    self._report(exc.code, exc.message, exc.hint, always=True)
            for request in requests:
                request.finish((exc.code, exc.message))
            if not paused:
                self._fatal(exc.code, exc.message, exc.hint)
                return
            log.warning("paused while the camera was opening, which then failed: %s: %s", exc.code, exc.message)
        finally:
            with self._lock:
                self._start_opening = False
        if self._stop_event.is_set():
            self._finish_requests(None)
            return

        with self._lock:
            self._sync_state()
            self._loop_alive = True
        thread = threading.Thread(target=self._loop, name="hands-loop", daemon=True)
        self._loop_thread = thread
        thread.start()

    def _release_resources(self) -> None:
        """Close whatever exists, each exactly once. Buttons first."""
        with self._lock:
            self._kb.close("command")  # while the desktop and the overlay still exist to be released and blanked
            executor, self._executor = self._executor, None
            camera, self._camera = self._camera, None
            tracker, self._tracker = self._tracker, None
            overlay, self._overlay = self._overlay, None
            desktop, self._desktop = self._desktop, None
        if executor is not None:
            for step in (executor.release_all, executor.stop):
                _quietly(step, "releasing the mouse")
        for name, resource in (("camera", camera), ("tracker", tracker), ("overlay", overlay), ("desktop", desktop)):
            if resource is not None:
                _quietly(resource.close, f"closing the {name}")

    def _release_everything(self) -> None:
        """Every held button up, now. Never while holding the runtime lock (the executor may wait on it)."""
        executor = self._executor
        if executor is not None:
            _quietly(executor.release_all, "releasing the mouse")

    # -- the frame loop -----------------------------------------------------------------------

    def _loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                self._reconcile_camera()
                self._housekeeping(self._clock())
                camera = self._camera
                if camera is None:  # paused
                    self._wake.wait(PAUSED_POLL_S)
                    self._wake.clear()
                    continue
                asked = self._clock()
                captured = camera.read(READ_TIMEOUT_S)
                if self._stop_event.is_set():
                    break
                if captured is None:
                    now = self._clock()
                    self._no_frame(now)
                    if now - asked < READ_TIMEOUT_S / 2:  # it did not wait: do not spin on it
                        self._stop_event.wait(NO_FRAME_BACKOFF_S)
                    continue
                arrived = self._clock()
                tracker = self._tracker
                if tracker is None or self._desktop_blocked:
                    continue
                frame = tracker.process(captured.image, captured.t)
                self._process(frame, self._clock(), arrived=arrived)
        except (CameraError, TrackerError) as exc:
            self._release_everything()
            if not self._stop_event.is_set():
                self._fatal(exc.code, exc.message, exc.hint)
        except Exception as exc:
            self._release_everything()
            if self._stop_event.is_set():
                log.debug("the hand loop ended while stopping: %r", exc)
            else:
                log.exception("the hand loop failed")
                self._fatal("internal", f"Hand tracking failed: {exc_text(exc)}")
        finally:
            self._release_everything()
            self._keep_awake(False)
            self._close_camera()
            with self._lock:
                self._loop_alive = False
                requests, self._requests = self._requests, []
            for request in requests:
                request.finish(None)
            log.info("hand loop stopped")

    def _process(self, frame: Frame, now: float, *, arrived: float | None = None) -> None:
        """One frame through the engine; ``arrived`` is None for a stall's empty frame (not tracked, not counted)."""
        with self._lock:
            if arrived is not None:
                self._frame_times.append(now)
                while self._frame_times and now - self._frame_times[0] > FPS_WINDOW_S:
                    self._frame_times.popleft()
                self._last_frame = _LastFrame(frame.t, arrived, frame.width, frame.height)
                if self._stall_logged:
                    self._stall_logged = False
                    log.info("the camera sends frames again")
            engine, executor = self._engine, self._executor
            if engine is None or executor is None or self._paused or self._desktop_blocked:
                return
            if self._wish_calibrate and arrived is not None:
                self._wish_calibrate = False
                engine.start_calibration(frame.t)
            kb = self._kb
            if kb.active:
                kb.frame(frame, now)  # the pointer is off: the engine is not called
                view = result = None
            else:
                actions = engine.update(kb.pointer_frame(frame))  # an empty frame while the pointer is quarantined
                executor.submit(actions)
                self._drain(engine)
                view = engine.view()
                result = engine.take_calibration_result()
                if not view.pressed:
                    self._dragging = False
                # Under the lock, so a pause or a config that hides the reticle can never be overdrawn by this frame.
                self._show(self._overlay_for(view, engine.engaged, now))
        if result is not None:
            self._calibrated(result)
        self._set_num_hands(2 if (kb.wants_two_hands or (view is not None and view.grabbing)) else 1)
        self._keep_awake(kb.active or (view is not None and (engine.engaged or view.state == "calibrating")))

    def _no_frame(self, now: float) -> None:
        """A read timed out: past ``hold_s`` without a frame, the engine sees an empty one (see the module docstring).

        Without it the engine's clock stands still for the whole stall, and
        with it any press or grab and the engagement.
        """
        with self._lock:
            last = self._last_frame
            if last is None or now - last.arrived <= self._settings.hold_s:
                return
            frame = Frame(self._frame_time(now), (), last.width, last.height)
            if not self._stall_logged:
                self._stall_logged = True
                log.info("the camera sent no frame for %.1f s: no hand until it does", now - last.arrived)
        self._process(frame, now)

    def _frame_time(self, now: float) -> float:
        """``now`` (runtime clock) on the frame clock; only once a frame was tracked (runtime lock held)."""
        last = self._last_frame
        assert last is not None
        return last.t + max(0.0, now - last.arrived)

    def _overlay_for(self, view: EngineView, engaged: bool, now: float) -> OverlayState:
        """The reticle for this frame; a hand missed for a frame or two does not blink it while engaged."""
        state = overlay_state(view, overlay=self._settings.overlay, dragging=self._dragging)
        if state.mode != "hidden":
            self._last_visible = (now, state)
        elif (
            engaged
            and self._settings.overlay
            and view.state == "active"
            and self._last_visible is not None
            and now - self._last_visible[0] <= self._settings.hold_s
        ):
            return self._last_visible[1]
        return state

    def _reconcile_camera(self) -> None:
        """Open or close the camera to match ``pause`` / ``resume``, and answer the commands waiting on it."""
        with self._lock:
            requests, self._requests = self._requests, []
            paused = self._paused
        error: tuple[protocol.ErrorCode, str] | None = None
        failed = False  # the camera itself failed, as opposed to a pause countermanding the resume
        if paused and self._camera is not None:
            self._close_camera()
            self._keep_awake(False)  # here, on the loop thread: a pause may last all night
            log.info("paused: the camera is off")
        elif not paused and self._camera is None and not self._stop_event.is_set():
            try:
                self._open_camera()
            except _PausedWhileOpening:
                # The camera is already off again and the state is "paused": only the resume needs an answer.
                log.info("paused while the camera was opening: it is off again")
                error = COUNTERMANDED
            except CameraError as exc:
                log.warning("could not reopen the camera: %s: %s", exc.code, exc.message)
                error, failed = (exc.code, exc.message), True
            except Exception as exc:
                log.exception("could not reopen the camera")
                error = ("internal", f"Could not reopen the camera: {exc_text(exc)}")
                failed = True
            if failed and error is not None:
                with self._lock:
                    self._paused = True
                    # A pause and a resume that came while it opened waited on this open too: the next pass
                    # would find the camera off and answer that resume "ok".
                    requests += self._requests
                    self._requests = []
                    # As a pause does: no calibration or engage asked for meanwhile waits for the next resume.
                    self._disengage_all("command")
                    self._sync_state()
                    # No resume left waiting for the answer (none, or every one gave up): say it as an
                    # event, so a camera a resume could not reopen still reaches the status line.
                    if not any(request.awaits_error for request in requests):
                        # Never throttled: a resume that answered "pending" waits for this one.
                        self._report(error[0], error[1], always=True)
        for request in requests:
            request.finish(error)
        if failed:
            self._release_everything()  # after the answers: a resume deciding on its answer is not kept waiting

    def _finish_requests(self, error: tuple[protocol.ErrorCode, str] | None) -> None:
        """Answer every command waiting on the camera (``start()`` only: after it, the loop does)."""
        with self._lock:
            requests, self._requests = self._requests, []
        for request in requests:
            request.finish(error)

    def _countermand_resumes(self) -> None:
        """``start()``'s open was undone by a pause: a resume still waiting on it was countermanded.

        Only while the pause still holds: a resume after it is the loop's to
        serve (it opens the camera again). A pause waiting ignores the error.
        """
        with self._lock:
            if not self._paused:
                return
        self._finish_requests(COUNTERMANDED)

    def _open_camera(self) -> CameraInfo:
        """Open the camera and announce it. Raises ``_PausedWhileOpening`` when a pause came meanwhile."""
        camera = self._camera_factory()
        try:
            info = camera.open()
        except BaseException:
            _quietly(camera.close, "closing the camera")
            raise
        with self._lock:
            # An open takes seconds (11 to 21 on some webcams): a pause may have come while it ran, and then
            # the camera must not stream or announce itself ready for the rest of this loop pass.
            paused = self._paused
            if not paused:
                self._camera = camera
        if paused:
            _quietly(camera.close, "closing the camera")
            raise _PausedWhileOpening
        with self._lock:
            self._camera_info = info
            self._last_frame = None  # a new camera: its frames set the clock again
            if self._engine is not None:
                # No frames came while it was closed: a hand that was up then must hold again to engage.
                self._engine.reset_tracks()
            executor = self._executor
            if executor is not None and info.fps > 0 and math.isfinite(info.fps):
                executor.frame_interval = 1.0 / info.fps
            log.info("camera: %s (%s) %dx%d at %.1f fps", info.name, info.backend, info.width, info.height, info.fps)
            self._ready = True
            self._announce_ready()
            self._sync_state()
        return info

    def _announce_ready(self) -> None:
        """A ``ready`` for the open camera and today's displays (runtime lock held)."""
        info = self._camera_info
        if info is None:
            return
        fps = round(info.fps, 1) if math.isfinite(info.fps) else 0.0
        self._announced_fps = fps
        self._emit(protocol.ready(info.name, info.width, info.height, fps, self._display_dicts()))

    def _close_camera(self) -> None:
        with self._lock:
            camera, self._camera = self._camera, None
        if camera is not None:
            _quietly(camera.close, "closing the camera")

    def _housekeeping(self, now: float) -> None:
        if now >= self._next_display_check:
            self._next_display_check = now + DISPLAY_REFRESH_S
            self._refresh_displays()
            self._refresh_camera_rate()
        if now >= self._next_desktop_check:
            self._next_desktop_check = now + INPUT_DESKTOP_CHECK_S
            self._check_input_desktop()
        self._maybe_start_overlay()

    def _refresh_displays(self) -> None:
        desktop, mapper = self._desktop, self._mapper
        if desktop is None or mapper is None:
            return
        try:
            displays = desktop.displays()
        except Exception:
            if not self._display_error_logged:
                log.exception("could not read the displays; keeping the last layout")
                self._display_error_logged = True
            return
        self._display_error_logged = False
        if not displays or displays == mapper.displays:
            return
        mapper.set_displays(displays)
        log.info(
            "displays changed: %s; hand control uses %s",
            ", ".join(f"{d.id} {d.rect.rounded()}" for d in displays),
            ", ".join(str(d.id) for d in mapper.used),
        )
        with self._lock:
            if self._camera is not None:  # the mod names the displays from the last ready
                self._announce_ready()

    def _refresh_camera_rate(self) -> None:
        """Follow the measured frame rate (a camera's info gives the requested one until frames flow).

        The executor glides each move over one frame interval: at the requested
        30 fps against a dim room's 15, the cursor would move half of each
        interval and stand still for the rest.
        """
        camera = self._camera
        if camera is None:
            return
        try:
            info = camera.info
        except Exception as exc:  # noqa: BLE001 - only a refinement; the last known rate stays
            log.debug("camera.info failed: %r", exc)
            return
        if info is None or not (math.isfinite(info.fps) and info.fps > 0):
            return
        with self._lock:
            if self._camera is not camera:  # closed or replaced meanwhile
                return
            self._camera_info = info
            executor = self._executor
            if executor is not None:
                executor.frame_interval = 1.0 / info.fps
            announced = self._announced_fps
            if not announced or abs(info.fps - announced) > RATE_ANNOUNCE_CHANGE * announced:
                log.info("the camera delivers %.1f fps", info.fps)
                self._announce_ready()

    def _check_input_desktop(self) -> None:
        """Lock screen or UAC prompt: nothing we inject arrives, so let go and wait for it to end."""
        check = getattr(self._desktop, "input_desktop_ok", None)
        if check is None:
            return
        try:
            ok = bool(check())
        except Exception as exc:  # noqa: BLE001 - an unanswerable check changes nothing; ask again soon
            log.debug("input_desktop_ok failed: %r", exc)
            return
        if ok != self._desktop_blocked:
            return
        executor = self._executor
        if ok:
            log.info("the desktop is back; hand control resumes")
            self._desktop_blocked = False
            if executor is not None:
                executor.set_desktop_blocked(False)
            engine = self._engine
            if engine is not None:  # the gap was a break in tracking: engaging takes the full hold again
                engine.reset_tracks()
            return
        log.info("the input desktop is not ours (lock screen or a UAC prompt); hand control waits")
        with self._lock:
            self._desktop_blocked = True
            self._kb.close("desktop_locked")
            self._disengage_all("desktop_locked")
            self._last_visible = None
            self._show(OverlayState())
        if executor is not None:
            # The cursor cannot even be read while another desktop has the input: no tick may try.
            executor.set_desktop_blocked(True)
        self._release_everything()
        self._keep_awake(False)

    def _keep_awake(self, on: bool) -> None:
        """Keep the display on while engaged. Loop thread only: Windows keeps this per thread."""
        if on == self._awake:
            return
        self._awake = on
        keep_awake = getattr(self._desktop, "keep_awake", None)
        if keep_awake is None:
            return
        try:
            keep_awake(on)
        except Exception:
            log.warning("could not %s the display", "keep awake" if on else "release", exc_info=True)

    def _set_num_hands(self, n: int) -> None:
        tracker = self._tracker
        if n == self._num_hands or tracker is None:
            return
        self._num_hands = n  # also on failure: do not retry every frame
        try:
            tracker.set_num_hands(n)
            log.debug("tracking %d hand(s)", n)
        except Exception:
            log.warning("could not switch the tracker to %d hand(s)", n, exc_info=True)

    def _calibrated(self, homography: np.ndarray) -> None:
        mapper = self._mapper
        try:
            path = save_calibration(self.options.data_dir, homography)
            log.info("calibration saved to %s", path)
        except (OSError, ValueError) as exc:
            log.error("could not save the calibration: %s", exc)
            with self._lock:
                self._report("internal", f"Could not save the calibration: {exc}")
        if mapper is not None:
            try:
                mapper.set_homography(homography)
            except ValueError as exc:
                log.error("the calibration is not usable: %s", exc)

    # -- overlay --------------------------------------------------------------------------------

    def _maybe_start_overlay(self) -> None:
        with self._lock:
            wanted = (
                self.options.overlay
                and self._settings.overlay
                and self._overlay is None
                and not self._overlay_failed
                and not self._stop_event.is_set()
            )
        if not wanted:
            return
        overlay: Overlay | None = None
        try:
            overlay = self._overlay_factory()
            overlay.start()
        except Exception as exc:  # noqa: BLE001 - OverlayError or anything else: hand control carries on without it
            if overlay is not None:
                _quietly(overlay.close, "closing the overlay")
            self._overlay_broke(exc)
            return
        with self._lock:
            stopped = self._stopped  # stop() released everything while this one was starting
            if not stopped:
                self._overlay = overlay
        if stopped:
            _quietly(overlay.close, "closing the overlay")
            return
        log.info("overlay: %s", type(overlay).__name__)

    def _show(self, state: OverlayState) -> None:
        """Draw the reticle (runtime lock held: ``Overlay.show`` is cheap and keeps only the newest state)."""
        overlay = self._overlay
        if overlay is None:
            return
        try:
            overlay.show(state)
        except Exception as exc:  # noqa: BLE001 - _overlay_broke logs it; the reticle is optional
            _quietly(overlay.close, "closing the overlay")
            self._overlay_broke(exc)

    def _overlay_broke(self, exc: Exception) -> None:
        log.warning("the overlay failed (%s); hand control carries on without it", exc_text(exc))
        detail = exc_text(exc, typed=False)  # before the close below ends the scrub: an open session never reports text
        with self._lock:
            first = not self._overlay_failed
            self._overlay_failed = True
            self._overlay = NullOverlay()
            self._kb.close("no_overlay")  # after the swap, so that its blanking draw goes to the null overlay
            if first:
                self._report(
                    "overlay_failed",
                    f"The on-screen reticle could not be shown ({detail}); hand control works without it.",
                    always=True,
                )

    # -- engine plumbing (runtime lock held) ----------------------------------------------------

    def _drain(self, engine: GestureEngine) -> None:
        """The engine's events as protocol events, in order."""
        for event in engine.take_events():
            if event.kind == "gesture":
                self._gesture(event.value)
            elif event.kind == "calibration":
                if event.value in CALIBRATION_STEPS:
                    self._emit(protocol.calibration(cast("protocol.CalibrationStep", event.value)))
                else:
                    log.debug("unknown calibration step %r", event.value)
            elif event.kind == "state":
                if event.value in ("idle", "active", "calibrating"):
                    self._engine_state = cast("protocol.HandsState", event.value)
                self._sync_state()

    def _gesture(self, name: str) -> None:
        if name == "drag_start":
            self._dragging = True
        elif name in ("drag_end", "click", "double_click", "right_click", "disengage", "user_input"):
            self._dragging = False
        if name not in GESTURE_NAMES:
            log.debug("unknown gesture %r", name)
            return
        if self._gesture_limit.allow(self._clock()):
            self._emit(protocol.gesture(cast("protocol.GestureName", name)))
        else:
            log.debug("gesture %s dropped (more than %d a second)", name, GESTURES_PER_S)

    def _disengage_all(self, reason: str) -> None:
        """Disengage and lift every button (runtime lock held); the caller then calls ``_release_everything``."""
        engine, executor = self._engine, self._executor
        actions: list[Action] = []
        self._wish_calibrate = False
        if engine is not None:
            if engine.state == "calibrating":
                engine.cancel_calibration()
            actions = engine.disengage(reason)
            self._drain(engine)
        if executor is not None:
            # Whatever the engine queued before it was told to let go is stale: it must not go out later.
            executor.flush()
            executor.submit([*actions, ReleaseAll()])
        self._dragging = False

    def _effective_state(self) -> protocol.HandsState:
        if self._failed:
            return "error"
        # A resume is "paused" until the camera is back.
        if self._paused or (self._ready and self._camera is None and not self._stop_event.is_set()):
            return "paused"
        if not self._ready:
            return "starting"
        return self._engine_state

    def _sync_state(self) -> None:
        if self._published is None:  # start() has not begun: nothing announced yet
            return
        state = self._effective_state()
        if state != self._published:
            self._published = state
            self._emit(protocol.state(state))

    def _fatal(self, code: protocol.ErrorCode, message: str, hint: str | None = None) -> None:
        with self._lock:
            if self._failed:
                return
            self._failed = True
            self._kb.close("camera")
            self.exit_code = EXIT_FATAL
            log.error("fatal: %s: %s", code, message)
            self._emit(protocol.error(code, message, hint, fatal=True))
            self._sync_state()
        self.request_stop()

    def _report(self, code: str, message: str, hint: str | None = None, *, always: bool = False) -> None:
        """A non-fatal error event, at most once per code every ERROR_REPEAT_S (runtime lock held)."""
        known = cast("protocol.ErrorCode", code if code in ERROR_CODES else "internal")
        if not self._error_throttle.allow(known, self._clock()) and not always:
            log.debug("error %s not repeated: %s", known, message)
            return
        self._emit(protocol.error(known, message, hint, fatal=False))

    def _emit(self, event: dict[str, Any]) -> None:
        try:
            self._writer.emit(event)
        except Exception:
            log.exception("could not emit %s", event.get("type"))

    def _display_dicts(self) -> list[dict[str, Any]]:
        mapper = self._mapper
        if mapper is None:
            return []
        used = {d.id for d in mapper.used}
        return [protocol.display_dict(d, d.id in used) for d in mapper.displays]

    def _tracked_fps(self, now: float) -> float:
        times = [t for t in self._frame_times if now - t <= FPS_WINDOW_S]
        if len(times) < 2 or times[-1] <= times[0]:
            return 0.0
        return (len(times) - 1) / (times[-1] - times[0])

    # -- executor callbacks (executor thread) ---------------------------------------------------

    def _on_user_input(self) -> None:
        """The real mouse moved: the engine lets go and disengages (its ``user_input`` gesture goes out)."""
        with self._lock:
            engine, executor = self._engine, self._executor
            if engine is None or executor is None:
                return
            executor.submit(engine.on_user_input())
            self._drain(engine)

    def _on_executor_error(self, code: str, message: str) -> None:
        with self._lock:
            self._report(code, message)

    # -- commands (control server threads) ------------------------------------------------------

    def handle_command(self, name: str, body: dict[str, Any]) -> dict[str, Any]:
        """A validated command from the mod. Thread-safe; works before, during and after ``start()``."""
        try:
            if name == "status":
                return self._status()
            handler = {
                "config": self._cmd_config,
                "pause": self._cmd_pause,
                "resume": self._cmd_resume,
                "engage": self._cmd_engage,
                "disengage": self._cmd_disengage,
                "calibrate": self._cmd_calibrate,
                "keyboard": self._cmd_keyboard,
            }.get(name)
            if handler is None:
                return protocol.error_response("bad_request", f"unknown command {name!r}")
            if self._stop_event.is_set():
                return protocol.error_response("internal", "hand control is stopping")
            response: dict[str, Any] = handler(body)
            return response
        except Exception as exc:
            log.exception("command %s failed", name)
            return protocol.error_response("internal", exc_text(exc))

    def _status(self) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            engine, mapper, tracker, info = self._engine, self._mapper, self._tracker, self._camera_info
            response: dict[str, Any] = {
                "ok": True,
                "state": self._effective_state(),
                "version": __version__,
                "platform": plat.name(),
                "engaged": bool(engine.engaged) if engine is not None else False,
                "fps": round(self._tracked_fps(now), 1),
                "inferMs": 0.0,
                "displays": self._display_dicts(),
                "settings": self._settings.status(),
            }
            keyboard = self._kb.status()
            if keyboard is not None:
                response["keyboard"] = keyboard
            if info is not None:
                response["camera"] = info.name
        if tracker is not None:
            try:
                response["inferMs"] = round(_non_negative(tracker.infer_ms), 2)
            except Exception:
                log.debug("infer_ms failed", exc_info=True)
        if mapper is not None:
            response["calibrated"] = mapper.calibrated
        else:
            response["calibrated"] = load_calibration(self.options.data_dir) is not None
        return response

    def _cmd_config(self, body: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            displays, overlay = self._settings.displays, self._settings.overlay
            try:
                self._settings.apply_config(body)
            except ValueError as exc:
                return protocol.error_response("bad_request", str(exc))
            if self._mapper is not None and self._settings.displays != displays:
                self._mapper.set_selection(self._settings.displays)
            if overlay and not self._settings.overlay:
                self._kb.close("no_overlay")
                self._last_visible = None
                self._show(OverlayState())
            log.info("settings: %s", self._settings.status() | {"displays": self._settings.displays})
        return protocol.ok_response()

    def _cmd_pause(self, body: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            again = self._paused
            if not again:
                self._paused = True
                self._kb.close("paused")
                self._disengage_all("command")
                self._sync_state()
                self._last_visible = None
                self._show(OverlayState())
            # A pause again waits as well: the first may have answered "pending" with the camera still opening.
            # With the camera off, the loop's next pass answers it at once.
            request = self._camera_request(resume=False)
        if not again:
            log.info("pause requested")
            self._release_everything()
        # The loop may be in the middle of a camera open: say the camera is still on its way out, so the mod
        # does not tell the user it is off while its light is still on.
        pending = request is not None and not self._wait(request, PAUSE_WAIT_S)
        return protocol.ok_response(pending=pending)

    def _cmd_resume(self, body: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if not self._paused:
                return protocol.ok_response()
            self._paused = False
            if not self._ready:  # the camera never opened yet: start() or the loop opens it now
                self._sync_state()
            request = self._camera_request(resume=True)
            # Nothing to wait on before start() gets to the camera, which it then opens: not on yet.
            opening_later = not self._ready and not self._failed and not self._stop_event.is_set()
        log.info("resume requested")
        if request is None:
            return protocol.ok_response(pending=opening_later)
        if not self._wait(request, RESUME_WAIT_S):
            # The camera is still opening (slower than RESUME_WAIT_S): the mod says so rather than "the
            # camera is on again", and a failure after this goes out as an error event.
            return protocol.ok_response(pending=True)
        if request.error is not None:
            return protocol.error_response(*request.error)
        return protocol.ok_response()

    def _camera_request(self, *, resume: bool) -> _CameraRequest | None:
        """A request for the loop (or ``start()``'s open) to reconcile the camera (runtime lock held).

        None when neither runs: the camera is not on and not on its way, so
        the command has nothing to wait for.
        """
        if not self._loop_alive and not self._start_opening:
            return None
        request = _CameraRequest(resume=resume)
        self._requests.append(request)
        self._wake.set()
        return request

    def _wait(self, request: _CameraRequest, timeout: float) -> bool:
        """Until the loop reconciled the camera. False (and the request is abandoned) when it took too long."""
        deadline = self._clock() + timeout
        while not request.done.wait(0.05):
            if self._stop_event.is_set() or self._clock() >= deadline:
                with self._lock:
                    if not request.done.is_set():
                        request.abandoned = True
                        return False
                return True
        return True

    def _cmd_engage(self, body: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._kb.close("command", quarantine=False)  # an explicit request wins over the quarantine (2.9)
            engine = self._engine
            if engine is None:
                self._wish_engage = True
            else:
                engine.engage()
                self._drain(engine)
        return protocol.ok_response()

    def _cmd_disengage(self, body: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._wish_engage = False
            engine, executor = self._engine, self._executor
            if engine is not None:
                actions = engine.disengage()
                if executor is not None:
                    executor.submit(actions)
                self._drain(engine)
        return protocol.ok_response()

    def _cmd_calibrate(self, body: dict[str, Any]) -> dict[str, Any]:
        action = body.get("action")
        with self._lock:
            engine = self._engine
            if action == "start":
                if self._paused:
                    return protocol.error_response("bad_request", "Hand control is paused; resume it to calibrate.")
                if self._ready and self._camera is None:  # a resume is reopening it: the state still says paused
                    return protocol.error_response(
                        "bad_request", "The camera is still turning back on; calibrate once it is on."
                    )
                self._kb.close("command", quarantine=False)
                if engine is None or self._last_frame is None:  # starts with the first frame, on its clock
                    self._wish_calibrate = True
                else:
                    engine.start_calibration(self._frame_time(self._clock()))
                    self._drain(engine)
            elif action == "cancel":
                self._wish_calibrate = False
                if engine is not None:
                    engine.cancel_calibration()
                    self._drain(engine)
            else:
                return protocol.error_response("bad_request", f"unknown calibrate action {action!r}")
        return protocol.ok_response()

    def _cmd_keyboard(self, body: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            response = self._kb.command(body)
        if self._kb.take_release():  # an open lifted every button; the executor's own lock is taken outside ours
            self._release_everything()
        return response

    # -- the air keyboard's view of the runtime (runtime lock held) -------------------------------

    def _kb_ready(self) -> bool:
        """The engine exists and the camera has been on; a pause counts, so that it is refused as "paused"."""
        return self._engine is not None and self._ready and (self._camera is not None or self._paused)

    def _kb_pointer_off(self) -> None:
        """A session opens: the pointer lets go of everything and forgets its tracks (the caller then releases)."""
        self._disengage_all("keyboard")
        if self._engine is not None:
            self._engine.reset_tracks()

    def _kb_pointer_reset(self) -> None:
        if self._engine is not None:
            self._engine.reset_tracks()


def _double_click(desktop: Desktop) -> tuple[float, int, int]:
    try:
        seconds, width, height = desktop.double_click()
        return float(seconds), int(width), int(height)
    except Exception:
        log.warning("could not read the double-click settings; using Windows' defaults", exc_info=True)
        return DEFAULT_DOUBLE_CLICK


def _num_hands(tracker: Tracker) -> int:
    try:
        return int(tracker.num_hands)
    except Exception:  # noqa: BLE001 - a tracker that cannot say is tracking the default
        return 1


def _non_negative(value: float) -> float:
    value = float(value)
    return value if math.isfinite(value) and value > 0 else 0.0


def _quietly(step: Callable[[], object], what: str) -> None:
    try:
        step()
    except Exception:
        log.exception("%s failed", what)
