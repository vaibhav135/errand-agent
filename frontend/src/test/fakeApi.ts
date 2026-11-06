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
  // jsdom has no EventSource: a silent stand-in for the live trace stream.
  vi.stubGlobal(
    'EventSource',
    class {
      addEventListener() {}
      close() {}
    },
  )
  return { calls }
}
