"""GuardedToolEnv: every provider call traced, every run bounded.

What: `RunBudget` (max model turns, tool calls, wall-clock seconds per run),
      `BudgetMeter` (counts and enforces them), `LimitExceeded`, and
      `GuardedToolEnv`, a `ToolEnv` wrapper that charges the meter and records a
      `tool_call` trace span (args, sanitized result, status, latency) per call.

Why:  The assignment requires hard limits on model turns, tool calls and
      wall-clock time, and a trace with tool names, arguments, sanitized results
      and latency. Doing both in one wrapper means no call can skip either.

How:  - Limits are checked *before* a call starts; a call already in flight is never
        cut off, because interrupting a side effect (book/cancel/reschedule) would
        create exactly the "did it happen?" ambiguity the workflow works to avoid.
        In-flight calls are bounded by the provider's own timeout.
      - get_booking_status is budget-exempt (still counted and traced): after a
        side effect, finding out what happened must always be possible. Callers
        bound it themselves (the executor makes at most a few status checks).
      - Hitting a limit emits a `limit_hit` trace event and raises LimitExceeded;
        the run then ends with a clear outcome for the user.
      - Model turns are charged by the agent loop (Phase 5) on the same meter.
"""

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from errand_agent.core.clock import Clock
from errand_agent.core.config import Settings
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
from errand_agent.providers.tool_env import ToolEnv
from errand_agent.tracing.recorder import RunTrace

LimitName = Literal["model_turns", "tool_calls", "wall_clock"]


class RunBudget(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_model_turns: int = 8
    max_tool_calls: int = 20
    max_seconds: float = 60.0

    @classmethod
    def from_settings(cls, settings: Settings) -> "RunBudget":
        return cls(
            max_model_turns=settings.max_model_turns,
            max_tool_calls=settings.max_tool_calls,
            max_seconds=settings.max_run_seconds,
        )


class LimitExceeded(Exception):
    def __init__(self, limit: LimitName, used: float, allowed: float, blocked: str) -> None:
        super().__init__(f"{limit} limit reached ({used}/{allowed}); blocked {blocked}")
        self.limit = limit
        self.used = used
        self.allowed = allowed
        self.blocked = blocked


class BudgetMeter:
    def __init__(self, budget: RunBudget, *, clock: Clock, run: RunTrace) -> None:
        self._budget = budget
        self._clock = clock
        self._run = run
        self._started = clock.monotonic()
        self.tool_calls = 0
        self.model_turns = 0
        self._committed = 0

    @property
    def budget(self) -> RunBudget:
        return self._budget

    def elapsed(self) -> float:
        return self._clock.monotonic() - self._started

    def ensure_room(self, tool: str) -> None:
        """Raise LimitExceeded now if `tool` could not start, without charging it.

        Used just before a commit point: if the side effect can't start, stop *before*
        anything is committed rather than after the approval was consumed.
        """
        if self.tool_calls >= self._budget.max_tool_calls:
            self._hit("tool_calls", self.tool_calls, self._budget.max_tool_calls, tool)
        self._check_time(tool)

    @contextmanager
    def committed(self) -> Iterator[None]:
        """Inside this block calls are counted but never blocked: once a side effect
        is committed, finishing it (retries, status checks) beats stopping half-way."""
        self._committed += 1
        try:
            yield
        finally:
            self._committed -= 1

    def charge_tool_call(self, tool: str, *, exempt: bool = False) -> None:
        if not (exempt or self._committed):
            if self.tool_calls >= self._budget.max_tool_calls:
                self._hit("tool_calls", self.tool_calls, self._budget.max_tool_calls, tool)
            self._check_time(tool)
        self.tool_calls += 1

    def charge_model_turn(self) -> None:
        if self.model_turns >= self._budget.max_model_turns:
            self._hit("model_turns", self.model_turns, self._budget.max_model_turns, "model_turn")
        self._check_time("model_turn")
        self.model_turns += 1

    def _check_time(self, blocked: str) -> None:
        if self.elapsed() > self._budget.max_seconds:
            self._hit("wall_clock", round(self.elapsed(), 3), self._budget.max_seconds, blocked)

    def _hit(self, limit: LimitName, used: float, allowed: float, blocked: str) -> None:
        self._run.emit(
            "limit_hit", {"limit": limit, "used": used, "allowed": allowed, "blocked": blocked}
        )
        raise LimitExceeded(limit, used, allowed, blocked)


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value


class GuardedToolEnv:
    def __init__(self, inner: ToolEnv, *, run: RunTrace, meter: BudgetMeter) -> None:
        self._inner = inner
        self._run = run
        self._meter = meter

    async def _call[T](
        self,
        tool: str,
        args: dict[str, Any],
        call: Callable[[], Awaitable[T]],
        *,
        exempt: bool = False,
    ) -> T:
        self._meter.charge_tool_call(tool, exempt=exempt)
        fields: dict[str, Any] = {"tool": tool, "args": _jsonable(args)}
        if exempt:
            fields["budget_exempt"] = True
        with self._run.span("tool_call", **fields) as span:
            result = await call()
            span.set(result=_jsonable(result))
        return result

    async def search_providers(
        self, service: Service, area: str, date_range: DateRange
    ) -> SearchResult:
        return await self._call(
            "search_providers",
            {"service": service, "area": area, "date_range": date_range},
            lambda: self._inner.search_providers(service, area, date_range),
        )

    async def get_provider(self, provider_id: str) -> ProviderResult:
        return await self._call(
            "get_provider",
            {"provider_id": provider_id},
            lambda: self._inner.get_provider(provider_id),
        )

    async def hold_slot(self, provider_id: str, slot: datetime) -> HoldResult:
        return await self._call(
            "hold_slot",
            {"provider_id": provider_id, "slot": slot},
            lambda: self._inner.hold_slot(provider_id, slot),
        )

    async def book(self, hold_id: str, idempotency_key: str) -> BookResult:
        return await self._call(
            "book",
            {"hold_id": hold_id, "idempotency_key": idempotency_key},
            lambda: self._inner.book(hold_id, idempotency_key),
        )

    async def get_booking_status(
        self, *, hold_id: str | None = None, idempotency_key: str | None = None
    ) -> BookingStatusResult:
        return await self._call(
            "get_booking_status",
            {"hold_id": hold_id, "idempotency_key": idempotency_key},
            lambda: self._inner.get_booking_status(
                hold_id=hold_id, idempotency_key=idempotency_key
            ),
            exempt=True,
        )

    async def cancel(self, booking_id: str, idempotency_key: str) -> CancelResult:
        return await self._call(
            "cancel",
            {"booking_id": booking_id, "idempotency_key": idempotency_key},
            lambda: self._inner.cancel(booking_id, idempotency_key),
        )

    async def reschedule(
        self, booking_id: str, slot: datetime, idempotency_key: str
    ) -> RescheduleResult:
        return await self._call(
            "reschedule",
            {"booking_id": booking_id, "slot": slot, "idempotency_key": idempotency_key},
            lambda: self._inner.reschedule(booking_id, slot, idempotency_key),
        )

    async def list_bookings(self, user_id: str) -> list[Booking]:
        return await self._call(
            "list_bookings", {"user_id": user_id}, lambda: self._inner.list_bookings(user_id)
        )
