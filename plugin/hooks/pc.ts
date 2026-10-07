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
import type { Verdict } from './guard'
import { judge, rulesSnippet } from './guard'
import { DESKTOP_TOOL, DesktopTool } from './desktop'
import { actionLabel } from './hud'
import type { AdminFacts, UacLevel } from './platform'
import { probeAdmin } from './platform'
import { phraseWords } from './voice'

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
/** The desktop tool's hook closures over its own `$`. */
export type AskPorts = { ask: CallAsk; check: (input: Record<string, unknown>) => Promise<RuleVerdict> }
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
  /** What a spoken yes is for: the tool and the exact command, or "desktop:clipboard_read". */
  key: string
  /** A plain phrase: "pushes commits to the remote". */
  reason: string
  /** What the model does after a yes: "run exactly the same command again". */
  again: string
  screen: ScreenQuestion
  agentId?: string
}

/** The answer when the guard itself failed (a throw, its budget, a re-entry): nothing runs. */
export const GUARD_FAILED = "Jarvis's safety check failed, so this command did not run. Try a simpler command, or ask the user to run it."

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

/** A held command waits this long for its spoken OK. */
const CONSENT_MS = 120_000
const TASKS_MAX = 20
/** Long enough for an aborted call's result (and its background task id) to arrive. */
const STAND_DOWN_WAIT_MS = 300

const ELEVATED_DETAIL = 'Claude Code runs as administrator, so Jarvis stays off'
const ELEVATED_LOG =
  'Jarvis stays off: Claude Code runs as administrator, and so would the voice helper and every command Claude runs. Start Claude Code from a normal window, not "Run as administrator".'
const ADMIN_PENDING = 'Jarvis is still checking whether Claude Code runs as administrator; nothing of Jarvis\'s starts until it knows. Try again in a few seconds.'
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
  for (const field of ['command', 'script', 'file_path', 'notebook_path']) {
    const value = call[field]
    if (typeof value === 'string') return value
  }
  return ''
}

/** "it deletes files", or the reason alone where it is not a verb phrase ("unreadable Bash input"). */
const itDoes = (verdict: Verdict): string => (verdict.reason.startsWith('unreadable ') ? verdict.reason : `it ${verdict.reason}`)

/** The on-screen question for a guarded call: what it does, and the command itself (one line, cut short). */
function guardQuestion(call: GuardCall, verdict: Verdict): ScreenQuestion {
  const tool = String(call.tool)
  const isFile = tool === 'Write' || tool === 'Edit' || tool === 'NotebookEdit'
  const unreadable = verdict.reason.startsWith('unreadable ')
  const what = TOOL_WORDS[tool] ?? `use ${tool}`
  const which = unreadable ? (isFile ? 'whose path Jarvis could not read' : 'Jarvis could not read') : `that ${verdict.reason}`
  const warning = WARNINGS[verdict.rule]
  const shown = clip(commandText(call).replace(/\s+/g, ' ').trim(), SHOWN_MAX)
  return {
    question: `${LEAD}Claude wants to ${what} ${which}${shown === '' ? '' : `: "${shown}"`}.${warning === undefined ? '' : ` ${warning}`} ${isFile ? 'Go ahead?' : 'Run it?'}`,
    no: NO_RUN,
    yes: YES_RUN,
    nothing: 'nothing ran',
  }
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
  const words = phraseWords(answer)
  if (TYPED_YES.has(words) || words === phraseWords(q.yes)) return undefined
  return `The user did not confirm; they wrote: "${clip(answer.trim(), 200)}". ${capitalize(q.nothing)}.`
}

export class PcControl {
  /** The desktop tool's calls and timers (desktop.ts). */
  readonly desktopTool: DesktopTool
  /**
   * The main loop's permission mode, as the latest classic hook input said
   * (a prompt, a session start, a plan-mode tool). Undefined until one has:
   * the desktop tool then changes nothing, as in plan mode.
   */
  permissionMode: string | undefined

  /**
   * The one command held for a spoken OK: when, and in which turn (the yes
   * must come in the next one). Void once a turn held two different commands.
   */
  private held: { key: string; at: number; turnId: string; isVoid?: boolean } | undefined
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
  private admin: AdminFacts | undefined
  /** The administrator check has ended (a failed check too); until then nothing of Jarvis's starts. */
  private hasCheckedAdmin = false
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
   * check has not ended yet. Anything of Jarvis's that starts a process (a
   * helper, a setup) checks it first.
   */
  get holdsHelper(): boolean {
    return !this.hasCheckedAdmin || this.isElevated
  }

  /** Why nothing of Jarvis's may start now (elevated, or not known yet); undefined when it may. */
  whyHeld(): string | undefined {
    if (this.isElevated) return ELEVATED_LOG
    return this.hasCheckedAdmin ? undefined : ADMIN_PENDING
  }

  /** True (and says why) when Claude Code runs elevated, or the check has not ended: nothing of Jarvis's may start then. */
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
    return refused
  }

  private async decide(call: GuardCall, ask: CallAsk, signal: AbortSignal | undefined): Promise<string | undefined> {
    // A cloud session runs nothing on the user's PC (and its platform is set before isLocal).
    if (this.app.platform !== undefined && !this.app.isLocal) return undefined
    const verdict = judge(String(call.tool), call)
    switch (verdict.tier) {
      case 'pass':
        return undefined
      case 'never':
        return `Jarvis blocks this: it ${verdict.reason}, which is on Jarvis's never list. Do not retry it or work around it; the user can do it themselves.`
      case 'voice':
        return await this.confirm(
          {
            key: `${String(call.tool)}\n${commandText(call).replace(/\s+/g, ' ').trim()}`,
            reason: verdict.reason,
            again: 'run exactly the same command again',
            screen: guardQuestion(call, verdict),
            agentId: call.agentId,
          },
          ask,
          signal,
        )
      default:
        return await this.askOnScreen(guardQuestion(call, verdict), ask, signal, call.agentId)
    }
  }

  /**
   * The voice tier. In the running voice turn (main loop): the call is held
   * and the model asks aloud; the next main-loop turn, if its words are only
   * a yes (not said over Jarvis's own speech), lets exactly that call through
   * once. Any turn in between, typed or spoken, leaves the yes unbound. One
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
      const said = voice.voiceTurnConsent
      if (said === 'yes') {
        this.held = undefined
        engine.debug(`jarvis: spoken OK for: ${clip(consent.key.replace('\n', ' '), 120)}`)
        return undefined
      }
      if (said === 'yes_while_talking') {
        this.held = { key: consent.key, at: now, turnId }
        return "The user's 'yes' was heard while Jarvis was still speaking, so it does not count. Ask again in one short sentence and end your turn."
      }
    }
    if (held !== undefined && held.turnId === turnId && (held.isVoid === true || held.key !== consent.key)) {
      this.held = { ...held, at: now, isVoid: true }
      return `Jarvis did not hold this: it ${consent.reason}, and this turn already held another command. One spoken yes answers one question, so neither is held now. Tell the user in one short sentence that you will ask about them one at a time, and end your turn.`
    }
    this.held = { key: consent.key, at: now, turnId }
    return `Jarvis held this: it ${consent.reason} and needs the user's spoken OK. Say in one short sentence exactly what it will do and ask whether to go ahead, then end your turn without calling another tool. If they answer yes, ${consent.again}; anything else, drop it.`
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
   * the UAC level. True when elevated: Jarvis then stays off (PLAN.md:150). A
   * failed check is taken as not elevated, and only logged to debug.
   */
  async onSessionStart(): Promise<boolean> {
    const { engine, platform } = this.app
    try {
      if (engine === undefined || platform === undefined) return false
      this.admin = await probeAdmin(engine, platform)
    } catch (error) {
      engine?.debug(`jarvis: the administrator check failed, so Jarvis takes it as not elevated: ${describeError(error)}`)
      return false
    } finally {
      this.hasCheckedAdmin = true
    }
    if (this.admin.isElevated) {
      // Nothing started during the check (holdsHelper); stopping is a second line.
      await this.app.helper?.stop()
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
    this.notePermissionMode(mode)
    if (mode !== 'bypassPermissions' || this.hasWarnedBypass) return undefined
    this.hasWarnedBypass = true
    return BYPASS_WARNING
  }

  /** classic.UserPromptSubmit: the mode the prompt's turn runs in. A subagent's or teammate's is not the main loop's. */
  notePermissionMode(mode: string | undefined, agentId?: string): void {
    if (agentId !== undefined) return
    if (mode !== undefined && mode !== '') this.permissionMode = mode
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
    else this.notePermissionMode(mode)
  }

  /** The desktop tool's call (its hook's closures over its own `$`), on the HUD's log as the HUD's own hook would put it. */
  async desktop(e: Record<string, unknown>, ports: AskPorts, signal?: AbortSignal): Promise<{ result: string } | { deny: string }> {
    const hud = this.app.hud
    const id = hud?.onToolStart(actionLabel({ ...e, tool: String(e.tool) }))
    let isOk = false
    try {
      const answer = await this.desktopTool.call(e, ports, signal)
      isOk = !('deny' in answer)
      return answer
    } finally {
      if (id !== undefined) hud?.onToolEnd(id, isOk)
    }
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
          `The desktop tool asks on screen before each action unless your rules allow it. To let its actions run without that question, add "${DESKTOP_TOOL}" to "allow" yourself; Jarvis's own questions, such as before reading the clipboard, still come.`,
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
    if (admin === undefined) lines.push('Administrator: not checked (it runs when the session starts).')
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
        ? 'Permission mode: not known yet (the desktop tool changes nothing until the next prompt).'
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
