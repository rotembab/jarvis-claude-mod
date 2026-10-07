// The app's settings, in userData/settings.json (%APPDATA%\Jarvis on
// Windows): when the overlay shows, whether the orb shows and where it sits.
// Starting with Windows is not stored here: Windows itself holds that, and
// main.ts asks it. A damaged file never stops the app; each bad field falls
// back to its default on its own.

import { promises as fs } from 'node:fs'
import { join } from 'node:path'

import { writeFileAtomic } from './discovery'
import { OVERLAY_SETTINGS, type OverlaySetting } from './visibility'

/** The orb window's width and height. */
export const ORB_SIZE = 180
/** The orb's gap from the corner of the screen when it has no saved place. */
export const ORB_MARGIN = 24

export type Point = { x: number; y: number }
export type Rect = { x: number; y: number; width: number; height: number }

export type Settings = { v: 1; overlay: OverlaySetting; showOrb: boolean; orb?: Point }

export const DEFAULT_SETTINGS: Settings = { v: 1, overlay: 'auto', showOrb: true }

export function settingsPath(userData: string): string {
  return join(userData, 'settings.json')
}

const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value)

const isFiniteNumber = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value)

export async function loadSettings(userData: string): Promise<Settings> {
  let value: unknown
  try {
    value = JSON.parse(await fs.readFile(settingsPath(userData), 'utf8'))
  } catch {
    return { ...DEFAULT_SETTINGS }
  }
  if (!isObject(value)) return { ...DEFAULT_SETTINGS }
  const settings: Settings = {
    v: 1,
    overlay: OVERLAY_SETTINGS.find(setting => setting === value.overlay) ?? DEFAULT_SETTINGS.overlay,
    showOrb: typeof value.showOrb === 'boolean' ? value.showOrb : DEFAULT_SETTINGS.showOrb,
  }
  const orb = value.orb
  if (isObject(orb) && isFiniteNumber(orb.x) && isFiniteNumber(orb.y)) settings.orb = { x: Math.round(orb.x), y: Math.round(orb.y) }
  return settings
}

export async function saveSettings(userData: string, settings: Settings): Promise<void> {
  await fs.mkdir(userData, { recursive: true })
  await writeFileAtomic(settingsPath(userData), JSON.stringify(settings, null, 2) + '\n')
}

const clamp = (value: number, low: number, high: number): number => Math.min(Math.max(value, low), Math.max(low, high))

/**
 * Where the orb goes: its saved place, pulled inside the screen whose area
 * holds its centre; the bottom right corner of the main screen when it has no
 * place or that screen is gone (a monitor unplugged since).
 */
export function placeOrb(saved: Point | undefined, areas: readonly Rect[], primary: Rect): Point {
  const fallback = { x: primary.x + primary.width - ORB_SIZE - ORB_MARGIN, y: primary.y + primary.height - ORB_SIZE - ORB_MARGIN }
  if (saved === undefined) return { x: Math.round(fallback.x), y: Math.round(fallback.y) }
  const cx = saved.x + ORB_SIZE / 2
  const cy = saved.y + ORB_SIZE / 2
  const area = areas.find(a => cx >= a.x && cx < a.x + a.width && cy >= a.y && cy < a.y + a.height)
  if (area === undefined) return { x: Math.round(fallback.x), y: Math.round(fallback.y) }
  return {
    x: Math.round(clamp(saved.x, area.x, area.x + area.width - ORB_SIZE)),
    y: Math.round(clamp(saved.y, area.y, area.y + area.height - ORB_SIZE)),
  }
}
