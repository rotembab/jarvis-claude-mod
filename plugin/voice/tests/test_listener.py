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
    def __init__(self, *, plain: bool = False, **kwargs: Any) -> None:
        self.wake = ScriptedWake(plain=plain)
        self.playing = True
        self.events: list[tuple[str, Any]] = []
        self.clips: list[np.ndarray] = []
        kwargs.setdefault("plain_wake", plain)
        self.listener = Listener(
            ListenerConfig(),
            vad=LoudnessVad(),
            wake=self.wake,
            on_start=lambda kind: self.events.append(("start", kind)),
            on_end=self._on_end,
            on_follow_up=lambda opened: self.events.append(("follow", opened)),
            on_digital_silence=lambda silent: self.events.append(("silence", silent)),
            audio_playing=lambda: self.playing,
            **kwargs,
        )

    def _on_end(self, pcm: np.ndarray | None, kind: str) -> None:
        self.events.append(("end", (None if pcm is None else pcm.size / RATE, kind)))
        if pcm is not None:
            self.clips.append(pcm)

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


# -- plain "Jarvis" ---------------------------------------------------------------------------------


def speech_starts_at(clip: np.ndarray) -> float:
    """Seconds into a clip where the (tone) speech begins."""
    return float(np.flatnonzero(np.abs(clip) > 0.1)[0]) / RATE


def say_plain_jarvis(rig: Rig, *, before: float = 1.0) -> None:
    """Silence, "Jarvis" (half a second), then the plain model fires as the word ends."""
    rig.play(hush(before))
    rig.play(tone(0.5))
    rig.wake.say_plain()


def test_plain_jarvis_then_a_short_pause_starts_an_utterance_with_the_whole_word() -> None:
    rig = Rig(plain=True)
    assert rig.listener.plain_ready
    say_plain_jarvis(rig)  # "Jarvis,"
    rig.play(hush(0.2))  # <0.2 s pause>
    assert rig.kinds("start") == ["jarvis"]
    rig.play(tone(1.0))  # "open the browser"
    rig.play(hush(0.8))
    [(seconds, kind)] = rig.kinds("end")
    assert kind == "jarvis" and seconds >= 0.5 + 0.2 + 1.0 + 0.7
    # The pre-roll reaches back to where the speech began, and 0.2 s before it.
    assert 0.15 <= speech_starts_at(rig.clips[0]) <= 0.25


def test_plain_jarvis_after_okay_and_a_short_pause_wakes() -> None:
    rig = Rig(plain=True)
    rig.play(hush(1.0))
    rig.play(tone(0.4))  # "Okay."
    rig.play(hush(0.3))
    rig.play(tone(0.5))  # "Jarvis"
    rig.wake.say_plain()
    rig.play(hush(0.2))
    assert rig.kinds("start") == ["jarvis"]


def test_a_pause_starts_a_new_utterance_for_plain_jarvis() -> None:
    rig = Rig(plain=True)
    rig.play(hush(1.0))
    rig.play(tone(1.4))  # "Right, let me think."
    say_plain_jarvis(rig, before=0.5)  # ... "Jarvis,"
    rig.play(hush(0.2))
    assert rig.kinds("start") == ["jarvis"]
    rig.play(tone(0.6))
    rig.play(hush(0.8))
    # The clip starts at "Jarvis", not at the sentence before the pause.
    [(seconds, _kind)] = rig.kinds("end")
    assert seconds < 0.2 + 0.5 + 0.2 + 0.6 + 0.9 and 0.15 <= speech_starts_at(rig.clips[0]) <= 0.25


def test_plain_jarvis_inside_a_sentence_is_ignored() -> None:
    rig = Rig(plain=True)
    rig.play(hush(1.0))
    rig.play(tone(2.0))  # "... and then I asked Jarvis" two seconds into speech
    rig.wake.say_plain()
    rig.play(tone(0.3))
    rig.play(hush(1.0))
    assert rig.kinds("start") == []
    say_plain_jarvis(rig)  # the next utterance starts with it
    rig.play(hush(0.2))
    assert rig.kinds("start") == ["jarvis"]


def test_a_short_dip_between_words_does_not_start_a_new_utterance() -> None:
    rig = Rig(plain=True)
    rig.play(hush(1.0))
    rig.play(tone(1.0))  # "I was talking to"
    rig.play(hush(0.15))  # a breath, not a pause
    rig.play(tone(0.5))  # "Jarvis"
    rig.wake.say_plain()
    rig.play(hush(0.2))
    assert rig.kinds("start") == []


FRAME = 512  # one speech-detection frame


@pytest.mark.parametrize(("pause", "starts"), [(0.25, []), (0.3, ["jarvis"])])
def test_plain_jarvis_needs_a_pause_of_three_tenths_of_a_second_before_it(pause: float, starts: list[str]) -> None:
    # Whole frames, so the pause shows as whole silent frames: 0.25 s as 7 of
    # them, 0.3 s as 9 (0.288 s), which still counts as 0.3 s.
    rig = Rig(plain=True)
    rig.play(hush(1.1)[: 32 * FRAME])
    rig.play(tone(1.6)[: 47 * FRAME])  # a sentence long enough that its own start is past the window
    rig.play(hush(pause))
    rig.play(tone(0.5))  # "Jarvis"
    rig.wake.say_plain()
    rig.play(hush(0.2))
    assert rig.kinds("start") == starts


def test_plain_jarvis_never_interrupts_jarvis_but_hey_jarvis_still_does() -> None:
    rig = Rig(plain=True, barge_mode="wake")
    rig.listener.set_speaking(True)
    say_plain_jarvis(rig)
    rig.play(hush(0.3))
    assert rig.kinds("start") == []
    rig.wake.say()
    rig.play(hush(0.1))
    assert rig.kinds("start") == ["wake"]


def test_plain_jarvis_waits_out_the_refractory_after_a_cancelled_start() -> None:
    rig = Rig(plain=True)
    say_plain_jarvis(rig)
    rig.play(hush(0.1))
    assert rig.kinds("start") == ["jarvis"]
    rig.listener.cancel()  # the daemon could not take it
    rig.wake.say_plain()  # and the score is still high
    rig.play(hush(0.2))
    assert rig.kinds("start") == ["jarvis"] and rig.listener.mode == "idle"


def test_a_cancelled_plain_jarvis_does_not_hold_off_hey_jarvis() -> None:
    # Plain "Jarvis" fired on a sound-alike just as a reply became audible, so the
    # daemon cancelled it. "Hey Jarvis, stop" right after must still start.
    rig = Rig(plain=True)
    say_plain_jarvis(rig)
    rig.play(hush(0.1))
    assert rig.kinds("start") == ["jarvis"]
    rig.listener.cancel()
    rig.listener.set_speaking(True)
    rig.wake.say()  # well inside the 1.5 s that holds off plain "Jarvis"
    rig.play(hush(0.1))
    assert rig.kinds("start") == ["jarvis", "wake"] and rig.listener.mode == "record"


def test_a_cancelled_hey_jarvis_holds_off_plain_jarvis() -> None:
    # The daemon cancelled a "Hey Jarvis" start (push-to-talk was busy, or the
    # speech model is not loaded). The plain model firing late on the same
    # "Jarvis" must not start a second utterance (and a second error chime).
    rig = Rig(plain=True)
    rig.play(hush(1.0))
    rig.play(tone(0.5))  # "Hey Jarvis", at the start of an utterance
    rig.wake.say()
    rig.play(hush(0.1))
    assert rig.kinds("start") == ["wake"]
    rig.listener.cancel()
    rig.wake.say_plain()  # well inside the 1.5 s that holds off both phrases
    rig.play(hush(0.2))
    assert rig.kinds("start") == ["wake"] and rig.listener.mode == "idle"


def test_hey_jarvis_wins_when_both_phrases_fire_on_the_same_chunk() -> None:
    rig = Rig(plain=True)
    rig.play(hush(1.0))
    rig.play(tone(0.5))
    rig.wake.say()
    rig.wake.say_plain()
    rig.play(hush(0.1))
    assert rig.kinds("start") == ["wake"]


def test_plain_jarvis_is_off_unless_switched_on() -> None:
    rig = Rig(plain=True, plain_wake=False)
    assert not rig.listener.plain_ready
    say_plain_jarvis(rig)
    rig.play(hush(0.3))
    assert rig.kinds("start") == []
    rig.listener.set_plain_wake(True)
    say_plain_jarvis(rig)
    rig.play(hush(0.3))
    assert rig.kinds("start") == ["jarvis"]
    rig.listener.set_wake_enabled(False)  # the wake word off silences both
    assert not rig.listener.plain_ready


def test_without_its_model_plain_jarvis_cannot_be_ready() -> None:
    rig = Rig(plain_wake=True)  # the scorer has no plain model
    assert rig.listener.plain_enabled and not rig.listener.plain_ready
    rig.play(hush(1.0))
    rig.play(tone(0.5))
    rig.play(hush(0.3))
    assert rig.kinds("start") == []


def test_hey_jarvis_keeps_its_short_pre_roll_unless_plain_jarvis_is_on() -> None:
    starts = []
    flags = []
    # Off with no plain model; off with the plain model loaded (what /jarvis setup gives everyone); on.
    for kwargs in ({"plain": False}, {"plain": True, "plain_wake": False}, {"plain": True}):
        rig = Rig(**kwargs)
        rig.play(hush(1.0))
        rig.play(tone(0.5))  # "Jarvis": the "Hey Jarvis" model fires on it often enough
        rig.wake.say()
        rig.play(hush(0.2))
        rig.play(tone(1.0))
        rig.play(hush(0.8))
        assert rig.kinds("start") == ["wake"]
        starts.append(speech_starts_at(rig.clips[0]))
        flags.append(rig.listener.long_preroll)  # what the daemon reads as the clip is handed over
    assert flags == [False, False, True]
    assert starts[0] < 0.1  # 0.3 s of pre-roll: the word is cut
    assert starts[1] < 0.1  # a loaded plain model alone changes nothing
    assert 0.15 <= starts[2] <= 0.25  # plain "Jarvis" on: the whole word


def test_hey_jarvis_well_into_speech_keeps_its_short_pre_roll_with_plain_jarvis_on() -> None:
    # Said four seconds into talking, "Hey Jarvis" is not at the start of an
    # utterance: the earlier, unrelated speech must not be sent with the request.
    lengths = []
    for plain in (False, True):
        rig = Rig(plain=plain)
        rig.play(hush(1.0))
        rig.play(tone(4.0))  # talking, then "... hey Jarvis"
        rig.wake.say()
        rig.play(hush(0.2))
        rig.play(tone(1.0))
        rig.play(hush(0.8))
        assert rig.kinds("start") == ["wake"]
        [(seconds, _kind)] = rig.kinds("end")
        lengths.append(seconds)
    assert lengths[0] == lengths[1]


def test_hey_jarvis_keeps_its_short_pre_roll_while_the_plain_model_is_missing() -> None:
    rig = Rig(plain_wake=True)  # switched on, but the scorer has no plain model
    rig.play(hush(1.0))
    rig.play(tone(0.5))
    rig.wake.say()
    rig.play(hush(0.2))
    rig.play(tone(1.0))
    rig.play(hush(0.8))
    assert rig.kinds("start") == ["wake"]
    assert speech_starts_at(rig.clips[0]) < 0.1


def test_hey_jarvis_over_jarvis_keeps_its_short_pre_roll_with_plain_jarvis_on() -> None:
    # Through speakers the listener hears Jarvis himself. A sentence of his that
    # began after a pause must not be put in front of the user's words.
    lengths = []
    for plain in (False, True):
        rig = Rig(plain=plain, barge_mode="wake")
        rig.listener.set_speaking(True)
        rig.play(hush(1.0))
        rig.play(tone(1.0))  # his voice
        rig.play(hush(0.4))  # between two of his sentences
        rig.play(tone(0.5))  # his next sentence, and the user says "Hey Jarvis" over it
        rig.wake.say()
        rig.play(hush(0.1))
        assert rig.kinds("start") == ["wake"]
        rig.play(tone(1.0))
        rig.play(hush(0.8))
        [(seconds, _kind)] = rig.kinds("end")
        lengths.append(seconds)
    assert lengths[0] == lengths[1]


def test_the_sensitivity_applies_to_both_phrases() -> None:
    rig = Rig(plain=True)
    rig.listener.set_wake_threshold(0.95)
    say_plain_jarvis(rig)  # scores 0.9
    rig.play(hush(0.3))
    rig.wake.say()
    rig.play(hush(0.1))
    assert rig.kinds("start") == []


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


@pytest.fixture(scope="module")
def plain_wake_models(wake_models: Path) -> Path:
    from jarvis_voice.listen.models import JARVIS_V2, PLAIN_JARVIS_FILES, is_downloaded

    if not is_downloaded(wake_models, PLAIN_JARVIS_FILES):
        pytest.skip(f"put {JARVIS_V2.name} into JARVIS_WAKE_MODELS_DIR to test plain Jarvis")
    return wake_models


def test_with_plain_jarvis_on_the_wake_phrase_is_kept_from_its_first_syllable(plain_wake_models: Path) -> None:
    from jarvis_voice.listen.models import JARVIS_V2
    from jarvis_voice.listen.vad import SileroVad
    from jarvis_voice.listen.wakeword import OpenWakeWord

    speech = read_wav(DATA / "hey-jarvis-weather.wav")
    starts = []
    for plain in (False, True):
        clips: list[np.ndarray] = []
        listener = Listener(
            ListenerConfig(),
            vad=SileroVad(),
            wake=OpenWakeWord(plain_wake_models, plain=JARVIS_V2),
            on_start=lambda kind: None,
            on_end=lambda pcm, kind: clips.append(pcm),
            plain_wake=plain,
        )
        audio = np.concatenate([hush(1.5), speech, hush(1.0)])
        for i in range(0, audio.size, 160):
            listener.process(audio[i : i + 160])
        [clip] = clips
        starts.append(float(np.flatnonzero(np.abs(clip) > 0.02)[0]) / RATE)
    assert starts[0] == 0.0  # 0.3 s of pre-roll starts inside "Jarvis"
    assert 0.05 <= starts[1] <= 0.25  # from Silero's onset, less 0.2 s


def test_plain_jarvis_runs_on_the_same_features_without_changing_hey_jarvis(plain_wake_models: Path) -> None:
    # No test clip says plain "Jarvis": the model misses some synthetic voices,
    # espeak-ng's among them. This checks the pairing and the false alarms.
    from jarvis_voice.listen.models import JARVIS_V2
    from jarvis_voice.listen.wakeword import OpenWakeWord

    paired = OpenWakeWord(plain_wake_models, plain=JARVIS_V2)
    alone = OpenWakeWord(plain_wake_models)
    assert (paired.name, paired.plain_name, alone.plain_name) == ("hey_jarvis_v0.1", "jarvis_v2", None)
    for name in ("hey-jarvis-weather.wav", "hello-there.wav"):
        paired.reset()
        alone.reset()
        audio = np.concatenate([hush(2.0), read_wav(DATA / name), hush(1.0)])
        pairs = paired.process_pair(audio)
        assert np.allclose([hey for hey, _ in pairs], alone.process(audio))
        assert all(plain is not None and 0.0 <= plain <= 1.0 for _, plain in pairs)
        if name == "hello-there.wav":
            assert max(plain for _, plain in pairs if plain is not None) < 0.2


def test_plain_jarvis_attached_to_a_running_wake_word_scores_like_one_loaded_with_it(plain_wake_models: Path) -> None:
    from jarvis_voice.listen.models import JARVIS_V2
    from jarvis_voice.listen.wakeword import OpenWakeWord

    paired = OpenWakeWord(plain_wake_models, plain=JARVIS_V2)
    later = OpenWakeWord(plain_wake_models)
    audio = np.concatenate([hush(2.0), read_wav(DATA / "hey-jarvis-weather.wav"), hush(1.0)])
    half = audio.size // 2
    before = later.process_pair(audio[:half])
    assert all(plain is None for _, plain in before)
    later.attach_plain(plain_wake_models, JARVIS_V2)
    assert later.plain_name == "jarvis_v2"
    after = later.process_pair(audio[half:])
    expected = paired.process_pair(audio[:half]) + paired.process_pair(audio[half:])
    assert np.allclose([hey for hey, _ in before + after], [hey for hey, _ in expected])
    tail = expected[len(before) :]
    assert np.allclose([plain for _, plain in after], [plain for _, plain in tail])
