"""Test support shipped in the package like ``synthetic.py``: a session, a sink and a fake desktop wired together.

DESIGN-KEYBOARD.md 5.1. ``ScriptedPress`` is a ``PressMethod`` that emits the ``PressEvent`` objects a test scripts, so
a session can be tested without a detector. ``KbRig`` is the controller's frame loop (3.8) around a session, a sink
and a ``FakeDesktop``, and it makes the frames: hands drawn as landmarks, laid out so that the plane the session
places puts every fingertip on its own home key (a s d f and j k l ') and so that a pinch of any one finger passes the
warm-up's validity rules. Nothing outside the tests uses this module.

Pure: no clock of its own (the rig keeps the time of the last frame and the sink gets it as its injected clock), no I/O.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any

import numpy as np

from ..desktop.fake import FakeDesktop
from ..landmarks import FINGERS, MIDDLE_MCP, THUMB_CMC, THUMB_IP, THUMB_MCP, THUMB_TIP, WRIST, Frame, HandObservation
from ..overlay.base import KeyboardView
from .layout import Key, Layout, layout_for
from .plane import Plane
from .press_air import AirTapPress
from .press_pinch import PinchPress
from .review import InsertStep, InsertSummary
from .session import KeyboardSession, SessionOutput
from .sink import KeySink, SinkFailed
from .synth import REST, STYLES, AirTypist, Event, Noise, Posture, Typist, scenario
from .tuning import Tuning
from .types import (
    CloseReason,
    Commit,
    Decoder,
    FingerView,
    HandSample,
    Lang,
    Mode,
    PressEvent,
    PressLevel,
    PressMethod,
    PressName,
    PressQuality,
    Side,
    TipState,
)

#: The camera frame the rig's hands are drawn into, and its height over width (pose space scales y by it).
WIDTH, HEIGHT = 1280, 720
ASPECT = HEIGHT / WIDTH
#: The nominal palm (wrist to middle knuckle) and the distance between neighbouring fingertips, in frame widths. The
#: fingertips are one key pitch apart, so a plane placed from two hands six pitches apart puts them on a s d f and
#: j k l '.
PALM = 0.098
PITCH = Tuning().pitch
#: The wrist sits this far below the fingertip row, and the row this far down the picture, in frame widths.
_WRIST_DROP = 0.20
ROW_Y = 0.25
#: A pinching thumb starts this far below the fingertip it will meet, so it passes no other fingertip.
_THUMB_BELOW = 0.12
#: Where the two hands stand (the left one's centre, the right one's) and where a single hand stands.
_HAND_X = {"left": 0.5 - 3 * PITCH, "right": 0.5 + 3 * PITCH}
_ONE_HAND_X = 0.5

#: Appendix C, by the English character of the key's position (Hebrew keys stand in the same places).
_FINGERING: dict[str, tuple[Side, int]] = {}
for _side, _finger, _chars in (
    ("left", 3, "qaz"),
    ("left", 2, "wsx"),
    ("left", 1, "edc"),
    ("left", 0, "rfvtgb"),
    ("right", 0, "yhnujm"),
    ("right", 1, "ik,"),
    ("right", 2, "ol."),
    ("right", 3, "p'/-?"),
):
    for _c in _chars:
        _FINGERING[_c] = (_side, _finger)  # type: ignore[assignment]
#: The special keys, by kind. The Enter key is Send in the review layout (left ring) and Enter in the direct one.
_KIND_FINGERS: dict[str, tuple[Side, int]] = {
    "space": ("right", 0),
    "shift": ("left", 3),
    "lang": ("left", 3),
    "private": ("left", 3),
    "home": ("left", 3),
    "close": ("right", 3),
    "insert": ("right", 3),
    "clear": ("left", 3),
    "chip": ("right", 1),
}


def rig_hand(
    side: Side,
    x: float,
    *,
    y: float = ROW_Y,
    aspect: float = ASPECT,
    fist: bool = False,
    pinch: tuple[int, float] | None = None,
    levels: Sequence[float] | None = None,
) -> HandObservation:
    """A hand whose fingertips stand one pitch apart on the row ``y`` (pose space) around ``x``.

    The tracker levels the tips by ``levels`` (default: the tuning's), so each tip is drawn that much the other way and
    the aims come out on one line. ``fist`` pulls every tip back below its knuckle (curled). ``pinch=(finger, k)`` puts
    the thumb tip ``k`` of the way from just below that fingertip onto it: the finger's ratio is about 1.2 (1 - k), and
    its neighbours stay half a palm or more away, so a pinch of any one finger is a valid warm-up pinch.
    """
    levels = tuple(levels) if levels is not None else Tuning().level_palm
    sign = 1.0 if side == "right" else -1.0
    pts = np.zeros((21, 3))
    wrist = np.array([x, y + _WRIST_DROP])
    pts[WRIST, :2] = wrist
    tips = np.zeros((4, 2))
    for f in range(4):
        mcp_i, tip_i = FINGERS[("index", "middle", "ring", "pinky")[f]]
        tip_x = x + sign * (f - 1.5) * PITCH
        mcp = np.array([x + (tip_x - x) * 0.4, wrist[1] - PALM])
        tip = np.array([tip_x, y - levels[f] * PALM])
        if fist:
            tip = wrist + (mcp - wrist) * 0.85
        tips[f] = tip
        pts[mcp_i, :2] = mcp
        pts[mcp_i + 1, :2] = mcp + (tip - mcp) * 0.4
        pts[mcp_i + 2, :2] = mcp + (tip - mcp) * 0.7
        pts[tip_i, :2] = tip
    assert FINGERS["middle"][0] == MIDDLE_MCP
    base = np.array([x - sign * 0.6 * PALM, wrist[1] - 0.3 * PALM])
    thumb = np.array([x - sign * 0.14, wrist[1] - 0.05])
    if pinch is not None:
        finger, k = pinch
        start = tips[finger] + np.array([0.0, _THUMB_BELOW])
        thumb = start + (tips[finger] - start) * k
    pts[THUMB_CMC, :2] = base
    pts[THUMB_MCP, :2] = base + (thumb - base) * 0.35
    pts[THUMB_IP, :2] = base + (thumb - base) * 0.7
    pts[THUMB_TIP, :2] = thumb
    image = pts.copy()
    image[:, 1] /= aspect  # pose space scales y by the aspect; the picture does not
    return HandObservation(handedness=side, score=0.95, image=image, world=image.copy())


#: How far a pinch goes in each frame of one warm-up pinch: 0 is open, the minimum ratio is about 0.18.
PINCH_PROFILE = (0.0, 0.2, 0.4, 0.6, 0.75, 0.85, 0.85, 0.6, 0.4, 0.2, 0.0)


class ScriptedPress:
    """A ``PressMethod`` that emits what a test scripts and records what the session did to it."""

    name: PressName
    requires_review: bool
    rejects: dict[str, int]

    def __init__(self, name: PressName = "air", *, requires_review: bool | None = None) -> None:
        self.name = name
        self.requires_review = name == "air" if requires_review is None else requires_review
        self.rejects = {}
        #: What the session told it, for the tests to read.
        self.updates = 0
        self.resets = 0
        self.levels: list[PressLevel] = []
        self.calibrating = False
        self.calibrating_calls: list[bool] = []
        self.thresholds: dict[tuple[Side, int], tuple[float, float | None]] = {}
        #: The hands of the latest ``update``.
        self.seen: list[HandSample] = []
        #: What ``quality()`` answers: the ladder's input.
        self._quality = PressQuality(30.0, 0.017, 0) if name == "air" else PressQuality(0.0, None)
        #: (time, order, make) of the events not yet emitted.
        self._pending: list[tuple[float, int, Callable[[float], PressEvent]]] = []
        self._order = 0
        #: (side, finger) -> (state, fill, note) of the views ``fingers`` answers; the default is ``open``.
        self.view_states: dict[tuple[Side, int], tuple[TipState, float, str]] = {}

    # -- scripting
    def emit_at(self, t: float, make: PressEvent | Callable[[float], PressEvent]) -> None:
        """The event goes out at the first ``update`` that sees a hand at time ``t`` or later."""
        factory = make if callable(make) else (lambda _t, _e=make: _e)
        self._pending.append((t, self._order, factory))
        self._order += 1

    def emit(self, event: PressEvent) -> None:
        """The event goes out at the next ``update`` that sees a hand."""
        self.emit_at(float("-inf"), event)

    def reject(self, name: str, n: int = 1) -> None:
        self.rejects[name] = self.rejects.get(name, 0) + n

    def set_quality(self, fps: float, noise: float | None = None, gaps: int = 0) -> None:
        self._quality = PressQuality(fps, noise, gaps)

    def hand_of(self, side: Side) -> int:
        """The track id of the hand with this label in the latest ``update``; 1 when none."""
        return next((h.hand for h in self.seen if h.side == side), 1)

    @property
    def pending(self) -> int:
        return len(self._pending)

    # -- the PressMethod
    def update(self, hands: Sequence[HandSample]) -> list[PressEvent]:
        self.updates += 1
        self.seen = list(hands)
        if not hands:
            return []
        now = max(h.t for h in hands)
        due = sorted((item for item in self._pending if item[0] <= now + 1e-9), key=lambda item: (item[0], item[1]))
        if not due:
            return []
        self._pending = [item for item in self._pending if item not in due]
        return [make(item_t if item_t != float("-inf") else now) for item_t, _, make in due]

    def reset(self) -> None:
        self.resets += 1

    def set_finger(self, side: Side, finger: int, close: float, open_: float | None = None) -> None:
        if self.name == "pinch" and open_ is None:
            raise ValueError("a pinch threshold needs both ends")
        self.thresholds[(side, finger)] = (close, open_)

    def fingers(self, hands: Sequence[HandSample]) -> list[FingerView]:
        views = []
        for hand in hands:
            for f in hand.fingers:
                state, fill, note = self.view_states.get((hand.side, f.finger), ("open", 0.0, ""))
                views.append(
                    FingerView(hand.hand, hand.side, f.finger, (float(f.aim[0]), float(f.aim[1])), state, fill, note)
                )
        return views

    def quality(self) -> PressQuality:
        return self._quality

    def set_level(self, level: PressLevel) -> None:
        self.levels.append(level)

    def set_calibrating(self, on: bool) -> None:
        self.calibrating = on
        self.calibrating_calls.append(on)


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


@dataclass
class _Record:
    """What one frame of the rig did, for tests that look at the sequence."""

    t: float
    strokes: int
    steps: int


class KbRig:
    """The controller's frame loop (3.8) around one session, one sink and one fake desktop.

    ``feed`` takes frames; ``run`` makes frames of hands standing still; ``arm`` places the hands and warms up; ``tap``,
    ``insert`` and ``send`` script presses. The sink exists in live mode only (a practice session has none).
    """

    def __init__(
        self,
        *,
        commit: Commit = "direct",
        tuning: Tuning | None = None,
        decoder: Decoder | None = None,
        press: PressName | None = None,
        scripted: ScriptedPress | PressMethod | None = None,
        sides: Sequence[Side] = ("left", "right"),
        lang: Lang = "en",
        enter: str = "twice",
        idle_s: int = 30,
        mode: Mode = "live",
        fallback: bool | Callable[[], PressMethod] = False,
        phrases: Sequence[str] | None = None,
        reach: float = 1.0,
        fps: float = 30.0,
        start_t: float = 0.0,
        overlay_ok: bool = True,
        keep_views: bool = True,
        drop: Callable[[int, float], bool] | None = None,
    ) -> None:
        self.commit: Commit = commit
        self.tuning = tuning or Tuning()
        self.sides: tuple[Side, ...] = tuple(s for s in ("left", "right") if s in sides)  # type: ignore[misc]
        self.layout: Layout = layout_for(commit)
        self.lang: Lang = lang
        self.fps = fps
        self.dt = 1.0 / fps
        self.overlay_ok = overlay_ok
        self.keep_views = keep_views
        #: ``drop(k, t)``: the camera lost frame ``k`` (counted from 0), taken at ``t``; no hand, no update (X57).
        self._drop = drop
        self.t = start_t
        #: A ``ScriptedPress`` for the tests that script events; any other ``PressMethod`` (the real detectors, see
        #: ``AirScene`` and ``PinchScene``) when the frames are what the test controls.
        self.press: Any = scripted or ScriptedPress(press or ("air" if commit == "review" else "pinch"))
        self.fallback_press: ScriptedPress | None = None
        make_fallback: Callable[[], PressMethod] | None
        if callable(fallback):
            make_fallback = fallback
        elif fallback:
            make_fallback = self._make_fallback
        else:
            make_fallback = None
        self.desktop = FakeDesktop()
        self._clock = _Clock()
        self._clock.t = start_t
        self.sink: KeySink | None = (
            KeySink(self.desktop, inject="unicode", clock=self._clock, commit=commit) if mode == "live" else None
        )
        self.session = KeyboardSession(
            press=self.press,
            tuning=self.tuning,
            idle_s=idle_s,
            enter=enter,  # type: ignore[arg-type]
            lang=lang,
            mode=mode,
            aspect=ASPECT,
            start_t=start_t,
            reach=reach,
            phrases=phrases,
            commit=commit,
            fallback=make_fallback,
            decoder=decoder,
        )
        self._closed: CloseReason | None = None
        self._started = False
        self.summaries: list[InsertSummary] = []
        self.views: list[KeyboardView] = []
        self.view: KeyboardView | None = None
        self.records: list[_Record] = []
        self.steps: list[InsertStep] = []
        #: The answers the sink gave to every stroke and step, in order.
        self.answers: list[str] = []
        self.frames = 0
        self._hands_cache: dict[object, HandObservation] = {}

    def _make_fallback(self) -> PressMethod:
        self.fallback_press = ScriptedPress("pinch")
        return self.fallback_press

    @property
    def active(self) -> ScriptedPress:
        """The press the session uses now: the first one, or the pinch fallback after a switch."""
        if self.fallback_press is not None and self.session.press_name == self.fallback_press.name:
            return self.fallback_press
        return self.press

    # ------------------------------------------------------------------------------------------------ the frames

    def hands_frame(
        self,
        t: float,
        *,
        fist: bool | Sequence[Side] = False,
        pinch: dict[Side, tuple[int, float]] | None = None,
        sides: Sequence[Side] | None = None,
        offset: tuple[float, float] = (0.0, 0.0),
    ) -> Frame:
        """A frame at ``t`` with the rig's hands standing still; ``fist`` closes all of them or the ones named.

        ``offset`` moves every hand by that many key units (right, down) from where the session placed them.
        """
        out = []
        use = tuple(sides) if sides is not None else self.sides
        for side in use:
            x = _HAND_X[side] if len(self.sides) > 1 else _ONE_HAND_X
            closed = fist if isinstance(fist, bool) else side in fist
            key = (side, offset, closed, (pinch or {}).get(side))
            hand = self._hands_cache.get(key)
            if hand is None:  # a hand that stands still is the same observation in every frame
                hand = rig_hand(
                    side,
                    x + offset[0] * PITCH,
                    y=ROW_Y + offset[1] * PITCH * Tuning().pitch_y_ratio,
                    fist=closed,
                    pinch=(pinch or {}).get(side),
                )
                self._hands_cache[key] = hand
            out.append(hand)
        return Frame(t, tuple(out), WIDTH, HEIGHT)

    def run(self, seconds: float, **kw: object) -> None:
        """Frames with the hands standing still, from the next frame time for ``seconds``."""
        for _ in range(max(round(seconds * self.fps), 0)):
            self.feed([self.hands_frame(self.t + self.dt, **kw)])  # type: ignore[arg-type]

    def hover(self, seconds: float) -> None:
        self.run(seconds)

    def empty(self, seconds: float) -> None:
        """Frames with no hand in view."""
        for _ in range(max(round(seconds * self.fps), 0)):
            self.feed([Frame(self.t + self.dt, (), WIDTH, HEIGHT)])

    def feed(self, frames: Sequence[Frame]) -> None:
        for k, frame in enumerate(frames):
            if self._closed is not None:
                break
            if self._drop is not None and self._drop(k, frame.t):
                self.t = frame.t  # the clock goes on, the session sees nothing
                continue
            self._step(frame)

    def _step(self, frame: Frame) -> None:
        now = frame.t
        self.t = now
        self._clock.t = now
        self.frames += 1
        session, sink = self.session, self.sink
        try:
            hold = sink.gate(now, overlay_ok=self.overlay_ok) if sink is not None and session.armed else None
        except SinkFailed:  # the sink could not tell whether the real keyboard was used (S10)
            session.close("input_blocked")
            self._closed = "input_blocked"
            return
        out: SessionOutput = session.update(frame, hold, sink.target_name if sink is not None else "")
        try:
            for stroke in out.strokes:
                assert sink is not None
                result = sink.send(stroke, now)
                self.answers.append(result)
                session.note_result(stroke, result, frame.t)
            for step in out.steps:
                assert sink is not None
                self.steps.append(step)
                if step.first and not sink.begin_run(now, total=step.total, again=step.kind == "enter"):
                    session.note_step(step, "hold", frame.t, sink.last_hold)
                    self.answers.append("hold")
                else:
                    result = sink.send_run(step.stroke, now)
                    self.answers.append(result)
                    session.note_step(step, result, frame.t, sink.last_hold)
        except SinkFailed:
            session.close("input_blocked")
        summary = session.take_summary()
        if summary is not None:
            assert sink is not None
            sink.end_run(now, completed=summary.outcome == "done" and summary.kind == "text")
            self.summaries.append(summary)
        if sink is not None:
            if session.armed and not self._started:
                sink.start(now)  # the controller starts it at arming (3.8)
                self._started = True
            elif not session.armed:
                self._started = False
        if self.keep_views:
            self.views.append(out.view)
        self.view = out.view
        self.records.append(_Record(now, len(out.strokes), len(out.steps)))
        reason = out.closed or session.closed
        if reason is None and sink is not None and sink.runaway:
            session.close("runaway")
            reason = "runaway"
        if reason is not None:
            self._closed = reason

    # --------------------------------------------------------------------------------------------- arming

    def place(self, limit_s: float = 5.0) -> None:
        """Hands standing still until the session has placed them."""
        end = self.t + limit_s
        while self.session.phase == "placing" and self.t < end and self._closed is None:
            self.run(self.dt)

    def warm(self, depth: float = 0.30, limit_s: float = 60.0) -> None:
        """Whatever warms the session up: prompted taps of the named fingers (air) or one pinch of each finger."""
        end = self.t + limit_s
        if self.session.press_name == "air":
            while not self.session.armed and self.t < end and self._closed is None:
                prompt = self.session.warmup_prompt
                if prompt is not None:
                    self.warm_tap(*prompt, depth=depth)
                self.run(self.dt)
        else:
            for side in self.sides:
                for finger in range(4):
                    for k in PINCH_PROFILE:
                        self.feed([self.hands_frame(self.t + self.dt, pinch={side: (finger, k)})])

    def warm_tap(self, side: Side, finger: int, *, depth: float = 0.30, du: float = 0.0, dv: float = 0.0) -> None:
        """Script one warm-up tap of ``finger`` on its own home key, to go out at the next frame."""
        self.finger_tap(self.t + self.dt, side, finger, depth=depth, du=du, dv=dv)

    def finger_tap(
        self,
        t: float,
        side: Side,
        finger: int,
        *,
        depth: float = 0.30,
        margin: float = 1.0,
        du: float = 0.0,
        dv: float = 0.0,
    ) -> None:
        """Script a tap of ``finger`` aimed at its own home key plus ``(du, dv)`` key units, out at the first frame at
        ``t``. The home positions are the session's, read when the event goes out (the placing may not be done yet)."""
        press = self.active

        def make(at: float) -> PressEvent:
            plane, home_f = self.session.plane, self.session.home_f
            if plane is None or (side, finger) not in home_f:
                aim = (0.0, 0.0)
            else:
                u, v = home_f[(side, finger)]
                aim = plane.pose(u + du, v + dv)
            return PressEvent(at, at - 0.1, press.hand_of(side), side, finger, aim, 0.0, margin, depth, 0.9)

        press.emit_at(t, make)

    def arm(self) -> None:
        """Placing and warm-up, then run on until the session types."""
        self.place()
        self.warm()
        assert self.session.armed, "the rig could not arm the session"

    # ------------------------------------------------------------------------------------------------ the taps

    def key(self, key_or_char: Key | str) -> Key:
        """A key of the rig's layout: a ``Key``, a character, or the name of a kind ("insert", "backspace", ...).

        A character names the key that types it in the rig's language; failing that, the key at the place of that
        English letter (so a Hebrew rig can say "a" for the key that types Shin).
        """
        if isinstance(key_or_char, Key):
            return key_or_char
        if len(key_or_char) == 1:
            try:
                return self.layout.find(char=key_or_char, lang=self.lang)
            except KeyError:  # a Hebrew rig also names its keys by the English letter in the same place
                return self.layout.find(char=key_or_char, lang="en")
        return self.layout.find(kind=key_or_char)  # type: ignore[arg-type]

    def finger_of(self, key: Key) -> tuple[Side, int]:
        """The finger Appendix C gives the key; with one hand, that hand's index finger."""
        if key.kind == "char":
            side, finger = _FINGERING[key.en]
        elif key.kind == "backspace":
            side, finger = ("right", 0) if self.commit == "review" else ("right", 3)
        elif key.kind == "enter":
            side, finger = ("left", 2) if self.commit == "review" else ("right", 3)
        else:
            side, finger = _KIND_FINGERS[key.kind]
        if side not in self.sides:
            side, finger = self.sides[0], 0
        return side, finger

    def tap(
        self,
        t: float,
        key_or_char: Key | str,
        du: float = 0.0,
        dv: float = 0.0,
        finger: int | None = None,
        side: Side | None = None,
        *,
        conf: float | None = None,
        age: float = 0.0,
        before: Callable[[], None] | None = None,
    ) -> None:
        """Script a press of the key's centre plus ``(du, dv)`` key units, to go out at the first frame at ``t``.

        The aim is read from the plane the session has *when the event goes out*. The finger is Appendix C's unless
        given. Air events carry a depth, a margin and a confidence; pinch events a ratio and a margin. ``age`` makes
        the event older than the frame that delivers it (a press the camera reported late). ``before`` runs at that
        moment, after the frame's gate and before the session sees the tap (a window that changes in between).
        """
        key = self.key(key_or_char)
        default_side, default_finger = self.finger_of(key)
        side = side or default_side
        finger = default_finger if finger is None else finger
        press = self.active
        air = press.name == "air"
        confidence = (0.9 if air else 0.0) if conf is None else conf
        cu, cv = key.col + key.width / 2, key.row + 0.5

        def make(at: float) -> PressEvent:
            if before is not None:
                before()
            plane = self.session.plane
            aim = plane.pose(cu + du, cv + dv) if plane is not None else (0.0, 0.0)
            return PressEvent(
                at - age,
                at - age - 0.1,
                press.hand_of(side),  # type: ignore[arg-type]
                side,  # type: ignore[arg-type]
                finger,
                aim,
                0.0 if air else 0.2,
                1.0 if air else 0.2,
                0.3 if air else 0.0,
                confidence,
            )

        press.emit_at(t, make)

    def insert(self, t: float) -> None:
        """Three taps on Insert, 1.0 s apart."""
        for i in range(3):
            self.tap(t + i, "insert")

    def send(self, t: float) -> None:
        """Three taps on Send (the Enter key of the review layout), 1.0 s apart."""
        for i in range(3):
            self.tap(t + i, "enter")

    def type_text(self, text: str, *, gap_s: float = 0.35) -> None:
        """Script ``text`` one character per ``gap_s`` from the next frame on and run until the last one is out."""
        t = self.t + self.dt
        for i, c in enumerate(text):
            self.tap(t + i * gap_s, c)
        self.run(len(text) * gap_s + 0.3)

    def fill(self, text: str) -> None:
        """Test setup only: put ``text`` into the box without tapping it (the taps are other tests' business)."""
        machine = self.session._machine
        assert machine is not None, "only a review session has a box"
        for ch in text:
            assert machine.buffer.append(ch, self.t) == "ok"

    # ------------------------------------------------------------------------------------------- what to look at

    @property
    def typed(self) -> str:
        """``FakeDesktop.typed_text``."""
        return self.desktop.typed_text

    @property
    def closed(self) -> str | None:
        return self._closed

    @property
    def counts(self) -> Counter[str]:
        return self.session.counts

    @property
    def box(self) -> str:
        """Test-only accessor of ``buffer.text()``; "" outside review mode."""
        machine = self.session._machine
        return machine.buffer.text() if machine is not None else ""

    @property
    def summary_log(self) -> list[object]:
        return list(self.summaries)

    @property
    def chips(self) -> tuple[str, ...]:
        compose = self.view.compose if self.view is not None else None
        return compose.chips if compose is not None else ()

    @property
    def chip_active(self) -> int | None:
        compose = self.view.compose if self.view is not None else None
        return compose.chip_active if compose is not None else None


# ------------------------------------------------------------------------------- the real detectors, end to end
#
# ``KbRig`` with a ``ScriptedPress`` tests the session; the classes below test the whole path: landmarks made by the
# typists of ``synth.py``, the real ``HandTracker`` inside the session, the real ``AirTapPress`` or ``PinchPress``, the
# review machine, the sink and the fake desktop. Nothing in them reads a clock: a frame's own ``t`` is the only time.

#: Where the air typists' hands stand (image fractions): the study's, and they place a plane of about the base pitch.
AIR_HOME: dict[Side, tuple[float, float]] = {"left": (0.36, 0.55), "right": (0.64, 0.55)}
#: Hand and finger travel to a key: 0.10 s plus 0.09 s a bit of log distance (the study's Fitts-like planner).
_FITTS_BASE_S = 0.10
_FITTS_PER_BIT_S = 0.09
#: The next tap of a hand starts no sooner than half the last stroke plus this after the last one's start.
_RECOVER_S = 0.04
#: A tap the scene is asked for starts no sooner than this many frames ahead (the hand cannot move in the past).
_AHEAD_FRAMES = 2


def fitts(units: float) -> float:
    """Seconds to move the hand and finger ``units`` key units."""
    return _FITTS_BASE_S + _FITTS_PER_BIT_S * math.log2(1.0 + units)


class RecordingAir(AirTapPress):
    """The real air detector, with every event it emitted and every level it was told kept for the test to read."""

    def __init__(self, tuning: Tuning, **kw: Any) -> None:
        super().__init__(tuning, **kw)
        self.fired: list[PressEvent] = []
        self.levels: list[PressLevel] = []

    def update(self, hands: Sequence[HandSample]) -> list[PressEvent]:
        out = super().update(hands)
        self.fired += out
        return out

    def set_level(self, level: PressLevel) -> None:
        self.levels.append(level)
        super().set_level(level)


class RecordingPinch(PinchPress):
    """The real pinch detector, with every event it emitted kept for the test to read."""

    def __init__(self, tuning: Tuning) -> None:
        super().__init__(tuning)
        self.fired: list[PressEvent] = []

    def update(self, hands: Sequence[HandSample]) -> list[PressEvent]:
        out = super().update(hands)
        self.fired += out
        return out


class LiveAirHand(AirTypist):
    """An ``AirTypist`` that is handed its taps while it runs: a user who decides as they go.

    ``add`` makes the draws the constructor makes for an event (the stroke's gain, the neighbours that follow it) and
    plans the path again; the path before the new event's own move does not change, so a tap must be added before its
    move begins (``AirScene`` sees to it).
    """

    def __init__(
        self,
        side: Side,
        home_at: tuple[float, float],
        rng: np.random.Generator,
        noise: Noise,
        *,
        coupling_p: float = 0.4,
        coupling_share: tuple[float, float] = (0.10, 0.35),
        **kw: Any,
    ) -> None:
        self._coupling_p, self._coupling_share = coupling_p, coupling_share
        super().__init__(side, home_at, [], rng, noise, coupling_p=coupling_p, coupling_share=coupling_share, **kw)

    def set_sigma(self, sigma: float) -> None:
        """The landmark noise from the next frame on (a camera that gets worse, or better)."""
        self._lm.noise = replace(self._lm.noise, sigma=sigma)

    def add(self, event: Event) -> Event:
        index = sum(1 for e in self.events if e.t <= event.t)
        self.events.insert(index, event)
        self._amp.insert(index, float(self.rng.uniform(*self.noise.amp_gain)))
        shares = []
        for neighbour in (event.finger - 1, event.finger + 1):
            if 0 <= neighbour < 4 and self.rng.random() < self._coupling_p:
                shares.append((neighbour, float(self.rng.uniform(*self._coupling_share))))
        event.cpl = tuple(shares)
        self._build()
        return event


class Shifted:
    """A hand whose own clock started at ``t0``: ``scenario`` hands have events and phases in absolute time."""

    def __init__(self, hand: Any, t0: float) -> None:
        self.hand, self.t0 = hand, t0

    def observe(self, t: float, dt: float) -> HandObservation:
        obs: HandObservation = self.hand.observe(t - self.t0, dt)
        return obs


class Parked:
    """A hand drawn ``(dx, dy)`` of the picture from where it was: the hand of a user who rests a finger on some key."""

    def __init__(self, hand: Any, dx: float, dy: float) -> None:
        self.hand, self.dx, self.dy = hand, dx, dy

    def observe(self, t: float, dt: float) -> HandObservation:
        obs: HandObservation = self.hand.observe(t, dt)
        image = obs.image.copy()
        image[:, 0] += self.dx
        image[:, 1] += self.dy
        return HandObservation(obs.handedness, obs.score, image, obs.world)


@dataclass(frozen=True)
class TapTruth:
    """A tap the scene scripted: when its stroke starts, who made it, and the key it was aimed at."""

    t: float
    side: Side
    finger: int
    key: int


class AirScene:
    """Air-tap typists, the real ``AirTapPress`` and a review ``KbRig`` around them, one frame at a time.

    The hands are ``LiveAirHand`` s (``hands[side]``), so a tap can be decided while the stream runs. ``tap`` plans a
    tap of the standard finger of a key as the study's planner does (a Fitts-like move of the hand and finger, no
    sooner than the hand can get there); ``user_type`` and ``user_insert`` are a patient user who reads the box and
    corrects. The session's own ``home_f`` is where every finger rests, so a tap is aimed relative to what the
    session placed, not to a plane the test made up.
    """

    def __init__(
        self,
        *,
        sides: Sequence[Side] = ("left", "right"),
        seed: int = 0,
        style: str = "ordinary",
        sigma: float = 0.001,
        glitch_p: float = 0.003,
        amp_gain: tuple[float, float] = (0.7, 1.1),
        coupling_p: float = 0.4,
        alpha: float = 0.65,
        lead_s: float = 0.08,
        rest: Sequence[float] = REST,
        fps: float = 30.0,
        tuning: Tuning | None = None,
        trace: bool = False,
        drop: Callable[[int, float], bool] | None = None,
        **rig_kw: Any,
    ) -> None:
        self.tuning = tuning or Tuning()
        self.rng = np.random.default_rng(seed)
        self.style = STYLES[style]
        self.lead = lead_s
        self.noise = Noise(sigma=sigma, glitch_p=glitch_p, amp_gain=amp_gain)
        self.press = RecordingAir(self.tuning, trace=trace)
        self.rig = KbRig(commit="review", scripted=self.press, sides=sides, fps=fps, tuning=self.tuning, **rig_kw)
        self.hands: dict[Side, Any] = {
            side: LiveAirHand(
                side, AIR_HOME[side], self.rng, self.noise, coupling_p=coupling_p, alpha=alpha, lead_s=lead_s, rest=rest
            )
            for side in self.rig.sides
        }
        #: The hands in view; a hand that is not in this set is left out of every frame.
        self.present: set[Side] = set(self.hands)
        self.taps: list[TapTruth] = []
        #: Per side: the drawn offset of a hand that rests a finger on some key (``park``), as (dx, dy) of the picture.
        self.parked: dict[Side, tuple[float, float]] = {}
        #: Each finger's resting (u, v) on the placed plane, measured by ``place``.
        self.rest: dict[tuple[Side, int], tuple[float, float]] = {}
        self._drop = drop
        self._frame_no = 0
        #: Per hand: when its last scripted stroke started, how long it lasts, and where the hand went.
        self._last: dict[Side, tuple[float, float, tuple[float, float]]] = {
            side: (-math.inf, 0.0, (0.0, 0.0)) for side in self.hands
        }

    # ------------------------------------------------------------------------------------------------ the frames

    @property
    def session(self) -> KeyboardSession:
        return self.rig.session

    @property
    def t(self) -> float:
        return self.rig.t

    def set_fps(self, fps: float) -> None:
        """The camera's rate from the next frame on."""
        self.rig.fps = fps
        self.rig.dt = 1.0 / fps

    def set_sigma(self, sigma: float) -> None:
        """The landmark noise of every hand from the next frame on."""
        self.noise = replace(self.noise, sigma=sigma)
        for hand in self.hands.values():
            if hasattr(hand, "set_sigma"):
                hand.set_sigma(sigma)

    def step(self) -> None:
        rig = self.rig
        t = rig.t + rig.dt
        k, self._frame_no = self._frame_no, self._frame_no + 1
        if self._drop is not None and self._drop(k, t):
            rig.t = t  # the camera lost the frame: time goes on, the session sees nothing
            return
        seen = tuple(self.hands[s].observe(t, rig.dt) for s in self.hands if s in self.present)
        rig.feed([Frame(t, seen, WIDTH, HEIGHT)])

    def run(self, seconds: float) -> None:
        for _ in range(max(round(seconds * self.rig.fps), 0)):
            self.step()

    def run_until(self, done: Callable[[], bool], limit_s: float) -> bool:
        """Frames until ``done()`` or ``limit_s`` seconds or the session closes; True when ``done()`` came true."""
        end = self.rig.t + limit_s
        while not done() and self.rig.t < end and self.rig.closed is None:
            self.step()
        return done()

    # ----------------------------------------------------------------------------------------- placing and warm-up

    def place(self, limit_s: float = 10.0, *, rest_s: float = 0.5) -> None:
        """Run until the session has placed the hands, then ``rest_s`` more (inside the warm-up's first second, before
        anything taps) to measure where each finger rests on the plane the session made.

        The session's ``home_f`` is the mean of the placing window, made before it learned the per-user levels; the
        levels move a finger's own rest by a fraction of a key (the pinky's most), so a tap aimed at ``home_f`` would
        land that far off the key. The aim of a tap is relative to the rest measured here."""
        self.run_until(lambda: self.session.phase != "placing", limit_s)
        assert self.session.phase != "placing", "the session did not place the hands"
        plane = self.session.plane
        assert plane is not None
        seen: dict[tuple[Side, int], list[tuple[float, float]]] = {}
        for _ in range(max(round(rest_s * self.rig.fps), 1)):
            self.step()
            for hand in self.session.hands:
                for f in hand.fingers:
                    seen.setdefault((hand.side, f.finger), []).append(plane.units(f.aim))
        self.rest = {
            who: (sum(u for u, _ in uv) / len(uv), sum(v for _, v in uv) / len(uv)) for who, uv in seen.items()
        }

    def stroke(self, finger: int, *, amp: float | None = None, dur: float | None = None) -> tuple[float, float]:
        """The style's draw for a tap: (degrees of flexion, seconds)."""
        weak = finger >= 2
        mean, spread = self.style["weak" if weak else "amps"]
        lo, hi = self.style["dur"]
        flex = float(np.clip(self.rng.normal(mean, spread), 10, 60)) if amp is None else amp
        return flex, float(self.rng.uniform(lo, hi)) if dur is None else dur

    def warm(
        self, *, reaction: tuple[float, float] = (0.45, 0.75), retap_s: float = 1.2, limit_s: float = 90.0
    ) -> bool:
        """A user who watches the strip: taps the finger it names ``reaction`` seconds after it is named on that
        finger's own key, and again ``retap_s`` after a tap that was not taken. True when the session armed."""
        session, rig = self.session, self.rig
        shown: tuple[Side, int] | None = None
        retap_at = math.inf
        end = rig.t + limit_s
        while not session.armed and rig.t < end and rig.closed is None:
            prompt = session.warmup_prompt
            if prompt != shown:
                shown = prompt
                retap_at = math.inf
                if prompt is not None:
                    retap_at = rig.t + float(self.rng.uniform(*reaction))
                    self._warm_tap(prompt, retap_at)
                    retap_at += retap_s
            elif prompt is not None and rig.t >= retap_at - 0.3:
                self._warm_tap(prompt, retap_at)
                retap_at += retap_s
            self.step()
        return session.armed

    def _warm_tap(self, who: tuple[Side, int], at: float) -> None:
        side, finger = who
        flex, dur = self.stroke(finger)
        event = Event(
            side, finger, max(at, self.rig.t + _AHEAD_FRAMES * self.rig.dt + self.lead + 0.05), (0.0, 0.0), flex, dur
        )
        event.mt = 0.05
        self.hands[side].add(event)

    def arm(self) -> None:
        self.place()
        assert self.warm(), "the scene could not arm the session"

    # ------------------------------------------------------------------------------------------------ the taps

    def tap(
        self,
        key_or_char: Key | str,
        *,
        du: float = 0.0,
        dv: float = 0.0,
        side: Side | None = None,
        finger: int | None = None,
        at: float | None = None,
        amp: float | None = None,
        dur: float | None = None,
    ) -> TapTruth:
        """Script a tap of ``key`` by the standard finger (or the one given), ``(du, dv)`` key units off its centre.

        It starts at ``at`` or as soon as that finger's hand can be there. The hand stays where it went."""
        rig, session = self.rig, self.session
        key = rig.key(key_or_char)
        default_side, default_finger = rig.finger_of(key)
        who_side = side or default_side
        who_finger = default_finger if finger is None else finger
        home_u, home_v = self.rest.get((who_side, who_finger)) or session.home_f[(who_side, who_finger)]
        target = (key.col + key.width / 2 + du - home_u, key.row + 0.5 + dv - home_v)
        last_t, last_dur, last_target = self._last[who_side]
        move = fitts(math.hypot(target[0] - last_target[0], target[1] - last_target[1]))
        start = max(
            at if at is not None else -math.inf,
            last_t + 0.5 * last_dur + _RECOVER_S + move + self.lead,
            rig.t + _AHEAD_FRAMES * rig.dt + self.lead + move,
        )
        flex, length = self.stroke(who_finger, amp=amp, dur=dur)
        self.hands[who_side].add(
            Event(who_side, who_finger, start, target, flex, length, key=key.index, ch=key.en, mt=move)
        )
        self._last[who_side] = (start, length, target)
        truth = TapTruth(start, who_side, who_finger, key.index)
        self.taps.append(truth)
        return truth

    def type(self, text: str, *, gap_s: float = 1.0, tail_s: float = 1.0) -> list[TapTruth]:
        """``text`` tapped in the standard fingering, a key every ``gap_s``; runs until the last tap is over."""
        out: list[TapTruth] = []
        for ch in text:
            out.append(self.tap(ch, at=out[-1].t + gap_s if out else None))
        self.run(max(out[-1].t - self.rig.t, 0.0) + tail_s if out else 0.0)
        return out

    @property
    def shown(self) -> str:
        """The box as the user reads it on the keyboard."""
        compose = self.rig.view.compose if self.rig.view is not None else None
        return compose.text if compose is not None else ""

    def user_type(self, text: str, *, settle_s: float = 0.7, tries: int = 6) -> bool:
        """Type ``text`` into the box the way a person who looks at it does: tap the next character, wait, and if what
        is in the box is not a start of ``text`` tap Backspace until it is. True when the box holds ``text``."""
        goal = ""
        for ch in text:
            goal += ch
            for _ in range(tries):
                now = self.shown
                if now == goal:
                    break
                self.tap(goal[len(now)] if goal.startswith(now) else "backspace")
                self.run(settle_s)
            else:
                return False
            if self.rig.closed is not None:
                return False
        return self.shown == text

    def user_insert(self, text: str | None = None, *, every_s: float = 1.0, limit_s: float = 30.0) -> bool:
        """Tap Insert (the right pinky) every ``every_s`` until a run has begun; True when one has.

        With ``text`` the user looks at the box before every tap and puts it right first (a character that came late,
        a tap that landed on a neighbour), as someone who is about to type it into a window would."""
        begun = self.rig.counts["insert_start"]
        end = self.rig.t + limit_s
        while self.rig.counts["insert_start"] == begun and self.rig.t < end and self.rig.closed is None:
            if text is not None and self.shown != text and not self.user_type(text):
                return False
            truth = self.tap("insert")
            self.run(max(truth.t - self.rig.t, 0.0) + every_s)
        return self.rig.counts["insert_start"] > begun

    def user_send(self, *, every_s: float = 1.0, limit_s: float = 12.0) -> bool:
        """Tap Send (the left ring finger, the Enter key of the review layout) every ``every_s`` until a Send run has
        begun; True when one has. The machine's own windows decide whether it counts (a Send needs a finished Insert
        before it, at most ``SEND_WINDOW_S`` ago)."""
        begun = self.rig.counts["send_start"]
        end = self.rig.t + limit_s
        while self.rig.counts["send_start"] == begun and self.rig.t < end and self.rig.closed is None:
            truth = self.tap("enter")
            self.run(max(truth.t - self.rig.t, 0.0) + every_s)
        return self.rig.counts["send_start"] > begun

    def burst(
        self, who: Sequence[tuple[Side, int]], gap_s: float, *, amp: float = 42.0, dur: float = 0.2, lead_s: float = 0.5
    ) -> float:
        """Taps of the given fingers, one every ``gap_s`` from ``lead_s`` ahead, each on that finger's own resting
        place and however fast: the strokes overlap when ``gap_s`` is short. Returns the time of the first."""
        t0 = self.rig.t + lead_s
        for i, (side, finger) in enumerate(who):
            event = Event(side, finger, t0 + i * gap_s, (0.0, 0.0), amp, dur)
            event.mt = 0.0
            self.hands[side].add(event)
        return t0

    # ------------------------------------------------------------------------------------- hands in and out of view

    def leave(self, side: Side) -> None:
        self.present.discard(side)

    def arrive(self, side: Side) -> None:
        self.present.add(side)

    def negative(self, name: str, *, sigma: float | None = None, seconds: float = 150.0) -> None:
        """From now on every hand in view is a hand of ``synth.scenario`` ``name`` (a hand that never means a key).

        ``seconds`` sizes the scenario's random processes and event lists (they are built up front); a hand carries on
        past it, held at its last value."""
        noise = self.noise if sigma is None else Noise(sigma=sigma, glitch_p=self.noise.glitch_p)
        for side in list(self.hands):
            hand: Any = Shifted(scenario(name, side, self.rng, noise, duration=seconds), self.rig.t)
            if side in self.parked:
                hand = Parked(hand, *self.parked[side])
            self.hands[side] = hand

    def park(self, side: Side, finger: int, key_or_char: Key | str) -> None:
        """The hand of ``side`` rests with ``finger`` over the centre of ``key`` from the next frame on, and stays so
        through ``negative``: every frame of that hand is drawn that far from where the placed plane has it (the plane
        and the session's home do not move, which is the point: the finger is now over a key it does not belong to)."""
        plane = self.session.plane
        assert plane is not None, "park after the hands are placed"
        key = self.rig.key(key_or_char)
        u0, v0 = self.rest.get((side, finger)) or self.session.home_f[(side, finger)]
        x0, y0 = plane.pose(u0, v0)
        x1, y1 = plane.pose(key.col + key.width / 2, key.row + 0.5)
        offset = (x1 - x0, (y1 - y0) / ASPECT)  # pose space scales y by the aspect ratio
        self.parked[side] = offset
        hand = self.hands[side]
        if isinstance(hand, Parked):
            hand = hand.hand
        self.hands[side] = Parked(hand, *offset)


class Clockwork:
    """The little of ``tests/scripted.Script`` that ``Typist`` needs, so the package can drive it: frames at a fixed
    rate from a start time."""

    def __init__(self, t0: float = 100.0, fps: float = 30.0, width: int = WIDTH, height: int = HEIGHT) -> None:
        self.t, self.fps, self.width, self.height = t0, fps, width, height

    @property
    def dt(self) -> float:
        return 1.0 / self.fps

    def frame(self, *hands: Any) -> Frame:
        out = Frame(self.t, tuple(h.observe((self.width, self.height)) for h in hands), self.width, self.height)
        self.t += self.dt
        return out

    def count(self, seconds: float) -> int:
        return max(1, round(seconds * self.fps))


class PinchScene:
    """A pinch ``Typist``, the real ``PinchPress`` and a ``KbRig`` around them.

    The typist's plane is the one the session will place from its hands (the base pitch, hands over the home row of the
    layout); ``place`` and ``warm`` are the typist's, ``feed`` takes any frames of ``typist``. ``following`` makes the
    typist of a session that is already placed (an air session that fell back to the pinch), on the plane it made.
    """

    def __init__(
        self,
        *,
        commit: Commit = "direct",
        sides: Sequence[Side] = ("left", "right"),
        seed: int = 1,
        fps: float = 30.0,
        jitter: float = 0.001,
        z_noise: float = 0.004,
        posture: Posture = "rest",
        tuning: Tuning | None = None,
        reach: float = 1.0,
        t0: float = 100.0,
        coactivation: float = 0.0,
        rig: KbRig | None = None,
        **rig_kw: Any,
    ) -> None:
        self.tuning = tuning or Tuning()
        layout = layout_for(commit if rig is None else rig.commit)
        t = self.tuning
        plane = Plane(0.5, 0.45 * ASPECT, t.pitch * reach, t.pitch * reach * t.pitch_y_ratio, layout.rows)
        levels = None
        if rig is not None:
            assert rig.session.plane is not None, "the session has not placed the hands yet"
            plane = rig.session.plane
            learned = list(rig.session._tracker.levels.values())  # what the tracker will level the typist's hands by
            levels = tuple(float(np.mean([lv[i] for lv in learned])) for i in range(4)) if learned else None
            fps, t0 = rig.fps, rig.t
        self.script = Clockwork(t0 + 1.0 / fps, fps)
        self.rng = np.random.default_rng(seed)
        self.typist = Typist(
            self.script,
            plane,
            rng=self.rng,
            jitter=jitter,
            z_noise=z_noise,
            posture=posture,
            layout=layout,
            levels=levels,
            coactivation=coactivation,
        )
        if rig is None:
            self.press: Any = RecordingPinch(self.tuning)
            rig = KbRig(
                commit=commit,
                scripted=self.press,
                sides=sides,
                fps=fps,
                tuning=self.tuning,
                start_t=t0,
                reach=reach,
                **rig_kw,
            )
        else:
            self.press = rig.session._press
        self.rig = rig
        self.sides: tuple[Side, ...] = self.rig.sides

    @classmethod
    def following(cls, rig: KbRig, **kw: Any) -> PinchScene:
        """The typist of ``rig``'s placed session, whatever press it runs now."""
        return cls(rig=rig, **kw)

    @property
    def session(self) -> KeyboardSession:
        return self.rig.session

    def feed(self, frames: Sequence[Frame]) -> None:
        self.rig.feed(frames)

    def hover(self, seconds: float) -> None:
        self.rig.feed(self.typist.hover(seconds, self.sides))

    def place(self) -> None:
        """The hands in view stand over the home row, still, until the session has placed them."""
        self.rig.feed(self.typist.hover(1.2, self.sides))
        assert self.session.phase != "placing", "the session did not place the hands"

    def warm(self) -> None:
        """One pinch of each finger, frame by frame, until the session arms; then a moment of stillness.

        The synthetic warm-up pinches the index last, and the thumb on its way to the middle finger passes the index
        and gives it a valid record: the session arms there, and the index's own pinch would be an ordinary press. The
        frames stop at the arming (T5's rig does the same), so no key is typed by the warm-up."""
        for frame in self.typist.warm():
            self.rig.feed([frame])
            if self.session.armed:
                break
        self.rig.feed(self.typist.hover(0.5, self.sides))

    def arm(self) -> None:
        self.place()
        self.warm()
        assert self.session.armed, "the pinch warm-up did not arm the session"

    def type(self, text: str, *, gap_s: float = 0.35, tail_s: float = 0.5, lang: Lang = "en") -> None:
        self.rig.feed(self.typist.type(text, gap_s=gap_s, lang=lang))
        self.rig.feed(self.typist.hover(tail_s, self.sides))

    def press_key(self, key_or_char: Key | str, **kw: Any) -> None:
        self.rig.feed(self.typist.press(self.rig.key(key_or_char), **kw))
