from datetime import timedelta
from pathlib import Path

import pytest

from errand_agent.core.ids import SequentialIdGen
from errand_agent.mocks.scenario import Scenario
from errand_agent.mocks.world import MockWorld
from errand_agent.store.db import connect
from errand_agent.workflow.approvals import fingerprint
from errand_agent.workflow.errands import InvalidTransition
from tests.unit.workflow_world import NOW, World


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
