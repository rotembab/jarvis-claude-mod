// Minimal UI: one status line entry ("JARVIS · <state>", with hand
// control's part while it is on) and a band above the prompt while the user
// is talking. Nothing is drawn when idle.

import type { Elements, RenderElement } from 'claude-code'

import type { HandsPhase, JarvisHandsView, JarvisPhase, JarvisView } from '../types'

/** The status line text for a view; undefined hides the entry. */
export function statusLine(view: JarvisView): string | undefined {
  const detail = view.detail ? ` · ${view.detail}` : ''
  const labels: Record<JarvisPhase, string | undefined> = {
    unavailable: undefined,
    stopped: 'stopped',
    not_installed: 'not set up · run /jarvis setup',
    setup: `setting up${detail}`,
    starting: 'starting',
    sleeping: `ready · ${view.wakePhrase ? `say "${view.wakePhrase}" or hold` : 'hold'} ${view.pttKey ?? 'the push-to-talk key'} to talk${detail}`,
    listening: 'listening',
    transcribing: 'transcribing',
    speaking: 'speaking · /jarvis stop',
    awake: 'awake · keep talking',
    error: `error${detail}`,
    restarting: `restarting${detail}`,
    elsewhere: 'active in another window',
    failed: `stopped${detail} · /jarvis restart`,
  }
  const label = labels[view.phase]
  if (label === undefined) return undefined
  const hands = view.hands === undefined ? undefined : handsLabel(view.hands)
  return hands === undefined ? `JARVIS · ${label}` : `JARVIS · ${label} · ${hands}`
}

/** Hand control's part of the status line; undefined while it is off. */
export function handsLabel(hands: JarvisHandsView): string | undefined {
  const detail = hands.detail ? ` · ${hands.detail}` : ''
  const labels: Record<HandsPhase, string | undefined> = {
    off: undefined,
    not_installed: 'hands not set up · /jarvis setup hands',
    setup: `hands setting up${detail}`,
    starting: 'hands starting',
    idle: `hands ready · ${hands.engage === 'always' ? 'raise a hand' : 'open palm'} to start`,
    active: 'hands active',
    // The detail: why the camera could not open again, when it could not.
    paused: `hands paused${detail}`,
    calibrating: `hands calibrating${detail}`,
    error: `hands stopped${detail}`,
    restarting: `hands restarting${detail}`,
    elsewhere: 'hands active in another window',
    // A restart cannot cure a final error; its message says what can.
    failed: hands.isFinal === true ? `hands stopped${detail}` : `hands stopped${detail} · /jarvis hands restart`,
  }
  return labels[hands.phase]
}

/** A ten-cell level meter for the microphone. */
export function meter(level: number | undefined): string {
  const cells = Math.round(Math.min(1, Math.max(0, level ?? 0)) * 10)
  return '█'.repeat(cells) + '▁'.repeat(10 - cells)
}

/** True when the band above the prompt has something to show. */
export function isBandShown(view: JarvisView): boolean {
  return view.phase === 'listening' || view.phase === 'transcribing'
}

type BandElements = Pick<Elements['terminal'], 'Box' | 'Text'>

/** The band's tree: who is listening, the mic level and the last utterance. */
export function bandTree({ Box, Text }: BandElements, view: JarvisView): RenderElement {
  return (
    <Box flexDirection="column">
      <Box flexDirection="row">
        <Text color="claude" bold>
          JARVIS{' '}
        </Text>
        <Text>{view.phase === 'listening' ? '● listening ' : '○ transcribing '}</Text>
        {view.phase === 'listening' ? <Text dimColor>{meter(view.micLevel)}</Text> : null}
      </Box>
      {view.lastUtterance ? (
        <Text dimColor wrap="truncate-end">
          last: {view.lastUtterance}
        </Text>
      ) : null}
    </Box>
  )
}
