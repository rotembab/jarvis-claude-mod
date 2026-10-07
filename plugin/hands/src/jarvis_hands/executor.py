"""Applies the engine's actions to the desktop: smooth cursor, buttons, wheel, windows, safety.

The engine runs at the camera's rate (30 fps); the executor ticks at 120 Hz
and moves the cursor linearly from the previous target to the new one over
one frame interval, so the cursor glides instead of stepping (at the cost of
one frame of latency). Every action that carries a point first puts the
cursor exactly there, so a press lands where the engine decided.

Safety rules, in order of importance:

- **Never leave a button down.** ``ReleaseAll``, ``stop()``, any exception in
  a tick and an atexit hook all release every held button and end any grab.
  A failing tick also drops what was queued (replaying it later, say after
  the PC is unlocked, would be worse), and a streak of failures is logged
  once with its traceback and then once a minute, not 120 times a second.
- **The real mouse wins.** Each tick, while the executor owns the cursor, it
  reads the cursor and compares it with where it last put it (read back
  after each move, so the OS clamping a point into a monitor is not mistaken
  for the user). More than ``user_input_px`` off means the user moved the
  mouse. While the hand moves the cursor, each tick moves it again and so
  overwrites what the user did since the last one, so the per-tick foreign
  motion is also summed over ``USER_INPUT_WINDOW_S`` (decaying); that sum
  past ``user_input_px`` counts the same. Then: release everything, call
  ``on_user_input`` (the runtime disengages the engine), and drop every
  action except ``ReleaseAll`` for ``suspend_s``.
  The engine computes a frame on another thread, so a batch made just before
  it was told to disengage can still arrive; a short time-based quiet period
  drops those stale actions, while a deliberate re-engagement (a palm held
  for half a second, or a spoken command) always comes later than that.
- **A window we may not move is left alone.** ``InputBlocked`` (an
  administrator's window, UIPI) reports ``input_blocked`` once per window
  through ``on_error`` and turns the rest of that grab into a no-op.

Windows: a grab records the window under the cursor and raises it once. A
maximized window is restored on the first real drag (not on the grab, so a
fist that lets go without moving changes nothing) and placed the way Windows
does it: the cursor keeps its relative x across the window and sits at most
``TITLE_GRAB_PX`` below its top edge. Drags move the grab-time rect by the
cursor's displacement; a two-hand resize keeps the hands' first positions
and scales the rect by the change in their separation. Throws move the
window to the next display that way (same relative place, size scaled to
the work area, maximized again if it was), snap it to that half of its
display when there is none, maximize (up) or minimize (down).

Callbacks run on the executor's thread; they may call ``submit`` but must
not call ``tick``.
"""

from __future__ import annotations

import atexit
import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from .actions import (
    Action,
    Button,
    DragWindow,
    GrabWindow,
    MouseButton,
    MoveCursor,
    ReleaseAll,
    ReleaseWindow,
    ResizeWindow,
    Scroll,
    ThrowWindow,
)
from .clock import now as clock_now
from .desktop.base import Desktop, Display, InputBlocked, Window
from .geometry import Point, Rect, bounding_rect
from .mapping import display_in_direction

log = logging.getLogger(__name__)

#: A restored window's top edge sits at most this far above the cursor (the title bar's height).
TITLE_GRAB_PX = 40
#: A maximized window is restored once the drag has moved this far (Windows' drag threshold is 4 px).
RESTORE_DRAG_PX = 8.0
#: Seconds after a real-mouse override during which queued actions are dropped.
SUSPEND_S = 0.25
#: Seconds over which the user's own cursor motion is summed (it decays with this time constant).
USER_INPUT_WINDOW_S = 0.1
#: While ticks keep failing, at most one log record this often.
FAILURE_LOG_INTERVAL_S = 60.0

_ORIGIN = Point(0, 0)
_NO_RECT = Rect(0, 0, 0, 0)


@dataclass
class _Grab:
    window: Window | None
    grab_point: Point = _ORIGIN
    #: The rect drags move from, and the cursor position it belongs to.
    rect0: Rect = _NO_RECT
    ref: Point = _ORIGIN
    was_maximized: bool = False
    #: Still maximized (not yet restored by a drag).
    maximized: bool = False
    #: a0, b0 and the rect when the two-hand resize began.
    resize: tuple[Point, Point, Rect] | None = None
    last_rect: Rect | None = None


class Executor:
    def __init__(
        self,
        desktop: Desktop,
        *,
        displays: Callable[[], list[Display]],
        on_user_input: Callable[[], None] | None = None,
        on_error: Callable[[str, str], None] | None = None,
        frame_interval: float = 1 / 30,
        tick_hz: float = 120.0,
        user_input_px: int = 6,
        min_size: tuple[int, int] = (240, 160),
        clock: Callable[[], float] = clock_now,
        sleep: Callable[[float], None] = time.sleep,
        suspend_s: float = SUSPEND_S,
    ) -> None:
        self._desktop = desktop
        #: The displays hand control uses (resize bounds, throw targets).
        self._displays = displays
        self._on_user_input = on_user_input
        self._on_error = on_error
        self.frame_interval = frame_interval
        self.tick_hz = tick_hz
        self.user_input_px = user_input_px
        self.min_size = min_size
        self.suspend_s = suspend_s
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.RLock()
        self._queue_lock = threading.Lock()
        self._queue: list[tuple[float, Action]] = []
        #: Where we put the cursor (float, the interpolation base) and where the OS says it went.
        self._pos: Point | None = None
        self._last_set: tuple[int, int] | None = None
        #: The cursor as last read (or read back after a move), and the user's recent motion summed.
        self._seen_at: tuple[int, int] | None = None
        self._foreign = (0.0, 0.0)
        self._foreign_t = 0.0
        self._failures = 0
        self._failure_logged = -math.inf
        self._segment: tuple[Point, Point, float] | None = None
        #: Replaced, never mutated, so other threads can read it without the lock.
        self._held: frozenset[MouseButton] = frozenset()
        self._grab: _Grab | None = None
        self._blocked_reported: set[int] = set()
        self._suspended_until = -math.inf
        self._scroll_rest = [0.0, 0.0]
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._atexit_registered = False

    # -- public API ---------------------------------------------------------------------

    @property
    def held(self) -> frozenset[str]:
        """Mouse buttons the executor currently holds down."""
        return self._held

    @property
    def grabbing(self) -> bool:
        return self._grab is not None and self._grab.window is not None

    def submit(self, actions: list[Action], now: float | None = None) -> None:
        """Queue actions (any thread); the next tick applies them in order."""
        if not actions:
            return
        t = self._clock() if now is None else now
        with self._queue_lock:
            self._queue.extend((t, action) for action in actions)

    def tick(self, now: float | None = None) -> None:
        """One step: notice the real mouse, apply queued actions, advance the cursor glide."""
        now = self._clock() if now is None else now
        with self._lock:
            try:
                if not self._user_moved(now):
                    with self._queue_lock:
                        batch, self._queue = self._queue, []
                    for submitted, action in batch:
                        self._apply(action, submitted, now)
                    self._glide(now)
            except Exception as exc:  # noqa: BLE001 - whatever failed, let go of the buttons; _failed logs it
                self._failed(exc, now)
                with self._queue_lock:
                    self._queue.clear()
                self._release_all()
            else:
                if self._failures:
                    log.info("executor works again after %d failed steps", self._failures)
                    self._failures = 0

    def release_all(self) -> None:
        """Every held button up and any grab ended. Never raises."""
        if self._lock.acquire(timeout=1.0):
            try:
                self._release_all()
            finally:
                self._lock.release()
        else:  # a tick is stuck in a desktop call: release anyway rather than leave a button down
            self._release_all()

    def start(self) -> Executor:
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="hands-executor", daemon=True)
        self._thread.start()
        if not self._atexit_registered:
            atexit.register(self.stop)
            self._atexit_registered = True
        return self

    def stop(self) -> None:
        """Stop ticking and release everything. Idempotent; never raises."""
        try:
            self._stop_event.set()
            thread, self._thread = self._thread, None
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=1.0)
            self.release_all()
            if self._atexit_registered:
                atexit.unregister(self.stop)
                self._atexit_registered = False
        except Exception:
            log.exception("executor stop failed")

    # -- the loop -----------------------------------------------------------------------

    def _run(self) -> None:
        period = 1.0 / self.tick_hz
        deadline = self._clock()
        while not self._stop_event.is_set():
            started = self._clock()
            self.tick()
            # Due one period after the last tick was due, so a sleep that overshoots a little does not slow the
            # rate; but never sooner than half a period after this tick began, so after a slow tick or a long
            # oversleep (macOS's can be tens of ms) the missed ticks are dropped, not sent back to back.
            deadline = max(deadline + period, started + period / 2)
            delay = deadline - self._clock()
            if delay > 0:
                # Not Event.wait: on Windows its timeout rounds up to the 15.6 ms timer tick (about 64 Hz),
                # while time.sleep uses a high-resolution timer since Python 3.11.
                self._sleep(delay)

    def _failed(self, exc: Exception, now: float) -> None:
        self._failures += 1
        if self._failures == 1:
            log.exception("executor step failed; releasing everything")
            self._failure_logged = now
        elif now - self._failure_logged >= FAILURE_LOG_INTERVAL_S:
            log.error("executor steps still failing (%d in a row): %r", self._failures, exc)
            self._failure_logged = now

    def _user_moved(self, now: float) -> bool:
        if self._last_set is None:
            return False
        x, y = self._desktop.cursor()
        seen = self._seen_at or self._last_set
        decay = math.exp(-max(0.0, now - self._foreign_t) / USER_INPUT_WINDOW_S)
        fx, fy = self._foreign[0] * decay + x - seen[0], self._foreign[1] * decay + y - seen[1]
        self._foreign, self._foreign_t, self._seen_at = (fx, fy), now, (x, y)
        off = math.hypot(x - self._last_set[0], y - self._last_set[1])
        if off <= self.user_input_px and math.hypot(fx, fy) <= self.user_input_px:
            return False
        log.info("real mouse moved (%d, %d) -> (%d, %d); letting go", *self._last_set, x, y)
        self._release_all()
        self._pos = Point(x, y)
        self._last_set = self._seen_at = None
        self._foreign = (0.0, 0.0)
        self._suspended_until = now + self.suspend_s
        with self._queue_lock:
            self._queue.clear()
        if self._on_user_input is not None:
            try:
                self._on_user_input()
            except Exception:
                log.exception("on_user_input callback failed")
        return True

    def _apply(self, action: Action, submitted: float, now: float) -> None:
        if now < self._suspended_until and not isinstance(action, ReleaseAll):
            return
        if isinstance(action, MoveCursor):
            start = self._pos if self._pos is not None else Point(*self._desktop.cursor())
            self._segment = (start, Point(action.x, action.y), submitted)
        elif isinstance(action, Button):
            self._move_exact(Point(action.x, action.y))
            if action.down and action.button not in self._held:
                self._desktop.button(action.button, True)
                self._held = self._held | {action.button}
            elif not action.down and action.button in self._held:
                self._desktop.button(action.button, False)
                self._held = self._held - {action.button}
        elif isinstance(action, Scroll):
            self._move_exact(Point(action.x, action.y))
            self._scroll(action.dy, action.dx)
        elif isinstance(action, GrabWindow):
            self._move_exact(Point(action.x, action.y))
            self._grab_at(Point(action.x, action.y))
        elif isinstance(action, DragWindow):
            self._move_exact(Point(action.x, action.y))
            self._window_op(self._drag, Point(action.x, action.y))
        elif isinstance(action, ResizeWindow):
            self._move_exact(Point(action.ax, action.ay))
            self._window_op(self._resize, Point(action.ax, action.ay), Point(action.bx, action.by))
        elif isinstance(action, ReleaseWindow):
            self._grab = None
        elif isinstance(action, ThrowWindow):
            self._window_op(self._throw, action.direction)
            self._grab = None
        elif isinstance(action, ReleaseAll):
            self._release_all()

    # -- cursor -------------------------------------------------------------------------

    def _glide(self, now: float) -> None:
        if self._segment is None:
            return
        start, end, began = self._segment
        alpha = 1.0 if self.frame_interval <= 0 else min(1.0, max(0.0, (now - began) / self.frame_interval))
        self._move_to(start + (end - start).scale(alpha))
        if alpha >= 1.0:
            self._segment = None

    def _move_exact(self, p: Point) -> None:
        self._segment = None
        self._move_to(p, exact=True)

    def _move_to(self, p: Point, *, exact: bool = False) -> None:
        """Put the cursor on ``p``. A glide step trusts where we last put it; an exact move checks
        the real cursor, which a nudge too small to count as the user may have moved."""
        self._pos = p
        target = p.rounded()
        if exact or self._last_set is None:
            live = self._desktop.cursor()
            if live == target:
                if self._last_set is not None:
                    self._last_set = self._seen_at = live
                return
        elif target == self._last_set:
            return
        self._desktop.move_cursor(*target)
        # Read back: the OS clamps points between monitors, and that must not look like the user.
        self._last_set = self._seen_at = self._desktop.cursor()

    def _scroll(self, dy: float, dx: float) -> None:
        self._scroll_rest[0] += dy
        self._scroll_rest[1] += dx
        out_dy, out_dx = math.trunc(self._scroll_rest[0]), math.trunc(self._scroll_rest[1])
        if out_dy or out_dx:
            self._scroll_rest[0] -= out_dy
            self._scroll_rest[1] -= out_dx
            self._desktop.scroll(out_dy, out_dx)

    def _release_all(self) -> None:
        for button in sorted(self._held):
            try:
                self._desktop.button(button, False)
            except Exception:
                log.exception("could not release the %s button", button)
        self._held = frozenset()
        self._grab = None
        self._segment = None
        self._scroll_rest = [0.0, 0.0]

    # -- windows ------------------------------------------------------------------------

    def _grab_at(self, p: Point) -> None:
        self._grab = None
        window = self._desktop.window_at(*p.rounded())
        if window is None:
            self._grab = _Grab(window=None)
            return
        grab = _Grab(window=window, grab_point=p, ref=p)
        self._grab = grab
        try:
            self._desktop.raise_window(window)
            state = self._desktop.window_state(window)
            grab.rect0 = self._desktop.window_rect(window)
        except InputBlocked as exc:
            self._blocked(window, exc)
            return
        grab.was_maximized = grab.maximized = state == "maximized"

    def _window_op(self, op: Callable[..., None], *args: object) -> None:
        grab = self._grab
        if grab is None or grab.window is None:
            return
        try:
            op(grab, *args)
        except InputBlocked as exc:
            self._blocked(grab.window, exc)

    def _restore_at(self, grab: _Grab, p: Point) -> None:
        """Un-maximize the grabbed window and place it under the cursor the way Windows does.

        The cursor keeps the relative x it had across the maximized window when
        it grabbed, and the window's top edge goes at most TITLE_GRAB_PX above it.
        """
        assert grab.window is not None
        maximized, grabbed = grab.rect0, grab.grab_point
        self._desktop.restore(grab.window)
        restored = self._desktop.window_rect(grab.window)
        rel_x = min(1.0, max(0.0, (grabbed.x - maximized.left) / maximized.width)) if maximized.width > 0 else 0.5
        below_top = min(max(grabbed.y - maximized.top, 0.0), TITLE_GRAB_PX)
        rect = Rect(p.x - rel_x * restored.width, p.y - below_top, restored.width, restored.height)
        self._set_rect(grab, rect)
        grab.rect0, grab.ref, grab.maximized = rect, p, False

    def _drag(self, grab: _Grab, p: Point) -> None:
        if grab.maximized:
            if p.distance(grab.grab_point) >= RESTORE_DRAG_PX:
                self._restore_at(grab, p)
            return
        self._set_rect(grab, grab.rect0.moved_to(grab.rect0.x + p.x - grab.ref.x, grab.rect0.y + p.y - grab.ref.y))

    def _resize(self, grab: _Grab, a: Point, b: Point) -> None:
        if grab.maximized:
            self._restore_at(grab, a)
        if grab.resize is None:
            grab.resize = (a, b, grab.last_rect or grab.rect0)
        a0, b0, rect0 = grab.resize
        min_w, min_h = self.min_size
        width = max(min_w, rect0.width + abs(b.x - a.x) - abs(b0.x - a0.x))
        height = max(min_h, rect0.height + abs(b.y - a.y) - abs(b0.y - a0.y))
        mid, mid0 = (a + b).scale(0.5), (a0 + b0).scale(0.5)
        centre = rect0.center + (mid - mid0)
        bounds = self._work_bounds()
        if bounds is not None:
            width, height = min(width, bounds.width), min(height, bounds.height)
        x, y = centre.x - width / 2, centre.y - height / 2
        if bounds is not None:
            x = min(max(x, bounds.left), bounds.right - width)
            y = min(max(y, bounds.top), bounds.bottom - height)
        self._set_rect(grab, Rect(x, y, width, height))

    def _throw(self, grab: _Grab, direction: str) -> None:
        window = grab.window
        assert window is not None
        if direction == "up":
            self._desktop.maximize(window)
            return
        if direction == "down":
            self._desktop.minimize(window)
            return
        displays = self._used_displays()
        if not displays:
            return
        if grab.maximized:
            self._desktop.restore(window)
            grab.maximized = False
        rect = self._desktop.window_rect(window)
        current = _display_of(rect, displays)
        target = display_in_direction(displays, current, "left" if direction == "left" else "right")
        if target is None:
            work = current.work
            half = work.width / 2
            self._set_rect(grab, Rect(work.x + (half if direction == "right" else 0), work.y, half, work.height))
            return
        src, dst = current.work, target.work
        width = min(dst.width, rect.width * dst.width / src.width)
        height = min(dst.height, rect.height * dst.height / src.height)
        x = dst.x + (rect.x - src.x) * dst.width / src.width
        y = dst.y + (rect.y - src.y) * dst.height / src.height
        x = min(max(x, dst.left), dst.right - width)
        y = min(max(y, dst.top), dst.bottom - height)
        self._set_rect(grab, Rect(x, y, width, height))
        if grab.was_maximized:
            self._desktop.maximize(window)

    def _set_rect(self, grab: _Grab, rect: Rect) -> None:
        assert grab.window is not None
        whole = Rect(*rect.rounded())
        if whole != grab.last_rect:
            self._desktop.set_window_rect(grab.window, whole)
            grab.last_rect = whole

    def _blocked(self, window: Window, exc: InputBlocked) -> None:
        if self._grab is not None and self._grab.window == window:
            self._grab = _Grab(window=None)
        if window.handle in self._blocked_reported:
            return
        self._blocked_reported.add(window.handle)
        name = f'"{window.title}"' if window.title else "this window"
        message = f"Windows won't let hand control move {name}; it probably runs as administrator."
        log.warning("%s (%s)", message, exc)
        if self._on_error is not None:
            try:
                self._on_error("input_blocked", message)
            except Exception:
                log.exception("on_error callback failed")

    def _used_displays(self) -> list[Display]:
        try:
            return list(self._displays())
        except Exception:
            log.exception("could not list displays")
            return []

    def _work_bounds(self) -> Rect | None:
        displays = self._used_displays()
        return bounding_rect([d.work for d in displays]) if displays else None


def _display_of(rect: Rect, displays: list[Display]) -> Display:
    centre = rect.center
    for d in displays:
        if d.rect.contains(centre):
            return d
    return min(displays, key=lambda d: d.rect.distance_to(centre))
