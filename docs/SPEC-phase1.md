# Jarvis for Claude Code: phase 1 build spec ("Talking Jarvis")

Source of truth for the phase 1 build. The product plan lives in the project's plan doc; this file is the engineering contract both halves build against.

## Goal of phase 1

On Rotem's Windows 11 PC (i7-13700F, RTX 4070 Ti, Logitech PRO X wireless headset, uv 0.11 with Python 3.12, Claude Code 2.1.289, Windows Terminal, Git Bash NOT on PATH, so Claude uses its PowerShell tool), inside Claude Code (Windows Terminal or the desktop app Code tab):

1. `/jarvis setup` installs the voice helper (Python 3.12 venv via uv) and downloads the speech-to-text model into `%USERPROFILE%\.jarvis`.
2. Hold the push-to-talk key (default: Right Ctrl), speak, release.
3. The helper transcribes locally (faster-whisper; CUDA when available, else CPU int8) and sends the text to the mod.
4. The mod submits it as a prompt (as the user's words), Claude answers in the JARVIS persona.
5. As Claude streams its reply, the mod splits it into sentences and sends each to the helper, which streams them to Fish Audio TTS and plays the audio as it arrives.
6. `/jarvis stop` (or pressing push-to-talk while Jarvis speaks) stops speech immediately.

Wake word, echo cancelling and barge-in-by-voice are phase 2; the HUD is phase 3; desktop actions and the tool guard are phase 4. Phase 1 must not block those: keep the interfaces below.

## Repository layout

```
.claude-plugin/marketplace.json   repo root is a plugin marketplace; lists plugin "jarvis" at ./plugin
plugin/                           the only folder that ships
  .claude-plugin/plugin.json      name "jarvis", version, description, userConfig, "types"
  hooks/hooks.json                { "modules": ["./register.tsx"] }
  hooks/register.tsx              entry: wires the modules below
  hooks/platform.ts               ALL OS-specific choices for the mod (paths, python exe, data dir)
  hooks/helper.ts                 spawn/restart helper, read events, send commands
  hooks/sentences.ts              pure sentence splitter for speech (unit tested)
  hooks/voice.ts                  voice turns: submit utterances, stream reply sentences, persona
  hooks/commands.ts               /jarvis slash commands
  hooks/ui.tsx                    status line + band above the prompt (minimal in phase 1)
  hooks/protocol.ts               TypeScript types for plugin/protocol/schema.json
  hooks/*.test.ts                 `claude plugin test` tests
  types/index.d.ts                PluginState contract for $.state values
  protocol/schema.json            JSON Schema for helper<->mod messages (shared contract)
  voice/                          Python helper "jarvis-voice" (uv project, src layout)
    pyproject.toml                build backend uv_build or hatchling (NOT setuptools)
    src/jarvis_voice/...
    tests/...
docs/                             this spec, dev notes
README.md, LICENSE (MIT), .gitignore, .github/workflows/ci.yml
```

## Process model

- The mod starts the helper with `$.process.spawn({ argv, env })` at `session.start`, only when the session's engine runs on the user's own machine with a local surface (terminal or desktop). Not in cloud/remote sessions.
- argv: `[<dataDir>/venv/Scripts/python.exe, "-m", "jarvis_voice", "run", "--data-dir", <dataDir>]` on Windows (`bin/python` elsewhere). Absolute path; never a bare `python`/`py` (Rotem has no `py` launcher and system python is 3.14).
- env adds: `PYTHONUNBUFFERED=1`, `PYTHONUTF8=1`, `JARVIS_TOKEN=<random per spawn>`, `JARVIS_PARENT=claude-code`, and `FISH_AUDIO_API_KEY` from the plugin's sensitive userConfig if set (otherwise the helper inherits it from the process env, which Claude Code fills from settings.json `env`). `NO_PROXY` gains `127.0.0.1,localhost`.
- stdin is closed (spawn has no input). Helper -> mod: one JSON object per line on stdout. Mod -> helper: HTTP POST to `http://127.0.0.1:<port>/v1/<command>` with header `Authorization: Bearer <token>`, JSON body, via `$.http.fetch`.
- The helper prints `hello` (with its port) as its first stdout line. Everything else it logs goes to stderr and to `<dataDir>/logs/voice.log` (rotating).
- Lifecycle: the mod POSTs `/v1/heartbeat` every 2 s. The helper exits if it has seen no heartbeat for 15 s (covers the venv redirector grandchild and crashes). Hot reloads kill the child; the mod restarts it on (re)load. Do NOT shut the helper down on `session.end` (it fires on /clear and /resume with no session.start after).
- Single instance: the helper takes a per-user named mutex (`Local\\JarvisVoice` via kernel32 CreateMutexW on Windows; an flock'd file elsewhere). If it is taken, the helper prints `error` code `already_running` then exits 3; the mod shows "JARVIS · active in another window" and does not retry until the user runs `/jarvis`.
- Restart policy: if the helper exits unexpectedly, restart with backoff 1 s, 2 s, 5 s, 10 s, then stop and show the error.

## Protocol (plugin/protocol/schema.json is authoritative)

All messages carry `"v": 1` and `"type"`. Helper -> mod events (stdout lines):

| type | fields | when |
| --- | --- | --- |
| hello | port, pid, platform ("windows"\|"macos"\|"linux"), version, capabilities: string[] | first line, after the control server is listening |
| state | state: "starting"\|"sleeping"\|"listening"\|"transcribing"\|"speaking"\|"error" | on every change |
| level | mic: 0..1, out: 0..1 | at most 15 per second while listening or speaking (phase 3 HUD uses it) |
| utterance | id, text, source: "ptt"\|"command"\|"wake", durationMs, language? | after transcription of a non-empty clip |
| speech_started | replyId | first audio of a reply plays |
| speech_done | replyId, interrupted: bool, spokenText | reply finished or was stopped |
| barge_in | replyId?, spokenText | reserved for phase 2 (speech over Jarvis); phase 1 emits it when push-to-talk is pressed while speaking |
| error | code, message, hint?, fatal: bool | see codes below |
| ready | sttModel, sttDevice, voiceId?, pttKey | when models are loaded and PTT is armed |

Error codes: `already_running`, `fish_key_missing`, `fish_auth_failed`, `fish_unreachable`, `mic_blocked` (Windows privacy switches), `mic_in_use`, `no_input_device`, `no_output_device`, `stt_model_missing`, `stt_failed`, `ptt_unavailable`, `internal`.

Mod -> helper commands (POST `/v1/<name>`, JSON body; response `{ "ok": true, ... }` or `{ "ok": false, "error": { code, message } }`):

| name | body | effect |
| --- | --- | --- |
| heartbeat | {} | keeps helper alive |
| speak | replyId, seq, text, final: bool | queue sentence `seq` of reply `replyId`; `final: true` (text may be empty) closes the reply |
| stop | reason? | stop playback now, drop queue, close the TTS stream; emits speech_done(interrupted) |
| listen | action: "start"\|"stop" | command-driven push-to-talk (for /jarvis talk) |
| config | voiceId?, pttKey?, sttModel?, language? | apply settings live |
| status | {} | returns state, devices, models, versions |
| test_voice | text? | speak a test line |
| shutdown | {} | exit cleanly |

Security: bind 127.0.0.1 only, port 0 (OS picks). Compare the bearer token in constant time. Reject any request with an `Origin` header, or a `Host` other than `127.0.0.1:<port>`/`localhost:<port>`. Body limit 64 KB.

## Mod responsibilities (plugin/hooks)

- `platform.ts`: OS detection; data dir = `%USERPROFILE%\.jarvis` on Windows (read `USERPROFILE`, never `HOME`), `~/.jarvis` elsewhere; venv python path; uv lookup order (`%USERPROFILE%\.local\bin\uv.exe`, WinGet links folder `%LOCALAPPDATA%\Microsoft\WinGet\Links\uv.exe`, then PATH via `where`/`which`); persona's OS paragraph (PowerShell tool, Windows paths, PowerShell 5.1 has no && or ||).
- `helper.ts`: spawn, line reader over the spawn stream (handle partial lines), event dispatch, command client with timeouts, heartbeat timer, restart policy, single-instance handling.
- `voice.ts`:
  - On `utterance`: submit it with `$.prompt.submit({ text, asUser: true })` and remember that the next turn is a voice turn. If a turn is running, queue it (or abort the running voice turn if the user spoke while Jarvis was speaking).
  - Persona: a system prompt section via `prompt.compose`, written in the JARVIS voice (dry, British, unflappable, brief; "sir" sparingly; 1 to 3 sentences out loud; long output stays on screen and Jarvis says where it is; states plainly what risky actions do). It must apply to voice turns; typed turns keep Claude's normal style. Use whatever the API offers to scope it (e.g. a per-turn section or a marker in context); if only session scope is possible, make the section conditional ("for messages that arrive by voice...") and mark voice messages accordingly.
  - Reply streaming: hook `turn.step` (async generator: forward every chunk unchanged with `yield*`/for-await) and collect the assistant's text deltas for voice turns only; feed them to the sentence splitter; POST `speak` per sentence; on turn end POST `speak` with `final: true`. Never speak tool-call JSON. Replace fenced code blocks and long tables with a short spoken note ("The code is on screen, sir.").
  - `/jarvis stop`, and `barge_in` from the helper, abort the running voice turn with `$.turn.abort` (track the turn id from `turn.start`).
- `sentences.ts`: incremental splitter: push(delta) -> sentences[]; flush() -> rest. Handles abbreviations (Mr., Dr., e.g., i.e., etc.), decimals (3.14), ellipses, URLs, file paths (C:\Users\...), markdown (strip **, `, #, list bullets), code fences (skip content, emit placeholder once), and a max length (split long sentences at commas around 220 chars). First sentence should be emitted as early as possible.
- `commands.ts`: `/jarvis` (status + help), `/jarvis setup` (runs uv via `$.process.spawn`, streams progress to the status line; if uv is missing, prints the one-line install command `winget install astral-sh.uv` and stops; never auto-downloads installers in phase 1), `/jarvis stop`, `/jarvis talk` (toggle command-driven listening), `/jarvis test` (test_voice), `/jarvis restart`, `/jarvis voice <id>`, `/jarvis devices`.
- `ui.tsx`: status line entry "JARVIS · <state>" (and an error hint), a band above the prompt showing the live/last utterance while listening/transcribing; nothing when idle.
- userConfig (each field needs `title` and `description`): `fishApiKey` (string, sensitive, optional), `voiceId` (string, optional; Fish Audio model reference id), `pttKey` (string, default "right ctrl"), `sttModel` (string picker: "auto", "base.en", "small.en", "small", "medium", "large-v3-turbo"; default "auto" = large-v3-turbo on CUDA, small.en on CPU), `language` (string, default "en").
- Guard rails: a `.catch` on every gating hook that should fail closed; tool matchers are exact (arrays/RegExp), not "A|B" strings. Phase 1 adds no tool guard yet (phase 4).

## Helper responsibilities (plugin/voice, package jarvis_voice)

- Python 3.11 or 3.12 (`requires-python = ">=3.11,<3.13"`); pin 3.12 in setup.
- Dependencies (phase 1): numpy, sounddevice, soxr, faster-whisper (pin ctranslate2 >= 4.6.3), websockets, ormsgpack (Fish live TTS uses msgpack frames: verify against Fish docs / fish-audio-sdk source), pynput (global push-to-talk on Windows/macOS). Optional extra `cuda` adds nvidia-cublas-cu12 (and cudnn only if needed) with DLL dirs registered via os.add_dll_directory on Windows.
- CLI: `python -m jarvis_voice run --data-dir D` (the daemon), `python -m jarvis_voice setup --data-dir D [--stt-model M]` (downloads models, prints JSON-lines progress `{type:"progress", step, pct, message}`), `python -m jarvis_voice doctor` (prints a JSON report: devices, mic access, CUDA, Fish reachability).
- Modules (suggested): `protocol.py` (dataclasses + encode/validate against schema), `events.py` (stdout writer thread with a queue; never write from the audio callback), `control.py` (ThreadingHTTPServer, auth, routing), `lifecycle.py` (heartbeat watchdog, single-instance lock), `audio/devices.py` (WASAPI-first device selection by NAME, default device, reopen on change), `audio/capture.py` (16 kHz mono float32 capture; sounddevice with `WasapiSettings(auto_convert=True)` on Windows; resample with soxr if needed), `audio/playback.py` (OutputStream fed from a queue, instant stop, levels), `audio/chimes.py` (generated tones, no asset files), `stt/base.py` (`Transcriber` protocol: `transcribe(pcm16k: np.ndarray, language) -> Result`), `stt/faster_whisper_engine.py`, `tts/base.py` (`SpeechSynth` protocol: streaming text in, PCM chunks out, cancel), `tts/fish.py` (Fish Audio live WebSocket client; one connection per reply; opened on the first sentence; request 16-bit PCM at 24 or 32 kHz), `ptt/base.py` + `ptt/pynput_backend.py` (hold-to-talk, key name parsing, works without focus), `platform/{windows,macos,linux}.py` (mutex/lock, mic privacy hint text, opening `ms-settings:privacy-microphone`), `daemon.py` (state machine wiring everything).
- State machine: starting -> sleeping (models loaded, PTT armed; emit `ready`) -> listening (PTT held; chime; capture with 300 ms pre-roll) -> transcribing -> sleeping (emit utterance if text non-empty; ignore clips < 300 ms or silent) ... speaking (on first `speak` audio) -> sleeping (on final drained or stop). PTT press while speaking = stop speech + emit `barge_in` + start listening.
- Fish Audio: base URL configurable for tests (`JARVIS_FISH_BASE_URL`, default the real API). Key from `FISH_AUDIO_API_KEY`. Voice from config `voiceId` (Fish "reference_id"); if missing use Fish's default voice and emit a one-time hint. Handle auth errors (fish_auth_failed), network errors (fish_unreachable) without crashing.
- Errors are events, not crashes. Unexpected exceptions: log with traceback to the log file, emit `error` code `internal`, keep running when possible.
- Never print secrets (mask the key in logs/doctor output).

## Testing

- Mod: `claude plugin validate plugin` must pass; `claude plugin test plugin` runs `hooks/*.test.ts` (sentence splitter, utterance -> prompt.submit, turn.step text -> speak commands with final, stop -> turn.abort + stop command, helper restart/backoff, commands output); `tsc -p plugin` type-checks once types are laid (or with a tsconfig pointing at the bundled claude-code.d.ts). No live helper needed: fake `$.process.spawn` / `$.http.fetch` via the testing kit's mocks.
- Helper: pytest, no audio hardware, no network: fake audio backends, a fake Fish WebSocket server (local), fake transcriber, fake PTT key events. Tests for protocol encoding/schema conformance, control server auth (token, Origin, Host, body limit), heartbeat watchdog, single-instance lock, speech queue + instant stop, Fish client against the fake server (msgpack frames, auth error), state machine end to end (PTT press -> utterance event; speak -> speech_started/speech_done; PTT during speech -> barge_in), sentence-level playback ordering by seq.
- Integration: a pytest that spawns `python -m jarvis_voice run` in `--fake-audio --fake-stt --fake-fish` mode as a subprocess, reads hello from stdout, drives it over HTTP like the mod does, and checks the event sequence.
- CI (.github/workflows/ci.yml): helper tests via uv on windows-latest and macos-latest (and ubuntu-latest); plugin validate on ubuntu with `npm i -g @anthropic-ai/claude-code` (if `claude plugin test` needs auth in CI, keep validate + tsc and note it).

## Out of scope for phase 1

Wake word, echo cancelling, voice barge-in detection, HUD ring, desktop actions, tool guard, hardened mode, Mac-specific backends beyond stubs, code signing.

## Fish Audio live TTS protocol (researched 2026-10-06; re-verify with WebFetch on docs.fish.audio and the fish-audio-sdk source)

- WebSocket `wss://api.fish.audio/v1/tts/live`, header `Authorization: Bearer <FISH_AUDIO_API_KEY>`, optional header `model: <name>` (default `s2.1-pro`; accepted `s1`, `s2-pro`, `s2.1-pro`, `s2.1-pro-free`). Env `JARVIS_TTS_MODEL` overrides; default `s2.1-pro`.
- MessagePack frames. Client sends `{event:"start", request:{text:"", format:"pcm", sample_rate:24000, reference_id:<voiceId or omitted>, latency:"balanced", chunk_length:<100..300>, prosody:{speed:1.0, volume:0}}}`, then `{event:"text", text:<sentence + " ">}` per sentence, `{event:"flush"}` to force generation (after each sentence for low latency), and `{event:"stop"}` at the end of the reply.
- Server sends `{event:"audio", audio:<bytes>}` repeatedly, then `{event:"finish", reason:"stop"|"error"}`, after which it closes the socket. One socket per reply.
- PCM is 16-bit little-endian mono; allowed sample rates 8000, 16000, 24000, 32000, 44100 (default 44100). Use 24000 (or 32000) and let WASAPI auto_convert/soxr adapt to the device.
- The official SDK (`fish-audio-sdk` 1.3.x: httpx, httpx-ws, ormsgpack, pydantic) has `tts.stream_websocket()`; defaults to model `s2-pro`, so pass the model explicitly if used. Either the SDK or a small client on `websockets` + `ormsgpack` is fine; the fake server in tests must speak the same frames.
- The container that builds this cannot reach api.fish.audio (egress policy); live testing happens on Rotem's PC.
