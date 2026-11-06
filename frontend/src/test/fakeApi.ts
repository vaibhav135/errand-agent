// A fake backend for component tests: routes fetch() calls by method and path.

import { vi } from 'vitest'

type Handler = (body: unknown) => unknown

export function fakeApi(routes: Record<string, Handler | unknown>) {
  const calls: Array<{ method: string; path: string; body: unknown }> = []
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const method = init?.method ?? 'GET'
    const path = String(input)
    const body = init?.body ? JSON.parse(String(init.body)) : undefined
    calls.push({ method, path, body })
    const route = routes[`${method} ${path}`]
    if (route === undefined) return Response.json({ detail: 'not found' }, { status: 404 })
    const value = typeof route === 'function' ? (route as Handler)(body) : route
    return value instanceof Response ? value : Response.json(value)
  })
  vi.stubGlobal('fetch', fetchMock)
  // jsdom has no EventSource: a stand-in for /api/events that a test can push through.
  const listeners: Array<{ name: string; listener: (e: MessageEvent) => void }> = []
  vi.stubGlobal(
    'EventSource',
    class {
      addEventListener(name: string, listener: (e: MessageEvent) => void) {
        listeners.push({ name, listener })
      }
      close() {}
    },
  )
  /** Send an SSE event (e.g. "message") to every open stream listening for it. */
  const push = (name: string, data: unknown) => {
    for (const l of listeners.filter((l) => l.name === name)) {
      l.listener(new MessageEvent(name, { data: JSON.stringify(data) }))
    }
  }
  return { calls, push }
}
