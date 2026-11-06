// One session's workspace: chat beside the side panel (D4). Keyed by session id by
// its parent, so a new session starts with a clean chat and trace.
//
// The divider drags to resize; dragging the panel shut (or the header button) hides it
// and the chat takes the full width. The width is remembered (useSidePanel).

import { useState } from 'react'

import { Chat } from '@/components/chat/Chat'
import { SidePanel } from '@/components/SidePanel'
import { ResizableHandle, ResizablePanel, ResizablePanelGroup } from '@/components/ui/resizable'
import { savedPanelWidth, savePanelWidth } from '@/hooks/useSidePanel'
import { useTrace } from '@/hooks/useTrace'

export function Workspace({
  sessionId,
  panelOpen,
  onHidePanel,
}: {
  sessionId: string | null
  panelOpen: boolean
  onHidePanel: () => void
}) {
  const trace = useTrace(sessionId)
  // Bumped after a turn or a card decision: a booking or memory may have changed.
  const [version, setVersion] = useState(0)
  const changed = () => setVersion((v) => v + 1)
  const [width] = useState(savedPanelWidth)

  return (
    <ResizablePanelGroup
      orientation="horizontal"
      className="min-h-0 flex-1"
      onLayoutChanged={(layout) => savePanelWidth(layout.side ?? 0)}
    >
      <ResizablePanel id="chat" minSize="30%">
        <section aria-label="Chat" className="flex h-full min-h-0 flex-col">
          <Chat sessionId={sessionId} onTrace={trace.add} onChanged={changed} />
        </section>
      </ResizablePanel>
      {panelOpen && (
        <>
          <ResizableHandle withHandle />
          <ResizablePanel
            id="side"
            defaultSize={`${width}%`}
            minSize="20%"
            collapsible
            collapsedSize="0%"
            // Dragged shut: hide it for real, so the header button shows "Show".
            onResize={(size) => size.asPercentage === 0 && onHidePanel()}
          >
            <section aria-label="Side panel" className="flex h-full min-h-0 flex-col">
              <SidePanel runs={trace.runs} version={version} onChanged={changed} />
            </section>
          </ResizablePanel>
        </>
      )}
    </ResizablePanelGroup>
  )
}
