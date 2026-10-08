"""Live settings for hand control, and the calibration file.

``HandsSettings`` holds what the mod can change at run time (the protocol's
``config`` command, camelCase on the wire) next to the tuning values the
gesture engine and the mapper read every frame. The engine reads the object
each time instead of copying it, so a ``config`` applies on the next frame.

The calibration homography lives in ``<dataDir>/hands/calibration.json``. It
is written atomically (temp file + ``os.replace``) so a crash mid-write never
leaves a half file, and a missing or unreadable file simply means "use the
default box".
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import numpy as np

log = logging.getLogger(__name__)

EngageMode = Literal["palm", "always"]
HandChoice = Literal["right", "left", "any"]
AnchorChoice = Literal["knuckles", "index"]
DisplaySelection = Literal["all"] | tuple[int, ...]

CALIBRATION_VERSION = 1


@dataclass(frozen=True)
class Knob:
    """One sensitivity knob of the protocol's ``config`` command: its wire key, attribute, range and default."""

    key: str
    attr: str
    low: float
    high: float
    default: float

    def check(self, value: Any) -> float:
        """``value`` as a float, or ValueError naming the key and the range.

        A bool is not a number in JSON, NaN and the infinities are not numbers
        at all (they fail the range test), and an int is compared as it is, so
        one with hundreds of digits is refused instead of overflowing.
        """
        if isinstance(value, bool) or not isinstance(value, int | float) or not self.low <= value <= self.high:
            raise ValueError(f"{self.key} must be a number from {self.low:g} to {self.high:g}")
        return float(value)


#: The sensitivity knobs, in the order the status reports them. ``default`` is today's behaviour: each knob scales or
#: replaces a tuning value of ``HandsSettings`` (below), and its default leaves that value as it was.
#: The ranges are what stays usable, measured on the real model's test photos (with rotated, scaled, dimmed and noisy
#: variants) and on synthetic hands, so each end is a number and not a guess: below 0.85 an OK-sign pinch fires on
#: barely half of the variants, and above 1.15 the close threshold's margin to the closest hand that is not pinching
#: (woman_hands' relaxed palms, which rest the thumb against the index at 0.39 and 0.47 palm sizes) falls under 18%:
#: at 1.4 it is 0.5%, and with the noise of a live hand that palm reads as a pinch in every run from 1.35 and in one
#: run in nine at 1.3, which presses the button or keeps the hand from engaging (``pinch``); below 0.85 (0.9 keeps a
#: margin) the loosest real fist stops counting and above 1.1 a relaxed, jittering hand starts to grab (``fist``);
#: under 0.7 a click turns into a drag on a pinch that only drifts by landmark noise (``dragDistance``); under 0.7
#: the screen edges leave the camera frame (``cursorSpeed``); above 3 the cursor needs close to a second to settle after
#: a slow stop (``smoothing``); above 2.5 a hand that counts as still throws the window (``flingSensitivity``).
#: ``tests/test_poses_real.py`` pins the pinch and fist margins on the photos.
KNOBS: tuple[Knob, ...] = (
    #: Gain on the cursor around the centre of the target region: the hand travel that crosses the screen is divided
    #: by it.
    Knob("cursorSpeed", "cursor_speed", 0.7, 3.0, 1.0),
    #: Steadiness against lag: the One Euro filter's ``min_cutoff`` is divided by it.
    Knob("smoothing", "smoothing", 0.2, 3.0, 1.0),
    #: How close thumb and finger must be for a pinch: scales ``pinch_close`` (``pinch_open`` only follows it down).
    Knob("pinch", "pinch_sensitivity", 0.85, 1.15, 1.0),
    #: How tightly the fingers must curl for a fist: scales what counts as one (``poses.thresholds_for``).
    Knob("fist", "fist_sensitivity", 0.9, 1.1, 1.0),
    #: Seconds an open palm is held still to take the cursor.
    Knob("engageSeconds", "engage_s", 0.1, 2.0, 0.5),
    #: Multiplier on ``slop``: how far the hand may drift during a pinch before it becomes a drag.
    Knob("dragDistance", "drag_distance", 0.7, 4.0, 1.0),
    #: How light a flick throws a grabbed window: ``throw_speed`` is divided by it.
    Knob("flingSensitivity", "fling_sensitivity", 0.5, 2.5, 1.0),
    #: Multiplier on the scroll distance per hand movement.
    Knob("scrollSpeed", "scroll_speed", 0.1, 10.0, 1.0),
    #: Pixels the filtered cursor must move before the cursor follows.
    Knob("deadZone", "dead_zone_px", 0.0, 8.0, 1.0),
)
KNOB_BY_KEY: dict[str, Knob] = {knob.key: knob for knob in KNOBS}


@dataclass
class HandsSettings:
    engage: EngageMode = "palm"
    hand: HandChoice = "any"
    anchor: AnchorChoice = "knuckles"
    overlay: bool = True
    scroll_speed: float = 1.0
    displays: DisplaySelection = "all"

    # The sensitivity knobs (``KNOBS``): the mod sends them, ``status()`` reports them, and the engine and the
    # mapper read them every frame (where a change would move something that is held, they wait; see the engine).
    #: Gain on the cursor around the centre of the target region (higher: less hand travel crosses the screen).
    cursor_speed: float = 1.0
    #: Divides the cursor filter's ``min_cutoff`` (higher: steadier at rest, more lag).
    smoothing: float = 1.0
    #: Scales ``PoseThresholds.pinch_close``, and ``pinch_open`` below 1.0 (higher: a looser pinch counts).
    pinch_sensitivity: float = 1.0
    #: Scales what counts as a fist (higher: a looser fist counts); below 1.0 it asks more of the fist and nothing else.
    fist_sensitivity: float = 1.0
    #: Multiplies ``slop`` (higher: the hand may drift further during a pinch before it is a drag).
    drag_distance: float = 1.0
    #: Divides ``throw_speed`` (higher: a lighter flick throws the window).
    fling_sensitivity: float = 1.0

    # Tuning (SPEC-hands.md); not part of the protocol.
    #: Frames a raw pose must hold before the engine acts on it (entering and leaving).
    confirm_frames: int = 2
    #: Seconds a still open palm must be held to engage.
    engage_s: float = 0.5
    #: Frame widths per second; a palm moving faster does not count as still.
    engage_max_speed: float = 0.3
    #: Seconds a lost pointer hand keeps everything as it is (a dropped frame must not end a drag).
    hold_s: float = 0.25
    #: Seconds without the pointer hand before the engine disengages.
    lost_s: float = 1.5
    #: Frame widths the anchor may move during a press before it becomes a drag.
    slop: float = 0.015
    #: A press lands where the cursor was this long before the pinch began.
    rewind_s: float = 0.07
    #: Display widths per second a released grab must move at to throw the window.
    throw_speed: float = 1.5
    #: Seconds of cursor history the throw speed is measured over.
    throw_window_s: float = 0.12
    # One Euro filter on the cursor, desktop pixels (Casiez et al. 2012).
    min_cutoff: float = 1.0
    beta: float = 0.004
    d_cutoff: float = 1.0
    dead_zone_px: float = 1.0
    #: The default camera box mapped onto the target region: x0, y0, x1, y1 of the mirrored frame.
    box: tuple[float, float, float, float] = (0.2, 0.2, 0.8, 0.7)

    def apply_config(self, body: dict[str, Any]) -> None:
        """Apply a protocol ``config`` body. Absent keys keep their value; unknown keys are ignored.

        Raises ValueError (and changes nothing) when a value is invalid.
        """
        changes: dict[str, Any] = {}
        if "engage" in body:
            changes["engage"] = _choice(body["engage"], ("palm", "always"), "engage")
        if "hand" in body:
            changes["hand"] = _choice(body["hand"], ("right", "left", "any"), "hand")
        if "anchor" in body:
            changes["anchor"] = _choice(body["anchor"], ("knuckles", "index"), "anchor")
        if "overlay" in body:
            if not isinstance(body["overlay"], bool):
                raise ValueError("overlay must be true or false")
            changes["overlay"] = body["overlay"]
        for knob in KNOBS:
            if knob.key in body:
                changes[knob.attr] = knob.check(body[knob.key])
        if "displays" in body:
            changes["displays"] = parse_displays(body["displays"])
        # Everything was checked above: the engine thread never sees a half-valid command.
        for name, value in changes.items():
            setattr(self, name, value)

    def status(self) -> dict[str, Any]:
        """The ``settings`` object of the protocol's StatusResponse."""
        return {
            "engage": self.engage,
            "hand": self.hand,
            "anchor": self.anchor,
            "overlay": self.overlay,
            **{knob.key: getattr(self, knob.attr) for knob in KNOBS},
        }


def parse_displays(value: Any) -> DisplaySelection:
    """``"all"`` or a list of 1-based display ids, as the protocol sends it."""
    if value == "all":
        return "all"
    if isinstance(value, list | tuple) and all(isinstance(v, int) and not isinstance(v, bool) for v in value):
        if any(v < 1 for v in value):
            raise ValueError("display ids start at 1")
        if not value:
            return "all"
        return tuple(dict.fromkeys(value))
    raise ValueError('displays must be "all" or a list of display ids')


def _choice(value: Any, allowed: tuple[str, ...], name: str) -> Any:
    if value not in allowed:
        raise ValueError(f"{name} must be one of {', '.join(allowed)}")
    return value


def calibration_path(data_dir: Path) -> Path:
    return Path(data_dir) / "hands" / "calibration.json"


def load_calibration(data_dir: Path) -> np.ndarray | None:
    """The saved camera -> target homography, or None when there is none (or it is unreadable)."""
    path = calibration_path(data_dir)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        log.warning("cannot read %s (%s); using the default box", path, exc)
        return None
    try:
        # utf-8-sig: a file re-saved by Notepad as "UTF-8 with BOM" is still ours.
        data = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(data, dict) or data.get("version") != CALIBRATION_VERSION:
            raise ValueError(f"unknown calibration version {data.get('version') if isinstance(data, dict) else None}")
        return _homography(data.get("homography"))
    except (ValueError, TypeError, ArithmeticError, RecursionError) as exc:
        # UnicodeDecodeError is a ValueError; a number too big for a float is an OverflowError.
        log.warning("ignoring corrupt calibration %s (%s); using the default box", path, exc)
        return None


def save_calibration(data_dir: Path, homography: np.ndarray) -> Path:
    """Write the homography atomically and return the file's path."""
    matrix = _homography(np.asarray(homography, dtype=float).tolist())
    path = calibration_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"version": CALIBRATION_VERSION, "homography": matrix.tolist()}, indent=2)
    fd, tmp = tempfile.mkstemp(prefix=".calibration-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload + "\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def _homography(value: Any) -> np.ndarray:
    """A finite, invertible 3x3 matrix from nested lists, or ValueError."""
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError("homography must be a 3x3 list")
    rows: list[list[float]] = []
    for row in value:
        if not isinstance(row, list) or len(row) != 3:
            raise ValueError("homography must be a 3x3 list")
        if not all(isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v) for v in row):
            raise ValueError("homography must hold finite numbers")
        rows.append([float(v) for v in row])
    matrix = np.array(rows, dtype=float)
    if abs(np.linalg.det(matrix)) < 1e-12:
        raise ValueError("homography is singular")
    return matrix
