// Model routing for voice turns: Sonnet answers by default, so the first
// words come quickly; Opus takes complex requests and Fable the hardest.
// The user's own words decide first ("use Opus", "think hard"), else a quick
// Haiku judgement of the request. Typed turns and subagents keep the
// session's model.

import type { ModelCompleteRequest, ModelCompleteResult, TurnCompleteInput, TurnStepInput, TurnStepResult } from 'claude-code'

import type { Engine } from './engine'
import { describeError, TIMEOUT, withTimeout } from './engine'

export const ROUTING_MODES = ['auto', 'off'] as const
export type RoutingMode = (typeof ROUTING_MODES)[number]

export type Tier = 'simple' | 'complex' | 'hardest'

/** The model alias each tier names. */
export const TIER_MODELS: Record<Tier, string> = { simple: 'sonnet', complex: 'opus', hardest: 'fable' }
export const TIER_NAMES: Record<Tier, string> = { simple: 'Sonnet', complex: 'Opus', hardest: 'Fable' }

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

const SPOKEN_MODEL = /\b(?:use|using|with|on|switch to|ask|try)\s+(sonnet|opus|fable)\b/i
const THINK_HARDEST = /\b(?:ultrathink|think (?:as hard as (?:you|ye) can|really hard|very hard|hardest))\b/i
const THINK_HARD = /\bthink (?:hard|harder|carefully|deeply|it through|this through)\b/i

/** The tier the user asked for in so many words ("use Opus", "think hard"), if any. */
export function spokenTier(text: string): Tier | undefined {
  const named = SPOKEN_MODEL.exec(text)?.[1]?.toLowerCase()
  if (named !== undefined) return (Object.keys(TIER_MODELS) as Tier[]).find(tier => TIER_MODELS[tier] === named)
  if (THINK_HARDEST.test(text)) return 'hardest'
  if (THINK_HARD.test(text)) return 'complex'
  return undefined
}

/** The judge's answer as a tier; undefined when it named none. */
export function parseTier(answer: string): Tier | undefined {
  const word = answer.trim().toLowerCase().match(/[a-z]+/)?.[0]
  return word === 'simple' || word === 'complex' || word === 'hardest' ? word : undefined
}

/**
 * The model a step should name for a tier, or undefined to keep the
 * session's: it already runs that family. The session's 1M-context suffix
 * is kept, so a long conversation still fits.
 */
export function stepModel(sessionModel: string, tier: Tier): string | undefined {
  const family = TIER_MODELS[tier]
  if (sessionModel.toLowerCase().includes(family)) return undefined
  return /\[1m\]$/i.test(sessionModel) ? `${family}[1m]` : family
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
  /** The model the last routed step named. */
  model: string | undefined
  isAnnounced: boolean
  /** A routed step got no response: the model may not have been accepted. */
  hasFailedStep: boolean
}

const normalize = (text: string): string => text.replace(/\s+/g, ' ').trim()
/** Enough of a request to judge it. */
const clip = (text: string): string => (text.length > 500 ? `${text.slice(0, 500)}…` : text)

export class ModelRouter {
  /** Utterances judged and not yet started as turns, oldest first. */
  private pending: Judgement[] = []
  private turns = new Map<string, RoutedTurn>()
  private previous: { text: string; tier: Tier } | undefined
  /** Routed models that have answered at least once this session. */
  private answered = new Set<string>()
  /** Set when a routed model was refused: the session's model answers from then on. */
  private isOff = false

  constructor(
    private readonly engine: Engine,
    private readonly options: RouterOptions,
  ) {}

  /** The user's words were heard: start judging them while the turn is set up. */
  async judge(text: string): Promise<void> {
    if (this.isOff || (await this.options.mode().catch(() => 'auto' as const)) === 'off') return
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
    this.turns.set(turnId, { judgement, model: undefined, isAnnounced: false, hasFailedStep: false })
  }

  /**
   * The model a main-loop step of a voice turn names; undefined keeps the
   * engine's. The first step waits a moment for the judgement; a later one
   * takes it as soon as it is in.
   */
  async modelFor(e: TurnStepInput): Promise<string | undefined> {
    const turn = e.agentId === undefined && !this.isOff ? this.turns.get(e.turnId) : undefined
    if (turn === undefined) return undefined
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
    const model = stepModel(e.model, tier)
    turn.model = model
    if (tier !== 'simple' && !turn.isAnnounced) {
      turn.isAnnounced = true
      this.engine.log(`Jarvis: ${TIER_NAMES[tier]} is taking this one.`)
    }
    if (model !== undefined) this.engine.debug(`jarvis: turn ${e.turnId} step ${e.index} → ${model} (${tier})`)
    return model
  }

  /** A routed step ended: no response at all, from a model that never answered, may mean it was not accepted. */
  onStepResult(e: TurnStepInput, result: TurnStepResult | undefined): void {
    const turn = this.turns.get(e.turnId)
    const model = turn?.model
    if (turn === undefined || model === undefined || e.agentId !== undefined) return
    if (result !== undefined && result.stopReason !== null) this.answered.add(model)
    else if (!this.answered.has(model)) turn.hasFailedStep = true
  }

  onTurnComplete(e: TurnCompleteInput): void {
    if (e.agentId !== undefined) return
    const turn = this.turns.get(e.turnId)
    if (turn === undefined) return
    this.turns.delete(e.turnId)
    const tier = turn.judgement.tier ?? 'simple'
    this.previous = { text: turn.judgement.text, tier }
    if (e.reason === 'error' && turn.hasFailedStep && turn.model !== undefined && !this.isOff) {
      this.isOff = true
      this.engine.log(
        `Jarvis: Claude Code could not answer on "${turn.model}", so model routing is off for this session and your session's model answers voice requests. /jarvis routing off turns it off for good.`,
      )
    }
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
