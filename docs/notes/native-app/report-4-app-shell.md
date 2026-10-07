# Report 4: native app shell options

Raw research for [Jarvis as a native app](../2026-10-07-native-app.md), 2026-10-07. Unchecked; the note is the checked version.

**Recommendation: Electron**, with Tauri 2 as a close second.

Both are Chromium-based on Windows: Electron bundles Chromium, and Tauri uses WebView2, which Windows 11 includes. So the HUD would draw and perform about the same in either. Electron is ahead on three things Jarvis needs:

1. **Node is built in.** The TypeScript Agent SDK can run in Electron's main process. The mod's existing TS code (router, sentences, protocol, helper supervisor, `hud-svg.ts`) would mostly move over with light porting.
2. **Selective click-through.** Electron can forward mouse moves while ignoring clicks, so a transparent overlay can pass clicks through everywhere except the orb. Tauri can only switch click-through on or off for the whole window.
3. **One web engine on Windows and Mac.** Tauri uses WebKit (WKWebView) on macOS, so the HUD would have to be tested on a second engine.

Tauri's main advantage, size, matters little here. The Claude Code binary that the Agent SDK bundles is about 256 MB unpacked for Windows x64, and the Python voice dependencies add more. Either one is much larger than the gap between the two shells.

## Facts that drive the choice
- **Agent SDK runtime:** needs Node.js 18+ or Python 3.10+, and both SDKs bundle a native Claude Code binary [S1]. The current npm release is @anthropic-ai/claude-agent-sdk 0.3.293, with `engines: node >=18.0.0` (npm registry, read 2026-10-07). Its `@anthropic-ai/claude-agent-sdk-win32-x64` binary package is about 256 MB unpacked. The Python package claude-agent-sdk 0.2.164 has a 109.9 MB Windows wheel, needs Python 3.10+, and its README says "Claude Code CLI is automatically bundled" (PyPI, 2026-10-06).
- **SDK features Jarvis uses:** `includePartialMessages` for sentence streaming, `interrupt()` for barge-in, `setModel()` for model routing, `canUseTool` for the guard, and `pathToClaudeCodeExecutable` to point at Rotem's installed `claude` instead of shipping 256 MB (sdk.d.ts:1590, 1863, 1980, 2861, 2897 in 0.3.293). The Python SDK has partial messages and `ClaudeSDKClient.interrupt()` [S2].
- **Electron 44.6.0** (2026-10-05) bundles Node 24.21.0 and Chromium 152 [S3]. It supports Windows 10+ and macOS Ventura+ (electron@44.6.0 README:35-40). A new major ships every 8 weeks and only the latest 3 are supported [S4].
- **Tauri 2.12.1** (crate released 2026-10-01) has no Node. A TypeScript SDK on Tauri needs Node packaged as a sidecar (pkg or similar) [S5]. If you compile it with `bun build --compile`, the SDK "cannot resolve the bundled CLI binary" without the `extractFromBunfs()` workaround [S6].
  - Alternative (inferred): run the Python Agent SDK inside the existing `jarvis-voice` process. Its `>=3.11,<3.13` Python range fits. That means rewriting the mod's TypeScript logic in Python or Rust.

## Comparison

| | Electron 44 | Tauri 2.12 | WinUI 3 / WPF | Flutter |
|---|---|---|---|---|
| Agent SDK | TS SDK runs in the main process | Node sidecar, or the Python SDK in the helper | Sidecar only | Sidecar only |
| Transparent overlay + orb | `setIgnoreMouseEvents(true,{forward:true})`, forwarding works on Windows and macOS (electron.d.ts:3392, 22298-22306; [S7]) | `setIgnoreCursorEvents(ignore: boolean)` only (window.d.ts:1425). Workaround (inferred): poll `cursorPosition()` (window.d.ts:2354) | Windows App SDK runs only on Windows 10 1809+ [S8], so no Mac | No public multi-window API in stable 3.44.8 (Aug 2026) [S9] |
| Tray, login start, hotkey | Built in. `globalShortcut` fires on press only, no release (electron.d.ts:8649) | Tray needs the `tray-icon` feature [S10]. Autostart plugin [S11]. Global-shortcut plugin reports `Pressed`/`Released`, so it supports push-to-talk [S12] | | |
| Updates | electron-updater with NSIS installer, publishes to GitHub Releases [S13] | Updater plugin, update signature mandatory, static JSON on GitHub Releases [S14] | | |
| Size and memory | About 85 MB installer, about 120 MB idle RAM (one developer's Windows 11 test) [S15] | About 2.5 MB installer, about 80 MB idle [S15]. WebView2 is part of Windows 11 [S16] | | |

**Others:**
- Wails v3 is still beta (status page 2026-10-01) [S17].
- Electrobun (Bun runtime, WebView2 on Windows, about 14 MB) [S18] is worth watching, since the SDK accepts `executable: 'bun'` (sdk.d.ts:1668). Its Windows maturity is unproven.

## HUD at 60 fps
- On Windows both shells use the same Chromium-family engine, so performance should match (inferred). Tauri uses WebKit on macOS, so it needs a second round of testing there [S19].
- **Drawing method:** today's `hud-svg.ts` animates with SMIL and targets about 30 fps. For 60 fps I'd use Canvas 2D, or WebGL for glow:
  - Pre-render static layers to an offscreen canvas, use layered canvases, and avoid `shadowBlur` [S20].
  - Drive motion from the `requestAnimationFrame` timestamp. Callbacks follow the display rate (60, 120 or 144 Hz) and pause when hidden [S21].
  - Animate only `transform` and `opacity` for anything done in DOM or CSS [S22].
- **GPU cost (inferred):** a full-screen per-pixel-alpha window gets composited by Windows every frame. Size the overlay to the HUD's bounds, drop to about 30 fps when idle, and keep the full-screen mode opaque.
- **Tauri on Windows:** set `shadow:false`, because `true` adds a 1 px white border to undecorated windows (Tauri config schema). WebView2 supports only alpha 0 or 255 for its background colour [S23].

## Python helper and uv in one installer
- Ship `uv.exe` as an extra resource (uv 0.12.23, Windows wheel 18.1 MB on PyPI). On first run, the app runs `uv sync --frozen` with these variables pointing into `%LOCALAPPDATA%\Jarvis`: `UV_PYTHON_INSTALL_DIR`, `UV_CACHE_DIR` and `UV_PROJECT_ENVIRONMENT`. Add `UV_MANAGED_PYTHON=1` so it never touches system Python [S24, S25].
- uv downloads managed CPython automatically [S25]. Alternative: run the official install script with `UV_UNMANAGED_INSTALL` [S26].
- Do the first sync in-app with progress shown in the HUD rather than in the installer (inferred). It downloads hundreds of MB, or GBs with the CUDA extra (inferred).
- Electron spawns the helper with `child_process`. The stdout-JSON plus 127.0.0.1-HTTP protocol stays as it is.
- The SDK's native binary must sit outside the asar archive (`asarUnpack`), because only `execFile` works on files inside asar [S27].
- Tauri equivalents: NSIS installer hooks, and per-user install needs no admin [S28].

## Signing, SmartScreen, updates
- **Rotem's own builds:** files built locally have no Mark-of-the-Web, so SmartScreen does not check them [S29].
- **Unsigned downloads** show "Windows protected your PC" with "Run anyway", and reputation starts from zero for each new version [S30].
- **EV certificates no longer bypass SmartScreen** (Microsoft, 2026-05-06) [S30]. electron-builder's docs still claim EV gives "immediate trust"; that is outdated [S31].
- **Azure Artifact Signing** costs about $9.99/month, but individuals can use it only in the USA and Canada [S32].
- **Microsoft Store:** registration has been free for individuals since 2025-09-10 [S33]. MSIX packages get free signing and no SmartScreen warnings. Win32 EXE/MSI installers must still be signed by the developer [S32].

## macOS later
- $99/year Apple Developer account for Developer ID signing and notarization [S34].
- `NSMicrophoneUsageDescription` in Info.plist and `askForMediaAccess` [S35], plus the `com.apple.security.device.audio-input` entitlement [S36]. Inferred: macOS charges the Python child's mic access to the parent app.
- Auto-update requires a signed app [S37, S13]. Login items need signing and notarization to work reliably [S38].
- Risk (inferred): a Python environment downloaded by uv at runtime sits outside the notarized bundle.

## Main risks
1. **Claude login.** Anthropic doesn't let developers build claude.ai login into their own apps or send requests through Free, Pro or Max credentials on users' behalf, and its SDK docs tell developers to use an API key unless Anthropic has approved otherwise. It explicitly allows a user signing in to the unmodified Claude Code binary with their own subscription [S39, S1]. Rotem's personal use reads as allowed (inferred), but this needs confirming before sharing the app.
2. **Bundle size.** The Claude binary (about 256 MB) plus the ML wheels dominate. Using `pathToClaudeCodeExecutable` avoids the binary but risks the SDK and CLI versions drifting apart (inferred).
3. **Electron upkeep.** Majors every 8 weeks, only 3 supported [S4]. You also have to keep Electron's security settings right yourself (context isolation, a restricted preload).
4. **Push-to-talk.** Electron's hotkey has no release event, so hold-to-talk stays in the helper's `pynput`.
5. **Signing for others.** Signing is cheap only in the USA and Canada; otherwise the options are an OV certificate or the Store.

## Sources
- S1 https://code.claude.com/docs/en/agent-sdk/quickstart ; https://code.claude.com/docs/en/agent-sdk/overview
- S2 https://code.claude.com/docs/en/agent-sdk/python
- S3 https://releases.electronjs.org/release/v44.6.0
- S4 https://www.electronjs.org/docs/latest/tutorial/electron-timelines
- S5 https://v2.tauri.app/learn/sidecar-nodejs/ ; https://v2.tauri.app/develop/sidecar/
- S6 https://code.claude.com/docs/en/agent-sdk/typescript
- S7 https://www.electronjs.org/docs/latest/tutorial/custom-window-interactions
- S8 https://learn.microsoft.com/en-us/windows/apps/windows-app-sdk/
- S9 https://startdebugging.net/2026/08/how-to-enable-multi-window-support-in-a-flutter-desktop-app/
- S10 https://v2.tauri.app/learn/system-tray/
- S11 https://v2.tauri.app/plugin/autostart/
- S12 https://v2.tauri.app/plugin/global-shortcut/ ; @tauri-apps/plugin-global-shortcut 2.4.0 index.d.ts:10
- S13 https://www.electron.build/auto-update.html
- S14 https://v2.tauri.app/plugin/updater/
- S15 https://www.digitalapplied.com/blog/desktop-apps-web-stack-tauri-electron-deno-wails-2026
- S16 https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/distribution
- S17 https://v3.wails.io/status/
- S18 https://betterstack.com/community/guides/scaling-nodejs/electrobun-desktop-apps-typescript/
- S19 https://hackernoon.com/six-months-with-tauri-the-benefits-and-the-bill
- S20 https://developer.mozilla.org/en-US/docs/Web/API/Canvas_API/Tutorial/Optimizing_canvas
- S21 https://developer.mozilla.org/en-US/docs/Web/API/Window/requestAnimationFrame
- S22 https://web.dev/articles/stick-to-compositor-only-properties-and-manage-layer-count
- S23 https://learn.microsoft.com/en-us/microsoft-edge/webview2/reference/win32/icorewebview2controller2
- S24 https://docs.astral.sh/uv/reference/environment/
- S25 https://docs.astral.sh/uv/concepts/python-versions/
- S26 https://docs.astral.sh/uv/reference/installer/
- S27 https://www.electronjs.org/docs/latest/tutorial/asar-archives
- S28 https://v2.tauri.app/distribute/windows-installer/
- S29 https://textslashplain.com/2023/08/23/smartscreen-application-reputation-in-pictures
- S30 https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/smartscreen-reputation
- S31 https://www.electron.build/code-signing-win.html
- S32 https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/code-signing-options
- S33 https://blogs.windows.com/windowsdeveloper/2025/09/10/free-developer-registration-for-individual-developers-on-microsoft-store/
- S34 https://v2.tauri.app/distribute/sign/macos/
- S35 https://www.electronjs.org/docs/latest/api/system-preferences
- S36 https://developer.apple.com/documentation/bundleresources/entitlements/com.apple.security.device.audio-input
- S37 https://www.electronjs.org/docs/latest/api/auto-updater
- S38 https://www.electronjs.org/docs/latest/api/app
- S39 https://code.claude.com/docs/en/legal-and-compliance
- Repo context: `plugin/voice/pyproject.toml`, `plugin/hooks/hud-svg.ts` and `docs/WINDOWS-NOTES.md` on origin/main of rotembab/jarvis-claude-mod.
- Package files I downloaded and read are in `/tmp/claude-0/-home-claude-jarvis-claude-mod/198c950a-9122-5877-b218-52d05752d037/scratchpad/` (sdk/, el/, tapi/, tgs/, tauricli/).
