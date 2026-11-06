"""Eval scenarios: booking, end to end (happy path and booking correctness)."""

from pathlib import Path

import pytest

from errand_agent.mocks.scenario import Scenario, SlotRef
from tests.evals.conftest import EvalRow
from tests.evals.harness import SESSION, Sim, proposes, to_card
from tests.unit.agent_script import ScriptedModel, call, say
from tests.unit.test_agent_tools import FULL
from tests.unit.test_workflow_changes import COOLBREEZE, booking_row, coolbreeze_slot
from tests.unit.workflow_world import ProcessKilled, ScriptedEnv

AREA_AND_DATES = {k: v for k, v in FULL.items() if k != "service"}


@pytest.mark.eval(
    scenario="Full booking from a vague request",
    category="happy path",
    expected="asks only what's missing; options; pick holds; approve books once",
)
async def test_full_booking(tmp_path: Path, report: EvalRow) -> None:
    sim = Sim(
        tmp_path,
        ScriptedModel(
            [call("update_requirements", service="plumber")],
            [say("Which area, and which dates?")],
            [call("update_requirements", **AREA_AND_DATES)],
            [call("search_and_compare")],
            [say("Here are three plumbers.")],
        ),
    )

    asked = await sim.say("I need a plumber")
    found = await sim.say("Indiranagar, 5 to 8 Nov, mornings, under 1500")
    options = found.outcome.options
    assert asked.outcome.kind == "answered" and options
    held = await sim.pick(options[0].provider_id, options[0].slots[0])
    assert held.outcome.kind == "needs_approval" and not sim.agent_bookings()
    booked = await sim.approve()

    assert booked.outcome.kind == "booked"
    assert len(sim.agent_bookings()) == 1
    sim.check()
    report.actual = f"asked, {len(options)} options, held {options[0].name}, booked once"
    report.notes = "nothing booked before the approval click"


@pytest.mark.eval(
    scenario="'yes' twice, then Approve clicked on the used card",
    category="repeated confirmation",
    expected="one booking; the second yes and the click book nothing",
)
async def test_repeated_confirmation(tmp_path: Path, report: EvalRow) -> None:
    sim = Sim(tmp_path, proposes([say("That's already booked.")]))
    card = await to_card(sim)

    first = await sim.say("yes")
    second = await sim.say("yes")  # nothing pending now: an ordinary message
    clicked = await sim.handler.decide(
        session_id=SESSION,
        approval_id=card.approval_id,
        fingerprint=card.fingerprint,
        approve=True,
    )

    assert first.outcome.kind == "booked"
    assert second.outcome.kind == "answered"
    assert "already decided" in clicked.outcome.message
    assert len(sim.agent_bookings()) == 1
    sim.check()
    report.actual = "booked once; second yes answered, click: 'already decided'"
    report.notes = f"{len(sim.provider_calls('book'))} book call in all"


@pytest.mark.eval(
    scenario="Booking times out, but it went through",
    category="timeout after success",
    expected="checks the provider's status, reports booked, never books again",
)
async def test_timeout_after_success(tmp_path: Path, report: EvalRow) -> None:
    sim = Sim(tmp_path, proposes(), scenario=Scenario(book_outcomes=["timeout_booked"]))
    await to_card(sim)

    reply = await sim.say("yes")

    books, checks = sim.provider_calls("book"), sim.provider_calls("get_booking_status")
    assert reply.outcome.kind == "booked"
    assert len(sim.agent_bookings()) == 1
    assert [c["status"] for c in books] == ["error"]  # the one call, timed out
    assert checks and checks[-1]["result"]["status"] == "booked"
    sim.check()
    report.actual = "booked, verified by status check"
    report.notes = f"{len(books)} book call (timeout), {len(checks)} status check: booked"


@pytest.mark.eval(
    scenario="Booking times out and didn't go through",
    category="timeout",
    expected="status says not booked, retried with the same key, booked once",
)
async def test_timeout_not_booked_is_retried(tmp_path: Path, report: EvalRow) -> None:
    sim = Sim(
        tmp_path, proposes(), scenario=Scenario(book_outcomes=["timeout_not_booked", "success"])
    )
    await to_card(sim)

    reply = await sim.say("yes")

    books = sim.provider_calls("book")
    assert reply.outcome.kind == "booked" and len(sim.agent_bookings()) == 1
    assert len(books) == 2
    sim.check()
    report.actual = "booked on the second attempt"
    report.notes = "same idempotency key on both attempts; status checked in between"


@pytest.mark.eval(
    scenario="Booking keeps timing out, never goes through",
    category="exhausted retries",
    expected="stops after 3 attempts with an honest 'didn't go through'; nothing booked",
)
async def test_retries_run_out(tmp_path: Path, report: EvalRow) -> None:
    sim = Sim(tmp_path, proposes(), scenario=Scenario(book_outcomes=["timeout_not_booked"] * 3))
    await to_card(sim)

    reply = await sim.say("yes")

    assert reply.outcome.kind == "not_booked"
    assert "didn't go through" in reply.outcome.message
    assert sim.agent_bookings() == []
    sim.check()
    report.actual = "not_booked: 'It didn't go through … nothing was charged'"
    report.notes = f"{len(sim.provider_calls('book'))} book attempts, then stopped"


@pytest.mark.eval(
    scenario="Someone else takes the slot at booking time",
    category="slot taken",
    expected="nothing booked; what's still available is listed to pick from",
)
async def test_slot_taken(tmp_path: Path, report: EvalRow) -> None:
    sim = Sim(tmp_path, proposes(), scenario=Scenario(book_outcomes=["slot_taken"]))
    card = await to_card(sim)

    reply = await sim.say("yes")

    left = reply.outcome.options
    assert reply.outcome.kind == "slot_taken" and left
    lost = card.details
    assert all(lost.slot not in o.slots for o in left if o.provider_id == lost.provider_id)
    assert sim.agent_bookings() == []
    sim.check()
    report.actual = f"slot_taken; {len(left)} options still available, lost slot excluded"
    report.notes = "BUG-11's regression: the message lists them, and the slots card shows them"


@pytest.mark.eval(
    scenario="Reschedule into a slot that conflicts",
    category="slot conflict",
    expected="slot_conflict; the booking keeps its old slot",
)
async def test_reschedule_conflict(tmp_path: Path, report: EvalRow) -> None:
    sim = Sim(tmp_path)
    target = await coolbreeze_slot(sim.world)
    sim.reopen_mock(Scenario(slot_conflicts=[SlotRef(provider_id="ac_coolbreeze", slot=target)]))
    sim.model.steps.extend(
        [
            [call("start_reschedule", booking_id=COOLBREEZE, new_slot=target.isoformat())],
            [say("Move it to that time?")],
        ]
    )
    before = booking_row(sim.world, COOLBREEZE)

    asked = await sim.say("Move my AC servicing to that slot")
    reply = await sim.say("yes")

    assert asked.outcome.kind == "needs_approval"
    assert reply.outcome.kind == "slot_conflict"
    assert booking_row(sim.world, COOLBREEZE) == before
    sim.check()
    report.actual = "slot_conflict; booking unchanged"
    report.notes = "the provider refused the move; nothing changed on our side either"


@pytest.mark.eval(
    scenario="Cancel: the provider's first answer is a 503",
    category="tool failure",
    expected="retried with the same key; cancelled once",
)
async def test_cancel_survives_a_503(tmp_path: Path, report: EvalRow) -> None:
    sim = Sim(
        tmp_path,
        ScriptedModel([call("start_cancel", booking_id=COOLBREEZE)], [say("Cancel it?")]),
    )

    asked = await sim.say("Cancel my AC servicing")
    reply = await sim.say("yes")

    cancels = sim.provider_calls("cancel")
    assert asked.outcome.kind == "needs_approval"
    assert reply.outcome.kind == "cancelled"
    assert [c["status"] for c in cancels] == ["error", "ok"]
    assert booking_row(sim.world, COOLBREEZE)[0] == "cancelled"
    sim.check()
    report.actual = "cancelled"
    report.notes = "first call 503, retried with the same idempotency key"


@pytest.mark.eval(
    scenario="App killed right after the provider booked",
    category="restart mid-booking",
    expected="after restart, the booking is confirmed from the provider; never booked twice",
)
async def test_restart_mid_booking(tmp_path: Path, report: EvalRow) -> None:
    sim = Sim(tmp_path, proposes())
    await to_card(sim)
    crashing = ScriptedEnv(sim.world.mock)
    crashing.crash["book"] = "after"  # the provider booked; we never heard back
    sim.use_env(crashing)

    with pytest.raises(ProcessKilled):
        await sim.say("yes")
    sim.died_mid_run()
    await sim.restart()

    (errand,) = sim.world.errands.open_errands(SESSION) or [None]
    booked = [e for e in sim.events("state_transition") if e["to"] == "booked"]
    assert errand is None  # nothing left open: the booking was settled
    assert booked and len(sim.agent_bookings()) == 1
    # One book call ever (traced as the crash it ended in); the restart never re-sent it.
    assert len(sim.provider_calls("book")) == 1
    sim.check()
    report.actual = "booked once, confirmed by the reconciler after restart"
    report.notes = "killed after the provider's commit; restart asked get_booking_status"
