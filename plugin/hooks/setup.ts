// /jarvis setup: installs the helper into <dataDir>/venv with uv (Python
// 3.12, non-editable, from the plugin's own voice/ project) and lets the
// helper download its speech-to-text model. Never downloads installers:
// commands.ts finds uv first and, if it is missing, prints the install line.

import type { Engine } from './engine'
import { describeError } from './engine'
import { LineReader } from './helper'
import type { Platform } from './platform'
import { joinPath } from './platform'

export type SetupRequest = {
  /** Passed to the helper's setup as --stt-model; undefined lets it choose ("auto"). */
  sttModel?: string
  /** Install the `cuda` extra (cuBLAS, plus cuDNN on Windows, for faster-whisper on an NVIDIA GPU). */
  useCuda: boolean
  onProgress: (text: string) => void
  debug: (line: string) => void
}

export type SetupResult =
  | { ok: true; summary: string }
  | { ok: false; reason: 'uv_failed' | 'model_failed'; message: string }

type ProgressLine = { type?: unknown; step?: unknown; pct?: unknown; message?: unknown }

/** The helper's distribution name in voice/pyproject.toml. */
export const HELPER_PACKAGE = 'jarvis-voice'

/** The `uv sync` argv for the helper project (exported for tests). */
export function uvSyncArgv(uv: string, project: string, { useCuda, isFrozen }: { useCuda: boolean; isFrozen: boolean }): string[] {
  return [
    uv,
    'sync',
    '--project',
    project,
    '--python',
    '3.12',
    '--no-dev',
    '--no-editable',
    // A non-editable install is not rebuilt while its version stays the same,
    // so changed helper code would never arrive; rebuilding the one small
    // local wheel is cheap (its dependencies stay as they are).
    '--reinstall-package',
    HELPER_PACKAGE,
    ...(isFrozen ? ['--frozen'] : []),
    ...(useCuda ? ['--extra', 'cuda'] : []),
  ]
}

export type Progress = { step: string | undefined; text: string }

/** Reads one JSON progress line of `jarvis_voice setup`; undefined for other output. */
export function parseProgress(line: string): Progress | undefined {
  let value: ProgressLine
  try {
    value = JSON.parse(line) as ProgressLine
  } catch {
    return undefined
  }
  if (typeof value !== 'object' || value === null || value.type !== 'progress') return undefined
  const step = typeof value.step === 'string' ? value.step : undefined
  const message = typeof value.message === 'string' ? value.message : step
  if (message === undefined) return undefined
  // "done" and "error" say it all in words; the percentage is for the steps between.
  const pct = typeof value.pct === 'number' && Number.isFinite(value.pct) ? value.pct : undefined
  const isEnd = step === 'done' || step === 'error'
  return { step, text: pct === undefined || isEnd ? message : `${message} ${Math.round(pct)}%` }
}

/** True when the machine has an NVIDIA driver, so the CUDA extra is worth installing. */
export async function hasNvidiaGpu(engine: Engine, platform: Platform): Promise<boolean> {
  if (platform.nvidiaMarker === undefined) return false
  return engine.exists(platform.nvidiaMarker).catch(() => false)
}

/** Runs a child to its end, handing each output line to `onLine`; resolves its exit code. */
async function runStreaming(
  engine: Engine,
  request: { argv: string[]; cwd: string; env: Record<string, string> },
  onLine: (stream: 'stdout' | 'stderr', line: string) => void,
): Promise<number | null> {
  const readers = { stdout: new LineReader(), stderr: new LineReader() }
  const child = engine.spawn(request)
  for await (const chunk of child) {
    for (const line of readers[chunk.stream].push(chunk.text)) onLine(chunk.stream, line)
  }
  for (const stream of ['stdout', 'stderr'] as const) {
    for (const line of readers[stream].flush()) onLine(stream, line)
  }
  return (await child.result).code
}

/** What /jarvis setup prints when uv is not installed (it never installs uv itself). */
export function uvMissingMessage(platform: Platform): string {
  return `uv is not installed. Install it with:\n  ${platform.uvInstallHint}\nthen open a new terminal and run /jarvis setup again.`
}

/** Installs the helper with the uv at `uv` (find it first with findUv). */
export async function runSetup(
  engine: Engine,
  platform: Platform,
  uv: string,
  request: SetupRequest,
): Promise<SetupResult> {
  const { onProgress, debug } = request

  // Creates the data folder (fs.write makes parents) and says what it is.
  await engine.writeFile(
    joinPath(platform.sep, platform.dataDir, 'README.txt'),
    'Jarvis for Claude Code keeps its voice helper here: venv/ (Python environment), models/, logs/ and uv-cache/.\nDelete this folder to uninstall; /jarvis setup recreates it.\n',
  )

  const project = joinPath(platform.sep, engine.pluginRoot, 'voice')
  const isFrozen = await engine.exists(joinPath(platform.sep, project, 'uv.lock')).catch(() => false)
  const env = {
    UV_PROJECT_ENVIRONMENT: platform.venvDir,
    UV_CACHE_DIR: joinPath(platform.sep, platform.dataDir, 'uv-cache'),
    UV_NO_PROGRESS: '1',
  }
  const tail: string[] = []
  const keep = (line: string): void => {
    tail.push(line.slice(0, 300))
    if (tail.length > 8) tail.shift()
  }

  onProgress(request.useCuda ? 'installing the helper (Python 3.12, with CUDA)' : 'installing the helper (Python 3.12)')
  let code: number | null
  try {
    code = await runStreaming(
      engine,
      { argv: uvSyncArgv(uv, project, { useCuda: request.useCuda, isFrozen }), cwd: project, env },
      (_stream, line) => {
        // uv reports on stderr: "Resolved 41 packages", "Installed 38 packages".
        keep(line)
        debug(`uv: ${line}`)
        onProgress(`installing · ${line.trim().slice(0, 80)}`)
      },
    )
  } catch (error) {
    return { ok: false, reason: 'uv_failed', message: `uv could not run: ${describeError(error)}` }
  }
  if (code !== 0) {
    return { ok: false, reason: 'uv_failed', message: `uv sync failed (exit ${code ?? 'signal'}):\n${tail.join('\n')}` }
  }

  onProgress('downloading the speech model')
  tail.length = 0
  let lastProgress = ''
  let failure: string | undefined
  try {
    code = await runStreaming(
      engine,
      {
        argv: [
          platform.venvPython,
          '-m',
          'jarvis_voice',
          'setup',
          '--data-dir',
          platform.dataDir,
          ...(request.sttModel === undefined ? [] : ['--stt-model', request.sttModel]),
        ],
        cwd: platform.dataDir,
        env: { PYTHONUNBUFFERED: '1', PYTHONUTF8: '1' },
      },
      (stream, line) => {
        const progress = stream === 'stdout' ? parseProgress(line) : undefined
        if (progress !== undefined) {
          lastProgress = progress.text
          if (progress.step === 'error') failure = progress.text
          onProgress(progress.text)
        } else {
          keep(line)
          debug(`helper setup: ${line}`)
        }
      },
    )
  } catch (error) {
    return { ok: false, reason: 'model_failed', message: `the helper could not run: ${describeError(error)}` }
  }
  if (code !== 0) {
    const details = [failure, ...tail].filter(line => line !== undefined).join('\n')
    return { ok: false, reason: 'model_failed', message: `the helper's setup failed (exit ${code ?? 'signal'}):\n${details}` }
  }
  return { ok: true, summary: lastProgress === '' ? 'installed' : lastProgress }
}
