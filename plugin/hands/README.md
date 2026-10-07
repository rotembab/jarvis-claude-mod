# jarvis-hands

The hand-gesture helper for the Jarvis Claude Code mod. A webcam watches your hands,
MediaPipe finds them, and the helper turns poses into mouse and window actions: point
to move the cursor, pinch to click and drag, two fingers to scroll, a fist to grab a
window. The build spec is [`docs/SPEC-hands.md`](../../docs/SPEC-hands.md).

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

Exit codes: 0 OK, 1 error, 2 usage error or `JARVIS_TOKEN` missing, 3 another instance
is already running.

## Files

| Path | What |
|------|------|
| `<dataDir>/models/hands/hand_landmarker.task` | The hand model |
| `<dataDir>/hands/calibration.json` | The four-corner calibration, when you made one |
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
