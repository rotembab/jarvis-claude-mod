# Jarvis hands: build spec (hand-gesture control)

The engineering contract for hand control: a webcam watches your hands and Jarvis turns what they do into mouse and window actions, Tony Stark style. The product plan is the "Hands" phase in [PLAN.md](PLAN.md); this file is what the code builds against.

## Goal of the first version

On Rotem's Windows 11 PC (i7-13700F, RTX 4070 Ti, a UGREEN USB webcam, one or more monitors, a projector later), inside the same Claude Code session that runs Jarvis's voice:

1. `/jarvis setup hands` installs the hand helper (its own Python 3.12 venv, about 500 MB) and downloads the MediaPipe hand model (8 MB).
2. `/jarvis hands on` (or "Jarvis, turn on hand control") starts it. The webcam light comes on; nothing leaves the PC.
3. Hold an open palm toward the camera for half a second: the reticle appears and the cursor follows your hand.
4. Pinch thumb and index to click; pinch and move to drag (files, text, a window by its title bar); pinch twice quickly to double-click; pinch thumb and middle finger to right-click.
5. Hold up two fingers (index and middle) and move the hand up or down to scroll.
6. Make a fist over a window to grab it and move it; bring the other hand in as a fist too and pull them apart or together to resize it; fling it left or right to throw it onto the next display (the projector), up to maximize, down to minimize.
7. Drop your hand out of view, or touch the real mouse, and control lets go at once.

Out of scope for this version: the projector wall mode (camera aimed at the projection), touch on the wall, depth cameras, macOS. The code keeps room for them (see Later).

## Repository layout

```text
plugin/hands/                     the hand helper: uv project "jarvis-hands", package jarvis_hands (src layout)
  pyproject.toml, uv.lock         mediapipe==1.1.0, numpy, opencv-contrib-python; dev: pytest, jsonschema
  src/jarvis_hands/
    __init__.py                   __version__ (the helper versions on its own; the plugin version is bumped as usual)
    __main__.py                   python -m jarvis_hands
    cli.py                        run | setup | doctor | preview
    protocol.py                   event and command shapes, validated against plugin/protocol/hands.schema.json in tests
    events.py                     JSON-lines writer on a private copy of stdout (same design as jarvis_voice.events)
    control.py                    token-protected HTTP control server on 127.0.0.1 (same guard as jarvis_voice.control)
    lifecycle.py                  heartbeat watchdog and the single-instance lock ("JarvisHands")
    logs.py                       stderr + <dataDir>/logs/hands.log
    settings.py                   HandsSettings, calibration file load/save
    geometry.py                   Point, Rect, homography helpers
    filters.py                    One Euro filter
    landmarks.py                  HandObservation, Frame, landmark indices
    poses.py                      per-hand pose classifier with hysteresis
    mapping.py                    camera -> desktop pixels across the chosen displays
    gestures.py                   the gesture state machine: frames in, actions out
    actions.py                    the action types the engine emits
    calibration.py                four-corner calibration flow
    executor.py                   applies actions to the desktop: smoothing, buttons, windows, safety
    runtime.py                    wires camera -> tracker -> engine -> executor + overlay; command handler
    models.py                     hand model download (sha256 checked)
    doctor.py                     JSON health report
    desktop/  base.py windows.py fake.py unsupported.py
    camera/   base.py opencv_camera.py fake.py
    tracker/  base.py mediapipe_tracker.py fake.py
    overlay/  base.py render.py windows.py
  tests/
plugin/protocol/hands.schema.json helper <-> mod messages for hands (authoritative)
plugin/hooks/hands.ts             the mod side: supervisor, controller, /jarvis hands, the `hands` tool
plugin/hooks/hands.test.ts
```

Separate venv (`<dataDir>/hands/venv`), separate process, separate lock: MediaPipe and OpenCV never touch the voice helper's environment, a crash in one never takes down the other, and either can be installed alone.

## Process model

Same contract as the voice helper ([SPEC-phase1.md](SPEC-phase1.md#process-model)), with these values:

- argv: `[<dataDir>\hands\venv\Scripts\python.exe, "-m", "jarvis_hands", "run", "--data-dir", <dataDir>]`, plus `--camera <spec>` when the user chose a camera.
- env: `PYTHONUNBUFFERED=1`, `PYTHONUTF8=1`, `JARVIS_TOKEN`, `JARVIS_PARENT=claude-code`, `NO_PROXY` with the loopback hosts, and `OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS=0` (fast camera open on Windows).
- The mod starts it at session start only when hand control is on (`/jarvis hands on` stores that; off by default, since it turns the camera on), and only in a local session on a local surface.
- First stdout line `hello` with the port; heartbeats every 2 s; the helper exits 15 s after the last one (60 s before the first).
- Single instance per user: named mutex `Local\JarvisHands` on Windows, a flock'd file elsewhere. Exit code 3 and an `already_running` error when it is taken.
- Restart policy: 1, 2, 5, 10 s, then give up and show the error; `/jarvis hands restart` tries again.
- Exit always releases any mouse button the helper holds down (finally block and atexit).

## Coordinates and units

- **Camera space**: the frame is mirrored (flipped left-right) before tracking, so it reads like a mirror: moving your right hand to your right moves it right in the image. Normalized `x, y` in `[0, 1]`, origin top-left. MediaPipe's handedness then names the user's real hand.
- **World landmarks**: metres, from MediaPipe's `hand_world_landmarks`. All pose features use these, so they hold whatever the distance to the camera.
- **Desktop space**: physical pixels in Windows' virtual-screen coordinates. The helper makes itself per-monitor DPI aware (v2) before any other Windows call, so every rect and cursor position is in physical pixels, and monitors left of or above the primary have negative coordinates.
- **Time**: seconds from `time.monotonic()`; the engine and executor take `now` as an argument so tests drive the clock.

## Hand data

```python
@dataclass(frozen=True)
class HandObservation:
    handedness: Literal["left", "right"]   # the user's hand (mirrored frame)
    score: float                            # handedness confidence 0..1
    image: np.ndarray                       # (21, 3) float: normalized x, y in the mirrored frame, z relative depth
    world: np.ndarray                       # (21, 3) float: metres, hand-centred

@dataclass(frozen=True)
class Frame:
    t: float                                # capture time, monotonic seconds
    hands: tuple[HandObservation, ...]      # 0..2 hands
    width: int                              # camera frame size in pixels (for aspect)
    height: int
```

Landmark indices are MediaPipe's: 0 wrist; thumb 1-4; index 5-8; middle 9-12; ring 13-16; pinky 17-20 (MCP, PIP, DIP, TIP for fingers).

## Poses (poses.py)

Per hand, per frame, from world landmarks, with per-hand hysteresis state (a `PoseTracker` keyed by track id):

- palm size `s = |w0 - w9|` (wrist to middle MCP; about 9 cm).
- finger reach `r_f = |tip - w0| / |mcp - w0|` for index (8/5), middle (12/9), ring (16/13), pinky (20/17). Measured on MediaPipe's test images: straight fingers 1.6 to 2.0, curled 0.7 to 1.05.
  - extended: enters above 1.40, leaves below 1.25.
  - curled: enters below 1.10, leaves above 1.20.
- index pinch `p_i = |w4 - w8| / s`: closes below 0.25, opens above 0.40, and only while the index is not curled (a fist brings the thumb near the index too: 0.36 on the test fist).
- middle pinch `p_m = |w4 - w12| / s`: same thresholds, only while the middle finger is not curled and the index pinch is open (in a pointing pose the thumb rests on the curled middle finger).

The pose, in priority order: `pinch` (index pinch closed), `pinch_middle`, `fist` (all four fingers curled), `two` (index and middle extended, ring and pinky curled), `palm` (all four extended, index pinch open), else `hover`.

The **anchor**, the one point that drives the cursor, is the mean of the index and middle knuckles (image landmarks 5 and 9). It barely moves when you pinch, curl into a fist or raise two fingers, which is what keeps clicks from drifting. Setting `anchor: index` uses the index fingertip instead.

## Mapping (mapping.py)

1. **Calibration** maps camera space to target space `[0, 1]^2` with a homography `H` (3x3). The default is the box `x 0.20..0.80, y 0.20..0.70` of the mirrored frame (`u = (x - 0.2) / 0.6`, `v = (y - 0.2) / 0.5`). `/jarvis hands calibrate` replaces it with one fitted to the four corners you show (cv2.getPerspectiveTransform). The default box is small on purpose: less reach, less "gorilla arm".
2. **Target region**: the bounding rect of the chosen displays (default all displays except ones whose adapter or monitor name says it is virtual; `/jarvis hands display <n|all>` chooses). `p = region.origin + (u * region.w, v * region.h)`.
3. **Clamp** `p` into the nearest chosen display (gaps between displays of different heights). Points past the edges clamp to the edge, so overshooting reaches the taskbar and screen corners.
4. **Filter** `p` with a One Euro filter in desktop pixels (`min_cutoff 1.0 Hz`, `beta 0.004`, `d_cutoff 1.0 Hz`, tuned on the PC), then a 1 px dead zone.

Desk mode spans the displays as Windows arranges them, so a projector set up to the right of the monitor is reached by moving your hand right, and windows thrown right land on it. Wall mode (later) keeps a separate calibration per display, keyed by its device name.

## Gestures (gestures.py)

`GestureEngine.update(frame: Frame, now: float) -> list[Action]`, pure apart from its own state. The engine also exposes `view()` for the overlay and the HUD: state, reticle position, pinch closeness 0..1, engage progress 0..1.

A raw pose must hold for 2 frames (`confirm_frames`) before the engine acts on it; this applies to entering and leaving every pose.

### Engagement

- **Disengaged**: the camera runs, nothing moves. The overlay shows a faint ring under a visible hand.
- **Engage**: an open `palm`, moving less than 0.3 frame widths per second, held for `engage_s` = 0.5 s. That hand becomes the pointer hand. Setting `engage: always` skips this (any hand engages at once), for a projector room.
- **Pointer hand tracking**: the observation whose anchor is nearest the pointer's last anchor (within 0.25), else the one with the same handedness. The other hand, if any, is the helper hand.
- **Hand lost**: for `hold_s` = 0.25 s nothing changes (a dropped frame must not end a drag). After that every held button is released and any window grab ends. After `lost_s` = 1.5 s without the pointer hand the engine disengages.
- **Real mouse**: when the executor sees the cursor where it did not put it, the engine releases everything and disengages (`engine.on_user_input()`). Touching the mouse always wins.
- **Commands**: `engage` and `disengage` from the mod (a voice request) work from any state; `engage` still needs a hand to follow.

### While engaged

| Pose | Gesture | Actions |
| --- | --- | --- |
| `hover`, `palm` | point | `MoveCursor(p)` every frame |
| `pinch` | left press | Freeze the cursor at its position 70 ms before the pinch began (the "rewind", undoing the pinch's own drift), then `Button(left, down)`. While the anchor stays within `slop` = 0.015 (camera units) of where the pinch started, the cursor stays frozen; past it, drag: the cursor follows again. Release: `Button(left, up)`. |
| `pinch` x2 | double-click | A press that starts within the system double-click time of the last click and within 3x the double-click rectangle of it is pressed at the last click's point, so Windows sees a double-click. |
| `pinch_middle` | right press | Same as the left press, with the right button. |
| `two` | scroll | The cursor freezes; the anchor's vertical motion in desktop pixels scrolls the content with the hand (`Scroll(dy=+k*Δy_px)` in wheel units, k = 2.4 per px times `scroll_speed`, sub-notch deltas allowed and accumulated), horizontal likewise. |
| `fist` | grab | `GrabWindow(p)`: the executor picks the window under the cursor (never the desktop, taskbar or the overlay). Moving drags it: `DragWindow(p)`. |
| `fist` + helper hand `fist` | two-hand resize | `ResizeWindow(a, b)`: the midpoint between the hands moves the window, the change in their horizontal and vertical separation (in desktop pixels) changes its width and height. Ends when either hand opens. |
| `fist` released fast | throw | On release, the cursor's speed over the last 120 ms: above 1.5 display widths per second, mostly sideways, throws the window to the next display that way (or snaps it to that half of its display when there is none); up maximizes, down minimizes: `ThrowWindow(direction)`. Otherwise `ReleaseWindow()`. |

Latches: after a grab ends, a new grab needs the fist opened first; after a pinch release, the next press needs the pinch opened first (hysteresis already gives this).

### Calibration flow (calibration.py)

`/jarvis hands calibrate` puts the engine in calibration: the overlay draws a target in one corner of the target region at a time (top-left, top-right, bottom-right, bottom-left); you hold an open palm with your knuckles where that corner should be, still for 1 s; the engine records the anchor and moves on. After the fourth it fits `H`, saves `<dataDir>/hands/calibration.json`, and emits `calibration` events for each step. Any command `calibrate {action: cancel}`, or 30 s without progress, cancels and keeps the old mapping. The mod speaks each step through the voice helper when it runs.

## Actions (actions.py)

Plain frozen dataclasses, applied in order by the executor:

```text
MoveCursor(x, y)                         desktop px
Button(button: left|right, down: bool, x, y)   the cursor is set to (x, y) first
Scroll(dy: float, dx: float, x, y)        wheel units (120 = one notch); positive dy scrolls up
GrabWindow(x, y) / DragWindow(x, y) / ReleaseWindow()
ResizeWindow(ax, ay, bx, by)             both hands' points; the first one starts the resize
ThrowWindow(direction: left|right|up|down)
ReleaseAll()                             buttons up, grab ended (hand lost, disengage, shutdown)
```

## Executor (executor.py)

- Runs on its own thread at 120 Hz. Cursor moves are interpolated linearly from the previous target to the new one over one camera frame interval, so a 30 fps camera still gives a smooth cursor (costs one frame of latency).
- Every action carrying a point first moves the cursor exactly there.
- Before each cursor move it reads the cursor; if it is more than 6 px from where the executor last put it, the user moved the real mouse: release everything and call `on_user_input`.
- It never leaves a button down: `ReleaseAll` on disengage, on hand loss, on shutdown, on any exception in the loop, and from an atexit hook.
- Windows: on grab it records the window and its visible rect (DWM extended frame bounds); a maximized window is restored first and placed so the grab point keeps its relative position across the title bar, as Windows does. Each drag sets the rect with SetWindowPos (no activation, no z-order change other than raising it once at grab). Minimum size 240 x 160; resize clamps to the union of the chosen displays' work areas. A window it cannot move (an administrator's window; UIPI) gives one non-fatal `input_blocked` error per window, and the grab becomes a no-op.

## Desktop backend (desktop/)

```python
class Desktop(Protocol):
    def displays(self) -> list[Display]            # id, name, rect, work, primary, virtual
    def cursor(self) -> tuple[int, int]
    def move_cursor(self, x: int, y: int) -> None
    def button(self, button: str, down: bool) -> None
    def scroll(self, dy: int, dx: int = 0) -> None # wheel units
    def window_at(self, x: int, y: int) -> Window | None
    def window_rect(self, window: Window) -> Rect   # the visible frame
    def window_state(self, window: Window) -> Literal["normal", "maximized", "minimized"]
    def set_window_rect(self, window: Window, rect: Rect) -> None   # raises InputBlocked when Windows refuses
    def raise_window(self, window: Window) -> None
    def restore(self, window: Window) -> None
    def maximize(self, window: Window) -> None
    def minimize(self, window: Window) -> None
    def double_click(self) -> tuple[float, int, int]   # seconds, width, height
    def close(self) -> None
```

`windows.py` implements it with ctypes only (no pywin32). `fake.py` keeps displays, windows and a log of calls for tests. `unsupported.py` (macOS, Linux) raises `UnsupportedPlatform` at construction; the helper then reports `unsupported_platform`.

## Camera and tracker

- `camera/opencv_camera.py`: `cv2.VideoCapture` on Media Foundation (DirectShow as fallback), MJPG, 1280 x 720 at 30 fps requested; a reader thread keeps only the newest frame. `--camera` takes an index or part of a camera's name. Errors become `camera_blocked` (Windows privacy switch), `camera_in_use`, `no_camera` or `camera_lost` with a hint.
- `tracker/mediapipe_tracker.py`: `HandLandmarker` in VIDEO mode, two hands, detection confidence 0.6, presence and tracking 0.5. It mirrors the frame, converts BGR to RGB and returns `HandObservation`s. Closed explicitly on exit.
- Fakes replay scripted frames so the whole runtime runs in tests without a camera or a model.

## Overlay (overlay/)

A small click-through, always-on-top window that follows the cursor and draws the reticle: a thin cyan ring when tracking, an arc that closes as you pinch, a filled core on press, an amber bracket ring while grabbing, chevrons while scrolling, the engage progress arc, and the calibration targets. `render.py` draws into a premultiplied BGRA array (pure, tested on every OS); `windows.py` shows it with a layered window (`UpdateLayeredWindow`) on its own thread. If the overlay fails to start, hand control carries on without it and says so once (`overlay_failed`).

## Protocol (plugin/protocol/hands.schema.json is authoritative)

Every message carries `"v": 1` and `"type"`.

Events (helper -> mod, stdout lines):

| type | fields | when |
| --- | --- | --- |
| `hello` | `port, pid, platform, version, capabilities` | first line |
| `state` | `state`: `starting`, `idle`, `active`, `paused`, `calibrating`, `error` | on change |
| `ready` | `camera, width, height, fps, displays[]` | camera and model up |
| `gesture` | `name` | engage, disengage, click, double_click, right_click, drag_start, drag_end, scroll_start, grab, release, throw_left, throw_right, throw_up, throw_down, resize_start, user_input (at most 10 per second) |
| `calibration` | `step`: `top_left`, `top_right`, `bottom_right`, `bottom_left`, `done`, `cancelled` | calibration progress |
| `error` | `code, message, hint?, fatal` | see codes |

Error codes: `already_running`, `unsupported_platform`, `no_camera`, `camera_blocked`, `camera_in_use`, `camera_lost`, `model_missing`, `tracker_failed`, `input_blocked`, `overlay_failed`, `bad_request`, `unauthorized`, `internal`.

Commands (mod -> helper, `POST /v1/<name>`):

| name | body | does |
| --- | --- | --- |
| `heartbeat` | `{}` | keeps it alive |
| `status` | `{}` | state, camera, fps, inference ms, engaged, displays, settings |
| `config` | `{engage?, displays?, hand?, anchor?, overlay?, scrollSpeed?}` | live settings |
| `pause` / `resume` | `{}` | release / reopen the camera |
| `engage` / `disengage` | `{}` | voice control of engagement |
| `calibrate` | `{action: "start" \| "cancel"}` | calibration |
| `shutdown` | `{}` | exit cleanly |

## The mod side (plugin/hooks/hands.ts)

- `HandsHelper`: the supervisor (spawn, hello, heartbeats, backoff, stop), reusing helper.ts's `LineReader` and timing constants.
- `Hands`: on/off kept in `$.store` (`handsEnabled`), the chosen camera, engage mode and displays; starts the helper at session start when on and installed; turns events into the `hands` part of the view and the status line (`JARVIS · ready · … · hands active`).
- `/jarvis hands [on|off|status|calibrate|display <n|all>|engage <palm|always>|restart|setup]`, and `/jarvis setup hands`.
- A model tool `hands` (`mcp__jarvis__hands`, input `{action: on|off|status|calibrate|pause|resume, display?}`) so "Jarvis, turn on hand control" works by voice or typing.
- Setup: `uv sync --project <plugin>/hands --frozen --no-dev --no-editable --reinstall-package jarvis-hands` with `UV_PROJECT_ENVIRONMENT=<dataDir>\hands\venv`, then `python -m jarvis_hands setup --data-dir <dataDir>` for the model (JSON progress lines, as the voice setup).

## Tests

- Python, no camera or model needed: poses on built hands, filters, mapping, every gesture in the table as scripted frames, the executor against the fake desktop (interpolation, button safety, real-mouse override, window rects, throw), calibration, render, protocol against the schema, the control server's guards, and `python -m jarvis_hands run --fake` driven over HTTP.
- With the real model (skipped unless `JARVIS_HANDS_MODELS_DIR` is set; CI sets it): poses on MediaPipe's own test photos (fist, pointing up, victory, open hands).
- Windows only: the ctypes structure sizes, monitor enumeration and a cursor round trip.
- Mod: `hands.test.ts` with the fake child and HTTP like `helper.test.ts`.
- CI: a `hands` job on Windows, macOS and Ubuntu.

## Later

- **Projector wall mode**: the camera faces the projected image; calibration projects ArUco markers and fits the camera-to-projector homography automatically, per display; pointing at the wall moves the cursor on the projector; pinch clicks.
- **Touch on the wall**: needs depth (an Intel RealSense, Orbbec or OAK-D camera) or an IR rig; a plain webcam cannot tell touching from hovering reliably.
- **macOS**: the desktop backend through Quartz (CGEvent) and the Accessibility API, camera permission tied to the terminal or the Claude app.
- Voice and hands together: "put this on the projector" while pointing at a window.
