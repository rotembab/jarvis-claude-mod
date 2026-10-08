// The slice of `$` the mod uses, as a plain object of functions. The engine
// requires `$` to be spelled `$.noun.event(...)` at every call site (it is
// never passed around), so register.tsx builds this port inside a hook from
// closures, and every other module depends on the port instead of on `$`.

import type {
  AskOptions,
  HookStream,
  HttpInit,
  HttpResponse,
  ModelCompleteRequest,
  ModelCompleteResult,
  ProcessRunInit,
  ProcessRunResult,
  ProcessSpawnChunk,
  ProcessSpawnRequest,
  ProcessSpawnResult,
  PromptSubmitResult,
  SessionMessage,
  Timer,
  ToastOptions,
  UiBlitArgs,
  UiBlitResult,
  UiOpenResult,
} from 'claude-code'

import type { JarvisHelperRef, JarvisHud, JarvisView } from '../types'
import type { PaneSize } from './hud'

/** The engine's permission decision for a tool call (`$.tool.check`'s answer, the fields the mod reads). */
export type ToolVerdict = {
  decision: 'allow' | 'ask' | 'deny'
  /** Why, in a sentence. */
  reason?: string
  /** The settings rule that decided, as written; absent for a mode's own decision. */
  rule?: string
  /** The most permissive verdict the organization lets a call of the tool reach. */
  ceiling?: 'allow' | 'ask' | 'deny'
}

/** The environment variables the mod reads (each read by its literal name). */
export type EnvSnapshot = {
  OS?: string
  USERPROFILE?: string
  LOCALAPPDATA?: string
  HOME?: string
  NO_PROXY?: string
  CLAUDE_CODE_REMOTE?: string
}

export type Engine = {
  /** The plugin's folder (holds .claude-plugin/ and voice/). */
  pluginRoot: string
  env: () => Promise<EnvSnapshot>

  now: () => Promise<number>
  after: (ms: number, fn: () => void) => Timer
  every: (ms: number, fn: () => void) => Timer

  fetch: (url: string, init: HttpInit) => Promise<HttpResponse>
  spawn: (request: ProcessSpawnRequest) => HookStream<ProcessSpawnChunk, ProcessSpawnResult>
  run: (argv: readonly string[], init?: ProcessRunInit) => Promise<ProcessRunResult>
  exists: (path: string) => Promise<boolean>
  writeFile: (path: string, text: string) => Promise<void>

  storeGet: (key: string) => Promise<unknown>
  storeSet: (key: string, value: unknown) => Promise<void>
  storeDelete: (key: string) => Promise<void>
  readHelperRef: () => Promise<JarvisHelperRef | null>
  writeHelperRef: (ref: JarvisHelperRef | null) => Promise<void>
  readHandsRef: () => Promise<JarvisHelperRef | null>
  writeHandsRef: (ref: JarvisHelperRef | null) => Promise<void>
  writeView: (view: JarvisView) => Promise<void>
  writeHud: (hud: JarvisHud) => Promise<void>
  /** Whether focus mode folds the conversation's rows away (they read it). */
  writeFolded: (isFolded: boolean) => Promise<void>

  status: (text: string | undefined) => void
  toast: (text: string, options?: ToastOptions) => void
  /** A dim line in the transcript (not sent to the model). */
  log: (text: string) => void
  /** A line in the debug log only. */
  debug: (text: string) => void
  /** Opens the HUD pane at a size (placed at any width when the person asked for it, from 144 columns when not); opening it again resizes it. */
  openPane: (size: PaneSize) => Promise<UiOpenResult>
  closePane: () => Promise<void>
  /** Repaints the pane's ring in place. */
  blit: (args: UiBlitArgs) => Promise<UiBlitResult>
  /** Redraws this plugin's drawings (the desktop ring follows the levels this way). */
  invalidate: () => void
  /**
   * Asks the user in the engine's on-screen question dialog; resolves to the
   * label chosen or the text typed under "Other". Rejects when the dialog is
   * dismissed, and in a `-p` run (nobody to ask).
   */
  ask: (question: string, options: readonly string[] | AskOptions) => Promise<string>
  /**
   * The session's permission decision for a call of `tool` now (its rules and
   * mode): nothing runs, no dialog opens. A tool the mod answers itself never
   * meets these rules otherwise.
   */
  checkTool: (tool: string, input: Record<string, unknown>) => Promise<ToolVerdict>

  /** Submits text as the user's own words; resolves once its turn started or it was queued, or with `drop`. */
  submitPrompt: (text: string) => Promise<PromptSubmitResult>
  abortTurn: (turnId: string) => Promise<void>
  /** One completion through the session's own client and credentials. */
  complete: (request: ModelCompleteRequest) => Promise<ModelCompleteResult>
  /** The main conversation's messages (the newest 4096). */
  messages: () => Promise<readonly SessionMessage[]>
  /** The conversation's size at the last response, in tokens; undefined before the first (or after a compaction). */
  contextTokens: () => Promise<number | undefined>
}

/** Resolves after `ms` on the engine's clock (a hooks module has no timers). */
export function delay(engine: Engine, ms: number): Promise<void> {
  return new Promise(resolve => {
    engine.after(ms, resolve)
  })
}

export const TIMEOUT = Symbol('timeout')

/** Races `promise` against the clock; `$.http.fetch` itself has no timeout. */
export function withTimeout<T>(engine: Engine, promise: Promise<T>, ms: number): Promise<T | typeof TIMEOUT> {
  return new Promise((resolve, reject) => {
    const timer = engine.after(ms, () => resolve(TIMEOUT))
    promise.then(
      value => {
        timer.cancel()
        resolve(value)
      },
      (error: unknown) => {
        timer.cancel()
        reject(error)
      },
    )
  })
}

export const describeError = (error: unknown): string => (error instanceof Error ? error.message : String(error))
