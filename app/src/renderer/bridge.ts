// The preload's API as the pages see it (window.jarvis).

import type { AppView } from '../shared/view'

export type JarvisBridge = {
  onView(fn: (view: AppView) => void): void
  toggleOverlay(): void
  openMenu(): void
}

declare global {
  interface Window {
    jarvis: JarvisBridge
  }
}

export function bridge(): JarvisBridge {
  return window.jarvis
}
