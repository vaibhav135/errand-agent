"""Event hub: trace events fanned out to whoever is listening, live.

What: `EventHub.publish(event)` (a Tracer subscriber) and `subscribe(session_id,
      callback)`, which returns an unsubscribe function.

Why:  The web UI shows each turn's trace as it happens (D4): the messages stream sends
      the turn's events, and `GET /events` will push what arrives unasked (Phase 7).
      The tracer already sees every event; the hub hands them on without the workflow
      knowing anyone listens.

How:  Listeners are per session (or None for every session). Publishing is synchronous
      and runs on the event loop (the tracer emits there, as all database work does),
      so callbacks must only enqueue, never block. A failing listener is dropped, never
      allowed to break the run that emitted the event.
"""

import logging
from collections.abc import Callable
from typing import Any

from errand_agent.tracing.recorder import TraceEvent

logger = logging.getLogger(__name__)

Listener = Callable[[dict[str, Any]], None]


class EventHub:
    def __init__(self) -> None:
        self._listeners: list[tuple[str | None, Listener]] = []

    def subscribe(self, session_id: str | None, listener: Listener) -> Callable[[], None]:
        entry = (session_id, listener)
        self._listeners.append(entry)

        def unsubscribe() -> None:
            if entry in self._listeners:
                self._listeners.remove(entry)

        return unsubscribe

    def publish(self, event: TraceEvent) -> None:
        data = event.to_dict()
        for entry in list(self._listeners):
            session_id, listener = entry
            if session_id is not None and data.get("session_id") != session_id:
                continue
            try:
                listener(data)
            except Exception:
                logger.exception("event listener failed; dropping it")
                self._listeners.remove(entry)
