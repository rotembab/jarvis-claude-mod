"""The ``keytrace`` subcommand: records a landmark trace with the camera, outside any session.

DESIGN-KEYBOARD.md 3.13, 5.7, 2.12.6 and 2.12.10. It opens the camera and the hand tracker (no runtime, no desktop,
nothing is typed anywhere), counts down, and then follows a list of segments, printing what to do in each one::

    place   hold both hands over the keys, still (the replay places the keyboard from it)
    type    type in the air, as at a keyboard (the console shows one of the practice phrases to type)
    tap     pinch: pinch each finger to the thumb in turn; air: tap each finger in turn
    rest    hands in view, doing nothing
    wave    wave, open and close the hands, tapping nothing
    drill   (air only) the console names a finger and sometimes a key, one prompt every 1.2 s, in random order

Before a ``type``, ``tap`` or ``drill`` that does not follow another one, a ``place`` of ``PLACE_S`` seconds is added.

**What the file holds (privacy).** One ``.npz`` in the format of ``trace.py``: per frame the time, which hand is which,
the handedness score and the 21 landmarks of each hand (x, y, z in frame widths, float32, NaN where a hand is absent),
the segment table, and the prompted key index of each drill frame. ``press`` says which press method the recording is
for and ``version`` is 2. On top of that one array, ``drill_prompts``, int32 ``[m, 5]``: for each drill prompt its first
frame, the side (0 left, 1 right), the finger (0 index .. 3 pinky), the key index and 1 when the key is displaced. That
is the whole file: **landmarks, times and the prompts the drill showed, no pixel, no window, no typed character**, and
nothing is sent anywhere: it is written to the path given and stays local. A ``type`` segment is the one place where
the person's own movements could in principle be read back into letters by this same tool; the console therefore shows
a practice phrase to type, and the file is deleted by hand like any other (practice traces are kept 14 days).

Only ``cli.py`` imports this module, lazily inside the function that dispatches the subcommand. The camera and the
tracker are the package's own wrappers, opened inside ``run`` (the arguments of ``run`` replace them in tests).
"""

from __future__ import annotations

import argparse
import math
import os
import random
import sys
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from itertools import pairwise
from pathlib import Path
from typing import Any, Final, TextIO

import numpy as np

from ..clock import now as clock_now
from .layout import Layout, layout_for
from .limits import AIR_DRILL_GAP_S
from .practice import PHRASES, DrillPrompt, make_drill
from .trace import TRACE_MAX_S, TraceWriter
from .types import PressName, Side

EXIT_OK, EXIT_FAILED, EXIT_REFUSED = 0, 1, 2

KINDS: Final = ("place", "type", "tap", "rest", "wave", "drill")
#: The segments in which keys are tapped: their plane comes from a ``place`` just before them.
KEY_KINDS: Final = ("type", "tap", "drill")
PLACE_S: Final = 3.0
DEFAULT_SECONDS: Final = 120.0
DEFAULT_COUNTDOWN_S: Final = 5.0
MAX_COUNTDOWN_S: Final = 60.0
#: The extra array of the file (see the docstring); ``keyreplay`` reads it by this name.
PROMPTS_KEY: Final = "drill_prompts"
#: No frame for this long ends the recording: the camera has stopped.
STALL_S: Final = 5.0
READ_TIMEOUT_S: Final = 0.5
#: Frame times are sums of floats: a boundary that falls on a frame is not missed by a rounding step.
_EPS: Final = 1e-6
#: The hands seen in this many seconds before a drill begins are the hands it names.
_SIDES_LOOK_S: Final = 1.5

_BAD_SEGMENTS = (
    "Each segment is kind:seconds, for example drill:60. The kinds are place, type, tap, rest, wave and drill."
)
_BAD_SECONDS = "The seconds of a segment are a number above 0, for example drill:60."
_TOO_LONG = f"A recording can be at most {TRACE_MAX_S:.0f} seconds long, the places that are added included."
_INSTRUCTION: Final = {
    "place": "Hold both hands over the keys, still.",
    "rest": "Rest: hands in view, do not tap.",
    "wave": "Wave, open and close your hands. Do not tap.",
}


def parse_segments(spec: str) -> list[tuple[str, float]]:
    """``"type:30,rest:20"`` -> ``[("type", 30.0), ("rest", 20.0)]``. A ValueError carries a fixed sentence."""
    segments: list[tuple[str, float]] = []
    for item in spec.split(","):
        kind, colon, seconds = item.strip().partition(":")
        if not colon or kind not in KINDS:
            raise ValueError(_BAD_SEGMENTS)
        try:
            value = float(seconds)
        except ValueError:
            raise ValueError(_BAD_SECONDS) from None
        if not (math.isfinite(value) and value > 0):
            raise ValueError(_BAD_SECONDS)
        segments.append((kind, value))
    return segments


def default_segments(press: PressName, seconds: float) -> list[tuple[str, float]]:
    """``--seconds`` alone: one stretch of the natural kind for the press method."""
    return [("drill" if press == "air" else "type", seconds)]


def build_plan(segments: Sequence[tuple[str, float]], press: PressName) -> list[tuple[str, float]]:
    """The segments with a ``place`` in front of every key segment that follows anything but a key segment."""
    if press == "pinch" and any(kind == "drill" for kind, _ in segments):
        raise ValueError("A drill needs the air tap: use --press air, or leave drill out of --segments.")
    plan: list[tuple[str, float]] = []
    for kind, seconds in segments:
        before = plan[-1][0] if plan else None
        if kind in KEY_KINDS and before not in (*KEY_KINDS, "place"):
            plan.append(("place", PLACE_S))
        plan.append((kind, seconds))
    return plan


# ---------------------------------------------------------------------------------------------------- the drill prompts


def _deck(
    layout: Layout, sides: Sequence[Side], rng: random.Random, after: tuple[Side, int] | None
) -> list[DrillPrompt]:
    """One round of the home-row prompts: six for each finger of the hands, every second one displaced.

    It is the practice drill (``make_drill``) without the reach prompts. Taking those out can leave one finger twice in
    a row, so a deck is drawn again until it does not (and does not begin with the finger the last deck ended on).
    """
    deck: list[DrillPrompt] = []
    for _ in range(500):
        deck = [p for p in make_drill(layout, sides, rng) if not p.reach]
        fingers = [(p.side, p.finger) for p in deck]
        if (after is None or fingers[0] != after) and all(a != b for a, b in pairwise(fingers)):
            break
    return deck


class PromptStream:
    """The endless prompt list of a recording: decks one after another, taken ``n`` at a time by each drill."""

    def __init__(self, layout: Layout, sides: Sequence[Side], rng: random.Random) -> None:
        self.sides = tuple(sides)
        self._layout, self._rng = layout, rng
        self._pool: deque[DrillPrompt] = deque()
        self._last: tuple[Side, int] | None = None

    def take(self, count: int) -> list[DrillPrompt]:
        taken: list[DrillPrompt] = []
        while len(taken) < count:
            if not self._pool:
                self._pool.extend(_deck(self._layout, self.sides, self._rng, self._last))
            prompt = self._pool.popleft()
            self._last = (prompt.side, prompt.finger)
            taken.append(prompt)
        return taken


def home_prompts(layout: Layout, sides: Sequence[Side], count: int, rng: random.Random) -> list[DrillPrompt]:
    return PromptStream(layout, sides, rng).take(count)


# -------------------------------------------------------------------------------------------------------- the arguments


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--yes-record",
        action="store_true",
        help="say that you agree to record your hands: landmarks and times only, never pixels, kept on this computer",
    )
    parser.add_argument(
        "--seconds",
        type=float,
        default=None,
        metavar="SECONDS",
        help=f"without --segments: one stretch this long (drill for air, typing for pinch); at most {TRACE_MAX_S:.0f}",
    )
    parser.add_argument(
        "--segments",
        default=None,
        metavar="KIND:SECONDS,...",
        help="what to record, e.g. drill:60,drill:60,rest:20,wave:20 (kinds: place, type, tap, rest, wave, drill)",
    )
    parser.add_argument(
        "--camera",
        default=os.environ.get("JARVIS_HANDS_CAMERA") or None,
        help="camera index, or part of its name (default: the first)",
    )
    parser.add_argument(
        "--press",
        choices=("air", "pinch"),
        default="air",
        help="the press method the recording is for (stored in the file; drill segments need air)",
    )
    parser.add_argument("--out", type=Path, required=True, metavar="FILE.npz", help="where to write the recording")
    parser.add_argument(
        "--countdown",
        type=float,
        default=DEFAULT_COUNTDOWN_S,
        metavar="SECONDS",
        help=f"seconds to get the hands in place before recording starts (default {DEFAULT_COUNTDOWN_S:g})",
    )
    parser.add_argument("--data-dir", type=Path, default=None, help="where the hand model is (default ~/.jarvis)")


def _data_dir(args: argparse.Namespace) -> Path:
    given = getattr(args, "data_dir", None)
    if given:
        return Path(given).expanduser()
    return Path(os.environ.get("JARVIS_DATA_DIR") or Path.home() / ".jarvis")


def _plan(args: argparse.Namespace) -> list[tuple[str, float]]:
    """The segments to record; ValueError (a fixed sentence) when they make no sense."""
    press: PressName = args.press
    if args.segments is not None and args.seconds is not None:
        raise ValueError("Give --seconds or --segments, not both.")
    if args.segments is not None:
        segments = parse_segments(args.segments)
    else:
        seconds = DEFAULT_SECONDS if args.seconds is None else float(args.seconds)
        if not (math.isfinite(seconds) and seconds > 0):
            raise ValueError("--seconds is a number above 0.")
        segments = default_segments(press, seconds)
    plan = build_plan(segments, press)
    if sum(seconds for _, seconds in plan) > TRACE_MAX_S:
        raise ValueError(_TOO_LONG)
    return plan


# -------------------------------------------------------------------------------------------------------- the recorder


class _Ended(Exception):
    """The recording ended before its plan did. ``why`` is a fixed sentence for the person at the screen."""

    def __init__(self, why: str) -> None:
        super().__init__(why)
        self.why = why


def _frames(camera: Any, tracker: Any, clock: Callable[[], float]) -> Iterator[tuple[float, Any]]:
    """Frames as the tracker makes them, each with the time it was captured; ``_Ended`` when the camera is gone."""
    from ..camera import CameraError

    last = clock()
    while True:
        try:
            got = camera.read(READ_TIMEOUT_S)
        except CameraError:
            raise _Ended("the camera went away") from None
        if got is None:
            if clock() - last >= STALL_S:
                raise _Ended("the camera stopped sending frames")
            continue
        last = clock()
        yield got.t, tracker.process(got.image, got.t)


class _Recorder:
    """The plan, frame by frame: which segment a frame is in, what the console says, and the drill's prompts."""

    def __init__(
        self,
        plan: Sequence[tuple[str, float]],
        press: PressName,
        rng: random.Random,
        say: Callable[[str], None],
    ) -> None:
        self.writer = TraceWriter(press=press, max_s=TRACE_MAX_S)
        self.rows: list[tuple[int, int, int, int, int]] = []
        self._plan = list(plan)
        self._ends = list(np.cumsum([seconds for _, seconds in plan]))
        self._layout = layout_for("review")
        self._rng, self._say = rng, say
        self._segment = -1
        self._start = 0.0
        self._prompts: list[DrillPrompt] = []
        self._window = -1
        self._stream: PromptStream | None = None
        self._seen: deque[tuple[float, frozenset[Side]]] = deque()
        self._types = 0

    @property
    def total(self) -> float:
        return float(self._ends[-1])

    def segment_at(self, elapsed: float) -> int | None:
        """The segment holding ``elapsed`` seconds from the start; None once the plan is over."""
        for index, end in enumerate(self._ends):
            if elapsed + _EPS < end:
                return index
        return None

    def add(self, t: float, elapsed: float, frame: Any) -> bool:
        """Records one frame; False when the plan is over or the writer will take no more."""
        index = self.segment_at(elapsed)
        if index is None or self.writer.full:
            return False
        kind, seconds = self._plan[index]
        if index != self._segment:
            self._enter(index, kind, seconds, t)
        target = -1
        position = len(self.writer)
        if kind == "drill":
            window = int((t - self._start) / AIR_DRILL_GAP_S + _EPS)
            if window < len(self._prompts):
                target = self._prompts[window].key
                if window != self._window:
                    self._prompt(window, position)
        accepted = self.writer.add(frame, kind, target)
        self._seen.append((t, frozenset(h.handedness for h in frame.hands)))
        while self._seen and t - self._seen[0][0] > _SIDES_LOOK_S:
            self._seen.popleft()
        return accepted or not self.writer.full

    def _enter(self, index: int, kind: str, seconds: float, t: float) -> None:
        self._segment, self._start, self._window = index, t, -1
        self._say(f"{kind} ({seconds:g} s)")
        if kind == "drill":
            count = int(seconds / AIR_DRILL_GAP_S)
            self._prompts = self._take(count)
        elif kind == "type":
            phrases = PHRASES["en"]
            self._say(f'  Type this in the air, as at a keyboard: "{phrases[self._types % len(phrases)]}"')
            self._types += 1
        elif kind == "tap":
            self._say("  Tap each finger in turn, index to pinky, right hand then left (pinch: pinch it to the thumb).")
        elif kind in _INSTRUCTION:
            self._say(f"  {_INSTRUCTION[kind]}")

    def _take(self, count: int) -> list[DrillPrompt]:
        """The next ``count`` prompts, for the hands that were in view just before the drill began."""
        counts = {"left": 0, "right": 0}
        for _, sides in self._seen:
            for side in sides:
                counts[side] += 1
        frames = max(len(self._seen), 1)
        sides: tuple[Side, ...] = tuple(s for s in ("left", "right") if counts[s] * 2 >= frames)  # type: ignore[misc]
        sides = sides or ("left", "right")
        if self._stream is None or self._stream.sides != sides:
            self._stream = PromptStream(self._layout, sides, self._rng)
        return self._stream.take(count)

    def _prompt(self, window: int, position: int) -> None:
        self._window = window
        prompt = self._prompts[window]
        self.rows.append(
            (position, 0 if prompt.side == "left" else 1, prompt.finger, prompt.key, int(prompt.displaced))
        )
        line = prompt.text
        if prompt.displaced:
            line += f", key {self._layout.keys[prompt.key].en}"
        self._say(line)


def _save(writer: TraceWriter, rows: Sequence[tuple[int, int, int, int, int]], path: Path) -> None:
    """The file, atomically: the pinned arrays from the writer, then (with a drill) the prompt table added to them."""
    if not rows:
        writer.save(path)
        return
    staging = path.with_name(path.name + ".part")
    temp = path.with_name(path.name + ".tmp")
    try:
        writer.save(staging)
        with np.load(staging, allow_pickle=False) as npz:
            arrays = {name: npz[name] for name in npz.files}
        arrays[PROMPTS_KEY] = np.array(rows, dtype=np.int32).reshape(len(rows), 5)
        with temp.open("wb") as handle:
            np.savez_compressed(handle, **arrays)
        os.replace(temp, path)
    finally:
        staging.unlink(missing_ok=True)
        temp.unlink(missing_ok=True)


# ------------------------------------------------------------------------------------------------------------- run


def run(
    args: argparse.Namespace,
    *,
    camera: Any = None,
    tracker: Any = None,
    clock: Callable[[], float] = clock_now,
    out: TextIO | None = None,
    rng: random.Random | None = None,
) -> int:
    """0 when the plan was recorded and saved, 1 when it ended early (the part is saved) or could not start, 2 when
    refused. The keyword arguments are for tests: a scripted camera and tracker, the clock the stall timer reads, the
    output and the generator of the drill's prompts."""
    stream = out if out is not None else sys.stdout

    def say(line: str) -> None:
        print(line, file=stream, flush=True)

    if not args.yes_record:
        say(
            "keytrace records your hands (landmarks and times, never pixels) and keeps the file here. Add --yes-record."
        )
        say("Nothing was recorded.")
        return EXIT_REFUSED
    try:
        plan = _plan(args)
    except ValueError as exc:
        say(str(exc))
        return EXIT_REFUSED
    countdown = float(args.countdown)
    if not 0.0 <= countdown <= MAX_COUNTDOWN_S:  # false for nan too
        say(f"--countdown is between 0 and {MAX_COUNTDOWN_S:g} seconds.")
        return EXIT_REFUSED
    path = Path(args.out)
    if path.suffix != ".npz":
        say("--out is a file name ending in .npz.")
        return EXIT_REFUSED
    if path.exists():
        say("That file already exists. Choose another name, or delete it first.")
        return EXIT_REFUSED

    from .. import models
    from ..camera import CameraError, create_camera
    from ..tracker import TrackerError, create_tracker

    data_dir = _data_dir(args)
    if tracker is None and not models.is_installed(data_dir):
        say("The hand model is not installed. Run /jarvis setup hands (or python -m jarvis_hands setup) first.")
        return EXIT_FAILED
    try:
        if tracker is None:
            tracker = create_tracker(models.model_path(data_dir), num_hands=2)
        if camera is None:
            camera = create_camera(args.camera)
        camera.open()
    except (CameraError, TrackerError) as exc:
        say(" ".join(part for part in (exc.message, exc.hint) if part))
        _close(camera, tracker)
        return EXIT_FAILED
    try:
        return _record(camera, tracker, plan, args.press, path, countdown, clock, say, rng or random.Random())
    finally:
        _close(camera, tracker)


def _close(camera: Any, tracker: Any) -> None:
    for device in (tracker, camera):
        if device is not None:
            try:
                device.close()
            except Exception:  # noqa: BLE001 - closing must not hide the result
                pass


def _record(
    camera: Any,
    tracker: Any,
    plan: Sequence[tuple[str, float]],
    press: PressName,
    path: Path,
    countdown: float,
    clock: Callable[[], float],
    say: Callable[[str], None],
    rng: random.Random,
) -> int:
    recorder = _Recorder(plan, press, rng, say)
    total = recorder.total
    say(f"keytrace: {total:.0f} s in {len(plan)} segments, after a countdown of {countdown:g} s. Ctrl+C stops early.")
    ending: str | None = None
    try:
        begin: float | None = None
        shown = -1
        for t, frame in _frames(camera, tracker, clock):
            if begin is None:
                begin = t
            elapsed = t - begin - countdown
            if elapsed < -_EPS:
                left = math.ceil(-elapsed - _EPS)
                if left != shown:
                    shown = left
                    say(f"  {left}")
                continue
            if not recorder.add(t, elapsed, frame):
                break
    except _Ended as ended:
        ending = ended.why
    except KeyboardInterrupt:
        ending = "you pressed Ctrl+C"
    frames = len(recorder.writer)
    if frames == 0:
        say("Nothing was recorded." if ending is None else f"Stopped ({ending}). Nothing was recorded.")
        return EXIT_FAILED if ending is not None else EXIT_OK
    try:
        _save(recorder.writer, recorder.rows, path)
    except OSError:
        say("Could not save the recording. Nothing was written.")
        return EXIT_FAILED
    if ending is not None:
        say(f"Stopped ({ending}). Saved the {frames} frames recorded so far to {path}.")
        return EXIT_FAILED
    say(f"Saved {frames} frames to {path}. It holds landmarks and times only.")
    say(f"Read it with: python -m jarvis_hands keyreplay {path}")
    return EXIT_OK
