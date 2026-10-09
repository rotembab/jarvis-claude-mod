import { describe, expect, test } from 'claude-code/testing'
import type { On } from 'claude-code'
import type { Engine as TestEngine } from 'claude-code/testing'

import type { JarvisHud } from '../types'
import { KNOBS, PRESET_NAMES, toolTuning } from './hands'
import type { RuleVerdict } from './pc'
import type { FakeChild, World } from './test-harness'
import {
  completeTurn,
  HANDS_INSTALLED,
  HANDS_PORT,
  HANDS_PYTHON,
  PLUGIN_VERSION,
  promptTurn,
  startHelper,
  startSession,
  world,
} from './test-harness'

const HANDS = 'mcp__jarvis__hands'
const ALLOWED = (): RuleVerdict => ({ decision: 'allow', rule: HANDS })
const PLAN_MODE = /^Plan mode is on, so Jarvis does not change hand control now\./
const MODE_UNKNOWN = /^Jarvis cannot tell yet whether plan mode is on/
const REFUSED = /^The user's permission settings do not let the hands tool /
const DONT = /^The user chose "Don't do it"/
const ON_QUESTION = 'Jarvis: Claude wants to use the hands tool to turn hand control on, which opens the camera. Do it?'
const ENGAGE_QUESTION = 'Jarvis: Claude wants to use the hands tool to give your hand the cursor now, with no open palm needed. Do it?'
const TURNED_ON = 'Hand control is on. The camera starts in a moment'
const TUNING_KEY = 'handsTuning'
/** The questions Jarvis asks about a sensitivity call (the tool's text is what it asks about, not the model's words). */
const ask = (what: string): string => `Jarvis: Claude wants to use the hands tool to ${what}. Do it?`

/** A model call of the hands tool, with its raw arguments. */
function hands($: TestEngine, args: Record<string, unknown>) {
  return $.tool.call({ tool: HANDS, ...args })
}

/** Hand control installed by this version, and turned on (as /jarvis hands on stores it) when `isOn`. */
function handsWorld(on: On, isOn: boolean): World {
  const w = world(on)
  if (isOn) w.store.set('handsEnabled', { isOn: true, setting: false })
  w.existing.add(HANDS_PYTHON)
  w.files.set(HANDS_INSTALLED, JSON.stringify({ pluginVersion: PLUGIN_VERSION }))
  return w
}

/** The voice helper up, and hand control's helper up to ready and idle. */
async function handsUp($: TestEngine, w: World): Promise<void> {
  await startHelper($, w)
  const helper = w.lastHands()
  helper.hello(HANDS_PORT)
  helper.event({ type: 'state', state: 'starting' })
  helper.event({ type: 'ready', camera: 'UGREEN Camera', width: 1280, height: 720, fps: 30, displays: [] })
  helper.event({ type: 'state', state: 'idle' })
  await w.settle()
}

/** The commands sent to the hand helper that change something (not its status reads), from the `from`th on. */
const handsChanges = (w: World, from: number): string[] =>
  w.commands
    .filter(command => command.url.includes(`:${HANDS_PORT}/`) && command.name !== 'status')
    .slice(from)
    .map(command => command.name)

describe('the hands tool: the user\'s rules', () => {
  test('a deny rule refuses it: the camera stays off and nothing is asked', async ($, on) => {
    const w = handsWorld(on, false)
    await startHelper($, w)
    await promptTurn($)
    w.toolCheck = () => ({ decision: 'deny', reason: 'Permission rule denies', rule: HANDS })
    w.askAnswer = 'Do it'
    expect(await hands($, { action: 'on' })).toEqual({
      deny: "The user's permission settings do not let the hands tool turn hand control on here (Permission rule denies).",
    })
    expect(await hands($, { action: 'status' })).toEqual({ deny: expect.stringMatching(REFUSED) })
    await w.settle()
    expect(w.checks.at(0)).toEqual({ tool: HANDS, input: { action: 'on' } })
    expect(w.asked).toEqual([])
    expect(w.handsHelpers()).toHaveLength(0)
    expect(w.store.has('handsEnabled')).toBe(false)
  })

  test("the user's own allow rule runs it with no question", async ($, on) => {
    const w = handsWorld(on, false)
    await startHelper($, w)
    w.toolCheck = ALLOWED
    await promptTurn($)
    expect((await hands($, { action: 'on' })).result).toContain(TURNED_ON)
    await w.settle()
    expect(w.asked).toEqual([])
    expect(w.handsHelpers()).toHaveLength(1)
    expect(w.checks).toEqual([{ tool: HANDS, input: { action: 'on' } }])
  })

  test("an allow from the mode alone (bypassPermissions, no rule) asks on screen, as does the engine's plain ask", async ($, on) => {
    const w = handsWorld(on, false)
    await startHelper($, w)
    await promptTurn($, 'bypassPermissions')
    w.toolCheck = () => ({ decision: 'allow' })
    w.askAnswer = "Don't do it"
    expect(await hands($, { action: 'on' })).toMatchObject({ result: expect.stringMatching(DONT) })
    expect(w.asked).toEqual([ON_QUESTION])
    expect(w.dialogs.at(-1)?.options).toEqual(["Don't do it", 'Do it'])
    await w.settle()
    expect(w.handsHelpers()).toHaveLength(0)
    // The engine's own default for a tool no rule allows yet: a plain ask, which an unanswered question leaves a no.
    await promptTurn($)
    w.toolCheck = () => ({ decision: 'ask' })
    w.askAnswer = undefined
    expect(await hands($, { action: 'on' })).toMatchObject({ result: 'Jarvis could not ask the user on screen, so nothing was done.' })
    await w.settle()
    expect(w.handsHelpers()).toHaveLength(0)
    w.askAnswer = 'Do it'
    expect((await hands($, { action: 'on' })).result).toContain(TURNED_ON)
    await w.settle()
    expect(w.handsHelpers()).toHaveLength(1)
    expect(w.asked).toEqual([ON_QUESTION, ON_QUESTION, ON_QUESTION])
  })

  test('an ask rule asks before the cursor is taken; a subagent is asked even with an allow rule', async ($, on) => {
    const w = handsWorld(on, true)
    await handsUp($, w)
    const sent = handsChanges(w, 0).length
    await promptTurn($)
    w.toolCheck = () => ({ decision: 'ask', reason: 'Permission rule asks', rule: HANDS })
    w.askAnswer = "Don't do it"
    expect(await hands($, { action: 'engage' })).toMatchObject({ result: expect.stringMatching(DONT) })
    w.toolCheck = ALLOWED
    expect(await hands($, { action: 'engage', agentId: 'agent-1' })).toMatchObject({ result: expect.stringMatching(DONT) })
    expect(w.asked).toEqual([ENGAGE_QUESTION, ENGAGE_QUESTION])
    expect(handsChanges(w, sent)).toEqual([])
    // The main loop, the mode known for this turn: the user's allow rule runs it.
    expect((await hands($, { action: 'engage' })).result).toBe('Hand control has the cursor: it follows your hand as soon as one is in view.')
    expect(handsChanges(w, sent)).toEqual(['engage'])
    expect(w.asked).toHaveLength(2)
    // A display choice is named as hand control reads it, before the action.
    w.toolCheck = () => ({ decision: 'ask' })
    expect(await hands($, { action: 'disengage', display: '2 1' })).toMatchObject({ result: expect.stringMatching(DONT) })
    expect(await hands($, { display: 'all' })).toMatchObject({ result: expect.stringMatching(DONT) })
    expect(w.asked.slice(2)).toEqual([
      'Jarvis: Claude wants to use the hands tool to make hand control reach displays 2 and 1, then take the cursor away from your hand. Do it?',
      'Jarvis: Claude wants to use the hands tool to make hand control reach all displays. Do it?',
    ])
    expect(handsChanges(w, sent)).toEqual(['engage'])
  })

  test('dontAsk refuses what the rules do not allow beforehand, with no question', async ($, on) => {
    const w = handsWorld(on, false)
    await startHelper($, w)
    await promptTurn($, 'dontAsk')
    w.askAnswer = 'Do it'
    expect(await hands($, { action: 'on' })).toEqual({ deny: expect.stringMatching(REFUSED) })
    w.toolCheck = () => ({ decision: 'allow' })
    expect(await hands($, { action: 'on' })).toEqual({ deny: expect.stringMatching(REFUSED) })
    await w.settle()
    expect(w.asked).toEqual([])
    expect(w.handsHelpers()).toHaveLength(0)
    // Its own allow rule still runs it.
    w.toolCheck = ALLOWED
    expect((await hands($, { action: 'on' })).result).toContain(TURNED_ON)
  })

  test('a check that fails refuses, and so do settings that cannot be read; a settings hook that could match asks', async ($, on) => {
    const w = handsWorld(on, false)
    await startHelper($, w)
    await promptTurn($)
    w.askAnswer = 'Do it'
    w.toolCheck = () => {
      throw new Error('rules unreadable')
    }
    expect(await hands($, { action: 'on' })).toEqual({ deny: 'Jarvis could not read your permission rules, so nothing was done.' })
    w.toolCheck = ALLOWED
    w.settingsError = 'unreadable'
    expect(await hands($, { action: 'on' })).toEqual({ deny: 'Jarvis could not read the hooks in your settings, so nothing was done.' })
    expect(w.asked).toEqual([])
    w.settingsError = undefined
    w.settings = { user: { env: { ANTHROPIC_API_KEY: 'sk-not-real' }, hooks: { PreToolUse: [{ matcher: 'mcp__.*', hooks: [{ type: 'command', command: 'audit.sh' }] }] } } }
    w.askAnswer = "Don't do it"
    expect(await hands($, { action: 'on' })).toMatchObject({ result: expect.stringMatching(DONT) })
    expect(w.asked).toEqual([ON_QUESTION])
    await w.settle()
    expect(w.handsHelpers()).toHaveLength(0)
    expect(w.logs.some(line => line.includes('sk-not-real') || line.includes('audit.sh'))).toBe(false)
  })

  test('arguments it cannot use are answered with what to fix; nothing is asked or done', async ($, on) => {
    const w = handsWorld(on, false)
    await startHelper($, w)
    await promptTurn($)
    w.askAnswer = 'Do it'
    expect((await hands($, { action: 'explode' })).result).toBe(
      'Unknown action "explode"; use one of on, off, status, calibrate, pause, resume, engage, disengage, keyboard, keyboard_practice, keyboard_off.',
    )
    expect((await hands($, {})).result).toBe('Give an action, a display, or both.')
    expect((await hands($, { display: 'left' })).result).toBe('"left" is not a display. Use /jarvis hands display all, a display number such as 2, or a list such as 1,2.')
    expect(w.asked).toEqual([])
    expect(w.checks).toEqual([])
    expect(w.handsHelpers()).toHaveLength(0)
  })

  test("its calls are on the HUD's action log, a refused one as failed", async ($, on) => {
    const w = handsWorld(on, false)
    await startHelper($, w)
    w.toolCheck = ALLOWED
    await promptTurn($)
    expect((await hands($, { action: 'status' })).result).toMatch(/^Hand control is off\./)
    w.toolCheck = () => ({ decision: 'deny', rule: HANDS })
    expect(await hands($, { action: 'on' })).toEqual({ deny: expect.stringMatching(REFUSED) })
    await w.settle()
    const hud = w.state.get('jarvis.hud') as JarvisHud
    expect(hud.actions.map(action => [action.label, action.status])).toEqual([
      [HANDS, 'failed'],
      [HANDS, 'done'],
    ])
  })
})

describe('the hands tool: plan mode', () => {
  test("plan mode refuses turning it on, even with the user's allow rule; status still runs", async ($, on) => {
    const w = handsWorld(on, false)
    await startHelper($, w)
    w.toolCheck = ALLOWED
    await promptTurn($, 'plan')
    expect(await hands($, { action: 'on' })).toMatchObject({ result: expect.stringMatching(PLAN_MODE) })
    expect((await hands($, { action: 'status' })).result).toMatch(/^Hand control is off\./)
    await w.settle()
    expect(w.handsHelpers()).toHaveLength(0)
    expect(w.store.has('handsEnabled')).toBe(false)
    expect(w.asked).toEqual([])
  })

  test('plan mode refuses every change: the cursor, the displays, off, pause; status reads', async ($, on) => {
    const w = handsWorld(on, true)
    await handsUp($, w)
    const sent = handsChanges(w, 0).length
    w.toolCheck = ALLOWED
    await promptTurn($, 'plan')
    for (const args of [{ action: 'engage' }, { action: 'disengage' }, { display: '2' }, { action: 'off' }, { action: 'pause' }, { action: 'calibrate' }]) {
      expect(await hands($, args), JSON.stringify(args)).toMatchObject({ result: expect.stringMatching(PLAN_MODE) })
    }
    expect((await hands($, { action: 'status' })).result).toMatch(/^Hand control: ready/)
    expect(handsChanges(w, sent)).toEqual([])
    expect(w.store.get('handsEnabled')).toEqual({ isOn: true, setting: false })
    // An approved plan leaves plan mode mid-turn.
    await $.classic.PostToolUse({ tool_name: 'ExitPlanMode', tool_input: {}, tool_response: {}, tool_use_id: 'toolu_1', permission_mode: 'plan' })
    expect((await hands($, { action: 'engage' })).result).toBe('Hand control has the cursor: it follows your hand as soon as one is in view.')
    expect(handsChanges(w, sent)).toEqual(['engage'])
  })

  test('a mode not known yet changes nothing; status still runs', async ($, on) => {
    const w = handsWorld(on, false)
    await startSession($, w)
    w.toolCheck = ALLOWED
    expect(await hands($, { action: 'on' })).toMatchObject({ result: expect.stringMatching(MODE_UNKNOWN) })
    expect((await hands($, { action: 'status' })).result).toMatch(/^Hand control is off\./)
    await w.settle()
    expect(w.handsHelpers()).toHaveLength(0)
  })

  test('a turn begun with no prompt asks before a change, even with an allow rule', async ($, on) => {
    const w = handsWorld(on, false)
    await startHelper($, w)
    w.toolCheck = ALLOWED
    const turn = await promptTurn($)
    await completeTurn($, turn)
    await $.turn.start({ text: '', turnId: 't-continue' })
    w.askAnswer = "Don't do it"
    expect(await hands($, { action: 'on' })).toMatchObject({ result: expect.stringMatching(DONT) })
    expect(w.asked).toEqual([ON_QUESTION])
  })
})

describe('the hands tool: a voice turn', () => {
  test('turning it on is held for a spoken yes to the question Jarvis asks', async ($, on) => {
    const w = handsWorld(on, false)
    const helper = await startHelper($, w)
    helper.event({ type: 'utterance', id: 'u1', text: 'Jarvis, turn on hand control', source: 'wake', durationMs: 900, language: 'en' })
    await w.settle()
    await $.classic.UserPromptSubmit({ prompt: 'Jarvis, turn on hand control', permission_mode: 'default' })
    await $.turn.start({ text: 'Jarvis, turn on hand control', turnId: 't1' })
    expect(await hands($, { action: 'on' })).toMatchObject({
      result: expect.stringMatching(/^Jarvis held this: it uses the hands tool to turn hand control on and needs the user's spoken OK\./),
    })
    await completeTurn($, 't1')
    await w.settle()
    expect(w.named('speak').map(command => command.body.text).join(' ')).toContain('Claude wants to turn hand control on, which opens the camera. Say yes to let it, sir.')
    expect(w.handsHelpers()).toHaveLength(0)
    helper.event({ type: 'speech_done', replyId: 't1', interrupted: false, spokenText: '', endedAtMs: 40_000 })
    helper.event({ type: 'utterance', id: 'u2', text: 'Go ahead.', source: 'wake', durationMs: 600, language: 'en', startedAtMs: 40_500, overSpeech: false })
    await w.settle()
    await $.classic.UserPromptSubmit({ prompt: 'Go ahead.', permission_mode: 'default' })
    await $.turn.start({ text: 'Go ahead.', turnId: 't2' })
    expect((await hands($, { action: 'on' })).result).toContain(TURNED_ON)
    await w.settle()
    expect(w.handsHelpers()).toHaveLength(1)
    expect(w.asked).toEqual([])
  })
})

describe('the hands tool: sensitivity', () => {
  const SPEED = 'Cursor speed is now 1.5 (was 1; default 1, range 0.7 to 3).'

  /** The next prompt's turn, the one before it done: a prompt typed while a turn runs has its mode unknown, so it would ask. */
  async function nextTurn($: TestEngine, turn: string, mode = 'default'): Promise<string> {
    await completeTurn($, turn)
    return await promptTurn($, mode)
  }

  test('a change asks first, naming the setting and its new value; a no changes nothing', async ($, on) => {
    const w = handsWorld(on, true)
    await handsUp($, w)
    const sent = handsChanges(w, 0).length
    await promptTurn($)
    w.toolCheck = () => ({ decision: 'ask' })
    w.askAnswer = "Don't do it"
    const calls: [Record<string, unknown>, string][] = [
      [{ setting: 'speed', value: 1.5 }, 'set the hand control cursor speed setting to 1.5'],
      [{ setting: 'Palm hold time', value: '1.5' }, 'set the hand control palm hold time setting to 1.5 seconds'],
      [{ setting: 'dead-zone', value: 3 }, 'set the hand control dead zone setting to 3 pixels'],
      [{ setting: 'dead-zone', value: 'Default' }, 'put the hand control dead zone setting back to its default'],
      [{ preset: 'Fast' }, 'apply the fast hand control preset, which sets every sensitivity setting'],
      [{ preset: 'default' }, 'apply the balanced hand control preset, which sets every sensitivity setting'],
      [
        { display: '2', setting: 'pinch', value: 1.1, action: 'status' },
        "make hand control reach display 2, then set the hand control pinch sensitivity setting to 1.1, then read hand control's status",
      ],
      [{ preset: 'precise', action: 'engage' }, 'apply the precise hand control preset, which sets every sensitivity setting, then give your hand the cursor now, with no open palm needed'],
    ]
    for (const [args] of calls) expect(await hands($, args), JSON.stringify(args)).toMatchObject({ result: expect.stringMatching(DONT) })
    expect(w.asked).toEqual(calls.map(([, what]) => ask(what)))
    await w.settle()
    expect(handsChanges(w, sent)).toEqual([])
    expect(w.store.has(TUNING_KEY)).toBe(false)
    expect(w.store.get('handsEnabled')).toEqual({ isOn: true, setting: false })
    // The same call, answered yes, is done.
    w.askAnswer = 'Do it'
    expect((await hands($, { setting: 'speed', value: 1.5 })).result).toBe(SPEED)
    expect(handsChanges(w, sent)).toEqual(['config'])
    expect(w.store.get(TUNING_KEY)).toEqual({ cursorSpeed: { value: 1.5, setting: 1 } })
  })

  test('every setting in the table is asked about by its label, its value and its unit in words', async ($, on) => {
    const w = handsWorld(on, false)
    await startHelper($, w)
    await promptTurn($)
    w.toolCheck = () => ({ decision: 'ask' })
    w.askAnswer = "Don't do it"
    const units: Record<string, string> = { s: ' seconds', px: ' pixels' }
    for (const knob of KNOBS) {
      // A new unit needs its words in hands-gate.ts (UNIT_WORDS) and here.
      expect(knob.unit === undefined || knob.unit in units, `the unit of ${knob.name}`).toBe(true)
      await hands($, { setting: knob.name, value: knob.max })
    }
    expect(w.asked).toEqual(KNOBS.map(knob => ask(`set the hand control ${knob.label.toLowerCase()} setting to ${knob.max}${units[knob.unit ?? ''] ?? ''}`)))
    expect(w.store.has(TUNING_KEY)).toBe(false)
  })

  test("a read asks like the status does; the user's own allow rule runs both with no question", async ($, on) => {
    const w = handsWorld(on, true)
    await handsUp($, w)
    await promptTurn($)
    w.toolCheck = () => ({ decision: 'ask' })
    w.askAnswer = "Don't do it"
    expect(await hands($, { setting: 'speed' })).toMatchObject({ result: expect.stringMatching(DONT) })
    expect(await hands($, { action: 'status', setting: 'speed' })).toMatchObject({ result: expect.stringMatching(DONT) })
    expect(w.asked).toEqual([ask('read the hand control cursor speed setting'), ask("read the hand control cursor speed setting, then read hand control's status")])
    w.toolCheck = ALLOWED
    expect((await hands($, { setting: 'speed' })).result).toMatch(/^Cursor speed is 1 \(default 1, range 0\.7 to 3\)\./)
    expect(w.asked).toHaveLength(2)
  })

  test("the user's own allow rule runs a change with no question; a subagent, a mode from bypassPermissions alone and a hook that could match still ask", async ($, on) => {
    const w = handsWorld(on, true)
    await handsUp($, w)
    const sent = handsChanges(w, 0).length
    let turn = await promptTurn($)
    w.toolCheck = ALLOWED
    expect((await hands($, { setting: 'speed', value: 1.5 })).result).toBe(SPEED)
    expect(await hands($, { preset: 'balanced' })).toMatchObject({ result: 'Preset balanced: every setting is at its default.' })
    expect(w.asked).toEqual([])
    expect(w.checks.at(0)).toEqual({ tool: HANDS, input: { setting: 'speed', value: 1.5 } })
    expect(handsChanges(w, sent)).toEqual(['config', 'config'])
    // A subagent's own mode is never known: it is asked, even with the allow rule.
    w.askAnswer = "Don't do it"
    expect(await hands($, { setting: 'speed', value: 2, agentId: 'agent-1' })).toMatchObject({ result: expect.stringMatching(DONT) })
    expect(await hands($, { preset: 'fast', agentId: 'agent-1' })).toMatchObject({ result: expect.stringMatching(DONT) })
    expect(w.asked).toEqual([
      ask('set the hand control cursor speed setting to 2'),
      ask('apply the fast hand control preset, which sets every sensitivity setting'),
    ])
    // An allow from the mode alone is no rule of the user's.
    turn = await nextTurn($, turn, 'bypassPermissions')
    w.toolCheck = () => ({ decision: 'allow' })
    expect(await hands($, { setting: 'speed', value: 2 })).toMatchObject({ result: expect.stringMatching(DONT) })
    // A settings hook that could match the tool.
    turn = await nextTurn($, turn)
    w.toolCheck = ALLOWED
    w.settings = { user: { hooks: { PreToolUse: [{ matcher: 'mcp__.*', hooks: [{ type: 'command', command: 'audit.sh' }] }] } } }
    expect(await hands($, { setting: 'speed', value: 2 })).toMatchObject({ result: expect.stringMatching(DONT) })
    expect(w.asked).toHaveLength(4)
    expect(w.store.get(TUNING_KEY)).toBeUndefined()
    // With no hook, the same turn's allow rule runs it again.
    w.settings = {}
    expect((await hands($, { setting: 'speed', value: 1.5 })).result).toBe(SPEED)
    expect(w.asked).toHaveLength(4)
  })

  test('a deny rule refuses a read, a change and a preset alike, and so does dontAsk without an allow rule', async ($, on) => {
    const w = handsWorld(on, true)
    await handsUp($, w)
    const sent = handsChanges(w, 0).length
    const turn = await promptTurn($)
    w.askAnswer = 'Do it'
    w.toolCheck = () => ({ decision: 'deny', reason: 'Permission rule denies', rule: HANDS })
    expect(await hands($, { setting: 'speed', value: 1.5 })).toEqual({
      deny: "The user's permission settings do not let the hands tool set the hand control cursor speed setting to 1.5 here (Permission rule denies).",
    })
    for (const args of [{ setting: 'speed' }, { preset: 'fast' }, { setting: 'speed', value: 'default' }]) {
      expect(await hands($, args), JSON.stringify(args)).toEqual({ deny: expect.stringMatching(REFUSED) })
    }
    await nextTurn($, turn, 'dontAsk')
    w.toolCheck = () => ({ decision: 'allow' })
    for (const args of [{ setting: 'speed' }, { setting: 'speed', value: 1.5 }, { preset: 'fast' }]) {
      expect(await hands($, args), JSON.stringify(args)).toEqual({ deny: expect.stringMatching(REFUSED) })
    }
    expect(w.asked).toEqual([])
    expect(handsChanges(w, sent)).toEqual([])
    expect(w.store.has(TUNING_KEY)).toBe(false)
    // Its own allow rule still runs it.
    w.toolCheck = ALLOWED
    expect((await hands($, { setting: 'speed', value: 1.5 })).result).toBe(SPEED)
  })

  test('arguments it cannot use are answered with what to fix; nothing is asked or changed', async ($, on) => {
    const w = handsWorld(on, true)
    await handsUp($, w)
    const sent = handsChanges(w, 0).length
    await promptTurn($)
    w.askAnswer = 'Do it'
    const answers: [Record<string, unknown>, string | RegExp][] = [
      [{ value: 1.5 }, 'Say which setting the value is for, such as setting "speed" with value 1.5.'],
      [{ setting: 'warp', value: 1 }, /^Unknown setting "warp"\./],
      [{ setting: 5, value: 1 }, 'The setting must be a name, such as "speed"; /jarvis hands tune lists them.'],
      [{ setting: 'speed', value: 99 }, '99 is outside the range for Cursor speed: 0.7 to 3 (default 1). Nothing was changed.'],
      [{ setting: 'speed', value: 'fast' }, /^"fast" is not a number\./],
      [{ setting: 'speed', value: true }, 'The value must be a number, or "default".'],
      [{ preset: 'fast', setting: 'speed' }, 'Give either a preset or a setting, not both.'],
      [{ preset: 'fast', value: 2 }, 'Give either a preset or a setting, not both.'],
      [{ preset: 'turbo' }, /^Unknown preset "turbo"\./],
      [{ preset: 5 }, 'The preset must be one of precise, balanced or fast.'],
      [{ action: 'explode', setting: 'speed', value: 1.5 }, /^Unknown action "explode"/],
    ]
    for (const [args, answer] of answers) {
      const { result } = await hands($, args)
      if (typeof answer === 'string') expect(result, JSON.stringify(args)).toBe(answer)
      else expect(result, JSON.stringify(args)).toMatch(answer)
    }
    expect(w.asked).toEqual([])
    expect(w.checks).toEqual([])
    expect(handsChanges(w, sent)).toEqual([])
    expect(w.store.has(TUNING_KEY)).toBe(false)
  })

  test('plan mode refuses every change to the sensitivity, even with the allow rule; a setting read by name still runs', async ($, on) => {
    const w = handsWorld(on, true)
    await handsUp($, w)
    const sent = handsChanges(w, 0).length
    w.toolCheck = ALLOWED
    await promptTurn($, 'plan')
    const changes: Record<string, unknown>[] = [
      { setting: 'speed', value: 1.5 },
      { setting: 'speed', value: 'default' },
      { preset: 'fast' },
      { preset: 'balanced', action: 'status' },
      { setting: 'speed', value: 1.5, action: 'status' },
      { setting: 'speed', display: '2' },
      { setting: 'speed', action: 'engage' },
      { setting: 'speed', action: 'off' },
    ]
    for (const args of changes) expect(await hands($, args), JSON.stringify(args)).toMatchObject({ result: expect.stringMatching(PLAN_MODE) })
    expect((await hands($, { setting: 'speed' })).result).toMatch(/^Cursor speed is 1 \(default 1, range 0\.7 to 3\)\./)
    expect((await hands($, { action: 'status', setting: 'pinch' })).result).toMatch(/^Pinch sensitivity is 1 [^]*Hand control: ready/)
    // The plan's own answers to what to fix are not a change either.
    expect((await hands($, { setting: 'speed', value: 99 })).result).toMatch(/^99 is outside the range/)
    expect(w.asked).toEqual([])
    expect(handsChanges(w, sent)).toEqual([])
    expect(w.store.has(TUNING_KEY)).toBe(false)
    // An approved plan leaves plan mode mid-turn.
    await $.classic.PostToolUse({ tool_name: 'ExitPlanMode', tool_input: {}, tool_response: {}, tool_use_id: 'toolu_1', permission_mode: 'plan' })
    expect((await hands($, { setting: 'speed', value: 1.5 })).result).toBe(SPEED)
  })

  test('a mode not known yet changes nothing; a setting read by name still runs', async ($, on) => {
    const w = handsWorld(on, false)
    await startSession($, w)
    w.toolCheck = ALLOWED
    for (const args of [{ setting: 'speed', value: 1.5 }, { preset: 'precise' }, { setting: 'smoothing', value: 'default' }]) {
      expect(await hands($, args), JSON.stringify(args)).toMatchObject({ result: expect.stringMatching(MODE_UNKNOWN) })
    }
    expect((await hands($, { setting: 'speed' })).result).toMatch(/^Cursor speed is 1 /)
    expect(w.store.has(TUNING_KEY)).toBe(false)
    expect(w.asked).toEqual([])
  })

  test('a turn begun with no prompt asks before a change, even with an allow rule', async ($, on) => {
    const w = handsWorld(on, false)
    await startHelper($, w)
    w.toolCheck = ALLOWED
    const turn = await promptTurn($)
    await completeTurn($, turn)
    await $.turn.start({ text: '', turnId: 't-continue' })
    w.askAnswer = "Don't do it"
    expect(await hands($, { preset: 'fast' })).toMatchObject({ result: expect.stringMatching(DONT) })
    expect(w.asked).toEqual([ask('apply the fast hand control preset, which sets every sensitivity setting')])
    expect(w.store.has(TUNING_KEY)).toBe(false)
  })

  /** Every shape of tuning arguments a model might send: in range, outside it, mistyped, doubled up, with an action. */
  function shapes(): Record<string, unknown>[] {
    const all: Record<string, unknown>[] = []
    for (const knob of KNOBS) {
      all.push({ setting: knob.name }, { setting: knob.key.toUpperCase(), value: knob.max })
      for (const value of [knob.min, knob.max + 1, 'default', ` ${knob.max} `, 'x', null, [knob.min]]) all.push({ setting: knob.name, value })
      all.push({ setting: knob.name, value: knob.min, action: 'status' })
    }
    for (const preset of [...PRESET_NAMES, 'Fast', 'default', 'turbo', '', 5, null, ['fast']]) all.push({ preset })
    all.push({ preset: 'fast', setting: 'speed' }, { preset: 'fast', value: 1 }, { value: 1.5 }, { setting: 5, value: 1 }, { setting: 'warp', value: 1 })
    all.push({ setting: '', value: 1 }, { setting: ['speed'], value: 1 }, { action: 'status', preset: 'precise' }, { action: 'explode', preset: 'precise' })
    return all
  }

  /** Whether `args` change what is kept, run on a fresh start of the choices. */
  async function changes($: TestEngine, w: World, args: Record<string, unknown>): Promise<boolean> {
    w.store.delete(TUNING_KEY)
    await hands($, args)
    const isChanged = w.store.has(TUNING_KEY)
    w.store.delete(TUNING_KEY)
    return isChanged
  }

  test('whatever changes the sensitivity is asked about first, and plan mode and an unknown mode leave all of it undone', async ($, on) => {
    const w = handsWorld(on, false)
    await startHelper($, w)
    const all = shapes()
    // 1. Under the user's allow rule everything runs: which shapes change the choices?
    w.toolCheck = ALLOWED
    let turn = await promptTurn($)
    const changed = new Map<Record<string, unknown>, boolean>()
    for (const args of all) changed.set(args, await changes($, w, args))
    expect([...changed.values()].some(Boolean)).toBe(true)
    expect(w.asked).toEqual([])
    // Each of them is what hands.ts toolTuning calls a set or a preset, and nothing else is.
    for (const args of all) {
      const kind = toolTuning(args)?.kind
      expect(changed.get(args) === true && kind !== 'set' && kind !== 'preset', JSON.stringify(args)).toBe(false)
    }
    // 2. Under an ask the user refuses, every one that changes something was asked about, and none ran.
    w.toolCheck = () => ({ decision: 'ask' })
    w.askAnswer = "Don't do it"
    turn = await nextTurn($, turn)
    for (const args of all) {
      const asked = w.asked.length
      expect(await changes($, w, args), JSON.stringify(args)).toBe(false)
      if (changed.get(args) === true) expect(w.asked.length, JSON.stringify(args)).toBe(asked + 1)
    }
    // 3. In plan mode none of them changes anything.
    w.toolCheck = ALLOWED
    w.askAnswer = 'Do it'
    const asked = w.asked.length
    await nextTurn($, turn, 'plan')
    for (const args of all) expect(await changes($, w, args), JSON.stringify(args)).toBe(false)
    expect(w.asked).toHaveLength(asked)
  })

  test('with the mode not known yet, none of them changes anything', async ($, on) => {
    const w = handsWorld(on, false)
    await startSession($, w)
    w.toolCheck = ALLOWED
    w.askAnswer = 'Do it'
    for (const args of shapes()) expect(await changes($, w, args), JSON.stringify(args)).toBe(false)
    expect(w.asked).toEqual([])
  })

  test('what a tool call asks of the tuning is read as the tool runs it', () => {
    const speed = KNOBS.find(knob => knob.name === 'speed')
    expect(speed).toBeDefined()
    expect(toolTuning({ setting: 'speed' })).toEqual({ kind: 'read', knob: speed })
    expect(toolTuning({ setting: 'Cursor Speed', value: ' 1.5 ' })).toEqual({ kind: 'set', knob: speed, value: 1.5 })
    expect(toolTuning({ setting: 'cursorSpeed', value: 'DEFAULT' })).toEqual({ kind: 'set', knob: speed, value: 'default' })
    expect(toolTuning({ setting: 'speed', value: 0 })).toBeUndefined()
    expect(toolTuning({ preset: 'FAST' })).toEqual({ kind: 'preset', name: 'fast' })
    expect(toolTuning({ preset: 'default' })).toEqual({ kind: 'preset', name: 'balanced' })
    for (const input of [{}, { action: 'on' }, { display: '2' }, { value: 1 }, { setting: 'warp' }, { setting: 5 }, { preset: 'fast', setting: 'speed' }, { preset: 'fast', value: 1 }, { preset: 'x' }, { preset: 1 }]) {
      expect(toolTuning(input), JSON.stringify(input)).toBeUndefined()
    }
  })
})

describe('the hands tool: sensitivity, a voice turn', () => {
  /** A voice turn t1 that says `text`, with its prompt. */
  async function say($: TestEngine, w: World, helper: FakeChild, text: string, turnId: string, extra: Record<string, unknown> = {}): Promise<void> {
    helper.event({ type: 'utterance', id: `u-${turnId}`, text, source: 'wake', durationMs: 900, language: 'en', ...extra })
    await w.settle()
    await $.classic.UserPromptSubmit({ prompt: text, permission_mode: 'default' })
    await $.turn.start({ text, turnId })
  }

  test('a spoken yes covers exactly the setting and value it was asked about', async ($, on) => {
    const w = handsWorld(on, false)
    const helper = await startHelper($, w)
    await say($, w, helper, 'Jarvis, make the cursor faster', 't1')
    expect(await hands($, { setting: 'speed', value: 1.5 })).toMatchObject({
      result: expect.stringMatching(/^Jarvis held this: it uses the hands tool to set the hand control cursor speed setting to 1\.5 and needs the user's spoken OK\./),
    })
    await completeTurn($, 't1')
    await w.settle()
    expect(w.named('speak').map(command => command.body.text).join(' ')).toContain('Claude wants to set the hand control cursor speed setting to 1.5. Say yes to let it, sir.')
    helper.event({ type: 'speech_done', replyId: 't1', interrupted: false, spokenText: '', endedAtMs: 40_000 })
    await say($, w, helper, 'Go ahead.', 't2', { startedAtMs: 40_500, overSpeech: false })
    // The yes was for 1.5: another value, another setting and a preset are held again, and nothing runs.
    expect(await hands($, { setting: 'speed', value: 3 })).toMatchObject({ result: expect.stringMatching(/^Jarvis held this: it uses the hands tool to set the hand control cursor speed setting to 3 /) })
    expect(w.store.has(TUNING_KEY)).toBe(false)
    expect(w.asked).toEqual([])
  })

  test('the call it was asked about runs once the yes is said, however its value is written', async ($, on) => {
    const w = handsWorld(on, false)
    const helper = await startHelper($, w)
    await say($, w, helper, 'Jarvis, use the fast preset', 't1')
    expect(await hands($, { preset: 'fast' })).toMatchObject({ result: expect.stringMatching(/^Jarvis held this: it uses the hands tool to apply the fast hand control preset /) })
    await completeTurn($, 't1')
    await w.settle()
    helper.event({ type: 'speech_done', replyId: 't1', interrupted: false, spokenText: '', endedAtMs: 40_000 })
    await say($, w, helper, 'Go ahead.', 't2', { startedAtMs: 40_500, overSpeech: false })
    expect((await hands($, { preset: 'Fast' })).result).toMatch(/^Preset fast: /)
    expect(w.store.get(TUNING_KEY)).toMatchObject({ cursorSpeed: { value: 1.6 } })
    expect(w.asked).toEqual([])
  })
})

describe('the hands tool: the air keyboard actions', () => {
  const KEYBOARD_CAPS = ['heartbeat', 'status', 'config', 'pause', 'resume', 'engage', 'disengage', 'calibrate', 'shutdown', 'keyboard']
  const KEYBOARD_ACTIONS = ['keyboard', 'keyboard_practice', 'keyboard_off'] as const
  /** The plugin option that turns the keyboard on, the only way there is. */
  const KEYBOARD_ON = { options: { handKeyboard: 'on' } }
  /** What Jarvis asks about each (the tool's words, not the model's). */
  const QUESTION = {
    keyboard: ask('open the air keyboard, which you then type on yourself in the air'),
    keyboard_practice: ask('open the air keyboard in practice mode, where nothing you tap is typed'),
    keyboard_off: ask('close the air keyboard, which throws away what is in its review box'),
  }

  /** Hand control on and up, with a helper that has the keyboard. */
  async function keyboardUp($: TestEngine, w: World): Promise<void> {
    await startHelper($, w)
    const helper = w.lastHands()
    helper.hello(HANDS_PORT, KEYBOARD_CAPS)
    helper.event({ type: 'state', state: 'starting' })
    helper.event({ type: 'ready', camera: 'UGREEN Camera', width: 1280, height: 720, fps: 30, displays: [] })
    helper.event({ type: 'state', state: 'idle' })
    await w.settle()
  }

  /** The keyboard commands the helper has heard so far. */
  const heard = (w: World): unknown[] => w.handsNamed('keyboard').map(command => command.body)

  test('G1: each asks the user by name before it runs; a no sends nothing, a yes sends the command', KEYBOARD_ON, async ($, on) => {
    const w = handsWorld(on, true)
    await keyboardUp($, w)
    const before = heard(w).length
    await promptTurn($)
    w.toolCheck = () => ({ decision: 'ask', reason: 'Permission rule asks', rule: HANDS })
    w.askAnswer = "Don't do it"
    for (const action of KEYBOARD_ACTIONS) expect(await hands($, { action }), action).toMatchObject({ result: expect.stringMatching(DONT) })
    expect(w.asked).toEqual(KEYBOARD_ACTIONS.map(action => QUESTION[action]))
    expect(heard(w)).toHaveLength(before)
    w.askAnswer = 'Do it'
    expect((await hands($, { action: 'keyboard' })).result).toContain("The air keyboard is opening on the user's screen.")
    expect(heard(w).slice(before).at(-1)).toEqual({ action: 'start' })
    expect((await hands($, { action: 'keyboard_practice' })).result).toContain('practice is opening')
    expect(heard(w).at(-1)).toEqual({ action: 'practice' })
    expect((await hands($, { action: 'keyboard_off' })).result).toContain('closing')
    expect(heard(w).at(-1)).toEqual({ action: 'stop' })
    expect(w.asked).toHaveLength(6)
  })

  test('G2: a deny rule refuses all three before anything is asked or sent', KEYBOARD_ON, async ($, on) => {
    const w = handsWorld(on, true)
    await keyboardUp($, w)
    const before = heard(w).length
    await promptTurn($)
    w.toolCheck = () => ({ decision: 'deny', reason: 'Permission rule denies', rule: HANDS })
    w.askAnswer = 'Do it'
    for (const action of KEYBOARD_ACTIONS) expect(await hands($, { action }), action).toEqual({ deny: expect.stringMatching(REFUSED) })
    expect(w.asked).toEqual([])
    expect(heard(w)).toHaveLength(before)
  })

  test('G3: plan mode refuses all three, even with the user\'s allow rule', KEYBOARD_ON, async ($, on) => {
    const w = handsWorld(on, true)
    await keyboardUp($, w)
    const before = heard(w).length
    w.toolCheck = ALLOWED
    await promptTurn($, 'plan')
    for (const action of KEYBOARD_ACTIONS) expect(await hands($, { action }), action).toMatchObject({ result: expect.stringMatching(PLAN_MODE) })
    expect(heard(w)).toHaveLength(before)
    expect(w.asked).toEqual([])
  })

  test('G4: dontAsk refuses what no rule allows beforehand; the user\'s own allow rule runs them with no question', KEYBOARD_ON, async ($, on) => {
    const w = handsWorld(on, true)
    await keyboardUp($, w)
    const before = heard(w).length
    await promptTurn($, 'dontAsk')
    w.toolCheck = () => ({ decision: 'allow' })
    w.askAnswer = 'Do it'
    for (const action of KEYBOARD_ACTIONS) expect(await hands($, { action }), action).toEqual({ deny: expect.stringMatching(REFUSED) })
    expect(heard(w)).toHaveLength(before)
    w.toolCheck = ALLOWED
    expect((await hands($, { action: 'keyboard_practice' })).result).toContain('practice is opening')
    expect(heard(w).at(-1)).toEqual({ action: 'practice' })
    expect(w.asked).toEqual([])
  })

  test('G5: a subagent is asked even with an allow rule', KEYBOARD_ON, async ($, on) => {
    const w = handsWorld(on, true)
    await keyboardUp($, w)
    const before = heard(w).length
    await promptTurn($)
    w.toolCheck = ALLOWED
    w.askAnswer = "Don't do it"
    expect(await hands($, { action: 'keyboard', agentId: 'agent-1' })).toMatchObject({ result: expect.stringMatching(DONT) })
    expect(w.asked).toEqual([QUESTION.keyboard])
    expect(heard(w)).toHaveLength(before)
  })

  test('G6: with the option off an allowed call is still only the off text; closing still closes', async ($, on) => {
    const w = handsWorld(on, true)
    await handsUp($, w)
    const sent = handsChanges(w, 0).length
    w.toolCheck = ALLOWED
    await promptTurn($)
    const off = 'The air keyboard is off. Turn it on in the Jarvis plugin settings (handKeyboard).'
    expect((await hands($, { action: 'keyboard' })).result).toBe(off)
    expect((await hands($, { action: 'keyboard_practice' })).result).toBe(off)
    expect(handsChanges(w, sent)).toEqual([])
    expect(w.asked).toEqual([])
  })
})
