// The two halves of the link, together: the plugin's own code (its address
// rule, Hud and Companion from plugin/hooks) against the app's own (paths,
// address file, link server and session board). Each half's other tests pin
// only its own side; these fail when the two drift apart. The engine port is
// the only stand-in: Node's fetch, fs and timers in place of Claude Code's.
//
// The plugin modules' types need Claude Code's declarations, which are not in
// git, so the app's tsc leaves this one file out (tsconfig.json); esbuild
// bundles it like every other test.

import assert from 'node:assert/strict'
import { promises as fsp, mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { homedir, tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'

import type { JarvisPhase, JarvisView } from '../../plugin/types'
import { appEndpointPath, Companion, type CompanionEngine, parseEndpoint } from '../../plugin/hooks/companion'
import { Hud } from '../../plugin/hooks/hud'
import { describePlatform } from '../../plugin/hooks/platform'
import { newToken, removeEndpointSync, writeEndpoint } from '../src/main/discovery'
import { endpointPath, jarvisHome } from '../src/main/paths'
import { startLinkServer } from '../src/main/server'
import { SessionBoard } from '../src/main/sessions'
import { buildView } from '../src/shared/view'

const OS = { win32: 'windows', darwin: 'macos' } as const
/** Every phase the plugin can send (plugin/types JarvisPhase). */
const PHASES: Record<JarvisPhase, true> = {
  unavailable: true,
  stopped: true,
  not_installed: true,
  setup: true,
  starting: true,
  sleeping: true,
  listening: true,
  transcribing: true,
  speaking: true,
  awake: true,
  error: true,
  restarting: true,
  elsewhere: true,
  failed: true,
}
const pluginPlatform = (platform: NodeJS.Platform, home: string) =>
  describePlatform({ os: platform === 'win32' || platform === 'darwin' ? OS[platform] : 'linux', home })

function tempHome(): string {
  return mkdtempSync(join(tmpdir(), 'jarvis-link-'))
}

async function until(what: string, isDone: () => boolean, timeoutMs = 5000): Promise<void> {
  const start = Date.now()
  while (!isDone()) {
    if (Date.now() - start > timeoutMs) throw new Error(`not within ${timeoutMs} ms: ${what}`)
    await new Promise(resolve => setTimeout(resolve, 20))
  }
}

/** The engine port as register.tsx builds it, for what the Hud and the Companion use. */
function nodeEngine(statuses: (number | string)[], debugs: string[], toasts: string[]): CompanionEngine {
  const timer = (handle: NodeJS.Timeout, stop: (handle: NodeJS.Timeout) => void) => {
    handle.unref()
    return { cancel: () => stop(handle) }
  }
  const store = new Map<string, unknown>()
  const engine = {
    now: async () => Date.now(),
    after: (ms: number, fn: () => void) => timer(setTimeout(fn, ms), clearTimeout),
    every: (ms: number, fn: () => void) => timer(setInterval(fn, ms), clearInterval),
    // Like $.http.fetch: no Origin header, and a Content-Length.
    fetch: async (url: string, init: { method?: string; headers?: Record<string, string>; body?: string }) => {
      try {
        const response = await fetch(url, init)
        statuses.push(response.status)
        return { status: response.status, ok: response.ok, headers: Object.fromEntries(response.headers), text: await response.text() }
      } catch (error) {
        statuses.push('refused')
        throw error
      }
    },
    readFile: (path: string) => fsp.readFile(path, 'utf8'),
    storeGet: async (key: string) => store.get(key),
    storeSet: async (key: string, value: unknown) => void store.set(key, value),
    debug: (text: string) => void debugs.push(text),
    toast: (text: string) => void toasts.push(text),
    log: (text: string) => void toasts.push(text),
    writeHud: async () => undefined,
    invalidate: () => undefined,
    openPane: async () => ({ ok: true }),
    blit: async () => ({ ok: true }),
  }
  return engine as unknown as CompanionEngine
}

test('the plugin and the app look for one address file', () => {
  const windowsHome = 'C:\\Users\\Rotem'
  for (const value of [undefined, '', 'E:\\j', ' E:/j ', 'e:\\jarvis home\\', 'jarvis', '\\\\server\\share', '\\foo', '/tmp/j']) {
    const env = value === undefined ? {} : { JARVIS_HOME: value }
    const plugin = appEndpointPath(pluginPlatform('win32', windowsHome), env)
    assert.equal(plugin, endpointPath(jarvisHome(env, 'win32', windowsHome), 'win32'), `Windows, JARVIS_HOME ${JSON.stringify(value)}`)
  }
  for (const [platform, home] of [['linux', '/home/rotem'], ['darwin', '/Users/rotem']] as const) {
    for (const value of [undefined, '', '/tmp/j', ' /tmp/j ', '/tmp/j/', '//tmp//j', 'tmp/j', 'C:\\j']) {
      const env = value === undefined ? {} : { JARVIS_HOME: value }
      const plugin = appEndpointPath(pluginPlatform(platform, home), env)
      assert.equal(plugin, endpointPath(jarvisHome(env, platform, home), platform), `${platform}, JARVIS_HOME ${JSON.stringify(value)}`)
    }
  }
})

test('the plugin reads the address file the app writes', async () => {
  const home = tempHome()
  try {
    const token = newToken()
    const written = await writeEndpoint(home, { v: 1, port: 50999, token, pid: 1234, version: '0.8.0' })
    assert.equal(appEndpointPath(pluginPlatform(process.platform, homedir()), { JARVIS_HOME: home }), written)
    assert.deepEqual(parseEndpoint(readFileSync(written, 'utf8')), { port: 50999, token, pid: 1234, version: '0.8.0' })
  } finally {
    rmSync(home, { recursive: true, force: true })
  }
})

test("every push from the plugin passes the app's checks and shows on its board", async () => {
  const home = tempHome()
  const token = newToken()
  const board = new SessionBoard()
  const phases = new Set<string>()
  const server = await startLinkServer({
    token,
    version: '0.8.0',
    onSnapshot: snapshot => {
      phases.add(snapshot.phase)
      board.accept(snapshot, performance.now())
    },
  })
  await writeEndpoint(home, { v: 1, port: server.port, token, pid: process.pid, version: '0.8.0' })
  const shown = () => buildView(board.current(performance.now()), true)

  // One Claude Code window, wired as app.ts wires it.
  const statuses: (number | string)[] = []
  const debugs: string[] = []
  const toasts: string[] = []
  const engine = nodeEngine(statuses, debugs, toasts)
  const hud = new Hud(engine)
  let view = { phase: 'sleeping' } as JarvisView
  const endpoint = appEndpointPath(pluginPlatform(process.platform, homedir()), { JARVIS_HOME: home })
  const companion = new Companion(engine, { endpointPath: endpoint, source: { hud, view: () => view, isOwner: () => true } })
  hud.onChange = () => companion.poke()
  const publish = (patch: Partial<JarvisView>): void => {
    view = { ...view, ...patch }
    hud.setPhase(view.phase)
    companion.poke()
  }
  const level = (mic: number, out: number): void => {
    hud.setLevels(mic, out)
    companion.poke()
  }

  try {
    publish({ phase: 'sleeping' })
    await companion.start()
    await until('the plugin finds the app', () => companion.isConnected)
    assert.deepEqual(await companion.check(), { state: 'connected', version: '0.8.0' })
    await until('the app shows the window standing by', () => shown().label === 'STANDING BY')

    for (const phase of Object.keys(PHASES) as JarvisPhase[]) {
      publish({ phase })
      await until(`the app takes the phase ${phase}`, () => phases.has(phase))
    }
    publish({ phase: 'sleeping' })

    publish({ phase: 'listening' })
    level(0.6, 0)
    await until('the app shows Jarvis listening', () => shown().mode === 'listening' && shown().mic === 0.6)

    // Longer than the limits: the plugin cuts what you said, the app the action's
    // label (the Hud's own labels are shorter); neither refuses them.
    publish({ phase: 'sleeping', lastUtterance: `Run the unit tests ${'and then some more '.repeat(40)}` })
    hud.onTurnStart()
    companion.onTurnStart()
    const bash = hud.onToolStart(`Bash ${'Run the unit tests '.repeat(6)}`)
    hud.onToolEnd(bash, false)
    hud.onTurnComplete()
    companion.setReply('**Two tests failed** in `server.test.ts`:\n\n```ts\nexpect(status).toBe(403)\n```\n\nEverything else passes.')
    publish({ phase: 'speaking' })
    level(0, 0.8)
    await until('the app shows Jarvis speaking the reply', () => shown().mode === 'speaking' && shown().reply !== '')

    const speaking = shown()
    assert.equal(speaking.label, 'SPEAKING')
    assert.equal(speaking.out, 0.8)
    assert.equal(speaking.utterance.length, 500)
    assert.ok(speaking.utterance.startsWith('Run the unit tests') && speaking.utterance.endsWith('…'))
    assert.match(speaking.reply, /^Two tests failed in server\.test\.ts/)
    assert.doesNotMatch(speaking.reply, /[*`]/)
    assert.deepEqual(
      speaking.actions.map(action => [action.mark, action.status, action.label.length <= 80]),
      [['✗', 'failed', true]],
    )
    assert.deepEqual([...new Set(statuses)], [200], 'every push was taken')

    // The app ends without removing its address (Task Manager): nothing answers
    // there, and the plugin lets go quietly and says the app is not running.
    await server.close()
    await until('the plugin notices the app is gone', () => {
      level(0, Math.random())
      return !companion.isConnected
    })
    assert.deepEqual(await companion.check(), { state: 'not_running' })
    // It quits properly: the address is gone too.
    removeEndpointSync(home, token)
    assert.deepEqual(await companion.check(), { state: 'not_running' })
    assert.deepEqual(toasts, [])
    assert.ok(debugs.every(line => line.startsWith('jarvis: ')), debugs.join('\n'))
  } finally {
    companion.dispose()
    hud.dispose()
    await server.close()
    rmSync(home, { recursive: true, force: true })
  }
})
