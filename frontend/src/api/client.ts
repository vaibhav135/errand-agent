// The backend API (backend/src/errand_agent/api/*_routes.py), one function per route.
//
// A chat turn is a POST answered with an SSE stream (D4): `tool` (a tool is about to
// run), `text` (reply chunks), `trace` (trace events), then `final`, the
// authoritative reply that cards are drawn from. Everything else is plain JSON.

import { readSse } from './sse'
import type { BookingRow, ChatMessage, Memory, Reply, Run, TraceEvent } from './types'

export class ApiError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

async function check(res: Response): Promise<Response> {
  if (res.ok) return res
  let detail = res.statusText
  try {
    const body: { detail?: unknown } = await res.json()
    if (typeof body.detail === 'string') detail = body.detail
  } catch {
    // not JSON: keep the status text
  }
  throw new ApiError(res.status, detail)
}

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await check(
    await fetch(path, {
      ...init,
      headers: init?.body ? { 'content-type': 'application/json' } : undefined,
    }),
  )
  return res.status === 204 ? (undefined as T) : ((await res.json()) as T)
}

// --- chat --------------------------------------------------------------------------

export function createSession(): Promise<{ session_id: string }> {
  return json('/api/sessions', { method: 'POST' })
}

export function getMessages(sessionId: string): Promise<ChatMessage[]> {
  return json(`/api/sessions/${sessionId}/messages`)
}

export interface TurnHandlers {
  onTool?: (tool: string) => void
  onText?: (chunk: string) => void
  onTrace?: (event: TraceEvent) => void
  signal?: AbortSignal
}

/** Sends a message and streams the turn; resolves with the final reply. */
export async function sendMessage(
  sessionId: string,
  text: string,
  handlers: TurnHandlers,
): Promise<Reply> {
  const res = await check(
    await fetch(`/api/sessions/${sessionId}/messages`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ text }),
      signal: handlers.signal,
    }),
  )
  if (!res.body) throw new ApiError(res.status, 'the turn came back without a body')
  for await (const { event, data } of readSse(res.body, handlers.signal)) {
    if (event === 'tool') handlers.onTool?.((data as { tool: string }).tool)
    else if (event === 'text') handlers.onText?.((data as { chunk: string }).chunk)
    else if (event === 'trace') handlers.onTrace?.(data as TraceEvent)
    else if (event === 'final') return data as Reply
  }
  if (handlers.signal?.aborted) throw new DOMException('stopped', 'AbortError')
  throw new ApiError(res.status, 'the turn ended without a reply')
}

/** A card's Approve / Decline: sends back the fingerprint of the card that was shown. */
export function decide(approvalId: string, approve: boolean, fingerprint: string): Promise<Reply> {
  return json(`/api/approvals/${approvalId}/decision`, {
    method: 'POST',
    body: JSON.stringify({ approve, fingerprint }),
  })
}

// --- trace -------------------------------------------------------------------------

export function getRuns(sessionId: string): Promise<Run[]> {
  return json(`/api/sessions/${sessionId}/runs`)
}

/** Live trace events for a session (incl. runs nobody typed); returns a close function. */
export function subscribeEvents(
  sessionId: string,
  onTrace: (event: TraceEvent) => void,
): () => void {
  const source = new EventSource(`/api/events?session_id=${encodeURIComponent(sessionId)}`)
  source.addEventListener('trace', (e) => onTrace(JSON.parse((e as MessageEvent).data)))
  return () => source.close()
}

// --- memory and bookings -------------------------------------------------------------

export function getMemories(): Promise<{ saved: Memory[]; suggestions: Memory[] }> {
  return json('/api/memories')
}

export function confirmSuggestion(memoryId: string): Promise<Memory> {
  return json(`/api/memories/${memoryId}/confirm`, { method: 'POST' })
}

export function rejectSuggestion(memoryId: string): Promise<Memory> {
  return json(`/api/memories/${memoryId}/reject`, { method: 'POST' })
}

export function correctMemory(memoryId: string, value: string): Promise<Memory> {
  return json(`/api/memories/${memoryId}`, { method: 'PATCH', body: JSON.stringify({ value }) })
}

export function forgetMemory(memoryId: string): Promise<void> {
  return json(`/api/memories/${memoryId}`, { method: 'DELETE' })
}

export function getBookings(): Promise<BookingRow[]> {
  return json('/api/bookings')
}

// --- demo (only when the server runs with ERRAND_DEMO_MODE=true) ----------------------

export function getDemo(): Promise<{ enabled: boolean }> {
  return json('/api/demo')
}

export function demoAction(
  action: 'next-book' | 'provider-cancels' | 'price-drift' | 'reset',
  body?: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  return json(`/api/demo/${action}`, {
    method: 'POST',
    body: body ? JSON.stringify(body) : undefined,
  })
}
