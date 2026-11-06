// The chat pane: conversation, streamed replies with code-drawn cards, composer (D4).

import { Fragment, type KeyboardEvent, useEffect, useRef, useState } from 'react'
import Markdown from 'react-markdown'

import type { Option, TraceEvent } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { type ChatItem, useChat } from '@/hooks/useChat'
import { useSmoothText } from '@/hooks/useSmoothText'
import { cn } from '@/lib/utils'

import { ApprovalCard, BookingCard, SuggestionsCard } from './cards'
import { SlotPicker } from './SlotPicker'
import { dayKey, dayLabel, formatFullTime, formatTime } from './format'

export interface ChatProps {
  sessionId: string | null
  onTrace?: (event: TraceEvent) => void
  onChanged?: () => void
}

export function Chat({ sessionId, onTrace, onChanged }: ChatProps) {
  const chat = useChat(sessionId, { onTrace, onChanged })
  const [draft, setDraft] = useState('')
  const bottom = useRef<HTMLDivElement>(null)
  const input = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    bottom.current?.scrollIntoView?.({ block: 'end' })
  }, [chat.items, chat.progress])

  // The box is disabled during a turn, which drops its focus; give it back when the
  // reply has arrived (this runs after the render that re-enables it).
  useEffect(() => {
    if (!chat.busy) input.current?.focus()
  }, [chat.busy])

  const decideCard: typeof chat.decideCard = async (approval, approve) => {
    await chat.decideCard(approval, approve)
    input.current?.focus() // the click left focus on the card's button
  }

  const pickSlot: typeof chat.pickSlot = async (messageId, option, slot) => {
    await chat.pickSlot(messageId, option, slot)
    input.current?.focus() // same: the click left focus on the slot's chip
  }

  const submit = () => {
    const text = draft.trim()
    if (!text || chat.busy) return
    setDraft('')
    void chat.send(text)
  }

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault() // Enter sends; Shift+Enter is a new line
      submit()
    }
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex-1 space-y-3 overflow-auto p-4">
        {chat.items.map((item, i) => (
          <Fragment key={item.id}>
            {startsNewDay(chat.items, i) && <DayDivider at={item.at!} />}
            <Message
              item={item}
              // Nothing to read yet and no tool line: show that the agent is working.
              typing={!!item.streaming && !item.text && !chat.progress}
              decided={chat.decided}
              onDecide={decideCard}
              picked={!!chat.picked[item.id]}
              onPick={(option, slot) => void pickSlot(item.id, option, slot)}
              onChanged={onChanged}
            />
          </Fragment>
        ))}
        {chat.progress && (
          <div className="animate-pulse text-xs text-muted-foreground">{chat.progress}</div>
        )}
        <div ref={bottom} />
      </div>

      <div className="flex items-end gap-2 border-t p-3">
        <Textarea
          ref={input}
          aria-label="Message"
          placeholder="Ask for a plumber, an electrician, AC servicing…"
          className="max-h-40 min-h-10 resize-none"
          rows={1}
          value={draft}
          disabled={!sessionId || chat.busy}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={onKeyDown}
        />
        {chat.busy ? (
          <Button variant="outline" onClick={chat.stop}>
            Stop
          </Button>
        ) : (
          <Button onClick={submit} disabled={!draft.trim()}>
            Send
          </Button>
        )}
      </div>
    </div>
  )
}

/** Whether item i is the first dated message of its day (undated ones, like a reply
 * still streaming or a notice, never start a day). */
function startsNewDay(items: ChatItem[], i: number): boolean {
  const at = items[i].at
  if (!at || items[i].role === 'notice') return false
  const previous = items.slice(0, i).findLast((item) => item.at && item.role !== 'notice')
  return !previous || dayKey(previous.at!) !== dayKey(at)
}

function DayDivider({ at }: { at: string }) {
  return (
    <div role="separator" className="flex items-center gap-3 py-1 text-xs text-muted-foreground">
      <span className="h-px flex-1 bg-border" />
      <span>{dayLabel(at)}</span>
      <span className="h-px flex-1 bg-border" />
    </div>
  )
}

function Time({ at }: { at?: string }) {
  if (!at) return null
  return (
    <time dateTime={at} title={formatFullTime(at)} className="mt-1 px-1 text-[11px] text-muted-foreground">
      {formatTime(at)}
    </time>
  )
}

function TypingDots() {
  return (
    <article className="flex flex-col items-start">
      <div
        role="status"
        aria-label="Agent is typing"
        className="flex gap-1 rounded-lg bg-muted px-3 py-3"
      >
        {[0, 150, 300].map((delay) => (
          <span
            key={delay}
            className="size-1.5 animate-bounce rounded-full bg-muted-foreground"
            style={{ animationDelay: `${delay}ms` }}
          />
        ))}
      </div>
    </article>
  )
}

function Message({
  item,
  typing,
  decided,
  onDecide,
  picked,
  onPick,
  onChanged,
}: {
  item: ChatItem
  typing: boolean
  decided: Record<string, 'approved' | 'declined'>
  onDecide: ReturnType<typeof useChat>['decideCard']
  picked: boolean // this reply's slots card was used
  onPick: (option: Option, slot: string) => void
  onChanged?: () => void
}) {
  // Called before any return: hooks must run in the same order on every render.
  const text = useSmoothText(item.text, item.role === 'assistant' && !!item.streaming)
  if (typing) return <TypingDots />
  if (item.role === 'notice') {
    return <p className="text-xs text-muted-foreground italic">{item.text}</p>
  }
  if (item.role === 'user') {
    const action = item.text.startsWith('[') // a code-written line, e.g. a card click
    return (
      <article className="flex flex-col items-end">
        <div
          data-bubble={action ? undefined : 'user'}
          className={cn(
            'max-w-[80%] rounded-lg px-3 py-2 text-sm whitespace-pre-wrap',
            action ? 'text-xs text-muted-foreground' : 'bg-primary text-primary-foreground',
          )}
        >
          {item.text}
        </div>
        {!action && <Time at={item.at} />}
      </article>
    )
  }
  const reply = item.reply
  // The reply's text is a bubble (surface colour, so the user's stay the accented ones);
  // its cards follow it, not inside it, so they aren't boxes within a box.
  return (
    <article className="flex max-w-[85%] flex-col items-start text-sm">
      {text && (
        <div
          data-bubble="assistant"
          data-streaming={item.streaming ? 'true' : 'false'} // the caret: index.css
          className="rounded-lg bg-muted px-3 py-2 [&_li]:my-0.5 [&_ol]:list-decimal [&_ol]:pl-5 [&_p]:my-1 [&_p:first-child]:mt-0 [&_p:last-child]:mb-0 [&_strong]:font-semibold [&_ul]:list-disc [&_ul]:pl-5"
        >
          <Markdown>{text}</Markdown>
        </div>
      )}
      <div className="w-full">
        {reply?.approval && (
          <ApprovalCard
            approval={reply.approval}
            decided={decided[reply.approval.approval_id]}
            onDecide={(approve) => void onDecide(reply.approval!, approve)}
          />
        )}
        {reply?.booking && <BookingCard kind={reply.kind} booking={reply.booking} />}
        {reply && reply.suggestions.length > 0 && (
          <SuggestionsCard suggestions={reply.suggestions} onChanged={onChanged} />
        )}
        {reply?.options && reply.options.length > 0 && (
          <SlotPicker options={reply.options} locked={picked} onPick={onPick} />
        )}
      </div>
      <Time at={item.at} />
    </article>
  )
}
