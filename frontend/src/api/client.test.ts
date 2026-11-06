import { afterEach, describe, expect, it, vi } from 'vitest'

import { ApiError, decide, sendMessage } from './client'

function sseResponse(frames: string): Response {
  return new Response(frames, { headers: { 'content-type': 'text/event-stream' } })
}

afterEach(() => vi.unstubAllGlobals())

describe('sendMessage', () => {
  it('routes progress, text and trace events, then returns the final reply', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        sseResponse(
          'event: tool\ndata: {"tool":"search_and_compare"}\n\n' +
            'event: trace\ndata: {"type":"model_call","seq":3}\n\n' +
            'event: text\ndata: {"chunk":"Found "}\n\n' +
            'event: text\ndata: {"chunk":"three."}\n\n' +
            'event: final\ndata: {"kind":"answered","message":"Found three.","run_id":"run_1",' +
            '"approval":null,"booking":null,"suggestions":[]}\n\n',
        ),
      ),
    )
    const seen: string[] = []

    const reply = await sendMessage('sess_1', 'plumber?', {
      onTool: (tool) => seen.push(`tool:${tool}`),
      onText: (chunk) => seen.push(`text:${chunk}`),
      onTrace: (event) => seen.push(`trace:${event.type}`),
    })

    expect(seen).toEqual([
      'tool:search_and_compare',
      'trace:model_call',
      'text:Found ',
      'text:three.',
    ])
    expect(reply.message).toBe('Found three.')
    expect(fetch).toHaveBeenCalledWith(
      '/api/sessions/sess_1/messages',
      expect.objectContaining({ method: 'POST', body: JSON.stringify({ text: 'plumber?' }) }),
    )
  })

  it('surfaces the server’s explanation when a turn is refused', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        Response.json({ detail: 'ANTHROPIC_API_KEY is not set' }, { status: 503 }),
      ),
    )

    await expect(sendMessage('sess_1', 'hi', {})).rejects.toThrow(
      new ApiError(503, 'ANTHROPIC_API_KEY is not set'),
    )
  })

  it('fails loudly if the stream ends without a final reply', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => sseResponse('event: text\ndata: {"chunk":"a"}\n\n')))

    await expect(sendMessage('sess_1', 'hi', {})).rejects.toThrow(/ended without a reply/)
  })
})

describe('decide', () => {
  it('sends back the fingerprint of the card that was shown', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => Response.json({ kind: 'booked', message: 'Booked.', suggestions: [] })),
    )

    const reply = await decide('appr_1', true, 'fp_shown')

    expect(reply.kind).toBe('booked')
    expect(fetch).toHaveBeenCalledWith(
      '/api/approvals/appr_1/decision',
      expect.objectContaining({ body: JSON.stringify({ approve: true, fingerprint: 'fp_shown' }) }),
    )
  })
})
