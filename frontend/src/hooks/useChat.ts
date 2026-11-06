// Chat state: the conversation, the turn being streamed, and card decisions (D4).
//
// A turn streams in two parts: `tool` events become a transient progress line, `text`
// chunks fill the reply bubble as they arrive; the `final` reply then replaces the
// bubble's text and carries the code-drawn cards. Stop aborts only the *display*: the
// turn still finishes on the server (cancelling could interrupt a booking), and its
// reply is in the history after a reload. Card decisions (Approve / Decline) are
// decided in code on the server and their reply is appended like any other.

import { useCallback, useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'

import {
  decide,
  getMessages,
  getPending,
  pickSlot as pickSlotApi,
  sendMessage,
  subscribeEvents,
} from '@/api/client'
import type { Approval, Option, Reply, TraceEvent } from '@/api/types'
import { progressFor } from '@/components/chat/format'
import { errorText } from '@/lib/errors'

export interface ChatItem {
  id: number
  role: 'user' | 'assistant' | 'notice'
  text: string
  reply?: Reply // cards to draw (final replies only; history has text alone)
  /** When it was said (ISO): the server's time for history, the browser's for new
   * messages; a reply still streaming has none yet. */
  at?: string
  /** A reply still coming in: shows the typing dots, then the caret (BUG-10). */
  streaming?: boolean
  /** The run that said it: matches a pushed message to one already in the history. */
  runId?: string
}

const now = () => new Date().toISOString()

/** A card still waiting, as the reply it belongs to (the history keeps text only). */
const waiting = (approval: Approval, message: string): Reply => ({
  run_id: '',
  kind: 'needs_approval',
  message,
  approval,
  booking: null,
  suggestions: [],
})

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
  // only loads the history, with any card still waiting (made while the user was away,
  // D6) drawn under the latest reply.
  useEffect(() => {
    if (!sessionId) return
    let cancelled = false
    Promise.all([getMessages(sessionId), getPending(sessionId).catch(() => [] as Approval[])])
      .then(([history, pending]) => {
        if (cancelled) return
        const loaded: ChatItem[] = history.map((m) => ({
          id: nextId++,
          role: m.role,
          text: m.content,
          at: m.created_at || undefined,
          runId: m.run_id ?? undefined,
        }))
        const card = pending.at(-1)
        const last = loaded.findLastIndex((item) => item.role === 'assistant')
        if (card && last >= 0) loaded[last] = { ...loaded[last], reply: waiting(card, loaded[last].text) }
        setItems(loaded)
      })
      .catch(() => undefined) // a missing session is replaced by useSession
    return () => {
      cancelled = true
    }
  }, [sessionId])

  // Messages the agent sends unasked (a provider cancelled: D6), live on /api/events.
  useEffect(() => {
    if (!sessionId) return
    return subscribeEvents(sessionId, {
      onMessage: (m) => {
        setItems((all) =>
          // Already there: saved just as the history loaded.
          all.some((item) => item.runId === m.run_id && item.text === m.content)
            ? all
            : [
                ...all,
                {
                  id: nextId++,
                  role: 'assistant',
                  text: m.content,
                  reply: m.reply ?? undefined,
                  at: now(),
                  runId: m.run_id,
                },
              ],
        )
        if (!m.reply) toast.info(m.content) // the news itself; a card speaks for itself
        opts.current.onChanged?.() // e.g. the Bookings tab shows the cancellation
      },
    })
  }, [sessionId])

  const update = (id: number, change: Partial<ChatItem>) =>
    setItems((all) => all.map((item) => (item.id === id ? { ...item, ...change } : item)))

  const send = useCallback(
    async (text: string) => {
      if (!sessionId || busy) return
      const replyId = nextId++
      setItems((all) => [
        ...all,
        { id: nextId++, role: 'user', text, at: now() },
        { id: replyId, role: 'assistant', text: '', streaming: true },
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
        update(replyId, { text: reply.message, reply, at: now(), streaming: false })
        opts.current.onChanged?.()
      } catch (err) {
        const stopped = abort.signal.aborted
        setItems((all) => [
          // Keep what streamed so far (no longer streaming); drop an empty placeholder.
          ...all
            .filter((item) => item.id !== replyId || item.text !== '')
            .map((item) => (item.id === replyId ? { ...item, streaming: false } : item)),
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
      setItems((all) => [
        ...all,
        { id: nextId++, role: 'assistant', text: reply.message, reply, at: now() },
      ])
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

  // A slot clicked on a slots card (keyed by the reply it belongs to): held in code,
  // and the reply brings its approval card. A used card locks: one hold per card.
  const [picked, setPicked] = useState<Record<number, true>>({})
  const pickSlot = useCallback(
    async (messageId: number, option: Option, slot: string) => {
      if (!sessionId) return
      setPicked((p) => ({ ...p, [messageId]: true }))
      try {
        const reply = await pickSlotApi(sessionId, option.provider_id, slot)
        setItems((all) => [
          ...all,
          { id: nextId++, role: 'assistant', text: reply.message, reply, at: now() },
        ])
        opts.current.onChanged?.()
      } catch (err) {
        setPicked((p) => {
          const { [messageId]: _, ...rest } = p // nothing was held: let them try again
          return rest
        })
        setItems((all) => [
          ...all,
          { id: nextId++, role: 'notice', text: `Couldn't hold that slot: ${errorText(err)}` },
        ])
      }
    },
    [sessionId],
  )

  return { items, busy, progress, decided, send, stop, decideCard, picked, pickSlot }
}
