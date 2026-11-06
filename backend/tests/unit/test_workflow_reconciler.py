from datetime import timedelta
from pathlib import Path

import pytest

from errand_agent.mocks.scenario import Scenario
from errand_agent.providers.errors import ProviderTimeout
from errand_agent.tracing.recorder import Tracer
from errand_agent.workflow.reconciler import Reconciler
from tests.unit.test_workflow_changes import COOLBREEZE, approved, booking_row
from tests.unit.workflow_world import ProcessKilled, ScriptedEnv, World


def reconciler(world: World, env=None) -> Reconciler:  # type: ignore[no-untyped-def]
    return Reconciler(
        conn=world.conn,
        env=env or world.mock,
        errands=world.errands,
        approvals=world.approvals,
        actions=world.actions,
        clock=world.clock,
        tracer=Tracer(world.clock, world.ids, subscribers=[world.events.append]),
    )


def bookings(world: World) -> int:
    return world.mock_count(
        "SELECT count(*) FROM bookings WHERE user_id = 'user_1' AND idempotency_key IS NOT NULL"
    )


async def crashed_booking(world: World, when: str) -> str:
    env = ScriptedEnv(world.mock)
    errand_id, approval_id = await world.approved_errand(env)
    env.crash["book"] = when
    with pytest.raises(ProcessKilled):
        await world.flow(env).execute(errand_id, approval_id)
    assert world.errands.get(errand_id).state == "booking"
    assert world.actions.for_errand(errand_id)[0].state == "in_flight"
    return errand_id


# --- bookings left behind by a crash ------------------------------------------------------


async def test_crash_after_the_provider_booked_is_confirmed_not_rebooked(tmp_path: Path) -> None:
    world = World(tmp_path, Scenario(book_outcomes=["success"]))
    errand_id = await crashed_booking(world, "after")
    env = ScriptedEnv(world.mock)

    [result] = await reconciler(world, env).reconcile_once()

    assert result.outcome == "booked"
    assert world.errands.get(errand_id).state == "booked"
    assert [name for name, _ in env.calls] == ["get_booking_status"]  # no second book
    assert bookings(world) == 1


async def test_crash_before_the_request_left_completes_the_approved_booking(
    tmp_path: Path,
) -> None:
    world = World(tmp_path, Scenario(book_outcomes=["success"]))
    errand_id = await crashed_booking(world, "before")
    env = ScriptedEnv(world.mock)

    [result] = await reconciler(world, env).reconcile_once()

    assert result.outcome == "booked"
    errand = world.errands.get(errand_id)
    assert errand.state == "booked"
    [action] = world.actions.for_errand(errand_id)
    book_calls = [kw for name, kw in env.calls if name == "book"]
    assert book_calls == [{"args": (action.hold_id, action.idempotency_key)}]  # same hold + key
    assert bookings(world) == 1


async def test_completion_with_an_expired_hold_books_nothing(tmp_path: Path) -> None:
    world = World(tmp_path, Scenario(book_outcomes=["success"]))
    errand_id = await crashed_booking(world, "before")
    world.clock.advance(11 * 60)  # the restart took a while: the hold expired

    [result] = await reconciler(world).reconcile_once()

    assert result.outcome == "not_booked"
    assert world.errands.get(errand_id).state == "comparing"
    assert world.actions.for_errand(errand_id)[0].state == "failed"
    assert bookings(world) == 0


async def test_unverified_booking_is_resolved_when_status_is_reachable(tmp_path: Path) -> None:
    world = World(tmp_path, Scenario(book_outcomes=["timeout_booked"]))
    env = ScriptedEnv(world.mock)
    errand_id, approval_id = await world.approved_errand(env)
    env.status_error = ProviderTimeout("status endpoint down")
    assert (await world.flow(env).execute(errand_id, approval_id)).outcome == "unverified"

    [result] = await reconciler(world).reconcile_once()  # the endpoint is back

    assert result.outcome == "booked"
    assert world.errands.get(errand_id).state == "booked"
    assert bookings(world) == 1


async def test_still_unreachable_stays_parked(tmp_path: Path) -> None:
    world = World(tmp_path, Scenario(book_outcomes=["timeout_booked"]))
    env = ScriptedEnv(world.mock)
    errand_id, approval_id = await world.approved_errand(env)
    env.status_error = ProviderTimeout("status endpoint down")
    await world.flow(env).execute(errand_id, approval_id)

    [result] = await reconciler(world, env).reconcile_once()

    assert result.outcome == "still_unknown"
    assert world.errands.get(errand_id).state == "unverified"


# --- pending cancellations ----------------------------------------------------------------


async def pending_cancel(world: World) -> str:
    errand_id, approval_id = await approved(world, "cancel")
    result = await world.change_flow().execute_cancel(errand_id, approval_id)
    assert result.outcome == "cancel_pending"
    return errand_id


async def test_pending_cancel_is_retried_once_due_and_succeeds(tmp_path: Path) -> None:
    world = World(tmp_path, Scenario(cancel_transient_failures=10))
    errand_id = await pending_cancel(world)
    world.mock = world.open_mock(Scenario(cancel_transient_failures=4))  # provider recovers

    assert await reconciler(world).reconcile_once() == []  # not due yet (30 s backoff)
    world.clock.advance(31)
    [result] = await reconciler(world).reconcile_once()

    assert result.outcome == "cancelled"
    assert world.errands.get(errand_id).state == "cancelled"
    assert booking_row(world, COOLBREEZE)[0] == "cancelled"


async def test_pending_cancel_backs_off_while_the_provider_stays_down(tmp_path: Path) -> None:
    world = World(tmp_path, Scenario(cancel_transient_failures=100))
    errand_id = await pending_cancel(world)
    r = reconciler(world)

    world.clock.advance(31)
    [first] = await r.reconcile_once()
    world.clock.advance(31)
    assert await r.reconcile_once() == []  # backoff doubled to 60 s
    world.clock.advance(30)
    [second] = await r.reconcile_once()

    assert first.outcome == second.outcome == "still_pending"
    assert world.errands.get(errand_id).state == "cancel_pending"


async def test_pending_cancel_gives_up_when_the_window_closes(tmp_path: Path) -> None:
    world = World(tmp_path, Scenario(cancel_transient_failures=100))
    errand_id = await pending_cancel(world)
    world.clock.advance(16 * 60)  # past approval + 15 min
    env = ScriptedEnv(world.mock)

    [result] = await reconciler(world, env).reconcile_once()

    assert result.outcome == "cancel_failed"
    assert world.errands.get(errand_id).state == "cancel_failed"
    assert [name for name, _ in env.calls if name == "cancel"] == []  # no pointless retry
    assert booking_row(world, COOLBREEZE)[0] == "confirmed"


# --- reschedule left in flight ------------------------------------------------------------


async def test_crashed_reschedule_is_completed_with_the_same_key(tmp_path: Path) -> None:
    from tests.unit.test_workflow_changes import coolbreeze_slot

    world = World(tmp_path)
    target = await coolbreeze_slot(world)
    errand_id, approval_id = await approved(world, "reschedule", slot=target)
    env = ScriptedEnv(world.mock)
    env.crash["reschedule"] = "before"
    with pytest.raises(ProcessKilled):
        await world.change_flow(env).execute_reschedule(errand_id, approval_id)

    [result] = await reconciler(world).reconcile_once()

    assert result.outcome == "rescheduled"
    assert world.errands.get(errand_id).state == "rescheduled"


# --- scope and tracing --------------------------------------------------------------------


async def test_nothing_open_means_no_trace(tmp_path: Path) -> None:
    world = World(tmp_path)
    before = len(world.events)

    assert await reconciler(world).reconcile_once() == []

    assert len(world.events) == before


async def test_reconcile_can_be_scoped_to_one_session(tmp_path: Path) -> None:
    world = World(tmp_path, Scenario(book_outcomes=["success"]))
    await crashed_booking(world, "after")

    assert await reconciler(world).reconcile_once(session_id="sess_other") == []
    assert len(await reconciler(world).reconcile_once(session_id="sess_1")) == 1


async def test_a_pass_with_work_is_traced_as_a_reconcile_run(tmp_path: Path) -> None:
    world = World(tmp_path, Scenario(book_outcomes=["success"]))
    errand_id = await crashed_booking(world, "after")
    world.events.clear()

    await reconciler(world).reconcile_once()

    started = [e.to_dict() for e in world.events if e.type == "run_started"]
    finished = [e.to_dict() for e in world.events if e.type == "run_finished"]
    assert [s["trigger"] for s in started] == ["reconcile"]
    assert finished[0]["outcome"] == "reconciled"
    assert any(
        e.type == "state_transition" and e.to_dict()["errand_id"] == errand_id for e in world.events
    )


async def test_background_loop_runs_passes_until_stopped(tmp_path: Path) -> None:
    world = World(tmp_path, Scenario(book_outcomes=["success"]))
    errand_id = await crashed_booking(world, "after")
    before = world.clock.now()

    await reconciler(world).run_periodically(interval_seconds=30, max_passes=2)

    assert world.errands.get(errand_id).state == "booked"
    assert world.clock.now() - before == timedelta(seconds=30)  # one wait between two passes
