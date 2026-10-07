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
    '- Home devices (the home_control tool): a command marked * in its device list waits for the user\'s OK in a dialog on screen, and a spoken "yes" does not count, so say that before you run it.',
    '- Voice changes how you talk, not what you can do: use your tools exactly as you would for a typed request.',
    platform.personaOsParagraph,
  ].join('\n')
}

const normalize = (text: string): string => text.replace(/\s+/g, ' ').trim()

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

/** True when the words are only a request to stop ("Jarvis, stop.", "Never mind, thanks."). */
export function isStopPhrase(text: string): boolean {
  const words = text
    .toLowerCase()
    .replace(/[’']/g, '')
    .replace(/[^a-z\s]/g, ' ')
    .split(/\s+/)
    .filter(word => word !== '')
  while (words.length > 0 && ['hey', 'ok', 'okay', 'jarvis'].includes(words[0] ?? '')) words.shift()
  while (words.length > 0 && ['jarvis', 'please', 'thanks', 'now'].includes(words[words.length - 1] ?? '')) words.pop()
  if (words.length > 1 && words[words.length - 2] === 'thank' && words[words.length - 1] === 'you') words.splice(-2)
  return STOP_PHRASES.has(words.join(' '))
}

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

type PendingPrompt = { key: string; hasNote: boolean }
type VoiceTurn = { turnId: string; reply: VoiceReply; needsTurnSection: boolean }

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
  private turn: VoiceTurn | undefined
  private speakingReplyId: string | undefined
  /** Sticky once voice is in use, so the system prompt stays the same afterwards. */
  private isPersonaOn = false

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
        void this.interrupt('barge_in')
        return
      default:
        return
    }
  }

  /** Submits the user's words as their own prompt; queued if a turn runs. */
  async onUtterance(event: UtteranceEvent): Promise<void> {
    const text = event.text.trim()
    if (text === '') return
    if (isStopPhrase(text)) {
      await this.interrupt('voice')
      this.engine.log(`Jarvis: stopped ("${text}")`)
      return
    }
    this.enablePersona()
    // Speaking over Jarvis means "stop and listen to me", not "queue this".
    const turn = this.turn
    if (turn !== undefined && turn.turnId === this.runningTurnId && this.speakingReplyId === turn.reply.replyId) {
      await this.interrupt('barge_in')
    }
    const entry: PendingPrompt = { key: normalize(text), hasNote: false }
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
    this.turn = {
      turnId: e.turnId,
      reply: new VoiceReply(e.turnId, this.options.helper, line => this.engine.debug(`jarvis: ${line}`)),
      needsTurnSection: entry?.hasNote !== true,
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
