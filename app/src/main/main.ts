// The Jarvis app's main process: wiring only. It starts the link server,
// writes the address file the plugin reads, and shows what the plugin pushes
// in the overlay, the orb and the tray. Claude Code and the plugin stay the
// brain; when no session pushes, the app shows Jarvis offline.

import { app, BrowserWindow, dialog, globalShortcut, ipcMain, Menu, screen, session } from 'electron'
import { homedir } from 'node:os'

import { type AppView, buildView } from '../shared/view'
import { newToken, removeEndpointSync, writeEndpoint } from './discovery'
import { loginItem } from './login'
import { jarvisHome, userDataOverride } from './paths'
import { type LinkServer, startLinkServer } from './server'
import { SessionBoard } from './sessions'
import { loadSettings, placeOrb, type Point, saveSettings, type Settings } from './settings'
import { type AppTray, createTray } from './tray'
import type { TrayAction } from './tray-menu'
import { OverlayVisibility, WAKE_PHASES } from './visibility'
import { createOrbWindow, createOverlayWindow, OverlayFader, sendView } from './windows'

/** How often the app re-judges which session is live and whether the overlay shows. */
const HOUSEKEEPING_MS = 500
/** The orb's place is saved this long after it stops moving. */
const ORB_SAVE_MS = 400
const HOTKEY = 'Control+Alt+J'

const describe = (error: unknown): string => (error instanceof Error ? error.message : String(error))

// The app's own clock for when pushes arrive and how long the overlay lingers:
// it never steps back when the PC's time is set, as Date.now() can.
const clock = (): number => performance.now()

async function main(): Promise<void> {
  // Tests give each copy its own folder, and with it its own single-instance lock.
  const userDataDir = userDataOverride(process.env, process.platform)
  if (userDataDir !== undefined) app.setPath('userData', userDataDir)

  // One app per user: a second launch only asks the first to show its overlay.
  if (!app.requestSingleInstanceLock()) {
    // Most often `npm start` after a pull, so say why the new build did not start.
    console.log('Jarvis is already running, and shows its overlay now. To run a new build, quit Jarvis from the tray or the orb first.')
    app.quit()
    return
  }
  // Keeps the Start with Windows entry apart from other Electron apps run from source.
  if (process.platform === 'win32') app.setAppUserModelId('com.rotembab.jarvis')

  const version = app.getVersion()
  const home = jarvisHome(process.env, process.platform, homedir())
  const login = loginItem(process.execPath, app.getAppPath())
  const board = new SessionBoard()

  let settings: Settings | undefined
  let visibility: OverlayVisibility | undefined
  let overlay: BrowserWindow | undefined
  let fader: OverlayFader | undefined
  let orb: BrowserWindow | undefined
  let tray: AppTray | undefined
  let server: LinkServer | undefined
  let token: string | undefined
  let housekeeping: NodeJS.Timeout | undefined
  let startWithWindows: boolean | undefined
  let view: AppView = buildView(undefined, false)
  let viewKey = ''
  let wasAwake = false
  /** Where the app itself last put the orb; only a move to anywhere else is saved. */
  let orbPlacedAt: Point | undefined

  const readLoginItem = (): boolean | undefined =>
    process.platform === 'win32' ? app.getLoginItemSettings(login).openAtLogin : undefined

  function refresh(): void {
    if (visibility === undefined || overlay === undefined || fader === undefined || settings === undefined) return
    const now = clock()
    const shown = board.current(now)
    visibility.update(shown && { mode: shown.mode, phase: shown.phase }, now)
    const next = buildView(shown, visibility.isVisible)
    fader.apply(next.isOverlayShown)
    // Woken while the overlay was already up (Always, or shown by hand): a
    // topmost window activated since, such as Task Manager, may cover it.
    const isAwake = shown !== undefined && WAKE_PHASES.has(shown.phase)
    if (isAwake && !wasAwake) fader.raise()
    wasAwake = isAwake
    const key = JSON.stringify(next)
    if (key !== viewKey) {
      viewKey = key
      view = next
      sendView(overlay, view)
      if (orb !== undefined) sendView(orb, view)
    }
    tray?.update(
      {
        version,
        status: view.status,
        note: view.note,
        isOverlayShown: view.isOverlayShown,
        overlay: visibility.setting,
        showOrb: settings.showOrb,
        startWithWindows,
      },
      view.mode,
    )
  }

  function toggleOverlay(): void {
    visibility?.toggle()
    refresh()
    fader?.raise()
  }

  function save(): void {
    if (settings === undefined) return
    saveSettings(app.getPath('userData'), settings).catch(error => console.error(`Jarvis could not save its settings: ${describe(error)}`))
  }

  const workAreas = () => screen.getAllDisplays().map(display => display.workArea)

  function openOrb(): void {
    if (orb !== undefined || settings === undefined) return
    orbPlacedAt = placeOrb(settings.orb, workAreas(), screen.getPrimaryDisplay().workArea)
    const win = createOrbWindow(orbPlacedAt, () => tray?.popup(win))
    orb = win
    win.webContents.on('did-finish-load', () => sendView(win, view))
    // Only the person's drags are saved. When a screen goes away, Windows and
    // placeWindows move the orb too, and saving that would lose the place it
    // goes back to when the screen returns. 'moved' (Windows) comes once, at
    // the end of a drag. Linux has only 'move', so there a move to where the
    // app itself put the orb is skipped.
    let saveTimer: NodeJS.Timeout | undefined
    const saveSoon = (): void => {
      clearTimeout(saveTimer)
      saveTimer = setTimeout(() => {
        if (win.isDestroyed() || settings === undefined) return
        const { x, y } = win.getBounds()
        if (orbPlacedAt !== undefined && x === orbPlacedAt.x && y === orbPlacedAt.y) return
        settings.orb = { x, y }
        save()
      }, ORB_SAVE_MS)
    }
    if (process.platform === 'linux') win.on('move', saveSoon)
    else win.on('moved', saveSoon)
    win.on('closed', () => {
      clearTimeout(saveTimer)
      if (orb === win) orb = undefined
    })
  }

  function closeOrb(): void {
    const win = orb
    orb = undefined
    if (win !== undefined && !win.isDestroyed()) win.destroy()
  }

  function onAction(action: TrayAction): void {
    if (settings === undefined || visibility === undefined) return
    switch (action.kind) {
      case 'toggle-overlay':
        toggleOverlay()
        return
      case 'overlay':
        visibility.setSetting(action.value)
        settings.overlay = action.value
        save()
        refresh()
        return
      case 'show-orb':
        if (action.value) openOrb()
        else closeOrb()
        settings.showOrb = action.value
        save()
        refresh()
        return
      case 'start-with-windows':
        app.setLoginItemSettings({ ...login, openAtLogin: action.value })
        startWithWindows = readLoginItem()
        refresh()
        return
      case 'quit':
        app.quit()
        return
    }
  }

  // A second launch can fire this more than once (seen on Linux): show, never toggle.
  app.on('second-instance', () => {
    visibility?.show()
    refresh()
    fader?.raise()
  })
  // Closing a window never quits the app; only the tray's Quit does.
  app.on('window-all-closed', () => undefined)
  // The pages never navigate, open windows or embed others.
  app.on('web-contents-created', (_event, contents) => {
    contents.on('will-navigate', event => event.preventDefault())
    contents.on('will-redirect', event => event.preventDefault())
    contents.on('will-attach-webview', event => event.preventDefault())
    contents.setWindowOpenHandler(() => ({ action: 'deny' }))
  })
  let isQuitting = false
  function cleanUp(): void {
    if (token !== undefined) removeEndpointSync(home, token)
    clearInterval(housekeeping)
    globalShortcut.unregisterAll()
    void server?.close()
  }
  app.on('before-quit', () => {
    isQuitting = true
    cleanUp()
  })
  // Ctrl+C in the window `npm start` runs in, or that window closing: quit
  // properly, so the address file goes too.
  for (const signal of ['SIGINT', 'SIGTERM', 'SIGHUP', 'SIGBREAK'] as const) process.on(signal, () => app.quit())

  await app.whenReady()
  if (isQuitting) return
  Menu.setApplicationMenu(null)
  // No permission prompts, and no network: only the app's own files load.
  session.defaultSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false))
  session.defaultSession.setPermissionCheckHandler(() => false)
  session.defaultSession.webRequest.onBeforeRequest((details, callback) =>
    callback({ cancel: !details.url.startsWith('file:') && !details.url.startsWith('devtools:') }),
  )

  settings = await loadSettings(app.getPath('userData'))
  visibility = new OverlayVisibility(settings.overlay)

  try {
    const launchToken = newToken()
    server = await startLinkServer({
      token: launchToken,
      version,
      onSnapshot: snapshot => {
        if (board.accept(snapshot, clock())) refresh()
      },
    })
    await writeEndpoint(home, { v: 1, port: server.port, token: launchToken, pid: process.pid, version })
    token = launchToken
  } catch (error) {
    dialog.showErrorBox('Jarvis', `Jarvis could not open its link to Claude Code in ${home}: ${describe(error)}`)
    app.quit()
    return
  }
  // Told to quit while starting (Windows signing out, a test closing it at
  // once): a window made now would keep the app alive, so stop here.
  if (isQuitting) {
    cleanUp()
    return
  }
  console.log(`Jarvis app ${version}: the HUD link listens on 127.0.0.1:${server.port}`)

  const overlayWin = createOverlayWindow()
  overlay = overlayWin
  fader = new OverlayFader(overlayWin)
  // Windows signing out, restarting or shutting down ends the app without
  // 'before-quit'; this is the last moment to remove the address file.
  overlayWin.on('session-end', cleanUp)
  overlayWin.webContents.on('did-finish-load', () => sendView(overlayWin, view))
  if (settings.showOrb) openOrb()
  tray = createTray(onAction)
  startWithWindows = readLoginItem()

  if (!globalShortcut.register(HOTKEY, toggleOverlay)) console.log('Ctrl+Alt+J is taken by another app')

  // Only the orb's page may ask for these.
  ipcMain.on('jarvis:toggle-overlay', event => {
    if (orb !== undefined && event.sender === orb.webContents) toggleOverlay()
  })
  ipcMain.on('jarvis:orb-menu', event => {
    if (orb !== undefined && event.sender === orb.webContents) tray?.popup(orb)
  })

  // A screen plugged in or out: keep the overlay on the main screen and the orb on a screen.
  const placeWindows = (): void => {
    overlay?.setBounds(screen.getPrimaryDisplay().workArea)
    if (orb !== undefined && settings !== undefined) {
      orbPlacedAt = placeOrb(settings.orb, workAreas(), screen.getPrimaryDisplay().workArea)
      orb.setPosition(orbPlacedAt.x, orbPlacedAt.y)
    }
  }
  screen.on('display-added', placeWindows)
  screen.on('display-removed', placeWindows)
  screen.on('display-metrics-changed', placeWindows)

  housekeeping = setInterval(refresh, HOUSEKEEPING_MS)
  refresh()
}

main().catch(error => {
  console.error(`Jarvis could not start: ${describe(error)}`)
  app.exit(1)
})
