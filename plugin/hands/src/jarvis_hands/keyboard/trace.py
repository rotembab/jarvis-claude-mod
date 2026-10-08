"""The practice tap log and the landmark trace: writers, readers and clean-up (DESIGN-KEYBOARD.md 5.7, 2.12.10).

STUB (T2). Track T2 replaces this file; the names below are the contract.
"""

from __future__ import annotations

from pathlib import Path

STUB_OWNER = "T2"


def cleanup(data_dir: Path) -> None:
    """Deletes the traces older than ``TRACE_KEEP_DAYS``; called at helper start and at every session open."""
    raise NotImplementedError("T2: keyboard.trace")
