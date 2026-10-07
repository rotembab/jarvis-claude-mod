import assert from 'node:assert/strict'
import { test } from 'node:test'

import { trayMenu, type TrayMenuState } from '../src/main/tray-menu'

const STATE: TrayMenuState = {
  version: '0.8.0',
  status: 'standing by',
  isOverlayShown: false,
  overlay: 'auto',
  showOrb: true,
  startWithWindows: undefined,
}

test('the items, in order', () => {
  const items = trayMenu(STATE)
  assert.deepEqual(
    items.map(item => [item.type, item.label]),
    [
      ['normal', 'Jarvis 0.8.0: standing by'],
      ['separator', undefined],
      ['normal', 'Show the overlay'],
      ['submenu', 'Overlay'],
      ['checkbox', 'Show the orb'],
      ['separator', undefined],
      ['normal', 'Quit Jarvis'],
    ],
  )
  assert.equal(items[0]?.enabled, false)
  assert.equal(items[2]?.accelerator, 'Control+Alt+J')
  assert.deepEqual(items[2]?.action, { kind: 'toggle-overlay' })
  assert.equal(trayMenu({ ...STATE, isOverlayShown: true })[2]?.label, 'Hide the overlay')
})

test('the overlay choices follow the setting and carry their action', () => {
  for (const overlay of ['auto', 'always', 'off'] as const) {
    const submenu = trayMenu({ ...STATE, overlay })[3]?.submenu ?? []
    assert.deepEqual(
      submenu.map(item => [item.type, item.label, item.checked, item.action]),
      [
        ['radio', 'When you talk to Jarvis', overlay === 'auto', { kind: 'overlay', value: 'auto' }],
        ['radio', 'Always', overlay === 'always', { kind: 'overlay', value: 'always' }],
        ['radio', 'Never', overlay === 'off', { kind: 'overlay', value: 'off' }],
      ],
    )
  }
})

test('the orb checkbox flips the orb', () => {
  const shown = trayMenu(STATE)[4]
  assert.equal(shown?.checked, true)
  assert.deepEqual(shown?.action, { kind: 'show-orb', value: false })
  assert.deepEqual(trayMenu({ ...STATE, showOrb: false })[4]?.action, { kind: 'show-orb', value: true })
})

test('Start with Windows shows only where the app can start with the OS', () => {
  assert.equal(trayMenu(STATE).some(item => item.label === 'Start with Windows'), false)
  const items = trayMenu({ ...STATE, startWithWindows: true })
  const item = items.find(entry => entry.label === 'Start with Windows')
  assert.equal(item?.type, 'checkbox')
  assert.equal(item?.checked, true)
  assert.deepEqual(item?.action, { kind: 'start-with-windows', value: false })
  assert.equal(items.at(-1)?.label, 'Quit Jarvis')
  assert.deepEqual(items.at(-1)?.action, { kind: 'quit' })
})
