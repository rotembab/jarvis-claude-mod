// Every OS-specific choice the mod makes lives here: paths, the helper's
// python, where uv is, how to install it, and the persona's OS paragraph.
// Windows is the phase 1 target; macOS and Linux get working defaults.

import type { Engine, EnvSnapshot } from './engine'

export type OsKind = 'windows' | 'macos' | 'linux'

export type Platform = {
  os: OsKind
  /** Path separator for building paths on this OS. */
  sep: '\\' | '/'
  /** The user's home folder (USERPROFILE on Windows, HOME elsewhere). */
  home: string
  /** Where the helper's venv, models, logs and caches live. */
  dataDir: string
  /** The venv the helper runs from. */
  venvDir: string
  /** Absolute path of the venv's python; never a bare `python`. */
  venvPython: string
  /** The local voice's own venv (torch and Chatterbox), kept apart from the helper's. */
  localVenvDir: string
  /** Absolute path of the local voice venv's python. */
  localVenvPython: string
  /** Fixed places uv is looked for, in order, before the PATH. */
  uvCandidates: string[]
  /** argv that prints uv's path(s) when it is on the PATH. */
  uvWhichArgv: string[]
  /** One line the user runs to install uv. */
  uvInstallHint: string
  /** A file whose presence means an NVIDIA driver is installed (CUDA possible). */
  nvidiaMarker: string | undefined
  /** The persona's paragraph about this machine's shell and paths. */
  personaOsParagraph: string
}

/** Joins path parts with the platform's separator, collapsing doubled separators. */
export function joinPath(sep: '\\' | '/', ...parts: string[]): string {
  const joined = parts.filter(part => part !== '').join(sep)
  return sep === '\\' ? joined.replace(/[\\/]+/g, '\\') : joined.replace(/\/+/g, '/')
}

const WINDOWS_PERSONA = [
  'This machine runs Windows 11. Run shell commands with the PowerShell tool (Git Bash is not installed) and write Windows paths such as C:\\Users\\name\\file.txt.',
  'Windows PowerShell 5.1 has no && or || operators: chain commands with ";" or `if ($?) { ... }`.',
].join(' ')

const MACOS_PERSONA =
  'This machine runs macOS: shell commands run in zsh, and paths look like /Users/name/file.txt.'

const LINUX_PERSONA = 'This machine runs Linux: shell commands run in bash, and paths look like /home/name/file.txt.'

type PlatformInputs = {
  os: OsKind
  home: string
  localAppData?: string
}

/** Builds the platform description from already-read facts (pure, tested). */
export function describePlatform({ os, home, localAppData }: PlatformInputs): Platform {
  if (os === 'windows') {
    const sep = '\\'
    const dataDir = joinPath(sep, home, '.jarvis')
    const venvDir = joinPath(sep, dataDir, 'venv')
    const localVenvDir = joinPath(sep, dataDir, 'local-voice', 'venv')
    const appData = localAppData ?? joinPath(sep, home, 'AppData', 'Local')
    return {
      os,
      sep,
      home,
      dataDir,
      venvDir,
      venvPython: joinPath(sep, venvDir, 'Scripts', 'python.exe'),
      localVenvDir,
      localVenvPython: joinPath(sep, localVenvDir, 'Scripts', 'python.exe'),
      uvCandidates: [
        joinPath(sep, home, '.local', 'bin', 'uv.exe'),
        joinPath(sep, appData, 'Microsoft', 'WinGet', 'Links', 'uv.exe'),
      ],
      uvWhichArgv: ['where', 'uv'],
      uvInstallHint: 'winget install astral-sh.uv',
      nvidiaMarker: 'C:\\Windows\\System32\\nvcuda.dll',
      personaOsParagraph: WINDOWS_PERSONA,
    }
  }
  const sep = '/'
  const dataDir = joinPath(sep, home, '.jarvis')
  const venvDir = joinPath(sep, dataDir, 'venv')
  const localVenvDir = joinPath(sep, dataDir, 'local-voice', 'venv')
  return {
    os,
    sep,
    home,
    dataDir,
    venvDir,
    venvPython: joinPath(sep, venvDir, 'bin', 'python'),
    localVenvDir,
    localVenvPython: joinPath(sep, localVenvDir, 'bin', 'python'),
    uvCandidates: [
      joinPath(sep, home, '.local', 'bin', 'uv'),
      joinPath(sep, home, '.cargo', 'bin', 'uv'),
      ...(os === 'macos' ? ['/opt/homebrew/bin/uv'] : []),
      '/usr/local/bin/uv',
    ],
    uvWhichArgv: ['which', 'uv'],
    uvInstallHint: os === 'macos' ? 'brew install uv' : 'curl -LsSf https://astral.sh/uv/install.sh | sh',
    nvidiaMarker: os === 'linux' ? '/proc/driver/nvidia/version' : undefined,
    personaOsParagraph: os === 'macos' ? MACOS_PERSONA : LINUX_PERSONA,
  }
}

/**
 * Detects the OS and reads the paths the mod needs. Windows is recognised by
 * `OS=Windows_NT` (set on every Windows process) and reads USERPROFILE, never
 * HOME; elsewhere `uname -s` tells macOS from Linux.
 */
export async function detectPlatform(engine: Engine, env: EnvSnapshot): Promise<Platform> {
  if (env.OS === 'Windows_NT') {
    return describePlatform({
      os: 'windows',
      home: env.USERPROFILE ?? 'C:\\Users\\Default',
      localAppData: env.LOCALAPPDATA,
    })
  }
  const home = env.HOME ?? '/tmp'
  let os: OsKind = home.startsWith('/Users/') ? 'macos' : 'linux'
  try {
    const { exitCode, stdout } = await engine.run(['uname', '-s'], { timeoutMs: 5000 })
    if (exitCode === 0) os = stdout.trim() === 'Darwin' ? 'macos' : 'linux'
  } catch {
    // keep the HOME-based guess
  }
  return describePlatform({ os, home })
}

/** True when this session runs in a cloud container rather than on the user's machine. */
export function isRemoteSession(env: EnvSnapshot): boolean {
  return env.CLAUDE_CODE_REMOTE === 'true'
}

/**
 * Finds uv in the spec's order: the fixed candidates, then the PATH. Returns
 * the absolute path, or undefined when uv is not installed.
 */
export async function findUv(engine: Engine, platform: Platform): Promise<string | undefined> {
  for (const candidate of platform.uvCandidates) {
    if (await engine.exists(candidate).catch(() => false)) return candidate
  }
  try {
    const { exitCode, stdout } = await engine.run(platform.uvWhichArgv, { timeoutMs: 5000 })
    if (exitCode !== 0) return undefined
    const lines = stdout.split(/\r?\n/).map(line => line.trim()).filter(line => line !== '')
    // `where` may list a uv without an extension first (a shim); prefer the .exe.
    return platform.os === 'windows' ? (lines.find(line => /\.exe$/i.test(line)) ?? lines[0]) : lines[0]
  } catch {
    return undefined
  }
}

/**
 * A command line to paste into the user's shell, the program path quoted.
 * PowerShell (the Windows shell here) runs a quoted path only after its call
 * operator `&`; POSIX shells take it as is.
 */
export function shellCommandLine(platform: Platform, program: string, args: readonly string[]): string {
  const line = [`"${program}"`, ...args].join(' ')
  return platform.os === 'windows' ? `& ${line}` : line
}

/** Appends loopback hosts to a NO_PROXY value so local traffic never goes through a proxy. */
export function withLoopbackNoProxy(current: string | undefined): string {
  const entries = (current ?? '').split(',').map(entry => entry.trim()).filter(entry => entry !== '')
  for (const host of ['127.0.0.1', 'localhost']) {
    if (!entries.includes(host)) entries.push(host)
  }
  return entries.join(',')
}

/** True on Windows: `OS=Windows_NT` is set on every Windows process. */
export function isWindowsEnv(env: EnvSnapshot): boolean {
  return env.OS === 'Windows_NT'
}

// ---- Administrator rights and UAC (docs/PC-CONTROL.md) ----

/** Windows' UAC level, as its policy values set it; `standard-account` when the user is no administrator. */
export type UacLevel = 'off' | 'never-notify' | 'default' | 'default-no-dim' | 'always-notify' | 'admin-protection' | 'standard-account'

/** What the administrator check found. */
export type AdminFacts = {
  /** Claude Code runs with an administrator's (or root's) full rights, and so does every command it starts. */
  isElevated: boolean
  /** Windows: the user is in the Administrators group, so an admin step needs only a click on the UAC prompt. */
  isAdminAccount?: boolean
  /** Windows: the UAC level; undefined when the policy could not be read. */
  uacLevel?: UacLevel
}

/** The mandatory labels of an elevated (High) and a SYSTEM token. */
const ELEVATED_SIDS = ['S-1-16-12288', 'S-1-16-16384']
/** BUILTIN\Administrators: in an administrator's token whether elevated or not (then "for deny only"). */
const ADMINISTRATORS_SID = 'S-1-5-32-544'
const UAC_POLICY_KEY = 'HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System'
const ADMIN_PROBE_TIMEOUT_MS = 5000

/** The SIDs `whoami /groups /fo csv /nh` lists. Group names are translated; SIDs are not. */
export function parseWhoamiGroups(csv: string): Set<string> {
  return new Set(csv.match(/S-1-[0-9-]*[0-9]/g) ?? [])
}

/**
 * The UAC level from `reg query` of the UAC policy key, read from its
 * REG_DWORD values (never translated); undefined when they are not there.
 */
export function parseUacPolicy(text: string): Exclude<UacLevel, 'standard-account'> | undefined {
  const values = new Map<string, number>()
  for (const match of text.matchAll(/^\s*(\w+)\s+REG_DWORD\s+0x([0-9a-f]+)\s*$/gim)) {
    values.set((match[1] ?? '').toLowerCase(), Number.parseInt(match[2] ?? '', 16))
  }
  if (values.get('enablelua') === 0) return 'off'
  if (values.get('typeofadminapprovalmode') === 2) return 'admin-protection'
  const consent = values.get('consentpromptbehavioradmin')
  if (consent === undefined) return undefined
  if (consent === 0) return 'never-notify'
  if (consent === 5) return values.get('promptonsecuredesktop') === 0 ? 'default-no-dim' : 'default'
  return 'always-notify' // 1 to 4: every elevation asks
}

/**
 * Windows's own System32 folder: the programs there are run by full path, so
 * another `whoami` earlier on PATH (Git's, from Git Bash) cannot answer.
 */
export function system32(systemRoot: string | undefined): string {
  const root = systemRoot !== undefined && /^[A-Za-z]:\\[^"<>|?*]*$/.test(systemRoot) ? systemRoot.replace(/\\+$/, '') : 'C:\\Windows'
  return `${root}\\System32`
}

/**
 * Whether Claude Code runs elevated and, on Windows, the UAC level: the
 * token's groups from `whoami`, the policy from `reg query`. macOS and
 * Linux check for root. Throws when it cannot tell.
 */
export async function probeAdmin(engine: Engine, platform: Platform): Promise<AdminFacts> {
  const init = { timeoutMs: ADMIN_PROBE_TIMEOUT_MS }
  if (platform.os !== 'windows') {
    const { exitCode, stdout } = await engine.run(['id', '-u'], init)
    if (exitCode !== 0 || !/^\d+$/.test(stdout.trim())) throw new Error(`id -u exited with ${exitCode}`)
    return { isElevated: stdout.trim() === '0' }
  }
  const folder = system32((await engine.env()).SystemRoot)
  const [groups, policy] = await Promise.all([
    engine.run([`${folder}\\whoami.exe`, '/groups', '/fo', 'csv', '/nh'], init),
    engine.run([`${folder}\\reg.exe`, 'query', UAC_POLICY_KEY], init).catch(() => undefined),
  ])
  const sids = parseWhoamiGroups(groups.stdout)
  if (groups.exitCode !== 0 || sids.size === 0) throw new Error(`whoami /groups exited with ${groups.exitCode}`)
  const isAdminAccount = sids.has(ADMINISTRATORS_SID)
  const uacLevel = !isAdminAccount ? 'standard-account' : policy?.exitCode === 0 ? parseUacPolicy(policy.stdout) : undefined
  return { isElevated: ELEVATED_SIDS.some(sid => sids.has(sid)), isAdminAccount, ...(uacLevel === undefined ? {} : { uacLevel }) }
}
