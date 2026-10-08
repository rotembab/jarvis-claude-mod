"""The safety constants of the air keyboard: code only.

Nothing loads, changes or overrides these values while the helper runs: no file, no
command, no setting of the mod. A person who can edit this file can edit the code
anyway, and a person who cannot must not be able to loosen a guard. Accuracy numbers
(which a recording may retune) live in ``tuning.py`` inside clamps; none of these is
among them. ``tests/test_kb_limits.py`` pins every value and the relations between
them, so a change here needs a deliberate edit of that test and of the design.

Times are seconds unless the name says otherwise. DESIGN-KEYBOARD.md 3.2 explains each.
"""

# ----------------------------------------------------------------------- keys and rates (direct mode, shared breakers)

#: Minimum spacing of two emitted keys.
MIN_GAP_S = 0.06
#: Pending resolved presses, and how long one waits.
QUEUE_MAX = 3
QUEUE_AGE_S = 0.30
#: Direct mode: the 12th key in 2 s closes the session (runaway).
#: Review mode: the 12th tap freezes taps for STORM_FREEZE_S.
STORM_N = 12
STORM_S = 2.0
#: The sink's independent breaker for the key lane; it sits above the storm limit so a storm trips the session first.
BACKSTOP_N = 16
BACKSTOP_S = 2.0
#: Hold after foreign input, after the foreground window changed, and the window after sink.start that only
#: re-baselines.
YIELD_S = 1.5
FOCUS_SETTLE_S = 0.5
SINK_WARMUP_S = 0.3
#: Direct mode only: the second Enter must come within this.
ENTER_CONFIRM_S = 1.5
SHIFT_S = 5.0
FIST_EXIT_S = 1.0
NO_KEY_CLOSE_S = 300
PLACE_TIMEOUT_S = 30
ARM_TIMEOUT_S = 90
GAP_RESET_S = 0.25
SLOW_ENTER_S = 0.100
SLOW_EXIT_S = 0.083
HEALTH_MAX_AGE_S = 0.75
HEALTH_CLOSE_S = 2.0
SEND_SLOW_S = 0.25
SEND_FAIL_CLOSE = 3
FOREIGN_FAIL_CLOSE = 10
QUARANTINE_CLEAR_S = 0.6
ECHO_CHARS = 24
PRACTICE_MIN_REST_S = 20
PRACTICE_MAX_PHANTOMS_PER_MIN = 1.0
TRACE_KEEP_DAYS = 14

# --------------------------------------------------------------------------- the air tap: the detector's defences

AIR_MIN_VISIBLE_S = 0.35
AIR_MIN_SAMPLES = 8
AIR_MIN_SCORE = 0.6
AIR_MIN_POSTURE_LIFT = 0.35
AIR_POSTURE_FINGERS = 3
AIR_COHERENCE_N = 3
AIR_COHERENCE_RATIO = 1.3
AIR_COHERENCE_PEER = 0.5
AIR_COHERENCE_HOLD_S = 0.30
AIR_COH_BACK_S = 0.35
AIR_REFRACTORY_S = 0.12
AIR_HAND_EXCL_S = 0.06
AIR_TREMOR_N = 5
AIR_TREMOR_WINDOW_S = 0.5
AIR_TREMOR_HOLD_S = 0.6
AIR_JUMP_FW = 0.06
AIR_JUMP_HOLD_S = 0.30
AIR_PINCH_GAP = 0.30
AIR_SETTLE_FRAMES = 2
AIR_FLASH_S = 0.18
#: No noise estimate below this: a noise-free synthetic hand must not set a threshold of zero.
AIR_SIGMA_FLOOR = 0.008
#: The floors the tuning clamps cannot cross.
AIR_THETA_FLOOR = 0.07
AIR_THETA_K_FLOOR = 3.5
AIR_VETO_FLOOR = 0.5
#: The degradation ladder: frame rate, noise and holes in the stream, and how long a reading must hold.
AIR_LEVEL_FPS_DEGRADED = 26
AIR_LEVEL_FPS_OFF = 13
AIR_LEVEL_FPS_S = 2.0
AIR_LEVEL_NOISE_S = 3.0
AIR_LEVEL_RECOVER_S = 8.0
AIR_LEVEL_NOISE_DEGRADED = 0.022
AIR_LEVEL_NOISE_OFF = 0.036
AIR_LEVEL_GAPS_DEGRADED = 3
AIR_THETA_MULT_DEGRADED = 1.3
#: The air practice marker is accepted only inside these.
AIR_PRACTICE_MAX_PHANTOMS_PER_MIN = 3.0
AIR_PRACTICE_MIN_DRILL_RECALL = 0.70
AIR_PRACTICE_MIN_DRILL_PROMPTS = 24
AIR_PRACTICE_MIN_REST_S = 60
AIR_REST_S = 35
AIR_TALK_S = 30
AIR_DRILL_GAP_S = 1.2
AIR_DRILL_PER_FINGER = 6
AIR_DRILL_MOVE_EVERY = 2
AIR_DRILL_MOVE_U = (3.0, 4.0)
AIR_DRILL_MOVE_ROWS = (-1, 0, 1)
AIR_DRILL_PER_REACH_KEY = 3
#: (key kind, side, finger) of the reach prompts: the four keys that are not under a resting fingertip.
AIR_DRILL_REACH_KEYS = (
    ("backspace", "right", "index"),
    ("insert", "right", "pinky"),
    ("clear", "left", "pinky"),
    ("enter", "left", "ring"),
)
#: The warm-up cannot be loosened by a file: these are code constants, not tuning fields.
AIR_WARMUP_MIN_MARGIN = 0.5
AIR_WARMUP_MIN_DEPTH = 0.10
AIR_WARMUP_GAP_S = 1.0
AIR_WARMUP_CLEAN_S = 1.0
AIR_WARMUP_AIM_TOL = 0.6
AIR_WARMUP_STRAY_LIMIT = 3
#: (side, finger) in the fixed order; with one hand: index, middle, ring, pinky.
AIR_WARMUP_ORDER = (
    ("right", 0),
    ("left", 0),
    ("right", 1),
    ("left", 1),
    ("right", 2),
    ("left", 2),
    ("right", 3),
    ("left", 3),
)

# ----------------------------------------------------------------------- review mode: the box, the guards and the run

#: Characters in the box and in one run; equals the schema maximum.
COMPOSE_MAX = 200
#: Taps that start a run, and taps that press Enter: floors of 3, one constant for every press method.
INSERT_TAPS = 3
SEND_TAPS = 3
#: A tap closer than GUARD_MIN_S to the last counted tap of a guard is a bounce; a guard must complete within
#: GUARD_MAX_S.
GUARD_MIN_S = 0.25
GUARD_MAX_S = 6.0
#: Minimum spacing of two characters of a run.
INSERT_GAP_S = 0.030
#: The sink's run-lane breaker: the 81st send in 2.0 s closes the session (runaway).
INSERT_BACKSTOP_N = 80
INSERT_BACKSTOP_S = 2.0
#: Minimum time from the end of one run to the start of the next, and the budgets over any 60 s.
RUN_COOLDOWN_S = 0.5
RUN_MAX_PER_MIN = 12
RUN_MAX_CHARS_PER_MIN = 600
#: A run older than this aborts; a tap on Insert stops a run only after STOP_ARM_S.
INSERT_MAX_S = 30.0
STOP_ARM_S = 0.5
#: Every Send tap must come within this long after the completed Insert (ceiling 10 s).
SEND_WINDOW_S = 10.0
ABORT_SHOW_S = 8.0
STORM_FREEZE_S = 3.0
REVIEW_IDLE_S = 120
IDLE_WARN_S = 20
#: Send is refused when the first non-space inserted character is one of these (a slash command, a shell line).
SEND_REFUSE_FIRST = ("/", "!")
