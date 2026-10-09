"""S3 at session level (DESIGN 5.2, 5.10): random users, random cameras, random trouble, real presses.

The string half of S3 (characters, control codes and integers through ``KeyStroke``, ``events_for`` and ``KeySink``) is
in ``test_kb_sink.py``. This half drives the whole session with a synthetic typist whose detector is real: both press
methods (the air tap through ``AirScene``, the pinch through ``PinchScene``), both commit modes where the press allows
them, and a seeded program of things that go wrong around typing: words typed at random gaps and fingers, hands that
jump or leave, a foreign key, a physical modifier, a window that cannot be typed into, a window that just took the
focus, an overlay that died, a camera that slows down or gets noisy, a Windows that takes none or part of a batch,
bursts of taps, Insert and Send taps at any moment, and the run lane that follows them.

Whatever the program does the desktop may only ever see:

* the allowed characters, plus ``space``, ``backspace`` (a direct-mode key only) and ``enter`` as controls;
* batches that are balanced (no key left down);
* no key while a hold was in force (the driver keeps its own record of when each hold began and ended, apart from the
  session's, and a key inside one is a violation);
* no key after the session closed;
* at most ``STORM_N - 1`` keys in two seconds in direct mode (the 12th closes) and ``INSERT_BACKSTOP_N`` in review mode.

These are properties of every stream, so they are asserted for every seed. How much a seed typed is not: a separate
check per variant asserts that the programs typed enough, over all seeds, to make the invariants mean something.
Seeds 0 to 2 run by default (each is a program of 36 steps through a whole session, three to four seconds) and 0 to 47
with ``KB_FULL=1``.
"""

from __future__ import annotations

import dataclasses
import os
import random
from dataclasses import dataclass, field

import pytest

from jarvis_hands.desktop.fake import events_balanced
from jarvis_hands.desktop.keys import ALLOWED_CHARS
from jarvis_hands.keyboard.limits import FOCUS_SETTLE_S, INSERT_BACKSTOP_N, INSERT_BACKSTOP_S, STORM_N, STORM_S, YIELD_S
from jarvis_hands.keyboard.rig import AirScene, PinchScene
from jarvis_hands.keyboard.tuning import RANGES, Tuning
from jarvis_hands.keyboard.types import Side

#: Things the program does to a session.
STEPS = 36
SEEDS = range(48) if os.environ.get("KB_FULL") == "1" else range(3)
LETTERS = "abcdefghijklmnopqrstuvwxyz'"
#: How much of the default a random tuning field may move, as a fraction of its clamp.
SPREAD = 0.15
#: Fields a random tuning moves (the rest stay at their defaults: a tuning that cannot arm tests nothing).
AIR_FIELDS = ("air_theta_k", "air_depth_frac", "air_return_frac", "air_aim_speed", "pitch", "edge_tolerance")
PINCH_FIELDS = ("close", "open", "margin", "pitch", "edge_tolerance", "warm_factor", "hand_hold_s")


def random_tuning(rng: random.Random, names: tuple[str, ...]) -> Tuning:
    """The default tuning with a few fields moved within their clamps, by a fraction of the clamp's width."""
    default = Tuning()
    changes: dict[str, float] = {}
    for name in names:
        low, high = RANGES[name]
        base = getattr(default, name)
        value = base + rng.uniform(-SPREAD, SPREAD) * (high - low)
        changes[name] = min(max(value, low), high)
    return dataclasses.replace(default, **changes)


@dataclass
class Watch:
    """What the driver knows about the desktop and when, apart from what the session believes."""

    scene: AirScene | PinchScene
    commit: str
    keys: list[tuple[float, str, str]] = field(default_factory=list)
    #: (start, end, why): a key strictly inside is a violation.
    holds: list[tuple[float, float, str]] = field(default_factory=list)
    after_close: int = 0

    def __post_init__(self) -> None:
        self.scene.rig.desktop.after_key = self._on_key

    @property
    def t(self) -> float:
        return self.scene.rig.t

    def _on_key(self, _n: int) -> None:
        # Recorded, not asserted: the sink may swallow an exception raised from here.
        kind, value = self.scene.rig.desktop.key_calls[-1]
        self.keys.append((self.t, kind, value))
        if self.scene.rig.closed is not None:
            self.after_close += 1

    def hold(self, start: float, end: float, why: str) -> None:
        self.holds.append((start, end, why))

    def check(self) -> None:
        desktop = self.scene.rig.desktop
        assert self.after_close == 0, "a key was sent after the session closed"
        review = self.commit == "review"
        for kind, value in desktop.key_calls:
            if kind == "char":
                assert value in ALLOWED_CHARS, "a character outside the allow-list reached the desktop"
            else:
                controls = ("space", "enter") if review else ("space", "backspace", "enter")
                assert kind == "control" and value in controls
        assert len(desktop.key_batches) == len(desktop.key_calls)
        assert all(events_balanced(b) for b in desktop.key_batches)
        for t, _, _ in self.keys:
            for start, end, why in self.holds:
                assert not (start < t < end), f"a key at {t:.2f} inside the {why} hold ({start:.2f}, {end:.2f})"
        times = [t for t, _, _ in self.keys]
        limit, window = (INSERT_BACKSTOP_N, INSERT_BACKSTOP_S) if review else (STORM_N - 1, STORM_S)
        for i, t in enumerate(times):
            n = sum(1 for u in times[i:] if u - t <= window)
            assert n <= limit, f"{n} keys in {window} s from {t:.2f}"


def hostile(scene: AirScene | PinchScene, watch: Watch, rng: random.Random, advance) -> None:  # type: ignore[no-untyped-def]
    """One piece of trouble around typing, recorded in ``watch`` as the interval in which no key may be sent.

    ``advance(seconds)`` lets the scene's own typist idle for that long (the hands hover, or the air hands rest)."""
    rig = scene.rig
    desktop = rig.desktop
    kind = rng.choice(["foreign", "modifiers", "blocked", "password", "covered", "focus", "overlay", "fail", "partial"])
    t0 = rig.t
    if kind == "foreign":
        desktop.user_typed()
        watch.hold(t0, t0 + YIELD_S, "foreign input")
        advance(rng.uniform(0.2, 2.5))
    elif kind == "modifiers":
        desktop.modifiers = True
        advance(rng.uniform(0.3, 2.0))
        desktop.modifiers = False
        watch.hold(t0, rig.t, "modifiers")
    elif kind in ("blocked", "password", "covered"):
        before = desktop.target
        if kind == "blocked":
            desktop.target = dataclasses.replace(before, blocked=rng.choice(["elevated", "shell", "own"]))
        elif kind == "password":
            desktop.target = dataclasses.replace(before, password=True)
        else:
            desktop.target = dataclasses.replace(before, covered=True)
        advance(rng.uniform(0.3, 2.5))
        desktop.target = before
        watch.hold(t0, rig.t, kind)
    elif kind == "focus":
        desktop.target = dataclasses.replace(desktop.target, hwnd=desktop.target.hwnd + 1)
        watch.hold(t0, t0 + FOCUS_SETTLE_S, "focus")
        advance(rng.uniform(0.2, 1.5))
    elif kind == "overlay":
        rig.overlay_ok = False
        advance(rng.uniform(0.3, 2.0))
        rig.overlay_ok = True
        watch.hold(t0, rig.t, "overlay")
    elif kind == "fail":
        desktop.fail_keys = rng.randint(1, 3)
    else:
        desktop.partial_keys = rng.randint(1, 2)


# ------------------------------------------------------------------------------------------------- the air program


def air_program(seed: int) -> tuple[AirScene, Watch]:
    rng = random.Random(seed)
    scene = AirScene(
        seed=seed,
        alpha=1.0,
        sigma=rng.choice([0.001, 0.001, 0.0015]),
        tuning=random_tuning(rng, AIR_FIELDS),
    )
    scene.place()
    if not scene.warm():
        return scene, Watch(scene, "review")  # a tuning this user cannot warm up on types nothing: nothing to fuzz
    scene.run(0.5)
    watch = Watch(scene, "review")
    rig = scene.rig
    sides: list[Side] = list(scene.hands)
    for _ in range(STEPS):
        if rig.closed is not None:
            break
        action = rng.choices(
            ["type", "gap", "leave", "hold", "level", "insert", "send", "burst", "clear", "full"],
            weights=[34, 12, 8, 14, 6, 8, 4, 6, 2, 8],
        )[0]
        if action == "type":
            for _ in range(rng.randint(1, 6)):
                scene.tap(rng.choice(LETTERS + "  "), du=rng.uniform(-0.45, 0.45), dv=rng.uniform(-0.3, 0.3))
                scene.run(rng.uniform(0.15, 1.0))
        elif action == "gap":
            scene.run(rng.uniform(0.05, 3.0))
        elif action == "leave":
            side = rng.choice(sides)
            scene.leave(side)
            scene.run(rng.uniform(0.05, 1.2))
            scene.arrive(side)
            scene.run(rng.uniform(0.1, 1.0))
        elif action == "hold":
            hostile(scene, watch, rng, scene.run)
        elif action == "level":
            if rng.random() < 0.5:
                scene.set_fps(rng.choice([12.0, 20.0, 30.0, 30.0, 45.0]))
            else:
                scene.set_sigma(rng.choice([0.001, 0.002, 0.004]))
            scene.run(rng.uniform(0.5, 2.0))
        elif action in ("insert", "send"):
            key = "insert" if action == "insert" else "enter"
            for _ in range(rng.randint(1, 4)):
                scene.tap(key)
                scene.run(rng.uniform(0.3, 1.2))
        elif action == "burst":
            who = [(rng.choice(sides), rng.randrange(4)) for _ in range(rng.randint(4, 16))]
            scene.burst(who, rng.uniform(0.05, 0.3), amp=rng.uniform(25, 50))
            scene.run(rng.uniform(1.0, 3.0))
        elif action == "full":
            scene.user_insert(limit_s=8.0)  # a run, if the box has text and the taps are seen ...
            scene.run(rng.uniform(0.5, 4.0))
            scene.user_send(limit_s=6.0)  # ... and then Send inside its window
            scene.run(rng.uniform(0.5, 2.0))
        else:
            scene.tap("clear")
            scene.run(0.5)
    scene.run(3.0)
    return scene, watch


# ----------------------------------------------------------------------------------------------- the pinch program


def pinch_program(seed: int, commit: str) -> tuple[PinchScene, Watch]:
    rng = random.Random(seed)
    scene = PinchScene(commit=commit, seed=seed, tuning=random_tuning(rng, PINCH_FIELDS))  # type: ignore[arg-type]
    try:
        scene.arm()
    except AssertionError:
        return scene, Watch(scene, commit)  # a tuning whose warm-up does not arm types nothing
    watch = Watch(scene, commit)
    rig, typist = scene.rig, scene.typist
    both = scene.sides

    def hover(seconds: float) -> None:
        scene.feed(typist.hover(seconds, both))

    for _ in range(STEPS):
        if rig.closed is not None:
            break
        action = rng.choices(
            ["type", "gap", "jump", "hold", "level", "insert", "send", "burst", "full"],
            weights=[36, 12, 10, 16, 6, 8, 4, 6, 8],
        )[0]
        if action == "type":
            text = "".join(rng.choice(LETTERS) for _ in range(rng.randint(1, 6)))
            scene.feed(typist.type(text, gap_s=rng.uniform(0.2, 0.9)))
            hover(rng.uniform(0.1, 0.6))
        elif action == "gap":
            hover(rng.uniform(0.05, 3.0))
        elif action == "jump":
            side = rng.choice(both)
            home = typist.home(side)
            scene.feed(typist.glide({side: (home[0] + rng.uniform(-0.2, 0.2), home[1] + rng.uniform(-0.1, 0.1))}))
            hover(rng.uniform(0.1, 1.0))
        elif action == "hold":
            hostile(scene, watch, rng, hover)
        elif action == "level":
            scene.script.fps = rng.choice([8.0, 12.0, 20.0, 30.0, 30.0, 45.0])
            hover(rng.uniform(0.5, 2.0))
        elif action in ("insert", "send"):
            # the direct layout has no Insert key: a letter stands in for it
            key = rig.key("enter" if action == "send" else ("insert" if commit == "review" else "a"))
            scene.feed(typist.tap_n(key, rng.randint(1, 4), rng.uniform(0.3, 0.9)))
            hover(rng.uniform(0.2, 1.0))
        elif action == "full":
            if commit == "review":
                scene.feed(typist.tap_n(rig.key("insert"), 3, 0.5))
                hover(rng.uniform(1.0, 6.0))
                scene.feed(typist.tap_n(rig.key("enter"), 3, 0.5))
            else:
                scene.feed(typist.tap_n(rig.key("enter"), 2, 0.4))
            hover(rng.uniform(0.5, 2.0))
        else:
            text = "".join(rng.choice("asdfjkl'") for _ in range(rng.randint(8, 20)))
            scene.feed(typist.type(text, gap_s=rng.uniform(0.08, 0.2)))
            hover(rng.uniform(0.5, 2.0))
    hover(3.0)
    return scene, watch


# ------------------------------------------------------------------------------------------------------- the tests


def typed_enough(total: int, runs: int, floor_per_run: int) -> None:
    assert total >= floor_per_run * runs, f"only {total} keys over {runs} programs: the invariants say little"


def test_s3_air_programs_never_put_anything_but_allowed_strokes_outside_a_hold_on_the_desktop() -> None:
    typed = 0
    for seed in SEEDS:
        scene, watch = air_program(seed)
        watch.check()
        typed += scene.rig.counts["keys"]
    typed_enough(typed, len(SEEDS), 3)


@pytest.mark.parametrize("commit", ["direct", "review"])
def test_s3_pinch_programs_never_put_anything_but_allowed_strokes_outside_a_hold_on_the_desktop(commit: str) -> None:
    typed = 0
    for seed in SEEDS:
        scene, watch = pinch_program(seed, commit)
        watch.check()
        typed += scene.rig.counts["keys"]
    typed_enough(typed, len(SEEDS), 3)


# ------------------------------------------------------------------------------ the oracle itself can fail


def oracle(commit: str = "review") -> Watch:
    scene = PinchScene(commit=commit)  # type: ignore[arg-type]
    return Watch(scene, commit)


def send(watch: Watch, t: float, kind: str, value: str) -> None:
    """Stands in for a stroke the desktop took at time ``t`` (the fake records it as the real one does)."""
    desktop = watch.scene.rig.desktop
    watch.scene.rig.t = t
    desktop.key_calls.append((kind, value))
    desktop.key_batches.append([(65, 30, 0), (65, 30, 2)])
    watch._on_key(len(desktop.key_calls))


def test_the_oracle_accepts_a_clean_record() -> None:
    watch = oracle()
    send(watch, 1.0, "char", "a")
    send(watch, 1.4, "control", "space")
    watch.hold(2.0, 3.0, "foreign input")
    watch.check()


@pytest.mark.parametrize(
    ("kind", "value", "review"),
    [
        ("char", "1", True),  # a digit
        ("char", "!", True),
        ("char", "\x1b", True),
        ("control", "tab", True),
        ("control", "backspace", True),  # not a stroke of the review box
    ],
)
def test_the_oracle_refuses_what_the_allow_list_refuses(kind: str, value: str, review: bool) -> None:
    watch = oracle("review" if review else "direct")
    send(watch, 1.0, kind, value)
    with pytest.raises(AssertionError):
        watch.check()


def test_the_oracle_refuses_a_key_inside_a_hold_a_key_after_a_close_and_a_storm() -> None:
    watch = oracle()
    send(watch, 2.5, "char", "a")
    watch.hold(2.0, 3.0, "foreign input")
    with pytest.raises(AssertionError):
        watch.check()
    closed = oracle()
    closed.scene.rig._closed = "idle"  # what the rig records when a frame ends in a close
    send(closed, 5.0, "char", "a")
    with pytest.raises(AssertionError):
        closed.check()
    storm = oracle("direct")
    for i in range(STORM_N):
        send(storm, 10.0 + i * 0.1, "char", "a")
    with pytest.raises(AssertionError):
        storm.check()
