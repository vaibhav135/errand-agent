from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from errand_agent.core.clock import FakeClock, utc_text
from errand_agent.core.ids import SequentialIdGen
from errand_agent.mocks.scenario import Scenario
from errand_agent.mocks.world import MockWorld
from errand_agent.providers.models import DateRange, Held, HoldFailed, SearchOk
from errand_agent.store.db import connect

NOW = datetime(2026, 11, 5, 3, 30, tzinfo=UTC)  # Thursday 09:00 IST
WEEK = DateRange(start=date(2026, 11, 5), end=date(2026, 11, 11))
QUICKFIX = "plm_quickfix"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(NOW)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "mock_world.db"


def open_world(db: Path, clock: FakeClock, scenario: Scenario | None = None) -> MockWorld:
    return MockWorld.open(db, clock=clock, ids=SequentialIdGen(), scenario=scenario or Scenario())


@pytest.fixture
def world(db: Path, clock: FakeClock) -> MockWorld:
    return open_world(db, clock)


async def open_slots(world: MockWorld, provider_id: str = QUICKFIX) -> list[datetime]:
    result = await world.search_providers("plumber", "Indiranagar", WEEK)
    assert isinstance(result, SearchOk)
    return next(p for p in result.providers if p.provider_id == provider_id).available_slots


async def hold(world: MockWorld, slot: datetime, provider_id: str = QUICKFIX) -> Held:
    result = await world.hold_slot(provider_id, slot)
    assert isinstance(result, Held), result
    return result


def insert_foreign_hold(db: Path, slot: datetime, expires_at: datetime) -> None:
    connect(db).execute(
        "INSERT INTO holds (hold_id, provider_id, slot, quoted_price_inr, expires_at,"
        " created_at, user_id) VALUES ('hold_other', ?, ?, 699, ?, ?, 'user_2')",
        (QUICKFIX, utc_text(slot), utc_text(expires_at), utc_text(NOW)),
    )


# --- happy path ------------------------------------------------------------------------------


async def test_hold_returns_id_quote_and_ten_minute_expiry(world: MockWorld) -> None:
    slot = (await open_slots(world))[0]

    held = await hold(world, slot)

    assert held.hold.hold_id == "hold_0001"
    assert held.hold.provider_id == QUICKFIX
    assert held.hold.slot == slot
    assert held.hold.slot.utcoffset() == timedelta(hours=5, minutes=30)
    assert held.hold.quoted_price_inr == 699  # no drift configured
    assert held.hold.expires_at == NOW + timedelta(minutes=10)


async def test_quoted_price_includes_scenario_drift(db: Path, clock: FakeClock) -> None:
    world = open_world(db, clock, Scenario(price_drift={QUICKFIX: 0.12}))
    slot = (await open_slots(world))[0]

    held = await hold(world, slot)

    assert held.hold.quoted_price_inr == 783  # 699 x 1.12 = 782.88, half-up


async def test_held_slot_disappears_from_search(world: MockWorld) -> None:
    slot = (await open_slots(world))[0]

    await hold(world, slot)

    assert slot not in await open_slots(world)


async def test_hold_is_stored_as_utc_text(world: MockWorld, db: Path) -> None:
    slot = (await open_slots(world))[0]
    held = await hold(world, slot)

    row = (
        connect(db)
        .execute("SELECT * FROM holds WHERE hold_id = ?", (held.hold.hold_id,))
        .fetchone()
    )

    assert row["slot"] == utc_text(slot) and row["slot"].endswith("+00:00")
    assert row["user_id"] == "user_1"


# --- refusals --------------------------------------------------------------------------------


async def test_another_users_active_hold_blocks_the_slot(world: MockWorld, db: Path) -> None:
    slot = (await open_slots(world))[0]
    insert_foreign_hold(db, slot, NOW + timedelta(minutes=10))

    result = await world.hold_slot(QUICKFIX, slot)

    assert isinstance(result, HoldFailed) and result.status == "slot_unavailable"


async def test_slot_frees_up_after_a_foreign_hold_expires(
    world: MockWorld, db: Path, clock: FakeClock
) -> None:
    slot = (await open_slots(world))[0]
    insert_foreign_hold(db, slot, NOW + timedelta(minutes=10))
    clock.advance(10 * 60 + 1)

    assert isinstance(await world.hold_slot(QUICKFIX, slot), Held)


async def test_slot_not_offered_by_the_schedule_is_unavailable(world: MockWorld) -> None:
    slot = (await open_slots(world))[0]

    half_past = await world.hold_slot(QUICKFIX, slot + timedelta(minutes=30))

    assert isinstance(half_past, HoldFailed) and half_past.status == "slot_unavailable"


async def test_past_slot_is_unavailable(world: MockWorld) -> None:
    past = datetime(2026, 11, 4, 10, 0, tzinfo=UTC)

    result = await world.hold_slot(QUICKFIX, past)

    assert isinstance(result, HoldFailed) and result.status == "slot_unavailable"


async def test_confirmed_booking_makes_the_slot_unavailable(world: MockWorld, db: Path) -> None:
    slot = (await open_slots(world))[0]
    connect(db).execute(
        "INSERT INTO bookings (booking_id, user_id, provider_id, service, slot, price_inr,"
        " status, created_at, updated_at)"
        " VALUES ('bk_x', 'user_2', ?, 'plumber', ?, 699, 'confirmed', ?, ?)",
        (QUICKFIX, utc_text(slot), utc_text(NOW), utc_text(NOW)),
    )

    result = await world.hold_slot(QUICKFIX, slot)

    assert isinstance(result, HoldFailed) and result.status == "slot_unavailable"


async def test_unknown_provider(world: MockWorld) -> None:
    result = await world.hold_slot("prov_nope", NOW + timedelta(days=1))

    assert isinstance(result, HoldFailed) and result.status == "provider_not_found"


# --- re-holding your own slot ------------------------------------------------------------------


async def test_re_holding_your_own_slot_replaces_the_hold(
    world: MockWorld, db: Path, clock: FakeClock
) -> None:
    slot = (await open_slots(world))[0]
    first = await hold(world, slot)
    clock.advance(9 * 60 + 45)  # 15 s left: inside the workflow's safety margin

    second = await hold(world, slot)

    assert second.hold.hold_id != first.hold.hold_id
    assert second.hold.expires_at == clock.now() + timedelta(minutes=10)
    old = (
        connect(db)
        .execute("SELECT expires_at FROM holds WHERE hold_id = ?", (first.hold.hold_id,))
        .fetchone()
    )
    assert old["expires_at"] <= utc_text(clock.now())  # old hold ended immediately
    assert slot not in await open_slots(world)  # still held, by the new hold
