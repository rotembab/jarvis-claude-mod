# jarvis-voice

The voice helper for the Jarvis Claude Code mod. The mod starts one copy of it per
Claude Code session as a child process. It handles:

- the microphone and push-to-talk (PTT);
- speech-to-text with faster-whisper;
- text-to-speech over the Fish Audio live WebSocket API;
- audio playback and barge-in;
- echo cancelling (WebRTC's AEC3, through livekit), so the wake word and barge-in
  don't hear Jarvis's own voice through speakers.

The mod and the helper talk over a versioned JSON protocol. The protocol is defined in
`../protocol/schema.json`, and a copy ships inside the package as
`jarvis_voice/protocol_schema.json`.

## Commands

```
python -m jarvis_voice run    [--data-dir D] [--stt-model M] [--ptt-key K] [--voice-id ID]
                              [--language L] [--input-device NAME] [--output-device NAME]
                              [--aec on|off] [--instance-name N] [--log-level LEVEL]
                              [--fake-audio] [--fake-stt] [--fake-fish URL]
python -m jarvis_voice setup  [--data-dir D] [--stt-model M] [--no-verify]
python -m jarvis_voice doctor [--data-dir D] [--no-network] [--no-mic]
python -m jarvis_voice probe-cuda --model DIR      (internal: used by run and setup)
```

The installed `jarvis-voice` script does the same as `python -m jarvis_voice`.

- **run**: the daemon.
  - stdout carries one ASCII JSON event per line. `hello` (with the control port) is
    always the first line. The helper keeps a private copy of the stdout descriptor for
    these lines and points descriptor 1 at stderr, so nothing else (not even native
    libraries) can write into the stream.
  - Logs go to stderr and to `<dataDir>/logs/voice.log`.
  - Commands arrive as `POST http://127.0.0.1:<port>/v1/<command>` with
    `Authorization: Bearer $JARVIS_TOKEN`.
  - The helper exits when no `heartbeat` arrives for 15 s. There is a 60 s grace
    period before the first one, and the 15 s restart after the computer wakes from sleep.
- **setup**: downloads the Whisper model into `<dataDir>/models` and test-loads it.
  - stdout gets one JSON progress line per step: `prepare`, `detect`, `download`,
    `verify`, then `done` or `error`.
- **doctor**: prints a JSON report. It covers audio devices, a 1 s microphone test,
  CUDA, downloaded models, Fish Audio (the key is masked), the wake word, echo
  cancelling (a self-test on a synthetic echo: nothing is played) and PTT availability.

Exit codes:

| Code | Meaning |
|------|---------|
| 0 | OK |
| 1 | Error |
| 2 | Usage error, or `JARVIS_TOKEN` missing |
| 3 | Another instance is already running |

## Environment

| Variable | Purpose |
|----------|---------|
| `JARVIS_TOKEN` | Bearer token for the control server. Required by `run`; with any `--fake-*` flag a fixed test token is used instead. |
| `FISH_AUDIO_API_KEY` | Fish Audio API key. It is never logged. |
| `JARVIS_TTS_MODEL` | Fish model header. Default `s2.1-pro`. |
| `JARVIS_FISH_BASE_URL` | Override the Fish API base URL. |
| `JARVIS_VOICE_ID` | Fish `reference_id` for the voice. |
| `JARVIS_STT_MODEL` | `auto` (default): `large-v3-turbo` on CUDA, `small.en` on CPU. Or any faster-whisper model name or path. |
| `JARVIS_PTT_KEY` | Push-to-talk key, e.g. `right ctrl` (default), `f13`, `alt+space`, `vk:123`. |
| `JARVIS_LANGUAGE` | Whisper language code. Default `en`. |
| `JARVIS_INPUT_DEVICE` / `JARVIS_OUTPUT_DEVICE` | Device name, or part of one. Default: the system default. |
| `JARVIS_AEC` | `off` turns echo cancelling off (the `--aec` default). Default `on`. |
| `JARVIS_INSTANCE_NAME` | Name of the single-instance lock. Default `JarvisVoice`. |
| `JARVIS_DATA_DIR` | Data directory. Default `~/.jarvis`. |
| `JARVIS_LOG_LEVEL` | Log level. Default `INFO`. |

## GPU (optional)

`/jarvis setup` installs the `cuda` extra when it finds an NVIDIA driver. By hand:

```
uv sync --project <plugin>/voice --python 3.12 --no-dev --no-editable --extra cuda
```

The `cuda` extra installs `nvidia-cublas-cu12` and, on Windows, `nvidia-cudnn-cu12`. The
cuDNN version is pinned to the `cudnn64_9.dll` that the pinned CTranslate2 wheel bundles,
so bump `ctranslate2` and `nvidia-cudnn-cu12` together. On Windows the helper adds the
wheels' `nvidia/*/bin` folders to the DLL search path before importing CTranslate2.

A broken CUDA setup can abort the process instead of raising (a missing cuDNN
sub-library does), so the first GPU load of a model runs in a child process
(`python -m jarvis_voice probe-cuda --model <dir>`). The helper uses the GPU only if
that child succeeds; otherwise it falls back to CPU int8. Results are cached in
`<dataDir>/models/cuda-probe.json` (`doctor` shows them), and `setup` always tests again.

## Development

```
uv sync                      # Python 3.12 from .python-version; dev group included
uv run pytest                # unit tests + subprocess integration tests (no audio hardware needed)
uvx ruff check src tests && uvx ruff format --check src tests
uv run --with mypy mypy src/jarvis_voice --ignore-missing-imports
uv build                     # sdist + wheel; the wheel carries the schema
```

The integration tests run the real daemon (`python -m jarvis_voice run --fake-audio
--fake-stt --fake-fish <url>`) against a local fake Fish WebSocket server
(`tests/fish_fake.py`). They drive it the same way the mod does.
