# Report 2: Claude Code plugin surfaces and headless behavior

Raw research for [Jarvis as a native app](../2026-10-07-native-app.md), 2026-10-07. Unchecked; the note is the checked version.

**Native Jarvis app: how Claude Code lets other clients draw a plugin's UI (research only, from the two plugin API files)**

Sources:
- **D** = `/tmp/claude-0/bundled-skills/2.1.292/c10c8baefad44dcbceab6b3d84399fe8/plugin-authoring/types/claude-code.d.ts`, written by Claude Code 2.1.292 (D:1). It is marked "EARLY ACCESS: this surface may change between releases without notice" (D:4).
- **R** = `.../plugin-authoring/reference.md` in the same folder.
- **J** = `rotembab/jarvis-claude-mod` at `origin/main` 97e6bf4 (read-only).
- The examples folder has nothing specific to surfaces.
- I used no web sources, so nothing below covers the Agent SDK's own docs (for example `canUseTool` or the stream-json flags).

---

## 1. Surfaces, attaching, and the drawing round-trip

**There are four surfaces, and the list is closed.** `RenderSurface = 'terminal' | 'desktop' | 'mobile' | 'vscode'` (D:10304).
- D:10293-10297: "`terminal` is Ink, which draws the hook's whole tree; the rest are remote surfaces drawing it themselves. `desktop` is Claude Code Desktop, `mobile` the Claude mobile app, `vscode` Claude Code for VS Code."
- R:105-107 lists the same four.
- No fifth, third-party surface is declared anywhere.

**Each surface draws a different set of elements** (D:3770-3772, table at D:3775-3840).
- All four have Box, Text, Button, Link, Code and Markdown.
- Svg is on every remote surface. Raster and Image are terminal only.
- Input and Select are on every surface except mobile. D:3809-3810: "the mobile app draws no field yet; not a limit of the device, nor of the control protocol (ui_input, ui_select)."
- `Client` is on terminal and desktop only. D:3824-3828 explains why vscode lacks it: "a remote `Client`'s module, presses and posts (ui_client_module, ui_client_press, ui_message) name no surface. They are the desktop's alone today…"
- Svg is drawn in isolation: as an image, or in a sandboxed frame without scripts when `isInteractive` is set; the mobile app uses a web view (SvgProps doc, about D:13300-13340).

**How a client attaches**
- The terminal never "attaches": "the terminal's attachment is the REPL's binding and raises nothing" (D:10624).
- A remote client raises `session.attach`. D:4355-4356: "Fires when a remote client joins the session's roster of attached surfaces: it said so (ui_attach), or it first asked to draw." D:10623 repeats this.
- The attach input carries:
  - `surface`: "what it declared, a rendering fact" (D:10629-10631)
  - `clientId`: "A client that never named itself is `<surface>:default`" (D:10633-10634)
  - an optional `viewport` (D:10637-10640)
- Leaving is `session.detach`, with reason `detach` ("it said so (ui_detach)") or `end` (D:10947-10950).
- Several clients can be attached at once ("a terminal and two phones", D:2781). `$.session.surfaces()` lists them (D:2778-2788).

**How a remote client gets the drawing**
- D:10297-10302: "A remote surface asks over the wire (ui_render), draws with the props the hook handed core… Each surface's ask is its own evaluation." So `ui.render` runs once per surface, and `e.surface` picks the element set (R:105-119).
- The tree it receives is plain data. Event handlers never leave the plugin. For a Button: "The `onPress` closure stays in the plugin's own environment under `press.handle`; the host holds the handle for the lifetime of the drawing" (D:9306-9309). Select works the same way (D:9480-9486).
- Pane placement: the remote surface reports `isFullscreen` along with its size (D:10327-10336). "A remote surface that reports it places panes… the mobile app reports `false`" (D:10335-10336).
- Status and toast text is drawn up to 10,000 characters "remotely" (D:2431, D:2445). Inferred: remote surfaces do show status and toasts.

**How input comes back**
- Presses, typing and picks raise `ui.press`, `ui.input` and `ui.select`, each with `e.surface` (D:3962-3989; argument types at D:13833, D:14050).
- The wire names given are `ui_input` and `ui_select` (D:3810). For Client regions they are `ui_client_press` and `ui_message` (D:3825).
- These files do not name a wire message for an ordinary Button press, and do not give the format of any wire message.

**The `Client` element and `ui_client_module`**
- `Client` is "A region one of the plugin's SURFACE MODULES, named by path, draws and handles input for on the drawing thread, without `$`… talks to the plugin's hooks through `ui.message`. The desktop carries it as data" (D:9545-9550).
- Its props (D:1489-1530):
  - `key`
  - `module`: a string literal path. "a variable there is refused at load" (D:1499-1506)
  - `props`: JSON of at most 100,000 characters (D:1511)
  - `width`, `height`, `flexGrow`
- The module is a `ClientModule(props, surface)` that returns a tree with no nested Client (D:1420-1430).
- What the module gets in `ClientSurface` (D:1540-1594):
  - `elements`, `state` and `setState`
  - `columns` and `rows`
  - `every(ms, fn)`: its own frame clock (D:1575)
  - `onPointer` (D:1580) and `onKey` (D:1585)
  - `post(data)`, which reaches only this plugin as a `ui.message` "one per frame at most" (D:1587-1594, D:3991-3998)
- A `ui.message` hook can answer `{ props }` to update that instance without a redraw (D:13924-13935).
- `UiMessageArgument.surface` is "`terminal`, or `desktop` once it has them" (D:13893).
- Failures raise `ui.fault`, whose phase `load` means "not fetched, refused, a throw at mount" (D:13671).
- **Inferred:** `ui_client_module` is how a remote surface fetches the module's source, since a `load` fault can mean "not fetched". The files do not say what it carries.

## 2. Can a third-party app speak this protocol?

**Exact text, with what each line actually says:**

| Where | Quote | Covers |
|---|---|---|
| D:3809-3810 | "nor of the control protocol (ui_input, ui_select)" | Field input only |
| D:3824-3828 | "a remote `Client`'s module, presses and posts (ui_client_module, ui_client_press, ui_message) name no surface. They are the desktop's alone today" | Client messages, desktop only |
| D:10297 | "A remote surface asks over the wire (ui_render)" | Drawing |
| D:4356 / D:10623 | "(ui_attach)"; D:10947 "(ui_detach)" | Attach and detach |
| D:2392-2393 | "a `-p` or SDK host receives it as `ui_log`" | `$.ui.log` output |
| R:69 | "a long-lived headless session (SDK, desktop) … its reload lines reaching the host as `ui_log` messages" | Reload notices |
| R:68 | "`CLAUDE_CODE_PLUGIN_DIRS` names the same folders where no flag can be given (a session the desktop app or an SDK host starts)" | Loading plugins |
| R:172-175 | "In a session a host runs headless (the desktop app) … asked of every attached client that said it answers selection reads" | Selection reads |
| D:11597-11598 | "null for a `-p` run or the SDK, which draw nowhere yet." | Start state |

**What is stated:**
- The desktop app runs Claude Code headless as a host and attaches as a client (R:69, R:172-175).
- An SDK host receives at least `ui_log` (D:2392-2393).
- The only named clients are Anthropic's desktop app, mobile app and VS Code extension (D:10296-10297). The surface list is closed (D:10304).

**What is not stated:**
- Neither file says a third-party or Agent SDK host may send `ui_attach` or `ui_render`.
- Neither file gives the format of any `ui_*` message.
- There is no "custom" or "native" surface value.

**Inferred:**
- The desktop app probably uses the same headless channel an SDK host uses. "Draw nowhere yet" suggests a surface can attach later.
- A third-party host would have to claim to be `desktop` (or another of the four), and the protocol is undocumented, early access (D:4) and possibly private.
- The published SDK's type declarations were outside this task. Checking them for `ui_attach` is the next step.

## 3. A session with no terminal (`claude -p` or an Agent SDK host)

**Events**

| Event | Stated | Source |
|---|---|---|
| `session.start` | Fires "once per process for each loaded plugin, before the first prompt", with `surface: null` and `isInteractive: false` for -p and the SDK | D:4295, D:11597-11604 |
| `prompt.submit` | Fires; origin `{kind:'sdk'}` is "The SDK host's own turn (`claude -p`, the Agent SDK)" | D:8825-8828 |
| `prompt.compose` | Fires, with trait `print`: "a session with no terminal behind it (`-p`, the SDK)". `sdk-preset` is the SDK's `claude_code` preset | D:8359, D:8354-8355 |
| `command.run` | Fires: `exitCode` applies "when this command was the whole prompt of a headless run (`claude -p "/lint"`)" | D:1843-1847 |
| `session.append` | Fires: "the headless session" is one of the owners that append rows | R:133 |
| `session.end` | Fires: one listed reason is "a `-p` run done" | D:4388 |
| `turn.step`, `tool.call` | No headless exception is stated. **Inferred:** they fire | D:4418, D:3917 |

**`$.ui` calls**
- `open`: "a `-p` run places all" (D:2459), and the "waits undrawn" answer "never comes back" in a bare -p run (D:13975-13976). **Inferred:** the pane opens but nothing draws it unless a surface attaches.
- `render`: runs only when a surface asks (D:10297).
- `ask`: "Rejects when dismissed, and in a `-p` run (no one to ask)" (D:2411).
- `log`: goes to the host as `ui_log` (D:2392-2393).
- `status` and `toast`: no headless behaviour is stated. **Unknown**, probably dropped with no surface.
- `copy`: returns `no-surface` (D:13605).
- `selection`: returns `undefined` (D:2554).
- `$.session.surfaces()`: "Empty in a plain -p run" (D:2783).

**`$.prompt`**
- `$.prompt.submit` queues "a turn of its own, once the session is idle" (D:2911-2912; R:158). Nothing excludes headless.
- **Inferred:** it works in a long-lived SDK session, but in a one-shot `claude -p` the process may end before the queued turn runs. This needs testing.
- `read` returns empty "(a -p run, an SDK host)" (D:2927). `fill` and `suggest` answer false when headless (D:2938, D:2953).

**`$.process.spawn` and `$.http.fetch`**
- Process: "Commands on the host, run as the user the session runs as. CLI only." (D:3465).
- HTTP: "through the host" (D:3443).
- No headless restriction is stated for either. **Inferred:** both work under an SDK host, because that host runs the CLI.
- `$.audio.play` uses afplay on macOS and plays nothing in a Linux or Windows terminal (D:2638-2640). This does not matter for Jarvis, whose helper does its own playback.

**Permission prompts**
- "The permission dialog is drawn by the engine alone" (D:9288-9289). Plugins cannot draw it.
- A tool check's `ask` goes "to the mode's decider (the dialog, the auto-mode classifier, a headless host)" (D:12718-12719). So under the SDK, the host answers.
- The SDK callback's name (`canUseTool`) is not in these files.
- A plugin can decide ahead of time with `tool.check` allow or deny (D:12780-12784), or with `classic.PermissionRequest` (D:1315-1317).

**Unknown:** R:76 mentions "the switch being off" for a `--plugin-dir` module in `claude -p` without naming the switch. Something may gate function hooks.

## 4. Two ways a native Jarvis app could reuse the plugin

**Already true today (J):**
- Jarvis already draws for the desktop surface. The terminal gets a Raster ring; every other surface gets an Svg ring (J `plugin/hooks/register.tsx:152-165`, `ui.tsx:188-203`).
- It handles a desktop attach that arrives after `session.start` (J `app.ts:193-205`, `register.tsx:90-92`).
- The R:101 statement agrees: a user-scope mod "draw[s] for the `desktop` surface."
- So the Claude desktop app's Code tab is already a "native app hosting Claude Code" that shows the Jarvis HUD.
- A desktop `Client` surface module would allow a smoother ring with its own frame clock, pointer and keys (D:1540-1594) than re-sending Svg.

**(a) Host Claude Code through the SDK or headless, load Jarvis with `CLAUDE_CODE_PLUGIN_DIRS` or `--plugin-dir` (R:68), and attach as a drawing surface**
- Should work, as stated or inferred:
  - The plugin loads, and `session.start`, `turn.step`, `tool.call` and `command.run` fire.
  - Voice prompts go in through `$.prompt.submit`.
  - Speech streaming keeps working, because it lives in `turn.step`.
  - The helper spawns.
  - Permissions are answered by the host.
- Blocker: attaching as a surface (`ui_attach` and `ui_render`) is not documented for third parties. A custom app would have to impersonate `desktop`, and the format is unspecified.
- Jarvis only auto-starts the helper for a `terminal` or `desktop` surface (J `app.ts:104`, `app.ts:195`). With no attach it stays off until `/jarvis restart` (J `commands.ts:58`, `commands.ts:178`) or a code change. This is inferred from the code and not tested.
- A documented fallback for HUD state: `$.ui.log` lines reach the host as `ui_log` (D:2392-2393). But the message format is not documented, and `ui.ask` rejects headless (D:2411).
- To test:
  - whether an SDK host can send `ui_attach` and `ui_render` at all
  - whether turns started by `$.prompt.submit` show up in the SDK output stream
  - `$.process.spawn` of the uv helper under an SDK host on Windows
  - logging in with Rotem's subscription through the SDK (not covered by these files)
  - the unnamed "switch" (R:76)

**(b) Leave Claude Code where it is (terminal or desktop app) and run a separate native window that only talks to the plugin and helper**
- Uses only documented APIs, and needs no surface protocol.
- What blocks it today:
  - The helper's control server accepts POST only and rejects any request with an `Origin` header (J `plugin/voice/src/jarvis_voice/control.py:4`, `:79-80`, `:98`). A webview's own `fetch` would be refused; the window's native side would have to make the call.
  - The token reaches the helper through the `JARVIS_TOKEN` environment variable (J `cli.py:325`).
  - Events go only to stdout of the process that spawned the helper, which is the mod (J `events.py:1`).
- Two ways to get state into the window:
  - The mod POSTs to a localhost server the window runs, using `$.http.fetch` (D:3441-3460), and polls it for commands with `$.clock.every`. **Inferred:** the plugin API has no way to listen for inbound connections; I found none in D.
  - Or add a second, token-protected event stream to the helper.
- Prompts from the window would travel window → helper → mod stdout → `$.prompt.submit`, the path Jarvis already uses.
- To test: how fast and reliable the polling or the new event stream is, and how to share the token with the window safely.

**Bottom line:**
- Today, the supported "native" Jarvis is Claude Code inside the Claude desktop app, where Jarvis already draws.
- A fully custom app is possible with (b), using only public plugin API plus changes to the helper.
- (a), a custom app attached as a surface, relies on a protocol these files name but never document or open to third parties.
