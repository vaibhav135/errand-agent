// The session's trace: past runs, the live push stream, and events from the chat's own
// turn stream, merged without duplicates (an event arrives on both live paths).

import { useCallback, useEffect, useState } from 'react'

import { getRuns, subscribeEvents } from '@/api/client'
import type { TraceEvent } from '@/api/types'
import { mergeEvent, type Runs } from '@/components/trace/model'

export function useTrace(sessionId: string | null) {
  const [runs, setRuns] = useState<Runs>({})

  const add = useCallback((event: TraceEvent) => setRuns((r) => mergeEvent(r, event)), [])

  // The caller mounts a fresh trace per session (keyed by session id), so this only
  // loads and subscribes.
  useEffect(() => {
    if (!sessionId) return
    let cancelled = false
    getRuns(sessionId)
      .then((past) => {
        if (cancelled) return
        setRuns((r) => past.flatMap((run) => run.events).reduce(mergeEvent, r))
      })
      .catch(() => undefined) // no past runs to show: the live ones still arrive
    const close = subscribeEvents(sessionId, { onTrace: add }) // runs nobody typed
    return () => {
      cancelled = true
      close()
    }
  }, [sessionId, add])

  return { runs, add }
}
