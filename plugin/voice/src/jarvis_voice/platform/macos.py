"""macOS: flock single instance and privacy hints (phase 1 stubs beyond that)."""

from __future__ import annotations

import logging
import subprocess

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

log = logging.getLogger(__name__)

PREFERRED_HOSTAPI = "Core Audio"
_MIC_SETTINGS_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"


def mic_blocked_hint() -> str:
    return (
        "macOS is blocking microphone access. Open System Settings > Privacy & Security > Microphone "
        "and allow your terminal (or the Claude app)."
    )


def digital_silence_hint() -> str:
    return mic_blocked_hint()


def mic_in_use_hint() -> str:
    return "The microphone is busy. Close other apps that are recording and try again."


def no_input_device_hint() -> str:
    return "No usable microphone found. Check System Settings > Sound > Input."


def no_output_device_hint() -> str:
    return "No usable audio output found. Check System Settings > Sound > Output."


def ptt_hint() -> str:
    return (
        "Allow your terminal (or the Claude app) under System Settings > Privacy & Security > "
        "Input Monitoring and Accessibility, then restart it. Meanwhile use /jarvis talk."
    )


def open_mic_settings() -> bool:
    try:
        subprocess.run(["open", _MIC_SETTINGS_URL], check=True, timeout=5)
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("could not open microphone settings: %s", exc)
        return False


def suppress_crash_dialogs() -> None:
    """Nothing to do: a crashing process never waits on a dialog here."""


def ensure_com() -> None:
    """Nothing to do: COM is Windows-only."""
