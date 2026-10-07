import { describe, expect, test } from 'claude-code/testing'
import type { On } from 'claude-code'
import type { Engine as TestEngine } from 'claude-code/testing'

import type { RuleVerdict } from './pc'
import type { World } from './test-harness'
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
    expect((await hands($, { action: 'explode' })).result).toBe('Unknown action "explode"; use one of on, off, status, calibrate, pause, resume, engage, disengage.')
    expect((await hands($, {})).result).toBe('Give an action, a display, or both.')
    expect((await hands($, { display: 'left' })).result).toBe('"left" is not a display. Use /jarvis hands display all, a display number such as 2, or a list such as 1,2.')
    expect(w.asked).toEqual([])
    expect(w.checks).toEqual([])
    expect(w.handsHelpers()).toHaveLength(0)
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
