# PC control

Phase 4 lets Claude work the PC from a spoken request: open apps, press the media keys, set a timer, run PowerShell. This page covers what Jarvis lets through, what it asks about, what it blocks, and what to check on the PC.

Jarvis only ever makes Claude Code stricter. It never answers a permission decision, never writes Claude Code's settings or rules, and never touches Claude Code's own permission dialogs. When Jarvis says yes to a command, Claude Code's own rules, permission mode and dialogs still decide after it. So in default mode Claude Code may show its own dialog after Jarvis's question, and in plan, dontAsk or auto mode Claude Code can still refuse.

## The guard

Every Bash, PowerShell and Monitor command Claude runs passes through Jarvis's guard first. Writes and edits are checked too: for Claude Code's own settings, Jarvis's secrets, the rest of Claude Code's configuration (skills, agents, commands and hooks, and `.mcp.json`), and places a file runs on its own from (see "Startup locations" below). The guard sorts each command into one of four tiers. When a command does several things, the strictest tier wins.

**What the guard is, and what it is not.** The guard is a best-effort extra check that reads command text. It is not a sandbox. It reads what a command says, and the files a command runs where it can find and read them; it does not watch what a program does once it is running, and a program can do anything its own code says (`npm test` runs whatever the project's test script says, and the guard does not read that). It will miss spellings it does not know. Claude Code's own permission rules remain the real boundary: keep deny rules for what must never happen (`/jarvis pc rules` prints a set), and don't allow Bash or PowerShell broadly, since a command Claude Code allows runs without its own dialog once the guard lets it pass.

**Files a command runs.** When a command runs a local file, Jarvis reads the file and judges its contents with the same rules (with the same handling of line ends and dashes as a typed command), and the strictest tier wins. A file it can't find or read is screen tier: one that isn't there, one over 4 MB, or one whose name or folder is in a variable Jarvis can't fill in (`bash "$SCRIPTS/build.sh"`). This covers:

- a program named by a path, quoted or not, with or without an extension: `& "C:\x\a.ps1"`, `.\x`, `./deploy`, `"./x.sh"`. A file that bash runs directly is read as its `#!` line says, or as a shell script if it has none; a binary file small enough to read is a program, judged by its name. PowerShell and cmd look up a name without an extension the way Windows does (`x.ps1`, `x.bat`, `x.cmd`, a Windows Script Host file ...; cmd also looks for a bare name in the current folder), and every script found is judged. Windows opens other files by their type: a `.py` file is read, documents and pictures pass (they open in their app), and anything else (`.js`, `.vbs`, `.hta`, `.lnk`, `.reg`, an unknown type) is screen;
- launchers: `Start-Process`, `Invoke-Item` (`ii`), `explorer`, cmd's `start` (and Git Bash's), `Import-Module` and `using module` (a `.psm1` or `.ps1` is read; a manifest or a binary module is screen), `powershell -File`, `bash`, `sh`, `source`, a shell or interpreter fed a file (`bash < x.sh`, `bash -s < x.sh`, `cmd < x.bat`, `python3 < x.py`), `python x.py` and `node x.js`;
- inside a script, `$PSScriptRoot` and `%~dp0` stand for the script's own folder, and `~`, `$HOME`, `$env:USERPROFILE` and `%USERPROFILE%` for your user folder. A script that runs another is followed a few levels deep; past that, what is left unread is screen.

Python, JavaScript and other interpreters' files are only checked for a few signs (deleting files, starting programs, Jarvis's secrets); they are not read as closely as shell scripts. A program by name, a program straight in a system folder (`/usr/bin/x`) and a package manager's own scripts (`npm test`, `npm run build`) are not read, so they stay pass and Claude Code's own rules decide them. A script that builds the command it runs (`exec "$JAVACMD" "$@"`, as Gradle's `gradlew` does) is screen. Jarvis reads a file as it is when the command is checked, so a file that an earlier part of the same command may write (a redirection into it, or a command that names it and may change it, such as `cp`, `sed -i` or `curl -o`; not `chmod`) is screen: it runs as that part leaves it.

**Files Claude wrote.** Jarvis remembers the files Claude writes or edits in a session (the last 256). When a later command that does more than read names one that is a script (`.ps1`, `.psm1`, `.sh`, `.bat`, `.cmd`, a Windows Script Host file, or a file with no extension that starts with `#!`), Jarvis reads it and judges it as above, whatever runs it, a launcher it doesn't know included (`wt pwsh deploy.ps1`, `nohup ship &`). A file deleted since adds nothing; one it can't read, or one that another part of the same command writes, is screen.

**Folders.** After a `cd` (`Set-Location`, `sl`, `pushd`, `Push-Location`, cmd's `cd /d`), a relative path that the command later runs or writes is placed in the new folder before it is judged: `cd ~/.claude && cp x settings.json` is never, like `cp x ~/.claude/settings.json`. When Jarvis can't place the new folder (a variable it can't fill in, `cd -`, a drive switch such as `D:`, a registry drive), running a relative path there, redirecting output into one, or naming something that may be a path (it has a dot or a slash in it, or is a name such as `plugins`) in a command that does more than read is screen. Reading there is not affected.

**Paths.** Paths are compared after folding `.` and `..`, doubled separators, case, both kinds of slash, and the trailing dots Windows drops from a name (`.claude.\settings.json` is `.claude\settings.json`; for Write and Edit, trailing spaces too). Where a `..` could matter, both the folded and the written spelling are checked, and the stricter wins. A `.claude` folder and what is in it, named apart (`Join-Path $HOME .claude plugins`), count as that path. So does a new name: `Rename-Item ...\.claude\pluginz plugins` is never, and `ren ...\.claude\x agents` is screen. Copying, moving and linking commands are judged by every path they name. Creating a symbolic link, junction or hard link to Claude Code's or Jarvis's folder (`New-Item -ItemType Junction`, `mklink`, `ln -s`, `CreateSymbolicLink`) is screen.

**Writes and edits.** Every Write, Edit and NotebookEdit is judged twice: by the path as given, and by where it really lands with every symbolic link and junction followed (Jarvis asks Claude Code's file system; for a new file, it asks about the nearest folder that exists). The stricter wins. Screen tier, when the path can't be placed: there is nowhere to resolve it, the real path still has an 8.3 short name in it (`AB12CD~1.JSO`, or the hashed form `SE12AB~1.JSO`), there is a `..` past a folder that isn't there, or the name is a link that leads nowhere. A hard link keeps its own name, so the guard can't see through one; creating one to these folders is screen.

**Startup locations.** Writing into a place that a file runs on its own from is screen tier: a Startup folder (by its path, or as `[Environment]::GetFolderPath('Startup')` and WScript's `SpecialFolders` name it), a PowerShell profile (`$PROFILE`, or a profile file by its name in any folder), the shell startup files Git Bash runs (`~/.bashrc`, `~/.bash_profile`, `~/.profile`, `~/.bash_login`, and zsh's), a Git hook or a repository's `.git/config`, a scheduled-task folder, and `git config` settings that run a program later (`core.hooksPath`, `core.fsmonitor`, `core.editor`, an alias that starts with `!` ...).

| Tier | What happens | Examples |
| --- | --- | --- |
| never | Blocked. Claude is told not to retry or work around it; you can still run it yourself. | `Set-MpPreference -DisableRealtimeMonitoring $true` (turns off Windows Defender), `Format-Volume -DriveLetter D`, `bcdedit /set safeboot minimal`, `vssadmin delete shadows /all`, `New-LocalUser bob`, SendKeys or `xdotool type` (types keystrokes into a window), `claude mcp add ...`, writing `~/.claude/settings.json` or Jarvis's credentials (its 8.3 short name such as `.claude\SETTIN~1.JSO` too, a path with `..` in it, or a relative path after a `cd`; with Write and Edit, through a link or junction as well) |
| screen | A question on screen. Nothing runs until you click "Run it". | `Remove-Item -Recurse -Force $env:USERPROFILE\Downloads`, `git reset --hard`, `git push --force`, `Stop-Computer`, `logoff`, `Set-ItemProperty HKCU:\...`, `Restart-Service Spooler`, `schtasks /create ...`, `iwr ... \| iex`, `Start-Process powershell -Verb RunAs`, `Send-MailMessage`, `Set-ExecutionPolicy`, `icacls ... /grant`, `manage-bde -off C:`, writing a skill, agent, command, hook or `.mcp.json` under `.claude`, writing into a startup location, `.\x.vbs`, `New-Item -ItemType Junction ... -Target $HOME\.claude` |
| voice | In a voice conversation, a spoken yes. Otherwise the same on-screen question. | `git push`, `gh pr create`, `npm publish`, `winget install Spotify.Spotify`, `npm install -g typescript`, `Stop-Process -Name notepad`, `Move-Item *.jpg ~\Pictures`, `Get-Clipboard` (or `cat /dev/clipboard` in Git Bash), `git diff --output=changes.diff`, `shutdown /h` (sleep), `curl.exe -X POST -d @notes.txt https://...` |
| pass | Runs without a question from Jarvis. Claude Code's own rules still apply. | `Get-ChildItem`, `git status`, `npm test`, `New-Item notes.md`, `Rename-Item a.txt b.txt`, `Start-Process spotify`, a script whose contents are all pass |

Commands the guard cannot read are treated as screen tier, not pass: a command built at run time (`Invoke-Expression $s`, `bash $SCRIPT`), a file it runs that Jarvis cannot read, a hidden or encoded command (or a script file with hidden characters, such as one saved as UTF-16), unbalanced quotes, a brace expansion too big to check, `cmd` with arguments it cannot place (a program name behind leading `=`, `,`, `;`, spaces or `@`, quoted or not, is stripped the way cmd.exe strips it), or a PowerShell call whose input Jarvis does not recognise. If the guard itself fails, the command is refused.

The guard is off in cloud sessions, because nothing there runs on your PC.

### The on-screen question

The question uses Claude Code's own question dialog, under the heading "Jarvis":

> Jarvis: Claude wants to run a PowerShell command that deletes files: "Remove-Item -Recurse -Force $env:USERPROFILE\Downloads". Run it?

- "Don't run it" is always the first option, so a stray Enter says no. "Run it" is the second.
- The command is shown on one line, with each line break shown as ⏎, so you can see when several commands are joined. A command longer than 300 characters is shown by the parts that set its tier (the lines, or the pieces between `;`, `&&`, `||` and `|`, that are in that tier on their own; a piece that runs a script file is judged by what the file does), as many as fit, with the number of characters not shown. If no short part sets the tier, the first 300 characters are shown, again with the number not shown.
- Under "Other" you can type a yes ("yes", "run it", "go ahead", "do it", "ok"). Anything else you type counts as no, and Claude is shown what you wrote. A yes with words in another script beside it ("ok, לא עכשיו", "ok, not now") counts as no.
- If you close the dialog, or it closes by itself while you are away, nothing runs and Claude is told to ask again later.
- Two risky commands at once (from two subagents, say) each get their own question. If two questions would read the same, the second ends in "(2)".
- In a voice conversation Jarvis says "That one needs your OK on screen, sir." A spoken answer while the dialog is open doesn't count; Jarvis says "I need a click on screen for that one, sir."
- If you stop the turn while the question is open, the answer is no.

### A spoken yes (voice tier)

In a voice conversation, a voice-tier command is held the first time Claude tries it. Claude says in one sentence what it wants to do and ends its turn. Then Jarvis asks himself, in a fixed line after Claude's words, built from the guard's reason and the start of the command:

> Claude wants to run a Bash command that pushes commits to the remote: git push. Say yes to run it, sir.

The yes answers Jarvis's line, which names the command whatever Claude said before it. A notice and the transcript quote the command as the on-screen question does: a command longer than 300 characters by the parts that set its tier (or its first 300 characters), with the number of characters not shown. The yes is for the whole command, the characters not shown included. If your very next message is a spoken yes and nothing else, and you began saying it after Jarvis finished that line, Claude runs that exact command once.

- Words that count as a yes: "yes", "yeah", "yep", "yes please", "go ahead", "go for it", "do it", "proceed", "confirm", "confirmed", "affirmative", "sure", "ok", "okay". "Jarvis, go ahead." counts. "Yes, and delete dist too" does not; it is a new request. Nor does a yes with words in another script ("OK, не надо", "OK, don't"), or with any word not on the list.
- The yes covers only the command Claude held, with the same tool and the same text. Spacing within a line doesn't matter, but line breaks do: a held `git push origin npm publish` (one command) doesn't cover `git push origin` and `npm publish` on two lines. A different command is held again. When the command runs a script file, the yes is also for that file as Jarvis read it when he asked: if the file has changed by the time the command runs again, the yes doesn't cover it, and the changed file is judged and asked about afresh.
- The helper times both sides on its own clock: when you began speaking (the push-to-talk press, or where your voice began) and when Jarvis's line finished playing. A yes begun before the line ended goes to the on-screen question instead. That covers a yes said while Claude was still working (it waits as the next message, so it would answer a question not yet asked), and a line that was cut short. A helper too old to time its clips gets the on-screen question every time; `/jarvis setup` updates it.
- A yes said over Jarvis's voice (it cut him off) doesn't count, because it may have been his own voice or a TV. The command is held again and Jarvis asks again. The helper marks the clip that cut him off, so another clip's barge-in never counts against your yes. Push-to-talk is never taken for speech over him, but a press before his line ended still goes to the on-screen question.
- The yes answers only the question just asked. The held command is dropped, and Claude has to ask again, when the turn is stopped (Esc, "stop", talking over Jarvis), when you say anything other than a bare yes (a "no", "Jarvis, stop", another question), when a yes or no is spoken while a dialog waits for a click, when you type a prompt, and on a `/clear` or a new session. Any turn in between also unbinds it.
- One command waits at a time, for 2 minutes at most; a newer one replaces it. If Claude tries two different commands in one turn, neither is held, and Claude asks about one at a time.
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

The screen-tier protections (writing a skill, agent, command or hook under `.claude`, or into a Startup folder, `$PROFILE` or `.git\hooks`) get no rules here. A deny rule would block authoring them for good, and an ask rule is the very thing Jarvis does itself; so these apply only while Jarvis is running, and Claude Code's own rules are the backstop when it is not. The never-tier settings and credentials files still get deny rules.

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

A rule like `PowerShell(Set-MpPreference *)` also matches the bare `Set-MpPreference`, so the trailing ` *` doesn't leave a gap. The guard catches some spellings the rules can't, such as aliases, `cmd /c` wrappers (Git Bash's `cmd //c` too), brace expansions and commands joined with `;`. The rules hold where the guard can't: when Jarvis is off or not installed, and for anything the guard's reading of the text misses. Use both; the rules are the boundary, the guard an extra check.

## The desktop tool

On Windows (not in cloud sessions), Claude gets a tool called `mcp__jarvis__desktop` with a few narrow actions, one per call. The voice helper carries them out.

| Action | What it does |
| --- | --- |
| `open {target}` | Starts an app by its Start-menu name ("Spotify", "Notepad"), opens a folder, or opens a link. Links are limited to `https:`, `http:`, `spotify:`, `ms-settings:` and `mailto:`. It never opens a file. |
| `focus {target}` | Brings an open app's window to the front. |
| `media {key}` | `play_pause`, `next`, `previous` or `stop`. Windows has one play/pause toggle, not separate keys. |
| `volume {level}` or `{change}` | Sets 0-100, steps `up` or `down`, or mutes and unmutes. With neither, it reads the volume. |
| `screenshot` | Saves the screen to your Screenshots folder and returns the path. It asks you first. Claude looks at it only by reading the file, and is told to do that only when you asked it to see the screen. |
| `lock` | Locks the PC. |
| `clipboard_read` | Returns the text on the clipboard, up to 4,000 characters. It asks you first. |
| `clipboard_write {text}` | Puts text on the clipboard, up to 20,000 characters. |
| `timer {seconds, label?}` / `timer_cancel {label?}` | When the time is up, Jarvis says "Sir, your tea timer is done." and shows a notice. |

There is deliberately no typing, clicking, other keys, opening files, or running commands. For anything else Claude uses PowerShell, which goes through the guard.

The mod answers the tool itself, so Claude Code's own permission path never sees its calls. Jarvis applies your own Claude Code rules for `mcp__jarvis__desktop` instead:

- A deny rule refuses the action, as does an organization's deny.
- Any ask shows an on-screen question ("Don't do it" / "Do it"): an ask rule, and also Claude Code's own default for a tool no rule allows yet. The question shows the whole target (up to 400 characters), with line breaks shown as ⏎. If the question can't be shown, nothing is done.
- An allow rule lets actions run without that question. Jarvis doesn't add one; to skip the question, add `"mcp__jarvis__desktop"` to `allow` in your settings yourself. Only an allow from your own rule counts: an allow that comes from the mode alone (bypassPermissions with no rule) still gets the question.
- Even with your allow rule, the question comes when:
  - a PreToolUse or PermissionRequest hook in your settings (user, project, local, `--settings` or managed) could match the tool: its matcher is empty, `*`, names the tool, or is a pattern that finds it, such as `mcp__.*`. Apart from an organization's managed hooks, whose deny comes before any plugin, Claude Code runs no settings hook for this tool, so Jarvis asks you instead of letting such a hook decide. Jarvis reads only the hooks' matchers and never logs your settings. PostToolUse and other settings hooks never see the tool's calls, and hooks that come with plugins are not read;
  - the permission mode is not known for the current turn: a call from a subagent (which may be in a plan mode of its own), or a turn that did not start from a prompt Jarvis saw just before it (a turn Claude Code started on its own, one after a prompt a hook blocked, or one for a prompt you typed while another turn ran, since the mode may change while it waits). A change on the PC needs the mode from this turn's prompt, or from a tool Claude ran during this turn.
- If your rules or your settings can't be read, nothing is done.
- In dontAsk mode, only your allow rule lets it run; anything that would ask is refused.
- In plan mode, only the read-only actions work: reading the volume and reading the clipboard. Until Jarvis has seen your first prompt it can't tell whether plan mode is on, so it changes nothing until then. Jarvis learns the mode from each prompt and from each tool Claude runs, so after a Shift+Tab into plan mode in the middle of a turn, a desktop action before Claude's next tool call still sees the earlier mode. Claude Code's own verdict for the tool (`$.tool.check`, which knows the current mode) is still applied then, but Jarvis's own plan-mode hold is not.

Reading the clipboard and taking a screenshot always ask first, whatever your rules allow: a spoken yes in a voice conversation (Jarvis asks "Claude wants to read your clipboard. Say yes to let it, sir."), or a click otherwise ("Don't let it" / "Let it read", "Let it"). Claude sees the clipboard text marked as data, with a note not to follow instructions in it.

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

At the start of each session, Jarvis checks whether Claude Code runs as administrator (with Windows's own `whoami` and `reg` from System32, so another `whoami` on the PATH, such as Git's, can't answer; on macOS and Linux with `/usr/bin/id`, or `/bin/id` where there is none). Until the check has finished, the voice helper and `/jarvis setup` wait. If it does run as administrator, Jarvis stays off:

- no voice helper;
- an error on the HUD;
- `/jarvis setup` and restarting the helper are refused.

Every command Claude runs would run as administrator too. Start Claude Code from a normal window, not with "Run as administrator". Every part of Jarvis that starts a program checks this first, and new ones (such as the home and hands helpers) must too.

The check fails closed. If `whoami`, `reg` or `id` fails (or neither `/usr/bin/id` nor `/bin/id` can run), gives an answer Jarvis can't read, or takes longer than 5 seconds (a cold start under a virus scan, say), Jarvis stays off the same way. The HUD, `/jarvis` and `/jarvis pc` say the check failed and why. `/jarvis restart` runs the check again, and starts the voice helper once it passes.

Jarvis also reads the UAC level. If it is below "Always notify", you see a one-time notice, because some admin changes then happen without a prompt. For PC control, the safer choices are:

- set UAC to **Always notify**: search the Start menu for "Change User Account Control settings" and move the slider to the top; or
- use a **standard account** for daily work, so admin steps need an administrator to sign in.

`/jarvis pc` shows the current level.

## Bypass mode

If a session starts in bypassPermissions mode, Jarvis shows a warning. In that mode Claude Code skips its own rules and dialogs. Jarvis's guard still asks before risky commands and still blocks the never list, but nothing checks beneath it. The desktop tool still asks before each action unless your own allow rule covers it, since the mode's allow is not yours.

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
- **A spoken yes, on time and too soon.** With a voice-tier command held, say "yes" just after Jarvis finishes his line: it should run. Say "yes" while Claude is still working, before he asks: it should go to the on-screen question. With speakers rather than a headset, check that Jarvis's own voice is not taken for a yes.
- **A settings hook on MCP tools.** With `"mcp__jarvis__desktop"` allowed and a PreToolUse hook whose matcher is `mcp__.*`, a desktop action should still ask on screen.
- **`/jarvis pc` on a PC with UAC at the Windows default.** It should show the UAC notice once.
- **A junction to `.claude`.** Make one yourself (`cmd /c mklink /J C:\work\cfg %USERPROFILE%\.claude`), then ask Claude to write `C:\work\cfg\settings.json` with the Write tool. It should be blocked; a Write to a new file in a new folder elsewhere should not ask. This checks that Claude Code's file system answers a junction's real path.
- **A script by its bare name.** With `x.bat` in the project folder (containing `vssadmin delete shadows /all`), `cmd /c x` and `.\x` in PowerShell should be blocked, and a script Claude just wrote with that line in it should be blocked when a command Jarvis doesn't know (`wt pwsh x.ps1`) runs it.
- **8.3 names.** On a volume with short names on, check that Claude Code's real path for `C:\PROGRA~1\...` comes back with long names; if it keeps the short name, every Write by a short name asks on screen.
