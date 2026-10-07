# Voice bring-up on the Windows PC (2026-10-07)

Findings from getting Jarvis to speak on the owner's PC (Windows 11, RTX 4070 Ti 12 GB,
Logitech PRO X headset), and the options for a free or local voice. Facts marked
*measured* were checked on that PC on 2026-10-07; the rest come from Fish Audio's docs
and model cards as read that day, so re-check them before relying on them.

## Why Fish Audio returned HTTP 402

- Fish keeps two balances. **Platform credit** (the free plan's 8,000 credits a month)
  pays only for the fish.audio web app. **API credit** pays for the developer API, which is
  what the helper uses. The account had API credit 0.000000 and had never been topped up.
- Fish's 402 body says so itself: "Insufficient API credit. API credit is managed
  independently from platform credit." Since 4688525 the helper passes that text through.
- The key itself was fine: the wallet endpoints answered 200 with it. *measured*
- Plan credit cannot be spent through the API. Fish's official MCP server
  (`https://api.fish.audio/mcp`) does bill plan credit, but it returns audio as download URLs,
  not a stream, so it does not fit the live voice path. Scraping the web app is against
  Fish's Terms of Use.

## What works today: the free API model

- `s2.1-pro-free` is Fish's $0 API model, described as the same model as `s2.1-pro`, free under
  fair-use limits, with no latency guarantee. Fish says it is free **through 2026-11-30** and that
  requests to it may be used to improve their models.
- With API credit 0, the live WebSocket handshake is accepted for `s2.1-pro-free` and refused
  with 402 for `s2.1-pro`, `s2-pro` and `s1`. *measured*
- Full replies played on the headset with `interrupted: false`. *measured*
- To use it, set in the `env` block of the Claude Code user settings and restart Claude Code:

  ```json
  "JARVIS_TTS_MODEL": "s2.1-pro-free",
  "JARVIS_VOICE_ID": "<a Fish voice-library id>"
  ```

  Spell the model exactly: per Fish's docs an unknown `model` value silently falls back to the
  paid `s2.1-pro`, which brings the 402 back.
- After 2026-11-30: API credit costs $15 per million UTF-8 bytes for `s2.1-pro`, `s2-pro` and
  `s1`, about $0.003 for a 200-character English reply (Hebrew is 2 bytes a character).
  `speech-1.5` and `speech-1.6` were retired on 2026-02-28.

## Streaming and time to first audio (*measured*)

The reply was sent to `/v1/speak` in three chunks at 0, 1.5 and 3.0 s, the way Claude writes
sentence by sentence (`plugin/voice/scripts/voice_latency.py`).

| Setup | First audio after the first chunk |
| --- | --- |
| Fake Fish server, silent fake audio (helper overhead only) | 75–123 ms |
| Real Fish `s2.1-pro-free`, headset | 1.41–1.52 s |

- Speech started long before the last chunk arrived, and Fish received one `text` + `flush` per
  chunk, so the helper streams.
- Almost all of the 1.4 s is Fish's side (connection setup plus synthesis on the free model).
- `level` events appear only while speaking: none while idle, 180 over about 10 s of speech.

## Other observations

- `doctor` reports the headset microphone as digital silence (muted, switched off or boom
  flipped up). This is unresolved and blocks push-to-talk testing.
- The helper's venv `python.exe` is a launcher, so `Start-Process` sees a different PID from the
  one in the `hello` event, and its exit code reads blank. The helper logs its own exit code (0).

## A Jarvis-like voice

- Fish's voice library has fan-made "Jarvis" voices cloned from the films. Fish's cloning guide
  says never to clone celebrity or public-figure voices without permission, and such models are
  removed when the rights holder complains. They are a poor base for this project; keep any
  experiments with them private and do not commit their audio.
- Routes that stay within the rules:
  - **Fish Voice Design**: an original voice from a text description (e.g. "calm, dry, refined
    RP British male, middle-aged, AI butler"). About 2,000 platform credits per generation.
  - **Fish library voices "inspired by" the style**, e.g. British butler voices. Audition first.
  - **A local engine cloning an original voice.** Generate 2–3 clean 15–20 s clips with a designed
    voice and use them as the reference.

## Local TTS options for this PC

Fish's self-hosted `fish-speech` S2 Pro needs 24 GB of VRAM, runs on Linux or WSL only and
offers HTTP only (no `/v1/tts/live`), so it does not fit. Every engine below needs a new
`SpeechSynth` implementation (`tts/base.py`); the Fish base-URL override cannot point at them.

| Engine | Licence | Voice | Notes |
| --- | --- | --- | --- |
| Kokoro-82M | Apache-2.0 | Presets; British male `bm_george` and `bm_fable` (grade C); blending | About 300 ms first audio and 3 GB VRAM (Kokoro-FastAPI, 4060 Ti); 24 kHz; Windows needs espeak-ng |
| XTTS-v2 (`coqui-tts` fork) | CPML, non-commercial | Clones from about 6 s | Streaming under 200 ms claimed; Windows wheels |
| Chatterbox-Turbo | MIT | Clones from about 10 s | 350M parameters, English only; outputs are watermarked |
| Qwen3-TTS VoiceDesign | Apache-2.0 | Original voice from a description, then reuse as a clone prompt | About 97 ms streaming claimed |
| F5-TTS | CC-BY-NC weights | Clones | No documented streaming |
| Piper | GPL-3.0 | `en_GB` presets | Light but robotic |

## Diagnostic scripts

Run them with the helper's Python (`~/.jarvis/venv/Scripts/python.exe`). Neither prints the key.

- `plugin/voice/scripts/fish_check.py`: handshake per model, plus API and plan balances.
  Free: it sends no text.
- `plugin/voice/scripts/voice_latency.py fake|live [--model M] [--voice-id ID]`: time to
  first audio for a streamed reply.

## Open items

- Pick a voice: a Fish Voice Design voice now, then possibly a local engine.
- Decide whether to make `s2.1-pro-free` the default model and add a fallback for when it ends.
- Fix the microphone and test push-to-talk end to end.
