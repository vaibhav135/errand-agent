// The chat session: remembered across reloads, replaced on "New session".
//
// The id lives in localStorage (a per-browser convenience, not a source of truth). On
// load, a remembered session is checked against the server; if it's gone (e.g. the
// data directory was reset), a fresh one is created instead of showing errors.

import { useCallback, useEffect, useState } from 'react'

import { ApiError, createSession, getMessages } from '@/api/client'

const KEY = 'errand.session'

function remembered(): string | null {
  try {
    return localStorage.getItem(KEY)
  } catch {
    return null // storage blocked (private window): just start a new session
  }
}

function remember(id: string): void {
  try {
    localStorage.setItem(KEY, id)
  } catch {
    // storage blocked: the session still works, it just won't survive a reload
  }
}

export function useSession(): { sessionId: string | null; newSession: () => Promise<void> } {
  const [sessionId, setSessionId] = useState<string | null>(null)

  const newSession = useCallback(async () => {
    const { session_id } = await createSession()
    remember(session_id)
    setSessionId(session_id)
  }, [])

  useEffect(() => {
    let cancelled = false
    const stored = remembered()
    const start = async () => {
      if (stored) {
        try {
          await getMessages(stored)
          if (!cancelled) setSessionId(stored)
          return
        } catch (err) {
          if (!(err instanceof ApiError && err.status === 404)) throw err
        }
      }
      if (!cancelled) await newSession()
    }
    void start()
    return () => {
      cancelled = true
    }
  }, [newSession])

  return { sessionId, newSession }
}
