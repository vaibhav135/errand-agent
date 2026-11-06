// Shapes the backend sends (backend/src/errand_agent/api/*_routes.py).

export type OutcomeKind =
  | 'needs_info'
  | 'options_ready'
  | 'needs_approval'
  | 'answered'
  | 'booked'
  | 'needs_reapproval'
  | 'slot_gone'
  | 'slot_taken'
  | 'not_booked'
  | 'unverified'
  | 'cancelled'
  | 'cancel_pending'
  | 'cancel_failed'
  | 'rescheduled'
  | 'slot_conflict'
  | 'reschedule_failed'
  | 'booking_changed'
  | 'limit_reached'
  | 'error'

export interface Approval {
  approval_id: string
  action: 'book' | 'cancel' | 'reschedule'
  fingerprint: string
  status: string
  details: {
    provider_name: string
    slot: string // ISO, with offset
    price_inr: number
    booking_id: string | null
  }
}

export interface Booking {
  booking_id: string
  provider_name: string
  service: string
  slot: string
  price_inr: number
  status: string
}

export interface Suggestion {
  memory_id: string
  text: string
  reason: string | null
}

/** The authoritative end of a turn: cards are drawn from this, never from text. */
/** A provider from the last search, with the slots that can be picked (the slots card). */
export interface Option {
  provider_id: string
  name: string
  price_inr: number
  rating: number
  review_count: number
  verified: boolean
  slots: string[] // ISO, with offset
  reasons: string[]
}

export interface Reply {
  run_id: string
  kind: OutcomeKind
  message: string
  approval: Approval | null
  booking: Booking | null
  suggestions: Suggestion[]
  options?: Option[] | null // a search's results, or what's left after a lost slot
}

export interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
  run_id: string | null
  created_at: string
}

/** One trace event: the envelope plus the event's own fields. */
export interface TraceEvent {
  ts: string
  run_id: string
  session_id: string
  seq: number
  type: string
  [field: string]: unknown
}

export interface Run {
  run_id: string
  events: TraceEvent[]
}

export interface Memory {
  memory_id: string
  key: string
  scope: string
  topic: string | null
  text: string
  source: 'explicit' | 'inferred'
  status: string
  reason: string | null
}

export interface BookingRow extends Booking {
  when: 'upcoming' | 'past'
}
