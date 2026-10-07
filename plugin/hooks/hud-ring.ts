// The HUD ring for the terminal: one frame as pixels (a small "shader" per
// mode), packed into half-block cells for a Raster. Two pixels stack in each
// cell (▀ with the top as foreground and the bottom as background), so a ring
// `rows` tall is `2 * rows` columns wide and comes out round.
//
// It follows the two reference images. At rest and listening: the monitor
// photo, a glowing blue ring around "JARVIS" on a dark grid, a white arc to
// its left and a sparse ring of dots. Thinking and speaking: the orange
// sphere, a turning 3D shell of glowing fragments (bright in front, dim
// behind) around a bright core wrapped in orbits. The drawing swells and
// shrinks with the voice (hudZoom); the backdrop stays put. Every pixel is one of a few dozen colors (a ramp per
// mode, a white ramp, the backdrop), well under a Raster's 1,024 color pairs.

export type HudMode = 'offline' | 'sleeping' | 'listening' | 'thinking' | 'speaking' | 'interrupted'

export type RingInput = {
  mode: HudMode
  /** Seconds, for the motion. */
  t: number
  /** Microphone level, 0..1 (listening). */
  mic: number
  /** Jarvis's own audio level, 0..1 (speaking). */
  out: number
}

type Rgb = readonly [number, number, number]

const hex = (value: number): Rgb => [(value >> 16) & 0xff, (value >> 8) & 0xff, value & 0xff]

/** Each mode's main color, for the pane's title too. */
export const HUD_COLORS: Record<HudMode, number> = {
  offline: 0x6b757d,
  sleeping: 0x2f9bff,
  listening: 0x5cc8ff,
  thinking: 0xff9a2a,
  speaking: 0xffc04a,
  interrupted: 0xbfe6ff,
}

const TAU = Math.PI * 2
/** Steps in each ramp, from the backdrop up to its brightest color. */
const STEPS = 14
/** Below this a pixel shows the backdrop. */
const FLOOR = 0.06

type Family = 'blue' | 'amber' | 'gray'

const FAMILY: Record<HudMode, Family> = {
  offline: 'gray',
  sleeping: 'blue',
  listening: 'blue',
  interrupted: 'blue',
  thinking: 'amber',
  speaking: 'amber',
}

/** Each family's backdrop, grid lines and ramps (backdrop first, brightest last). */
type Look = { backdrop: number; line: number; disc?: number; main: number[]; white: number[]; glow: number }

function ramp(stops: readonly number[], steps: number): number[] {
  const colors: number[] = []
  for (let k = 0; k <= steps; k += 1) {
    const at = (k / steps) * (stops.length - 1)
    const i = Math.min(stops.length - 2, Math.floor(at))
    const f = at - i
    const [r0, g0, b0] = hex(stops[i] as number)
    const [r1, g1, b1] = hex(stops[i + 1] as number)
    const mix = (u: number, v: number) => Math.round(u + (v - u) * f)
    colors.push((mix(r0, r1) << 16) | (mix(g0, g1) << 8) | mix(b0, b1))
  }
  return colors
}

const LOOKS: Record<Family, Look> = {
  blue: {
    backdrop: 0x121519,
    line: 0x181c21,
    main: ramp([0x121519, 0x0d2b4d, 0x1565c0, 0x2f9bff, 0x8fd0ff, 0xeaf7ff], STEPS),
    white: ramp([0x121519, 0x3b4148, 0x9ba4ac, 0xffffff], STEPS),
    glow: 0.55,
  },
  amber: {
    backdrop: 0x0a0705,
    line: 0x0a0705,
    disc: 0x140c07,
    main: ramp([0x0a0705, 0x3d1a04, 0xb44a06, 0xff8a1a, 0xffc05a, 0xfff2d2], STEPS),
    white: ramp([0x0a0705, 0x5a3a1a, 0xffd9a0, 0xfff8ea], STEPS),
    glow: 0.8,
  },
  gray: {
    backdrop: 0x121416,
    line: 0x1b1e21,
    main: ramp([0x121416, 0x2a2e32, 0x535b62, 0x868f96], STEPS),
    white: ramp([0x121416, 0x2a2e32, 0x5f666c, 0x9aa1a7], STEPS),
    glow: 0,
  },
}

/** 1 on the band around `center`, fading out over `width` either side. */
const band = (r: number, center: number, width: number): number => {
  const d = (r - center) / width
  return Math.exp(-d * d)
}

/** 1 within `half` radians of `center`, fading to 0 just beyond. */
function within(a: number, center: number, half: number): number {
  let d = Math.abs(a - center) % TAU
  if (d > Math.PI) d = TAU - d
  if (d <= half) return 1
  return Math.max(0, 1 - (d - half) / 0.08)
}

/** A stable pseudo-random number in 0..1 for `n`. */
function hash(n: number): number {
  const s = Math.sin(n * 127.1 + 311.7) * 43758.5453
  return s - Math.floor(s)
}

/** The sector of `count` around the circle that angle `a` (turned by `turn`) falls in, and where in it. */
function sector(a: number, count: number, turn: number): { k: number; at: number } {
  let angle = (a - turn) % TAU
  if (angle < 0) angle += TAU
  const position = (angle / TAU) * count
  const k = Math.floor(position)
  return { k, at: position - k }
}

type Shade = { main: number; white: number }
const NONE: Shade = { main: 0, white: 0 }

/** The blue ring's radius (the pane's radius is 1). */
const RING = 0.6

/** The monitor photo: a blue ring, a white arc on its left, dots and ticks around. */
function blueRing(input: RingInput, r: number, a: number): Shade {
  const { mode, t, mic } = input
  const level = mode === 'listening' ? Math.min(1, mic) : 0
  const isStill = mode === 'offline'
  const breathe = isStill ? 0 : 0.06 * Math.sin(t * 0.9)

  // The ring: a solid band with a bright inner edge (the glow comes later).
  const width = 0.045 + level * 0.03
  let main = band(r, RING, width) * (mode === 'sleeping' ? 0.78 + breathe : 0.9 + level * 0.1)
  main = Math.max(main, band(r, RING - width * 0.9, 0.016) * (mode === 'offline' ? 0.7 : 1))
  if (mode === 'interrupted') main = Math.max(main, band(r, RING, 0.07))
  // A faint arc low on the right.
  main = Math.max(main, band(r, 0.73, 0.016) * within(a, Math.PI / 4 + (isStill ? 0 : t * 0.05), 0.45) * 0.4)

  // The white arc on the left, sweeping round as you speak, and its twin on the right.
  const sweep = 0.42 + level * 1.5
  const drift = isStill ? 0 : 0.25 * Math.sin(t * 0.25)
  let white = band(r, 0.78, 0.018) * within(a, Math.PI + drift, sweep) * 0.95
  if (level > 0.15) white = Math.max(white, band(r, 0.78, 0.018) * within(a, drift, (level - 0.15) * 1.6) * 0.85)

  // The outer ring of dots, a few missing, turning slowly; long ticks top and bottom.
  const dots = sector(a, 44, isStill ? 0 : t * (0.04 + level * 0.3))
  if (hash(dots.k) > 0.22 && dots.at < 0.3) white = Math.max(white, band(r, 0.93, 0.02) * (0.4 + level * 0.4))
  const cardinal = within(a, Math.PI / 2, 0.012) + within(a, -Math.PI / 2, 0.012)
  if (r > 0.84 && r < 0.98) white = Math.max(white, cardinal * 0.55)
  const short = sector(a, 12, 0)
  if (short.at < 0.04 && r > 0.88 && r < 0.95) white = Math.max(white, 0.32)
  return { main, white }
}

/** The sphere's radius, inside the debris around it. */
const SPHERE = 0.74

/** The orange sphere, flat parts: shading, debris, orbits and the core (the shell is drawn by `drawShell`). */
function amberSphere(input: RingInput, x: number, y: number, r: number, a: number): Shade {
  const { mode, t, out } = input
  const isSpeaking = mode === 'speaking'
  const level = isSpeaking ? Math.min(1, out) : 0.35
  let main = 0

  // Volume: the sphere lit from the top left, and a rim of light.
  if (r < SPHERE) {
    const nz = Math.sqrt(1 - (r / SPHERE) ** 2)
    const light = Math.max(0, (-0.45 * x - 0.55 * y) / SPHERE + 0.7 * nz)
    main = 0.06 + light * 0.1
  }
  main = Math.max(main, band(r, SPHERE, 0.025) * 0.3)

  // Debris outside the sphere: glinting pieces at random radii, some gaps, turning slowly.
  const { k, at } = sector(a, 90, t * 0.12)
  if (hash(k + 1) > 0.35) {
    const h2 = hash(k + 1.37)
    const h3 = hash(k + 1.71)
    const from = 0.82 + h2 * 0.1
    if (r > from && r < from + 0.03 + h3 * 0.06 && at < 0.3 + h3 * 0.5) {
      const glint = 0.55 + 0.45 * Math.sin(t * (2 + h2 * 3) + k)
      main = Math.max(main, (0.4 + 0.4 * h3) * (0.7 + 0.3 * glint))
    }
  }

  // Orbits: tilted rings turning round the core.
  for (let i = 0; i < 3; i += 1) {
    const angle = (i * Math.PI) / 3 + t * (isSpeaking ? 0.6 : 1.1) * (i % 2 === 0 ? 1 : -1)
    const u = x * Math.cos(angle) + y * Math.sin(angle)
    const v = -x * Math.sin(angle) + y * Math.cos(angle)
    const e = Math.hypot(u / 0.44, v / 0.13)
    main = Math.max(main, band(e, 1, 0.06) * 0.45)
  }

  // The core, and while speaking streaks out from it with the voice.
  const core = 0.1 + level * 0.08 + (isSpeaking ? 0 : 0.015 * Math.sin(t * 4))
  let white = band(r, 0, core * 0.5)
  main = Math.max(main, band(r, 0, core * 1.1) * 0.95)
  if (isSpeaking) {
    const ray = sector(a, 18, t * 0.3)
    const reach = 0.2 + level * 0.5 * (0.5 + 0.5 * hash(ray.k + Math.floor(t * 8)))
    if (ray.at < 0.18 && r > core && r < reach) main = Math.max(main, (1 - r / reach) * 0.85)
  }
  if (white < 0.25) white = 0
  return { main, white }
}

type ShellPoint = { x: number; y: number; z: number; glint: number }

/** Points on the sphere's shell in patches, like the fragments of the reference (unit sphere, y up). */
const SHELL: readonly ShellPoint[] = (() => {
  const points: ShellPoint[] = []
  const count = 400
  const golden = Math.PI * (3 - Math.sqrt(5))
  for (let i = 0; i < count; i += 1) {
    const y = 1 - (2 * (i + 0.5)) / count
    const ring = Math.sqrt(1 - y * y)
    const lon = i * golden
    // Patches: whole cells of latitude and longitude left out, as in the reference's broken shell.
    const cell = Math.floor(((lon % TAU) / TAU) * 7) * 13 + Math.floor((y + 1) * 3)
    if (hash(cell + 0.5) < 0.3 || hash(i + 0.25) < 0.2) continue
    const lift = 0.94 + hash(i + 0.75) * 0.1
    points.push({ x: Math.cos(lon) * ring * lift, y: y * lift, z: Math.sin(lon) * ring * lift, glint: hash(i + 0.9) })
  }
  return points
})()

/** The camera looks down on the sphere this far (radians), so it reads as a ball. */
const TILT = 0.38

/**
 * The shell of the orange sphere, turning about its upright axis: each piece
 * a short streak along its path, bright in front and dim behind.
 */
function drawShell(main: Float32Array, width: number, height: number, radius: number, zoom: number, t: number, isSpeaking: boolean): void {
  const cx = (width - 1) / 2
  const cy = (height - 1) / 2
  const size = SPHERE * radius * zoom
  const turn = t * (isSpeaking ? 0.35 : 0.55)
  const cosTilt = Math.cos(TILT)
  const sinTilt = Math.sin(TILT)
  const project = (p: ShellPoint, angle: number): [number, number, number] => {
    const x = p.x * Math.cos(angle) + p.z * Math.sin(angle)
    const z = -p.x * Math.sin(angle) + p.z * Math.cos(angle)
    const y = p.y * cosTilt - z * sinTilt
    const depth = p.y * sinTilt + z * cosTilt
    return [cx + x * size, cy - y * size, depth]
  }
  const plot = (px: number, py: number, value: number) => {
    const ix = Math.round(px)
    const iy = Math.round(py)
    if (ix < 0 || iy < 0 || ix >= width || iy >= height) return
    const at = iy * width + ix
    if ((main[at] as number) < value) main[at] = value
  }
  for (const p of SHELL) {
    const [x, y, depth] = project(p, turn)
    const [tailX, tailY] = project(p, turn - 0.09)
    const near = (depth + 1) / 2
    const glint = 0.75 + 0.25 * Math.sin(t * 3 + p.glint * TAU)
    const value = (0.18 + 0.72 * near * near) * glint * (0.6 + 0.4 * p.glint)
    plot(x, y, value)
    plot((x + tailX) / 2, (y + tailY) / 2, value * 0.8)
    plot(tailX, tailY, value * 0.55)
  }
}

/** How big the drawing is now: it swells and shrinks with the voice, and breathes while thinking. */
export function hudZoom(input: RingInput): number {
  switch (input.mode) {
    case 'speaking':
      return 0.84 + 0.16 * Math.min(1, input.out)
    case 'listening':
      return 0.95 + 0.06 * Math.min(1, input.mic)
    case 'thinking':
      return 0.92 + 0.03 * Math.sin(input.t * 2.6)
    default:
      return 1
  }
}

function shade(input: RingInput, x: number, y: number): Shade {
  const r = Math.hypot(x, y)
  if (r > 1.02) return NONE
  const a = Math.atan2(y, x) // 0 at the right, π/2 at the bottom
  return FAMILY[input.mode] === 'amber' ? amberSphere(input, x, y, r, a) : blueRing(input, r, a)
}

// "JARVIS" in a 3x5 pixel font ("I" one pixel wide), for inside the blue ring.
const GLYPHS: readonly (readonly string[])[] = [
  ['..#', '..#', '..#', '#.#', '.#.'],
  ['.#.', '#.#', '###', '#.#', '#.#'],
  ['##.', '#.#', '##.', '#.#', '#.#'],
  ['#.#', '#.#', '#.#', '#.#', '.#.'],
  ['#', '#', '#', '#', '#'],
  ['.##', '#..', '.#.', '..#', '##.'],
]
const WORD_WIDTH = GLYPHS.reduce((sum, glyph) => sum + (glyph[0] as string).length, 0) + GLYPHS.length - 1
const WORD_HEIGHT = 5

/** Draws the word into `white` (row-major `width` wide), centered, when it fits inside the ring. */
function drawWord(white: Float32Array, width: number, height: number, radius: number, intensity: number): void {
  // The word needs a pixel clear each side inside the ring.
  if (WORD_WIDTH + 2 > radius * (RING - 0.07) * 2 || height < WORD_HEIGHT + 4) return
  let x = Math.round((width - WORD_WIDTH) / 2)
  const y0 = Math.round((height - WORD_HEIGHT) / 2)
  for (const glyph of GLYPHS) {
    for (let row = 0; row < WORD_HEIGHT; row += 1) {
      const line = glyph[row] as string
      for (let column = 0; column < line.length; column += 1) {
        if (line[column] === '#') white[(y0 + row) * width + x + column] = intensity
      }
    }
    x += (glyph[0] as string).length + 1
  }
}

/** A blur of `values` (two box passes each way), for the glow. */
function blur(values: Float32Array, width: number, height: number, reach: number): Float32Array {
  let source = values
  for (let pass = 0; pass < 2; pass += 1) {
    const across = new Float32Array(width * height)
    for (let y = 0; y < height; y += 1) {
      for (let x = 0; x < width; x += 1) {
        let sum = 0
        let n = 0
        for (let d = -reach; d <= reach; d += 1) {
          const xx = x + d
          if (xx < 0 || xx >= width) continue
          sum += source[y * width + xx] as number
          n += 1
        }
        across[y * width + x] = sum / n
      }
    }
    const down = new Float32Array(width * height)
    for (let y = 0; y < height; y += 1) {
      for (let x = 0; x < width; x += 1) {
        let sum = 0
        let n = 0
        for (let d = -reach; d <= reach; d += 1) {
          const yy = y + d
          if (yy < 0 || yy >= height) continue
          sum += across[yy * width + x] as number
          n += 1
        }
        down[y * width + x] = sum / n
      }
    }
    source = down
  }
  return source
}

const step = (colors: number[], value: number): number => colors[Math.max(1, Math.min(STEPS, Math.round(value * STEPS)))] as number

/**
 * One frame of the ring as `width * height` pixels, row-major, each a color
 * as 0x00RRGGBB: the backdrop and its grid, the ring, its glow.
 */
export function ringPixels(input: RingInput, width: number, height: number): Int32Array {
  const look = LOOKS[FAMILY[input.mode]]
  const radius = Math.min(width, height) / 2
  const cx = (width - 1) / 2
  const cy = (height - 1) / 2
  const main = new Float32Array(width * height)
  const white = new Float32Array(width * height)
  const zoom = hudZoom(input)
  for (let py = 0; py < height; py += 1) {
    for (let px = 0; px < width; px += 1) {
      const { main: m, white: w } = shade(input, (px - cx) / radius / zoom, (py - cy) / radius / zoom)
      main[py * width + px] = m
      white[py * width + px] = w
    }
  }
  if (FAMILY[input.mode] === 'amber') drawShell(main, width, height, radius, zoom, input.t, input.mode === 'speaking')
  else drawWord(white, width, height, radius, input.mode === 'offline' ? 0.75 : 1)
  const glow = look.glow > 0 ? blur(main, width, height, Math.max(1, Math.round(radius / 12))) : undefined
  const spacing = Math.max(4, Math.round(radius / 3))
  const pixels = new Int32Array(width * height)
  for (let i = 0; i < pixels.length; i += 1) {
    const m = Math.max(main[i] as number, glow === undefined ? 0 : (glow[i] as number) * look.glow * 1.6)
    const w = white[i] as number
    if (w >= FLOOR && w >= m) pixels[i] = step(look.white, w)
    else if (m >= FLOOR) pixels[i] = step(look.main, m)
    else {
      const x = i % width
      const y = Math.floor(i / width)
      const isLine = (x - Math.round(cx)) % spacing === 0 || (y - Math.round(cy)) % spacing === 0
      const isDisc = look.disc !== undefined && Math.hypot(x - cx, y - cy) < radius * 0.98
      pixels[i] = isDisc ? (look.disc as number) : isLine ? look.line : look.backdrop
    }
  }
  return pixels
}

const UPPER_HALF = 0x2580

/** Packs pixels (two rows per cell) into a Raster's `cells`: base64 of u32 [glyph, fg, bg] triplets. */
export function packHalfBlocks(pixels: Int32Array, columns: number, rows: number): string {
  const words = new Uint32Array(columns * rows * 3)
  for (let row = 0; row < rows; row += 1) {
    for (let column = 0; column < columns; column += 1) {
      const at = (row * columns + column) * 3
      words[at] = UPPER_HALF
      words[at + 1] = (pixels[2 * row * columns + column] ?? 0) >>> 0
      words[at + 2] = (pixels[(2 * row + 1) * columns + column] ?? 0) >>> 0
    }
  }
  return toBase64(new Uint8Array(words.buffer))
}

/** One frame of the ring for a Raster `columns` wide and `rows` tall (columns = 2 * rows draws it round). */
export function ringCells(input: RingInput, columns: number, rows: number): string {
  return packHalfBlocks(ringPixels(input, columns, rows * 2), columns, rows)
}

const ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'

/** Standard padded base64 (the module has no Buffer, and btoa wants a string). */
export function toBase64(bytes: Uint8Array): string {
  let text = ''
  let i = 0
  for (; i + 2 < bytes.length; i += 3) {
    const n = ((bytes[i] as number) << 16) | ((bytes[i + 1] as number) << 8) | (bytes[i + 2] as number)
    text += ALPHABET[(n >> 18) & 63]! + ALPHABET[(n >> 12) & 63]! + ALPHABET[(n >> 6) & 63]! + ALPHABET[n & 63]!
  }
  const left = bytes.length - i
  if (left === 1) {
    const n = (bytes[i] as number) << 16
    text += `${ALPHABET[(n >> 18) & 63]!}${ALPHABET[(n >> 12) & 63]!}==`
  } else if (left === 2) {
    const n = ((bytes[i] as number) << 16) | ((bytes[i + 1] as number) << 8)
    text += `${ALPHABET[(n >> 18) & 63]!}${ALPHABET[(n >> 12) & 63]!}${ALPHABET[(n >> 6) & 63]!}=`
  }
  return text
}
