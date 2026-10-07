// The overlay page: the big ring, its label, and under it what you said, the
// start of Claude's reply, the last actions and any note. Every text goes in
// through textContent, never as HTML: it comes from speech and from Claude.

import type { AppView } from '../shared/view'
import { bridge } from './bridge'
import { RingView } from './ring'

const byId = (id: string): HTMLElement => {
  const element = document.getElementById(id)
  if (element === null) throw new Error(`overlay.html has no #${id}`)
  return element
}

/** Sets an element's text and hides it when there is none. */
function setText(element: HTMLElement, text: string): void {
  element.textContent = text
  element.hidden = text === ''
}

function showActions(list: HTMLElement, actions: AppView['actions']): void {
  list.replaceChildren(
    ...actions.map(action => {
      const item = document.createElement('li')
      item.dataset.status = action.status
      const mark = document.createElement('span')
      mark.className = 'mark'
      mark.textContent = action.mark
      const text = document.createElement('span')
      text.className = 'text'
      text.textContent = action.label
      item.append(mark, text)
      return item
    }),
  )
  list.hidden = actions.length === 0
}

const ring = new RingView(byId('ring'))
const label = byId('label')
const panel = byId('panel')
const utterance = byId('utterance')
const reply = byId('reply')
const actions = byId('actions')
const note = byId('note')

bridge().onView(view => {
  document.body.dataset.mode = view.mode
  document.body.dataset.visible = String(view.isOverlayShown)
  ring.show(view.mode, view.mic, view.out)
  setText(label, view.label)
  setText(utterance, view.utterance)
  setText(reply, view.reply)
  showActions(actions, view.actions)
  setText(note, view.note)
  panel.hidden = view.utterance === '' && view.reply === '' && view.actions.length === 0 && view.note === ''
})
