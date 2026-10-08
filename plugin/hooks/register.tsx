// Jarvis for Claude Code: entry module. Builds the engine port (engine.ts)
// from `$` and wires the session's events to the coordinator (app.ts).

import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import { DESKTOP_TOOL, DESKTOP_TOOL_SPEC, GUARD_FAILED, wantsDesktopTool } from './pc'
import type { AskPorts, GuardCall } from './pc'
import { Jarvis, readSettings } from './app'
import { runJarvisCommand } from './commands'
import type { Engine } from './engine'
import { describeError } from './engine'
import type { HandsEngine } from './hands'
import { HANDS_TOOL } from './hands'
import { callHandsTool, HANDS_TOOL_ID } from './hands-gate'
import { HOME_TOOL, HOME_TOOL_SPEC } from './home'
import { actionLabel, HUD_PANE, hudLayout, hudMode, logOwnTool, PANE_START } from './hud'
import { isRemoteSession } from './platform'
import { bandTree, foldedRow, hudSvgTree, hudTerminalTree, isBandShown } from './ui'

// The session state this mod owns (types/index.d.ts declares it).
const viewAtom = atom({ plugin: 'jarvis', key: 'view' } as const, { phase: 'stopped' })
const helperRefAtom = atom({ plugin: 'jarvis', key: 'helper' } as const, null)
const handsRefAtom = atom({ plugin: 'jarvis', key: 'handsHelper' } as const, null)
const hudAtom = atom({ plugin: 'jarvis', key: 'hud' } as const, { isThinking: false, actions: [] })
const foldedAtom = atom({ plugin: 'jarvis', key: 'folded' } as const, false)

/** The HUD pane; its size follows the screen (hud.ts paneSize). */
const HUD_OPEN = { id: HUD_PANE, title: 'JARVIS', ...PANE_START }
/** How many folders up the guard looks for one that is there when it places a new file. */
const REAL_PATH_DEPTH = 64

/**
 * A tool this plugin answers itself (the desktop, hands and home control tools), through its
 * hook's own `$`: the question dialog, the engine's verdict on the user's
 * rules for `tool`, and each settings source as loaded (only the hooks'
 * matchers are read there, and nothing of it is logged).
 */
function ownToolPorts($: EngineInterface, tool: string): AskPorts {
  return {
    ask: (question, options) => $.ui.ask(question, options),
    check: input => $.tool.check({ tool, input }),
    settings: async () => [
      await $.settings.read({ source: 'user' }),
      await $.settings.read({ source: 'project' }),
      await $.settings.read({ source: 'local' }),
      await $.settings.read({ source: 'flag' }),
      await $.settings.read({ source: 'policy' }),
    ],
  }
}

export const register: Register = (on, options) => {
  const app = new Jarvis(readSettings(options))

  on('session.start', async ($, e, next) => {
    const started = await next(e)
    // `$` is spelled out at each call (the engine reads calls off the source),
    // so the port is a set of closures over this hook's `$`.
    const engine: HandsEngine = {
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
      readFile: path => $.fs.read(path),
      writeFile: (path, text) => $.fs.write(path, text),
      readFileText: path => $.fs.read(path).catch(() => null),
      realPath: async path => {
        // Where a file tool's path lands, every link and junction followed, so the guard judges the
        // real file (a link or an 8.3 short name can hide a protected one). The file itself when it
        // stats; else the nearest folder above that does (a Write may name a file, and folders, not
        // there yet) plus the rest. Null when it cannot be placed: a spelling with no folder to stand
        // on, a rest with `.` or `..` in it, or a link there that leads nowhere (a write through it
        // would make its target).
        let cut = Math.max(path.lastIndexOf('/'), path.lastIndexOf('\\'))
        const name = path.slice(cut + 1)
        const placeable = !/^[A-Za-z]:(?![\\/])/.test(path) && !/^[\\/][\\/]/.test(path) && !/^[A-Za-z]:/.test(name) && name !== '' && name !== '.' && name !== '..'
        if (!placeable) return null
        const own = await $.fs.stat(path, { resolve: true }).catch(() => undefined)
        if (own !== undefined) return own.realPath ?? null
        for (let depth = 0; depth < REAL_PATH_DEPTH; depth++) {
          const folder = cut < 0 ? '.' : path.slice(0, cut + 1)
          const rest = path.slice(cut + 1).split(/[\\/]/)
          if (rest.some(part => part === '.' || part === '..')) return null
          const dir = await $.fs.stat(folder, { resolve: true }).catch(() => undefined)
          if (dir !== undefined) {
            if (dir.realPath === undefined) return null
            const first = (rest[0] ?? '').toLowerCase()
            const entries = await $.fs.list(folder).catch(() => undefined)
            if (entries === undefined || entries.some(entry => entry.isLink && entry.name.toLowerCase() === first)) return null
            const sep = dir.realPath.includes('\\') ? '\\' : '/'
            return `${dir.realPath.replace(/[\\/]$/, '')}${sep}${rest.join(sep)}`
          }
          if (cut <= 0) return null
          cut = Math.max(path.lastIndexOf('/', cut - 1), path.lastIndexOf('\\', cut - 1))
          if (cut < 0) return null
        }
        return null
      },
      storeGet: key => $.store.get(key),
      storeSet: (key, value) => $.store.set(key, value),
      storeDelete: key => $.store.delete(key),
      readHelperRef: () => read($, helperRefAtom),
      writeHelperRef: async ref => {
        await update($, helperRefAtom, () => ref)
      },
      readHandsRef: () => read($, handsRefAtom),
      writeHandsRef: async ref => {
        await update($, handsRefAtom, () => ref)
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
      ask: (question, askOptions) => $.ui.ask(question, askOptions),
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
      description: 'Jarvis voice, hand, home and PC control: status and help; setup, stop, talk, test, restart, voice <id>, routing, devices, hud, hands, home, pc',
      argumentHint: '[setup|stop|talk|test|restart|voice <id>|routing|devices|hud|hands|home|pc]',
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
    // "Jarvis, turn on hand control": only where the hand helper can run.
    if (app.isLocal) {
      await $.tool.register(HANDS_TOOL).catch((error: unknown) => {
        engine.debug(`jarvis: the hands tool was not registered: ${describeError(error)}`)
      })
    }
    return started
  })

  // The engine runs no permission check, schema check, plan-mode block or settings hook for a
  // tool its plugin answers: HomeControl does all of that itself, applying the user's own rules for
  // it ($.tool.check) as the desktop and hands tools do. A failure here refuses the call rather than
  // falling through to the engine. The call's signal goes along: a call abandoned while its dialog
  // is open (Esc, a spoken stop) confirms nothing, whatever is clicked later.
  on('tool.call', { tool: 'mcp__jarvis__home_control' }, ($, e, next) => app.home.tool(e, ownToolPorts($, HOME_TOOL), next.signal)).catch(
    ($, e, next) => (next.called ? next(e) : { deny: `home_control failed: ${next.error.message ?? next.error.kind}` }),
  )

  // Kept in the prompt rather than behind ToolSearch, so a spoken request
  // needs no extra round trip to find it.
  on('tool.describe', { tool: 'mcp__jarvis__home_control' }, async ($, e, next) => ({ ...(await next(e)), isDeferred: false }))

  on('session.attach', async ($, e, next) => {
    const attached = await next(e)
    app.onAttach(e.surface)
    return attached
  })

  // A /clear (no session.start follows) or any other end: no command stays held for a spoken yes.
  on('session.end', ($, e, next) => {
    app.pc.clearHold()
    return next(e)
  }).catch(($, e, next) => next(e))

  // The command opens the HUD through its own `$`: the person asked, so it is placed at any width.
  on('command.run', { command: 'jarvis' }, ($, e) => runJarvisCommand(app, e.args, { openHud: size => $.ui.open({ ...HUD_OPEN, ...size }) }))

  // The hands tool: answered here like the desktop tool, so hands-gate.ts applies the user's own
  // rules for it ($.tool.check), asks when a settings hook could match it, and keeps plan mode read-only.
  // On the HUD's action log as its own hook would put it (that hook sits beneath and never sees the call).
  on('tool.call', { tool: 'mcp__jarvis__hands' }, ($, e, next) =>
    logOwnTool(app.hud, actionLabel({ ...e, tool: HANDS_TOOL_ID }), () => callHandsTool(app, e, ownToolPorts($, HANDS_TOOL_ID), next.signal)),
  ).catch(() => ({
    result: 'Hand control did not answer in time; /jarvis hands shows its state.',
  }))

  on('turn.start', ($, e, next) => {
    app.pc.onTurnStart(e)
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

  // The desktop tool: answered here, so the engine's permission path and its settings hooks never
  // see it; desktop.ts applies the user's own rules for it ($.tool.check), asks when a settings hook
  // could match it (each source read on its own: only the hooks' matchers are looked at, and nothing
  // of it is logged), plan mode and the tiers. Outer to the HUD's hook too, so it logs its own actions there.
  on('tool.call', { tool: 'mcp__jarvis__desktop' }, ($, e, next) =>
    app.pc.desktop(e as unknown as Record<string, unknown>, ownToolPorts($, DESKTOP_TOOL), next.signal),
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
    // The turn that held a command for a spoken yes ends with Jarvis's own question about it.
    const closingLine = app.pc.onTurnComplete(e)
    app.voice?.onTurnComplete(e, closingLine)
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

  // The permission mode, for the plan-mode hold of the desktop, hands and home control tools: each
  // prompt's, and a change mid-turn (Shift+Tab, EnterPlanMode, ExitPlanMode) that the next tool's
  // PostToolUse reports. One tracker for all three, so they agree on the turn's mode.
  // Matched (on a field every input has) so another hook of the same event may stand beside them.
  on('classic.UserPromptSubmit', { hook_event_name: 'UserPromptSubmit' }, ($, e, next) => {
    app.pc.notePermissionMode(e.permission_mode, e.prompt, e.agent_id)
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

  // Typing at the prompt brings the conversation back from focus mode. Any prompt but
  // Jarvis's own also drops a command held for a spoken yes: the yes answers only the question just asked.
  on('prompt.submit', ($, e, next) => {
    app.pc.onPrompt(e.origin)
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
