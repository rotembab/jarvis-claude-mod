// End-to-end: the built app in a real Electron, each test with its own Jarvis
// home and userData folder. On Linux the tests run under Xvfb and pass
// --no-sandbox (CI runners and root cannot use Chromium's sandbox); the app
// itself never does. JARVIS_SHOTS=<folder> also saves screenshots of each mode.

import { _electron as electron, type ElectronApplication, expect, type Page, test } from '@playwright/test'
import { spawn } from 'node:child_process'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs'
import { request } from 'node:http'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const APP_DIR = join(__dirname, '..')
const LINUX_ARGS = process.platform === 'linux' ? ['--no-sandbox'] : []
const VERSION: string = JSON.parse(readFileSync(join(APP_DIR, 'package.json'), 'utf8')).version
const SHOTS = process.env.JARVIS_SHOTS

type Endpoint = { v: number; port: number; token: string; pid: number; version: string }
type Dirs = { root: string; home: string; userData: string }
type Rect = { x: number; y: number; width: number; height: number }

const created: string[] = []
test.afterAll(() => {
  for (const dir of created) rmSync(dir, { recursive: true, force: true })
})

/** Playwright wants every value a string. */
function baseEnv(): Record<string, string> {
  return Object.fromEntries(Object.entries(process.env).filter((pair): pair is [string, string] => pair[1] !== undefined))
}

function tempDirs(): Dirs {
  const root = mkdtempSync(join(tmpdir(), 'jarvis-e2e-'))
  created.push(root)
  const dirs = { root, home: join(root, 'home'), userData: join(root, 'userData') }
  mkdirSync(dirs.userData, { recursive: true })
  return dirs
}

const appEnv = (dirs: Dirs): Record<string, string> => ({ ...baseEnv(), JARVIS_HOME: dirs.home, JARVIS_USER_DATA: dirs.userData })

function launch(dirs: Dirs): Promise<ElectronApplication> {
  return electron.launch({ args: [...LINUX_ARGS, APP_DIR], env: appEnv(dirs) })
}

function writeSettings(dirs: Dirs, settings: object): void {
  writeFileSync(join(dirs.userData, 'settings.json'), JSON.stringify({ v: 1, ...settings }))
}

const endpointFile = (home: string): string => join(home, 'app', 'endpoint.json')

async function readEndpoint(home: string): Promise<Endpoint> {
  await expect.poll(() => existsSync(endpointFile(home)), { timeout: 20_000 }).toBe(true)
  return JSON.parse(readFileSync(endpointFile(home), 'utf8'))
}

/** The window showing `name`.html; app.windows() lists hidden windows too. */
async function windowFor(app: ElectronApplication, name: 'overlay' | 'orb'): Promise<Page> {
  for (let attempt = 0; attempt < 20; attempt += 1) {
    const page = app.windows().find(win => win.url().endsWith(`/${name}.html`))
    if (page !== undefined) return page
    await app.waitForEvent('window', { timeout: 500 }).catch(() => undefined)
  }
  throw new Error(`the app opened no ${name} window`)
}

function push(ep: Endpoint, body: unknown, options: { token?: string; headers?: Record<string, string> } = {}): Promise<Response> {
  return fetch(`http://127.0.0.1:${ep.port}/v1/hud`, {
    method: 'POST',
    headers: { authorization: `Bearer ${options.token ?? ep.token}`, 'content-type': 'application/json', ...options.headers },
    body: JSON.stringify(body),
  })
}

function snapshot(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return { v: 1, sessionId: 'e2e1', mode: 'sleeping', phase: 'sleeping', mic: 0, out: 0, actions: [], isOwner: true, at: Date.now(), ...overrides }
}

/** A request fetch cannot make: one with a Host header of our choosing. */
function statusWithHost(ep: Endpoint, host: string): Promise<number> {
  return new Promise((resolve, reject) => {
    const req = request(
      { host: '127.0.0.1', port: ep.port, path: '/v1/health', headers: { host, authorization: `Bearer ${ep.token}` }, agent: false },
      res => {
        res.resume()
        resolve(res.statusCode ?? 0)
      },
    )
    req.on('error', reject)
    req.end()
  })
}

type WindowInfo = { isVisible: boolean; isAlwaysOnTop: boolean; isFocusable: boolean; bounds: Rect }

function windowInfo(app: ElectronApplication, name: 'overlay' | 'orb'): Promise<WindowInfo | undefined> {
  return app.evaluate(({ BrowserWindow }, suffix) => {
    const win = BrowserWindow.getAllWindows().find(each => each.webContents.getURL().endsWith(suffix))
    if (win === undefined) return undefined
    return { isVisible: win.isVisible(), isAlwaysOnTop: win.isAlwaysOnTop(), isFocusable: win.isFocusable(), bounds: win.getBounds() }
  }, `/${name}.html`)
}

const overlayInfo = (app: ElectronApplication) => windowInfo(app, 'overlay')
const overlayShown = async (app: ElectronApplication): Promise<boolean | undefined> => (await overlayInfo(app))?.isVisible

const primaryWorkArea = (app: ElectronApplication): Promise<Rect> => app.evaluate(({ screen }) => screen.getPrimaryDisplay().workArea)

function expectNear(actual: Rect, expected: Rect): void {
  for (const key of ['x', 'y', 'width', 'height'] as const) expect(Math.abs(actual[key] - expected[key]), key).toBeLessThanOrEqual(2)
}

test('writes its address and guards the link', async () => {
  const dirs = tempDirs()
  const app = await launch(dirs)
  try {
    const ep = await readEndpoint(dirs.home)
    await windowFor(app, 'overlay')
    expect(ep.v).toBe(1)
    expect(Number.isInteger(ep.port) && ep.port > 0 && ep.port < 65536).toBe(true)
    expect(ep.token).toMatch(/^[0-9a-f]{64}$/)
    expect(ep.pid).toBeGreaterThan(0)
    expect(ep.version).toBe(VERSION)
    if (process.platform === 'linux') expect(statSync(endpointFile(dirs.home)).mode & 0o777).toBe(0o600)

    const health = (headers: Record<string, string>) => fetch(`http://127.0.0.1:${ep.port}/v1/health`, { headers })
    const ok = await health({ authorization: `Bearer ${ep.token}` })
    expect(ok.status).toBe(200)
    expect(await ok.json()).toEqual({ ok: true, v: 1, version: VERSION, pid: ep.pid })
    expect((await health({})).status).toBe(401)
    expect((await push(ep, snapshot(), { token: 'f'.repeat(64) })).status).toBe(401)
    expect((await health({ authorization: `Bearer ${ep.token}`, origin: 'https://example.com' })).status).toBe(403)
    expect((await push(ep, snapshot(), { headers: { origin: 'null' } })).status).toBe(403)
    expect(await statusWithHost(ep, `evil.example:${ep.port}`)).toBe(403)
    expect((await fetch(`http://127.0.0.1:${ep.port}/v1/hud`, { headers: { authorization: `Bearer ${ep.token}` } })).status).toBe(405)
    expect((await push(ep, { v: 1, mode: 'dancing' })).status).toBe(400)
    expect((await push(ep, snapshot())).status).toBe(200)
  } finally {
    await app.close()
  }
})

test('the orb shows offline, and the overlay appears when Jarvis listens', async () => {
  const dirs = tempDirs()
  const app = await launch(dirs)
  try {
    const ep = await readEndpoint(dirs.home)
    const orb = await windowFor(app, 'orb')
    const overlay = await windowFor(app, 'overlay')
    await expect(orb.locator('body')).toHaveAttribute('data-mode', 'offline')

    expect((await push(ep, snapshot())).status).toBe(200)
    await expect(orb.locator('body')).toHaveAttribute('data-mode', 'sleeping')
    await overlay.waitForTimeout(600)
    expect(await overlayShown(app)).toBe(false)

    // Speech and Claude's words go in as text, never as markup.
    expect((await push(ep, snapshot({ mode: 'listening', phase: 'listening', mic: 0.4, utterance: '<b>hi</b>' }))).status).toBe(200)
    await expect.poll(() => overlayShown(app)).toBe(true)
    await expect(overlay.locator('#utterance')).toHaveText('<b>hi</b>')
    await expect(overlay.locator('#utterance b')).toHaveCount(0)
    await expect(overlay.locator('body')).toHaveAttribute('data-visible', 'true')

    const info = await overlayInfo(app)
    expect(info?.isAlwaysOnTop).toBe(true)
    expect(info?.isFocusable).toBe(false)
    expectNear(info?.bounds ?? { x: 0, y: 0, width: 0, height: 0 }, await primaryWorkArea(app))
  } finally {
    await app.close()
  }
})

test('it shows each mode with what was said, the reply and the actions', async () => {
  const dirs = tempDirs()
  // "Always", so the overlay shows standing by and offline too, for the screenshots.
  writeSettings(dirs, { overlay: 'always', showOrb: true })
  const app = await launch(dirs)
  try {
    const ep = await readEndpoint(dirs.home)
    const orb = await windowFor(app, 'orb')
    const overlay = await windowFor(app, 'overlay')
    if (SHOTS !== undefined) mkdirSync(SHOTS, { recursive: true })

    const shoot = async (name: string): Promise<void> => {
      if (SHOTS === undefined) return
      // Past the fade and a ring redraw.
      await overlay.waitForTimeout(700)
      await overlay.screenshot({ path: join(SHOTS, `overlay-${name}.png`), omitBackground: true })
      await orb.screenshot({ path: join(SHOTS, `orb-${name}.png`), omitBackground: true })
    }

    await expect(overlay.locator('body')).toHaveAttribute('data-visible', 'true')
    await expect(overlay.locator('#label')).toHaveText('OFFLINE')
    await expect(overlay.locator('#note')).toHaveText('Waiting for Claude Code. Start a session with the Jarvis plugin.')
    await shoot('offline')

    const steps: { name: string; push: Record<string, unknown>; label: string; check: () => Promise<void> }[] = [
      {
        name: 'sleeping',
        push: {},
        label: 'STANDING BY',
        check: async () => {
          await expect(overlay.locator('#panel')).toBeHidden()
        },
      },
      {
        name: 'listening',
        push: { mode: 'listening', phase: 'listening', mic: 0.6, utterance: 'Run the unit tests and tell me what failed' },
        label: 'LISTENING',
        check: async () => {
          await expect(overlay.locator('#utterance')).toHaveText('Run the unit tests and tell me what failed')
          await expect(overlay.locator('#reply')).toBeHidden()
        },
      },
      {
        name: 'thinking',
        push: {
          mode: 'thinking',
          utterance: 'Run the unit tests and tell me what failed',
          actions: [
            { label: 'Bash Run the unit tests', status: 'running' },
            { label: 'Read app/src/main/server.ts', status: 'done' },
          ],
        },
        label: 'THINKING',
        check: async () => {
          await expect(overlay.locator('#actions li')).toHaveCount(2)
          await expect(overlay.locator('#actions li').first()).toHaveAttribute('data-status', 'running')
          await expect(overlay.locator('#actions li .mark').first()).toHaveText('›')
        },
      },
      {
        name: 'speaking',
        push: {
          mode: 'speaking',
          phase: 'speaking',
          out: 0.8,
          utterance: 'Run the unit tests and tell me what failed',
          reply: 'All 61 tests pass. The link server refuses requests with an Origin header and checks the token before anything else.',
          actions: [
            { label: 'Bash Run the unit tests', status: 'done' },
            { label: 'Edit app/src/main/server.ts', status: 'failed' },
            { label: 'Read app/src/main/server.ts', status: 'done' },
          ],
        },
        label: 'SPEAKING',
        check: async () => {
          await expect(overlay.locator('#reply')).toHaveText(/^All 61 tests pass\./)
          await expect(overlay.locator('#actions li .mark')).toHaveText(['✓', '✗', '✓'])
          await expect(overlay.locator('#actions li').nth(1)).toHaveAttribute('data-status', 'failed')
        },
      },
      {
        name: 'interrupted',
        push: { mode: 'interrupted', phase: 'listening', mic: 0.7, utterance: 'Stop' },
        label: 'LISTENING',
        check: async () => {
          await expect(overlay.locator('#utterance')).toHaveText('Stop')
          await expect(overlay.locator('#actions li')).toHaveCount(0)
        },
      },
    ]
    for (const step of steps) {
      expect((await push(ep, snapshot(step.push))).status, step.name).toBe(200)
      const mode = (step.push.mode as string | undefined) ?? 'sleeping'
      await expect(overlay.locator('body')).toHaveAttribute('data-mode', mode)
      await expect(orb.locator('body')).toHaveAttribute('data-mode', mode)
      await expect(overlay.locator('#label')).toHaveText(step.label)
      await step.check()
      // The ring is the plugin's SVG, drawn into both pages.
      await expect(overlay.locator('#ring svg')).toHaveCount(1)
      await expect(orb.locator('#ring svg')).toHaveCount(1)
      await shoot(step.name)
    }

    // The longest texts the app takes still fit, beside the ring on a wide
    // screen and under it on a tall one, and the ring takes most of the height.
    const label = (verb: string) => `${verb} ${'app/src/main/a-long-folder-name/'.repeat(3)}`.slice(0, 80)
    const longest = () => snapshot({
      mode: 'speaking',
      phase: 'speaking',
      out: 0.5,
      utterance: 'Run the unit tests, then '.repeat(20).slice(0, 500),
      reply: 'Two tests failed in the link server, both about its checks. '.repeat(10).slice(0, 600),
      actions: ['Bash', 'Read', 'Edit', 'Grep', 'Write', 'Glob'].map(verb => ({ label: label(verb), status: 'done' })),
    })
    expect((await push(ep, longest())).status).toBe(200)
    await expect(overlay.locator('#actions li')).toHaveCount(6)
    const fit = () =>
      overlay.evaluate(() => {
        const box = (id: string) => document.getElementById(id)?.getBoundingClientRect() ?? new DOMRect()
        const parts = ['ring', 'label', 'panel'].map(box)
        return {
          width: innerWidth,
          height: innerHeight,
          ring: box('ring').height,
          isInside: parts.every(part => part.top >= 0 && part.left >= 0 && part.bottom <= innerHeight && part.right <= innerWidth),
        }
      })
    const full = await fit()
    expect(full.isInside, JSON.stringify(full)).toBe(true)
    expect(full.ring / full.height, JSON.stringify(full)).toBeGreaterThanOrEqual(0.55)
    await shoot('longest')
    // Other screens, by resizing the overlay: 1080p at 150%, 4:3, 5:4, 1440p and portrait.
    for (const [width, height] of [[1280, 688], [1024, 768], [1280, 1024], [2560, 1400], [800, 1240]] as const) {
      await app.evaluate(({ BrowserWindow }, size) => {
        const win = BrowserWindow.getAllWindows().find(each => each.webContents.getURL().endsWith('/overlay.html'))
        win?.setResizable(true)
        win?.setBounds({ x: 0, y: 0, ...size })
      }, { width, height })
      await expect.poll(async () => (await fit()).width).toBe(width)
      // Again, so the session stays live (a session unheard from for 6 s is gone).
      expect((await push(ep, longest())).status).toBe(200)
      await expect(overlay.locator('#actions li')).toHaveCount(6)
      const sized = await fit()
      expect(sized.isInside, JSON.stringify(sized)).toBe(true)
      if (width > height) expect(sized.ring / sized.height, JSON.stringify(sized)).toBeGreaterThanOrEqual(0.55)
      await shoot(`longest-${width}x${height}`)
    }
  } finally {
    await app.close()
  }
})

test('it fades out after the turn and goes offline when pushes stop', async () => {
  const dirs = tempDirs()
  const app = await launch(dirs)
  try {
    const ep = await readEndpoint(dirs.home)
    const orb = await windowFor(app, 'orb')
    await windowFor(app, 'overlay')
    await push(ep, snapshot({ mode: 'listening', phase: 'listening' }))
    await expect.poll(() => overlayShown(app)).toBe(true)
    await push(ep, snapshot())
    // About 4.4 s: the linger, then the fade.
    await expect.poll(() => overlayShown(app), { timeout: 10_000 }).toBe(false)
    // About 6 s after the last push: three missed heartbeats.
    await expect(orb.locator('body')).toHaveAttribute('data-mode', 'offline', { timeout: 10_000 })
  } finally {
    await app.close()
  }
})

test("the orb's centre toggles the overlay", async () => {
  const dirs = tempDirs()
  const app = await launch(dirs)
  try {
    await readEndpoint(dirs.home)
    const orb = await windowFor(app, 'orb')
    await windowFor(app, 'overlay')
    expect(await overlayShown(app)).toBe(false)
    await orb.click('#hit')
    await expect.poll(() => overlayShown(app)).toBe(true)
    await orb.click('#hit')
    await expect.poll(() => overlayShown(app)).toBe(false)
    expect(await app.evaluate(({ globalShortcut }) => globalShortcut.isRegistered('Control+Alt+J'))).toBe(true)
  } finally {
    await app.close()
  }
})

test('the pages stay locked down', async () => {
  const dirs = tempDirs()
  const app = await launch(dirs)
  try {
    await readEndpoint(dirs.home)
    const overlay = await windowFor(app, 'overlay')
    await overlay.waitForLoadState('load')

    expect(await overlay.evaluate(() => window.open('https://example.com') === null)).toBe(true)
    await overlay.evaluate(() => {
      location.href = 'https://example.com'
    }).catch(() => undefined)
    await overlay.waitForTimeout(500)
    expect(overlay.url()).toMatch(/\/overlay\.html$/)
    expect(await overlay.evaluate(() => fetch('https://example.com').then(() => 'loaded', () => 'refused'))).toBe('refused')

    // page.evaluate itself is exempt from the page's policy, so test it with a script element.
    const injected = await overlay.evaluate(
      () =>
        new Promise<{ directive: string; flag: unknown }>(resolve => {
          const flagged = window as unknown as { injected?: boolean }
          document.addEventListener('securitypolicyviolation', event => resolve({ directive: event.effectiveDirective, flag: flagged.injected }), { once: true })
          const script = document.createElement('script')
          script.textContent = 'window.injected = true'
          document.head.append(script)
          setTimeout(() => resolve({ directive: 'none fired', flag: flagged.injected }), 3000)
        }),
    )
    expect(injected.directive).toMatch(/^script-src/)
    expect(injected.flag).toBeUndefined()

    expect(await overlay.evaluate(() => Object.keys(window.jarvis).sort())).toEqual(['onView', 'openMenu', 'toggleOverlay'])
    expect(await overlay.evaluate(() => typeof (globalThis as { require?: unknown }).require)).toBe('undefined')
    expect(await overlay.evaluate(() => typeof (globalThis as { process?: unknown }).process)).toBe('undefined')
  } finally {
    await app.close()
  }
})

test('settings: orb position, off-screen reset, no orb, always', async () => {
  const run = async (settings: object, check: (app: ElectronApplication) => Promise<void>): Promise<void> => {
    const dirs = tempDirs()
    writeSettings(dirs, settings)
    const app = await launch(dirs)
    try {
      await readEndpoint(dirs.home)
      await windowFor(app, 'overlay')
      await check(app)
    } finally {
      await app.close()
    }
  }

  await run({ orb: { x: 100, y: 120 } }, async app => {
    await windowFor(app, 'orb')
    await expect.poll(async () => (await windowInfo(app, 'orb'))?.bounds).toMatchObject({ x: 100, y: 120, width: 180, height: 180 })
  })

  await run({ orb: { x: -5000, y: -5000 } }, async app => {
    await windowFor(app, 'orb')
    const area = await primaryWorkArea(app)
    const corner = { x: area.x + area.width - 204, y: area.y + area.height - 204, width: 180, height: 180 }
    await expect.poll(async () => (await windowInfo(app, 'orb'))?.bounds).toBeTruthy()
    expectNear((await windowInfo(app, 'orb'))?.bounds ?? { x: 0, y: 0, width: 0, height: 0 }, corner)
  })

  await run({ showOrb: false }, async app => {
    await app.evaluate(() => new Promise(resolve => setTimeout(resolve, 1000)))
    expect(await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().map(win => win.webContents.getURL().split('/').pop()))).toEqual([
      'overlay.html',
    ])
  })

  await run({ overlay: 'always' }, async app => {
    await expect.poll(() => overlayShown(app)).toBe(true)
  })
})

test('a second copy exits and the first shows its overlay', async () => {
  const dirs = tempDirs()
  const app = await launch(dirs)
  try {
    await readEndpoint(dirs.home)
    await windowFor(app, 'overlay')
    expect(await overlayShown(app)).toBe(false)
    // Spawned, not launched through Playwright, which would wait for a window that never opens.
    const electronBinary = require('electron') as string
    const second = spawn(electronBinary, [...LINUX_ARGS, APP_DIR], { env: appEnv(dirs), stdio: 'ignore' })
    const code = await new Promise<number | null>((resolve, reject) => {
      const timer = setTimeout(() => {
        second.kill()
        reject(new Error('the second copy did not exit within 10 s'))
      }, 10_000)
      second.on('exit', exitCode => {
        clearTimeout(timer)
        resolve(exitCode)
      })
    })
    expect(code).toBe(0)
    await expect.poll(() => overlayShown(app)).toBe(true)
  } finally {
    await app.close()
  }
})

test('removes its address on quit, unless another copy took it', async () => {
  const dirs = tempDirs()
  // Closed only once its windows are up: Playwright's quit can land in the
  // middle of start-up code, between two windows, which no real quit can.
  const started = async (app: ElectronApplication): Promise<Endpoint> => {
    const ep = await readEndpoint(dirs.home)
    await windowFor(app, 'overlay')
    await windowFor(app, 'orb')
    return ep
  }
  const first = await launch(dirs)
  await started(first)
  await first.close()
  expect(existsSync(endpointFile(dirs.home))).toBe(false)

  const second = await launch(dirs)
  const ep = await started(second)
  writeFileSync(endpointFile(dirs.home), JSON.stringify({ ...ep, token: 'e'.repeat(64) }) + '\n')
  await second.close()
  expect(existsSync(endpointFile(dirs.home))).toBe(true)
})
