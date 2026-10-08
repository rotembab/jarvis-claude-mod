# Jarvis Mod for Claude Code: Plan

Written Oct 6, 2026. A snapshot of the living plan document, saved here so the plan is versioned with the code. Phase 1 has since been built (see [notes/2026-10-07-voice-bring-up.md](notes/2026-10-07-voice-bring-up.md) for what changed on the real PC); Windows-specific traps are in [WINDOWS-NOTES.md](WINDOWS-NOTES.md).

## Summary

Jarvis is a Claude Code mod plus a small local voice helper, built for Windows first and laid out so a Mac version is mostly new helper files. You say the wake word and talk. Claude Code answers in a Fish Audio JARVIS voice while a holographic ring HUD reacts, and it can act on your PC through Claude Code's own tools.

What it does:

- **Always listening for its wake word** on your machine, with nothing sent anywhere until it fires: "Hey Jarvis" from day one, plain "Jarvis" once its custom model is trained.
- **Speaks every answer** in a JARVIS voice streamed from Fish Audio, sentence by sentence as Claude writes.
- **Barge-in**: start talking while Jarvis speaks and the voice stops within about a quarter second, then it listens to you.
- **Controls your PC** through Claude Code's PowerShell and file tools, a few built-in desktop actions, and computer use in the desktop app. A clear line separates what it does freely from what it asks first.
- **A HUD pane** inside Claude Code: the blue ring from your monitor photo as the resting state, flaring into the orange and gold radial look from the first image while it thinks and speaks.

Defaults I picked where your ask left a choice:

- **Windows 10 or 11 on a standard 64-bit PC** is the first target. Everything that differs on a Mac sits behind small, swappable parts (see Windows now, Mac later).
- Your Fish Audio key lives under `env` in Claude Code's settings.json on your PC, which you've done. At install you can move it to Jarvis's own hidden settings field, which keeps it out of the commands Claude runs.
- PC control runs only inside Claude Code on your own computer, in a native Windows session. Not in WSL, and not in a cloud session.
- Wake word and speech detection run locally. Fish Audio does the voice, and speech-to-text runs locally by default with Fish Audio's transcription as a switch.
- The ready-made wake phrase is "Hey Jarvis", because the engine that shipped a plain "Jarvis" keyword ended its free plan on 30 June 2026. A custom plain "Jarvis" model is trained in phase 2.
- Personality is Iron Man's JARVIS: dry, British, unflappable, brief.

## Where it runs and where the code lives

Everything runs on your Windows PC, inside your usual Claude Code: Windows Terminal, or the desktop app's Code tab, where the HUD looks best. Mods need Claude Code 2.1.287 or later in the terminal, or 2.1.286 or later in the desktop app.

| Where you run Claude Code | Voice | HUD | Note |
| --- | --- | --- | --- |
| Windows Terminal (PowerShell or cmd running `claude`) | Yes | Ring in block characters | Main setup while we build |
| Desktop app, Code tab, local session | Yes | Full animated ring | The only place computer use exists on Windows |
| VS Code extension chat panel | Yes | No | Mods can't draw in that panel |
| Remote Control from your phone | At the PC only | On the PC's screen | The helper uses the PC's mic and speakers, not the phone's |
| Desktop app WSL session | No | No | Plugins don't load in WSL sessions |
| Cloud session or SSH remote | No | No | The helper would run far from your mic, so Jarvis keeps voice off |

- **The mod** is a Claude Code plugin: a hooks module written in TypeScript. It draws the HUD pane, adds `/jarvis` commands, sends your words as prompts, streams Claude's replies to the voice, and guards risky tool calls. It hot-reloads while we build it, and all its Windows-specific choices sit in one file.
- **The voice helper** (`jarvis-voice`) is a small Python program the mod starts with the session. A mod has no microphone access of its own, so the helper owns the mic, wake word, speech detection, transcription and all sound output.
- **How they talk**: the helper sends events (your words, sound levels, "you interrupted") as lines of JSON on its output. Claude Code closes the helper's input once it starts, so the mod sends commands to a private local address instead: `127.0.0.1`, with a secret token that changes each session.
- **Staying up**: if the helper stops, for example after a crash or a code reload while we build, the mod restarts it. If Claude Code closes, the helper notices within a few seconds and exits, releasing the mic. Only one Claude Code window owns the mic at a time; the others stay quiet.
- **Installing the helper**: `/jarvis setup` uses uv, a Python installer, to fetch Python 3.12 and the exact packages Jarvis was tested with. If uv is missing, it asks before putting a private copy in Jarvis's folder. It then downloads the speech models, roughly 0.5 to 3 GB. Everything lands in `%USERPROFILE%\.jarvis`, never in the plugin folder, which Claude Code replaces on every update. There is no Jarvis .exe in version 1, so there is no unsigned program for SmartScreen to warn about.
- **The code** will live in one GitHub repository, for example `jarvis-claude-mod`. Only its `plugin/` folder ships: `plugin/hooks/` (the mod), `plugin/voice/` (the helper's source) and `plugin/protocol/` (the message format both share). This project has no repository attached yet; attach one in [Project settings](#project-settings/resources) when you're ready and I'll scaffold it there. The same repo doubles as a plugin marketplace: install with `/plugin install jarvis --marketplace <you>/jarvis-claude-mod` in a terminal, or from the desktop app's plugin browser.

One thing to know: the build itself has to run on your PC to hear your mic and drive your desktop, so testing happens there, either by you or through a Remote Control session I start in that folder.

## Architecture

```text
Your PC
  mic ─► voice helper (Python): wake word ─► speech detection ─► local transcription
             │  events as JSON lines on stdout (your words, levels, barge-in)
             ▼
         Jarvis mod inside Claude Code ─► prompt ─► Claude ─► reply, sentence by sentence
             │  commands over HTTP on 127.0.0.1 with a per-session token (speak, stop)
             ▼
  speakers ◄─ voice helper ◄─ Fish Audio streaming TTS (only Claude's reply text leaves the PC)
```

Your voice never leaves the PC: the helper turns it into text for the mod, and only Claude's reply sentences go to Fish Audio. The two sides talk over two one-way channels. The helper sends events on its output: your words, sound levels and barge-in. The mod sends commands to the helper's private local address: speak this sentence, stop, run a desktop action. On barge-in the helper stops playback itself first, then tells the mod to abort Claude's turn.

## Voice: wake word, listening loop, barge-in

The helper runs one loop with four states (sleeping, listening, thinking, speaking), and the mic never closes, which is what makes barge-in possible. Inside the helper all audio is 16 kHz mono in 10 ms frames, the size the echo canceller needs.

### The loop

1. **Sleeping.** The mic streams through the echo canceller into the wake word engine only. Nothing is recorded or sent.
2. **Wake word heard.** The helper plays a short chime, the HUD ring lights up, and recording starts. The helper also opens the Fish Audio connection now, so the reply starts sooner.
3. **Listening.** A voice activity detector (Silero VAD) decides when you've finished, after about 700 ms of silence. The clip is transcribed on your PC and the text goes to the mod.
4. **Thinking.** The mod submits your words as a prompt in the Claude Code session, tagged as spoken so Jarvis answers for the ear.
5. **Speaking.** As Claude streams its reply, the mod cuts it into sentences and sends each one to the helper. The helper streams it to Fish Audio and plays the audio as it arrives. The first words should be audible within about a second.
6. **Follow-up window.** For about 8 seconds after Jarvis stops, you can keep talking without the wake word. Then it goes back to sleep.

### Wake word

- **Ready-made: openWakeWord "Hey Jarvis".** Open source, runs fully on your PC, needs no account, and shares its engine (onnxruntime) with speech detection. Its models are licensed for personal, non-commercial use.
- **Plain "Jarvis": a custom model**, trained from synthetic speech with openWakeWord's training notebook in phase 2. "Hey Jarvis" stays as the fallback, and the same model file works on a Mac.
- **Porcupine is optional only.** Picovoice switched off free personal keys on 30 June 2026 and offers no personal plan. Jarvis uses Porcupine only if you already hold a paid key.
- Sensitivity is a setting, so you can trade missed wakes against false ones from the TV.

### Barge-in

The hard part is that Jarvis must not hear itself. The helper feeds everything it plays into an echo canceller: the WebRTC one from the `livekit` package, which runs on Windows and Mac. It subtracts Jarvis's own voice before speech detection hears the mic. With headphones this is free; with speakers it is what makes barge-in work.

When you talk over Jarvis:

1. Speech detected for about 200 ms while speaking triggers a barge-in.
2. The helper stops playback instantly, drops queued audio, and closes the Fish Audio stream. It does this on its own, before telling the mod, so nothing waits on a round trip.
3. The mod aborts Claude's turn if it is still writing, and records where Jarvis was cut off.
4. Your new words become the next prompt, with a note of what Jarvis had said before you interrupted, so "no, the other one" makes sense. The helper keeps the last half second of audio, so your first syllables aren't lost.

Two modes, switchable with `/jarvis bargein`: **any speech** interrupts (default), or **say the wake word** to interrupt, for noisy rooms. "Jarvis, stop" always just stops talking. If the echo canceller can't keep up, say with loud laptop speakers, Jarvis switches to the wake word mode by itself and the HUD shows the switch.

### Mic and speakers on Windows

Jarvis uses Windows' normal shared audio (WASAPI) through the sounddevice library, so other apps keep their sound and Windows converts sample rates.

- **Devices by name.** Jarvis remembers your mic and speakers by name, not by number, and reopens them when you plug something in or change the Windows default. `/jarvis devices` picks them.
- **Bluetooth headsets.** Using a classic Bluetooth headset's mic drops all its sound to phone-call quality for as long as Jarvis listens. So Jarvis prefers the built-in or a USB mic, and warns if you pick a Bluetooth one.
- **Windows privacy switches.** Windows must let desktop apps use the microphone. If it doesn't, the HUD names the switch to turn on and opens the Settings page for you.
- **Another app holding the mic.** Some recording apps and games take the mic for themselves. Jarvis says so instead of going silent.

### What runs where

| Piece | Runs | Notes |
| --- | --- | --- |
| Wake word | Local | openWakeWord "Hey Jarvis", then a custom "Jarvis" model; Porcupine only with a paid key |
| Speech detection, echo cancelling | Local | Silero VAD as a small ONNX model (no PyTorch); WebRTC echo canceller from `livekit` |
| Speech to text | Local by default | faster-whisper: `small.en` or `base.en` on the CPU, picked by a quick benchmark on your PC; a larger model on an NVIDIA GPU; Fish Audio's speech-to-text API as a switch |
| Thinking and actions | Claude Code | Your normal session, model and permissions |
| Voice | Fish Audio | Streaming text-to-speech over WebSocket with your chosen JARVIS voice model, one connection per reply |
| Playback | Local | The helper plays all sound, voice and chimes alike. Claude Code's built-in player is silent in a Windows terminal, and the echo canceller must hear everything played. |

## PC control and permissions

Jarvis can do anything Claude Code can do on your PC, so the plan is full automation for everyday tasks and a spoken or clicked confirmation before anything that can't be undone. On Windows nothing sandboxes Claude's commands, so the permission rules and the mod's guard carry the whole load.

### How it acts

- **Shell and files**: Claude Code's PowerShell tool is the main shell on Windows, alongside its read, write and edit tools. If Git for Windows is installed, Claude also gets Bash, plus Monitor for long-running commands. This covers launching apps, scripts, file housekeeping, git, installs and system queries.
- **Desktop tools the mod adds**: a handful of quick, named actions so common requests don't need a script each time. Open or focus an app, media and volume, take a screenshot, lock the screen, set a timer, clipboard. The voice helper performs them with Windows' own interfaces and says plainly when Windows refuses, for example when it won't let a background program bring a window to the front.
- **Seeing and clicking the screen**: computer use, only in the Claude desktop app on Windows, on a Pro or Max plan. It is a research preview, off until you turn it on in the app's settings. The Windows terminal has none, so the desktop tools above cover the common requests. The HUD shows whether computer use is available.
- **Web**: Claude Code's web search and fetch, plus Chrome control where you have it set up.

### What it does freely, and what it asks first

| Tier | Examples on Windows | How it's handled |
| --- | --- | --- |
| Just do it | Open apps, media, volume, timers, lock the screen, read files, search, system info, save a screenshot for you | Allowed in settings, no prompt |
| Do it, then tell you | Create or edit files in your projects, run scripts you've run before | Allowed for chosen folders; each action is logged in the HUD |
| Ask by voice | Move or rename many files, install software with winget, change a setting, git push, put the PC to sleep, send the clipboard or a screenshot to Claude | Jarvis says what it's about to do and waits for "yes" or "go ahead". It ignores any "yes" heard while it is still talking. |
| Ask on screen | Delete files, send email or messages, anything involving money, admin rights, shutdown or restart, services, scheduled tasks, registry edits | Needs a click or a typed yes, because a voice from a TV or video could say "yes" too. Admin actions also need your click on the Windows UAC prompt. |
| Never | Turn off Defender or the firewall, format or wipe a disk, change boot settings, delete restore points or event logs, add user accounts, type passwords, type into Claude's own window, change Claude Code's own permissions | Denied outright |

Shutdown and restart count down 60 seconds so you can say "cancel". Windows then closes open apps without saving, so Jarvis warns you first.

The tiers are enforced in two layers:

- **Permission rules** in Claude Code's settings (allow, ask and deny lists) are the hard floor. `/jarvis pc rules` prints deny rules for the Never tier, for each shell (for example `PowerShell(Format-Volume *)` beside `Bash(diskpart *)`), and the `CLAUDE_CODE_USE_POWERSHELL_TOOL=1` env line (on Windows any Bash deny rule otherwise switches the PowerShell tool off), for you to paste into your settings. Jarvis never writes Claude Code's settings itself.
- **The mod's guard** reads every PowerShell, Bash and Monitor command before it runs and sorts it into a tier. It catches tricks that text rules miss, such as shortened parameters, full program paths and nested shells. It only ever makes a decision stricter, never approves anything on its own, and blocks if it crashes.

For the two Ask tiers, Jarvis asks you itself, by voice or with an on-screen button. Claude Code's own prompt is not enough, because in auto mode a classifier may answer it instead of you. Claude Code already refuses deletions of drive roots, the Windows folder and your home folder; Jarvis relies on that rather than repeating it.

Safety switches:

- **"Jarvis, stand down"** stops speech, aborts the running task, and kills any command it started.
- **No bypass mode.** Jarvis should never run with permission checks skipped; the mod warns on start if it is.
- **Never as administrator.** Jarvis won't start if Claude Code runs elevated, and it never tries to answer a UAC prompt. At Windows' default UAC level some built-in admin tools elevate without asking, so the HUD shows your level. "Always notify", a standard account, or Windows 11's Administrator protection makes every admin step need your click.
- **Hardened mode (optional).** One admin-approved step copies Jarvis's Never list into Claude Code's machine-wide policy folder. After that, no program running without admin rights can loosen those rules, Jarvis included.

## Persona and voice direction

Jarvis is a composed British butler who happens to run your computer: dry wit, total competence, never flustered, always brief.

### Personality

- **Address**: "sir" by default (switchable to your name or "ma'am"), used sparingly, not every sentence.
- **Tone**: understated and polite, with deadpan humour when something goes wrong. "I've restarted the build. Third time's the charm, one hopes."
- **Brevity for the ear**: one to three sentences out loud. Long output (code, logs, tables) stays on screen, and Jarvis says where it is: "The diff is on screen, sir."
- **Proactive, not chatty**: reports what it did and what it found, offers one next step, never asks how you are.
- **Honest about risk**: states plainly what a risky action will do before asking. "That removes 412 files from Downloads. Shall I proceed?"

The mod adds this as a section of Claude Code's system prompt for voice turns only, so typed work in the same session stays in Claude's normal style. On Windows that section also tells Claude to use the PowerShell tool and Windows paths; the Mac version swaps in one paragraph for macOS.

### Voice

- **Fish Audio voice model**: pick a calm, mid-low British male voice from Fish Audio's library, or build your own there from clean reference audio. Put its id in Jarvis's plugin settings, or set it as `JARVIS_VOICE_ID`. If you clone the film actor's voice, keep it for personal use and don't share the model.
- **Delivery**: measured pace, slight warmth, crisp consonants, no excitement spikes.
- **Audio treatment** (optional, local): a faint high-shelf lift and a touch of short room reverb gives the "in the suit" feel without sounding robotic. The helper applies it just before playback, so the echo canceller still hears exactly what comes out.
- **Sound design**: a soft rising chime on wake, a low tick when it goes back to sleep, a subtle hum while thinking. These are local files the helper plays, so they are instant and never trigger a false barge-in.

## HUD design

The two references become one ring that changes colour with Jarvis's state: the calm blue ring on a grid from your monitor photo at rest, and the orange and gold particle burst from the first image while it thinks and speaks.

### The ring by state

| State | Look | Driven by |
| --- | --- | --- |
| Sleeping | Dim cyan ring, thin tick marks slowly rotating, faint grid behind | A slow timer |
| Listening | Bright cyan ring with a glow; an inner arc swells with your voice | Mic level from the helper |
| Thinking | Colour sweeps to amber; fragments orbit the core, outer arcs counter-rotate | Claude's turn running |
| Speaking | Gold radial bursts and a bright core pulse in time with the words | Jarvis's audio level |
| Interrupted | Snaps back to cyan in one frame | Barge-in |
| Needs you | Amber ring with a pulsing outer bracket and the question as text | A confirmation waiting |

### Layout of the pane

- **Centre**: the ring with the JARVIS wordmark, as in the photo.
- **Top bracket bar**: current state and the action in progress ("Running build").
- **Left readout**: what you just said, live while you talk.
- **Right readout**: the action log, newest first, each with a tick or cross.
- **Bottom icon row**, like the photo's: mic mute, transcript, screen view, media.
- **Corner dial**: CPU and memory, echoing the small gauge at the bottom right of the first image.

### Where it shows

- **Desktop app's Code tab**: the full animated vector HUD in a side pane, around 30 frames a second. This is the closest match to the references.
- **Windows Terminal**: the same ring drawn in half-block and shade characters, which Windows Terminal draws crisply whatever the font. It opens on its own in wide terminals (144 columns or more), or with `/jarvis` in narrower ones. True pictures need a terminal like kitty or Ghostty, so on Windows the terminal ring stays in characters. The old Windows 10 console window works but looks rougher.
- **Terminal and desktop app**: a status line entry ("JARVIS · listening") and a band above the prompt showing your words as you speak.
- **VS Code chat panel**: mods can't draw there, so there is no HUD; voice still works.
- **Later, optional**: a full-screen HUD window like your monitor photo, served by the voice helper as a local page, for a second screen. The helper already runs a local web server, so this costs little.

## Windows now, Mac later

Jarvis is built and tested on Windows first, and every part that depends on the operating system sits behind a small interface, so the Mac version is mostly new helper files, not a rewrite.

| Piece | Windows | Mac later | Shared code |
| --- | --- | --- | --- |
| Supported systems | Windows 10 (1809 or later) or 11, 64-bit Intel or AMD | macOS 14 or later on Apple Silicon | One lock file covering both |
| Where Claude Code runs | Windows Terminal; desktop app Code tab, local session | Terminal, iTerm or Ghostty; desktop app Code tab | The whole mod, except one platform file |
| Claude's shell | PowerShell tool; also Bash and Monitor with Git for Windows | Bash (zsh) | Guard and rule files keyed by shell, not by OS |
| Mod to helper | Local HTTP on 127.0.0.1 with a per-session token | Same, or a Unix socket | One command interface and message format |
| Helper to mod | JSON lines on the helper's output | Same | Same line reader |
| Mic and speakers | sounddevice on WASAPI, shared mode | sounddevice on CoreAudio | One audio module; only a settings object differs |
| Echo cancelling | WebRTC canceller from `livekit` | Same package | Identical; Apple's voice processing is an option later |
| Wake word | openWakeWord on onnxruntime | Same model files | Identical |
| Speech detection | Silero VAD ONNX model on onnxruntime | Same | Identical |
| Speech to text | faster-whisper on the CPU; optional NVIDIA CUDA | whisper.cpp (pywhispercpp) on the Apple GPU; faster-whisper as fallback | One `transcribe(audio) -> text` interface |
| Voice and chimes | Fish Audio stream and local chimes, played by the helper | Same | Identical; Claude Code's own audio player is never used |
| Desktop actions | Start menu app list, UI Automation (pywinauto), pycaw volume, media keys, LockWorkStation | `open`, AppleScript, pyobjc | One action table: same names, inputs and tiers |
| Screenshot, clipboard | mss, pyperclip | Same libraries | Identical |
| Computer use | Desktop app only, Pro or Max | Desktop app | Detected from its tools at session start, not from the OS |
| HUD | Vector ring in the desktop app; block characters in the terminal | Same | Chosen by surface, not by OS |
| Mic permission | Windows privacy switches; a clear error when blocked | macOS asks per app; a blocked mic gives silence | First-run mic test with advice per OS |
| Admin gate | UAC level and elevation check | Admin password dialogs; refuse to run as root | One status check shown on the HUD |
| One Jarvis at a time | Named mutex | File lock | One lock interface |
| Data folder | `%USERPROFILE%\.jarvis` | `~/.jarvis` | One paths module |

Decisions made now so the Mac port is cheap:

1. **OS checks in the mod live in one file**, `platform.ts`: the helper path, data folder, shell rule files and the persona's OS paragraph. The HUD picks its look by surface.
2. **Commands never rely on the helper's input stream.** The command channel is an interface, so the Mac helper can later run as its own signed app over a socket with the same messages.
3. **One message format**, written once as a schema and generated for TypeScript and Python. The helper's first message names its platform and what it can do, and the mod registers only those desktop tools.
4. **The helper owns every sound on every OS**, so the echo canceller always hears what plays.
5. **Cross-platform libraries first.** Only desktop actions, the mic permission check and transcription speed-ups have OS-specific code, each behind an interface with a fake version for tests.
6. **Shell rules come from one tier table**, written out as PowerShell rules on Windows and Bash rules on both.
7. **Tested on both from day one.** CI runs the helper's tests on Windows and macOS runners with the fake desktop backend, so Mac breakage shows early.
8. **The helper can get its own identity later.** Windows 11 is testing per-app mic consent, and macOS ties mic access to the app that launched the helper. Both may call for a signed, named helper (`jarvis-voice.exe`, `JarvisVoice.app`) without changing the mod.

Engineering details and what still needs testing on your PC: [Windows build notes](WINDOWS-NOTES.md)

## Hand control (gestures)

Added Oct 7, 2026, from Rotem's ask: control the screen with your hands through the webcam, like Tony Stark (move and size windows, drag files), and later a projector on the wall driven by the same gestures. The build contract is [SPEC-hands.md](SPEC-hands.md).

- **A second helper.** `jarvis-hands` is its own Python program with its own environment (`%USERPROFILE%\.jarvis\hands\venv`, MediaPipe and OpenCV), started by the same mod the same way as the voice helper: JSON lines out, token-protected commands in, heartbeats, one per user. A crash in one never takes down the other, and the camera is off until you turn hand control on.
- **Tracking runs on your PC.** MediaPipe's hand landmarker reads 21 points per hand from each camera frame on the CPU. It is pinned to MediaPipe 0.10.33, the newest release that sends no usage statistics to Google (0.10.35 and later do, with no off switch). No camera picture is stored or sent anywhere.
- **The gestures.** An open palm held still for half a second takes the cursor, so a hand passing by does nothing. The cursor follows your knuckles (they barely move when you pinch, which keeps clicks on target). Pinch thumb and index to click, pinch and move to drag (files included), pinch twice to double-click, thumb and middle finger for a right click, two fingers up to scroll, a fist to grab the window under the cursor, a fist with each hand to resize it, and a fling to throw it to the next display, maximize or minimize it. Dropping your hand or touching the real mouse lets go at once, and no button is ever left held down.
- **Feedback.** A small click-through reticle follows the cursor: a cyan ring that closes as you pinch, amber brackets while grabbing, a progress arc while engaging and during calibration.
- **Fit to your reach.** By default the middle of the camera's view maps to your screens, so your hand moves in a small box in front of you, elbow down. `/jarvis hands calibrate` fits it to where you actually reach.
- **Displays.** Every display Windows reports, except virtual ones (your Virtual Display Driver), is in reach as Windows arranges them, so a projector set up to the right of the monitor is reached by moving your hand right, and a window flung right lands on it. `/jarvis hands display` picks which.
- **By voice or command.** `/jarvis hands on|off|calibrate|pause|resume`, and "Jarvis, turn on hand control" through the mod's `hands` tool.

Stages:

1. **Desk prototype (now).** One webcam on the monitor, the gestures above on Windows, the reticle, calibration, the doctor and a camera preview (`python -m jarvis_hands preview`). Done when you can drag a file into a folder and fling a window to another display without touching the mouse.
2. **Tune on your PC.** One short session at your desk: pinch thresholds, smoothing, the box size and the engage hold, set from what your hands and camera actually do.
3. **Projector wall mode.** The camera faces the projected image; calibration projects ArUco markers and fits the camera-to-projector mapping automatically; pointing at the wall moves the cursor on the projector display and a pinch clicks. Needs a camera that sees the whole wall (or a second camera) and the projector set up as its own display in Windows.
4. **Touch on the wall (optional).** A plain webcam can't tell touching the wall from hovering near it reliably; real touch needs a depth camera (Intel RealSense, Orbbec, OAK-D) or an IR light curtain.
5. **Together with voice.** "Put this on the projector" while pointing at a window, and the HUD showing hand state. macOS later: the desktop backend through Quartz and the Accessibility API.

## Build phases

Five phases, each ending in something you can try on your PC.

1. **Talking Jarvis.** It opens with a short check on your PC: the helper must launch from Windows Terminal and the desktop Code tab with no console window flashing, and the mic must open. Then come the mod and helper skeleton, `/jarvis setup`, `/jarvis` commands, push-to-talk, local transcription, Fish Audio voice streaming sentence by sentence, and the persona. Done when you can hold a key, ask a question, and hear Jarvis answer.
2. **Wake word and barge-in.** Always listening, with "Hey Jarvis" working at once and a custom plain "Jarvis" model trained and tuned for your voice. Also the follow-up window, echo cancelling, interrupting mid-sentence, "stand down", device switching, the Bluetooth warning, and one Jarvis at a time across Claude Code windows. Done when you can say "Jarvis" and cut it off with speakers on, and it stops cleanly.
3. **HUD.** The ring in all six states in the desktop Code tab, the Windows Terminal version, the status line and the live transcript band. Done when the ring visibly tracks your voice and Jarvis's.
4. **PC control.** The Windows desktop actions, PowerShell and Bash permission rules, the guard with voice and on-screen confirmations, the UAC check, and the action log. Done when "Jarvis, open Spotify and play my focus playlist" works and "delete my Downloads folder" stops for a click.
5. **Polish.** Sound design, voice audio treatment, wake word tuning for your room, a smoother one-command install, and the optional full-screen HUD. Optional extras: NVIDIA speed-up for transcription, and hardened mode.

**Alongside the phases: hand control**, in its own stages (see [Hand control](#hand-control-gestures)).

**Alongside the phases: home devices.** Jarvis controls the Apple TV, the Sony Bravia TV, Smart Life (Tuya) devices and Home Assistant over the home network, through a `home_control` tool and a setup window that keeps keys out of the chat. Unlocking, disarming and opening a garage door ask on screen, like the Ask on screen tier above. Guide: [HOME.md](HOME.md).

**Then the Mac port.** Most of it is new helper backend files plus Mac permission setup; the mod and the action list carry over (see Windows now, Mac later).

## What I need from you

- [x] Your Fish Audio key under `env` in `%USERPROFILE%\.claude\settings.json` on your PC.
- [x] A GitHub repository: [rotembab/jarvis-claude-mod](https://github.com/rotembab/jarvis-claude-mod) (public), cloned to `Documents\My Projects\Jarvis` on your PC.
- [ ] The Fish Audio voice you want, as its id, or I can suggest a few to audition.
- [x] Speakers or headphones day to day, and which mic: the Logitech PRO X wireless headset (USB dongle, not Bluetooth), so echo cancelling matters little.
- [x] Windows 10 or Windows 11: Windows 11 Home, build 26200.
- [x] Whether the PC has an NVIDIA graphics card: an RTX 4070 Ti, so transcription runs on the GPU with the larger large-v3-turbo model.
- [ ] Whether you'll use the Claude desktop app on Windows, and whether your plan is Pro or Max. Computer use needs both.
- [ ] The language you'll speak to Jarvis. English-only speech models are faster; Hebrew or mixed speech needs a multilingual model. The wake word stays English either way.
- [ ] Whether you're happy to set UAC to "Always notify", so every admin action needs your click.
