"""Audio I/O: capture (16 kHz mono float32 with pre-roll) and queue-fed playback."""

from .capture import STT_SAMPLERATE, Capture, CaptureBuffer
from .errors import AudioError, classify_audio_error
from .playback import TTS_SAMPLERATE, Playback, PlaybackMixer

__all__ = [
    "STT_SAMPLERATE",
    "TTS_SAMPLERATE",
    "AudioError",
    "Capture",
    "CaptureBuffer",
    "Playback",
    "PlaybackMixer",
    "classify_audio_error",
]
