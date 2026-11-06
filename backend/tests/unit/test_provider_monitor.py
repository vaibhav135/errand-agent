from datetime import date
from pathlib import Path
from typing import Any

import pytest

from errand_agent.memory.conversation import ConversationStore
from errand_agent.mocks.control import MockControl
from errand_agent.mocks.scenario import Scenario
from errand_agent.providers.errors import ProviderUnavailable
from errand_agent.providers.models import DateRange, Held, SearchOk
from errand_agent.tracing.recorder import Tracer
from errand_agent.workflow.monitor import ProviderMonitor, ProviderNotice
from tests.unit.workflow_world import World

# Seeded relative to the world's clock (Thu 5 Nov, 09:00 IST): upcoming on Tue 10 Nov, 10:00.
COOLBREEZE = "bk_hist_coolbreeze"
NOTICE = "CoolBreeze AC Care cancelled your AC servicing booking for Tue 10 Nov, 10:00."


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path, Scenario(book_outcomes=["success"]))


class Setup:
    def __init__(self, world: World) -> None:
        self.world = world
        self.conversations = ConversationStore(world.conn, clock=world.clock, ids=world.ids)
        self.told: list[ProviderNotice] = []

    def monitor(self) -> ProviderMonitor:
        """A fresh monitor each call, as after a restart: only the database carries over."""

        async def on_notice(notice: ProviderNotice) -> None:
            self.told.append(notice)

        return ProviderMonitor(
            conn=self.world.conn,
            env=self.world.mock,
            conversations=self.conversations,
            clock=self.world.clock,
            tracer=Tracer(self.world.clock, self.world.ids, subscribers=[self.world.events.append]),
            on_notice=on_notice,
        )

    def said(self, session_id: str) -> list[str]:
        return [m.content for m in self.conversations.for_session(session_id)]

    def events(self, kind: str) -> list[dict[str, Any]]:
        return [e.to_dict() for e in self.world.events if e.type == kind]


@pytest.fixture
def setup(world: World) -> Setup:
    return Setup(world)


def provider_cancels(world: World, booking_id: str = COOLBREEZE) -> None:
    MockControl(world.mock).cancel_by_provider(booking_id, "technician unavailable")


async def test_a_provider_cancellation_is_noticed_without_a_prompt(
    world: World, setup: Setup
) -> None:
    provider_cancels(world)

    (notice,) = await setup.monitor().check_once()

    assert (notice.booking_id, notice.decision, notice.session_id) == (
        COOLBREEZE,
        "replace",
        "sess_1",
    )
    (said,) = setup.said("sess_1")
    assert said.startswith(NOTICE) and "looking for a replacement" in said
    assert setup.told == [notice]  # handed on, to look for a replacement
    (started,) = [e for e in setup.events("run_started") if e["trigger"] == "provider_cancelled"]
    (decided,) = setup.events("notice_decision")
    assert decided["run_id"] == started["run_id"]
    assert (decided["decision"], decided["booking_id"]) == ("replace", COOLBREEZE)


async def test_a_pass_with_nothing_new_does_nothing_and_leaves_no_trace(
    world: World, setup: Setup
) -> None:
    assert await setup.monitor().check_once() == []
    assert setup.said("sess_1") == [] and setup.told == []
    assert not [e for e in setup.events("run_started") if e["trigger"] == "provider_cancelled"]


async def test_each_cancellation_is_handled_once_even_after_a_restart(
    world: World, setup: Setup
) -> None:
    provider_cancels(world)
    await setup.monitor().check_once()

    again = await setup.monitor().check_once()

    assert again == []
    assert len(setup.said("sess_1")) == 1 and len(setup.told) == 1


async def test_the_users_own_cancellation_is_not_a_provider_event(
    world: World, setup: Setup
) -> None:
    with pytest.raises(ProviderUnavailable):  # the contract: the first cancel call is a 503
        await world.mock.cancel(COOLBREEZE, "idem_user_cancel")
    await world.mock.cancel(COOLBREEZE, "idem_user_cancel")

    assert await setup.monitor().check_once() == []


async def test_a_cancelled_slot_already_past_is_recorded_without_telling_anyone(
    world: World, setup: Setup
) -> None:
    provider_cancels(world)
    world.clock.advance((6 * 24 + 2) * 3600)  # now Wed 11 Nov, 11:00 IST: the slot has gone

    (notice,) = await setup.monitor().check_once()

    assert notice.decision == "past"
    assert setup.said("sess_1") == [] and setup.told == []
    assert await setup.monitor().check_once() == []  # and it isn't looked at again


async def test_another_booking_for_that_service_means_a_notice_but_no_search(
    world: World, setup: Setup
) -> None:
    provider_cancels(world)
    day = DateRange(start=date(2026, 11, 10), end=date(2026, 11, 10))  # the same day
    search = await world.mock.search_providers("ac_servicing", "Indiranagar", day)
    assert isinstance(search, SearchOk)
    other = next(p for p in search.providers if p.provider_id != "ac_coolbreeze")
    held = await world.mock.hold_slot(other.provider_id, other.available_slots[0])
    assert isinstance(held, Held)
    await world.mock.book(held.hold.hold_id, "idem_other_ac")

    (notice,) = await setup.monitor().check_once()

    assert notice.decision == "notify_only"
    (said,) = setup.said("sess_1")
    assert said.startswith(NOTICE) and f"You already have {other.name} booked" in said
    assert setup.told == [notice]  # told, so the chat can show it; no replacement run


async def test_the_session_where_it_was_booked_is_told(world: World, setup: Setup) -> None:
    errand_id, approval_id = await world.approved_errand()  # a plumber, booked in sess_1
    booked = await world.flow().execute(errand_id, approval_id)
    assert booked.booking is not None
    world.errands.create_session("sess_2", user_id="user_1")  # a newer session
    provider_cancels(world, booked.booking.booking_id)

    (notice,) = await setup.monitor().check_once()

    assert notice.session_id == "sess_1"


async def test_a_booking_the_agent_did_not_make_goes_to_the_latest_session(
    world: World, setup: Setup
) -> None:
    world.clock.advance(60)
    world.errands.create_session("sess_2", user_id="user_1")
    provider_cancels(world)  # the seeded CoolBreeze booking belongs to no errand

    (notice,) = await setup.monitor().check_once()

    assert notice.session_id == "sess_2"
