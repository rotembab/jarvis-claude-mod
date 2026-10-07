"""A hand tracker without a model: scripted hands, for tests and ``run --fake``.

``FakeTracker(script)`` ignores the pixels and returns the hands the script
says are in view, built with the kinematic model in ``jarvis_hands.synthetic``
(already mirrored, like the real tracker's output). The script is one of:

- ``None``: no hands, ever.
- A list of ``(t_offset, hands)`` steps. Offsets are seconds since the first
  ``process()`` call; each step holds until the next one, and the last one
  holds for good (before the first step's offset there are no hands). Steps
  are sorted by offset.
- A callable taking those same elapsed seconds and returning the hands, or a
  ``Frame`` (its hands are used; its time and size are replaced by the
  capture time and the image's size, so the engine's clock stays the
  camera's).

A hand is a ``HandObservation``, or a spec built with ``synthetic.hand``:
a mapping ``{"pose": "palm", "at": [x, y], "handedness": "right", "score":
0.95}`` (everything optional; the defaults are a right palm at (0.5, 0.45))
or a tuple ``(pose, at)`` / ``(pose, at, handedness)``. ``at`` is where the
anchor (the index and middle knuckles) sits, in normalized coordinates of
the mirrored frame.

Like MediaPipe, it reports at most ``num_hands`` hands: the first ones of the
step. So a scripted second hand only shows up once the runtime asks for two.

``load_script(path)`` reads the ``--fake-script`` file: JSON lines, one step
per line, blank lines ignored::

    {"t": 0, "hands": [{"pose": "palm", "at": [0.5, 0.45], "handedness": "right"}]}
    {"t": 1.0, "hands": [{"pose": "pinch", "at": [0.5, 0.45]}]}
    {"t": 1.3, "hands": []}
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from bisect import bisect_right
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from ..landmarks import Frame, HandObservation
from ..synthetic import POSES, hand

log = logging.getLogger(__name__)

HandSpec = HandObservation | Mapping[str, Any] | tuple[Any, ...]
Step = tuple[float, Sequence[HandSpec]]
ScriptFunction = Callable[[float], Frame | Sequence[HandSpec]]
Script = ScriptFunction | Sequence[Step] | None

_HAND_KEYS = frozenset({"pose", "at", "handedness", "score"})
#: Model times averaged for ``infer_ms``.
_TIMING_FRAMES = 90


class FakeTracker:
    def __init__(self, script: Script = None, *, num_hands: int = 1) -> None:
        self._function: ScriptFunction | None = None
        self._offsets: list[float] = []
        self._steps: list[Sequence[HandSpec]] = []
        if callable(script):
            self._function = script
        elif script is not None:
            for offset, hands in sorted(script, key=lambda step: float(step[0])):
                self._offsets.append(float(offset))
                self._steps.append(list(hands))
        self._lock = threading.Lock()
        self._num_hands = _check_num_hands(num_hands)
        #: Every value ``set_num_hands`` was given, in order (tests).
        self.num_hands_history: list[int] = []
        self.calls = 0
        self.closed = False
        self._t0: float | None = None
        self._times: deque[float] = deque(maxlen=_TIMING_FRAMES)

    @property
    def num_hands(self) -> int:
        return self._num_hands

    def set_num_hands(self, n: int) -> None:
        n = _check_num_hands(n)
        with self._lock:
            self.num_hands_history.append(n)
            self._num_hands = n

    @property
    def infer_ms(self) -> float:
        with self._lock:
            return sum(self._times) / len(self._times) if self._times else 0.0

    def process(self, image: np.ndarray, t: float) -> Frame:
        height, width = int(image.shape[0]), int(image.shape[1])
        if self.closed:
            return Frame(t, (), width, height)
        started = time.perf_counter()
        with self._lock:
            self.calls += 1
            if self._t0 is None:
                self._t0 = t
            elapsed = t - self._t0
            limit = self._num_hands
        hands = [build_hand(spec, (width, height)) for spec in self._hands_at(elapsed)][:limit]
        with self._lock:
            self._times.append((time.perf_counter() - started) * 1000.0)
        return Frame(t, tuple(hands), width, height)

    def close(self) -> None:
        self.closed = True

    def _hands_at(self, elapsed: float) -> Sequence[HandSpec]:
        if self._function is not None:
            result = self._function(elapsed)
            return result.hands if isinstance(result, Frame) else result
        i = bisect_right(self._offsets, elapsed) - 1
        return self._steps[i] if i >= 0 else ()


def build_hand(spec: HandSpec, size: tuple[int, int]) -> HandObservation:
    """A ``HandObservation`` from a script's hand spec, projected into a frame of ``size`` (width, height)."""
    if isinstance(spec, HandObservation):
        return spec
    if isinstance(spec, Mapping):
        fields = dict(spec)
    elif isinstance(spec, tuple | list) and 1 <= len(spec) <= 3:
        fields = dict(zip(("pose", "at", "handedness"), spec, strict=False))
    else:
        raise ValueError(f"a scripted hand is a mapping or a (pose, at[, handedness]) tuple, not {spec!r}")
    pose, at, handedness, score = _hand_fields(fields)
    return hand(pose, at, handedness=handedness, score=score, size=size)


def _hand_fields(fields: Mapping[str, Any]) -> tuple[Any, tuple[float, float], Any, float]:
    extra = sorted(set(fields) - _HAND_KEYS)
    if extra:
        raise ValueError(f"unknown hand field {extra[0]!r} (expected {', '.join(sorted(_HAND_KEYS))})")
    pose = fields.get("pose", "palm")
    if pose not in POSES:
        raise ValueError(f"unknown pose {pose!r}; expected one of {', '.join(POSES)}")
    at = fields.get("at", (0.5, 0.45))
    if not isinstance(at, list | tuple) or len(at) != 2 or not all(_is_number(v) and math.isfinite(v) for v in at):
        raise ValueError(f"'at' must be [x, y], got {at!r}")
    handedness = fields.get("handedness", "right")
    if handedness not in ("left", "right"):
        raise ValueError(f"handedness must be 'left' or 'right', got {handedness!r}")
    score = fields.get("score", 0.95)
    if not _is_number(score) or not 0.0 <= score <= 1.0:
        raise ValueError(f"score must be a number from 0 to 1, got {score!r}")
    return pose, (float(at[0]), float(at[1])), handedness, float(score)


def load_script(path: Path) -> list[Step]:
    """The steps of a ``--fake-script`` JSON-lines file. Raises ValueError (with the line number) or OSError."""
    steps: list[Step] = []
    text = Path(path).read_text(encoding="utf-8-sig")
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
            if not isinstance(item, dict):
                raise ValueError("each line must be a JSON object")
            extra = sorted(set(item) - {"t", "hands"})
            if extra:
                raise ValueError(f"unknown field {extra[0]!r} (expected 't' and 'hands')")
            t = item.get("t")
            if not _is_number(t) or not math.isfinite(t) or t < 0:
                raise ValueError(f"'t' must be a number of seconds >= 0, got {t!r}")
            hands = item.get("hands", [])
            if not isinstance(hands, list) or not all(isinstance(h, dict) for h in hands):
                raise ValueError("'hands' must be a list of objects")
            for h in hands:
                _hand_fields(h)
        except ValueError as exc:  # json.JSONDecodeError is a ValueError
            raise ValueError(f"{path}:{lineno}: {exc}") from exc
        steps.append((float(t), hands))
    return steps


def _check_num_hands(n: int) -> int:
    if isinstance(n, bool) or not isinstance(n, int) or n not in (1, 2):
        raise ValueError(f"num_hands must be 1 or 2, got {n!r}")
    return n


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)
