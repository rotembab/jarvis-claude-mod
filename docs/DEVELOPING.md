# Developing Jarvis

How to work on Jarvis on Windows: run your working copy inside Claude Code, run the tests, drive the voice helper by hand, and find out what went wrong. The engineering contract is [SPEC-phase1.md](SPEC-phase1.md); the helper <-> mod message format is [plugin/protocol/schema.json](../plugin/protocol/schema.json).

Commands below are for PowerShell (Windows Terminal). Git Bash is not needed.

## Layout

```text
.claude-plugin/marketplace.json   makes the repo a plugin marketplace listing "jarvis" at ./plugin
plugin/                           the plugin, the only folder that ships
  .claude-plugin/plugin.json      manifest and userConfig (Fish Audio, voice engine, wake word, barge-in, echo cancelling, model routing, push-to-talk, speech model)
  hooks/                          the mod: TypeScript hooks module (register.tsx) and its *.test.ts
  types/index.d.ts                the mod's $.state contract
  tsconfig.json                   type-checks the mod (tsc -p plugin)
  protocol/schema.json            helper <-> mod messages (authoritative)
  voice/                          the voice helper: Python package jarvis_voice (uv project)
    src/jarvis_voice/home/        home control: device list, credential store, drivers, setup wizard
docs/                             the plan, the phase 1 spec, the home-control guide (HOME.md) and this file
```

## Prerequisites

- Git, and [uv](https://docs.astral.sh/uv/) (`winget install astral-sh.uv`). uv fetches Python 3.12 itself; you do not need a system Python.
- Claude Code 2.1.287 or later (`claude --version`).
- A Fish Audio API key in the `env` block of `%USERPROFILE%\.claude\settings.json` as `FISH_AUDIO_API_KEY` (see "Options while developing" below). Never commit it, and never put it in a project's `.claude\settings.json`.

## Run your working copy in Claude Code

```powershell
git clone https://github.com/rotembab/jarvis-claude-mod.git
cd jarvis-claude-mod
claude --plugin-dir "$PWD\plugin"
```

Point `--plugin-dir` at `plugin`, not at the repository root: the flag loads the plugin in that folder, and a folder without a manifest is read as a folder of plugins. The plugin is loaded for that session only, as `jarvis@inline`.

In the session, run `/jarvis setup` once. It installs the helper from `plugin\voice` in your working copy into `%USERPROFILE%\.jarvis\venv` and downloads the speech model into `%USERPROFILE%\.jarvis\models`. After that, the mod starts the helper at every session start.

If you also installed Jarvis from the marketplace, disable that copy while you develop so only one loads:

```powershell
claude plugin disable jarvis@jarvis-claude-mod
```

**Desktop app.** Its Code tab cannot take a flag, so name the folder in `CLAUDE_CODE_PLUGIN_DIRS` in the `env` block of `%USERPROFILE%\.claude\settings.json` (user settings only; a project's settings are ignored for this). It takes one or more absolute paths, separated by `;` on Windows. A long-lived desktop session watches the folder for changes only when `CLAUDE_CODE_PLUGIN_DIR_WATCH` is `1`, set the same way:

```json
{
  "env": {
    "CLAUDE_CODE_PLUGIN_DIRS": "C:\\src\\jarvis-claude-mod\\plugin",
    "CLAUDE_CODE_PLUGIN_DIR_WATCH": "1"
  }
}
```

### Options while developing

A `--plugin-dir` plugin reads its options from `pluginConfigs` in your settings, under the key `jarvis` (or `jarvis@inline`). The non-secret ones (`voiceId`, `pttKey`, `sttModel`, `language`) are rows in `/config`, and changing one there reloads the mod with the new value. For the Fish Audio key, use the `FISH_AUDIO_API_KEY` environment variable from your user settings `env` block; Claude Code reads that block at startup, so restart Claude Code after changing it.

## Hot reload

In an interactive session, the `--plugin-dir` folder is watched:

- Saving a file in the plugin folder reloads the hooks module: `register` runs again in a fresh environment, and the old environment's timers are dropped. Saves from your editor reload once the folder has been quiet (a lone save after about a quarter of a second). Saves Claude makes during its own turn reload once, when the turn ends, or sooner when a command the plugin registered (such as `/jarvis`) is about to run.
- A reload kills the voice helper, and the reloaded mod starts a new one, so after every save the status line goes through `JARVIS · starting` back to `JARVIS · ready · hold right ctrl to talk`.
- Module-level variables do not survive a reload; anything that must lives in `$.state` or `$.store`.
- When a module fails to load, or a hook throws and is skipped, the transcript says so once, in a dim line naming the plugin, the event and the reason. Start Claude Code with `--debug` (see [Logs](#logs)) for every occurrence.

### Python changes

`/jarvis setup` installs the helper as a regular, non-editable package and rebuilds it on every run (`uv sync --reinstall-package jarvis-voice`), so running it again picks up edited Python files even when the version in `plugin\voice\pyproject.toml` stays the same. For a quicker loop, install the helper editable into the same venv once, with Claude Code closed (Windows keeps a running helper's files locked):

```powershell
$env:UV_PROJECT_ENVIRONMENT = "$env:USERPROFILE\.jarvis\venv"
uv sync --project plugin\voice --python 3.12 --extra cuda   # leave out --extra cuda without an NVIDIA GPU
Remove-Item Env:UV_PROJECT_ENVIRONMENT
```

From then on the venv runs the code in your working copy: save a `.py` file, then run `/jarvis restart` (or let a hot reload restart the helper). Running `/jarvis setup` later turns it back into a regular install.

## Tests

### Helper (Python)

```powershell
uv sync --locked --project plugin\voice --group dev
uv run --locked --project plugin\voice pytest -q plugin\voice\tests
```

This creates `plugin\voice\.venv` (ignored by git). The tests need no microphone, speakers, GPU or network: audio, push-to-talk, speech-to-text and Fish Audio are faked, the Fish fake being a local WebSocket server (`tests\fish_fake.py`). The hands-free tests drive the listener with fakes too, and Silero VAD (shipped inside faster-whisper) on synthetic speech in `tests\data`. Two tests run the real "Hey Jarvis" model and are skipped unless `JARVIS_WAKE_MODELS_DIR` names a folder for it (they download it there, about 3.7 MB, when it is missing); CI sets it. Three more run the plain "Jarvis" model beside it, only when `jarvis_v2.onnx` is already in that folder: the tests never download it, so CI skips them. `tests\test_integration.py` starts `python -m jarvis_voice run` as a subprocess in fake mode and drives it over HTTP the way the mod does. The named-mutex test runs on Windows only.

The home-control tests (`tests\test_home_*.py`) use fake drivers (`tests\home_fakes.py`) and fake devices served on 127.0.0.1, so they need no Apple TV, TV or Tuya device either. The DPAPI test runs on Windows only.

`--locked` fails if `uv.lock` no longer matches `pyproject.toml`. After changing dependencies, run `uv lock --project plugin\voice` and commit the new `uv.lock`; `/jarvis setup` installs from it.

### Mod (TypeScript)

```powershell
claude plugin validate .        # the marketplace file, and the plugin.json it lists
claude plugin validate plugin   # manifest, userConfig, types contract and hooks module
claude plugin test plugin       # runs plugin\hooks\*.test.ts against the engine
tsc -p plugin                   # type-checks the mod (see below)
```

`claude plugin test` needs no login or API key: the tests replace `$.process.spawn` and `$.http.fetch` with mocks, so no helper runs. `claude plugin validate plugin` also lists what the module hooks and calls, which is the quickest check that the engine sees what you meant.

`tsc -p plugin` (TypeScript 5.4 or later, `npm i -g typescript`) reads the API's declarations from `plugin\.claude-plugin\types\`, which Claude Code writes there (ignored by git) each time it loads the mod from your folder. Start `claude --plugin-dir "$PWD\plugin"` once after cloning, and again after Claude Code updates, before you type-check.

### CI

[.github/workflows/ci.yml](../.github/workflows/ci.yml) runs the helper tests on Windows, macOS and Ubuntu, and the validate and test commands above on Ubuntu with the latest Claude Code from npm. It does not run `tsc -p plugin`: the declarations exist only once a Claude Code session has loaded the mod, and no command writes them without one (the runner has no login). Type-check locally before you push.

## Releasing

Users update with `claude plugin update jarvis@jarvis-claude-mod`, which installs a new copy only when `version` in `plugin\.claude-plugin\plugin.json` changes. Bump it with every push to main that changes anything under `plugin\`, together with `version` in `plugin\voice\pyproject.toml` and `__version__` in `plugin\voice\src\jarvis_voice\__init__.py`, then run `uv lock --project plugin\voice`. `tests\test_version.py` fails if the three differ. Use a patch bump (0.2.1) for fixes and a minor bump (0.3.0) for new features.

## Run the helper by hand

The mod normally starts the helper; you can run it yourself to watch its events or poke its control server.

### Fake mode

Fake mode needs no hardware, no speech model and no token:

```powershell
uv run --project plugin\voice python -m jarvis_voice run --data-dir "$env:TEMP\jarvis-dev" `
  --fake-audio --fake-stt --instance-name JarvisVoiceDev --heartbeat-timeout 600 --heartbeat-grace 600
```

- `--fake-audio` replaces the microphone, speakers and push-to-talk key; `--fake-stt` always transcribes the same sentence (set `JARVIS_FAKE_STT_TEXT` to change it).
- `--instance-name` lets it run next to the helper of an open Claude Code session, which holds the default `JarvisVoice` lock (a second helper with the same name prints `already_running` and exits 3).
- The two heartbeat flags stretch the watchdog to 10 minutes, so it does not exit while you type. Without them it exits when no heartbeat has arrived for 15 seconds (60 before the first).
- With no `JARVIS_TOKEN` set, fake mode accepts the token `jarvis-fake-token`.

Its first stdout line is `hello`, carrying the port; every other stdout line is an event (one JSON object per line). Its log goes to stderr and to `logs\voice.log` in the data folder. From a second terminal:

```powershell
$port = 53124   # the port from the hello line
$jarvis = @{ Method = 'Post'; ContentType = 'application/json'; Headers = @{ Authorization = 'Bearer jarvis-fake-token' } }

Invoke-RestMethod @jarvis -Uri "http://127.0.0.1:$port/v1/status" -Body '{}'
Invoke-RestMethod @jarvis -Uri "http://127.0.0.1:$port/v1/listen" -Body '{"action":"start"}'
Invoke-RestMethod @jarvis -Uri "http://127.0.0.1:$port/v1/listen" -Body '{"action":"stop"}'
# the first terminal prints state listening, state transcribing, then an utterance
Invoke-RestMethod @jarvis -Uri "http://127.0.0.1:$port/v1/shutdown" -Body '{}'
```

`speak` and `test_voice` go to Fish Audio unless you pass `--fake-fish <url>` with the address of a fake server. With `--fake-audio` the synthesized audio goes to a fake player, so you hear nothing; the events (`speech_started`, `speech_done`) still arrive. `tests\test_integration.py` shows the whole sequence against the fake Fish server.

The control server answers only on `127.0.0.1`, only with the bearer token, and refuses any request that carries an `Origin` header or another `Host`, so browsers cannot reach it.

### Real audio

To run the installed helper on your real microphone and speakers, outside Claude Code (close Claude Code first, or pass `--instance-name` as above):

```powershell
$env:JARVIS_TOKEN = 'dev-token'
& "$env:USERPROFILE\.jarvis\venv\Scripts\python.exe" -m jarvis_voice run --heartbeat-timeout 600 --heartbeat-grace 600
```

It uses `%USERPROFILE%\.jarvis` (models, logs) by default, and `FISH_AUDIO_API_KEY` from the environment. Use `Bearer dev-token` in the requests above.

### Doctor

```powershell
& "$env:USERPROFILE\.jarvis\venv\Scripts\python.exe" -m jarvis_voice doctor
```

Prints a JSON report: audio devices, microphone access (a one-second test recording), CUDA, the installed speech models, the wake word models, an echo-cancelling self-test (a synthetic 24 kHz echo 30 ms late, fed the way the speaker's audio is; nothing is played or recorded), the push-to-talk backend, and whether Fish Audio accepts your key (a handshake only: no text is sent, nothing is billed). `--no-mic` and `--no-network` skip those checks.

The doctor reads `FISH_AUDIO_API_KEY` from its own environment, and a plain PowerShell window does not have the `env` block of Claude Code's settings. Set it for that window with `$env:FISH_AUDIO_API_KEY = Read-Host 'Fish Audio key'`, which keeps the key out of your command history. The key is masked in the report.

## Home control

The helper answers the `home` command (`list`, `status`, `do`, `info`, `reload`); the mod registers it as the model tool `home_control` and handles on-screen confirmations. User-facing steps are in [HOME.md](HOME.md).

```text
plugin/voice/src/jarvis_voice/home/
  model.py         devices, commands (CommandSpec), tiers, value parsing, command synonyms
  store.py         devices.json and credentials.dat (DPAPI on Windows), shared by the helper and the setup window
  service.py       HomeService: finds the device, checks the tier, runs the driver with a lock and a time limit
  base.py          the Driver interface and the registries (DRIVERS, WIZARD_STEPS)
  runner.py        one asyncio loop thread for the async libraries (pyatv, the Smart Life SDK)
  net.py           plain HTTP without a proxy, Wake-on-LAN
  wizard.py        the setup console (`/jarvis home setup`), run in a window of its own
  commandline.py   `jarvis_voice home ...` without the helper
  appletv.py, bravia.py, tuya*.py, homeassistant.py   the drivers and their setup steps
```

**Safety rules.** Plugin-answered tools skip Claude Code's permission prompts, so the tiers are enforced here: the service returns `confirm` for a `screen` command until the mod sends `confirmed: true` after the user clicked yes in `$.ui.ask`, refuses `never` commands, and the mod refuses `do` in plan mode. `jarvis_voice home call` (the mod's fallback when the helper is not running) strips `confirmed`, and `home do` asks only in an interactive console, so nothing reachable through Claude's shell can confirm a `screen` command.

**Credentials** go only through `HomeStore.set_secret`, are typed only in the setup window, and never appear in `devices.json`, tool results, logs, argv or test fixtures. Driver loggers are clamped in `base.QUIET_LOGGERS`.

**Adding a driver.** Subclass `base.Driver` (`commands` reads saved data only, never the network; `run` and `status` finish within `DriverContext.call_timeout`), add it to `DRIVERS`, and add its setup step to `WIZARD_STEPS`. Use the canonical command names in `model.COMMAND_SYNONYMS` so "switch on" and "turn on" mean the same everywhere. Never poll a device in the background: a request to an Apple TV wakes it and, over HDMI-CEC, the TV.

Try it from a PowerShell window, using your real data folder or a scratch one:

```powershell
$py = "$env:USERPROFILE\.jarvis\venv\Scripts\python.exe"
& $py -m jarvis_voice home setup                      # the setup console, in this window
& $py -m jarvis_voice home --data-dir "$env:TEMP\jh" setup   # the same, on a scratch folder
& $py -m jarvis_voice home list
& $py -m jarvis_voice home status "Sony TV"
& $py -m jarvis_voice home do "Sony TV" set_volume 20  # asks y/n first for a confirm-tier command
```

## Logs

| What | Where |
| --- | --- |
| Voice helper | `%USERPROFILE%\.jarvis\logs\voice.log`, rotated at 1 MB with three old files kept. Follow it with `Get-Content "$env:USERPROFILE\.jarvis\logs\voice.log" -Tail 50 -Wait`. |
| Mod | Claude Code's debug log. Start with `claude --plugin-dir "$PWD\plugin" --debug-file "$env:TEMP\claude-debug.log"`. The mod's lines start with `jarvis:`, the helper's stderr appears as `jarvis: helper: ...`, and `/jarvis setup` logs uv's output as `jarvis: uv: ...`. |

Secrets (the Fish Audio key, the control token) are masked in the helper's log.

## Troubleshooting

### The microphone is blocked or silent

The helper reports `mic_blocked` when Windows denies microphone access. Open Settings > Privacy & security > Microphone (Win+R, `ms-settings:privacy-microphone`) and turn on **Microphone access**, **Let apps access your microphone** and **Let desktop apps access your microphone**. Then:

- Check the headset's own mute button and that its microphone boom is plugged in.
- `mic_blocked` with "pure digital silence" means Windows gets exact zeros from the device: it is muted before Windows (a mute button or light, a flipped-up or loose boom, or the headset's own software such as Logitech G HUB), not by Windows. The listener reports it once, after 30 seconds of nothing but exact zeros since it started. Some headsets (the Logitech PRO X with its noise gate, for one) send exact zeros whenever the user is quiet, so once any sound has arrived, zeros are never reported, and a push-to-talk clip of exact zeros is then ignored as silent.
- `aec_unavailable` means the echo canceller (livekit's `livekit_ffi.dll`, about 25 MB and not Authenticode-signed) could not be loaded ("could not be started": Windows Smart App Control can block it; `/jarvis setup` reinstalls it), or loaded and then stopped working mid-session ("stopped working": `/jarvis restart` tries again). Hands-free listening goes on without it, hearing the raw microphone, and `doctor` has an `aec` section that runs it on a synthetic echo. `JARVIS_AEC=off` (the `echoCancelling` setting) skips it.
- `wake_unavailable` means the "Hey Jarvis" model could not be downloaded or loaded (it comes from openWakeWord's GitHub releases into `%USERPROFILE%\.jarvis\models\wake`). Push-to-talk still works; `/jarvis setup`, or the next helper start, tries again. When it names plain "Jarvis", only `jarvis_v2.onnx` is missing (it comes from the fwartner/home-assistant-wakewords-collection repository on GitHub, pinned to one commit), and "Hey Jarvis" still works. The helper never waits for that model before "Hey Jarvis" is live: it loads it from disk if it is there, and otherwise fetches it only when plain "Jarvis" is switched on (giving up after 10 seconds with no data, or 60 seconds in all), reporting a failure once; `/jarvis setup` downloads it too.
- `mic_in_use` means another app holds the microphone in exclusive mode: close it, or untick **Allow applications to take exclusive control of this device** in the microphone's Properties > Advanced.
- The helper picks the Windows default input and output devices (WASAPI first). To pick another, set `JARVIS_INPUT_DEVICE` or `JARVIS_OUTPUT_DEVICE` in the `env` block of your user settings to part of the device name, such as `PRO X`, and restart Claude Code. `/jarvis devices` shows what is in use.

### Jarvis does not speak

| Error | Meaning |
| --- | --- |
| `fish_key_missing` | No key: set the plugin's **Fish Audio API key** option or `FISH_AUDIO_API_KEY` (see the README). |
| `fish_auth_failed` | Fish Audio rejected the key (HTTP 401/403: check it was copied whole and has not been revoked), or the key works but the account has no API credit (HTTP 402, doctor status `no_credit`: add credit at fish.audio, or use the free `s2.1-pro-free` model, the default). |
| `fish_unreachable` | No connection to `api.fish.audio`: check the network, a firewall, or a proxy. |
| `local_voice_failed` | The local voice (`/jarvis engine local`) is not installed, could not load its model, or stopped. The message says which; `/jarvis setup local` installs or repairs it, and the helper log has the local voice's own output. |

`/jarvis test` speaks a test line, and the doctor checks the key without speaking. The helper reads the key when it starts: after changing it, run `/jarvis restart`, and restart Claude Code if the key is in `settings.json` (Claude Code reads its `env` block at startup). Without a `voiceId`, Fish Audio's default voice is used. The model comes from `JARVIS_TTS_MODEL` (the mod sets it from the `fishModel` option; default `s2.1-pro-free`). Fish silently falls back to the paid `s2.1-pro` for a model name it doesn't know, which then fails with HTTP 402 when there is no API credit, so the option is a fixed list.

### CUDA is not used

`/jarvis` shows `speech model <name> on <device>`. On an NVIDIA machine it should say `cuda`. If it says `cpu`:

- Update the NVIDIA driver; the CUDA 12 libraries need a recent one.
- Run `/jarvis setup cuda` to install the CUDA libraries (setup adds them only when it finds an NVIDIA driver, and `/jarvis setup cpu` leaves them out).
- Run the doctor and look in `voice.log` for `CUDA not available` or a missing DLL such as `cublas64_12.dll` or `cudnn_ops64_9.dll`. The helper falls back to the CPU rather than failing.
- On the CPU, `auto` picks `small.en`; `large-v3-turbo` on a CPU is slow.

### The helper keeps restarting, or says it is active in another window

- `JARVIS · active in another window`: another Claude Code window (or a helper you started by hand) holds the lock. Close it, then run `/jarvis`.
- If the helper keeps exiting, the mod restarts it after 1, 2, 5 and 10 seconds, then stops and shows the error. The reason is in `voice.log`, and in the debug log as `jarvis: helper: ...` lines. `/jarvis restart` (or `/jarvis`) tries again.
- `JARVIS · not set up · run /jarvis setup`: the venv is missing; run `/jarvis setup`. If setup fails, the debug log has uv's output.

### uv is not found

`/jarvis setup` looks for uv in `%USERPROFILE%\.local\bin\uv.exe`, then `%LOCALAPPDATA%\Microsoft\WinGet\Links\uv.exe`, then on `PATH`. After `winget install astral-sh.uv`, open a new terminal so Claude Code sees the updated `PATH`.
