"""Practice: the phrases, the drill, the scoring and the markers that gate a first live session.

DESIGN-KEYBOARD.md 5.7, 2.12.6 and 4.4. A practice is the normal session with nothing sent anywhere: placing, the
warm-up, then a script. ``PracticeScript`` is the script (pure: every time is an argument, the session drives it with
the frame time); ``PracticeResult`` is what it measured; the markers at the end of the file are what a completed
practice leaves for the next live open, and what that open reads back.

The script scores at two moments, on purpose. What the *detector* did is decided at the event, before the session's
queue can drop or delay anything: a phantom in a rest, a drill tap of the wrong finger, the recall of the prompted
finger. What the *typing* did is decided when the queue delivers the key, because that is the moment a live session
would have typed it: the phrases.
"""

from __future__ import annotations

import json
import math
import os
import random
import statistics
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, Literal

from .ladder import Level
from .layout import Key, Layout
from .limits import (
    AIR_DRILL_GAP_S,
    AIR_DRILL_MOVE_EVERY,
    AIR_DRILL_MOVE_ROWS,
    AIR_DRILL_MOVE_U,
    AIR_DRILL_PER_FINGER,
    AIR_DRILL_PER_REACH_KEY,
    AIR_DRILL_REACH_KEYS,
    AIR_PRACTICE_MAX_PHANTOMS_PER_MIN,
    AIR_PRACTICE_MIN_DRILL_PROMPTS,
    AIR_PRACTICE_MIN_DRILL_RECALL,
    AIR_PRACTICE_MIN_REST_S,
    AIR_REST_S,
    AIR_TALK_S,
    GAP_RESET_S,
    PRACTICE_MAX_PHANTOMS_PER_MIN,
    PRACTICE_MIN_REST_S,
)
from .types import Lang, PressName, Side

# ----------------------------------------------------------------------------------------------------- the phrases

#: Two groups of three, a rest after each (5.7). Hebrew is used with ``layout: he``.
PHRASES: Final[Mapping[Lang, tuple[str, ...]]] = MappingProxyType(
    {
        "en": (
            "the quick brown fox",
            "jumps over the lazy dog",
            "hello world",
            "yes please",
            "go ahead, thanks.",
            "what is this?",
        ),
        "he": ("שלום עולם", "תודה רבה", "כן בבקשה", "מה נשמע", "בוקר טוב", "לילה טוב"),
    }
)
PHRASES_PER_GROUP: Final = 3
#: A character not typed within this long counts as a miss and the script moves on.
CHAR_TIMEOUT_S: Final = 8.0
#: The air practice opens with this much of the start message, before the drill; the one-hand hint is shown as long.
INTRO_S: Final = 4.0
HINT_S: Final = 4.0

FINGER_NAMES: Final = ("index", "middle", "ring", "pinky")
#: The home key under each finger (a s d f, j k l '): the key the drill lights when it names the finger.
HOME_CHARS: Final[Mapping[Side, str]] = MappingProxyType({"left": "fdsa", "right": "jkl'"})
#: What the strip calls the reach keys (Appendix F.1; the Enter key reads Send in the review layout).
REACH_LEGENDS: Final = MappingProxyType({"backspace": "Bksp", "insert": "Insert", "clear": "Clear", "enter": "Send"})

#: The fixed strings (Appendix F.1). ``{side}``, ``{finger}`` and ``{key}`` come from the two tables above, ``{n}`` and
#: ``{total}`` are numbers.
PRACTICE_TEXT: Final = MappingProxyType(
    {
        "drill": "Tap: {side} {finger}",
        "reach": "Tap: {side} {finger}, {key}",
        "rest_air": "Rest: do not tap. Wave, open and close your hands.",
        "rest_pinch": "Rest: do not press. Wave, open and close your hands.",
        "talk": "Talk to the camera as on a call. Keep your hands moving. Do not tap.",
        "start_air": "Practice checks your camera and your taps. Nothing is typed anywhere.",
        "one_hand": "One hand: move to the key, stop, then tap.",
        "not_usable": "Air tap is not usable on this camera. Use the pinch method.",
        "phrase": "Phrase {n}/{total}",
        "bad_phrase": "a practice phrase uses a character the keyboard does not have",
    }
)

#: The trace segment kind of each script segment (5.7: ``place``, ``warm``, ``phrase``, ``rest``, ``type``, ``tap``,
#: ``drill``; the talk segment is new). The intro belongs to the warm-up as far as a recording is concerned.
_TRACE_KIND: Final = MappingProxyType(
    {"intro": "warm", "drill": "drill", "phrase": "phrase", "rest": "rest", "talk": "talk", "done": "rest"}
)


def finger_label(side: Side, finger: int) -> str:
    """``"left.ring"``: the key of ``PracticeResult.per_finger``."""
    return f"{side}.{FINGER_NAMES[finger]}"


# ------------------------------------------------------------------------------------------------- the result record


@dataclass(frozen=True)
class PracticeResult:
    #: Six phrases finished or timed out, both rests done.
    completed: bool
    presses: int
    correct: int
    hit_rate: float
    #: Seconds of REST with a hand in view.
    rest_s: float
    phantoms: int
    phantoms_per_min: float
    #: Median frame rate over the session.
    fps: float
    #: "left.ring" -> (presses, correct); no key identity.
    per_finger: dict[str, tuple[int, int]]
    # Air only (2.12.6): reported, never gated unless the marker rules say so. Everything below defaults to 0.
    talk_s: float = 0.0
    talk_phantoms: int = 0
    drill_prompts: int = 0
    drill_hits: int = 0
    #: Index and middle fingers only.
    drill_prompts_im: int = 0
    drill_hits_im: int = 0
    drill_prompts_reach: int = 0
    drill_hits_reach: int = 0
    #: Standard deviation of ``aim - target centre`` in key units, kept for the decoder track.
    aim_sd_u: float = 0.0
    aim_sd_v: float = 0.0
    #: Median depth noise.
    noise: float = 0.0
    #: The ladder level at the end.
    level: Level = "ok"


# ------------------------------------------------------------------------------------------------------- the drill


@dataclass(frozen=True)
class DrillPrompt:
    side: Side
    finger: int
    #: The key to tap: the home key, the displaced letter key, or the reach key.
    key: int
    #: One of the four reach keys of 2.12.6; counted apart from the home-row prompts.
    reach: bool
    #: Every second home-row prompt.
    displaced: bool
    #: The strip line.
    text: str


def _key_centre(key: Key) -> tuple[float, float]:
    return key.col + key.width / 2, key.row + 0.5


def _displaced(layout: Layout, side: Side, finger: int, rng: random.Random) -> Key:
    """A letter key 3.0 to 4.0 units left or right of the finger's home key and a row up, level or down.

    A draw that falls outside the keyboard or on anything but a letter is made again (2.12.6).
    """
    home = layout.find(char=HOME_CHARS[side][finger])
    hu, hv = _key_centre(home)
    for _ in range(500):
        du = rng.uniform(*AIR_DRILL_MOVE_U) * rng.choice((-1, 1))
        dv = float(rng.choice(AIR_DRILL_MOVE_ROWS))
        key = layout.key_at(hu + du, hv + dv, tol=0.0)
        if key is not None and key.kind == "char" and key.en.isalpha():
            return key
    return home  # unreachable with the shipped tables; the prompt is then the home key itself


def _no_repeat_order(
    items: list[tuple[Side, int, str | None]], rng: random.Random
) -> list[tuple[Side, int, str | None]]:
    """A random order in which no finger comes twice in a row; a dead end starts again."""
    for _ in range(200):
        pool = list(items)
        order: list[tuple[Side, int, str | None]] = []
        while pool:
            last = (order[-1][0], order[-1][1]) if order else None
            options = [i for i, item in enumerate(pool) if (item[0], item[1]) != last]
            if not options:
                break
            order.append(pool.pop(rng.choice(options)))
        if not pool:
            return order
    # A set this small and this even cannot dead-end 200 times; keep the contract anyway with a plain shuffle.
    rng.shuffle(items)
    return items


def make_drill(layout: Layout, sides: Sequence[Side], rng: random.Random) -> list[DrillPrompt]:
    """The drill of 2.12.6: ``AIR_DRILL_PER_FINGER`` prompts per finger of the hands in use plus the reach prompts.

    The same ``rng`` state gives the same list.
    """
    used = [s for s in ("left", "right") if s in sides]
    items: list[tuple[Side, int, str | None]] = [
        (side, finger, None) for side in used for finger in range(4) for _ in range(AIR_DRILL_PER_FINGER)
    ]
    for kind, side, name in AIR_DRILL_REACH_KEYS:
        if side in used:
            items += [(side, FINGER_NAMES.index(name), kind)] * AIR_DRILL_PER_REACH_KEY
    prompts: list[DrillPrompt] = []
    home_n = 0
    for side, finger, reach in _no_repeat_order(items, rng):
        if reach is not None:
            key = layout.find(kind=reach)  # type: ignore[arg-type]
            text = PRACTICE_TEXT["reach"].format(side=side, finger=FINGER_NAMES[finger], key=REACH_LEGENDS[reach])
            prompts.append(DrillPrompt(side, finger, key.index, True, False, text))
            continue
        home_n += 1
        move = home_n % AIR_DRILL_MOVE_EVERY == 0
        key = _displaced(layout, side, finger, rng) if move else layout.find(char=HOME_CHARS[side][finger])
        text = PRACTICE_TEXT["drill"].format(side=side, finger=FINGER_NAMES[finger])
        prompts.append(DrillPrompt(side, finger, key.index, False, move, text))
    return prompts


# ------------------------------------------------------------------------------------------------------ the script


@dataclass
class _Step:
    kind: Literal["intro", "drill", "phrase", "rest", "talk"]
    #: intro, rest, talk: how long (the last two in seconds with a hand in view).
    seconds: float = 0.0
    #: phrase: the phrases ``first`` .. ``last - 1``.
    first: int = 0
    last: int = 0


class PracticeScript:
    """One practice from the first typing frame to the last rest (or talk).

    ``tick`` once per frame; ``event`` for every press the method emitted and ``press`` for every key the session's
    queue delivered. Nothing here knows about hands, planes or holds: the session passes keys as indices and aims as key
    units.
    """

    def __init__(
        self,
        *,
        press: PressName,
        lang: Lang,
        layout: Layout,
        sides: Sequence[Side],
        phrases: Sequence[str] | None = None,
        seed: int = 0,
    ) -> None:
        if press not in ("air", "pinch"):
            raise ValueError("practice needs the air or the pinch method")
        self._air = press == "air"
        self._lang = lang
        self._layout = layout
        self._sides = tuple(s for s in ("left", "right") if s in sides)
        self._texts = tuple(phrases) if phrases is not None else PHRASES[lang]
        # The keys of every phrase, up front: a character the layout lacks is the caller's mistake, found at once.
        self._keys: list[list[int]] = []
        for text in self._texts:
            try:
                self._keys.append([layout.find(char=c, lang=lang).index for c in text])
            except KeyError:
                raise ValueError(PRACTICE_TEXT["bad_phrase"]) from None
        self._drill = make_drill(layout, self._sides, random.Random(seed)) if self._air else []
        self._steps = self._plan()
        self.counts: Counter[str] = Counter()
        # the clock
        self._k = 0
        self._t0: float | None = None
        self._last_t: float | None = None
        self._in_view_s = 0.0
        # totals
        self._rest_s = 0.0
        self._talk_s = 0.0
        self._rest_phantoms = 0
        self._talk_phantoms = 0
        # phrases
        self._phrase = 0
        self._char = 0
        self._since = 0.0
        self._echo = ""
        self._hint_until = -math.inf
        self._presses = 0
        self._correct = 0
        self._by_finger: dict[str, list[int]] = {}
        # drill
        self._issued = 0
        self._hit = [False] * len(self._drill)
        self._aim: list[tuple[float, float]] = []

    # ------------------------------------------------------------------------------------------------ the plan

    def _plan(self) -> list[_Step]:
        groups = [
            (first, min(first + PHRASES_PER_GROUP, len(self._texts)))
            for first in range(0, len(self._texts), PHRASES_PER_GROUP)
        ]
        steps: list[_Step] = []
        if self._air:
            steps += [_Step("intro", INTRO_S), _Step("drill")]
        rest = AIR_REST_S if self._air else PRACTICE_MIN_REST_S
        for first, last in groups:
            steps += [_Step("phrase", first=first, last=last), _Step("rest", rest)]
        if self._air:
            steps.append(_Step("talk", AIR_TALK_S))
        return steps

    # ------------------------------------------------------------------------------------------------ the state

    @property
    def done(self) -> bool:
        return self._k >= len(self._steps)

    @property
    def segment(self) -> str:
        """``intro``, ``drill``, ``phrase``, ``rest``, ``talk`` or ``done``."""
        return "done" if self.done else self._steps[self._k].kind

    @property
    def trace_kind(self) -> str:
        return _TRACE_KIND[self.segment]

    @property
    def phrase_index(self) -> int:
        """The phrase being typed (the number of phrases finished while a rest follows them)."""
        return self._phrase

    @property
    def echo(self) -> str:
        """The characters of the current phrase typed so far."""
        return self._echo if self.segment == "phrase" else ""

    @property
    def progress(self) -> float:
        """0..1 through a rest or the talk, counted in seconds with a hand in view."""
        step = self._steps[self._k] if not self.done else None
        if step is None or step.kind not in ("rest", "talk") or step.seconds <= 0:
            return 0.0
        return min(self._in_view_s / step.seconds, 1.0)

    def _drill_index(self, t: float) -> int:
        if self._t0 is None:
            return -1
        return int((t - self._t0) / AIR_DRILL_GAP_S + 1e-9)

    @property
    def _prompt(self) -> DrillPrompt | None:
        if self.segment != "drill" or self._issued == 0:
            return None
        return self._drill[self._issued - 1]

    @property
    def target_key(self) -> int | None:
        """The key lit as the target: the next character of the phrase or the key of the drill prompt."""
        if self.segment == "phrase":
            return self._keys[self._phrase][self._char]
        prompt = self._prompt
        return prompt.key if prompt is not None else None

    @property
    def named(self) -> tuple[Side, int] | None:
        """The finger the drill names now (its ring pulses)."""
        prompt = self._prompt
        return (prompt.side, prompt.finger) if prompt is not None else None

    @property
    def prompt(self) -> str:
        """The prompt line: the phrase, the drill prompt or the rest and talk instruction."""
        kind = self.segment
        if kind == "phrase":
            return self._texts[self._phrase]
        if kind == "drill":
            prompt = self._prompt
            return prompt.text if prompt is not None else ""
        if kind == "rest":
            return PRACTICE_TEXT["rest_air" if self._air else "rest_pinch"]
        if kind == "talk":
            return PRACTICE_TEXT["talk"]
        return ""

    def strip(self, t: float) -> str:
        """The status line now: the prompt, or for a moment the start message or the one-hand hint."""
        kind = self.segment
        if kind == "intro":
            return PRACTICE_TEXT["start_air"]
        if kind == "phrase":
            if t < self._hint_until:
                return PRACTICE_TEXT["one_hand"]
            return PRACTICE_TEXT["phrase"].format(n=self._phrase + 1, total=len(self._texts))
        return self.prompt

    # ------------------------------------------------------------------------------------------------- the clock

    def tick(self, t: float, in_view: bool) -> None:
        """One frame at time ``t``; ``in_view`` is whether any hand was seen in it."""
        if self._last_t is None:
            self._last_t = t
            self._begin(t)
        dt = min(max(t - self._last_t, 0.0), GAP_RESET_S)  # a stalled camera is not time with a hand in view
        self._last_t = t
        if self.done:
            return
        step = self._steps[self._k]
        if step.kind in ("rest", "talk"):
            if in_view:
                self._in_view_s += dt
                if step.kind == "rest":
                    self._rest_s += dt
                else:
                    self._talk_s += dt
            if self._in_view_s >= step.seconds - 1e-9:
                self._next(t)
        elif step.kind == "intro":
            if self._t0 is not None and t - self._t0 >= step.seconds - 1e-9:
                self._next(t)
        elif step.kind == "drill":
            index = self._drill_index(t)
            self._issued = min(max(index + 1, self._issued), len(self._drill))
            if index >= len(self._drill):
                self._next(t)  # every prompt has had its whole window
        elif t - self._since >= CHAR_TIMEOUT_S - 1e-9:
            self._presses += 1  # the character that was not typed
            self.counts["practice_timeout"] += 1
            self._advance(t)

    def _begin(self, t: float) -> None:
        self._t0 = t
        self._in_view_s = 0.0
        step = self._steps[0]
        if step.kind == "phrase":
            self._load(step.first, t)

    def _next(self, t: float) -> None:
        self._k += 1
        self._t0 = t
        self._in_view_s = 0.0
        if self.done:
            return
        step = self._steps[self._k]
        if step.kind == "drill":
            self._issued = 1 if self._drill else 0
        elif step.kind == "phrase":
            self._load(step.first, t)

    def _load(self, index: int, t: float) -> None:
        self._phrase = index
        self._char = 0
        self._since = t
        self._echo = ""
        if index == 0 and self._air and len(self._sides) == 1:
            self._hint_until = t + HINT_S

    def _advance(self, t: float) -> None:
        """The current character is done (typed or timed out)."""
        self._char += 1
        self._since = t
        if self._char < len(self._keys[self._phrase]):
            return
        step = self._steps[self._k]
        self._phrase += 1
        if self._phrase >= step.last:
            self._next(t)
        else:
            self._load(self._phrase, t)

    # ------------------------------------------------------------------------------------------------ the events

    def event(self, t: float, side: Side, finger: int, u: float | None, v: float | None, key: int | None) -> str:
        """A press the method emitted, at its own time. Returns what it was: ``phantom`` (a rest), ``talk`` (the talk),
        ``hit``, ``wrong_finger`` or ``extra`` (the drill), else ``""``."""
        kind = self.segment
        if kind == "rest":
            self._rest_phantoms += 1
            self.counts["practice_phantom"] += 1
            return "phantom"
        if kind == "talk":
            self._talk_phantoms += 1
            self.counts["practice_talk_phantom"] += 1
            return "talk"
        if kind != "drill":
            return ""
        index = self._drill_index(t)
        if not 0 <= index < len(self._drill):
            return ""
        prompt = self._drill[index]
        if (side, finger) != (prompt.side, prompt.finger):
            self.counts["practice_wrong_finger"] += 1
            return "wrong_finger"
        if self._hit[index]:
            self.counts["practice_extra"] += 1
            return "extra"
        self._hit[index] = True
        if not prompt.reach and u is not None and v is not None:
            cu, cv = _key_centre(self._layout.keys[prompt.key])
            self._aim.append((u - cu, v - cv))
        return "hit"

    def press(self, t: float, side: Side, finger: int, key: int | None) -> str:
        """A key the session's queue delivered. Returns ``hit`` or ``miss`` while a phrase is on, else ``""``."""
        if self.segment != "phrase":
            return ""
        self._presses += 1
        slot = self._by_finger.setdefault(finger_label(side, finger), [0, 0])
        slot[0] += 1
        if key is not None and key == self._keys[self._phrase][self._char]:
            self._correct += 1
            slot[1] += 1
            self._echo += self._texts[self._phrase][self._char]
            self._advance(t)
            return "hit"
        return "miss"

    # ------------------------------------------------------------------------------------------------ the result

    def result(self, *, fps: float, noise: float, level: Level, completed: bool | None = None) -> PracticeResult:
        """What has been measured so far; ``completed`` is the script's own end unless the caller says otherwise."""
        home = [(i, p) for i, p in enumerate(self._drill[: self._issued]) if not p.reach]
        reach = [(i, p) for i, p in enumerate(self._drill[: self._issued]) if p.reach]
        per_finger: dict[str, tuple[int, int]] = {}
        if self._air:
            for i, p in home:
                prompts, hits = per_finger.get(finger_label(p.side, p.finger), (0, 0))
                per_finger[finger_label(p.side, p.finger)] = (prompts + 1, hits + int(self._hit[i]))
        else:
            per_finger = {label: (n, ok) for label, (n, ok) in self._by_finger.items()}
        im = [(i, p) for i, p in home if p.finger in (0, 1)]
        sd_u = statistics.pstdev([a for a, _ in self._aim]) if len(self._aim) > 1 else 0.0
        sd_v = statistics.pstdev([b for _, b in self._aim]) if len(self._aim) > 1 else 0.0
        return PracticeResult(
            completed=self.done if completed is None else completed,
            presses=self._presses,
            correct=self._correct,
            hit_rate=self._correct / self._presses if self._presses else 0.0,
            rest_s=self._rest_s,
            phantoms=self._rest_phantoms,
            phantoms_per_min=self._rest_phantoms / (self._rest_s / 60.0) if self._rest_s > 0 else 0.0,
            fps=fps,
            per_finger=per_finger,
            talk_s=self._talk_s,
            talk_phantoms=self._talk_phantoms,
            drill_prompts=len(home),
            drill_hits=sum(self._hit[i] for i, _ in home),
            drill_prompts_im=len(im),
            drill_hits_im=sum(self._hit[i] for i, _ in im),
            drill_prompts_reach=len(reach),
            drill_hits_reach=sum(self._hit[i] for i, _ in reach),
            aim_sd_u=sd_u,
            aim_sd_v=sd_v,
            noise=noise,
            level=level,
        )


# ------------------------------------------------------------------------------------------------------- the markers

MARKER_VERSION: Final = 1
#: A marker is a few hundred bytes; anything much larger is not one.
MARKER_MAX_BYTES: Final = 64 * 1024
_MARKER_FILES: Final = MappingProxyType({"air": "keyboard-practice-air.json", "pinch": "keyboard-practice.json"})
MarkerStatus = Literal["missing", "out_of_range", "ok"]


def marker_path(data_dir: Path, press: PressName) -> Path:
    """``<dataDir>/hands/keyboard-practice-air.json`` or ``keyboard-practice.json`` (3.14)."""
    if press not in _MARKER_FILES:
        raise ValueError("only the air and the pinch method have a practice marker")
    return Path(data_dir) / "hands" / _MARKER_FILES[press]


def build_marker(result: PracticeResult, press: PressName, *, now: datetime | None = None) -> dict[str, Any]:
    """The marker of a completed practice: numbers only, no key, no phrase, no timing of an individual tap (3.14)."""
    stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if press == "pinch":
        return {
            "version": MARKER_VERSION,
            "completedAt": stamp,
            "presses": result.presses,
            "restS": round(result.rest_s, 1),
            "phantoms": result.phantoms,
            "hitRate": round(result.hit_rate, 4),
            "fps": round(result.fps, 1),
        }
    if press != "air":
        raise ValueError("only the air and the pinch method have a practice marker")
    return {
        "version": MARKER_VERSION,
        "press": "air",
        "completedAt": stamp,
        "restS": round(result.rest_s, 1),
        "phantoms": result.phantoms,
        "fps": round(result.fps, 1),
        "noise": round(result.noise, 4),
        "talkS": round(result.talk_s, 1),
        "talkPhantoms": result.talk_phantoms,
        "drillPrompts": result.drill_prompts,
        "drillHits": result.drill_hits,
        "drillHitsIM": result.drill_hits_im,
        "drillPromptsIM": result.drill_prompts_im,
        "keyHit": round(result.hit_rate, 4),
        "aimSdU": round(result.aim_sd_u, 4),
        "aimSdV": round(result.aim_sd_v, 4),
    }


def write_marker(
    data_dir: Path, press: PressName, result: PracticeResult, *, now: datetime | None = None
) -> Path | None:
    """Writes the marker of ``press`` atomically when the script completed; a practice closed early writes none."""
    if not result.completed:
        return None
    path = marker_path(data_dir, press)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    try:
        # newline="\n": the same bytes on every platform (text mode would write CRLF on Windows).
        text = json.dumps(build_marker(result, press, now=now), allow_nan=False) + "\n"
        temp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(temp, path)
    except OSError:
        temp.unlink(missing_ok=True)
        raise
    return path


def _number(value: object) -> float | None:
    """A finite JSON number; a bool is not one."""
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return float(value)


def read_marker(data_dir: Path, press: PressName, *, now: datetime | None = None) -> dict[str, Any] | None:
    """The marker of ``press`` when it is present, parsable, for that method, of version 1 and not dated in the future.

    Whether its numbers are inside the bounds is ``marker_in_range``'s question: the open says one thing when there is
    no marker and another when the last practice was too poor (3.8).
    """
    path = marker_path(data_dir, press)
    try:
        if path.stat().st_size > MARKER_MAX_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # a missing, unreadable or unparsable file; ValueError covers decoding as well
        return None
    if not isinstance(data, dict):
        return None
    version = data.get("version")
    if isinstance(version, bool) or version != MARKER_VERSION:
        return None
    if data.get("press", "pinch" if press == "pinch" else None) != press:
        return None
    stamp = data.get("completedAt")
    if not isinstance(stamp, str):
        return None
    try:
        completed = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if completed.tzinfo is None:
        completed = completed.replace(tzinfo=UTC)
    if completed > (now or datetime.now(UTC)):
        return None
    if _number(data.get("restS")) is None or _number(data.get("phantoms")) is None:
        return None
    return data


def marker_in_range(marker: Mapping[str, Any], press: PressName) -> bool:
    """4.4: the bounds a live open needs. ``marker`` has passed ``read_marker``."""
    rest_s = _number(marker.get("restS"))
    phantoms = _number(marker.get("phantoms"))
    if rest_s is None or phantoms is None or rest_s < 0 or phantoms < 0:
        return False
    if press == "pinch":
        return rest_s >= PRACTICE_MIN_REST_S and phantoms * 60.0 <= PRACTICE_MAX_PHANTOMS_PER_MIN * rest_s
    if press != "air":
        return False
    if rest_s < AIR_PRACTICE_MIN_REST_S or phantoms * 60.0 > AIR_PRACTICE_MAX_PHANTOMS_PER_MIN * rest_s:
        return False
    # The talk is reported and never gated; a marker without its numbers reads as 0 and 0.
    prompts = _number(marker.get("drillPromptsIM")) or 0.0
    hits = _number(marker.get("drillHitsIM")) or 0.0
    return prompts < AIR_PRACTICE_MIN_DRILL_PROMPTS or hits / prompts >= AIR_PRACTICE_MIN_DRILL_RECALL


def marker_status(data_dir: Path, press: PressName, *, now: datetime | None = None) -> MarkerStatus:
    """``missing`` (no valid marker), ``out_of_range`` (a marker of a poor practice) or ``ok``."""
    marker = read_marker(data_dir, press, now=now)
    if marker is None:
        return "missing"
    return "ok" if marker_in_range(marker, press) else "out_of_range"


def load_marker(data_dir: Path, press: PressName, *, now: datetime | None = None) -> dict[str, Any] | None:
    """The practice marker of ``press``: present, parsable, for that method, not dated in the future, in range."""
    marker = read_marker(data_dir, press, now=now)
    return marker if marker is not None and marker_in_range(marker, press) else None
