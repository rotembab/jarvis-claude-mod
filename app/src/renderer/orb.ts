// The orb page: the ring alone, gray while no Claude Code session is heard
// from. Its centre is a button (click: show or hide the overlay; right-click:
// the tray menu); the rest of it is the window's drag handle.

import { bridge } from './bridge'
import { RingView } from './ring'

const host = document.getElementById('ring')
const hit = document.getElementById('hit')
if (host === null || hit === null) throw new Error('orb.html needs #ring and #hit')

const jarvis = bridge()
const ring = new RingView(host)
ring.show('offline', 0, 0)

jarvis.onView(view => {
  document.body.dataset.mode = view.mode
  ring.show(view.mode, view.mic, view.out)
})

hit.addEventListener('click', () => jarvis.toggleOverlay())
hit.addEventListener('contextmenu', event => {
  event.preventDefault()
  jarvis.openMenu()
})
