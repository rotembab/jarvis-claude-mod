import assert from 'node:assert/strict'
import { mkdtempSync, readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'

import { type Endpoint, newToken, removeEndpointSync, renameWithRetry, writeEndpoint } from '../src/main/discovery'

const ENDPOINT: Endpoint = { v: 1, port: 50999, token: 'f'.repeat(64), pid: 1234, version: '0.8.0' }

const tempHome = (): string => join(mkdtempSync(join(tmpdir(), 'jarvis-discovery-')), 'home')

test('writes the address as one JSON line, creating its folder', async () => {
  const home = tempHome()
  const path = await writeEndpoint(home, ENDPOINT)
  assert.equal(path, join(home, 'app', 'endpoint.json'))
  assert.equal(readFileSync(path, 'utf8'), '{"v":1,"port":50999,"token":"ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff","pid":1234,"version":"0.8.0"}\n')
  if (process.platform !== 'win32') {
    assert.equal(statSync(path).mode & 0o777, 0o600)
    assert.equal(statSync(join(home, 'app')).mode & 0o777, 0o700)
  }
})

test('replaces an existing file and leaves no temporary file behind', async () => {
  const home = tempHome()
  await writeEndpoint(home, ENDPOINT)
  const path = await writeEndpoint(home, { ...ENDPOINT, port: 51000 })
  assert.equal(JSON.parse(readFileSync(path, 'utf8')).port, 51000)
  assert.deepEqual(readdirSync(join(home, 'app')), ['endpoint.json'])
})

test('removes the file on quit only while it holds our token', async () => {
  const home = tempHome()
  const path = await writeEndpoint(home, ENDPOINT)
  writeFileSync(path, JSON.stringify({ ...ENDPOINT, token: 'e'.repeat(64) }))
  assert.equal(removeEndpointSync(home, ENDPOINT.token), false)
  assert.ok(statSync(path).isFile())
  await writeEndpoint(home, ENDPOINT)
  assert.equal(removeEndpointSync(home, ENDPOINT.token), true)
  assert.deepEqual(readdirSync(join(home, 'app')), [])
  assert.equal(removeEndpointSync(home, ENDPOINT.token), false)
  assert.equal(removeEndpointSync(join(home, 'missing'), ENDPOINT.token), false)
})

/** A rename that fails with each code in turn, then succeeds; counts its calls. */
function flakyRename(codes: string[]) {
  const fake = async (_from: string, _to: string): Promise<void> => {
    fake.calls += 1
    const code = codes.shift()
    if (code !== undefined) throw Object.assign(new Error(code), { code })
  }
  fake.calls = 0
  return fake
}

test('a rename Windows reports busy is retried a few times', async () => {
  const twice = flakyRename(['EPERM', 'EPERM'])
  await renameWithRetry('a', 'b', twice, 5, 1)
  assert.equal(twice.calls, 3)

  const always = flakyRename(['EPERM', 'EBUSY', 'EACCES', 'EPERM', 'EPERM', 'EPERM'])
  await assert.rejects(renameWithRetry('a', 'b', always, 5, 1), /EPERM/)
  assert.equal(always.calls, 5)

  const missing = flakyRename(['ENOENT'])
  await assert.rejects(renameWithRetry('a', 'b', missing, 5, 1), /ENOENT/)
  assert.equal(missing.calls, 1)
})

test('each token is 32 fresh random bytes in hex', () => {
  const token = newToken()
  assert.match(token, /^[0-9a-f]{64}$/)
  assert.notEqual(newToken(), token)
})
