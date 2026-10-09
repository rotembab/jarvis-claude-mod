"""The one object the runtime talks to: opens, drives and closes the keyboard (DESIGN-KEYBOARD.md 3.8).

The runtime owns the lock, the threads and the pointer; the controller owns one keyboard session at a time and nothing
else. It is called with the runtime lock held (``command``, ``frame``, ``pointer_frame``, ``close``, ``status``), and it
reaches back only through ``KeyboardDeps``, so a test can run it with no runtime at all.

Three rules shape the file.

* The pointer is off for as long as a session is open, and a close hands it back through the quarantine of 2.11: the
  engine sees empty frames until the camera has seen no hand for ``QUARANTINE_CLEAR_S``, because the engine's latches
  cannot be set from outside and a hand still up would otherwise be read as an engage, a click or a grab.
* The sink is the last gate (3.6). Everything that may refuse a key is in it; the controller only feeds it the
  session's strokes and steps, in the order of the pseudo-code of 3.8, and closes on what it reports.
* Exception text is data (F4). Every public method runs in its own ``try``; a handler writes the exception's *type
  name* and the method's name, never its text, and the replies and the report carry fixed sentences. This is the only
  keyboard module that may import ``protocol`` and ``OverlayState``.
"""

from __future__ import annotations

import logging
from collections import Counter, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, Literal

from .. import logs, protocol
from ..desktop.base import Display, KeyDesktop, Window, as_key_desktop
from ..landmarks import Frame
from ..overlay.base import OverlayState, overlay_health
from .limits import HEALTH_CLOSE_S, HEALTH_MAX_AGE_S, QUARANTINE_CLEAR_S
from .practice import PracticeResult, load_marker, marker_status, write_marker
from .press import make_press
from .review import InsertSummary
from .session import KeyboardSession
from .settings import AIR_NEEDS_REVIEW, KeyboardSettings
from .sink import KeySink, SinkFailed
from .trace import TapLog, TraceWriter, cleanup, trace_path
from .tuning import Tuning, load_tuning
from .types import CloseReason, Commit, Hold, Lang, Mode, PressMethod, PressName

log = logging.getLogger(__name__)

#: A change event is sent at most this often (3.9). Closed events and insert results are never held back.
EVENTS_PER_S: Final = 2
#: A practice cut short by the ladder shows its one fixed sentence this long before it closes.
CUT_SHOW_S: Final = 8.0
#: How often the air detector's quality is sampled for the close line.
QUALITY_SAMPLE_S: Final = 1.0
#: Where the keyboard goes when no display can be found (the primary monitor of a plain desktop).
FALLBACK_WORK: Final = (0, 0, 1920, 1040)
#: The low byte of a Windows keyboard-layout language id that means Hebrew (0x040D).
HEBREW_PRIMARY: Final = 0x0D

#: Every sentence the controller puts into a reply or a report. Fixed strings: none carries a name, a path or a value.
TEXT = MappingProxyType(
    {
        "off": "The air keyboard is off. Turn it on in the Jarvis settings (handKeyboard).",
        "starting": "Hand control is still starting.",
        "paused": "Hand control is paused; resume it first.",
        "blocked": "The desktop is locked or showing a system prompt.",
        "overlay": "The air keyboard needs the on-screen overlay.",
        "render": "Text rendering is unavailable, so the keyboard cannot be shown.",
        "no_typing": "This computer cannot type for the keyboard yet.",
        "air_direct": AIR_NEEDS_REVIEW,
        "practice_windows": "Practice needs the air or pinch method.",
        "practice_air": "Practice first: run /jarvis hands keyboard practice once with the air method.",
        "practice_air_poor": "The last air practice had too many false or missed taps; practice again.",
        "practice_pinch": "Practice first: run /jarvis hands keyboard practice once on this computer.",
        "practice_pinch_poor": "The last practice had too many false presses; practice again.",
        "osk": "Windows' on-screen keyboard did not start.",
        "other_mode": "The air keyboard is already open in the other mode; stop it first.",
        "unknown": "The air keyboard does not know that command.",
        "command_error": "The air keyboard hit an internal error.",
        "frame_error": "The air keyboard stopped because of an internal error.",
        "input_blocked": "The air keyboard stopped because Windows did not take its keys.",
    }
)


#: Where the texts of ``KeyboardSettings.apply`` come from: the file its errors are raised in.
_SETTINGS_FILE: Final = KeyboardSettings.apply.__code__.co_filename


def _raised_by_settings(exc: BaseException) -> bool:
    """The error was raised in the settings table itself, whose texts are fixed sentences that never echo the body."""
    tb = exc.__traceback__
    while tb is not None and tb.tb_next is not None:
        tb = tb.tb_next
    return tb is not None and tb.tb_frame.f_code.co_filename == _SETTINGS_FILE


@dataclass(frozen=True)
class _Plan:
    """What one open decided. The session itself is built by the first frame, which knows the time and the size."""

    mode: Mode
    #: The press method asked for; the session's own ``press_name`` follows a ladder fallback.
    press: PressName
    commit: Commit
    lang: Lang
    tuning: Tuning
    idle_s: int
    enter: Literal["twice", "off"]
    reach: float
    #: x, y, width, height of the work area of the display the keyboard is on.
    work: tuple[int, int, int, int]


@dataclass(frozen=True)
class KeyboardDeps:
    data_dir: Path
    #: The runtime's desktop; ``as_key_desktop()`` decides what it can do.
    desktop: Callable[[], object | None]
    #: May be a NullOverlay; ``overlay_health()`` decides.
    overlay: Callable[[], object | None]
    displays: Callable[[], Sequence[Display]]
    #: The engine exists and the camera is open.
    ready: Callable[[], bool]
    paused: Callable[[], bool]
    #: Lock screen or UAC.
    desktop_blocked: Callable[[], bool]
    #: ``HandsSettings.overlay``.
    overlay_enabled: Callable[[], bool]
    fps: Callable[[], float]
    #: Protocol event out (``runtime._emit``).
    emit: Callable[[dict[str, Any]], None]
    #: ``runtime._show``.
    show: Callable[[OverlayState], None]
    #: ``runtime._disengage_all("keyboard")`` then ``engine.reset_tracks()``.
    pointer_off: Callable[[], None]
    #: ``engine.reset_tracks()``.
    pointer_reset: Callable[[], None]
    #: ``runtime._report(code, message)``, throttled by the runtime.
    report: Callable[[str, str], None]
    clock: Callable[[], float]


class KeyboardController:
    settings: KeyboardSettings

    def __init__(self, deps: KeyboardDeps) -> None:
        self._deps = deps
        self.settings = KeyboardSettings()
        self._active = False
        self._release = False
        # the quarantine of 2.11
        self._quarantine = False
        self._clear_since: float | None = None
        # one open
        self._plan: _Plan | None = None
        self._press: PressMethod | None = None
        self._keys: KeyDesktop | None = None
        self._sink: KeySink | None = None
        self._session: KeyboardSession | None = None
        self._sink_started = False
        self._private = False  # asked for before the first frame built the session
        self._opened_at = 0.0
        self._seq = 0
        self._last_hold: Hold | None = None
        # events
        self._sent: deque[float] = deque()
        self._last_key: tuple[object, ...] | None = None
        # health of the overlay, and the practice's cut
        self._bad_since: float | None = None
        self._cut_since: float | None = None
        self._quality: tuple[float, float | None] = (0.0, None)
        self._quality_at = float("-inf")
        # practice only: the tap log and the landmark trace (X47: never in a live session)
        self._taps: list[dict[str, Any]] = []
        self._trace: TraceWriter | None = None

    # ------------------------------------------------------------------------------------------------ what to read

    @property
    def active(self) -> bool:
        """A session is open (the pointer is off)."""
        return self._active

    @property
    def wants_two_hands(self) -> bool:
        """Both hands type: the runtime rebuilds the landmarker for two at the next frame, and back after a close."""
        return self._active

    @property
    def needs_release(self) -> bool:
        """Set by an open; the runtime releases everything outside its lock, then clears it via ``take_release``."""
        return self._release

    def take_release(self) -> bool:
        taken, self._release = self._release, False
        return taken

    # ---------------------------------------------------------------------------------------------------- command

    def command(self, body: Mapping[str, Any]) -> dict[str, Any]:
        """The protocol ``keyboard`` command; the runtime lock is held by the caller."""
        try:
            return self._command(body)
        except Exception as exc:  # noqa: BLE001 - F4: the type name is all that is kept
            self._warn("command", exc)
            if self._active:
                self.close("error")  # a half-run command leaves a session nobody can vouch for
            return protocol.error_response("internal", TEXT["command_error"])

    def _command(self, body: Mapping[str, Any]) -> dict[str, Any]:
        action = body.get("action") if isinstance(body, Mapping) else None
        if action == "configure":
            return self._configure(body.get("settings", {}))
        if action in ("start", "practice"):
            return self._open("live" if action == "start" else "practice")
        if action == "stop":
            self.close("command")
            return protocol.ok_response()
        if action == "recenter":
            if self._session is not None:
                self._session.recenter()
            return protocol.ok_response()
        if action in ("private", "public"):
            self._private = action == "private"
            if self._session is not None:
                self._session.set_private(self._private)
            return protocol.ok_response()
        return protocol.error_response("bad_request", TEXT["unknown"])

    def _configure(self, settings: Any) -> dict[str, Any]:
        was = self.settings.enabled
        try:
            self.settings.apply(settings)
        except ValueError as exc:
            if not _raised_by_settings(exc):
                raise  # a ValueError from anywhere else is data, not a sentence for the user (F4)
            # Settings texts are fixed sentences that name the setting and never what was sent (settings.py).
            return protocol.error_response("bad_request", str(exc))
        if was and not self.settings.enabled and self._active:
            self.close("disabled")
        return protocol.ok_response()

    # ------------------------------------------------------------------------------------------------------ open

    def _open(self, mode: Mode) -> dict[str, Any]:
        if self._active:
            plan = self._plan
            if plan is not None and plan.mode == mode:
                return protocol.ok_response()  # idempotent: already open in this mode
            return protocol.error_response("bad_request", TEXT["other_mode"])
        refusal = self._refusal(mode)
        if refusal is not None:
            return protocol.error_response("bad_request", refusal)
        settings = self.settings
        desktop = self._deps.desktop()
        keys = as_key_desktop(desktop)
        if settings.press == "windows":
            # Windows' own keyboard: no session, no overlay, no safeguard of ours; the mod says so (1.5).
            assert keys is not None
            if not keys.open_os_keyboard():
                return protocol.error_response("bad_request", TEXT["osk"])
            return protocol.ok_response()

        live = mode == "live"
        tuning = load_tuning(self._deps.data_dir)
        cleanup(self._deps.data_dir)
        review = settings.commit == "review"
        press = make_press(settings.press, tuning, review=review)
        lang = self._lang(mode, keys)
        plan = _Plan(
            mode=mode,
            press=settings.press,
            commit=settings.commit,
            lang=lang,
            tuning=tuning,
            idle_s=settings.idle_s,
            enter=settings.enter,
            reach=settings.reach,
            work=self._work(desktop, keys if live else None),
        )
        sink = None
        if live:
            assert keys is not None
            sink = KeySink(keys, inject=settings.inject, clock=self._deps.clock, commit=settings.commit)

        self._deps.pointer_off()
        self._release = True
        logs.keyboard_scrub(True)
        self._plan, self._press, self._keys, self._sink, self._session = plan, press, keys, sink, None
        self._active = True
        self._quarantine, self._clear_since = False, None
        self._sink_started, self._private = False, False
        self._opened_at = self._deps.clock()
        self._seq, self._last_hold = 0, None
        self._sent.clear()
        self._bad_since = self._cut_since = None
        self._quality, self._quality_at = (0.0, None), float("-inf")
        self._taps = []
        self._trace = TraceWriter(press=settings.press) if not live else None  # type: ignore[arg-type]
        log.info("keyboard open (%s, %s, %s, %s)", mode, settings.press, settings.commit, lang)
        self._last_key = self._key_before_the_session()
        self._emit_open()
        return protocol.ok_response()

    def _refusal(self, mode: Mode) -> str | None:
        """The first reason this open must be refused, in the order of the table of 3.8; None when it may go on."""
        deps, settings = self._deps, self.settings
        if not settings.enabled:
            return TEXT["off"]
        if not deps.ready():
            return TEXT["starting"]
        if deps.paused():
            return TEXT["paused"]
        if deps.desktop_blocked():
            return TEXT["blocked"]
        desktop = deps.desktop()
        press, live = settings.press, mode == "live"
        if press != "windows":  # Windows' own keyboard needs none of ours to be drawn
            health = overlay_health(deps.overlay())
            alive = health is not None and health.alive and health.failures == 0
            real = bool(getattr(desktop, "injects_for_real", False))
            if not deps.overlay_enabled() or (real and not alive):
                return TEXT["overlay"]
            if alive and health is not None and not health.keyboard_ok:
                return TEXT["render"]
        if live and as_key_desktop(desktop) is None:
            return TEXT["no_typing"]
        if press == "air" and settings.commit == "direct":
            return TEXT["air_direct"]
        if not live and press == "windows":
            return TEXT["practice_windows"]
        if live and press == "air":
            status = marker_status(deps.data_dir, "air")
            if status != "ok":
                return TEXT["practice_air" if status == "missing" else "practice_air_poor"]
        elif live and press == "pinch" and settings.commit == "direct":
            status = marker_status(deps.data_dir, "pinch")
            if status != "ok":
                return TEXT["practice_pinch" if status == "missing" else "practice_pinch_poor"]
        return None

    def _lang(self, mode: Mode, keys: KeyDesktop | None) -> Lang:
        layout = self.settings.layout
        if layout in ("en", "he"):
            return layout
        if mode == "practice" or keys is None:
            return "en"
        try:
            return "he" if keys.key_target().lang_id & 0xFF == HEBREW_PRIMARY else "en"
        except Exception:  # noqa: BLE001 - a layout that cannot be read is English
            return "en"

    def _work(self, desktop: object | None, keys: KeyDesktop | None) -> tuple[int, int, int, int]:
        """The work area of the display the keyboard goes on: the one under the target window, else the primary."""
        try:
            displays = list(self._deps.displays())
        except Exception:  # noqa: BLE001 - the layout of the displays is only a placement
            displays = []
        chosen: Display | None = None
        if keys is not None:
            try:
                target = keys.key_target()
                rect = desktop.window_rect(Window(target.hwnd)) if target.hwnd else None  # type: ignore[attr-defined]
                if rect is not None:
                    chosen = next((d for d in displays if d.rect.contains(rect.center)), None)
            except Exception:  # noqa: BLE001 - no window, no rect: the primary display
                chosen = None
        if chosen is None:
            chosen = next((d for d in displays if d.primary), displays[0] if displays else None)
        if chosen is None:
            return FALLBACK_WORK
        x, y, width, height = chosen.work.rounded()
        return (int(x), int(y), int(width), int(height))

    def _fallback(self) -> PressMethod:
        """The pinch the ladder switches to (2.12.7); the controller keeps it so that status reads the active method."""
        plan = self._plan
        assert plan is not None
        self._press = make_press("pinch", plan.tuning, review=plan.commit == "review")
        return self._press

    def _build(self, frame: Frame) -> KeyboardSession:
        plan, press = self._plan, self._press
        assert plan is not None and press is not None
        # A live air session always gets a fallback; a practice session has none, so level off ends its drill.
        fallback = self._fallback if plan.press == "air" and plan.mode == "live" else None
        session = KeyboardSession(
            press=press,
            tuning=plan.tuning,
            idle_s=plan.idle_s,
            enter=plan.enter,
            lang=plan.lang,
            mode=plan.mode,
            aspect=frame.height / frame.width if frame.width > 0 else 0.5625,
            start_t=frame.t,
            reach=plan.reach,
            commit=plan.commit,
            fallback=fallback,
            decoder=None,
        )
        if self._private:
            session.set_private(True)
        return session

    # ------------------------------------------------------------------------------------------------------ frame

    def frame(self, frame: Frame, now: float) -> None:
        """Runtime lock held, loop thread, only while active."""
        if not self._active:
            return
        try:
            self._frame(frame, now)
        except Exception as exc:  # noqa: BLE001 - F4: the type name is all that is kept
            self._warn("frame", exc)
            self.close("error")
            self._report("internal", TEXT["frame_error"])

    def _frame(self, frame: Frame, now: float) -> None:
        plan = self._plan
        assert plan is not None
        session = self._session
        if session is None:
            session = self._session = self._build(frame)
        overlay_ok = self._overlay_ok(now)
        if not self._active:  # the overlay was gone for good and the health policy closed us
            return
        sink = self._sink
        try:
            hold = sink.gate(now, overlay_ok=overlay_ok) if sink is not None and session.armed else None
            out = session.update(frame, hold, sink.target_name if sink is not None else "")
            for stroke in out.strokes:  # direct mode
                assert sink is not None
                session.note_result(stroke, sink.send(stroke, now), frame.t)
            for step in out.steps:  # review mode; at most one per frame
                assert sink is not None
                if step.first and not sink.begin_run(now, total=step.total, again=step.kind == "enter"):
                    session.note_step(step, "hold", frame.t, sink.last_hold)
                else:
                    session.note_step(step, sink.send_run(step.stroke, now), frame.t, sink.last_hold)
            summary = session.take_summary()
            if summary is not None:
                assert sink is not None
                sink.end_run(now, completed=summary.outcome == "done" and summary.kind == "text")
                self._publish_insert(session, summary)
        except SinkFailed:
            self.close("input_blocked")
            self._report("input_blocked", TEXT["input_blocked"])
            return
        if sink is not None:
            # Started at arming, and again after a fallback re-arms it: a second start only re-baselines.
            if session.armed and not self._sink_started:
                sink.start(now)
                self._sink_started = True
            elif not session.armed:
                self._sink_started = False
        if plan.mode == "practice":
            self._practice_frame(session, frame)
        self._sample_quality(now)
        if out.closed is not None:
            self.close(out.closed)
            return
        if sink is not None and sink.runaway:
            self.close("runaway")
            return
        self._seq += 1
        view = replace(
            out.view,
            work=plan.work,
            size=self.settings.size,
            dock=self.settings.dock,
            seq=self._seq,
            exclude_capture=session.private,
        )
        self._last_hold = view.hold
        self._deps.show(OverlayState(keyboard=view))
        if not self._active:  # a draw that failed made the runtime close us on the spot
            return
        self._publish(session, view.hold, now)
        if plan.mode == "practice" and session.practice_done:
            self._practice_ends(session, now)

    def _overlay_ok(self, now: float) -> bool:
        """The health policy of 3.11: a live overlay with a recent good draw, or the sink holds ``overlay``."""
        desktop = self._deps.desktop()
        if not getattr(desktop, "injects_for_real", False):
            return True  # a fake desktop with no overlay stays valid: ``run --fake`` and the integration tests
        health = overlay_health(self._deps.overlay())
        if (
            health is not None
            and health.alive
            and health.keyboard_ok
            and health.failures == 0
            and health.ok_age_s is not None
            and health.ok_age_s <= HEALTH_MAX_AGE_S
        ):
            self._bad_since = None
            return True
        if health is not None and not health.alive:
            self.close("no_overlay")
            return False
        if self._bad_since is None:
            self._bad_since = now
        elif now - self._bad_since >= HEALTH_CLOSE_S:
            self.close("no_overlay")
        return False

    def _sample_quality(self, now: float) -> None:
        press = self._press
        if press is None or press.name != "air" or now - self._quality_at < QUALITY_SAMPLE_S:
            return
        self._quality_at = now
        quality = press.quality()
        self._quality = (quality.fps, quality.noise)

    def _practice_frame(self, session: KeyboardSession, frame: Frame) -> None:
        """The landmark trace and the tap log of a practice, kept in memory and written at the close."""
        self._taps.extend(session.take_tap_log())
        writer = self._trace
        if writer is None:
            return
        try:
            writer.add(frame, session.trace_kind, session.trace_target)
        except Exception as exc:  # noqa: BLE001 - a trace that cannot be kept never stops the practice
            self._warn("trace", exc)
            self._trace = None

    def _practice_ends(self, session: KeyboardSession, now: float) -> None:
        result = session.practice_result()
        if result is not None and result.completed:
            self.close("command")
            return
        # The ladder found the air tap unusable and cut the drill: its one sentence stays up for a while.
        if self._cut_since is None:
            self._cut_since = now
        elif now - self._cut_since >= CUT_SHOW_S:
            self.close("command")

    # ----------------------------------------------------------------------------------------------------- events

    def _key_before_the_session(self) -> tuple[object, ...]:
        plan = self._plan
        assert plan is not None
        review = "composing" if plan.mode == "live" and plan.commit == "review" else None
        return ("placing", None, False, plan.lang, review, "ok" if plan.press == "air" else None, plan.press)

    def _event(
        self, session: KeyboardSession | None, hold: Hold | None, *, insert: InsertSummary | None = None
    ) -> dict[str, Any]:
        """The ``open`` or ``practice`` event for the state now: enums and counts only (3.9)."""
        plan = self._plan
        assert plan is not None
        live = plan.mode == "live"
        review: dict[str, Any] | None = None
        if session is not None and session.review_state is not None:
            review = {"state": session.review_state, "chars": session.compose_len}
            if insert is not None:
                review["insert"] = {
                    "kind": insert.kind,
                    "outcome": insert.outcome,
                    "sent": insert.sent,
                    "of": insert.of,
                }
                if insert.reason is not None:
                    review["insert"]["reason"] = insert.reason
        return protocol.keyboard(
            "open" if live else "practice",
            phase=session.phase if session is not None else "placing",
            hold=hold,
            lang=session.lang if session is not None else plan.lang,
            press=session.press_name if session is not None else plan.press,
            level=(session.level if session is not None else "ok") if plan.press == "air" else None,
            commit=plan.commit if live else None,
            private=session.private if session is not None else self._private,
            review=review if live else None,
        )

    def _emit_open(self) -> None:
        self._sent.append(self._deps.clock())
        self._deps.emit(self._event(None, None))

    def _publish(self, session: KeyboardSession, hold: Hold | None, now: float) -> None:
        """A change event on phase, hold, private, lang, review state, level or press; at most two a second (3.9)."""
        plan = self._plan
        assert plan is not None
        key = (
            session.phase,
            hold,
            session.private,
            session.lang,
            session.review_state,
            session.level if plan.press == "air" else None,
            session.press_name,
        )
        if key == self._last_key:
            return
        while self._sent and now - self._sent[0] >= 1.0:
            self._sent.popleft()
        if len(self._sent) >= EVENTS_PER_S:
            return  # the change stays pending and goes out when the budget allows
        self._sent.append(now)
        self._last_key = key
        self._deps.emit(self._event(session, hold))

    def _publish_insert(self, session: KeyboardSession, summary: InsertSummary) -> None:
        """A run ended: one event, not rate limited (3.9)."""
        self._deps.emit(self._event(session, self._last_hold, insert=summary))

    # ---------------------------------------------------------------------------------------------------- close

    def close(self, reason: CloseReason, *, quarantine: bool = True) -> None:
        """Idempotent and re-entrant."""
        if not self._active:
            if not quarantine:
                self._quarantine, self._clear_since = False, None  # an explicit request wins (2.9)
            return
        # Taken first, so a draw that fails inside this close and makes the runtime close us again finds nothing.
        self._active = False
        plan, session, sink, keys = self._plan, self._session, self._sink, self._keys
        trace, taps = self._trace, self._taps
        self._session = self._sink = self._keys = self._trace = None
        self._taps = []
        self._quarantine, self._clear_since = quarantine, None
        discarded = 0
        result: PracticeResult | None = None
        line = ""
        try:
            if session is not None:
                session.close(reason)
                discarded = session.discarded
                if plan is not None and plan.mode == "practice":
                    result = session.practice_result()
            line = self._close_line(reason, plan, session, sink, discarded)
        except Exception as exc:  # noqa: BLE001 - F4
            self._warn("close", exc)
        if keys is not None:
            self._step("close", keys.release_keys)
        self._step("close", self._deps.pointer_reset)
        self._step("close", lambda: self._deps.show(OverlayState()))
        practice = self._practice_field(plan, result)
        if plan is not None and plan.mode == "practice":
            self._step("close", lambda: self._keep_practice(plan, result, trace, taps))
        self._step(
            "close",
            lambda: self._deps.emit(protocol.keyboard("closed", reason=reason, discarded=discarded, practice=practice)),
        )
        if line:
            log.info("%s", line)
        self._step("close", lambda: logs.keyboard_scrub(False))

    def _step(self, where: str, run: Callable[[], object]) -> None:
        """One step of a close in its own ``try``: a failing step never skips the ones after it."""
        try:
            run()
        except Exception as exc:  # noqa: BLE001 - F4
            self._warn(where, exc)

    def _practice_field(self, plan: _Plan | None, result: PracticeResult | None) -> dict[str, Any] | None:
        """The numbers of a finished practice for the closed event; None for a practice that was not finished."""
        if plan is None or result is None or not result.completed:
            return None
        field: dict[str, Any] = {"hitRate": result.hit_rate, "phantomsPerMin": result.phantoms_per_min}
        if plan.press == "air" and result.drill_prompts_im > 0:
            field["recallIM"] = result.drill_hits_im / result.drill_prompts_im
        return field

    def _keep_practice(
        self, plan: _Plan, result: PracticeResult | None, trace: TraceWriter | None, taps: list[dict[str, Any]]
    ) -> None:
        """Marker, trace and tap log, each on its own: a failing write is a missing file and never an exception."""
        data_dir = self._deps.data_dir
        if result is not None:
            self._step("marker", lambda: write_marker(data_dir, plan.press, result))
        if trace is not None and len(trace) > 0:
            self._step("trace", lambda: trace.save(trace_path(data_dir)))
        if taps:
            self._step("tap log", lambda: TapLog(data_dir).write(taps))

    def _close_line(
        self,
        reason: CloseReason,
        plan: _Plan | None,
        session: KeyboardSession | None,
        sink: KeySink | None,
        discarded: int,
    ) -> str:
        """The one INFO line of a close: counts, reasons and timings, never a key (SR13)."""
        seconds = max(self._deps.clock() - self._opened_at, 0.0)
        counts: Counter[str] = Counter()
        rejects: Counter[str] = Counter()
        keys = 0
        if session is not None:
            counts.update(session.counts)
            rejects.update(session.rejects)
            keys = counts.pop("keys", 0)
        if sink is not None:
            counts.update({f"sink_{name}": number for name, number in sink.counts.items()})
            keys = sink.counts["sent"] or keys
        health = overlay_health(self._deps.overlay())
        draw = f"{health.draw_ms:.1f}" if health is not None and health.draw_ms is not None else "n/a"
        line = (
            f"keyboard closed ({reason}): {keys} keys in {seconds:.0f} s; dropped {dict(sorted(counts.items()))}; "
            f"rejects {dict(sorted(rejects.items()))}; discarded {discarded}; overlay draw median {draw} ms"
        )
        if plan is not None and plan.press == "air" and session is not None:
            fps, noise = self._quality
            line += f"; level {session.level}; noise {noise if noise is not None else 0.0:.3f}; fps {fps:.1f}"
        return line

    # ------------------------------------------------------------------------------------------------ the pointer

    def pointer_frame(self, frame: Frame) -> Frame:
        """The frame the engine gets: empty while the pointer is off or quarantined (2.11)."""
        try:
            if self._active:
                return Frame(frame.t, (), frame.width, frame.height)
            if not self._quarantine:
                return frame
            if frame.hands:
                self._clear_since = None
            elif self._clear_since is None:
                self._clear_since = frame.t
            elif frame.t - self._clear_since >= QUARANTINE_CLEAR_S:
                # From the next frame on the real frames flow; the engine then engages by its own rules.
                self._quarantine, self._clear_since = False, None
            return Frame(frame.t, (), frame.width, frame.height)
        except Exception as exc:  # noqa: BLE001 - F4
            self._warn("pointer_frame", exc)
            return Frame(frame.t, (), frame.width, frame.height)

    # ----------------------------------------------------------------------------------------------------- status

    def status(self) -> dict[str, Any] | None:
        """``StatusResponse.keyboard``."""
        try:
            return self._status()
        except Exception as exc:  # noqa: BLE001 - F4
            self._warn("status", exc)
            return None

    def _status(self) -> dict[str, Any]:
        plan = self._plan if self._active else None
        press = plan.press if plan is not None else self.settings.press
        marker = load_marker(self._deps.data_dir, press) if press in ("air", "pinch") else None
        status: dict[str, Any] = {
            "enabled": self.settings.enabled,
            "state": ("open" if plan.mode == "live" else "practice") if plan is not None else "closed",
            "practiced": marker is not None,
        }
        if marker is not None:
            rest_s, phantoms = marker.get("restS"), marker.get("phantoms")
            if isinstance(rest_s, int | float) and isinstance(phantoms, int | float) and rest_s > 0:
                status["phantomsPerMin"] = round(phantoms * 60.0 / rest_s, 1)
        if plan is None:
            return status
        session = self._session
        active = session.press_name if session is not None else plan.press
        status["phase"] = session.phase if session is not None else "placing"
        status["press"] = active
        status["commit"] = plan.commit
        status["lang"] = session.lang if session is not None else plan.lang
        status["private"] = session.private if session is not None else self._private
        if self._last_hold is not None:
            status["hold"] = self._last_hold
        if active == "air" and self._press is not None:
            quality = self._press.quality()
            fps = quality.fps if quality.fps > 0 else self._deps.fps()
            status["level"] = session.level if session is not None else "ok"
            status["airFps"] = round(max(fps, 0.0), 1)
            status["airNoise"] = round(quality.noise if quality.noise is not None else 0.0, 3)
        if session is not None and session.review_state is not None:
            status["review"] = {"state": session.review_state, "chars": session.compose_len}
        return status

    # ---------------------------------------------------------------------------------------------------- helpers

    def _report(self, code: str, message: str) -> None:
        try:
            self._deps.report(code, message)
        except Exception as exc:  # noqa: BLE001 - F4
            self._warn("report", exc)

    @staticmethod
    def _warn(where: str, exc: BaseException) -> None:
        """The one line a caught exception gets: its type and the fixed name of the place, never its text (F4)."""
        log.warning("keyboard: %s in %s", type(exc).__name__, where)
