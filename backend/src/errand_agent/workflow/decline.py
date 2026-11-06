"""Decline: the user says no to whatever is waiting for their approval.

What: `decline_pending(conn, approvals, errands, session_id, run=)` rejects every
      pending approval in the session and moves each errand back a step
      (`approval_rejected`: a booking goes back to comparing, a change to collecting).

Why:  "No" must be as reliable as "yes", and both the code fast path (typed "no")
      and the model (fuzzier "neit, leave it") need the same operation. Declining
      can only take authority away: nothing is booked, cancelled or charged, which is
      why the model is allowed to call it.

How:  - The approval rejection and the errand transition are one transaction, so an
        errand never sits in awaiting_approval with nothing to approve.
      - All pending approvals in the session are declined, not one picked by the
        caller: the model can't aim this at a particular card, and a "no" while two
        cards are open is safest read as "neither".
      - The provider hold isn't released (the contract has no release call); it
        expires on its own.
"""

import sqlite3

from errand_agent.store.db import atomic
from errand_agent.tracing.recorder import RunTrace
from errand_agent.workflow.approvals import Approval, ApprovalStore
from errand_agent.workflow.errands import ErrandStore


def decline_pending(
    conn: sqlite3.Connection,
    approvals: ApprovalStore,
    errands: ErrandStore,
    session_id: str,
    *,
    run: RunTrace | None = None,
    only: str | None = None,
) -> list[Approval]:
    """Decline everything pending in the session, or only the approval `only` (a card's
    own Decline button)."""
    declined: list[Approval] = []
    with atomic(conn):
        for pending in approvals.pending_for_session(session_id):
            if only is not None and pending.approval_id != only:
                continue
            declined.append(approvals.reject(pending.approval_id, run=run))
            errands.transition(
                pending.errand_id, "approval_rejected", run=run, reason="user declined"
            )
    return declined
