from __future__ import annotations

import threading
import time
from collections.abc import Iterator

import numpy as np
import pytest

from jarvis_voice.audio.errors import AudioError
from jarvis_voice.audio.fake import FakePlayback
from jarvis_voice.speech import SpeechPipeline, estimate_spoken_text
from jarvis_voice.tts.base import SynthError

from conftest import RecordingSink, wait_until
from fakes import FakeSynth


@pytest.fixture
def playback() -> Iterator[FakePlayback]:
    pb = FakePlayback(speed=8.0)
    yield pb
    pb.close()


def make(
    sink: RecordingSink, playback: FakePlayback, synth: FakeSynth | None = None, **kw: object
) -> tuple[SpeechPipeline, FakeSynth, list[bool]]:
    synth = synth or FakeSynth()
    speaking: list[bool] = []
    pipe = SpeechPipeline(synth, playback, sink, voice_id=lambda: "voice-1", on_speaking=speaking.append, **kw).start()  # type: ignore[arg-type]
    return pipe, synth, speaking


def test_sentences_are_spoken_in_seq_order(sink: RecordingSink, playback: FakePlayback) -> None:
    pipe, synth, speaking = make(sink, playback)
    try:
        # The mod's POSTs can race: deliver 2, 0, 3(final), 1.
        assert pipe.speak("r1", 2, "Third.", False) == {"ok": True}
        pipe.speak("r1", 0, "First.", False)
        pipe.speak("r1", 3, "", True)
        pipe.speak("r1", 1, "Second.", False)
        done = sink.wait_type("speech_done", replyId="r1")
        assert synth.log == ["First.", "Second.", "Third."]
        assert done == {
            "v": 1,
            "type": "speech_done",
            "replyId": "r1",
            "interrupted": False,
            "spokenText": "First. Second. Third.",
        }
        assert sink.types() == ["speech_started", "speech_done"]
        assert speaking == [True, False]
        assert len(synth.streams) == 1 and synth.streams[0].voice_id == "voice-1"  # one stream per reply
        assert playback.ends == 1  # end of reply flushed once
    finally:
        pipe.close()


def test_audio_plays_through_playback(sink: RecordingSink, playback: FakePlayback) -> None:
    pipe, synth, _ = make(sink, playback)
    try:
        pipe.speak("r1", 0, "Hello there.", True)
        sink.wait_type("speech_done")
        expected = int(len("Hello there.") * synth.ms_per_char / 1000 * 24_000)
        assert playback.speech_frames_played == expected
    finally:
        pipe.close()


def test_duplicates_and_late_sentences_are_dropped(sink: RecordingSink, playback: FakePlayback) -> None:
    pipe, synth, _ = make(sink, playback)
    try:
        pipe.speak("r1", 0, "One.", False)
        assert pipe.speak("r1", 0, "One again.", False)["dropped"] is True
        pipe.speak("r1", 1, "Two.", True)
        assert pipe.speak("r1", 2, "After final.", False)["dropped"] is True
        sink.wait_type("speech_done")
        assert pipe.speak("r1", 3, "Way late.", False) == {"ok": True, "dropped": True}
        assert synth.log == ["One.", "Two."]
    finally:
        pipe.close()


def test_final_only_reply_finishes_without_speaking(sink: RecordingSink, playback: FakePlayback) -> None:
    pipe, synth, speaking = make(sink, playback)
    try:
        pipe.speak("r1", 0, "   ", True)
        done = sink.wait_type("speech_done")
        assert done["interrupted"] is False and done["spokenText"] == ""
        assert sink.types() == ["speech_done"] and speaking == [] and synth.streams == []
    finally:
        pipe.close()


def test_stop_is_instant_and_drops_late_sentences(sink: RecordingSink, playback: FakePlayback) -> None:
    pipe, synth, speaking = make(sink, playback, FakeSynth(ms_per_char=200))  # long audio
    try:
        pipe.speak("r1", 0, "A sentence long enough to still be playing.", False)
        sink.wait_type("speech_started")
        wait_until(lambda: playback.speech_frames_played > 0.3 * 24_000)  # the user heard it begin
        result = pipe.stop("user")
        assert result is not None and result.reply_id == "r1"
        done = sink.wait_type("speech_done")
        assert done["interrupted"] is True
        assert done["spokenText"] == "A sentence long enough to still be playing."
        assert playback.speech_idle() and playback.stops == 1
        assert synth.streams[0].cancelled
        assert pipe.speak("r1", 1, "Too late.", True)["dropped"] is True
        assert pipe.stop() is None  # nothing left to stop
        assert speaking == [True, False]
        assert not pipe.busy()
    finally:
        pipe.close()


def test_stop_drops_queued_replies_and_barge_in_comes_first(sink: RecordingSink, playback: FakePlayback) -> None:
    pipe, synth, _ = make(sink, playback, FakeSynth(ms_per_char=200))
    try:
        pipe.speak("r1", 0, "First reply sentence.", False)
        pipe.speak("r2", 0, "Second reply, queued.", True)
        sink.wait_type("speech_started", replyId="r1")
        wait_until(lambda: playback.speech_frames_played > 0.3 * 24_000)
        pipe.stop("barge", barge_in=True)
        assert sink.types() == ["speech_started", "barge_in", "speech_done", "speech_done"]
        barge = sink.of_type("barge_in")[0]
        assert barge["replyId"] == "r1" and barge["spokenText"] == "First reply sentence."
        dones = sink.of_type("speech_done")
        assert [(d["replyId"], d["interrupted"], d["spokenText"]) for d in dones] == [
            ("r1", True, "First reply sentence."),
            ("r2", True, ""),
        ]
        assert synth.log == ["First reply sentence."]
    finally:
        pipe.close()


def test_replies_play_one_after_another(sink: RecordingSink, playback: FakePlayback) -> None:
    pipe, synth, _ = make(sink, playback)
    try:
        pipe.speak("r1", 0, "Reply one.", True)
        pipe.speak("r2", 0, "Reply two.", True)
        sink.wait_type("speech_done", replyId="r2")
        assert [(e["type"], e.get("replyId")) for e in sink.events] == [
            ("speech_started", "r1"),
            ("speech_done", "r1"),
            ("speech_started", "r2"),
            ("speech_done", "r2"),
        ]
        assert len(synth.streams) == 2
    finally:
        pipe.close()


def test_missing_sentence_is_skipped_after_gap_timeout(sink: RecordingSink, playback: FakePlayback) -> None:
    pipe, synth, _ = make(sink, playback, gap_timeout=0.2)
    try:
        pipe.speak("r1", 0, "Zero.", False)
        pipe.speak("r1", 2, "Two.", True)  # seq 1 is lost
        done = sink.wait_type("speech_done", timeout=3)
        assert synth.log == ["Zero.", "Two."] and done["interrupted"] is False
    finally:
        pipe.close()


def test_key_missing_reports_error_and_finishes_reply(sink: RecordingSink, playback: FakePlayback) -> None:
    synth = FakeSynth(fail_open=SynthError("fish_key_missing", "no key", "set it"))
    pipe, _, speaking = make(sink, playback, synth)
    try:
        pipe.speak("r1", 0, "Hello.", False)
        err = sink.wait_type("error")
        assert err["code"] == "fish_key_missing" and err["fatal"] is False and err["hint"] == "set it"
        done = sink.wait_type("speech_done")
        assert done["interrupted"] is True and done["spokenText"] == ""
        assert pipe.speak("r1", 1, "More.", True)["dropped"] is True  # the reply is closed
        assert speaking == []
    finally:
        pipe.close()


def test_stream_failure_mid_reply(sink: RecordingSink, playback: FakePlayback) -> None:
    pipe, _, _ = make(sink, playback, FakeSynth(fail_after=1))
    try:
        pipe.speak("r1", 0, "One.", False)
        pipe.speak("r1", 1, "Two.", False)
        assert sink.wait_type("error")["code"] == "fish_unreachable"
        assert sink.wait_type("speech_done")["interrupted"] is True
        assert playback.speech_idle()
    finally:
        pipe.close()


def test_audio_in_flight_after_stop_is_not_played(sink: RecordingSink, playback: FakePlayback) -> None:
    pipe, synth, _ = make(sink, playback, FakeSynth(ms_per_char=200))
    try:
        pipe.speak("r1", 0, "Long enough sentence.", False)
        sink.wait_type("speech_started")
        stream = synth.streams[0]
        pipe.stop()
        stream.cancelled = False  # simulate a chunk already in the reader when stop hit
        stream._on_audio(np.ones(4800, "<i2").tobytes())
        assert playback.speech_idle()
    finally:
        pipe.close()


def test_playback_gating_holds_speech(sink: RecordingSink, playback: FakePlayback) -> None:
    pipe, _, _ = make(sink, playback)
    try:
        playback.set_paused(True)  # e.g. the user is holding push-to-talk
        pipe.speak("r1", 0, "Wait for it.", True)
        wait_until(lambda: not playback.speech_idle())
        assert sink.types() == []  # queued, not started
        playback.set_paused(False)
        sink.wait_type("speech_done")
        assert sink.types() == ["speech_started", "speech_done"]
    finally:
        pipe.close()


def test_estimate_spoken_text() -> None:
    sentences = ["Hello there.", "This is the second sentence.", "Third."]  # 12, 28, 6 chars
    assert estimate_spoken_text(sentences, 0.0, 10.0) == ""
    assert estimate_spoken_text(sentences, 0.1, 10.0) == ""
    assert estimate_spoken_text(sentences, 0.5, 10.0) == "Hello there."
    assert estimate_spoken_text(sentences, 1.3, 10.0) == "Hello there."  # second starts at 1.2 s
    assert estimate_spoken_text(sentences, 1.4, 10.0) == "Hello there. This is the second sentence."
    assert estimate_spoken_text(sentences, 99.0, 10.0) == " ".join(sentences)


def test_reply_without_final_is_closed_once_a_newer_reply_waits(sink: RecordingSink, playback: FakePlayback) -> None:
    """A lost final (or a sentence that landed after a stop) must not block every later reply."""
    pipe, synth, speaking = make(sink, playback, stale_after=0.3)
    try:
        pipe.speak("turn-A", 0, "First reply sentence.", False)  # its final never comes
        sink.wait_type("speech_started", replyId="turn-A")
        pipe.speak("turn-B", 0, "Second reply.", True)
        done_b = sink.wait_type("speech_done", replyId="turn-B", timeout=5)
        done_a = sink.of_type("speech_done")[0]
        assert done_a["replyId"] == "turn-A" and done_a["interrupted"] is True
        assert done_a["spokenText"] == "First reply sentence."  # it had played out in full
        assert done_b["interrupted"] is False and synth.log == ["First reply sentence.", "Second reply."]
        wait_until(lambda: speaking == [True, False, True, False])
        assert not pipe.busy()
        assert pipe.speak("turn-A", 1, "Too late.", True)["dropped"] is True
    finally:
        pipe.close()


def test_lone_open_reply_waits_for_its_final(sink: RecordingSink, playback: FakePlayback) -> None:
    """Tools may run for minutes between sentences of one voice turn: no timeout without a newer reply."""
    pipe, _, _ = make(sink, playback, stale_after=0.1)
    try:
        pipe.speak("r1", 0, "Let me check.", False)
        sink.wait_type("speech_started")
        wait_until(lambda: playback.speech_idle())
        time.sleep(0.4)
        assert sink.of_type("speech_done") == [] and pipe.active_reply_id() == "r1"
        pipe.speak("r1", 1, "All tests pass.", False)
        pipe.speak("r1", 2, "", True)
        done = sink.wait_type("speech_done")
        assert done["interrupted"] is False and done["spokenText"] == "Let me check. All tests pass."
    finally:
        pipe.close()


class DeadOutput(FakePlayback):
    """An output that accepts audio but stops rendering (headset switched off mid-reply)."""

    def __init__(self) -> None:
        super().__init__(speed=8.0)
        self.dead = False
        self.closes = 0

    def _render_block(self) -> None:
        if not self.dead:
            self.mixer.render(self.block)

    def close(self) -> None:
        self.closes += 1
        self.dead = False  # reopening picks a working device
        super().close()


def test_output_that_stops_playing_ends_the_reply_with_no_output_device(sink: RecordingSink) -> None:
    playback = DeadOutput()
    pipe, _, speaking = make(sink, playback, FakeSynth(ms_per_char=400), stall_timeout=0.3)  # ~2 s at 8x
    try:
        pipe.speak("r1", 0, "A sentence that will not finish playing.", True)
        sink.wait_type("speech_started")
        playback.dead = True
        err = sink.wait_type("error", timeout=5)
        assert err["code"] == "no_output_device" and err["fatal"] is False and err["hint"]
        done = sink.wait_type("speech_done")
        assert done["interrupted"] is True
        wait_until(lambda: speaking == [True, False])
        assert playback.closes == 1 and playback.speech_idle()
        # The next reply reopens the output and plays normally.
        pipe.speak("r2", 0, "Back again.", True)
        assert sink.wait_type("speech_done", replyId="r2")["interrupted"] is False
    finally:
        pipe.close()


REOPEN_FAILED = "The audio output could not be reopened (headset disconnected or asleep?)."


def test_output_that_cannot_reopen_ends_the_reply_at_once(sink: RecordingSink, playback: FakePlayback) -> None:
    pipe, _, speaking = make(sink, playback, FakeSynth(ms_per_char=400))  # long audio, default stall timeout
    try:
        pipe.speak("r1", 0, "A sentence that will not finish playing.", True)
        sink.wait_type("speech_started")
        playback.open_error = AudioError("no_output_device", REOPEN_FAILED, "check it")
        err = sink.wait_type("error", timeout=1.0)  # at once, not after the stall timeout
        assert err["code"] == "no_output_device" and err["message"] == REOPEN_FAILED
        assert err["hint"] == "check it" and err["fatal"] is False
        assert sink.wait_type("speech_done")["interrupted"] is True
        assert len(sink.of_type("error")) == 1 and playback.stops == 1 and playback.speech_idle()
        wait_until(lambda: speaking == [True, False])
    finally:
        pipe.close()


@pytest.mark.parametrize("output_back", [True, False])
def test_output_that_cannot_reopen_waits_while_the_user_talks(
    sink: RecordingSink, playback: FakePlayback, output_back: bool
) -> None:
    """Hands-free: the user talks over a reply (speech paused) while its output is down."""
    pipe, _, _ = make(sink, playback, FakeSynth(ms_per_char=50))
    try:
        playback.set_paused(True)
        pipe.speak("r1", 0, "Held while you talk.", True)
        wait_until(lambda: not playback.speech_idle())
        playback.open_error = AudioError("no_output_device", REOPEN_FAILED, "check it")
        time.sleep(0.3)
        assert sink.of_type("error") == []  # not while the user is talking
        if not output_back:
            playback.fail_with = AudioError("no_output_device", REOPEN_FAILED, "check it")
        playback.set_paused(False)  # the user stopped: one more try at the output
        done = sink.wait_type("speech_done")
        if output_back:
            assert done["interrupted"] is False and sink.of_type("error") == []
        else:
            assert done["interrupted"] is True
            assert [e["message"] for e in sink.of_type("error")] == [REOPEN_FAILED]
    finally:
        pipe.close()


def test_replies_hold_the_output_open_until_the_last_speech_done(sink: RecordingSink, playback: FakePlayback) -> None:
    pipe, _, _ = make(sink, playback, FakeSynth(ms_per_char=50))
    try:
        assert playback.held is False
        pipe.speak("r1", 0, "Let me check.", False)
        assert playback.held is True  # from the first sentence on
        sink.wait_type("speech_started")
        pipe.speak("r2", 0, "Queued reply.", True)
        pipe.speak("r1", 1, "", True)
        sink.wait_type("speech_done", replyId="r1")
        assert playback.held is True  # r2 is still to come
        sink.wait_type("speech_done", replyId="r2")
        assert playback.held is False
        pipe.speak("r3", 0, "Interrupted.", False)
        assert playback.held is True
        pipe.stop("user")
        assert playback.held is False
    finally:
        pipe.close()


def test_a_queued_id_without_its_reply_still_releases_the_output(sink: RecordingSink, playback: FakePlayback) -> None:
    pipe = SpeechPipeline(FakeSynth(), playback, sink)
    playback.hold_open(True)
    pipe._order.append("ghost")  # the worker's defensive case: queue and replies out of sync
    pipe.start()
    try:
        wait_until(lambda: not playback.held)
        assert not pipe.busy()
    finally:
        pipe.close()


def test_paused_output_is_not_a_stall(sink: RecordingSink, playback: FakePlayback) -> None:
    pipe, _, _ = make(sink, playback, stall_timeout=0.1)
    try:
        playback.set_paused(True)  # push-to-talk held for longer than the stall timeout
        pipe.speak("r1", 0, "Wait for it.", True)
        time.sleep(0.4)
        assert sink.of_type("error") == []
        playback.set_paused(False)
        assert sink.wait_type("speech_done")["interrupted"] is False
    finally:
        pipe.close()


def test_prewarm_opens_the_stream_the_first_sentence_then_uses(sink: RecordingSink, playback: FakePlayback) -> None:
    pipe, synth, _ = make(sink, playback)
    try:
        pipe.prewarm()
        wait_until(lambda: len(synth.streams) == 1)
        assert synth.streams[0].sentences == []  # opened, nothing sent yet
        pipe.speak("r1", 0, "Hello there.", True)
        done = sink.wait_type("speech_done", replyId="r1")
        assert done["interrupted"] is False and done["spokenText"] == "Hello there."
        assert len(synth.streams) == 1 and synth.streams[0].sentences == ["Hello there."]
        pipe.prewarm()  # the next utterance warms a new one
        wait_until(lambda: len(synth.streams) == 2)
    finally:
        pipe.close()
    assert synth.streams[1].cancelled  # closing drops an unused pre-opened stream


def test_unused_prewarm_expires_and_the_reply_opens_its_own(sink: RecordingSink, playback: FakePlayback) -> None:
    pipe, synth, _ = make(sink, playback, warm_ttl=0.2)
    try:
        pipe.prewarm()
        wait_until(lambda: len(synth.streams) == 1)
        wait_until(lambda: synth.streams[0].cancelled, timeout=3.0)
        pipe.speak("r1", 0, "Fresh stream.", True)
        assert sink.wait_type("speech_done", replyId="r1")["interrupted"] is False
        assert len(synth.streams) == 2 and synth.streams[1].sentences == ["Fresh stream."]
    finally:
        pipe.close()


def test_prewarm_for_another_voice_is_not_used(sink: RecordingSink, playback: FakePlayback) -> None:
    voice = ["voice-1"]
    synth = FakeSynth()
    pipe = SpeechPipeline(synth, playback, sink, voice_id=lambda: voice[0]).start()  # type: ignore[arg-type]
    try:
        pipe.prewarm()
        wait_until(lambda: len(synth.streams) == 1)
        voice[0] = "voice-2"
        pipe.speak("r1", 0, "New voice.", True)
        sink.wait_type("speech_done", replyId="r1")
        assert synth.streams[0].cancelled and synth.streams[1].voice_id == "voice-2"
    finally:
        pipe.close()


def test_prewarm_without_a_key_does_nothing(sink: RecordingSink, playback: FakePlayback) -> None:
    missing = SynthError("fish_key_missing", "No Fish Audio API key is configured.")
    pipe, synth, _ = make(sink, playback, FakeSynth(fail_open=missing))
    try:
        pipe.prewarm()
        time.sleep(0.1)
        assert synth.streams == [] and sink.types() == []
    finally:
        pipe.close()


def test_a_reply_waits_for_a_stream_still_connecting(sink: RecordingSink, playback: FakePlayback) -> None:
    synth = FakeSynth()
    gate = threading.Event()
    real_open = synth.open_stream

    def slow_open(on_audio, *, voice_id):  # type: ignore[no-untyped-def]
        gate.wait(2.0)
        return real_open(on_audio, voice_id=voice_id)

    synth.open_stream = slow_open  # type: ignore[method-assign]
    pipe = SpeechPipeline(synth, playback, sink, voice_id=lambda: None).start()  # type: ignore[arg-type]
    try:
        pipe.prewarm()
        time.sleep(0.05)
        pipe.speak("r1", 0, "Only one connection.", True)
        time.sleep(0.1)
        gate.set()
        assert sink.wait_type("speech_done", replyId="r1")["interrupted"] is False
        assert len(synth.streams) == 1
    finally:
        pipe.close()
