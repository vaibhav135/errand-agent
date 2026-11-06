import { useEffect, useState } from 'react'

import { fetchHealth, type HealthStatus } from '@/api/health'
import { SidePanel } from '@/components/SidePanel'
import { Button } from '@/components/ui/button'
import { useSession } from '@/hooks/useSession'
import { cn } from '@/lib/utils'

const STATUS_STYLE: Record<HealthStatus, string> = {
  checking: 'bg-muted-foreground',
  ok: 'bg-emerald-500',
  down: 'bg-destructive',
}

function App() {
  const [health, setHealth] = useState<HealthStatus>('checking')
  const { sessionId, newSession } = useSession()

  useEffect(() => {
    // AbortController so StrictMode's double-mount doesn't leave a stale request
    // racing to set state after unmount.
    const controller = new AbortController()
    fetchHealth(controller.signal).then(setHealth)
    return () => controller.abort()
  }, [])

  return (
    <div className="flex h-svh flex-col">
      <header className="flex items-center justify-between gap-3 border-b px-4 py-2">
        <h1 className="text-sm font-semibold">Errand Agent</h1>
        <div className="flex items-center gap-3 text-xs text-muted-foreground">
          <span className="flex items-center gap-2">
            <span className={cn('size-2 rounded-full', STATUS_STYLE[health])} />
            backend: {health}
          </span>
          <span aria-label="Session" className="hidden font-mono sm:inline">
            {sessionId ?? 'starting…'}
          </span>
          <Button size="sm" variant="outline" onClick={() => void newSession()}>
            New session
          </Button>
        </div>
      </header>

      <main className="grid min-h-0 flex-1 grid-cols-1 md:grid-cols-2">
        <section
          aria-label="Chat"
          className="flex min-h-0 flex-col border-b md:border-r md:border-b-0"
        >
          <div className="flex-1 overflow-auto p-4 text-sm text-muted-foreground">
            Chat ({sessionId ?? '…'})
          </div>
        </section>

        <section aria-label="Side panel" className="flex min-h-0 flex-col">
          <SidePanel sessionId={sessionId} />
        </section>
      </main>
    </div>
  )
}

export default App
