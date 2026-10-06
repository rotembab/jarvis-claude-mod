"""Real audio via sounddevice/PortAudio (WASAPI shared mode with auto-convert on Windows).

sounddevice is imported lazily: on a machine without PortAudio (CI, the build
container) importing it raises OSError, and the rest of the helper must still
work in fake mode.
"""

from __future__ import annotations

import logging
import threading
import time
from types import ModuleType
from typing import Any, Literal

import numpy as np

from .. import platform as plat
from ..protocol import ErrorCode
from .capture import STT_SAMPLERATE, CaptureBuffer, to_stt_rate
from .devices import DeviceInfo, select_device
from .errors import AudioError, classify_audio_error
from .playback import TTS_SAMPLERATE, PlaybackMixer

log = logging.getLogger(__name__)

WASAPI = "Windows WASAPI"


PORTAUDIO_HINT = (
    "The PortAudio audio library could not be loaded. On Linux install it with your package manager "
    "(e.g. libportaudio2); on Windows and macOS reinstall the voice helper, whose sounddevice wheel bundles it."
)


def load_sounddevice(kind: Literal["input", "output"] = "input") -> ModuleType:
    try:
        import sounddevice
    except (OSError, ImportError) as exc:
        code: ErrorCode = "no_input_device" if kind == "input" else "no_output_device"
        raise AudioError(code, f"PortAudio is not available: {exc}", PORTAUDIO_HINT) from exc
    return sounddevice


def query_devices(sd: ModuleType) -> tuple[list[DeviceInfo], dict[str, tuple[int, int]]]:
    """All devices plus each host API's (default input, default output) index."""
    hostapis = sd.query_hostapis()
    devices = [
        DeviceInfo(
            index=i,
            name=str(d["name"]),
            hostapi=str(hostapis[d["hostapi"]]["name"]),
            max_input_channels=int(d["max_input_channels"]),
            max_output_channels=int(d["max_output_channels"]),
            default_samplerate=float(d["default_samplerate"]),
        )
        for i, d in enumerate(sd.query_devices())
    ]
    defaults = {str(h["name"]): (int(h["default_input_device"]), int(h["default_output_device"])) for h in hostapis}
    return devices, defaults


def resolve_device(sd: ModuleType, kind: Literal["input", "output"], wanted: str | None) -> DeviceInfo:
    devices, defaults = query_devices(sd)
    hostapi = plat.current().PREFERRED_HOSTAPI
    slot = 0 if kind == "input" else 1
    if hostapi and hostapi in defaults:
        default_index: int | None = defaults[hostapi][slot]
    else:
        hostapi = None
        try:
            default_index = int(sd.default.device[slot])
        except (TypeError, ValueError, IndexError):
            default_index = None
    device = select_device(devices, kind, wanted=wanted, hostapi=hostapi, default_index=default_index)
    if device is None:
        hints = plat.current()
        if kind == "input":
            raise AudioError("no_input_device", "No microphone was found.", hints.no_input_device_hint())
        raise AudioError("no_output_device", "No audio output device was found.", hints.no_output_device_hint())
    if wanted and wanted.casefold() not in device.name.casefold():
        log.warning("%s device %r not found; using %r", kind, wanted, device.name)
    return device


def _extra_settings(sd: ModuleType, device: DeviceInfo) -> Any:
    if device.hostapi == WASAPI:
        # Shared mode + auto_convert: Windows resamples/remixes for us.
        return sd.WasapiSettings(auto_convert=True)
    return None


def last_host_error(sd: ModuleType) -> int | None:
    """PortAudio's last host error code (an HRESULT on Windows), or None.

    Read it on the thread that just failed, right after the failure: it is a
    process-wide slot that the next failing call overwrites.
    """
    try:
        code = int(sd._lib.Pa_GetLastHostErrorInfo().errorCode)
    except Exception:  # noqa: BLE001 - a sounddevice without _lib, or no info at all
        return None
    return code or None


def rescan_devices() -> None:
    """Re-initialise PortAudio so newly plugged devices appear (closes all streams)."""
    sd = load_sounddevice()
    sd._terminate()
    sd._initialize()


class SoundDeviceCapture:
    def __init__(self, wanted: str | None = None, *, preroll_s: float = 0.3, max_record_s: float = 60.0) -> None:
        self._wanted = wanted
        self._preroll_s = preroll_s
        self._max_record_s = max_record_s
        self._lock = threading.Lock()
        self._stream: Any = None
        self._buffer = CaptureBuffer(STT_SAMPLERATE, preroll_s=preroll_s, max_record_s=max_record_s)
        self._device: DeviceInfo | None = None
        self._channels = 1
        self._dead = False
        self._began_at = 0.0

    @property
    def device_name(self) -> str | None:
        return self._device.name if self._device else None

    @property
    def is_open(self) -> bool:
        return self._stream is not None and not self._dead

    @property
    def level(self) -> float:
        return self._buffer.level

    def _callback(self, indata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
        # Real-time thread: copy channel 0 and hand it over; nothing else.
        self._buffer.feed(indata[:, 0].copy())

    def _finished(self) -> None:
        self._dead = True

    def open(self) -> None:
        with self._lock:
            if self.is_open:
                return
            self._close_locked()
            sd = load_sounddevice("input")
            device = resolve_device(sd, "input", self._wanted)
            settings = _extra_settings(sd, device)
            native = int(device.default_samplerate) or 48_000
            attempts = [(STT_SAMPLERATE, 1), (native, 1), (native, min(2, device.max_input_channels))]
            last_exc: BaseException | None = None
            last_host: int | None = None
            for rate, channels in dict.fromkeys(attempts):
                try:
                    stream = sd.InputStream(
                        device=device.index,
                        samplerate=rate,
                        channels=channels,
                        dtype="float32",
                        latency="low",
                        callback=self._callback,
                        finished_callback=self._finished,
                        extra_settings=settings,
                    )
                    self._buffer = CaptureBuffer(rate, preroll_s=self._preroll_s, max_record_s=self._max_record_s)
                    self._dead = False
                    stream.start()
                except Exception as exc:  # noqa: BLE001 - PortAudioError, or ValueError for bad parameters
                    last_exc, last_host = exc, last_host_error(sd)
                    continue
                self._stream, self._device, self._channels = stream, device, channels
                log.info("microphone open: %r (%s) at %d Hz, %d ch", device.name, device.hostapi, rate, channels)
                return
            assert last_exc is not None
            raise classify_audio_error(last_exc, "input", host_code=last_host) from last_exc

    def _close_locked(self) -> None:
        if self._stream is not None:
            try:
                self._stream.abort()
                self._stream.close()
            except Exception:
                log.debug("error closing input stream", exc_info=True)
            self._stream = None

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def begin(self) -> None:
        self._buffer.begin()
        self._began_at = time.monotonic()

    def end(self) -> np.ndarray:
        buffer = self._buffer
        recorded, elapsed = buffer.recorded_frames, time.monotonic() - self._began_at
        clip = buffer.end()
        if elapsed > 0.5 and recorded < 0.2 * elapsed * buffer.samplerate:
            # The stream is open but silent at the driver level: the device went away.
            self._dead = True
            raise AudioError(
                "no_input_device",
                "The microphone stopped delivering audio (unplugged or switched off?).",
                plat.current().no_input_device_hint(),
            )
        return to_stt_rate(clip, buffer.samplerate)

    def recording_seconds(self) -> float:
        return self._buffer.clip_frames / self._buffer.samplerate


class SoundDevicePlayback:
    """Output stream fed by the mixer. Closed after ``idle_close_s`` of silence so
    the headset can sleep; reopened on demand (open() or new audio)."""

    samplerate = TTS_SAMPLERATE

    def __init__(self, wanted: str | None = None, *, idle_close_s: float | None = 30.0) -> None:
        self._wanted = wanted
        self._idle_close_s = idle_close_s
        self._last_active = time.monotonic()
        self._reaper: threading.Thread | None = None
        self._lock = threading.Lock()
        self._stream: Any = None
        self._device: DeviceInfo | None = None
        self._device_rate = TTS_SAMPLERATE
        self._mixer = PlaybackMixer(TTS_SAMPLERATE)
        self._resampler: Any = None
        self._dead = False

    # -- properties

    @property
    def device_name(self) -> str | None:
        return self._device.name if self._device else None

    @property
    def level(self) -> float:
        return self._mixer.level

    @property
    def speech_generation(self) -> int:
        return self._mixer.generation

    @property
    def speech_frames_played(self) -> int:
        # Reported in TTS-rate frames regardless of the device rate.
        return int(self._mixer.frames_played * self.samplerate / self._device_rate)

    @property
    def speech_frames_queued(self) -> int:
        return int(self._mixer.frames_queued * self.samplerate / self._device_rate)

    @property
    def paused(self) -> bool:
        return self._mixer.paused

    @property
    def output_latency(self) -> float:
        try:
            return float(self._stream.latency) if self._stream is not None else 0.0
        except Exception:  # noqa: BLE001
            return 0.0

    # -- device

    def _callback(self, outdata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
        outdata[:] = self._mixer.render(frames)[:, None]
        if self._mixer.level > 0.0:
            self._last_active = time.monotonic()

    def _finished(self) -> None:
        self._dead = True

    def _reap_idle(self) -> None:
        assert self._idle_close_s is not None
        while True:
            time.sleep(min(2.0, self._idle_close_s / 2))
            with self._lock:
                idle = time.monotonic() - self._last_active > self._idle_close_s
                if self._stream is not None and idle and self._mixer.idle():
                    log.info("closing idle audio output")
                    self._close_locked()

    def open(self) -> None:
        with self._lock:
            self._last_active = time.monotonic()
            if self._idle_close_s is not None and self._reaper is None:
                self._reaper = threading.Thread(target=self._reap_idle, name="output-reaper", daemon=True)
                self._reaper.start()
            if self._stream is not None and not self._dead:
                return
            self._close_locked()
            sd = load_sounddevice("output")
            device = resolve_device(sd, "output", self._wanted)
            settings = _extra_settings(sd, device)
            native = int(device.default_samplerate) or 48_000
            attempts = [(TTS_SAMPLERATE, 1), (native, 1), (native, min(2, device.max_output_channels))]
            last_exc: BaseException | None = None
            last_host: int | None = None
            for rate, channels in dict.fromkeys(attempts):
                try:
                    stream = sd.OutputStream(
                        device=device.index,
                        samplerate=rate,
                        channels=channels,
                        dtype="float32",
                        latency="low",
                        callback=self._callback,
                        finished_callback=self._finished,
                        extra_settings=settings,
                    )
                    if rate != self._device_rate:
                        # Keep the mixer (and its generation) so an in-flight reply survives a reopen.
                        self._mixer.samplerate = rate
                        self._device_rate = rate
                        self._resampler = None
                    self._dead = False
                    stream.start()
                except Exception as exc:  # noqa: BLE001
                    last_exc, last_host = exc, last_host_error(sd)
                    continue
                self._stream, self._device = stream, device
                log.info("audio output open: %r (%s) at %d Hz, %d ch", device.name, device.hostapi, rate, channels)
                return
            assert last_exc is not None
            raise classify_audio_error(last_exc, "output", host_code=last_host) from last_exc

    def _close_locked(self) -> None:
        if self._stream is not None:
            try:
                self._stream.abort()
                self._stream.close()
            except Exception:
                log.debug("error closing output stream", exc_info=True)
            self._stream = None

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    # -- audio

    def _to_device_rate(self, pcm: np.ndarray, *, streaming: bool) -> np.ndarray:
        if self._device_rate == self.samplerate:
            return pcm
        import soxr

        if not streaming:
            return np.asarray(soxr.resample(pcm, self.samplerate, self._device_rate), dtype=np.float32)
        if self._resampler is None:
            self._resampler = soxr.ResampleStream(self.samplerate, self._device_rate, 1, dtype="float32")
        return np.asarray(self._resampler.resample_chunk(pcm), dtype=np.float32)

    def _ensure_open(self) -> None:
        if self._stream is None or self._dead:
            try:
                self.open()
            except AudioError as exc:
                log.warning("audio output unavailable: %s", exc.message)

    def add_speech(self, pcm: np.ndarray, generation: int | None = None) -> None:
        if generation is not None and generation != self._mixer.generation:
            return
        self._ensure_open()
        self._mixer.add_speech(self._to_device_rate(np.asarray(pcm, np.float32), streaming=True), generation)

    def add_effect(self, pcm: np.ndarray) -> None:
        self._ensure_open()
        self._mixer.add_effect(self._to_device_rate(np.asarray(pcm, np.float32), streaming=False))

    def end_speech(self, generation: int | None = None) -> None:
        # The streaming resampler holds back a few ms of filter delay; release it.
        resampler, self._resampler = self._resampler, None
        if resampler is not None:
            tail = np.asarray(resampler.resample_chunk(np.zeros(0, np.float32), last=True), dtype=np.float32)
            self._mixer.add_speech(tail, generation)

    def stop_speech(self) -> None:
        self._mixer.clear_speech()
        self._resampler = None
        stream = self._stream
        if stream is None:
            return
        try:
            # abort() discards what the device already buffered; restart for chimes.
            stream.abort()
            stream.start()
            self._dead = False  # abort() fired finished_callback; the stream is alive again
        except Exception:
            log.warning("output stream restart failed; will reopen", exc_info=True)
            self._dead = True

    def reset_speech_counters(self) -> None:
        self._mixer.reset_counters()

    def speech_idle(self) -> bool:
        return self._mixer.speech_pending() == 0

    def set_paused(self, paused: bool) -> None:
        self._mixer.paused = paused
