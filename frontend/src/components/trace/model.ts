// Trace data for the Trace tab: runs merged from several sources, and one-line summaries.
//
// A turn's events arrive twice (its own SSE stream, and the /api/events push), past
// runs come from /api/sessions/{id}/runs, so events are merged by (run_id, seq) and
// kept in seq order. Summaries pick each event type's key fact (backend event table:
// docs/ARCHITECTURE.md, "Run lifecycle").

import type { TraceEvent } from '@/api/types'
import { formatSlot } from '@/components/chat/format'

export interface RunView {
  run_id: string
  events: TraceEvent[]
}

export type Runs = Record<string, RunView>

export function mergeEvent(runs: Runs, event: TraceEvent): Runs {
  const run = runs[event.run_id] ?? { run_id: event.run_id, events: [] }
  if (run.events.some((e) => e.seq === event.seq)) return runs
  const events = [...run.events, event].sort((a, b) => a.seq - b.seq)
  return { ...runs, [event.run_id]: { ...run, events } }
}

export function newestFirst(runs: Runs): RunView[] {
  const started = (r: RunView) => r.events[0]?.ts ?? ''
  return Object.values(runs).sort((a, b) => started(b).localeCompare(started(a)))
}

const n = (value: unknown) => Number(value ?? 0).toLocaleString('en-US')

/** "1 call", "2 calls": a count with its noun. */
export function count(value: unknown, noun: string): string {
  const num = Number(value ?? 0)
  return `${n(num)} ${noun}${num === 1 ? '' : 's'}`
}

export function seconds(ms: unknown): string {
  return `${(Number(ms ?? 0) / 1000).toFixed(1)} s`
}

/** The key fact of an event, on one line. */
export function summarize(e: TraceEvent): string {
  const f = e as Record<string, unknown>
  switch (e.type) {
    case 'run_started':
      return `trigger: ${f.trigger}`
    case 'run_finished':
      return `outcome: ${f.outcome}`
    case 'model_call': {
      const estimate = f.estimated_input_tokens == null ? '–' : n(f.estimated_input_tokens)
      // How much of the input came from the prompt cache (D5), when any did.
      const cached = Number(f.cache_read_tokens ?? 0) > 0 ? `${n(f.cache_read_tokens)} cached · ` : ''
      return `${f.agent} · ${estimate} est / ${n(f.input_tokens)} real · ${cached}${seconds(f.latency_ms)}`
    }
    case 'agent_tool':
    case 'tool_call':
      return [f.tool, f.status === 'ok' ? null : f.status, `${f.latency_ms} ms`]
        .filter(Boolean)
        .join(' · ')
    case 'state_transition':
      return `${f.from} → ${f.to}`
    case 'approval':
      return `${f.action} ${f.status}${f.via ? ` · via ${f.via}` : ''}`
    case 'context_sent': {
      const tokens = f.tokens as { total?: number } | undefined
      return `${n(tokens?.total)} tok · ${count(f.messages_included, 'message')} · ${n(f.messages_out_of_scope)} out of scope`
    }
    case 'limit_hit':
      return `${f.limit} ${f.used} / ${f.allowed}`
    case 'memory':
      return `${f.event ?? f.action ?? ''} ${f.key ?? ''}`.trim()
    case 'error':
      return String(f.message ?? f.error ?? 'error')
    case 'provider_cancellation': // the monitor saw it, unasked (D6)
      return `${f.provider_name} cancelled · ${String(f.service).replaceAll('_', ' ')} · ${formatSlot(String(f.slot))}`
    case 'notice_decision':
      return `${f.decision} · ${f.reason}`
    default:
      return ''
  }
}

export function isAlert(e: TraceEvent): boolean {
  const status = (e as Record<string, unknown>).status
  return e.type === 'limit_hit' || e.type === 'error' || status === 'error'
}
