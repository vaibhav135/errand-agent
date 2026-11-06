import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import App from './App'
import { fakeApi } from './test/fakeApi'

const BASE = {
  'GET /api/health': { status: 'ok' },
  'POST /api/sessions': { session_id: 'sess_new' },
  'GET /api/sessions/sess_new/messages': [],
  'GET /api/sessions/sess_new/runs': [],
  'GET /api/memories': { saved: [], suggestions: [] },
  'GET /api/bookings': [],
}

beforeEach(() => localStorage.clear())
afterEach(() => vi.unstubAllGlobals())

describe('App shell', () => {
  it('shows chat beside the Trace, Memory and Bookings tabs', async () => {
    fakeApi({ ...BASE, 'GET /api/demo': { enabled: false } })

    render(<App />)

    expect(await screen.findByRole('tab', { name: 'Trace' })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'Memory' })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'Bookings' })).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Chat' })).toBeInTheDocument()
    await waitFor(() => expect(screen.getByText(/backend: ok/)).toBeInTheDocument())
    expect(screen.queryByRole('tab', { name: 'Demo' })).not.toBeInTheDocument()
  })

  it('shows the Demo tab only when the server is in demo mode', async () => {
    fakeApi({ ...BASE, 'GET /api/demo': { enabled: true } })

    render(<App />)

    expect(await screen.findByRole('tab', { name: 'Demo' })).toBeInTheDocument()
  })

  it('keeps the session across a reload, and starts a new one on request', async () => {
    localStorage.setItem('errand.session', 'sess_old')
    const { calls } = fakeApi({
      ...BASE,
      'GET /api/demo': { enabled: false },
      'GET /api/sessions/sess_old/messages': [],
      'GET /api/sessions/sess_old/runs': [],
    })

    render(<App />)
    const session = screen.getByLabelText('Session')
    await waitFor(() => expect(session).toHaveTextContent('sess_old'))
    expect(calls.some((c) => c.method === 'POST' && c.path === '/api/sessions')).toBe(false)

    await userEvent.click(screen.getByRole('button', { name: 'New session' }))

    await waitFor(() => expect(session).toHaveTextContent('sess_new'))
    expect(localStorage.getItem('errand.session')).toBe('sess_new')
  })

  it('starts a new session when the remembered one is gone', async () => {
    localStorage.setItem('errand.session', 'sess_gone') // its history route is a 404
    fakeApi({ ...BASE, 'GET /api/demo': { enabled: false } })

    render(<App />)

    await waitFor(() => expect(screen.getByLabelText('Session')).toHaveTextContent('sess_new'))
  })
})
