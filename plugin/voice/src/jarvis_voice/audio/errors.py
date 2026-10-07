"""Audio errors and the PortAudio -> protocol error-code mapping."""

from __future__ import annotations

from typing import Any, Literal

from .. import platform as plat
from ..protocol import ErrorCode

# PortAudio PaErrorCode values we care about.
PA_UNANTICIPATED_HOST_ERROR = -9999
PA_INVALID_SAMPLE_RATE = -9997
PA_INVALID_DEVICE = -9996
PA_INSUFFICIENT_MEMORY = -9992
PA_DEVICE_UNAVAILABLE = -9985

# PortAudio's WASAPI backend (v19.7, as bundled with sounddevice) reports a
# failed IAudioClient::Initialize as paInvalidDevice and a failed activation
# as paInsufficientMemory; the HRESULT only goes to the "last host error"
# slot, which sounddevice does not attach to these errors. The backend reads
# that slot right after a failed open and passes it in as ``host_code``.
CODES_WITH_HIDDEN_HOST_ERROR = (PA_INVALID_DEVICE, PA_INSUFFICIENT_MEMORY, PA_INVALID_SAMPLE_RATE)

# HRESULTs WASAPI reports through PortAudio's host error info (signed 32-bit).
E_ACCESSDENIED = -2147024891  # 0x80070005
AUDCLNT_E_DEVICE_IN_USE = -2004287478  # 0x8889000A
AUDCLNT_E_DEVICE_INVALIDATED = -2004287484  # 0x88890004
E_NOTFOUND = -2147023728  # 0x80070490

_BLOCKED_MARKERS = (
    "e_accessdenied",
    "0x80070005",
    "access is denied",
    "access denied",
    "permission denied",
    str(E_ACCESSDENIED),
)
_IN_USE_MARKERS = (
    "audclnt_e_device_in_use",
    "0x8889000a",
    "device in use",
    "device unavailable",
    "device or resource busy",
    "exclusive mode",
    str(AUDCLNT_E_DEVICE_IN_USE),
)
_MISSING_MARKERS = (
    "invalid device",
    "no default",
    "error querying device -1",
    "device_invalidated",
    "0x88890004",
    "0x80070490",
    "no such device",
    str(AUDCLNT_E_DEVICE_INVALIDATED),
    str(E_NOTFOUND),
)


class AudioError(Exception):
    """An audio failure the user can act on; carries a protocol error code and a hint."""

    def __init__(self, code: ErrorCode, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.code: ErrorCode = code
        self.message = message
        self.hint = hint


def _pa_details(exc: BaseException) -> tuple[str, int | None, int | None]:
    """(lowercased text, PaErrorCode, host error code) from a sounddevice.PortAudioError."""
    args: tuple[Any, ...] = tuple(getattr(exc, "args", ()) or ())
    pa_code = args[1] if len(args) > 1 and isinstance(args[1], int) else None
    host_code = None
    if len(args) > 2 and isinstance(args[2], tuple) and len(args[2]) >= 2 and isinstance(args[2][1], int):
        host_code = args[2][1]
    text = " ".join(str(a) for a in args).lower() or str(exc).lower()
    return text, pa_code, host_code


def classify_audio_error(
    exc: BaseException, kind: Literal["input", "output"], *, host_code: int | None = None
) -> AudioError:
    """Map a PortAudio (or OS) exception to mic_blocked / mic_in_use / no_*_device.

    ``host_code`` is PortAudio's last host error (an HRESULT on Windows), read
    by the caller right after the failure; it is used only for the PortAudio
    codes that hide it (see ``CODES_WITH_HIDDEN_HOST_ERROR``).
    """
    if isinstance(exc, AudioError):
        return exc
    text, pa_code, host = _pa_details(exc)
    if host is None and pa_code in CODES_WITH_HIDDEN_HOST_ERROR:
        host = host_code or None
    hints = plat.current()
    detail = str(exc) or type(exc).__name__
    if host is not None:
        detail += f" (host error 0x{host & 0xFFFFFFFF:08X})"

    if kind == "output":
        return AudioError("no_output_device", f"Cannot open the audio output: {detail}", hints.no_output_device_hint())

    if host == E_ACCESSDENIED or any(m in text for m in _BLOCKED_MARKERS):
        return AudioError("mic_blocked", f"Microphone access is blocked: {detail}", hints.mic_blocked_hint())
    if host == AUDCLNT_E_DEVICE_IN_USE or pa_code == PA_DEVICE_UNAVAILABLE or any(m in text for m in _IN_USE_MARKERS):
        return AudioError("mic_in_use", f"The microphone is in use by another app: {detail}", hints.mic_in_use_hint())
    if (
        pa_code == PA_INVALID_DEVICE
        or host in (AUDCLNT_E_DEVICE_INVALIDATED, E_NOTFOUND)
        or any(m in text for m in _MISSING_MARKERS)
    ):
        return AudioError("no_input_device", f"No usable microphone: {detail}", hints.no_input_device_hint())
    # Unknown open failure: report it as a device problem, with the raw text.
    return AudioError("no_input_device", f"Cannot open the microphone: {detail}", hints.no_input_device_hint())


def digital_silence_error() -> AudioError:
    """Exact zeros for a whole clip: the OS (privacy switch) or a hardware mute is feeding silence."""
    return AudioError(
        "mic_blocked",
        "The microphone delivered pure digital silence; it is muted or blocked.",
        plat.current().digital_silence_hint(),
    )
