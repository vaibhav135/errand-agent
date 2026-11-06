// The Bookings tab: upcoming and past bookings, from the provider's records.

import { getBookings } from '@/api/client'
import type { BookingRow } from '@/api/types'
import { formatInr, formatSlot } from '@/components/chat/format'
import { Badge } from '@/components/ui/badge'
import { useLoad } from '@/hooks/useLoad'

export function BookingsTab({ version }: { version: number }) {
  const { data, error } = useLoad(getBookings, version)
  if (error) return <p className="text-destructive">Couldn't load bookings: {error}</p>
  if (!data) return <p className="text-muted-foreground">Loading…</p>

  const upcoming = data.filter((b) => b.when === 'upcoming')
  const past = data.filter((b) => b.when === 'past').reverse() // most recent first
  return (
    <div className="space-y-5">
      <Group title="Upcoming" bookings={upcoming} empty="Nothing booked yet." />
      <Group title="Past" bookings={past} empty="No past bookings." />
    </div>
  )
}

function Group({ title, bookings, empty }: { title: string; bookings: BookingRow[]; empty: string }) {
  return (
    <section aria-label={title}>
      <h3 className="mb-2 text-xs font-medium tracking-wide text-muted-foreground uppercase">
        {title}
      </h3>
      {bookings.length === 0 ? (
        <p className="text-sm text-muted-foreground">{empty}</p>
      ) : (
        <ul className="divide-y rounded border">
          {bookings.map((b) => (
            <li key={b.booking_id} className="flex items-start justify-between gap-2 px-3 py-2 text-sm">
              <div>
                <div className="font-medium">{b.provider_name}</div>
                <div className="text-muted-foreground">
                  {b.service.replace('_', ' ')} · {formatSlot(b.slot)} · {formatInr(b.price_inr)}
                </div>
                <div className="font-mono text-xs text-muted-foreground">{b.booking_id}</div>
              </div>
              <Badge variant={b.status === 'confirmed' ? 'default' : 'secondary'}>
                {b.status.replaceAll('_', ' ')}
              </Badge>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
