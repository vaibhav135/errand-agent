"""MockControl: the mock world's control plane for tests and the demo.

What: `MockControl(world).cancel_by_provider(booking_id, reason)` simulates a
      provider cancelling a confirmed booking; `script_next_book(outcome)`,
      `set_price_drift(provider_id, fraction)` and `reset()` change the scenario
      while running, for the live demo (D4); `state()` says what is armed.

Why:  The proactive follow-up scenario needs a provider-side event the agent did
      not cause. It is a *separate* object, not a MockWorld method: the agent only
      ever receives a `ToolEnv`, so nothing on that object can be a back door into
      provider-side actions.

How:  The booking becomes `cancelled_by_provider` (freeing its slot) and the event
      and reason go to `provider_events`. The agent learns of it only through the
      contract (`list_bookings`, `get_booking_status`); `provider_events` stays
      internal to the mock. Misuse (unknown, non-confirmed or past booking) raises
      ValueError: this is a test/demo tool, so mistakes should be loud.
"""

from dataclasses import dataclass

from errand_agent.core.clock import utc_text
from errand_agent.mocks.scenario import _BOOK_CURSOR, BookOutcome  # pyright: ignore[reportPrivateUsage]
from errand_agent.mocks.world import MockWorld
from errand_agent.providers.models import Booking
from errand_agent.store.db import transaction


@dataclass(frozen=True)
class ProviderListing:
    provider_id: str
    name: str
    service: str
    price_inr: int


@dataclass(frozen=True)
class DemoState:
    """What is armed right now: the Demo tab shows it as pressed."""

    next_book: BookOutcome | None  # the next booking's scripted outcome, until used
    price_drift: dict[str, float]  # provider_id -> fraction more (or less) at hold


class MockControl:
    def __init__(self, world: MockWorld) -> None:
        self._world = world
        self._original = world._scenario  # what reset() goes back to

    # --- live demo controls (D4): failures on demand, without a restart -----------------

    def script_next_book(self, outcome: BookOutcome) -> None:
        """The next `book` call answers with `outcome` (e.g. "timeout_booked": it goes
        through but the answer is lost); after that, outcomes are random again."""
        self._world._scenario = self._world._scenario.model_copy(
            update={"book_outcomes": [outcome]}
        )
        with transaction(self._world._conn):
            self._world._conn.execute("DELETE FROM scenario_state WHERE key = ?", (_BOOK_CURSOR,))

    def set_price_drift(self, provider_id: str, fraction: float) -> None:
        """Holds with this provider quote `fraction` more (or less) than the search
        showed, e.g. 0.2 = 20% higher; 0 removes the drift."""
        drift = {**self._world._scenario.price_drift, provider_id: fraction}
        if fraction == 0:
            drift.pop(provider_id)
        self._world._scenario = self._world._scenario.model_copy(update={"price_drift": drift})

    def state(self) -> DemoState:
        """The scripted next booking (until a booking uses it) and active price drifts."""
        scenario = self._world._scenario
        row = self._world._conn.execute(
            "SELECT value FROM scenario_state WHERE key = ?", (_BOOK_CURSOR,)
        ).fetchone()
        cursor = row["value"] if row else 0
        scripted = scenario.book_outcomes
        return DemoState(
            next_book=scripted[cursor] if cursor < len(scripted) else None,
            price_drift=dict(scenario.price_drift),
        )

    def providers(self) -> list[ProviderListing]:
        """Every provider by name, for the demo's pickers (ids aren't human-friendly)."""
        rows = self._world._conn.execute(
            "SELECT provider_id, name, service, price_inr FROM providers ORDER BY service, name"
        ).fetchall()
        return [ProviderListing(**dict(row)) for row in rows]

    def reset(self) -> None:
        """Back to the scenario the world started with."""
        self._world._scenario = self._original
        with transaction(self._world._conn):
            self._world._conn.execute("DELETE FROM scenario_state WHERE key = ?", (_BOOK_CURSOR,))

    def cancel_by_provider(self, booking_id: str, reason: str) -> Booking:
        conn = self._world._conn  # same package: the control plane shares the mock's DB
        now = utc_text(self._world._clock.now())
        with transaction(conn):
            row = conn.execute(
                "SELECT status, slot FROM bookings WHERE booking_id = ?", (booking_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"no booking {booking_id!r}")
            if row["status"] != "confirmed" or row["slot"] <= now:
                raise ValueError(
                    f"booking {booking_id!r} is {row['status']} (slot {row['slot']}); "
                    "only confirmed future bookings can be cancelled by a provider"
                )
            conn.execute(
                "UPDATE bookings SET status = 'cancelled_by_provider', updated_at = ?"
                " WHERE booking_id = ?",
                (now, booking_id),
            )
            conn.execute(
                "INSERT INTO provider_events (booking_id, kind, reason, created_at)"
                " VALUES (?, 'cancelled_by_provider', ?, ?)",
                (booking_id, reason, now),
            )
        return self._world._booking(booking_id)
