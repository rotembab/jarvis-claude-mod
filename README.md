# Jarvis for Claude Code

A JARVIS-style voice assistant mod for [Claude Code](https://code.claude.com). Hold a key, speak, let go: Claude hears you and answers out loud, in a dry, unflappable British voice, while the full answer stays on screen.

- **Push-to-talk now.** Hold Right Ctrl, speak, release. Speech is transcribed on your own machine with faster-whisper (on an NVIDIA GPU when there is one).
- **Fish Audio voice.** Replies are spoken sentence by sentence as Claude writes them, through Fish Audio's streaming text-to-speech, in the voice you pick.
- **Wake word "Jarvis" and barge-in next.** Hands-free listening and interrupting Jarvis by voice are phase 2.
- **Later:** a holographic HUD, and control of your PC with permission tiers.

> **Status: phase 1 of 4 ("Talking Jarvis").** Push-to-talk, local speech-to-text and Fish Audio speech work on Windows. Expect rough edges; see the [roadmap](#roadmap).

## Contents

- [Requirements](#requirements)
- [Install](#install)
- [Fish Audio key and voice](#fish-audio-key-and-voice)
- [Local voice (optional)](#local-voice-optional)
- [Using Jarvis](#using-jarvis)
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

1. Hold the push-to-talk key (Right Ctrl by default). A chime plays, the status line shows `JARVIS · listening`, and a band above the prompt shows the microphone level.
2. Speak, then release the key. Jarvis transcribes what you said and sends it to Claude as your message, exactly as if you had typed it.
3. Claude answers. The answer appears on screen as usual and Jarvis reads it aloud as it streams. Code blocks and long tables are not read out; Jarvis tells you they are on screen.
4. To cut Jarvis off, press push-to-talk again (you can start speaking straight away) or run `/jarvis stop`.

The push-to-talk key works while another window has focus. Messages you type are answered in Claude's normal style and are not read aloud; the Jarvis persona applies to voice messages only.

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
| `/jarvis voice <id\|default>` | Use a Fish Audio voice by its model id, or go back to the default voice. |
| `/jarvis devices` | Show the microphone, speakers and models in use. |
| `/jarvis restart` | Restart the voice helper. |

## Settings

Change these with `/plugin configure jarvis@jarvis-claude-mod`, from the `/plugin` panel, or (all but the key) in `/config`.

| Setting | Default | Meaning |
| --- | --- | --- |
| `fishApiKey` | empty | Fish Audio API key. Sensitive; kept in secure storage. Empty means `FISH_AUDIO_API_KEY` from your environment. |
| `voiceId` | empty | Fish Audio voice model id. Empty means Fish Audio's default voice. |
| `fishModel` | `s2.1-pro-free` | Fish Audio model: `s2.1-pro-free` (free through 30 Nov 2026) or `s2.1-pro` (needs API credit). Run `/jarvis restart` after changing it. |
| `voiceEngine` | `fish` | Who speaks: `fish` (Fish Audio) or `local` (Chatterbox-Turbo on your PC, after `/jarvis setup local`). `/jarvis engine` changes it too. |
| `localVoiceClip` | empty | A 10 to 20 second recording for the local voice to copy. Empty means its built-in voice. Run `/jarvis restart` after changing it. |
| `pttKey` | `right ctrl` | The push-to-talk key, for example `right ctrl`, `right alt`, `f13` or `caps lock`. |
| `sttModel` | `auto` | Speech-to-text model: `auto`, `base.en`, `small.en`, `small`, `medium` or `large-v3-turbo`. `auto` means `large-v3-turbo` on an NVIDIA GPU and `small.en` on the CPU. Run `/jarvis setup` after changing it. |
| `language` | `en` | Language code for speech-to-text, such as `en`, `de` or `he`. English-only models (`.en`) ignore it. |

## How it works

Jarvis has two halves: a **mod** inside Claude Code (TypeScript hooks) and a **voice helper** (a Python program) that the mod starts on your machine.

```text
     microphone, push-to-talk key                      speakers / headset
                  |                                            ^
                  v                                            |
  +--------------------------- voice helper (Python 3.12) ----------------------+
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

- **Your audio stays on your computer.** Recording and transcription happen locally. While Jarvis runs, the helper keeps the microphone open so your first word is not clipped (Windows shows Python using the microphone), but it holds only the last 0.3 seconds in memory. Nothing is transcribed or kept unless you hold push-to-talk (or between `/jarvis talk` and its second run), and audio is never written to disk.
- **Transcripts go to Claude as your prompt**, the same way typed messages do, and are handled like any other Claude Code message.
- **Only text goes to Fish Audio**: the sentences Jarvis speaks, sent with your API key to produce the audio. Fish Audio's own terms and privacy policy apply to that text.
- The helper keeps a log in `%USERPROFILE%\.jarvis\logs\voice.log`. API keys and tokens are masked in it.
- Apart from installing (uv downloads Python packages, and `/jarvis setup` downloads the speech model from Hugging Face), Jarvis talks to nothing else.

## Troubleshooting

| Problem | What to try |
| --- | --- |
| `JARVIS · not set up · run /jarvis setup` | Run `/jarvis setup`. |
| Jarvis does not hear you | Windows may be blocking the microphone: Settings > Privacy & security > Microphone, turn on **Microphone access** and **Let desktop apps access your microphone**. Check the headset's mute switch. `/jarvis devices` shows which microphone is used. |
| Jarvis does not speak | Check the Fish Audio key (see above) and run `/jarvis test`. |
| `JARVIS · active in another window` | Another Claude Code window has the helper. Close that window, then run `/jarvis` here. |
| Slow transcription | Without an NVIDIA GPU, use `small.en` or `base.en`. With one, run `/jarvis setup` again so it installs the CUDA libraries. |

More detail, including logs and running the helper by hand, is in [docs/DEVELOPING.md](docs/DEVELOPING.md).

## Uninstall

```text
/plugin uninstall jarvis@jarvis-claude-mod
```

Then delete `%USERPROFILE%\.jarvis` (the helper's Python environment, speech models and logs). To remove the marketplace too: `/plugin marketplace remove jarvis-claude-mod`.

## Roadmap

| Phase | Name | What it adds |
| --- | --- | --- |
| 1 (now) | Talking Jarvis | Push-to-talk, local speech-to-text, Fish Audio speech, the JARVIS persona for voice turns, `/jarvis` commands. |
| 2 | Always listening | The wake word "Jarvis", echo cancelling, and barge-in: talk over Jarvis to interrupt it. |
| 3 | HUD | A holographic heads-up display that shows Jarvis listening, thinking and speaking. |
| 4 | Hands | Control of your PC (apps, windows, files) behind permission tiers, with a guard on risky tool calls. |

Not scheduled yet: macOS support, a hardened mode, and signed releases.

## Contributing

Issues and pull requests are welcome. [docs/DEVELOPING.md](docs/DEVELOPING.md) covers the development loop and tests; [docs/SPEC-phase1.md](docs/SPEC-phase1.md) is the engineering contract for phase 1.

## License

[MIT](LICENSE). Copyright (c) 2026 Rotem (rotembab).

JARVIS is a fictional character. This is an independent fan project, not affiliated with or endorsed by Marvel, Fish Audio or Anthropic.
