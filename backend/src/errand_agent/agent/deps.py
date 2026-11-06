"""Agent dependencies: everything one run's tools may touch, and nothing more.

What: `AgentDeps`, passed to every tool as `ctx.deps`.

Why:  Tools are thin adapters from the model to the workflow. Giving them one explicit
      bundle (stores, the *guarded* provider env, the run trace and budget meter, who the
      user is and what they just said) keeps them testable and keeps the model's reach
      exactly as wide as this object.

How:  - `env` is the GuardedToolEnv for this run: every provider call is traced and
        budgeted, and only the workflow flows built here can cause side effects.
      - `user_message` is the user's current message: the provenance that `remember`
        and `update_profile` are checked against.
      - Tools record approval cards they produced in `cards`; the message handler
        shows those (code-rendered) alongside the model's reply.
"""

import sqlite3
from dataclasses import dataclass, field

from errand_agent.core.clock import Clock
from errand_agent.core.ids import IdGen
from errand_agent.memory.profile import ProfileStore
from errand_agent.memory.store import MemoryStore
from errand_agent.providers.tool_env import ToolEnv
from errand_agent.tracing.recorder import RunTrace
from errand_agent.workflow.actions import ProviderActionStore
from errand_agent.workflow.approvals import Approval, ApprovalStore
from errand_agent.workflow.booking import BookingFlow
from errand_agent.workflow.changes import ChangeFlow
from errand_agent.workflow.errands import ErrandStore
from errand_agent.workflow.guarded_env import BudgetMeter


@dataclass
class AgentDeps:
    conn: sqlite3.Connection
    clock: Clock
    ids: IdGen
    env: ToolEnv
    errands: ErrandStore
    approvals: ApprovalStore
    actions: ProviderActionStore
    memories: MemoryStore
    profiles: ProfileStore
    user_id: str
    session_id: str
    user_message: str
    run: RunTrace | None = None
    meter: BudgetMeter | None = None
    errand_id: str | None = None
    cards: list[Approval] = field(default_factory=list)

    def _flow_kwargs(self) -> dict[str, object]:
        return {
            "conn": self.conn,
            "env": self.env,
            "errands": self.errands,
            "approvals": self.approvals,
            "actions": self.actions,
            "clock": self.clock,
            "ids": self.ids,
            "run": self.run,
            "meter": self.meter,
        }

    def booking_flow(self) -> BookingFlow:
        return BookingFlow(**self._flow_kwargs())  # type: ignore[arg-type]

    def change_flow(self) -> ChangeFlow:
        return ChangeFlow(**self._flow_kwargs())  # type: ignore[arg-type]
