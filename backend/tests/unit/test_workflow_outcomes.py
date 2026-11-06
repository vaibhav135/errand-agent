from datetime import UTC, datetime
from pathlib import Path

import pytest

from errand_agent.core.clock import IST, FakeClock
from errand_agent.core.ids import SequentialIdGen
from errand_agent.providers.models import Booking
from errand_agent.tracing.recorder import TraceEvent, Tracer
from errand_agent.workflow.booking import Execution
from errand_agent.workflow.changes import ChangeResult
from errand_agent.workflow.guarded_env import LimitExceeded, RunBudget
from errand_agent.workflow.outcomes import (
    RunContext,
    RunOutcome,
    for_change,
    for_error,
    for_execution,
    for_limit,
    run_with_outcome,
)
from tests.unit.workflow_world import World

NOW = datetime(2026, 11, 5, 3, 30, tzinfo=UTC)
SLOT = datetime(2026, 11, 7, 9, 0, tzinfo=IST)
BOOKING = Booking(
    booking_id="bk_0001",
    user_id="user_1",
    provider_id="plm_quickfix",
    provider_name="QuickFix Plumbing",
    service="plumber",
    slot=SLOT,
    price_inr=699,
    status="confirmed",
)


# --- facts come from code ----------------------------------------------------------------------


def test_booked_message_states_the_verified_facts() -> None:
    o = for_execution(Execution(outcome="booked", booking=BOOKING))

    assert o.kind == "booked"
    assert "QuickFix Plumbing" in o.message and "₹699" in o.message
    assert "Sat 07 Nov, 09:00" in o.message and "bk_0001" in o.message


@pytest.mark.parametrize("outcome", ["unverified", "not_booked", "slot_taken", "slot_gone"])
def test_unconfirmed_outcomes_never_claim_a_booking(outcome: str) -> None:
    o = for_execution(Execution(outcome=outcome))  # type: ignore[arg-type]

    assert o.kind == outcome
    assert "booked" not in o.message.lower().replace("not booked", "").replace("book again", "")


def test_unverified_tells_the_user_not_to_book_again() -> None:
    o = for_execution(Execution(outcome="unverified"))

    assert "checking" in o.message.lower() and "don't book again" in o.message.lower()


def test_price_change_shows_both_prices(tmp_path: Path) -> None:
    world = World(tmp_path)
    errand = world.errands.create("sess_1", "book")
    from errand_agent.workflow.approvals import ApprovalDetails

    approval = world.approvals.request(
        errand.errand_id,
        ApprovalDetails(
            action="book",
            provider_id="plm_quickfix",
            provider_name="QuickFix Plumbing",
            slot=SLOT,
            price_inr=783,
        ),
    )

    o = for_execution(Execution(outcome="needs_reapproval", approval=approval), previous_price=699)

    assert "₹699" in o.message and "₹783" in o.message
    assert o.approval == approval  # the card to show


@pytest.mark.parametrize(
    ("result", "must_say"),
    [
        (ChangeResult(outcome="cancelled", booking=BOOKING), "cancelled"),
        (ChangeResult(outcome="cancel_pending", booking=BOOKING), "still active"),
        (ChangeResult(outcome="cancel_failed", reason="window_closed"), "contact"),
        (ChangeResult(outcome="cancel_failed", reason="not_cancellable"), "can't be cancelled"),
        (ChangeResult(outcome="rescheduled", booking=BOOKING), "moved"),
        (ChangeResult(outcome="slot_conflict"), "another time"),
        (ChangeResult(outcome="booking_changed"), "changed"),
    ],
)
def test_change_outcomes_are_explained(result: ChangeResult, must_say: str) -> None:
    assert must_say in for_change(result).message.lower()


@pytest.mark.parametrize("limit", ["tool_calls", "model_turns", "wall_clock"])
def test_limit_outcome_is_honest_and_resumable(limit: str) -> None:
    o = for_limit(LimitExceeded(limit, 20, 20, "hold_slot"))  # type: ignore[arg-type]

    assert o.kind == "limit_reached"
    assert "nothing was booked or changed" in o.message.lower()
    assert "continue" in o.message.lower()


def test_error_while_a_side_effect_is_in_flight_does_not_claim_nothing_happened() -> None:
    in_flight = for_error(RuntimeError("boom"), errand_state="booking")
    idle = for_error(RuntimeError("boom"), errand_state="comparing")

    assert "checking" in in_flight.message.lower()
    assert "nothing was booked" not in in_flight.message.lower()
    assert "nothing was booked or changed" in idle.message.lower()


# --- every exit path ends the run with an outcome ------------------------------------------------


@pytest.fixture
def events() -> list[TraceEvent]:
    return []


@pytest.fixture
def tracer(events: list[TraceEvent]) -> Tracer:
    return Tracer(FakeClock(NOW), SequentialIdGen(), subscribers=[events.append])


async def run(tracer: Tracer, tmp_path: Path, body) -> RunOutcome:  # type: ignore[no-untyped-def]
    world = World(tmp_path)
    return await run_with_outcome(
        tracer,
        session_id="sess_1",
        trigger="user_message",
        budget=RunBudget(max_tool_calls=1),
        env=world.mock,
        clock=world.clock,
        errands=world.errands,
        body=body,
    )


def finished(events: list[TraceEvent]) -> dict[str, object]:
    [event] = [e.to_dict() for e in events if e.type == "run_finished"]
    return event


async def test_normal_exit(tracer: Tracer, events: list[TraceEvent], tmp_path: Path) -> None:
    async def body(ctx: RunContext) -> RunOutcome:
        return RunOutcome(kind="needs_info", message="Which area?")

    outcome = await run(tracer, tmp_path, body)

    assert outcome.kind == "needs_info"
    assert (finished(events)["outcome"], finished(events)["message"]) == (
        "needs_info",
        "Which area?",
    )


async def test_limit_exit(tracer: Tracer, events: list[TraceEvent], tmp_path: Path) -> None:
    async def body(ctx: RunContext) -> RunOutcome:
        await ctx.env.list_bookings("user_1")
        await ctx.env.list_bookings("user_1")  # second call exceeds max_tool_calls=1
        raise AssertionError("unreachable")

    outcome = await run(tracer, tmp_path, body)

    assert outcome.kind == "limit_reached"
    assert finished(events)["outcome"] == "limit_reached"


async def test_unexpected_error_exit(
    tracer: Tracer, events: list[TraceEvent], tmp_path: Path
) -> None:
    async def body(ctx: RunContext) -> RunOutcome:
        raise ValueError("bug in some step")

    outcome = await run(tracer, tmp_path, body)

    assert outcome.kind == "error"
    assert finished(events)["outcome"] == "error"
    assert finished(events)["error"] == {"type": "ValueError", "message": "bug in some step"}


async def test_error_message_reflects_the_errand_the_body_was_working_on(
    tracer: Tracer, events: list[TraceEvent], tmp_path: Path
) -> None:
    world = World(tmp_path)
    errand = world.errands.create("sess_1", "cancel", booking_id="bk_x")
    for event in ("option_proposed", "approved"):
        world.errands.transition(errand.errand_id, event)  # type: ignore[arg-type]

    async def body(ctx: RunContext) -> RunOutcome:
        ctx.errand_id = errand.errand_id
        raise RuntimeError("connection reset")

    outcome = await run_with_outcome(
        tracer,
        session_id="sess_1",
        trigger="user_message",
        budget=RunBudget(),
        env=world.mock,
        clock=world.clock,
        errands=world.errands,
        body=body,
    )

    assert "checking" in outcome.message.lower()


async def test_no_exit_path_is_unspecified(
    tracer: Tracer, events: list[TraceEvent], tmp_path: Path
) -> None:
    async def ok(ctx: RunContext) -> RunOutcome:
        return RunOutcome(kind="needs_info", message="?")

    async def boom(ctx: RunContext) -> RunOutcome:
        raise RuntimeError("x")

    for body in (ok, boom):
        await run(tracer, tmp_path / body.__name__, body)

    outcomes = [e.to_dict()["outcome"] for e in events if e.type == "run_finished"]
    assert outcomes and "unspecified" not in outcomes
