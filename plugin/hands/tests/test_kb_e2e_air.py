"""The air keyboard end to end: typists' landmarks, the real tracker and ``AirTapPress``, the session, the review box,
the sink and a ``FakeDesktop`` (DESIGN-KEYBOARD.md 5.10 X40 to X43 and X31 to X33 with real streams, 5.2 N40 to N47 and
5.3 S40 to S46 with real taps, 2.8 and 2.12.8).

``test_kb_air_session.py`` and ``test_kb_review.py`` drive the session with taps a test scripts; the detector is what
this file adds. A hand is an ``AirTypist`` (``rig.AirScene``): it moves to a key and flexes a finger, the tracker reads
its landmarks, ``AirTapPress`` decides, and what reaches the ``FakeDesktop`` is what the whole chain made of it. So
the assertions are of two kinds. *Safety* holds for every stream: nothing reaches the window outside a valid Insert
triple, a stream that means no key types nothing, no character is typed twice, a run that stops leaves its remainder
in the box. *Liveness* holds for the seeds named in the test: a user who looks at the box and corrects it gets their
text into the window. A seed that fails a liveness row is a detector miss, not a session fault; the row's helper says
how many taps it took.

Two choices are the test's, not the design's. The hand moves as a whole with the finger (``alpha`` 1.0): at the
design's 0.65 a ring or pinky finger does not show a tap one row up from home (2.12.6, the table of reaches), so a
sentence with ``o``, ``w`` or ``p`` in it does not always get typed however long the user tries; the rows that need
that case say so and use home-row and bottom-row text. And ``user_type`` taps the next character, waits 0.7 s and
corrects what it sees, the way a person looks at the box.

Timing is the frame's own ``t``; nothing sleeps. Seeds are fixed.
"""

from __future__ import annotations

import copy
import dataclasses
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import pairwise

import pytest

from jarvis_hands.desktop.fake import events_balanced
from jarvis_hands.desktop.keys import ALLOWED_CHARS
from jarvis_hands.keyboard.limits import GUARD_MAX_S, GUARD_MIN_S, GUARD_STILL_SPEED, INSERT_TAPS, SEND_TAPS, STORM_N
from jarvis_hands.keyboard.review import REVIEW_TEXT, GuardTap
from jarvis_hands.keyboard.rig import AirScene, PinchScene, RecordingPinch
from jarvis_hands.keyboard.tuning import Tuning
from jarvis_hands.keyboard.types import PressMethod, Side

FULL = os.environ.get("KB_FULL") == "1"
#: Seeds whose streams the liveness rows were checked on (0 to 3). Each is a whole armed session and about a second, so
#: the default run takes the first two and ``KB_FULL=1`` all four. Safety does not depend on the seed: every row that
#: asserts it asserts it for each seed that runs.
SEEDS = (0, 1, 2, 3) if FULL else (0, 1)
THREE_SEEDS = (0, 1, 2) if FULL else (0, 1)
#: How long a hand that means no key is watched for strays (N40). A stray is a few a second at worst, so twenty seconds
#: of a gesturing hand is a long run for "none of them reaches the window"; ``KB_FULL=1`` watches for forty.
NO_KEY_S = 40.0 if FULL else 20.0
SENTENCE = "hello there, my friend"

#: ``armed`` builds a scene once for each (seed, options) and hands every test its own deep copy: placing the hands and
#: the warm-up are half a second of the one second most of these rows take, and they are the same frames every time.
#: The copy carries the typists' random streams where they stood, so a row sees exactly what it saw when it built its
#: own scene, whatever order the rows run in.
_ARMED: dict[tuple[object, ...], AirScene] = {}


def armed(seed: int = 0, **kw: object) -> AirScene:
    """A warmed-up review session on a typist that moves its hand as a whole, past the sink's warm-up window."""
    kw.setdefault("alpha", 1.0)
    key = (seed, *sorted(kw.items()))
    if "drop" in kw:  # a test's own closure: no other test would ask for the same one
        return _armed(seed, kw)
    if key not in _ARMED:
        _ARMED[key] = _armed(seed, kw)
    return copy.deepcopy(_ARMED[key])


def _armed(seed: int, kw: dict[str, object]) -> AirScene:
    scene = AirScene(seed=seed, **kw)  # type: ignore[arg-type]
    scene.arm()
    scene.run(0.5)
    return scene


@dataclass
class TapLog:
    """Every tap the review machine received, with the time it got it, and the taps that started a run.

    The wrapper sits on the machine of the session under test and changes nothing it does."""

    taps: list[tuple[float, str]] = field(default_factory=list)
    #: (index into ``taps``, "text" | "enter") of each tap that began a run.
    starts: list[tuple[int, str]] = field(default_factory=list)

    @classmethod
    def on(cls, scene: AirScene) -> TapLog:
        log = cls()
        machine = scene.session._machine
        assert machine is not None
        original = machine.tap

        def tap(kind: str, ch: str, t: float, **kw: object) -> object:
            log.taps.append((t, kind))
            step = original(kind, ch, t, **kw)  # type: ignore[arg-type]
            if step is not None and step.first:
                log.starts.append((len(log.taps) - 1, step.kind))
            return step

        machine.tap = tap  # type: ignore[method-assign]
        return log

    def check_oracle(self) -> None:
        """N41: a run began only on the third of three Insert (or Send) taps in a valid window, nothing between."""
        for index, kind in self.starts:
            want, need = ("insert", INSERT_TAPS) if kind == "text" else ("enter", SEND_TAPS)
            window = self.taps[index - need + 1 : index + 1]
            assert len(window) == need and all(k == want for _, k in window), (kind, window)
            times = [t for t, _ in window]
            assert all(b - a >= GUARD_MIN_S for a, b in pairwise(times)), times
            assert times[-1] - times[0] <= GUARD_MAX_S, times

    def count(self, kind: str) -> int:
        return sum(1 for _, k in self.taps if k == kind)


def allowed_only(scene: AirScene) -> None:
    """S3/S4 for what the desktop saw: allowed characters and the review controls only, every batch balanced."""
    for kind, value in scene.rig.desktop.key_calls:
        if kind == "char":
            assert value in ALLOWED_CHARS
        else:
            assert value in ("space", "enter")
    assert all(events_balanced(b) for b in scene.rig.desktop.key_batches)
    assert len(scene.rig.desktop.key_batches) == len(scene.rig.desktop.key_calls)


def compose(scene: AirScene, text: str, tries: int = 10) -> None:
    assert scene.user_type(text, tries=tries), (text, scene.shown)
    assert scene.rig.desktop.key_calls == []


def insert(scene: AirScene, text: str | None = None) -> None:
    assert scene.user_insert(text), (scene.rig.counts, scene.shown)


def pinch_fallback() -> PressMethod:
    """What the controller gives a live air session: the real pinch detector, to switch to at ladder level off."""
    return RecordingPinch(Tuning())


def set_target(scene: AirScene, **changes: object) -> None:
    scene.rig.desktop.target = dataclasses.replace(scene.rig.desktop.target, **changes)


def finish_run(scene: AirScene, since: int = 0, limit_s: float = 15.0) -> None:
    """Frames until the summaries number more than ``since``: the run that was started has ended."""
    rig = scene.rig
    end = rig.t + limit_s
    while len(rig.summaries) <= since and rig.t < end and rig.closed is None:
        scene.step()
    assert len(rig.summaries) > since, "the run did not end"


# --------------------------------------------------------------------------------------------- X40: end to end


@pytest.mark.parametrize("seed", SEEDS)
def test_x40_warm_up_by_taps_then_hello_and_three_insert_taps_type_it(seed: int) -> None:
    scene = AirScene(seed=seed, alpha=1.0)
    scene.place()
    assert scene.warm(), scene.rig.counts
    rig = scene.rig
    assert rig.counts["warmup_accepted"] == 8 and rig.desktop.key_calls == []  # the warm-up types nothing
    assert rig.session.armed and rig.session.phase == "typing" and rig.box == ""
    scene.run(0.5)
    log = TapLog.on(scene)
    compose(scene, "hello")
    assert rig.box == "hello"
    inserted_before = rig.counts["insert_start"]
    # the taps one at a time: nothing reaches the window before the third counted one
    counted = 0
    while rig.counts["insert_start"] == inserted_before and counted < 8:
        before = log.count("insert")
        truth = scene.tap("insert")
        scene.run(max(truth.t - scene.t, 0.0) + 0.9)
        counted += log.count("insert") - before
        if rig.counts["insert_start"] == inserted_before:
            assert rig.desktop.key_calls == []
    assert rig.counts["insert_start"] == inserted_before + 1
    finish_run(scene)
    assert rig.typed == "hello" and rig.box == "" and rig.summaries[-1].outcome == "done"
    log.check_oracle()
    allowed_only(scene)


@pytest.mark.parametrize("seed", SEEDS)
def test_a_sentence_composed_in_the_box_reaches_the_window_only_by_the_run_and_exactly(seed: int) -> None:
    scene = armed(seed)
    rig = scene.rig
    log = TapLog.on(scene)
    compose(scene, SENTENCE)
    assert rig.desktop.key_calls == [] and rig.box == SENTENCE
    insert(scene, SENTENCE)
    finish_run(scene)
    assert rig.typed == SENTENCE and rig.box == "" and rig.closed is None
    assert [s.outcome for s in rig.summaries] == ["done"] and rig.summaries[0].sent == len(SENTENCE)
    assert len(rig.desktop.key_calls) == len(SENTENCE) and len(log.starts) == 1
    log.check_oracle()
    allowed_only(scene)
    # a space is the control key, a letter a character; the run types one character per frame at most
    assert rig.desktop.key_calls.count(("control", "space")) == SENTENCE.count(" ")


@pytest.mark.parametrize("seed", SEEDS)
def test_a_wrong_key_is_taken_out_with_backspace_before_anything_is_typed(seed: int) -> None:
    scene = armed(seed)
    rig = scene.rig
    compose(scene, "hel")
    truth = scene.tap("x")  # a key the user did not mean
    scene.run(max(truth.t - scene.t, 0.0) + 0.9)
    assert rig.box.startswith("hel") and len(rig.box) <= 4
    wrong = rig.box != "hel"
    if wrong:
        truth = scene.tap("backspace")
        scene.run(max(truth.t - scene.t, 0.0) + 0.9)
    compose(scene, "hello")
    insert(scene, "hello")
    finish_run(scene)
    assert rig.typed == "hello" and rig.box == ""  # the wrong key never reached the window
    allowed_only(scene)


@pytest.mark.parametrize("seed", SEEDS)
def test_clear_needs_two_taps_and_empties_the_box_without_typing(seed: int) -> None:
    scene = armed(seed)
    rig = scene.rig
    compose(scene, "abc")
    for _ in range(4):  # the left pinky reaches Clear; retried until it counts
        truth = scene.tap("clear")
        scene.run(max(truth.t - scene.t, 0.0) + 0.9)
        if rig.box == "":
            break
    assert rig.box == "" and rig.counts["cleared"] == 1 and rig.desktop.key_calls == []


@pytest.mark.parametrize("seed", SEEDS)
def test_send_is_three_more_taps_after_the_insert_and_presses_enter_once(seed: int) -> None:
    scene = armed(seed)
    rig = scene.rig
    log = TapLog.on(scene)
    compose(scene, "hello")
    insert(scene, "hello")
    finish_run(scene)
    assert rig.typed == "hello"
    assert scene.user_send(), rig.counts
    finish_run(scene, since=1)
    assert rig.typed == "hello\n" and rig.counts["send_done"] == 1 and rig.summaries[-1].kind == "enter"
    for _ in range(SEND_TAPS):  # single shot: three more taps on Send find no opportunity
        truth = scene.tap("enter")
        scene.run(max(truth.t - scene.t, 0.0) + 1.0)
    assert rig.typed == "hello\n" and rig.counts["send_start"] == 1
    log.check_oracle()
    allowed_only(scene)


def test_send_without_an_insert_before_it_types_nothing_however_often_it_is_tapped() -> None:
    scene = armed(1)
    rig = scene.rig
    compose(scene, "hello")
    for _ in range(10):
        truth = scene.tap("enter")
        scene.run(max(truth.t - scene.t, 0.0) + 0.8)
    assert rig.desktop.key_calls == [] and rig.counts["send_start"] == 0 and rig.counts["insert_start"] == 0


def test_an_empty_or_blank_box_is_not_a_run_however_often_insert_is_tapped() -> None:
    scene = armed(2)
    rig = scene.rig
    for _ in range(6):
        truth = scene.tap("insert")
        scene.run(max(truth.t - scene.t, 0.0) + 0.8)
    assert rig.desktop.key_calls == [] and rig.counts["insert_start"] == 0
    truth = scene.tap(" ")
    scene.run(max(truth.t - scene.t, 0.0) + 0.9)
    for _ in range(6):
        truth = scene.tap("insert")
        scene.run(max(truth.t - scene.t, 0.0) + 0.8)
    assert rig.desktop.key_calls == [] and rig.counts["insert_start"] == 0


def test_a_tap_on_another_key_between_the_insert_taps_starts_the_count_again() -> None:
    scene = armed(0)
    rig = scene.rig
    log = TapLog.on(scene)
    compose(scene, "hi")
    begun = rig.counts["insert_start"]
    for kind in ("insert", "insert", "e", "insert", "insert"):
        truth = scene.tap(kind)
        scene.run(max(truth.t - scene.t, 0.0) + 0.9)
    assert rig.counts["insert_start"] == begun and rig.desktop.key_calls == []
    log.check_oracle()
    held = rig.box  # the stray 'e' tap, if it landed, is in the box too
    assert held.startswith("hi")
    insert(scene, None)
    finish_run(scene)
    assert rig.typed == held and rig.box == ""
    log.check_oracle()
    allowed_only(scene)


# ----------------------------------------------------------------------------------- phantoms (X8, X53, N40 to N47)

#: The repo's negative scenarios, grouped by what they stand for.
CALM = ("still", "roll", "thumb", "drift")
MOVING = ("wave", "wave_fast", "open_close", "open_close_fast", "reach", "finger_wiggle")
GESTURING = ("talk_hands", "talking", "fidget")


#: The default run leaves out the scenarios that are a gentler version of another: ``roll``, ``thumb`` and ``drift``
#: are calm hands like ``still``, ``wave`` and ``open_close`` the slower ones of their ``_fast`` kin. ``KB_FULL=1``
#: runs all of them.
NO_KEY_SCENARIOS = [*CALM, *MOVING, *GESTURING]
if not FULL:
    NO_KEY_SCENARIOS = [n for n in NO_KEY_SCENARIOS if n not in ("roll", "thumb", "drift", "wave", "open_close")]


@pytest.mark.parametrize("name", NO_KEY_SCENARIOS)
def test_hands_that_mean_no_key_never_put_a_stroke_on_the_desktop(name: str) -> None:
    """The invariant of N40: a stream of tapping-like movement fills the box with strays at worst. It never reaches
    the window, and no run starts. The ladder may judge the camera unreliable on a hand that is gesturing: with the
    controller's fallback that is a switch to the pinch warm-up, which types nothing either. A hand that is only
    resting, rolling, drifting or holding the thumb out makes next to no strays at all (at most two)."""
    scene = armed(7, fallback=pinch_fallback)
    rig = scene.rig
    log = TapLog.on(scene)
    compose(scene, "hello")
    keys = rig.counts["keys"]
    scene.negative(name, seconds=NO_KEY_S + 5.0)
    scene.run(NO_KEY_S)
    assert rig.desktop.key_calls == [] and rig.counts["insert_start"] == 0 and log.starts == []
    assert rig.closed in (None, "air_unreliable")  # never input_blocked or runaway
    allowed_only(scene)
    if name in CALM:
        assert rig.closed is None and rig.counts["keys"] - keys <= 2


def test_three_insert_taps_by_a_parked_pinky_in_a_gesturing_stream_do_not_start_a_run() -> None:
    """The deliberate first tap of N48 with the phantoms of a talking hand that follow it: the run never starts from
    strays, because the guard needs three Insert taps with nothing else between and the stray stream is not that."""
    scene = armed(5, fallback=pinch_fallback)
    rig = scene.rig
    log = TapLog.on(scene)
    compose(scene, "hello")
    truth = scene.tap("insert")
    scene.run(max(truth.t - scene.t, 0.0) + 0.9)
    scene.negative("talk_hands")
    scene.run(20.0)
    assert rig.desktop.key_calls == [] and rig.counts["insert_start"] == 0
    log.check_oracle()


def test_the_session_gives_the_machine_the_evidence_of_an_air_tap_on_insert_and_send_only() -> None:
    """The guards weigh a tap by what the press knew of it (2.13.4): its firmness, the speed of its hand at the frame of
    the press and its finger. That is the session's to hand over, for Insert and Send and for nothing else, and it
    carries no place and no letter."""
    scene = armed(1)
    machine = scene.session._machine
    assert machine is not None
    seen: list[tuple[str, object, object]] = []
    original = machine.tap

    def spy(kind: str, ch: str, t: float, **kw: object) -> object:
        seen.append((kind, kw.get("touch"), kw.get("guard")))
        return original(kind, ch, t, **kw)  # type: ignore[arg-type]

    machine.tap = spy  # type: ignore[method-assign]
    compose(scene, "hi")
    insert(scene)
    letters = [(touch, guard) for kind, touch, guard in seen if kind == "char"]
    inserts = [(touch, guard) for kind, touch, guard in seen if kind == "insert"]
    assert letters and all(touch is not None and guard is None for touch, guard in letters)
    assert len(inserts) >= INSERT_TAPS and all(touch is None for touch, _ in inserts)
    for _, guard in inserts:
        assert isinstance(guard, GuardTap)
        assert 0.0 < guard.conf <= 1.0 and 0.0 <= guard.speed <= GUARD_STILL_SPEED
        assert (guard.side, guard.finger) == ("right", 3)  # the pinky of the right hand, the user's Insert finger
        assert repr(guard) == "<GuardTap>"


# -------------------------------------------------------------------------------------- X42: storm and tremor

TWO_HANDS = [("left", 0), ("right", 0), ("left", 1), ("right", 1), ("left", 2), ("right", 2), ("left", 3), ("right", 3)]


@pytest.mark.parametrize("seed", THREE_SEEDS)
def test_x42_sixteen_taps_of_two_hands_in_two_seconds_freeze_the_taps_at_the_twelfth_and_close_nothing(
    seed: int,
) -> None:
    scene = armed(seed, style="decisive")
    rig = scene.rig
    scene.burst([TWO_HANDS[i % 8] for i in range(16)], 0.13, amp=48, dur=0.14)
    froze = scene.run_until(lambda: rig.counts["storm_freeze"] == 1, 4.0)
    assert froze
    assert {t.state for t in rig.view.tips} == {"latched"}  # the freeze re-latched every finger (press.reset)
    typed = len(rig.box)
    scene.run(1.0)
    assert 8 <= typed <= 11 and len(rig.box) <= 11  # the 12th tap and every one after it are not in the box
    assert rig.counts["storm_freeze"] == 1 and rig.closed is None and rig.desktop.key_calls == []
    assert rig.counts["frozen"] >= 1 and rig.view.strip == REVIEW_TEXT["storm"]


@pytest.mark.parametrize("seed", (0, 1))
def test_x42_the_taps_come_back_after_the_freeze_and_the_next_one_is_typed(seed: int) -> None:
    scene = armed(seed, style="decisive")
    rig = scene.rig
    scene.burst([TWO_HANDS[i % 8] for i in range(16)], 0.13, amp=48, dur=0.14)
    assert scene.run_until(lambda: rig.counts["storm_freeze"] == 1, 4.0)
    box = rig.box
    scene.run(3.6)  # past STORM_FREEZE_S and the two quiet frames a finger needs to be open again
    truth = scene.tap("j")
    scene.run(max(truth.t - scene.t, 0.0) + 0.9)
    assert rig.box == box + "j" and rig.desktop.key_calls == []


def test_x42_normal_fast_typing_five_keys_a_second_is_no_storm() -> None:
    scene = armed(3, style="ordinary")
    rig = scene.rig
    scene.burst([TWO_HANDS[i % 8] for i in range(24)], 0.2, amp=42, dur=0.16)
    scene.run(24 * 0.2 + 1.5)
    assert rig.counts["storm_freeze"] == 0 and rig.counts["frozen"] == 0 and len(rig.box) >= 18


def test_x42_a_twelve_a_second_burst_of_one_hand_trips_the_tremor_guard_not_the_storm() -> None:
    """Middle and pinky alternating at 83 ms: five commits of one hand inside 0.5 s are thrown away and the hand is
    suppressed 0.6 s (S13). It does not trip on every draw (the coupling veto thins some bursts below five commits),
    so the guard is counted over four seeds (it tripped on two of them); what holds on each is that the burst makes no
    storm freeze and no run."""
    tripped = 0
    for seed in range(4):
        scene = armed(seed, coupling_p=0.0)
        rig = scene.rig
        before = scene.session.rejects["tremor"]
        scene.burst([("right", 1 if i % 2 == 0 else 3) for i in range(14)], 1 / 12, amp=42, dur=0.2)
        scene.run(3.0)
        tripped += scene.session.rejects["tremor"] > before
        assert rig.counts["storm_freeze"] == 0 and rig.closed is None and rig.desktop.key_calls == []
        assert len(rig.box) < STORM_N
    assert tripped >= 1  # X17 pins the count on the detector alone


# ---------------------------------------------------------------------------------------- X43: two hands at once


@pytest.mark.parametrize("seed", SEEDS)
def test_x43_taps_of_both_hands_in_the_same_frame_are_both_typed_in_order(seed: int) -> None:
    scene = armed(seed)
    rig = scene.rig
    scene.burst([("left", 0), ("right", 0)], 0.0)
    scene.run(1.2)
    assert sorted(rig.box) == ["f", "j"] and rig.counts["stale"] == 0 and rig.counts["queue"] == 0


# ------------------------------------------------------------------------------------------- the ladder (X31-X33)


def test_the_ladder_stays_ok_on_a_clean_stream_and_a_hover_does_not_degrade_it() -> None:
    scene = armed(0)
    scene.run(20.0)
    assert scene.session.level == "ok" and scene.rig.view.banner == "" and scene.press.levels == []


def test_x32_noise_of_0_002_degrades_to_the_strict_thresholds_and_banner_and_typing_goes_on() -> None:
    scene = armed(1, fallback=pinch_fallback)
    rig = scene.rig
    scene.set_sigma(0.002)
    assert scene.run_until(lambda: scene.session.level == "degraded", 15.0)
    assert rig.view.banner == "Air tap is less sure: hand tracking is shaky. Tap a little firmer."
    assert rig.view.banner_level == "warn" and scene.press.levels == ["degraded"]
    assert scene.session.press_name == "air" and rig.session.phase == "typing"
    scene.set_sigma(0.001)  # a camera that gets better: back to ok after the recovery time, not before
    scene.run(4.0)
    assert scene.session.level == "degraded"
    assert scene.run_until(lambda: scene.session.level == "ok", 30.0)
    assert rig.view.banner == "" and scene.press.levels == ["degraded", "ok"]


def test_x32_noise_of_0_004_is_off_and_the_session_switches_to_the_pinch_warm_up() -> None:
    scene = armed(2, fallback=pinch_fallback)
    rig = scene.rig
    compose(scene, "ok")
    scene.set_sigma(0.004)
    box = rig.box
    while scene.session.press_name == "air" and rig.t < 40.0 and rig.closed is None:
        box = rig.box  # a shaky camera makes strays of its own; the switch frame must not change the box
        scene.step()
    assert scene.session.press_name == "pinch" and rig.box == box
    assert scene.session.phase == "warmup" and not scene.session.armed and scene.session.level == "off"
    assert rig.view.banner == "Air tap off: hand tracking too shaky, using pinch"
    assert rig.desktop.key_calls == []  # nothing was typed by the switch


def test_x31_a_slow_camera_degrades_with_the_frame_rate_in_the_banner_and_changes_no_threshold() -> None:
    scene = armed(3, fallback=pinch_fallback)
    rig = scene.rig
    scene.set_fps(20)
    assert scene.run_until(lambda: scene.session.level == "degraded", 20.0)
    assert rig.view.banner.startswith("Air tap is less sure: camera at ")
    assert scene.press.levels == []  # a slow camera loses recall, not precision: the thresholds stay (2.12.7)
    scene.set_fps(30)
    assert scene.run_until(lambda: scene.session.level == "ok", 40.0) and rig.view.banner == ""


def test_x31_a_camera_at_twelve_frames_a_second_is_off_and_the_fallback_is_pinch_end_to_end() -> None:
    """Air at 30 fps, then the camera drops to 12 fps: banner, level off, the switch to the pinch warm-up with the box
    kept and nothing typed; a pinch typist (on the plane the air session placed) warms up, types into the same box and
    taps Insert three times, and the text reaches the window."""
    scene = armed(0, fallback=pinch_fallback)
    rig = scene.rig
    compose(scene, "hi")
    scene.set_fps(12)
    assert scene.run_until(lambda: scene.session.press_name == "pinch", 30.0)
    session = scene.session
    assert (session.phase, session.armed, session.level) == ("warmup", False, "off")
    assert rig.view.banner == "Air tap off: camera at 12 fps, using pinch" and rig.box == "hi"
    assert rig.desktop.key_calls == [] and rig.counts["insert_start"] == 0
    scene.set_fps(30)
    pinch = PinchScene.following(rig, seed=3)
    pinch.warm()
    assert session.armed and session.phase == "typing" and session.press_name == "pinch"
    pinch.type("a", gap_s=0.5)
    assert rig.box == "hia"
    for _ in range(3):
        pinch.press_key("insert")
        pinch.hover(0.8)
    pinch.hover(1.5)
    assert rig.typed == "hia" and rig.box == "" and rig.summaries[-1].outcome == "done"
    allowed_only(scene)


def test_x31_with_no_fallback_a_live_session_closes_air_unreliable_and_counts_its_box() -> None:
    scene = armed(1)
    rig = scene.rig
    compose(scene, "hello")
    scene.set_fps(11)
    assert scene.run_until(lambda: rig.closed is not None, 30.0)
    assert rig.closed == "air_unreliable" and rig.session.discarded == 5 and rig.desktop.key_calls == []
    keys = rig.desktop.key_calls
    scene.run(1.0)
    assert rig.desktop.key_calls == keys


# --------------------------------------------------------------------------- hands leaving and returning (2.8)


def test_a_hand_that_leaves_for_three_seconds_comes_back_to_a_session_that_is_still_armed() -> None:
    scene = armed(0)
    rig = scene.rig
    compose(scene, "ab")
    accepted = rig.counts["warmup_accepted"]
    scene.leave("right")
    scene.run(3.0)
    assert [h.side for h in rig.session.hands] == ["left"] and rig.session.armed and rig.closed is None
    truth = scene.tap("a")  # the left hand goes on typing meanwhile
    scene.run(max(truth.t - scene.t, 0.0) + 0.9)
    assert rig.box == "aba"
    scene.arrive("right")
    scene.run(1.5)  # a returning hand is latched until a finger has been open for two frames
    truth = scene.tap("j")
    scene.run(max(truth.t - scene.t, 0.0) + 0.9)
    assert rig.box == "abaj" and rig.counts["warmup_accepted"] == accepted  # no second warm-up
    assert rig.session.phase == "typing" and rig.desktop.key_calls == []


@pytest.mark.parametrize("seed", THREE_SEEDS)
def test_a_hand_lost_in_the_middle_of_a_tap_types_nothing_for_that_tap(seed: int) -> None:
    """2.8 and X18: a tap still in progress when the hand goes is ignored; the hand that returns is a new track whose
    fingers are latched, so the dip is never finished by a hand that comes back."""
    scene = armed(seed)
    rig = scene.rig
    keys = rig.counts["keys"]
    truth = scene.tap("j")
    scene.run_until(lambda: scene.t >= truth.t + 0.10, 3.0)
    scene.leave("right")
    scene.run(0.6)
    scene.arrive("right")
    scene.run(2.5)
    assert rig.counts["keys"] == keys and rig.box == ""
    truth = scene.tap("j")  # and the next tap is seen as usual
    scene.run(max(truth.t - scene.t, 0.0) + 0.9)
    assert rig.box == "j"


def test_a_tap_that_had_already_committed_is_delivered_when_the_hand_leaves_right_after() -> None:
    scene = armed(0)
    rig = scene.rig
    scene.tap("j")
    scene.run_until(lambda: len(scene.press.fired) > 8, 3.0)  # the 8 warm-up taps, then this one
    scene.leave("right")
    scene.run(0.5)
    assert rig.box == "j" and rig.counts["stale"] == 0


@pytest.mark.parametrize("seed", THREE_SEEDS)
def test_a_camera_stall_in_the_middle_of_a_tap_gives_no_key_and_the_next_tap_is_typed(seed: int) -> None:
    """N11 for the air tap: no frames for 0.4 s (above GAP_RESET_S): the press resets, the dip is not finished."""
    stall: list[float] = []
    scene = armed(seed, drop=lambda k, t: bool(stall) and stall[0] <= t < stall[0] + 0.4)
    rig = scene.rig
    keys = rig.counts["keys"]
    truth = scene.tap("j")
    stall.append(truth.t + 0.10)
    scene.run(max(truth.t - scene.t, 0.0) + 1.5)
    assert rig.counts["keys"] == keys and rig.box == ""
    truth = scene.tap("j")
    scene.run(max(truth.t - scene.t, 0.0) + 0.9)
    assert rig.box == "j"


def test_both_hands_away_keep_the_box_for_a_long_time_and_close_it_idle_after_two_minutes() -> None:
    scene = armed(0)
    rig = scene.rig
    compose(scene, "hello")
    scene.leave("left")
    scene.leave("right")
    scene.run(101.0)
    assert rig.closed is None and rig.box == "hello"  # idle_s would close an empty box; a box waits REVIEW_IDLE_S
    assert rig.view.strip.startswith("Closing in ")  # the last 20 s are counted down
    scene.run(30.0)
    assert rig.closed == "idle" and rig.session.discarded == 5 and rig.desktop.key_calls == []


def test_hands_that_come_back_before_the_idle_close_find_the_box_as_they_left_it() -> None:
    scene = armed(1)
    rig = scene.rig
    compose(scene, "hello")
    scene.leave("left")
    scene.leave("right")
    scene.run(60.0)
    scene.arrive("left")
    scene.arrive("right")
    scene.run(2.0)
    assert rig.closed is None and rig.box == "hello" and rig.session.armed
    insert(scene, "hello")
    finish_run(scene)
    assert rig.typed == "hello"


# ------------------------------------------------------------------------------------------------- one hand


@pytest.mark.parametrize("side", ["right", "left"])
@pytest.mark.parametrize("seed", THREE_SEEDS)
def test_one_hand_warms_up_with_four_taps_and_types_a_word_and_inserts_it(side: Side, seed: int) -> None:
    scene = AirScene(seed=seed, alpha=1.0, sides=(side,))
    scene.place()
    assert scene.warm(), scene.rig.counts
    rig = scene.rig
    assert rig.counts["warmup_accepted"] == 4 and rig.desktop.key_calls == []
    scene.run(0.5)
    compose(scene, "hello", tries=12)
    insert(scene, "hello")
    finish_run(scene)
    assert rig.typed == "hello" and rig.box == "" and rig.closed is None
    allowed_only(scene)


def test_one_hand_reaches_every_review_key_with_its_index_finger() -> None:
    scene = AirScene(seed=4, alpha=1.0, sides=("right",))
    scene.arm()
    scene.run(0.5)
    for kind in ("backspace", "clear", "enter"):
        key = scene.rig.key(kind)
        truth = scene.tap(kind)
        assert (truth.side, truth.finger) == ("right", 0) and truth.key == key.index


# ------------------------------------------------------------------------------- partial runs (S42 to S46, air)

TEN = "hellothere"


def compose_and_insert(scene: AirScene, text: str, hook: Callable[[int], None] | None = None) -> None:
    compose(scene, text)
    scene.rig.desktop.after_key = hook
    insert(scene, text)


@pytest.mark.parametrize("seed", (0, 1))
def test_s42_a_focus_change_after_five_of_ten_keeps_the_rest_and_a_second_insert_types_only_that(seed: int) -> None:
    scene = armed(seed)
    rig = scene.rig
    log = TapLog.on(scene)
    original = rig.desktop.target

    def away(n: int) -> None:
        if n == 5:
            rig.desktop.target = dataclasses.replace(original, hwnd=999, pid=300)

    compose_and_insert(scene, TEN, away)
    finish_run(scene)
    first = rig.summaries[0]
    assert (first.kind, first.outcome, first.sent, first.of, first.reason) == ("text", "aborted", 5, 10, "focus")
    assert rig.typed == "hello" and rig.box == "there" and rig.counts["abort_focus"] == 1
    rig.desktop.after_key = None
    rig.desktop.target = original  # the user goes back to the window
    scene.run(2.0)
    insert(scene, "there")
    finish_run(scene, since=1)
    assert rig.typed == TEN and rig.box == "" and rig.summaries[1].outcome == "done"
    log.check_oracle()
    allowed_only(scene)


@pytest.mark.parametrize("seed", (0, 1))
def test_s46_a_refused_stroke_leaves_the_remainder_from_that_character_and_a_second_insert_types_it(seed: int) -> None:
    scene = armed(seed)
    rig = scene.rig
    compose_and_insert(scene, "hello", lambda n: setattr(rig.desktop, "fail_keys", 1) if n == 3 else None)
    finish_run(scene)
    first = rig.summaries[0]
    assert (first.outcome, first.sent, first.of, first.reason) == ("aborted", 3, 5, "failed")
    assert rig.typed == "hel" and rig.box == "lo"
    rig.desktop.after_key = None
    scene.run(1.0)
    insert(scene, "lo")
    finish_run(scene, since=1)
    assert rig.typed == "hello" and rig.box == ""


@pytest.mark.parametrize("seed", (0, 1))
def test_s46_a_partly_taken_batch_counts_as_typed_and_the_remainder_starts_after_it(seed: int) -> None:
    scene = armed(seed)
    rig = scene.rig
    compose_and_insert(scene, "hello", lambda n: setattr(rig.desktop, "partial_keys", 1) if n == 3 else None)
    finish_run(scene)
    first = rig.summaries[0]
    assert (first.outcome, first.sent, first.of, first.reason) == ("aborted", 4, 5, "failed")
    assert rig.typed == "hell" and rig.box == "o"  # the partial one is typed: never typed twice
    rig.desktop.after_key = None
    scene.run(1.0)
    insert(scene, "o")
    finish_run(scene, since=1)
    assert rig.typed == "hello" and rig.box == ""


def test_s43_the_user_typing_on_the_real_keyboard_stops_the_run_and_the_rest_waits_in_the_box() -> None:
    scene = armed(2)
    rig = scene.rig
    compose_and_insert(scene, TEN, lambda n: rig.desktop.user_typed() if n == 3 else None)
    finish_run(scene)
    first = rig.summaries[0]
    assert (first.outcome, first.sent, first.reason) == ("aborted", 3, "yield")
    assert rig.typed == "hel" and rig.box == "lothere"
    rig.desktop.after_key = None
    scene.run(3.0)  # the hold of 1.5 s, and the fingers reopen
    insert(scene, "lothere")
    finish_run(scene, since=1)
    assert rig.typed == TEN and rig.box == ""


def test_s43_a_tap_on_insert_a_second_into_a_long_run_stops_it_and_loses_and_repeats_nothing() -> None:
    scene = armed(3)
    rig = scene.rig
    text = "".join("abcdefghij"[i % 10] for i in range(200))
    rig.fill(text)
    insert(scene, None)
    scene.run_until(lambda: len(rig.desktop.key_calls) >= 20, 5.0)
    assert rig.session.review_state == "inserting"
    for _ in range(4):  # the Stop tap: Insert again, once STOP_ARM_S has passed; retried while the run still goes
        truth = scene.tap("insert")
        scene.run(max(truth.t - scene.t, 0.0) + 0.6)
        if rig.summaries:
            break
    finish_run(scene)
    summary = rig.summaries[0]
    assert summary.outcome == "aborted" and summary.reason == "stopped" and 20 <= summary.sent < 200
    assert rig.typed + rig.box == text and rig.typed == text[: summary.sent]  # nothing lost, nothing typed twice


def test_the_hands_leaving_in_the_middle_of_a_run_do_not_cut_it_idle_time_does_not_run_during_a_run() -> None:
    scene = armed(1)
    rig = scene.rig
    text = "".join("abcdefghij"[i % 10] for i in range(200))
    rig.fill(text)
    insert(scene, None)
    scene.leave("left")
    scene.leave("right")
    finish_run(scene, since=0, limit_s=20.0)
    assert rig.summaries[0].outcome == "done" and rig.typed == text and rig.closed is None


@pytest.mark.parametrize("blocked", ["elevated", "shell", "own", "none"])
def test_s44_a_window_that_cannot_be_typed_into_takes_the_insert_taps_and_types_nothing(blocked: str) -> None:
    scene = armed(0)
    rig = scene.rig
    compose(scene, "hello")
    set_target(scene, blocked=blocked)
    scene.run(0.5)
    for _ in range(4):
        truth = scene.tap("insert")
        scene.run(max(truth.t - scene.t, 0.0) + 0.9)
    assert rig.desktop.key_calls == [] and rig.counts["insert_start"] == 0 and rig.box == "hello"
    assert rig.view.hold == "blocked"
