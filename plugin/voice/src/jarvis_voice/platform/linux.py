"""Linux: flock single instance and generic hints."""

from __future__ import annotations

from .posix_lock import make_instance_lock

__all__ = [
    "PREFERRED_HOSTAPI",
    "digital_silence_hint",
    "ensure_com",
    "make_instance_lock",
    "mic_blocked_hint",
    "mic_in_use_hint",
    "no_input_device_hint",
    "no_output_device_hint",
    "open_mic_settings",
    "ptt_hint",
    "suppress_crash_dialogs",
]

PREFERRED_HOSTAPI: str | None = None  # let PortAudio pick (PulseAudio/PipeWire via ALSA)


def mic_blocked_hint() -> str:
    return "The microphone delivered no signal. Check it is not muted (pavucontrol or alsamixer)."


def digital_silence_hint() -> str:
    return mic_blocked_hint()


def mic_in_use_hint() -> str:
    return "The microphone is busy. Close other apps that are recording and try again."


def no_input_device_hint() -> str:
    return "No usable microphone found. Check your sound settings (pavucontrol)."


def no_output_device_hint() -> str:
    return "No usable audio output found. Check your sound settings (pavucontrol)."


def ptt_hint() -> str:
    return "Global push-to-talk needs an X11 session (Wayland blocks global key hooks). Use /jarvis talk instead."


def open_mic_settings() -> bool:
    return False


def suppress_crash_dialogs() -> None:
    """Nothing to do: a crashing process never waits on a dialog here."""


def ensure_com() -> None:
    """Nothing to do: COM is Windows-only."""
