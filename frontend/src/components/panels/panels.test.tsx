import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { Memory } from '@/api/types'
import { fakeApi } from '@/test/fakeApi'
import { findToast, renderWithToasts } from '@/test/toasts'

import { BookingsTab } from './BookingsTab'
import { MemoryTab } from './MemoryTab'

function memory(id: string, key: string, text: string, extra: Partial<Memory> = {}): Memory {
  return {
    memory_id: id,
    key,
    scope: 'global',
    topic: null,
    text,
    source: 'explicit',
    status: 'active',
    reason: null,
    ...extra,
  }
}

const MEMORIES = {
  saved: [
    memory('m_start', 'earliest_start', 'Earliest start: 10:00'),
    memory('m_budget', 'budget_max_inr', 'Budget for plumber: up to ₹1500', { scope: 'plumber' }),
    memory('m_dog', 'note', 'Note: dog at home, ring the bell twice', { topic: 'home_services' }),
    memory('m_food', 'note', "Note: I'm vegetarian", { topic: 'food' }),
    memory('m_clean', 'preferred_provider', 'Usual cleaning: FreshNest Cleaners', {
      source: 'inferred',
    }),
  ],
  suggestions: [
    memory('m_sparks', 'preferred_provider', 'Usual electrician: Sparks Electricals', {
      source: 'inferred',
      status: 'pending_confirmation',
      reason: '2 completed bookings with Sparks Electricals',
    }),
  ],
}

afterEach(() => vi.unstubAllGlobals())

describe('MemoryTab', () => {
  it('groups booking preferences, personal notes and suggestions, with where each came from', async () => {
    fakeApi({ 'GET /api/memories': MEMORIES })

    render(<MemoryTab version={0} />)

    const prefs = await screen.findByRole('region', { name: 'Booking preferences' })
    expect(prefs).toHaveTextContent('Earliest start: 10:00')
    expect(prefs).toHaveTextContent('dog at home')
    expect(within(prefs).getAllByText('you told me').length).toBeGreaterThan(0)
    expect(within(prefs).getByText('inferred, you confirmed')).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Personal notes' })).toHaveTextContent('vegetarian')
    const suggestions = screen.getByRole('region', { name: 'Suggestions' })
    expect(suggestions).toHaveTextContent('2 completed bookings with Sparks Electricals')
  })

  it('corrects a fact in place, sending budgets as numbers', async () => {
    const { calls } = fakeApi({
      'GET /api/memories': MEMORIES,
      'PATCH /api/memories/m_budget': memory('m_new', 'budget_max_inr', 'Budget: up to ₹1200'),
    })
    render(<MemoryTab version={0} />)
    const row = await screen.findByRole('listitem', { name: /Budget for plumber/ })

    await userEvent.click(within(row).getByRole('button', { name: 'Edit' }))
    const input = within(row).getByRole('textbox', { name: 'New value' })
    expect(input).toHaveValue('1500')
    await userEvent.clear(input)
    await userEvent.type(input, '1200{Enter}')

    await waitFor(() =>
      expect(calls.find((c) => c.method === 'PATCH')?.body).toEqual({ value: 1200 }),
    )
  })

  it('confirms a correction with a toast; a refused one stays beside the field', async () => {
    fakeApi({
      'GET /api/memories': MEMORIES,
      'PATCH /api/memories/m_budget': memory('m_new', 'budget_max_inr', 'Budget: up to ₹1200'),
      'PATCH /api/memories/m_start': Response.json({ detail: "doesn't fit" }, { status: 422 }),
    })
    renderWithToasts(<MemoryTab version={0} />)

    const budget = await screen.findByRole('listitem', { name: /Budget for plumber/ })
    await userEvent.click(within(budget).getByRole('button', { name: 'Edit' }))
    await userEvent.type(within(budget).getByRole('textbox', { name: 'New value' }), '{Enter}')
    expect(await findToast('Updated: Budget: up to ₹1200')).toBeInTheDocument()

    const start = screen.getByRole('listitem', { name: /Earliest start/ })
    await userEvent.click(within(start).getByRole('button', { name: 'Edit' }))
    await userEvent.type(within(start).getByRole('textbox', { name: 'New value' }), '{Enter}')
    expect(await within(start).findByText("doesn't fit")).toBeInTheDocument()
    expect(screen.queryByText(/Couldn't/)).not.toBeInTheDocument()
  })

  it('shows why a correction was refused', async () => {
    fakeApi({
      'GET /api/memories': MEMORIES,
      'PATCH /api/memories/m_start': Response.json(
        { detail: "that value doesn't fit: invalid time" },
        { status: 422 },
      ),
    })
    render(<MemoryTab version={0} />)
    const row = await screen.findByRole('listitem', { name: /Earliest start/ })

    await userEvent.click(within(row).getByRole('button', { name: 'Edit' }))
    const input = within(row).getByRole('textbox', { name: 'New value' })
    await userEvent.clear(input)
    await userEvent.type(input, 'teatime{Enter}')

    expect(await within(row).findByText(/doesn't fit/)).toBeInTheDocument()
  })

  it('forgets a fact only after a second click', async () => {
    const { calls } = fakeApi({
      'GET /api/memories': MEMORIES,
      'DELETE /api/memories/m_dog': new Response(null, { status: 204 }),
    })
    render(<MemoryTab version={0} />)
    const row = await screen.findByRole('listitem', { name: /dog at home/ })

    await userEvent.click(within(row).getByRole('button', { name: 'Forget' }))
    expect(calls.some((c) => c.method === 'DELETE')).toBe(false)
    await userEvent.click(within(row).getByRole('button', { name: 'Forget? Yes' }))

    await waitFor(() => expect(calls.some((c) => c.method === 'DELETE')).toBe(true))
  })

  it('confirms a suggestion', async () => {
    const { calls } = fakeApi({
      'GET /api/memories': MEMORIES,
      'POST /api/memories/m_sparks/confirm': MEMORIES.suggestions[0],
    })
    render(<MemoryTab version={0} />)
    const row = await screen.findByRole('listitem', { name: /Sparks/ })

    await userEvent.click(within(row).getByRole('button', { name: 'Confirm' }))

    await waitFor(() =>
      expect(calls.some((c) => c.path === '/api/memories/m_sparks/confirm')).toBe(true),
    )
  })

  it('confirms each change with a toast', async () => {
    fakeApi({
      'GET /api/memories': MEMORIES,
      'POST /api/memories/m_sparks/confirm': MEMORIES.suggestions[0],
      'DELETE /api/memories/m_dog': new Response(null, { status: 204 }),
    })
    renderWithToasts(<MemoryTab version={0} />)

    const suggestion = await screen.findByRole('listitem', { name: /Sparks/ })
    await userEvent.click(within(suggestion).getByRole('button', { name: 'Confirm' }))
    expect(await findToast('Saved: Usual electrician: Sparks Electricals')).toBeInTheDocument()

    const dog = screen.getByRole('listitem', { name: /dog at home/ })
    await userEvent.click(within(dog).getByRole('button', { name: 'Forget' }))
    await userEvent.click(within(dog).getByRole('button', { name: 'Forget? Yes' }))
    expect(await findToast('Forgotten: Note: dog at home, ring the bell twice')).toBeInTheDocument()
  })

  it('shows a failed change as an error toast', async () => {
    fakeApi({
      'GET /api/memories': MEMORIES,
      'POST /api/memories/m_sparks/reject': Response.json(
        { detail: 'already decided' },
        { status: 409 },
      ),
    })
    renderWithToasts(<MemoryTab version={0} />)

    const suggestion = await screen.findByRole('listitem', { name: /Sparks/ })
    await userEvent.click(within(suggestion).getByRole('button', { name: 'Reject' }))

    expect(await findToast("Couldn't dismiss that: already decided")).toBeInTheDocument()
  })

  it('reloads when something changed elsewhere', async () => {
    const { calls } = fakeApi({ 'GET /api/memories': MEMORIES })
    const { rerender } = render(<MemoryTab version={0} />)
    await screen.findByText('Earliest start: 10:00')

    rerender(<MemoryTab version={1} />)

    await waitFor(() =>
      expect(calls.filter((c) => c.path === '/api/memories').length).toBe(2),
    )
  })
})

describe('BookingsTab', () => {
  it('lists upcoming before past, with slot, price and status', async () => {
    fakeApi({
      'GET /api/bookings': [
        {
          booking_id: 'bk_old',
          provider_name: 'Sparks Electricals',
          service: 'electrician',
          slot: '2026-10-13T11:00:00+05:30',
          price_inr: 800,
          status: 'completed',
          when: 'past',
        },
        {
          booking_id: 'bk_next',
          provider_name: 'CoolBreeze AC Care',
          service: 'ac_servicing',
          slot: '2026-11-10T10:00:00+05:30',
          price_inr: 1200,
          status: 'confirmed',
          when: 'upcoming',
        },
      ],
    })

    render(<BookingsTab version={0} />)

    const upcoming = await screen.findByRole('region', { name: 'Upcoming' })
    expect(upcoming).toHaveTextContent('CoolBreeze AC Care')
    expect(upcoming).toHaveTextContent('Tue, 10 Nov, 10:00')
    expect(upcoming).toHaveTextContent('₹1,200')
    expect(upcoming).toHaveTextContent('confirmed')
    expect(screen.getByRole('region', { name: 'Past' })).toHaveTextContent('Sparks Electricals')
  })
})
