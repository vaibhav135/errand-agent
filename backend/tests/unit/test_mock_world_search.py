from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from errand_agent.core.clock import FakeClock
from errand_agent.core.ids import SequentialIdGen
from errand_agent.mocks.scenario import Scenario
from errand_agent.mocks.schedule import IST
from errand_agent.mocks.seed import AREAS, ATTACK_CATEGORIES, SERVICES
from errand_agent.mocks.world import MockWorld, utc_text
from errand_agent.providers.models import (
    MAX_DATE_RANGE_DAYS,
    DateRange,
    ProviderFailed,
    ProviderOk,
    SearchFailed,
    SearchOk,
)
from errand_agent.store.db import connect

NOW = datetime(2026, 11, 5, 3, 30, tzinfo=UTC)  # Thursday 09:00 IST
WEEK = DateRange(start=date(2026, 11, 5), end=date(2026, 11, 11))


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(NOW)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "mock_world.db"


@pytest.fixture
def world(db: Path, clock: FakeClock) -> MockWorld:
    return MockWorld.open(db, clock=clock, ids=SequentialIdGen(), scenario=Scenario())


async def search(world: MockWorld, service: str = "plumber", area: str = "Indiranagar") -> SearchOk:
    result = await world.search_providers(service, area, WEEK)  # type: ignore[arg-type]
    assert isinstance(result, SearchOk), result
    return result


# --- opening -----------------------------------------------------------------------------


def test_opening_twice_seeds_once(db: Path, clock: FakeClock) -> None:
    MockWorld.open(db, clock=clock, ids=SequentialIdGen(), scenario=Scenario())
    MockWorld.open(db, clock=clock, ids=SequentialIdGen(), scenario=Scenario())

    assert connect(db).execute("SELECT count(*) FROM providers").fetchone()[0] == 30


# --- search_providers ------------------------------------------------------------------------


async def test_every_service_area_pair_returns_three_to_eight_providers(world: MockWorld) -> None:
    for service in SERVICES:
        for area in AREAS:
            result = await search(world, service, area)
            assert 3 <= len(result.providers) <= 8, (service, area)
            assert {p.service for p in result.providers} == {service}
            assert {p.area for p in result.providers} == {area}


async def test_unknown_service_and_area_get_typed_failures(world: MockWorld) -> None:
    bad_service = await world.search_providers("gardener", "Indiranagar", WEEK)  # type: ignore[arg-type]
    bad_area = await world.search_providers("plumber", "Mumbai", WEEK)

    assert isinstance(bad_service, SearchFailed) and bad_service.status == "unknown_service"
    assert isinstance(bad_area, SearchFailed) and bad_area.status == "unknown_area"


async def test_area_matching_ignores_case_and_returns_canonical_spelling(world: MockWorld) -> None:
    result = await search(world, "plumber", "  hsr layout ")

    assert {p.area for p in result.providers} == {"HSR Layout"}


async def test_date_range_over_limit_is_refused(world: MockWorld) -> None:
    too_long = DateRange(
        start=date(2026, 11, 5), end=date(2026, 11, 5) + timedelta(days=MAX_DATE_RANGE_DAYS)
    )

    result = await world.search_providers("plumber", "Indiranagar", too_long)

    assert isinstance(result, SearchFailed) and result.status == "date_range_too_long"


async def test_results_are_in_stable_id_order_not_ranked(world: MockWorld) -> None:
    first = await search(world)
    second = await search(world)

    ids = [p.provider_id for p in first.providers]
    assert ids == sorted(ids)
    assert first == second


async def test_search_prices_are_base_prices(world: MockWorld, db: Path) -> None:
    result = await search(world)
    base = dict(connect(db).execute("SELECT provider_id, price_inr FROM providers").fetchall())

    assert all(p.price_inr == base[p.provider_id] for p in result.providers)


async def test_slots_are_ist_within_range_sorted_and_not_in_the_past(world: MockWorld) -> None:
    result = await search(world)

    for p in result.providers:
        assert p.available_slots == sorted(p.available_slots)
        for s in p.available_slots:
            assert s.utcoffset() == timedelta(hours=5, minutes=30)
            assert WEEK.start <= s.date() <= WEEK.end
            assert s > NOW


# --- availability is derived from holds and bookings ---------------------------------------


async def first_open_slot(world: MockWorld) -> tuple[str, datetime]:
    result = await search(world)
    provider = next(p for p in result.providers if p.available_slots)
    return provider.provider_id, provider.available_slots[0]


async def slots_of(world: MockWorld, provider_id: str) -> list[datetime]:
    result = await search(world)
    return next(p for p in result.providers if p.provider_id == provider_id).available_slots


async def test_active_hold_hides_a_slot_until_it_expires(
    world: MockWorld, db: Path, clock: FakeClock
) -> None:
    provider_id, slot = await first_open_slot(world)
    connect(db).execute(
        "INSERT INTO holds (hold_id, provider_id, slot, quoted_price_inr, expires_at, created_at)"
        " VALUES ('hold_x', ?, ?, 999, ?, ?)",
        (provider_id, utc_text(slot), utc_text(NOW + timedelta(minutes=10)), utc_text(NOW)),
    )

    assert slot not in await slots_of(world, provider_id)

    clock.advance(11 * 60)  # hold expired
    assert slot in await slots_of(world, provider_id)


async def test_confirmed_booking_hides_a_slot_and_cancelled_frees_it(
    world: MockWorld, db: Path
) -> None:
    provider_id, slot = await first_open_slot(world)
    conn = connect(db)
    conn.execute(
        "INSERT INTO bookings (booking_id, user_id, provider_id, service, slot, price_inr,"
        " status, created_at, updated_at)"
        " VALUES ('bk_x', 'user_2', ?, 'plumber', ?, 999, 'confirmed', ?, ?)",
        (provider_id, utc_text(slot), utc_text(NOW), utc_text(NOW)),
    )
    assert slot not in await slots_of(world, provider_id)

    conn.execute("UPDATE bookings SET status = 'cancelled' WHERE booking_id = 'bk_x'")
    assert slot in await slots_of(world, provider_id)


def test_utc_text_normalises_offset_and_precision() -> None:
    ist = datetime(2026, 11, 7, 10, 0, 0, 123456, tzinfo=IST)

    assert utc_text(ist) == "2026-11-07T04:30:00+00:00"


# --- get_provider ------------------------------------------------------------------------------


async def test_get_provider_returns_details_and_all_areas(world: MockWorld) -> None:
    result = await world.get_provider("plm_quickfix")

    assert isinstance(result, ProviderOk)
    p = result.provider
    assert p.name == "QuickFix Plumbing"
    assert p.areas == sorted(["Indiranagar", "Koramangala", "HSR Layout"])
    assert len(p.reviews) == 4


async def test_reviews_are_returned_verbatim_including_injections(world: MockWorld) -> None:
    result = await world.get_provider("plm_quickfix")
    assert isinstance(result, ProviderOk)

    texts = " ".join(r.text for r in result.provider.reviews)

    assert "SYSTEM NOTICE TO AI ASSISTANT" in texts  # data is data: the mock doesn't filter
    assert "pre-authorised" in result.provider.description


async def test_attack_labels_never_leave_the_mock(world: MockWorld, db: Path) -> None:
    provider_ids = [r[0] for r in connect(db).execute("SELECT provider_id FROM providers")]

    for provider_id in provider_ids:
        result = await world.get_provider(provider_id)
        assert isinstance(result, ProviderOk)
        payload = result.model_dump_json()
        assert "attack" not in payload
        assert not any(label in payload for label in ATTACK_CATEGORIES)


async def test_unknown_provider_is_a_typed_failure(world: MockWorld) -> None:
    result = await world.get_provider("prov_nope")

    assert isinstance(result, ProviderFailed) and result.status == "provider_not_found"
