import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { fakeApi } from '@/test/fakeApi'

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
  'POST /api/demo/next-book': { next_book: 'timeout_booked' },
  'POST /api/demo/provider-cancels': { booking_id: 'bk_next', status: 'cancelled_by_provider' },
  'POST /api/demo/price-drift': { provider_id: 'plm_flowright', fraction: 0.2 },
  'POST /api/demo/reset': { scenario: 'reset' },
}

afterEach(() => vi.unstubAllGlobals())

describe('DemoTab', () => {
  it('scripts the next booking and says what it set', async () => {
    const { calls } = fakeApi(ROUTES)
    render(<DemoTab version={0} onChanged={() => undefined} />)

    await userEvent.click(await screen.findByRole('button', { name: 'Times out after succeeding' }))

    expect(calls.find((c) => c.path === '/api/demo/next-book')?.body).toEqual({
      outcome: 'timeout_booked',
    })
    expect(await screen.findByRole('status')).toHaveTextContent(/next booking/i)
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

  it('resets everything', async () => {
    const { calls } = fakeApi(ROUTES)
    render(<DemoTab version={0} onChanged={() => undefined} />)

    await userEvent.click(await screen.findByRole('button', { name: 'Reset' }))

    expect(calls.some((c) => c.path === '/api/demo/reset')).toBe(true)
  })
})
