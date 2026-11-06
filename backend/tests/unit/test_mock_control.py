from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from errand_agent.core.clock import FakeClock
from errand_agent.core.ids import SequentialIdGen
from errand_agent.mocks.control import DemoState, MockControl
from errand_agent.mocks.scenario import Scenario
from errand_agent.mocks.world import MockWorld
from errand_agent.providers.errors import ProviderTimeout
from errand_agent.providers.models import DateRange, Held, SearchOk
from errand_agent.store.db import connect

NOW = datetime(2026, 11, 5, 3, 30, tzinfo=UTC)
WEEK = DateRange(start=date(2026, 11, 5), end=date(2026, 11, 11))
COOLBREEZE_BOOKING = "bk_hist_coolbreeze"  # seeded: upcoming, confirmed
SPARKS_BOOKING = "bk_hist_sparks"  # seeded: completed


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "mock_world.db"


@pytest.fixture
def world(db: Path) -> MockWorld:
    return MockWorld.open(db, clock=FakeClock(NOW), ids=SequentialIdGen(), scenario=Scenario())


@pytest.fixture
def control(world: MockWorld) -> MockControl:
    return MockControl(world)


async def test_provider_cancellation_is_visible_through_the_contract(
    world: MockWorld, control: MockControl
) -> None:
    booking = control.cancel_by_provider(COOLBREEZE_BOOKING, "technician unavailable")

    assert booking.status == "cancelled_by_provider"
    listed = {b.booking_id: b for b in await world.list_bookings("user_1")}
    assert listed[COOLBREEZE_BOOKING].status == "cancelled_by_provider"


def test_provider_cancellation_frees_the_slot(db: Path, control: MockControl) -> None:
    # Whether the slot is *re-offered* depends on that day's schedule; what must hold
    # is that no confirmed booking blocks it any more.
    control.cancel_by_provider(COOLBREEZE_BOOKING, "technician unavailable")

    blocking = (
        connect(db)
        .execute(
            "SELECT count(*) FROM bookings"
            " WHERE provider_id = 'ac_coolbreeze' AND status = 'confirmed'"
        )
        .fetchone()[0]
    )

    assert blocking == 0


def test_event_and_reason_are_recorded(db: Path, control: MockControl) -> None:
    control.cancel_by_provider(COOLBREEZE_BOOKING, "technician unavailable")

    row = connect(db).execute("SELECT * FROM provider_events").fetchone()

    assert (row["booking_id"], row["kind"], row["reason"]) == (
        COOLBREEZE_BOOKING,
        "cancelled_by_provider",
        "technician unavailable",
    )


@pytest.mark.parametrize("booking_id", ["bk_nope", SPARKS_BOOKING])
def test_misuse_is_loud(control: MockControl, booking_id: str) -> None:
    with pytest.raises(ValueError):
        control.cancel_by_provider(booking_id, "reason")


def test_control_is_not_part_of_the_tool_env(world: MockWorld) -> None:
    # The agent only ever receives a ToolEnv; the control plane must not hang off it.
    assert not hasattr(world, "cancel_by_provider")
    assert not hasattr(world, "script_next_book")


# --- live demo controls (D4): failures on demand, without restarting ------------------------


async def held(world: MockWorld) -> Held:
    search = await world.search_providers("plumber", "Indiranagar", WEEK)
    assert isinstance(search, SearchOk)
    first = search.providers[0]
    result = await world.hold_slot(first.provider_id, first.available_slots[0])
    assert isinstance(result, Held)
    return result


async def test_next_booking_can_time_out_after_succeeding(
    world: MockWorld, control: MockControl
) -> None:
    control.script_next_book("timeout_booked")
    hold = (await held(world)).hold

    with pytest.raises(ProviderTimeout):
        await world.book(hold.hold_id, "idem_demo")

    status = await world.get_booking_status(idempotency_key="idem_demo")
    assert status.status == "booked"  # it went through: only the answer was lost


async def test_next_booking_can_lose_the_slot(world: MockWorld, control: MockControl) -> None:
    control.script_next_book("slot_taken")
    hold = (await held(world)).hold

    result = await world.book(hold.hold_id, "idem_demo")

    assert result.status == "slot_taken"


async def test_the_state_says_what_is_armed_until_it_is_used(
    world: MockWorld, control: MockControl
) -> None:
    assert control.state() == DemoState(next_book=None, price_drift={})

    control.script_next_book("slot_taken")
    control.set_price_drift("plm_flowright", 0.2)
    armed = control.state()
    hold = (await held(world)).hold
    await world.book(hold.hold_id, "idem_demo")  # uses the scripted outcome

    assert armed == DemoState(next_book="slot_taken", price_drift={"plm_flowright": 0.2})
    assert control.state() == DemoState(next_book=None, price_drift={"plm_flowright": 0.2})

    control.reset()

    assert control.state() == DemoState(next_book=None, price_drift={})


def test_providers_are_listed_by_name(control: MockControl) -> None:
    providers = {p.provider_id: p for p in control.providers()}

    assert providers["plm_quickfix"].name == "QuickFix Plumbing"
    assert providers["ac_coolbreeze"].service == "ac_servicing"


async def test_a_price_can_change_at_hold_time_and_reset(
    world: MockWorld, control: MockControl
) -> None:
    search = await world.search_providers("plumber", "Indiranagar", WEEK)
    assert isinstance(search, SearchOk)
    first = search.providers[0]

    control.set_price_drift(first.provider_id, 0.2)
    drifted = await world.hold_slot(first.provider_id, first.available_slots[0])
    control.reset()
    normal = await world.hold_slot(first.provider_id, first.available_slots[1])

    assert isinstance(drifted, Held) and isinstance(normal, Held)
    assert drifted.hold.quoted_price_inr == round(first.price_inr * 1.2)
    assert normal.hold.quoted_price_inr == first.price_inr
