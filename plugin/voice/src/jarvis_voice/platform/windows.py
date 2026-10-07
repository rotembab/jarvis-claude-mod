"""Windows: named-mutex single instance, WASAPI, microphone privacy hints."""

from __future__ import annotations

import ctypes
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

PREFERRED_HOSTAPI = "Windows WASAPI"
ERROR_ALREADY_EXISTS = 183
ERROR_ACCESS_DENIED = 5
MIC_SETTINGS_URI = "ms-settings:privacy-microphone"
_MIC_CONSENT_KEY = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"


class NamedMutexLock:
    """Per-session named mutex (``Local\\<name>``) via kernel32.CreateMutexW.

    We only need existence, not ownership: the mutex object lives as long as a
    handle to it is open, and Windows closes our handle when the process dies,
    so a crash never leaves a stale lock behind.
    """

    def __init__(self, mutex_name: str) -> None:
        self.mutex_name = mutex_name
        self._handle: int | None = None

    @staticmethod
    def _kernel32() -> ctypes.WinDLL:  # type: ignore[name-defined]
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        # Explicit prototypes: the default int restype would truncate 64-bit handles.
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL
        return k32

    def acquire(self) -> bool:
        k32 = self._kernel32()
        handle = k32.CreateMutexW(None, False, self.mutex_name)
        err = ctypes.get_last_error()  # type: ignore[attr-defined]
        if not handle and err == ERROR_ACCESS_DENIED:
            return False  # exists, created by an instance running with other rights
        if not handle:
            raise OSError(err, f"CreateMutexW({self.mutex_name!r}) failed with error {err}")
        if err == ERROR_ALREADY_EXISTS:
            k32.CloseHandle(handle)
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle:
            self._kernel32().CloseHandle(self._handle)
            self._handle = None


def make_instance_lock(instance_name: str, lock_dir: Path) -> NamedMutexLock:
    del lock_dir  # the mutex lives in the kernel object namespace
    return NamedMutexLock(f"Local\\{instance_name}")


def mic_blocked_hint() -> str:
    return (
        "Windows is blocking microphone access. Open Settings > Privacy & security > Microphone "
        "and turn on 'Microphone access' and 'Let desktop apps access your microphone' "
        f"(Win+R, then {MIC_SETTINGS_URI}). If your headset has a mute switch or flip-to-mute boom, "
        "make sure it is unmuted."
    )


def mic_silent_hint() -> str:
    return (
        "The microphone is sending only silence. Check the headset is switched on and not muted "
        "(mute button or flip-to-mute boom), and that it is the default input under "
        "Settings > System > Sound > Input. Some headsets send exact silence while you are quiet, "
        "so if Jarvis hears you when you speak, the microphone is fine."
    )


def mic_access_denied() -> bool:
    """True when a Windows privacy switch denies desktop apps the microphone."""
    import winreg

    switches = (
        (winreg.HKEY_LOCAL_MACHINE, _MIC_CONSENT_KEY),  # Microphone access (this device)
        (winreg.HKEY_CURRENT_USER, _MIC_CONSENT_KEY),  # Microphone access (this user)
        (winreg.HKEY_CURRENT_USER, _MIC_CONSENT_KEY + r"\NonPackaged"),  # Let desktop apps access
    )
    for root, path in switches:
        try:
            with winreg.OpenKey(root, path) as key:
                value, _kind = winreg.QueryValueEx(key, "Value")
        except OSError:
            continue  # never set: Windows treats it as allowed
        if str(value).lower() == "deny":
            return True
    return False


def digital_silence_hint() -> str:
    """Pure digital silence comes from a privacy switch or from a muted or switched-off device."""
    try:
        denied = mic_access_denied()
    except Exception:  # registry trouble must not hide the hint
        log.debug("could not read the microphone consent", exc_info=True)
        denied = True
    return mic_blocked_hint() if denied else mic_silent_hint()


def mic_in_use_hint() -> str:
    return (
        "Another app has the microphone in exclusive mode. Close it, or untick 'Allow applications "
        "to take exclusive control of this device' under Sound settings > the microphone > Properties > Advanced."
    )


def no_input_device_hint() -> str:
    return (
        "No usable microphone found. Check the headset is connected and switched on, and that it is "
        "enabled under Settings > System > Sound > Input."
    )


def no_output_device_hint() -> str:
    return "No usable audio output found. Check Settings > System > Sound > Output."


def ptt_hint() -> str:
    return "Global keyboard hooks are unavailable; use /jarvis talk to talk instead."


def open_mic_settings() -> bool:
    """Open the microphone privacy page. Returns False when it could not be opened."""
    try:
        os.startfile(MIC_SETTINGS_URI)  # type: ignore[attr-defined]
        return True
    except OSError as exc:
        log.warning("could not open %s: %s", MIC_SETTINGS_URI, exc)
        return False


def suppress_crash_dialogs() -> None:
    """Let a crash end this process at once instead of waiting on an error dialog.

    Used by the ``probe-cuda`` child, which may abort inside CTranslate2: the
    parent must see the exit code, not a hung "has stopped working" box.
    """
    sem_failcriticalerrors, sem_nogpfaulterrorbox = 0x0001, 0x0002
    try:
        ctypes.WinDLL("kernel32").SetErrorMode(sem_failcriticalerrors | sem_nogpfaulterrorbox)  # type: ignore[attr-defined]
    except OSError as exc:
        log.debug("SetErrorMode failed: %s", exc)
