import assert from 'node:assert/strict'
import { test } from 'node:test'

import { loginItem } from '../src/main/login'

// Electron 44 writes the Run value as AddQuoteForArg(path) + ' ' + AddQuoteForArg(arg)
// for each arg (shell/common/command_line_util_win.cc), and Windows splits it
// back with CommandLineToArgvW's rules. Both are ported here to pin what
// Windows will run at sign-in.

function addQuoteForArg(arg: string): string {
  if (!/[ \\"]/.test(arg)) return arg
  let out = '"'
  for (let i = 0; i < arg.length; i += 1) {
    if (arg[i] === '\\') {
      let end = i + 1
      while (end < arg.length && arg[end] === '\\') end += 1
      const count = end - i
      out += '\\'.repeat(end === arg.length || arg[end] === '"' ? count * 2 : count)
      i = end - 1
    } else if (arg[i] === '"') out += '\\"'
    else out += arg[i]
  }
  return `${out}"`
}

function commandLineToArgv(line: string): string[] {
  const args: string[] = []
  let current = ''
  let isQuoted = false
  let hasArg = false
  for (let i = 0; i < line.length; i += 1) {
    const char = line[i]
    if (char === '\\') {
      let end = i
      while (line[end] === '\\') end += 1
      const count = end - i
      if (line[end] === '"') {
        current += '\\'.repeat(Math.floor(count / 2))
        if (count % 2 === 1) {
          current += '"'
          i = end
        } else i = end - 1
      } else {
        current += '\\'.repeat(count)
        i = end - 1
      }
      hasArg = true
    } else if (char === '"') {
      isQuoted = !isQuoted
      hasArg = true
    } else if ((char === ' ' || char === '\t') && !isQuoted) {
      if (hasArg) args.push(current)
      current = ''
      hasArg = false
    } else {
      current += char
      hasArg = true
    }
  }
  if (hasArg) args.push(current)
  return args
}

const runValue = ({ path, args }: { path: string; args: string[] }): string => [path, ...args].map(addQuoteForArg).join(' ')

test('Start with Windows runs Electron with the app folder, quoted once', () => {
  const exe = 'C:\\Users\\Rotem\\jarvis-claude-mod\\app\\node_modules\\electron\\dist\\electron.exe'
  for (const folder of ['C:\\Users\\Rotem\\jarvis-claude-mod\\app', 'C:\\Users\\Rotem Bab\\code\\jarvis-claude-mod\\app', 'D:\\jarvis\\app\\']) {
    const item = loginItem(exe, folder)
    assert.deepEqual(item, { path: exe, args: [folder] })
    assert.deepEqual(commandLineToArgv(runValue(item)), [exe, folder])
  }
  // What quoting it here would do: Windows would hand Electron the quotes too.
  const twice = runValue({ path: exe, args: ['"C:\\Users\\Rotem\\jarvis-claude-mod\\app"'] })
  assert.equal(commandLineToArgv(twice)[1], '"C:\\Users\\Rotem\\jarvis-claude-mod\\app"')
})
