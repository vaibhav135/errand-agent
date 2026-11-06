"""Clock: the single source of time for the agent and the mocks.

What: A `Clock` protocol (`now`, `monotonic`, `sleep`) with two implementations:
      `SystemClock` (real time) and `FakeClock` (test-controlled time).

Why:  Behaviour depends on time: hold expiry (10 min), retry backoff, wall-clock
      limits, "this week" date ranges. Tests must be able to jump time forward,
      and the agent and the mock world must see the *same* time, or a test can
      "expire" a hold on one side while the other still treats it as valid.

How:  Nothing calls `datetime.now()` / `time.sleep()` directly; a `Clock` is
      injected wherever time is read. All datetimes are UTC-aware.
      `FakeClock.advance()` moves `now()` and `monotonic()` together, and
      `FakeClock.sleep()` advances time instantly instead of waiting.
      `utc_text()` is the single way a datetime is written to SQLite: UTC,
      whole seconds, ISO-8601, because SQLite compares these as text.
"""

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime:
        """Current wall-clock time, UTC-aware."""
        ...

    def monotonic(self) -> float:
        """Seconds from an arbitrary origin; only differences are meaningful."""
        ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class FakeClock:
    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None or start.utcoffset() is None:
            raise ValueError("FakeClock start must be timezone-aware")
        self._now = start.astimezone(UTC)
        self._mono = 0.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("time cannot go backwards")
        self._now += timedelta(seconds=seconds)
        self._mono += seconds

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)
        # Still yield once so other tasks get scheduled, as a real sleep would;
        # otherwise a retry loop could starve the rest of the event loop in tests.
        await asyncio.sleep(0)


def utc_text(dt: datetime) -> str:
    """The one way a datetime is stored in SQLite: UTC, whole seconds, ISO-8601.

    SQLite compares these as text, so every stored value must share one offset and
    one precision (a SystemClock "now" carries microseconds; slots don't).
    """
    return dt.astimezone(UTC).isoformat(timespec="seconds")
