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
<radialGradient id="ball" cx="0.38" cy="0.32" r="0.75"><stop offset="0" stop-color="#6a3410" stop-opacity="0.6"/><stop offset="0.55" stop-color="#2e1607" stop-opacity="0.55"/><stop offset="0.95" stop-color="#140a04" stop-opacity="0.5"/><stop offset="1" stop-color="#0a0705" stop-opacity="0"/></radialGradient>
<linearGradient id="depthFade" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#262626"/><stop offset="0.5" stop-color="#808080"/><stop offset="1" stop-color="#ffffff"/></linearGradient>
<mask id="depth" maskUnits="userSpaceOnUse" x="-90" y="-90" width="180" height="180"><rect x="-80" y="-80" width="160" height="160" fill="url(#depthFade)"/></mask>
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
  const backdrop = parts.splice(0)
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
  return backdrop.join('') + zoomed(parts.join(''), mode, mic, 0, t)
}

/** The sphere's radius, and how far the camera looks down on it (radians). */
const R = 74
const TILT = 0.38

/**
 * The sphere's turning shell: rings of fragments at each latitude. Each ring
 * is drawn flat, spun in its own plane and squashed, which is exactly how a
 * ring on a turning ball looks from above; the `depth` mask, in that flat
 * space, dims the half that is behind.
 */
function shell(seconds: number, t: number, opacity: number): string {
  const colors = ['#ff7a1a', '#ff9a2a', '#ffb347', '#ffd27a']
  const next = random(41)
  const squash = round(Math.sin(TILT) * 1000) / 1000
  const rings: string[] = []
  for (const degrees of [-68, -54, -40, -27, -14, -2, 11, 24, 37, 50, 63]) {
    const latitude = (degrees * Math.PI) / 180
    const radius = R * Math.cos(latitude)
    const y = C - R * Math.sin(latitude) * Math.cos(TILT)
    const count = Math.round(30 * Math.cos(latitude)) + 5
    const pieces: string[] = []
    for (let j = 0; j < count; j += 1) {
      const a = (j / count) * TAU + next() * 0.15
      if (next() < 0.28) continue
      const r = round(radius * (0.97 + next() * 0.06))
      const color = colors[Math.floor(next() * colors.length)] as string
      const alpha = round(Math.min(1, (0.55 + next() * 0.45) * opacity))
      const width = round(1.1 + next() * 1.5)
      if (next() < 0.8) {
        const span = 0.05 + next() * 0.16
        const from = `${round(Math.cos(a) * r)} ${round(Math.sin(a) * r)}`
        const to = `${round(Math.cos(a + span) * r)} ${round(Math.sin(a + span) * r)}`
        pieces.push(`<path d="M${from}A${r} ${r} 0 0 1 ${to}" stroke="${color}" stroke-opacity="${alpha}" stroke-width="${width}" vector-effect="non-scaling-stroke"/>`)
      } else {
        pieces.push(`<circle cx="${round(Math.cos(a) * r)}" cy="${round(Math.sin(a) * r)}" r="${round(width)}" fill="${color}" fill-opacity="${alpha}"/>`)
      }
    }
    rings.push(
      `<g transform="translate(${C} ${round(y)}) scale(1 ${squash})" mask="url(#depth)"><g>${pieces.join('')}${spin(seconds, t, 0, 0)}</g></g>`,
    )
  }
  return `<g fill="none" stroke-linecap="round" filter="url(#soft)">${rings.join('')}</g>`
}

/** Two meridians sweeping across the ball as it turns. */
function meridians(seconds: number, t: number): string {
  const half = seconds / 2
  return [0, half / 2]
    .map(offset => {
      const begin = round(-((t + offset) % half))
      return `<ellipse cx="${C}" cy="${C}" rx="${R}" ry="${round(R * 0.97)}"><animate attributeName="rx" values="${R};0;${R}" keyTimes="0;0.5;1" calcMode="spline" keySplines="0.45 0 0.9 0.55;0.1 0.45 0.55 1" dur="${round(half)}s" begin="${begin}s" repeatCount="indefinite"/></ellipse>`
    })
    .join('')
}

/** The orange sphere: frame, the turning 3D shell, debris, orbits and the core. */
function amberSphere(mode: HudMode, out: number, t: number): string {
  const isSpeaking = mode === 'speaking'
  const level = isSpeaking ? Math.min(1, out) : 0.35
  const turn = isSpeaking ? 20 : 13
  // The frame stays put: bars top and bottom, lines down the right, a dial in the corner.
  const frame = [
    `<rect width="${SIZE}" height="${SIZE}" rx="10" fill="#0a0705"/>`,
    `<g fill="#3a2410"><rect x="14" y="12" width="150" height="5"/><rect x="40" y="226" width="160" height="3"/></g>`,
    `<g fill="#c8792a"><rect x="14" y="17" width="58" height="2"/><rect x="132" y="12" width="20" height="5" fill-opacity="0.7"/></g>`,
    `<g stroke="#5a3a18" stroke-width="1"><path d="M224 26V200"/><path d="M229 40V150"/><path d="M8 70H44"/></g>`,
    `<g fill="none" stroke="#c8792a" stroke-opacity="0.8"><circle cx="216" cy="216" r="9" stroke-dasharray="10 4"/><circle cx="216" cy="216" r="4" fill="#ffc05a" fill-opacity="0.6"/>${spin(
      isSpeaking ? 3 : 6,
      t,
      216,
      216,
    )}</g>`,
  ].join('')

  // Debris around the ball: glinting pieces at random radii, some gaps, turning slowly.
  const colors = ['#ff7a1a', '#ff9a2a', '#ffb347', '#ffd27a']
  const next = random(11)
  const debris: string[] = []
  for (let k = 0; k < 170; k += 1) {
    const a = (k / 170) * TAU + next() * 0.05
    if (next() < 0.3) continue
    const r = 84 + next() * 24
    const color = colors[Math.floor(next() * colors.length)] as string
    const alpha = round(0.3 + next() * 0.6)
    const width = round(0.8 + next() * 1.8)
    if (next() < 0.6) {
      debris.push(`<path d="M${at(r, a)}L${at(r + 2 + next() * 8, a)}" stroke="${color}" stroke-opacity="${alpha}" stroke-width="${width}"/>`)
    } else {
      debris.push(`<path d="${arc(r, a, a + 0.02 + next() * 0.1)}" stroke="${color}" stroke-opacity="${alpha}" stroke-width="${width}" fill="none"/>`)
    }
  }

  // Orbits round the core, tilted rings turning apart.
  const orbits = [0, 60, 120]
    .map((tilt, i) => {
      const seconds = (isSpeaking ? 9 : 5) + i * 2
      return `<g transform="rotate(${tilt} ${C} ${C})"><g><ellipse cx="${C}" cy="${C}" rx="46" ry="13"/>${spin(i % 2 === 0 ? seconds : -seconds, t)}</g></g>`
    })
    .join('')

  // The core; while speaking, streaks out from it with the voice.
  const core = round(18 + level * 14)
  const rays: string[] = []
  if (isSpeaking) {
    const ray = random(31)
    for (let k = 0; k < 20; k += 1) {
      const a = (k / 20) * TAU + ray() * 0.2
      const length = 14 + level * 46 * (0.4 + ray() * 0.6)
      rays.push(
        `<path d="M${at(core * 0.6, a)}L${at(core * 0.6 + length, a)}"><animate attributeName="stroke-opacity" values="0.9;0.25;0.9" dur="${round(0.3 + ray() * 0.4)}s" repeatCount="indefinite"/></path>`,
      )
    }
  }

  const ball = [
    // Volume: the ball lit from the top left, a rim of light, a haze around it.
    `<circle cx="${C}" cy="${C}" r="${R + 4}" fill="url(#ball)"/>`,
    `<circle cx="${C}" cy="${C}" r="${R + 8}" fill="none" stroke="#ff8a1a" stroke-width="24" stroke-opacity="${round(0.14 + level * 0.1)}" filter="url(#haze)"/>`,
    `<circle cx="${C}" cy="${C}" r="${R}" fill="none" stroke="#ff9a2a" stroke-opacity="0.3" stroke-width="1.2" filter="url(#soft)"/>`,
    `<g fill="none" stroke="#ff9a2a" stroke-opacity="0.22" stroke-width="1">${meridians(turn, t)}</g>`,
    shell(turn, t, round(0.85 + level * 0.25)),
    `<g filter="url(#soft)">${debris.join('')}${spin(isSpeaking ? 70 : 45, t)}</g>`,
    `<g fill="none" stroke="#ffc05a" stroke-opacity="0.55" stroke-width="1.2" filter="url(#soft)">${orbits}</g>`,
    rays.length > 0 ? `<g stroke="#ffb347" stroke-width="1.6" stroke-linecap="round" filter="url(#soft)">${rays.join('')}</g>` : '',
    `<circle cx="${C}" cy="${C}" r="${core}" fill="url(#core)" filter="url(#soft)">${
      isSpeaking ? '' : `<animate attributeName="r" values="${core};${round(core * 1.18)};${core}" dur="1.6s" repeatCount="indefinite"/>`
    }</circle>`,
  ].join('')
  return frame + zoomed(ball, mode, 0, out, t)
}

/**
 * Draws `content` swelling and shrinking about the centre: with Jarvis's
 * voice while he speaks, with yours while he listens, a slow breath while he
 * thinks. The size follows the level at each redraw; a short pulse keeps it
 * moving in between.
 */
function zoomed(content: string, mode: HudMode, mic: number, out: number, t: number): string {
  let scale = 1
  let pulse = 0
  let seconds = 1
  if (mode === 'speaking') {
    const level = Math.min(1, out)
    scale = 0.84 + 0.16 * level
    pulse = 0.015 + 0.04 * level
    seconds = 0.7
  } else if (mode === 'listening') {
    const level = Math.min(1, mic)
    scale = 0.95 + 0.06 * level
    pulse = 0.02 * level
    seconds = 0.8
  } else if (mode === 'thinking') {
    scale = 0.93
    pulse = 0.025
    seconds = 2.4
  }
  if (scale === 1 && pulse === 0) return content
  const s = (value: number): string => `${Math.round(value * 1000) / 1000}`
  const animation =
    pulse === 0
      ? ''
      : `<animateTransform attributeName="transform" type="scale" values="${s(scale)};${s(scale * (1 + pulse))};${s(scale * (1 - pulse * 0.6))};${s(scale * (1 + pulse * 0.7))};${s(scale)}" dur="${seconds}s" begin="${round(-(t % seconds))}s" repeatCount="indefinite"/>`
  return `<g transform="translate(${C} ${C})"><g transform="scale(${s(scale)})">${animation}<g transform="translate(${-C} ${-C})">${content}</g></g></g>`
}

/** The ring as an SVG document for `mode`, with the levels (0..1) it shows, `t` seconds into its motion. */
export function ringSvg(mode: HudMode, mic: number, out: number, t = 0): string {
  const body = mode === 'thinking' || mode === 'speaking' ? amberSphere(mode, out, t) : blueRing(mode, mic, t)
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${SIZE} ${SIZE}" width="${SIZE}" height="${SIZE}">${DEFS}${body}</svg>`
}
