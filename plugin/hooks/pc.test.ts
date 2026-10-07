import { describe, expect, test } from 'claude-code/testing'
import type { Plugin, Engine as TestEngine } from 'claude-code/testing'

import type { JarvisHud } from '../types'
import { GUARD_FAILED } from './pc'
import { parseUacPolicy, parseWhoamiGroups, system32 } from './platform'
import type { FakeChild, RunAnswer, RunCall, World } from './test-harness'
import { completeTurn, jarvis, runStep, startHelper, startSession, textChunks, WINDOWS_ENV, world } from './test-harness'
import { isNoPhrase, isStandDownPhrase, isStopPhrase, isYesPhrase } from './voice'

const CLOUD_ENV = { ...WINDOWS_ENV, CLAUDE_CODE_REMOTE: 'true' }
const LINUX_ENV = { HOME: '/home/rotem' }
const HELD = /^Jarvis held this: it pushes commits to the remote and needs the user's spoken OK\. Say in one short sentence/
const SECOND_HOLD = /^Jarvis did not hold this: it [^,]+, and this turn already held another command\. One spoken yes answers one question, so neither is held now\./
const HEARD_WHILE_TALKING =
  "The user's 'yes' was heard while Jarvis was still speaking, so it does not count; the command is held again. End your turn now without calling another tool: Jarvis asks the user again himself."
/** Jarvis's own question about a held `git push`, as the helper is sent it (sentence by sentence). */
const ASK_PUSH_SPOKEN = ['Claude wants to run a Bash command that pushes commits to the remote: git push.', 'Say yes to run it, sir.']
const PUSH_QUESTION = 'Jarvis: Claude wants to run a Bash command that pushes commits to the remote: "git push". Run it?'
const DECLINED = 'The user chose "Don\'t run it" on Jarvis\'s on-screen question, so nothing ran. Do not retry unless they ask.'
const COULD_NOT_ASK = 'Jarvis could not ask the user on screen, so nothing ran.'
const BYPASS = "Permission checks are off. Jarvis still asks before risky commands, but Claude Code's own rules are skipped."
const ELEVATED_STATUS = 'JARVIS · error · Claude Code runs as administrator, so Jarvis stays off'
const FAILED_STATUS = 'JARVIS · error · Jarvis could not check for administrator rights, so it stays off (/jarvis restart checks again)'

/** `whoami /groups /fo csv /nh` for an administrator: elevated (High label) or not (Medium, Administrators for deny only). */
function groups(elevated: boolean, admin = true): string {
  return [
    '"Everyone","Well-known group","S-1-1-0","Mandatory group, Enabled by default, Enabled group"',
    ...(admin ? [`"BUILTIN\\Administrators","Alias","S-1-5-32-544","${elevated ? 'Enabled group, Group owner' : 'Group used for deny only'}"`] : []),
    '"BUILTIN\\Users","Alias","S-1-5-32-545","Mandatory group, Enabled by default, Enabled group"',
    elevated
      ? '"Mandatory Label\\High Mandatory Level","Label","S-1-16-12288",""'
      : '"Mandatory Label\\Medium Mandatory Level","Label","S-1-16-8192",""',
  ].join('\r\n')
}

/** `reg query` of the UAC policy key with these REG_DWORD values. */
function policy(values: Record<string, number>): string {
  const lines = Object.entries(values).map(([name, value]) => `    ${name}    REG_DWORD    0x${value.toString(16)}`)
  return ['', 'HKEY_LOCAL_MACHINE\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System', ...lines, ''].join('\r\n')
}

const ALWAYS_NOTIFY = policy({ ConsentPromptBehaviorAdmin: 2, EnableLUA: 1, PromptOnSecureDesktop: 1 })
const WINDOWS_DEFAULT = policy({ ConsentPromptBehaviorAdmin: 5, EnableLUA: 1, PromptOnSecureDesktop: 1 })

const WHOAMI = 'C:\\WINDOWS\\System32\\whoami.exe'
const REG = 'C:\\WINDOWS\\System32\\reg.exe'

/** The administrator check's two commands, answered (by full path only: a `whoami` on PATH may be Git's), after `delayMs`. */
function adminCheck(w: World, whoami: string, reg: string, delayMs?: number): void {
  w.onRun = (call: RunCall): RunAnswer | undefined => {
    if (call.argv[0] === WHOAMI) return { exitCode: 0, stdout: whoami, delayMs }
    if (call.argv[0] === REG) return { exitCode: 0, stdout: reg, delayMs }
    return undefined
  }
}

/** When a clip began on the helper's own clock, and whether it began over Jarvis's voice (an older helper says neither). */
type Timing = { startedAtMs?: number; overSpeech?: boolean }

/** The helper hears the user. */
async function heard(w: World, helper: FakeChild, text: string, id: string, source: 'wake' | 'ptt' = 'wake', timing: Timing = {}): Promise<void> {
  helper.event({ type: 'utterance', id, text, source, durationMs: 900, language: 'en', ...timing })
  await w.settle()
}

/** Jarvis's reply in turn `turnId` (the question he asked at its end) stopped playing at `endedAtMs`, on the helper's clock. */
async function spoke(w: World, helper: FakeChild, turnId: string, endedAtMs: number | undefined = 50_000, interrupted = false): Promise<void> {
  helper.event({ type: 'speech_done', replyId: turnId, interrupted, spokenText: '', ...(endedAtMs === undefined ? {} : { endedAtMs }) })
  await w.settle()
}

/** Jarvis's question at the end of turn `turnId` played out, and then the user answered it. */
async function answered(w: World, helper: FakeChild, turnId: string, text: string, id: string, source: 'wake' | 'ptt' = 'wake'): Promise<void> {
  await spoke(w, helper, turnId, 50_000)
  await heard(w, helper, text, id, source, { startedAtMs: 50_400, overSpeech: false })
}

/** The engine starts the turn for a prompt (spoken or typed). */
async function turn($: TestEngine, w: World, text: string, turnId: string): Promise<void> {
  await $.classic.UserPromptSubmit({ prompt: text, permission_mode: 'default' })
  await $.turn.start({ text, turnId })
  await w.settle()
}

const bash = ($: TestEngine, command: string) => $.tool.call({ tool: 'Bash', command })

const spokenTexts = (w: World) => w.named('speak').map(command => String(command.body.text))

/** Waits in real time, which the hook budget counts (the test runner's own timer; the hooks' typings leave it out). */
const realSleep = (ms: number): Promise<void> =>
  new Promise(resolve => (globalThis as unknown as { setTimeout: (fn: () => void, ms: number) => void }).setTimeout(resolve, ms))

/**
 * Above Jarvis: every question dialog is answered only after 12 s of real
 * time, past one hook's 10 s budget. The wait is three 4 s naps, each a tool
 * call of its own, so no hook spends more than 4 s of its own time.
 */
const SLOW_ANSWER: Plugin = {
  name: 'slow-answer',
  tier: 'prepend',
  register(on) {
    on('tool.call', { tool: 'mcp__slow__nap' }, async () => {
      await realSleep(4000)
      return { result: 'rested' }
    })
    on('tool.call', { tool: 'AskUserQuestion' }, async ($, e, next) => {
      for (let nap = 0; nap < 3; nap += 1) await $.tool.call({ tool: 'mcp__slow__nap' })
      return next(e)
    })
  },
}

/** Above Jarvis: abandons a Bash call a second (mocked) after it began, as Esc does. */
const INTERRUPTER: Plugin = {
  name: 'interrupter',
  tier: 'prepend',
  register(on) {
    on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
      void next(e).catch(() => undefined)
      await $.clock.sleep(1000)
      return { deny: 'Interrupted by the user.' }
    })
  },
}

describe('the guard hook', () => {
  test('a screen-tier command waits for a click; "Run it" lets it run unchanged', async ($, on) => {
    const ran: string[] = []
    on('tool.call', { tool: 'Bash' }, ($, e) => {
      ran.push(e.command)
      return { result: { stdout: 'removed', stderr: '', interrupted: false } }
    })
    const w = world(on)
    await startHelper($, w)
    w.askAnswer = 'Run it'
    expect(await bash($, 'rm -rf ~/Downloads')).toMatchObject({ result: { stdout: 'removed' } })
    expect(ran).toEqual(['rm -rf ~/Downloads'])
    // "Don't run it" first: Enter, a default or an auto-resolve lands on no.
    expect(w.dialogs).toEqual([
      {
        question: 'Jarvis: Claude wants to run a Bash command that deletes files: "rm -rf ~/Downloads". Run it?',
        header: 'Jarvis',
        options: ["Don't run it", 'Run it'],
        multiSelect: false,
      },
    ])
  })

  test('"Don\'t run it" refuses the call, and the HUD shows it failed', async ($, on) => {
    const w = world(on)
    await startHelper($, w)
    w.askAnswer = "Don't run it"
    expect(await bash($, 'rm -rf ~/Downloads')).toEqual({ deny: DECLINED })
    await w.settle()
    const hud = w.state.get('jarvis.hud') as JarvisHud
    expect(hud.actions.find(action => action.label === 'Bash rm -rf ~/Downloads')?.status).toBe('failed')
  })

  test('a yes typed under "Other" runs it; other words do not', async ($, on) => {
    const w = world(on)
    await startHelper($, w)
    w.askAnswer = 'Yes, please'
    expect(await bash($, 'rm old.log')).toMatchObject({ result: 'ok' })
    w.askAnswer = 'only the .tmp files'
    expect(await bash($, 'rm old.log')).toEqual({ deny: 'The user did not confirm; they wrote: "only the .tmp files". Nothing ran.' })
  })

  test('an idle auto-resolve, a dismissal or no dialog at all is a no', async ($, on) => {
    const w = world(on)
    await startHelper($, w)
    w.askAnswer = 'Run it'
    w.askIdleMs = 60_000
    expect(await bash($, 'rm old.log')).toEqual({
      deny: "Jarvis's on-screen question closed while the user was away, so nothing ran. Ask again when they are back.",
    })
    w.askIdleMs = undefined
    w.askAnswer = undefined
    expect(await bash($, 'rm old.log')).toEqual({ deny: COULD_NOT_ASK })
  })

  test('the never list is refused with no question; a harmless command asks nothing', async ($, on) => {
    const w = world(on)
    await startHelper($, w)
    w.askAnswer = 'Run it'
    expect(await bash($, 'sudo csrutil disable')).toEqual({
      deny: "Jarvis blocks this: it turns off system protection, which is on Jarvis's never list. Do not retry it or work around it; the user can do it themselves.",
    })
    expect(await bash($, 'npm test')).toMatchObject({ result: 'ok' })
    expect(w.asked).toEqual([])
  })

  test("writes to Claude Code's own settings are refused", async ($, on) => {
    const w = world(on)
    await startHelper($, w)
    const refused = await $.tool.call({ tool: 'Write', file_path: '/home/rotem/.claude/settings.json', content: '{}' })
    expect(refused).toEqual({ deny: expect.stringMatching(/^Jarvis blocks this: it changes Claude Code's own permissions or plugins/) })
    // Any other path is placed first (where it really lands): a new file in an existing folder.
    w.realPaths.set('/home/rotem', '/home/rotem')
    expect(await $.tool.call({ tool: 'Write', file_path: '/home/rotem/notes.md', content: 'hi' })).toMatchObject({ result: 'ok' })
    expect(w.asked).toEqual([])
  })

  test('a Write through a link or junction is judged where it lands; one that cannot be placed asks first', async ($, on) => {
    const w = world(on)
    await startHelper($, w)
    w.askAnswer = 'Run it'
    // C:\work\cfg is a junction to .claude: the settings file and a new plugin behind it are blocked.
    w.realPaths.set('C:\\work\\cfg\\', 'C:\\Users\\Rotem\\.claude')
    for (const file_path of ['C:\\work\\cfg\\settings.json', 'C:\\work\\cfg\\plugins\\evil\\hooks\\hooks.json']) {
      expect(await $.tool.call({ tool: 'Write', file_path, content: '{}' }), file_path).toEqual({ deny: expect.stringMatching(/^Jarvis blocks this/) })
    }
    // New folders under an ordinary one are placed through the nearest folder that is there.
    w.realPaths.set('C:\\work\\', 'C:\\work')
    expect(await $.tool.call({ tool: 'Write', file_path: 'C:\\work\\new\\deep\\a.ts', content: 'x' })).toMatchObject({ result: 'ok' })
    expect(w.asked).toEqual([])
    // A link that leads nowhere (a write would make its target), or a path with `..` past a missing folder: asked first.
    w.links.add('C:\\work\\docs.md')
    expect(await $.tool.call({ tool: 'Write', file_path: 'C:\\work\\docs.md', content: 'x' })).toMatchObject({ result: 'ok' })
    expect(await $.tool.call({ tool: 'Write', file_path: 'C:\\work\\gone\\..\\cfg\\settings.json', content: '{}' })).toMatchObject({ result: 'ok' })
    expect(w.asked).toHaveLength(2)
  })

  test('a file Claude wrote is read and judged when a later command names it, by any launcher', async ($, on) => {
    const w = world(on)
    await startHelper($, w)
    w.realPaths.set('C:\\proj\\', 'C:\\proj')
    expect(await $.tool.call({ tool: 'Write', file_path: 'C:\\proj\\deploy.ps1', content: 'x' })).toMatchObject({ result: 'ok' })
    w.fileText.set('C:\\proj\\deploy.ps1', 'Set-MpPreference -DisableRealtimeMonitoring $true')
    expect(await bash($, 'wt -d . pwsh deploy.ps1')).toEqual({ deny: expect.stringMatching(/^Jarvis blocks this: it turns off Windows Defender/) })
    // A command that only reads it runs nothing of it; once it is deleted, there is nothing to judge.
    expect(await bash($, 'cat deploy.ps1')).toMatchObject({ result: 'ok' })
    w.fileText.delete('C:\\proj\\deploy.ps1')
    expect(await bash($, 'wt -d . pwsh deploy.ps1')).toMatchObject({ result: 'ok' })
    expect(w.asked).toEqual([])
    // Its text never reaches the logs.
    expect(w.logs.join('\n')).not.toContain('Set-MpPreference')
  })

  test('a script file is read and judged before it runs; what it contains decides the tier', async ($, on) => {
    const ran: string[] = []
    on('tool.call', { tool: 'Bash' }, ($, e) => {
      ran.push(e.command)
      return { result: { stdout: '', stderr: '', interrupted: false } }
    })
    const w = world(on)
    await startHelper($, w)
    w.askAnswer = 'Run it'
    // A harmless script reads clean and runs with no question.
    w.fileText.set('/home/rotem/build.sh', 'npm run build\necho done')
    expect(await bash($, 'bash /home/rotem/build.sh')).toMatchObject({ result: { stdout: '' } })
    expect(w.asked).toEqual([])
    // A script that deletes files is held for a click.
    w.fileText.set('/home/rotem/clean.sh', 'rm -rf "$HOME/Downloads"')
    expect(await bash($, 'bash /home/rotem/clean.sh')).toMatchObject({ result: { stdout: '' } })
    expect(w.asked).toHaveLength(1)
    // A script that runs a never-list command is blocked with no question.
    w.fileText.set('/home/rotem/evil.sh', 'mkfs.ext4 /dev/sdb1')
    expect(await bash($, 'bash /home/rotem/evil.sh')).toMatchObject({ deny: expect.stringMatching(/Jarvis blocks this/) })
    // A script Jarvis cannot read is held for a click (it does not run unasked).
    w.askAnswer = "Don't run it"
    expect(await bash($, 'bash /home/rotem/missing.sh')).toEqual({ deny: DECLINED })
    expect(ran).toEqual(['bash /home/rotem/build.sh', 'bash /home/rotem/clean.sh'])
  })

  test('an 8.3 short name is resolved to its real path before a Write is judged', async ($, on) => {
    const w = world(on)
    await startHelper($, w)
    w.askAnswer = 'Run it'
    // A short name whose stem names a settings file is blocked outright (no file system needed).
    expect(await $.tool.call({ tool: 'Write', file_path: 'C:\\Users\\Rotem\\.claude\\SETTIN~1.JSO', content: '{}' })).toEqual({
      deny: expect.stringMatching(/^Jarvis blocks this: it changes Claude Code's own permissions or plugins/),
    })
    // A short name whose stem looks innocent is resolved: if it lands on the settings file, it is blocked.
    w.realPaths.set('C:\\Users\\Rotem\\AB1234~1.JSO', 'C:\\Users\\Rotem\\.claude\\settings.json')
    expect(await $.tool.call({ tool: 'Write', file_path: 'C:\\Users\\Rotem\\AB1234~1.JSO', content: '{}' })).toEqual({
      deny: expect.stringMatching(/^Jarvis blocks this/),
    })
    // One that resolves to an ordinary file runs with no question.
    w.realPaths.set('C:\\Users\\Rotem\\NOTES~1.TXT', 'C:\\Users\\Rotem\\notes-of-mine.txt')
    expect(await $.tool.call({ tool: 'Write', file_path: 'C:\\Users\\Rotem\\NOTES~1.TXT', content: 'x' })).toMatchObject({ result: 'ok' })
    // One that cannot be resolved is held for a click, never run unasked.
    expect(await $.tool.call({ tool: 'Write', file_path: 'C:\\Users\\Rotem\\GONE~1.TXT', content: 'x' })).toMatchObject({ result: 'ok' })
    expect(w.asked).toHaveLength(1)
  })

  test('a call abandoned while its question is open runs nothing, whatever is clicked later', { plugins: [INTERRUPTER] }, async ($, on) => {
    const ran: string[] = []
    on('tool.call', { tool: 'Bash' }, ($, e) => {
      ran.push(e.command)
      return { result: { stdout: '', stderr: '', interrupted: false } }
    })
    const w = world(on)
    await startHelper($, w)
    w.askAnswer = 'Run it'
    w.askDelayMs = 5000
    const call = bash($, 'rm -rf ~/Downloads')
    await w.settle()
    expect(w.asked).toHaveLength(1)
    await w.clock.advance(1000) // Esc: the call is abandoned with the question still open
    expect(await call).toEqual({ deny: 'Interrupted by the user.' })
    await w.clock.advance(5000) // then "Run it" is clicked
    await w.settle()
    expect(ran).toEqual([])
  })

  test('questions open at once each wait in a dialog of their own, under a text of their own', async ($, on) => {
    const w = world(on)
    await startHelper($, w)
    w.askAnswer = 'Run it'
    w.askDelayMs = 5000
    const first = bash($, 'rm -rf build')
    const second = bash($, 'rm -rf build')
    await w.settle()
    expect(w.asked).toEqual([
      'Jarvis: Claude wants to run a Bash command that deletes files: "rm -rf build". Run it?',
      'Jarvis: Claude wants to run a Bash command that deletes files: "rm -rf build". Run it? (2)',
    ])
    await w.clock.advance(5000)
    expect(await first).toMatchObject({ result: 'ok' })
    expect(await second).toMatchObject({ result: 'ok' })
    // Both closed: the text is free again.
    const third = bash($, 'rm -rf build')
    await w.settle()
    expect(w.asked.at(-1)).toBe('Jarvis: Claude wants to run a Bash command that deletes files: "rm -rf build". Run it?')
    await w.clock.advance(5000)
    expect(await third).toMatchObject({ result: 'ok' })
  })

  test('two calls asked at once, answered after more than 10 s of real time, are both asked, never run', { plugins: [SLOW_ANSWER], timeoutMs: 30_000 }, async ($, on) => {
    const ran: string[] = []
    on('tool.call', { tool: 'Bash' }, ($, e) => {
      ran.push(e.command)
      return { result: { stdout: '', stderr: '', interrupted: false } }
    })
    const w = world(on)
    await startHelper($, w)
    w.askAnswer = "Don't run it"
    const [build, dist] = await Promise.all([bash($, 'rm -rf build'), bash($, 'rm -rf dist')])
    expect(build).toEqual({ deny: DECLINED })
    expect(dist).toEqual({ deny: DECLINED })
    expect(w.asked).toHaveLength(2)
    expect(ran).toEqual([])
  })

  test('the question shows line breaks, and a long command by the parts that set its tier with how much is not shown', async ($, on) => {
    const w = world(on)
    await startHelper($, w)
    w.askAnswer = "Don't run it"
    await bash($, 'echo hi\n  rm -rf   build')
    expect(w.asked.at(-1)).toBe('Jarvis: Claude wants to run a Bash command that deletes files: "echo hi ⏎ rm -rf build". Run it?')

    const filler = Array.from({ length: 40 }, (_, n) => `echo step ${n}`).join('; ')
    const long = `rm -rf ./build\n${filler}\nrm -rf ~/Documents`
    await bash($, long)
    const question = w.asked.at(-1) ?? ''
    const hidden = `rm -rf ./build ⏎ ${filler} ⏎ rm -rf ~/Documents`.length - 'rm -rf ./build'.length - 'rm -rf ~/Documents'.length
    expect(question).toBe(
      `Jarvis: Claude wants to run a Bash command that deletes files, in these parts: "rm -rf ./build" … "rm -rf ~/Documents" (${hidden.toLocaleString('en-US')} more characters not shown). Run it?`,
    )

    // One long line: the piece of it that deletes.
    await bash($, `${filler}; rm -rf ~/Documents`)
    expect(w.asked.at(-1)).toMatch(/that deletes files, in this part: "rm -rf ~\/Documents" \([\d,]+ more characters not shown\)\. Run it\?$/)

    // The piece that runs a script, when what the script does sets the tier.
    w.fileText.set('/home/rotem/clean.sh', 'rm -rf "$HOME/Downloads"')
    await bash($, `${filler}; bash /home/rotem/clean.sh`)
    expect(w.asked.at(-1)).toMatch(/that deletes files, in this part: "bash \/home\/rotem\/clean\.sh" \([\d,]+ more characters not shown\)\. Run it\?$/)
  })

  test('a cloud session is left alone', async ($, on) => {
    const w = world(on, { env: CLOUD_ENV })
    await startSession($, w)
    expect(await bash($, 'rm -rf build')).toMatchObject({ result: 'ok' })
    expect(w.asked).toEqual([])
  })

  test('the failure answer refuses', () => {
    expect(GUARD_FAILED).toMatch(/did not run/)
  })
})

describe('voice consent', () => {
  test('a voice-tier command is held, Jarvis asks about it himself, and it runs on a plain spoken yes', async ($, on) => {
    const w = world(on)
    const helper = await startHelper($, w)
    await heard(w, helper, 'Push my branch', 'u1')
    await turn($, w, 'Push my branch', 't1')
    expect(await bash($, 'git push')).toEqual({ deny: expect.stringMatching(HELD) })
    expect(w.asked).toEqual([])
    // The model's own words may say anything: Jarvis's line, last in the reply, names the command.
    await runStep($, w, 't1', textChunks('Shall I look up the forecast for you?'))
    await completeTurn($, 't1')
    await w.settle()
    const said = w.named('speak').filter(command => command.body.replyId === 't1').map(command => String(command.body.text))
    expect(said).toEqual(['Shall I look up the forecast for you?', ...ASK_PUSH_SPOKEN, ''])
    expect(w.named('speak').at(-1)?.body).toMatchObject({ replyId: 't1', text: '', final: true })
    expect(w.toasts).toContain('Jarvis: say yes to run a Bash command: "git push"')
    expect(w.logs).toContain('Jarvis is waiting for a spoken yes to run a Bash command: "git push"')

    await answered(w, helper, 't1', 'Yes.', 'u2')
    await turn($, w, 'Yes.', 't2')
    // Exactly that command; spacing aside.
    expect(await bash($, 'git  push')).toMatchObject({ result: 'ok' })
    // Another command is not covered by that yes; nor is the same one twice.
    expect(await bash($, 'git push --tags')).toEqual({ deny: expect.stringMatching(/^Jarvis held this/) })
    expect(await bash($, 'git push')).toEqual({ deny: expect.stringMatching(SECOND_HOLD) })
    expect(w.asked).toEqual([])
  })

  test('a yes answers only the question just asked: any turn in between unbinds it', async ($, on) => {
    const w = world(on)
    const helper = await startHelper($, w)
    await heard(w, helper, 'Close Notepad', 'u1')
    await turn($, w, 'Close Notepad', 't1')
    expect(await bash($, 'git push')).toEqual({ deny: expect.stringMatching(HELD) })
    await completeTurn($, 't1')
    // Spoken: a no, and another question.
    await heard(w, helper, 'No, leave it. What time is it?', 'u2')
    await turn($, w, 'No, leave it. What time is it?', 't2')
    await completeTurn($, 't2')
    await heard(w, helper, 'Yes.', 'u3')
    await turn($, w, 'Yes.', 't3')
    expect(await bash($, 'git push')).toEqual({ deny: expect.stringMatching(HELD) })
    await completeTurn($, 't3')
    // Typed, in between.
    await turn($, w, 'what is the weather', 't4')
    await completeTurn($, 't4')
    await heard(w, helper, 'Yes.', 'u5')
    await turn($, w, 'Yes.', 't5')
    expect(await bash($, 'git push')).toEqual({ deny: expect.stringMatching(HELD) })
    expect(w.asked).toEqual([])
  })

  test('one yes runs one command: a turn that holds two holds neither', async ($, on) => {
    const w = world(on)
    const helper = await startHelper($, w)
    await heard(w, helper, 'Push and open a PR', 'u1')
    await turn($, w, 'Push and open a PR', 't1')
    expect(await bash($, 'git push')).toEqual({ deny: expect.stringMatching(HELD) })
    expect(await bash($, 'gh pr create --fill')).toEqual({ deny: expect.stringMatching(SECOND_HOLD) })
    expect(await bash($, 'git push')).toEqual({ deny: expect.stringMatching(SECOND_HOLD) })
    await completeTurn($, 't1')
    await heard(w, helper, 'Yes.', 'u2')
    await turn($, w, 'Yes.', 't2')
    expect(await bash($, 'git push')).toEqual({ deny: expect.stringMatching(HELD) })
    expect(await bash($, 'gh pr create --fill')).toEqual({ deny: expect.stringMatching(SECOND_HOLD) })
    await completeTurn($, 't2')
    // Asked about one: that one runs on the yes, and only once.
    await heard(w, helper, 'Push it', 'u3')
    await turn($, w, 'Push it', 't3')
    expect(await bash($, 'git push')).toEqual({ deny: expect.stringMatching(HELD) })
    await completeTurn($, 't3')
    await heard(w, helper, 'Yes.', 'u4')
    await turn($, w, 'Yes.', 't4')
    expect(await bash($, 'gh pr create --fill')).toEqual({ deny: expect.stringMatching(/^Jarvis held this: it posts to GitHub and needs the user's spoken OK/) })
    await completeTurn($, 't4')
    await w.settle() // Jarvis's question about it is sent before the test ends
    expect(w.asked).toEqual([])
  })

  test('a yes said over Jarvis does not count, and he asks again; a push-to-talk yes after his question does', async ($, on) => {
    const w = world(on)
    const helper = await startHelper($, w)
    await heard(w, helper, 'Push my branch', 'u1')
    await turn($, w, 'Push my branch', 't1')
    await bash($, 'git push')
    await completeTurn($, 't1')

    helper.event({ type: 'barge_in', spokenText: 'Claude wants to' })
    await spoke(w, helper, 't1', 50_000, true)
    await heard(w, helper, 'yes', 'u2', 'wake', { startedAtMs: 49_800, overSpeech: true })
    await turn($, w, 'yes', 't2')
    expect(await bash($, 'git push')).toEqual({ deny: HEARD_WHILE_TALKING })
    await completeTurn($, 't2')
    await w.settle()
    expect(spokenTexts(w).filter(text => text === ASK_PUSH_SPOKEN[0])).toHaveLength(2)

    await answered(w, helper, 't2', 'Go ahead, Jarvis.', 'u3', 'ptt')
    await turn($, w, 'Go ahead, Jarvis.', 't3')
    expect(await bash($, 'git push')).toMatchObject({ result: 'ok' })
  })

  test('the flag for words said over Jarvis is the clip\'s own: a clean yes after a barge-in counts, a yes over him never does', async ($, on) => {
    const w = world(on)
    const helper = await startHelper($, w)
    await heard(w, helper, 'Push my branch', 'u1')
    await turn($, w, 'Push my branch', 't1')
    await bash($, 'git push')
    await completeTurn($, 't1')
    // Someone cut in (a clip still being transcribed), then the user said yes once Jarvis had finished.
    helper.event({ type: 'barge_in', spokenText: 'Claude wants' })
    await answered(w, helper, 't1', 'Yes.', 'u2')
    await turn($, w, 'Yes.', 't2')
    expect(await bash($, 'git push')).toMatchObject({ result: 'ok' })
    await completeTurn($, 't2')

    await heard(w, helper, 'Push my branch', 'u3')
    await turn($, w, 'Push my branch', 't3')
    await bash($, 'git push')
    await completeTurn($, 't3')
    // A TV says "yes" over Jarvis's question; the barge_in event came before another clip's words.
    await spoke(w, helper, 't3', 60_000, true)
    await heard(w, helper, 'Yes.', 'u4', 'wake', { startedAtMs: 59_000, overSpeech: true })
    await turn($, w, 'Yes.', 't4')
    expect(await bash($, 'git push')).toEqual({ deny: HEARD_WHILE_TALKING })
  })

  test('a yes said before Jarvis asked (queued while the turn ran) answers nothing: a click decides', async ($, on) => {
    const ran: string[] = []
    on('tool.call', { tool: 'Bash' }, ($, e) => {
      ran.push(e.command)
      return { result: { stdout: '', stderr: '', interrupted: false } }
    })
    const w = world(on)
    const helper = await startHelper($, w)
    await heard(w, helper, 'Tidy up my repo', 'u1', 'wake', { startedAtMs: 10_000, overSpeech: false })
    await turn($, w, 'Tidy up my repo', 't1')
    // Jarvis is quiet while a tool runs: the user's "Go ahead." waits as the next prompt.
    await heard(w, helper, 'Go ahead.', 'u2', 'wake', { startedAtMs: 20_000, overSpeech: false })
    expect(await bash($, 'git push')).toEqual({ deny: expect.stringMatching(HELD) })
    await completeTurn($, 't1')
    await spoke(w, helper, 't1', 30_000)
    await turn($, w, 'Go ahead.', 't2')
    w.askAnswer = "Don't run it"
    expect(await bash($, 'git push')).toEqual({ deny: DECLINED })
    expect(ran).toEqual([])
    expect(w.asked).toEqual([PUSH_QUESTION])
  })

  test('a yes the helper did not time (an older helper), or one begun while Jarvis still asked, goes to a click', async ($, on) => {
    const w = world(on)
    const helper = await startHelper($, w)
    await heard(w, helper, 'Push my branch', 'u1')
    await turn($, w, 'Push my branch', 't1')
    await bash($, 'git push')
    await completeTurn($, 't1')
    await spoke(w, helper, 't1', undefined)
    await heard(w, helper, 'Yes.', 'u2')
    await turn($, w, 'Yes.', 't2')
    w.askAnswer = "Don't run it"
    expect(await bash($, 'git push')).toEqual({ deny: DECLINED })
    expect(w.asked).toEqual([PUSH_QUESTION])
    await completeTurn($, 't2')

    // Push-to-talk pressed while Jarvis still asked: a key press, but before the question ended.
    await heard(w, helper, 'Push my branch', 'u3')
    await turn($, w, 'Push my branch', 't3')
    await bash($, 'git push')
    await completeTurn($, 't3')
    await spoke(w, helper, 't3', 70_000, true)
    await heard(w, helper, 'Yes.', 'u4', 'ptt', { startedAtMs: 69_500, overSpeech: true })
    await turn($, w, 'Yes.', 't4')
    expect(await bash($, 'git push')).toEqual({ deny: DECLINED })
    expect(w.asked).toEqual([PUSH_QUESTION, PUSH_QUESTION])
  })

  test('an abort, a spoken stop, a dropped "No.", a typed prompt, a /clear or a new session drops the hold', async ($, on) => {
    const w = world(on)
    const helper = await startHelper($, w)
    const hold = async (turnId: string, id: string): Promise<void> => {
      await heard(w, helper, 'Push my branch', id)
      await turn($, w, 'Push my branch', turnId)
      expect(await bash($, 'git push')).toEqual({ deny: expect.stringMatching(HELD) })
    }
    const isWaiting = async (): Promise<boolean> => (await jarvis($, 'pc')).includes('Waiting for a spoken OK: one command')

    // Esc before Jarvis asked: nothing is said, and a later "Okay." is no answer.
    await hold('t1', 'u1')
    await completeTurn($, 't1', true)
    expect(spokenTexts(w)).not.toContain(ASK_PUSH_SPOKEN[0])
    expect(await isWaiting()).toBe(false)
    await answered(w, helper, 't1', 'Okay.', 'u2')
    await turn($, w, 'Okay.', 't2')
    expect(await bash($, 'git push')).toEqual({ deny: expect.stringMatching(HELD) })
    await completeTurn($, 't2', true)

    // "Jarvis, stop." after the question.
    await hold('t3', 'u3')
    await completeTurn($, 't3')
    await spoke(w, helper, 't3')
    expect(await isWaiting()).toBe(true)
    await heard(w, helper, 'Jarvis, stop.', 'u4', 'wake', { startedAtMs: 50_200, overSpeech: false })
    expect(await isWaiting()).toBe(false)
    await heard(w, helper, 'Yes.', 'u5', 'wake', { startedAtMs: 50_400, overSpeech: false })
    await turn($, w, 'Yes.', 't4')
    expect(await bash($, 'git push')).toEqual({ deny: expect.stringMatching(HELD) })
    await completeTurn($, 't4', true)

    // "No." dropped while another dialog waits for a click still counts as no.
    await hold('t5', 'u6')
    await completeTurn($, 't5')
    w.askAnswer = 'Leave it'
    w.askDelayMs = 5000
    const dialog = $.tool.call({
      tool: 'AskUserQuestion',
      questions: [{ question: 'Turn the TV off?', header: 'Jarvis home', options: [{ label: 'Leave it', description: '' }, { label: 'Turn it off', description: '' }], multiSelect: false }],
    })
    await w.settle()
    await heard(w, helper, 'No.', 'u7', 'wake', { startedAtMs: 50_200, overSpeech: false })
    expect(w.logs).toContain('Jarvis: "No." was not sent: the question on screen needs a click.')
    await w.clock.advance(5000)
    await dialog
    w.askDelayMs = undefined
    expect(await isWaiting()).toBe(false)
    await answered(w, helper, 't5', 'Yes.', 'u8')
    await turn($, w, 'Yes.', 't6')
    expect(await bash($, 'git push')).toEqual({ deny: expect.stringMatching(HELD) })
    await completeTurn($, 't6', true)

    // A typed prompt in between.
    await hold('t7', 'u9')
    await completeTurn($, 't7')
    await $.prompt.submit({ text: 'what is the weather', wait: false, origin: { kind: 'composer' } })
    expect(await isWaiting()).toBe(false)
    await completeTurn($, 't7', true)

    // A /clear: the session ends, and no session.start follows.
    await hold('t8', 'u10')
    await completeTurn($, 't8')
    expect(await isWaiting()).toBe(true)
    await $.session.end({ reason: 'clear', sessionId: 's1', resume: { id: 's1' } })
    expect(await isWaiting()).toBe(false)
    // A session started anew.
    await hold('t9', 'u11')
    await completeTurn($, 't9')
    await w.settle()
    expect(await isWaiting()).toBe(true)
    await $.session.start({ cwd: 'C:\\work', surface: 'terminal', isInteractive: true })
    await w.settle()
    expect(await isWaiting()).toBe(false)
  })

  test('the yes is bound to the command line by line: one command does not answer for two', async ($, on) => {
    const ran: string[] = []
    on('tool.call', { tool: 'Bash' }, ($, e) => {
      ran.push(e.command)
      return { result: { stdout: '', stderr: '', interrupted: false } }
    })
    const w = world(on)
    const helper = await startHelper($, w)
    await heard(w, helper, 'Push it', 'u1')
    await turn($, w, 'Push it', 't1')
    expect(await bash($, 'git push origin npm publish')).toEqual({ deny: expect.stringMatching(/^Jarvis held this/) })
    await completeTurn($, 't1')
    await answered(w, helper, 't1', 'Yes.', 'u2')
    await turn($, w, 'Yes.', 't2')
    expect(await bash($, 'git push origin\nnpm publish')).toEqual({ deny: expect.stringMatching(/^Jarvis held this/) })
    expect(ran).toEqual([])
  })

  test('a yes for a script run is bound to what the script held: one rewritten since is held again', async ($, on) => {
    const ran: string[] = []
    on('tool.call', { tool: 'Bash' }, ($, e) => {
      ran.push(e.command)
      return { result: { stdout: '', stderr: '', interrupted: false } }
    })
    const w = world(on)
    const helper = await startHelper($, w)
    w.fileText.set('/home/rotem/ship.sh', 'git push')
    await heard(w, helper, 'Ship it', 'u1')
    await turn($, w, 'Ship it', 't1')
    expect(await bash($, 'bash /home/rotem/ship.sh')).toEqual({ deny: expect.stringMatching(HELD) })
    await completeTurn($, 't1')
    await answered(w, helper, 't1', 'Yes.', 'u2')
    await turn($, w, 'Yes.', 't2')
    // The same command, but the script now does something else: the yes was not for that.
    w.fileText.set('/home/rotem/ship.sh', 'npm publish')
    expect(await bash($, 'bash /home/rotem/ship.sh')).toEqual({ deny: expect.stringMatching(/^Jarvis held this: it publishes a package/) })
    await completeTurn($, 't2')
    // Asked again and answered: exactly that script runs.
    await answered(w, helper, 't2', 'Yes.', 'u3')
    await turn($, w, 'Yes.', 't3')
    expect(await bash($, 'bash /home/rotem/ship.sh')).toMatchObject({ result: { stdout: '' } })
    expect(ran).toEqual(['bash /home/rotem/ship.sh'])
    expect(w.asked).toEqual([])
    // The script's text never reaches the logs.
    expect(w.logs.join('\n')).not.toContain('npm publish')
  })

  test('a yes with more words, or a typed yes, is not a spoken OK', async ($, on) => {
    const w = world(on)
    const helper = await startHelper($, w)
    await heard(w, helper, 'Push my branch', 'u1')
    await turn($, w, 'Push my branch', 't1')
    await bash($, 'git push')
    await completeTurn($, 't1')
    await heard(w, helper, 'Yes, and delete dist', 'u2')
    await turn($, w, 'Yes, and delete dist', 't2')
    expect(await bash($, 'git push')).toEqual({ deny: expect.stringMatching(HELD) })
    await completeTurn($, 't2')

    // Typed: asked on screen instead.
    w.askAnswer = "Don't run it"
    await turn($, w, 'yes', 't3')
    expect(await bash($, 'git push')).toEqual({ deny: DECLINED })
    expect(w.asked).toEqual([PUSH_QUESTION])
  })

  test('a yes beside words in another script is no yes, spoken or typed', async ($, on) => {
    const w = world(on)
    const helper = await startHelper($, w)
    await heard(w, helper, 'Push my branch', 'u1')
    await turn($, w, 'Push my branch', 't1')
    await bash($, 'git push')
    await completeTurn($, 't1')
    await answered(w, helper, 't1', 'OK, не надо', 'u2')
    await turn($, w, 'OK, не надо', 't2')
    expect(await bash($, 'git push')).toEqual({ deny: expect.stringMatching(HELD) })
    await completeTurn($, 't2', true)
    // Typed under "Other": "ok, not now".
    await turn($, w, 'clean up', 't3')
    w.askAnswer = 'ok, לא עכשיו'
    expect(await bash($, 'rm -rf ~/Downloads')).toEqual({
      deny: 'The user did not confirm; they wrote: "ok, לא עכשיו". Nothing ran.',
    })
  })

  test('a held command expires after two minutes', async ($, on) => {
    const w = world(on)
    const helper = await startHelper($, w)
    await heard(w, helper, 'Push my branch', 'u1')
    await turn($, w, 'Push my branch', 't1')
    await bash($, 'git push')
    await completeTurn($, 't1')
    await w.clock.advance(121_000)
    await heard(w, helper, 'yes', 'u2')
    await turn($, w, 'yes', 't2')
    expect(await bash($, 'git push')).toEqual({ deny: expect.stringMatching(HELD) })
  })

  test('a typed turn asks on screen', async ($, on) => {
    const w = world(on)
    await startHelper($, w)
    w.askAnswer = 'Run it'
    await turn($, w, 'push it', 't1')
    expect(await bash($, 'git push')).toMatchObject({ result: 'ok' })
    expect(w.asked).toHaveLength(1)
  })

  test('while a question is open on screen, a spoken yes is not sent: it needs a click', async ($, on) => {
    const w = world(on)
    const helper = await startHelper($, w)
    await heard(w, helper, 'Clear out the build folder', 'u1')
    await turn($, w, 'Clear out the build folder', 't1')
    w.askAnswer = "Don't run it"
    w.askDelayMs = 5000
    const call = bash($, 'rm -rf build')
    await w.settle()
    expect(w.asked).toHaveLength(1)
    expect(spokenTexts(w)).toContain('That one needs your OK on screen, sir.')

    await heard(w, helper, 'Yes!', 'u2')
    expect(w.submits).toHaveLength(1)
    expect(spokenTexts(w)).toContain('I need a click on screen for that one, sir.')
    expect(w.logs).toContain('Jarvis: "Yes!" was not sent: the question on screen needs a click.')

    await w.clock.advance(5000)
    expect(await call).toEqual({ deny: DECLINED })
    // Once answered, words are prompts again.
    await heard(w, helper, 'Yes!', 'u3')
    expect(w.submits).toHaveLength(2)
  })

  test("another dialog open (another plugin's, Claude's own) also needs a click, not a spoken yes", async ($, on) => {
    const w = world(on)
    const helper = await startHelper($, w)
    w.askAnswer = 'Leave it'
    w.askDelayMs = 5000
    const dialog = $.tool.call({
      tool: 'AskUserQuestion',
      questions: [{ question: 'Turn the TV off?', header: 'Jarvis home', options: [{ label: 'Leave it', description: '' }, { label: 'Turn it off', description: '' }], multiSelect: false }],
    })
    await w.settle()
    await heard(w, helper, 'Yes.', 'u1')
    expect(w.submits).toEqual([])
    expect(w.logs).toContain('Jarvis: "Yes." was not sent: the question on screen needs a click.')
    await w.clock.advance(5000)
    await dialog
    await heard(w, helper, 'Yes.', 'u2')
    expect(w.submits).toHaveLength(1)
  })
})

describe('spoken phrases', () => {
  test('a yes, a no and stand down are words said on their own', () => {
    for (const yes of ['Yes.', 'Yes please, Jarvis.', 'Go ahead.', 'OK.', 'Okay, do it.', 'Proceed, thank you.']) expect(isYesPhrase(yes)).toBe(true)
    for (const other of ['Yes, and delete dist', 'Is it done?', 'Go ahead and push everything', 'yesterday']) expect(isYesPhrase(other)).toBe(false)
    // A refusal in another script beside the yes: "OK, don't", "Okay, don't do it", "Sure, don't".
    for (const mixed of ['OK, не надо', 'Okay, אל תעשה את זה', 'Sure, 不要', 'Yes 2']) expect(isYesPhrase(mixed), mixed).toBe(false)
    for (const no of ['No.', "Don't do it.", 'Nope, thanks.']) expect(isNoPhrase(no)).toBe(true)
    for (const down of ['Jarvis, stand down.', 'Abort that!', 'Abort.']) expect(isStandDownPhrase(down)).toBe(true)
    expect(isStandDownPhrase('Abort the merge and push')).toBe(false)
    // Still a stop phrase, for a voice without PC control.
    expect(isStopPhrase('Stand down.')).toBe(true)
  })
})

describe('stand down', () => {
  test('"Stand down" stops speech, aborts the running typed turn and stops its background command', async ($, on) => {
    on('tool.call', { tool: 'Bash' }, () => ({ result: { stdout: '', stderr: '', interrupted: false, backgroundTaskId: 'b1' } }))
    const w = world(on)
    const helper = await startHelper($, w)
    await turn($, w, 'Start the dev server', 't1')
    await $.tool.call({ tool: 'Bash', command: 'npm run dev', run_in_background: true })

    await heard(w, helper, 'Jarvis, stand down.', 'u1')
    await w.clock.advance(300)
    await w.settle()
    expect(w.aborts).toEqual(['t1'])
    expect(w.stoppedTasks).toEqual(['b1'])
    expect(w.named('stop').map(command => command.body)).toEqual([{ reason: 'stand_down' }])
    expect(w.submits).toEqual([])
    expect(w.logs).toContain('Jarvis: stood down (stopped speech, aborted the turn, stopped 1 background command).')
    expect(w.toasts).toContain('Jarvis stood down.')
  })

  test('/jarvis pc stop does the same; a stopped task is not stopped twice', async ($, on) => {
    on('tool.call', { tool: 'Monitor' }, () => ({ result: { taskId: 'm7', timeoutMs: 60_000, persistent: false } }))
    const w = world(on)
    await startHelper($, w)
    await $.tool.call({ tool: 'Monitor', command: 'tail -f app.log', description: 'Watch the log', timeout_ms: 60_000 })
    const stopping = jarvis($, 'pc stop')
    await w.settle()
    expect(await stopping).toBe('Stood down: stopped speech, no turn was running, stopped 1 background command.')
    expect(w.stoppedTasks).toEqual(['m7'])
    expect(await jarvis($, 'pc stop')).toBe('Stood down: stopped speech, no turn was running, stopped 0 background commands.')
  })
})

describe('never as administrator', () => {
  test('when Claude Code runs elevated, the helper never starts', async ($, on) => {
    const w = world(on)
    adminCheck(w, groups(true), ALWAYS_NOTIFY)
    await startSession($, w)
    expect(w.helpers()).toEqual([])
    expect(w.status()).toBe(ELEVATED_STATUS)
    expect(w.toasts).toContain('Jarvis stays off: Claude Code runs as administrator.')

    await jarvis($, '')
    await jarvis($, 'restart')
    await w.settle()
    expect(w.helpers()).toEqual([])
    expect(w.logs.filter(line => line.startsWith('Jarvis stays off: Claude Code runs as administrator'))).toHaveLength(2)
    expect(await jarvis($, 'pc')).toContain('Administrator: Claude Code runs as administrator')
  })

  test("as root on Linux too, by the system's own id", async ($, on) => {
    const w = world(on, { env: LINUX_ENV })
    w.onRun = call => (call.argv.join(' ') === '/usr/bin/id -u' ? { exitCode: 0, stdout: '0\n' } : undefined)
    await startSession($, w)
    expect(w.status()).toBe(ELEVATED_STATUS)
    expect(w.children).toEqual([])
  })

  test('with normal rights it starts; a UAC level below Always notify gets one hint', async ($, on) => {
    const w = world(on)
    adminCheck(w, groups(false), WINDOWS_DEFAULT)
    await startSession($, w)
    expect(w.helpers()).toHaveLength(1)
    expect(w.probes.map(call => call.argv.join(' '))).toEqual([
      `${WHOAMI} /groups /fo csv /nh`,
      `${REG} query HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System`,
    ])
    expect(w.runs).toEqual([])
    expect(w.toasts.filter(text => text.startsWith('Jarvis: UAC is below "Always notify"'))).toHaveLength(1)
    expect(w.store.get('pc.uacHint')).toBe(true)
    expect(await jarvis($, 'pc')).toContain('UAC: notify only when apps try to make changes (the Windows default). For PC control')
  })

  test('the hint is shown once, and not at all for Always notify', async ($, on) => {
    const w = world(on)
    w.store.set('pc.uacHint', true)
    adminCheck(w, groups(false), WINDOWS_DEFAULT)
    await startSession($, w)
    expect(w.toasts).toEqual([])
  })

  test('a surface that attaches while the check runs starts nothing; setup waits for it too', async ($, on) => {
    const w = world(on)
    adminCheck(w, groups(true), ALWAYS_NOTIFY, 3000)
    const starting = $.session.start({ cwd: 'C:\\work', surface: null, isInteractive: false })
    await w.settle()
    await $.session.attach({ surface: 'desktop', clientId: 'desktop:default' })
    await w.settle()
    expect(w.helpers()).toEqual([])
    expect(await jarvis($, 'setup')).toMatch(/^Jarvis is still checking whether Claude Code runs as administrator/)
    await w.clock.advance(3000)
    await starting
    await w.settle()
    expect(w.helpers()).toEqual([])
    expect(w.status()).toBe(ELEVATED_STATUS)
    expect(await jarvis($, 'setup')).toMatch(/^Jarvis stays off: Claude Code runs as administrator/)
    expect(w.children).toEqual([])
  })

  test('with normal rights, a surface that attached during the check gets the helper once it ends', async ($, on) => {
    const w = world(on)
    adminCheck(w, groups(false), ALWAYS_NOTIFY, 3000)
    const starting = $.session.start({ cwd: 'C:\\work', surface: null, isInteractive: false })
    await w.settle()
    await $.session.attach({ surface: 'desktop', clientId: 'desktop:default' })
    await w.settle()
    expect(w.helpers()).toEqual([])
    await w.clock.advance(3000)
    await starting
    await w.settle()
    expect(w.helpers()).toHaveLength(1)
  })

  test("the check runs Windows's own whoami and reg by full path", () => {
    expect(system32('C:\\WINDOWS')).toBe('C:\\WINDOWS\\System32')
    expect(system32('D:\\Win\\')).toBe('D:\\Win\\System32')
    for (const odd of [undefined, '', 'Windows', '\\\\server\\share', '%SystemRoot%']) expect(system32(odd), String(odd)).toBe('C:\\Windows\\System32')
  })

  test('a failed check keeps Jarvis off and says so; /jarvis restart checks again', async ($, on) => {
    const w = world(on)
    w.onRun = call => (call.argv[0] === WHOAMI ? { deny: 'timed out after 5000 ms' } : call.argv[0] === REG ? { exitCode: 0, stdout: ALWAYS_NOTIFY } : undefined)
    await startSession($, w)
    expect(w.helpers()).toEqual([])
    expect(w.status()).toBe(FAILED_STATUS)
    expect(w.logs.some(line => line.startsWith('Jarvis stays off: it could not check whether Claude Code runs as administrator'))).toBe(true)
    expect(await jarvis($, 'pc')).toMatch(/^Administrator: check failed \(.*timed out after 5000 ms.*\), so Jarvis stays off\. \/jarvis restart checks again\.$/m)
    expect(await jarvis($, '')).toContain('Jarvis stays off: it could not check whether Claude Code runs as administrator')
    expect(await jarvis($, 'setup')).toMatch(/^Jarvis stays off: it could not check/)
    await w.settle()
    expect(w.helpers()).toEqual([])

    // The reg query failing fails the check too.
    w.onRun = call => (call.argv[0] === WHOAMI ? { exitCode: 0, stdout: groups(false) } : call.argv[0] === REG ? { exitCode: 1, stdout: '' } : undefined)
    expect(await jarvis($, 'restart')).toMatch(/^Checking again whether Claude Code runs as administrator/)
    await w.settle()
    expect(w.helpers()).toEqual([])
    expect(w.status()).toBe(FAILED_STATUS)

    adminCheck(w, groups(false), ALWAYS_NOTIFY)
    await jarvis($, 'restart')
    await w.settle()
    expect(w.helpers()).toHaveLength(1)
    expect(await jarvis($, 'pc')).toContain('Administrator: Claude Code runs with normal rights.')
  })

  test('UAC levels from the policy values', () => {
    expect(parseUacPolicy(policy({ EnableLUA: 0, ConsentPromptBehaviorAdmin: 5 }))).toBe('off')
    expect(parseUacPolicy(policy({ EnableLUA: 1, ConsentPromptBehaviorAdmin: 0 }))).toBe('never-notify')
    expect(parseUacPolicy(policy({ EnableLUA: 1, ConsentPromptBehaviorAdmin: 2 }))).toBe('always-notify')
    expect(parseUacPolicy(policy({ EnableLUA: 1, ConsentPromptBehaviorAdmin: 5, PromptOnSecureDesktop: 1 }))).toBe('default')
    expect(parseUacPolicy(policy({ EnableLUA: 1, ConsentPromptBehaviorAdmin: 5, PromptOnSecureDesktop: 0 }))).toBe('default-no-dim')
    expect(parseUacPolicy(policy({ EnableLUA: 1, ConsentPromptBehaviorAdmin: 2, TypeOfAdminApprovalMode: 2 }))).toBe('admin-protection')
    expect(parseUacPolicy(policy({ EnableLUA: 1 }))).toBeUndefined()
    expect(parseWhoamiGroups(groups(true)).has('S-1-16-12288')).toBe(true)
  })

  test('a standard account is reported as such', async ($, on) => {
    const w = world(on)
    adminCheck(w, groups(false, false), ALWAYS_NOTIFY)
    await startSession($, w)
    expect(await jarvis($, 'pc')).toContain('UAC: a standard account')
    expect(w.toasts).toEqual([])
  })
})

describe('bypass warning', () => {
  test('a session started with permission checks skipped gets one warning', async ($, on) => {
    const w = world(on)
    await startSession($, w)
    await $.classic.SessionStart({ source: 'startup', permission_mode: 'default' })
    expect(w.toasts).not.toContain(BYPASS)
    await $.classic.SessionStart({ source: 'startup', permission_mode: 'bypassPermissions' })
    await $.classic.SessionStart({ source: 'resume', permission_mode: 'bypassPermissions' })
    expect(w.toasts.filter(text => text === BYPASS)).toHaveLength(1)
    expect(await jarvis($, 'pc')).toContain('Permission mode: bypassPermissions.')
  })
})

describe('/jarvis pc', () => {
  test('shows the guard, the desktop tool and what stand down would stop', async ($, on) => {
    const w = world(on)
    await startSession($, w)
    const text = await jarvis($, 'pc')
    expect(text).toContain('Guard: on for PowerShell, Bash, Monitor')
    expect(text).toContain('Desktop tool: offered; its actions wait for the voice helper')
    expect(text).toContain('Permission mode: not known yet')
    expect(text).toContain('/jarvis pc check <command>')
  })

  test('check names the tier and the reason per shell', async ($, on) => {
    const w = world(on)
    await startSession($, w)
    expect(await jarvis($, 'pc check rm -rf build')).toMatch(/^Bash: screen · asks for a click on screen: it deletes files \(rule [a-z-]+\)$/m)
    expect(await jarvis($, 'pc check git push')).toMatch(/^PowerShell: voice · asks for a spoken yes in a voice conversation, else a click: it pushes commits/m)
    expect(await jarvis($, 'pc check git status')).toContain("Bash: pass · runs without a question from Jarvis (Claude Code's own rules still apply)")
    expect(await jarvis($, 'pc check')).toMatch(/^Which command\?/)
  })

  test('rules prints the snippet to paste and writes nothing', async ($, on) => {
    const w = world(on)
    await startSession($, w)
    const text = await jarvis($, 'pc rules')
    expect(text).toContain('Jarvis writes no settings')
    const snippet = JSON.parse(text.slice(text.indexOf('{'))) as { permissions: Record<string, string[]>; env: Record<string, string> }
    expect(snippet.permissions.deny?.length).toBeGreaterThan(0)
    expect(snippet.permissions.allow).toBeUndefined()
    expect(snippet.permissions.ask).toBeUndefined()
    expect(snippet.env).toEqual({ CLAUDE_CODE_USE_POWERSHELL_TOOL: '1' })
    expect(w.existing.size).toBe(1) // only the venv the world started with
  })
})
