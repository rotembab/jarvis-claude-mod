"""The hands-free listener: wake word, end of speech, follow-up window, barge-in."""

from __future__ import annotations

import os
import time
import wave
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from jarvis_voice.listen.listener import RATE, Listener, ListenerConfig

from conftest import wait_until
from fakes import LoudnessVad, ScriptedWake

DATA = Path(__file__).parent / "data"


def tone(seconds: float, amp: float = 0.3) -> np.ndarray:
    t = np.arange(int(seconds * RATE)) / RATE
    return (amp * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def hush(seconds: float) -> np.ndarray:
    return (np.random.default_rng(0).standard_normal(int(seconds * RATE)) * 0.001).astype(np.float32)


class Rig:
    def __init__(self, **kwargs: Any) -> None:
        self.wake = ScriptedWake()
        self.playing = True
        self.events: list[tuple[str, Any]] = []
        self.listener = Listener(
            ListenerConfig(),
            vad=LoudnessVad(),
            wake=self.wake,
            on_start=lambda kind: self.events.append(("start", kind)),
            on_end=lambda pcm, kind: self.events.append(("end", (None if pcm is None else pcm.size / RATE, kind))),
            on_follow_up=lambda opened: self.events.append(("follow", opened)),
            on_digital_silence=lambda silent: self.events.append(("silence", silent)),
            audio_playing=lambda: self.playing,
            **kwargs,
        )

    def play(self, audio: np.ndarray) -> None:
        for i in range(0, audio.size, 160):  # 10 ms blocks, like the sound card
            self.listener.process(audio[i : i + 160])

    def kinds(self, name: str) -> list[Any]:
        return [value for event, value in self.events if event == name]


def test_wake_word_then_speech_ends_after_the_silence() -> None:
    rig = Rig()
    rig.play(hush(1.0))
    rig.wake.say()
    rig.play(hush(0.08))
    assert rig.kinds("start") == ["wake"] and rig.listener.mode == "record"
    rig.play(tone(1.0))
    rig.play(hush(0.5))
    assert rig.kinds("end") == []  # not yet: 0.7 s of silence ends it
    rig.play(hush(0.3))
    [(seconds, kind)] = rig.kinds("end")
    assert kind == "wake" and 0.3 + 1.0 + 0.7 <= seconds <= 0.3 + 1.0 + 0.8
    assert rig.listener.mode == "idle"


def test_a_pause_after_the_wake_word_does_not_end_it() -> None:
    rig = Rig()
    rig.play(hush(1.0))
    rig.wake.say()
    rig.play(tone(0.16))  # the tail of "Jarvis"
    rig.play(hush(1.5))  # "Hey Jarvis" ... thinking ...
    assert rig.kinds("end") == []
    rig.play(tone(0.8))
    rig.play(hush(0.8))
    [(seconds, kind)] = rig.kinds("end")
    assert kind == "wake" and seconds > 2.5


def test_wake_word_and_nothing_else_is_dropped_after_five_seconds() -> None:
    rig = Rig()
    rig.play(hush(1.0))
    rig.wake.say()
    rig.play(hush(4.9))
    assert rig.kinds("end") == []
    rig.play(hush(0.2))
    assert rig.kinds("end") == [(None, "wake")]


def test_speech_alone_does_not_wake_jarvis() -> None:
    rig = Rig()
    rig.play(tone(2.0))
    rig.play(hush(1.0))
    assert rig.events == []


def test_follow_up_window_takes_speech_without_the_wake_word() -> None:
    rig = Rig()
    rig.play(hush(0.5))
    rig.listener.open_follow_up()
    assert rig.kinds("follow") == [True] and rig.listener.mode == "follow"
    rig.play(hush(2.0))
    rig.play(tone(1.0))
    assert rig.kinds("start") == ["follow"]
    assert rig.kinds("follow") == [True, False]
    rig.play(hush(0.8))
    [(seconds, kind)] = rig.kinds("end")
    # The speech that opened it is kept, with half a second before it.
    assert kind == "follow" and seconds >= 0.5 + 1.0 + 0.7


def test_follow_up_window_closes_after_eight_seconds() -> None:
    rig = Rig()
    rig.listener.open_follow_up()
    rig.play(hush(8.1))
    assert rig.kinds("follow") == [True, False] and rig.listener.mode == "idle"
    rig.play(tone(1.0))
    assert rig.kinds("start") == []


def test_talking_over_jarvis_barges_in_after_a_fifth_of_a_second() -> None:
    rig = Rig()
    rig.listener.set_speaking(True)
    rig.play(hush(0.5))
    rig.play(tone(0.15))
    assert rig.kinds("start") == []
    rig.play(tone(0.1))
    assert rig.kinds("start") == ["barge"]


def test_speech_during_a_silent_gap_in_the_reply_is_not_a_barge_in() -> None:
    rig = Rig()
    rig.listener.set_speaking(True)
    rig.playing = False  # the reply is open, but Jarvis is quiet (Claude is running a tool)
    rig.play(tone(1.0))
    assert rig.kinds("start") == []


@pytest.mark.parametrize(("mode", "speech_starts", "wake_starts"), [("wake", [], ["wake"]), ("off", [], [])])
def test_barge_in_modes(mode: str, speech_starts: list[str], wake_starts: list[str]) -> None:
    rig = Rig(barge_mode=mode)
    rig.listener.set_speaking(True)
    rig.play(tone(1.0))
    assert rig.kinds("start") == speech_starts
    rig.play(hush(0.5))
    rig.wake.say()
    rig.play(hush(0.1))
    assert rig.kinds("start") == wake_starts


def test_push_to_talk_pauses_the_listener() -> None:
    rig = Rig()
    rig.listener.pause()
    rig.wake.say()
    rig.play(hush(0.2))
    assert rig.kinds("start") == []
    rig.listener.resume()
    rig.play(hush(0.5))
    assert rig.wake.resets == 1  # fresh state after push-to-talk
    rig.wake.say()
    rig.play(hush(0.1))
    assert rig.kinds("start") == ["wake"]


def test_wake_word_switched_off() -> None:
    rig = Rig(wake_enabled=False)
    rig.wake.say()
    rig.play(hush(0.2))
    assert rig.kinds("start") == [] and not rig.listener.wake_ready


def test_long_utterances_are_cut_at_thirty_seconds() -> None:
    rig = Rig()
    rig.wake.say()
    rig.play(hush(0.1))
    rig.play(tone(30.5))
    [(seconds, kind)] = rig.kinds("end")
    assert kind == "wake" and 30.0 <= seconds <= 30.5


def test_a_microphone_that_never_sent_sound_is_reported_once_and_cleared() -> None:
    rig = Rig()
    rig.play(np.zeros(int(29.9 * RATE), np.float32))
    assert rig.kinds("silence") == [] and not rig.listener.heard_sound
    rig.play(np.zeros(int(1.0 * RATE), np.float32))
    assert rig.kinds("silence") == [True]
    rig.play(np.zeros(int(40.0 * RATE), np.float32))
    assert rig.kinds("silence") == [True]
    rig.play(hush(0.1))
    assert rig.kinds("silence") == [True, False] and rig.listener.heard_sound


def test_a_noise_gate_is_not_mistaken_for_a_muted_microphone() -> None:
    # Some headsets send exact zeros whenever the user is quiet.
    rig = Rig()
    for _ in range(3):
        rig.play(hush(0.5))
        rig.play(np.zeros(int(40.0 * RATE), np.float32))
    assert rig.kinds("silence") == []


def test_the_thread_resamples_sound_card_blocks() -> None:
    rig = Rig()
    rig.listener.start()
    try:
        t = np.arange(int(1.0 * 48_000)) / 48_000
        loud = (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
        quiet = np.zeros(48_000, np.float32) + 1e-3
        rig.wake.say()
        for audio in (quiet[:4800], loud, quiet):
            for i in range(0, audio.size, 480):
                rig.listener.feed(audio[i : i + 480], 48_000)
        wait_until(lambda: rig.kinds("end") != [])
        [(seconds, kind)] = rig.kinds("end")
        assert kind == "wake" and seconds > 1.0
    finally:
        rig.listener.close()


# -- the real models, on synthetic speech (espeak-ng) ---------------------------------------------


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as wav:
        assert wav.getframerate() == RATE and wav.getnchannels() == 1 and wav.getsampwidth() == 2
        return np.frombuffer(wav.readframes(wav.getnframes()), "<i2").astype(np.float32) / 32768.0


@pytest.fixture(scope="module")
def wake_models() -> Path:
    folder = os.environ.get("JARVIS_WAKE_MODELS_DIR")
    if not folder:
        pytest.skip("set JARVIS_WAKE_MODELS_DIR to a folder with the wake word models")
    from jarvis_voice.listen.models import download, is_downloaded

    if not is_downloaded(Path(folder)):
        download(Path(folder))
    return Path(folder)


def test_silero_finds_the_speech_in_a_clip() -> None:
    from jarvis_voice.listen.vad import SileroVad

    vad = SileroVad()
    audio = read_wav(DATA / "hey-jarvis-weather.wav")
    probs = np.array(vad.process(audio))
    voiced = np.flatnonzero(probs >= 0.5) * 0.032
    assert 1.5 < voiced[-1] - voiced[0] < 3.5  # about 2.7 s of speech
    vad.reset()
    assert max(vad.process(hush(2.0))) < 0.3


def test_hey_jarvis_wakes_and_the_question_is_captured(wake_models: Path) -> None:
    from jarvis_voice.listen.vad import SileroVad
    from jarvis_voice.listen.wakeword import OpenWakeWord

    events: list[tuple[str, Any]] = []
    listener = Listener(
        ListenerConfig(),
        vad=SileroVad(),
        wake=OpenWakeWord(wake_models),
        on_start=lambda kind: events.append(("start", kind)),
        on_end=lambda pcm, kind: events.append(("end", None if pcm is None else pcm.size / RATE)),
    )
    audio = np.concatenate([hush(1.5), read_wav(DATA / "hey-jarvis-weather.wav"), hush(1.0)])
    started = time.perf_counter()
    for i in range(0, audio.size, 160):
        listener.process(audio[i : i + 160])
    took = time.perf_counter() - started
    assert [e for e, _ in events] == ["start", "end"]
    assert events[1][1] is not None and events[1][1] > 1.0  # "what's the weather today" and the silence after it
    assert took < audio.size / RATE / 4  # comfortably faster than real time


def test_other_speech_does_not_wake(wake_models: Path) -> None:
    from jarvis_voice.listen.wakeword import OpenWakeWord

    wake = OpenWakeWord(wake_models)
    audio = np.concatenate([hush(2.0), read_wav(DATA / "hello-there.wav"), hush(1.0)])
    assert max(wake.process(audio)) < 0.2
