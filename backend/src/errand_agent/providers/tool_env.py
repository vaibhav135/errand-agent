"""ToolEnv: the eight fixed provider tools as one protocol.

What: `ToolEnv`, an async protocol whose method names, inputs, outputs and
      failure behaviours match the assignment's tool table exactly.

Why:  The workflow, agent and proactive monitor depend on this protocol, never
      on a concrete implementation, so the mock can be swapped for a real
      provider adapter without touching them, and layering tests can forbid
      those layers from importing `mocks/` at all.

How:  Every method is async (a real provider is network I/O; PydanticAI tools are
      async). Business outcomes come back as the discriminated results in
      `models.py`; transport failures raise the exceptions in `errors.py`:
      - `book` may raise `ProviderTimeout`: the outcome is unknown until
        `get_booking_status` says otherwise.
      - `cancel` may raise `ProviderUnavailable` (503): retry with the same key.
      - `get_booking_status` takes exactly one of `hold_id` / `idempotency_key`
        (see `models.booking_reference`).
"""

from datetime import datetime
from typing import Protocol

from errand_agent.providers.models import (
    Booking,
    BookingStatusResult,
    BookResult,
    CancelResult,
    DateRange,
    HoldResult,
    ProviderResult,
    RescheduleResult,
    SearchResult,
    Service,
)


class ToolEnv(Protocol):
    async def search_providers(
        self, service: Service, area: str, date_range: DateRange
    ) -> SearchResult: ...

    async def get_provider(self, provider_id: str) -> ProviderResult: ...

    async def hold_slot(self, provider_id: str, slot: datetime) -> HoldResult: ...

    async def book(self, hold_id: str, idempotency_key: str) -> BookResult: ...

    async def get_booking_status(
        self, *, hold_id: str | None = None, idempotency_key: str | None = None
    ) -> BookingStatusResult: ...

    async def cancel(self, booking_id: str, idempotency_key: str) -> CancelResult: ...

    async def reschedule(
        self, booking_id: str, slot: datetime, idempotency_key: str
    ) -> RescheduleResult: ...

    async def list_bookings(self, user_id: str) -> list[Booking]: ...
