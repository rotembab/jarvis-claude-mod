// The link server: the plugin POSTs HUD snapshots here. It listens on
// 127.0.0.1 only and answers only a caller that read the address file (the
// bearer token) and is not a web page: browsers always send Origin on a
// cross-site POST and a Host naming the site, so both are refused, which also
// stops DNS rebinding. The checks run in a fixed order and the first failure
// answers (docs/APP.md lists them).

import { timingSafeEqual } from 'node:crypto'
import { createServer, type IncomingMessage, type ServerResponse } from 'node:http'
import type { AddressInfo } from 'node:net'

import { type AppSnapshot, parseSnapshot } from '../shared/snapshot'

export const MAX_BODY_BYTES = 65_536

export type LinkServerOptions = {
  token: string
  version: string
  onSnapshot: (snapshot: AppSnapshot) => void
}

export type LinkServer = { host: '127.0.0.1'; port: number; close: () => Promise<void> }

const HOST = '127.0.0.1'
const ALLOW: Record<string, string> = { '/v1/health': 'GET', '/v1/hud': 'POST' }

type Answer = { status: number; body: unknown; headers?: Record<string, string>; close?: boolean }

const failure = (status: number, code: string, message: string, extra: Partial<Answer> = {}): Answer => ({
  status,
  body: { ok: false, error: { code, message } },
  ...extra,
})

function send(res: ServerResponse, answer: Answer): void {
  const text = JSON.stringify(answer.body)
  res.writeHead(answer.status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Cache-Control': 'no-store',
    'X-Content-Type-Options': 'nosniff',
    'Content-Length': String(Buffer.byteLength(text)),
    ...answer.headers,
    // Node closes the socket after a response that says so, unread body and all.
    ...(answer.close ? { Connection: 'close' } : {}),
  })
  res.end(text)
}

function hasToken(header: string | undefined, token: Buffer): boolean {
  if (header === undefined) return false
  const space = header.indexOf(' ')
  if (space < 0 || header.slice(0, space).toLowerCase() !== 'bearer') return false
  const given = Buffer.from(header.slice(space + 1), 'utf8')
  return given.length === token.length && timingSafeEqual(given, token)
}

/** Reads exactly the declared body; Node's parser stops at Content-Length. */
function readBody(req: IncomingMessage): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = []
    let size = 0
    req.on('data', (chunk: Buffer) => {
      size += chunk.length
      if (size > MAX_BODY_BYTES) {
        req.destroy()
        reject(new Error('body too large'))
        return
      }
      chunks.push(chunk)
    })
    req.on('end', () => resolve(Buffer.concat(chunks)))
    req.on('error', reject)
    // A caller that hangs up mid-body never sends 'end'; after 'end' this is a no-op.
    req.on('close', () => reject(new Error('the connection closed')))
  })
}

const UTF8 = new TextDecoder('utf-8', { fatal: true })

export async function startLinkServer(options: LinkServerOptions): Promise<LinkServer> {
  const token = Buffer.from(options.token, 'utf8')
  let port = 0

  async function answer(req: IncomingMessage): Promise<Answer> {
    if (req.headers.origin !== undefined) {
      return failure(403, 'forbidden', 'requests with an Origin header are not accepted', { close: true })
    }
    const host = (req.headers.host ?? '').trim().toLowerCase()
    if (host !== `127.0.0.1:${port}` && host !== `localhost:${port}`) {
      return failure(403, 'forbidden', 'unexpected Host header', { close: true })
    }
    if (!hasToken(req.headers.authorization, token)) {
      return failure(401, 'unauthorized', 'missing or invalid bearer token', { close: true })
    }
    const path = (req.url ?? '').split('?')[0] ?? ''
    const allowed = Object.hasOwn(ALLOW, path) ? ALLOW[path] : undefined
    if (allowed === undefined) return failure(404, 'not_found', `unknown path ${path}`)
    if (req.method !== allowed) {
      return failure(405, 'method_not_allowed', `${path} takes ${allowed}`, { headers: { Allow: allowed } })
    }
    if (path === '/v1/health') return { status: 200, body: { ok: true, v: 1, version: options.version, pid: process.pid } }

    // POST /v1/hud: a declared length only, so a body is never read past the cap.
    if (req.headers['transfer-encoding'] !== undefined) {
      return failure(411, 'length_required', 'send the snapshot with a Content-Length', { close: true })
    }
    const length = req.headers['content-length']
    if (length === undefined) return failure(411, 'length_required', 'send the snapshot with a Content-Length', { close: true })
    if (!/^\d+$/.test(length)) return failure(400, 'bad_request', 'invalid Content-Length', { close: true })
    if (Number(length) > MAX_BODY_BYTES) {
      return failure(413, 'too_large', `the snapshot must be at most ${MAX_BODY_BYTES} bytes`, { close: true })
    }
    let value: unknown
    try {
      value = JSON.parse(UTF8.decode(await readBody(req)))
    } catch {
      return failure(400, 'bad_json', 'the body must be UTF-8 JSON')
    }
    const parsed = parseSnapshot(value)
    if (!parsed.ok) return failure(400, 'bad_snapshot', parsed.message)
    try {
      options.onSnapshot(parsed.snapshot)
    } catch {
      return failure(500, 'internal', 'the app could not show the snapshot')
    }
    return { status: 200, body: { ok: true } }
  }

  const server = createServer((req, res) => {
    answer(req).then(
      result => send(res, result),
      () => send(res, failure(500, 'internal', 'the app could not answer')),
    )
  })
  server.headersTimeout = 3000
  server.requestTimeout = 5000
  server.keepAliveTimeout = 5000

  await new Promise<void>((resolve, reject) => {
    server.once('error', reject)
    server.listen(0, HOST, () => {
      server.off('error', reject)
      resolve()
    })
  })
  port = (server.address() as AddressInfo).port

  return {
    host: HOST,
    port,
    close: () =>
      new Promise<void>(resolve => {
        server.close(() => resolve())
        server.closeIdleConnections()
      }),
  }
}
