// The guard's classifier: how much say the person needs before a shell
// command or file edit runs. Pure: no `$` and no app import, unit tested in
// guard.test.ts.
//
// judge() returns one of four tiers, strictest first: never (denied, no
// question), screen (a click or a typed yes), voice (a spoken yes in a voice
// turn, else a click) and pass (no question). It only ever adds a question:
// a command it does not know passes, and the engine's own rules, mode and
// dialogs still decide everything that reaches them. Anything it cannot read,
// that is hidden, or that is built at run time goes to screen. It is a
// best-effort reading of command text (and of the files a command runs), not
// a sandbox: Claude Code's own permission rules stay the real boundary.
//
// A command's tier is the strictest over all of its parts: every segment of a
// chain or pipeline, every nested `$( )`, `( )`, `{ }` and backtick, and the
// literal text handed to another shell (`cmd /c`, `bash -c`, `powershell
// -Command`, `iex`, `Start-Process -ArgumentList`, ...). The rules follow
// docs/PLAN.md, "PC control and permissions"; rulesSnippet() prints the never
// rows as deny rules the person can paste (nothing here writes them).

export type Tier = 'pass' | 'voice' | 'screen' | 'never'
/**
 * Something the guard must read off the file system before it can judge a call, which judge() is too
 * pure to do: pc.ts fetches it and judges again with the answer in `Resolved`. A local file a command
 * runs (`script`) and every Write/Edit/NotebookEdit path (`realpath`) wait on one. An `optional` read
 * is of a file that may not be there (a name cmd or PowerShell looks up, a file Claude wrote that was
 * deleted since): its absence is an answer, not a failure.
 */
export type Need = { kind: 'script'; path: string; optional?: true } | { kind: 'realpath'; path: string }
/** A judgement: the tier, the rule that set it, a short plain reason ("deletes files"), and what it still needs read. */
export type Verdict = { tier: Tier; rule: string; reason: string; needs?: readonly Need[] }
/**
 * What pc.ts knows that judge() cannot find out by itself: a file tool's real target (null when it
 * cannot be placed), files' contents (null when they cannot be read, false when an optional one is not
 * there), the files Claude wrote or edited this session (Write, Edit, NotebookEdit, as the tools named
 * them), and the user's home folder (for `~`, `$HOME`, `$env:USERPROFILE` and `%USERPROFILE%`).
 */
export type Resolved = {
  realPath?: string | null
  scripts?: Readonly<Record<string, string | null | false>>
  written?: readonly string[]
  home?: string
}

/** The command languages the guard reads: the PowerShell tool, the Bash tool (and Monitor), and `cmd /c` text. */
export type Dialect = 'pwsh' | 'bash' | 'cmd'

const RANK: Record<Tier, number> = { pass: 0, voice: 1, screen: 2, never: 3 }

const PASS: Verdict = { tier: 'pass', rule: 'pass', reason: 'nothing to ask about' }

const verdict = (tier: Tier, rule: string, reason: string): Verdict => ({ tier, rule, reason })

/** `v` with a read need added, so it bubbles up to pc.ts for the second pass. */
const withNeed = (v: Verdict, need: Need): Verdict => ({ ...v, needs: [...(v.needs ?? []), need] })

/** The stricter of two verdicts; on a tie the one found first, so its reason stays. Read needs from both sides carry up. */
function stricter(a: Verdict, b: Verdict): Verdict {
  const winner = RANK[b.tier] > RANK[a.tier] ? b : a
  const needs = [...(a.needs ?? []), ...(b.needs ?? [])]
  return needs.length === 0 ? winner : { ...winner, needs }
}

/** Longer commands are not lexed at all: screen. */
const MAX_CHARS = 8000
/** How deep nested scripts and shells-in-shells may go before the guard stops reading: screen. */
const MAX_DEPTH = 6

const UNBALANCED = 'has unbalanced quotes or brackets'
const TOO_DEEP = 'is nested too deeply to check'
const TOO_MANY_WORDS = 'expands to too many words to check'
/** How many words one bash word's braces may expand to before the guard stops reading: screen. */
const BRACE_WORDS_MAX = 256
const BUILT = verdict('screen', 'built', 'runs a command built at run time')
const HIDDEN = verdict('screen', 'hidden', 'runs a hidden command')

// ---- Lexing ----

/** One word of a command, with quotes and escapes resolved. */
export type Word = {
  /** The text; variables and subexpressions stay as written (`$env:USERPROFILE\x`). */
  text: string
  /** pwsh: the text split at unquoted commas, the items of an array argument. */
  items: string[]
  /** Some of it was quoted: data, not a command name. */
  quoted: boolean
  /** It expands a variable or runs a subexpression, so its value is only known at run time. */
  dynamic: boolean
  /** pwsh: it is a `{ }` script block (its commands are in the command's `inner`). */
  block: boolean
  /** bash: where `{`, `,`, `.` and `}` stand outside quotes in `text`, for brace expansion. */
  braces?: number[]
}

/** One simple command: the words between two separators. */
export type Command = {
  words: Word[]
  /** Its source outside quotes (quoted text blanked, nested parts as `()`), for method-call patterns. */
  code: string
  /** It writes a file through a redirection (`>`, `>>`) other than to null. */
  writes: boolean
  /** The files it writes through a redirection (not null). */
  targets: Word[]
  /** It reads a pipe: the command before it ended in `|`. */
  piped: boolean
  /** Here-documents and here-strings it reads (bash `<<EOF`, `<<<`). */
  stdin: string[]
  /** The files it reads on stdin (`< x.sh`); one named at run time sets `piped` instead. */
  reads: Word[]
  /** Scripts nested in its words: `$( )`, `( )`, `{ }`, backticks. */
  inner: Script[]
}

/** A lexed script: its commands in order and, when it could not be read in full, why. */
export type Script = { dialect: Dialect; commands: Command[]; problem?: string }

export const lexPwsh = (text: string): Script => new Lexer(text, 'pwsh', 0).lex()
export const lexBash = (text: string): Script => new Lexer(text, 'bash', 0).lex()
export const lexCmd = (text: string): Script => new Lexer(text, 'cmd', 0).lex()

const CURLY_SINGLE = '\u2018\u2019\u201a\u201b'
const CURLY_DOUBLE = '\u201c\u201d\u201e'
const NULL_TARGETS = new Set(['/dev/null', 'nul', 'nul:', '$null'])

type Redirect = 'write' | 'read' | 'string' | { strip: boolean }
type Heredoc = { delim: string; strip: boolean; expand: boolean; into: Command; depth: number }

const newWord = (): Word => ({ text: '', items: [''], quoted: false, dynamic: false, block: false })
const newCommand = (piped: boolean): Command => ({ words: [], code: '', writes: false, targets: [], piped, stdin: [], reads: [], inner: [] })
const isEmpty = (c: Command): boolean => c.words.length === 0 && c.inner.length === 0 && c.stdin.length === 0 && c.reads.length === 0 && !c.writes

function addText(word: Word, text: string): void {
  word.text += text
  word.items[word.items.length - 1] += text
}

/**
 * A small lexer for the three dialects: quotes, escapes, comments, line
 * continuations, separators, redirections, here-documents and nesting. It
 * recurses into `$( )`, `( )`, `{ }` (pwsh) and backticks (bash), so each
 * nested script ends at its own closing bracket.
 */
class Lexer {
  private pos = 0
  private problem: string | undefined
  private readonly heredocs: Heredoc[] = []

  constructor(
    private readonly src: string,
    private readonly dialect: Dialect,
    private readonly depth: number,
  ) {}

  lex(): Script {
    const script = this.script(undefined, this.depth)
    return this.problem === undefined ? script : { ...script, problem: this.problem }
  }

  private fail(problem: string): void {
    this.problem ??= problem
  }

  /** Lexes commands up to `close` (the bracket that ends a nested script) or the end of the text. */
  private script(close: string | undefined, depth: number): Script {
    const { src, dialect } = this
    const commands: Command[] = []
    let cur = newCommand(false)
    let word: Word | undefined
    let redirect: Redirect | undefined
    const open = (): Word => (word ??= newWord())
    const endWord = (): void => {
      if (word === undefined) return
      const done = word
      word = undefined
      const kind = redirect
      redirect = undefined
      if (kind === undefined) cur.words.push(...this.expandBraces(done))
      else if (kind === 'write') {
        const isNull = NULL_TARGETS.has(done.text.toLowerCase())
        cur.writes ||= !isNull
        if (!isNull) cur.targets.push(done)
      } else if (kind === 'read') {
        cur.piped ||= done.dynamic // `< <(curl ...)`, `< "$f"`: it reads what is known only at run time
        if (!done.dynamic) cur.reads.push(done)
      } else if (kind === 'string') cur.stdin.push(done.text)
      else this.heredocs.push({ delim: done.text, strip: kind.strip, expand: !done.quoted, into: cur, depth })
    }
    const endCommand = (piped: boolean): void => {
      endWord()
      redirect = undefined
      if (isEmpty(cur)) {
        cur.piped ||= piped
        return
      }
      commands.push(cur)
      cur = newCommand(piped)
    }
    const escape = dialect === 'bash' ? '\\' : dialect === 'pwsh' ? '`' : '^'

    while (this.pos < src.length) {
      const c = src[this.pos] ?? ''
      const n = src[this.pos + 1] ?? ''

      if (c === close) {
        this.pos++
        endCommand(false)
        return { dialect, commands }
      }
      if (c === '\n') {
        this.pos++
        endCommand(false)
        this.readHeredocs()
        continue
      }
      if (/\s/.test(c)) {
        endWord()
        cur.code += ' '
        this.pos++
        continue
      }
      if (c === '#' && word === undefined && dialect !== 'cmd') {
        while (this.pos < src.length && src[this.pos] !== '\n') this.pos++
        continue
      }
      if (dialect === 'pwsh' && c === '<' && n === '#') {
        const end = src.indexOf('#>', this.pos + 2)
        if (end < 0) {
          this.fail(UNBALANCED)
          this.pos = src.length
        } else this.pos = end + 2
        continue
      }
      if (c === escape) {
        this.pos += n === '' ? 1 : 2
        if (n !== '\n' && n !== '') {
          addText(open(), n)
          cur.code += n
        }
        continue
      }

      // Separators: ; && || | & and, in bash, |& (a pipe of both streams).
      if (c === ';') {
        this.pos++
        endCommand(false)
        continue
      }
      if (c === '|') {
        this.pos += n === '|' || (dialect === 'bash' && n === '&') ? 2 : 1
        endCommand(n !== '|')
        continue
      }
      if (c === '&') {
        if (n === '&') {
          this.pos += 2
          endCommand(false)
        } else if (dialect === 'bash' && n === '>') {
          endWord()
          this.pos += src[this.pos + 2] === '>' ? 3 : 2
          redirect = 'write'
        } else if (dialect === 'pwsh' && word === undefined) {
          // The call operator: `& 'C:\x\tool.exe' args`, `& $cmd`, `& { ... }`.
          this.pos++
          cur.words.push({ ...newWord(), text: '&', items: ['&'] })
          cur.code += '&'
        } else {
          this.pos++
          endCommand(false)
        }
        continue
      }

      // Redirections. A word of digits just before is a stream number (2>, 1>>), as is pwsh's `*>`.
      if (c === '>' || (c === '<' && dialect !== 'pwsh')) {
        if (dialect === 'bash' && n === '(') {
          // Process substitution, <( ) or >( ).
          open().dynamic = true
          addText(open(), '(…)')
          this.pos += 2
          this.nest(cur, ')', depth)
          continue
        }
        if (word !== undefined && !word.quoted && (/^\d+$/.test(word.text) || (dialect === 'pwsh' && word.text === '*'))) word = undefined
        else endWord()
        if (c === '>') {
          const length = n === '>' || n === '|' ? 2 : 1
          if (src[this.pos + length] === '&') {
            this.pos += length + 1
            while (/[\d-]/.test(src[this.pos] ?? '')) this.pos++
          } else {
            this.pos += length
            redirect = 'write'
          }
          continue
        }
        if (dialect === 'bash' && n === '<') {
          if (src[this.pos + 2] === '<') {
            this.pos += 3
            redirect = 'string'
          } else {
            const strip = src[this.pos + 2] === '-'
            this.pos += strip ? 3 : 2
            redirect = { strip }
          }
          continue
        }
        if (n === '&') {
          this.pos += 2
          while (/[\d-]/.test(src[this.pos] ?? '')) this.pos++
          continue
        }
        this.pos += n === '>' ? 2 : 1
        redirect = 'read'
        continue
      }

      // Quotes.
      if ((c === "'" && dialect !== 'cmd') || (dialect === 'pwsh' && CURLY_SINGLE.includes(c))) {
        this.single(open(), cur)
        continue
      }
      if (c === '"' || (dialect === 'pwsh' && CURLY_DOUBLE.includes(c))) {
        this.double(open(), cur, depth)
        continue
      }

      // Variables and subexpressions.
      if (c === '$' && dialect !== 'cmd') {
        const w = open()
        if (dialect === 'bash' && n === "'") {
          this.ansi(w, cur)
          continue
        }
        if (dialect === 'bash' && n === '"') {
          this.pos++
          this.double(w, cur, depth)
          continue
        }
        const start = this.pos
        if (!this.variable(w, cur, depth)) {
          addText(w, c)
          this.pos++
        }
        cur.code += src.slice(start, this.pos).startsWith('$(') ? '$()' : src.slice(start, this.pos)
        continue
      }
      if (dialect === 'cmd' && (c === '%' || c === '!')) {
        const match = (c === '%' ? /^%(%?~?[A-Za-z]|[^%\s]+%)/ : /^![^!\s]+!/).exec(src.slice(this.pos))
        const w = open()
        if (match) w.dynamic = true
        addText(w, match ? match[0] : c)
        cur.code += match ? match[0] : c
        this.pos += match ? match[0].length : 1
        continue
      }

      // Nesting.
      if (dialect === 'pwsh') {
        if (c === '@' && (n === "'" || n === '"') && src[this.pos + 2] === '\n') {
          this.hereString(open(), cur, depth)
          continue
        }
        if ((c === '@' && (n === '(' || n === '{')) || c === '(') {
          const w = open()
          w.dynamic = true
          addText(w, c === '@' ? '@(…)' : '(…)')
          cur.code += '()'
          const closer = c === '@' && n === '{' ? '}' : ')'
          this.pos += c === '@' ? 2 : 1
          this.nest(cur, closer, depth)
          continue
        }
        if (c === '{') {
          endWord()
          const start = this.pos
          this.pos++
          this.nest(cur, '}', depth)
          const text = src.slice(start, this.pos)
          cur.words.push({ text, items: [text], quoted: false, dynamic: false, block: true })
          cur.code += '{}'
          continue
        }
        if (c === ')' || c === '}') {
          this.fail(UNBALANCED)
          this.pos++
          continue
        }
        if (c === '=' && word !== undefined && word.text.startsWith('$')) {
          // `$x=Get-Process`: the assignment operator ends the variable.
          endWord()
          cur.words.push({ ...newWord(), text: '=', items: ['='] })
          cur.code += '='
          this.pos++
          continue
        }
        if (c === ',') {
          // An array argument: `'-a', '-b'` stays one word whose items are the elements.
          const w = open()
          w.text += ','
          w.items.push('')
          cur.code += ','
          this.pos++
          while (src[this.pos] === ' ' || src[this.pos] === '\t') this.pos++
          continue
        }
      }
      if (dialect === 'bash') {
        if (c === '`') {
          const w = open()
          w.dynamic = true
          addText(w, '`…`')
          cur.code += '``'
          this.pos++
          this.nest(cur, '`', depth)
          continue
        }
        if (c === '(' && n === ')') {
          // `name() { ...; }` defines a function: the body's commands follow as their own.
          this.pos += 2
          endCommand(false)
          continue
        }
        if (c === '(') {
          endWord()
          cur.code += '()'
          this.pos++
          this.nest(cur, ')', depth)
          continue
        }
        if (c === ')') {
          this.fail(UNBALANCED)
          this.pos++
          continue
        }
      }
      if (dialect === 'cmd' && c === '(' && word === undefined) {
        cur.code += '()'
        this.pos++
        this.nest(cur, ')', depth)
        continue
      }

      const w = open()
      if (dialect === 'bash' && '{,.}'.includes(c)) (w.braces ??= []).push(w.text.length)
      addText(w, c)
      cur.code += c
      this.pos++
    }
    if (close !== undefined) this.fail(UNBALANCED)
    endCommand(false)
    return { dialect, commands }
  }

  /** Lexes a nested script up to `closer` into the command's `inner`. Past MAX_DEPTH it stops reading. */
  private nest(cur: Command, closer: string, depth: number): void {
    if (depth + 1 > MAX_DEPTH) {
      this.fail(TOO_DEEP)
      this.pos = this.src.length
      return
    }
    cur.inner.push(this.script(closer, depth + 1))
  }

  /** bash: the words a word's braces expand to (`{rm,-rf,~}` is three), as bash runs them. Past BRACE_WORDS_MAX it stops reading. */
  private expandBraces(word: Word): Word[] {
    if (word.braces === undefined) return [word]
    const texts: string[] = []
    if (!expandInto(word.text, new Set(word.braces), texts)) {
      this.fail(TOO_MANY_WORDS)
      return [word]
    }
    return texts.map(text => ({ ...word, text, items: [text], braces: undefined }))
  }

  /** Single quotes: literal, with `''` for a quote in pwsh. */
  private single(w: Word, cur: Command): void {
    const { src, dialect } = this
    const isQuote = (c: string): boolean => c === "'" || (dialect === 'pwsh' && c !== '' && CURLY_SINGLE.includes(c))
    w.quoted = true
    cur.code += "''"
    this.pos++
    while (this.pos < src.length) {
      const c = src[this.pos] ?? ''
      if (isQuote(c)) {
        if (dialect === 'pwsh' && isQuote(src[this.pos + 1] ?? '')) {
          addText(w, "'")
          this.pos += 2
          continue
        }
        this.pos++
        return
      }
      addText(w, c)
      this.pos++
    }
    this.fail(UNBALANCED)
  }

  /** Double quotes: escapes, variables and `$( )` (and backticks in bash) still work inside. */
  private double(w: Word, cur: Command, depth: number): void {
    w.quoted = true
    cur.code += '""'
    this.pos++
    if (!this.expandable(w, cur, depth, '"')) this.fail(UNBALANCED)
  }

  /**
   * Reads expandable text up to `end`: a double quote, a pwsh here-string's
   * closing line, or (undefined) the end of the text, for an unquoted
   * here-document's body. Returns whether the end was found.
   */
  private expandable(w: Word, cur: Command, depth: number, end: '"' | '\n"@' | undefined): boolean {
    const { src, dialect } = this
    const isQuote = (c: string): boolean => c === '"' || (dialect === 'pwsh' && c !== '' && CURLY_DOUBLE.includes(c))
    while (this.pos < src.length) {
      const c = src[this.pos] ?? ''
      const n = src[this.pos + 1] ?? ''
      if (end === '"' && isQuote(c)) {
        if (dialect === 'pwsh' && isQuote(n)) {
          addText(w, '"')
          this.pos += 2
          continue
        }
        this.pos++
        return true
      }
      if (end === '\n"@' && src.startsWith(end, this.pos)) {
        this.pos += end.length
        return true
      }
      if (dialect === 'bash' && c === '\\' && n !== '') {
        addText(w, '$`"\\'.includes(n) ? n : n === '\n' ? '' : c + n)
        this.pos += 2
        continue
      }
      if (dialect === 'pwsh' && c === '`' && n !== '') {
        addText(w, n)
        this.pos += 2
        continue
      }
      if (c === '$' && dialect !== 'cmd' && this.variable(w, cur, depth)) continue
      if (dialect === 'bash' && c === '`') {
        w.dynamic = true
        addText(w, '`…`')
        this.pos++
        this.nest(cur, '`', depth)
        continue
      }
      if (dialect === 'cmd' && c === '%' && /^%[^%\s]+%/.test(src.slice(this.pos))) w.dynamic = true
      addText(w, c)
      this.pos++
    }
    return end === undefined
  }

  /** A pwsh here-string, `@'` or `@"` up to a line starting `'@` or `"@`. */
  private hereString(w: Word, cur: Command, depth: number): void {
    const { src } = this
    const quote = src[this.pos + 1]
    w.quoted = true
    cur.code += '""'
    // The cursor stays on the line break, so an empty body still finds its closing line.
    this.pos += 2
    if (quote === '"') {
      if (!this.expandable(w, cur, depth, '\n"@')) this.fail(UNBALANCED)
      return
    }
    const end = src.indexOf("\n'@", this.pos)
    if (end < 0) {
      this.fail(UNBALANCED)
      this.pos = src.length
      return
    }
    addText(w, src.slice(this.pos + 1, Math.max(end, this.pos + 1)))
    this.pos = end + 3
  }

  /** bash `$'...'`: backslash escapes can spell any character, so one there hides the text. */
  private ansi(w: Word, cur: Command): void {
    const { src } = this
    w.quoted = true
    cur.code += "''"
    this.pos += 2
    while (this.pos < src.length) {
      const c = src[this.pos] ?? ''
      if (c === "'") {
        this.pos++
        return
      }
      if (c === '\\') {
        w.dynamic = true
        addText(w, src.slice(this.pos, this.pos + 2))
        this.pos += 2
        continue
      }
      addText(w, c)
      this.pos++
    }
    this.fail(UNBALANCED)
  }

  /** A `$` at the cursor: a variable, `${...}` or `$( )`. Returns false when it is a plain `$`. */
  private variable(w: Word, cur: Command, depth: number): boolean {
    const { src, dialect } = this
    const n = src[this.pos + 1] ?? ''
    if (n === '(') {
      w.dynamic = true
      addText(w, '$(…)')
      this.pos += 2
      this.nest(cur, ')', depth)
      return true
    }
    if (n === '{') {
      const end = src.indexOf('}', this.pos + 2)
      if (end < 0) {
        this.fail(UNBALANCED)
        this.pos = src.length
        return true
      }
      w.dynamic = true
      addText(w, src.slice(this.pos, end + 1))
      this.pos = end + 1
      return true
    }
    const name = (dialect === 'pwsh' ? /^[\w:?^$]+/ : /^([A-Za-z_]\w*|[0-9@*#?$!-])/).exec(src.slice(this.pos + 1))
    if (!name) return false
    if (!(dialect === 'pwsh' && /^(true|false|null)$/i.test(name[0]))) w.dynamic = true
    addText(w, '$' + name[0])
    this.pos += 1 + name[0].length
    return true
  }

  /** After a line break: the bodies of the here-documents opened on that line. */
  private readHeredocs(): void {
    const { src } = this
    for (let doc = this.heredocs.shift(); doc !== undefined; doc = this.heredocs.shift()) {
      const lines: string[] = []
      while (this.pos < src.length) {
        let end = src.indexOf('\n', this.pos)
        if (end < 0) end = src.length
        const line = src.slice(this.pos, end)
        this.pos = Math.min(end + 1, src.length)
        if ((doc.strip ? line.replace(/^\t+/, '') : line) === doc.delim) break
        lines.push(line)
      }
      const body = lines.join('\n')
      doc.into.stdin.push(body)
      if (doc.expand && /[$`]/.test(body)) {
        // An unquoted delimiter: `$( )` and backticks in the body still run.
        const inner = new Lexer(body, 'bash', doc.depth)
        inner.expandable(newWord(), doc.into, doc.depth, undefined)
        if (inner.problem !== undefined) this.fail(inner.problem)
      }
    }
  }
}

/**
 * bash brace expansion of `text` into `out`, as bash does it before anything
 * else: the first `{` (among `marks`, the brace characters outside quotes)
 * with a `}` and a comma at its own level, or a sequence (`{1..3}`, `{a..c}`),
 * becomes one word per item, each expanded again. Returns false past
 * BRACE_WORDS_MAX words.
 */
function expandInto(text: string, marks: ReadonlySet<number>, out: string[]): boolean {
  for (let open = 0; open < text.length; open++) {
    if (text[open] !== '{' || !marks.has(open)) continue
    let depth = 0
    let close = -1
    const commas: number[] = []
    for (let i = open + 1; i < text.length && close < 0; i++) {
      if (!marks.has(i)) continue
      if (text[i] === '{') depth++
      else if (text[i] === '}' && depth > 0) depth--
      else if (text[i] === '}') close = i
      else if (text[i] === ',' && depth === 0) commas.push(i)
    }
    if (close < 0) continue
    const inner = text.slice(open + 1, close)
    let items: { text: string; marks: number[] }[]
    if (commas.length > 0) {
      const cuts = [open, ...commas, close]
      items = cuts.slice(1).map((end, k) => {
        const start = (cuts[k] ?? open) + 1
        return { text: text.slice(start, end), marks: [...marks].filter(i => i >= start && i < end).map(i => i - start) }
      })
    } else {
      const sequence = braceSequence(inner)
      const dots = inner.split('').flatMap((ch, i) => (ch === '.' ? [open + 1 + i] : []))
      if (sequence === undefined || !dots.every(i => marks.has(i))) continue
      if (sequence.length > BRACE_WORDS_MAX) return false
      items = sequence.map(item => ({ text: item, marks: [] }))
    }
    const before = [...marks].filter(i => i < open)
    for (const item of items) {
      const after = [...marks].filter(i => i > close).map(i => i - close - 1 + open + item.text.length)
      const next = new Set([...before, ...item.marks.map(i => i + open), ...after])
      if (!expandInto(text.slice(0, open) + item.text + text.slice(close + 1), next, out)) return false
    }
    return true
  }
  out.push(text)
  return out.length <= BRACE_WORDS_MAX
}

/** A bash sequence expression's items (`1..3`, `a..e`, `10..0..2`), cut after BRACE_WORDS_MAX + 1; undefined for anything else. */
function braceSequence(inner: string): string[] | undefined {
  const match = /^(-?\d+|[A-Za-z])\.\.(-?\d+|[A-Za-z])(?:\.\.(-?\d+))?$/.exec(inner)
  if (!match) return undefined
  const [, from = '', to = '', by] = match
  const isNumber = /\d/.test(from)
  if (isNumber !== /\d/.test(to)) return undefined
  const start = isNumber ? Number(from) : from.charCodeAt(0)
  const end = isNumber ? Number(to) : to.charCodeAt(0)
  const step = Math.max(1, Math.abs(Number(by ?? 1))) * (end < start ? -1 : 1)
  const items: string[] = []
  for (let n = start; step > 0 ? n <= end : n >= end; n += step) {
    items.push(isNumber ? String(n) : String.fromCharCode(n))
    if (items.length > BRACE_WORDS_MAX) break
  }
  return items
}

// ---- Finding the command word ----

/** Where a command's program is: the word's index, or undefined for a keyword, an expression or nothing. */
type Found = {
  at: number | undefined
  /** sudo, gsudo or doas came before it. */
  admin: boolean
  /** A wrapper (sudo, xargs, env, ...) came before it, so a lone quoted word may be a whole command line. */
  wrapped: boolean
  /** It runs a command it builds itself (`env -S`). */
  built: boolean
}

/**
 * The folder a command's later parts run in: where the call started, a folder a `cd` named (as
 * written, joined onto the one before when relative; `isLiteral` when it can be read as it stands,
 * not just matched, as `$HOME\x` can), or one Jarvis cannot place.
 */
type Cwd = { kind: 'here' } | { kind: 'at'; path: string; isLiteral: boolean } | { kind: 'unknown' }
const HERE: Cwd = { kind: 'here' }
const UNPLACEABLE: Cwd = { kind: 'unknown' }

/**
 * What one judge() call learns as it walks: after a loop, every move counts as many; after a `cd`,
 * a relative path lands in that folder (`stack` holds pushd's folders). By file name (fileName):
 * `made` holds what the command's parts so far may write, `ran` the local files they run, `named`
 * the files Claude wrote that the command names, and `namedIn` in how many of its typed parts each
 * name stands. A file run after a part that may write it is run as that part left it, not as Jarvis
 * read it.
 */
type State = { loop: boolean; cwd: Cwd; stack: Cwd[]; made: Set<string>; ran: string[]; named: Set<string>; namedIn: Map<string, number> }
const newState = (): State => ({ loop: false, cwd: HERE, stack: [], made: new Set(), ran: [], named: new Set(), namedIn: new Map() })

const BASH_PREFIXES = new Set(['!', '{', '}', 'if', 'then', 'else', 'elif', 'do', 'while', 'until', 'time', 'coproc'])
const BASH_ENDS = new Set(['for', 'select', 'case', 'esac', 'fi', 'done', 'in', ';;'])
const BASH_LOOPS = new Set(['for', 'select', 'while', 'until'])
const PWSH_PREFIXES = new Set([
  'if', 'elseif', 'else', 'switch', 'try', 'catch', 'finally', 'trap', 'begin', 'process', 'end', 'dynamicparam',
  'foreach', 'for', 'while', 'do', 'until', 'return', 'throw',
])
const PWSH_DEFINITIONS = new Set(['function', 'filter', 'workflow', 'configuration', 'class', 'enum', 'using', 'param', 'data'])
const PWSH_LOOPS = new Set(['foreach', 'for', 'while', 'do', 'until'])
const ASSIGNMENT = /^[A-Za-z_]\w*(\[[^\]]*\])?\+?=/

/**
 * Wrappers that run the command after their own options: the options that
 * take a value, and how many plain words (a duration, a folder) come before
 * the command.
 */
const WRAPPERS: Record<string, { values: readonly string[]; admin?: boolean; bash?: boolean; operands?: number }> = {
  sudo: {
    admin: true,
    values: ['-u', '-g', '-h', '-p', '-r', '-t', '-C', '-D', '-R', '-T', '-U', '--user', '--group', '--host', '--prompt',
      '--role', '--type', '--chdir', '--chroot', '--close-from', '--command-timeout', '--other-user'],
  },
  gsudo: { admin: true, values: ['-u', '--user', '-i', '--integrity', '--loglevel'] },
  doas: { admin: true, values: ['-u', '-C'] },
  xargs: {
    values: ['-I', '-i', '-n', '-P', '-d', '-L', '-l', '-s', '-E', '-e', '-a', '--arg-file', '--delimiter', '--max-args',
      '--max-procs', '--replace', '--max-lines', '--max-chars', '--eof'],
  },
  env: { bash: true, values: ['-u', '--unset', '-C', '--chdir'] },
  nice: { bash: true, values: ['-n', '--adjustment'] },
  nohup: { bash: true, values: [] },
  time: { bash: true, values: [] },
  command: { bash: true, values: [] },
  builtin: { bash: true, values: [] },
  exec: { bash: true, values: ['-a'] },
  stdbuf: { bash: true, values: ['-i', '-o', '-e'] },
  timeout: { bash: true, values: ['-s', '-k', '--signal', '--kill-after'], operands: 1 },
  pkexec: { admin: true, bash: true, values: ['--user'] },
  watch: { bash: true, values: ['-n', '--interval', '-d', '-q', '--equexit'] },
  setsid: { bash: true, values: [] },
  ionice: { bash: true, values: ['-c', '-n', '-p', '-P', '-u', '--class', '--classdata'] },
  chrt: { bash: true, values: ['-T', '-P', '-D'], operands: 1 },
  taskset: { bash: true, values: [], operands: 1 },
  flock: { bash: true, values: ['-w', '-E', '--timeout', '--conflict-exit-code'], operands: 1 },
  chroot: { bash: true, admin: true, values: ['--userspec', '--groups'], operands: 1 },
  strace: { bash: true, values: ['-o', '-e', '-p', '-s', '-u', '-E', '-I', '-a', '-b', '-O', '-P', '-S', '-X'] },
  ltrace: { bash: true, values: ['-o', '-e', '-p', '-s', '-u', '-n', '-a', '-A', '-D', '-l', '-x'] },
  unbuffer: { bash: true, values: [] },
  faketime: { bash: true, values: [], operands: 1 },
  caffeinate: { bash: true, values: ['-t', '-w'] },
  busybox: { bash: true, values: [] },
  toybox: { bash: true, values: [] },
  psexec: { admin: true, values: ['-u', '-p', '-w', '-n', '-a', '-r', '-g'] },
  psexec64: { admin: true, values: ['-u', '-p', '-w', '-n', '-a', '-r', '-g'] },
  paexec: { admin: true, values: ['-u', '-p', '-w', '-n', '-a', '-r', '-g'] },
  conhost: { values: ['--width', '--height'] },
}

/**
 * Finds the program a command runs, past assignments, keywords and
 * wrappers. Loop keywords set `state.loop`.
 */
function findCommand(words: readonly Word[], dialect: Dialect, state: State): Found {
  const found: Found = { at: undefined, admin: false, wrapped: false, built: false }
  let i = 0
  while (i < words.length) {
    const word = words[i]
    if (word === undefined) break
    const text = word.text
    const low = text.toLowerCase()
    const plain = !word.quoted && !word.dynamic && !word.block

    if (dialect === 'pwsh') {
      const next = words[i + 1]
      // `& cmd` calls it and `. cmd` dot-sources it: both run it.
      if (plain && (text === '&' || text === '.')) {
        if (next === undefined || next.block) return found
        return { ...found, at: i + 1 }
      }
      if ((text.startsWith('$') || text.startsWith('[')) && next !== undefined && /^[-+*/%?]?=$/.test(next.text)) {
        i += 2 // `$x = command`: the right side is a command
        continue
      }
      // An expression, not a command; after a wrapper (`gsudo "..."`) a quoted word is a command line.
      if (!found.wrapped && (word.quoted || word.block || /^[$([@\d-]/.test(text))) return found
      if (plain && PWSH_DEFINITIONS.has(low)) return found
      if (plain && PWSH_PREFIXES.has(low)) {
        if (PWSH_LOOPS.has(low)) state.loop = true
        i++
        continue
      }
    } else if (dialect === 'bash' && plain) {
      if (ASSIGNMENT.test(text)) {
        i++
        continue
      }
      if (BASH_LOOPS.has(text)) state.loop = true
      if (BASH_ENDS.has(text)) return found
      if (BASH_PREFIXES.has(text)) {
        i++
        continue
      }
      if (text === 'function') {
        i += 2
        continue
      }
      if (text === 'command' && words.slice(i + 1).some(w => w.text === '-v' || w.text === '-V')) return found
      if (text === 'env' && words.slice(i + 1).some(w => w.text === '-S' || w.text.startsWith('--split-string'))) found.built = true
    } else if (dialect === 'cmd' && plain) {
      const name = low.replace(/^@+/, '')
      if (name === 'if') {
        i = afterCmdCondition(words, i + 1)
        continue
      }
      if (name === 'else' || name === 'call') {
        i++
        continue
      }
      if (name === 'for') {
        const body = words.findIndex((w, j) => j > i && w.text.toLowerCase() === 'do')
        if (body < 0) return found
        state.loop = true
        i = body + 1
        continue
      }
    }

    const name = programNames(text, dialect)[0] ?? ''
    const wrapper = plain ? WRAPPERS[name] : undefined
    if (wrapper !== undefined && (wrapper.bash !== true || dialect === 'bash')) {
      found.wrapped = true
      if (wrapper.admin === true) found.admin = true
      i++
      let operandsLeft = wrapper.operands ?? 0
      while (i < words.length) {
        const option = words[i]?.text ?? ''
        if (option === '--') {
          i++
          break
        }
        if (option.startsWith('-') && option.length > 1) {
          i += wrapper.values.includes(option) ? 2 : 1
          continue
        }
        if (operandsLeft > 0) {
          operandsLeft--
          i++
          continue
        }
        // `env A=1 cmd`, psexec's `\\computer`, and a number left by an option with an optional value (`psexec -i 1`).
        if ((name === 'env' && ASSIGNMENT.test(option)) || option.startsWith('\\\\') || /^\d+$/.test(option)) {
          i++
          continue
        }
        break
      }
      continue
    }
    return { ...found, at: i }
  }
  return found
}

/** cmd's `if [/i] [not] exist x | defined x | errorlevel n | a==b | a op b`: the index of the command after it. */
function afterCmdCondition(words: readonly Word[], start: number): number {
  let i = start
  while (['/i', 'not'].includes(words[i]?.text.toLowerCase() ?? '')) i++
  const first = words[i]?.text.toLowerCase() ?? ''
  if (['exist', 'defined', 'errorlevel', 'cmdextversion'].includes(first)) return i + 2
  if (/^(==|equ|neq|lss|leq|gtr|geq)$/.test(words[i + 1]?.text.toLowerCase() ?? '')) return i + 3
  if (first.endsWith('==')) return i + 2
  return i + 1
}

// ---- Program names ----

const PWSH_ALIASES: Record<string, string> = {
  rm: 'remove-item', ri: 'remove-item', del: 'remove-item', erase: 'remove-item', rd: 'remove-item', rmdir: 'remove-item',
  mv: 'move-item', mi: 'move-item', move: 'move-item',
  ren: 'rename-item', rni: 'rename-item',
  cp: 'copy-item', copy: 'copy-item', cpi: 'copy-item',
  sc: 'set-content', ac: 'add-content', clc: 'clear-content',
  iex: 'invoke-expression', icm: 'invoke-command', sajb: 'start-job', r: 'invoke-history', ihy: 'invoke-history',
  iwr: 'invoke-webrequest', wget: 'invoke-webrequest', curl: 'invoke-webrequest', irm: 'invoke-restmethod',
  saps: 'start-process', start: 'start-process', kill: 'stop-process', spps: 'stop-process',
  gcb: 'get-clipboard', scb: 'set-clipboard', ii: 'invoke-item', ipmo: 'import-module',
  sal: 'set-alias', nal: 'new-alias',
  '%': 'foreach-object', foreach: 'foreach-object', '?': 'where-object', where: 'where-object',
  cat: 'get-content', gc: 'get-content', type: 'get-content',
  ls: 'get-childitem', dir: 'get-childitem', gci: 'get-childitem', gi: 'get-item', gp: 'get-itemproperty',
  sls: 'select-string', sp: 'set-itemproperty', ni: 'new-item', si: 'set-item', rp: 'remove-itemproperty',
  cli: 'clear-item', clp: 'clear-itemproperty', gsv: 'get-service', spsv: 'stop-service', sasv: 'start-service',
}

/** The part after the last `/` or `\`. */
function baseName(text: string): string {
  const trimmed = text.trim()
  return trimmed.slice(Math.max(trimmed.lastIndexOf('/'), trimmed.lastIndexOf('\\')) + 1)
}

/**
 * The names a command word may run, lowercased, without folder or `.exe`.
 * In pwsh a bare alias is judged both as its cmdlet and as the program of
 * that name: `sc` is Set-Content in Windows PowerShell and sc.exe in
 * PowerShell 7, and the stricter tier wins.
 */
function programNames(text: string, dialect: Dialect): string[] {
  const base = baseName(text).toLowerCase().replace(/^@+/, '')
  const stripped = base.replace(/\.(exe|com|cmd|bat|ps1)$/, '')
  const name = /^python[\d.]*w?$/.test(stripped) ? 'python' : /^mkfs(\.\w+)?$/.test(stripped) ? 'mkfs' : stripped
  if (dialect !== 'pwsh' || stripped !== base || /[\\/]/.test(text)) return [name]
  const alias = PWSH_ALIASES[name]
  return alias === undefined ? [name] : [alias, name]
}

// ---- Judging ----

type Ctx = {
  dialect: Dialect
  /** Everything being judged, lowercased: rows look here for what is piped in. */
  text: string
  depth: number
  state: State
  /** What pc.ts knows (files' contents, a real path, the files Claude wrote, the home folder); nothing read yet on the first pass. */
  resolved: Resolved
  /** The folder of the script file being judged, for `$PSScriptRoot` and `%~dp0`. */
  scriptDir?: string
  /** The files being judged further up (normalized): each is judged once, so a script that names itself does not loop. */
  files?: readonly string[]
}

/**
 * An argument as a Windows program gets it. Git Bash (the Bash tool on
 * Windows) turns a lone `/c` into a path, so `cmd //c` and `schtasks
 * //create` are how switches are written there: it passes `//x` on as `/x`.
 */
const switchText = (text: string, dialect: Dialect): string => (dialect === 'bash' && /^\/\/[^/]/.test(text) ? text.slice(1) : text)

/** A program and its arguments, as the rows see it. */
type Call = {
  name: string
  dialect: Dialect
  args: readonly Word[]
  /** The arguments' texts, lowercased (in bash, `//x` as `/x`: switchText). */
  low: readonly string[]
  piped: boolean
  stdin: readonly string[]
  /** The files it reads on stdin (`bash < x.sh`). */
  reads: readonly Word[]
  /** A loop came before it (or it runs inside one). */
  loop: boolean
  text: string
}

const ADMIN = verdict('screen', 'admin', 'runs as administrator')
const DOWNLOAD_RUN = verdict('screen', 'download-run', 'downloads and runs code')
const CLAUDE_OWN = "changes Claude Code's own permissions or plugins"
const CLAUDE_SETTINGS = verdict('never', 'claude-settings', CLAUDE_OWN)
const CLAUDE_CONFIG = verdict('screen', 'claude-config', "changes Claude Code's configuration")
/** A Write/Edit/NotebookEdit whose real path is not known: it cannot be placed, or it still carries an 8.3 short name. */
const UNRESOLVED_PATH = verdict('screen', 'unresolved-path', 'edits a path Jarvis cannot resolve')
const JARVIS_SECRETS = verdict('never', 'jarvis-secrets', "reaches Jarvis's secrets")
const JARVIS_HELPER = verdict('never', 'jarvis-helper', "talks to Jarvis's helper")
const DOWNLOADING = /downloadstring|downloaddata|downloadfile|net\.webclient|invoke-webrequest|invoke-restmethod|\b(iwr|irm|curl|wget)\b|https?:\/\//

function judgeScript(script: Script, ctx: Ctx): Verdict {
  const inner = { ...ctx, dialect: script.dialect }
  let v = script.problem === undefined ? PASS : verdict('screen', 'unreadable', script.problem)
  script.commands.forEach((command, i) => {
    const cwd = ctx.state.cwd
    v = stricter(v, judgeCommand(command, inner))
    // A pipeline's parts run apart: a `cd` there may not last.
    if (ctx.state.cwd !== cwd && (command.piped || script.commands[i + 1]?.piped === true)) ctx.state.cwd = UNPLACEABLE
  })
  return v
}

function judgeCommand(command: Command, ctx: Ctx): Verdict {
  const ranBefore = ctx.state.ran.length
  let v = judgeCode(command, ctx)
  const found = findCommand(command.words, ctx.dialect, ctx.state)
  // Its relative paths land where it starts, before its own `cd` moves on.
  v = stricter(v, judgeMovedPaths(found, command, ctx))
  v = stricter(v, judgeFound(found, command.words, command, ctx))
  if (ctx.dialect === 'pwsh') v = stricter(v, stricter(judgeBarewords(command, found.at, ctx), usingModule(command, ctx)))
  for (const inner of command.inner) {
    const cwd = ctx.state.cwd
    v = stricter(v, judgeScript(inner, { ...ctx, depth: ctx.depth + 1 }))
    // A bash subshell's `cd` ends with it; PowerShell's and cmd's blocks share the folder, maybe or maybe not moved.
    if (ctx.state.cwd !== cwd) ctx.state.cwd = ctx.dialect === 'bash' ? cwd : UNPLACEABLE
  }
  noteMade(found, command, ranBefore, ctx)
  return v
}

function judgeFound(found: Found, words: readonly Word[], command: Command, ctx: Ctx): Verdict {
  let v = found.admin ? ADMIN : PASS
  if (found.built) v = stricter(v, BUILT)
  const word = found.at === undefined ? undefined : words[found.at]
  if (found.at === undefined || word === undefined || word.block) return v
  const args = words.slice(found.at + 1)
  // cmd ignores a leading run of its token delimiters (space, tab, `;`, `,`, `=`) and the echo-off `@`
  // before the program name, quoted or not, so the guard must too: `=vssadmin`, `,rd`, `="vssadmin"`,
  // `=v"ss"admin` and `@ vssadmin` are vssadmin and rd, not names of their own (the DOSfuscation
  // `cmd /c ,;=cmd` trick). It then ends an unquoted name at `/`, `,`, `;` or `=`: `rd/s/q x` is rd
  // with /s /q, `cmd/c` is cmd.
  if (ctx.dialect === 'cmd') {
    const head = word.text.replace(/^[\s;,=@]+/, '')
    const cut = word.quoted ? -1 : head.search(/[/,;=]/)
    if (head !== word.text || cut >= 0) {
      const nameText = cut < 0 ? head : head.slice(0, cut)
      const name = nameText === '' ? [] : [{ ...newWord(), text: nameText, items: [nameText], quoted: word.quoted, dynamic: word.dynamic }]
      const extra = (cut < 0 ? '' : head.slice(cut))
        .split(/(?=\/)|[,;=]+/)
        .filter(part => part !== '')
        .map(part => ({ ...newWord(), text: part, items: [part] }))
      const rest = [...name, ...extra, ...args]
      // A word of nothing but delimiters (`cmd /c = vssadmin`): the next word is the program.
      return rest.length === 0 ? v : stricter(v, judgeFound({ ...found, at: 0 }, rest, command, ctx))
    }
  }
  // A leading `=` or `,` is never part of a real program name, quoted or not. An outer shell can split
  // a cmd delimiter run off into a word of its own (pwsh/bash break `cmd /c ;=vssadmin ...` at the `;`,
  // leaving `=vssadmin` as a word judged in that shell): strip it and re-judge.
  if (!word.dynamic && ctx.dialect !== 'cmd' && /^[=,]+\S/.test(word.text)) {
    const nameText = word.text.replace(/^[=,]+/, '')
    return stricter(v, judgeFound({ ...found, at: 0 }, [{ ...newWord(), text: nameText, items: [nameText], quoted: word.quoted }, ...args], command, ctx))
  }
  // `gsudo "Remove-Item x"`: a wrapper given one quoted command line.
  if (found.wrapped && args.length === 0 && word.quoted && !word.dynamic && /\s/.test(word.text.trim())) {
    return stricter(v, judgeText(word.text, ctx.dialect, ctx))
  }
  // A variable or subexpression names the program, or bash `$'\x72\x6d'` spells it in escapes.
  // (A folder in a variable, "$HOME/bin/tool", is a local file named at run time: launch() below.)
  if (word.dynamic && (/[$%!(`…]/.test(baseName(word.text)) || (ctx.dialect === 'bash' && word.text.includes('\\')))) {
    return stricter(v, BUILT)
  }
  if (ctx.dialect === 'bash' && !word.quoted && /[*?[]/.test(word.text)) return stricter(v, BUILT)
  for (const name of programNames(word.text, ctx.dialect)) {
    const call: Call = {
      name,
      dialect: ctx.dialect,
      args,
      low: args.map(w => switchText(w.text, ctx.dialect).toLowerCase()),
      piped: command.piped,
      stdin: command.stdin,
      reads: command.reads,
      loop: ctx.state.loop,
      text: ctx.text,
    }
    v = stricter(v, judgeRows(call))
    const evaluate = EVALUATORS[name]
    if (evaluate !== undefined) v = stricter(v, evaluate(call, ctx))
  }
  changeDir(word, args, ctx)
  // The program may be a local file (`.\deploy.ps1`, `& "C:\x\a.ps1"`, `./deploy`, cmd's `x` for x.bat):
  // read and judged, since the engine's own rules would run its whole content on one yes.
  return stricter(v, launch(word, ctx.dialect, ctx))
}

/** Judges a list of words as a command of its own (`find -exec ...`, `wsl -e ...`, cmd's `start ...`), run apart from this one. */
function judgeWords(words: readonly Word[], from: Call, ctx: Ctx, dialect: Dialect = ctx.dialect): Verdict {
  if (ctx.depth + 1 > MAX_DEPTH) return verdict('screen', 'unreadable', TOO_DEEP)
  const inner = { ...ctx, dialect, depth: ctx.depth + 1 }
  const command: Command = { ...newCommand(from.piped), words: [...words], stdin: [...from.stdin], reads: [...from.reads] }
  const cwd = ctx.state.cwd
  const v = judgeFound(findCommand(command.words, dialect, ctx.state), command.words, command, inner)
  if (ctx.state.cwd !== cwd) ctx.state.cwd = UNPLACEABLE
  return v
}

/** Windows line ends, and the dashes PowerShell also takes as a parameter's hyphen: a typed command and a script file alike. */
const normalizeText = (raw: string): string => raw.replace(/\r\n?/g, '\n').replace(/[\u2013\u2014\u2015]/g, '-')

/**
 * Judges literal text handed to another shell (or decoded, or read from a script file), one level
 * deeper, normalized as a typed command is. It runs apart from the command that hands it on: a `cd`
 * inside may or may not last, so after one the folder is not known.
 */
function judgeText(raw: string, dialect: Dialect, ctx: Ctx): Verdict {
  if (ctx.depth + 1 > MAX_DEPTH) return verdict('screen', 'unreadable', TOO_DEEP)
  const text = normalizeText(raw)
  const script = new Lexer(text, dialect, ctx.depth + 1).lex()
  let v = HIDDEN_CHARS.test(text) ? HIDDEN_TEXT : PASS
  v = stricter(v, stricter(secrets(text), encoded(text)))
  if (dialect === 'bash') v = stricter(v, devClipboard(text))
  const inner = { ...ctx, dialect, depth: ctx.depth + 1, text: `${ctx.text}\n${text.toLowerCase()}` }
  if (!readsOnly(script)) v = stricter(v, stricter(writePathVerdict(text), judgeWritten(text, inner)))
  const cwd = ctx.state.cwd
  v = stricter(v, judgeScript(script, inner))
  if (ctx.state.cwd !== cwd) ctx.state.cwd = UNPLACEABLE
  return v
}

/** Judges words handed to another shell as one command line; any word known only at run time makes it screen. */
function judgeLine(words: readonly Word[], dialect: Dialect, ctx: Ctx): Verdict {
  if (words.some(w => w.dynamic)) return BUILT
  return judgeText(words.map(w => w.items.join(' ')).join(' '), dialect, ctx)
}

/** A shell or interpreter that reads its program from a pipe: what came down the pipe is unknown. */
const runsInput = (c: Call): Verdict => (DOWNLOADING.test(c.text) ? DOWNLOAD_RUN : BUILT)

/** A script file named at run time: a process substitution (`bash <(curl ...)`) or a variable for the file itself. */
const isBuiltFile = (word: Word): boolean => word.dynamic && /[$%!(`…]/.test(baseName(word.text))

// ---- Local files a command runs ----

/** A local file the command runs that cannot be read (or is named only at run time): screen. */
const SCRIPT_UNREADABLE = verdict('screen', 'script-file', 'runs a script file Jarvis cannot read')
/** A file Windows starts by its type (Windows Script Host, a shortcut, an installer) or a script for an interpreter Jarvis does not read. */
const RUNS_FILE = verdict('screen', 'script-host', 'runs a file Jarvis cannot judge')
/** A file the same command writes before it runs it: it runs as the command left it, not as Jarvis read it. */
const MADE_FIRST = verdict('screen', 'script-file', 'runs a file it writes first')

/** Programs that name files without changing what is in them: a file they name is not written (`chmod +x x.sh && ./x.sh`). */
const KEEPS_CONTENT = new Set([
  'chmod', 'chown', 'chgrp', 'attrib', 'icacls', 'takeown', 'unblock-file', 'test', '[', '[[', 'echo', 'printf', 'write-output',
  'write-host', 'stat', 'file', 'which', 'where', 'get-command', 'touch',
])

/**
 * The file names a command may write, for the parts after it (State.made): its redirections, and
 * every word after a program that may write what it names (not one that only reads, changes folder
 * or keeps content), but for the files it runs itself (`python build.py` does not write build.py).
 */
function noteMade(found: Found, command: Command, ranBefore: number, ctx: Ctx): void {
  const state = ctx.state
  const ranHere = new Set(state.ran.slice(ranBefore))
  const add = (text: string): void => {
    const name = fileName(text.replace(/^-[^=:]*[=:]/, '')) // --output=x.sh, -OutFile:x.ps1
    if (name !== '' && !ranHere.has(name)) state.made.add(name)
  }
  // A typed part (not a script file's own text) that names a file, for the check in judgeShell.
  if (ctx.files === undefined) {
    const here = new Set([...command.words, ...command.targets].map(w => fileName(w.text)))
    for (const name of here) state.namedIn.set(name, (state.namedIn.get(name) ?? 0) + 1)
  }
  for (const target of command.targets) add(target.text)
  const word = found.at === undefined ? undefined : command.words[found.at]
  if (found.at === undefined || word === undefined || word.block) return
  const names = programNames(word.text, ctx.dialect)
  if (names.some(name => READERS.has(name) || CD_NAMES.has(name) || KEEPS_CONTENT.has(name))) return
  for (const operand of command.words.slice(found.at + 1)) if (!operand.block) add(operand.text)
}

/** A relative path run or written after a `cd` to a folder Jarvis cannot place. */
const UNPLACED = verdict('screen', 'unplaced', 'runs or writes a path in a folder Jarvis cannot place')
const HIDDEN_TEXT = verdict('screen', 'hidden', 'has hidden characters')

/** What a file is read and judged as: a shell's script, an interpreter's code, a program, or how its first line says to run it (`exec`). */
type ReadAs = Dialect | 'python' | 'node' | 'perl' | 'ruby' | 'php' | 'osascript' | 'binary' | 'host'
type FileKind = ReadAs | 'exec'

/** Script files by extension, wherever they run: their dialect. */
const SCRIPT_EXT: Record<string, Dialect> = { ps1: 'pwsh', psm1: 'pwsh', sh: 'bash', bash: 'bash', ksh: 'bash', zsh: 'bash', bat: 'cmd', cmd: 'cmd' }
/** What Windows runs a file as, by its extension (through its file type); any other type but a document is screen. */
const WINDOWS_KINDS: Record<string, FileKind> = { ...SCRIPT_EXT, py: 'python', pyw: 'python' }
/** Interpreters' files by extension, for a file with no shebang. */
const CODE_EXT: Record<string, ReadAs> = { py: 'python', js: 'node', mjs: 'node', cjs: 'node', ts: 'node', mts: 'node', rb: 'ruby', pl: 'perl', php: 'php' }
/** What a shebang's program runs a file as; any other interpreter is one Jarvis cannot judge. */
const INTERPRETER_KINDS: Record<string, ReadAs> = {
  sh: 'bash', bash: 'bash', dash: 'bash', zsh: 'bash', ksh: 'bash', mksh: 'bash', ash: 'bash', pwsh: 'pwsh', powershell: 'pwsh',
  python: 'python', py: 'python', pypy: 'python', node: 'node', nodejs: 'node', deno: 'node', bun: 'node', tsx: 'node', 'ts-node': 'node',
  perl: 'perl', ruby: 'ruby', php: 'php', osascript: 'osascript',
}
/** The extensions cmd, ShellExecute and PowerShell try in a folder for a name given without one (PATHEXT's scripts; PowerShell adds .ps1). */
const LOOKUP_EXTS = ['bat', 'cmd', 'vbs', 'vbe', 'js', 'jse', 'wsf', 'wsh', 'msc', 'py', 'pyw']
/** File types Windows opens in a viewer or editor, where nothing of the file runs. */
const DOCUMENTS = new Set([
  'txt', 'md', 'markdown', 'log', 'csv', 'tsv', 'json', 'jsonc', 'yaml', 'yml', 'toml', 'xml', 'html', 'htm', 'pdf', 'png', 'jpg', 'jpeg', 'gif',
  'bmp', 'webp', 'svg', 'ico', 'tif', 'tiff', 'heic', 'mp3', 'wav', 'flac', 'ogg', 'm4a', 'aac', 'mp4', 'm4v', 'mkv', 'mov', 'avi', 'webm', 'wmv',
  'docx', 'xlsx', 'pptx', 'odt', 'ods', 'odp', 'rtf', 'epub', 'zip',
])
/** cmd's own commands: never looked up as files. */
const CMD_BUILTINS = new Set([
  'assoc', 'break', 'call', 'cd', 'chdir', 'cls', 'color', 'copy', 'date', 'del', 'dir', 'dpath', 'echo', 'endlocal', 'erase', 'exit', 'for',
  'ftype', 'goto', 'if', 'keys', 'md', 'mkdir', 'mklink', 'move', 'path', 'pause', 'popd', 'prompt', 'pushd', 'rd', 'rem', 'ren', 'rename',
  'rmdir', 'set', 'setlocal', 'shift', 'start', 'time', 'title', 'type', 'ver', 'verify', 'vol',
])

/** A file name's extension, lowercased, without the dot; '' for none (`python3.12` has none). */
const extensionOf = (name: string): string => /\.([a-z][a-z0-9_-]*)$/.exec(name)?.[1] ?? ''
/** A path's last name, lowercased, as Windows reads it: trailing dots and spaces dropped (`x.bat.` is x.bat). */
const fileName = (path: string): string => baseName(path).replace(/[. ]+$/, '').toLowerCase()
/** The folder a path is in: '.' for a bare name. */
function dirOf(path: string): string {
  const cut = Math.max(path.lastIndexOf('/'), path.lastIndexOf('\\'))
  return cut < 0 ? '.' : cut === 0 ? path.slice(0, 1) : path.slice(0, cut)
}
/** A path that does not depend on the folder a command runs in: absolute, from home, or from a variable. */
const isAnchored = (path: string): boolean => /^([\\/~$%]|[a-z]:)/i.test(path)
/** A link (`https:`, `spotify:`, `ms-settings:`), not a file: two letters or more before the colon. */
const isLink = (text: string): boolean => /^[a-z][a-z0-9+.-]+:(?!:)/i.test(text)
/** `path` in `folder` (a leading `./` dropped), with the folder's own separator (else the path's). */
function joinPath(folder: string, path: string): string {
  const separator = folder.includes('\\') || (!folder.includes('/') && path.includes('\\')) ? '\\' : '/'
  return `${folder.replace(/[\\/]+$/, '')}${separator}${path.replace(/^\.[\\/]+/, '')}`
}

/** The home folder at a word's start, as each shell spells it (`~` aside). */
const HOME_PREFIXES: Record<Dialect, RegExp> = {
  bash: /^\$\{?(HOME|USERPROFILE)\}?(?=$|[\\/])/,
  pwsh: /^\$\{?(home|env:home|env:userprofile)\}?(?=$|[\\/])/i,
  cmd: /^%(userprofile|home)%(?=$|[\\/])/i,
}

/**
 * A word as a path the guard can read: the home folder pc.ts gave for `~`, `$HOME`,
 * `$env:USERPROFILE` or `%USERPROFILE%` at its start, and the folder of the script being judged for
 * `$PSScriptRoot` and `%~dp0`. Undefined while any other part is known only at run time.
 */
function literalText(word: Word, ctx: Ctx): string | undefined {
  const home = ctx.resolved.home
  const folder = ctx.scriptDir
  let text = word.text
  if (home !== undefined && !word.quoted && ctx.dialect !== 'cmd') text = text.replace(/^~(?=$|[\\/])/, () => home)
  if (word.dynamic || ctx.dialect === 'cmd') {
    if (home !== undefined) text = text.replace(HOME_PREFIXES[ctx.dialect], () => home)
    if (folder !== undefined) text = text.replace(/^\$\{?psscriptroot\}?(?=$|[\\/])/i, () => folder).replace(/^%~dp0/i, () => `${folder}\\`)
  }
  if (!word.dynamic) return text
  return /[$%!(`…]/.test(text) || (ctx.dialect === 'bash' && text.includes('\\')) ? undefined : text
}

/** A word as a path written there, for matching only: its literal text, or as written when only a folder in it is a variable (`$HOME\.claude`). */
const pathText = (word: Word, ctx: Ctx): string | undefined => literalText(word, ctx) ?? (isBuiltFile(word) ? undefined : word.text)

/** Where a path a command names lands after its `cd`s: joined onto the folder named, or undefined when that folder cannot be read. */
function placed(path: string, ctx: Ctx): string | undefined {
  const cwd = ctx.state.cwd
  if (cwd.kind === 'here' || isAnchored(path)) return path
  return cwd.kind === 'at' && cwd.isLiteral ? joinPath(cwd.path, path) : undefined
}

/**
 * The content of a local file at `path`: its text; false when an optional file is not there; else a
 * verdict, screen when it cannot be read, and on the first pass the read need (pass for an optional
 * file, which may well not be there; screen for one the command runs by that very path).
 */
function fileContent(path: string, ctx: Ctx, isOptional = false): string | false | Verdict {
  const content = ctx.resolved.scripts?.[path]
  if (content === undefined) return isOptional ? withNeed(PASS, { kind: 'script', path, optional: true }) : withNeed(SCRIPT_UNREADABLE, { kind: 'script', path })
  if (content === null || (content === false && !isOptional)) return SCRIPT_UNREADABLE
  return content
}

/** A local file the command runs: read (in the folder a `cd` moved to) and judged as `kind`; screen until pc.ts reads it, or when it cannot. */
function runFile(path: string, kind: FileKind, ctx: Ctx): Verdict {
  if (ctx.state.made.has(fileName(path))) return MADE_FIRST
  ctx.state.ran.push(fileName(path))
  const at = placed(path, ctx)
  if (at === undefined) return UNPLACED
  const got = fileContent(at, ctx)
  if (typeof got === 'string') return judgeFile(got, kind, at, ctx)
  return got === false ? SCRIPT_UNREADABLE : got
}

/** Files a name may run (cmd's and PowerShell's lookup by extension, Git Bash's x.exe for x): every one there is judged; with none there, `none`. */
function lookup(candidates: readonly { path: string; kind: FileKind }[], none: Verdict, ctx: Ctx): Verdict {
  let v = PASS
  let isThere = false
  for (const candidate of candidates) {
    if (ctx.state.made.has(fileName(candidate.path))) return MADE_FIRST
    ctx.state.ran.push(fileName(candidate.path))
    const at = placed(candidate.path, ctx)
    if (at === undefined) return UNPLACED
    const got = fileContent(at, ctx, true)
    if (got === false) continue
    if (typeof got === 'string') {
      isThere = true
      v = stricter(v, judgeFile(got, candidate.kind, at, ctx))
    } else if (got.needs !== undefined) {
      v = stricter(v, got) // not read yet
    } else {
      // There but unreadable: screen, but for a program (`x.exe`), which is judged by its name.
      isThere = true
      if (candidate.kind !== 'binary') v = stricter(v, got)
    }
  }
  return isThere ? v : stricter(v, none)
}

/** The kind a shebang names (`#!/usr/bin/env bash`); undefined without one, 'host' for an interpreter Jarvis cannot judge. */
function shebangKind(text: string): ReadAs | undefined {
  const line = /^#!([^\n]*)/.exec(text)?.[1]
  if (line === undefined) return undefined
  const words = line.trim().split(/\s+/)
  let program = baseName(words[0] ?? '').toLowerCase()
  if (program === 'env') program = baseName(words.slice(1).find(word => !word.startsWith('-') && !/^\w+=/.test(word)) ?? '').toLowerCase()
  return INTERPRETER_KINDS[program.replace(/[\d.]+$/, '')] ?? 'host'
}

/**
 * A file's text judged as `kind`. An `exec` file (one run straight by its path) is a program when
 * binary, else read as its shebang says; with none, a shell runs it, so it is judged as bash (and as
 * its extension's language too). A file already being judged further up is judged there.
 */
function judgeFile(text: string, kind: FileKind, path: string, ctx: Ctx): Verdict {
  const key = normalizePath(path)
  if (ctx.files?.includes(key) === true) return PASS
  const inner: Ctx = { ...ctx, scriptDir: dirOf(path), files: [...(ctx.files ?? []), key] }
  const body = text.replace(/^\ufeff/, '')
  if (kind !== 'exec') return judgeAs(body, kind, inner)
  if (body.slice(0, 4096).includes('\u0000')) return PASS
  const shebang = shebangKind(body)
  if (shebang !== undefined) return judgeAs(body, shebang, inner)
  const code = CODE_EXT[extensionOf(fileName(path))]
  return code === undefined ? judgeAs(body, 'bash', inner) : stricter(judgeAs(body, 'bash', inner), judgeAs(body, code, inner))
}

function judgeAs(body: string, kind: ReadAs, ctx: Ctx): Verdict {
  switch (kind) {
    case 'pwsh':
    case 'bash':
    case 'cmd':
      return judgeText(body, kind, ctx)
    case 'binary':
      return PASS
    case 'host':
      return RUNS_FILE
    default: {
      const text = normalizeText(body)
      return HIDDEN_CHARS.test(text) ? HIDDEN_TEXT : inlineVerdict(kind, text)
    }
  }
}

/** A program file straight in a system folder: /bin, /sbin, /usr/bin, /usr/sbin (in Git Bash, Git's own). */
const SYSTEM_PROGRAM = /^(\/usr)?\/s?bin\/[^\\/]+$/

/** How a file named as a program is started: by a shell's own rules, or (`shell`) by its type, as Start-Process, Invoke-Item and cmd's start do. */
type RunMode = Dialect | 'shell'

/**
 * A program word or a launcher's argument that may name a local file: read and judged as `mode`
 * starts it (runLocal). One whose folder or name is known only at run time cannot be read: screen,
 * unless it is a program by name (`.exe`, a bare name a shell finds on PATH).
 */
function launch(word: Word, mode: RunMode, ctx: Ctx): Verdict {
  if (word.block) return PASS
  const text = literalText(word, ctx)
  if (text !== undefined) return runLocal(text, mode, ctx)
  const ext = extensionOf(fileName(word.text))
  if (ext === 'exe' || ext === 'com') return PASS
  return /[\\/]/.test(word.text) || ext !== '' || mode === 'cmd' || mode === 'shell' ? SCRIPT_UNREADABLE : PASS
}

/**
 * A program named by `path`, as `mode` starts it:
 * - a program by name or `.exe` is judged by its name alone (the rows);
 * - a script by extension (`.ps1`, `.sh`, `.bat`, `.cmd`) is read and judged in its dialect;
 * - bash runs a path straight: a binary is a program, a text file runs as its shebang says (else as
 *   a shell script), and Git Bash finds `x.exe` for `./x`; neither there is screen;
 * - Windows (PowerShell, cmd, Start-Process) starts other files by their type: `.py` is read, a
 *   document opens in its viewer, and anything else (Windows Script Host, a shortcut, an installer)
 *   is screen. A name without an extension is looked up in its folder (cmd and Start-Process for a
 *   bare name too, PowerShell for a path, with .ps1 first), and each script found there is judged.
 */
function runLocal(path: string, mode: RunMode, ctx: Ctx): Verdict {
  const url = /^file:(?:\/\/)?(.*)$/i.exec(path)
  const plain = path.replace(/^(microsoft\.powershell\.core\\)?filesystem::/i, '')
  if (url === null && isLink(plain)) return PASS
  const file = url === null ? plain : (url[1] ?? '').replace(/^\/(?=[a-z]:)/i, '')
  const name = fileName(file)
  if (name === '' || name.startsWith(':') || /^[a-z]:$/i.test(file)) return PASS // a folder (`.`, `..`), a cmd label, a drive
  // A program in the system's own folders (only an administrator writes there) is judged by its name, as on PATH.
  if (SYSTEM_PROGRAM.test(file)) return PASS
  const isQualified = /[\\/]/.test(file)
  const ext = extensionOf(name)
  if (ext === 'exe' || ext === 'com') return PASS
  const dialect = SCRIPT_EXT[ext]
  if (mode === 'bash') {
    if (dialect !== undefined) return runFile(file, dialect, ctx)
    if (!isQualified) return PASS
    return lookup([{ path: file, kind: 'exec' }, ...(ext === '' ? [{ path: `${file}.exe`, kind: 'binary' as const }] : [])], SCRIPT_UNREADABLE, ctx)
  }
  if (mode === 'cmd' && !isQualified && ext === '' && CMD_BUILTINS.has(name)) return PASS
  if (ext !== '') {
    const kind = WINDOWS_KINDS[ext]
    if (kind !== undefined) return runFile(file, kind, ctx)
    return DOCUMENTS.has(ext) ? PASS : RUNS_FILE
  }
  if (mode === 'pwsh' && !isQualified) return PASS // PowerShell runs a bare name only from PATH
  const exts = mode === 'pwsh' ? ['ps1', ...LOOKUP_EXTS] : LOOKUP_EXTS
  return lookup(exts.map(one => ({ path: `${file}.${one}`, kind: WINDOWS_KINDS[one] ?? 'host' })), PASS, ctx)
}

// ---- Where a command runs: cd ----

const CD_NAMES = new Set(['cd', 'chdir', 'sl', 'set-location', 'pushd', 'push-location', 'popd', 'pop-location'])

/** A `cd` (Set-Location, pushd, popd): where the command's later relative paths land. */
function changeDir(word: Word, args: readonly Word[], ctx: Ctx): void {
  const state = ctx.state
  // cmd's and PowerShell's `cd..` and `cd\`, and a drive switch (`D:`): somewhere Jarvis does not follow.
  if (/^(cd|chdir)[.\\]/i.test(word.text) || (ctx.dialect !== 'bash' && /^[a-z]:$/i.test(word.text))) {
    state.cwd = UNPLACEABLE
    return
  }
  const name = programNames(word.text, ctx.dialect)[0] ?? ''
  if (!CD_NAMES.has(name)) return
  if (name === 'popd' || name === 'pop-location') {
    state.cwd = state.stack.pop() ?? UNPLACEABLE
    return
  }
  if (name === 'pushd' || name === 'push-location') state.stack.push(state.cwd)
  const target = cdTarget(args, ctx)
  if (target === 'stays') return
  if (target === undefined) {
    state.cwd = UNPLACEABLE
    return
  }
  const cwd = state.cwd
  if (isAnchored(target.path) || cwd.kind === 'here') state.cwd = { kind: 'at', ...target }
  else state.cwd = cwd.kind === 'at' ? { kind: 'at', path: joinPath(cwd.path, target.path), isLiteral: cwd.isLiteral && target.isLiteral } : UNPLACEABLE
}

/**
 * The folder a `cd` names: its text and whether it can be read as it stands (`$HOME\x` can be matched
 * but not read); undefined when it is not known (a variable, `cd -`, a drive's own folder, a registry
 * or other provider drive), and 'stays' for cmd's bare `cd`, which only prints the folder.
 */
function cdTarget(args: readonly Word[], ctx: Ctx): { path: string; isLiteral: boolean } | undefined | 'stays' {
  const values: Word[] = []
  for (let i = 0; i < args.length; i++) {
    const word = args[i]
    if (word === undefined || word.block) return undefined
    const low = word.text.toLowerCase()
    if (ctx.dialect === 'cmd' && low === '/d') continue
    if (word.quoted || !low.startsWith('-') || low === '-') {
      values.push(word)
      continue
    }
    if (ctx.dialect !== 'pwsh') continue // bash's -L, -P, -e, -@ and --
    const option = low.slice(1).split(':')[0] ?? ''
    const isPath = ['path', 'literalpath', 'pspath', 'lp'].some(one => one.startsWith(option))
    if (!isPath && !'stackname'.startsWith(option)) continue // -PassThru
    if (low.includes(':')) {
      if (isPath) values.push({ ...word, text: word.text.slice(word.text.indexOf(':') + 1) })
      continue
    }
    const value = args[++i]
    if (isPath && value !== undefined) values.push(value)
  }
  if (values.length === 0) return ctx.dialect === 'cmd' ? 'stays' : ctx.dialect === 'bash' ? { path: ctx.resolved.home ?? '~', isLiteral: true } : undefined
  // cmd's cd takes the rest of the line as the folder, spaces and all.
  const parts = ctx.dialect === 'cmd' ? values : values.slice(0, 1)
  const texts = parts.map(word => pathText(word, ctx))
  if (texts.some(text => text === undefined)) return undefined
  const path = texts.join(' ')
  if (/^[-+]\d*$/.test(path) || /^[a-z][\w.-]+:/i.test(path) || /^[a-z]:(?![\\/])/i.test(path)) return undefined
  return { path, isLiteral: parts.every(word => literalText(word, ctx) !== undefined) }
}

/**
 * A command's word that may name a path in a folder Jarvis cannot place: it has a separator, an
 * extension or a dot (`.`, `a.bat`, `sub/x`), or would name a protected place in Claude Code's or
 * Jarvis's folder (`plugins`, `settings.json`). A plain word (`test`, `build`, `status`) does not.
 */
function mayBePath(text: string): boolean {
  if (/[\\/.]/.test(text)) return true
  return writePathVerdict(`/.claude/${text}`).tier !== 'pass' || secrets(`/.jarvis/home/${text}`).tier !== 'pass'
}

/**
 * After a `cd` in the same command, a relative path a later part names lands in that folder: what is
 * written there (any operand of a command that does not only read, or a redirection) takes the tier
 * that path earns, and what is read there is checked for Jarvis's secrets. When the folder cannot be
 * placed, a redirection there, or an operand that may be a path, is screen (reading there is not).
 */
function judgeMovedPaths(found: Found, command: Command, ctx: Ctx): Verdict {
  const cwd = ctx.state.cwd
  if (cwd.kind === 'here') return PASS
  const word = found.at === undefined ? undefined : command.words[found.at]
  const names = word === undefined || word.dynamic || word.block ? [] : programNames(word.text, ctx.dialect)
  if (names.some(name => CD_NAMES.has(name))) return PASS
  const onlyReads = names.some(name => READERS.has(name))
  const operands = found.at === undefined ? [] : command.words.slice(found.at + 1).filter(w => !w.block && (w.quoted || !w.text.startsWith('-')))
  let v = PASS
  for (const [i, operand] of [...operands, ...command.targets].entries()) {
    const isTarget = i >= operands.length
    const writes = !onlyReads || isTarget
    const text = pathText(operand, ctx)
    if (text === undefined || isAnchored(text) || isLink(text)) continue
    if (cwd.kind === 'unknown') {
      const reachesSecrets = secrets(`/.jarvis/home/${text}`).tier !== 'pass'
      if (isTarget || reachesSecrets || (writes && mayBePath(text))) v = stricter(v, UNPLACED)
      continue
    }
    const path = joinPath(cwd.path, text)
    v = stricter(v, secrets(path))
    if (writes) v = stricter(v, writePathVerdict(path))
  }
  return v
}

// ---- Launchers ----

/** bash `source` and `.`: the file runs in this shell; its text is read and judged as bash. */
const sourceFile: Evaluator = (c, ctx) => {
  const file = c.args.find(w => !w.text.startsWith('-'))
  if (file === undefined || file.block) return PASS
  if (isBuiltFile(file)) return runsInput(c)
  const path = literalText(file, ctx)
  return path === undefined ? SCRIPT_UNREADABLE : runFile(path, 'bash', ctx)
}

/** A script file a shell or interpreter reads on stdin (`bash < x.sh`), judged as `kind`. */
const stdinFiles = (c: Call, kind: FileKind, ctx: Ctx): Verdict =>
  c.reads.reduce((v, word) => {
    const path = literalText(word, ctx)
    return stricter(v, path === undefined ? SCRIPT_UNREADABLE : runFile(path, kind, ctx))
  }, PASS)

/** Invoke-Item (ii) and explorer open each file by its type, which may run it: each is judged as Windows starts it. */
const openItems: Evaluator = (c, ctx) => {
  let v = PASS
  for (const [i, word] of c.args.entries()) {
    const low = c.low[i] ?? ''
    if (word.block || (!word.quoted && (low.startsWith('-') || (c.name === 'explorer' && low.startsWith('/'))))) continue
    v = stricter(v, launch(word, 'shell', ctx))
  }
  return v
}

/** A module file Import-Module or `using module` loads runs its code: a .psm1 or .ps1 is read and judged; a manifest, a binary module or a module folder is not read: screen. */
const MODULE_UNREADABLE = verdict('screen', 'script-file', 'loads a module Jarvis cannot read')
function moduleFile(word: Word, ctx: Ctx): Verdict {
  if (word.block) return PASS
  const text = literalText(word, ctx)
  const isPath = /[\\/]/.test(word.text) || extensionOf(fileName(word.text)) !== ''
  if (text === undefined) return isPath || isBuiltFile(word) ? MODULE_UNREADABLE : PASS
  if (!isPath) return PASS // a module by name, from PSModulePath
  const ext = extensionOf(fileName(text))
  return ext === 'psm1' || ext === 'ps1' ? runFile(text, 'pwsh', ctx) : MODULE_UNREADABLE
}

const importModule: Evaluator = (c, ctx) => {
  let v = PASS
  for (const [i, word] of c.args.entries()) {
    if (!word.quoted && (c.low[i] ?? '').startsWith('-')) continue
    v = stricter(v, moduleFile(word, ctx))
  }
  return v
}

/** PowerShell's `using module .\x.psm1` statement loads the module as Import-Module does. */
function usingModule(command: Command, ctx: Ctx): Verdict {
  const [first, second, third] = command.words
  if (first?.text.toLowerCase() !== 'using' || second?.text.toLowerCase() !== 'module' || third === undefined) return PASS
  return moduleFile(third, ctx)
}

/** Rename-Item and cmd's ren: the new name lands in the item's own folder, so it is judged there. */
const renameItem: Evaluator = (c, ctx) => {
  if (c.dialect === 'bash') return PASS // bash's rename is another tool (`rename 's/a/b/' files`)
  let source: Word | undefined
  let name: Word | undefined
  for (let i = 0; i < c.args.length; i++) {
    const word = c.args[i]
    const low = c.low[i] ?? ''
    if (word === undefined || word.block) continue
    if (c.dialect === 'pwsh' && !word.quoted && /^-[a-z]/.test(low)) {
      const option = low.slice(1).split(':')[0] ?? ''
      const takesValue = ['newname', 'path', 'literalpath', 'pspath', 'lp', 'credential'].some(one => one.startsWith(option))
      const glued = low.includes(':') ? { ...word, text: word.text.slice(word.text.indexOf(':') + 1) } : undefined
      const value = glued ?? (takesValue ? c.args[++i] : undefined)
      if ('newname'.startsWith(option)) name = value
      else if (['path', 'literalpath', 'pspath', 'lp'].some(one => one.startsWith(option))) source = value
      continue
    }
    if (source === undefined) source = word
    else name ??= word
  }
  const from = source === undefined ? undefined : pathText(source, ctx)
  const to = name === undefined ? undefined : pathText(name, ctx)
  if (from === undefined || to === undefined) return PASS
  const target = /[\\/]/.test(to) ? to : `${from.slice(0, Math.max(from.lastIndexOf('/'), from.lastIndexOf('\\')) + 1)}${to}`
  const cwd = ctx.state.cwd
  const path = isAnchored(target) || cwd.kind !== 'at' ? target : joinPath(cwd.path, target)
  return stricter(secrets(path), writePathVerdict(path))
}

/** A link or junction to Claude Code's or Jarvis's folder lets a later write or read reach it under another name. */
const LINKS_PROTECTED = verdict('screen', 'link', "links to Claude Code's or Jarvis's folder")

/** New-Item -ItemType SymbolicLink/Junction/HardLink, mklink, ln and fsutil hardlink: screen when any path names a protected folder. */
const makesLink: Evaluator = (c, ctx) => {
  if (c.name === 'new-item' && !/^(sym|jun|har|\$)/.test(paramValue(c, 'itemtype') ?? paramValue(c, 'type') ?? '')) return PASS
  if (c.name === 'fsutil' && !(c.low.includes('hardlink') && c.low.includes('create'))) return PASS
  const cwd = ctx.state.cwd
  return c.args.some(word => {
    const text = word.block ? undefined : pathText(word, ctx)
    if (text === undefined) return false
    return namesProtectedFolder(isAnchored(text) || cwd.kind !== 'at' ? text : joinPath(cwd.path, text))
  })
    ? LINKS_PROTECTED
    : PASS
}

// ---- Files Claude wrote ----

/** The files Claude may write and a command then run, by extension: what they are read as. Others (source code, data) are not followed. */
const WRITTEN_KINDS: Record<string, FileKind> = {
  ps1: 'pwsh', psm1: 'pwsh', sh: 'bash', bash: 'bash', zsh: 'bash', ksh: 'bash', bat: 'cmd', cmd: 'cmd',
  vbs: 'host', vbe: 'host', jse: 'host', wsf: 'host', wsh: 'host', hta: 'host',
}

const escapeRegExp = (text: string): string => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

/**
 * A script Claude wrote or edited this session (pc.ts keeps the list) that a command names, by any
 * launcher or spelling Jarvis does not know (`wt deploy.ps1`, `nohup ship &`, `--file=C:/x/run.bat`):
 * read and judged as it would run. Its name counts with its extension, or without one after a path
 * separator (`.\deploy`); a file without an extension counts only when it starts with a shebang. A
 * file no longer there adds nothing; one that cannot be read is screen.
 */
function judgeWritten(text: string, ctx: Ctx): Verdict {
  const written = ctx.resolved.written
  if (written === undefined || written.length === 0) return PASS
  const hay = ` ${normalizePath(text)} `
  const end = '(?=[\\s,;)&|<>]|$)'
  let v = PASS
  for (const path of written) {
    const name = fileName(path)
    const ext = extensionOf(name)
    const kind = ext === '' ? 'exec' : WRITTEN_KINDS[ext]
    if (name === '' || kind === undefined) continue
    const stem = ext === '' ? name : name.slice(0, -(ext.length + 1))
    const named =
      new RegExp(`[\\s/=:,;(&|<>@]${escapeRegExp(name)}${end}`).test(hay) || (ext !== '' && new RegExp(`/${escapeRegExp(stem)}${end}`).test(hay))
    if (!named) continue
    ctx.state.named.add(name)
    const got = fileContent(path, ctx, true)
    if (got === false) continue
    if (typeof got !== 'string') {
      v = stricter(v, got)
      continue
    }
    if (kind === 'exec' && !got.replace(/^\ufeff/, '').startsWith('#!')) continue
    v = stricter(v, judgeFile(got, kind, path, ctx))
  }
  return v
}

/** pwsh: a cmdlet name from the table standing as a plain word anywhere (`Set-Alias x Remove-Item`). */
function judgeBarewords(command: Command, at: number | undefined, ctx: Ctx): Verdict {
  let v = PASS
  command.words.forEach((word, i) => {
    if (i === at || word.quoted || word.dynamic || word.block) return
    const name = word.text.toLowerCase()
    if (!BAREWORDS.has(name)) return
    v = stricter(v, judgeRows({ name, dialect: 'pwsh', args: [], low: [], piped: false, stdin: [], reads: [], loop: false, text: ctx.text }))
  })
  return v
}

/** pwsh patterns in the code itself, outside quotes: .NET calls and methods. `text` must match the whole text too. */
const CODE_RULES: readonly { tier: Tier; rule: string; reason: string; code: RegExp; text?: RegExp }[] = [
  { tier: 'never', rule: 'keystrokes', reason: 'types keystrokes into a window', code: /\bsendkeys\b|\bsendwait\s*\(/ },
  {
    tier: 'never', rule: 'keystrokes', reason: 'types keystrokes into a window', code: /\badd-type\b/,
    text: /keybd_event|sendinput|sendkeys|mouse_event|postmessage|sendmessage/,
  },
  { tier: 'never', rule: 'restore-logs', reason: 'deletes restore points or logs', code: /\.delete\s*\(/, text: /win32_shadowcopy/ },
  { tier: 'never', rule: 'restore-logs', reason: 'deletes restore points or logs', code: /clearlog\s*\(|\.clear\s*\(\s*\)/, text: /eventlog/ },
  { tier: 'screen', rule: 'delete', reason: 'deletes files', code: /(::|\.)delete(directory|file)?\s*\(/ },
  { tier: 'screen', rule: 'email', reason: 'sends email or messages', code: /\.send\s*\(/, text: /outlook\.application/ },
  { tier: 'screen', rule: 'built', reason: BUILT.reason, code: /\[scriptblock\]::create\s*\(|\.invoke(returnasis)?\s*\(|\$executioncontext\.invokecommand/ },
  { tier: 'screen', rule: 'add-type', reason: 'compiles and runs code', code: /\[(system\.)?reflection\.assembly\]::load/ },
  { tier: 'screen', rule: 'link', reason: LINKS_PROTECTED.reason, code: /create(symbolic|hard)link\s*\(|createjunction\s*\(/, text: /\.claude|\.jarvis|claude~|jarvis~/ },
  // Programs started through .NET or COM, out of the shell's sight.
  { tier: 'screen', rule: 'hidden', reason: HIDDEN.reason, code: /process\]::start\s*\(|processstartinfo|\.shellexecute\s*\(/ },
  { tier: 'screen', rule: 'hidden', reason: HIDDEN.reason, code: /\.(run|exec)\s*\(/, text: /wscript\.shell/ },
  { tier: 'screen', rule: 'hidden', reason: HIDDEN.reason, code: /\.create\s*\(/, text: /win32_process\b/ },
  { tier: 'screen', rule: 'security', reason: 'changes a system setting', code: /setenvironmentvariable\s*\(/, text: /['"]machine['"]|::machine\b/ },
  { tier: 'voice', rule: 'setting', reason: 'changes a setting', code: /setenvironmentvariable\s*\(/, text: /['"]user['"]|::user\b/ },
  { tier: 'voice', rule: 'close', reason: 'closes programs', code: /\.(kill|closemainwindow)\s*\(/ },
  { tier: 'voice', rule: 'clipboard', reason: 'shows the clipboard to Claude', code: /clipboard\]::get/ },
  { tier: 'voice', rule: 'screenshot', reason: 'takes a screenshot', code: /copyfromscreen\s*\(/ },
]

function judgeCode(command: Command, ctx: Ctx): Verdict {
  if (ctx.dialect !== 'pwsh') return PASS
  const code = command.code.toLowerCase()
  if (/\.foreach\s*\(/.test(code)) ctx.state.loop = true
  let v = PASS
  for (const rule of CODE_RULES) {
    if (rule.code.test(code) && (rule.text === undefined || rule.text.test(ctx.text))) v = stricter(v, verdict(rule.tier, rule.rule, rule.reason))
  }
  return v
}

// ---- Raw-text checks ----

/** A `..` after a name, folded away (`a/x/../b` is `a/b`): mid-path, and at a token's start. */
const DOTDOT_MID = /\/(?!\.\.?(?:\/|$|[\s,;)]))[^/\s,;()]+\/\.\.(?=\/|$|[\s,;)])/
const DOTDOT_START = /(^|[\s=:,;(])(?!\.\.?(?:\/|$|[\s,;)]))[^/\s=:,;()]+\/\.\.(?:\/|(?=$|[\s,;)]))/

/**
 * A path in any spelling, as Windows reads it: lowercased, `\` as `/`, quotes and pwsh backticks
 * dropped, doubled separators and `.` segments collapsed, the trailing dots Windows drops from a name
 * dropped (`.claude.\` is `.claude\`), and `x/..` folded away.
 */
function normalizePath(text: string): string {
  let path = text
    .toLowerCase()
    .replace(/\\/g, '/')
    .replace(/['"`]/g, '')
    .replace(/\/{2,}/g, '/')
    .replace(/\/(\.\/)+/g, '/')
    .replace(/\/\.(?=$|[\s,;)])/g, '')
    .replace(/([^/.\s])\.+(?=\/|$|[\s,;)])/g, '$1')
  for (let i = 0; i < 32; i++) {
    const next = path.replace(DOTDOT_MID, '').replace(DOTDOT_START, '$1')
    if (next === path) break
    path = next
  }
  return path
}

/**
 * The spellings a path check looks at: normalized with `..` folded, and before folding (a `..` after
 * a link leads where the link's target leads, so the stricter of the two wins).
 */
function pathForms(text: string): string[] {
  const folded = normalizePath(text)
  const unfolded = text.toLowerCase().replace(/\\/g, '/').replace(/['"`]/g, '').replace(/\/{2,}/g, '/')
  return folded === unfolded ? [folded] : [folded, unfolded]
}

/** Claude Code's own settings, permissions and plugins. */
const PROTECTED_PATH =
  /\.claude\/settings(\.local)?\.json|\.claude\.json|managed-settings\.json|\/claudecode(\/|$|[\s,;)])|\/etc\/claude-code(\/|$|[\s,;)])|\.claude\/plugins(\/|$|[\s,;)])/

/** A `.claude` folder named apart from what is in it (`Join-Path $HOME .claude plugins`, `[IO.Path]::Combine($HOME, '.claude', 'skills')`). */
const CLAUDE_APART = /(^|[\s=:,;(/])\.claude\/?([\s,;)]|$)/
/** A word standing apart, after a `.claude` named apart: `plugins`, `skills x`. */
const apart = (word: string): RegExp => new RegExp(`(^|[\\s=:,;(])${word}([\\s/,;)]|$)`)
const PLUGINS_APART = apart('plugins')
const CONFIG_APART = apart('(skills|agents|commands|hooks)')

function mentionsProtected(text: string): boolean {
  return pathForms(text).some(path => {
    if (PROTECTED_PATH.test(path)) return true
    if (hasProtectedShortName(path)) return true
    // A `.claude` folder and a settings file or the plugins folder named apart: `Join-Path $HOME .claude settings.json`.
    return CLAUDE_APART.test(path) && (/settings(\.local)?\.json/.test(path) || PLUGINS_APART.test(path))
  })
}

/** The stems of the 8.3 short-name tokens (`.../SETTIN~1.JSO x` -> `settin`) in a normalized path or command, lowercased. */
function shortNameStems(text: string): string[] {
  return [...text.matchAll(/(?:^|[\s/=:,;("'`])([a-z0-9]+)~\d/g)].map(m => m[1] ?? '')
}

/**
 * Whether `stem` could be the short name of one of `fulls`: either is a prefix of the other
 * (`SETTIN~1`), or it is the hashed form Windows makes once four such names clash in a folder
 * (`SE12AB~1`: the first two letters, then four hex digits).
 */
const stemCouldBe = (stem: string, ...fulls: string[]): boolean =>
  fulls.some(full => full.startsWith(stem) || stem.startsWith(full) || (/^[a-z0-9]{2}[0-9a-f]{4}$/.test(stem) && full.startsWith(stem.slice(0, 2))))

/**
 * Windows makes 8.3 short names (`SETTIN~1.JSO` for settings.json, `CLAUDE~1` for .claude) on the
 * system volume, and a tool opens the long path behind them, so a short name must be judged like the
 * long one. True when a `~<digit>` token could name `.claude`, a settings file, `.claude/plugins` or
 * `.mcp` (normalizePath has already lowered the text and turned `\` into `/`).
 */
function hasProtectedShortName(path: string): boolean {
  return shortNameStems(path).some(stem => stemCouldBe(stem, 'claude', 'settin', 'manage', 'plugin', 'mcp'))
}

/** Whether a path carries an 8.3 short-name token at all (a Write/Edit then is not resolved until its real path has none). */
export function hasShortName(path: string): boolean {
  return typeof path === 'string' && path.split(/[\\/]/).some(segment => /^[^\\/]*[a-z0-9]~\d/i.test(segment))
}

/**
 * Claude Code configuration that can grant tools or run code without touching the settings files:
 * a skills-folder plugin (reference.md: `~/.claude/skills/<name>` and `<project>/.claude/skills/<name>`
 * load as plugins), an agent, a command, a hooks file, or `.mcp.json`. Screen tier, so authoring one
 * still works after a click; the settings files and `.claude/plugins` are stricter (never), above.
 */
const CLAUDE_CONFIG_PATH = /\.claude\/(skills|agents|commands|hooks)(\/|$|[\s,;)])|(^|[\s/=:,;(])\.mcp\.json([\s,;)]|$)/

function mentionsClaudeConfig(text: string): boolean {
  return pathForms(text).some(path => CLAUDE_CONFIG_PATH.test(path) || (CLAUDE_APART.test(path) && CONFIG_APART.test(path)))
}

/**
 * Places a file written there runs on its own, so a write into one is screen tier: a Startup folder
 * (by its path, or as .NET or Windows Script Host name it), a PowerShell profile (`$PROFILE`, or a
 * profile file by its name), a shell's startup file (`~/.bashrc`, `~/.profile`: Git Bash runs them),
 * a Git hook or a repository's git config (core.hooksPath, core.fsmonitor), or a scheduled-task
 * folder. Catching the path means a write that side-steps schtasks or the profile cmdlets
 * (`Set-Content ...\Startup\a.bat`) is not missed.
 */
const AUTORUN_PATH = new RegExp(
  [
    String.raw`\/start menu\/programs\/startup(\/|$|[\s,;)])`,
    String.raw`getfolderpath\s*\(\s*(\[[\w.+]*\]::)?\s*(common)?startup\b`,
    String.raw`getfolderpath\s*\(\s*(7|24)\s*\)`,
    String.raw`specialfolders?\s*\(\s*(allusers)?startup\b`,
    String.raw`specialfolder\]::(common)?startup\b`,
    String.raw`(^|[\s/=:,;(])\$profile\b`,
    String.raw`(^|[\s/=:,;(])(microsoft\.powershell|microsoft\.vscode|powershell)_profile\.ps1`,
    String.raw`\/profile\.ps1(\/|$|[\s,;)])`,
    String.raw`\.git\/hooks(\/|$|[\s,;)])`,
    String.raw`\.git\/config(\/|$|[\s,;)])`,
    String.raw`\/(system32|windows)\/tasks\/`,
    String.raw`(^|[\s/=:,;(])\.(bashrc|bash_profile|bash_login|bash_logout|profile|zshrc|zprofile|zshenv|zlogin)([\s,;)]|$)`,
  ].join('|'),
)

function mentionsAutorun(text: string): boolean {
  return pathForms(text).some(path => AUTORUN_PATH.test(path))
}

const AUTORUN = verdict('screen', 'autorun', 'writes to a startup location')

/** The tier a write to `text` (a path, or a command's text) earns from the path alone: never for the settings, screen for Claude Code config and startup locations. */
function writePathVerdict(text: string): Verdict {
  if (mentionsProtected(text)) return CLAUDE_SETTINGS
  if (mentionsClaudeConfig(text)) return CLAUDE_CONFIG
  if (mentionsAutorun(text)) return AUTORUN
  return PASS
}

/**
 * Whether a link's path or target names Claude Code's or Jarvis's own folder (`.claude`, `.jarvis`,
 * `.claude.json`, by any spelling, a short name too) or anything a write there would make stricter.
 */
function namesProtectedFolder(text: string): boolean {
  if (writePathVerdict(text).tier !== 'pass' || secrets(text).tier !== 'pass') return true
  return pathForms(text).some(
    path =>
      /(^|[\s/=:,;(])\.(claude|jarvis)(\.json)?(\/|$|[\s,;)])/.test(path) ||
      shortNameStems(path).some(stem => stemCouldBe(stem, 'claude', 'jarvis')),
  )
}

/** Programs that only read, so naming a protected path with them is allowed. */
const READERS = new Set(['get-content', 'cat', 'type', 'test-path', 'ls', 'get-childitem', 'dir', 'get-item', 'select-string', 'grep'])

/** Whether every command of the script (nested ones too) only reads or changes folder, with no redirection into a file. */
function readsOnly(script: Script): boolean {
  if (script.problem !== undefined) return false
  return script.commands.every(command => {
    if (command.writes) return false
    const found = findCommand(command.words, script.dialect, newState())
    const word = found.at === undefined ? undefined : command.words[found.at]
    if (word === undefined) {
      if (command.words.length > 0) return false
    } else if (word.dynamic || word.block || !programNames(word.text, script.dialect).some(name => READERS.has(name) || CD_NAMES.has(name))) {
      return false
    }
    return command.inner.every(readsOnly)
  })
}

/** Jarvis's key and secrets, and its helper's local command port. */
function secrets(text: string): Verdict {
  const path = normalizePath(text)
  if (/jarvis_token|fish_audio_api_key/.test(path) || path.includes('.jarvis/home/credentials')) return JARVIS_SECRETS
  if (hasJarvisSecretShortName(path)) return JARVIS_SECRETS
  if (LOCAL_HOST.test(path) && path.includes('/v1/')) return JARVIS_HELPER
  return PASS
}

/** The 8.3 short-name spelling of `.jarvis/home/credentials*`: a `.jarvis` segment and a credentials file, either spelled short. */
function hasJarvisSecretShortName(path: string): boolean {
  const stems = shortNameStems(path)
  if (stems.length === 0) return false
  const jarvis = /(^|[\s/=:,;("'`])\.jarvis(\/|$|[\s,;)])/.test(path) || stems.some(s => stemCouldBe(s, 'jarvis'))
  const credentials = /credentials/.test(path) || stems.some(s => stemCouldBe(s, 'creden'))
  return jarvis && credentials
}

const CLIPBOARD = verdict('voice', 'clipboard', 'shows the clipboard to Claude')

/** Whether a shell glob (`*`, `?`, `[...]`), or a plain name, matches `name`. */
function globMatches(pattern: string, name: string): boolean {
  if (!/[*?[]/.test(pattern)) return pattern === name
  try {
    return new RegExp(`^${pattern.replace(/[.+^${}()|\\]/g, '\\$&').replace(/\*/g, '.*').replace(/\?/g, '.')}$`).test(name)
  } catch {
    return true
  }
}

/**
 * bash: in Git Bash (the Bash tool on Windows) /dev/clipboard is the
 * clipboard, so reading it (an argument, `< /dev/clipboard`, `/dev/clip*`,
 * or a name after `cd /dev`) shows it to Claude. Writing to it
 * (`> /dev/clipboard`) passes.
 */
function devClipboard(text: string): Verdict {
  const path = normalizePath(text).replace(/(^|[^<])>[>|]?\s*\/dev\/clipboard(?=[\s;&|)]|$)/g, '$1')
  const names = [...path.matchAll(/\/dev\/([^\s/;&|<>()]+)/g)].map(match => match[1] ?? '')
  if (/(^|[\s;&|(])\/dev\/?([\s;&|)]|$)/.test(path)) names.push(...path.split(/[\s;&|<>()]+/))
  return names.some(name => globMatches(name, 'clipboard')) ? CLIPBOARD : PASS
}

/** This machine in a URL (after normalizePath, so `http:/`), in the spellings curl accepts: 127.1, 2130706433, 0x7f000001, [::1]. */
const LOCAL_HOST =
  /(^|[\s=@(,;]|:\/)(127(\.\d+){0,3}|2130706433|0x7f[0-9a-f]{6}|0(\.0){0,3}|\[[0:]*:0*1\]|\[::ffff:127(\.\d+){3}\])([:/\s]|$)|\b127\.\d+\.\d+\.\d+|\blocalhost\b|\[::1\]/

/** -EncodedCommand (and its short forms before base64), FromBase64String and `[char]` arithmetic. */
function encoded(text: string): Verdict {
  const low = text.toLowerCase()
  if (/frombase64string|\[char\[\]\]|\[char\]\s*\(?\s*(0x)?\d/.test(low)) return HIDDEN
  const tokens = low.split(/\s+/).map(token => token.replace(/^['"]|['"]$/g, ''))
  for (const [i, token] of tokens.entries()) {
    if (!/^[-/]e[a-z]*$/.test(token)) continue
    const name = token.slice(1)
    if (name === 'ec') return HIDDEN
    if (!'encodedcommand'.startsWith(name)) continue
    if (name.length >= 6 || /^[a-z0-9+/]{16,}={0,2}$/.test(tokens[i + 1] ?? '')) return HIDDEN
  }
  return PASS
}

const B64 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'

/** -EncodedCommand's text: base64 of UTF-16LE. Undefined when it is not valid base64. */
function decodeCommand(text: string): string | undefined {
  const clean = text.trim().replace(/=+$/, '')
  if (clean.length < 2 || !/^[A-Za-z0-9+/]+$/.test(clean)) return undefined
  const bytes: number[] = []
  let buffer = 0
  let bits = 0
  for (const ch of clean) {
    buffer = ((buffer << 6) | B64.indexOf(ch)) & 0xffffff
    bits += 6
    if (bits >= 8) {
      bits -= 8
      bytes.push((buffer >> bits) & 0xff)
    }
  }
  let out = ''
  for (let i = 0; i + 1 < bytes.length; i += 2) out += String.fromCharCode((bytes[i] ?? 0) | ((bytes[i + 1] ?? 0) << 8))
  return out
}

// ---- Argument helpers for the table ----

/** Whether any argument is one of these (lowercase). */
const has = (c: Call, ...values: string[]): boolean => c.low.some(a => values.includes(a))
/** All the arguments as one lowercased line. */
const joined = (c: Call): string => c.low.join(' ')
/** A Windows switch, as /x or -x, optionally /x:value. */
const sw = (c: Call, ...names: string[]): boolean => c.low.some(a => /^[-/]/.test(a) && names.includes(a.slice(1).split(':')[0] ?? ''))
/** The arguments that are not options. */
const operands = (c: Call): string[] => c.low.filter(a => !a.startsWith('-'))
/** The first operand: a subcommand such as `winget install`'s install. */
const sub = (c: Call): string => operands(c)[0] ?? ''

/** pwsh: the index of a parameter given by any prefix of its name (`-Rec` for -Recurse, `-Name:value`). */
function paramIndex(c: Call, name: string): number {
  return c.low.findIndex(a => {
    const given = /^-([a-z][\w-]*)(:.*)?$/.exec(a)?.[1]
    return given !== undefined && name.startsWith(given)
  })
}
const param = (c: Call, name: string): boolean => paramIndex(c, name) >= 0
/** pwsh: a parameter's value, lowercased (`-Enabled False`, `-Enabled:$false`). */
function paramValue(c: Call, name: string): string | undefined {
  const i = paramIndex(c, name)
  const given = c.low[i]
  if (i < 0 || given === undefined) return undefined
  const colon = given.indexOf(':')
  return colon >= 0 ? given.slice(colon + 1) : c.low[i + 1]
}

/** Unix: whether a flag is given; short ones may be clustered (`-rf`), long ones may carry `=value`. */
function flagIn(texts: readonly string[], short: string | undefined, long?: string): boolean {
  return texts.some(a => {
    if (long !== undefined && (a === long || a.startsWith(`${long}=`))) return true
    return short !== undefined && /^-[A-Za-z0-9]+$/.test(a) && a.slice(1).includes(short)
  })
}
const flag = (c: Call, short: string | undefined, long?: string): boolean => flagIn(c.args.map(w => w.text), short, long)

/** Whether the arguments, or what is piped in, name one of these (service names, say). */
function names(c: Call, list: readonly string[]): boolean {
  if (c.low.some(a => a.split(/[\s,:=]/).some(part => list.includes(part.replace(/\.service$/, ''))))) return true
  return c.piped && list.some(item => new RegExp(`\\b${item}\\b`).test(c.text))
}

/** git's subcommand and the arguments after it, as written. */
function gitArgs(c: Call): { sub: string; rest: string[] } {
  let i = 0
  while (i < c.args.length) {
    const a = c.args[i]?.text ?? ''
    if (['-C', '-c', '--git-dir', '--work-tree', '--namespace', '--config-env'].includes(a)) i += 2
    else if (a.startsWith('-')) i++
    else break
  }
  return { sub: c.low[i] ?? '', rest: c.args.slice(i + 1).map(w => w.text) }
}

/** git settings that name a program to run (or a file of more settings). */
const GIT_RUNS_KEY =
  /^(alias\.|core\.(pager|editor|sshcommand|hookspath|fsmonitor|askpass|gitproxy)\b|credential\.|sequence\.editor|diff\.|difftool\.|merge\.|mergetool\.|filter\.|pager\.|gpg\.|uploadpack\.|receivepack\.|protocol\.|remote\..*\.(uploadpack|receivepack|vcs)|include\.|includeif\.|ssh\.)/

/** `git -c alias.x='!cmd'`, `-c core.pager=...` and the like: settings given on the command line that run a program. */
function gitRunsOption(c: Call): boolean {
  const risky = GIT_RUNS_KEY
  for (let i = 0; i < c.low.length; i++) {
    const a = c.low[i] ?? ''
    if (!a.startsWith('-')) return false // the subcommand: its own options are not settings
    if (a === '-c' || a === '--config-env') {
      if (c.args[i + 1]?.dynamic === true || risky.test(c.low[i + 1] ?? '')) return true
      i++
    } else if (a.startsWith('--config-env=') && risky.test(a.slice('--config-env='.length))) {
      return true
    } else if (['-C', '--git-dir', '--work-tree', '--namespace'].includes(c.args[i]?.text ?? '')) {
      i++
    }
  }
  return false
}

/** `git config` options that read or remove a setting, not set one. */
const GIT_CONFIG_READS = /^(--get(-all|-regexp|-urlmatch|-color|-colorbool)?|--list|-l|--unset(-all)?|--remove-section|--rename-section|--edit|-e|--show-origin|--show-scope|--name-only)$/
/** `git config` options that take a value. */
const GIT_CONFIG_VALUES = new Set(['-f', '--file', '--blob', '--type', '--default', '--comment', '--value'])

/**
 * `git config core.hooksPath x`, `git config alias.x '!cmd'`: a setting that runs a program later,
 * at a git command that looks harmless. An alias runs a program only when it starts with `!`.
 */
function gitConfigRuns(c: Call): boolean {
  const { sub, rest } = gitArgs(c)
  if (sub !== 'config') return false
  const words: string[] = []
  for (let i = 0; i < rest.length; i++) {
    const a = rest[i] ?? ''
    if (GIT_CONFIG_READS.test(a)) return false
    if (GIT_CONFIG_VALUES.has(a)) i++
    else if (!a.startsWith('-') || a === '-') words.push(a)
  }
  // git 2.46's `git config set key value` and `git config get key`.
  if (['get', 'list', 'unset', 'remove-section', 'rename-section', 'edit'].includes(words[0] ?? '')) return false
  if (words[0] === 'set') words.shift()
  const [key = '', value] = words
  if (value === undefined || !GIT_RUNS_KEY.test(key.toLowerCase())) return false
  return !key.toLowerCase().startsWith('alias.') || value.trimStart().startsWith('!') || value.includes('$')
}

function gitDiscards(c: Call): boolean {
  const { sub, rest } = gitArgs(c)
  switch (sub) {
    case 'reset':
      return rest.includes('--hard')
    case 'clean':
      return flagIn(rest, 'f', '--force')
    case 'checkout':
      return rest.includes('.') || flagIn(rest, 'f', '--force') || rest.indexOf('--') < rest.length - 1 && rest.includes('--')
    case 'restore':
      return !flagIn(rest, 'S', '--staged') || flagIn(rest, 'W', '--worktree')
    case 'stash':
      return rest[0] === 'drop' || rest[0] === 'clear'
    case 'switch':
      return flagIn(rest, 'f', '--force') || rest.includes('--discard-changes')
    default:
      return false
  }
}

function gitForcePush(c: Call): boolean {
  const { sub, rest } = gitArgs(c)
  if (sub !== 'push') return false
  return (
    flagIn(rest, 'f', '--force') ||
    flagIn(rest, 'd', '--delete') ||
    rest.some(a => a.startsWith('--force-with-lease') || a === '--force-if-includes' || a === '--mirror' || /^[+:]/.test(a))
  )
}

/**
 * git log, show, diff, format-patch and the like writing their output to a
 * file (`--output`, `--output-directory`, format-patch's `-o`, also
 * abbreviated): 'outside' for a path outside the project, in `.git` (whose
 * hooks run) or known only at run time; 'inside' for one in the project.
 */
function gitOutput(c: Call): 'inside' | 'outside' | undefined {
  const { sub } = gitArgs(c)
  if (!/^(log|show|diff|format-patch|whatchanged|range-diff|diff-tree|diff-index|diff-files)$/.test(sub)) return undefined
  const words = c.args.slice(c.low.indexOf(sub) + 1)
  let found: 'inside' | 'outside' | undefined
  for (const [i, word] of words.entries()) {
    const text = word.text
    const eq = text.indexOf('=')
    const name = eq < 0 ? text : text.slice(0, eq)
    const isLong = name.length >= 5 && ('--output'.startsWith(name) || (name.startsWith('--output-d') && '--output-directory'.startsWith(name)))
    const isShort = sub === 'format-patch' && /^-o/.test(text)
    if (!isLong && !isShort) continue
    // The path is glued on (`--output=x`, `-ox`) or the next word.
    const glued = isLong ? (eq < 0 ? '' : text.slice(eq + 1)) : text.slice(2)
    const target = glued !== '' ? word : words[i + 1]
    const path = glued !== '' ? glued : (target?.text ?? '')
    const isOutside = target === undefined || target.dynamic || /^([\\/~$%]|[A-Za-z]:)|(^|[\\/])(\.\.|\.git)([\\/]|$)/.test(path)
    found = isOutside ? 'outside' : (found ?? 'inside')
  }
  return found
}

function gitConfigGlobal(c: Call): boolean {
  const { sub, rest } = gitArgs(c)
  if (sub !== 'config' || !rest.some(a => a === '--global' || a === '--system')) return false
  const values = rest.filter(a => !a.startsWith('-'))
  return (
    values.length >= 2 ||
    /^(set|unset|edit|rename-section|remove-section)$/.test(values[0] ?? '') ||
    rest.some(a => /^--(unset|unset-all|add|replace-all|edit|rename-section|remove-section)$/.test(a) || a === '-e')
  )
}

/** `claude` options and subcommands that would loosen Claude Code's own permissions or plugins. */
function claudeLoosens(c: Call): boolean {
  const options = ['--dangerously-skip-permissions', '--allow-dangerously-skip-permissions', '--allowedtools', '--allowed-tools', '--settings', '--add-dir', '--permission-prompt-tool']
  if (c.low.some(a => options.includes(a.split('=')[0] ?? ''))) return true
  const mode = c.low.findIndex(a => a.split('=')[0] === '--permission-mode')
  if (mode >= 0) {
    const given = c.low[mode] ?? ''
    const value = given.includes('=') ? given.slice(given.indexOf('=') + 1) : c.low[mode + 1]
    if (value !== 'default' && value !== 'plan') return true
  }
  return claudePair(c, /^config$/, /^(set|add|remove)$/) ||
    claudePair(c, /^mcp$/, /^(add|add-json|add-from-claude-desktop|remove|reset-project-choices)$/) ||
    claudePair(c, /^plugins?$/, /^(uninstall|disable|remove|rm)$/)
}

/** Two operands in a row, such as `plugin uninstall`, anywhere after the options. */
function claudePair(c: Call, first: RegExp, second: RegExp): boolean {
  const words = operands(c)
  return words.some((word, i) => first.test(word) && second.test(words[i + 1] ?? ''))
}

/** gh commands that post, change or delete something on GitHub. */
const GH_WRITES: Record<string, RegExp> = {
  pr: /^(create|merge|close|reopen|comment|review|edit|ready|lock|unlock)$/,
  issue: /^(create|comment|close|reopen|edit|delete|transfer|lock|unlock|pin|unpin|develop)$/,
  release: /^(create|delete|edit|upload|delete-asset)$/,
  repo: /^(create|delete|fork|edit|rename|archive|unarchive|sync|deploy-key)$/,
  gist: /^(create|edit|delete|rename)$/,
  secret: /^(set|delete)$/,
  variable: /^(set|delete)$/,
  workflow: /^(run|enable|disable)$/,
  run: /^(rerun|cancel|delete)$/,
  label: /^(create|edit|delete|clone)$/,
  cache: /^delete$/,
  'ssh-key': /^(add|delete)$/,
  'gpg-key': /^(add|delete)$/,
}

function ghWrites(c: Call): boolean {
  const [group = '', action = ''] = operands(c)
  if (group !== 'api') return GH_WRITES[group]?.test(action) ?? false
  const method = c.low.findIndex(a => a === '-x' || a === '--method')
  const value = method >= 0 ? c.low[method + 1] : c.low.find(a => /^(-x|--method=)/.test(a))?.replace(/^(-x|--method=)/, '')
  if (value !== undefined && value !== '') return value !== 'get'
  return c.args.some(w => /^(-f|-F|--field|--raw-field|--input)(=|$)/.test(w.text))
}

/** curl options that send data: -d, --data*, -F, --form*, -T, --upload-file, --json, or a -X method other than GET. */
function curlSends(c: Call): boolean {
  for (const [i, word] of c.args.entries()) {
    const a = word.text
    if (a.startsWith('--')) {
      const name = a.split('=')[0] ?? ''
      if (/^--(data(-\w+)?|form(-string)?|upload-file|json)$/.test(name)) return true
      if (name === '--request') {
        const method = a.includes('=') ? a.slice(a.indexOf('=') + 1) : c.args[i + 1]?.text ?? ''
        if (!/^(get|head|options)$/i.test(method)) return true
      }
      continue
    }
    if (!/^-[A-Za-z]/.test(a)) continue
    for (let j = 1; j < a.length; j++) {
      const option = a[j] ?? ''
      if ('dFT'.includes(option)) return true
      if (option === 'X') {
        const method = a.slice(j + 1) || (c.args[i + 1]?.text ?? '')
        if (!/^(get|head|options)$/i.test(method)) return true
        break
      }
      // The rest of the word is this option's value (-o file, -H header, -u user).
      if ('AbcCDeEHKmortuUwxyYz'.includes(option)) break
    }
  }
  return false
}

/** scp and rsync: whether the destination (the last operand) is another machine. */
function sendsAway(c: Call): boolean {
  const last = c.args.filter(w => !w.text.startsWith('-')).at(-1)?.text ?? ''
  return /^(?:[\w.-]+@)?[\w.-]{2,}:(?!\/\/)|^\w+:\/\//.test(last)
}

/** A move counts as many with a wildcard, -Recurse, input from a pipe, or inside a loop. */
const many = (c: Call): boolean =>
  c.piped || c.loop || (c.dialect === 'pwsh' && param(c, 'recurse')) || c.args.some(w => !w.text.startsWith('-') && /[*?]/.test(w.text))

/** A registry path: HKLM:\, HKCU:\, Registry::..., or piped from one. */
const REGISTRY = /(^|[\s:'"=])(hklm|hkcu|hkcr|hku|hkcc):|registry::|^hkey_/
const registryPath = (c: Call): boolean => c.low.some(a => REGISTRY.test(a)) || (c.piped && REGISTRY.test(c.text))

/** rundll32's first argument without its folder: `user32.dll,lockworkstation`. */
const dllEntry = (c: Call): string => baseName(c.low[0] ?? '')

const SC_CHANGES = new Set(['config', 'create', 'delete', 'stop', 'start', 'pause', 'continue', 'failure', 'failureflag', 'sidtype',
  'privs', 'managedaccount', 'control', 'description', 'sdset', 'triggerinfo', 'preferrednode'])
const SC_STOPS = new Set(['config', 'delete', 'stop', 'pause', 'failure', 'failureflag', 'sdset', 'privs', 'sidtype', 'triggerinfo'])
/** sc.exe's subcommand, past an optional `\\server`. */
const scSub = (c: Call): string => c.low.find(a => !a.startsWith('\\\\')) ?? ''
const SECURITY_SERVICES = ['windefend', 'wdnissvc', 'sense', 'wdboot', 'wdfilter', 'securityhealthservice', 'wscsvc', 'mpssvc', 'bfe']

/** `net user|localgroup|group` that adds, removes, enables or sets a password. */
function netAccounts(c: Call): boolean {
  const [group = '', ...rest] = c.low.filter(a => !a.startsWith('/'))
  const switches = c.low.filter(a => a.startsWith('/') && a !== '/domain')
  if (group === 'user' || group === 'users') return switches.length > 0 || rest.length >= 2
  if (group === 'localgroup' || group === 'group') return rest.length >= 2 || switches.some(a => /^\/(add|delete|del)$/.test(a))
  return false
}

/** fdisk and friends when they only list or print. */
const partitionReads = (c: Call): boolean =>
  has(c, '-l', '--list', '-p', '--print', '-v', '--version', '-h', '--help', '-d', '--dump') || (c.name === 'parted' && has(c, 'print'))

// ---- The tier table ----

type Rules = { powershell?: readonly string[]; bash?: readonly string[] }

/**
 * One row of the tier table: the programs or cmdlets it looks at, the
 * dialects it applies in (all when left out), an argument test (every call
 * when left out), and, for never rows, the deny rules /jarvis pc rules
 * prints for it. A command no row names passes.
 */
type Row = {
  id: string
  tier: Tier
  reason: string
  names: readonly string[]
  in?: readonly Dialect[]
  when?: (c: Call) => boolean
  rules?: Rules
}

const PWSH: readonly Dialect[] = ['pwsh']
const ps = (...rules: string[]): Rules => ({ powershell: rules })
const both = (...rules: string[]): Rules => ({ powershell: rules, bash: rules })

const DEFENDER_OFF = 'turns off Windows Defender'
const FIREWALL_OFF = 'turns off the firewall'
const DISK = 'erases a disk'
const BOOT = 'changes how the PC boots'
const RESTORE = 'deletes restore points or logs'
const ACCOUNTS = 'changes user accounts'
const KEYSTROKES = 'types keystrokes into a window'
const DELETE = 'deletes files'
const SERVICE = 'changes a service'
const TASKS = 'changes scheduled tasks'
const REGISTRY_EDIT = 'edits the registry'
const EMAIL = 'sends email or messages'
const INSTALL = 'installs or removes software'
const PUBLISH = 'publishes a package'
const SETTING = 'changes a setting'
const FIREWALL = 'changes the firewall'
const PERMISSIONS = 'changes file permissions'
const SHUTDOWN = 'shuts down or restarts the PC'

const LOCAL_ACCOUNTS = ['New-LocalUser', 'Set-LocalUser', 'Enable-LocalUser', 'Disable-LocalUser', 'Remove-LocalUser', 'Rename-LocalUser',
  'Add-LocalGroupMember', 'Remove-LocalGroupMember', 'New-LocalGroup', 'Remove-LocalGroup', 'Rename-LocalGroup', 'Set-LocalGroup']
const REGISTRY_CMDLETS = ['set-itemproperty', 'new-itemproperty', 'remove-itemproperty', 'rename-itemproperty', 'clear-itemproperty',
  'copy-itemproperty', 'move-itemproperty', 'new-item', 'set-item', 'remove-item', 'rename-item', 'move-item', 'copy-item', 'clear-item']
const lower = (list: readonly string[]): string[] => list.map(name => name.toLowerCase())

const TIER_TABLE: readonly Row[] = [
  // ---- never: denied outright ----
  {
    id: 'defender-off', tier: 'never', reason: DEFENDER_OFF, names: ['set-mppreference'], in: PWSH,
    when: c => c.low.some(a => /^-(dis|exc)/.test(a)) || c.low.some((a, i) => /^-\w*defaultaction/.test(a) && /^(allow|6|9|noaction)$/.test(c.low[i + 1] ?? '')),
    rules: ps('Set-MpPreference *'),
  },
  { id: 'defender-off', tier: 'never', reason: DEFENDER_OFF, names: ['add-mppreference'], in: PWSH, when: c => c.low.some(a => a.startsWith('-exc')), rules: ps('Add-MpPreference *') },
  {
    id: 'defender-off', tier: 'never', reason: DEFENDER_OFF, names: ['stop-service', 'set-service', 'suspend-service', 'remove-service'], in: PWSH,
    when: c => names(c, SECURITY_SERVICES),
    rules: ps('Stop-Service WinDefend *', 'Set-Service WinDefend *', 'Stop-Service mpssvc *', 'Set-Service mpssvc *'),
  },
  {
    id: 'defender-off', tier: 'never', reason: DEFENDER_OFF, names: ['sc', 'net', 'net1'],
    when: c => (c.name === 'sc' ? SC_STOPS.has(scSub(c)) : /^(stop|pause)$/.test(sub(c))) && names(c, SECURITY_SERVICES),
    rules: both('sc.exe stop WinDefend *', 'sc.exe config WinDefend *', 'sc.exe delete WinDefend *', 'net stop WinDefend *', 'sc.exe stop mpssvc *', 'sc.exe config mpssvc *', 'net stop mpssvc *'),
  },
  {
    id: 'defender-off', tier: 'never', reason: DEFENDER_OFF, names: ['reg'],
    when: c => /^(add|delete|import|restore|load|copy)$/.test(sub(c)) && joined(c).includes('windows defender'),
    rules: both('reg add HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows Defender *', 'reg add "HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows Defender" *'),
  },
  {
    id: 'defender-off', tier: 'never', reason: DEFENDER_OFF, names: ['set-itemproperty', 'new-itemproperty', 'new-item', 'set-item'], in: PWSH,
    when: c => registryPath(c) && (joined(c).includes('windows defender') || (c.piped && c.text.includes('windows defender'))),
    rules: ps('Set-ItemProperty HKLM:\\SOFTWARE\\Policies\\Microsoft\\Windows Defender *', 'New-ItemProperty HKLM:\\SOFTWARE\\Policies\\Microsoft\\Windows Defender *'),
  },
  {
    id: 'defender-off', tier: 'never', reason: DEFENDER_OFF, names: ['uninstall-windowsfeature', 'remove-windowsfeature', 'disable-windowsoptionalfeature'], in: PWSH,
    when: c => joined(c).includes('defender'),
    rules: ps('Uninstall-WindowsFeature *Defender*', 'Remove-WindowsFeature *Defender*', 'Disable-WindowsOptionalFeature *Defender*'),
  },
  {
    id: 'defender-off', tier: 'never', reason: DEFENDER_OFF, names: ['dism'], when: c => sw(c, 'disable-feature', 'remove-capability') && joined(c).includes('defender'),
    rules: both('dism /online /disable-feature /featurename:Windows-Defender *'),
  },
  { id: 'protection-off', tier: 'never', reason: 'turns off system protection', names: ['spctl'], when: c => has(c, '--master-disable', '--global-disable'), rules: both('spctl --master-disable *', 'spctl --global-disable *') },
  { id: 'protection-off', tier: 'never', reason: 'turns off system protection', names: ['csrutil'], when: c => has(c, 'disable'), rules: both('csrutil disable *', 'csrutil authenticated-root disable *') },
  { id: 'protection-off', tier: 'never', reason: 'turns off system protection', names: ['setenforce'], when: c => has(c, '0', 'permissive'), rules: both('setenforce 0 *', 'setenforce permissive *') },
  {
    id: 'firewall-off', tier: 'never', reason: FIREWALL_OFF, names: ['set-netfirewallprofile'], in: PWSH,
    when: c => /^(\$?false|0)$/.test(paramValue(c, 'enabled') ?? '') || paramValue(c, 'defaultinboundaction') === 'allow',
    rules: ps('Set-NetFirewallProfile *'),
  },
  {
    id: 'firewall-off', tier: 'never', reason: FIREWALL_OFF, names: ['netsh'],
    when: c => (has(c, 'advfirewall') && has(c, 'state') && has(c, 'off')) || (has(c, 'firewall') && !has(c, 'advfirewall') && has(c, 'disable', 'mode=disable', 'opmode=disable')),
    rules: both('netsh advfirewall set *', 'netsh firewall set opmode *'),
  },
  { id: 'firewall-off', tier: 'never', reason: FIREWALL_OFF, names: ['ufw'], when: c => sub(c) === 'disable', rules: both('ufw disable *') },
  {
    id: 'firewall-off', tier: 'never', reason: FIREWALL_OFF, names: ['systemctl'],
    when: c => /^(stop|disable|mask|kill)$/.test(sub(c)) && names(c, ['firewalld', 'ufw', 'nftables', 'iptables']),
    rules: both('systemctl stop firewalld *', 'systemctl disable firewalld *', 'systemctl mask firewalld *', 'systemctl stop ufw *', 'systemctl disable ufw *', 'systemctl mask ufw *'),
  },
  {
    id: 'firewall-off', tier: 'never', reason: FIREWALL_OFF, names: ['socketfilterfw'], when: c => has(c, '--setglobalstate') && has(c, 'off'),
    rules: both('/usr/libexec/ApplicationFirewall/socketfilterfw --setglobalstate off *'),
  },
  {
    id: 'disk-wipe', tier: 'never', reason: DISK, names: ['format-volume', 'clear-disk', 'initialize-disk', 'remove-partition'], in: PWSH,
    rules: ps('Format-Volume *', 'Clear-Disk *', 'Initialize-Disk *', 'Remove-Partition *'),
  },
  {
    id: 'disk-wipe', tier: 'never', reason: DISK, names: ['format', 'diskpart', 'wipefs', 'blkdiscard', 'mkfs', 'mke2fs', 'mkntfs'],
    rules: both('format *', 'diskpart *', 'wipefs *', 'blkdiscard *', 'mkfs *', 'mkfs.ext4 *', 'mkfs.vfat *', 'mkfs.ntfs *', 'mke2fs *', 'mkntfs *'),
  },
  { id: 'disk-wipe', tier: 'never', reason: DISK, names: ['cipher'], when: c => c.low.some(a => /^\/w(:|$)/.test(a)), rules: both('cipher *') },
  { id: 'disk-wipe', tier: 'never', reason: DISK, names: ['dd'], when: c => c.low.some(a => /^of=(\/dev\/|\\\\\.\\)/.test(a)), rules: both('dd *') },
  { id: 'disk-wipe', tier: 'never', reason: DISK, names: ['shred'], when: c => c.low.some(a => a.startsWith('/dev/')), rules: both('shred /dev/*') },
  {
    id: 'disk-wipe', tier: 'never', reason: DISK, names: ['diskutil'],
    when: c => /^(erasedisk|erasevolume|zerodisk|randomdisk|secureerase|partitiondisk|reformat)$/.test(sub(c)) || (sub(c) === 'apfs' && /^(deletecontainer|deletevolume|erasevolume)$/.test(operands(c)[1] ?? '')),
    rules: both('diskutil eraseDisk *', 'diskutil eraseVolume *', 'diskutil zeroDisk *', 'diskutil randomDisk *', 'diskutil secureErase *', 'diskutil partitionDisk *', 'diskutil reformat *', 'diskutil apfs deleteContainer *'),
  },
  {
    id: 'disk-wipe', tier: 'never', reason: DISK, names: ['fdisk', 'sfdisk', 'gdisk', 'cfdisk', 'sgdisk', 'parted'], when: c => !partitionReads(c),
    rules: both('fdisk *', 'sfdisk *', 'gdisk *', 'cfdisk *', 'sgdisk *', 'parted *'),
  },
  { id: 'boot', tier: 'never', reason: BOOT, names: ['bcdedit'], when: c => c.low.length > 0 && !/^[-/](enum|v|\?)$/.test(c.low[0] ?? ''), rules: both('bcdedit *') },
  { id: 'boot', tier: 'never', reason: BOOT, names: ['bcdboot', 'bootrec', 'bootsect', 'grub-install', 'grub2-install'], rules: both('bcdboot *', 'bootrec *', 'bootsect *', 'grub-install *', 'grub2-install *') },
  {
    id: 'boot', tier: 'never', reason: BOOT, names: ['efibootmgr'], when: c => c.low.some(a => !['-v', '-vv', '--verbose'].includes(a)),
    rules: both('efibootmgr -B *', 'efibootmgr -b *', 'efibootmgr -c *', 'efibootmgr -o *', 'efibootmgr -n *', 'efibootmgr -a *', 'efibootmgr -A *'),
  },
  { id: 'restore-logs', tier: 'never', reason: RESTORE, names: ['vssadmin'], when: c => /^(delete|resize)$/.test(sub(c)), rules: both('vssadmin delete *', 'vssadmin resize *') },
  { id: 'restore-logs', tier: 'never', reason: RESTORE, names: ['wmic'], when: c => joined(c).includes('shadowcopy') && has(c, 'delete'), rules: both('wmic shadowcopy delete *') },
  { id: 'restore-logs', tier: 'never', reason: RESTORE, names: ['remove-ciminstance', 'remove-wmiobject'], in: PWSH, when: c => c.text.includes('win32_shadowcopy'), rules: ps('Remove-CimInstance *', 'Remove-WmiObject *') },
  { id: 'restore-logs', tier: 'never', reason: RESTORE, names: ['disable-computerrestore', 'clear-eventlog', 'remove-eventlog'], in: PWSH, rules: ps('Disable-ComputerRestore *', 'Clear-EventLog *', 'Remove-EventLog *') },
  {
    id: 'restore-logs', tier: 'never', reason: RESTORE, names: ['wevtutil'],
    when: c => /^(cl|clear-log|um|uninstall-manifest)$/.test(sub(c)) || (/^(sl|set-log)$/.test(sub(c)) && c.low.some(a => /^\/e(nabled)?:false$/.test(a))),
    rules: both('wevtutil cl *', 'wevtutil clear-log *', 'wevtutil um *', 'wevtutil uninstall-manifest *'),
  },
  { id: 'restore-logs', tier: 'never', reason: RESTORE, names: ['log'], when: c => sub(c) === 'erase', rules: both('log erase *') },
  {
    id: 'restore-logs', tier: 'never', reason: RESTORE, names: ['tmutil'], when: c => /^(delete|deletelocalsnapshots|disable|thinlocalsnapshots|deleteinprogress)$/.test(sub(c)),
    rules: both('tmutil delete *', 'tmutil deletelocalsnapshots *', 'tmutil disable *', 'tmutil thinlocalsnapshots *'),
  },
  {
    id: 'restore-logs', tier: 'never', reason: RESTORE, names: ['journalctl'], when: c => c.low.some(a => a.startsWith('--vacuum')),
    rules: both('journalctl --vacuum-time *', 'journalctl --vacuum-size *', 'journalctl --vacuum-files *'),
  },
  { id: 'accounts', tier: 'never', reason: ACCOUNTS, names: lower(LOCAL_ACCOUNTS), in: PWSH, rules: ps(...LOCAL_ACCOUNTS.map(name => `${name} *`)) },
  {
    id: 'accounts', tier: 'never', reason: ACCOUNTS, names: ['net', 'net1'], when: netAccounts,
    rules: both('net user * /add *', 'net user * /active:yes *', 'net user * /delete *', 'net localgroup * /add *', 'net localgroup * /delete *', 'net group * /add *'),
  },
  {
    id: 'accounts', tier: 'never', reason: ACCOUNTS, names: ['useradd', 'adduser', 'userdel', 'deluser', 'chpasswd', 'newusers'],
    rules: both('useradd *', 'adduser *', 'userdel *', 'deluser *', 'chpasswd *', 'newusers *'),
  },
  {
    id: 'accounts', tier: 'never', reason: ACCOUNTS, names: ['usermod', 'gpasswd'], when: c => names(c, ['sudo', 'wheel', 'admin', 'adm', 'root']),
    rules: both('usermod -aG sudo *', 'usermod -aG wheel *', 'usermod -aG admin *', 'gpasswd -a *'),
  },
  {
    id: 'accounts', tier: 'never', reason: ACCOUNTS, names: ['dscl'], when: c => c.low.some(a => /^-(create|append|passwd|delete|merge|change)$/.test(a)),
    rules: both('dscl . -create *', 'dscl . -append *', 'dscl . -passwd *', 'dscl . -delete *'),
  },
  {
    id: 'accounts', tier: 'never', reason: ACCOUNTS, names: ['sysadminctl'], when: c => c.low.some(a => /^-(adduser|deleteuser|resetpasswordfor|securetokenon|securetokenoff)$/.test(a)),
    rules: both('sysadminctl -addUser *', 'sysadminctl -deleteUser *', 'sysadminctl -resetPasswordFor *'),
  },
  { id: 'accounts', tier: 'never', reason: ACCOUNTS, names: ['dseditgroup'], when: c => has(c, 'edit'), rules: both('dseditgroup -o edit *') },
  {
    id: 'keystrokes', tier: 'never', reason: KEYSTROKES, names: ['xdotool', 'ydotool'], when: c => /^(type|key|keydown|keyup|click|mousedown|mouseup)$/.test(sub(c)),
    rules: both('xdotool type *', 'xdotool key *', 'xdotool keydown *', 'xdotool keyup *', 'xdotool click *', 'ydotool type *', 'ydotool key *', 'ydotool click *'),
  },
  { id: 'keystrokes', tier: 'never', reason: KEYSTROKES, names: ['wtype'], rules: both('wtype *') },
  { id: 'keystrokes', tier: 'never', reason: KEYSTROKES, names: ['osascript'], when: c => /keystroke|key code/.test(joined(c)), rules: both('osascript *keystroke*', 'osascript *key code*') },
  {
    id: 'claude-permissions', tier: 'never', reason: CLAUDE_OWN, names: ['claude'], when: claudeLoosens,
    rules: both('claude --dangerously-skip-permissions *', 'claude --allow-dangerously-skip-permissions *', 'claude --permission-mode *', 'claude --allowedTools *',
      'claude --allowed-tools *', 'claude --settings *', 'claude --add-dir *', 'claude --permission-prompt-tool *', 'claude config set *', 'claude config add *',
      'claude config remove *', 'claude mcp add *', 'claude mcp add-json *', 'claude mcp add-from-claude-desktop *', 'claude mcp remove *',
      'claude plugin uninstall *', 'claude plugin disable *', 'claude plugin remove *'),
  },

  // ---- screen: a click or a typed yes ----
  { id: 'claude-plugins', tier: 'screen', reason: "changes Claude Code's plugins", names: ['claude'], when: c => claudePair(c, /^plugins?$/, /^(install|i|update|enable|marketplace)$/) },
  { id: 'defender-change', tier: 'screen', reason: 'changes Windows Defender', names: ['set-mppreference', 'add-mppreference', 'remove-mppreference'], in: PWSH },
  { id: 'registry', tier: 'screen', reason: REGISTRY_EDIT, names: ['reg'], when: c => /^(add|delete|import|load|unload|restore|copy|save)$/.test(sub(c)) },
  { id: 'registry', tier: 'screen', reason: REGISTRY_EDIT, names: ['regedit', 'regini'], when: c => c.low.length > 0 },
  { id: 'registry', tier: 'screen', reason: REGISTRY_EDIT, names: REGISTRY_CMDLETS, in: PWSH, when: registryPath },
  { id: 'delete', tier: 'screen', reason: DELETE, names: ['remove-item', 'clear-content', 'clear-recyclebin', 'clear-item'], in: PWSH },
  { id: 'delete', tier: 'screen', reason: DELETE, names: ['rm', 'unlink', 'rmdir', 'rd', 'del', 'erase', 'shred', 'truncate', 'srm'] },
  { id: 'delete', tier: 'screen', reason: DELETE, names: ['robocopy'], when: c => sw(c, 'mir', 'purge', 'mov', 'move') },
  { id: 'delete', tier: 'screen', reason: DELETE, names: ['find'], when: c => has(c, '-delete') },
  { id: 'delete', tier: 'screen', reason: DELETE, names: ['rsync'], when: c => c.low.some(a => a.startsWith('--delete') || a === '--remove-source-files') },
  { id: 'delete', tier: 'screen', reason: 'deletes a Linux system', names: ['wsl'], when: c => has(c, '--unregister') },
  { id: 'delete', tier: 'screen', reason: 'deletes a GitHub repository', names: ['gh'], when: c => sub(c) === 'repo' && operands(c)[1] === 'delete' },
  { id: 'git-discard', tier: 'screen', reason: 'discards uncommitted work', names: ['git'], when: gitDiscards },
  { id: 'git-force', tier: 'screen', reason: 'overwrites history on the remote', names: ['git'], when: gitForcePush },
  { id: 'git-rewrite', tier: 'screen', reason: 'rewrites history', names: ['git'], when: c => /^filter-(branch|repo)$/.test(gitArgs(c).sub) },
  {
    id: 'git-branch', tier: 'screen', reason: 'deletes a branch', names: ['git'],
    when: c => gitArgs(c).sub === 'branch' && (gitArgs(c).rest.includes('-D') || (flagIn(gitArgs(c).rest, 'd', '--delete') && flagIn(gitArgs(c).rest, 'f', '--force'))),
  },
  { id: 'built', tier: 'screen', reason: 'runs a command set in its options', names: ['git'], when: gitRunsOption },
  { id: 'autorun', tier: 'screen', reason: 'sets a git setting that runs a program', names: ['git'], when: gitConfigRuns },
  { id: 'git-output', tier: 'screen', reason: 'writes a file outside the project', names: ['git'], when: c => gitOutput(c) === 'outside' },
  { id: 'hidden', tier: 'screen', reason: HIDDEN.reason, names: ['invoke-cimmethod', 'invoke-wmimethod'], in: PWSH, when: c => /win32_process\b/.test(joined(c)) },
  { id: 'shutdown', tier: 'screen', reason: SHUTDOWN, names: ['stop-computer', 'restart-computer'], in: PWSH },
  {
    id: 'shutdown', tier: 'screen', reason: SHUTDOWN, names: ['shutdown'],
    when: c => !sw(c, 'a', 'c', '?') && !(c.low.includes('/h') && !sw(c, 's', 'r', 'p', 'l', 'g', 'sg', 'fw')),
  },
  { id: 'shutdown', tier: 'screen', reason: SHUTDOWN, names: ['reboot', 'poweroff', 'halt'] },
  { id: 'shutdown', tier: 'screen', reason: SHUTDOWN, names: ['init', 'telinit'], when: c => has(c, '0', '6') },
  { id: 'shutdown', tier: 'screen', reason: SHUTDOWN, names: ['systemctl'], when: c => /^(poweroff|reboot|halt|kexec|soft-reboot|emergency|rescue)$/.test(sub(c)) },
  { id: 'shutdown', tier: 'screen', reason: 'signs you out of Windows', names: ['logoff'] },
  {
    id: 'service', tier: 'screen', reason: SERVICE, in: PWSH,
    names: ['stop-service', 'start-service', 'restart-service', 'set-service', 'new-service', 'remove-service', 'suspend-service', 'resume-service'],
  },
  { id: 'service', tier: 'screen', reason: SERVICE, names: ['sc'], when: c => SC_CHANGES.has(scSub(c)) },
  { id: 'service', tier: 'screen', reason: SERVICE, names: ['net', 'net1'], when: c => /^(stop|pause|continue)$/.test(sub(c)) || (sub(c) === 'start' && operands(c).length > 1) },
  {
    id: 'service', tier: 'screen', reason: SERVICE, names: ['systemctl'],
    when: c => /^(stop|start|restart|reload|try-restart|reload-or-restart|enable|disable|mask|unmask|kill|isolate|set-default|edit|preset|link|revert)$/.test(sub(c)),
  },
  { id: 'service', tier: 'screen', reason: SERVICE, names: ['service'], when: c => /^(start|stop|restart|reload|force-reload)$/.test(operands(c)[1] ?? '') },
  { id: 'service', tier: 'screen', reason: SERVICE, names: ['launchctl'], when: c => /^(load|unload|bootstrap|bootout|enable|disable|kill|kickstart|remove|submit|stop|start)$/.test(sub(c)) },
  {
    id: 'tasks', tier: 'screen', reason: TASKS, in: PWSH,
    names: ['register-scheduledtask', 'unregister-scheduledtask', 'set-scheduledtask', 'enable-scheduledtask', 'disable-scheduledtask', 'start-scheduledtask',
      'stop-scheduledtask', 'register-scheduledjob', 'unregister-scheduledjob'],
  },
  { id: 'tasks', tier: 'screen', reason: TASKS, names: ['schtasks'], when: c => sw(c, 'create', 'delete', 'change', 'run', 'end') },
  { id: 'tasks', tier: 'screen', reason: TASKS, names: ['crontab'], when: c => !(c.low.length > 0 && c.low.every((a, i) => a === '-l' || a === '-u' || c.low[i - 1] === '-u')) },
  { id: 'tasks', tier: 'screen', reason: TASKS, names: ['at', 'atrm', 'batch'] },
  { id: 'email', tier: 'screen', reason: EMAIL, names: ['send-mailmessage'], in: PWSH },
  { id: 'email', tier: 'screen', reason: EMAIL, names: ['mail', 'mailx', 'sendmail', 'msmtp', 'mutt', 'neomutt', 's-nail'] },
  { id: 'email', tier: 'screen', reason: EMAIL, names: ['curl'], when: c => c.low.some(a => a.startsWith('--mail-rcpt') || /^smtps?:\/\//.test(a)) },
  { id: 'email', tier: 'screen', reason: EMAIL, names: ['osascript'], when: c => /\b(mail|messages)\b/.test(joined(c)) && /\bsend\b/.test(joined(c)) },
  { id: 'download-run', tier: 'screen', reason: 'downloads or decodes a file', names: ['certutil'], when: c => sw(c, 'urlcache', 'decode', 'decodehex', 'verifyctl') },
  { id: 'download-run', tier: 'screen', reason: 'downloads a file', names: ['bitsadmin'], when: c => sw(c, 'transfer', 'addfile', 'create', 'setnotifycmdline', 'resume') },
  { id: 'download-run', tier: 'screen', reason: 'downloads a file', names: ['start-bitstransfer'], in: PWSH },
  { id: 'script-host', tier: 'screen', reason: 'runs a script or library', names: ['mshta', 'regsvr32', 'wscript', 'cscript', 'installutil', 'regasm', 'regsvcs', 'cmstp', 'msxsl', 'odbcconf'] },
  { id: 'script-host', tier: 'screen', reason: 'runs a script or library', names: ['rundll32'], when: c => !/^(user32\.dll,lockworkstation|powrprof\.dll,setsuspendstate)/.test(dllEntry(c)) },
  { id: 'installer', tier: 'screen', reason: INSTALL, names: ['msiexec'], when: c => c.low.length > 0 && !has(c, '/?') },
  {
    id: 'add-type', tier: 'screen', reason: 'compiles and runs code', names: ['add-type'], in: PWSH,
    when: c => param(c, 'typedefinition') || param(c, 'memberdefinition') || param(c, 'path') || param(c, 'literalpath') || (c.low[0] !== undefined && !c.low[0].startsWith('-')),
  },
  { id: 'security', tier: 'screen', reason: 'changes security settings', names: ['set-executionpolicy'], in: PWSH, when: c => has(c, 'bypass', 'unrestricted') },
  { id: 'security', tier: 'screen', reason: 'changes a system setting', names: ['setx'], when: c => sw(c, 'm') },
  { id: 'permissions', tier: 'screen', reason: PERMISSIONS, names: ['set-acl'], in: PWSH },
  { id: 'permissions', tier: 'screen', reason: PERMISSIONS, names: ['icacls'], when: c => c.low.some(a => /^\/(grant|deny|remove|setowner|reset|inheritance|restore|setintegritylevel)/.test(a)) },
  { id: 'permissions', tier: 'screen', reason: PERMISSIONS, names: ['takeown'] },
  { id: 'permissions', tier: 'screen', reason: PERMISSIONS, names: ['cacls'], when: c => c.low.some(a => a.startsWith('/')) },
  {
    id: 'firewall', tier: 'screen', reason: FIREWALL, in: PWSH,
    names: ['new-netfirewallrule', 'set-netfirewallrule', 'remove-netfirewallrule', 'enable-netfirewallrule', 'disable-netfirewallrule', 'copy-netfirewallrule',
      'rename-netfirewallrule', 'set-netfirewallprofile'],
  },
  { id: 'firewall', tier: 'screen', reason: FIREWALL, names: ['netsh'], when: c => has(c, 'advfirewall', 'firewall') && has(c, 'add', 'delete', 'set', 'reset', 'import') },
  { id: 'firewall', tier: 'screen', reason: FIREWALL, names: ['ufw'], when: c => !/^(status|version|show|app)?$/.test(sub(c)) },
  {
    id: 'firewall', tier: 'screen', reason: FIREWALL, names: ['iptables', 'ip6tables', 'nft'],
    when: c => !(c.args.some(w => /^-[a-z]*[LS][a-z]*$/.test(w.text) || w.text.startsWith('--list')) || sub(c) === 'list'),
  },
  { id: 'encryption', tier: 'screen', reason: 'turns off disk encryption', names: ['manage-bde'], when: c => sw(c, 'off', 'disable', 'pause', 'unlock') || (sw(c, 'protectors') && sw(c, 'delete', 'disable')) },
  { id: 'encryption', tier: 'screen', reason: 'turns off disk encryption', names: ['disable-bitlocker', 'suspend-bitlocker'], in: PWSH },
  { id: 'accounts', tier: 'screen', reason: ACCOUNTS, names: ['usermod', 'passwd', 'groupadd', 'groupdel', 'groupmod'] },
  { id: 'alias', tier: 'screen', reason: 'redefines a command name', names: ['set-alias', 'new-alias'], in: PWSH },
  {
    id: 'jarvis-helper', tier: 'screen', reason: "runs Jarvis's own helper", names: ['python', 'py'],
    when: c => c.low.some((a, i) => (a === '-m' && /^jarvis_(voice|hands)\b/.test(c.low[i + 1] ?? '')) || /^-mjarvis_(voice|hands)\b/.test(a)),
  },
  { id: 'jarvis-helper', tier: 'screen', reason: "runs Jarvis's own helper", names: ['jarvis-voice', 'jarvis_voice', 'jarvis-hands', 'jarvis_hands'] },
  { id: 'jarvis-helper', tier: 'screen', reason: "runs Jarvis's own helper", names: ['uv', 'uvx', 'pipx'], when: c => c.low.some(a => /^jarvis[-_](voice|hands)$/.test(a)) },

  // ---- voice: a spoken yes in a voice turn, otherwise a click ----
  { id: 'move-many', tier: 'voice', reason: 'moves or renames many files', names: ['move-item', 'rename-item', 'mv', 'move', 'ren', 'rename'], when: many },
  { id: 'install', tier: 'voice', reason: INSTALL, names: ['winget'], when: c => /^(install|add|upgrade|update|uninstall|remove|rm|import|configure|repair|pin|source|settings)$/.test(sub(c)) },
  { id: 'install', tier: 'voice', reason: INSTALL, names: ['choco'], when: c => /^(install|upgrade|uninstall|update)$/.test(sub(c)) },
  { id: 'install', tier: 'voice', reason: INSTALL, names: ['cinst', 'cup', 'cuninst'] },
  { id: 'install', tier: 'voice', reason: INSTALL, names: ['scoop'], when: c => /^(install|uninstall|update|reset|bucket)$/.test(sub(c)) },
  {
    id: 'install', tier: 'voice', reason: INSTALL, in: PWSH,
    names: ['install-module', 'install-package', 'install-script', 'uninstall-module', 'uninstall-package', 'uninstall-script', 'update-module', 'update-script',
      'install-psresource', 'update-psresource', 'uninstall-psresource', 'add-appxpackage', 'remove-appxpackage', 'install-windowsfeature',
      'enable-windowsoptionalfeature', 'disable-windowsoptionalfeature'],
  },
  {
    id: 'install', tier: 'voice', reason: INSTALL, names: ['npm', 'pnpm', 'bun'],
    when: c => /^(install|i|add|uninstall|remove|rm|un|update|up|upgrade|link|ln)$/.test(sub(c)) && (flag(c, 'g', '--global') || has(c, '--location=global')),
  },
  { id: 'install', tier: 'voice', reason: INSTALL, names: ['yarn'], when: c => sub(c) === 'global' },
  { id: 'publish', tier: 'voice', reason: PUBLISH, names: ['npm', 'pnpm', 'yarn', 'bun'], when: c => /^(publish|unpublish|deprecate)$/.test(sub(c)) },
  { id: 'install', tier: 'voice', reason: INSTALL, names: ['pipx'], when: c => /^(install|uninstall|upgrade|upgrade-all|reinstall|reinstall-all|inject|uninstall-all)$/.test(sub(c)) },
  { id: 'install', tier: 'voice', reason: INSTALL, names: ['uv'], when: c => sub(c) === 'tool' && /^(install|uninstall|upgrade)$/.test(operands(c)[1] ?? '') },
  { id: 'install', tier: 'voice', reason: INSTALL, names: ['cargo', 'go', 'gem'], when: c => /^(install|uninstall)$/.test(sub(c)) },
  { id: 'install', tier: 'voice', reason: INSTALL, names: ['brew', 'port'], when: c => /^(install|uninstall|reinstall|upgrade|remove|rm|tap|untap)$/.test(sub(c)) },
  {
    id: 'install', tier: 'voice', reason: INSTALL, names: ['apt', 'apt-get', 'aptitude', 'dnf', 'yum', 'zypper', 'snap', 'flatpak'],
    when: c => /^(install|reinstall|remove|purge|autoremove|upgrade|full-upgrade|dist-upgrade|in|rm|erase|uninstall|update)$/.test(sub(c)),
  },
  { id: 'install', tier: 'voice', reason: INSTALL, names: ['pacman'], when: c => c.args.some(w => /^-[SRU]/.test(w.text) || /^--(sync|remove|upgrade)$/.test(w.text)) },
  { id: 'install', tier: 'voice', reason: INSTALL, names: ['dpkg'], when: c => c.args.some(w => /^(-i|-r|-P|--install|--remove|--purge|--unpack|--configure)$/.test(w.text)) },
  { id: 'install', tier: 'voice', reason: INSTALL, names: ['rpm'], when: c => c.args.some(w => /^-[iUFe]/.test(w.text) || /^--(install|upgrade|freshen|erase)$/.test(w.text)) },
  { id: 'install', tier: 'voice', reason: INSTALL, names: ['softwareupdate'], when: c => has(c, '-i', '--install', '-ia', '--install-rosetta') },
  { id: 'install', tier: 'voice', reason: INSTALL, names: ['dism'], when: c => sw(c, 'enable-feature', 'disable-feature', 'add-package', 'remove-package', 'add-capability', 'remove-capability') },
  { id: 'install', tier: 'voice', reason: INSTALL, names: ['wsl'], when: c => has(c, '--install', '--update') },
  {
    id: 'setting', tier: 'voice', reason: SETTING, in: PWSH,
    names: ['set-timezone', 'set-date', 'set-culture', 'set-winuserlanguagelist', 'set-winhomelocation', 'set-winsystemlocale', 'set-winuilanguageoverride',
      'set-windefaultinputmethodoverride', 'rename-computer'],
  },
  { id: 'setting', tier: 'voice', reason: SETTING, names: ['tzutil'], when: c => sw(c, 's') },
  {
    id: 'setting', tier: 'voice', reason: SETTING, names: ['powercfg'],
    when: c => sw(c, 'change', 'x', 'setactive', 's', 'setacvalueindex', 'setdcvalueindex', 'hibernate', 'h', 'delete', 'd', 'import', 'duplicatescheme', 'changename',
      'setsecuritydescriptor', 'deviceenablewake', 'devicedisablewake', 'requestsoverride'),
  },
  { id: 'setting', tier: 'voice', reason: SETTING, names: ['setx'] },
  { id: 'setting', tier: 'voice', reason: SETTING, names: ['defaults'], when: c => /^(write|delete|import|rename)$/.test(sub(c)) },
  { id: 'setting', tier: 'voice', reason: SETTING, names: ['gsettings'], when: c => /^(set|reset|reset-recursively)$/.test(sub(c)) },
  { id: 'setting', tier: 'voice', reason: SETTING, names: ['dconf'], when: c => /^(write|reset|load)$/.test(sub(c)) },
  { id: 'setting', tier: 'voice', reason: SETTING, names: ['git'], when: gitConfigGlobal },
  { id: 'setting', tier: 'voice', reason: SETTING, names: ['timedatectl', 'hostnamectl', 'localectl'], when: c => /^set-/.test(sub(c)) || (sub(c) === 'hostname' && operands(c).length > 1) },
  { id: 'setting', tier: 'voice', reason: SETTING, names: ['scutil'], when: c => has(c, '--set') },
  { id: 'setting', tier: 'voice', reason: SETTING, names: ['systemsetup', 'networksetup'], when: c => c.low.some(a => a.startsWith('-set')) },
  { id: 'push', tier: 'voice', reason: 'pushes commits to the remote', names: ['git'], when: c => gitArgs(c).sub === 'push' },
  { id: 'git-output', tier: 'voice', reason: 'writes a file', names: ['git'], when: c => gitOutput(c) === 'inside' },
  { id: 'github', tier: 'voice', reason: 'posts to GitHub', names: ['gh'], when: ghWrites },
  { id: 'publish', tier: 'voice', reason: PUBLISH, names: ['docker', 'podman'], when: c => sub(c) === 'push' },
  { id: 'publish', tier: 'voice', reason: PUBLISH, names: ['twine'], when: c => sub(c) === 'upload' },
  { id: 'publish', tier: 'voice', reason: PUBLISH, names: ['cargo', 'poetry', 'uv'], when: c => sub(c) === 'publish' },
  { id: 'publish', tier: 'voice', reason: PUBLISH, names: ['gem'], when: c => sub(c) === 'push' },
  { id: 'sleep', tier: 'voice', reason: 'puts the PC to sleep', names: ['rundll32'], when: c => dllEntry(c).startsWith('powrprof.dll,setsuspendstate') },
  { id: 'sleep', tier: 'voice', reason: 'puts the PC to sleep', names: ['shutdown'], when: c => c.low.includes('/h') },
  { id: 'sleep', tier: 'voice', reason: 'puts the PC to sleep', names: ['pmset'], when: c => has(c, 'sleepnow') },
  { id: 'sleep', tier: 'voice', reason: 'puts the PC to sleep', names: ['systemctl'], when: c => /^(suspend|hibernate|hybrid-sleep|suspend-then-hibernate)$/.test(sub(c)) },
  { id: 'close', tier: 'voice', reason: 'closes programs', names: ['stop-process', 'taskkill', 'tskill', 'kill', 'pkill', 'killall', 'xkill'] },
  { id: 'close', tier: 'voice', reason: 'closes programs', names: ['wmic'], when: c => has(c, 'process') && has(c, 'delete', 'terminate') },
  { id: 'close', tier: 'voice', reason: 'closes programs', names: ['wsl'], when: c => has(c, '--shutdown', '--terminate', '-t') },
  { id: 'clipboard', tier: 'voice', reason: 'shows the clipboard to Claude', names: ['get-clipboard', 'pbpaste', 'wl-paste'] },
  { id: 'clipboard', tier: 'voice', reason: 'shows the clipboard to Claude', names: ['xclip'], when: c => has(c, '-o', '-out') },
  {
    id: 'clipboard', tier: 'voice', reason: 'shows the clipboard to Claude', names: ['xsel'],
    when: c => !c.args.some(w => /^--(input|clear|delete|keep|exchange|append)$/.test(w.text) || /^-[a-z]*[icdkxa][a-z]*$/.test(w.text)),
  },
  { id: 'screenshot', tier: 'voice', reason: 'takes a screenshot', names: ['screencapture', 'scrot', 'gnome-screenshot', 'grim', 'spectacle', 'maim', 'flameshot'] },
  { id: 'screenshot', tier: 'voice', reason: 'takes a screenshot', names: ['import'], when: c => has(c, '-window') },
  { id: 'send', tier: 'voice', reason: 'sends data out', names: ['curl'], when: curlSends },
  {
    id: 'send', tier: 'voice', reason: 'sends data out', names: ['wget'],
    when: c => c.low.some(a => /^--(post-data|post-file|body-data|body-file)/.test(a) || (a.startsWith('--method=') && !/=(get|head|options)$/.test(a))),
  },
  {
    id: 'send', tier: 'voice', reason: 'sends data out', names: ['invoke-webrequest', 'invoke-restmethod'], in: PWSH,
    when: c => !/^(get|head|options|default)?$/.test(paramValue(c, 'method') ?? '') || param(c, 'body') || param(c, 'infile') || param(c, 'form'),
  },
  { id: 'send', tier: 'voice', reason: 'sends data out', names: ['scp', 'rsync'], when: sendsAway },
  { id: 'send', tier: 'voice', reason: 'sends data out', names: ['sftp', 'ftp', 'tftp'] },

]

const ROWS_BY_NAME = new Map<string, Row[]>()
for (const row of TIER_TABLE) {
  for (const name of row.names) ROWS_BY_NAME.set(name, [...(ROWS_BY_NAME.get(name) ?? []), row])
}
/** pwsh cmdlet names (Verb-Noun) of rows that ask or deny: matched as plain words anywhere. */
const BAREWORDS = new Set(TIER_TABLE.filter(row => row.tier !== 'pass').flatMap(row => row.names.filter(name => /^[a-z]+-[a-z]+$/.test(name))))

function judgeRows(c: Call): Verdict {
  let v = PASS
  for (const row of ROWS_BY_NAME.get(c.name) ?? []) {
    if (row.in !== undefined && !row.in.includes(c.dialect)) continue
    if (row.when !== undefined && !row.when(c)) continue
    v = stricter(v, verdict(row.tier, row.id, row.reason))
  }
  return v
}

// ---- Commands that run other commands ----

type Evaluator = (c: Call, ctx: Ctx) => Verdict

/** Code passed inline to an interpreter that deletes files or starts programs. */
const INLINE_TRIGGERS =
  /rmtree|os\.remove|unlink|rmdir|rmsync|rimraf|file\.delete|directory\.delete|subprocess|os\.system|os\.popen|os\.exec|child_process|\bexec\w*\s*\(|\bspawn\w*\s*\(|\bsystem\s*[('"`\s]|shell_exec|passthru\s*\(|popen|fileutils\.rm|\brm_r|do shell script|send2trash/i
const INLINE = verdict('screen', 'inline-code', 'runs code that deletes files or starts programs')

/** The options that carry inline code, by interpreter. */
const CODE_OPTIONS: Record<string, RegExp> = {
  python: /^-[A-Za-z]*c$/,
  py: /^-[A-Za-z]*c$/,
  node: /^(-e|--eval|-p|--print)$/,
  bun: /^(-e|--eval|-p|--print)$/,
  perl: /^-[A-Za-z]*[eE]$/,
  ruby: /^-[A-Za-z]*e$/,
  php: /^-r$/,
  osascript: /^-e$/,
}

/** python -c, node -e, perl -e, deno eval, osascript -e ...: the inline code, and the script file named, if any. */
function inlineCode(c: Call): { code: Word[]; file: Word | undefined } {
  const option = CODE_OPTIONS[c.name]
  const code: Word[] = []
  if (c.name === 'deno') {
    const at = c.low.indexOf('eval')
    const word = at >= 0 ? c.args.slice(at + 1).find(w => !w.text.startsWith('-')) : undefined
    const file = at < 0 ? c.args.slice(c.low.indexOf('run') + 1).find(w => !w.text.startsWith('-')) : undefined
    return { code: word === undefined ? [] : [word], file }
  }
  for (let i = 0; i < c.args.length; i++) {
    const word = c.args[i]
    const text = word?.text ?? ''
    if (option?.test(text)) {
      const value = c.args[i + 1]
      if (value !== undefined) code.push(value)
      i++
      continue
    }
    if (text === '-m' || text === '--') break // a module or the end of options: what follows is not inline code
    if (!text.startsWith('-')) return { code, file: word }
  }
  return { code, file: undefined }
}

/** What an interpreter's code is checked as: its kind of file. */
const INTERPRETER_FILE: Record<string, FileKind> = {
  python: 'python', py: 'python', node: 'node', bun: 'node', deno: 'node', perl: 'perl', ruby: 'ruby', php: 'php', osascript: 'osascript',
}

/** Code for an interpreter (inline, piped in, or a file's text): what it does that needs a say. */
function inlineVerdict(kind: string, text: string): Verdict {
  const low = text.toLowerCase()
  let v = PASS
  if (kind === 'osascript' && /keystroke|key code/.test(low)) v = verdict('never', 'keystrokes', KEYSTROKES)
  if (kind === 'osascript' && /\b(mail|messages)\b/.test(low) && /\bsend\b/.test(low)) v = stricter(v, verdict('screen', 'email', EMAIL))
  if (INLINE_TRIGGERS.test(text)) v = stricter(v, INLINE)
  return stricter(v, secrets(text))
}

const interpreter: Evaluator = (c, ctx) => {
  const { code, file } = inlineCode(c)
  if (code.some(w => w.dynamic)) return BUILT
  if (file !== undefined && isBuiltFile(file)) return runsInput(c)
  const kind = INTERPRETER_FILE[c.name] ?? 'python'
  let v = PASS
  // A script file (`python app.py`, `node build.js`): its text gets the same checks; one that cannot be read is screen.
  if (file !== undefined && !file.block) {
    const path = literalText(file, ctx)
    v = stricter(v, path === undefined ? SCRIPT_UNREADABLE : runFile(path, kind, ctx))
  }
  const texts = [...code.map(w => w.text), ...(file !== undefined || code.length > 0 ? [] : c.stdin)]
  for (const text of texts) v = stricter(v, inlineVerdict(c.name, text))
  if (code.length === 0 && file === undefined) {
    // A file fed on stdin (`python3 < x.py`) is the program.
    v = stricter(v, stdinFiles(c, kind, ctx))
    if (c.stdin.length === 0 && c.reads.length === 0 && c.piped && c.name !== 'osascript') v = stricter(v, runsInput(c))
  }
  return v
}

/** bash, sh, zsh ...: `-c 'text'` is judged as bash; with no script file (or `-s`) the program comes from stdin: a pipe, a here-document or a file. */
const unixShell: Evaluator = (c, ctx) => {
  let v = PASS
  for (const text of c.stdin) v = stricter(v, judgeText(text, 'bash', ctx))
  for (let i = 0; i < c.args.length; i++) {
    const text = c.args[i]?.text ?? ''
    if (/^-[A-Za-z]*c[A-Za-z]*$/.test(text)) {
      const script = c.args[i + 1]
      if (script === undefined) return v
      return stricter(v, script.dynamic ? BUILT : judgeText(script.text, 'bash', ctx))
    }
    // -s: the program is read from stdin; what follows are its arguments.
    if (/^-[A-Za-z]*s[A-Za-z]*$/.test(text)) break
    if (/^[-+][oO]$/.test(text)) {
      i++
      continue
    }
    if (/^[-+]/.test(text)) continue
    // A script file the shell runs: read its text and judge it as bash; one named only at run time is screen.
    const file = c.args[i]
    if (file === undefined) return v
    if (isBuiltFile(file)) return stricter(v, runsInput(c))
    if (file.block) return v
    const path = literalText(file, ctx)
    return stricter(v, path === undefined ? SCRIPT_UNREADABLE : runFile(path, 'bash', ctx))
  }
  v = stricter(v, stdinFiles(c, 'bash', ctx))
  return c.piped && c.stdin.length === 0 && c.reads.length === 0 ? stricter(v, runsInput(c)) : v
}

/** `wmic process call create "cmd"` starts a program out of sight; its command line is judged as cmd too. */
const wmic: Evaluator = (c, ctx) => {
  const create = c.low.indexOf('create')
  if (!c.low.includes('process') || !c.low.includes('call') || create < 0) return PASS
  return stricter(HIDDEN, judgeLine(c.args.slice(create + 1), 'cmd', ctx))
}

/** su runs as another user, usually root; its -c text is judged as bash. */
const su: Evaluator = (c, ctx) => {
  let v = ADMIN
  for (const [i, word] of c.args.entries()) {
    const text = word.text
    if (text.startsWith('--command=')) return stricter(v, word.dynamic ? BUILT : judgeText(text.slice('--command='.length), 'bash', ctx))
    if (text === '-c' || text === '--command' || /^-[A-Za-z]*c$/.test(text)) {
      const script = c.args[i + 1]
      if (script !== undefined) v = stricter(v, script.dynamic ? BUILT : judgeText(script.text, 'bash', ctx))
      return v
    }
  }
  return v
}

const PWSH_SWITCHES = ['noprofile', 'nologo', 'noninteractive', 'noexit', 'sta', 'mta', 'login', 'interactive', 'noprofileloadtime']
const PWSH_VALUES = ['executionpolicy', 'ep', 'windowstyle', 'version', 'inputformat', 'outputformat', 'configurationname', 'workingdirectory', 'wd',
  'settingsfile', 'custompipename', 'psconsolefile']

/** powershell.exe and pwsh: -Command text and positional text are judged as pwsh, -EncodedCommand is decoded and judged. */
const powershell: Evaluator = (c, ctx) => {
  for (let i = 0; i < c.args.length; i++) {
    const word = c.args[i]
    if (word === undefined || word.block) return PASS
    const text = c.low[i] ?? ''
    if (!/^[-/]./.test(text) || word.quoted) return judgeLine(c.args.slice(i), 'pwsh', ctx)
    const name = text.slice(1).replace(/^-/, '').split(':')[0] ?? ''
    if ('command'.startsWith(name) && name.startsWith('c')) {
      const rest = c.args.slice(i + 1)
      if (rest[0]?.text === '-') return pwshInput(c, ctx)
      return rest[0]?.block === true ? PASS : judgeLine(rest, 'pwsh', ctx)
    }
    if (name === 'ec' || ('encodedcommand'.startsWith(name) && name.startsWith('e'))) {
      const value = c.args[i + 1]
      const decoded = value === undefined || value.dynamic ? undefined : decodeCommand(value.text)
      return decoded === undefined ? HIDDEN : stricter(HIDDEN, judgeText(decoded, 'pwsh', ctx))
    }
    if ('file'.startsWith(name) && name.startsWith('f')) {
      // -File runs a script: its text is read and judged as pwsh; one named only at run time is screen.
      const script = c.args[i + 1]
      if (script === undefined || script.block) return PASS
      if (isBuiltFile(script)) return BUILT
      const path = literalText(script, ctx)
      return path === undefined ? SCRIPT_UNREADABLE : runFile(path, 'pwsh', ctx)
    }
    if (PWSH_SWITCHES.some(option => option.startsWith(name))) continue
    if (PWSH_VALUES.some(option => option.startsWith(name))) i++
  }
  return pwshInput(c, ctx)
}

/** PowerShell reading its commands from stdin (`-Command -`, or no command at all): a file there is read and judged as pwsh; a pipe or here-document is screen. */
function pwshInput(c: Call, ctx: Ctx): Verdict {
  let v = stdinFiles(c, 'pwsh', ctx)
  for (const text of c.stdin) v = stricter(v, judgeText(text, 'pwsh', ctx))
  return c.piped || c.stdin.length > 0 ? stricter(v, runsInput(c)) : v
}

/** cmd's own switches before /c, /k or /r: /a /u /q /d /s /x /y /?, and /e: /f: /v: /t: with their values. */
const CMD_SWITCH = /^(a|u|q|d|s|x|y|\?|[efv](:\w*)?|t:[0-9a-f]{1,2})$/i
const CMD_UNREADABLE = verdict('screen', 'unreadable', 'runs cmd with arguments Jarvis cannot read')

/**
 * cmd /c, /k and /r: the rest of the line is judged as cmd. Switches may be
 * joined (`/s/c`, `/v:on/c`) and the command glued on (`/cdel x`, `/c"del
 * x"`), as cmd.exe reads them. Arguments it cannot read are screen.
 */
const cmd: Evaluator = (c, ctx) => {
  for (const [i, word] of c.args.entries()) {
    const text = switchText(word.text, c.dialect)
    if (!/^[/-]/.test(text)) return CMD_UNREADABLE
    // Each switch up to the next `/`; a doubled `/` is read as one.
    for (const [j, part] of text.slice(1).split('/').entries()) {
      if (part === '') continue
      if (/^[ckr]/i.test(part)) {
        const rest = [part.slice(1), ...text.slice(1).split('/').slice(j + 1)].join('/')
        const glued = rest === '' ? [] : [{ ...word, text: rest, items: [rest] }]
        return judgeLine([...glued, ...c.args.slice(i + 1)], 'cmd', ctx)
      }
      if (!CMD_SWITCH.test(part)) return CMD_UNREADABLE
    }
  }
  // No /c: cmd reads its commands from stdin, a file (`cmd < x.bat`) judged as cmd.
  const v = stdinFiles(c, 'cmd', ctx)
  return c.piped || c.stdin.length > 0 ? stricter(v, runsInput(c)) : v
}

/** wsl: `wsl cmd ...`, `wsl -e cmd ...`, `wsl -- cmd ...` run in the default Linux shell. */
const wsl: Evaluator = (c, ctx) => {
  let v = PASS
  for (let i = 0; i < c.args.length; i++) {
    const text = c.low[i] ?? ''
    if (text === '-e' || text === '--exec') return stricter(v, judgeWords(c.args.slice(i + 1), c, ctx, 'bash'))
    if (text === '--') return stricter(v, judgeLine(c.args.slice(i + 1), 'bash', ctx))
    if (text === '-u' || text === '--user') {
      if (c.low[i + 1] === 'root') v = stricter(v, ADMIN)
      i++
      continue
    }
    if (['-d', '--distribution', '--cd', '--shell-type', '--distribution-id'].includes(text)) {
      i++
      continue
    }
    if (text.startsWith('-')) return v // --install, --list, --shutdown ...: the table judges those
    return stricter(v, judgeLine(c.args.slice(i), 'bash', ctx))
  }
  return v
}

/** pwsh parameter binding for Start-Process: which words are FilePath, ArgumentList, Verb and Credential. */
const START_PROCESS: readonly [string, boolean][] = [
  ['filepath', true], ['argumentlist', true], ['args', true], ['verb', true], ['credential', true], ['workingdirectory', true],
  ['windowstyle', true], ['redirectstandardinput', true], ['redirectstandardoutput', true], ['redirectstandarderror', true],
  ['environment', true], ['wait', false], ['nonewwindow', false], ['passthru', false], ['usenewenvironment', false], ['loaduserprofile', false],
]

const startProcess: Evaluator = (c, ctx) => {
  const named = new Map<string, Word>()
  const positional: Word[] = []
  for (let i = 0; i < c.args.length; i++) {
    const word = c.args[i]
    if (word === undefined) break
    const given = word.quoted ? undefined : /^-([a-z]\w*)(:(.*))?$/i.exec(word.text)
    if (!given) {
      positional.push(word)
      continue
    }
    const [name, takesValue] = START_PROCESS.find(([option]) => option.startsWith((given[1] ?? '').toLowerCase())) ?? ['', false]
    if (!takesValue) continue
    const value = given[3] !== undefined ? { ...newWord(), text: given[3], items: [given[3]] } : c.args[++i]
    if (value !== undefined) named.set(name === 'args' ? 'argumentlist' : name, value)
  }
  let v = PASS
  if (/^runas/i.test(named.get('verb')?.text ?? '')) v = ADMIN
  if (named.has('credential')) v = stricter(v, verdict('screen', 'admin', 'runs as another user'))
  const file = named.get('filepath') ?? positional.shift()
  const list = named.get('argumentlist') ?? positional.shift()
  if (file === undefined || file.block) return v
  if (file.dynamic && /[$(`…]/.test(baseName(file.text))) return stricter(v, BUILT)
  if (list?.dynamic === true) return stricter(v, BUILT)
  // Windows starts the file by its type, looking a bare name up in the current folder too.
  v = stricter(v, launch(file, 'shell', ctx))
  const program = literalText(file, ctx) ?? file.text
  const line = `& '${program.replace(/'/g, "''")}' ${list === undefined ? '' : list.items.join(' ')}`
  return stricter(v, judgeText(line, 'pwsh', ctx))
}

/** cmd's `start ["title"] [/options] program args`; Git Bash's `start` hands its words to it too. */
const cmdStart: Evaluator = (c, ctx) => {
  if (c.dialect === 'pwsh') return PASS
  let i = c.args[0]?.quoted === true ? 1 : 0
  while (c.low[i]?.startsWith('/') === true) i += /^\/(d|node|affinity|machine)$/.test(c.low[i] ?? '') ? 2 : 1
  return judgeWords(c.args.slice(i), c, ctx, 'cmd')
}

/** Literal text after an option (`schtasks /tr`, `forfiles /c`, `runas`'s program), judged as cmd. */
const optionText = (option: RegExp): Evaluator => (c, ctx) => {
  const at = c.low.findIndex(a => option.test(a))
  const word = at < 0 ? undefined : c.args[at + 1]
  if (word === undefined) return PASS
  return word.dynamic ? BUILT : judgeText(word.text, 'cmd', ctx)
}

const runas: Evaluator = (c, ctx) => {
  const program = c.args.find((w, i) => !(c.low[i] ?? '').startsWith('/'))
  if (program === undefined) return ADMIN
  return stricter(ADMIN, program.dynamic ? BUILT : judgeText(program.text, 'cmd', ctx))
}

/** Invoke-Expression: a literal is judged as pwsh; anything else is built at run time. */
const invokeExpression: Evaluator = (c, ctx) => {
  const arg = c.args.find(w => w.quoted || w.dynamic || !w.text.startsWith('-'))
  if (arg === undefined) return c.piped ? runsInput(c) : PASS
  if (arg.dynamic || arg.block) return DOWNLOADING.test(arg.text.toLowerCase()) || DOWNLOADING.test(c.text) ? DOWNLOAD_RUN : BUILT
  return judgeText(arg.text, 'pwsh', ctx)
}

/** Invoke-Command, Start-Job: a script block in a variable is built at run time (a literal block is judged as nested). */
const invokeCommand: Evaluator = c => {
  for (const [i, word] of c.args.entries()) {
    const text = c.low[i] ?? ''
    const option = /^-([a-z]+)/.exec(text)?.[1]
    const value = c.args[i + 1]
    if (option !== undefined && option.startsWith('s') && 'scriptblock'.startsWith(option) && value?.dynamic === true && !value.block) return BUILT
    if (i === 0 && option === undefined && word.dynamic && !word.block) return BUILT
  }
  return PASS
}

/** ForEach-Object: its block runs once per item (a loop); `% Delete` and `% Kill` call a method on each. */
const forEachObject: Evaluator = (c, ctx) => {
  ctx.state.loop = true
  const member = c.args.find(w => !w.block && !w.text.startsWith('-'))?.text.toLowerCase()
  if (member === 'delete') return verdict('screen', 'delete', DELETE)
  if (member === 'kill') return verdict('voice', 'close', 'closes programs')
  return PASS
}

const findExec: Evaluator = (c, ctx) => {
  let v = PASS
  for (let i = 0; i < c.args.length; i++) {
    if (!/^-(exec|execdir|ok|okdir)$/.test(c.low[i] ?? '')) continue
    let end = i + 1
    while (end < c.args.length && c.args[end]?.text !== ';' && c.args[end]?.text !== '+') end++
    v = stricter(v, judgeWords(c.args.slice(i + 1, end), c, ctx, 'bash'))
    i = end
  }
  return v
}

const EVALUATORS: Record<string, Evaluator> = {
  bash: unixShell, sh: unixShell, zsh: unixShell, dash: unixShell, ksh: unixShell, ash: unixShell, fish: unixShell,
  powershell, pwsh: powershell, powershell_ise: powershell,
  cmd,
  wsl,
  'start-process': startProcess,
  start: cmdStart,
  'invoke-item': openItems, explorer: openItems,
  'import-module': importModule,
  'rename-item': renameItem, ren: renameItem, rename: renameItem,
  'new-item': makesLink, mklink: makesLink, ln: makesLink, fsutil: makesLink,
  runas,
  schtasks: optionText(/^\/tr$/),
  forfiles: optionText(/^\/c$/),
  'invoke-expression': invokeExpression,
  'invoke-command': invokeCommand, 'start-job': invokeCommand, 'start-threadjob': invokeCommand,
  'invoke-history': () => BUILT,
  'foreach-object': forEachObject,
  eval: (c, ctx) => judgeLine(c.args, 'bash', ctx),
  source: sourceFile, '.': sourceFile,
  su,
  wmic,
  find: findExec,
  python: interpreter, py: interpreter, node: interpreter, bun: interpreter, deno: interpreter,
  perl: interpreter, ruby: interpreter, php: interpreter, osascript: interpreter,
}

// ---- The judge ----

/**
 * How much say the person needs before this tool call runs. Bash, Monitor
 * and PowerShell commands are read as shell text; Write, Edit and
 * NotebookEdit are judged by their path alone. Any other tool passes. It
 * never throws: an error inside counts as screen.
 */
export function judge(tool: string, input: Readonly<Record<string, unknown>>, resolved: Resolved = {}): Verdict {
  try {
    return judgeTool(tool, input, resolved)
  } catch {
    return verdict('screen', 'unreadable', 'could not be checked')
  }
}

function judgeTool(tool: string, input: Readonly<Record<string, unknown>>, resolved: Resolved): Verdict {
  switch (tool) {
    case 'Bash':
      return typeof input.command === 'string' ? judgeShell(input.command, ['bash'], resolved) : verdict('screen', 'unreadable', 'unreadable Bash input')
    case 'PowerShell': {
      const text = typeof input.command === 'string' ? input.command : typeof input.script === 'string' ? input.script : undefined
      return text === undefined ? verdict('screen', 'unreadable', 'unreadable PowerShell input') : judgeShell(text, ['pwsh'], resolved)
    }
    case 'Monitor': {
      // Its shell is not stated, so a command is read both ways and the stricter wins.
      if (typeof input.command === 'string') return judgeShell(input.command, ['bash', 'pwsh'], resolved)
      const ws = input.ws
      if (input.command === undefined && typeof ws === 'object' && ws !== null && typeof (ws as { url?: unknown }).url === 'string') {
        return secrets((ws as { url: string }).url)
      }
      return verdict('screen', 'unreadable', 'unreadable Monitor input')
    }
    case 'Write':
    case 'Edit':
      return judgePath(input.file_path, resolved)
    case 'NotebookEdit':
      return judgePath(input.notebook_path, resolved)
    default:
      return PASS
  }
}

/** A file tool's path as Windows opens it: each name's trailing dots and spaces dropped (`settings.json. ` is settings.json). */
const windowsNames = (path: string): string =>
  path
    .split(/([\\/])/)
    .map(part => (part === '.' || part === '..' ? part : part.replace(/[. ]+$/, '')))
    .join('')

/**
 * Write, Edit and NotebookEdit: judged by the path as given and by its real path (every link and
 * junction followed, which pc.ts asks the engine for), and the stricter wins. A path that is not yet
 * resolved waits on that (screen if it never comes); one whose real path still carries an 8.3 short
 * name is not resolved either: screen.
 */
function judgePath(path: unknown, resolved: Resolved): Verdict {
  if (typeof path !== 'string') return verdict('screen', 'unreadable', 'unreadable file path')
  const given = (spelling: string): Verdict => stricter(stricter(secrets(spelling), writePathVerdict(spelling)), stricter(secrets(windowsNames(spelling)), writePathVerdict(windowsNames(spelling))))
  let v = given(path)
  if (v.tier === 'never') return v
  const real = resolved.realPath
  if (real === undefined) return stricter(v, withNeed(UNRESOLVED_PATH, { kind: 'realpath', path }))
  if (real === null) return stricter(v, UNRESOLVED_PATH)
  v = stricter(v, given(real))
  return hasShortName(real) ? stricter(v, UNRESOLVED_PATH) : v
}

const HIDDEN_CHARS = /[\u0000-\u0008\u000b-\u001f\u007f\u0085\u200b-\u200f\u2028-\u202e\u2060-\u2064\u2066-\u2069\ufeff]/

function judgeShell(raw: string, dialects: readonly Dialect[], resolved: Resolved): Verdict {
  const text = normalizeText(raw)
  const secret = secrets(text)
  if (secret.tier === 'never') return secret
  if (text.length > MAX_CHARS) return mentionsProtected(text) ? CLAUDE_SETTINGS : verdict('screen', 'unreadable', 'is too long to check')
  let v = HIDDEN_CHARS.test(text) ? HIDDEN_TEXT : PASS
  v = stricter(v, encoded(text))
  if (dialects.includes('bash')) v = stricter(v, devClipboard(text))
  for (const dialect of dialects) {
    const script = new Lexer(text, dialect, 0).lex()
    const ctx: Ctx = { dialect, text: text.toLowerCase(), depth: 0, state: newState(), resolved }
    // A file Claude wrote this session, named in a command that does more than read: its contents decide too.
    if (!readsOnly(script)) v = stricter(v, stricter(writePathVerdict(text), judgeWritten(text, ctx)))
    v = stricter(v, judgeScript(script, ctx))
    // Such a file that one part of the command may write and another names runs as the first left it, not as Jarvis read it.
    const state = ctx.state
    if ([...state.named].some(name => state.made.has(name) && (state.namedIn.get(name) ?? 0) > 1)) v = stricter(v, MADE_FIRST)
  }
  return v
}

// ---- Settings rules ----

/** Deny rules for writing Claude Code's own settings and Jarvis's secrets with Write, Edit and NotebookEdit. */
const FILE_DENY = [
  'Edit(~/.claude/settings.json)', 'Edit(~/.claude/settings.local.json)', 'Edit(~/.claude.json)', 'Edit(~/.claude/plugins/**)',
  'Edit(.claude/settings.json)', 'Edit(.claude/settings.local.json)', 'Edit(~/.jarvis/home/credentials*)',
]

/**
 * The tier table's never rows as settings rules the person can paste into
 * their own settings.json (/jarvis pc rules prints it; nothing here writes
 * settings), and the env value that turns on the PowerShell tool. Only deny
 * rules: an allow rule would make Claude Code looser (`git log --output`
 * writes files, `cat` reads any), and an ask rule would put the engine's
 * dialog on top of the guard's own question.
 */
export function rulesSnippet(): string {
  const deny = new Set<string>()
  for (const row of TIER_TABLE) {
    if (row.tier !== 'never' || row.rules === undefined) continue
    for (const rule of row.rules.powershell ?? []) deny.add(`PowerShell(${rule})`)
    for (const rule of row.rules.bash ?? []) deny.add(`Bash(${rule})`)
  }
  for (const rule of FILE_DENY) deny.add(rule)
  const settings = {
    env: { CLAUDE_CODE_USE_POWERSHELL_TOOL: '1' },
    permissions: { deny: [...deny] },
  }
  return JSON.stringify(settings, null, 2)
}
