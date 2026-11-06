// The Trace tab: runs newest first, collapsible, one line per event, JSON on click (D4).

import {
  ArrowLeftRight,
  Brain,
  ChevronDown,
  ChevronRight,
  CircleAlert,
  Database,
  Flag,
  GitCommitHorizontal,
  Layers,
  Play,
  ShieldCheck,
  User,
  Wrench,
} from 'lucide-react'
import { type ComponentType, useState } from 'react'

import type { TraceEvent } from '@/api/types'
import { cn } from '@/lib/utils'

import { count, isAlert, newestFirst, type Runs, type RunView, seconds, summarize } from './model'

// An open run or event: tinted, with an accent bar on the left. Keyed off aria-expanded,
// so what screen readers hear and what's drawn can't disagree. The bar is an inset
// shadow, not a border, so opening a row doesn't shift its text.
const OPEN_ROW = 'aria-expanded:bg-muted aria-expanded:shadow-[inset_3px_0_0_var(--color-primary)]'

const ICON: Record<string, ComponentType<{ className?: string }>> = {
  run_started: Play,
  run_finished: Flag,
  context_sent: Layers,
  model_call: Brain,
  agent_tool: Wrench,
  tool_call: ArrowLeftRight,
  state_transition: GitCommitHorizontal,
  approval: ShieldCheck,
  memory: Database,
  profile: User,
  limit_hit: CircleAlert,
  error: CircleAlert,
}

export function TracePane({ runs }: { runs: Runs }) {
  const ordered = newestFirst(runs)
  if (ordered.length === 0) {
    return <p className="text-muted-foreground">No runs yet: each message you send is one.</p>
  }
  return (
    <div className="space-y-2 font-mono text-xs">
      {ordered.map((run, i) => (
        <RunGroup key={run.run_id} run={run} open={i === 0} />
      ))}
    </div>
  )
}

function RunGroup({ run, open: initiallyOpen }: { run: RunView; open: boolean }) {
  const [open, setOpen] = useState(initiallyOpen)
  const started = run.events.find((e) => e.type === 'run_started') as Record<string, unknown> | undefined
  const finished = run.events.find((e) => e.type === 'run_finished') as Record<string, unknown> | undefined
  const totals = finished?.totals as Record<string, number | boolean | null> | undefined
  const Chevron = open ? ChevronDown : ChevronRight

  return (
    <div role="group" aria-label={run.run_id} className="overflow-hidden rounded border">
      {/* One block for both header lines, so the open tint covers the totals too. */}
      <button
        type="button"
        aria-expanded={open}
        className={cn('block w-full px-3 py-2 text-left hover:bg-muted', OPEN_ROW)}
        onClick={() => setOpen(!open)}
      >
        <span className="flex items-center gap-2">
          <Chevron className="size-3 shrink-0" />
          <span className="truncate">{run.run_id}</span>
          <span className="text-muted-foreground">{String(started?.trigger ?? '')}</span>
          <span className={cn('ml-auto font-semibold', !finished && 'text-muted-foreground')}>
            {finished ? String(finished.outcome) : 'running…'}
          </span>
        </span>
        {totals && (
          <span className="mt-1 block pl-5 text-muted-foreground">
            {count(totals.model_calls, 'call')} ·{' '}
            {(Number(totals.input_tokens) + Number(totals.output_tokens)).toLocaleString('en-US')}{' '}
            tok · {totals.cost_usd != null && totals.cost_complete ? `$${Number(totals.cost_usd).toFixed(4)}` : 'cost n/a'}{' '}
            · {seconds(totals.duration_ms)}
          </span>
        )}
      </button>
      {open && (
        <ul className="border-t py-1.5">
          {run.events.map((e) => (
            <EventRow key={e.seq} event={e} />
          ))}
        </ul>
      )}
    </div>
  )
}

function EventRow({ event }: { event: TraceEvent }) {
  const [open, setOpen] = useState(false)
  const Icon = ICON[event.type] ?? Layers
  const alert = isAlert(event)
  const { ts: _ts, run_id: _run, session_id: _session, seq: _seq, ...payload } = event

  return (
    <li>
      <button
        type="button"
        data-alert={alert}
        aria-expanded={open}
        className={cn(
          'flex w-full items-center gap-2 px-3 py-1 text-left hover:bg-muted',
          OPEN_ROW,
          alert && 'text-destructive',
        )}
        onClick={() => setOpen(!open)}
      >
        <Icon className="size-3 shrink-0" />
        <span className="w-28 shrink-0 text-muted-foreground">{event.type}</span>
        <span className="truncate">{summarize(event)}</span>
      </button>
      {open && (
        <pre className="mx-2 my-1 max-h-64 overflow-auto rounded bg-muted p-2 whitespace-pre-wrap">
          {JSON.stringify(payload, null, 2)}
        </pre>
      )}
    </li>
  )
}
