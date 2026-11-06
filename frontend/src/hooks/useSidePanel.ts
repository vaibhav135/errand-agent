// The side panel's open/hidden state and width, remembered across reloads.
//
// Two separate values on purpose: dragging the panel shut makes its width 0, and if
// that were saved as the width, "Show side panel" would reopen it at 0. So the width
// is only saved while the panel is open, and hidden is its own flag.

import { useState } from 'react'

const OPEN_KEY = 'errand.sidePanel' // 'hidden' | 'open'
const WIDTH_KEY = 'errand.sidePanelWidth' // percent of the workspace
const DEFAULT_WIDTH = 50

function read(key: string): string | null {
  try {
    return localStorage.getItem(key)
  } catch {
    return null // storage blocked: fall back to the defaults
  }
}

function write(key: string, value: string) {
  try {
    localStorage.setItem(key, value)
  } catch {
    // not remembered, but still applied for this visit
  }
}

export function useSidePanel() {
  const [open, setOpen] = useState(() => read(OPEN_KEY) !== 'hidden')

  function setPanelOpen(value: boolean) {
    setOpen(value)
    write(OPEN_KEY, value ? 'open' : 'hidden')
  }

  return { open, setOpen: setPanelOpen }
}

export function savedPanelWidth(): number {
  const width = Number(read(WIDTH_KEY))
  return width > 0 && width < 100 ? width : DEFAULT_WIDTH
}

export function savePanelWidth(percent: number) {
  if (percent > 0) write(WIDTH_KEY, percent.toFixed(1))
}
