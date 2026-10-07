import { describe, expect, test } from 'claude-code/testing'
import type { Engine as TestEngine } from 'claude-code/testing'
import type { On, RenderPropsOf, RenderViewport } from 'claude-code'

import { completeTurn, jarvis, startHelper, startSession, world } from './test-harness'
import type { World } from './test-harness'
import type { Engine } from './engine'
import { Hud, HUD_PANE, RING_KEY } from './hud'
import { replyLines } from './ui'

/** Docked beside the conversation in a 160 x 45 fullscreen terminal. */
const DOCK: RenderPropsOf['Pane'] = {
  title: 'JARVIS',
  isFocused: false,
  bodyColumns: 53,
  placement: 'dock',
  scroll: { offset: 0, bodyRows: 38 },
  view: {},
}
const FULLSCREEN: RenderViewport = { columns: 106, rows: 45, isFullscreen: true }

/** The engine's own transcript rows beneath the plugin. */
function transcript(on: On): void {
  on('ui.render', { component: 'UserMessage' }, ($, e) => {
    const { Text } = $.ui.resolve(e)
    return <Text>{`you: ${e.props.text}`}</Text>
  })
  on('ui.render', { component: 'AssistantMessage' }, ($, e) => {
    const { Text } = $.ui.resolve(e)
    return <Text>{`claude: ${e.props.text}`}</Text>
  })
}

/** Whether a prompt's row shows (it draws nothing while focus mode folds it). */
async function isPromptShown($: TestEngine, isExpanded = false): Promise<boolean> {
  const row = await $.ui.mount({
    plugin: 'jarvis',
    surface: 'terminal',
    component: 'UserMessage',
    props: { text: 'Run the tests', origin: { kind: 'composer' }, isExpanded },
  })
  return (await row.find({ type: 'Text', text: 'you: Run the tests' })) !== undefined
}

async function isReplyShown($: TestEngine): Promise<boolean> {
  const row = await $.ui.mount({ plugin: 'jarvis', surface: 'terminal', component: 'AssistantMessage', props: { text: 'Done.', isFirstOfReply: true } })
  return (await row.find({ type: 'Text', text: 'claude: Done.' })) !== undefined
}

async function mountHud($: TestEngine, w: World, props = DOCK, viewport = FULLSCREEN) {
  const ui = await $.ui.mount({ plugin: 'jarvis', surface: 'terminal', component: 'Pane', requestId: HUD_PANE, props, viewport })
  await w.clock.advance(1)
  await w.settle()
  return ui
}

describe('focus mode', () => {
  test('is off by default: the conversation shows beside the HUD', async ($, on) => {
    const w = world(on)
    transcript(on)
    await startHelper($, w)
    await mountHud($, w)
    expect(await isPromptShown($)).toBe(true)
    expect(await isReplyShown($)).toBe(true)
    expect(w.opens.at(-1)).toMatchObject({ id: HUD_PANE, columns: 60 })
  })

  test('/jarvis focus folds the conversation away and widens the HUD to nearly the whole screen', async ($, on) => {
    const w = world(on)
    transcript(on)
    await startHelper($, w)
    expect(await jarvis($, 'focus')).toContain('Focus mode on')
    expect(w.store.get('focus')).toBe(true)
    const narrow = await mountHud($, w)
    expect(await isPromptShown($)).toBe(false)
    expect(await isReplyShown($)).toBe(false)
    expect(w.opens.at(-1)).toMatchObject({ id: HUD_PANE, columns: 156 })
    // The engine gives the pane its new width: the texts go beside a bigger ring.
    await narrow.unmount()
    const wide = await mountHud($, w, { ...DOCK, bodyColumns: 135 }, { ...FULLSCREEN, columns: 24 })
    expect((await wide.find({ type: 'Raster', key: RING_KEY }))?.props).toMatchObject({ columns: 74, rows: 37 })
  })

  test('Ctrl+O still shows the whole conversation', async ($, on) => {
    const w = world(on)
    transcript(on)
    await startHelper($, w)
    await jarvis($, 'focus on')
    await mountHud($, w)
    expect(await isPromptShown($, true)).toBe(true)
    expect(await isReplyShown($)).toBe(true)
    // Back from the detailed view, the rows fold again.
    expect(await isPromptShown($)).toBe(false)
  })

  test('typing brings the conversation back until you next talk to Jarvis', async ($, on) => {
    const w = world(on)
    transcript(on)
    const helper = await startHelper($, w)
    await jarvis($, 'focus on')
    await mountHud($, w)
    await $.prompt.submit({ text: 'Run the tests', wait: false, origin: { kind: 'composer' } })
    await w.settle()
    expect(await isPromptShown($)).toBe(true)
    expect(w.opens.at(-1)).toMatchObject({ columns: 60 })
    helper.event({ type: 'state', state: 'listening' })
    await w.clock.advance(1)
    await w.settle()
    expect(await isPromptShown($)).toBe(false)
    expect(w.opens.at(-1)).toMatchObject({ columns: 156 })
  })

  test('waits for Jarvis to run', async ($, on) => {
    const w = world(on, { installed: false })
    transcript(on)
    await startSession($, w)
    expect(await jarvis($, 'focus on')).toContain('once Jarvis is running')
    await mountHud($, w)
    expect(await isPromptShown($)).toBe(true)
    expect(w.opens.at(-1)).toMatchObject({ columns: 60 })
  })

  test('/jarvis focus off ends it, in later sessions too', async ($, on) => {
    const w = world(on)
    transcript(on)
    await startHelper($, w)
    await jarvis($, 'focus on')
    const hud = await mountHud($, w)
    expect(await isPromptShown($)).toBe(false)
    expect(await jarvis($, 'focus off')).toContain('Focus mode off')
    expect(w.store.get('focus')).toBe(false)
    expect(await isPromptShown($)).toBe(true)
    await hud.unmount()
    await startSession($, w)
    await mountHud($, w)
    expect(await isPromptShown($)).toBe(true)
  })

  test('a closed HUD stays closed when focus mode changes', () => {
    const opens: unknown[] = []
    const engine = {
      after: (ms: number, fn: () => void) => fn(),
      openPane: async (size: unknown) => {
        opens.push(size)
        return { isPlaced: true }
      },
      writeHud: async () => undefined,
      debug: () => undefined,
    } as unknown as Engine
    const hud = new Hud(engine)
    hud.onLayout({ placement: 'dock', bodyColumns: 53, bodyRows: 38, columns: 106, rows: 45 })
    expect(opens).toEqual([{ rows: 24, columns: 60 }])
    hud.onClosed()
    hud.setFocus(true)
    expect(opens).toHaveLength(1)
    hud.dispose()
  })

  test('above the prompt on the main screen it folds nothing: the tall HUD pushes the conversation up', async ($, on) => {
    const w = world(on)
    transcript(on)
    await startHelper($, w)
    await jarvis($, 'focus on')
    const inline: RenderPropsOf['Pane'] = { ...DOCK, placement: 'inline', bodyColumns: 100, scroll: { offset: 0, bodyRows: 22 } }
    await mountHud($, w, inline, { columns: 100, rows: 60, isFullscreen: false })
    expect(await isPromptShown($)).toBe(true)
    expect(w.opens.at(-1)).toMatchObject({ id: HUD_PANE, rows: 52 })
  })

  test("shows Claude's last reply under the ring", async ($, on) => {
    const w = world(on)
    await startHelper($, w)
    await jarvis($, 'focus on')
    const ui = await mountHud($, w)
    w.messages = [
      { role: 'user', text: 'What time is it?', toolUses: [] },
      { role: 'assistant', text: "It's **three** o'clock, sir.", toolUses: [] },
    ]
    await $.turn.start({ text: 'What time is it?', turnId: 't1' })
    await completeTurn($, 't1')
    await w.settle()
    await ui.redraw()
    expect(await ui.find({ type: 'Text', text: /It's three o'clock, sir\./ })).toBeDefined()
  })

  test('a reply is cut to a few lines, its markdown dropped', () => {
    const reply = '# Done\n\nI ran **the tests**: all `42` passed.\n```\nnpm test\n```\nNext I will deploy it.'
    expect(replyLines(reply, 20, 2)).toEqual(['Done I ran the', 'tests: all 42…'])
    expect(replyLines('Hello there', 20, 3)).toEqual(['Hello there'])
    expect(replyLines('snake_case_name stays', 40, 1)).toEqual(['snake_case_name stays'])
  })
})
