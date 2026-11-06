import { describe, expect, it } from 'vitest'

import { readSse, SseParser } from './sse'

function collect(chunks: string[]) {
  const events: Array<{ event: string; data: unknown }> = []
  const parser = new SseParser((event, data) => events.push({ event, data }))
  for (const chunk of chunks) parser.feed(chunk)
  return events
}

describe('SseParser', () => {
  it('reads named events with JSON data', () => {
    const events = collect(['event: text\ndata: {"chunk":"Hi"}\n\n'])

    expect(events).toEqual([{ event: 'text', data: { chunk: 'Hi' } }])
  })

  it('reassembles frames split anywhere across network chunks', () => {
    const frame = 'event: tool\ndata: {"tool":"list_memories"}\n\nevent: text\ndata: {"chunk":"a"}\n\n'
    const events = collect([frame.slice(0, 7), frame.slice(7, 30), frame.slice(30)])

    expect(events.map((e) => e.event)).toEqual(['tool', 'text'])
  })

  it('ignores comments such as keep-alives', () => {
    const events = collect([': connected\n\n: keep-alive\n\nevent: trace\ndata: {"seq":1}\n\n'])

    expect(events).toEqual([{ event: 'trace', data: { seq: 1 } }])
  })
})

describe('readSse', () => {
  it('yields events from a streamed response body', async () => {
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        const encoder = new TextEncoder()
        controller.enqueue(encoder.encode('event: text\ndata: {"chunk":"He'))
        controller.enqueue(encoder.encode('llo"}\n\nevent: final\ndata: {"kind":"answered"}\n\n'))
        controller.close()
      },
    })

    const events = []
    for await (const event of readSse(body)) events.push(event)

    expect(events).toEqual([
      { event: 'text', data: { chunk: 'Hello' } },
      { event: 'final', data: { kind: 'answered' } },
    ])
  })
})
