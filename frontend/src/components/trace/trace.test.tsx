import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import type { TraceEvent } from '@/api/types'

import { mergeEvent, newestFirst, type Runs, summarize } from './model'
import { TracePane } from './TracePane'

function ev(run: string, seq: number, type: string, fields: object = {}): TraceEvent {
  return {
    ts: `2026-11-06T10:00:0${seq}Z`,
    run_id: run,
    session_id: 's',
    seq,
    type,
    ...fields,
  }
}

const TOTALS = {
  model_calls: 2,
  tool_calls: 1,
  input_tokens: 7000,
  output_tokens: 300,
  cost_usd: 0.0185,
  cost_complete: true,
  duration_ms: 4700,
}

function runsOf(...events: TraceEvent[]): Runs {
  return events.reduce(mergeEvent, {} as Runs)
}

describe('mergeEvent', () => {
  it('keeps each event once, in order, even when it arrives twice', () => {
    const runs = runsOf(ev('r1', 2, 'model_call'), ev('r1', 0, 'run_started'), ev('r1', 2, 'model_call'))

    expect(runs.r1.events.map((e) => e.seq)).toEqual([0, 2])
  })

  it('lists runs newest first', () => {
    const runs = runsOf(ev('old', 0, 'run_started'), { ...ev('new', 0, 'run_started'), ts: '2026-11-06T11:00:00Z' })

    expect(newestFirst(runs).map((r) => r.run_id)).toEqual(['new', 'old'])
  })
})

describe('summarize', () => {
  it.each([
    [ev('r', 1, 'model_call', { agent: 'orchestrator', input_tokens: 3612, estimated_input_tokens: 3700, latency_ms: 2100 }), 'orchestrator · 3,700 est / 3,612 real · 2.1 s'],
    [ev('r', 1, 'agent_tool', { tool: 'propose_booking', status: 'ok', latency_ms: 90 }), 'propose_booking · 90 ms'],
    [ev('r', 1, 'agent_tool', { tool: 'propose_booking', status: 'retry', latency_ms: 5 }), 'propose_booking · retry · 5 ms'],
    [ev('r', 1, 'tool_call', { tool: 'hold_slot', status: 'ok', latency_ms: 82 }), 'hold_slot · 82 ms'],
    [ev('r', 1, 'state_transition', { from: 'comparing', to: 'awaiting_approval' }), 'comparing → awaiting_approval'],
    [ev('r', 1, 'approval', { status: 'granted', action: 'book', via: 'button' }), 'book granted · via button'],
    [ev('r', 1, 'context_sent', { tokens: { total: 3400 }, messages_included: 3, messages_out_of_scope: 200 }), '3,400 tok · 3 messages · 200 out of scope'],
    [ev('r', 1, 'limit_hit', { limit: 'context_tokens', used: 4400, allowed: 4000 }), 'context_tokens 4400 / 4000'],
  ])('%#: %s', (event, line) => {
    expect(summarize(event)).toBe(line)
  })
})

describe('TracePane', () => {
  it('shows a run header with its outcome and totals, and one line per event', () => {
    const runs = runsOf(
      ev('run_7', 0, 'run_started', { trigger: 'user_message' }),
      ev('run_7', 1, 'agent_tool', { tool: 'propose_booking', status: 'ok', latency_ms: 90 }),
      ev('run_7', 2, 'run_finished', { outcome: 'needs_approval', totals: TOTALS }),
    )

    render(<TracePane runs={runs} />)

    const run = screen.getByRole('group', { name: /run_7/ })
    expect(run).toHaveTextContent('user_message')
    expect(run).toHaveTextContent('needs_approval')
    expect(run).toHaveTextContent('2 calls · 7,300 tok · $0.0185 · 4.7 s')
    expect(within(run).getByText('propose_booking · 90 ms')).toBeInTheDocument()
  })

  it('expands an event to its JSON on click', async () => {
    const runs = runsOf(ev('r', 1, 'agent_tool', { tool: 'list_memories', status: 'ok', latency_ms: 5, args: { include_personal_notes: true } }))
    render(<TracePane runs={runs} />)

    await userEvent.click(screen.getByRole('button', { name: /list_memories/ }))

    expect(screen.getByText(/"include_personal_notes": true/)).toBeInTheDocument()
  })

  it('marks a run still in progress, and flags limits', () => {
    const runs = runsOf(
      ev('r', 0, 'run_started', { trigger: 'user_message' }),
      ev('r', 1, 'limit_hit', { limit: 'model_turns', used: 8, allowed: 8 }),
    )

    render(<TracePane runs={runs} />)

    expect(screen.getByText('running…')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /model_turns/ })).toHaveAttribute('data-alert', 'true')
  })

  it('says when there is nothing yet', () => {
    render(<TracePane runs={{}} />)

    expect(screen.getByText(/No runs yet/)).toBeInTheDocument()
  })
})
