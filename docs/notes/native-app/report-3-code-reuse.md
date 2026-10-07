# Report 3: what Jarvis code carries over

Raw research for [Jarvis as a native app](../2026-10-07-native-app.md), 2026-10-07. Unchecked; the note is the checked version.

**Can Jarvis become a native app that uses Claude Code? Yes. Most of the code can be reused, and very little of it depends on the plugin system.**

- **Reused as-is (about 73% of source lines, my calculation from `wc -l`):** the Python helper, the local voice, the protocol schema and four pure TypeScript files.
- **Ported:** about 3.1k lines of TypeScript. They already reach Claude Code only through one interface, `engine.ts:1-4,39-87`.
- **Replaced:** about 0.5k lines of glue and drawing code.
- **No longer needed:** the manifest and the state contract.
- Only `register.tsx:4` imports anything from `claude-code` at runtime. Every other module imports only types from it.

`git show origin/main` sources were exported to `/tmp/claude-0/-home-claude-jarvis-claude-mod/198c950a-9122-5877-b218-52d05752d037/scratchpad/main/`, and every repo path below is under `plugin/`. Agent SDK facts come from the published `@anthropic-ai/claude-agent-sdk@0.3.293` type file (`sdk.d.ts`), released 2026-10-07 and bundling Claude Code 2.1.293. Plugin API facts come from `claude-code.d.ts` 2.1.292 (called `cc.d.ts` below).

## 1. Components

| Component | Files (lines) | What it does | Plugin APIs it depends on | Native app |
|---|---|---|---|---|
| Entry and wiring | `hooks/register.tsx` (203) | Builds the engine object from `$` and hooks every event | session.start/attach, command.run, turn.start/step/complete, tool.call, prompt.compose, classic.UserPromptSubmit, ui.render (Pane, AbovePrompt, transcript rows), ui.close, prompt.submit, `$.command.register`, `$.state` | **Replace** with the app's main process and one SDK session |
| Engine interface | `hooks/engine.ts` (115) | The slice of `$` the mod uses, as plain functions | $.env, $.clock, $.http.fetch, $.process.spawn/run, $.fs, $.store, $.state, $.ui.*, $.prompt.submit, $.model.complete, $.session.messages/usage, $.turn.abort | **Port.** Keep the interface; write a Node + SDK version of it. Its types come from `claude-code` and need local copies. |
| Coordinator | `hooks/app.ts` (630) | Settings, stored overrides, view state, status line, toasts, focus mode | PluginOptions (userConfig), $.store, $.state, $.ui.status/toast/log, surfaces (`app.ts:104,195-205`) | **Port.** Settings move to an app store; focus mode is dropped |
| Helper supervisor and protocol | `hooks/helper.ts` (454), `protocol.ts` (209), `platform.ts` (177) | Spawns the helper, reads its event lines, POSTs commands with a bearer token (`helper.ts:197-199`), heartbeat (`:397-401`), restart backoff, cleanup of a leftover helper, child environment (`:423-438`) | $.process.spawn (streaming), $.http.fetch, $.clock, $.fs.exists, `$.state` helperRef | `protocol.ts` **as-is**; `helper.ts` and `platform.ts` **port** to child_process and fetch |
| Setup | `hooks/setup.ts` (316) | Installs the helper with uv into `%USERPROFILE%\.jarvis` and downloads models | $.process.spawn/run, $.fs.write, `$.plugin.root` | **Port**, or replace with a first-run installer |
| Voice turns | `hooks/voice.ts` (424) | Utterance becomes a prompt, stop phrases, reply sent sentence by sentence, barge-in, persona | $.prompt.submit, classic.UserPromptSubmit additionalContext, turn.start/step/complete, $.turn.abort, prompt.compose | **Port.** `VoiceReply`, `isStopPhrase` and `personaText` carry over; the event wiring changes (section 4) |
| Sentence splitter | `hooks/sentences.ts` (336) | Turns streamed markdown into speakable sentences | none (pure) | **As-is.** I ran it unmodified under plain Node 22 |
| Model router | `hooks/router.ts` (271) | Haiku rates the request; Sonnet, Opus or Fable answers; context-size limits; drops a model Claude Code refuses | turn.step model rewrite per request, $.model.complete, $.session.usage, $.ui.log | **Port.** Its pure functions (`spokenTier`, `parseTier`, `familyFor`, `JUDGE_SYSTEM`) carry over; the switching mechanism changes |
| HUD state | `hooks/hud.ts` (441) | Ring mode (`hudMode` `:59-67`), action log, level easing, pane sizing, terminal frame loop | $.ui.blit, $.ui.invalidate, $.ui.open, $.clock.every, `$.state`, tool.call | **Port** `hudMode`, `actionLabel`, the action log and easing. **Drop** pane sizing and blit |
| Ring drawing | `hooks/hud-svg.ts` (289), `hud-ring.ts` (446) | Desktop SVG ring; terminal half-block pixels | none (pure, no imports in `hud-ring.ts`) | `hud-svg.ts` **as-is** in a webview. `hud-ring.ts`: **drop**, or reuse `ringPixels` (`:370`) for a canvas |
| Pane, band and status UI | `hooks/ui.tsx` (209) | Pane trees built from Box/Text/Svg/Raster, band above the prompt, folded transcript rows | ui.render with the engine's elements | **Replace** with HTML in a webview; port the pure helpers (`statusLine`, `replyLines`, `meter`) |
| `/jarvis` commands | `hooks/commands.ts` (385) | Status, setup and settings subcommands | $.command.register (immediate), command.run, $.ui.open | **Replace** with tray, settings UI and voice; parts of the parsing carry over |
| Manifest and state contract | `.claude-plugin/plugin.json` (13 userConfig keys, `fishApiKey` sensitive), `types/index.d.ts` (84), `hooks/hooks.json`, `../.claude-plugin/marketplace.json` | Plugin packaging | plugin system | **Drop.** The Fish key needs an OS credential store (inferred) |
| Mod tests | `hooks/*.test.ts(x)` (2,028) + `test-harness.ts` (450) | 132 tests against the engine | `claude-code/testing`, `claude plugin test` | **Port.** Pure-module tests move easily; the rest need a fake engine |
| Voice helper | `voice/` (8,164 src) | Wake word, VAD, Whisper, echo cancelling, Fish/local TTS, playback, push-to-talk | none. It only receives a token by env and talks over stdout and HTTP; only some strings mention Claude Code (`cli.py:40,354`) | **As-is** |
| Local voice | `local-voice/` (430) | Chatterbox server the helper starts | none | **As-is** |
| Protocol schema | `protocol/schema.json` (181) | v1 events and commands | none | **As-is** |
| CI | `../.github/workflows/ci.yml` (116) | Helper tests on Windows, macOS and Ubuntu; mod validated and tested with the `claude` CLI | `claude plugin validate/test` (`:96-112`) | Helper jobs **as-is**; mod job **replaced** by an app build |
| Branch `claude/hand-gestures-txb7p3` (11 ahead, 8 behind) | `hands/` (+10,647 src), `hooks/hands.ts` (1,412), `protocol/hands.schema.json` | Webcam gesture control as a second Python helper with the same pattern; its Win32 cursor reticle is already a native window | $.tool.register for a `hands` tool answered in a tool.call hook, $.process.spawn, $.http.fetch, `$.state` | Helper **as-is**; `hands.ts` **port**; the tool becomes an SDK in-process tool (`createSdkMcpServer` and `tool`, `sdk.d.ts:615,9784`) |
| Branch `claude/home-control-gje88v` (7 ahead, 5 behind) | `voice/src/jarvis_voice/home/*` (+8,467), `hooks/home.ts` (458) | Apple TV, Bravia, Tuya and Home Assistant control; a console setup wizard | $.tool.register (`mcp__jarvis__home_control`), $.ui.ask for confirmations | Python **as-is**; `home.ts` **port**; confirmations move to an app dialog or `canUseTool` (`sdk.d.ts:1590`) |

Other branches:
- `feat/echo-cancel` and `feat/plain-jarvis-wake` are 0 commits ahead of main, so they are already merged.
- `feat/pc-control` does not exist.

## 2. Helper protocol

**Events** (helper to app, one JSON object per stdout line, ASCII only; `events.py:29-32`): `hello` (port, pid, platform, version, capabilities), `state`, `level` (mic and output, at most 15 Hz; `daemon.py:132,797-803`), `utterance`, `speech_started`, `speech_done` (includes `spokenText`), `barge_in`, `error` (code, fatal), `ready`. Defined in `schema.json:21-105`.

**Commands** (app to helper, `POST http://127.0.0.1:<port>/v1/<name>` with a bearer token): `heartbeat`, `speak{replyId, seq, text≤4000, final}`, `stop`, `listen{start|stop}`, `config`, `status`, `test_voice`, `shutdown`. Defined in `schema.json:107-180`; dispatched in `daemon.py:807-841`.

**Is it a clean boundary? Yes.** It is versioned (v1) and validated on both sides (`events.py:58-67`, `control.py:122-134`). It has capability negotiation (`wake.plain`) and contains nothing Claude-specific, so a native backend can drive it directly. What the app must do itself:
- **Spawn the helper and read stdout.** The port is only known from `hello`, and the token goes in by environment (`cli.py:325-335`).
- **Send a heartbeat about every 2 s.** The helper exits after 15 s without one, or 60 s before the first (`lifecycle.py:35-36`).
- **One helper per user.** A second instance exits with code 3, so the plugin and the app cannot both own the mic at the same time.
- **Split sentences itself.** The helper only takes ordered `speak` sequences (`voice.ts:163-171`).
- **Abort the model turn after `barge_in`.** The helper has already stopped playback by then (`PLAN.md:90`).
- **Call it from a backend, not a webview.** Requests with an `Origin` header, a wrong `Host` or a non-POST method are refused (`control.py:76-94,147-151`). The plan's "HUD page served by the helper" idea (`PLAN.md:204`) would need a new endpoint.
- **`spokenText` is unused today.** The mod declares it (`protocol.ts:69,72`) but never reads it, so the planned "what I said before you interrupted" context (`PLAN.md:92`) is not implemented on main (my reading).

## 3. The HUD

**`ringSvg` is a pure function of (mode, mic, out, t)** (`hud-svg.ts:286-289`):
- It imports only a type (`:13`) and uses a seeded random generator (`:22-31`).
- I ran it unmodified under Node 22. It returns identical output on repeated calls: 5.3–6.1 KB in the blue modes and 50.7–53.5 KB in the thinking and speaking modes.
- Motion is SMIL. `t` sets each spin's start angle, so a redraw continues the motion instead of restarting it.

A webview can render it as-is, with three caveats:
- The SVG has fixed `width="240" height="240"` attributes (`:15,288`). It has a viewBox, so CSS scaling works.
- Reacting to voice levels currently means redrawing, throttled to 150 ms (`hud.ts:29,340-348`). A webview could do it every frame, or turn levels into transforms (inferred).
- The blur filters may be expensive at full-screen size (inferred).

Ring mode comes from `hudMode` (`hud.ts:59-67`): the helper's state plus "a turn is running". In the SDK, a turn runs from sending a message until its `result` message (inferred).

**What a full-screen HUD needs that a pane cannot give:**
- **Its own window.** A pane is a framed region the surface places inside Claude Code. An unrequested pane appears only at 144+ terminal columns (`cc.d.ts:2451-2472`). It cannot be full-screen, borderless, on a second monitor, always on top, or a transparent click-through overlay.
- **More than character cells in the terminal.** The terminal has no Svg (`reference.md:119`). The Raster element is limited to 512×256 cells and 1,024 color pairs (`cc.d.ts:9181-9213`). It is repainted by blit every 50 ms, with frames skipped for large rings (`hud.ts:21,33,284`).
- **Scripting on desktop.** The desktop Svg is limited to 131,072 characters and drawn in a script-less sandboxed frame: SMIL and hover only, no canvas, WebGL, requestAnimationFrame or clickable widgets (`cc.d.ts:12185-12219`).
- **Focus mode on desktop.** It does nothing in the desktop app (`README.md:171`). In the terminal it can only fold transcript rows (`register.tsx:185-196`).

## 4. Where the voice-turn logic goes with the Agent SDK

The inferred design is an Electron app, or Tauri with a Node sidecar. It holds one long-lived `query()` with an async-iterable prompt (streaming input mode, `sdk.d.ts:3247-3250`), the ported `Helper`, `Voice` and `ModelRouter` classes, and a webview for the HUD.

| Mod mechanism | Agent SDK equivalent |
|---|---|
| `$.prompt.submit({ asUser })` (`register.tsx:71`) | Push an `SDKUserMessage` into the input stream (`sdk.d.ts:6282-6294`). The `priority` field's queueing behavior is undocumented in the types. |
| Persona as a `prompt.compose` section (`voice.ts:413-423`) | `systemPrompt: { type: 'preset', preset: 'claude_code', append }` (`sdk.d.ts:2404-2413`) |
| Per-message voice note (`voice.ts:299-308`) | `UserPromptSubmit` hook `additionalContext` (`sdk.d.ts:9960-9962`), or a prefix on the message. The app knows which messages are spoken, so the text-matching in `voice.ts:310-331` goes away (inferred). |
| Sentence streaming from `turn.step` text chunks (`voice.ts:362-386`) | `includePartialMessages: true` (`sdk.d.ts:1859-1863`) gives `stream_event` messages carrying raw Messages API events (`sdk.d.ts:5509-5517`). Feed text deltas to `VoiceReply.feed`, call `endLine` when a tool_use block starts, skip events with a non-null `parent_tool_use_id` (subagents), and call `finish` on `result` (`:5759`). |
| Barge-in via `$.turn.abort({ turnId })` (`voice.ts:283`, `cc.d.ts:2893-2903`) | `Query.interrupt()`, streaming input mode only (`sdk.d.ts:2853-2861`), or `abortController` (`:1527`). Interrupt is not tied to a turn id, so "never abort a typed turn" becomes the app's own bookkeeping. |
| Model routing by rewriting `model` per request in `turn.step` (`router.ts:167-196`, `cc.d.ts:13306-13310`) | **Not exposed.** The SDK hook list has no per-request hook (`sdk.d.ts:959`). Use `Query.setModel()` (`:2891-2897`) before pushing the message. The docs say a mid-turn switch takes effect after the current response ([TypeScript reference](https://code.claude.com/docs/en/agent-sdk/typescript)). The 160k-token fallback (`router.ts:170-175`) becomes a mid-turn `setModel()` and the refused-model handling uses `fallbackModel` (`:1685`) (both inferred). |
| Haiku judge via `$.model.complete` (`router.ts:254`) | No one-shot completion is exported. Use a second short `query()` (with `prewarm`, `:2826`) or a direct API call (inferred; costs extra latency). |
| Context size via `$.session.usage` | `getContextUsage()` (`:3037`) or usage on result messages |
| HUD action log from tool.call (`register.tsx:118-130`) | `PreToolUse`, `PostToolUse` and `PostToolUseFailure` hooks with `tool_name` and `tool_input` (`sdk.d.ts:2639-2661,2741-2744`). `actionLabel` carries over. |
| Permission prompts drawn by Claude Code | The app draws them through `canUseTool` (inferred) |

**Hybrid option (inferred, awkward).** An SDK host can load the Jarvis plugin itself, through `plugins: [{ type: 'local', path }]` (`sdk.d.ts:2036,5623-5635`) or `CLAUDE_CODE_PLUGIN_DIRS` (`reference.md:68`). That would keep `turn.step` routing and the persona in the mod unchanged. But:
- SDK sessions have no surface and "draw nowhere yet" (`cc.d.ts:11596-11599`), so panes, the band and the status line show nothing.
- `app.ts:195-205` starts the helper only when a terminal or desktop surface attaches, so the helper would not start without a code change.
- The SDK types have no channel that carries a plugin's state or UI to the host.

**Caveat on the subscription.** The SDK docs say: "Unless previously approved, Anthropic does not allow third party developers to offer claude.ai login or rate limits for their products, including agents built on the Claude Agent SDK" ([overview](https://code.claude.com/docs/en/agent-sdk/overview)). The SDK can report that it is running on a claude.ai login (`sdk.d.ts:129`). Whether that applies to a personal app on Rotem's own plan should be checked.

## 5. Size (lines from `wc -l`; tests counted with grep)

| Part | Source | Tests |
|---|---|---|
| Mod (`hooks/`) | 4,905: pure 1,280, ported logic 3,098, glue 527 | 2,028 lines + 450 harness; 132 tests in 7 files (commands 42, helper 20, hud 18, voice 15, sentences 14, router 13, focus 10) |
| Voice helper | 8,164: audio 1,886, daemon/speech/control/events/lifecycle/protocol 2,226, cli/setup/doctor/logs 985, tts 846, listen 843, stt 563, platform 439, ptt 349; plus 253 in scripts | 6,955 lines; 349 test functions (plus 39 parametrize decorators) in 19 files |
| Local voice | 430 | 154 lines; 7 tests |
| Hands branch | +10,647 Python, +1,495 mod | +10,635 lines; 588 Python tests, 39 mod tests |
| Home branch | +8,467 Python, +558 mod | +5,467 Python, +530 mod; 188 Python tests, 28 mod tests |

The scratch copies used for the Node runs are in `/tmp/claude-0/-home-claude-jarvis-claude-mod/198c950a-9122-5877-b218-52d05752d037/scratchpad/svgcheck/`. The SDK type file is at `/tmp/claude-0/-home-claude-jarvis-claude-mod/198c950a-9122-5877-b218-52d05752d037/scratchpad/sdkpkg/x/package/sdk.d.ts`.
