// The HUD ring for the desktop app: one SVG per mode, its motion in SMIL
// (the surface draws it in a script-less frame), redrawn when the mode or a
// level changes enough to see. Each spin starts at the angle it has reached
// at `t`, so a redraw carries the motion on rather than restarting it.
//
// It follows the two reference images. At rest and listening: the monitor
// photo, a glowing blue ring around "JARVIS" on a dark grid with a ruler
// along the top, a white arc to its left and a sparse ring of dots.
// Thinking and speaking: the orange sphere, a broken ring of glowing
// fragments around a bright core wrapped in elliptical orbits, inside a
// frame of amber bars and a small dial.

import type { HudMode } from './hud-ring'

const SIZE = 240
const C = SIZE / 2
const TAU = Math.PI * 2

const round = (value: number): number => Math.round(value * 10) / 10

/** A small seeded generator, so the fragments sit still between redraws. */
function random(seed: number): () => number {
  let state = seed >>> 0
  return () => {
    state = (state + 0x6d2b79f5) >>> 0
    let x = state
    x = Math.imul(x ^ (x >>> 15), x | 1)
    x ^= x + Math.imul(x ^ (x >>> 7), x | 61)
    return ((x ^ (x >>> 14)) >>> 0) / 4294967296
  }
}

/** A full turn every `seconds` (negative turns the other way) about `x`, `y`, already `t` seconds in. */
function spin(seconds: number, t: number, x = C, y = C): string {
  const start = round((((t / seconds) * 360) % 360 + 360) % 360)
  const end = round(start + (seconds < 0 ? -360 : 360))
  return `<animateTransform attributeName="transform" type="rotate" from="${start} ${x} ${y}" to="${end} ${x} ${y}" dur="${Math.abs(seconds)}s" repeatCount="indefinite"/>`
}

/** A point at `r` and angle `a` (radians, 0 at the right, clockwise). */
const at = (r: number, a: number): string => `${round(C + Math.cos(a) * r)} ${round(C + Math.sin(a) * r)}`

/** An arc on radius `r` from angle `from` to `to` (radians, clockwise). */
function arc(r: number, from: number, to: number): string {
  const large = to - from > Math.PI ? 1 : 0
  return `M${at(r, from)}A${r} ${r} 0 ${large} 1 ${at(r, to)}`
}

const DEFS = `<defs>
<filter id="glow" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="4" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
<filter id="soft" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="1.6" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
<filter id="haze" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="10"/></filter>
<pattern id="grid" width="16" height="16" patternUnits="userSpaceOnUse" x="8" y="8"><path d="M16 0H0V16" fill="none" stroke="#ffffff" stroke-opacity="0.06" stroke-width="0.6"/></pattern>
<radialGradient id="core"><stop offset="0" stop-color="#fff6dc"/><stop offset="0.35" stop-color="#ffd27a"/><stop offset="0.7" stop-color="#ff8a1a" stop-opacity="0.55"/><stop offset="1" stop-color="#ff6a00" stop-opacity="0"/></radialGradient>
<radialGradient id="disc"><stop offset="0" stop-color="#2a1708" stop-opacity="0.55"/><stop offset="0.85" stop-color="#1a0f07" stop-opacity="0.75"/><stop offset="1" stop-color="#1a0f07" stop-opacity="0"/></radialGradient>
</defs>`

/** The monitor photo: grid, ruler, the blue ring and its marks. */
function blueRing(mode: HudMode, mic: number, t: number): string {
  const isOffline = mode === 'offline'
  const level = mode === 'listening' ? Math.min(1, mic) : 0
  const blue = isOffline ? '#5d666e' : mode === 'interrupted' ? '#9fdcff' : '#2f9bff'
  const edge = isOffline ? '#8a939a' : '#d6efff'
  const white = isOffline ? '#7b848b' : '#ffffff'
  const glow = isOffline ? '' : ' filter="url(#glow)"'
  const parts: string[] = [
    `<rect width="${SIZE}" height="${SIZE}" rx="10" fill="#121519"/>`,
    `<rect width="${SIZE}" height="${SIZE}" rx="10" fill="url(#grid)"/>`,
  ]
  // The ruler and bracket along the top.
  const ruler: string[] = []
  for (let x = 14; x <= SIZE - 14; x += 8) ruler.push(`<line x1="${x}" y1="14" x2="${x}" y2="${(x - 14) % 32 === 0 ? 20 : 17}"/>`)
  parts.push(
    `<g stroke="#ffffff" stroke-opacity="0.22" stroke-width="0.7">${ruler.join('')}</g>`,
    `<path d="M70 6H92L98 10H142L148 6H170" fill="none" stroke="#ffffff" stroke-opacity="0.3" stroke-width="0.8"/>`,
  )
  // The halo, the ring with its bright inner edge, and the dark inside.
  if (!isOffline) {
    parts.push(
      `<circle cx="${C}" cy="${C}" r="56" fill="none" stroke="${blue}" stroke-width="${round(22 + level * 16)}" stroke-opacity="${round(0.22 + level * 0.2)}" filter="url(#haze)"><animate attributeName="stroke-opacity" values="${round(0.16 + level * 0.2)};${round(0.3 + level * 0.2)};${round(0.16 + level * 0.2)}" dur="4s" repeatCount="indefinite"/></circle>`,
    )
  }
  const ringWidth = round(8 + level * 4)
  parts.push(
    `<circle cx="${C}" cy="${C}" r="52" fill="#0b0d10"/>`,
    `<circle cx="${C}" cy="${C}" r="56" fill="none" stroke="${blue}" stroke-width="${ringWidth}"${glow}/>`,
    `<circle cx="${C}" cy="${C}" r="${round(56 - ringWidth / 2 + 0.6)}" fill="none" stroke="${edge}" stroke-width="1.4" stroke-opacity="0.9"/>`,
    `<text x="${C}" y="${C + 5.5}" text-anchor="middle" font-family="Segoe UI, Helvetica, Arial, sans-serif" font-size="15" font-weight="500" letter-spacing="4" fill="${white}">JARVIS</text>`,
  )
  // The white arc on the left, sweeping as you speak, its twin on the right, and a faint blue arc low right.
  const half = 0.42 + level * 1.5
  const marks = [`<path d="${arc(74, Math.PI - half, Math.PI + half)}"/>`]
  if (level > 0.15) marks.push(`<path d="${arc(74, -(level - 0.15) * 1.6, (level - 0.15) * 1.6)}"/>`)
  parts.push(
    `<g fill="none" stroke="${white}" stroke-width="1.8" stroke-linecap="round"${isOffline ? '' : ' filter="url(#soft)"'}>${marks.join('')}${
      isOffline ? '' : `<animateTransform attributeName="transform" type="rotate" values="-12 ${C} ${C};12 ${C} ${C};-12 ${C} ${C}" dur="16s" repeatCount="indefinite"/>`
    }</g>`,
    `<path d="${arc(68, Math.PI / 4 - 0.45, Math.PI / 4 + 0.45)}" fill="none" stroke="${blue}" stroke-opacity="0.45" stroke-width="1.4"/>`,
  )
  // The ring of dots, a few missing, and ticks: long ones top and bottom.
  const next = random(7)
  const dots: string[] = []
  for (let k = 0; k < 48; k += 1) {
    const isGap = next() < 0.22
    if (isGap) continue
    const a = (k / 48) * TAU
    dots.push(k % 6 === 3 ? `<path d="M${at(94, a)}L${at(100, a)}" stroke="${white}"/>` : `<circle cx="${round(C + Math.cos(a) * 97)}" cy="${round(C + Math.sin(a) * 97)}" r="1"/>`)
  }
  const dotOpacity = round(0.45 + level * 0.4)
  parts.push(
    `<g fill="${white}" fill-opacity="${dotOpacity}" stroke-opacity="${dotOpacity}" stroke-width="1">${dots.join('')}${isOffline ? '' : spin(90 - level * 60, t)}</g>`,
    `<g stroke="${white}" stroke-opacity="0.75" stroke-width="1.4"><path d="M${C} 18V30"/><path d="M${C} 210V222"/></g>`,
  )
  return parts.join('')
}

/** The orange sphere: frame, fragments, orbits and the core. */
function amberSphere(mode: HudMode, out: number, t: number): string {
  const isSpeaking = mode === 'speaking'
  const level = isSpeaking ? Math.min(1, out) : 0.35
  const parts: string[] = [`<rect width="${SIZE}" height="${SIZE}" rx="10" fill="#0a0705"/>`]
  // The frame: bars top and bottom, lines down the right, a dial in the corner.
  parts.push(
    `<g fill="#3a2410"><rect x="14" y="12" width="150" height="5"/><rect x="40" y="226" width="160" height="3"/></g>`,
    `<g fill="#c8792a"><rect x="14" y="17" width="58" height="2"/><rect x="132" y="12" width="20" height="5" fill-opacity="0.7"/></g>`,
    `<g stroke="#5a3a18" stroke-width="1"><path d="M224 26V200"/><path d="M229 40V150"/><path d="M8 70H44"/></g>`,
    `<g fill="none" stroke="#c8792a" stroke-opacity="0.8"><circle cx="216" cy="216" r="9" stroke-dasharray="10 4"/><circle cx="216" cy="216" r="4" fill="#ffc05a" fill-opacity="0.6"/>${spin(
      isSpeaking ? 3 : 6,
      t,
      216,
      216,
    )}</g>`,
    `<circle cx="${C}" cy="${C}" r="108" fill="url(#disc)"/>`,
  )
  // Fragments: glinting pieces around the sphere at random radii, some gaps; two layers turning apart.
  const colors = ['#ff7a1a', '#ff9a2a', '#ffb347', '#ffd27a']
  const layer = (seed: number, count: number, inner: number, outer: number, seconds: number, opacity: number): string => {
    const next = random(seed)
    const pieces: string[] = []
    for (let k = 0; k < count; k += 1) {
      const a = (k / count) * TAU + next() * 0.05
      if (next() < 0.18) continue
      const r = inner + next() * (outer - inner)
      const color = colors[Math.floor(next() * colors.length)] as string
      const alpha = round((0.35 + next() * 0.65) * opacity)
      const width = round(0.8 + next() * 2)
      const kind = next()
      if (kind < 0.55) {
        // A radial streak.
        const length = 2 + next() * 9
        pieces.push(`<path d="M${at(r, a)}L${at(r + length, a)}" stroke="${color}" stroke-opacity="${alpha}" stroke-width="${width}"/>`)
      } else if (kind < 0.85) {
        // A piece of arc.
        const span = 0.02 + next() * 0.12
        pieces.push(`<path d="${arc(r, a, a + span)}" stroke="${color}" stroke-opacity="${alpha}" stroke-width="${width}" fill="none"/>`)
      } else {
        // A glint.
        pieces.push(`<circle cx="${round(C + Math.cos(a) * r)}" cy="${round(C + Math.sin(a) * r)}" r="${round(width * 0.7)}" fill="${color}" fill-opacity="${alpha}"/>`)
      }
    }
    return `<g filter="url(#soft)">${pieces.join('')}${spin(seconds, t)}</g>`
  }
  const bright = round(0.8 + level * 0.3)
  parts.push(
    // A haze of light along the ring, under the fragments.
    `<circle cx="${C}" cy="${C}" r="88" fill="none" stroke="#ff8a1a" stroke-width="22" stroke-opacity="${round(0.16 + level * 0.12)}" filter="url(#haze)"/>`,
    layer(11, 220, 72, 106, isSpeaking ? 50 : 30, bright),
    layer(23, 150, 56, 98, isSpeaking ? -70 : -45, bright * 0.8),
    `<g fill="none" stroke="#ff9a2a" stroke-opacity="0.55" stroke-width="1.2">${[0.3, 1.5, 2.6, 3.7, 4.9, 5.8]
      .map(a => `<path d="${arc(50, a, a + 0.5)}"/>`)
      .join('')}${spin(-24, t)}</g>`,
  )
  // Orbits: tilted ellipses turning around the core, apart from each other.
  const orbits = [0, 60, 120]
    .map((tilt, i) => {
      const seconds = (isSpeaking ? 9 : 5) + i * 2
      return `<g transform="rotate(${tilt} ${C} ${C})"><g><ellipse cx="${C}" cy="${C}" rx="58" ry="17"/>${spin(i % 2 === 0 ? seconds : -seconds, t)}</g></g>`
    })
    .join('')
  parts.push(`<g fill="none" stroke="#ffc05a" stroke-opacity="0.6" stroke-width="1.3" filter="url(#soft)">${orbits}</g>`)
  // The core; while speaking, streaks out from it with the voice.
  const core = round(20 + level * 18)
  if (isSpeaking) {
    const next = random(31)
    const rays: string[] = []
    for (let k = 0; k < 20; k += 1) {
      const a = (k / 20) * TAU + next() * 0.2
      const length = 18 + level * 60 * (0.4 + next() * 0.6)
      rays.push(
        `<path d="M${at(core * 0.6, a)}L${at(core * 0.6 + length, a)}"><animate attributeName="stroke-opacity" values="0.9;0.25;0.9" dur="${round(0.3 + next() * 0.4)}s" repeatCount="indefinite"/></path>`,
      )
    }
    parts.push(`<g stroke="#ffb347" stroke-width="1.6" stroke-linecap="round" filter="url(#soft)">${rays.join('')}</g>`)
  }
  parts.push(
    `<circle cx="${C}" cy="${C}" r="${core}" fill="url(#core)" filter="url(#soft)">${
      isSpeaking ? '' : `<animate attributeName="r" values="${core};${round(core * 1.18)};${core}" dur="1.6s" repeatCount="indefinite"/>`
    }</circle>`,
  )
  return parts.join('')
}

/** The ring as an SVG document for `mode`, with the levels (0..1) it shows, `t` seconds into its motion. */
export function ringSvg(mode: HudMode, mic: number, out: number, t = 0): string {
  const body = mode === 'thinking' || mode === 'speaking' ? amberSphere(mode, out, t) : blueRing(mode, mic, t)
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${SIZE} ${SIZE}" width="${SIZE}" height="${SIZE}">${DEFS}${body}</svg>`
}
