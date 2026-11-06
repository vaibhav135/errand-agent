// Chat state: the conversation, the turn being streamed, and card decisions (D4).
//
// A turn streams in two parts: `tool` events become a transient progress line, `text`
// chunks fill the reply bubble as they arrive; the `final` reply then replaces the
// bubble's text and carries the code-drawn cards. Stop aborts only the *display*: the
// turn still finishes on the server (cancelling could interrupt a booking), and its
// reply is in the history after a reload. Card decisions (Approve / Decline) are
// decided in code on the server and their reply is appended like any other.

import { useCallback, useEffect, useRef, useState } from 'react'

import { decide, getMessages, sendMessage } from '@/api/client'
import type { Approval, Reply, TraceEvent } from '@/api/types'
import { progressFor } from '@/components/chat/format'

export interface ChatItem {
  id: number
  role: 'user' | 'assistant' | 'notice'
  text: string
  reply?: Reply // cards to draw (final replies only; history has text alone)
}

export interface ChatOptions {
  onTrace?: (event: TraceEvent) => void
  onChanged?: () => void // a booking or memory may have changed: refresh other tabs
}

let nextId = 1

export function useChat(sessionId: string | null, options: ChatOptions = {}) {
  const [items, setItems] = useState<ChatItem[]>([])
  const [busy, setBusy] = useState(false)
  const [progress, setProgress] = useState<string | null>(null)
  const [decided, setDecided] = useState<Record<string, 'approved' | 'declined'>>({})
  const controller = useRef<AbortController | null>(null)
  const opts = useRef(options)
  useEffect(() => {
    opts.current = options // the latest callbacks, without re-creating the actions
  })

  // A new session mounts a fresh chat (the caller keys it by session id), so this
  // only loads the history.
  useEffect(() => {
    if (!sessionId) return
    let cancelled = false
    getMessages(sessionId)
      .then((history) => {
        if (cancelled) return
        setItems(history.map((m) => ({ id: nextId++, role: m.role, text: m.content })))
      })
      .catch(() => undefined) // a missing session is replaced by useSession
    return () => {
      cancelled = true
    }
  }, [sessionId])

  const update = (id: number, change: Partial<ChatItem>) =>
    setItems((all) => all.map((item) => (item.id === id ? { ...item, ...change } : item)))

  const send = useCallback(
    async (text: string) => {
      if (!sessionId || busy) return
      const replyId = nextId++
      setItems((all) => [
        ...all,
        { id: nextId++, role: 'user', text },
        { id: replyId, role: 'assistant', text: '' },
      ])
      setBusy(true)
      const abort = new AbortController()
      controller.current = abort
      try {
        const reply = await sendMessage(sessionId, text, {
          onTool: (tool) => setProgress(progressFor(tool)),
          onText: (chunk) =>
            setItems((all) =>
              all.map((item) => (item.id === replyId ? { ...item, text: item.text + chunk } : item)),
            ),
          onTrace: (event) => opts.current.onTrace?.(event),
          signal: abort.signal,
        })
        update(replyId, { text: reply.message, reply })
        opts.current.onChanged?.()
      } catch (err) {
        const stopped = abort.signal.aborted
        setItems((all) => [
          ...all.filter((item) => item.id !== replyId || item.text !== ''),
          {
            id: nextId++,
            role: 'notice',
            text: stopped
              ? 'Stopped showing this turn. It still finishes on the server; its reply will' +
                ' be in the conversation after a reload.'
              : `Couldn't send that: ${err instanceof Error ? err.message : String(err)}`,
          },
        ])
      } finally {
        controller.current = null
        setProgress(null)
        setBusy(false)
      }
    },
    [sessionId, busy],
  )

  const stop = useCallback(() => controller.current?.abort(), [])

  const decideCard = useCallback(async (approval: Approval, approve: boolean) => {
    setDecided((d) => ({ ...d, [approval.approval_id]: approve ? 'approved' : 'declined' }))
    try {
      const reply = await decide(approval.approval_id, approve, approval.fingerprint)
      setItems((all) => [...all, { id: nextId++, role: 'assistant', text: reply.message, reply }])
      opts.current.onChanged?.()
    } catch (err) {
      setDecided((d) => {
        const { [approval.approval_id]: _, ...rest } = d
        return rest
      })
      setItems((all) => [
        ...all,
        { id: nextId++, role: 'notice', text: `Couldn't record that: ${String(err)}` },
      ])
    }
  }, [])

  return { items, busy, progress, decided, send, stop, decideCard }
}
