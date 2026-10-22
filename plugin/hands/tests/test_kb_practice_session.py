"""Practice through the session (2.12.6, 5.7): the script, the drill, the tap log and the trace accessors.

``test_kb_practice.py`` drives ``PracticeScript`` alone. These tests run the whole practice: the controller's loop over
a scripted press, a practice session (no sink, nothing is typed anywhere), and a closed-loop user who taps what the
strip asks for.
"""

from __future__ import annotations

from itertools import pairwise
from typing import Any

import pytest

from jarvis_hands.keyboard.practice import PRACTICE_TEXT
from jarvis_hands.keyboard.review import REVIEW_TEXT
from jarvis_hands.keyboard.rig import KbRig, ScriptedPress

SHORT = ["ab", "cd", "ef", "gh", "ij", "kl"]


class TracingPress(ScriptedPress):
    """A scripted press that also keeps a trace, as the real ones do in practice."""

    def __init__(self, name: Any = "air") -> None:
        super().__init__(name)
        self.trace: list[dict[str, Any]] = []

    def update(self, hands: Any) -> Any:
        events = super().update(hands)
        for ev in events:  # every event leaves a fire record, as the real presses do when they trace
            self.trace.append(
                {"k": "fire", "t": ev.t, "side": ev.side, "finger": ev.finger, "depth": ev.depth, "extra": "kept"}
            )
        return events

    def take_trace(self) -> list[dict[str, Any]]:
        out, self.trace = self.trace, []
        return out


def practice_rig(**kw: Any) -> KbRig:
    kw.setdefault("commit", "review")
    kw.setdefault("keep_views", False)
    rig = KbRig(mode="practice", **kw)
    rig.arm()
    rig.run(0.2)
    return rig


def script_of(rig: KbRig):
    script = rig.session._script
    assert script is not None
    return script


def drill(rig: KbRig, *, until_segment: str = "phrase", wrong_every: int = 0) -> list[tuple[float, str, int]]:
    """A user who taps the prompted finger on the prompted key 0.3 s after each new drill prompt."""
    script = script_of(rig)
    seen: list[tuple[float, str, int]] = []
    last: tuple[str, int] | None = None
    count = 0
    while script.segment != until_segment and rig.closed is None and rig.t < 400:
        rig.run(rig.dt)
        if script.segment == "drill" and script.named is not None:
            now = (rig.view.prompt, rig.session.trace_target)
            if now != last:
                last = now
                count += 1
                seen.append((rig.t, *now))
                side, finger = script.named
                if wrong_every and count % wrong_every == 0:
                    finger = (finger + 1) % 4
                rig.tap(rig.t + 0.3, rig.layout.keys[rig.session.trace_target], side=side, finger=finger)
    return seen


def type_phrases(rig: KbRig, *, gap_s: float = 0.4, until_segment: str | None = None, seen: int = 0) -> None:
    """A user who types each phrase correctly, then rests and talks with the hands in view.

    Stops when the practice is done, or when ``until_segment`` has begun for the ``seen + 1``-th time.
    """
    script = script_of(rig)
    entered = 0
    previous = script.segment
    while not rig.session.practice_done and rig.closed is None and rig.t < 900:
        if script.segment != previous:
            previous = script.segment
            if previous == until_segment:
                entered += 1
                if entered > seen:
                    return
        if script.segment == "phrase":
            echo, prompt = rig.view.echo, rig.view.prompt
            if len(echo) < len(prompt):
                ch = prompt[len(echo)]
                rig.tap(rig.t + rig.dt, "space" if ch == " " else ch)
                rig.run(gap_s)
                continue
        rig.run(rig.dt)


# --------------------------------------------------------------------------------------------- the air practice


def test_the_air_practice_has_no_sink_sends_nothing_and_shows_its_start_message_then_the_drill() -> None:
    rig = practice_rig()
    assert rig.sink is None and rig.view.mode == "practice"
    assert rig.view.strip == PRACTICE_TEXT["start_air"]
    assert script_of(rig).segment == "intro"
    assert rig.session.trace_kind == "warm" and rig.session.trace_target == -1
    seen = drill(rig)
    assert len(seen) == 60
    assert all(r.strokes == 0 and r.steps == 0 for r in rig.records)
    assert rig.desktop.key_calls == [] and rig.box == ""


def test_the_drill_asks_for_one_finger_every_1_2_s_never_the_same_finger_twice_in_a_row() -> None:
    rig = practice_rig()
    seen = drill(rig)
    assert [round(b[0] - a[0], 1) for a, b in pairwise(seen)] == [1.2] * (len(seen) - 1)
    prompts = [p.split(",")[0] for _, p, _ in seen]
    assert all(a != b for a, b in pairwise(prompts))
    assert sum(1 for _, p, _ in seen if "," in p) == 12 and all(p.startswith("Tap: ") for _, p, _ in seen)
    assert rig.session.trace_kind == "phrase"


def test_a_perfect_drill_scores_every_prompt_and_keeps_the_reach_keys_apart() -> None:
    rig = practice_rig()
    drill(rig)
    result = rig.session.practice_result()
    assert result is not None
    assert (result.drill_prompts, result.drill_hits) == (48, 48)
    assert (result.drill_prompts_im, result.drill_hits_im) == (24, 24)
    assert (result.drill_prompts_reach, result.drill_hits_reach) == (12, 12)
    assert sorted(result.per_finger.values()) == [(6, 6)] * 8  # the reach prompts are not in per_finger
    assert result.aim_sd_u < 1e-6 and result.aim_sd_v < 1e-6
    assert result.level == "ok" and not result.completed and not rig.session.practice_done


def test_a_drill_tap_of_another_finger_is_not_a_hit_for_the_prompted_one() -> None:
    rig = practice_rig()
    drill(rig, wrong_every=10)  # every tenth prompt, six of the sixty, is answered by the next finger
    result = rig.session.practice_result()
    assert result is not None
    assert (result.drill_prompts, result.drill_hits) == (48, 42)  # all six fell on home-row prompts
    assert (result.drill_prompts_reach, result.drill_hits_reach) == (12, 12)
    assert sum(correct for _, correct in result.per_finger.values()) == 42


def test_the_whole_air_practice_with_a_perfect_user_completes_and_scores_the_phrases() -> None:
    rig = practice_rig()
    drill(rig)
    type_phrases(rig)
    result = rig.session.practice_result()
    assert result is not None and rig.session.practice_done and result.completed
    assert result.hit_rate == pytest.approx(1.0) and result.phantoms == 0
    assert (
        result.rest_s == pytest.approx(70.0, abs=0.1)
        and result.talk_s == pytest.approx(30.0, abs=0.1)
        and result.talk_phantoms == 0
    )
    assert result.fps == pytest.approx(30.0, abs=0.5)
    assert rig.desktop.key_calls == [] and rig.box == "" and rig.closed is None


def test_a_tap_in_a_rest_is_a_phantom_and_a_tap_in_the_talk_is_a_talk_phantom() -> None:
    rig = practice_rig()
    drill(rig)
    script = script_of(rig)
    type_phrases(rig, until_segment="rest")
    assert script.segment == "rest"
    rig.tap(rig.t + 0.5, "a")
    rig.run(1.0)
    result = rig.session.practice_result()
    assert result is not None and result.phantoms == 1 and result.talk_phantoms == 0
    type_phrases(rig, until_segment="talk")
    assert script.segment == "talk"
    rig.tap(rig.t + 0.5, "a")
    rig.run(1.0)
    result = rig.session.practice_result()
    assert result is not None and result.talk_phantoms == 1 and result.phantoms == 1  # not counted in `phantoms`


def test_insert_clear_chips_and_send_do_nothing_in_a_practice_and_say_so() -> None:
    rig = practice_rig()
    drill(rig)
    before = rig.counts["practice_review_key"]  # the drill's reach prompts tapped Insert, Clear and Send themselves
    for name in ("insert", "clear", "chip", "enter"):
        rig.tap(rig.t + rig.dt, name)
        rig.run(0.3)
        assert rig.view.strip == REVIEW_TEXT["practice"], name
        assert rig.box == "" and rig.desktop.key_calls == []
    assert rig.counts["practice_review_key"] == before + 4 and rig.closed is None


# ------------------------------------------------------------------------------------------------------- tap log


def test_a_live_session_has_no_tap_log_no_script_and_no_result() -> None:
    rig = KbRig(commit="review")
    rig.arm()
    rig.type_text("ab")
    assert rig.session.take_tap_log() == [] and rig.session.practice_result() is None
    assert not rig.session.practice_done and rig.session.trace_target == -1 and rig.session.trace_kind == "type"


def test_the_tap_log_finishes_each_event_with_the_sessions_verdict_numbers_and_words_only() -> None:
    rig = practice_rig()
    drill(rig)
    log = rig.session.take_tap_log()
    taps = [r for r in log if r.get("outcome") is not None]
    assert len(taps) >= 60
    hit = [r for r in taps if r.get("outcome") == "key"]
    assert hit and all(set(r) >= {"t", "side", "finger", "outcome", "u", "v", "target", "hit"} for r in hit)
    assert all(r["target"] == r["hit"] for r in hit[:50])
    assert all(isinstance(v, (int, float, str, list)) for r in taps for v in r.values())
    assert rig.session.take_tap_log() == []  # drained


def test_the_warmup_taps_are_in_the_log_with_their_outcome() -> None:
    rig = KbRig(commit="review", mode="practice", keep_views=False)
    rig.arm()
    log = rig.session.take_tap_log()
    warm = [r for r in log if r.get("outcome") == "warmup_tap"]
    assert len(warm) >= 8 and all(r["k"] == "fire" for r in warm)


def test_records_of_the_presss_trace_that_are_not_fires_pass_through_untouched() -> None:
    press = TracingPress()
    rig = KbRig(commit="review", mode="practice", scripted=press, keep_views=False)
    rig.arm()
    rig.run(0.3)
    rig.session.take_tap_log()
    press.trace += [{"k": "reject", "t": rig.t, "why": "coherence"}, {"k": "gate", "t": rig.t, "why": "speed"}]
    rig.run(0.2)
    log = rig.session.take_tap_log()
    assert {"k": "reject", "t": pytest.approx(log[0]["t"]), "why": "coherence"} in log
    assert [r["k"] for r in log] == ["reject", "gate"]


def test_a_fire_record_of_the_trace_is_finished_with_the_outcome_where_and_what_was_aimed_at() -> None:
    press = TracingPress()
    rig = KbRig(commit="review", mode="practice", scripted=press, keep_views=False)
    rig.arm()
    rig.run(0.2)
    rig.session.take_tap_log()
    script = script_of(rig)
    while script.named is None or script.segment != "drill" or rig.layout.keys[rig.session.trace_target].kind != "char":
        rig.run(rig.dt)
    side, finger = script.named
    key = rig.layout.keys[rig.session.trace_target]
    rig.tap(rig.t + 0.3, key, side=side, finger=finger)
    rig.run(0.6)
    records = [r for r in rig.session.take_tap_log() if r.get("extra") == "kept"]
    assert len(records) == 1
    record = records[0]
    assert record["k"] == "fire" and record["depth"] == 0.30 and record["outcome"] == "key"
    assert record["target"] == record["hit"] == key.index and "u" in record and "v" in record


# ------------------------------------------------------------------------------------------------ the pinch practice


def test_the_pinch_practice_types_its_phrases_in_direct_mode_and_scores_them() -> None:
    rig = KbRig(commit="direct", mode="practice", phrases=SHORT, keep_views=False)
    rig.arm()
    rig.run(0.2)
    assert rig.sink is None and rig.view.prompt == "ab" and rig.view.strip == "Phrase 1/6"
    assert rig.session.trace_kind == "phrase" and rig.session.trace_target == rig.key("a").index
    type_phrases(rig, gap_s=0.5)
    result = rig.session.practice_result()
    assert result is not None and result.completed and rig.session.practice_done
    assert result.presses == result.correct == 12 and result.hit_rate == pytest.approx(1.0)
    assert result.rest_s == pytest.approx(40.0, abs=0.1) and result.phantoms == 0
    assert rig.desktop.key_calls == [] and all(r.strokes == 0 for r in rig.records)


def test_a_wrong_key_in_a_phrase_is_a_press_that_is_not_correct() -> None:
    rig = KbRig(commit="direct", mode="practice", phrases=SHORT, keep_views=False)
    rig.arm()
    rig.run(0.2)
    rig.tap(rig.t + 0.1, "z")
    rig.run(0.5)
    rig.tap(rig.t + 0.1, "a")
    rig.run(0.5)
    result = rig.session.practice_result()
    assert result is not None and result.presses == 2 and result.correct == 1
    assert rig.view.echo == "a"
