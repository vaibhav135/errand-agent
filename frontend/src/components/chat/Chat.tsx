// The chat pane: conversation, streamed replies with code-drawn cards, composer (D4).

import { type KeyboardEvent, useEffect, useRef, useState } from 'react'
import Markdown from 'react-markdown'

import type { TraceEvent } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { type ChatItem, useChat } from '@/hooks/useChat'
import { cn } from '@/lib/utils'

import { ApprovalCard, BookingCard, SuggestionsCard } from './cards'

export interface ChatProps {
  sessionId: string | null
  onTrace?: (event: TraceEvent) => void
  onChanged?: () => void
}

export function Chat({ sessionId, onTrace, onChanged }: ChatProps) {
  const chat = useChat(sessionId, { onTrace, onChanged })
  const [draft, setDraft] = useState('')
  const bottom = useRef<HTMLDivElement>(null)

  useEffect(() => {
    bottom.current?.scrollIntoView?.({ block: 'end' })
  }, [chat.items, chat.progress])

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
        {chat.items.map((item) => (
          <Message
            key={item.id}
            item={item}
            decided={chat.decided}
            onDecide={chat.decideCard}
            onChanged={onChanged}
          />
        ))}
        {chat.progress && (
          <div className="animate-pulse text-xs text-muted-foreground">{chat.progress}</div>
        )}
        <div ref={bottom} />
      </div>

      <div className="flex items-end gap-2 border-t p-3">
        <Textarea
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

function Message({
  item,
  decided,
  onDecide,
  onChanged,
}: {
  item: ChatItem
  decided: Record<string, 'approved' | 'declined'>
  onDecide: ReturnType<typeof useChat>['decideCard']
  onChanged?: () => void
}) {
  if (item.role === 'notice') {
    return <p className="text-xs text-muted-foreground italic">{item.text}</p>
  }
  if (item.role === 'user') {
    const action = item.text.startsWith('[') // a code-written line, e.g. a card click
    return (
      <div className="flex justify-end">
        <div
          className={cn(
            'max-w-[80%] rounded-lg px-3 py-2 text-sm whitespace-pre-wrap',
            action ? 'text-xs text-muted-foreground' : 'bg-primary text-primary-foreground',
          )}
        >
          {item.text}
        </div>
      </div>
    )
  }
  const reply = item.reply
  return (
    <div className="max-w-[85%] text-sm">
      {item.text && (
        <div className="[&_li]:my-0.5 [&_ol]:list-decimal [&_ol]:pl-5 [&_p]:my-1 [&_strong]:font-semibold [&_ul]:list-disc [&_ul]:pl-5">
          <Markdown>{item.text}</Markdown>
        </div>
      )}
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
    </div>
  )
}
