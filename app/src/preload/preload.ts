// The only bridge between a page and the app: the view to draw, and the two
// things the orb can ask for. Pages run sandboxed with context isolation, so
// they reach nothing else (no Node, no ipcRenderer).

import { contextBridge, ipcRenderer } from 'electron'

import type { AppView } from '../shared/view'

contextBridge.exposeInMainWorld('jarvis', {
  onView: (fn: (view: AppView) => void) => {
    ipcRenderer.on('jarvis:view', (_event, view: AppView) => fn(view))
  },
  toggleOverlay: () => ipcRenderer.send('jarvis:toggle-overlay'),
  openMenu: () => ipcRenderer.send('jarvis:orb-menu'),
})
