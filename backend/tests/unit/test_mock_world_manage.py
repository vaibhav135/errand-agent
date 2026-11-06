from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from errand_agent.core.clock import FakeClock, utc_text
from errand_agent.core.ids import SequentialIdGen
from errand_agent.mocks.scenario import Scenario, SlotRef
from errand_agent.mocks.seed import load_seed
from errand_agent.mocks.world import MockWorld
from errand_agent.providers.errors import ProviderUnavailable
from errand_agent.providers.models import (
    Booked,
    Booking,
    CancelFailed,
    Cancelled,
    DateRange,
    Held,
    Rescheduled,
    RescheduleFailed,
    SearchOk,
)
from errand_agent.providers.tool_env import ToolEnv
from errand_agent.store.db import connect

NOW = datetime(2026, 11, 5, 3, 30, tzinfo=UTC)  # Thursday 09:00 IST
FORTNIGHT = DateRange(start=date(2026, 11, 5), end=date(2026, 11, 18))
COOLBREEZE_BOOKING = "bk_hist_coolbreeze"  # seeded: upcoming, confirmed
SPARKS_BOOKING = "bk_hist_sparks"  # seeded: completed
AQUACARE_BOOKING = "bk_hist_aquacare"  # seeded: cancelled


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(NOW)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "mock_world.db"


def open_world(db: Path, clock: FakeClock, **scenario: object) -> MockWorld:
    return MockWorld.open(
        db, clock=clock, ids=SequentialIdGen(), scenario=Scenario.model_validate(scenario)
    )


@pytest.fixture
def world(db: Path, clock: FakeClock) -> MockWorld:
    return open_world(db, clock, cancel_transient_failures=0, book_outcomes=["success"])


async def coolbreeze_slots(world: MockWorld) -> list[datetime]:
    result = await world.search_providers("ac_servicing", "Indiranagar", FORTNIGHT)
    assert isinstance(result, SearchOk)
    return next(p for p in result.providers if p.provider_id == "ac_coolbreeze").available_slots


async def book_quickfix(world: MockWorld, key: str = "idem_book") -> Booking:
    search = await world.search_providers("plumber", "Indiranagar", FORTNIGHT)
    assert isinstance(search, SearchOk)
    slot = next(p for p in search.providers if p.provider_id == "plm_quickfix").available_slots[0]
    held = await world.hold_slot("plm_quickfix", slot)
    assert isinstance(held, Held)
    booked = await world.book(held.hold.hold_id, key)
    assert isinstance(booked, Booked)
    return booked.booking


def test_mock_world_satisfies_the_tool_env_protocol(world: MockWorld) -> None:
    env: ToolEnv = world  # checked statically by pyright; fails type-check if a tool drifts
    assert env is world


# --- cancel ---------------------------------------------------------------------------------


async def test_first_cancel_attempt_is_a_503_then_the_retry_succeeds(
    db: Path, clock: FakeClock
) -> None:
    world = open_world(db, clock, book_outcomes=["success"])  # default: 1 transient failure
    booking = await book_quickfix(world)

    with pytest.raises(ProviderUnavailable):
        await world.cancel(booking.booking_id, "idem_cancel")
    status = (
        connect(db)
        .execute("SELECT status FROM bookings WHERE booking_id = ?", (booking.booking_id,))
        .fetchone()[0]
    )
    assert status == "confirmed"  # a 503 did nothing

    retry = await world.cancel(booking.booking_id, "idem_cancel")
    assert isinstance(retry, Cancelled) and retry.booking.status == "cancelled"


async def test_transient_failure_is_counted_per_booking(db: Path, clock: FakeClock) -> None:
    world = open_world(db, clock, book_outcomes=["success", "success"])
    first = await book_quickfix(world, "idem_b1")
    with pytest.raises(ProviderUnavailable):
        await world.cancel(first.booking_id, "idem_c1")
    await world.cancel(first.booking_id, "idem_c1")

    with pytest.raises(ProviderUnavailable):  # a different booking gets its own first 503
        await world.cancel(COOLBREEZE_BOOKING, "idem_c2")


async def test_cancel_frees_the_slot_and_replays_its_result(world: MockWorld) -> None:
    booking = await book_quickfix(world)

    first = await world.cancel(booking.booking_id, "idem_cancel")
    again = await world.cancel(booking.booking_id, "idem_cancel")

    assert isinstance(first, Cancelled)
    assert again == first
    search = await world.search_providers("plumber", "Indiranagar", FORTNIGHT)
    assert isinstance(search, SearchOk)
    quickfix = next(p for p in search.providers if p.provider_id == "plm_quickfix")
    assert booking.slot in quickfix.available_slots


async def test_cancelling_an_already_cancelled_booking_is_not_an_error(world: MockWorld) -> None:
    result = await world.cancel(AQUACARE_BOOKING, "idem_1")

    assert isinstance(result, Cancelled) and result.booking.status == "cancelled"


async def test_provider_cancelled_booking_reports_its_status(world: MockWorld, db: Path) -> None:
    connect(db).execute(
        "UPDATE bookings SET status = 'cancelled_by_provider' WHERE booking_id = ?",
        (COOLBREEZE_BOOKING,),
    )

    result = await world.cancel(COOLBREEZE_BOOKING, "idem_1")

    assert isinstance(result, Cancelled) and result.booking.status == "cancelled_by_provider"


async def test_completed_booking_is_not_cancellable(world: MockWorld) -> None:
    result = await world.cancel(SPARKS_BOOKING, "idem_1")

    assert isinstance(result, CancelFailed) and result.status == "not_cancellable"


async def test_cancel_unknown_or_foreign_booking(world: MockWorld, db: Path) -> None:
    connect(db).execute(
        "UPDATE bookings SET user_id = 'user_2' WHERE booking_id = ?", (SPARKS_BOOKING,)
    )

    unknown = await world.cancel("bk_nope", "idem_1")
    foreign = await world.cancel(SPARKS_BOOKING, "idem_2")

    assert isinstance(unknown, CancelFailed) and unknown.status == "booking_not_found"
    assert isinstance(foreign, CancelFailed) and foreign.status == "booking_not_found"


async def test_cancel_key_reused_for_another_booking(world: MockWorld) -> None:
    booking = await book_quickfix(world)
    await world.cancel(booking.booking_id, "idem_cancel")

    result = await world.cancel(COOLBREEZE_BOOKING, "idem_cancel")

    assert isinstance(result, CancelFailed) and result.status == "idempotency_key_reused"


# --- reschedule -----------------------------------------------------------------------------


async def test_reschedule_moves_the_booking_and_keeps_the_price(world: MockWorld) -> None:
    target = (await coolbreeze_slots(world))[0]

    result = await world.reschedule(COOLBREEZE_BOOKING, target, "idem_r1")

    assert isinstance(result, Rescheduled)
    assert result.booking.slot == target
    assert result.booking.price_inr == 1200 and result.booking.status == "confirmed"
    assert target not in await coolbreeze_slots(world)


async def test_configured_slot_conflict(db: Path, clock: FakeClock) -> None:
    probe = open_world(db, clock)
    target = (await coolbreeze_slots(probe))[0]
    world = open_world(
        db, clock, slot_conflicts=[SlotRef(provider_id="ac_coolbreeze", slot=target)]
    )

    result = await world.reschedule(COOLBREEZE_BOOKING, target, "idem_r1")

    assert isinstance(result, RescheduleFailed) and result.status == "slot_conflict"
    retry = await world.reschedule(COOLBREEZE_BOOKING, target, "idem_r1")
    assert retry == result  # definitive result is replayed


async def test_unavailable_targets_are_conflicts(world: MockWorld, db: Path) -> None:
    target = (await coolbreeze_slots(world))[0]
    connect(db).execute(
        "INSERT INTO bookings (booking_id, user_id, provider_id, service, slot, price_inr,"
        " status, created_at, updated_at)"
        " VALUES ('bk_x', 'user_2', 'ac_coolbreeze', 'ac_servicing', ?, 1200, 'confirmed', ?, ?)",
        (utc_text(target), utc_text(NOW), utc_text(NOW)),
    )

    taken = await world.reschedule(COOLBREEZE_BOOKING, target, "idem_1")
    not_offered = await world.reschedule(
        COOLBREEZE_BOOKING, target + timedelta(minutes=30), "idem_2"
    )
    past = await world.reschedule(COOLBREEZE_BOOKING, NOW - timedelta(days=1), "idem_3")

    for result in (taken, not_offered, past):
        assert isinstance(result, RescheduleFailed) and result.status == "slot_conflict"


async def test_reschedule_replay_and_key_reuse(world: MockWorld) -> None:
    first_slot, second_slot = (await coolbreeze_slots(world))[:2]

    first = await world.reschedule(COOLBREEZE_BOOKING, first_slot, "idem_r1")
    replay = await world.reschedule(COOLBREEZE_BOOKING, first_slot, "idem_r1")
    reused = await world.reschedule(COOLBREEZE_BOOKING, second_slot, "idem_r1")

    assert replay == first
    assert isinstance(reused, RescheduleFailed) and reused.status == "idempotency_key_reused"


async def test_only_confirmed_future_bookings_can_be_rescheduled(world: MockWorld) -> None:
    target = (await coolbreeze_slots(world))[0]

    completed = await world.reschedule(SPARKS_BOOKING, target, "idem_1")
    cancelled = await world.reschedule(AQUACARE_BOOKING, target, "idem_2")
    unknown = await world.reschedule("bk_nope", target, "idem_3")

    assert isinstance(completed, RescheduleFailed) and completed.status == "not_reschedulable"
    assert isinstance(cancelled, RescheduleFailed) and cancelled.status == "not_reschedulable"
    assert isinstance(unknown, RescheduleFailed) and unknown.status == "booking_not_found"


# --- list_bookings --------------------------------------------------------------------------------


async def test_list_bookings_returns_history_ordered_by_slot(world: MockWorld) -> None:
    bookings = await world.list_bookings("user_1")

    assert [b.slot for b in bookings] == sorted(b.slot for b in bookings)
    assert {b.booking_id for b in bookings} == {h.booking_id for h in load_seed().history}
    status = {b.booking_id: b.status for b in bookings}
    assert (status[AQUACARE_BOOKING], status[SPARKS_BOOKING], status[COOLBREEZE_BOOKING]) == (
        "cancelled",
        "completed",
        "confirmed",
    )
    assert all(b.slot.utcoffset() == timedelta(hours=5, minutes=30) for b in bookings)


async def test_list_bookings_includes_new_bookings_but_not_ghosts(
    db: Path, clock: FakeClock
) -> None:
    world = open_world(db, clock, book_outcomes=["slot_taken", "success"])
    search = await world.search_providers("plumber", "Indiranagar", FORTNIGHT)
    assert isinstance(search, SearchOk)
    slots = next(p for p in search.providers if p.provider_id == "plm_quickfix").available_slots
    lost = await world.hold_slot("plm_quickfix", slots[0])
    assert isinstance(lost, Held)
    await world.book(lost.hold.hold_id, "idem_lost")  # slot_taken: a ghost booking appears
    won = await world.hold_slot("plm_quickfix", slots[1])
    assert isinstance(won, Held)
    await world.book(won.hold.hold_id, "idem_won")

    bookings = await world.list_bookings("user_1")

    assert len(bookings) == len(load_seed().history) + 1  # the won booking, not the ghost
    assert all(b.user_id == "user_1" for b in bookings)


async def test_list_bookings_for_a_user_without_any(world: MockWorld) -> None:
    assert await world.list_bookings("user_2") == []
