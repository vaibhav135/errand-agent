import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ThemeToggle } from './ThemeToggle'

/** jsdom has no matchMedia: stand in for the computer's light/dark setting. */
function systemPrefersDark(dark: boolean) {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: query === '(prefers-color-scheme: dark)' && dark,
    media: query,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
  }))
}

const isDark = () => document.documentElement.classList.contains('dark')

beforeEach(() => {
  localStorage.clear()
  document.documentElement.classList.remove('dark')
})
afterEach(() => vi.unstubAllGlobals())

describe('ThemeToggle', () => {
  it('follows the computer’s setting when nothing was chosen', () => {
    systemPrefersDark(true)
    render(<ThemeToggle />)

    expect(isDark()).toBe(true)
    expect(screen.getByRole('button', { name: 'Switch to light mode' })).toBeInTheDocument()
  })

  it('switches and remembers the choice', async () => {
    systemPrefersDark(false)
    render(<ThemeToggle />)
    expect(isDark()).toBe(false)

    await userEvent.click(screen.getByRole('button', { name: 'Switch to dark mode' }))

    expect(isDark()).toBe(true)
    expect(localStorage.getItem('theme')).toBe('dark')
  })

  it('a saved choice wins over the computer’s setting', () => {
    systemPrefersDark(true)
    localStorage.setItem('theme', 'light')
    render(<ThemeToggle />)

    expect(isDark()).toBe(false)
  })
})
