"""Trace routes: the live push stream and past runs, for the web UI's trace pane (D4).

What: `GET /api/events?session_id=` (SSE: every trace event of the session as it is
      emitted) and `GET /api/sessions/{id}/runs` (the session's past runs, from the
      trace files).

Why:  A turn's own stream carries its events, but some runs start with no request: the
      reconcile pass, and Phase 7's "your provider cancelled". The push stream shows
      those too. Past runs make the trace pane whole again after a page reload.

How:  - `event_stream()` subscribes to the EventHub for one session and yields SSE
        frames: a ": connected" comment first, a `trace` frame per event, and a
        ": keep-alive" comment whenever it has been quiet for HEARTBEAT_SECONDS, which
        also checks whether the client left (then it unsubscribes and ends).
      - Past runs are read from the JSONL trace files, newest MAX_RUNS, in a worker
        thread so a long history can't stall the event loop (the reader touches only
        files, never SQLite, whose connections stay on the loop's thread).
"""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from errand_agent.api.chat_routes import _user_of, sse
from errand_agent.api.events import EventHub

if TYPE_CHECKING:
    from errand_agent.api.app import Services

router = APIRouter()

HEARTBEAT_SECONDS = 15.0
MAX_RUNS = 50


async def event_stream(
    hub: EventHub,
    session_id: str | None,
    is_disconnected: Callable[[], Awaitable[bool]],
    *,
    heartbeat_seconds: float,
) -> AsyncIterator[str]:
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    unsubscribe = hub.subscribe(session_id, queue.put_nowait)
    try:
        yield ": connected\n\n"
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=heartbeat_seconds)
            except TimeoutError:
                if await is_disconnected():
                    return
                yield ": keep-alive\n\n"
                continue
            yield sse("trace", event)
    finally:
        unsubscribe()


@router.get("/events")
async def events(request: Request, session_id: str | None = None) -> StreamingResponse:
    services: Services = request.app.state.services
    stream = event_stream(
        services.hub, session_id, request.is_disconnected, heartbeat_seconds=HEARTBEAT_SECONDS
    )
    return StreamingResponse(stream, media_type="text/event-stream")


def _read_runs(trace_dir: Path, session_id: str) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for path in trace_dir.glob("*.jsonl"):
        with path.open(encoding="utf-8") as f:
            first = f.readline()
            if not first or json.loads(first).get("session_id") != session_id:
                continue
            events = [json.loads(first), *(json.loads(line) for line in f if line.strip())]
        runs.append({"run_id": events[0]["run_id"], "events": events})
    runs.sort(key=lambda r: r["events"][0]["ts"])
    return runs[-MAX_RUNS:]


async def past_runs(trace_dir: Path, session_id: str) -> list[dict[str, Any]]:
    return await asyncio.to_thread(_read_runs, trace_dir, session_id)


@router.get("/sessions/{session_id}/runs")
async def runs(session_id: str, request: Request) -> list[dict[str, Any]]:
    services: Services = request.app.state.services
    _user_of(services, session_id)
    return await past_runs(services.settings.trace_dir, session_id)
