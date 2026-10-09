# jarvis-hands

The hand-gesture helper for the Jarvis Claude Code mod. A webcam watches your hands,
MediaPipe finds them, and the helper turns poses into mouse and window actions: point
to move the cursor, pinch to click and drag, two fingers to scroll, a fist to grab a
window. The build spec is [`docs/SPEC-hands.md`](../../docs/SPEC-hands.md).

It also has an air keyboard: an on-screen keyboard you type on in the air, off until the
`handKeyboard` plugin option is on. The guide is [`docs/HANDS-KEYBOARD.md`](../../docs/HANDS-KEYBOARD.md),
and its last half is the map of the code for people changing it.

It runs as its own process with its own venv (`<dataDir>/hands/venv`), separate from the
voice helper, and talks to the mod the same way: JSON events on stdout, commands over a
token-protected HTTP server on 127.0.0.1. The contract is
[`../protocol/hands.schema.json`](../protocol/hands.schema.json). Nothing leaves the PC.

## Commands

```
python -m jarvis_hands run     [--data-dir D] [--camera INDEX|NAME] [--width 1280] [--height 720] [--fps 30]
                               [--no-overlay] [--instance-name N] [--log-level LEVEL]
                               [--fake] [--fake-script FRAMES.jsonl]
python -m jarvis_hands setup   [--data-dir D]
python -m jarvis_hands doctor  [--data-dir D] [--no-camera]
python -m jarvis_hands preview [--data-dir D] [--camera INDEX|NAME]
python -m jarvis_hands keytest   [--countdown SECONDS] [--inject unicode|vk|both] [--hebrew]
python -m jarvis_hands keytrace  --yes-record --out FILE.npz [--segments KIND:SECONDS,... | --seconds N]
                                 [--press air|pinch] [--camera INDEX|NAME] [--countdown SECONDS] [--data-dir D]
python -m jarvis_hands keyreplay FILE.npz [--press air|pinch] [--set NAME=VALUE]... [--csv OUT.csv]
                                 [--write] [--no-suggest] [--data-dir D]
```

- **run**: the helper the mod starts (`/jarvis hands on`).
  - stdout carries one ASCII JSON event per line; `hello` (with the control port) comes
    first. The helper keeps a private copy of the stdout descriptor for these lines and
    points descriptor 1 at stderr, so MediaPipe's and OpenCV's native output cannot get in.
  - Commands arrive as `POST http://127.0.0.1:<port>/v1/<command>` with
    `Authorization: Bearer $JARVIS_TOKEN`. Requests carrying an `Origin` header or an
    unexpected `Host` are refused; bodies are capped at 64 KB.
  - It exits 15 s after the last `heartbeat` (60 s grace before the first, and a fresh
    15 s after the PC wakes from sleep), on `shutdown`, or when stdout closes. Every exit
    releases any mouse button it holds.
  - `--fake` runs a scripted camera, tracker and desktop: no hardware, for tests.
- **setup**: downloads the MediaPipe hand model (7.8 MB, SHA-256 pinned) to
  `<dataDir>/models/hands/hand_landmarker.task`. stdout gets JSON progress lines
  (`download`, `verify`, then `done` or `error`), as the voice setup does.
- **doctor**: prints a JSON health report; `--no-camera` leaves the camera off.
- **preview**: a window with the camera picture and the tracked hands, for checking the
  camera and lighting.
- **keytest**, **keytrace**, **keyreplay**: console tools for the air keyboard, run by hand
  on the PC (the helper does not start them).
  - `keytest` types a fixed line (`abc ABC .,'-?/ ok`, and a Hebrew word with `--hebrew`)
    into the window in front after a countdown, to check that Windows takes the keys, and
    times the window check. It types only on a desktop that types for real.
  - `keytrace` records hand landmarks and times to an `.npz` file, never a picture or a key.
    It needs `--yes-record`, opens the camera itself (pause hand control first) and needs
    the hand model.
  - `keyreplay` replays such a file (or a practice's own trace) offline and prints the
    report and the decision rule; `--write` merges clamped suggestions into
    `keyboard-tuning.json`. It needs no camera and no model.
  - Details and exact use: [`docs/HANDS-KEYBOARD.md`](../../docs/HANDS-KEYBOARD.md#try-it-on-your-pc).

Exit codes: 0 OK, 1 error, 2 usage error or `JARVIS_TOKEN` missing, 3 another instance
is already running.

## Files

| Path | What |
|------|------|
| `<dataDir>/models/hands/hand_landmarker.task` | The hand model |
| `<dataDir>/hands/calibration.json` | The four-corner calibration, when you made one |
| `<dataDir>/hands/keyboard-practice-air.json`, `keyboard-practice.json` | The air keyboard's practice markers (numbers only), written by a completed practice |
| `<dataDir>/hands/keyboard-trace-<time>.npz`, `keyboard-practice.jsonl` | A practice's landmark trace (kept 14 days) and tap log (1 MB, three copies). Practice only; a live keyboard session writes neither |
| `<dataDir>/hands/keyboard-tuning.json` | Accuracy numbers `keyreplay --write` may set. Read at every keyboard open, clamped, never holds a safety rule |
| `<dataDir>/logs/hands.log` | Log (1 MB, three old copies kept) |
| `<dataDir>/run/JarvisHands.lock` | Single-instance lock on macOS and Linux (`Local\JarvisHands` mutex on Windows) |

## Environment

| Variable | Purpose |
|----------|---------|
| `JARVIS_TOKEN` | Bearer token for the control server. Required by `run`; `--fake` uses a fixed test token when it is unset. |
| `JARVIS_HANDS_CAMERA` | Camera index, or part of its name. Default: the first camera. |
| `JARVIS_HANDS_INSTANCE_NAME` | Name of the single-instance lock. Default `JarvisHands`. |
| `JARVIS_DATA_DIR` | Data directory. Default `~/.jarvis`. |
| `JARVIS_LOG_LEVEL` | Log level. Default `INFO`. |
| `HTTPS_PROXY` | Used by `setup` for the model download. |

## Development

```
uv sync                      # dev group included
uv run pytest                # no camera, GPU, display or network needed
uvx ruff check src tests && uvx ruff format --check src tests
```

The air keyboard's tests are `tests/test_kb_*.py` (`uv run pytest -k test_kb_`). They run
against fakes: nothing has run against a real Windows `SendInput`, overlay window or camera.
`KB_FULL=1` adds the long statistical rows, `KB_FROZEN_BASE=<commit>` turns on the check that
the pointer files are unchanged since that commit, and `JARVIS_HANDS_MODELS_DIR` the
real-model tests. The `hello` event lists `keyboard` among its capabilities.
