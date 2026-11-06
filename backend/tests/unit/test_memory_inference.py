from datetime import datetime, time, timedelta
from pathlib import Path

from errand_agent.core.clock import IST, utc_text
from errand_agent.memory.inference import infer_from_bookings
from errand_agent.memory.store import MemoryStore
from errand_agent.mocks.scenario import Scenario
from errand_agent.providers.models import Booking
from errand_agent.workflow.suggestions import suggest_from_history
from tests.unit.workflow_world import World

BASE = datetime(2026, 9, 1, 0, 0, tzinfo=IST)


def booking(
    n: int,
    provider: str = "ele_sparks",
    service: str = "electrician",
    hour: int = 11,
    status: str = "completed",
) -> Booking:
    return Booking(
        booking_id=f"bk_{n}",
        user_id="user_1",
        provider_id=provider,
        provider_name={"ele_sparks": "Sparks Electricals", "ele_brightwire": "BrightWire"}.get(
            provider, provider
        ),
        service=service,  # type: ignore[arg-type]
        slot=BASE + timedelta(days=7 * n, hours=hour),
        price_inr=800,
        status=status,  # type: ignore[arg-type]
    )


# --- the rules (pure) -------------------------------------------------------------------


def test_two_completed_bookings_suggest_a_usual_provider() -> None:
    found = infer_from_bookings([booking(1), booking(2), booking(3, provider="ele_brightwire")])

    [s] = [x for x in found if x.key == "preferred_provider"]  # 3 late starts also suggest a time

    assert (s.key, s.scope) == ("preferred_provider", "electrician")
    assert s.value == {"provider_id": "ele_sparks", "name": "Sparks Electricals"}
    assert s.reason == "2 completed bookings with Sparks Electricals"


def test_one_booking_or_cancellations_are_not_enough() -> None:
    assert infer_from_bookings([booking(1)]) == []
    assert infer_from_bookings([booking(1), booking(2, status="cancelled")]) == []


def test_consistently_late_starts_suggest_an_earliest_start() -> None:
    history = [
        booking(1, hour=10),
        booking(2, provider="x", service="plumber", hour=11),
        booking(3, provider="y", service="cleaning", hour=14, status="confirmed"),
    ]

    [s] = infer_from_bookings(history)

    assert (s.key, s.scope, s.value) == ("earliest_start", "global", time(10))
    assert s.reason == "all 3 bookings started at 10:00 or later"


def test_an_early_booking_means_no_earliest_start() -> None:
    history = [
        booking(1, hour=10),
        booking(2, provider="x", hour=8),
        booking(3, provider="y", hour=11),
    ]

    assert all(s.key != "earliest_start" for s in infer_from_bookings(history))


def test_rules_are_deterministic_regardless_of_order() -> None:
    history = [booking(i, hour=10 + i % 3) for i in range(1, 6)]

    assert infer_from_bookings(history) == infer_from_bookings(list(reversed(history)))


# --- applied to a real history ------------------------------------------------------------


async def test_suggestions_from_the_providers_history_are_pending(tmp_path: Path) -> None:
    world = World(tmp_path, Scenario())
    second = utc_text(world.clock.now() - timedelta(days=10))
    world.mock_exec(
        "INSERT INTO bookings (booking_id, user_id, provider_id, service, slot, price_inr,"
        " status, created_at, updated_at) VALUES ('bk_sparks_2', 'user_1', 'ele_sparks',"
        " 'electrician', ?, 800, 'completed', ?, ?)",
        second,
        second,
        second,
    )
    memories = MemoryStore(world.conn, clock=world.clock, ids=world.ids)

    created = await suggest_from_history(world.mock, memories, "user_1")

    [m] = [x for x in created if x.key == "preferred_provider"]
    assert {x.status for x in created} == {"pending_confirmation"}
    assert (m.key, m.scope, m.status, m.source) == (
        "preferred_provider",
        "electrician",
        "pending_confirmation",
        "inferred",
    )
    assert await suggest_from_history(world.mock, memories, "user_1") == []  # idempotent

    memories.forget(m.memory_id)
    assert await suggest_from_history(world.mock, memories, "user_1") == []  # stays forgotten
