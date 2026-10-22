// Where the Jarvis home is, by the same rule as the plugin (companion.ts), so
// both find one address file: JARVIS_HOME when it is an absolute local path,
// else .jarvis in the user's home, the folder /jarvis setup uses.

import { posix, win32 } from 'node:path'

const DRIVE_PATH = /^[A-Za-z]:[\\/]/

/**
 * True for an absolute path on a local disk. On Windows that is a drive path:
 * UNC paths (\\server\share) are refused because Claude Code's file access
 * never reads network locations, and \foo has no drive.
 */
export function isAbsoluteLocal(value: string, platform: NodeJS.Platform): boolean {
  return platform === 'win32' ? DRIVE_PATH.test(value) : value.startsWith('/')
}

const pathFor = (platform: NodeJS.Platform) => (platform === 'win32' ? win32 : posix)

export function jarvisHome(env: Record<string, string | undefined>, platform: NodeJS.Platform, homedir: string): string {
  const override = env.JARVIS_HOME?.trim() ?? ''
  if (isAbsoluteLocal(override, platform)) return override
  return pathFor(platform).join(homedir, '.jarvis')
}

/** The app's address file, which the plugin reads to find it. */
export function endpointPath(home: string, platform: NodeJS.Platform): string {
  return pathFor(platform).join(home, 'app', 'endpoint.json')
}

/** A userData folder for tests (JARVIS_USER_DATA), so each copy gets its own settings and lock. */
export function userDataOverride(env: Record<string, string | undefined>, platform: NodeJS.Platform): string | undefined {
  const value = env.JARVIS_USER_DATA?.trim() ?? ''
  return isAbsoluteLocal(value, platform) ? value : undefined
}
