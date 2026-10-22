import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { test } from 'node:test'

// The plugin's colours, imported at runtime here only: the app's own code keeps a copy.
import { HUD_COLORS } from '../../plugin/hooks/hud-ring'
import type { AppSnapshot } from '../src/shared/snapshot'
import { ACTION_MARKS, buildView, hoverText, MODE_COLORS, noteFor, statusLine } from '../src/shared/view'

const snap = (overrides: Partial<AppSnapshot> = {}): AppSnapshot => ({
  v: 1,
  sessionId: 's1',
  mode: 'sleeping',
  phase: 'sleeping',
  mic: 0,
  out: 0,
  actions: [],
  isOwner: true,
  at: 1,
  ...overrides,
})

test('the app colours are the plugin HUD colours', () => {
  assert.deepEqual(MODE_COLORS, HUD_COLORS)
})

test('style.css gives every mode the same colour', () => {
  // dist-test/ sits beside src/.
  const css = readFileSync(join(__dirname, '..', 'src', 'renderer', 'style.css'), 'utf8')
  const found: Record<string, number> = {}
  for (const match of css.matchAll(/body\[data-mode="(\w+)"\]\s*\{\s*--mode:\s*#([0-9a-f]{6})/g)) {
    found[match[1] as string] = Number.parseInt(match[2] as string, 16)
  }
  assert.deepEqual(found, MODE_COLORS)
})

test('with no session the view is offline and says what to do', () => {
  const view = buildView(undefined, false)
  assert.equal(view.mode, 'offline')
  assert.equal(view.label, 'OFFLINE')
  assert.equal(view.status, 'waiting for Claude Code')
  assert.equal(view.note, 'Waiting for Claude Code. Start a session with the Jarvis plugin.')
  assert.equal(view.mic, 0)
  assert.equal(view.utterance, '')
  assert.equal(view.reply, '')
  assert.deepEqual(view.actions, [])
})

test('the view carries the snapshot texts and marks the actions', () => {
  const view = buildView(
    snap({
      mode: 'interrupted',
      mic: 0.4,
      utterance: 'stop',
      actions: [
        { label: 'Read a.ts', status: 'running' },
        { label: 'Bash npm test', status: 'done' },
        { label: 'Edit b.ts', status: 'failed' },
      ],
    }),
    true,
  )
  assert.equal(view.label, 'LISTENING')
  assert.equal(view.status, 'listening')
  assert.equal(view.mic, 0.4)
  assert.equal(view.utterance, 'stop')
  assert.equal(view.reply, '')
  assert.equal(view.isOverlayShown, true)
  assert.deepEqual(
    view.actions.map(action => action.mark),
    ['›', '✓', '✗'],
  )
  assert.deepEqual(ACTION_MARKS, { running: '›', done: '✓', failed: '✗' })
})

test('a note for each phase that needs the user', () => {
  const cases: [string, string][] = [
    ['not_installed', 'The voice helper is not set up. Run /jarvis setup in Claude Code.'],
    ['setup', 'Setting up the voice helper.'],
    ['starting', 'Starting the voice helper.'],
    ['restarting', 'Starting the voice helper.'],
    ['stopped', 'The voice helper is not running. Run /jarvis in Claude Code.'],
    ['error', 'The voice helper stopped. Run /jarvis restart in Claude Code.'],
    ['failed', 'The voice helper stopped. Run /jarvis restart in Claude Code.'],
    ['elsewhere', 'Jarvis runs in another Claude Code window.'],
    ['unavailable', 'This Claude Code session runs in the cloud.'],
    ['sleeping', ''],
    ['listening', ''],
    ['speaking', ''],
  ]
  for (const [phase, note] of cases) assert.equal(noteFor(snap({ phase })), note, phase)
})

test('the tray status for each mode', () => {
  const cases: [AppSnapshot['mode'], string][] = [
    ['offline', 'offline'],
    ['sleeping', 'standing by'],
    ['listening', 'listening'],
    ['interrupted', 'listening'],
    ['thinking', 'thinking'],
    ['speaking', 'speaking'],
  ]
  for (const [mode, status] of cases) assert.equal(statusLine(snap({ mode })), status)
  assert.equal(statusLine(undefined), 'waiting for Claude Code')
})

test('hovering says what to do when Jarvis needs you, else what he is doing', () => {
  assert.equal(hoverText(buildView(snap(), false)), 'Jarvis: standing by')
  assert.equal(hoverText(buildView(snap({ mode: 'offline', phase: 'not_installed' }), false)), 'The voice helper is not set up. Run /jarvis setup in Claude Code.')
  assert.equal(hoverText(buildView(undefined, false)), 'Waiting for Claude Code. Start a session with the Jarvis plugin.')
})
