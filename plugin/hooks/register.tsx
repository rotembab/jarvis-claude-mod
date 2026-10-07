// Jarvis for Claude Code: entry module. Builds the engine port (engine.ts)
// from `$` and wires the session's events to the coordinator (app.ts).

import { atom, read, update } from 'claude-code'
import type { Register } from 'claude-code'

import { Jarvis, readSettings } from './app'
import { runJarvisCommand } from './commands'
import type { Engine } from './engine'
import { actionLabel, HUD_PANE, hudMode, ringSize } from './hud'
import { bandTree, HUD_TEXT_ROWS, hudSvgTree, hudTerminalTree, isBandShown } from './ui'

// The session state this mod owns (types/index.d.ts declares it).
const viewAtom = atom({ plugin: 'jarvis', key: 'view' } as const, { phase: 'stopped' })
const helperRefAtom = atom({ plugin: 'jarvis', key: 'helper' } as const, null)
const hudAtom = atom({ plugin: 'jarvis', key: 'hud' } as const, { isThinking: false, actions: [] })

/** The HUD pane: about as tall as the ring and its log, as wide when docked. */
const HUD_OPEN = { id: HUD_PANE, title: 'JARVIS', rows: 24, columns: 52 }

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
      writeHud: async hud => {
        await update($, hudAtom, () => hud)
      },
      status: text => $.ui.status(text),
      toast: (text, toastOptions) => $.ui.toast(text, toastOptions),
      log: text => $.ui.log(text),
      debug: text => $.ui.log(text, { to: 'debug' }),
      openPane: () => $.ui.open(HUD_OPEN),
      closePane: () => $.ui.close({ id: HUD_PANE }),
      blit: args => $.ui.blit(args),
      invalidate: () => $.ui.invalidate('ui.render'),
      submitPrompt: text => $.prompt.submit({ text, asUser: true }),
      complete: request => $.model.complete(request),
      contextTokens: async () => (await $.session.usage()).context.tokens,
      abortTurn: turnId => $.turn.abort({ turnId }),
    }
    await $.command.register({
      name: 'jarvis',
      description: 'Jarvis voice: status and help; setup, stop, talk, test, restart, voice <id>, routing, devices, hud',
      argumentHint: '[setup|stop|talk|test|restart|voice <id>|routing|devices|hud]',
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

  // The command opens the HUD through its own `$`: the person asked, so it is placed at any width.
  on('command.run', { command: 'jarvis' }, ($, e) => runJarvisCommand(app, e.args, { openHud: () => $.ui.open(HUD_OPEN) }))

  on('turn.start', ($, e, next) => {
    app.voice?.onTurnStart(e)
    app.hud?.onTurnStart()
    return next(e)
  })

  on('turn.step', async function* ($, e, next) {
    const voice = app.voice
    if (voice === undefined) return yield* next(e)
    return yield* voice.step(e, input => next(input))
  })

  on('turn.complete', ($, e, next) => {
    app.voice?.onTurnComplete(e)
    if (e.agentId === undefined) app.hud?.onTurnComplete()
    return next(e)
  })

  // The HUD's action log: each tool call, and how it ended.
  on('tool.call', async ($, e, next) => {
    const hud = app.hud
    if (hud === undefined) return next(e)
    const id = hud.onToolStart(actionLabel(e as unknown as { tool: string } & Record<string, unknown>))
    let isOk = false
    try {
      const result = await next(e)
      isOk = result.deny === undefined && result.isError !== true
      return result
    } finally {
      hud.onToolEnd(id, isOk)
    }
  }).catch(($, e, next) => next(e)) // never in the way of a tool: next is replay-safe here

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

  on('ui.render', { component: 'Pane', requestId: HUD_PANE }, async ($, e) => {
    const view = await read($, viewAtom)
    const hud = await read($, hudAtom)
    const data = { mode: hudMode(view.phase, hud.isThinking), view, hud }
    if (e.surface === 'terminal') {
      const { Box, Text, Raster } = $.ui.resolve(e)
      const bodyRows = e.props.scroll.bodyRows
      const ring = ringSize(e.props.bodyColumns, bodyRows, 1 + HUD_TEXT_ROWS)
      const cells = app.hud?.mountTerminal(HUD_PANE, ring.columns, ring.rows) ?? ''
      const actionRows = bodyRows - 1 - ring.rows - (view.lastUtterance ? 1 : 0)
      return hudTerminalTree({ Box, Text, Raster }, data, { ...ring, cells }, actionRows)
    }
    const { Box, Text, Svg } = $.ui.resolve(e)
    app.hud?.mountDesktop()
    return hudSvgTree({ Box, Text, Svg }, data, app.hud?.levels() ?? { mic: 0, out: 0, t: 0 })
  })

  on('ui.close', ($, e, next) => {
    if (e.id === HUD_PANE) app.hud?.onClosed()
    return next(e)
  }).catch(($, e, next) => next(e))

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const view = await read($, viewAtom)
    if (e.props.hasSurvey || !isBandShown(view)) return next(e)
    return bandTree($.ui.resolve(e), view)
  })
}
