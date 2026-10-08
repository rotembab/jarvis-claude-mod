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
  pyproject.toml, uv.lock         mediapipe==0.10.33 (the newest without usage telemetry), numpy, opencv-contrib-python 5; dev: pytest, jsonschema
  src/jarvis_hands/
    __init__.py                   __version__ (the helper versions on its own; the plugin version is bumped as usual)
    __main__.py                   python -m jarvis_hands
    cli.py                        run | setup | doctor | preview
    protocol.py                   event and command shapes, validated against plugin/protocol/hands.schema.json in tests
    events.py                     JSON-lines writer on a private copy of stdout (same design as jarvis_voice.events)
    control.py                    token-protected HTTP control server on 127.0.0.1 (same guard as jarvis_voice.control)
    lifecycle.py                  heartbeat watchdog and the single-instance lock ("JarvisHands")
    logs.py                       stderr + <dataDir>/logs/hands.log
    settings.py                   HandsSettings, KNOBS (the sensitivity settings), calibration file load/save
    geometry.py                   Point, Rect, homography helpers
    filters.py                    One Euro filter
    landmarks.py                  HandObservation, Frame, landmark indices
    poses.py                      per-hand pose classifier with hysteresis; thresholds_for() applies the pinch and fist settings
    mapping.py                    camera -> desktop pixels across the chosen displays, with the cursor gain
    gestures.py                   the gesture state machine: frames in, actions out
    actions.py                    the action types the engine emits
    calibration.py                four-corner calibration flow
    executor.py                   applies actions to the desktop: smoothing, buttons, windows, safety
    runtime.py                    wires camera -> tracker -> engine -> executor + overlay; command handler
    synthetic.py                  synthetic hands (a small kinematic model) for tests and `run --fake`
    models.py                     hand model download (sha256 checked)
    doctor.py                     JSON health report
    preview.py                    camera window with the tracked hands drawn on it
    desktop/  base.py windows.py fake.py           create_desktop() in __init__
    camera/   base.py opencv_camera.py fake.py     create_camera(), list_cameras()
    tracker/  base.py mediapipe_tracker.py fake.py create_tracker()
    overlay/  base.py render.py windows.py         create_overlay() (NullOverlay off Windows)
  tests/
plugin/protocol/hands.schema.json helper <-> mod messages for hands (authoritative)
plugin/hooks/hands.ts             the mod side: supervisor, controller, /jarvis hands, the tuning table and presets, the `hands` tool
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

- **Camera space**: mirrored, so it reads like a mirror: moving your right hand to your right moves it right. The model runs on the raw camera frame, where MediaPipe's Tasks handedness is anatomical (a right hand is "Right"; flipping the frame would swap the label), and the tracker then mirrors the landmarks: image `x' = 1 - x`, world `x' = -x`. Normalized `x, y` in `[0, 1]`, origin top-left; `x` can stray slightly outside when a hand is cut by the frame edge.
- **Pose space**: image landmarks as `(x, y * height / width, z)`, all in frame widths (MediaPipe's `z` uses roughly the scale of `x`). Pose features are ratios of distances here. World landmarks (metres) are kept for reference, but their depth is poor: on MediaPipe's OK-sign photo the world thumb-index distance is 4 to 7 cm with the tips touching.
- **Desktop space**: physical pixels in Windows' virtual-screen coordinates. The helper makes itself per-monitor DPI aware (v2) before any other Windows call, so every rect and cursor position is in physical pixels, and monitors left of or above the primary have negative coordinates.
- **Time**: seconds from `jarvis_hands.clock.now` (`time.perf_counter`; on Windows, Python 3.12's `time.monotonic` moves in 15.6 ms steps, which would make 30 fps frame intervals read 31 or 47 ms). Frame times, gestures, filters and the executor all use it; the engine and executor take `now` as an argument so tests drive the clock. Plain timeouts may use `time.monotonic`.

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
    t: float                                # capture time, clock.now() seconds
    hands: tuple[HandObservation, ...]      # 0..2 hands
    width: int                              # camera frame size in pixels (for aspect)
    height: int
```

Landmark indices are MediaPipe's: 0 wrist; thumb 1-4; index 5-8; middle 9-12; ring 13-16; pinky 17-20 (MCP, PIP, DIP, TIP for fingers).

## Poses (poses.py)

Per hand, per frame, from the pose-space points `P` (see Coordinates), with per-hand hysteresis state (a `PoseTracker` keyed by track id):

- palm size `s = |P0 - P9|` (wrist to middle MCP).
- finger reach `r_f = |tip - P0| / |mcp - P0|` for index (8/5), middle (12/9), ring (16/13), pinky (20/17). Measured on MediaPipe's test photos: straight fingers 1.5 to 2.2, relaxed 1.3 to 1.4, curled 0.7 to 0.95; the OK sign's index (pinching) 1.3.
  - extended: enters above 1.40, leaves below 1.25.
  - curled: enters below 1.10, leaves above 1.20.
- index pinch `p_i = |P4 - P8| / s`: closes below 0.28, opens above 0.40, and only while the index is not curled (a fist brings the thumb near the index too: 0.20 on the test fist). The OK-sign photo gives 0.13 and 0.22; open and relaxed hands 0.39 and up.
- middle pinch `p_m = |P4 - P12| / s`: same thresholds, only while the middle finger is not curled and the index pinch is open (in a pointing pose the thumb rests on the curled middle finger).

The numbers above are the defaults, `PoseThresholds()`. The `pinch` and `fist` settings move them through `thresholds_for(pinch, fist)`, which returns exactly `PoseThresholds()` for `(1.0, 1.0)` (see [Sensitivity settings](#sensitivity-settings)).

The pose, in priority order: `pinch` (index pinch closed), `pinch_middle`, `fist` (all four fingers curled), `two` (index and middle extended, ring and pinky curled), `palm` (all four extended, index pinch open), else `hover`.

The **anchor**, the one point that drives the cursor, is the mean of the index and middle knuckles (image landmarks 5 and 9). It barely moves when you pinch, curl into a fist or raise two fingers, which is what keeps clicks from drifting. Setting `anchor: index` moves the cursor with the index fingertip instead; tracks, the drag slop and held presses still follow the knuckles, and a press lands where the fingertip pointed before the finger began to bend (looking back up to 0.8 s for where the index was straightest), plus the knuckles' motion since.

## Mapping (mapping.py)

1. **Calibration** maps camera space to target space `[0, 1]^2` with a homography `H` (3x3). The default is the box `x 0.20..0.80, y 0.20..0.70` of the mirrored frame (`u = (x - 0.2) / 0.6`, `v = (y - 0.2) / 0.5`). `/jarvis hands calibrate` replaces it with one fitted to the four corners you show (cv2.getPerspectiveTransform). The default box is small on purpose: less reach, less "gorilla arm".
2. **Target region**: the chosen displays (`/jarvis hands display <n|all>` chooses; the default is every display that is not virtual, or all of them when every one is). A display is virtual when Windows reports its output technology as `INDIRECT_VIRTUAL` (17) or its adapter or monitor name contains "virtual" or "IddSampleDriver"; USB display adapters (`INDIRECT_WIRED`) are real. The unit square spans the chosen displays laid end to end: on each axis the desktop spans no chosen display covers are taken out, so a display left out, or the gap beside a shorter display, costs no reach. `p = (xs.to_desktop(u * xs.length), ys.to_desktop(v * ys.length))`.
3. **Clamp** `p` into the nearest chosen display (gaps between displays of different heights). Points past the edges clamp to the edge, so overshooting reaches the taskbar and screen corners.
4. **Filter** `p` with a One Euro filter in desktop pixels (`min_cutoff 1.0 Hz`, `beta 0.004`, `d_cutoff 1.0 Hz`, tuned on the PC), then a 1 px dead zone. The `smoothing` setting divides `min_cutoff` and the `deadZone` setting is the dead zone's size.

The `cursorSpeed` setting is a gain on the unit square after step 1: `(uv - 0.5) * gain + 0.5`. Hand movement that scrolls the page or throws a window (relative motion) and the calibration targets do not use it.

Desk mode spans the displays as Windows arranges them, so a projector set up to the right of the monitor is reached by moving your hand right, and windows thrown right land on it. Wall mode (later) keeps a separate calibration per display, keyed by its device name.

## Gestures (gestures.py)

`GestureEngine.update(frame: Frame, now: float) -> list[Action]`, pure apart from its own state. The engine also exposes `view()` for the overlay and the HUD: state, reticle position, pinch closeness 0..1, engage progress 0..1.

A raw pose must hold for 2 frames (`confirm_frames`) before the engine acts on it; this applies to entering and leaving every pose.

A hand closing into a fist, or opening out of one, passes through `pinch` (the thumb crosses the index tip before the index curls), so a pinch presses only once the hand has **settled** into it: the least-squares slope of the other three fingers' mean reach over the last `SETTLE_WINDOW_S` = 0.1 s (frames from before the pinch included, never back past the pinching finger's last curl) is at most `SETTLE_RATE` = 0.3 reach units per second, over at least 0.05 s and three frames, and the pinch has lasted that long too. The pinching finger must hold still as well: the slope of its own reach over the pinch's frames in that window is at most `SETTLE_FINGER_RATE` = 1.0 reach units per second, since one finger curling or uncurling on its own past the thumb tucked against the curled ones (pointing into a fist, the scroll pose into pointing and back) passes through a pinch while the other fingers are still. A held pinch therefore presses about 0.1 s after the fingers stop. A pinch let go before it pressed is a quick tap: it clicks at the press point as it ends, unless it ends in a fist or another action pose, or its finger was curled within 0.15 s before it began (a hand opening out of a fist); while the other fingers are still closing, its click waits up to 0.15 s for the fist that would drop it.

### Engagement

- **Disengaged**: the camera runs, nothing moves. The overlay shows a faint ring under a visible hand.
- **Engage**: an open `palm`, moving less than 0.3 frame widths per second, held for `engage_s` = 0.5 s (the `engageSeconds` setting). That hand becomes the pointer hand. Setting `engage: always` skips this (any hand engages at once), for a projector room.
- **Pointer hand tracking**: the observation whose anchor is nearest the pointer's last anchor (within 0.25), else the one with the same handedness. The other hand, if any, is the helper hand.
- **Hand lost**: for `hold_s` = 0.25 s nothing changes (a dropped frame must not end a drag). After that every held button is released and any window grab ends. After `lost_s` = 1.5 s without the pointer hand the engine disengages.
- **Real mouse**: when the executor sees the cursor where it did not put it, the engine releases everything and disengages (`engine.on_user_input()`). Touching the mouse always wins. After that, or after the `disengage` command, a palm still in view must drop it (or leave view for `hold_s`) before it engages again, and with `engage: always` no hand engages at once (only through the palm hold, or the `engage` command) until none has been in view for `hold_s` or tracking breaks; another hand, a returning hand and the `engage` command are not held back.
- **A break in tracking**: a camera reopen (resume) and the desktop coming back from the lock screen or a UAC prompt start every track over (`reset_tracks()`, applied on the next frame), so a palm already up needs the full `engage_s` hold again and the cursor re-anchors instead of jumping. The hands up before the break cannot be told from new ones, so nothing a command or the real mouse held back stays held back: with `engage: always` a hand in view engages at once after a resume, as after the lock screen.
- **Commands**: `engage` and `disengage` come from the mod's `hands` tool ("let my hand take the cursor", by voice or typed) and work from any state; `engage` still needs a hand to follow.

### While engaged

| Pose | Gesture | Actions |
| --- | --- | --- |
| `hover`, `palm` | point | `MoveCursor(p)` every frame |
| `pinch` | left press | Once the hand has settled into the pinch (above), freeze the cursor at its position 70 ms before the pinch began (the "rewind", undoing the pinch's own drift), then `Button(left, down)`; a pinch let go before that is a click at that point. While the anchor stays within `slop` = 0.015 (camera units, times the `dragDistance` setting, taken when the press begins) of where the pinch started, the cursor stays frozen; past it, drag: the cursor follows again. Release: `Button(left, up)`. |
| `pinch` x2 | double-click | A press that starts within the system double-click time of the last click and within 3x the double-click rectangle of it is pressed at the last click's point, so Windows sees a double-click. |
| `pinch_middle` | right press | Same as the left press, with the right button. |
| `two` | scroll | The cursor freezes; the anchor's vertical motion in desktop pixels scrolls the content with the hand (`Scroll(dy=+k*Δy_px)` in wheel units, k = 2.4 per px times `scroll_speed` (the `scrollSpeed` setting), sub-notch deltas allowed and accumulated), horizontal likewise. |
| `fist` | grab | `GrabWindow(p)`: the executor picks the window under the cursor (never the desktop, taskbar or the overlay). Moving drags it: `DragWindow(p)`. |
| `fist` + helper hand `fist` | two-hand resize | `ResizeWindow(a, b)`: the midpoint between the hands moves the window, the change in their horizontal and vertical separation (in desktop pixels) changes its width and height. Ends when either hand opens. |
| `fist` released fast | throw | On release, the cursor's speed over the last 120 ms: above 1.5 display widths per second (divided by the `flingSensitivity` setting), mostly sideways, throws the window to the next display that way (or snaps it to that half of its display when there is none); up maximizes, down minimizes: `ThrowWindow(direction)`. Otherwise `ReleaseWindow()`. |

Latches: after a grab ends, a new grab needs the fist opened first, and `pinch` and `pinch_middle` (what a fist passes through as it opens) stay latched until the hand shows a palm, another action pose, or a pose that is no action for 0.2 s; engaging with a fist latches them the same way. After a pinch release, the next press needs the pinch opened first (hysteresis already gives this).

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
- Queued actions have a shelf life of two frame intervals (`SHELF_FRAMES`): when a desktop call stalls, the presses, scrolls and window steps queued meanwhile are dropped rather than replayed in one burst (which Windows would read as a double-click). Letting go (`ReleaseAll`, `ReleaseWindow`, `ThrowWindow`, the up of a held button) is never dropped. Of a batch only the newest `MoveCursor` is applied, a run of drags or resizes keeps its first and last step, and the newest drag or resize is applied however late (one rect computed from the grab: it puts the window where the hand was last seen, even when the let-go arrives in a later batch). `flush()` empties the queue down to its releases, a throw becoming `ReleaseWindow()`, for a pause, the lock screen and the way out.
- While Windows shows the lock screen or a UAC prompt (the runtime's input-desktop check, every 0.5 s), `set_desktop_blocked(True)` stops cursor reads, glides and new actions; releases still go out, best effort, and a refused call there is logged at debug level, not as a fault.
- The first move after a takeover glides from where the user left the cursor.
- Before each cursor move it reads the cursor; if it is more than 6 px from where the executor last put it, the user moved the real mouse: release everything and call `on_user_input`.
- It never leaves a button down: `ReleaseAll` on disengage, on hand loss, on shutdown, on any exception in the loop, and from an atexit hook.
- Windows: on grab it records the window and its visible rect (DWM extended frame bounds); a maximized window is restored first and placed so the grab point keeps its relative position across the title bar, as Windows does. Each drag sets the rect with SetWindowPos (no activation, no z-order change other than raising it once at grab), moving the window by the cursor's displacement and keeping the size the window has now, so the size an app picks when it crosses onto a monitor with another DPI is kept; when that size differs from the one last set, the drag goes on from where the app put the window (Windows' suggested rect for the new DPI) rather than putting it back by its top-left, which would leave most of it on the old monitor and flip its DPI at every frame; only a two-hand resize sets the size. Minimum size 240 x 160; resize clamps to the union of the chosen displays' work areas. A window it cannot move (an administrator's window; UIPI) gives one non-fatal `input_blocked` error per window, and the grab becomes a no-op.

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

`windows.py` implements it with ctypes only (no pywin32). `fake.py` keeps displays, windows and a log of calls for tests. `create_desktop()` raises `UnsupportedPlatform` off Windows; the helper then reports `unsupported_platform`.

## Camera and tracker

- `camera/opencv_camera.py`: `cv2.VideoCapture` on Media Foundation (DirectShow as fallback), 1280 x 720 at 30 fps requested; a reader thread keeps only the newest frame. `--camera` takes an index or part of a camera's name. The two backends number cameras differently (DirectShow's list also holds filter-based virtual cameras), so before DirectShow opens one the camera is matched by its device interface path, or a name only one device has; when nothing tells it apart, DirectShow is skipped rather than opening another device. About 2 s without a frame reopens the device with backoff; once that keeps failing, `read()` raises `camera_lost`, and so does every later read. Errors become `camera_blocked` (Windows privacy switch), `camera_in_use`, `no_camera` or `camera_lost` with a hint.
- `tracker/mediapipe_tracker.py`: `HandLandmarker` in VIDEO mode on the raw frame (BGR to RGB with `cv2.cvtColor`, never a strided view), then mirrors the landmarks (see Coordinates). One hand by default; the runtime asks for two only while a window is grabbed (two-hand resize), because with two requested the palm detector runs on every frame while one hand is in view (about twice the CPU). Closed explicitly on exit.
- Fakes replay scripted frames so the whole runtime runs in tests without a camera or a model.

## Overlay (overlay/)

A small click-through, always-on-top window that follows the cursor and draws the reticle: a thin cyan ring when tracking, an arc that closes as you pinch, a filled core on press, an amber bracket ring while grabbing, chevrons while scrolling, the engage progress arc, and the calibration targets. `render.py` draws into a premultiplied BGRA array (pure, tested on every OS); `windows.py` shows it with a layered window (`UpdateLayeredWindow`) on its own thread. If the overlay fails to start, hand control carries on without it and says so once (`overlay_failed`).

## Sensitivity settings

Nine settings change how hand control feels. Each is one `Knob(key, attr, low, high, default)` line in `KNOBS` (`settings.py`). The mod sends them in the `config` command under the wire key (camelCase, a JSON number); `HandsSettings` holds each as the attribute; the status response's `settings` object reports them under the same keys. The engine and the mapper read the `HandsSettings` object every frame, never a copy, so a change applies on the next frame (with the exceptions under [Live apply](#live-apply)). Every default reproduces the behaviour from before the settings existed: run against the version without them, 1210 of the earlier tests make identical engine calls, and 120 seeded scripted scenarios give byte-identical actions, also with every default sent through `config` first.

| Wire key | Attribute | Range | Default | Higher means |
| --- | --- | --- | --- | --- |
| `cursorSpeed` | `cursor_speed` | 0.7 to 3 | 1 | A faster cursor: the hand travel that crosses the screen is divided by it. |
| `smoothing` | `smoothing` | 0.2 to 3 | 1 | Steadier at rest, more lag when moving. |
| `pinch` | `pinch_sensitivity` | 0.85 to 1.15 | 1 | A lighter, looser pinch counts as a click. |
| `fist` | `fist_sensitivity` | 0.9 to 1.1 | 1 | A looser fist counts as a grab. |
| `engageSeconds` | `engage_s` | 0.1 to 2 | 0.5 | Longer to hold an open palm before it takes the cursor. |
| `dragDistance` | `drag_distance` | 0.7 to 4 | 1 | The hand may drift further during a pinch before it becomes a drag: steadier clicks, a later drag. |
| `flingSensitivity` | `fling_sensitivity` | 0.5 to 2.5 | 1 | A lighter flick throws a grabbed window. |
| `scrollSpeed` | `scroll_speed` | 0.1 to 10 | 1 | More scroll per hand movement. |
| `deadZone` | `dead_zone_px` | 0 to 8 | 1 | Steadier at rest, coarser small moves (pixels the filtered cursor must move before the cursor follows). |

`hands.schema.json` (ConfigCommand and StatusResponse), the protocol validator (`protocol.py`) and `KNOBS` carry the same ranges, and the status `settings` lists all nine.

### The config command

- `config` takes these beside `engage`, `displays`, `hand`, `anchor` and `overlay`. A key left out keeps its value.
- A value that is not a number (a string, a boolean, `null`), NaN, infinite or outside the range gets `bad_request` (`$.key: expected number`, or `$.key: must be between lo and hi`). An integer is a number.
- The helper checks every key first and applies afterwards: a refused command applies nothing, the keys that were fine included.
- A key the helper does not know is refused too (`$: unexpected property`). That is how an older helper answers a newer mod.

### How each is applied

- **`cursorSpeed`** is a gain on the unit square after the homography, in `to_target` and `to_desktop` (the cursor): `(uv - 0.5) * gain + 0.5`. At 1 the point is returned untouched. `to_region` (the relative motion behind scrolling and throws) and the calibration (its targets, and the homography it fits) do not use it. Because the cursor moves `gain` times as far for the same hand jitter, two distances widen with it when the gain is above 1: the double-click rectangle (1.5 times the system's, times the gain) and the drag that restores a maximized window (8 px times the gain, taken when the window is grabbed).
- **`smoothing`** divides `min_cutoff` (1.0 Hz) of the cursor, knuckle and helper-hand One Euro filters. `beta` and `d_cutoff` stay; the plain filter that measures motion for scrolling and throws is not scaled. Smoothing 1 is the old filter.
- **`deadZone`** is `dead_zone_px`: the cursor stays where it is until the filtered point is farther than that from it.
- **`pinch`** scales the pinch thresholds (ratios of palm size): `pinch_close = 0.28 * pinch`, `pinch_open = max(0.40 * min(pinch, 1), pinch_close + 0.04)`, and the overlay's fully-open ratio `max(0.80, pinch_close + 0.04)`. A stricter pinch lowers both thresholds in proportion. A looser one raises only `pinch_close`, so a pinch lets go where it always did: a relaxed open palm rests the thumb 0.39 to 0.47 palm sizes from the index and must not hold the button down after a click. The gap between close and open stays at least 0.04, which is about six times the ratio's noise.
- **`fist`** scales what counts as a fist. Above 1 it scales the curled thresholds (`curled_enter = 1.10 * fist`, `curled_leave = 1.20 * fist`) and raises the extended ones only as far as needed to keep 0.04 above them (`extended_leave = max(1.25, curled_leave + 0.04)`, `extended_enter = max(1.40, extended_leave + 0.04)`), so `curled_enter < curled_leave < extended_leave < extended_enter` holds at every value. Below 1 the curled and extended thresholds stay, and only two new ones tighten, `fist_enter = 1.10 * fist` and `fist_leave = 1.20 * fist`; a third set of hysteresis flags uses them for the `fist` pose and the ring and pinky of the `two` pose. The curled flags also keep a pinch from starting on a curled finger and tell the settle logic how still a pinch is, so scaling them down would change when a hand closing slowly into a fist presses the button. At fist 0.9, closing from hover into a fist in 1.0 s gave 0 stray presses in 60 runs (5 when the curled flags were scaled too), and in 1.2 s 1 in 60, as at the default (23 when scaled).
- **`engageSeconds`** is `engage_s`, read each frame for the engage progress.
- **`dragDistance`** multiplies `slop`. Each press takes `slop * drag_distance` when it begins and keeps it.
- **`flingSensitivity`** divides the throw speed: `throw_speed / fling_sensitivity` display widths per second (1.5 at the default), read when the fist opens.
- **`scrollSpeed`** is `scroll_speed` in the scroll's wheel units per pixel (2.4 times it).

### Live apply

A setting never moves a held drag, leaves a button down, jumps the cursor under a held press or grab, or releases anything. The engine therefore applies a change in one of these ways:

- **Waits for `point`.** While the engine is in a press, drag, scroll, grab or resize (`_mode` is not `point`), the pose thresholds of every tracked hand (a held pinch must not let go because the pinch got stricter), the smoothing, the dead zone and the cursor gain keep their old values. They take the new ones on the first frame back in `point`.
- **Glides.** The cursor gain and the smoothing then move to their new values with a 0.12 s time constant (`TUNE_TAU_S`; smoothing moves by the same factor each step), so a still hand sees no jump and a moving one no sudden change of speed. A frame that arrives late glides no further than 0.1 s of it.
- **Taken at once** while the engine is disengaged or calibrating, since nothing is steered then; the filters that start with the next engagement read them.
- **Read where used.** `engageSeconds` (each frame, for the engage progress), `scrollSpeed` (each frame, for the next wheel units) and `flingSensitivity` (when the fist opens) move nothing that is held. `dragDistance` is taken per press, so a change never turns a held press into a drag.

### Measured

Measured on the real model's test photos with rotated, scaled, dimmed and noisy variants (1835 hands) and on synthetic hands. "Rest" is 0.001 frame widths of landmark noise at 30 fps over 12 s; "lag" is how far the cursor trails a hand moving at constant speed. The numbers come from developer sweeps that are not committed; the tests in `test_knobs.py`, `test_sensitivity.py` and `test_poses_real.py` pin the margins that set each range end, not the sweeps themselves.

| Setting | Measurement |
| --- | --- |
| `smoothing` | Cursor step at rest: 1.56, 0.81, 0.32, 0.11 px per frame at 0.25, 0.5, 1, 2; the cursor moves in 76, 52, 25, 9.5% of frames. Lag at 640 px/s: 15.5, 22.3, 28.6, 33.3 px (24, 35, 45, 52 ms). After a slow stop (160 px/s) the cursor settles within 3 px in 0.00, 0.07, 0.20, 0.57 s, and in 0.80 s at 3. |
| `deadZone` | At default smoothing the cursor moves in 100, 25, 5, 0, 0% of frames at rest at 0, 1, 2, 4, 8 px. Lag is unchanged except at 160 px/s with 8 px: 18.2 px against 15.5 px. |
| `cursorSpeed` | Hand travel across the screen (x, in frame widths): 0.86, 0.60, 0.375, 0.30, 0.20 at 0.7, 1, 1.6, 2, 3 (y: 0.71, 0.50, 0.31, 0.25, 0.17). Cursor step at rest: 0.15, 0.33, 0.79, 1.14, 2.03 px per frame. |
| `pinch` | Close/open thresholds: 0.238/0.34 at 0.85, 0.28/0.40 at 1, 0.322/0.40 at 1.15. MediaPipe's OK-sign photo reads as a pinch in 52, 64, 84, 91% of its variants at 0.85, 0.9, 1, 1.1. |
| `fist` | A finger enters the fist below a reach of 0.99, 1.10, 1.21 at 0.9, 1, 1.1 (all four fingers must). |
| `dragDistance` | `slop` is 0.0105, 0.015, 0.03, 0.06 frame widths at 0.7, 1, 2, 4. With landmark noise of 0.003, clicks that become drags: with no drift 1% at 0.7 and 0% from 1; with the knuckles drifting 0.008 as the pinch closes 41% at 0.7, 2% at 1 and 0% at 2. A deliberate drag of 0.1 frame widths in 0.5 s always starts; the cursor's first jump is 30, 69, 180 px at 1, 2, 4. |
| `flingSensitivity` | The slowest fist sweep that throws, in frame widths per second: 1.76, 0.85, 0.56, 0.41 at 0.5, 1, 1.5, 2. |
| `engageSeconds` | A still palm engages after the setting plus 0.03 s (0.13 s at 0.1, 0.53 s at 0.5, 2.03 s at 2). A palm moving at 0.3 frame widths per second or more never engages, at any setting. |
| `scrollSpeed` | Not measured: the scroll is `2.4 * scroll_speed` wheel units per pixel of hand motion. |

### Where each range ends

Each end is where hand control stops being usable, as measured.

| Setting | End | Why |
| --- | --- | --- |
| `cursorSpeed` | min 0.7 | At 0.6 the hand has to reach the frame border to reach the screen edge. At 0.7 the screen edges are reached at 7% and 93% of the frame in x, 9% and 81% in y. |
| `smoothing` | max 3 | A slow stop settles within 3 px in 0.80 s at 3, 1.10 s at 4 and 1.57 s at 5. |
| `pinch` | min 0.85 | The OK sign reads as a pinch in 84% of variants at 1, 52% at 0.85, 14% at 0.6 and 4% at 0.5. |
| `pinch` | max 1.15 | The close threshold's margin to the closest hand that is not pinching (a relaxed palm, thumb 0.394 palm sizes from the index) is 18% at 1.15 and 0.5% at 1.4. With the noise of a live hand that palm reads as a pinch in every run from 1.35 and in one run in nine at 1.3, which presses the button or keeps the hand from engaging; at 1.15 none of its 29 variants did, against 7 of 29 at 1.4. |
| `fist` | min 0.9 | At 0.8 the real fist photo reads as a fist 18% of the time. 0.85 is the edge; 0.9 leaves margin on the loosest real fist. |
| `fist` | max 1.1 | At 1.2 the OK sign drops from 84% to 63%, and a hovering hand with 0.003 jitter makes 10 false grabs in 20 s. At 1.1 there are none. |
| `dragDistance` | min 0.7 | With 0.003 landmark noise and no drift, 1% of clicks become drags at 0.7, 20% at 0.5 and 88% at 0.3. |
| `flingSensitivity` | max 2.5 | At 3 a hand moving 0.28 frame widths per second throws the window, which is below the 0.3 that counts as still for engagement. |

`engageSeconds` (0.1 to 2), `scrollSpeed` (0.1 to 10) and `deadZone` (0 to 8) have no measured limit; their ends are plain bounds.

`tests/test_poses_real.py` pins the pinch and fist margins on the photos, and `tests/test_knobs.py` pins the table.

### Not done: detection confidence

A `detection` setting for the tracker's hand detection confidence (0.2 to 0.9) was left out. MediaPipe takes the confidence when the landmarker is built (`MIN_HAND_DETECTION_CONFIDENCE`, 0.6 now), so applying it live means rebuilding the landmarker while the camera runs.

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
| `status` | `{}` | state, camera, fps, inference ms, engaged, displays, settings (`engage`, `hand`, `anchor`, `overlay` and the nine [sensitivity settings](#sensitivity-settings) under their wire keys) |
| `config` | `{engage?, displays?, hand?, anchor?, overlay?, cursorSpeed?, smoothing?, pinch?, fist?, engageSeconds?, dragDistance?, flingSensitivity?, scrollSpeed?, deadZone?}` | live settings. The last nine are numbers with the ranges under [Sensitivity settings](#sensitivity-settings); one that is not a number or is out of range gets `bad_request` and nothing in the command is applied. |
| `pause` / `resume` | `{}` | release / reopen the camera. `{ok: true, pending: true}` while the camera is still opening, for a second pause too (and a resume before `start()` reaches the camera); a reopen that fails after that is an `error` event, unless a resume is still waiting on it (one sent after a pause during that open too), which answers with the error instead. A failed reopen pauses again as a pause does: a calibration or engage asked for meanwhile is dropped. A pause while `start()` opens the camera leaves hand control paused, not failed, even when that open then fails (a non-fatal `error` event says why); a resume a later pause countermands answers `bad_request`. |
| `engage` / `disengage` | `{}` | take or drop the cursor (the `hands` tool) |
| `calibrate` | `{action: "start" \| "cancel"}` | calibration. `start` answers `bad_request` while paused and while a resume is still reopening the camera; sent before the camera has first opened (state `starting`: `start()`'s open, or a resume's after a pause since `hello`), it begins with the first frame. |
| `shutdown` | `{}` | exit cleanly, once the answer is written |

## The mod side (plugin/hooks/hands.ts)

- `HandsHelper`: the supervisor (spawn, hello, heartbeats, backoff, stop), reusing helper.ts's `LineReader` and timing constants.
- `Hands`: on/off kept in `$.store` (`handsEnabled`, `{isOn, setting}`: `/jarvis hands on|off`'s choice with the `handControl` setting it overrode, dropped once the setting changes, so the setting has the last word), pause kept there too (`handsPaused`, sent right after `hello` so a restarted helper never opens the camera while paused), the chosen camera, engage mode and displays; starts the helper at session start when on and installed; turns events into the `hands` part of the view and the status line (`JARVIS · ready · … · hands active`).
- `/jarvis hands [on|off|status|calibrate|display <n|all>|engage <palm|always>|camera <n|name>|pause|resume|restart|setup]`, the tuning commands (below), and `/jarvis setup hands`.
- A model tool `hands` (`mcp__jarvis__hands`, input `{action?: on|off|status|calibrate|pause|resume|engage|disengage, display?: "all" | "2" | "1,2", setting?: <name>, value?: <number> | "default", preset?: precise|balanced|fast}`; `display` alone, or applied before the action) so "Jarvis, turn on hand control" and "make the cursor faster" work by voice or typing. `setting` and `value` change one sensitivity setting, a `setting` alone reports its current value, and `preset` sets a whole profile (not together with `setting` or `value`). They come alone or with an action, apply after `display` and before the action, and give the text the matching `/jarvis hands set` or `preset` prints. The tool's description lists the settings with their ranges and what a higher value does, and asks the model to read a setting first, change it by a modest step (about a fifth of the way to its limit, never straight to an extreme) and report the new value. The helper's other live settings (`hand`, `anchor`, `overlay`) are not exposed.
- Tuning, in `hands.ts`:
  - **The table.** `ROWS` (as `KNOBS`) has one line per sensitivity setting: the wire key, the name the commands use, a label, the range and default, the words for "higher means", aliases and, for an everyday setting, the plugin setting that gives its default. Its ranges and defaults mirror the helper's `KNOBS`; the helper refuses what is outside, and the mod checks first so the user hears the range. `hands.test.ts` writes the helper's ranges out and checks the table against them.
  - **Names.** `speed` (`cursorSpeed`), `smoothing`, `pinch`, `fist`, `engage-time` (`engageSeconds`), `drag-distance` (`dragDistance`), `fling` (`flingSensitivity`), `scroll-speed` (`scrollSpeed`) and `dead-zone` (`deadZone`). The wire key, the label and an alias (`cursor`, `click`, `grab`, `scroll`, ...) name a setting too, in any case and spelling. A value is a plain decimal inside the range, or `default`; a number outside it is refused with the range in the message, never trimmed.
  - **Commands.** `tune` (also `tuning`, `sensitivity`) lists every setting with its value, default, range and what higher does, marking changed ones. `set <name> <value|default>` changes one (`to` and `=` are allowed: `set speed to 1.5`), and a name alone shows it. `preset [precise|balanced|fast]` applies a profile, or lists them with none. `reset [name]` puts every setting, or one, back to its default. The status gains a line: `Tuning: balanced (all defaults)`, `Tuning: precise preset` or `Tuning: custom, N changed (speed 1.4, ...)`.
  - **Presets** (`PRESETS`) are plain knob values: `precise` is `cursorSpeed 0.8, smoothing 1.8, deadZone 2, dragDistance 1.5, pinch 0.9`; `balanced` names none, so every setting is at its default; `fast` is `cursorSpeed 1.6, smoothing 0.5, deadZone 0, pinch 1.1`. A preset is a whole profile: a setting it does not name goes back to its default, so the status can name a preset exactly or say "custom". Measured with the sweeps and noise of the table above, `precise` crosses the screen with 0.75 frame widths of hand travel (balanced 0.60, `fast` 0.37), trails a hand at 640 px/s by 31.3 px (balanced 28.6, `fast` 19.7) and settles within 3 px after a slow stop in 0.47 s (balanced 0.20, `fast` 0.07). With 0.001 frame widths of noise at rest the cursor of `precise` never moves and `fast`'s moves in every frame (its dead zone is 0).
  - **What is kept.** A choice by `set`, `preset` or the tool is kept in `$.store` under `handsTuning`, per setting as `{value, setting}`: the value with the plugin setting (or built-in default) it overrode, as `handsEnabled` does. It lapses and is dropped once that plugin setting changes, so the plugin setting has the last word, and a choice equal to the setting is none. `default` (and `reset`) means the plugin setting when it is a valid number, else the built-in default; `preset balanced` uses the built-in defaults.
  - **Plugin settings.** `handCursorSpeed`, `handSmoothing`, `handPinch` and `handScrollSpeed` (`userConfig` strings, default `1`) give the default of `cursorSpeed`, `smoothing`, `pinch` and `scrollSpeed`; the other settings are for the commands. A value that is not a number in range is replaced by the built-in default, logged and toasted once.
  - **Sending.** The start's `config` carries `engage`, `displays` and only the settings that differ from the built-in defaults, so the rest stay the helper's own. A change sends `config` with only the settings that changed. When the helper refuses it (`bad_request`) or cannot apply it, the user is told its message ("Nothing was changed") and the stored choice is rolled back, so the table, the status and the helper agree. When the helper is not running, the choice is kept and goes out at the next start. If an older helper refuses the start's `config` because it does not know the settings, the mod sends it again without them, so `engage` and `displays` still apply; it logs and toasts, and `tune` and the status say the tuning is not in force.
  - **Engage time in the text.** The instructions the mod prints (start, status, help) name the palm hold from the setting ("half a second" at 0.5, else the number of seconds).
- Setup: `uv sync --project <plugin>/hands --frozen --no-dev --no-editable --reinstall-package jarvis-hands` with `UV_PROJECT_ENVIRONMENT=<dataDir>\hands\venv`, then `python -m jarvis_hands setup --data-dir <dataDir>` for the model (JSON progress lines, as the voice setup). It writes `<dataDir>\hands\installed.json` (`{pluginVersion}`); a start whose record names another version says once to run `/jarvis setup hands`. Plain `/jarvis setup` refreshes an installed hand helper along with the voice helper, leaving alone one that another window runs.

## Tests

- Python, no camera or model needed: poses on built hands, filters, mapping, every gesture in the table as scripted frames, the executor against the fake desktop (interpolation, button safety, real-mouse override, window rects, throw), calibration, render, protocol against the schema, the control server's guards, and `python -m jarvis_hands run --fake` driven over HTTP.
- With the real model (skipped unless `JARVIS_HANDS_MODELS_DIR` is set; CI sets it): poses on MediaPipe's own test photos (fist, pointing up, victory, open hands).
- Windows only: the ctypes structure sizes, monitor enumeration and a cursor round trip.
- Sensitivity settings: `tests/test_knobs.py` (the table against the contract with the mod, range edges and just outside, bad types, a refused command applying nothing, protocol and schema agreement, the mapper's gain), `tests/test_sensitivity.py` (each setting's effect through the engine, the hysteresis gaps across every range, live changes in the middle of a press, a drag, a scroll and a grab checked against an engine that never got the change, and a seeded fuzz of live changes), thresholds in `test_poses.py`, config and status in `test_protocol.py` and `test_runtime.py`, and the pinch and fist margins at the range corners on the real photos in `test_poses_real.py`. Timing tests run on scripted frames or injected clocks, never real sleeps.
- Mod: `hands.test.ts` with the fake child and HTTP like `helper.test.ts`; its tuning tests cover the table, names and values, `tune`, `set`, `preset` and `reset`, what is kept, the config sent at start and on a change, rollback when the helper refuses, the older-helper fallback and the tool.
- CI: a `hands` job on Windows, macOS and Ubuntu.

## Later

- **Projector wall mode**: the camera faces the projected image; calibration projects ArUco markers and fits the camera-to-projector homography automatically, per display; pointing at the wall moves the cursor on the projector; pinch clicks.
- **Touch on the wall**: needs depth (an Intel RealSense, Orbbec or OAK-D camera) or an IR rig; a plain webcam cannot tell touching from hovering reliably.
- **macOS**: the desktop backend through Quartz (CGEvent) and the Accessibility API, camera permission tied to the terminal or the Claude app.
- Voice and hands together: "put this on the projector" while pointing at a window.
