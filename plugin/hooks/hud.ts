// The HUD: a pane with Jarvis's ring, what you said and what Claude is doing.
// The ring's mode follows the helper's state and Claude's turn; in the
// terminal it animates by repainting a Raster ($.ui.blit) while the pane is
// shown, and on the desktop it is an SVG whose motion is SMIL, redrawn when
// the mode or a level changes. The texts redraw from $.state (the view and
// the `hud` value), so a reload keeps them.

import type { Timer } from 'claude-code'

import type { HudAction, JarvisPhase } from '../types'
import type { Engine } from './engine'
import { describeError } from './engine'
import type { HudMode } from './hud-ring'
import { ringCells } from './hud-ring'

export type { HudMode } from './hud-ring'

export const HUD_PANE = 'jarvis-hud'
export const RING_KEY = 'ring'
/** The terminal ring's frame interval while it moves. */
export const FRAME_MS = 50
/** How long the ring flashes after the user talks over Jarvis. */
export const INTERRUPT_MS = 450
/** How many actions the log keeps. */
export const ACTION_LIMIT = 6
/** At rest the ring repaints on every this-many frames. */
const SLEEPING_EVERY = 4
/** The desktop redraws for a level change at most this often. */
const LEVEL_REDRAW_MS = 250

export const MODE_LABELS: Record<HudMode, string> = {
  offline: 'OFFLINE',
  sleeping: 'STANDING BY',
  listening: 'LISTENING',
  thinking: 'THINKING',
  speaking: 'SPEAKING',
  interrupted: 'LISTENING',
}

const OFFLINE: ReadonlySet<JarvisPhase> = new Set([
  'unavailable',
  'stopped',
  'not_installed',
  'setup',
  'error',
  'elsewhere',
  'failed',
])

/** The ring's mode for the helper's phase and Claude's turn. */
export function hudMode(phase: JarvisPhase, isThinking: boolean, isInterrupted = false): HudMode {
  if (OFFLINE.has(phase)) return 'offline'
  if (isInterrupted) return 'interrupted'
  if (phase === 'speaking') return 'speaking'
  if (phase === 'listening') return 'listening'
  if (phase === 'transcribing' || isThinking) return 'thinking'
  if (phase === 'awake') return 'listening'
  return 'sleeping'
}

/** The ring's size in cells for a pane body: twice as wide as tall draws it round. */
export function ringSize(bodyColumns: number, bodyRows: number, textRows: number): { columns: number; rows: number } {
  const rows = Math.max(6, Math.min(24, Math.floor(bodyColumns / 2), bodyRows - textRows))
  return { columns: rows * 2, rows }
}

/** A tool call as one line of the action log ("Bash npm test"). */
export function actionLabel(e: { tool: string } & Record<string, unknown>): string {
  const text = (key: string): string | undefined => {
    const value = e[key]
    return typeof value === 'string' && value.trim() !== '' ? value : undefined
  }
  const path = text('file_path') ?? text('notebook_path') ?? text('path')
  const detail =
    text('description') ?? text('command') ?? (path === undefined ? undefined : path.split(/[\\/]/).pop()) ?? text('pattern') ?? text('url') ?? text('query')
  const line = detail === undefined ? e.tool : `${e.tool} ${detail}`
  const flat = line.replace(/\s+/g, ' ').trim()
  return flat.length > 60 ? `${flat.slice(0, 59)}…` : flat
}

type Mounted = { requestId: string; columns: number; rows: number }

export class Hud {
  phase: JarvisPhase = 'stopped'
  isThinking = false
  /** Levels as they arrive (0..1), and as the ring shows them (eased). */
  private mic = 0
  private out = 0
  private shownMic = 0
  private shownOut = 0
  private interruptedUntil = 0
  private actions: HudAction[] = []
  private nextActionId = 1

  private terminal: Mounted | undefined
  private timer: Timer | undefined
  private isBlitting = false
  private ticks = 0
  private lastStaticMode: HudMode | undefined
  private isDesktopShown = false
  private desktopKey = ''
  private lastDesktopRedraw = 0

  constructor(private readonly engine: Engine) {}

  /** The ring's mode now. */
  mode(now = performance.now()): HudMode {
    return hudMode(this.phase, this.isThinking, now < this.interruptedUntil)
  }

  /** The levels as they arrived, for the desktop ring (its own motion smooths them), and the time its motion has reached. */
  levels(): { mic: number; out: number; t: number } {
    return { mic: this.mic, out: this.out, t: performance.now() / 1000 }
  }

  // -- inputs

  setPhase(phase: JarvisPhase): void {
    if (phase === this.phase) return
    this.phase = phase
    if (phase !== 'listening' && phase !== 'awake') this.mic = 0
    if (phase !== 'speaking') this.out = 0
    this.changed()
  }

  setLevels(mic: number, out: number): void {
    this.mic = clamp(mic)
    this.out = clamp(out)
    if (!this.isDesktopShown) return
    const now = performance.now()
    const key = this.desktopLevelKey()
    if (key !== this.desktopKey && now - this.lastDesktopRedraw >= LEVEL_REDRAW_MS) this.redrawDesktop(now)
  }

  onBargeIn(): void {
    this.interruptedUntil = performance.now() + INTERRUPT_MS
    this.changed()
  }

  onTurnStart(): void {
    if (this.isThinking) return
    this.isThinking = true
    this.changed()
    this.save()
  }

  onTurnComplete(): void {
    if (!this.isThinking) return
    this.isThinking = false
    this.changed()
    this.save()
  }

  /** A tool call started; returns its id for onToolEnd. */
  onToolStart(label: string): number {
    const id = this.nextActionId
    this.nextActionId += 1
    this.actions = [{ id, label, status: 'running' as const }, ...this.actions].slice(0, ACTION_LIMIT)
    this.save()
    return id
  }

  onToolEnd(id: number, isOk: boolean): void {
    if (!this.actions.some(one => one.id === id)) return
    this.actions = this.actions.map(one => (one.id === id ? { ...one, status: isOk ? ('done' as const) : ('failed' as const) } : one))
    this.save()
  }

  // -- drawing

  /**
   * The terminal pane drew a ring of this size: its first frame, and the
   * repaints start (they stop once the pane is gone).
   */
  mountTerminal(requestId: string, columns: number, rows: number): string {
    this.terminal = { requestId, columns, rows }
    this.lastStaticMode = undefined
    this.ensureTimer()
    return this.frame(performance.now())
  }

  /** The desktop pane drew: redraws follow the levels from now on. */
  mountDesktop(): void {
    this.isDesktopShown = true
    this.desktopKey = this.desktopLevelKey()
  }

  frame(now: number): string {
    const terminal = this.terminal
    if (terminal === undefined) return ''
    return ringCells({ mode: this.mode(now), t: now / 1000, mic: this.shownMic, out: this.shownOut }, terminal.columns, terminal.rows)
  }

  /** One animation step: ease the levels and repaint the terminal ring. */
  async tick(now = performance.now()): Promise<void> {
    this.ease()
    const terminal = this.terminal
    if (terminal === undefined || this.isBlitting) return
    const mode = this.mode(now)
    this.ticks += 1
    if (mode === 'sleeping' && this.ticks % SLEEPING_EVERY !== 0) return // slow motion: a few frames a second
    if (mode === 'offline') {
      // A still ring: paint it once.
      if (this.lastStaticMode === mode) return
      this.lastStaticMode = mode
    } else this.lastStaticMode = undefined
    this.isBlitting = true
    try {
      const result = await this.engine.blit({
        requestId: terminal.requestId,
        key: RING_KEY,
        cells: this.frame(now),
        columns: terminal.columns,
        rows: terminal.rows,
      })
      if (result.deny !== undefined && this.terminal === terminal) {
        // Closed, resized or hidden: stop until it draws again.
        this.engine.debug(`jarvis: HUD repaint stopped (${result.deny})`)
        this.terminal = undefined
        this.stopTimer()
      }
    } catch (error) {
      this.engine.debug(`jarvis: HUD repaint failed: ${describeError(error)}`)
      this.terminal = undefined
      this.stopTimer()
    } finally {
      this.isBlitting = false
    }
  }

  /** The pane was closed. */
  onClosed(): void {
    this.terminal = undefined
    this.isDesktopShown = false
    this.stopTimer()
  }

  dispose(): void {
    this.onClosed()
  }

  // -- internals

  private ease(): void {
    // Rise at once, fall over a few frames.
    this.shownMic = this.mic >= this.shownMic ? this.mic : this.shownMic * 0.8 + this.mic * 0.2
    this.shownOut = this.out >= this.shownOut ? this.out : this.shownOut * 0.8 + this.out * 0.2
  }

  private desktopLevelKey(): string {
    return `${this.mode()}:${Math.round(this.mic * 5)}:${Math.round(this.out * 5)}`
  }

  private redrawDesktop(now: number): void {
    this.desktopKey = this.desktopLevelKey()
    this.lastDesktopRedraw = now
    this.engine.invalidate()
  }

  /** The mode may have changed: the desktop redraws (the terminal ring repaints on its own). */
  private changed(): void {
    if (this.isDesktopShown) this.redrawDesktop(performance.now())
  }

  private save(): void {
    void this.engine.writeHud({ isThinking: this.isThinking, actions: this.actions }).catch(() => undefined)
  }

  private ensureTimer(): void {
    if (this.timer !== undefined) return
    this.timer = this.engine.every(FRAME_MS, () => {
      void this.tick()
    })
  }

  private stopTimer(): void {
    this.timer?.cancel()
    this.timer = undefined
  }
}

const clamp = (value: number): number => (Number.isFinite(value) ? Math.min(1, Math.max(0, value)) : 0)
