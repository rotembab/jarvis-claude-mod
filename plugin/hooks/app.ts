// The mod's session-wide coordinator: built in register() from the user's
// settings, bound to the engine port at session.start, it owns the helper
// supervisor, the voice controller, hand control and the view the status line
// and band draw.

import type { PluginOptions, RenderSurface, UiOpenResult } from 'claude-code'

import type { JarvisPhase, JarvisView } from '../types'
import type { Engine } from './engine'
import { describeError } from './engine'
import type { HandsEngine, HandsSettings } from './hands'
import { Hands, readHandsSettings } from './hands'
import { Helper } from './helper'
import type { HelperPhase } from './helper'
import { HomeControl } from './home'
import { Hud, hudMode } from './hud'
import type { PaneSize } from './hud'
import type { Platform } from './platform'
import { detectPlatform, isRemoteSession } from './platform'
import type { BargeInMode, ConfigCommand, HelperEvent, HelperState, ReadyEvent } from './protocol'
import type { RoutingMode } from './router'
import { ModelRouter, ROUTING_MODES } from './router'
import { hasNvidiaGpu, runLocalVoiceSetup, runSetup } from './setup'
import { statusLine } from './ui'
import { Voice } from './voice'

export const STT_MODELS = ['auto', 'base.en', 'small.en', 'small', 'medium', 'large-v3-turbo'] as const
export type SttModel = (typeof STT_MODELS)[number]
/** What `auto` means on the CPU (the helper's own pick there). */
export const CPU_AUTO_MODEL: SttModel = 'small.en'

/** What /jarvis setup was asked for: a model, and whether to install the CUDA libraries. */
export type SetupOptions = { sttModel?: SttModel; useCuda?: boolean }

/** Fish Audio models offered in the plugin settings; the free one is the default. */
export const FISH_MODELS = ['s2.1-pro-free', 's2.1-pro'] as const

/** Who speaks: Fish Audio, or the local voice (Chatterbox-Turbo on this machine). */
export const VOICE_ENGINES = ['fish', 'local'] as const
export type VoiceEngine = (typeof VOICE_ENGINES)[number]

/** What interrupts Jarvis by voice: any speech (default), only the wake word, or nothing. */
export const BARGE_IN_MODES = ['speech', 'wake', 'off'] as const satisfies readonly BargeInMode[]

/** What wakes Jarvis: "Hey Jarvis" (default), plain "Jarvis" as well, or nothing (push-to-talk only). */
export const WAKE_MODES = ['on', 'jarvis', 'off'] as const
export type WakeMode = (typeof WAKE_MODES)[number]
/** The hello capability of a helper whose config takes `plainWake` (older ones refuse the whole config). */
export const PLAIN_WAKE_CAPABILITY = 'wake.plain'

/** Wake word sensitivity, as the helper's score threshold: high wakes more easily, and falsely more often. */
export const WAKE_SENSITIVITY = { low: 0.7, medium: 0.5, high: 0.3 } as const
export type WakeSensitivity = keyof typeof WAKE_SENSITIVITY

export type JarvisSettings = {
  fishApiKey?: string
  fishModel: string
  voiceId?: string
  voiceEngine: VoiceEngine
  localVoiceClip?: string
  pttKey: string
  sttModel: SttModel
  language: string
  wakeWord: WakeMode
  bargeIn: BargeInMode
  wakeSensitivity: WakeSensitivity
  /** The helper removes Jarvis's own voice from what the microphone hears. */
  echoCancelling: boolean
  modelRouting: RoutingMode
  hands: HandsSettings
}

const optionString = (options: PluginOptions, key: string): string | undefined => {
  const value = options[key]
  return typeof value === 'string' && value.trim() !== '' ? value.trim() : undefined
}

/** Reads the manifest's userConfig values, defaults filled in. */
export function readSettings(options: PluginOptions): JarvisSettings {
  const stt = optionString(options, 'sttModel')
  return {
    fishApiKey: optionString(options, 'fishApiKey'),
    fishModel: FISH_MODELS.find(model => model === optionString(options, 'fishModel')) ?? FISH_MODELS[0],
    voiceId: optionString(options, 'voiceId'),
    voiceEngine: VOICE_ENGINES.find(engine => engine === optionString(options, 'voiceEngine')) ?? 'fish',
    localVoiceClip: optionString(options, 'localVoiceClip'),
    pttKey: optionString(options, 'pttKey') ?? 'right ctrl',
    sttModel: STT_MODELS.find(model => model === stt) ?? 'auto',
    language: optionString(options, 'language') ?? 'en',
    wakeWord: WAKE_MODES.find(mode => mode === optionString(options, 'wakeWord')) ?? 'on',
    bargeIn: BARGE_IN_MODES.find(mode => mode === optionString(options, 'bargeIn')) ?? 'speech',
    wakeSensitivity:
      (Object.keys(WAKE_SENSITIVITY) as WakeSensitivity[]).find(level => level === optionString(options, 'wakeSensitivity')) ??
      'medium',
    echoCancelling: optionString(options, 'echoCancelling') !== 'off',
    modelRouting: ROUTING_MODES.find(mode => mode === optionString(options, 'modelRouting')) ?? 'auto',
    hands: readHandsSettings(options),
  }
}

const HELPER_STATES: ReadonlySet<string> = new Set<HelperState>([
  'starting',
  'sleeping',
  'listening',
  'transcribing',
  'speaking',
  'awake',
  'error',
])
const LOCAL_SURFACES: ReadonlySet<RenderSurface> = new Set<RenderSurface>(['terminal', 'desktop'])
const VOICE_OVERRIDE_KEY = 'voiceId'
const ENGINE_OVERRIDE_KEY = 'voiceEngine'
const WAKE_OVERRIDE_KEY = 'wakeWord'
const BARGE_IN_OVERRIDE_KEY = 'bargeIn'
const ROUTING_OVERRIDE_KEY = 'modelRouting'
/** Whether the HUD pane opens by itself (/jarvis hud on|off). */
const HUD_OVERRIDE_KEY = 'hud'
const FOCUS_KEY = 'focus'
/**
 * The speech model the last `/jarvis setup <model>` installed (the helper
 * never downloads one itself), with the sttModel setting it overrode: it
 * lapses once the setting changes, so the setting always has the last word.
 */
const STT_OVERRIDE_KEY = 'sttModel'
type SttOverride = { model: SttModel; setting: SttModel }
const LEVEL_INTERVAL_MS = 200

export class Jarvis {
  engine: Engine | undefined
  platform: Platform | undefined
  helper: Helper | undefined
  voice: Voice | undefined
  /** Hand control (hands.ts): its own helper, started only when the user turned it on. */
  hands: Hands | undefined
  /** The HUD pane's ring and action log (local sessions). */
  hud: Hud | undefined
  /** The home_control tool and /jarvis home (home.ts). */
  readonly home: HomeControl = new HomeControl(this)
  /** False in a cloud session: nothing local is started there. */
  isLocal = false
  ready: ReadyEvent | undefined
  view: JarvisView = { phase: 'stopped' }
  isSetupRunning = false

  private hasAutoStarted = false
  /** A local surface attached; it may come while session.start is still binding. */
  private hasLocalAttach = false
  private hasAnnouncedReady = false
  private hasAnnouncedWake = false
  /** This helper's echo canceller could not load, or stopped working (aec_unavailable). */
  private isEchoCancelBroken = false
  private lastLevelAt = 0
  private shownStatus: string | undefined
  /** Focus mode: the person's setting, whether they typed since Jarvis last listened, whether it shows, and whether it folds the conversation. */
  private isFocusOn = false
  private hasTypedSinceVoice = false
  private isFocusShown = false
  private isFolded = false

  constructor(readonly settings: JarvisSettings) {}

  /** session.start: binds the port; starts the helper when a local surface draws. */
  async onSessionStart(engine: HandsEngine, surface: RenderSurface | null): Promise<void> {
    this.engine = engine
    const env = await engine.env()
    const platform = await detectPlatform(engine, env)
    this.platform = platform
    const helper = new Helper(engine, {
      platform,
      fishApiKey: this.settings.fishApiKey,
      fishModel: this.settings.fishModel,
      echoCancel: this.settings.echoCancelling,
      noProxy: env.NO_PROXY,
      sttModel: () => this.sttModel(),
      tts: async () => ({ engine: await this.voiceEngine(), localVoiceClip: this.settings.localVoiceClip }),
      initialConfig: () => this.initialConfig(),
      onEvent: event => this.onHelperEvent(event),
      onPhase: (phase, detail) => this.onHelperPhase(phase, detail),
    })
    this.helper = helper
    this.voice = new Voice(engine, {
      platform,
      helper,
      onSubmitted: text => this.patch({ lastUtterance: text }),
      router: new ModelRouter(engine, { mode: () => this.routingMode(), complete: request => engine.complete(request) }),
    })
    this.isLocal = !isRemoteSession(env)
    this.hands = new Hands(engine, {
      platform,
      settings: this.settings.hands,
      isLocal: this.isLocal,
      noProxy: env.NO_PROXY,
      voice: () => this.helper,
      onView: hands => this.patch({ hands }),
    })
    if (!this.isLocal) {
      this.publish({ phase: 'unavailable' })
      return
    }
    this.hud?.dispose()
    this.hud = new Hud(engine)
    this.hud.setPhase(this.view.phase)
    this.hud.save()
    this.hud.onShownChange = () => this.updateFocus()
    this.isFocusOn = await this.focusSetting()
    // $.state outlives a reload: focus mode starts hidden until the new HUD draws.
    this.isFocusShown = false
    this.isFolded = false
    void engine.writeFolded(false).catch(() => undefined)
    if (await this.isHudOn()) void this.openHud()
    // The desktop app starts its sessions with no surface, then attaches one
    // (perhaps while the awaits above ran).
    if ((surface !== null && LOCAL_SURFACES.has(surface)) || this.hasLocalAttach) this.autoStart()
  }

  onAttach(surface: RenderSurface): void {
    if (!LOCAL_SURFACES.has(surface)) return
    this.hasLocalAttach = true
    // The desktop app attaches its surface after session.start: show the HUD there too.
    if (this.isLocal && this.hud !== undefined) void this.isHudOn().then(isOn => (isOn ? this.openHud() : false))
    // Before session.start has finished, it starts the helper itself.
    if (this.isLocal) this.autoStart()
  }

  /** Starts the helper (a user's request retries after already_running or a give-up). */
  startHelper(userInitiated: boolean): void {
    if (!this.isLocal || this.isSetupRunning) return
    this.helper?.start({ userInitiated })
  }

  async restartHelper(): Promise<void> {
    if (!this.isLocal || this.isSetupRunning || this.helper === undefined) return
    await this.helper.restart()
  }

  /** The voice override /jarvis voice saved, else the userConfig value. */
  async voiceId(): Promise<string | undefined> {
    const stored = await this.engine?.storeGet(VOICE_OVERRIDE_KEY).catch(() => undefined)
    return typeof stored === 'string' && stored !== '' ? stored : this.settings.voiceId
  }

  /** The engine /jarvis engine chose, else the voiceEngine setting. */
  async voiceEngine(): Promise<VoiceEngine> {
    const stored = await this.engine?.storeGet(ENGINE_OVERRIDE_KEY).catch(() => undefined)
    return VOICE_ENGINES.find(engine => engine === stored) ?? this.settings.voiceEngine
  }

  async setVoiceEngine(engine: VoiceEngine): Promise<void> {
    await this.engine?.storeSet(ENGINE_OVERRIDE_KEY, engine)
  }

  /** What wakes Jarvis: /jarvis wake's choice, else the wakeWord setting. */
  async wakeWord(): Promise<WakeMode> {
    const stored = await this.engine?.storeGet(WAKE_OVERRIDE_KEY).catch(() => undefined)
    // Before plain "Jarvis", /jarvis wake saved on and off as true and false.
    if (typeof stored === 'boolean') return stored ? 'on' : 'off'
    return WAKE_MODES.find(mode => mode === stored) ?? this.settings.wakeWord
  }

  async setWakeWord(mode: WakeMode): Promise<void> {
    await this.engine?.storeSet(WAKE_OVERRIDE_KEY, mode)
  }

  /** The running helper takes `plainWake` (a helper installed before it would refuse the whole config). */
  get canPlainWake(): boolean {
    return this.helper?.hello?.capabilities.includes(PLAIN_WAKE_CAPABILITY) === true
  }

  /** What interrupts Jarvis by voice: /jarvis bargein's choice, else the bargeIn setting. */
  async bargeIn(): Promise<BargeInMode> {
    const stored = await this.engine?.storeGet(BARGE_IN_OVERRIDE_KEY).catch(() => undefined)
    return BARGE_IN_MODES.find(mode => mode === stored) ?? this.settings.bargeIn
  }

  async setBargeIn(mode: BargeInMode): Promise<void> {
    await this.engine?.storeSet(BARGE_IN_OVERRIDE_KEY, mode)
  }

  /** Whether the HUD opens with the session: /jarvis hud's choice, on by default. */
  async isHudOn(): Promise<boolean> {
    const stored = await this.engine?.storeGet(HUD_OVERRIDE_KEY).catch(() => undefined)
    return stored !== false
  }

  async setHudOn(isOn: boolean): Promise<void> {
    await this.engine?.storeSet(HUD_OVERRIDE_KEY, isOn)
  }

  /** Whether focus mode is on: /jarvis focus's choice, off by default. */
  async focusSetting(): Promise<boolean> {
    const stored = await this.engine?.storeGet(FOCUS_KEY).catch(() => undefined)
    return stored === true
  }

  async setFocus(isOn: boolean): Promise<void> {
    this.isFocusOn = isOn
    this.hasTypedSinceVoice = false
    await this.engine?.storeSet(FOCUS_KEY, isOn)
    this.updateFocus()
  }

  /** Jarvis's helper is up (the ring is not the gray one). */
  get isRunning(): boolean {
    return hudMode(this.view.phase, false) !== 'offline'
  }

  /** The person typed a prompt: the conversation comes back until Jarvis next listens. */
  onTyped(): void {
    if (this.hasTypedSinceVoice) return
    this.hasTypedSinceVoice = true
    this.updateFocus()
  }

  /** A turn of the main conversation ended: in focus mode the HUD shows the reply. */
  async onTurnComplete(): Promise<void> {
    this.hud?.onTurnComplete()
    if (!this.isFocusOn || this.engine === undefined) return
    try {
      const messages = await this.engine.messages()
      const reply = [...messages].reverse().find(message => message.role === 'assistant' && message.text.trim() !== '')
      this.hud?.setLastReply(reply?.text.trim())
    } catch (error) {
      this.engine.debug(`jarvis: could not read the last reply: ${describeError(error)}`)
    }
  }

  /**
   * Opens the HUD pane; false when the surface could not place it yet (a
   * narrow terminal). `open` is the person's own when they asked for it
   * (/jarvis hud), which the engine places at any width.
   */
  async openHud(open?: (size: PaneSize) => Promise<UiOpenResult>): Promise<boolean> {
    const engine = this.engine
    if (engine === undefined || this.hud === undefined) return false
    try {
      const opened = await (open ?? engine.openPane)(this.hud.size)
      if (!opened.isPlaced) engine.debug(`jarvis: HUD waits to be placed (${opened.reason})`)
      return opened.isPlaced
    } catch (error) {
      engine.debug(`jarvis: HUD did not open: ${describeError(error)}`)
      return false
    }
  }

  async closeHud(): Promise<void> {
    await this.engine?.closePane().catch(() => undefined)
    this.hud?.onClosed()
  }

  /** Echo cancelling as set, or unavailable when the running helper could not load it. */
  get echoCancel(): 'on' | 'off' | 'unavailable' {
    if (!this.settings.echoCancelling) return 'off'
    return this.isEchoCancelBroken ? 'unavailable' : 'on'
  }

  /** Whether voice requests are routed between Sonnet, Opus and Fable: /jarvis routing's choice, else the setting. */
  async routingMode(): Promise<RoutingMode> {
    const stored = await this.engine?.storeGet(ROUTING_OVERRIDE_KEY).catch(() => undefined)
    return ROUTING_MODES.find(mode => mode === stored) ?? this.settings.modelRouting
  }

  async setRoutingMode(mode: RoutingMode): Promise<void> {
    await this.engine?.storeSet(ROUTING_OVERRIDE_KEY, mode)
  }

  /**
   * The speech model in use: the one `/jarvis setup <model>` last installed
   * while the setting is what it was then, else the setting.
   */
  async sttModelChoice(): Promise<SttModel> {
    return (await this.storedSttModel()) ?? this.settings.sttModel
  }

  /** The speech model the helper loads (`--stt-model`); undefined lets it choose ("auto"). */
  async sttModel(): Promise<string | undefined> {
    const model = await this.sttModelChoice()
    return model === 'auto' ? undefined : model
  }

  /** The model a setup installs: the one named, else the one in use. */
  async setupModel({ sttModel, useCuda }: SetupOptions): Promise<SttModel> {
    const model = sttModel ?? (await this.sttModelChoice())
    // The helper resolves auto by the GPU it sees, not by the CUDA libraries
    // installed: without them, auto would pick the GPU model and fall back.
    return model === 'auto' && useCuda === false ? CPU_AUTO_MODEL : model
  }

  async setVoiceOverride(voiceId: string | undefined): Promise<void> {
    if (this.engine === undefined) return
    if (voiceId === undefined) await this.engine.storeDelete(VOICE_OVERRIDE_KEY)
    else await this.engine.storeSet(VOICE_OVERRIDE_KEY, voiceId)
  }

  /**
   * /jarvis setup after uv was found: stops the helper (Windows locks a
   * running venv), installs, downloads the model, starts the helper again.
   * A model named here (or pinned by `cpu`) is remembered, so the helper
   * loads the one installed. Then it updates the hand helper, when that is
   * installed: so one /jarvis setup after a plugin update covers both.
   */
  async runSetup(uv: string, request: SetupOptions): Promise<void> {
    const { engine, platform, helper } = this
    if (engine === undefined || platform === undefined || helper === undefined || this.isSetupRunning) return
    this.isSetupRunning = true
    this.publish({ ...this.view, phase: 'setup', detail: 'stopping the helper' })
    try {
      if (!(await helper.stop())) {
        // uv would fail on (or half-replace) files the live helper holds open.
        engine.log(
          'Jarvis setup did not run: the voice helper did not exit, and a running helper keeps its files locked. It stops by itself within a minute; then run /jarvis setup again.',
        )
        engine.toast('Jarvis setup did not run; the transcript says why.')
        return
      }
      const useCuda = request.useCuda ?? (await hasNvidiaGpu(engine, platform))
      const sttModel = await this.setupModel(request)
      const result = await runSetup(engine, platform, uv, {
        sttModel: sttModel === 'auto' ? undefined : sttModel,
        useCuda,
        onProgress: text => this.publish({ ...this.view, phase: 'setup', detail: text }),
        debug: line => engine.debug(`jarvis: ${line}`),
      })
      if (result.ok) {
        const isChosen = request.sttModel !== undefined || request.useCuda === false
        await this.rememberSttModel(isChosen ? sttModel : undefined)
        engine.log(`Jarvis setup finished (${result.summary}). Starting the voice helper.`)
        engine.toast('Jarvis is installed. Starting the voice helper.')
      } else {
        engine.log(`Jarvis setup failed: ${result.message}`)
        engine.toast('Jarvis setup failed; the transcript has the details.')
      }
    } catch (error) {
      engine.log(`Jarvis setup failed: ${describeError(error)}`)
      engine.toast('Jarvis setup failed; the transcript has the details.')
    } finally {
      this.isSetupRunning = false
      this.publish({ ...this.view, phase: 'stopped', detail: undefined })
      helper.start({ userInitiated: true })
      await this.refreshHands(uv)
    }
  }

  /**
   * The hand helper's venv holds a copy of the plugin's hands package (a
   * non-editable install), so an update reaches it only through a setup of
   * its own: /jarvis setup runs that too while hand control is installed.
   */
  private async refreshHands(uv: string): Promise<void> {
    const hands = this.hands
    if (hands === undefined || !hands.isLocal || !(await hands.isInstalled())) return
    await hands.runSetup(uv, { isRefresh: true })
  }

  /**
   * /jarvis setup local after uv was found: stops the helper (a running local
   * voice holds its venv's files), installs the local voice and its model,
   * then starts the helper again. The engine does not switch by itself.
   */
  async runLocalSetup(uv: string, request: { useCuda?: boolean }): Promise<void> {
    const { engine, platform, helper } = this
    if (engine === undefined || platform === undefined || helper === undefined || this.isSetupRunning) return
    this.isSetupRunning = true
    this.publish({ ...this.view, phase: 'setup', detail: 'stopping the helper' })
    try {
      if (!(await helper.stop())) {
        engine.log('Jarvis local voice setup did not run: the voice helper did not exit. Run /jarvis setup local again in a minute.')
        engine.toast('Jarvis setup did not run; the transcript says why.')
        return
      }
      const result = await runLocalVoiceSetup(engine, platform, uv, {
        useCuda: request.useCuda ?? (await hasNvidiaGpu(engine, platform)),
        voiceClip: this.settings.localVoiceClip,
        onProgress: text => this.publish({ ...this.view, phase: 'setup', detail: text }),
        debug: line => engine.debug(`jarvis: ${line}`),
      })
      if (result.ok) {
        const isOn = (await this.voiceEngine()) === 'local'
        engine.log(`Jarvis local voice installed (${result.summary}).${isOn ? '' : ' Switch to it with /jarvis engine local.'}`)
        engine.toast(isOn ? 'The local voice is installed.' : 'The local voice is installed. Switch with /jarvis engine local.')
      } else {
        engine.log(`Jarvis local voice setup failed: ${result.message}`)
        engine.toast('Jarvis local voice setup failed; the transcript has the details.')
      }
    } catch (error) {
      engine.log(`Jarvis local voice setup failed: ${describeError(error)}`)
      engine.toast('Jarvis local voice setup failed; the transcript has the details.')
    } finally {
      this.isSetupRunning = false
      this.publish({ ...this.view, phase: 'stopped', detail: undefined })
      helper.start({ userInitiated: true })
    }
  }

  /** Merges a change into the view and publishes it. */
  patch(change: Partial<JarvisView>): void {
    this.publish({ ...this.view, ...change })
  }

  /** The `/jarvis setup <model>` override, while the setting is the one it overrode. */
  private async storedSttModel(): Promise<SttModel | undefined> {
    const stored = await this.engine?.storeGet(STT_OVERRIDE_KEY).catch(() => undefined)
    if (typeof stored !== 'object' || stored === null) return undefined
    const { model, setting } = stored as Partial<Record<keyof SttOverride, unknown>>
    return setting === this.settings.sttModel ? STT_MODELS.find(one => one === model) : undefined
  }

  /**
   * Records the model a setup installed against the current setting; a model
   * equal to the setting needs no override. Undefined (a bare setup, which
   * installed the model in use) only clears an override that has lapsed.
   */
  private async rememberSttModel(model: SttModel | undefined): Promise<void> {
    const engine = this.engine
    if (engine === undefined) return
    const setting = this.settings.sttModel
    if (model === undefined) {
      if ((await this.storedSttModel()) === undefined) await engine.storeDelete(STT_OVERRIDE_KEY)
    } else if (model === setting) {
      await engine.storeDelete(STT_OVERRIDE_KEY)
    } else {
      const override: SttOverride = { model, setting }
      await engine.storeSet(STT_OVERRIDE_KEY, override)
    }
  }

  private autoStart(): void {
    if (this.hasAutoStarted) return
    this.hasAutoStarted = true
    this.startHelper(false)
    void this.hands?.autoStart()
  }

  private async initialConfig(): Promise<ConfigCommand> {
    const voiceId = await this.voiceId()
    const wake = await this.wakeWord()
    // The speech model is not here: the helper loads it from its argv at start.
    // plainWake is off in a fresh helper, so it is sent only to switch it on.
    return {
      pttKey: this.settings.pttKey,
      language: this.settings.language,
      wakeWord: wake !== 'off',
      ...(wake === 'jarvis' && this.canPlainWake ? { plainWake: true } : {}),
      bargeIn: await this.bargeIn(),
      wakeThreshold: WAKE_SENSITIVITY[this.settings.wakeSensitivity],
      ...(voiceId === undefined ? {} : { voiceId }),
    }
  }

  private onHelperEvent(event: HelperEvent): void {
    this.voice?.onHelperEvent(event)
    switch (event.type) {
      case 'hello':
        this.isEchoCancelBroken = false // a new helper tries again
        this.patch({ phase: 'starting', detail: undefined })
        return
      case 'state':
        this.patch({ phase: event.state, ...(event.state === 'listening' ? {} : { micLevel: undefined }) })
        return
      case 'ready': {
        this.ready = event
        this.patch({ pttKey: event.pttKey, wakePhrase: event.wakePhrase })
        const wake = event.wakePhrase
        if (!this.hasAnnouncedReady) {
          this.hasAnnouncedReady = true
          this.hasAnnouncedWake = wake !== undefined
          this.engine?.toast(
            wake === undefined
              ? `Jarvis is ready. Hold ${event.pttKey} to talk.`
              : `Jarvis is ready. Say "${wake}" or hold ${event.pttKey} to talk.`,
          )
        } else if (wake !== undefined && !this.hasAnnouncedWake) {
          // The wake word model arrived after the first ready (its first download).
          this.hasAnnouncedWake = true
          this.engine?.toast(`Jarvis is listening for "${wake}".`)
        }
        return
      }
      case 'level': {
        this.hud?.setLevels(event.mic, event.out)
        const now = performance.now()
        if (this.view.phase !== 'listening' || now - this.lastLevelAt < LEVEL_INTERVAL_MS) return
        this.lastLevelAt = now
        this.patch({ micLevel: event.mic })
        return
      }
      case 'utterance':
        this.patch({ lastUtterance: event.text })
        return
      case 'barge_in':
        this.hud?.onBargeIn()
        return
      case 'error': {
        if (event.code === 'aec_unavailable') this.isEchoCancelBroken = true
        const detail = event.hint ? `${event.message} (${event.hint})` : event.message
        if (event.fatal) this.patch({ phase: 'error', detail })
        else {
          this.patch({ detail })
          this.engine?.toast(`Jarvis: ${detail}`, { timeoutMs: 8000 })
        }
        return
      }
      default:
        return
    }
  }

  private onHelperPhase(phase: HelperPhase, detail: string | undefined): void {
    if (this.isSetupRunning) return // setup owns the status line until it ends
    const phases: Record<HelperPhase, JarvisPhase | undefined> = {
      stopped: 'stopped',
      not_installed: 'not_installed',
      starting: 'starting',
      running: undefined, // the helper's own state events say what it is doing
      restarting: 'restarting',
      elsewhere: 'elsewhere',
      failed: 'failed',
    }
    const next = phases[phase]
    if (next === undefined) {
      if (!HELPER_STATES.has(this.view.phase)) this.patch({ phase: 'starting' })
      return
    }
    this.patch({ phase: next, detail })
  }

  /**
   * Focus mode shows while it is on, the HUD is drawn in the terminal, Jarvis
   * runs, and the person has not typed since he last listened. Docked beside
   * the conversation (the fullscreen view) the conversation's rows fold away;
   * above the prompt on the main screen they print as usual and the tall pane
   * pushes them up into the scrollback, so none is lost.
   */
  private updateFocus(): void {
    const isShown = this.isFocusOn && this.hud?.isShown === true && this.isRunning && !this.hasTypedSinceVoice
    if (isShown !== this.isFocusShown) {
      this.isFocusShown = isShown
      this.hud?.setFocus(isShown)
    }
    const isFolded = isShown && this.hud?.isDocked === true
    if (isFolded === this.isFolded) return
    this.isFolded = isFolded
    void this.engine?.writeFolded(isFolded).catch(() => undefined)
  }

  private publish(view: JarvisView): void {
    // undefined fields are dropped: $.state holds JSON data.
    const clean = JSON.parse(JSON.stringify(view)) as JarvisView
    // Talking to Jarvis again takes focus mode back from typing.
    if ((clean.phase === 'listening' || clean.phase === 'awake') && clean.phase !== this.view.phase) this.hasTypedSinceVoice = false
    this.view = clean
    this.hud?.setPhase(clean.phase)
    this.updateFocus()
    const engine = this.engine
    if (engine === undefined) return
    void engine.writeView(clean).catch(() => undefined)
    const line = statusLine(clean)
    if (line !== this.shownStatus) {
      this.shownStatus = line
      engine.status(line)
    }
  }
}
