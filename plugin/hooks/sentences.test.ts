import { describe, expect, test } from 'claude-code/testing'

import { CODE_PLACEHOLDER, SentenceSplitter, splitSentences, TABLE_PLACEHOLDER } from './sentences'

/** Feeds `text` one character at a time, as the slowest stream would. */
function streamed(text: string): string[] {
  const splitter = new SentenceSplitter()
  const out: string[] = []
  for (const char of text) out.push(...splitter.push(char))
  return [...out, ...splitter.flush()]
}

describe('sentence splitter', () => {
  test('splits plain sentences on . ! and ?', () => {
    expect(splitSentences('Hello there. How are you? I am fine!')).toEqual([
      'Hello there.',
      'How are you?',
      'I am fine!',
    ])
  })

  test('keeps abbreviations, initials and decimals inside a sentence', () => {
    expect(splitSentences('Mr. Smith met Dr. Jones at 3.14 p.m. sharp. Then they left.')).toEqual([
      'Mr. Smith met Dr. Jones at 3.14 p.m. sharp.',
      'Then they left.',
    ])
    expect(splitSentences('Use e.g. apples, i.e. fruit, etc. and more. Next one.')).toEqual([
      'Use e.g. apples, i.e. fruit, etc. and more.',
      'Next one.',
    ])
    expect(splitSentences('J. R. R. Tolkien wrote it. Version 2.0 is out. It costs $3. Cheap.')).toEqual([
      'J. R. R. Tolkien wrote it.',
      'Version 2.0 is out.',
      'It costs $3.',
      'Cheap.',
    ])
  })

  test('does not split inside Windows paths or URLs', () => {
    expect(
      splitSentences('Open C:\\Users\\Rotem\\notes.txt. Then visit https://example.com/a.b?x=1. Done.'),
    ).toEqual(['Open C:\\Users\\Rotem\\notes.txt.', 'Then visit https://example.com/a.b?x=1.', 'Done.'])
  })

  test('treats an ellipsis as an end only before a capital', () => {
    expect(splitSentences('Wait... what? Well... I think so.')).toEqual(['Wait... what?', 'Well...', 'I think so.'])
  })

  test('keeps closing quotes with their sentence', () => {
    expect(splitSentences('He said "Done." Then he left.')).toEqual(['He said "Done."', 'Then he left.'])
  })

  test('strips markdown: headings, emphasis, inline code, links, bullets', () => {
    const text = '## Result\n\nThe **build** passed in `ci`. See [the log](https://ci.example/run/1.2).\n- one item\n2. second item\n> quoted'
    expect(splitSentences(text)).toEqual([
      'Result',
      'The build passed in ci.',
      'See the log.',
      'one item',
      'second item',
      'quoted',
    ])
    expect(splitSentences('snake_case stays, _emphasis_ goes, *star* goes, 3 * 4 stays.')).toEqual([
      'snake_case stays, emphasis goes, star goes, 3 * 4 stays.',
    ])
  })

  test('replaces fenced code with one spoken note per reply', () => {
    const text = 'Here it is:\n```python\nprint("not. spoken.")\n```\nAnd another:\n~~~\nmore code\n~~~\nThat is all.'
    expect(splitSentences(text)).toEqual(['Here it is:', CODE_PLACEHOLDER, 'And another:', 'That is all.'])
  })

  test('an unterminated fence swallows the rest', () => {
    expect(splitSentences('Look:\n```\ncode. more.')).toEqual(['Look:', CODE_PLACEHOLDER])
  })

  test('replaces a markdown table with a spoken note', () => {
    expect(splitSentences('Results:\n| a | b |\n|---|---|\n| 1 | 2 |\nAfter the table.')).toEqual([
      'Results:',
      TABLE_PLACEHOLDER,
      'After the table.',
    ])
  })

  test('splits a long sentence near the cap at a comma or space', () => {
    const long = `${'word '.repeat(30)}and then, ${'more words '.repeat(30)}end.`
    const parts = splitSentences(long)
    expect(parts.length).toBeGreaterThan(1)
    for (const part of parts) expect(part.length).toBeLessThanOrEqual(220)
    expect(parts.join(' ').replace(/\s+/g, ' ')).toBe(long.trim().replace(/\s+/g, ' '))
    expect(parts[0]?.length ?? 0).toBeLessThanOrEqual(120)
  })

  test('streaming one character at a time gives the same sentences as all at once', () => {
    const texts = [
      'Mr. Smith met Dr. Jones at 3.14 p.m. sharp. Then they left.',
      'See [the docs](https://docs.example.com/x.y) for more. **Bold** end.',
      'Here:\n```ts\nconst a = 1.5\n```\n| x |\n|---|\nDone. Wait... Really?',
      '1. First step.\n2. Second step.\n- [ ] a task\n#hashtag and #1 priority.',
    ]
    for (const text of texts) expect(streamed(text)).toEqual(splitSentences(text))
  })

  test('emits the first sentence as soon as the next word starts', () => {
    const splitter = new SentenceSplitter()
    expect(splitter.push('Certainly, sir. ')).toEqual([])
    expect(splitter.push('T')).toEqual(['Certainly, sir.'])
    expect(splitter.push('he build passed')).toEqual([])
    expect(splitter.flush()).toEqual(['The build passed'])
  })

  test('a line end finishes the sentence on it', () => {
    const splitter = new SentenceSplitter()
    expect(splitter.push('Let me check that')).toEqual([])
    expect(splitter.push('\n')).toEqual(['Let me check that'])
  })

  test('drops pieces with nothing to say', () => {
    expect(splitSentences('---\n\n***\n:\n')).toEqual([])
  })
})
