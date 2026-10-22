// When the overlay shows. "auto" shows it while you talk to Jarvis: from a
// wake (wake word or push-to-talk) through his thinking and speaking, then
// for a few seconds after he goes back to sleep. A typed prompt makes him
// think without waking, so it never shows the overlay. Ctrl+Alt+J (or the orb)
// flips it by hand; that choice holds until the automatic one changes.

import type { HudMode } from '../../../plugin/hooks/hud-ring'

/** How long the overlay stays after a turn ends. */
export const LINGER_MS = 4000

export type OverlaySetting = 'auto' | 'always' | 'off'
export const OVERLAY_SETTINGS: readonly OverlaySetting[] = ['auto', 'always', 'off']

/** Helper phases that mean the user just spoke to Jarvis. */
export const WAKE_PHASES: ReadonlySet<string> = new Set(['listening', 'awake', 'transcribing', 'speaking'])
/** Ring modes of a turn still under way. */
export const BUSY_MODES: ReadonlySet<HudMode> = new Set<HudMode>(['listening', 'thinking', 'speaking', 'interrupted'])

/** What the shown session is doing; undefined when no session is live. */
export type Shown = { mode: HudMode; phase: string } | undefined

export class OverlayVisibility {
  private current: OverlaySetting
  /** Jarvis was woken and the turn (or its linger) is not over. */
  private engaged = false
  private lingerUntil: number | undefined
  /** A choice made by hand, until the automatic one flips. */
  private manual: boolean | undefined
  private lastAuto: boolean
  private visible: boolean

  constructor(setting: OverlaySetting) {
    this.current = setting
    this.lastAuto = setting === 'always'
    this.visible = this.lastAuto
  }

  get isVisible(): boolean {
    return this.visible
  }

  get setting(): OverlaySetting {
    return this.current
  }

  update(shown: Shown, now: number): void {
    if (shown === undefined || shown.mode === 'offline') {
      this.engaged = false
      this.lingerUntil = undefined
    } else if (WAKE_PHASES.has(shown.phase)) {
      this.engaged = true
      this.lingerUntil = undefined
    } else if (this.engaged && BUSY_MODES.has(shown.mode)) {
      this.lingerUntil = undefined
    } else if (this.engaged) {
      this.lingerUntil ??= now + LINGER_MS
      if (now >= this.lingerUntil) {
        this.engaged = false
        this.lingerUntil = undefined
      }
    }
    const auto = this.auto()
    if (auto !== this.lastAuto) this.manual = undefined
    this.lastAuto = auto
    this.visible = this.manual ?? auto
  }

  toggle(): void {
    this.manual = !this.visible
    this.visible = this.manual
  }

  /** Shows it by hand (a second launch of the app asks for this). */
  show(): void {
    this.manual = true
    this.visible = true
  }

  setSetting(setting: OverlaySetting): void {
    this.current = setting
    this.manual = undefined
    this.lastAuto = this.auto()
    this.visible = this.lastAuto
  }

  private auto(): boolean {
    return this.current === 'always' || (this.current === 'auto' && this.engaged)
  }
}
