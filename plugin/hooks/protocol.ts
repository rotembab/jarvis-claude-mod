// TypeScript mirror of plugin/protocol/schema.json (protocol v1).
// schema.json is authoritative: keep these types in step with it by hand.

export const PROTOCOL_VERSION = 1 as const

export type HelperState =
  | 'starting'
  | 'sleeping'
  | 'listening'
  | 'transcribing'
  | 'speaking'
  /** A few seconds after a reply: the user can follow up without the wake word. */
  | 'awake'
  | 'error'
export type HelperPlatform = 'windows' | 'macos' | 'linux'
export type UtteranceSource = 'ptt' | 'command' | 'wake'
/** What interrupts Jarvis by voice: any speech, only the wake word, or nothing. */
export type BargeInMode = 'speech' | 'wake' | 'off'
export type EchoCancelState = 'on' | 'off' | 'loading' | 'unavailable'

export type ErrorCode =
  | 'already_running'
  | 'fish_key_missing'
  | 'fish_auth_failed'
  | 'fish_unreachable'
  | 'local_voice_failed'
  | 'mic_blocked'
  | 'mic_in_use'
  | 'no_input_device'
  | 'no_output_device'
  | 'stt_model_missing'
  | 'stt_failed'
  | 'ptt_unavailable'
  | 'wake_unavailable'
  | 'aec_unavailable'
  | 'bad_request'
  | 'unauthorized'
  | 'internal'

type Envelope<T extends string> = { v: typeof PROTOCOL_VERSION; type: T }

// ---- Helper -> mod events (one JSON object per stdout line) ----

export type HelloEvent = Envelope<'hello'> & {
  port: number
  pid: number
  platform: HelperPlatform
  version: string
  capabilities: string[]
}

export type StateEvent = Envelope<'state'> & { state: HelperState }

export type LevelEvent = Envelope<'level'> & { mic: number; out: number }

export type UtteranceEvent = Envelope<'utterance'> & {
  id: string
  text: string
  source: UtteranceSource
  durationMs: number
  language?: string
}

export type SpeechStartedEvent = Envelope<'speech_started'> & { replyId: string }

export type SpeechDoneEvent = Envelope<'speech_done'> & {
  replyId: string
  interrupted: boolean
  spokenText: string
}

export type BargeInEvent = Envelope<'barge_in'> & { replyId?: string; spokenText: string }

export type ErrorEvent = Envelope<'error'> & {
  code: ErrorCode
  message: string
  hint?: string
  fatal: boolean
}

export type ReadyEvent = Envelope<'ready'> & {
  sttModel: string
  sttDevice: 'cuda' | 'cpu'
  voiceId?: string
  pttKey: string
  /** "Hey Jarvis" while the wake word is loaded and switched on; "Jarvis" while plain "Jarvis" works too. */
  wakePhrase?: string
  bargeIn?: BargeInMode
}

export type HelperEvent =
  | HelloEvent
  | StateEvent
  | LevelEvent
  | UtteranceEvent
  | SpeechStartedEvent
  | SpeechDoneEvent
  | BargeInEvent
  | ErrorEvent
  | ReadyEvent

export type HelperEventType = HelperEvent['type']

// ---- Mod -> helper commands (POST /v1/<name>, JSON body) ----

export type SpeakCommand = { replyId: string; seq: number; text: string; final: boolean }
export type StopCommand = { reason?: string }
export type ListenCommand = { action: 'start' | 'stop' }
export type DesktopAction = 'open' | 'focus' | 'media' | 'volume' | 'screenshot' | 'lock' | 'clipboard_read' | 'clipboard_write'
/** One desktop action (the mod's desktop tool); the answer's `desktop` field is read in desktop.ts. */
export type DesktopCommand = {
  action: DesktopAction
  /** open: an app's Start-menu name, an allowed link or a folder; focus: an app or a window title. 1 to 400 characters. */
  target?: string
  key?: 'play_pause' | 'next' | 'previous' | 'stop'
  /** volume: 0 to 100. */
  level?: number
  change?: 'up' | 'down' | 'mute' | 'unmute'
  /** clipboard_write: at most 20,000 characters. */
  text?: string
}
export type ConfigCommand = {
  voiceId?: string
  pttKey?: string
  sttModel?: string
  language?: string
  wakeWord?: boolean
  /** Also wake on plain "Jarvis" at the start of an utterance (helpers with the `wake.plain` capability). */
  plainWake?: boolean
  bargeIn?: BargeInMode
  /** 0.05 to 0.95; lower wakes more easily. */
  wakeThreshold?: number
}
export type TestVoiceCommand = { text?: string }
type Empty = Record<string, never>

export type CommandBodies = {
  heartbeat: Empty
  speak: SpeakCommand
  stop: StopCommand
  listen: ListenCommand
  config: ConfigCommand
  status: Empty
  desktop: DesktopCommand
  test_voice: TestVoiceCommand
  shutdown: Empty
}

export type CommandName = keyof CommandBodies

export type CommandResponse = {
  ok: boolean
  error?: { code: ErrorCode; message: string }
  [extra: string]: unknown
}

export type StatusResponse = {
  ok: true
  state: HelperState
  version: string
  platform: HelperPlatform
  sttModel?: string
  sttDevice?: string
  inputDevice?: string
  outputDevice?: string
  voiceId?: string
  pttKey?: string
  fishKeySet?: boolean
  ttsEngine?: 'fish' | 'local'
  /** The wake phrase, while it is loaded and switched on. */
  wakeWord?: string
  bargeIn?: BargeInMode
  /** Echo cancelling of what the listener hears: loading at start; unavailable if it failed to load or broke. */
  echoCancel?: EchoCancelState
}

/** schema.json's maxLength on SpeakCommand.text. */
export const SPEAK_TEXT_MAX = 4000

const EVENT_TYPES: ReadonlySet<string> = new Set<HelperEventType>([
  'hello',
  'state',
  'level',
  'utterance',
  'speech_started',
  'speech_done',
  'barge_in',
  'error',
  'ready',
])

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value)

/**
 * Parses one stdout line into an event. Checks the envelope and the fields the
 * mod relies on; anything else (a stray print, a newer event type) is undefined.
 */
export function parseEvent(line: string): HelperEvent | undefined {
  let value: unknown
  try {
    value = JSON.parse(line)
  } catch {
    return undefined
  }
  if (!isRecord(value) || value.v !== PROTOCOL_VERSION) return undefined
  const type = value.type
  if (typeof type !== 'string' || !EVENT_TYPES.has(type)) return undefined
  switch (type) {
    case 'hello':
      return Number.isInteger(value.port) ? (value as HelloEvent) : undefined
    case 'state':
      return typeof value.state === 'string' ? (value as StateEvent) : undefined
    case 'utterance':
      return typeof value.text === 'string' && typeof value.id === 'string'
        ? (value as UtteranceEvent)
        : undefined
    case 'error':
      return typeof value.code === 'string' ? (value as ErrorEvent) : undefined
    default:
      return value as HelperEvent
  }
}
