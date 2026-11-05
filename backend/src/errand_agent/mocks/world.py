"""MockWorld: the ToolEnv implementation backed by mock_world.db.

What: `MockWorld.open(path, clock=, ids=, scenario=, user_id=)` migrates and seeds
      the database, then serves the eight provider tools. (This module grows tool
      by tool; see TASKS.md.)

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
      - All datetimes go through `utc_text` before touching the DB; datetimes
        returned to callers are IST.
"""

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from errand_agent.core.clock import Clock
from errand_agent.core.ids import IdGen
from errand_agent.mocks.scenario import Scenario, quoted_price
from errand_agent.mocks.schedule import IST, slot_schedule, utc_text
from errand_agent.mocks.seed import AREAS, SERVICES, seed_world
from errand_agent.providers.models import (
    Attributes,
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
    Review,
    SearchFailed,
    SearchOk,
    SearchResult,
    Service,
)
from errand_agent.store.db import connect, transaction
from errand_agent.store.migrate import migrate, migrations_for

__all__ = ["HOLD_MINUTES", "MAX_SEARCH_DAYS", "MockWorld", "utc_text"]

MAX_SEARCH_DAYS = 14
HOLD_MINUTES = 10
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
