import assert from 'node:assert/strict'
import { test } from 'node:test'

import { MAX_SESSIONS, REORDER_MS, SessionBoard, STALE_MS } from '../src/main/sessions'
import type { AppSnapshot } from '../src/shared/snapshot'

const snap = (sessionId: string, overrides: Partial<AppSnapshot> = {}): AppSnapshot => ({
  v: 1,
  sessionId,
  mode: 'sleeping',
  phase: 'sleeping',
  mic: 0,
  out: 0,
  actions: [],
  isOwner: false,
  at: 1000,
  ...overrides,
})

test('an empty board shows nothing', () => {
  assert.equal(new SessionBoard().current(0), undefined)
})

test('a session shows until it misses three heartbeats', () => {
  const board = new SessionBoard()
  assert.equal(board.accept(snap('a'), 10_000), true)
  assert.equal(board.current(10_000)?.sessionId, 'a')
  assert.equal(board.current(10_000 + STALE_MS - 1)?.sessionId, 'a')
  assert.equal(board.current(10_000 + STALE_MS), undefined)
  assert.equal(board.liveCount(10_000 + STALE_MS), 0)
})

test('the window running the voice helper wins over a later one', () => {
  const board = new SessionBoard()
  board.accept(snap('owner', { isOwner: true }), 1000)
  board.accept(snap('other'), 2000)
  assert.equal(board.current(2000)?.sessionId, 'owner')
  assert.equal(board.liveCount(2000), 2)
})

test('without an owner, the session that changed last wins', () => {
  const board = new SessionBoard()
  board.accept(snap('a'), 1000)
  board.accept(snap('b'), 2000)
  assert.equal(board.current(2000)?.sessionId, 'b')
  board.accept(snap('a', { at: 1100, mode: 'thinking' }), 3000)
  assert.equal(board.current(3000)?.sessionId, 'a')
})

test('heartbeats that repeat a display do not make two windows take turns', () => {
  const board = new SessionBoard()
  const a = (at: number) => snap('a', { phase: 'stopped', utterance: 'Open the browser', at })
  const b = (at: number, overrides: Partial<AppSnapshot> = {}) => snap('b', { phase: 'elsewhere', at, ...overrides })
  board.accept(b(1000), 1000)
  board.accept(a(1100), 1100)
  assert.equal(board.current(1100)?.sessionId, 'a')
  // Each heartbeat (every 2 s) repeats what its window showed: a, which changed last, stays.
  for (let t = 3000; t <= 9000; t += 2000) {
    board.accept(b(t), t)
    assert.equal(board.current(t)?.sessionId, 'a', `after b's heartbeat at ${t}`)
    board.accept(a(t + 100), t + 100)
    assert.equal(board.current(t + 100)?.sessionId, 'a', `after a's heartbeat at ${t + 100}`)
  }
  // Heartbeats keep a session live: a's last one came at 9100.
  assert.equal(board.current(9100 + STALE_MS - 1)?.sessionId, 'a')
  // Once b shows something new, it wins.
  board.accept(b(10_000, { mode: 'thinking' }), 10_000)
  assert.equal(board.current(10_000)?.sessionId, 'b')
})

test('a snapshot older than the one held for its session is ignored', () => {
  const board = new SessionBoard()
  board.accept(snap('a', { at: 2000, mode: 'thinking' }), 1000)
  assert.equal(board.accept(snap('a', { at: 1500, mode: 'listening' }), 1100), false)
  assert.equal(board.current(1100)?.mode, 'thinking')
})

test("after the PC's clock is set back, the session's next snapshot is taken", () => {
  const board = new SessionBoard()
  const hour = 3_600_000
  board.accept(snap('a', { at: 2 * hour, mode: 'thinking' }), 1000)
  assert.equal(board.accept(snap('a', { at: 2 * hour - REORDER_MS - 1, mode: 'speaking' }), 3000), true)
  assert.equal(board.accept(snap('a', { at: hour, mode: 'listening' }), 5000), true)
  assert.equal(board.current(5000)?.mode, 'listening')
  assert.equal(board.accept(snap('a', { at: hour + 2000, mode: 'sleeping' }), 7000), true)
  assert.equal(board.current(7000 + STALE_MS - 1)?.mode, 'sleeping')
})

test('a stale owner loses to a live session', () => {
  const board = new SessionBoard()
  board.accept(snap('owner', { isOwner: true }), 1000)
  board.accept(snap('other'), 5000)
  assert.equal(board.current(1000 + STALE_MS)?.sessionId, 'other')
})

test('the board keeps at most MAX_SESSIONS sessions', () => {
  const board = new SessionBoard()
  for (let i = 0; i < 20; i += 1) board.accept(snap(`s${i}`), 1000 + i)
  assert.equal(board.liveCount(1020), MAX_SESSIONS)
  assert.equal(board.current(1020)?.sessionId, 's19')
})
