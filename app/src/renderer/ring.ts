// Draws the plugin's own HUD ring (plugin/hooks/hud-svg.ts) into a page. The
// SVG's motion is SMIL, so it moves without a script; it is redrawn when the
// mode changes, and for a level change at most every LEVEL_REDRAW_MS, with
// one trailing redraw so the last level always shows. Nothing here touches
// the DOM until a RingView is made, so the module loads in Node tests.

import type { HudMode } from '../../../plugin/hooks/hud-ring'
import { ringSvg } from '../../../plugin/hooks/hud-svg'

export const LEVEL_REDRAW_MS = 150

export type Drawn = { mode: HudMode; micTenth: number; outTenth: number; at: number }

const tenth = (level: number): number => Math.round(level * 10)

export function shouldRedraw(drawn: Drawn | undefined, mode: HudMode, mic: number, out: number, now: number): boolean {
  if (drawn === undefined || drawn.mode !== mode) return true
  if (drawn.micTenth === tenth(mic) && drawn.outTenth === tenth(out)) return false
  return now - drawn.at >= LEVEL_REDRAW_MS
}

export class RingView {
  private drawn: Drawn | undefined
  private trailing: ReturnType<typeof setTimeout> | undefined
  private latest: { mode: HudMode; mic: number; out: number } | undefined

  constructor(private readonly host: HTMLElement) {}

  show(mode: HudMode, mic: number, out: number): void {
    this.latest = { mode, mic, out }
    const now = performance.now()
    if (shouldRedraw(this.drawn, mode, mic, out, now)) {
      this.draw(now)
      return
    }
    const drawn = this.drawn
    if (drawn === undefined || this.trailing !== undefined) return
    if (drawn.micTenth === tenth(mic) && drawn.outTenth === tenth(out)) return
    this.trailing = setTimeout(() => {
      this.trailing = undefined
      this.draw(performance.now())
    }, LEVEL_REDRAW_MS - (now - drawn.at))
  }

  private draw(now: number): void {
    const latest = this.latest
    if (latest === undefined) return
    clearTimeout(this.trailing)
    this.trailing = undefined
    const { mode, mic, out } = latest
    const doc = new DOMParser().parseFromString(ringSvg(mode, mic, out, now / 1000), 'image/svg+xml')
    this.host.replaceChildren(document.importNode(doc.documentElement, true))
    this.drawn = { mode, micTenth: tenth(mic), outTenth: tenth(out), at: now }
  }
}
