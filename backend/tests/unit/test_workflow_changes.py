from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest

from errand_agent.core.clock import utc_text
from errand_agent.mocks.scenario import Scenario, SlotRef
from errand_agent.providers.models import DateRange, SearchOk
from errand_agent.store.db import connect
from errand_agent.workflow.approvals import ApprovalError
from errand_agent.workflow.changes import ChangeProposal, cancel_deadline
from tests.unit.workflow_world import ScriptedEnv, World

COOLBREEZE = "bk_hist_coolbreeze"  # seeded: confirmed, in 5 days at 10:00 IST, ₹1200
SPARKS = "bk_hist_sparks"  # seeded: completed


def booking_row(world: World, booking_id: str) -> tuple[str, str]:
    row = (
        connect(world.mock_path)
        .execute("SELECT status, slot FROM bookings WHERE booking_id = ?", (booking_id,))
        .fetchone()
    )
    return row["status"], row["slot"]


async def proposed(
    world: World,
    kind: Literal["cancel", "reschedule"],
    booking_id: str = COOLBREEZE,
    slot: datetime | None = None,
) -> tuple[str, ChangeProposal]:
    errand = world.errands.create("sess_1", kind, booking_id=booking_id)
    flow = world.change_flow()
    if kind == "cancel":
        return errand.errand_id, await flow.propose_cancel(errand.errand_id)
    assert slot is not None, "a reschedule needs a target slot"
    return errand.errand_id, await flow.propose_reschedule(errand.errand_id, slot)


async def approved(
    world: World, kind: Literal["cancel", "reschedule"], slot: datetime | None = None
) -> tuple[str, str]:
    errand_id, proposal = await proposed(world, kind, slot=slot)
    a = proposal.approval
    assert a is not None
    world.approvals.grant(a.approval_id, presented_fingerprint=a.fingerprint, via="button")
    return errand_id, a.approval_id


async def coolbreeze_slot(world: World) -> datetime:
    start = world.clock.now().date()
    search = await world.mock.search_providers(
        "ac_servicing", "Indiranagar", DateRange(start=start, end=start + timedelta(days=13))
    )
    assert isinstance(search, SearchOk)
    return next(p for p in search.providers if p.provider_id == "ac_coolbreeze").available_slots[0]


# --- proposing ------------------------------------------------------------------------------


async def test_propose_cancel_asks_to_approve_the_exact_booking(tmp_path: Path) -> None:
    world = World(tmp_path)

    errand_id, proposal = await proposed(world, "cancel")

    assert proposal.status == "proposed" and proposal.approval is not None
    d = proposal.approval.details
    assert (d.action, d.booking_id, d.provider_id, d.price_inr) == (
        "cancel",
        COOLBREEZE,
        "ac_coolbreeze",
        1200,
    )
    assert world.errands.get(errand_id).state == "awaiting_approval"


@pytest.mark.parametrize(
    ("booking_id", "status"), [(SPARKS, "not_changeable"), ("bk_nope", "booking_not_found")]
)
async def test_bookings_that_cannot_change_are_refused_up_front(
    tmp_path: Path, booking_id: str, status: str
) -> None:
    world = World(tmp_path)

    errand_id, proposal = await proposed(world, "cancel", booking_id=booking_id)

    assert proposal.status == status and proposal.approval is None
    assert world.errands.get(errand_id).state == "collecting"


# --- cancel -----------------------------------------------------------------------------------


async def test_cancel_retries_the_503_with_the_same_key(tmp_path: Path) -> None:
    world = World(tmp_path)  # default scenario: the first attempt per booking is a 503
    env = ScriptedEnv(world.mock)
    errand_id, approval_id = await approved(world, "cancel")
    before = world.clock.now()

    result = await world.change_flow(env).execute_cancel(errand_id, approval_id)

    assert result.outcome == "cancelled"
    assert world.errands.get(errand_id).state == "cancelled"
    keys = [kw["args"][1] for name, kw in env.calls if name == "cancel"]
    assert len(keys) == 2 and keys[0] == keys[1]
    assert world.clock.now() - before == timedelta(seconds=1)
    assert booking_row(world, COOLBREEZE)[0] == "cancelled"
    assert world.actions.for_errand(errand_id)[0].state == "succeeded"


async def test_persistent_503s_leave_the_booking_active_and_pending(tmp_path: Path) -> None:
    world = World(tmp_path, Scenario(cancel_transient_failures=10))
    errand_id, approval_id = await approved(world, "cancel")
    before = world.clock.now()

    result = await world.change_flow().execute_cancel(errand_id, approval_id)

    assert result.outcome == "cancel_pending"
    assert world.errands.get(errand_id).state == "cancel_pending"
    assert booking_row(world, COOLBREEZE)[0] == "confirmed"  # honestly still active
    assert world.actions.for_errand(errand_id)[0].state == "in_flight"  # reconciler retries
    assert world.clock.now() - before == timedelta(seconds=1 + 2 + 4)


async def test_no_background_retry_when_the_visit_is_under_an_hour_away(tmp_path: Path) -> None:
    world = World(tmp_path, Scenario(cancel_transient_failures=10))
    _, slot_text = booking_row(world, COOLBREEZE)
    world.clock.advance(
        (datetime.fromisoformat(slot_text) - world.clock.now()).total_seconds() - 30 * 60
    )
    errand_id, approval_id = await approved(world, "cancel")

    result = await world.change_flow().execute_cancel(errand_id, approval_id)

    assert result.outcome == "cancel_failed"
    events = [event for _, _, event, _, _ in world.errands.history(errand_id)]
    assert events[-2:] == ["retries_exhausted", "window_closed"]


async def test_provider_refusal_at_execution_fails_the_cancel(tmp_path: Path) -> None:
    world = World(tmp_path, Scenario(cancel_transient_failures=0))
    errand_id, approval_id = await approved(world, "cancel")
    world.mock_exec("UPDATE bookings SET status = 'completed' WHERE booking_id = ?", COOLBREEZE)

    result = await world.change_flow().execute_cancel(errand_id, approval_id)

    assert result.outcome == "cancel_failed"
    assert world.errands.get(errand_id).state == "cancel_failed"


async def test_booking_changed_since_approval_asks_again(tmp_path: Path) -> None:
    world = World(tmp_path, Scenario(cancel_transient_failures=0))
    errand_id, approval_id = await approved(world, "cancel")
    _, slot_text = booking_row(world, COOLBREEZE)
    moved = utc_text(datetime.fromisoformat(slot_text) + timedelta(days=1))
    world.mock_exec("UPDATE bookings SET slot = ? WHERE booking_id = ?", moved, COOLBREEZE)

    result = await world.change_flow().execute_cancel(errand_id, approval_id)

    assert result.outcome == "booking_changed"
    assert world.approvals.get(approval_id).status == "invalidated"
    assert world.errands.get(errand_id).state == "collecting"
    assert world.actions.for_errand(errand_id) == []
    assert booking_row(world, COOLBREEZE)[0] == "confirmed"


async def test_cancel_needs_a_granted_approval(tmp_path: Path) -> None:
    world = World(tmp_path)
    errand_id, proposal = await proposed(world, "cancel")
    assert proposal.approval is not None

    with pytest.raises(ApprovalError):
        await world.change_flow().execute_cancel(errand_id, proposal.approval.approval_id)

    assert booking_row(world, COOLBREEZE)[0] == "confirmed"


def test_cancel_deadline_is_the_earlier_of_window_and_visit_cutoff() -> None:
    approved_at = datetime(2026, 11, 5, 4, 0, tzinfo=UTC)
    far_visit = approved_at + timedelta(days=5)
    near_visit = approved_at + timedelta(minutes=70)

    assert cancel_deadline(approved_at, far_visit) == approved_at + timedelta(minutes=15)
    assert cancel_deadline(approved_at, near_visit) == near_visit - timedelta(hours=1)


# --- reschedule -----------------------------------------------------------------------------------


async def test_reschedule_moves_the_booking_at_the_same_price(tmp_path: Path) -> None:
    world = World(tmp_path)
    target = await coolbreeze_slot(world)
    errand_id, approval_id = await approved(world, "reschedule", slot=target)
    d = world.approvals.get(approval_id).details
    assert (d.action, d.slot, d.price_inr, d.booking_id) == ("reschedule", target, 1200, COOLBREEZE)

    result = await world.change_flow().execute_reschedule(errand_id, approval_id)

    assert result.outcome == "rescheduled" and result.booking is not None
    assert result.booking.slot == target and result.booking.price_inr == 1200
    assert world.errands.get(errand_id).state == "rescheduled"
    assert booking_row(world, COOLBREEZE)[1] == utc_text(target)


async def test_slot_conflict_goes_back_to_choosing(tmp_path: Path) -> None:
    world = World(tmp_path)
    target = await coolbreeze_slot(world)
    world.mock = world.open_mock(
        Scenario(slot_conflicts=[SlotRef(provider_id="ac_coolbreeze", slot=target)])
    )
    original = booking_row(world, COOLBREEZE)
    errand_id, approval_id = await approved(world, "reschedule", slot=target)

    result = await world.change_flow().execute_reschedule(errand_id, approval_id)

    assert result.outcome == "slot_conflict"
    assert world.errands.get(errand_id).state == "collecting"
    assert booking_row(world, COOLBREEZE) == original


async def test_booking_no_longer_reschedulable_fails_the_errand(tmp_path: Path) -> None:
    world = World(tmp_path)
    target = await coolbreeze_slot(world)
    errand_id, approval_id = await approved(world, "reschedule", slot=target)
    world.mock_exec("UPDATE bookings SET status = 'completed' WHERE booking_id = ?", COOLBREEZE)

    result = await world.change_flow().execute_reschedule(errand_id, approval_id)

    assert result.outcome == "reschedule_failed"
    assert world.errands.get(errand_id).state == "reschedule_failed"
