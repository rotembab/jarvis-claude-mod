// The voice helper's supervisor: spawns `python -m jarvis_voice run`, reads
// its JSON-lines events, sends it commands over loopback HTTP, keeps it alive
// with heartbeats and restarts it with backoff when it dies unexpectedly.

import type { HookStream, ProcessSpawnChunk, ProcessSpawnResult, Timer } from 'claude-code'

import type { JarvisHelperRef } from '../types'
import type { Engine } from './engine'
import { delay, describeError, TIMEOUT, withTimeout } from './engine'
import type { Platform } from './platform'
import { withLoopbackNoProxy } from './platform'
import { parseEvent } from './protocol'
import type { CommandBodies, CommandName, CommandResponse, ConfigCommand, ErrorCode, HelloEvent, HelperEvent } from './protocol'

export const HEARTBEAT_MS = 2000
/** Restart delays after unexpected exits; after the last one the supervisor gives up. */
export const BACKOFF_MS: readonly number[] = [1000, 2000, 5000, 10000]
/** A helper that ran this long before dying earns a fresh backoff sequence. */
export const HEALTHY_UPTIME_MS = 60_000
/** The helper's exit code when another instance holds the single-instance lock. */
export const ALREADY_RUNNING_EXIT = 3
/**
 * How long to wait before trying again when our own previous helper (left by
 * a reload, or let go while silent) may still hold the single-instance lock.
 * Its heartbeat watchdog ends it 15 s after the last heartbeat (60 s if it
 * never had one), and shutting down audio on Windows takes a moment more.
 */
export const ORPHAN_RETRY_MS = 20_000
/** Late retries for an orphan before another window is assumed to own the helper. */
export const ORPHAN_RETRIES = 2
export const HELLO_TIMEOUT_MS = 30_000
export const STOP_WAIT_MS = 3000

const COMMAND_TIMEOUT_MS: Record<CommandName, number> = {
  heartbeat: 1500,
  speak: 3000,
  stop: 2000,
  // The helper waits up to 5 s for capture to open (it may rescan devices).
  listen: 6000,
  config: 5000,
  status: 5000,
  // The helper gives each desktop action 6 s.
  desktop: 8000,
  test_voice: 5000,
  shutdown: 2000,
}

export type HelperPhase = 'stopped' | 'not_installed' | 'starting' | 'running' | 'restarting' | 'elsewhere' | 'failed'

export type CommandOutcome =
  | { ok: true; response: CommandResponse }
  | { ok: false; code: ErrorCode | 'not_running' | 'timeout' | 'http'; message: string }

export type HelperOptions = {
  platform: Platform
  /** The Fish Audio key from the plugin's sensitive userConfig, when set. */
  fishApiKey?: string
  /** The Fish Audio model (`JARVIS_TTS_MODEL`); undefined keeps the helper's default. */
  fishModel?: string
  /** False switches the helper's echo cancelling off (`JARVIS_AEC=off`); it is on by default. */
  echoCancel?: boolean
  /** NO_PROXY as the session has it; the child's gains the loopback hosts. */
  noProxy?: string
  /**
   * The speech model to load at start (`--stt-model`), so the helper never
   * loads its own pick first; undefined lets it choose ("auto").
   */
  sttModel: () => Promise<string | undefined>
  /** Who speaks, read at each start: Fish Audio (the default) or the local voice and its reference clip. */
  tts?: () => Promise<TtsChoice>
  /** Settings sent with `config` as soon as the helper says hello. */
  initialConfig: () => Promise<ConfigCommand>
  onEvent: (event: HelperEvent) => void
  onPhase: (phase: HelperPhase, detail?: string) => void
}

export type TtsChoice = { engine: 'fish' | 'local'; localVoiceClip?: string }

/** Splits a stream of text pieces into lines; a line may span pieces. */
export class LineReader {
  private rest = ''

  push(text: string): string[] {
    const parts = (this.rest + text).split('\n')
    this.rest = parts.pop() ?? ''
    return parts.map(line => line.replace(/\r$/, '')).filter(line => line.trim() !== '')
  }

  flush(): string[] {
    const last = this.rest.replace(/\r$/, '')
    this.rest = ''
    return last.trim() === '' ? [] : [last]
  }
}

export function randomToken(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(32))
  return Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('')
}

type Exit = {
  result: ProcessSpawnResult | undefined
  failure: string | undefined
  sawAlreadyRunning: boolean
  uptime: number
}

export class Helper {
  phase: HelperPhase = 'stopped'
  /** The running helper's hello, once it has said it. */
  hello: HelloEvent | undefined

  private token = ''
  private generation = 0
  private attempt = 0
  private stream: HookStream<ProcessSpawnChunk, ProcessSpawnResult> | undefined
  private running: Promise<void> | undefined
  private heartbeat: Timer | undefined
  private retryTimer: Timer | undefined
  private isStopping = false
  private hasCheckedPrevious = false
  /** Late retries left while a helper of ours may still hold the lock. */
  private orphanRetries = 0
  private stderrTail: string[] = []
  private lastFatal: string | undefined

  constructor(
    private readonly engine: Engine,
    private readonly options: HelperOptions,
  ) {}

  get isRunning(): boolean {
    return this.phase === 'running' && this.hello !== undefined
  }

  /**
   * Starts the helper unless it runs or is about to. Automatic starts leave a
   * helper that gave up, or one owned by another window, alone; a start the
   * user asked for (`/jarvis`, `/jarvis restart`) always tries again.
   */
  start({ userInitiated = false }: { userInitiated?: boolean } = {}): void {
    if (this.running !== undefined) return
    if (this.phase === 'restarting' && !userInitiated) return
    if (!userInitiated && (this.phase === 'elsewhere' || this.phase === 'failed')) return
    if (userInitiated) this.attempt = 0
    this.retryTimer?.cancel()
    this.retryTimer = undefined
    this.launch()
  }

  /**
   * Stops the helper on purpose: asks it to shut down, then ends the child.
   * Resolves false when the child outlived both waits and was let go: it may
   * run on (holding its files and the single-instance lock) until its
   * heartbeat watchdog ends it.
   */
  async stop(): Promise<boolean> {
    this.retryTimer?.cancel()
    this.retryTimer = undefined
    const running = this.running
    if (running === undefined) {
      this.setPhase('stopped')
      return true
    }
    const generation = this.generation
    this.isStopping = true
    if (this.hello !== undefined) await this.send('shutdown', {})
    if ((await withTimeout(this.engine, running, STOP_WAIT_MS)) !== TIMEOUT) return true
    if (this.generation !== generation) return false // let go meanwhile: it never said hello
    this.kill()
    if ((await withTimeout(this.engine, running, STOP_WAIT_MS)) !== TIMEOUT) return true
    if (this.generation === generation) {
      this.abandon()
      this.isStopping = false
      this.setPhase('stopped')
    }
    return false
  }

  async restart(): Promise<void> {
    await this.stop()
    this.start({ userInitiated: true })
  }

  /** Sends one command to the running helper; never rejects. */
  async send<N extends CommandName>(name: N, body: CommandBodies[N], timeoutMs?: number): Promise<CommandOutcome> {
    const hello = this.hello
    if (hello === undefined) return { ok: false, code: 'not_running', message: 'The voice helper is not running.' }
    return this.post(hello.port, this.token, name, body, timeoutMs ?? COMMAND_TIMEOUT_MS[name])
  }

  private async post(
    port: number,
    token: string,
    name: CommandName,
    body: unknown,
    timeoutMs: number,
  ): Promise<CommandOutcome> {
    const request = this.engine.fetch(`http://127.0.0.1:${port}/v1/${name}`, {
      method: 'POST',
      headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' },
      body: JSON.stringify(body),
    })
    let response: Awaited<typeof request> | typeof TIMEOUT
    try {
      response = await withTimeout(this.engine, request, timeoutMs)
    } catch (error) {
      return { ok: false, code: 'http', message: `${name}: ${describeError(error)}` }
    }
    if (response === TIMEOUT) return { ok: false, code: 'timeout', message: `${name} timed out after ${timeoutMs} ms` }
    let parsed: CommandResponse | undefined
    try {
      const value: unknown = JSON.parse(response.text)
      if (typeof value === 'object' && value !== null && 'ok' in value) parsed = value as CommandResponse
    } catch {
      // not JSON: judged by the status below
    }
    if (parsed?.ok === true) return { ok: true, response: parsed }
    if (parsed?.error !== undefined) return { ok: false, code: parsed.error.code, message: parsed.error.message }
    return { ok: false, code: 'http', message: `${name}: HTTP ${response.status}` }
  }

  private launch(): void {
    this.generation += 1
    const generation = this.generation
    this.running = this.runOnce(generation)
      .catch((error: unknown) => this.engine.debug(`jarvis: helper loop failed: ${describeError(error)}`))
      .finally(() => {
        if (this.generation === generation) this.running = undefined
      })
  }

  private kill(): void {
    void this.stream?.return({ code: null, signal: null }).catch(() => undefined)
  }

  /**
   * Lets go of the current child without waiting for its loop: `return()` on
   * a spawn stream takes effect only when the child next writes, so a silent
   * one would hold `running`, and every start after it, indefinitely. The
   * loop's late end, and anything the child still says, are ignored (stale
   * generation). Its watchdog ends it, so the next start allows for its lock.
   */
  private abandon(): void {
    const stream = this.stream
    this.generation += 1
    this.running = undefined
    this.stream = undefined
    this.hello = undefined
    this.heartbeat?.cancel()
    this.heartbeat = undefined
    this.orphanRetries = ORPHAN_RETRIES
    void stream?.return({ code: null, signal: null }).catch(() => undefined)
  }

  private async runOnce(generation: number): Promise<void> {
    const { platform } = this.options
    const isCurrent = (): boolean => this.generation === generation
    this.setPhase('starting')
    if (!this.hasCheckedPrevious) {
      this.hasCheckedPrevious = true
      await this.shutdownPrevious()
    }
    if (!(await this.engine.exists(platform.venvPython).catch(() => false))) {
      if (isCurrent()) this.setPhase('not_installed')
      return
    }

    const startedAt = await this.engine.now()
    const reader = new LineReader()
    const exit: Exit = { result: undefined, failure: undefined, sawAlreadyRunning: false, uptime: 0 }
    const sttModel = await this.options.sttModel().catch(() => undefined)
    const tts = (await this.options.tts?.().catch(() => undefined)) ?? { engine: 'fish' }
    // stop() may have come while this run got ready: start nothing.
    if (!isCurrent()) return
    if (this.isStopping) {
      this.afterExit(exit)
      return
    }

    this.token = randomToken()
    this.stderrTail = []
    this.lastFatal = undefined
    const stream = this.engine.spawn({
      argv: [
        platform.venvPython,
        '-m',
        'jarvis_voice',
        'run',
        '--data-dir',
        platform.dataDir,
        ...(sttModel === undefined ? [] : ['--stt-model', sttModel]),
      ],
      cwd: platform.dataDir,
      env: this.childEnv(tts),
    })
    this.stream = stream
    // A helper that never says hello (hung opening a device, say) is let go
    // and the restart policy acts now; waiting on its loop could take forever.
    const helloTimer = this.engine.after(HELLO_TIMEOUT_MS, () => {
      if (!isCurrent() || this.hello !== undefined) return
      exit.failure = `no hello within ${HELLO_TIMEOUT_MS / 1000} s`
      exit.uptime = HELLO_TIMEOUT_MS
      this.abandon()
      this.afterExit(exit)
    })
    const handle = (line: string): void => {
      if (!isCurrent()) return // a child we let go of
      const event = parseEvent(line)
      if (event === undefined) {
        this.engine.debug(`jarvis: helper stdout (not an event): ${line.slice(0, 200)}`)
        return
      }
      if (event.type === 'error' && event.code === 'already_running') exit.sawAlreadyRunning = true
      this.handleEvent(event)
    }
    try {
      for await (const chunk of stream) {
        if (chunk.stream === 'stdout') reader.push(chunk.text).forEach(handle)
        else if (isCurrent()) this.keepStderr(chunk.text)
      }
      reader.flush().forEach(handle)
      exit.result = await stream.result
    } catch (error) {
      exit.failure ??= describeError(error)
    } finally {
      helloTimer.cancel()
      if (isCurrent()) {
        this.heartbeat?.cancel()
        this.heartbeat = undefined
        this.hello = undefined
        this.stream = undefined
      }
    }
    if (!isCurrent()) return
    await this.engine.writeHelperRef(null).catch(() => undefined)
    exit.uptime = (await this.engine.now()) - startedAt
    if (isCurrent()) this.afterExit(exit)
  }

  private afterExit(exit: Exit): void {
    if (this.isStopping) {
      this.isStopping = false
      this.setPhase('stopped')
      return
    }
    const code = exit.result?.code ?? null
    if (exit.sawAlreadyRunning || code === ALREADY_RUNNING_EXIT) {
      // The lock may still be held by a helper of ours that lost its parent
      // (a reload) or was let go; its watchdog ends it. Retry late, then it is real.
      if (this.orphanRetries > 0) {
        this.orphanRetries -= 1
        this.scheduleLaunch(ORPHAN_RETRY_MS, 'waiting for the previous helper to exit')
        return
      }
      this.setPhase('elsewhere')
      return
    }
    if (exit.uptime >= HEALTHY_UPTIME_MS) this.attempt = 0
    const tail = this.stderrTail.at(-1)
    const ended = code !== null ? `exited with code ${code}` : `ended (${exit.result?.signal ?? 'killed'})`
    const why = this.lastFatal ?? exit.failure ?? (tail === undefined ? ended : `${ended}: ${tail}`)
    const wait = BACKOFF_MS[this.attempt]
    if (wait === undefined) {
      this.setPhase('failed', why)
      return
    }
    this.attempt += 1
    this.scheduleLaunch(wait, `in ${wait / 1000} s: ${why}`)
  }

  private scheduleLaunch(ms: number, detail: string): void {
    const generation = this.generation
    this.setPhase('restarting', detail)
    this.retryTimer = this.engine.after(ms, () => {
      this.retryTimer = undefined
      if (this.generation === generation && this.phase === 'restarting') this.launch()
    })
  }

  private handleEvent(event: HelperEvent): void {
    if (event.type === 'hello') {
      this.hello = event
      this.onHello(event)
    } else if (event.type === 'error' && event.fatal) {
      this.lastFatal = event.hint ? `${event.message} (${event.hint})` : event.message
    }
    try {
      this.options.onEvent(event)
    } catch (error) {
      this.engine.debug(`jarvis: event handler failed on ${event.type}: ${describeError(error)}`)
    }
  }

  private onHello(hello: HelloEvent): void {
    this.orphanRetries = 0
    this.setPhase('running')
    this.heartbeat?.cancel()
    this.heartbeat = this.engine.every(HEARTBEAT_MS, () => {
      void this.send('heartbeat', {})
    })
    const ref: JarvisHelperRef = { port: hello.port, token: this.token, pid: hello.pid }
    void this.engine.writeHelperRef(ref).catch(() => undefined)
    void this.options
      .initialConfig()
      .then(config => this.send('config', config))
      .then(outcome => {
        if (!outcome.ok) this.engine.debug(`jarvis: config not applied: ${outcome.message}`)
      })
  }

  /**
   * A reloaded module finds the previous helper's address in session state:
   * that helper lost its parent but may still run, holding the lock.
   */
  private async shutdownPrevious(): Promise<void> {
    const previous = await this.engine.readHelperRef().catch(() => null)
    if (previous === null) return
    this.orphanRetries = ORPHAN_RETRIES
    const outcome = await this.post(previous.port, previous.token, 'shutdown', {}, COMMAND_TIMEOUT_MS.shutdown)
    await this.engine.writeHelperRef(null).catch(() => undefined)
    if (outcome.ok) await delay(this.engine, 1000)
  }

  private childEnv(tts: TtsChoice): Record<string, string> {
    const env: Record<string, string> = {
      PYTHONUNBUFFERED: '1',
      PYTHONUTF8: '1',
      JARVIS_TOKEN: this.token,
      JARVIS_PARENT: 'claude-code',
      NO_PROXY: withLoopbackNoProxy(this.options.noProxy),
    }
    if (this.options.fishApiKey) env.FISH_AUDIO_API_KEY = this.options.fishApiKey
    if (this.options.fishModel) env.JARVIS_TTS_MODEL = this.options.fishModel
    if (this.options.echoCancel === false) env.JARVIS_AEC = 'off'
    if (tts.engine === 'local') {
      env.JARVIS_TTS_ENGINE = 'local'
      if (tts.localVoiceClip) env.JARVIS_LOCAL_VOICE = tts.localVoiceClip
    }
    return env
  }

  private keepStderr(text: string): void {
    for (const line of text.split(/\r?\n/)) {
      if (line.trim() === '') continue
      this.stderrTail.push(line.slice(0, 300))
      this.engine.debug(`jarvis: helper: ${line.slice(0, 300)}`)
    }
    if (this.stderrTail.length > 20) this.stderrTail.splice(0, this.stderrTail.length - 20)
  }

  private setPhase(phase: HelperPhase, detail?: string): void {
    this.phase = phase
    this.options.onPhase(phase, detail)
  }
}
