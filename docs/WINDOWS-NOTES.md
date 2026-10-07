# Windows build notes

These are the traps found while researching Windows and the Mac port, written down so they are designed in from phase 1. The main tab is the plan; this tab is the checklist I build against.

## Mod side (Claude Code plugin)

**Helper channel**

- `$.process.spawn` writes its `input` once and closes it; with no input, the child's stdin is closed from the start. So events go helper to mod as JSON lines on stdout, and commands go mod to helper as `$.http.fetch` calls to the helper's local server.
- The helper binds `127.0.0.1` only (a wider bind can raise a Windows Firewall prompt), checks a per-session token in constant time, rejects requests that carry an `Origin` header, and checks `Host`. Add `127.0.0.1` and `localhost` to `NO_PROXY` in case a proxy is configured.
- Other installed plugins can hook `process.spawn` and `http.fetch` and see what passes through them, including the token line and spawn environment. Keep the API key out of spawn arguments; the helper reads it from its inherited environment.
- Once 1,048,576 characters sit unread on the pipe, the child blocks on its next write. The helper writes stdout from its own writer thread, never from the audio callback.
- Run the helper with `PYTHONUNBUFFERED=1` (or flush every line) so barge-in events aren't held in a buffer, and `PYTHONUTF8=1` so Hebrew or other non-English transcripts don't fail on Windows' default code page.

**Lifecycle**

- A hot reload of the mod kills the helper and all mod timers. The mod restarts the helper; reminders that must survive belong in the helper, not in mod timers.
- `session.end` also fires on `/clear` and `/resume`, and no `session.start` follows. Never shut the helper down unconditionally there.
- On Windows the venv's `python.exe` is a small redirector that starts the real interpreter as a child, so killing it may leave the helper running. The helper exits on its own if the mod's heartbeat stops.
- Every terminal and desktop session loads the mod. A named mutex makes only one helper own the mic; other sessions show the HUD read-only.
- If the worker that runs mods crashes three times, Claude Code unloads every mod for the session, which also stops the helper.

**Guard and prompts**

- Tool matchers in a mod are exact: `'Bash|PowerShell'` matches nothing. Use `{ tool: ['Bash', 'PowerShell', 'Monitor'] }`. Monitor runs shell commands too.
- A hook that throws or overruns its 10-second budget is skipped, so a guard fails open unless its registration has a `.catch` that refuses. Waiting for your voice or click happens inside an engine call, not a plain promise.
- `$.tool.check` only reads the rules and mode; it asks no hooks or classifier. In auto mode an `ask` goes to the classifier, not to you, which is why Jarvis asks for itself.
- Spoken prompts are submitted with `asUser: true` so Claude treats them as your words. That also means a voice from a TV can speak for you, so the wake word and the confirmation tiers are the defence.
- Data folder: on Windows read `USERPROFILE` or `LOCALAPPDATA`, not `HOME`, which is usually unset outside Git Bash. Stay out of `AppData` in case the desktop app is installed as an MSIX package.

## Helper side (Python)

| Area | What to do | Why |
| --- | --- | --- |
| Frame sizes | Keep the microphone at 16 kHz mono internally and re-chunk per consumer: 10 ms for the echo canceller, 512 samples (32 ms) for Silero, 1,280 samples (80 ms) for openWakeWord. The speaker's audio goes to the echo canceller at the device's own rate, 10 ms at a time | Each library insists on its own size; a streaming resampler on the speaker's audio hands it over in bursts, behind its own echo |
| Echo cancelling | `livekit` `rtc.AudioProcessingModule`; feed it exactly the samples played, and call `set_stream_delay_ms` every frame while echo cancelling is on | The old `webrtc-audio-processing` package has no Windows wheel |
| Fish Audio format | Request 16-bit PCM at 16 or 32 kHz | Fish PCM comes only at 8, 16, 24, 32 or 44.1 kHz, and the canceller wants a rate it handles natively |
| openWakeWord | Pass `inference_framework='onnx'`, download models into the data folder with `target_directory`, list the models explicitly | Its default is TFLite, which has no Windows wheel; downloads otherwise land inside the Python install |
| Leaner wake word | Consider `pyopen-wakeword` or `pymicro-wakeword` | openWakeWord also pulls in scipy and scikit-learn |
| Silero VAD | Use `pysilero-vad` for streaming | faster-whisper's bundled ONNX file is a modified graph meant for its own batch use |
| faster-whisper on NVIDIA | Pin `ctranslate2` 4.6.3 or later; cuDNN is optional from that version | RTX 50-series needs its CUDA 12.8 build |
| Language | `.en` models for English only; multilingual `small`, `turbo` or `large-v3` for Hebrew or mixed speech | The `.en` models and the wake models are English-only |
| Device changes | Re-open streams when the default device changes or a device is unplugged | WASAPI streams don't follow the Windows default by themselves |
| Audio threads | Initialise COM (`CoInitializeEx`, multithreaded) on every thread before it opens, starts or restarts a stream | PortAudio's WASAPI start needs COM on the calling thread; without it the start fails with "Unanticipated host error" and stale WDM-KS text |
| Mic errors | Tell a privacy block (three Windows toggles: Microphone access, apps, desktop apps) apart from another app holding the device exclusively | Each needs a different fix from you |
| Ducking | Avoid Windows' communications audio category | Windows lowers other sounds by 80% while a communications stream is open |

## Desktop actions and shell rules on Windows

| Action | Windows | Mac later |
| --- | --- | --- |
| Launch an app | Start menu app list and AUMIDs for Store apps, `os.startfile` | `open -a` |
| Focus a window | UI Automation through pywinauto; Windows may refuse to bring a window forward from a background process | AppleScript or pyobjc; needs Accessibility permission |
| Volume and mute | pycaw `EndpointVolume`: `GetMute`, `SetMute(1, None)`, `SetMasterVolumeLevelScalar` | AppleScript `set volume` |
| Media keys, now playing | Virtual media keys; Windows media session API | Media keys; now-playing needs a helper library |
| Lock | `LockWorkStation` | Lock via the system's screen-lock shortcut |
| Screenshot | mss | mss; needs Screen Recording permission |
| Clipboard | pyperclip | pyperclip |
| Notifications | winotify or BurntToast | `osascript` display notification |

Shell rule notes:

- `shutdown /s /t 60` implies `/f`: after the countdown Windows force-closes apps without saving.
- Windows ships PowerShell 5.1, where `&&` and `||` don't exist; the persona's Windows paragraph tells Claude to chain with `;` or `if`.
- Add Windows download-and-run tools to the Ask or Never tiers: `certutil`, `bitsadmin` and `Start-BitsTransfer`, `mshta`, `rundll32`, `regsvr32`, `wscript` and `cscript`, `msiexec`, `Add-Type`, and `iwr ... | iex`.
- A trailing `  * ` in a rule also matches the bare command, so denying `PowerShell(net user *)` blocks read-only listing too.
- Microsoft says UAC is not a security boundary, and at the default level built-in admin tools can elevate silently. Real human gating needs a standard account, "Always notify", or Windows 11 Administrator protection.
- A Jarvis-written managed settings file applies to every user on the PC, is ignored where an organization already manages Claude Code, and a malformed one stops Claude Code from starting. Hardened mode validates the file before installing it.

## Packaging and install on Windows

- **uv, not an .exe, for version 1.** uv prefers a system Python when one fits, else downloads its own. Pin Python 3.12 and commit `uv.lock`.
- **Find uv by absolute path.** After `winget install`, the running Claude Code keeps its old PATH, so a bare `uv` fails until restart. Check `%USERPROFILE%\.local\bin\uv.exe`, the WinGet links folder, then Jarvis's private copy.
- **No `irm ... | iex` installer.** Launched from a background process it looks like malware to antivirus and can be blocked by policy. Download uv's release zip directly and verify it.
- **Build backend** `uv_build` or `hatchling`, not setuptools, which writes `build/` and `.egg-info` into the plugin folder and sets off hot-reload storms.
- **Short paths.** Hugging Face cache paths plus a deep venv can pass Windows' 260-character limit; keep the data folder short and set `UV_CACHE_DIR` on the same drive so uv can hard-link.
- **No PyInstaller one-file exe.** Unsigned ones are often flagged by Defender and SmartScreen. If Jarvis is ever shared, sign the helper: Microsoft's Artifact Signing costs about $9.99 a month, and individual sign-up is limited to the USA and Canada.
- **Plugin settings.** A hidden (sensitive) `userConfig` field needs both a `title` and a `description`, or validation fails.
- **Windows on ARM is out for version 1**: `ctranslate2`, `livekit` and `soxr` publish no Windows ARM wheels.

## Notes for the Mac port

- **Target macOS 14 or later on Apple Silicon.** Recent onnxruntime needs macOS 14 on arm64, and Intel Macs are not planned.
- **Speech to text: pywhispercpp (whisper.cpp)**, not mlx-whisper, which requires PyTorch, numba and scipy.
- **Mic permission belongs to the launching app.** macOS asks on behalf of the terminal or the Claude app, not Python. If that app doesn't declare a microphone usage description, a child's mic is silently denied; test this first.
- **Apple's voice processing** (an alternative echo canceller) ducks other audio by default; macOS 14 added a setting to control it.
- **A signed helper app** (`JarvisVoice.app`) later would get its own mic permission, but then needs code signing and notarization.
- **Accessibility, Screen Recording and Automation prompts** appear for window focus, screenshots and AppleScript control; the first-run check walks through them.

## To test on your PC

These could not be confirmed from documentation and are checked in phase 1 or when their phase starts.

- [ ] The helper spawns from a desktop app Code-tab session; the API docs call process spawning "CLI only".
- [ ] No console window flashes when the helper starts, and stdout pipes still work if `pythonw.exe` is needed.
- [ ] `$.http.fetch` to `127.0.0.1` isn't routed through a proxy or blocked by policy, and binding `127.0.0.1` raises no firewall prompt.
- [ ] Killing the venv redirector also stops the real interpreter; otherwise the heartbeat watchdog covers it.
- [ ] The PowerShell tool reaches the mod as `PowerShell` with a `command` field (this build's type files were generated on Linux).
- [ ] Speech-to-text latency on your CPU or GPU; the estimates of about 0.5 to 1.5 s for `small.en` on an 8-core CPU need measuring.
- [ ] Echo cancelling quality with your speakers at your usual volume.
- [ ] Smart App Control, if on, allows uv's Python and the unsigned extension modules in the wheels.
- [ ] Model loading works from your profile path, including non-English characters or long paths.
- [ ] Where Claude Code stores a hidden plugin setting on Windows, and whether that's acceptable for the Fish key.
- [ ] Whether Claude Code's own permission prompts fire a hook in the desktop app, so they could be answered by voice.
- [ ] Total model download size, and the license of the "Hey Jarvis" model files.

## Sources

Claude Code: [mods overview](https://code.claude.com/docs/en/plugins/mods/overview), [mods reference](https://code.claude.com/docs/en/plugins/mods/reference), [mods API](https://code.claude.com/docs/en/plugins/mods/api), [mods admin](https://code.claude.com/docs/en/plugins/mods/admin), [plugins reference](https://code.claude.com/docs/en/plugins-reference), [desktop](https://code.claude.com/docs/en/desktop), [setup](https://code.claude.com/docs/en/setup), [permissions](https://code.claude.com/docs/en/permissions), [permission modes](https://code.claude.com/docs/en/permission-modes), [tools reference](https://code.claude.com/docs/en/tools-reference), [computer use](https://code.claude.com/docs/en/computer-use), and the mod API type files bundled with Claude Code 2.1.291.

Voice: [Fish Audio live TTS](https://docs.fish.audio/api-reference/endpoint/websocket/tts-live.md), [Fish Audio TTS API](https://docs.fish.audio/api-reference/endpoint/openapi-v1/text-to-speech), [Fish Audio speech to text](https://docs.fish.audio/api-reference/endpoint/openapi-v1/speech-to-text), [openWakeWord](https://github.com/dscripka/openWakeWord), [Picovoice FAQ](https://picovoice.ai/docs/faq/general/), [Porcupine free tier shutdown thread](https://community.home-assistant.io/t/porcupine-free-tier-shutdown-alternatives-for-home-assistant-voice-users/1012382), [Silero VAD](https://raw.githubusercontent.com/snakers4/silero-vad/master/README.md), [pysilero-vad](https://pypi.org/pypi/pysilero-vad/json), [livekit APM](https://raw.githubusercontent.com/livekit/python-sdks/main/livekit-rtc/livekit/rtc/apm.py), [faster-whisper](https://github.com/SYSTRAN/faster-whisper), [CTranslate2 changelog](https://raw.githubusercontent.com/OpenNMT/CTranslate2/master/CHANGELOG.md), [pywhispercpp](https://raw.githubusercontent.com/absadiki/pywhispercpp/main/README.md), [sounddevice WASAPI settings](https://python-sounddevice.readthedocs.io/en/latest/api/platform-specific-settings.html).

Windows: [UAC how it works](https://learn.microsoft.com/en-us/windows/security/application-security/application-control/user-account-control/how-it-works), [Administrator protection](https://learn.microsoft.com/en-us/windows/security/application-security/application-control/administrator-protection/), [Microsoft security servicing criteria](https://www.microsoft.com/en-us/msrc/windows-security-servicing-criteria), [shutdown command](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/shutdown), [SetForegroundWindow](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setforegroundwindow), [LockWorkStation](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-lockworkstation), [stream attenuation](https://learn.microsoft.com/en-us/windows/win32/coreaudio/stream-attenuation), [microphone privacy](https://support.microsoft.com/windows/privacy/windows-camera-microphone-and-privacy), [per-app mic consent preview](https://windowslatest.com/2026/08/19/windows-11-is-getting-a-major-privacy-upgrade-blocks-apps-spying-on-your-camera-and-mic-with-new-controls), [MSIX behind the scenes](https://learn.microsoft.com/en-us/windows/msix/desktop/desktop-to-uwp-behind-the-scenes), [code signing options](https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/code-signing-options), [pycaw](https://github.com/AndreMiras/pycaw), [pywinauto](https://pywinauto.readthedocs.io/en/latest/), [mss](https://python-mss.readthedocs.io/), [Windows Terminal 1.21](https://devblogs.microsoft.com/commandline/windows-terminal-preview-1-21-release/), [uv Python versions](https://docs.astral.sh/uv/concepts/python-versions/), [Python on Windows](https://docs.python.org/3/using/windows.html), [PEP 686](https://peps.python.org/pep-0686/).

Mac: [responsible process for permissions](https://www.qt.io/blog/the-curious-case-of-the-responsible-process), [media capture authorization](https://developer.apple.com/documentation/bundleresources/requesting-authorization-for-media-capture-on-macos.md), [notarization](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution.md), [voice processing](<https://developer.apple.com/documentation/avfaudio/avaudioionode/setvoiceprocessingenabled(_:).md>).
