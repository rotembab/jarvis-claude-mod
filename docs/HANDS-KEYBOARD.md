# The air keyboard

Jarvis can draw a small keyboard on your screen and let you type on it in the air: hold your hands over the keys as if they rested on a real keyboard, and tap a finger down over a key. The webcam sees the tap. It is an add-on to [hand control](../README.md#hand-control-preview), it is off until you turn it on, and it is for short messages when voice does not fit and the real keyboard is out of reach.

Read the [honest limits](#honest-limits) first. Typing in the air with a webcam is slow and makes mistakes, and nothing in this document is measured on a real hand yet. The mistakes cost little, because what you tap goes into a box on the keyboard and stays there until you tap **Insert** three times.

The first part is for the person typing (setup, a walk-through, every key, the commands, tests to run on your PC, troubleshooting). The [second part](#for-developers) is for people changing the code. The binding design, written before the build, is kept in [notes/2026-10-08-hands-keyboard-design.md](notes/2026-10-08-hands-keyboard-design.md); where it and the code differ, the code and this page win.

**What was and was not tested.** All of the logic runs in the test suite against fakes: a fake camera, a scripted tracker, a fake desktop and a stand-in for the Windows calls. No test has run on a real Windows PC with a real camera, a real `SendInput`, a real overlay window or a real Claude Code terminal. [What only your PC can settle](#what-only-your-pc-can-settle) lists it.

## Using it

### What you need

- Windows, a webcam, and hand control installed (`/jarvis setup hands`) and turned on (`/jarvis hands on`).
- Your hands in view at typing height. A camera on top of the monitor often cannot see them; tilt it down, or hold your hands higher and rest your forearms.
- A bright, even light on your hands. A dim room makes webcams drop to 15 frames a second, and the air tap needs more (see [the camera](#the-camera-and-the-banner)).
- A mouse and keyboard within reach, for clicking into the right window first and as the way out.

### Set it up

1. Update the plugin and restart Claude Code: `claude plugin update jarvis@jarvis-claude-mod`.
2. Update the hand helper: `/jarvis setup hands`.
3. Turn the keyboard on: `/plugin configure jarvis@jarvis-claude-mod`, set **Air keyboard** (`handKeyboard`) to `on`. This option is the only switch. No command and no tool call can turn it on. If `/jarvis hands keyboard` still answers that the keyboard is off, restart Claude Code (it has not been checked on a PC whether the option applies without a restart).
4. Make sure hand control is on: `/jarvis hands on`.
5. Practise once: `/jarvis hands keyboard practice`. Nothing you tap goes into any window. It takes a few minutes (see [practice](#practice)) and the keyboard will not open for real until an air practice has passed. Practise again after you change the camera.
6. Open it: `/jarvis hands keyboard`. Click into the window you want to type into first (for example Claude Code) with the real mouse: the keyboard never takes the focus, and Insert types into whatever window is in front.

You can also ask Claude to open it ("open the air keyboard"). Jarvis asks you first, like any other hand control action, unless your own rules allow `mcp__jarvis__hands`. Claude can open it, open the practice and close it. It cannot turn it on, insert, send or read what is in the box.

### The walk-through

1. **Open it.** The keyboard appears at the top of the screen, on the display of the window in front, with an empty box above the keys. At size 1.0 and 96 DPI it is about 660 by 420 pixels. The hand control pointer is off while it is open. The strip says `Hold your hands over the keys`.
2. **Placing.** Rest both hands over the home row (a s d f and j k l ') with the fingers raised and slightly curved, and keep them still for about half a second. Faint rings mark your home positions. One hand works too; it is slower.
3. **Warm-up.** The strip names one finger at a time, `Tap: right index  1/8`, in a fixed order: right index, left index, right middle, left middle, right ring, left ring, right pinky, left pinky. With one hand: index, middle, ring, pinky. Tap that finger over its own home key, with the other fingers still. The ring turns green when the tap is counted and the next finger is named a second later. Nothing is typed. A tap of another finger is a stray, and the third stray sends you back to the first finger (`Only tap the finger the strip names. Starting again.`). A finger that is not seen after 15 seconds gets `tap a bit firmer with your fingers raised`; after 25 seconds without progress, or after the second restart, the strip says `Taps not showing up? Try /jarvis hands keyboard press pinch`. If the warm-up is not done in 90 seconds the keyboard closes. The warm-up measures how deep each of your fingers taps, and sets each finger's threshold from its own depth. It is a speed bump against accidents. It does not prove that you meant to type.
4. **Typing.** A ghost key shows under every fingertip. When a finger starts to dip, the key under it turns amber (that is the key the tap will type), and a few frames later it flashes green and the letter appears in the box. A refused tap flashes red. Move the hand to the next key, let it stop, tap again. There is no key repeat: a finger held down types nothing more.
5. **Fix mistakes.** `Bksp` (the end of the home row) removes the last character. `Clear` (bottom row, far left) empties the box after two taps. You cannot move a cursor inside the box: the box is edited at its end only.
6. **Insert.** Tap `Insert` (bottom row, far right) three times. The strip counts: `Insert 37 characters into WindowsTerminal? Tap Insert 2 more, firmly.` then `Tap Insert once more to type into WindowsTerminal.` The third tap starts the run. The key reads `Stop`, the strip says `Typing 12/37 into WindowsTerminal. Tap Insert to stop.`, and the box dims as it empties. When it is done: `Typed 37 characters into WindowsTerminal. Send: 3 firm taps within 10 s.`
7. **Send (optional).** Tap `Send` (bottom row, second from the left) three times to press Enter in that window. Send only exists for ten seconds after a text Insert. See [Insert and Send](#insert-and-send).
8. **Close.** Tap `Close` (right end of the space row), hold both fists up for one second, run `/jarvis hands keyboard off`, or let it close. With text in the box, `Close` takes two taps. **Closing throws the box away. Nothing in it is typed.** After a close, lower your hands out of the camera's view for a moment (the pointer comes back once no hand has been seen for 0.6 seconds), then it works as usual.

If something stops the run, the strip says why (`Typed 12 of 37, then stopped (the window changed). The rest is still in the box.`). Fix the situation (click the right window with the real mouse and wait a moment) and tap Insert three times again: only the rest is typed.

### The keys

The review layout has five rows. The letters are in the same places as on a real keyboard.

```
row 0: q  w  e  r  t  y  u  i  o  p  [unused]
row 1: a  s  d  f  g  h  j  k  l  '  [Bksp]            your fingertips rest on this row
row 2: [Shift] z  x  c  v  b  n  m  ,  .  /
row 3: [Lang] [Priv] [Home] [        Space        ] -  ?  [Close]
row 4: [Clear] [Send]  (three cells that do nothing yet)  [Insert]
```

| Key | What it does |
| --- | --- |
| Letters, `' , . / - ?` | Add that character to the box. |
| Space | Adds a space. |
| Bksp | Removes the last character. Empty box: nothing. |
| Shift | The next English letter is a capital. It clears after one character or 5 seconds. Inert in Hebrew. |
| Lang | Switches between English and Hebrew. Hebrew uses the same positions. `layout auto` picks the language from the window in front when the keyboard opens. |
| Priv | Private mode: the box shows one bullet per character and the key highlights are hidden. It asks Windows to keep the keyboard out of screen capture, as far as Windows allows. It hides nothing from someone looking at your screen or your hands. Insert still works. |
| Home | Places the keyboard again under your hands. Hold still for a moment. The box and the warm-up are kept. |
| Close | Closes the keyboard. Two taps when the box holds text. |
| Clear | Empties the box after two taps. No undo. |
| Insert | Types the box into the window in front after three taps. Reads `Stop` during a run. |
| Send | Presses Enter once after a text Insert, after three taps. |
| The three middle cells of row 4 | Do nothing. A tap on one flashes red and takes the Send opportunity away. They are kept for a later word decoder. |

What the keyboard does not have, on purpose: digits, `!`, `;`, Esc, Tab, arrows, Delete, Home or End, function keys, Ctrl, Alt, Windows or any shortcut, and Enter inside the box. These are keys that answer or change Claude Code's prompts, so nothing here can type them. The box takes English and Hebrew letters, the six marks, and the space, at most 200 characters.

### Insert and Send

Nothing reaches another window until three deliberate taps on Insert. With the air tap, a tap counts toward Insert only when:

- it comes at least 0.25 seconds after the last one and all three come within 6 seconds;
- your hand is at rest (it moves under 0.10 frame widths a second) at the moment of the tap;
- one finger makes all the taps;
- at least two of the taps are firm. A soft tap still counts, and when three have counted without two firm ones the run stays open and the strip asks `a little firmer`, so it can take four taps or more;
- you tapped no other key, and did not edit the box, in between.

A tap from a moving hand neither counts nor cancels, and the strip says `Hold your hand still, then tap Insert`. An empty box, or a box of only spaces, cannot be inserted.

The stillness, one-finger and firmness rules come from the air tap's own evidence. A pinch press carries none, so with the pinch method Insert and Send count three taps, 0.25 seconds apart, within 6 seconds, and nothing else.

A run types about 30 characters a second at 30 frames a second (one character per camera frame, with at least 0.030 seconds between them). A run is at most 200 characters and 30 seconds. It stops early when you tap Insert (`Stop`, from half a second after the run began), when you touch the real keyboard or mouse (the keyboard steps aside for 1.5 seconds, and for as long as Ctrl, Alt or Windows is down), when the window in front changes, when the window becomes one it may not type into, or when the keyboard cannot be drawn. Whatever was not typed stays in the box.

Send presses Enter once in the same window. All of this must hold:

- a text Insert has just finished, in a window that is still in front, and the box is empty;
- each of the three taps comes within 10 seconds of the Insert and, with the air tap, from a hand at rest, by one finger, with at least two firm (the same evidence as for Insert);
- the text did not begin with `/` or `!`, and no earlier Insert in this session did. For those the strip says `Not sent: it starts with "/". Press Enter yourself.` Claude Code treats a first `/` as a command and a first `!` as shell mode. The refusal is a heuristic, not a boundary;
- you tapped no other key since the Insert (a letter, Shift, a middle cell), and no hold came up (a slow camera does not count);
- it is the first Send after that Insert.

`/jarvis hands keyboard enter off` removes Send altogether.

**Jarvis cannot see Claude Code's prompts.** If a permission question is showing when you Insert, the letters and spaces you insert go into that window like any other typing. A box holding only `y` can answer it. A first `/` can open the command menu. Look at the window before the third tap.

### When it stops, pauses or closes

The strip says `Paused: ...` and the keyboard dims while one of these holds. Taps are dropped while it lasts, not queued, and every finger must reopen afterwards.

| Strip says | Why | What to do |
| --- | --- | --- |
| that window cannot be typed into | The window in front runs as administrator, is the taskbar, Start or the task switcher, is a Jarvis window, or Windows would not say which window it is. | Click a normal window. |
| that looks like a password box | A classic Windows password box has the focus. | Do not use the air keyboard for passwords. |
| the screen is covered | A full screen app or presentation is in front, or Windows says the screen is busy. | Leave the full screen. |
| the keyboard could not be drawn | The overlay stopped drawing. | Wait; it closes after 2 seconds of it. |
| the window changed | The window in front just changed. | Wait half a second. |
| you used the keyboard or mouse | You touched the real keyboard or mouse. | Wait 1.5 seconds. |
| the camera is too slow | Under 10 frames a second. | Light the room. It resumes at 12. |

The keyboard closes by itself, and throws the box away, when:

- you pause hand control, lock the screen, or a UAC prompt appears; nothing reopens it afterwards;
- the camera stops, or the overlay cannot be shown for 2 seconds;
- your hands are out of view for 30 seconds (2 minutes while the box has text; the last 20 seconds are counted down), or no key is accepted for 5 minutes;
- placing takes over 30 seconds with no still hand, or the warm-up is not done in 90 seconds;
- Windows stops taking the keys three times in a row, or too many keys arrive at once;
- an internal error happens (hand control carries on).

### The camera and the banner

The air tap needs at least 26 frames a second and steady landmarks. Below that the keyboard keeps working with a banner:

| Banner | Meaning |
| --- | --- |
| `Air tap is less sure: camera at 18 fps. Tap a little firmer.` | Under 26 frames a second for 2 seconds. Nothing else changes. |
| `Air tap is less sure: hand tracking is shaky. Tap a little firmer.` | Landmark noise over 0.022 frame widths for 3 seconds. Every tap threshold is 1.3 times higher while it lasts. |
| `Air tap is less sure: the camera is dropping frames. Tap a little firmer.` | Three or more holes in the stream within 5 seconds. Nothing else changes. |
| `Air tap off: camera at 11 fps, using pinch` or `Air tap off: hand tracking too shaky, using pinch` | Under 13 frames a second, or noise over 0.036. The air tap is switched off for the rest of the session and the strip asks you to pinch each finger to your thumb once. The box and its text are kept. |

A banner goes away only after 8 seconds of good readings, and `off` never goes away for that session. Open the keyboard again in better light to get the air tap back. With no pinch to fall back on, the keyboard closes with `air_unreliable`; so does a warm-up that you tapped through for 90 seconds without arming.

Strip hints you may see while typing: `Raise your fingers a little, curved, as over a real keyboard` (the hand is too relaxed to see taps), `Hold your hands steadier to type` (the hand moves while you tap), `Keep the other fingers still while one taps`, and `Hands drifted: press Home` (your hands rested away from where the keys were placed for two seconds).

### Practice

`/jarvis hands keyboard practice` opens the keyboard in practice mode. Nothing is typed anywhere and Insert, Clear, Send and the middle cells do nothing (`Practice: nothing is inserted.`). With the air tap it goes through:

1. placing and the warm-up;
2. a 4 second introduction;
3. a drill of 60 prompts, 1.2 seconds apart, with two hands (30 with one): `Tap: left ring` and, for the four keys that are not under a resting fingertip (Bksp, Insert, Clear, Send), the finger and the key. Every second home prompt is a key away from the home position;
4. six short phrases in two groups of three (`the quick brown fox`, `jumps over the lazy dog`, `hello world`, `yes please`, `go ahead, thanks.`, `what is this?`; Hebrew phrases with `layout he`), each group followed by a 35 second rest in which you wave, open and close your hands and do **not** tap;
5. 30 seconds of talking to the camera with your hands moving, again without tapping.

It ends with `Practice done: 91% of keys right, 1.5 false taps a minute, 93% of index and middle taps seen.` (those numbers are an example). A completed practice writes `keyboard-practice-air.json` under `%USERPROFILE%\.jarvis\hands\`. The next real air open accepts it when the rests add up to at least 60 seconds, the false taps in them are at most 3.0 a minute, and, with two hands, index and middle recall in the drill is at least 0.70. That bound is coarse: it stops a camera where most taps are lost. It is not the bar for calling the air tap good (see [the live tests](#try-it-on-your-pc)). The marker is friction against opening it by accident. It is not proof that you meant to type.

A practice that you stop early writes no marker. If the camera or tracking is too poor during an air practice, it ends with `Air tap is not usable on this camera. Use the pinch method.`, writes no marker, and the next air open tells you to use `/jarvis hands keyboard press pinch`.

Practice also writes two files for tuning (see [recording a trace](#record-a-trace-and-tune-offline)): a landmark trace `keyboard-trace-<time>.npz` (kept 14 days) and a tap log `keyboard-practice.jsonl` (1 MB, three copies). Both hold numbers about the prompted taps only, never what you typed in a live session. A live session writes neither.

### Commands

| Command | What it does |
| --- | --- |
| `/jarvis hands keyboard [on]` | Open the keyboard (needs a practice first). |
| `/jarvis hands keyboard practice` | Open it in practice mode. |
| `/jarvis hands keyboard off` (or `stop`) | Close it. The box is thrown away; nothing is typed. Works even when the option is off. |
| `/jarvis hands keyboard recenter` | Place the keys under your hands again (the `Home` key does the same). |
| `/jarvis hands keyboard private` and `public` | Hide or show the box and the key highlights. A keyboard that opens next starts public. |
| `/jarvis hands keyboard status` | Whether it is on, its state and the settings in force, and how many characters wait in the box (a count, never the text). |
| `/jarvis hands keyboard help` | The list. |
| `/jarvis hands keyboard <setting> [value\|default]` | A setting by name alone shows it; a value sets it; `default` puts it back. A change applies the next time the keyboard opens. |

`recenter`, `private` and `public` act on an open keyboard and say so when it is closed. No command carries text and none can insert, send or clear the box. The mod keeps the settings you choose in its store and sends them to the helper at every start; that they survive a new session has not been checked on a PC.

| Setting | Values | Default | What it does |
| --- | --- | --- | --- |
| `press` | `air`, `pinch`, `windows` | the plugin option `handKeyboardPress` (`air`) | How a key is pressed. |
| `commit` | `review`, `direct` | `review` | `review`: taps fill the box. `direct` types each key into the window at once and works only with `pinch`. |
| `layout` | `auto`, `en`, `he` | `auto` | The language when the keyboard opens. |
| `size` | 0.6 to 1.6 | 1 | On-screen scale of the keyboard. |
| `reach` | 0.8 to 1.5 | 1 | How far apart the keys are in front of the camera. Higher: more hand travel and an easier aim. |
| `dock` | `top`, `bottom` | `top` | Where on the display it sits. |
| `enter` | `twice`, `off` | `twice` | `twice` keeps Send (three guarded taps in review mode; two presses in direct mode). `off` removes it. |

The `air` method cannot be combined with `commit direct`, and `direct` cannot be set unless the method is `pinch`; the command refuses and changes nothing. The helper also has two settings the mod does not expose: `idleS` (5 to 300 seconds, default 30: how long hands may be out of view) and `inject` (`unicode` or `vk`, default `unicode`: how characters are sent to Windows).

The plugin options are **Air keyboard** (`handKeyboard`, `on` or `off`, default `off`) and **Air keyboard press method** (`handKeyboardPress`, `air`, `pinch` or `windows`, default `air`).

### Other press methods

**pinch.** Pinch the finger over a key to your thumb. It has its own warm-up (pinch each finger once). With `commit review` (the default) it fills the same box, with no practice marker needed, and has the same Insert and Send, counted by number only (see [Insert and Send](#insert-and-send)). It is also what the air tap falls back to. The design expects it to be more reliable than the air tap, but it needs steadier landmarks (it fails near a landmark noise of 0.020), and that is not measured on your camera either.

**pinch with `commit direct`.** Each pinch types its key straight into the window in front, with no box and no Insert. This is the only mode where a stray pinch is a stray key in a window. It needs its own practice (`keyboard-practice.json`: rests of at least 20 seconds, at most 1.0 false presses a minute) and shows the last 24 characters typed. Enter is two presses within 1.5 seconds. Use it only after you have seen the pinch behave on your camera.

**windows.** Opens Windows' own on-screen keyboard (`osk.exe`) and you press its keys with the hand control pointer. **None of Jarvis' keyboard safeguards apply to it**: no box, no refusal of administrator windows, no Enter guard, no rate limits, no yielding to your real keyboard, no private mode, and it has Ctrl, Alt, Win, Esc and Tab. It can type into administrator windows. It still needs `handKeyboard` on.

### Try it on your PC

Three console tools check the parts that only a real PC can settle. Run them in PowerShell. They use the hand helper's Python:

```powershell
$py = "$env:USERPROFILE\.jarvis\hands\venv\Scripts\python.exe"
```

#### keytest: does Unicode typing reach Claude Code's terminal?

```powershell
& $py -m jarvis_hands keytest --inject both --hebrew
```

After a 5 second countdown (change it with `--countdown SECONDS`, 0 to 60) it types `abc ABC .,'-?/ ok`, a space and a Hebrew word, then one `x` and a Backspace, into the window in front, one key at a time, through the same Windows call and the same allow-list of keys that the keyboard uses (it checks the window in front once, before it starts, not before each character). `--inject unicode` (the default) or `vk` tries one way; `both` runs Unicode, a space, then `vk`. In `vk` mode a letter that your keyboard layout has no key for (Hebrew on an English layout) goes in as Unicode anyway. Click into the window you want to test before the countdown ends. In the last second it also prints the median time of 100 reads of the window in front (`key_target()`), which must be 2 ms or less.

Test Windows Terminal with Claude Code (at its empty prompt), Notepad and a browser text box. Every character should appear once, in order, nothing missing or doubled, and the last `x` should be gone again. Clear the line afterwards. If characters are missing in Claude Code's terminal, tell us: the `inject` default may need to change to `vk`. It refuses to type into a window that runs as administrator, a password box, a full screen app, a Jarvis window or no window; it prints a fixed sentence and the program name only.

#### Record a trace and tune offline

`keytrace` records your hands for a few minutes and `keyreplay` replays the recording offline, so the detector can be tuned without the camera. Do this when the air tap misses your taps or makes extra ones.

**What is recorded.** The file holds, for each camera frame, the time, which hand is which, the handedness score, and 21 landmark points per hand (x, y, z in frame widths), plus the list of segments and, for a drill, the finger and key index of each prompt (as numbers). **No picture, no window, no key press and no character you typed.** Nothing is sent anywhere; the file stays where you write it. Because the movements of a `type` segment could in principle be read back into letters, type only the phrases the console shows, and delete a recording like any other file when you are done. `keytrace` refuses to run without `--yes-record`.

`keytrace` opens the camera itself, and the hand helper must not be holding it. In Claude Code run `/jarvis hands pause` (or `/jarvis hands off`), then in PowerShell:

```powershell
& $py -m jarvis_hands keytrace --yes-record --segments rest:20,wave:20,rest:20 --out "$env:USERPROFILE\kt-room.npz"
```

When it is done run `/jarvis hands resume` (or `on`). It needs the hand model from `/jarvis setup hands`.

Segments are `kind:seconds`, comma separated, at most 600 seconds in all. The kinds are `place` (hold both hands over the keys, still), `type` (type the practice phrases shown), `tap` (tap or pinch each finger in turn), `rest` (hands in view, do nothing), `wave` (wave, open and close your hands, tap nothing) and `drill` (air only: the console names a finger every 1.2 seconds, never a reach key). A 3 second `place` is added before a `type`, `tap` or `drill` segment that does not follow another one. `--seconds N` alone records one stretch of `drill` (air) or `type` (pinch); the default is 120. Other options: `--press air|pinch`, `--camera INDEX|NAME`, `--countdown SECONDS` (default 5), `--data-dir D` (where the hand model is). `--out` must end in `.npz`. It will not overwrite a file; Ctrl+C stops early and saves what it has.

Two recordings answer the questions that matter:

1. **The room** (about a minute): `--segments rest:20,wave:20,rest:20`. The report should show at least 26 fps by the median frame gap and a landmark noise under 0.022 (level `ok`), and the resting lift of each finger at least 0.40 with the posture gate closed under 2% of the time.
2. **The drill** (about six minutes): `--segments drill:60,drill:60,drill:60,drill:60,drill:60,rest:20,wave:20`, which is 250 prompts. Tap the finger the console names over its home key. The drill of `keytrace` names fingers only, with no reach keys, so the report's reach line says `(no prompts)`; the reach keys are measured by the practice (`/jarvis hands keyboard practice`), not here.

Record each with its own `--out`, then replay:

```powershell
& $py -m jarvis_hands keyreplay "$env:USERPROFILE\kt-drill.npz"
```

The replay of a five minute recording takes about a minute (49 seconds for a synthetic one on the development machine). The report prints the frame rate and landmark noise, the placement, the camera level, the taps counted and their latency, the hands in view, the drill (hits, wrong finger, extra and missed taps, and why), the false taps, a table per finger (taps, prompts, hits, recall, depth, threshold), the phantoms in each rest, the posture at rest and the key accuracy of each aim rule. It also prints the **decision rule**: index and middle recall in the drill at least 95%, extra taps at most 3% of the prompts, reach-key recall at least 85% (when the recording has reach prompts), and ring and pinky recall reported against a target of 75%, each marked `met` or `below`. Those bars come from the simulation (see [the limits](#honest-limits)). A recording is not a live session, so it says nothing about the review box or Insert.

Options: `--press air|pinch` (replay with the other method than the file was recorded for), `--set NAME=VALUE` (try a tuning field, for example `air_theta_k=6.5`; inside its range only; repeat for more), `--csv OUT.csv` (the per-frame numbers), `--no-suggest` (skip the search for better values), `--data-dir D`, and:

- `--write` merges the suggested values, and your `--set` values, into `%USERPROFILE%\.jarvis\hands\keyboard-tuning.json`. The write is atomic and every value is clamped to the range in [the tuning table](#the-tuning-file). It can only name tuning fields, so it cannot relax a safety rule. The keyboard reads the file each time it opens.

A suggestion is only made from a drill of at least 20 prompts, and thresholds only when the recording does not already meet the target, because values found by shaving two stray taps off one recording are a fit and not a setting. Record twice before you trust a change.

A practice leaves its own trace in `%USERPROFILE%\.jarvis\hands\keyboard-trace-<time>.npz`; `keyreplay` reads those too.

#### What only your PC can settle

These have not been run against real Windows or a real camera. Please report what you see.

- That typed characters, including Hebrew, reach Windows Terminal with Claude Code, Notepad and a browser (`keytest`).
- That reading the window in front takes 2 ms or less, and what Windows says for a full screen game or a presentation.
- That `osk.exe` starts for the `windows` method.
- That an administrator window is refused for real (Windows drops the keys of a lower-privilege program silently, so the helper's own check is the only protection) and that a classic password box is recognised.
- How the keyboard looks and how fast it draws on your display; Hebrew glyph widths.
- How often your hands, parked on Insert or Send, make a phantom tap that three in a row would complete.
- The real landmark noise, frame rate and phantom rate of your camera and room, and whether the ladder thresholds (26 and 13 frames a second, 0.022 and 0.036) suit them.
- How the pointer comes back after a close (the 0.6 second wait feels right or not).
- Whether a changed `handKeyboard` option applies without restarting Claude Code, and whether the keyboard settings you choose survive a new session.

### Honest limits

- **No number here is measured on a real hand.** The figures below come from a kinematic model in this repository, which has no depth error, no occlusion and no motion blur. Real MediaPipe landmarks are noisier and coupled between fingers. Treat them as an upper bound.
- **Air tapping is far less reliable than touching a key.** On the model, with a clean camera (landmark noise 0.001 frame widths) index and middle taps are seen about nine times in ten (0.92) with about three false taps per hundred real ones; ring taps 0.83 and pinky taps 0.75; the right key is typed for 91% of detected taps. At landmark noise 0.002 or more, no combination tried met the decision rule, with or without the ladder. What your camera gives is what `keytrace` and `keyreplay` measure.
- **Pace.** The detector is built for about one key a second, including aiming. On the model, with two hands at 0.9 keys a second 0.92 of taps are seen, at 2.1 keys a second 0.79, at 3.6 keys a second 0.59, and no setting removes that ceiling. A fast typist will have to slow down. One hand is slower still: move the hand to the key, let it stop, then tap, about one key every two seconds. Plan for a short sentence to take a minute.
- **Raise and curve your fingers**, hold the hand roughly still while a finger taps, and tap with some decision. A relaxed hover with drooping fingers is invisible to the camera. A hand pointing straight at the camera (within about 14 degrees of the line of sight) has no usable axis and types nothing.
- **A fast roll loses a letter.** Two neighbouring fingers within about 100 ms ("er", "th" on one hand) keep both letters only about three times in four on the model (0.17 at 0 ms, 0.77 at 100 ms, 0.97 at 150 ms). The detector commits one tap per hand per 0.06 seconds.
- **Phantom taps happen, and they land in the box.** While you talk with your hands in view the model makes 14 to 29 phantom letters a minute in the worst case. Backspace and Clear are the cost. Nothing reaches another window until three deliberate taps on Insert, but the box can fill with letters you did not mean, and those go to the window if you Insert them.
- **An accidental Insert is possible in principle.** On an invented phantom model, 20 phantom taps a minute spread over the keys give about 0.013 accidental Inserts an hour. For a hand parked on the key, the plain count of three taps was not good enough in replays; the rule above (a hand at rest, one finger, two firm taps) completed none in 43.1 hours of replayed phantoms and 2 in 8.4 hours at phantom rates above the practice gate. These are simulation figures. Your PC will say what it does in practice.
- **Jitter bursts and dropped frames** cost recall and make extra taps. Half a second of extra landmark noise makes about one phantom per hand per burst, and the banner does not see it.
- **The box is on screen in clear text** (only `Priv` hides it), and in the helper's memory while the keyboard is open. Python cannot scrub a freed string; this is not a claim that the text is erased. A screen capture shows it.
- **Password fields** in browsers, Electron apps and terminals cannot be recognised; only classic Windows password boxes can. The keyboard is not for passwords. A keyboard covered by something Windows does not report cannot be seen either.
- **No autocorrect, no suggestions, no learning.** A wrong key stays wrong until you press Backspace. Hebrew has the same limits.
- **Your arms tire** when held in the air. Rest your forearms.
- **A normal setup for a short message, a command or an answer**, not for long text, code or speed.

### Troubleshooting

| Problem | What to try |
| --- | --- |
| `The air keyboard is off. Turn it on in the Jarvis plugin settings (handKeyboard).` | `/plugin configure jarvis@jarvis-claude-mod`, set **Air keyboard** to `on`. Restart Claude Code if it still says so. |
| `The hand helper is too old for the air keyboard: run /jarvis setup hands.` | Run `/jarvis setup hands`. |
| `Practice first: run /jarvis hands keyboard practice once with the air method.` | Run the practice. |
| `The last air practice had too many false or missed taps; practice again.` | Light the room, raise and curve your fingers, tap with decision, and practise again. If it keeps failing, use `/jarvis hands keyboard press pinch`. |
| `The last air practice found the air tap unusable on this camera.` | The camera is too slow or shaky for the air tap. Light the room or use the pinch method. |
| `Hand control is still starting.` / `Hand control is paused; resume it first.` | `/jarvis hands on` or `/jarvis hands resume`. |
| `The desktop is locked or showing a system prompt.` | Unlock or close the prompt. |
| `The air keyboard needs the on-screen overlay.` / `Text rendering is unavailable...` | The overlay or its font is missing. Run `/jarvis setup hands`, and do not start hand control with `--no-overlay`. |
| The warm-up never finishes | Tap only the finger the strip names, over its home key, with the others still and your hand steady. Raise and curve the fingers. After 90 seconds it closes (`air_unreliable` if you did tap). Try pinch. |
| Taps are not seen | Light your hands from the front, raise the finger first and tap a little firmer. Record the room (`keytrace`, below) and read the fps, noise and posture lines of `keyreplay`. |
| Letters appear that you did not tap | Stop gesturing over the keyboard, tap `Clear`, and look at the phantoms line of `keyreplay`. Raise `air_theta_k` or use pinch. |
| Insert does nothing | Hold your hand still, tap with one finger three times, firmly, with nothing in between. The strip names the missing part. |
| Insert stops half way | The strip says why (window changed, you used the keyboard or mouse, a window that cannot be typed into, a password box, a covered screen, the keyboard could not be drawn, you stopped it, it took too long). The rest is in the box; tap Insert three times again. |
| Nothing arrives in the window | The window runs as administrator, is a password box, is covered by a full screen app, or changed. Click a normal window with the real mouse. |
| The pointer does not come back after closing | Lower your hands out of the camera's view until the pointer is back (it needs 0.6 seconds with no hand seen). |
| It closed on its own | Hands out of view for 30 seconds (2 minutes with text), no key for 5 minutes, hand control paused, the screen locked, or the camera stopped. The strip or a toast says which. |
| `keytrace` says the camera is in use | `/jarvis hands pause` first, and `/jarvis hands resume` afterwards. |
| Hebrew letters come out wrong in the box | Tap `Lang` (the strip shows `EN` or `HE`), or set `/jarvis hands keyboard layout he`. |

## For developers

This part is the map of the code: the modules, the safety rules and the tests that hold each one, the constants and the files, how to add a layout, and where the detector came from. The design record is [notes/2026-10-08-hands-keyboard-design.md](notes/2026-10-08-hands-keyboard-design.md); its SR numbers are the rule names used below.

### Module map

All keyboard code is under `plugin/hands/src/jarvis_hands/keyboard/` unless a path says otherwise. A keyboard module imports downward without a cycle, and a test checks that no keyboard module imports a pointer module.

| Module | What it is |
| --- | --- |
| `controller.py` | The one object `runtime.py` talks to: `command`, `frame`, `close`, `pointer_frame`, `status`. Opens, drives and closes a session; owns the refusals (with fixed sentences), the practice markers, the overlay health policy, the pointer quarantine after a close and the exception boundary. The only module (with the runtime and the CLI) that imports the protocol and the overlay state. |
| `session.py` | One session, frame by frame: placing, warm-up, typing, holds, the tap queue, the storm freeze, the both-fists exit, idle timers, the strip text and hints, the ladder wiring, practice mode. |
| `review.py` | The review machine: the box, the Insert, Clear, Close and Send guards, the run state, the fixed strip sentences (`REVIEW_TEXT`). |
| `compose.py` | The box: 200 characters, edited at its end, no `__str__`, no iteration, `repr` shows a length. `insert_check`. |
| `sink.py` | The last gate before Windows: the key lane (direct mode) and the run lane (review mode), holds, yield, target checks, breakers, run budgets. |
| `press_air.py` | The air tap detector (below). Pure Python: `math` and `collections` only, no clock, no I/O. |
| `press_pinch.py` | The pinch press. `press.py` is the registry (`make_press`, which refuses `air` with a direct commit). |
| `hands.py` | Hand tracking for the keyboard: identity across frames, finger features, levelling, the per-hand `HandSample`. |
| `plane.py`, `layout.py` | Where the keys lie in front of the camera (`place_plane`), and the key tables with their lookup (`key_at`). |
| `warmup.py`, `ladder.py` | The warm-up that sets each finger's threshold and arms the session; the `ok` / `degraded` / `off` ladder. |
| `practice.py`, `trace.py` | The practice script, scoring and markers; the tap log, the trace writer and reader, and clean-up. |
| `settings.py`, `tuning.py`, `limits.py` | The settings the mod sends; the accuracy numbers and their file; the safety constants (code only). |
| `types.py` | Names and shapes shared across modules. |
| `keytest.py`, `keytrace.py`, `keyreplay.py` | The console tools. Only `cli.py` imports them, lazily. |
| `synth.py`, `rig.py` | Test support shipped in the package: synthetic hands that type (`AirTypist`), and a session, sink and fake desktop wired together (`KbRig`). |
| `desktop/keys.py` | The allow-list and the input events (`KeyStroke`, `events_for`). Standard library only. |
| `desktop/windows.py`, `desktop/base.py`, `desktop/fake.py` | `send_keys`, `key_target`, the ledger of unreleased keys, foreign-input probe, `open_os_keyboard`; the protocol; the fake. |
| `overlay/keyboard_render.py`, `overlay/text.py`, `overlay/windows.py` | Pure drawing of the keyboard layer; wrapping and the display order of mixed Hebrew and English; the layered window and its health. |
| `logs.py` | The keyboard scrub: while a session is open, exception text never reaches a log, only the type name. |
| `runtime.py`, `protocol.py`, `cli.py` | The `keyboard` command, the status block and the lifecycle hooks (pause, lock, camera loss close the session); the validators; the subparsers. |
| `plugin/hooks/hands-keyboard.ts` | The mod side: the option, `/jarvis hands keyboard`, the tool's three actions, toasts and status, with fixed strings. `hands.ts`, `hands-gate.ts` and `plugin.json` carry small edits. |
| `plugin/protocol/hands.schema.json` | The `keyboard` command, event and status. |

A frame goes: camera and tracker (the hand helper) -> `controller.frame` -> `hands.HandTracker` -> the press (`AirTapPress` or `PinchPress`) -> `KeyboardSession` (queue, holds, warm-up, ladder) -> in review mode the `ReviewMachine` (box and guards) -> a run step -> `KeySink` (run lane) -> `KeyDesktop.send_keys` -> `SendInput`. The overlay draws the session's view. The pointer path (`engine`, `executor`) gets no frames while a session is open; `pointer_frame` hands it back only after a quarantine.

### Safety rules and their tests

The mod only ever makes Claude Code stricter. A keyboard is the first part of Jarvis that adds an ability (typing), so each rule below removes ability, bounds it or makes it visible. Test files are in `plugin/hands/tests/`, `hands-keyboard.test.ts` in `plugin/hooks/`; the short ids (`s42`, `u42b`, `x26`) are the test-name prefixes, which are the design's test ids.

| Rule | Where | Tests |
| --- | --- | --- |
| **Opt-in** (SR1). The `handKeyboard` option is the only switch. The helper's `enabled` starts false and is set by the mod from the option at every `hello`. No command and no tool action sets it. | `hands-keyboard.ts` `sync`; the controller's first refusal | `hands-keyboard.test.ts` M1 to M3; `test_kb_controller.py` `k4_*`; `test_kb_settings.py` |
| **Nothing is typed before arming** (SR2, SR36). Placing, then every needed finger's warm-up act. Warm-up taps add nothing to the box. | `session.py`, `warmup.py` | `test_kb_air_session.py` x26 to x29, x39; `test_kb_session.py` `s19_*`; `test_kb_warmup_phantom.py` x53; `test_kb_warmup_user.py` x54 |
| **Allow-list in four layers** (SR3, SR28): `KeyStroke` construction, `events_for`, `KeySink`, the Windows desktop. 85 characters and three controls; the box alphabet is those plus space. | `desktop/keys.py`, `sink.py`, `compose.py`, `windows.py` | `test_kb_keys.py`; `test_kb_sink.py` `s3_*` and `a_stroke_forced_past_the_constructor_*`; `test_kb_e2e_fuzz.py` s3; `test_kb_desktop.py` w2 |
| **No key left down** (SR4). One atomic `SendInput` per stroke; a partial insert sends the missing ups at once and parks the rest in a ledger retried on every later call and at close. | `windows.py`, controller | `test_kb_desktop.py` w4 to w6, w12 |
| **Rate layers** (SR5, SR34). One event per tap; `MIN_GAP_S`, `QUEUE_MAX`, `QUEUE_AGE_S`; the 12th key in 2 s closes `runaway` in direct mode and freezes taps for 3 s in review mode; sink backstops of 16 (key lane) and 80 (run lane) sends in 2 s. | `session.py`, `review.py`, `sink.py` | `test_kb_session.py` b8, s1; `test_kb_safety.py` s57; `test_kb_e2e_air.py` x42; `test_kb_sink.py` s2, s48 |
| **Holds are not breakers** (SR6). A hold drops taps, dims the keyboard and re-latches every finger. Order: blocked, password, covered, overlay, focus, yield; `slow` is the session's own. | `sink.gate`, `session.py` | `test_kb_sink.py` `the_holds_have_the_order_of_sr6`, s11 to s15; `test_kb_air_session.py` x41 |
| **Yield** (SR7). Foreign input holds 1.5 s; Ctrl, Alt or Win while down; a failing probe counts as foreign. | `sink.py`, `windows.py` | `test_kb_sink.py` s8 to s10; `test_kb_desktop.py` w7, w8, w11 |
| **Targets** (SR8). Never an elevated window (absolute rule: High or System integrity whatever the helper's level, anything above the helper's, an unreadable token, any window if the helper cannot read its own), a shell surface, Jarvis's own windows, no window, a classic password edit, a covered desktop; 0.5 s after a focus change; or when the foreground changed between resolving and sending. | `windows.py` `key_target`, `sink.py` | `test_kb_desktop.py` w9, w13, w15; `test_kb_sink.py` s11 to s15, s44 |
| **Visible when typing** (SR9, SR30). Real injection needs a live overlay with a recent good draw; otherwise hold `overlay` (a run aborts), and close `no_overlay` after 2 s. | controller `_overlay_ok`, `sink.py` | `test_kb_controller.py` `k2_*`; `test_kb_sink.py` s14, s45; `test_kb_safety.py` s45 |
| **Close semantics** (SR10). Every close releases keys, discards the box and any run, and quarantines the pointer. Nothing reopens or resumes by itself. | controller, `runtime.py` | `test_kb_runtime.py` rt7, rt8, rt10, s21, k43, q5; `test_kb_controller.py` q1 to q4, q40; `test_kb_safety.py` s49 |
| **Hands-only exit** (SR12). Both fists for 1.0 s, in every phase. | `session.py` | `test_kb_session.py` s17 |
| **Privacy** (SR13, SR26, SR37). No typed character, key, box text, window title or executable name reaches a log, event, status, file or the mod. Exception text is data too: while a session is open only the type name is logged. The tap log, trace and markers exist in practice only and hold numbers. | everywhere; `logs.py` | `test_kb_static.py` `no_log_call_or_exception_message_interpolates_typed_content`; `test_kb_privacy.py`; `test_kb_runtime.py` exc_text; `test_kb_controller.py` s22b, k9; `test_kb_desktop.py` sr13; `hands-keyboard.test.ts` M43, M44 |
| **Files cannot relax a rule** (SR14, SR31). The tuning file has clamps and floors, falls back on anything wrong, and cannot name a safety constant. | `tuning.py`, `limits.py` | `test_kb_tuning.py`; `test_kb_limits.py`; `test_kb_air.py` x25 |
| **Passwords** (SR15). A classic password edit holds `password`; it never switches to a mode that keeps typing. | `windows.py`, `sink.py` | `test_kb_desktop.py` w9; `test_kb_sink.py` s12 |
| **Authority** (SR16, SR27, SR38). No command carries text or a threshold. The `hands` tool can open, practise and close; it cannot enable, change `press` or `commit`, insert, send or read. Only the confirming third tap starts a run. | `protocol.py`, schema, controller, mod, `review.py` | `hands-keyboard.test.ts` M1 to M4, M41; `test_kb_review.py` `a_run_is_started_from_exactly_one_place`, s50; `test_kb_safety.py` s50 |
| **The pointer path is untouched** (SR17). The frozen files have an empty diff against `ed04e05`; with the keyboard closed `_process` submits what it did before. | CI | `test_kb_pointer_diff.py` p7, p45; `test_kb_static.py` frozen files (with `KB_FROZEN_BASE`) |
| **Threads** (SR18). `send_keys` runs on the runtime thread under the lock and is timed; the command thread only changes session state. | controller, `runtime.py` | `test_kb_runtime.py` k5, k10, k44, k49 |
| **Staging** (SR21). In review mode a tap can only change the box. A stroke reaches the desktop only from a run, and a run starts only at the confirming tap. Each lane of the sink is dead in the other mode. | `session.py`, `sink.py`, `review.py` | `test_kb_sink.py` `s48_each_lane_is_dead_in_the_other_mode`; `test_kb_safety.py` s40, s50; `test_kb_review.py` n41 to n44 |
| **Insert is harder than a letter** (SR22). 3 taps (`INSERT_TAPS`, a floor), 0.25 s apart, within 6.0 s; a hand at rest, one finger, two firm taps; no other act between; an empty box cannot arm. | `review.py`, `session.py`, `limits.py` | `test_kb_review.py` u42, u42b, u43; `test_kb_safety.py` n42 to n44, n47; `test_kb_limits.py`; `test_kb_parked.py` n48 (`KB_FULL=1`) |
| **A run is bounded and revocable** (SR23). Pinned `(hwnd, pid)`; a fresh target read before every character; a stop tap; at most 200 characters and 30 s; 80 sends in 2 s breaker; 12 runs and 600 characters in any minute and 0.5 s between runs. | `sink.py`, `review.py` | `test_kb_sink.py` s43 to s48, s47b; `test_kb_safety.py` s42 to s47, b40, b41 |
| **No retype, no loss, no guess** (SR24). `sent` advances only on `sent` or `maybe`; an exception after delivery is `maybe`; the box keeps the rest; a re-Insert types only the rest. | `review.py`, `sink.py`, `windows.py` | `test_kb_sink.py` s46b; `test_kb_safety.py` s46, s46b; `test_kb_desktop.py` w16 |
| **Send is narrower than Enter** (SR25). After a completed text run, same window, box empty, each tap within 10 s, the same evidence as Insert; refused for `/` and `!`; once. | `review.py`, `sink.py` | `test_kb_review.py` s51, u45, n45, n46; `test_kb_safety.py` s51 |
| **The air method needs review** (SR29). `make_press("air", review=False)` raises; settings and the controller refuse air with direct; the mod refuses direct unless the method is pinch. | `press.py`, `settings.py`, controller, mod | `test_kb_settings.py`; `test_kb_controller.py` x48, s58; `hands-keyboard.test.ts` M8, M40 |
| **The ladder is visible and one-way** (SR32). `degraded` and `off` always show the banner; `off` is terminal; the switch to pinch types nothing and waits for a run to end. | `ladder.py`, `session.py` | `test_kb_air_session.py` x31 to x33, x38; `test_kb_e2e_air.py` x31, x32; `test_kb_run_ladder.py` |
| **A tap in progress is never completed by a state change** (SR33). Reset cuts the finger's history. | `press_air.py` | `test_kb_air.py` x5, x6, x18 |
| **Whole-hand motion is not typing** (SR35). A closing or opening hand, a moving hand, a relaxed hand, a doubtful label and a tremor each suppress taps. | `press_air.py`, `limits.py` | `test_kb_air.py` x11 to x14, x17 |

The documentation rules (SR19) are checked by review and by one test: `test_kb_static.py` `no_document_states_the_number_of_keys`, which scans every `.md` file in the repository. Write "every key" or name the layout, never a count of keys.

### Constants

The safety constants are code only, in `limits.py`. Nothing loads or overrides them: no file, no command, no setting. `test_kb_limits.py` pins every value and the relations and floors between them, so a change there needs a deliberate edit of the test. The ones a reader most often wants:

| Constant | Value | Meaning |
| --- | --- | --- |
| `INSERT_TAPS`, `SEND_TAPS` | 3, 3 | Taps for Insert and Send. A floor of 3. |
| `GUARD_MIN_S`, `GUARD_MAX_S` | 0.25 s, 6.0 s | Spacing of two counted guard taps; the window to complete a guard. |
| `GUARD_STILL_SPEED` | 0.10 frame widths/s | An air tap counts toward Insert and Send only from a hand at or under this speed. Below `Tuning.still_speed` (0.15), which gates placing. |
| `GUARD_FIRM_CONF`, `GUARD_FIRM_TAPS` | 0.8, 2 | Of one finger's guard taps, at least 2 must be this firm. |
| `COMPOSE_MAX` | 200 | Box and run length. |
| `INSERT_GAP_S`, `INSERT_MAX_S`, `STOP_ARM_S` | 0.030 s, 30.0 s, 0.5 s | Spacing of run characters; longest run; the earliest a Stop tap counts. |
| `INSERT_BACKSTOP_N/S`, `BACKSTOP_N/S` | 80 / 2.0 s, 16 / 2.0 s | The sink's run-lane and key-lane breakers. |
| `RUN_COOLDOWN_S`, `RUN_MAX_PER_MIN`, `RUN_MAX_CHARS_PER_MIN` | 0.5 s, 12, 600 | Run budgets. |
| `SEND_WINDOW_S`, `SEND_REFUSE_FIRST` | 10.0 s, `/` and `!` | Send's window after the Insert; first characters that refuse it. |
| `STORM_N/S`, `STORM_FREEZE_S` | 12 / 2.0 s, 3.0 s | The storm breaker. |
| `YIELD_S`, `FOCUS_SETTLE_S`, `HEALTH_MAX_AGE_S`, `HEALTH_CLOSE_S` | 1.5, 0.5, 0.75, 2.0 s | Hold lengths and overlay health. |
| `NO_KEY_CLOSE_S`, `PLACE_TIMEOUT_S`, `ARM_TIMEOUT_S`, `REVIEW_IDLE_S`, `FIST_EXIT_S` | 300, 30, 90, 120, 1.0 s | Close timers. |
| `AIR_LEVEL_FPS_DEGRADED/OFF` | 26, 13 | The ladder. |
| `AIR_LEVEL_NOISE_DEGRADED/OFF` | 0.022, 0.036 | The ladder. |
| `AIR_THETA_FLOOR`, `AIR_THETA_K_FLOOR`, `AIR_VETO_FLOOR` | 0.07, 3.5, 0.5 | Floors under the tuning clamps. |
| `AIR_PRACTICE_*` | 3.0 phantoms/min, recall 0.70 from 24 drill prompts, rest 60 s | The air marker bounds. |
| `AIR_WARMUP_*` | margin 0.5, depth 0.10, aim 0.6 key, 3 strays, 1.0 s gap | The warm-up cannot be loosened by a file. |

### The tuning file

`%USERPROFILE%\.jarvis\hands\keyboard-tuning.json` holds the accuracy numbers a recording may retune: `{"version": 1, "pinch": {...}, "plane": {...}, "hands": {...}, "air": {...}}`. Every field of `Tuning` has a default and a clamp; a value outside its clamp is not pulled to the edge but replaced by the default for that field, so a forged file cannot land just above a floor. A corrupt, oversized (over 64 KB), mistyped or unknown file gives the defaults and one log line that names only our own fields. Loading never raises and never writes. Unknown keys, and keys in the wrong group, are skipped. `keyreplay --write` is the only writer, and it merges and clamps. The controller reads the file at every open.

The air fields and their defaults and ranges:

| Field | Default | Range | What it does |
| --- | --- | --- | --- |
| `air_theta_k` | 5 | 4 to 8 | Nominal threshold as a multiple of the finger's noise estimate. |
| `air_theta_min`, `air_theta_max` | 0.10, 0.25 | 0.08 to 0.20, 0.18 to 0.40 | Bounds of the nominal threshold. |
| `air_depth_frac` | 0.5 | 0.4 to 0.7 | After the warm-up a finger's threshold is this share of the depth its warm-up tap reached, kept between three quarters of the nominal threshold and the nominal threshold. |
| `air_back_s`, `air_rise_win_s`, `air_fall_win_s`, `air_return_frac` | 0.20, 0.16, 0.12, 0.5 | 0.15 to 0.30, 0.10 to 0.25, 0.08 to 0.20, 0.35 to 0.65 | The windows of the peak search, and how far the finger must come back. |
| `air_width_min_s`, `air_width_max_s` | 0.04, 0.30 | 0.03 to 0.08, 0.20 to 0.45 | Narrowest and widest dip (seconds) that is a tap. |
| `air_speed_gate`, `air_vmax_gate` | 0.5, 0.5 | 0.3 to 1.0 | Hand speed gates (frame widths a second). |
| `air_veto_ratio` | 0.7 | 0.5 to 0.85 | A dip under this share of a neighbouring finger's dip at the same moment is taken as coupling and dropped. |
| `air_aim`, `air_aim_speed` | `auto`, 0.05 | `auto`, `onset`, `commit`; 0.02 to 0.15 | Which fingertip position names the key, and the speed at which `auto` switches. |

The floors in `limits.py` (threshold 0.07, `air_theta_k` 3.5, veto ratio 0.5) sit under these clamps and are applied again inside the detector, so a `Tuning` built in code cannot go below them either. The pinch, plane and hands fields (`close`, `open`, `pitch`, `still_speed`, `level_palm`, and so on) are in `tuning.py` with their ranges. Safety constants are not representable in the file; `test_kb_tuning.py` `no_safety_constant_is_representable_in_the_file` and `a_hostile_file_cannot_change_anything_but_accuracy_numbers` hold that.

### The air detector and its reference

`press_air.py` is a step-for-step port of the simulation's reference detector, `airtap_ref.py`. The reference and the simulations that chose its numbers live outside the repository (at build time in `/tmp/claude-0/kbd/sim-air/`, with the generator of the goldens); the repository holds the port and the fixtures. The comments S0 to S13 in the code are the design's steps. The detector measures a finger's **lift** (how far the PIP, DIP and tip stand along the hand's own axis) against that finger's rest value, with what the whole hand did removed. A tap is a **completed peak** of that depth, found when the finger is already coming back. It fires only if every gate on the hand (warm, score, hold, speed, posture, coherence) and on the candidate (narrow, wide, refractory, pinched, motion) agrees; a neighbour finger that dipped nearly as far vetoes it, and five commits of a hand in half a second are a tremor. The key is the one under that finger's tip before the dip, chosen by the `auto` aim rule, never at the peak.

Two golden fixtures pin the port against the reference's recorded events, rejects and tap log: `tests/data/air_golden_typing.json` (536 frames, 27 events) and `tests/data/air_golden_neg.json` (9 events). They are tests X2 and X3 in `test_kb_air.py`. **Change a number or an order in `press_air.py` and read those two tests first.** The goldens were generated from the reference, so a change that is right on purpose means regenerating them from a changed reference, not editing them. Retuned accuracy numbers belong in `tuning.py` and the tuning file, not in the detector.

While the warm-up runs, `theta_of` returns the nominal threshold, never a more sensitive one: at landmark noise 0.002 a 4-sigma bar armed 4 of 12 two-hand runs and the typing bar 12 of 12. A hand whose wrist-to-middle-knuckle axis shows under a quarter of its palm in the picture (`_MIN_AXIS` in `hands.py`) has no axis, so its lift is 0 and it is not levelled.

### The mod and the protocol

The contract is `plugin/protocol/hands.schema.json`. The helper says `keyboard` in the `capabilities` of its `hello`.

- **Command** `keyboard`: `{action: start | practice | stop | recenter | private | public}` or `{action: configure, settings: {...}}`. There is no action that inserts, sends or clears, and no field that carries text. `configure` takes `enabled`, `press`, `commit`, `layout`, `size`, `reach`, `dock`, `idleS`, `inject`, `enter`; it is all or nothing, and its errors name the setting, never the value. `settings.py` and `protocol.py` keep two copies of the ranges, and `test_kb_settings.py` checks they accept the same bodies.
- **Event** `keyboard`: `state` (`open`, `practice`, `closed`), `phase`, `reason` of a close, `hold`, `lang`, `press`, `level`, `commit`, `private`, `review` (`state`, `chars` and the result of an Insert as counts and enums), `discarded` and `practice` (numbers). Change events come at most twice a second; a closed event is never held back. The mod rebuilds the event from the keys it knows, so a stray text field never travels.
- **Status**: a `keyboard` block with the same kind of fields, plus `practiced`, `airFps` and `airNoise`.
- The close reasons are `command`, `close_key`, `fists`, `idle`, `paused`, `desktop_locked`, `runaway`, `no_overlay`, `camera`, `disabled`, `error`, `input_blocked`, `air_unreliable`.

The mod's side is `hands-keyboard.ts`: every string it says is a fixed string with number placeholders. Nothing it shows is built from text the helper sent. `enabled` is sent from the plugin option only; the chosen settings are kept in the mod store (`handsKeyboard*`) and re-sent at every `hello`. Direct typing is sent only with the pinch method (the stricter review box is sent otherwise).

### Add a layout

A layout here is the table of keys and the characters they type. The direct layout's `ROW_TABLE` in `layout.py` is the single source of truth; the review layout is derived from it, so a letter cannot be on two different cells. A cell is `(kind, width, English, Hebrew)`; every row is exactly 11.5 key units wide. To change the keys or add a language:

1. **Edit the table** in `layout.py`. Keep the direct layout's key indices stable: a review key takes its index from `(kind, English character)` in the direct table, never from the flattened review order, so traces and practice targets stay comparable. New cells are numbered last.
2. **Keep the allow-list in step.** The characters the layout can produce, in every language and shift state, must equal `ALLOWED_CHARS` in `desktop/keys.py` (a test, `u1_the_characters_the_layout_can_produce_are_the_allow_list`, holds it). Widening the alphabet is a named constant in `desktop/keys.py` (`ALLOWED_CHARS` and `COMPOSE_CHARS`) plus, in the same commit: the layout, the bake cache, `bidi_display` in `overlay/text.py` if the script needs it, the fuzz test (`test_kb_e2e_fuzz.py` s3), `test_kb_keys.py` and `test_kb_compose.py`. Keep `insert_check`'s refusal of a leading `!` and the Send refusal of `/` and `!`. A symbols page (digits, `!`) also needs a second confirmation; see the design's step-2 list.
3. **A new language** also needs: `Lang` in `types.py`; the `layout` choices in `settings.py`, `protocol.py`, the schema (`KeyboardLang`) and `hands-keyboard.ts`; the language id mapping in `controller.py` (`layout: auto` reads the foreground thread's keyboard layout); phrases in `practice.py` (`PHRASES`); the Windows `vk` lookup keeps working through `_layout_id`; the overlay font must cover the glyphs (the draw error codes include `font`).
4. **Never state a count of keys** in a test, a comment or a document. Derive it from `Layout.count` or `KEY_COUNT`; `test_kb_layout.py` is the one place that pins the numbers, and `test_kb_static.py` scans for the rest.
5. Run `test_kb_layout.py`, `test_kb_plane.py`, `test_kb_render.py`, `test_kb_keys.py` and `test_kb_static.py`, then the whole keyboard family.

### Tests

The keyboard tests are `plugin/hands/tests/test_kb_*.py` and `plugin/hooks/hands-keyboard.test.ts`. They run on Windows, macOS and Ubuntu in CI. The Windows code is tested against `FakeWin32`, a stand-in that checks every call against its declared prototype, and the rest against `FakeDesktop` and a recording overlay. **No test needs a camera, a display or a real keyboard, and none has run against real Windows input or a real overlay window.** Timing tests use injected clocks and compare times from `jarvis_hands.clock.now`, never real sleeps (macOS CI oversleeps by tens of milliseconds).

| Family | Files |
| --- | --- |
| Contracts and static lint | `test_kb_keys`, `test_kb_limits`, `test_kb_types`, `test_kb_settings`, `test_kb_tuning`, `test_kb_static` (imports, privacy lint, frozen files, key counts, test-id length) |
| Layout and tracking (U, A) | `test_kb_layout`, `test_kb_plane`, `test_kb_hands`, `test_kb_press`, `test_kb_scenarios` |
| Air detector (X) | `test_kb_air` (the goldens X2 and X3, gates, statistical rows), `test_kb_air_session` (warm-up, ladder, hints), `test_kb_warmup_phantom`, `test_kb_warmup_user` |
| Box, guards, run (N, S, B) | `test_kb_compose`, `test_kb_review`, `test_kb_sink`, `test_kb_safety`, `test_kb_session`, `test_kb_privacy` |
| End to end | `test_kb_e2e_air`, `test_kb_e2e_pinch`, `test_kb_e2e_fuzz`, `test_kb_parked` (N48), `test_kb_run_fake`, `test_kb_run_ladder` |
| Windows, overlay, controller | `test_kb_desktop` (and `test_desktop_windows`), `test_kb_render`, `test_kb_text`, `test_kb_controller`, `test_kb_runtime`, `test_kb_pointer_diff` |
| Practice and tools | `test_kb_practice`, `test_kb_practice_session`, `test_kb_keytest`, `test_kb_keytrace`, `test_kb_keyreplay` |
| Mod | `hands-keyboard.test.ts`, with two assertions in `hands.test.ts` |

How to run them, and `KB_FULL=1` for the long statistical rows, `KB_FROZEN_BASE` for the frozen-file check, and `JARVIS_HANDS_MODELS_DIR` for the real-model tests, are in [DEVELOPING.md](DEVELOPING.md#hand-helper-python).

Known limits of the suite:

- **Runtime.** The whole helper suite took 10 minutes 48 seconds on the development machine after it was trimmed (the target was 8), and about 16 minutes is expected on the Windows runner. Before the trim, CI took 28 minutes on Windows, 13 on macOS and 17 on Ubuntu for the first run, and 40 on Windows and 15 on macOS for the run after it. Running the suite under `pytest-xdist` in CI was suggested and has not been done. `KB_FULL=1` is much longer (the N48 run alone is about an hour).
- **Runner differences.** The first CI run found three failures that only appear off Linux: a render test that looked for pixels at fixed columns (the fonts differ: Segoe UI on Windows, an Arial-class font on macOS, DejaVu Sans on Linux), a log-size test that counted CRLF line ends on Windows, and tests whose giant parameters became `tmp_path` directory names that Windows refuses. They are fixed in the tree (`test_o44_*` now finds the typed letters from the pixels that change, the tap log is written with `\n` line ends, ids are short), and `test_kb_static.py` limits a test id to 1000 characters because Windows refuses an environment value over 32,767 characters. The fixes have not been confirmed on the runners yet.
- **Real hardware.** Everything that touches Windows runs against fakes; see [what only your PC can settle](#what-only-your-pc-can-settle).

### Files the keyboard writes

All under `%USERPROFILE%\.jarvis\hands\` (the data directory is `JARVIS_DATA_DIR`, default `~/.jarvis`).

| File | When | What |
| --- | --- | --- |
| `keyboard-practice-air.json` | A completed air practice | The marker: rest seconds, phantoms, fps, noise, drill counts, key hit rate. Numbers only. Friction, not a boundary. |
| `keyboard-practice.json` | A completed pinch practice | The pinch marker (needed for `commit direct`). |
| `keyboard-trace-<time>.npz` | A practice | Landmarks and times; deleted after 14 days. |
| `keyboard-practice.jsonl` | A practice | One line per press attempt; rotated at 1 MB, three copies. |
| `keyboard-tuning.json` | `keyreplay --write` | Accuracy numbers (above). |

A live session writes no tap log and no trace, and the box is never written anywhere.

### Not done

- The word decoder, suggestions and autocorrect. The review layout has three inert cells and `review.py` has four seams that return "not handled" in O(1); nothing calls a decoder.
- A symbols page, Esc, editing inside the box, key repeat, sounds.
- A dwell-confirm for one-handed use.
- A burst gate in the detector for jitter bursts (untested; it would change the goldens).
- Anything that needs the live PC: see [what only your PC can settle](#what-only-your-pc-can-settle).
