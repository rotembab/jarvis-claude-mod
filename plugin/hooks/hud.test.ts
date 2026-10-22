import { describe, expect, test } from 'claude-code/testing'
import type { RenderPropsOf } from 'claude-code'

import { completeTurn, jarvis, startHelper, startSession, world } from './test-harness'
import { actionLabel, FRAME_MS, HUD_PANE, hudLayout, hudMode, paneSize, RING_KEY, ringSize } from './hud'
import type { HudMode } from './hud'
import { hudZoom, ringCells, ringPixels, toBase64 } from './hud-ring'
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

/** How bright a frame is in all: the sum of its channels. */
const brightness = (mode: HudMode, mic = 0, out = 0, t = 1) =>
  ringPixels({ mode, t, mic, out }, 40, 40).reduce((sum, pixel) => sum + ((pixel >> 16) & 255) + ((pixel >> 8) & 255) + (pixel & 255), 0)
const MODES: HudMode[] = ['offline', 'sleeping', 'listening', 'thinking', 'speaking', 'interrupted']

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

  test('a frame is a Raster of half blocks in well under 1,024 color pairs', () => {
    for (const mode of MODES) {
      const triplets = decodeCells(ringCells({ mode, t: 1.7, mic: 0.6, out: 0.6 }, 48, 24))
      expect(triplets).toHaveLength(48 * 24)
      const pairs = new Set<string>()
      for (const [glyph, fg, bg] of triplets) {
        expect(glyph).toBe(0x2580)
        expect(fg).toBeLessThanOrEqual(0xffffff)
        expect(bg).toBeLessThanOrEqual(0xffffff)
        pairs.add(`${fg}:${bg}`)
      }
      expect(pairs.size).toBeLessThan(1024)
    }
    // The biggest ring too.
    for (const mode of ['sleeping', 'speaking'] as HudMode[]) {
      const pairs = new Set(decodeCells(ringCells({ mode, t: 1.7, mic: 0.6, out: 0.6 }, 128, 64)).map(([, fg, bg]) => `${fg}:${bg}`))
      expect(pairs.size).toBeLessThan(1024)
    }
  })

  test('JARVIS sits inside the blue ring when there is room for it', () => {
    const middle = (columns: number) => {
      const pixels = ringPixels({ mode: 'sleeping', t: 0, mic: 0, out: 0 }, columns, columns)
      const row = Math.floor(columns / 2) - 1
      return Array.from(pixels.slice(row * columns, (row + 1) * columns))
    }
    expect(middle(48).filter(pixel => pixel === 0xffffff).length).toBeGreaterThan(5)
    expect(middle(24)).not.toContain(0xffffff)
  })

  test('the white arc sweeps with your voice and the core flares with Jarvis speaking', () => {
    expect(brightness('listening', 0.9)).toBeGreaterThan(brightness('listening', 0.05))
    expect(brightness('speaking', 0, 0.9)).toBeGreaterThan(brightness('speaking', 0, 0.05))
  })

  test('the HUD swells and shrinks with the voice', () => {
    const zoom = (mode: HudMode, level: number) => hudZoom({ mode, t: 0, mic: level, out: level })
    expect(zoom('speaking', 0.9)).toBeGreaterThan(zoom('speaking', 0.1) + 0.1)
    expect(zoom('speaking', 1)).toBeLessThanOrEqual(1)
    expect(zoom('listening', 0.9)).toBeGreaterThan(zoom('listening', 0.1))
    expect(zoom('sleeping', 0.9)).toBe(1)
    const scale = (svg: string) => Number(/scale\(([\d.]+)\)/.exec(svg)?.[1])
    expect(scale(ringSvg('speaking', 0, 0.9, 1))).toBeGreaterThan(scale(ringSvg('speaking', 0, 0.1, 1)))
  })

  test('the ring moves at rest and stands still when Jarvis is off', () => {
    const at = (mode: HudMode, t: number) => ringCells({ mode, t, mic: 0, out: 0 }, 32, 16)
    // Compared as booleans: a failure would print two frames of base64.
    expect(at('sleeping', 0) === at('sleeping', 3)).toBe(false)
    expect(at('thinking', 0) === at('thinking', 0.5)).toBe(false)
    expect(at('offline', 0) === at('offline', 3)).toBe(true)
  })

  test('blue at rest, amber while thinking, gray when off', () => {
    const tint = (mode: HudMode) => {
      let [red, green, blue] = [0, 0, 0]
      for (const pixel of ringPixels({ mode, t: 0, mic: 0, out: 0 }, 32, 32)) {
        red += (pixel >> 16) & 255
        green += (pixel >> 8) & 255
        blue += pixel & 255
      }
      return { red, green, blue }
    }
    const resting = tint('sleeping')
    expect(resting.blue).toBeGreaterThan(resting.red * 1.5)
    const thinking = tint('thinking')
    expect(thinking.red).toBeGreaterThan(thinking.blue * 3)
    for (const pixel of ringPixels({ mode: 'offline', t: 0, mic: 0, out: 0 }, 32, 32)) {
      const channels = [(pixel >> 16) & 255, (pixel >> 8) & 255, pixel & 255]
      expect(Math.max(...channels) - Math.min(...channels)).toBeLessThan(0x20)
    }
  })

  test('base64 matches the standard alphabet and padding', () => {
    const bytes = (text: string) => new Uint8Array([...text].map(c => c.charCodeAt(0)))
    expect(toBase64(bytes('Man'))).toBe('TWFu')
    expect(toBase64(bytes('Ma'))).toBe('TWE=')
    expect(toBase64(bytes('M'))).toBe('TQ==')
    expect(toBase64(new Uint8Array([255, 254, 253, 0]))).toBe('//79AA==')
  })

  test('the ring fills the pane, round, between 6 and 64 rows', () => {
    expect(ringSize(52, 34, 9)).toEqual({ columns: 50, rows: 25 })
    expect(ringSize(52, 30, 9)).toEqual({ columns: 42, rows: 21 })
    expect(ringSize(30, 10, 9)).toEqual({ columns: 12, rows: 6 })
    expect(ringSize(300, 100, 9)).toEqual({ columns: 128, rows: 64 })
  })

  test('the pane asks for the room the ring can use', () => {
    // Docked in a 160 x 45 screen: as wide as the ring its height allows.
    const dock = { placement: 'dock', bodyColumns: 53, bodyRows: 38, columns: 106, rows: 45 } as const
    expect(paneSize(dock, 9, false)).toEqual({ rows: 24, columns: 60 })
    // Never more than half the screen beside the conversation.
    expect(paneSize({ ...dock, bodyRows: 100, columns: 66 }, 9, false)).toEqual({ rows: 24, columns: 60 })
    // Focus mode: nearly all of it.
    expect(paneSize(dock, 12, true)).toEqual({ rows: 24, columns: 156 })
    // Above the prompt: half the screen's height, nearly all of it in focus mode.
    const inline = { placement: 'inline', bodyColumns: 100, bodyRows: 22, columns: 100, rows: 60 } as const
    expect(paneSize(inline, 9, false)).toEqual({ rows: 30, columns: 52 })
    expect(paneSize(inline, 12, true)).toEqual({ rows: 52, columns: 52 })
  })

  test('the texts go beside the ring when that lets it grow', () => {
    expect(hudLayout(52, 34, false, false)).toMatchObject({ ring: { columns: 50, rows: 25 }, isSide: false, replyRows: 0, actionRows: 8 })
    expect(hudLayout(135, 38, true, true)).toEqual({ ring: { columns: 74, rows: 37 }, isSide: true, textColumns: 59, replyRows: 8, actionRows: 27 })
  })

  test('the desktop ring is an SVG for every mode', () => {
    for (const mode of MODES) {
      const svg = ringSvg(mode, 0.5, 0.5, 1234.5)
      expect(svg.startsWith('<svg')).toBe(true)
      expect(svg.length).toBeLessThan(131_072)
      expect(svg).not.toContain('NaN')
    }
  })

  test('the desktop app can have the ring without its square tile', () => {
    const tile = `<rect width="240" height="240"`
    for (const mode of MODES) {
      expect(ringSvg(mode, 0.5, 0.5, 1)).toContain(tile)
      const round = ringSvg(mode, 0.5, 0.5, 1, { frame: false })
      expect(round).not.toContain(tile)
      expect(round).not.toContain('url(#grid)')
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
    expect(ring?.props).toMatchObject({ columns: 50, rows: 25 })
    // It animates by repainting the Raster in place.
    await w.clock.advance(FRAME_MS * 8)
    await w.settle()
    expect(w.blits.length).toBeGreaterThan(0)
    expect(w.blits.at(-1)).toMatchObject({ requestId: HUD_PANE, key: RING_KEY, columns: 50, rows: 25 })
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
