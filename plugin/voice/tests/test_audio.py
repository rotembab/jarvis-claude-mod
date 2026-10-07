from __future__ import annotations

import numpy as np
import pytest

from jarvis_voice.audio import chimes
from jarvis_voice.audio.capture import CaptureBuffer, to_stt_rate
from jarvis_voice.audio.devices import DeviceInfo, select_device
from jarvis_voice.audio.errors import AudioError, classify_audio_error, digital_silence_error
from jarvis_voice.audio.fake import FakePlayback
from jarvis_voice.audio.levels import is_digital_silence, is_silent, meter
from jarvis_voice.audio.playback import PlaybackMixer
from jarvis_voice.platform import name as plat_name

from conftest import wait_until

# --------------------------------------------------------------------------- mixer


def ramp(n: int, start: float = 0.0) -> np.ndarray:
    return (start + np.arange(n, dtype=np.float32)) / 10_000


def test_mixer_plays_speech_in_order_across_block_boundaries() -> None:
    mixer = PlaybackMixer(24_000)
    a, b = ramp(150), ramp(100, 150)
    mixer.add_speech(a)
    mixer.add_speech(b)
    out = np.concatenate([mixer.render(64) for _ in range(5)])
    np.testing.assert_allclose(out[:250], np.concatenate([a, b]))
    assert not out[250:].any()
    assert mixer.frames_played == 250 and mixer.frames_queued == 250
    assert mixer.speech_pending() == 0


def test_pause_holds_speech_but_effects_still_play() -> None:
    mixer = PlaybackMixer(24_000)
    mixer.add_speech(np.full(100, 0.5, np.float32))
    mixer.add_effect(np.full(10, 0.25, np.float32))
    mixer.paused = True
    out = mixer.render(50)
    assert out[:10].tolist() == [0.25] * 10 and not out[10:].any()
    assert mixer.frames_played == 0 and mixer.speech_pending() == 100
    mixer.paused = False
    assert mixer.render(100).tolist() == [0.5] * 100


def test_effects_mix_on_top_of_speech_and_clip() -> None:
    mixer = PlaybackMixer(24_000)
    mixer.add_speech(np.full(4, 0.9, np.float32))
    mixer.add_effect(np.full(4, 0.5, np.float32))
    assert mixer.render(4).tolist() == [1.0] * 4


def test_clear_speech_is_instant_and_drops_stale_generation() -> None:
    mixer = PlaybackMixer(24_000)
    gen = mixer.generation
    mixer.add_speech(np.ones(1000, np.float32), gen)
    mixer.render(10)
    mixer.clear_speech()
    assert not mixer.render(100).any()
    # A chunk tagged with the old generation (in flight during stop) is discarded.
    assert mixer.add_speech(np.ones(10, np.float32), gen) is False
    assert not mixer.render(10).any()
    assert mixer.add_speech(np.ones(10, np.float32), mixer.generation) is True


def test_level_meter_follows_output() -> None:
    mixer = PlaybackMixer(24_000)
    mixer.add_speech(np.full(240, 0.5, np.float32))
    mixer.render(240)
    assert 0.8 < mixer.level <= 1.0
    mixer.render(240)
    assert mixer.level == 0.0


def test_fake_playback_consumes_in_real_time() -> None:
    pb = FakePlayback(speed=4.0)
    pb.open()
    try:
        pb.add_speech(np.full(24_000, 0.1, np.float32))  # 1 s of audio at 4x -> ~0.25 s
        wait_until(lambda: pb.speech_frames_played > 0, 1.0)
        wait_until(pb.speech_idle, 3.0)
        assert pb.speech_frames_played == 24_000
    finally:
        pb.close()


# --------------------------------------------------------------------------- capture


def test_capture_buffer_includes_300ms_preroll() -> None:
    buf = CaptureBuffer(16_000, preroll_s=0.3)
    for i in range(50):  # 50 x 10 ms blocks of increasing value
        buf.feed(np.full(160, i, np.float32))
    buf.begin()
    buf.feed(np.full(160, 999, np.float32))
    clip = buf.end()
    assert clip.size == 4800 + 160
    assert clip[0] == 20 and clip[4799] == 49 and clip[-1] == 999


def test_capture_buffer_preroll_shorter_than_available() -> None:
    buf = CaptureBuffer(16_000, preroll_s=0.3)
    buf.feed(np.ones(100, np.float32))
    buf.begin()
    assert buf.end().size == 100


def test_capture_buffer_caps_recording_length() -> None:
    buf = CaptureBuffer(1000, preroll_s=0.0, max_record_s=1.0)
    buf.begin()
    for _ in range(30):
        buf.feed(np.ones(100, np.float32))
    assert buf.full
    assert buf.end().size == 1000
    assert not buf.recording


def test_resample_to_16k() -> None:
    t = np.arange(48_000) / 48_000
    pcm = np.sin(2 * np.pi * 440 * t).astype(np.float32)
    out = to_stt_rate(pcm, 48_000)
    assert out.dtype == np.float32 and abs(out.size - 16_000) <= 2
    assert to_stt_rate(pcm[:16], 16_000) is not None


def test_levels_helpers() -> None:
    assert meter(np.zeros(10, np.float32)) == 0.0
    assert meter(np.ones(10, np.float32)) == 1.0
    assert is_silent(np.full(100, 0.001, np.float32))
    assert not is_silent(np.full(100, 0.1, np.float32))
    assert is_digital_silence(np.zeros(100, np.float32))
    assert not is_digital_silence(np.full(100, 1e-6, np.float32))


def test_chimes_are_short_and_click_free() -> None:
    for make in (chimes.listen_start, chimes.listen_stop, chimes.error):
        tone = make(24_000)
        assert tone.dtype == np.float32 and 0.05 < tone.size / 24_000 < 0.5
        assert abs(tone[0]) < 1e-3 and abs(tone[-1]) < 1e-3
        assert np.max(np.abs(tone)) <= 0.2


# --------------------------------------------------------------------------- errors & devices


class FakePortAudioError(Exception):
    """Mimics sounddevice.PortAudioError's args layout."""


@pytest.mark.parametrize(
    ("args", "code"),
    [
        (("Error opening InputStream: Unanticipated host error", -9999, (13, -2147024891, "")), "mic_blocked"),
        (("Error starting stream: Unanticipated host error", -9999, (13, 0, "E_ACCESSDENIED")), "mic_blocked"),
        (("Error opening InputStream: Unanticipated host error", -9999, (13, -2004287478, "")), "mic_in_use"),
        (("Error opening InputStream: Device unavailable", -9985), "mic_in_use"),
        (("Error opening InputStream: Invalid device", -9996), "no_input_device"),
        (("Error querying device -1",), "no_input_device"),
        (("Something odd", -9986), "no_input_device"),
    ],
)
def test_portaudio_errors_map_to_protocol_codes(args: tuple[object, ...], code: str) -> None:
    err = classify_audio_error(FakePortAudioError(*args), "input")
    assert err.code == code
    assert err.hint  # every mic error tells the user what to do


E_ACCESSDENIED, AUDCLNT_E_DEVICE_IN_USE, E_NOTFOUND = -2147024891, -2004287478, -2147023728


@pytest.mark.parametrize(
    ("args", "host_code", "code"),
    [
        # What sounddevice 0.5.6 raises on Windows: PortAudio 19.7's WASAPI backend turns a failed
        # IAudioClient::Initialize into paInvalidDevice and a failed activation into
        # paInsufficientMemory; the HRESULT is only in Pa_GetLastHostErrorInfo().
        (("Error opening InputStream: Invalid device", -9996), AUDCLNT_E_DEVICE_IN_USE, "mic_in_use"),
        (("Error opening InputStream: Invalid device", -9996), E_ACCESSDENIED, "mic_blocked"),
        (("Error opening InputStream: Insufficient memory", -9992), E_ACCESSDENIED, "mic_blocked"),
        (("Error opening InputStream: Insufficient memory", -9992), E_NOTFOUND, "no_input_device"),
        (("Error opening InputStream: Invalid sample rate", -9997), AUDCLNT_E_DEVICE_IN_USE, "mic_in_use"),
        (("Error opening InputStream: Invalid device", -9996), None, "no_input_device"),
        # Only the codes that hide the HRESULT consult it (the slot may be stale otherwise).
        (("Error opening InputStream: Something odd", -9986), AUDCLNT_E_DEVICE_IN_USE, "no_input_device"),
        # An HRESULT sounddevice did attach wins over the separately read one.
        (("Unanticipated host error", -9999, (13, E_ACCESSDENIED, "")), AUDCLNT_E_DEVICE_IN_USE, "mic_blocked"),
    ],
)
def test_wasapi_hidden_host_errors(args: tuple[object, ...], host_code: int | None, code: str) -> None:
    err = classify_audio_error(FakePortAudioError(*args), "input", host_code=host_code)
    assert err.code == code
    if host_code is not None and code != "no_input_device":
        assert "host error 0x" in err.message  # the HRESULT lands in the log line


def test_mic_blocked_hint_mentions_privacy_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvis_voice import platform as plat
    from jarvis_voice.platform import windows

    monkeypatch.setattr(plat, "current", lambda: windows)
    err = classify_audio_error(FakePortAudioError("x", -9999, (13, -2147024891, "")), "input")
    assert "Privacy & security > Microphone" in (err.hint or "")
    assert "ms-settings:privacy-microphone" in (err.hint or "")
    assert "Let desktop apps access your microphone" in (err.hint or "")


@pytest.mark.parametrize("denied", [True, False])
def test_windows_digital_silence_hint_follows_the_privacy_switch(monkeypatch: pytest.MonkeyPatch, denied: bool) -> None:
    from jarvis_voice import platform as plat
    from jarvis_voice.platform import windows

    monkeypatch.setattr(plat, "current", lambda: windows)
    monkeypatch.setattr(windows, "mic_access_denied", lambda: denied)
    hint = digital_silence_error().hint or ""
    assert ("Privacy & security > Microphone" in hint) is denied
    assert ("switched on and not muted" in hint) is not denied


def test_windows_mic_consent_reads_without_error() -> None:
    from jarvis_voice.platform import windows

    if plat_name() != "windows":
        pytest.skip("reads the Windows registry")
    assert isinstance(windows.mic_access_denied(), bool)


def test_windows_ensure_com_initialises_com_on_the_calling_thread() -> None:
    import ctypes
    import threading

    from jarvis_voice.platform import windows

    if plat_name() != "windows":
        pytest.skip("calls ole32")
    probe: list[int] = []

    def run() -> None:
        windows.ensure_com()
        windows.ensure_com()  # once per thread: does nothing
        ole32 = ctypes.WinDLL("ole32")  # type: ignore[attr-defined]
        ole32.CoInitializeEx.restype = ctypes.c_long
        probe.append(ole32.CoInitializeEx(None, windows.COINIT_MULTITHREADED) & 0xFFFFFFFF)
        ole32.CoUninitialize()  # balances the probe only

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(5.0)
    assert probe == [windows.S_FALSE]  # COM was already up on that thread, multithreaded


@pytest.mark.parametrize(
    ("hresult", "ready"),
    [(0x0, True), (0x1, True), (0x80010106, True), (0x80070057, False)],
    ids=["S_OK", "S_FALSE", "RPC_E_CHANGED_MODE", "E_INVALIDARG"],
)
def test_windows_ensure_com_is_done_only_once_com_is_up(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, hresult: int, ready: bool
) -> None:
    import ctypes
    import threading
    import types

    from jarvis_voice.platform import windows

    calls: list[str] = []

    def co_initialize_ex(reserved: object, coinit: int) -> int:
        calls.append(threading.current_thread().name)
        return hresult - (1 << 32) if hresult & 0x80000000 else hresult  # signed, as its c_long restype gives it

    ole32 = types.SimpleNamespace(CoInitializeEx=co_initialize_ex)
    fake_ctypes = types.SimpleNamespace(
        WinDLL=lambda name: ole32, c_void_p=ctypes.c_void_p, c_ulong=ctypes.c_ulong, c_long=ctypes.c_long
    )
    monkeypatch.setattr(windows, "ctypes", fake_ctypes)
    thread = threading.Thread(target=lambda: (windows.ensure_com(), windows.ensure_com()), name="com-probe")
    with caplog.at_level("WARNING", logger="jarvis_voice.platform.windows"):
        thread.start()
        thread.join(5.0)
    assert calls.count("com-probe") == (1 if ready else 2)  # a failure is tried again on the next call
    assert len([r for r in caplog.records if "com-probe" in r.getMessage()]) == (0 if ready else 1)  # logged once


def test_a_zero_host_error_code_is_left_out() -> None:
    # What a start on a thread without COM raised on Windows: the WDM-KS text is stale, its code 0.
    exc = FakePortAudioError("Error starting stream: Unanticipated host error", -9999, (11, 0, "WdmSyncIoctl: ..."))
    err = classify_audio_error(exc, "output")
    assert err.code == "no_output_device" and "host error 0x" not in err.message


def test_output_errors_and_passthrough() -> None:
    assert classify_audio_error(FakePortAudioError("Invalid device", -9996), "output").code == "no_output_device"
    original = AudioError("mic_in_use", "busy")
    assert classify_audio_error(original, "input") is original


DEVICES = [
    DeviceInfo(0, "Microsoft Sound Mapper - Input", "MME", 2, 0, 44100),
    DeviceInfo(1, "Headset Microphone (PRO X Wireless Gaming Headset)", "MME", 1, 0, 44100),
    DeviceInfo(5, "Speakers (Realtek(R) Audio)", "Windows WASAPI", 0, 2, 48000),
    DeviceInfo(6, "Headset Earphone (PRO X Wireless Gaming Headset)", "Windows WASAPI", 0, 2, 48000),
    DeviceInfo(7, "Headset Microphone (PRO X Wireless Gaming Headset)", "Windows WASAPI", 1, 0, 48000),
    DeviceInfo(8, "Microphone (Webcam)", "Windows WASAPI", 2, 0, 48000),
]


def test_select_by_name_within_preferred_hostapi() -> None:
    dev = select_device(DEVICES, "input", wanted="pro x", hostapi="Windows WASAPI", default_index=8)
    assert dev is not None and dev.index == 7
    dev = select_device(
        DEVICES,
        "output",
        wanted="Headset Earphone (PRO X Wireless Gaming Headset)",
        hostapi="Windows WASAPI",
        default_index=5,
    )
    assert dev is not None and dev.index == 6


def test_select_falls_back_to_hostapi_default() -> None:
    dev = select_device(DEVICES, "input", wanted="Blue Yeti", hostapi="Windows WASAPI", default_index=8)
    assert dev is not None and dev.index == 8
    dev = select_device(DEVICES, "output", wanted=None, hostapi="Windows WASAPI", default_index=5)
    assert dev is not None and dev.index == 5


def test_select_never_leaves_the_preferred_hostapi_while_it_has_a_device() -> None:
    # A default index that points outside WASAPI (stale) and a name only another host API has.
    dev = select_device(DEVICES, "input", wanted="Sound Mapper", hostapi="Windows WASAPI", default_index=1)
    assert dev is not None and (dev.index, dev.hostapi) == (7, "Windows WASAPI")


def test_select_without_preferred_hostapi_or_devices() -> None:
    dev = select_device(DEVICES, "input", wanted=None, hostapi="Core Audio", default_index=1)
    assert dev is not None and dev.index == 1  # host API missing: any input, default first
    assert select_device([], "input", wanted=None, hostapi=None, default_index=None) is None
    assert select_device(DEVICES[:2], "output", wanted=None, hostapi=None, default_index=None) is None
