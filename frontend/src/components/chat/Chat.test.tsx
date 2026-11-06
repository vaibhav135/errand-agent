import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { Reply } from '@/api/types'
import { fakeApi } from '@/test/fakeApi'
import { findToast, renderWithToasts } from '@/test/toasts'

import { Chat } from './Chat'

const SESSION = 'sess_1'

/** An SSE response the test feeds frame by frame. */
function liveStream() {
  const encoder = new TextEncoder()
  let controller!: ReadableStreamDefaultController<Uint8Array>
  const body = new ReadableStream<Uint8Array>({
    start(c) {
      controller = c
    },
  })
  return {
    response: new Response(body, { headers: { 'content-type': 'text/event-stream' } }),
    send(event: string, data: unknown) {
      controller.enqueue(encoder.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`))
    },
    close() {
      controller.close()
    },
  }
}

function reply(overrides: Partial<Reply>): Reply {
  return {
    run_id: 'run_1',
    kind: 'answered',
    message: '',
    approval: null,
    booking: null,
    suggestions: [],
    ...overrides,
  }
}

const CARD = {
  approval_id: 'appr_1',
  action: 'book' as const,
  fingerprint: 'fp_shown',
  status: 'pending',
  details: {
    provider_name: 'FlowRight Plumbers',
    slot: '2026-11-07T11:00:00+05:30',
    price_inr: 1200,
    booking_id: null,
  },
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

/** "Now" is Fri 06 Nov 2026, 12:00 IST; only Date is faked, so streams and waits run. */
function nowIs(iso = '2026-11-06T12:00:00+05:30') {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(new Date(iso))
}

/** The message (article) that holds this text. */
const messageWith = (text: string) => screen.getByText(text).closest('article') as HTMLElement

describe('Chat times', () => {
  it('shows each message’s local time, and a divider when the day changes', async () => {
    nowIs()
    fakeApi({
      [`GET /api/sessions/${SESSION}/messages`]: [
        { role: 'user', content: 'older question', run_id: null, created_at: '2026-11-04T08:00:00+00:00' },
        { role: 'assistant', content: 'older answer', run_id: 'r1', created_at: '2026-11-04T08:00:09+00:00' },
        { role: 'user', content: 'yesterday', run_id: null, created_at: '2026-11-05T04:30:00+00:00' },
        { role: 'user', content: 'this morning', run_id: null, created_at: '2026-11-06T03:32:00+00:00' },
      ],
    })

    render(<Chat sessionId={SESSION} />)

    await screen.findByText('older answer')
    expect(screen.getAllByRole('separator').map((d) => d.textContent)).toEqual([
      'Wed, 04 Nov',
      'Yesterday',
      'Today',
    ])
    expect(within(messageWith('older question')).getByText('13:30')).toBeInTheDocument() // 08:00 UTC
    expect(within(messageWith('this morning')).getByText('09:02')).toBeInTheDocument()
    expect(within(messageWith('older answer')).getByText('13:30')).toHaveAttribute(
      'title',
      'Wed, 04 Nov 2026, 13:30:09',
    )
  })

  it('stamps a new message and its reply with the time they happen', async () => {
    nowIs()
    const stream = liveStream()
    fakeApi({
      [`GET /api/sessions/${SESSION}/messages`]: [],
      [`POST /api/sessions/${SESSION}/messages`]: stream.response,
    })
    render(<Chat sessionId={SESSION} />)

    await type('hello')
    expect(within(messageWith('hello')).getByText('12:00')).toBeInTheDocument()
    expect(screen.getByRole('separator')).toHaveTextContent('Today')

    vi.setSystemTime(new Date('2026-11-06T12:01:00+05:30'))
    stream.send('final', reply({ message: 'Hi there' }))
    stream.close()

    expect(await screen.findByText('Hi there')).toBeInTheDocument()
    expect(within(messageWith('Hi there')).getByText('12:01')).toBeInTheDocument()
  })
})

async function type(text: string) {
  await userEvent.type(screen.getByRole('textbox', { name: 'Message' }), `${text}{Enter}`)
}

describe('Chat', () => {
  it('loads the conversation so far', async () => {
    fakeApi({
      [`GET /api/sessions/${SESSION}/messages`]: [
        { role: 'user', content: 'hi', run_id: null, created_at: '' },
        { role: 'assistant', content: 'Hello! How can I help?', run_id: 'r', created_at: '' },
      ],
    })

    render(<Chat sessionId={SESSION} />)

    expect(await screen.findByText('Hello! How can I help?')).toBeInTheDocument()
  })

  it('shows progress while tools run, streams the text, then the final reply', async () => {
    const stream = liveStream()
    fakeApi({
      [`GET /api/sessions/${SESSION}/messages`]: [],
      [`POST /api/sessions/${SESSION}/messages`]: stream.response,
    })
    render(<Chat sessionId={SESSION} />)

    await type('find me a plumber')
    expect(screen.getByText('find me a plumber')).toBeInTheDocument()

    stream.send('tool', { tool: 'search_and_compare' })
    expect(await screen.findByText('Searching providers…')).toBeInTheDocument()
    stream.send('text', { chunk: 'Found ' })
    expect(await screen.findByText(/Found/)).toBeInTheDocument()
    stream.send('text', { chunk: 'three.' })
    stream.send('final', reply({ message: 'Found three plumbers.' }))
    stream.close()

    expect(await screen.findByText('Found three plumbers.')).toBeInTheDocument()
    expect(screen.queryByText('Searching providers…')).not.toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Message' })).toBeEnabled()
  })

  it('draws the approval card from the reply and decides with the shown fingerprint', async () => {
    const stream = liveStream()
    const { calls } = fakeApi({
      [`GET /api/sessions/${SESSION}/messages`]: [],
      [`POST /api/sessions/${SESSION}/messages`]: stream.response,
      'POST /api/approvals/appr_1/decision': reply({
        kind: 'booked',
        message: 'Booked FlowRight Plumbers for Sat 07 Nov, 11:00 at ₹1200. Booking ID bk_9.',
        booking: {
          booking_id: 'bk_9',
          provider_name: 'FlowRight Plumbers',
          service: 'plumber',
          slot: CARD.details.slot,
          price_inr: 1200,
          status: 'confirmed',
        },
      }),
    })
    render(<Chat sessionId={SESSION} />)
    await type('book the 11:00 slot')
    stream.send('final', reply({ kind: 'needs_approval', message: 'Shall I?', approval: CARD }))
    stream.close()

    const card = await screen.findByRole('group', { name: 'Approve booking?' })
    expect(card).toHaveTextContent('FlowRight Plumbers')
    expect(card).toHaveTextContent('Sat, 07 Nov, 11:00')
    expect(card).toHaveTextContent('₹1,200')

    await userEvent.click(within(card).getByRole('button', { name: 'Approve' }))

    expect(await screen.findByText(/Booking ID bk_9/)).toBeInTheDocument()
    expect(calls.find((c) => c.path === '/api/approvals/appr_1/decision')?.body).toEqual({
      approve: true,
      fingerprint: 'fp_shown',
    })
    expect(within(card).getByRole('button', { name: 'Approve' })).toBeDisabled()
    expect(screen.getByRole('group', { name: 'Booked' })).toHaveTextContent('bk_9')
  })

  it('lets the user confirm a suggestion on its card', async () => {
    const stream = liveStream()
    const { calls } = fakeApi({
      [`GET /api/sessions/${SESSION}/messages`]: [],
      [`POST /api/sessions/${SESSION}/messages`]: stream.response,
      'POST /api/memories/mem_7/confirm': { memory_id: 'mem_7' },
    })
    render(<Chat sessionId={SESSION} />)
    await type('what have you noticed?')
    stream.send(
      'final',
      reply({
        message: 'Here they are.',
        suggestions: [
          { memory_id: 'mem_7', text: 'Usual electrician: Sparks Electricals', reason: '2 jobs' },
        ],
      }),
    )
    stream.close()

    const card = await screen.findByRole('group', { name: 'Suggestions from your bookings' })
    await userEvent.click(within(card).getByRole('button', { name: 'Confirm' }))

    expect(await within(card).findByText('Saved')).toBeInTheDocument()
    expect(calls.some((c) => c.path === '/api/memories/mem_7/confirm')).toBe(true)
  })

  it('says so when saving a suggestion fails, instead of failing silently', async () => {
    const stream = liveStream()
    fakeApi({
      [`GET /api/sessions/${SESSION}/messages`]: [],
      [`POST /api/sessions/${SESSION}/messages`]: stream.response,
      'POST /api/memories/mem_7/confirm': Response.json({ detail: 'already decided' }, { status: 409 }),
    })
    renderWithToasts(<Chat sessionId={SESSION} />)
    await type('what have you noticed?')
    stream.send(
      'final',
      reply({ suggestions: [{ memory_id: 'mem_7', text: 'Usual electrician: Sparks', reason: '2 jobs' }] }),
    )
    stream.close()

    const card = await screen.findByRole('group', { name: 'Suggestions from your bookings' })
    await userEvent.click(within(card).getByRole('button', { name: 'Confirm' }))

    expect(await findToast("Couldn't save that: already decided")).toBeInTheDocument()
    expect(within(card).queryByText('Saved')).not.toBeInTheDocument()
  })

  it('puts the agent’s reply in its own bubble, with its cards after it', async () => {
    const stream = liveStream()
    fakeApi({
      [`GET /api/sessions/${SESSION}/messages`]: [],
      [`POST /api/sessions/${SESSION}/messages`]: stream.response,
    })
    render(<Chat sessionId={SESSION} />)
    await type('book the 11:00 slot')
    stream.send('final', reply({ kind: 'needs_approval', message: 'Shall I?', approval: CARD }))
    stream.close()

    const card = await screen.findByRole('group', { name: 'Approve booking?' })
    const bubble = screen.getByText('Shall I?').closest('[data-bubble]')
    expect(bubble).toHaveAttribute('data-bubble', 'assistant')
    expect(bubble).not.toContainElement(card) // a card is not a box inside the bubble
    expect(bubble?.closest('article')).toContainElement(card) // but it belongs to the reply
  })

  // BUG-10: a turn with no tool call showed nothing until the first word arrived.
  it('shows that the agent is working before the first word (BUG-10)', async () => {
    const stream = liveStream()
    fakeApi({
      [`GET /api/sessions/${SESSION}/messages`]: [],
      [`POST /api/sessions/${SESSION}/messages`]: stream.response,
    })
    render(<Chat sessionId={SESSION} />)

    await type('hello')

    expect(screen.getByRole('status', { name: 'Agent is typing' })).toBeInTheDocument()
    stream.send('text', { chunk: 'Hel' })
    await waitFor(() =>
      expect(screen.queryByRole('status', { name: 'Agent is typing' })).not.toBeInTheDocument(),
    )
  })

  it('swaps the typing dots for a tool’s progress line', async () => {
    const stream = liveStream()
    fakeApi({
      [`GET /api/sessions/${SESSION}/messages`]: [],
      [`POST /api/sessions/${SESSION}/messages`]: stream.response,
    })
    render(<Chat sessionId={SESSION} />)
    await type('find me a plumber')
    expect(screen.getByRole('status', { name: 'Agent is typing' })).toBeInTheDocument()

    stream.send('tool', { tool: 'search_and_compare' })

    expect(await screen.findByText('Searching providers…')).toBeInTheDocument()
    expect(screen.queryByRole('status', { name: 'Agent is typing' })).not.toBeInTheDocument()
  })

  it('marks the reply as streaming until the final arrives, revealing text steadily', async () => {
    const stream = liveStream()
    fakeApi({
      [`GET /api/sessions/${SESSION}/messages`]: [],
      [`POST /api/sessions/${SESSION}/messages`]: stream.response,
    })
    render(<Chat sessionId={SESSION} />)
    await type('hello')
    const long = 'Hello! ' + 'I can help with plumbers, electricians and more. '.repeat(4)

    stream.send('text', { chunk: long })

    const bubble = await waitFor(() => {
      const el = document.querySelector('[data-bubble="assistant"]')
      expect(el).not.toBeNull()
      return el as HTMLElement
    })
    expect(bubble).toHaveAttribute('data-streaming', 'true') // the caret shows
    expect(bubble.textContent!.length).toBeLessThan(long.trim().length) // not all at once
    await waitFor(() => expect(bubble).toHaveTextContent(long.trim())) // but soon all of it

    stream.send('final', reply({ message: long.trim() }))
    stream.close()
    await waitFor(() => expect(bubble).toHaveAttribute('data-streaming', 'false'))
  })

  it('explains a turn the server refused', async () => {
    fakeApi({
      [`GET /api/sessions/${SESSION}/messages`]: [],
      [`POST /api/sessions/${SESSION}/messages`]: Response.json(
        { detail: 'ANTHROPIC_API_KEY is not set (add it to backend/.env)' },
        { status: 503 },
      ),
    })
    render(<Chat sessionId={SESSION} />)

    await type('hi')

    expect(await screen.findByText(/ANTHROPIC_API_KEY is not set/)).toBeInTheDocument()
  })

  it('Stop stops showing the turn, and says the server will finish it', async () => {
    const stream = liveStream()
    fakeApi({
      [`GET /api/sessions/${SESSION}/messages`]: [],
      [`POST /api/sessions/${SESSION}/messages`]: stream.response,
    })
    render(<Chat sessionId={SESSION} />)
    await type('find me a plumber')
    stream.send('tool', { tool: 'search_and_compare' })
    await screen.findByText('Searching providers…')

    await userEvent.click(screen.getByRole('button', { name: 'Stop' }))

    expect(await screen.findByText(/still finishes on the server/)).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Message' })).toBeEnabled()
  })
})
