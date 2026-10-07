import { describe, expect, test } from 'claude-code/testing'
import type { RenderPropsOf } from 'claude-code'

import { completeTurn, jarvis, startHelper, startSession, world } from './test-harness'
import { actionLabel, FRAME_MS, HUD_PANE, hudMode, RING_KEY, ringSize } from './hud'
import type { HudMode } from './hud'
import { HUD_COLORS, ringCells, ringPixels, toBase64 } from './hud-ring'
import { ringSvg } from './hud-svg'

const PANE: RenderPropsOf['Pane'] = {
  title: 'JARVIS',
  isFocused: false,
  bodyColumns: 52,
  placement: 'dock',
  scroll: { offset: 0, bodyRows: 34 },
  view: {},
}

/** A Raster's cells back as [glyph, fg, bg] triplets. */
function decodeCells(cells: string): [number, number, number][] {
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'
  const clean = cells.replace(/=+$/, '')
  const bytes: number[] = []
  for (let i = 0; i < clean.length; i += 4) {
    const n = [0, 1, 2, 3].reduce((acc, k) => (acc << 6) | Math.max(0, alphabet.indexOf(clean[i + k] ?? 'A')), 0)
    bytes.push((n >> 16) & 255, (n >> 8) & 255, n & 255)
  }
  const triplets: [number, number, number][] = []
  const word = (at: number) => ((bytes[at + 3] ?? 0) << 24) | ((bytes[at + 2] ?? 0) << 16) | ((bytes[at + 1] ?? 0) << 8) | (bytes[at] ?? 0)
  for (let at = 0; at + 12 <= Math.floor((clean.length * 3) / 4); at += 12) triplets.push([word(at) >>> 0, word(at + 4) >>> 0, word(at + 8) >>> 0])
  return triplets
}

const lit = (mode: HudMode, mic = 0, out = 0, t = 1) => ringPixels({ mode, t, mic, out }, 40, 40).filter(pixel => pixel >= 0).length

describe('HUD ring', () => {
  test('the mode follows the helper and the turn', () => {
    expect(hudMode('sleeping', false)).toBe('sleeping')
    expect(hudMode('listening', false)).toBe('listening')
    expect(hudMode('awake', false)).toBe('listening')
    expect(hudMode('transcribing', false)).toBe('thinking')
    expect(hudMode('sleeping', true)).toBe('thinking')
    expect(hudMode('speaking', true)).toBe('speaking')
    expect(hudMode('listening', true)).toBe('listening') // the user talking over a running turn
    expect(hudMode('sleeping', false, true)).toBe('interrupted')
    expect(hudMode('stopped', true)).toBe('offline')
    expect(hudMode('not_installed', false)).toBe('offline')
  })

  test('a frame is a Raster of half blocks over the terminal background', () => {
    const triplets = decodeCells(ringCells({ mode: 'listening', t: 1, mic: 0.5, out: 0 }, 40, 20))
    expect(triplets).toHaveLength(40 * 20)
    const glyphs = new Set(triplets.map(([glyph]) => glyph))
    for (const glyph of glyphs) expect([0x20, 0x2580, 0x2584]).toContain(glyph)
    expect(glyphs.has(0x2580)).toBe(true)
    // Empty cells, and the corners, are the terminal's own colors.
    expect(triplets[0]).toEqual([0x20, 0x01000000, 0x01000000])
    for (const [, fg, bg] of triplets) {
      expect(fg === 0x01000000 || fg <= 0xffffff).toBe(true)
      expect(bg === 0x01000000 || bg <= 0xffffff).toBe(true)
    }
  })

  test('the inner arc swells with the voice and the bursts with Jarvis speaking', () => {
    expect(lit('listening', 0.9)).toBeGreaterThan(lit('listening', 0.05))
    expect(lit('speaking', 0, 0.9)).toBeGreaterThan(lit('speaking', 0, 0.05))
  })

  test('the ring moves at rest and stands still when Jarvis is off', () => {
    const at = (mode: HudMode, t: number) => ringCells({ mode, t, mic: 0, out: 0 }, 32, 16)
    expect(at('sleeping', 0)).not.toBe(at('sleeping', 3))
    expect(at('thinking', 0)).not.toBe(at('thinking', 0.5))
    expect(at('offline', 0)).toBe(at('offline', 3))
  })

  test('each mode draws in its own color', () => {
    const [r0, g0, b0] = [(HUD_COLORS.offline >> 16) & 255, (HUD_COLORS.offline >> 8) & 255, HUD_COLORS.offline & 255]
    for (const pixel of ringPixels({ mode: 'offline', t: 0, mic: 0, out: 0 }, 32, 32)) {
      if (pixel < 0) continue
      // A dimmed gray of the offline color: every channel scaled alike.
      const scale = ((pixel >> 16) & 255) / r0
      expect(Math.abs(((pixel >> 8) & 255) - g0 * scale)).toBeLessThan(2)
      expect(Math.abs((pixel & 255) - b0 * scale)).toBeLessThan(2)
    }
  })

  test('base64 matches the standard alphabet and padding', () => {
    const bytes = (text: string) => new Uint8Array([...text].map(c => c.charCodeAt(0)))
    expect(toBase64(bytes('Man'))).toBe('TWFu')
    expect(toBase64(bytes('Ma'))).toBe('TWE=')
    expect(toBase64(bytes('M'))).toBe('TQ==')
    expect(toBase64(new Uint8Array([255, 254, 253, 0]))).toBe('//79AA==')
  })

  test('the ring fits the pane, round, between 6 and 24 rows', () => {
    expect(ringSize(52, 34, 9)).toEqual({ columns: 48, rows: 24 })
    expect(ringSize(52, 30, 9)).toEqual({ columns: 42, rows: 21 })
    expect(ringSize(30, 10, 9)).toEqual({ columns: 12, rows: 6 })
  })

  test('the desktop ring is an SVG for every mode', () => {
    for (const mode of ['offline', 'sleeping', 'listening', 'thinking', 'speaking', 'interrupted'] as HudMode[]) {
      const svg = ringSvg(mode, 0.5, 0.5)
      expect(svg.startsWith('<svg')).toBe(true)
      expect(svg.length).toBeLessThan(131_072)
      expect(svg).not.toContain('NaN')
    }
  })

  test('a tool call becomes one line of the log', () => {
    expect(actionLabel({ tool: 'Bash', command: 'npm test', description: 'Run the tests' })).toBe('Bash Run the tests')
    expect(actionLabel({ tool: 'Read', file_path: 'C:\\work\\src\\app.ts' })).toBe('Read app.ts')
    expect(actionLabel({ tool: 'Grep', pattern: 'TODO' })).toBe('Grep TODO')
    expect(actionLabel({ tool: 'TodoWrite' })).toBe('TodoWrite')
    expect(actionLabel({ tool: 'Bash', command: `echo ${'x'.repeat(100)}` })).toHaveLength(60)
  })
})

describe('HUD pane', () => {
  test('opens with the session and draws the ring for the helper state', async ($, on) => {
    const w = world(on)
    await startHelper($, w)
    expect(w.opens.map(open => open.id)).toContain(HUD_PANE)
    const ui = await $.ui.mount({ plugin: 'jarvis', surface: 'terminal', component: 'Pane', requestId: HUD_PANE, props: PANE })
    expect(await ui.find({ type: 'Text', text: /JARVIS · STANDING BY/ })).toBeDefined()
    const ring = await ui.find({ type: 'Raster', key: RING_KEY })
    expect(ring?.props).toMatchObject({ columns: 48, rows: 24 })
    // It animates by repainting the Raster in place.
    await w.clock.advance(FRAME_MS * 8)
    await w.settle()
    expect(w.blits.length).toBeGreaterThan(0)
    expect(w.blits.at(-1)).toMatchObject({ requestId: HUD_PANE, key: RING_KEY, columns: 48, rows: 24 })
  })

  test('says what Jarvis is doing, what you said, and what Claude ran', async ($, on) => {
    const w = world(on)
    const helper = await startHelper($, w)
    const ui = await $.ui.mount({ plugin: 'jarvis', surface: 'terminal', component: 'Pane', requestId: HUD_PANE, props: PANE })
    // The world answers $.state beneath the plugin, so the kit does not see
    // the writes that redraw a reader in a session: each step redraws.
    const shown = async (text: string | RegExp) => {
      await w.settle()
      await ui.redraw()
      return ui.find({ type: 'Text', text })
    }
    helper.event({ type: 'state', state: 'listening' })
    expect(await shown(/JARVIS · LISTENING/)).toBeDefined()
    helper.event({ type: 'utterance', id: 'u1', text: 'Run the tests', source: 'wake', durationMs: 900, language: 'en' })
    helper.event({ type: 'state', state: 'sleeping' })
    await $.turn.start({ text: 'Run the tests', turnId: 't1' })
    expect(await shown(/JARVIS · THINKING/)).toBeDefined()
    expect(await shown(/Run the tests/)).toBeDefined()
    await $.tool.call({ tool: 'Bash', command: 'npm test', description: 'Run the unit tests' })
    expect(await shown('✓ Bash Run the unit tests')).toBeDefined()
    await completeTurn($, 't1')
    expect(await shown(/JARVIS · STANDING BY/)).toBeDefined()
  })

  test('stops repainting once the pane is gone', async ($, on) => {
    const w = world(on)
    await startHelper($, w)
    await $.ui.mount({ plugin: 'jarvis', surface: 'terminal', component: 'Pane', requestId: HUD_PANE, props: PANE })
    w.blitDeny = 'not mounted'
    await w.clock.advance(FRAME_MS * 8)
    await w.settle()
    const count = w.blits.length
    await w.clock.advance(FRAME_MS * 20)
    await w.settle()
    expect(w.blits.length).toBe(count)
  })

  test('draws an SVG ring on the desktop', async ($, on) => {
    const w = world(on)
    await startHelper($, w)
    const ui = await $.ui.mount({ plugin: 'jarvis', surface: 'desktop', component: 'Pane', requestId: HUD_PANE, props: PANE })
    const svg = await ui.find({ type: 'Svg' })
    expect(String(svg?.props.source)).toContain('<svg')
    expect(await ui.find({ text: /JARVIS · STANDING BY/ })).toBeDefined()
  })

  test('/jarvis hud off closes it and keeps it closed in new sessions; on brings it back', async ($, on) => {
    const w = world(on)
    await startSession($, w)
    expect(await jarvis($, 'hud off')).toContain('HUD closed')
    expect(w.closes).toContain(HUD_PANE)
    expect(w.store.get('hud')).toBe(false)
    const before = w.opens.length
    await $.session.start({ cwd: 'C:\\work', surface: 'terminal', isInteractive: true })
    await w.settle()
    expect(w.opens.length).toBe(before)
    expect(await jarvis($, 'hud on')).toContain('opens with each session')
    expect(w.opens.length).toBe(before + 1)
    expect(await jarvis($, 'hud')).toBe('HUD open.')
  })
})
