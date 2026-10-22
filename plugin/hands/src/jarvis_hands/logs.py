"""Logging: stderr plus a rotating ``<dataDir>/logs/hands.log``, with the control token masked.

stderr is what the mod keeps from a crashed helper; the file is what a user
sends when something went wrong an hour ago. Both sit next to voice's.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
import threading
import traceback
from collections.abc import Mapping
from pathlib import Path
from types import TracebackType
from typing import Any

LOG_FILE_NAME = "hands.log"
_FORMAT = "%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s"
# Libraries that log at INFO (or chatter through absl) and drown the helper's own lines.
NOISY_LOGGERS = ("matplotlib", "PIL", "absl", "urllib3")


def mask_secret(value: str | None) -> str | None:
    """Return a display-safe form of a secret: only the last four characters survive."""
    if not value:
        return None
    if len(value) < 12:
        return "****"
    return "****" + value[-4:]


class SecretFilter(logging.Filter):
    """Replaces any registered secret in log records with its masked form."""

    def __init__(self) -> None:
        super().__init__()
        self._secrets: list[str] = []

    def add(self, secret: str | None) -> None:
        if secret and len(secret) >= 6 and secret not in self._secrets:
            self._secrets.append(secret)

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        message = record.getMessage()
        masked = message
        for secret in self._secrets:
            if secret in masked:
                masked = masked.replace(secret, mask_secret(secret) or "****")
        if masked != message:
            record.msg, record.args = masked, None
        return True


secret_filter = SecretFilter()

# --- exception text while the air keyboard is open (DESIGN-KEYBOARD.md 3.8, F4) ------------------------------------
# An exception raised near a typed key can carry the key, a stroke or a box fragment in its message. While a keyboard
# session is open every route that would write that message somewhere (a log record, stderr, a reply, a report) writes
# the type name instead, and a traceback keeps its frames: where it happened, never what it carried.

_scrub_on = False
_MARK = "_jarvis_keyboard_scrub"


def exc_text(exc: BaseException, *, typed: bool = True) -> str:
    """``"Type: message"`` as it always was (``typed=False``: the message); the type name while the scrub is on."""
    if _scrub_on:
        return type(exc).__name__
    return f"{type(exc).__name__}: {exc}" if typed else str(exc)


def keyboard_scrub(on: bool) -> None:
    """The controller calls this with True at every open and False at every close. The first True installs the hooks."""
    global _scrub_on
    if on:
        _install_scrub_hooks()
    _scrub_on = on


def _frames_and_type(exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None) -> str:
    # the primary exception only: no message, and no chained exception (its message is data too)
    name = type(exc).__name__ if exc is not None else getattr(exc_type, "__name__", "Exception")
    return "".join(traceback.format_tb(tb)) + name


def _scrub_args(args: Any) -> Any:
    if isinstance(args, Mapping):
        return {k: type(v).__name__ if isinstance(v, BaseException) else v for k, v in args.items()}
    if isinstance(args, tuple):
        return tuple(type(a).__name__ if isinstance(a, BaseException) else a for a in args)
    return args


def _scrub_record(record: logging.LogRecord) -> None:
    if isinstance(record.msg, BaseException):
        record.msg = type(record.msg).__name__
    record.args = _scrub_args(record.args)
    if record.exc_info:
        exc_type, exc, tb = record.exc_info
        record.exc_text = _frames_and_type(exc_type, exc, tb)
        record.exc_info = None


def _install_scrub_hooks() -> None:
    inner_factory = logging.getLogRecordFactory()
    if not getattr(inner_factory, _MARK, False):

        def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
            record = inner_factory(*args, **kwargs)
            if _scrub_on:
                _scrub_record(record)
            return record

        setattr(factory, _MARK, True)
        logging.setLogRecordFactory(factory)

    inner_hook = sys.excepthook
    if not getattr(inner_hook, _MARK, False):

        def excepthook(exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None) -> None:
            if not _scrub_on:
                inner_hook(exc_type, exc, tb)
                return
            sys.stderr.write(f"{_frames_and_type(exc_type, exc, tb)}\n")

        setattr(excepthook, _MARK, True)
        sys.excepthook = excepthook

    inner_thread_hook = threading.excepthook
    if not getattr(inner_thread_hook, _MARK, False):

        def thread_hook(args: threading.ExceptHookArgs) -> None:
            if not _scrub_on:
                inner_thread_hook(args)
                return
            who = args.thread.name if args.thread is not None else "?"
            sys.stderr.write(
                f"Exception in thread {who}:\n{_frames_and_type(args.exc_type, args.exc_value, args.exc_traceback)}\n"
            )

        setattr(thread_hook, _MARK, True)
        threading.excepthook = thread_hook


def setup_logging(data_dir: Path | None, level: str | int = "INFO") -> Path | None:
    """Configure root logging. Returns the log file path when one is used."""
    root = logging.getLogger()
    try:
        root.setLevel(level.upper() if isinstance(level, str) else level)
    except (TypeError, ValueError):
        root.setLevel(logging.INFO)
        bad_level: object = level
    else:
        bad_level = None
    for handler in list(root.handlers):
        root.removeHandler(handler)
    formatter = logging.Formatter(_FORMAT)

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)
    stderr_handler.addFilter(secret_filter)
    root.addHandler(stderr_handler)

    log_file: Path | None = None
    if data_dir is not None:
        try:
            log_dir = data_dir / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_file = log_dir / LOG_FILE_NAME
            file_handler = logging.handlers.RotatingFileHandler(
                log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
            )
            file_handler.setFormatter(formatter)
            file_handler.addFilter(secret_filter)
            root.addHandler(file_handler)
        except OSError as exc:
            logging.getLogger(__name__).warning("cannot open log file in %s: %s", data_dir, exc)
            log_file = None

    for noisy in NOISY_LOGGERS:
        logging.getLogger(noisy).setLevel(max(logging.WARNING, root.level))
    if bad_level is not None:
        logging.getLogger(__name__).warning("unknown log level %r; using INFO", bad_level)
    return log_file
