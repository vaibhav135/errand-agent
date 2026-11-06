"""Reconciler: resolve every side effect whose outcome we don't know yet.

What: `Reconciler.reconcile_once(session_id=None)` looks at open provider actions
      (in_flight / verifying / unverified) and settles each against the provider's
      truth; `run_periodically()` repeats that in the background.

Why:  Booking correctness across process restarts and unreachable providers: a crash
      right after the commit point, a timeout whose status checks all failed, a
      cancel that kept getting 503s. None of these may be guessed; each is settled
      from the source of truth, and never by creating a *new* booking.

How:  - Runs at app startup (restart recovery), every RECONCILE_INTERVAL in the
        background, and per session at the start of a user run (wired by the agent
        loop). A pass that finds work gets its own `trigger="reconcile"` run trace;
        an empty pass leaves no trace.
      - book: get_booking_status(key). Booked -> confirm (never book again). Not
        booked -> complete the *approved* action once: book(same hold, same key);
        the provider refuses if the hold expired, so nothing new can happen.
        Status unreachable -> leave it parked for the next pass.
      - cancel (cancel_pending): retried with the same key once due, with capped
        backoff (30 s, 1, 2, 4, then 5 min); after `cancel_deadline` it stops and
        ends in cancel_failed without a pointless call.
      - reschedule: re-sent with the same key (the provider replays or performs it).
      - All calls go through GuardedToolEnv inside `committed()`: traced like any
        run, never blocked by a budget, since this is finishing started work.
"""

import logging
import sqlite3
from collections import defaultdict
from datetime import timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict

from errand_agent.core.clock import Clock
from errand_agent.providers.errors import ProviderError, ProviderTimeout, ProviderUnavailable
from errand_agent.providers.models import Booked, Cancelled, Rescheduled, StatusBooked
from errand_agent.providers.tool_env import ToolEnv
from errand_agent.store.db import atomic
from errand_agent.tracing.recorder import RunTrace, Tracer
from errand_agent.workflow.actions import ProviderAction, ProviderActionStore
from errand_agent.workflow.approvals import ApprovalStore
from errand_agent.workflow.changes import CANCEL_RETRIES, cancel_deadline
from errand_agent.workflow.errands import ErrandStore
from errand_agent.workflow.guarded_env import BudgetMeter, GuardedToolEnv, RunBudget

logger = logging.getLogger(__name__)

RECONCILE_INTERVAL_SECONDS = 30.0
CANCEL_BACKOFF_BASE = timedelta(seconds=30)
CANCEL_BACKOFF_CAP = timedelta(minutes=5)

ReconcileOutcome = Literal[
    "booked",
    "not_booked",
    "still_unknown",
    "cancelled",
    "cancel_failed",
    "still_pending",
    "rescheduled",
    "slot_conflict",
    "reschedule_failed",
]


class ReconcileResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    errand_id: str
    operation: str
    outcome: ReconcileOutcome
    booking_id: str | None = None


class Reconciler:
    def __init__(
        self,
        *,
        conn: sqlite3.Connection,
        env: ToolEnv,
        errands: ErrandStore,
        approvals: ApprovalStore,
        actions: ProviderActionStore,
        clock: Clock,
        tracer: Tracer,
    ) -> None:
        self._conn = conn
        self._env = env
        self._errands = errands
        self._approvals = approvals
        self._actions = actions
        self._clock = clock
        self._tracer = tracer

    # --- entry points --------------------------------------------------------------------

    async def run_periodically(
        self,
        interval_seconds: float = RECONCILE_INTERVAL_SECONDS,
        *,
        max_passes: int | None = None,
    ) -> None:
        passes = 0
        while max_passes is None or passes < max_passes:
            try:
                await self.reconcile_once()
            except Exception:  # keep the loop alive; the next pass retries
                logger.exception("reconcile pass failed")
            passes += 1
            if max_passes is None or passes < max_passes:
                await self._clock.sleep(interval_seconds)

    async def reconcile_once(self, *, session_id: str | None = None) -> list[ReconcileResult]:
        by_session: dict[str, list[ProviderAction]] = defaultdict(list)
        for action in self._actions.open_actions():
            errand = self._errands.get(action.errand_id)
            if session_id is not None and errand.session_id != session_id:
                continue
            if action.operation == "cancel" and not self._cancel_due(action, errand.state):
                continue
            by_session[errand.session_id].append(action)

        results: list[ReconcileResult] = []
        for session, actions in by_session.items():
            with self._tracer.start_run(session_id=session, trigger="reconcile") as run:
                meter = BudgetMeter(RunBudget(), clock=self._clock, run=run)
                env = GuardedToolEnv(self._env, run=run, meter=meter)
                session_results: list[ReconcileResult] = []
                with meter.committed():
                    for action in actions:
                        session_results.append(await self._settle(action, env, run))
                run.finish(
                    outcome="reconciled",
                    results=[r.model_dump() for r in session_results],
                )
            results.extend(session_results)
        return results

    # --- per operation -------------------------------------------------------------------

    async def _settle(self, action: ProviderAction, env: ToolEnv, run: RunTrace) -> ReconcileResult:
        if action.operation == "book":
            return await self._settle_book(action, env, run)
        if action.operation == "cancel":
            return await self._settle_cancel(action, env, run)
        return await self._settle_reschedule(action, env, run)

    async def _settle_book(
        self, action: ProviderAction, env: ToolEnv, run: RunTrace
    ) -> ReconcileResult:
        key, errand_id = action.idempotency_key, action.errand_id
        try:
            status = await env.get_booking_status(idempotency_key=key)
        except ProviderError:
            return self._result(action, "still_unknown")
        if isinstance(status, StatusBooked):
            return self._booked(action, status.booking.booking_id, run)

        # Not booked: complete the approved action once, same hold and same key.
        assert action.hold_id is not None
        self._actions.count_attempt(key)
        try:
            result = await env.book(action.hold_id, key)
        except ProviderTimeout:
            return self._result(action, "still_unknown")  # next pass checks status again
        if isinstance(result, Booked):
            return self._booked(action, result.booking.booking_id, run)
        with atomic(self._conn):
            self._actions.set_state(key, "failed", error=result.status)
            if self._errands.get(errand_id).state == "booking":
                self._errands.transition(errand_id, "timeout", run=run, reason="process restarted")
            self._errands.transition(errand_id, "not_booked", run=run, reason=result.status)
        return self._result(action, "not_booked")

    def _booked(self, action: ProviderAction, booking_id: str, run: RunTrace) -> ReconcileResult:
        errand = self._errands.get(action.errand_id)
        event = "booked" if errand.state == "booking" else "confirmed"
        with atomic(self._conn):
            self._actions.set_state(action.idempotency_key, "succeeded", booking_id=booking_id)
            self._errands.set_booking(action.errand_id, booking_id)
            self._errands.transition(action.errand_id, event, run=run, reason="reconciled")
        return self._result(action, "booked", booking_id)

    async def _settle_cancel(
        self, action: ProviderAction, env: ToolEnv, run: RunTrace
    ) -> ReconcileResult:
        key, errand_id = action.idempotency_key, action.errand_id
        assert action.booking_id is not None
        approval = self._approvals.get(action.approval_id)
        assert approval.decided_at is not None
        state = self._errands.get(errand_id).state

        if self._clock.now() >= cancel_deadline(approval.decided_at, approval.details.slot):
            with atomic(self._conn):
                self._actions.set_state(key, "failed", error="retry window closed")
                if state == "cancelling":
                    self._errands.transition(errand_id, "retries_exhausted", run=run)
                self._errands.transition(errand_id, "window_closed", run=run)
            return self._result(action, "cancel_failed")

        self._actions.count_attempt(key)
        try:
            result = await env.cancel(action.booking_id, key)
        except ProviderUnavailable as exc:
            with atomic(self._conn):
                self._actions.set_state(key, "in_flight", error=str(exc))
                if state == "cancelling":
                    self._errands.transition(errand_id, "retries_exhausted", run=run)
            return self._result(action, "still_pending")
        if isinstance(result, Cancelled):
            with atomic(self._conn):
                self._actions.set_state(key, "succeeded")
                self._errands.transition(errand_id, "cancelled", run=run, reason="reconciled")
            return self._result(action, "cancelled", action.booking_id)
        with atomic(self._conn):
            self._actions.set_state(key, "failed", error=result.status)
            self._errands.transition(errand_id, "not_cancellable", run=run, reason=result.status)
        return self._result(action, "cancel_failed")

    async def _settle_reschedule(
        self, action: ProviderAction, env: ToolEnv, run: RunTrace
    ) -> ReconcileResult:
        key, errand_id = action.idempotency_key, action.errand_id
        assert action.booking_id is not None
        slot = self._approvals.get(action.approval_id).details.slot
        self._actions.count_attempt(key)
        try:
            result = await env.reschedule(action.booking_id, slot, key)
        except ProviderError:
            return self._result(action, "still_unknown")
        if isinstance(result, Rescheduled):
            with atomic(self._conn):
                self._actions.set_state(key, "succeeded")
                self._errands.transition(errand_id, "rescheduled", run=run, reason="reconciled")
            return self._result(action, "rescheduled", action.booking_id)
        event = "slot_conflict" if result.status == "slot_conflict" else "not_reschedulable"
        with atomic(self._conn):
            self._actions.set_state(key, "failed", error=result.status)
            self._errands.transition(errand_id, event, run=run, reason=result.status)
        return self._result(
            action, "slot_conflict" if event == "slot_conflict" else "reschedule_failed"
        )

    # --- helpers -----------------------------------------------------------------------------

    def _cancel_due(self, action: ProviderAction, errand_state: str) -> bool:
        """Crashed mid-cancel: retry now. Pending after 503s: capped exponential backoff."""
        if errand_state != "cancel_pending":
            return True
        background_tries = max(0, action.attempts - (CANCEL_RETRIES + 1))
        delay = min(CANCEL_BACKOFF_BASE * (2**background_tries), CANCEL_BACKOFF_CAP)
        approval = self._approvals.get(action.approval_id)
        assert approval.decided_at is not None
        past_deadline = self._clock.now() >= cancel_deadline(
            approval.decided_at, approval.details.slot
        )
        return past_deadline or self._clock.now() >= action.updated_at + delay

    def _result(
        self, action: ProviderAction, outcome: ReconcileOutcome, booking_id: str | None = None
    ) -> ReconcileResult:
        return ReconcileResult(
            errand_id=action.errand_id,
            operation=action.operation,
            outcome=outcome,
            booking_id=booking_id,
        )
