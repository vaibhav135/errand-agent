"""Slot schedule: which hourly slots a provider offers on a given day.

What: `slot_schedule(provider_id, day, seed)` returns the provider's slot starts
      for that day as IST (+05:30) aware datetimes, sorted.

Why:  Slots are computed, not stored. A stored table generated "for the next 14
      days" at seed time goes stale as real time passes, and a separate slot
      status could drift from holds/bookings. Computing the offer and deriving
      availability from holds/bookings keeps both problems away.

How:  Hourly starts 08:00-18:00 IST (last slot ends 19:00). Each hour is offered
      when `keyed_unit(seed, "slot", provider_id, day, hour)` falls under a
      fixed share, so the schedule is stable per (seed, provider, day) and
      differs between providers. At least one slot per day is guaranteed so a
      provider never appears with an empty calendar. IST because "mornings only"
      means mornings in Bengaluru; storage stays UTC (see the mock_world schema).
"""

from datetime import UTC, date, datetime, time, timedelta, timezone

from errand_agent.mocks.rand import keyed_unit

IST = timezone(timedelta(hours=5, minutes=30), name="IST")
FIRST_HOUR = 8
LAST_HOUR = 18
OFFER_SHARE = 0.55


def utc_text(dt: datetime) -> str:
    """The one way mock_world stores a datetime: UTC, whole seconds, ISO-8601.

    SQLite compares these as text, so every stored value must share one offset and
    one precision (a SystemClock "now" carries microseconds; slots don't).
    """
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def slot_schedule(provider_id: str, day: date, *, seed: int) -> list[datetime]:
    hours = [
        h
        for h in range(FIRST_HOUR, LAST_HOUR + 1)
        if keyed_unit(seed, "slot", provider_id, day.isoformat(), h) < OFFER_SHARE
    ]
    if not hours:  # always offer something: pick one hour deterministically
        span = LAST_HOUR - FIRST_HOUR + 1
        hours = [FIRST_HOUR + int(keyed_unit(seed, "fallback", provider_id, day) * span)]
    return [datetime.combine(day, time(hour=h), IST) for h in hours]
