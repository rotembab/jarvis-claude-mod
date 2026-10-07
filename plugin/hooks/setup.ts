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

/** The local voice's distribution name in local-voice/pyproject.toml. */
export const LOCAL_VOICE_PACKAGE = 'jarvis-local-voice'

/** The `uv pip install` argv for the local voice (exported for tests). */
export function localVoiceInstallArgv(
  uv: string,
  platform: Platform,
  project: string,
  { useCuda }: { useCuda: boolean },
): string[] {
  // PyPI's Windows and Linux torch has no CUDA; --torch-backend takes torch from
  // PyTorch's own index instead. cu126 has the torch 2.6.0 Chatterbox pins.
  const backend = useCuda ? ['--torch-backend', 'cu126'] : platform.os === 'macos' ? [] : ['--torch-backend', 'cpu']
  return [
    uv,
    'pip',
    'install',
    '--python',
    platform.localVenvPython,
    ...backend,
    '--reinstall-package',
    LOCAL_VOICE_PACKAGE,
    project,
  ]
}

export type LocalSetupRequest = {
  useCuda: boolean
  /** The reference clip, so the setup's test sentence uses it. */
  voiceClip?: string
  onProgress: (text: string) => void
  debug: (line: string) => void
}

/**
 * /jarvis setup local: a separate Python 3.12 venv in <dataDir>/local-voice/venv
 * with torch and Chatterbox-Turbo (`uv pip install`, so no lock file is ever
 * written into the plugin folder), then the model download and one test
 * sentence. About 3 GB of packages and 3 GB of model.
 */
export async function runLocalVoiceSetup(
  engine: Engine,
  platform: Platform,
  uv: string,
  request: LocalSetupRequest,
): Promise<SetupResult> {
  const { onProgress, debug } = request
  const env = {
    UV_CACHE_DIR: joinPath(platform.sep, platform.dataDir, 'uv-cache'),
    UV_NO_PROGRESS: '1',
    HF_HUB_DISABLE_TELEMETRY: '1',
  }
  const tail: string[] = []
  const keep = (line: string): void => {
    tail.push(line.slice(0, 300))
    if (tail.length > 8) tail.shift()
  }
  const uvStep = async (argv: string[], label: string): Promise<SetupResult | undefined> => {
    onProgress(label)
    let code: number | null
    try {
      code = await runStreaming(engine, { argv, cwd: platform.dataDir, env }, (_stream, line) => {
        keep(line)
        debug(`uv: ${line}`)
        onProgress(`${label} · ${line.trim().slice(0, 80)}`)
      })
    } catch (error) {
      return { ok: false, reason: 'uv_failed', message: `uv could not run: ${describeError(error)}` }
    }
    if (code === 0) return undefined
    return { ok: false, reason: 'uv_failed', message: `${argv.slice(0, 3).join(' ')} failed (exit ${code ?? 'signal'}):\n${tail.join('\n')}` }
  }

  const project = joinPath(platform.sep, engine.pluginRoot, 'local-voice')
  const venvFailed = await uvStep(
    [uv, 'venv', platform.localVenvDir, '--python', '3.12', '--allow-existing'],
    'creating the local voice environment',
  )
  if (venvFailed !== undefined) return venvFailed
  const installFailed = await uvStep(
    localVoiceInstallArgv(uv, platform, project, { useCuda: request.useCuda }),
    request.useCuda ? 'installing the local voice (PyTorch with CUDA, about 3 GB)' : 'installing the local voice',
  )
  if (installFailed !== undefined) return installFailed

  onProgress('downloading the local voice model (about 3 GB)')
  tail.length = 0
  let lastProgress = ''
  let failure: string | undefined
  let code: number | null
  try {
    code = await runStreaming(
      engine,
      {
        argv: [
          platform.localVenvPython,
          '-m',
          'jarvis_local_voice',
          'download',
          '--models-dir',
          joinPath(platform.sep, platform.dataDir, 'models'),
          '--verify',
          ...(request.voiceClip === undefined ? [] : ['--voice', request.voiceClip]),
        ],
        cwd: platform.dataDir,
        env: { ...env, PYTHONUNBUFFERED: '1', PYTHONUTF8: '1' },
      },
      (stream, line) => {
        const progress = stream === 'stdout' ? parseProgress(line) : undefined
        if (progress !== undefined) {
          lastProgress = progress.text
          if (progress.step === 'error') failure = progress.text
          onProgress(progress.text)
        } else {
          keep(line)
          debug(`local voice setup: ${line}`)
        }
      },
    )
  } catch (error) {
    return { ok: false, reason: 'model_failed', message: `the local voice could not run: ${describeError(error)}` }
  }
  if (code !== 0) {
    const details = [failure, ...tail].filter(line => line !== undefined).join('\n')
    return { ok: false, reason: 'model_failed', message: `the local voice setup failed (exit ${code ?? 'signal'}):\n${details}` }
  }
  return { ok: true, summary: lastProgress === '' ? 'local voice installed' : lastProgress }
}
