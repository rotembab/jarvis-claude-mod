# Jarvis for Claude Code

A JARVIS-style voice assistant mod for [Claude Code](https://code.claude.com). Hold a key, speak, let go: Claude hears you and answers out loud, in a dry, unflappable British voice, while the full answer stays on screen.

- **Push-to-talk now.** Hold Right Ctrl, speak, release. Speech is transcribed on your own machine with faster-whisper (on an NVIDIA GPU when there is one).
- **Fish Audio voice.** Replies are spoken sentence by sentence as Claude writes them, through Fish Audio's streaming text-to-speech, in the voice you pick.
- **Hands-free.** Say "Hey Jarvis" (or just "Jarvis", once you switch it on) and talk; talk over him to interrupt.
- **HUD.** An arc reactor ring in a pane beside the conversation shows Jarvis standing by, listening, thinking and speaking, with what you said and what Claude is running.
- **Later:** control of your PC with permission tiers.

> **Status: phase 3 of 4 ("HUD").** Hands-free voice (with an optional plain "Jarvis" wake word), barge-in, echo cancelling for speakers and the HUD work on Windows. Expect rough edges; see the [roadmap](#roadmap).

## Contents

- [Requirements](#requirements)
- [Install](#install)
- [Fish Audio key and voice](#fish-audio-key-and-voice)
- [Local voice (optional)](#local-voice-optional)
- [Using Jarvis](#using-jarvis)
- [Hand control (preview)](#hand-control-preview)
- [Commands](#commands)
- [Settings](#settings)
- [How it works](#how-it-works)
- [Privacy](#privacy)
- [Troubleshooting](#troubleshooting)
- [Uninstall](#uninstall)
- [Roadmap](#roadmap)
- [Contributing](#contributing)
- [License](#license)

## Requirements

| | |
| --- | --- |
| Operating system | Windows 10 or 11, x64. macOS support comes later; Linux is used only for CI. |
| Claude Code | A version with mods support: **2.1.287 or later** in the terminal, **2.1.286 or later** in the desktop app's Code tab. Jarvis runs in local sessions only, not in cloud sessions (it needs your microphone). |
| uv | [uv](https://docs.astral.sh/uv/) installs Python 3.12 and the voice helper for you. On Windows: `winget install astral-sh.uv` |
| Fish Audio | A [Fish Audio](https://fish.audio) account and API key. Text-to-speech usage is billed by Fish Audio. |
| Audio | A microphone and speakers, or a headset. |
| GPU (optional) | An NVIDIA GPU with a current driver makes speech recognition faster and lets Jarvis use a larger, more accurate model. Without one, Jarvis runs on the CPU. |
| Disk | A few GB in your user folder for the Python environment, the speech model and, on NVIDIA machines, the CUDA libraries. |

## Install

In a Claude Code terminal session, run:

```text
/plugin install jarvis --marketplace rotembab/jarvis-claude-mod
```

Answer `y` to add the marketplace, choose a scope (**Install for you**, the user scope, is the usual choice), and set the options if Claude Code offers them (you can leave them all empty for now).

The same thing in two steps, or from your shell:

```text
/plugin marketplace add rotembab/jarvis-claude-mod
/plugin install jarvis@jarvis-claude-mod
```

```powershell
claude plugin marketplace add rotembab/jarvis-claude-mod
claude plugin install jarvis@jarvis-claude-mod
```

**Desktop app.** Once the marketplace is added, open a local session in the Code tab, click **+** next to the prompt, choose **Plugins**, then **Add plugin**, and pick **jarvis**. A user-scope install made in the terminal also loads in the desktop app's local sessions, and the other way round.

### Set up the voice helper

Then, in a session, run:

```text
/jarvis setup
```

This creates `%USERPROFILE%\.jarvis`, makes a Python 3.12 environment there with uv, installs the voice helper (with the CUDA libraries if an NVIDIA driver is present), and downloads the speech-to-text model. Progress shows in the status line, and the helper starts when it is done. The first run can download a few GB (most of it on NVIDIA machines, for the CUDA libraries and the larger model) and can take several minutes.

- If uv is not installed, `/jarvis setup` prints the command to install it (`winget install astral-sh.uv`) and stops. Install uv, open a new terminal, and run `/jarvis setup` again. Jarvis never downloads installers itself.
- `/jarvis setup cpu` skips the CUDA libraries and, with the model on `auto`, installs the CPU model (`small.en`). `/jarvis setup small.en` (or another model name from [Settings](#settings)) installs a specific speech model, which Jarvis then uses until you change the `sttModel` setting.
- Run `/jarvis setup` again at any time to repair the installation. It also reinstalls the voice helper from the plugin, so run it after updating Jarvis.

### Update

```text
claude plugin update jarvis@jarvis-claude-mod
```

Then restart Claude Code and run `/jarvis setup`, which reinstalls the voice helper from the new version, and the hand helper too if you set up hand control. Until then Jarvis says when the hand helper is from an older version; `/jarvis setup hands` updates only that one.

## Fish Audio key and voice

Jarvis needs a Fish Audio API key to speak. Create one on the API keys page of your Fish Audio account, then give it to Jarvis in **one** of these ways:

1. **Plugin settings (recommended).** Run `/plugin configure jarvis@jarvis-claude-mod` (or open `/plugin`, the **Installed** tab, **jarvis**, **Configure options**) and paste the key into **Fish Audio API key**. It is a sensitive field: Claude Code keeps it in secure storage, not in a settings file.
2. **Environment variable.** Add it to the `env` block of your *user* settings file, `%USERPROFILE%\.claude\settings.json`:

   ```json
   {
     "env": {
       "FISH_AUDIO_API_KEY": "paste-your-key-here"
     }
   }
   ```

   Never put the key in a project's `.claude/settings.json` (that file is meant to be committed), and never commit it anywhere. If both are set, the plugin setting wins.

**Voice.** Without a voice set, Fish Audio's default voice is used. To pick one, open the voice on fish.audio, copy its model id (the id in the voice's page address), and either set the **Fish Audio voice** option or run `/jarvis voice <id>`. `/jarvis voice default` goes back to the default voice.

**Fish Audio model.** Jarvis uses `s2.1-pro-free` by default: Fish Audio's free API model, the same voice model as `s2.1-pro` at no cost under Fish's fair use policy through 30 November 2026 ([announcement](https://fish.audio/blog/s2-1-pro-free-api/)). It needs only an API key, no API credit. Fish says requests to it may be used to improve its models. To use the paid `s2.1-pro` instead, set the **Fish Audio model** option and add API credit at [fish.audio/app/developers](https://fish.audio/app/developers); the app plan's monthly credits don't pay for the API.

## Local voice (optional)

Jarvis can also speak with a voice that runs on your own PC: [Chatterbox-Turbo](https://huggingface.co/ResembleAI/chatterbox-turbo) by Resemble AI (MIT licensed). It needs no API key and no internet once installed, and copies a voice from a short clip you give it. Fish Audio stays the default; you switch between the two with one command.

1. Run `/jarvis setup local`. It installs PyTorch and Chatterbox into their own environment in `%USERPROFILE%\.jarvis\local-voice` and downloads the model: about 6 GB in all. An NVIDIA GPU is strongly recommended; `/jarvis setup local cpu` installs the CPU build, which works but speaks noticeably later.
2. Optional: set the **Local voice clip** option to a recording of the voice you want, 10 to 20 seconds of one person speaking clearly (WAV or MP3, at least 5 seconds). Without a clip, Chatterbox's built-in voice is used.
3. Run `/jarvis engine local`. `/jarvis engine fish` switches back.

The local voice loads onto the GPU when the helper starts (a few seconds) and stays loaded while Claude Code is open. Its audio carries Resemble AI's inaudible watermark. Only copy a voice you have the right to use, and keep a clip of someone else's voice to personal use.

## Using Jarvis

1. Say **"Hey Jarvis"**, then what you want, in one breath or after a pause: "Hey Jarvis, did the build pass?" A chime plays and the status line shows `JARVIS · listening`. After `/jarvis wake jarvis`, plain **"Jarvis"** works too when it starts what you say: "Jarvis, open the logs." Said in the middle of a sentence it is ignored, and it does nothing once Jarvis has started speaking a reply, until it ends, even in a quiet pause while Claude runs a tool: use "Hey Jarvis" then. It needs English transcription (`language` `en`). Its model (about 200 KB) is downloaded when you first switch it on, unless `/jarvis setup` already did.
2. Stop talking. After about 0.7 seconds of silence Jarvis transcribes what you said and sends it to Claude as your message, exactly as if you had typed it.
3. Claude answers. The answer appears on screen as usual and Jarvis reads it aloud as it streams. Code blocks and long tables are not read out; Jarvis tells you they are on screen.
4. For about 8 seconds after Jarvis finishes, the status line shows `JARVIS · awake · keep talking`: just speak, no wake word needed.
5. To cut Jarvis off, talk over him: he stops within about a quarter second and listens. Saying only "stop", "stand down" or "never mind" just stops him. `/jarvis stop` works too.

Push-to-talk still works the whole time: hold the key (Right Ctrl by default), speak, release. It works while another window has focus, and pressing it while Jarvis speaks also cuts him off.

**Speakers instead of a headset?** Echo cancelling takes Jarvis's own voice out of what the microphone hears, so you can talk over him with speakers too. It learns your room during the first second or two he speaks. If he still stops himself mid-sentence (very loud speakers, or Windows sound effects such as loudness equalization), run `/jarvis bargein wake` so only "Hey Jarvis" interrupts him. If the TV or other people wake him by mistake, set **Wake word sensitivity** to `low`.

Messages you type are answered in Claude's normal style and are not read aloud; the Jarvis persona applies to voice messages only.

### Which model answers

Sonnet answers what you say, so replies start quickly. Before each voice request reaches Claude, a quick Haiku check rates it. Complex work (multi-step coding, debugging, changes across files) goes to Opus. The hardest problems (architecture, subtle bugs, large migrations) go to Fable. A follow-up such as "go ahead" is rated with the request before it. When Opus or Fable takes a request, the transcript says so.

- Choose yourself by saying so: "use Opus", "make Fable do it", "switch to Sonnet", or "think hard" (Opus) and "ultrathink" (Fable).
- Messages you type, and subagents, always use your session's model (`/model`).
- In a long conversation (over about 100,000 tokens) your session's model answers voice requests, because Sonnet, Opus and Fable have a smaller context window than a 1M-context session and moving a long conversation to another model is slow. The transcript says so once. Routing starts again after `/compact` or `/clear`, or in a new session, so Jarvis works best in a session of its own.
- If Claude Code can't answer on a model (your plan may not offer Fable), Jarvis stops using it for the rest of the session: Opus takes Fable's requests, and your session's model takes Sonnet's or Opus's. The transcript says so; say the request again.
- `/jarvis routing off` makes your session's model answer voice requests too.
- Moving between models costs some prompt caching: the first request on a model that has not answered for a while reads the conversation again.

### The HUD

A pane titled JARVIS opens beside the conversation when a session starts. Its ring shows what Jarvis is doing:

| Ring | Means |
| --- | --- |
| A glowing blue ring around JARVIS on a dark grid, its dots turning slowly | Standing by for "Hey Jarvis" |
| The ring brightens, grows a little and the white arc beside it sweeps round as you talk | Listening |
| An orange globe of glowing fragments turning in 3D, orbits spinning round its core | Thinking: transcribing you, or Claude working |
| The globe flares gold and swells and shrinks with his voice | Speaking |
| The blue ring in gray, still | Offline: the voice helper is not running |

Under the ring are the last thing you said and the last six things Claude ran (`›` running, `✓` done, `✗` failed).

- In the terminal the ring is drawn in block characters and grows to fill the room the pane has; the pane asks for up to half the window. A pane opens by itself only in a window at least 144 columns wide; in a narrower one it waits for room, and `/jarvis hud` opens it at any width.
- In the desktop app's Code tab the ring is a vector drawing.
- `/jarvis hud off` closes it and keeps it closed in new sessions; `/jarvis hud on` brings it back.

#### Focus mode

`/jarvis focus` shows only the HUD and the prompt while Jarvis is running. The HUD takes nearly the whole window, with what you said, the start of Claude's last reply and what Claude ran beside the ring, and the conversation folds away.

- Typing a prompt brings the conversation back; it folds away again the next time you talk to Jarvis.
- Ctrl+O shows the whole conversation at any time.
- Questions for you, permission prompts and command output still show.
- The reply Claude is writing streams in a narrow strip beside the HUD, and folds away once it is done.
- In Claude Code's classic (non-fullscreen) view nothing is folded: the HUD grows tall above the prompt and the conversation scrolls up out of sight, where you can scroll back to it.
- In the desktop app focus mode does nothing yet.
- `/jarvis focus off` turns it off. The choice is kept for new sessions.

### The Jarvis app (optional)

The Jarvis app shows the HUD on your desktop as well: a ring over your screen while you talk to Jarvis, a small orb you can put anywhere, and a tray icon. Claude Code and the plugin still do all the work; the app only shows what the plugin sends it, over `127.0.0.1` on your own computer.

It needs the plugin 0.8.0 or later, so [update Jarvis](#update) first (`claude plugin update jarvis@jarvis-claude-mod`, restart Claude Code, `/jarvis setup`). There is no installer yet, so the app runs from a clone of this repository:

```text
git clone https://github.com/rotembab/jarvis-claude-mod
cd jarvis-claude-mod/app
npm ci
npm start
```

- It needs [Node.js](https://nodejs.org) 22 or later (`winget install OpenJS.NodeJS.LTS`). The first start downloads Electron, about 100 MB. If PowerShell refuses to run `npm`, type `npm.cmd` instead.
- Claude Code finds the app within a few seconds of it starting. `/jarvis app` says whether it is connected (if it says `Unknown subcommand "app"`, the plugin is not updated yet); `/jarvis app off` stops sending it the HUD.
- To update the app, quit it from its tray menu first, then `git pull`, `npm ci` and `npm start`.
- [docs/APP.md](docs/APP.md) covers the overlay, the orb, the tray menu, Ctrl+Alt+J and starting with Windows.

## Hand control (preview)

Your webcam can drive the mouse and windows: point with your hand, pinch to click and drag files, make a fist to grab a window and fling it to another screen. It runs on Windows, needs a webcam, and is off until you turn it on.

1. Install it once: `/jarvis setup hands` (about 500 MB: MediaPipe and OpenCV in `%USERPROFILE%\.jarvis\hands`, and an 8 MB hand model in `%USERPROFILE%\.jarvis\models\hands`).
2. Turn it on: `/jarvis hands on`, or say "Jarvis, turn on hand control". The camera light comes on.
3. Hold an open palm toward the camera, still, for half a second. A cyan ring appears at the cursor, which now follows your hand.

| Gesture | What it does |
| --- | --- |
| Open palm toward the camera, still for half a second | Take the cursor |
| Move your hand | Move the cursor (it follows your knuckles, which stay put when you pinch) |
| Pinch thumb and index finger | Click; pinch and move to drag; pinch twice to double-click |
| Pinch thumb and middle finger | Right-click |
| Index and middle finger up, then move | Scroll |
| Fist over a window | Grab the window and move it |
| A fist with each hand | Resize the grabbed window |
| Fling a grabbed window | Left or right: to the next display that way, or that half of the screen when there is none. Up: maximize. Down: minimize |
| Drop your hand out of view, or touch the mouse | Let go at once |

- Sit so the camera sees your hand at chest height with your elbow down; small movements cover the whole screen.
- `/jarvis hands calibrate` fits the mapping to your reach: hold an open palm still on each corner target as it appears.
- `/jarvis hands pause` turns the camera off without turning hand control off. It stays paused when the helper restarts and in new sessions, until `/jarvis hands resume`, `on` or `off`.
- Some webcams take up to about 20 seconds to open. If the camera is still opening when `/jarvis hands resume` answers, it says so, and a message follows when the camera is on or why it could not open.
- You can ask Claude too: "turn on hand control", "pause hand control", "calibrate my hands", "put hand control on display 2", "let my hand take the cursor" (no open palm needed) or "take the cursor away from my hand".
- Hand control runs in one Claude Code window at a time, since one camera can serve only one. In another window `/jarvis hands` says where it runs; turning it off there and running `/jarvis hands restart` brings it to this window.
- Displays are reached the way Windows arranges them, so a projector set to the right of your monitor is reached by moving your hand right. Virtual displays (virtual display drivers and streaming dummies) are left out; USB display adapters count as real displays. `/jarvis hands display 1` keeps your hand on one display, and a display you leave out takes none of your reach.
- Windows run as administrator can't be clicked, moved or resized by hand control (Windows blocks it). Jarvis says so once when you grab one; a pinch on one does nothing.
- `/jarvis hands` shows the camera, the displays and the gestures. The camera picture never leaves your computer.

## Commands

| Command | What it does |
| --- | --- |
| `/jarvis` | Status and help. Also starts the helper again after it stopped, or after another window released it. |
| `/jarvis setup [model] [cpu]` | Install or repair the voice helper and download the speech model. |
| `/jarvis talk` | Start listening without the push-to-talk key; run it again when you have finished speaking. |
| `/jarvis stop` | Stop speaking and cancel the spoken reply. |
| `/jarvis test` | Speak a test line (checks your Fish Audio key, voice and speakers). |
| `/jarvis setup local [cpu]` | Install the local voice (Chatterbox-Turbo, about 6 GB). |
| `/jarvis engine <fish\|local>` | Speak with Fish Audio or with the local voice. |
| `/jarvis wake <on\|jarvis\|off>` | Listen for "Hey Jarvis", for plain "Jarvis" as well, or use push-to-talk only. |
| `/jarvis bargein <speech\|wake\|off>` | What interrupts Jarvis while he speaks: any speech (default), only "Hey Jarvis", or nothing. |
| `/jarvis routing <auto\|off>` | Sonnet answers voice requests and Opus or Fable the hard ones (default), or your session's model answers. |
| `/jarvis voice <id\|default>` | Use a Fish Audio voice by its model id, or go back to the default voice. |
| `/jarvis hud [on\|off]` | Open the HUD pane. `off` closes it and keeps it closed in new sessions; `on` opens it with each session again. |
| `/jarvis focus [on\|off]` | Focus mode: while Jarvis runs, only the HUD and the prompt show. |
| `/jarvis app [on\|off]` | Whether the Jarvis app is connected. `off` stops sending it the HUD, in new sessions too; `on` sends it again. |
| `/jarvis devices` | Show the microphone, speakers and models in use. |
| `/jarvis restart` | Restart the voice helper. |
| `/jarvis setup hands` | Install hand control (MediaPipe, OpenCV and the hand model). |
| `/jarvis hands [on\|off]` | Hand control status, or turn it (and the camera) on or off. |
| `/jarvis hands calibrate [cancel]` | Fit hand control to your reach: an open palm on each corner target. |
| `/jarvis hands display <n\|all>` | The displays your hand reaches (a list such as `1,2` works). |
| `/jarvis hands engage <palm\|always>` | Start with an open palm (default), or let any hand take the cursor at once. |
| `/jarvis hands camera <n\|name>` | The camera to use: its number or part of its name. |
| `/jarvis hands pause\|resume\|restart` | Turn the camera off and on, or restart the hand helper. |

## Settings

Change these with `/plugin configure jarvis@jarvis-claude-mod`, from the `/plugin` panel, or (all but the key) in `/config`.

| Setting | Default | Meaning |
| --- | --- | --- |
| `fishApiKey` | empty | Fish Audio API key. Sensitive; kept in secure storage. Empty means `FISH_AUDIO_API_KEY` from your environment. |
| `voiceId` | empty | Fish Audio voice model id. Empty means Fish Audio's default voice. |
| `fishModel` | `s2.1-pro-free` | Fish Audio model: `s2.1-pro-free` (free through 30 Nov 2026) or `s2.1-pro` (needs API credit). Run `/jarvis restart` after changing it. |
| `voiceEngine` | `fish` | Who speaks: `fish` (Fish Audio) or `local` (Chatterbox-Turbo on your PC, after `/jarvis setup local`). `/jarvis engine` changes it too. |
| `localVoiceClip` | empty | A 10 to 20 second recording for the local voice to copy. Empty means its built-in voice. Run `/jarvis restart` after changing it. |
| `wakeWord` | `on` | `on` listens for "Hey Jarvis"; `jarvis` also for plain "Jarvis" at the start of what you say (needs English transcription; its model is downloaded when you switch it on); `off` is push-to-talk only. `/jarvis wake` changes it too. |
| `bargeIn` | `speech` | What interrupts Jarvis: `speech` (talking over him), `wake` (only "Hey Jarvis", for a noisy room, or speakers he still hears himself through) or `off`. `/jarvis bargein` changes it too. |
| `echoCancelling` | `on` | `on` takes Jarvis's own voice out of what the microphone hears, so with speakers he doesn't interrupt or wake himself. `off` uses the microphone as it is. Run `/jarvis restart` after changing it. |
| `modelRouting` | `auto` | `auto`: Sonnet answers voice requests, Opus complex ones and Fable the hardest ([Which model answers](#which-model-answers)). `off`: your session's model. `/jarvis routing` changes it too. |
| `wakeSensitivity` | `medium` | `high` wakes more easily and more often by mistake; `low` needs a clearer "Hey Jarvis" (or "Jarvis"). Run `/jarvis restart` after changing it. |
| `pttKey` | `right ctrl` | The push-to-talk key, for example `right ctrl`, `right alt`, `f13` or `caps lock`. |
| `sttModel` | `auto` | Speech-to-text model: `auto`, `base.en`, `small.en`, `small`, `medium` or `large-v3-turbo`. `auto` means `large-v3-turbo` on an NVIDIA GPU and `small.en` on the CPU. Run `/jarvis setup` after changing it. |
| `language` | `en` | Language code for speech-to-text, such as `en`, `de` or `he`. English-only models (`.en`) ignore it. |
| `handControl` | `off` | `on` watches your hands through the webcam (after `/jarvis setup hands`). `/jarvis hands on\|off` changes it too. |
| `handCamera` | empty | The webcam for hand control: its number or part of its name, such as `UGREEN`. Empty means the first camera. |
| `handEngage` | `palm` | `palm`: hold an open palm still to take the cursor. `always`: any hand in view takes it at once. |

## How it works

Jarvis has two halves: a **mod** inside Claude Code (TypeScript hooks) and a **voice helper** (a Python program) that the mod starts on your machine.

```text
     microphone, push-to-talk key                      speakers / headset
                  |                                            ^
                  v                                            |
  +--------------------------- voice helper (Python 3.12) ----------------------+
  |  "Hey Jarvis" (openWakeWord) + speech detection (Silero VAD), or            |
  |  push-to-talk -> record 16 kHz -> faster-whisper (local) -> transcript      |
  |  sentences -> Fish Audio streaming TTS (WebSocket) -> playback              |
  +-----------------------------------------------------------------------------+
        |  events: one JSON object per line            ^  commands: HTTP POST to
        |  on stdout (hello, state, utterance,         |  127.0.0.1:<port>/v1/<name>
        |  speech_started, speech_done, error, ...)    |  with a per-launch token
        v                                              |  (speak, stop, listen, ...)
  +--------------------------- Jarvis mod (inside Claude Code) ------------------+
  |  utterance -> submitted as your message, with the persona for voice turns   |
  |  Claude's streamed reply -> split into sentences -> speak                   |
  |  /jarvis commands, status line, the band above the prompt                   |
  +-----------------------------------------------------------------------------+
                                  |       ^
                                  v       |
                                  Claude
```

- The mod starts the helper when a local session starts (never in cloud sessions) from `%USERPROFILE%\.jarvis\venv`, and restarts it with backoff if it crashes.
- The helper's control server listens on `127.0.0.1` only, on a port the OS picks, and accepts only requests carrying the token the mod generated for that launch. Requests from web pages are refused.
- The mod sends a heartbeat every 2 seconds; the helper exits on its own if the heartbeats stop for 15 seconds, so it never outlives Claude Code.
- One helper runs per user. A second Claude Code window shows `JARVIS · active in another window`; run `/jarvis` there to take over once the first window lets go.
- The message format between the two halves is defined in [`plugin/protocol/schema.json`](plugin/protocol/schema.json).

## Privacy

- **Your audio stays on your computer.** The wake word, speech detection and transcription all run locally. While Jarvis runs, the helper keeps the microphone open (Windows shows Python using the microphone) and listens for the wake word ("Hey Jarvis", and plain "Jarvis" too once you switch it on), holding only the last 2 seconds in memory. Nothing is recorded, transcribed or kept until it hears the wake word, you hold push-to-talk, you speak in the few seconds after a reply, or you talk over Jarvis while he speaks. With plain "Jarvis" on, a clip that woke it is transcribed on your computer and dropped unless it starts with "Jarvis", so a sound-alike such as "Travis" goes nowhere. Audio is never written to disk. `/jarvis wake off` turns the wake word off.
- **Transcripts go to Claude as your prompt**, the same way typed messages do, and are handled like any other Claude Code message. With model routing on, each one also goes to Haiku, through the same Claude Code connection, to choose the model.
- **Only text goes to Fish Audio**: the sentences Jarvis speaks, sent with your API key to produce the audio. Fish Audio's own terms and privacy policy apply to that text.
- **Hand control uses the camera only while it is on.** The hand helper reads each frame, finds your hands on your own computer (MediaPipe, on the CPU) and drops the frame; no picture is stored or sent anywhere. Jarvis pins MediaPipe 0.10.33, the newest version that sends Google no usage statistics. The camera light is on while hand control watches, and off after `/jarvis hands pause` or `off`.
- The helper keeps a log in `%USERPROFILE%\.jarvis\logs\voice.log` (hand control: `hands.log`). API keys and tokens are masked in it.
- **The Jarvis app**, if you run it, receives the HUD from the plugin over `127.0.0.1` with a token only the two of them know, and makes no network requests of its own.
- Apart from installing (uv downloads Python packages, `/jarvis setup` downloads the speech model from Hugging Face and the wake word models from GitHub: openWakeWord's releases, and the plain "Jarvis" model from a community collection, and `/jarvis setup hands` downloads the hand model from Google's MediaPipe models) and the helper fetching a wake word model that is missing (the "Hey Jarvis" model when it starts, the plain "Jarvis" model when you switch it on), Jarvis talks to nothing else.

## Troubleshooting

| Problem | What to try |
| --- | --- |
| `JARVIS · not set up · run /jarvis setup` | Run `/jarvis setup`. |
| Jarvis does not hear you | Windows may be blocking the microphone: Settings > Privacy & security > Microphone, turn on **Microphone access** and **Let desktop apps access your microphone**. Check the headset's mute switch. `/jarvis devices` shows which microphone is used. |
| "The microphone delivered pure digital silence" | Something mutes the microphone completely: Windows' microphone privacy settings (above), or the headset itself (a mute button or light, a flipped-up or unplugged boom mic, or its own software, such as Logitech G HUB). Jarvis says this only when the microphone has sent nothing at all since it started, or for a push-to-talk clip before it has heard any sound. Headsets with a noise gate send exact silence between words, which is fine. |
| Jarvis wakes by mistake, or misses "Hey Jarvis" | Change **Wake word sensitivity** (`low` wakes less often, `high` more easily). Say it clearly, like "hey JAR-vis". |
| Plain "Jarvis" does not wake him | Say it first, after a moment's quiet: "Jarvis, ..." In the middle of a sentence it is ignored, and once Jarvis has started speaking a reply, until it ends (even while Claude runs a tool), only "Hey Jarvis" (or talking over him) works. It needs English transcription: with another `language`, Whisper may write the name differently and the request is dropped. A chime with no request means plain "Jarvis" woke on a sound-alike ("Travis, ...") and the transcript did not start with "Jarvis", so nothing was sent. If `/jarvis wake` says plain "Jarvis" is not ready, give it a moment (its model downloads when you switch it on); if it stays so, run `/jarvis setup`. |
| Jarvis stops himself mid-sentence | With speakers he can hear his own voice. `/jarvis` shows whether echo cancelling is on; if it is and he still does, turn the speakers down, turn off Windows sound effects (loudness equalization, spatial sound), use a headset, or run `/jarvis bargein wake`. |
| "Echo cancelling could not be started" | Run `/jarvis setup` to reinstall the voice helper. On Windows, Smart App Control can block the echo canceller's library (`livekit_ffi.dll`); everything else keeps working, and `/jarvis bargein wake` stops Jarvis interrupting himself through speakers. |
| "Echo cancelling stopped working" | It loaded and ran, then failed; Jarvis now hears the microphone as it is. `/jarvis restart` tries again. Meanwhile, with speakers, `/jarvis bargein wake` stops Jarvis interrupting himself. |
| Jarvis does not speak | Check the Fish Audio key (see above) and run `/jarvis test`. |
| `JARVIS · active in another window` | Another Claude Code window has the helper. Close that window, then run `/jarvis` here. |
| Hand control: "camera blocked" | Settings > Privacy & security > Camera: turn on **Camera access** and **Let desktop apps access your camera**, then `/jarvis hands restart`. |
| Hand control: "camera in use" | Another app (Teams, Zoom, the Camera app, OBS) has the webcam. Close it, then `/jarvis hands restart`. |
| Hand control misses pinches or the cursor jitters | Light your hand from the front, keep it 40 to 80 cm from the camera, and run `/jarvis hands calibrate`. |
| Slow transcription | Without an NVIDIA GPU, use `small.en` or `base.en`. With one, run `/jarvis setup` again so it installs the CUDA libraries. |

More detail, including logs and running the helper by hand, is in [docs/DEVELOPING.md](docs/DEVELOPING.md).

## Uninstall

```text
/plugin uninstall jarvis@jarvis-claude-mod
```

Then delete `%USERPROFILE%\.jarvis` (the helper's Python environment, speech models and logs). To remove the marketplace too: `/plugin marketplace remove jarvis-claude-mod`.

If you ran the Jarvis app, untick **Start with Windows** in its tray menu first, then delete its folder and `%APPDATA%\Jarvis`.

## Roadmap

| Phase | Name | What it adds |
| --- | --- | --- |
| 1 (done) | Talking Jarvis | Push-to-talk, local speech-to-text, Fish Audio speech, the JARVIS persona for voice turns, `/jarvis` commands, an optional local voice. |
| 2 (now) | Always listening | Done: "Hey Jarvis", plain "Jarvis" (optional), end-of-speech detection, the follow-up window, barge-in, echo cancelling for speakers, spoken "stop", Sonnet by default with Opus and Fable for hard requests. Next: a plain "Jarvis" model trained for Jarvis. |
| 3 (now) | HUD | Done: an arc reactor ring pane in Windows Terminal and the desktop app that shows Jarvis standing by, listening, thinking and speaking, with your last words and Claude's last actions. Next: tuning it on your screen. |
| 4 | Hands | Control of your PC (apps, windows, files) behind permission tiers, with a guard on risky tool calls. |
| App | Jarvis app | Step A1 (now): a desktop app that shows the HUD over your screen while Claude Code runs, with an orb, a tray icon and Ctrl+Alt+J ([docs/APP.md](docs/APP.md)). Next: the app runs the voice helper itself, so Jarvis listens from the moment Windows starts. |
| Alongside | Hand control | Now: webcam gestures for the mouse and windows (preview). Next: tuning on a real desk, then a projector wall mode. |

Not scheduled yet: macOS support, a hardened mode, and signed releases.

## Contributing

Issues and pull requests are welcome. [docs/DEVELOPING.md](docs/DEVELOPING.md) covers the development loop and tests; [docs/SPEC-phase1.md](docs/SPEC-phase1.md) is the engineering contract for phase 1.

## License

[MIT](LICENSE). Copyright (c) 2026 Rotem (rotembab).

The wake word code in `plugin/voice/src/jarvis_voice/listen/wakeword.py` is ported from [openWakeWord](https://github.com/dscripka/openWakeWord) (Apache License 2.0, David Scripka). The "Hey Jarvis" model it downloads is openWakeWord's and is licensed [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/): personal, non-commercial use. The optional plain "Jarvis" model (`jarvis_v2.onnx`) comes from [fwartner/home-assistant-wakewords-collection](https://github.com/fwartner/home-assistant-wakewords-collection); that repository is MIT-licensed, but the model was likely trained with openWakeWord's tooling and CC BY-NC-SA data, so treat it as personal, non-commercial use too. Neither model is in this repository: they are downloaded to your computer (by `/jarvis setup`, or by the helper: "Hey Jarvis" when it starts, plain "Jarvis" when you switch it on), pinned and checked against fixed SHA-256 sums. Speech detection uses the Silero VAD model that faster-whisper ships (MIT).

JARVIS is a fictional character. This is an independent fan project, not affiliated with or endorsed by Marvel, Fish Audio or Anthropic.
