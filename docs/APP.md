# The Jarvis app

The Jarvis app shows Jarvis on your desktop while you work in Claude Code: a big ring over your main screen while you talk to him, a small orb you can put anywhere, and a tray icon. This is step A1 of [the native app plan](notes/2026-10-07-native-app.md): the app is a display only.

- Claude Code and the Jarvis plugin still do all the work. The plugin runs the voice helper, routes your requests and speaks the replies, exactly as before.
- The plugin sends the app what its HUD shows: the ring's mode, your voice level and Jarvis's, what you said, the start of Claude's reply and the last things Claude ran.
- The app shows that as an overlay over the main screen, in the orb, and in the colour of its tray icon.
- Without the app, Jarvis works as before. Without Claude Code, the app shows Jarvis offline.

## Start it

There is no installer yet, so the app runs from a clone of this repository. It needs [Node.js](https://nodejs.org) 22 or later (`winget install OpenJS.NodeJS.LTS`).

```text
git clone https://github.com/rotembab/jarvis-claude-mod
cd jarvis-claude-mod/app
npm ci
npm start
```

- The first start downloads Electron, about 100 MB ("Downloading Electron binary..."). Later starts take a few seconds.
- `npm start` builds the app, then runs it. The window you started it from prints one line, `Jarvis app 0.8.0: the HUD link listens on 127.0.0.1:<port>`, and can be minimized; closing it closes the app.
- A ring appears in the bottom right corner of the main screen (the orb) and in the tray. Claude Code finds the app within a few seconds.
- To stop the app, right-click the tray icon or the orb and choose **Quit Jarvis**.
- Starting it a second time does not open a second copy: the one already running shows its overlay.

To update it, pull the repository and run `npm ci` and `npm start` again.

## What you see

### The overlay

A big ring in the middle of the main screen, with its label (STANDING BY, LISTENING, THINKING, SPEAKING) and a panel under it: what you said, the start of Claude's reply, the last six things Claude ran (`›` running, `✓` done, `✗` failed), and a note when the voice helper needs you (for example "The voice helper is not set up. Run /jarvis setup in Claude Code.").

- **Clicks pass through it** everywhere, and it never takes the keyboard, so you keep typing in Claude Code under it.
- It stays above other windows and out of the taskbar and Alt+Tab.
- It sits on the main screen only.
- **Ctrl+Alt+J** shows or hides it at any time.

When it shows is the **Overlay** choice in the tray menu:

| Choice | The overlay shows |
| --- | --- |
| When you talk to Jarvis (the default) | From "Hey Jarvis" or push-to-talk, while Jarvis listens, thinks and speaks, and fades out 4 seconds after he goes back to standing by. A prompt you type does not show it. |
| Always | All the time, offline too. |
| Never | Only when you show it with Ctrl+Alt+J, the orb or the tray. |

Showing or hiding it by hand lasts until the next change: if you hide it while Jarvis listens, it stays hidden for that turn and shows again the next time you wake him.

### The orb

A small ring, about 180 pixels across, that stays above other windows. It shows the same ring as the HUD, in gray while no Claude Code session is connected.

- Drag it by its edge to move it. Its place is kept for the next start; if the screen it was on is gone, it goes back to the bottom right corner of the main screen.
- Click its centre to show or hide the overlay.
- Right-click it for the tray menu.

### The tray icon

A small ring in the colour of what Jarvis is doing (gray offline, blue standing by or listening, orange thinking or speaking). Click it to show or hide the overlay; right-click it for the menu:

- **Jarvis 0.8.0: standing by**: the version and what Jarvis is doing (grayed, for reading).
- **Show the overlay** or **Hide the overlay** (Ctrl+Alt+J).
- **Overlay**: When you talk to Jarvis, Always, Never.
- **Show the orb**.
- **Start with Windows** (on Windows only).
- **Quit Jarvis**.

## In Claude Code

The plugin looks for the app at the start of each session and every few seconds while it is not connected, so the order you start them in does not matter. Nothing is shown in Claude Code when the app is not running.

| Command | What it says |
| --- | --- |
| `/jarvis app` while the app runs | "The Jarvis app 0.8.0 is connected and shows the HUD. /jarvis app off stops sending it." |
| `/jarvis app` while it does not | "The Jarvis app is not running. Start it with npm start in the app folder (docs/APP.md); Jarvis finds it within a few seconds." |
| `/jarvis app` when the app's address is there but it does not answer | "Found the Jarvis app's address, but it did not answer (*reason*). Start it again with npm start in the app folder." |
| `/jarvis app off` | "The HUD is no longer sent to the Jarvis app. /jarvis app on sends it again." It stays off in new sessions. |
| `/jarvis app on` | Sends it again (on is the default). |

- With several Claude Code windows open, each one sends its HUD. The app shows the window that runs the voice helper, and otherwise the one it heard from last.
- A Claude Code session in the cloud never connects: the app is on your computer, the session is not.

## Start with Windows

Tick **Start with Windows** in the tray menu. Windows then starts the app from this clone when you sign in (the Electron in `app\node_modules` with the `app` folder). It runs what was last built, so after pulling changes run `npm start` once. If you move or delete the clone, untick it first.

## Settings

The app keeps its settings in `%APPDATA%\Jarvis\settings.json`:

```json
{
  "v": 1,
  "overlay": "auto",
  "showOrb": true,
  "orb": { "x": 1716, "y": 836 }
}
```

`overlay` is `auto` (When you talk to Jarvis), `always` or `off` (Never). `orb` is where the orb was left. Use the tray menu rather than editing it; a damaged file only resets the setting it damaged. Start with Windows is kept by Windows itself, not here.

## How the link works

The plugin cannot open a port, so the app is the server and the plugin the client. Everything stays on your computer, on `127.0.0.1`.

### The address file

When it starts, the app writes its address to `<Jarvis home>\app\endpoint.json`. The Jarvis home is the folder `/jarvis setup` uses, `%USERPROFILE%\.jarvis`, unless the `JARVIS_HOME` environment variable is set:

- `JARVIS_HOME` (spaces trimmed) is used only when it is an absolute local path: on Windows a drive path such as `D:\jarvis`, on macOS and Linux a path starting with `/`. Network paths (`\\server\share`) and paths without a drive (`\jarvis`) are ignored, because Claude Code's file access never reads network locations.
- It only moves the app's address file, and Claude Code and the app must both see it, so set it as a user environment variable, not in one terminal.
- The file is one line of JSON, readable only by you:

```json
{"v":1,"port":50999,"token":"<64 lowercase hex characters>","pid":1234,"version":"0.8.0"}
```

| Field | What it is |
| --- | --- |
| `port` | The link's port, chosen by Windows at each start. |
| `token` | 32 random bytes, new at each start. The app never writes it anywhere else, not even its log. |
| `pid` | The app's process id. |
| `version` | The app's version. |

The app writes it to a temporary file and renames that over the old one, so the plugin never reads half a file, and removes it when it quits (unless another copy of the app has written its own since). The plugin reads it at the start of a session, then at most every 5 seconds until it connects.

### Requests

| Request | Answer |
| --- | --- |
| `GET /v1/health` | `200 {"ok":true,"v":1,"version":"0.8.0","pid":1234}` |
| `POST /v1/hud` with a snapshot (below) | `200 {"ok":true}` |

Every request is checked in this order, and the first failure answers. An error looks like `{"ok":false,"error":{"code":"unauthorized","message":"missing or invalid bearer token"}}`.

| Check | Answer when it fails |
| --- | --- |
| No `Origin` header (a web page always sends one with a POST) | 403 `forbidden` |
| `Host` is `127.0.0.1:<port>` or `localhost:<port>` | 403 `forbidden` |
| `Authorization: Bearer <token>` with this start's token | 401 `unauthorized` |
| A known path | 404 `not_found` |
| The right method for it | 405 `method_not_allowed`, with an `Allow` header |
| POST: a `Content-Length` and no `Transfer-Encoding` | 411 `length_required` (400 `bad_request` for an invalid length) |
| POST: at most 65,536 bytes | 413 `too_large`, without reading the body |
| POST: UTF-8 JSON | 400 `bad_json` |
| POST: a valid snapshot | 400 `bad_snapshot`, naming the field |

Every answer is JSON with `Cache-Control: no-store` and `X-Content-Type-Options: nosniff`, and never an `Access-Control-*` header. The plugin waits at most 1 second for an answer, sends one request at a time, and after anything but a 2xx or 400 forgets the address and reads the file again 5 seconds later.

### The snapshot

The plugin sends one when anything the HUD shows changes, its voice levels at most 15 times a second while Jarvis listens or speaks, and once every 2 seconds anyway. The app treats a window it has not heard from for 6 seconds as gone. Version 1 of the snapshot, as a JSON Schema:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "Jarvis HUD snapshot, version 1",
  "type": "object",
  "required": ["v", "sessionId", "mode", "phase", "mic", "out", "actions", "isOwner", "at"],
  "properties": {
    "v": { "const": 1 },
    "sessionId": { "type": "string", "pattern": "^[A-Za-z0-9_-]{1,64}$" },
    "mode": { "enum": ["offline", "sleeping", "listening", "thinking", "speaking", "interrupted"] },
    "phase": { "type": "string", "pattern": "^[a-z_]{1,32}$" },
    "mic": { "type": "number", "minimum": 0, "maximum": 1 },
    "out": { "type": "number", "minimum": 0, "maximum": 1 },
    "utterance": { "type": "string", "maxLength": 500 },
    "reply": { "type": "string", "maxLength": 600 },
    "actions": {
      "type": "array", "maxItems": 6,
      "items": {
        "type": "object", "required": ["label", "status"],
        "properties": {
          "label": { "type": "string", "maxLength": 80 },
          "status": { "enum": ["running", "done", "failed"] }
        }
      }
    },
    "isOwner": { "type": "boolean" },
    "at": { "type": "number", "minimum": 0 }
  }
}
```

| Field | What it carries |
| --- | --- |
| `sessionId` | A random id, new with each Claude Code session. |
| `mode` | The ring's mode. |
| `phase` | What the voice helper is doing (`sleeping`, `listening`, `speaking`, `not_installed` and so on). |
| `mic`, `out` | Your voice level and Jarvis's, 0 to 1. |
| `utterance` | The last thing you said. |
| `reply` | The start of Claude's last reply, without markdown or code. |
| `actions` | The last things Claude ran, newest first. |
| `isOwner` | True from the window that runs the voice helper. |
| `at` | When the plugin sent it, in milliseconds; the app only uses it to drop a snapshot that arrives after a newer one from the same window. |

The app is strict about types and lenient about lengths: a wrong type is refused, but a text that is too long is cut (ending in "…"), levels are clamped to 0 to 1, a list longer than 6 keeps its first 6, and fields it does not know are dropped. That way a newer plugin never freezes an older app's display.

A sample:

```json
{"v":1,"sessionId":"3f9a0c1d2b4e5f60","mode":"thinking","phase":"sleeping","mic":0,"out":0,"utterance":"Run the tests","actions":[{"label":"Bash Run the unit tests","status":"done"}],"isOwner":true,"at":1759870000000}
```

### Why it is safe

- The link listens on `127.0.0.1` only, so other computers cannot reach it.
- A request needs the token, which is in a file only you can read and changes every time the app starts. The token is compared in constant time.
- Web pages cannot use it: a browser sends an `Origin` header with every cross-site POST and the site's name as `Host`, and the app refuses both. That also stops DNS rebinding.
- Bodies are capped at 64 KB, must declare their length, and are checked field by field before anything is shown. Texts are shown as plain text, never as HTML, so nothing you say or Claude writes can run in the app.
- The link is one way: the app can only show what it is sent. It cannot send Claude Code anything, run commands or read your files.
- The app's windows run with Electron's sandbox and context isolation, without Node, under a strict content security policy (no remote content, no inline scripts or styles). They cannot navigate, open windows or load anything from the network, and get no permissions (camera, microphone, notifications). The page script can reach only three functions: receive the view, toggle the overlay, open the menu.

## Privacy

- The app makes no network requests, and blocks every request from its windows other than its own files.
- It writes only its settings (`%APPDATA%\Jarvis`) and the address file (`%USERPROFILE%\.jarvis\app`), plus the folder Electron keeps for any app's cache in `%APPDATA%\Jarvis`.
- What the plugin sends it (your words, the start of Claude's reply, the names of the tools Claude ran) stays in memory and is gone when the app quits.

## Known limits

- It runs from a clone with `npm start`: no installer, no signed build, no automatic updates.
- The overlay shows on the main screen only. The orb can sit on any screen.
- Prompts you type do not show the overlay in its default setting; choose **Always** to see every turn.
- The app only shows the HUD. The voice helper still runs inside Claude Code, so Jarvis listens only while a Claude Code window with the plugin is open. Moving the voice into the app is the next step (A2).
- It is built and tested on Windows 11 and Linux (the tests run under a virtual screen). Some Windows behaviour is checked only by hand: the overlay and orb staying out of Alt+Tab, dragging the orb, its right-click menu, staying above the taskbar, and Start with Windows.

## Troubleshooting

| Problem | What to try |
| --- | --- |
| `/jarvis app` says it is not running | Start the app (see [Start it](#start-it)). If `JARVIS_HOME` is set, Claude Code and the app must both see it: set it as a user environment variable and restart both. |
| `/jarvis app` says it did not answer | The app was closed without quitting (for example by ending it in Task Manager). Start it again; it writes a new address. |
| The hotkey does nothing | Another app took Ctrl+Alt+J. The window you started the app from says "Ctrl+Alt+J is taken by another app". Use the orb or the tray instead. |
| Nothing shows when you type a prompt | That is by design. Choose **Always** in the tray's Overlay menu. |
| The orb is gone | Tick **Show the orb** in the tray menu. |
| The app says it could not open its link to Claude Code | It could not write `%USERPROFILE%\.jarvis\app\endpoint.json` (or the `JARVIS_HOME` folder). Check that the folder is writable. |

## Development

From `app/`:

| Command | What it does |
| --- | --- |
| `npm run typecheck` | Type-checks the app, its tests and the plugin files it uses. |
| `npm test` | Unit tests (Node's test runner), no Electron needed. |
| `npm run e2e` | End-to-end tests (Playwright) that start the real app. On Linux run it under a virtual screen: `xvfb-run -a npm run e2e`. The tests pass `--no-sandbox` on Linux only, where CI runners cannot use Chromium's sandbox; the app itself never does. |
| `npm run build` | Builds into `dist/` without starting. |

- Two environment variables keep tests apart from your own copy: `JARVIS_HOME` (where the address file goes) and `JARVIS_USER_DATA` (the settings folder, which also holds the one-copy lock). `JARVIS_SHOTS=<folder>` makes the end-to-end tests save screenshots of the overlay and the orb in each mode.
- The ring is the plugin's own drawing: the app imports `ringSvg` from `plugin/hooks/hud-svg.ts` (and only types from `hud-ring.ts`), and esbuild bundles it. It never imports a plugin file that uses Claude Code's API.
- The version in `app/package.json` moves with the plugin's; `plugin/voice/tests/test_version.py` checks it.

The source:

| Path | What it holds |
| --- | --- |
| `src/main/main.ts` | Wiring: windows, tray, hotkey, link, settings. |
| `src/main/server.ts` | The link server and its checks. |
| `src/main/discovery.ts`, `paths.ts` | The token and the address file. |
| `src/main/sessions.ts` | Which Claude Code window is shown. |
| `src/main/visibility.ts` | When the overlay shows. |
| `src/main/settings.ts` | The settings file and where the orb goes. |
| `src/main/windows.ts`, `tray.ts`, `tray-menu.ts`, `tray-icon.ts` | The overlay and orb windows, the tray and its menu, the tray's ring icon (drawn as a PNG at runtime). |
| `src/preload/preload.ts` | The three functions the pages may call. |
| `src/renderer/` | The two pages, their style and scripts. |
| `src/shared/` | The snapshot check and the view the pages draw. |
| `test/`, `e2e/` | Unit and end-to-end tests. |

## Uninstall

1. Untick **Start with Windows** in the tray menu, then quit the app.
2. Delete the clone, `%APPDATA%\Jarvis` and `%USERPROFILE%\.jarvis\app`.
