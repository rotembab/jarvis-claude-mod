import { describe, expect, test } from 'claude-code/testing'
import type { ModelCompleteResult } from 'claude-code'
import type { Engine as TestEngine } from 'claude-code/testing'

import type { FakeChild, World } from './test-harness'
import { answered, completeTurn, failTurn, jarvis, runStep, startHelper, textChunks, world } from './test-harness'
import { JUDGE_MODEL, JUDGE_WAIT_MS, parseTier, spokenTier, stepModel } from './router'

/** The user says `text`: the helper's utterance, then the turn the engine starts for it. */
async function voiceTurn($: TestEngine, w: World, helper: FakeChild, text: string, turnId: string): Promise<void> {
  helper.event({ type: 'utterance', id: turnId, text, source: 'wake', durationMs: 1500, language: 'en' })
  await w.settle()
  await $.classic.UserPromptSubmit({ prompt: text })
  await $.turn.start({ text, turnId })
  await w.settle()
}

/** The model each step of `turnId` named when it reached the engine. */
const models = (w: World, turnId: string) => w.stepInputs.filter(one => one.turnId === turnId).map(one => one.model)

describe('model routing', () => {
  test('a voice request is answered on Sonnet when the judge calls it simple', async ($, on) => {
    const w = world(on)
    const helper = await startHelper($, w)
    await voiceTurn($, w, helper, 'What time is it in Tokyo?', 't1')
    await runStep($, w, 't1', textChunks('Half past nine, sir.'))
    expect(models(w, 't1')).toEqual(['sonnet'])
    expect(w.completions).toHaveLength(1)
    expect(w.completions[0]).toMatchObject({ model: JUDGE_MODEL, prompt: 'Request: What time is it in Tokyo?', maxTokens: 5 })
    expect(w.logs.filter(line => line.includes('taking this one'))).toEqual([])
  })

  test('a complex request goes to Opus for every step, and the transcript says so', async ($, on) => {
    const w = world(on)
    w.complete = () => answered('complex')
    const helper = await startHelper($, w)
    await voiceTurn($, w, helper, 'Refactor the auth module and fix the failing tests', 't1')
    await runStep($, w, 't1', textChunks('Looking at the tests now.'))
    await runStep($, w, 't1', textChunks('Done.'), { index: 1 })
    expect(models(w, 't1')).toEqual(['opus', 'opus'])
    expect(w.logs).toContain('Jarvis: Opus is taking this one.')
  })

  test('the user can name the model or ask for hard thinking; no judge is asked', async ($, on) => {
    const w = world(on)
    const helper = await startHelper($, w)
    await voiceTurn($, w, helper, 'Use Fable to design the sync engine', 't1')
    await runStep($, w, 't1', textChunks('Very good.'))
    await completeTurn($, 't1')
    await voiceTurn($, w, helper, 'Think hard about why the cache misses', 't2')
    await runStep($, w, 't2', textChunks('Right.'))
    expect(models(w, 't1')).toEqual(['fable'])
    expect(models(w, 't2')).toEqual(['opus'])
    expect(w.completions).toEqual([])
  })

  test('typed turns and subagents keep the session model', async ($, on) => {
    const w = world(on)
    w.complete = () => answered('hardest')
    const helper = await startHelper($, w)
    await $.turn.start({ text: 'typed request', turnId: 'typed' })
    await runStep($, w, 'typed', textChunks('Sure.'))
    await completeTurn($, 'typed')
    await voiceTurn($, w, helper, 'Migrate the database layer', 't1')
    await runStep($, w, 't1', [], { agentId: 'agent-1' })
    expect(models(w, 'typed')).toEqual(['claude-test'])
    expect(w.stepInputs.find(one => one.agentId === 'agent-1')?.model).toBe('claude-test')
    expect(w.completions).toHaveLength(1) // the voice request only
  })

  test('a slow judge leaves the first step on Sonnet and moves the next one', async ($, on) => {
    const w = world(on)
    let judged: (result: ModelCompleteResult) => void = () => undefined
    w.complete = () => new Promise(resolve => (judged = resolve))
    const helper = await startHelper($, w)
    await voiceTurn($, w, helper, 'Find out why the nightly build is slow', 't1')
    const first = runStep($, w, 't1', textChunks('Checking the build logs.'))
    await w.clock.advance(JUDGE_WAIT_MS)
    await first
    judged(answered('complex'))
    await w.settle()
    await runStep($, w, 't1', textChunks('Found it.'), { index: 1 })
    expect(models(w, 't1')).toEqual(['sonnet', 'opus'])
  })

  test('the session model is kept when it is already the right family, and its 1M context is kept', () => {
    expect(stepModel('claude-opus-5-5[1m]', 'complex')).toBeUndefined()
    expect(stepModel('claude-opus-5-5[1m]', 'simple')).toBe('sonnet[1m]')
    expect(stepModel('claude-sonnet-5-5', 'simple')).toBeUndefined()
    expect(stepModel('claude-sonnet-5-5', 'hardest')).toBe('fable')
  })

  test('the follow-up of a complex request is judged with it as context', async ($, on) => {
    const w = world(on)
    w.complete = () => answered('complex')
    const helper = await startHelper($, w)
    await voiceTurn($, w, helper, 'Plan the new billing service', 't1')
    await runStep($, w, 't1', textChunks('Here is the plan.'))
    await completeTurn($, 't1')
    await voiceTurn($, w, helper, 'Go ahead', 't2')
    expect(w.completions[1]?.prompt).toBe('Previous request (complex): Plan the new billing service\n\nRequest: Go ahead')
  })

  test('/jarvis routing off leaves voice requests on the session model', async ($, on) => {
    const w = world(on)
    const helper = await startHelper($, w)
    expect(await jarvis($, 'routing off')).toBe("Your session's model answers voice requests.")
    await voiceTurn($, w, helper, 'What is on my calendar?', 't1')
    await runStep($, w, 't1', textChunks('Nothing today.'))
    expect(models(w, 't1')).toEqual(['claude-test'])
    expect(w.completions).toEqual([])
    expect(await jarvis($, 'routing')).toContain("Voice requests: your session's model")
  })

  test('a model Claude Code refuses turns routing off for the session', async ($, on) => {
    const w = world(on)
    const helper = await startHelper($, w)
    await voiceTurn($, w, helper, 'Hello there', 't1')
    w.failedSteps.add('t1:0')
    await runStep($, w, 't1', [])
    await failTurn($, 't1')
    await w.settle()
    expect(w.logs.some(line => line.includes('model routing is off for this session'))).toBe(true)
    await voiceTurn($, w, helper, 'Hello again', 't2')
    await runStep($, w, 't2', textChunks('Hello, sir.'))
    expect(models(w, 't2')).toEqual(['claude-test'])
  })

  test('an API error on a model that already answered leaves routing on', async ($, on) => {
    const w = world(on)
    const helper = await startHelper($, w)
    await voiceTurn($, w, helper, 'Hello there', 't1')
    await runStep($, w, 't1', textChunks('Good evening, sir.'))
    await completeTurn($, 't1')
    await voiceTurn($, w, helper, 'And the weather?', 't2')
    w.failedSteps.add('t2:0')
    await runStep($, w, 't2', [])
    await failTurn($, 't2')
    await voiceTurn($, w, helper, 'The weather, please', 't3')
    await runStep($, w, 't3', textChunks('Rain, sir.'))
    expect(models(w, 't3')).toEqual(['sonnet'])
    expect(w.logs.some(line => line.includes('model routing is off'))).toBe(false)
  })

  test('spoken choices and judge answers', () => {
    expect(spokenTier('Jarvis, use Opus for this')).toBe('complex')
    expect(spokenTier('switch to sonnet and list the files')).toBe('simple')
    expect(spokenTier('ultrathink: why does this deadlock?')).toBe('hardest')
    expect(spokenTier('Think carefully before you delete anything')).toBe('complex')
    expect(spokenTier('Tell me about the opus magnum')).toBeUndefined()
    expect(spokenTier('What is a fable?')).toBeUndefined()
    expect(parseTier('Complex.')).toBe('complex')
    expect(parseTier(' hardest')).toBe('hardest')
    expect(parseTier('I think this is simple')).toBeUndefined()
  })
})
