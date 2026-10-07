// The tray icon: a ring in the colour of Jarvis's mode, a click toggles the
// overlay, a right-click opens the menu (tray-menu.ts). The orb pops the same
// menu up. The icon and menu are rebuilt only when they change, because the
// app refreshes twice a second.

import { type BrowserWindow, Menu, type MenuItemConstructorOptions, nativeImage, type NativeImage, Tray } from 'electron'

import type { HudMode } from '../../../plugin/hooks/hud-ring'
import { MODE_COLORS } from '../shared/view'
import { ringPng } from './tray-icon'
import { type TrayAction, type TrayItem, trayMenu, type TrayMenuState } from './tray-menu'

export type AppTray = {
  update(state: TrayMenuState, mode: HudMode): void
  popup(window?: BrowserWindow): void
  destroy(): void
}

function icon(mode: HudMode): NativeImage {
  const image = nativeImage.createEmpty()
  image.addRepresentation({ scaleFactor: 1, buffer: ringPng(16, MODE_COLORS[mode]) })
  image.addRepresentation({ scaleFactor: 2, buffer: ringPng(32, MODE_COLORS[mode]) })
  return image
}

function template(items: readonly TrayItem[], onAction: (action: TrayAction) => void): MenuItemConstructorOptions[] {
  return items.map(item => {
    if (item.type === 'submenu') return { label: item.label, submenu: template(item.submenu ?? [], onAction) }
    const action = item.action
    return {
      type: item.type,
      label: item.label,
      enabled: item.enabled,
      checked: item.checked,
      accelerator: item.accelerator,
      // Shown as a hint only; the global shortcut is main.ts's.
      registerAccelerator: false,
      click: action === undefined ? undefined : () => onAction(action),
    }
  })
}

export function createTray(onAction: (action: TrayAction) => void): AppTray {
  let mode: HudMode = 'offline'
  let menuKey = ''
  let menu: Menu | undefined
  const tray = new Tray(icon(mode))
  tray.setToolTip('Jarvis')
  tray.on('click', () => onAction({ kind: 'toggle-overlay' }))

  return {
    update(state, nextMode) {
      if (nextMode !== mode) {
        mode = nextMode
        tray.setImage(icon(mode))
      }
      const key = JSON.stringify(state)
      if (key === menuKey) return
      menuKey = key
      menu = Menu.buildFromTemplate(template(trayMenu(state), onAction))
      tray.setContextMenu(menu)
      tray.setToolTip(`Jarvis: ${state.status}`)
    },
    popup(window) {
      menu?.popup(window === undefined ? {} : { window })
    },
    destroy() {
      tray.destroy()
    },
  }
}
