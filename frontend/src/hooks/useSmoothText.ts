// Reveal streamed text at a steady pace instead of in bursts (BUG-10).
//
// Chunks arrive in bursts: a ~50-token reply is generated in about half a second, so
// it would otherwise appear all at once. Each animation frame reveals a share of what
// is still hidden: at least MIN_CHARS (a fast typist's pace), and more when far behind,
// so the gap shrinks by about 1/CATCH_UP every frame and closes within ~0.5 s. It never
// waits for text that hasn't arrived, so it can't make a reply slower than the model.
// Once the reply is final, the whole text shows at once.

import { useEffect, useRef, useState } from 'react'

const MIN_CHARS = 2 // per frame: ~120 characters a second at 60 fps
const CATCH_UP = 12 // remaining gap after 30 frames (0.5 s): (11/12)^30, about 7%

export function useSmoothText(target: string, streaming: boolean): string {
  const [shown, setShown] = useState(0)
  // The animation loop's own state (used in the frame callback, never during render):
  // how much is revealed, and the pending frame to cancel on cleanup.
  const revealed = useRef(0)
  const frame = useRef(0)

  useEffect(() => {
    if (!streaming) return
    const tick = () => {
      const behind = target.length - revealed.current
      if (behind <= 0) return // caught up: wait for the next chunk
      revealed.current += Math.max(MIN_CHARS, Math.ceil(behind / CATCH_UP))
      setShown(Math.min(revealed.current, target.length))
      frame.current = requestAnimationFrame(tick)
    }
    frame.current = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(frame.current)
  }, [target, streaming])

  return streaming ? target.slice(0, shown) : target
}
