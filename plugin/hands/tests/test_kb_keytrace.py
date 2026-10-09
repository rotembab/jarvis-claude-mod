"""``keytrace``: the recorder of landmark traces (DESIGN-KEYBOARD.md 3.13, 5.7, 2.12.6; the recording half of X50).

Nothing here opens a camera or sleeps: the camera and the tracker are scripted, their frames carry their own times, and
the stall timer reads a clock the camera moves. What is pinned: the segment grammar and the plan it makes, the drill's
prompts (balanced, alternately displaced, never one finger twice in a row), the file that comes out (the pinned arrays
plus the prompt table, nothing else, ``press`` and ``version`` 2) and every way the recording can end.
"""

from __future__ import annotations

import argparse
import io
import os
import random
import re
from collections import Counter
from collections.abc import Callable
from itertools import pairwise
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from jarvis_hands import models
from jarvis_hands import synthetic as syn
from jarvis_hands.camera.base import CameraError, CameraFrame, CameraInfo
from jarvis_hands.keyboard import keytrace
from jarvis_hands.keyboard.layout import layout_for
from jarvis_hands.keyboard.limits import AIR_DRILL_GAP_S, AIR_DRILL_MOVE_ROWS, AIR_DRILL_MOVE_U
from jarvis_hands.keyboard.practice import FINGER_NAMES, HOME_CHARS, PHRASES
from jarvis_hands.keyboard.trace import TRACE_MAX_S, read_trace
from jarvis_hands.landmarks import Frame
from jarvis_hands.tracker.base import TrackerError

REVIEW = layout_for("review")
FPS = 30.0
START = 1000.0
OBS = {
    "left": syn.hand("palm", (0.36, 0.55), handedness="left"),
    "right": syn.hand("palm", (0.64, 0.55), handedness="right"),
}
ALLOWED_ARRAYS = {
    "t", "present", "side", "score", "lm", "aspect", "width", "height", "segments", "targets", "press", "version",
    "drill_prompts",
}  # fmt: skip


# ------------------------------------------------------------------------------------------------------ the scripts


class Clock:
    """The one clock the stall timer reads; the camera moves it."""

    def __init__(self, start: float = START) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t


class ScriptedCamera:
    """A camera that gives a frame every 1/fps of the shared clock, until told to fail, stall or be interrupted."""

    def __init__(
        self,
        clock: Clock,
        *,
        fps: float = FPS,
        lost_after: int | None = None,
        stall_after: int | None = None,
        interrupt_after: int | None = None,
    ) -> None:
        self.clock, self.fps = clock, fps
        self.lost_after, self.stall_after, self.interrupt_after = lost_after, stall_after, interrupt_after
        self.reads = 0
        self.opened = 0
        self.closed = 0

    @property
    def info(self) -> CameraInfo | None:
        return None

    def open(self) -> CameraInfo:
        self.opened += 1
        return CameraInfo("scripted", 0, "fake", 1280, 720, self.fps)

    def read(self, timeout: float) -> CameraFrame | None:
        self.reads += 1
        if self.interrupt_after is not None and self.reads > self.interrupt_after:
            raise KeyboardInterrupt
        if self.lost_after is not None and self.reads > self.lost_after:
            raise CameraError("camera_lost", "the camera went away")
        if self.stall_after is not None and self.reads > self.stall_after:
            self.clock.t += timeout
            return None
        self.clock.t += 1.0 / self.fps
        return CameraFrame(self.reads, self.clock.t, np.zeros((2, 2, 3), dtype=np.uint8))

    def close(self) -> None:
        self.closed += 1


class ScriptedTracker:
    """Hands in view: the sides asked for, the same landmarks every frame (the file's contents are not the point)."""

    num_hands = 2
    infer_ms = 0.0

    def __init__(
        self, sides: tuple[str, ...] = ("left", "right"), sides_at: Callable[[float], tuple[str, ...]] | None = None
    ) -> None:
        self.sides, self.sides_at = sides, sides_at
        self.closed = 0
        self.calls = 0

    def process(self, image: np.ndarray, t: float) -> Frame:
        self.calls += 1
        sides = self.sides if self.sides_at is None else self.sides_at(t)
        return Frame(t, tuple(OBS[s] for s in sides), 1280, 720)

    def set_num_hands(self, n: int) -> None:
        pass

    def close(self) -> None:
        self.closed += 1


def parse(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    keytrace.add_arguments(parser)
    return parser.parse_args(list(argv))


class Session:
    """One run of ``keytrace.run`` over a scripted camera and tracker."""

    def __init__(
        self,
        tmp_path: Path,
        *argv: str,
        sides: tuple[str, ...] = ("left", "right"),
        sides_at: Callable[[float], tuple[str, ...]] | None = None,
        **camera: Any,
    ) -> None:
        self.clock = Clock()
        self.camera = ScriptedCamera(self.clock, **camera)
        self.tracker = ScriptedTracker(sides, sides_at)
        self.path = tmp_path / "rec.npz"
        self.out = io.StringIO()
        flags = ["--yes-record", "--countdown", "0", "--out", str(self.path)]
        self.args = parse(*flags, *argv)
        self.code: int | None = None

    def run(self, seed: int = 7) -> Session:
        self.code = keytrace.run(
            self.args,
            camera=self.camera,
            tracker=self.tracker,
            clock=self.clock,
            out=self.out,
            rng=random.Random(seed),
        )
        return self

    @property
    def text(self) -> str:
        return self.out.getvalue()

    def trace(self) -> Any:
        return read_trace(self.path)

    def files(self) -> set[str]:
        with np.load(self.path, allow_pickle=False) as npz:
            return set(npz.files)

    def prompts(self) -> np.ndarray:
        with np.load(self.path, allow_pickle=False) as npz:
            return np.asarray(npz[keytrace.PROMPTS_KEY])


# --------------------------------------------------------------------------------------------- the segment grammar


def test_a_segment_list_is_kind_colon_seconds_in_order():
    assert keytrace.parse_segments("type:30,rest:20,drill:60") == [("type", 30.0), ("rest", 20.0), ("drill", 60.0)]
    assert keytrace.parse_segments(" wave:2.5 , tap:1 ") == [("wave", 2.5), ("tap", 1.0)]
    assert keytrace.parse_segments("place:3") == [("place", 3.0)]


@pytest.mark.parametrize(
    "spec",
    [
        *("", ",", "drill", "drill:", ":30", "drill:0", "drill:-5", "drill:abc", "drill:nan"),
        *("drill:inf", "dance:10", "drill:60,", "DRILL:60", "drill:60:2"),
    ],
)
def test_a_bad_segment_list_is_refused_with_one_of_two_fixed_sentences(spec):
    sentences = set()
    for known_bad in ("dance:10", "drill:abc"):
        with pytest.raises(ValueError) as caught:
            keytrace.parse_segments(known_bad)
        sentences.add(str(caught.value))
    assert len(sentences) == 2
    with pytest.raises(ValueError) as caught:
        keytrace.parse_segments(spec)
    assert str(caught.value) in sentences  # whatever was typed is not echoed into the sentence


def test_the_kinds_are_the_design_s_plus_place_and_wave():
    assert set(keytrace.KINDS) == {"place", "type", "tap", "rest", "wave", "drill"}
    assert set(keytrace.KEY_KINDS) == {"type", "tap", "drill"}


def test_a_key_segment_after_a_rest_gets_a_place_in_front_of_it():
    """The plane of the replay is placed from the hands being still, so the recording says when they are."""
    plan = keytrace.build_plan(
        [("rest", 20.0), ("drill", 60.0), ("drill", 60.0), ("wave", 20.0), ("type", 30.0)], "air"
    )
    assert plan == [
        ("rest", 20.0), ("place", keytrace.PLACE_S), ("drill", 60.0), ("drill", 60.0), ("wave", 20.0),
        ("place", keytrace.PLACE_S), ("type", 30.0),
    ]  # fmt: skip


def test_a_plan_that_starts_with_keys_starts_with_a_place_and_an_explicit_place_is_kept_once():
    assert keytrace.build_plan([("drill", 10.0)], "air")[0] == ("place", keytrace.PLACE_S)
    assert keytrace.build_plan([("place", 2.0), ("drill", 10.0)], "air") == [("place", 2.0), ("drill", 10.0)]
    assert keytrace.build_plan([("rest", 5.0), ("wave", 5.0)], "air") == [("rest", 5.0), ("wave", 5.0)]


def test_the_default_plan_is_one_drill_for_air_and_one_typing_stretch_for_pinch():
    assert keytrace.default_segments("air", 120.0) == [("drill", 120.0)]
    assert keytrace.default_segments("pinch", 90.0) == [("type", 90.0)]


# -------------------------------------------------------------------------------------------------- the drill prompts


def prompt_rows(count: int, sides: tuple[str, ...], seed: int = 1):
    return keytrace.home_prompts(REVIEW, sides, count, random.Random(seed))


def test_the_drill_gives_each_finger_the_same_number_of_prompts():
    counts = Counter((p.side, p.finger) for p in prompt_rows(48, ("left", "right")))
    assert set(counts.values()) == {6} and len(counts) == 8
    one = Counter((p.side, p.finger) for p in prompt_rows(24, ("right",)))
    assert set(one.values()) == {6} and {side for side, _ in one} == {"right"}


def test_five_minutes_of_drill_are_balanced_over_the_eight_fingers():
    """L62: five segments of 60 s are 250 prompts, about half of them for index and middle."""
    rows = prompt_rows(250, ("left", "right"))
    counts = Counter((p.side, p.finger) for p in rows)
    assert (
        len(rows) == 250 and min(counts.values()) >= 30 and max(counts.values()) <= 35
    )  # five full decks and ten over
    assert 120 <= sum(1 for p in rows if p.finger in (0, 1)) <= 130


def test_no_finger_comes_twice_in_a_row_not_even_across_two_decks():
    for seed in range(20):
        rows = prompt_rows(250, ("left", "right"), seed)
        assert all((a.side, a.finger) != (b.side, b.finger) for a, b in pairwise(rows))


def test_every_second_prompt_is_displaced_and_none_is_a_reach_key():
    rows = prompt_rows(250, ("left", "right"))
    assert [p.displaced for p in rows] == [k % 2 == 1 for k in range(250)]
    assert not any(p.reach for p in rows)
    assert all(REVIEW.keys[p.key].kind == "char" for p in rows)


def test_a_home_prompt_is_the_key_under_the_fingers_home_position():
    rows = [p for p in prompt_rows(96, ("left", "right")) if not p.displaced]
    assert rows
    for p in rows:
        assert REVIEW.keys[p.key].en == HOME_CHARS[p.side][p.finger]


def test_a_displaced_prompt_is_three_to_four_units_aside_and_at_most_a_row_away():
    rows = [p for p in prompt_rows(240, ("left", "right"), 3) if p.displaced]
    assert rows
    low, high = AIR_DRILL_MOVE_U
    for p in rows:
        home = REVIEW.find(char=HOME_CHARS[p.side][p.finger])
        key = REVIEW.keys[p.key]
        du = abs((key.col + key.width / 2) - (home.col + home.width / 2))
        assert low - 0.5 <= du <= high + 0.5  # the draw is a point 3.0 to 4.0 away, the key is the cell holding it
        assert abs(key.row - home.row) <= max(abs(r) for r in AIR_DRILL_MOVE_ROWS)
        assert key.en.isalpha()


def test_the_same_seed_gives_the_same_prompts():
    one = [(p.side, p.finger, p.key) for p in prompt_rows(100, ("left", "right"), 5)]
    two = [(p.side, p.finger, p.key) for p in prompt_rows(100, ("left", "right"), 5)]
    three = [(p.side, p.finger, p.key) for p in prompt_rows(100, ("left", "right"), 6)]
    assert one == two and one != three


# ------------------------------------------------------------------------------------------------------- the arguments


def test_the_defaults_are_air_and_a_two_minute_drill():
    args = parse("--yes-record", "--out", "a.npz")
    assert args.press == "air" and args.seconds is None and args.segments is None
    assert args.countdown == keytrace.DEFAULT_COUNTDOWN_S == 5.0


def test_the_out_file_is_required_and_the_press_is_air_or_pinch():
    parser = argparse.ArgumentParser()
    keytrace.add_arguments(parser)
    with pytest.raises(SystemExit):
        parser.parse_args(["--yes-record"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--yes-record", "--out", "a.npz", "--press", "windows"])


# ----------------------------------------------------------------------------------------------------------- refusals


@pytest.fixture
def never_open(monkeypatch):
    """Any attempt to open a camera or load the model fails the test: a refusal must come first."""

    def boom(*a: Any, **k: Any) -> None:
        raise AssertionError("opened")

    monkeypatch.setattr("jarvis_hands.camera.create_camera", boom)
    monkeypatch.setattr("jarvis_hands.tracker.create_tracker", boom)


def run_refused(tmp_path: Path, *argv: str, flags: tuple[str, ...] = ("--yes-record",)) -> tuple[int, str]:
    out = io.StringIO()
    args = parse(*flags, "--out", str(tmp_path / "x.npz"), "--countdown", "0", *argv)
    return keytrace.run(args, out=out), out.getvalue()


def test_without_the_consent_flag_nothing_is_opened_and_nothing_is_written(tmp_path, never_open):
    code, text = run_refused(tmp_path, "--segments", "rest:5", flags=())
    assert code == keytrace.EXIT_REFUSED == 2
    assert "--yes-record" in text
    assert not (tmp_path / "x.npz").exists()


def test_a_drill_with_the_pinch_method_is_refused_before_the_camera_opens(tmp_path, never_open):
    code, text = run_refused(tmp_path, "--press", "pinch", "--segments", "drill:60")
    assert code == 2 and "drill" in text and "air" in text
    assert len(text.strip().splitlines()) == 1  # one line


def test_a_drill_among_other_segments_is_still_refused_with_pinch(tmp_path, never_open):
    code, _ = run_refused(tmp_path, "--press", "pinch", "--segments", "type:30,rest:20,drill:10")
    assert code == 2


@pytest.mark.parametrize(
    "argv",
    [
        ("--segments", "dance:10"),
        ("--segments", ""),
        ("--segments", "drill:700"),
        ("--segments", "drill:300,drill:300,drill:100"),
        ("--seconds", "0"),
        ("--seconds", "-3"),
        ("--seconds", "601"),
        ("--seconds", "nan"),
        ("--seconds", "60", "--segments", "drill:60"),
    ],
)
def test_a_plan_that_makes_no_sense_or_is_too_long_is_refused_before_the_camera_opens(tmp_path, never_open, argv):
    code, text = run_refused(tmp_path, *argv)
    assert code == 2 and text.strip()


@pytest.mark.parametrize("countdown", ["-1", "61", "nan", "inf"])
def test_a_countdown_outside_zero_to_sixty_seconds_is_refused_before_the_camera_opens(tmp_path, never_open, countdown):
    code, text = run_refused(tmp_path, "--segments", "rest:5", "--countdown", countdown)
    assert code == 2 and "--countdown" in text


def test_an_infinite_number_of_seconds_is_refused_as_not_a_number_above_zero(tmp_path, never_open):
    code, text = run_refused(tmp_path, "--seconds", "inf")
    assert code == 2 and "above 0" in text


def test_the_recording_may_not_outlast_the_trace_limit_counting_the_places_it_adds(tmp_path, never_open):
    # 590 s of segments is allowed alone, but the place in front of the drill makes it 593 s: still under 600 s
    assert TRACE_MAX_S == 600.0
    code, _ = run_refused(tmp_path, "--segments", "drill:598")
    assert code == 2  # 598 + 3 > 600


def test_an_out_name_that_is_not_npz_or_already_exists_is_refused(tmp_path, never_open):
    out = io.StringIO()
    args = parse("--yes-record", "--out", str(tmp_path / "x.txt"), "--segments", "rest:5")
    assert keytrace.run(args, out=out) == 2
    existing = tmp_path / "there.npz"
    existing.write_bytes(b"keep me")
    args = parse("--yes-record", "--out", str(existing), "--segments", "rest:5")
    assert keytrace.run(args, out=io.StringIO()) == 2
    assert existing.read_bytes() == b"keep me"


def test_a_missing_model_is_said_before_the_camera_is_opened(tmp_path, monkeypatch, never_open):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "empty"))
    out = io.StringIO()
    args = parse("--yes-record", "--out", str(tmp_path / "x.npz"), "--segments", "rest:5", "--countdown", "0")
    assert keytrace.run(args, out=out) == keytrace.EXIT_FAILED
    assert "model" in out.getvalue()
    assert not (tmp_path / "x.npz").exists()


# ---------------------------------------------------------------------------------------------- the recorded file


def test_a_drill_records_a_place_then_the_drill_with_the_press_method_and_version_2(tmp_path):
    s = Session(tmp_path, "--segments", "drill:12").run()
    assert s.code == 0, s.text
    data = s.trace()
    assert data.press == "air" and data.version == 2
    assert [(a, b, k) for a, b, k in data.segments] == [(0, 90, "place"), (90, 90 + 360, "drill")]
    assert data.t[1] - data.t[0] == pytest.approx(1 / FPS)
    assert data.present.all() and data.width == 1280 and data.height == 720


def test_the_file_holds_the_pinned_arrays_and_the_prompt_table_and_nothing_else(tmp_path):
    s = Session(tmp_path, "--segments", "drill:6").run()
    assert s.files() == ALLOWED_ARRAYS
    with np.load(s.path, allow_pickle=False) as npz:  # no pickled object anywhere
        assert all(npz[name].dtype != object for name in npz.files)


def test_a_recording_without_a_drill_has_no_prompt_table(tmp_path):
    s = Session(tmp_path, "--segments", "rest:3,wave:3").run()
    assert s.code == 0
    assert s.files() == ALLOWED_ARRAYS - {"drill_prompts"}


def test_the_file_is_small_a_frame_costs_a_few_hundred_bytes_before_compression(tmp_path):
    s = Session(tmp_path, "--segments", "rest:10").run()
    with np.load(s.path, allow_pickle=False) as npz:
        raw = sum(npz[name].nbytes for name in npz.files)
        frames = len(npz["t"])
    assert frames == 300 and raw / frames < 700


def test_the_pinch_press_is_stored_and_a_pinch_recording_has_type_rest_and_tap(tmp_path):
    s = Session(tmp_path, "--press", "pinch", "--segments", "type:2,rest:2,tap:2").run()
    assert s.code == 0, s.text
    data = s.trace()
    assert data.press == "pinch" and data.version == 2
    assert [k for _, _, k in data.segments] == ["place", "type", "rest", "place", "tap"]
    assert not any(k == "drill" for _, _, k in data.segments)
    assert "drill_prompts" not in s.files()


def test_targets_are_minus_one_outside_a_drill_and_the_prompted_key_inside(tmp_path):
    s = Session(tmp_path, "--segments", "rest:2,drill:6").run()
    data = s.trace()
    kinds = [data.kind_at(i) for i in range(len(data))]
    assert all(t == -1 for t, k in zip(data.targets, kinds, strict=True) if k != "drill")
    prompts = s.prompts()
    assert prompts.shape == (5, 5)  # 6 s of drill is five prompts
    for k, (frame, side, finger, key, displaced) in enumerate(prompts.tolist()):
        end = prompts[k + 1][0] if k + 1 < len(prompts) else data.segments[-1][1]
        assert all(data.targets[frame:end] == key)
        assert REVIEW.keys[key].kind == "char" and side in (0, 1) and 0 <= finger <= 3 and displaced in (0, 1)


def test_the_prompts_come_every_1_2_seconds_of_frame_time_from_the_drill_start(tmp_path):
    s = Session(tmp_path, "--segments", "drill:30").run()
    data = s.trace()
    prompts = s.prompts()
    start = data.t[data.segments[-1][0]]
    assert len(prompts) == 25  # 30 / 1.2
    for k, row in enumerate(prompts.tolist()):
        assert start + k * AIR_DRILL_GAP_S - 1e-6 <= data.t[row[0]] < start + k * AIR_DRILL_GAP_S + 1 / FPS


def test_the_tail_of_a_drill_that_is_not_a_whole_number_of_prompts_has_no_target(tmp_path):
    s = Session(tmp_path, "--segments", "drill:5").run()  # 4 prompts (4.8 s) and 0.2 s over
    data = s.trace()
    assert len(s.prompts()) == 4
    assert data.targets[-1] == -1 and data.targets[-6:].tolist() == [-1] * 6


def test_the_hands_in_view_when_the_drill_begins_decide_whose_fingers_are_named(tmp_path):
    s = Session(tmp_path, "--segments", "drill:24", sides=("right",)).run()
    assert set(s.prompts()[:, 1].tolist()) == {1}  # only the right hand
    both = Session(tmp_path / "b", "--segments", "drill:24").run()
    assert set(both.prompts()[:, 1].tolist()) == {0, 1}


def test_with_no_hand_in_view_both_hands_are_named(tmp_path):
    s = Session(tmp_path, "--segments", "drill:24", sides=()).run()
    assert s.code == 0 and set(s.prompts()[:, 1].tolist()) == {0, 1}


def test_a_hand_that_was_in_view_for_half_the_frames_before_a_drill_is_named(tmp_path):
    """Half is enough (the right hand shows on every second frame): a hand that flickers is still a hand to drill."""
    flicker = lambda t: ("left", "right") if round((t - START) * 4) % 2 == 1 else ("left",)  # noqa: E731
    s = Session(tmp_path, "--segments", "place:1,drill:24", sides_at=flicker, fps=4.0).run()
    assert set(s.prompts()[:, 1].tolist()) == {0, 1}


def test_a_hand_that_left_before_the_last_second_and_a_half_is_not_named(tmp_path):
    """Both hands for three of the five seconds of place, then the left only: the right hand is gone."""
    gone = lambda t: ("left", "right") if t - START < 3.0 else ("left",)  # noqa: E731
    s = Session(tmp_path, "--segments", "place:5,drill:24", sides_at=gone).run()
    assert set(s.prompts()[:, 1].tolist()) == {0}


def test_the_hands_are_chosen_again_for_every_drill_and_the_stream_follows_them(tmp_path):
    """Both hands in the first drill, the left alone in the second: its prompts name the left hand only."""
    gone = lambda t: ("left", "right") if t - START < 9.0 else ("left",)  # noqa: E731
    s = Session(tmp_path, "--segments", "drill:6,rest:3,drill:12", sides_at=gone).run()
    data = s.trace()
    second = data.segments[-1][0]
    rows = s.prompts()
    assert set(rows[rows[:, 0] < second][:, 1].tolist()) == {0, 1}
    assert set(rows[rows[:, 0] >= second][:, 1].tolist()) == {0}


def test_a_drill_of_whole_prompt_gaps_gets_all_of_them_even_when_its_length_is_a_float_product(tmp_path):
    s = Session(tmp_path, "--segments", f"drill:{3 * AIR_DRILL_GAP_S!r}").run()  # 3.5999999999999996 s
    assert len(s.prompts()) == 3


def test_a_seed_gives_a_reproducible_recording(tmp_path):
    one = Session(tmp_path / "a", "--segments", "drill:12").run(seed=4)
    two = Session(tmp_path / "b", "--segments", "drill:12").run(seed=4)
    other = Session(tmp_path / "c", "--segments", "drill:12").run(seed=5)
    assert np.array_equal(one.prompts(), two.prompts()) and not np.array_equal(one.prompts(), other.prompts())


def test_the_prompt_stream_goes_on_across_drill_segments(tmp_path):
    """Five segments of 60 s balance over the fingers as one long drill, not as five separate ones."""
    s = Session(tmp_path, "--segments", "drill:30,drill:30,drill:30,drill:30,drill:30").run()
    counts = Counter(tuple(r[1:3]) for r in s.prompts().tolist())
    assert sum(counts.values()) == 125 and min(counts.values()) >= 14 and max(counts.values()) <= 17


def test_seconds_alone_records_one_stretch_of_the_natural_kind(tmp_path):
    s = Session(tmp_path, "--seconds", "6").run()
    assert [k for _, _, k in s.trace().segments] == ["place", "drill"]
    p = Session(tmp_path / "p", "--press", "pinch", "--seconds", "6").run()
    assert [k for _, _, k in p.trace().segments] == ["place", "type"]


# ------------------------------------------------------------------------------------------------------- the console


def test_the_console_names_each_segment_and_each_prompt_with_fixed_words(tmp_path):
    s = Session(tmp_path, "--segments", "drill:6,rest:2,wave:2").run()
    lines = s.text.splitlines()
    assert any("still" in line.lower() for line in lines)  # the place
    assert any(line.lower().startswith("rest") for line in lines) and any("wave" in line.lower() for line in lines)
    taps = [line for line in lines if line.startswith("Tap:")]
    assert len(taps) == 5
    names = "|".join(FINGER_NAMES)
    assert all(re.fullmatch(rf"Tap: (left|right) ({names})(, key [a-z])?", line) for line in taps)
    # the second prompt is displaced: it says which key; the first is a home key and does not
    assert ", key " not in taps[0] and ", key " in taps[1]


def test_the_countdown_is_printed_and_not_recorded(tmp_path):
    s = Session(tmp_path, "--segments", "rest:2")
    s.args.countdown = 3.0
    s.run()
    lines = s.text.splitlines()
    assert [line.strip() for line in lines if line.strip() in ("3", "2", "1")] == ["3", "2", "1"]
    data = s.trace()
    assert data.t[0] >= START + 3.0 and len(data) == 60


def test_two_typing_stretches_show_two_different_phrases(tmp_path):
    s = Session(tmp_path, "--press", "pinch", "--segments", "type:2,rest:1,type:2").run()
    shown = [line for line in s.text.splitlines() if line.lstrip().startswith("Type this")]
    assert len(shown) == 2 and shown[0] != shown[1]


def test_a_type_segment_shows_one_of_the_practice_phrases_and_nothing_the_user_typed(tmp_path):
    s = Session(tmp_path, "--press", "pinch", "--segments", "type:2").run()
    shown = [p for p in PHRASES["en"] if p in s.text]
    assert shown, s.text


def test_the_success_line_names_the_file_and_the_replay_command(tmp_path):
    s = Session(tmp_path, "--segments", "rest:2").run()
    assert str(s.path) in s.text and "keyreplay" in s.text


# --------------------------------------------------------------------------------------------- the ways it can end


def test_camera_and_tracker_are_closed_after_a_good_run(tmp_path):
    s = Session(tmp_path, "--segments", "rest:2").run()
    assert s.camera.opened == 1 and s.camera.closed == 1 and s.tracker.closed == 1


def test_ctrl_c_saves_what_was_recorded_and_says_so(tmp_path):
    s = Session(tmp_path, "--segments", "rest:20", interrupt_after=150).run()
    assert s.code == keytrace.EXIT_FAILED
    assert "Stopped" in s.text and "saved" in s.text.lower()
    assert len(s.trace()) == 150  # no countdown: every frame read is a recorded one
    assert s.camera.closed == 1 and s.tracker.closed == 1


def test_ctrl_c_during_the_countdown_saves_nothing(tmp_path):
    s = Session(tmp_path, "--segments", "rest:20", interrupt_after=20)
    s.args.countdown = 5.0
    s.run()
    assert s.code == keytrace.EXIT_FAILED and not s.path.exists()
    assert s.camera.closed == 1 and s.tracker.closed == 1


def test_a_camera_that_is_lost_mid_recording_saves_the_part_and_says_why(tmp_path):
    s = Session(tmp_path, "--segments", "rest:20", lost_after=100).run()
    assert s.code == keytrace.EXIT_FAILED
    assert "camera" in s.text.lower() and len(s.trace()) == 100
    assert s.camera.closed == 1 and s.tracker.closed == 1


def test_a_camera_that_stops_giving_frames_ends_the_recording_after_the_stall_limit(tmp_path):
    s = Session(tmp_path, "--segments", "rest:60", stall_after=60).run()
    assert s.code == keytrace.EXIT_FAILED
    assert len(s.trace()) == 60
    waited = s.clock.t - (START + 60 / FPS)
    assert 4.9 <= waited <= 6.0  # five seconds of silence, read in half-second steps


def test_a_camera_that_never_gives_a_frame_saves_nothing(tmp_path):
    s = Session(tmp_path, "--segments", "rest:10", stall_after=0).run()
    assert s.code == keytrace.EXIT_FAILED and not s.path.exists()


def test_a_camera_that_cannot_open_is_said_and_the_tracker_is_closed(tmp_path):
    s = Session(tmp_path, "--segments", "rest:5")

    def fail() -> CameraInfo:
        raise CameraError("camera_in_use", "the camera is in use", "Close the other program.")

    s.camera.open = fail  # type: ignore[method-assign]
    s.run()
    assert s.code == keytrace.EXIT_FAILED and "in use" in s.text and "Close the other program" in s.text
    assert s.tracker.closed == 1 and not s.path.exists()


def test_the_file_is_written_atomically_no_part_file_stays_and_a_failed_write_leaves_nothing(tmp_path, monkeypatch):
    Session(tmp_path, "--segments", "drill:6").run()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["rec.npz"]

    real = os.replace

    def broken(src: Any, dst: Any) -> None:
        if Path(dst).name == "rec.npz":  # the staging copy goes through, the file itself does not
            raise OSError("disk full")
        real(src, dst)

    other = Session(tmp_path / "w", "--segments", "drill:6")
    other.path.parent.mkdir()
    monkeypatch.setattr("jarvis_hands.keyboard.keytrace.os.replace", broken)
    other.run()
    assert other.code == keytrace.EXIT_FAILED and not other.path.exists()
    assert list(other.path.parent.iterdir()) == []  # no .tmp, no .part
    assert "could not" in other.text.lower()


def test_a_failed_second_step_does_not_clobber_a_good_file_that_appeared_meanwhile(tmp_path, monkeypatch):
    s = Session(tmp_path, "--segments", "drill:6")

    def appear(*a: Any, **k: Any) -> None:
        s.path.write_bytes(b"someone else's")
        raise OSError("disk full")

    monkeypatch.setattr("jarvis_hands.keyboard.keytrace.os.replace", appear)
    s.run()
    assert s.path.read_bytes() == b"someone else's"


# ------------------------------------------------------------------------------------------------ the real devices


def test_the_camera_and_the_tracker_are_made_from_the_arguments_and_the_model_in_the_data_dir(tmp_path, monkeypatch):
    """With no devices handed in, run opens the package's own: the named camera and a two-hand tracker on the model."""
    s = Session(tmp_path, "--segments", "rest:2", "--camera", "Logi", "--data-dir", str(tmp_path / "data"))
    seen: dict[str, Any] = {}

    def make_camera(spec: Any) -> ScriptedCamera:
        seen["camera"] = spec
        return s.camera

    def make_tracker(model: Path, num_hands: int) -> ScriptedTracker:
        seen["tracker"] = (model, num_hands)
        return s.tracker

    monkeypatch.setattr("jarvis_hands.models.is_installed", lambda data_dir: True)
    monkeypatch.setattr("jarvis_hands.camera.create_camera", make_camera)
    monkeypatch.setattr("jarvis_hands.tracker.create_tracker", make_tracker)
    code = keytrace.run(s.args, clock=s.clock, out=s.out, rng=random.Random(1))
    assert code == 0, s.text
    assert seen["camera"] == "Logi"
    assert seen["tracker"] == (models.model_path(tmp_path / "data"), 2)
    assert s.camera.closed == 1 and s.tracker.closed == 1


def test_a_tracker_that_cannot_start_is_said_and_the_camera_is_never_opened(tmp_path, monkeypatch):
    s = Session(tmp_path, "--segments", "rest:2", "--data-dir", str(tmp_path / "data"))

    def make_tracker(model: Path, num_hands: int) -> ScriptedTracker:
        raise TrackerError("tracker_failed", "the hand model would not load", "Run setup again.")

    monkeypatch.setattr("jarvis_hands.models.is_installed", lambda data_dir: True)
    monkeypatch.setattr("jarvis_hands.tracker.create_tracker", make_tracker)
    monkeypatch.setattr("jarvis_hands.camera.create_camera", lambda spec: s.camera)
    code = keytrace.run(s.args, clock=s.clock, out=s.out, rng=random.Random(1))
    assert code == keytrace.EXIT_FAILED
    assert "would not load" in s.text and "Run setup again" in s.text
    assert s.camera.opened == 0 and not s.path.exists()


# ------------------------------------------------------------------------------------------------------- the promise


def test_the_module_says_what_is_recorded_and_what_is_not():
    doc = (keytrace.__doc__ or "").lower()
    for phrase in ("landmark", "no pixel", "typed", "local", "prompt"):
        assert phrase in doc, phrase


def test_the_recorder_imports_no_opencv_and_prints_only_through_its_stream():
    source = Path(keytrace.__file__).read_text(encoding="utf-8")
    assert "import cv2" not in source and "mediapipe" not in source
    prints = [line for line in source.splitlines() if "print(" in line]
    assert prints and all("file=stream" in line for line in prints)
