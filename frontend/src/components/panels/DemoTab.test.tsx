import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { fakeApi } from '@/test/fakeApi'
import { findToast, renderWithToasts } from '@/test/toasts'

import { DemoTab } from './DemoTab'

const ROUTES = {
  'GET /api/bookings': [
    {
      booking_id: 'bk_next',
      provider_name: 'CoolBreeze AC Care',
      service: 'ac_servicing',
      slot: '2026-11-10T10:00:00+05:30',
      price_inr: 1200,
      status: 'confirmed',
      when: 'upcoming',
    },
    {
      booking_id: 'bk_old',
      provider_name: 'Sparks Electricals',
      service: 'electrician',
      slot: '2026-10-13T11:00:00+05:30',
      price_inr: 800,
      status: 'completed',
      when: 'past',
    },
  ],
  'GET /api/demo/providers': [
    { provider_id: 'plm_quickfix', name: 'QuickFix Plumbing', service: 'plumber', price_inr: 699 },
    { provider_id: 'plm_flowright', name: 'FlowRight Plumbers', service: 'plumber', price_inr: 1200 },
  ],
  'GET /api/demo/state': { next_book: null, price_drift: [] },
  'POST /api/demo/next-book': { next_book: 'timeout_booked' },
  'POST /api/demo/provider-cancels': { booking_id: 'bk_next', status: 'cancelled_by_provider' },
  'POST /api/demo/price-drift': { provider_id: 'plm_flowright', fraction: 0.2 },
  'POST /api/demo/reset': { scenario: 'reset' },
}

afterEach(() => vi.unstubAllGlobals())

describe('DemoTab', () => {
  it('scripts the next booking and says what it set in a toast', async () => {
    const { calls } = fakeApi(ROUTES)
    renderWithToasts(<DemoTab version={0} onChanged={() => undefined} />)

    await userEvent.click(await screen.findByRole('button', { name: 'Times out after succeeding' }))

    expect(calls.find((c) => c.path === '/api/demo/next-book')?.body).toEqual({
      outcome: 'timeout_booked',
    })
    expect(await findToast('Next booking: times out after succeeding.')).toBeInTheDocument()
  })

  it('shows a refused action as an error toast', async () => {
    fakeApi({
      ...ROUTES,
      'POST /api/demo/reset': Response.json({ detail: 'demo mode is off' }, { status: 404 }),
    })
    renderWithToasts(<DemoTab version={0} onChanged={() => undefined} />)

    await userEvent.click(await screen.findByRole('button', { name: 'Reset' }))

    expect(await findToast("Didn't work: demo mode is off")).toBeInTheDocument()
  })

  it('lets a provider cancel one of the upcoming confirmed bookings', async () => {
    const { calls } = fakeApi(ROUTES)
    const changed = vi.fn()
    render(<DemoTab version={0} onChanged={changed} />)

    const select = await screen.findByRole('combobox', { name: 'Booking' })
    expect(screen.queryByRole('option', { name: /Sparks/ })).not.toBeInTheDocument() // past
    await userEvent.selectOptions(select, 'bk_next')
    await userEvent.click(screen.getByRole('button', { name: 'Provider cancels' }))

    expect(calls.find((c) => c.path === '/api/demo/provider-cancels')?.body).toMatchObject({
      booking_id: 'bk_next',
    })
    await waitFor(() => expect(changed).toHaveBeenCalled()) // bookings tab reloads
  })

  it('changes a provider’s price at hold time, by percent', async () => {
    const { calls } = fakeApi(ROUTES)
    render(<DemoTab version={0} onChanged={() => undefined} />)

    await userEvent.selectOptions(await screen.findByRole('combobox', { name: 'Provider' }), 'plm_flowright')
    const percent = screen.getByRole('spinbutton', { name: 'Change %' })
    await userEvent.clear(percent)
    await userEvent.type(percent, '20')
    await userEvent.click(screen.getByRole('button', { name: 'Change price' }))

    expect(calls.find((c) => c.path === '/api/demo/price-drift')?.body).toEqual({
      provider_id: 'plm_flowright',
      fraction: 0.2,
    })
  })

  it('shows what is armed: the next booking’s outcome pressed, and active price changes', async () => {
    const { calls } = fakeApi({
      ...ROUTES,
      'GET /api/demo/state': {
        next_book: 'slot_taken',
        price_drift: [{ provider_id: 'plm_flowright', name: 'FlowRight Plumbers', fraction: 0.2 }],
      },
    })
    render(<DemoTab version={0} onChanged={() => undefined} />)

    expect(await screen.findByRole('button', { name: 'Slot taken' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: 'Times out after succeeding' })).toHaveAttribute('aria-pressed', 'false')
    expect(screen.getByText('Armed: the next booking will hit "slot taken".')).toBeInTheDocument()
    const drift = screen.getByRole('listitem', { name: 'FlowRight Plumbers' })
    expect(drift).toHaveTextContent('+20% at hold')

    await userEvent.click(within(drift).getByRole('button', { name: 'Clear' }))

    expect(calls.find((c) => c.path === '/api/demo/price-drift')?.body).toEqual({
      provider_id: 'plm_flowright',
      fraction: 0,
    })
  })

  it('reloads what is armed when something changed elsewhere (a booking used it)', async () => {
    const { calls } = fakeApi(ROUTES)
    const { rerender } = render(<DemoTab version={0} onChanged={() => undefined} />)
    await screen.findByRole('button', { name: 'Slot taken' })

    rerender(<DemoTab version={1} onChanged={() => undefined} />)

    await waitFor(() =>
      expect(calls.filter((c) => c.path === '/api/demo/state')).toHaveLength(2),
    )
  })

  it('resets everything', async () => {
    const { calls } = fakeApi(ROUTES)
    render(<DemoTab version={0} onChanged={() => undefined} />)

    await userEvent.click(await screen.findByRole('button', { name: 'Reset' }))

    expect(calls.some((c) => c.path === '/api/demo/reset')).toBe(true)
  })
})
