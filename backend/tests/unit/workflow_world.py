"""Shared test helpers for workflow tests: agent stores + mock world on one FakeClock."""

import sqlite3
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from errand_agent.core.clock import FakeClock
from errand_agent.core.ids import SequentialIdGen
from errand_agent.mocks.scenario import Scenario
from errand_agent.mocks.world import MockWorld
from errand_agent.providers.models import DateRange, SearchOk
from errand_agent.store.db import connect
from errand_agent.store.migrate import migrate, migrations_for
from errand_agent.tracing.recorder import RunTrace, TraceEvent, Tracer
from errand_agent.workflow.actions import ProviderActionStore
from errand_agent.workflow.approvals import ApprovalStore
from errand_agent.workflow.booking import BookingFlow
from errand_agent.workflow.changes import ChangeFlow
from errand_agent.workflow.errands import ErrandStore
from errand_agent.workflow.guarded_env import BudgetMeter
from errand_agent.workflow.ranking import Option, rank_options
from errand_agent.workflow.requirements import Requirements

NOW = datetime(2026, 11, 5, 3, 30, tzinfo=UTC)  # Thursday 09:00 IST
WEEK = DateRange(start=date(2026, 11, 5), end=date(2026, 11, 11))
REQS = Requirements(service="plumber", area="Indiranagar", date_range=WEEK, budget_max_inr=1500)


class World:
    """Agent-side stores and a mock world sharing one FakeClock and id generator."""

    def __init__(self, tmp_path: Path, scenario: Scenario | None = None) -> None:
        self.clock = FakeClock(NOW)
        self.ids = SequentialIdGen()
        self.conn: sqlite3.Connection = connect(tmp_path / "agent.db")
        migrate(self.conn, migrations_for("agent"), self.clock)
        self.mock_path = tmp_path / "mock_world.db"
        self.mock = self.open_mock(scenario or Scenario())
        self.errands = ErrandStore(self.conn, clock=self.clock, ids=self.ids)
        self.approvals = ApprovalStore(self.conn, clock=self.clock, ids=self.ids)
        self.actions = ProviderActionStore(self.conn, clock=self.clock)
        self.events: list[TraceEvent] = []
        self.run: RunTrace = Tracer(
            self.clock, self.ids, subscribers=[self.events.append]
        ).start_run(session_id="sess_1", trigger="test")
        self.errands.create_session("sess_1", user_id="user_1")

    def open_mock(self, scenario: Scenario, user_id: str = "user_1") -> MockWorld:
        return MockWorld.open(
            self.mock_path, clock=self.clock, ids=self.ids, scenario=scenario, user_id=user_id
        )

    def flow(self, env: Any = None, meter: BudgetMeter | None = None) -> BookingFlow:
        return BookingFlow(
            conn=self.conn,
            env=env or self.mock,
            errands=self.errands,
            approvals=self.approvals,
            actions=self.actions,
            clock=self.clock,
            ids=self.ids,
            run=self.run,
            meter=meter,
        )

    def change_flow(self, env: Any = None, meter: BudgetMeter | None = None) -> ChangeFlow:
        return ChangeFlow(
            conn=self.conn,
            env=env or self.mock,
            errands=self.errands,
            approvals=self.approvals,
            actions=self.actions,
            clock=self.clock,
            ids=self.ids,
            run=self.run,
            meter=meter,
        )

    def mock_exec(self, sql: str, *params: object) -> None:
        connect(self.mock_path).execute(sql, params)

    def mock_count(self, sql: str) -> int:
        return connect(self.mock_path).execute(sql).fetchone()[0]

    async def comparing_errand(self, provider_id: str = "plm_quickfix") -> tuple[str, Option]:
        errand = self.errands.create("sess_1", "book")
        self.errands.set_requirements(errand.errand_id, REQS)
        search = await self.mock.search_providers("plumber", "Indiranagar", WEEK)
        assert isinstance(search, SearchOk)
        option = next(
            o for o in rank_options(search.providers, REQS).options if o.provider_id == provider_id
        )
        self.errands.transition(errand.errand_id, "options_ready", run=self.run)
        return errand.errand_id, option

    async def approved_errand(self, env: Any = None) -> tuple[str, str]:
        """A book errand with a granted approval for QuickFix's first slot."""
        errand_id, option = await self.comparing_errand()
        proposal = await self.flow(env).propose(errand_id, option, option.slots[0])
        assert proposal.approval is not None
        a = proposal.approval
        self.approvals.grant(a.approval_id, presented_fingerprint=a.fingerprint, via="button")
        return errand_id, a.approval_id


class ScriptedEnv:
    """Wraps a ToolEnv; lets a test inject behaviour the mock never produces itself."""

    def __init__(self, inner: MockWorld) -> None:
        self.inner = inner
        self.before_book: Callable[[], None] | None = None
        self.status_error: Exception | None = None
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Callable[..., Awaitable[Any]]:
        target = getattr(self.inner, name)

        async def call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, kwargs or {"args": args}))
            if name == "book" and self.before_book is not None:
                self.before_book()
            if name == "get_booking_status" and self.status_error is not None:
                raise self.status_error
            return await target(*args, **kwargs)

        return call
