import { describe, expect, test } from 'claude-code/testing'
import type { On } from 'claude-code'

import { BACKOFF_MS, HEARTBEAT_MS, ORPHAN_RETRY_MS, STOP_WAIT_MS } from './helper'
import { parseDisplaySelection, parseHandsEvent } from './hands'
import type { FakeChild, World, WorldOptions } from './test-harness'
import {
  DATA_DIR,
  HANDS_PORT,
  HANDS_PYTHON,
  jarvis,
  PORT,
  startHelper,
  startSession,
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

const DISPLAYS = [
  { id: 1, name: 'DELL U2720Q', x: 0, y: 0, width: 2560, height: 1440, primary: true, virtual: false, used: true },
  { id: 2, name: 'EPSON Projector', x: 2560, y: 0, width: 1920, height: 1080, primary: false, virtual: false, used: true },
]

/** Hand control turned on (as /jarvis hands on stores it) and installed. */
function handsWorld(on: On, options: WorldOptions = {}): World {
  const w = world(on, options)
  w.store.set('handsEnabled', true)
  w.existing.add(HANDS_PYTHON)
  return w
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
    expect(w.tools).toEqual(['hands'])
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
    w.store.set('handsEnabled', false)
    await startSession($, w)
    expect(w.handsHelpers()).toHaveLength(0)
  })

  test('on but not installed: says so in the status line and spawns nothing', async ($, on) => {
    const w = world(on)
    w.store.set('handsEnabled', true)
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
    expect(w.status()).toBe(`${VOICE_READY} · hands stopped · The hand model is missing (run /jarvis setup hands) · /jarvis hands restart`)
    await w.clock.advance(30_000)
    await w.settle()
    expect(w.handsHelpers()).toHaveLength(1)
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
    w.store.set('handsEnabled', true)
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
    w.store.set('handsEnabled', true)
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
    w.store.set('handsEnabled', true)
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
    expect(w.store.get('handsEnabled')).toBe(true)
    expect(w.handsHelpers()).toHaveLength(1)
    await handsReady(w)
    expect(await jarvis($, 'hands on')).toBe('Hand control is already on.')

    expect(await jarvis($, 'hands off')).toBe('Hand control is off and the camera is closed.')
    expect(w.handsNamed('shutdown')).toHaveLength(1)
    expect(w.store.get('handsEnabled')).toBe(false)
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
    expect(w.store.get('handsEnabled')).toBe(true)
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
    expect(await jarvis($, 'hands pause')).toBe('Hand control paused: the camera is off. /jarvis hands resume turns it back on.')
    expect(await jarvis($, 'hands resume')).toBe('Hand control resumed: the camera is on again.')
    expect(w.handsNamed('pause')).toHaveLength(1)
    expect(w.handsNamed('resume')).toHaveLength(1)
    w.respond = () => ({ status: 503, body: { ok: false, error: { code: 'camera_in_use', message: 'The camera is in use by another app.' } } })
    expect(await jarvis($, 'hands resume')).toBe('Could not resume hand control: The camera is in use by another app.')
  })

  test('commands that need the helper explain why it is not there', async ($, on) => {
    const w = world(on)
    await startHelper($, w)
    expect(await jarvis($, 'hands pause')).toBe('Hand control is off. Turn it on with /jarvis hands on.')
    expect(await jarvis($, 'hands calibrate')).toBe('Hand control is off. Turn it on with /jarvis hands on.')
    expect(await jarvis($, 'hands restart')).toBe('Hand control is off. Turn it on with /jarvis hands on.')
    w.store.set('handsEnabled', true)
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
    w.store.set('handsEnabled', true)
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

    expect(w.statuses).toContain(`${VOICE_READY} · hands setting up · installing the hand helper (Python 3.12, MediaPipe and OpenCV)`)
    expect(w.statuses).toContain(`${VOICE_READY} · hands setting up · installing · Installed 27 packages in 14.1s`)
    expect(w.statuses).toContain(`${VOICE_READY} · hands setting up · downloading the hand model 50%`)
    expect(w.statuses).toContain(`${VOICE_READY} · hands setting up · Ready: hand model (7.8 MB)`)
    expect(w.statuses.some(line => line?.includes('urllib3') === true)).toBe(false)
    expect(w.logs).toContain('Hand control installed (Ready: hand model (7.8 MB)). Starting the hand helper.')
    expect(w.toasts).toContain('Hand control is installed. Starting the camera.')

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

  test('without uv it prints the install command; a failed uv sync is reported', async ($, on) => {
    const w = world(on)
    w.store.set('handsEnabled', true)
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
    expect(w.tools).toEqual(['hands'])

    const turnedOn = await $.tool.call({ tool: 'mcp__jarvis__hands', action: 'on' })
    expect(turnedOn.result).toContain('Hand control is on. The camera starts in a moment')
    expect(w.store.get('handsEnabled')).toBe(true)
    await w.settle()
    await handsReady(w)

    const status = await $.tool.call({ tool: 'mcp__jarvis__hands', action: 'status' })
    expect(status.result).toBe(await jarvis($, 'hands status'))
    const calibrate = await $.tool.call({ tool: 'mcp__jarvis__hands', action: 'calibrate' })
    expect(calibrate.result).toContain('Calibrating')
    expect(w.handsNamed('calibrate')).toHaveLength(1)

    const off = await $.tool.call({ tool: 'mcp__jarvis__hands', action: 'off' })
    expect(off.result).toBe('Hand control is off and the camera is closed.')
    expect(w.store.get('handsEnabled')).toBe(false)

    const unknown = await $.tool.call({ tool: 'mcp__jarvis__hands', action: 'explode' })
    expect(unknown.result).toBe('Unknown action "explode"; use one of on, off, status, calibrate, pause, resume.')
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

  test('display selections', () => {
    expect(parseDisplaySelection('all')).toBe('all')
    expect(parseDisplaySelection('2')).toEqual([2])
    expect(parseDisplaySelection('1,,2, 1')).toEqual([1, 2])
    expect(parseDisplaySelection('0')).toBeUndefined()
    expect(parseDisplaySelection('all,2')).toBeUndefined()
    expect(parseDisplaySelection('')).toBeUndefined()
  })
})
