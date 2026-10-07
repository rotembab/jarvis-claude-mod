import { describe, expect, test } from 'claude-code/testing'
import type { Engine as TestEngine } from 'claude-code/testing'

import { BadHomeInput, HOME_HELP, HOME_TOOL, parseHomeToolInput } from './home'
import type { RunAnswer, RunCall, SentCommand, World } from './test-harness'
import { DATA_DIR, jarvis, PORT, startHelper, startSession, VENV_PYTHON, WINDOWS_ENV, world } from './test-harness'

const CLOUD_ENV = { ...WINDOWS_ENV, CLAUDE_CODE_REMOTE: 'true' }
const PLAN_MODE = /^Plan mode is on, so Jarvis does not change devices/

/** A model call of the tool, with its raw arguments. */
function homeTool($: TestEngine, args: Record<string, unknown>) {
  return $.tool.call({ tool: HOME_TOOL, ...args })
}

/** The helper's `home` answer: done unless given, with plain words. */
function answer(text: string, extra: Record<string, unknown> = {}) {
  return { status: 200, body: { ok: true, result: 'done', code: 'ok', text, ...extra } }
}

/** A helper whose `home` command answers with `reply`, and `ok` to the rest. */
function homeReplies(w: World, reply: (command: SentCommand) => { status: number; body: unknown }): void {
  w.respond = command => (command.name === 'home' ? reply(command) : { status: 200, body: { ok: true } })
}

/** A front door that unlocks only once confirmed. */
function guardedDoor(w: World): void {
  homeReplies(w, command =>
    command.body.confirmed === true
      ? answer('The front door is unlocked.')
      : answer("This needs the user's OK on screen: unlock the Front door.", {
          result: 'confirm',
          code: 'confirm',
          tier: 'screen',
          prompt: 'Let Jarvis unlock the Front door?',
        }),
  )
}

/** One-shot `jarvis_voice home ...` runs answered by `reply` (other programs: not found). */
function oneShot(w: World, reply: (call: RunCall) => RunAnswer | undefined): void {
  w.onRun = call => (call.argv[0] === VENV_PYTHON && call.argv[3] === 'home' ? reply(call) : undefined)
}

const printed = (value: Record<string, unknown>): RunAnswer => ({
  exitCode: 0,
  // A stray line first: only the last JSON line is the answer.
  stdout: `starting\n${JSON.stringify(value)}\n`,
})

const homeBodies = (w: World) => w.named('home').map(command => command.body)

describe('home_control: registration', () => {
  test('is registered at session start with a small schema, and kept in the prompt', async ($, on) => {
    const w = world(on)
    on('tool.describe', ($, e) => ({ description: e.description }))
    await startSession($, w)
    expect(w.tools.map(tool => tool.name)).toEqual(['home_control'])
    const [spec] = w.tools
    expect(spec?.description.length).toBeLessThan(1500)
    expect(spec?.description).toContain('Never ask for a PIN, key, password or token in the chat')
    expect(spec?.inputSchema).toEqual({
      type: 'object',
      properties: {
        action: { type: 'string', enum: ['list', 'status', 'do', 'setup'] },
        device: expect.objectContaining({ type: 'string' }),
        command: expect.objectContaining({ type: 'string' }),
        value: expect.objectContaining({ type: ['string', 'number'] }),
        query: expect.objectContaining({ type: 'string' }),
      },
      required: ['action'],
      additionalProperties: false,
    })
    expect(JSON.stringify(spec?.inputSchema).length).toBeLessThan(800)
    const described = await $.tool.describe({ tool: HOME_TOOL, description: 'd', provider: { plugin: 'engine', tier: 'core' } })
    expect(described).toEqual({ description: 'd', isDeferred: false })
  })

  test('a refused registration leaves the rest of the session start alone', async ($, on) => {
    const w = world(on)
    w.toolRefusal = 'allowedMcpServers (managed): plugins outside policy may not add tools'
    await startSession($, w)
    expect(w.tools).toEqual([])
    expect(w.helpers()).toHaveLength(1) // the helper still starts
    expect(await jarvis($, 'home help')).toBe(HOME_HELP) // and /jarvis still answers
  })

  test('is not registered in a cloud session, and says why if called', async ($, on) => {
    const w = world(on, { env: CLOUD_ENV })
    await startSession($, w)
    expect(w.tools).toEqual([])
    expect(await homeTool($, { action: 'list' })).toEqual({
      result: "Home control runs on the user's own computer, and this session runs in the cloud, so it is not available here.",
    })
    expect(w.runs).toEqual([])
    expect(w.commands).toEqual([])
  })
})

describe('home_control: arguments', () => {
  test('only the checked fields reach the running helper; a model-supplied confirmed is dropped', async ($, on) => {
    const w = world(on)
    homeReplies(w, () => answer('The Sony TV volume is 20.'))
    await startHelper($, w)
    const ran = await homeTool($, {
      action: ' do ',
      device: '  Sony TV ',
      command: 'set_volume',
      value: '20',
      confirmed: true,
      query: 'ignored for do',
      extra: { anything: 1 },
    })
    expect(ran).toEqual({ result: 'The Sony TV volume is 20.' })
    const [sent] = w.named('home')
    expect(sent?.url).toBe(`http://127.0.0.1:${PORT}/v1/home`)
    expect(sent?.headers.authorization).toMatch(/^Bearer [0-9a-f]{64}$/)
    expect(sent?.body).toEqual({ action: 'do', device: 'Sony TV', command: 'set_volume', value: '20' })
  })

  test('numbers and true/false pass as values; a number for a name is its digits; list takes a query', async ($, on) => {
    const w = world(on)
    homeReplies(w, () => answer('Done.'))
    await startHelper($, w)
    await homeTool($, { action: 'do', device: 'Sony TV', command: 'set_volume', value: 35 })
    await homeTool($, { action: 'do', device: 'Desk lamp', command: 'turn_on', value: true })
    await homeTool($, { action: 'Status', device: 3 })
    await homeTool($, { action: 'list', query: ' bedroom ' })
    await homeTool($, { action: 'list', device: 'lights' })
    await homeTool($, { action: 'list', query: '', value: { ignored: true } })
    expect(homeBodies(w)).toEqual([
      { action: 'do', device: 'Sony TV', command: 'set_volume', value: 35 },
      { action: 'do', device: 'Desk lamp', command: 'turn_on', value: true },
      { action: 'status', device: '3' },
      { action: 'list', query: 'bedroom' },
      { action: 'list', query: 'lights' },
      { action: 'list' },
    ])
  })

  test('bad arguments are refused, saying what is wrong, and nothing is sent', async ($, on) => {
    const w = world(on)
    await startHelper($, w)
    const cases: [Record<string, unknown>, string][] = [
      [{ action: 'unlock_door', device: 'Front door' }, 'home_control: unknown action "unlock_door": use list, status, do or setup.'],
      [{ device: 'Sony TV' }, 'home_control: action is required: list, status, do or setup.'],
      [{ action: 'do', device: 'Sony TV' }, 'home_control: do needs a device and a command (list shows them).'],
      [{ action: 'status' }, 'home_control: status needs a device (list shows them).'],
      [{ action: 'do', device: 'Sony TV', command: 'set_volume', value: 'x'.repeat(501) }, 'home_control: value is too long (at most 500 characters).'],
      [{ action: 'do', device: 'Sony TV', command: 'set_volume', value: { level: 20 } }, 'home_control: value must be text, a number or true/false.'],
      [{ action: 'do', device: 'Sony TV', command: 'set_volume', value: [20] }, 'home_control: value must be text, a number or true/false.'],
      [{ action: 'status', device: 'x'.repeat(201) }, 'home_control: device is too long (at most 200 characters).'],
      [{ action: 'do', device: 'Sony TV', command: 'x'.repeat(65) }, 'home_control: command is too long (at most 64 characters).'],
      [{ action: 'do', device: ['Sony TV'], command: 'turn_on' }, 'home_control: device must be text.'],
      [{ action: 'list', query: 'q'.repeat(201) }, 'home_control: query is too long (at most 200 characters).'],
    ]
    for (const [args, deny] of cases) expect(await homeTool($, args)).toEqual({ deny })
    expect(w.named('home')).toHaveLength(0)
  })

  test('parseHomeToolInput keeps only what each action uses', () => {
    expect(parseHomeToolInput({ action: 'setup', device: 'TV', confirmed: true })).toEqual({ action: 'setup' })
    expect(parseHomeToolInput({ action: 'do', device: 'TV', command: 'mute', value: '  ' })).toEqual({
      action: 'do',
      body: { action: 'do', device: 'TV', command: 'mute' },
    })
    expect(() => parseHomeToolInput({ action: 'do', device: 'TV', command: 'set_volume', value: Number.NaN })).toThrow(BadHomeInput)
    expect(() => parseHomeToolInput({ action: 'info' })).toThrow('unknown action "info"')
  })

  test('a failure carries the next step for the model', async ($, on) => {
    const w = world(on)
    const failures: Record<string, string> = {
      ambiguous: 'Two devices match "the light": Desk lamp, Ceiling light.',
      not_found: 'There is no device called "the toaster".',
      needs_setup: 'No home devices are set up yet.',
      unreachable: 'The Sony TV is off or unreachable.',
    }
    homeReplies(w, command => {
      const code = String(command.body.device)
      return answer(failures[code] ?? '?', { result: 'failed', code })
    })
    await startHelper($, w)
    const said = async (code: string) => ((await homeTool($, { action: 'status', device: code })) as { result: string }).result
    expect(await said('ambiguous')).toBe(`${failures.ambiguous} Ask the user which one they mean.`)
    expect(await said('not_found')).toBe(`${failures.not_found} Call list to see the devices and their names.`)
    expect(await said('needs_setup')).toBe(`${failures.needs_setup} Offer to open the home setup window (action "setup").`)
    expect(await said('unreachable')).toBe(failures.unreachable)
  })

  test('a helper that refuses the command says so', async ($, on) => {
    const w = world(on)
    homeReplies(w, () => ({ status: 400, body: { ok: false, error: { code: 'bad_request', message: 'unknown command home' } } }))
    await startHelper($, w)
    expect(await homeTool($, { action: 'list' })).toEqual({
      result: 'Home control failed: unknown command home. Run /jarvis setup to update the Jarvis helper.',
    })
  })
})

describe('home_control: confirming on screen', () => {
  test('a "confirm" answer asks the user, and only a yes sends the same request confirmed', async ($, on) => {
    const w = world(on)
    guardedDoor(w)
    w.askAnswer = 'Yes, do it'
    await startHelper($, w)
    const ran = await homeTool($, { action: 'do', device: 'front door', command: 'unlock' })
    expect(ran).toEqual({ result: 'The front door is unlocked.' })
    expect(w.asked).toEqual(['Let Jarvis unlock the Front door?'])
    expect(homeBodies(w)).toEqual([
      { action: 'do', device: 'front door', command: 'unlock' },
      { action: 'do', device: 'front door', command: 'unlock', confirmed: true },
    ])
  })

  test('confirmed: true from the model still asks first', async ($, on) => {
    const w = world(on)
    guardedDoor(w)
    w.askAnswer = 'No'
    await startHelper($, w)
    await homeTool($, { action: 'do', device: 'front door', command: 'unlock', confirmed: true })
    expect(w.asked).toHaveLength(1)
    expect(homeBodies(w)).toEqual([{ action: 'do', device: 'front door', command: 'unlock' }])
  })

  test('no, a typed answer, or a dismissed dialog does nothing', async ($, on) => {
    const w = world(on)
    guardedDoor(w)
    await startHelper($, w)
    const outcomes: [string | undefined, string][] = [
      ['No', 'The user said no on screen, so nothing was done. Do not try again unless they ask.'],
      [
        'only the back door',
        'The user did not confirm on screen; they wrote instead: "only the back door". Nothing was done.',
      ],
      [
        undefined,
        'The user could not be asked on screen (the question was dismissed, or nobody is at this session), so nothing was done.',
      ],
    ]
    for (const [reply, result] of outcomes) {
      w.askAnswer = reply
      expect(await homeTool($, { action: 'do', device: 'front door', command: 'unlock' })).toEqual({ result })
    }
    expect(w.asked).toHaveLength(3)
    expect(homeBodies(w).filter(body => body.confirmed !== undefined)).toEqual([])
  })
})

describe('home_control: plan mode', () => {
  test('do and setup are refused in plan mode; list and status still work', async ($, on) => {
    const w = world(on)
    homeReplies(w, () => answer('Done.'))
    oneShot(w, () => printed({ ok: true, opened: true, text: 'The Jarvis home setup window is open on your desktop.' }))
    await startHelper($, w)
    await $.classic.UserPromptSubmit({ prompt: 'Plan turning off the TV', permission_mode: 'plan' })

    const refused = (await homeTool($, { action: 'do', device: 'Sony TV', command: 'turn_off' })) as { result: string }
    expect(refused.result).toMatch(PLAN_MODE)
    expect(((await homeTool($, { action: 'setup' })) as { result: string }).result).toMatch(PLAN_MODE)
    expect(w.runs).toEqual([])
    await homeTool($, { action: 'list' })
    await homeTool($, { action: 'status', device: 'Sony TV' })
    expect(homeBodies(w)).toEqual([{ action: 'list' }, { action: 'status', device: 'Sony TV' }])
  })

  test('leaving plan mode (a new prompt, or an approved plan) allows changes again', async ($, on) => {
    const w = world(on)
    homeReplies(w, () => answer('The Sony TV is off.'))
    await startHelper($, w)
    const turnOff = () => homeTool($, { action: 'do', device: 'Sony TV', command: 'turn_off' })

    await $.classic.UserPromptSubmit({ prompt: 'Plan it', permission_mode: 'plan' })
    expect(((await turnOff()) as { result: string }).result).toMatch(PLAN_MODE)
    await $.classic.PostToolUse({ tool_name: 'ExitPlanMode', tool_input: {}, tool_response: {}, tool_use_id: 'toolu_1', permission_mode: 'plan' })
    expect(await turnOff()).toEqual({ result: 'The Sony TV is off.' })

    await $.classic.PostToolUse({ tool_name: 'EnterPlanMode', tool_input: {}, tool_response: {}, tool_use_id: 'toolu_2' })
    expect(((await turnOff()) as { result: string }).result).toMatch(PLAN_MODE)
    await $.classic.UserPromptSubmit({ prompt: 'Do it now', permission_mode: 'acceptEdits' })
    expect(await turnOff()).toEqual({ result: 'The Sony TV is off.' })
    expect(homeBodies(w)).toHaveLength(2)
  })
})

describe('home_control: without the helper', () => {
  test('the one-shot command answers while the helper is not running', async ($, on) => {
    const w = world(on)
    oneShot(w, () => printed({ ok: true, result: 'done', code: 'ok', text: 'The Sony TV is off.' }))
    await startSession($, w) // the helper is spawned but has not said hello
    const ran = await homeTool($, { action: 'do', device: 'Sony TV', command: 'turn_off', confirmed: true })
    expect(ran).toEqual({ result: 'The Sony TV is off.' })
    expect(w.commands).toEqual([])
    expect(w.runs).toEqual([
      {
        argv: [
          VENV_PYTHON,
          '-m',
          'jarvis_voice',
          'home',
          'call',
          JSON.stringify({ action: 'do', device: 'Sony TV', command: 'turn_off' }),
          '--data-dir',
          DATA_DIR,
        ],
        cwd: DATA_DIR,
        env: { PYTHONUNBUFFERED: '1', PYTHONUTF8: '1' },
        timeoutMs: 35_000,
      },
    ])
  })

  test('a "confirm" from the one-shot command asks nobody and does nothing', async ($, on) => {
    const w = world(on)
    w.askAnswer = 'Yes, do it'
    const text =
      "This needs the user's OK on screen: unlock the Front door. Confirming it needs the Jarvis voice helper running: start it with /jarvis."
    oneShot(w, () => printed({ ok: true, result: 'confirm', code: 'confirm', text, prompt: 'Let Jarvis unlock the Front door?' }))
    await startSession($, w)
    expect(await homeTool($, { action: 'do', device: 'front door', command: 'unlock' })).toEqual({
      result: `${text} Nothing was done.`,
    })
    expect(w.asked).toEqual([])
    expect(w.runs).toHaveLength(1)
  })

  test('a refusal, a run with no answer, a run that timed out, and one that could not start', async ($, on) => {
    const w = world(on)
    const replies: RunAnswer[] = [
      { exitCode: 0, stdout: '{"ok": false, "error": {"code": "bad_request", "message": "invalid home command: bad value"}}\n' },
      { exitCode: 2, stdout: '', stderr: 'usage: jarvis_voice home ...' },
      { deny: 'jarvis: $.process.run aborted: still running after 35000ms' },
      { deny: 'jarvis: $.process.run failed to start: ENOENT' },
    ]
    oneShot(w, () => replies.shift())
    await startSession($, w)
    const said = async () => ((await homeTool($, { action: 'list' })) as { result: string }).result
    expect(await said()).toBe('Home control refused the request: invalid home command: bad value.')
    expect(await said()).toBe('Home control failed (exit 2, no answer). Run /jarvis setup to repair the Jarvis helper.')
    expect(await said()).toBe('Home control did not finish in time; the device may still act on it.')
    expect(await said()).toBe('Home control could not start the Jarvis helper. Run /jarvis setup to repair it.')
  })

  test('not installed: the model is told to have the user run /jarvis setup', async ($, on) => {
    const w = world(on, { installed: false })
    await startSession($, w)
    expect(await homeTool($, { action: 'list' })).toEqual({
      result:
        'Home control needs the Jarvis helper, which is not installed on this computer yet. Ask the user to run /jarvis setup, then /jarvis home setup to add devices.',
    })
    expect(await homeTool($, { action: 'setup' })).toEqual({
      result:
        'Home control needs the Jarvis helper, which is not installed on this computer yet. Ask the user to run /jarvis setup, then /jarvis home setup to add devices.',
    })
    expect(w.runs).toEqual([])
  })
})

describe('home_control: setup', () => {
  test('opens the setup window with the one-shot command, never through the helper', async ($, on) => {
    const w = world(on)
    oneShot(w, () => printed({ ok: true, opened: true, text: 'The Jarvis home setup window is open on your desktop.' }))
    await startHelper($, w)
    expect(await homeTool($, { action: 'setup' })).toEqual({
      result:
        'The Jarvis home setup window is open on your desktop. The user adds or pairs devices there and types any PIN, key or token there, not in the chat.',
    })
    expect(w.runs.map(run => run.argv)).toEqual([[VENV_PYTHON, '-m', 'jarvis_voice', 'home', 'open-setup', '--data-dir', DATA_DIR]])
    expect(w.runs[0]?.cwd).toBe(DATA_DIR)
    expect(w.named('home')).toHaveLength(0)
  })

  test('a window that could not open: its own words, else the command to run', async ($, on) => {
    const w = world(on)
    const replies: (RunAnswer | undefined)[] = [
      printed({ ok: true, opened: false, text: 'Open a terminal and run: python -m jarvis_voice home setup' }),
      undefined, // the program is missing
    ]
    oneShot(w, () => replies.shift())
    await startSession($, w)
    expect(await homeTool($, { action: 'setup' })).toEqual({ result: 'Open a terminal and run: python -m jarvis_voice home setup' })
    expect(await homeTool($, { action: 'setup' })).toEqual({
      result: `The home setup window did not open. To open it yourself, run this in a terminal: & "${VENV_PYTHON}" -m jarvis_voice home setup`,
    })
  })
})

describe('/jarvis home', () => {
  test('bare: what is set up (through the helper), and the home commands', async ($, on) => {
    const w = world(on)
    homeReplies(w, () => answer('Devices: Sony Bravia 1, Tuya 3\nDevices file: C:\\Users\\Rotem\\.jarvis\\home\\devices.json'))
    await startHelper($, w)
    const text = await jarvis($, 'home')
    expect(text).toBe(`Devices: Sony Bravia 1, Tuya 3\nDevices file: C:\\Users\\Rotem\\.jarvis\\home\\devices.json\n\n${HOME_HELP}`)
    expect(homeBodies(w)).toEqual([{ action: 'info' }])
  })

  test('bare, without the helper: the one-shot command; not installed: how to set up', async ($, on) => {
    const w = world(on)
    oneShot(w, () => printed({ ok: true, result: 'done', code: 'ok', text: 'No home devices are set up yet.' }))
    await startSession($, w)
    expect(await jarvis($, 'home')).toBe(`No home devices are set up yet.\n\n${HOME_HELP}`)
    expect(w.runs[0]?.argv[5]).toBe('{"action":"info"}')
    w.existing.clear()
    expect(await jarvis($, 'home info')).toContain(
      'Home control needs the Jarvis helper: run /jarvis setup first, then /jarvis home setup to add your devices.',
    )
  })

  test('list, status and do send what was typed', async ($, on) => {
    const w = world(on)
    homeReplies(w, () => answer('Done.'))
    await startHelper($, w)
    await jarvis($, 'home list')
    await jarvis($, 'home list bedroom lights')
    await jarvis($, 'home status living room TV')
    await jarvis($, 'home do Sony TV -- set_volume 20')
    await jarvis($, 'home do Sony TV -- launch_app Disney Plus')
    await jarvis($, 'home do Desk lamp -- turn_off')
    expect(homeBodies(w)).toEqual([
      { action: 'list' },
      { action: 'list', query: 'bedroom lights' },
      { action: 'status', device: 'living room TV' },
      { action: 'do', device: 'Sony TV', command: 'set_volume', value: '20' },
      { action: 'do', device: 'Sony TV', command: 'launch_app', value: 'Disney Plus' },
      { action: 'do', device: 'Desk lamp', command: 'turn_off' },
    ])
  })

  test('do without "--", status without a device, and an unknown word explain themselves', async ($, on) => {
    const w = world(on)
    await startHelper($, w)
    expect(await jarvis($, 'home do Sony TV set_volume 20')).toMatch(/^Usage: \/jarvis home do <device> -- <command> \[value\]/)
    expect(await jarvis($, 'home do -- turn_off')).toMatch(/^Usage:/)
    expect(await jarvis($, 'home do Sony TV --')).toMatch(/^Usage:/)
    expect(await jarvis($, 'home status')).toBe('Which device? For example /jarvis home status living room TV.')
    expect(await jarvis($, 'home frobnicate')).toBe(`Unknown home subcommand "frobnicate".\n\n${HOME_HELP}`)
    expect(await jarvis($, `home status ${'x'.repeat(201)}`)).toBe('Not sent: device is too long (at most 200 characters).')
    expect(w.named('home')).toHaveLength(0)
  })

  test('do asks on screen as the tool does; nothing is confirmed for the user', async ($, on) => {
    const w = world(on)
    guardedDoor(w)
    await startHelper($, w)
    w.askAnswer = 'No'
    expect(await jarvis($, 'home do front door -- unlock')).toBe('Nothing was done.')
    w.askAnswer = 'Yes, do it'
    expect(await jarvis($, 'home do front door -- unlock')).toBe('The front door is unlocked.')
    expect(w.asked).toEqual(['Let Jarvis unlock the Front door?', 'Let Jarvis unlock the Front door?'])
    expect(homeBodies(w)).toEqual([
      { action: 'do', device: 'front door', command: 'unlock' },
      { action: 'do', device: 'front door', command: 'unlock' },
      { action: 'do', device: 'front door', command: 'unlock', confirmed: true },
    ])
  })

  test('setup opens the window', async ($, on) => {
    const w = world(on)
    oneShot(w, () => printed({ ok: true, opened: true, text: 'The Jarvis home setup window is open on your desktop.' }))
    await startSession($, w)
    expect(await jarvis($, 'home setup')).toBe('The Jarvis home setup window is open on your desktop.')
    expect(w.runs.map(run => run.argv[4])).toEqual(['open-setup'])
  })

  test('in a cloud session it says home control is not here', async ($, on) => {
    const w = world(on, { env: CLOUD_ENV })
    await startSession($, w)
    const cloud = 'Home control runs on your own computer; this session runs in the cloud, so it is not available here.'
    expect(await jarvis($, 'home setup')).toBe(cloud)
    expect(await jarvis($, 'home list')).toBe(cloud)
    expect(w.runs).toEqual([])
  })

  test('/jarvis help lists the home commands', async ($, on) => {
    const w = world(on)
    await startHelper($, w)
    expect(await jarvis($, 'help')).toContain(HOME_HELP)
  })
})
