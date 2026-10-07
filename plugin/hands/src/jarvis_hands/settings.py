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
SCROLL_SPEED_RANGE = (0.1, 10.0)


@dataclass
class HandsSettings:
    engage: EngageMode = "palm"
    hand: HandChoice = "any"
    anchor: AnchorChoice = "knuckles"
    overlay: bool = True
    scroll_speed: float = 1.0
    displays: DisplaySelection = "all"

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
        if "scrollSpeed" in body:
            speed = body["scrollSpeed"]
            low, high = SCROLL_SPEED_RANGE
            if isinstance(speed, bool) or not isinstance(speed, int | float) or not low <= speed <= high:
                raise ValueError(f"scrollSpeed must be a number from {low} to {high}")
            changes["scroll_speed"] = float(speed)
        if "displays" in body:
            changes["displays"] = parse_displays(body["displays"])
        for name, value in changes.items():
            setattr(self, name, value)

    def status(self) -> dict[str, Any]:
        """The ``settings`` object of the protocol's StatusResponse."""
        return {
            "engage": self.engage,
            "hand": self.hand,
            "anchor": self.anchor,
            "overlay": self.overlay,
            "scrollSpeed": self.scroll_speed,
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
