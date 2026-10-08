"""The ``keyreplay`` subcommand: replays a recorded trace offline and prints the report (DESIGN-KEYBOARD.md 3.13, 5.7).

STUB (T8). Track T8 replaces this file; the names below are the contract. Only ``cli.py`` imports this module,
lazily inside the function that dispatches the subcommand.
"""

from __future__ import annotations

import argparse

STUB_OWNER = "T8"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    raise NotImplementedError("T8: keyboard.keyreplay")


def run(args: argparse.Namespace) -> int:
    raise NotImplementedError("T8: keyboard.keyreplay")
