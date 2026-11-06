import { PanelRightClose, PanelRightOpen } from 'lucide-react'
import { useEffect, useState } from 'react'
import { toast } from 'sonner'

import { fetchHealth, type HealthStatus } from '@/api/health'
import { ThemeToggle } from '@/components/ThemeToggle'
import { Button } from '@/components/ui/button'
import { Toaster } from '@/components/ui/sonner'
import { Workspace } from '@/components/Workspace'
import { useSession } from '@/hooks/useSession'
import { useSidePanel } from '@/hooks/useSidePanel'
import { errorText } from '@/lib/errors'
import { cn } from '@/lib/utils'

const STATUS_STYLE: Record<HealthStatus, string> = {
  checking: 'bg-muted-foreground',
  ok: 'bg-emerald-500',
  down: 'bg-destructive',
}

function App() {
  const [health, setHealth] = useState<HealthStatus>('checking')
  const { sessionId, newSession } = useSession()
  const panel = useSidePanel()

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
          <Button
            size="sm"
            variant="outline"
            onClick={() =>
              newSession().then(
                () => toast.success('Started a new session'),
                (err: unknown) => toast.error(`Couldn't start a new session: ${errorText(err)}`),
              )
            }
          >
            New session
          </Button>
          <Button
            size="icon-sm"
            variant="ghost"
            aria-label={panel.open ? 'Hide side panel' : 'Show side panel'}
            title={panel.open ? 'Hide side panel' : 'Show side panel'}
            onClick={() => panel.setOpen(!panel.open)}
          >
            {panel.open ? <PanelRightClose /> : <PanelRightOpen />}
          </Button>
          <ThemeToggle />
        </div>
      </header>

      <Workspace
        key={sessionId ?? 'none'}
        sessionId={sessionId}
        panelOpen={panel.open}
        onHidePanel={() => panel.setOpen(false)}
      />
      <Toaster position="bottom-right" />
    </div>
  )
}

export default App
