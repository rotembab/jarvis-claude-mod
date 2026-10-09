"""The ``keyreplay`` subcommand: replays a recorded landmark trace offline and says what the detector did with it.

DESIGN-KEYBOARD.md 3.13, 2.12.10 and 5.7. The file is a ``keytrace`` recording or a practice trace. The replay runs the
package's own ``HandTracker``, places the plane the way the session does (still hands, then the per-user levels), and
runs the press method the file names (``press``: ``AirTapPress`` or ``PinchPress``; ``--press`` overrides it, and a
version 1 file, which has none, is a pinch file) over the same hand samples, so every number of ``Tuning`` can be
retuned from a recording without the camera. The report is landmarks, times and counts: it never prints a key, a
character or a phrase, and the prompt table it reads holds key *indices* only.

What the replay does not do, on purpose: it is not a ``KeyboardSession``. A ``keytrace`` file has no warm-up and no
script that reacts to the events, so a session would wait for taps nobody was asked for. The replay drives the same
components directly: placing (2.4), the warm-up when the file has one (a practice trace), the degradation ladder
(2.12.7), and then counts every tap. Where a file has no warm-up the finger depth ``D_f`` is the median depth of that
finger's own prompted taps, and the report says so.

The report is what Rotem's live tests read: the camera and landmark noise (L9, L60), the hands in view (L12), the
posture at rest (L61), the drill's recall per finger and the numbers of the decision rule (L62), the false taps and the
key accuracy of each aim rule (L63). The suggestions come in three kinds. Thresholds (``air_theta_k``,
``air_theta_min``, ``air_depth_frac``) are searched one field at a time and replayed whole, but only when the recording
does not already meet the target: values found by shaving two stray taps off one recording are a fit, not a setting.
The aim (``air_aim``, ``air_aim_speed``) is chosen from the aims every rule gave for each prompted tap. The motion
gate ``air_vmax_gate`` has its own rule (2.12.10): it is raised to 0.75 only when more than 15% of the prompts were
refused for ``motion`` and the rest, in the recording and in its replay with the gate raised, has at most one stray
tap a minute.

``--set`` overrides ``Tuning`` fields and refuses what the clamps of ``tuning.py`` would not take (it never changes a
value silently). ``--write`` merges the suggested accuracy values (and the ``--set`` ones) into
``keyboard-tuning.json``: atomic, clamped to the same ranges, and unable to name anything that is not a ``Tuning``
field, so it cannot relax a safety constant of ``limits.py``.

Only ``cli.py`` imports this module, lazily inside the function that dispatches the subcommand. Standard library and
numpy only.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import os
import sys
from collections import Counter, deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final, TextIO

import numpy as np

from .hands import HandTracker
from .ladder import AirLadder
from .layout import Layout, layout_for
from .limits import AIR_DRILL_REACH_KEYS, GAP_RESET_S
from .plane import Placement, place_plane
from .practice import FINGER_NAMES, HOME_CHARS
from .press_air import AirTapPress
from .press_pinch import PinchPress
from .trace import TraceData, TraceError, read_trace
from .tuning import (
    AIR_AIM_CHOICES,
    GROUPS,
    INT_FIELDS,
    LEVEL_PALM_RANGE,
    MAX_FILE_BYTES,
    RANGES,
    Tuning,
    load_tuning,
    parse_tuning,
    tuning_path,
)
from .types import HandSample, PressEvent, PressName, Side
from .warmup import Warmup

EXIT_OK, EXIT_FAILED, EXIT_REFUSED = 0, 1, 2

#: The extra array of a ``keytrace`` file (rows of frame, side 0/1, finger, key index, displaced). ``keytrace`` writes
#: it under this name; the two modules do not import each other, so a test pins both ends.
PROMPTS_KEY: Final = "drill_prompts"
#: The estimated landmark noise in z above which the pinch fails (3.13) is 0.020; this warns before it.
JITTER_WARN_Z: Final = 0.015
#: A decision needs at least this many prompts to say anything about recall.
MIN_PROMPTS_TO_SUGGEST: Final = 20
#: The decision rule (7.4): false taps at most this share of all taps, and at least this share of the prompts found.
FALSE_RATE_LIMIT: Final = 0.05
RECALL_TARGET: Final = 0.90
#: The posture check of the live test L61: every finger's median lift at rest at least this, and the posture gate
#: closed less than this share of the rest.
REST_LIFT_MIN: Final = 0.40
POSTURE_CLOSED_MAX: Final = 0.02
#: The bars of the live test L62 (the decision rule): index and middle recall in the drill, extra taps as a share of the
#: prompts, recall of the reach keys; ring and pinky are reported against a target.
IM_RECALL_BAR: Final = 0.95
EXTRA_BAR: Final = 0.03
REACH_RECALL_BAR: Final = 0.85
RING_PINKY_TARGET: Final = 0.75
#: A frame gap longer than this is not counted as time spent in a segment.
_GAP_CAP_S: Final = 0.25
#: The still-hand placement of the session (``session.py``), repeated here because those names are private there.
_STILL_WINDOW_S: Final = 0.5
_PLACE_GRACE_S: Final = 1.0
_EPS: Final = 1e-9
_PHANTOM_KINDS: Final = ("rest", "wave", "talk")
_RULES: Final = ("onset", "commit", "auto", "peak")
#: Where the search looks for a better value, field by field (each inside its clamp; the current value is added).
#: ``air_vmax_gate`` is not here: the motion gate keeps waving and talking out, so it has its own rule (below), which
#: loosens it to one value and only when the recording shows what that costs.
_CANDIDATES: Final = {
    "air_theta_k": (4.0, 4.5, 5.0, 5.5, 6.0, 6.5, 7.0, 8.0),
    "air_theta_min": (0.08, 0.09, 0.10, 0.12, 0.14, 0.16, 0.20),
    "air_depth_frac": (0.4, 0.5, 0.6, 0.7),
}
#: The motion rule (DESIGN 2.12.10, amend-air 6.8): when more than this share of the named prompts were refused for
#: ``motion`` and the rest has at most this many stray taps a minute, ``air_vmax_gate`` is suggested at this value.
VMAX_SUGGESTED: Final = 0.75
MOTION_SHARE_LIMIT: Final = 0.15
REST_PHANTOMS_PER_MIN: Final = 1.0
_AIM_SPEEDS: Final = (0.02, 0.03, 0.05, 0.08, 0.10, 0.15)
#: A rule is kept when it is within this of the best one: a tie does not change the setting.
_AIM_TIE: Final = 0.01

#: Every name ``--set`` and ``--write`` may use, with the group of the file it lives in: the fields of ``Tuning``.
TUNABLE: Final = {name: group for group, names in GROUPS.items() for name in names}

_BAD_NAME = "That is not a setting. Settings are the fields of the keyboard tuning, for example air_theta_k."
_NOT_A_TRACE = "That file is not a landmark trace (python -m jarvis_hands keytrace writes one)."
_NO_FILE = "That file does not exist."
_PINCH_DRILL = "A pinch recording cannot hold a drill: use --press air, or replay a recording made for the air tap."


class ReplayError(ValueError):
    """The replay stops. The text is a fixed sentence for the person at the screen: never a path, a value or a key."""


# ---------------------------------------------------------------------------------------------------- the arguments


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("file", type=Path, metavar="FILE.npz", help="a recording from keytrace, or a practice trace")
    parser.add_argument(
        "--press",
        choices=("air", "pinch"),
        default=None,
        help="the press method to replay with (default: the one the file was recorded for)",
    )
    parser.add_argument(
        "--set",
        dest="sets",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="a tuning field to try, for example air_theta_k=6.5 (inside its range; repeat for more)",
    )
    parser.add_argument("--csv", type=Path, default=None, metavar="OUT.csv", help="also write the per-frame numbers")
    parser.add_argument(
        "--write",
        action="store_true",
        help="merge the suggested values (and the --set ones) into keyboard-tuning.json, clamped to their ranges",
    )
    parser.add_argument("--no-suggest", action="store_true", help="skip the search for better values (faster)")
    parser.add_argument("--data-dir", type=Path, default=None, help="where keyboard-tuning.json is (default ~/.jarvis)")


def _data_dir(args: argparse.Namespace) -> Path:
    given = getattr(args, "data_dir", None)
    if given:
        return Path(given).expanduser()
    return Path(os.environ.get("JARVIS_DATA_DIR") or Path.home() / ".jarvis")


# -------------------------------------------------------------------------------------------------------- --set


def _document(tuning: Tuning) -> dict[str, Any]:
    """The tuning as the file would hold it, so ``parse_tuning`` can say whether the loader would take it as it is."""
    doc: dict[str, Any] = {"version": 1}
    for group, names in GROUPS.items():
        doc[group] = {name: list(v) if isinstance(v := getattr(tuning, name), tuple) else v for name in names}
    return doc


def _fits(tuning: Tuning) -> bool:
    """True when the loader of ``tuning.py`` would keep every value of ``tuning`` (ranges and the relations)."""
    return parse_tuning(_document(tuning)) == tuning


def _in_range(name: str, number: float) -> bool:
    low, high = RANGES[name]
    return math.isfinite(number) and low <= number <= high


def _range_sentence(name: str) -> str:
    low, high = RANGES[name]
    return f"{name} is a number from {low:g} to {high:g}."


def _value(name: str, text: str) -> Any:
    """The value ``text`` stands for in field ``name``, inside its range, or a ReplayError with a fixed sentence."""
    if name == "level_palm":
        low, high = LEVEL_PALM_RANGE
        try:
            numbers = tuple(float(part) for part in text.split(","))
        except ValueError:
            numbers = ()
        if len(numbers) != 4 or not all(math.isfinite(n) and low <= n <= high for n in numbers):
            raise ReplayError(f"level_palm is four numbers from {low:g} to {high:g}, separated by commas.")
        return numbers
    if name == "air_aim":
        if text not in AIR_AIM_CHOICES:
            raise ReplayError("air_aim is one of: " + ", ".join(AIR_AIM_CHOICES) + ".")
        return text
    if name in INT_FIELDS:
        low, high = RANGES[name]
        try:
            whole = int(text)
        except ValueError:
            raise ReplayError(f"{name} is a whole number from {low:g} to {high:g}.") from None
        if not low <= whole <= high:
            raise ReplayError(f"{name} is a whole number from {low:g} to {high:g}.")
        return whole
    try:
        number = float(text)
    except ValueError:
        raise ReplayError(_range_sentence(name)) from None
    if not _in_range(name, number):
        raise ReplayError(_range_sentence(name))
    return number


def parse_sets(pairs: Iterable[str], base: Tuning) -> Tuning:
    """``base`` with each ``NAME=VALUE`` applied, later ones over earlier ones. A name that is not a ``Tuning`` field, a
    value outside the field's range, or fields that break a relation raise ``ReplayError`` (nothing is replaced)."""
    changes: dict[str, Any] = {}
    for pair in pairs:
        name, equals, text = pair.partition("=")
        name = name.strip()
        if not equals or name not in TUNABLE:
            raise ReplayError(_BAD_NAME)
        changes[name] = _value(name, text.strip())
    tuning = replace(base, **changes)
    if not _fits(tuning):
        raise ReplayError("Those settings do not fit together: a lower limit must stay below its upper limit.")
    return tuning


def set_names(pairs: Iterable[str]) -> frozenset[str]:
    return frozenset(pair.partition("=")[0].strip() for pair in pairs)


# --------------------------------------------------------------------------------------------------------- --write


def _clamped(name: str, value: Any) -> Any:
    """``value`` brought inside the range of field ``name``; ReplayError when it is not a usable value for it."""
    if name not in TUNABLE:
        raise ReplayError(_BAD_NAME)
    if name == "level_palm":
        if not isinstance(value, Sequence) or isinstance(value, str) or len(value) != 4:
            raise ReplayError("level_palm is four numbers.")
        low, high = LEVEL_PALM_RANGE
        return [_number(v, low, high) for v in value]
    if name == "air_aim":
        if value not in AIR_AIM_CHOICES:
            raise ReplayError("air_aim is one of: " + ", ".join(AIR_AIM_CHOICES) + ".")
        return value
    low, high = RANGES[name]
    number = _number(value, low, high)
    return round(number) if name in INT_FIELDS else number


def _number(value: Any, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ReplayError("A tuning value is a finite number.")
    return float(min(max(value, low), high))


def _not_json(name: str) -> Any:
    """``NaN`` and ``Infinity`` are not JSON: a file that holds them is one the merge would only carry on."""
    raise ValueError(name)


def _existing(path: Path) -> dict[str, Any]:
    """The tuning file as it is, or a new one; ReplayError when it is there but is not a file we may merge into."""
    unreadable = ReplayError("The tuning file cannot be read. Move it away, then run again.")
    try:
        if not path.exists():
            return {"version": 1}
        if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            raise unreadable
        doc = json.loads(path.read_bytes().decode("utf-8-sig"), parse_constant=_not_json)
    except (OSError, ValueError, RecursionError):  # ReplayError is a ValueError; also bad bytes and bad JSON
        raise unreadable from None
    if not isinstance(doc, dict) or doc.get("version", 1) != 1 or type(doc.get("version", 1)) is not int:
        raise ReplayError("The tuning file is not a version 1 object. Move it away, then run again.")
    if any(group in doc and not isinstance(doc[group], dict) for group in GROUPS):
        raise ReplayError("The tuning file has a group that is not an object. Move it away, then run again.")
    doc.setdefault("version", 1)
    return doc


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.capitalize() for part in rest)


def write_tuning(path: Path, values: Mapping[str, Any]) -> list[str]:
    """Merges ``values`` (``Tuning`` field names only) into the tuning file at ``path``, atomically.

    Every value is clamped to its range first, and the merged document must load back to exactly those values (so a
    pair that would break a relation is refused whole). What the file already holds besides is kept. Returns the field
    names written, in the order of the file's groups. ReplayError (and nothing written) for anything else.
    """
    clean = {name: _clamped(name, value) for name, value in values.items()}
    doc = _existing(path)
    for name, value in clean.items():
        group = doc.setdefault(TUNABLE[name], {})
        group.pop(_camel(name), None)  # the loader prefers the snake_case spelling; a stale twin would only confuse
        group[name] = value
    loaded = parse_tuning(doc)
    for name, value in clean.items():
        got = getattr(loaded, name)
        if (list(got) if isinstance(got, tuple) else got) != value:
            raise ReplayError("Those values would not be accepted together by the keyboard. Nothing was written.")
    temp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, path)
    except OSError:
        temp.unlink(missing_ok=True)
        raise ReplayError("The tuning file could not be written. Nothing was changed.") from None
    return [name for group in GROUPS for name in GROUPS[group] if name in clean]


# ----------------------------------------------------------------------------------------------------------- loading


@dataclass(frozen=True, eq=False)
class Recording:
    data: TraceData
    #: ``[m, 5]`` int: frame, side (0 left, 1 right), finger, key index, displaced. None for a file without the table.
    prompts: np.ndarray | None


def load(path: Path) -> Recording:
    """The trace and its prompt table; ReplayError (a fixed sentence) for anything that is not a good trace."""
    path = Path(path)
    if not path.is_file():
        raise ReplayError(_NO_FILE)
    try:
        data = read_trace(path)
        with np.load(path, allow_pickle=False) as npz:
            table = np.asarray(npz[PROMPTS_KEY]) if PROMPTS_KEY in npz.files else None
    except (TraceError, OSError, ValueError, KeyError):
        raise ReplayError(_NOT_A_TRACE) from None
    if table is None:
        return Recording(data, None)
    count = layout_for("review").count
    ok = (
        table.ndim == 2
        and table.shape[1] == 5
        and np.issubdtype(table.dtype, np.integer)
        and bool(np.all((table[:, 0] >= 0) & (table[:, 0] < max(len(data), 1))))
        and bool(np.all(np.isin(table[:, 1], (0, 1))))
        and bool(np.all((table[:, 2] >= 0) & (table[:, 2] <= 3)))
        and bool(np.all((table[:, 3] >= 0) & (table[:, 3] < count)))
        and bool(np.all(np.isin(table[:, 4], (0, 1))))
    )
    if not ok:
        raise ReplayError(_NOT_A_TRACE)
    return Recording(data, table.astype(np.int64))


# ----------------------------------------------------------------------------------------------- the hand samples


@dataclass
class _Hands:
    """The tracker's output for every frame, with the plane the still hands set. It does not depend on the press."""

    samples: list[list[HandSample]]
    placement: Placement | None
    #: Index of the frame at which the plane was placed (taps are counted from the next frame on).
    placed_at: int | None
    levels: dict[Side, tuple[float, ...]]
    #: The pinch warm-up that ran over every frame (the thresholds of the pinch press).
    pinch_warm: Warmup


class _Placer:
    """The session's placing phase (2.4): which hands are still, and the plane once they have been for ``still_s``."""

    def __init__(self, tuning: Tuning, layout: Layout) -> None:
        self._tuning, self._layout = tuning, layout
        self._speeds: dict[int, deque[tuple[float, float]]] = {}
        self._since: dict[int, float] = {}
        self._window: deque[tuple[float, list[HandSample]]] = deque()
        self._grace: float | None = None

    def step(self, hands: list[HandSample], t: float) -> Placement | None:
        tuning = self._tuning
        live = {h.hand for h in hands}
        for hand_id in [h for h in self._speeds if h not in live]:
            del self._speeds[hand_id]
            self._since.pop(hand_id, None)
        for h in hands:
            speeds = self._speeds.setdefault(h.hand, deque())
            speeds.append((t, h.speed))
            while speeds and t - speeds[0][0] > _STILL_WINDOW_S + _EPS:
                speeds.popleft()
            mean = math.fsum(s for _, s in speeds) / len(speeds)
            if mean < tuning.still_speed and sum(not f.curled for f in h.fingers) >= 3:
                self._since.setdefault(h.hand, t)
            else:
                self._since.pop(h.hand, None)
        self._window.append((t, hands))
        while self._window and t - self._window[0][0] > 2.0 * tuning.still_s:
            self._window.popleft()
        ready = [h for h in hands if t - self._since.get(h.hand, t + 1.0) >= tuning.still_s - _EPS]
        if not ready:
            self._grace = None
            return None
        if self._grace is None:
            self._grace = t
        if len(ready) < len(hands) and t - self._grace < _PLACE_GRACE_S - _EPS:
            return None  # the other hand is nearly still: one plane for both is worth a moment
        ids = {h.hand for h in ready}
        frames = [
            [h for h in samples if h.hand in ids] for then, samples in self._window if t - then <= tuning.still_s + _EPS
        ]
        return place_plane(frames, layout=self._layout, tuning=tuning)


def _hand_pass(data: TraceData, tuning: Tuning, layout: Layout) -> _Hands:
    tracker = HandTracker(tuning)
    placer = _Placer(tuning, layout)
    warm = Warmup(tuning, "pinch")
    samples: list[list[HandSample]] = []
    placement: Placement | None = None
    placed_at: int | None = None
    for index, frame in enumerate(data.frames()):
        hands = tracker.update(frame)
        samples.append(hands)
        warm.update(hands)
        if placement is None:
            placement = placer.step(hands, frame.t)
            if placement is not None:
                placed_at = index
                tracker.learn_levels()
    return _Hands(samples, placement, placed_at, dict(tracker.levels), warm)


# ------------------------------------------------------------------------------------------------------ the press


@dataclass
class Tap:
    """One press the method committed after the plane was placed."""

    frame: int
    t: float
    onset_t: float
    side: Side
    finger: int
    hand: int
    depth: float
    #: The aim the method gave (its own rule), pose space.
    aim: tuple[float, float]
    #: Segment kind of the commit frame.
    kind: str
    #: Air only, from the tap-log record: anchor speed at the left base, peak time, the rule used, the threshold.
    vl: float = 0.0
    peak_t: float = 0.0
    rule: str = ""
    theta: float = 0.0
    #: Air only: the aim each rule gives, computed from the hand samples ("onset", "commit", "peak").
    rules: dict[str, tuple[float, float]] = field(default_factory=dict)


@dataclass
class _Run:
    taps: list[Tap]
    rejects: Counter[str]
    #: (side, finger) -> sorted (time, why) of the peaks a gate refused.
    refused: dict[tuple[Side, int], list[tuple[float, str]]]
    #: Per frame: side -> the hand gate that is closed (air only; empty for pinch).
    gates: list[dict[Side, str]]
    #: (side, finger) -> the threshold and the noise estimate, sampled on every frame after placing.
    theta: dict[tuple[Side, int], list[float]]
    sigma: dict[tuple[Side, int], list[float]]
    depth: dict[tuple[Side, int], float]
    depth_source: str
    quality: Any
    ladder: AirLadder | None
    #: Frame time at which the ladder said ``off`` (air), else None.
    off_at: float | None


def _has_warmup(data: TraceData) -> bool:
    return any(kind == "warm" for _, _, kind in data.segments)


def _press_pass(
    data: TraceData,
    hands: _Hands,
    tuning: Tuning,
    name: PressName,
    kinds: Sequence[str],
    *,
    depths: Mapping[tuple[Side, int], float] | None = None,
) -> _Run:
    air = name == "air"
    press: Any = AirTapPress(tuning, trace=True) if air else PinchPress(tuning)
    run = _Run([], Counter(), {}, [], {}, {}, {}, "none", None, None, None)
    if air and depths:
        for (side, finger), value in depths.items():
            press.set_finger(side, finger, value)
        run.depth_source = "prompted taps"
    if not air:
        for (side, finger), (close, open_) in hands.pinch_warm.thresholds().items():
            press.set_finger(side, finger, close, open_)
    placed = hands.placed_at
    warm: Warmup | None = None
    ladder = run.ladder = AirLadder() if air else None
    strict = False
    fires: dict[tuple[float, int, int], dict[str, Any]] = {}
    last: float | None = None
    for index, (t, samples) in enumerate(zip(data.t.tolist(), hands.samples, strict=True)):
        if last is not None and t - last > GAP_RESET_S + _EPS:
            press.reset()
        last = t
        after = placed is not None and index > placed
        if air and after and ladder is not None:
            if ladder.update(t, press.quality(), bool(samples)) == "off" and run.off_at is None:
                run.off_at = t
            if ladder.strict != strict:
                strict = ladder.strict
                press.set_level("degraded" if strict else "ok")
        if air and placed is not None and index == placed and _has_warmup(data) and not depths:
            warm = Warmup(tuning, "air", home_f=hands.placement.home_f)  # type: ignore[union-attr]
            press.set_calibrating(True)
        before = sum(v for k, v in press.rejects.items() if k != "veto")
        events: list[PressEvent] = press.update(samples)
        if air:
            for record in press.take_trace():
                if record["k"] == "fire":
                    fires[(record["t"], record["hand"], record["finger"])] = record
                elif record["k"] == "reject":
                    run.refused.setdefault((record["side"], record["finger"]), []).append((record["t"], record["why"]))
            run.gates.append({h.side: press.gate_of(h.hand) or "" for h in samples})
        if not after:
            continue
        if warm is not None and (warm.complete or kinds[index] not in ("place", "warm")):
            for (side, finger), (close, _) in warm.thresholds().items():
                press.set_finger(side, finger, close)
            press.set_calibrating(False)
            run.depth_source, warm = "warm-up", None
        if warm is not None:
            warm.update(samples, events, t=t, plane=hands.placement.plane, rejects=before)  # type: ignore[union-attr]
            continue
        for event in events:
            record = fires.get((event.t, event.hand, event.finger), {})
            run.taps.append(
                Tap(
                    frame=index,
                    t=event.t,
                    onset_t=event.onset_t,
                    side=event.side,
                    finger=event.finger,
                    hand=event.hand,
                    depth=event.depth,
                    aim=(float(event.aim[0]), float(event.aim[1])),
                    kind=kinds[index],
                    vl=float(record.get("vl", 0.0)),
                    peak_t=float(record.get("pkT", event.t)),
                    rule=str(record.get("aimRule", "")),
                    theta=float(record.get("theta", 0.0)),
                )
            )
        if air:
            for s in samples:
                for f in range(4):
                    if (theta := press.theta(s.hand, f)) is not None:
                        run.theta.setdefault((s.side, f), []).append(theta)
                        run.sigma.setdefault((s.side, f), []).append(press.sigma(s.hand, f))
    run.rejects = Counter(press.rejects)
    run.quality = press.quality()
    if air:
        run.depth = {(s, f): d for s in ("left", "right") for f in range(4) if (d := press.depth_of(s, f)) is not None}
    return run


# ------------------------------------------------------------------------------------------------------ the prompts


SIDES_BY_NUMBER: Final[dict[int, Side]] = {0: "left", 1: "right"}


@dataclass(frozen=True)
class _Prompt:
    t0: float
    t1: float
    #: None for a prompt whose finger the file does not name (a practice trace's displaced key).
    side: Side | None
    finger: int | None
    key: int


def _finger_of_key(layout: Layout) -> dict[int, tuple[Side, int]]:
    """The finger that a home key or a reach key names; a displaced key can be reached by several, so it is absent."""
    named: dict[int, tuple[Side, int]] = {}
    for side in ("left", "right"):
        for finger, char in enumerate(HOME_CHARS[side]):
            named[layout.find(char=char).index] = (side, finger)
    for kind, side, name in AIR_DRILL_REACH_KEYS:
        named[layout.find(kind=kind).index] = (side, FINGER_NAMES.index(name))  # type: ignore[arg-type]
    return named


def _segment_end_time(data: TraceData, end: int) -> float:
    if end < len(data):
        return float(data.t[end])
    step = float(np.median(np.diff(data.t))) if len(data) > 1 else 0.0
    return float(data.t[-1]) + step


def _prompts(rec: Recording, layout: Layout) -> list[_Prompt]:
    """The drill's prompt windows: from the table when the file has one, else from the drill segments' targets."""
    data = rec.data
    spans = [(s, e) for s, e, kind in data.segments if kind == "drill"]
    if rec.prompts is not None:
        out: list[_Prompt] = []
        for k, (frame, side, finger, key, _) in enumerate(rec.prompts.tolist()):
            end = next((e for s, e in spans if s <= frame < e), len(data))
            nxt = rec.prompts[k + 1, 0] if k + 1 < len(rec.prompts) else end
            stop = min(int(nxt), end)
            out.append(_Prompt(float(data.t[frame]), _segment_end_time(data, stop), SIDES_BY_NUMBER[side], finger, key))
        return out
    named = _finger_of_key(layout)
    out = []
    for start, end in spans:
        run_start = start
        for i in range(start + 1, end + 1):
            if i == end or data.targets[i] != data.targets[run_start]:
                key = int(data.targets[run_start])
                if key >= 0:
                    who = named.get(key)
                    out.append(
                        _Prompt(
                            float(data.t[run_start]),
                            _segment_end_time(data, i),
                            who[0] if who else None,
                            who[1] if who else None,
                            key,
                        )
                    )
                run_start = i
    return out


@dataclass
class _Scores:
    """What each tap and each prompt came to."""

    #: Per tap: "hit", "wrong", "extra", "unnamed", "phantom", "free".
    outcome: list[str]
    #: Per tap: the prompt index it fell in, or None.
    window: list[int | None]
    #: Per prompt: the index of the tap that hit it, or None.
    hit: list[int | None]


def _classify(taps: Sequence[Tap], prompts: Sequence[_Prompt]) -> _Scores:
    starts = [p.t0 for p in prompts]
    outcome: list[str] = []
    window: list[int | None] = []
    hit: list[int | None] = [None] * len(prompts)
    for index, tap in enumerate(taps):
        at = bisect.bisect_right(starts, tap.t) - 1
        if at < 0 or tap.t >= prompts[at].t1:
            at = -1
        if at < 0:
            window.append(None)
            outcome.append("phantom" if tap.kind in _PHANTOM_KINDS else "free")
            continue
        window.append(at)
        prompt = prompts[at]
        if prompt.side is None:
            outcome.append("unnamed")
        elif (tap.side, tap.finger) != (prompt.side, prompt.finger):
            outcome.append("wrong")
        elif hit[at] is None:
            hit[at] = index
            outcome.append("hit")
        else:
            outcome.append("extra")
    return _Scores(outcome, window, hit)


# ------------------------------------------------------------------------------------------------------ the analysis


def _percentile(values: Sequence[float], q: float) -> float | None:
    return round(float(np.percentile(values, q)), 3) if values else None


def _median(values: Sequence[float]) -> float | None:
    return round(float(np.median(values)), 3) if values else None


def _label(side: str, finger: int) -> str:
    return f"{side}.{FINGER_NAMES[finger]}"


def _jitter(data: TraceData) -> dict[str, Any]:
    """The landmark noise of the still frames (rest, else place): the median over landmarks of the robust spread of the
    second difference of each coordinate, which a steady hand's slow drift does not enter. xy and z in frame widths."""
    for wanted in ("rest", "place"):
        spans = [(s, e) for s, e, kind in data.segments if kind == wanted]
        residuals = []
        for start, end in spans:
            for slot in (0, 1):
                for i in range(start + 1, end - 1):
                    if (
                        bool(data.present[i - 1 : i + 2, slot].all())
                        and float(data.t[i + 1] - data.t[i - 1]) < 2 * _GAP_CAP_S
                    ):
                        lm = data.lm[i - 1 : i + 2, slot].astype(np.float64)
                        residuals.append(lm[1] - 0.5 * (lm[0] + lm[2]))
        if len(residuals) >= 30:
            stack = np.array(residuals) * np.array([1.0, data.aspect, 1.0])
            spread = 1.4826 * np.median(np.abs(stack - np.median(stack, axis=0)), axis=0) / math.sqrt(1.5)
            xy = float(np.median(spread[:, :2]))
            z = float(np.median(spread[:, 2]))
            return {
                "xy": round(xy, 5),
                "z": round(z, 5),
                "frames": len(residuals),
                "from": wanted,
                "warn": z > JITTER_WARN_Z,
            }
    return {"xy": None, "z": None, "frames": 0, "from": "", "warn": False}


def meets_target(recall: float, false_rate: float) -> bool:
    """True when the recording already does what the decision rule asks: thresholds are then left as they are, because
    a value found by shaving two stray taps off a recording is a fit to that recording, not a better setting."""
    return recall >= RECALL_TARGET and false_rate <= FALSE_RATE_LIMIT


def vmax_verdict(refused: int, named: int, rest_per_min: float | None, current: float) -> str:
    """The motion rule: "raise" (suggest ``VMAX_SUGGESTED``), "keep" (nothing to say), "no_rest" (taps are refused for
    motion but the recording holds no rest to show the gate may be loosened) or "rest_noisy" (the rest has strays)."""
    if current >= VMAX_SUGGESTED or not named or refused / named <= MOTION_SHARE_LIMIT:
        return "keep"
    if rest_per_min is None:
        return "no_rest"
    return "raise" if rest_per_min <= REST_PHANTOMS_PER_MIN else "rest_noisy"


class Analysis:
    """One replay of a recording with one tuning; ``report`` is everything the tool prints."""

    def __init__(
        self,
        rec: Recording,
        tuning: Tuning,
        press: PressName | None = None,
        *,
        progress: Callable[[str], None] | None = None,
        hands: _Hands | None = None,
    ) -> None:
        self.rec, self.tuning = rec, tuning
        self.press: PressName = press or rec.data.press
        self._say = progress or (lambda _line: None)
        data = rec.data
        if self.press == "pinch" and any(kind == "drill" for _, _, kind in data.segments):
            raise ReplayError(_PINCH_DRILL)
        self.layout = layout_for("review")
        self.kinds = [data.kind_at(i) for i in range(len(data))]
        # ``hands`` is for a replay that changes no hand-pass field: the same samples, so the pass is not run again
        self.hands = hands if hands is not None else _hand_pass(data, tuning, self.layout)
        self.prompts = _prompts(rec, self.layout)
        #: Air with no warm-up in the file: the depths the warm-up would have measured (see the module docstring).
        self.depths: dict[tuple[Side, int], float] = {}
        self.run = self._press(tuning, None)
        if self.press == "air" and not _has_warmup(data) and self.prompts:
            self.depths = self._stand_in_depths(_classify(self.run.taps, self.prompts))
            if self.depths:
                self.run = self._press(tuning, self.depths)

    # -- running

    @property
    def taps(self) -> list[Tap]:
        return self.run.taps

    def _press(self, tuning: Tuning, depths: Mapping[tuple[Side, int], float] | None, *, aims: bool = True) -> _Run:
        run = _press_pass(self.rec.data, self.hands, tuning, self.press, self.kinds, depths=depths)
        if aims:
            self._aims(run)
        return run

    def _stand_in_depths(self, scores: _Scores) -> dict[tuple[Side, int], float]:
        """No warm-up in the file: the depth ``D_f`` of a finger is the median depth of its prompted taps."""
        found: dict[tuple[Side, int], list[float]] = {}
        for index in scores.hit:
            if index is not None:
                tap = self.run.taps[index]
                found.setdefault((tap.side, tap.finger), []).append(tap.depth)
        return {key: float(np.median(values)) for key, values in found.items() if len(values) >= 2}

    @property
    def placed_t(self) -> float | None:
        placed = self.hands.placed_at
        return float(self.rec.data.t[placed]) if placed is not None else None

    def _sample(self, frame: int, hand: int) -> HandSample | None:
        if 0 <= frame < len(self.hands.samples):
            return next((s for s in self.hands.samples[frame] if s.hand == hand), None)
        return None

    def _nearest(self, t: float) -> int:
        times = self.rec.data.t
        at = int(np.searchsorted(times, t))
        if at > 0 and (at == len(times) or abs(times[at - 1] - t) <= abs(times[at] - t)):
            at -= 1
        return min(at, len(times) - 1)

    def _aims(self, run: _Run) -> None:
        """The aim each rule gives for every air tap, from the hand samples around the tap (2.12.3)."""
        if self.press != "air":
            return
        for tap in run.taps:
            base = self._nearest(tap.onset_t)
            around = [s for j in (base - 1, base, base + 1) if (s := self._sample(j, tap.hand)) is not None]
            at_commit = self._sample(tap.frame, tap.hand)
            at_peak = self._sample(self._nearest(tap.peak_t), tap.hand)
            if not around or at_commit is None or at_peak is None:
                continue
            xs = [float(s.fingers[tap.finger].aim[0]) for s in around]
            ys = [float(s.fingers[tap.finger].aim[1]) for s in around]
            tap.rules = {
                "onset": (float(np.median(xs)), float(np.median(ys))),
                "commit": (float(at_commit.fingers[tap.finger].aim[0]), float(at_commit.fingers[tap.finger].aim[1])),
                "peak": (float(at_peak.fingers[tap.finger].aim[0]), float(at_peak.fingers[tap.finger].aim[1])),
            }

    # -- the report

    def report(self, *, suggest: bool = True, pinned: Iterable[str] = ()) -> dict[str, Any]:
        data = self.rec.data
        run = self.run
        scores = _classify(run.taps, self.prompts)
        times = data.t
        gaps = np.diff(times)
        report: dict[str, Any] = {
            "press": self.press,
            "recorded_for": data.press,
            "version": data.version,
            "frames": len(data),
            "seconds": round(float(times[-1] - times[0]), 3) if len(data) > 1 else 0.0,
            "fps": {
                "mean": round(float((len(data) - 1) / (times[-1] - times[0])), 2)
                if len(data) > 1 and times[-1] > times[0]
                else None,
                "median": round(float(1.0 / np.median(gaps)), 2) if len(gaps) and np.median(gaps) > 0 else None,
            },
            "segments": self._segment_seconds(),
            "jitter": _jitter(data),
            "placed": None
            if self.hands.placement is None
            else {"at": round(self.placed_t - float(times[0]), 3), "pitch": round(self.hands.placement.plane.px, 5)},  # type: ignore[operator]
            "events": len(run.taps),
            "latency_ms": _median([1000.0 * (t.t - t.onset_t) for t in run.taps]),
            "rejects": dict(sorted(run.rejects.items(), key=lambda kv: (-kv[1], kv[0]))),
            "level": self._level(),
            "depth_source": run.depth_source,
            "prompts": self._prompt_block(scores),
            "false_taps": self._false_taps(scores),
            "decision": self._decision(scores),
            "fingers": self._fingers(scores),
            "view": self._view(),
            "phantoms": self._phantoms(),
            "posture": self._posture(),
            "aim": self._aim_table(scores),
            "phrase": self._phrase(),
            "suggest": None,
        }
        if self.press == "pinch":
            report["pinch"] = self._pinch_block()
        if suggest:
            report["suggest"] = self.suggest(pinned)
        return report

    def _segment_seconds(self) -> dict[str, float]:
        totals: dict[str, float] = {}
        times = self.rec.data.t.tolist()
        for i, kind in enumerate(self.kinds):
            step = min(times[i + 1] - times[i], _GAP_CAP_S) if i + 1 < len(times) else 0.0
            totals[kind] = totals.get(kind, 0.0) + step
        return {kind: round(seconds, 3) for kind, seconds in totals.items()}

    def _level(self) -> dict[str, Any] | None:
        """The ladder's verdict at the end of the recording and the readings it was given (2.12.7)."""
        ladder, quality = self.run.ladder, self.run.quality
        if ladder is None or quality is None:
            return None
        start = float(self.rec.data.t[0])
        return {
            "name": ladder.level,
            "reason": ladder.reason,
            "fps": round(quality.fps, 1),
            "noise": None if quality.noise is None else round(quality.noise, 4),
            "gaps": quality.gaps,
            "off_at": None if self.run.off_at is None else round(self.run.off_at - start, 3),
        }

    def _prompt_block(self, scores: _Scores) -> dict[str, Any]:
        counts = Counter(scores.outcome)
        named = [i for i, p in enumerate(self.prompts) if p.side is not None]
        hits = sum(1 for i in named if scores.hit[i] is not None)
        missed = Counter(self._why_missed(i) for i in named if scores.hit[i] is None)
        return {
            "total": len(self.prompts),
            "named": len(named),
            "unnamed": len(self.prompts) - len(named),
            "hit": hits,
            "missed": len(named) - hits,
            "wrong_finger": counts["wrong"],
            "extra": counts["extra"],
            "missed_by": dict(sorted(missed.items(), key=lambda kv: (-kv[1], kv[0]))),
            "recall": round(hits / len(named), 3) if named else None,
        }

    def _false_taps(self, scores: _Scores) -> dict[str, Any]:
        """The taps nobody asked for, added up: a wrong finger or a second tap in a prompt's window, and a tap in a
        rest, wave or talk. Taps that fall in no window of a drill or a typing stretch are not counted as false."""
        counts = Counter(scores.outcome)
        taps = len(self.run.taps)
        false = counts["wrong"] + counts["extra"] + counts["phantom"]
        return {
            "taps": taps,
            "wrong_finger": counts["wrong"],
            "extra": counts["extra"],
            "stray": counts["phantom"],
            "share": round(false / taps, 3) if taps else None,
        }

    def _decision(self, scores: _Scores) -> dict[str, Any] | None:
        """The numbers of the decision rule (L62): the drill's recall pooled over both hands for index and middle, for
        ring and pinky and for the reach keys, and the extra taps as a share of all prompts. A tap is extra when it is
        of a wrong finger, a second one in a prompt's window, or in the drill with no prompt at all."""
        if not self.prompts:
            return None
        reach = {self.layout.find(kind=kind).index for kind, _, _ in AIR_DRILL_REACH_KEYS}
        home = [(i, p) for i, p in enumerate(self.prompts) if p.side is not None and p.key not in reach]

        def pool(indices: list[int]) -> dict[str, Any]:
            hits = sum(1 for i in indices if scores.hit[i] is not None)
            return {"prompts": len(indices), "hits": hits, "recall": round(hits / len(indices), 3) if indices else None}

        extra = sum(
            1
            for tap, outcome in zip(self.run.taps, scores.outcome, strict=True)
            if outcome in ("wrong", "extra") or (outcome == "free" and tap.kind == "drill")
        )
        total = len(self.prompts)
        return {
            "index_middle": pool([i for i, p in home if p.finger in (0, 1)]),
            "ring_pinky": pool([i for i, p in home if p.finger in (2, 3)]),
            "reach": pool([i for i, p in enumerate(self.prompts) if p.key in reach]),
            "extra": {"taps": extra, "of": total, "share": round(extra / total, 3)},
        }

    def _why_missed(self, index: int) -> str:
        """What took a prompted tap away first: a peak a gate refused, a closed hand gate, or nothing at all."""
        prompt = self.prompts[index]
        assert prompt.side is not None and prompt.finger is not None
        for t, why in self.run.refused.get((prompt.side, prompt.finger), []):
            if prompt.t0 <= t < prompt.t1:
                return why
        times = self.rec.data.t
        first, last = int(np.searchsorted(times, prompt.t0)), int(np.searchsorted(times, prompt.t1))
        closed = Counter(g.get(prompt.side, "") for g in self.run.gates[first:last])
        closed.pop("", None)
        if closed and sum(closed.values()) * 3 >= max(last - first, 1):
            return "g_" + closed.most_common(1)[0][0]
        return "no_peak"

    def _fingers(self, scores: _Scores) -> list[dict[str, Any]]:
        out = []
        for side in ("left", "right"):
            for finger in range(4):
                mine = [t for t in self.run.taps if (t.side, t.finger) == (side, finger)]
                prompted = [i for i, p in enumerate(self.prompts) if (p.side, p.finger) == (side, finger)]
                hits = sum(1 for i in prompted if scores.hit[i] is not None)
                wrong = sum(
                    1
                    for k, t in enumerate(self.run.taps)
                    if (t.side, t.finger) == (side, finger) and scores.outcome[k] == "wrong"
                )
                depths = [t.depth for t in mine] if self.press == "air" else []
                key = (side, finger)
                out.append(
                    {
                        "side": side,
                        "finger": finger,
                        "name": FINGER_NAMES[finger],
                        "taps": len(mine),
                        "prompts": len(prompted),
                        "hits": hits,
                        "missed": len(prompted) - hits,
                        "wrong": wrong,
                        "recall": round(hits / len(prompted), 3) if prompted else None,
                        "depth": {
                            "p10": _percentile(depths, 10),
                            "p50": _percentile(depths, 50),
                            "p90": _percentile(depths, 90),
                        },
                        "D": None if key not in self.run.depth else round(self.run.depth[key], 3),
                        "sigma": _median(self.run.sigma.get(key, [])),
                        "theta": _median(self.run.theta.get(key, [])),
                        "latency_ms": _median([1000.0 * (t.t - t.onset_t) for t in mine]),
                    }
                )
        return out

    def _phantom_block(self, run: _Run, kind: str) -> dict[str, Any] | None:
        """The taps of ``run`` in segments of ``kind`` (rest, wave, talk) and the seconds with a hand in view in them;
        None when the recording has no such segment."""
        if kind not in self.kinds:
            return None
        present = self.rec.data.present.any(axis=1)
        times = self.rec.data.t.tolist()
        seconds = 0.0
        for i, k in enumerate(self.kinds):
            if k == kind and present[i] and i + 1 < len(times):
                seconds += min(times[i + 1] - times[i], _GAP_CAP_S)
        taps = [t for t in run.taps if t.kind == kind]
        by_finger = Counter(_label(t.side, t.finger) for t in taps)
        return {
            "seconds": round(seconds, 3),
            "taps": len(taps),
            "per_min": round(60.0 * len(taps) / seconds, 2) if seconds > 0 else None,
            "by_finger": dict(sorted(by_finger.items())),
        }

    def _phantoms(self) -> dict[str, Any]:
        blocks = {kind: self._phantom_block(self.run, kind) for kind in _PHANTOM_KINDS}
        return {kind: block for kind, block in blocks.items() if block is not None}

    def _rest_per_min(self, run: _Run) -> float | None:
        """Stray taps a minute in the rests of ``run``; None when there is no second of rest with a hand in view."""
        block = self._phantom_block(run, "rest")
        return None if block is None else block["per_min"]

    def _view(self) -> dict[str, Any]:
        """How much of the recording each hand was in view (the tracker's hands, so a doubled label counts once)."""
        samples = self.hands.samples
        total = len(samples)
        left = [any(s.side == "left" for s in frame) for frame in samples]
        right = [any(s.side == "right" for s in frame) for frame in samples]

        def share(flags: Iterable[bool]) -> float:
            return round(sum(flags) / total, 3) if total else 0.0

        return {
            "frames": total,
            "both": share(a and b for a, b in zip(left, right, strict=True)),
            "left": share(left),
            "right": share(right),
            "none": share(not (a or b) for a, b in zip(left, right, strict=True)),
        }

    def _posture(self) -> dict[str, Any] | None:
        """Air only: the median lift of each finger over the frames of the rests, and how much of them the posture gate
        was closed (L61). None for a pinch file and for a recording without a rest that has a hand in view."""
        if self.press != "air":
            return None
        lifts: dict[Side, list[list[float]]] = {"left": [[] for _ in range(4)], "right": [[] for _ in range(4)]}
        frames: Counter[str] = Counter()
        shut: Counter[str] = Counter()
        for index, kind in enumerate(self.kinds):
            if kind != "rest":
                continue
            gates = self.run.gates[index] if index < len(self.run.gates) else {}
            for sample in self.hands.samples[index]:
                frames[sample.side] += 1
                shut[sample.side] += gates.get(sample.side) == "posture"
                for finger in range(4):
                    lifts[sample.side][finger].append(sample.fingers[finger].lift)
        sides: dict[str, Any] = {}
        low: list[str] = []
        for side in ("left", "right"):
            if not frames[side]:
                continue
            medians = [round(float(np.median(values)), 3) for values in lifts[side]]
            sides[side] = {"frames": frames[side], "lift": medians, "closed": round(shut[side] / frames[side], 4)}
            low += [_label(side, f) for f, value in enumerate(medians) if value < REST_LIFT_MIN]
        if not sides:
            return None
        ok = not low and all(row["closed"] < POSTURE_CLOSED_MAX for row in sides.values())
        return {"sides": sides, "low": low, "ok": ok}

    def _key_of(self, aim: tuple[float, float]) -> int | None:
        plane = self.hands.placement.plane if self.hands.placement is not None else None
        if plane is None:
            return None
        u, v = plane.units(aim)
        key = self.layout.key_at(u, v, self.tuning.edge_tolerance)
        return key.index if key is not None else None

    def _aim_taps(self, scores: _Scores) -> list[tuple[Tap, int]]:
        """The air taps that were aimed at a known key: a drill hit (its prompted key)."""
        found = []
        for p, index in enumerate(scores.hit):
            if index is not None and self.run.taps[index].rules:
                found.append((self.run.taps[index], self.prompts[p].key))
        return found

    def _aim_table(self, scores: _Scores) -> dict[str, Any] | None:
        if self.press != "air" or self.hands.placement is None:
            return None
        scored = self._aim_taps(scores)
        if not scored:
            return None
        speed = self.tuning.air_aim_speed
        rules: dict[str, dict[str, list[int]]] = {rule: {"still": [0, 0], "moving": [0, 0]} for rule in _RULES}
        for tap, wanted in scored:
            group = "still" if tap.vl <= speed else "moving"
            for rule in _RULES:
                aim = self._aim_for(tap, rule, speed)
                rules[rule][group][1] += 1
                if self._key_of(aim) == wanted:
                    rules[rule][group][0] += 1
        out: dict[str, Any] = {"n": len(scored), "aim_speed": speed, "in_use": self.tuning.air_aim, "rules": {}}
        for rule in _RULES:
            both = [rules[rule]["still"][0] + rules[rule]["moving"][0], len(scored)]
            out["rules"][rule] = {
                "still": rules[rule]["still"],
                "moving": rules[rule]["moving"],
                "all": both,
                "accuracy": round(both[0] / both[1], 3),
            }
        return out

    @staticmethod
    def _aim_for(tap: Tap, rule: str, speed: float) -> tuple[float, float]:
        if rule == "auto":
            return tap.rules["commit"] if tap.vl > speed else tap.rules["onset"]
        return tap.rules[rule]

    def _phrase(self) -> dict[str, Any] | None:
        """Taps in the phrases of a practice trace against the key the script was asking for."""
        data = self.rec.data
        phrase = [t for t in self.run.taps if t.kind == "phrase"]
        if not phrase or self.hands.placement is None:
            return None
        scored = right = 0
        for tap in phrase:
            wanted = int(data.targets[self._nearest(tap.onset_t)])
            if wanted < 0:
                continue
            scored += 1
            right += self._key_of(tap.aim) == wanted
        return {
            "taps": len(phrase),
            "scored": scored,
            "right": right,
            "accuracy": round(right / scored, 3) if scored else None,
        }

    def _pinch_block(self) -> dict[str, Any]:
        warm = self.hands.pinch_warm
        factor = self.tuning.warm_factor
        fingers = []
        for (side, finger), (close, open_) in sorted(warm.thresholds().items()):
            done = (side, finger) in warm.done
            fingers.append(
                {
                    "side": side,
                    "finger": finger,
                    "name": FINGER_NAMES[finger],
                    "recorded": done,
                    "r_min": round(close / factor, 3) if done else None,
                    "close": round(close, 3),
                    "open": round(open_, 3),
                }
            )
        return {"warm": fingers, "levels": {side: list(levels) for side, levels in self.hands.levels.items()}}

    # -- the suggestions

    def _measure(self, run: _Run) -> tuple[float, float, int]:
        """(recall, false-tap share, prompts) of one pass; the objective of the search."""
        scores = _classify(run.taps, self.prompts)
        named = [i for i, p in enumerate(self.prompts) if p.side is not None]
        hits = sum(1 for i in named if scores.hit[i] is not None)
        bad = sum(1 for o in scores.outcome if o in ("wrong", "extra", "phantom"))
        return (hits / len(named) if named else 0.0), (bad / len(run.taps) if run.taps else 0.0), len(named)

    @staticmethod
    def _better(new: tuple[float, float], old: tuple[float, float]) -> bool:
        """Recall counts while the false-tap share is within its limit; past the limit, fewer false taps win.

        Taps that find no prompt at all are never better than taps that find some: a detector that fires on nothing
        has no false taps either.
        """
        if new[0] <= 0.0 < old[0]:
            return False
        new_ok, old_ok = new[1] <= FALSE_RATE_LIMIT, old[1] <= FALSE_RATE_LIMIT
        if new_ok != old_ok:
            return new_ok
        if new_ok:
            return new[0] > old[0] + 1e-9 or (abs(new[0] - old[0]) <= 1e-9 and new[1] < old[1] - 1e-9)
        return new[1] < old[1] - 1e-9

    def suggest(self, pinned: Iterable[str] = ()) -> dict[str, Any] | None:
        """The values that would do better on this recording, and what they would do; None when it cannot say."""
        pinned = frozenset(pinned)
        values: dict[str, Any] = {}
        notes: list[str] = []
        base: dict[str, Any] = {}
        if self.press == "air" and len(self.prompts) >= MIN_PROMPTS_TO_SUGGEST:
            values, base, notes = self._search_air(pinned)
        elif self.press == "air":
            notes.append("A drill of at least 20 prompts is needed to suggest the air tap's numbers.")
        if self.press == "pinch":
            values.update(self._plane_values(pinned))
            values.update(self._pinch_values(pinned))
        values = {k: v for k, v in values.items() if v != getattr(self.tuning, k)}
        return {"values": values, "notes": notes, **base}

    def _plane_values(self, pinned: frozenset[str]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        placement = self.hands.placement
        if placement is not None and "pitch" not in pinned:
            low, high = RANGES["pitch"]
            out["pitch"] = round(min(max(placement.plane.px, low), high), 5)
        learned = list(self.hands.levels.values())
        if learned and "level_palm" not in pinned:
            mean = np.mean(np.array(learned), axis=0)
            low, high = LEVEL_PALM_RANGE
            out["level_palm"] = tuple(round(float(min(max(x, low), high)), 3) for x in mean)
        return out

    def _pinch_values(self, pinned: frozenset[str]) -> dict[str, Any]:
        recorded = [f["close"] for f in self._pinch_block()["warm"] if f["recorded"]]
        if len(recorded) >= 4 and "close" not in pinned:
            low, high = RANGES["close"]
            return {"close": round(min(max(float(np.median(recorded)), low), high), 3)}
        return {}

    def _search_air(self, pinned: frozenset[str]) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
        """One field at a time, each over its candidates; a change is kept only if the recording does better.

        The search holds the finger depths ``D_f`` of the baseline fixed (a warm-up is done once, whatever is tuned
        afterwards). What it finds is then replayed whole, as ``--set`` would, and the numbers reported are that
        replay's, so they are what the person gets by trying the values.
        """
        depths = self.depths or None
        tuning = self.tuning
        base = self._measure(self.run)[:2]
        best = base
        found: dict[str, Any] = {}
        notes: list[str] = []
        fields = [
            name for name in _CANDIDATES if name not in pinned and not (name == "air_depth_frac" and not self.run.depth)
        ]
        if meets_target(*base):
            fields = []
            notes.append(
                f"The recording already meets the target (recall {_pct(base[0])}, false taps {_pct(base[1])} of taps): "
                "no threshold is changed."
            )
        else:
            total = sum(len(_CANDIDATES[name]) for name in fields)
            self._say(f"Looking for better values (about {total} replays of the press)...")
        for name in fields:
            # nearest to the current value first: among equally good values the smallest change wins
            for value in sorted(_CANDIDATES[name], key=lambda v, n=name: abs(v - getattr(self.tuning, n))):
                if value == getattr(tuning, name):
                    continue
                trial = replace(tuning, **{name: value})
                if not _fits(trial):
                    continue
                score = self._measure(self._press(trial, depths, aims=False))[:2]
                if self._better(score, best):
                    best, tuning, found = score, trial, {**found, name: value}
        stats: dict[str, Any] = {"base_recall": round(base[0], 3), "base_false_rate": round(base[1], 3)}
        if found:
            checked = Analysis(self.rec, tuning, self.press, hands=self.hands)
            best = checked._measure(checked.run)[:2]
            if not self._better(best, base):
                found, best, tuning = {}, base, self.tuning
                notes.append("No value did better when the recording was replayed with it.")
        # the aim rule needs no replay: the aims of every rule are known for each prompted tap
        found.update(self._aim_choice(pinned))
        gate = self._motion_gate(pinned, tuning)
        if gate is not None:
            stats["motion"], loosened, said = gate
            notes += said
            if loosened is not None:
                found["air_vmax_gate"] = VMAX_SUGGESTED
                best = loosened._measure(loosened.run)[:2]
        return found, {**stats, "recall": round(best[0], 3), "false_rate": round(best[1], 3)}, notes

    def _motion_gate(
        self, pinned: frozenset[str], tuning: Tuning
    ) -> tuple[dict[str, Any], Analysis | None, list[str]] | None:
        """The motion rule: what the recording says about ``air_vmax_gate``, and the replay with it loosened.

        None when there is nothing to say (the field is pinned, already as loose as the suggestion, or fewer than a
        sixth of the named prompts were refused for motion). Otherwise the facts, and the replay of ``tuning`` with the
        gate at ``VMAX_SUGGESTED`` when the rule says to raise it and that replay's own rest is still quiet.
        """
        if "air_vmax_gate" in pinned:
            return None
        named = sum(1 for p in self.prompts if p.side is not None)
        refused = self._prompts_missed_for("motion")
        rest = self._rest_per_min(self.run)
        verdict = vmax_verdict(refused, named, rest, self.tuning.air_vmax_gate)
        if verdict == "keep":
            return None
        facts: dict[str, Any] = {
            "refused": refused,
            "named": named,
            "rest": rest,
            "rest_after": None,
            "wave_after": None,
            "raised": False,
        }
        notes: list[str] = []
        if verdict == "no_rest":
            notes.append(
                f"Taps are refused for motion ({refused} of {named}), but there is no rest in the recording to show "
                "that air_vmax_gate can be raised: record a rest of 20 s with it."
            )
        elif verdict == "rest_noisy":
            notes.append(
                f"Taps are refused for motion ({refused} of {named}), but the rest has {rest:g} stray taps a minute "
                "already: air_vmax_gate stays."
            )
        else:
            loosened = Analysis(self.rec, replace(tuning, air_vmax_gate=VMAX_SUGGESTED), self.press, hands=self.hands)
            after = loosened._rest_per_min(loosened.run)
            facts["rest_after"] = after
            wave = loosened._phantom_block(loosened.run, "wave")
            facts["wave_after"] = None if wave is None else wave["per_min"]
            if after is not None and after <= REST_PHANTOMS_PER_MIN:
                facts["raised"] = True
                return facts, loosened, notes
            notes.append(
                f"Taps are refused for motion ({refused} of {named}), but with air_vmax_gate={VMAX_SUGGESTED:g} the "
                f"rest would have {_num(after, 1)} stray taps a minute: air_vmax_gate stays."
            )
        return facts, None, notes

    def _prompts_missed_for(self, why: str) -> int:
        """How many named prompts were missed, first of all, because a gate refused their peak for ``why``."""
        scores = _classify(self.run.taps, self.prompts)
        return sum(
            1
            for i, p in enumerate(self.prompts)
            if p.side is not None and scores.hit[i] is None and self._why_missed(i) == why
        )

    def _aim_choice(self, pinned: frozenset[str]) -> dict[str, Any]:
        scores = _classify(self.run.taps, self.prompts)
        scored = self._aim_taps(scores)
        if len(scored) < MIN_PROMPTS_TO_SUGGEST // 2 or self.hands.placement is None:
            return {}

        def accuracy(rule: str, speed: float) -> float:
            return sum(self._key_of(self._aim_for(t, rule, speed)) == wanted for t, wanted in scored) / len(scored)

        out: dict[str, Any] = {}
        current = self.tuning.air_aim_speed
        best_speed = max(_AIM_SPEEDS, key=lambda s: (accuracy("auto", s), -abs(s - current)))
        if "air_aim_speed" not in pinned and accuracy("auto", best_speed) > accuracy("auto", current) + 1e-9:
            out["air_aim_speed"] = best_speed
        speed = out.get("air_aim_speed", current)
        scores_by = {
            "auto": accuracy("auto", speed),
            "onset": accuracy("onset", speed),
            "commit": accuracy("commit", speed),
        }
        if "air_aim" not in pinned:
            top = max(scores_by.values())
            if scores_by[self.tuning.air_aim] < top - _AIM_TIE:
                # a tie goes to the first named, so to ``auto``: the rule that needs no choosing
                out["air_aim"] = max(("auto", "onset", "commit"), key=lambda r: scores_by[r])
        return out

    # -- the per-frame numbers

    def csv_rows(self) -> Iterable[list[Any]]:
        yield ["t", "segment", "side", *(f"ratio_{i}" for i in range(4)), *(f"lift_{i}" for i in range(4)), "speed"]
        start = float(self.rec.data.t[0])
        for index, samples in enumerate(self.hands.samples):
            for s in samples:
                yield [
                    round(float(self.rec.data.t[index]) - start, 4), self.kinds[index], s.side,
                    *(round(f.ratio, 4) for f in s.fingers), *(round(f.lift, 4) for f in s.fingers), round(s.speed, 4),
                ]  # fmt: skip


def analyse(
    rec: Recording,
    tuning: Tuning,
    press: PressName | None = None,
    *,
    suggest: bool = True,
    pinned: Iterable[str] = (),
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    return Analysis(rec, tuning, press, progress=progress).report(suggest=suggest, pinned=pinned)


# ---------------------------------------------------------------------------------------------------------- the text


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{100 * value:.0f}%"


def _num(value: float | None, digits: int = 3) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def render(report: Mapping[str, Any]) -> list[str]:
    """The report as lines of text. Counts, times and finger labels only: never a key, a character or a phrase."""
    name = "air tap" if report["press"] == "air" else "pinch"
    lines = [
        f"keyreplay: the {name}, {report['frames']} frames, {report['seconds']:.1f} s, "
        f"{_num(report['fps']['median'], 1)} fps by the median frame gap, "
        f"{_num(report['fps']['mean'], 1)} fps over the whole recording "
        f"(recorded for the {report['recorded_for']}, file version {report['version']})",
        "segments: " + (", ".join(f"{kind} {seconds:.1f} s" for kind, seconds in report["segments"].items()) or "none"),
        _render_jitter(report["jitter"]),
    ]
    placed = report["placed"]
    lines.append(
        "placement: no still hands were found"
        if placed is None
        else f"placement: at {placed['at']:.1f} s, pitch {placed['pitch']:.4f}"
    )
    level = report["level"]
    if level is not None:
        off = "" if level["off_at"] is None else f", off at {level['off_at']:.1f} s"
        lines.append(
            f"camera: level {level['name']}{off}, fps {level['fps']}, noise {_num(level['noise'], 4)}, "
            f"{level['gaps']} gaps"
        )
    lines.append(f"taps counted: {report['events']}, median onset-to-commit {_num(report['latency_ms'], 0)} ms")
    view = report["view"]
    lines.append(
        f"hands in view: both {_pct(view['both'])}, left {_pct(view['left'])}, right {_pct(view['right'])}, "
        f"none {_pct(view['none'])} of the frames"
    )
    lines.extend(_render_prompts(report["prompts"]))
    lines.extend(_render_false(report["false_taps"], bool(report["prompts"]["total"] or report["phantoms"])))
    lines.extend(_render_decision(report["decision"]))
    lines.extend(_render_fingers(report["fingers"], report["press"] == "air"))
    if report["depth_source"] == "prompted taps":
        lines.append("D is the median depth of each finger's own prompted taps: the recording has no warm-up")
    if report["rejects"]:
        lines.append("rejects: " + ", ".join(f"{k} {v}" for k, v in report["rejects"].items()))
    for kind, block in report["phantoms"].items():
        split = ", ".join(f"{k} {v}" for k, v in block["by_finger"].items())
        lines.append(
            f"phantoms in {kind}: {block['taps']} taps in {block['seconds']:.0f} s, "
            f"{_num(block['per_min'], 1)} a minute" + (f" ({split})" if split else "")
        )
    lines.extend(_render_posture(report["posture"]))
    lines.extend(_render_aim(report["aim"]))
    phrase = report["phrase"]
    if phrase is not None:
        lines.append(f"phrases: {phrase['taps']} taps, {phrase['right']} of {phrase['scored']} on the asked key")
    pinch = report.get("pinch")
    for f in pinch["warm"] if pinch is not None else ():
        seen = f"r_min {f['r_min']:.2f}, " if f["recorded"] else "no pinch seen, "
        lines.append(f"warm-up {f['side']}.{f['name']}: {seen}close {f['close']:.2f}, open {f['open']:.2f}")
    lines.extend(_render_suggest(report["suggest"]))
    return lines


def _render_jitter(jitter: Mapping[str, Any]) -> str:
    if jitter["xy"] is None:
        return "landmark noise: no still stretch in this recording (record a rest segment)"
    warning = f"  WARNING: z above {JITTER_WARN_Z} (the pinch fails near 0.020)" if jitter["warn"] else ""
    return f"landmark noise (from {jitter['from']}): xy {jitter['xy']:.4f}, z {jitter['z']:.4f} frame widths{warning}"


def _render_prompts(prompts: Mapping[str, Any]) -> list[str]:
    if not prompts["total"]:
        return []
    why = ", ".join(f"{k} {v}" for k, v in prompts["missed_by"].items())
    return [
        f"drill: {prompts['total']} prompts ({prompts['unnamed']} without a named finger), {prompts['hit']} hit "
        f"({_pct(prompts['recall'])} of the named), {prompts['wrong_finger']} wrong finger, "
        f"{prompts['extra']} extra, {prompts['missed']} missed" + (f" ({why})" if why else "")
    ]


def _render_false(false: Mapping[str, Any], worth_saying: bool) -> list[str]:
    if not false["taps"] or not worth_saying:
        return []
    count = false["wrong_finger"] + false["extra"] + false["stray"]
    return [
        f"false taps: {count} of {false['taps']} taps ({_pct(false['share'])}): {false['wrong_finger']} wrong finger, "
        f"{false['extra']} extra, {false['stray']} stray in the rests"
    ]


def _bar(value: float | None, bar: float, *, most: bool = False) -> str:
    """``met`` or ``below`` (``above`` for a share that must stay under its bar) the bar of the live test."""
    if value is None:
        return "no prompts"
    return ("met" if value <= bar else "above") if most else ("met" if value >= bar else "below")


def _render_decision(decision: Mapping[str, Any] | None) -> list[str]:
    if decision is None:
        return []

    def recall(block: Mapping[str, Any], what: str, bar: float | None, word: str = "bar") -> str:
        text = f"{what} recall {block['hits']} of {block['prompts']}"
        if block["recall"] is None:
            return text + " (no prompts)"
        verdict = "" if bar is None else f", {word} {bar:.0%}: {_bar(block['recall'], bar)}"
        return f"{text} ({block['recall']:.0%}{verdict})"

    extra = decision["extra"]
    parts = [
        recall(decision["index_middle"], "index+middle", IM_RECALL_BAR),
        f"extra taps {extra['taps']} of {extra['of']} prompts ({extra['share']:.1%}, bar {EXTRA_BAR:.0%}: "
        f"{_bar(extra['share'], EXTRA_BAR, most=True)})",
        recall(decision["reach"], "reach", REACH_RECALL_BAR),
        recall(decision["ring_pinky"], "ring+pinky", RING_PINKY_TARGET, "target"),
    ]
    return ["decision rule (L62): " + "; ".join(parts)]


def _render_fingers(fingers: Sequence[Mapping[str, Any]], air: bool) -> list[str]:
    if not air:
        lines = ["finger         taps  ms"]
        lines += [f"{f['side'] + '.' + f['name']:<14}{f['taps']:>4}{_num(f['latency_ms'], 0):>4}" for f in fingers]
        return lines
    lines = ["finger         taps prompts  hit recall  depth p10  p50  p90      D  sigma theta  ms"]
    for f in fingers:
        d = f["depth"]
        lines.append(
            f"{f['side'] + '.' + f['name']:<14}{f['taps']:>4}{f['prompts']:>8}{f['hits']:>5}{_pct(f['recall']):>7}"
            f"  {_num(d['p10'], 2):>11}{_num(d['p50'], 2):>5}{_num(d['p90'], 2):>5}"
            f"{_num(f['D'], 2):>7}{_num(f['sigma'], 3):>7}{_num(f['theta'], 3):>6}{_num(f['latency_ms'], 0):>4}"
        )
    return lines


def _render_posture(posture: Mapping[str, Any] | None) -> list[str]:
    if posture is None:
        return []
    lines = ["posture at rest (index, middle, ring, pinky):"]
    for side, row in posture["sides"].items():
        lift = " ".join(f"{x:.2f}" for x in row["lift"])
        lines.append(
            f"  {side:<5} lift {lift}, posture gate closed {100 * row['closed']:.1f}% of {row['frames']} frames"
        )
    parts = []
    if posture["low"]:
        parts.append(f"a median rest lift under {REST_LIFT_MIN:.2f} ({', '.join(posture['low'])})")
    shut = [side for side, row in posture["sides"].items() if row["closed"] >= POSTURE_CLOSED_MAX]
    if shut:
        parts.append(f"the posture gate closed {POSTURE_CLOSED_MAX:.0%} of the rest or more ({', '.join(shut)})")
    if parts:
        lines.append(
            "  WARNING: " + " and ".join(parts) + ": the fingers droop, or the camera sees them from too far above"
        )
    return lines


def _render_aim(aim: Mapping[str, Any] | None) -> list[str]:
    if aim is None:
        return []
    lines = [
        f"aim rules, prompted taps against their keys (n {aim['n']}; still = anchor speed up to {aim['aim_speed']}):"
    ]
    for rule, row in aim["rules"].items():
        use = "   (in use)" if rule == aim["in_use"] else ""
        lines.append(
            f"  {rule:<7}{_pct(row['accuracy']):>5}   still {row['still'][0]}/{row['still'][1]}"
            f"   moving {row['moving'][0]}/{row['moving'][1]}{use}"
        )
    return lines


def _render_suggest(suggest: Mapping[str, Any] | None) -> list[str]:
    if suggest is None:
        return []
    lines = [*suggest["notes"]]
    motion = suggest.get("motion")
    if motion is not None and motion["raised"]:
        wave = "" if motion["wave_after"] is None else f", waving {_num(motion['wave_after'], 1)}"
        lines.append(
            f"motion gate: {motion['refused']} of {motion['named']} prompted taps were refused for motion and the rest "
            f"has {_num(motion['rest'], 1)} stray taps a minute; at {VMAX_SUGGESTED:g} the rest has "
            f"{_num(motion['rest_after'], 1)} a minute{wave} (waving and talking get through more: check the practice "
            "rest)"
        )
    if "recall" in suggest:
        lines.append(
            f"with the suggested values: recall {_pct(suggest['recall'])} (now {_pct(suggest['base_recall'])}), "
            f"false taps {_pct(suggest['false_rate'])} of taps (now {_pct(suggest['base_false_rate'])})"
        )
    if suggest["values"]:
        lines.append("suggested: " + ", ".join(f"{k}={_show(v)}" for k, v in suggest["values"].items()))
    else:
        lines.append("suggested: nothing to change")
    return lines


def _show(value: Any) -> str:
    if isinstance(value, tuple | list):
        return ",".join(f"{x:g}" for x in value)
    return f"{value:g}" if isinstance(value, float) else str(value)


# ------------------------------------------------------------------------------------------------------------- run


def _write_csv(path: Path, rows: Iterable[Sequence[Any]]) -> None:
    temp = path.with_name(path.name + ".tmp")
    try:
        with temp.open("w", newline="", encoding="utf-8") as out:
            csv.writer(out).writerows(rows)
        os.replace(temp, path)
    except OSError:
        temp.unlink(missing_ok=True)
        raise


def run(args: argparse.Namespace, *, out: TextIO | None = None) -> int:
    """0 when the report was printed (and written, if asked), 1 when the tuning file or the csv could not be written or
    Ctrl+C stopped the replay, 2 when the arguments or the file were refused. Every refusal is one fixed line."""
    stream = out if out is not None else sys.stdout

    def say(line: str) -> None:
        print(line, file=stream, flush=True)

    csv_path = Path(args.csv) if args.csv else None
    try:
        rec = load(args.file)
        data_dir = _data_dir(args)
        base = parse_sets(args.sets, load_tuning(data_dir))
        if csv_path is not None and (csv_path.suffix != ".csv" or csv_path.exists()):
            raise ReplayError("--csv is a new file name ending in .csv.")
        analysis = Analysis(rec, base, args.press, progress=say)
        pinned = set_names(args.sets)
        report = analysis.report(suggest=not args.no_suggest, pinned=pinned)
    except ReplayError as exc:
        say(str(exc))
        return EXIT_REFUSED
    except KeyboardInterrupt:  # a long recording takes a minute; nothing has been written yet
        say("Stopped with Ctrl+C. Nothing was written.")
        return EXIT_FAILED
    if args.sets:
        say("settings tried: " + ", ".join(args.sets))
    for line in render(report):
        say(line)
    code = EXIT_OK
    if csv_path is not None:
        try:
            _write_csv(csv_path, analysis.csv_rows())
            say("Wrote the per-frame numbers to the csv file.")
        except OSError:
            say("The csv file could not be written.")
            code = EXIT_FAILED
    if args.write:
        code = max(code, _write(args, base, report, pinned, say))
    return code


def _write(
    args: argparse.Namespace,
    base: Tuning,
    report: Mapping[str, Any],
    pinned: frozenset[str],
    say: Callable[[str], None],
) -> int:
    values: dict[str, Any] = dict(report["suggest"]["values"]) if report["suggest"] else {}
    for name in pinned:
        values[name] = getattr(base, name)  # what the person asked for is written as asked, suggestion or not
    if not values:
        say("Nothing to write: no suggested value differs from the current one.")
        return EXIT_OK
    path = tuning_path(_data_dir(args))
    try:
        written = write_tuning(path, values)
    except ReplayError as exc:
        say(str(exc))
        return EXIT_FAILED
    say("Wrote to the tuning file: " + ", ".join(f"{name}={_show(_clamped(name, values[name]))}" for name in written))
    return EXIT_OK
