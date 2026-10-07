// The tray menu as plain data, so it is tested without Electron; tray.ts
// turns it into a native menu and runs the actions through main.ts.

import type { OverlaySetting } from './visibility'

export type TrayAction =
  | { kind: 'toggle-overlay' }
  | { kind: 'overlay'; value: OverlaySetting }
  | { kind: 'show-orb'; value: boolean }
  | { kind: 'start-with-windows'; value: boolean }
  | { kind: 'quit' }

export type TrayItem = {
  type: 'normal' | 'separator' | 'checkbox' | 'radio' | 'submenu'
  label?: string
  enabled?: boolean
  checked?: boolean
  accelerator?: string
  action?: TrayAction
  submenu?: TrayItem[]
}

export type TrayMenuState = {
  version: string
  status: string
  /** What Jarvis needs from you, if anything (view.ts noteFor); shown under the status. */
  note: string
  isOverlayShown: boolean
  overlay: OverlaySetting
  showOrb: boolean
  /** undefined where the app cannot start with the OS (anywhere but Windows for now). */
  startWithWindows: boolean | undefined
}

const OVERLAY_LABELS: Record<OverlaySetting, string> = {
  auto: 'When you talk to Jarvis',
  always: 'Always',
  off: 'Never',
}

export function trayMenu(state: TrayMenuState): TrayItem[] {
  const items: TrayItem[] = [
    { type: 'normal', label: `Jarvis ${state.version}: ${state.status}`, enabled: false },
    ...(state.note === '' ? [] : [{ type: 'normal', label: state.note, enabled: false } as const]),
    { type: 'separator' },
    {
      type: 'normal',
      label: state.isOverlayShown ? 'Hide the overlay' : 'Show the overlay',
      accelerator: 'Control+Alt+J',
      action: { kind: 'toggle-overlay' },
    },
    {
      type: 'submenu',
      label: 'Overlay',
      submenu: (Object.keys(OVERLAY_LABELS) as OverlaySetting[]).map(value => ({
        type: 'radio',
        label: OVERLAY_LABELS[value],
        checked: state.overlay === value,
        action: { kind: 'overlay', value },
      })),
    },
    { type: 'checkbox', label: 'Show the orb', checked: state.showOrb, action: { kind: 'show-orb', value: !state.showOrb } },
  ]
  if (state.startWithWindows !== undefined) {
    items.push({
      type: 'checkbox',
      label: 'Start with Windows',
      checked: state.startWithWindows,
      action: { kind: 'start-with-windows', value: !state.startWithWindows },
    })
  }
  items.push({ type: 'separator' }, { type: 'normal', label: 'Quit Jarvis', action: { kind: 'quit' } })
  return items
}
