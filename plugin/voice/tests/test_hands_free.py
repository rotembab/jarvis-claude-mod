"""Hands-free listening through the daemon: wake word, follow-up window, barge-in by voice."""

from __future__ import annotations

from collections.abc import Iterator

import numpy as np
import pytest
from test_daemon import Rig, build, start, wait_ready

from jarvis_voice.daemon import DaemonConfig, strip_wake_phrase
from jarvis_voice.listen.listener import RATE

from conftest import RecordingSink, wait_until
from fakes import FakeSynth, LoudnessVad, ScriptedWake


def tone(seconds: float) -> np.ndarray:
    t = np.arange(int(seconds * RATE)) / RATE
    return (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def hush(seconds: float) -> np.ndarray:
    return (np.random.default_rng(0).standard_normal(int(seconds * RATE)) * 0.001).astype(np.float32)


def hands_free(sink: RecordingSink, **kwargs: object) -> tuple[Rig, ScriptedWake]:
    wake = ScriptedWake()
    rig = start(build(sink, vad=LoudnessVad(), wake_loader=lambda: wake, **kwargs))  # type: ignore[arg-type]
    return rig, wake


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
    mark = rig.sink.mark()
    rig.capture.push(np.zeros(int(3.5 * RATE), np.float32))
    error = rig.sink.wait_type("error", after=mark)
    assert error["code"] == "mic_blocked"


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
    ],
)
def test_strip_wake_phrase(heard: str, meant: str) -> None:
    assert strip_wake_phrase(heard) == meant


def test_config_defaults() -> None:
    config = DaemonConfig()
    assert config.wake_word and config.barge_in == "speech" and config.follow_up_s == 8.0
