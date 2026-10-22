"""The safety constants of ``keyboard/limits.py``: every value pinned, the relations, the floors no edit may loosen.

U18 and U49 (every value of DESIGN 3.2), X30 and P44 (the relations), P12 (the file is code only).
A changed constant fails here and needs a deliberate edit of this file and of the design.
"""

from __future__ import annotations

import ast
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jarvis_hands.keyboard import limits

PINNED: dict[str, Any] = {
    # the first version's constants
    "MIN_GAP_S": 0.06,
    "QUEUE_MAX": 3,
    "QUEUE_AGE_S": 0.30,
    "STORM_N": 12,
    "STORM_S": 2.0,
    "BACKSTOP_N": 16,
    "BACKSTOP_S": 2.0,
    "YIELD_S": 1.5,
    "FOCUS_SETTLE_S": 0.5,
    "SINK_WARMUP_S": 0.3,
    "ENTER_CONFIRM_S": 1.5,
    "SHIFT_S": 5.0,
    "FIST_EXIT_S": 1.0,
    "NO_KEY_CLOSE_S": 300,
    "PLACE_TIMEOUT_S": 30,
    "ARM_TIMEOUT_S": 90,
    "GAP_RESET_S": 0.25,
    "SLOW_ENTER_S": 0.100,
    "SLOW_EXIT_S": 0.083,
    "HEALTH_MAX_AGE_S": 0.75,
    "HEALTH_CLOSE_S": 2.0,
    "SEND_SLOW_S": 0.25,
    "SEND_FAIL_CLOSE": 3,
    "FOREIGN_FAIL_CLOSE": 10,
    "QUARANTINE_CLEAR_S": 0.6,
    "ECHO_CHARS": 24,
    "PRACTICE_MIN_REST_S": 20,
    "PRACTICE_MAX_PHANTOMS_PER_MIN": 1.0,
    "TRACE_KEEP_DAYS": 14,
    # the air tap: the detector's defences (no file can relax them)
    "AIR_MIN_VISIBLE_S": 0.35,
    "AIR_MIN_SAMPLES": 8,
    "AIR_MIN_SCORE": 0.6,
    "AIR_MIN_POSTURE_LIFT": 0.35,
    "AIR_POSTURE_FINGERS": 3,
    "AIR_COHERENCE_N": 3,
    "AIR_COHERENCE_RATIO": 1.3,
    "AIR_COHERENCE_PEER": 0.5,
    "AIR_COHERENCE_HOLD_S": 0.30,
    "AIR_COH_BACK_S": 0.35,
    "AIR_REFRACTORY_S": 0.12,
    "AIR_HAND_EXCL_S": 0.06,
    "AIR_TREMOR_N": 5,
    "AIR_TREMOR_WINDOW_S": 0.5,
    "AIR_TREMOR_HOLD_S": 0.6,
    "AIR_JUMP_FW": 0.06,
    "AIR_JUMP_HOLD_S": 0.30,
    "AIR_PINCH_GAP": 0.30,
    "AIR_SETTLE_FRAMES": 2,
    "AIR_FLASH_S": 0.18,
    "AIR_SIGMA_FLOOR": 0.008,
    "AIR_THETA_FLOOR": 0.07,
    "AIR_THETA_K_FLOOR": 3.5,
    "AIR_VETO_FLOOR": 0.5,
    "AIR_LEVEL_FPS_DEGRADED": 26,
    "AIR_LEVEL_FPS_OFF": 13,
    "AIR_LEVEL_FPS_S": 2.0,
    "AIR_LEVEL_NOISE_S": 3.0,
    "AIR_LEVEL_RECOVER_S": 8.0,
    "AIR_LEVEL_NOISE_DEGRADED": 0.022,
    "AIR_LEVEL_NOISE_OFF": 0.036,
    "AIR_LEVEL_GAPS_DEGRADED": 3,
    "AIR_THETA_MULT_DEGRADED": 1.3,
    "AIR_PRACTICE_MAX_PHANTOMS_PER_MIN": 3.0,
    "AIR_PRACTICE_MIN_DRILL_RECALL": 0.70,
    "AIR_PRACTICE_MIN_DRILL_PROMPTS": 24,
    "AIR_PRACTICE_MIN_REST_S": 60,
    "AIR_REST_S": 35,
    "AIR_TALK_S": 30,
    "AIR_DRILL_GAP_S": 1.2,
    "AIR_DRILL_PER_FINGER": 6,
    "AIR_DRILL_MOVE_EVERY": 2,
    "AIR_DRILL_MOVE_U": (3.0, 4.0),
    "AIR_DRILL_MOVE_ROWS": (-1, 0, 1),
    "AIR_DRILL_PER_REACH_KEY": 3,
    "AIR_DRILL_REACH_KEYS": (
        ("backspace", "right", "index"),
        ("insert", "right", "pinky"),
        ("clear", "left", "pinky"),
        ("enter", "left", "ring"),
    ),
    "AIR_WARMUP_MIN_MARGIN": 0.5,
    "AIR_WARMUP_MIN_DEPTH": 0.10,
    "AIR_WARMUP_GAP_S": 1.0,
    "AIR_WARMUP_CLEAN_S": 1.0,
    "AIR_WARMUP_AIM_TOL": 0.6,
    "AIR_WARMUP_STRAY_LIMIT": 3,
    "AIR_WARMUP_ORDER": (
        ("right", 0),
        ("left", 0),
        ("right", 1),
        ("left", 1),
        ("right", 2),
        ("left", 2),
        ("right", 3),
        ("left", 3),
    ),
    # review mode: the box, the guards and the run
    "COMPOSE_MAX": 200,
    "INSERT_TAPS": 3,
    "GUARD_MIN_S": 0.25,
    "GUARD_MAX_S": 6.0,
    "GUARD_STILL_SPEED": 0.10,
    "GUARD_FIRM_CONF": 0.8,
    "GUARD_FIRM_TAPS": 2,
    "INSERT_GAP_S": 0.030,
    "INSERT_BACKSTOP_N": 80,
    "INSERT_BACKSTOP_S": 2.0,
    "RUN_COOLDOWN_S": 0.5,
    "RUN_MAX_PER_MIN": 12,
    "RUN_MAX_CHARS_PER_MIN": 600,
    "INSERT_MAX_S": 30.0,
    "STOP_ARM_S": 0.5,
    "SEND_TAPS": 3,
    "SEND_WINDOW_S": 10.0,
    "ABORT_SHOW_S": 8.0,
    "STORM_FREEZE_S": 3.0,
    "REVIEW_IDLE_S": 120,
    "IDLE_WARN_S": 20,
    "SEND_REFUSE_FIRST": ("/", "!"),
}


def public_names() -> set[str]:
    return {name for name in vars(limits) if name.isupper()}


@pytest.mark.parametrize(("name", "value"), PINNED.items(), ids=list(PINNED))
def test_every_constant_has_its_pinned_value_and_type(name: str, value: Any) -> None:
    actual = getattr(limits, name)
    assert actual == value
    assert type(actual) is type(value)  # an int stays an int, a float a float, a tuple a tuple


def test_no_constant_is_missing_from_this_table_or_from_the_module() -> None:
    """A new constant needs a deliberate line here (and in the design); a pinned one cannot vanish."""
    assert public_names() == set(PINNED)


# --------------------------------------------------------------------------- the relations and the floors


def violations(c: Any) -> list[str]:
    """The relations of DESIGN 3.2, over any object with the constants as attributes. Empty when all hold."""
    checks: dict[str, bool] = {
        "BACKSTOP_N >= STORM_N + 4": c.BACKSTOP_N >= c.STORM_N + 4,
        "BACKSTOP_S >= STORM_S": c.BACKSTOP_S >= c.STORM_S,
        "INSERT_BACKSTOP_N >= ceil(INSERT_BACKSTOP_S / INSERT_GAP_S) + 8": (
            math.ceil(c.INSERT_BACKSTOP_S / c.INSERT_GAP_S) + 8 <= c.INSERT_BACKSTOP_N
        ),
        "COMPOSE_MAX * INSERT_GAP_S * 4 <= INSERT_MAX_S": c.COMPOSE_MAX * c.INSERT_GAP_S * 4 <= c.INSERT_MAX_S,
        "(INSERT_TAPS - 1) * GUARD_MIN_S < GUARD_MAX_S": (c.INSERT_TAPS - 1) * c.GUARD_MIN_S < c.GUARD_MAX_S,
        "(SEND_TAPS - 1) * GUARD_MIN_S < GUARD_MAX_S": (c.SEND_TAPS - 1) * c.GUARD_MIN_S < c.GUARD_MAX_S,
        "ENTER_CONFIRM_S > GUARD_MIN_S": c.ENTER_CONFIRM_S > c.GUARD_MIN_S,
        # the floors and ceilings that no edit may loosen (7.4)
        "INSERT_TAPS >= 3": c.INSERT_TAPS >= 3,
        "SEND_TAPS >= 3": c.SEND_TAPS >= 3,
        "0.20 <= GUARD_MIN_S <= 0.40": 0.20 <= c.GUARD_MIN_S <= 0.40,
        "GUARD_MAX_S <= 6.0": c.GUARD_MAX_S <= 6.0,
        "0.05 <= GUARD_STILL_SPEED <= 0.15": 0.05 <= c.GUARD_STILL_SPEED <= 0.15,
        "0.75 <= GUARD_FIRM_CONF <= 0.9": 0.75 <= c.GUARD_FIRM_CONF <= 0.9,
        "2 <= GUARD_FIRM_TAPS <= min(INSERT_TAPS, SEND_TAPS)": (
            2 <= c.GUARD_FIRM_TAPS <= min(c.INSERT_TAPS, c.SEND_TAPS)
        ),
        "SEND_WINDOW_S <= 10.0": c.SEND_WINDOW_S <= 10.0,
        "STOP_ARM_S < INSERT_MAX_S": c.STOP_ARM_S < c.INSERT_MAX_S,
        "INSERT_BACKSTOP_N > BACKSTOP_N": c.INSERT_BACKSTOP_N > c.BACKSTOP_N,
        "COMPOSE_MAX <= RUN_MAX_CHARS_PER_MIN <= 3 * COMPOSE_MAX": (
            c.COMPOSE_MAX <= c.RUN_MAX_CHARS_PER_MIN <= 3 * c.COMPOSE_MAX
        ),
        "RUN_MAX_CHARS_PER_MIN >= INSERT_BACKSTOP_N": c.RUN_MAX_CHARS_PER_MIN >= c.INSERT_BACKSTOP_N,
        "6 <= RUN_MAX_PER_MIN <= 12": 6 <= c.RUN_MAX_PER_MIN <= 12,
        "0.25 <= RUN_COOLDOWN_S <= 1.0": 0.25 <= c.RUN_COOLDOWN_S <= 1.0,
        "AIR_LEVEL_FPS_OFF < AIR_LEVEL_FPS_DEGRADED": c.AIR_LEVEL_FPS_OFF < c.AIR_LEVEL_FPS_DEGRADED,
        "AIR_LEVEL_NOISE_DEGRADED < AIR_LEVEL_NOISE_OFF": c.AIR_LEVEL_NOISE_DEGRADED < c.AIR_LEVEL_NOISE_OFF,
        "AIR_THETA_MULT_DEGRADED >= 1.1": c.AIR_THETA_MULT_DEGRADED >= 1.1,
        "AIR_PRACTICE_MAX_PHANTOMS_PER_MIN <= 3.0": c.AIR_PRACTICE_MAX_PHANTOMS_PER_MIN <= 3.0,
        "AIR_REFRACTORY_S >= 0.10": c.AIR_REFRACTORY_S >= 0.10,
    }
    return [relation for relation, holds in checks.items() if not holds]


def test_the_relations_and_floors_hold() -> None:
    assert violations(limits) == []


def mutated(**changes: Any) -> SimpleNamespace:
    return SimpleNamespace(**{**{name: getattr(limits, name) for name in public_names()}, **changes})


@pytest.mark.parametrize(
    ("changes", "broken"),
    [
        ({"INSERT_TAPS": 2}, "INSERT_TAPS >= 3"),
        ({"INSERT_TAPS": 1}, "INSERT_TAPS >= 3"),
        ({"SEND_TAPS": 2}, "SEND_TAPS >= 3"),
        ({"GUARD_MIN_S": 0.10}, "0.20 <= GUARD_MIN_S <= 0.40"),
        ({"GUARD_MIN_S": 0.45}, "0.20 <= GUARD_MIN_S <= 0.40"),
        ({"GUARD_MAX_S": 6.5}, "GUARD_MAX_S <= 6.0"),
        ({"GUARD_STILL_SPEED": 0.20}, "0.05 <= GUARD_STILL_SPEED <= 0.15"),
        ({"GUARD_STILL_SPEED": 0.02}, "0.05 <= GUARD_STILL_SPEED <= 0.15"),
        ({"GUARD_FIRM_CONF": 0.7}, "0.75 <= GUARD_FIRM_CONF <= 0.9"),
        ({"GUARD_FIRM_CONF": 0.95}, "0.75 <= GUARD_FIRM_CONF <= 0.9"),
        ({"GUARD_FIRM_TAPS": 1}, "2 <= GUARD_FIRM_TAPS <= min(INSERT_TAPS, SEND_TAPS)"),
        ({"GUARD_FIRM_TAPS": 4}, "2 <= GUARD_FIRM_TAPS <= min(INSERT_TAPS, SEND_TAPS)"),
        ({"SEND_WINDOW_S": 10.5}, "SEND_WINDOW_S <= 10.0"),
        ({"BACKSTOP_N": 15}, "BACKSTOP_N >= STORM_N + 4"),
        ({"BACKSTOP_S": 1.0}, "BACKSTOP_S >= STORM_S"),
        ({"INSERT_BACKSTOP_N": 70}, "INSERT_BACKSTOP_N >= ceil(INSERT_BACKSTOP_S / INSERT_GAP_S) + 8"),
        ({"INSERT_BACKSTOP_N": 16}, "INSERT_BACKSTOP_N > BACKSTOP_N"),
        ({"INSERT_MAX_S": 20.0}, "COMPOSE_MAX * INSERT_GAP_S * 4 <= INSERT_MAX_S"),
        ({"STOP_ARM_S": 31.0}, "STOP_ARM_S < INSERT_MAX_S"),
        ({"ENTER_CONFIRM_S": 0.2}, "ENTER_CONFIRM_S > GUARD_MIN_S"),
        ({"RUN_MAX_CHARS_PER_MIN": 100}, "COMPOSE_MAX <= RUN_MAX_CHARS_PER_MIN <= 3 * COMPOSE_MAX"),
        ({"RUN_MAX_CHARS_PER_MIN": 601 * 2}, "COMPOSE_MAX <= RUN_MAX_CHARS_PER_MIN <= 3 * COMPOSE_MAX"),
        ({"RUN_MAX_PER_MIN": 13}, "6 <= RUN_MAX_PER_MIN <= 12"),
        ({"RUN_MAX_PER_MIN": 5}, "6 <= RUN_MAX_PER_MIN <= 12"),
        ({"RUN_COOLDOWN_S": 0.1}, "0.25 <= RUN_COOLDOWN_S <= 1.0"),
        ({"AIR_LEVEL_FPS_OFF": 30}, "AIR_LEVEL_FPS_OFF < AIR_LEVEL_FPS_DEGRADED"),
        ({"AIR_LEVEL_NOISE_OFF": 0.01}, "AIR_LEVEL_NOISE_DEGRADED < AIR_LEVEL_NOISE_OFF"),
        ({"AIR_THETA_MULT_DEGRADED": 1.0}, "AIR_THETA_MULT_DEGRADED >= 1.1"),
        ({"AIR_PRACTICE_MAX_PHANTOMS_PER_MIN": 4.0}, "AIR_PRACTICE_MAX_PHANTOMS_PER_MIN <= 3.0"),
        ({"AIR_REFRACTORY_S": 0.05}, "AIR_REFRACTORY_S >= 0.10"),
    ],
)
def test_a_loosened_constant_breaks_a_named_relation(changes: dict[str, Any], broken: str) -> None:
    """The checker is not vacuous: each floor and relation is the one that fails for the edit that loosens it."""
    assert broken in violations(mutated(**changes))


def test_the_guards_leave_room_for_their_taps_at_the_slowest_frame_rate() -> None:
    """Three taps at the minimum spacing fit the window with a margin of a tap, also at 15 fps (one frame 67 ms)."""
    frame = 1 / 15
    assert (limits.INSERT_TAPS - 1) * (limits.GUARD_MIN_S + frame) < limits.GUARD_MAX_S
    assert (limits.SEND_TAPS - 1) * (limits.GUARD_MIN_S + frame) < limits.GUARD_MAX_S


# --------------------------------------------------------------------------- the file is code only (P12, SR14)


LIMITS_SOURCE = Path(limits.__file__).read_text(encoding="utf-8")


@pytest.mark.parametrize("needle", ["open(", "json", "environ", "getenv", "argv", "import os", "import sys", "pathlib"])
def test_limits_never_reads_anything(needle: str) -> None:
    assert needle not in LIMITS_SOURCE


def test_limits_is_constants_only() -> None:
    tree = ast.parse(LIMITS_SOURCE)
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            assert node.module == "__future__"
        elif isinstance(node, ast.Expr):
            assert isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)  # the docstring
        else:
            assert isinstance(node, ast.Assign | ast.AnnAssign), ast.dump(node)[:60]
    for node in ast.walk(tree):
        assert not isinstance(node, ast.Call | ast.Lambda | ast.FunctionDef | ast.ClassDef | ast.Import), ast.dump(
            node
        )[:60]


def test_limits_has_no_setter_and_the_tuples_are_tuples() -> None:
    for name in public_names():
        assert not isinstance(getattr(limits, name), list | dict | set), name
