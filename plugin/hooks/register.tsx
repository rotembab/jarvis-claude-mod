// Jarvis for Claude Code: entry module. Builds the engine port (engine.ts)
// from `$` and wires the session's events to the coordinator (app.ts).

import { atom, read, update } from 'claude-code'
import type { Register } from 'claude-code'

import { DESKTOP_TOOL_SPEC, GUARD_FAILED, wantsDesktopTool } from './pc'
import type { GuardCall } from './pc'
import { Jarvis, readSettings } from './app'
import { runJarvisCommand } from './commands'
import type { Engine } from './engine'
import { actionLabel, HUD_PANE, hudLayout, hudMode, PANE_START } from './hud'
import { bandTree, foldedRow, hudSvgTree, hudTerminalTree, isBandShown } from './ui'

// The session state this mod owns (types/index.d.ts declares it).
const viewAtom = atom({ plugin: 'jarvis', key: 'view' } as const, { phase: 'stopped' })
const helperRefAtom = atom({ plugin: 'jarvis', key: 'helper' } as const, null)
const hudAtom = atom({ plugin: 'jarvis', key: 'hud' } as const, { isThinking: false, actions: [] })
const foldedAtom = atom({ plugin: 'jarvis', key: 'folded' } as const, false)

/** The HUD pane; its size follows the screen (hud.ts paneSize). */
const HUD_OPEN = { id: HUD_PANE, title: 'JARVIS', ...PANE_START }

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
        SystemRoot: await $.env.get('SystemRoot'),
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
      writeFolded: async isFolded => {
        await update($, foldedAtom, () => isFolded)
      },
      status: text => $.ui.status(text),
      toast: (text, toastOptions) => $.ui.toast(text, toastOptions),
      log: text => $.ui.log(text),
      debug: text => $.ui.log(text, { to: 'debug' }),
      openPane: size => $.ui.open({ ...HUD_OPEN, ...size }),
      closePane: () => $.ui.close({ id: HUD_PANE }),
      blit: args => $.ui.blit(args),
      invalidate: () => $.ui.invalidate('ui.render'),
      submitPrompt: text => $.prompt.submit({ text, asUser: true }),
      complete: request => $.model.complete(request),
      messages: async () => {
        const messages = await $.session.messages()
        return Array.isArray(messages) ? messages : []
      },
      contextTokens: async () => (await $.session.usage()).context.tokens,
      abortTurn: turnId => $.turn.abort({ turnId }),
      stopTask: taskId => $.tool.call({ tool: 'TaskStop', task_id: taskId }),
    }
    // Jarvis's desktop tool (pc.ts, desktop.ts): local Windows sessions only; a refusal costs nothing else.
    if (await wantsDesktopTool(engine)) {
      await $.tool.register(DESKTOP_TOOL_SPEC).catch((error: unknown) => engine.debug(`jarvis: the desktop tool is not available: ${String(error)}`))
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
  on('command.run', { command: 'jarvis' }, ($, e) => runJarvisCommand(app, e.args, { openHud: size => $.ui.open({ ...HUD_OPEN, ...size }) }))

  on('turn.start', ($, e, next) => {
    app.voice?.onTurnStart(e)
    app.hud?.onTurnStart()
    return next(e)
  })

  // Jarvis's guard (guard.ts, pc.ts): shell commands, and writes to Claude's own settings,
  // sorted into a tier before they run. Waits for a click inside this hook's own `$` call. Fails closed.
  // Registered before the HUD's tool.call hook: hooks nest in order, first outermost, so an overrun
  // lands on this hook's own catch, never on a catch-all that would run the call.
  on('tool.call', { tool: /^(Bash|PowerShell|Monitor|Write|Edit|NotebookEdit)$/ }, async ($, e, next) => {
    const call = e as unknown as GuardCall
    const held = await app.pc.guard(call, (question, options) => $.ui.ask(question, options), next.signal)
    if (held !== undefined) return { deny: held }
    const result = await next(e)
    app.pc.noteRan(call, result)
    return result
  }).catch(($, e, next) => (next.called ? next(e) : { deny: GUARD_FAILED }))

  // The desktop tool: answered here, so the engine's permission path never sees it; desktop.ts
  // applies the user's own rules for it ($.tool.check), plan mode and the tiers. Outer to the HUD's
  // hook too, so it logs its own actions there.
  on('tool.call', { tool: 'mcp__jarvis__desktop' }, ($, e, next) =>
    app.pc.desktop(
      e as unknown as Record<string, unknown>,
      {
        ask: (question, options) => $.ui.ask(question, options),
        check: input => $.tool.check({ tool: 'mcp__jarvis__desktop', input }),
      },
      next.signal,
    ),
  ).catch(($, e, next) => (next.called ? next(e) : { deny: 'The desktop action failed, so it is not known whether it was done.' }))
  on('tool.describe', { tool: 'mcp__jarvis__desktop' }, async ($, e, next) => ({ ...(await next(e)), isDeferred: false }))

  // Every question dialog (Jarvis's, another plugin's, Claude's own): while one is open a spoken yes
  // or no counts for nothing. Jarvis's own questions (`$.ui.ask` above) give only the label chosen:
  // the dialog's record says when it closed by itself while the user was away. Read, never changed.
  on('tool.call', { tool: 'AskUserQuestion' }, async ($, e, next) => {
    app.pc.dialogOpened()
    try {
      const result = await next(e)
      app.pc.noteDialog(e.questions, result)
      return result
    } finally {
      app.pc.dialogClosed()
    }
  }).catch(($, e, next) => next(e))

  on('turn.step', async function* ($, e, next) {
    const voice = app.voice
    if (voice === undefined) return yield* next(e)
    return yield* voice.step(e, input => next(input))
  })

  on('turn.complete', ($, e, next) => {
    app.voice?.onTurnComplete(e)
    if (e.agentId === undefined) void app.onTurnComplete()
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

  // No bypass mode (PLAN.md:149): one warning when the session starts with permission checks skipped.
  on('classic.SessionStart', async ($, e, next) => {
    const result = await next(e)
    const warning = app.pc.onPermissionMode(e.permission_mode)
    if (warning !== undefined) $.ui.toast(warning, { timeoutMs: 10_000 })
    return result
  }).catch(($, e, next) => next(e))

  // The permission mode, for the desktop tool's plan-mode hold: each prompt's, and a change
  // mid-turn (Shift+Tab, EnterPlanMode, ExitPlanMode) that the next tool's PostToolUse reports.
  // Matched (on a field every input has) so another hook of the same event may stand beside them.
  on('classic.UserPromptSubmit', { hook_event_name: 'UserPromptSubmit' }, ($, e, next) => {
    app.pc.notePermissionMode(e.permission_mode, e.agent_id)
    return next(e)
  }).catch(($, e, next) => next(e))
  on('classic.PostToolUse', { hook_event_name: 'PostToolUse' }, ($, e, next) => {
    app.pc.noteToolUse(e.tool_name, e.permission_mode, e.agent_id)
    return next(e)
  }).catch(($, e, next) => next(e))

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
    const isFocus = hud.isFocus === true
    const data = { mode: hudMode(view.phase, hud.isThinking), view, hud, isFocus }
    if (e.surface === 'terminal') {
      // The pane asks for the room the screen spares (more in focus mode).
      if (e.viewport !== undefined) {
        const { placement, bodyColumns, scroll } = e.props
        app.hud?.onLayout({ placement, bodyColumns, bodyRows: scroll.bodyRows, columns: e.viewport.columns, rows: e.viewport.rows })
      }
      const { Box, Text, Raster } = $.ui.resolve(e)
      const layout = hudLayout(e.props.bodyColumns, e.props.scroll.bodyRows, view.lastUtterance !== undefined, isFocus)
      const cells = app.hud?.mountTerminal(HUD_PANE, layout.ring.columns, layout.ring.rows) ?? ''
      return hudTerminalTree({ Box, Text, Raster }, data, layout, cells)
    }
    const { Box, Text, Svg } = $.ui.resolve(e)
    app.hud?.mountDesktop()
    return hudSvgTree({ Box, Text, Svg }, data, app.hud?.levels() ?? { mic: 0, out: 0, t: 0 }, e.props.bodyColumns)
  })

  on('ui.close', ($, e, next) => {
    if (e.id === HUD_PANE) app.hud?.onClosed()
    return next(e)
  }).catch(($, e, next) => next(e))

  // Typing at the prompt brings the conversation back from focus mode.
  on('prompt.submit', ($, e, next) => {
    if (e.origin.kind === 'composer') app.onTyped()
    return next(e)
  }).catch(($, e, next) => next(e))

  // Focus mode folds the conversation away: its rows draw nothing while it
  // shows. Questions for you and command output are left alone, and
  // permission dialogs are Claude Code's own. ctrl+o's detailed transcript
  // draws the prompts expanded (the first row of it is one): while it shows,
  // nothing is folded, so the whole conversation is there.
  let isDetailed = false
  on('ui.render', { component: 'UserMessage' }, async ($, e, next) => {
    isDetailed = e.props.isExpanded
    return !isDetailed && (await read($, foldedAtom)) ? foldedRow($.ui.resolve(e)) : next(e)
  })
  on('ui.render', { component: 'AssistantMessage' }, async ($, e, next) => (!isDetailed && (await read($, foldedAtom)) ? foldedRow($.ui.resolve(e)) : next(e)))
  on('ui.render', { component: 'ToolUse' }, async ($, e, next) => (!isDetailed && (await read($, foldedAtom)) ? foldedRow($.ui.resolve(e)) : next(e)))
  on('ui.render', { component: 'ToolResult' }, async ($, e, next) => (!isDetailed && (await read($, foldedAtom)) ? foldedRow($.ui.resolve(e)) : next(e)))
  on('ui.render', { component: 'ToolGroup' }, async ($, e, next) => (!isDetailed && (await read($, foldedAtom)) ? foldedRow($.ui.resolve(e)) : next(e)))
  on('ui.render', { component: 'ToolProgress' }, async ($, e, next) => (!isDetailed && (await read($, foldedAtom)) ? foldedRow($.ui.resolve(e)) : next(e)))
  on('ui.render', { component: 'Spinner' }, async ($, e, next) => (!isDetailed && (await read($, foldedAtom)) ? foldedRow($.ui.resolve(e)) : next(e)))
  on('ui.render', { component: 'TurnDuration' }, async ($, e, next) => (!isDetailed && (await read($, foldedAtom)) ? foldedRow($.ui.resolve(e)) : next(e)))
  on('ui.render', { component: 'InfoNotice' }, async ($, e, next) => (!isDetailed && (await read($, foldedAtom)) ? foldedRow($.ui.resolve(e)) : next(e)))

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const view = await read($, viewAtom)
    if (e.props.hasSurvey || !isBandShown(view)) return next(e)
    return bandTree($.ui.resolve(e), view)
  })
}
