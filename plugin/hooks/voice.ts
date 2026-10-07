// Voice turns: an utterance from the helper becomes a prompt in the user's
// own words; the reply to it streams back to the helper sentence by
// sentence; the JARVIS persona applies to those turns and no others.

import type {
  ClassicResultOf,
  HookStream,
  PromptComposeResult,
  PromptComposeSection,
  TurnCompleteInput,
  TurnStartInput,
  TurnStepChunk,
  TurnStepInput,
  TurnStepResult,
} from 'claude-code'

import type { Engine } from './engine'
import { describeError } from './engine'
import type { CommandOutcome } from './helper'
import type { Platform } from './platform'
import type { CommandBodies, HelperEvent, UtteranceEvent } from './protocol'
import { SPEAK_TEXT_MAX } from './protocol'
import type { ModelRouter } from './router'
import { SentenceSplitter } from './sentences'

/** What voice needs of the helper: sending commands. */
export type CommandSender = {
  send: <N extends 'speak' | 'stop'>(name: N, body: CommandBodies[N]) => Promise<CommandOutcome>
}

export type VoiceOptions = {
  platform: Platform
  helper: CommandSender
  /** Told when the user's words were submitted (the band shows them). */
  onSubmitted?: (text: string) => void
  /** Picks the model for each step of a voice turn. */
  router?: ModelRouter
  /** "Stand down": stops speech, the running turn and the commands Jarvis saw start (pc.ts). */
  onStandDown?: () => Promise<unknown>
  /** True while an on-screen question waits for a click: a spoken yes or no then counts for nothing. */
  isAwaitingClick?: () => boolean
}

/** The note a voice prompt carries (classic UserPromptSubmit additionalContext). */
export const VOICE_NOTE =
  '[Jarvis voice] The user spoke this message aloud (local speech-to-text, so a word may be misheard). Your reply will be read aloud: follow the "Jarvis voice mode" section of the system prompt.'

/** Fallback when the note could not ride with the prompt: a section for this turn only. */
export const VOICE_TURN_SECTION_ID = 'jarvis:voice-turn'
const VOICE_TURN_TEXT =
  'Jarvis voice mode is on for the current user message: it was spoken aloud and your reply will be read aloud. Follow the "Jarvis voice mode" section above.'

export const PERSONA_SECTION_ID = 'jarvis:voice'

/** The persona: session-wide and constant (cache friendly), applied per message by the note. */
export function personaText(platform: Platform): string {
  return [
    '# Jarvis voice mode',
    'The user can talk to you out loud through Jarvis, a voice add-on for Claude Code: they say "Hey Jarvis" (or hold a push-to-talk key), speak, and local speech-to-text turns their words into the message you receive. They can talk over you to interrupt. A message that arrived this way carries a note starting "[Jarvis voice]", or this prompt says voice mode is on for the current message. Every other message was typed: answer typed messages in your usual style and ignore this section for them.',
    '',
    "Your reply to a voice message is spoken by a text-to-speech voice as you write it, so answer as JARVIS, the user's composed AI butler:",
    '- Dry, British, unflappable, quietly witty, never gushing. Call the user "sir" sparingly: at most once in a reply, and not in every reply.',
    '- Speak one to three short sentences, the answer first. No preamble and no recap of the question.',
    '- When you need tools before you can answer (reading files, running commands, searching), first say one short sentence about what you are doing ("Checking the build logs now.") and then call the tools, so the user is not left in silence. Skip it when you can answer straight away.',
    '- Write plain spoken English: no markdown, lists, tables, emoji, URLs or file paths in the spoken part. Say numbers and symbols as a person would.',
    '- Longer material (code, diffs, logs, lists, tables, long explanations) still belongs on screen: include it as usual, then say in one sentence where it is ("The script is on screen, sir.") instead of reading it out. Code blocks and tables are never read aloud.',
    '- Before an action that deletes or overwrites files, runs commands with side effects, installs software, spends money or contacts anyone, say plainly what it will do.',
    '- Speech-to-text can mishear names and commands. If a request is ambiguous and acting on it would be risky, ask one short question instead of guessing.',
    '- Voice changes how you talk, not what you can do: use your tools exactly as you would for a typed request.',
    platform.personaOsParagraph,
  ].join('\n')
}

const normalize = (text: string): string => text.replace(/\s+/g, ' ').trim()

/** A reply id no earlier reply had, across reloads too. */
const freshId = (): string => Array.from(crypto.getRandomValues(new Uint8Array(8)), byte => byte.toString(16).padStart(2, '0')).join('')

/** Said on its own, these just stop Jarvis: no prompt is sent. */
const STOP_PHRASES: ReadonlySet<string> = new Set([
  'stop',
  'stop it',
  'stop talking',
  'stand down',
  'never mind',
  'nevermind',
  'cancel',
  'cancel that',
  'quiet',
  'be quiet',
  'silence',
  'enough',
  'thats enough',
  'shut up',
  'hush',
])

/**
 * A short spoken phrase as plain words: lower case, no punctuation, without
 * the words around it ("Hey Jarvis, ...", "..., please", "..., thank you").
 * Its last word always stays, so "Okay." is still "okay".
 */
export function phraseWords(text: string): string {
  const words = text
    .toLowerCase()
    .replace(/[’']/g, '')
    .replace(/[^a-z\s]/g, ' ')
    .split(/\s+/)
    .filter(word => word !== '')
  for (;;) {
    if (words.length > 2 && words[words.length - 2] === 'thank' && words[words.length - 1] === 'you') words.splice(-2)
    else if (words.length > 1 && ['jarvis', 'please', 'thanks', 'now'].includes(words[words.length - 1] ?? '')) words.pop()
    else break
  }
  while (words.length > 1 && ['hey', 'ok', 'okay', 'jarvis'].includes(words[0] ?? '')) words.shift()
  return words.join(' ')
}

/** True when the words are only a request to stop ("Jarvis, stop.", "Never mind, thanks."). */
export function isStopPhrase(text: string): boolean {
  return STOP_PHRASES.has(phraseWords(text))
}

/** Said on its own, these stand Jarvis down: speech, the running turn and the commands he saw start all stop. */
const STAND_DOWN_PHRASES: ReadonlySet<string> = new Set(['stand down', 'abort', 'abort that', 'abort it'])

/** True for "Jarvis, stand down." and "Abort that." ("stand down" is also a stop phrase, for a voice without PC control). */
export function isStandDownPhrase(text: string): boolean {
  return STAND_DOWN_PHRASES.has(phraseWords(text))
}

/** A spoken OK, said on its own: what lets Jarvis run a command he held for it. */
const YES_PHRASES: ReadonlySet<string> = new Set([
  'yes',
  'yeah',
  'yep',
  'yes please',
  'go ahead',
  'go for it',
  'do it',
  'proceed',
  'confirm',
  'confirmed',
  'affirmative',
  'sure',
  'ok',
  'okay',
])

/** True when the words are only a yes ("Yes.", "Go ahead, Jarvis."); "Yes, and delete dist too" is not. */
export function isYesPhrase(text: string): boolean {
  return YES_PHRASES.has(phraseWords(text))
}

const NO_PHRASES: ReadonlySet<string> = new Set(['no', 'nope', 'nah', 'no thanks', 'dont', 'dont do it', 'do not', 'negative'])

/** True when the words are only a no ("No.", "Don't do it."). */
export function isNoPhrase(text: string): boolean {
  return NO_PHRASES.has(phraseWords(text))
}

/** Said when a spoken answer comes while an on-screen question waits. */
const CLICK_NEEDED = 'I need a click on screen for that one, sir.'

/** One spoken reply: sentences of one voice turn, numbered for the helper. */
export class VoiceReply {
  private readonly splitter = new SentenceSplitter()
  private seq = 0
  private block: string | undefined
  private chain: Promise<void> = Promise.resolve()
  private isCancelled = false
  private isFinished = false

  constructor(
    readonly replyId: string,
    private readonly helper: CommandSender,
    private readonly debug: (line: string) => void,
  ) {}

  get cancelled(): boolean {
    return this.isCancelled
  }

  /** Feeds a text delta of block `block` (a new block ends the previous line). */
  feed(text: string, block: string): void {
    if (this.isCancelled || this.isFinished) return
    if (this.block !== undefined && block !== this.block) this.sendAll(this.splitter.push('\n'))
    this.block = block
    this.sendAll(this.splitter.push(text))
  }

  /** Ends the current line now (a step ended; tools may run for a while). */
  endLine(): void {
    if (this.isCancelled || this.isFinished) return
    this.sendAll(this.splitter.push('\n'))
  }

  /** The turn ended: speak what is left and close the reply. */
  finish(): Promise<void> {
    if (!this.isCancelled && !this.isFinished) {
      this.sendAll(this.splitter.flush())
      this.enqueue('', true)
      this.isFinished = true
    }
    return this.chain
  }

  /** Drops everything not yet sent; nothing more is spoken for this reply. */
  cancel(): void {
    this.isCancelled = true
  }

  /** Resolves once every queued speak call has been sent. */
  settled(): Promise<void> {
    return this.chain
  }

  private sendAll(sentences: string[]): void {
    for (const sentence of sentences) this.enqueue(sentence, false)
  }

  // Sends are chained so the helper receives them in seq order.
  private enqueue(text: string, final: boolean): void {
    const body = { replyId: this.replyId, seq: this.seq, text: text.slice(0, SPEAK_TEXT_MAX), final }
    this.seq += 1
    this.chain = this.chain.then(async () => {
      if (this.isCancelled) return
      const outcome = await this.helper.send('speak', body)
      if (!outcome.ok) this.debug(`speak ${body.seq} not delivered: ${outcome.message}`)
    })
  }
}

/**
 * What a voice turn's words were, for a command Jarvis held for a spoken OK:
 * a plain yes, or a plain yes said over Jarvis's own speech (which may be a
 * TV, and is not taken as one).
 */
export type VoiceConsent = 'yes' | 'yes_while_talking'

type PendingPrompt = { key: string; hasNote: boolean; heardWhileTalking: boolean }
type VoiceTurn = { turnId: string; reply: VoiceReply; needsTurnSection: boolean; consent?: VoiceConsent }

/** What an interrupt did: whether it aborted the voice turn, and what the helper said to `stop`. */
export type InterruptResult = {
  abortedTurn: boolean
  /** stopped: speech stopped; silent: nothing was playing; not_running / no_answer: the helper was not reached. */
  speech: 'stopped' | 'silent' | 'not_running' | 'no_answer'
}

export class Voice {
  /** Voice prompts submitted and not yet started, oldest first. */
  private pending: PendingPrompt[] = []
  private runningTurnId: string | undefined
  /** The latest main-loop turn to start, and the one before it (typed or spoken). */
  private lastTurnId: string | undefined
  private turnBefore: string | undefined
  private turn: VoiceTurn | undefined
  private speakingReplyId: string | undefined
  /** Sticky once voice is in use, so the system prompt stays the same afterwards. */
  private isPersonaOn = false
  /** A barge_in came since the last utterance: the next one was said over Jarvis. */
  private bargeSinceUtterance = false
  private sayCount = 0

  constructor(
    private readonly engine: Engine,
    private readonly options: VoiceOptions,
  ) {}

  get voiceTurnId(): string | undefined {
    return this.turn?.turnId
  }

  get isTurnRunning(): boolean {
    return this.runningTurnId !== undefined
  }

  /** The running turn's id, typed or spoken. */
  get runningTurn(): string | undefined {
    return this.runningTurnId
  }

  /** The main-loop turn that started just before the latest one, typed or spoken. */
  get previousTurn(): string | undefined {
    return this.turnBefore
  }

  /** Whether the voice turn's words were only a yes, and how it was heard; undefined for anything else. */
  get voiceTurnConsent(): VoiceConsent | undefined {
    return this.turn?.consent
  }

  /**
   * Speaks a line of Jarvis's own (a notice, a timer). Inside a voice turn it
   * is a line of that turn's reply: a second reply queued mid-turn would make
   * the helper close the turn's reply early. Otherwise it is a reply of its
   * own (not a `test-` one, so the helper listens for an answer after it).
   */
  say(text: string): void {
    const turn = this.turn
    if (turn !== undefined && !turn.reply.cancelled) {
      this.sayCount += 1
      turn.reply.endLine()
      turn.reply.feed(text, `jarvis:say:${this.sayCount}`)
      turn.reply.endLine()
      return
    }
    const body = { replyId: `jarvis-say-${freshId()}`, seq: 0, text: text.slice(0, SPEAK_TEXT_MAX), final: true }
    void this.options.helper.send('speak', body).then(outcome => {
      if (!outcome.ok) this.engine.debug(`jarvis: "${text}" not spoken: ${outcome.message}`)
    })
  }

  /** Turns the persona section on for the rest of the session. */
  enablePersona(): void {
    this.isPersonaOn = true
  }

  onHelperEvent(event: HelperEvent): void {
    switch (event.type) {
      case 'hello':
        this.enablePersona()
        return
      case 'utterance':
        void this.onUtterance(event)
        return
      case 'speech_started':
        this.speakingReplyId = event.replyId
        return
      case 'speech_done':
        if (this.speakingReplyId === event.replyId) this.speakingReplyId = undefined
        return
      case 'barge_in':
        this.bargeSinceUtterance = true
        void this.interrupt('barge_in')
        return
      default:
        return
    }
  }

  /** Submits the user's words as their own prompt; queued if a turn runs. */
  async onUtterance(event: UtteranceEvent): Promise<void> {
    // Talking over Jarvis raises barge_in before the words arrive; push-to-talk
    // is a deliberate key press, which a TV cannot make.
    const heardWhileTalking = this.bargeSinceUtterance && event.source !== 'ptt'
    this.bargeSinceUtterance = false
    const text = event.text.trim()
    if (text === '') return
    const onStandDown = this.options.onStandDown
    if (onStandDown !== undefined && isStandDownPhrase(text)) {
      await onStandDown().catch((error: unknown) => this.engine.debug(`jarvis: stand down failed: ${describeError(error)}`))
      return
    }
    if (isStopPhrase(text)) {
      await this.interrupt('voice')
      this.engine.log(`Jarvis: stopped ("${text}")`)
      return
    }
    if (this.options.isAwaitingClick?.() === true && (isYesPhrase(text) || isNoPhrase(text))) {
      // An on-screen question is open: a voice could be a TV's, so only a click answers it.
      this.engine.log(`Jarvis: "${text}" was not sent: the question on screen needs a click.`)
      this.say(CLICK_NEEDED)
      return
    }
    this.enablePersona()
    // Speaking over Jarvis means "stop and listen to me", not "queue this".
    const turn = this.turn
    if (turn !== undefined && turn.turnId === this.runningTurnId && this.speakingReplyId === turn.reply.replyId) {
      await this.interrupt('barge_in')
    }
    const entry: PendingPrompt = { key: normalize(text), hasNote: false, heardWhileTalking }
    this.pending = [...this.pending, entry].slice(-5)
    this.options.onSubmitted?.(text)
    // The model judgement runs while the engine sets the turn up.
    await this.options.router?.judge(text).catch((error: unknown) => {
      this.engine.debug(`jarvis: model routing skipped: ${describeError(error)}`)
    })
    const forget = (): void => {
      this.pending = this.pending.filter(one => one !== entry)
    }
    try {
      // Resolves once the turn started or was queued behind the running one: not awaited by callers.
      const result = await this.engine.submitPrompt(text)
      if (result.drop !== undefined) {
        // Refused (a hook beneath): no turn will start for it.
        forget()
        this.engine.log(`Jarvis: "${text}" was not sent: ${result.drop}`)
      }
    } catch (error) {
      forget()
      this.engine.debug(`jarvis: voice prompt not submitted: ${describeError(error)}`)
    }
  }

  /**
   * Stops speech and aborts the running voice turn (never a typed one).
   * Returns what it did, for /jarvis stop's output.
   */
  async interrupt(reason: string): Promise<InterruptResult> {
    const turn = this.turn
    let abortedTurn = false
    if (turn !== undefined) {
      turn.reply.cancel()
      if (turn.turnId === this.runningTurnId) {
        try {
          await this.engine.abortTurn(turn.turnId)
          abortedTurn = true
        } catch (error) {
          this.engine.debug(`jarvis: turn.abort failed: ${describeError(error)}`)
        }
      }
    }
    const outcome = await this.options.helper.send('stop', { reason })
    if (outcome.ok) {
      // The helper answers `stopped: false` when nothing was playing.
      return { abortedTurn, speech: outcome.response.stopped === false ? 'silent' : 'stopped' }
    }
    return { abortedTurn, speech: outcome.code === 'not_running' ? 'not_running' : 'no_answer' }
  }

  /** classic.UserPromptSubmit: a voice prompt (its exact words pending) gets the note the persona keys on. */
  markPrompt(
    prompt: string,
    result: ClassicResultOf['classic.UserPromptSubmit'],
  ): ClassicResultOf['classic.UserPromptSubmit'] {
    const key = normalize(prompt)
    const entry = this.pending.find(one => one.key === key && !one.hasNote)
    if (entry === undefined) return result
    entry.hasNote = true
    return { ...result, additionalContext: [...(result.additionalContext ?? []), VOICE_NOTE] }
  }

  onTurnStart(e: TurnStartInput): void {
    this.runningTurnId = e.turnId
    this.turnBefore = this.lastTurnId
    this.lastTurnId = e.turnId
    // A voice turn's text is exactly the words submitted. The mod's prompts
    // start in the order it submitted them, so this is the oldest pending
    // entry with those words, and older entries never started (refused or
    // removed from the queue). Containment is not enough: a typed "Yes. Also
    // delete dist." would take a pending "Yes." and be spoken.
    const key = normalize(e.text)
    const index = key === '' ? -1 : this.pending.findIndex(one => one.key === key)
    if (index === -1) {
      this.turn = undefined
      return
    }
    const entry = this.pending[index]
    this.pending = this.pending.slice(index + 1)
    this.options.router?.onVoiceTurn(e.turnId, e.text)
    const consent: VoiceConsent | undefined = !isYesPhrase(e.text) ? undefined : entry?.heardWhileTalking === true ? 'yes_while_talking' : 'yes'
    this.turn = {
      turnId: e.turnId,
      reply: new VoiceReply(e.turnId, this.options.helper, line => this.engine.debug(`jarvis: ${line}`)),
      needsTurnSection: entry?.hasNote !== true,
      ...(consent === undefined ? {} : { consent }),
    }
  }

  /**
   * turn.step: forwards every chunk unchanged; for the main loop's voice turn
   * it names the routed model and feeds the visible text to the reply. Tool
   * calls, their JSON input and thinking are never spoken.
   */
  async *step(
    e: TurnStepInput,
    next: (input: TurnStepInput) => HookStream<TurnStepChunk, TurnStepResult>,
  ): AsyncGenerator<TurnStepChunk, TurnStepResult> {
    const turn = e.agentId === undefined && this.turn?.turnId === e.turnId ? this.turn : undefined
    if (turn === undefined) return yield* next(e)
    const router = this.options.router
    let model: string | undefined
    try {
      model = await router?.modelFor(e)
    } catch (error) {
      this.engine.debug(`jarvis: model routing skipped: ${describeError(error)}`)
    }
    const input = model === undefined ? e : { ...e, model }
    const stream = next(input)
    let result: TurnStepResult | undefined
    try {
      result = yield* this.speakStep(input, turn, stream)
      return result
    } finally {
      router?.onStepResult(input, result)
    }
  }

  private async *speakStep(
    e: TurnStepInput,
    turn: VoiceTurn,
    stream: HookStream<TurnStepChunk, TurnStepResult>,
  ): AsyncGenerator<TurnStepChunk, TurnStepResult> {
    let lineOpen = false
    for await (const chunk of stream) {
      if (chunk.kind === 'text') {
        try {
          turn.reply.feed(chunk.text, `${e.index}:${chunk.index}`)
          lineOpen = true
        } catch (error) {
          this.engine.debug(`jarvis: reply feed failed: ${describeError(error)}`)
        }
      } else if (lineOpen) {
        // A tool call (or other block) started: speak the sentence before it
        // now rather than after the tool's whole input has streamed.
        lineOpen = false
        turn.reply.endLine()
      }
      yield chunk
    }
    turn.reply.endLine()
    return await stream.result
  }

  onTurnComplete(e: TurnCompleteInput): void {
    if (e.agentId !== undefined) return
    this.options.router?.onTurnComplete(e)
    const turn = this.turn
    if (turn !== undefined && turn.turnId === e.turnId) {
      this.turn = undefined
      if (e.isAborted) {
        // Interrupted (Esc, /jarvis stop, barge-in): silence what was queued.
        if (!turn.reply.cancelled) {
          turn.reply.cancel()
          void this.options.helper.send('stop', { reason: 'turn_aborted' })
        }
      } else {
        void turn.reply.finish()
      }
    }
    if (this.runningTurnId === e.turnId) this.runningTurnId = undefined
  }

  /** The reply in flight, for tests and status. */
  get reply(): VoiceReply | undefined {
    return this.turn?.reply
  }

  /** prompt.compose: the persona (constant) and, if the note did not ride along, this turn's flag. */
  compose(result: PromptComposeResult): PromptComposeResult {
    if (!this.isPersonaOn) return result
    const sections: PromptComposeSection[] = [
      ...result.sections,
      { id: PERSONA_SECTION_ID, text: personaText(this.options.platform), scope: 'session' },
    ]
    if (this.turn?.needsTurnSection === true) {
      sections.push({ id: VOICE_TURN_SECTION_ID, text: VOICE_TURN_TEXT, scope: 'session' })
    }
    return { sections }
  }
}
