// The hands tool's gate (mcp__jarvis__hands; the tool itself is hands.ts's).
// This plugin answers the tool itself (register.tsx), so the engine's
// permission path never sees a call, nor do the user's settings hooks: as for
// the desktop tool, the user's own rules for it are applied here
// (desktop.ts checkRules, through `$.tool.check`: a deny refuses, only an
// allow by the user's own rule runs unasked, and not when a PreToolUse or
// PermissionRequest hook in their settings could match the tool, nor when the
// turn's mode is not known; every other verdict asks, a spoken yes in a voice
// turn, else a click, pc.ts confirm), and plan mode keeps it read-only: only
// its status is read then. Then hands.ts runs the call as before.

import type { Jarvis } from './app'
import type { OwnToolCall } from './desktop'
import { checkRules } from './desktop'
import type { DisplaySelection } from './hands'
import { HANDS_TOOL, parseDisplaySelection, runHandsTool } from './hands'
import type { AskPorts } from './pc'

/** The name the model calls it by (`jarvis` is the plugin's name, plugin.json). */
export const HANDS_TOOL_ID = `mcp__jarvis__${HANDS_TOOL.name}`

type HandsToolAction = (typeof HANDS_TOOL.inputSchema.properties.action.enum)[number]

const ACTIONS: readonly HandsToolAction[] = HANDS_TOOL.inputSchema.properties.action.enum
const isAction = (value: unknown): value is HandsToolAction => (ACTIONS as readonly unknown[]).includes(value)

/** What each action does, in a few words, and what a question adds after them (the camera, the palm). */
const ACTION_WORDS: Record<HandsToolAction, { what: string; more?: string }> = {
  on: { what: 'turn hand control on', more: ', which opens the camera' },
  off: { what: 'turn hand control off' },
  status: { what: "read hand control's status" },
  calibrate: { what: 'start calibrating hand control' },
  pause: { what: 'pause hand control', more: ', which turns the camera off' },
  resume: { what: 'resume hand control', more: ', which turns the camera back on' },
  engage: { what: 'give your hand the cursor', more: ' now, with no open palm needed' },
  disengage: { what: 'take the cursor away from your hand' },
}

const NOTHING_DONE = 'nothing was done'
const PLAN_MODE =
  "Plan mode is on, so Jarvis does not change hand control now. Describe what you would do in the plan instead; reading hand control's status still works, and the user can change it themselves with /jarvis hands."
const MODE_UNKNOWN =
  "Jarvis cannot tell yet whether plan mode is on (it has not seen a prompt since it started), so it does not change hand control until the user's next message. Reading hand control's status still works."

/**
 * The displays a call's `display` chooses, read as /jarvis hands display
 * reads it (hands.ts passes the tool's value on the same way); undefined when
 * there is none, or none hand control knows, which changes nothing.
 */
function displayChoice(value: unknown): DisplaySelection | undefined {
  if (value === undefined) return undefined
  const text = Array.isArray(value) ? value.map(String).join(',') : typeof value === 'string' || typeof value === 'number' ? String(value) : ''
  return parseDisplaySelection(text)
}

/** "make hand control reach displays 1 and 2". */
function displayWords(selection: DisplaySelection): string {
  if (selection === 'all') return 'make hand control reach all displays'
  if (selection.length === 1) return `make hand control reach display ${selection[0]}`
  return `make hand control reach displays ${selection.slice(0, -1).join(', ')} and ${selection.at(-1)}`
}

/**
 * The model's call of the hands tool: `{ result }` in words (hands.ts's, or
 * why nothing was done), or `{ deny }` when the user's rules refuse it. A
 * call hands.ts would answer with what to fix, doing nothing, is answered so
 * with nothing asked. `signal` is the call's own: once it aborts, nothing
 * more is asked or done.
 */
export async function callHandsTool(app: Jarvis, input: Record<string, unknown>, ports: AskPorts, signal?: AbortSignal): Promise<{ result: string } | { deny: string }> {
  const action = isAction(input.action) ? input.action : undefined
  const display = displayChoice(input.display)
  const hands = app.hands
  // Nothing to ask about: arguments to fix (a display hand control does not know only says so), or a session where it cannot run.
  const isNoOp = (input.action !== undefined && action === undefined) || (action === undefined && display === undefined)
  if (isNoOp || hands === undefined || !hands.isLocal) return { result: await runHandsTool(hands, input) }
  const pc = app.pc
  const isReadOnly = action === 'status' && display === undefined
  if (!isReadOnly) {
    // Plan mode reads only: the engine's own plan-mode block never sees this tool.
    const mode = pc.permissionMode
    if (mode === undefined) return { result: MODE_UNKNOWN }
    if (mode === 'plan') return { result: PLAN_MODE }
  }
  const agentId = typeof input.agentId === 'string' ? input.agentId : undefined
  const words = action === undefined ? undefined : ACTION_WORDS[action]
  const parts = [display === undefined ? '' : displayWords(display), words?.what ?? '']
  const what = parts.filter(part => part !== '').join(', then ')
  const described = `${what}${words?.more ?? ''}`
  const call: OwnToolCall = { tool: HANDS_TOOL_ID, name: HANDS_TOOL.name, what, input }
  // A change needs the mode known for this very turn: a Shift+Tab into plan mode may have come since.
  const gate = await checkRules(app, pc, call, ports, !isReadOnly && !pc.isModeKnownForTurn(agentId))
  if (typeof gate === 'object') return gate
  if (gate === 'ask') {
    // A spoken yes in a voice turn, else a click.
    const refused = await pc.confirm(
      {
        key: `${HANDS_TOOL_ID}\n${action ?? ''}\n${display === undefined ? '' : String(display)}`,
        reason: `uses the hands tool to ${what}`,
        again: 'call the hands tool with the same arguments again',
        spoken: `Claude wants to ${described}. Say yes to let it, sir.`,
        shown: `let Claude ${described}`,
        screen: { question: `Jarvis: Claude wants to use the hands tool to ${described}. Do it?`, no: "Don't do it", yes: 'Do it', nothing: NOTHING_DONE },
        agentId,
      },
      ports.ask,
      signal,
    )
    if (refused !== undefined) return { result: refused }
  }
  if (signal?.aborted === true) return { result: `The call was interrupted, so ${NOTHING_DONE}.` }
  return { result: await runHandsTool(hands, input) }
}
