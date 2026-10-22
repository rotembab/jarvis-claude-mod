import assert from 'node:assert/strict'
import { test } from 'node:test'

import { LINGER_MS, OverlayVisibility, type Shown } from '../src/main/visibility'

const standingBy: Shown = { mode: 'sleeping', phase: 'sleeping' }
const listening: Shown = { mode: 'listening', phase: 'listening' }
const thinking: Shown = { mode: 'thinking', phase: 'sleeping' }
const speaking: Shown = { mode: 'speaking', phase: 'speaking' }
const offline: Shown = { mode: 'offline', phase: 'stopped' }

test('auto stays hidden while standing by and for a typed prompt', () => {
  const v = new OverlayVisibility('auto')
  assert.equal(v.isVisible, false)
  v.update(standingBy, 0)
  assert.equal(v.isVisible, false)
  // A typed prompt: Claude thinks, but Jarvis was never woken.
  v.update(thinking, 100)
  assert.equal(v.isVisible, false)
})

test('a spoken turn shows it from the wake through the reply', () => {
  const v = new OverlayVisibility('auto')
  for (const [shown, now] of [[listening, 0], [thinking, 1000], [speaking, 3000]] as const) {
    v.update(shown, now)
    assert.equal(v.isVisible, true, shown?.mode)
  }
})

test('it lingers a few seconds after Jarvis goes back to sleep', () => {
  const v = new OverlayVisibility('auto')
  v.update(listening, 9000)
  v.update(standingBy, 10_000)
  v.update(standingBy, 10_000 + LINGER_MS - 1)
  assert.equal(v.isVisible, true)
  v.update(standingBy, 10_000 + LINGER_MS)
  assert.equal(v.isVisible, false)
})

test('a wake during the linger keeps it shown', () => {
  const v = new OverlayVisibility('auto')
  v.update(listening, 0)
  v.update(standingBy, 1000)
  v.update(listening, 3000)
  v.update(standingBy, 6000)
  assert.equal(v.isVisible, true)
  v.update(standingBy, 6000 + LINGER_MS)
  assert.equal(v.isVisible, false)
})

test('offline or no session hides it at once', () => {
  for (const shown of [offline, undefined]) {
    const v = new OverlayVisibility('auto')
    v.update(listening, 0)
    v.update(shown, 100)
    assert.equal(v.isVisible, false)
  }
})

test('always shows it with no session; off never does', () => {
  const always = new OverlayVisibility('always')
  assert.equal(always.isVisible, true)
  always.update(undefined, 0)
  assert.equal(always.isVisible, true)
  const off = new OverlayVisibility('off')
  off.update(listening, 0)
  assert.equal(off.isVisible, false)
})

test('a toggle while idle holds until the next turn ends', () => {
  const v = new OverlayVisibility('auto')
  v.update(standingBy, 0)
  v.toggle()
  assert.equal(v.isVisible, true)
  v.update(standingBy, 10_000)
  v.update(standingBy, 20_000)
  assert.equal(v.isVisible, true)
  v.update(listening, 30_000)
  assert.equal(v.isVisible, true)
  v.update(standingBy, 31_000)
  v.update(standingBy, 31_000 + LINGER_MS)
  assert.equal(v.isVisible, false)
})

test('a toggle while listening hides it until the next wake', () => {
  const v = new OverlayVisibility('auto')
  v.update(listening, 0)
  v.toggle()
  assert.equal(v.isVisible, false)
  v.update(listening, 500)
  assert.equal(v.isVisible, false)
  v.update(standingBy, 1000)
  v.update(standingBy, 1000 + LINGER_MS)
  assert.equal(v.isVisible, false)
  v.update(listening, 9000)
  assert.equal(v.isVisible, true)
})

test('changing the setting drops a toggle; show() shows it', () => {
  const v = new OverlayVisibility('auto')
  v.toggle()
  assert.equal(v.isVisible, true)
  v.setSetting('auto')
  assert.equal(v.isVisible, false)
  v.setSetting('always')
  assert.equal(v.setting, 'always')
  assert.equal(v.isVisible, true)
  v.setSetting('off')
  assert.equal(v.isVisible, false)
  v.show()
  assert.equal(v.isVisible, true)
})
