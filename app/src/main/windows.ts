// The two windows. The overlay covers the main screen's work area, above
// everything, and every click passes through it; it never takes the focus,
// so it never steals typing from Claude Code. The orb is a small round
// window that stays on top: drag it by its edge, click its centre. Both pages
// run sandboxed, with only the preload's three functions to reach the app.

import { BrowserWindow, type BrowserWindowConstructorOptions, screen } from 'electron'
import { join } from 'node:path'

import type { AppView } from '../shared/view'
import { ORB_SIZE, type Point } from './settings'

/** The pages fade the overlay in and out over this long (style.css). */
export const FADE_MS = 300

// In the bundle __dirname is dist/, beside renderer/ and preload.js.
const pagePath = (name: 'overlay' | 'orb'): string => join(__dirname, 'renderer', `${name}.html`)
const preloadPath = join(__dirname, 'preload.js')

/** What both windows share: frameless, see-through, on top, never focused, out of the taskbar. */
function baseOptions(): BrowserWindowConstructorOptions {
  return {
    show: false,
    frame: false,
    transparent: true,
    backgroundColor: '#00000000',
    resizable: false,
    minimizable: false,
    maximizable: false,
    fullscreenable: false,
    focusable: false,
    skipTaskbar: true,
    alwaysOnTop: true,
    hasShadow: false,
    thickFrame: false,
    // A toolbar window stays out of Alt+Tab on Windows.
    ...(process.platform === 'win32' ? { type: 'toolbar' } : {}),
    webPreferences: {
      preload: preloadPath,
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
      spellcheck: false,
      // The ring keeps moving while the window is in the background.
      backgroundThrottling: false,
    },
  }
}

export function createOverlayWindow(): BrowserWindow {
  // The work area, not the whole screen: a topmost window the size of the
  // screen can make Windows treat it as a full-screen app.
  const win = new BrowserWindow({ ...baseOptions(), ...screen.getPrimaryDisplay().workArea, movable: false })
  // Nothing on the overlay is clickable, so the mouse goes straight through
  // (no forwarding: that would hook every mouse move for no use).
  win.setIgnoreMouseEvents(true)
  win.setAlwaysOnTop(true, 'screen-saver')
  void win.loadFile(pagePath('overlay'))
  return win
}

export function createOrbWindow(position: Point, onMenu: () => void): BrowserWindow {
  const win = new BrowserWindow({ ...baseOptions(), width: ORB_SIZE, height: ORB_SIZE, x: position.x, y: position.y })
  win.setAlwaysOnTop(true, 'screen-saver')
  // A right-click on the drag edge raises Windows' own window menu; show ours instead.
  win.on('system-context-menu', event => {
    event.preventDefault()
    onMenu()
  })
  win.once('ready-to-show', () => win.showInactive())
  void win.loadFile(pagePath('orb'))
  return win
}

/**
 * Shows and hides the overlay window around the page's fade: shown before
 * the page fades in, hidden once it has faded out, so a hidden overlay costs
 * nothing to draw.
 */
export class OverlayFader {
  private hideTimer: NodeJS.Timeout | undefined

  constructor(private readonly win: BrowserWindow) {}

  apply(visible: boolean): void {
    if (this.win.isDestroyed()) return
    if (visible) {
      clearTimeout(this.hideTimer)
      this.hideTimer = undefined
      if (!this.win.isVisible()) {
        this.win.showInactive()
        // Showing again can drop the level on Windows; set it each time.
        this.win.setAlwaysOnTop(true, 'screen-saver')
      }
    } else if (this.win.isVisible() && this.hideTimer === undefined) {
      this.hideTimer = setTimeout(() => {
        this.hideTimer = undefined
        if (!this.win.isDestroyed()) this.win.hide()
      }, FADE_MS + 50)
    }
  }
}

export function sendView(win: BrowserWindow, view: AppView): void {
  if (!win.isDestroyed()) win.webContents.send('jarvis:view', view)
}
