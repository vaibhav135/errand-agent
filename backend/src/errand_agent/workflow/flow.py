"""Flow base: what every side-effect flow (book, cancel, reschedule) shares.

What: `Flow`, the base of `BookingFlow` and `ChangeFlow`: their dependencies, the
      commit point and the "finish what was started" budget scope.

Why:  The commit point is the most safety-critical step in the workflow: consume the
      approval, record the provider action under a fresh idempotency key and move the
      errand to its in-flight state, all-or-nothing. Written once, it can't drift
      between booking, cancelling and rescheduling.

How:  - `_commit_point()` first checks the budget has room for the side effect
        (`ensure_room`, so a run that can't finish stops *before* consuming the
        approval), then does the three writes in one `atomic` unit and returns the key.
      - `_finishing()` is the scope after the commit point: calls are counted but never
        blocked by the budget, because finishing a started side effect (retries,
        status checks) beats stopping half-way.
"""

import contextlib
import sqlite3
from contextlib import AbstractContextManager

from errand_agent.core.clock import Clock
from errand_agent.core.ids import IdGen
from errand_agent.providers.tool_env import ToolEnv
from errand_agent.store.db import atomic
from errand_agent.tracing.recorder import RunTrace
from errand_agent.workflow.actions import Operation, ProviderActionStore
from errand_agent.workflow.approvals import ApprovalDetails, ApprovalStore
from errand_agent.workflow.errands import ErrandStore
from errand_agent.workflow.guarded_env import BudgetMeter


class Flow:
    def __init__(
        self,
        *,
        conn: sqlite3.Connection,
        env: ToolEnv,
        errands: ErrandStore,
        approvals: ApprovalStore,
        actions: ProviderActionStore,
        clock: Clock,
        ids: IdGen,
        run: RunTrace | None = None,
        meter: BudgetMeter | None = None,
    ) -> None:
        self._conn = conn
        self._env = env
        self._errands = errands
        self._approvals = approvals
        self._actions = actions
        self._clock = clock
        self._ids = ids
        self._run = run
        self._meter = meter

    def _commit_point(
        self,
        *,
        errand_id: str,
        approval_id: str,
        details: ApprovalDetails,
        operation: Operation,
        hold_id: str | None = None,
        booking_id: str | None = None,
    ) -> str:
        if self._meter is not None:
            self._meter.ensure_room(operation)
        key = self._ids.new("idem")
        with atomic(self._conn):
            self._approvals.consume(approval_id, details, run=self._run)
            self._actions.create(
                key,
                errand_id=errand_id,
                approval_id=approval_id,
                operation=operation,
                hold_id=hold_id,
                booking_id=booking_id,
            )
            self._errands.transition(errand_id, "approved", run=self._run, reason=key)
        return key

    def _finishing(self) -> AbstractContextManager[None]:
        return self._meter.committed() if self._meter else contextlib.nullcontext()
