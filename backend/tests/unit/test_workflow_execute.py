from datetime import timedelta
from pathlib import Path

import pytest

from errand_agent.mocks.scenario import Scenario
from errand_agent.providers.errors import ProviderTimeout
from errand_agent.workflow.approvals import ApprovalError
from errand_agent.workflow.errands import InvalidTransition
from errand_agent.workflow.guarded_env import BudgetMeter, GuardedToolEnv, LimitExceeded, RunBudget
from tests.unit.workflow_world import ScriptedEnv, World


def bookings(world: World) -> int:
    return world.mock_count(
        "SELECT count(*) FROM bookings WHERE user_id = 'user_1' AND idempotency_key IS NOT NULL"
    )


def book_keys(env: ScriptedEnv) -> list[str]:
    return [kw["args"][1] for name, kw in env.calls if name == "book"]


# --- happy path & approval enforcement ------------------------------------------------------


async def test_approved_booking_is_made_once(tmp_path: Path) -> None:
    world = World(tmp_path, Scenario(book_outcomes=["success"]))
    errand_id, approval_id = await world.approved_errand()

    result = await world.flow().execute(errand_id, approval_id)

    assert result.outcome == "booked" and result.booking is not None
    errand = world.errands.get(errand_id)
    assert errand.state == "booked" and errand.booking_id == result.booking.booking_id
    assert world.approvals.get(approval_id).status == "consumed"
    [action] = world.actions.for_errand(errand_id)
    assert (action.state, action.booking_id, action.attempts) == ("succeeded", errand.booking_id, 1)
    assert bookings(world) == 1


async def test_without_a_granted_approval_nothing_happens(tmp_path: Path) -> None:
    world = World(tmp_path, Scenario(book_outcomes=["success"]))
    errand_id, option = await world.comparing_errand()
    proposal = await world.flow().propose(errand_id, option, option.slots[0])
    assert proposal.approval is not None

    with pytest.raises(ApprovalError, match="pending"):
        await world.flow().execute(errand_id, proposal.approval.approval_id)

    assert world.errands.get(errand_id).state == "awaiting_approval"
    assert world.actions.for_errand(errand_id) == []
    assert bookings(world) == 0


async def test_repeated_confirmation_cannot_book_twice(tmp_path: Path) -> None:
    world = World(tmp_path, Scenario(book_outcomes=["success", "success"]))
    errand_id, approval_id = await world.approved_errand()
    await world.flow().execute(errand_id, approval_id)

    with pytest.raises((ApprovalError, InvalidTransition)):
        await world.flow().execute(errand_id, approval_id)

    assert bookings(world) == 1


# --- hold refresh before the commit point ----------------------------------------------------


async def test_near_expiry_hold_is_refreshed_and_approval_kept(tmp_path: Path) -> None:
    world = World(tmp_path, Scenario(book_outcomes=["success"]))
    errand_id, approval_id = await world.approved_errand()
    old_hold = world.errands.get(errand_id).hold_id
    world.clock.advance(9 * 60 + 45)  # 15 s left: inside the 30 s margin

    result = await world.flow().execute(errand_id, approval_id)

    assert result.outcome == "booked"
    [action] = world.actions.for_errand(errand_id)
    assert action.hold_id != old_hold  # booked on the refreshed hold


async def test_refresh_with_a_new_price_asks_again(tmp_path: Path) -> None:
    world = World(tmp_path)
    errand_id, approval_id = await world.approved_errand()
    world.clock.advance(9 * 60 + 45)
    pricier = world.open_mock(Scenario(price_drift={"plm_quickfix": 0.12}))

    result = await world.flow(env=pricier).execute(errand_id, approval_id)

    assert result.outcome == "needs_reapproval"
    assert result.approval is not None and result.approval.details.price_inr == 783
    assert world.approvals.get(approval_id).status == "invalidated"
    assert world.errands.get(errand_id).state == "awaiting_approval"
    assert bookings(world) == 0


async def test_refresh_when_the_slot_is_gone(tmp_path: Path) -> None:
    world = World(tmp_path)
    errand_id, approval_id = await world.approved_errand()
    slot = world.approvals.get(approval_id).details.slot
    world.clock.advance(10 * 60 + 1)  # our hold expired...
    other = world.open_mock(Scenario(), user_id="user_2")
    await other.hold_slot("plm_quickfix", slot)  # ...and someone else took the slot

    result = await world.flow().execute(errand_id, approval_id)

    assert result.outcome == "slot_gone"
    assert world.errands.get(errand_id).state == "comparing"
    assert world.approvals.get(approval_id).status == "invalidated"


# --- provider outcomes after the commit point ----------------------------------------------------


async def test_slot_taken_goes_back_to_comparing(tmp_path: Path) -> None:
    world = World(tmp_path, Scenario(book_outcomes=["slot_taken"]))
    errand_id, approval_id = await world.approved_errand()

    result = await world.flow().execute(errand_id, approval_id)

    assert result.outcome == "slot_taken"
    assert world.errands.get(errand_id).state == "comparing"
    assert world.actions.for_errand(errand_id)[0].state == "failed"
    assert bookings(world) == 0


async def test_hold_expired_at_book_time_asks_again(tmp_path: Path) -> None:
    world = World(tmp_path, Scenario(book_outcomes=["success"]))
    env = ScriptedEnv(world.mock)
    errand_id, approval_id = await world.approved_errand(env)
    env.before_book = lambda: world.clock.advance(11 * 60)  # expires just as we book

    result = await world.flow(env).execute(errand_id, approval_id)

    assert result.outcome == "needs_reapproval"
    assert result.approval is not None and result.approval.status == "pending"
    assert world.errands.get(errand_id).state == "awaiting_approval"
    assert bookings(world) == 0


# --- timeouts: never claim success until verified -----------------------------------------------


async def test_timeout_after_success_is_verified_not_rebooked(tmp_path: Path) -> None:
    world = World(tmp_path, Scenario(book_outcomes=["timeout_booked"]))
    env = ScriptedEnv(world.mock)
    errand_id, approval_id = await world.approved_errand(env)

    result = await world.flow(env).execute(errand_id, approval_id)

    assert result.outcome == "booked"
    assert len(book_keys(env)) == 1  # verified via status, not by booking again
    assert bookings(world) == 1
    history = [event for _, _, event, _, _ in world.errands.history(errand_id)]
    assert history[-3:] == ["approved", "timeout", "confirmed"]


async def test_timeout_without_success_retries_with_the_same_key(tmp_path: Path) -> None:
    world = World(tmp_path, Scenario(book_outcomes=["timeout_not_booked", "success"]))
    env = ScriptedEnv(world.mock)
    errand_id, approval_id = await world.approved_errand(env)

    result = await world.flow(env).execute(errand_id, approval_id)

    assert result.outcome == "booked"
    keys = book_keys(env)
    assert len(keys) == 2 and keys[0] == keys[1]
    assert bookings(world) == 1


async def test_repeated_failures_end_as_not_booked(tmp_path: Path) -> None:
    world = World(tmp_path, Scenario(book_outcomes=["timeout_not_booked"] * 3))
    env = ScriptedEnv(world.mock)
    errand_id, approval_id = await world.approved_errand(env)

    result = await world.flow(env).execute(errand_id, approval_id)

    assert result.outcome == "not_booked"
    assert len(book_keys(env)) == 3
    assert world.errands.get(errand_id).state == "comparing"
    assert bookings(world) == 0


async def test_unknowable_outcome_is_parked_as_unverified(tmp_path: Path) -> None:
    world = World(tmp_path, Scenario(book_outcomes=["timeout_booked"]))
    env = ScriptedEnv(world.mock)
    errand_id, approval_id = await world.approved_errand(env)
    env.status_error = ProviderTimeout("status endpoint down")
    before = world.clock.now()

    result = await world.flow(env).execute(errand_id, approval_id)

    assert result.outcome == "unverified" and result.booking is None
    assert world.errands.get(errand_id).state == "unverified"
    assert world.actions.for_errand(errand_id)[0].state == "unverified"
    status_checks = [c for c in env.calls if c[0] == "get_booking_status"]
    assert len(status_checks) == 3
    # waited 8 s (mock timeout) + backoff 1 + 2 between the three checks
    assert world.clock.now() - before == timedelta(seconds=11)


# --- budget around the commit point -------------------------------------------------------------


async def test_no_room_in_the_budget_stops_before_anything_happens(tmp_path: Path) -> None:
    world = World(tmp_path, Scenario(book_outcomes=["success"]))
    errand_id, approval_id = await world.approved_errand()
    meter = BudgetMeter(RunBudget(max_tool_calls=1), clock=world.clock, run=world.run)
    meter.charge_tool_call("search_providers")  # budget used up
    env = GuardedToolEnv(world.mock, run=world.run, meter=meter)

    with pytest.raises(LimitExceeded):
        await world.flow(env, meter).execute(errand_id, approval_id)

    assert world.approvals.get(approval_id).status == "granted"  # still usable next run
    assert world.actions.for_errand(errand_id) == []
    assert world.errands.get(errand_id).state == "awaiting_approval"


async def test_after_the_commit_point_the_booking_is_finished(tmp_path: Path) -> None:
    world = World(tmp_path, Scenario(book_outcomes=["timeout_not_booked", "success"]))
    errand_id, approval_id = await world.approved_errand()
    meter = BudgetMeter(RunBudget(max_tool_calls=1), clock=world.clock, run=world.run)
    env = GuardedToolEnv(world.mock, run=world.run, meter=meter)

    # Room for exactly one call at the commit point; the retry and status checks after
    # it are part of finishing what was started, so the budget doesn't block them.
    result = await world.flow(env, meter).execute(errand_id, approval_id)

    assert result.outcome == "booked"
    assert bookings(world) == 1
