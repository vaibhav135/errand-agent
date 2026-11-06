"""The invariant checks themselves: each must catch the violation it exists for.

Without this, a check that never fires would make every scenario look green.
"""

from pathlib import Path

from errand_agent.providers.models import Held, SearchOk
from errand_agent.workflow.guarded_env import RunBudget
from tests.evals.harness import SESSION, USER, Sim
from tests.unit.workflow_world import WEEK


async def test_a_clean_world_has_no_violations(tmp_path: Path) -> None:
    assert Sim(tmp_path).violations() == []


async def test_a_booking_nobody_approved_is_caught(tmp_path: Path) -> None:
    sim = Sim(tmp_path)
    search = await sim.world.mock.search_providers("plumber", "Indiranagar", WEEK)
    assert isinstance(search, SearchOk)
    p = search.providers[0]
    held = await sim.world.mock.hold_slot(p.provider_id, p.available_slots[0])
    assert isinstance(held, Held)
    await sim.world.mock.book(held.hold.hold_id, "idem_behind_our_back")  # no approval

    assert any("no approval behind it" in v for v in sim.violations())


async def test_an_unfinished_run_is_caught(tmp_path: Path) -> None:
    sim = Sim(tmp_path)
    sim.tracer.start_run(session_id=SESSION, trigger="user_message")  # never finished

    assert any("never finished" in v for v in sim.violations())


async def test_a_reply_that_was_never_saved_is_caught(tmp_path: Path) -> None:
    sim = Sim(tmp_path)
    sim.tracer.start_run(session_id=SESSION, trigger="user_message").finish(outcome="answered")

    assert any("saved no reply" in v for v in sim.violations())


async def test_context_over_budget_is_caught(tmp_path: Path) -> None:
    sim = Sim(tmp_path)
    with sim.tracer.start_run(session_id=SESSION, trigger="test_context") as run:
        run.emit("context_sent", {"tokens": {"total": 9000}, "budget": {"total": 7500}})

    assert any("9000 > 7500" in v for v in sim.violations())


async def test_a_forgotten_memory_in_context_is_caught(tmp_path: Path) -> None:
    sim = Sim(tmp_path)
    memories = sim.handler._memories  # pyright: ignore[reportPrivateUsage]
    m = memories.remember(
        USER, "earliest_start", "10:00", quote="10 am", user_message="after 10 am"
    )
    sim.world.clock.advance(60)
    memories.forget(m.memory_id)
    sim.world.clock.advance(60)
    with sim.tracer.start_run(session_id=SESSION, trigger="test_context") as run:
        run.emit(
            "context_sent",
            {"tokens": {"total": 10}, "budget": {"total": 7500}, "memory_ids": [m.memory_id]},
        )

    assert any("after it was forgotten" in v for v in sim.violations())


async def test_a_forget_later_in_the_same_turn_is_not_flagged(tmp_path: Path) -> None:
    # Within one turn the context is built first; a forget may follow at the same instant.
    sim = Sim(tmp_path)
    memories = sim.handler._memories  # pyright: ignore[reportPrivateUsage]
    m = memories.remember(
        USER, "earliest_start", "10:00", quote="10 am", user_message="after 10 am"
    )
    sim.world.clock.advance(60)
    with sim.tracer.start_run(session_id=SESSION, trigger="test_context") as run:
        run.emit(
            "context_sent",
            {"tokens": {"total": 10}, "budget": {"total": 7500}, "memory_ids": [m.memory_id]},
        )
        memories.forget(m.memory_id)  # same FakeClock instant as the context

    assert not any("forgotten" in v for v in sim.violations())


async def test_a_run_past_its_limits_is_caught(tmp_path: Path) -> None:
    sim = Sim(tmp_path, budget=RunBudget(max_model_turns=1))
    run = sim.tracer.start_run(session_id=SESSION, trigger="test_limits")
    run._model_calls = 2  # pyright: ignore[reportPrivateUsage]  (as if two calls were made)
    run.finish(outcome="answered")

    assert any("past its limits" in v for v in sim.violations())
