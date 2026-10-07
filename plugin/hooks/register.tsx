// Jarvis for Claude Code: entry module. Builds the engine port (engine.ts)
// from `$` and wires the session's events to the coordinator (app.ts).

import { atom, read, update } from 'claude-code'
import type { Register } from 'claude-code'

import { Jarvis, readSettings } from './app'
import { runJarvisCommand } from './commands'
import type { Engine } from './engine'
import { describeError } from './engine'
import { HOME_TOOL, HOME_TOOL_SPEC } from './home'
import { isRemoteSession } from './platform'
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
      ask: (question, askOptions) => $.ui.ask(question, askOptions),
      submitPrompt: text => $.prompt.submit({ text, asUser: true }),
      complete: request => $.model.complete(request),
      contextTokens: async () => (await $.session.usage()).context.tokens,
      abortTurn: turnId => $.turn.abort({ turnId }),
    }
    await $.command.register({
      name: 'jarvis',
      description: 'Jarvis voice: status and help; setup, stop, talk, test, restart, voice <id>, routing, devices, home',
      argumentHint: '[setup|stop|talk|test|restart|voice <id>|routing|devices|home]',
      immediate: true,
    })
    // The home_control tool. A refused registration (a managed policy, a
    // host that cannot add tools) costs nothing else, and a failing start of
    // the rest does not cost the tool. A cloud session has no helper and no
    // home network, so no tool there.
    const registerHomeTool = async (): Promise<void> => {
      try {
        if (isRemoteSession({ CLAUDE_CODE_REMOTE: await $.env.get('CLAUDE_CODE_REMOTE') })) return
        const { tool } = await $.tool.register(HOME_TOOL_SPEC)
        if (tool !== HOME_TOOL) $.ui.log(`jarvis: home_control registered as ${tool}, but its hooks serve ${HOME_TOOL}`, { to: 'debug' })
      } catch (error) {
        $.ui.log(`jarvis: the home_control tool is not available in this session: ${describeError(error)}`, { to: 'debug' })
      }
    }
    try {
      await app.onSessionStart(engine, e.surface)
    } finally {
      await registerHomeTool()
    }
    return started
  })

  // The engine runs no permission check, schema check or plan-mode block for a
  // tool its plugin answers: HomeControl does all of that itself. A failure
  // here refuses the call rather than falling through to the engine.
  on('tool.call', { tool: 'mcp__jarvis__home_control' }, ($, e) => app.home.tool(e)).catch(($, e, next) =>
    next.called ? next(e) : { deny: `home_control failed: ${next.error.message ?? next.error.kind}` },
  )

  // Kept in the prompt rather than behind ToolSearch, so a spoken request
  // needs no extra round trip to find it.
  on('tool.describe', { tool: 'mcp__jarvis__home_control' }, async ($, e, next) => ({ ...(await next(e)), isDeferred: false }))

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
    return yield* voice.step(e, input => next(input))
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
  // It also records the permission mode the turn runs in: home control keeps plan mode read-only.
  on('classic.UserPromptSubmit', async ($, e, next) => {
    app.home.notePermissionMode(e.permission_mode)
    const result = await next(e)
    return app.voice?.markPrompt(e.prompt, result) ?? result
  }).catch(($, e, next) => next(e))

  // Plan mode can also begin or end mid-turn, through these two tools.
  on('classic.PostToolUse', { tool_name: ['EnterPlanMode', 'ExitPlanMode'] }, ($, e, next) => {
    app.home.notePlanTool(e.tool_name, e.permission_mode)
    return next(e)
  }).catch(($, e, next) => next(e))

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const view = await read($, viewAtom)
    if (e.props.hasSurvey || !isBandShown(view)) return next(e)
    return bandTree($.ui.resolve(e), view)
  })
}
