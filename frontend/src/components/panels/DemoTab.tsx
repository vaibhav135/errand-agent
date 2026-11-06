// The Demo tab (D4, only when the server runs with ERRAND_DEMO_MODE=true): make the
// provider side misbehave on cue, for a recorded demo of the failure handling.
//
// Each control is one call to /api/demo/* (MockControl); the agent never sees these.
// Every action says what it set (a toast), so the person demoing knows what the next
// turn will hit.

import { useState } from 'react'
import { toast } from 'sonner'

import { demoAction, getBookings, getDemoProviders, getDemoState } from '@/api/client'
import { formatSlot } from '@/components/chat/format'
import { Button } from '@/components/ui/button'
import { useLoad } from '@/hooks/useLoad'
import { errorText } from '@/lib/errors'

const NEXT_BOOK = [
  { outcome: 'timeout_booked', label: 'Times out after succeeding' },
  { outcome: 'slot_taken', label: 'Slot taken' },
  { outcome: 'timeout_not_booked', label: 'Fails without booking' },
] as const

const FIELD = 'w-full rounded border bg-background px-2 py-1 text-sm'

/** `onChanged` reloads the other tabs (a provider cancellation changes Bookings). */
export function DemoTab({ version, onChanged }: { version: number; onChanged: () => void }) {
  const bookings = useLoad(getBookings, version)
  const providers = useLoad(getDemoProviders, version)
  // What is armed, shown pressed. `version` changes after each action here and after a
  // turn or card in the chat, so a booking that used the armed outcome un-presses it.
  const state = useLoad(getDemoState, version)
  const armedLabel = NEXT_BOOK.find((n) => n.outcome === state.data?.next_book)?.label
  const [bookingId, setBookingId] = useState('')
  const [providerId, setProviderId] = useState('')
  const [percent, setPercent] = useState('20')

  // Only these can be cancelled by a provider (MockControl refuses anything else).
  const cancellable = (bookings.data ?? []).filter(
    (b) => b.when === 'upcoming' && b.status === 'confirmed',
  )

  async function run(action: Parameters<typeof demoAction>[0], body: object | undefined, done: string) {
    try {
      await demoAction(action, body as Record<string, unknown> | undefined)
      toast.success(done)
      onChanged()
    } catch (err) {
      toast.error(`Didn't work: ${errorText(err)}`)
    }
  }

  const providerName = providers.data?.find((p) => p.provider_id === providerId)?.name

  return (
    <div className="space-y-5">
      <p className="text-muted-foreground">
        Dev only: these change how the mock providers behave. The agent can't see them.
      </p>

      <Section title="Next booking">
        <div className="flex flex-wrap gap-2">
          {NEXT_BOOK.map(({ outcome, label }) => {
            const armed = state.data?.next_book === outcome
            return (
              <Button
                key={outcome}
                size="sm"
                variant={armed ? 'default' : 'outline'} // pressed until a booking uses it
                aria-pressed={armed}
                onClick={() => run('next-book', { outcome }, `Next booking: ${label.toLowerCase()}.`)}
              >
                {label}
              </Button>
            )
          })}
        </div>
        {armedLabel && (
          <p className="text-xs text-muted-foreground">
            Armed: the next booking will hit "{armedLabel.toLowerCase()}".
          </p>
        )}
      </Section>

      <Section title="Provider cancels">
        <label className="block space-y-1">
          <span className="text-xs text-muted-foreground">Booking</span>
          <select className={FIELD} value={bookingId} onChange={(e) => setBookingId(e.target.value)}>
            <option value="">{cancellable.length ? 'Pick an upcoming booking…' : 'No upcoming bookings'}</option>
            {cancellable.map((b) => (
              <option key={b.booking_id} value={b.booking_id}>
                {b.provider_name} · {formatSlot(b.slot)}
              </option>
            ))}
          </select>
        </label>
        <Button
          size="sm"
          variant="outline"
          disabled={!bookingId}
          onClick={() =>
            run('provider-cancels', { booking_id: bookingId }, `${bookingId} was cancelled by the provider.`).then(
              () => setBookingId(''),
            )
          }
        >
          Provider cancels
        </Button>
      </Section>

      <Section title="Price change at hold">
        <label className="block space-y-1">
          <span className="text-xs text-muted-foreground">Provider</span>
          <select className={FIELD} value={providerId} onChange={(e) => setProviderId(e.target.value)}>
            <option value="">Pick a provider…</option>
            {providers.data?.map((p) => (
              <option key={p.provider_id} value={p.provider_id}>
                {p.name} ({p.service.replace('_', ' ')})
              </option>
            ))}
          </select>
        </label>
        <label className="block space-y-1">
          <span className="text-xs text-muted-foreground">Change %</span>
          <input
            type="number"
            className={FIELD}
            value={percent}
            min={-99}
            max={200}
            onChange={(e) => setPercent(e.target.value)}
          />
        </label>
        <Button
          size="sm"
          variant="outline"
          disabled={!providerId || percent === ''}
          onClick={() =>
            run(
              'price-drift',
              // The API takes a fraction (0.2 = 20% more); people think in percent.
              { provider_id: providerId, fraction: Number(percent) / 100 },
              `${providerName}'s holds will quote ${percent}% ${Number(percent) < 0 ? '' : 'more '}than search.`,
            )
          }
        >
          Change price
        </Button>
        {(state.data?.price_drift.length ?? 0) > 0 && (
          <ul aria-label="Active price changes" className="space-y-1">
            {state.data?.price_drift.map((d) => (
              <li
                key={d.provider_id}
                aria-label={d.name}
                className="flex items-center justify-between gap-2 rounded border border-primary/50 px-2 py-1"
              >
                <span>
                  {d.name}: {d.fraction > 0 ? '+' : ''}
                  {Math.round(d.fraction * 100)}% at hold
                </span>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() =>
                    run(
                      'price-drift',
                      { provider_id: d.provider_id, fraction: 0 },
                      `${d.name}'s price is back to normal.`,
                    )
                  }
                >
                  Clear
                </Button>
              </li>
            ))}
          </ul>
        )}
      </Section>

      <Button size="sm" variant="destructive" onClick={() => run('reset', undefined, 'Back to normal.')}>
        Reset
      </Button>
    </div>
  )
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section aria-label={title} className="space-y-2">
      <h3 className="text-xs font-medium tracking-wide text-muted-foreground uppercase">{title}</h3>
      {children}
    </section>
  )
}
