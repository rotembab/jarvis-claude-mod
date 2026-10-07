import assert from 'node:assert/strict'
import { mkdtempSync, readdirSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'

import { DEFAULT_SETTINGS, loadSettings, placeOrb, saveSettings, settingsPath } from '../src/main/settings'

const tempDir = (): string => mkdtempSync(join(tmpdir(), 'jarvis-settings-'))

test('no file, or a damaged one, gives the defaults', async () => {
  const dir = tempDir()
  assert.deepEqual(await loadSettings(dir), DEFAULT_SETTINGS)
  writeFileSync(settingsPath(dir), '{not json')
  assert.deepEqual(await loadSettings(dir), DEFAULT_SETTINGS)
  writeFileSync(settingsPath(dir), '[1, 2]')
  assert.deepEqual(await loadSettings(dir), DEFAULT_SETTINGS)
})

test('each bad field falls back on its own', async () => {
  const dir = tempDir()
  writeFileSync(settingsPath(dir), '{"overlay":"loud","showOrb":false}')
  assert.deepEqual(await loadSettings(dir), { v: 1, overlay: 'auto', showOrb: false })
  writeFileSync(settingsPath(dir), '{"overlay":"off","showOrb":"yes","orb":{"x":"a","y":4}}')
  assert.deepEqual(await loadSettings(dir), { v: 1, overlay: 'off', showOrb: true })
  writeFileSync(settingsPath(dir), '{"orb":{"x":10.6,"y":-4.2}}')
  assert.deepEqual((await loadSettings(dir)).orb, { x: 11, y: -4 })
})

test('saved settings load back, with no temporary file left', async () => {
  const dir = join(tempDir(), 'Jarvis')
  const settings = { v: 1 as const, overlay: 'always' as const, showOrb: false, orb: { x: 100, y: 120 } }
  await saveSettings(dir, settings)
  assert.deepEqual(await loadSettings(dir), settings)
  assert.deepEqual(readdirSync(dir), ['settings.json'])
})

test('placeOrb keeps the orb on a screen', () => {
  const primary = { x: 0, y: 0, width: 1920, height: 1040 }
  const second = { x: 1920, y: 0, width: 1280, height: 1024 }
  const areas = [primary, second]
  assert.deepEqual(placeOrb(undefined, areas, primary), { x: 1716, y: 836 })
  assert.deepEqual(placeOrb({ x: 100, y: 120 }, areas, primary), { x: 100, y: 120 })
  assert.deepEqual(placeOrb({ x: 1800, y: 500 }, areas, primary), { x: 1740, y: 500 })
  assert.deepEqual(placeOrb({ x: -5000, y: -5000 }, areas, primary), { x: 1716, y: 836 })
  assert.deepEqual(placeOrb({ x: 2500, y: 300 }, areas, primary), { x: 2500, y: 300 })
  // Its screen unplugged since: back to the main screen's corner.
  assert.deepEqual(placeOrb({ x: 2500, y: 300 }, [primary], primary), { x: 1716, y: 836 })
})
