// Model routing for voice turns: Sonnet answers by default, so the first
// words come quickly; Opus takes complex requests and Fable the hardest.
// The user's own words decide first ("use Opus", "think hard"), else a quick
// Haiku judgement of the request. Typed turns and subagents keep the
// session's model, and so does a long conversation: the routed models have
// the standard context window, and moving one there re-reads it uncached.

import type { ModelCompleteRequest, ModelCompleteResult, TurnCompleteInput, TurnStepInput, TurnStepResult } from 'claude-code'

import type { Engine } from './engine'
import { describeError, TIMEOUT, withTimeout } from './engine'

export const ROUTING_MODES = ['auto', 'off'] as const
export type RoutingMode = (typeof ROUTING_MODES)[number]

export type Tier = 'simple' | 'complex' | 'hardest'
export type Family = 'sonnet' | 'opus' | 'fable'

/**
 * The model id a step names for each family. `turn.step` hands the name to
 * the API as it is, so an alias such as `sonnet` is refused there and a full
 * id is needed. (`$.model.complete` resolves aliases, so the judge keeps one.)
 */
export const MODEL_IDS: Record<Family, string> = {
  sonnet: 'claude-sonnet-5-5',
  opus: 'claude-opus-5-5',
  fable: 'claude-fable-5-1',
}
export const FAMILY_NAMES: Record<Family, string> = { sonnet: 'Sonnet', opus: 'Opus', fable: 'Fable' }
/** The families a tier tries, in order: the hardest requests fall back to Opus where Fable is not offered. */
const TIER_FAMILIES: Record<Tier, readonly Family[]> = { simple: ['sonnet'], complex: ['opus'], hardest: ['fable', 'opus'] }
const SPOKEN_TIERS: Record<Family, Tier> = { sonnet: 'simple', opus: 'complex', fable: 'hardest' }

/** A voice turn is routed only while the conversation is at most this many tokens. */
export const ROUTE_LIMIT_TOKENS = 100_000
/** A later step stays routed only while the step before it was at most this many: the routed models' window is 200,000. */
export const STEP_LIMIT_TOKENS = 160_000

/** The judge: small and fast. */
export const JUDGE_MODEL = 'haiku'
/** How long the first step of a voice turn waits for the judgement, counted from the moment the user's words arrived. */
export const JUDGE_WAIT_MS = 1200
const JUDGE_TIMEOUT_MS = 8000

export const JUDGE_SYSTEM = [
  'You route a request that a user spoke to Claude Code (an AI assistant that codes and runs tools on their computer) to a model. Reply with exactly one word: simple, complex or hardest.',
  'simple: conversation, questions answered from general knowledge or a quick look, status checks, opening an app or a file, one small edit or command.',
  'complex: multi-step coding or debugging, changes across several files, writing substantial code or documents, research that needs many tool calls, planning a feature, reviewing code.',
  'hardest: deep architecture or system design, subtle bugs (concurrency, security, memory), large migrations or refactors, novel algorithms, anything the user calls very hard or critical to get exactly right.',
  'A short follow-up ("yes", "do it", "go ahead", "continue") has the level of the previous request.',
].join('\n')

const SPOKEN_MODEL = /\b(?:use|using|with|on|switch to|ask|try|make|have|let|get)\s+(sonnet|opus|fable)\b/i
const THINK_HARDEST = /\b(?:ultrathink|think (?:as hard as (?:you|ye) can|really hard|very hard|hardest))\b/i
const THINK_HARD = /\bthink (?:hard|harder|carefully|deeply|it through|this through)\b/i

/** The tier the user asked for in so many words ("use Opus", "think hard"), if any. */
export function spokenTier(text: string): Tier | undefined {
  const named = SPOKEN_MODEL.exec(text)?.[1]?.toLowerCase() as Family | undefined
  if (named !== undefined) return SPOKEN_TIERS[named]
  if (THINK_HARDEST.test(text)) return 'hardest'
  if (THINK_HARD.test(text)) return 'complex'
  return undefined
}

/** The judge's answer as a tier; undefined when it named none. */
export function parseTier(answer: string): Tier | undefined {
  const word = answer.trim().toLowerCase().match(/[a-z]+/)?.[0]
  return word === 'simple' || word === 'complex' || word === 'hardest' ? word : undefined
}

/** The family a tier's steps run on: the first it tries that was not refused this session, if any. */
export function familyFor(tier: Tier, refused: ReadonlySet<Family> = new Set()): Family | undefined {
  return TIER_FAMILIES[tier].find(family => !refused.has(family))
}

/** The model a step names for `family`, or undefined to keep the session's: it already runs that family. */
export function stepModel(sessionModel: string, family: Family): string | undefined {
  return sessionModel.toLowerCase().includes(family) ? undefined : MODEL_IDS[family]
}

export type RouterOptions = {
  /** auto or off: /jarvis routing's choice, else the setting. */
  mode: () => Promise<RoutingMode>
  /** One completion through the session's own client ($.model.complete). */
  complete: (request: ModelCompleteRequest) => Promise<ModelCompleteResult>
}

type Judgement = {
  key: string
  text: string
  startedAt: number
  /** Known once the words or the judge decided. */
  tier: Tier | undefined
  done: Promise<Tier>
}

type RoutedTurn = {
  judgement: Judgement
  /** The family and model the last step named; undefined when it kept the session's. */
  family: Family | undefined
  model: string | undefined
  isAnnounced: boolean
  /** The conversation's size at the last step, in tokens. */
  tokens: number
  /** A routed step got no response from a family that never answered: it may not be offered. */
  failed: Family | undefined
}

const normalize = (text: string): string => text.replace(/\s+/g, ' ').trim()
/** Enough of a request to judge it. */
const clip = (text: string): string => (text.length > 500 ? `${text.slice(0, 500)}…` : text)

export class ModelRouter {
  /** Utterances judged and not yet started as turns, oldest first. */
  private pending: Judgement[] = []
  private turns = new Map<string, RoutedTurn>()
  private previous: { text: string; tier: Tier } | undefined
  /** Models that have answered a routed step this session. */
  private answered = new Set<string>()
  /** Families Claude Code could not answer on this session: their requests go elsewhere. */
  private refused = new Set<Family>()
  /** Whether the transcript already says the conversation is too long to route. */
  private hasSaidLong = false

  constructor(
    private readonly engine: Engine,
    private readonly options: RouterOptions,
  ) {}

  /** The user's words were heard: start judging them while the turn is set up. */
  async judge(text: string): Promise<void> {
    if ((await this.options.mode().catch(() => 'auto' as const)) === 'off' || (await this.isLong())) return
    const startedAt = await this.engine.now()
    const spoken = spokenTier(text)
    const judgement: Judgement = {
      key: normalize(text),
      text,
      startedAt,
      tier: spoken,
      done: Promise.resolve(spoken ?? 'simple'),
    }
    if (spoken === undefined) {
      judgement.done = this.ask(text).then(tier => {
        judgement.tier = tier
        return tier
      })
    }
    this.pending = [...this.pending, judgement].slice(-5)
  }

  /** A voice turn started with these words: its steps are routed. */
  onVoiceTurn(turnId: string, text: string): void {
    const key = normalize(text)
    const index = this.pending.findIndex(one => one.key === key)
    if (index === -1) return
    const judgement = this.pending[index] as Judgement
    this.pending = this.pending.slice(index + 1)
    this.turns.set(turnId, { judgement, family: undefined, model: undefined, isAnnounced: false, tokens: 0, failed: undefined })
  }

  /**
   * The model a main-loop step of a voice turn names; undefined keeps the
   * engine's. The first step waits a moment for the judgement; a later one
   * takes it as soon as it is in, unless the turn outgrew the routed window.
   */
  async modelFor(e: TurnStepInput): Promise<string | undefined> {
    const turn = e.agentId === undefined ? this.turns.get(e.turnId) : undefined
    if (turn === undefined) return undefined
    if (e.index > 0 && turn.tokens > STEP_LIMIT_TOKENS) {
      if (turn.model !== undefined) this.engine.debug(`jarvis: turn ${e.turnId} reached ${turn.tokens} tokens; the session's model takes it on`)
      turn.family = undefined
      turn.model = undefined
      return undefined
    }
    const { judgement } = turn
    let tier = judgement.tier
    if (tier === undefined && e.index === 0) {
      const waitMs = judgement.startedAt + JUDGE_WAIT_MS - (await this.engine.now())
      if (waitMs > 0) {
        const judged = await withTimeout(this.engine, judgement.done, waitMs).catch((): typeof TIMEOUT => TIMEOUT)
        tier = judged === TIMEOUT ? undefined : judged
      }
    }
    tier ??= 'simple'
    const family = familyFor(tier, this.refused)
    const model = family === undefined ? undefined : stepModel(e.model, family)
    turn.family = model === undefined ? undefined : family
    turn.model = model
    if (family !== undefined && tier !== 'simple' && !turn.isAnnounced) {
      turn.isAnnounced = true
      this.engine.log(`Jarvis: ${FAMILY_NAMES[family]} is taking this one.`)
    }
    if (model !== undefined) this.engine.debug(`jarvis: turn ${e.turnId} step ${e.index} → ${model} (${tier})`)
    return model
  }

  /** A step of a voice turn ended: its size, and whether a routed model answered. */
  onStepResult(e: TurnStepInput, result: TurnStepResult | undefined): void {
    const turn = e.agentId === undefined ? this.turns.get(e.turnId) : undefined
    if (turn === undefined) return
    const usage = result?.usage
    if (usage) turn.tokens = usage.input_tokens + usage.cache_read_input_tokens + usage.cache_creation_input_tokens + usage.output_tokens
    const { family, model } = turn
    if (family === undefined || model === undefined) return
    if (result !== undefined && result.stopReason !== null) this.answered.add(model)
    else if (!this.answered.has(model)) turn.failed = family
  }

  onTurnComplete(e: TurnCompleteInput): void {
    if (e.agentId !== undefined) return
    const turn = this.turns.get(e.turnId)
    if (turn === undefined) return
    this.turns.delete(e.turnId)
    const tier = turn.judgement.tier ?? 'simple'
    this.previous = { text: turn.judgement.text, tier }
    const { failed } = turn
    if (e.reason === 'error' && failed !== undefined && !this.refused.has(failed)) {
      this.refused.add(failed)
      const instead = failed === 'fable' ? 'Opus' : "your session's model"
      this.engine.log(
        `Jarvis: Claude Code could not answer on ${FAMILY_NAMES[failed]}, so ${instead} takes its voice requests for the rest of this session. Please say that again.`,
      )
    }
  }

  /** Whether the conversation is too long to route; the transcript says so once each time it gets there. */
  private async isLong(): Promise<boolean> {
    let tokens: number | undefined
    try {
      tokens = await this.engine.contextTokens()
    } catch (error) {
      this.engine.debug(`jarvis: model routing skipped: no context size (${describeError(error)})`)
      return true
    }
    if ((tokens ?? 0) <= ROUTE_LIMIT_TOKENS) {
      this.hasSaidLong = false
      return false
    }
    this.engine.debug(`jarvis: model routing skipped: the conversation is ${tokens} tokens`)
    if (!this.hasSaidLong) {
      this.hasSaidLong = true
      this.engine.log(
        "Jarvis: this conversation is long, so your session's model answers voice requests. Sonnet, Opus and Fable take over again after /compact or /clear, or in a new session.",
      )
    }
    return true
  }

  private async ask(text: string): Promise<Tier> {
    const previous = this.previous
    const context = previous === undefined ? '' : `Previous request (${previous.tier}): ${clip(previous.text)}\n\n`
    try {
      const result = await this.options.complete({
        model: JUDGE_MODEL,
        system: JUDGE_SYSTEM,
        prompt: `${context}Request: ${clip(text)}`,
        maxTokens: 5,
        timeoutMs: JUDGE_TIMEOUT_MS,
      })
      if (!result.isAnswered) {
        this.engine.debug(`jarvis: the model judge gave no answer (${result.reason})`)
        return 'simple'
      }
      return parseTier(result.text) ?? 'simple'
    } catch (error) {
      this.engine.debug(`jarvis: the model judge failed: ${describeError(error)}`)
      return 'simple'
    }
  }
}
