"""Inference rules: booking patterns that suggest a preference (never assert one).

What: `infer_from_bookings(bookings)` → `Suggestion`s (key, scope, value, reason).

Why:  "Distinguish explicit preferences from inferred ones." Inferences are useful
      ("same electrician as usual?") but can be wrong, so they are only ever suggested:
      the store keeps them pending until the user confirms.

How:  Pure functions over the provider's booking history, deliberately conservative:
      - usual provider: 2+ completed bookings with one provider for a service;
      - earliest start: 3+ completed or confirmed bookings, all starting at 09:00 IST
        or later -> the earliest of those start times.
      Cancelled bookings are ignored (a cancellation says nothing about preference),
      and there is no "avoid provider" rule: inferring dislike is too error-prone.
      Output order doesn't depend on input order.
"""

from collections import Counter
from datetime import time

from pydantic import BaseModel, ConfigDict

from errand_agent.core.clock import IST
from errand_agent.memory.store import MemoryKey
from errand_agent.providers.models import Booking

MIN_BOOKINGS_FOR_PROVIDER = 2
MIN_BOOKINGS_FOR_START = 3
EARLIEST_PLAUSIBLE_START = time(9)


class Suggestion(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: MemoryKey
    scope: str
    value: object
    reason: str


def infer_from_bookings(bookings: list[Booking]) -> list[Suggestion]:
    ordered = sorted(bookings, key=lambda b: (b.slot, b.booking_id))
    suggestions: list[Suggestion] = []

    completed = [b for b in ordered if b.status == "completed"]
    counts = Counter((b.service, b.provider_id) for b in completed)
    names = {b.provider_id: b.provider_name for b in completed}
    for (service, provider_id), n in sorted(counts.items()):
        if n >= MIN_BOOKINGS_FOR_PROVIDER:
            suggestions.append(
                Suggestion(
                    key="preferred_provider",
                    scope=service,
                    value={"provider_id": provider_id, "name": names[provider_id]},
                    reason=f"{n} completed bookings with {names[provider_id]}",
                )
            )

    kept = [b for b in ordered if b.status in ("completed", "confirmed")]
    if len(kept) >= MIN_BOOKINGS_FOR_START:
        starts = [b.slot.astimezone(IST).time() for b in kept]
        earliest = min(starts)
        if earliest >= EARLIEST_PLAUSIBLE_START:
            suggestions.append(
                Suggestion(
                    key="earliest_start",
                    scope="global",
                    value=earliest,
                    reason=f"all {len(kept)} bookings started at {earliest:%H:%M} or later",
                )
            )
    return suggestions
