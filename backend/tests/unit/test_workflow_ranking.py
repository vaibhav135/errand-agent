from datetime import UTC, date, datetime, time
from pathlib import Path

import pytest

from errand_agent.core.clock import IST, FakeClock
from errand_agent.core.ids import SequentialIdGen
from errand_agent.mocks.scenario import Scenario
from errand_agent.mocks.world import MockWorld
from errand_agent.providers.models import Attributes, DateRange, ProviderSummary, SearchOk
from errand_agent.store.db import connect
from errand_agent.workflow.ranking import (
    MAX_OPTIONS,
    MAX_SLOTS_PER_OPTION,
    adjusted_rating,
    rank_options,
)
from errand_agent.workflow.requirements import Requirements, TimeWindow

WEEK = DateRange(start=date(2026, 11, 5), end=date(2026, 11, 11))


def slot(day: int, hour: int) -> datetime:
    return datetime(2026, 11, day, hour, 0, tzinfo=IST)


def provider(
    pid: str,
    *,
    price: int = 1000,
    rating: float = 4.5,
    reviews: int = 100,
    verified: bool = True,
    slots: list[datetime] | None = None,
) -> ProviderSummary:
    return ProviderSummary(
        provider_id=pid,
        name=pid.title(),
        service="plumber",
        area="Indiranagar",
        rating=rating,
        review_count=reviews,
        price_inr=price,
        attributes=Attributes(verified=verified, years_experience=5, languages=["en"]),
        available_slots=slots if slots is not None else [slot(6, 9), slot(6, 14)],
    )


def reqs(**overrides: object) -> Requirements:
    base: dict[str, object] = {"service": "plumber", "area": "Indiranagar", "date_range": WEEK}
    return Requirements.model_validate({**base, **overrides})


def ids(result) -> list[str]:  # type: ignore[no-untyped-def]
    return [o.provider_id for o in result.options]


# --- filtering ------------------------------------------------------------------------------


def test_budget_is_inclusive() -> None:
    providers = [provider("at_budget", price=1500), provider("over", price=1501)]

    result = rank_options(providers, reqs(budget_max_inr=1500))

    assert ids(result) == ["at_budget"]


def test_no_budget_or_any_keeps_everyone() -> None:
    providers = [provider("a", price=500), provider("b", price=5000)]

    assert set(ids(rank_options(providers, reqs()))) == {"a", "b"}
    assert set(ids(rank_options(providers, reqs(budget_max_inr="any")))) == {"a", "b"}


def test_time_window_keeps_only_matching_slots_inclusively() -> None:
    p = provider("p", slots=[slot(6, 7), slot(6, 8), slot(6, 12), slot(6, 13)])
    mornings = TimeWindow(earliest=time(8), latest=time(12))

    result = rank_options([p], reqs(time_window=mornings))

    assert result.options[0].slots == [slot(6, 8), slot(6, 12)]


def test_time_window_is_read_in_local_time() -> None:
    # 03:30 UTC is 09:00 IST: a morning slot, even though 03:30 looks like night.
    utc_slot = datetime(2026, 11, 6, 3, 30, tzinfo=UTC)
    p = provider("p", slots=[utc_slot])

    result = rank_options([p], reqs(time_window=TimeWindow(earliest=time(8), latest=time(12))))

    assert ids(result) == ["p"]


def test_providers_without_matching_slots_are_dropped() -> None:
    providers = [
        provider("evening_only", slots=[slot(6, 17)]),
        provider("morning", slots=[slot(6, 9)]),
    ]

    result = rank_options(providers, reqs(time_window=TimeWindow(latest=time(12))))

    assert ids(result) == ["morning"]


# --- ranking ------------------------------------------------------------------------------------


def test_adjusted_rating_trusts_many_reviews_over_few() -> None:
    assert adjusted_rating(4.7, 300) > adjusted_rating(5.0, 3)


def test_rank_order_and_tie_breaks() -> None:
    providers = [
        provider("cheap_low", rating=4.0, price=500),
        provider("best", rating=4.8, price=1400),
        provider("tie_expensive", rating=4.5, price=1200),
        provider("tie_cheap_late", rating=4.5, price=1000, slots=[slot(7, 9)]),
        provider("tie_cheap_early", rating=4.5, price=1000, slots=[slot(6, 9)]),
    ]

    result = rank_options(providers, reqs())

    assert ids(result) == ["best", "tie_cheap_early", "tie_cheap_late"]
    assert len(result.options) == MAX_OPTIONS


def test_ranking_is_deterministic_regardless_of_input_order() -> None:
    providers = [provider(f"p{i}", rating=4.5, price=1000) for i in range(6)]

    forward = rank_options(providers, reqs())
    backward = rank_options(list(reversed(providers)), reqs())

    assert forward == backward
    assert ids(forward) == ["p0", "p1", "p2"]  # full tie: provider id decides


def test_each_option_shows_at_most_three_earliest_slots() -> None:
    p = provider("p", slots=[slot(8, 9), slot(6, 9), slot(7, 9), slot(6, 15), slot(9, 9)])

    option = rank_options([p], reqs()).options[0]

    assert option.slots == [slot(6, 9), slot(6, 15), slot(7, 9)]
    assert len(option.slots) == MAX_SLOTS_PER_OPTION


def test_reasons_are_computed_in_code() -> None:
    p = provider("p", price=1200, rating=4.6, reviews=98, verified=True)

    option = rank_options([p], reqs(budget_max_inr=1500)).options[0]

    assert "₹1200 (within your ₹1500 budget)" in option.reasons
    assert "4.6★ from 98 reviews" in option.reasons
    assert "verified" in option.reasons


# --- preferred provider ------------------------------------------------------------------------


def test_preferred_provider_comes_first_when_it_fits() -> None:
    providers = [provider("star", rating=4.9), provider("usual", rating=4.0)]

    result = rank_options(providers, reqs(preferred_provider_id="usual"))

    assert ids(result)[0] == "usual"
    assert "your preferred provider" in result.options[0].reasons
    assert result.preferred_note is None


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"budget_max_inr": 900}, "over your ₹900 budget"),
        ({"time_window": TimeWindow(earliest=time(16))}, "no slots in your time window"),
    ],
)
def test_preferred_provider_that_does_not_fit_is_explained(
    overrides: dict[str, object], expected: str
) -> None:
    providers = [
        provider("usual", price=1000, slots=[slot(6, 9)]),
        provider("other", price=800, slots=[slot(6, 17)]),
    ]

    result = rank_options(providers, reqs(preferred_provider_id="usual", **overrides))

    assert "usual" not in ids(result)
    assert result.preferred_note is not None and expected in result.preferred_note


def test_preferred_provider_not_in_this_area_is_explained() -> None:
    result = rank_options([provider("other")], reqs(preferred_provider_id="ele_sparks"))

    assert result.preferred_note is not None and "isn't available" in result.preferred_note


# --- the ranking-manipulation fixture can't move anything ----------------------------------------


async def test_ranking_ignores_the_manipulation_review(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 11, 5, 3, 30, tzinfo=UTC))
    db = tmp_path / "mock_world.db"
    world = MockWorld.open(db, clock=clock, ids=SequentialIdGen(), scenario=Scenario())
    want = reqs(budget_max_inr=1500, time_window=TimeWindow(earliest=time(8), latest=time(12)))

    async def ranked() -> list[str]:
        search = await world.search_providers("plumber", "Indiranagar", WEEK)
        assert isinstance(search, SearchOk)
        return ids(rank_options(search.providers, want))

    with_attack = await ranked()
    connect(db).execute("DELETE FROM reviews WHERE attack = 'ranking_manipulation'")
    without_attack = await ranked()

    assert with_attack == without_attack
    assert with_attack  # non-trivial: there are options to rank
