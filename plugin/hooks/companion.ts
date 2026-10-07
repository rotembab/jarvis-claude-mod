// The link to the Jarvis desktop app (app/ in this repository): while it
// runs, it shows the HUD in its own windows. The app writes its address to
// <JARVIS_HOME>/app/endpoint.json; this module reads it and POSTs snapshots of
// the HUD to 127.0.0.1 with the app's token. A missing or silent app costs a
// file read every few seconds and nothing else: no toast, no error, and never
// a wait inside a turn.

import type { Timer } from 'claude-code'

import type { HudAction, JarvisPhase, JarvisView } from '../types'
import type { Engine, EnvSnapshot } from './engine'
import { describeError, TIMEOUT, withTimeout } from './engine'
import type { Hud, HudMode } from './hud'
import type { Platform } from './platform'
import { joinPath } from './platform'
import { replyLines } from './ui'

/** /jarvis app on|off, kept in the store (on unless set to false). */
export const APP_LINK_KEY = 'appLink'
/** A snapshot goes out at least this often, so the app knows the session is alive. */
export const APP_HEARTBEAT_MS = 2000
/** While the app is not found, its address file is read again at most this often. */
export const APP_REREAD_MS = 5000
/** How long one push may take before the app counts as gone. */
export const APP_PUSH_TIMEOUT_MS = 1000
/** Level changes go out at most this often (15 a second) while Jarvis listens or speaks. */
export const APP_LEVEL_MS = 67
/** What you said is cut to this many characters (the app's own limit). */
export const UTTERANCE_MAX = 500
/** The start of the reply: a few lines' worth, its markdown dropped. */
const REPLY_WIDTH = 100
const REPLY_ROWS = 3

export type AppEndpoint = { port: number; token: string; pid: number; version: string }

/** One push to the app (docs/APP.md has the contract). */
export type AppSnapshot = {
  v: 1
  sessionId: string
  mode: HudMode
  phase: JarvisPhase
  mic: number
  out: number
  utterance?: string
  reply?: string
  actions: Pick<HudAction, 'label' | 'status'>[]
  isOwner: boolean
  at: number
}

export type CompanionSource = {
  hud: Pick<Hud, 'mode' | 'levels' | 'actionLog'>
  view: () => JarvisView
  /** This window runs the voice helper (another window shows it as elsewhere). */
  isOwner: () => boolean
}

export type CompanionOptions = { endpointPath: string; source: CompanionSource }

/** The engine port and a file read, for the app's address file (register.tsx builds `readFile` for hand control too). */
export type CompanionEngine = Engine & { readFile: (path: string) => Promise<string> }

export type AppLinkStatus =
  | { state: 'off' }
  | { state: 'not_running' }
  | { state: 'connected'; version: string }
  | { state: 'no_answer'; reason: string }

/**
 * Where the app writes its address: JARVIS_HOME when it is an absolute local
 * path (a drive path on Windows; $.fs never reads a network location), else
 * the data folder. The app applies the same rule (app/src/main/paths.ts).
 */
export function appEndpointPath(platform: Platform, env: EnvSnapshot): string {
  const custom = env.JARVIS_HOME?.trim()
  const isAbsolute = custom !== undefined && (platform.os === 'windows' ? /^[A-Za-z]:[\\/]/.test(custom) : custom.startsWith('/'))
  return joinPath(platform.sep, isAbsolute ? custom : platform.dataDir, 'app', 'endpoint.json')
}

/** The app's address file, or undefined when it is not one. */
export function parseEndpoint(text: string): AppEndpoint | undefined {
  let value: unknown
  try {
    value = JSON.parse(text)
  } catch {
    return undefined
  }
  if (typeof value !== 'object' || value === null) return undefined
  const { v, port, token, pid, version } = value as Record<string, unknown>
  if (v !== 1 || typeof token !== 'string' || !/^[0-9a-f]{64}$/.test(token)) return undefined
  if (!Number.isInteger(port) || (port as number) < 1 || (port as number) > 65535) return undefined
  if (!Number.isInteger(pid) || (pid as number) < 1) return undefined
  if (typeof version !== 'string' || version.length > 32) return undefined
  return { port: port as number, token, pid: pid as number, version }
}

/** The start of a reply for the app, or undefined when nothing is left to show. */
export function replyStart(text: string | undefined): string | undefined {
  if (text === undefined) return undefined
  const start = replyLines(text, REPLY_WIDTH, REPLY_ROWS).join(' ')
  return start === '' ? undefined : start
}

const cut = (text: string, max: number): string => (text.length > max ? `${text.slice(0, max - 1)}…` : text)
const level = (value: number): number => (Number.isFinite(value) ? Math.round(Math.min(1, Math.max(0, value)) * 100) / 100 : 0)

function randomId(): string {
  return Array.from(crypto.getRandomValues(new Uint8Array(8)), byte => byte.toString(16).padStart(2, '0')).join('')
}

type Shown = Omit<AppSnapshot, 'at'>
/** `isRefused`: the app answered 400 (it would not take the snapshot). `isGone`: nothing answered at its address. */
type Outcome = { ok: true } | { ok: false; reason: string; isRefused: boolean; isGone: boolean }

export class Companion {
  /** Tells this window's pushes apart from another's; new with each session start. */
  private readonly sessionId = randomId()
  private isEnabled = false
  private isDisposed = false
  private endpoint: AppEndpoint | undefined
  private connected = false
  private lastReadAt = Number.NEGATIVE_INFINITY
  private heartbeat: Timer | undefined
  private levelGate: Timer | undefined
  private hasLevelsWaiting = false
  private isSending = false
  private isDirty = false
  private sentKey = ''
  private sentLevels = ''
  private reply: string | undefined

  constructor(
    private readonly engine: CompanionEngine,
    private readonly options: CompanionOptions,
  ) {}

  /** The last push reached the app. */
  get isConnected(): boolean {
    return this.connected
  }

  /** Reads the setting; when on, starts the heartbeat and looks for the app. */
  async start(): Promise<void> {
    this.isEnabled = await this.isOn()
    if (this.isEnabled && !this.isDisposed) this.run()
  }

  /** Something the app shows may have changed. Never waits and never throws. */
  poke(): void {
    if (!this.isEnabled || this.endpoint === undefined) return
    const shown = this.shown()
    if (contentKey(shown) !== this.sentKey) {
      this.queue()
      return
    }
    if (shown.mode !== 'listening' && shown.mode !== 'speaking') return
    if (levelKey(shown) === this.sentLevels) return
    if (this.levelGate !== undefined) {
      this.hasLevelsWaiting = true
      return
    }
    this.queue()
  }

  /** A turn started: the last reply no longer answers what is being asked. */
  onTurnStart(): void {
    if (this.reply === undefined) return
    this.reply = undefined
    this.poke()
  }

  /** Claude's last reply ended; the app shows its start. */
  setReply(text: string | undefined): void {
    const start = replyStart(text)
    if (start === this.reply) return
    this.reply = start
    this.poke()
  }

  async isOn(): Promise<boolean> {
    return (await this.engine.storeGet(APP_LINK_KEY).catch(() => undefined)) !== false
  }

  async setOn(isOn: boolean): Promise<void> {
    await this.engine.storeSet(APP_LINK_KEY, isOn)
    if (isOn === this.isEnabled) return
    this.isEnabled = isOn
    if (isOn) {
      this.lastReadAt = Number.NEGATIVE_INFINITY
      this.run()
    } else this.stop()
  }

  /** For /jarvis app: looks for the app now (the person asked) and says how the link stands. */
  async check(): Promise<AppLinkStatus> {
    if (!(await this.isOn())) return { state: 'off' }
    this.lastReadAt = await this.engine.now()
    const endpoint = await this.readEndpoint()
    if (endpoint === undefined) {
      this.forget()
      return { state: 'not_running' }
    }
    this.endpoint = endpoint
    const outcome = await this.post(endpoint)
    // Nothing listens there: the app ended without removing its address
    // (Task Manager, a closed terminal), so it is simply not running.
    if (!outcome.ok && outcome.isGone) return { state: 'not_running' }
    if (!outcome.ok) return { state: 'no_answer', reason: outcome.reason }
    return { state: 'connected', version: endpoint.version }
  }

  dispose(): void {
    this.isDisposed = true
    this.stop()
  }

  // -- internals

  private run(): void {
    if (this.heartbeat === undefined) this.heartbeat = this.engine.every(APP_HEARTBEAT_MS, () => void this.tick())
    void this.tick()
  }

  private stop(): void {
    this.heartbeat?.cancel()
    this.heartbeat = undefined
    this.levelGate?.cancel()
    this.levelGate = undefined
    this.forget()
  }

  /** Each heartbeat: a push when the app is known, else a look for it now and then. */
  private async tick(): Promise<void> {
    if (!this.isEnabled || this.isDisposed) return
    if (this.endpoint !== undefined) {
      this.queue()
      return
    }
    const now = await this.engine.now()
    if (now - this.lastReadAt < APP_REREAD_MS) return
    this.lastReadAt = now
    const endpoint = await this.readEndpoint()
    if (endpoint === undefined || !this.isEnabled || this.isDisposed) return
    this.endpoint = endpoint
    this.queue()
  }

  private async readEndpoint(): Promise<AppEndpoint | undefined> {
    const text = await this.engine.readFile(this.options.endpointPath).catch(() => undefined)
    return text === undefined ? undefined : parseEndpoint(text)
  }

  /** One push at a time; changes meanwhile go out once it is done. */
  private queue(): void {
    if (this.isSending) {
      this.isDirty = true
      return
    }
    void this.send()
  }

  private async send(): Promise<void> {
    const endpoint = this.endpoint
    if (endpoint === undefined || this.isDisposed) return
    this.isSending = true
    this.isDirty = false
    try {
      await this.post(endpoint)
    } finally {
      this.isSending = false
    }
    if (this.isDirty && this.endpoint !== undefined) this.queue()
  }

  private async post(endpoint: AppEndpoint): Promise<Outcome> {
    const shown = this.shown()
    this.sentKey = contentKey(shown)
    this.sentLevels = levelKey(shown)
    this.startLevelGate()
    const at = await this.engine.now()
    let outcome: Outcome
    try {
      const snapshot: AppSnapshot = { ...shown, at }
      const request = this.engine.fetch(`http://127.0.0.1:${endpoint.port}/v1/hud`, {
        method: 'POST',
        headers: { authorization: `Bearer ${endpoint.token}`, 'content-type': 'application/json' },
        body: JSON.stringify(snapshot),
      })
      const response = await withTimeout(this.engine, request, APP_PUSH_TIMEOUT_MS)
      if (response === TIMEOUT) outcome = { ok: false, reason: `no answer within ${APP_PUSH_TIMEOUT_MS} ms`, isRefused: false, isGone: false }
      else if (response.ok) outcome = { ok: true }
      else outcome = { ok: false, reason: `HTTP ${response.status}`, isRefused: response.status === 400, isGone: false }
    } catch (error) {
      outcome = { ok: false, reason: describeError(error), isRefused: false, isGone: true }
    }
    if (this.endpoint !== endpoint) return outcome
    if (outcome.ok) {
      if (!this.connected) this.engine.debug(`jarvis: the Jarvis app ${endpoint.version} shows the HUD (port ${endpoint.port})`)
      this.connected = true
    } else if (outcome.isRefused) {
      // A snapshot the app would not take (a bug on one side): the link stays.
      this.engine.debug(`jarvis: the Jarvis app refused a HUD snapshot (${outcome.reason})`)
    } else {
      if (this.connected) this.engine.debug(`jarvis: the Jarvis app stopped answering (${outcome.reason})`)
      this.forget()
      // Its address file is read again after the wait, not at the next heartbeat.
      this.lastReadAt = at
    }
    return outcome
  }

  /** Drops the app's address: the heartbeat looks for it again after the wait. */
  private forget(): void {
    this.endpoint = undefined
    this.connected = false
    this.sentKey = ''
    this.sentLevels = ''
  }

  private startLevelGate(): void {
    this.levelGate?.cancel()
    this.levelGate = this.engine.after(APP_LEVEL_MS, () => {
      this.levelGate = undefined
      if (!this.hasLevelsWaiting) return
      this.hasLevelsWaiting = false
      this.poke()
    })
  }

  private shown(): Shown {
    const { hud, view, isOwner } = this.options.source
    const current = view()
    const levels = hud.levels()
    return {
      v: 1,
      sessionId: this.sessionId,
      mode: hud.mode(),
      phase: current.phase,
      mic: level(levels.mic),
      out: level(levels.out),
      ...(current.lastUtterance ? { utterance: cut(current.lastUtterance, UTTERANCE_MAX) } : {}),
      ...(this.reply === undefined ? {} : { reply: this.reply }),
      actions: hud.actionLog.map(({ label, status }) => ({ label, status })),
      isOwner: isOwner(),
    }
  }
}

/** Everything a push carries but the levels. */
const contentKey = (shown: Shown): string => JSON.stringify({ ...shown, mic: 0, out: 0 })
const levelKey = ({ mic, out }: Shown): string => `${mic}:${out}`
