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

import { isAlert, newestFirst, type Runs, type RunView, seconds, summarize } from './model'

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
    <div role="group" aria-label={run.run_id} className="rounded border">
      <button
        type="button"
        className="flex w-full items-center gap-2 px-2 py-1.5 text-left hover:bg-muted"
        onClick={() => setOpen(!open)}
      >
        <Chevron className="size-3 shrink-0" />
        <span className="truncate">{run.run_id}</span>
        <span className="text-muted-foreground">{String(started?.trigger ?? '')}</span>
        <span className={cn('ml-auto font-semibold', !finished && 'text-muted-foreground')}>
          {finished ? String(finished.outcome) : 'running…'}
        </span>
      </button>
      {totals && (
        <div className="px-7 pb-1 text-muted-foreground">
          {totals.model_calls} calls ·{' '}
          {(Number(totals.input_tokens) + Number(totals.output_tokens)).toLocaleString('en-US')}{' '}
          tok · {totals.cost_usd != null && totals.cost_complete ? `$${Number(totals.cost_usd).toFixed(4)}` : 'cost n/a'}{' '}
          · {seconds(totals.duration_ms)}
        </div>
      )}
      {open && (
        <ul className="border-t py-1">
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
        className={cn(
          'flex w-full items-center gap-2 px-2 py-0.5 text-left hover:bg-muted',
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
