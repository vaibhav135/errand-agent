import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from errand_agent.core.clock import FakeClock
from errand_agent.core.ids import SequentialIdGen
from errand_agent.mocks.scenario import Scenario
from errand_agent.mocks.world import MockWorld
from errand_agent.providers.models import DateRange, SearchOk
from errand_agent.store.db import connect
from errand_agent.store.migrate import migrate, migrations_for
from errand_agent.tracing.recorder import RunTrace, TraceEvent, Tracer
from errand_agent.workflow.approvals import ApprovalStore, fingerprint
from errand_agent.workflow.booking import BookingFlow
from errand_agent.workflow.errands import ErrandStore, InvalidTransition
from errand_agent.workflow.ranking import Option, rank_options
from errand_agent.workflow.requirements import Requirements

NOW = datetime(2026, 11, 5, 3, 30, tzinfo=UTC)
WEEK = DateRange(start=date(2026, 11, 5), end=date(2026, 11, 11))
REQS = Requirements(service="plumber", area="Indiranagar", date_range=WEEK, budget_max_inr=1500)


class World:
    """Agent-side stores and a mock world sharing one FakeClock."""

    def __init__(self, tmp_path: Path, scenario: Scenario) -> None:
        self.clock = FakeClock(NOW)
        ids = SequentialIdGen()
        self.conn: sqlite3.Connection = connect(tmp_path / "agent.db")
        migrate(self.conn, migrations_for("agent"), self.clock)
        self.mock_path = tmp_path / "mock_world.db"
        self.mock = MockWorld.open(self.mock_path, clock=self.clock, ids=ids, scenario=scenario)
        self.errands = ErrandStore(self.conn, clock=self.clock, ids=ids)
        self.approvals = ApprovalStore(self.conn, clock=self.clock, ids=ids)
        self.events: list[TraceEvent] = []
        self.run: RunTrace = Tracer(self.clock, ids, subscribers=[self.events.append]).start_run(
            session_id="sess_1", trigger="test"
        )
        self.errands.create_session("sess_1", user_id="user_1")

    def flow(self) -> BookingFlow:
        return BookingFlow(
            conn=self.conn,
            env=self.mock,
            errands=self.errands,
            approvals=self.approvals,
            clock=self.clock,
            ids=SequentialIdGen(),
            run=self.run,
        )

    async def comparing_errand(self) -> tuple[str, Option]:
        errand = self.errands.create("sess_1", "book")
        self.errands.set_requirements(errand.errand_id, REQS)
        search = await self.mock.search_providers("plumber", "Indiranagar", WEEK)
        assert isinstance(search, SearchOk)
        option = next(
            o
            for o in rank_options(search.providers, REQS).options
            if o.provider_id == "plm_quickfix"
        )
        self.errands.transition(errand.errand_id, "options_ready", run=self.run)
        return errand.errand_id, option


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path, Scenario())


async def test_proposal_holds_the_slot_and_asks_for_approval(world: World) -> None:
    errand_id, option = await world.comparing_errand()

    proposal = await world.flow().propose(errand_id, option, option.slots[0])

    assert proposal.status == "proposed"
    assert proposal.hold is not None and proposal.approval is not None
    errand = world.errands.get(errand_id)
    assert errand.state == "awaiting_approval"
    assert errand.hold_id == proposal.hold.hold_id
    assert errand.hold_expires_at == NOW + timedelta(minutes=10)
    approval = proposal.approval
    assert approval.status == "pending"
    d = approval.details
    assert (d.action, d.provider_id, d.provider_name) == (
        "book",
        "plm_quickfix",
        "QuickFix Plumbing",
    )
    assert d.slot == option.slots[0] and d.price_inr == 699
    assert approval.fingerprint == fingerprint(d)
    assert proposal.price_changed_from is None


async def test_proposal_shows_a_changed_quote(tmp_path: Path) -> None:
    world = World(tmp_path, Scenario(price_drift={"plm_quickfix": 0.12}))
    errand_id, option = await world.comparing_errand()

    proposal = await world.flow().propose(errand_id, option, option.slots[0])

    assert proposal.approval is not None
    assert proposal.approval.details.price_inr == 783  # the user approves the real price
    assert proposal.price_changed_from == 699


async def test_unavailable_slot_leaves_the_errand_comparing(world: World) -> None:
    errand_id, option = await world.comparing_errand()
    taken = option.slots[0]
    other_user = MockWorld.open(
        world.mock_path,
        clock=world.clock,
        ids=SequentialIdGen(),
        scenario=Scenario(),
        user_id="user_2",
    )
    await other_user.hold_slot(option.provider_id, taken)

    proposal = await world.flow().propose(errand_id, option, taken)

    assert proposal.status == "unavailable" and proposal.reason == "slot_unavailable"
    assert world.errands.get(errand_id).state == "comparing"
    assert world.approvals.pending_for_session("sess_1") == []


async def test_proposing_from_the_wrong_state_places_no_hold(world: World) -> None:
    errand = world.errands.create("sess_1", "book")  # still collecting
    _, option = await world.comparing_errand()

    with pytest.raises(InvalidTransition):
        await world.flow().propose(errand.errand_id, option, option.slots[0])

    assert connect(world.mock_path).execute("SELECT count(*) FROM holds").fetchone()[0] == 0


async def test_only_book_errands_can_be_proposed(world: World) -> None:
    errand = world.errands.create("sess_1", "cancel", booking_id="bk_hist_coolbreeze")
    _, option = await world.comparing_errand()

    with pytest.raises(InvalidTransition, match="book"):
        await world.flow().propose(errand.errand_id, option, option.slots[0])


async def test_proposal_is_traced_after_commit(world: World) -> None:
    errand_id, option = await world.comparing_errand()

    await world.flow().propose(errand_id, option, option.slots[0])

    kinds = [(e.type, e.to_dict().get("to") or e.to_dict().get("status")) for e in world.events]
    assert ("approval", "pending") in kinds
    assert ("state_transition", "awaiting_approval") in kinds
