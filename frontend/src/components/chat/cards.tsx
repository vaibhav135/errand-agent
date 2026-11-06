// Cards drawn by code from the reply's verified data, never from the model's text.

import { useState } from 'react'
import { toast } from 'sonner'

import { confirmSuggestion, rejectSuggestion } from '@/api/client'
import type { Approval, Booking, OutcomeKind, Suggestion } from '@/api/types'
import { Button } from '@/components/ui/button'
import { errorText } from '@/lib/errors'

import { formatInr, formatSlot } from './format'

const APPROVAL_TITLE: Record<Approval['action'], string> = {
  book: 'Approve booking?',
  cancel: 'Approve cancellation?',
  reschedule: 'Approve new time?',
}

function CardFrame({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div role="group" aria-label={title} className="mt-2 rounded-lg border bg-card p-3 text-sm">
      <div className="mb-1 text-xs font-medium text-muted-foreground">{title}</div>
      {children}
    </div>
  )
}

export function ApprovalCard({
  approval,
  decided,
  onDecide,
}: {
  approval: Approval
  decided?: 'approved' | 'declined'
  onDecide: (approve: boolean) => void
}) {
  const d = approval.details
  return (
    <CardFrame title={APPROVAL_TITLE[approval.action]}>
      <div className="font-medium">{d.provider_name}</div>
      <div className="text-muted-foreground">
        {formatSlot(d.slot)} · {formatInr(d.price_inr)}
      </div>
      <div className="mt-2 flex items-center gap-2">
        <Button size="sm" disabled={decided !== undefined} onClick={() => onDecide(true)}>
          Approve
        </Button>
        <Button
          size="sm"
          variant="outline"
          disabled={decided !== undefined}
          onClick={() => onDecide(false)}
        >
          Decline
        </Button>
        {decided && <span className="text-xs text-muted-foreground">{decided}</span>}
      </div>
    </CardFrame>
  )
}

const BOOKING_TITLE: Partial<Record<OutcomeKind, string>> = {
  booked: 'Booked',
  rescheduled: 'Rescheduled',
  cancelled: 'Cancelled',
  cancel_pending: 'Cancellation pending',
}

export function BookingCard({ kind, booking }: { kind: OutcomeKind; booking: Booking }) {
  return (
    <CardFrame title={BOOKING_TITLE[kind] ?? 'Booking'}>
      <div className="font-medium">{booking.provider_name}</div>
      <div className="text-muted-foreground">
        {formatSlot(booking.slot)} · {formatInr(booking.price_inr)}
      </div>
      <div className="mt-1 font-mono text-xs text-muted-foreground">{booking.booking_id}</div>
    </CardFrame>
  )
}

export function SuggestionsCard({
  suggestions,
  onChanged,
}: {
  suggestions: Suggestion[]
  onChanged?: () => void
}) {
  const [state, setState] = useState<Record<string, 'Saved' | 'Dismissed'>>({})

  const act = async (s: Suggestion, confirm: boolean) => {
    try {
      await (confirm ? confirmSuggestion(s.memory_id) : rejectSuggestion(s.memory_id))
    } catch (err) {
      // Was unhandled: a failed save left the card as it was, with no word why.
      toast.error(`Couldn't ${confirm ? 'save' : 'dismiss'} that: ${errorText(err)}`)
      return
    }
    setState((st) => ({ ...st, [s.memory_id]: confirm ? 'Saved' : 'Dismissed' }))
    toast.success(`${confirm ? 'Saved' : 'Dismissed'}: ${s.text}`)
    onChanged?.()
  }

  return (
    <CardFrame title="Suggestions from your bookings">
      <ul className="space-y-2">
        {suggestions.map((s) => (
          <li key={s.memory_id} className="flex items-center justify-between gap-3">
            <div>
              <div>{s.text}</div>
              {s.reason && <div className="text-xs text-muted-foreground">{s.reason}</div>}
            </div>
            {state[s.memory_id] ? (
              <span className="text-xs text-muted-foreground">{state[s.memory_id]}</span>
            ) : (
              <div className="flex shrink-0 gap-1">
                <Button size="sm" onClick={() => void act(s, true)}>
                  Confirm
                </Button>
                <Button size="sm" variant="outline" onClick={() => void act(s, false)}>
                  Reject
                </Button>
              </div>
            )}
          </li>
        ))}
      </ul>
    </CardFrame>
  )
}
