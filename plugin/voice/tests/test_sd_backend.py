"""The sounddevice backend against an in-memory fake of the sounddevice module.

No PortAudio is needed: the fake reproduces the parts of the API we use
(query_*, InputStream/OutputStream with callbacks, WasapiSettings, PortAudioError).
"""

from __future__ import annotations

import sys
import threading
import time
import types
from collections.abc import Iterator
from typing import Any

import numpy as np
import pytest

from jarvis_voice import platform as plat
from jarvis_voice.audio import sd_backend
from jarvis_voice.audio.errors import AudioError
from jarvis_voice.audio.sd_backend import SoundDeviceCapture, SoundDevicePlayback
from jarvis_voice.platform import windows

from conftest import wait_until


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
        self._thread: threading.Thread | None = None
        failure = sd.fail_open.get((kind, self.samplerate))
        if failure is not None:
            if sd.host_error_on_fail:
                sd._lib.host_error = sd.host_error_on_fail
            raise failure

    def start(self) -> None:
        self.active = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        block = int(self.samplerate * 0.01)
        n = 0
        while self.active:
            if self.kind == "input":
                if not self.sd.mute_input:
                    data = np.full((block, self.channels), 0.2 + 0.001 * (n % 7), np.float32)
                    self.kwargs["callback"](data, block, None, None)
            else:
                out = np.zeros((block, self.channels), np.float32)
                self.kwargs["callback"](out, block, None, None)
                self.sd.rendered.append(out[:, 0].copy())
            n += 1
            time.sleep(block / self.samplerate)  # real-time pacing

    def abort(self) -> None:
        self.aborts += 1
        self.active = False
        if self._thread:
            self._thread.join()
        self.kwargs["finished_callback"]()

    def close(self) -> None:
        self.closed = True


class FakeLib:
    """sounddevice._lib: only the last-host-error slot PortAudio keeps per process."""

    def __init__(self) -> None:
        self.host_error = 0

    def Pa_GetLastHostErrorInfo(self) -> types.SimpleNamespace:  # noqa: N802 - PortAudio's name
        return types.SimpleNamespace(hostApiType=13, errorCode=self.host_error, errorText=b"UNKNOWN ERROR")


class FakeSD(types.ModuleType):
    def __init__(self) -> None:
        super().__init__("sounddevice")
        self.PortAudioError = FakePortAudioError
        self.default = types.SimpleNamespace(device=[0, 2])
        self.fail_open: dict[tuple[str, int], Exception] = {}
        self._lib = FakeLib()
        self.host_error_on_fail = 0  # what a failing open leaves in the host error slot
        self.mute_input = False
        self.streams: list[FakeStream] = []
        self.rendered: list[np.ndarray] = []
        sd = self

        class WasapiSettings:
            def __init__(
                self, exclusive: bool = False, auto_convert: bool = False, explicit_sample_format: bool = False
            ) -> None:
                self.exclusive, self.auto_convert = exclusive, auto_convert

        def make(kind: str) -> Any:
            def factory(**kwargs: Any) -> FakeStream:
                stream = FakeStream(sd, kind, **kwargs)
                sd.streams.append(stream)
                return stream

            return factory

        self.WasapiSettings = WasapiSettings
        self.InputStream = make("input")
        self.OutputStream = make("output")

    @staticmethod
    def query_hostapis() -> list[dict[str, Any]]:
        return [
            {"name": "MME", "default_input_device": 0, "default_output_device": 1},
            {"name": "Windows WASAPI", "default_input_device": 4, "default_output_device": 2},
        ]

    @staticmethod
    def query_devices() -> list[dict[str, Any]]:
        def dev(name: str, api: int, ins: int, outs: int) -> dict[str, Any]:
            return {
                "name": name,
                "hostapi": api,
                "max_input_channels": ins,
                "max_output_channels": outs,
                "default_samplerate": 48000.0,
            }

        return [
            dev("Microphone (PRO X Wireless Gaming Headset)", 0, 1, 0),
            dev("Speakers (PRO X Wireless Gaming Headset)", 0, 0, 2),
            dev("Speakers (Realtek(R) Audio)", 1, 0, 2),
            dev("Headset Earphone (PRO X Wireless Gaming Headset)", 1, 0, 2),
            dev("Microphone (Webcam)", 1, 2, 0),
            dev("Headset Microphone (PRO X Wireless Gaming Headset)", 1, 1, 0),
        ]


@pytest.fixture
def sd(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeSD]:
    fake = FakeSD()
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    monkeypatch.setattr(plat, "current", lambda: windows)  # WASAPI-first selection, Windows hints
    yield fake
    for stream in fake.streams:
        stream.active = False


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
        time.sleep(0.6)
        clip = cap.end()
        # 0.3 s pre-roll + ~0.6 s, captured at 48 kHz and resampled to 16 kHz.
        assert 0.6 * 16_000 < clip.size < 1.5 * 16_000
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


def test_playback_closes_when_idle_and_reopens_on_audio(sd: FakeSD) -> None:
    pb = SoundDevicePlayback(None, idle_close_s=0.2)
    pb.open()
    try:
        first = sd.streams[0]
        wait_until(lambda: first.closed, 3.0)
        pb.add_speech(np.full(240, 0.3, np.float32))
        assert len(sd.streams) == 2 and sd.streams[1].active
        wait_until(lambda: pb.speech_idle())
    finally:
        pb.close()


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
