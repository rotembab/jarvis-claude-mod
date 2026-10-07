// The Jarvis app's main process: wiring only. It starts the link server,
// writes the address file the plugin reads, and shows what the plugin pushes
// in the overlay, the orb and the tray. Claude Code and the plugin stay the
// brain; when no session pushes, the app shows Jarvis offline.

import { app, BrowserWindow, dialog, globalShortcut, ipcMain, Menu, screen, session } from 'electron'
import { homedir } from 'node:os'

import { type AppView, buildView } from '../shared/view'
import { newToken, removeEndpointSync, writeEndpoint } from './discovery'
import { jarvisHome, userDataOverride } from './paths'
import { type LinkServer, startLinkServer } from './server'
import { SessionBoard } from './sessions'
import { loadSettings, placeOrb, saveSettings, type Settings } from './settings'
import { type AppTray, createTray } from './tray'
import type { TrayAction } from './tray-menu'
import { OverlayVisibility } from './visibility'
import { createOrbWindow, createOverlayWindow, OverlayFader, sendView } from './windows'

/** How often the app re-judges which session is live and whether the overlay shows. */
const HOUSEKEEPING_MS = 500
/** The orb's place is saved this long after it stops moving. */
const ORB_SAVE_MS = 400
const HOTKEY = 'Control+Alt+J'

const describe = (error: unknown): string => (error instanceof Error ? error.message : String(error))

async function main(): Promise<void> {
  // Tests give each copy its own folder, and with it its own single-instance lock.
  const userDataDir = userDataOverride(process.env, process.platform)
  if (userDataDir !== undefined) app.setPath('userData', userDataDir)

  // One app per user: a second launch only asks the first to show its overlay.
  if (!app.requestSingleInstanceLock()) {
    app.quit()
    return
  }
  // Keeps the Start with Windows entry apart from other Electron apps run from source.
  if (process.platform === 'win32') app.setAppUserModelId('com.rotembab.jarvis')

  const version = app.getVersion()
  const home = jarvisHome(process.env, process.platform, homedir())
  const loginArgs = [`"${app.getAppPath()}"`]
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

  const readLoginItem = (): boolean | undefined =>
    process.platform === 'win32' ? app.getLoginItemSettings({ path: process.execPath, args: loginArgs }).openAtLogin : undefined

  function refresh(): void {
    if (visibility === undefined || overlay === undefined || fader === undefined || settings === undefined) return
    const now = Date.now()
    const shown = board.current(now)
    visibility.update(shown && { mode: shown.mode, phase: shown.phase }, now)
    const next = buildView(shown, visibility.isVisible)
    fader.apply(next.isOverlayShown)
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
  }

  function save(): void {
    if (settings === undefined) return
    saveSettings(app.getPath('userData'), settings).catch(error => console.error(`Jarvis could not save its settings: ${describe(error)}`))
  }

  const workAreas = () => screen.getAllDisplays().map(display => display.workArea)

  function openOrb(): void {
    if (orb !== undefined || settings === undefined) return
    const win = createOrbWindow(placeOrb(settings.orb, workAreas(), screen.getPrimaryDisplay().workArea), () => tray?.popup(win))
    orb = win
    win.webContents.on('did-finish-load', () => sendView(win, view))
    let saveTimer: NodeJS.Timeout | undefined
    win.on('move', () => {
      clearTimeout(saveTimer)
      saveTimer = setTimeout(() => {
        if (win.isDestroyed() || settings === undefined) return
        const { x, y } = win.getBounds()
        settings.orb = { x, y }
        save()
      }, ORB_SAVE_MS)
    })
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
        app.setLoginItemSettings({ openAtLogin: action.value, path: process.execPath, args: loginArgs })
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
        if (board.accept(snapshot, Date.now())) refresh()
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
      const { x, y } = placeOrb(settings.orb, workAreas(), screen.getPrimaryDisplay().workArea)
      orb.setPosition(x, y)
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
