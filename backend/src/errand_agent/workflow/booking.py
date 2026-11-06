"""Booking flow: propose an option, then execute the approved booking.

What: `BookingFlow.propose(errand_id, option, slot)` holds the slot with the
      provider and asks the user to approve the exact provider, slot and quoted
      price. `BookingFlow.execute(errand_id, approval_id)` makes the approved booking
      exactly once, or reports precisely why it couldn't.

Why:  The model never books. It can only *propose*; the workflow turns a proposal
      into a hold plus a pending approval, and only the user's approval (checked
      in code) can lead to a booking.

How:  - Preconditions (book errand in `comparing`) are checked before calling the
        provider, so a wrong-state call places no hold.
      - The hold is placed first (an external call), then the errand's hold, the
        approval request and the `option_proposed` transition are saved in one
        `atomic` unit of work. If those writes failed, the provider-side hold would
        simply expire after 10 minutes; nothing else needs undoing.
      - The approval carries the *quoted* price. When the provider quotes a
        different price than the search showed (drift), `price_changed_from` lets
        the approval card say so: the user approves the real price, knowingly.
      - A slot that is no longer available returns status "unavailable" and leaves
        the errand in `comparing` with no approval.

      execute():
      1. Hold freshness: a hold within HOLD_REFRESH_MARGIN of expiry is re-held. Same
         price: continue (the fingerprint excludes hold_id). New price: a fresh
         approval for the new price (needs_reapproval). Slot gone: back to
         comparing (slot_gone).
      2. Commit point, one atomic unit: consume the approval (details must still
         match), record the provider action under a new idempotency key, transition
         to `booking`. The budget is checked *before* this point; after it, calls
         are exempt, because finishing a started side effect beats stopping half-way.
      3. book() with the same key, at most MAX_BOOK_ATTEMPTS times:
         Booked -> booked; slot_taken -> comparing; hold_expired -> re-hold and ask
         again; ProviderTimeout -> verifying: get_booking_status(key) up to
         STATUS_CHECKS times (backoff between failed checks). Booked -> booked
         (never re-book); not booked -> retry with the same key while attempts
         remain, else not_booked -> comparing; checks all failed -> unverified
         (parked for the reconciler). Success is never claimed until verified.
"""

from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict

from errand_agent.providers.errors import ProviderError, ProviderTimeout
from errand_agent.providers.models import (
    Booked,
    BookFailed,
    Booking,
    Hold,
    HoldFailed,
    StatusBooked,
)
from errand_agent.store.db import atomic
from errand_agent.workflow.approvals import Approval, ApprovalDetails, ApprovalError
from errand_agent.workflow.errands import InvalidTransition
from errand_agent.workflow.flow import Flow
from errand_agent.workflow.ranking import Option


class Proposal(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["proposed", "unavailable"]
    approval: Approval | None = None
    hold: Hold | None = None
    price_changed_from: int | None = None  # the search price, when the quote differs
    reason: str | None = None


HOLD_REFRESH_MARGIN = timedelta(seconds=30)
MAX_BOOK_ATTEMPTS = 3
STATUS_CHECKS = 3
STATUS_BACKOFF_SECONDS = (1, 2, 4)

ExecutionOutcome = Literal[
    "booked", "needs_reapproval", "slot_gone", "slot_taken", "not_booked", "unverified"
]


class Execution(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome: ExecutionOutcome
    booking: Booking | None = None
    approval: Approval | None = None  # the fresh approval, when the user must re-confirm


class BookingFlow(Flow):
    async def propose(self, errand_id: str, option: Option, slot: datetime) -> Proposal:
        errand = self._errands.get(errand_id)
        if errand.kind != "book" or errand.state != "comparing":
            raise InvalidTransition(
                f"can only propose for a book errand in 'comparing' (got {errand.kind} in"
                f" '{errand.state}')"
            )

        held = await self._env.hold_slot(option.provider_id, slot)
        if isinstance(held, HoldFailed):
            return Proposal(status="unavailable", reason=held.status)
        hold = held.hold

        details = ApprovalDetails(
            action="book",
            provider_id=option.provider_id,
            provider_name=option.name,
            slot=hold.slot,
            price_inr=hold.quoted_price_inr,
        )
        with atomic(self._conn):
            self._errands.set_hold(errand_id, hold.hold_id, hold.expires_at)
            approval = self._approvals.request(errand_id, details, run=self._run)
            self._errands.transition(
                errand_id,
                "option_proposed",
                run=self._run,
                reason=f"{option.name}, {hold.slot.isoformat()}, ₹{hold.quoted_price_inr}",
            )
        return Proposal(
            status="proposed",
            approval=approval,
            hold=hold,
            price_changed_from=(
                option.price_inr if hold.quoted_price_inr != option.price_inr else None
            ),
        )

    # --- execute ----------------------------------------------------------------------

    async def execute(self, errand_id: str, approval_id: str) -> Execution:
        errand = self._errands.get(errand_id)
        approval = self._approvals.get(approval_id)
        if errand.kind != "book" or errand.state != "awaiting_approval":
            raise InvalidTransition(
                f"can only execute a book errand in 'awaiting_approval' (got {errand.kind} in"
                f" '{errand.state}')"
            )
        if approval.errand_id != errand_id:
            raise ApprovalError(f"approval {approval_id} belongs to another errand")
        if approval.status != "granted":
            raise ApprovalError(f"approval {approval_id} is {approval.status}, not granted")
        details = approval.details

        # 1. Hold freshness (before the commit point: normal budget rules apply).
        assert errand.hold_id is not None and errand.hold_expires_at is not None
        hold_id = errand.hold_id
        if errand.hold_expires_at - self._clock.now() <= HOLD_REFRESH_MARGIN:
            refreshed = await self._refresh_hold(errand_id, details)
            if isinstance(refreshed, Execution):
                return refreshed
            hold_id = refreshed

        # 2. Commit point.
        key = self._commit_point(
            errand_id=errand_id,
            approval_id=approval_id,
            details=details,
            operation="book",
            hold_id=hold_id,
        )

        # 3. Finish what was started.
        with self._finishing():
            return await self._book_until_known(errand_id, key, hold_id, details)

    async def _refresh_hold(self, errand_id: str, details: ApprovalDetails) -> str | Execution:
        held = await self._env.hold_slot(details.provider_id, details.slot)
        if isinstance(held, HoldFailed):
            with atomic(self._conn):
                self._approvals.invalidate_live(errand_id, reason="slot no longer available")
                self._errands.transition(
                    errand_id, "option_unavailable", run=self._run, reason=held.status
                )
            return Execution(outcome="slot_gone")
        hold = held.hold
        if hold.quoted_price_inr != details.price_inr:
            renewed = details.model_copy(update={"price_inr": hold.quoted_price_inr})
            with atomic(self._conn):
                self._errands.set_hold(errand_id, hold.hold_id, hold.expires_at)
                fresh = self._approvals.request(errand_id, renewed, run=self._run)
                self._errands.transition(
                    errand_id,
                    "approval_renewed",
                    run=self._run,
                    reason=f"price changed ₹{details.price_inr} -> ₹{hold.quoted_price_inr}",
                )
            return Execution(outcome="needs_reapproval", approval=fresh)
        self._errands.set_hold(errand_id, hold.hold_id, hold.expires_at)
        return hold.hold_id

    async def _book_until_known(
        self, errand_id: str, key: str, hold_id: str, details: ApprovalDetails
    ) -> Execution:
        for attempt in range(1, MAX_BOOK_ATTEMPTS + 1):
            self._actions.count_attempt(key)
            try:
                result = await self._env.book(hold_id, key)
            except ProviderTimeout as exc:
                with atomic(self._conn):
                    self._actions.set_state(key, "verifying", error=str(exc))
                    self._errands.transition(errand_id, "timeout", run=self._run)
                truth = await self._verify(key)
                if isinstance(truth, Booking):
                    return self._finish_booked(errand_id, key, truth, event="confirmed")
                if truth is None:
                    with atomic(self._conn):
                        self._actions.set_state(key, "unverified")
                        self._errands.transition(
                            errand_id,
                            "unverifiable",
                            run=self._run,
                            reason=f"{STATUS_CHECKS} status checks failed",
                        )
                    return Execution(outcome="unverified")
                if attempt < MAX_BOOK_ATTEMPTS:  # provider says not booked: same key again
                    with atomic(self._conn):
                        self._actions.set_state(key, "in_flight")
                        self._errands.transition(
                            errand_id, "retry", run=self._run, reason=f"attempt {attempt + 1}"
                        )
                    continue
                with atomic(self._conn):
                    self._actions.set_state(key, "failed", error="not booked after retries")
                    self._errands.transition(errand_id, "not_booked", run=self._run)
                return Execution(outcome="not_booked")

            if isinstance(result, Booked):
                return self._finish_booked(errand_id, key, result.booking, event="booked")
            return await self._handle_refusal(errand_id, key, result, details)

        raise AssertionError("unreachable: every attempt returns or continues")

    async def _verify(self, key: str) -> Booking | Literal["not_booked"] | None:
        """Ask the source of truth. None means it couldn't be reached."""
        for check in range(STATUS_CHECKS):
            try:
                status = await self._env.get_booking_status(idempotency_key=key)
            except ProviderError:
                if check < STATUS_CHECKS - 1:
                    await self._clock.sleep(STATUS_BACKOFF_SECONDS[check])
                continue
            if isinstance(status, StatusBooked):
                return status.booking
            return "not_booked"  # not_booked, or a key the provider never saw
        return None

    def _finish_booked(
        self, errand_id: str, key: str, booking: Booking, *, event: Literal["booked", "confirmed"]
    ) -> Execution:
        with atomic(self._conn):
            self._actions.set_state(key, "succeeded", booking_id=booking.booking_id)
            self._errands.set_booking(errand_id, booking.booking_id)
            self._errands.transition(errand_id, event, run=self._run, reason=booking.booking_id)
        return Execution(outcome="booked", booking=booking)

    async def _handle_refusal(
        self, errand_id: str, key: str, result: BookFailed, details: ApprovalDetails
    ) -> Execution:
        self._actions.set_state(key, "failed", error=result.status)
        if result.status == "slot_taken":
            self._errands.transition(errand_id, "slot_taken", run=self._run)
            return Execution(outcome="slot_taken")
        if result.status == "hold_expired":
            self._errands.transition(errand_id, "needs_reapproval", run=self._run)
            held = await self._env.hold_slot(details.provider_id, details.slot)
            if isinstance(held, HoldFailed):
                self._errands.transition(errand_id, "option_unavailable", run=self._run)
                return Execution(outcome="slot_gone")
            renewed = details.model_copy(update={"price_inr": held.hold.quoted_price_inr})
            with atomic(self._conn):
                self._errands.set_hold(errand_id, held.hold.hold_id, held.hold.expires_at)
                fresh = self._approvals.request(errand_id, renewed, run=self._run)
            return Execution(outcome="needs_reapproval", approval=fresh)
        # hold_not_found / idempotency_key_reused: our own bookkeeping is wrong.
        raise RuntimeError(f"book refused with {result.status} for key {key}: workflow bug")
