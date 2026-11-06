// The right-hand panel: Trace, Memory, Bookings, and Demo when the server allows it (D4).

import { useEffect, useState } from 'react'

import { getDemo } from '@/api/client'
import type { Runs } from '@/components/trace/model'
import { TracePane } from '@/components/trace/TracePane'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'

export function SidePanel({ runs }: { runs: Runs }) {
  const [demo, setDemo] = useState(false)

  useEffect(() => {
    getDemo()
      .then((d) => setDemo(d.enabled))
      .catch(() => setDemo(false))
  }, [])

  return (
    <Tabs defaultValue="trace" className="flex min-h-0 flex-1 flex-col">
      <TabsList className="mx-3 mt-2">
        <TabsTrigger value="trace">Trace</TabsTrigger>
        <TabsTrigger value="memory">Memory</TabsTrigger>
        <TabsTrigger value="bookings">Bookings</TabsTrigger>
        {demo && <TabsTrigger value="demo">Demo</TabsTrigger>}
      </TabsList>
      <TabsContent value="trace" className="min-h-0 flex-1 overflow-auto p-3 text-xs">
        <TracePane runs={runs} />
      </TabsContent>
      <TabsContent value="memory" className="min-h-0 flex-1 overflow-auto p-3 text-sm" />
      <TabsContent value="bookings" className="min-h-0 flex-1 overflow-auto p-3 text-sm" />
      {demo && <TabsContent value="demo" className="min-h-0 flex-1 overflow-auto p-3 text-sm" />}
    </Tabs>
  )
}
