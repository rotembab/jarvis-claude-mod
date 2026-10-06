// Jarvis for Claude Code: entry module. Builds the engine port (engine.ts)
// from `$` and wires the session's events to the coordinator (app.ts).

import { atom, read, update } from 'claude-code'
import type { Register } from 'claude-code'

import { Jarvis, readSettings } from './app'
import { runJarvisCommand } from './commands'
import type { Engine } from './engine'
import { bandTree, isBandShown } from './ui'

// The session state this mod owns (types/index.d.ts declares it).
const viewAtom = atom({ plugin: 'jarvis', key: 'view' } as const, { phase: 'stopped' })
const helperRefAtom = atom({ plugin: 'jarvis', key: 'helper' } as const, null)

export const register: Register = (on, options) => {
  const app = new Jarvis(readSettings(options))

  on('session.start', async ($, e, next) => {
    const started = await next(e)
    // `$` is spelled out at each call (the engine reads calls off the source),
    // so the port is a set of closures over this hook's `$`.
    const engine: Engine = {
      pluginRoot: $.plugin.root,
      env: async () => ({
        OS: await $.env.get('OS'),
        USERPROFILE: await $.env.get('USERPROFILE'),
        LOCALAPPDATA: await $.env.get('LOCALAPPDATA'),
        HOME: await $.env.get('HOME'),
        NO_PROXY: await $.env.get('NO_PROXY'),
        CLAUDE_CODE_REMOTE: await $.env.get('CLAUDE_CODE_REMOTE'),
      }),
      now: () => $.clock.now(),
      after: (ms, fn) => $.clock.after(ms, fn),
      every: (ms, fn) => $.clock.every(ms, fn),
      fetch: (url, init) => $.http.fetch(url, init),
      spawn: request => $.process.spawn(request),
      run: (argv, init) => $.process.run(argv, init),
      exists: path => $.fs.exists(path),
      writeFile: (path, text) => $.fs.write(path, text),
      storeGet: key => $.store.get(key),
      storeSet: (key, value) => $.store.set(key, value),
      storeDelete: key => $.store.delete(key),
      readHelperRef: () => read($, helperRefAtom),
      writeHelperRef: async ref => {
        await update($, helperRefAtom, () => ref)
      },
      writeView: async view => {
        await update($, viewAtom, () => view)
      },
      status: text => $.ui.status(text),
      toast: (text, toastOptions) => $.ui.toast(text, toastOptions),
      log: text => $.ui.log(text),
      debug: text => $.ui.log(text, { to: 'debug' }),
      submitPrompt: text => $.prompt.submit({ text, asUser: true }),
      abortTurn: turnId => $.turn.abort({ turnId }),
    }
    await $.command.register({
      name: 'jarvis',
      description: 'Jarvis voice: status and help; setup, stop, talk, test, restart, voice <id>, devices',
      argumentHint: '[setup|stop|talk|test|restart|voice <id>|devices]',
      immediate: true,
    })
    await app.onSessionStart(engine, e.surface)
    return started
  })

  on('session.attach', async ($, e, next) => {
    const attached = await next(e)
    app.onAttach(e.surface)
    return attached
  })

  on('command.run', { command: 'jarvis' }, ($, e) => runJarvisCommand(app, e.args))

  on('turn.start', ($, e, next) => {
    app.voice?.onTurnStart(e)
    return next(e)
  })

  on('turn.step', async function* ($, e, next) {
    const voice = app.voice
    if (voice === undefined) return yield* next(e)
    return yield* voice.step(e, next(e))
  })

  on('turn.complete', ($, e, next) => {
    app.voice?.onTurnComplete(e)
    return next(e)
  })

  // The persona: a constant section once voice is in use, plus a per-turn
  // flag only when a voice prompt's note could not ride along with it.
  on('prompt.compose', async ($, e, next) => {
    const composed = await next(e)
    return app.voice?.compose(composed) ?? composed
  })

  // A voice prompt carries a note the persona keys on, so the system prompt
  // stays the same between typed and spoken turns (cache friendly). Fails open.
  // (Not at prompt.submit: a plugin's own hooks there skip its own prompts.)
  on('classic.UserPromptSubmit', async ($, e, next) => {
    const result = await next(e)
    return app.voice?.markPrompt(e.prompt, result) ?? result
  }).catch(($, e, next) => next(e))

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const view = await read($, viewAtom)
    if (e.props.hasSurvey || !isBandShown(view)) return next(e)
    return bandTree($.ui.resolve(e), view)
  })
}
