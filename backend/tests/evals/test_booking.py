"""Eval scenarios: booking, end to end (happy path and booking correctness)."""

from pathlib import Path

import pytest

from tests.evals.conftest import EvalRow
from tests.evals.harness import Sim
from tests.unit.agent_script import ScriptedModel, call, say
from tests.unit.test_agent_tools import FULL

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
