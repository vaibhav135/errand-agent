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

describe('Chat: the slots card', () => {
  const option = (provider_id: string, name: string, price_inr: number, ...slots: string[]) => ({
    provider_id,
    name,
    price_inr,
    rating: 4.2,
    review_count: 120,
    verified: true,
    slots,
    reasons: [],
  })
  const OPTIONS = [
    option('plm_quickfix', 'QuickFix Plumbing', 699, '2026-11-07T11:00:00+05:30', '2026-11-07T12:00:00+05:30', '2026-11-08T10:00:00+05:30'),
    option('plm_flowright', 'FlowRight Plumbers', 1200, '2026-11-07T14:00:00+05:30'),
  ]

  async function searched(routes: Record<string, unknown> = {}) {
    const stream = liveStream()
    const api = fakeApi({
      [`GET /api/sessions/${SESSION}/messages`]: [],
      [`POST /api/sessions/${SESSION}/messages`]: stream.response,
      ...routes,
    })
    render(<Chat sessionId={SESSION} />)
    await type('find me a plumber')
    stream.send('final', reply({ message: 'Here they are.', options: OPTIONS }))
    stream.close()
    return { ...api, picker: await screen.findByRole('group', { name: 'Pick a slot' }) }
  }

  it('shows the options by day, with each provider’s times that day', async () => {
    const { picker } = await searched()

    expect(within(picker).getByRole('button', { name: 'Sat 07 Nov' })).toHaveAttribute('aria-pressed', 'true')
    expect(within(picker).getByRole('button', { name: 'QuickFix Plumbing at Sat 07 Nov, 11:00' })).toBeInTheDocument()
    expect(within(picker).getByRole('button', { name: 'FlowRight Plumbers at Sat 07 Nov, 14:00' })).toBeInTheDocument()
    expect(within(picker).queryByRole('button', { name: /Sun 08 Nov, 10:00/ })).not.toBeInTheDocument()

    await userEvent.click(within(picker).getByRole('button', { name: 'Sun 08 Nov' }))

    expect(within(picker).getByRole('button', { name: 'QuickFix Plumbing at Sun 08 Nov, 10:00' })).toBeInTheDocument()
    expect(within(picker).queryByText('FlowRight Plumbers')).not.toBeInTheDocument() // nothing that day
  })

  it('holds a picked slot and shows its approval card; the picker then locks', async () => {
    const { calls, picker } = await searched({
      [`POST /api/sessions/${SESSION}/pick`]: reply({
        kind: 'needs_approval',
        message: 'I’ve held QuickFix Plumbing for Sat 07 Nov, 12:00 at ₹699.',
        approval: CARD,
      }),
    })
    const chip = within(picker).getByRole('button', { name: 'QuickFix Plumbing at Sat 07 Nov, 12:00' })

    await userEvent.click(chip)

    expect(calls.find((c) => c.path === `/api/sessions/${SESSION}/pick`)?.body).toEqual({
      provider_id: 'plm_quickfix',
      slot: '2026-11-07T12:00:00+05:30',
    })
    expect(await screen.findByRole('group', { name: 'Approve booking?' })).toBeInTheDocument()
    expect(chip).toBeDisabled() // one hold per card
  })
})

describe('Chat: messages the agent sends unasked (Phase 7)', () => {
  const NOTICE = 'CoolBreeze AC Care cancelled your AC servicing booking for Tue 10 Nov, 10:00.'

  it('shows a card that waited while the user was away, under the latest reply', async () => {
    fakeApi({
      [`GET /api/sessions/${SESSION}/messages`]: [
        { role: 'assistant', content: NOTICE, run_id: 'run_n', created_at: '' },
        { role: 'assistant', content: 'Here is a replacement.', run_id: 'run_r', created_at: '' },
      ],
      [`GET /api/sessions/${SESSION}/pending`]: [CARD],
    })

    render(<Chat sessionId={SESSION} />)

    const card = await screen.findByRole('group', { name: 'Approve booking?' })
    expect(card).toHaveTextContent('FlowRight Plumbers')
    expect(screen.getByText('Here is a replacement.').closest('article')).toContainElement(card)
  })

  it('appends a pushed notice with a toast, then the replacement with its card', async () => {
    const { push } = fakeApi({ [`GET /api/sessions/${SESSION}/messages`]: [] })
    const changed = vi.fn()
    renderWithToasts(<Chat sessionId={SESSION} onChanged={changed} />)
    await waitFor(() => expect(screen.getByRole('textbox', { name: 'Message' })).toBeEnabled())

    push('message', { role: 'assistant', content: NOTICE, run_id: 'run_n', reply: null, session_id: SESSION })

    expect(await findToast(NOTICE)).toBeInTheDocument()
    // And in the conversation (the same text is in the toast, so pick the chat's copy).
    expect(screen.getAllByText(NOTICE).filter((el) => el.closest('article'))).toHaveLength(1)
    expect(changed).toHaveBeenCalled() // the Bookings tab shows the cancellation

    push('message', {
      role: 'assistant',
      content: 'Here is a replacement.',
      run_id: 'run_r',
      reply: reply({ kind: 'needs_approval', message: 'Here is a replacement.', approval: CARD }),
      session_id: SESSION,
    })

    const card = await screen.findByRole('group', { name: 'Approve booking?' })
    expect(messageWith('Here is a replacement.')).toContainElement(card)
  })

  it('does not show a pushed message twice when the history already has it', async () => {
    const { push } = fakeApi({
      [`GET /api/sessions/${SESSION}/messages`]: [
        { role: 'assistant', content: NOTICE, run_id: 'run_n', created_at: '' },
      ],
    })
    render(<Chat sessionId={SESSION} />)
    await screen.findByText(NOTICE)

    push('message', { role: 'assistant', content: NOTICE, run_id: 'run_n', reply: null, session_id: SESSION })
    // A later message: once it shows, the duplicate before it has been handled too.
    push('message', { role: 'assistant', content: 'Later.', run_id: 'run_l', reply: null, session_id: SESSION })
    await screen.findByText('Later.')

    expect(screen.getAllByText(NOTICE)).toHaveLength(1)
  })
})

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

  it('puts the cursor back in the message box when the reply arrives', async () => {
    const stream = liveStream()
    fakeApi({
      [`GET /api/sessions/${SESSION}/messages`]: [],
      [`POST /api/sessions/${SESSION}/messages`]: stream.response,
    })
    render(<Chat sessionId={SESSION} />)
    await type('hello')
    const box = screen.getByRole('textbox', { name: 'Message' })
    box.blur() // what a browser does to a box that is disabled mid-turn

    stream.send('final', reply({ message: 'Hi!' }))
    stream.close()

    await waitFor(() => expect(box).toHaveFocus())
  })

  it('puts the cursor back in the message box after a card decision', async () => {
    const stream = liveStream()
    fakeApi({
      [`GET /api/sessions/${SESSION}/messages`]: [],
      [`POST /api/sessions/${SESSION}/messages`]: stream.response,
      'POST /api/approvals/appr_1/decision': reply({ kind: 'answered', message: 'OK, I won’t book it.' }),
    })
    render(<Chat sessionId={SESSION} />)
    await type('book it')
    stream.send('final', reply({ kind: 'needs_approval', message: 'Shall I?', approval: CARD }))
    stream.close()
    const card = await screen.findByRole('group', { name: 'Approve booking?' })

    await userEvent.click(within(card).getByRole('button', { name: 'Decline' })) // focus moves to the button

    await screen.findByText('OK, I won’t book it.')
    await waitFor(() => expect(screen.getByRole('textbox', { name: 'Message' })).toHaveFocus())
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
