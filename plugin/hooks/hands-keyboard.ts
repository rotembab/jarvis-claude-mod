// The air keyboard, the mod side. The hand helper draws an on-screen keyboard
// that the user taps in the air; what is tapped goes into a review box on that
// keyboard, and only three taps on its Insert key type the box into the window
// in front. This file is the mod's whole part in it: the handKeyboard plugin
// option (its only switch), /jarvis hands keyboard, the tool's three actions,
// the helper's events as toasts, and a status line. The protocol has no text
// field and no insert, send or clear action, so the mod never sees, sends or
// keeps what is typed: events carry enums and counts, and every event is
// rebuilt here from known keys so that a stray field cannot travel on.

import type { HandsCommandOutcome } from './hands'

// ---- Protocol: a mirror of plugin/protocol/hands.schema.json (v1), which is authoritative ----

export const KEYBOARD_PRESS = ['air', 'pinch', 'windows'] as const
export type KeyboardPress = (typeof KEYBOARD_PRESS)[number]
export const KEYBOARD_COMMIT = ['review', 'direct'] as const
export type KeyboardCommit = (typeof KEYBOARD_COMMIT)[number]
const KEYBOARD_LAYOUT = ['auto', 'en', 'he'] as const
const KEYBOARD_DOCK = ['top', 'bottom'] as const
const KEYBOARD_ENTER = ['twice', 'off'] as const

export type KeyboardAction = 'start' | 'practice' | 'stop' | 'recenter' | 'private' | 'public'
export type KeyboardSettingsBody = {
  enabled?: boolean
  press?: KeyboardPress
  commit?: KeyboardCommit
  layout?: (typeof KEYBOARD_LAYOUT)[number]
  size?: number
  reach?: number
  dock?: (typeof KEYBOARD_DOCK)[number]
  idleS?: number
  inject?: 'unicode' | 'vk'
  enter?: (typeof KEYBOARD_ENTER)[number]
}
export type KeyboardCommandBody = { action: KeyboardAction } | { action: 'configure'; settings: KeyboardSettingsBody }

const KEYBOARD_STATES = ['open', 'practice', 'closed'] as const
const KEYBOARD_PHASES = ['placing', 'warmup', 'typing'] as const
const KEYBOARD_LEVELS = ['ok', 'degraded', 'off'] as const
const KEYBOARD_LANGS = ['en', 'he'] as const
const KEYBOARD_HOLDS = ['blocked', 'password', 'covered', 'overlay', 'focus', 'yield', 'slow'] as const
const CLOSE_REASONS = [
  'command',
  'close_key',
  'fists',
  'idle',
  'paused',
  'desktop_locked',
  'runaway',
  'no_overlay',
  'camera',
  'disabled',
  'error',
  'input_blocked',
  'air_unreliable',
] as const
const REVIEW_STATES = ['composing', 'inserting', 'aborted'] as const
const INSERT_ABORTS = ['blocked', 'password', 'covered', 'overlay', 'focus', 'yield', 'stopped', 'timeout', 'failed'] as const
const INSERT_KINDS = ['text', 'enter'] as const
const INSERT_OUTCOMES = ['done', 'aborted'] as const

export type KeyboardCloseReason = (typeof CLOSE_REASONS)[number]
export type ReviewState = (typeof REVIEW_STATES)[number]
export type InsertAbort = (typeof INSERT_ABORTS)[number]
export type KeyboardPhase = (typeof KEYBOARD_PHASES)[number]
export type KeyboardLevel = (typeof KEYBOARD_LEVELS)[number]
export type KeyboardInsertResult = {
  kind: (typeof INSERT_KINDS)[number]
  outcome: (typeof INSERT_OUTCOMES)[number]
  sent: number
  of: number
  reason?: InsertAbort
}
export type KeyboardReview = { state: ReviewState; chars: number; insert?: KeyboardInsertResult }
export type HandsKeyboardEvent = {
  v: 1
  type: 'keyboard'
  state: (typeof KEYBOARD_STATES)[number]
  phase?: KeyboardPhase
  reason?: KeyboardCloseReason
  hold?: (typeof KEYBOARD_HOLDS)[number]
  lang?: (typeof KEYBOARD_LANGS)[number]
  press?: KeyboardPress
  level?: KeyboardLevel
  commit?: KeyboardCommit
  private?: boolean
  review?: KeyboardReview
  discarded?: number
  practice?: { hitRate: number; phantomsPerMin: number; recallIM?: number }
}

/** StatusResponse.keyboard: numbers and enums only. */
export type KeyboardStatus = {
  enabled: boolean
  state: string
  phase?: string
  press?: string
  level?: string
  airFps?: number
  airNoise?: number
  commit?: KeyboardCommit
  lang?: string
  hold?: string
  private?: boolean
  practiced: boolean
  phantomsPerMin?: number
  review?: { state: ReviewState; chars: number }
}

/** The box holds at most this many characters: the schema's maximum for every count. */
const COMPOSE_MAX = 200

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value)

const oneOf = <T extends string>(list: readonly T[], value: unknown): T | undefined => list.find(item => item === value)

/** An integer from `low` to `high`, else undefined. */
const whole = (value: unknown, low: number, high: number): number | undefined =>
  typeof value === 'number' && Number.isInteger(value) && value >= low && value <= high ? value : undefined

/** A finite number from `low` to `high`, else undefined. */
const between = (value: unknown, low: number, high: number): number | undefined =>
  typeof value === 'number' && Number.isFinite(value) && value >= low && value <= high ? value : undefined

function parseInsert(value: unknown): KeyboardInsertResult | undefined {
  if (!isRecord(value)) return undefined
  const kind = oneOf(INSERT_KINDS, value.kind)
  const outcome = oneOf(INSERT_OUTCOMES, value.outcome)
  const sent = whole(value.sent, 0, COMPOSE_MAX)
  const of = whole(value.of, 1, COMPOSE_MAX)
  if (kind === undefined || outcome === undefined || sent === undefined || of === undefined) return undefined
  const reason = oneOf(INSERT_ABORTS, value.reason)
  return { kind, outcome, sent, of, ...(reason === undefined ? {} : { reason }) }
}

function parseReview(value: unknown): KeyboardReview | undefined {
  if (!isRecord(value)) return undefined
  const state = oneOf(REVIEW_STATES, value.state)
  const chars = whole(value.chars, 0, COMPOSE_MAX)
  if (state === undefined || chars === undefined) return undefined
  const insert = parseInsert(value.insert)
  return { state, chars, ...(insert === undefined ? {} : { insert }) }
}

function parsePractice(value: unknown): HandsKeyboardEvent['practice'] {
  if (!isRecord(value)) return undefined
  const hitRate = between(value.hitRate, 0, 1)
  const phantomsPerMin = between(value.phantomsPerMin, 0, Number.POSITIVE_INFINITY)
  if (hitRate === undefined || phantomsPerMin === undefined) return undefined
  const recallIM = between(value.recallIM, 0, 1)
  return { hitRate, phantomsPerMin, ...(recallIM === undefined ? {} : { recallIM }) }
}

/**
 * A keyboard event from the helper, rebuilt from the keys the mod knows (RC4):
 * an invalid state makes it undefined, any other invalid field is dropped and
 * the event lives, and nothing else the helper wrote is passed on.
 */
export function parseKeyboardEvent(value: Record<string, unknown>): HandsKeyboardEvent | undefined {
  const state = oneOf(KEYBOARD_STATES, value.state)
  if (state === undefined) return undefined
  const phase = oneOf(KEYBOARD_PHASES, value.phase)
  const reason = oneOf(CLOSE_REASONS, value.reason)
  const hold = oneOf(KEYBOARD_HOLDS, value.hold)
  const lang = oneOf(KEYBOARD_LANGS, value.lang)
  const press = oneOf(KEYBOARD_PRESS, value.press)
  const level = oneOf(KEYBOARD_LEVELS, value.level)
  const commit = oneOf(KEYBOARD_COMMIT, value.commit)
  const review = parseReview(value.review)
  const discarded = whole(value.discarded, 0, COMPOSE_MAX)
  const practice = parsePractice(value.practice)
  return {
    v: 1,
    type: 'keyboard',
    state,
    ...(phase === undefined ? {} : { phase }),
    ...(reason === undefined ? {} : { reason }),
    ...(hold === undefined ? {} : { hold }),
    ...(lang === undefined ? {} : { lang }),
    ...(press === undefined ? {} : { press }),
    ...(level === undefined ? {} : { level }),
    ...(commit === undefined ? {} : { commit }),
    ...(typeof value.private === 'boolean' ? { private: value.private } : {}),
    ...(review === undefined ? {} : { review }),
    ...(discarded === undefined ? {} : { discarded }),
    ...(practice === undefined ? {} : { practice }),
  }
}

// ---- Words: every string the mod says about the keyboard is fixed here ----

/**
 * The fixed strings. {placeholders} are numbers (or the `why` words below); no
 * string is built from text the helper sent, so a word of the box, a key or a
 * window title cannot reach a toast or a reply (SR13, SR26).
 */
const TEXT = {
  off: 'The air keyboard is off. Turn it on in the Jarvis plugin settings (handKeyboard).',
  tooOld: 'The hand helper is too old for the air keyboard: run /jarvis setup hands.',
  notOpen: 'The air keyboard is not open.',
  // Toasts when it opens.
  openAir: 'Air keyboard open. Hold your hands over the keys, then tap each finger the strip names.',
  openPinch: 'Air keyboard open. Hold your hands over the keys, then pinch each finger once.',
  openWindows: "Windows' own on-screen keyboard is open. Jarvis' keyboard safeguards do not apply to it.",
  openPractice: 'Air keyboard practice: nothing you type is sent anywhere.',
  byClaudeAir: 'Claude opened the air keyboard. It does nothing until you tap each finger the strip names.',
  byClaudePinch: 'Claude opened the air keyboard. It does nothing until you pinch each finger once.',
  // Toasts when it closes: a reason's own words, then the pointer line, then the count.
  closedPlain: 'Air keyboard closed.',
  closedRunaway: 'Air keyboard closed: too many keys at once.',
  closedDesktopLocked: 'Air keyboard closed: the screen was locked.',
  closedIdle: 'Air keyboard closed: nobody was using it.',
  closedNoOverlay: 'Air keyboard closed: it could not be shown.',
  closedCamera: 'Air keyboard closed: the camera stopped.',
  closedError: 'Air keyboard closed after an internal error; hand control carries on.',
  closedInputBlocked: 'Air keyboard closed: Windows would not take the keys (is the window running as administrator?).',
  closedAirUnreliable: 'Air keyboard closed: the camera or hand tracking was too unsteady for tapping.',
  pointerBack: ' Lower your hands for a second to give the pointer back.',
  discardedOne: ' {discarded} typed character was thrown away.',
  discardedMany: ' {discarded} typed characters were thrown away.',
  practiceAir: 'Practice done: {hitRate}% of keys right, {phantomsPerMin} false taps a minute, {recallIM}% of index and middle taps seen.',
  practiceAirNoRecall: 'Practice done: {hitRate}% of keys right, {phantomsPerMin} false taps a minute.',
  practicePinch: 'Practice done: {hitRate}% of keys right, {phantomsPerMin} false presses a minute.',
  // An Insert or Send that stopped.
  abortedText: 'Air keyboard: typed {sent} of {of} characters, then stopped ({why}). The rest is still in the box.',
  abortedTextNoWhy: 'Air keyboard: typed {sent} of {of} characters, then stopped. The rest is still in the box.',
  abortedEnter: 'Air keyboard: Enter was not pressed ({why}).',
  abortedEnterNoWhy: 'Air keyboard: Enter was not pressed.',
  // What /jarvis hands keyboard answers.
  openingAir:
    'Opening the air keyboard. Hold your hands over the keys, then tap each finger the strip names. What you tap goes into a review box; nothing reaches another window until you tap Insert three times.',
  openingPinchReview:
    'Opening the air keyboard. Hold your hands over the keys, then pinch each finger once. What you type goes into a review box; nothing reaches another window until you press Insert three times.',
  openingPinchDirect:
    'Opening the air keyboard. Hold your hands over the keys, then pinch each finger once. Direct typing is on: what you type goes straight into the window in front.',
  openingWindows: "Opening Windows' own on-screen keyboard. Jarvis' keyboard safeguards do not apply to it.",
  openingPractice: 'Opening the air keyboard in practice mode: nothing you tap is sent anywhere.',
  closing: 'Closing the air keyboard. Anything in its review box is thrown away; nothing is typed.',
  recentering: 'Recentering the air keyboard on your hands.',
  privateOn: 'Air keyboard private mode is on: the box and the key highlights are hidden on screen.',
  privateOff: 'Air keyboard private mode is off: the box and the key highlights show again.',
  // What the tool answers the model: the same facts, from the user's side.
  toolOpenAir:
    "The air keyboard is opening on the user's screen. It does nothing until the user taps each finger the strip names. What they tap goes into a review box on the keyboard; only the user can insert it into a window, and Jarvis cannot read, insert or send it.",
  toolOpenPinchReview:
    "The air keyboard is opening on the user's screen. It does nothing until the user pinches each finger once. What they type goes into a review box on the keyboard; only the user can insert it into a window, and Jarvis cannot read, insert or send it.",
  toolOpenPinchDirect:
    "The air keyboard is opening on the user's screen. It does nothing until the user pinches each finger once. Direct typing is on, so what they type goes straight into the window in front; Jarvis cannot read, insert or send it.",
  toolOpenWindows:
    "Windows' own on-screen keyboard is opening on the user's screen. Jarvis' keyboard safeguards do not apply to it, and Jarvis cannot type for the user.",
  toolPractice: "The air keyboard practice is opening on the user's screen. Nothing they tap is sent anywhere.",
  toolClosing: 'The air keyboard is closing. Anything in its review box is thrown away; nothing is typed.',
  // The status.
  statusReady:
    'Air keyboard: on. /jarvis hands keyboard opens it; /jarvis hands keyboard practice tries it first; /jarvis hands keyboard status shows its settings.',
  levelDegraded: 'Air keyboard: the air tap is less sure right now; tap a little firmer.',
  levelOff: 'Air keyboard: the air tap is off (the camera or hand tracking is too unsteady); it switched to pinch.',
} as const satisfies Record<string, string>

export const KEYBOARD_TEXT: Readonly<Record<keyof typeof TEXT, string>> = TEXT

/** Why an Insert stopped, in words ({why} of the aborted toast). */
const WHY: Readonly<Record<InsertAbort, string>> = {
  focus: 'the window changed',
  yield: 'you used the keyboard or mouse',
  blocked: 'that window cannot be typed into',
  password: 'that looks like a password box',
  covered: 'the screen is covered',
  overlay: 'the keyboard could not be drawn',
  stopped: 'you stopped it',
  timeout: 'it took too long',
  failed: 'Windows would not take the keys',
}

/** A close's own toast, for the reasons that have one. */
const CLOSE_TEXT: Partial<Record<KeyboardCloseReason, string>> = {
  runaway: TEXT.closedRunaway,
  desktop_locked: TEXT.closedDesktopLocked,
  idle: TEXT.closedIdle,
  no_overlay: TEXT.closedNoOverlay,
  camera: TEXT.closedCamera,
  error: TEXT.closedError,
  input_blocked: TEXT.closedInputBlocked,
  air_unreliable: TEXT.closedAirUnreliable,
}

/** The pointer comes back when the hands drop, unless the camera or hand control itself stopped, or the mod closed it. */
const NO_POINTER_LINE: ReadonlySet<KeyboardCloseReason | undefined> = new Set<KeyboardCloseReason | undefined>(['paused', 'camera', 'command', 'disabled', undefined])

const PHASE_WORDS: Readonly<Record<KeyboardPhase, string>> = { placing: 'finding your hands', warmup: 'warm-up', typing: 'typing' }

/** Fills {name} with a number or a vetted word; there is no other way text gets into a string here. */
function fill(template: string, values: Readonly<Record<string, number | string>>): string {
  return template.replace(/\{([a-zA-Z]+)\}/g, (_, key: string) => String(values[key] ?? ''))
}

/** One decimal, without a trailing zero: 0 -> "0", 2.34 -> "2.3". */
const oneDecimal = (value: number): string => String(Math.round(value * 10) / 10)

const KEYBOARD_HELP_LINES = [
  'Air keyboard (needs the handKeyboard plugin setting, and hand control on):',
  '/jarvis hands keyboard [on]           open it; tap each finger the strip names, then tap keys in the air',
  '/jarvis hands keyboard practice       try it first: nothing you tap is sent anywhere',
  '/jarvis hands keyboard off            close it; what is in its box is thrown away',
  '/jarvis hands keyboard recenter       put the keys back under your hands',
  '/jarvis hands keyboard private|public hide or show the box and the key highlights on screen',
  '/jarvis hands keyboard status         whether it is on, its settings, and how many characters wait in the box',
  '/jarvis hands keyboard press <air|pinch|windows|default>   how a key is pressed (air: tap; pinch: thumb to finger)',
  '/jarvis hands keyboard commit <review|direct|default>      review: taps fill a box first (the only choice for air)',
  '/jarvis hands keyboard layout <auto|en|he|default>   dock <top|bottom|default>   enter <twice|off|default>',
  '/jarvis hands keyboard size <0.6-1.6|default>   reach <0.8-1.5|default>',
  'What you tap goes into a review box. Nothing reaches another window until you tap Insert three times; Jarvis cannot read the box or type it for you.',
]
export const KEYBOARD_HELP: string = KEYBOARD_HELP_LINES.join('\n')

// ---- Settings: what /jarvis hands keyboard keeps, and sends with `configure` ----

type SettingName = 'press' | 'commit' | 'layout' | 'size' | 'reach' | 'dock' | 'enter'

type ChoiceSetting = { kind: 'choice'; store: string; label: string; article: string; noun: string; values: readonly string[]; default: string }
type NumberSetting = { kind: 'number'; store: string; label: string; min: number; max: number; default: number }

/**
 * One line each. The ranges and words are the helper's (keyboard/settings.py,
 * which is authoritative and refuses what is outside); the mod checks first so
 * the user hears the range and a refused value is never kept. The press
 * method's default is the plugin option, so it has none here.
 */
const SETTINGS = {
  press: { kind: 'choice', store: 'handsKeyboardPress', label: 'press method', article: 'a', noun: 'press method', values: KEYBOARD_PRESS, default: '' },
  commit: { kind: 'choice', store: 'handsKeyboardCommit', label: 'commit mode', article: 'a', noun: 'commit mode', values: KEYBOARD_COMMIT, default: 'review' },
  layout: { kind: 'choice', store: 'handsKeyboardLayout', label: 'layout', article: 'a', noun: 'layout', values: KEYBOARD_LAYOUT, default: 'auto' },
  size: { kind: 'number', store: 'handsKeyboardSize', label: 'size', min: 0.6, max: 1.6, default: 1 },
  reach: { kind: 'number', store: 'handsKeyboardReach', label: 'reach', min: 0.8, max: 1.5, default: 1 },
  dock: { kind: 'choice', store: 'handsKeyboardDock', label: 'dock', article: 'a', noun: 'dock position', values: KEYBOARD_DOCK, default: 'top' },
  enter: { kind: 'choice', store: 'handsKeyboardEnter', label: 'Enter key', article: 'an', noun: 'Enter key choice', values: KEYBOARD_ENTER, default: 'twice' },
} as const satisfies Record<SettingName, ChoiceSetting | NumberSetting>

const SETTING_NAMES = Object.keys(SETTINGS) as SettingName[]

/** What the helper refuses together (keyboard/settings.py), as it words it. */
const AIR_NEEDS_REVIEW = 'The air-tap method only works with the review box (commit: review).'
const DIRECT_NEEDS_PINCH = 'Direct typing works only with the pinch method. Use press pinch first.'

const PRESS_NOTE: Readonly<Record<KeyboardPress, string>> = {
  air: 'tap a finger in the air; letters go to a review box first',
  pinch: 'pinch your thumb to the finger over a key',
  windows: "Windows' own on-screen keyboard opens instead, and Jarvis' keyboard safeguards do not apply to it",
}
const COMMIT_NOTE: Readonly<Record<KeyboardCommit, string>> = {
  review: 'what you tap goes into the box, and only three taps on Insert type it into a window',
  direct: 'pinch presses type straight into the window in front; practice first (/jarvis hands keyboard practice)',
}
const ENTER_NOTE: Readonly<Record<(typeof KEYBOARD_ENTER)[number], string>> = {
  twice: 'Send exists, three guarded taps right after an Insert',
  off: 'there is no Send key',
}

/** What is kept, each value valid; anything else in the store is ignored. */
type Stored = {
  press?: KeyboardPress
  commit?: KeyboardCommit
  layout?: (typeof KEYBOARD_LAYOUT)[number]
  size?: number
  reach?: number
  dock?: (typeof KEYBOARD_DOCK)[number]
  enter?: (typeof KEYBOARD_ENTER)[number]
}

/** The words after a setting's name: "default", or a value of the setting; undefined when it is neither. */
const DECIMAL = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$/
const tidy = (value: number): number => Math.round(value * 1000) / 1000

const clip = (text: string, length = 40): string => text.slice(0, length)

/** A helper's message as one clean line of at most `length` characters (a refusal is a fixed sentence, but nothing it adds may swell or break a reply). */
const cleanMessage = (message: string, length = 200): string => message.replace(/[\u0000-\u001f\u007f]+/g, ' ').replace(/\s+/g, ' ').trim().slice(0, length)

/** A message without its closing full stop, which the sentence around it supplies. */
const plainMessage = (message: string): string => cleanMessage(message).replace(/[\s.]+$/, '')

// ---- The keyboard ----

export type KeyboardDeps = {
  helper: {
    readonly isRunning: boolean
    capabilities: () => readonly string[]
    send: (name: 'keyboard', body: KeyboardCommandBody) => Promise<HandsCommandOutcome>
  }
  engine: {
    storeGet(key: string): Promise<unknown>
    storeSet(key: string, value: unknown): Promise<void>
    storeDelete(key: string): Promise<void>
    toast(text: string, options?: { timeoutMs?: number }): void
    /** A line in the debug log only. */
    debug?(text: string): void
  }
  /** The plugin options (hands.ts readHandsSettings): the master switch and the press method's default. */
  userConfig: () => { keyboard: boolean; keyboardPress: KeyboardPress }
  /** Hands.notRunning: why a command that needs the running hand helper cannot go; undefined when it can. */
  notRunning: () => Promise<string | undefined>
}

type Pushed = { sent: boolean; failure?: { code: string; message: string } }

/** A close toast stays up a little longer than a plain one: it says something went wrong or was thrown away. */
const CLOSE_TOAST_MS = 8000

export class HandsKeyboard {
  /** The last state the helper reported; undefined until it has said one (and again after a helper restart). */
  private state: HandsKeyboardEvent['state'] | undefined
  private phase: KeyboardPhase | undefined
  private level: KeyboardLevel | undefined
  /** The press method of this session as the helper reports it (the active one: it changes when the air tap falls back to pinch). */
  private press: KeyboardPress | undefined
  /** The box's last count within this session; events that do not name it leave it standing. */
  private review: { state: ReviewState; chars: number } | undefined
  /** The user did not ask for the open that is under way: the tool did, and the toast says so. */
  private isOpenedByClaude = false
  /** A `configure` went out to this helper: a later change of the option must reach it too. */
  private isConfigured = false
  /** Settings go out one at a time, in the order they changed. */
  private queue: Promise<unknown> = Promise.resolve()

  constructor(private readonly deps: KeyboardDeps) {}

  // ---- Settings sent to the helper ----

  /**
   * Sends the keyboard settings: `enabled` from the plugin option only, the rest
   * from what /jarvis hands keyboard kept. Nothing goes to a helper without the
   * keyboard (an older one, whose hello has no `keyboard`), nor while the
   * option is off, nothing is kept and none was sent before: such a helper
   * starts with the keyboard off. Called after the helper's own config.
   */
  async sync(): Promise<void> {
    await this.push()
  }

  private push(): Promise<Pushed> {
    const run = this.queue.then(() => this.pushNow())
    this.queue = run
    return run
  }

  private async pushNow(): Promise<Pushed> {
    if (!this.hasKeyboard()) return { sent: false }
    const config = this.deps.userConfig()
    const stored = await this.stored()
    if (!config.keyboard && Object.keys(stored).length === 0 && !this.isConfigured) return { sent: false }
    const { enabled, ...settings } = this.effective(config, stored)
    const outcome = await this.deps.helper.send('keyboard', { action: 'configure', settings: { enabled, ...settings } })
    if (outcome.ok) {
      this.isConfigured = true
      return { sent: true }
    }
    this.deps.engine.debug?.(`jarvis: air keyboard settings not applied: ${cleanMessage(outcome.message)}`)
    return { sent: false, failure: { code: outcome.code, message: outcome.message } }
  }

  /** The helper can take keyboard commands: it runs and said `keyboard` in its hello. */
  private hasKeyboard(): boolean {
    return this.deps.helper.isRunning && this.deps.helper.capabilities().includes('keyboard')
  }

  /** What is kept, each value checked. */
  private async stored(): Promise<Stored> {
    const read = (name: SettingName): Promise<unknown> => this.deps.engine.storeGet(SETTINGS[name].store).catch(() => undefined)
    const choice = <T extends string>(name: SettingName, values: readonly T[], raw: unknown): T | undefined => (SETTINGS[name].kind === 'choice' ? oneOf(values, raw) : undefined)
    const count = (name: 'size' | 'reach', raw: unknown): number | undefined => between(raw, SETTINGS[name].min, SETTINGS[name].max)
    const [press, commit, layout, size, reach, dock, enter] = await Promise.all(SETTING_NAMES.map(read))
    const stored: Stored = {}
    const set = <K extends keyof Stored>(key: K, value: Stored[K]): void => {
      if (value !== undefined) stored[key] = value
    }
    set('press', choice('press', KEYBOARD_PRESS, press))
    set('commit', choice('commit', KEYBOARD_COMMIT, commit))
    set('layout', choice('layout', KEYBOARD_LAYOUT, layout))
    set('size', count('size', size))
    set('reach', count('reach', reach))
    set('dock', choice('dock', KEYBOARD_DOCK, dock))
    set('enter', choice('enter', KEYBOARD_ENTER, enter))
    return stored
  }

  /**
   * Every setting the helper should hold, in full, so that clearing a kept
   * choice sends the default again. `enabled` is the plugin option's and
   * nothing else's. Direct typing needs the pinch method (the helper refuses
   * the rest): under another method the stricter review box is sent.
   */
  private effective(config: ReturnType<KeyboardDeps['userConfig']>, stored: Stored): Required<Pick<KeyboardSettingsBody, 'enabled' | 'press' | 'commit' | 'layout' | 'size' | 'reach' | 'dock' | 'enter'>> {
    const press = stored.press ?? config.keyboardPress
    const commit = stored.commit === 'direct' && press !== 'pinch' ? 'review' : (stored.commit ?? 'review')
    return {
      enabled: config.keyboard,
      press,
      commit,
      layout: stored.layout ?? 'auto',
      size: stored.size ?? SETTINGS.size.default,
      reach: stored.reach ?? SETTINGS.reach.default,
      dock: stored.dock ?? 'top',
      enter: stored.enter ?? 'twice',
    }
  }

  // ---- Commands ----

  /** `/jarvis hands keyboard <args>`; the caller catches what it throws. */
  async run(args: readonly string[]): Promise<string> {
    const [sub = '', ...rest] = args
    const word = sub.toLowerCase()
    if (word === 'help') return KEYBOARD_HELP
    // Closing is always allowed: it throws the box away and types nothing.
    if (word === 'off' || word === 'stop') return await this.close('user')
    const isSetting = SETTING_NAMES.includes(word as SettingName)
    const isPlain = ['', 'on', 'practice', 'recenter', 'private', 'public', 'status'].includes(word)
    if (!isSetting && !isPlain) return `Unknown subcommand "${clip(sub)}" for /jarvis hands keyboard.\n\n${KEYBOARD_HELP}`
    if (!this.deps.userConfig().keyboard) return TEXT.off
    if (isSetting) return await this.choose(word as SettingName, rest)
    if (rest.length > 0) return `Unknown option "${clip(rest[0] ?? '')}" for /jarvis hands keyboard ${word}; it takes none.`
    switch (word) {
      case '':
      case 'on':
        return await this.open('start', 'user')
      case 'practice':
        return await this.open('practice', 'user')
      case 'status':
        return await this.statusText()
      default:
        return await this.simple(word as 'recenter' | 'private' | 'public')
    }
  }

  /** The tool's three actions: the same as the commands, answered to the model. */
  async runTool(action: 'keyboard' | 'keyboard_practice' | 'keyboard_off'): Promise<string> {
    if (action === 'keyboard_off') return await this.close('claude')
    return await this.open(action === 'keyboard' ? 'start' : 'practice', 'claude')
  }

  /** Opens the keyboard, or its practice, for the user. */
  private async open(action: 'start' | 'practice', by: 'user' | 'claude'): Promise<string> {
    const config = this.deps.userConfig()
    if (!config.keyboard) return TEXT.off
    const notRunning = await this.deps.notRunning()
    if (notRunning !== undefined) return notRunning
    if (!this.hasKeyboard()) return TEXT.tooOld
    // The settings first, so that the helper has the option as it is now.
    const pushed = await this.push()
    if (pushed.failure !== undefined) {
      const { code, message } = pushed.failure
      if (code === 'bad_request') {
        return `The hand helper refused the air keyboard settings (${plainMessage(message)}). /jarvis setup hands updates an older hand helper. Nothing was opened.`
      }
      return `Could not open the air keyboard: ${cleanMessage(message)}`
    }
    const settings = this.effective(config, await this.stored())
    // The toast of an open the tool asked for says so (the event, which may come before the answer, reads this).
    this.isOpenedByClaude = by === 'claude' && action === 'start'
    const outcome = await this.deps.helper.send('keyboard', { action })
    if (!outcome.ok) {
      this.isOpenedByClaude = false
      return this.failure(outcome, 'open the air keyboard')
    }
    if (action === 'practice') return by === 'claude' ? TEXT.toolPractice : TEXT.openingPractice
    if (settings.press === 'windows') return by === 'claude' ? TEXT.toolOpenWindows : TEXT.openingWindows
    const isDirect = settings.press === 'pinch' && settings.commit === 'direct'
    if (settings.press === 'pinch') {
      if (by === 'claude') return isDirect ? TEXT.toolOpenPinchDirect : TEXT.toolOpenPinchReview
      return isDirect ? TEXT.openingPinchDirect : TEXT.openingPinchReview
    }
    return by === 'claude' ? TEXT.toolOpenAir : TEXT.openingAir
  }

  /** Closes it. Needs no setting and no option: the box is thrown away and nothing is typed. */
  private async close(by: 'user' | 'claude'): Promise<string> {
    if (!this.hasKeyboard()) return TEXT.notOpen
    const outcome = await this.deps.helper.send('keyboard', { action: 'stop' })
    if (!outcome.ok) return this.failure(outcome, 'close the air keyboard')
    return by === 'claude' ? TEXT.toolClosing : TEXT.closing
  }

  /** recenter, private and public: one command each. */
  private async simple(action: 'recenter' | 'private' | 'public'): Promise<string> {
    const notRunning = await this.deps.notRunning()
    if (notRunning !== undefined) return notRunning
    if (!this.hasKeyboard()) return TEXT.tooOld
    const outcome = await this.deps.helper.send('keyboard', { action })
    if (!outcome.ok) return this.failure(outcome, action === 'recenter' ? 'recenter the air keyboard' : 'change the air keyboard\'s private mode')
    return action === 'recenter' ? TEXT.recentering : action === 'private' ? TEXT.privateOn : TEXT.privateOff
  }

  /** A refusal is the helper's own fixed sentence (keyboard/controller.py); any other failure is the mod's. */
  private failure(outcome: Extract<HandsCommandOutcome, { ok: false }>, what: string): string {
    return outcome.code === 'bad_request' ? cleanMessage(outcome.message, 240) : `Could not ${what}: ${cleanMessage(outcome.message)}`
  }

  /** `press`, `commit`, `layout`, `size`, `reach`, `dock`, `enter`: a value to keep, "default" to clear it, or nothing to read it. */
  private async choose(name: SettingName, args: readonly string[]): Promise<string> {
    const setting: ChoiceSetting | NumberSetting = SETTINGS[name]
    const config = this.deps.userConfig()
    const stored = await this.stored()
    const [word, ...extra] = args
    if (word === undefined) return this.describeSetting(name, stored, config)
    if (extra.length > 0) return `Use /jarvis hands keyboard ${name} ${this.choices(name)}.`
    const parsed = this.parseSettingWord(name, word)
    if (typeof parsed === 'string') return parsed
    const { value } = parsed
    // What the helper refuses together, said before anything is kept.
    const press = name === 'press' ? (value ?? config.keyboardPress) : (stored.press ?? config.keyboardPress)
    if (name === 'press' && press === 'air' && stored.commit === 'direct') return AIR_NEEDS_REVIEW
    if (name === 'commit' && value === 'direct' && press !== 'pinch') return DIRECT_NEEDS_PINCH
    // A running helper that never heard of the keyboard would not take it; with none running the choice waits for the next start.
    if (this.deps.helper.isRunning && !this.hasKeyboard()) return TEXT.tooOld
    const previous = await this.deps.engine.storeGet(setting.store).catch(() => undefined)
    if (value === undefined) await this.deps.engine.storeDelete(setting.store)
    else await this.deps.engine.storeSet(setting.store, value)
    const pushed = await this.push()
    if (pushed.failure !== undefined) {
      // Whatever the helper did not take is not kept: one bad value would otherwise refuse every later sync.
      if (previous === undefined) await this.deps.engine.storeDelete(setting.store)
      else await this.deps.engine.storeSet(setting.store, previous)
      const { code, message } = pushed.failure
      return `The hand helper ${code === 'bad_request' ? 'refused it' : 'could not apply it'}: ${plainMessage(message)}. Nothing was changed.`
    }
    const ending = pushed.sent ? '. It applies the next time the keyboard opens.' : '; it applies when hand control starts.'
    return `${this.statement(name, value, config)}${ending}`
  }

  /** "air" / 1.2 / "default" (undefined here) from the words typed; a sentence when they are not a value. */
  private parseSettingWord(name: SettingName, word: string): { value: string | number | undefined } | string {
    const setting: ChoiceSetting | NumberSetting = SETTINGS[name]
    if (word.toLowerCase() === 'default') return { value: undefined }
    if (setting.kind === 'choice') {
      const found = setting.values.find(one => one === word.toLowerCase())
      if (found !== undefined) return { value: found }
      return `"${clip(word)}" is not ${setting.article} ${setting.noun}. Use /jarvis hands keyboard ${name} ${this.choices(name)}.`
    }
    const range = `${setting.min} to ${setting.max} (default ${setting.default})`
    if (!DECIMAL.test(word.trim())) {
      const example = Math.round((setting.default + (setting.max - setting.default) / 4) * 100) / 100
      return `"${clip(word)}" is not a number. The keyboard ${name} takes a number from ${range}, or "default": /jarvis hands keyboard ${name} ${example}.`
    }
    const number = Number(word.trim())
    if (!Number.isFinite(number) || number < setting.min || number > setting.max) {
      return `${clip(word.trim())} is outside the range for the keyboard ${name}: ${range}. Nothing was changed.`
    }
    return { value: tidy(number) }
  }

  /** "<air|pinch|windows|default>" or "<0.6 to 1.6|default>". */
  private choices(name: SettingName): string {
    const setting: ChoiceSetting | NumberSetting = SETTINGS[name]
    return `<${setting.kind === 'choice' ? setting.values.join('|') : `${setting.min} to ${setting.max}`}|default>`
  }

  /** What was just kept, or cleared, in words (without its ending). */
  private statement(name: SettingName, value: string | number | undefined, config: ReturnType<KeyboardDeps['userConfig']>): string {
    const setting: ChoiceSetting | NumberSetting = SETTINGS[name]
    const head = `Air keyboard ${setting.label}`
    if (value === undefined) {
      if (name === 'press') return `${head} is back to the plugin setting, ${config.keyboardPress}`
      return `${head} is back to its default, ${setting.default}`
    }
    if (name === 'press') return `${head} is now ${value}: ${PRESS_NOTE[value as KeyboardPress]}`
    if (name === 'commit') return `${head} is now ${value}: ${COMMIT_NOTE[value as KeyboardCommit]}`
    if (name === 'enter') return `${head} is now ${value}: ${ENTER_NOTE[value as (typeof KEYBOARD_ENTER)[number]]}`
    if (setting.kind === 'number') return `${head} is now ${value} (range ${setting.min} to ${setting.max})`
    return `${head} is now ${value}`
  }

  /** A setting named alone: its value, its default, and how to change it. */
  private describeSetting(name: SettingName, stored: Stored, config: ReturnType<KeyboardDeps['userConfig']>): string {
    const setting: ChoiceSetting | NumberSetting = SETTINGS[name]
    const now = this.effective(config, stored)
    const head = `Air keyboard ${setting.label} is ${now[name]}`
    const change = `Change it with /jarvis hands keyboard ${name} ${this.choices(name)}.`
    if (name === 'press') return `${head} (the plugin setting says ${config.keyboardPress}). ${change}`
    if (setting.kind === 'number') return `${head} (default ${setting.default}, range ${setting.min} to ${setting.max}). ${change}`
    return `${head} (default ${setting.default}). ${change}`
  }

  /** `/jarvis hands keyboard status`: the state lines and the settings in force. */
  private async statusText(): Promise<string> {
    const now = this.effective(this.deps.userConfig(), await this.stored())
    const settings = `Air keyboard settings: press ${now.press}, commit ${now.commit}, layout ${now.layout}, size ${now.size}, reach ${now.reach}, dock ${now.dock}, enter ${now.enter}. /jarvis hands keyboard help lists how to change them.`
    return [...this.statusLines(), settings].join('\n')
  }

  // ---- Events ----

  /** The helper's keyboard event: toasts with fixed words, and what `/jarvis hands` says. */
  onEvent(event: HandsKeyboardEvent): void {
    const was = this.state
    if (event.state === 'closed') {
      // The same close said twice is said once.
      if (was !== 'closed') this.toastClosed(event)
      this.state = 'closed'
      this.phase = undefined
      this.level = undefined
      this.review = undefined
      this.press = undefined
      this.isOpenedByClaude = false
      return
    }
    const isNew = was !== event.state
    this.state = event.state
    this.phase = event.phase
    this.level = event.level
    this.press = event.press ?? (isNew ? undefined : this.press)
    // A new session starts with no box; one that names no count leaves the last standing.
    if (isNew) this.review = undefined
    if (event.review !== undefined) this.review = { state: event.review.state, chars: event.review.chars }
    if (isNew) this.toastOpened(event)
    const insert = event.review?.insert
    if (insert?.outcome === 'aborted') this.toastAborted(insert)
  }

  private toastOpened(event: HandsKeyboardEvent): void {
    const press = event.press ?? this.deps.userConfig().keyboardPress
    const isByClaude = this.isOpenedByClaude && event.state === 'open'
    if (event.state === 'open') this.isOpenedByClaude = false
    if (event.state === 'practice') this.deps.engine.toast(TEXT.openPractice)
    else if (press === 'windows') this.deps.engine.toast(TEXT.openWindows)
    else if (isByClaude) this.deps.engine.toast(press === 'pinch' ? TEXT.byClaudePinch : TEXT.byClaudeAir)
    else this.deps.engine.toast(press === 'pinch' ? TEXT.openPinch : TEXT.openAir)
  }

  private toastClosed(event: HandsKeyboardEvent): void {
    const reason = oneOf(CLOSE_REASONS, event.reason)
    const own = reason === undefined ? undefined : CLOSE_TEXT[reason]
    const practice = event.practice === undefined ? undefined : this.practiceText(event.practice)
    const discarded = whole(event.discarded, 1, COMPOSE_MAX)
    const said = [practice, own].filter((text): text is string => text !== undefined).join(' ')
    // Nothing to say: a close the user made, with nothing thrown away.
    if (said === '' && discarded === undefined) return
    const base = said === '' ? TEXT.closedPlain : said
    const pointer = NO_POINTER_LINE.has(reason) ? '' : TEXT.pointerBack
    const thrown = discarded === undefined ? '' : fill(discarded === 1 ? TEXT.discardedOne : TEXT.discardedMany, { discarded })
    this.deps.engine.toast(`${base}${pointer}${thrown}`, { timeoutMs: CLOSE_TOAST_MS })
  }

  /** The practice's numbers, in the words of the press method it was (the closed event does not say). */
  private practiceText(practice: NonNullable<HandsKeyboardEvent['practice']>): string {
    const hitRate = Math.round(practice.hitRate * 100)
    const phantomsPerMin = oneDecimal(practice.phantomsPerMin)
    if (this.press === 'pinch') return fill(TEXT.practicePinch, { hitRate, phantomsPerMin })
    if (practice.recallIM === undefined) return fill(TEXT.practiceAirNoRecall, { hitRate, phantomsPerMin })
    return fill(TEXT.practiceAir, { hitRate, phantomsPerMin, recallIM: Math.round(practice.recallIM * 100) })
  }

  private toastAborted(insert: KeyboardInsertResult): void {
    const sent = whole(insert.sent, 0, COMPOSE_MAX) ?? 0
    const of = whole(insert.of, 1, COMPOSE_MAX) ?? 1
    const reason = oneOf(INSERT_ABORTS, insert.reason)
    const why = reason === undefined ? undefined : WHY[reason]
    const template =
      insert.kind === 'enter'
        ? why === undefined ? TEXT.abortedEnterNoWhy : TEXT.abortedEnter
        : why === undefined ? TEXT.abortedTextNoWhy : TEXT.abortedText
    this.deps.engine.toast(fill(template, { sent, of, why: why ?? '' }), { timeoutMs: CLOSE_TOAST_MS })
  }

  // ---- Status ----

  /** What `/jarvis hands` adds: whether the keyboard is on and, while it is open, its phase, the air tap's level and the box's count. Nothing when the option is off. */
  statusLines(): string[] {
    if (!this.deps.userConfig().keyboard) return []
    if (this.state !== 'open' && this.state !== 'practice') return [TEXT.statusReady]
    const phase = this.phase === undefined ? '' : ` (${PHASE_WORDS[this.phase]})`
    const lines = [this.state === 'practice' ? `Air keyboard: practice${phase}. Nothing is typed anywhere.` : `Air keyboard: open${phase}.`]
    if (this.level === 'degraded') lines.push(TEXT.levelDegraded)
    if (this.level === 'off') lines.push(TEXT.levelOff)
    if (this.review !== undefined) lines.push(`Air keyboard review box: ${this.review.chars} ${this.review.chars === 1 ? 'character' : 'characters'} waiting.`)
    return lines
  }

  /** The helper restarted: what it reported is gone with it, and it has been sent nothing yet. */
  reset(): void {
    this.state = undefined
    this.phase = undefined
    this.level = undefined
    this.press = undefined
    this.review = undefined
    this.isOpenedByClaude = false
    this.isConfigured = false
  }
}
