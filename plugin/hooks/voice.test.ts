import { describe, expect, test } from 'claude-code/testing'
import type { TurnStepChunk } from 'claude-code'
import type { Engine as TestEngine } from 'claude-code/testing'

import type { FakeChild, World } from './test-harness'
import { completeTurn, runStep, startHelper, textChunks, world } from './test-harness'
import { CODE_PLACEHOLDER } from './sentences'
import { isStopPhrase, PERSONA_SECTION_ID, VOICE_NOTE, VOICE_TURN_SECTION_ID } from './voice'

const COMPOSE_INPUT = {
  model: 'claude-test',
  promptModel: 'claude-test',
  surfaces: ['terminal' as const],
  tools: [],
  outputStyle: null,
  traits: [],
}

/** The helper hears the user: an utterance event, as after push-to-talk. */
async function speak(w: World, helper: FakeChild, text: string, id = 'u1'): Promise<void> {
  helper.event({ type: 'utterance', id, text, source: 'ptt', durationMs: 1800, language: 'en' })
  await w.settle()
}

/** The engine starts the turn for a prompt, as it does after UserPromptSubmit. */
async function startTurn($: TestEngine, w: World, text: string, turnId: string): Promise<void> {
  await $.classic.UserPromptSubmit({ prompt: text })
  await $.turn.start({ text, turnId })
  await w.settle()
}

const spoken = (w: World) =>
  w.named('speak').map(command => command.body as { replyId: string; seq: number; text: string; final: boolean })

describe('voice turns', () => {
  test('an utterance is submitted as the user\'s own words', async ($, on) => {
    const w = world(on)
    const helper = await startHelper($, w)
    await speak(w, helper, 'What time is it in Tokyo?')
    expect(w.submits).toHaveLength(1)
    expect(w.submits[0]).toMatchObject({
      text: 'What time is it in Tokyo?',
      origin: { kind: 'plugin', name: 'jarvis', asUser: true },
    })
  })

  test('the voice reply streams to the helper sentence by sentence, then final', async ($, on) => {
    const w = world(on)
    const helper = await startHelper($, w)
    await speak(w, helper, 'Did the build pass?')
    await startTurn($, w, 'Did the build pass?', 'turn-1')

    const reply = 'Certainly, sir. The build passed in 42.5 seconds. Two tests were skipped, see `ci.log`.'
    const chunks = textChunks(reply)
    const forwarded = await runStep($, w, 'turn-1', chunks)
    expect(forwarded).toEqual(chunks) // every chunk passes through unchanged

    // Sentences went out while streaming; the step's end spoke the last one.
    expect(spoken(w).map(one => one.text)).toEqual(['Certainly, sir.', 'The build passed in 42.5 seconds.', 'Two tests were skipped, see ci.log.'])
    await completeTurn($, 'turn-1')
    await w.settle()
    expect(spoken(w)).toEqual([
      { replyId: 'turn-1', seq: 0, text: 'Certainly, sir.', final: false },
      { replyId: 'turn-1', seq: 1, text: 'The build passed in 42.5 seconds.', final: false },
      { replyId: 'turn-1', seq: 2, text: 'Two tests were skipped, see ci.log.', final: false },
      { replyId: 'turn-1', seq: 3, text: '', final: true },
    ])
  })

  test('tool calls, their JSON and thinking are never spoken; code becomes a note', async ($, on) => {
    const w = world(on)
    const helper = await startHelper($, w)
    await speak(w, helper, 'List the files')
    await startTurn($, w, 'List the files', 'turn-2')
    const step0: TurnStepChunk[] = [
      { kind: 'thinking', index: 0, text: 'The user wants a listing.' },
      ...textChunks('Right away', 1),
      { kind: 'tool', index: 2, id: 'toolu_1', name: 'PowerShell' },
      { kind: 'input', index: 2, json: '{"command":"Get-ChildItem"}' },
      { kind: 'stop', stopReason: 'tool_use', usage: null },
    ]
    await runStep($, w, 'turn-2', step0)
    // The step ended (tools run now), so its last line is spoken already.
    expect(spoken(w).map(one => one.text)).toEqual(['Right away'])
    await runStep($, w, 'turn-2', textChunks('Here they are:\n```\nsrc\ndocs\n```\nSeven files in all.'), { index: 1 })
    await completeTurn($, 'turn-2')
    await w.settle()
    expect(spoken(w).map(one => one.text)).toEqual(['Right away', 'Here they are:', CODE_PLACEHOLDER, 'Seven files in all.', ''])
    expect(spoken(w).map(one => one.seq)).toEqual([0, 1, 2, 3, 4])
  })

  test('the sentence before a tool call is spoken as the call starts, not after its input', async ($, on) => {
    const w = world(on)
    const helper = await startHelper($, w)
    await speak(w, helper, 'Check the build')
    await startTurn($, w, 'Check the build', 'turn-5')
    w.steps.set('turn-5:0', [
      ...textChunks('Checking the build now, sir.', 0),
      { kind: 'tool', index: 1, id: 'toolu_1', name: 'PowerShell' },
      { kind: 'input', index: 1, json: '{"command":"npm test"}' },
      { kind: 'stop', stopReason: 'tool_use', usage: null },
    ])
    const heardBy: Partial<Record<TurnStepChunk['kind'], string[]>> = {}
    for await (const chunk of $.turn.step({ turnId: 'turn-5', index: 0, model: 'claude-test', messageCount: 1 })) {
      await w.settle()
      heardBy[chunk.kind] ??= spoken(w).map(one => one.text)
    }
    expect(heardBy.text).toEqual([])
    expect(heardBy.tool).toEqual(['Checking the build now, sir.'])
  })

  test('typed turns are never spoken', async ($, on) => {
    const w = world(on)
    await startHelper($, w)
    await startTurn($, w, 'refactor the parser', 'typed-1')
    await runStep($, w, 'typed-1', textChunks('Done. I split the parser into three modules.'))
    await completeTurn($, 'typed-1')
    await w.settle()
    expect(w.named('speak')).toHaveLength(0)
  })

  test('a subagent\'s steps inside a voice turn are not spoken', async ($, on) => {
    const w = world(on)
    const helper = await startHelper($, w)
    await speak(w, helper, 'Research this')
    await startTurn($, w, 'Research this', 'turn-3')
    await runStep($, w, 'turn-3', textChunks('Subagent notes. Not for speech.'), { agentId: 'agent-1' })
    await w.settle()
    expect(w.named('speak')).toHaveLength(0)
  })

  test('the voice prompt carries the note; typed prompts do not', async ($, on) => {
    const w = world(on)
    const helper = await startHelper($, w)
    await speak(w, helper, 'Open the readme')
    const voiced = await $.classic.UserPromptSubmit({ prompt: 'Open the readme' })
    expect(voiced.additionalContext).toEqual([VOICE_NOTE])
    const typed = await $.classic.UserPromptSubmit({ prompt: 'open the readme please' })
    expect(typed.additionalContext).toBeUndefined()
  })

  test('a typed prompt that starts with the spoken words is not taken for the voice turn', async ($, on) => {
    const w = world(on)
    const helper = await startHelper($, w)
    // A typed task runs; the user says "Yes." (queued behind it), then a typed
    // prompt holding the same words starts first.
    await startTurn($, w, 'refactor the parser', 'typed-0')
    await speak(w, helper, 'Yes.')
    await completeTurn($, 'typed-0')
    await startTurn($, w, 'Yes. Also delete the dist folder.', 'typed-1')
    await runStep($, w, 'typed-1', textChunks('Deleting dist now. Done.'))
    await completeTurn($, 'typed-1')
    await w.settle()
    expect(w.named('speak')).toHaveLength(0)

    // The voice turn itself, when it comes, is spoken.
    await startTurn($, w, 'Yes.', 'voice-2')
    await runStep($, w, 'voice-2', textChunks('Very good, sir.'))
    await completeTurn($, 'voice-2')
    await w.settle()
    expect(spoken(w).map(one => [one.replyId, one.text])).toEqual([
      ['voice-2', 'Very good, sir.'],
      ['voice-2', ''],
    ])
  })

  test('a refused voice prompt is forgotten: the same words typed later stay unspoken', async ($, on) => {
    const w = world(on)
    const helper = await startHelper($, w)
    w.submitDrop = 'Blocked by a UserPromptSubmit hook'
    await speak(w, helper, 'Run the tests.')
    expect(w.logs).toContain('Jarvis: "Run the tests." was not sent: Blocked by a UserPromptSubmit hook')

    w.submitDrop = undefined
    const typed = await $.classic.UserPromptSubmit({ prompt: 'Run the tests.' })
    expect(typed.additionalContext).toBeUndefined()
    await $.turn.start({ text: 'Run the tests.', turnId: 'typed-2' })
    await runStep($, w, 'typed-2', textChunks('All 55 tests pass.'))
    await completeTurn($, 'typed-2')
    await w.settle()
    expect(w.named('speak')).toHaveLength(0)
  })

  test('the persona section is added once voice is in use and stays constant', async ($, on) => {
    const w = world(on, { installed: false })
    await $.session.start({ cwd: 'C:\\work', surface: 'terminal', isInteractive: true })
    await w.settle()
    const before = await $.prompt.compose(COMPOSE_INPUT)
    expect(before.sections.map(section => section.id)).toEqual(['intro'])

    w.existing.add('C:\\Users\\Rotem\\.jarvis\\venv\\Scripts\\python.exe')
    await $.command.run({ command: 'jarvis', args: 'restart', origin: { kind: 'composer' }, presentation: { isFullscreen: false, columns: 80 } })
    await w.settle()
    w.lastHelper().hello()
    await w.settle()
    const after = await $.prompt.compose(COMPOSE_INPUT)
    expect(after.sections.map(section => section.id)).toEqual(['intro', PERSONA_SECTION_ID])
    const persona = after.sections[1]
    expect(persona?.scope).toBe('session')
    expect(persona?.text).toContain('[Jarvis voice]')
    expect(persona?.text).toContain('PowerShell')
    expect(persona?.text).toContain('one to three short sentences')

    // A voice turn whose note rode along keeps the very same system prompt.
    await speak(w, w.lastHelper(), 'Status report')
    await startTurn($, w, 'Status report', 'turn-4')
    const during = await $.prompt.compose(COMPOSE_INPUT)
    expect(during).toEqual(after)
  })

  test('without the note, the voice turn gets a section of its own', async ($, on) => {
    const w = world(on)
    const helper = await startHelper($, w)
    await speak(w, helper, 'Status report')
    await $.turn.start({ text: 'Status report', turnId: 'turn-5' }) // no UserPromptSubmit
    await w.settle()
    const during = await $.prompt.compose(COMPOSE_INPUT)
    expect(during.sections.map(section => section.id)).toEqual(['intro', PERSONA_SECTION_ID, VOICE_TURN_SECTION_ID])
    await completeTurn($, 'turn-5')
    const afterwards = await $.prompt.compose(COMPOSE_INPUT)
    expect(afterwards.sections.map(section => section.id)).toEqual(['intro', PERSONA_SECTION_ID])
  })

  test('barge_in aborts the running voice turn and stops speech', async ($, on) => {
    const w = world(on)
    const helper = await startHelper($, w)
    await speak(w, helper, 'Tell me a long story')
    await startTurn($, w, 'Tell me a long story', 'turn-6')
    await runStep($, w, 'turn-6', textChunks('Once upon a time, sir. There was a build. '))
    helper.event({ type: 'speech_started', replyId: 'turn-6' })
    helper.event({ type: 'barge_in', replyId: 'turn-6', spokenText: 'Once upon a time, sir.' })
    await w.settle()
    expect(w.aborts).toEqual(['turn-6'])
    expect(w.named('stop').map(command => command.body)).toEqual([{ reason: 'barge_in' }])

    // The aborted turn's end sends nothing more for that reply.
    const speaksBefore = w.named('speak').length
    await completeTurn($, 'turn-6', true)
    await w.settle()
    expect(w.named('speak')).toHaveLength(speaksBefore)
    expect(w.named('stop')).toHaveLength(1)
  })

  test('an interrupted voice turn (Esc) silences what was queued', async ($, on) => {
    const w = world(on)
    const helper = await startHelper($, w)
    await speak(w, helper, 'Explain monads')
    await startTurn($, w, 'Explain monads', 'turn-7')
    await runStep($, w, 'turn-7', textChunks('A monad is a pattern. '))
    await completeTurn($, 'turn-7', true)
    await w.settle()
    expect(w.named('stop').map(command => command.body)).toEqual([{ reason: 'turn_aborted' }])
    expect(spoken(w).some(one => one.final)).toBe(false)
  })
})

describe('spoken stop', () => {
  test('"Jarvis, stop." stops the reply and sends no prompt', async ($, on) => {
    const w = world(on)
    const helper = await startHelper($, w)
    await speak(w, helper, 'Tell me a long story')
    await startTurn($, w, 'Tell me a long story', 'turn-8')
    await runStep($, w, 'turn-8', textChunks('Once upon a time, sir. '))
    helper.event({ type: 'utterance', id: 'u2', text: 'Jarvis, stop.', source: 'wake', durationMs: 900, language: 'en' })
    await w.settle()
    expect(w.submits).toHaveLength(1)
    expect(w.aborts).toEqual(['turn-8'])
    expect(w.named('stop').map(command => command.body)).toEqual([{ reason: 'voice' }])
    expect(w.logs).toContain('Jarvis: stopped ("Jarvis, stop.")')
  })

  test('stop phrases, and words that only contain them', () => {
    for (const said of ['Stop.', 'stop it', 'Jarvis, stand down.', 'Never mind, thanks.', "That's enough, Jarvis.", 'Hey Jarvis cancel that', 'Quiet please'])
      expect(isStopPhrase(said)).toBe(true)
    for (const said of ['Stop the build.', 'Cancel my 3 pm meeting', 'Never mind the tests, ship it', 'Is that enough?', ''])
      expect(isStopPhrase(said)).toBe(false)
  })
})
