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
- Since f08afa1 it is the default: the plugin's **Fish Audio model** option (`fishModel`) offers
  `s2.1-pro-free` and `s2.1-pro` and passes the choice to the helper as `JARVIS_TTS_MODEL`.
  The option is a fixed list because, per Fish's docs, an unknown `model` value silently falls
  back to the paid `s2.1-pro`, which brings the 402 back. Pick a voice with `/jarvis voice <id>`.
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
- Almost all of the 1.4 s is Fish's side. It splits into two parts, measured directly
  against `wss://api.fish.audio/v1/tts/live` with `s2.1-pro-free` (3 runs per mode):

  | `latency` mode | Connect (TCP + TLS + upgrade) | Flush to first audio frame |
  | --- | --- | --- |
  | `balanced` (the helper's setting) | 654–716 ms | 635–757 ms |
  | `low` | 648–715 ms | 646–720 ms |
  | `normal` | 710–749 ms | 2,423–2,846 ms |

  The new connection the helper opens for every reply costs about 0.7 s, half the wait. `low`
  is no faster than `balanced` in these runs, and `normal` is much slower.
- `level` events appear only while speaking: none while idle, 180 over about 10 s of speech.

## Where the remaining waits come from (code audit)

A read-only audit traced the path from Claude's text to the speaker. Every stage streams:
text deltas are split into sentences as they arrive, each sentence is POSTed at once, sent to
Fish with a flush, and audio frames play as they arrive with no prebuffer. The waits a
listener notices come from these places, most important first. A reviewer re-checked each one
against the code.

1. **Nothing is heard before Claude's first text** (confirmed). Thinking and tool calls are never
   spoken (`plugin/hooks/voice.ts:298`). `onTurnStart` sends nothing to the helper. The persona's
   "answer first, no preamble" rule (`voice.ts:56`) discourages a heads-up before tool work.
   The status line also reads "ready" during this time. Fixes:
   - reword the persona so a turn that uses tools first says one short sentence about what it
     is checking;
   - optionally, play a local earcon when a voice turn starts. That needs a small new helper
     command reusing the chime code at `daemon.py:255-264`.
2. **The sentence before a tool call waits until the tool's whole input has streamed**
   (confirmed). The splitter holds a sentence that ends at the end of its buffer
   (`plugin/hooks/sentences.ts:247, 279`). The line is ended only after the step finishes
   (`voice.ts:307`). Fix: in `Voice.step`, call `turn.reply.endLine()` when a non-text chunk
   follows text. Add a test that checks the speak command right after the `tool` chunk.
3. **Each reply opens a new Fish connection, only once the first sentence is ready**
   (confirmed; it costs about 0.7 s, measured above). Fix: open a spare connection in the
   background when the user's clip is queued for transcription (`daemon.py:338-341`), and claim
   it for the first sentence. Drop it if the voice id changes or it sits idle too long.
   `SpeechSynth`/`FishStream` need a settable `on_audio`. This would cut time to first audio
   from about 1.4 s to about 0.7 s.
4. **The output device closes after 30 s of silence** (partly confirmed; small cost). Its reopen
   runs before the Fish connect (`sd_backend.py:222`, `speech.py:341`). A pause of more than
   30 s inside one reply puts the reopen right in front of the audio. Fix: keep the output
   open while a voice reply is expected or still open, with a cap.

Status on main (2026-10-07): item 3 is done (f7b9870: the Fish stream opens while the clip is
transcribed, and the first sentence reuses it). Item 2 is done (`Voice.step` ends the line when a
tool call starts, with a test). Item 1 is half done: the persona now asks for one short sentence
before tool work; the earcon is still open.

Item 4 (branch fix/output-cutoff): it did cut replies off on the Windows PC (10:54 and 11:35).
The reaper closed a healthy output during a tool pause of more than 30 s, and the reopen, on the
Fish reader thread, failed with "Unanticipated host error" and WDM-KS text. Measured on that PC
(PortAudio 19.7, WASAPI): a stream starts on the main thread, but on a worker thread only after
`CoInitializeEx`. PortAudio's WASAPI start needs COM on the calling thread, Python's worker threads
never set it up, and the WDM-KS text is stale host-error info. Now every thread initialises COM
before it opens, starts or restarts a stream, and an open reply holds the output open from its
first sentence to `speech_done` (at most 5 minutes of silence). Reopening for new audio backs off
(1 s, doubling up to 16 s). A reply whose output cannot be reopened ends at once with a plain
message, and PortAudio's text goes to the log.

Smaller items:
- There are no timing logs between the speak POST and the first audio frame. Add them first.
- Speak POSTs are sent one after another.
- There is no timer flush when Claude pauses mid-sentence.
- Sentences sent while the helper restarts are dropped.
- Socket closes other than 1000 end the reply instead of reconnecting.
- The next reply's socket opens only after the previous reply has finished playing.

## A local engine inside the helper

- What it must implement: `SpeechSynth`/`SynthStream` (`tts/base.py:23-67`). It must output
  16-bit mono PCM at exactly 24 kHz (the playback path assumes 24 kHz). `send_text` must not
  block.
- The plugin side needs no change.
- Two ways to wire it:
  - **No helper change:** run a local bridge that speaks Fish's `/v1/tts/live` protocol
    (`tests/fish_fake.py` is the template). Point `JARVIS_FISH_BASE_URL` at it, with any
    non-empty key.
  - **Built in:** a new `tts/kokoro.py`, plus a `JARVIS_TTS_ENGINE` switch in `cli.py`, new
    error codes, setup and doctor steps, and a `local-tts` extra (also passed from
    `plugin/hooks/setup.ts`).
- Avoid putting `onnxruntime-gpu` or a torch-CUDA voice inside the helper. It would clash with
  the CPU `onnxruntime` that faster-whisper already pulls in and with the pinned cuDNN. A GPU
  voice-cloning model is better run as a separate bridge process.

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

- Latency fixes 1–3 above, starting with timing logs.
- Pick a voice: a Fish Voice Design voice now, then possibly a local engine.
- Decide what happens when `s2.1-pro-free` ends on 2026-11-30 (API credit, or a local engine).
- Fix the microphone and test push-to-talk end to end.
