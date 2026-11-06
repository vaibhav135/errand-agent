// The Memory tab: inspect, correct and forget what's remembered; decide suggestions (D2).
//
// Everything here is code talking to the user: the model never sees suggestions'
// content and has no part in these decisions. Facts are grouped as booking preferences
// (what the agent uses) and personal notes (kept, but only sent to the model when the
// user asks); each says where it came from.

import { type FormEvent, useState } from 'react'
import { toast } from 'sonner'

import {
  confirmSuggestion,
  correctMemory,
  forgetMemory,
  getMemories,
  rejectSuggestion,
} from '@/api/client'
import type { Memory } from '@/api/types'
import { Button } from '@/components/ui/button'
import { useLoad } from '@/hooks/useLoad'
import { errorText } from '@/lib/errors'

const isPersonal = (m: Memory) => m.key === 'note' && m.topic !== 'home_services'
const isProvider = (m: Memory) => m.key === 'preferred_provider' || m.key === 'avoided_provider'

/** The editable value inside a fact's text ("Budget for plumber: up to ₹1500" -> "1500"). */
function currentValue(m: Memory): string {
  const value = m.text.slice(m.text.indexOf(': ') + 2)
  return m.key === 'budget_max_inr' ? value.replace(/\D/g, '') : value
}

export function MemoryTab({ version }: { version: number }) {
  const { data, error, reload } = useLoad(getMemories, version)
  if (error) return <p className="text-destructive">Couldn't load memories: {error}</p>
  if (!data) return <p className="text-muted-foreground">Loading…</p>

  const prefs = data.saved.filter((m) => !isPersonal(m))
  const personal = data.saved.filter(isPersonal)
  return (
    <div className="space-y-5">
      {data.suggestions.length > 0 && (
        <Section title="Suggestions" hint="From your bookings. Only used if you confirm.">
          {data.suggestions.map((m) => (
            <SuggestionRow key={m.memory_id} memory={m} onChanged={reload} />
          ))}
        </Section>
      )}
      <Section title="Booking preferences" hint="What the agent uses when arranging errands.">
        {prefs.map((m) => (
          <FactRow key={m.memory_id} memory={m} onChanged={reload} />
        ))}
      </Section>
      {personal.length > 0 && (
        <Section title="Personal notes" hint="Kept, but sent to the agent only if you ask.">
          {personal.map((m) => (
            <FactRow key={m.memory_id} memory={m} onChanged={reload} />
          ))}
        </Section>
      )}
    </div>
  )
}

function Section({
  title,
  hint,
  children,
}: {
  title: string
  hint: string
  children: React.ReactNode
}) {
  return (
    <section aria-label={title}>
      <h3 className="text-xs font-medium tracking-wide text-muted-foreground uppercase">{title}</h3>
      <p className="mb-2 text-xs text-muted-foreground">{hint}</p>
      <ul className="divide-y rounded border">{children}</ul>
    </section>
  )
}

function FactRow({ memory, onChanged }: { memory: Memory; onChanged: () => void }) {
  const [editing, setEditing] = useState(false)
  const [value, setValue] = useState('')
  const [confirmForget, setConfirmForget] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const startEdit = () => {
    setValue(currentValue(memory))
    setError(null)
    setEditing(true)
  }

  const save = async (e: FormEvent) => {
    e.preventDefault()
    const typed = value.trim()
    const sent = memory.key === 'budget_max_inr' && /^\d+$/.test(typed) ? Number(typed) : typed
    try {
      const updated = await correctMemory(memory.memory_id, sent)
      setEditing(false)
      toast.success(`Updated: ${updated.text}`)
      onChanged()
    } catch (err) {
      // Stays beside the field: "doesn't fit: ..." is about what you just typed.
      setError(errorText(err))
    }
  }

  const forget = async () => {
    try {
      await forgetMemory(memory.memory_id)
      toast.success(`Forgotten: ${memory.text}`)
      onChanged()
    } catch (err) {
      toast.error(`Couldn't forget that: ${errorText(err)}`)
    }
  }

  return (
    <li aria-label={memory.text} className="px-3 py-2 text-sm">
      <div className="flex items-center justify-between gap-2">
        <div>
          <div>{memory.text}</div>
          <div className="text-xs text-muted-foreground">
            {memory.source === 'explicit' ? 'you told me' : 'inferred, you confirmed'}
          </div>
        </div>
        <div className="flex shrink-0 gap-1">
          {!isProvider(memory) && !editing && (
            <Button size="sm" variant="ghost" onClick={startEdit}>
              Edit
            </Button>
          )}
          {confirmForget ? (
            <Button size="sm" variant="destructive" onClick={() => void forget()}>
              Forget? Yes
            </Button>
          ) : (
            <Button size="sm" variant="ghost" onClick={() => setConfirmForget(true)}>
              Forget
            </Button>
          )}
        </div>
      </div>
      {editing && (
        <form onSubmit={(e) => void save(e)} className="mt-2 flex gap-2">
          <input
            aria-label="New value"
            className="flex-1 rounded border bg-background px-2 py-1 text-sm"
            value={value}
            onChange={(e) => setValue(e.target.value)}
          />
          <Button size="sm" type="submit">
            Save
          </Button>
          <Button size="sm" variant="ghost" type="button" onClick={() => setEditing(false)}>
            Cancel
          </Button>
        </form>
      )}
      {error && <p className="mt-1 text-xs text-destructive">{error}</p>}
    </li>
  )
}

function SuggestionRow({ memory, onChanged }: { memory: Memory; onChanged: () => void }) {
  const decide = async (confirm: boolean) => {
    try {
      await (confirm ? confirmSuggestion(memory.memory_id) : rejectSuggestion(memory.memory_id))
      toast.success(`${confirm ? 'Saved' : 'Dismissed'}: ${memory.text}`)
      onChanged()
    } catch (err) {
      toast.error(`Couldn't ${confirm ? 'save' : 'dismiss'} that: ${errorText(err)}`)
    }
  }

  return (
    <li aria-label={memory.text} className="px-3 py-2 text-sm">
      <div className="flex items-center justify-between gap-2">
        <div>
          <div>{memory.text}</div>
          {memory.reason && <div className="text-xs text-muted-foreground">{memory.reason}</div>}
        </div>
        <div className="flex shrink-0 gap-1">
          <Button size="sm" onClick={() => void decide(true)}>
            Confirm
          </Button>
          <Button size="sm" variant="outline" onClick={() => void decide(false)}>
            Reject
          </Button>
        </div>
      </div>
    </li>
  )
}
