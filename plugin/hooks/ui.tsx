// The UI: a status line entry ("JARVIS · <state>", with hand control's part
// while it is on), a band above the prompt while the user is talking, and the
// HUD pane (the ring, what you said and what Claude is doing).

import type { Elements, RenderElement } from 'claude-code'

import type { HandsPhase, HudAction, JarvisHandsView, JarvisHud, JarvisPhase, JarvisView } from '../types'
import type { HudMode } from './hud'
import type { HudLayout } from './hud'
import { ACTION_LIMIT, MODE_LABELS, REPLY_ROWS, RING_KEY } from './hud'
import { HUD_COLORS } from './hud-ring'
import { ringSvg } from './hud-svg'

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

type HudTerminalElements = Pick<Elements['terminal'], 'Box' | 'Text' | 'Raster'>
type HudSvgElements = Pick<Elements['desktop'], 'Box' | 'Text' | 'Svg'>

export type HudPaneData = {
  mode: HudMode
  view: JarvisView
  hud: JarvisHud
  /** Focus mode shows: the reply goes under the ring, the conversation being folded away. */
  isFocus?: boolean
}

const ACTION_MARKS: Record<HudAction['status'], string> = { running: '›', done: '✓', failed: '✗' }

/**
 * The start of a reply in at most `rows` lines of `width` cells, its markdown
 * marks dropped; an ellipsis ends it when it goes on.
 */
export function replyLines(text: string, width: number, rows: number): string[] {
  const words = text
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/^\s{0,3}(#{1,6}|>)\s*/gm, '')
    .replace(/\*\*|__|`/g, '')
    .split(/\s+/)
    .filter(word => word !== '')
  const lines: string[] = []
  let line = ''
  for (const word of words) {
    const next = line === '' ? word : `${line} ${word}`
    if (next.length <= width || line === '') {
      line = next.length > width ? next.slice(0, width) : next
      continue
    }
    lines.push(line)
    line = word.slice(0, width)
    if (lines.length === rows) break
  }
  if (lines.length < rows && line !== '') lines.push(line)
  const isCut = lines.join(' ').length < words.join(' ').length
  if (isCut && lines.length > 0) {
    const last = lines[lines.length - 1] as string
    lines[lines.length - 1] = `${last.slice(0, Math.max(0, width - 1))}…`
  }
  return lines
}

/** The lines under the ring: what you said, Claude's reply in focus mode, then what Claude did, newest first. */
function hudTexts<E extends Pick<Elements['terminal'], 'Box' | 'Text'>>(
  { Box, Text }: E,
  data: HudPaneData,
  limit: number,
  columns: number,
  replyRows: number,
): RenderElement {
  const actions = data.hud.actions.slice(0, Math.max(0, limit))
  const reply = data.isFocus && data.hud.lastReply ? replyLines(data.hud.lastReply, Math.max(10, columns - 7), replyRows) : []
  return (
    <Box flexDirection="column">
      {data.view.lastUtterance ? (
        <Text wrap="truncate-end">
          <Text dimColor>you </Text>
          {data.view.lastUtterance}
        </Text>
      ) : null}
      {reply.map((line, index) => (
        <Text wrap="truncate-end">
          <Text dimColor>{index === 0 ? 'jarvis ' : '       '}</Text>
          {line}
        </Text>
      ))}
      {actions.map(action => (
        <Text wrap="truncate-end" dimColor={action.status !== 'running'} color={action.status === 'failed' ? 'error' : undefined}>
          {ACTION_MARKS[action.status]} {action.label}
        </Text>
      ))}
    </Box>
  )
}

/** The title bar: the mode in the ring's color. */
function hudTitle<E extends Pick<Elements['terminal'], 'Text'>>({ Text }: E, mode: HudMode): RenderElement {
  return (
    <Text color={hudColor(mode)} bold>
      {`⟨ JARVIS · ${MODE_LABELS[mode]} ⟩`}
    </Text>
  )
}

export const hudColor = (mode: HudMode): string => `#${HUD_COLORS[mode].toString(16).padStart(6, '0')}`

/** A transcript row folded away by focus mode: it draws nothing. */
export function foldedRow({ Box }: Pick<Elements['terminal'], 'Box'>): RenderElement {
  return <Box />
}

/** The terminal pane: title, the ring (a Raster the HUD repaints), the texts. */
export function hudTerminalTree({ Box, Text, Raster }: HudTerminalElements, data: HudPaneData, layout: HudLayout, cells: string): RenderElement {
  const { ring } = layout
  const raster = <Raster key={RING_KEY} columns={ring.columns} rows={ring.rows} cells={cells} />
  const texts = hudTexts({ Box, Text }, data, layout.actionRows, layout.textColumns, layout.replyRows)
  return (
    <Box flexDirection="column">
      <Box justifyContent="center">{hudTitle({ Text }, data.mode)}</Box>
      {layout.isSide ? (
        <Box flexDirection="row" justifyContent="center">
          {raster}
          <Box flexDirection="column" justifyContent="center" marginLeft={2} width={layout.textColumns}>
            {texts}
          </Box>
        </Box>
      ) : (
        <Box justifyContent="center">{raster}</Box>
      )}
      {layout.isSide ? null : texts}
    </Box>
  )
}

/** The desktop pane: the same, the ring as an animated SVG. */
export function hudSvgTree(
  { Box, Text, Svg }: HudSvgElements,
  data: HudPaneData,
  levels: { mic: number; out: number; t: number },
  columns: number,
): RenderElement {
  return (
    <Box flexDirection="column">
      <Box justifyContent="center">{hudTitle({ Text }, data.mode)}</Box>
      <Box justifyContent="center">
        <Svg
          source={ringSvg(data.mode, levels.mic, levels.out, levels.t)}
          alt={`Jarvis: ${MODE_LABELS[data.mode].toLowerCase()}`}
          width={240}
          height={240}
          isInteractive
        />
      </Box>
      {hudTexts({ Box, Text }, data, ACTION_LIMIT, columns, REPLY_ROWS)}
    </Box>
  )
}
