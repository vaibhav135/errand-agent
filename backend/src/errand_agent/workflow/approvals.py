"""Approval gate: the user's agreement to one exact action, enforced in code.

What: `ApprovalDetails` (action + material details), `fingerprint()`,
      `ApprovalStore` (request / grant / reject / invalidate / consume) and
      `is_explicit_yes()` for typed confirmations.

Why:  The assignment: booking, cancellation and rescheduling need explicit user
      confirmation of the exact action and material details; approval is enforced
      by application code, not inferred from model output; any material change to
      provider, slot or price invalidates it.

How:  - fingerprint = sha256 over canonical JSON of (action, provider_id, slot as UTC
        text, price_inr, booking_id). Same instant in another time zone -> same
        fingerprint; the display name is not material. It is a correctness check,
        not a secret (no key): the trust boundary is that only the workflow creates
        approvals; the user can only grant or reject one.
      - request(): creates a pending approval and supersedes the errand's live one
        (DB index: at most one pending/granted approval per errand).
      - grant(): pending only, and only with the fingerprint the user was shown
        ("what you saw is what you approved").
      - consume(): inside the executor's transaction, together with persisting the
        idempotency key. Granted only; the *current* details must still match;
        single use, so a repeated confirmation can't execute twice. It writes
        nothing when it refuses: anything written would roll back with the
        caller's transaction anyway, so the caller invalidates as its own step.
      - Typed "yes": the whole message must be an explicit affirmative, and it
        resolves only when exactly one approval is pending in the session. The
        model never decides whether the user agreed.
"""

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict

from errand_agent.core.clock import Clock, utc_text
from errand_agent.core.ids import IdGen
from errand_agent.store.db import transaction
from errand_agent.tracing.recorder import RunTrace

Action = Literal["book", "reschedule", "cancel"]
ApprovalStatus = Literal["pending", "granted", "rejected", "invalidated", "consumed"]
Via = Literal["button", "typed"]

_EXPLICIT_YES = frozenset(
    {
        "yes",
        "y",
        "yes please",
        "yep",
        "confirm",
        "confirmed",
        "i confirm",
        "go ahead",
        "yes go ahead",
        "book it",
        "yes book it",
        "please book it",
        "approve",
        "approved",
        "do it",
        "yes do it",
    }
)


class ApprovalError(Exception):
    """The approval can't be used for this (wrong state, or details changed)."""


class ApprovalDetails(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action: Action
    provider_id: str
    provider_name: str
    slot: AwareDatetime
    price_inr: int
    booking_id: str | None = None  # the existing booking, for reschedule / cancel


class Approval(BaseModel):
    model_config = ConfigDict(frozen=True)

    approval_id: str
    errand_id: str
    action: Action
    fingerprint: str
    details: ApprovalDetails
    status: ApprovalStatus
    created_at: datetime
    decided_at: datetime | None
    decided_via: Via | None
    reason: str | None


def fingerprint(details: ApprovalDetails) -> str:
    material = {
        "action": details.action,
        "provider_id": details.provider_id,
        "slot": utc_text(details.slot),
        "price_inr": details.price_inr,
        "booking_id": details.booking_id,
    }
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def is_explicit_yes(text: str) -> bool:
    """True only if the *whole* message is an explicit affirmative.

    Punctuation at the ends and repeated spaces are ignored, but a question mark
    means it isn't a confirmation, and anything extra ("yes but make it 11am")
    disqualifies it: a conditional yes is not agreement to the exact action.
    """
    stripped = text.strip()
    if stripped.endswith("?"):
        return False
    normalised = re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", stripped.lower())).strip()
    return normalised in _EXPLICIT_YES


class ApprovalStore:
    def __init__(self, conn: sqlite3.Connection, *, clock: Clock, ids: IdGen) -> None:
        self._conn = conn
        self._clock = clock
        self._ids = ids

    # --- queries ----------------------------------------------------------------------

    def get(self, approval_id: str) -> Approval:
        row = self._conn.execute(
            "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"no approval {approval_id!r}")
        return _approval(row)

    def pending_for_session(self, session_id: str) -> list[Approval]:
        rows = self._conn.execute(
            "SELECT a.* FROM approvals a JOIN errands e USING (errand_id)"
            " WHERE e.session_id = ? AND a.status = 'pending' ORDER BY a.created_at, a.approval_id",
            (session_id,),
        ).fetchall()
        return [_approval(r) for r in rows]

    def resolve_typed_yes(self, session_id: str, text: str) -> str | None:
        """The approval a typed message confirms, or None if it confirms nothing."""
        if not is_explicit_yes(text):
            return None
        pending = self.pending_for_session(session_id)
        return pending[0].approval_id if len(pending) == 1 else None

    # --- lifecycle --------------------------------------------------------------------

    def request(
        self, errand_id: str, details: ApprovalDetails, *, run: RunTrace | None = None
    ) -> Approval:
        approval_id = self._ids.new("appr")
        with self._tx():
            self._invalidate_live(errand_id, reason="superseded by a new request")
            self._conn.execute(
                "INSERT INTO approvals (approval_id, errand_id, action, fingerprint, details,"
                " status, created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                (
                    approval_id,
                    errand_id,
                    details.action,
                    fingerprint(details),
                    details.model_dump_json(),
                    utc_text(self._clock.now()),
                ),
            )
        return self._traced(approval_id, run)

    def grant(
        self,
        approval_id: str,
        *,
        presented_fingerprint: str,
        via: Via,
        run: RunTrace | None = None,
    ) -> Approval:
        with self._tx():
            current = self.get(approval_id)
            if current.status != "pending":
                raise ApprovalError(f"approval {approval_id} is {current.status}, not pending")
            if presented_fingerprint != current.fingerprint:
                raise ApprovalError(
                    f"approval {approval_id} changed since it was shown; show the new details"
                )
            self._decide(approval_id, "granted", via=via)
        return self._traced(approval_id, run)

    def reject(self, approval_id: str, *, run: RunTrace | None = None) -> Approval:
        with self._tx():
            current = self.get(approval_id)
            if current.status != "pending":
                raise ApprovalError(f"approval {approval_id} is {current.status}, not pending")
            self._decide(approval_id, "rejected", reason="user declined")
        return self._traced(approval_id, run)

    def invalidate_live(self, errand_id: str, *, reason: str) -> None:
        with self._tx():
            self._invalidate_live(errand_id, reason=reason)

    def consume(
        self,
        approval_id: str,
        current: ApprovalDetails,
        *,
        run: RunTrace | None = None,
    ) -> Approval:
        if not self._conn.in_transaction:
            raise RuntimeError(
                "consume() must run inside the caller's transaction, with the idempotency key"
            )
        approval = self.get(approval_id)
        if approval.status != "granted":
            raise ApprovalError(f"approval {approval_id} is {approval.status}, not granted")
        if fingerprint(current) != approval.fingerprint:
            raise ApprovalError(
                f"approval {approval_id} no longer matches the action (provider, slot or price"
                " changed); ask the user again"
            )
        self._conn.execute(
            "UPDATE approvals SET status = 'consumed' WHERE approval_id = ?", (approval_id,)
        )
        if run is not None:
            run.emit("approval", _trace_payload(self.get(approval_id)))
        return self.get(approval_id)

    # --- internals --------------------------------------------------------------------

    @contextmanager
    def _tx(self) -> Iterator[None]:
        """Join the caller's transaction, or open one."""
        if self._conn.in_transaction:
            yield
        else:
            with transaction(self._conn):
                yield

    def _decide(
        self,
        approval_id: str,
        status: ApprovalStatus,
        *,
        via: Via | None = None,
        reason: str | None = None,
    ) -> None:
        self._conn.execute(
            "UPDATE approvals SET status = ?, decided_at = ?, decided_via = ?, reason = ?"
            " WHERE approval_id = ?",
            (status, utc_text(self._clock.now()), via, reason, approval_id),
        )

    def _invalidate_live(self, errand_id: str, *, reason: str) -> None:
        self._conn.execute(
            "UPDATE approvals SET status = 'invalidated', decided_at = ?, reason = ?"
            " WHERE errand_id = ? AND status IN ('pending', 'granted')",
            (utc_text(self._clock.now()), reason, errand_id),
        )

    def _traced(self, approval_id: str, run: RunTrace | None) -> Approval:
        approval = self.get(approval_id)
        if run is not None:
            run.emit("approval", _trace_payload(approval))
        return approval


def _trace_payload(a: Approval) -> dict[str, object]:
    return {
        "approval_id": a.approval_id,
        "errand_id": a.errand_id,
        "action": a.action,
        "status": a.status,
        "fingerprint": a.fingerprint,
        "via": a.decided_via,
    }


def _approval(row: sqlite3.Row) -> Approval:
    return Approval(
        approval_id=row["approval_id"],
        errand_id=row["errand_id"],
        action=row["action"],
        fingerprint=row["fingerprint"],
        details=ApprovalDetails.model_validate_json(row["details"]),
        status=row["status"],
        created_at=datetime.fromisoformat(row["created_at"]),
        decided_at=datetime.fromisoformat(row["decided_at"]) if row["decided_at"] else None,
        decided_via=row["decided_via"],
        reason=row["reason"],
    )
