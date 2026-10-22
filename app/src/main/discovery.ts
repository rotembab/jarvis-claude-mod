// The address file: how the plugin finds the app. Written once per launch,
// after the link server listens, and removed on quit. It holds the port and a
// fresh token that lasts one launch; nothing here logs the token.

import { randomBytes } from 'node:crypto'
import { promises as fs, readFileSync, unlinkSync } from 'node:fs'
import { dirname } from 'node:path'

import { endpointPath } from './paths'

export type Endpoint = { v: 1; port: number; token: string; pid: number; version: string }

/** 32 random bytes as lowercase hex: the bearer token for this launch. */
export function newToken(): string {
  return randomBytes(32).toString('hex')
}

/** Errors Windows raises while another process has the target file open. */
const BUSY = new Set(['EPERM', 'EBUSY', 'EACCES'])

const wait = (ms: number): Promise<void> => new Promise(resolve => setTimeout(resolve, ms))

/** Renames `from` over `to`, retrying a few times while Windows reports the file busy. */
export async function renameWithRetry(
  from: string,
  to: string,
  rename: (from: string, to: string) => Promise<void> = fs.rename,
  attempts = 5,
  waitMs = 50,
): Promise<void> {
  for (let attempt = 1; ; attempt += 1) {
    try {
      await rename(from, to)
      return
    } catch (error) {
      const code = (error as NodeJS.ErrnoException).code
      if (attempt >= attempts || code === undefined || !BUSY.has(code)) throw error
      await wait(waitMs)
    }
  }
}

/** Writes `text` beside `path` and renames it into place, so a reader never sees half a file. */
export async function writeFileAtomic(path: string, text: string, mode?: number): Promise<void> {
  const tmp = `${path}.${process.pid}.tmp`
  try {
    await fs.writeFile(tmp, text, { encoding: 'utf8', mode })
    await renameWithRetry(tmp, path)
  } catch (error) {
    await fs.rm(tmp, { force: true }).catch(() => undefined)
    throw error
  }
}

/** Writes the address file under `home` (only this user may read it) and returns its path. */
export async function writeEndpoint(home: string, endpoint: Endpoint, platform: NodeJS.Platform = process.platform): Promise<string> {
  const path = endpointPath(home, platform)
  await fs.mkdir(dirname(path), { recursive: true, mode: 0o700 })
  await writeFileAtomic(path, JSON.stringify(endpoint) + '\n', 0o600)
  return path
}

/**
 * Removes the address file if it is still ours (another copy may have taken it
 * over). Synchronous, for before-quit; never throws. True when it removed it.
 */
export function removeEndpointSync(home: string, token: string, platform: NodeJS.Platform = process.platform): boolean {
  const path = endpointPath(home, platform)
  try {
    const written = JSON.parse(readFileSync(path, 'utf8')) as { token?: unknown }
    if (written.token !== token) return false
    unlinkSync(path)
    return true
  } catch {
    return false
  }
}
