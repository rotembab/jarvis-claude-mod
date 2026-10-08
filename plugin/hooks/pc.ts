// PC control (docs/PC-CONTROL.md): the guard over Claude's shell commands and
// file edits, the desktop tool, stand down, and the never-as-administrator
// check. Jarvis only ever makes Claude Code stricter: the guard denies, or
// asks the person (a click on the engine's own question dialog, or a spoken
// yes in a voice conversation) and then hands the call on unchanged, so the
// engine's own rules, mode, classifier and dialogs still decide beneath it.
// Nothing here writes settings, answers a permission check or approves a call.
//
// State is module memory (a reload starts afresh, which only asks again).

import type { Jarvis } from './app'
import { delay, describeError } from './engine'
import type { Resolved, Verdict } from './guard'
import { judge, rulesSnippet } from './guard'
import { DESKTOP_TOOL, DesktopTool, textLines, visibleText } from './desktop'
import { HANDS_TOOL_ID } from './hands-gate'
import { HOME_TOOL } from './home'
import { actionLabel, logOwnTool } from './hud'
import type { AdminFacts, UacLevel } from './platform'
import { probeAdmin } from './platform'
import { hasNonLatinLetters, isYesPhrase, phraseWords } from './voice'

export { DESKTOP_TOOL, DESKTOP_TOOL_SPEC, wantsDesktopTool } from './desktop'

/**
 * Raises the engine's own question dialog (`$.ui.ask`, AskUserQuestion) through
 * the waiting hook's own `$`, so its budget clock stops while the person
 * decides. Resolves to the label chosen or the text typed under "Other";
 * rejects when dismissed or when no one can be asked.
 */
export type CallAsk = (question: string, options: { options: string[]; header: string }) => Promise<string>
/** The engine's permission decision for a call (`$.tool.check`'s answer, the fields read here). */
export type RuleVerdict = {
  decision: 'allow' | 'ask' | 'deny'
  reason?: string
  /** The settings rule that decided, as written; absent for a mode's own decision. */
  rule?: string
  /** The most permissive verdict the organization lets a call of the tool reach. */
  ceiling?: 'allow' | 'ask' | 'deny'
}
/**
 * The desktop tool's hook closures over its own `$`: its question dialog, the
 * engine's permission verdict, and each settings source as loaded (only the
 * hooks' matchers are read there, and nothing of it is ever logged).
 */
export type AskPorts = {
  ask: CallAsk
  check: (input: Record<string, unknown>) => Promise<RuleVerdict>
  settings: () => Promise<readonly Readonly<Record<string, unknown>>[]>
}
/** A guarded tool call as `tool.call` carries it: the tool, its arguments beside it, `agentId` in a subagent. */
export type GuardCall = { tool: string; tool_use_id?: string; agentId?: string } & Record<string, unknown>

/** A question with "no" listed first, so Enter, a default or an auto-resolve lands on no. */
export type ScreenQuestion = {
  question: string
  no: string
  yes: string
  /** What happened instead, for the model: "nothing ran". */
  nothing: string
}

/** A voice-tier confirmation: a spoken yes in the voice turn, else a click. */
export type Consent = {
  /** What a spoken yes is for: the tool, the exact command (its line breaks kept) and the text of any script it runs; or "desktop:clipboard_read". */
  key: string
  /** A plain phrase: "pushes commits to the remote". */
  reason: string
  /** What the model does after a yes: "run exactly the same command again". */
  again: string
  /**
   * The question Jarvis speaks himself at the end of the turn that held it,
   * after the model's own words: the yes answers this line, not the model's.
   */
  spoken: string
  /**
   * What the yes would let happen, as the toast and the transcript show it:
   * 'run a Bash command: "git push"'; a long command by the parts that set its
   * tier, with how much is not shown, as the on-screen question shows it.
   */
  shown: string
  screen: ScreenQuestion
  agentId?: string
}

/** The answer when the guard itself failed (a throw, its budget, a re-entry): nothing runs. */
export const GUARD_FAILED = "Jarvis's safety check failed, so this command did not run. Try a simpler command, or ask the user to run it."

/** The plugin's name (plugin.json): its own prompts carry it as their origin. */
const PLUGIN_NAME = 'jarvis'

/** The chip on Jarvis's questions; with the "Jarvis: " lead it marks them as Jarvis's own. */
const HEADER = 'Jarvis'
const LEAD = 'Jarvis: '
const NO_RUN = "Don't run it"
const YES_RUN = 'Run it'
/** Typed under "Other", these count as yes (as do the yes option's own words). */
const TYPED_YES: ReadonlySet<string> = new Set(['yes', 'y', 'run it', 'go ahead', 'do it', 'ok', 'okay'])
const ON_SCREEN = 'That one needs your OK on screen, sir.'
/** How much of the command the question shows (the dialog has no room for a whole script). */
const SHOWN_MAX = 300
/** The longest part of a long command shown as the part that set its tier. */
const PART_MAX = 200
/** The few words of a held command Jarvis says aloud (the toast and the transcript quote more). */
const SPOKEN_WORDS = 3
const SPOKEN_MAX = 40
const HEARD_WHILE_TALKING =
  "The user's 'yes' was heard while Jarvis was still speaking, so it does not count; the command is held again. End your turn now without calling another tool: Jarvis asks the user again himself."

/** A held command waits this long for its spoken OK. */
const CONSENT_MS = 120_000
const TASKS_MAX = 20
/** How many written files the guard remembers (the oldest drop first). */
const WRITTEN_MAX = 256
/**
 * How many rounds of reads one judgement takes (a script that runs a script ...) and how many files
 * it reads in all: what is still wanted after that counts as unreadable, so the call is screen.
 */
const READ_ROUNDS = 4
const READS_MAX = 256
/** Long enough for an aborted call's result (and its background task id) to arrive. */
const STAND_DOWN_WAIT_MS = 300

const ELEVATED_DETAIL = 'Claude Code runs as administrator, so Jarvis stays off'
const ELEVATED_LOG =
  'Jarvis stays off: Claude Code runs as administrator, and so would the voice helper and every command Claude runs. Start Claude Code from a normal window, not "Run as administrator".'
const ADMIN_PENDING = 'Jarvis is still checking whether Claude Code runs as administrator; nothing of Jarvis\'s starts until it knows. Try again in a few seconds.'
const ADMIN_FAILED_DETAIL = 'Jarvis could not check for administrator rights, so it stays off (/jarvis restart checks again)'
const adminFailedLog = (why: string): string =>
  `Jarvis stays off: it could not check whether Claude Code runs as administrator (${why}), and it does not start without knowing. /jarvis restart checks again.`
const UAC_HINT_KEY = 'pc.uacHint'
const WEAK_UAC: ReadonlySet<UacLevel> = new Set(['off', 'never-notify', 'default', 'default-no-dim'])
const UAC_NAMES: Record<UacLevel, string> = {
  off: 'off (UAC is disabled)',
  'never-notify': 'never notify',
  default: 'notify only when apps try to make changes (the Windows default)',
  'default-no-dim': 'notify without dimming the desktop',
  'always-notify': 'always notify',
  'admin-protection': 'administrator protection',
  'standard-account': 'a standard account (admin steps need an administrator to sign in)',
}
const UAC_HINT =
  'Jarvis: UAC is below "Always notify" on this PC, so some admin changes happen without a prompt. For PC control, set it to "Always notify" or use a standard account. /jarvis pc says more.'
const BYPASS_WARNING = "Permission checks are off. Jarvis still asks before risky commands, but Claude Code's own rules are skipped."

const PC_HELP = [
  '/jarvis pc                    the guard, the UAC level, the desktop tool and what Jarvis can stop',
  '/jarvis pc check <command>    which tier a command falls in, and why',
  '/jarvis pc rules              permission rules to paste into your settings (Jarvis writes none)',
  '/jarvis pc stop               stand down: stop speech, the running turn and the background commands Jarvis saw start',
].join('\n')

/** How a guarded tool's call is put in a question. */
const TOOL_WORDS: Record<string, string> = {
  Bash: 'run a Bash command',
  PowerShell: 'run a PowerShell command',
  Monitor: 'start a Monitor command',
  Write: 'write a file',
  Edit: 'edit a file',
  NotebookEdit: 'edit a notebook',
}
/** Said in the question for these rules. */
const WARNINGS: Record<string, string> = {
  shutdown: 'Windows may close open apps without saving.',
}

const clip = (value: string, max: number): string => (value.length <= max ? value : `${value.slice(0, max - 1)}…`)
const capitalize = (text: string): string => text.charAt(0).toUpperCase() + text.slice(1)

/** The text a guarded call runs or writes: its command, script or path. */
function commandText(call: Readonly<Record<string, unknown>>): string {
  return commandField(call)?.value ?? ''
}

function commandField(call: Readonly<Record<string, unknown>>): { field: string; value: string } | undefined {
  for (const field of ['command', 'script', 'file_path', 'notebook_path']) {
    const value = call[field]
    if (typeof value === 'string') return { field, value }
  }
  return undefined
}

/**
 * A command as a spoken yes is bound to it: spacing within a line aside, but
 * every line break kept, so "git push origin npm publish" (one command) never
 * answers for "git push origin" and "npm publish" (two).
 */
export const holdKey = (text: string): string => textLines(text).join('\n')

/**
 * What the guard read to judge a call (a script file's text, where a file
 * tool's path lands), as its hold key carries it: a yes for running a script
 * is for what the script held when Jarvis asked, not for whatever it holds
 * later. Empty when nothing was read. Only ever compared, never logged.
 */
function readsKey(resolved: Resolved | undefined): string {
  const scripts = Object.entries(resolved?.scripts ?? {}).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
  if (resolved === undefined || (resolved.realPath === undefined && scripts.length === 0)) return ''
  return `\n${JSON.stringify([resolved.realPath ?? null, scripts])}`
}

/** "it deletes files", or the reason alone where it is not a verb phrase ("unreadable Bash input"). */
const itDoes = (verdict: Verdict): string => (verdict.reason.startsWith('unreadable ') ? verdict.reason : `it ${verdict.reason}`)

const isFileTool = (tool: string): boolean => tool === 'Write' || tool === 'Edit' || tool === 'NotebookEdit'

/** "run a Bash command that deletes files", "write a file whose path Jarvis could not read". */
function callWords(call: GuardCall, verdict: Verdict): string {
  const tool = String(call.tool)
  const what = TOOL_WORDS[tool] ?? `use ${tool}`
  if (!verdict.reason.startsWith('unreadable ')) return `${what} that ${verdict.reason}`
  return `${what} ${isFileTool(tool) ? 'whose path Jarvis could not read' : 'Jarvis could not read'}`
}

/**
 * The parts of a long command that set its tier: each line, else each piece
 * of one between `;`, `&&`, `||` and `|`, that the guard puts in the same tier
 * for the same rule on its own (short ones only), with the same files read as
 * for the whole (so the part that runs a script is found by what the script
 * does). None when no short part does.
 */
function decidingParts(call: GuardCall, verdict: Verdict, resolved: Resolved | undefined): string[] {
  const found = commandField(call)
  if (found === undefined || isFileTool(String(call.tool))) return []
  const sameTier = (part: string): boolean => {
    const alone = judge(String(call.tool), { ...call, [found.field]: part }, resolved)
    return alone.tier === verdict.tier && alone.rule === verdict.rule
  }
  const parts: string[] = []
  for (const line of textLines(found.value).filter(one => one !== '')) {
    if (!sameTier(line)) continue
    if (line.length <= PART_MAX) parts.push(line)
    else {
      const pieces = line.split(/;|&&|\|\||\|/).map(one => one.trim())
      parts.push(...pieces.filter(one => one !== '' && one.length <= PART_MAX && sameTier(one)))
    }
  }
  return parts
}

/**
 * The command as the question shows it: whole, its line breaks visible; a
 * long one by the parts that set its tier, as many as fit (else its start),
 * with how much is not shown.
 */
function shownCommand(call: GuardCall, verdict: Verdict, resolved: Resolved | undefined): { text: string; parts: number; hidden: number } {
  const whole = visibleText(commandText(call))
  if (whole.length <= SHOWN_MAX) return { text: whole, parts: 0, hidden: 0 }
  const shown: string[] = []
  let length = 0
  for (const part of decidingParts(call, verdict, resolved)) {
    if (length + part.length > SHOWN_MAX) break
    shown.push(part)
    length += part.length
  }
  if (shown.length > 0) return { text: shown.join('" … "'), parts: shown.length, hidden: whole.length - length }
  const head = whole.slice(0, SHOWN_MAX)
  return { text: `${head}…`, parts: 0, hidden: whole.length - head.length }
}

/**
 * The command quoted after what the call does, for the question, the toast and
 * the transcript alike: ': "git push"'; a long one ', in this part: "…" (N more
 * characters not shown)'. Empty when the call has no text.
 */
function quotedCommand(call: GuardCall, verdict: Verdict, resolved: Resolved | undefined): string {
  const shown = shownCommand(call, verdict, resolved)
  if (shown.text === '') return ''
  if (shown.hidden === 0) return `: "${shown.text}"`
  const where = shown.parts === 0 ? '' : shown.parts === 1 ? ', in this part' : ', in these parts'
  return `${where}: "${shown.text}" (${shown.hidden.toLocaleString('en-US')} more characters not shown)`
}

/** The on-screen question for a guarded call: what it does, and the command itself (`quoted`: a long one by the parts that matter). */
function guardQuestion(call: GuardCall, verdict: Verdict, quoted: string): ScreenQuestion {
  const isFile = isFileTool(String(call.tool))
  const warning = WARNINGS[verdict.rule]
  return {
    question: `${LEAD}Claude wants to ${callWords(call, verdict)}${quoted}.${warning === undefined ? '' : ` ${warning}`} ${isFile ? 'Go ahead?' : 'Run it?'}`,
    no: NO_RUN,
    yes: YES_RUN,
    nothing: 'nothing ran',
  }
}

/** A few words of a command, safe to say aloud: "git push origin and more". */
function spokenCommand(text: string): string {
  const lines = textLines(text).filter(line => line !== '')
  const first = lines[0] ?? ''
  const words = first.replace(/[`*_#|<>[\]{}\\"]/g, ' ').split(' ').filter(word => word !== '')
  let said = words.slice(0, SPOKEN_WORDS).join(' ')
  if (said.length > SPOKEN_MAX) said = said.slice(0, SPOKEN_MAX).trim()
  return said === first && lines.length === 1 ? said : `${said} and more`
}

/**
 * What a held guarded call would do, for the toast and the transcript, the
 * command quoted as the question quotes it: 'run a Bash command: "git push"';
 * a long one by the parts that set its tier, so what the yes lets run is in view.
 */
function shownForYes(call: GuardCall, quoted: string): string {
  const tool = String(call.tool)
  return `${TOOL_WORDS[tool] ?? `use ${tool}`}${quoted}`
}

const ABORTED = Symbol('aborted')

/** `promise`, or ABORTED as soon as `signal` aborts (what it would settle to is then dropped). */
function unlessAborted<T>(promise: Promise<T>, signal: AbortSignal | undefined): Promise<T | typeof ABORTED> {
  if (signal === undefined) return promise
  return new Promise((resolve, reject) => {
    const onAbort = (): void => resolve(ABORTED)
    if (signal.aborted) onAbort()
    else signal.addEventListener('abort', onAbort, { once: true })
    promise.then(
      value => {
        signal.removeEventListener('abort', onAbort)
        resolve(value)
      },
      (error: unknown) => {
        signal.removeEventListener('abort', onAbort)
        reject(error)
      },
    )
  })
}

/** How a dialog of Jarvis's ended other than by the person's answer, as its own record says. */
type DialogEnd = 'idle' | 'follow_up'

/**
 * Undefined on a yes; else what the model is told. Only a person's click or
 * typed yes is a yes: an idle auto-resolve is nobody's answer, whatever it picked.
 */
function readAnswer(q: ScreenQuestion, answer: string, end: DialogEnd | undefined): string | undefined {
  if (end === 'idle') return `Jarvis's on-screen question closed while the user was away, so ${q.nothing}. Ask again when they are back.`
  if (end === 'follow_up') return `The user wants to talk it over first, so ${q.nothing}. Ask them what they want.`
  if (answer.trim() === '') return `Jarvis could not ask the user on screen, so ${q.nothing}.`
  if (answer === q.yes) return undefined
  if (answer === q.no) return `The user chose "${q.no}" on Jarvis's on-screen question, so ${q.nothing}. Do not retry unless they ask.`
  // Words in another script beside a yes ("ok, לא עכשיו": "ok, not now") make it no yes.
  const words = phraseWords(answer)
  if (!hasNonLatinLetters(answer) && (TYPED_YES.has(words) || words === phraseWords(q.yes))) return undefined
  return `The user did not confirm; they wrote: "${clip(answer.trim(), 200)}". ${capitalize(q.nothing)}.`
}

export class PcControl {
  /** The desktop tool's calls and timers (desktop.ts). */
  readonly desktopTool: DesktopTool
  /**
   * The main loop's permission mode, as the latest classic hook input said
   * (a prompt, a session start, a plan-mode tool). Undefined until one has:
   * the desktop and hands tools then change nothing, as in plan mode.
   */
  permissionMode: string | undefined
  /** The main-loop turn running now, and the turn the mode was last noted in (a prompt's own turn, or a tool's). */
  private runningTurnId: string | undefined
  private modeTurnId: string | undefined
  /**
   * The prompt that noted the mode while no turn ran, until the next turn
   * starts: the mode is that turn's only when the turn starts with this text.
   */
  private modePrompt: string | undefined

  /**
   * The one command held for a spoken OK: when, in which turn (the yes must
   * come in the next one), and what Jarvis says and shows about it. Void once
   * a turn held two different commands.
   */
  private held: { key: string; at: number; turnId: string; spoken: string; shown: string; isVoid?: boolean } | undefined
  /**
   * Jarvis's own on-screen questions open now, and every question dialog
   * (another plugin's, Claude's own): while one is, a spoken yes or no counts for nothing.
   */
  private awaitingClick = 0
  private openDialogs = 0
  /** Jarvis's questions that ended without an answer (`$.ui.ask` gives only the label), by question. */
  private readonly dialogEnds = new Map<string, DialogEnd>()
  /** The texts of Jarvis's questions open now: no two at once are the same, so a record is its own call's. */
  private readonly openQuestions = new Set<string>()
  /** Background commands and monitors the guard saw start, by task id, for stand down. */
  private readonly tasks = new Map<string, string>()
  /** The files Claude wrote or edited this session (as the tools named them), oldest first: a command naming one has it read and judged. */
  private readonly written = new Map<string, true>()
  /** The user's home folder, for `~` and `$HOME` in a path the guard reads: looked up once (null: none). */
  private home: string | null | undefined
  private admin: AdminFacts | undefined
  /** The administrator check: running, done, or failed (Jarvis then stays off until /jarvis restart checks again). */
  private adminCheck: 'pending' | 'done' | 'failed' = 'pending'
  private adminFailure: string | undefined
  private hasWarnedBypass = false

  constructor(private readonly app: Jarvis) {
    this.desktopTool = new DesktopTool(app, this)
  }

  get isAwaitingClick(): boolean {
    return this.awaitingClick > 0 || this.openDialogs > 0
  }

  /** An AskUserQuestion dialog opened (anyone's: home's, a skill's, Claude's own); `dialogClosed` when it ends. */
  dialogOpened(): void {
    this.openDialogs += 1
  }

  dialogClosed(): void {
    this.openDialogs = Math.max(0, this.openDialogs - 1)
  }

  /** Claude Code runs elevated (known once the session started). */
  get isElevated(): boolean {
    return this.admin?.isElevated === true
  }

  /**
   * Whether Jarvis's helpers must stay off: Claude Code runs elevated, or the
   * check has not ended yet, or it failed. Anything of Jarvis's that starts a
   * process (a helper, a setup) checks it first.
   */
  get holdsHelper(): boolean {
    return this.adminCheck !== 'done' || this.isElevated
  }

  /** The administrator check failed: Jarvis stays off until /jarvis restart checks again. */
  get hasAdminCheckFailed(): boolean {
    return this.adminCheck === 'failed'
  }

  /** Why nothing of Jarvis's may start now (elevated, not known yet, or the check failed); undefined when it may. */
  whyHeld(): string | undefined {
    if (this.isElevated) return ELEVATED_LOG
    if (this.adminCheck === 'failed') return adminFailedLog(this.adminFailure ?? 'no answer')
    return this.adminCheck === 'pending' ? ADMIN_PENDING : undefined
  }

  /** True (and says why) when Claude Code runs elevated, or the check has not ended or failed: nothing of Jarvis's may start then. */
  refusesElevated(): boolean {
    const why = this.whyHeld()
    if (why === undefined) return false
    this.app.engine?.log(why)
    return true
  }

  /**
   * The guard (a `tool.call` hook on the shells, Monitor and the file tools):
   * undefined lets the call go on to the engine unchanged, a string is the
   * deny. `ask` raises the engine's question dialog through the hook's own
   * `$`; `signal` is the call's own (an abort while asking is a no).
   */
  async guard(call: GuardCall, ask: CallAsk, signal?: AbortSignal): Promise<string | undefined> {
    const refused = await this.decide(call, ask, signal)
    // The HUD's own hook sits beneath the guard: a refused call never reaches it.
    if (refused !== undefined) this.logAction(call, false)
    else this.noteWritten(call)
    return refused
  }

  /** A file Claude is about to write or edit: a later command that names it has it read and judged (it may be a script). */
  private noteWritten(call: GuardCall): void {
    if (!isFileTool(String(call.tool))) return
    const path = call.tool === 'NotebookEdit' ? call.notebook_path : call.file_path
    if (typeof path !== 'string' || path === '') return
    this.written.delete(path)
    this.written.set(path, true)
    for (const oldest of this.written.keys()) {
      if (this.written.size <= WRITTEN_MAX) break
      this.written.delete(oldest)
    }
  }

  private async decide(call: GuardCall, ask: CallAsk, signal: AbortSignal | undefined): Promise<string | undefined> {
    // A cloud session runs nothing on the user's PC (and its platform is set before isLocal).
    if (this.app.platform !== undefined && !this.app.isLocal) return undefined
    const { verdict, resolved } = await this.classify(call)
    switch (verdict.tier) {
      case 'pass':
        return undefined
      case 'never':
        return `Jarvis blocks this: it ${verdict.reason}, which is on Jarvis's never list. Do not retry it or work around it; the user can do it themselves.`
      case 'voice': {
        const command = commandText(call)
        const quoted = quotedCommand(call, verdict, resolved)
        return await this.confirm(
          {
            key: `${String(call.tool)}\n${holdKey(command)}${readsKey(resolved)}`,
            reason: verdict.reason,
            again: 'run exactly the same command again',
            spoken: `Claude wants to ${callWords(call, verdict)}: ${spokenCommand(command)}. Say yes to ${isFileTool(String(call.tool)) ? 'go ahead' : 'run it'}, sir.`,
            shown: shownForYes(call, quoted),
            screen: guardQuestion(call, verdict, quoted),
            agentId: call.agentId,
          },
          ask,
          signal,
        )
      }
      default:
        return await this.askOnScreen(guardQuestion(call, verdict, quotedCommand(call, verdict, resolved)), ask, signal, call.agentId)
    }
  }

  /**
   * The guard's judgement. judge() is pure, so a `never` stands on its own, but a file a command runs
   * (or a file Claude wrote that it names) and every Write/Edit/NotebookEdit path leave a read for
   * Jarvis: reads it (through the engine's file system, never the real clipboard or keys) and judges
   * again, in rounds, since a script can run another. A read that fails, or one still wanted after the
   * last round, leaves the call at screen. What was read comes back too: the question and a spoken yes
   * are for those contents.
   */
  private async classify(call: GuardCall): Promise<{ verdict: Verdict; resolved?: Resolved }> {
    const tool = String(call.tool)
    const engine = this.app.engine
    const known: Resolved = { written: [...this.written.keys()] }
    let verdict = judge(tool, call, known)
    if (verdict.tier === 'never' || verdict.needs === undefined || verdict.needs.length === 0) {
      // Without the home folder `~` and `$HOME` cannot be read; with it the call may need a read after all.
      if (verdict.tier !== 'screen' || engine === undefined) return { verdict }
    }
    const home = await this.homeFolder()
    const scripts: Record<string, string | null | false> = {}
    let realPath: string | null | undefined
    const resolvedNow = (): Resolved => ({
      ...known,
      ...(home === undefined ? {} : { home }),
      scripts: { ...scripts },
      ...(realPath === undefined ? {} : { realPath }),
    })
    verdict = judge(tool, call, resolvedNow())
    let reads = 0
    for (let round = 0; verdict.tier !== 'never' && verdict.needs !== undefined && verdict.needs.length > 0; round++) {
      const isLast = round >= READ_ROUNDS || engine === undefined
      const pending: Promise<void>[] = []
      for (const need of verdict.needs) {
        if (need.kind === 'realpath') {
          if (realPath !== undefined) continue
          realPath = null
          if (!isLast && engine !== undefined) {
            pending.push(engine.realPath(need.path).then(real => void (realPath = real), () => undefined))
          }
          continue
        }
        if (need.path in scripts) continue
        scripts[need.path] = null
        if (isLast || reads >= READS_MAX) continue
        reads++
        const path = need.path
        pending.push(this.readForGuard(path, need.optional === true).then(text => void (scripts[path] = text)))
      }
      await Promise.all(pending)
      verdict = judge(tool, call, resolvedNow())
      if (isLast) break
    }
    return { verdict, resolved: resolvedNow() }
  }

  /** A file the guard judges: its text; false when an optional one (a name a shell looks up, a file deleted since) is not there; null when it cannot be read. */
  private async readForGuard(path: string, isOptional: boolean): Promise<string | null | false> {
    const engine = this.app.engine
    if (engine === undefined) return null
    const text = await engine.readFileText(path).catch(() => null)
    if (text !== null || !isOptional) return text
    return (await engine.exists(path).catch(() => true)) ? null : false
  }

  /** The user's home folder (USERPROFILE on Windows, else HOME), looked up once. */
  private async homeFolder(): Promise<string | undefined> {
    if (this.home === undefined) {
      const env = await this.app.engine?.env().catch(() => undefined)
      this.home = env?.USERPROFILE ?? env?.HOME ?? null
    }
    return this.home ?? undefined
  }

  /**
   * The voice tier. In the running voice turn (main loop): the call is held;
   * when the turn ends, Jarvis speaks his own question about it (consent.spoken)
   * after the model's words, and a toast quotes it as the question would. The
   * next main-loop turn lets exactly that call through once if its words are
   * only a yes the user began saying after that question had played out, by
   * the helper's own clock: a yes said over Jarvis holds it again; one said
   * before he finished asking (a queued one, say), or one the helper did not
   * time, goes to a click on screen instead. Any turn in between, an abort, a
   * stop, any other words, a typed prompt or a new session drops the hold. One
   * call is held at a time: a later turn's hold replaces it, and a second
   * command held in the same turn voids both (the yes would answer two
   * questions). Any other turn or loop asks on screen.
   */
  async confirm(consent: Consent, ask: CallAsk, signal?: AbortSignal): Promise<string | undefined> {
    const engine = this.app.engine
    const turnId = this.voiceTurnOf(consent.agentId)
    if (engine === undefined || turnId === undefined) return await this.askOnScreen(consent.screen, ask, signal, consent.agentId)
    const now = await engine.now()
    const held = this.held !== undefined && now - this.held.at <= CONSENT_MS ? this.held : undefined
    const voice = this.app.voice
    if (voice !== undefined && held !== undefined && held.isVoid !== true && held.key === consent.key && held.turnId === voice.previousTurn) {
      const said = voice.consentTo(held.turnId)
      if (said === 'yes') {
        this.held = undefined
        engine.debug(`jarvis: spoken OK to ${clip(consent.shown, 120)}`)
        return undefined
      }
      if (said === 'yes_while_talking') {
        this.held = { key: consent.key, at: now, turnId, spoken: consent.spoken, shown: consent.shown }
        return HEARD_WHILE_TALKING
      }
      if (said !== undefined) {
        // Begun before Jarvis had finished asking, or not timed: a click decides.
        this.held = undefined
        engine.debug(`jarvis: a spoken yes that ${said === 'yes_untimed' ? 'the helper did not time' : 'began before the question ended'}, so the question goes on screen`)
        return await this.askOnScreen(consent.screen, ask, signal, consent.agentId)
      }
    }
    if (held !== undefined && held.turnId === turnId && (held.isVoid === true || held.key !== consent.key)) {
      this.held = { ...held, at: now, isVoid: true }
      return `Jarvis did not hold this: it ${consent.reason}, and this turn already held another command. One spoken yes answers one question, so neither is held now. Tell the user in one short sentence that you will ask about them one at a time, and end your turn.`
    }
    this.held = { key: consent.key, at: now, turnId, spoken: consent.spoken, shown: consent.shown }
    return `Jarvis held this: it ${consent.reason} and needs the user's spoken OK. Say in one short sentence what it will do, then end your turn without calling another tool: Jarvis then reads it out and asks the user himself. If they answer yes, ${consent.again}; anything else, drop it.`
  }

  /** Drops the command held for a spoken OK: nothing a yes says later lets it run. */
  clearHold(): void {
    this.held = undefined
  }

  /**
   * Every utterance with words (voice.ts), as it arrives: anything but a bare
   * yes going on as a prompt (a stop, other words, an answer dropped while a
   * question waits on screen) drops the hold.
   */
  noteHeard(text: string, isSubmitted: boolean): void {
    if (!(isSubmitted && isYesPhrase(text))) this.clearHold()
  }

  /** prompt.submit: any prompt but Jarvis's own (one typed, another plugin's) drops the hold. */
  onPrompt(origin: { kind: string; name?: string }): void {
    if (origin.kind === 'plugin' && origin.name === PLUGIN_NAME) return
    this.clearHold()
  }

  /**
   * turn.start in the main loop: the turn a prompt's mode belongs to, when it
   * is that prompt's own (it starts with the prompt's text). Any other turn (a
   * continuation, one after a prompt a hook blocked) starts with its mode unknown.
   */
  onTurnStart(e: { turnId: string; text: string }): void {
    this.runningTurnId = e.turnId
    if (this.modePrompt !== undefined && e.text !== '' && e.text === this.modePrompt) this.modeTurnId = e.turnId
    this.modePrompt = undefined
  }

  /**
   * turn.complete in the main loop. An abort drops the hold; the turn that
   * held a command ends with Jarvis's own question about it, returned for
   * voice.ts to speak last in its reply, and with a toast and a transcript
   * line that quote the command as the question does (a long one by the parts
   * that set its tier, with how much is not shown): what the yes lets run.
   */
  onTurnComplete(e: { turnId: string; isAborted: boolean; agentId?: string }): string | undefined {
    if (e.agentId !== undefined) return undefined
    if (this.runningTurnId === e.turnId) this.runningTurnId = undefined
    if (e.isAborted) {
      this.clearHold()
      return undefined
    }
    const held = this.held
    if (held === undefined || held.isVoid === true || held.turnId !== e.turnId) return undefined
    const engine = this.app.engine
    engine?.log(`Jarvis is waiting for a spoken yes to ${held.shown}`)
    engine?.toast(`Jarvis: say yes to ${held.shown}`, { timeoutMs: 15_000 })
    return held.spoken
  }

  /**
   * Whether the main loop's permission mode is known for the call's turn: a
   * prompt noted it for this turn, or a tool's PostToolUse did during it. A
   * subagent's own mode is never known (it may run in plan mode of its own).
   */
  isModeKnownForTurn(agentId: string | undefined): boolean {
    return agentId === undefined && this.runningTurnId !== undefined && this.modeTurnId === this.runningTurnId
  }

  /**
   * Asks on screen through the engine's own question dialog, "no" first: the
   * guard puts no timeout on it (it fails closed by waiting), and an idle
   * auto-resolve is a no. Each call waits inside its own `$.ui.ask`, whose
   * time the hook budget does not count; questions open together get texts
   * of their own, so a dialog's record is read for its own call. Undefined on
   * a yes; else what the model is told.
   */
  async askOnScreen(q: ScreenQuestion, ask: CallAsk, signal: AbortSignal | undefined, agentId: string | undefined): Promise<string | undefined> {
    const interrupted = `The call was interrupted before the user answered, so ${q.nothing}.`
    if (signal?.aborted === true) return interrupted
    if (this.voiceTurnOf(agentId) !== undefined) this.app.voice?.say(ON_SCREEN)
    let question = q.question
    for (let n = 2; this.openQuestions.has(question); n += 1) question = `${q.question} (${n})`
    // Taken until the dialog closes, even when the call is abandoned first.
    let isTaken = true
    const free = (): void => {
      if (isTaken) this.openQuestions.delete(question)
      isTaken = false
    }
    this.openQuestions.add(question)
    this.dialogEnds.delete(question)
    this.awaitingClick += 1
    let answer: string | typeof ABORTED
    try {
      const asked = ask(question, { options: [q.no, q.yes], header: HEADER })
      asked.then(free, free)
      answer = await unlessAborted(asked, signal)
    } catch (error) {
      // Dismissed, a -p run or a background agent (no one to ask), or no dialog at all.
      this.app.engine?.debug(`jarvis: the on-screen question was not answered: ${describeError(error)}`)
      free()
      return `Jarvis could not ask the user on screen, so ${q.nothing}.`
    } finally {
      this.awaitingClick -= 1
    }
    if (answer === ABORTED) return interrupted
    const end = this.dialogEnds.get(question)
    this.dialogEnds.delete(question)
    return readAnswer(q, answer, end)
  }

  /**
   * Every AskUserQuestion call, after the dialog closed (a hook that changes
   * nothing): for Jarvis's own questions, notes an idle auto-resolve or a
   * request to talk it over, which the answer's label alone does not show.
   */
  noteDialog(questions: readonly { question: string; header?: string }[], outcome: { result?: unknown }): void {
    const [first] = questions
    if (questions.length !== 1 || first === undefined || first.header !== HEADER || !first.question.startsWith(LEAD)) return
    const result = outcome.result
    if (typeof result !== 'object' || result === null) return
    const { afkTimeoutMs, followUp } = result as Record<string, unknown>
    if (afkTimeoutMs !== undefined) this.dialogEnds.set(first.question, 'idle')
    else if (followUp === true) this.dialogEnds.set(first.question, 'follow_up')
  }

  /** After a guarded call ran: a command or monitor left running is remembered for stand down. */
  noteRan(call: GuardCall, outcome: { result?: unknown }): void {
    const result = outcome.result
    if (typeof result !== 'object' || result === null) return
    const fields = result as Record<string, unknown>
    const id = call.tool === 'Monitor' ? fields.taskId : fields.backgroundTaskId
    if (typeof id !== 'string' || id === '') return
    this.tasks.delete(id)
    this.tasks.set(id, clip(commandText(call), 80))
    for (const oldest of this.tasks.keys()) {
      if (this.tasks.size <= TASKS_MAX) break
      this.tasks.delete(oldest)
    }
  }

  /**
   * Stand down ("stand down", "abort", /jarvis pc stop): stops speech, aborts
   * the running turn (typed ones too), then stops every background command
   * Jarvis saw start: a plugin's turn abort only moves a running command to
   * the background.
   */
  async standDown(): Promise<string> {
    const { engine, voice } = this.app
    if (engine === undefined) return 'Jarvis is still starting; nothing to stand down.'
    const turnId = voice?.runningTurn
    const isVoiceTurn = turnId !== undefined && voice?.voiceTurnId === turnId
    const interrupted = await voice?.interrupt('stand_down')
    let abortedTurn = interrupted?.abortedTurn === true
    if (turnId !== undefined && !isVoiceTurn) {
      try {
        await engine.abortTurn(turnId)
        abortedTurn = true
      } catch (error) {
        engine.debug(`jarvis: turn.abort failed: ${describeError(error)}`)
      }
    }
    this.held = undefined
    if (abortedTurn) await delay(engine, STAND_DOWN_WAIT_MS)
    let stopped = 0
    const tasks = [...this.tasks.keys()]
    this.tasks.clear()
    for (const id of tasks) {
      try {
        const answer = (await engine.stopTask(id)) as { deny?: unknown; isError?: unknown } | undefined
        if (answer?.deny === undefined && answer?.isError !== true) stopped += 1
      } catch (error) {
        engine.debug(`jarvis: TaskStop ${id} failed: ${describeError(error)}`)
      }
    }
    const parts = [
      interrupted === undefined || interrupted.speech === 'not_running' ? 'no voice to stop' : 'stopped speech',
      abortedTurn ? 'aborted the turn' : 'no turn was running',
      `stopped ${stopped} background command${stopped === 1 ? '' : 's'}`,
    ]
    engine.log(`Jarvis: stood down (${parts.join(', ')}).`)
    engine.toast('Jarvis stood down.')
    return `Stood down: ${parts.join(', ')}.`
  }

  /**
   * At session start (local sessions): whether Claude Code runs elevated, and
   * the UAC level. True when Jarvis stays off (PLAN.md:150): elevated, or the
   * check failed (it fails closed: /jarvis restart checks again).
   */
  async onSessionStart(): Promise<boolean> {
    this.clearHold()
    return await this.checkAdmin()
  }

  /** /jarvis restart after a failed check: checks again; true while Jarvis must still stay off. */
  async recheckAdmin(): Promise<boolean> {
    if (this.adminCheck === 'pending') return true
    return await this.checkAdmin()
  }

  private async checkAdmin(): Promise<boolean> {
    const { engine, platform } = this.app
    this.adminCheck = 'pending'
    this.adminFailure = undefined
    this.admin = undefined
    try {
      if (engine === undefined || platform === undefined) throw new Error('Jarvis was not started')
      this.admin = await probeAdmin(engine, platform)
      this.adminCheck = 'done'
    } catch (error) {
      this.adminCheck = 'failed'
      this.adminFailure = clip(describeError(error), 160)
      await this.app.helper?.stop()
      await this.app.hands?.helper.stop()
      this.app.patch({ phase: 'error', detail: ADMIN_FAILED_DETAIL })
      engine?.log(adminFailedLog(this.adminFailure))
      engine?.toast('Jarvis stays off: it could not check whether Claude Code runs as administrator.', { timeoutMs: 10_000 })
      return true
    }
    if (this.admin.isElevated) {
      // Nothing started during the check (holdsHelper); stopping is a second line.
      await this.app.helper?.stop()
      await this.app.hands?.helper.stop()
      this.app.patch({ phase: 'error', detail: ELEVATED_DETAIL })
      engine.log(ELEVATED_LOG)
      engine.toast('Jarvis stays off: Claude Code runs as administrator.', { timeoutMs: 10_000 })
      return true
    }
    const level = this.admin.uacLevel
    if (level !== undefined && WEAK_UAC.has(level) && (await engine.storeGet(UAC_HINT_KEY).catch(() => undefined)) !== true) {
      await engine.storeSet(UAC_HINT_KEY, true).catch(() => undefined)
      engine.toast(UAC_HINT, { timeoutMs: 10_000 })
    }
    return false
  }

  /** classic.SessionStart: notes the mode; the bypass warning, the first time only. */
  onPermissionMode(mode: string | undefined): string | undefined {
    // The session's mode, not a turn's: a turn that starts with no prompt still has its mode unknown.
    if (mode !== undefined && mode !== '') this.permissionMode = mode
    if (mode !== 'bypassPermissions' || this.hasWarnedBypass) return undefined
    this.hasWarnedBypass = true
    return BYPASS_WARNING
  }

  /**
   * classic.UserPromptSubmit: the mode the prompt's turn runs in. A subagent's
   * or teammate's is not the main loop's. A prompt typed while a turn runs
   * fires this at Enter and waits (or goes into that turn): the mode may change
   * before its own turn starts, so that turn starts with its mode unknown.
   */
  notePermissionMode(mode: string | undefined, prompt: string, agentId?: string): void {
    if (agentId !== undefined) return
    // No earlier prompt's mode is this one's.
    this.modePrompt = undefined
    if (mode === undefined || mode === '') return
    this.permissionMode = mode
    if (this.runningTurnId === undefined) this.modePrompt = prompt
  }

  /**
   * classic.PostToolUse in the main loop: the mode now, which Shift+Tab may
   * have changed mid-turn. EnterPlanMode and ExitPlanMode report the mode
   * they were called in, so they set it themselves.
   */
  noteToolUse(tool: string, mode: string | undefined, agentId?: string): void {
    if (agentId !== undefined) return
    if (tool === 'EnterPlanMode') this.permissionMode = 'plan'
    else if (tool === 'ExitPlanMode') this.permissionMode = mode !== undefined && mode !== '' && mode !== 'plan' ? mode : 'default'
    else if (mode !== undefined && mode !== '') this.permissionMode = mode
    else return
    this.modeTurnId = this.runningTurnId
  }

  /** The desktop tool's call (its hook's closures over its own `$`), on the HUD's log as the HUD's own hook would put it. */
  async desktop(e: Record<string, unknown>, ports: AskPorts, signal?: AbortSignal): Promise<{ result: string } | { deny: string }> {
    return await logOwnTool(this.app.hud, actionLabel({ ...e, tool: String(e.tool) }), () => this.desktopTool.call(e, ports, signal))
  }

  /** `/jarvis pc [check <command>|rules|stop]`, the words after `pc`. */
  async command(args: readonly string[]): Promise<string> {
    const [sub = '', ...rest] = args
    switch (sub.toLowerCase()) {
      case '':
      case 'status':
        return `${this.status()}\n\n${PC_HELP}`
      case 'check':
        return this.check(rest.join(' '))
      case 'rules':
        return [
          'Permission rules from the same table the guard uses. Jarvis writes no settings: paste these into the "permissions" (and "env") of your user settings yourself (.claude\\settings.json in your user folder). Claude cannot do it for you: the guard blocks edits to Claude Code\'s settings.',
          'The deny rules keep the never list blocked even without Jarvis. There are no allow rules (Jarvis only makes Claude Code stricter) and no ask rules (Jarvis asks itself; an ask rule would add a second dialog). The env line turns on the PowerShell tool.',
          `The desktop tool asks on screen before each action unless your rules allow it. To let its actions run without that question, add "${DESKTOP_TOOL}" to "allow" yourself. Even then Jarvis asks before reading the clipboard or taking a screenshot (a spoken yes in a voice conversation, else a click), and asks on screen when: a PreToolUse or PermissionRequest hook in your settings could match the tool (Claude Code runs none of your settings hooks for it, PostToolUse ones included), the permission mode is not known for the turn (a subagent's call, a turn Jarvis did not see a prompt start, or one whose prompt was typed while another turn ran), or the allow comes from the mode alone (bypassPermissions) rather than your rule. If your rules or settings cannot be read, nothing is done.`,
          `The hands tool (hand control) follows your rules the same way: it asks before each action (a spoken yes in a voice conversation, else a click) unless you add "${HANDS_TOOL_ID}" to "allow" yourself, and in plan mode it only reads hand control's status.`,
          `The home control tool follows them too: it asks on screen before each change to a device, and before opening its setup window, unless you add "${HOME_TOOL}" to "allow" yourself (a device that needs your OK still asks, never by voice). Listing devices, reading a device's state and searching the network run without that question unless an ask rule or a settings hook could cover the tool; a deny rule, or dontAsk without your allow rule, refuses them too.`,
          '',
          rulesSnippet(),
        ].join('\n')
      case 'stop':
        return await this.standDown()
      case 'help':
        return PC_HELP
      default:
        return `Unknown pc subcommand "${sub}".\n\n${PC_HELP}`
    }
  }

  private check(command: string): string {
    if (command.trim() === '') return 'Which command? For example /jarvis pc check Remove-Item -Recurse C:\\temp\\old'
    const line = (shell: 'PowerShell' | 'Bash'): string => {
      const verdict = judge(shell, { command })
      const tiers: Record<Verdict['tier'], string> = {
        pass: 'runs without a question from Jarvis (Claude Code\'s own rules still apply)',
        voice: `asks for a spoken yes in a voice conversation, else a click: ${itDoes(verdict)}`,
        screen: `asks for a click on screen: ${itDoes(verdict)}`,
        never: `blocked: ${itDoes(verdict)}`,
      }
      return `${shell}: ${verdict.tier} · ${tiers[verdict.tier]}${verdict.tier === 'pass' ? '' : ` (rule ${verdict.rule})`}`
    }
    return [line('PowerShell'), line('Bash')].join('\n')
  }

  private status(): string {
    const { platform, isLocal, helper } = this.app
    if (platform !== undefined && !isLocal) return 'PC control is off: this session runs in the cloud, not on your PC.'
    const lines = ['Guard: on for PowerShell, Bash, Monitor and edits to Claude Code\'s settings and Jarvis\'s secrets.']
    const admin = this.admin
    if (this.adminCheck === 'failed') {
      lines.push(`Administrator: check failed (${this.adminFailure ?? 'no answer'}), so Jarvis stays off. /jarvis restart checks again.`)
    } else if (admin === undefined) lines.push('Administrator: still checking (it runs when the session starts).')
    else if (admin.isElevated) lines.push(`Administrator: ${ELEVATED_LOG.replace(/^Jarvis stays off: /, '')}`)
    else lines.push('Administrator: Claude Code runs with normal rights.')
    if (admin?.uacLevel !== undefined) {
      const weak = WEAK_UAC.has(admin.uacLevel) ? ' For PC control, "Always notify" or a standard account is safer.' : ''
      lines.push(`UAC: ${UAC_NAMES[admin.uacLevel]}.${weak}`)
    }
    if (platform?.os !== 'windows') lines.push('Desktop tool: Windows only.')
    else if (helper?.isRunning !== true) lines.push('Desktop tool: offered; its actions wait for the voice helper (/jarvis starts it).')
    else if (!(helper.hello?.capabilities ?? []).some(capability => capability.startsWith('desktop.'))) {
      lines.push('Desktop tool: offered, but this helper has no desktop actions; run /jarvis setup to update it.')
    } else lines.push('Desktop tool: ready.')
    const mode = this.permissionMode
    lines.push(
      mode === undefined
        ? 'Permission mode: not known yet (the desktop, hands and home control tools change nothing until the next prompt).'
        : `Permission mode: ${mode}.${mode === 'bypassPermissions' ? ' Claude Code\'s own rules are skipped; the guard still asks.' : ''}`,
    )
    const timers = this.desktopTool.timerLabels
    lines.push(
      `Stand down would stop ${this.tasks.size} background command${this.tasks.size === 1 ? '' : 's'}. Waiting for a spoken OK: ${this.held === undefined || this.held.isVoid === true ? 'nothing' : 'one command'}. Timers: ${timers.length}.`,
    )
    return lines.join('\n')
  }

  /** One finished line on the HUD's action log. */
  private logAction(call: GuardCall, isOk: boolean): void {
    const hud = this.app.hud
    hud?.onToolEnd(hud.onToolStart(actionLabel(call)), isOk)
  }

  /** The voice turn's id when this call is part of it (the main loop, while that turn runs). */
  private voiceTurnOf(agentId: string | undefined): string | undefined {
    const voice = this.app.voice
    const turnId = voice?.voiceTurnId
    return agentId === undefined && turnId !== undefined && turnId === voice?.runningTurn ? turnId : undefined
  }
}
