"""Hands-free listening through the daemon: wake word, follow-up window, barge-in by voice."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from test_daemon import Rig, build, start, wait_ready
from test_listener import read_wav

from jarvis_voice.audio.echo import synthetic_speech
from jarvis_voice.audio.fake import FakeCapture, FakePlayback
from jarvis_voice.audio.playback import TTS_SAMPLERATE
from jarvis_voice.daemon import DaemonConfig, strip_plain_wake, strip_wake_phrase
from jarvis_voice.listen.listener import RATE

from conftest import RecordingSink, wait_until
from fakes import FakeSynth, LoudnessVad, ScriptedWake

DATA = Path(__file__).parent / "data"


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


# -- echo cancelling: Jarvis through speakers ----------------------------------------------------


class EchoRoom(FakePlayback):
    """Speakers and a microphone in one room. The microphone hears what played ``delay_s``
    earlier at half volume, plus whatever the user says (``say``).

    The microphone runs on the speaker's clock, in its render thread, so every
    block reaches the listener after the speaker block it echoes, as with a
    real sound card. Sound reaches it steadily: the room's resampler ("QQ")
    hands back each block's worth at once, where the default quality would
    hold audio back and release it in bursts.
    """

    def __init__(self, capture: FakeCapture, *, delay_s: float = 0.06, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        import soxr

        self.capture = capture
        self._to_16k = soxr.ResampleStream(TTS_SAMPLERATE, RATE, 1, dtype="float32", quality="QQ")
        self._path = np.zeros(round(delay_s * RATE), np.float32)  # from the speaker to the microphone
        self._user = np.zeros(0, np.float32)
        self._lock = threading.Lock()
        self._hiss = np.random.default_rng(5)

    def say(self, audio: np.ndarray) -> None:
        """The user speaks (16 kHz); the microphone hears it from the next block on."""
        with self._lock:
            self._user = np.concatenate([self._user, audio])

    def _render_block(self) -> np.ndarray:
        out = super()._render_block()  # which also hands it to the echo canceller
        echo = 0.5 * np.asarray(self._to_16k.resample_chunk(out), np.float32)
        self._path = np.concatenate([self._path, echo])
        n = RATE // 100
        mic = np.zeros(n, np.float32)
        heard, self._path = self._path[:n], self._path[n:]
        mic[: heard.size] += heard
        with self._lock:
            user, self._user = self._user[:n], self._user[n:]
        mic[: user.size] += user
        mic += (self._hiss.standard_normal(n) * 1e-4).astype(np.float32)
        listener = self.capture.listener
        if listener is not None:
            listener(mic, RATE)
        return out


def babble(samples: int) -> np.ndarray:
    """Jarvis's voice for the room: speech-like, about -15 dBFS, with no pauses for the echo to hide in."""
    return 2.0 * synthetic_speech(samples / TTS_SAMPLERATE, seed=11, rate=TTS_SAMPLERATE, floor=0.4)


def in_a_room(sink: RecordingSink, *, echo_cancelling: bool, delay_s: float) -> tuple[Rig, EchoRoom]:
    pytest.importorskip("livekit.rtc")
    from jarvis_voice.audio.echo import loader

    capture = FakeCapture()
    room = EchoRoom(capture, delay_s=delay_s, speed=4.0)
    # What the devices report, close to the path itself: the delay hint the helper builds from them.
    room.output_latency, capture.input_latency = 0.6 * delay_s, 0.4 * delay_s  # type: ignore[misc]

    rig = start(
        build(
            sink,
            capture=capture,
            playback=room,
            synth=FakeSynth(ms_per_char=100, voice=babble),
            vad=LoudnessVad(),
            wake_loader=ScriptedWake,
            echo_loader=loader(room, capture) if echo_cancelling else None,
            stt_text="What's the weather today?",
        )
    )
    ready_with_wake(rig)
    wait_until(lambda: rig.command("status")["echoCancel"] == ("on" if echo_cancelling else "off"))
    return rig, room


def stop(rig: Rig) -> None:
    rig.daemon.request_exit(0)
    assert rig.thread is not None
    rig.thread.join(5)


LONG_REPLY = "Certainly, sir. The forecast calls for light rain this evening, clearing by the morning."  # 8.8 s


ROOM_PATHS = pytest.mark.parametrize("delay_s", [0.02, 0.03, 0.06], ids=["20ms", "30ms", "60ms"])


@ROOM_PATHS
def test_without_echo_cancelling_jarvis_interrupts_himself(sink: RecordingSink, delay_s: float) -> None:
    # The rig is a fair test: through the speakers, Jarvis's own voice sounds like the user talking over him.
    rig, _room = in_a_room(sink, echo_cancelling=False, delay_s=delay_s)
    try:
        mark = sink.mark()
        rig.command("speak", {"replyId": "r1", "seq": 0, "text": LONG_REPLY, "final": True})
        assert sink.wait_type("barge_in", after=mark)["replyId"] == "r1"
        sink.wait_type("speech_done", after=mark, interrupted=True)
    finally:
        stop(rig)


@ROOM_PATHS
def test_echo_cancelling_keeps_jarvis_from_hearing_himself(sink: RecordingSink, delay_s: float) -> None:
    # A close speaker (20-30 ms) is the hard case: a far end that reached the canceller in bursts, behind
    # its own echo, held for a few seconds and then let Jarvis interrupt himself.
    rig, room = in_a_room(sink, echo_cancelling=True, delay_s=delay_s)
    try:
        mark = sink.mark()
        rig.command("speak", {"replyId": "r1", "seq": 0, "text": LONG_REPLY, "final": True})
        sink.wait_type("speech_done", after=mark, interrupted=False, timeout=10)
        assert [e for e in sink.events[mark:] if e["type"] in ("barge_in", "utterance")] == []

        # The user still gets through: talking over the next reply stops it.
        rig.command("speak", {"replyId": "r2", "seq": 0, "text": LONG_REPLY, "final": True})
        sink.wait_type("speech_started", after=mark, replyId="r2")
        wait_until(lambda: room.speech_frames_played > 1.0 * TTS_SAMPLERATE)
        talk = sink.mark()
        room.say(2.0 * read_wav(DATA / "hey-jarvis-weather.wav"))
        assert sink.wait_type("barge_in", after=talk)["replyId"] == "r2"
        sink.wait_type("speech_done", after=talk, replyId="r2", interrupted=True)
        assert sink.wait_type("utterance", after=talk, timeout=10)["text"] == "What's the weather today?"
    finally:
        stop(rig)


def test_echo_cancelling_that_cannot_load_is_reported_and_hands_free_remains(sink: RecordingSink) -> None:
    def blocked() -> Any:
        raise OSError("[WinError 4551] An Application Control policy has blocked this file")

    rig = start(build(sink, vad=LoudnessVad(), wake_loader=ScriptedWake, echo_loader=blocked))
    try:
        error = sink.wait_type("error", code="aec_unavailable")
        assert "Application Control" in error["message"] and error["fatal"] is False and error["hint"]
        ready_with_wake(rig)
        assert rig.command("status")["echoCancel"] == "unavailable"
    finally:
        stop(rig)


class RecordingEcho:
    def __init__(self) -> None:
        self.far_blocks = 0
        self.near_blocks = 0
        self.closed = False
        self.on_failed: Any = None

    def set_on_failed(self, callback: Any) -> None:
        self.on_failed = callback

    def far(self, block: np.ndarray | None, rate: int) -> None:
        self.far_blocks += block is not None

    def near(self, audio: np.ndarray, stamp: float) -> np.ndarray:
        self.near_blocks += 1
        return audio

    def close(self) -> None:
        self.closed = True


def test_the_echo_canceller_is_wired_in_and_freed_at_shutdown(sink: RecordingSink) -> None:
    echo = RecordingEcho()
    rig = start(build(sink, vad=LoudnessVad(), wake_loader=ScriptedWake, echo_loader=lambda: echo))
    try:
        wait_until(lambda: rig.command("status")["echoCancel"] == "on")
        rig.capture.push(hush(0.1))
        wait_until(lambda: echo.far_blocks > 0 and echo.near_blocks > 0)  # the speaker's blocks, the mic's blocks
    finally:
        stop(rig)
    assert echo.closed and rig.playback._far_listener is None


def test_echo_cancelling_that_stops_working_is_reported_once_and_unhooked(sink: RecordingSink) -> None:
    from test_echo import FakeApm, FakeFrame

    from jarvis_voice.audio.echo import WebRtcEchoCanceller

    apm = FakeApm(fail_on=3)
    canceller = WebRtcEchoCanceller(new_apm=lambda: apm, new_frame=FakeFrame)
    rig = start(build(sink, vad=LoudnessVad(), wake_loader=ScriptedWake, echo_loader=lambda: canceller))
    try:
        wait_until(lambda: rig.command("status")["echoCancel"] == "on")
        mark = sink.mark()
        rig.capture.push(hush(0.1))  # the canceller fails on its fourth microphone frame
        error = sink.wait_type("error", after=mark, code="aec_unavailable")
        assert "apm_process_stream failed" in error["message"] and error["fatal"] is False
        # It loaded and ran: reinstalling would not help, and Smart App Control is not to blame.
        assert "/jarvis restart" in error["hint"] and "/jarvis bargein wake" in error["hint"]
        assert "setup" not in error["hint"] and "livekit_ffi" not in error["hint"]
        wait_until(lambda: rig.command("status")["echoCancel"] == "unavailable")
        assert rig.playback._far_listener is None and canceller._apm is None  # unhooked and freed
        listener = rig.daemon.listener
        assert listener is not None and listener._echo is None
        heard = listener._t
        rig.capture.push(hush(0.5))  # the microphone still reaches the listener, as it is
        wait_until(lambda: listener._t >= heard + 0.5)
        assert [e["code"] for e in sink.events[mark:] if e["type"] == "error"] == ["aec_unavailable"]  # once
    finally:
        stop(rig)


def test_no_echo_canceller_without_hands_free(sink: RecordingSink) -> None:
    loads: list[int] = []
    rig = start(build(sink, echo_loader=lambda: loads.append(1)))
    try:
        wait_ready(rig)
        assert rig.command("status")["echoCancel"] == "off" and loads == []
    finally:
        stop(rig)
