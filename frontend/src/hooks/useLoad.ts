// Load data for a panel, again whenever `version` changes (something changed elsewhere,
// e.g. a booking from the chat) or the panel calls `reload()` after its own change.

import { useCallback, useEffect, useState } from 'react'

export function useLoad<T>(load: () => Promise<T>, version: number) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [tick, setTick] = useState(0)

  useEffect(() => {
    let cancelled = false
    load()
      .then((value) => {
        if (cancelled) return
        setData(value)
        setError(null)
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err))
      })
    return () => {
      cancelled = true
    }
  }, [load, version, tick])

  const reload = useCallback(() => setTick((t) => t + 1), [])
  return { data, error, reload }
}
