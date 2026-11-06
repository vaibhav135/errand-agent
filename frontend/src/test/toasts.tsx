// Render a component with the app's Toaster beside it, so tests can find its toasts.
import { render, screen, within } from '@testing-library/react'
import type { ReactElement } from 'react'

import { Toaster } from '@/components/ui/sonner'

export function renderWithToasts(ui: ReactElement) {
  return render(
    <>
      {ui}
      <Toaster />
    </>,
  )
}

/** A toast with this text, found inside sonner's notifications region (so the same
 * text shown elsewhere on the page doesn't count). */
export async function findToast(text: string) {
  const region = await screen.findByRole('region', { name: /Notifications/ })
  return within(region).findByText(text)
}
