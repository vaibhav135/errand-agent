"""Booking flow: propose an option, then (next commit) execute the approved booking.

What: `BookingFlow.propose(errand_id, option, slot)` holds the slot with the
      provider and asks the user to approve the exact provider, slot and quoted
      price.

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
"""

import sqlite3
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from errand_agent.core.clock import Clock
from errand_agent.core.ids import IdGen
from errand_agent.providers.models import Hold, HoldFailed
from errand_agent.providers.tool_env import ToolEnv
from errand_agent.store.db import atomic
from errand_agent.tracing.recorder import RunTrace
from errand_agent.workflow.approvals import Approval, ApprovalDetails, ApprovalStore
from errand_agent.workflow.errands import ErrandStore, InvalidTransition
from errand_agent.workflow.ranking import Option


class Proposal(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["proposed", "unavailable"]
    approval: Approval | None = None
    hold: Hold | None = None
    price_changed_from: int | None = None  # the search price, when the quote differs
    reason: str | None = None


class BookingFlow:
    def __init__(
        self,
        *,
        conn: sqlite3.Connection,
        env: ToolEnv,
        errands: ErrandStore,
        approvals: ApprovalStore,
        clock: Clock,
        ids: IdGen,
        run: RunTrace | None = None,
    ) -> None:
        self._conn = conn
        self._env = env
        self._errands = errands
        self._approvals = approvals
        self._clock = clock
        self._ids = ids
        self._run = run

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
