// Incremental sentence splitter for speech: the model's markdown streams in
// as small deltas and complete, speakable sentences come out as early as
// possible. Pure: no `$`, unit tested in sentences.test.ts.
//
// Model: the text is processed line by line. A line is first classified by
// its start (fence, table row, rule, heading, list item, plain text); text
// lines are cleaned of inline markdown and scanned for sentence ends. A line
// end always ends a sentence (headings and list items are their own
// utterances). A partial line is re-cleaned on every push, but only up to a
// "stable" cut, so what was already emitted never changes.

export type SplitterOptions = {
  /** Longest chunk sent to TTS; longer sentences split at a comma or space. */
  maxLength?: number
  /** Cap for the very first chunk, so the first audio starts sooner. */
  firstMaxLength?: number
  /** Spoken once per reply in place of fenced code blocks. */
  codePlaceholder?: string
  /** Spoken once per reply in place of markdown tables. */
  tablePlaceholder?: string
}

export const DEFAULT_MAX_LENGTH = 220
export const DEFAULT_FIRST_MAX_LENGTH = 120
export const CODE_PLACEHOLDER = 'The code is on screen, sir.'
export const TABLE_PLACEHOLDER = 'The table is on screen.'

const TERMINATORS = new Set(['.', '!', '?', '…'])
const CLOSERS = new Set(['"', "'", '”', '’', ')', ']', '»'])

// Never end a sentence after these (they precede a name or an example).
const STRICT_ABBREVIATIONS = new Set([
  'mr', 'mrs', 'ms', 'dr', 'prof', 'st', 'mt', 'vs', 'cf', 'e.g', 'i.e', 'eg', 'ie',
  'approx', 'fig', 'figs', 'vol', 'vols', 'dept', 'gen', 'gov', 'sen', 'rep', 'capt', 'lt', 'col',
])
// End a sentence after these only when the next word is capitalised.
const SOFT_ABBREVIATIONS = new Set([
  'etc', 'inc', 'ltd', 'co', 'corp', 'jr', 'sr', 'est', 'no', 'nos', 'al', 'resp', 'min', 'max',
  'jan', 'feb', 'mar', 'apr', 'jun', 'jul', 'aug', 'sep', 'sept', 'oct', 'nov', 'dec',
  'mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun', 'a.m', 'p.m', 'u.s', 'u.k', 'ph.d',
])

const isSpace = (c: string | undefined): boolean => c !== undefined && /\s/.test(c)
const isWordChar = (c: string | undefined): boolean => c !== undefined && /[\p{L}\p{N}]/u.test(c)
const isUpper = (c: string | undefined): boolean => c !== undefined && /\p{Lu}/u.test(c)
const isLower = (c: string | undefined): boolean => c !== undefined && /\p{Ll}/u.test(c)

type Fence = { char: string; length: number }

type LineClass =
  | { kind: 'pending' }
  | { kind: 'blank' }
  | { kind: 'rule' }
  | { kind: 'table' }
  | { kind: 'fence'; fence: Fence }
  | { kind: 'text'; start: number }

/**
 * Classifies a line by its start. `complete` says whether the line has ended;
 * a partial line whose start is still ambiguous ("`", "1", "-") is pending.
 */
function classify(line: string, complete: boolean): LineClass {
  const indent = /^\s*/.exec(line)?.[0].length ?? 0
  let rest = line.slice(indent)
  if (rest === '') return complete ? { kind: 'blank' } : { kind: 'pending' }

  const fence = /^(`{3,}|~{3,})/.exec(rest)
  if (fence?.[1] !== undefined) return { kind: 'fence', fence: { char: fence[1][0] ?? '`', length: fence[1].length } }
  if (!complete && /^(`{1,2}|~{1,2})$/.test(rest)) return { kind: 'pending' }
  if (rest.startsWith('|')) return { kind: 'table' }

  // A thematic break (---, ***, ___) or, while partial, maybe one or a bullet.
  if (/^([-*_])(\s*\1){2,}\s*$/.test(rest)) return complete ? { kind: 'rule' } : { kind: 'pending' }
  if (!complete && /^([-*_])(\s*\1)*\s*$/.test(rest)) return { kind: 'pending' }

  let start = indent
  const quote = /^(>\s?)+/.exec(rest)
  if (quote) {
    start += quote[0].length
    rest = rest.slice(quote[0].length)
  }

  const heading = /^#{1,6}(\s+|$)/.exec(rest)
  if (heading) {
    if (heading[1] === '' && !complete) return { kind: 'pending' }
    return { kind: 'text', start: start + heading[0].length }
  }
  if (!complete && /^#{1,6}$/.test(rest)) return { kind: 'pending' }

  const bullet = /^(?:[-*+]|\d{1,3}[.)])\s+(?:\[[ xX]\]\s+)?/.exec(rest)
  if (bullet) return { kind: 'text', start: start + bullet[0].length }
  if (!complete && /^(?:[-*+]|\d{1,3}[.)]?)$/.test(rest)) return { kind: 'pending' }

  return { kind: 'text', start }
}

function isClosingFence(line: string, fence: Fence): boolean {
  const trimmed = line.trim()
  return trimmed.length >= fence.length && [...trimmed].every(c => c === fence.char)
}

/**
 * How much of a partial line's content can be cleaned now without the result
 * changing once more text arrives: holds back a trailing markup character
 * (whose meaning depends on the next one) and an unfinished `[label](url)`.
 */
function stableCut(content: string): number {
  let cut = content.length
  while (cut > 0 && '*_~`!\\<'.includes(content[cut - 1] ?? '')) cut -= 1
  const open = content.lastIndexOf('[', cut - 1)
  if (open !== -1) {
    const tail = content.slice(open, cut)
    const isLink = /^\[[^\]]*\]\([^)]*\)/.test(tail)
    const isNotLink = /^\[[^\]]*\][^(]/.test(tail) || (tail.length > 300 && !tail.includes(']'))
    if (!isLink && !isNotLink) cut = content[open - 1] === '!' ? open - 1 : open
  }
  return cut
}

/** Strips inline markdown from (a stable prefix of) one line's content. */
function clean(text: string): string {
  const stripped = text
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/<br\s*\/?>/gi, ' ')
    .replace(/<(https?:\/\/[^\s>]+)>/g, '$1')
    .replace(/`+/g, '')
    .replace(/\*\*|__|~~/g, '')
    .replace(/~(?=\d)/g, 'about ')
  // A lone `*` stays only as an operator (spaces on both sides); a `_` goes
  // when it opens or closes a word (`_word_`) and stays inside one (snake_case).
  let out = ''
  for (let i = 0; i < stripped.length; i += 1) {
    const c = stripped[i] ?? ''
    const before = stripped[i - 1]
    const after = stripped[i + 1]
    if (c === '*' && !(isSpace(before) && isSpace(after))) continue
    if (c === '_' && isWordChar(before) !== isWordChar(after)) continue
    out += c === '|' ? ',' : c
  }
  return out.replace(/\s+/g, ' ')
}

/** The non-space run before index `end`, without leading punctuation. */
function wordBefore(text: string, end: number): string {
  let start = end
  while (start > 0 && !isSpace(text[start - 1])) start -= 1
  return text.slice(start, end).replace(/^[("'“‘[]+/, '')
}

type Verdict = 'yes' | 'no' | 'wait'

export class SentenceSplitter {
  private readonly maxLength: number
  private readonly firstMaxLength: number
  private readonly codePlaceholder: string
  private readonly tablePlaceholder: string

  /** Raw text of the current, unfinished line. */
  private line = ''
  /** Cleaned characters of the current line already emitted. */
  private committed = 0
  private fence: Fence | undefined
  private hasNotedCode = false
  private hasNotedTable = false
  private hasEmitted = false

  constructor(options: SplitterOptions = {}) {
    this.maxLength = Math.max(40, options.maxLength ?? DEFAULT_MAX_LENGTH)
    this.firstMaxLength = Math.max(40, Math.min(this.maxLength, options.firstMaxLength ?? DEFAULT_FIRST_MAX_LENGTH))
    this.codePlaceholder = options.codePlaceholder ?? CODE_PLACEHOLDER
    this.tablePlaceholder = options.tablePlaceholder ?? TABLE_PLACEHOLDER
  }

  /** Feeds the next delta; returns the sentences it completed, in order. */
  push(delta: string): string[] {
    const out: string[] = []
    this.line += delta.replace(/\r/g, '')
    let newline = this.line.indexOf('\n')
    while (newline !== -1) {
      const whole = this.line.slice(0, newline)
      this.line = this.line.slice(newline + 1)
      this.processLine(whole, true, out)
      this.committed = 0
      newline = this.line.indexOf('\n')
    }
    if (this.line !== '') this.processLine(this.line, false, out)
    return out
  }

  /** Ends the text: returns whatever is left as sentences (possibly none). */
  flush(): string[] {
    const out: string[] = []
    if (this.line !== '') this.processLine(this.line, true, out)
    this.line = ''
    this.committed = 0
    this.fence = undefined
    return out
  }

  private processLine(line: string, complete: boolean, out: string[]): void {
    if (this.fence !== undefined) {
      if (complete && isClosingFence(line, this.fence)) this.fence = undefined
      return
    }
    const kind = classify(line, complete)
    switch (kind.kind) {
      case 'pending':
      case 'blank':
      case 'rule':
        return
      case 'table':
        if (!this.hasNotedTable) {
          this.hasNotedTable = true
          this.emit(this.tablePlaceholder, out)
        }
        return
      case 'fence':
        if (!complete) return
        this.fence = kind.fence
        if (!this.hasNotedCode) {
          this.hasNotedCode = true
          this.emit(this.codePlaceholder, out)
        }
        return
      case 'text': {
        const content = line.slice(kind.start)
        const cut = complete ? content.length : stableCut(content)
        this.scan(clean(content.slice(0, cut)), complete, out)
      }
    }
  }

  /** Emits every sentence of `text` past `committed` that is known to be done. */
  private scan(text: string, complete: boolean, out: string[]): void {
    let segment = this.committed
    let i = segment
    while (i < text.length) {
      if (!TERMINATORS.has(text[i] ?? '')) {
        i += 1
        continue
      }
      let runEnd = i
      while (runEnd < text.length && TERMINATORS.has(text[runEnd] ?? '')) runEnd += 1
      let end = runEnd
      while (end < text.length && CLOSERS.has(text[end] ?? '')) end += 1
      if (end >= text.length) break // the line end (or more text) decides
      if (!isSpace(text[end])) {
        i = end // "3.14", "example.com", "e.g.x": not an end
        continue
      }
      const verdict = this.judge(text, i, runEnd, end, complete)
      if (verdict === 'wait') break
      if (verdict === 'yes') {
        segment = this.emitCapped(text, segment, end, out)
        this.emit(text.slice(segment, end), out)
        segment = end
      }
      i = end
    }
    // An open segment past the cap is cut at a comma or space now.
    while (text.length - segment > this.cap()) {
      const cutAt = this.chunkEnd(text, segment)
      this.emit(text.slice(segment, cutAt), out)
      segment = cutAt
    }
    if (complete) {
      this.emit(text.slice(segment), out)
      segment = text.length
    }
    this.committed = segment
  }

  /** Decides whether the terminator run text[start, runEnd) ends a sentence. */
  private judge(text: string, start: number, runEnd: number, end: number, complete: boolean): Verdict {
    let at = end
    while (at < text.length && isSpace(text[at])) at += 1
    const next = text[at]
    if (next === undefined && !complete) return 'wait'
    const run = text.slice(start, runEnd)
    if (run.includes('!') || run.includes('?')) return 'yes'
    if (run !== '.') return next === undefined || isUpper(next) || /["“‘(]/.test(next) ? 'yes' : 'no' // ellipsis
    const word = wordBefore(text, start)
    const lower = word.toLowerCase()
    if (STRICT_ABBREVIATIONS.has(lower)) return 'no'
    if (lower === 'no' && next !== undefined && /\d/.test(next)) return 'no'
    if (/^\p{Lu}$/u.test(word)) return 'no' // an initial: "J. R. R. Tolkien"
    if (SOFT_ABBREVIATIONS.has(lower) || /^(\p{L}\.)+\p{L}$/u.test(word)) {
      return next === undefined || isUpper(next) ? 'yes' : 'no'
    }
    return isLower(next) ? 'no' : 'yes'
  }

  private cap(): number {
    return this.hasEmitted ? this.maxLength : this.firstMaxLength
  }

  /** Emits leading chunks of text[segment, end) while it is over the cap. */
  private emitCapped(text: string, segment: number, end: number, out: string[]): number {
    let at = segment
    while (end - at > this.cap()) {
      const cutAt = this.chunkEnd(text, at)
      this.emit(text.slice(at, cutAt), out)
      at = cutAt
    }
    return at
  }

  /** Where to cut an over-long chunk starting at `start`: after a comma-like mark, else at a space. */
  private chunkEnd(text: string, start: number): number {
    const cap = this.cap()
    const window = text.slice(start, start + cap)
    const floor = Math.floor(cap * 0.4)
    let best = -1
    for (const mark of [', ', '; ', ': ', ' – ', ' — ', ' - ']) {
      const at = window.lastIndexOf(mark)
      if (at >= floor) best = Math.max(best, at + mark.trimEnd().length)
    }
    if (best > 0) return start + best
    const space = window.lastIndexOf(' ')
    return start + (space >= floor ? space : cap)
  }

  private emit(raw: string, out: string[]): void {
    const sentence = raw.replace(/\s+/g, ' ').trim()
    if (!/[\p{L}\p{N}]/u.test(sentence)) return
    out.push(sentence)
    this.hasEmitted = true
  }
}

/** Splits a whole text at once (tests, one-shot speech). */
export function splitSentences(text: string, options?: SplitterOptions): string[] {
  const splitter = new SentenceSplitter(options)
  return [...splitter.push(text), ...splitter.flush()]
}
