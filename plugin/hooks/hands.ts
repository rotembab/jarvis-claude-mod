// Hand control, the mod side: supervises the hand helper (`python -m
// jarvis_hands run`, a webcam watching your hands that drives the mouse and
// windows), turns its events into the status line, and answers /jarvis hands
// (with tune, set, preset and reset for its sensitivity), /jarvis setup hands
// and the model's `hands` tool. It is off until the user
// turns it on, since it turns the camera on. The helper has its own venv,
// process and single-instance lock, so voice and hands never take each other
// down and either can be installed alone.

import type { HookStream, PluginOptions, ProcessSpawnChunk, ProcessSpawnResult, Timer } from 'claude-code'

import type { HandsPhase, JarvisHandsView, JarvisHelperRef } from '../types'
import type { Engine } from './engine'
import { delay, describeError, TIMEOUT, withTimeout } from './engine'
import type { Helper } from './helper'
import {
  ALREADY_RUNNING_EXIT,
  BACKOFF_MS,
  HEALTHY_UPTIME_MS,
  HEARTBEAT_MS,
  HELLO_TIMEOUT_MS,
  LineReader,
  ORPHAN_RETRIES,
  ORPHAN_RETRY_MS,
  randomToken,
  STOP_WAIT_MS,
} from './helper'
import type { Platform } from './platform'
import { findUv, joinPath, withLoopbackNoProxy } from './platform'
import type { SetupResult } from './setup'
import { parseProgress, runStreaming } from './setup'

// ---- Protocol: a mirror of plugin/protocol/hands.schema.json (v1), which is authoritative ----

export type HandsState = 'starting' | 'idle' | 'active' | 'paused' | 'calibrating' | 'error'

export type HandsErrorCode =
  | 'already_running'
  | 'unsupported_platform'
  | 'no_camera'
  | 'camera_blocked'
  | 'camera_in_use'
  | 'camera_lost'
  | 'model_missing'
  | 'tracker_failed'
  | 'input_blocked'
  | 'overlay_failed'
  | 'bad_request'
  | 'unauthorized'
  | 'internal'

export type CalibrationStep = 'top_left' | 'top_right' | 'bottom_right' | 'bottom_left' | 'done' | 'cancelled'
/** palm: an open palm held still starts control; always: any hand does at once. */
export type EngageMode = 'palm' | 'always'
/** The displays the hand reaches: every real one, or these ids (1-based). */
export type DisplaySelection = 'all' | number[]

export type HandsDisplay = {
  id: number
  name: string
  x: number
  y: number
  width: number
  height: number
  primary: boolean
  virtual: boolean
  /** Whether hand control maps onto this display. */
  used: boolean
}

type Envelope<T extends string> = { v: 1; type: T }

export type HandsHelloEvent = Envelope<'hello'> & {
  port: number
  pid: number
  platform: string
  version: string
  capabilities: string[]
}
export type HandsStateEvent = Envelope<'state'> & { state: HandsState }
export type HandsReadyEvent = Envelope<'ready'> & {
  /** The camera's name, or its index when the name is unknown. */
  camera: string
  width: number
  height: number
  fps: number
  displays: HandsDisplay[]
}
/** engage, click, drag_start, grab, throw_left, user_input, ... (schema GestureName). */
export type GestureEvent = Envelope<'gesture'> & { name: string }
export type CalibrationEvent = Envelope<'calibration'> & { step: CalibrationStep; display?: string }
export type HandsErrorEvent = Envelope<'error'> & { code: HandsErrorCode; message: string; hint?: string; fatal: boolean }

export type HandsEvent =
  | HandsHelloEvent
  | HandsStateEvent
  | HandsReadyEvent
  | GestureEvent
  | CalibrationEvent
  | HandsErrorEvent

export type HandsConfigCommand = {
  engage?: EngageMode
  displays?: DisplaySelection
  hand?: 'right' | 'left' | 'any'
  anchor?: 'knuckles' | 'index'
  overlay?: boolean
} & Partial<TuningValues>
type Empty = Record<string, never>

export type HandsCommandBodies = {
  heartbeat: Empty
  status: Empty
  config: HandsConfigCommand
  pause: Empty
  resume: Empty
  engage: Empty
  disengage: Empty
  calibrate: { action: 'start' | 'cancel' }
  shutdown: Empty
}
export type HandsCommandName = keyof HandsCommandBodies

export type HandsCommandResponse = {
  ok: boolean
  /** pause and resume: the camera is still being closed or opened as the helper answers. */
  pending?: true
  error?: { code: HandsErrorCode; message: string }
  [extra: string]: unknown
}

export type HandsStatusResponse = {
  ok: true
  state: HandsState
  version: string
  platform: string
  camera?: string
  engaged: boolean
  /** Frames tracked per second, last few seconds. */
  fps: number
  /** Mean hand-landmark time per frame. */
  inferMs: number
  displays: HandsDisplay[]
  calibrated?: boolean
  settings: { engage: EngageMode; hand: string; anchor: string; overlay: boolean } & Partial<TuningValues>
}

const HANDS_STATES: ReadonlySet<string> = new Set<HandsState>(['starting', 'idle', 'active', 'paused', 'calibrating', 'error'])
const CALIBRATION_STEPS: ReadonlySet<string> = new Set<CalibrationStep>([
  'top_left',
  'top_right',
  'bottom_right',
  'bottom_left',
  'done',
  'cancelled',
])

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value)

const integer = (value: unknown): number => (Number.isInteger(value) ? (value as number) : 0)

const words = (text: string): string[] => text.toLowerCase().match(/[a-z0-9]+/g) ?? []

/**
 * An error's message with its hint in brackets, unless the hint only says the
 * message again (in the same words or nearly: most of its words are in it).
 */
export function describeHandsError(message: string, hint: string | undefined): string {
  const hinted = words(hint ?? '')
  if (hinted.length === 0) return message
  const said = new Set(words(message))
  const isRepeat = hinted.filter(word => said.has(word)).length >= hinted.length * 0.8
  return isRepeat ? message : `${message} (${hint})`
}

function toDisplay(value: unknown): HandsDisplay | undefined {
  if (!isRecord(value) || !Number.isInteger(value.id) || typeof value.name !== 'string') return undefined
  return {
    id: value.id as number,
    name: value.name,
    x: integer(value.x),
    y: integer(value.y),
    width: integer(value.width),
    height: integer(value.height),
    primary: value.primary === true,
    virtual: value.virtual === true,
    used: value.used === true,
  }
}

/** The displays of a ready event or a status answer; undefined when it carries no list. */
export function parseDisplays(value: unknown): HandsDisplay[] | undefined {
  if (!Array.isArray(value)) return undefined
  return value.map(toDisplay).filter((display): display is HandsDisplay => display !== undefined)
}

/**
 * Parses one stdout line of the hand helper into an event. Checks the
 * envelope and the fields the mod relies on; anything else (a stray print, a
 * newer event type) is undefined and ignored.
 */
export function parseHandsEvent(line: string): HandsEvent | undefined {
  let value: unknown
  try {
    value = JSON.parse(line)
  } catch {
    return undefined
  }
  if (!isRecord(value) || value.v !== 1) return undefined
  switch (value.type) {
    case 'hello':
      return Number.isInteger(value.port) ? (value as HandsHelloEvent) : undefined
    case 'state':
      return typeof value.state === 'string' && HANDS_STATES.has(value.state) ? (value as HandsStateEvent) : undefined
    case 'ready': {
      const displays = parseDisplays(value.displays)
      if (displays === undefined) return undefined
      const camera = typeof value.camera === 'string' ? value.camera : String(value.camera ?? 'camera')
      const fps = typeof value.fps === 'number' ? value.fps : 0
      return { ...(value as HandsReadyEvent), camera, fps, displays }
    }
    case 'gesture':
      return typeof value.name === 'string' ? (value as GestureEvent) : undefined
    case 'calibration':
      return typeof value.step === 'string' && CALIBRATION_STEPS.has(value.step) ? (value as CalibrationEvent) : undefined
    case 'error':
      return typeof value.code === 'string' && typeof value.message === 'string'
        ? { ...(value as HandsErrorEvent), fatal: value.fatal === true }
        : undefined
    default:
      return undefined
  }
}

// ---- Tuning: the sensitivity knobs ----

type KnobFields = {
  /** What the commands call it; its key, its label and the aliases name it too, in any case and spelling. */
  name: string
  label: string
  min: number
  max: number
  /** The value the helper starts with, which reproduces how hand control behaved before the knobs. */
  default: number
  /** Said after "Higher means": what a higher value does. */
  higher: string
  aliases: readonly string[]
  /** Shown after the number and its range when it has one (seconds, pixels). */
  unit?: string
  /** An everyday knob has a plugin setting (plugin.json userConfig) of this name; the others are for /jarvis hands set. */
  setting?: string
}

/**
 * The knobs, as protocol hands.schema.json's config command and status
 * settings know them (the `key`s). Their ranges and defaults mirror
 * KNOBS in the helper's settings.py, which is authoritative and refuses what
 * is outside; the mod checks first so the user hears the range. One line
 * each: a new knob is a new line here, plus its plugin setting in plugin.json
 * when it is an everyday one.
 */
const ROWS = [
  { key: 'cursorSpeed', name: 'speed', label: 'Cursor speed', min: 0.7, max: 3, default: 1, higher: 'a faster cursor, with less hand travel to cross the screen', aliases: ['cursor', 'pointer', 'mouse', 'mouse speed', 'gain'], setting: 'handCursorSpeed' },
  { key: 'smoothing', name: 'smoothing', label: 'Smoothing', min: 0.2, max: 3, default: 1, higher: 'a steadier cursor at rest, but more lag when you move', aliases: ['smooth', 'smoothness', 'steady', 'steadiness'], setting: 'handSmoothing' },
  { key: 'pinch', name: 'pinch', label: 'Pinch sensitivity', min: 0.85, max: 1.15, default: 1, higher: 'a lighter, looser pinch counts as a click', aliases: ['click', 'clicks', 'clicking'], setting: 'handPinch' },
  { key: 'fist', name: 'fist', label: 'Fist sensitivity', min: 0.9, max: 1.1, default: 1, higher: 'a looser fist counts as a grab', aliases: ['grab', 'grip'] },
  { key: 'engageSeconds', name: 'engage-time', label: 'Palm hold time', min: 0.1, max: 2, default: 0.5, unit: 's', higher: 'a longer hold with an open palm before it takes the cursor', aliases: ['engage', 'palm', 'hold', 'delay'] },
  { key: 'dragDistance', name: 'drag-distance', label: 'Drag distance', min: 0.7, max: 4, default: 1, higher: 'steadier clicks: your hand may drift further during a pinch before it becomes a drag', aliases: ['drag', 'slop'] },
  { key: 'flingSensitivity', name: 'fling', label: 'Fling sensitivity', min: 0.5, max: 2.5, default: 1, higher: 'a lighter flick throws a grabbed window', aliases: ['throw', 'flick'] },
  { key: 'scrollSpeed', name: 'scroll-speed', label: 'Scroll speed', min: 0.1, max: 10, default: 1, higher: 'faster scrolling', aliases: ['scroll', 'scrolling'], setting: 'handScrollSpeed' },
  { key: 'deadZone', name: 'dead-zone', label: 'Dead zone', min: 0, max: 8, default: 1, unit: 'px', higher: 'a steadier cursor at rest, but coarser small moves', aliases: ['dead', 'deadband'] },
] as const satisfies readonly (KnobFields & { key: string })[]

/** The wire key of a knob: the field of the config command and of the status settings. */
export type KnobKey = (typeof ROWS)[number]['key']
export type Knob = KnobFields & { key: KnobKey }
export const KNOBS: readonly Knob[] = ROWS

/** Every knob's value. */
export type TuningValues = Record<KnobKey, number>

export const PRESET_NAMES = ['precise', 'balanced', 'fast'] as const
export type PresetName = (typeof PRESET_NAMES)[number]

/** The preset a word names, in any case ("default" is balanced); undefined for any other word. */
export function presetNamed(word: string): PresetName | undefined {
  const lower = word.toLowerCase()
  return lower === 'default' ? 'balanced' : PRESET_NAMES.find(one => one === lower)
}

/**
 * Ready-made sets of values. A preset is a whole profile: the knobs it does
 * not name are at their defaults, so the tuning is either one of these or custom.
 */
export const PRESETS: Record<PresetName, Partial<TuningValues>> = {
  precise: { cursorSpeed: 0.8, smoothing: 1.8, deadZone: 2, dragDistance: 1.5, pinch: 0.9 },
  balanced: {},
  fast: { cursorSpeed: 1.6, smoothing: 0.5, deadZone: 0, pinch: 1.1 },
}

/** Three decimals are plenty, and keep 1.1 + 0.2 from showing as 1.3000000000000003. */
const tidy = (value: number): number => Math.round(value * 1000) / 1000
const same = (a: number, b: number): boolean => Math.abs(a - b) < 1e-9
const number = (value: number): string => String(tidy(value))
const withUnit = (knob: Knob, value: number): string => (knob.unit === undefined ? number(value) : `${number(value)} ${knob.unit}`)
const rangeText = (knob: Knob): string => `${number(knob.min)} to ${withUnit(knob, knob.max)}`

const squash = (text: string): string => text.toLowerCase().replace(/[^a-z0-9]/g, '')
const KNOB_BY_WORD: ReadonlyMap<string, Knob> = new Map(
  KNOBS.flatMap(knob => [knob.name, knob.key, knob.label, ...knob.aliases].map(word => [squash(word), knob] as const)),
)

/** The knob a word names: its name, key, label or an alias, whatever the case, spaces, dashes and underscores. */
export function findKnob(text: string): Knob | undefined {
  const word = squash(text)
  return word === '' ? undefined : KNOB_BY_WORD.get(word)
}

export type KnobValue = { ok: true; value: number | 'default' } | { ok: false; why: 'not_number' | 'range' }

const DECIMAL = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$/

/**
 * A knob's new value from what the user typed or the model sent: "default",
 * or a plain decimal number inside the knob's range (not 1e3, 0x10, NaN or
 * Infinity; a number outside the range is refused, never clamped).
 */
export function parseKnobValue(knob: Knob, input: unknown): KnobValue {
  let value: number
  if (typeof input === 'number') {
    value = input
  } else if (typeof input === 'string') {
    const text = input.trim()
    if (text.toLowerCase() === 'default') return { ok: true, value: 'default' }
    if (!DECIMAL.test(text)) return { ok: false, why: 'not_number' }
    value = Number(text)
  } else {
    return { ok: false, why: 'not_number' }
  }
  if (!Number.isFinite(value)) return { ok: false, why: 'not_number' }
  if (value < knob.min || value > knob.max) return { ok: false, why: 'range' }
  return { ok: true, value: tidy(value) }
}

/** "a, b and c" (or "a, b or c"). */
function listWords(items: readonly string[], joiner: 'and' | 'or' = 'and'): string {
  return items.length < 2 ? items.join('') : `${items.slice(0, -1).join(', ')} ${joiner} ${items.at(-1)}`
}

/** The preset a set of values is exactly, if any. */
function presetOf(values: TuningValues): PresetName | undefined {
  return PRESET_NAMES.find(name => KNOBS.every(knob => same(values[knob.key], PRESETS[name][knob.key] ?? knob.default)))
}

/** The knobs that are not at their built-in default, as `config` carries them. */
function changedValues(values: TuningValues): Partial<TuningValues> {
  const changed: Partial<TuningValues> = {}
  for (const knob of KNOBS) if (!same(values[knob.key], knob.default)) changed[knob.key] = values[knob.key]
  return changed
}

/** "speed 1.4, smoothing 1.8": the knobs' values by name, in table order. */
function describeValues(values: Partial<TuningValues>): string {
  return KNOBS.flatMap(knob => {
    const value = values[knob.key]
    return value === undefined ? [] : [`${knob.name} ${withUnit(knob, value)}`]
  }).join(', ')
}

// ---- The supervisor ----

const COMMAND_TIMEOUT_MS: Record<HandsCommandName, number> = {
  heartbeat: 1500,
  status: 5000,
  config: 5000,
  // The helper answers these within its own waits for the camera (about 4 s
  // to close it, 9 s to open it), with `pending` when it is still at it: a
  // webcam can take 20 s to open on Windows.
  pause: 10_000,
  resume: 15_000,
  engage: 2000,
  disengage: 2000,
  calibrate: 3000,
  shutdown: 2000,
}

/** Fatal errors no restart cures (the platform, a missing model): the supervisor gives up at once. */
const FINAL_ERRORS: ReadonlySet<string> = new Set<HandsErrorCode>(['unsupported_platform', 'model_missing'])

export type HandsHelperPhase = 'stopped' | 'not_installed' | 'starting' | 'running' | 'restarting' | 'elsewhere' | 'failed'

export type HandsCommandOutcome =
  | { ok: true; response: HandsCommandResponse }
  | { ok: false; code: HandsErrorCode | 'not_running' | 'timeout' | 'http'; message: string }

export type HandsHelperOptions = {
  platform: Platform
  /** NO_PROXY as the session has it; the child's gains the loopback hosts. */
  noProxy?: string
  /** `--camera`, read at each start: an index or part of a camera's name; undefined opens the first. */
  camera: () => Promise<string | undefined>
  /** Settings sent with `config` as soon as the helper says hello. */
  initialConfig: () => Promise<HandsConfigCommand>
  /**
   * The helper refused that config (bad_request) and nothing of it applied:
   * the config to send instead, or undefined to leave it. A helper older than
   * a setting refuses the whole command, which would cost the rest of it too.
   */
  onConfigRefused?: (config: HandsConfigCommand, message: string) => HandsConfigCommand | undefined
  /** Whether the user paused hand control: `pause` then goes out before `config`, before the camera opens. */
  isPaused: () => Promise<boolean>
  onEvent: (event: HandsEvent) => void
  /** `isFinal`: a failure no restart cures (FINAL_ERRORS), so the user is not told to restart. */
  onPhase: (phase: HandsHelperPhase, detail?: string, isFinal?: boolean) => void
  /** Why no child may start now (pc.ts whyHeld: Claude Code runs elevated, or the administrator check has not passed). */
  whyHeld: () => string | undefined
}

type Exit = {
  result: ProcessSpawnResult | undefined
  failure: string | undefined
  sawAlreadyRunning: boolean
  isFinal: boolean
  uptime: number
}

/**
 * The hand helper's supervisor: the voice helper's contract (helper.ts) for
 * `python -m jarvis_hands run`. Spawns it, reads its JSON-lines events, sends
 * commands over loopback HTTP, keeps it alive with heartbeats and restarts it
 * with backoff when it dies unexpectedly.
 */
export class HandsHelper {
  phase: HandsHelperPhase = 'stopped'
  /** The running helper's hello, once it has said it. */
  hello: HandsHelloEvent | undefined

  private token = ''
  private generation = 0
  /** Counts stop() calls: a restart starts again only if no other stop came while it stopped. */
  private stops = 0
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
    private readonly options: HandsHelperOptions,
  ) {}

  get isRunning(): boolean {
    return this.phase === 'running' && this.hello !== undefined
  }

  /**
   * Starts the helper unless it runs or is about to. Automatic starts leave a
   * helper that gave up, or one owned by another window, alone; a start the
   * user asked for (`/jarvis hands on`, `restart`) always tries again.
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
   * Resolves false when the child outlived both waits and was let go; its
   * heartbeat watchdog ends it (and lets go of the camera) soon after.
   */
  async stop(): Promise<boolean> {
    this.stops += 1
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

  /**
   * Stops the helper, then starts it again, unless another stop came while
   * it stopped (hand control turned off, a setup began: theirs wins) or
   * `shouldStart` says no once it has stopped.
   */
  async restart(shouldStart: () => Promise<boolean> = async () => true): Promise<void> {
    const stopping = this.stop()
    const stops = this.stops
    await stopping
    if (this.stops !== stops || !(await shouldStart()) || this.stops !== stops) return
    this.start({ userInitiated: true })
  }

  /** Sends one command to the running helper; never rejects. */
  async send<N extends HandsCommandName>(
    name: N,
    body: HandsCommandBodies[N],
    timeoutMs?: number,
  ): Promise<HandsCommandOutcome> {
    const hello = this.hello
    if (hello === undefined) return { ok: false, code: 'not_running', message: 'The hand helper is not running.' }
    return this.post(hello.port, this.token, name, body, timeoutMs ?? COMMAND_TIMEOUT_MS[name])
  }

  private async post(
    port: number,
    token: string,
    name: HandsCommandName,
    body: unknown,
    timeoutMs: number,
  ): Promise<HandsCommandOutcome> {
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
    let parsed: HandsCommandResponse | undefined
    try {
      const value: unknown = JSON.parse(response.text)
      if (isRecord(value) && 'ok' in value) parsed = value as HandsCommandResponse
    } catch {
      // not JSON: judged by the status below
    }
    if (parsed?.ok === true) return { ok: true, response: parsed }
    if (isRecord(parsed?.error) && typeof parsed.error.message === 'string') {
      return { ok: false, code: parsed.error.code, message: parsed.error.message }
    }
    return { ok: false, code: 'http', message: `${name}: HTTP ${response.status}` }
  }

  private launch(): void {
    // Never as administrator, nor before the check has passed (pc.ts holdsHelper): every start and retry comes here.
    const held = this.options.whyHeld()
    if (held !== undefined) {
      this.engine.debug(`jarvis: the hand helper was not started: ${held}`)
      if (this.phase !== 'stopped') this.setPhase('stopped')
      return
    }
    this.generation += 1
    const generation = this.generation
    this.running = this.runOnce(generation)
      .catch((error: unknown) => this.engine.debug(`jarvis: hand helper loop failed: ${describeError(error)}`))
      .finally(() => {
        if (this.generation === generation) this.running = undefined
      })
  }

  private kill(): void {
    void this.stream?.return({ code: null, signal: null }).catch(() => undefined)
  }

  /**
   * Lets go of the current child without waiting for its loop (a silent
   * child would hold `running` indefinitely); what it still says is ignored
   * (stale generation), and the next start allows for its lock.
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
    if (!(await this.engine.exists(platform.handsVenvPython).catch(() => false))) {
      // A stop() that came meanwhile wants "stopped", and must not leave its flag for the next start.
      if (isCurrent() && !this.finishStop()) this.setPhase('not_installed')
      return
    }

    const startedAt = await this.engine.now()
    const reader = new LineReader()
    const exit: Exit = { result: undefined, failure: undefined, sawAlreadyRunning: false, isFinal: false, uptime: 0 }
    const camera = await this.options.camera().catch(() => undefined)
    // stop() may have come while this run got ready: start nothing.
    if (!isCurrent() || this.finishStop()) return

    this.token = randomToken()
    this.stderrTail = []
    this.lastFatal = undefined
    const stream = this.engine.spawn({
      argv: [
        platform.handsVenvPython,
        '-m',
        'jarvis_hands',
        'run',
        '--data-dir',
        platform.dataDir,
        ...(camera === undefined ? [] : ['--camera', camera]),
      ],
      cwd: platform.dataDir,
      env: this.childEnv(),
    })
    this.stream = stream
    // A helper that never says hello (hung opening the camera, say) is let go
    // and the restart policy acts now.
    const helloTimer = this.engine.after(HELLO_TIMEOUT_MS, () => {
      if (!isCurrent() || this.hello !== undefined) return
      exit.failure = `no hello within ${HELLO_TIMEOUT_MS / 1000} s`
      exit.uptime = HELLO_TIMEOUT_MS
      this.abandon()
      this.afterExit(exit)
    })
    const handle = (line: string): void => {
      if (!isCurrent()) return // a child we let go of
      const event = parseHandsEvent(line)
      if (event === undefined) {
        this.engine.debug(`jarvis: hand helper stdout (not an event): ${line.slice(0, 200)}`)
        return
      }
      if (event.type === 'error' && event.code === 'already_running') exit.sawAlreadyRunning = true
      if (event.type === 'error' && event.fatal && FINAL_ERRORS.has(event.code)) exit.isFinal = true
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
    await this.engine.writeHandsRef(null).catch(() => undefined)
    exit.uptime = (await this.engine.now()) - startedAt
    if (isCurrent()) this.afterExit(exit)
  }

  /** Ends a stop() in progress, if one is: true when the run ends as "stopped". */
  private finishStop(): boolean {
    if (!this.isStopping) return false
    this.isStopping = false
    this.setPhase('stopped')
    return true
  }

  private afterExit(exit: Exit): void {
    if (this.finishStop()) return
    const code = exit.result?.code ?? null
    if (exit.sawAlreadyRunning || code === ALREADY_RUNNING_EXIT) {
      // The lock may still be held by a helper of ours that lost its parent
      // (a reload) or was let go; its watchdog ends it. Retry late, then it is real.
      if (this.orphanRetries > 0) {
        this.orphanRetries -= 1
        this.scheduleLaunch(ORPHAN_RETRY_MS, 'waiting for the previous hand helper to exit')
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
    if (wait === undefined || exit.isFinal) {
      this.setPhase('failed', why, exit.isFinal)
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

  private handleEvent(event: HandsEvent): void {
    if (event.type === 'hello') {
      this.hello = event
      this.onHello(event)
    } else if (event.type === 'error' && event.fatal) {
      this.lastFatal = describeHandsError(event.message, event.hint)
    }
    try {
      this.options.onEvent(event)
    } catch (error) {
      this.engine.debug(`jarvis: hands event handler failed on ${event.type}: ${describeError(error)}`)
    }
  }

  private onHello(hello: HandsHelloEvent): void {
    this.orphanRetries = 0
    this.setPhase('running')
    this.heartbeat?.cancel()
    this.heartbeat = this.engine.every(HEARTBEAT_MS, () => {
      void this.send('heartbeat', {})
    })
    const ref: JarvisHelperRef = { port: hello.port, token: this.token, pid: hello.pid }
    void this.engine.writeHandsRef(ref).catch(() => undefined)
    void this.sendFirst().catch((error: unknown) => this.engine.debug(`jarvis: hands config not sent: ${describeError(error)}`))
  }

  /**
   * What a helper that just said hello hears first: `pause` while the user
   * has hand control paused (it arrives before the camera opens, which keeps
   * it closed), then the settings. The config does not wait for the pause's
   * answer, which may wait on the camera.
   */
  private async sendFirst(): Promise<void> {
    if (await this.options.isPaused()) {
      void this.send('pause', {}).then(outcome => {
        if (!outcome.ok) this.engine.debug(`jarvis: hands pause not applied: ${outcome.message}`)
      })
    }
    const config = await this.options.initialConfig()
    let outcome = await this.send('config', config)
    if (!outcome.ok && outcome.code === 'bad_request') {
      const instead = this.options.onConfigRefused?.(config, outcome.message)
      if (instead !== undefined) outcome = await this.send('config', instead)
    }
    if (!outcome.ok) this.engine.debug(`jarvis: hands config not applied: ${outcome.message}`)
  }

  /**
   * A reloaded module finds the previous hand helper's address in session
   * state: that helper lost its parent but may still run, holding the camera
   * and the lock.
   */
  private async shutdownPrevious(): Promise<void> {
    const previous = await this.engine.readHandsRef().catch(() => null)
    if (previous === null) return
    this.orphanRetries = ORPHAN_RETRIES
    const outcome = await this.post(previous.port, previous.token, 'shutdown', {}, COMMAND_TIMEOUT_MS.shutdown)
    await this.engine.writeHandsRef(null).catch(() => undefined)
    if (outcome.ok) await delay(this.engine, 1000)
  }

  private childEnv(): Record<string, string> {
    return {
      PYTHONUNBUFFERED: '1',
      PYTHONUTF8: '1',
      JARVIS_TOKEN: this.token,
      JARVIS_PARENT: 'claude-code',
      NO_PROXY: withLoopbackNoProxy(this.options.noProxy),
      // Media Foundation opens a webcam in about a second instead of ten or more.
      OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS: '0',
    }
  }

  private keepStderr(text: string): void {
    for (const line of text.split(/\r?\n/)) {
      if (line.trim() === '') continue
      this.stderrTail.push(line.slice(0, 300))
      this.engine.debug(`jarvis: hand helper: ${line.slice(0, 300)}`)
    }
    if (this.stderrTail.length > 20) this.stderrTail.splice(0, this.stderrTail.length - 20)
  }

  private setPhase(phase: HandsHelperPhase, detail?: string, isFinal = false): void {
    this.phase = phase
    this.options.onPhase(phase, detail, isFinal)
  }
}

// ---- Setup ----

/** The hand helper's distribution name in hands/pyproject.toml. */
export const HANDS_PACKAGE = 'jarvis-hands'

/**
 * <dataDir>/hands/installed.json: what the last /jarvis setup hands
 * installed, `{ pluginVersion }`. The venv holds a copy of the plugin's hands
 * package (a non-editable install), so a plugin update leaves the helper as it
 * was until the setup runs again; this record is how the mod tells.
 */
export const HANDS_INSTALLED_FILE = 'installed.json'
type InstalledRecord = { pluginVersion?: string }

/** The `uv sync` argv for the hand helper project (exported for tests). */
export function handsUvSyncArgv(uv: string, project: string, { isFrozen }: { isFrozen: boolean }): string[] {
  return [
    uv,
    'sync',
    '--project',
    project,
    '--python',
    '3.12',
    '--no-dev',
    '--no-editable',
    // A non-editable install is not rebuilt while its version stays the same.
    '--reinstall-package',
    HANDS_PACKAGE,
    ...(isFrozen ? ['--frozen'] : []),
  ]
}

export type HandsSetupRequest = {
  /** This plugin's version, recorded once the install succeeded (undefined: unknown). */
  pluginVersion: string | undefined
  onProgress: (text: string) => void
  debug: (line: string) => void
}

/**
 * /jarvis setup hands: the hand helper in its own Python 3.12 venv
 * (<dataDir>/hands/venv, about 500 MB with MediaPipe and OpenCV), then its
 * hand model (8 MB, checked by size and SHA-256) through the helper itself.
 */
export async function runHandsSetup(
  engine: Engine,
  platform: Platform,
  uv: string,
  request: HandsSetupRequest,
): Promise<SetupResult> {
  const { onProgress, debug } = request
  const handsDir = joinPath(platform.sep, platform.dataDir, 'hands')
  // Creates the folders (fs.write makes parents): the model setup runs in the data folder.
  await engine.writeFile(
    joinPath(platform.sep, handsDir, 'README.txt'),
    'Jarvis hand control keeps its helper here: venv/ (its own Python environment), calibration.json, and installed.json (the version of Jarvis that installed it). The hand model is in ../models/hands.\nDelete this folder and ../models/hands to uninstall hand control; /jarvis setup hands recreates them.\n',
  )

  const project = joinPath(platform.sep, engine.pluginRoot, 'hands')
  const isFrozen = await engine.exists(joinPath(platform.sep, project, 'uv.lock')).catch(() => false)
  const env = {
    UV_PROJECT_ENVIRONMENT: platform.handsVenvDir,
    UV_CACHE_DIR: joinPath(platform.sep, platform.dataDir, 'uv-cache'),
    UV_NO_PROGRESS: '1',
  }
  const tail: string[] = []
  const keep = (line: string): void => {
    tail.push(line.slice(0, 300))
    if (tail.length > 8) tail.shift()
  }

  onProgress('installing the hand helper (Python 3.12, MediaPipe and OpenCV)')
  let code: number | null
  try {
    code = await runStreaming(
      engine,
      { argv: handsUvSyncArgv(uv, project, { isFrozen }), cwd: project, env },
      (_stream, line) => {
        keep(line)
        debug(`uv: ${line}`)
        onProgress(`installing · ${line.trim().slice(0, 80)}`)
      },
    )
  } catch (error) {
    return { ok: false, reason: 'uv_failed', message: `uv could not run: ${describeError(error)}` }
  }
  if (code !== 0) {
    return { ok: false, reason: 'uv_failed', message: `uv sync failed (exit ${code ?? 'signal'}):\n${tail.join('\n')}` }
  }

  onProgress('downloading the hand model')
  tail.length = 0
  let lastProgress = ''
  let failure: string | undefined
  try {
    code = await runStreaming(
      engine,
      {
        argv: [platform.handsVenvPython, '-m', 'jarvis_hands', 'setup', '--data-dir', platform.dataDir],
        cwd: platform.dataDir,
        env: { PYTHONUNBUFFERED: '1', PYTHONUTF8: '1' },
      },
      (stream, line) => {
        const progress = stream === 'stdout' ? parseProgress(line) : undefined
        if (progress !== undefined) {
          lastProgress = progress.text
          if (progress.step === 'error') failure = progress.text
          onProgress(progress.text)
        } else {
          keep(line)
          debug(`hands setup: ${line}`)
        }
      },
    )
  } catch (error) {
    return { ok: false, reason: 'model_failed', message: `the hand helper could not run: ${describeError(error)}` }
  }
  if (code !== 0) {
    const details = [failure, ...tail].filter(line => line !== undefined).join('\n')
    return { ok: false, reason: 'model_failed', message: `the hand helper's setup failed (exit ${code ?? 'signal'}):\n${details}` }
  }
  const record: InstalledRecord = { pluginVersion: request.pluginVersion }
  await engine.writeFile(joinPath(platform.sep, handsDir, HANDS_INSTALLED_FILE), `${JSON.stringify(record)}\n`)
  return { ok: true, summary: lastProgress === '' ? 'installed' : lastProgress }
}

// ---- The controller ----

export const ENGAGE_MODES = ['palm', 'always'] as const satisfies readonly EngageMode[]

/** The plugin settings for hand control (userConfig); /jarvis hands choices override them. */
export type HandsSettings = {
  isOn: boolean
  camera?: string
  engage: EngageMode
  /** The everyday knobs' plugin settings that are numbers in range: the defaults /jarvis hands set overrides. */
  tuning: Partial<TuningValues>
  /** What is wrong with the other plugin settings, in words: they use the knob's default instead. */
  badTuning: string[]
}

/** Reads hand control's userConfig values, defaults filled in. */
export function readHandsSettings(options: PluginOptions): HandsSettings {
  const text = (key: string): string | undefined => {
    const value = options[key]
    return typeof value === 'string' && value.trim() !== '' ? value.trim() : undefined
  }
  const tuning: Partial<TuningValues> = {}
  const badTuning: string[] = []
  for (const knob of KNOBS) {
    const raw = knob.setting === undefined ? undefined : text(knob.setting)
    if (knob.setting === undefined || raw === undefined) continue
    const parsed = parseKnobValue(knob, raw)
    if (!parsed.ok) {
      badTuning.push(
        `The ${knob.setting} setting "${raw.slice(0, 40)}" is not a number from ${rangeText(knob)}, so the default, ${withUnit(knob, knob.default)}, is used.`,
      )
    } else if (parsed.value !== 'default') {
      tuning[knob.key] = parsed.value
    }
  }
  return {
    isOn: text('handControl') === 'on',
    camera: text('handCamera'),
    engage: ENGAGE_MODES.find(mode => mode === text('handEngage')) ?? 'palm',
    tuning,
    badTuning,
  }
}

/**
 * /jarvis hands on|off's choice, kept with the handControl setting it
 * overrode: it lapses (and is dropped) once the setting changes, so the
 * setting always has the last word. A choice equal to the setting is none.
 */
const ENABLED_KEY = 'handsEnabled'
type EnabledOverride = { isOn: boolean; setting: boolean }
/**
 * /jarvis hands pause, until resume, on or off: kept in $.store, so a helper
 * that restarts (a crash, a camera change, a reload, the next session) is
 * paused again before its camera opens. "The camera is off until resume".
 */
const PAUSED_KEY = 'handsPaused'
const CAMERA_KEY = 'handsCamera'
const ENGAGE_KEY = 'handsEngage'
const DISPLAYS_KEY = 'handsDisplays'
/**
 * /jarvis hands set|preset|reset's choices, per knob: `{ value, setting }`,
 * kept with the plugin setting (or built-in default) they overrode, like
 * /jarvis hands on|off's. A choice lapses once its setting changes, and one
 * equal to the setting is none.
 */
const TUNING_KEY = 'handsTuning'
type TuningChoice = { value: number; setting: number }
type TuningChoices = Partial<Record<KnobKey, TuningChoice>>
/** What the tuning is now: each knob's value, what it is without a command's choice, and the choices that hold. */
type TuningState = { values: TuningValues; defaults: TuningValues; choices: TuningChoices }
/** A non-fatal error is toasted once per code in this long. */
const ERROR_TOAST_INTERVAL_MS = 60_000
/** Errors that say why the camera is off (a reopen that failed): shown after "hands paused" while it is. */
const CAMERA_ERRORS: ReadonlySet<string> = new Set<HandsErrorCode>([
  'no_camera',
  'camera_blocked',
  'camera_in_use',
  'camera_lost',
  'internal',
])
const CALIBRATION_TOAST_MS = 10_000

/** The view phases the running helper's own state events set. */
const HELPER_STATES: ReadonlySet<HandsPhase> = new Set<HandsPhase>(['starting', 'idle', 'active', 'paused', 'calibrating', 'error'])

const CORNERS: Partial<Record<CalibrationStep, string>> = {
  top_left: 'top-left corner',
  top_right: 'top-right corner',
  bottom_right: 'bottom-right corner',
  bottom_left: 'bottom-left corner',
}

const CALIBRATION_TEXT: Record<CalibrationStep, string> = {
  top_left: 'Hold your open hand at the top-left corner of the screen, knuckles on the target, until it moves on.',
  top_right: 'Now the top-right corner.',
  bottom_right: 'Now the bottom-right corner.',
  bottom_left: 'And the bottom-left corner.',
  done: 'Calibrated.',
  cancelled: 'Calibration cancelled; the previous calibration stays.',
}

/**
 * How long the palm is held, in words: the default's own words ("half a
 * second") when it is the default, else the number of seconds, since the
 * hold time is a knob (engage-time) and the instructions must not say a
 * time the user has changed.
 */
export function holdPhrase(seconds: number): string {
  if (same(seconds, 0.5)) return 'half a second'
  if (same(seconds, 1)) return 'one second'
  return `${number(seconds)} seconds`
}

const engageText = (mode: EngageMode, seconds: number): string =>
  mode === 'palm'
    ? `Hand control starts when you hold an open palm toward the camera for ${holdPhrase(seconds)}`
    : 'Any hand in view takes the cursor at once, with no open palm needed (for a projector room)'

/** What to do when another Claude Code window holds the hand helper's lock (and the camera). */
const ELSEWHERE_HINT = 'Turn it off there (/jarvis hands off), then run /jarvis hands restart here.'
const ELSEWHERE_TEXT = `Hand control is running in another Claude Code window. ${ELSEWHERE_HINT}`

const PHASE_TEXT: Record<HandsPhase, string> = {
  off: 'off',
  not_installed: 'on, but not set up yet: run /jarvis setup hands',
  setup: 'setting up',
  starting: 'starting the camera',
  idle: 'ready',
  active: 'active: your hand has the cursor',
  paused: 'paused: the camera is off until /jarvis hands resume',
  calibrating: 'calibrating',
  error: 'stopped by an error',
  restarting: 'restarting',
  elsewhere: `running in another Claude Code window. ${ELSEWHERE_HINT}`,
  failed: 'stopped: the hand helper kept failing; /jarvis hands restart tries again',
}

/** What the hands part of the view says in words (`/jarvis hands`). */
function phaseText(view: JarvisHandsView): string {
  // A failure no restart cures: the reason (the detail) says what to do.
  return view.phase === 'failed' && view.isFinal === true ? 'stopped' : PHASE_TEXT[view.phase]
}

const RESUMED = 'Hand control resumed: the camera is on again.'

const NOT_LOCAL =
  'Hand control runs on your own computer; this session runs in the cloud, so the hand helper is not started here.'

const HANDS_HELP = [
  '/jarvis hands                      hand control status: the camera, the displays and the gestures',
  '/jarvis hands on|off               turn hand control (and the camera) on or off',
  '/jarvis hands calibrate [cancel]   fit it to your reach: an open palm at each corner of the screen',
  '/jarvis hands display <n|all>      the displays your hand reaches (n can be a list, such as 1,2)',
  '/jarvis hands engage <palm|always> start with an open palm, or let any hand take the cursor at once',
  '/jarvis hands camera <n|name>      the camera to use: its number or part of its name (default: the first)',
  '/jarvis hands tune                 every sensitivity setting, its value and what it does',
  '/jarvis hands set <name> <number>  change one setting, such as: set speed 1.5 (or default)',
  '/jarvis hands preset <name>        precise, balanced or fast: a ready-made set of settings',
  '/jarvis hands reset [name]         put every setting (or one) back to its default',
  '/jarvis hands pause|resume         turn the camera off and on without turning hand control off',
  '/jarvis hands restart              restart the hand helper',
  '/jarvis setup hands                install hand control (about 500 MB)',
].join('\n')

const gestures = (holdSeconds: number): string => [
  `Open palm toward the camera, still for ${holdPhrase(holdSeconds)}: start (the cursor follows your hand)`,
  'Pinch thumb and index finger: click; pinch and move: drag; pinch twice: double-click',
  'Pinch thumb and middle finger: right-click',
  'Index and middle finger up, then move: scroll',
  'Fist over a window: grab and move it; a fist with each hand: resize it; fling it sideways: next display, up: maximize, down: minimize',
  'Drop your hand out of view, or touch the mouse: control lets go at once',
].join('\n')

const TOOL_ACTIONS = ['on', 'off', 'status', 'calibrate', 'pause', 'resume', 'engage', 'disengage'] as const
type ToolAction = (typeof TOOL_ACTIONS)[number]

const TUNING_DESCRIPTION = [
  'Sensitivity: setting names one sensitivity setting and value is its new number (or "default"); preset applies precise, balanced or fast. They come alone or with an action, apply after display and before the action, and are remembered. A setting alone reports its current value.',
  `Settings, each with its range and what a higher value means: ${KNOBS.map(knob => `${knob.name} ${number(knob.min)} to ${withUnit(knob, knob.max)} (${knob.higher}${knob.default === 1 ? '' : `; default ${withUnit(knob, knob.default)}`})`).join('; ')}. A default is 1 unless shown.`,
  'For a request such as "make the cursor faster" or "less jittery", read the setting first, change it by a modest step (about a fifth of the way to its limit, never straight to an extreme), then report the new value and what it does, and say they can ask for more or less. Status shows which settings differ from their defaults.',
].join(' ')

/** The tool the model calls for "Jarvis, turn on hand control" (`mcp__jarvis__hands`). */
export const HANDS_TOOL = {
  name: 'hands',
  description: `Jarvis hand control: the user's webcam watches their hands and turns gestures into mouse and window actions on their own computer (an open palm starts it, a pinch clicks, a fist grabs a window). It is separate from Jarvis's voice and off until turned on, since it uses the camera. Use this tool when the user asks to turn hand or gesture control on or off, to calibrate it, to pause or resume it, to choose the displays it reaches (such as a projector), to take or let go of the cursor, to make it more or less sensitive, or to check whether it is working. Actions: on starts the camera and hand tracking; off stops them; calibrate starts the four-corner calibration (the user holds an open palm at each corner of the screen); pause turns the camera off while hand control stays on, and resume turns it back on; engage gives the cursor to the user's hand now, with no open palm needed, and disengage takes it away; status reports the state, the camera and the displays with their numbers and names. display chooses the displays the hand reaches: all, a display number, or a list such as 1,2 (status lists them); it can come alone or with an action, and applies first. ${TUNING_DESCRIPTION} Relay the result to the user in a sentence or two.`,
  inputSchema: {
    type: 'object',
    properties: {
      action: {
        type: 'string',
        enum: [...TOOL_ACTIONS],
        description: 'What to do with hand control.',
      },
      display: {
        type: 'string',
        description: 'The displays the hand reaches: all, a display number such as 2, or a list such as 1,2.',
      },
      setting: {
        type: 'string',
        description: `A sensitivity setting to read or change: ${KNOBS.map(knob => knob.name).join(', ')}. With value it changes it; alone it reports it.`,
      },
      value: {
        type: ['number', 'string'],
        description: "The setting's new number, inside its range, or \"default\".",
      },
      preset: {
        type: 'string',
        enum: [...PRESET_NAMES],
        description: 'A ready-made set of all the sensitivity settings. Do not combine it with setting.',
      },
    },
    additionalProperties: false,
  },
}

/**
 * The engine port and a file read, which hand control needs for its install
 * record and the plugin's version (engine.ts's port has no read of its own).
 */
export type HandsEngine = Engine & { readFile: (path: string) => Promise<string> }

export type HandsOptions = {
  platform: Platform
  settings: HandsSettings
  /** False in a cloud session: nothing is started there. */
  isLocal: boolean
  /** NO_PROXY as the session has it. */
  noProxy?: string
  /** The voice helper, which speaks the calibration steps while it runs. */
  voice: () => Pick<Helper, 'isRunning' | 'send'> | undefined
  onView: (view: JarvisHandsView) => void
  /**
   * Why nothing of Jarvis's may start now (pc.ts whyHeld): Claude Code runs
   * elevated, or the administrator check is still running or failed.
   * Undefined when it may. The hand helper and its setup wait for it, as the
   * voice helper does.
   */
  whyHeld: () => string | undefined
}

/**
 * Hand control for the session: whether it is on (`/jarvis hands on` stores
 * that), the chosen camera, engage mode and displays; the hand helper's
 * supervisor; and the `hands` part of the view.
 */
export class Hands {
  readonly helper: HandsHelper
  readonly platform: Platform
  readonly isLocal: boolean
  view: JarvisHandsView = { phase: 'off' }
  /** The running helper's camera and displays, once it said ready. */
  ready: HandsReadyEvent | undefined
  isSetupRunning = false

  private engage: EngageMode
  private hasAnnouncedReady = false
  private lastError: string | undefined
  private corner: string | undefined
  private readonly mutedErrors = new Set<string>()
  /** A resume answered `pending`: the camera is still opening, and the user waits to hear how it went. */
  private isResumePending = false
  private hasCheckedInstall = false
  /** Says to run /jarvis setup hands, while the venv is an earlier version's. */
  private updateNotice: string | undefined
  private versionRead: Promise<string | undefined> | undefined
  /** Says the running helper refused the tuning at its start, so it runs with its defaults. */
  private tuningNote: string | undefined
  private hasWarnedAboutSettings = false

  constructor(
    readonly engine: HandsEngine,
    private readonly options: HandsOptions,
  ) {
    this.platform = options.platform
    this.isLocal = options.isLocal
    this.engage = options.settings.engage
    this.helper = new HandsHelper(engine, {
      platform: options.platform,
      noProxy: options.noProxy,
      camera: () => this.camera(),
      initialConfig: () => this.initialConfig(),
      onConfigRefused: (config, message) => this.withoutTuning(config, message),
      isPaused: () => this.isPaused(),
      onEvent: event => this.onEvent(event),
      onPhase: (phase, detail, isFinal) => this.onHelperPhase(phase, detail, isFinal),
      whyHeld: () => options.whyHeld(),
    })
  }

  /** Why the hand helper and its setup may not start now (elevated, or the administrator check has not passed); undefined when they may. */
  whyHeld(): string | undefined {
    return this.options.whyHeld()
  }

  /**
   * Whether hand control is on: /jarvis hands on|off's choice while the
   * handControl setting is still the one it overrode, else the setting.
   */
  async isEnabled(): Promise<boolean> {
    const stored = await this.engine.storeGet(ENABLED_KEY).catch(() => undefined)
    const setting = this.options.settings.isOn
    if (stored === undefined) return setting
    const override: Record<string, unknown> = isRecord(stored) ? stored : {}
    if (typeof override.isOn === 'boolean' && override.setting === setting) return override.isOn
    // Lapsed: the setting changed since. Dropped, so changing it back does not revive it.
    await this.engine.storeDelete(ENABLED_KEY).catch(() => undefined)
    return setting
  }

  /** Stores /jarvis hands on|off's choice against the setting it overrides; one equal to the setting is none. */
  private async setEnabled(isOn: boolean): Promise<void> {
    const setting = this.options.settings.isOn
    if (isOn === setting) {
      await this.engine.storeDelete(ENABLED_KEY)
      return
    }
    const override: EnabledOverride = { isOn, setting }
    await this.engine.storeSet(ENABLED_KEY, override)
  }

  /** Whether the user paused hand control (the camera stays off until resume, on or off). */
  async isPaused(): Promise<boolean> {
    return (await this.engine.storeGet(PAUSED_KEY).catch(() => undefined)) === true
  }

  private async setPaused(isPaused: boolean): Promise<void> {
    if (isPaused) await this.engine.storeSet(PAUSED_KEY, true)
    else await this.engine.storeDelete(PAUSED_KEY)
  }

  /** This plugin's version, from its plugin.json; undefined when it cannot be read. */
  pluginVersion(): Promise<string | undefined> {
    this.versionRead ??= this.engine
      .readFile(joinPath(this.platform.sep, this.engine.pluginRoot, '.claude-plugin', 'plugin.json'))
      .then(text => {
        const value: unknown = JSON.parse(text)
        const version = isRecord(value) ? value.version : undefined
        return typeof version === 'string' && version !== '' ? version : undefined
      })
      .catch(() => undefined)
    return this.versionRead
  }

  /** The camera /jarvis hands camera chose, else the handCamera setting; undefined: the first. */
  async camera(): Promise<string | undefined> {
    const stored = await this.engine.storeGet(CAMERA_KEY).catch(() => undefined)
    return typeof stored === 'string' && stored !== '' ? stored : this.options.settings.camera
  }

  /** How control starts: /jarvis hands engage's choice, else the handEngage setting. */
  async engageMode(): Promise<EngageMode> {
    const stored = await this.engine.storeGet(ENGAGE_KEY).catch(() => undefined)
    return ENGAGE_MODES.find(mode => mode === stored) ?? this.options.settings.engage
  }

  /** The displays /jarvis hands display chose; all (every real display) by default. */
  async displays(): Promise<DisplaySelection> {
    const stored = await this.engine.storeGet(DISPLAYS_KEY).catch(() => undefined)
    if (Array.isArray(stored) && stored.length > 0 && stored.every(id => Number.isInteger(id) && id >= 1)) {
      return stored as number[]
    }
    return 'all'
  }

  async isInstalled(): Promise<boolean> {
    return this.engine.exists(this.platform.handsVenvPython).catch(() => false)
  }

  /** A local surface drew: starts the hand helper when hand control is on. */
  async autoStart(): Promise<void> {
    if (!this.isLocal || this.whyHeld() !== undefined) return
    this.engage = await this.engageMode()
    if (await this.isEnabled()) this.helper.start()
  }

  async turnOn(): Promise<string> {
    if (!this.isLocal) return NOT_LOCAL
    // Nothing changes while Jarvis is held: the camera never runs under an administrator's token.
    const held = this.whyHeld()
    if (held !== undefined) return held
    await this.setEnabled(true)
    // "On" means the camera on: it ends a pause too.
    const wasPaused = await this.isPaused()
    await this.setPaused(false)
    if (this.isSetupRunning) return 'Hand control is on; the camera starts when setup is done.'
    if (!(await this.isInstalled())) {
      this.publish({ phase: 'not_installed' })
      return 'Hand control is on, but it is not set up yet. Run /jarvis setup hands (it downloads about 500 MB); the camera starts when it is done.'
    }
    if (this.helper.isRunning) {
      return wasPaused || this.view.phase === 'paused' ? await this.resumeRunning() : 'Hand control is already on.'
    }
    this.engage = await this.engageMode()
    const hold = await this.holdSeconds()
    const wasElsewhere = this.helper.phase === 'elsewhere'
    this.helper.start({ userInitiated: true })
    if (wasElsewhere) {
      return 'Hand control is on, but it was running in another Claude Code window; trying again here now. If that window still has it, turn it off there (/jarvis hands off), then run /jarvis hands restart here.'
    }
    const start = this.engage === 'always' ? 'raise a hand' : `hold an open palm toward the camera for ${holdPhrase(hold)}`
    return `Hand control is on. The camera starts in a moment; then ${start} to take the cursor. Nothing leaves this computer.`
  }

  async turnOff(): Promise<string> {
    if (!this.isLocal) return NOT_LOCAL
    await this.setEnabled(false)
    await this.setPaused(false)
    // The other window's helper is not told (nor does it watch the store).
    const wasElsewhere = this.helper.phase === 'elsewhere'
    // The supervisor's "stopped" turns the hands part of the view off.
    const isStopped = await this.helper.stop()
    if (wasElsewhere) {
      return 'Hand control is off here, but it still runs, with the camera, in another Claude Code window. Turn it off there with /jarvis hands off.'
    }
    return isStopped
      ? 'Hand control is off and the camera is closed.'
      : 'Hand control is off. The hand helper did not exit when asked; it closes the camera and exits by itself within a minute.'
  }

  async restart(): Promise<string> {
    const held = this.whyHeld()
    if (held !== undefined) return held
    if (this.isSetupRunning) return 'Hand control setup is running; the hand helper starts when it is done.'
    if (!(await this.isEnabled())) return 'Hand control is off. Turn it on with /jarvis hands on.'
    void this.restartHelper()
    return 'Restarting the hand helper.'
  }

  /** Restarts the helper, unless hand control was turned off or a setup began while the old one stopped. */
  private restartHelper(): Promise<void> {
    return this.helper.restart(async () => !this.isSetupRunning && (await this.isEnabled()))
  }

  async calibrate(choice: string | undefined): Promise<string> {
    const word = choice?.toLowerCase()
    if (word !== undefined && word !== 'start' && word !== 'cancel') {
      return `Unknown option "${choice}". Use /jarvis hands calibrate, or /jarvis hands calibrate cancel.`
    }
    const notRunning = await this.notRunning()
    if (notRunning !== undefined) return notRunning
    const action = word === 'cancel' ? 'cancel' : 'start'
    const outcome = await this.helper.send('calibrate', { action })
    if (!outcome.ok) return `Could not ${action} calibration: ${outcome.message}`
    return action === 'cancel'
      ? 'Calibration cancelled; the previous calibration stays.'
      : 'Calibrating: a target appears in each corner of the screen in turn (top-left, top-right, bottom-right, bottom-left). Hold an open palm with your knuckles on it, still, for a second each. /jarvis hands calibrate cancel stops.'
  }

  async pause(): Promise<string> {
    const notRunning = await this.notRunning()
    if (notRunning !== undefined) return notRunning
    const outcome = await this.helper.send('pause', {})
    if (!outcome.ok) return `Could not pause hand control: ${outcome.message}`
    this.isResumePending = false
    await this.setPaused(true)
    return outcome.response.pending === true
      ? 'Pausing hand control: the camera is still starting, and it turns off as soon as it has started. /jarvis hands resume turns it back on.'
      : 'Hand control paused: the camera is off. /jarvis hands resume turns it back on.'
  }

  async resume(): Promise<string> {
    // The user wants the camera on: whatever helper runs next opens it.
    if (this.isLocal) await this.setPaused(false)
    const notRunning = await this.notRunning()
    if (notRunning !== undefined) return notRunning
    return this.resumeRunning()
  }

  private async resumeRunning(): Promise<string> {
    const outcome = await this.helper.send('resume', {})
    if (!outcome.ok) return `Could not resume hand control: ${outcome.message}`
    // Still opening: this resume found the camera opening, or an earlier one
    // did and the helper's state has not said it is back since. A helper that
    // started paused never opened its camera, so it says "starting" meanwhile.
    const isOpening =
      (outcome.response.pending === true || this.isResumePending) &&
      (this.view.phase === 'paused' || this.view.phase === 'starting')
    if (!isOpening) return RESUMED
    // The helper says the rest: a state other than paused or starting once the
    // camera is on, or an error event if it cannot open it (onEvent tells the user).
    this.isResumePending = true
    return 'Resuming hand control: the camera is still opening, which can take up to about 20 seconds. A message says when it is on, or why it could not open.'
  }

  /** The tool's engage and disengage: gives the cursor to the hand now, or takes it away. */
  async setEngaged(isEngaged: boolean): Promise<string> {
    const notRunning = await this.notRunning()
    if (notRunning !== undefined) return notRunning
    if (this.view.phase === 'paused') return 'Hand control is paused, so the camera is off. Resume it first: /jarvis hands resume.'
    const outcome = await this.helper.send(isEngaged ? 'engage' : 'disengage', {})
    if (!outcome.ok) return `Could not ${isEngaged ? 'take' : 'let go of'} the cursor: ${outcome.message}`
    if (isEngaged) return 'Hand control has the cursor: it follows your hand as soon as one is in view.'
    const start = this.engage === 'always' ? 'Raise a hand' : 'Hold an open palm toward the camera'
    return `Hand control let go of the cursor. ${start} to take it again.`
  }

  async chooseDisplays(args: readonly string[]): Promise<string> {
    const known = this.ready?.displays ?? []
    const usage = 'Choose with /jarvis hands display <n|all> (n can be a list, such as 1,2).'
    if (args.length === 0) return `Hand control reaches ${describeSelection(await this.displays(), known)}. ${usage}`
    const selection = parseDisplaySelection(args.join(','))
    if (selection === undefined) {
      return `"${args.join(' ')}" is not a display. Use /jarvis hands display all, a display number such as 2, or a list such as 1,2.`
    }
    if (selection !== 'all' && known.length > 0) {
      const missing = selection.filter(id => !known.some(display => display.id === id))
      if (missing.length > 0) {
        const listed = known.map(display => `${display.id} (${display.name})`).join(', ')
        return `There is no display ${missing.join(', ')}. The displays are ${listed}.`
      }
    }
    if (selection === 'all') await this.engine.storeDelete(DISPLAYS_KEY)
    else await this.engine.storeSet(DISPLAYS_KEY, selection)
    const refused = await this.sendConfig({ displays: selection })
    const reach = `Hand control now reaches ${describeSelection(selection, known)}`
    if (refused === undefined) return `${reach}; it applies when hand control starts.`
    return refused === '' ? `${reach}.` : `Saved, but the hand helper refused it: ${refused}`
  }

  async chooseEngage(choice: string | undefined): Promise<string> {
    const usage = 'Switch with /jarvis hands engage palm or /jarvis hands engage always.'
    if (choice === undefined) return `${engageText(await this.engageMode(), await this.holdSeconds())}. ${usage}`
    const mode = ENGAGE_MODES.find(one => one === choice.toLowerCase())
    if (mode === undefined) return `Unknown choice "${choice}". Use /jarvis hands engage palm or /jarvis hands engage always.`
    await this.engine.storeSet(ENGAGE_KEY, mode)
    this.engage = mode
    this.publish(this.view) // the ready label says how to start
    const refused = await this.sendConfig({ engage: mode })
    if (refused) return `Saved, but the hand helper refused it: ${refused}`
    return `${engageText(mode, await this.holdSeconds())}.`
  }

  async chooseCamera(args: readonly string[]): Promise<string> {
    const usage = 'Choose with /jarvis hands camera <number or part of its name>, or /jarvis hands camera default.'
    if (args.length === 0) {
      const camera = await this.camera()
      const inUse = this.ready === undefined ? '' : ` (in use: ${this.ready.camera})`
      return `Camera: ${camera === undefined ? 'the first one' : `"${camera}"`}${inUse}. ${usage}`
    }
    // A name with spaces is often typed in quotes; the helper matches the bare name.
    const spec = unquote(args.join(' '))
    if (spec === '' || spec.length > 100 || /[\u0000-\u001f]/.test(spec)) {
      return `"${spec.slice(0, 100)}" is not a camera name. ${usage}`
    }
    const isReset = spec.toLowerCase() === 'default'
    if (isReset) await this.engine.storeDelete(CAMERA_KEY)
    else await this.engine.storeSet(CAMERA_KEY, spec)
    const camera = await this.camera()
    const label = camera === undefined ? 'the first camera' : `"${camera}"`
    const isRunning = this.helper.isRunning
    // A helper that gave up (a wrong camera is the likely first-run failure) tries again with the new one.
    if (!isRunning && (this.whyHeld() !== undefined || this.isSetupRunning || !(await this.isEnabled()) || !(await this.isInstalled()))) {
      return `Camera set to ${label}; it applies when hand control starts.`
    }
    void this.restartHelper()
    // A pause outlives the restart: the new helper keeps the camera closed.
    if (await this.isPaused()) {
      return `Camera set to ${label}. The hand helper restarts with it, still paused: /jarvis hands resume opens the camera.`
    }
    return `Camera set to ${label}. The hand helper ${isRunning ? 'restarts' : 'starts again'} to open it.`
  }

  // ---- Tuning ----

  /**
   * Each knob's value now: /jarvis hands set|preset's choice while the plugin
   * setting (else the built-in default) is still the one it overrode, else
   * that setting. A choice that lapsed, or is malformed, is dropped here.
   */
  async tuningState(): Promise<TuningState> {
    const stored = await this.engine.storeGet(TUNING_KEY).catch(() => undefined)
    const saved: Record<string, unknown> = isRecord(stored) ? stored : {}
    let isStale = stored !== undefined && !isRecord(stored)
    const values = {} as TuningValues
    const defaults = {} as TuningValues
    const choices: TuningChoices = {}
    for (const knob of KNOBS) {
      const base = this.options.settings.tuning[knob.key] ?? knob.default
      defaults[knob.key] = base
      values[knob.key] = base
      const choice = saved[knob.key]
      if (choice === undefined) continue
      const isKept =
        isRecord(choice) &&
        typeof choice.value === 'number' &&
        typeof choice.setting === 'number' &&
        parseKnobValue(knob, choice.value).ok &&
        same(choice.setting, base)
      if (isKept) {
        choices[knob.key] = { value: choice.value as number, setting: base }
        values[knob.key] = choice.value as number
      } else {
        isStale = true // lapsed (the setting changed since), or not ours
      }
    }
    if (Object.keys(saved).some(key => !KNOBS.some(knob => knob.key === key))) isStale = true
    if (isStale) await this.saveTuning(choices).catch(() => undefined)
    return { values, defaults, choices }
  }

  /** How long an open palm is held to take the cursor now (the engage-time setting). */
  async holdSeconds(): Promise<number> {
    return (await this.tuningState()).values.engageSeconds
  }

  private async saveTuning(choices: TuningChoices): Promise<void> {
    if (Object.keys(choices).length === 0) await this.engine.storeDelete(TUNING_KEY)
    else await this.engine.storeSet(TUNING_KEY, choices)
  }

  /**
   * Changes knobs: each to a value, or to `default` (what the plugin setting
   * says, else the built-in default). Kept, then sent to the running helper
   * (only the knobs whose value changed); a helper that does not take it
   * leaves nothing changed, kept or sent.
   */
  private async changeTuning(wanted: Partial<Record<KnobKey, number | 'default'>>): Promise<TuningOutcome> {
    const state = await this.tuningState()
    const choices: TuningChoices = { ...state.choices }
    for (const knob of KNOBS) {
      const choice = wanted[knob.key]
      if (choice === undefined) continue
      const base = state.defaults[knob.key]
      if (choice === 'default' || same(choice, base)) delete choices[knob.key]
      else choices[knob.key] = { value: choice, setting: base }
    }
    const after = { ...state.defaults }
    for (const knob of KNOBS) after[knob.key] = choices[knob.key]?.value ?? state.defaults[knob.key]
    const changed = KNOBS.filter(knob => !same(after[knob.key], state.values[knob.key])).map(knob => knob.key)
    const outcome = { before: state.values, after, defaults: state.defaults, changed }
    if (changed.length === 0) return { ...outcome, end: 'unchanged' }
    await this.saveTuning(choices)
    if (!this.helper.isRunning) return { ...outcome, end: 'saved' }
    const body: Partial<TuningValues> = {}
    for (const key of changed) body[key] = after[key]
    const sent = await this.helper.send('config', body)
    if (sent.ok) return { ...outcome, end: 'applied' }
    // Stopped meanwhile: it starts with this at its next start.
    if (sent.code === 'not_running') return { ...outcome, end: 'saved' }
    // Whatever the helper did not take is not kept, so the table, the status and the next start agree with it.
    const current = (await this.tuningState()).choices
    for (const key of changed) {
      const before = state.choices[key]
      if (before === undefined) delete current[key]
      else current[key] = before
    }
    await this.saveTuning(current)
    // An older helper names the key it does not know; its version can look equal (a dev copy), so the words are the evidence too.
    const isOlder = this.updateNotice !== undefined || /unexpected property/i.test(sent.message)
    const stale = isOlder ? ' The hand helper may be older than this Jarvis: /jarvis setup hands updates it.' : ''
    const what = sent.code === 'bad_request' ? 'refused it' : 'could not apply it'
    return { ...outcome, end: { failure: `The hand helper ${what}: ${plainMessage(sent.message)}. Nothing was changed.${stale}` } }
  }

  /** "; it applies when hand control starts." for a change no helper took, else a full stop. */
  private static ending(end: TuningOutcome['end']): string {
    return end === 'saved' ? '; it applies when hand control starts.' : '.'
  }

  /** `/jarvis hands set` with its words: a name and a value, a name alone, or nothing. */
  async setTuningWords(args: readonly string[]): Promise<string> {
    const words = args
      .join(' ')
      .split(/[\s=]+/)
      .map(word => unquote(word))
      .filter(word => word !== '' && word.toLowerCase() !== 'to')
    if (words.length === 0) {
      return `Use /jarvis hands set <name> <number|default>, for example /jarvis hands set speed 1.5. The settings are ${listWords(KNOBS.map(knob => knob.name))}; /jarvis hands tune shows what each does.`
    }
    // "set cursor speed" names a setting; "set cursor speed 2" names one and gives its value.
    if (words.length > 1 && findKnob(words.join(' ')) === undefined) {
      return await this.setTuning(words.slice(0, -1).join(' '), words.at(-1))
    }
    return await this.setTuning(words.join(' '), undefined)
  }

  /** One setting: reported when `value` is undefined, else changed (the `set` command and the tool). */
  async setTuning(name: string, value: unknown): Promise<string> {
    const knob = findKnob(name)
    if (knob === undefined) return unknownKnob(name)
    if (value === undefined) return await this.describeKnob(knob)
    const parsed = parseKnobValue(knob, value)
    if (!parsed.ok) {
      const range = `${rangeText(knob)} (default ${withUnit(knob, knob.default)})`
      if (parsed.why === 'range') return `${numberOrText(value)} is outside the range for ${knob.label}: ${range}. Nothing was changed.`
      return `"${numberOrText(value)}" is not a number. ${knob.label} takes a number from ${range}, or "default": /jarvis hands set ${knob.name} ${exampleValue(knob)}.`
    }
    const outcome = await this.changeTuning({ [knob.key]: parsed.value })
    if (typeof outcome.end === 'object') return outcome.end.failure
    const now = withUnit(knob, outcome.after[knob.key])
    if (outcome.end === 'unchanged') return `${knob.label} is already ${now}.`
    const was = withUnit(knob, outcome.before[knob.key])
    const base = outcome.defaults[knob.key]
    let sentence: string
    if (parsed.value !== 'default') {
      sentence = `${knob.label} is now ${now} (was ${was}; default ${withUnit(knob, knob.default)}, range ${rangeText(knob)})`
    } else if (same(base, knob.default)) {
      sentence = `${knob.label} is back to its default, ${now} (was ${was})`
    } else {
      sentence = `${knob.label} is back to ${now}, what the ${knob.setting ?? 'plugin'} plugin setting says (was ${was})`
    }
    return `${sentence}${Hands.ending(outcome.end)}`
  }

  /** One setting in words: its value, default and range, what higher does, and how to change it. */
  private async describeKnob(knob: Knob): Promise<string> {
    const { values } = await this.tuningState()
    return `${knob.label} is ${withUnit(knob, values[knob.key])} (default ${withUnit(knob, knob.default)}, range ${rangeText(knob)}). Higher means ${knob.higher}. Change it with /jarvis hands set ${knob.name} <${number(knob.min)} to ${number(knob.max)}|default>.`
  }

  /** `/jarvis hands preset [name]`. */
  async choosePreset(choice: string | undefined): Promise<string> {
    if (choice === undefined) {
      const { values } = await this.tuningState()
      const lines = PRESET_NAMES.map(name => {
        const set = PRESETS[name]
        return `  ${name}: ${Object.keys(set).length === 0 ? 'every setting at its default' : describeValues(set)}`
      })
      const now = presetOf(values) ?? 'custom'
      return `${['The presets:', ...lines].join('\n')}\nNow: ${now}. Apply one with /jarvis hands preset <${PRESET_NAMES.join('|')}>; the settings it does not name go back to their defaults.`
    }
    const name = presetNamed(choice)
    if (name === undefined) {
      return `Unknown preset "${choice.slice(0, 40)}". The presets are ${listWords(PRESET_NAMES)}; /jarvis hands preset shows what each sets.`
    }
    const wanted: Partial<Record<KnobKey, number>> = {}
    for (const knob of KNOBS) wanted[knob.key] = PRESETS[name][knob.key] ?? knob.default
    const outcome = await this.changeTuning(wanted)
    if (typeof outcome.end === 'object') return outcome.end.failure
    if (outcome.end === 'unchanged') return `Preset ${name} is already in place.`
    const sentence =
      Object.keys(PRESETS[name]).length === 0
        ? 'Preset balanced: every setting is at its default'
        : `Preset ${name}: ${describeValues(PRESETS[name])} (every other setting is at its default)`
    return `${sentence}${Hands.ending(outcome.end)}`
  }

  /** `/jarvis hands reset [name]`: every setting, or one, back to its default. */
  async resetTuning(args: readonly string[]): Promise<string> {
    if (args.length > 0) return await this.setTuning(args.join(' '), 'default')
    const outcome = await this.changeTuning(Object.fromEntries(KNOBS.map(knob => [knob.key, 'default' as const])))
    if (typeof outcome.end === 'object') return outcome.end.failure
    if (outcome.end === 'unchanged') return 'Hand control tuning is already at its defaults.'
    // The plugin settings are the defaults a command's choice falls back to.
    const fromSettings = Object.fromEntries(KNOBS.filter(knob => !same(outcome.defaults[knob.key], knob.default)).map(knob => [knob.key, outcome.defaults[knob.key]]))
    const note = Object.keys(fromSettings).length === 0 ? '' : ` (your plugin settings count as defaults: ${describeValues(fromSettings)})`
    return `Hand control tuning is back to its defaults${note}${Hands.ending(outcome.end)}`
  }

  /** The status's line about the tuning: a preset by name, else custom and what is changed. */
  private async tuningLine(): Promise<string> {
    const { values } = await this.tuningState()
    const more = '/jarvis hands tune shows every setting and how to change it.'
    const preset = presetOf(values)
    if (preset === 'balanced') return `Tuning: balanced (all defaults). ${more}`
    if (preset !== undefined) return `Tuning: ${preset} preset. ${more}`
    const changed = changedValues(values)
    return `Tuning: custom, ${Object.keys(changed).length} changed (${describeValues(changed)}). ${more}`
  }

  /** `/jarvis hands tune`: every setting with its value, default, range and what higher does, and how to change them. */
  async tuneText(): Promise<string> {
    const { values, defaults } = await this.tuningState()
    const changed = changedValues(values)
    const preset = presetOf(values)
    const headline =
      preset === 'balanced'
        ? 'balanced (every setting is at its default)'
        : preset !== undefined
          ? `${preset} preset`
          : `custom, ${Object.keys(changed).length} changed`
    const lines = [`Hand control tuning: ${headline}.`, ...this.options.settings.badTuning]
    if (this.tuningNote !== undefined) lines.push(this.tuningNote)

    const rows = KNOBS.map(knob => {
      const isChanged = !same(values[knob.key], knob.default)
      const base = defaults[knob.key]
      const fromSetting = same(base, knob.default) ? '' : ` (${knob.setting ?? 'the plugin'} says ${withUnit(knob, base)})`
      return {
        knob,
        isChanged,
        name: `${knob.label} (${knob.name})`,
        value: withUnit(knob, values[knob.key]),
        default: `default ${withUnit(knob, knob.default)}${fromSetting}`,
        range: `range ${rangeText(knob)}`,
      }
    })
    const widths = (pick: (row: (typeof rows)[number]) => string): number => Math.max(...rows.map(row => pick(row).length))
    const [nameWidth, valueWidth, defaultWidth, rangeWidth] = [widths(r => r.name), widths(r => r.value), widths(r => r.default), widths(r => r.range)]
    const show = (row: (typeof rows)[number]): string[] => [
      `${row.isChanged ? '* ' : '  '}${row.name.padEnd(nameWidth)}  ${row.value.padEnd(valueWidth)}  ${row.default.padEnd(defaultWidth)}  ${row.isChanged ? `${row.range.padEnd(rangeWidth)}  changed` : row.range}`,
      `    Higher means ${row.knob.higher}.`,
    ]
    lines.push('', 'Everyday settings (also in the plugin settings):')
    for (const row of rows) if (row.knob.setting !== undefined) lines.push(...show(row))
    lines.push('', 'Fine tuning:')
    for (const row of rows) if (row.knob.setting === undefined) lines.push(...show(row))
    lines.push(
      '',
      'Change one: /jarvis hands set <name> <number|default>, for example /jarvis hands set speed 1.5.',
      `All at once: /jarvis hands preset <${PRESET_NAMES.join('|')}>. Back to the defaults: /jarvis hands reset.`,
      'Changes apply right away, never in the middle of a click, a drag or a grab, and are remembered. A plugin setting (such as handCursorSpeed) gives the default; a command overrides it until that setting is changed.',
    )
    return lines.join('\n')
  }

  /** `/jarvis hands`: the state, the camera, the displays, how to start, and the gestures. */
  async statusText(): Promise<string> {
    const lines: string[] = []
    if (!(await this.isEnabled())) {
      lines.push('Hand control is off. Turn it on with /jarvis hands on: your webcam watches your hands and moves the mouse and windows; nothing leaves this computer.')
      if (!(await this.isInstalled())) lines.push('It is not set up yet: run /jarvis setup hands first (about 500 MB).')
      return `${lines.join('\n')}\n\n${HANDS_HELP}`
    }
    const { phase, detail } = this.view
    const state = phase === 'off' ? 'on, but the hand helper is not running (/jarvis hands restart starts it)' : phaseText(this.view)
    lines.push(`Hand control: ${state}${detail ? ` (${detail})` : ''}`)
    if (this.updateNotice !== undefined) lines.push(this.updateNotice)
    const engage = await this.engageMode()
    const hello = this.helper.hello
    const report = await this.liveStatus()
    const ready = this.ready
    if (hello !== undefined) {
      const camera = report?.camera ?? ready?.camera
      const size = ready === undefined ? '' : ` at ${ready.width}x${ready.height}`
      // No camera yet: opening it, unless the helper started paused, which keeps it closed.
      const noCamera = this.view.phase === 'paused' ? 'camera off while paused' : 'opening the camera'
      const parts = [`Helper ${hello.version} (pid ${hello.pid})`, camera === undefined ? noCamera : `camera ${camera}${size}`]
      if (report !== undefined) parts.push(`${report.fps.toFixed(1)} fps, ${Math.round(report.inferMs)} ms per frame`)
      if (report?.calibrated !== undefined) parts.push(report.calibrated ? 'calibrated' : 'not calibrated (/jarvis hands calibrate)')
      lines.push(parts.join(' · '))
    } else {
      const camera = await this.camera()
      lines.push(`Camera: ${camera === undefined ? 'the first one' : `"${camera}"`}`)
    }
    const displays = report?.displays ?? ready?.displays ?? []
    if (displays.length > 0) {
      lines.push('Displays (/jarvis hands display <n|all> chooses):')
      for (const display of displays) lines.push(`  ${describeDisplay(display)}`)
    } else {
      lines.push(`Displays: ${describeSelection(await this.displays(), [])}`)
    }
    const hold = await this.holdSeconds()
    lines.push(`${engageText(engage, hold)} (/jarvis hands engage ${engage === 'palm' ? 'always' : 'palm'} switches).`)
    lines.push(await this.tuningLine())
    if (this.tuningNote !== undefined) lines.push(this.tuningNote)
    return `${lines.join('\n')}\n\nGestures:\n${gestures(hold)}\n\n${HANDS_HELP}`
  }

  /**
   * /jarvis setup hands after uv was found: stops the hand helper (Windows
   * locks a running venv), installs, downloads the model, then starts the
   * helper again when hand control is on. `isRefresh`: the same after
   * /jarvis setup, which updates an installed hand helper along with the
   * voice helper; it leaves one another window runs alone.
   */
  async runSetup(uv: string, { isRefresh = false }: { isRefresh?: boolean } = {}): Promise<void> {
    const engine = this.engine
    if (this.isSetupRunning) return
    // Nothing installs as administrator, or before the check has said.
    const held = this.whyHeld()
    if (held !== undefined) {
      engine.log(held)
      return
    }
    if (isRefresh && this.helper.phase === 'elsewhere') {
      engine.log(
        'The hand helper was not reinstalled: another Claude Code window runs it, which keeps its files in use. Turn it off there (/jarvis hands off), then run /jarvis setup hands here.',
      )
      return
    }
    this.isSetupRunning = true
    this.publish({ phase: 'setup', detail: 'stopping the hand helper' })
    try {
      if (!(await this.helper.stop())) {
        engine.log(
          'Hand control setup did not run: the hand helper did not exit, and a running helper keeps its files locked. It stops by itself within a minute; then run /jarvis setup hands again.',
        )
        engine.toast('Hand control setup did not run; the transcript says why.')
        return
      }
      const result = await runHandsSetup(engine, this.platform, uv, {
        pluginVersion: await this.pluginVersion(),
        onProgress: text => this.publish({ phase: 'setup', detail: text }),
        debug: line => engine.debug(`jarvis: ${line}`),
      })
      if (result.ok) {
        this.updateNotice = undefined
        const isOn = await this.isEnabled()
        if (isRefresh) {
          engine.log(`Hand control reinstalled for this version of Jarvis (${result.summary}).${isOn ? ' Starting the hand helper.' : ''}`)
          engine.toast(isOn ? 'Hand control is reinstalled. Starting the camera.' : 'Hand control is reinstalled.')
          return
        }
        const next = isOn ? 'Starting the hand helper.' : 'Turn it on with /jarvis hands on.'
        engine.log(`Hand control installed (${result.summary}). ${next}`)
        engine.toast(isOn ? 'Hand control is installed. Starting the camera.' : `Hand control is installed. ${next}`)
      } else {
        engine.log(`Hand control setup failed: ${result.message}`)
        engine.toast('Hand control setup failed; the transcript has the details.')
      }
    } catch (error) {
      engine.log(`Hand control setup failed: ${describeError(error)}`)
      engine.toast('Hand control setup failed; the transcript has the details.')
    } finally {
      this.isSetupRunning = false
      this.publish({ phase: 'off' })
      if (await this.isEnabled()) this.helper.start({ userInitiated: true })
    }
  }

  private async initialConfig(): Promise<HandsConfigCommand> {
    this.engage = await this.engageMode()
    this.tuningNote = undefined // a new helper gets its chance
    this.warnAboutSettings()
    // The knobs at their built-in default are the helper's own: only the changed ones go out.
    const { values } = await this.tuningState()
    return { engage: this.engage, displays: await this.displays(), ...changedValues(values) }
  }

  /** Says once what is wrong with the plugin's tuning settings (they use the defaults, and the start goes on). */
  private warnAboutSettings(): void {
    const bad = this.options.settings.badTuning
    if (this.hasWarnedAboutSettings || bad.length === 0) return
    this.hasWarnedAboutSettings = true
    for (const line of bad) this.engine.log(line)
    const first = bad[0] ?? ''
    this.engine.toast(
      bad.length === 1
        ? `Hand control: ${first.charAt(0).toLowerCase()}${first.slice(1)}`
        : `Hand control: ${bad.length} settings are not numbers in range, so their defaults are used. The transcript lists them.`,
      { timeoutMs: 8000 },
    )
  }

  /**
   * The helper refused the start's config: without the knobs, which an older
   * helper does not know, it can still take the rest. The tuning is then not
   * in force, which tune and the status say.
   */
  private withoutTuning(config: HandsConfigCommand, message: string): HandsConfigCommand | undefined {
    const rest: HandsConfigCommand = { ...config }
    let isDropped = false
    for (const knob of KNOBS) {
      if (knob.key in rest) {
        delete rest[knob.key]
        isDropped = true
      }
    }
    if (!isDropped) return undefined
    this.tuningNote = `The hand helper refused the tuning settings (${plainMessage(message)}), so it runs with its defaults. /jarvis setup hands updates an older hand helper; /jarvis hands restart tries again.`
    this.engine.log(this.tuningNote)
    this.engine.toast('Hand control: the hand helper refused the tuning settings, so it runs with its defaults. The transcript says why.', {
      timeoutMs: 8000,
    })
    return rest
  }

  /** The helper's status answer, when it runs and answers. */
  private async liveStatus(): Promise<HandsStatusResponse | undefined> {
    if (!this.helper.isRunning) return undefined
    const outcome = await this.helper.send('status', {})
    if (!outcome.ok) return undefined
    const report = outcome.response
    if (typeof report.fps !== 'number' || typeof report.inferMs !== 'number') return undefined
    return {
      ...(report as HandsStatusResponse),
      camera: typeof report.camera === 'string' ? report.camera : undefined,
      calibrated: typeof report.calibrated === 'boolean' ? report.calibrated : undefined,
      displays: parseDisplays(report.displays) ?? this.ready?.displays ?? [],
    }
  }

  /** Why a command that needs the running helper cannot go now; undefined when it can. */
  private async notRunning(): Promise<string | undefined> {
    if (this.helper.isRunning) return undefined
    if (!this.isLocal) return NOT_LOCAL
    if (!(await this.isEnabled())) return 'Hand control is off. Turn it on with /jarvis hands on.'
    if (this.view.phase === 'off') return 'The hand helper is not running; /jarvis hands restart starts it.'
    if (this.view.phase === 'elsewhere') return ELSEWHERE_TEXT
    return `The hand helper is not running (${phaseText(this.view)}).`
  }

  /**
   * The venv holds the hands package as the last /jarvis setup hands copied
   * it, so after a plugin update it runs the old code. Says once (the old
   * helper still runs) when that setup's record is missing or names another
   * version of the plugin.
   */
  private async checkInstall(): Promise<void> {
    // Not installed: nothing to be old yet; a start after the setup checks again.
    if (!(await this.isInstalled())) {
      this.hasCheckedInstall = false
      return
    }
    const version = await this.pluginVersion()
    if (version === undefined) return
    const path = joinPath(this.platform.sep, this.platform.dataDir, 'hands', HANDS_INSTALLED_FILE)
    const record: unknown = await this.engine
      .readFile(path)
      .then(text => JSON.parse(text) as unknown)
      .catch(() => undefined)
    const installed = isRecord(record) && typeof record.pluginVersion === 'string' ? record.pluginVersion : undefined
    if (installed === version) return
    const by = installed === undefined ? 'an earlier version of Jarvis' : `Jarvis ${installed}, and this is Jarvis ${version}`
    this.updateNotice = `The hand helper was installed by ${by}. Run /jarvis setup hands to update it; until then the old one runs.`
    this.engine.log(this.updateNotice)
    this.engine.toast('Hand control needs an update: run /jarvis setup hands.', { timeoutMs: 8000 })
  }

  /** Sends a live setting; undefined when no helper runs (it applies at the next start), '' when applied. */
  private async sendConfig(body: HandsConfigCommand): Promise<string | undefined> {
    if (!this.helper.isRunning) return undefined
    const outcome = await this.helper.send('config', body)
    return outcome.ok ? '' : outcome.message
  }

  private onEvent(event: HandsEvent): void {
    switch (event.type) {
      case 'hello':
        this.lastError = undefined
        this.isResumePending = false
        this.patch({ phase: 'starting', detail: undefined })
        return
      case 'state': {
        const detail = event.state === 'error' ? this.lastError : event.state === 'calibrating' ? this.corner : undefined
        // "starting" is the resume itself, in a helper whose camera never opened.
        if (this.isResumePending && event.state !== 'paused' && event.state !== 'starting') {
          // The camera opened after the helper answered the resume (or the helper failed: its error says so).
          this.isResumePending = false
          if (event.state !== 'error') this.engine.toast(RESUMED)
        }
        this.patch({ phase: event.state, detail })
        return
      }
      case 'ready':
        this.ready = event
        if (!this.hasAnnouncedReady) {
          this.hasAnnouncedReady = true
          this.engine.toast(
            this.engage === 'always'
              ? 'Hand control is on. Raise a hand toward the camera to start.'
              : 'Hand control is on. Hold an open palm toward the camera to start.',
          )
        }
        return
      case 'calibration':
        this.onCalibration(event)
        return
      case 'error':
        this.onError(event)
        return
      case 'gesture':
        // The on-screen reticle shows gestures; the status line does not chase them.
        return
    }
  }

  private onCalibration(event: CalibrationEvent): void {
    const corner = CORNERS[event.step]
    this.corner = corner
    if (corner !== undefined) this.patch({ phase: 'calibrating', detail: corner })
    const text = CALIBRATION_TEXT[event.step]
    this.engine.toast(text, corner === undefined ? undefined : { timeoutMs: CALIBRATION_TOAST_MS })
    this.speak(text)
  }

  private onError(event: HandsErrorEvent): void {
    // The supervisor says "active in another window" for this one.
    if (event.code === 'already_running') return
    const detail = describeHandsError(event.message, event.hint)
    if (event.fatal) {
      this.lastError = detail
      this.patch({ phase: 'error', detail })
      return
    }
    const isCamera = CAMERA_ERRORS.has(event.code)
    // A camera that could not open after a resume answered `pending`: the user waits for this one.
    const isAwaited = isCamera && this.isResumePending
    if (isAwaited) this.isResumePending = false
    // The camera stays off: the status line says why, until the next state.
    if (isCamera && this.view.phase === 'paused') this.patch({ detail })
    if (this.mutedErrors.has(event.code) && !isAwaited) return
    this.mutedErrors.add(event.code)
    this.engine.after(ERROR_TOAST_INTERVAL_MS, () => this.mutedErrors.delete(event.code))
    this.engine.toast(`Hand control: ${detail}`, { timeoutMs: 8000 })
  }

  /** Says a calibration step through the voice helper, when it runs. */
  private speak(text: string): void {
    const voice = this.options.voice()
    if (voice?.isRunning !== true) return
    // A prompt is not a reply: like the helper's own test line (an id starting
    // "test-"), it opens no follow-up listening afterwards.
    const replyId = `test-hands-${randomToken().slice(0, 12)}`
    void voice.send('speak', { replyId, seq: 0, text, final: true }).then(outcome => {
      if (!outcome.ok) this.engine.debug(`jarvis: calibration prompt not spoken: ${outcome.message}`)
    })
  }

  private onHelperPhase(phase: HandsHelperPhase, detail: string | undefined, isFinal = false): void {
    if (phase !== 'running') {
      this.ready = undefined
      this.isResumePending = false
    }
    if (phase === 'starting' && !this.hasCheckedInstall) {
      // Before hello: an old helper may not get that far with this mod.
      this.hasCheckedInstall = true
      void this.checkInstall().catch((error: unknown) => this.engine.debug(`jarvis: hands install not checked: ${describeError(error)}`))
    }
    if (this.isSetupRunning) return // setup owns the hands part until it ends
    const phases: Record<HandsHelperPhase, HandsPhase | undefined> = {
      stopped: 'off',
      not_installed: 'not_installed',
      starting: 'starting',
      running: undefined, // the helper's own state events say what it is doing
      restarting: 'restarting',
      elsewhere: 'elsewhere',
      failed: 'failed',
    }
    const next = phases[phase]
    if (next === undefined) {
      if (!HELPER_STATES.has(this.view.phase)) this.patch({ phase: 'starting', detail: undefined })
      return
    }
    // The status line has no room for what to do about it.
    if (next === 'elsewhere') this.engine.toast(ELSEWHERE_TEXT, { timeoutMs: 8000 })
    this.publish({ phase: next, detail, ...(isFinal ? { isFinal } : {}) })
  }

  private patch(change: Partial<JarvisHandsView>): void {
    this.publish({ ...this.view, ...change })
  }

  private publish(view: JarvisHandsView): void {
    this.view = { ...view, engage: this.engage }
    this.options.onView(this.view)
  }
}

type TuningOutcome = {
  before: TuningValues
  after: TuningValues
  /** What each knob would be without a command's choice. */
  defaults: TuningValues
  /** The knobs whose value is not what it was. */
  changed: KnobKey[]
  /** unchanged: nothing to do; saved: kept for the next start; applied: the running helper took it; failure: it did not, and nothing is kept. */
  end: 'unchanged' | 'saved' | 'applied' | { failure: string }
}

/** A helper's message without its closing full stop, which the sentence around it supplies. */
function plainMessage(message: string): string {
  return message.replace(/[\s.]+$/, '')
}

/** A value to show as an example: a quarter of the way from the default to the top of the range. */
const exampleValue = (knob: Knob): string => String(Math.round((knob.default + (knob.max - knob.default) / 4) * 100) / 100)

const numberOrText = (value: unknown): string => (typeof value === 'number' ? String(value) : String(value).trim().slice(0, 40))

function unknownKnob(name: string): string {
  return `Unknown setting "${name.slice(0, 40)}". The settings are ${listWords(KNOBS.map(knob => knob.name))}; /jarvis hands tune shows what each does.`
}

/** The text without one pair of matching quotes around it. */
function unquote(text: string): string {
  const match = /^(["'])(.*)\1$/s.exec(text.trim())
  return (match?.[2] ?? text).trim()
}

/** Parses `all`, `2` or `1,2` (spaces allowed); undefined when it is none of them. */
export function parseDisplaySelection(text: string): DisplaySelection | undefined {
  const words = text
    .toLowerCase()
    .split(/[\s,]+/)
    .filter(word => word !== '')
  if (words.length === 1 && words[0] === 'all') return 'all'
  if (words.length === 0 || !words.every(word => /^[1-9]\d{0,2}$/.test(word))) return undefined
  return [...new Set(words.map(Number))]
}

function describeSelection(selection: DisplaySelection, known: readonly HandsDisplay[]): string {
  if (selection === 'all') {
    // The helper's "all" leaves virtual displays out (a streaming or Spacedesk screen), unless every one is virtual.
    if (known.length === 0) return 'every display except virtual ones'
    const virtual = known.filter(display => display.virtual)
    if (virtual.length === 0 || virtual.length === known.length) return 'all displays'
    const listed = virtual.map(display => `${display.id} (${display.name})`).join(', ')
    return `every display except virtual ${virtual.length === 1 ? 'display' : 'displays'} ${listed}`
  }
  const named = selection.map(id => {
    const name = known.find(display => display.id === id)?.name
    return name ? `${id} (${name})` : `${id}`
  })
  if (named.length === 1) return `display ${named[0]}`
  return `displays ${named.slice(0, -1).join(', ')} and ${named.at(-1)}`
}

function describeDisplay(display: HandsDisplay): string {
  const notes = [
    `${display.width}x${display.height} at ${display.x},${display.y}`,
    ...(display.primary ? ['primary'] : []),
    ...(display.virtual ? ['virtual'] : []),
    display.used ? 'in use' : 'not used',
  ]
  return `${display.id}  ${display.name || 'display'} · ${notes.join(' · ')}`
}

// ---- Commands and the tool ----

/** What /jarvis setup hands prints when uv is not installed (it never installs uv itself). */
function uvMissingMessage(platform: Platform): string {
  return `uv is not installed. Install it with:\n  ${platform.uvInstallHint}\nthen open a new terminal and run /jarvis setup hands again.`
}

/** `/jarvis setup hands [cpu]`: finds uv, then installs in the background with progress in the status line. */
export async function runHandsSetupCommand(hands: Hands | undefined, args: readonly string[]): Promise<string> {
  if (hands === undefined || !hands.isLocal) return NOT_LOCAL
  // Hand tracking always runs on the CPU, so "cpu" changes nothing; it is accepted like the voice setup's.
  const unknown = args.find(arg => arg.toLowerCase() !== 'cpu')
  if (unknown !== undefined) return `Unknown option "${unknown}" for /jarvis setup hands; it takes none.`
  if (hands.isSetupRunning) return 'Hand control setup is already running; its progress is in the status line.'
  // Nothing installs as administrator, or before the check has said.
  const held = hands.whyHeld()
  if (held !== undefined) return held
  const { engine, platform } = hands
  const uv = await findUv(engine, platform)
  if (uv === undefined) return uvMissingMessage(platform)
  const isOn = await hands.isEnabled()
  void hands.runSetup(uv)
  return [
    `Setting up hand control with ${uv}:`,
    `  1. a separate Python 3.12 environment in ${platform.handsVenvDir} with MediaPipe and OpenCV (about 500 MB)`,
    `  2. the hand model (8 MB) in ${joinPath(platform.sep, platform.dataDir, 'models', 'hands')}`,
    isOn
      ? 'Progress shows in the status line; hand control starts when it is done.'
      : 'Progress shows in the status line. Then turn it on with /jarvis hands on.',
  ].join('\n')
}

/** Runs `/jarvis hands <args>`; the caller catches what it throws. */
export async function runHandsCommand(hands: Hands | undefined, args: readonly string[]): Promise<string> {
  const [sub = '', ...rest] = args
  const word = sub.toLowerCase()
  if (hands === undefined || !hands.isLocal) {
    return word === '' || word === 'status' || word === 'help' ? `${NOT_LOCAL}\n\n${HANDS_HELP}` : NOT_LOCAL
  }
  switch (word) {
    case '':
    case 'status':
      return await hands.statusText()
    case 'help':
      return `Gestures:\n${gestures(await hands.holdSeconds())}\n\n${HANDS_HELP}`
    case 'on':
      return await hands.turnOn()
    case 'off':
      return await hands.turnOff()
    case 'calibrate':
      return await hands.calibrate(rest[0])
    case 'display':
    case 'displays':
      return await hands.chooseDisplays(rest)
    case 'engage':
      return await hands.chooseEngage(rest[0])
    case 'camera':
      return await hands.chooseCamera(rest)
    case 'tune':
    case 'tuning':
    case 'sensitivity':
      return await hands.tuneText()
    case 'set':
      return await hands.setTuningWords(rest)
    case 'preset':
      return await hands.choosePreset(rest[0])
    case 'reset':
      return await hands.resetTuning(rest)
    case 'pause':
      return await hands.pause()
    case 'resume':
      return await hands.resume()
    case 'restart':
      return await hands.restart()
    case 'setup':
      return await runHandsSetupCommand(hands, rest)
    default:
      return `Unknown subcommand "${sub}" for /jarvis hands.\n\n${HANDS_HELP}`
  }
}

/** The tool's display as /jarvis hands display's words: a number, or a list of them, is taken too. */
function toolDisplay(value: unknown): string {
  if (Array.isArray(value)) return value.map(String).join(',')
  return typeof value === 'string' || typeof value === 'number' ? String(value) : JSON.stringify(value)
}

/** One tool action: the text the matching /jarvis hands command prints. */
async function runToolAction(hands: Hands | undefined, action: ToolAction): Promise<string> {
  // /jarvis hands engage chooses the engage mode; the tool's engage takes the cursor now.
  if (action !== 'engage' && action !== 'disengage') return runHandsCommand(hands, [action])
  if (hands === undefined || !hands.isLocal) return NOT_LOCAL
  return hands.setEngaged(action === 'engage')
}

/** Whether a tool call carries a tuning argument (setting, value or preset). */
export const isTuningCall = (input: Record<string, unknown>): boolean =>
  input.setting !== undefined || input.value !== undefined || input.preset !== undefined

/** Whether it changes the tuning: a value or a preset (a setting alone only reports it). */
export const changesTuning = (input: Record<string, unknown>): boolean => input.value !== undefined || input.preset !== undefined

/**
 * Serves the `hands` tool: a display choice (as /jarvis hands display), then
 * a tuning change (as /jarvis hands set or preset), then an action (as the
 * matching /jarvis hands command), their texts together; never throws.
 */
export async function runHandsTool(hands: Hands | undefined, input: Record<string, unknown>): Promise<string> {
  const action = TOOL_ACTIONS.find(one => one === input.action)
  if (input.action !== undefined && action === undefined) {
    return `Unknown action ${JSON.stringify(input.action)}; use one of ${TOOL_ACTIONS.join(', ')}.`
  }
  const isTuning = isTuningCall(input)
  if (action === undefined && input.display === undefined && !isTuning) return 'Give an action, a display, or both.'
  const texts: string[] = []
  try {
    if (input.display !== undefined) texts.push(await runHandsCommand(hands, ['display', toolDisplay(input.display)]))
    if (isTuning) texts.push(await runToolTuning(hands, input))
    if (action !== undefined) texts.push(await runToolAction(hands, action))
  } catch (error) {
    texts.push(`Hand control ${action ?? (isTuning ? 'tuning' : 'display')} failed: ${describeError(error)}`)
  }
  return texts.join('\n\n')
}

/** What a tool call's tuning arguments ask for. */
export type ToolTuning =
  | { kind: 'read'; knob: Knob }
  | { kind: 'set'; knob: Knob; value: number | 'default' }
  | { kind: 'preset'; name: PresetName }

/**
 * The tuning a call asks for, read exactly as runToolTuning reads it (keep the
 * two in step; hands-gate.ts asks before a change): a setting alone is read,
 * with a value inside its range it is set, a preset names a profile. Undefined
 * when the call carries no tuning, or runToolTuning would only answer with what
 * to fix and change nothing (an unknown setting, a value outside the range, a
 * preset together with a setting or a value, a wrong type).
 */
export function toolTuning(input: Record<string, unknown>): ToolTuning | undefined {
  const { setting, value, preset } = input
  if (preset !== undefined) {
    if (setting !== undefined || value !== undefined || typeof preset !== 'string') return undefined
    const name = presetNamed(preset)
    return name === undefined ? undefined : { kind: 'preset', name }
  }
  if (typeof setting !== 'string') return undefined
  const knob = findKnob(setting)
  if (knob === undefined) return undefined
  if (value === undefined) return { kind: 'read', knob }
  if (typeof value !== 'number' && typeof value !== 'string') return undefined
  const parsed = parseKnobValue(knob, value)
  return parsed.ok ? { kind: 'set', knob, value: parsed.value } : undefined
}

/** The tool's setting, value and preset: the text the matching /jarvis hands set or preset prints. */
async function runToolTuning(hands: Hands | undefined, input: Record<string, unknown>): Promise<string> {
  if (hands === undefined || !hands.isLocal) return NOT_LOCAL
  const { setting, value, preset } = input
  if (preset !== undefined) {
    if (setting !== undefined || value !== undefined) return 'Give either a preset or a setting, not both.'
    return typeof preset === 'string' ? await hands.choosePreset(preset) : `The preset must be one of ${listWords(PRESET_NAMES, 'or')}.`
  }
  if (setting === undefined) return 'Say which setting the value is for, such as setting "speed" with value 1.5.'
  if (typeof setting !== 'string') return 'The setting must be a name, such as "speed"; /jarvis hands tune lists them.'
  if (value !== undefined && typeof value !== 'number' && typeof value !== 'string') return 'The value must be a number, or "default".'
  return await hands.setTuning(setting, value)
}
