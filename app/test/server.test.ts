import assert from 'node:assert/strict'
import { request } from 'node:http'
import { after, before, test } from 'node:test'

import { type LinkServer, startLinkServer } from '../src/main/server'
import type { AppSnapshot } from '../src/shared/snapshot'

const TOKEN = 'a'.repeat(64)
const SNAPSHOT = {
  v: 1,
  sessionId: '3f9a0c1d2b4e5f60',
  mode: 'listening',
  phase: 'listening',
  mic: 1.4,
  out: 0,
  actions: [],
  isOwner: true,
  at: 1759870000000,
}

let server: LinkServer
const received: AppSnapshot[] = []

before(async () => {
  server = await startLinkServer({ token: TOKEN, version: '0.8.0', onSnapshot: snapshot => received.push(snapshot) })
})

after(() => server.close())

type Reply = { status: number; headers: Record<string, string | string[] | undefined>; body: any }

/**
 * One request through node:http, which (unlike fetch) lets a test set Host
 * and send a chunked body. `body: undefined` sends headers only.
 */
function call(
  options: { method?: string; path?: string; headers?: Record<string, string>; body?: string | Buffer; chunked?: boolean; token?: string | null } = {},
): Promise<Reply> {
  const headers: Record<string, string> = { host: `127.0.0.1:${server.port}` }
  if (options.token !== null) headers.authorization = `Bearer ${options.token ?? TOKEN}`
  if (options.body !== undefined && !options.chunked) headers['content-length'] = String(Buffer.byteLength(options.body))
  Object.assign(headers, options.headers)
  return new Promise((resolve, reject) => {
    const req = request(
      { host: '127.0.0.1', port: server.port, method: options.method ?? 'GET', path: options.path ?? '/v1/health', headers, agent: false },
      res => {
        const chunks: Buffer[] = []
        res.on('data', chunk => chunks.push(chunk))
        res.on('end', () => {
          const text = Buffer.concat(chunks).toString('utf8')
          resolve({ status: res.statusCode ?? 0, headers: res.headers, body: text === '' ? undefined : JSON.parse(text) })
        })
      },
    )
    req.on('error', reject)
    if (options.chunked) {
      req.write(options.body ?? '')
      req.end()
    } else if (options.body !== undefined) {
      req.end(options.body)
    } else if (options.headers?.['content-length'] !== undefined) {
      // Headers only: the server must answer without waiting for the body.
      req.flushHeaders()
    } else {
      req.end()
    }
  })
}

const hud = (body: unknown, extra: Parameters<typeof call>[0] = {}) =>
  call({ method: 'POST', path: '/v1/hud', body: typeof body === 'string' ? body : JSON.stringify(body), ...extra })

test('it listens on 127.0.0.1 only', () => {
  assert.equal(server.host, '127.0.0.1')
  assert.ok(server.port > 0)
})

test('health answers with the version and pid, and safe headers', async () => {
  const reply = await call()
  assert.equal(reply.status, 200)
  assert.deepEqual(reply.body, { ok: true, v: 1, version: '0.8.0', pid: process.pid })
  assert.equal(reply.headers['content-type'], 'application/json; charset=utf-8')
  assert.equal(reply.headers['cache-control'], 'no-store')
  assert.equal(reply.headers['x-content-type-options'], 'nosniff')
  assert.equal(Object.keys(reply.headers).some(name => name.startsWith('access-control-')), false)
})

test('a snapshot is accepted, normalized and handed on once', async () => {
  received.length = 0
  const reply = await hud(SNAPSHOT)
  assert.equal(reply.status, 200)
  assert.deepEqual(reply.body, { ok: true })
  assert.equal(received.length, 1)
  assert.deepEqual(received[0], { ...SNAPSHOT, mic: 1 })
})

test('a missing or wrong token is refused', async () => {
  for (const token of [null, 'b'.repeat(64), TOKEN + 'x', '']) {
    const reply = await call({ token })
    assert.equal(reply.status, 401, String(token))
    assert.equal(reply.body.error.code, 'unauthorized')
    assert.equal(reply.headers.connection, 'close')
  }
  assert.equal((await call({ headers: { authorization: `bearer ${TOKEN}` } })).status, 200)
  assert.equal((await call({ headers: { authorization: `Basic ${TOKEN}` } })).status, 401)
})

test('a request from a web page or for another host is refused, even with the token', async () => {
  const refused: Record<string, string>[] = [
    { origin: 'null' },
    { origin: `http://127.0.0.1:${server.port}` },
    { host: `evil.example:${server.port}` },
    { host: '127.0.0.1' },
  ]
  for (const headers of refused) {
    const reply = await call({ headers })
    assert.equal(reply.status, 403, JSON.stringify(headers))
    assert.equal(reply.body.error.code, 'forbidden')
    assert.equal(reply.headers.connection, 'close')
  }
  assert.equal((await call({ headers: { host: `localhost:${server.port}` } })).status, 200)
  // The Origin check comes first: no token needed to be refused.
  assert.equal((await call({ token: null, headers: { origin: 'https://example.com' } })).status, 403)
})

test('routes: unknown paths, wrong methods and query strings', async () => {
  const unknown = await call({ path: '/v1/nope' })
  assert.equal(unknown.status, 404)
  assert.equal(unknown.body.error.code, 'not_found')
  const getHud = await call({ path: '/v1/hud' })
  assert.equal(getHud.status, 405)
  assert.equal(getHud.headers.allow, 'POST')
  const postHealth = await call({ method: 'POST', path: '/v1/health', body: '{}' })
  assert.equal(postHealth.status, 405)
  assert.equal(postHealth.headers.allow, 'GET')
  assert.equal((await call({ path: '/v1/health?x=1' })).status, 200)
})

test('bodies: a length is required, capped, and must be a valid snapshot', async () => {
  received.length = 0
  const chunked = await hud(SNAPSHOT, { chunked: true })
  assert.equal(chunked.status, 411)
  assert.equal(chunked.body.error.code, 'length_required')

  const tooLarge = await call({ method: 'POST', path: '/v1/hud', headers: { 'content-length': '70000' } })
  assert.equal(tooLarge.status, 413)
  assert.equal(tooLarge.body.error.code, 'too_large')
  assert.equal(tooLarge.headers.connection, 'close')

  const badJson = await hud('{"v":1,')
  assert.equal(badJson.status, 400)
  assert.equal(badJson.body.error.code, 'bad_json')

  const badUtf8 = await call({ method: 'POST', path: '/v1/hud', body: Buffer.from([0x22, 0xff, 0x22]) })
  assert.equal(badUtf8.status, 400)
  assert.equal(badUtf8.body.error.code, 'bad_json')

  const badSnapshot = await hud({ ...SNAPSHOT, v: 2 })
  assert.equal(badSnapshot.status, 400)
  assert.equal(badSnapshot.body.error.code, 'bad_snapshot')
  assert.equal(badSnapshot.body.error.message, 'v must be 1')
  assert.equal(received.length, 0)
})

test('a failing display answers 500', async () => {
  const failing = await startLinkServer({
    token: TOKEN,
    version: '0.8.0',
    onSnapshot: () => {
      throw new Error('no window')
    },
  })
  const original = server
  server = failing
  try {
    const reply = await hud(SNAPSHOT)
    assert.equal(reply.status, 500)
    assert.equal(reply.body.error.code, 'internal')
  } finally {
    server = original
    await failing.close()
  }
})

test('after close, requests are refused', async () => {
  const closing = await startLinkServer({ token: TOKEN, version: '0.8.0', onSnapshot: () => undefined })
  const port = closing.port
  await closing.close()
  await assert.rejects(fetch(`http://127.0.0.1:${port}/v1/health`, { headers: { authorization: `Bearer ${TOKEN}` } }))
})
