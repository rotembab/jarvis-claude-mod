"""Logging: stderr plus a rotating ``<dataDir>/logs/hands.log``, with the control token masked.

stderr is what the mod keeps from a crashed helper; the file is what a user
sends when something went wrong an hour ago. Both sit next to voice's.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

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
