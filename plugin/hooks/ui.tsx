// The UI: a status line entry ("JARVIS · <state>"), a band above the prompt
// while the user is talking, and the HUD pane (the ring, what you said and
// what Claude is doing).

import type { Elements, RenderElement } from 'claude-code'

import type { HudAction, JarvisHud, JarvisPhase, JarvisView } from '../types'
import type { HudMode } from './hud'
import { ACTION_LIMIT, MODE_LABELS, RING_KEY } from './hud'
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
  return label === undefined ? undefined : `JARVIS · ${label}`
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
}

const ACTION_MARKS: Record<HudAction['status'], string> = { running: '›', done: '✓', failed: '✗' }

/** The lines under the ring: what you said, then what Claude did, newest first. */
function hudTexts<E extends Pick<Elements['terminal'], 'Box' | 'Text'>>({ Box, Text }: E, data: HudPaneData, limit: number): RenderElement {
  const actions = data.hud.actions.slice(0, Math.max(0, limit))
  return (
    <Box flexDirection="column">
      {data.view.lastUtterance ? (
        <Text wrap="truncate-end">
          <Text dimColor>you </Text>
          {data.view.lastUtterance}
        </Text>
      ) : null}
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

/** The rows the texts under the ring take at most. */
export const HUD_TEXT_ROWS = 2 + ACTION_LIMIT

/** The terminal pane: title, the ring (a Raster the HUD repaints), the texts. */
export function hudTerminalTree(
  { Box, Text, Raster }: HudTerminalElements,
  data: HudPaneData,
  ring: { columns: number; rows: number; cells: string },
  actionRows: number,
): RenderElement {
  return (
    <Box flexDirection="column">
      <Box justifyContent="center">{hudTitle({ Text }, data.mode)}</Box>
      <Box justifyContent="center">
        <Raster key={RING_KEY} columns={ring.columns} rows={ring.rows} cells={ring.cells} />
      </Box>
      {hudTexts({ Box, Text }, data, actionRows)}
    </Box>
  )
}

/** The desktop pane: the same, the ring as an animated SVG. */
export function hudSvgTree({ Box, Text, Svg }: HudSvgElements, data: HudPaneData, levels: { mic: number; out: number; t: number }): RenderElement {
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
      {hudTexts({ Box, Text }, data, ACTION_LIMIT)}
    </Box>
  )
}
