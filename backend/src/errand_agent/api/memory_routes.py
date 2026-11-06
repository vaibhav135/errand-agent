"""Memory and bookings routes: what the user can inspect and change, in code.

What: `GET /api/memories` (saved facts and suggestions), `POST /api/memories/{id}/
      confirm` and `/reject`, `PATCH /api/memories/{id}` (correct), `DELETE
      /api/memories/{id}` (forget), and `GET /api/bookings`.

Why:  The assignment: users can inspect, correct and forget stored facts, explicit and
      inferred kept apart. Suggestions are decided by the user in code, never through
      the model (D2): this is the web UI's memory panel, as `/memories`, `/confirm`
      and `/reject` are the chat's.

How:  - Saved facts carry their source ("explicit" = the user said it; "inferred" =
        from their bookings, confirmed by them); suggestions carry their reason.
      - Correcting goes through the store's provenance rule like the model's tool does:
        the value typed in the form is the user's own words, so it is both the quote
        and the message. Forgetting also scrubs the fact's words from the stored
        conversation (the store does it).
      - Errors say what happened: unknown id 404; wrong state (confirming a fact that
        isn't a suggestion) 409; a value the key can't hold ("teatime" as a time) 422.
        pydantic's ValidationError is a ValueError, so it is caught first.
      - Bookings come from the provider's records (`list_bookings`), never our copy.
"""

import json
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ValidationError

from errand_agent.memory.seedgen import DEMO_USER_ID
from errand_agent.memory.selection import describe
from errand_agent.memory.store import Memory, MemoryStore

if TYPE_CHECKING:
    from errand_agent.api.app import Services

router = APIRouter()


class CorrectionIn(BaseModel):
    value: str | int | dict[str, str]


def _store(request: Request) -> MemoryStore:
    services: Services = request.app.state.services
    return MemoryStore(services.agent_db, clock=services.clock, ids=services.ids)


def _own(store: MemoryStore, memory_id: str) -> Memory:
    try:
        m = store.get(memory_id)
    except KeyError as exc:
        raise HTTPException(404, f"no memory {memory_id!r}") from exc
    if m.user_id != DEMO_USER_ID:
        raise HTTPException(404, f"no memory {memory_id!r}")
    return m


def memory_json(m: Memory) -> dict[str, Any]:
    return {
        "memory_id": m.memory_id,
        "key": m.key,
        "scope": m.scope,
        "topic": m.topic,
        "text": describe(m),
        "source": m.source,
        "status": m.status,
        "reason": m.evidence if m.source == "inferred" else None,
    }


@router.get("/memories")
async def list_memories(request: Request) -> dict[str, list[dict[str, Any]]]:
    remembered = _store(request).list_for_user(DEMO_USER_ID)
    return {
        "saved": [memory_json(m) for m in remembered if m.status == "active"],
        "suggestions": [memory_json(m) for m in remembered if m.status == "pending_confirmation"],
    }


@router.post("/memories/{memory_id}/confirm")
async def confirm(memory_id: str, request: Request) -> dict[str, Any]:
    store = _store(request)
    _own(store, memory_id)
    try:
        return memory_json(store.confirm(memory_id))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/memories/{memory_id}/reject")
async def reject(memory_id: str, request: Request) -> dict[str, Any]:
    store = _store(request)
    _own(store, memory_id)
    try:
        return memory_json(store.reject(memory_id))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.patch("/memories/{memory_id}")
async def correct(memory_id: str, body: CorrectionIn, request: Request) -> dict[str, Any]:
    store = _store(request)
    _own(store, memory_id)
    typed = body.value if isinstance(body.value, str) else json.dumps(body.value)
    try:
        m = store.correct(memory_id, body.value, quote=typed, user_message=typed)
    except ValidationError as exc:  # before ValueError: it is one
        raise HTTPException(422, f"that value doesn't fit: {exc.errors()[0]['msg']}") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return memory_json(m)


@router.delete("/memories/{memory_id}", status_code=204)
async def forget(memory_id: str, request: Request) -> Response:
    store = _store(request)
    _own(store, memory_id)
    try:
        store.forget(memory_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return Response(status_code=204)


@router.get("/bookings")
async def bookings(request: Request) -> list[dict[str, Any]]:
    services: Services = request.app.state.services
    now = services.clock.now()
    return [
        {
            "booking_id": b.booking_id,
            "provider_name": b.provider_name,
            "service": b.service,
            "slot": b.slot.isoformat(),
            "price_inr": b.price_inr,
            "status": b.status,
            "when": "upcoming" if b.slot > now else "past",
        }
        for b in await services.world.list_bookings(DEMO_USER_ID)
    ]
