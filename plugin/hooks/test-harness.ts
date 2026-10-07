// Test support for the *.test.ts files: a fake world beneath the plugin.
// The testing kit's `on` hooks stand for the engine, so these answer every
// call the mod makes: a fake helper process, its HTTP control server, the
// clock, the filesystem checks, prompt submission and turn aborts.

import { mock } from 'claude-code/testing'
import type { Engine as TestEngine, MockClock } from 'claude-code/testing'
import type {
  ModelCompleteInput,
  ModelCompleteResult,
  On,
  PaneOpenArgs,
  ProcessSpawnChunk,
  ProcessSpawnRequest,
  PromptSubmitInput,
  SessionMessage,
  Settings,
  SettingsSource,
  ToolSpec,
  TurnCompleteInput,
  TurnStepChunk,
  TurnStepInput,
  TurnStepResult,
  UiBlitArgs,
} from 'claude-code'

import type { RuleVerdict } from './pc'

export const WINDOWS_ENV = {
  OS: 'Windows_NT',
  USERPROFILE: 'C:\\Users\\Rotem',
  LOCALAPPDATA: 'C:\\Users\\Rotem\\AppData\\Local',
  NO_PROXY: 'corp.example',
  SystemRoot: 'C:\\WINDOWS',
}
export const DATA_DIR = 'C:\\Users\\Rotem\\.jarvis'
export const VENV_PYTHON = 'C:\\Users\\Rotem\\.jarvis\\venv\\Scripts\\python.exe'
export const WINGET_UV = 'C:\\Users\\Rotem\\AppData\\Local\\Microsoft\\WinGet\\Links\\uv.exe'
export const PORT = 50123

/** One spawned child the test drives: what it writes and when it exits. */
export class FakeChild {
  private queue: ProcessSpawnChunk[] = []
  private wake: (() => void) | undefined
  private exitCode: number | null | undefined

  constructor(readonly request: ProcessSpawnRequest) {}

  get argv(): readonly string[] {
    return this.request.argv
  }

  get isHelperRun(): boolean {
    return this.request.argv[0] === VENV_PYTHON && this.request.argv.includes('run')
  }

  get hasExited(): boolean {
    return this.exitCode !== undefined
  }

  stdout(text: string): void {
    this.queue.push({ stream: 'stdout', text })
    this.poke()
  }

  stderr(text: string): void {
    this.queue.push({ stream: 'stderr', text })
    this.poke()
  }

  /** Writes one protocol event as a stdout line. */
  event(event: Record<string, unknown>): void {
    this.stdout(`${JSON.stringify({ v: 1, ...event })}\n`)
  }

  /** The default capabilities are an older helper's, without `wake.plain`. */
  hello(port = PORT, capabilities: string[] = ['ptt']): void {
    this.event({ type: 'hello', port, pid: 4242, platform: 'windows', version: '0.1.0', capabilities })
  }

  exit(code: number | null): void {
    this.exitCode = code
    this.poke()
  }

  async *chunks(): AsyncGenerator<ProcessSpawnChunk, number | null> {
    for (;;) {
      const next = this.queue.shift()
      if (next !== undefined) {
        yield next
        continue
      }
      if (this.exitCode !== undefined) return this.exitCode
      await new Promise<void>(resolve => {
        this.wake = resolve
      })
    }
  }

  private poke(): void {
    const wake = this.wake
    this.wake = undefined
    wake?.()
  }
}

export type SentCommand = {
  name: string
  url: string
  body: Record<string, unknown>
  headers: Record<string, string>
}

/** One `$.process.run` the mod made (cwd as the mod gave it). */
export type RunCall = { argv: readonly string[]; cwd?: string; env?: Record<string, string>; timeoutMs?: number }

/**
 * What a scripted `$.process.run` answers; `deny` rejects the call (it did not
 * start, or timed out), and `delayMs` holds the answer back that long on the mocked clock.
 */
export type RunAnswer = { exitCode: number; stdout: string; stderr?: string; delayMs?: number } | { deny: string }

/** One question dialog, as `$.ui.ask` raised it. */
export type AskedDialog = { question: string; header: string | undefined; options: string[]; multiSelect: boolean }

export type World = {
  clock: MockClock
  children: FakeChild[]
  commands: SentCommand[]
  submits: PromptSubmitInput[]
  aborts: string[]
  statuses: (string | undefined)[]
  logs: string[]
  toasts: string[]
  store: Map<string, unknown>
  /** The session state ($.state) by `plugin.key`. */
  state: Map<string, unknown>
  /** Paths that exist (`$.fs.write` adds to them). */
  existing: Set<string>
  /** Every path `$.fs.exists` was asked about. */
  checked: string[]
  /** When set, `$.fs.write` fails (the hook beneath throws this). */
  writeError: string | undefined
  /** When set, `$.prompt.submit` answers `{ drop }` with it (a hook beneath refused). */
  submitDrop: string | undefined
  /** Answers `$.fs.exists` (default: whether `existing` holds the path). */
  exists: (path: string) => boolean
  /** What `where uv` / `which uv` prints; undefined: not on the PATH. */
  uvOnPath: string | undefined
  /** Scripted behaviour per spawn (default: nothing, the test drives the child). */
  onSpawn: (child: FakeChild) => void
  /** The helper's answer per command (default `{ ok: true }`). */
  respond: (command: SentCommand) => { status: number; body: unknown }
  helpers: () => FakeChild[]
  lastHelper: () => FakeChild
  named: (name: string) => SentCommand[]
  status: () => string | undefined
  /** Model responses served beneath `turn.step`, by `${turnId}:${index}`. */
  steps: Map<string, TurnStepChunk[]>
  /** Every `turn.step` request as it reached the engine (the model it names). */
  stepInputs: TurnStepInput[]
  /** Steps (`${turnId}:${index}`) whose request gets no response, as when the API refused it. */
  failedSteps: Set<string>
  /** The conversation's size each step (`${turnId}:${index}`) reports in its usage; none: no usage. */
  stepTokens: Map<string, number>
  /** The main conversation as `$.session.messages()` reads it. */
  messages: SessionMessage[]
  /** What `$.session.usage()` says the conversation's size is (undefined: no response yet). */
  contextTokens: number | undefined
  /** Every `$.model.complete` call. */
  completions: ModelCompleteInput[]
  /** Answers `$.model.complete` (default: the judge says "simple"). */
  complete: (input: ModelCompleteInput) => ModelCompleteResult | Promise<ModelCompleteResult>
  /** Every pane the mod opened, and the ids it closed. */
  opens: PaneOpenArgs[]
  closes: string[]
  /** Every repaint of a Raster, and how many redraws the mod asked for. */
  blits: UiBlitArgs[]
  invalidations: number
  /** When set, `$.ui.blit` answers `{ deny }` with it (the Raster is gone). */
  blitDeny: string | undefined
  /** Lets every pending dispatch run, the clock where it is. */
  settle: (rounds?: number) => Promise<void>
  /** Every tool `$.tool.register` declared, in order. */
  tools: ToolSpec[]
  /** When set, `$.tool.register` is refused with it (a managed policy, a host that cannot add tools). */
  toolRefusal: string | undefined
  /** The questions `$.ui.ask` put to the user (AskUserQuestion). */
  asked: string[]
  /** Each of those dialogs as drawn: its header, its options in order, and whether it took several. */
  dialogs: AskedDialog[]
  /** The user's answer to `$.ui.ask`: a label, or text typed under "Other"; undefined dismisses the dialog. */
  askAnswer: string | undefined
  /** When set, the answer comes this long after the dialog opened, on the mocked clock. */
  askDelayMs: number | undefined
  /** When set, the dialog auto-resolved to `askAnswer` after this long idle (the user was away). */
  askIdleMs: number | undefined
  /** Every `$.tool.check` question, and the verdict it gets (default: the engine's plain "ask"). */
  checks: { tool: string; input: unknown }[]
  toolCheck: (tool: string, input: unknown) => RuleVerdict | Promise<RuleVerdict>
  /** The task ids TaskStop was called with. */
  stoppedTasks: string[]
  /** Every `$.process.run` the mod made, but the administrator check's. */
  runs: RunCall[]
  /** The administrator check's runs (whoami, reg, id), kept apart so other tests' runs stay their own. */
  probes: RunCall[]
  /**
   * Answers a `$.process.run` the world does not know; undefined: exit 127 (no
   * such program), but for the administrator check, which then finds a normal
   * user (ADMIN_PROBE_ANSWERS).
   */
  onRun: (call: RunCall) => RunAnswer | undefined
  /** Each settings source as `$.settings.read({ source })` answers it (`merged` for no source); none: `{}`. */
  settings: Partial<Record<SettingsSource | 'merged', Settings>>
  /** When set, `$.settings.read` fails with it. */
  settingsError: string | undefined
  /** The sources the mod read (`merged` for none). */
  settingsReads: string[]
}

/**
 * What the administrator check finds when a test does not say: Windows'
 * whoami (an administrator's token, not elevated) and its UAC policy (Always
 * notify), or a normal user's uid elsewhere.
 */
export const ADMIN_PROBE_ANSWERS: Record<'whoami' | 'reg' | 'id', string> = {
  whoami: [
    '"Everyone","Well-known group","S-1-1-0","Mandatory group, Enabled by default, Enabled group"',
    '"BUILTIN\\Administrators","Alias","S-1-5-32-544","Group used for deny only"',
    '"Mandatory Label\\Medium Mandatory Level","Label","S-1-16-8192",""',
  ].join('\r\n'),
  reg: [
    '',
    'HKEY_LOCAL_MACHINE\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System',
    '    ConsentPromptBehaviorAdmin    REG_DWORD    0x2',
    '    EnableLUA    REG_DWORD    0x1',
    '    PromptOnSecureDesktop    REG_DWORD    0x1',
    '',
  ].join('\r\n'),
  id: '1000\n',
}

export type WorldOptions = {
  env?: Record<string, string>
  /** Whether the helper's venv exists (default true). */
  installed?: boolean
}

/** Registers the fake world beneath the plugin. Call before the first `$` call. */
export function world(on: On, { env = WINDOWS_ENV, installed = true }: WorldOptions = {}): World {
  const clock = mock.clock(on, { now: 1_000_000 })
  mock.env(on, env)
  const w: World = {
    clock,
    children: [],
    commands: [],
    submits: [],
    aborts: [],
    statuses: [],
    logs: [],
    toasts: [],
    store: new Map(),
    state: new Map(),
    existing: new Set(installed ? [VENV_PYTHON] : []),
    checked: [],
    writeError: undefined,
    submitDrop: undefined,
    exists: path => w.existing.has(path),
    uvOnPath: undefined,
    steps: new Map(),
    stepInputs: [],
    failedSteps: new Set(),
    stepTokens: new Map(),
    contextTokens: undefined,
    messages: [],
    completions: [],
    complete: () => answered('simple'),
    opens: [],
    closes: [],
    blits: [],
    invalidations: 0,
    blitDeny: undefined,
    onSpawn: () => undefined,
    respond: () => ({ status: 200, body: { ok: true } }),
    helpers: () => w.children.filter(child => child.isHelperRun),
    lastHelper: () => {
      const helper = w.helpers().at(-1)
      if (helper === undefined) throw new Error('no helper was spawned')
      return helper
    },
    named: name => w.commands.filter(command => command.name === name),
    status: () => w.statuses.at(-1),
    settle: async (rounds = 8) => {
      for (let i = 0; i < rounds; i += 1) await clock.settle()
    },
    tools: [],
    toolRefusal: undefined,
    asked: [],
    dialogs: [],
    askAnswer: undefined,
    askDelayMs: undefined,
    askIdleMs: undefined,
    checks: [],
    toolCheck: () => ({ decision: 'ask' }),
    stoppedTasks: [],
    runs: [],
    probes: [],
    onRun: () => undefined,
    settings: {},
    settingsError: undefined,
    settingsReads: [],
  }
  const state = w.state

  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('session.attach', ($, e) => ({ clientId: e.clientId }))
  on('session.end', ($, e) => ({ sessionId: e.sessionId }))
  on('command.register', ($, e) => ({ value: { command: e.name } }))
  on('tool.register', ($, e) => {
    if (w.toolRefusal !== undefined) return { deny: w.toolRefusal }
    w.tools.push(e)
    return { value: { tool: `mcp__jarvis__${e.name}` } }
  })
  on('tool.describe', { tool: 'mcp__jarvis__desktop' }, ($, e) => ({ description: e.description, ...(e.isDeferred === true ? { isDeferred: true } : {}) }))
  // `$.ui.ask` is a call of the AskUserQuestion tool: the person's answer, a dismissal, or an idle auto-resolve.
  on('tool.call', { tool: 'AskUserQuestion' }, async ($, e) => {
    const [first] = e.questions
    const question = first?.question ?? ''
    w.asked.push(question)
    w.dialogs.push({
      question,
      header: first?.header,
      options: (first?.options ?? []).map(option => option.label),
      multiSelect: first?.multiSelect === true,
    })
    if (w.askDelayMs !== undefined) await clock.sleep(w.askDelayMs)
    if (w.askAnswer === undefined) return { deny: 'dismissed' }
    const idle = w.askIdleMs === undefined ? {} : { afkTimeoutMs: w.askIdleMs }
    return { result: { questions: e.questions, answers: { [question]: w.askAnswer }, ...idle } }
  })
  // The engine's verdict for the session's rules and mode; the default is its
  // plain "ask" for a tool nothing allows yet (default mode, no rule).
  on('tool.check', async ($, e) => {
    w.checks.push({ tool: e.tool, input: e.input })
    return await w.toolCheck(e.tool, e.input)
  })
  on('tool.call', { tool: 'TaskStop' }, ($, e) => {
    const taskId = e.task_id ?? ''
    w.stoppedTasks.push(taskId)
    return { result: { message: `Stopped ${taskId}`, task_id: taskId, task_type: 'local_bash' } }
  })
  on('ui.status', ($, e) => {
    w.statuses.push(e.text)
    return { value: undefined }
  })
  on('ui.log', ($, e) => {
    w.logs.push(e.text)
    return { value: undefined }
  })
  on('ui.toast', ($, e) => {
    w.toasts.push(e.text)
    return { value: undefined }
  })
  on('state.get', ($, e) => {
    const key = `${e.plugin}.${e.key}`
    return { value: { value: state.get(key), version: state.has(key) ? 1 : 0 } }
  })
  on('state.set', ($, e) => {
    state.set(`${e.plugin}.${e.key}`, e.value)
    return { value: { isSet: true, version: 1 } }
  })
  on('store.get', ($, e) => ({ value: w.store.get(e.key) }))
  on('store.set', ($, e) => {
    w.store.set(e.key, e.value)
    return { value: undefined }
  })
  on('store.delete', ($, e) => {
    w.store.delete(e.key)
    return { value: undefined }
  })
  on('fs.exists', ($, e) => {
    const path = windowsPath(e.path)
    w.checked.push(path)
    return { value: w.exists(path) }
  })
  on('fs.write', ($, e) => {
    if (w.writeError !== undefined) throw new Error(w.writeError)
    w.existing.add(windowsPath(e.path))
    return { value: undefined }
  })
  on('process.run', async ($, e) => {
    const [command] = e.argv
    if (command === 'where' || command === 'which') {
      return { value: run(w.uvOnPath === undefined ? 1 : 0, w.uvOnPath ?? '') }
    }
    if (command === 'uname') return { value: run(0, 'Linux\n') }
    const init = e.init ?? {}
    const call: RunCall = {
      argv: e.argv,
      ...(init.cwd === undefined ? {} : { cwd: windowsPath(init.cwd) }),
      ...(init.env === undefined ? {} : { env: init.env }),
      ...(init.timeoutMs === undefined ? {} : { timeoutMs: init.timeoutMs }),
    }
    const program = (command ?? '').split(/[\\/]/).at(-1)?.toLowerCase().replace(/\.exe$/, '')
    const isProbe = program === 'whoami' || program === 'reg' || program === 'id'
    if (isProbe) w.probes.push(call)
    else w.runs.push(call)
    const answer = w.onRun(call)
    if (answer === undefined && isProbe) return { value: run(0, ADMIN_PROBE_ANSWERS[program]) }
    if (answer === undefined) return { value: run(127, '') }
    if ('deny' in answer) return { deny: answer.deny }
    if (answer.delayMs !== undefined) await clock.sleep(answer.delayMs)
    return { value: { ...run(answer.exitCode, answer.stdout), stderr: answer.stderr ?? '' } }
  })
  on('process.spawn', async function* ($, e) {
    const child = new FakeChild(e.cwd === undefined ? e : { ...e, cwd: windowsPath(e.cwd) })
    w.children.push(child)
    w.onSpawn(child)
    const code = yield* child.chunks()
    return { value: { code, signal: null } }
  })
  on('http.fetch', ($, e) => {
    const url = new URL(e.url)
    const command: SentCommand = {
      name: url.pathname.replace(/^\/v1\//, ''),
      url: e.url,
      body: JSON.parse(e.init?.body ?? '{}') as Record<string, unknown>,
      headers: e.init?.headers ?? {},
    }
    w.commands.push(command)
    const { status, body } = w.respond(command)
    return { value: { status, ok: status < 300, headers: {}, text: JSON.stringify(body) } }
  })
  on('prompt.submit', ($, e) => {
    w.submits.push(e)
    return w.submitDrop === undefined ? { text: e.text } : { drop: w.submitDrop }
  })
  on('turn.abort', ($, e) => {
    w.aborts.push(e.turnId)
    return { value: undefined }
  })
  on('turn.start', ($, e) => ({ turnId: e.turnId }))
  on('turn.complete', ($, e) => ({ text: e.answer }))
  on('turn.step', async function* ($, e) {
    w.stepInputs.push(e)
    const key = `${e.turnId}:${e.index}`
    const isFailed = w.failedSteps.has(key)
    const tokens = w.stepTokens.get(key)
    const chunks = isFailed ? [] : (w.steps.get(key) ?? [])
    for (const chunk of chunks) yield chunk
    const result: TurnStepResult = {
      turnId: e.turnId,
      index: e.index,
      answer: chunks.map(chunk => (chunk.kind === 'text' ? chunk.text : '')).join(''),
      toolUses: [],
      stopReason: isFailed ? null : 'end_turn',
      usage:
        tokens === undefined
          ? null
          : { input_tokens: tokens, output_tokens: 0, cache_read_input_tokens: 0, cache_creation_input_tokens: 0, model: e.model },
    }
    return result
  })
  on('model.complete', async ($, e) => {
    w.completions.push(e)
    return { value: await w.complete(e) }
  })
  on('session.usage', () => ({
    value: {
      startedAt: 0,
      context: { window: 200_000, ...(w.contextTokens === undefined ? {} : { tokens: w.contextTokens }) },
      rateLimits: [],
    },
  }))
  on('session.messages', () => ({ value: w.messages }))
  // The settings, never the real ones: each source as the test set it.
  on('settings.read', ($, e) => {
    const source = e.source ?? 'merged'
    w.settingsReads.push(source)
    if (w.settingsError !== undefined) throw new Error(w.settingsError)
    return { value: w.settings[source] ?? {} }
  })
  on('ui.open', ($, e) => {
    w.opens.push(e)
    return { value: { isPlaced: true as const } }
  })
  on('ui.close', ($, e) => {
    w.closes.push(e.id)
    return { value: undefined }
  })
  on('ui.blit', ($, e) => {
    w.blits.push(e)
    return { value: w.blitDeny === undefined ? {} : { deny: w.blitDeny } }
  })
  on('ui.invalidate', () => {
    w.invalidations += 1
    return { value: undefined }
  })
  on('tool.call', () => ({ result: 'ok' }))
  on('prompt.compose', () => ({ sections: [{ id: 'intro', text: 'You are Claude Code.', scope: 'shared' }] }))
  on('classic.UserPromptSubmit', () => ({}))
  on('classic.SessionStart', () => ({}))
  on('classic.PostToolUse', () => ({}))
  return w
}

/**
 * The tests simulate Windows on a POSIX host, which resolves a `C:\\...` path
 * as relative to the session's folder before the hooks beneath see it.
 */
function windowsPath(path: string): string {
  return path.replace(/^.*?(?=[A-Za-z]:\\)/, '')
}

/** A completion that answered `text`. */
export function answered(text: string): ModelCompleteResult {
  return {
    isAnswered: true,
    text,
    usage: { input_tokens: 1, output_tokens: 1, cache_creation_input_tokens: 0, cache_read_input_tokens: 0 },
  }
}

function run(exitCode: number, stdout: string) {
  return { exitCode, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false }
}

/** Starts the session on the terminal and waits for the helper to be spawned. */
export async function startSession($: TestEngine, w: World): Promise<void> {
  await $.session.start({ cwd: 'C:\\work', surface: 'terminal', isInteractive: true })
  await w.settle()
}

/** Starts the session and brings a helper up to `hello` and `ready`. */
export async function startHelper($: TestEngine, w: World, capabilities?: string[]): Promise<FakeChild> {
  await startSession($, w)
  const helper = w.lastHelper()
  helper.hello(PORT, capabilities)
  helper.event({ type: 'state', state: 'sleeping' })
  helper.event({ type: 'ready', sttModel: 'large-v3-turbo', sttDevice: 'cuda', pttKey: 'right ctrl' })
  await w.settle()
  return helper
}

/** Runs `/jarvis <args>` as the person typing it. */
export async function jarvis($: TestEngine, args: string): Promise<string> {
  const result = await $.command.run({
    command: 'jarvis',
    args,
    origin: { kind: 'composer' },
    presentation: { isFullscreen: false, columns: 100 },
  })
  return result.text ?? ''
}

/** Text chunks as a model streams them, cut into small deltas. */
export function textChunks(text: string, index = 0, size = 7): TurnStepChunk[] {
  const chunks: TurnStepChunk[] = []
  for (let at = 0; at < text.length; at += size) chunks.push({ kind: 'text', index, text: text.slice(at, at + size) })
  return chunks
}

/**
 * Streams one `turn.step` whose model response is `chunks` (served by the
 * world's bottom hook) and returns what the plugin forwarded upwards, so a
 * test can check that every chunk passed through unchanged.
 */
export async function runStep(
  $: TestEngine,
  w: World,
  turnId: string,
  chunks: TurnStepChunk[],
  options: { index?: number; agentId?: string; model?: string } = {},
): Promise<TurnStepChunk[]> {
  const index = options.index ?? 0
  w.steps.set(`${turnId}:${index}`, chunks)
  const forwarded: TurnStepChunk[] = []
  const stream = $.turn.step({
    turnId,
    index,
    model: options.model ?? 'claude-test',
    messageCount: 1,
    ...(options.agentId === undefined ? {} : { agentId: options.agentId }),
  })
  for await (const chunk of stream) forwarded.push(chunk)
  return forwarded
}

/** Ends a main-loop turn as the engine does. */
export function completeTurn($: TestEngine, turnId: string, isAborted = false) {
  const input: TurnCompleteInput = isAborted
    ? { turnId, answer: '', durationMs: 10, isAborted: true, reason: 'aborted' }
    : { turnId, answer: 'done', durationMs: 10, isAborted: false, reason: 'answer' }
  return $.turn.complete(input)
}

/** Ends a main-loop turn on an API error, as when its request was refused. */
export function failTurn($: TestEngine, turnId: string) {
  return $.turn.complete({ turnId, answer: '', durationMs: 10, isAborted: false, reason: 'error' })
}
