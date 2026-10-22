import assert from 'node:assert/strict'
import { test } from 'node:test'

import { type AppSnapshot, parseSnapshot } from '../src/shared/snapshot'

/** The sample push in docs/APP.md; the plugin's tests describe the same shape. */
const SAMPLE: AppSnapshot = {
  v: 1,
  sessionId: '3f9a0c1d2b4e5f60',
  mode: 'thinking',
  phase: 'sleeping',
  mic: 0,
  out: 0,
  utterance: 'Run the tests',
  actions: [{ label: 'Bash Run the unit tests', status: 'done' }],
  isOwner: true,
  at: 1759870000000,
}

function parsed(value: unknown): AppSnapshot {
  const result = parseSnapshot(value)
  if (!result.ok) throw new Error(`expected a snapshot, got: ${result.message}`)
  return result.snapshot
}

function failure(value: unknown): string {
  const result = parseSnapshot(value)
  if (result.ok) throw new Error('expected the snapshot to be refused')
  return result.message
}

test('the sample push parses as it is', () => {
  assert.deepEqual(parsed(JSON.parse(JSON.stringify(SAMPLE))), SAMPLE)
})

test('utterance and reply may be missing', () => {
  const { utterance: _utterance, ...rest } = SAMPLE
  const snapshot = parsed(rest)
  assert.equal('utterance' in snapshot, false)
  assert.equal('reply' in snapshot, false)
})

test('unknown fields are dropped, inside actions too', () => {
  const snapshot = parsed({ ...SAMPLE, extra: 1, actions: [{ label: 'Read', status: 'running', tool: 'Read' }] })
  assert.deepEqual(snapshot, { ...SAMPLE, actions: [{ label: 'Read', status: 'running' }] })
})

test('levels are clamped to 0..1', () => {
  const snapshot = parsed({ ...SAMPLE, mic: 1.7, out: -1 })
  assert.equal(snapshot.mic, 1)
  assert.equal(snapshot.out, 0)
})

test('long texts and lists are cut, never refused', () => {
  const snapshot = parsed({
    ...SAMPLE,
    utterance: 'u'.repeat(501),
    reply: 'r'.repeat(700),
    actions: Array.from({ length: 8 }, (_, i) => ({ label: `${i}`.padEnd(100, 'x'), status: 'done' })),
  })
  assert.equal(snapshot.utterance?.length, 500)
  assert.ok(snapshot.utterance?.endsWith('…'))
  assert.equal(snapshot.reply?.length, 600)
  assert.ok(snapshot.reply?.endsWith('…'))
  assert.equal(snapshot.actions.length, 6)
  assert.equal(snapshot.actions[0]?.label.length, 80)
  assert.ok(snapshot.actions[0]?.label.endsWith('…'))
  assert.equal(snapshot.actions[5]?.label[0], '5')
})

test('an empty utterance or reply becomes absent', () => {
  const snapshot = parsed({ ...SAMPLE, utterance: '', reply: '' })
  assert.equal('utterance' in snapshot, false)
  assert.equal('reply' in snapshot, false)
})

test('each wrong field is refused with a message naming it', () => {
  const { isOwner: _isOwner, ...noOwner } = SAMPLE
  const cases: [unknown, RegExp][] = [
    [{ ...SAMPLE, v: 2 }, /^v must be 1/],
    [{ ...SAMPLE, mode: 'dancing' }, /^mode must be one of offline, sleeping, listening, thinking, speaking, interrupted/],
    [{ ...SAMPLE, sessionId: 'two words' }, /^sessionId/],
    [{ ...SAMPLE, sessionId: 'a'.repeat(65) }, /^sessionId/],
    [{ ...SAMPLE, phase: 'Sleeping' }, /^phase/],
    [{ ...SAMPLE, mic: '1' }, /^mic must be a number/],
    [{ ...SAMPLE, out: Number.NaN }, /^out must be a number/],
    [{ ...SAMPLE, utterance: 5 }, /^utterance/],
    [{ ...SAMPLE, reply: null }, /^reply/],
    [{ ...SAMPLE, actions: 'none' }, /^actions/],
    [{ ...SAMPLE, actions: [SAMPLE.actions[0], SAMPLE.actions[0], { label: 'x', status: 'paused' }] }, /^actions\[2\] needs a string label and a status of running, done or failed/],
    [noOwner, /^isOwner must be true or false/],
    [{ ...SAMPLE, at: -1 }, /^at must be a number of 0 or more/],
    [null, /JSON object/],
    [[], /JSON object/],
    ['x', /JSON object/],
  ]
  for (const [value, message] of cases) assert.match(failure(value), message, JSON.stringify(value)?.slice(0, 80))
})
