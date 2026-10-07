"""Echo cancelling: WebRTC's AEC3 (through livekit) on synthetic echoes, and the frame handling around it.

The tests marked ``livekit`` run the real native module; the rest use a fake
one, since a frame of the wrong size would kill the test process natively.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest
from test_listener import read_wav

from jarvis_voice.audio import echo as echo_module
from jarvis_voice.audio.echo import (
    FAR_QUEUE_BLOCKS,
    FRAME,
    RATE,
    WebRtcEchoCanceller,
    delay_ms,
    level_db,
    self_test,
    synthetic_echo,
    synthetic_speech,
)
from jarvis_voice.doctor import _aec_section

DATA = Path(__file__).parent / "data"


@pytest.fixture
def livekit() -> None:
    pytest.importorskip("livekit.rtc")


def resample(audio: np.ndarray, rate: int, to: int = RATE) -> np.ndarray:
    """The whole signal at once: no streaming bursts, as sound travelling through a room has none."""
    import soxr

    return np.asarray(soxr.resample(audio, rate, to), np.float32)


def cancel(
    far: np.ndarray,
    far_rate: int,
    mic: np.ndarray,
    *,
    burst: int = 1,
    latency: float = 0.0,
    output_latency: float = 0.0,
    block_s: float = 0.01,
    lag_s: float = 0.0,
    gap: tuple[float, float] | None = None,
    jitter_s: float = 0.0,
    pairs: bool = False,
    stop: tuple[float, float, float] | None = None,
    seed: int = 0,
) -> np.ndarray:
    """Play ``far`` in ``block_s`` blocks and return ``mic`` (16 kHz) through a real canceller, in 10 ms blocks.

    The sound card's two threads, in the order of their timestamps: the speaker block that plays at t is
    rendered ``output_latency`` before t (``burst`` 10 ms of them at a time, at the first one's time, as a
    sound card with a big buffer does); the microphone block heard at t is stamped t + 1 ms, up to
    ``jitter_s`` later (in order),
    or with ``pairs`` two blocks at a time, stamped with the second. The listener handles each ``lag_s``
    after its stamp, and microphone blocks inside ``gap`` (seconds) never arrive. With ``stop`` (start, end,
    shift) the output stops at ``start`` (and says so), renders nothing until ``end``, then renders ``shift``
    seconds earlier than before: a restarted stream in a new phase. Samples it never got back stay zero.
    """
    rng = np.random.default_rng(seed)
    events: list[tuple[float, int, int, np.ndarray | None]] = []  # (time, 0 speaker / 1 microphone, index, block)
    step = round(far_rate * block_s)
    for j in range(-(-far.size // step)):
        at = j * block_s - output_latency
        if burst > 1:
            at = int(j * block_s * 100 + 1e-9) // burst * burst / 100 - output_latency  # at the group's first block
        if stop is not None and stop[0] <= j * block_s < stop[1]:
            continue
        if stop is not None and j * block_s >= stop[1]:
            at -= stop[2]
        events.append((at, 0, j, far[j * step : (j + 1) * step]))
    if stop is not None:
        events.append((stop[0], 0, -1, None))
    stamp = 0.0
    for m in range(mic.size // FRAME):
        heard = m + 1 if pairs and m % 2 == 0 else m  # a pair arrives with its second block
        stamp = max(stamp, heard / 100 + 0.001 + rng.uniform(0, jitter_s))
        if gap is None or not gap[0] <= m / 100 < gap[1]:
            events.append((stamp + lag_s, 1, m, None))
    events.sort(key=lambda event: event[:3])
    now = [0.0]
    canceller = WebRtcEchoCanceller(lambda: latency, far_rate=far_rate, clock=lambda: now[0])
    cleaned = np.zeros(mic.size, np.float32)
    try:
        for at, kind, index, block in events:
            if kind == 0:
                now[0] = at
                canceller.far(block, far_rate)
            else:
                out = canceller.near(mic[index * FRAME : (index + 1) * FRAME], at - lag_s)
                cleaned[index * FRAME : index * FRAME + out.size] = out
    finally:
        canceller.close()
    return cleaned


def attenuation(mic: np.ndarray, cleaned: np.ndarray, start: float, end: float) -> float:
    window = slice(int(start * RATE), int(end * RATE))
    return level_db(mic[window]) - level_db(cleaned[window])


# -- the real canceller --------------------------------------------------------------------------


@pytest.mark.parametrize("delay", [0.015, 0.03, 0.06])
@pytest.mark.parametrize("far_rate", [24_000, 44_100, 48_000])
def test_a_known_echo_is_cancelled(livekit: None, far_rate: int, delay: float) -> None:
    # Jarvis's voice at the output's rate; the microphone hears it ``delay`` later at half volume, with a
    # room tail. Short delays are a close speaker: a reference that reaches the canceller after its own echo
    # (a streaming resampler's bursts) holds for a few seconds and then falls apart, hence the late window.
    far = synthetic_speech(10.0, seed=1, rate=far_rate)
    mic = synthetic_echo(resample(far, far_rate), delay_s=delay, gain=0.5)
    cleaned = cancel(far, far_rate, mic, latency=delay)
    early, late = attenuation(mic, cleaned, 2, 6), attenuation(mic, cleaned, 6, 10)
    print(f"known echo, {far_rate} Hz, {delay * 1000:.0f} ms: {early:.1f} dB quieter at 2-6 s, {late:.1f} dB at 6-10 s")
    assert early >= 20 and late >= 20


@pytest.mark.parametrize("burst", [10, 12, 16])
def test_a_far_end_delivered_in_groups_is_cancelled_too(livekit: None, burst: int) -> None:
    # A sound card with a big buffer calls back ``burst`` times 10 ms at once. Guessing a closed output from
    # 100 ms without a callback put silence between the groups, and AEC3 fell to 7-25 dB for good.
    far = synthetic_speech(10.0, seed=2, rate=48_000)
    mic = synthetic_echo(resample(far, 48_000), delay_s=0.06, gain=0.5)
    cleaned = cancel(far, 48_000, mic, burst=burst)
    early, late = attenuation(mic, cleaned, 2, 6), attenuation(mic, cleaned, 6, 10)
    print(f"far end in groups of {burst} x 10 ms: {early:.1f} / {late:.1f} dB quieter")
    assert early >= 30 and late >= 30


GAP_DELIVERY = {  # how the sound card delivers, which the far audio dropped after a gap must not depend on
    # Speaker blocks rendered 18.5 ms ahead and microphone stamps 1 ms late: jitter of 0.5 ms or more moves
    # which speaker block was rendered before a microphone block, as on a real sound card.
    "steady": {},
    "jitter 2 ms": {"jitter_s": 0.002, "seed": 1},
    "jitter 2 ms, another seed": {"jitter_s": 0.002, "seed": 3},
    "jitter 5 ms": {"jitter_s": 0.005, "seed": 2},
    "paired capture": {"pairs": True},
    "2.5 ms output blocks": {"block_s": 0.0025},
}


@pytest.mark.parametrize("delivery", list(GAP_DELIVERY))
@pytest.mark.parametrize("gap", [0.3, 1.0])
def test_a_gap_in_the_microphone_while_jarvis_speaks_is_cancelled_straight_after(
    livekit: None, gap: float, delivery: str
) -> None:
    # The microphone stalls (or the listener drops blocks) while the speaker plays on. Rebuilding the pairing
    # from callback stamps was a frame off with any jitter or paired capture (4-14 dB here); a new AEC3, given
    # the half second before the block as history, is not.
    far = synthetic_speech(10.0, seed=1, rate=24_000)
    mic = synthetic_echo(resample(far, 24_000), delay_s=0.04, gain=0.5)
    delivery_kw = GAP_DELIVERY[delivery]
    cleaned = cancel(far, 24_000, mic, latency=0.06, output_latency=0.0185, gap=(4.0, 4.0 + gap), **delivery_kw)
    end = 4.0 + gap
    first, second = attenuation(mic, cleaned, end, end + 0.25), attenuation(mic, cleaned, end, end + 1)
    print(f"{gap} s microphone gap, {delivery}: {first:.1f} dB quieter in the first 0.25 s, {second:.1f} in the second")
    assert first >= 40 and second >= 40


@pytest.mark.parametrize(
    "gap",
    [
        pytest.param(
            0.1,
            marks=pytest.mark.xfail(strict=True, reason="under 0.2 s, not told from a late callback: ~2 s to recover"),
        ),
        3.0,
    ],
)
def test_a_short_or_long_gap_in_the_microphone(livekit: None, gap: float) -> None:
    far = synthetic_speech(10.0, seed=1, rate=24_000)
    mic = synthetic_echo(resample(far, 24_000), delay_s=0.06, gain=0.5)
    cleaned = cancel(far, 24_000, mic, latency=0.06, gap=(4.0, 4.0 + gap))
    after = attenuation(mic, cleaned, 4 + gap, 5 + gap)
    print(f"{gap} s microphone gap: {after:.1f} dB quieter in the second after")
    assert after >= 20


def reply_after_a_stop(far_rate: int, stop: float, quiet: float) -> np.ndarray:
    """A reply that stops at ``stop`` s, then ``quiet`` s of nothing, then the next reply (to 2 s past it)."""
    far = synthetic_speech(stop + quiet + 2.0, seed=1, rate=far_rate)
    far[int(stop * far_rate) : int((stop + quiet) * far_rate)] = 0.0
    return far


@pytest.mark.parametrize("delay", [0.02, 0.06])
@pytest.mark.parametrize("closed", [2.0, 30.0])
def test_the_next_reply_after_the_output_was_closed_is_cancelled_from_its_start(
    livekit: None, closed: float, delay: float
) -> None:
    # The output closes when idle and reopens for the next reply in a new phase. An AEC3 that kept its old
    # filter took a second or more at 12-29 dB to find the echo again: a typed prompt answered aloud started
    # every reply that way.
    far = reply_after_a_stop(24_000, 4.0, closed)
    mic = synthetic_echo(resample(far, 24_000), delay_s=delay, gain=0.5)
    cleaned = cancel(far, 24_000, mic, latency=delay, stop=(4.0, 4.0 + closed, 0.007))
    start = 4.0 + closed
    first, second = attenuation(mic, cleaned, start, start + 0.25), attenuation(mic, cleaned, start, start + 1)
    print(f"closed {closed:.0f} s, {delay * 1000:.0f} ms: next reply {first:.1f} / {second:.1f} dB quieter")
    assert first >= 30 and second >= 30


@pytest.mark.parametrize("far_rate", [24_000, 48_000])
@pytest.mark.parametrize("restart", [0.02, 0.05, 0.09, 0.15])
def test_the_next_reply_after_a_stop_is_cancelled_from_its_start(livekit: None, restart: float, far_rate: int) -> None:
    # Stopping Jarvis aborts and restarts the output stream (``restart`` s without a callback); it then plays
    # silence until the next reply, two seconds later. Left to AEC3's old filter, that reply began at 9-25 dB.
    # The old filter still cancels the echo of the words that played last: a new AEC3 at once let it through.
    far = reply_after_a_stop(far_rate, 4.0, 2.0)
    mic = synthetic_echo(resample(far, far_rate), delay_s=0.06, gain=0.5)
    cleaned = cancel(far, far_rate, mic, latency=0.06, stop=(4.0, 4.0 + restart, 0.0))
    tail = attenuation(mic, cleaned, 3.9, 4.2)
    first, second = attenuation(mic, cleaned, 6.0, 6.25), attenuation(mic, cleaned, 6.0, 7.0)
    print(f"restart {restart * 1000:.0f} ms, {far_rate} Hz: {tail:.1f} dB at the stop, {first:.1f}/{second:.1f} after")
    assert tail >= 30 and first >= 30 and second >= 30


def test_the_first_reply_is_cancelled_from_its_start(livekit: None) -> None:
    # Silence goes in at the output's rate from the start: the first reply does not change the far end's rate,
    # which would start AEC3 over just as the reply begins (34-38 dB for its first seconds).
    far = synthetic_speech(6.0, seed=1, rate=24_000)
    far[: 2 * 24_000] = 0.0
    mic = synthetic_echo(resample(far, 24_000), delay_s=0.03, gain=0.5)
    cleaned = cancel(far, 24_000, mic, latency=0.03, stop=(0.0, 2.0, 0.0))  # the output opens with the reply
    first = attenuation(mic, cleaned, 2.0, 3.0)
    print(f"first reply: {first:.1f} dB quieter in its first second")
    assert first >= 50


def test_a_listener_far_behind_short_output_blocks_still_cancels(livekit: None) -> None:
    # 2.5 ms output blocks (CoreAudio, low-latency WASAPI) and a listener 1.5 s behind: 600 blocks queued.
    # A queue capped at 400 blocks dropped the very blocks the waiting microphone audio needed.
    far = synthetic_speech(10.0, seed=1, rate=24_000)
    mic = synthetic_echo(resample(far, 24_000), delay_s=0.03, gain=0.5)
    cleaned = cancel(far, 24_000, mic, latency=0.03, block_s=0.0025, lag_s=1.5)
    early, late = attenuation(mic, cleaned, 2, 6), attenuation(mic, cleaned, 6, 8.5)
    print(f"listener 1.5 s behind 2.5 ms blocks: {early:.1f} / {late:.1f} dB quieter")
    assert early >= 20 and late >= 20
    assert FAR_QUEUE_BLOCKS * 0.0025 >= 10  # the cap is only a safety net


def test_with_nothing_playing_the_microphone_passes_through(livekit: None) -> None:
    voice = synthetic_speech(4.0, seed=4)
    cleaned = cancel(np.zeros(0, np.float32), 24_000, voice)  # the output is closed: silence stands in
    change = attenuation(voice, cleaned, 1, 4)
    print(f"nothing playing: {change:.2f} dB quieter")
    assert abs(change) < 3
    zeros = np.zeros(2 * RATE, np.float32)
    assert not np.any(cancel(np.zeros(0, np.float32), 24_000, zeros))  # exact zeros stay exact zeros


def test_the_users_voice_survives_talking_over_jarvis(livekit: None) -> None:
    jarvis = np.tile(read_wav(DATA / "hello-there.wav"), 4)
    user = read_wav(DATA / "hey-jarvis-weather.wav")
    echo = synthetic_echo(jarvis, delay_s=0.06, gain=0.15)
    mic = echo.copy()
    at = jarvis.size - user.size - RATE // 2  # the user talks over the last few seconds of the reply
    mic[at : at + user.size] += user
    cleaned = cancel(resample(jarvis, RATE, 24_000), 24_000, mic)
    during = slice(at, at + user.size)
    loss = level_db(user) - level_db(cleaned[during])
    print(f"double talk: echo {level_db(echo[during]) - level_db(user):.1f} dB under the user, who lost {loss:.2f} dB")
    assert loss < 3
    assert level_db(echo[: at - RATE]) - level_db(cleaned[RATE : at - RATE]) >= 20  # while Jarvis spoke alone


def test_odd_blocks_and_rate_changes_reach_the_native_code_only_as_whole_frames(livekit: None) -> None:
    # In a child process: a frame of the wrong size would kill it outright, not raise.
    script = (
        "import numpy as np\n"
        "from jarvis_voice.audio.echo import WebRtcEchoCanceller\n"
        "now = [0.0]\n"
        "c = WebRtcEchoCanceller(clock=lambda: now[0])\n"
        "rng = np.random.default_rng(0)\n"
        "t = 0.0\n"
        "for rate in (24000, 48000, 44100, 22050, 16000, 96000, 7000, 24000):\n"
        "    for k in range(60):\n"
        "        now[0] = t\n"
        "        c.far((rng.standard_normal(int(rng.integers(1, 700))) * 0.1).astype(np.float32), rate)\n"
        "        c.near((rng.standard_normal(int(rng.integers(0, 400))) * 0.1).astype(np.float32), t + 0.001)\n"
        "        if k % 23 == 22:\n"
        "            c.far(None, rate)  # the output stops: silence, then a new module\n"
        "        t += 0.01\n"
        "assert not c._failed\n"
        "print(sorted(c._rev))\n"
        "c.close()\n"
    )
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    # 22.05 kHz has no whole 10 ms frame, and 7 kHz is below what WebRTC takes: both go in resampled to 16 kHz.
    assert proc.stdout.strip() == "[16000, 24000, 44100, 48000, 96000]"


def test_self_test_checks_the_live_path(livekit: None, monkeypatch: pytest.MonkeyPatch) -> None:
    blocks: list[tuple[int, int]] = []
    far = WebRtcEchoCanceller.far

    def spy(self: WebRtcEchoCanceller, block: np.ndarray, rate: int) -> None:
        blocks.append((block.size, rate))
        far(self, block, rate)

    monkeypatch.setattr(WebRtcEchoCanceller, "far", spy)
    report = self_test()
    print(f"self-test: {report}")
    assert set(blocks) == {(240, 24_000)} and len(blocks) >= 500  # the output's rate and blocks, for 5 s or more
    assert report["attenuationDb"] >= 20 and 0 < report["usPerFrame"] < 5000


def test_doctor_reports_echo_cancelling(livekit: None) -> None:
    section = _aec_section({})
    assert section["ok"] is True and section["enabled"] is True and section["version"]
    assert _aec_section({"JARVIS_AEC": "off"})["enabled"] is False


def test_doctor_explains_a_missing_or_blocked_livekit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "livekit.rtc.version", None)  # import fails, as when the DLL is blocked
    section = _aec_section({})
    assert section["ok"] is False and "livekit.rtc.version" in section["error"] and "/jarvis setup" in section["hint"]


def test_a_closed_canceller_leaves_no_noise_at_exit(livekit: None) -> None:
    # Left for interpreter exit, livekit's handle would print an AssertionError on stderr.
    script = (
        "import numpy as np\n"
        "from jarvis_voice.audio.echo import WebRtcEchoCanceller\n"
        "now = [0.0]\n"
        "c = WebRtcEchoCanceller(clock=lambda: now[0])\n"
        "for k in range(100):\n"
        "    now[0] = k / 100\n"
        "    c.far(np.full(240, 0.1 if k < 30 else 0.0, np.float32), 24000)\n"
        "    if k == 30:\n"
        "        c.far(None, 24000)  # the output stops: a new module replaces the first one\n"
        "    c.near(np.full(160, 0.1, np.float32), k / 100 + 0.001)\n"
        "c.close()\n"
    )
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "AssertionError" not in proc.stderr and "Exception ignored" not in proc.stderr


# -- frames, delay hints and failures, against a fake module -----------------------------------


class FakeFrame:
    """livekit's AudioFrame as the canceller uses it: mono int16 samples behind ``data``."""

    num_channels = 1

    def __init__(self, rate: int = RATE, samples: int | None = None) -> None:
        self.sample_rate = rate
        self._data = bytearray(2 * (rate // 100 if samples is None else samples))

    @property
    def data(self) -> memoryview:
        return memoryview(self._data).cast("B").cast("h")


class FakeApm:
    """Records every call the native module would get."""

    def __init__(
        self,
        *,
        refuse_delay: bool = False,
        fail_on: int | None = None,
        refuse_rates: frozenset[int] = frozenset(),
        swap_buffer: bool = False,
    ) -> None:
        self.calls: list[tuple[str, int, int, bool]] = []  # (stream, samples, rate, silent)
        self.delays: list[int] = []
        self.refuse_delay = refuse_delay
        self.fail_on = fail_on  # the microphone frame (0-based) that raises
        self.refuse_rates = refuse_rates  # far-end rates that raise, the way livekit refuses 7 kHz
        self.swap_buffer = swap_buffer  # replace the frame's buffer, the way livekit does with a read-only one

    def _record(self, stream: str, frame: FakeFrame) -> None:
        pcm = np.frombuffer(frame.data, np.int16)
        self.calls.append((stream, pcm.size, frame.sample_rate, not pcm.any()))

    def process_reverse_stream(self, frame: FakeFrame) -> None:
        if frame.sample_rate in self.refuse_rates:
            raise RuntimeError("an RtcError occurred: Internal - Failed to process reverse stream")
        self._record("far", frame)

    def process_stream(self, frame: FakeFrame) -> None:
        if self.fail_on is not None and self.near_calls() == self.fail_on:
            raise RuntimeError("apm_process_stream failed")
        self._record("near", frame)
        if self.swap_buffer:
            frame._data = bytearray(frame._data)
        pcm = np.frombuffer(frame.data, np.int16)
        pcm //= 2  # "cancels" half of everything, so processed frames are recognisable

    def set_stream_delay_ms(self, ms: int) -> None:
        self.delays.append(ms)
        if self.refuse_delay:
            raise RuntimeError("delay out of range")

    def near_calls(self) -> int:
        return self.streams().count("near")

    def streams(self) -> list[str]:
        return [stream for stream, *_ in self.calls]


def fake_canceller(apm: FakeApm, latency: Callable[[], float] = lambda: 0.0) -> tuple[WebRtcEchoCanceller, list[float]]:
    now = [0.0]
    return WebRtcEchoCanceller(latency, clock=lambda: now[0], new_apm=lambda: apm, new_frame=FakeFrame), now


def renewing_canceller(latency: float = 0.0) -> tuple[WebRtcEchoCanceller, list[float], list[FakeApm]]:
    """A canceller whose every new AEC3 is a new fake module, recorded in order."""
    now = [0.0]
    made: list[FakeApm] = []

    def new_apm() -> FakeApm:
        made.append(FakeApm())
        return made[-1]

    return WebRtcEchoCanceller(lambda: latency, clock=lambda: now[0], new_apm=new_apm, new_frame=FakeFrame), now, made


def noise(size: int, seed: int = 0) -> np.ndarray:
    return (np.random.default_rng(seed).standard_normal(size) * 0.1).astype(np.float32)


def test_every_native_call_gets_exactly_ten_milliseconds() -> None:
    apm = FakeApm()
    canceller, now = fake_canceller(apm)
    fed = returned = 0
    # Odd microphone blocks, and speaker blocks at the rates and sizes sound cards use, switching mid-stream
    # with samples left over. 22.05 kHz has no whole 10 ms frame: it goes in resampled to 16 kHz.
    for mic_size, far_rate, far_size in (
        (37, 48_000, 480),
        (100, 24_000, 100),
        (480, 44_100, 441),
        (160, 16_000, 37),
        (53, 22_050, 1000),
        (160, 24_000, 61),
    ):
        for k in range(40):
            now[0] += 0.01
            canceller.far(noise(far_size, k), far_rate)
            returned += canceller.near(noise(mic_size, k), now[0] + 0.001).size
            fed += mic_size
    assert {(samples, rate) for stream, samples, rate, _ in apm.calls if stream == "near"} == {(FRAME, RATE)}
    far = {(samples, rate) for stream, samples, rate, _ in apm.calls if stream == "far"}
    assert far == {(480, 48_000), (240, 24_000), (441, 44_100), (160, 16_000)}
    assert returned == apm.near_calls() * FRAME and fed - FRAME < returned <= fed  # whole frames; the rest waits
    assert not canceller._failed


@pytest.mark.parametrize(
    ("rate", "buffer", "size"),
    [
        (RATE, FRAME, 1),
        (RATE, FRAME, 100),
        (RATE, FRAME, 161),
        (RATE, FRAME, 320),
        (RATE, FRAME, 240),  # a 24 kHz frame's worth, into the 16 kHz frame
        (24_000, 240, 160),
        (24_000, 160, 160),  # a buffer that disagrees with its frame's rate
        (44_100, 441, 440),
    ],
)
def test_a_frame_of_any_other_size_never_reaches_the_native_code(rate: int, buffer: int, size: int) -> None:
    apm = FakeApm()
    canceller, _ = fake_canceller(apm)
    with pytest.raises(ValueError, match=f"{rate // 100} samples at {rate} Hz"):
        canceller._call(apm.process_reverse_stream, FakeFrame(rate, buffer), np.zeros(size, np.float32))
    assert apm.calls == []


def test_processed_audio_comes_back_in_order() -> None:
    apm = FakeApm()
    canceller, _ = fake_canceller(apm)
    audio = noise(5 * FRAME)
    cleaned = np.concatenate([canceller.near(audio[i : i + 100], 0.0) for i in range(0, audio.size, 100)])
    assert cleaned.size == audio.size
    np.testing.assert_allclose(cleaned, audio / 2, atol=2 / 32768)


def test_a_frame_whose_buffer_livekit_replaces_is_still_read_back() -> None:
    # livekit swaps in a fresh buffer for a read-only one; a view kept from before would miss the result.
    apm = FakeApm(swap_buffer=True)
    canceller, _ = fake_canceller(apm)
    audio = noise(3 * FRAME)
    np.testing.assert_allclose(canceller.near(audio, 0.0), audio / 2, atol=2 / 32768)


def test_far_blocks_go_in_up_to_the_microphone_block_and_no_further() -> None:
    apm = FakeApm()
    canceller, now = fake_canceller(apm)
    now[0] = 0.495
    canceller.far(noise(240, 49), 24_000)
    canceller.near(noise(FRAME), 0.5)  # the first block: anything earlier has nothing to pair with
    for at in (0.51, 0.52, 0.53):
        now[0] = at
        canceller.far(noise(240, round(at * 100)), 24_000)
    apm.calls.clear()
    canceller.near(noise(FRAME), 0.515)
    assert apm.streams() == ["far", "near"]  # the block rendered at 0.51 only: the next waits its turn
    canceller.near(noise(FRAME), 0.535)
    assert apm.streams() == ["far", "near", "far", "far", "near"]


def test_after_a_gap_a_new_aec3_gets_the_far_audio_just_before_it_paired_with_silence() -> None:
    canceller, now, made = renewing_canceller()
    for k in range(151):  # the speaker plays 1.5 s; the microphone stops after half a second
        now[0] = k / 100
        canceller.far(noise(240, k), 24_000)
        if k < 50:
            canceller.near(noise(FRAME, k), k / 100 + 0.001)
    assert len(made) == 1  # the first block is no gap for a module that has heard nothing yet
    canceller.near(noise(FRAME), 1.501)
    # A new AEC3: the old one's pairing is off by whatever the stamps got wrong. It gets the half second before
    # the block (the delay hint is 0), each far frame with a silent microphone frame; then the block's own far
    # frame and the block. The rest of the second is dropped.
    old, new = made
    assert old.near_calls() == 50
    assert [(stream, rate, silent) for stream, _, rate, silent in new.calls] == [
        ("far", 24_000, False),
        ("near", RATE, True),
    ] * 50 + [("far", 24_000, False), ("near", RATE, False)]
    assert not canceller._far_q


def test_while_nothing_plays_silence_stands_in_one_frame_per_10_ms_at_the_outputs_rate() -> None:
    canceller, now, made = renewing_canceller()
    # The output is closed: microphone blocks of 5 ms, two at a time, then one of 30 ms. The far end gets a
    # silent frame for every 10 ms that passes, at the rate the output will play at, however the blocks come.
    stamp = 1.0
    for _ in range(10):
        stamp += 0.01
        canceller.near(noise(FRAME // 2), stamp)
        canceller.near(noise(FRAME // 2), stamp)
    stamp += 0.03
    canceller.near(noise(3 * FRAME), stamp)
    [apm] = made
    far = [(samples, rate, silent) for stream, samples, rate, silent in apm.calls if stream == "far"]
    assert set(far) == {(240, 24_000, True)}
    assert len(far) == int((stamp - (1.01 - 0.005)) * 100 + 1e-6)  # since the first block began, not per block
    assert apm.near_calls() == 13


def test_output_callbacks_in_groups_get_no_silence_between_them() -> None:
    apm = FakeApm()
    canceller, now = fake_canceller(apm)
    for k in range(60):  # 12 x 10 ms at a time, every 120 ms: a sound card with a big buffer
        if k % 12 == 0:
            now[0] = k / 100
            for j in range(k, k + 12):
                canceller.far(noise(240, j), 24_000)
        canceller.near(noise(FRAME, k), k / 100 + 0.001)
    far = [silent for stream, _, _, silent in apm.calls if stream == "far"]
    assert len(far) == 60 and not any(far)  # the output's own blocks only: it never stopped


def test_a_stopped_output_gets_silence_at_its_rate_and_a_new_aec3_once_its_echo_has_died_away() -> None:
    canceller, now, made = renewing_canceller(latency=0.05)
    for k in range(50):  # half a second of a reply at 24 kHz, in blocks that leave samples over
        now[0] = k / 100
        canceller.far(noise(250, k), 24_000)
        canceller.near(noise(FRAME, k), k / 100 + 0.001)
    assert canceller._far_rest.size > 0
    now[0] = 0.495
    canceller.far(None, 24_000)  # the output stopped (closed, or aborted by a stop)
    canceller.far(None, 24_000)  # and said so twice (the stream's own finished callback)
    old = made[-1]
    old.calls.clear()
    for k in range(50, 100):  # the microphone goes on alone
        canceller.near(noise(FRAME, k), k / 100 + 0.001)
    # The old AEC3 cancels the echo of what played last until it has died away: the delay hint (50 ms) plus
    # 200 ms after the stop. Then one new one, which hears only silence for the far end. All the silence goes
    # in at the rate the output played at (a new rate would start AEC3 over), one frame per 10 ms.
    assert len(made) == 2
    new = made[-1]
    assert old.near_calls() == 25 and new.near_calls() == 25
    far = [(samples, rate, silent) for stream, samples, rate, silent in old.calls + new.calls if stream == "far"]
    assert set(far) == {(240, 24_000, True)} and len(far) == int((0.991 - 0.495) * 100)
    assert canceller._far_rest.size == 0  # what was left of the last block belonged to a stream that ended
    new.calls.clear()
    now[0] = 1.004  # the output plays again: silence up to where its first block begins, then the block
    canceller.far(noise(240, 100), 24_000)
    canceller.near(noise(FRAME, 100), 1.011)
    assert [(stream, silent) for stream, _, _, silent in new.calls] == [("far", True), ("far", False), ("near", False)]
    assert len(made) == 2  # nothing more to start over


def test_new_sound_after_a_stop_gets_a_new_aec3_from_its_first_frame() -> None:
    canceller, now, made = renewing_canceller(latency=0.05)
    for k in range(30):
        now[0] = k / 100
        canceller.far(noise(240, k), 24_000)
        canceller.near(noise(FRAME, k), k / 100 + 0.001)
    now[0] = 0.3
    canceller.far(None, 24_000)  # stopped by the user talking over Jarvis; the stream restarts at once
    for k in range(30, 40):
        now[0] = k / 100
        canceller.far(np.zeros(240, np.float32) if k < 35 else noise(240, k), 24_000)  # silence, then a chime
        canceller.near(noise(FRAME, k), k / 100 + 0.001)
    # The restarted stream's silence still went to the old AEC3; the chime, 50 ms in, starts the new one.
    assert len(made) == 2
    new = made[-1]
    assert [silent for stream, _, _, silent in new.calls if stream == "far"] == [False] * 5


def test_a_stop_before_anything_played_starts_nothing_over() -> None:
    canceller, now, made = renewing_canceller()
    canceller.far(None, 24_000)
    for k in range(100):
        canceller.near(noise(FRAME, k), 1.0 + k / 100)
    now[0] = 2.0
    canceller.far(noise(240), 24_000)
    canceller.near(noise(FRAME), 2.001)
    assert len(made) == 1


def test_silence_at_an_output_rate_the_native_code_refuses_goes_in_at_16_khz() -> None:
    apm = FakeApm(refuse_rates=frozenset({48_000}))
    canceller = WebRtcEchoCanceller(far_rate=48_000, clock=lambda: 0.0, new_apm=lambda: apm, new_frame=FakeFrame)
    canceller.near(noise(3 * FRAME), 1.0)  # nothing has played yet: silence, at the output's rate if it can
    assert not canceller._failed
    assert [(samples, rate) for stream, samples, rate, _ in apm.calls if stream == "far"] == [(FRAME, RATE)] * 3


def test_silence_goes_in_at_the_rate_the_output_has_when_silence_is_first_needed() -> None:
    # The helper loads the canceller in the background while the output opens: a device that refused 24 kHz
    # must get silence at its own rate, or its first reply starts AEC3 over.
    apm = FakeApm()
    output_rate = [24_000]
    canceller = WebRtcEchoCanceller(
        far_rate=lambda: output_rate[0], clock=lambda: 0.0, new_apm=lambda: apm, new_frame=FakeFrame
    )
    output_rate[0] = 48_000  # the output opened after the canceller loaded
    canceller.near(noise(3 * FRAME), 1.0)
    output_rate[0] = 44_100  # read once: silence keeps one rate until the output plays
    canceller.near(noise(3 * FRAME), 1.03)
    assert [(samples, rate) for stream, samples, rate, _ in apm.calls if stream == "far"] == [(480, 48_000)] * 6


def test_a_stop_in_the_far_audio_dropped_after_a_gap_still_counts() -> None:
    # The microphone stalls while Jarvis speaks; the output closes during the stall and the far audio around
    # the close is dropped with the rest of the gap. Ignoring the stop with it left the canceller waiting for
    # far blocks that never came, and the next reply began at 35-39 dB instead of 71.
    canceller, now, made = renewing_canceller()
    for k in range(60):  # 0.6 s of a reply at 48 kHz; the microphone stops after half a second
        now[0] = k / 100
        canceller.far(noise(480, k), 48_000)
        if k < 50:
            canceller.near(noise(FRAME, k), k / 100 + 0.001)
    now[0] = 0.6
    canceller.far(None, 48_000)  # the output closes inside the gap, long before the far audio that is kept
    for k in range(200, 205):  # the microphone comes back 1.5 s later, the output still closed
        canceller.near(noise(FRAME, k), k / 100 + 0.001)
    assert len(made) == 2 and canceller._playing is False
    # After the gap, silence at the rate the output last played at, one frame per 10 ms, for the new AEC3.
    assert [(stream, samples, rate, silent) for stream, samples, rate, silent in made[-1].calls] == [
        ("far", 480, 48_000, True),
        ("near", FRAME, RATE, False),
    ] * 5


def test_the_fallback_resampler_hands_audio_back_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    # 22.05 kHz has no whole 10 ms frame. The default quality holds audio back and releases it in bursts up
    # to 37 ms late, behind its own echo; "QQ" does not.
    import soxr

    made: list[dict[str, object]] = []
    real = soxr.ResampleStream

    def spy(*args: object, **kwargs: object) -> object:
        made.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(soxr, "ResampleStream", spy)
    apm = FakeApm()
    canceller, now = fake_canceller(apm)
    for k in range(10):
        now[0] = k / 100
        canceller.far(noise(220, k), 22_050)
        canceller.near(noise(FRAME, k), k / 100 + 0.001)
    assert made and all(kwargs.get("quality") == "QQ" for kwargs in made)
    assert {(samples, rate) for stream, samples, rate, _ in apm.calls if stream == "far"} == {(FRAME, RATE)}


def test_a_far_rate_the_native_code_refuses_is_resampled_instead() -> None:
    apm = FakeApm(refuse_rates=frozenset({48_000}))
    canceller, now = fake_canceller(apm)
    for k in range(20):
        now[0] = k / 100
        canceller.far(noise(480, k), 48_000)
        canceller.near(noise(FRAME, k), k / 100 + 0.001)
    for k in range(20, 40):
        now[0] = k / 100
        canceller.far(noise(240, k), 24_000)
        canceller.near(noise(FRAME, k), k / 100 + 0.001)
    far = [(samples, rate) for stream, samples, rate, _ in apm.calls if stream == "far"]
    assert set(far[:15]) == {(FRAME, RATE)} and set(far[-15:]) == {(240, 24_000)}
    assert not canceller._failed and apm.near_calls() == 40


@pytest.mark.parametrize(("latency", "sent"), [(0.8, 500), (-0.1, 0), (0.0424, 42), (float("nan"), 0)])
def test_the_delay_hint_is_clamped_and_sent_before_every_frame(latency: float, sent: int) -> None:
    apm = FakeApm()
    canceller, _ = fake_canceller(apm, lambda: latency)
    canceller.near(noise(3 * FRAME), 0.0)
    assert apm.delays == [sent] * 3
    assert delay_ms(latency) == sent


def test_a_refused_delay_hint_does_not_stop_the_canceller() -> None:
    apm = FakeApm(refuse_delay=True)
    canceller, _ = fake_canceller(apm)
    assert canceller.near(noise(3 * FRAME), 0.0).size == 3 * FRAME
    assert apm.near_calls() == 3


def test_a_latency_the_device_cannot_report_does_not_stop_the_canceller() -> None:
    def latency() -> float:
        raise AttributeError("'Capture' object has no attribute 'input_latency'")

    apm = FakeApm()
    canceller, _ = fake_canceller(apm, latency)
    audio = noise(3 * FRAME)
    np.testing.assert_allclose(canceller.near(audio, 0.0), audio / 2, atol=2 / 32768)
    assert apm.delays == [0] * 3 and not canceller._failed


def test_a_failing_canceller_passes_the_microphone_through_from_then_on() -> None:
    apm = FakeApm(fail_on=2)
    canceller, _ = fake_canceller(apm)
    failures: list[Exception] = []
    canceller.set_on_failed(failures.append)
    audio = noise(5 * FRAME + 50)
    cleaned = canceller.near(audio, 0.0)
    assert cleaned.size == audio.size  # nothing lost, the partial frame included
    np.testing.assert_allclose(cleaned[: 2 * FRAME], audio[: 2 * FRAME] / 2, atol=2 / 32768)
    np.testing.assert_array_equal(cleaned[2 * FRAME :], audio[2 * FRAME :])
    later = noise(FRAME, 1)
    assert canceller.near(later, 0.01) is later and apm.near_calls() == 2
    assert [str(exc) for exc in failures] == ["apm_process_stream failed"]  # told once


def test_close_drops_the_native_module() -> None:
    apm = FakeApm()
    canceller, _ = fake_canceller(apm)
    canceller.far(noise(240), 24_000)
    canceller.close()
    assert canceller._apm is None and not canceller._far_q
    audio = noise(FRAME)
    assert canceller.near(audio, 1.0) is audio and apm.calls == []


def test_synthetic_signals_are_seeded() -> None:
    a, b = synthetic_speech(1.0, seed=3), synthetic_speech(1.0, seed=3)
    np.testing.assert_array_equal(a, b)
    assert -30 < level_db(a) < -18
    echo = synthetic_echo(a, delay_s=0.06, gain=0.5)
    assert not np.any(echo[: int(0.06 * RATE)])  # nothing arrives before the delay


def test_the_helper_tells_the_canceller_both_latencies(monkeypatch: pytest.MonkeyPatch) -> None:
    class Output:
        output_latency = 0.031
        device_rate = 24_000

    class Microphone:
        input_latency = 0.012

    output = Output()
    made: list[tuple[float, Callable[[], int]]] = []

    class Spy(WebRtcEchoCanceller):
        def __init__(self, delay_s: Callable[[], float], *, far_rate: Callable[[], int]) -> None:  # no native module
            made.append((delay_s(), far_rate))

    monkeypatch.setattr(echo_module, "WebRtcEchoCanceller", Spy)
    echo_module.loader(output, Microphone())()  # type: ignore[arg-type]
    [(delay, far_rate)] = made
    assert delay == pytest.approx(0.043)
    # The canceller loads as the output opens: the rate is read later, when silence first goes in.
    output.device_rate = 48_000  # the output opened at its own rate after the canceller loaded
    assert far_rate() == 48_000


def test_the_command_line_switches_echo_cancelling(monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvis_voice.cli import _capabilities, build_parser

    monkeypatch.delenv("JARVIS_AEC", raising=False)
    args = build_parser().parse_args(["run"])
    assert args.aec == "on" and "aec" in _capabilities(args)
    monkeypatch.setenv("JARVIS_AEC", "OFF")  # the mod sets it when the setting is off
    args = build_parser().parse_args(["run"])
    assert args.aec == "off" and "aec" not in _capabilities(args)
    args = build_parser().parse_args(["run", "--aec", "on", "--fake-audio"])
    assert args.aec == "on" and "aec" not in _capabilities(args)  # nothing is loaded with fake audio
