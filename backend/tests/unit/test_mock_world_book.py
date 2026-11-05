from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from errand_agent.core.clock import FakeClock
from errand_agent.core.ids import SequentialIdGen
from errand_agent.mocks.scenario import BookOutcome, Scenario
from errand_agent.mocks.world import MockWorld, utc_text
from errand_agent.providers.errors import ProviderTimeout
from errand_agent.providers.models import (
    Booked,
    BookFailed,
    DateRange,
    Held,
    Hold,
    SearchOk,
    StatusBooked,
    StatusNotBooked,
    StatusUnknown,
)
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


def open_world(
    db: Path, clock: FakeClock, outcomes: list[BookOutcome] | None = None, ids=None
) -> MockWorld:
    scenario = Scenario(book_outcomes=outcomes or [], timeout_seconds=8.0)
    return MockWorld.open(db, clock=clock, ids=ids or SequentialIdGen(), scenario=scenario)


async def open_slots(world: MockWorld) -> list[datetime]:
    result = await world.search_providers("plumber", "Indiranagar", WEEK)
    assert isinstance(result, SearchOk)
    return next(p for p in result.providers if p.provider_id == QUICKFIX).available_slots


async def new_hold(world: MockWorld, index: int = 0) -> Hold:
    result = await world.hold_slot(QUICKFIX, (await open_slots(world))[index])
    assert isinstance(result, Held)
    return result.hold


def bookings_for_key(db: Path, key: str) -> int:
    return (
        connect(db)
        .execute("SELECT count(*) FROM bookings WHERE idempotency_key = ?", (key,))
        .fetchone()[0]
    )


# --- success & replay -----------------------------------------------------------------------


async def test_success_books_the_held_slot_at_the_quoted_price(db: Path, clock: FakeClock) -> None:
    world = open_world(db, clock, ["success"])
    hold = await new_hold(world)

    result = await world.book(hold.hold_id, "idem_1")

    assert isinstance(result, Booked)
    b = result.booking
    assert (b.user_id, b.provider_id, b.provider_name) == ("user_1", QUICKFIX, "QuickFix Plumbing")
    assert (b.service, b.status) == ("plumber", "confirmed")
    assert b.slot == hold.slot and b.slot.utcoffset() == timedelta(hours=5, minutes=30)
    assert b.price_inr == hold.quoted_price_inr
    assert hold.slot not in await open_slots(world)


async def test_same_key_and_hold_replays_the_original_booking(db: Path, clock: FakeClock) -> None:
    world = open_world(db, clock, ["success"])
    hold = await new_hold(world)
    first = await world.book(hold.hold_id, "idem_1")
    clock.advance(11 * 60)  # hold long expired: a replay must still succeed

    again = await world.book(hold.hold_id, "idem_1")

    assert again == first
    assert bookings_for_key(db, "idem_1") == 1


async def test_key_reused_for_a_different_hold_is_refused(db: Path, clock: FakeClock) -> None:
    world = open_world(db, clock, ["success", "success"])
    first, second = await new_hold(world, 0), await new_hold(world, 1)
    await world.book(first.hold_id, "idem_1")

    result = await world.book(second.hold_id, "idem_1")

    assert isinstance(result, BookFailed) and result.status == "idempotency_key_reused"


async def test_consumed_hold_with_a_new_key_does_not_double_book(
    db: Path, clock: FakeClock
) -> None:
    # The classic client bug: retrying with a *fresh* key after success.
    world = open_world(db, clock, ["success", "success"])
    hold = await new_hold(world)
    await world.book(hold.hold_id, "idem_1")

    result = await world.book(hold.hold_id, "idem_2")

    assert isinstance(result, BookFailed) and result.status == "hold_expired"
    count = (
        connect(db)
        .execute("SELECT count(*) FROM bookings WHERE hold_id = ?", (hold.hold_id,))
        .fetchone()[0]
    )
    assert count == 1


# --- refusals ---------------------------------------------------------------------------------


async def test_unknown_hold(db: Path, clock: FakeClock) -> None:
    world = open_world(db, clock)

    result = await world.book("hold_nope", "idem_1")

    assert isinstance(result, BookFailed) and result.status == "hold_not_found"


async def test_another_users_hold_is_not_found(db: Path, clock: FakeClock) -> None:
    world = open_world(db, clock)
    slot = (await open_slots(world))[0]
    connect(db).execute(
        "INSERT INTO holds (hold_id, provider_id, slot, quoted_price_inr, expires_at,"
        " created_at, user_id) VALUES ('hold_other', ?, ?, 699, ?, ?, 'user_2')",
        (QUICKFIX, utc_text(slot), utc_text(NOW + timedelta(minutes=10)), utc_text(NOW)),
    )

    result = await world.book("hold_other", "idem_1")

    assert isinstance(result, BookFailed) and result.status == "hold_not_found"


async def test_expired_hold_is_refused_but_the_request_is_remembered(
    db: Path, clock: FakeClock
) -> None:
    world = open_world(db, clock, ["success"])
    hold = await new_hold(world)
    clock.advance(10 * 60 + 1)

    result = await world.book(hold.hold_id, "idem_1")

    assert isinstance(result, BookFailed) and result.status == "hold_expired"
    assert isinstance(await world.get_booking_status(idempotency_key="idem_1"), StatusNotBooked)


async def test_slot_taken_is_stable_across_retries(db: Path, clock: FakeClock) -> None:
    world = open_world(db, clock, ["slot_taken", "success"])
    hold = await new_hold(world)

    first = await world.book(hold.hold_id, "idem_1")
    retry = await world.book(hold.hold_id, "idem_1")

    assert isinstance(first, BookFailed) and first.status == "slot_taken"
    assert isinstance(retry, BookFailed) and retry.status == "slot_taken"
    assert hold.slot not in await open_slots(world)  # someone else has it now
    assert bookings_for_key(db, "idem_1") == 0


# --- timeouts -----------------------------------------------------------------------------------


async def test_timeout_after_success_booked_and_replay_returns_it(
    db: Path, clock: FakeClock
) -> None:
    world = open_world(db, clock, ["timeout_booked"])
    hold = await new_hold(world)
    before = clock.now()

    with pytest.raises(ProviderTimeout):
        await world.book(hold.hold_id, "idem_1")

    assert clock.now() - before == timedelta(seconds=8)  # waited the configured timeout
    by_key = await world.get_booking_status(idempotency_key="idem_1")
    by_hold = await world.get_booking_status(hold_id=hold.hold_id)
    assert isinstance(by_key, StatusBooked) and isinstance(by_hold, StatusBooked)
    assert by_key.booking == by_hold.booking

    replay = await world.book(hold.hold_id, "idem_1")
    assert isinstance(replay, Booked) and replay.booking == by_key.booking
    assert bookings_for_key(db, "idem_1") == 1


async def test_timeout_without_success_allows_a_fresh_attempt(db: Path, clock: FakeClock) -> None:
    world = open_world(db, clock, ["timeout_not_booked", "success"])
    hold = await new_hold(world)

    with pytest.raises(ProviderTimeout):
        await world.book(hold.hold_id, "idem_1")

    assert isinstance(await world.get_booking_status(idempotency_key="idem_1"), StatusNotBooked)
    assert hold.slot not in await open_slots(world)  # hold still active

    retry = await world.book(hold.hold_id, "idem_1")
    assert isinstance(retry, Booked)
    assert bookings_for_key(db, "idem_1") == 1


async def test_restart_mid_booking_keeps_the_truth(db: Path, clock: FakeClock) -> None:
    ids = SequentialIdGen()  # shared across "processes": see core/ids.py caveat
    before = open_world(db, clock, ["timeout_booked"], ids=ids)
    hold = await new_hold(before)
    with pytest.raises(ProviderTimeout):
        await before.book(hold.hold_id, "idem_1")

    after = open_world(db, clock, ["timeout_booked"], ids=ids)  # process restarted

    status = await after.get_booking_status(idempotency_key="idem_1")
    assert isinstance(status, StatusBooked)
    replay = await after.book(hold.hold_id, "idem_1")
    assert isinstance(replay, Booked) and replay.booking == status.booking
    assert bookings_for_key(db, "idem_1") == 1


# --- get_booking_status ------------------------------------------------------------------------


async def test_status_for_unknown_references(db: Path, clock: FakeClock) -> None:
    world = open_world(db, clock)

    assert isinstance(await world.get_booking_status(idempotency_key="idem_never"), StatusUnknown)
    assert isinstance(await world.get_booking_status(hold_id="hold_never"), StatusUnknown)


async def test_status_for_a_hold_that_was_never_booked(db: Path, clock: FakeClock) -> None:
    world = open_world(db, clock)
    hold = await new_hold(world)

    assert isinstance(await world.get_booking_status(hold_id=hold.hold_id), StatusNotBooked)


async def test_status_needs_exactly_one_reference(db: Path, clock: FakeClock) -> None:
    world = open_world(db, clock)

    with pytest.raises(ValueError):
        await world.get_booking_status()
    with pytest.raises(ValueError):
        await world.get_booking_status(hold_id="h", idempotency_key="k")
