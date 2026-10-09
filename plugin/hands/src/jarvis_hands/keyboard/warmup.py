"""The warm-up that decides each finger's threshold and arms the keyboard (DESIGN-KEYBOARD.md 2.5, 2.12.5, 3.7).

Two variants behind one class. ``pinch`` watches the ratios: a deliberate close-and-open cycle of each finger records
its minimum ratio, and the per-finger thresholds follow from it (2.5). ``air`` is prompted: the strip names one finger
at a time and only a clean, firm, on-the-key tap of that finger counts (2.12.5), because with the first version's
"any valid tap of any finger" rule a resting or fidgeting hand armed the session in most stress runs.

Pure: no clock (every time is an argument or a sample's own ``t``), no I/O. Arming is friction against accidents, not
proof of intent: the boundary is the three-tap Insert (SR2, SR21).
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .limits import (
    AIR_WARMUP_AIM_TOL,
    AIR_WARMUP_CLEAN_S,
    AIR_WARMUP_GAP_S,
    AIR_WARMUP_MIN_DEPTH,
    AIR_WARMUP_MIN_MARGIN,
    AIR_WARMUP_ORDER,
    AIR_WARMUP_STRAY_LIMIT,
)
from .plane import Plane
from .tuning import RANGES, Tuning
from .types import HandSample, PressEvent, PressName, Side

#: 2.5. A warm-up pinch is a cycle: the ratio falls below FALL from at least ARM and rises back above it within
#: RISE_S.
PINCH_ARM = 0.60
PINCH_FALL = 0.50
PINCH_RISE_S = 3.0
#: It is valid when the minimum is below RMIN, the finger leads the other three by MARGIN, none of them is below OTHER,
#: fewer than CURLED fingers are curled and the hand never moves faster than SPEED (frame widths a second).
PINCH_RMIN = 0.40
PINCH_MARGIN = 0.08
PINCH_OTHER = 0.45
PINCH_CURLED = 3
PINCH_SPEED = 0.5
#: ``open_f = max(tuning.open, close_f + PINCH_OPEN_GAP)``.
PINCH_OPEN_GAP = 0.12
#: A hand that was not seen for longer than this has its cycles dropped: a stalled camera must not finish a pinch.
PINCH_GAP_S = 0.25

#: The outcomes ``update`` returns for the air variant, one per event.
ACCEPTED = "accepted"
EARLY = "early"
STRAY = "stray"
RESTART = "restart"
WEAK = "weak"
OFF_KEY = "off_key"
UNCLEAN = "unclean"

Key = tuple[Side, int]


@dataclass
class _Cycle:
    """One finger of one hand, pinch variant."""

    #: The ratio has been at least PINCH_ARM since the last cycle: the next fall starts one.
    armed: bool = False
    #: When the ratio fell below PINCH_FALL; None outside a cycle.
    start: float | None = None
    r_min: float = math.inf
    #: The conditions that are judged at the frame of the minimum.
    valid_at_min: bool = False
    top_speed: float = 0.0


class _Track:
    """One hand, pinch variant."""

    def __init__(self, side: Side) -> None:
        self.side = side
        self.last_t = -math.inf
        self.cycles = [_Cycle() for _ in range(4)]


class Warmup:
    #: Air: the finger the strip names now; None during the wait between fingers, when complete, and for pinch.
    prompt: tuple[Side, int] | None
    #: Air: strays since the sequence began or last restarted.
    strays: int

    def __init__(
        self,
        tuning: Tuning,
        method: PressName = "pinch",
        *,
        home_f: Mapping[tuple[Side, int], tuple[float, float]] | None = None,
    ) -> None:
        if method not in ("pinch", "air"):
            raise ValueError("the warm-up knows the pinch and the air tap")
        if method == "air" and not home_f:
            raise ValueError("the air warm-up needs the home position of each finger")
        self._tuning = tuning
        self._method = method
        self._home_f: dict[Key, tuple[float, float]] = dict(home_f) if home_f else {}
        # The required set: the fingers of the hands that were still at placement. Without ``home_f`` (a pinch warm-up
        # built after a ladder fallback by a caller that has none) it is fixed by the first frame that shows a hand.
        self._required: frozenset[Key] = frozenset(self._home_f)
        self._fixed = bool(self._home_f)
        self.prompt = None
        self.strays = 0
        # pinch
        self._tracks: dict[int, _Track] = {}
        self._rmin: dict[Key, float] = {}
        # air
        self._order = [key for key in AIR_WARMUP_ORDER if key in self._required]
        self._done: set[Key] = set()
        self._depth: dict[Key, float] = {}
        self._ready_at: float | None = None
        self._rejects: deque[tuple[float, int]] = deque()

    # ------------------------------------------------------------------------------------------------------ state

    @property
    def required(self) -> frozenset[tuple[Side, int]]:
        return self._required

    @property
    def done(self) -> frozenset[tuple[Side, int]]:
        if self._method == "air":
            return frozenset(self._done)
        return frozenset(key for key in self._required if key in self._rmin)

    @property
    def complete(self) -> bool:
        return bool(self._required) and self.done == self._required

    @property
    def depth(self) -> dict[tuple[Side, int], float]:
        """Air: D_f per finger, the depth of its accepted tap. A copy; empty for pinch."""
        return dict(self._depth)

    def thresholds(self) -> dict[tuple[Side, int], tuple[float, float]]:
        """Pinch: ``(close, open)`` per required finger, from its record or the defaults. Air: ``(D_f, D_f)`` per
        finger with an accepted tap, which the session hands to ``press.set_finger`` unchanged."""
        if self._method == "air":
            return {key: (d, d) for key, d in self._depth.items()}
        low, high = RANGES["close"]
        out: dict[Key, tuple[float, float]] = {}
        for key in sorted(self._required):
            r_min = self._rmin.get(key)
            if r_min is None:
                out[key] = (self._tuning.close, self._tuning.open)
                continue
            close = min(max(self._tuning.warm_factor * r_min, low), high)
            out[key] = (close, max(self._tuning.open, close + PINCH_OPEN_GAP))
        return out

    def update(
        self,
        hands: Sequence[HandSample],
        events: Sequence[PressEvent] = (),
        *,
        t: float = 0.0,
        plane: Plane | None = None,
        rejects: int = 0,
    ) -> tuple[str, ...]:
        """Pinch: as 2.5, the other arguments are ignored and () is returned. Air: one outcome per event (2.12.5)."""
        if self._method == "pinch":
            self._update_pinch(hands)
            return ()
        return self._update_air(events, t, plane, rejects)

    # ------------------------------------------------------------------------------------------------------ pinch

    def _update_pinch(self, hands: Sequence[HandSample]) -> None:
        # A hand records under the side it was first seen with: a label that flips later must not move its records.
        # Two hands that carry the same label take one side each; a third hand records nothing.
        taken: set[Side] = {self._tracks[h.hand].side for h in hands if h.hand in self._tracks}
        seen: list[tuple[_Track, HandSample]] = []
        for hand in hands:
            track = self._tracks.get(hand.hand)
            if track is None:
                free = [s for s in ("left", "right") if s not in taken]
                side = hand.side if hand.side in free else (free[0] if free else None)
                if side is None:
                    continue
                taken.add(side)
                track = self._tracks[hand.hand] = _Track(side)
            if hand.t - track.last_t > PINCH_GAP_S:
                track.cycles = [_Cycle() for _ in range(4)]
            track.last_t = hand.t
            seen.append((track, hand))
        if not self._fixed and seen:
            # Without a ``home_f`` the hands of the first frame that shows any are the hands that were placed.
            self._required = frozenset((track.side, f) for track, _ in seen for f in range(4))
            self._fixed = True
        for track, hand in seen:
            for finger in range(4):
                self._step_cycle(track, hand, finger)
        live = {hand.hand for _, hand in seen}
        for hid in [h for h in self._tracks if h not in live]:
            del self._tracks[hid]  # a hand that left forgets its cycles; its records stay with (side, finger)

    def _step_cycle(self, track: _Track, hand: HandSample, finger: int) -> None:
        cycle = track.cycles[finger]
        ratio = hand.fingers[finger].ratio
        if cycle.start is None:
            if ratio >= PINCH_ARM:
                cycle.armed = True
            elif ratio < PINCH_FALL and cycle.armed:
                cycle.armed = False
                cycle.start = hand.t
                cycle.r_min = math.inf
                cycle.valid_at_min = False
                cycle.top_speed = 0.0
        if cycle.start is None:
            return
        cycle.top_speed = max(cycle.top_speed, hand.speed)
        if ratio < cycle.r_min:
            cycle.r_min = ratio
            cycle.valid_at_min = self._valid_at_min(hand, finger)
        if ratio > PINCH_FALL:
            if (
                hand.t - cycle.start <= PINCH_RISE_S  # too slow is not a deliberate pinch
                and cycle.valid_at_min
                and cycle.r_min < PINCH_RMIN
                and cycle.top_speed < PINCH_SPEED
            ):
                self._rmin[(track.side, finger)] = cycle.r_min  # a second valid cycle replaces the first
            cycle.start = None  # either way the finger must open fully (to ARM) before it can count again

    @staticmethod
    def _valid_at_min(hand: HandSample, finger: int) -> bool:
        """The conditions of 2.5 that are judged at the frame of the minimum."""
        mine = hand.fingers[finger].ratio
        others = [f.ratio for i, f in enumerate(hand.fingers) if i != finger]
        if min(others) - mine < PINCH_MARGIN:
            return False  # not clearly the smallest of the four
        if min(others) < PINCH_OTHER:
            return False  # another finger is nearly as closed
        return sum(1 for f in hand.fingers if f.curled) < PINCH_CURLED

    # ------------------------------------------------------------------------------------------------------- air

    def _update_air(self, events: Sequence[PressEvent], t: float, plane: Plane | None, rejects: int) -> tuple[str, ...]:
        if self._ready_at is None:
            self._ready_at = t + AIR_WARMUP_GAP_S
        self._note_rejects(t, rejects)
        shown = not self.complete and t >= self._ready_at
        out: list[str] = []
        for event in events:
            if not shown:
                out.append(EARLY)
                continue
            outcome = self._judge(event, t, plane, rejects)
            out.append(outcome)
            if outcome in (ACCEPTED, RESTART):
                shown = False  # the wait for the next finger starts now, for the rest of this frame too
        self.prompt = self._order[len(self._done)] if shown else None
        return tuple(out)

    def _note_rejects(self, t: float, total: int) -> None:
        log = self._rejects
        log.append((t, total))
        # Keep one reading at or before the start of the window: it is the baseline the clean-window check compares to.
        while len(log) > 1 and log[1][0] <= t - AIR_WARMUP_CLEAN_S:
            log.popleft()

    def _clean(self, rejects: int) -> bool:
        """True when the reject total did not rise in the last ``AIR_WARMUP_CLEAN_S`` seconds: ``_note_rejects`` has
        left the newest reading at or before the start of that window in front, and the total is compared to it."""
        return rejects <= self._rejects[0][1]

    def _judge(self, event: PressEvent, t: float, plane: Plane | None, rejects: int) -> str:
        named = self._order[len(self._done)]
        key = (event.side, event.finger)
        if key != named:
            self.strays += 1
            if self.strays >= AIR_WARMUP_STRAY_LIMIT:
                self._restart(t)
                return RESTART
            return STRAY
        if event.margin < AIR_WARMUP_MIN_MARGIN or event.depth < AIR_WARMUP_MIN_DEPTH:
            return WEAK
        if plane is None or not self._on_key(event, plane, key):
            return OFF_KEY
        if not self._clean(rejects):
            return UNCLEAN
        self._done.add(key)
        self._depth[key] = event.depth
        self._ready_at = t + AIR_WARMUP_GAP_S
        return ACCEPTED

    def _on_key(self, event: PressEvent, plane: Plane, key: Key) -> bool:
        u, v = plane.units(event.aim)
        home_u, home_v = self._home_f[key]
        return abs(u - home_u) <= AIR_WARMUP_AIM_TOL and abs(v - home_v) <= AIR_WARMUP_AIM_TOL

    def _restart(self, t: float) -> None:
        self._done.clear()
        self._depth.clear()
        self.strays = 0
        self._ready_at = t + AIR_WARMUP_GAP_S
