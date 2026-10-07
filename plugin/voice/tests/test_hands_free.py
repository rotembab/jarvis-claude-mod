"""Hands-free listening through the daemon: wake word, follow-up window, barge-in by voice."""

from __future__ import annotations

import threading
from collections.abc import Iterator

import numpy as np
import pytest
from test_daemon import Rig, build, start, wait_ready

from jarvis_voice.daemon import DaemonConfig, strip_plain_wake, strip_wake_phrase
from jarvis_voice.listen.listener import RATE

from conftest import RecordingSink, wait_until
from fakes import FakeSynth, LoudnessVad, ScriptedWake


def tone(seconds: float) -> np.ndarray:
    t = np.arange(int(seconds * RATE)) / RATE
    return (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def hush(seconds: float) -> np.ndarray:
    return (np.random.default_rng(0).standard_normal(int(seconds * RATE)) * 0.001).astype(np.float32)


def hands_free(sink: RecordingSink, *, plain: bool = False, **kwargs: object) -> tuple[Rig, ScriptedWake]:
    wake = ScriptedWake(plain=plain)
    rig = start(build(sink, vad=LoudnessVad(), wake_loader=lambda: wake, **kwargs))  # type: ignore[arg-type]
    return rig, wake


def stop(rig: Rig) -> None:
    rig.daemon.request_exit(0)
    assert rig.thread is not None
    rig.thread.join(5)


@pytest.fixture
def hf(sink: RecordingSink) -> Iterator[tuple[Rig, ScriptedWake]]:
    rig, wake = hands_free(sink, stt_text="Hey Jarvis, turn on the lights.")
    yield rig, wake
    rig.daemon.request_exit(0)
    assert rig.thread is not None
    rig.thread.join(5)


def ready_with_wake(rig: Rig) -> dict[str, object]:
    return rig.sink.wait_for(lambda e: e["type"] == "ready" and e.get("wakePhrase") == "Hey Jarvis")


def test_hey_jarvis_starts_listening_and_the_words_after_it_are_sent(hf: tuple[Rig, ScriptedWake]) -> None:
    rig, wake = hf
    ready_with_wake(rig)
    rig.capture.push(hush(1.0))
    mark = rig.sink.mark()
    wake.say()
    rig.capture.push(hush(0.1))
    rig.sink.wait_type("state", after=mark, state="listening")
    effects = rig.playback.effects_played
    assert effects >= 1  # the wake chime
    rig.capture.push(tone(1.0))
    rig.capture.push(hush(0.9))
    utterance = rig.sink.wait_type("utterance", after=mark)
    assert utterance["source"] == "wake" and utterance["text"] == "turn on the lights."
    assert rig.sink.wait_type("state", after=mark, state="sleeping")


def test_ready_announces_the_wake_phrase_and_config_can_switch_it_off(hf: tuple[Rig, ScriptedWake]) -> None:
    rig, _wake = hf
    ready = ready_with_wake(rig)
    assert ready["bargeIn"] == "speech"
    mark = rig.sink.mark()
    response = rig.command("config", {"wakeWord": False, "bargeIn": "wake"})
    assert response["ok"] and set(response["applied"]) == {"wakeWord", "bargeIn"}
    again = rig.sink.wait_type("ready", after=mark)
    assert "wakePhrase" not in again and again["bargeIn"] == "wake"
    status = rig.command("status")
    assert status["bargeIn"] == "wake" and "wakeWord" not in status


def test_a_follow_up_needs_no_wake_word(hf: tuple[Rig, ScriptedWake]) -> None:
    rig, _wake = hf
    ready_with_wake(rig)
    mark = rig.sink.mark()
    rig.command("speak", {"replyId": "r1", "seq": 0, "text": "The lights are on, sir.", "final": True})
    rig.sink.wait_type("speech_done", after=mark, interrupted=False)
    rig.sink.wait_type("state", after=mark, state="awake")
    rig.capture.push(tone(1.0))
    rig.sink.wait_type("state", after=mark, state="listening")
    rig.capture.push(hush(0.9))
    utterance = rig.sink.wait_type("utterance", after=mark)
    assert utterance["source"] == "wake"


def test_the_test_line_opens_no_follow_up(hf: tuple[Rig, ScriptedWake]) -> None:
    rig, _wake = hf
    ready_with_wake(rig)
    mark = rig.sink.mark()
    rig.command("test_voice", {"text": "Testing."})
    rig.sink.wait_type("speech_done", after=mark)
    rig.capture.push(hush(0.2))
    assert "awake" not in rig.sink.states()[-3:]
    assert rig.daemon.listener is not None and rig.daemon.listener.mode == "idle"


def test_talking_over_jarvis_stops_him_and_is_heard(sink: RecordingSink) -> None:
    rig, _wake = hands_free(sink, synth=FakeSynth(ms_per_char=150), stt_text="No, the other one.")
    try:
        ready_with_wake(rig)
        rig.command("speak", {"replyId": "r9", "seq": 0, "text": "This is a rather long answer, sir.", "final": False})
        sink.wait_type("speech_started")
        wait_until(lambda: rig.playback.speech_frames_played > 0.3 * 24_000)
        mark = sink.mark()
        rig.capture.push(tone(0.4))
        barge = sink.wait_type("barge_in", after=mark)
        assert barge["replyId"] == "r9"
        sink.wait_type("speech_done", after=mark, interrupted=True)
        sink.wait_type("state", after=mark, state="listening")
        rig.capture.push(hush(0.9))
        assert sink.wait_type("utterance", after=mark)["text"] == "No, the other one."
    finally:
        rig.daemon.request_exit(0)
        assert rig.thread is not None
        rig.thread.join(5)


def test_push_to_talk_still_works_beside_the_wake_word(hf: tuple[Rig, ScriptedWake]) -> None:
    rig, _wake = hf
    ready_with_wake(rig)
    mark = rig.sink.mark()
    rig.ptt.press()
    rig.sink.wait_type("state", after=mark, state="listening")
    assert rig.daemon.listener is not None and rig.daemon.listener.mode == "paused"
    rig.capture.push(tone(0.5))  # not a follow-up or a barge-in: push-to-talk owns the microphone
    rig.ptt.release()
    utterance = rig.sink.wait_type("utterance", after=mark)
    assert utterance["source"] == "ptt" and utterance["text"] == "Hey Jarvis, turn on the lights."
    assert rig.daemon.listener is not None
    wait_until(lambda: rig.daemon.listener is not None and rig.daemon.listener.mode == "idle")


def test_a_silent_microphone_is_reported(hf: tuple[Rig, ScriptedWake]) -> None:
    rig, _wake = hf
    ready_with_wake(rig)
    assert rig.daemon.listener is not None
    rig.daemon.listener.config.silence_alarm_s = 1.0  # 30 s by default
    mark = rig.sink.mark()
    rig.capture.push(np.zeros(int(1.5 * RATE), np.float32))
    error = rig.sink.wait_type("error", after=mark)
    assert error["code"] == "mic_blocked"


def test_a_silent_clip_from_a_microphone_that_worked_is_not_reported(hf: tuple[Rig, ScriptedWake]) -> None:
    # A headset with a noise gate sends exact zeros while the user is quiet.
    rig, _wake = hf
    ready_with_wake(rig)
    listener = rig.daemon.listener
    assert listener is not None
    rig.capture.push(hush(0.1))
    wait_until(lambda: listener.heard_sound)
    mark = rig.sink.mark()
    rig.capture.next_clip = np.zeros(16_000, np.float32)
    rig.ptt.press()
    rig.sink.wait_type("state", after=mark, state="listening")
    released = rig.sink.mark()
    rig.ptt.release()
    rig.sink.wait_type("state", after=released, state="sleeping")
    assert [e for e in rig.sink.events[mark:] if e["type"] in ("error", "utterance")] == []


def test_a_missing_wake_model_is_reported_and_push_to_talk_remains(sink: RecordingSink) -> None:
    def offline() -> ScriptedWake:
        raise OSError("github.com unreachable")

    rig = start(build(sink, vad=LoudnessVad(), wake_loader=offline))
    try:
        error = sink.wait_type("error", code="wake_unavailable")
        assert "github.com unreachable" in error["message"] and "Push-to-talk" in error["hint"]
        assert "wakePhrase" not in wait_ready(rig)
    finally:
        rig.daemon.request_exit(0)
        assert rig.thread is not None
        rig.thread.join(5)


def say_plain_jarvis_then(rig: Rig, wake: ScriptedWake) -> None:
    """A second of quiet, "Jarvis," (the plain model fires as it ends), a pause, then the request."""
    wake.say_plain(at=1.5)  # in audio time: the listener's thread may still be behind
    rig.capture.push(hush(1.0))
    rig.capture.push(tone(0.5))
    rig.capture.push(hush(0.2))
    rig.capture.push(tone(1.0))
    rig.capture.push(hush(0.9))


def ready_with_plain(rig: Rig) -> dict[str, object]:
    return rig.sink.wait_for(lambda e: e["type"] == "ready" and e.get("wakePhrase") == "Jarvis")


def test_plain_jarvis_is_heard_when_the_transcript_starts_with_it(sink: RecordingSink) -> None:
    rig, wake = hands_free(sink, plain=True, config=DaemonConfig(plain_wake=True), stt_text="Jarvis, open the browser.")
    try:
        ready_with_plain(rig)
        mark = sink.mark()
        say_plain_jarvis_then(rig, wake)
        sink.wait_type("state", after=mark, state="listening")
        utterance = sink.wait_type("utterance", after=mark)
        assert utterance["source"] == "wake" and utterance["text"] == "open the browser."
        assert rig.playback.effects_played >= 2  # the start and stop chimes, as for "Hey Jarvis"
    finally:
        stop(rig)


def test_plain_jarvis_is_dropped_when_the_transcript_does_not_start_with_it(sink: RecordingSink) -> None:
    rig, wake = hands_free(sink, plain=True, config=DaemonConfig(plain_wake=True), stt_text="Travis, open the browser.")
    try:
        ready_with_plain(rig)
        mark = sink.mark()
        say_plain_jarvis_then(rig, wake)
        sink.wait_type("state", after=mark, state="transcribing")
        sink.wait_type("state", after=mark, state="sleeping")
        assert rig.transcribers[0].calls  # transcribed, then dropped
        assert [e for e in sink.events[mark:] if e["type"] in ("utterance", "error")] == []
    finally:
        stop(rig)


def test_a_plain_jarvis_trigger_never_cuts_jarvis_off(sink: RecordingSink) -> None:
    rig, _wake = hands_free(sink, plain=True, config=DaemonConfig(plain_wake=True), synth=FakeSynth(ms_per_char=150))
    try:
        ready_with_plain(rig)
        rig.command("speak", {"replyId": "r7", "seq": 0, "text": "This is a rather long answer, sir.", "final": False})
        sink.wait_type("speech_started")
        wait_until(lambda: rig.playback.speech_frames_played > 0.3 * 24_000)
        mark = sink.mark()
        listener = rig.daemon.listener
        assert listener is not None
        # The listener let one through just before he became audible, and is recording it.
        with listener._lock:
            listener._start_locked("jarvis")
        assert listener.mode == "record"
        rig.daemon._on_listener_start("jarvis")
        rig.daemon._call_in_loop(dict)  # after the queued start
        assert rig.daemon.state == "speaking"
        assert listener.mode == "idle"  # the recording was dropped
        assert [e for e in sink.events[mark:] if e["type"] in ("barge_in", "speech_done")] == []
    finally:
        stop(rig)


def test_config_switches_plain_jarvis_and_ready_says_which_phrase(sink: RecordingSink) -> None:
    rig, _wake = hands_free(sink, plain=True)
    try:
        ready_with_wake(rig)  # off by default: "Hey Jarvis" only
        mark = sink.mark()
        response = rig.command("config", {"wakeWord": True, "plainWake": True})
        assert response["ok"] and set(response["applied"]) == {"wakeWord", "plainWake"}
        sink.wait_for(lambda e: e["type"] == "ready" and e.get("wakePhrase") == "Jarvis", after=mark)
        assert rig.command("status")["wakeWord"] == "Jarvis"
        mark = sink.mark()
        rig.command("config", {"plainWake": False})
        sink.wait_for(lambda e: e["type"] == "ready" and e.get("wakePhrase") == "Hey Jarvis", after=mark)
        assert [e for e in sink.events[mark:] if e["type"] == "error"] == []
    finally:
        stop(rig)


def test_a_missing_plain_jarvis_model_is_reported_and_hey_jarvis_remains(sink: RecordingSink) -> None:
    rig, _wake = hands_free(sink)  # the wake word loaded without the plain model
    try:
        ready_with_wake(rig)
        mark = sink.mark()
        assert rig.command("config", {"plainWake": True})["ok"]
        error = sink.wait_type("error", after=mark, code="wake_unavailable")
        assert 'plain "Jarvis"' in error["message"] and '"Hey Jarvis" still works' in error["hint"]
        assert rig.command("status")["wakeWord"] == "Hey Jarvis"
    finally:
        stop(rig)


def test_plain_jarvis_wanted_from_the_start_without_its_model_is_reported(sink: RecordingSink) -> None:
    rig, _wake = hands_free(sink, config=DaemonConfig(plain_wake=True))
    try:
        error = sink.wait_type("error", code="wake_unavailable")
        assert 'plain "Jarvis"' in error["message"]
        assert ready_with_wake(rig)["wakePhrase"] == "Hey Jarvis"
    finally:
        stop(rig)


def plain_errors(sink: RecordingSink) -> list[dict[str, object]]:
    return [e for e in sink.events if e["type"] == "error" and 'plain "Jarvis"' in str(e.get("message"))]


def test_a_missing_plain_jarvis_model_is_reported_once(sink: RecordingSink) -> None:
    rig, _wake = hands_free(sink, config=DaemonConfig(plain_wake=True))
    try:
        sink.wait_type("error", code="wake_unavailable")
        for _ in range(2):  # /jarvis wake jarvis, again
            assert rig.command("config", {"plainWake": True})["ok"]
        rig.command("status")
        assert len(plain_errors(sink)) == 1
    finally:
        stop(rig)


def test_hey_jarvis_is_live_while_the_plain_model_is_still_on_its_way(sink: RecordingSink) -> None:
    arrived = threading.Event()
    fetched: list[ScriptedWake] = []

    def fetch_plain(scorer: ScriptedWake) -> None:  # a slow host
        fetched.append(scorer)
        assert arrived.wait(5)
        scorer.attach_plain()

    rig, wake = hands_free(sink, plain_loader=fetch_plain, config=DaemonConfig(plain_wake=True))
    try:
        ready_with_wake(rig)  # "Hey Jarvis" works before the plain model is there
        wait_until(lambda: fetched == [wake])
        assert rig.command("status")["wakeWord"] == "Hey Jarvis" and plain_errors(sink) == []
        arrived.set()
        ready_with_plain(rig)
        assert rig.command("status")["wakeWord"] == "Jarvis"
    finally:
        arrived.set()
        stop(rig)


def test_the_plain_model_is_fetched_only_once_plain_jarvis_is_switched_on(sink: RecordingSink) -> None:
    fetched: list[ScriptedWake] = []

    def fetch_plain(scorer: ScriptedWake) -> None:
        fetched.append(scorer)
        scorer.attach_plain()

    rig, wake = hands_free(sink, plain_loader=fetch_plain)
    try:
        ready_with_wake(rig)
        rig.command("config", {"wakeWord": True, "bargeIn": "wake"})
        rig.command("status")
        assert fetched == []  # "Hey Jarvis" only: nothing to fetch
        mark = sink.mark()
        assert rig.command("config", {"plainWake": True})["ok"]
        sink.wait_for(lambda e: e["type"] == "ready" and e.get("wakePhrase") == "Jarvis", after=mark)
        assert fetched == [wake] and plain_errors(sink) == []
        rig.command("config", {"plainWake": False})
        rig.command("config", {"plainWake": True})
        assert fetched == [wake]  # loaded once
    finally:
        stop(rig)


def test_switching_plain_jarvis_on_again_during_its_fetch_starts_no_second_one(sink: RecordingSink) -> None:
    # Two fetches would write the same jarvis_v2.onnx.part.
    arrived = threading.Event()
    fetched: list[ScriptedWake] = []

    def fetch_plain(scorer: ScriptedWake) -> None:  # a slow host
        fetched.append(scorer)
        assert arrived.wait(5)
        scorer.attach_plain()

    def fetching() -> list[threading.Thread]:
        return [t for t in threading.enumerate() if t.name == "plain-wake-load"]

    rig, wake = hands_free(sink, plain_loader=fetch_plain)
    try:
        ready_with_wake(rig)
        assert rig.command("config", {"plainWake": True})["ok"]
        wait_until(lambda: fetched == [wake])
        assert rig.command("config", {"plainWake": True})["ok"]  # /jarvis wake jarvis, again
        assert len(fetching()) == 1
        arrived.set()
        ready_with_plain(rig)
        for thread in fetching():
            thread.join(5)
        assert fetched == [wake] and plain_errors(sink) == []
    finally:
        arrived.set()
        stop(rig)


def test_a_plain_model_that_cannot_be_fetched_is_reported_once_and_hey_jarvis_remains(sink: RecordingSink) -> None:
    tries: list[ScriptedWake] = []

    def offline(scorer: ScriptedWake) -> None:
        tries.append(scorer)
        raise OSError("raw.githubusercontent.com unreachable")

    rig, _wake = hands_free(sink, plain_loader=offline, config=DaemonConfig(plain_wake=True))
    try:
        error = sink.wait_type("error", code="wake_unavailable")
        assert "raw.githubusercontent.com unreachable" in error["message"]
        assert '"Hey Jarvis" still works' in error["hint"]
        assert ready_with_wake(rig)["wakePhrase"] == "Hey Jarvis"
        wait_until(lambda: not rig.daemon._plain_loading)  # the first try is over
        rig.command("config", {"plainWake": True})  # switched on again: one more try, no second message
        wait_until(lambda: len(tries) == 2)
        wait_until(lambda: not rig.daemon._plain_loading)
        rig.command("status")
        assert len(plain_errors(sink)) == 1 and rig.command("status")["wakeWord"] == "Hey Jarvis"
    finally:
        stop(rig)


def test_without_hands_free_nothing_changes(sink: RecordingSink) -> None:
    rig = start(build(sink))
    try:
        assert rig.daemon.listener is None
        assert "wakePhrase" not in wait_ready(rig) and "bargeIn" not in rig.command("status")
    finally:
        rig.daemon.request_exit(0)
        assert rig.thread is not None
        rig.thread.join(5)


@pytest.mark.parametrize(
    ("heard", "meant"),
    [
        ("Hey Jarvis, what time is it?", "what time is it?"),
        ("hey, jarvis. Lights on.", "Lights on."),
        ("Jarvis, stop.", "stop."),
        ("Okay Jarvis open the logs", "open the logs"),
        ("Tell Jarvis to wait", "Tell Jarvis to wait"),
        ("Hey Jarvis.", ""),
        ("Okay, hey Jarvis, open the logs.", "open the logs."),
        # One or two short words before "Hey Jarvis" (kept by a longer pre-roll) go too.
        ("So, hey Jarvis, open the logs.", "open the logs."),
        ("Um, uh, hey Jarvis, lights on.", "lights on."),
        ("So Jarvis, open it.", "So Jarvis, open it."),  # no greeting before the name: not the wake word
        ("I think that the hey Jarvis model works", "I think that the hey Jarvis model works"),
    ],
)
def test_strip_wake_phrase(heard: str, meant: str) -> None:
    assert strip_wake_phrase(heard, woken=True) == meant


@pytest.mark.parametrize(
    ("heard", "meant"),
    [
        ("Hey Jarvis, stop.", "stop."),
        ("Jarvis, wait.", "wait."),
        # Words before "Jarvis" in a follow-up or a barge-in are the request.
        ("No, it's ok Jarvis, I've got it.", "No, it's ok Jarvis, I've got it."),
        ("Stop, hey Jarvis, wait.", "Stop, hey Jarvis, wait."),
        ("That's okay Jarvis, never mind.", "That's okay Jarvis, never mind."),
        ("Say hello Jarvis.", "Say hello Jarvis."),
        ("So, hey Jarvis, open the logs.", "So, hey Jarvis, open the logs."),
    ],
)
def test_strip_wake_phrase_after_a_follow_up_or_barge_in(heard: str, meant: str) -> None:
    assert strip_wake_phrase(heard) == meant


@pytest.mark.parametrize(
    ("plain_model", "plain_on", "meant"),
    [
        # Plain "Jarvis" on: at the start of an utterance the clip keeps all of the
        # speech, so the words before "Hey Jarvis" are in it and go.
        (True, True, "open the logs."),
        # "Hey Jarvis" only (with the plain model on disk or not): the short pre-roll,
        # and the transcript is stripped exactly as before plain "Jarvis".
        (True, False, "So, hey Jarvis, open the logs."),
        (False, False, "So, hey Jarvis, open the logs."),
    ],
)
def test_words_before_hey_jarvis_are_dropped_only_when_its_clip_reaches_back_to_them(
    sink: RecordingSink, plain_model: bool, plain_on: bool, meant: str
) -> None:
    rig, wake = hands_free(
        sink, plain=plain_model, config=DaemonConfig(plain_wake=plain_on), stt_text="So, hey Jarvis, open the logs."
    )
    try:
        phrase = "Jarvis" if plain_on else "Hey Jarvis"
        sink.wait_for(lambda e: e["type"] == "ready" and e.get("wakePhrase") == phrase)
        mark = sink.mark()
        wake.say(at=1.4)  # in audio time: as "So, hey Jarvis" ends, at the start of an utterance
        rig.capture.push(hush(1.0))
        rig.capture.push(tone(0.5))
        rig.capture.push(hush(0.2))
        rig.capture.push(tone(1.0))
        rig.capture.push(hush(0.9))
        assert sink.wait_type("utterance", after=mark)["text"] == meant
        listener = rig.daemon.listener
        assert listener is not None and listener.long_preroll == plain_on
    finally:
        stop(rig)


def test_a_follow_up_keeps_the_words_before_jarvis(sink: RecordingSink) -> None:
    rig, _wake = hands_free(sink, stt_text="No, it's ok Jarvis, I've got it.")
    try:
        ready_with_wake(rig)
        mark = sink.mark()
        rig.command("speak", {"replyId": "r1", "seq": 0, "text": "Shall I restart it, sir?", "final": True})
        sink.wait_type("state", after=mark, state="awake")
        rig.capture.push(tone(1.0))
        rig.capture.push(hush(0.9))
        assert sink.wait_type("utterance", after=mark)["text"] == "No, it's ok Jarvis, I've got it."
    finally:
        stop(rig)


def test_a_barge_in_keeps_the_words_before_hey_jarvis(sink: RecordingSink) -> None:
    rig, _wake = hands_free(sink, synth=FakeSynth(ms_per_char=150), stt_text="Stop, hey Jarvis, wait.")
    try:
        ready_with_wake(rig)
        rig.command("speak", {"replyId": "r9", "seq": 0, "text": "This is a rather long answer, sir.", "final": False})
        sink.wait_type("speech_started")
        wait_until(lambda: rig.playback.speech_frames_played > 0.3 * 24_000)
        mark = sink.mark()
        rig.capture.push(tone(0.4))
        sink.wait_type("barge_in", after=mark)
        rig.capture.push(hush(0.9))
        assert sink.wait_type("utterance", after=mark)["text"] == "Stop, hey Jarvis, wait."
    finally:
        stop(rig)


@pytest.mark.parametrize(
    ("heard", "meant"),
    [
        ("Jarvis, open the browser.", "open the browser."),
        ("Jarvis open the browser", "open the browser"),
        ("Okay. Jarvis, what time is it?", "what time is it?"),
        ("Hey Jarvis, lights on.", "lights on."),
        ("Jarvas, lights on.", "lights on."),  # one slip, still a "j"
        ("Jervis lights on", "lights on"),
        ("Jarvis.", ""),
        ("Travis, open the browser.", None),
        ("Harvis, lights on.", None),  # one slip from "jarvis", but no "j"
        ("Marvis open it", None),
        ("Service is down.", None),
        ("Davis said the servers are fine.", None),
        ("Jars of jam.", None),
        ("The jars are full.", None),
        ("So Jarvis, open it.", None),  # not at the start
        ("So, hey Jarvis, open it.", None),  # a plain clip allows no other word first
        ("", None),
    ],
)
def test_strip_plain_wake(heard: str, meant: str | None) -> None:
    assert strip_plain_wake(heard) == meant


def test_config_defaults() -> None:
    config = DaemonConfig()
    assert config.wake_word and config.barge_in == "speech" and config.follow_up_s == 8.0
    assert not config.plain_wake
