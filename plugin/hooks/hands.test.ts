import { describe, expect, test } from 'claude-code/testing'
import type { On } from 'claude-code'
import type { Engine as TestEngine } from 'claude-code/testing'

import { BACKOFF_MS, HEARTBEAT_MS, ORPHAN_RETRY_MS, STOP_WAIT_MS } from './helper'
import {
  changesTuning,
  describeHandsError,
  findKnob,
  HANDS_TOOL,
  isTuningCall,
  KNOBS,
  parseDisplaySelection,
  parseHandsEvent,
  parseKnobValue,
  PRESETS,
} from './hands'
// The tool's calls here run under the user's own allow rule (allowedTurn); hands-gate.test.ts has its permissions.
import { HANDS_TOOL_ID } from './hands-gate'
import type { Answer, FakeChild, World, WorldOptions } from './test-harness'
import {
  allowedTurn,
  DATA_DIR,
  HANDS_INSTALLED,
  HANDS_PORT,
  HANDS_PYTHON,
  jarvis,
  PLUGIN_VERSION,
  PORT,
  startHelper,
  startSession,
  VENV_PYTHON,
  WINGET_UV,
  world,
} from './test-harness'

const VOICE_READY = 'JARVIS · ready · hold right ctrl to talk'
const HANDS_LOCK = /[\\/]hands[\\/]uv\.lock$/
const PALM_TOAST = 'Hand control is on. Hold an open palm toward the camera to start.'
const NOT_LOCAL =
  'Hand control runs on your own computer; this session runs in the cloud, so the hand helper is not started here.'
const ELSEWHERE =
  'Hand control is running in another Claude Code window. Turn it off there (/jarvis hands off), then run /jarvis hands restart here.'
/** A previous load's hand helper: the first start gives it a second to exit before it checks the venv. */
const PREVIOUS_HANDS = { port: 40001, token: 'old-hands-token', pid: 98 }
/** What /jarvis hands on stores under the default Hand control setting (off). */
const ON = { isOn: true, setting: false }
const PAUSED = 'Hand control paused: the camera is off. /jarvis hands resume turns it back on.'
const RESUMED = 'Hand control resumed: the camera is on again.'
const IN_USE = { type: 'error', code: 'camera_in_use', message: 'The camera is in use by another app', hint: 'close Teams or Zoom', fatal: false }
const IN_USE_TOAST = 'Hand control: The camera is in use by another app (close Teams or Zoom)'
const STALE_TOAST = 'Hand control needs an update: run /jarvis setup hands.'
/** What the helper says on macOS and Linux (runtime.py), message and hint. */
const UNSUPPORTED = 'Hand control can drive the mouse and windows on Windows only, for now.'
const UNSUPPORTED_HINT = 'Hand control drives the mouse and windows on Windows only, for now.'

const DISPLAYS = [
  { id: 1, name: 'DELL U2720Q', x: 0, y: 0, width: 2560, height: 1440, primary: true, virtual: false, used: true },
  { id: 2, name: 'EPSON Projector', x: 2560, y: 0, width: 1920, height: 1080, primary: false, virtual: false, used: true },
]

/** Hand control turned on (as /jarvis hands on stores it) and installed by this version. */
function handsWorld(on: On, options: WorldOptions = {}): World {
  const w = world(on, options)
  w.store.set('handsEnabled', ON)
  w.existing.add(HANDS_PYTHON)
  w.files.set(HANDS_INSTALLED, JSON.stringify({ pluginVersion: PLUGIN_VERSION }))
  return w
}

/** The commands sent to the hand helper, by name, in order. */
function handsSent(w: World): string[] {
  return w.commands.filter(command => command.url.includes(`:${HANDS_PORT}/`)).map(command => command.name)
}

/** Holds the hand helper's answers to `names` until the test calls the returned function. */
function slowAnswers(w: World, names: string[], body: Record<string, unknown> = { ok: true }): () => void {
  const waiting: (() => void)[] = []
  w.respond = command => {
    if (!names.includes(command.name)) return { status: 200, body: { ok: true } }
    return new Promise<Answer>(resolve => waiting.push(() => resolve({ status: 200, body })))
  }
  return () => waiting.splice(0).forEach(answer => answer())
}

/** Answers `name` with `body`, and everything else with ok. */
function answer(w: World, name: string, body: Record<string, unknown>): void {
  w.respond = command => ({ status: 200, body: command.name === name ? body : { ok: true } })
}

/** Brings the running hand helper up to hello, ready and idle. */
async function handsReady(w: World): Promise<FakeChild> {
  const hands = w.lastHands()
  hands.hello(HANDS_PORT)
  hands.event({ type: 'state', state: 'starting' })
  hands.event({ type: 'ready', camera: 'UGREEN Camera', width: 1280, height: 720, fps: 30, displays: DISPLAYS })
  hands.event({ type: 'state', state: 'idle' })
  await w.settle()
  return hands
}

/** uv and the model setup succeed, the venv appearing as uv ends. */
function installable(w: World): void {
  w.existing.add(WINGET_UV)
  w.exists = path => w.existing.has(path) || HANDS_LOCK.test(path)
  w.onSpawn = child => {
    if (child.argv[0] === WINGET_UV) {
      w.existing.add(HANDS_PYTHON)
      child.exit(0)
    } else if (child.argv.includes('setup')) {
      child.stdout('{"v":1,"type":"progress","step":"done","pct":100,"message":"Ready: hand model (7.8 MB)"}\n')
      child.exit(0)
    }
  }
}

/** The running helper fails with `code` until the supervisor gives up. */
async function giveUp(w: World, error: Record<string, unknown>): Promise<void> {
  for (const wait of [...BACKOFF_MS, undefined]) {
    w.lastHands().event({ type: 'error', fatal: true, ...error })
    w.lastHands().exit(1)
    await w.settle()
    if (wait === undefined) return
    await w.clock.advance(wait)
    await w.settle()
  }
}

/** Both helpers exit when told to shut down, as the real ones do. */
function exitOnShutdown(w: World): void {
  w.respond = command => {
    if (command.name === 'shutdown') {
      if (command.url.includes(`:${HANDS_PORT}/`)) w.lastHands().exit(0)
      else if (command.url.includes(`:${PORT}/`)) w.lastHelper().exit(0)
    }
    return { status: 200, body: { ok: true } }
  }
}

describe('hand helper process', () => {
  test('off by default: no hand helper, no camera, nothing more in the status line', async ($, on) => {
    const w = world(on)
    w.existing.add(HANDS_PYTHON)
    await startHelper($, w)
    expect(w.handsHelpers()).toHaveLength(0)
    expect(w.checked).not.toContain(HANDS_PYTHON)
    expect(w.status()).toBe(VOICE_READY)
    expect(w.tools).toEqual(['desktop', 'home_control', 'hands'])
    expect(await jarvis($, '')).toContain('/jarvis hands [on|off]           hand control: your webcam drives the mouse and windows')
  })

  test('turned on: spawns the hand helper by absolute path with the protocol env and the chosen camera', async ($, on) => {
    const w = handsWorld(on)
    w.store.set('handsCamera', 'UGREEN')
    await startHelper($, w)
    expect(w.handsHelpers()).toHaveLength(1)
    const { request } = w.lastHands()
    expect(request.argv).toEqual([HANDS_PYTHON, '-m', 'jarvis_hands', 'run', '--data-dir', DATA_DIR, '--camera', 'UGREEN'])
    expect(request.cwd).toBe(DATA_DIR)
    expect(request.input).toBeUndefined()
    const env = request.env ?? {}
    expect(env.PYTHONUNBUFFERED).toBe('1')
    expect(env.PYTHONUTF8).toBe('1')
    expect(env.JARVIS_PARENT).toBe('claude-code')
    expect(env.JARVIS_TOKEN).toMatch(/^[0-9a-f]{64}$/)
    expect(env.JARVIS_TOKEN).not.toBe(w.lastHelper().request.env?.JARVIS_TOKEN)
    expect(env.NO_PROXY).toBe('corp.example,127.0.0.1,localhost')
    expect(env.OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS).toBe('0')
    expect(env.FISH_AUDIO_API_KEY).toBeUndefined()
    expect(w.status()).toBe(`${VOICE_READY} · hands starting`)
  })

  test('the handControl and handCamera settings turn it on and pick the camera', { options: { handControl: 'on', handCamera: '1' } }, async ($, on) => {
    const w = world(on)
    w.existing.add(HANDS_PYTHON)
    await startSession($, w)
    expect(w.lastHands().argv.slice(-2)).toEqual(['--camera', '1'])
  })

  test('/jarvis hands off wins over the handControl setting', { options: { handControl: 'on' } }, async ($, on) => {
    const w = world(on)
    w.existing.add(HANDS_PYTHON)
    w.store.set('handsEnabled', { isOn: false, setting: true })
    await startSession($, w)
    expect(w.handsHelpers()).toHaveLength(0)
  })

  test('/jarvis hands on wins over the default setting of off, and is kept with it', async ($, on) => {
    const w = world(on)
    w.existing.add(HANDS_PYTHON)
    await startHelper($, w)
    exitOnShutdown(w)
    await jarvis($, 'hands on')
    await w.settle()
    expect(w.store.get('handsEnabled')).toEqual({ isOn: true, setting: false })
    expect(w.handsHelpers()).toHaveLength(1)
    await handsReady(w)
    // Off is what the setting says: no override is left to outlive a change of the setting.
    await jarvis($, 'hands off')
    expect(w.store.has('handsEnabled')).toBe(false)
  })

  test('the setting changed since /jarvis hands on: the setting wins, and the old choice is dropped', async ($, on) => {
    const w = world(on)
    w.existing.add(HANDS_PYTHON)
    // "On" while the setting was on, then the setting set to off (the default here).
    w.store.set('handsEnabled', { isOn: true, setting: true })
    await startSession($, w)
    expect(w.handsHelpers()).toHaveLength(0)
    expect(w.store.has('handsEnabled')).toBe(false)
  })

  test('a choice with no record of the setting it overrode is no override', async ($, on) => {
    const w = world(on)
    w.existing.add(HANDS_PYTHON)
    w.store.set('handsEnabled', true)
    await startSession($, w)
    expect(w.handsHelpers()).toHaveLength(0)
  })

  test('"turn on hand control" while the setting is on stores nothing, so setting it to off later turns the camera off', { options: { handControl: 'on' } }, async ($, on) => {
    const w = world(on)
    w.existing.add(HANDS_PYTHON)
    await startSession($, w)
    await handsReady(w)
    await allowedTurn($, w, HANDS_TOOL_ID)
    expect((await $.tool.call({ tool: 'mcp__jarvis__hands', action: 'on' })).result).toBe('Hand control is already on.')
    expect(w.store.has('handsEnabled')).toBe(false)
  })

  test('on but not installed: says so in the status line and spawns nothing', async ($, on) => {
    const w = world(on)
    w.store.set('handsEnabled', ON)
    await startHelper($, w)
    expect(w.handsHelpers()).toHaveLength(0)
    expect(w.checked).toContain(HANDS_PYTHON)
    expect(w.status()).toBe(`${VOICE_READY} · hands not set up · /jarvis setup hands`)
  })

  test('hello: the stored engage mode and displays go out as config, then heartbeats every 2 s', async ($, on) => {
    const w = handsWorld(on)
    w.store.set('handsEngage', 'always')
    w.store.set('handsDisplays', [2])
    await startHelper($, w)
    const hands = w.lastHands()
    const token = hands.request.env?.JARVIS_TOKEN
    hands.hello(HANDS_PORT)
    await w.settle()

    const config = w.handsNamed('config')
    expect(config.map(command => command.body)).toEqual([{ engage: 'always', displays: [2] }])
    expect(config[0]?.headers.authorization).toBe(`Bearer ${token}`)
    expect(w.state.get('jarvis.handsHelper')).toEqual({ port: HANDS_PORT, token, pid: 4242 })

    expect(w.handsNamed('heartbeat')).toHaveLength(0)
    await w.clock.advance(HEARTBEAT_MS)
    await w.settle()
    expect(w.handsNamed('heartbeat')).toHaveLength(1)
    await w.clock.advance(HEARTBEAT_MS * 2)
    await w.settle()
    expect(w.handsNamed('heartbeat')).toHaveLength(3)
    expect(w.handsNamed('heartbeat')[0]?.headers.authorization).toBe(`Bearer ${token}`)
  })

  test('without stored choices the config is an open palm on every display', async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    await handsReady(w)
    expect(w.handsNamed('config')[0]?.body).toEqual({ engage: 'palm', displays: 'all' })
  })

  test('events become the status line; the ready toast comes once; stray lines are ignored', async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    const hands = await handsReady(w)
    expect(w.status()).toBe(`${VOICE_READY} · hands ready · open palm to start`)
    expect(w.toasts).toContain(PALM_TOAST)

    hands.event({ type: 'gesture', name: 'engage' })
    hands.event({ type: 'state', state: 'active' })
    hands.stdout('INFO: TensorFlow Lite XNNPACK delegate\n')
    hands.event({ type: 'teleport', to: 'mars' })
    hands.event({ type: 'state', state: 'levitating' })
    await w.settle()
    expect(w.status()).toBe(`${VOICE_READY} · hands active`)

    hands.event({ type: 'state', state: 'paused' })
    hands.event({ type: 'ready', camera: 'UGREEN Camera', width: 1280, height: 720, fps: 30, displays: DISPLAYS })
    await w.settle()
    expect(w.status()).toBe(`${VOICE_READY} · hands paused`)
    expect(w.toasts.filter(toast => toast === PALM_TOAST)).toHaveLength(1)
  })

  test('calibration: each step is toasted, shown and spoken by the voice helper', async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    const hands = await handsReady(w)
    expect(await jarvis($, 'hands calibrate')).toContain('Calibrating: a target appears in each corner of the screen in turn')
    expect(w.handsNamed('calibrate').map(command => command.body)).toEqual([{ action: 'start' }])

    hands.event({ type: 'state', state: 'calibrating' })
    hands.event({ type: 'calibration', step: 'top_left', display: 'all displays' })
    await w.settle()
    expect(w.status()).toBe(`${VOICE_READY} · hands calibrating · top-left corner`)
    const first = 'Hold your open hand at the top-left corner of the screen, knuckles on the target, until it moves on.'
    expect(w.toasts.at(-1)).toBe(first)

    for (const step of ['top_right', 'bottom_right', 'bottom_left', 'done']) hands.event({ type: 'calibration', step })
    hands.event({ type: 'state', state: 'idle' })
    await w.settle()
    expect(w.toasts.at(-1)).toBe('Calibrated.')
    expect(w.status()).toBe(`${VOICE_READY} · hands ready · open palm to start`)

    const spoken = w.named('speak')
    expect(spoken.map(command => command.body.text)).toEqual([
      first,
      'Now the top-right corner.',
      'Now the bottom-right corner.',
      'And the bottom-left corner.',
      'Calibrated.',
    ])
    expect(spoken.every(command => command.url === `http://127.0.0.1:${PORT}/v1/speak`)).toBe(true)
    expect(spoken[0]?.body).toMatchObject({ seq: 0, final: true })
    expect(String(spoken[0]?.body.replyId)).toMatch(/^test-hands-[0-9a-f]{12}$/)
    expect(new Set(spoken.map(command => command.body.replyId)).size).toBe(spoken.length)

    expect(await jarvis($, 'hands calibrate cancel')).toBe('Calibration cancelled; the previous calibration stays.')
    expect(w.handsNamed('calibrate').at(-1)?.body).toEqual({ action: 'cancel' })
    expect(await jarvis($, 'hands calibrate now')).toBe('Unknown option "now". Use /jarvis hands calibrate, or /jarvis hands calibrate cancel.')
  })

  test('without the voice helper the calibration steps are only toasted', async ($, on) => {
    const w = handsWorld(on, { installed: false })
    await startSession($, w)
    const hands = await handsReady(w)
    hands.event({ type: 'calibration', step: 'top_left' })
    await w.settle()
    expect(w.toasts.at(-1)).toContain('top-left corner')
    expect(w.named('speak')).toHaveLength(0)
  })

  test('a non-fatal error is toasted once per code per minute', async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    const hands = await handsReady(w)
    const overlay = { type: 'error', code: 'overlay_failed', message: 'The on-screen reticle could not start', hint: 'hand control carries on without it', fatal: false }
    const toast = 'Hand control: The on-screen reticle could not start (hand control carries on without it)'
    hands.event(overlay)
    hands.event(overlay)
    hands.event({ type: 'error', code: 'input_blocked', message: 'Windows refused to move an administrator window', fatal: false })
    await w.settle()
    expect(w.toasts.filter(one => one === toast)).toHaveLength(1)
    expect(w.toasts.at(-1)).toBe('Hand control: Windows refused to move an administrator window')
    expect(w.status()).toBe(`${VOICE_READY} · hands ready · open palm to start`)

    await w.clock.advance(60_000)
    await w.settle()
    hands.event(overlay)
    await w.settle()
    expect(w.toasts.filter(one => one === toast)).toHaveLength(2)
  })

  test('a fatal error shows, then 1, 2, 5, 10 s restarts, then it gives up until /jarvis hands restart', async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    const why = 'The camera is in use by another app (close Teams or Zoom)'
    for (const [attempt, wait] of BACKOFF_MS.entries()) {
      const hands = w.lastHands()
      hands.event({ type: 'error', code: 'camera_in_use', message: 'The camera is in use by another app', hint: 'close Teams or Zoom', fatal: true })
      await w.settle()
      expect(w.status()).toBe(`${VOICE_READY} · hands stopped · ${why}`)
      hands.exit(1)
      await w.settle()
      expect(w.status()).toBe(`${VOICE_READY} · hands restarting · in ${wait / 1000} s: ${why}`)
      await w.clock.advance(wait)
      await w.settle()
      expect(w.handsHelpers()).toHaveLength(attempt + 2)
    }
    w.lastHands().stderr('Traceback: boom\n')
    w.lastHands().exit(1)
    await w.settle()
    expect(w.status()).toBe(`${VOICE_READY} · hands stopped · exited with code 1: Traceback: boom · /jarvis hands restart`)
    await w.clock.advance(120_000)
    await w.settle()
    expect(w.handsHelpers()).toHaveLength(BACKOFF_MS.length + 1)
    expect(w.helpers()).toHaveLength(1) // the voice helper ran on

    expect(await jarvis($, 'hands restart')).toBe('Restarting the hand helper.')
    await w.settle()
    expect(w.handsHelpers()).toHaveLength(BACKOFF_MS.length + 2)
  })

  test('a missing hand model gives up at once: restarting cannot fix it', async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    const hands = w.lastHands()
    hands.event({ type: 'error', code: 'model_missing', message: 'The hand model is missing', hint: 'run /jarvis setup hands', fatal: true })
    hands.exit(1)
    await w.settle()
    expect(w.status()).toBe(`${VOICE_READY} · hands stopped · The hand model is missing (run /jarvis setup hands)`)
    await w.clock.advance(30_000)
    await w.settle()
    expect(w.handsHelpers()).toHaveLength(1)
  })

  test('an unsupported platform stops it for good: no restart offered, and the reason said once', async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    const hands = w.lastHands()
    hands.hello(HANDS_PORT)
    hands.event({ type: 'state', state: 'starting' })
    hands.event({ type: 'error', code: 'unsupported_platform', message: UNSUPPORTED, hint: UNSUPPORTED_HINT, fatal: true })
    hands.event({ type: 'state', state: 'error' })
    hands.exit(1)
    await w.settle(20)
    expect(w.status()).toBe(`${VOICE_READY} · hands stopped · ${UNSUPPORTED}`)
    await w.clock.advance(30_000)
    await w.settle()
    expect(w.handsHelpers()).toHaveLength(1)
    const text = await jarvis($, 'hands')
    expect(text).toContain(`Hand control: stopped (${UNSUPPORTED})\n`)
    expect(text).not.toContain('kept failing')
  })

  test('already_running: active in another window, what to do about it, and no retry until the user asks', async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    const hands = w.lastHands()
    hands.event({ type: 'error', code: 'already_running', message: 'Hand control is running in another window', fatal: true })
    hands.exit(3)
    await w.settle()
    expect(w.status()).toBe(`${VOICE_READY} · hands active in another window`)
    expect(w.toasts.filter(toast => toast === ELSEWHERE)).toHaveLength(1)
    await w.clock.advance(60_000)
    await w.settle()
    expect(w.handsHelpers()).toHaveLength(1)
    expect(await jarvis($, 'hands pause')).toBe(ELSEWHERE)
    expect(await jarvis($, 'hands')).toContain(
      'Hand control: running in another Claude Code window. Turn it off there (/jarvis hands off), then run /jarvis hands restart here.',
    )

    // on tries again (that window may have closed) and says it may not take.
    expect(await jarvis($, 'hands on')).toBe(
      'Hand control is on, but it was running in another Claude Code window; trying again here now. If that window still has it, turn it off there (/jarvis hands off), then run /jarvis hands restart here.',
    )
    await w.settle()
    expect(w.handsHelpers()).toHaveLength(2)
    w.lastHands().exit(3)
    await w.settle()
    expect(w.status()).toBe(`${VOICE_READY} · hands active in another window`)
    expect(w.toasts.filter(toast => toast === ELSEWHERE)).toHaveLength(2)
  })

  test('a reloaded module shuts down the hand helper the previous load left running', async ($, on) => {
    const w = handsWorld(on)
    w.state.set('jarvis.handsHelper', { port: 40001, token: 'old-hands-token', pid: 98 })
    await startSession($, w)
    const shutdown = w.named('shutdown')
    expect(shutdown.map(command => command.url)).toEqual(['http://127.0.0.1:40001/v1/shutdown'])
    expect(shutdown[0]?.headers.authorization).toBe('Bearer old-hands-token')
    expect(w.handsHelpers()).toHaveLength(0) // gives it a second to exit
    await w.clock.advance(1000)
    await w.settle()
    expect(w.handsHelpers()).toHaveLength(1)
    expect(w.state.get('jarvis.handsHelper')).toBeNull()

    // If it still holds the lock, a late retry outlives its watchdog.
    w.lastHands().exit(3)
    await w.settle()
    expect(w.status()).toBe('JARVIS · starting · hands restarting · waiting for the previous hand helper to exit')
    await w.clock.advance(ORPHAN_RETRY_MS)
    await w.settle()
    expect(w.handsHelpers()).toHaveLength(2)
  })

  test('setup during the first start\'s wait on a previous helper starts the helper when it is done', async ($, on) => {
    const w = world(on)
    w.store.set('handsEnabled', ON)
    w.state.set('jarvis.handsHelper', PREVIOUS_HANDS)
    await startHelper($, w)
    installable(w)
    expect(await jarvis($, 'setup hands')).toContain('Setting up hand control')
    await w.clock.advance(1000)
    await w.settle(40)
    expect(w.logs).toContain('Hand control installed (Ready: hand model (7.8 MB)). Starting the hand helper.')
    expect(w.handsHelpers()).toHaveLength(1)
    expect(w.status()).toBe(`${VOICE_READY} · hands starting`)
  })

  test('off during the first start\'s wait on a previous helper ends it, and the next on starts the helper', async ($, on) => {
    const w = world(on)
    w.store.set('handsEnabled', ON)
    w.state.set('jarvis.handsHelper', PREVIOUS_HANDS)
    await startHelper($, w)
    const off = jarvis($, 'hands off')
    await w.settle()
    await w.clock.advance(1000)
    await w.settle()
    expect(await off).toBe('Hand control is off and the camera is closed.')
    expect(w.status()).toBe(VOICE_READY)

    w.existing.add(HANDS_PYTHON) // set up meanwhile
    expect(await jarvis($, 'hands on')).toContain('The camera starts in a moment')
    await w.settle()
    expect(w.handsHelpers()).toHaveLength(1)
    expect(w.status()).toBe(`${VOICE_READY} · hands starting`)
  })

  test('never starts in a cloud session, and offers no tool there', async ($, on) => {
    const w = world(on, { env: { HOME: '/root', CLAUDE_CODE_REMOTE: 'true' } })
    w.store.set('handsEnabled', ON)
    await startSession($, w)
    expect(w.children).toHaveLength(0)
    expect(w.tools).toEqual([])
    expect(await jarvis($, 'hands on')).toBe(NOT_LOCAL)
    expect(await jarvis($, 'setup hands')).toBe(NOT_LOCAL)
    expect(await jarvis($, 'hands')).toContain(NOT_LOCAL)
  })
})

describe('/jarvis hands', () => {
  test('on starts the camera and off stops it; the voice helper is left alone', async ($, on) => {
    const w = world(on)
    w.existing.add(HANDS_PYTHON)
    await startHelper($, w)
    exitOnShutdown(w)
    expect(await jarvis($, 'hands on')).toBe(
      'Hand control is on. The camera starts in a moment; then hold an open palm toward the camera for half a second to take the cursor. Nothing leaves this computer.',
    )
    await w.settle()
    expect(w.store.get('handsEnabled')).toEqual(ON)
    expect(w.handsHelpers()).toHaveLength(1)
    await handsReady(w)
    expect(await jarvis($, 'hands on')).toBe('Hand control is already on.')

    expect(await jarvis($, 'hands off')).toBe('Hand control is off and the camera is closed.')
    expect(w.handsNamed('shutdown')).toHaveLength(1)
    expect(w.store.has('handsEnabled')).toBe(false)
    await w.settle()
    expect(w.status()).toBe(VOICE_READY)
    expect(w.lastHelper().hasExited).toBe(false)
    expect(w.named('shutdown')).toHaveLength(1)
  })

  test('on before setup says how to set it up', async ($, on) => {
    const w = world(on)
    await startHelper($, w)
    expect(await jarvis($, 'hands on')).toBe(
      'Hand control is on, but it is not set up yet. Run /jarvis setup hands (it downloads about 500 MB); the camera starts when it is done.',
    )
    expect(w.store.get('handsEnabled')).toEqual(ON)
    expect(w.handsHelpers()).toHaveLength(0)
    expect(w.status()).toBe(`${VOICE_READY} · hands not set up · /jarvis setup hands`)
    expect(await jarvis($, 'hands off')).toBe('Hand control is off and the camera is closed.')
    expect(w.status()).toBe(VOICE_READY)
  })

  test('status: the state, the camera, the displays, how to start and the gestures', async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    await handsReady(w)
    w.respond = command =>
      command.url === `http://127.0.0.1:${HANDS_PORT}/v1/status`
        ? {
            status: 200,
            body: {
              ok: true,
              state: 'idle',
              version: '0.1.0',
              platform: 'windows',
              camera: 'UGREEN Camera',
              engaged: false,
              fps: 29.84,
              inferMs: 11.6,
              displays: [DISPLAYS[0], { ...DISPLAYS[1], used: false }],
              calibrated: true,
              settings: { engage: 'palm', hand: 'any', anchor: 'knuckles', overlay: true, scrollSpeed: 1 },
            },
          }
        : { status: 200, body: { ok: true } }
    const text = await jarvis($, 'hands')
    expect(text).toContain('Hand control: ready\n')
    expect(text).toContain('Helper 0.1.0 (pid 4242) · camera UGREEN Camera at 1280x720 · 29.8 fps, 12 ms per frame · calibrated')
    expect(text).toContain('  1  DELL U2720Q · 2560x1440 at 0,0 · primary · in use')
    expect(text).toContain('  2  EPSON Projector · 1920x1080 at 2560,0 · not used')
    expect(text).toContain(
      'Hand control starts when you hold an open palm toward the camera for half a second (/jarvis hands engage always switches).',
    )
    expect(text).toContain('Pinch thumb and index finger: click; pinch and move: drag; pinch twice: double-click')
    expect(text).toContain('/jarvis setup hands                install hand control (about 500 MB)')
  })

  test('status while off says how to turn it on and set it up', async ($, on) => {
    const w = world(on)
    await startHelper($, w)
    const text = await jarvis($, 'hands status')
    expect(text).toContain('Hand control is off. Turn it on with /jarvis hands on')
    expect(text).toContain('It is not set up yet: run /jarvis setup hands first (about 500 MB).')
    expect(w.commands.some(command => command.url.includes(`:${HANDS_PORT}/`))).toBe(false)
  })

  test('display chooses the displays the hand reaches and tells the running helper', async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    await handsReady(w)
    expect(await jarvis($, 'hands display 2')).toBe('Hand control now reaches display 2 (EPSON Projector).')
    expect(w.store.get('handsDisplays')).toEqual([2])
    expect(w.handsNamed('config').at(-1)?.body).toEqual({ displays: [2] })

    expect(await jarvis($, 'hands display 1, 2')).toBe('Hand control now reaches displays 1 (DELL U2720Q) and 2 (EPSON Projector).')
    expect(w.store.get('handsDisplays')).toEqual([1, 2])
    expect(await jarvis($, 'hands display 3')).toBe('There is no display 3. The displays are 1 (DELL U2720Q), 2 (EPSON Projector).')
    expect(await jarvis($, 'hands display left')).toBe(
      '"left" is not a display. Use /jarvis hands display all, a display number such as 2, or a list such as 1,2.',
    )
    expect(await jarvis($, 'hands display')).toContain('Hand control reaches displays 1 (DELL U2720Q) and 2 (EPSON Projector).')

    expect(await jarvis($, 'hands display all')).toBe('Hand control now reaches all displays.')
    expect(w.store.has('handsDisplays')).toBe(false)
    expect(w.handsNamed('config').at(-1)?.body).toEqual({ displays: 'all' })

    w.respond = () => ({ status: 400, body: { ok: false, error: { code: 'bad_request', message: 'display ids start at 1' } } })
    expect(await jarvis($, 'hands display 1')).toBe('Saved, but the hand helper refused it: display ids start at 1')
  })

  test('display without a running helper is saved for its next start', async ($, on) => {
    const w = world(on)
    await startHelper($, w)
    expect(await jarvis($, 'hands display 2')).toBe('Hand control now reaches display 2; it applies when hand control starts.')
    expect(w.store.get('handsDisplays')).toEqual([2])
    expect(await jarvis($, 'hands display all')).toBe(
      'Hand control now reaches every display except virtual ones; it applies when hand control starts.',
    )
  })

  test('display all leaves a virtual display out, as the helper does', async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    const hands = w.lastHands()
    hands.hello(HANDS_PORT)
    const parsec = { id: 3, name: 'Parsec Virtual Display', x: 4480, y: 0, width: 1920, height: 1080, primary: false, virtual: true, used: false }
    hands.event({ type: 'ready', camera: 'UGREEN Camera', width: 1280, height: 720, fps: 30, displays: [...DISPLAYS, parsec] })
    hands.event({ type: 'state', state: 'idle' })
    await w.settle()
    expect(await jarvis($, 'hands display all')).toBe(
      'Hand control now reaches every display except virtual display 3 (Parsec Virtual Display).',
    )
  })

  test('engage switches between an open palm and any hand', async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    await handsReady(w)
    expect(await jarvis($, 'hands engage always')).toBe(
      'Any hand in view takes the cursor at once, with no open palm needed (for a projector room).',
    )
    expect(w.store.get('handsEngage')).toBe('always')
    expect(w.handsNamed('config').at(-1)?.body).toEqual({ engage: 'always' })
    expect(w.status()).toBe(`${VOICE_READY} · hands ready · raise a hand to start`)
    expect(await jarvis($, 'hands engage')).toBe(
      'Any hand in view takes the cursor at once, with no open palm needed (for a projector room). Switch with /jarvis hands engage palm or /jarvis hands engage always.',
    )
    expect(await jarvis($, 'hands engage wave')).toBe('Unknown choice "wave". Use /jarvis hands engage palm or /jarvis hands engage always.')
    expect(await jarvis($, 'hands engage palm')).toBe(
      'Hand control starts when you hold an open palm toward the camera for half a second.',
    )
    expect(w.status()).toBe(`${VOICE_READY} · hands ready · open palm to start`)
  })

  test('pause and resume release and reopen the camera', async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    await handsReady(w)
    expect(await jarvis($, 'hands pause')).toBe(PAUSED)
    expect(await jarvis($, 'hands resume')).toBe(RESUMED)
    expect(w.handsNamed('pause')).toHaveLength(1)
    expect(w.handsNamed('resume')).toHaveLength(1)
    w.respond = () => ({ status: 503, body: { ok: false, error: { code: 'camera_in_use', message: 'The camera is in use by another app.' } } })
    expect(await jarvis($, 'hands resume')).toBe('Could not resume hand control: The camera is in use by another app.')
  })

  test('pause and resume wait longer than the helper does for the camera before giving up', async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    await handsReady(w)
    // The helper answers within its own waits (about 4 s to close the camera, 9 s to open it).
    const release = slowAnswers(w, ['pause', 'resume'])
    const paused = jarvis($, 'hands pause')
    await w.settle()
    await w.clock.advance(6000)
    await w.settle()
    release()
    expect(await paused).toBe(PAUSED)

    const resumed = jarvis($, 'hands resume')
    await w.settle()
    await w.clock.advance(12_000)
    await w.settle()
    release()
    expect(await resumed).toBe(RESUMED)
  })

  test('a resume the camera is still opening for says so, then says when it is on', async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    const hands = await handsReady(w)
    expect(await jarvis($, 'hands pause')).toBe(PAUSED)
    hands.event({ type: 'state', state: 'paused' })
    await w.settle()

    answer(w, 'resume', { ok: true, pending: true })
    expect(await jarvis($, 'hands resume')).toBe(
      'Resuming hand control: the camera is still opening, which can take up to about 20 seconds. A message says when it is on, or why it could not open.',
    )
    expect(w.status()).toBe(`${VOICE_READY} · hands paused`)
    expect(w.toasts).not.toContain(RESUMED)
    // Asked again meanwhile: the helper is no longer paused, so it answers plain ok, but the camera is still opening.
    answer(w, 'resume', { ok: true })
    expect(await jarvis($, 'hands resume')).toContain('the camera is still opening')
    hands.event({ type: 'state', state: 'idle' })
    await w.settle()
    expect(w.toasts.filter(toast => toast === RESUMED)).toHaveLength(1)
    expect(w.status()).toBe(`${VOICE_READY} · hands ready · open palm to start`)
    // The next state change is not a resume.
    hands.event({ type: 'state', state: 'active' })
    hands.event({ type: 'state', state: 'idle' })
    await w.settle()
    expect(w.toasts.filter(toast => toast === RESUMED)).toHaveLength(1)
  })

  test('a camera that fails after a pending resume is toasted and shown in the status line', async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    const hands = await handsReady(w)
    // Teams had the camera a moment ago: that error was toasted already.
    hands.event(IN_USE)
    await w.settle()
    expect(await jarvis($, 'hands pause')).toBe(PAUSED)
    hands.event({ type: 'state', state: 'paused' })
    await w.settle()

    answer(w, 'resume', { ok: true, pending: true })
    expect(await jarvis($, 'hands resume')).toContain('the camera is still opening')
    // The open fails after the helper answered: it stays paused and says why, though that was toasted a moment ago.
    hands.event(IN_USE)
    await w.settle()
    expect(w.toasts.filter(toast => toast === IN_USE_TOAST)).toHaveLength(2)
    expect(w.status()).toBe(`${VOICE_READY} · hands paused · The camera is in use by another app (close Teams or Zoom)`)
    expect(await jarvis($, 'hands')).toContain(
      'Hand control: paused: the camera is off until /jarvis hands resume (The camera is in use by another app (close Teams or Zoom))',
    )
    // Opened at last: the reason goes.
    answer(w, 'resume', { ok: true })
    expect(await jarvis($, 'hands resume')).toBe(RESUMED)
    hands.event({ type: 'state', state: 'idle' })
    await w.settle()
    expect(w.status()).toBe(`${VOICE_READY} · hands ready · open palm to start`)
  })

  test('a resume of a helper that started paused, its camera never opened, says the camera is still opening', async ($, on) => {
    const w = handsWorld(on)
    w.store.set('handsPaused', true) // paused yesterday
    await startHelper($, w)
    const hands = w.lastHands()
    hands.hello(HANDS_PORT)
    hands.event({ type: 'state', state: 'starting' })
    // Teams had the camera a moment ago: that error was toasted already.
    hands.event(IN_USE)
    // The pause came before the camera opened, which kept it closed.
    hands.event({ type: 'state', state: 'paused' })
    await w.settle()
    // Never opened, so the helper's resume says "starting" before it waits 9 s for the open, then answers pending.
    let release: (() => void) | undefined
    w.respond = command => {
      if (command.name !== 'resume') return { status: 200, body: { ok: true } }
      hands.event({ type: 'state', state: 'starting' })
      return new Promise<Answer>(resolve => {
        release = () => resolve({ status: 200, body: { ok: true, pending: true } })
      })
    }
    const resumed = jarvis($, 'hands resume')
    await w.settle()
    await w.clock.advance(9000)
    await w.settle()
    release?.()
    expect(await resumed).toContain('the camera is still opening')
    expect(w.toasts).not.toContain(RESUMED)
    // Asked again meanwhile, it is not "on again" either.
    answer(w, 'resume', { ok: true })
    expect(await jarvis($, 'hands resume')).toContain('the camera is still opening')
    // It cannot open: paused again, and the user waits for why, so it is toasted although it was a moment ago.
    hands.event({ type: 'state', state: 'paused' })
    hands.event(IN_USE)
    await w.settle()
    expect(w.toasts.filter(toast => toast === IN_USE_TOAST)).toHaveLength(2)
    expect(w.toasts).not.toContain(RESUMED)
    expect(w.status()).toBe(`${VOICE_READY} · hands paused · The camera is in use by another app (close Teams or Zoom)`)
  })

  test('a pending resume\'s own "starting" is not "on again"; the camera opening is, once', async ($, on) => {
    const w = handsWorld(on)
    w.store.set('handsPaused', true)
    await startHelper($, w)
    const hands = w.lastHands()
    hands.hello(HANDS_PORT)
    hands.event({ type: 'state', state: 'starting' })
    hands.event({ type: 'state', state: 'paused' })
    await w.settle()
    answer(w, 'resume', { ok: true, pending: true })
    expect(await jarvis($, 'hands resume')).toContain('the camera is still opening')
    // The resume's "starting" reaches the mod after its answer.
    hands.event({ type: 'state', state: 'starting' })
    await w.settle()
    expect(w.toasts).not.toContain(RESUMED)
    hands.event({ type: 'ready', camera: 'UGREEN Camera', width: 1280, height: 720, fps: 30, displays: DISPLAYS })
    hands.event({ type: 'state', state: 'idle' })
    await w.settle()
    expect(w.toasts.filter(toast => toast === RESUMED)).toHaveLength(1)
    expect(w.status()).toBe(`${VOICE_READY} · hands ready · open palm to start`)
  })

  test('status of a helper that started paused says the camera is off, not opening', async ($, on) => {
    const w = handsWorld(on)
    w.store.set('handsPaused', true)
    await startHelper($, w)
    const hands = w.lastHands()
    hands.hello(HANDS_PORT)
    hands.event({ type: 'state', state: 'starting' })
    hands.event({ type: 'state', state: 'paused' })
    await w.settle()
    // It never opened a camera, so its status names none.
    answer(w, 'status', { ok: true, state: 'paused', fps: 0, inferMs: 0, calibrated: true, displays: DISPLAYS })
    const text = await jarvis($, 'hands')
    expect(text).toContain('Hand control: paused: the camera is off until /jarvis hands resume')
    expect(text).not.toContain('opening the camera')
    expect(text).toContain('camera off while paused')
  })

  test('a pause while the camera is still starting says it turns off once started', async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    await handsReady(w)
    answer(w, 'pause', { ok: true, pending: true })
    expect(await jarvis($, 'hands pause')).toBe(
      'Pausing hand control: the camera is still starting, and it turns off as soon as it has started. /jarvis hands resume turns it back on.',
    )
    expect(w.store.get('handsPaused')).toBe(true)
  })

  test('a pause is remembered: a restarted helper is paused at its hello, before its config, until resume', async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    const first = await handsReady(w)
    expect(await jarvis($, 'hands pause')).toBe(PAUSED)
    expect(w.store.get('handsPaused')).toBe(true)
    first.event({ type: 'state', state: 'paused' })
    first.exit(1)
    await w.settle()
    await w.clock.advance(BACKOFF_MS[0] ?? 1000)
    await w.settle()
    expect(w.handsHelpers()).toHaveLength(2)
    const before = handsSent(w).length
    w.lastHands().hello(HANDS_PORT)
    await w.settle()
    // The helper keeps the camera off when pause comes before its start.
    expect(handsSent(w).slice(before)).toEqual(['pause', 'config'])

    expect(await jarvis($, 'hands resume')).toBe(RESUMED)
    expect(w.store.has('handsPaused')).toBe(false)
    w.lastHands().exit(1)
    await w.settle()
    await w.clock.advance(BACKOFF_MS[1] ?? 2000)
    await w.settle()
    expect(w.handsHelpers()).toHaveLength(3)
    const after = handsSent(w).length
    w.lastHands().hello(HANDS_PORT)
    await w.settle()
    expect(handsSent(w).slice(after)).toEqual(['config'])
  })

  test('a pause outlives the session; off, on and resume forget it', async ($, on) => {
    const w = handsWorld(on)
    w.store.set('handsPaused', true)
    await startHelper($, w)
    w.lastHands().hello(HANDS_PORT)
    await w.settle()
    expect(handsSent(w)).toEqual(['pause', 'config'])

    exitOnShutdown(w)
    await jarvis($, 'hands off')
    expect(w.store.has('handsPaused')).toBe(false)

    w.store.set('handsPaused', true)
    await jarvis($, 'hands on')
    expect(w.store.has('handsPaused')).toBe(false)
    await w.settle()
    w.lastHands().hello(HANDS_PORT)
    await w.settle()
    expect(handsSent(w).filter(name => name === 'pause')).toHaveLength(1)

    // Resume while the helper is down: it opens the camera when it is back.
    w.store.set('handsPaused', true)
    w.lastHands().exit(1)
    await w.settle()
    expect(await jarvis($, 'hands resume')).toContain('The hand helper is not running')
    expect(w.store.has('handsPaused')).toBe(false)
  })

  test('a camera change while paused restarts the helper still paused', async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    const first = await handsReady(w)
    exitOnShutdown(w)
    expect(await jarvis($, 'hands pause')).toBe(PAUSED)
    first.event({ type: 'state', state: 'paused' })
    await w.settle()
    expect(await jarvis($, 'hands camera UGREEN')).toBe(
      'Camera set to "UGREEN". The hand helper restarts with it, still paused: /jarvis hands resume opens the camera.',
    )
    await w.settle(20)
    expect(w.handsHelpers()).toHaveLength(2)
    const before = handsSent(w).length
    w.lastHands().hello(HANDS_PORT)
    await w.settle()
    expect(handsSent(w).slice(before)).toEqual(['pause', 'config'])
  })

  test('on while paused turns the camera back on', async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    const hands = await handsReady(w)
    await jarvis($, 'hands pause')
    hands.event({ type: 'state', state: 'paused' })
    await w.settle()
    expect(await jarvis($, 'hands on')).toBe(RESUMED)
    expect(w.handsNamed('resume')).toHaveLength(1)
    expect(w.store.has('handsPaused')).toBe(false)
  })

  test('commands that need the helper explain why it is not there', async ($, on) => {
    const w = world(on)
    await startHelper($, w)
    expect(await jarvis($, 'hands pause')).toBe('Hand control is off. Turn it on with /jarvis hands on.')
    expect(await jarvis($, 'hands calibrate')).toBe('Hand control is off. Turn it on with /jarvis hands on.')
    expect(await jarvis($, 'hands restart')).toBe('Hand control is off. Turn it on with /jarvis hands on.')
    w.store.set('handsEnabled', ON)
    expect(await jarvis($, 'hands pause')).toBe('The hand helper is not running; /jarvis hands restart starts it.')
  })

  test('camera picks a webcam by name and restarts the helper to open it', async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    await handsReady(w)
    exitOnShutdown(w)
    expect(await jarvis($, 'hands camera')).toContain('Camera: the first one (in use: UGREEN Camera).')
    expect(await jarvis($, 'hands camera UGREEN Camera')).toBe('Camera set to "UGREEN Camera". The hand helper restarts to open it.')
    await w.settle(20)
    expect(w.store.get('handsCamera')).toBe('UGREEN Camera')
    expect(w.handsHelpers()).toHaveLength(2)
    expect(w.lastHands().argv.slice(-2)).toEqual(['--camera', 'UGREEN Camera'])

    // The new helper has not said hello yet, and opens the old choice: it starts again.
    expect(await jarvis($, 'hands camera default')).toBe('Camera set to the first camera. The hand helper starts again to open it.')
    expect(w.store.has('handsCamera')).toBe(false)
    // It is asked to end, then let go of (this fake one ignores the kill).
    for (let wait = 0; wait < 2; wait += 1) {
      await w.clock.advance(STOP_WAIT_MS)
      await w.settle(20)
    }
    expect(w.handsHelpers()).toHaveLength(3)
    expect(w.lastHands().argv).not.toContain('--camera')
  })

  test('off while a camera change or a restart is stopping the helper keeps it stopped', async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    let first = await handsReady(w)
    expect(await jarvis($, 'hands camera UGREEN')).toBe('Camera set to "UGREEN". The hand helper restarts to open it.')
    await w.settle()
    expect(w.handsNamed('shutdown')).toHaveLength(1)
    // The helper takes a moment to close the camera and exit; the user turns hand control off meanwhile.
    const off = jarvis($, 'hands off')
    await w.settle()
    first.exit(0)
    await w.settle(20)
    expect(await off).toBe('Hand control is off and the camera is closed.')
    expect(w.handsHelpers()).toHaveLength(1)
    expect(w.status()).toBe(VOICE_READY)

    // The same with a restart and the tool's off.
    await jarvis($, 'hands on')
    await w.settle()
    first = await handsReady(w)
    expect(await jarvis($, 'hands restart')).toBe('Restarting the hand helper.')
    await w.settle()
    await allowedTurn($, w, HANDS_TOOL_ID)
    const toolOff = $.tool.call({ tool: 'mcp__jarvis__hands', action: 'off' })
    await w.settle()
    first.exit(0)
    await w.settle(20)
    expect((await toolOff).result).toBe('Hand control is off and the camera is closed.')
    expect(w.handsHelpers()).toHaveLength(2)
    expect(w.lastHands().hasExited).toBe(true)
    expect(w.status()).toBe(VOICE_READY)
  })

  test('setup during a pending restart installs before any helper starts', async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    const first = await handsReady(w)
    w.existing.add(WINGET_UV)
    let uv: FakeChild | undefined
    w.onSpawn = child => {
      if (child.argv[0] === WINGET_UV) uv = child // uv runs for a while
      else if (child.argv.includes('setup')) child.exit(0)
    }
    expect(await jarvis($, 'hands restart')).toBe('Restarting the hand helper.')
    await w.settle()
    expect(await jarvis($, 'setup hands')).toContain('Setting up hand control')
    await w.settle()
    first.exit(0) // the old helper exits after the shutdown both asked for
    await w.settle(20)
    expect(uv?.hasExited).toBe(false)
    expect(w.handsHelpers()).toHaveLength(1)
    uv?.exit(0)
    await w.settle(40)
    expect(w.handsHelpers()).toHaveLength(2)
    expect(w.status()).toBe(`${VOICE_READY} · hands starting`)
  })

  test('off in a window where another window runs the helper says it still runs there', async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    w.lastHands().event({ type: 'error', code: 'already_running', message: 'Hand control is running in another window', fatal: true })
    w.lastHands().exit(3)
    await w.settle()
    expect(w.status()).toBe(`${VOICE_READY} · hands active in another window`)
    expect(await jarvis($, 'hands off')).toBe(
      'Hand control is off here, but it still runs, with the camera, in another Claude Code window. Turn it off there with /jarvis hands off.',
    )
    expect(w.store.has('handsEnabled')).toBe(false)
    expect(w.status()).toBe(VOICE_READY)
  })

  test('camera takes a quoted name without its quotes; while off it waits for hand control', async ($, on) => {
    const w = world(on)
    w.existing.add(HANDS_PYTHON)
    await startHelper($, w)
    expect(await jarvis($, 'hands camera "UGREEN Camera"')).toBe('Camera set to "UGREEN Camera"; it applies when hand control starts.')
    expect(w.store.get('handsCamera')).toBe('UGREEN Camera')
    expect(await jarvis($, "hands camera 'Logitech BRIO'")).toBe('Camera set to "Logitech BRIO"; it applies when hand control starts.')
    expect(await jarvis($, 'hands camera ""')).toBe(
      '"" is not a camera name. Choose with /jarvis hands camera <number or part of its name>, or /jarvis hands camera default.',
    )
    expect(w.store.get('handsCamera')).toBe('Logitech BRIO')
    expect(w.handsHelpers()).toHaveLength(0)
  })

  test('camera after the helper gave up on a missing camera tries again with the new one', async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    await giveUp(w, { code: 'no_camera', message: 'No camera matches "0"' })
    expect(w.status()).toBe(`${VOICE_READY} · hands stopped · No camera matches "0" · /jarvis hands restart`)
    const count = w.handsHelpers().length
    expect(await jarvis($, 'hands camera UGREEN')).toBe('Camera set to "UGREEN". The hand helper starts again to open it.')
    await w.settle(20)
    expect(w.handsHelpers()).toHaveLength(count + 1)
    expect(w.lastHands().argv.slice(-2)).toEqual(['--camera', 'UGREEN'])
    expect(w.status()).toBe(`${VOICE_READY} · hands starting`)
  })

  test('an unknown subcommand shows the hands help', async ($, on) => {
    const w = world(on)
    await startHelper($, w)
    const text = await jarvis($, 'hands wave')
    expect(text).toContain('Unknown subcommand "wave" for /jarvis hands.')
    expect(text).toContain('/jarvis hands on|off               turn hand control (and the camera) on or off')
  })
})

describe('/jarvis setup hands', () => {
  test('installs with uv into its own venv, downloads the hand model, then starts the helper', async ($, on) => {
    const w = world(on)
    w.store.set('handsEnabled', ON)
    await startHelper($, w)
    w.existing.add(WINGET_UV)
    w.exists = path => w.existing.has(path) || HANDS_LOCK.test(path)
    w.onSpawn = child => {
      if (child.argv[0] === WINGET_UV) {
        child.stderr('Resolved 28 packages in 2ms\n')
        child.stderr('Installed 27 packages in 14.1s\n')
        w.existing.add(HANDS_PYTHON)
        child.exit(0)
      } else if (child.argv.includes('setup')) {
        child.stdout('{"v":1,"type":"progress","step":"download","pct":49.6,"message":"downloading the hand model"}\n')
        child.stderr('urllib3: GET hand_landmarker.task\n')
        child.stdout('{"v":1,"type":"progress","step":"done","pct":100,"message":"Ready: hand model (7.8 MB)"}\n')
        child.exit(0)
      }
    }

    const text = await jarvis($, 'setup hands')
    expect(text).toContain(`Setting up hand control with ${WINGET_UV}:`)
    expect(text).toContain(`${DATA_DIR}\\hands\\venv with MediaPipe and OpenCV`)
    expect(text).toContain('hand control starts when it is done')
    await w.settle(40)

    const uv = w.children.find(child => child.argv[0] === WINGET_UV)
    const project = uv?.argv[3] ?? ''
    expect(project).toMatch(/[\\/]hands$/)
    expect(uv?.argv).toEqual([
      WINGET_UV, 'sync', '--project', project, '--python', '3.12', '--no-dev', '--no-editable',
      '--reinstall-package', 'jarvis-hands', '--frozen',
    ])
    expect(uv?.request.cwd).toBe(project)
    expect(uv?.request.env).toEqual({
      UV_PROJECT_ENVIRONMENT: `${DATA_DIR}\\hands\\venv`,
      UV_CACHE_DIR: `${DATA_DIR}\\uv-cache`,
      UV_NO_PROGRESS: '1',
    })
    const model = w.children.find(child => child.argv.includes('setup'))
    expect(model?.argv).toEqual([HANDS_PYTHON, '-m', 'jarvis_hands', 'setup', '--data-dir', DATA_DIR])
    expect(model?.request.cwd).toBe(DATA_DIR)
    expect(w.existing.has(`${DATA_DIR}\\hands\\README.txt`)).toBe(true)
    // What it installed, so a later version of Jarvis can tell the venv is older than itself.
    expect(JSON.parse(w.files.get(HANDS_INSTALLED) ?? 'null')).toEqual({ pluginVersion: PLUGIN_VERSION })

    expect(w.statuses).toContain(`${VOICE_READY} · hands setting up · installing the hand helper (Python 3.12, MediaPipe and OpenCV)`)
    expect(w.statuses).toContain(`${VOICE_READY} · hands setting up · installing · Installed 27 packages in 14.1s`)
    expect(w.statuses).toContain(`${VOICE_READY} · hands setting up · downloading the hand model 50%`)
    expect(w.statuses).toContain(`${VOICE_READY} · hands setting up · Ready: hand model (7.8 MB)`)
    expect(w.statuses.some(line => line?.includes('urllib3') === true)).toBe(false)
    expect(w.logs).toContain('Hand control installed (Ready: hand model (7.8 MB)). Starting the hand helper.')
    expect(w.toasts).toContain('Hand control is installed. Starting the camera.')
    // Not installed at the session's start, this version's install after: nothing to update.
    expect(w.toasts).not.toContain(STALE_TOAST)

    expect(w.handsHelpers()).toHaveLength(1)
    expect(w.status()).toBe(`${VOICE_READY} · hands starting`)
    expect(w.helpers()).toHaveLength(1) // the voice helper ran on
    expect(w.lastHelper().hasExited).toBe(false)
  })

  test('while hand control is off it installs and says how to turn it on', async ($, on) => {
    const w = world(on)
    await startHelper($, w)
    w.existing.add(WINGET_UV)
    w.onSpawn = child => {
      if (child.argv[0] === WINGET_UV) w.existing.add(HANDS_PYTHON)
      if (!child.isHelperRun && !child.isHandsRun) child.exit(0)
    }
    expect(await jarvis($, 'setup hands cpu')).toContain('Then turn it on with /jarvis hands on.')
    await w.settle(40)
    expect(w.children.find(child => child.argv[0] === WINGET_UV)?.argv).not.toContain('--frozen')
    expect(w.logs).toContain('Hand control installed (installed). Turn it on with /jarvis hands on.')
    expect(w.handsHelpers()).toHaveLength(0)
    expect(w.status()).toBe(VOICE_READY)
  })

  test('stops a running hand helper before installing over it', async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    await handsReady(w)
    exitOnShutdown(w)
    w.existing.add(WINGET_UV)
    w.onSpawn = child => {
      if (!child.isHelperRun && !child.isHandsRun) child.exit(0)
    }
    await jarvis($, 'setup hands')
    await w.settle(40)
    expect(w.handsNamed('shutdown')).toHaveLength(1)
    expect(w.children.some(child => child.argv[0] === WINGET_UV)).toBe(true)
    expect(w.handsHelpers()).toHaveLength(2)
    expect(w.named('shutdown')).toHaveLength(1) // only the hand helper's
  })

  test('a hand helper an earlier version installed says once to run /jarvis setup hands, and still starts', async ($, on) => {
    const w = handsWorld(on)
    w.files.delete(HANDS_INSTALLED) // set up before the record was kept
    await startHelper($, w)
    const notice =
      'The hand helper was installed by an earlier version of Jarvis. Run /jarvis setup hands to update it; until then the old one runs.'
    // Said as it starts: an old helper may never get as far as hello with this mod.
    expect(w.logs).toContain(notice)
    expect(w.toasts).toContain(STALE_TOAST)
    await handsReady(w)
    expect(w.status()).toBe(`${VOICE_READY} · hands ready · open palm to start`)
    expect(await jarvis($, 'hands')).toContain(notice)

    // A restart does not say it again.
    w.lastHands().exit(1)
    await w.settle()
    await w.clock.advance(BACKOFF_MS[0] ?? 1000)
    await w.settle()
    await handsReady(w)
    expect(w.logs.filter(line => line === notice)).toHaveLength(1)
    expect(w.toasts.filter(toast => toast === STALE_TOAST)).toHaveLength(1)
  })

  test('a hand helper another version installed names both versions; this version\'s says nothing', async ($, on) => {
    const w = handsWorld(on)
    w.files.set(HANDS_INSTALLED, JSON.stringify({ pluginVersion: '0.4.0' }))
    await startHelper($, w)
    await handsReady(w)
    expect(w.logs).toContain(
      `The hand helper was installed by Jarvis 0.4.0, and this is Jarvis ${PLUGIN_VERSION}. Run /jarvis setup hands to update it; until then the old one runs.`,
    )
    // After /jarvis setup hands the record is this version's.
    installable(w)
    exitOnShutdown(w)
    await jarvis($, 'setup hands')
    await w.settle(40)
    expect(JSON.parse(w.files.get(HANDS_INSTALLED) ?? 'null')).toEqual({ pluginVersion: PLUGIN_VERSION })
    expect(await jarvis($, 'hands')).not.toContain('Run /jarvis setup hands to update it')
  })

  test('the installed version starting says nothing about updates', async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    await handsReady(w)
    expect(w.logs.some(line => line.includes('/jarvis setup hands'))).toBe(false)
    expect(w.toasts).not.toContain(STALE_TOAST)
  })

  test('/jarvis setup reinstalls an installed hand helper after the voice helper, then starts it again', async ($, on) => {
    const w = handsWorld(on)
    w.files.delete(HANDS_INSTALLED)
    await startHelper($, w)
    await handsReady(w)
    exitOnShutdown(w)
    w.existing.add(WINGET_UV)
    w.onSpawn = child => {
      if (!child.isHelperRun && !child.isHandsRun) child.exit(0)
    }
    const text = await jarvis($, 'setup')
    expect(text).toContain(`  3. then the hand helper in ${DATA_DIR}\\hands\\venv again, from this version of Jarvis`)
    await w.settle(80)

    const syncs = w.children.filter(child => child.argv[0] === WINGET_UV).map(child => child.argv.at(child.argv.indexOf('--reinstall-package') + 1))
    expect(syncs).toEqual(['jarvis-voice', 'jarvis-hands'])
    const models = w.children.filter(child => child.argv.includes('setup')).map(child => child.argv[0])
    expect(models).toEqual([VENV_PYTHON, HANDS_PYTHON])
    expect(w.handsNamed('shutdown')).toHaveLength(1)
    expect(w.helpers()).toHaveLength(2)
    expect(w.handsHelpers()).toHaveLength(2)
    expect(w.logs).toContain('Hand control reinstalled for this version of Jarvis (installed). Starting the hand helper.')
    expect(JSON.parse(w.files.get(HANDS_INSTALLED) ?? 'null')).toEqual({ pluginVersion: PLUGIN_VERSION })
    expect(w.status()).toBe('JARVIS · starting · hands starting')
  })

  test('/jarvis setup leaves hand control alone while it is not installed', async ($, on) => {
    const w = world(on)
    await startHelper($, w)
    exitOnShutdown(w)
    w.existing.add(WINGET_UV)
    w.onSpawn = child => {
      if (!child.isHelperRun && !child.isHandsRun) child.exit(0)
    }
    expect(await jarvis($, 'setup')).not.toContain('hand helper')
    await w.settle(40)
    expect(w.children.some(child => child.argv.includes('jarvis-hands'))).toBe(false)
    expect(w.logs.some(line => line.includes('Hand control'))).toBe(false)
  })

  test('/jarvis setup does not reinstall a hand helper another window runs', async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    w.lastHands().event({ type: 'error', code: 'already_running', message: 'Hand control is running in another window', fatal: true })
    w.lastHands().exit(3)
    await w.settle()
    exitOnShutdown(w)
    w.existing.add(WINGET_UV)
    w.onSpawn = child => {
      if (!child.isHelperRun && !child.isHandsRun) child.exit(0)
    }
    await jarvis($, 'setup')
    await w.settle(60)
    expect(w.children.some(child => child.argv.includes('jarvis-hands'))).toBe(false)
    expect(w.logs).toContain(
      'The hand helper was not reinstalled: another Claude Code window runs it, which keeps its files in use. Turn it off there (/jarvis hands off), then run /jarvis setup hands here.',
    )
    expect(w.status()).toBe('JARVIS · starting · hands active in another window')
  })

  test('without uv it prints the install command; a failed uv sync is reported', async ($, on) => {
    const w = world(on)
    w.store.set('handsEnabled', ON)
    await startHelper($, w)
    const missing = await jarvis($, 'setup hands')
    expect(missing).toContain('uv is not installed')
    expect(missing).toContain('winget install astral-sh.uv')
    expect(missing).toContain('run /jarvis setup hands again')
    expect(await jarvis($, 'setup hands gpu')).toBe('Unknown option "gpu" for /jarvis setup hands; it takes none.')

    w.existing.add(WINGET_UV)
    w.onSpawn = child => {
      child.stderr('error: Distribution `mediapipe==1.1.0` can\'t be installed\n')
      child.exit(2)
    }
    await jarvis($, 'setup hands')
    await w.settle(40)
    expect(w.logs).toContain("Hand control setup failed: uv sync failed (exit 2):\nerror: Distribution `mediapipe==1.1.0` can't be installed")
    expect(w.toasts).toContain('Hand control setup failed; the transcript has the details.')
    expect(w.status()).toBe(`${VOICE_READY} · hands not set up · /jarvis setup hands`)
  })
})

describe('the hands tool', () => {
  test('turns hand control on and off and reports it, with the commands\' words', async ($, on) => {
    const w = world(on)
    w.existing.add(HANDS_PYTHON)
    await startHelper($, w)
    exitOnShutdown(w)
    expect(w.tools).toEqual(['desktop', 'home_control', 'hands'])
    await allowedTurn($, w, HANDS_TOOL_ID)

    const turnedOn = await $.tool.call({ tool: 'mcp__jarvis__hands', action: 'on' })
    expect(turnedOn.result).toContain('Hand control is on. The camera starts in a moment')
    expect(w.store.get('handsEnabled')).toEqual(ON)
    await w.settle()
    await handsReady(w)

    const status = await $.tool.call({ tool: 'mcp__jarvis__hands', action: 'status' })
    expect(status.result).toBe(await jarvis($, 'hands status'))
    const calibrate = await $.tool.call({ tool: 'mcp__jarvis__hands', action: 'calibrate' })
    expect(calibrate.result).toContain('Calibrating')
    expect(w.handsNamed('calibrate')).toHaveLength(1)

    const off = await $.tool.call({ tool: 'mcp__jarvis__hands', action: 'off' })
    expect(off.result).toBe('Hand control is off and the camera is closed.')
    expect(w.store.has('handsEnabled')).toBe(false)

    const unknown = await $.tool.call({ tool: 'mcp__jarvis__hands', action: 'explode' })
    expect(unknown.result).toBe(
      'Unknown action "explode"; use one of on, off, status, calibrate, pause, resume, engage, disengage.',
    )
  })

  test('display chooses the displays as /jarvis hands display does, alone or with an action', async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    await handsReady(w)
    await allowedTurn($, w, HANDS_TOOL_ID)
    const call = async (input: Record<string, unknown>): Promise<string> =>
      String((await $.tool.call({ tool: 'mcp__jarvis__hands', ...input })).result)

    // "Jarvis, put hand control on the projector"
    expect(await call({ display: '2' })).toBe('Hand control now reaches display 2 (EPSON Projector).')
    expect(w.store.get('handsDisplays')).toEqual([2])
    expect(w.handsNamed('config').at(-1)?.body).toEqual({ displays: [2] })
    expect(await call({ display: [1, 2] })).toBe('Hand control now reaches displays 1 (DELL U2720Q) and 2 (EPSON Projector).')
    expect(await call({ display: 1 })).toBe('Hand control now reaches display 1 (DELL U2720Q).')
    expect(await call({ display: 'left' })).toBe(await jarvis($, 'hands display left'))

    const both = await call({ action: 'status', display: 'all' })
    expect(both.startsWith('Hand control now reaches all displays.\n\nHand control: ready\n')).toBe(true)
    expect(w.store.has('handsDisplays')).toBe(false)
    expect(await call({})).toBe('Give an action, a display, or both.')
  })

  test('engage and disengage take and let go of the cursor through the helper', async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    await handsReady(w)
    await allowedTurn($, w, HANDS_TOOL_ID)
    const call = async (action: string): Promise<string> =>
      String((await $.tool.call({ tool: 'mcp__jarvis__hands', action })).result)

    // "Jarvis, take the cursor"
    expect(await call('engage')).toBe('Hand control has the cursor: it follows your hand as soon as one is in view.')
    expect(await call('disengage')).toBe(
      'Hand control let go of the cursor. Hold an open palm toward the camera to take it again.',
    )
    expect(handsSent(w).filter(name => name === 'engage' || name === 'disengage')).toEqual(['engage', 'disengage'])
    expect(w.handsNamed('engage')[0]?.body).toEqual({})
    // The engage mode is not changed by either.
    expect(w.store.has('handsEngage')).toBe(false)

    answer(w, 'engage', { ok: false, error: { code: 'internal', message: 'boom' } })
    expect(await call('engage')).toBe('Could not take the cursor: boom')
  })

  test('the schema offers the display, the engage actions and the tuning', () => {
    const { properties } = HANDS_TOOL.inputSchema
    expect(properties.action.enum).toEqual(['on', 'off', 'status', 'calibrate', 'pause', 'resume', 'engage', 'disengage'])
    expect(Object.keys(properties)).toEqual(['action', 'display', 'setting', 'value', 'preset'])
    expect(properties.value.type).toEqual(['number', 'string'])
    expect(properties.preset.enum).toEqual(['precise', 'balanced', 'fast'])
    expect(HANDS_TOOL.inputSchema).not.toHaveProperty('required')
  })
})

// ---- Tuning: the sensitivity knobs ----

/** What the helper's KNOBS (settings.py) and the protocol schema say of each knob: [min, max, default]. */
const CONTRACT: Record<string, [number, number, number]> = {
  cursorSpeed: [0.7, 3, 1],
  smoothing: [0.2, 3, 1],
  pinch: [0.85, 1.15, 1],
  fist: [0.9, 1.1, 1],
  engageSeconds: [0.1, 2, 0.5],
  dragDistance: [0.7, 4, 1],
  flingSensitivity: [0.5, 2.5, 1],
  scrollSpeed: [0.1, 10, 1],
  deadZone: [0, 8, 1],
}

const TUNING_KEY = 'handsTuning'
const BAD_REQUEST = { status: 400, body: { ok: false, error: { code: 'bad_request', message: 'cursorSpeed must be between 0.5 and 2.5' } } }

/** The config commands the hand helper got, by body, in order. */
const configBodies = (w: World): Record<string, unknown>[] => w.handsNamed('config').map(command => command.body)

/** The hand helper refuses every config command (`when` picks which). */
function refuseConfig(w: World, when: (body: Record<string, unknown>) => boolean = () => true): void {
  w.respond = command => (command.name === 'config' && when(command.body) ? BAD_REQUEST : { status: 200, body: { ok: true } })
}

describe('the sensitivity knobs', () => {
  test('the table says what the helper says, and its words and names do not collide', () => {
    expect(KNOBS.map(knob => knob.key)).toEqual(Object.keys(CONTRACT))
    for (const knob of KNOBS) {
      expect([knob.min, knob.max, knob.default]).toEqual(CONTRACT[knob.key])
      expect(knob.min).toBeLessThan(knob.max)
      expect(knob.default).toBeGreaterThanOrEqual(knob.min)
      expect(knob.default).toBeLessThanOrEqual(knob.max)
      expect(knob.higher).not.toBe('')
      expect(knob.label).not.toBe('')
    }
    // Every word that names a knob (its name, key, label, aliases) names exactly one.
    const owners = new Map<string, string>()
    for (const knob of KNOBS) {
      for (const word of [knob.name, knob.key, knob.label, ...knob.aliases]) {
        const found = findKnob(word)
        expect(found?.key).toBe(knob.key)
        const norm = word.toLowerCase().replace(/[^a-z0-9]/g, '')
        const owner = owners.get(norm)
        expect(owner === undefined || owner === knob.key).toBe(true)
        owners.set(norm, knob.key)
      }
    }
  })

  test('findKnob takes the natural ways of writing a name', () => {
    expect(findKnob('speed')?.key).toBe('cursorSpeed')
    expect(findKnob('Cursor Speed')?.key).toBe('cursorSpeed')
    expect(findKnob('cursor_speed')?.key).toBe('cursorSpeed')
    expect(findKnob('CURSORSPEED')?.key).toBe('cursorSpeed')
    expect(findKnob('cursor-speed')?.key).toBe('cursorSpeed')
    expect(findKnob('smooth')?.key).toBe('smoothing')
    expect(findKnob('click')?.key).toBe('pinch')
    expect(findKnob('grab')?.key).toBe('fist')
    expect(findKnob('deadzone')?.key).toBe('deadZone')
    expect(findKnob('scroll')?.key).toBe('scrollSpeed')
    expect(findKnob('engageSeconds')?.key).toBe('engageSeconds')
    expect(findKnob('')).toBeUndefined()
    expect(findKnob('warp')).toBeUndefined()
    expect(findKnob('cursor speed fast')).toBeUndefined()
  })

  test('values are plain decimals inside the range; anything else says why', () => {
    const speed = findKnob('speed')
    if (speed === undefined) throw new Error('no speed knob')
    expect(parseKnobValue(speed, '1.5')).toEqual({ ok: true, value: 1.5 })
    expect(parseKnobValue(speed, ' 2 ')).toEqual({ ok: true, value: 2 })
    expect(parseKnobValue(speed, '.7')).toEqual({ ok: true, value: 0.7 })
    expect(parseKnobValue(speed, '3')).toEqual({ ok: true, value: 3 })
    expect(parseKnobValue(speed, 1.25)).toEqual({ ok: true, value: 1.25 })
    expect(parseKnobValue(speed, '1.23456')).toEqual({ ok: true, value: 1.235 })
    expect(parseKnobValue(speed, 'default')).toEqual({ ok: true, value: 'default' })
    expect(parseKnobValue(speed, 'Default')).toEqual({ ok: true, value: 'default' })
    for (const text of ['abc', '', '  ', '1e0', '0x10', 'NaN', 'Infinity', '-Infinity', '1.5.2', '1,5', 'true', '--1']) {
      expect(parseKnobValue(speed, text)).toEqual({ ok: false, why: 'not_number' })
    }
    for (const value of [Number.NaN, Number.POSITIVE_INFINITY, true, null, {}, [1]]) {
      expect(parseKnobValue(speed, value)).toEqual({ ok: false, why: 'not_number' })
    }
    for (const value of ['0.69', '3.01', '99', '-1', '0', 0.5, 3.5]) {
      expect(parseKnobValue(speed, value)).toEqual({ ok: false, why: 'range' })
    }
    // A zero is a fine dead zone.
    const dead = findKnob('dead-zone')
    if (dead === undefined) throw new Error('no dead zone knob')
    expect(parseKnobValue(dead, '0')).toEqual({ ok: true, value: 0 })
    expect(parseKnobValue(dead, '8.01')).toEqual({ ok: false, why: 'range' })
  })

  test('a very long value is answered at once, not matched in quadratic time', () => {
    const speed = findKnob('speed')
    if (speed === undefined) throw new Error('no speed knob')
    // A pattern that backtracks takes seconds on this (8 s for 100,000 digits), before any permission check.
    expect(parseKnobValue(speed, `${'9'.repeat(200_000)}x`)).toEqual({ ok: false, why: 'not_number' })
    expect(parseKnobValue(speed, `${'1'.repeat(200_000)}.${'1'.repeat(200_000)}x`)).toEqual({ ok: false, why: 'not_number' })
    expect(parseKnobValue(speed, `2.${'0'.repeat(200_000)}`)).toEqual({ ok: true, value: 2 })
  })

  test('the presets name known knobs with values in range, and balanced is every default', () => {
    expect(Object.keys(PRESETS)).toEqual(['precise', 'balanced', 'fast'])
    expect(PRESETS.balanced).toEqual({})
    for (const preset of Object.values(PRESETS)) {
      for (const [key, value] of Object.entries(preset)) {
        const knob = KNOBS.find(one => one.key === key)
        expect(knob).toBeDefined()
        expect(value).toBeGreaterThanOrEqual(knob?.min ?? 0)
        expect(value).toBeLessThanOrEqual(knob?.max ?? 0)
      }
    }
    expect(PRESETS.precise).toEqual({ cursorSpeed: 0.8, smoothing: 1.8, deadZone: 2, dragDistance: 1.5, pinch: 0.9 })
    expect(PRESETS.fast).toEqual({ cursorSpeed: 1.6, smoothing: 0.5, deadZone: 0, pinch: 1.1 })
  })

  test('the everyday knobs are the ones with a plugin setting (plugin.json declares the same four)', () => {
    // Tests cannot read plugin.json; keep this list and its userConfig in step by hand.
    const everyday = KNOBS.filter(knob => knob.setting !== undefined)
    expect(everyday.map(knob => [knob.name, knob.setting])).toEqual([
      ['speed', 'handCursorSpeed'],
      ['smoothing', 'handSmoothing'],
      ['pinch', 'handPinch'],
      ['scroll-speed', 'handScrollSpeed'],
    ])
  })
})

describe('/jarvis hands tune, set, preset and reset', () => {
  test('tune lists every knob: value, default, range, what higher does and how to change it', async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    await handsReady(w)
    const text = await jarvis($, 'hands tune')
    expect(text).toContain('Hand control tuning: balanced (every setting is at its default).')
    for (const knob of KNOBS) {
      expect(text).toContain(knob.label)
      expect(text).toContain(`Higher means ${knob.higher}.`)
    }
    expect(text).toContain('Cursor speed (speed)')
    expect(text).toMatch(/Cursor speed \(speed\)\s+1\s+default 1\s+range 0\.7 to 3\n/)
    expect(text).toMatch(/Palm hold time \(engage-time\)\s+0\.5 s\s+default 0\.5 s\s+range 0\.1 to 2 s\n/)
    expect(text).toMatch(/Dead zone \(dead-zone\)\s+1 px\s+default 1 px\s+range 0 to 8 px\n/)
    expect(text).toContain('Change one: /jarvis hands set <name> <number|default>, for example /jarvis hands set speed 1.5.')
    expect(text).toContain('/jarvis hands preset <precise|balanced|fast>')
    expect(text).toContain('/jarvis hands reset')
    expect(text).toContain('never in the middle of a click, a drag or a grab')
    expect(text).not.toContain('* ')
    expect(w.handsNamed('config')).toHaveLength(1) // only the start's
    expect(await jarvis($, 'hands tuning')).toBe(text)
    expect(await jarvis($, 'hands sensitivity')).toBe(text)
  })

  test('tune marks what is changed and says how many', async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    await handsReady(w)
    await jarvis($, 'hands set speed 1.4')
    await jarvis($, 'hands set dead-zone 3')
    const text = await jarvis($, 'hands tune')
    expect(text).toContain('Hand control tuning: custom, 2 changed.')
    expect(text).toMatch(/^\* Cursor speed \(speed\)\s+1\.4\s+default 1\s+range 0\.7 to 3\s+changed$/m)
    expect(text).toMatch(/^\* Dead zone \(dead-zone\)\s+3 px\s+default 1 px\s+range 0 to 8 px\s+changed$/m)
    expect(text).toMatch(/^ {2}Smoothing \(smoothing\)\s+1\s+default 1\s+range 0\.2 to 3$/m)
    expect(text.match(/^\* /gm)).toHaveLength(2)
  })

  test('set changes one setting at once: sent to the helper, kept, and told with the old and the new value', async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    await handsReady(w)
    expect(await jarvis($, 'hands set speed 1.4')).toBe('Cursor speed is now 1.4 (was 1; default 1, range 0.7 to 3).')
    expect(w.handsNamed('config').at(-1)?.body).toEqual({ cursorSpeed: 1.4 })
    expect(w.store.get(TUNING_KEY)).toEqual({ cursorSpeed: { value: 1.4, setting: 1 } })
    expect(await jarvis($, 'hands set engage-time 0.8')).toBe('Palm hold time is now 0.8 s (was 0.5 s; default 0.5 s, range 0.1 to 2 s).')
    expect(w.handsNamed('config').at(-1)?.body).toEqual({ engageSeconds: 0.8 })
    expect(w.store.get(TUNING_KEY)).toEqual({
      cursorSpeed: { value: 1.4, setting: 1 },
      engageSeconds: { value: 0.8, setting: 0.5 },
    })
    // It does not touch the engage mode, the displays or the pause.
    expect(w.store.has('handsEngage')).toBe(false)
    expect(handsSent(w).filter(name => name !== 'config')).toEqual([])
  })

  test('set takes aliases, any case, a name of several words and "to"', async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    await handsReady(w)
    const sent = async (command: string): Promise<unknown> => {
      const before = w.handsNamed('config').length
      await jarvis($, command)
      return w.handsNamed('config').length === before + 1 ? w.handsNamed('config').at(-1)?.body : 'nothing sent'
    }
    expect(await sent('hands set Speed 1.5')).toEqual({ cursorSpeed: 1.5 })
    expect(await sent('hands set cursor speed to 2')).toEqual({ cursorSpeed: 2 })
    expect(await sent('hands set cursorSpeed 1.2')).toEqual({ cursorSpeed: 1.2 })
    expect(await sent('hands set cursor_speed = 1.3')).toEqual({ cursorSpeed: 1.3 })
    expect(await sent('hands set "speed" "1.1"')).toEqual({ cursorSpeed: 1.1 })
    expect(await sent('hands set smooth 2')).toEqual({ smoothing: 2 })
    expect(await sent('hands set click 1.1')).toEqual({ pinch: 1.1 })
    expect(await sent('hands set grab 0.9')).toEqual({ fist: 0.9 })
    expect(await sent('hands set deadzone 4')).toEqual({ deadZone: 4 })
    expect(await sent('hands set drag 2')).toEqual({ dragDistance: 2 })
    expect(await sent('hands set fling 1.5')).toEqual({ flingSensitivity: 1.5 })
    expect(await sent('hands set scroll 2.5')).toEqual({ scrollSpeed: 2.5 })
    expect(await sent('hands set engage 0.7')).toEqual({ engageSeconds: 0.7 })
  })

  test('set to default puts the setting back and sends its default', async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    await handsReady(w)
    await jarvis($, 'hands set speed 1.4')
    expect(await jarvis($, 'hands set speed default')).toBe('Cursor speed is back to its default, 1 (was 1.4).')
    expect(w.handsNamed('config').at(-1)?.body).toEqual({ cursorSpeed: 1 })
    expect(w.store.has(TUNING_KEY)).toBe(false)
    // Already there: nothing to send.
    const sent = w.handsNamed('config').length
    expect(await jarvis($, 'hands set speed default')).toBe('Cursor speed is already 1.')
    expect(await jarvis($, 'hands set speed 1')).toBe('Cursor speed is already 1.')
    expect(w.handsNamed('config')).toHaveLength(sent)
    expect(w.store.has(TUNING_KEY)).toBe(false)
  })

  test('set with only a name shows that setting; with nothing it shows the names', async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    await handsReady(w)
    await jarvis($, 'hands set pinch 1.1')
    const text = await jarvis($, 'hands set pinch')
    expect(text).toBe(
      'Pinch sensitivity is 1.1 (default 1, range 0.85 to 1.15). Higher means a lighter, looser pinch counts as a click. Change it with /jarvis hands set pinch <0.85 to 1.15|default>.',
    )
    expect(await jarvis($, 'hands set cursor speed')).toContain('Cursor speed is 1 (default 1, range 0.7 to 3).')
    const usage = await jarvis($, 'hands set')
    expect(usage).toContain('Use /jarvis hands set <name> <number|default>, for example /jarvis hands set speed 1.5.')
    expect(usage).toContain('speed, smoothing, pinch, fist, engage-time, drag-distance, fling, scroll-speed and dead-zone')
    expect(w.handsNamed('config')).toHaveLength(2) // the start's, and the pinch
  })

  test('set refuses an unknown setting, a non-number and a value out of range, and says what is allowed', async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    await handsReady(w)
    expect(await jarvis($, 'hands set warp 2')).toBe(
      'Unknown setting "warp". The settings are speed, smoothing, pinch, fist, engage-time, drag-distance, fling, scroll-speed and dead-zone; /jarvis hands tune shows what each does.',
    )
    expect(await jarvis($, 'hands set speed fast')).toBe(
      '"fast" is not a number. Cursor speed takes a number from 0.7 to 3 (default 1), or "default": /jarvis hands set speed 1.5.',
    )
    expect(await jarvis($, 'hands set speed 5')).toBe('5 is outside the range for Cursor speed: 0.7 to 3 (default 1). Nothing was changed.')
    expect(await jarvis($, 'hands set speed 0')).toBe('0 is outside the range for Cursor speed: 0.7 to 3 (default 1). Nothing was changed.')
    expect(await jarvis($, 'hands set engage-time 9')).toBe(
      '9 is outside the range for Palm hold time: 0.1 to 2 s (default 0.5 s). Nothing was changed.',
    )
    expect(await jarvis($, 'hands set speed NaN')).toContain('"NaN" is not a number.')
    expect(await jarvis($, 'hands set speed Infinity')).toContain('"Infinity" is not a number.')
    expect(await jarvis($, 'hands set speed 1e9')).toContain('"1e9" is not a number.')
    expect(w.handsNamed('config')).toHaveLength(1)
    expect(w.store.has(TUNING_KEY)).toBe(false)
  })

  test('preset applies a whole profile, the other settings going back to their defaults', async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    await handsReady(w)
    await jarvis($, 'hands set scroll-speed 2')
    await jarvis($, 'hands set speed 2')
    expect(await jarvis($, 'hands preset precise')).toBe(
      'Preset precise: speed 0.8, smoothing 1.8, pinch 0.9, drag-distance 1.5, dead-zone 2 px (every other setting is at its default).',
    )
    // Everything that changed is sent; scroll speed went back to 1.
    expect(w.handsNamed('config').at(-1)?.body).toEqual({
      cursorSpeed: 0.8,
      smoothing: 1.8,
      pinch: 0.9,
      dragDistance: 1.5,
      deadZone: 2,
      scrollSpeed: 1,
    })
    expect(w.store.get(TUNING_KEY)).toEqual({
      cursorSpeed: { value: 0.8, setting: 1 },
      smoothing: { value: 1.8, setting: 1 },
      pinch: { value: 0.9, setting: 1 },
      dragDistance: { value: 1.5, setting: 1 },
      deadZone: { value: 2, setting: 1 },
    })
    expect(await jarvis($, 'hands preset precise')).toBe('Preset precise is already in place.')
    expect(await jarvis($, 'hands Preset FAST')).toBe(
      'Preset fast: speed 1.6, smoothing 0.5, pinch 1.1, dead-zone 0 px (every other setting is at its default).',
    )
    expect(w.handsNamed('config').at(-1)?.body).toEqual({ cursorSpeed: 1.6, smoothing: 0.5, pinch: 1.1, dragDistance: 1, deadZone: 0 })
    expect(await jarvis($, 'hands preset balanced')).toBe('Preset balanced: every setting is at its default.')
    expect(w.handsNamed('config').at(-1)?.body).toEqual({ cursorSpeed: 1, smoothing: 1, pinch: 1, deadZone: 1 })
    expect(w.store.has(TUNING_KEY)).toBe(false)
    expect(await jarvis($, 'hands preset default')).toBe('Preset balanced is already in place.')
  })

  test('preset with no name lists them; an unknown one says which exist', async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    await handsReady(w)
    const list = await jarvis($, 'hands preset')
    expect(list).toContain('precise: speed 0.8, smoothing 1.8, pinch 0.9, drag-distance 1.5, dead-zone 2 px')
    expect(list).toContain('balanced: every setting at its default')
    expect(list).toContain('fast: speed 1.6, smoothing 0.5, pinch 1.1, dead-zone 0 px')
    expect(list).toContain('Now: balanced.')
    expect(list).toContain('/jarvis hands preset <precise|balanced|fast>')
    expect(await jarvis($, 'hands preset turbo')).toBe(
      'Unknown preset "turbo". The presets are precise, balanced and fast; /jarvis hands preset shows what each sets.',
    )
    expect(w.handsNamed('config')).toHaveLength(1)
  })

  test('reset puts every setting back and sends what changed; reset with a name resets that one', async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    await handsReady(w)
    expect(await jarvis($, 'hands reset')).toBe('Hand control tuning is already at its defaults.')
    await jarvis($, 'hands set speed 1.4')
    await jarvis($, 'hands set fist 0.9')
    await jarvis($, 'hands set scroll 3')
    expect(await jarvis($, 'hands reset speed')).toBe('Cursor speed is back to its default, 1 (was 1.4).')
    expect(w.handsNamed('config').at(-1)?.body).toEqual({ cursorSpeed: 1 })
    expect(await jarvis($, 'hands reset')).toBe('Hand control tuning is back to its defaults.')
    expect(w.handsNamed('config').at(-1)?.body).toEqual({ fist: 1, scrollSpeed: 1 })
    expect(w.store.has(TUNING_KEY)).toBe(false)
    expect(await jarvis($, 'hands reset warp')).toContain('Unknown setting "warp".')
  })

  test('with no hand helper running a choice is kept and goes out with the next start', async ($, on) => {
    const w = world(on)
    w.existing.add(HANDS_PYTHON)
    await startHelper($, w)
    expect(await jarvis($, 'hands set speed 1.4')).toBe(
      'Cursor speed is now 1.4 (was 1; default 1, range 0.7 to 3); it applies when hand control starts.',
    )
    expect(await jarvis($, 'hands preset fast')).toContain('(every other setting is at its default); it applies when hand control starts.')
    expect(await jarvis($, 'hands reset')).toBe('Hand control tuning is back to its defaults; it applies when hand control starts.')
    expect(await jarvis($, 'hands preset precise')).toContain('it applies when hand control starts.')
    expect(w.handsNamed('config')).toHaveLength(0)
    expect(w.handsHelpers()).toHaveLength(0)
    // On at last: the helper's first config carries every changed setting, and only those.
    await jarvis($, 'hands on')
    await w.settle()
    await handsReady(w)
    expect(configBodies(w)).toEqual([
      { engage: 'palm', displays: 'all', cursorSpeed: 0.8, smoothing: 1.8, pinch: 0.9, dragDistance: 1.5, deadZone: 2 },
    ])
  })

  test('a choice outlives the helper and the session: the next hello sends it again', async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    const first = await handsReady(w)
    await jarvis($, 'hands set smoothing 2.5')
    await jarvis($, 'hands set fling 2')
    first.exit(1)
    await w.settle()
    await w.clock.advance(BACKOFF_MS[0] ?? 1000)
    await w.settle()
    await handsReady(w)
    expect(configBodies(w).at(-1)).toEqual({ engage: 'palm', displays: 'all', smoothing: 2.5, flingSensitivity: 2 })
    expect(w.store.get(TUNING_KEY)).toEqual({ smoothing: { value: 2.5, setting: 1 }, flingSensitivity: { value: 2, setting: 1 } })
  })

  test('a choice kept by an earlier session goes out with this one\'s start', async ($, on) => {
    const w = handsWorld(on)
    w.store.set(TUNING_KEY, { smoothing: { value: 2.5, setting: 1 }, flingSensitivity: { value: 2, setting: 1 } })
    await startHelper($, w)
    await handsReady(w)
    expect(configBodies(w)).toEqual([{ engage: 'palm', displays: 'all', smoothing: 2.5, flingSensitivity: 2 }])
    expect(await jarvis($, 'hands')).toContain('Tuning: custom, 2 changed (smoothing 2.5, fling 2).')
  })

  test('a setting the helper refuses is shown, and the stored value is rolled back', async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    await handsReady(w)
    refuseConfig(w)
    expect(await jarvis($, 'hands set speed 2.8')).toBe('The hand helper refused it: cursorSpeed must be between 0.5 and 2.5. Nothing was changed.')
    expect(w.store.has(TUNING_KEY)).toBe(false)
    expect(w.handsNamed('config').at(-1)?.body).toEqual({ cursorSpeed: 2.8 })

    // With an earlier choice: it stays.
    w.respond = () => ({ status: 200, body: { ok: true } })
    await jarvis($, 'hands set speed 1.4')
    refuseConfig(w)
    expect(await jarvis($, 'hands set speed 2')).toContain('The hand helper refused it:')
    expect(w.store.get(TUNING_KEY)).toEqual({ cursorSpeed: { value: 1.4, setting: 1 } })
    expect(await jarvis($, 'hands preset fast')).toContain('The hand helper refused it:')
    expect(await jarvis($, 'hands reset')).toContain('The hand helper refused it:')
    expect(await jarvis($, 'hands set speed default')).toContain('The hand helper refused it:')
    expect(w.store.get(TUNING_KEY)).toEqual({ cursorSpeed: { value: 1.4, setting: 1 } })
    expect(await jarvis($, 'hands tune')).toMatch(/^\* Cursor speed \(speed\)\s+1\.4\s/m)

    w.respond = () => ({ status: 200, body: { ok: true } })
    expect(await jarvis($, 'hands set speed 2')).toBe('Cursor speed is now 2 (was 1.4; default 1, range 0.7 to 3).')
  })

  test('an answer that is not a refusal also leaves nothing half done', async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    await handsReady(w)
    answer(w, 'config', { ok: false, error: { code: 'internal', message: 'boom' } })
    expect(await jarvis($, 'hands set pinch 1.1')).toBe('The hand helper could not apply it: boom. Nothing was changed.')
    expect(w.store.has(TUNING_KEY)).toBe(false)
    w.respond = () => ({ status: 502, body: 'bad gateway' })
    expect(await jarvis($, 'hands set pinch 1.1')).toBe('The hand helper could not apply it: config: HTTP 502. Nothing was changed.')
    expect(w.store.has(TUNING_KEY)).toBe(false)
  })

  test('a refusal from a helper an earlier version installed also says to update it', async ($, on) => {
    const w = handsWorld(on)
    w.files.delete(HANDS_INSTALLED)
    await startHelper($, w)
    await handsReady(w)
    refuseConfig(w)
    expect(await jarvis($, 'hands set speed 2')).toBe(
      'The hand helper refused it: cursorSpeed must be between 0.5 and 2.5. Nothing was changed. The hand helper may be older than this Jarvis: /jarvis setup hands updates it.',
    )
  })

  test('a helper that refuses the tuning at its start still gets the engage mode and the displays', async ($, on) => {
    const w = handsWorld(on)
    w.store.set('handsEngage', 'always')
    w.store.set(TUNING_KEY, { cursorSpeed: { value: 1.4, setting: 1 } })
    refuseConfig(w, body => 'cursorSpeed' in body)
    await startHelper($, w)
    await handsReady(w)
    expect(configBodies(w)).toEqual([
      { engage: 'always', displays: 'all', cursorSpeed: 1.4 },
      { engage: 'always', displays: 'all' },
    ])
    const why =
      'The hand helper refused the tuning settings (cursorSpeed must be between 0.5 and 2.5), so it runs with its defaults. /jarvis setup hands updates an older hand helper; /jarvis hands restart tries again.'
    expect(w.logs).toContain(why)
    expect(w.toasts).toContain('Hand control: the hand helper refused the tuning settings, so it runs with its defaults. The transcript says why.')
    // The tuning table does not claim the settings are in force.
    expect(await jarvis($, 'hands tune')).toContain(why)
    expect(await jarvis($, 'hands')).toContain(why)
  })

  test('a refusal of a start config with nothing to drop is only logged', async ($, on) => {
    const w = handsWorld(on)
    refuseConfig(w)
    await startHelper($, w)
    await handsReady(w)
    expect(configBodies(w)).toEqual([{ engage: 'palm', displays: 'all' }])
    expect(w.logs.some(line => line.includes('refused the tuning'))).toBe(false)
  })

  test('the status says the tuning in a line: a preset by name, otherwise custom and how many', async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    await handsReady(w)
    const more = '/jarvis hands tune shows every setting and how to change it.'
    expect(await jarvis($, 'hands')).toContain(`Tuning: balanced (all defaults). ${more}`)
    await jarvis($, 'hands preset precise')
    expect(await jarvis($, 'hands')).toContain(`Tuning: precise preset. ${more}`)
    await jarvis($, 'hands preset fast')
    expect(await jarvis($, 'hands status')).toContain(`Tuning: fast preset. ${more}`)
    await jarvis($, 'hands set speed 1.4')
    expect(await jarvis($, 'hands')).toContain(`Tuning: custom, 4 changed (speed 1.4, smoothing 0.5, pinch 1.1, dead-zone 0 px). ${more}`)
    await jarvis($, 'hands reset')
    expect(await jarvis($, 'hands set scroll 2')).toContain('Scroll speed is now 2')
    expect(await jarvis($, 'hands')).toContain(`Tuning: custom, 1 changed (scroll-speed 2). ${more}`)
  })

  test('settings that add up to a preset are that preset', async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    await handsReady(w)
    for (const [name, value] of [['speed', 0.8], ['smoothing', 1.8], ['pinch', 0.9], ['drag-distance', 1.5]] as const) {
      await jarvis($, `hands set ${name} ${value}`)
      expect(await jarvis($, 'hands')).toContain('Tuning: custom')
    }
    await jarvis($, 'hands set dead-zone 2')
    expect(await jarvis($, 'hands')).toContain('Tuning: precise preset.')
    expect(await jarvis($, 'hands tune')).toContain('Hand control tuning: precise preset.')
    // One more change and it is custom again; putting it back makes it the preset again.
    await jarvis($, 'hands set fist 1.1')
    expect(await jarvis($, 'hands')).toContain('Tuning: custom, 6 changed')
    await jarvis($, 'hands set fist default')
    expect(await jarvis($, 'hands')).toContain('Tuning: precise preset.')
  })

  test('while hand control is off the status stays short, and tune still shows the tuning', async ($, on) => {
    const w = world(on)
    await startHelper($, w)
    expect(await jarvis($, 'hands')).not.toContain('Tuning:')
    await jarvis($, 'hands set fist 0.9')
    expect(await jarvis($, 'hands tune')).toMatch(/^\* Fist sensitivity \(fist\)\s+0\.9\s+default 1/m)
    expect(await jarvis($, 'hands')).not.toContain('Tuning:')
  })

  test('the help lists the sensitivity commands, in the hands help and in /jarvis', async ($, on) => {
    const w = world(on)
    await startHelper($, w)
    const help = await jarvis($, 'hands wave')
    expect(help).toContain('/jarvis hands tune                 every sensitivity setting, its value and what it does')
    expect(help).toContain('/jarvis hands set <name> <number>  change one setting, such as: set speed 1.5 (or default)')
    expect(help).toContain('/jarvis hands preset <name>        precise, balanced or fast: a ready-made set of settings')
    expect(help).toContain('/jarvis hands reset [name]         put every setting (or one) back to its default')
    expect(await jarvis($, '')).toContain('/jarvis hands tune|preset|set    tune the sensitivity: cursor speed, smoothing, pinch and more')
    expect(await jarvis($, 'hands help')).toContain('/jarvis hands preset <name>')
  })

  test('never in a cloud session', async ($, on) => {
    const w = world(on, { env: { HOME: '/root', CLAUDE_CODE_REMOTE: 'true' } })
    await startSession($, w)
    for (const command of ['tune', 'set speed 1.5', 'preset fast', 'reset']) {
      expect(await jarvis($, `hands ${command}`)).toBe(NOT_LOCAL)
    }
    expect(w.store.has(TUNING_KEY)).toBe(false)
  })
})

describe('tuning and the plugin settings', () => {
  const SETTINGS = { handCursorSpeed: '1.5', handSmoothing: '2', handPinch: '1.1', handScrollSpeed: '3' }

  test('the plugin settings are the defaults: they go out with the start and tune shows them', { options: SETTINGS }, async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    await handsReady(w)
    expect(configBodies(w)).toEqual([{ engage: 'palm', displays: 'all', cursorSpeed: 1.5, smoothing: 2, pinch: 1.1, scrollSpeed: 3 }])
    const text = await jarvis($, 'hands tune')
    expect(text).toMatch(/^\* Cursor speed \(speed\)\s+1\.5\s+default 1 \(handCursorSpeed says 1\.5\)\s+range 0\.7 to 3\s+changed$/m)
    expect(await jarvis($, 'hands')).toContain('Tuning: custom, 4 changed (speed 1.5, smoothing 2, pinch 1.1, scroll-speed 3).')
  })

  test('a command overrides the plugin setting and is kept with the value it overrode', { options: SETTINGS }, async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    await handsReady(w)
    expect(await jarvis($, 'hands set speed 2')).toBe('Cursor speed is now 2 (was 1.5; default 1, range 0.7 to 3).')
    expect(w.handsNamed('config').at(-1)?.body).toEqual({ cursorSpeed: 2 })
    expect(w.store.get(TUNING_KEY)).toEqual({ cursorSpeed: { value: 2, setting: 1.5 } })
    // A value equal to the setting is no override.
    expect(await jarvis($, 'hands set speed 1.5')).toBe('Cursor speed is now 1.5 (was 2; default 1, range 0.7 to 3).')
    expect(w.store.has(TUNING_KEY)).toBe(false)
    // The factory value is a choice against the setting.
    expect(await jarvis($, 'hands set speed 1')).toBe('Cursor speed is now 1 (was 1.5; default 1, range 0.7 to 3).')
    expect(w.store.get(TUNING_KEY)).toEqual({ cursorSpeed: { value: 1, setting: 1.5 } })
    // "default" gives the setting its say again.
    expect(await jarvis($, 'hands set speed default')).toBe(
      'Cursor speed is back to 1.5, what the handCursorSpeed plugin setting says (was 1).',
    )
    expect(w.handsNamed('config').at(-1)?.body).toEqual({ cursorSpeed: 1.5 })
    expect(w.store.has(TUNING_KEY)).toBe(false)
  })

  test('reset and balanced differ when settings differ: reset gives the settings their say, balanced does not', { options: SETTINGS }, async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    await handsReady(w)
    await jarvis($, 'hands set speed 2')
    expect(await jarvis($, 'hands reset')).toBe(
      'Hand control tuning is back to its defaults (your plugin settings count as defaults: speed 1.5, smoothing 2, pinch 1.1, scroll-speed 3).',
    )
    expect(w.handsNamed('config').at(-1)?.body).toEqual({ cursorSpeed: 1.5 })
    expect(await jarvis($, 'hands preset balanced')).toBe('Preset balanced: every setting is at its default.')
    expect(w.handsNamed('config').at(-1)?.body).toEqual({ cursorSpeed: 1, smoothing: 1, pinch: 1, scrollSpeed: 1 })
    expect(w.store.get(TUNING_KEY)).toEqual({
      cursorSpeed: { value: 1, setting: 1.5 },
      smoothing: { value: 1, setting: 2 },
      pinch: { value: 1, setting: 1.1 },
      scrollSpeed: { value: 1, setting: 3 },
    })
  })

  test('a choice lapses once its setting changed: the setting has the last word, and the old choice is dropped', { options: { handCursorSpeed: '1.8' } }, async ($, on) => {
    const w = handsWorld(on)
    // Chosen while the setting said 1.5, and 2 for the smoothing, which the setting does not name.
    w.store.set(TUNING_KEY, { cursorSpeed: { value: 2, setting: 1.5 }, smoothing: { value: 2, setting: 1 } })
    await startHelper($, w)
    await handsReady(w)
    expect(configBodies(w)).toEqual([{ engage: 'palm', displays: 'all', cursorSpeed: 1.8, smoothing: 2 }])
    expect(w.store.get(TUNING_KEY)).toEqual({ smoothing: { value: 2, setting: 1 } })
  })

  test('a choice with no record of the setting it overrode, or a value out of range, is none', async ($, on) => {
    const w = handsWorld(on)
    w.store.set(TUNING_KEY, {
      cursorSpeed: 1.4,
      smoothing: { value: 2 },
      pinch: { value: 9, setting: 1 },
      fist: { value: 'tight', setting: 1 },
      deadZone: { value: 3, setting: 1 },
      warp: { value: 3, setting: 1 },
    })
    await startHelper($, w)
    await handsReady(w)
    expect(configBodies(w)).toEqual([{ engage: 'palm', displays: 'all', deadZone: 3 }])
    expect(w.store.get(TUNING_KEY)).toEqual({ deadZone: { value: 3, setting: 1 } })
    w.store.set(TUNING_KEY, 'nonsense')
    expect(await jarvis($, 'hands tune')).toContain('balanced')
    expect(w.store.has(TUNING_KEY)).toBe(false)
  })

  test(
    'settings that are not numbers in range fall back to the defaults and say so once, never stopping the start',
    { options: { handCursorSpeed: 'fast', handSmoothing: '9', handPinch: ' 1.1 ', handScrollSpeed: '' } },
    async ($, on) => {
      const w = handsWorld(on)
      await startHelper($, w)
      const first = await handsReady(w)
      expect(configBodies(w)).toEqual([{ engage: 'palm', displays: 'all', pinch: 1.1 }])
      expect(w.logs).toContain('The handCursorSpeed setting "fast" is not a number from 0.7 to 3, so the default, 1, is used.')
      expect(w.logs).toContain('The handSmoothing setting "9" is not a number from 0.2 to 3, so the default, 1, is used.')
      expect(w.logs.filter(line => line.includes(' setting "'))).toHaveLength(2)
      expect(w.toasts).toContain('Hand control: 2 settings are not numbers in range, so their defaults are used. The transcript lists them.')
      expect(w.status()).toBe(`${VOICE_READY} · hands ready · open palm to start`)

      // Not again when the helper restarts.
      first.exit(1)
      await w.settle()
      await w.clock.advance(BACKOFF_MS[0] ?? 1000)
      await w.settle()
      await handsReady(w)
      expect(w.logs.filter(line => line.includes(' setting "'))).toHaveLength(2)
      expect(w.toasts.filter(toast => toast.includes('not numbers in range'))).toHaveLength(1)
      // tune says so too.
      expect(await jarvis($, 'hands tune')).toContain('The handCursorSpeed setting "fast" is not a number from 0.7 to 3, so the default, 1, is used.')
    },
  )

  test('one bad setting is toasted by itself', { options: { handPinch: 'loose' } }, async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    await handsReady(w)
    expect(w.toasts).toContain('Hand control: the handPinch setting "loose" is not a number from 0.85 to 1.15, so the default, 1, is used.')
  })

  test('without a bad setting there is nothing to say', { options: { handCursorSpeed: '1', handSmoothing: ' ' } }, async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    await handsReady(w)
    expect(w.logs.some(line => line.includes(' setting "'))).toBe(false)
    expect(configBodies(w)).toEqual([{ engage: 'palm', displays: 'all' }])
  })
})

describe('the hands tool: tuning', () => {
  const call = async ($: TestEngine, input: Record<string, unknown>): Promise<string> =>
    String((await $.tool.call({ tool: 'mcp__jarvis__hands', ...input })).result)

  test('the description names the settings with their ranges and asks for modest steps and a report', () => {
    const { description } = HANDS_TOOL
    for (const knob of KNOBS) expect(description).toContain(`${knob.name} ${knob.min} to ${knob.max}`)
    expect(description).toContain('modest')
    expect(description).toContain('report')
    expect(description).toContain('preset')
    expect(description).toContain('precise, balanced or fast')
    expect(description.length).toBeLessThan(5000)
    const { properties } = HANDS_TOOL.inputSchema
    expect(properties.setting.description).toContain('speed')
    expect(properties.preset.enum).toEqual(Object.keys(PRESETS))
  })

  test('setting and value change a setting as /jarvis hands set does: "Jarvis, make the cursor faster"', async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    await handsReady(w)
    await allowedTurn($, w, HANDS_TOOL_ID)
    expect(await call($, { setting: 'speed' })).toBe(
      'Cursor speed is 1 (default 1, range 0.7 to 3). Higher means a faster cursor, with less hand travel to cross the screen. Change it with /jarvis hands set speed <0.7 to 3|default>.',
    )
    expect(await call($, { setting: 'speed', value: 1.3 })).toBe('Cursor speed is now 1.3 (was 1; default 1, range 0.7 to 3).')
    expect(w.handsNamed('config').at(-1)?.body).toEqual({ cursorSpeed: 1.3 })
    expect(w.store.get(TUNING_KEY)).toEqual({ cursorSpeed: { value: 1.3, setting: 1 } })
    expect(await call($, { setting: 'cursorSpeed', value: '1.6' })).toBe('Cursor speed is now 1.6 (was 1.3; default 1, range 0.7 to 3).')
    expect(await call($, { setting: 'smoothing', value: 'default' })).toBe('Smoothing is already 1.')
    expect(await call($, { setting: 'speed', value: 'default' })).toBe('Cursor speed is back to its default, 1 (was 1.6).')
    expect(w.store.has(TUNING_KEY)).toBe(false)
    // Zero is a value, not an absence.
    expect(await call($, { setting: 'dead-zone', value: 0 })).toBe('Dead zone is now 0 px (was 1 px; default 1 px, range 0 to 8 px).')
    expect(w.handsNamed('config').at(-1)?.body).toEqual({ deadZone: 0 })
    expect(w.store.get(TUNING_KEY)).toEqual({ deadZone: { value: 0, setting: 1 } })
  })

  test('preset applies a preset', async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    await handsReady(w)
    await allowedTurn($, w, HANDS_TOOL_ID)
    expect(await call($, { preset: 'precise' })).toBe(
      'Preset precise: speed 0.8, smoothing 1.8, pinch 0.9, drag-distance 1.5, dead-zone 2 px (every other setting is at its default).',
    )
    expect(w.handsNamed('config').at(-1)?.body).toEqual({ cursorSpeed: 0.8, smoothing: 1.8, pinch: 0.9, dragDistance: 1.5, deadZone: 2 })
    expect(await call($, { preset: 'Balanced' })).toBe('Preset balanced: every setting is at its default.')
    expect(await call($, { preset: 'turbo' })).toBe(await jarvis($, 'hands preset turbo'))
  })

  test('a call is a tuning change when it carries a value or a preset; a setting alone only reports', () => {
    expect(isTuningCall({ setting: 'speed' })).toBe(true)
    expect(isTuningCall({ value: 1 })).toBe(true)
    expect(isTuningCall({ preset: 'fast' })).toBe(true)
    expect(isTuningCall({ action: 'status', display: '1' })).toBe(false)
    expect(changesTuning({ setting: 'speed' })).toBe(false)
    expect(changesTuning({ setting: 'speed', value: 'default' })).toBe(true)
    expect(changesTuning({ preset: 'balanced' })).toBe(true)
    expect(changesTuning({ action: 'on' })).toBe(false)
  })

  test('arguments the model must fix are answered in words, and nothing changes', async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    await handsReady(w)
    expect(await call($, { value: 1.5 })).toBe('Say which setting the value is for, such as setting "speed" with value 1.5.')
    expect(await call($, { setting: 5, value: 1 })).toBe('The setting must be a name, such as "speed"; /jarvis hands tune lists them.')
    expect(await call($, { setting: 'speed', value: true })).toBe('The value must be a number, or "default".')
    expect(await call($, { setting: 'speed', value: [1] })).toBe('The value must be a number, or "default".')
    expect(await call($, { setting: 'speed', value: 99 })).toBe('99 is outside the range for Cursor speed: 0.7 to 3 (default 1). Nothing was changed.')
    expect(await call($, { setting: 'speed', value: 'fast' })).toContain('"fast" is not a number.')
    expect(await call($, { setting: 'warp', value: 1 })).toContain('Unknown setting "warp".')
    expect(await call($, { preset: 'fast', setting: 'speed' })).toBe('Give either a preset or a setting, not both.')
    expect(await call($, { preset: 'fast', value: 2 })).toBe('Give either a preset or a setting, not both.')
    expect(await call($, { preset: 5 })).toBe('The preset must be one of precise, balanced or fast.')
    expect(w.handsNamed('config')).toHaveLength(1)
    expect(w.store.has(TUNING_KEY)).toBe(false)
    expect(await call($, {})).toBe('Give an action, a display, or both.')
  })

  test('it comes alone or with an action, applies after the display and before the action', async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    await handsReady(w)
    await allowedTurn($, w, HANDS_TOOL_ID)
    const both = await call($, { display: '2', setting: 'pinch', value: 1.1, action: 'status' })
    const parts = both.split('\n\n')
    expect(parts[0]).toBe('Hand control now reaches display 2 (EPSON Projector).')
    expect(parts[1]).toBe('Pinch sensitivity is now 1.1 (was 1; default 1, range 0.85 to 1.15).')
    expect(parts[2]).toContain('Hand control: ready')
    expect(both).toContain('Tuning: custom, 1 changed (pinch 1.1).')
  })

  test('a refusal by the helper reaches the model, with the value rolled back', async ($, on) => {
    const w = handsWorld(on)
    await startHelper($, w)
    await handsReady(w)
    await allowedTurn($, w, HANDS_TOOL_ID)
    refuseConfig(w)
    expect(await call($, { setting: 'speed', value: 2.8 })).toBe(
      'The hand helper refused it: cursorSpeed must be between 0.5 and 2.5. Nothing was changed.',
    )
    expect(w.store.has(TUNING_KEY)).toBe(false)
  })

  test('it works while hand control is off: the choice waits for the start', async ($, on) => {
    const w = world(on)
    await startHelper($, w)
    expect(w.tools).toEqual(['desktop', 'home_control', 'hands'])
    await allowedTurn($, w, HANDS_TOOL_ID)
    expect(await call($, { setting: 'fist', value: 0.9 })).toBe(
      'Fist sensitivity is now 0.9 (was 1; default 1, range 0.9 to 1.1); it applies when hand control starts.',
    )
    expect(w.store.get(TUNING_KEY)).toEqual({ fist: { value: 0.9, setting: 1 } })
  })
})

describe('hands protocol parsing', () => {
  test('events are checked defensively', () => {
    expect(parseHandsEvent('{"v":1,"type":"state","state":"idle"}')).toEqual({ v: 1, type: 'state', state: 'idle' })
    expect(parseHandsEvent('{"v":1,"type":"state","state":"dancing"}')).toBeUndefined()
    expect(parseHandsEvent('{"v":2,"type":"state","state":"idle"}')).toBeUndefined()
    expect(parseHandsEvent('{"v":1,"type":"hello","port":"80"}')).toBeUndefined()
    expect(parseHandsEvent('{"v":1,"type":"calibration","step":"middle"}')).toBeUndefined()
    expect(parseHandsEvent('{"v":1,"type":"ready","camera":0,"width":640,"height":480,"fps":30}')).toBeUndefined()
    const ready = parseHandsEvent(
      '{"v":1,"type":"ready","camera":0,"width":640,"height":480,"fps":30,"displays":[{"id":1,"name":"A","x":0,"y":0,"width":1,"height":1,"primary":true,"virtual":false,"used":true},{"name":"no id"}]}',
    )
    expect(ready).toMatchObject({ type: 'ready', camera: '0', displays: [{ id: 1, name: 'A', used: true }] })
    expect(parseHandsEvent('{"v":1,"type":"error","code":"internal","message":"boom"}')).toMatchObject({ fatal: false })
    expect(parseHandsEvent('[1,2]')).toBeUndefined()
    expect(parseHandsEvent('not json')).toBeUndefined()
  })

  test('an error\'s hint is shown unless it says the message again', () => {
    expect(describeHandsError('The hand model is missing', 'run /jarvis setup hands')).toBe('The hand model is missing (run /jarvis setup hands)')
    expect(describeHandsError('The camera is in use by another app', 'close the other app using the camera')).toBe(
      'The camera is in use by another app (close the other app using the camera)',
    )
    expect(describeHandsError(UNSUPPORTED, UNSUPPORTED_HINT)).toBe(UNSUPPORTED)
    expect(describeHandsError('No camera found', 'No camera found.')).toBe('No camera found')
    expect(describeHandsError('No camera found', undefined)).toBe('No camera found')
    expect(describeHandsError('No camera found', ' ')).toBe('No camera found')
  })

  test('display selections', () => {
    expect(parseDisplaySelection('all')).toBe('all')
    expect(parseDisplaySelection('2')).toEqual([2])
    expect(parseDisplaySelection('1,,2, 1')).toEqual([1, 2])
    expect(parseDisplaySelection('0')).toBeUndefined()
    expect(parseDisplaySelection('all,2')).toBeUndefined()
    expect(parseDisplaySelection('')).toBeUndefined()
  })
})

describe('the palm hold time in the instructions', () => {
  const held = (seconds: number): Record<string, unknown> => ({ engageSeconds: { value: seconds, setting: 0.5 } })
  const call = async ($: TestEngine, input: Record<string, unknown>): Promise<string> =>
    String((await $.tool.call({ tool: 'mcp__jarvis__hands', ...input })).result)

  test('the start reply says the hold time that is set, in the command and in the tool', async ($, on) => {
    const w = world(on)
    w.existing.add(HANDS_PYTHON)
    w.store.set(TUNING_KEY, held(1.5))
    await startHelper($, w)
    exitOnShutdown(w)
    const reply = 'Hand control is on. The camera starts in a moment; then hold an open palm toward the camera for 1.5 seconds to take the cursor. Nothing leaves this computer.'
    await allowedTurn($, w, HANDS_TOOL_ID)
    expect(await jarvis($, 'hands on')).toBe(reply)
    await w.settle()
    await handsReady(w)
    await jarvis($, 'hands off')
    expect(await call($, { action: 'on' })).toBe(reply)
  })

  test('the status, the help and the engage reply say it too, and follow a change at once', async ($, on) => {
    const w = handsWorld(on)
    w.store.set(TUNING_KEY, held(1.5))
    await startHelper($, w)
    await handsReady(w)
    await allowedTurn($, w, HANDS_TOOL_ID)
    const status = await jarvis($, 'hands')
    expect(status).toContain('Hand control starts when you hold an open palm toward the camera for 1.5 seconds (/jarvis hands engage always switches).')
    expect(status).toContain('Open palm toward the camera, still for 1.5 seconds: start (the cursor follows your hand)')
    expect(status).not.toContain('half a second')
    const help = await jarvis($, 'hands help')
    expect(help).toContain('Open palm toward the camera, still for 1.5 seconds: start (the cursor follows your hand)')
    expect(help).not.toContain('half a second')
    expect(await jarvis($, 'hands engage palm')).toBe('Hand control starts when you hold an open palm toward the camera for 1.5 seconds.')
    expect(await jarvis($, 'hands engage')).toContain('toward the camera for 1.5 seconds. Switch with')
    expect(await call($, { action: 'status' })).toContain('still for 1.5 seconds: start')

    // A change shows at once, and the default keeps its old words.
    await jarvis($, 'hands set engage-time 1')
    expect(await jarvis($, 'hands engage palm')).toBe('Hand control starts when you hold an open palm toward the camera for one second.')
    await jarvis($, 'hands set engage-time 2')
    expect(await jarvis($, 'hands engage palm')).toBe('Hand control starts when you hold an open palm toward the camera for 2 seconds.')
    await jarvis($, 'hands set engage-time 0.1')
    expect(await jarvis($, 'hands engage palm')).toBe('Hand control starts when you hold an open palm toward the camera for 0.1 seconds.')
    await jarvis($, 'hands set engage-time 1.25')
    expect(await jarvis($, 'hands engage palm')).toBe('Hand control starts when you hold an open palm toward the camera for 1.25 seconds.')
    await jarvis($, 'hands set engage-time default')
    expect(await jarvis($, 'hands engage palm')).toBe('Hand control starts when you hold an open palm toward the camera for half a second.')
    expect(await jarvis($, 'hands help')).toContain('Open palm toward the camera, still for half a second: start')
  })
})

describe('a helper that does not know the settings', () => {
  test('a refusal naming an unknown property says to update it, even when the versions look equal', async ($, on) => {
    const w = handsWorld(on) // installed by this version: no update notice
    await startHelper($, w)
    await handsReady(w)
    const unknown = { status: 400, body: { ok: false, error: { code: 'bad_request', message: "$: unexpected property 'cursorSpeed'" } } }
    w.respond = command => (command.name === 'config' ? unknown : { status: 200, body: { ok: true } })
    const hint = 'The hand helper may be older than this Jarvis: /jarvis setup hands updates it.'
    expect(await jarvis($, 'hands set speed 2')).toBe(`The hand helper refused it: $: unexpected property 'cursorSpeed'. Nothing was changed. ${hint}`)
    expect(await jarvis($, 'hands preset fast')).toContain(hint)
    expect(w.store.has(TUNING_KEY)).toBe(false)
    // Any other refusal gets no such hint (and no second one when the notice already says it).
    refuseConfig(w)
    expect(await jarvis($, 'hands set speed 2')).toBe('The hand helper refused it: cursorSpeed must be between 0.5 and 2.5. Nothing was changed.')
  })
})
