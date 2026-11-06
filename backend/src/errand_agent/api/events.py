"""Event hub: trace events and chat messages fanned out to whoever is listening, live.

What: `EventHub.publish(event)` (a Tracer subscriber), `publish_message(session_id,
      message)` (a chat message nobody asked for: Phase 7's "your provider cancelled"),
      and `subscribe(session_id, listener)`, which returns an unsubscribe function.
      A listener is called with `(name, data)`: name is "trace" or "message".

Why:  The web UI shows each turn's trace as it happens (D4), and `GET /events` pushes
      what arrives unasked: runs the user didn't start, and messages the agent sends
      on its own (D6). The tracer already sees every event; the hub hands them on
      without the workflow knowing anyone listens.

How:  Listeners are per session (or None for every session). Publishing is synchronous
      and runs on the event loop (the tracer emits there, as all database work does),
      so listeners must only enqueue, never block. A failing listener is dropped, never
      allowed to break the run that emitted the event.
"""

import logging
from collections.abc import Callable
from typing import Any

from errand_agent.tracing.recorder import TraceEvent

logger = logging.getLogger(__name__)

Listener = Callable[[str, dict[str, Any]], None]  # (event name, data)


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
        self._send("trace", event.to_dict())

    def publish_message(self, session_id: str, message: dict[str, Any]) -> None:
        self._send("message", {**message, "session_id": session_id})

    def _send(self, name: str, data: dict[str, Any]) -> None:
        for entry in list(self._listeners):
            session_id, listener = entry
            if session_id is not None and data.get("session_id") != session_id:
                continue
            try:
                listener(name, data)
            except Exception:
                logger.exception("event listener failed; dropping it")
                self._listeners.remove(entry)
