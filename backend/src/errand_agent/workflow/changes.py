"""Change flow: cancel or reschedule an existing booking, with the same guarantees.

What: `ChangeFlow.propose_cancel / propose_reschedule` (ask the user to approve the
      exact change) and `execute_cancel / execute_reschedule` (perform it once).
      `cancel_deadline()` is the end of the background retry window.

Why:  Cancelling and rescheduling are consequential actions: explicit approval of the
      exact booking and details, enforced in code; idempotent execution; honest
      outcomes when the provider is unreachable.

How:  - Booking details come from the provider's own record (`list_bookings`), both
        when proposing and again before executing. If the booking changed in between
        (e.g. the provider moved it), the fingerprint no longer matches: the approval
        is invalidated, the errand goes back to collecting and the outcome is
        `booking_changed`, so the user is asked again with current details.
      - Execution uses the shared commit point (approval consumed, provider action
        recorded under a fresh idempotency key) and finishes within `_finishing()`.
      - cancel: a 503 is retried CANCEL_RETRIES times with the same key and backoff.
        Still failing -> `cancel_pending`: the booking is **still active** (a 503 means
        nothing was done) and the reconciler keeps retrying until
        `cancel_deadline` = min(approval + 15 min, visit - 1 h). If that deadline has
        already passed, the errand ends in `cancel_failed` straight away ("contact the
        provider"). A provider refusal (`not_cancellable`, `booking_not_found`) ends in
        `cancel_failed`. An already-cancelled booking counts as cancelled.
      - reschedule: the price stays the same; one call (the contract has no transient
        failures for it). `slot_conflict` -> back to collecting to choose another
        slot; a booking no longer reschedulable -> `reschedule_failed`.
"""

from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict

from errand_agent.providers.errors import ProviderUnavailable
from errand_agent.providers.models import Booking, Cancelled, Rescheduled
from errand_agent.store.db import atomic
from errand_agent.workflow.approvals import Approval, ApprovalDetails, ApprovalError, fingerprint
from errand_agent.workflow.errands import ErrandKind, InvalidTransition
from errand_agent.workflow.flow import Flow

CANCEL_RETRIES = 3
CANCEL_BACKOFF_SECONDS = (1, 2, 4)
CANCEL_RETRY_WINDOW = timedelta(minutes=15)
CANCEL_CUTOFF_BEFORE_VISIT = timedelta(hours=1)

ChangeOutcome = Literal[
    "cancelled",
    "cancel_pending",
    "cancel_failed",
    "rescheduled",
    "slot_conflict",
    "reschedule_failed",
    "booking_changed",
]


class ChangeProposal(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["proposed", "booking_not_found", "not_changeable"]
    approval: Approval | None = None
    booking: Booking | None = None


class ChangeResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome: ChangeOutcome
    booking: Booking | None = None
    reason: str | None = None


def cancel_deadline(approved_at: datetime, visit: datetime) -> datetime:
    """When background cancel retries stop: the earlier of the retry window and an
    hour before the visit (after that, cancelling no longer helps the provider)."""
    return min(approved_at + CANCEL_RETRY_WINDOW, visit - CANCEL_CUTOFF_BEFORE_VISIT)


class ChangeFlow(Flow):
    # --- proposing ---------------------------------------------------------------------

    async def propose_cancel(self, errand_id: str) -> ChangeProposal:
        return await self._propose(errand_id, "cancel", new_slot=None)

    async def propose_reschedule(self, errand_id: str, new_slot: datetime) -> ChangeProposal:
        return await self._propose(errand_id, "reschedule", new_slot=new_slot)

    async def _propose(
        self, errand_id: str, kind: ErrandKind, *, new_slot: datetime | None
    ) -> ChangeProposal:
        errand = self._errands.get(errand_id)
        if errand.kind != kind or errand.state != "collecting" or errand.booking_id is None:
            raise InvalidTransition(
                f"can only propose a {kind} for a {kind} errand in 'collecting' with a booking"
                f" (got {errand.kind} in '{errand.state}')"
            )
        booking = await self._current_booking(errand_id, errand.booking_id)
        if booking is None:
            return ChangeProposal(status="booking_not_found")
        if booking.status != "confirmed" or booking.slot <= self._clock.now():
            return ChangeProposal(status="not_changeable", booking=booking)

        details = _details(kind, booking, new_slot)
        with atomic(self._conn):
            approval = self._approvals.request(errand_id, details, run=self._run)
            self._errands.transition(
                errand_id, "option_proposed", run=self._run, reason=f"{kind} {booking.booking_id}"
            )
        return ChangeProposal(status="proposed", approval=approval, booking=booking)

    # --- executing ---------------------------------------------------------------------

    async def execute_cancel(self, errand_id: str, approval_id: str) -> ChangeResult:
        approval, booking = await self._ready(errand_id, approval_id, "cancel")
        if isinstance(approval, ChangeResult):
            return approval
        booking_id = approval.details.booking_id
        assert booking_id is not None
        key = self._commit_point(
            errand_id=errand_id,
            approval_id=approval_id,
            details=approval.details,
            operation="cancel",
            booking_id=booking_id,
        )
        with self._finishing():
            for attempt in range(CANCEL_RETRIES + 1):
                self._actions.count_attempt(key)
                try:
                    result = await self._env.cancel(booking_id, key)
                except ProviderUnavailable as exc:
                    self._actions.set_state(key, "in_flight", error=str(exc))
                    if attempt < CANCEL_RETRIES:
                        await self._clock.sleep(CANCEL_BACKOFF_SECONDS[attempt])
                    continue
                if isinstance(result, Cancelled):
                    with atomic(self._conn):
                        self._actions.set_state(key, "succeeded")
                        self._errands.transition(errand_id, "cancelled", run=self._run)
                    return ChangeResult(outcome="cancelled", booking=result.booking)
                if result.status == "idempotency_key_reused":
                    raise RuntimeError(f"cancel refused key {key}: workflow bug")
                with atomic(self._conn):
                    self._actions.set_state(key, "failed", error=result.status)
                    self._errands.transition(
                        errand_id, "not_cancellable", run=self._run, reason=result.status
                    )
                return ChangeResult(outcome="cancel_failed", reason=result.status)

        # Every attempt was a 503: nothing was done, the booking is still active.
        self._errands.transition(
            errand_id, "retries_exhausted", run=self._run, reason="provider unavailable"
        )
        assert approval.decided_at is not None and booking is not None
        if self._clock.now() >= cancel_deadline(approval.decided_at, booking.slot):
            with atomic(self._conn):
                self._actions.set_state(key, "failed", error="retry window closed")
                self._errands.transition(
                    errand_id, "window_closed", run=self._run, reason="visit under an hour away"
                )
            return ChangeResult(outcome="cancel_failed", booking=booking, reason="window_closed")
        return ChangeResult(outcome="cancel_pending", booking=booking)

    async def execute_reschedule(self, errand_id: str, approval_id: str) -> ChangeResult:
        approval, _ = await self._ready(errand_id, approval_id, "reschedule")
        if isinstance(approval, ChangeResult):
            return approval
        details = approval.details
        assert details.booking_id is not None
        key = self._commit_point(
            errand_id=errand_id,
            approval_id=approval_id,
            details=details,
            operation="reschedule",
            booking_id=details.booking_id,
        )
        with self._finishing():
            self._actions.count_attempt(key)
            result = await self._env.reschedule(details.booking_id, details.slot, key)
        if isinstance(result, Rescheduled):
            with atomic(self._conn):
                self._actions.set_state(key, "succeeded")
                self._errands.transition(errand_id, "rescheduled", run=self._run)
            return ChangeResult(outcome="rescheduled", booking=result.booking)
        if result.status == "idempotency_key_reused":
            raise RuntimeError(f"reschedule refused key {key}: workflow bug")
        event = "slot_conflict" if result.status == "slot_conflict" else "not_reschedulable"
        with atomic(self._conn):
            self._actions.set_state(key, "failed", error=result.status)
            self._errands.transition(errand_id, event, run=self._run, reason=result.status)
        outcome: ChangeOutcome = (
            "slot_conflict" if event == "slot_conflict" else "reschedule_failed"
        )
        return ChangeResult(outcome=outcome, reason=result.status)

    # --- helpers -----------------------------------------------------------------------

    async def _current_booking(self, errand_id: str, booking_id: str) -> Booking | None:
        bookings = await self._env.list_bookings(self._errands.user_of(errand_id))
        return next((b for b in bookings if b.booking_id == booking_id), None)

    async def _ready(
        self, errand_id: str, approval_id: str, kind: ErrandKind
    ) -> tuple[Approval | ChangeResult, Booking | None]:
        """Check preconditions; detect a booking that changed since it was approved."""
        errand = self._errands.get(errand_id)
        approval = self._approvals.get(approval_id)
        if errand.kind != kind or errand.state != "awaiting_approval":
            raise InvalidTransition(
                f"can only execute a {kind} errand in 'awaiting_approval' (got {errand.kind}"
                f" in '{errand.state}')"
            )
        if approval.errand_id != errand_id:
            raise ApprovalError(f"approval {approval_id} belongs to another errand")
        if approval.status != "granted":
            raise ApprovalError(f"approval {approval_id} is {approval.status}, not granted")

        assert errand.booking_id is not None
        booking = await self._current_booking(errand_id, errand.booking_id)
        if booking is not None:
            current = _details(kind, booking, approval.details.slot)
            if fingerprint(current) != approval.fingerprint:
                with atomic(self._conn):
                    self._approvals.invalidate_live(errand_id, reason="booking changed")
                    self._errands.transition(
                        errand_id, "approval_rejected", run=self._run, reason="booking changed"
                    )
                return ChangeResult(outcome="booking_changed", booking=booking), booking
        return approval, booking


def _details(kind: ErrandKind, booking: Booking, new_slot: datetime | None) -> ApprovalDetails:
    """The material details of a change, from the provider's record of the booking."""
    return ApprovalDetails(
        action=kind,
        provider_id=booking.provider_id,
        provider_name=booking.provider_name,
        slot=new_slot if kind == "reschedule" and new_slot is not None else booking.slot,
        price_inr=booking.price_inr,
        booking_id=booking.booking_id,
    )
