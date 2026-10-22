"""The air tap's degradation ladder: ok, degraded, off (DESIGN-KEYBOARD.md 2.12.7, 3.7).

Pure: no clock (every time is an argument), no I/O, no numpy. It reads what the press method measures about the camera
(``PressQuality``) and answers a level; what each level does (a banner, a stricter detector, the fallback to pinch) is
the session's. ``off`` is terminal for the session: a new session re-tests the camera.
"""

from __future__ import annotations

from typing import Literal

from .limits import (
    AIR_LEVEL_FPS_DEGRADED,
    AIR_LEVEL_FPS_OFF,
    AIR_LEVEL_FPS_S,
    AIR_LEVEL_GAPS_DEGRADED,
    AIR_LEVEL_NOISE_DEGRADED,
    AIR_LEVEL_NOISE_OFF,
    AIR_LEVEL_NOISE_S,
    AIR_LEVEL_RECOVER_S,
)
from .types import PressQuality

Level = Literal["ok", "degraded", "off"]
Reason = Literal["", "fps", "noise", "both", "gaps"]

#: A degraded level ends only on readings clearly better than the thresholds that began it, so a camera that hovers at a
#: threshold does not make the banner flicker: fps at least 1.1 x, noise at most 0.9 x.
_RECOVER_FPS_FACTOR = 1.1
_RECOVER_NOISE_FACTOR = 0.9
#: Up to this many holes in 5 s still count as a good stream.
_RECOVER_GAPS = 1

#: Frame times are sums of floats; a condition that has held for "2.0 s" must not miss it by one rounding step.
_EPSILON = 1e-9

# The timers: name -> seconds a condition must have held. Holes use the frame-rate time (2.12.7 rule 4).
_HELD_S = {
    "fps_off": AIR_LEVEL_FPS_S,
    "noise_off": AIR_LEVEL_NOISE_S,
    "fps_deg": AIR_LEVEL_FPS_S,
    "noise_deg": AIR_LEVEL_NOISE_S,
    "gaps_deg": AIR_LEVEL_FPS_S,
}


class AirLadder:
    level: Level
    reason: Reason
    #: True while a noise condition holds: the session then tells the press method to be strict.
    strict: bool

    def __init__(self) -> None:
        self.level = "ok"
        self.reason = ""
        self.strict = False
        # condition -> the time it began to hold (absent: it does not hold)
        self._since: dict[str, float] = {}
        self._good_since: float | None = None
        # The first update without a hand; the stretch up to the next hand is not time the conditions held.
        self._absent_from: float | None = None

    def update(self, t: float, q: PressQuality, hands_present: bool) -> Level:
        """One reading per frame. ``off`` is terminal."""
        if self.level == "off":
            return "off"
        if not hands_present:
            # Rule 1: nothing changes and no timer runs. The first frame back moves every timer forward by the time
            # that was spent away, so the conditions resume exactly where they stopped.
            if self._absent_from is None:
                self._absent_from = t
            return self.level
        if self._absent_from is not None:
            away = t - self._absent_from
            self._absent_from = None
            self._since = {name: began + away for name, began in self._since.items()}
            if self._good_since is not None:
                self._good_since += away

        fps, noise, gaps = q.fps, q.noise, q.gaps
        conditions = {
            "fps_off": 0 < fps < AIR_LEVEL_FPS_OFF,
            "fps_deg": 0 < fps < AIR_LEVEL_FPS_DEGRADED,
            "noise_off": noise is not None and noise > AIR_LEVEL_NOISE_OFF,
            "noise_deg": noise is not None and noise > AIR_LEVEL_NOISE_DEGRADED,
            "gaps_deg": gaps >= AIR_LEVEL_GAPS_DEGRADED,
        }
        for name, holds in conditions.items():
            if holds:
                self._since.setdefault(name, t)
            else:
                self._since.pop(name, None)
        held = {name for name, began in self._since.items() if t - began >= _HELD_S[name] - _EPSILON}

        if "fps_off" in held or "noise_off" in held:
            self._set("off", held)
            return "off"
        if held & {"fps_deg", "noise_deg", "gaps_deg"}:
            self._set("degraded", held)
            return "degraded"
        if self.level == "degraded":
            self._recover(t, q)
        return self.level

    def _set(self, level: Level, held: set[str]) -> None:
        self.level = level
        fps = "fps_deg" in held
        noise = "noise_deg" in held
        if fps and noise:
            self.reason = "both"
        elif fps:
            self.reason = "fps"
        elif noise:
            self.reason = "noise"
        else:
            self.reason = "gaps"  # holes alone; with fps or noise the reason keeps theirs
        self.strict = self.reason in ("noise", "both")

    def _recover(self, t: float, q: PressQuality) -> None:
        # An unknown reading (fps 0.0, noise None) does not keep a level down: it is the absence of a bad one.
        fps_ok = q.fps == 0 or q.fps >= _RECOVER_FPS_FACTOR * AIR_LEVEL_FPS_DEGRADED
        noise_ok = q.noise is None or q.noise <= _RECOVER_NOISE_FACTOR * AIR_LEVEL_NOISE_DEGRADED
        if not (fps_ok and noise_ok and q.gaps <= _RECOVER_GAPS):
            self._good_since = None
            return
        if self._good_since is None:
            self._good_since = t
        if t - self._good_since >= AIR_LEVEL_RECOVER_S - _EPSILON:
            self.level = "ok"
            self.reason = ""
            self.strict = False
            self._good_since = None
