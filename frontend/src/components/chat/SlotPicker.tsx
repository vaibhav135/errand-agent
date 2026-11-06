// The slots card: a search's options as days, then each provider's times that day.
//
// Clicking a time holds exactly that provider and slot in code (POST /pick, no model)
// and the reply brings its approval card: picking never books. One pick per card, so
// a second click can't hold a second slot. Times are IST, like every slot in the app.

import { useState } from 'react'

import type { Option } from '@/api/types'
import { cn } from '@/lib/utils'

import { formatInr, slotDay, slotTime } from './format'

export function SlotPicker({
  options,
  locked,
  onPick,
}: {
  options: Option[]
  locked: boolean
  onPick: (option: Option, slot: string) => void
}) {
  // Days in time order (slots are ISO with the same offset, so they sort as text).
  const days = [...new Set(options.flatMap((o) => o.slots).sort().map(slotDay))]
  const [day, setDay] = useState(days[0])
  const onDay = options
    .map((o) => ({ option: o, slots: o.slots.filter((s) => slotDay(s) === day) }))
    .filter((row) => row.slots.length > 0)

  return (
    <div role="group" aria-label="Pick a slot" className="mt-2 rounded-lg border bg-card p-3 text-sm">
      <div className="mb-2 text-xs font-medium text-muted-foreground">Pick a slot</div>
      <div className="mb-3 flex flex-wrap gap-1">
        {days.map((d) => (
          <button
            key={d}
            type="button"
            aria-pressed={d === day}
            onClick={() => setDay(d)}
            className={cn(
              'rounded-md border px-2 py-1 text-xs',
              d === day ? 'border-primary bg-primary text-primary-foreground' : 'hover:bg-muted',
            )}
          >
            {d}
          </button>
        ))}
      </div>
      <ul className="space-y-2">
        {onDay.map(({ option, slots }) => (
          <li key={option.provider_id}>
            <div className="mb-1">
              <span className="font-medium">{option.name}</span>{' '}
              <span className="text-muted-foreground">
                · {formatInr(option.price_inr)} · {option.rating}★
              </span>
            </div>
            <div className="flex flex-wrap gap-1">
              {slots.map((slot) => (
                <button
                  key={slot}
                  type="button"
                  disabled={locked}
                  aria-label={`${option.name} at ${day}, ${slotTime(slot)}`}
                  onClick={() => onPick(option, slot)}
                  className="rounded-md border px-2 py-1 font-mono text-xs hover:border-primary hover:bg-muted disabled:opacity-50"
                >
                  {slotTime(slot)}
                </button>
              ))}
            </div>
          </li>
        ))}
      </ul>
    </div>
  )
}
