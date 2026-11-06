from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from errand_agent.core.clock import FakeClock
from errand_agent.core.config import Settings
from errand_agent.core.ids import SequentialIdGen
from errand_agent.mocks.scenario import Scenario
from errand_agent.mocks.world import MockWorld
from errand_agent.providers.errors import ProviderTimeout
from errand_agent.providers.models import DateRange, Held, SearchOk
from errand_agent.providers.tool_env import ToolEnv
from errand_agent.store.db import connect
from errand_agent.tracing.recorder import RunTrace, TraceEvent, Tracer
from errand_agent.workflow.guarded_env import BudgetMeter, GuardedToolEnv, LimitExceeded, RunBudget

NOW = datetime(2026, 11, 5, 3, 30, tzinfo=UTC)
WEEK = DateRange(start=date(2026, 11, 5), end=date(2026, 11, 11))


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(NOW)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "mock_world.db"


@pytest.fixture
def events() -> list[TraceEvent]:
    return []


@pytest.fixture
def run(clock: FakeClock, events: list[TraceEvent]) -> RunTrace:
    tracer = Tracer(clock, SequentialIdGen(), subscribers=[events.append])
    return tracer.start_run(session_id="sess_1", trigger="test")


def guarded(
    db: Path, clock: FakeClock, run: RunTrace, budget: RunBudget | None = None, **scenario: object
) -> GuardedToolEnv:
    world = MockWorld.open(
        db, clock=clock, ids=SequentialIdGen(), scenario=Scenario.model_validate(scenario)
    )
    meter = BudgetMeter(budget or RunBudget(), clock=clock, run=run)
    return GuardedToolEnv(world, run=run, meter=meter)


def tool_events(events: list[TraceEvent]) -> list[dict[str, object]]:
    return [e.to_dict() for e in events if e.type == "tool_call"]


async def held_quickfix(env: GuardedToolEnv) -> Held:
    search = await env.search_providers("plumber", "Indiranagar", WEEK)
    assert isinstance(search, SearchOk)
    slot = next(p for p in search.providers if p.provider_id == "plm_quickfix").available_slots[0]
    held = await env.hold_slot("plm_quickfix", slot)
    assert isinstance(held, Held)
    return held


# --- tracing ------------------------------------------------------------------------------


async def test_every_call_is_traced_with_args_result_and_latency(
    db: Path, clock: FakeClock, run: RunTrace, events: list[TraceEvent]
) -> None:
    env = guarded(db, clock, run)

    result = await env.search_providers("plumber", "Indiranagar", WEEK)

    assert isinstance(result, SearchOk)  # the inner result is returned unchanged
    [event] = tool_events(events)
    assert event["tool"] == "search_providers"
    assert event["args"] == {
        "service": "plumber",
        "area": "Indiranagar",
        "date_range": {"start": "2026-11-05", "end": "2026-11-11"},
    }
    assert event["status"] == "ok"
    assert event["result"]["status"] == "ok"  # type: ignore[index]
    assert "latency_ms" in event


async def test_provider_failures_are_traced_and_re_raised(
    db: Path, clock: FakeClock, run: RunTrace, events: list[TraceEvent]
) -> None:
    env = guarded(db, clock, run, book_outcomes=["timeout_booked"])
    held = await held_quickfix(env)

    with pytest.raises(ProviderTimeout):
        await env.book(held.hold.hold_id, "idem_1")

    book_event = tool_events(events)[-1]
    assert book_event["tool"] == "book"
    assert book_event["status"] == "error"
    assert book_event["error"]["type"] == "ProviderTimeout"  # type: ignore[index]


async def test_untrusted_review_text_is_truncated_in_traces(
    db: Path, clock: FakeClock, run: RunTrace, events: list[TraceEvent]
) -> None:
    env = guarded(db, clock, run)

    await env.get_provider("plm_quickfix")

    payload = str(tool_events(events)[-1]["result"])
    assert "SYSTEM NOTICE" in payload  # data is kept for debugging, as data
    assert len(payload) < 4000  # but long free text is capped by the sanitizer


def test_guarded_env_satisfies_tool_env(db: Path, clock: FakeClock, run: RunTrace) -> None:
    env: ToolEnv = guarded(db, clock, run)  # checked statically by pyright
    assert env is not None


# --- limits -------------------------------------------------------------------------------


async def test_tool_call_limit_stops_before_the_call(
    db: Path, clock: FakeClock, run: RunTrace, events: list[TraceEvent]
) -> None:
    env = guarded(db, clock, run, RunBudget(max_tool_calls=2))
    search = await env.search_providers("plumber", "Indiranagar", WEEK)
    assert isinstance(search, SearchOk)
    slot = search.providers[0].available_slots[0]
    await env.get_provider(search.providers[0].provider_id)

    with pytest.raises(LimitExceeded) as exc:
        await env.hold_slot(search.providers[0].provider_id, slot)

    assert exc.value.limit == "tool_calls"
    assert connect(db).execute("SELECT count(*) FROM holds").fetchone()[0] == 0  # never ran
    hit = [e.to_dict() for e in events if e.type == "limit_hit"]
    assert hit == [
        {**hit[0], "limit": "tool_calls", "used": 2, "allowed": 2, "blocked": "hold_slot"}
    ]


async def test_wall_clock_limit(db: Path, clock: FakeClock, run: RunTrace) -> None:
    env = guarded(db, clock, run, RunBudget(max_seconds=60))
    clock.advance(61)

    with pytest.raises(LimitExceeded) as exc:
        await env.list_bookings("user_1")

    assert exc.value.limit == "wall_clock"


async def test_status_checks_are_never_blocked_by_the_budget(
    db: Path, clock: FakeClock, run: RunTrace, events: list[TraceEvent]
) -> None:
    # After a side effect, finding out what happened must always be possible.
    env = guarded(db, clock, run, RunBudget(max_tool_calls=2), book_outcomes=["timeout_booked"])
    held = await held_quickfix(env)  # 2 calls: budget exhausted
    clock.advance(120)  # and wall clock exhausted

    status = await env.get_booking_status(hold_id=held.hold.hold_id)

    assert status.status == "not_booked"
    assert tool_events(events)[-1]["budget_exempt"] is True


def test_model_turn_limit(clock: FakeClock, run: RunTrace) -> None:
    meter = BudgetMeter(RunBudget(max_model_turns=2), clock=clock, run=run)
    meter.charge_model_turn()
    meter.charge_model_turn()

    with pytest.raises(LimitExceeded) as exc:
        meter.charge_model_turn()

    assert exc.value.limit == "model_turns"


def test_budget_comes_from_settings() -> None:
    s = Settings(_env_file=None, max_model_turns=5, max_tool_calls=9, max_run_seconds=30)  # type: ignore[call-arg]

    budget = RunBudget.from_settings(s)

    assert (budget.max_model_turns, budget.max_tool_calls, budget.max_seconds) == (5, 9, 30)


def test_default_budget_matches_settings_defaults() -> None:
    assert RunBudget.from_settings(Settings(_env_file=None)) == RunBudget()  # type: ignore[call-arg]
