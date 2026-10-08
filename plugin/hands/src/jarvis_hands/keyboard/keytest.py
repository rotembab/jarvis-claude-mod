"""The ``keytest`` subcommand: types a fixed line into the focused window through the keyboard's own injection path
(DESIGN-KEYBOARD.md 3.13).

STUB (T3). Track T3 replaces this file; the names below are the contract. Only ``cli.py`` imports this module,
lazily inside the function that dispatches the subcommand.
"""

from __future__ import annotations

import argparse

STUB_OWNER = "T3"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    raise NotImplementedError("T3: keyboard.keytest")


def run(args: argparse.Namespace) -> int:
    raise NotImplementedError("T3: keyboard.keytest")
