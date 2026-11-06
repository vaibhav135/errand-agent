"""Demo routes: trigger failures live on the mock world, for a recorded demo (D4).

What: `GET /api/demo` -> {enabled}; with ERRAND_DEMO_MODE=true, `POST /api/demo/
      next-book` {outcome}, `/provider-cancels` {booking_id}, `/price-drift`
      {provider_id, fraction} and `/reset`.

Why:  The assignment's demo is "a strong plus if one failure is handled live, such as
      prompt injection or timeout-after-success". These make the provider side misbehave
      on cue (and Phase 7's provider cancellation happen), with no restart.

How:  Thin wrappers over MockControl, the mock's control plane, which the agent never
      sees (it only gets a ToolEnv). Off by default: when off, every action route is
      404 and `GET /api/demo` says {enabled: false}, so the UI hides its Demo tab.
      Misuse (cancelling a booking that isn't confirmed) is a 409 with the reason.
"""

from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from errand_agent.mocks.scenario import BookOutcome

if TYPE_CHECKING:
    from errand_agent.api.app import Services

router = APIRouter(prefix="/demo")


class NextBookIn(BaseModel):
    outcome: BookOutcome


class ProviderCancelsIn(BaseModel):
    booking_id: str
    reason: str = "technician unavailable"


class PriceDriftIn(BaseModel):
    provider_id: str
    fraction: float = Field(gt=-1.0, le=2.0)  # 0.2 = 20% more at hold time


def _services(request: Request) -> "Services":
    services: Services = request.app.state.services
    if not services.settings.demo_mode:
        raise HTTPException(404, "demo mode is off (set ERRAND_DEMO_MODE=true)")
    return services


@router.get("")
async def demo_status(request: Request) -> dict[str, bool]:
    return {"enabled": request.app.state.services.settings.demo_mode}


@router.post("/next-book")
async def next_book(body: NextBookIn, request: Request) -> dict[str, str]:
    _services(request).control.script_next_book(body.outcome)
    return {"next_book": body.outcome}


@router.post("/provider-cancels")
async def provider_cancels(body: ProviderCancelsIn, request: Request) -> dict[str, str]:
    try:
        booking = _services(request).control.cancel_by_provider(body.booking_id, body.reason)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"booking_id": booking.booking_id, "status": booking.status}


@router.post("/price-drift")
async def price_drift(body: PriceDriftIn, request: Request) -> dict[str, float | str]:
    _services(request).control.set_price_drift(body.provider_id, body.fraction)
    return {"provider_id": body.provider_id, "fraction": body.fraction}


@router.post("/reset")
async def reset(request: Request) -> dict[str, str]:
    _services(request).control.reset()
    return {"scenario": "reset"}
