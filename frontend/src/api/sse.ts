// Server-sent events over fetch (D4).
//
// The browser's EventSource can only GET, but a chat turn is a POST whose response is
// the event stream, so we read the response body ourselves. Network chunks can split
// a frame anywhere, so text is buffered until a blank line ends a frame. Lines starting
// with ":" are comments (": connected", ": keep-alive") and are skipped. Our server
// sends one `data:` line of JSON per frame.

export type SseHandler = (event: string, data: unknown) => void

export class SseParser {
  private buffer = ''
  private readonly onEvent: SseHandler

  constructor(onEvent: SseHandler) {
    this.onEvent = onEvent
  }

  feed(chunk: string): void {
    this.buffer += chunk
    let end = this.buffer.indexOf('\n\n')
    while (end !== -1) {
      this.frame(this.buffer.slice(0, end))
      this.buffer = this.buffer.slice(end + 2)
      end = this.buffer.indexOf('\n\n')
    }
  }

  private frame(text: string): void {
    let event = 'message'
    let data: string | null = null
    for (const line of text.split('\n')) {
      if (line.startsWith(':')) continue
      if (line.startsWith('event: ')) event = line.slice('event: '.length)
      else if (line.startsWith('data: ')) data = line.slice('data: '.length)
    }
    if (data !== null) this.onEvent(event, JSON.parse(data))
  }
}

/** Yields {event, data} for each frame of a streamed response body. Aborting `signal`
 * cancels the reader, so a pending read() returns at once and the loop ends. */
export async function* readSse(
  body: ReadableStream<Uint8Array>,
  signal?: AbortSignal,
): AsyncGenerator<{ event: string; data: unknown }> {
  const queue: Array<{ event: string; data: unknown }> = []
  const parser = new SseParser((event, data) => queue.push({ event, data }))
  const decoder = new TextDecoder() // stream mode: a UTF-8 character split across chunks survives
  const reader = body.getReader()
  const stop = () => void reader.cancel()
  signal?.addEventListener('abort', stop, { once: true })
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      parser.feed(decoder.decode(value, { stream: true }))
      while (queue.length > 0) yield queue.shift()!
    }
  } finally {
    signal?.removeEventListener('abort', stop)
    reader.releaseLock()
  }
}
