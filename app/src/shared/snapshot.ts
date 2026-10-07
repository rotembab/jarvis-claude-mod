// The HUD snapshot the plugin pushes (POST /v1/hud, docs/APP.md has its JSON
// Schema). The check is strict on types and lenient on lengths: a string that
// grew in a later plugin is cut, never refused, because the plugin keeps its
// link after a 400 and every push would then fail, freezing the display.

import type { HudMode } from '../../../plugin/hooks/hud-ring'

/** Every ring mode; a Record, so tsc fails here if the plugin adds one. */
const MODE_SET: Record<HudMode, true> = {
  offline: true,
  sleeping: true,
  listening: true,
  thinking: true,
  speaking: true,
  interrupted: true,
}
export const HUD_MODES: readonly HudMode[] = Object.keys(MODE_SET) as HudMode[]

export const ACTION_STATUSES = ['running', 'done', 'failed'] as const
export type ActionStatus = (typeof ACTION_STATUSES)[number]

export const UTTERANCE_MAX = 500
export const REPLY_MAX = 600
export const LABEL_MAX = 80
export const ACTIONS_MAX = 6

export type AppAction = { label: string; status: ActionStatus }

export type AppSnapshot = {
  v: 1
  sessionId: string
  mode: HudMode
  phase: string
  mic: number
  out: number
  utterance?: string
  reply?: string
  actions: AppAction[]
  isOwner: boolean
  at: number
}

export type Parsed = { ok: true; snapshot: AppSnapshot } | { ok: false; message: string }

const SESSION_ID = /^[A-Za-z0-9_-]{1,64}$/
const PHASE = /^[a-z_]{1,32}$/

/** `text` cut to `max` characters, the last one an ellipsis when it was longer. */
export function cut(text: string, max: number): string {
  return text.length > max ? text.slice(0, max - 1) + '…' : text
}

const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value)

const isNumber = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value)

const clamp01 = (value: number): number => Math.min(1, Math.max(0, value))

const isMode = (value: unknown): value is HudMode => typeof value === 'string' && Object.hasOwn(MODE_SET, value)

const isStatus = (value: unknown): value is ActionStatus =>
  typeof value === 'string' && (ACTION_STATUSES as readonly string[]).includes(value)

/** An optional text: absent stays absent, '' becomes absent, a long one is cut; undefined when it is not a string. */
function optionalText(value: unknown, max: number): { ok: true; text?: string } | { ok: false } {
  if (value === undefined) return { ok: true }
  if (typeof value !== 'string') return { ok: false }
  return value === '' ? { ok: true } : { ok: true, text: cut(value, max) }
}

/** Checks and normalizes one pushed snapshot; unknown fields are dropped. */
export function parseSnapshot(value: unknown): Parsed {
  const fail = (message: string): Parsed => ({ ok: false, message })
  if (!isObject(value)) return fail('the snapshot must be a JSON object')
  if (value.v !== 1) return fail('v must be 1')
  if (typeof value.sessionId !== 'string' || !SESSION_ID.test(value.sessionId)) {
    return fail('sessionId must be 1 to 64 letters, digits, _ or -')
  }
  if (!isMode(value.mode)) return fail(`mode must be one of ${HUD_MODES.join(', ')}`)
  if (typeof value.phase !== 'string' || !PHASE.test(value.phase)) return fail('phase must be 1 to 32 lowercase letters or _')
  if (!isNumber(value.mic)) return fail('mic must be a number')
  if (!isNumber(value.out)) return fail('out must be a number')
  const utterance = optionalText(value.utterance, UTTERANCE_MAX)
  if (!utterance.ok) return fail('utterance must be a string')
  const reply = optionalText(value.reply, REPLY_MAX)
  if (!reply.ok) return fail('reply must be a string')
  if (!Array.isArray(value.actions)) return fail('actions must be a list')
  const actions: AppAction[] = []
  for (const [index, item] of value.actions.entries()) {
    if (!isObject(item) || typeof item.label !== 'string' || !isStatus(item.status)) {
      return fail(`actions[${index}] needs a string label and a status of running, done or failed`)
    }
    if (actions.length < ACTIONS_MAX) actions.push({ label: cut(item.label, LABEL_MAX), status: item.status })
  }
  if (typeof value.isOwner !== 'boolean') return fail('isOwner must be true or false')
  if (!isNumber(value.at) || value.at < 0) return fail('at must be a number of 0 or more')
  const snapshot: AppSnapshot = {
    v: 1,
    sessionId: value.sessionId,
    mode: value.mode,
    phase: value.phase,
    mic: clamp01(value.mic),
    out: clamp01(value.out),
    actions,
    isOwner: value.isOwner,
    at: value.at,
  }
  if (utterance.text !== undefined) snapshot.utterance = utterance.text
  if (reply.text !== undefined) snapshot.reply = reply.text
  return { ok: true, snapshot }
}
