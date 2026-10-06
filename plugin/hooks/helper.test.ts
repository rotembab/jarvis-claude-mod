import { describe, expect, test } from 'claude-code/testing'

import { BACKOFF_MS, HEARTBEAT_MS, HELLO_TIMEOUT_MS, ORPHAN_RETRIES, ORPHAN_RETRY_MS, STOP_WAIT_MS } from './helper'
import { DATA_DIR, jarvis, PORT, startHelper, startSession, VENV_PYTHON, world } from './test-harness'

describe('helper process', () => {
  test('spawns the venv python by absolute path with the protocol env', { options: { fishApiKey: 'fk-test' } }, async ($, on) => {
    const w = world(on)
    await startSession($, w)
    expect(w.helpers()).toHaveLength(1)
    const { request } = w.lastHelper()
    expect(request.argv).toEqual([VENV_PYTHON, '-m', 'jarvis_voice', 'run', '--data-dir', DATA_DIR])
    expect(request.cwd).toBe(DATA_DIR)
    expect(request.input).toBeUndefined()
    const env = request.env ?? {}
    expect(env.PYTHONUNBUFFERED).toBe('1')
    expect(env.PYTHONUTF8).toBe('1')
    expect(env.JARVIS_PARENT).toBe('claude-code')
    expect(env.JARVIS_TOKEN).toMatch(/^[0-9a-f]{64}$/)
    expect(env.FISH_AUDIO_API_KEY).toBe('fk-test')
    expect(env.NO_PROXY).toBe('corp.example,127.0.0.1,localhost')
    expect(w.status()).toBe('JARVIS · starting')
  })

  test('loads the configured speech model from the start', { options: { sttModel: 'small.en' } }, async ($, on) => {
    const w = world(on)
    await startHelper($, w)
    expect(w.lastHelper().argv).toEqual([VENV_PYTHON, '-m', 'jarvis_voice', 'run', '--data-dir', DATA_DIR, '--stt-model', 'small.en'])
    expect(w.named('config')[0]?.body).toEqual({ pttKey: 'right ctrl', language: 'en' })
  })

  test('the model /jarvis setup installed wins over the setting', { options: { sttModel: 'small.en' } }, async ($, on) => {
    const w = world(on)
    w.store.set('sttModel', { model: 'medium', setting: 'small.en' })
    await startSession($, w)
    expect(w.lastHelper().argv.slice(-2)).toEqual(['--stt-model', 'medium'])
  })

  test('a setting changed since /jarvis setup <model> wins again', { options: { sttModel: 'small.en' } }, async ($, on) => {
    const w = world(on)
    w.store.set('sttModel', { model: 'medium', setting: 'auto' })
    await startSession($, w)
    expect(w.lastHelper().argv.slice(-2)).toEqual(['--stt-model', 'small.en'])
  })

  test('leaves FISH_AUDIO_API_KEY to the inherited environment when unset', async ($, on) => {
    const w = world(on)
    await startSession($, w)
    expect(w.lastHelper().request.env?.FISH_AUDIO_API_KEY).toBeUndefined()
  })

  test('reads events split across pieces, then heartbeats every 2 s with the bearer token', async ($, on) => {
    const w = world(on)
    await startSession($, w)
    const helper = w.lastHelper()
    const token = helper.request.env?.JARVIS_TOKEN
    const hello = JSON.stringify({ v: 1, type: 'hello', port: PORT, pid: 7, platform: 'windows', version: '0.1.0', capabilities: [] })
    helper.stdout(hello.slice(0, 20))
    helper.stdout(`${hello.slice(20)}\r\n{"v":1,"type":"state","state":"sle`)
    helper.stdout('eping"}\n')
    helper.event({ type: 'ready', sttModel: 'small.en', sttDevice: 'cpu', pttKey: 'right ctrl' })
    await w.settle()

    expect(w.status()).toBe('JARVIS · ready · hold right ctrl to talk')
    const config = w.named('config')
    expect(config).toHaveLength(1)
    expect(config[0]?.body).toEqual({ pttKey: 'right ctrl', language: 'en' })
    expect(config[0]?.url).toBe(`http://127.0.0.1:${PORT}/v1/config`)
    expect(config[0]?.headers.authorization).toBe(`Bearer ${token}`)

    expect(w.named('heartbeat')).toHaveLength(0)
    await w.clock.advance(HEARTBEAT_MS)
    await w.settle()
    expect(w.named('heartbeat')).toHaveLength(1)
    await w.clock.advance(HEARTBEAT_MS * 2)
    await w.settle()
    expect(w.named('heartbeat')).toHaveLength(3)
    expect(w.named('heartbeat')[0]?.headers.authorization).toBe(`Bearer ${token}`)
    expect(w.toasts).toContain('Jarvis is ready. Hold right ctrl to talk.')
  })

  test('restarts after unexpected exits with 1, 2, 5, 10 s backoff, then gives up', async ($, on) => {
    const w = world(on)
    await startSession($, w)
    w.lastHelper().stderr('Traceback: boom\n')
    w.lastHelper().exit(1)
    await w.settle()

    for (const [attempt, wait] of BACKOFF_MS.entries()) {
      expect(w.helpers()).toHaveLength(attempt + 1)
      expect(w.status()).toContain(`JARVIS · restarting · in ${wait / 1000} s`)
      await w.clock.advance(wait - 1)
      await w.settle()
      expect(w.helpers()).toHaveLength(attempt + 1) // not yet
      await w.clock.advance(1)
      await w.settle()
      expect(w.helpers()).toHaveLength(attempt + 2)
      w.lastHelper().stderr('Traceback: boom again\n')
      w.lastHelper().exit(1)
      await w.settle()
    }

    expect(w.helpers()).toHaveLength(BACKOFF_MS.length + 1)
    expect(w.status()).toBe('JARVIS · stopped · exited with code 1: Traceback: boom again · /jarvis restart')
    await w.clock.advance(120_000)
    await w.settle()
    expect(w.helpers()).toHaveLength(BACKOFF_MS.length + 1)

    // The user asking again starts a fresh sequence.
    await jarvis($, 'restart')
    await w.settle()
    expect(w.helpers()).toHaveLength(BACKOFF_MS.length + 2)
  })

  test('a helper that never says hello is let go and restarted; its late words are ignored', async ($, on) => {
    const w = world(on)
    await startSession($, w)
    const silent = w.lastHelper() // never writes, never exits (hung opening a device)
    const firstWait = BACKOFF_MS[0] ?? 0
    await w.clock.advance(HELLO_TIMEOUT_MS)
    await w.settle()
    expect(w.status()).toBe(`JARVIS · restarting · in ${firstWait / 1000} s: no hello within ${HELLO_TIMEOUT_MS / 1000} s`)
    await w.clock.advance(firstWait)
    await w.settle()
    expect(w.helpers()).toHaveLength(2)

    // The first child speaks up at last: nothing of it reaches the mod.
    silent.hello(40000)
    silent.exit(0)
    await w.settle()
    expect(w.status()).toBe('JARVIS · starting')
    expect(w.helpers()).toHaveLength(2)

    // It may still hold the lock: a late retry instead of "another window".
    w.lastHelper().exit(3)
    await w.settle()
    expect(w.status()).toBe('JARVIS · restarting · waiting for the previous helper to exit')
    await w.clock.advance(ORPHAN_RETRY_MS)
    await w.settle()
    w.lastHelper().hello()
    await w.clock.advance(HEARTBEAT_MS)
    await w.settle()
    expect(w.helpers()).toHaveLength(3)
    expect(w.named('heartbeat').map(command => command.url)).toEqual([`http://127.0.0.1:${PORT}/v1/heartbeat`])
  })

  test('restart lets go of a helper that ignores shutdown and starts a new one', async ($, on) => {
    const w = world(on)
    const stuck = await startHelper($, w) // answers shutdown, then never exits
    expect(await jarvis($, 'restart')).toBe('Restarting the voice helper.')
    await w.settle()
    expect(w.named('shutdown')).toHaveLength(1)
    await w.clock.advance(STOP_WAIT_MS * 2)
    await w.settle()
    expect(w.helpers()).toHaveLength(2)
    expect(w.status()).toBe('JARVIS · starting')

    // When it finally exits, that is not a crash of the new one.
    stuck.exit(1)
    await w.settle()
    await w.clock.advance(BACKOFF_MS[0] ?? 0)
    await w.settle()
    expect(w.helpers()).toHaveLength(2)
    expect(w.status()).toBe('JARVIS · starting')
  })

  test('a fatal error event is what the give-up shows', async ($, on) => {
    const w = world(on)
    await startSession($, w)
    for (let run = 0; run <= BACKOFF_MS.length; run += 1) {
      const helper = w.lastHelper()
      helper.event({ type: 'error', code: 'no_input_device', message: 'No microphone found', hint: 'plug in the headset', fatal: true })
      helper.exit(2)
      await w.settle()
      await w.clock.advance(BACKOFF_MS[run] ?? 0)
      await w.settle()
    }
    expect(w.status()).toBe('JARVIS · stopped · No microphone found (plug in the headset) · /jarvis restart')
  })

  test('already_running: shows "active in another window" and does not retry until /jarvis', async ($, on) => {
    const w = world(on)
    await startSession($, w)
    const helper = w.lastHelper()
    helper.event({ type: 'error', code: 'already_running', message: 'Jarvis is running in another window', fatal: true })
    helper.exit(3)
    await w.settle()
    expect(w.status()).toBe('JARVIS · active in another window')

    await w.clock.advance(60_000)
    await w.settle()
    expect(w.helpers()).toHaveLength(1)

    const text = await jarvis($, '')
    await w.settle()
    expect(text).toContain('Starting the voice helper')
    expect(w.helpers()).toHaveLength(2)
  })

  test('exit code 3 alone also means another window owns the helper', async ($, on) => {
    const w = world(on)
    await startSession($, w)
    w.lastHelper().exit(3)
    await w.settle()
    await w.clock.advance(30_000)
    await w.settle()
    expect(w.helpers()).toHaveLength(1)
    expect(w.status()).toBe('JARVIS · active in another window')
  })

  test('a reloaded module shuts down the helper the previous load left running', async ($, on) => {
    const w = world(on)
    // What a previous load of the module left in session state: the
    // orphaned helper's address (its parent end was killed by the reload).
    w.state.set('jarvis.helper', { port: 40000, token: 'old-token', pid: 99 })
    await $.session.start({ cwd: 'C:\\work', surface: 'terminal', isInteractive: true })
    await w.settle()
    const shutdown = w.named('shutdown')
    expect(shutdown).toHaveLength(1)
    expect(shutdown[0]?.url).toBe('http://127.0.0.1:40000/v1/shutdown')
    expect(shutdown[0]?.headers.authorization).toBe('Bearer old-token')
    expect(w.children).toHaveLength(0) // gives it a second to exit
    await w.clock.advance(1000)
    await w.settle()
    expect(w.helpers()).toHaveLength(1)

    // If the orphan still holds the lock, late retries outlive its watchdog.
    for (let retry = 1; retry <= ORPHAN_RETRIES; retry += 1) {
      w.lastHelper().exit(3)
      await w.settle()
      expect(w.status()).toBe('JARVIS · restarting · waiting for the previous helper to exit')
      await w.clock.advance(ORPHAN_RETRY_MS)
      await w.settle()
      expect(w.helpers()).toHaveLength(retry + 1)
    }
    w.lastHelper().exit(3)
    await w.settle()
    expect(w.status()).toBe('JARVIS · active in another window')
    expect(w.state.get('jarvis.helper')).toBeNull()
  })

  test('without the venv it asks for /jarvis setup and spawns nothing', async ($, on) => {
    const w = world(on, { installed: false })
    await startSession($, w)
    expect(w.children).toHaveLength(0)
    expect(w.status()).toBe('JARVIS · not set up · run /jarvis setup')
  })

  test('never starts in a cloud session', async ($, on) => {
    const w = world(on, { env: { HOME: '/root', CLAUDE_CODE_REMOTE: 'true' } })
    await startSession($, w)
    expect(w.children).toHaveLength(0)
    expect(await jarvis($, 'setup')).toContain('runs on your own computer')
  })

  test('the desktop app starts the helper when its surface attaches', async ($, on) => {
    const w = world(on)
    await $.session.start({ cwd: 'C:\\work', surface: null, isInteractive: false })
    await w.settle()
    expect(w.children).toHaveLength(0)
    await $.session.attach({ surface: 'mobile', clientId: 'mobile:default' })
    await w.settle()
    expect(w.children).toHaveLength(0)
    await $.session.attach({ surface: 'desktop', clientId: 'desktop:default' })
    await w.settle()
    expect(w.helpers()).toHaveLength(1)
  })

  test('a desktop attach that arrives while session.start runs still starts the helper', async ($, on) => {
    const w = world(on)
    const starting = $.session.start({ cwd: 'C:\\work', surface: null, isInteractive: false })
    await $.session.attach({ surface: 'desktop', clientId: 'desktop:default' })
    await starting
    await w.settle()
    expect(w.helpers()).toHaveLength(1)
    expect(w.status()).toBe('JARVIS · starting')
  })
})
