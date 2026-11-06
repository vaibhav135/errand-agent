// Display helpers: slots in IST and rupees, the way the backend writes them in text.

const SLOT = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'Asia/Kolkata',
  weekday: 'short',
  day: '2-digit',
  month: 'short',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
})

const slotParts = (iso: string) =>
  Object.fromEntries(SLOT.formatToParts(new Date(iso)).map((p) => [p.type, p.value]))

/** "Sat, 07 Nov, 11:00": built from parts so it doesn't depend on locale punctuation. */
export function formatSlot(iso: string): string {
  const part = slotParts(iso)
  return `${part.weekday}, ${part.day} ${part.month}, ${part.hour}:${part.minute}`
}

/** "Sat 07 Nov": a slot's day in IST (the slots card's day tabs). */
export function slotDay(iso: string): string {
  const part = slotParts(iso)
  return `${part.weekday} ${part.day} ${part.month}`
}

/** "11:00": a slot's time in IST (the slots card's chips). */
export function slotTime(iso: string): string {
  const part = slotParts(iso)
  return `${part.hour}:${part.minute}`
}

// Message times are the viewer's local time (no timeZone option), unlike slots, which
// are always IST because that's where the providers are.
const LOCAL = new Intl.DateTimeFormat('en-GB', {
  weekday: 'short',
  day: '2-digit',
  month: 'short',
  year: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
})

const localParts = (iso: string) =>
  Object.fromEntries(LOCAL.formatToParts(new Date(iso)).map((p) => [p.type, p.value]))

/** "13:30": a message's time, under its bubble. */
export function formatTime(iso: string): string {
  const p = localParts(iso)
  return `${p.hour}:${p.minute}`
}

/** "Wed, 04 Nov 2026, 13:30:09": the full time, on hover. */
export function formatFullTime(iso: string): string {
  const p = localParts(iso)
  return `${p.weekday}, ${p.day} ${p.month} ${p.year}, ${p.hour}:${p.minute}:${p.second}`
}

/** The local calendar day, for "did the day change between these two messages?". */
export function dayKey(iso: string): string {
  const d = new Date(iso)
  return `${d.getFullYear()}-${d.getMonth() + 1}-${d.getDate()}`
}

/** "Today", "Yesterday", or "Wed, 04 Nov": the divider when the day changes. */
export function dayLabel(iso: string, now = new Date()): string {
  const yesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1)
  if (dayKey(iso) === dayKey(now.toISOString())) return 'Today'
  if (dayKey(iso) === dayKey(yesterday.toISOString())) return 'Yesterday'
  const p = localParts(iso)
  return `${p.weekday}, ${p.day} ${p.month}`
}

export function formatInr(amount: number): string {
  return `₹${amount.toLocaleString('en-IN')}`
}

/** A progress line for a tool that is about to run (the `tool` stream event). */
const PROGRESS: Record<string, string> = {
  update_requirements: 'Noting what you need…',
  search_and_compare: 'Searching providers…',
  propose_booking: 'Holding the slot…',
  get_reviews_summary: 'Reading reviews…',
  find_my_bookings: 'Looking up your bookings…',
  available_slots: 'Checking free slots…',
  start_cancel: 'Preparing the cancellation…',
  start_reschedule: 'Checking the new time…',
  decline_pending: 'Withdrawing the card…',
  remember: 'Saving that…',
  list_memories: 'Checking what I remember…',
  forget_memory: 'Forgetting that…',
  correct_memory: 'Updating that…',
  show_suggestions: 'Getting your suggestions…',
  update_profile: 'Saving your details…',
  add_address: 'Saving your address…',
}

export function progressFor(tool: string): string {
  return PROGRESS[tool] ?? 'Working…'
}
