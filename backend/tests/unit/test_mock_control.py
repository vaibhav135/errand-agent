from datetime import UTC, datetime
from pathlib import Path

import pytest

from errand_agent.core.clock import FakeClock
from errand_agent.core.ids import SequentialIdGen
from errand_agent.mocks.control import MockControl
from errand_agent.mocks.scenario import Scenario
from errand_agent.mocks.world import MockWorld
from errand_agent.store.db import connect

NOW = datetime(2026, 11, 5, 3, 30, tzinfo=UTC)
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
