// The HUD ring for the desktop app: one SVG per mode, its motion in SMIL
// (the surface draws it in a script-less frame), redrawn when the mode or a
// level changes enough to see.

import type { HudMode } from './hud-ring'
import { HUD_COLORS } from './hud-ring'

const SIZE = 240
const C = SIZE / 2

const css = (color: number): string => `#${color.toString(16).padStart(6, '0')}`
const round = (value: number): number => Math.round(value * 10) / 10

/** A full turn every `seconds`; negative turns the other way. */
function spin(seconds: number): string {
  const to = seconds < 0 ? -360 : 360
  return `<animateTransform attributeName="transform" type="rotate" from="0 ${C} ${C}" to="${to} ${C} ${C}" dur="${Math.abs(seconds)}s" repeatCount="indefinite"/>`
}

/** `count` tick marks between radii `inner` and `outer`, every `long`-th one longer. */
function ticks(count: number, inner: number, outer: number, long: number): string {
  const lines: string[] = []
  for (let k = 0; k < count; k += 1) {
    const a = (k / count) * Math.PI * 2
    const r0 = k % long === 0 ? inner - 6 : inner
    lines.push(
      `<line x1="${round(C + Math.cos(a) * r0)}" y1="${round(C + Math.sin(a) * r0)}" x2="${round(C + Math.cos(a) * outer)}" y2="${round(C + Math.sin(a) * outer)}"/>`,
    )
  }
  return lines.join('')
}

/** An arc of `degrees` on radius `r`, centred on the top. */
function topArc(r: number, degrees: number): string {
  const length = (Math.PI * 2 * r * Math.min(359.9, degrees)) / 360
  const circumference = Math.PI * 2 * r
  // Dashes start at 3 o'clock: turn so the arc's middle sits at 12.
  const rotate = -90 - degrees / 2
  return `<circle cx="${C}" cy="${C}" r="${r}" fill="none" stroke-dasharray="${round(length)} ${round(circumference)}" transform="rotate(${round(rotate)} ${C} ${C})"/>`
}

const WORDMARK = (color: string): string =>
  `<text x="${C}" y="${C + 5}" text-anchor="middle" font-family="Segoe UI, Helvetica, Arial, sans-serif" font-size="15" letter-spacing="5" fill="${color}">JARVIS</text>`

/** The ring as an SVG document for `mode`, with the levels (0..1) it shows. */
export function ringSvg(mode: HudMode, mic: number, out: number): string {
  const color = css(HUD_COLORS[mode])
  const parts: string[] = [
    `<defs><filter id="glow" x="-20%" y="-20%" width="140%" height="140%"><feGaussianBlur stdDeviation="3" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter></defs>`,
    // The faint grid of the reference photo.
    `<g fill="none" stroke="${color}" stroke-opacity="0.08">${[30, 60, 90, 116].map(r => `<circle cx="${C}" cy="${C}" r="${r}"/>`).join('')}<line x1="0" y1="${C}" x2="${SIZE}" y2="${C}"/><line x1="${C}" y1="0" x2="${C}" y2="${SIZE}"/></g>`,
  ]
  switch (mode) {
    case 'offline':
      parts.push(`<circle cx="${C}" cy="${C}" r="88" fill="none" stroke="${color}" stroke-width="3"/>`, WORDMARK(color))
      break
    case 'sleeping':
      parts.push(
        `<g stroke="${color}" stroke-opacity="0.5" stroke-width="2">${ticks(48, 104, 112, 6)}${spin(60)}</g>`,
        `<circle cx="${C}" cy="${C}" r="88" fill="none" stroke="${color}" stroke-width="3" stroke-opacity="0.8" filter="url(#glow)"/>`,
        WORDMARK(color),
      )
      break
    case 'listening': {
      const level = Math.min(1, mic)
      parts.push(
        `<g stroke="${color}" stroke-opacity="0.8" stroke-width="2">${ticks(48, 104, 112, 6)}${spin(24)}</g>`,
        `<circle cx="${C}" cy="${C}" r="88" fill="none" stroke="${color}" stroke-width="${round(4 + level * 3)}" filter="url(#glow)"/>`,
        `<g stroke="${color}" stroke-width="${round(4 + level * 6)}" stroke-linecap="round" filter="url(#glow)">${topArc(66, 20 + level * 320)}</g>`,
        WORDMARK(color),
      )
      break
    }
    case 'thinking': {
      const arcs = [0, 120, 240]
        .map(start => {
          const r = 110
          const circumference = Math.PI * 2 * r
          return `<circle cx="${C}" cy="${C}" r="${r}" fill="none" stroke-dasharray="${round(circumference / 6)} ${round(circumference)}" transform="rotate(${start} ${C} ${C})"/>`
        })
        .join('')
      const fragments = Array.from({ length: 6 }, (_, k) => {
        const a = (k / 6) * Math.PI * 2
        return `<circle cx="${round(C + Math.cos(a) * 56)}" cy="${round(C + Math.sin(a) * 56)}" r="5"/>`
      }).join('')
      parts.push(
        `<g stroke="${color}" stroke-width="4" filter="url(#glow)">${arcs}${spin(3)}</g>`,
        `<g stroke="${color}" stroke-opacity="0.6" stroke-width="2">${ticks(36, 96, 100, 3)}${spin(-8)}</g>`,
        `<circle cx="${C}" cy="${C}" r="86" fill="none" stroke="${color}" stroke-width="3" filter="url(#glow)"/>`,
        `<g fill="${color}" filter="url(#glow)">${fragments}${spin(-2.8)}</g>`,
        `<circle cx="${C}" cy="${C}" r="16" fill="${color}" fill-opacity="0.6" filter="url(#glow)"><animate attributeName="r" values="14;19;14" dur="1.4s" repeatCount="indefinite"/></circle>`,
      )
      break
    }
    case 'speaking': {
      const level = Math.min(1, out)
      const rays = Array.from({ length: 16 }, (_, k) => {
        const a = ((k + 0.5) / 16) * Math.PI * 2
        const length = 30 + level * 60 * (0.6 + 0.4 * Math.abs(Math.sin(k * 1.7)))
        const r0 = 28
        return `<line x1="${round(C + Math.cos(a) * r0)}" y1="${round(C + Math.sin(a) * r0)}" x2="${round(C + Math.cos(a) * (r0 + length))}" y2="${round(C + Math.sin(a) * (r0 + length))}"><animate attributeName="stroke-opacity" values="1;0.35;1" dur="${round(0.35 + (k % 4) * 0.08)}s" repeatCount="indefinite"/></line>`
      }).join('')
      const core = round(18 + level * 14)
      parts.push(
        `<circle cx="${C}" cy="${C}" r="94" fill="none" stroke="${color}" stroke-width="${round(3 + level * 3)}" filter="url(#glow)"/>`,
        `<g stroke="#ff7a1a" stroke-width="4" stroke-linecap="round" filter="url(#glow)">${rays}</g>`,
        `<circle cx="${C}" cy="${C}" r="${core}" fill="${color}" filter="url(#glow)"/>`,
        `<circle cx="${C}" cy="${C}" r="${round(core * 0.55)}" fill="#fff3c4"/>`,
      )
      break
    }
    case 'interrupted':
      parts.push(
        `<g stroke="${color}" stroke-width="2">${ticks(48, 104, 112, 6)}</g>`,
        `<circle cx="${C}" cy="${C}" r="88" fill="none" stroke="${color}" stroke-width="6" filter="url(#glow)"/>`,
        WORDMARK(color),
      )
      break
  }
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${SIZE} ${SIZE}" width="${SIZE}" height="${SIZE}">${parts.join('')}</svg>`
}
