"""Practice (DESIGN-KEYBOARD.md 5.7, 2.12.6, 4.4) and the files it leaves behind (2.12.10, 3.14).

X36 the drill, X37 the air marker, X38 (the script half), X51 (the aim spread), the pinch script and its scoring, the
markers of both methods, the tap log, the landmark trace and the clean-up of old traces. Everything is driven by calls
with the times as arguments: no hands, no camera, no sleeping.
"""

from __future__ import annotations

import itertools
import json
import os
import random
import statistics
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jarvis_hands.keyboard import limits, practice
from jarvis_hands.keyboard.layout import layout_for
from jarvis_hands.keyboard.practice import PHRASES, PracticeResult, PracticeScript, make_drill
from jarvis_hands.keyboard.types import Side

DIRECT = layout_for("direct")
REVIEW = layout_for("review")
BOTH: tuple[Side, ...] = ("left", "right")
FINGERS = ("index", "middle", "ring", "pinky")
DT = 1 / 30


def key_of(char: str, layout=REVIEW, lang: str = "en") -> int:
    return layout.find(char=char, lang=lang).index  # type: ignore[arg-type]


class Run:
    """Drives a ``PracticeScript`` at 30 fps with a hand in view; presses are scripted by the test."""

    def __init__(self, script: PracticeScript, t0: float = 0.0) -> None:
        self.script = script
        self.t = t0
        self.in_view = True
        self.tick()

    def tick(self) -> None:
        self.script.tick(self.t, self.in_view)

    def advance(self, seconds: float) -> None:
        for _ in range(round(seconds / DT)):
            self.t += DT
            self.tick()

    def until(self, segment: str, limit_s: float = 400.0) -> None:
        n = 0
        while self.script.segment != segment:
            self.t += DT
            self.tick()
            n += 1
            assert n * DT < limit_s, f"never reached {segment}"

    def event(self, side: Side = "right", finger: int = 0, key: int | None = None, uv=(0.0, 0.0)) -> str:
        return self.script.event(self.t, side, finger, uv[0], uv[1], key)

    def press(self, key: int | None, side: Side = "right", finger: int = 0) -> str:
        return self.script.press(self.t, side, finger, key)

    def type_phrase(self) -> None:
        """Presses the target until the phrase changes."""
        n = self.script.phrase_index
        while self.script.segment == "phrase" and self.script.phrase_index == n:
            target = self.script.target_key
            assert target is not None
            self.press(target)
            self.t += DT
            self.tick()


def pinch_script(**kw) -> PracticeScript:
    kw = {"press": "pinch", "lang": "en", "layout": DIRECT, "sides": BOTH, **kw}
    return PracticeScript(**kw)


def air_script(**kw) -> PracticeScript:
    kw = {"press": "air", "lang": "en", "layout": REVIEW, "sides": BOTH, "seed": 1, **kw}
    return PracticeScript(**kw)


# ------------------------------------------------------------------------------------------------------ the phrases


def test_the_phrases_are_the_six_of_the_design_in_both_languages() -> None:
    assert PHRASES["en"] == (
        "the quick brown fox",
        "jumps over the lazy dog",
        "hello world",
        "yes please",
        "go ahead, thanks.",
        "what is this?",
    )
    assert PHRASES["he"] == ("שלום עולם", "תודה רבה", "כן בבקשה", "מה נשמע", "בוקר טוב", "לילה טוב")


@pytest.mark.parametrize("layout", [DIRECT, REVIEW], ids=["direct", "review"])
@pytest.mark.parametrize("lang", ["en", "he"])
def test_every_character_of_every_phrase_is_a_key_of_both_layouts(layout, lang) -> None:
    for phrase in PHRASES[lang]:
        for char in phrase:
            layout.find(char=char, lang=lang)  # a KeyError would be the failure


def test_the_phrases_hold_no_character_the_compose_alphabet_refuses() -> None:
    from jarvis_hands.desktop.keys import COMPOSE_CHARS

    for phrases in PHRASES.values():
        assert set("".join(phrases)) <= COMPOSE_CHARS


# ----------------------------------------------------------------------------------------------------- X36: the drill


def one_hand_drill(side: Side, seed: int = 1):
    return make_drill(REVIEW, (side,), random.Random(seed))


def test_x36_two_hands_sixty_prompts_six_per_finger_and_three_per_reach_key() -> None:
    drill = make_drill(REVIEW, BOTH, random.Random(1))
    home = [p for p in drill if not p.reach]
    reach = [p for p in drill if p.reach]
    assert len(home) == limits.AIR_DRILL_PER_FINGER * 8 == 48
    assert len(reach) == limits.AIR_DRILL_PER_REACH_KEY * len(limits.AIR_DRILL_REACH_KEYS) == 12
    for side in BOTH:
        for finger in range(4):
            assert sum(1 for p in home if (p.side, p.finger) == (side, finger)) == limits.AIR_DRILL_PER_FINGER
    for kind, side, finger in limits.AIR_DRILL_REACH_KEYS:
        index = REVIEW.find(kind=kind).index
        mine = [p for p in reach if p.key == index]
        assert len(mine) == limits.AIR_DRILL_PER_REACH_KEY
        assert {(p.side, FINGERS[p.finger]) for p in mine} == {(side, finger)}


@pytest.mark.parametrize(("side", "keys"), [("right", {"backspace", "insert"}), ("left", {"clear", "enter"})])
def test_x36_one_hand_is_half_the_prompts_and_only_its_own_reach_keys(side, keys) -> None:
    drill = one_hand_drill(side)
    assert len(drill) == 4 * limits.AIR_DRILL_PER_FINGER + 2 * limits.AIR_DRILL_PER_REACH_KEY
    assert {p.side for p in drill} == {side}
    assert {p.key for p in drill if p.reach} == {REVIEW.find(kind=k).index for k in keys}
    assert sum(1 for p in drill if not p.reach) == 4 * limits.AIR_DRILL_PER_FINGER


@pytest.mark.parametrize("seed", range(12))
def test_x36_no_two_prompts_in_a_row_for_the_same_finger(seed) -> None:
    for sides in (BOTH, ("left",), ("right",)):
        drill = make_drill(REVIEW, sides, random.Random(seed))
        for a, b in itertools.pairwise(drill):
            assert (a.side, a.finger) != (b.side, b.finger)


def test_x36_the_same_seed_gives_the_same_sequence_and_another_seed_another() -> None:
    a = make_drill(REVIEW, BOTH, random.Random(5))
    assert a == make_drill(REVIEW, BOTH, random.Random(5))
    assert a != make_drill(REVIEW, BOTH, random.Random(6))


def home_char(side: Side, finger: int) -> str:
    return {"left": "fdsa", "right": "jkl'"}[side][finger]


@pytest.mark.parametrize("seed", range(6))
def test_x36_every_second_home_row_prompt_is_displaced_by_three_to_four_units_and_a_row_at_most(seed) -> None:
    drill = make_drill(REVIEW, BOTH, random.Random(seed))
    home = [p for p in drill if not p.reach]
    for n, p in enumerate(home, start=1):
        own = REVIEW.find(char=home_char(p.side, p.finger))
        assert p.displaced == (n % limits.AIR_DRILL_MOVE_EVERY == 0)
        if not p.displaced:
            assert p.key == own.index
            continue
        key = REVIEW.keys[p.key]
        assert key.kind == "char" and key.en.isalpha()  # a letter key
        du = abs((key.col + key.width / 2) - (own.col + own.width / 2))
        # the point is 3.0 to 4.0 units from the home key's centre; the key that holds it (row 2 is offset by half a
        # key) has its centre within half a key of that
        assert 2.5 <= du <= 4.5
        assert abs(key.row - own.row) <= 1


def test_x36_the_displacements_use_both_directions_and_all_three_rows() -> None:
    rows: set[int] = set()
    signs: set[int] = set()
    for seed in range(8):
        for p in make_drill(REVIEW, BOTH, random.Random(seed)):
            if not p.displaced:
                continue
            own = REVIEW.find(char=home_char(p.side, p.finger))
            key = REVIEW.keys[p.key]
            rows.add(key.row - own.row)
            signs.add(1 if key.col > own.col else -1)
    assert rows == {-1, 0, 1}
    assert signs == {-1, 1}


def test_x36_a_displaced_prompt_keeps_its_finger_and_never_lands_on_a_special_key() -> None:
    for seed in range(8):
        for p in make_drill(REVIEW, BOTH, random.Random(seed)):
            if p.displaced:
                assert REVIEW.keys[p.key].kind == "char"


def test_x36_reach_prompts_name_the_key_and_are_never_displaced() -> None:
    for p in make_drill(REVIEW, BOTH, random.Random(2)):
        if p.reach:
            assert not p.displaced
            assert REVIEW.keys[p.key].kind in {"backspace", "insert", "clear", "enter"}


def test_x36_the_strip_text_names_the_finger_and_for_a_reach_prompt_the_key() -> None:
    drill = make_drill(REVIEW, BOTH, random.Random(3))
    texts = {p.text for p in drill}
    assert "Tap: right pinky, Insert" in texts
    assert "Tap: right index, Bksp" in texts
    assert "Tap: left pinky, Clear" in texts
    assert "Tap: left ring, Send" in texts
    assert all(t.startswith("Tap: ") for t in texts)
    assert all("," not in p.text for p in drill if not p.reach)
    assert {"Tap: left index", "Tap: right ring"} <= texts


# ------------------------------------------------------------------------------------------ the pinch script, in order


def test_the_pinch_script_starts_with_the_first_phrase_and_lights_its_first_key() -> None:
    run = Run(pinch_script())
    s = run.script
    assert s.segment == "phrase"
    assert s.prompt == PHRASES["en"][0]
    assert s.target_key == key_of("t", DIRECT)
    assert s.named is None
    assert s.phrase_index == 0


def test_a_press_on_the_target_advances_and_a_wrong_key_does_not() -> None:
    run = Run(pinch_script())
    s = run.script
    assert run.press(key_of("h", DIRECT)) == "miss"
    assert s.target_key == key_of("t", DIRECT)
    assert run.press(key_of("t", DIRECT)) == "hit"
    assert s.target_key == key_of("h", DIRECT)
    assert s.echo == "t"


def test_a_key_that_is_off_the_keyboard_is_a_miss_that_does_not_advance() -> None:
    run = Run(pinch_script())
    assert run.press(None) == "miss"
    assert run.script.target_key == key_of("t", DIRECT)
    assert run.script.result(fps=30.0, noise=0.0, level="ok").presses == 1


def test_eight_seconds_without_progress_is_a_miss_and_advances() -> None:
    run = Run(pinch_script())
    run.advance(limits_char_timeout() - 0.1)
    assert run.script.target_key == key_of("t", DIRECT)
    run.advance(0.2)
    assert run.script.target_key == key_of("h", DIRECT)
    result = run.script.result(fps=30.0, noise=0.0, level="ok")
    assert (result.presses, result.correct) == (1, 0)


def limits_char_timeout() -> float:
    return practice.CHAR_TIMEOUT_S


def test_the_timeout_runs_from_the_last_progress_not_from_the_last_press() -> None:
    run = Run(pinch_script())
    run.advance(6.0)
    run.press(key_of("x", DIRECT))  # a miss does not restart the clock
    run.advance(2.1)
    assert run.script.target_key == key_of("h", DIRECT)


def test_a_hit_restarts_the_timeout_for_the_next_character() -> None:
    run = Run(pinch_script())
    run.advance(7.0)
    run.press(key_of("t", DIRECT))
    run.advance(7.0)
    assert run.script.target_key == key_of("h", DIRECT)
    run.advance(1.1)
    assert run.script.target_key == key_of("e", DIRECT)


def test_a_finished_phrase_shows_the_next_one() -> None:
    run = Run(pinch_script())
    run.type_phrase()
    assert run.script.phrase_index == 1
    assert run.script.prompt == PHRASES["en"][1]
    assert run.script.echo == ""


def test_the_echo_is_the_correct_characters_of_the_current_phrase_so_far() -> None:
    run = Run(pinch_script())
    for char in "the q":
        run.press(key_of(char, DIRECT))
    assert run.script.echo == "the q"


def test_three_phrases_then_a_rest_of_twenty_seconds_with_a_hand_in_view() -> None:
    run = Run(pinch_script())
    for _ in range(3):
        run.type_phrase()
    s = run.script
    assert s.segment == "rest"
    assert s.prompt == "Rest: do not press. Wave, open and close your hands."
    assert s.target_key is None
    run.advance(limits.PRACTICE_MIN_REST_S - 0.5)
    assert s.segment == "rest"
    assert 0.9 < s.progress < 1.0
    run.advance(1.0)
    assert s.segment == "phrase"
    assert s.phrase_index == 3


def test_the_rest_counts_only_the_seconds_with_a_hand_in_view() -> None:
    run = Run(pinch_script())
    for _ in range(3):
        run.type_phrase()
    run.in_view = False
    run.advance(60.0)
    assert run.script.segment == "rest"
    assert run.script.progress < 0.01  # the frame that ended the phrase was in view
    run.in_view = True
    run.advance(10.0)
    assert run.script.segment == "rest"
    run.in_view = False
    run.advance(30.0)
    run.in_view = True
    run.advance(10.5)
    assert run.script.segment == "phrase"
    assert run.script.result(fps=30.0, noise=0.0, level="ok").rest_s == pytest.approx(20.0, abs=0.1)


def test_a_gap_in_the_frames_is_not_counted_as_time_in_view() -> None:
    run = Run(pinch_script())
    for _ in range(3):
        run.type_phrase()
    run.t += 5.0  # the camera stalled
    run.tick()
    assert run.script.progress < 0.02


def test_an_event_during_a_rest_is_a_phantom_and_nothing_else() -> None:
    run = Run(pinch_script())
    for _ in range(3):
        run.type_phrase()
    assert run.event() == "phantom"
    assert run.event() == "phantom"
    r = run.script.result(fps=30.0, noise=0.0, level="ok")
    assert r.phantoms == 2
    assert (r.presses, r.correct) == (len(r_chars_of(0, 3)), len(r_chars_of(0, 3)))
    assert run.script.counts["practice_phantom"] == 2


def r_chars_of(a: int, b: int) -> str:
    return "".join(PHRASES["en"][a:b])


def test_a_press_the_queue_delivers_late_is_not_a_phantom_when_the_rest_began_after_it_fired() -> None:
    # Phantoms are decided at the event, scoring at emission: an emission in a rest is ignored, not scored.
    run = Run(pinch_script())
    for _ in range(3):
        run.type_phrase()
    assert run.press(key_of("a", DIRECT)) == ""
    r = run.script.result(fps=30.0, noise=0.0, level="ok")
    assert r.presses == len(r_chars_of(0, 3))


def test_two_groups_and_two_rests_complete_the_pinch_script() -> None:
    run = Run(pinch_script())
    for _ in range(3):
        run.type_phrase()
    run.until("phrase")
    for _ in range(3):
        run.type_phrase()
    assert run.script.segment == "rest"
    assert not run.script.done
    run.until("done")
    assert run.script.done
    r = run.script.result(fps=30.0, noise=0.0, level="ok")
    assert r.completed
    assert r.rest_s == pytest.approx(2 * limits.PRACTICE_MIN_REST_S, abs=0.1)
    assert r.hit_rate == 1.0
    assert (r.presses, r.correct) == (len(r_chars_of(0, 6)), len(r_chars_of(0, 6)))
    assert r.talk_s == 0.0 and r.drill_prompts == 0


def test_the_pinch_result_is_not_complete_before_the_end() -> None:
    run = Run(pinch_script())
    run.type_phrase()
    assert not run.script.result(fps=30.0, noise=0.0, level="ok").completed


def test_a_phrase_of_misses_alone_still_finishes_by_timeouts() -> None:
    run = Run(pinch_script(phrases=["ab"]))
    run.advance(2 * practice.CHAR_TIMEOUT_S + 0.5)
    assert run.script.segment == "rest"
    r = run.script.result(fps=30.0, noise=0.0, level="ok")
    assert (r.presses, r.correct, r.hit_rate) == (2, 0, 0.0)


def test_the_hit_rate_is_correct_over_presses_with_the_timeouts_in_the_denominator() -> None:
    run = Run(pinch_script(phrases=["ab"]))
    run.press(key_of("a", DIRECT))
    run.press(key_of("q", DIRECT))
    run.advance(practice.CHAR_TIMEOUT_S + 0.2)  # b times out
    r = run.script.result(fps=30.0, noise=0.0, level="ok")
    assert (r.presses, r.correct) == (3, 1)
    assert r.hit_rate == pytest.approx(1 / 3)


def test_per_finger_counts_presses_and_hits_by_hand_and_finger_without_key_identity() -> None:
    run = Run(pinch_script(phrases=["ab"]))
    run.press(key_of("a", DIRECT), "left", 3)
    run.press(key_of("q", DIRECT), "left", 3)
    run.press(key_of("b", DIRECT), "left", 0)
    r = run.script.result(fps=30.0, noise=0.0, level="ok")
    assert r.per_finger == {"left.pinky": (2, 1), "left.index": (1, 1)}


def test_custom_phrases_are_split_into_groups_of_three() -> None:
    run = Run(pinch_script(phrases=["a", "b", "c", "d"]))
    for _ in range(3):
        run.type_phrase()
    assert run.script.segment == "rest"
    run.until("phrase")
    run.type_phrase()
    assert run.script.segment == "rest"


def test_a_phrase_with_a_character_the_layout_cannot_type_is_refused_with_a_fixed_text() -> None:
    with pytest.raises(ValueError, match=r"^a practice phrase uses a character the keyboard does not have$") as info:
        pinch_script(phrases=["aéz"])
    assert "é" not in str(info.value)


def test_hebrew_phrases_target_the_keys_of_the_hebrew_letters() -> None:
    run = Run(pinch_script(lang="he"))
    assert run.script.prompt == "שלום עולם"
    assert run.script.target_key == key_of("a", DIRECT)  # ש is on the English a
    run.press(key_of("a", DIRECT))
    assert run.script.echo == "ש"


def test_the_trace_kind_of_every_segment() -> None:
    run = Run(pinch_script())
    assert run.script.trace_kind == "phrase"
    for _ in range(3):
        run.type_phrase()
    assert run.script.trace_kind == "rest"
    run.until("done")
    assert run.script.trace_kind == "rest"  # nothing follows; the session stops recording at the close


def test_a_script_that_has_not_ticked_is_at_its_first_segment_and_the_first_tick_starts_it_whenever_it_comes() -> None:
    s = pinch_script()
    assert s.segment == "phrase"
    s.tick(100.0, True)
    s.tick(100.0 + DT, True)
    assert s.segment == "phrase"
    assert s.result(fps=30.0, noise=0.0, level="ok").presses == 0


# ------------------------------------------------------------------------------------------------ the air script (X36)


def drill_prompts(script: PracticeScript) -> list[practice.DrillPrompt]:
    return list(script._drill)


def play_drill(run: Run, *, hit=lambda p, n: True, key_ok=lambda p, n: True, aim=lambda p, n: None) -> None:
    """One tap per prompt, 0.5 s into its window, where ``hit(prompt, n)`` says; the rest of the window waits."""
    prompts = drill_prompts(run.script)
    run.until("drill")
    for n, p in enumerate(prompts):
        window_start = run.t
        run.advance(0.5)
        if hit(p, n):
            uv = aim(p, n)
            key = p.key if key_ok(p, n) else (p.key + 1) % len(REVIEW.keys)
            run.script.event(run.t, p.side, p.finger, uv[0] if uv else None, uv[1] if uv else None, key)
        run.advance(limits.AIR_DRILL_GAP_S - (run.t - window_start) - 1e-6)
        run.t += 2e-6
        run.tick()


def test_x37_the_air_script_has_a_start_message_a_drill_two_groups_two_rests_and_a_talk() -> None:
    run = Run(air_script())
    s = run.script
    seen = [s.segment]
    while not s.done:
        run.advance(0.5)
        if s.segment != seen[-1]:
            seen.append(s.segment)
        if s.segment in ("phrase", "drill") and s.target_key is not None and s.segment == "phrase":
            run.press(s.target_key)
        assert run.t < 600
    assert seen == ["intro", "drill", "phrase", "rest", "phrase", "rest", "talk", "done"]


def test_x37_the_start_message_comes_first_for_four_seconds_and_names_no_finger() -> None:
    run = Run(air_script())
    s = run.script
    assert s.strip(run.t) == "Practice checks your camera and your taps. Nothing is typed anywhere."
    assert s.named is None and s.target_key is None
    assert s.trace_kind == "warm"
    assert practice.INTRO_S == 4.0  # the design's number, not whatever the constant happens to be
    run.advance(3.8)
    assert s.segment == "intro"
    run.advance(0.4)
    assert s.segment == "drill"


def test_x37_the_rests_are_thirty_five_seconds_and_the_talk_thirty_with_a_hand_in_view() -> None:
    run = Run(air_script())
    s = run.script
    run.until("phrase")
    for _ in range(3):
        run.type_phrase()
    assert s.segment == "rest"
    assert s.prompt == "Rest: do not tap. Wave, open and close your hands."
    run.advance(limits.AIR_REST_S - 1.0)
    assert s.segment == "rest"
    run.advance(1.2)
    assert s.segment == "phrase"
    for _ in range(3):
        run.type_phrase()
    run.until("talk")
    assert s.prompt == "Talk to the camera as on a call. Keep your hands moving. Do not tap."
    assert s.trace_kind == "talk"
    run.advance(limits.AIR_TALK_S - 1.0)
    assert s.segment == "talk"
    run.advance(1.2)
    assert s.done
    r = s.result(fps=30.0, noise=0.01, level="ok")
    assert r.rest_s == pytest.approx(2 * limits.AIR_REST_S, abs=0.1)
    assert r.talk_s == pytest.approx(limits.AIR_TALK_S, abs=0.1)


def test_x37_the_talk_counts_only_the_seconds_with_a_hand_in_view() -> None:
    run = Run(air_script())
    s = run.script
    run.until("phrase")
    for _ in range(3):
        run.type_phrase()
    run.until("phrase")
    for _ in range(3):
        run.type_phrase()
    run.until("talk")
    run.in_view = False
    run.advance(100.0)
    assert s.segment == "talk"
    run.in_view = True
    run.advance(limits.AIR_TALK_S + 0.2)
    assert s.done


def test_x37_talk_phantoms_are_not_phantoms_and_never_enter_the_rate() -> None:
    run = Run(air_script())
    s = run.script
    run.until("phrase")
    for _ in range(3):
        run.type_phrase()
    assert run.event() == "phantom"
    run.until("phrase")
    for _ in range(3):
        run.type_phrase()
    run.until("talk")
    for _ in range(5):
        assert run.event() == "talk"
    run.until("done")
    r = s.result(fps=30.0, noise=0.01, level="ok")
    assert (r.phantoms, r.talk_phantoms) == (1, 5)
    assert r.phantoms_per_min == pytest.approx(1 / (2 * limits.AIR_REST_S / 60), rel=0.01)


def test_x37_the_one_hand_hint_shows_once_for_four_seconds_when_the_phrases_begin() -> None:
    run = Run(air_script(sides=("right",)))
    s = run.script
    run.until("phrase")
    assert s.strip(run.t) == "One hand: move to the key, stop, then tap."
    run.advance(3.0)  # the design pins no length; long enough to read, short enough not to hide the phrase count
    assert s.strip(run.t) == "One hand: move to the key, stop, then tap."
    run.advance(1.6)
    assert s.strip(run.t) == "Phrase 1/6"
    for _ in range(3):
        run.type_phrase()
    run.until("phrase")
    assert s.strip(run.t) == "Phrase 4/6"  # not again for the second group


def test_x37_two_hands_get_no_one_hand_hint_and_pinch_gets_none_either() -> None:
    run = Run(air_script())
    run.until("phrase")
    assert run.script.strip(run.t) == "Phrase 1/6"
    run = Run(pinch_script(sides=("right",)))
    assert run.script.strip(run.t) == "Phrase 1/6"


def test_x36_the_drill_runs_one_prompt_every_one_point_two_seconds_and_lights_its_key() -> None:
    run = Run(air_script())
    s = run.script
    run.until("drill")
    prompts = drill_prompts(s)
    assert len(prompts) == 60
    for n in (0, 1, 7, 30, 59):
        while s._issued != n + 1:
            run.advance(DT)
            assert s.segment == "drill"
        assert s.named == (prompts[n].side, prompts[n].finger)
        assert s.target_key == prompts[n].key
        assert s.prompt == prompts[n].text
        assert s.trace_kind == "drill"
    run.until("phrase")
    drill_seconds = run.t
    assert drill_seconds == pytest.approx(practice.INTRO_S + 60 * limits.AIR_DRILL_GAP_S, abs=0.1)


def test_x36_one_hand_drill_runs_thirty_six_seconds() -> None:
    run = Run(air_script(sides=("right",)))
    run.until("drill")
    start = run.t
    run.until("phrase")
    assert len(drill_prompts(run.script)) == 30
    assert run.t - start == pytest.approx(30 * limits.AIR_DRILL_GAP_S, abs=0.1)


def test_x36_a_tap_of_the_prompted_finger_in_its_window_is_a_hit_and_the_first_one_only() -> None:
    run = Run(air_script())
    run.until("drill")
    p = drill_prompts(run.script)[0]
    run.advance(0.3)
    assert run.script.event(run.t, p.side, p.finger, 0.0, 0.0, p.key) == "hit"
    run.advance(0.3)
    assert run.script.event(run.t, p.side, p.finger, 0.0, 0.0, p.key) == "extra"
    assert run.script.counts["practice_extra"] == 1
    r = run.script.result(fps=30.0, noise=0.0, level="ok")
    assert (r.drill_prompts, r.drill_hits) == (1, 1)


def test_x36_another_fingers_tap_is_a_wrong_finger_and_a_false_tap() -> None:
    run = Run(air_script())
    run.until("drill")
    p = drill_prompts(run.script)[0]
    run.advance(0.3)
    other = (p.finger + 1) % 4
    assert run.script.event(run.t, p.side, other, None, None, None) == "wrong_finger"
    assert (
        run.script.event(run.t, "left" if p.side == "right" else "right", p.finger, None, None, None) == "wrong_finger"
    )
    assert run.script.counts["practice_wrong_finger"] == 2
    r = run.script.result(fps=30.0, noise=0.0, level="ok")
    assert r.drill_hits == 0


def test_x36_a_tap_in_the_next_window_belongs_to_the_next_prompt() -> None:
    run = Run(air_script())
    run.until("drill")
    first, second = drill_prompts(run.script)[:2]
    run.advance(limits.AIR_DRILL_GAP_S + 0.1)
    assert run.script.event(run.t, first.side, first.finger, None, None, None) == "wrong_finger"
    assert run.script.event(run.t, second.side, second.finger, None, None, second.key) == "hit"


def test_x36_the_counts_by_finger_index_and_middle_and_reach_are_kept_apart() -> None:
    run = Run(air_script())
    play_drill(run, hit=lambda p, n: p.finger in (0, 1) or p.reach)
    run.until("phrase")
    r = run.script.result(fps=30.0, noise=0.0, level="ok")
    prompts = drill_prompts(run.script)
    home = [p for p in prompts if not p.reach]
    assert r.drill_prompts == 48 and r.drill_prompts_reach == 12
    assert r.drill_hits_reach == 12
    assert r.drill_prompts_im == sum(1 for p in home if p.finger in (0, 1)) == 24
    assert r.drill_hits_im == 24
    assert r.drill_hits == 24  # only index and middle hit
    assert r.per_finger["left.index"] == (6, 6)
    assert r.per_finger["right.pinky"] == (6, 0)
    assert len(r.per_finger) == 8
    assert all("insert" not in label and "backspace" not in label for label in r.per_finger)


def test_x36_reach_prompts_are_in_the_reach_counts_only() -> None:
    run = Run(air_script())
    play_drill(run, hit=lambda p, n: p.reach, aim=lambda p, n: (50.0, 50.0))
    run.until("phrase")
    r = run.script.result(fps=30.0, noise=0.0, level="ok")
    assert r.drill_hits == 0 and r.drill_hits_im == 0
    assert (r.drill_prompts_reach, r.drill_hits_reach) == (12, 12)
    assert (r.aim_sd_u, r.aim_sd_v) == (0.0, 0.0)  # no home-row hit, no spread, and the reach aims did not enter it
    assert all(hits == 0 for _, hits in r.per_finger.values())


def test_x36_the_key_is_scored_apart_from_the_finger() -> None:
    run = Run(air_script())
    play_drill(run, key_ok=lambda p, n: False)
    run.until("phrase")
    r = run.script.result(fps=30.0, noise=0.0, level="ok")
    assert (r.drill_hits, r.drill_hits_reach) == (48, 12)  # the finger was right


def test_x36_a_drill_cut_short_counts_only_the_prompts_that_were_issued() -> None:
    run = Run(air_script())
    run.until("drill")
    run.advance(5 * limits.AIR_DRILL_GAP_S + 0.1)
    r = run.script.result(fps=30.0, noise=0.0, level="off", completed=False)
    assert r.drill_prompts + r.drill_prompts_reach == 6
    assert not r.completed and r.level == "off"


def test_x51_the_aim_spread_is_the_standard_deviation_of_aim_minus_target_centre_over_the_home_row_hits() -> None:
    rng = random.Random(9)
    errors: list[tuple[float, float]] = []

    def aim(p: practice.DrillPrompt, n: int) -> tuple[float, float]:
        cx = REVIEW.keys[p.key].col + REVIEW.keys[p.key].width / 2
        cy = REVIEW.keys[p.key].row + 0.5
        e = (rng.gauss(0.1, 0.2), rng.gauss(-0.05, 0.3))
        errors.append(e)
        return cx + e[0], cy + e[1]

    run = Run(air_script())
    play_drill(run, hit=lambda p, n: True, aim=aim)
    run.until("phrase")
    r = run.script.result(fps=30.0, noise=0.0, level="ok")
    home_errors = [e for e, p in zip(errors, drill_prompts(run.script), strict=True) if not p.reach]
    assert len(home_errors) == 48
    assert r.aim_sd_u == pytest.approx(statistics.pstdev([e[0] for e in home_errors]))
    assert r.aim_sd_v == pytest.approx(statistics.pstdev([e[1] for e in home_errors]))
    assert 0.1 < r.aim_sd_u < 0.3 and 0.2 < r.aim_sd_v < 0.4


def test_x51_the_aim_of_a_wrong_finger_is_not_in_the_spread() -> None:
    run = Run(air_script())
    run.until("drill")
    p = drill_prompts(run.script)[0]
    run.advance(0.2)
    run.script.event(run.t, p.side, (p.finger + 1) % 4, 40.0, 40.0, None)
    run.script.event(run.t, p.side, p.finger, 10.0, 10.0, p.key)
    run.advance(limits.AIR_DRILL_GAP_S)
    q = drill_prompts(run.script)[1]
    run.script.event(run.t, q.side, q.finger, 10.0, 10.0, q.key)
    r = run.script.result(fps=30.0, noise=0.0, level="ok")
    assert r.aim_sd_u == pytest.approx(0.0, abs=20.0)  # two samples; the 40.0 never entered
    assert r.aim_sd_u < 20.0


def test_the_pinch_result_has_no_air_fields() -> None:
    run = Run(pinch_script())
    for _ in range(3):
        run.type_phrase()
    r = run.script.result(fps=30.0, noise=0.0, level="ok")
    assert (r.drill_prompts, r.drill_hits_reach, r.aim_sd_u, r.talk_s) == (0, 0, 0.0, 0.0)


def test_an_event_in_the_intro_or_a_phrase_is_nothing_for_the_script() -> None:
    run = Run(air_script())
    assert run.event() == ""
    run.until("phrase")
    assert run.event() == ""
    assert run.script.result(fps=30.0, noise=0.0, level="ok").phantoms == 0


def test_a_press_outside_the_phrases_is_ignored() -> None:
    run = Run(air_script())
    assert run.press(5) == ""
    assert run.script.result(fps=30.0, noise=0.0, level="ok").presses == 0


def test_the_script_refuses_a_method_that_has_no_practice() -> None:
    with pytest.raises(ValueError, match=r"^practice needs the air or the pinch method$"):
        PracticeScript(press="windows", lang="en", layout=DIRECT, sides=BOTH)  # type: ignore[arg-type]


# ------------------------------------------------------------------------------------------------------- the markers

NOW = datetime(2026, 10, 8, 12, 0, 0, tzinfo=UTC)
AIR_FILE = "keyboard-practice-air.json"
PINCH_FILE = "keyboard-practice.json"


def result(**kw) -> PracticeResult:
    base = {
        "completed": True,
        "presses": 100,
        "correct": 93,
        "hit_rate": 0.93,
        "rest_s": 70.0,
        "phantoms": 1,
        "phantoms_per_min": 1 / (70 / 60),
        "fps": 29.84,
        "per_finger": {"left.ring": (6, 5)},
        "talk_s": 28.5,
        "talk_phantoms": 6,
        "drill_prompts": 48,
        "drill_hits": 41,
        "drill_prompts_im": 24,
        "drill_hits_im": 22,
        "drill_prompts_reach": 12,
        "drill_hits_reach": 10,
        "aim_sd_u": 0.2143,
        "aim_sd_v": 0.2689,
        "noise": 0.01712,
        "level": "ok",
    }
    return PracticeResult(**{**base, **kw})


def air_doc(**over) -> dict:
    doc = {
        "version": 1,
        "press": "air",
        "completedAt": "2026-10-08T11:00:00Z",
        "restS": 70.0,
        "phantoms": 1,
        "fps": 29.8,
        "noise": 0.017,
        "talkS": 28.5,
        "talkPhantoms": 6,
        "drillPrompts": 48,
        "drillHits": 41,
        "drillHitsIM": 22,
        "drillPromptsIM": 24,
        "keyHit": 0.93,
        "aimSdU": 0.21,
        "aimSdV": 0.27,
    }
    doc.update(over)
    return {k: v for k, v in doc.items() if v is not None}


def pinch_doc(**over) -> dict:
    doc = {
        "version": 1,
        "completedAt": "2026-10-08T11:00:00Z",
        "presses": 100,
        "restS": 40.0,
        "phantoms": 0,
        "hitRate": 0.9,
        "fps": 30.0,
    }
    doc.update(over)
    return {k: v for k, v in doc.items() if v is not None}


def put(tmp_path: Path, name: str, doc: object) -> None:
    folder = tmp_path / "hands"
    folder.mkdir(exist_ok=True)
    text = doc if isinstance(doc, str) else json.dumps(doc)
    (folder / name).write_text(text, encoding="utf-8")


def air_ok(tmp_path: Path, **over) -> bool:
    put(tmp_path, AIR_FILE, air_doc(**over))
    return practice.load_marker(tmp_path, "air", now=NOW) is not None


def pinch_ok(tmp_path: Path, **over) -> bool:
    put(tmp_path, PINCH_FILE, pinch_doc(**over))
    return practice.load_marker(tmp_path, "pinch", now=NOW) is not None


def test_the_marker_files_are_under_the_hands_folder_one_per_method(tmp_path) -> None:
    assert practice.marker_path(tmp_path, "air") == tmp_path / "hands" / AIR_FILE
    assert practice.marker_path(tmp_path, "pinch") == tmp_path / "hands" / PINCH_FILE
    with pytest.raises(ValueError, match=r"^only the air and the pinch method have a practice marker$"):
        practice.marker_path(tmp_path, "windows")


def test_the_air_marker_has_the_fields_of_the_design_and_nothing_else(tmp_path) -> None:
    path = practice.write_marker(tmp_path, "air", result(), now=NOW)
    assert path == tmp_path / "hands" / AIR_FILE
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc == {
        "version": 1,
        "press": "air",
        "completedAt": "2026-10-08T12:00:00Z",
        "restS": 70.0,
        "phantoms": 1,
        "fps": 29.8,
        "noise": 0.0171,
        "talkS": 28.5,
        "talkPhantoms": 6,
        "drillPrompts": 48,
        "drillHits": 41,
        "drillHitsIM": 22,
        "drillPromptsIM": 24,
        "keyHit": 0.93,
        "aimSdU": 0.2143,
        "aimSdV": 0.2689,
    }


def test_the_pinch_marker_has_the_fields_of_the_design_and_nothing_else(tmp_path) -> None:
    path = practice.write_marker(tmp_path, "pinch", result(rest_s=40.0, phantoms=0), now=NOW)
    assert path == tmp_path / "hands" / PINCH_FILE
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "version": 1,
        "completedAt": "2026-10-08T12:00:00Z",
        "presses": 100,
        "restS": 40.0,
        "phantoms": 0,
        "hitRate": 0.93,
        "fps": 29.8,
    }


def test_a_marker_holds_no_key_no_phrase_and_no_finger(tmp_path) -> None:
    for press in ("air", "pinch"):
        path = practice.write_marker(tmp_path, press, result(per_finger={"left.ring": (6, 5)}), now=NOW)
        text = path.read_text(encoding="utf-8")
        assert "left" not in text and "ring" not in text and "quick" not in text


def test_a_practice_that_did_not_complete_writes_no_marker(tmp_path) -> None:
    assert practice.write_marker(tmp_path, "air", result(completed=False), now=NOW) is None
    assert practice.write_marker(tmp_path, "pinch", result(completed=False), now=NOW) is None
    assert not (tmp_path / "hands").exists() or not list((tmp_path / "hands").iterdir())


def test_a_marker_is_written_whatever_its_numbers_because_the_open_judges_them(tmp_path) -> None:
    bad = result(phantoms=50, drill_hits_im=0)
    path = practice.write_marker(tmp_path, "air", bad, now=NOW)
    assert path is not None and path.exists()
    assert practice.marker_status(tmp_path, "air", now=NOW) == "out_of_range"
    assert practice.load_marker(tmp_path, "air", now=NOW) is None


def test_writing_replaces_the_marker_atomically_and_leaves_no_temporary_file(tmp_path) -> None:
    practice.write_marker(tmp_path, "air", result(phantoms=2), now=NOW)
    practice.write_marker(tmp_path, "air", result(phantoms=0), now=NOW)
    assert sorted(p.name for p in (tmp_path / "hands").iterdir()) == [AIR_FILE]
    assert practice.read_marker(tmp_path, "air", now=NOW)["phantoms"] == 0


def test_a_failed_write_leaves_the_old_marker_and_no_temporary_file(tmp_path, monkeypatch) -> None:
    practice.write_marker(tmp_path, "air", result(phantoms=2), now=NOW)

    def boom(*_a, **_k):
        raise OSError("disk")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        practice.write_marker(tmp_path, "air", result(phantoms=0), now=NOW)
    monkeypatch.undo()
    assert sorted(p.name for p in (tmp_path / "hands").iterdir()) == [AIR_FILE]
    assert practice.read_marker(tmp_path, "air", now=NOW)["phantoms"] == 2


def test_a_marker_written_now_is_accepted_by_the_reader(tmp_path) -> None:
    practice.write_marker(tmp_path, "air", result(), now=datetime.now(UTC))
    assert practice.load_marker(tmp_path, "air") is not None
    practice.write_marker(tmp_path, "pinch", result(rest_s=40.0, phantoms=0), now=datetime.now(UTC))
    assert practice.load_marker(tmp_path, "pinch") is not None


def test_x37_no_file_is_no_marker(tmp_path) -> None:
    assert practice.load_marker(tmp_path, "air", now=NOW) is None
    assert practice.marker_status(tmp_path, "air", now=NOW) == "missing"
    assert practice.marker_status(tmp_path, "pinch", now=NOW) == "missing"


def test_x37_a_good_marker_is_accepted_and_returned(tmp_path) -> None:
    assert air_ok(tmp_path)
    assert practice.marker_status(tmp_path, "air", now=NOW) == "ok"
    assert practice.load_marker(tmp_path, "air", now=NOW)["restS"] == 70.0


@pytest.mark.parametrize(
    ("phantoms", "rest_s", "accepted"),
    [(3, 60.0, True), (5, 100.0, True), (31, 600.0, False), (5, 96.0, False), (0, 60.0, True), (4, 60.0, False)],
)
def test_x37_three_phantoms_a_minute_is_accepted_and_more_is_refused(tmp_path, phantoms, rest_s, accepted) -> None:
    assert air_ok(tmp_path, phantoms=phantoms, restS=rest_s) is accepted


@pytest.mark.parametrize(("rest_s", "accepted"), [(59.9, False), (60.0, True), (70.0, True), (0.0, False)])
def test_x37_the_rest_must_be_at_least_sixty_seconds(tmp_path, rest_s, accepted) -> None:
    assert air_ok(tmp_path, restS=rest_s, phantoms=0) is accepted
    assert air_ok(tmp_path, restS=rest_s, phantoms=3 if rest_s == 60.0 else 0) is accepted


def test_x37_the_boundary_pair_of_the_design_sixty_seconds_with_three_phantoms(tmp_path) -> None:
    assert air_ok(tmp_path, restS=60.0, phantoms=3)
    assert not air_ok(tmp_path, restS=59.9, phantoms=3)


def test_x37_a_marker_without_the_talk_numbers_is_accepted_and_the_talk_is_never_gated(tmp_path) -> None:
    assert air_ok(tmp_path, talkS=None, talkPhantoms=None)
    assert air_ok(tmp_path, talkPhantoms=0)
    assert air_ok(tmp_path, talkPhantoms=10_000)
    assert air_ok(tmp_path, talkS=0.0, talkPhantoms=99)


@pytest.mark.parametrize(("hits", "accepted"), [(70, True), (69, False), (100, True), (0, False)])
def test_x37_drill_recall_of_seventy_percent_is_accepted_and_sixty_nine_refused(tmp_path, hits, accepted) -> None:
    assert air_ok(tmp_path, drillPromptsIM=100, drillHitsIM=hits) is accepted


def test_x37_the_recall_rule_needs_twenty_four_prompts(tmp_path) -> None:
    assert not air_ok(tmp_path, drillPromptsIM=24, drillHitsIM=16)  # 0.667
    assert air_ok(tmp_path, drillPromptsIM=24, drillHitsIM=17)  # 0.708
    assert air_ok(tmp_path, drillPromptsIM=23, drillHitsIM=0)  # too few prompts to judge
    assert air_ok(tmp_path, drillPromptsIM=None, drillHitsIM=None)  # a marker without them


@pytest.mark.parametrize(
    "over",
    [
        {"press": "pinch"},
        {"press": None},
        {"press": "windows"},
        {"version": 2},
        {"version": 0},
        {"version": "1"},
        {"version": True},
        {"completedAt": "2026-10-08T12:00:01Z"},
        {"completedAt": "2030-01-01T00:00:00Z"},
        {"completedAt": "yesterday"},
        {"completedAt": 12345},
        {"completedAt": None},
        {"restS": None},
        {"restS": "70"},
        {"restS": True},
        {"restS": float("nan")},
        {"phantoms": None},
        {"phantoms": "0"},
        {"phantoms": float("inf")},
        {"phantoms": False},
        {"restS": 1000.0, "phantoms": True},
    ],
)
def test_x37_a_marker_that_is_not_what_the_design_describes_is_no_marker(tmp_path, over) -> None:
    put(tmp_path, AIR_FILE, json.dumps(air_doc(**over), allow_nan=True))
    assert practice.load_marker(tmp_path, "air", now=NOW) is None
    assert practice.marker_status(tmp_path, "air", now=NOW) == "missing"  # not a marker at all, not a poor one


def test_x37_negative_numbers_are_a_marker_out_of_range(tmp_path) -> None:
    for over in ({"phantoms": -1}, {"restS": -5.0}):
        put(tmp_path, AIR_FILE, air_doc(**over))
        assert practice.load_marker(tmp_path, "air", now=NOW) is None


@pytest.mark.parametrize("text", ["", "{", "[]", "null", "42", '"air"', '{"version": 1', "\u0000\u0001", "{" * 5000])
def test_x37_an_unparsable_file_is_no_marker(tmp_path, text) -> None:
    put(tmp_path, AIR_FILE, text)
    assert practice.load_marker(tmp_path, "air", now=NOW) is None
    assert practice.marker_status(tmp_path, "air", now=NOW) == "missing"


def test_x37_a_file_of_bytes_that_are_not_text_is_no_marker(tmp_path) -> None:
    (tmp_path / "hands").mkdir()
    (tmp_path / "hands" / AIR_FILE).write_bytes(b"\xff\xfe\x00{")
    assert practice.load_marker(tmp_path, "air", now=NOW) is None


def test_x37_an_oversized_file_is_no_marker(tmp_path) -> None:
    put(tmp_path, AIR_FILE, json.dumps({**air_doc(), "pad": "x" * practice.MARKER_MAX_BYTES}))
    assert practice.load_marker(tmp_path, "air", now=NOW) is None


def test_x37_a_directory_where_the_file_should_be_is_no_marker(tmp_path) -> None:
    (tmp_path / "hands" / AIR_FILE).mkdir(parents=True)
    assert practice.load_marker(tmp_path, "air", now=NOW) is None


def test_x37_the_pinch_marker_is_not_accepted_for_air_even_when_copied_over(tmp_path) -> None:
    put(tmp_path, AIR_FILE, pinch_doc(restS=100.0))
    assert practice.load_marker(tmp_path, "air", now=NOW) is None
    put(tmp_path, PINCH_FILE, air_doc())
    assert practice.load_marker(tmp_path, "pinch", now=NOW) is None


def test_x37_each_method_reads_only_its_own_file(tmp_path) -> None:
    put(tmp_path, AIR_FILE, air_doc())
    assert practice.load_marker(tmp_path, "pinch", now=NOW) is None
    put(tmp_path, PINCH_FILE, pinch_doc())
    assert practice.load_marker(tmp_path, "pinch", now=NOW) is not None
    (tmp_path / "hands" / AIR_FILE).unlink()
    assert practice.load_marker(tmp_path, "air", now=NOW) is None


def test_x37_the_date_may_be_now_but_not_later_and_a_date_without_a_zone_reads_as_utc(tmp_path) -> None:
    assert air_ok(tmp_path, completedAt="2026-10-08T12:00:00Z")
    assert air_ok(tmp_path, completedAt="2026-10-08T12:00:00+00:00")
    assert air_ok(tmp_path, completedAt="2026-10-08T12:00:00")
    assert not air_ok(tmp_path, completedAt="2026-10-08T12:00:00.5Z")
    assert not air_ok(tmp_path, completedAt="2026-10-08T14:00:00+01:00")  # 13:00 UTC
    assert air_ok(tmp_path, completedAt="2026-10-08T12:59:00+01:00")  # 11:59 UTC


def test_the_pinch_marker_needs_twenty_seconds_of_rest_and_one_phantom_a_minute(tmp_path) -> None:
    assert pinch_ok(tmp_path)
    assert pinch_ok(tmp_path, restS=20.0, phantoms=0)
    assert not pinch_ok(tmp_path, restS=19.9, phantoms=0)
    assert pinch_ok(tmp_path, restS=60.0, phantoms=1)  # exactly one a minute
    assert not pinch_ok(tmp_path, restS=40.0, phantoms=1)  # 1.5 a minute: the intended strictness
    assert not pinch_ok(tmp_path, restS=60.0, phantoms=2)


def test_the_pinch_marker_ignores_the_air_drill_fields(tmp_path) -> None:
    assert pinch_ok(tmp_path, drillPromptsIM=100, drillHitsIM=0)


def test_the_air_bounds_are_the_constants_of_the_limits() -> None:
    assert limits.AIR_PRACTICE_MIN_REST_S == 60 and limits.AIR_PRACTICE_MAX_PHANTOMS_PER_MIN == 3.0
    assert limits.AIR_PRACTICE_MIN_DRILL_PROMPTS == 24 and limits.AIR_PRACTICE_MIN_DRILL_RECALL == 0.70
    assert limits.PRACTICE_MIN_REST_S == 20 and limits.PRACTICE_MAX_PHANTOMS_PER_MIN == 1.0


def test_the_air_marker_is_never_built_for_a_method_without_one() -> None:
    with pytest.raises(ValueError):
        practice.build_marker(result(), "windows", now=NOW)


def test_the_stub_is_gone() -> None:
    assert not hasattr(practice, "STUB_OWNER")
    with pytest.raises(AttributeError):
        practice.no_such_name  # noqa: B018 - the canonical stub lookup is removed, a missing name is a plain AttributeError


# ----------------------------------------------------------------------------------------------------------- tap log

from jarvis_hands import synthetic  # noqa: E402
from jarvis_hands.keyboard import trace  # noqa: E402
from jarvis_hands.keyboard.trace import TapLog, TraceData, TraceError, TraceWriter  # noqa: E402

PINCH_LINE = {
    "t": 12.345,
    "side": "left",
    "finger": "ring",
    "u": 4.12,
    "v": 1.31,
    "ratio": 0.23,
    "margin": 0.18,
    "closingMs": 133,
    "outcome": "key",
    "target": 17,
    "hit": 17,
}
FIRE = {
    "k": "fire", "t": 12.345, "hand": 2, "side": "right", "finger": 1, "onsetT": 12.211, "pkT": 12.278, "depth": 0.312,
    "theta": 0.1, "sigma": 0.0158, "margin": 1.8, "conf": 0.8, "widthMs": 93, "riseMs": 67, "fallMs": 33, "speed": 0.08,
    "vmax": 0.11, "nPeers": 1, "E": [0.66, 0.76, 0.71, 0.51], "delta": [0.01, 0.31, 0.02, -0.01],
    "win": [[-433, 0.01, 0.02], [0, 0.14, 0.15]], "aim": [0.4872, 0.2977], "aimRule": "onset", "vl": 0.02, "u": 4.12,
    "v": 1.31, "fps": 30.1, "outcome": "key", "target": 17, "hit": 17,
}  # fmt: skip
REJECT = {
    "k": "reject",
    "t": 12.9,
    "hand": 2,
    "side": "right",
    "finger": 3,
    "why": "plateau",
    "rise": 0.126,
    "theta": 0.1,
    "width": 0.14,
    "vmax": 0.62,
}
GATE = {"k": "gate", "t": 13.4, "hand": 2, "side": "right", "gate": "coherence", "dur": 0.3}


def lines_of(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def test_the_tap_log_is_one_json_line_per_record_under_the_hands_folder(tmp_path) -> None:
    log = TapLog(tmp_path)
    assert log.path == tmp_path / "hands" / "keyboard-practice.jsonl"
    assert log.write([PINCH_LINE, FIRE, REJECT, GATE]) == 4
    assert lines_of(log.path) == [PINCH_LINE, FIRE, REJECT, GATE]
    assert log.write([REJECT]) == 1
    assert len(lines_of(log.path)) == 5


def test_nothing_is_created_for_nothing_to_write(tmp_path) -> None:
    log = TapLog(tmp_path)
    assert log.write([]) == 0
    assert log.write([{"k": "other"}]) == 0
    assert not (tmp_path / "hands").exists()


def test_the_tap_log_writes_the_fields_of_the_design_and_drops_every_other_field(tmp_path) -> None:
    sneaky = {
        **PINCH_LINE,
        "char": "a",
        "text": "hello",
        "key": "q",
        "phrase": "the quick brown fox",
        "title": "Notes",
        "keyIndex": 3,
        "code": 97,
        "chars": [104, 105],
    }
    TapLog(tmp_path).write([sneaky, {**FIRE, "window": "Notepad", "vk": 65}])
    written = lines_of(tmp_path / "hands" / "keyboard-practice.jsonl")
    assert written == [PINCH_LINE, FIRE]
    assert "hello" not in (tmp_path / "hands" / "keyboard-practice.jsonl").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("outcome", "q"),
        ("outcome", "the"),
        ("outcome", "KEY"),
        ("outcome", ""),
        ("side", "a"),
        ("finger", "q"),
        ("finger", "thumb"),
        ("aimRule", "z"),
    ],
)
def test_a_text_field_outside_its_fixed_words_is_left_out_so_no_character_can_ride_in_it(
    tmp_path, field, value
) -> None:
    record = dict(FIRE) if field == "aimRule" else dict(PINCH_LINE)
    record[field] = value
    TapLog(tmp_path).write([record])
    (line,) = lines_of(tmp_path / "hands" / "keyboard-practice.jsonl")
    assert field not in line


@pytest.mark.parametrize(
    ("record", "field"),
    [({**REJECT, "why": "x"}, "why"), ({**REJECT, "why": "gate_speed "}, "why"), ({**GATE, "gate": "q"}, "gate")],
)
def test_the_reject_and_gate_words_are_closed_too(tmp_path, record, field) -> None:
    TapLog(tmp_path).write([record])
    (line,) = lines_of(tmp_path / "hands" / "keyboard-practice.jsonl")
    assert field not in line


@pytest.mark.parametrize("why", ["plateau", "narrow", "wide", "motion", "pinched", "veto", "excl", "coherence_raw",
                                 "gate_speed", "gate_posture", "gate_hold", "gate_score", "gate_warm", "gate_coherence",
                                 "gate_refractory", "tremor"])  # fmt: skip
def test_every_reject_reason_of_the_detector_is_written(tmp_path, why) -> None:
    TapLog(tmp_path).write([{**REJECT, "why": why}])
    assert lines_of(tmp_path / "hands" / "keyboard-practice.jsonl")[0]["why"] == why


def test_every_outcome_of_the_design_is_written(tmp_path) -> None:
    outcomes = ["key", "off", "held", "stale", "queue", "not_armed", "warmup_tap", "practice_review_key"]
    assert sorted(trace.OUTCOMES) == sorted(outcomes)
    TapLog(tmp_path).write([{**PINCH_LINE, "outcome": o} for o in outcomes])
    assert [x["outcome"] for x in lines_of(tmp_path / "hands" / "keyboard-practice.jsonl")] == outcomes


def test_a_list_with_a_word_in_it_is_left_out_whole() -> None:
    line = trace.tap_log_line({**FIRE, "E": [0.1, "a", 0.3], "win": [[1, 2, "b"]]})
    assert line is not None
    doc = json.loads(line)
    assert "E" not in doc and "win" not in doc and doc["delta"] == FIRE["delta"]


def test_not_a_number_and_infinity_become_null_and_numpy_values_are_numbers() -> None:
    import numpy as np

    line = trace.tap_log_line(
        {**PINCH_LINE, "ratio": float("nan"), "margin": float("inf"), "u": np.float32(0.5), "v": np.int64(3), "t": 1.0}
    )
    doc = json.loads(line)
    assert doc["ratio"] is None and doc["margin"] is None
    assert doc["u"] == 0.5 and doc["v"] == 3
    fire = json.loads(trace.tap_log_line({**FIRE, "E": np.array([0.1, 0.2]), "aim": (np.float64(0.25), 0.5)}))
    assert fire["E"] == [0.1, 0.2] and fire["aim"] == [0.25, 0.5]


def test_booleans_and_objects_are_not_written() -> None:
    doc = json.loads(trace.tap_log_line({**PINCH_LINE, "hit": True, "target": {"a": 1}, "u": object()}))
    assert "hit" not in doc and "target" not in doc and "u" not in doc


def test_a_record_of_an_unknown_kind_is_not_written() -> None:
    assert trace.tap_log_line({"k": "text", "t": 1.0}) is None
    assert trace.tap_log_line({"k": 5, "t": 1.0}) is None


def test_the_rotation_keeps_one_megabyte_in_each_of_three_files() -> None:
    assert trace.TAP_LOG_MAX_BYTES == 1_000_000 and trace.TAP_LOG_FILES == 3


def test_the_log_rotates_to_dot_one_and_dot_two_and_the_oldest_goes(tmp_path) -> None:
    log = TapLog(tmp_path, max_bytes=600, files=3)
    for n in range(60):
        log.write([{**PINCH_LINE, "t": float(n)}])
    names = sorted(p.name for p in (tmp_path / "hands").iterdir())
    assert names == ["keyboard-practice.jsonl", "keyboard-practice.jsonl.1", "keyboard-practice.jsonl.2"]
    for p in (tmp_path / "hands").iterdir():
        assert p.stat().st_size <= 600
    newest = lines_of(log.path)
    older = lines_of(log.path.with_name("keyboard-practice.jsonl.1"))
    oldest = lines_of(log.path.with_name("keyboard-practice.jsonl.2"))
    assert newest[-1]["t"] == 59.0
    assert older[-1]["t"] < newest[0]["t"] and oldest[-1]["t"] < older[0]["t"]
    assert [x["t"] for x in oldest + older + newest] == sorted(x["t"] for x in oldest + older + newest)


def test_the_rotation_loses_nothing_before_the_third_file_is_needed(tmp_path) -> None:
    log = TapLog(tmp_path, max_bytes=600, files=3)
    for n in range(10):
        log.write([{**PINCH_LINE, "t": float(n)}])
    all_t = []
    for suffix in (".2", ".1", ""):
        p = tmp_path / "hands" / f"keyboard-practice.jsonl{suffix}"
        if p.exists():
            all_t += [x["t"] for x in lines_of(p)]
    assert all_t == [float(n) for n in range(10)]


def test_a_file_may_fill_to_exactly_its_size_and_the_next_line_starts_the_next_file(tmp_path) -> None:
    line = trace.tap_log_line(PINCH_LINE)
    assert line is not None
    size = len(line) + 1
    log = TapLog(tmp_path, max_bytes=2 * size, files=3)
    log.write([PINCH_LINE])
    log.write([PINCH_LINE])
    assert log.path.stat().st_size == 2 * size
    assert not log.path.with_name("keyboard-practice.jsonl.1").exists()
    log.write([PINCH_LINE])
    assert log.path.with_name("keyboard-practice.jsonl.1").stat().st_size == 2 * size
    assert log.path.stat().st_size == size


def test_a_batch_larger_than_a_file_is_written_whole_into_an_empty_log_without_rotating(tmp_path) -> None:
    log = TapLog(tmp_path, max_bytes=10, files=3)
    log.write([PINCH_LINE, PINCH_LINE])
    assert [p.name for p in (tmp_path / "hands").iterdir()] == ["keyboard-practice.jsonl"]
    assert len(lines_of(log.path)) == 2
    log.path.write_text("", encoding="utf-8")  # an empty live file is not rotated either
    log.write([PINCH_LINE])
    assert [p.name for p in (tmp_path / "hands").iterdir()] == ["keyboard-practice.jsonl"]


def test_a_single_file_log_starts_over_instead_of_keeping_an_older_one(tmp_path) -> None:
    log = TapLog(tmp_path, max_bytes=300, files=1)
    for n in range(20):
        log.write([{**PINCH_LINE, "t": float(n)}])
    assert [p.name for p in (tmp_path / "hands").iterdir()] == ["keyboard-practice.jsonl"]
    assert lines_of(log.path)[-1]["t"] == 19.0


def test_a_batch_is_never_split_across_two_files(tmp_path) -> None:
    log = TapLog(tmp_path, max_bytes=700, files=3)
    log.write([{**PINCH_LINE, "t": 1.0}] * 3)
    log.write([{**PINCH_LINE, "t": 2.0}] * 3)
    assert {x["t"] for x in lines_of(log.path)} == {2.0}
    assert {x["t"] for x in lines_of(log.path.with_name("keyboard-practice.jsonl.1"))} == {1.0}


def test_a_tap_log_needs_a_size_and_a_file_count(tmp_path) -> None:
    with pytest.raises(ValueError):
        TapLog(tmp_path, max_bytes=0)
    with pytest.raises(ValueError):
        TapLog(tmp_path, files=0)


# ------------------------------------------------------------------------------------------------------------ traces


def obs(side: str, x: float, y: float = 0.5, score: float = 0.95, pose: str = "palm"):
    return synthetic.hand(pose, (x, y), handedness=side, score=score)  # type: ignore[arg-type]


def make_frames(n: int = 6, *, t0: float = 100.0, hands: int = 2) -> list:
    frames = []
    for i in range(n):
        seen = []
        if hands >= 1:
            seen.append(obs("right", 0.6 + 0.001 * i))
        if hands >= 2:
            seen.append(obs("left", 0.4))
        frames.append(synthetic.frame(t0 + i * DT, *seen))
    return frames


def written(frames, kinds, targets=None, *, press="air") -> TraceWriter:
    w = TraceWriter(press=press)
    for i, f in enumerate(frames):
        w.add(f, kinds[i], -1 if targets is None else targets[i])
    return w


def test_a_trace_round_trips_every_array(tmp_path) -> None:
    frames = make_frames(6)
    kinds = ["place", "place", "warm", "phrase", "phrase", "rest"]
    targets = [-1, -1, -1, 17, 18, -1]
    path = written(frames, kinds, targets).save(tmp_path / "t.npz")
    data = trace.read_trace(path)
    assert isinstance(data, TraceData)
    assert len(data) == 6
    assert data.press == "air" and data.version == 2
    assert data.t.dtype == np.float64 and list(data.t) == pytest.approx([f.t for f in frames])
    assert data.present.dtype == bool and data.present.shape == (6, 2) and data.present.all()
    assert data.side.dtype == np.int8 and data.score.dtype == np.float32
    assert data.lm.dtype == np.float32 and data.lm.shape == (6, 2, 21, 3)
    assert data.targets.dtype == np.int16 and list(data.targets) == targets
    assert data.segments == ((0, 2, "place"), (2, 3, "warm"), (3, 5, "phrase"), (5, 6, "rest"))
    assert (data.width, data.height) == (1280, 720)
    assert data.aspect == pytest.approx(720 / 1280)


import numpy as np  # noqa: E402


def test_slot_zero_is_the_hand_further_left_in_the_picture_and_the_side_codes_are_0_and_1(tmp_path) -> None:
    f = synthetic.frame(1.0, obs("right", 0.7), obs("left", 0.3))  # given right first
    data = trace.read_trace(written([f], ["type"]).save(tmp_path / "t.npz"))
    assert list(data.side[0]) == [0, 1]
    assert data.lm[0, 0, 0, 0] < data.lm[0, 1, 0, 0]
    assert np.allclose(data.lm[0, 1], f.hands[0].image, atol=1e-6)


def test_an_absent_hand_is_nan_with_side_minus_one_and_score_zero(tmp_path) -> None:
    f = synthetic.frame(1.0, obs("right", 0.6))
    g = synthetic.frame(1.0 + DT)
    data = trace.read_trace(written([f, g], ["type", "type"]).save(tmp_path / "t.npz"))
    assert list(data.present[0]) == [True, False] and list(data.present[1]) == [False, False]
    assert list(data.side[0]) == [1, -1]
    assert data.score[0, 1] == 0.0 and data.score[0, 0] == pytest.approx(0.95)
    assert np.isnan(data.lm[0, 1]).all() and np.isnan(data.lm[1]).all()
    assert not np.isnan(data.lm[0, 0]).any()


def test_more_than_two_hands_keep_the_two_most_confident(tmp_path) -> None:
    f = synthetic.frame(1.0, obs("left", 0.2, score=0.5), obs("right", 0.5, score=0.9), obs("right", 0.8, score=0.8))
    data = trace.read_trace(written([f], ["type"]).save(tmp_path / "t.npz"))
    assert sorted(data.score[0].tolist()) == pytest.approx([0.8, 0.9])


def test_the_frames_come_back_as_the_tracker_gets_them(tmp_path) -> None:
    frames = make_frames(3)
    data = trace.read_trace(written(frames, ["type"] * 3).save(tmp_path / "t.npz"))
    back = list(data.frames())
    assert len(back) == 3
    for a, b in zip(frames, back, strict=True):
        assert (b.t, b.width, b.height) == (pytest.approx(a.t), a.width, a.height)
        assert len(b.hands) == 2
        by_side = {h.handedness: h for h in b.hands}
        for h in a.hands:
            assert np.allclose(by_side[h.handedness].image, h.image, atol=1e-6)
            assert by_side[h.handedness].score == pytest.approx(h.score)
            assert not by_side[h.handedness].world.any()


def test_the_recording_stops_at_ten_minutes() -> None:
    w = TraceWriter(press="air")
    assert w.add(synthetic.frame(0.0), "type")
    assert w.add(synthetic.frame(600.0), "type")
    assert not w.add(synthetic.frame(600.1), "type")
    assert w.full and len(w) == 2
    assert not w.add(synthetic.frame(601.0), "type")


def test_a_frame_of_another_size_is_not_added() -> None:
    w = TraceWriter(press="air")
    assert w.add(synthetic.frame(0.0), "type")
    assert not w.add(synthetic.frame(0.1, size=(640, 480)), "type")
    assert len(w) == 1 and not w.full


def test_segments_are_runs_and_a_kind_that_returns_is_a_new_segment() -> None:
    w = written(make_frames(5), ["phrase", "rest", "rest", "phrase", "talk"])
    assert w.segments() == [(0, 1, "phrase"), (1, 3, "rest"), (3, 4, "phrase"), (4, 5, "talk")]


@pytest.mark.parametrize("kind", ["", "Rest", "a b", "re-st", "r3", "x" * 13, "é"])
def test_a_segment_kind_is_a_short_lowercase_word(kind) -> None:
    with pytest.raises(ValueError, match=r"^a segment kind is a short lowercase word$"):
        TraceWriter(press="air").add(synthetic.frame(0.0), kind)


@pytest.mark.parametrize("kind", ["place", "warm", "phrase", "rest", "type", "tap", "drill", "talk", "wave"])
def test_every_segment_kind_of_the_design_is_accepted(kind) -> None:
    assert TraceWriter(press="air").add(synthetic.frame(0.0), kind)


def test_a_trace_is_recorded_for_the_air_or_the_pinch_method_only() -> None:
    with pytest.raises(ValueError):
        TraceWriter(press="windows")  # type: ignore[arg-type]


def test_the_file_holds_exactly_the_arrays_of_the_design_and_no_text(tmp_path) -> None:
    path = written(make_frames(2), ["type", "type"]).save(tmp_path / "t.npz")
    with np.load(path, allow_pickle=False) as npz:
        assert sorted(npz.files) == sorted(
            [
                "t",
                "present",
                "side",
                "score",
                "lm",
                "aspect",
                "width",
                "height",
                "segments",
                "targets",
                "press",
                "version",
            ]
        )
        assert npz["version"] == 2 and str(npz["press"]) == "air"
        for name in npz.files:
            assert npz[name].dtype != object


def test_a_pinch_recording_says_pinch(tmp_path) -> None:
    data = trace.read_trace(written(make_frames(2), ["type", "type"], press="pinch").save(tmp_path / "t.npz"))
    assert data.press == "pinch"


def test_saving_is_atomic_and_makes_the_folder(tmp_path) -> None:
    path = written(make_frames(2), ["type", "type"]).save(tmp_path / "deep" / "er" / "t.npz")
    assert path.exists()
    assert [p.name for p in path.parent.iterdir()] == ["t.npz"]


def test_a_failed_save_leaves_the_old_file_and_no_temporary_one(tmp_path, monkeypatch) -> None:
    path = written(make_frames(2), ["type", "type"]).save(tmp_path / "t.npz")
    before = path.read_bytes()

    def boom(*_a, **_k):
        raise OSError("disk")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        written(make_frames(4), ["type"] * 4).save(path)
    monkeypatch.undo()
    assert path.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["t.npz"]


def test_an_empty_trace_saves_and_reads(tmp_path) -> None:
    data = trace.read_trace(TraceWriter(press="air").save(tmp_path / "t.npz"))
    assert len(data) == 0 and data.segments == () and data.lm.shape == (0, 2, 21, 3)


def test_the_trace_name_carries_the_utc_time(tmp_path) -> None:
    when = datetime(2026, 10, 8, 12, 5, 9, tzinfo=UTC)
    assert trace.trace_path(tmp_path, when) == tmp_path / "hands" / "keyboard-trace-20261008T120509Z.npz"


def save_raw(path: Path, **arrays) -> Path:
    np.savez(path, **arrays)
    return path


def good_arrays(n: int = 2, **over):
    base = {
        "t": np.arange(n, dtype=np.float64),
        "present": np.ones((n, 2), bool),
        "side": np.zeros((n, 2), np.int8),
        "score": np.ones((n, 2), np.float32),
        "lm": np.zeros((n, 2, 21, 3), np.float32),
        "aspect": np.float64(0.5625),
        "width": np.int32(1280),
        "height": np.int32(720),
        "segments": np.array([(0, n, "type")], dtype=[("start", "<i4"), ("end", "<i4"), ("kind", "<U12")]),
        "targets": np.full(n, -1, np.int16),
        "press": np.str_("air"),
        "version": np.int32(2),
    }
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


def test_a_version_one_trace_has_no_press_and_reads_as_pinch(tmp_path) -> None:
    data = trace.read_trace(save_raw(tmp_path / "t.npz", **good_arrays(press=None, version=np.int32(1))))
    assert data.press == "pinch" and data.version == 1


def test_a_good_hand_built_trace_reads(tmp_path) -> None:
    assert trace.read_trace(save_raw(tmp_path / "t.npz", **good_arrays())).press == "air"


@pytest.mark.parametrize(
    "over",
    [
        {"version": np.int32(3)},
        {"version": np.int32(0)},
        {"press": np.str_("windows")},
        {"press": None},  # version 2 needs it
        {"present": np.ones((3, 2), bool)},
        {"lm": np.zeros((2, 2, 20, 3), np.float32)},
        {"targets": np.full(5, -1, np.int16)},
        {"t": np.zeros((2, 1))},
        {"segments": np.array([(0, 9, "type")], dtype=[("start", "<i4"), ("end", "<i4"), ("kind", "<U12")])},
        {"segments": np.array([(1, 0, "type")], dtype=[("start", "<i4"), ("end", "<i4"), ("kind", "<U12")])},
        {"segments": np.array([(0, 1, "Type!")], dtype=[("start", "<i4"), ("end", "<i4"), ("kind", "<U12")])},
        {"t": None},
        {"segments": None},
    ],
)
def test_a_trace_that_is_not_one_raises_a_fixed_error(tmp_path, over) -> None:
    path = save_raw(tmp_path / "secret-name.npz", **good_arrays(**over))
    with pytest.raises(TraceError) as info:
        trace.read_trace(path)
    assert str(info.value) == "not a landmark trace"
    assert "secret-name" not in repr(info.value)


@pytest.mark.parametrize("content", [b"", b"PK\x03\x04junk", b"not an archive at all", b"\x93NUMPY\x01\x00"])
def test_a_file_that_is_not_an_archive_raises_the_fixed_error(tmp_path, content) -> None:
    path = tmp_path / "t.npz"
    path.write_bytes(content)
    with pytest.raises(TraceError, match=r"^not a landmark trace$"):
        trace.read_trace(path)


def test_a_missing_file_raises_the_fixed_error(tmp_path) -> None:
    with pytest.raises(TraceError, match=r"^not a landmark trace$"):
        trace.read_trace(tmp_path / "nope.npz")


@pytest.mark.parametrize("payload", [np.array([{"a": 1}], dtype=object), np.array("air", dtype=object)])
def test_a_trace_that_needs_unpickling_is_refused_even_when_what_it_holds_would_be_fine(tmp_path, payload) -> None:
    path = save_raw(tmp_path / "t.npz", **good_arrays(press=payload))
    with pytest.raises(TraceError, match=r"^not a landmark trace$"):
        trace.read_trace(path)


# --------------------------------------------------------------------------------------------------------- clean-up

DAY = 86400.0


def trace_file(tmp_path: Path, name: str, age_days: float, now: float) -> Path:
    folder = tmp_path / "hands"
    folder.mkdir(exist_ok=True)
    path = folder / name
    path.write_bytes(b"x")
    os.utime(path, (now - age_days * DAY, now - age_days * DAY))
    return path


def test_traces_older_than_the_keep_days_are_deleted_and_newer_ones_kept(tmp_path) -> None:
    now = 1_800_000_000.0
    old = trace_file(tmp_path, "keyboard-trace-20260101T000000Z.npz", limits.TRACE_KEEP_DAYS + 0.01, now)
    older = trace_file(tmp_path, "keyboard-trace-20250101T000000Z.npz", 400, now)
    new = trace_file(tmp_path, "keyboard-trace-20261001T000000Z.npz", limits.TRACE_KEEP_DAYS - 0.01, now)
    fresh = trace_file(tmp_path, "keyboard-trace-20261008T000000Z.npz", 0, now)
    trace.cleanup(tmp_path, now=now)
    assert not old.exists() and not older.exists()
    assert new.exists() and fresh.exists()


def test_a_trace_exactly_at_the_keep_age_is_kept(tmp_path) -> None:
    now = 1_800_000_000.0
    edge = trace_file(tmp_path, "keyboard-trace-edge.npz", limits.TRACE_KEEP_DAYS, now)
    trace.cleanup(tmp_path, now=now)
    assert edge.exists()


def test_the_keep_days_are_fourteen() -> None:
    assert limits.TRACE_KEEP_DAYS == 14


def test_cleanup_touches_nothing_but_traces(tmp_path) -> None:
    now = 1_800_000_000.0
    others = [
        trace_file(tmp_path, "keyboard-tuning.json", 400, now),
        trace_file(tmp_path, "keyboard-practice.json", 400, now),
        trace_file(tmp_path, "keyboard-practice-air.json", 400, now),
        trace_file(tmp_path, "keyboard-practice.jsonl", 400, now),
        trace_file(tmp_path, "keyboard-practice.jsonl.1", 400, now),
        trace_file(tmp_path, "notes.npz", 400, now),
        trace_file(tmp_path, "keyboard-trace.npz", 400, now),
    ]
    trace.cleanup(tmp_path, now=now)
    assert all(p.exists() for p in others)


def test_an_old_half_written_trace_goes_too(tmp_path) -> None:
    now = 1_800_000_000.0
    half = trace_file(tmp_path, "keyboard-trace-20260101T000000Z.npz.tmp", 30, now)
    trace.cleanup(tmp_path, now=now)
    assert not half.exists()


def test_cleanup_of_a_folder_that_is_not_there_is_nothing(tmp_path) -> None:
    trace.cleanup(tmp_path / "never", now=1.0)
    trace.cleanup(tmp_path)  # the real clock, an empty folder


def test_cleanup_leaves_a_file_it_cannot_remove_and_goes_on(tmp_path, monkeypatch) -> None:
    now = 1_800_000_000.0
    a = trace_file(tmp_path, "keyboard-trace-a.npz", 30, now)
    b = trace_file(tmp_path, "keyboard-trace-b.npz", 30, now)
    real = Path.unlink
    calls: list[str] = []

    def flaky(self, *args, **kwargs):
        calls.append(self.name)
        if self.name == "keyboard-trace-a.npz":
            raise PermissionError("in use")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky)
    trace.cleanup(tmp_path, now=now)
    monkeypatch.undo()
    assert a.exists() and not b.exists()
    assert len(calls) == 2


def test_cleanup_uses_the_real_clock_by_default(tmp_path) -> None:
    path = tmp_path / "hands"
    path.mkdir()
    old = path / "keyboard-trace-old.npz"
    old.write_bytes(b"x")
    long_ago = datetime.now(UTC) - timedelta(days=limits.TRACE_KEEP_DAYS + 2)
    os.utime(old, (long_ago.timestamp(), long_ago.timestamp()))
    new = path / "keyboard-trace-new.npz"
    new.write_bytes(b"x")
    trace.cleanup(tmp_path)
    assert not old.exists() and new.exists()


def test_the_stub_is_gone_from_the_trace_module() -> None:
    assert not hasattr(trace, "STUB_OWNER")
    with pytest.raises(AttributeError):
        trace.no_such_name  # noqa: B018
