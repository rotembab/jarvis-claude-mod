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
/** The desktop redraws for a level change at most this often (it swells and shrinks with the voice). */
const LEVEL_REDRAW_MS = 150
/** The terminal ring's largest size in rows (twice as many columns). */
export const RING_MAX_ROWS = 64
/** Cells a repaint every frame may cost; a bigger ring repaints on every second or third frame. */
const CELLS_PER_FRAME = 3000
/** The pane's size before the surface has said how big the screen is. */
export const PANE_START = { rows: 24, columns: 52 }
/** Lines of Claude's reply shown under the ring in focus mode. */
export const REPLY_ROWS = 3

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

/** The ring's size in cells for a pane body: twice as wide as tall draws it round, as big as the body allows. */
export function ringSize(bodyColumns: number, bodyRows: number, textRows: number): { columns: number; rows: number } {
  const rows = Math.max(6, Math.min(RING_MAX_ROWS, Math.floor(bodyColumns / 2), bodyRows - textRows))
  return { columns: rows * 2, rows }
}

/**
 * Where the surface seated the pane, the body it gave it, and the room beside
 * it: a dock's `columns` are the conversation's beside it, `rows` the screen's.
 */
export type PaneLayout = { placement: 'dock' | 'inline'; bodyColumns: number; bodyRows: number; columns: number; rows: number }
export type PaneSize = { rows: number; columns: number }

/**
 * The size the HUD asks for: as much room as the ring can use. Docked beside
 * the conversation (floor to ceiling) it is as wide as the ring its height
 * allows, up to half the screen; inline above the prompt, half the screen's
 * height. In focus mode the conversation is folded away, so it asks for
 * nearly the whole screen.
 */
export function paneSize(layout: PaneLayout, textRows: number, isFocus: boolean): PaneSize {
  if (layout.placement === 'dock') {
    const screen = layout.columns + layout.bodyColumns + 1
    if (isFocus) return { rows: PANE_START.rows, columns: Math.max(PANE_START.columns, screen - 4) }
    const ringRows = Math.min(RING_MAX_ROWS, layout.bodyRows - textRows)
    return { rows: PANE_START.rows, columns: Math.max(PANE_START.columns, Math.min(2 * ringRows + 2, Math.floor(screen / 2))) }
  }
  const rows = isFocus ? layout.rows - 8 : Math.floor(layout.rows / 2)
  return { rows: Math.max(PANE_START.rows, rows), columns: PANE_START.columns }
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
  private layout: PaneLayout | undefined
  private asked: PaneSize = PANE_START
  private isFocus = false
  private lastReply: string | undefined

  constructor(private readonly engine: Engine) {}

  /** The ring's mode now. */
  mode(now = performance.now()): HudMode {
    return hudMode(this.phase, this.isThinking, now < this.interruptedUntil)
  }

  /** Whether the terminal pane is drawn (focus mode is the terminal's). */
  get isShown(): boolean {
    return this.terminal !== undefined
  }

  /** Whether the pane is docked beside the conversation (the fullscreen view) rather than above the prompt. */
  get isDocked(): boolean {
    return this.layout?.placement === 'dock'
  }

  /** The size to open the pane at: what the HUD asked for last. */
  get size(): PaneSize {
    return this.asked
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

  /** Claude's last reply, for focus mode. */
  setLastReply(text: string | undefined): void {
    if (text === this.lastReply) return
    this.lastReply = text
    this.save()
  }

  /** Focus mode came or went: the pane asks for its new size. */
  setFocus(isFocus: boolean): void {
    if (isFocus === this.isFocus) return
    this.isFocus = isFocus
    this.save()
    this.resize()
  }

  /**
   * The pane drew with this screen: when the size it should have changed,
   * it asks for it (the person's own drag or keys still win).
   */
  onLayout(layout: PaneLayout): void {
    const last = this.layout
    if (last !== undefined && (Object.keys(layout) as (keyof PaneLayout)[]).every(key => last[key] === layout[key])) return
    this.layout = layout
    this.resize()
    if (last?.placement !== layout.placement) this.onShownChange?.(this.isShown)
  }

  // -- drawing

  /**
   * The terminal pane drew a ring of this size: its first frame, and the
   * repaints start (they stop once the pane is gone).
   */
  mountTerminal(requestId: string, columns: number, rows: number): string {
    const wasShown = this.isShown
    this.terminal = { requestId, columns, rows }
    this.lastStaticMode = undefined
    this.ensureTimer()
    if (!wasShown) this.onShownChange?.(true)
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
    // A big ring repaints less often, so the terminal keeps up.
    if (this.ticks % Math.ceil((terminal.columns * terminal.rows) / CELLS_PER_FRAME) !== 0) return
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
    const wasShown = this.isShown
    this.terminal = undefined
    this.isDesktopShown = false
    // Until it draws again there is nothing to resize (a resize would open it again).
    this.layout = undefined
    this.stopTimer()
    if (wasShown) this.onShownChange?.(false)
  }

  /** Called when the terminal pane starts or stops being drawn, or moves between dock and inline (focus mode follows both). */
  onShownChange: ((isShown: boolean) => void) | undefined

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
    return `${this.mode()}:${Math.round(this.mic * 10)}:${Math.round(this.out * 10)}`
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

  /** Writes what the pane shows to the session state (a new module's HUD starts from its own). */
  save(): void {
    const hud = {
      isThinking: this.isThinking,
      actions: this.actions,
      ...(this.lastReply === undefined ? {} : { lastReply: this.lastReply }),
      ...(this.isFocus ? { isFocus: true } : {}),
    }
    void this.engine.writeHud(hud).catch(() => undefined)
  }

  /** Asks for the size the screen and focus mode call for, once it differs from the last ask. */
  private resize(): void {
    const layout = this.layout
    if (layout === undefined) return
    const size = paneSize(layout, hudTextRows(this.isFocus), this.isFocus)
    if (size.rows === this.asked.rows && size.columns === this.asked.columns) return
    this.asked = size
    // Not from inside a drawing: the open goes out on the next tick.
    this.engine.after(0, () => {
      void this.engine.openPane(size).catch(error => this.engine.debug(`jarvis: HUD resize failed: ${describeError(error)}`))
    })
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

/** The rows the title and the texts under the ring take at most. */
export function hudTextRows(isFocus: boolean): number {
  return 1 + 2 + ACTION_LIMIT + (isFocus ? REPLY_ROWS : 0)
}

/** The narrowest the texts may be beside the ring. */
const SIDE_TEXT_COLUMNS = 40
/** Lines of the reply when the texts sit beside the ring. */
const SIDE_REPLY_ROWS = 8

/** How a terminal pane's body is shared between the ring and the texts. */
export type HudLayout = {
  ring: { columns: number; rows: number }
  /** The texts sit beside the ring, else under it. */
  isSide: boolean
  /** Cells across the texts. */
  textColumns: number
  replyRows: number
  actionRows: number
}

/**
 * The ring as big as the body allows, the texts under it; or, in a body wide
 * enough that a column of texts beside it lets the ring grow (focus mode), beside it.
 */
export function hudLayout(bodyColumns: number, bodyRows: number, hasUtterance: boolean, isFocus: boolean): HudLayout {
  const stacked = ringSize(bodyColumns, bodyRows, hudTextRows(isFocus))
  const sideRows = Math.min(RING_MAX_ROWS, bodyRows - 1, Math.floor((bodyColumns - SIDE_TEXT_COLUMNS - 2) / 2))
  const utteranceRows = hasUtterance ? 1 : 0
  if (sideRows > stacked.rows) {
    const replyRows = isFocus ? SIDE_REPLY_ROWS : 0
    return {
      ring: { columns: sideRows * 2, rows: sideRows },
      isSide: true,
      textColumns: bodyColumns - sideRows * 2 - 2,
      replyRows,
      actionRows: Math.max(0, sideRows - utteranceRows - replyRows - 1),
    }
  }
  const replyRows = isFocus ? REPLY_ROWS : 0
  return {
    ring: stacked,
    isSide: false,
    textColumns: bodyColumns,
    replyRows,
    actionRows: Math.max(0, bodyRows - 1 - stacked.rows - utteranceRows - replyRows),
  }
}

const clamp = (value: number): number => (Number.isFinite(value) ? Math.min(1, Math.max(0, value)) : 0)
