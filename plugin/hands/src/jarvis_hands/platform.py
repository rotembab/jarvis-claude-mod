"""OS-specific bits: the single-instance lock, camera privacy hints, opening the camera settings.

One module rather than voice's package: hands has far less that differs
between systems, and each function decides by ``name()`` when it is called, so
tests can exercise every OS's branch on any machine.
"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Protocol

from .protocol import PlatformName

log = logging.getLogger(__name__)

ERROR_ALREADY_EXISTS = 183
ERROR_ACCESS_DENIED = 5
WINDOWS_CAMERA_SETTINGS_URI = "ms-settings:privacy-webcam"
MACOS_CAMERA_SETTINGS_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_Camera"
_WEBCAM_CONSENT_KEY = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\webcam"


class InstanceLock(Protocol):
    def acquire(self) -> bool:
        """Take the lock. False means another instance holds it."""
        ...

    def release(self) -> None: ...


def name() -> PlatformName:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


# --------------------------------------------------------------------------- single instance


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


class FileLock:
    """Exclusive, non-blocking ``flock`` on a file; the kernel drops it if we die."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> bool:
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        except BaseException:
            os.close(fd)
            raise
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode("ascii"))
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        import fcntl

        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None


def make_instance_lock(instance_name: str, lock_dir: Path) -> InstanceLock:
    """``Local\\<name>`` on Windows (``lock_dir`` unused); ``<lock_dir>/<name>.lock`` elsewhere."""
    if name() == "windows":
        return NamedMutexLock(f"Local\\{instance_name}")
    return FileLock(lock_dir / f"{instance_name}.lock")


# --------------------------------------------------------------------------- camera hints


def camera_access_denied() -> bool:
    """True when a Windows privacy switch denies desktop apps the camera (always False elsewhere).

    OpenCV only says the camera would not open; this tells "blocked" apart
    from "busy" or "missing".
    """
    if name() != "windows":
        return False
    import winreg

    switches = (
        (winreg.HKEY_LOCAL_MACHINE, _WEBCAM_CONSENT_KEY),  # Camera access (this device)
        (winreg.HKEY_CURRENT_USER, _WEBCAM_CONSENT_KEY),  # Camera access (this user)
        (winreg.HKEY_CURRENT_USER, _WEBCAM_CONSENT_KEY + r"\NonPackaged"),  # Let desktop apps access
    )
    for root, path in switches:
        try:
            with winreg.OpenKey(root, path) as key:  # type: ignore[attr-defined]
                value, _kind = winreg.QueryValueEx(key, "Value")  # type: ignore[attr-defined]
        except OSError:
            continue  # never set: Windows treats it as allowed
        if str(value).lower() == "deny":
            return True
    return False


def camera_blocked_hint() -> str:
    if name() == "windows":
        return (
            "Windows is blocking the camera. Open Settings > Privacy & security > Camera and turn on "
            "'Camera access' and 'Let desktop apps access your camera' "
            f"(Win+R, then {WINDOWS_CAMERA_SETTINGS_URI}). If the webcam has a privacy shutter or an "
            "off switch, open it. Then run /jarvis hands restart."
        )
    if name() == "macos":
        return (
            "macOS is blocking the camera. Open System Settings > Privacy & Security > Camera, allow your "
            "terminal (or the Claude app), then restart it."
        )
    return (
        "The camera could not be opened. Check that your user may use /dev/video* (the 'video' group) "
        "and that the webcam's privacy shutter is open."
    )


def camera_in_use_hint() -> str:
    if name() == "windows":
        return (
            "Another app is using the camera (Teams, Zoom, OBS, the Camera app or a browser tab). "
            "Close it, then run /jarvis hands restart."
        )
    return "Another app is using the camera. Close it, then run /jarvis hands restart."


def no_camera_hint() -> str:
    if name() == "windows":
        return (
            "No camera found. Check the webcam is plugged in (try another USB port, not a hub) and is listed "
            "under Settings > Bluetooth & devices > Cameras."
        )
    if name() == "macos":
        return "No camera found. Check the webcam is connected and shows up in System Information > Camera."
    return "No camera found. Check the webcam is connected and shows up as /dev/video*."


def open_camera_settings() -> bool:
    """Open the OS camera privacy page. Returns False when there is none or it could not be opened."""
    if name() == "windows":
        try:
            os.startfile(WINDOWS_CAMERA_SETTINGS_URI)  # type: ignore[attr-defined]
            return True
        except OSError as exc:
            log.warning("could not open %s: %s", WINDOWS_CAMERA_SETTINGS_URI, exc)
            return False
    if name() == "macos":
        try:
            subprocess.run(["open", MACOS_CAMERA_SETTINGS_URL], check=True, timeout=5)
            return True
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("could not open camera settings: %s", exc)
            return False
    return False


def suppress_crash_dialogs() -> None:
    """Let a crash end this process at once instead of waiting on an error dialog (Windows only).

    A native crash in MediaPipe or a camera driver would otherwise leave a hung
    "has stopped working" box holding the webcam, and the mod could not restart us.
    """
    if name() != "windows":
        return
    sem_failcriticalerrors, sem_nogpfaulterrorbox = 0x0001, 0x0002
    try:
        ctypes.WinDLL("kernel32").SetErrorMode(sem_failcriticalerrors | sem_nogpfaulterrorbox)  # type: ignore[attr-defined]
    except (OSError, AttributeError) as exc:
        log.debug("SetErrorMode failed: %s", exc)
