// The hands tool's gate (mcp__jarvis__hands; the tool itself is hands.ts's).
// This plugin answers the tool itself (register.tsx), so the engine's
// permission path never sees a call, nor do the user's settings hooks: as for
// the desktop tool, the user's own rules for it are applied here
// (desktop.ts checkRules, through `$.tool.check`: a deny refuses, only an
// allow by the user's own rule runs unasked, and not when a PreToolUse or
// PermissionRequest hook in their settings could match the tool, nor when the
// turn's mode is not known; every other verdict asks, a spoken yes in a voice
// turn, else a click, pc.ts confirm), and plan mode keeps it read-only: only
// its status, and a sensitivity setting read by name, are read then. Then
// hands.ts runs the call as before. A call changes something when it carries a
// display, an action but status, or a sensitivity setting's value or a preset
// (hands.ts toolTuning reads the last as the tool does); only a call the tool
// would answer with what to fix, doing nothing, runs unasked.

import type { Jarvis } from './app'
import type { OwnToolCall } from './desktop'
import { checkRules } from './desktop'
import type { DisplaySelection, ToolTuning } from './hands'
import { HANDS_TOOL, parseDisplaySelection, runHandsTool, toolTuning } from './hands'
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
  // Opening it only puts a keyboard on the screen: what is tapped is the user's own, and the tool has no way to insert or send it.
  keyboard: { what: 'open the air keyboard', more: ', which you then type on yourself in the air' },
  keyboard_practice: { what: 'open the air keyboard in practice mode', more: ', where nothing you tap is typed' },
  keyboard_off: { what: 'close the air keyboard', more: ', which throws away what is in its review box' },
}

const NOTHING_DONE = 'nothing was done'
const PLAN_MODE =
  "Plan mode is on, so Jarvis does not change hand control now. Describe what you would do in the plan instead; reading hand control's status or one of its sensitivity settings still works, and the user can change it themselves with /jarvis hands."
const MODE_UNKNOWN =
  "Jarvis cannot tell yet whether plan mode is on (it has not seen a prompt since it started), so it does not change hand control until the user's next message. Reading hand control's status or one of its sensitivity settings still works."

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

/** What a unit is called aloud (a spoken question says "1.5 seconds", not "1.5 s"). */
const UNIT_WORDS: Record<string, string> = { s: ' seconds', px: ' pixels' }

/** The sensitivity change a call makes, in a few words ("set the hand control cursor speed setting to 1.5"). */
function tuningWords(tuning: ToolTuning): { what: string; more?: string } {
  if (tuning.kind === 'preset') return { what: `apply the ${tuning.name} hand control preset`, more: ', which sets every sensitivity setting' }
  const setting = `the hand control ${tuning.knob.label.toLowerCase()} setting`
  if (tuning.kind === 'read') return { what: `read ${setting}` }
  if (tuning.value === 'default') return { what: `put ${setting} back to its default` }
  return { what: `set ${setting} to ${tuning.value}${UNIT_WORDS[tuning.knob.unit ?? ''] ?? ''}` }
}

/** What a spoken yes must match, exactly: the setting by its wire key, the value as hands.ts reads it. */
function tuningKey(tuning: ToolTuning | undefined): string {
  if (tuning === undefined) return ''
  if (tuning.kind === 'preset') return `preset ${tuning.name}`
  return tuning.kind === 'read' ? `read ${tuning.knob.key}` : `set ${tuning.knob.key} ${tuning.value}`
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
  const tuning = toolTuning(input)
  const hands = app.hands
  // Nothing to ask about: arguments to fix (a display or a setting hand control does not know only says so), or a session where it cannot run.
  const isNoOp = (input.action !== undefined && action === undefined) || (action === undefined && display === undefined && tuning === undefined)
  if (isNoOp || hands === undefined || !hands.isLocal) return { result: await runHandsTool(hands, input) }
  const pc = app.pc
  // The status and a setting read by name change nothing; every other part of a call does.
  const isReadOnly = (action === undefined || action === 'status') && display === undefined && (tuning === undefined || tuning.kind === 'read')
  if (!isReadOnly) {
    // Plan mode reads only: the engine's own plan-mode block never sees this tool.
    const mode = pc.permissionMode
    if (mode === undefined) return { result: MODE_UNKNOWN }
    if (mode === 'plan') return { result: PLAN_MODE }
  }
  const agentId = typeof input.agentId === 'string' ? input.agentId : undefined
  // In the order hands.ts does them: the displays, the sensitivity, then the action.
  const parts: { what: string; more?: string }[] = []
  if (display !== undefined) parts.push({ what: displayWords(display) })
  if (tuning !== undefined) parts.push(tuningWords(tuning))
  if (action !== undefined) parts.push(ACTION_WORDS[action])
  const what = parts.map(part => part.what).join(', then ')
  const described = parts.map(part => `${part.what}${part.more ?? ''}`).join(', then ')
  const call: OwnToolCall = { tool: HANDS_TOOL_ID, name: HANDS_TOOL.name, what, input }
  // A change needs the mode known for this very turn: a Shift+Tab into plan mode may have come since.
  const gate = await checkRules(app, pc, call, ports, !isReadOnly && !pc.isModeKnownForTurn(agentId))
  if (typeof gate === 'object') return gate
  if (gate === 'ask') {
    // A spoken yes in a voice turn, else a click.
    const refused = await pc.confirm(
      {
        key: `${HANDS_TOOL_ID}\n${action ?? ''}\n${display === undefined ? '' : String(display)}\n${tuningKey(tuning)}`,
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
