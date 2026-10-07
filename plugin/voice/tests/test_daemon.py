"""State machine end to end with fake audio, fake STT, fake PTT and fake/real TTS."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pytest

from jarvis_voice import protocol
from jarvis_voice.audio.errors import AudioError
from jarvis_voice.audio.fake import FakeCapture, FakePlayback
from jarvis_voice.daemon import Daemon, DaemonConfig
from jarvis_voice.ptt.fake import FakePushToTalk
from jarvis_voice.ptt.keys import Hotkey
from jarvis_voice.stt.base import SttError
from jarvis_voice.stt.fake import FakeTranscriber
from jarvis_voice.tts.base import SynthError
from jarvis_voice.tts.fish import FishLiveSynth, FishSettings

from conftest import RecordingSink, wait_until
from fakes import FakeSynth
from fish_fake import FakeFishServer


@dataclass
class Rig:
    daemon: Daemon
    sink: RecordingSink
    capture: FakeCapture
    playback: FakePlayback
    ptt: FakePushToTalk
    synth: Any
    transcribers: list[FakeTranscriber] = field(default_factory=list)
    thread: threading.Thread | None = None

    def command(self, name: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        body = body or {}
        protocol.validate_command(name, body)
        return self.daemon.handle_command(name, body)


def build(
    sink: RecordingSink,
    *,
    synth: Any = None,
    capture: FakeCapture | None = None,
    playback: FakePlayback | None = None,
    ptt: FakePushToTalk | None = None,
    stt_fail: SttError | None = None,
    stt_delay: float = 0.0,
    config: DaemonConfig | None = None,
    stt_text: str = "turn on the lights",
    vad: Any = None,
    wake_loader: Callable[[], Any] | None = None,
    plain_loader: Callable[[Any], None] | None = None,
) -> Rig:
    rig = Rig(
        daemon=None,  # type: ignore[arg-type]
        sink=sink,
        capture=capture or FakeCapture(),
        playback=playback or FakePlayback(speed=8.0),
        ptt=ptt or FakePushToTalk(),
        synth=synth or FakeSynth(),
    )

    def factory(requested: str) -> FakeTranscriber:
        t = FakeTranscriber(stt_text, name=f"fake-{requested}", load_delay=stt_delay, fail=stt_fail)
        rig.transcribers.append(t)
        return t

    rig.daemon = Daemon(
        config or DaemonConfig(),
        events=sink,
        capture=rig.capture,
        playback=rig.playback,
        synth=rig.synth,
        ptt=rig.ptt,
        transcriber_factory=factory,
        platform_name="linux",
        vad=vad,
        wake_loader=wake_loader,
        plain_loader=plain_loader,
    )
    return rig


def start(rig: Rig) -> Rig:
    rig.daemon.start()
    rig.thread = threading.Thread(target=rig.daemon.run, daemon=True)
    rig.thread.start()
    return rig


@pytest.fixture
def rig(sink: RecordingSink) -> Iterator[Rig]:
    r = start(build(sink))
    yield r
    r.daemon.request_exit(0)
    assert r.thread is not None
    r.thread.join(5)


def wait_ready(rig: Rig) -> dict[str, Any]:
    return rig.sink.wait_type("ready")


def test_startup_reaches_sleeping_and_ready(rig: Rig) -> None:
    ready = wait_ready(rig)
    assert ready["sttModel"] == "fake-auto" and ready["sttDevice"] == "cpu" and ready["pttKey"] == "right ctrl"
    assert "voiceId" not in ready
    assert rig.sink.types()[:3] == ["state", "state", "ready"]
    assert rig.sink.states() == ["starting", "sleeping"]
    assert rig.capture.is_open and rig.ptt.started


def test_ptt_press_release_produces_utterance(rig: Rig) -> None:
    wait_ready(rig)
    mark = rig.sink.mark()
    rig.ptt.press()
    rig.sink.wait_type("state", after=mark, state="listening")
    assert rig.playback.effects_played == 1  # start chime
    time.sleep(0.4)
    rig.ptt.release()
    utt = rig.sink.wait_type("utterance", after=mark)
    assert utt["text"] == "turn on the lights" and utt["source"] == "ptt" and utt["language"] == "en"
    assert utt["durationMs"] >= 600  # 300 ms pre-roll + ~400 ms held
    rig.sink.wait_type("state", after=mark, state="sleeping")
    assert [e["state"] for e in rig.sink.of_type("state")][-3:] == ["listening", "transcribing", "sleeping"]
    assert rig.playback.effects_played == 2  # stop chime
    assert rig.transcribers[0].calls[0][1] == "en"


def test_ptt_hook_callbacks_only_enqueue(sink: RecordingSink) -> None:
    rig = build(sink)  # not started: no loop is serving actions
    rig.daemon.start()
    try:
        wait_ready(rig)
        t0 = time.monotonic()
        rig.ptt.press()
        assert time.monotonic() - t0 < 0.2  # the keyboard hook returns at once
        assert rig.daemon.state == "sleeping"
    finally:
        rig.daemon._shutdown_components()


def test_short_and_silent_clips_are_ignored(rig: Rig) -> None:
    wait_ready(rig)
    mark = rig.sink.mark()
    rig.capture.next_clip = np.full(1600, 0.3, np.float32)  # 100 ms
    rig.ptt.press()
    rig.ptt.release()
    wait_until(lambda: rig.capture.next_clip is None)  # taken by the first release, not replaced before it
    rig.capture.next_clip = (np.random.default_rng(1).standard_normal(16_000) * 1e-4).astype(np.float32)
    rig.ptt.press()
    rig.ptt.release()
    rig.sink.wait_type("state", after=mark, state="sleeping")
    time.sleep(0.2)
    assert rig.sink.of_type("utterance") == [] and rig.sink.of_type("error") == []
    assert "transcribing" not in rig.sink.states()


def test_digital_silence_reports_mic_blocked(rig: Rig) -> None:
    wait_ready(rig)
    rig.capture.next_clip = np.zeros(16_000, np.float32)
    rig.ptt.press()
    rig.ptt.release()
    err = rig.sink.wait_type("error")
    assert err["code"] == "mic_blocked" and err["fatal"] is False and err["hint"]


def test_speak_emits_started_and_done_with_spoken_text(rig: Rig) -> None:
    wait_ready(rig)
    mark = rig.sink.mark()
    assert rig.command("speak", {"replyId": "r1", "seq": 0, "text": "Certainly, sir.", "final": False})["ok"]
    rig.command("speak", {"replyId": "r1", "seq": 1, "text": "Lights are on.", "final": True})
    done = rig.sink.wait_type("speech_done", after=mark)
    assert done["spokenText"] == "Certainly, sir. Lights are on." and done["interrupted"] is False
    assert rig.sink.types()[-4:] == ["speech_started", "state", "speech_done", "state"]
    assert rig.sink.states()[-2:] == ["speaking", "sleeping"]


def test_ptt_during_speech_barges_in_and_listens(sink: RecordingSink) -> None:
    rig = start(build(sink, synth=FakeSynth(ms_per_char=150)))
    try:
        wait_ready(rig)
        rig.command("speak", {"replyId": "r9", "seq": 0, "text": "This is a rather long answer, sir.", "final": False})
        sink.wait_type("speech_started")
        wait_until(lambda: rig.playback.speech_frames_played > 0.3 * 24_000)
        mark = sink.mark()
        rig.ptt.press()
        sink.wait_type("state", after=mark, state="listening")
        types = [e["type"] for e in sink.events[mark:] if e["type"] != "level"]
        assert types[:3] == ["barge_in", "speech_done", "state"]
        barge = sink.of_type("barge_in")[0]
        assert barge["replyId"] == "r9" and barge["spokenText"] == "This is a rather long answer, sir."
        done = sink.of_type("speech_done")[0]
        assert done["interrupted"] is True
        assert rig.playback.speech_idle()
        # Late sentences of the interrupted reply are dropped.
        assert rig.command("speak", {"replyId": "r9", "seq": 1, "text": "More.", "final": True})["dropped"] is True
        rig.ptt.release()
        sink.wait_type("utterance", after=mark)
    finally:
        rig.daemon.request_exit(0)
        assert rig.thread is not None
        rig.thread.join(5)


def test_listen_command_start_stop(rig: Rig) -> None:
    wait_ready(rig)
    assert rig.command("listen", {"action": "start"}) == {"ok": True}
    assert rig.command("listen", {"action": "start"})["already"] is True
    assert rig.daemon.state == "listening"
    rig.ptt.press()  # PTT release must not end a command-driven recording
    rig.ptt.release()
    time.sleep(0.1)
    assert rig.daemon.state == "listening"
    time.sleep(0.3)
    assert rig.command("listen", {"action": "stop"}) == {"ok": True}
    utt = rig.sink.wait_type("utterance")
    assert utt["source"] == "command"


def test_stop_command(rig: Rig) -> None:
    wait_ready(rig)
    assert rig.command("stop", {}) == {"ok": True, "stopped": False}
    rig.synth.ms_per_char = 200
    rig.command("speak", {"replyId": "r1", "seq": 0, "text": "Talking for a while.", "final": True})
    rig.sink.wait_type("speech_started")
    assert rig.command("stop", {"reason": "user"}) == {"ok": True, "stopped": True}
    assert rig.sink.wait_type("speech_done")["interrupted"] is True
    assert rig.sink.of_type("barge_in") == []


def test_levels_while_listening_are_rate_limited(rig: Rig) -> None:
    wait_ready(rig)
    mark = rig.sink.mark()
    rig.ptt.press()
    time.sleep(1.0)
    rig.ptt.release()
    rig.sink.wait_type("utterance")
    levels = [e for e in rig.sink.events[mark:] if e["type"] == "level"]
    assert 3 <= len(levels) <= 16  # at most 15 per second
    assert all(0 <= e["mic"] <= 1 and 0 <= e["out"] <= 1 for e in levels)
    count = len(rig.sink.of_type("level"))
    time.sleep(0.3)
    assert len(rig.sink.of_type("level")) == count  # silent when idle


def test_config_applies_live_and_validates_keys(rig: Rig) -> None:
    wait_ready(rig)
    assert rig.command("config", {"pttKey": "hyperkey"})["ok"] is False
    resp = rig.command("config", {"pttKey": "f13", "voiceId": "v-42", "language": "de"})
    assert resp["ok"] and set(resp["applied"]) == {"pttKey", "voiceId", "language"}
    status = rig.command("status")
    assert status["pttKey"] == "f13" and status["voiceId"] == "v-42" and status["language"] == "de"
    rig.ptt.key_down("ctrl_r")  # the old key no longer works
    rig.ptt.key_up("ctrl_r")
    time.sleep(0.1)
    assert rig.daemon.state == "sleeping"
    rig.ptt.key_down("f13")
    rig.sink.wait_type("state", state="listening")
    rig.ptt.key_up("f13")
    rig.sink.wait_type("utterance")
    assert rig.transcribers[0].calls[-1][1] == "de"
    # Model change reloads in the background and announces ready again.
    rig.command("config", {"sttModel": "small.en"})
    ready = rig.sink.wait_for(lambda e: e["type"] == "ready" and e["sttModel"] == "fake-small.en")
    assert ready["voiceId"] == "v-42" and ready["pttKey"] == "f13"
    assert rig.command("config", {"sttModel": "small.en"})["applied"] == []  # unchanged: no reload


def test_status_matches_schema(rig: Rig) -> None:
    wait_ready(rig)
    status = rig.command("status")
    protocol.load_schema().validate(status, "StatusResponse")
    assert status["state"] == "sleeping" and status["platform"] == "linux"
    assert status["fishKeySet"] is True and status["inputDevice"] == "Fake Microphone"


def test_test_voice_speaks_default_line(rig: Rig) -> None:
    wait_ready(rig)
    resp = rig.command("test_voice")
    done = rig.sink.wait_type("speech_done", replyId=resp["replyId"])
    assert done["spokenText"].startswith("Good evening, sir.")


def test_test_voice_without_a_key_fails_up_front(sink: RecordingSink) -> None:
    missing = SynthError("fish_key_missing", "No Fish Audio API key is configured.")
    rig = start(build(sink, synth=FakeSynth(fail_open=missing)))
    try:
        wait_ready(rig)
        resp = rig.command("test_voice")
        assert resp["ok"] is False and resp["error"]["code"] == "fish_key_missing"
        assert "FISH_AUDIO_API_KEY" in resp["error"]["message"]
        time.sleep(0.2)
        assert not {"speech_started", "speech_done", "error"} & set(sink.types())
    finally:
        rig.daemon.request_exit(0)
        assert rig.thread is not None
        rig.thread.join(5)


def test_shutdown_command_ends_run(sink: RecordingSink) -> None:
    rig = start(build(sink))
    wait_ready(rig)
    assert rig.command("shutdown") == {"ok": True}
    assert rig.thread is not None
    rig.thread.join(3)
    assert not rig.thread.is_alive()


def test_heartbeat_watchdog_exits(sink: RecordingSink) -> None:
    rig = start(build(sink, config=DaemonConfig(heartbeat_timeout=0.3, heartbeat_grace=0.5)))
    assert rig.thread is not None
    for _ in range(5):
        rig.command("heartbeat")
        time.sleep(0.1)
    assert rig.thread.is_alive()
    rig.thread.join(3)
    assert not rig.thread.is_alive()


def test_missing_model_means_error_state(sink: RecordingSink) -> None:
    rig = start(build(sink, stt_fail=SttError("stt_model_missing", "not downloaded", "run /jarvis setup")))
    try:
        err = sink.wait_type("error")
        assert err["code"] == "stt_model_missing" and err["hint"] == "run /jarvis setup"
        sink.wait_type("state", state="error")
        resp = rig.command("listen", {"action": "start"})
        assert resp["ok"] is False and resp["error"]["code"] == "stt_model_missing"
        rig.ptt.press()
        wait_until(lambda: len(sink.of_type("error")) >= 3)
        assert rig.daemon.state == "error"
        # Speech still works without a speech-to-text model.
        rig.command("speak", {"replyId": "r1", "seq": 0, "text": "I can still talk.", "final": True})
        sink.wait_type("speech_done")
        assert sink.states()[-1] == "error"
    finally:
        rig.daemon.request_exit(0)
        assert rig.thread is not None
        rig.thread.join(5)


def test_clip_waits_for_model_still_loading(sink: RecordingSink) -> None:
    rig = start(build(sink, stt_delay=0.6))
    try:
        rig.ptt.press()
        time.sleep(0.2)
        rig.ptt.release()
        utt = sink.wait_type("utterance", timeout=5)
        assert utt["text"] == "turn on the lights"
        assert sink.of_type("ready")
    finally:
        rig.daemon.request_exit(0)
        assert rig.thread is not None
        rig.thread.join(5)


def test_ptt_unavailable_is_reported_but_not_fatal(sink: RecordingSink) -> None:
    rig = start(build(sink, ptt=FakePushToTalk(unavailable="no X server")))
    try:
        err = sink.wait_type("error")
        assert err["code"] == "ptt_unavailable" and err["fatal"] is False
        wait_ready(rig)
        assert rig.command("status")["pttArmed"] is False
        assert rig.command("listen", {"action": "start"})["ok"]
    finally:
        rig.daemon.request_exit(0)
        assert rig.thread is not None
        rig.thread.join(5)


def test_mic_errors_surface_on_start_and_on_listen(sink: RecordingSink) -> None:
    blocked = AudioError("mic_blocked", "access denied", "Settings > Privacy & security > Microphone")
    rig = start(build(sink, capture=FakeCapture(fail_with=blocked)))
    try:
        err = sink.wait_type("error")
        assert err["code"] == "mic_blocked" and "Privacy" in err["hint"]
        wait_ready(rig)
        resp = rig.command("listen", {"action": "start"})
        assert resp == {"ok": False, "error": {"code": "mic_blocked", "message": "access denied"}}
        assert rig.daemon.state == "sleeping"
    finally:
        rig.daemon.request_exit(0)
        assert rig.thread is not None
        rig.thread.join(5)


def test_end_to_end_with_fish_websocket(sink: RecordingSink) -> None:
    """The real Fish client against the fake server, through the whole daemon."""
    with FakeFishServer(api_key="k-123") as fish:
        synth = FishLiveSynth(FishSettings(api_key="k-123", base_url=fish.url))
        rig = start(build(sink, synth=synth))
        try:
            wait_ready(rig)
            rig.command("config", {"voiceId": "jarvis-voice"})
            rig.command("speak", {"replyId": "r1", "seq": 1, "text": "Shall I proceed?", "final": False})
            rig.command("speak", {"replyId": "r1", "seq": 0, "text": "The build is green.", "final": False})
            rig.command("speak", {"replyId": "r1", "seq": 2, "text": "", "final": True})
            done = sink.wait_type("speech_done", timeout=10)
            assert done == {
                "v": 1,
                "type": "speech_done",
                "replyId": "r1",
                "interrupted": False,
                "spokenText": "The build is green. Shall I proceed?",
            }
            session = fish.sessions[0]
            assert session.events() == ["start", "text", "flush", "text", "flush", "stop"]
            assert session.start["request"]["reference_id"] == "jarvis-voice"  # type: ignore[index]
            assert rig.playback.speech_frames_played == session.audio_bytes // 2
        finally:
            rig.daemon.request_exit(0)
            assert rig.thread is not None
            rig.thread.join(5)


def test_fish_auth_failure_through_daemon(sink: RecordingSink) -> None:
    with FakeFishServer(api_key="right") as fish:
        rig = start(build(sink, synth=FishLiveSynth(FishSettings(api_key="wrong", base_url=fish.url))))
        try:
            wait_ready(rig)
            rig.command("speak", {"replyId": "r1", "seq": 0, "text": "Hello.", "final": True})
            assert sink.wait_type("error")["code"] == "fish_auth_failed"
            assert sink.wait_type("speech_done")["interrupted"] is True
            assert rig.daemon.state == "sleeping"
        finally:
            rig.daemon.request_exit(0)
            assert rig.thread is not None
            rig.thread.join(5)


class SlowStartPushToTalk(FakePushToTalk):
    """Like pynput's backend, which imports pynput before it can take a new key."""

    def __init__(self, delay: float) -> None:
        super().__init__()
        self.delay = delay
        self.starting = threading.Event()

    def start(self, hotkey: Hotkey, on_down: Callable[[], None], on_up: Callable[[], None]) -> None:
        self.starting.set()
        time.sleep(self.delay)
        super().start(hotkey, on_down, on_up)


def test_config_during_ptt_start_still_arms_the_new_key(sink: RecordingSink) -> None:
    ptt = SlowStartPushToTalk(delay=0.3)
    rig = build(sink, ptt=ptt)
    starter = threading.Thread(target=rig.daemon.start, daemon=True)
    starter.start()
    try:
        assert ptt.starting.wait(5)
        assert rig.command("config", {"pttKey": "f13"})["ok"]  # the mod's config right after hello
        starter.join(5)
        assert ptt.detector is not None and ptt.detector.hotkey.tokens == ("f13",)
        assert rig.command("status")["pttKey"] == "f13"
        assert wait_ready(rig)["pttKey"] == "f13"
    finally:
        rig.daemon._shutdown_components()


def test_bad_ptt_key_does_not_void_the_rest_of_the_config(rig: Rig) -> None:
    wait_ready(rig)
    resp = rig.command("config", {"pttKey": "not a key", "language": "de", "voiceId": "abc"})
    assert resp["ok"] is False and resp["error"]["code"] == "bad_request"
    assert set(resp["applied"]) == {"language", "voiceId"}
    protocol.load_schema().validate(resp, "CommandResponse")
    status = rig.command("status")
    assert status["language"] == "de" and status["voiceId"] == "abc" and status["pttKey"] == "right ctrl"
    err = rig.sink.wait_type("error", code="bad_request")
    assert err["fatal"] is False and "not a key" in err["message"] and err["hint"]


def test_ready_is_announced_again_when_key_or_voice_change(rig: Rig) -> None:
    assert wait_ready(rig)["pttKey"] == "right ctrl"
    mark = rig.sink.mark()
    rig.command("config", {"pttKey": "f13"})
    ready = rig.sink.wait_type("ready", after=mark)
    assert ready["pttKey"] == "f13" and ready["sttModel"] == "fake-auto"
    mark = rig.sink.mark()
    rig.command("config", {"voiceId": "v-7", "language": "en"})
    assert rig.sink.wait_type("ready", after=mark)["voiceId"] == "v-7"
    mark = rig.sink.mark()
    rig.command("config", {"pttKey": "f13", "language": "fr"})  # nothing that ready carries changed
    time.sleep(0.1)
    assert [e for e in rig.sink.events[mark:] if e["type"] == "ready"] == []


def test_config_before_the_model_loads_shapes_the_first_ready(sink: RecordingSink) -> None:
    rig = start(build(sink, stt_delay=0.4))
    try:
        rig.command("config", {"pttKey": "f13", "voiceId": "v-1"})
        ready = wait_ready(rig)
        assert ready["pttKey"] == "f13" and ready["voiceId"] == "v-1"
        assert len(sink.of_type("ready")) == 1  # no announcement before the model is there
    finally:
        rig.daemon.request_exit(0)
        assert rig.thread is not None
        rig.thread.join(5)


def test_utterance_prewarms_the_voice_stream(rig: Rig) -> None:
    wait_ready(rig)
    rig.ptt.press()
    time.sleep(0.3)
    rig.ptt.release()
    rig.sink.wait_type("utterance")
    wait_until(lambda: len(rig.synth.streams) == 1)  # connected while Claude thinks
    reply = rig.command("speak", {"replyId": "r1", "seq": 0, "text": "Right away, sir.", "final": True})
    assert reply == {"ok": True}
    rig.sink.wait_type("speech_done", replyId="r1")
    assert len(rig.synth.streams) == 1 and rig.synth.streams[0].sentences == ["Right away, sir."]
