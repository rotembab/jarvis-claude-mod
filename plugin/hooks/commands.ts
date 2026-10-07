// /jarvis and its subcommands. The command is registered `immediate` so
// `/jarvis stop` works while a reply is streaming; work that takes longer
// than a moment (setup, restart) runs on after the command has answered.

import type { CommandRunResult, UiOpenResult } from 'claude-code'

import type { PaneSize } from './hud'

import type { Jarvis, SttModel, VoiceEngine, WakeMode } from './app'
import { BARGE_IN_MODES, STT_MODELS, VOICE_ENGINES, WAKE_MODES } from './app'
import { findUv, shellCommandLine } from './platform'
import type { BargeInMode, StatusResponse } from './protocol'
import { ROUTING_MODES } from './router'
import { uvMissingMessage } from './setup'
import { statusLine } from './ui'

const HELP = [
  '/jarvis setup [model] [cpu]      install or repair the voice helper and its speech model',
  '/jarvis setup local [cpu]        install the local voice (Chatterbox, about 6 GB)',
  '/jarvis engine <fish|local>      speak with Fish Audio or the local voice',
  '/jarvis wake <on|jarvis|off>     wake on "Hey Jarvis", also on plain "Jarvis", or push-to-talk only',
  '/jarvis bargein <speech|wake|off>  what interrupts Jarvis: any speech, "Hey Jarvis", or nothing',
  '/jarvis routing <auto|off>       Sonnet answers voice requests, Opus or Fable the hard ones; off: your model',
  '/jarvis stop                     stop speaking and cancel the spoken reply',
  '/jarvis pc [check <command>|rules|stop]  PC control: guard tiers, UAC level, rules to paste, stand down',
  '/jarvis talk                     start or stop listening without the push-to-talk key',
  '/jarvis test                     speak a test line',
  '/jarvis restart                  restart the voice helper',
  '/jarvis voice <id|default>       use a Fish Audio voice (its model id)',
  '/jarvis devices                  show the audio devices and models in use',
  '/jarvis hud [on|off]             show the HUD now; on or off: whether it opens with each session',
  '/jarvis focus [on|off]           focus mode: while Jarvis runs, only the HUD and the prompt show',
].join('\n')

const NOT_LOCAL = 'Jarvis runs on your own computer; this session runs in the cloud, so the voice helper is not started here.'

const VOICE_ID = /^[A-Za-z0-9_-]{1,128}$/

/** Runs `/jarvis <args>`; never throws (a failure is the command's output). */
/** What the command's own hook does for it: opens the HUD as the person asked. */
export type CommandUi = { openHud?: (size: PaneSize) => Promise<UiOpenResult> }

export async function runJarvisCommand(app: Jarvis, args: string, ui: CommandUi = {}): Promise<CommandRunResult> {
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
      case 'pc':
        return { text: await app.pc.command(rest) }
      case 'talk':
        return { text: await talk(app) }
      case 'test':
        return { text: await testVoice(app) }
      case 'restart':
        return { text: restart(app) }
      case 'voice':
        return { text: await voice(app, rest[0]) }
      case 'engine':
        return { text: await voiceEngine(app, rest[0]) }
      case 'wake':
        return { text: await wake(app, rest[0]) }
      case 'bargein':
      case 'barge-in':
        return { text: await bargeIn(app, rest[0]) }
      case 'routing':
      case 'models':
        return { text: await routing(app, rest[0]) }
      case 'devices':
        return { text: await devices(app) }
      case 'hud':
        return { text: await hud(app, rest[0], ui) }
      case 'focus':
        return { text: await focus(app, rest[0], ui) }
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
  // Why nothing of Jarvis's starts: elevated, the administrator check still running, or failed.
  const held = app.pc.whyHeld()
  if (held !== undefined) lines.push(held)
  const hello = app.helper?.hello
  if (hello !== undefined) {
    const ready = app.ready
    const model = ready ? `speech model ${ready.sttModel} on ${ready.sttDevice}` : 'loading models'
    const voiceId = (await app.voiceEngine()) === 'local' ? 'local' : ((await app.voiceId()) ?? 'Fish Audio default')
    lines.push(`Helper ${hello.version} (pid ${hello.pid}) · ${model} · voice ${voiceId}`)
    lines.push(`${await handsFreeLine(app)} · echo cancelling ${app.echoCancel}`)
    lines.push(await routingLine(app))
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
  // Nothing installs as administrator, or before the check has said.
  const held = app.pc.whyHeld()
  if (held !== undefined) return held
  if (args[0]?.toLowerCase() === 'local') return await setupLocal(app, args.slice(1))
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

async function setupLocal(app: Jarvis, args: string[]): Promise<string> {
  const { engine, platform } = app
  if (engine === undefined || platform === undefined) return NOT_LOCAL
  let useCuda: boolean | undefined
  for (const arg of args) {
    const word = arg.toLowerCase()
    if (word === 'cpu') useCuda = false
    else if (word === 'cuda' || word === 'gpu') useCuda = true
    else return `Unknown option "${arg}" for /jarvis setup local; add "cpu" to install PyTorch without CUDA.`
  }
  const uv = await findUv(engine, platform)
  if (uv === undefined) return uvMissingMessage(platform)
  void app.runLocalSetup(uv, { useCuda })
  const clip = app.settings.localVoiceClip
  return [
    `Setting up the local voice with ${uv}:`,
    `  1. a separate Python 3.12 environment in ${platform.localVenvDir} with PyTorch and Chatterbox-Turbo (about 3 GB)`,
    `  2. the Chatterbox-Turbo model (about 3 GB) in ${platform.dataDir}${platform.sep}models, and one test sentence`,
    clip === undefined
      ? 'No reference clip is set, so it uses Chatterbox\'s built-in voice. Set "Local voice clip" in the plugin settings to copy a voice from a clip of 10 to 20 s.'
      : `Voice: copied from ${clip}.`,
    'Progress shows in the status line. Then switch with /jarvis engine local.',
  ].join('\n')
}

async function voiceEngine(app: Jarvis, choice: string | undefined): Promise<string> {
  const current = await app.voiceEngine()
  if (choice === undefined) {
    return `Voice engine: ${current === 'local' ? 'the local voice (Chatterbox)' : 'Fish Audio'}. Switch with /jarvis engine fish or /jarvis engine local.`
  }
  const engine = VOICE_ENGINES.find(one => one === choice.toLowerCase()) as VoiceEngine | undefined
  if (engine === undefined) return `Unknown engine "${choice}". Use /jarvis engine fish or /jarvis engine local.`
  const platform = app.platform
  if (engine === 'local' && platform !== undefined && app.engine !== undefined) {
    const isInstalled = await app.engine.exists(platform.localVenvPython).catch(() => false)
    if (!isInstalled) return 'The local voice is not installed yet. Run /jarvis setup local first (it downloads about 6 GB).'
  }
  await app.setVoiceEngine(engine)
  if (engine === current) return `Already using ${engine === 'local' ? 'the local voice' : 'Fish Audio'}.`
  if (app.isLocal && !app.isSetupRunning) void app.restartHelper()
  return engine === 'local'
    ? 'Switched to the local voice. The helper restarts and loads the model onto your GPU, which takes a few seconds.'
    : 'Switched to Fish Audio. The helper restarts.'
}

const BARGE_IN_LABELS: Record<BargeInMode, string> = {
  speech: 'talking over Jarvis interrupts him',
  wake: 'only "Hey Jarvis" interrupts him',
  off: 'only push-to-talk or /jarvis stop interrupts him',
}

/** The helper's ready.wakePhrase while plain "Jarvis" works (it says "Hey Jarvis" otherwise). */
const PLAIN_WAKE_PHRASE = 'Jarvis'
// A helper that takes plain "Jarvis" fetches its model when it is switched on; an older one never will.
const PLAIN_NOT_READY = 'plain "Jarvis" is not ready yet: its model is downloading; if it stays so, run /jarvis setup'
const PLAIN_NEEDS_SETUP = 'plain "Jarvis" is not ready: run /jarvis setup'

async function handsFreeLine(app: Jarvis): Promise<string> {
  const mode = await app.wakeWord()
  const phrase = app.ready?.wakePhrase
  let wakeText = mode === 'off' ? 'Wake word off' : phrase !== undefined ? `Say "${phrase}"` : 'Wake word loading'
  if (mode === 'jarvis' && phrase !== undefined) {
    const notReady = app.canPlainWake ? PLAIN_NOT_READY : PLAIN_NEEDS_SETUP
    wakeText = phrase === PLAIN_WAKE_PHRASE ? 'Say "Jarvis" or "Hey Jarvis"' : `${wakeText} (${notReady})`
  }
  return `${wakeText} · ${BARGE_IN_LABELS[await app.bargeIn()]}`
}

type HandsFreeConfig = { wakeWord?: boolean; plainWake?: boolean; bargeIn?: BargeInMode }

/** Sends a hands-free change to a running helper; undefined when none runs (it applies at the next start). */
async function sendConfig(app: Jarvis, body: HandsFreeConfig): Promise<string | undefined> {
  if (app.helper?.isRunning !== true) return undefined
  const outcome = await app.helper.send('config', body)
  return outcome.ok ? '' : outcome.message
}

const WAKE_SAVED: Record<WakeMode, string> = {
  on: 'Jarvis listens for "Hey Jarvis". Nothing is recorded or sent until he hears it.',
  jarvis:
    'Jarvis listens for "Jarvis" at the start of what you say, and for "Hey Jarvis". Nothing is recorded or sent until he hears one; plain "Jarvis" never interrupts him.',
  off: 'Wake word off: hold the push-to-talk key to talk.',
}

async function wake(app: Jarvis, choice: string | undefined): Promise<string> {
  if (choice === undefined) {
    return `${await handsFreeLine(app)}. Switch with /jarvis wake on, jarvis or off.`
  }
  const mode = WAKE_MODES.find(one => one === choice.toLowerCase())
  if (mode === undefined) return `Unknown choice "${choice}". Use /jarvis wake on, jarvis or off.`
  await app.setWakeWord(mode)
  // A helper installed before plain "Jarvis" would refuse plainWake, and with it the whole change.
  const canPlain = app.canPlainWake
  const refused = await sendConfig(app, { wakeWord: mode !== 'off', ...(canPlain ? { plainWake: mode === 'jarvis' } : {}) })
  if (refused) return `Saved, but the helper refused it: ${refused}`
  if (mode === 'jarvis' && app.helper?.isRunning === true && !canPlain) {
    return 'Saved, but this voice helper is older than plain "Jarvis": run /jarvis setup to update it. Until then, say "Hey Jarvis".'
  }
  return WAKE_SAVED[mode]
}

async function bargeIn(app: Jarvis, choice: string | undefined): Promise<string> {
  if (choice === undefined) {
    return `Now ${BARGE_IN_LABELS[await app.bargeIn()]}. Choose with /jarvis bargein speech, wake or off.`
  }
  const mode = BARGE_IN_MODES.find(one => one === choice.toLowerCase())
  if (mode === undefined) return `Unknown choice "${choice}". Use /jarvis bargein speech, wake or off.`
  await app.setBargeIn(mode)
  const refused = await sendConfig(app, { bargeIn: mode })
  if (refused) return `Saved, but the helper refused it: ${refused}`
  const note = mode === 'speech' ? ' With speakers instead of a headset, Jarvis may hear himself: use /jarvis bargein wake there.' : ''
  return `Now ${BARGE_IN_LABELS[mode]}.${note}`
}

async function routingLine(app: Jarvis): Promise<string> {
  return (await app.routingMode()) === 'auto'
    ? 'Voice requests: Sonnet, or Opus and Fable for hard ones (say "use Opus" or "think hard" to choose)'
    : "Voice requests: your session's model"
}

async function routing(app: Jarvis, choice: string | undefined): Promise<string> {
  if (choice === undefined) return `${await routingLine(app)}. Switch with /jarvis routing auto or /jarvis routing off.`
  const mode = ROUTING_MODES.find(one => one === choice.toLowerCase())
  if (mode === undefined) return `Unknown choice "${choice}". Use /jarvis routing auto or /jarvis routing off.`
  await app.setRoutingMode(mode)
  return mode === 'auto'
    ? 'Sonnet answers voice requests; a quick check sends complex ones to Opus and the hardest to Fable. Typed messages keep your model.'
    : "Your session's model answers voice requests."
}

async function hud(app: Jarvis, choice: string | undefined, ui: CommandUi): Promise<string> {
  if (!app.isLocal) return NOT_LOCAL
  switch (choice?.toLowerCase()) {
    case undefined:
    case 'show':
      return (await app.openHud(ui.openHud)) ? 'HUD open.' : 'The HUD could not open here; it shows once there is room.'
    case 'on':
      await app.setHudOn(true)
      await app.openHud(ui.openHud)
      return 'The HUD opens with each session (it shows beside the conversation in wide windows).'
    case 'off':
      await app.setHudOn(false)
      await app.closeHud()
      return 'HUD closed. It stays closed in new sessions until /jarvis hud on.'
    default:
      return `Unknown choice "${choice}". Use /jarvis hud, /jarvis hud on or /jarvis hud off.`
  }
}

async function focus(app: Jarvis, choice: string | undefined, ui: CommandUi): Promise<string> {
  if (!app.isLocal) return NOT_LOCAL
  switch (choice?.toLowerCase()) {
    case undefined:
    case 'on': {
      await app.setFocus(true)
      const isOpen = await app.openHud(ui.openHud)
      const how = 'Typing a prompt brings the conversation back until you next talk to Jarvis; /jarvis focus off ends it.'
      if (!isOpen) return `Focus mode is on; it takes over once the HUD has room to open. ${how}`
      return app.isRunning
        ? `Focus mode on: while Jarvis runs, the HUD fills the screen and the conversation folds away. ${how}`
        : `Focus mode is on; it takes over once Jarvis is running (/jarvis setup if he is not set up). ${how}`
    }
    case 'off':
      await app.setFocus(false)
      return 'Focus mode off: the conversation is back beside the HUD.'
    default:
      return `Unknown choice "${choice}". Use /jarvis focus on or /jarvis focus off.`
  }
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
  const isRecheck = app.pc.hasAdminCheckFailed
  void app.restartHelper()
  return isRecheck
    ? 'Checking again whether Claude Code runs as administrator; the voice helper starts if it does not.'
    : 'Restarting the voice helper.'
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
    `Wake word: ${report.wakeWord ?? 'off'} · barge-in: ${report.bargeIn ?? 'push-to-talk only'} · echo cancelling: ${report.echoCancel ?? 'off'}`,
  ].join('\n') + doctor
}
