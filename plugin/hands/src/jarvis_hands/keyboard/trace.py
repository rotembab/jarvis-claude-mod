"""The practice tap log and the landmark trace: writers, readers and clean-up (DESIGN-KEYBOARD.md 5.7, 2.12.10, 3.14).

Both files exist only in practice (X47): the controller builds a ``TapLog`` and a ``TraceWriter`` when it opens a
practice session and never in a live one, because a live key histogram is typed text (SR13). What they hold is
numbers: times, sides, finger numbers, ratios, depths, key *indices* of the prompted phrases, and landmarks. No pixel,
no character, no phrase and no window name. The tap-log writer keeps that promise itself: it writes the fields of the
design and nothing else, and a text field only when its value is one of the fixed words of the record it belongs to.

The trace is one ``.npz`` per practice, ``keyboard-trace-<ts>.npz``, version 2::

    t        float64 [n]            frame time
    present  bool    [n, 2]         a hand is in this slot
    side     int8    [n, 2]         0 left, 1 right, -1 absent
    score    float32 [n, 2]         handedness confidence, 0 where absent
    lm       float32 [n, 2, 21, 3]  image landmarks, NaN where absent
    aspect, width, height           the camera frame
    segments structured [m]         (start, end, kind): frame indices, end exclusive, kind a short lowercase word
    targets  int16   [n]            the prompted key index, or -1
    press    "air" | "pinch"        the method the recording is for
    version  2                      (version 1 has no ``press``: it reads as pinch)

Slot 0 is the hand whose wrist is further left in the picture. The module imports numpy and the standard library only
(and the landmark types it rebuilds frames from).
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import numpy as np

from ..landmarks import Frame, HandObservation
from .limits import TRACE_KEEP_DAYS
from .types import PressName

TAP_LOG_NAME: Final = "keyboard-practice.jsonl"
#: 1 MB x 3 files (5.7): the live file and two older ones.
TAP_LOG_MAX_BYTES: Final = 1_000_000
TAP_LOG_FILES: Final = 3
TRACE_MAX_S: Final = 600.0
TRACE_VERSION: Final = 2
TRACE_PREFIX: Final = "keyboard-trace-"
_DAY_S: Final = 86400.0
_KIND = re.compile(r"[a-z]{1,12}")

# ----------------------------------------------------------------------------------------------------- the tap log

#: The closed vocabularies of the text fields (2.12.10, F24).
OUTCOMES: Final = frozenset({"key", "off", "held", "stale", "queue", "not_armed", "warmup_tap", "practice_review_key"})
_WHY: Final = frozenset(
    {"plateau", "narrow", "wide", "motion", "pinched", "veto", "excl", "coherence_raw", "tremor"}
    | {f"gate_{g}" for g in ("speed", "posture", "hold", "score", "warm", "coherence", "refractory")}
)
_TEXT_FIELDS: Final[Mapping[str, frozenset[str]]] = {
    "k": frozenset({"fire", "reject", "gate"}),
    "side": frozenset({"left", "right"}),
    "finger": frozenset({"index", "middle", "ring", "pinky"}),  # the pinch line names it; the air records number it
    "outcome": OUTCOMES,
    "aimRule": frozenset({"onset", "commit", "auto", "peak", "fire"}),
    "why": _WHY,
    "gate": frozenset({"coherence", "coherence_raw", "tremor"}),
}
_COMMON = {"t", "side", "finger", "u", "v", "outcome", "target", "hit"}
_ALLOWED: Final[Mapping[str, frozenset[str]]] = {
    # the pinch line has no "k"
    "pinch": frozenset(_COMMON | {"ratio", "margin", "closingMs"}),
    "fire": frozenset(
        _COMMON
        | {"k", "hand", "onsetT", "pkT", "depth", "theta", "sigma", "margin", "conf", "widthMs", "riseMs", "fallMs"}
        | {"speed", "vmax", "nPeers", "E", "delta", "win", "aim", "aimRule", "vl", "fps"}
    ),
    "reject": frozenset({"k", "t", "hand", "side", "finger", "why", "rise", "theta", "width", "vmax"}),
    "gate": frozenset({"k", "t", "hand", "side", "gate", "dur"}),
}


def _clean(key: str, value: Any) -> tuple[bool, Any]:
    """(keep, value): a number (NaN and infinity become null), a list of numbers or one of the key's fixed words."""
    if isinstance(value, np.ndarray):
        value = value.tolist()
    elif isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, bool):
        return False, None
    if isinstance(value, int):
        return True, value
    if isinstance(value, float):
        return True, (value if math.isfinite(value) else None)
    if isinstance(value, str):
        words = _TEXT_FIELDS.get(key)
        return (True, value) if words is not None and value in words else (False, None)
    if isinstance(value, list | tuple):
        out: list[Any] = []
        for item in value:
            keep, item = _clean("", item)  # no key: a word inside a list is never allowed
            if not keep:
                return False, None
            out.append(item)
        return True, out
    return False, None


def tap_log_line(record: Mapping[str, Any]) -> str | None:
    """One JSON line for ``record``, or None when it is not a tap-log record. Fields outside the design are dropped."""
    shape = record.get("k", "pinch")
    allowed = _ALLOWED.get(shape) if isinstance(shape, str) else None
    if allowed is None:
        return None
    line: dict[str, Any] = {}
    for key, value in record.items():
        if key in allowed:
            keep, cleaned = _clean(key, value)
            if keep:
                line[key] = cleaned
    return json.dumps(line, separators=(",", ":"), allow_nan=False)


class TapLog:
    """``keyboard-practice.jsonl``: one line per press attempt, rotated at ``max_bytes`` over ``files`` files."""

    def __init__(self, data_dir: Path, *, max_bytes: int = TAP_LOG_MAX_BYTES, files: int = TAP_LOG_FILES) -> None:
        if max_bytes < 1 or files < 1:
            raise ValueError("a tap log needs a size and a file count")
        self.path = Path(data_dir) / "hands" / TAP_LOG_NAME
        self._max = max_bytes
        self._files = files

    def write(self, records: Iterable[Mapping[str, Any]]) -> int:
        """Appends the records; returns how many lines were written."""
        lines = [line + "\n" for record in records if (line := tap_log_line(record)) is not None]
        if not lines:
            return 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        size = len("".join(lines).encode("utf-8"))
        try:
            current = self.path.stat().st_size
        except OSError:
            current = 0
        if current > 0 and current + size > self._max:
            self._rotate()
        with self.path.open("a", encoding="utf-8") as out:
            out.write("".join(lines))
        return len(lines)

    def _rotate(self) -> None:
        """``.jsonl`` -> ``.jsonl.1`` -> ``.jsonl.2``; the oldest goes."""
        oldest = self._files - 1
        if oldest < 1:
            self.path.unlink(missing_ok=True)
            return
        for number in range(oldest, 0, -1):
            older = self.path.with_name(f"{self.path.name}.{number}")
            newer = self.path if number == 1 else self.path.with_name(f"{self.path.name}.{number - 1}")
            if number == oldest:
                older.unlink(missing_ok=True)
            if newer.exists():
                os.replace(newer, older)


# --------------------------------------------------------------------------------------------------------- clean-up


def cleanup(data_dir: Path, *, now: float | None = None) -> None:
    """Deletes the traces older than ``TRACE_KEEP_DAYS``; called at helper start and at every session open.

    The age is the file's modification time. Never raises: a file that cannot be read or removed is left for the next
    call. Nothing else in the folder is touched.
    """
    folder = Path(data_dir) / "hands"
    cutoff = (time.time() if now is None else now) - TRACE_KEEP_DAYS * _DAY_S
    try:
        names = list(folder.glob(f"{TRACE_PREFIX}*.npz*"))
    except OSError:
        return
    for path in names:
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            continue


# ----------------------------------------------------------------------------------------------------------- the trace


class TraceError(ValueError):
    """A trace file that is not one. The text is fixed: it never carries a path or a value."""


def trace_path(data_dir: Path, when: datetime | None = None) -> Path:
    stamp = (when or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return Path(data_dir) / "hands" / f"{TRACE_PREFIX}{stamp}.npz"


class TraceWriter:
    """Collects the frames of a practice (or of ``keytrace``) and saves them as one ``.npz``."""

    def __init__(self, *, press: PressName, max_s: float = TRACE_MAX_S) -> None:
        if press not in ("air", "pinch"):
            raise ValueError("a trace is recorded for the air or the pinch method")
        self.press = press
        self._max_s = max_s
        self._t: list[float] = []
        self._present: list[list[bool]] = []
        self._side: list[list[int]] = []
        self._score: list[list[float]] = []
        self._lm: list[np.ndarray] = []
        self._kinds: list[str] = []
        self._targets: list[int] = []
        self._size: tuple[int, int] | None = None
        self.full = False

    def __len__(self) -> int:
        return len(self._t)

    def add(self, frame: Frame, kind: str, target: int = -1) -> bool:
        """One frame. False once the recording is full (``max_s`` seconds) or when the frame does not fit the first."""
        if not _KIND.fullmatch(kind):
            raise ValueError("a segment kind is a short lowercase word")
        if self.full:
            return False
        if self._size is None:
            self._size = (frame.width, frame.height)
        elif self._size != (frame.width, frame.height):
            return False
        if self._t and frame.t - self._t[0] > self._max_s:
            self.full = True
            return False
        hands = sorted(frame.hands, key=lambda h: float(h.image[0, 0]))
        if len(hands) > 2:
            hands = sorted(sorted(hands, key=lambda h: -h.score)[:2], key=lambda h: float(h.image[0, 0]))
        lm = np.full((2, 21, 3), np.nan, dtype=np.float32)
        present, side, score = [False, False], [-1, -1], [0.0, 0.0]
        for slot, hand in enumerate(hands):
            lm[slot] = hand.image
            present[slot] = True
            side[slot] = 0 if hand.handedness == "left" else 1
            score[slot] = float(hand.score)
        self._t.append(float(frame.t))
        self._present.append(present)
        self._side.append(side)
        self._score.append(score)
        self._lm.append(lm)
        self._kinds.append(kind)
        self._targets.append(int(target))
        return True

    def segments(self) -> list[tuple[int, int, str]]:
        """``(start, end, kind)`` runs of equal kind; end is exclusive."""
        out: list[tuple[int, int, str]] = []
        for index, kind in enumerate(self._kinds):
            if out and out[-1][2] == kind:
                out[-1] = (out[-1][0], index + 1, kind)
            else:
                out.append((index, index + 1, kind))
        return out

    def save(self, path: Path) -> Path:
        """Writes the file atomically; the folder is made when it is missing."""
        n = len(self._t)
        width, height = self._size or (0, 0)
        segments = np.array(self.segments(), dtype=[("start", "<i4"), ("end", "<i4"), ("kind", "<U12")])
        arrays: dict[str, Any] = {
            "t": np.array(self._t, dtype=np.float64),
            "present": np.array(self._present, dtype=bool).reshape(n, 2),
            "side": np.array(self._side, dtype=np.int8).reshape(n, 2),
            "score": np.array(self._score, dtype=np.float32).reshape(n, 2),
            "lm": np.array(self._lm, dtype=np.float32).reshape(n, 2, 21, 3),
            "aspect": np.float64(height / width if width else 0.0),
            "width": np.int32(width),
            "height": np.int32(height),
            "segments": segments,
            "targets": np.array(self._targets, dtype=np.int16),
            "press": np.str_(self.press),
            "version": np.int32(TRACE_VERSION),
        }
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        try:
            with temp.open("wb") as out:
                np.savez_compressed(out, **arrays)
            os.replace(temp, path)
        except OSError:
            temp.unlink(missing_ok=True)
            raise
        return path


@dataclass(frozen=True, eq=False)
class TraceData:
    t: np.ndarray
    present: np.ndarray
    side: np.ndarray
    score: np.ndarray
    lm: np.ndarray
    aspect: float
    width: int
    height: int
    segments: tuple[tuple[int, int, str], ...]
    targets: np.ndarray
    press: PressName
    version: int

    def __len__(self) -> int:
        return len(self.t)

    def frames(self) -> Iterator[Frame]:
        """The recorded frames as the tracker gets them. The metric landmarks are not recorded (the keyboard's tracker
        never reads them), so ``world`` is zeros."""
        zeros = np.zeros((21, 3))
        for i in range(len(self.t)):
            hands = tuple(
                HandObservation(
                    "left" if self.side[i, s] == 0 else "right",
                    float(self.score[i, s]),
                    self.lm[i, s].astype(np.float64),
                    zeros,
                )
                for s in range(2)
                if self.present[i, s]
            )
            yield Frame(float(self.t[i]), hands, self.width, self.height)

    def kind_at(self, index: int) -> str:
        for start, end, kind in self.segments:
            if start <= index < end:
                return kind
        return ""


def read_trace(path: Path) -> TraceData:
    """Reads a trace of version 1 or 2; anything else raises ``TraceError`` with a fixed text.

    Nothing is unpickled: a file that needs it is refused. A version 1 file has no ``press`` and reads as pinch.
    """
    try:
        with np.load(Path(path), allow_pickle=False) as npz:
            data = {name: npz[name] for name in npz.files}
    except Exception:  # noqa: BLE001 - a bad archive raises a dozen different types, and none of them is the caller's business
        raise TraceError("not a landmark trace") from None
    try:
        return _checked(data)
    except (KeyError, IndexError, TypeError, ValueError):
        raise TraceError("not a landmark trace") from None


def _checked(data: Mapping[str, np.ndarray]) -> TraceData:
    version = int(data["version"])
    if version not in (1, TRACE_VERSION):
        raise TraceError("not a landmark trace")
    press = str(data["press"]) if version >= 2 else "pinch"
    if press not in ("air", "pinch"):
        raise TraceError("not a landmark trace")
    t = np.asarray(data["t"], dtype=np.float64)
    n = len(t)
    present = np.asarray(data["present"], dtype=bool)
    side = np.asarray(data["side"], dtype=np.int8)
    score = np.asarray(data["score"], dtype=np.float32)
    lm = np.asarray(data["lm"], dtype=np.float32)
    targets = np.asarray(data["targets"], dtype=np.int16)
    if t.ndim != 1 or present.shape != (n, 2) or side.shape != (n, 2) or score.shape != (n, 2):
        raise TraceError("not a landmark trace")
    if lm.shape != (n, 2, 21, 3) or targets.shape != (n,):
        raise TraceError("not a landmark trace")
    segments: list[tuple[int, int, str]] = []
    for start, end, kind in np.asarray(data["segments"]).tolist():
        if not (isinstance(kind, str) and _KIND.fullmatch(kind) and 0 <= start <= end <= n):
            raise TraceError("not a landmark trace")
        segments.append((int(start), int(end), kind))
    return TraceData(
        t, present, side, score, lm, float(data["aspect"]), int(data["width"]), int(data["height"]),
        tuple(segments), targets, press, version,  # type: ignore[arg-type]
    )  # fmt: skip
