// Light or dark: the `dark` class on <html> switches the theme tokens in index.css.
//
// The first visit follows the computer's setting; once someone picks, the choice is kept
// in localStorage under `theme`. index.html applies the same rule before the first paint
// so a dark-mode reload doesn't flash white; keep the two in step.

import { useEffect, useState } from 'react'

export type Theme = 'light' | 'dark'

const KEY = 'theme'

function saved(): Theme | null {
  try {
    const value = localStorage.getItem(KEY)
    return value === 'light' || value === 'dark' ? value : null
  } catch {
    return null // storage blocked (e.g. some private windows): just don't remember
  }
}

function system(): Theme {
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

export function useTheme() {
  const [theme, setTheme] = useState<Theme>(() => saved() ?? system())

  useEffect(() => {
    document.documentElement.classList.toggle('dark', theme === 'dark')
  }, [theme])

  function toggle() {
    const next: Theme = theme === 'dark' ? 'light' : 'dark'
    setTheme(next)
    try {
      localStorage.setItem(KEY, next)
    } catch {
      // not remembered, but still switched for this visit
    }
  }

  return { theme, toggle }
}
