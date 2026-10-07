import { describe, expect, test } from 'claude-code/testing'

import type { JarvisHud } from '../types'
import { APP_LINK_KEY, appEndpointPath, parseEndpoint, replyStart } from './companion'
import { describePlatform } from './platform'
import { completeTurn, jarvis, startHelper, startSession, WINDOWS_ENV, world } from './test-harness'
import type { SentCommand, World } from './test-harness'

const APP_ENDPOINT = 'C:\\Users\\Rotem\\.jarvis\\app\\endpoint.json'
const APP_PORT = 50999
const APP_TOKEN = 'ab'.repeat(32)
const OK = { status: 200, body: { ok: true } }

/** The Jarvis app runs: its address file is there. */
function appRunning(w: World, token = APP_TOKEN, path = APP_ENDPOINT): void {
  w.files.set(path, JSON.stringify({ v: 1, port: APP_PORT, token, pid: 777, version: '0.8.0' }))
}

const isApp = (command: SentCommand): boolean => command.url.startsWith(`http://127.0.0.1:${APP_PORT}/`)
const pushes = (w: World): SentCommand[] => w.commands.filter(isApp)
const lastPush = (w: World): Record<string, unknown> => {
  const push = pushes(w).at(-1)
  if (push === undefined) throw new Error('nothing was pushed to the app')
  return push.body
}

describe('Jarvis app link', () => {
  test('finds the app and pushes the HUD with its token', async ($, on) => {
    const w = world(on)
    appRunning(w)
    await startHelper($, w)
    const push = pushes(w).at(-1)
    expect(push?.url).toBe(`http://127.0.0.1:${APP_PORT}/v1/hud`)
    expect(push?.headers).toMatchObject({ authorization: `Bearer ${APP_TOKEN}`, 'content-type': 'application/json' })
    expect(push?.body).toMatchObject({ v: 1, mode: 'sleeping', phase: 'sleeping', mic: 0, out: 0, actions: [], isOwner: true })
    expect(String(push?.body.sessionId)).toMatch(/^[0-9a-f]{16}$/)
    expect(typeof push?.body.at).toBe('number')
  })

  test('pushes each change at once: the ring, your words and what Claude ran', async ($, on) => {
    const w = world(on)
    appRunning(w)
    const helper = await startHelper($, w)
    helper.event({ type: 'state', state: 'listening' })
    await w.settle()
    expect(lastPush(w)).toMatchObject({ mode: 'listening', phase: 'listening' })
    helper.event({ type: 'utterance', id: 'u1', text: 'Run the tests', source: 'wake', durationMs: 900, language: 'en' })
    helper.event({ type: 'state', state: 'sleeping' })
    await $.turn.start({ text: 'Run the tests', turnId: 't1' })
    await $.tool.call({ tool: 'Bash', command: 'npm test', description: 'Run the unit tests' })
    await w.settle()
    expect(lastPush(w)).toMatchObject({ mode: 'thinking', utterance: 'Run the tests', actions: [{ label: 'Bash Run the unit tests', status: 'done' }] })
  })

  test('sends levels at most 15 times a second, and only while Jarvis listens or speaks', async ($, on) => {
    const w = world(on)
    appRunning(w)
    const helper = await startHelper($, w)
    helper.event({ type: 'state', state: 'listening' })
    await w.clock.advance(100)
    const before = pushes(w).length
    for (let i = 1; i <= 10; i += 1) {
      helper.event({ type: 'level', mic: i / 10, out: 0 })
      await w.settle()
    }
    expect(pushes(w).length - before).toBeLessThanOrEqual(1)
    await w.clock.advance(70)
    expect(lastPush(w)).toMatchObject({ mic: 1 })
    helper.event({ type: 'state', state: 'sleeping' })
    await w.clock.advance(100)
    const resting = pushes(w).length
    for (let i = 1; i <= 5; i += 1) {
      helper.event({ type: 'level', mic: i / 10, out: 0 })
      await w.settle()
    }
    expect(pushes(w).length).toBe(resting)
  })

  test('says it is alive every 2 seconds', async ($, on) => {
    const w = world(on)
    appRunning(w)
    await startHelper($, w)
    const before = pushes(w).length
    await w.clock.advance(6000)
    expect(pushes(w).length - before).toBe(3)
  })

  test('without the app it reads the address file now and then, and says nothing', async ($, on) => {
    const w = world(on)
    await startHelper($, w)
    const toasts = w.toasts.length
    await w.clock.advance(12_000)
    expect(pushes(w)).toHaveLength(0)
    expect(w.reads.filter(path => path === APP_ENDPOINT).length).toBeLessThanOrEqual(3)
    expect(w.toasts).toHaveLength(toasts)
  })

  test('backs off quietly when the app stops answering, and finds it again', async ($, on) => {
    const w = world(on)
    appRunning(w)
    await startHelper($, w)
    w.respond = command => {
      if (isApp(command)) throw new Error('connect ECONNREFUSED 127.0.0.1:50999')
      return OK
    }
    const toasts = w.toasts.length
    const logs = w.logs.length
    await w.clock.advance(2000)
    const failed = pushes(w).length
    await w.clock.advance(3000)
    expect(pushes(w).length).toBe(failed)
    w.respond = () => OK
    await w.clock.advance(4000)
    expect(pushes(w).length).toBeGreaterThan(failed)
    expect(w.toasts.slice(toasts)).toEqual([])
    // Debug lines ("jarvis: ...") land in the same list; nothing reaches the transcript.
    expect(w.logs.slice(logs).filter(line => !line.startsWith('jarvis: '))).toEqual([])
  })

  test('after a failed push it waits 5 seconds before reading the address file again', async ($, on) => {
    const w = world(on)
    appRunning(w)
    await startHelper($, w)
    await w.clock.advance(20_000)
    w.respond = command => {
      if (isApp(command)) throw new Error('connect ECONNREFUSED 127.0.0.1:50999')
      return OK
    }
    const reads = (): number => w.reads.filter(path => path === APP_ENDPOINT).length
    await w.clock.advance(2000)
    const afterFailure = reads()
    await w.clock.advance(4000)
    expect(reads()).toBe(afterFailure)
    await w.clock.advance(2000)
    expect(reads()).toBe(afterFailure + 1)
  })

  test('a restarted app (a new token) is found again', async ($, on) => {
    const w = world(on)
    appRunning(w)
    await startHelper($, w)
    const NEW_TOKEN = 'cd'.repeat(32)
    w.respond = command => (isApp(command) && command.headers.authorization !== `Bearer ${NEW_TOKEN}` ? { status: 401, body: {} } : OK)
    appRunning(w, NEW_TOKEN)
    await w.clock.advance(8000)
    expect(pushes(w).at(-1)?.headers.authorization).toBe(`Bearer ${NEW_TOKEN}`)
  })

  test('an app that hangs is given a second, and requests do not pile up', async ($, on) => {
    const w = world(on)
    appRunning(w)
    w.respond = async command => {
      if (isApp(command)) await w.clock.sleep(60_000)
      return OK
    }
    const helper = await startHelper($, w)
    for (const state of ['listening', 'transcribing', 'speaking', 'sleeping']) {
      helper.event({ type: 'state', state })
      await w.clock.advance(500)
    }
    expect(pushes(w).length).toBeLessThanOrEqual(2)
  })

  test("sends the start of Claude's last reply, and drops it when a new turn starts", async ($, on) => {
    const w = world(on)
    appRunning(w)
    await startHelper($, w)
    w.messages = [
      { role: 'user', text: 'What time is it?', toolUses: [] },
      { role: 'assistant', text: "It's **three** o'clock, sir.", toolUses: [] },
    ]
    await $.turn.start({ text: 'What time is it?', turnId: 't1' })
    await completeTurn($, 't1')
    await w.settle()
    expect(lastPush(w)).toMatchObject({ reply: "It's three o'clock, sir." })
    // Outside focus mode the pane's own copy stays as it was.
    expect((w.state.get('jarvis.hud') as JarvisHud).lastReply).toBeUndefined()
    await $.turn.start({ text: 'And tomorrow?', turnId: 't2' })
    await w.settle()
    expect(lastPush(w).reply).toBeUndefined()
  })

  test('a long reply is cut to its start', () => {
    const start = replyStart('word '.repeat(400)) ?? ''
    expect(start.length).toBeLessThanOrEqual(310)
    expect(start.endsWith('…')).toBe(true)
    expect(replyStart('```\ncode only\n```')).toBeUndefined()
  })

  test('never runs in a cloud session', async ($, on) => {
    const w = world(on, { env: { ...WINDOWS_ENV, CLAUDE_CODE_REMOTE: 'true' } })
    appRunning(w)
    await startSession($, w)
    await w.clock.advance(10_000)
    expect(w.reads).not.toContain(APP_ENDPOINT)
    expect(pushes(w)).toHaveLength(0)
    expect(await jarvis($, 'app')).toContain('cloud')
  })

  test('/jarvis app says whether the app is connected', async ($, on) => {
    const w = world(on)
    await startHelper($, w)
    expect(await jarvis($, 'app')).toContain('not running')
    appRunning(w)
    expect(await jarvis($, 'app')).toContain('0.8.0 is connected')
  })

  test('/jarvis app: an address left behind reads as not running; an app that answers wrongly did not answer', async ($, on) => {
    const w = world(on)
    appRunning(w)
    w.respond = command => {
      if (isApp(command)) throw new Error('connect ECONNREFUSED 127.0.0.1:50999')
      return OK
    }
    await startHelper($, w)
    expect(await jarvis($, 'app')).toContain('not running')
    w.respond = command => (isApp(command) ? { status: 500, body: { ok: false } } : OK)
    expect(await jarvis($, 'app')).toContain('did not answer (HTTP 500)')
  })

  test('/jarvis app off stops sending, in later sessions too; on sends again', async ($, on) => {
    const w = world(on)
    appRunning(w)
    await startHelper($, w)
    expect(await jarvis($, 'app off')).toContain('no longer sent')
    expect(w.store.get(APP_LINK_KEY)).toBe(false)
    const before = pushes(w).length
    await w.clock.advance(10_000)
    await startSession($, w)
    await w.clock.advance(10_000)
    expect(pushes(w)).toHaveLength(before)
    expect(await jarvis($, 'app on')).toContain('connected')
    await w.clock.advance(2000)
    expect(pushes(w).length).toBeGreaterThan(before)
  })

  test('JARVIS_HOME moves the address file', async ($, on) => {
    const custom = 'D:\\jarvis-test\\app\\endpoint.json'
    const w = world(on, { env: { ...WINDOWS_ENV, JARVIS_HOME: 'D:\\jarvis-test' } })
    appRunning(w, APP_TOKEN, custom)
    await startHelper($, w)
    expect(w.reads).toContain(custom)
    expect(pushes(w).length).toBeGreaterThan(0)
  })

  test('a window that does not run the helper says so', async ($, on) => {
    const w = world(on, { installed: false })
    appRunning(w)
    await startSession($, w)
    expect(lastPush(w)).toMatchObject({ mode: 'offline', phase: 'not_installed', isOwner: false })
  })

  test('reads only a well-formed address file', () => {
    const good = { v: 1, port: 50999, token: 'ab'.repeat(32), pid: 7, version: '0.8.0' }
    expect(parseEndpoint(JSON.stringify(good))).toEqual({ port: 50999, token: 'ab'.repeat(32), pid: 7, version: '0.8.0' })
    expect(parseEndpoint('not json')).toBeUndefined()
    expect(parseEndpoint(JSON.stringify({ ...good, v: 2 }))).toBeUndefined()
    expect(parseEndpoint(JSON.stringify({ ...good, port: 70000 }))).toBeUndefined()
    expect(parseEndpoint(JSON.stringify({ ...good, token: 'short' }))).toBeUndefined()
    expect(parseEndpoint(JSON.stringify({ ...good, pid: undefined }))).toBeUndefined()
  })

  test('the address file lives in the data folder unless JARVIS_HOME says otherwise', () => {
    const windows = describePlatform({ os: 'windows', home: 'C:\\Users\\Rotem' })
    expect(appEndpointPath(windows, {})).toBe(APP_ENDPOINT)
    expect(appEndpointPath(windows, { JARVIS_HOME: 'E:\\j' })).toBe('E:\\j\\app\\endpoint.json')
    expect(appEndpointPath(windows, { JARVIS_HOME: 'relative' })).toBe(APP_ENDPOINT)
    expect(appEndpointPath(windows, { JARVIS_HOME: '\\\\server\\share' })).toBe(APP_ENDPOINT)
    expect(appEndpointPath(windows, { JARVIS_HOME: ' E:/j ' })).toBe('E:\\j\\app\\endpoint.json')
    const linux = describePlatform({ os: 'linux', home: '/home/rotem' })
    expect(appEndpointPath(linux, {})).toBe('/home/rotem/.jarvis/app/endpoint.json')
  })
})
