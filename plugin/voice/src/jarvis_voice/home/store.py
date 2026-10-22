"""Where home control keeps what it knows, under ``<dataDir>/home``.

- ``devices.json``: the devices and hubs, with addresses and names but no secrets.
- ``credentials.dat``: pairing credentials, keys and tokens. On Windows it is
  encrypted with DPAPI, so only the same Windows account on the same PC can
  read it; elsewhere it is JSON readable by the owner only (mode 0600).

Both files are shared by the running helper and the setup wizard (another
process), so every read and write takes a lock file, writes are atomic
(temp file, then replace) and reads come from a cache that follows the
file's size and modification time.
"""

from __future__ import annotations

import contextlib
import copy
import json
import logging
import os
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Protocol, TypeVar

from ..logs import secret_filter
from .model import HomeConfig

log = logging.getLogger(__name__)

T = TypeVar("T")

DEVICES_FILE = "devices.json"
SECRETS_FILE = "credentials.dat"
LOCK_FILE = ".lock"
_LOCK_WAIT_S = 10.0


class StoreError(Exception):
    """A file could not be read or written; the message says which and why."""


# --------------------------------------------------------------------------- secret codecs


class SecretCodec(Protocol):
    name: str

    def encrypt(self, plain: bytes) -> bytes: ...

    def decrypt(self, sealed: bytes) -> bytes: ...


class PlainCodec:
    """No encryption: the file's permissions are the protection (macOS, Linux, tests)."""

    name = "plain"

    def encrypt(self, plain: bytes) -> bytes:
        return plain

    def decrypt(self, sealed: bytes) -> bytes:
        return sealed


class DpapiCodec:
    """Windows DPAPI (CryptProtectData) for the current user, through ctypes."""

    name = "dpapi"
    _ENTROPY = b"jarvis-home-v1"
    _UI_FORBIDDEN = 0x1

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        class Blob(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

        self._ctypes = ctypes
        self._blob = Blob
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        ptr = ctypes.POINTER(Blob)
        self._protect = crypt32.CryptProtectData
        self._protect.argtypes = [ptr, wintypes.LPCWSTR, ptr, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ptr]
        self._protect.restype = wintypes.BOOL
        self._unprotect = crypt32.CryptUnprotectData
        self._unprotect.argtypes = [ptr, ctypes.c_void_p, ptr, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ptr]
        self._unprotect.restype = wintypes.BOOL
        self._free = kernel32.LocalFree
        self._free.argtypes = [ctypes.c_void_p]
        self._free.restype = ctypes.c_void_p

    def _in(self, data: bytes) -> Any:
        buffer = self._ctypes.create_string_buffer(data, len(data))
        blob = self._blob(len(data), self._ctypes.cast(buffer, self._ctypes.POINTER(self._ctypes.c_char)))
        blob._keep = buffer  # keep the buffer alive as long as the blob
        return blob

    def _call(self, fn: Any, data: bytes, *, protect: bool) -> bytes:
        ctypes = self._ctypes
        source, entropy, out = self._in(data), self._in(self._ENTROPY), self._blob()
        if protect:
            ok = fn(
                ctypes.byref(source),
                "jarvis-home",
                ctypes.byref(entropy),
                None,
                None,
                self._UI_FORBIDDEN,
                ctypes.byref(out),
            )
        else:
            ok = fn(
                ctypes.byref(source), None, ctypes.byref(entropy), None, None, self._UI_FORBIDDEN, ctypes.byref(out)
            )
        if not ok:
            verb = "encrypt" if protect else "decrypt"
            raise StoreError(f"Windows could not {verb} the credentials (error {ctypes.get_last_error()})")
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            self._free(out.pbData)

    def encrypt(self, plain: bytes) -> bytes:
        return self._call(self._protect, plain, protect=True)

    def decrypt(self, sealed: bytes) -> bytes:
        return self._call(self._unprotect, sealed, protect=False)


def default_codec() -> SecretCodec:
    return DpapiCodec() if sys.platform == "win32" else PlainCodec()


# --------------------------------------------------------------------------- file lock


@contextlib.contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """An exclusive lock across processes, held for one read or read-modify-write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + _LOCK_WAIT_S
        if sys.platform == "win32":
            import msvcrt

            while True:
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise StoreError(f"{path.parent} is busy (another Jarvis is writing it)") from None
                    time.sleep(0.05)
            try:
                yield
            finally:
                os.lseek(fd, 0, os.SEEK_SET)
                with contextlib.suppress(OSError):
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        raise StoreError(f"{path.parent} is busy (another Jarvis is writing it)") from None
                    time.sleep(0.05)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # Windows refuses to replace a file another process has open; that is
        # brief (readers hold the lock only to read), so retry for a moment.
        for attempt in range(20):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.05)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def _stamp(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    return (st.st_mtime_ns, st.st_size)


# --------------------------------------------------------------------------- the store


class HomeStore:
    def __init__(self, data_dir: Path, *, codec: SecretCodec | None = None) -> None:
        self.root = Path(data_dir) / "home"
        self.devices_path = self.root / DEVICES_FILE
        self.secrets_path = self.root / SECRETS_FILE
        self._lock_path = self.root / LOCK_FILE
        self._codec = codec
        self._mutex = threading.RLock()
        self._config: tuple[tuple[int, int] | None, HomeConfig] | None = None
        self._secrets: tuple[tuple[int, int] | None, dict[str, dict[str, Any]]] | None = None

    @property
    def codec(self) -> SecretCodec:
        if self._codec is None:
            self._codec = default_codec()
        return self._codec

    # -- devices.json

    def load(self) -> HomeConfig:
        """The saved configuration (a copy: change it through ``update``)."""
        with self._mutex:
            stamp = _stamp(self.devices_path)
            if self._config is None or self._config[0] != stamp:
                with _file_lock(self._lock_path):
                    self._config = self._read_config()
            return copy.deepcopy(self._config[1])

    def update(self, mutate: Callable[[HomeConfig], T]) -> T:
        """Reads the latest file, applies ``mutate`` and saves, all under the lock."""
        with self._mutex, _file_lock(self._lock_path):
            _, config = self._read_config()
            result = mutate(config)
            payload = json.dumps(config.to_json(), indent=2, ensure_ascii=False).encode("utf-8")
            self.root.mkdir(parents=True, exist_ok=True)
            _write_atomic(self.devices_path, payload + b"\n")
            self._config = (_stamp(self.devices_path), copy.deepcopy(config))
            return result

    def _read_config(self) -> tuple[tuple[int, int] | None, HomeConfig]:
        stamp = _stamp(self.devices_path)
        if stamp is None:
            return None, HomeConfig()
        try:
            data = json.loads(self.devices_path.read_text(encoding="utf-8"))
            return stamp, HomeConfig.from_json(data)
        except (OSError, ValueError) as exc:
            # A hand-edited file with a typo should not take home control down silently.
            log.warning("cannot read %s: %s", self.devices_path, exc)
            raise StoreError(f"{self.devices_path} could not be read: {exc}") from exc

    # -- credentials.dat

    def secret(self, key: str) -> dict[str, Any] | None:
        """The credentials saved under ``key`` (a copy), or None."""
        with self._mutex:
            value = self._load_secrets().get(key)
            return copy.deepcopy(value) if value is not None else None

    def set_secret(self, key: str, value: dict[str, Any] | None) -> None:
        """Saves (or with None, deletes) the credentials under ``key``."""
        with self._mutex, _file_lock(self._lock_path):
            secrets = dict(self._read_secrets()[1])
            if value is None:
                secrets.pop(key, None)
            else:
                secrets[key] = copy.deepcopy(value)
                _register(value)
            plain = json.dumps(secrets, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            self.root.mkdir(parents=True, exist_ok=True)
            _write_atomic(self.secrets_path, self.codec.encrypt(plain))
            self._secrets = (_stamp(self.secrets_path), secrets)

    def secret_keys(self) -> list[str]:
        with self._mutex:
            return sorted(self._load_secrets())

    def _load_secrets(self) -> dict[str, dict[str, Any]]:
        stamp = _stamp(self.secrets_path)
        if self._secrets is None or self._secrets[0] != stamp:
            with _file_lock(self._lock_path):
                self._secrets = self._read_secrets()
        return self._secrets[1]

    def _read_secrets(self) -> tuple[tuple[int, int] | None, dict[str, dict[str, Any]]]:
        stamp = _stamp(self.secrets_path)
        if stamp is None:
            return None, {}
        try:
            data = json.loads(self.codec.decrypt(self.secrets_path.read_bytes()).decode("utf-8"))
        except (OSError, ValueError, StoreError) as exc:
            # Never include the file's contents: it may be partly readable.
            log.warning("cannot read the home credentials: %s", type(exc).__name__)
            raise StoreError(
                f"{self.secrets_path} could not be read ({type(exc).__name__}); "
                "it opens only for the Windows account that saved it"
            ) from None
        secrets = (
            {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, dict)}
            if isinstance(data, dict)
            else {}
        )
        for value in secrets.values():
            _register(value)
        return stamp, secrets


def _register(value: Any) -> None:
    """Every secret string is masked in the logs from the moment it is loaded."""
    if isinstance(value, str):
        secret_filter.add(value)
    elif isinstance(value, dict):
        for item in value.values():
            _register(item)
    elif isinstance(value, list):
        for item in value:
            _register(item)
