"""The sounddevice backend against an in-memory fake of the sounddevice module.

No PortAudio is needed: the fake reproduces the parts of the API we use
(query_*, InputStream/OutputStream with callbacks, WasapiSettings, PortAudioError),
and, on any OS, that Windows' PortAudio only starts a stream on a thread with COM.
"""

from __future__ import annotations

import sys
import threading
import time
import types
from collections.abc import Callable, Iterator
from typing import Any

import numpy as np
import pytest

from jarvis_voice import platform as plat
from jarvis_voice.audio import sd_backend
from jarvis_voice.audio.errors import AudioError
from jarvis_voice.audio.sd_backend import SoundDeviceCapture, SoundDevicePlayback, resolve_device
from jarvis_voice.platform import windows
from jarvis_voice.speech import SpeechPipeline

from conftest import RecordingSink, wait_until
from fakes import FakeSynth


class FakePortAudioError(Exception):
    pass


class FakeStream:
    def __init__(self, sd: FakeSD, kind: str, **kwargs: Any) -> None:
        self.sd, self.kind, self.kwargs = sd, kind, kwargs
        self.samplerate = kwargs["samplerate"]
        self.channels = kwargs["channels"]
        self.latency = 0.0
        self.active = False
        self.aborts = 0
        self.closed = False
        self.closed_with_com = False  # close() ran on a thread set up for COM
        self._thread: threading.Thread | None = None
        failure = sd.fail_open.get((kind, self.samplerate))
        if failure is not None:
            if sd.host_error_on_fail:
                sd._lib.host_error = sd.host_error_on_fail
            raise failure

    def start(self) -> None:
        if not getattr(self.sd.com, "ready", False):
            # What PortAudio 19.7's WASAPI start raised on the Windows PC, on a thread without COM.
            raise FakePortAudioError(
                "Error starting stream: Unanticipated host error",
                -9999,
                (11, 0, "WdmSyncIoctl: DeviceIoControl GLE = 0x00000490"),
            )
        self.active = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        block = int(self.samplerate * 0.01)
        started = time.monotonic()
        n = 0
        while self.active:
            # Real-time pacing by the clock, like a driver: CI machines oversleep
            # short sleeps, which would otherwise deliver too few blocks.
            due = int((time.monotonic() - started) * self.samplerate / block) + 1
            while n < due and self.active:
                self._deliver(block, n)
                n += 1
            time.sleep(block / self.samplerate)

    def _deliver(self, block: int, n: int) -> None:
        if self.kind == "input":
            if not self.sd.mute_input:
                data = np.full((block, self.channels), 0.2 + 0.001 * (n % 7), np.float32)
                self.kwargs["callback"](data, block, None, None)
        else:
            out = np.zeros((block, self.channels), np.float32)
            self.kwargs["callback"](out, block, None, None)
            self.sd.rendered.append(out[:, 0].copy())

    def abort(self) -> None:
        self.aborts += 1
        self._halt()
        if self.sd.finish_on_abort:
            self.kwargs["finished_callback"]()

    def end_by_itself(self) -> None:
        """The device went away: PortAudio ends the stream, and only its finished callback says so."""
        self._halt()
        self.kwargs["finished_callback"]()

    def _halt(self) -> None:
        self.active = False
        if self._thread:
            self._thread.join()

    def close(self) -> None:
        self.closed_with_com = getattr(self.sd.com, "ready", False)
        self.closed = True


class FakeLib:
    """sounddevice._lib: only the last-host-error slot PortAudio keeps per process."""

    def __init__(self) -> None:
        self.host_error = 0

    def Pa_GetLastHostErrorInfo(self) -> types.SimpleNamespace:  # noqa: N802 - PortAudio's name
        return types.SimpleNamespace(hostApiType=13, errorCode=self.host_error, errorText=b"UNKNOWN ERROR")


def dev(name: str, api: int, ins: int, outs: int) -> dict[str, Any]:
    return {
        "name": name,
        "hostapi": api,
        "max_input_channels": ins,
        "max_output_channels": outs,
        "default_samplerate": 48000.0,
    }


class FakeSD(types.ModuleType):
    def __init__(self) -> None:
        super().__init__("sounddevice")
        self.PortAudioError = FakePortAudioError
        self.default = types.SimpleNamespace(device=[0, 2])
        self.fail_open: dict[tuple[str, int], Exception] = {}
        self._lib = FakeLib()
        self.host_error_on_fail = 0  # what a failing open leaves in the host error slot
        self.mute_input = False
        self.finish_on_abort = True  # PortAudio calls the finished callback on abort() too
        self.streams: list[FakeStream] = []
        self.rendered: list[np.ndarray] = []
        self.com = threading.local()  # .ready: ensure_com ran on that thread
        self.hostapis: list[dict[str, Any]] = [
            {"name": "MME", "default_input_device": 0, "default_output_device": 1},
            {"name": "Windows WASAPI", "default_input_device": 4, "default_output_device": 2},
        ]
        self.devices: list[dict[str, Any]] = [
            dev("Microphone (PRO X Wireless Gaming Headset)", 0, 1, 0),
            dev("Speakers (PRO X Wireless Gaming Headset)", 0, 0, 2),
            dev("Speakers (Realtek(R) Audio)", 1, 0, 2),
            dev("Headset Earphone (PRO X Wireless Gaming Headset)", 1, 0, 2),
            dev("Microphone (Webcam)", 1, 2, 0),
            dev("Headset Microphone (PRO X Wireless Gaming Headset)", 1, 1, 0),
        ]
        self.opens = {"input": 0, "output": 0}  # stream constructions, including ones that failed
        self.terminations = 0
        self.live_at_terminate: list[FakeStream] = []  # streams PortAudio would have freed under us
        sd = self

        class WasapiSettings:
            def __init__(
                self, exclusive: bool = False, auto_convert: bool = False, explicit_sample_format: bool = False
            ) -> None:
                self.exclusive, self.auto_convert = exclusive, auto_convert

        def make(kind: str) -> Any:
            def factory(**kwargs: Any) -> FakeStream:
                sd.opens[kind] += 1
                stream = FakeStream(sd, kind, **kwargs)
                sd.streams.append(stream)
                return stream

            return factory

        self.WasapiSettings = WasapiSettings
        self.InputStream = make("input")
        self.OutputStream = make("output")

    def query_hostapis(self) -> list[dict[str, Any]]:
        return self.hostapis

    def query_devices(self) -> list[dict[str, Any]]:
        return self.devices

    def _terminate(self) -> None:
        self._require_com("_terminate")
        self.terminations += 1
        self.live_at_terminate += self.unclosed()
        for stream in self.streams:
            stream.active = False

    def _initialize(self) -> None:
        self._require_com("_initialize")

    def _require_com(self, call: str) -> None:
        # Re-initialising PortAudio is a WASAPI call like a stream start: only on a thread set up for COM.
        if not getattr(self.com, "ready", False):
            raise AssertionError(f"{call} on thread {threading.current_thread().name!r} without ensure_com")

    def ensure_com(self) -> None:
        """Stands in for platform.windows.ensure_com."""
        self.com.ready = True

    def unclosed(self) -> list[FakeStream]:
        """Streams constructed and never closed: each one a PortAudio stream leaked."""
        return [s for s in self.streams if not s.closed]

    def outputs(self) -> list[FakeStream]:
        return [s for s in self.streams if s.kind == "output"]

    def unplug_output(self) -> None:
        """Every output open fails, the way it does for a device that is gone."""
        gone = FakePortAudioError("Error opening OutputStream: Invalid device", -9996)
        self.fail_open[("output", 24_000)] = self.fail_open[("output", 48_000)] = gone


@pytest.fixture
def sd(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeSD]:
    fake = FakeSD()
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    monkeypatch.setattr(plat, "current", lambda: windows)  # WASAPI-first selection, Windows hints
    monkeypatch.setattr(windows, "ensure_com", fake.ensure_com)
    yield fake
    for stream in fake.streams:
        stream.active = False


def on_worker(fn: Callable[[], object], name: str) -> None:
    """Run ``fn`` on a new thread, the way the helper's worker threads call the backend."""
    errors: list[BaseException] = []

    def run() -> None:
        try:
            fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the test thread
            errors.append(exc)

    thread = threading.Thread(target=run, name=name)
    thread.start()
    thread.join(5.0)
    if errors:
        raise errors[0]


def test_capture_prefers_wasapi_device_by_name_with_auto_convert(sd: FakeSD) -> None:
    cap = SoundDeviceCapture("PRO X")
    cap.open()
    try:
        stream = sd.streams[0]
        assert stream.kwargs["device"] == 5  # the WASAPI headset mic, not the MME one
        assert stream.kwargs["samplerate"] == 16_000 and stream.kwargs["channels"] == 1
        assert stream.kwargs["dtype"] == "float32"
        assert stream.kwargs["extra_settings"].auto_convert is True
        assert cap.device_name == "Headset Microphone (PRO X Wireless Gaming Headset)"
        time.sleep(0.4)  # fill the pre-roll
        cap.begin()
        time.sleep(0.6)
        clip = cap.end()
        assert clip.dtype == np.float32 and clip.size > 0.3 * 16_000
        assert 0 < cap.level <= 1
    finally:
        cap.close()


def test_capture_default_device_when_name_unknown(sd: FakeSD) -> None:
    cap = SoundDeviceCapture("Blue Yeti")
    cap.open()
    try:
        assert sd.streams[0].kwargs["device"] == 4  # WASAPI default input
    finally:
        cap.close()


def test_capture_falls_back_to_native_rate_and_resamples(sd: FakeSD) -> None:
    sd.fail_open[("input", 16_000)] = FakePortAudioError("Invalid sample rate", -9997)
    cap = SoundDeviceCapture(None)
    cap.open()
    try:
        assert sd.streams[-1].samplerate == 48_000
        time.sleep(0.4)
        cap.begin()
        began = time.monotonic()
        time.sleep(0.6)
        clip = cap.end()
        recorded = time.monotonic() - began  # a busy machine may oversleep
        # 0.3 s pre-roll + what was recorded, captured at 48 kHz and resampled to 16 kHz.
        assert 0.6 * 16_000 < clip.size < (0.3 + recorded + 0.2) * 16_000
    finally:
        cap.close()


def test_capture_access_denied_maps_to_mic_blocked(sd: FakeSD) -> None:
    denied = FakePortAudioError("Error opening InputStream: Unanticipated host error", -9999, (1, -2147024891, ""))
    sd.fail_open[("input", 16_000)] = denied
    sd.fail_open[("input", 48_000)] = denied
    with pytest.raises(AudioError) as info:
        SoundDeviceCapture(None).open()
    assert info.value.code == "mic_blocked"
    assert "ms-settings:privacy-microphone" in (info.value.hint or "")


@pytest.mark.parametrize(
    ("hresult", "code"), [(-2004287478, "mic_in_use"), (-2147024891, "mic_blocked"), (0, "no_input_device")]
)
def test_capture_reads_the_hresult_wasapi_hides(sd: FakeSD, hresult: int, code: str) -> None:
    """PortAudio 19.7 reports an exclusive-mode app or the privacy switch as 'Invalid device' (-9996)."""
    invalid = FakePortAudioError("Error opening InputStream: Invalid device", -9996)
    sd.fail_open[("input", 16_000)] = invalid
    sd.fail_open[("input", 48_000)] = invalid
    sd.host_error_on_fail = hresult
    with pytest.raises(AudioError) as info:
        SoundDeviceCapture(None).open()
    assert info.value.code == code


def test_last_host_error_without_portaudio_details() -> None:
    assert sd_backend.last_host_error(types.SimpleNamespace()) is None  # a sounddevice without _lib


def test_capture_detects_a_stalled_device_and_reopens(sd: FakeSD) -> None:
    cap = SoundDeviceCapture(None)
    cap.open()
    sd.mute_input = True  # stream still "open" but the driver delivers nothing
    cap.begin()
    time.sleep(0.7)
    with pytest.raises(AudioError) as info:
        cap.end()
    assert info.value.code == "no_input_device"
    assert not cap.is_open
    sd.mute_input = False
    cap.open()
    assert len(sd.streams) == 2 and cap.is_open
    cap.close()


def test_stop_with_no_speech_left_leaves_the_stream_running(sd: FakeSD) -> None:
    pb = SoundDevicePlayback("PRO X", idle_close_s=None)
    pb.open()
    try:
        stream = sd.streams[0]
        pb.stop_speech()  # e.g. a reply that failed before any audio (no Fish key)
        assert stream.aborts == 0 and stream.active
        pb.add_speech(np.full(240, 0.3, np.float32), pb.speech_generation)
        wait_until(pb.speech_idle)
        pb.stop_speech()  # everything already played: nothing to cut
        assert stream.aborts == 0 and stream.active and len(sd.streams) == 1
    finally:
        pb.close()


def test_playback_plays_stops_instantly_and_restarts(sd: FakeSD) -> None:
    pb = SoundDevicePlayback("PRO X", idle_close_s=None)
    pb.open()
    try:
        stream = sd.streams[0]
        assert stream.kwargs["device"] == 3 and stream.kwargs["samplerate"] == 24_000
        assert stream.kwargs["extra_settings"].auto_convert is True
        pb.add_speech(np.full(24_000, 0.3, np.float32), pb.speech_generation)
        wait_until(lambda: pb.speech_frames_played > 1000)
        assert pb.level > 0
        pb.stop_speech()
        assert stream.aborts == 1 and stream.active  # aborted, then restarted for chimes
        assert pb.speech_idle()
        played = pb.speech_frames_played
        time.sleep(0.1)
        assert pb.speech_frames_played == played
        pb.add_effect(np.full(240, 0.1, np.float32))
        wait_until(lambda: any(block.any() for block in sd.rendered[-20:]))
        pb.open()
        assert len(sd.streams) == 1  # still alive: no reopen after a stop
    finally:
        pb.close()


def test_playback_resamples_when_device_refuses_24k(sd: FakeSD) -> None:
    sd.fail_open[("output", 24_000)] = FakePortAudioError("Invalid sample rate", -9997)
    pb = SoundDevicePlayback(None, idle_close_s=None)
    pb.open()
    try:
        assert sd.streams[-1].samplerate == 48_000
        gen = pb.speech_generation
        pb.add_speech(np.full(2400, 0.3, np.float32), gen)  # 100 ms at 24 kHz
        pb.end_speech(gen)  # end of reply: the resampler's held-back tail is released
        wait_until(lambda: pb.speech_idle() and pb.speech_frames_played > 0)
        # Counters stay in 24 kHz units whatever the device rate is.
        assert 1800 <= pb.speech_frames_played <= 2600
    finally:
        pb.close()


def test_the_echo_canceller_gets_exactly_what_the_device_plays(sd: FakeSD) -> None:
    sd.fail_open[("output", 24_000)] = FakePortAudioError("Invalid sample rate", -9997)
    pb = SoundDevicePlayback(None, idle_close_s=None)
    tapped: list[tuple[np.ndarray, int]] = []
    pb.set_far_listener(lambda block, rate: tapped.append((block, rate)))
    pb.open()
    try:
        pb.add_effect(np.full(2400, 0.1, np.float32))
        wait_until(lambda: sum(bool(block.any()) for block, _ in tapped) >= 3)
        pb.set_far_listener(None)
        time.sleep(0.03)  # a callback already running may still deliver one
        count = len(tapped)
        time.sleep(0.05)
        assert len(tapped) == count  # detached, while the stream plays on
    finally:
        pb.close()
    assert {rate for _, rate in tapped} == {48_000}  # at the device's rate, after resampling
    for (block, _), played in zip(tapped, sd.rendered, strict=False):
        np.testing.assert_array_equal(block, played)


def test_the_echo_canceller_hears_when_the_output_stops(sd: FakeSD) -> None:
    """A stop aborts and restarts the stream, and the idle close ends it: either way the canceller hears None
    after the stream's last block, and can stand silence in for it and start afresh."""
    pb = SoundDevicePlayback(None, idle_close_s=0.2)
    tapped: list[np.ndarray | None] = []
    pb.set_far_listener(lambda block, rate: tapped.append(block))
    pb.open()
    try:
        assert pb.device_rate == 24_000
        stream = sd.outputs()[0]
        pb.add_speech(np.full(24_000, 0.3, np.float32), pb.speech_generation)
        wait_until(lambda: pb.speech_frames_played > 1000)
        pb.stop_speech()  # aborted, then restarted for chimes
        stop = next(i for i, block in enumerate(tapped) if block is None)
        assert any(block is not None and block.any() for block in tapped[:stop])  # the speech it cut
        wait_until(lambda: any(block is not None for block in tapped[stop:]))  # the restarted stream plays on
        wait_until(lambda: stream.closed and tapped[-1] is None, 3.0)  # idle: the reaper closed it, and said so
    finally:
        pb.close()


def stops(tapped: list[np.ndarray | None]) -> int:
    return sum(block is None for block in tapped)


def test_a_stop_tells_the_echo_canceller_itself(sd: FakeSD) -> None:
    """Not only through the finished callback abort() fires: a stop says so whatever the stream does."""
    sd.finish_on_abort = False
    pb = SoundDevicePlayback(None, idle_close_s=None)
    tapped: list[np.ndarray | None] = []
    pb.set_far_listener(lambda block, rate: tapped.append(block))
    pb.open()
    try:
        stream = sd.outputs()[0]
        pb.add_speech(np.full(24_000, 0.3, np.float32), pb.speech_generation)
        wait_until(lambda: pb.speech_frames_played > 1000)
        pb.stop_speech()
        assert stream.aborts == 1 and stream.active
        assert stops(tapped) == 1  # between the cut speech and the restarted stream
        stop = next(i for i, block in enumerate(tapped) if block is None)
        assert any(block is not None and block.any() for block in tapped[:stop])
        wait_until(lambda: len(tapped) > stop + 1)  # the restarted stream plays on
    finally:
        pb.close()


def test_a_close_tells_the_echo_canceller_itself(sd: FakeSD) -> None:
    sd.finish_on_abort = False
    pb = SoundDevicePlayback(None, idle_close_s=None)
    tapped: list[np.ndarray | None] = []
    pb.set_far_listener(lambda block, rate: tapped.append(block))
    pb.open()
    try:
        stream = sd.outputs()[0]
        wait_until(lambda: len(tapped) >= 3)
        pb.close()
        assert stream.closed and tapped[-1] is None and stops(tapped) == 1
    finally:
        pb.close()


def test_the_echo_canceller_hears_when_the_device_goes_away(sd: FakeSD) -> None:
    """PortAudio ends the stream by itself, with no abort or close of ours: only the finished callback knows."""
    pb = SoundDevicePlayback(None, idle_close_s=None)
    tapped: list[np.ndarray | None] = []
    pb.set_far_listener(lambda block, rate: tapped.append(block))
    pb.open()
    try:
        stream = sd.outputs()[0]
        wait_until(lambda: len(tapped) >= 3)
        stream.end_by_itself()
        assert stream.aborts == 0 and tapped[-1] is None and stops(tapped) == 1
        pb.add_effect(np.full(240, 0.1, np.float32))  # the next sound reopens the output
        assert len(sd.outputs()) == 2 and sd.outputs()[1].active
    finally:
        pb.close()


def test_the_device_rate_is_what_the_output_opened_at(sd: FakeSD) -> None:
    sd.fail_open[("output", 24_000)] = FakePortAudioError("Invalid sample rate", -9997)
    pb = SoundDevicePlayback(None, idle_close_s=None)
    assert pb.device_rate == 24_000  # until it opens: the rate it tries first
    pb.open()
    try:
        assert pb.device_rate == 48_000
    finally:
        pb.close()


def test_capture_reports_its_input_latency(sd: FakeSD) -> None:
    capture = SoundDeviceCapture("PRO X")
    assert capture.input_latency == 0.0
    capture.open()
    try:
        sd.streams[-1].latency = 0.012
        assert capture.input_latency == 0.012
    finally:
        capture.close()


def test_playback_closes_when_idle_and_reopens_on_audio(sd: FakeSD) -> None:
    pb = SoundDevicePlayback(None, idle_close_s=0.2)
    pb.open()
    try:
        first = sd.streams[0]
        wait_until(lambda: first.closed, 3.0)
        assert first.closed_with_com  # on the reaper's thread, which opened nothing
        pb.add_speech(np.full(240, 0.3, np.float32))
        assert len(sd.streams) == 2 and sd.streams[1].active
        wait_until(lambda: pb.speech_idle())
    finally:
        pb.close()


def test_open_reply_holds_the_output_through_a_long_silence(sd: FakeSD, sink: RecordingSink) -> None:
    """Claude runs a tool between two sentences: the idle close must not pull the output from under the reply."""
    pb = SoundDevicePlayback(None, idle_close_s=0.2)
    pipe = SpeechPipeline(FakeSynth(), pb, sink).start()
    try:
        pipe.speak("r1", 0, "Let me check.", False)
        sink.wait_type("speech_started")
        wait_until(pb.speech_idle)
        stream = sd.outputs()[0]
        time.sleep(0.8)  # four times idle_close_s without a sound
        assert stream.active and not stream.closed
        pipe.speak("r1", 1, "All tests pass.", False)
        pipe.speak("r1", 2, "", True)
        assert sink.wait_type("speech_done")["interrupted"] is False
        assert sd.outputs() == [stream]  # the whole reply played on one stream
        wait_until(lambda: stream.closed, 3.0)  # the reply is done: the headset may sleep again
    finally:
        pipe.close()
        pb.close()


def test_a_held_output_still_closes_after_the_cap(sd: FakeSD) -> None:
    pb = SoundDevicePlayback(None, idle_close_s=0.2, hold_max_s=2.0)
    pb.open()
    pb.hold_open(True)  # a reply whose final never comes
    try:
        stream = sd.outputs()[0]
        time.sleep(0.5)
        assert not stream.closed
        wait_until(lambda: stream.closed, 5.0)
    finally:
        pb.close()


@pytest.mark.parametrize("com", [True, False], ids=["ensure_com", "without_com"])
def test_output_reopened_mid_reply_from_a_worker_thread(
    sd: FakeSD, sink: RecordingSink, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, com: bool
) -> None:
    """The voice.log case: the output closed mid-reply and a worker thread (not the one that opened it) reopened it."""
    pb = SoundDevicePlayback(None, idle_close_s=None)
    pb.open()  # on this thread, as the daemon does at start-up
    if not com:
        monkeypatch.setattr(windows, "ensure_com", lambda: None)  # the helper before the fix
    pipe = SpeechPipeline(FakeSynth(), pb, sink).start()
    try:
        pipe.speak("r1", 0, "Let me check.", False)
        sink.wait_type("speech_started")
        wait_until(pb.speech_idle)
        sd.outputs()[0].abort()  # the stream ended during the tool pause
        with caplog.at_level("WARNING", logger="jarvis_voice.audio.sd_backend"):
            pipe.speak("r1", 1, "All tests pass.", True)  # its audio reopens the output on the speech thread
            done = sink.wait_type("speech_done")
        if com:
            assert done["interrupted"] is False and sink.of_type("error") == []
            assert len(sd.outputs()) == 2 and sd.outputs()[1].active
        else:
            assert done["interrupted"] is True
            [err] = sink.of_type("error")
            assert err["code"] == "no_output_device"
            assert err["message"] == "The audio output could not be reopened (headset disconnected or asleep?)."
            assert err["hint"] == windows.no_output_device_hint()
            assert "Unanticipated host error" in caplog.text  # PortAudio's own words stay in the log
            assert "host error 0x00000000" not in caplog.text
    finally:
        pipe.close()
        pb.close()


def test_stop_restarts_the_output_from_another_thread(sd: FakeSD) -> None:
    """stop() runs on the thread of the barge-in or command; the restart must start the stream there."""
    pb = SoundDevicePlayback("PRO X", idle_close_s=None)
    pb.open()
    try:
        stream = sd.outputs()[0]
        pb.add_speech(np.full(24_000, 0.3, np.float32), pb.speech_generation)
        wait_until(lambda: pb.speech_frames_played > 1000)
        on_worker(pb.stop_speech, "control-request")
        assert stream.aborts == 1 and stream.active and pb.speech_idle()
        pb.add_effect(np.full(240, 0.1, np.float32))
        wait_until(lambda: any(block.any() for block in sd.rendered[-20:]))
        assert len(sd.outputs()) == 1  # restarted, not reopened
    finally:
        pb.close()


def test_stop_does_not_restart_a_stream_closed_under_it(sd: FakeSD) -> None:
    """The reaper (or a reopen, or a rescan) closes the output just as a stop is about to restart it."""
    pb = SoundDevicePlayback(None, idle_close_s=None)
    pb.open()
    try:
        stream = sd.outputs()[0]
        pb.add_speech(np.full(24_000, 0.3, np.float32), pb.speech_generation)
        wait_until(lambda: pb.speech_frames_played > 0)
        stopper = threading.Thread(target=pb.stop_speech, name="control-request", daemon=True)
        with pb._lock:  # what the reaper holds while it closes an idle output
            stopper.start()
            time.sleep(0.2)
            assert stopper.is_alive() and stream.aborts == 0  # the stop waits for the lock
            pb._close_locked()
        stopper.join(2.0)
        assert not stopper.is_alive()
        assert stream.closed and not stream.active and stream.aborts == 1  # the close's abort, no restart after it
    finally:
        pb.close()


def test_open_on_a_running_stream_does_not_wait_for_the_portaudio_lock(sd: FakeSD) -> None:
    """A rescan or the other device's slow open holds the PortAudio lock; an open with nothing to do skips it."""
    cap = SoundDeviceCapture(None)
    pb = SoundDevicePlayback(None, idle_close_s=None)
    cap.open()
    pb.open()
    opener = threading.Thread(target=lambda: (cap.open(), pb.open()), name="speech", daemon=True)
    try:
        with sd_backend._PORTAUDIO_LOCK:
            opener.start()
            opener.join(2.0)
            assert not opener.is_alive()
        assert len(sd.streams) == 2
    finally:
        cap.close()
        pb.close()


@pytest.mark.parametrize("kind", ["input", "output"])
def test_a_stream_that_will_not_start_is_closed(sd: FakeSD, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    monkeypatch.setattr(windows, "ensure_com", lambda: None)  # every start fails, as on a thread without COM
    owner = SoundDeviceCapture(None) if kind == "input" else SoundDevicePlayback(None, idle_close_s=None)
    with pytest.raises(AudioError):
        on_worker(owner.open, "speech")
    assert sd.opens[kind] == 3 and sd.unclosed() == []  # each attempt opened, failed to start and was closed


def test_capture_opens_from_a_worker_thread(sd: FakeSD) -> None:
    cap = SoundDeviceCapture(None)
    try:
        on_worker(cap.open, "hands-free")
        assert cap.is_open and sd.streams[0].active
    finally:
        cap.close()


def test_rescan_closes_every_stream_before_portaudio_frees_them(sd: FakeSD) -> None:
    cap = SoundDeviceCapture(None)
    pb = SoundDevicePlayback(None, idle_close_s=None)
    cap.open()
    pb.open()
    try:

        def rescan_and_reopen() -> None:
            sd_backend.rescan_devices(cap, pb)
            cap.open()  # what the daemon does next, on the same thread

        on_worker(rescan_and_reopen, "control-request")
        assert sd.terminations == 1 and sd.live_at_terminate == []
        assert cap.is_open and [s.kind for s in sd.unclosed()] == ["input"]
    finally:
        cap.close()
        pb.close()


def test_rescan_sets_up_com_on_its_own_thread(sd: FakeSD) -> None:
    # Nothing is open (the output idled out), so no stream close sets up COM on that thread first.
    owners = SoundDeviceCapture(None), SoundDevicePlayback(None, idle_close_s=None)
    on_worker(lambda: sd_backend.rescan_devices(*owners), "control-request")
    assert sd.terminations == 1


def test_output_lost_mid_reply_fails_it_once_without_hammering(sd: FakeSD, sink: RecordingSink) -> None:
    """The output went away during a tool pause and cannot be reopened."""
    pb = SoundDevicePlayback("PRO X", idle_close_s=None)
    pipe = SpeechPipeline(FakeSynth(), pb, sink).start()
    try:
        pipe.speak("r1", 0, "Let me check.", False)
        sink.wait_type("speech_started")
        wait_until(pb.speech_idle)
        sd.unplug_output()
        sd.outputs()[0].abort()  # PortAudio ends the stream
        before = sd.opens["output"]
        pipe.speak("r1", 1, "All tests pass.", False)  # its audio arrives in pieces, each wanting the output
        pipe.speak("r1", 2, "The build is green too.", False)
        err = sink.wait_type("error", timeout=2.0)  # at once, not after the 5 s stall timeout
        assert err["code"] == "no_output_device" and err["fatal"] is False
        assert err["message"] == "The audio output could not be reopened (headset disconnected or asleep?)."
        assert sink.wait_type("speech_done")["interrupted"] is True
        time.sleep(0.3)
        assert len(sink.of_type("error")) == 1 and len(sink.of_type("speech_done")) == 1
        assert sd.opens["output"] - before == 3  # one open (its three attempts), not one per piece of audio
        assert all(s.closed for s in sd.outputs())
        # The headset is back: the next reply opens it and plays.
        sd.fail_open.clear()
        pipe.speak("r2", 0, "Back again.", True)
        assert sink.wait_type("speech_done", replyId="r2")["interrupted"] is False
    finally:
        pipe.close()
        pb.close()


def test_new_audio_does_not_retry_a_failed_output_on_every_chunk(sd: FakeSD, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sd_backend, "OPEN_RETRY_S", 0.3)
    pb = SoundDevicePlayback(None, idle_close_s=None)
    sd.unplug_output()
    try:
        for _ in range(20):
            pb.add_speech(np.full(240, 0.3, np.float32))
        assert sd.opens["output"] == 3
        assert pb.open_error is not None and pb.open_error.code == "no_output_device"
        assert pb.open_error.message == "The audio output could not be opened (headset disconnected or asleep?)."
        sd.fail_open.clear()
        time.sleep(0.4)  # past the back-off: the next chunk tries again
        pb.add_speech(np.full(240, 0.3, np.float32))
        assert sd.outputs()[-1].active and pb.open_error is None
        wait_until(pb.speech_idle)  # what queued meanwhile plays
    finally:
        pb.close()


def test_output_prefers_wasapi_when_another_host_api_has_the_same_name(sd: FakeSD) -> None:
    sd.devices[3]["name"] = "Speakers (PRO X Wireless Gaming Headset)"  # as Windows names it in both lists
    device = resolve_device(sd, "output", "Speakers (PRO X Wireless Gaming Headset)")
    assert (device.index, device.hostapi) == (3, "Windows WASAPI")  # not the MME entry listed first


def test_output_ignores_a_default_index_outside_wasapi(sd: FakeSD) -> None:
    sd.hostapis[1]["default_output_device"] = 1  # a stale index: now an MME device
    device = resolve_device(sd, "output", None)
    assert device.hostapi == "Windows WASAPI"


def test_output_falls_back_from_wasapi_only_when_it_has_none_and_says_so(
    sd: FakeSD, caplog: pytest.LogCaptureFixture
) -> None:
    for d in sd.devices[2:4]:
        d["max_output_channels"] = 0  # WASAPI lists no output
    with caplog.at_level("WARNING", logger="jarvis_voice.audio.sd_backend"):
        device = resolve_device(sd, "output", None)
    assert (device.index, device.hostapi) == (1, "MME")
    assert "no Windows WASAPI output device" in caplog.text and "(MME)" in caplog.text


def test_missing_portaudio_is_an_audio_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "sounddevice", None)  # import raises ImportError
    with pytest.raises(AudioError) as info:
        sd_backend.load_sounddevice("output")
    assert info.value.code == "no_output_device"


def test_missing_portaudio_is_a_device_error_with_a_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "sounddevice", None)  # makes "import sounddevice" raise ImportError
    with pytest.raises(AudioError) as info:
        sd_backend.load_sounddevice("output")
    assert info.value.code == "no_output_device" and info.value.hint and "PortAudio" in info.value.hint
