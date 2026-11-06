// Header button: flips between light and dark (see useTheme).

import { Moon, Sun } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { useTheme } from '@/hooks/useTheme'

export function ThemeToggle() {
  const { theme, toggle } = useTheme()
  const label = `Switch to ${theme === 'dark' ? 'light' : 'dark'} mode`

  return (
    <Button size="icon-sm" variant="ghost" aria-label={label} title={label} onClick={toggle}>
      {theme === 'dark' ? <Sun /> : <Moon />}
    </Button>
  )
}
