"""The ``keytest`` subcommand: types a fixed line into the focused window through the keyboard's own injection path
(DESIGN-KEYBOARD.md 3.13, live check L2).

After a countdown it types ``abc ABC .,'-?/ ok`` (with ``--hebrew`` also a space and a Hebrew word), then one ``x`` and
a Backspace, through ``KeyDesktop.send_keys`` one stroke at a time, so the allow-list and the Windows injection are the
ones the air keyboard uses. Rotem looks at the window: this settles in a minute whether Unicode input reaches Windows
Terminal with Claude Code, Notepad and a browser, and whether ``vk`` is needed (D11). In the last second of the
countdown it also prints the median of 100 ``key_target()`` reads, the price of the per-character check of 3.6.1
(2 ms budget).

It types fixed text only and refuses on a desktop that does not type for real. The console gets fixed sentences, counts
and the name of the program in front (the local screen only): never an exception text, never anything the user typed.
Only ``cli.py`` imports this module, lazily inside the function that dispatches the subcommand.
"""

from __future__ import annotations

import argparse
import math
import statistics
import sys
import time
from collections.abc import Callable
from typing import Literal, TextIO

from ..clock import now as clock_now
from ..desktop.base import InputBlocked, KeyDesktop, KeyTarget, UnsupportedPlatform, as_key_desktop
from ..desktop.keys import KeyStroke
from .limits import INSERT_GAP_S

EXIT_OK, EXIT_FAILED, EXIT_REFUSED = 0, 1, 2

#: The line typed first: both cases, the marks of the allow-list and the two-key words a terminal would mangle.
KEYTEST_LINE = "abc ABC .,'-?/ ok"
#: "Shalom": three letters and a final one, to see the Hebrew path (and, in ``vk``, the fall back to Unicode).
KEYTEST_HEBREW = "שלום"

DEFAULT_COUNTDOWN_S = 5.0
MAX_COUNTDOWN_S = 60.0
#: How many ``key_target()`` reads the median is taken over, and what 3.6.1 budgets for one.
TARGET_READS = 100
TARGET_BUDGET_MS = 2.0
#: The pause between two strokes: a little over the run lane's minimum gap, so what is tested is the speed it types at.
KEY_GAP_S = INSERT_GAP_S + 0.005

_Inject = Literal["unicode", "vk"]

#: Why a target gets no keys, in words for the person at the screen.
_NO_WINDOW = "no window has the focus"
_UNREADABLE = "Windows would not say which window is in front"
_BLOCKED_WHY = {
    "own": "that is a Jarvis window",
    "elevated": "that window runs as administrator, so Windows would drop the keys",
    "shell": "that is the taskbar, Start or the task switcher",
    "none": _UNREADABLE,
}


def _seconds(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a number of seconds") from None
    if not 0.0 <= value <= MAX_COUNTDOWN_S:  # false for nan too
        raise argparse.ArgumentTypeError(f"must be between 0 and {MAX_COUNTDOWN_S:g} seconds")
    return value


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--countdown",
        type=_seconds,
        default=DEFAULT_COUNTDOWN_S,
        metavar="SECONDS",
        help=f"seconds to click into the window to test before typing starts (default {DEFAULT_COUNTDOWN_S:g})",
    )
    parser.add_argument(
        "--inject",
        choices=("unicode", "vk", "both"),
        default="unicode",
        help="how the characters are sent: as Unicode (default), as the window's own keys, or both one after the other",
    )
    parser.add_argument("--hebrew", action="store_true", help="also type a space and a Hebrew word")


def strokes_for(line: str) -> list[KeyStroke]:
    """The strokes that spell ``line``; a space is the ``space`` control. A character off the allow-list raises."""
    return [KeyStroke("control", "space") if c == " " else KeyStroke("char", c) for c in line]


def _one_run(hebrew: bool) -> list[KeyStroke]:
    line = KEYTEST_LINE + (" " + KEYTEST_HEBREW if hebrew else "") + "x"
    return [*strokes_for(line), KeyStroke("control", "backspace")]


def sequence_for(inject: str, hebrew: bool) -> list[tuple[_Inject, KeyStroke]]:
    """Every stroke with the mode it is sent in; ``both`` is the run in Unicode, a space, then the run in ``vk``."""
    modes: tuple[_Inject, ...] = ("unicode", "vk") if inject == "both" else (inject,)  # type: ignore[assignment]
    steps: list[tuple[_Inject, KeyStroke]] = []
    for index, mode in enumerate(modes):
        if index:
            steps.append((mode, KeyStroke("control", "space")))
        steps.extend((mode, stroke) for stroke in _one_run(hebrew))
    return steps


def _ascii(name: str) -> str:
    """A program name safe for any console encoding."""
    return name.encode("ascii", "replace").decode("ascii")


def _why_not(target: KeyTarget) -> str | None:
    """Why nothing may be typed there, or None; the same checks the air keyboard makes before every character."""
    if target.hwnd == 0:
        return _NO_WINDOW
    if target.blocked is not None:
        return _BLOCKED_WHY[target.blocked]
    if target.password:
        return "a password box has the focus"
    if target.covered:
        return "a full screen app or a presentation is in front"
    return None


def _report_reads(keys: KeyDesktop, clock: Callable[[], float], say: Callable[[str], None]) -> bool:
    """Print the median of ``TARGET_READS`` reads of the window in front; False when a read raised."""
    durations: list[float] = []
    try:
        for _ in range(TARGET_READS):
            started = clock()
            keys.key_target()
            durations.append(clock() - started)
    except Exception as exc:  # noqa: BLE001 - a console tool: say what failed, by type only
        say(f"Could not read the window in front ({type(exc).__name__}). Nothing was typed.")
        return False
    median_ms = statistics.median(durations) * 1000.0
    if median_ms <= TARGET_BUDGET_MS:
        verdict = f"within the {TARGET_BUDGET_MS:g} ms budget"
    else:
        verdict = f"OVER the {TARGET_BUDGET_MS:g} ms budget (the covered check would then be read from a cache)"
    say(f"key_target(): median {median_ms:.2f} ms of {TARGET_READS} reads, {verdict}.")
    return True


def run(
    args: argparse.Namespace,
    *,
    desktop: object | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = clock_now,
    out: TextIO | None = None,
) -> int:
    """Exit 0 when the line was typed, 1 when it was not or Windows stopped it, 2 on a desktop that cannot type.

    The keyword arguments are for tests: a desktop to type into instead of the real one, and the sleep, clock and
    output they replace.
    """
    stream = out if out is not None else sys.stdout

    def say(line: str) -> None:
        print(line, file=stream, flush=True)

    owned = desktop is None
    if desktop is None:
        try:
            from ..desktop import create_desktop  # not in the keyboard's import list (3.0): the CLI tool alone needs it

            desktop = create_desktop()
        except UnsupportedPlatform:
            say("keytest types into real windows and needs Windows. Nothing was typed.")
            return EXIT_REFUSED
        except Exception as exc:  # noqa: BLE001 - say what failed, by type only
            say(f"Could not open the Windows desktop ({type(exc).__name__}). Nothing was typed.")
            return EXIT_FAILED
    try:
        keys = as_key_desktop(desktop)
        if keys is None:
            say("This desktop has no keyboard calls. Nothing was typed.")
            return EXIT_REFUSED
        if not keys.injects_for_real:
            say("This desktop does not type for real (a fake run?). Nothing was typed.")
            return EXIT_REFUSED
        return _type_the_line(keys, args, say, sleep, clock)
    finally:
        if owned:
            close = getattr(desktop, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:  # noqa: BLE001 - closing must not hide the result
                    pass


def _type_the_line(
    keys: KeyDesktop,
    args: argparse.Namespace,
    say: Callable[[str], None],
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> int:
    inject: str = args.inject
    steps = sequence_for(inject, bool(args.hebrew))
    try:
        say(f"keytest: click into the window to test; typing starts in {args.countdown:g} s.")
        measured = False
        remaining = float(args.countdown)
        while remaining > 0:
            say(f"  {math.ceil(remaining)}")
            if remaining <= 1.0 and not measured:  # the last second: the window to test is in front by now
                if not _report_reads(keys, clock, say):
                    return EXIT_FAILED
                measured = True
            step = min(1.0, remaining)
            sleep(step)
            remaining -= step
        if not measured and not _report_reads(keys, clock, say):
            return EXIT_FAILED
        target = keys.key_target()
        reason = _why_not(target)
        if reason is not None:
            say(f"Nothing was typed: {reason}.")
            return EXIT_FAILED
        if keys.modifiers_down():
            say("A Ctrl, Alt or Windows key is down. Let go of it and run keytest again. Nothing was typed.")
            return EXIT_FAILED
        layout = f"0x{target.lang_id:04X}" if target.lang_id else "unknown"
        program = _ascii(target.name) or "a program with no name"
        say(f"Typing into {program} (keyboard layout {layout}), inject {inject}.")
        for index, (mode, stroke) in enumerate(steps):
            if index:
                sleep(KEY_GAP_S)
            keys.send_keys([stroke], inject=mode)
    except KeyboardInterrupt:
        say("Stopped. Any key still held was released.")
        return EXIT_FAILED
    except InputBlocked:
        say("Windows took none of the keys (the lock screen or another input block has the input). Stopped.")
        return EXIT_FAILED
    except OSError:
        say("Windows took only part of a key; it was released. Stopped.")
        return EXIT_FAILED
    except Exception as exc:  # noqa: BLE001 - say what failed, by type only
        say(f"Typing failed ({type(exc).__name__}). Stopped.")
        return EXIT_FAILED
    finally:
        try:
            keys.release_keys()
        except Exception:  # noqa: BLE001 - the contract says it never raises; a console tool does not trust that
            pass
    say("Done. Look at the window: the line below should be there once, in order, nothing missing or doubled,")
    say("and the last x should be gone again (Backspace).")
    say(f"  {KEYTEST_LINE}")
    if args.hebrew:
        say("  then a space and a Hebrew word (in vk mode a letter the layout has no key for goes in as Unicode)")
    if inject == "both":
        say("Both modes ran: Unicode first, then a space, then vk. Compare the two.")
    return EXIT_OK
