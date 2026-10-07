// What the two pages draw, worked out in the main process from the snapshot
// it shows: the ring's mode and levels, the texts, and a note when the voice
// helper needs the user. The pages only copy it into the DOM.

import type { HudMode } from '../../../plugin/hooks/hud-ring'
import type { ActionStatus, AppSnapshot } from './snapshot'

/** The same words the plugin's HUD shows under its ring (plugin/hooks/hud.ts). */
export const MODE_LABELS: Record<HudMode, string> = {
  offline: 'OFFLINE',
  sleeping: 'STANDING BY',
  listening: 'LISTENING',
  thinking: 'THINKING',
  speaking: 'SPEAKING',
  interrupted: 'LISTENING',
}

/**
 * Each mode's colour, for the tray icon. A copy of the plugin's HUD_COLORS,
 * because importing hud-ring.ts at runtime would bundle its pixel shader; a
 * test keeps the two equal.
 */
export const MODE_COLORS: Record<HudMode, number> = {
  offline: 0x6b757d,
  sleeping: 0x2f9bff,
  listening: 0x5cc8ff,
  thinking: 0xff9a2a,
  speaking: 0xffc04a,
  interrupted: 0xbfe6ff,
}

export const ACTION_MARKS: Record<ActionStatus, string> = {
  running: '›',
  done: '✓',
  failed: '✗',
}

export type AppView = {
  mode: HudMode
  label: string
  /** A few words for the tray: "standing by", "waiting for Claude Code". */
  status: string
  /** What the user can do when the voice helper is not ready; '' when it is. */
  note: string
  mic: number
  out: number
  utterance: string
  reply: string
  actions: { mark: string; label: string; status: ActionStatus }[]
  isOverlayShown: boolean
}

/** A hint under the ring when the voice helper is not listening, by its phase. */
export function noteFor(snapshot: AppSnapshot | undefined): string {
  if (snapshot === undefined) return 'Waiting for Claude Code. Start a session with the Jarvis plugin.'
  switch (snapshot.phase) {
    case 'not_installed':
      return 'The voice helper is not set up. Run /jarvis setup in Claude Code.'
    case 'setup':
      return 'Setting up the voice helper.'
    case 'starting':
    case 'restarting':
      return 'Starting the voice helper.'
    case 'stopped':
      return 'The voice helper is not running. Run /jarvis in Claude Code.'
    case 'error':
    case 'failed':
      return 'The voice helper stopped. Run /jarvis restart in Claude Code.'
    case 'elsewhere':
      return 'Jarvis runs in another Claude Code window.'
    case 'unavailable':
      return 'This Claude Code session runs in the cloud.'
    default:
      return ''
  }
}

const STATUS: Record<HudMode, string> = {
  offline: 'offline',
  sleeping: 'standing by',
  listening: 'listening',
  thinking: 'thinking',
  speaking: 'speaking',
  interrupted: 'listening',
}

/** The tray's few words for what Jarvis is doing. */
export function statusLine(snapshot: AppSnapshot | undefined): string {
  return snapshot === undefined ? 'waiting for Claude Code' : STATUS[snapshot.mode]
}

export function buildView(snapshot: AppSnapshot | undefined, isOverlayShown: boolean): AppView {
  const mode = snapshot?.mode ?? 'offline'
  return {
    mode,
    label: MODE_LABELS[mode],
    status: statusLine(snapshot),
    note: noteFor(snapshot),
    mic: snapshot?.mic ?? 0,
    out: snapshot?.out ?? 0,
    utterance: snapshot?.utterance ?? '',
    reply: snapshot?.reply ?? '',
    actions: (snapshot?.actions ?? []).map(action => ({ mark: ACTION_MARKS[action.status], label: action.label, status: action.status })),
    isOverlayShown,
  }
}
