// One session's workspace: chat beside the side panel (D4). Keyed by session id by
// its parent, so a new session starts with a clean chat and trace.

import { useState } from 'react'

import { Chat } from '@/components/chat/Chat'
import { SidePanel } from '@/components/SidePanel'
import { useTrace } from '@/hooks/useTrace'

export function Workspace({ sessionId }: { sessionId: string | null }) {
  const trace = useTrace(sessionId)
  // Bumped after a turn or a card decision: a booking or memory may have changed.
  const [version, setVersion] = useState(0)

  return (
    <main className="grid min-h-0 flex-1 grid-cols-1 md:grid-cols-2">
      <section aria-label="Chat" className="flex min-h-0 flex-col border-b md:border-r md:border-b-0">
        <Chat
          sessionId={sessionId}
          onTrace={trace.add}
          onChanged={() => setVersion((v) => v + 1)}
        />
      </section>
      <section aria-label="Side panel" className="flex min-h-0 flex-col">
        <SidePanel runs={trace.runs} version={version} />
      </section>
    </main>
  )
}
