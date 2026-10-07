from __future__ import annotations

import socket
import threading
import time

import numpy as np
import pytest

from jarvis_voice.tts.base import Pcm16Decoder, SynthError
from jarvis_voice.tts.fish import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    FishLiveSynth,
    FishSettings,
    live_url,
    probe,
    start_request,
)

from conftest import wait_until
from fish_fake import FakeFishServer


class Collector:
    def __init__(self) -> None:
        self.chunks: list[bytes] = []
        self.lock = threading.Lock()

    def __call__(self, chunk: bytes) -> None:
        with self.lock:
            self.chunks.append(chunk)

    @property
    def total(self) -> int:
        with self.lock:
            return sum(len(c) for c in self.chunks)


def settings(url: str, key: str | None = "good-key", **kw: object) -> FishSettings:
    return FishSettings(api_key=key, base_url=url, **kw)  # type: ignore[arg-type]


def test_settings_from_env() -> None:
    s = FishSettings.from_env({})
    assert (s.api_key, s.base_url, s.model, s.sample_rate) == (None, DEFAULT_BASE_URL, DEFAULT_MODEL, 24_000)
    assert DEFAULT_MODEL == "s2.1-pro"
    s = FishSettings.from_env(
        {"FISH_AUDIO_API_KEY": " k ", "JARVIS_TTS_MODEL": "s1", "JARVIS_FISH_BASE_URL": "https://x.test"}
    )
    assert (s.api_key, s.model, s.base_url) == ("k", "s1", "https://x.test")
    s = FishSettings.from_env({}, fake_url="ws://127.0.0.1:9")
    assert s.base_url == "ws://127.0.0.1:9" and s.api_key == "fake-key"


@pytest.mark.parametrize(
    ("base", "url"),
    [
        ("wss://api.fish.audio", "wss://api.fish.audio/v1/tts/live"),
        ("https://api.fish.audio/", "wss://api.fish.audio/v1/tts/live"),
        ("http://127.0.0.1:8080", "ws://127.0.0.1:8080/v1/tts/live"),
        ("ws://127.0.0.1:8080/v1/tts/live", "ws://127.0.0.1:8080/v1/tts/live"),
    ],
)
def test_live_url(base: str, url: str) -> None:
    assert live_url(base) == url


def test_live_url_rejects_other_schemes() -> None:
    with pytest.raises(ValueError):
        live_url("ftp://x")


def test_start_request_shape() -> None:
    req = start_request(settings("wss://x"), "voice-123")
    assert req["event"] == "start"
    r = req["request"]
    assert r["text"] == "" and r["format"] == "pcm" and r["sample_rate"] == 24_000
    assert r["reference_id"] == "voice-123" and r["latency"] == "balanced"
    assert 100 <= r["chunk_length"] <= 300
    assert r["prosody"] == {"speed": 1.0, "volume": 0}
    assert "reference_id" not in start_request(settings("wss://x"), None)["request"]


def test_streams_sentences_and_receives_pcm() -> None:
    with FakeFishServer() as fish:
        collector = Collector()
        synth = FishLiveSynth(settings(fish.url))
        stream = synth.open_stream(collector, voice_id="v1")
        stream.send_text("Good evening, sir.")
        stream.send_text("All systems nominal.  ")
        wait_until(lambda: collector.total >= len(fish.pcm_for("Good evening, sir. ", 24_000)))
        stream.finish()
        wait_until(lambda: stream.done and stream.closed)
        session = fish.sessions[0]
        assert session.events() == ["start", "text", "flush", "text", "flush", "stop"]
        assert session.texts() == ["Good evening, sir. ", "All systems nominal. "]
        start = session.start
        assert start is not None and start["request"]["reference_id"] == "v1"
        assert start["request"]["format"] == "pcm" and start["request"]["sample_rate"] == 24_000
        assert session.headers["Authorization"] == "Bearer good-key"
        assert session.headers["model"] == "s2.1-pro"
        assert collector.total == session.audio_bytes > 0
        assert stream.error is None


def test_model_header_override() -> None:
    with FakeFishServer() as fish:
        stream = FishLiveSynth(settings(fish.url, model="s1")).open_stream(Collector(), voice_id=None)
        stream.finish()
        wait_until(lambda: stream.closed)
        assert fish.sessions[0].headers["model"] == "s1"
        assert "reference_id" not in fish.sessions[0].start["request"]  # type: ignore[index]


def test_bad_key_maps_to_fish_auth_failed() -> None:
    with FakeFishServer(api_key="good-key") as fish:
        with pytest.raises(SynthError) as info:
            FishLiveSynth(settings(fish.url, key="bad-key")).open_stream(Collector(), voice_id=None)
        assert info.value.code == "fish_auth_failed" and "401" in info.value.message
        assert info.value.hint
        assert fish.rejected == 1


def test_no_credit_is_reported_as_billing_not_a_bad_key() -> None:
    with FakeFishServer(no_credit=True) as fish:
        with pytest.raises(SynthError) as info:
            FishLiveSynth(settings(fish.url)).open_stream(Collector(), voice_id=None)
        assert info.value.code == "fish_auth_failed" and "402" in info.value.message
        assert "credit" in (info.value.hint or "")
        assert probe(settings(fish.url))["status"] == "no_credit"


def test_missing_key_maps_to_fish_key_missing() -> None:
    with pytest.raises(SynthError) as info:
        FishLiveSynth(settings("ws://127.0.0.1:9", key=None)).open_stream(Collector(), voice_id=None)
    assert info.value.code == "fish_key_missing"


def test_unreachable_server_maps_to_fish_unreachable() -> None:
    with socket.socket() as s:  # grab a free port and leave it closed
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(SynthError) as info:
        FishLiveSynth(settings(f"ws://127.0.0.1:{port}", connect_timeout=2)).open_stream(Collector(), voice_id=None)
    assert info.value.code == "fish_unreachable"


def test_finish_with_error_reason_sets_error() -> None:
    with FakeFishServer(mode="finish_error") as fish:
        stream = FishLiveSynth(settings(fish.url)).open_stream(Collector(), voice_id=None)
        stream.send_text("Hello.")
        stream.finish()
        wait_until(lambda: stream.closed)
        assert stream.error is not None and stream.error.code == "fish_unreachable"
        assert not stream.done


def test_abnormal_close_mid_stream_sets_error() -> None:
    with FakeFishServer(mode="drop") as fish:
        stream = FishLiveSynth(settings(fish.url)).open_stream(Collector(), voice_id=None)
        stream.send_text("One.")
        stream.send_text("Two.")
        wait_until(lambda: stream.closed)
        assert stream.error is not None and stream.error.code == "fish_unreachable"


def test_cancel_is_immediate_and_stops_callbacks() -> None:
    with FakeFishServer(chunk_delay=0.02, ms_per_char=80) as fish:
        collector = Collector()
        stream = FishLiveSynth(settings(fish.url)).open_stream(collector, voice_id=None)
        stream.send_text("A fairly long sentence that produces plenty of audio frames.")
        wait_until(lambda: collector.total > 0)
        t0 = time.monotonic()
        stream.cancel()
        assert time.monotonic() - t0 < 0.5  # never waits for the closing handshake
        assert stream.closed and stream.error is None
        time.sleep(0.15)
        settled = collector.total
        time.sleep(0.2)
        assert collector.total == settled


def test_send_after_close_raises() -> None:
    with FakeFishServer() as fish:
        stream = FishLiveSynth(settings(fish.url)).open_stream(Collector(), voice_id=None)
        stream.cancel()
        with pytest.raises(SynthError):
            stream.send_text("too late")


def test_pcm_decoder_reassembles_odd_chunks() -> None:
    samples = (np.arange(-500, 500) * 30).astype("<i2")
    raw = samples.tobytes()
    decoder = Pcm16Decoder()
    parts = [decoder.feed(raw[i : i + 7]) for i in range(0, len(raw), 7)]
    out = np.concatenate(parts)
    np.testing.assert_allclose(out, samples.astype(np.float32) / 32768.0)


def test_odd_sized_audio_frames_end_to_end() -> None:
    with FakeFishServer(odd_chunks=True) as fish:
        decoder = Pcm16Decoder()
        decoded: list[np.ndarray] = []
        stream = FishLiveSynth(settings(fish.url)).open_stream(lambda c: decoded.append(decoder.feed(c)), voice_id=None)
        stream.send_text("Testing odd frames.")
        stream.finish()
        wait_until(lambda: stream.done)
        total = sum(d.size for d in decoded)
        assert total == len(fish.pcm_for("Testing odd frames. ", 24_000)) // 2


def test_probe_reports_ok_auth_and_unreachable() -> None:
    with FakeFishServer() as fish:
        assert probe(settings(fish.url))["status"] == "ok"
        assert probe(settings(fish.url, key="nope"))["status"] == "auth_failed"
        result = probe(settings(fish.url, key=None))
        assert result["status"] == "key_missing" and result["reachable"]
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert probe(settings(f"ws://127.0.0.1:{port}"), timeout=2)["status"] == "unreachable"
