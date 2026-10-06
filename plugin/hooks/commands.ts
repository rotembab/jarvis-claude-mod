// /jarvis and its subcommands. The command is registered `immediate` so
// `/jarvis stop` works while a reply is streaming; work that takes longer
// than a moment (setup, restart) runs on after the command has answered.

import type { CommandRunResult } from 'claude-code'

import type { Jarvis, SttModel } from './app'
import { STT_MODELS } from './app'
import { findUv, shellCommandLine } from './platform'
import type { StatusResponse } from './protocol'
import { uvMissingMessage } from './setup'
import { statusLine } from './ui'

const HELP = [
  '/jarvis setup [model] [cpu]   install or repair the voice helper and its speech model',
  '/jarvis stop                  stop speaking and cancel the spoken reply',
  '/jarvis talk                  start or stop listening without the push-to-talk key',
  '/jarvis test                  speak a test line',
  '/jarvis restart               restart the voice helper',
  '/jarvis voice <id|default>    use a Fish Audio voice (its model id)',
  '/jarvis devices               show the audio devices and models in use',
].join('\n')

const NOT_LOCAL = 'Jarvis runs on your own computer; this session runs in the cloud, so the voice helper is not started here.'

const VOICE_ID = /^[A-Za-z0-9_-]{1,128}$/

/** Runs `/jarvis <args>`; never throws (a failure is the command's output). */
export async function runJarvisCommand(app: Jarvis, args: string): Promise<CommandRunResult> {
  const [sub = '', ...rest] = args.trim().split(/\s+/).filter(word => word !== '')
  try {
    switch (sub.toLowerCase()) {
      case '':
      case 'status':
      case 'help':
        return { text: await status(app) }
      case 'setup':
        return { text: await setup(app, rest) }
      case 'stop':
        return { text: await stop(app) }
      case 'talk':
        return { text: await talk(app) }
      case 'test':
        return { text: await testVoice(app) }
      case 'restart':
        return { text: restart(app) }
      case 'voice':
        return { text: await voice(app, rest[0]) }
      case 'devices':
        return { text: await devices(app) }
      default:
        return { text: `Unknown subcommand "${sub}".\n\n${HELP}` }
    }
  } catch (error) {
    return { text: `/jarvis ${sub} failed: ${error instanceof Error ? error.message : String(error)}` }
  }
}

async function status(app: Jarvis): Promise<string> {
  if (!app.isLocal) return `${NOT_LOCAL}\n\n${HELP}`
  // Bare /jarvis is also the user's "try again" after another window let go
  // of the helper, or after the restart policy gave up.
  const phase = app.view.phase
  const isRetry = phase === 'elsewhere' || phase === 'failed' || phase === 'stopped'
  if (isRetry) app.startHelper(true)

  const lines = [statusLine(app.view) ?? 'JARVIS']
  const hello = app.helper?.hello
  if (hello !== undefined) {
    const ready = app.ready
    const model = ready ? `speech model ${ready.sttModel} on ${ready.sttDevice}` : 'loading models'
    const voiceId = (await app.voiceId()) ?? 'Fish Audio default'
    lines.push(`Helper ${hello.version} (pid ${hello.pid}) · ${model} · voice ${voiceId}`)
  } else if (isRetry) {
    lines.push('Starting the voice helper…')
  }
  if (app.platform !== undefined) lines.push(`Data folder: ${app.platform.dataDir}`)
  return `${lines.join('\n')}\n\n${HELP}`
}

async function setup(app: Jarvis, args: string[]): Promise<string> {
  const { engine, platform } = app
  if (!app.isLocal || engine === undefined || platform === undefined) return NOT_LOCAL
  if (app.isSetupRunning) return 'Setup is already running; its progress is in the status line.'
  let sttModel: SttModel | undefined
  let useCuda: boolean | undefined
  for (const arg of args) {
    const word = arg.toLowerCase()
    const model = STT_MODELS.find(one => one === word)
    if (word === 'cpu') useCuda = false
    else if (word === 'cuda' || word === 'gpu') useCuda = true
    else if (model !== undefined) sttModel = model
    else return `Unknown setup option "${arg}". Models: ${STT_MODELS.join(', ')}; add "cpu" to skip the CUDA libraries.`
  }

  const uv = await findUv(engine, platform)
  if (uv === undefined) return uvMissingMessage(platform)
  // Without a model named here, setup repairs the one in use.
  const model = await app.setupModel({ sttModel, useCuda })
  void app.runSetup(uv, { sttModel, useCuda })
  return [
    `Setting up Jarvis with ${uv}:`,
    `  1. a Python 3.12 environment in ${platform.venvDir} with the voice helper`,
    `  2. the speech model (${model === 'auto' ? 'chosen for your hardware' : model})`,
    'Progress shows in the status line; the helper starts when it is done.',
  ].join('\n')
}

async function stop(app: Jarvis): Promise<string> {
  const voice = app.voice
  if (voice === undefined) return 'Jarvis is not running in this session.'
  const { abortedTurn, speech } = await voice.interrupt('user')
  if (abortedTurn) return 'Stopped speaking and cancelled the reply.'
  switch (speech) {
    case 'stopped':
      return 'Stopped speaking.'
    case 'silent':
      return 'Nothing was playing.'
    case 'not_running':
      return 'Nothing to stop: the voice helper is not running.'
    case 'no_answer':
      return 'The voice helper did not answer the stop request.'
  }
}

async function talk(app: Jarvis): Promise<string> {
  const helper = app.helper
  if (!app.isLocal || helper === undefined) return NOT_LOCAL
  if (!helper.isRunning) return `The voice helper is not running (${statusLine(app.view) ?? 'stopped'}).`
  const action = app.view.phase === 'listening' ? 'stop' : 'start'
  const outcome = await helper.send('listen', { action })
  if (!outcome.ok) return `Could not ${action} listening: ${outcome.message}`
  return action === 'start' ? 'Listening. Run /jarvis talk again when you have finished speaking.' : 'Sent.'
}

async function testVoice(app: Jarvis): Promise<string> {
  const helper = app.helper
  if (!app.isLocal || helper === undefined) return NOT_LOCAL
  if (!helper.isRunning) return `The voice helper is not running (${statusLine(app.view) ?? 'stopped'}).`
  const outcome = await helper.send('test_voice', {})
  return outcome.ok ? 'Speaking a test line.' : `The test line failed: ${outcome.message}`
}

function restart(app: Jarvis): string {
  if (!app.isLocal) return NOT_LOCAL
  if (app.isSetupRunning) return 'Setup is running; the helper starts when it is done.'
  void app.restartHelper()
  return 'Restarting the voice helper.'
}

async function voice(app: Jarvis, id: string | undefined): Promise<string> {
  if (id === undefined) {
    const current = await app.voiceId()
    return `Voice: ${current ?? 'Fish Audio default'}. Set one with /jarvis voice <id> (a Fish Audio model id), or /jarvis voice default.`
  }
  const isReset = id.toLowerCase() === 'default' || id.toLowerCase() === 'reset'
  if (!isReset && !VOICE_ID.test(id)) return `"${id}" does not look like a Fish Audio model id.`
  await app.setVoiceOverride(isReset ? undefined : id)
  const voiceId = (await app.voiceId()) ?? ''
  // An empty voiceId asks the helper for Fish Audio's default voice.
  const outcome = app.helper?.isRunning === true ? await app.helper.send('config', { voiceId }) : undefined
  const label = voiceId === '' ? 'the Fish Audio default voice' : voiceId
  if (outcome === undefined) return `Voice set to ${label}; it applies when the helper starts.`
  return outcome.ok ? `Voice set to ${label}.` : `Saved ${label}, but the helper refused it: ${outcome.message}`
}

async function devices(app: Jarvis): Promise<string> {
  const helper = app.helper
  if (!app.isLocal || helper === undefined) return NOT_LOCAL
  const doctor = app.platform
    ? `\nFull report: ${shellCommandLine(app.platform, app.platform.venvPython, ['-m', 'jarvis_voice', 'doctor'])}`
    : ''
  if (!helper.isRunning) return `The voice helper is not running (${statusLine(app.view) ?? 'stopped'}).${doctor}`
  const outcome = await helper.send('status', {})
  if (!outcome.ok) return `The helper did not answer: ${outcome.message}${doctor}`
  const report = outcome.response as Partial<StatusResponse>
  return [
    `Microphone: ${report.inputDevice ?? 'unknown'}`,
    `Speakers: ${report.outputDevice ?? 'unknown'}`,
    `Speech model: ${report.sttModel ?? 'unknown'}${report.sttDevice ? ` on ${report.sttDevice}` : ''}`,
    `Voice: ${report.voiceId ?? 'Fish Audio default'} · Fish Audio key ${report.fishKeySet === true ? 'set' : 'missing'}`,
    `Push-to-talk: ${report.pttKey ?? app.settings.pttKey} · helper ${report.version ?? '?'} on ${report.platform ?? '?'}`,
  ].join('\n') + doctor
}
