# PC control

Phase 4 lets Claude work the PC from a spoken request: open apps, press the media keys, set a timer, run PowerShell. This page covers what Jarvis lets through, what it asks about, what it blocks, and what to check on the PC.

Jarvis only ever makes Claude Code stricter. It never answers a permission decision, never writes Claude Code's settings or rules, and never touches Claude Code's own permission dialogs. When Jarvis says yes to a command, Claude Code's own rules, permission mode and dialogs still decide after it. So in default mode Claude Code may show its own dialog after Jarvis's question, and in plan, dontAsk or auto mode Claude Code can still refuse.

## The guard

Every Bash, PowerShell and Monitor command Claude runs passes through Jarvis's guard first. Writes and edits are checked too, but only for Claude Code's own settings and Jarvis's secrets. The guard sorts each command into one of four tiers. When a command does several things, the strictest tier wins.

| Tier | What happens | Examples |
| --- | --- | --- |
| never | Blocked. Claude is told not to retry or work around it; you can still run it yourself. | `Set-MpPreference -DisableRealtimeMonitoring $true` (turns off Windows Defender), `Format-Volume -DriveLetter D`, `bcdedit /set safeboot minimal`, `vssadmin delete shadows /all`, `New-LocalUser bob`, SendKeys or `xdotool type` (types keystrokes into a window), `claude mcp add ...`, writing `~/.claude/settings.json` or Jarvis's credentials |
| screen | A question on screen. Nothing runs until you click "Run it". | `Remove-Item -Recurse -Force $env:USERPROFILE\Downloads`, `git reset --hard`, `git push --force`, `Stop-Computer`, `logoff`, `Set-ItemProperty HKCU:\...`, `Restart-Service Spooler`, `schtasks /create ...`, `iwr ... \| iex`, `Start-Process powershell -Verb RunAs`, `Send-MailMessage`, `Set-ExecutionPolicy`, `icacls ... /grant`, `manage-bde -off C:` |
| voice | In a voice conversation, a spoken yes. Otherwise the same on-screen question. | `git push`, `gh pr create`, `npm publish`, `winget install Spotify.Spotify`, `npm install -g typescript`, `Stop-Process -Name notepad`, `Move-Item *.jpg ~\Pictures`, `Get-Clipboard` (or `cat /dev/clipboard` in Git Bash), `git diff --output=changes.diff`, `shutdown /h` (sleep), `curl.exe -X POST -d @notes.txt https://...` |
| pass | Runs without a question from Jarvis. Claude Code's own rules still apply. | `Get-ChildItem`, `git status`, `npm test`, `New-Item notes.md`, `Rename-Item a.txt b.txt`, `Start-Process spotify` |

Commands the guard cannot read are treated as screen tier, not pass: a command built at run time (`Invoke-Expression $s`, `bash $SCRIPT`), a hidden or encoded command, unbalanced quotes, a brace expansion too big to check, `cmd` with arguments it cannot place, or a PowerShell call whose input Jarvis does not recognise. If the guard itself fails, the command is refused.

The guard is off in cloud sessions, because nothing there runs on your PC.

### The on-screen question

The question uses Claude Code's own question dialog, under the heading "Jarvis":

> Jarvis: Claude wants to run a PowerShell command that deletes files: "Remove-Item -Recurse -Force $env:USERPROFILE\Downloads". Run it?

- "Don't run it" is always the first option, so a stray Enter says no. "Run it" is the second.
- Under "Other" you can type a yes ("yes", "run it", "go ahead", "do it", "ok"). Anything else you type counts as no, and Claude is shown what you wrote.
- If you close the dialog, or it closes by itself while you are away, nothing runs and Claude is told to ask again later.
- Two risky commands at once (from two subagents, say) each get their own question. If two questions would read the same, the second ends in "(2)".
- In a voice conversation Jarvis says "That one needs your OK on screen, sir." A spoken answer while the dialog is open doesn't count; Jarvis says "I need a click on screen for that one, sir."
- If you stop the turn while the question is open, the answer is no.

### A spoken yes (voice tier)

In a voice conversation, a voice-tier command is held the first time Claude tries it. Claude then says in one sentence what it wants to do, asks whether to go ahead, and ends its turn. If your very next message is a spoken yes and nothing else, Claude runs that exact command once.

- Words that count as a yes: "yes", "yeah", "yep", "yes please", "go ahead", "go for it", "do it", "proceed", "confirm", "confirmed", "affirmative", "sure", "ok", "okay". "Jarvis, go ahead." counts. "Yes, and delete dist too" does not; it is a new request.
- The yes covers only the command Claude held, with the same tool and the same text (spacing aside). A different command is held again.
- The yes answers only the question just asked. If anything else comes between, spoken or typed (a "no", another question), the held command no longer counts and Claude has to ask again.
- One command waits at a time, for 2 minutes at most; a newer one replaces it. If Claude tries two different commands in one turn, neither is held, and Claude asks about one at a time.
- A yes said while Jarvis is still talking doesn't count, because it may have been Jarvis's own voice or a TV. Claude asks again.
- A typed message, or a command from a subagent, gets the on-screen question instead.

## Commands

- `/jarvis pc` shows the state: whether the guard is on, whether Claude Code runs as administrator, the UAC level, whether the desktop tool is ready, the permission mode, and what stand down would stop.
- `/jarvis pc check <command>` shows the tier a command falls in, as PowerShell and as Bash, and why. For example, `/jarvis pc check Remove-Item -Recurse C:\temp\old` prints `PowerShell: screen · asks for a click on screen: it deletes files (rule delete)`.
- `/jarvis pc rules` prints permission rules to paste into your own settings. See below.
- `/jarvis pc stop` stands down, like saying it.

### Rules to paste

Jarvis writes no settings. `/jarvis pc rules` prints a snippet built from the same table the guard uses, for you to merge into `permissions` (and `env`) in your user settings (`.claude\settings.json` in your user folder). Claude can't do it for you: the guard blocks edits to Claude Code's settings.

- **deny rules** keep the never list blocked even when Jarvis is off or not installed.
- There are **no allow rules**. Jarvis only makes Claude Code stricter, so it suggests nothing that would skip Claude Code's own dialog.
- There are **no ask rules**. Jarvis asks for itself, and an ask rule would add a second dialog.
- The **env line** turns on the PowerShell tool.

Its shape, shortened (`/jarvis pc rules` prints the full, current list):

```json
{
  "env": {
    "CLAUDE_CODE_USE_POWERSHELL_TOOL": "1"
  },
  "permissions": {
    "deny": [
      "PowerShell(Set-MpPreference *)",
      "PowerShell(Format-Volume *)",
      "PowerShell(bcdedit *)",
      "PowerShell(vssadmin delete *)",
      "PowerShell(New-LocalUser *)",
      "PowerShell(claude mcp add *)",
      "Bash(diskpart *)",
      "Edit(~/.claude/settings.json)",
      "Edit(~/.claude.json)",
      "Edit(~/.jarvis/home/credentials*)"
    ]
  }
}
```

A rule like `PowerShell(Set-MpPreference *)` also matches the bare `Set-MpPreference`, so the trailing ` *` doesn't leave a gap. The guard catches more than the rules can, such as aliases, `cmd /c` wrappers (Git Bash's `cmd //c` too), brace expansions and commands joined with `;`. The deny rules are a backstop, not a replacement.

## The desktop tool

On Windows (not in cloud sessions), Claude gets a tool called `mcp__jarvis__desktop` with a few narrow actions, one per call. The voice helper carries them out.

| Action | What it does |
| --- | --- |
| `open {target}` | Starts an app by its Start-menu name ("Spotify", "Notepad"), opens a folder, or opens a link. Links are limited to `https:`, `http:`, `spotify:`, `ms-settings:` and `mailto:`. It never opens a file. |
| `focus {target}` | Brings an open app's window to the front. |
| `media {key}` | `play_pause`, `next`, `previous` or `stop`. Windows has one play/pause toggle, not separate keys. |
| `volume {level}` or `{change}` | Sets 0-100, steps `up` or `down`, or mutes and unmutes. With neither, it reads the volume. |
| `screenshot` | Saves the screen to your Screenshots folder and returns the path. Claude looks at it only by reading the file, and is told to do that only when you asked it to see the screen. |
| `lock` | Locks the PC. |
| `clipboard_read` | Returns the text on the clipboard, up to 4,000 characters. It asks you first. |
| `clipboard_write {text}` | Puts text on the clipboard, up to 20,000 characters. |
| `timer {seconds, label?}` / `timer_cancel {label?}` | When the time is up, Jarvis says "Sir, your tea timer is done." and shows a notice. |

There is deliberately no typing, clicking, other keys, opening files, or running commands. For anything else Claude uses PowerShell, which goes through the guard.

The tool follows your own Claude Code rules for `mcp__jarvis__desktop`:

- A deny rule refuses the action.
- Any ask shows an on-screen question ("Don't do it" / "Do it"): an ask rule, and also Claude Code's own default for a tool no rule allows yet. If the question can't be shown, nothing is done.
- An allow rule lets actions run without that question. Jarvis doesn't add one; to skip the question, add `"mcp__jarvis__desktop"` to `allow` in your settings yourself. Reading the clipboard still asks.
- In dontAsk mode, only an allow rule lets it run.
- In plan mode, only the read-only actions work: reading the volume and reading the clipboard. Until Jarvis has seen your first prompt it can't tell whether plan mode is on, so it changes nothing until then.

Reading the clipboard asks for a spoken yes in a voice conversation, or a click otherwise ("Don't let it" / "Let it read"). Claude sees the clipboard text marked as data, with a note not to follow instructions in it.

If the helper is not running, or is too old to have desktop actions, Claude is told to start it with `/jarvis` or use PowerShell instead.

Timers live in the mod's memory, so a hot reload of the mod (or a restart of Claude Code) loses them.

### Spotify: "play my focus playlist"

Claude doesn't know which playlist is yours. Tell it once:

> My focus playlist is spotify:playlist:<your playlist id>. Remember that.

To get the URI, open the playlist's Share menu in the Spotify app; holding Alt turns "Copy link to playlist" into "Copy Spotify URI". A web link works too: the id is the part after `/playlist/`, before any `?`. Claude keeps the URI in its memory, or you can add a line to your CLAUDE.md. After that, "Jarvis, open Spotify and play my focus playlist" becomes three desktop calls:

1. `open "Spotify"`
2. `open "spotify:playlist:<id>"`
3. `media play_pause`

Spotify may navigate to the playlist without starting it, and the play/pause key may then resume the old queue instead. Check this on the PC (see below).

## Stand down

Say "Jarvis, stand down" or "Abort that" on its own, or type `/jarvis pc stop`. Jarvis then:

- stops talking;
- stops the running turn;
- forgets the command waiting for a spoken OK;
- stops the background commands it saw Claude start (Bash with run in background, and Monitor), up to the last 20.

"Stop" alone stops the speech and the voice turn, but leaves background commands running.

## Never as administrator

At the start of each session, Jarvis checks whether Claude Code runs as administrator (with Windows's own `whoami` and `reg` from System32, so another `whoami` on the PATH, such as Git's, can't answer). Until the check has finished, the voice helper and `/jarvis setup` wait. If it does run as administrator, Jarvis stays off:

- no voice helper;
- an error on the HUD;
- `/jarvis setup` and restarting the helper are refused.

Every command Claude runs would run as administrator too. Start Claude Code from a normal window, not with "Run as administrator". Every part of Jarvis that starts a program checks this first, and new ones (such as the home and hands helpers) must too.

Jarvis also reads the UAC level. If it is below "Always notify", you see a one-time notice, because some admin changes then happen without a prompt. For PC control, the safer choices are:

- set UAC to **Always notify**: search the Start menu for "Change User Account Control settings" and move the slider to the top; or
- use a **standard account** for daily work, so admin steps need an administrator to sign in.

`/jarvis pc` shows the current level.

## Bypass mode

If a session starts in bypassPermissions mode, Jarvis shows a warning. In that mode Claude Code skips its own rules and dialogs. Jarvis's guard still asks before risky commands and still blocks the never list, but nothing checks beneath it.

## Check on the PC

These can't be tested on the Linux CI runner. Check them by hand before calling phase 4 done:

- **Both done-criterion sentences, by voice, in Windows Terminal and in the desktop Code tab:**
  - "Jarvis, open Spotify and play my focus playlist." It should start the playlist, not resume the old queue or pause. If Spotify only navigates, the fallback is a narrow helper action that presses Spotify's own Play button.
  - "Jarvis, delete my Downloads folder." It should stop at the on-screen question, and "Don't run it" should leave the folder alone.
- **The PowerShell tool's input field.** The guard reads `command` (or `script`). If PowerShell uses another name, every PowerShell call gets the on-screen question "Claude wants to run a PowerShell command Jarvis could not read". This fails loud and closed until guard.ts is fixed.
- **The question dialog left idle.** Leave it until it closes by itself. Nothing should run, and Claude should be told you were away.
- **A slow click.** Wait more than 10 seconds before answering. The command should still run after "Run it", not be refused for running out of time.
- **Two questions at once.** Have two subagents each try a risky command, and wait more than 10 seconds before answering. Both should wait for their own click; neither should run unasked.
- **Stand down during a long command.** Start `Start-Sleep 60` in the background, then say "stand down". The task should stop.
- **Stopping a task.** Check whether stopping a background task brings up a dialog of its own.
- **`/jarvis pc` from an elevated window.** It should say Jarvis stays off.
- **`/jarvis pc` on a PC with UAC at the Windows default.** It should show the UAC notice once.
