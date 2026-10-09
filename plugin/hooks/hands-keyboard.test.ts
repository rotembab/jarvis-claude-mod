import { describe, expect, test } from 'claude-code/testing'
import type { On } from 'claude-code'
import type { Engine as TestEngine } from 'claude-code/testing'

import type { HandsCommandOutcome } from './hands'
import { HANDS_TOOL, parseHandsEvent, readHandsSettings } from './hands'
import { BACKOFF_MS } from './helper'
import type { HandsKeyboardEvent, KeyboardCommandBody, KeyboardPress, KeyboardSettingsBody } from './hands-keyboard'
import { HandsKeyboard, KEYBOARD_HELP, KEYBOARD_PRESS, KEYBOARD_TEXT, parseKeyboardEvent } from './hands-keyboard'
import type { FakeChild, World } from './test-harness'
import { allowedTurn, HANDS_INSTALLED, HANDS_PORT, HANDS_PYTHON, jarvis, PLUGIN_VERSION, startHelper, startSession, world } from './test-harness'

// ---- Fixed strings (design 3.12, Appendix F: a string change is a contract change) ----

const OFF = 'The air keyboard is off. Turn it on in the Jarvis plugin settings (handKeyboard).'
const TOO_OLD = 'The hand helper is too old for the air keyboard: run /jarvis setup hands.'
const POINTER = ' Lower your hands for a second to give the pointer back.'
const AIR_NEEDS_REVIEW = 'The air-tap method only works with the review box (commit: review).'
const DIRECT_NEEDS_PINCH = 'Direct typing works only with the pinch method. Use press pinch first.'
const OPEN_AIR = 'Air keyboard open. Hold your hands over the keys, then tap each finger the strip names.'
const OPEN_PINCH = 'Air keyboard open. Hold your hands over the keys, then pinch each finger once.'
const OPEN_PRACTICE = 'Air keyboard practice: nothing you type is sent anywhere.'
const BY_CLAUDE_AIR = 'Claude opened the air keyboard. It does nothing until you tap each finger the strip names.'
const BY_CLAUDE_PINCH = 'Claude opened the air keyboard. It does nothing until you pinch each finger once.'
/** The name the model calls the tool by (hands-gate.ts HANDS_TOOL_ID; a literal here so that tool.call is typed). */
const HANDS = 'mcp__jarvis__hands'
const THE_SIX = ['start', 'practice', 'stop', 'recenter', 'private', 'public']

/** What a helper that has the keyboard says in its hello (cli.py capabilities). */
const KEYBOARD_CAPS = ['heartbeat', 'status', 'config', 'pause', 'resume', 'engage', 'disengage', 'calibrate', 'shutdown', 'keyboard']

// ---- A unit rig: the class with a fake helper, store and toasts ----

const OK: HandsCommandOutcome = { ok: true, response: { ok: true } }
const refusal = (message: string): HandsCommandOutcome => ({ ok: false, code: 'bad_request', message })

type Rig = ReturnType<typeof rig>

function rig(init: { keyboard?: boolean; press?: KeyboardPress; capabilities?: string[]; running?: boolean } = {}) {
  const sent: { name: string; body: KeyboardCommandBody }[] = []
  const toasts: string[] = []
  const debug: string[] = []
  const store = new Map<string, unknown>()
  const state = {
    keyboard: init.keyboard ?? true,
    press: init.press ?? ('air' as KeyboardPress),
    running: init.running ?? true,
    capabilities: init.capabilities ?? [...KEYBOARD_CAPS],
    notRunning: undefined as string | undefined,
    answer: (_body: KeyboardCommandBody): HandsCommandOutcome | Promise<HandsCommandOutcome> => OK,
  }
  const keyboard = new HandsKeyboard({
    helper: {
      get isRunning() {
        return state.running
      },
      capabilities: () => state.capabilities,
      send: async (name, body) => {
        sent.push({ name, body })
        return await state.answer(body)
      },
    },
    engine: {
      storeGet: async key => store.get(key),
      storeSet: async (key, value) => {
        store.set(key, value)
      },
      storeDelete: async key => {
        store.delete(key)
      },
      toast: text => {
        toasts.push(text)
      },
      debug: text => {
        debug.push(text)
      },
    },
    userConfig: () => ({ keyboard: state.keyboard, keyboardPress: state.press }),
    notRunning: async () => state.notRunning,
  })
  return { keyboard, sent, toasts, debug, store, state }
}

const bodies = (r: Rig): KeyboardCommandBody[] => r.sent.map(one => one.body)
const actions = (r: Rig): string[] => bodies(r).map(body => body.action)
const lastBody = (r: Rig): KeyboardCommandBody | undefined => r.sent.at(-1)?.body

/** The configure body of a keyboard with every setting at its default, `enabled` as the plugin option says. */
const DEFAULTS: KeyboardSettingsBody = { enabled: true, press: 'air', commit: 'review', layout: 'auto', size: 1, reach: 1, dock: 'top', enter: 'twice' }
const configure = (settings: Partial<KeyboardSettingsBody> = {}): KeyboardCommandBody => ({ action: 'configure', settings: { ...DEFAULTS, ...settings } })

/** A helper event as the parser makes it. */
function event(fields: Omit<HandsKeyboardEvent, 'v' | 'type'>): HandsKeyboardEvent {
  return { v: 1, type: 'keyboard', ...fields }
}
const OPEN = event({ state: 'open', phase: 'placing', press: 'air', level: 'ok', commit: 'review', lang: 'en', private: false })

// ---- The events (3.9): counts and enums, rebuilt from known keys (RC4) ----

/** The twelve examples of design 3.9, as the helper prints them. */
const EXAMPLES: Record<string, unknown>[] = [
  { v: 1, type: 'keyboard', state: 'open', phase: 'placing', press: 'air', level: 'ok', commit: 'review', lang: 'en', private: false },
  { v: 1, type: 'keyboard', state: 'open', phase: 'typing', press: 'air', level: 'ok', commit: 'review', lang: 'en', private: false, review: { state: 'composing', chars: 37 } },
  { v: 1, type: 'keyboard', state: 'open', phase: 'typing', press: 'air', level: 'degraded', commit: 'review', lang: 'he', private: false, hold: 'yield', review: { state: 'inserting', chars: 37 } },
  { v: 1, type: 'keyboard', state: 'open', phase: 'typing', press: 'air', level: 'ok', commit: 'review', lang: 'en', private: false, review: { state: 'composing', chars: 0, insert: { kind: 'text', outcome: 'done', sent: 37, of: 37 } } },
  { v: 1, type: 'keyboard', state: 'open', phase: 'typing', press: 'air', level: 'ok', commit: 'review', lang: 'he', private: false, review: { state: 'aborted', chars: 25, insert: { kind: 'text', outcome: 'aborted', sent: 12, of: 37, reason: 'focus' } } },
  { v: 1, type: 'keyboard', state: 'open', phase: 'typing', press: 'air', level: 'ok', commit: 'review', lang: 'en', private: false, review: { state: 'composing', chars: 0, insert: { kind: 'enter', outcome: 'done', sent: 1, of: 1 } } },
  { v: 1, type: 'keyboard', state: 'open', phase: 'warmup', press: 'pinch', level: 'off', commit: 'review', lang: 'en', private: false, review: { state: 'composing', chars: 12 } },
  { v: 1, type: 'keyboard', state: 'open', phase: 'typing', press: 'pinch', commit: 'direct', lang: 'en', private: false },
  { v: 1, type: 'keyboard', state: 'practice', phase: 'placing', press: 'air', level: 'ok', lang: 'en', private: false },
  { v: 1, type: 'keyboard', state: 'closed', reason: 'fists', discarded: 25 },
  { v: 1, type: 'keyboard', state: 'closed', reason: 'air_unreliable', discarded: 0 },
  { v: 1, type: 'keyboard', state: 'closed', reason: 'command', discarded: 0, practice: { hitRate: 0.93, phantomsPerMin: 0.0, recallIM: 0.92 } },
]

describe('the keyboard event (3.9)', () => {
  test('the twelve examples of the protocol parse to themselves', () => {
    for (const example of EXAMPLES) expect(parseKeyboardEvent(example), JSON.stringify(example)).toEqual(example)
  })

  test('M43: the event is rebuilt from known keys, so a stray text field never travels', () => {
    const hostile = {
      v: 1,
      type: 'keyboard',
      state: 'open',
      phase: 'typing',
      text: 'hello world',
      typed: 'abc',
      title: 'Notepad - passwords.txt',
      review: { state: 'composing', chars: 5, text: 'hello', box: ['h', 'e'], insert: { kind: 'text', outcome: 'done', sent: 5, of: 5, text: 'hello' } },
      practice: { hitRate: 0.5, phantomsPerMin: 1, text: 'secret' },
    }
    const parsed = parseKeyboardEvent(hostile)
    expect(parsed).toEqual({
      v: 1,
      type: 'keyboard',
      state: 'open',
      phase: 'typing',
      review: { state: 'composing', chars: 5, insert: { kind: 'text', outcome: 'done', sent: 5, of: 5 } },
      practice: { hitRate: 0.5, phantomsPerMin: 1 },
    })
    expect(JSON.stringify(parsed)).not.toMatch(/hello|abc|passwords|secret/)
    // A new object all the way down: nothing of the helper's is passed on by reference.
    expect(parsed?.review).not.toBe(hostile.review)
    expect(parsed?.review?.insert).not.toBe(hostile.review.insert)
  })

  test('an unknown state is no event; any other bad field is dropped and the event survives', () => {
    expect(parseKeyboardEvent({ v: 1, type: 'keyboard', state: 'floating' })).toBeUndefined()
    expect(parseKeyboardEvent({ v: 1, type: 'keyboard' })).toBeUndefined()
    expect(parseKeyboardEvent({ v: 1, type: 'keyboard', state: 7 })).toBeUndefined()
    const bad = {
      v: 1,
      type: 'keyboard',
      state: 'open',
      phase: 'dancing',
      reason: 'boredom',
      hold: 'sulking',
      lang: 'fr',
      press: 'foot',
      level: 'great',
      commit: 'sometimes',
      private: 'yes',
      review: { state: 'thinking', chars: 3 },
    }
    expect(parseKeyboardEvent(bad)).toEqual({ v: 1, type: 'keyboard', state: 'open' })
    // Every enum value of the schema is taken.
    expect(parseKeyboardEvent({ v: 1, type: 'keyboard', state: 'closed', reason: 'air_unreliable', hold: 'slow', level: 'degraded', press: 'windows', lang: 'he', private: true })).toEqual({
      v: 1,
      type: 'keyboard',
      state: 'closed',
      reason: 'air_unreliable',
      hold: 'slow',
      level: 'degraded',
      press: 'windows',
      lang: 'he',
      private: true,
    })
  })

  test('numbers outside their range drop their field: the box, the insert result, the discarded count, the practice numbers', () => {
    const base = { v: 1, type: 'keyboard', state: 'open' }
    // review needs state and chars (0..200, an integer); without them there is no review.
    for (const chars of [-1, 201, 1.5, '5', Number.NaN, Number.POSITIVE_INFINITY, null]) {
      expect(parseKeyboardEvent({ ...base, review: { state: 'composing', chars } }), String(chars)).toEqual(base)
    }
    expect(parseKeyboardEvent({ ...base, review: { state: 'composing', chars: 200 } })?.review).toEqual({ state: 'composing', chars: 200 })
    expect(parseKeyboardEvent({ ...base, review: { state: 'composing', chars: 0 } })?.review).toEqual({ state: 'composing', chars: 0 })
    // A bad insert result is dropped alone; the review survives. `of` is 1..200, `sent` 0..200, integers.
    const insert = { kind: 'text', outcome: 'done', sent: 3, of: 3 }
    for (const broken of [{ sent: -1 }, { sent: 201 }, { sent: 1.5 }, { of: 0 }, { of: 201 }, { kind: 'paste' }, { outcome: 'maybe' }, { of: '3' }]) {
      const parsed = parseKeyboardEvent({ ...base, review: { state: 'composing', chars: 0, insert: { ...insert, ...broken } } })
      expect(parsed?.review, JSON.stringify(broken)).toEqual({ state: 'composing', chars: 0 })
    }
    // A bad abort reason is dropped alone, keeping the insert result.
    expect(parseKeyboardEvent({ ...base, review: { state: 'aborted', chars: 4, insert: { kind: 'text', outcome: 'aborted', sent: 1, of: 5, reason: 'gremlins' } } })?.review?.insert).toEqual({
      kind: 'text',
      outcome: 'aborted',
      sent: 1,
      of: 5,
    })
    for (const discarded of [-1, 201, 2.5, '3', Number.NaN]) expect(parseKeyboardEvent({ ...base, discarded }), String(discarded)).toEqual(base)
    expect(parseKeyboardEvent({ ...base, discarded: 200 })?.discarded).toBe(200)
    // practice needs both numbers; recallIM goes alone when out of range.
    for (const practice of [{ hitRate: 1.5, phantomsPerMin: 0 }, { hitRate: -0.1, phantomsPerMin: 0 }, { hitRate: 0.5, phantomsPerMin: -1 }, { hitRate: 0.5 }, { phantomsPerMin: 1 }, { hitRate: Number.NaN, phantomsPerMin: 1 }]) {
      expect(parseKeyboardEvent({ ...base, practice }), JSON.stringify(practice)).toEqual(base)
    }
    expect(parseKeyboardEvent({ ...base, practice: { hitRate: 0.5, phantomsPerMin: 4.5, recallIM: 1.2 } })?.practice).toEqual({ hitRate: 0.5, phantomsPerMin: 4.5 })
    expect(parseKeyboardEvent({ ...base, practice: { hitRate: 0.5, phantomsPerMin: 4.5, recallIM: 0 } })?.practice).toEqual({ hitRate: 0.5, phantomsPerMin: 4.5, recallIM: 0 })
  })

  test('E2: the hands event parser hands keyboard events to it, and an extra text field never survives', () => {
    const line = JSON.stringify({ v: 1, type: 'keyboard', state: 'open', phase: 'typing', review: { state: 'composing', chars: 9, text: 'secret' }, text: 'secret' })
    expect(parseHandsEvent(line)).toEqual({ v: 1, type: 'keyboard', state: 'open', phase: 'typing', review: { state: 'composing', chars: 9 } })
    expect(parseHandsEvent('{"v":1,"type":"keyboard","state":"nope"}')).toBeUndefined()
    expect(parseHandsEvent('{"v":2,"type":"keyboard","state":"open"}')).toBeUndefined()
  })
})

// ---- Authority: the plugin option is the only switch (SR1, SR16, SR27) ----

describe('the plugin option is the only switch', () => {
  test('M1: with the option off every command and tool action answers the off text and sends nothing', async () => {
    const r = rig({ keyboard: false })
    const commands = [[], ['on'], ['practice'], ['recenter'], ['private'], ['public'], ['status'], ['press', 'pinch'], ['commit', 'review'], ['layout', 'he'], ['size', '1.2'], ['reach', '1'], ['dock', 'bottom'], ['enter', 'off']]
    for (const args of commands) expect(await r.keyboard.run(args), `keyboard ${args.join(' ')}`).toBe(OFF)
    for (const action of ['keyboard', 'keyboard_practice'] as const) expect(await r.keyboard.runTool(action), action).toBe(OFF)
    expect(r.sent).toEqual([])
    expect(r.store.size).toBe(0)
    // The help is only text.
    expect(await r.keyboard.run(['help'])).toBe(KEYBOARD_HELP)
    expect(r.keyboard.statusLines()).toEqual([])
  })

  test('M2: off, stop and keyboard_off always send stop, whatever the option says', async () => {
    for (const keyboard of [false, true]) {
      const r = rig({ keyboard })
      const calls = [() => r.keyboard.run(['off']), () => r.keyboard.run(['STOP']), () => r.keyboard.runTool('keyboard_off')]
      for (const call of calls) {
        r.sent.length = 0
        const text = await call()
        expect(bodies(r), `option ${keyboard}`).toEqual([{ action: 'stop' }])
        expect(text).toMatch(/closing/i)
        expect(text).toMatch(/thrown away/)
      }
      // Closing never needs the keyboard to be set up: nothing was configured first.
      expect(actions(r)).not.toContain('configure')
    }
  })

  test('M2: closing with no helper to close sends nothing and says it is not open', async () => {
    const stopped = rig({ keyboard: false, running: false })
    expect(await stopped.keyboard.run(['off'])).toBe('The air keyboard is not open.')
    const old = rig({ capabilities: ['ptt'] })
    expect(await old.keyboard.runTool('keyboard_off')).toBe('The air keyboard is not open.')
    expect(stopped.sent).toEqual([])
    expect(old.sent).toEqual([])
  })

  test('M3: enabled is sent only by sync, and only from the option; a stored value cannot make it true', async () => {
    // Option on: the configure says so.
    const on = rig()
    await on.keyboard.sync()
    expect(bodies(on)).toEqual([configure()])
    // Option off with a stored choice: the configure says off, and nothing stored changes that.
    const off = rig({ keyboard: false })
    off.store.set('handsKeyboardPress', 'pinch')
    for (const key of ['handsKeyboardEnabled', 'handsKeyboard', 'handKeyboard', 'enabled', 'handsKeyboardOn']) off.store.set(key, true)
    await off.keyboard.sync()
    expect(bodies(off)).toEqual([configure({ enabled: false, press: 'pinch' })])
    // The tool, with the option off, never reaches sync: no configure, no start.
    off.sent.length = 0
    await off.keyboard.runTool('keyboard')
    await off.keyboard.runTool('keyboard_practice')
    expect(off.sent).toEqual([])
    // With the option on the tool syncs first, with enabled from the option, then opens.
    on.sent.length = 0
    await on.keyboard.runTool('keyboard')
    expect(bodies(on)).toEqual([configure(), { action: 'start' }])
    // No command sets enabled: the words that would are unknown subcommands.
    for (const args of [['enabled', 'on'], ['enable'], ['enabled', 'true'], ['allow']]) {
      on.sent.length = 0
      expect(await on.keyboard.run(args), args.join(' ')).toMatch(/^Unknown subcommand/)
      expect(on.sent).toEqual([])
    }
  })

  test('M41: nothing inserts, sends, clears or types: not as a tool action, a subcommand or a command body', async () => {
    const actionsOfTool = HANDS_TOOL.inputSchema.properties.action.enum
    expect(actionsOfTool).toEqual(['on', 'off', 'status', 'calibrate', 'pause', 'resume', 'engage', 'disengage', 'keyboard', 'keyboard_practice', 'keyboard_off'])
    expect(Object.keys(HANDS_TOOL.inputSchema.properties)).toEqual(['action', 'display', 'setting', 'value', 'preset'])
    for (const action of actionsOfTool) expect(action).not.toMatch(/insert|send|clear|type|text|enter/i)
    // The subcommands that would are unknown, send nothing and store nothing.
    const r = rig()
    for (const args of [['insert'], ['send'], ['clear'], ['type', 'hello'], ['text', 'hello'], ['paste'], ['decoder', 'auto'], ['insert', 'hello world']]) {
      expect(await r.keyboard.run(args), args.join(' ')).toMatch(/^Unknown subcommand/)
    }
    expect(r.sent).toEqual([])
    expect(r.store.size).toBe(0)
    // Every body this class ever sends is one of the six actions or a configure of the protocol's settings.
    // recenter, private and public are only sent to a keyboard that is open (the helper drops them otherwise).
    r.keyboard.onEvent(OPEN)
    for (const word of ['on', 'practice', 'off', 'recenter', 'private', 'public']) await r.keyboard.run([word])
    for (const args of [['press', 'pinch'], ['commit', 'direct'], ['layout', 'he'], ['size', '1.4'], ['reach', '0.9'], ['dock', 'bottom'], ['enter', 'off']]) await r.keyboard.run(args)
    await r.keyboard.runTool('keyboard')
    await r.keyboard.runTool('keyboard_practice')
    await r.keyboard.runTool('keyboard_off')
    expect(r.sent.length).toBeGreaterThan(10)
    const SETTINGS = new Set(['enabled', 'press', 'commit', 'layout', 'size', 'reach', 'dock', 'enter'])
    for (const body of bodies(r)) {
      if (body.action === 'configure') {
        expect(Object.keys(body)).toEqual(['action', 'settings'])
        for (const [key, value] of Object.entries(body.settings)) {
          expect(SETTINGS.has(key), key).toBe(true)
          expect(['string', 'number', 'boolean']).toContain(typeof value)
        }
        // idleS and inject are helper config only, never sent by the mod.
        expect(Object.keys(body.settings)).not.toContain('idleS')
        expect(Object.keys(body.settings)).not.toContain('inject')
      } else {
        expect(Object.keys(body)).toEqual(['action'])
        expect(THE_SIX).toContain(body.action)
      }
    }
  })

  test('M4: the command body type has no text and no insert or send action (checked by tsc)', () => {
    // @ts-expect-error an insert action does not exist
    const insert: KeyboardCommandBody = { action: 'insert' }
    // @ts-expect-error a text field does not exist
    const text: KeyboardCommandBody = { action: 'start', text: 'hello' }
    // @ts-expect-error settings carry no text
    const settings: KeyboardCommandBody = { action: 'configure', settings: { text: 'hello' } }
    // @ts-expect-error nor an invented press method
    const press: KeyboardSettingsBody = { press: 'foot' }
    expect([insert, text, settings, press]).toHaveLength(4)
  })
})

// ---- sync (3.12 "sync() rule") ----

describe('sync: the keyboard settings, after the helper\'s config', () => {
  test('a helper without the keyboard capability hears nothing, whatever is stored or set', async () => {
    const r = rig({ capabilities: ['ptt'] })
    r.store.set('handsKeyboardSize', 1.2)
    await r.keyboard.sync()
    expect(r.sent).toEqual([])
    const stopped = rig({ running: false })
    await stopped.keyboard.sync()
    expect(stopped.sent).toEqual([])
  })

  test('with the option off, nothing stored and nothing sent before, it sends nothing: the helper starts with the keyboard off', async () => {
    const r = rig({ keyboard: false })
    await r.keyboard.sync()
    expect(r.sent).toEqual([])
  })

  test('the option on sends the whole effective set: the plugin option\'s press, every default, enabled true', async () => {
    const r = rig({ press: 'pinch' })
    await r.keyboard.sync()
    expect(bodies(r)).toEqual([configure({ press: 'pinch' })])
  })

  test('a stored choice wins over the plugin option\'s press; clearing it sends the default again', async () => {
    const r = rig({ press: 'air' })
    await r.keyboard.run(['press', 'pinch'])
    expect(lastBody(r)).toEqual(configure({ press: 'pinch' }))
    await r.keyboard.run(['press', 'default'])
    expect(lastBody(r)).toEqual(configure({ press: 'air' }))
    expect(r.store.has('handsKeyboardPress')).toBe(false)
  })

  test('a configure already sent is followed by another after a change of the option, until the helper restarts (reset)', async () => {
    const r = rig()
    await r.keyboard.sync()
    r.state.keyboard = false
    await r.keyboard.sync()
    expect(bodies(r)).toEqual([configure(), configure({ enabled: false })])
    r.keyboard.reset()
    r.sent.length = 0
    await r.keyboard.sync()
    expect(r.sent).toEqual([])
  })

  test('a stored value that is not valid is ignored, never sent', async () => {
    const r = rig()
    r.store.set('handsKeyboardSize', 9)
    r.store.set('handsKeyboardReach', 'wide')
    r.store.set('handsKeyboardPress', 'foot')
    r.store.set('handsKeyboardCommit', 'sometimes')
    r.store.set('handsKeyboardDock', 7)
    r.store.set('handsKeyboardLayout', null)
    r.store.set('handsKeyboardEnter', 'thrice')
    await r.keyboard.sync()
    expect(bodies(r)).toEqual([configure()])
  })

  test('stored direct typing is only sent with the pinch method (stricter only)', async () => {
    const r = rig({ press: 'pinch' })
    await r.keyboard.run(['commit', 'direct'])
    expect(lastBody(r)).toEqual(configure({ press: 'pinch', commit: 'direct' }))
    // The plugin option changes to air later: the helper would refuse air with direct, so review is sent.
    r.state.press = 'air'
    await r.keyboard.sync()
    expect(lastBody(r)).toEqual(configure({ press: 'air', commit: 'review' }))
    expect(r.store.get('handsKeyboardCommit')).toBe('direct')
  })

  test('a refused configure is logged and never thrown; syncs do not interleave', async () => {
    const r = rig()
    r.state.answer = () => refusal('settings.size: must be at most 1.6')
    await r.keyboard.sync()
    expect(r.debug.join('\n')).toContain('settings.size: must be at most 1.6')
    // Two syncs started together send in the order of the changes, each from a fresh read of the store.
    const slow = rig()
    const trace: string[] = []
    let release: () => void = () => undefined
    const gate = new Promise<void>(resolve => {
      release = resolve
    })
    slow.state.answer = async () => {
      trace.push('in')
      await gate
      trace.push('out')
      return OK
    }
    const first = slow.keyboard.sync()
    for (let turn = 0; turn < 10; turn += 1) await Promise.resolve()
    slow.store.set('handsKeyboardSize', 1.3)
    const second = slow.keyboard.sync()
    for (let turn = 0; turn < 10; turn += 1) await Promise.resolve()
    // The second waits for the first's answer; only then does it read the store and send.
    expect(trace).toEqual(['in'])
    release()
    await Promise.all([first, second])
    expect(trace).toEqual(['in', 'out', 'in', 'out'])
    expect(bodies(slow)).toEqual([configure(), configure({ size: 1.3 })])
  })
})

// ---- The commands ----

describe('/jarvis hands keyboard: opening and closing', () => {
  test('on (or nothing) syncs, then starts; practice does the same with practice', async () => {
    for (const args of [[], ['on'], ['ON']]) {
      const r = rig()
      const text = await r.keyboard.run(args)
      expect(bodies(r)).toEqual([configure(), { action: 'start' }])
      expect(text).toBe(
        'Opening the air keyboard. Hold your hands over the keys, then tap each finger the strip names. What you tap goes into a review box; nothing reaches another window until you tap Insert three times, firmly, with your hand still.',
      )
    }
    const practice = rig()
    expect(await practice.keyboard.run(['practice'])).toBe('Opening the air keyboard in practice mode: nothing you tap is sent anywhere.')
    expect(bodies(practice)).toEqual([configure(), { action: 'practice' }])
  })

  test('the reply follows the press method: pinch says pinch, windows says what it does not guard', async () => {
    const pinch = rig({ press: 'pinch' })
    expect(await pinch.keyboard.run([])).toContain('then pinch each finger once.')
    const windows = rig({ press: 'windows' })
    const text = await windows.keyboard.run([])
    expect(text).toBe("Opening Windows' own on-screen keyboard. Jarvis' keyboard safeguards do not apply to it.")
  })

  test('recenter, private and public are plain commands', async () => {
    const r = rig()
    r.keyboard.onEvent(OPEN)
    expect(await r.keyboard.run(['recenter'])).toBe('Recentering the air keyboard on your hands.')
    expect(await r.keyboard.run(['private'])).toBe('Air keyboard private mode is on: the box and the key highlights are hidden on screen.')
    expect(await r.keyboard.run(['public'])).toBe('Air keyboard private mode is off: the box and the key highlights show again.')
    expect(bodies(r)).toEqual([{ action: 'recenter' }, { action: 'private' }, { action: 'public' }])
    // The practice is a keyboard too: private and recenter reach it.
    const practice = rig()
    practice.keyboard.onEvent(event({ state: 'practice', phase: 'placing', press: 'air', level: 'ok', lang: 'en', private: false }))
    expect(await practice.keyboard.run(['private'])).toBe('Air keyboard private mode is on: the box and the key highlights are hidden on screen.')
    expect(await practice.keyboard.run(['recenter'])).toBe('Recentering the air keyboard on your hands.')
    expect(actions(practice)).toEqual(['private', 'recenter'])
  })

  test('with no keyboard open, recenter, private and public say so and send nothing (the helper would drop them, and a new open starts public)', async () => {
    const notOpen = (action: string): string => `The air keyboard is not open. Open it first, then use ${action}.`
    const r = rig()
    // Never opened: no event yet.
    for (const action of ['recenter', 'private', 'public']) expect(await r.keyboard.run([action]), action).toBe(notOpen(action))
    expect(r.sent).toEqual([])
    // Closed again: the same, and the reply never claims private mode is on.
    r.keyboard.onEvent(OPEN)
    r.keyboard.onEvent(event({ state: 'closed', reason: 'command' }))
    for (const action of ['recenter', 'private', 'public']) expect(await r.keyboard.run([action]), `closed ${action}`).toBe(notOpen(action))
    expect(r.sent).toEqual([])
    // A helper that restarted took its keyboard with it.
    r.keyboard.onEvent(OPEN)
    r.keyboard.reset()
    expect(await r.keyboard.run(['private'])).toBe(notOpen('private'))
    expect(r.sent).toEqual([])
    // Open again: it goes through.
    r.keyboard.onEvent(OPEN)
    expect(await r.keyboard.run(['private'])).toBe('Air keyboard private mode is on: the box and the key highlights are hidden on screen.')
    expect(bodies(r)).toEqual([{ action: 'private' }])
  })

  test('the closed-keyboard answer comes after the off and too-old answers, which still win', async () => {
    const off = rig({ keyboard: false })
    expect(await off.keyboard.run(['private'])).toBe(OFF)
    const old = rig({ capabilities: ['ptt'] })
    expect(await old.keyboard.run(['private'])).toBe(TOO_OLD)
    const down = rig()
    down.state.notRunning = 'Hand control is off. Turn it on with /jarvis hands on.'
    expect(await down.keyboard.run(['recenter'])).toBe('Hand control is off. Turn it on with /jarvis hands on.')
    expect(off.sent).toEqual([])
    expect(old.sent).toEqual([])
    expect(down.sent).toEqual([])
  })

  test('the plain subcommands take no words: an extra one is named and nothing is sent', async () => {
    const r = rig()
    for (const args of [['practice', 'now'], ['recenter', 'x'], ['private', 'please'], ['status', 'full'], ['on', 'twice']]) {
      expect(await r.keyboard.run(args), args.join(' ')).toBe(`Unknown option "${args[1]}" for /jarvis hands keyboard ${args[0]}; it takes none.`)
    }
    expect(r.sent).toEqual([])
  })

  test('it says why when hand control is not running, or the helper is too old, and sends nothing', async () => {
    const down = rig()
    down.state.notRunning = 'Hand control is off. Turn it on with /jarvis hands on.'
    expect(await down.keyboard.run([])).toBe('Hand control is off. Turn it on with /jarvis hands on.')
    expect(await down.keyboard.runTool('keyboard')).toBe('Hand control is off. Turn it on with /jarvis hands on.')
    expect(down.sent).toEqual([])
    // M5: an older helper has no keyboard command.
    const old = rig({ capabilities: ['ptt'] })
    for (const args of [[], ['practice'], ['recenter'], ['private'], ['press', 'pinch']]) expect(await old.keyboard.run(args), args.join(' ')).toBe(TOO_OLD)
    expect(await old.keyboard.runTool('keyboard')).toBe(TOO_OLD)
    expect(await old.keyboard.runTool('keyboard_practice')).toBe(TOO_OLD)
    expect(old.sent).toEqual([])
    // The refused setting was not kept either.
    expect(old.store.size).toBe(0)
  })

  test("the helper's refusal is the answer, as the helper words it (fixed texts of 3.8)", async () => {
    const r = rig()
    const text = 'Practice first: run /jarvis hands keyboard practice once with the air method.'
    r.state.answer = body => (body.action === 'start' ? refusal(text) : OK)
    expect(await r.keyboard.run([])).toBe(text)
    expect(await r.keyboard.runTool('keyboard')).toBe(text)
    // Anything else (no answer, a timeout) is the mod's own sentence.
    r.state.answer = body => (body.action === 'start' ? { ok: false, code: 'timeout', message: 'start timed out after 3000 ms' } : OK)
    expect(await r.keyboard.run([])).toBe('Could not open the air keyboard: start timed out after 3000 ms')
    // A refused configure stops the open: the helper would say the keyboard is off.
    r.state.answer = body => (body.action === 'configure' ? refusal('unexpected property "commit"') : OK)
    r.sent.length = 0
    expect(await r.keyboard.run([])).toBe(
      'The hand helper refused the air keyboard settings (unexpected property "commit"). /jarvis setup hands updates an older hand helper. Nothing was opened.',
    )
    expect(actions(r)).toEqual(['configure'])
  })

  test('a refused refusal text is clipped and cleaned: nothing the helper adds can swell or break the line', async () => {
    const r = rig()
    r.state.answer = body => (body.action === 'start' ? refusal(`${'x'.repeat(500)}\u0007\n`) : OK)
    const text = await r.keyboard.run([])
    expect(text.length).toBeLessThanOrEqual(240)
    expect(text).not.toMatch(/[\u0000-\u001f]/)
  })
})

describe('/jarvis hands keyboard: settings', () => {
  test('M8: press air is accepted, press air with a stored direct commit is refused, and an unknown method is refused before anything is stored', async () => {
    const r = rig({ press: 'pinch' })
    expect(await r.keyboard.run(['press', 'air'])).toBe(
      'Air keyboard press method is now air: tap a finger in the air; letters go to a review box first. It applies the next time the keyboard opens.',
    )
    expect(r.store.get('handsKeyboardPress')).toBe('air')
    expect(lastBody(r)).toEqual(configure({ press: 'air' }))
    expect(await r.keyboard.run(['press', 'pinch'])).toContain('pinch your thumb to the finger over a key.')
    expect(await r.keyboard.run(['commit', 'direct'])).toContain('Air keyboard commit mode is now direct')
    r.sent.length = 0
    expect(await r.keyboard.run(['press', 'air'])).toBe(AIR_NEEDS_REVIEW)
    // The same refusal when "default" would land on air (the plugin option says air).
    r.state.press = 'air'
    expect(await r.keyboard.run(['press', 'default'])).toBe(AIR_NEEDS_REVIEW)
    expect(r.sent).toEqual([])
    expect(r.store.get('handsKeyboardPress')).toBe('pinch')
    for (const word of ['foot', 'osk', '', 'air,pinch', 'default now']) {
      const before = r.store.size
      expect(await r.keyboard.run(['press', word]), word).toMatch(/is not a press method\. Use \/jarvis hands keyboard press <air\|pinch\|windows\|default>/)
      expect(r.store.size).toBe(before)
    }
    expect(r.sent).toEqual([])
  })

  test('X49: air, pinch and windows are the press methods and air is the default of the plugin option', () => {
    expect([...KEYBOARD_PRESS]).toEqual(['air', 'pinch', 'windows'])
    expect(readHandsSettings({})).toMatchObject({ keyboard: false, keyboardPress: 'air' })
    expect(readHandsSettings({ handKeyboard: 'on' })).toMatchObject({ keyboard: true, keyboardPress: 'air' })
    for (const press of KEYBOARD_PRESS) expect(readHandsSettings({ handKeyboardPress: press }).keyboardPress).toBe(press)
    expect(readHandsSettings({ handKeyboardPress: 'osk' }).keyboardPress).toBe('air')
    expect(readHandsSettings({ handKeyboardPress: ' pinch ' }).keyboardPress).toBe('pinch')
    // Only the exact word "on" turns it on.
    for (const value of ['ON', 'true', '1', 'yes', 'off', '']) expect(readHandsSettings({ handKeyboard: value }).keyboard, value).toBe(false)
    expect(readHandsSettings({ handKeyboard: true as unknown as string }).keyboard).toBe(false)
  })

  test('M40: commit is validated before it is stored; direct only with the pinch method; default clears it', async () => {
    const r = rig({ press: 'air' })
    expect(await r.keyboard.run(['commit', 'direct'])).toBe(DIRECT_NEEDS_PINCH)
    expect(await r.keyboard.run(['commit', 'sometimes'])).toMatch(/is not a commit mode\. Use \/jarvis hands keyboard commit <review\|direct\|default>/)
    expect(r.store.size).toBe(0)
    expect(r.sent).toEqual([])
    expect(await r.keyboard.run(['commit', 'review'])).toBe(
      'Air keyboard commit mode is now review: what you tap goes into the box, and only three taps on Insert type it into a window. It applies the next time the keyboard opens.',
    )
    expect(r.store.get('handsKeyboardCommit')).toBe('review')
    // Direct under a stored pinch choice.
    await r.keyboard.run(['press', 'pinch'])
    expect(await r.keyboard.run(['commit', 'direct'])).toBe(
      'Air keyboard commit mode is now direct: pinch presses type straight into the window in front; practice first (/jarvis hands keyboard practice). It applies the next time the keyboard opens.',
    )
    expect(lastBody(r)).toEqual(configure({ press: 'pinch', commit: 'direct' }))
    // The plugin option's press counts when nothing is stored for press.
    const viaOption = rig({ press: 'pinch' })
    expect(await viaOption.keyboard.run(['commit', 'direct'])).toContain('commit mode is now direct')
    // Default clears the store and sends review again, so the helper forgets direct.
    expect(await r.keyboard.run(['commit', 'default'])).toBe('Air keyboard commit mode is back to its default, review. It applies the next time the keyboard opens.')
    expect(r.store.has('handsKeyboardCommit')).toBe(false)
    expect(lastBody(r)).toEqual(configure({ press: 'pinch', commit: 'review' }))
  })

  test('M8: size 9 is rejected before anything is stored; the numbers are plain decimals inside their range', async () => {
    const r = rig()
    expect(await r.keyboard.run(['size', '9'])).toBe('9 is outside the range for the keyboard size: 0.6 to 1.6 (default 1). Nothing was changed.')
    for (const bad of ['abc', '1e0', '0x10', 'NaN', 'Infinity', '', '1,5']) {
      expect(await r.keyboard.run(['size', bad]), bad).toMatch(/is not a number\. The keyboard size takes a number from 0\.6 to 1\.6 \(default 1\), or "default": \/jarvis hands keyboard size 1\.15\./)
    }
    expect(await r.keyboard.run(['reach', '1.51'])).toBe('1.51 is outside the range for the keyboard reach: 0.8 to 1.5 (default 1). Nothing was changed.')
    expect(await r.keyboard.run(['reach', '0.79'])).toContain('outside the range')
    expect(r.store.size).toBe(0)
    expect(r.sent).toEqual([])
    // The ends are in.
    expect(await r.keyboard.run(['size', '1.6'])).toBe('Air keyboard size is now 1.6 (range 0.6 to 1.6). It applies the next time the keyboard opens.')
    expect(await r.keyboard.run(['size', '0.6'])).toContain('size is now 0.6')
    expect(await r.keyboard.run(['reach', '0.8'])).toContain('reach is now 0.8')
    expect(await r.keyboard.run(['reach', '1.5'])).toContain('reach is now 1.5')
    expect(await r.keyboard.run(['size', '1.2346'])).toContain('size is now 1.235 ')
    expect(r.store.get('handsKeyboardSize')).toBe(1.235)
    expect(lastBody(r)).toEqual(configure({ size: 1.235, reach: 1.5 }))
    expect(await r.keyboard.run(['size', 'default'])).toBe('Air keyboard size is back to its default, 1. It applies the next time the keyboard opens.')
    expect(r.store.has('handsKeyboardSize')).toBe(false)
    expect(lastBody(r)).toEqual(configure({ reach: 1.5 }))
  })

  test('layout, dock and enter take their words, in any case; the wrong ones are refused', async () => {
    const r = rig()
    expect(await r.keyboard.run(['layout', 'HE'])).toBe('Air keyboard layout is now he. It applies the next time the keyboard opens.')
    expect(await r.keyboard.run(['dock', 'Bottom'])).toBe('Air keyboard dock is now bottom. It applies the next time the keyboard opens.')
    expect(await r.keyboard.run(['enter', 'off'])).toBe('Air keyboard Enter key is now off: there is no Send key. It applies the next time the keyboard opens.')
    expect(lastBody(r)).toEqual(configure({ layout: 'he', dock: 'bottom', enter: 'off' }))
    expect(await r.keyboard.run(['enter', 'twice'])).toBe('Air keyboard Enter key is now twice: Send exists, three guarded taps right after an Insert. It applies the next time the keyboard opens.')
    expect(await r.keyboard.run(['layout', 'fr'])).toMatch(/is not a layout\. Use \/jarvis hands keyboard layout <auto\|en\|he\|default>/)
    expect(await r.keyboard.run(['dock', 'left'])).toMatch(/is not a dock position\. Use \/jarvis hands keyboard dock <top\|bottom\|default>/)
    expect(await r.keyboard.run(['enter', 'always'])).toMatch(/is not an Enter key choice\. Use \/jarvis hands keyboard enter <twice\|off\|default>/)
    expect(r.store.get('handsKeyboardLayout')).toBe('he')
    // Extra words are not guessed at.
    expect(await r.keyboard.run(['dock', 'top', 'please'])).toMatch(/^Use \/jarvis hands keyboard dock <top\|bottom\|default>/)
    expect(r.store.get('handsKeyboardDock')).toBe('bottom')
  })

  test('a setting named alone reports its value, its default and how to change it', async () => {
    const r = rig()
    expect(await r.keyboard.run(['size'])).toBe('Air keyboard size is 1 (default 1, range 0.6 to 1.6). Change it with /jarvis hands keyboard size <0.6 to 1.6|default>.')
    await r.keyboard.run(['reach', '1.3'])
    expect(await r.keyboard.run(['reach'])).toBe('Air keyboard reach is 1.3 (default 1, range 0.8 to 1.5). Change it with /jarvis hands keyboard reach <0.8 to 1.5|default>.')
    expect(await r.keyboard.run(['press'])).toBe('Air keyboard press method is air (the plugin setting says air). Change it with /jarvis hands keyboard press <air|pinch|windows|default>.')
    await r.keyboard.run(['press', 'windows'])
    expect(await r.keyboard.run(['press'])).toBe('Air keyboard press method is windows (the plugin setting says air). Change it with /jarvis hands keyboard press <air|pinch|windows|default>.')
    expect(await r.keyboard.run(['layout'])).toBe('Air keyboard layout is auto (default auto). Change it with /jarvis hands keyboard layout <auto|en|he|default>.')
  })

  test("a setting the helper refuses is not kept, so the next sync is not refused for it again", async () => {
    const r = rig()
    await r.keyboard.run(['size', '1.1'])
    r.state.answer = () => refusal('settings.size: unexpected')
    expect(await r.keyboard.run(['size', '1.4'])).toBe('The hand helper refused it: settings.size: unexpected. Nothing was changed.')
    expect(r.store.get('handsKeyboardSize')).toBe(1.1)
    await r.keyboard.run(['dock', 'bottom'])
    expect(r.store.has('handsKeyboardDock')).toBe(false)
  })

  test('with no helper running the choice is kept and applies when hand control starts', async () => {
    const r = rig({ running: false })
    expect(await r.keyboard.run(['size', '1.2'])).toBe('Air keyboard size is now 1.2 (range 0.6 to 1.6); it applies when hand control starts.')
    expect(r.store.get('handsKeyboardSize')).toBe(1.2)
    expect(r.sent).toEqual([])
    // The next hello sends it.
    r.state.running = true
    await r.keyboard.sync()
    expect(bodies(r)).toEqual([configure({ size: 1.2 })])
  })

  test('status shows the option, the state and the settings in force, never text', async () => {
    const r = rig()
    await r.keyboard.run(['reach', '1.2'])
    r.keyboard.onEvent(event({ state: 'open', phase: 'typing', press: 'air', level: 'ok', commit: 'review', review: { state: 'composing', chars: 12 } }))
    const text = await r.keyboard.run(['status'])
    expect(text).toBe(
      [
        'Air keyboard: open (typing).',
        'Air keyboard review box: 12 characters waiting.',
        'Air keyboard settings: press air, commit review, layout auto, size 1, reach 1.2, dock top, enter twice. /jarvis hands keyboard help lists how to change them.',
      ].join('\n'),
    )
  })

  test('help lists the subcommands and says what the box is', () => {
    expect(KEYBOARD_HELP).toContain('/jarvis hands keyboard [on]')
    expect(KEYBOARD_HELP).toContain('/jarvis hands keyboard off')
    expect(KEYBOARD_HELP).toContain('/jarvis hands keyboard practice')
    expect(KEYBOARD_HELP).toContain('press <air|pinch|windows|default>')
    expect(KEYBOARD_HELP).toContain('commit <review|direct|default>')
    expect(KEYBOARD_HELP).toContain('Nothing reaches another window until you tap Insert three times, firmly, with your hand still')
    // It claims no more than the design lets it (SR19).
    expect(KEYBOARD_HELP).not.toMatch(/never|always|cannot be|safe|guarantee|understand/i)
    expect(KEYBOARD_HELP).not.toMatch(/insert (the|it|text)|send (the|it|text)/i)
  })

  test('the subcommand is case-insensitive and unknown ones list the help', async () => {
    const r = rig()
    expect(await r.keyboard.run(['PRACTICE'])).toContain('practice mode')
    const unknown = await r.keyboard.run(['dance'])
    expect(unknown).toBe(`Unknown subcommand "dance" for /jarvis hands keyboard.\n\n${KEYBOARD_HELP}`)
    // An echoed word is clipped.
    expect(await r.keyboard.run(['x'.repeat(100)])).toContain(`"${'x'.repeat(40)}"`)
    expect(await r.keyboard.run(['x'.repeat(100)])).not.toContain('x'.repeat(41))
  })
})

// ---- The tool ----

describe('the hands tool: keyboard, keyboard_practice, keyboard_off', () => {
  test('M6: a tool-opened session is announced by a fixed toast, then it starts in placing like every session', async () => {
    const r = rig()
    const text = await r.keyboard.runTool('keyboard')
    expect(bodies(r)).toEqual([configure(), { action: 'start' }])
    expect(text).toBe(
      "The air keyboard is opening on the user's screen. It does nothing until the user taps each finger the strip names. What they tap goes into a review box on the keyboard; only the user can insert it into a window, and Jarvis cannot read, insert or send it.",
    )
    r.keyboard.onEvent(OPEN)
    expect(r.toasts).toEqual([BY_CLAUDE_AIR])
    // Later events of the same session add no toast.
    r.keyboard.onEvent(event({ ...OPEN, phase: 'warmup' }))
    expect(r.toasts).toEqual([BY_CLAUDE_AIR])
    // The next session the user opens is the user's own.
    r.keyboard.onEvent(event({ state: 'closed', reason: 'fists', discarded: 0 }))
    r.toasts.length = 0
    await r.keyboard.run([])
    r.keyboard.onEvent(OPEN)
    expect(r.toasts).toEqual([OPEN_AIR])
  })

  test('M6: with the pinch method the toast says pinch', async () => {
    const r = rig({ press: 'pinch' })
    await r.keyboard.runTool('keyboard')
    r.keyboard.onEvent(event({ ...OPEN, press: 'pinch' }))
    expect(r.toasts).toEqual([BY_CLAUDE_PINCH])
  })

  test('a tool open the helper refused leaves no flag behind for the next open', async () => {
    const r = rig()
    r.state.answer = body => (body.action === 'start' ? refusal('The desktop is locked or showing a system prompt.') : OK)
    expect(await r.keyboard.runTool('keyboard')).toBe('The desktop is locked or showing a system prompt.')
    r.state.answer = () => OK
    await r.keyboard.run([])
    r.keyboard.onEvent(OPEN)
    expect(r.toasts).toEqual([OPEN_AIR])
  })

  test('keyboard_practice opens practice and says so; its toast is the practice one', async () => {
    const r = rig()
    expect(await r.keyboard.runTool('keyboard_practice')).toBe("The air keyboard practice is opening on the user's screen. Nothing they tap is sent anywhere.")
    expect(bodies(r)).toEqual([configure(), { action: 'practice' }])
    r.keyboard.onEvent(event({ state: 'practice', phase: 'placing', press: 'air', level: 'ok', lang: 'en', private: false }))
    expect(r.toasts).toEqual([OPEN_PRACTICE])
  })

  test('keyboard_off closes it and says the box goes away', async () => {
    const r = rig()
    expect(await r.keyboard.runTool('keyboard_off')).toBe('The air keyboard is closing. Anything in its review box is thrown away; nothing is typed.')
    expect(bodies(r)).toEqual([{ action: 'stop' }])
  })

  test('a windows press says what Jarvis does not guard, to the model too', async () => {
    const r = rig({ press: 'windows' })
    expect(await r.keyboard.runTool('keyboard')).toBe(
      "Windows' own on-screen keyboard is opening on the user's screen. Jarvis' keyboard safeguards do not apply to it, and Jarvis cannot type for the user.",
    )
  })

  test('the tool description says what the model can and cannot do with it', () => {
    const text = HANDS_TOOL.description
    expect(text).toContain(
      "keyboard opens the on-screen air keyboard the user types on with their own fingers, if they turned it on in the plugin settings (Jarvis types nothing itself, cannot insert or send what was tapped, and cannot turn it on); keyboard_practice opens it in practice mode, where nothing is typed; keyboard_off closes it.",
    )
  })
})

// ---- Events: toasts, remembered state, status ----

describe('events: toasts with fixed words and numbers only', () => {
  test('the open toast: air and pinch say what to do, practice says nothing is sent, and a phase change says nothing', () => {
    const r = rig()
    r.keyboard.onEvent(OPEN)
    r.keyboard.onEvent(event({ ...OPEN, phase: 'warmup' }))
    r.keyboard.onEvent(event({ ...OPEN, phase: 'typing', hold: 'yield' }))
    expect(r.toasts).toEqual([OPEN_AIR])
    r.keyboard.onEvent(event({ state: 'closed', reason: 'command', discarded: 0 }))
    r.keyboard.onEvent(event({ ...OPEN, press: 'pinch', level: undefined }))
    expect(r.toasts).toEqual([OPEN_AIR, OPEN_PINCH])
    r.keyboard.onEvent(event({ state: 'closed', reason: 'command', discarded: 0 }))
    r.keyboard.onEvent(event({ state: 'practice', phase: 'placing', press: 'air', level: 'ok', lang: 'en', private: false }))
    expect(r.toasts).toEqual([OPEN_AIR, OPEN_PINCH, OPEN_PRACTICE])
    r.keyboard.onEvent(event({ state: 'closed', reason: 'command', discarded: 0 }))
    r.keyboard.onEvent(event({ ...OPEN, press: 'windows', level: undefined }))
    expect(r.toasts.at(-1)).toBe("Windows' own on-screen keyboard is open. Jarvis' keyboard safeguards do not apply to it.")
  })

  test('M7: every close reason has its fixed words; the pointer line follows except where the design says not', () => {
    const table: [string, string | undefined, boolean][] = [
      ['runaway', 'Air keyboard closed: too many keys at once.', true],
      ['desktop_locked', 'Air keyboard closed: the screen was locked.', true],
      ['idle', 'Air keyboard closed: nobody was using it.', true],
      ['no_overlay', 'Air keyboard closed: it could not be shown.', true],
      ['camera', 'Air keyboard closed: the camera stopped.', false],
      ['error', 'Air keyboard closed after an internal error; hand control carries on.', true],
      ['input_blocked', 'Air keyboard closed: Windows would not take the keys (is the window running as administrator?).', true],
      ['air_unreliable', 'Air keyboard closed: the camera or hand tracking was too unsteady for tapping. If the air tap does not work on this camera, try /jarvis hands keyboard press pinch.', true],
      // These show no toast of their own.
      ['command', undefined, false],
      ['close_key', undefined, true],
      ['fists', undefined, true],
      ['paused', undefined, false],
      ['disabled', undefined, false],
    ]
    for (const [reason, own, hasPointer] of table) {
      const r = rig()
      r.keyboard.onEvent(OPEN)
      r.toasts.length = 0
      r.keyboard.onEvent(event({ state: 'closed', reason: reason as never, discarded: 0 }))
      expect(r.toasts, reason).toEqual(own === undefined ? [] : [`${own}${hasPointer ? POINTER : ''}`])
      // With characters thrown away every reason shows a toast, and the count is last.
      const r2 = rig()
      r2.keyboard.onEvent(OPEN)
      r2.toasts.length = 0
      r2.keyboard.onEvent(event({ state: 'closed', reason: reason as never, discarded: 25 }))
      expect(r2.toasts, `${reason} with a box`).toEqual([`${own ?? 'Air keyboard closed.'}${hasPointer ? POINTER : ''} 25 typed characters were thrown away.`])
    }
  })

  test('one thrown-away character is counted in the singular; a closed event with no reason still counts', () => {
    const r = rig()
    r.keyboard.onEvent(OPEN)
    r.toasts.length = 0
    r.keyboard.onEvent(event({ state: 'closed', reason: 'command', discarded: 1 }))
    expect(r.toasts).toEqual(['Air keyboard closed. 1 typed character was thrown away.'])
    r.keyboard.onEvent(OPEN)
    r.toasts.length = 0
    r.keyboard.onEvent(event({ state: 'closed', discarded: 4 }))
    expect(r.toasts).toEqual(['Air keyboard closed. 4 typed characters were thrown away.'])
  })

  test('a second closed event for the same close says nothing again', () => {
    const r = rig()
    r.keyboard.onEvent(OPEN)
    r.keyboard.onEvent(event({ state: 'closed', reason: 'runaway', discarded: 3 }))
    const count = r.toasts.length
    r.keyboard.onEvent(event({ state: 'closed', reason: 'runaway', discarded: 3 }))
    expect(r.toasts).toHaveLength(count)
  })

  test('M7: the practice result is numbers only, and in the press method\'s own words', () => {
    const air = rig()
    air.keyboard.onEvent(event({ state: 'practice', phase: 'placing', press: 'air', level: 'ok', lang: 'en', private: false }))
    air.toasts.length = 0
    air.keyboard.onEvent(event({ state: 'closed', reason: 'command', discarded: 0, practice: { hitRate: 0.93, phantomsPerMin: 0, recallIM: 0.92 } }))
    expect(air.toasts).toEqual(['Practice done: 93% of keys right, 0 false taps a minute, 92% of index and middle taps seen.'])
    const noRecall = rig()
    noRecall.keyboard.onEvent(event({ state: 'practice', press: 'air' }))
    noRecall.toasts.length = 0
    noRecall.keyboard.onEvent(event({ state: 'closed', reason: 'close_key', discarded: 0, practice: { hitRate: 0.5, phantomsPerMin: 2.34 } }))
    expect(noRecall.toasts).toEqual([`Practice done: 50% of keys right, 2.3 false taps a minute.${POINTER}`])
    const pinch = rig({ press: 'pinch' })
    pinch.keyboard.onEvent(event({ state: 'practice', press: 'pinch' }))
    pinch.toasts.length = 0
    pinch.keyboard.onEvent(event({ state: 'closed', reason: 'command', discarded: 0, practice: { hitRate: 0.876, phantomsPerMin: 1.25 } }))
    expect(pinch.toasts).toEqual(['Practice done: 88% of keys right, 1.3 false presses a minute.'])
    // A practice that ended on a fault says both.
    const fault = rig()
    fault.keyboard.onEvent(event({ state: 'practice', press: 'air' }))
    fault.toasts.length = 0
    fault.keyboard.onEvent(event({ state: 'closed', reason: 'camera', discarded: 0, practice: { hitRate: 0.7, phantomsPerMin: 0.5, recallIM: 0.8 } }))
    expect(fault.toasts).toEqual(['Practice done: 70% of keys right, 0.5 false taps a minute, 80% of index and middle taps seen. Air keyboard closed: the camera stopped.'])
  })

  test('M42: an Insert that stopped says how far it got and why, in fixed words', () => {
    const whys: Record<string, string> = {
      focus: 'the window changed',
      yield: 'you used the keyboard or mouse',
      blocked: 'that window cannot be typed into',
      password: 'that looks like a password box',
      covered: 'the screen is covered',
      overlay: 'the keyboard could not be drawn',
      stopped: 'you stopped it',
      timeout: 'it took too long',
      failed: 'Windows would not take the keys',
    }
    for (const [reason, why] of Object.entries(whys)) {
      const r = rig()
      r.keyboard.onEvent(OPEN)
      r.toasts.length = 0
      r.keyboard.onEvent(parseKeyboardEvent({ ...EXAMPLES[4], review: { state: 'aborted', chars: 25, insert: { kind: 'text', outcome: 'aborted', sent: 12, of: 37, reason }, text: 'typed words' } }) as HandsKeyboardEvent)
      expect(r.toasts, reason).toEqual([`Air keyboard: typed 12 of 37 characters, then stopped (${why}). The rest is still in the box.`])
    }
    expect(Object.keys(whys)).toHaveLength(9)
    // A Send that stopped is not "typed 0 of 1".
    const send = rig()
    send.keyboard.onEvent(OPEN)
    send.toasts.length = 0
    send.keyboard.onEvent(event({ ...OPEN, review: { state: 'composing', chars: 0, insert: { kind: 'enter', outcome: 'aborted', sent: 0, of: 1, reason: 'focus' } } }))
    expect(send.toasts).toEqual(['Air keyboard: Enter was not pressed (the window changed).'])
    // Without a reason it still says what it knows.
    const bare = rig()
    bare.keyboard.onEvent(OPEN)
    bare.toasts.length = 0
    bare.keyboard.onEvent(event({ ...OPEN, review: { state: 'aborted', chars: 5, insert: { kind: 'text', outcome: 'aborted', sent: 2, of: 7 } } }))
    expect(bare.toasts).toEqual(['Air keyboard: typed 2 of 7 characters, then stopped. The rest is still in the box.'])
  })

  test('a completed Insert or Send shows no toast (the overlay says it), nor does a box change', () => {
    const r = rig()
    r.keyboard.onEvent(OPEN)
    r.toasts.length = 0
    r.keyboard.onEvent(event({ ...OPEN, review: { state: 'composing', chars: 0, insert: { kind: 'text', outcome: 'done', sent: 37, of: 37 } } }))
    r.keyboard.onEvent(event({ ...OPEN, review: { state: 'composing', chars: 0, insert: { kind: 'enter', outcome: 'done', sent: 1, of: 1 } } }))
    r.keyboard.onEvent(event({ ...OPEN, review: { state: 'inserting', chars: 20 } }))
    expect(r.toasts).toEqual([])
  })

  test('M7: no toast, status line or reply carries a word the helper added', async () => {
    const r = rig()
    const events = [
      { ...EXAMPLES[0], title: 'Untitled - Notepad', exe: 'notepad.exe', message: 'secret words' },
      { ...EXAMPLES[4], review: { state: 'aborted', chars: 25, insert: { kind: 'text', outcome: 'aborted', sent: 12, of: 37, reason: 'focus', window: 'secret words' }, preview: 'secret words' } },
      { v: 1, type: 'keyboard', state: 'closed', reason: 'fists', discarded: 25, message: 'secret words', text: 'secret words' },
      { v: 1, type: 'keyboard', state: 'closed', reason: 'camera', discarded: 0, practice: { hitRate: 0.5, phantomsPerMin: 1, recallIM: 0.5, note: 'secret words' } },
    ]
    for (const raw of events) {
      const parsed = parseKeyboardEvent(raw)
      if (parsed !== undefined) r.keyboard.onEvent(parsed)
      r.keyboard.onEvent(raw as never) // even an unparsed one handed straight in
    }
    const everything = [...r.toasts, ...r.keyboard.statusLines(), await r.keyboard.run(['status'])].join('\n')
    expect(everything).not.toMatch(/secret|Notepad|notepad|Untitled/)
    expect(r.toasts.length).toBeGreaterThan(2)
  })

  test('M44: the status shows counts and enums only, and nothing when the option is off', () => {
    const r = rig()
    const closeAndOpen = (fields: Omit<HandsKeyboardEvent, 'v' | 'type'>): void => {
      r.keyboard.onEvent(event({ state: 'closed', reason: 'command', discarded: 0 }))
      r.keyboard.onEvent(event(fields))
    }
    expect(r.keyboard.statusLines()).toEqual([
      'Air keyboard: on. /jarvis hands keyboard opens it; /jarvis hands keyboard practice tries it first; /jarvis hands keyboard status shows its settings.',
    ])
    r.keyboard.onEvent(OPEN)
    expect(r.keyboard.statusLines()).toEqual(['Air keyboard: open (finding your hands).'])
    r.keyboard.onEvent(event({ ...OPEN, phase: 'typing', review: { state: 'composing', chars: 37 } }))
    expect(r.keyboard.statusLines()).toEqual(['Air keyboard: open (typing).', 'Air keyboard review box: 37 characters waiting.'])
    r.keyboard.onEvent(event({ ...OPEN, phase: 'typing', review: { state: 'composing', chars: 1 } }))
    expect(r.keyboard.statusLines().at(-1)).toBe('Air keyboard review box: 1 character waiting.')
    // An event of the same session that names no review leaves the last count standing.
    r.keyboard.onEvent(event({ ...OPEN, phase: 'typing', hold: 'yield' }))
    expect(r.keyboard.statusLines().at(-1)).toBe('Air keyboard review box: 1 character waiting.')
    // The air tap's level, when it is not ok.
    r.keyboard.onEvent(event({ ...OPEN, phase: 'typing', level: 'degraded', review: { state: 'composing', chars: 0 } }))
    expect(r.keyboard.statusLines()).toEqual([
      'Air keyboard: open (typing).',
      'Air keyboard: the air tap is less sure right now; tap a little firmer.',
      'Air keyboard review box: 0 characters waiting.',
    ])
    // Off is reported once, together with the pinch method it fell back to.
    r.keyboard.onEvent(event({ ...OPEN, phase: 'warmup', level: 'off', press: 'pinch', review: { state: 'composing', chars: 12 } }))
    expect(r.keyboard.statusLines()).toEqual([
      'Air keyboard: open (warm-up).',
      'Air keyboard: the air tap is off (the camera or hand tracking is too unsteady); it switched to pinch.',
      'Air keyboard review box: 12 characters waiting.',
    ])
    // A new session starts with no count; a pinch session has no level.
    closeAndOpen({ ...OPEN, press: 'pinch', level: undefined, commit: 'direct' })
    expect(r.keyboard.statusLines()).toEqual(['Air keyboard: open (finding your hands).'])
    closeAndOpen({ state: 'practice', phase: 'warmup', press: 'air', level: 'ok' })
    expect(r.keyboard.statusLines()).toEqual(['Air keyboard: practice (warm-up). Nothing is typed anywhere.'])
    r.keyboard.onEvent(event({ state: 'closed', reason: 'command', discarded: 0 }))
    expect(r.keyboard.statusLines()).toHaveLength(1)
    expect(r.keyboard.statusLines()[0]).toMatch(/^Air keyboard: on\./)
    // reset() is the helper restarting: what was remembered is gone.
    r.keyboard.onEvent(event({ ...OPEN, review: { state: 'composing', chars: 3 } }))
    r.keyboard.reset()
    expect(r.keyboard.statusLines()).toHaveLength(1)
    expect(r.keyboard.statusLines()[0]).toMatch(/^Air keyboard: on\./)
    r.state.keyboard = false
    expect(r.keyboard.statusLines()).toEqual([])
  })

  test('the text table is fixed strings with only number placeholders', () => {
    for (const [key, text] of Object.entries(KEYBOARD_TEXT)) {
      expect(typeof text, key).toBe('string')
      const placeholders = text.match(/\{[a-zA-Z]+\}/g) ?? []
      for (const placeholder of placeholders) expect(['{discarded}', '{sent}', '{of}', '{why}', '{hitRate}', '{phantomsPerMin}', '{recallIM}', '{chars}']).toContain(placeholder)
    }
    expect(KEYBOARD_TEXT.off).toBe(OFF)
    expect(KEYBOARD_TEXT.tooOld).toBe(TOO_OLD)
  })
})

// ---- The mod: hands.ts wires it (E1-E10) ----

/** Hand control turned on (as /jarvis hands on stores it) and installed by this version. */
function handsWorld(on: On): World {
  const w = world(on)
  w.store.set('handsEnabled', { isOn: true, setting: false })
  w.existing.add(HANDS_PYTHON)
  w.files.set(HANDS_INSTALLED, JSON.stringify({ pluginVersion: PLUGIN_VERSION }))
  return w
}

const handsSent = (w: World): string[] => w.commands.filter(command => command.url.includes(`:${HANDS_PORT}/`)).map(command => command.name)
const keyboardSent = (w: World): Record<string, unknown>[] => w.handsNamed('keyboard').map(command => command.body)

/** The voice helper up, then the hand helper up to ready, with the given hello capabilities. */
async function handsUp($: TestEngine, w: World, capabilities: string[] = KEYBOARD_CAPS): Promise<FakeChild> {
  await startHelper($, w)
  const hands = w.lastHands()
  hands.hello(HANDS_PORT, capabilities)
  hands.event({ type: 'state', state: 'starting' })
  hands.event({ type: 'ready', camera: 'UGREEN Camera', width: 1280, height: 720, fps: 30, displays: [] })
  hands.event({ type: 'state', state: 'idle' })
  await w.settle()
  return hands
}

describe('hands.ts wires the keyboard', () => {
  test('M5: with the option off (the default) the commands after hello are today\'s: config and nothing else', async ($, on) => {
    const w = handsWorld(on)
    await handsUp($, w)
    // Option off (the default): the command sequence is exactly today's.
    expect(handsSent(w)).toEqual(['config'])
    expect(keyboardSent(w)).toEqual([])
  })

  test('E9: the option on sends one configure after config, and after pause when the user paused', { options: { handKeyboard: 'on' } }, async ($, on) => {
    const w = handsWorld(on)
    await handsUp($, w)
    expect(handsSent(w)).toEqual(['config', 'keyboard'])
    expect(keyboardSent(w)).toEqual([configure()])
    // A restarted helper hears it again (reset forgot the old one), and after pause and config.
    w.store.set('handsPaused', true)
    w.lastHands().exit(1)
    await w.settle()
    await w.clock.advance(BACKOFF_MS[0] ?? 1000)
    await w.settle()
    const before = handsSent(w).length
    w.lastHands().hello(HANDS_PORT, KEYBOARD_CAPS)
    await w.settle()
    expect(handsSent(w).slice(before)).toEqual(['pause', 'config', 'keyboard'])
  })

  test('M5: the option on with an older helper (hello without keyboard) sends no keyboard command, and says to update', { options: { handKeyboard: 'on' } }, async ($, on) => {
    const w = handsWorld(on)
    await handsUp($, w, ['ptt'])
    expect(handsSent(w)).toEqual(['config'])
    expect(await jarvis($, 'hands keyboard')).toBe(TOO_OLD)
    expect(await jarvis($, 'hands keyboard practice')).toBe(TOO_OLD)
    expect(handsSent(w)).toEqual(['config'])
  })

  test('M1: with the option off nothing opens and nothing is sent, from the command or the tool', async ($, on) => {
    const w = handsWorld(on)
    await handsUp($, w)
    await allowedTurn($, w, HANDS)
    const before = handsSent(w)
    expect(await jarvis($, 'hands keyboard')).toBe(OFF)
    expect(await jarvis($, 'hands keyboard practice')).toBe(OFF)
    expect((await $.tool.call({ tool: HANDS, action: 'keyboard' })).result).toBe(OFF)
    expect((await $.tool.call({ tool: HANDS, action: 'keyboard_practice' })).result).toBe(OFF)
    expect(handsSent(w)).toEqual(before)
    // Closing always goes through, by the command and by the tool.
    expect(await jarvis($, 'hands keyboard off')).toContain('Closing the air keyboard')
    expect((await $.tool.call({ tool: HANDS, action: 'keyboard_off' })).result).toContain('closing')
    expect(w.handsNamed('keyboard').map(command => command.body)).toEqual([{ action: 'stop' }, { action: 'stop' }])
  })

  test('E10, M6: /jarvis hands keyboard opens it; the helper\'s events become the toasts, and the box count is in the status', { options: { handKeyboard: 'on' } }, async ($, on) => {
    const w = handsWorld(on)
    const hands = await handsUp($, w)
    expect(await jarvis($, 'hands keyboard')).toContain('Opening the air keyboard.')
    expect(keyboardSent(w)).toEqual([configure(), configure(), { action: 'start' }])
    hands.event({ type: 'keyboard', state: 'open', phase: 'placing', press: 'air', level: 'ok', commit: 'review', lang: 'en', private: false })
    await w.settle()
    expect(w.toasts).toContain(OPEN_AIR)
    hands.event({ type: 'keyboard', state: 'open', phase: 'typing', press: 'air', level: 'ok', commit: 'review', lang: 'en', private: false, review: { state: 'composing', chars: 12, text: 'must not show' } })
    await w.settle()
    const status = await jarvis($, 'hands')
    expect(status).toContain('Air keyboard: open (typing).\nAir keyboard review box: 12 characters waiting.')
    expect(status).not.toContain('must not show')
    hands.event({ type: 'keyboard', state: 'closed', reason: 'fists', discarded: 12 })
    await w.settle()
    expect(w.toasts).toContain(`Air keyboard closed.${POINTER} 12 typed characters were thrown away.`)
    expect(w.toasts.join('\n')).not.toContain('must not show')
  })

  test('E9: the status lines are absent with the option off, so today\'s status is unchanged', async ($, on) => {
    const w = handsWorld(on)
    await handsUp($, w)
    const status = await jarvis($, 'hands')
    expect(status).not.toMatch(/Air keyboard:/)
    expect(status).toContain('/jarvis hands keyboard [off|practice|recenter|private]  type in the air on an on-screen keyboard (tap your fingers over the keys)')
  })

  test('E7: the gesture list names the keyboard when it is on', { options: { handKeyboard: 'on' } }, async ($, on) => {
    const w = handsWorld(on)
    await handsUp($, w)
    const help = await jarvis($, 'hands help')
    expect(help).toContain('Air keyboard: tap a finger in the air over a key to type it; both fists held for a second close it')
    expect(help).toContain('/jarvis hands keyboard [off|practice|recenter|private]')
  })

  test('E9: a helper that restarts forgets the open keyboard', { options: { handKeyboard: 'on' } }, async ($, on) => {
    const w = handsWorld(on)
    const hands = await handsUp($, w)
    hands.event({ type: 'keyboard', state: 'open', phase: 'typing', press: 'air', level: 'ok', commit: 'review', lang: 'en', private: false, review: { state: 'composing', chars: 4 } })
    await w.settle()
    expect(await jarvis($, 'hands')).toContain('review box: 4 characters')
    hands.exit(1)
    await w.settle()
    expect(await jarvis($, 'hands')).not.toContain('review box')
  })

  test('private, public and recenter reach a keyboard only while the helper says it is open', { options: { handKeyboard: 'on' } }, async ($, on) => {
    const w = handsWorld(on)
    const hands = await handsUp($, w)
    const sent = keyboardSent(w).length
    expect(await jarvis($, 'hands keyboard private')).toBe('The air keyboard is not open. Open it first, then use private.')
    expect(keyboardSent(w)).toHaveLength(sent)
    hands.event({ type: 'keyboard', state: 'open', phase: 'placing', press: 'air', level: 'ok', commit: 'review', lang: 'en', private: false })
    await w.settle()
    expect(await jarvis($, 'hands keyboard private')).toBe('Air keyboard private mode is on: the box and the key highlights are hidden on screen.')
    expect(keyboardSent(w).at(-1)).toEqual({ action: 'private' })
    hands.event({ type: 'keyboard', state: 'closed', reason: 'fists', discarded: 0 })
    await w.settle()
    const after = keyboardSent(w).length
    expect(await jarvis($, 'hands keyboard recenter')).toBe('The air keyboard is not open. Open it first, then use recenter.')
    expect(keyboardSent(w)).toHaveLength(after)
  })

  test('E8, M6: the tool opens the keyboard for the user, announces it, and cannot do more', { options: { handKeyboard: 'on' } }, async ($, on) => {
    const w = handsWorld(on)
    const hands = await handsUp($, w)
    await allowedTurn($, w, HANDS)
    const opened = await $.tool.call({ tool: HANDS, action: 'keyboard' })
    expect(opened.result).toContain("The air keyboard is opening on the user's screen.")
    expect(keyboardSent(w).at(-1)).toEqual({ action: 'start' })
    hands.event({ type: 'keyboard', state: 'open', phase: 'placing', press: 'air', level: 'ok', commit: 'review', lang: 'en', private: false })
    await w.settle()
    expect(w.toasts).toContain(BY_CLAUDE_AIR)
    expect(w.toasts).not.toContain(OPEN_AIR)
    const practice = await $.tool.call({ tool: HANDS, action: 'keyboard_practice' })
    expect(practice.result).toContain('practice is opening')
    expect(keyboardSent(w).at(-1)).toEqual({ action: 'practice' })
    const closed = await $.tool.call({ tool: HANDS, action: 'keyboard_off' })
    expect(closed.result).toContain('closing')
    expect(keyboardSent(w).at(-1)).toEqual({ action: 'stop' })
    // Nothing the tool takes makes text: an extra argument is no text channel.
    const sentBefore = w.commands.length
    await $.tool.call({ tool: HANDS, action: 'keyboard', text: 'rm -rf', keys: 'abc' })
    for (const command of w.commands.slice(sentBefore)) expect(JSON.stringify(command.body)).not.toMatch(/rm -rf|abc/)
  })

  test('E10: with hand control off the keyboard says so and sends nothing', { options: { handKeyboard: 'on' } }, async ($, on) => {
    const w = world(on)
    w.existing.add(HANDS_PYTHON)
    await startHelper($, w)
    expect(await jarvis($, 'hands keyboard')).toBe('Hand control is off. Turn it on with /jarvis hands on.')
    expect(handsSent(w)).toEqual([])
  })

  test('E10: in a cloud session the keyboard is not local either', { options: { handKeyboard: 'on' } }, async ($, on) => {
    const w = world(on, { env: { HOME: '/root', CLAUDE_CODE_REMOTE: 'true' } })
    await startSession($, w)
    expect(await jarvis($, 'hands keyboard')).toBe(
      'Hand control runs on your own computer; this session runs in the cloud, so the hand helper is not started here.',
    )
  })
})
