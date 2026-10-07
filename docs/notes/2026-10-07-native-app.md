# Jarvis as a native app

Design note for Rotem, written 2026-10-07, answering "is it possible that the whole jarvis will be a native app that uses claude code?". Research only: no code was changed. Two independent reviewers checked its claims against Anthropic's docs, the plugin API types and this repo, and their corrections are folded in.

**How to read the sources.** Short names used below:

| Name | What it is |
|---|---|
| [SDK overview](https://code.claude.com/docs/en/agent-sdk/overview), [SDK quickstart](https://code.claude.com/docs/en/agent-sdk/quickstart), [Headless](https://code.claude.com/docs/en/headless), [SDK plugins](https://code.claude.com/docs/en/agent-sdk/plugins), [TS reference](https://code.claude.com/docs/en/agent-sdk/typescript) | Anthropic's Claude Code docs |
| [Legal](https://code.claude.com/docs/en/legal-and-compliance) | Claude Code "Legal and compliance" page, read 2026-10-07. Policy pages change, so recheck before relying on it. |
| `sdk.d.ts:N` | Type file of `@anthropic-ai/claude-agent-sdk` 0.3.293 (released 2026-10-07, bundles Claude Code 2.1.293) |
| `claude-code.d.ts:N`, `reference.md:N` | Plugin API files from Claude Code 2.1.292. The API is marked "EARLY ACCESS: this surface may change between releases without notice" (claude-code.d.ts:4). |
| Repo paths | `rotembab/jarvis-claude-mod` at `origin/main` (97e6bf4) |
| Report 1 to 4 | The four research reports this note is built from, kept next to it: [1 SDK and login](native-app/report-1-sdk-and-login.md), [2 plugin surfaces](native-app/report-2-plugin-surfaces.md), [3 code reuse](native-app/report-3-code-reuse.md), [4 app shell](native-app/report-4-app-shell.md). They are raw research; where one disagrees with this note, the note is the checked version. |

Anything marked **(inferred)** is my reasoning, not something a source says.

---

## 1. Short answer

Yes, with one caveat about login. Jarvis can become a real Windows app (full-screen or floating HUD, tray icon, global hotkey, starts with Windows) that still uses Claude Code as its brain. The cleanest way is a companion app that sits next to the Claude Code you already run, because Claude Code itself stays unchanged and your subscription login stays exactly as it is today ([Legal](https://code.claude.com/docs/en/legal-and-compliance)). A fully standalone app that runs Claude Code inside itself through the Agent SDK is also technically possible. For an app only you use, Anthropic's terms read as allowing your own subscription, but the SDK docs steer app builders to API keys, so that is worth confirming with Anthropic before relying on it ([Legal](https://code.claude.com/docs/en/legal-and-compliance), [SDK overview](https://code.claude.com/docs/en/agent-sdk/overview)). Sharing the app with others clearly needs API keys.

---

## 2. The options

### At a glance

| | A. Companion app | B. App hosts Claude Code (Agent SDK) | C1. App hosts Claude Code and loads today's plugin | C2. App attaches as a Claude Code screen |
|---|---|---|---|---|
| Where Claude Code runs | Windows Terminal or the Claude desktop app, as today | Inside the app | Inside the app | Windows Terminal or desktop app |
| Login | Your subscription, unchanged | Your subscription for personal use reads as allowed but is unconfirmed; API key is the safe choice | Same as B | Your subscription |
| Built on documented APIs | Yes | Yes | Partly | No |
| Relative size | Medium | Large | Medium, plus open questions | Not feasible today |

There is also a baseline you already have: Jarvis already draws its ring inside the Claude desktop app's Code tab (covered at the end of this section).

### A. Companion app, Claude Code and the plugin stay as they are

**How it works**

- Claude Code keeps running where it runs today, with the Jarvis plugin loaded. The plugin keeps doing model routing per request in `turn.step` (`plugin/hooks/router.ts:167-196`), barge-in with `$.turn.abort` (`plugin/hooks/voice.ts:276-284` through the engine port, `plugin/hooks/register.tsx:78`) and sentence streaming (`plugin/hooks/voice.ts:362-386`).
- A new Jarvis app owns the windows: a full-screen or floating HUD, a tray icon, a hotkey, start at login, and a settings page.
- The app and the plugin talk over 127.0.0.1. A plugin has no way to open a listening port (Report 2 found none in `claude-code.d.ts`; inferred). But it can call out over HTTP with `$.http.fetch` (claude-code.d.ts:3441-3460), which it already does to reach the helper (`plugin/hooks/helper.ts:197-199`). It can also spawn a process and read its output as it arrives (`$.process.spawn`, claude-code.d.ts:3488-3525; the pieces are not split into lines, Jarvis does that itself at `plugin/hooks/helper.ts:82`), which is how it hears the helper today.
- This can be built in two steps:
  - **A1, app as a display only.** The plugin keeps owning the voice helper and pushes HUD state (ring mode, mic and voice levels, tool actions, reply text) to the app. This is the smallest change.
  - **A2, app owns the voice helper.** The app starts `jarvis-voice` at login, sends its heartbeats and reads its events. The plugin connects to the app through a small relay that it spawns where it spawns the helper today. The helper protocol is versioned, checked on both sides and contains nothing Claude-specific (`plugin/protocol/schema.json:21-180`, `plugin/voice/src/jarvis_voice/events.py:58-67`, `plugin/voice/src/jarvis_voice/control.py:122-134`). So the relay can speak that same protocol, and the plugin's helper and voice code would barely change (inferred). Typed prompts from the app would reach Claude Code along the path speech already uses: app, relay, plugin, `$.prompt.submit` (claude-code.d.ts:2911-2912; Report 2).

**What you get**

- A full-screen or floating HUD on any monitor, always on top, with clicks passing through everywhere except the orb (with Electron, see section 4).
- Tray icon, hotkey and settings page instead of `/jarvis` subcommands.
- With A2, Jarvis listens from the moment Windows starts.
- Everything you have today keeps working: per-request model routing, the persona, typed and spoken turns in the same Claude Code session, the `/jarvis` commands, and the 132 plugin tests (Report 3, section 5).

**What it costs**

- Claude Code has to be open for Jarvis to answer. If it is closed, the app could open Windows Terminal running `claude` for you (inferred). That is still the normal Claude Code with your normal login.
- A new link between the app and the plugin, and a small change in the plugin.
- Only one helper can run per Windows logon. It holds a named mutex (`Local\\JarvisVoice` by default) and a second copy exits with code 3, so the app and the plugin must agree who owns the mic (`plugin/voice/src/jarvis_voice/cli.py:26,67,346-355`, `plugin/voice/src/jarvis_voice/platform/windows.py:25-26,69-71`).
- The helper refuses any request that carries an `Origin` header (`plugin/voice/src/jarvis_voice/control.py:79-80,98`). So the app's background process, not its web page, must make those calls.

**Login and billing.** Unchanged. Claude Code runs as Anthropic publishes it and you sign in with your own subscription. The policy says its rules do not "prevent an end user from signing in to the unmodified Claude Code binary with their own Claude subscription" ([Legal](https://code.claude.com/docs/en/legal-and-compliance)). The app never touches Claude credentials. Your usual Pro or Max limits apply, which "assume ordinary, individual usage of Claude Code and the Agent SDK" ([Legal](https://code.claude.com/docs/en/legal-and-compliance)).

**Unknowns**

- How fast and light the app-to-plugin link is (Report 2).
- What happens when two Claude Code sessions both load the plugin. The app would need a rule for which one it talks to (inferred).
- How to hand the app and the plugin a shared secret safely. Today the helper gets its token through an environment variable (`plugin/voice/src/jarvis_voice/cli.py:325`).

### B. A native app that hosts Claude Code itself (Agent SDK)

**How it works**

- The app's main process runs the TypeScript Agent SDK. The SDK is "a library that runs the Claude Code binary" ([SDK overview](https://code.claude.com/docs/en/agent-sdk/overview)), and both the TypeScript and Python SDKs bundle that binary ([SDK quickstart](https://code.claude.com/docs/en/agent-sdk/quickstart)).
- The app keeps one long-lived session open in streaming input mode (sdk.d.ts:3247-3250), starts the voice helper itself, and draws the HUD in its own window.
- Today's plugin mechanisms map onto SDK features like this (Report 3, section 4):

| Today, in the plugin | In the app, with the SDK |
|---|---|
| Voice prompt through `$.prompt.submit` | Push a user message into the input stream (sdk.d.ts:6282-6294) |
| Persona added in `prompt.compose` (`plugin/hooks/voice.ts:413-423`) | `systemPrompt` with the `claude_code` preset and `append` (sdk.d.ts:2404-2413) |
| Sentence streaming from `turn.step` | `includePartialMessages: true`, feeding text pieces to today's sentence splitter (sdk.d.ts:1859-1863, 5509-5517) |
| Barge-in through `$.turn.abort` | `Query.interrupt()`; these controls are "only supported when streaming input/output is used" (sdk.d.ts:2849-2861) |
| Model routing by rewriting each request in `turn.step` | `Query.setModel()` before sending the message: "Change the model used for subsequent responses" (sdk.d.ts:2891-2897). There is no per-request hook (sdk.d.ts:959). |
| Haiku judge through `$.model.complete` | A second short query or a direct API call (inferred; adds delay) |
| HUD action log from `tool.call` | `PreToolUse` and `PostToolUse` hooks (sdk.d.ts:2639-2661) |
| Permission dialog drawn by Claude Code | The app draws it, through `canUseTool` (sdk.d.ts:1590) |
| Hands and home tools through `$.tool.register` | In-process tools with `createSdkMcpServer` and `tool` (sdk.d.ts:615, 9784) |

- Your CLAUDE.md files, skills, commands and settings can still load through the `settingSources` option ([Claude Code features in the SDK](https://code.claude.com/docs/en/agent-sdk/claude-code-features)).
- A lighter variant drives the command line instead: `claude -p --input-format stream-json --output-format stream-json` ([Headless](https://code.claude.com/docs/en/headless)). Report 1 found no documented way to stop a turn there except signalling or closing the process, which is a poor fit for barge-in.

**What you get**

- One standalone app with no terminal needed.
- The app controls everything on screen: HUD, transcript, permission prompts and settings.

**What it costs**

- The biggest rewrite (see section 3): about 3.1k lines of TypeScript ported and about 0.5k replaced (Report 3).
- New screens that Claude Code gives you for free today: a transcript, typed input and permission dialogs (inferred: large).
- The bundled Claude Code binary is about 256 MB unpacked on Windows (Report 4). The app can instead point at your installed `claude` with `pathToClaudeCodeExecutable` (sdk.d.ts:1980), at the risk of the SDK and Claude Code versions drifting apart (Report 4, inferred).

**Login and billing: this is the catch**

- The SDK docs say: "Unless previously approved, Anthropic does not allow third party developers to offer claude.ai login or rate limits for their products, including agents built on the Claude Agent SDK. Use the API key authentication methods described in this document instead." ([SDK quickstart](https://code.claude.com/docs/en/agent-sdk/quickstart); the same note is on the [SDK overview](https://code.claude.com/docs/en/agent-sdk/overview)).
- The legal page says developers "including those using the Agent SDK, should use API key authentication", and "Anthropic does not permit third-party developers to offer Claude.ai login into their own applications, or to route requests through Free, Pro, or Max plan credentials on behalf of their users." It adds that developers "may not collect, store, or intermediate Claude.ai credentials or session tokens" ([Legal](https://code.claude.com/docs/en/legal-and-compliance)).
- Technically, an SDK session can run on a claude.ai login. The SDK's credential-source type has a value for "no API key in use - e.g. claude.ai OAuth login" (sdk.d.ts:129-131). So the limit is policy, not technology.
- The same legal page also says the rules do not prevent "an end user from signing in to the unmodified Claude Code binary with their own Claude subscription", and that Pro and Max limits "assume ordinary, individual usage of Claude Code and the Agent SDK" ([Legal](https://code.claude.com/docs/en/legal-and-compliance)). A personal app that only you use, where you sign in through Anthropic's own flow and the app never handles your credentials, reads as close to allowed (inferred). The docs do not say so outright.
- **Safe choices:** use an API key (pay per use, billed separately from your subscription), or ask Anthropic first. The legal page points to sales for "questions about permitted authentication methods" ([Legal](https://code.claude.com/docs/en/legal-and-compliance)).
- **If you ever share the app,** each person must bring their own key or credentials, and the app must not offer claude.ai login itself ([Legal](https://code.claude.com/docs/en/legal-and-compliance)).

**Unknowns**

- How interrupt behaves when messages are queued. The interrupt reply lists queued messages that "WILL still run unless cancelled first" (sdk.d.ts:2853-2860).
- `setModel()` called mid-turn takes effect after the current response ([TS reference](https://code.claude.com/docs/en/agent-sdk/typescript), per Report 3), so the plugin's mid-turn switch for very long contexts (`plugin/hooks/router.ts:170-175`) may behave differently (inferred).
- How much delay the Haiku judge adds without `$.model.complete` (inferred).
- Packaging the SDK in Electron: if the SDK starts the Claude Code binary with `spawn`, the binary must be unpacked from Electron's asar archive, because `spawn` cannot run files inside it (`execFile` can) ([Electron asar docs](https://www.electronjs.org/docs/latest/tutorial/asar-archives)). Untested.

### C. Hybrids

**C1. The app hosts Claude Code through the SDK and loads today's plugin.**

- The SDK can load a plugin folder with `plugins: [{ type: 'local', path }]` (sdk.d.ts:2036; [SDK plugins](https://code.claude.com/docs/en/agent-sdk/plugins)). The `CLAUDE_CODE_PLUGIN_DIRS` variable does the same "where no flag can be given (a session the desktop app or an SDK host starts)" (reference.md:68).
- The hoped-for benefit is that per-request routing, the persona, sentence streaming and barge-in stay in the plugin unchanged. That is a hypothesis to test (spike 5), not documented: they depend on `turn.step`, `turn.start` and `turn.complete`, `classic.UserPromptSubmit`, `tool.call` and `$.turn.abort` (`plugin/hooks/register.tsx:78,99-145`), and the API types say nothing either way about those in SDK sessions (claude-code.d.ts:4412-4434). What is documented: SDK sessions raise `prompt.submit` with an `sdk` origin (claude-code.d.ts:8825-8829) and `prompt.compose` has SDK traits (claude-code.d.ts:8353-8359). The Claude desktop app itself runs its sessions as "a long-lived headless session (SDK, desktop)" and loads plugins there, which is encouraging (reference.md:68-69).
- The problems:
  - A session starts with no drawing surface and only gets one when a client attaches (claude-code.d.ts:11596-11600, 10619-10625). Your own SDK app has no public way to attach one, so the plugin's panes and status line would show nothing there; the app draws its own HUD instead.
  - The helper starts on its own only when a terminal or desktop screen attaches (`plugin/hooks/app.ts:104,195-205`). A bare `/jarvis` sent as a prompt should still start it with no code change, since plugin commands run in headless sessions (`plugin/hooks/commands.ts:86-92`, claude-code.d.ts:1793-1800, 1842-1848; inferred, untested).
  - Getting HUD state from the plugin to the app: `ui_log` lines reach an SDK host (claude-code.d.ts:2392-2393) but their format is not documented. A neater path: the app registers an in-process SDK tool server, and the plugin calls it with `$.mcp.call` (claude-code.d.ts:2665-2690, 5918-5925). Each piece is documented; the combination is inferred and untested. Option A's localhost link also works.
  - In a stream-json run, a plugin module that fails to load (for example when mods are switched off) is reported only in the debug log, so the app must check that the plugin actually loaded (reference.md:76).
- Login and billing are the same as option B, since the SDK hosts Claude Code.
- Size: medium, but only after the open questions above are answered.

**C2. The app attaches to Claude Code as a drawing screen, the way the Claude desktop app does.**

- The plugin API names the messages involved (`ui_attach` at claude-code.d.ts:4356, `ui_render` at claude-code.d.ts:10297) but never documents their format or opens them to other apps.
- The list of screens is closed: terminal, desktop, mobile and vscode (claude-code.d.ts:10304). A custom app would have to pretend to be `desktop`, on an early-access API that may change without notice (claude-code.d.ts:4).
- Not recommended.

**Baseline you already have: the Claude desktop app's Code tab.**

- Jarvis already draws its ring there as SVG (`plugin/hooks/register.tsx:152-165`, `plugin/hooks/ui.tsx:188-203`), and the Code tab runs on your subscription.
- Limits: SVG of up to 131,072 characters in a sandboxed frame with no scripts (claude-code.d.ts:12185-12219), no full-screen or overlay, and "In the desktop app focus mode does nothing yet" (`README.md:171`).
- The ring there is a fixed 240 by 240 pixels today (`plugin/hooks/ui.tsx:198-204`); making it fill the panel is a small change, and its motion already runs smoothly as SVG animation (claude-code.d.ts:12210-12218). The HUD still stays inside the Claude window.

---

## 3. What carries over and what is rewritten

Report 3 measured the code with `wc -l`. About 73% of source lines carry over unchanged even in a full move to option B (10,055 of 13,680). Among the plugin's non-test modules only `plugin/hooks/register.tsx:4` imports anything from `claude-code` at runtime; the others import only types. The tests are different: every test file imports `claude-code/testing` and runs only under `claude plugin test` (Report 3, checked by the verifier).

| Part | Size (lines) | Option B (SDK app) | Option A (companion) |
|---|---|---|---|
| Voice helper, Python (`plugin/voice/`) | 8,164 | As-is | As-is |
| Local voice, Chatterbox (`plugin/local-voice/`) | 430 | As-is | As-is |
| Protocol schema (`plugin/protocol/schema.json`) | 181 | As-is | As-is, maybe a few new HUD messages (inferred) |
| Pure TypeScript: `sentences.ts`, `hud-svg.ts`, `protocol.ts`, `hud-ring.ts` | 1,280 | As-is (`hud-ring.ts` may be dropped) | `hud-svg.ts` reused in the app's HUD |
| Logic: `app.ts`, `helper.ts`, `platform.ts`, `setup.ts`, `voice.ts`, `router.ts`, `hud.ts`, `commands.ts` | 3,098 | Ported to Node and the SDK; `commands.ts` becomes tray, settings and voice | Stays in the plugin; for A2, copies of the helper supervisor and setup go into the app |
| Glue: `register.tsx`, `ui.tsx`, `engine.ts` | 527 | Replaced by the app's main process and HTML; the `engine.ts` interface is kept | Stays |
| Plugin manifest and state contract | small | Dropped; the Fish Audio key moves to the Windows credential store (inferred) | Stays |
| Plugin tests | 2,028 + 450 harness, 132 tests | Ported to a new test runner and harness, pure ones included | Stay |
| Helper tests | 6,955; 349 test functions | As-is | As-is |
| Hands branch (`claude/hand-gestures-txb7p3`) | +10,648 Python source and +10,635 Python tests, `hands.ts` 1,412 | Python as-is; `hands.ts` ported; its tool becomes an SDK in-process tool | Unchanged |
| Home branch (`claude/home-control-gje88v`) | +8,467 Python in `jarvis_voice/home/`, small helper edits, +5,467 Python tests, `home.ts` 458 | Python as-is; `home.ts` ported; confirmations move to an app dialog or `canUseTool` | Unchanged |
| CI (`.github/workflows/ci.yml`) | 116 | Helper jobs as-is; plugin job replaced by an app build | Add an app build job |

Sources: Report 3, sections 1 and 5.

**Relative effort per part (inferred)**

| Part | Option A | Option B |
|---|---|---|
| App shell: window, overlay, tray, hotkey, start at login, settings | Medium | Medium |
| HUD in the app using today's `hud-svg.ts` | Small | Small |
| Smoother 60 fps canvas HUD (optional) | Medium | Medium |
| App-to-plugin link and relay | Medium | Not needed |
| App owns the voice helper (port supervisor and setup to Node) | Medium | Medium |
| Plugin changes | Small | Not needed (plugin retired) |
| SDK session and voice-turn wiring | Not needed | Large |
| Transcript, typed input, permission dialogs | Not needed | Large |
| Porting the 132 plugin tests | Not needed | Medium |
| Installer and first-run setup | Medium | Medium |
| **Overall** | **Medium** | **Large** |

---

## 4. Recommended shape, shell and path

### Shell: Electron, with Tauri 2 as a close second (Report 4)

- **Node is built in.** The TypeScript Agent SDK can run in Electron's main process, and the plugin's TypeScript (router, sentence splitter, protocol, helper supervisor, `hud-svg.ts`) moves over with light porting. Electron 44.6.0 (2026-10-05) bundles Node 24.21.0 and Chromium 152 ([Electron release](https://releases.electronjs.org/release/v44.6.0)). Tauri 2.12.1 has no Node, so it would need Node packaged as a separate helper program ([Tauri sidecar](https://v2.tauri.app/learn/sidecar-nodejs/)).
- **Clicks pass through the overlay except on the orb.** Electron's `setIgnoreMouseEvents(true, { forward: true })` works on Windows and macOS ([Electron window interactions](https://www.electronjs.org/docs/latest/tutorial/custom-window-interactions)). Tauri can only turn click-through on or off for the whole window (Report 4, `window.d.ts:1425`).
- **One web engine on Windows and Mac.** Tauri uses WebKit on macOS, so the HUD would need a second round of testing there ([Tauri webview versions](https://v2.tauri.app/reference/webview-versions/)).
- **Other options.** WinUI 3 and WPF are Windows only ([Windows App SDK](https://learn.microsoft.com/en-us/windows/apps/windows-app-sdk/)). Flutter's desktop multi-window support was still incomplete as of mid-2026 per a third-party write-up ([Flutter multi-window](https://startdebugging.net/2026/08/how-to-enable-multi-window-support-in-a-flutter-desktop-app/)); it may have moved since.
- **A cheaper Windows-only route for the HUD alone.** The hand-gesture branch already draws click-through, always-on-top overlay windows from Python with plain Win32 (`WS_EX_LAYERED | WS_EX_TRANSPARENT`, on `claude/hand-gestures-txb7p3`). The voice helper could draw a desktop HUD the same way with no web shell at all, though the ring would have to be redrawn in Python rather than reusing `hud-svg.ts` (inferred). It suits A1 but not a tray app with settings or option B.
- **Hold-to-talk stays in the helper.** Electron's global hotkey reports the key press but not its release (Report 4, `electron.d.ts:8649`), so hold-to-talk keeps using the helper's existing push-to-talk code.
- **Upkeep.** Electron ships a new major version every 8 weeks and supports only the latest 3 ([Electron timelines](https://www.electronjs.org/docs/latest/tutorial/electron-timelines)).

### Shape: start with A, and keep the door open to B

- **Why A first:**
  - It keeps your subscription login with no policy question ([Legal](https://code.claude.com/docs/en/legal-and-compliance)).
  - It uses only documented plugin APIs (Report 2).
  - It keeps per-request model routing and all existing tests.
  - It is the smaller job.
- **Why the door stays open to B:** only `register.tsx` imports Claude Code at runtime (Report 3), and much of the voice, router and HUD logic reaches Claude Code through one interface (`plugin/hooks/engine.ts:39-87`). A second engine implementation is not enough on its own, though: `voice.ts` and `router.ts` take Claude Code's hook shapes directly (`turn.step` streams, `prompt.compose` results; `plugin/hooks/voice.ts:5-15,338-352`, `plugin/hooks/router.ts:8,167-171`), so B also needs small adapters from the SDK's message stream to those shapes (inferred).

### Phased path

Each phase leaves the previous way of running Jarvis working.

| Phase | What changes | Jarvis still works because | Size |
|---|---|---|---|
| 0. Today | Plugin in Windows Terminal and the desktop Code tab | Nothing changes | None |
| 1. Spikes | Run the tests in section 5 | Nothing ships | Small |
| 2. HUD window (A1) | Electron app shows a full-screen or overlay HUD and tray icon. The plugin pushes HUD state to it when the app is running (a setting, off by default). | The plugin still owns the helper. If the app is closed, the push quietly fails and Jarvis runs as today (inferred). | Small to medium |
| 3. App owns the voice (A2) | The app starts the helper at login. The plugin connects through the relay. | If the app is not running, the plugin falls back to starting the helper itself, today's code path (inferred) | Medium |
| 4. Standalone mode (B), optional | The app can host Claude Code itself through the SDK, reusing the shared modules. A switch picks "use my Claude Code" or "standalone". | Option A stays available. The one-helper rule means only one mode holds the mic at a time (Report 3). | Large |
| 5. Mac | Build for macOS | Windows build unchanged | Medium |

**Packaging and setup notes (Report 4)**

- Ship `uv.exe` with the app. On first run, install into the same `%USERPROFILE%\.jarvis` folder `/jarvis setup` uses today (`plugin/hooks/platform.ts:61-75`, `plugin/hooks/setup.ts:31-48,108-120`), so the app and the plugin share one helper, one set of speech models and one version. Use `uv sync --frozen` with `UV_MANAGED_PYTHON=1` so it never touches system Python ([uv environment variables](https://docs.astral.sh/uv/reference/environment/), [uv Python versions](https://docs.astral.sh/uv/concepts/python-versions/)). Show progress in the HUD, since the download is large (inferred).
- Builds you make on your own PC have no Mark-of-the-Web, so SmartScreen does not check them ([SmartScreen in pictures](https://textslashplain.com/2023/08/23/smartscreen-application-reputation-in-pictures)). Code signing only matters if you share the app ([Microsoft SmartScreen reputation](https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/smartscreen-reputation)).
- Mac needs a $99/year Apple Developer account for signing and notarization ([Tauri macOS signing](https://v2.tauri.app/distribute/sign/macos/)). It also needs a microphone usage description and the audio-input entitlement ([Electron systemPreferences](https://www.electronjs.org/docs/latest/api/system-preferences), [Apple entitlement](https://developer.apple.com/documentation/bundleresources/entitlements/com.apple.security.device.audio-input)).

---

## 5. Risks and unknowns to test first

1. **Speed of the app-to-plugin link.** The plugin pushes HUD state with `$.http.fetch` and receives app events through a spawned relay's output. Measure the delay and CPU use in a terminal session and in a desktop-app session.
2. **Mic hand-off.** The app owns the helper, and the plugin falls back to starting it when the app is closed. Check the one-helper rule (a second copy exits with code 3) and heartbeats: the helper allows 60 seconds before the first heartbeat, then exits 15 seconds after the last one, and the plugin beats every 2 seconds (`plugin/voice/src/jarvis_voice/lifecycle.py:21-23,35-36`, `daemon.py:130-131,218-219`, `plugin/hooks/helper.ts:15,25`).
3. **Overlay HUD on Windows 11.** Test a transparent, click-through, always-on-top window. Check the GPU cost at full screen, and whether `hud-svg.ts` blur filters stay smooth at large sizes (Report 3, inferred).
4. **SDK smoke test in Electron on Windows (only if B matters).** Measure time to the first spoken sentence, `interrupt()` during speech, `setModel()` between turns, and a `canUseTool` dialog. Also note which credential source the session reports, without reading or handling any credential.
5. **Hybrid C1 check.** Load the Jarvis plugin through the SDK `plugins` option. Did its module load (reference.md:76)? Do `turn.step`, `turn.complete`, `classic.UserPromptSubmit` and `tool.call` fire, and does `$.turn.abort` stop a turn? Do turns started by `$.prompt.submit` appear in the SDK stream? Does a bare `/jarvis` start the helper?
6. **Login answer.** If running option B on your subscription matters, get Anthropic's answer in writing first.
7. **First-run install.** Test the app's uv setup into the shared `%USERPROFILE%\.jarvis` folder, the model downloads, and the total size.

---

## 6. Questions for Rotem

1. Is it fine if Claude Code has to be open (terminal or desktop app) while Jarvis works? (yes/no) If no, you need option B.
2. For a standalone version, would you pay per use with an API key, separate from your subscription? (yes/no) If no, option B waits on Anthropic's answer.
3. Will anyone besides you install this app? (yes/no) If yes, the login rules and code signing apply.
4. For the HUD, do you want full-screen, a floating orb, or both? (full/orb/both) This decides the window design.

---

## Notes on where the reports disagreed

Each conflict below was checked against the source.

- **Model switching.** Report 1 said the SDK has no per-turn model switching. The SDK type file has `setModel()`, "Change the model used for subsequent responses. Only available in streaming input mode." (sdk.d.ts:2891-2897, checked). Switching between turns works. Rewriting each request mid-turn, as `turn.step` does, has no SDK equivalent (sdk.d.ts:959, Report 3).
- **Barge-in.** Report 1 doubted that interrupt suits barge-in. The type file says `interrupt()` makes the query "stop processing and return control to the caller", in streaming mode (sdk.d.ts:2849-2861, checked).
- **How the SDK runs.** Report 1 said it runs in-process with no CLI subprocess. The SDK overview calls it "a library that runs the Claude Code binary" ([SDK overview](https://code.claude.com/docs/en/agent-sdk/overview)), and the SDK has `pathToClaudeCodeExecutable` (sdk.d.ts:1980).
- **Subscription login in headless or SDK sessions.** Report 1 said there is no path to a claude.ai login. The SDK type file lists "no API key in use - e.g. claude.ai OAuth login" as a credential source (sdk.d.ts:129-131, checked). The barrier is policy ([Legal](https://code.claude.com/docs/en/legal-and-compliance)), not technology.
- **Unofficial sources.** Report 1 cited claudeissues.com, which is not an Anthropic source. This note does not rely on it.
