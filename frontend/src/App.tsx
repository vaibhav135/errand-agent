import { useEffect, useState } from 'react'

import { fetchHealth, type HealthStatus } from '@/api/health'
import { cn } from '@/lib/utils'

const STATUS_STYLE: Record<HealthStatus, string> = {
  checking: 'bg-muted-foreground',
  ok: 'bg-emerald-500',
  down: 'bg-destructive',
}

function App() {
  const [health, setHealth] = useState<HealthStatus>('checking')

  useEffect(() => {
    // AbortController so StrictMode's double-mount doesn't leave a stale request
    // racing to set state after unmount.
    const controller = new AbortController()
    fetchHealth(controller.signal).then(setHealth)
    return () => controller.abort()
  }, [])

  return (
    <div className="flex h-svh flex-col">
      <header className="flex items-center justify-between border-b px-4 py-2">
        <h1 className="text-sm font-semibold">Errand Agent</h1>
        <span className="flex items-center gap-2 text-xs text-muted-foreground">
          <span className={cn('size-2 rounded-full', STATUS_STYLE[health])} />
          backend: {health}
        </span>
      </header>

      <main className="grid min-h-0 flex-1 grid-cols-1 md:grid-cols-2">
        <section aria-label="Chat" className="flex min-h-0 flex-col border-b md:border-r md:border-b-0">
          <h2 className="border-b px-4 py-2 text-xs font-medium text-muted-foreground uppercase">
            Chat
          </h2>
          <div className="flex-1 overflow-auto p-4 text-sm text-muted-foreground">
            Chat pane (Phase 7)
          </div>
        </section>

        <section aria-label="Trace" className="flex min-h-0 flex-col">
          <h2 className="border-b px-4 py-2 text-xs font-medium text-muted-foreground uppercase">
            Trace
          </h2>
          <div className="flex-1 overflow-auto p-4 font-mono text-xs text-muted-foreground">
            Trace pane (Phase 7)
          </div>
        </section>
      </main>
    </div>
  )
}

export default App
