// The HUD ring for the terminal: one frame as pixels (a small "shader" per
// mode), packed into half-block cells for a Raster. Two pixels stack in each
// cell (▀ with the top as foreground and the bottom as background), so a ring
// `rows` tall is `2 * rows` columns wide and comes out round. Pixels below a
// glow threshold stay the terminal's own background.

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

/** The colors of the reference images: cyan at rest, amber thinking, gold speaking. */
export const HUD_COLORS: Record<HudMode, number> = {
  offline: 0x4a5a62,
  sleeping: 0x1ba8cc,
  listening: 0x19d3ff,
  thinking: 0xffa21f,
  speaking: 0xffd24a,
  interrupted: 0xa8f4ff,
}
const ORANGE = hex(0xff7a1a)
const WHITE_HOT = hex(0xfff3c4)

/** Intensity steps: few distinct colors, so the terminal's palette holds them all. */
const STEPS = 12
/** Below this a pixel is left to the terminal's background. */
const FLOOR = 0.07

const TAU = Math.PI * 2

/** 1 on the band around `center`, fading out over `width` either side. */
const band = (r: number, center: number, width: number): number => {
  const d = (r - center) / width
  return Math.exp(-d * d)
}

/** How close angle `a` is to `center`, in radians, as 1 inside `half` and 0 beyond. */
function within(a: number, center: number, half: number): number {
  let d = Math.abs(a - center) % TAU
  if (d > Math.PI) d = TAU - d
  if (d <= half) return 1
  return Math.max(0, 1 - (d - half) / 0.06)
}

/** Tick marks around the rim: `count` of them, rotated by `turn` radians. */
function ticks(a: number, r: number, count: number, turn: number, inner: number, outer: number): number {
  if (r < inner - 0.02 || r > outer + 0.02) return 0
  const step = TAU / count
  let phase = (a - turn) % step
  if (phase < 0) phase += step
  const onTick = phase < step * 0.32 ? 1 : 0
  const radial = r >= inner && r <= outer ? 1 : 0.4
  return onTick * radial
}

/** One pixel's color as [r, g, b] and intensity, by mode. */
function shade(input: RingInput, x: number, y: number): readonly [Rgb, number] {
  const { mode, t, mic, out } = input
  const r = Math.hypot(x, y)
  const a = Math.atan2(y, x) + Math.PI // 0..2π, 0 at the left
  const base = hex(HUD_COLORS[mode])
  if (r > 1.02) return [base, 0]

  switch (mode) {
    case 'offline': {
      return [base, band(r, 0.74, 0.04) * 0.8]
    }
    case 'sleeping': {
      const rim = ticks(a, r, 48, t * 0.15, 0.88, 0.96) * 0.45
      const ring = band(r, 0.74, 0.035) * 0.75
      const core = band(r, 0, 0.12) * (0.25 + 0.1 * Math.sin(t * 1.5))
      return [base, Math.max(rim, ring, core)]
    }
    case 'listening': {
      const level = Math.min(1, mic)
      const rim = ticks(a, r, 48, t * 0.4, 0.88, 0.96) * 0.7
      const ring = band(r, 0.74, 0.04 + level * 0.02)
      // The inner arc swells from the top with the voice.
      const span = 0.25 + level * Math.PI * 0.9
      const arc = band(r, 0.56, 0.035 + level * 0.04) * within(a, Math.PI / 2, span)
      const core = band(r, 0, 0.14 + level * 0.1) * (0.5 + level * 0.5)
      return [base, Math.max(rim, ring, arc * 0.95, core)]
    }
    case 'thinking': {
      // Outer arcs turn one way, the rim the other; fragments orbit the core.
      let arcs = 0
      for (let k = 0; k < 3; k += 1) arcs = Math.max(arcs, within(a, t * 1.3 + (k * TAU) / 3, 0.5))
      const outer = band(r, 0.92, 0.035) * arcs
      const rim = ticks(a, r, 36, -t * 0.8, 0.82, 0.86) * 0.5
      const ring = band(r, 0.72, 0.035) * 0.85
      let fragments = 0
      for (let k = 0; k < 6; k += 1) {
        const angle = -t * 2.2 + (k * TAU) / 6
        const fx = Math.cos(angle) * 0.48
        const fy = Math.sin(angle) * 0.48
        fragments = Math.max(fragments, Math.exp(-(((x + fx) ** 2 + (y + fy) ** 2) / 0.006)))
      }
      const core = band(r, 0, 0.13) * (0.55 + 0.25 * Math.sin(t * 4))
      return [base, Math.max(outer, rim, ring, fragments, core)]
    }
    case 'speaking': {
      const level = Math.min(1, out)
      // Radial bursts whose length follows the words, and a pulsing core.
      const rays = 16
      const step = TAU / rays
      const k = Math.round((a - step / 2) / step)
      const rayAngle = k * step + step / 2
      const length = 0.3 + level * 0.55 * (0.55 + 0.45 * Math.sin(t * 7 + k * 1.7))
      const onRay = within(a, rayAngle, 0.05) * (r > 0.24 && r < 0.24 + length ? 1 - (r - 0.24) / (length + 0.05) : 0)
      const ring = band(r, 0.78, 0.035) * (0.7 + level * 0.3)
      const coreSize = 0.16 + level * 0.12
      const core = band(r, 0, coreSize)
      if (core > 0.75) return [WHITE_HOT, core]
      if (onRay > ring && onRay > core) return [ORANGE, onRay]
      return [base, Math.max(ring, core)]
    }
    case 'interrupted': {
      return [base, Math.max(band(r, 0.74, 0.06), ticks(a, r, 48, 0, 0.88, 0.96) * 0.8)]
    }
  }
}

/** A color at a quantised intensity, as 0x00RRGGBB. */
function scale([red, green, blue]: Rgb, intensity: number): number {
  const level = Math.round(Math.min(1, intensity) * STEPS) / STEPS
  const c = (v: number) => Math.round(v * level) & 0xff
  return (c(red) << 16) | (c(green) << 8) | c(blue)
}

/**
 * One frame of the ring as `width * height` pixels, row-major: a color per
 * pixel, or -1 where the terminal's background shows.
 */
export function ringPixels(input: RingInput, width: number, height: number): Int32Array {
  const pixels = new Int32Array(width * height)
  const radius = Math.min(width, height) / 2
  const cx = (width - 1) / 2
  const cy = (height - 1) / 2
  for (let py = 0; py < height; py += 1) {
    for (let px = 0; px < width; px += 1) {
      const [color, intensity] = shade(input, (px - cx) / radius, (py - cy) / radius)
      pixels[py * width + px] = intensity < FLOOR ? -1 : scale(color, intensity)
    }
  }
  return pixels
}

const DEFAULT_COLOR = 0x01000000
const UPPER_HALF = 0x2580
const LOWER_HALF = 0x2584
const SPACE = 0x20

/** Packs pixels (two rows per cell) into a Raster's `cells`: base64 of u32 [glyph, fg, bg] triplets. */
export function packHalfBlocks(pixels: Int32Array, columns: number, rows: number): string {
  const words = new Uint32Array(columns * rows * 3)
  for (let row = 0; row < rows; row += 1) {
    for (let column = 0; column < columns; column += 1) {
      const top = pixels[2 * row * columns + column] ?? -1
      const bottom = pixels[(2 * row + 1) * columns + column] ?? -1
      const at = (row * columns + column) * 3
      if (top < 0 && bottom < 0) {
        words[at] = SPACE
        words[at + 1] = DEFAULT_COLOR
        words[at + 2] = DEFAULT_COLOR
      } else if (bottom < 0) {
        words[at] = UPPER_HALF
        words[at + 1] = top
        words[at + 2] = DEFAULT_COLOR
      } else if (top < 0) {
        words[at] = LOWER_HALF
        words[at + 1] = bottom
        words[at + 2] = DEFAULT_COLOR
      } else {
        words[at] = UPPER_HALF
        words[at + 1] = top
        words[at + 2] = bottom
      }
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
