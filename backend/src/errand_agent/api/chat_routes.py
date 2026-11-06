"""Chat routes: sessions, history, and one turn answered as an SSE stream.

What: `POST /api/sessions`, `GET /api/sessions/{id}/messages` and
      `POST /api/sessions/{id}/messages`, which answers with `text/event-stream`.

Why:  The web UI shows a reply as it is written and the trace as it happens (D4). SSE
      keeps each turn an ordinary HTTP request: one lock per session, readable with
      curl, testable with the HTTP test client.

How:  - Events, one SSE frame each: `tool` {tool} just before a tool runs (a progress
        line), `text` {chunk} as reply text arrives, `trace` {event} for each trace
        event of the session, and finally `final` {kind, message, approval, booking,
        suggestions, run_id}: the authoritative reply, from which the UI draws cards.
      - The turn runs as its own task, under the session's lock (a second message
        waits). If the client disconnects, the turn still finishes and its reply is
        saved: cancelling mid-turn could interrupt a booking in flight.
      - No model key -> 503 saying what to set; unknown session -> 404.
      - History hides the code-written "[The app showed…]" note (BUG-8): it is for the
        model's next turn, not for the user's screen.
"""

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from errand_agent.agent.handler import Reply
from errand_agent.agent.models import ModelNotConfigured
from errand_agent.memory.conversation import ConversationStore
from errand_agent.memory.seedgen import DEMO_USER_ID
from errand_agent.memory.selection import describe

if TYPE_CHECKING:
    from errand_agent.api.app import Services

logger = logging.getLogger(__name__)

router = APIRouter()

SHOWN_NOTE = re.compile(r"\n\n\[The app showed the user \d+ suggestions? to review\.\]$")


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


def _services(request: Request) -> "Services":
    return request.app.state.services


def _user_of(services: "Services", session_id: str) -> str:
    row = services.agent_db.execute(
        "SELECT user_id FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, f"no session {session_id!r}")
    return row["user_id"]


def reply_json(reply: Reply) -> dict[str, Any]:
    outcome = reply.outcome
    return {
        "run_id": reply.run_id,
        "kind": outcome.kind,
        "message": outcome.message,
        "approval": outcome.approval.model_dump(mode="json") if outcome.approval else None,
        "booking": outcome.booking.model_dump(mode="json") if outcome.booking else None,
        "suggestions": [
            {"memory_id": m.memory_id, "text": describe(m), "reason": m.evidence}
            for m in reply.suggestions
        ],
    }


def sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


class DecisionIn(BaseModel):
    approve: bool
    fingerprint: str  # of the card the UI displayed: a changed card isn't approved


@router.post("/approvals/{approval_id}/decision")
async def decide(approval_id: str, body: DecisionIn, request: Request) -> dict[str, Any]:
    """A card's Approve / Decline button: decided in code, no model, so plain JSON."""
    services = _services(request)
    try:
        approval = services.approvals.get(approval_id)
    except KeyError as exc:
        raise HTTPException(404, f"no approval {approval_id!r}") from exc
    session_id = services.errands.get(approval.errand_id).session_id
    try:
        handler = services.handler()
    except ModelNotConfigured as exc:
        raise HTTPException(503, str(exc)) from exc
    async with services.lock_for(session_id):
        reply = await handler.decide(
            session_id=session_id,
            approval_id=approval_id,
            fingerprint=body.fingerprint,
            approve=body.approve,
        )
    return reply_json(reply)


@router.post("/sessions", status_code=201)
async def create_session(request: Request) -> dict[str, str]:
    services = _services(request)
    session_id = services.ids.new("sess")
    services.errands.create_session(session_id, user_id=DEMO_USER_ID)
    return {"session_id": session_id}


@router.get("/sessions/{session_id}/messages")
async def history(session_id: str, request: Request) -> list[dict[str, Any]]:
    services = _services(request)
    _user_of(services, session_id)
    store = ConversationStore(services.agent_db, clock=services.clock, ids=services.ids)
    return [
        {
            "role": m.role,
            "content": SHOWN_NOTE.sub("", m.content),
            "run_id": m.run_id,
            "created_at": m.created_at.isoformat(),
        }
        for m in store.for_session(session_id)
    ]


@router.post("/sessions/{session_id}/messages")
async def send_message(session_id: str, body: MessageIn, request: Request) -> StreamingResponse:
    services = _services(request)
    user_id = _user_of(services, session_id)
    try:
        handler = services.handler()
    except ModelNotConfigured as exc:
        raise HTTPException(503, str(exc)) from exc

    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

    async def on_text(chunk: str) -> None:
        await queue.put(("text", {"chunk": chunk}))

    async def on_tool(name: str) -> None:
        await queue.put(("tool", {"tool": name}))

    unsubscribe = services.hub.subscribe(session_id, lambda e: queue.put_nowait(("trace", e)))

    async def turn() -> None:
        try:
            async with services.lock_for(session_id):
                reply = await handler.handle(
                    session_id=session_id,
                    user_id=user_id,
                    text=body.text,
                    on_text=on_text,
                    on_tool=on_tool,
                )
            await queue.put(("final", reply_json(reply)))
        except Exception:  # the handler maps its own errors; this is the last resort
            logger.exception("turn failed outside the handler")
            await queue.put(("final", {"kind": "error", "message": "Something went wrong."}))
        finally:
            unsubscribe()

    task = asyncio.create_task(turn())
    services.background.add(task)
    task.add_done_callback(services.background.discard)

    async def stream() -> AsyncIterator[str]:
        while True:
            event, data = await queue.get()
            yield sse(event, data)
            if event == "final":
                return

    return StreamingResponse(stream(), media_type="text/event-stream")
