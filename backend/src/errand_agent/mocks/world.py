"""MockWorld: the ToolEnv implementation backed by mock_world.db.

What: `MockWorld.open(path, clock=, ids=, scenario=, user_id=)` migrates and seeds
      the database, then serves all eight provider tools (it satisfies `ToolEnv`).

Why:  The agent, workflow and evals need a provider ecosystem that behaves like
      the real thing, failures included, runs offline, and remembers state across
      agent restarts.

How:  - Bound to one calling user (like an API token): `hold_slot`/`book` take no
        user id in the fixed contract, so bookings belong to `user_id`.
      - search_providers: providers serving (service, area) in stable id order,
        never ranked (ranking is the workflow's job). Area matching ignores case.
        Available slots = computed schedule (schedule.py), minus past slots, minus
        slots under an active hold or a confirmed booking; expired holds and
        cancelled bookings free a slot. Ranges over MAX_SEARCH_DAYS are refused,
        not silently trimmed.
      - get_provider: details, all served areas and reviews *verbatim*, injection
        fixtures included (data is data); attack labels never leave the mock.
      - hold_slot: the slot must be offered by the schedule, in the future, and not
        taken by a confirmed booking or another user's active hold. Re-holding a
        slot you already hold *replaces* your hold (new id, fresh 10 minutes,
        re-quoted price; the old one ends now), so the workflow can refresh a
        hold inside its expiry safety margin. Check and insert share one
        BEGIN IMMEDIATE transaction, so two racing holds can't both win.
      - book: checks in order (1) key used for another hold -> idempotency_key_reused,
        (2) key already booked -> replay the original booking, (3) unknown or
        foreign hold -> hold_not_found, (4) hold consumed under another key or
        past expiry -> hold_expired, (5) slot confirmed for someone else ->
        slot_taken; then draws an outcome (scripted, else keyed-random). Timeouts
        *commit first*, then wait `timeout_seconds` on the clock and raise
        ProviderTimeout: the provider did its work and the reply was lost.
        Every call records key -> hold in `book_requests` on first use.
      - get_booking_status: booked / not_booked ("seen, nothing booked", e.g.
        after a timeout that didn't book) / unknown_reference ("never seen").
      - cancel / reschedule: a key replays its stored result; the same key with
        different parameters is idempotency_key_reused. cancel's first
        `cancel_transient_failures` attempts per booking raise ProviderUnavailable
        (503) and do nothing else; the attempt count is committed *before*
        raising, or the 503 would never clear. Cancelling an already-cancelled
        booking returns its current state. reschedule refuses non-confirmed or
        past bookings, and treats configured conflicts, unoffered, past or taken
        target slots alike as slot_conflict; the price stays the same.
      - list_bookings: the user's bookings ordered by slot (ghost bookings from
        slot_taken races belong to someone else and never appear).
      - All datetimes go through `utc_text` before touching the DB; datetimes
        returned to callers are IST.
"""

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, TypeAdapter

from errand_agent.core.clock import Clock
from errand_agent.core.ids import IdGen
from errand_agent.mocks.scenario import (
    Scenario,
    decide_book_outcome,
    is_slot_conflict,
    quoted_price,
)
from errand_agent.mocks.schedule import IST, slot_schedule, utc_text
from errand_agent.mocks.seed import AREAS, SERVICES, seed_world
from errand_agent.providers.errors import ProviderTimeout, ProviderUnavailable
from errand_agent.providers.models import (
    Attributes,
    Booked,
    BookFailed,
    Booking,
    BookingStatusResult,
    BookResult,
    CancelFailed,
    Cancelled,
    CancelResult,
    DateRange,
    Held,
    Hold,
    HoldFailed,
    HoldResult,
    ProviderDetails,
    ProviderFailed,
    ProviderOk,
    ProviderProfile,
    ProviderResult,
    ProviderSummary,
    Rescheduled,
    RescheduleFailed,
    RescheduleResult,
    Review,
    SearchFailed,
    SearchOk,
    SearchResult,
    Service,
    StatusBooked,
    StatusNotBooked,
    StatusUnknown,
    booking_reference,
)
from errand_agent.store.db import connect, transaction
from errand_agent.store.migrate import migrate, migrations_for

__all__ = ["HOLD_MINUTES", "MAX_SEARCH_DAYS", "MockWorld", "utc_text"]

MAX_SEARCH_DAYS = 14
HOLD_MINUTES = 10
_GHOST_USER = "someone_else"  # owner of bookings that win a slot_taken race
_CANCEL_RESULT: TypeAdapter[CancelResult] = TypeAdapter(CancelResult)
_RESCHEDULE_RESULT: TypeAdapter[RescheduleResult] = TypeAdapter(RescheduleResult)


class _KeyReused(Exception):
    """An idempotency key was replayed with different request parameters."""


_AREA_BY_KEY = {area.casefold(): area for area in AREAS}


class MockWorld:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        clock: Clock,
        ids: IdGen,
        scenario: Scenario,
        user_id: str,
    ) -> None:
        self._conn = conn
        self._clock = clock
        self._ids = ids
        self._scenario = scenario
        self._user_id = user_id

    @classmethod
    def open(
        cls,
        path: Path,
        *,
        clock: Clock,
        ids: IdGen,
        scenario: Scenario,
        user_id: str = "user_1",
    ) -> "MockWorld":
        conn = connect(path)
        migrate(conn, migrations_for("mock_world"), clock)
        seed_world(conn, clock, user_id=user_id)
        return cls(conn, clock=clock, ids=ids, scenario=scenario, user_id=user_id)

    # --- search_providers -----------------------------------------------------------

    async def search_providers(
        self, service: Service, area: str, date_range: DateRange
    ) -> SearchResult:
        if service not in SERVICES:
            return SearchFailed(status="unknown_service")
        canonical_area = _AREA_BY_KEY.get(area.strip().casefold())
        if canonical_area is None:
            return SearchFailed(status="unknown_area")
        if (date_range.end - date_range.start).days + 1 > MAX_SEARCH_DAYS:
            return SearchFailed(status="date_range_too_long")

        rows = self._conn.execute(
            "SELECT p.* FROM providers p JOIN provider_areas a USING (provider_id)"
            " WHERE p.service = ? AND a.area = ? ORDER BY p.provider_id",
            (service, canonical_area),
        ).fetchall()
        return SearchOk(
            providers=[
                ProviderSummary(
                    **_profile(row).model_dump(),
                    area=canonical_area,
                    available_slots=self._available_slots(row["provider_id"], date_range),
                )
                for row in rows
            ]
        )

    def _available_slots(self, provider_id: str, date_range: DateRange) -> list[datetime]:
        now = self._clock.now()
        busy = self._busy_slots(provider_id)
        slots: list[datetime] = []
        day: date = date_range.start
        while day <= date_range.end:
            slots.extend(
                s
                for s in slot_schedule(provider_id, day, seed=self._scenario.seed)
                if s > now and utc_text(s) not in busy
            )
            day += timedelta(days=1)
        return slots

    def _busy_slots(self, provider_id: str) -> set[str]:
        """Slots taken by a confirmed booking or by an unexpired, unbooked hold."""
        now = utc_text(self._clock.now())
        rows = self._conn.execute(
            "SELECT slot FROM bookings WHERE provider_id = ? AND status = 'confirmed'"
            " UNION"
            " SELECT h.slot FROM holds h WHERE h.provider_id = ? AND h.expires_at > ?"
            " AND NOT EXISTS (SELECT 1 FROM bookings b WHERE b.hold_id = h.hold_id)",
            (provider_id, provider_id, now),
        ).fetchall()
        return {r["slot"] for r in rows}

    # --- hold_slot --------------------------------------------------------------------

    async def hold_slot(self, provider_id: str, slot: datetime) -> HoldResult:
        now = self._clock.now()
        with transaction(self._conn):
            row = self._conn.execute(
                "SELECT price_inr FROM providers WHERE provider_id = ?", (provider_id,)
            ).fetchone()
            if row is None:
                return HoldFailed(status="provider_not_found")

            offered = slot_schedule(
                provider_id, slot.astimezone(IST).date(), seed=self._scenario.seed
            )
            if slot <= now or slot not in offered or self._taken_by_others(provider_id, slot):
                return HoldFailed(status="slot_unavailable")

            # Re-holding your own slot replaces your hold: end it now.
            self._conn.execute(
                "UPDATE holds SET expires_at = ? WHERE provider_id = ? AND slot = ?"
                " AND user_id = ? AND expires_at > ?"
                " AND NOT EXISTS (SELECT 1 FROM bookings b WHERE b.hold_id = holds.hold_id)",
                (utc_text(now), provider_id, utc_text(slot), self._user_id, utc_text(now)),
            )
            hold = Hold(
                hold_id=self._ids.new("hold"),
                provider_id=provider_id,
                slot=slot.astimezone(IST),
                quoted_price_inr=quoted_price(self._scenario, provider_id, row["price_inr"]),
                expires_at=(now + timedelta(minutes=HOLD_MINUTES)).astimezone(IST),
            )
            self._conn.execute(
                "INSERT INTO holds (hold_id, provider_id, slot, quoted_price_inr, expires_at,"
                " created_at, user_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    hold.hold_id,
                    provider_id,
                    utc_text(slot),
                    hold.quoted_price_inr,
                    utc_text(hold.expires_at),
                    utc_text(now),
                    self._user_id,
                ),
            )
        return Held(hold=hold)

    def _taken_by_others(self, provider_id: str, slot: datetime) -> bool:
        """A confirmed booking, or another user's unexpired, unbooked hold."""
        now = utc_text(self._clock.now())
        key = utc_text(slot)
        return (
            self._conn.execute(
                "SELECT 1 FROM bookings WHERE provider_id = ? AND slot = ? AND status = 'confirmed'"
                " UNION ALL"
                " SELECT 1 FROM holds h WHERE h.provider_id = ? AND h.slot = ? AND h.user_id != ?"
                " AND h.expires_at > ?"
                " AND NOT EXISTS (SELECT 1 FROM bookings b WHERE b.hold_id = h.hold_id)",
                (provider_id, key, provider_id, key, self._user_id, now),
            ).fetchone()
            is not None
        )

    # --- book ---------------------------------------------------------------------------

    async def book(self, hold_id: str, idempotency_key: str) -> BookResult:
        now = self._clock.now()
        result: BookResult | None = None
        timed_out = False
        with transaction(self._conn):
            seen = self._conn.execute(
                "SELECT hold_id FROM book_requests WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if seen is not None and seen["hold_id"] != hold_id:
                return BookFailed(status="idempotency_key_reused")
            done = self._conn.execute(
                "SELECT booking_id FROM bookings WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if done is not None:  # replay: the original answer, whatever happened since
                return Booked(booking=self._booking(done["booking_id"]))

            hold = self._conn.execute(
                "SELECT * FROM holds WHERE hold_id = ? AND user_id = ?", (hold_id, self._user_id)
            ).fetchone()
            if hold is None:
                return BookFailed(status="hold_not_found")
            if seen is None:
                self._conn.execute(
                    "INSERT INTO book_requests (idempotency_key, hold_id, user_id, created_at)"
                    " VALUES (?, ?, ?, ?)",
                    (idempotency_key, hold_id, self._user_id, utc_text(now)),
                )
            if self._conn.execute(
                "SELECT 1 FROM bookings WHERE hold_id = ?", (hold_id,)
            ).fetchone():
                return BookFailed(status="hold_expired")  # consumed under another key
            if hold["expires_at"] <= utc_text(now):
                return BookFailed(status="hold_expired")
            slot = datetime.fromisoformat(hold["slot"])
            if self._slot_confirmed(hold["provider_id"], slot):
                return BookFailed(status="slot_taken")

            attempt = hold["book_attempts"]
            self._conn.execute(
                "UPDATE holds SET book_attempts = ? WHERE hold_id = ?", (attempt + 1, hold_id)
            )
            outcome = decide_book_outcome(self._conn, self._scenario, hold_id, attempt)
            if outcome in ("success", "timeout_booked"):
                booking_id = self._insert_booking(hold, now, idempotency_key)
                result = Booked(booking=self._booking(booking_id))
            elif outcome == "slot_taken":
                self._insert_ghost_booking(hold, now)  # someone else got there first
                result = BookFailed(status="slot_taken")
            timed_out = outcome in ("timeout_booked", "timeout_not_booked")

        # Committed above: the provider did its work; now the reply gets lost.
        if timed_out:
            await self._clock.sleep(self._scenario.timeout_seconds)
            raise ProviderTimeout(f"book({hold_id}) timed out; check get_booking_status")
        assert result is not None
        return result

    def _slot_confirmed(self, provider_id: str, slot: datetime) -> bool:
        return (
            self._conn.execute(
                "SELECT 1 FROM bookings"
                " WHERE provider_id = ? AND slot = ? AND status = 'confirmed'",
                (provider_id, utc_text(slot)),
            ).fetchone()
            is not None
        )

    def _insert_booking(self, hold: sqlite3.Row, now: datetime, idempotency_key: str) -> str:
        booking_id = self._ids.new("bk")
        service = self._conn.execute(
            "SELECT service FROM providers WHERE provider_id = ?", (hold["provider_id"],)
        ).fetchone()["service"]
        self._conn.execute(
            "INSERT INTO bookings (booking_id, user_id, provider_id, service, slot, price_inr,"
            " status, hold_id, idempotency_key, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 'confirmed', ?, ?, ?, ?)",
            (
                booking_id,
                self._user_id,
                hold["provider_id"],
                service,
                hold["slot"],
                hold["quoted_price_inr"],
                hold["hold_id"],
                idempotency_key,
                utc_text(now),
                utc_text(now),
            ),
        )
        return booking_id

    def _insert_ghost_booking(self, hold: sqlite3.Row, now: datetime) -> None:
        service = self._conn.execute(
            "SELECT service FROM providers WHERE provider_id = ?", (hold["provider_id"],)
        ).fetchone()["service"]
        self._conn.execute(
            "INSERT INTO bookings (booking_id, user_id, provider_id, service, slot, price_inr,"
            " status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'confirmed', ?, ?)",
            (
                self._ids.new("bk"),
                _GHOST_USER,
                hold["provider_id"],
                service,
                hold["slot"],
                hold["quoted_price_inr"],
                utc_text(now),
                utc_text(now),
            ),
        )

    def _booking(self, booking_id: str) -> Booking:
        row = self._conn.execute(
            "SELECT b.*, p.name AS provider_name FROM bookings b"
            " JOIN providers p USING (provider_id) WHERE b.booking_id = ?",
            (booking_id,),
        ).fetchone()
        return Booking(
            booking_id=row["booking_id"],
            user_id=row["user_id"],
            provider_id=row["provider_id"],
            provider_name=row["provider_name"],
            service=row["service"],
            slot=datetime.fromisoformat(row["slot"]).astimezone(IST),
            price_inr=row["price_inr"],
            status=row["status"],
        )

    # --- get_booking_status -------------------------------------------------------------

    async def get_booking_status(
        self, *, hold_id: str | None = None, idempotency_key: str | None = None
    ) -> BookingStatusResult:
        kind, ref = booking_reference(hold_id=hold_id, idempotency_key=idempotency_key)
        if kind == "idempotency_key":
            seen = self._conn.execute(
                "SELECT 1 FROM book_requests WHERE idempotency_key = ? AND user_id = ?",
                (ref, self._user_id),
            ).fetchone()
            booked = self._conn.execute(
                "SELECT booking_id FROM bookings WHERE idempotency_key = ? AND user_id = ?",
                (ref, self._user_id),
            ).fetchone()
        else:
            seen = self._conn.execute(
                "SELECT 1 FROM holds WHERE hold_id = ? AND user_id = ?", (ref, self._user_id)
            ).fetchone()
            booked = self._conn.execute(
                "SELECT booking_id FROM bookings WHERE hold_id = ? AND user_id = ?",
                (ref, self._user_id),
            ).fetchone()
        if booked is not None:
            return StatusBooked(booking=self._booking(booked["booking_id"]))
        if seen is not None:
            return StatusNotBooked()
        return StatusUnknown()

    # --- cancel -------------------------------------------------------------------------

    async def cancel(self, booking_id: str, idempotency_key: str) -> CancelResult:
        now = self._clock.now()
        # The 503 is raised *after* the transaction: raising inside it would roll back
        # the attempt count, so every cancel would stay "the first attempt" forever.
        unavailable = False
        result: CancelResult | None = None
        with transaction(self._conn):
            params = {"booking_id": booking_id}
            try:
                replay = self._replay(idempotency_key, "cancel", params, _CANCEL_RESULT)
            except _KeyReused:
                return CancelFailed(status="idempotency_key_reused")
            if replay is not None:
                return replay
            row = self._own_booking(booking_id)
            if row is None:
                return CancelFailed(status="booking_not_found")

            attempts = self._conn.execute(
                "SELECT attempts FROM cancel_attempts WHERE booking_id = ?", (booking_id,)
            ).fetchone()
            seen = attempts["attempts"] if attempts else 0
            if seen < self._scenario.cancel_transient_failures:
                self._conn.execute(
                    "INSERT INTO cancel_attempts (booking_id, attempts) VALUES (?, ?)"
                    " ON CONFLICT (booking_id) DO UPDATE SET attempts = excluded.attempts",
                    (booking_id, seen + 1),
                )
                unavailable = True  # counted (committed) but nothing else done
            else:
                if row["status"] in ("cancelled", "cancelled_by_provider"):
                    result = Cancelled(booking=self._booking(booking_id))
                elif row["status"] == "completed" or row["slot"] <= utc_text(now):
                    result = CancelFailed(status="not_cancellable")
                else:
                    self._conn.execute(
                        "UPDATE bookings SET status = 'cancelled', updated_at = ?"
                        " WHERE booking_id = ?",
                        (utc_text(now), booking_id),
                    )
                    result = Cancelled(booking=self._booking(booking_id))
                self._remember(idempotency_key, "cancel", booking_id, params, result, now)

        if unavailable:
            raise ProviderUnavailable(f"cancel({booking_id}): 503, retry with the same key")
        assert result is not None
        return result

    # --- reschedule ---------------------------------------------------------------------

    async def reschedule(
        self, booking_id: str, slot: datetime, idempotency_key: str
    ) -> RescheduleResult:
        now = self._clock.now()
        result: RescheduleResult
        with transaction(self._conn):
            params = {"booking_id": booking_id, "slot": utc_text(slot)}
            try:
                replay = self._replay(idempotency_key, "reschedule", params, _RESCHEDULE_RESULT)
            except _KeyReused:
                return RescheduleFailed(status="idempotency_key_reused")
            if replay is not None:
                return replay
            row = self._own_booking(booking_id)
            if row is None:
                return RescheduleFailed(status="booking_not_found")

            if row["status"] != "confirmed" or row["slot"] <= utc_text(now):
                result = RescheduleFailed(status="not_reschedulable")
            elif utc_text(slot) == row["slot"]:
                result = Rescheduled(booking=self._booking(booking_id))  # already there
            elif self._cannot_move_to(row["provider_id"], slot, now):
                result = RescheduleFailed(status="slot_conflict")
            else:
                self._conn.execute(
                    "UPDATE bookings SET slot = ?, updated_at = ? WHERE booking_id = ?",
                    (utc_text(slot), utc_text(now), booking_id),
                )
                result = Rescheduled(booking=self._booking(booking_id))
            self._remember(idempotency_key, "reschedule", booking_id, params, result, now)
        return result

    def _cannot_move_to(self, provider_id: str, slot: datetime, now: datetime) -> bool:
        offered = slot_schedule(provider_id, slot.astimezone(IST).date(), seed=self._scenario.seed)
        return (
            is_slot_conflict(self._scenario, provider_id, slot)
            or slot <= now
            or slot not in offered
            or self._taken_by_others(provider_id, slot)
        )

    # --- list_bookings ------------------------------------------------------------------

    async def list_bookings(self, user_id: str) -> list[Booking]:
        rows = self._conn.execute(
            "SELECT booking_id FROM bookings WHERE user_id = ? ORDER BY slot, booking_id",
            (user_id,),
        ).fetchall()
        return [self._booking(r["booking_id"]) for r in rows]

    # --- request replay (cancel / reschedule) --------------------------------------------

    def _own_booking(self, booking_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM bookings WHERE booking_id = ? AND user_id = ?",
            (booking_id, self._user_id),
        ).fetchone()

    def _replay[R](
        self,
        key: str,
        operation: str,
        params: dict[str, str],
        adapter: TypeAdapter[R],
    ) -> R | None:
        """Stored result for this exact request; raises _KeyReused if the key meant
        a different request (other operation, booking or slot)."""
        row = self._conn.execute(
            "SELECT operation, request, result FROM requests WHERE idempotency_key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        if row["operation"] != operation or json.loads(row["request"]) != params:
            raise _KeyReused
        return adapter.validate_json(row["result"])

    def _remember(
        self,
        key: str,
        operation: str,
        booking_id: str,
        params: dict[str, str],
        result: BaseModel,
        now: datetime,
    ) -> None:
        self._conn.execute(
            "INSERT INTO requests (idempotency_key, operation, booking_id, request, result,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                key,
                operation,
                booking_id,
                json.dumps(params),
                result.model_dump_json(),
                utc_text(now),
            ),
        )

    # --- get_provider -----------------------------------------------------------------

    async def get_provider(self, provider_id: str) -> ProviderResult:
        row = self._conn.execute(
            "SELECT * FROM providers WHERE provider_id = ?", (provider_id,)
        ).fetchone()
        if row is None:
            return ProviderFailed(status="provider_not_found")
        areas = [
            r["area"]
            for r in self._conn.execute(
                "SELECT area FROM provider_areas WHERE provider_id = ? ORDER BY area",
                (provider_id,),
            )
        ]
        reviews = [
            Review(
                author=r["author"],
                rating=r["rating"],
                posted_on=date.fromisoformat(r["posted_on"]),
                text=r["text"],
            )
            for r in self._conn.execute(
                "SELECT author, rating, posted_on, text FROM reviews"
                " WHERE provider_id = ? ORDER BY posted_on DESC, review_id",
                (provider_id,),
            )
        ]
        return ProviderOk(
            provider=ProviderDetails(
                **_profile(row).model_dump(),
                areas=areas,
                description=row["description"],
                reviews=reviews,
            )
        )


def _profile(row: sqlite3.Row) -> ProviderProfile:
    """Shared provider fields from a providers row (attack labels never selected)."""
    return ProviderProfile(
        provider_id=row["provider_id"],
        name=row["name"],
        service=row["service"],
        rating=row["rating"],
        review_count=row["review_count"],
        price_inr=row["price_inr"],
        attributes=Attributes(
            verified=bool(row["verified"]),
            years_experience=row["years_experience"],
            languages=json.loads(row["languages"]),
        ),
    )
