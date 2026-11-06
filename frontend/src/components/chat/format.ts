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

/** "Sat, 07 Nov, 11:00": built from parts so it doesn't depend on locale punctuation. */
export function formatSlot(iso: string): string {
  const part = Object.fromEntries(SLOT.formatToParts(new Date(iso)).map((p) => [p.type, p.value]))
  return `${part.weekday}, ${part.day} ${part.month}, ${part.hour}:${part.minute}`
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
