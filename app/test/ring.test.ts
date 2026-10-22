import assert from 'node:assert/strict'
import { test } from 'node:test'

import { ringSvg } from '../../plugin/hooks/hud-svg'
import { LEVEL_REDRAW_MS, shouldRedraw } from '../src/renderer/ring'
import { HUD_MODES } from '../src/shared/snapshot'

test('the ring is redrawn for a mode change at once, for a level change at most every 150 ms', () => {
  const drawn = { mode: 'listening' as const, micTenth: 5, outTenth: 0, at: 1000 }
  assert.equal(shouldRedraw(undefined, 'listening', 0.5, 0, 1000), true)
  assert.equal(shouldRedraw(drawn, 'thinking', 0.5, 0, 1001), true)
  assert.equal(shouldRedraw(drawn, 'listening', 0.52, 0, 5000), false)
  assert.equal(shouldRedraw(drawn, 'listening', 0.8, 0, 1000 + LEVEL_REDRAW_MS - 1), false)
  assert.equal(shouldRedraw(drawn, 'listening', 0.8, 0, 1000 + LEVEL_REDRAW_MS), true)
})

test('the ring SVG holds nothing the pages policy would block', () => {
  // The pages allow no inline style or script and load nothing; SMIL animation is fine.
  for (const mode of HUD_MODES) {
    const svg = ringSvg(mode, 0.5, 0.5, 1, { frame: false })
    assert.doesNotMatch(svg, /<script|<style|<foreignObject|style=|href|\son[a-z]+=/i, mode)
    assert.match(svg, /^<svg /)
  }
})
