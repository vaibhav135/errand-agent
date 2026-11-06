"""Approval gate: the user's agreement to one exact action, enforced in code.

What: `ApprovalDetails` (action + material details), `fingerprint()`,
      `ApprovalStore` (request / grant / reject / invalidate / consume), and
      `is_explicit_yes()` / `is_explicit_no()` for typed answers.

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
      - The vocabulary is wide (casual, Hinglish, emoji) because people answer in
        their own words, but it is still a fixed list matched in code. The two
        mistakes cost very different amounts: a missed yes falls through to the
        model, which can't approve, so the user taps Approve; a false yes books
        something. So doubtful words stay out ("yo" is a greeting, "ha" a laugh).
      - Typed "no" works the same way; declining can only *remove* authority, so
        the model may also decline fuzzier phrasings (agent tool `decline_pending`).
"""

import hashlib
import json
import re
import sqlite3
from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict

from errand_agent.core.clock import Clock, utc_text
from errand_agent.core.ids import IdGen
from errand_agent.store.db import after_commit, atomic
from errand_agent.tracing.recorder import RunTrace

Action = Literal["book", "reschedule", "cancel"]
ApprovalStatus = Literal["pending", "granted", "rejected", "invalidated", "consumed"]
Via = Literal["button", "typed"]

_YES_WORDS = (
    # plain
    "yes", "y", "yes please", "yep", "yup", "yeah", "yea", "ya", "yah", "sure", "sure thing",
    "ok", "okay", "okie", "k", "absolutely", "perfect", "sounds good",
    # explicit
    "confirm", "confirmed", "i confirm", "approve", "approved",
    "go ahead", "yes go ahead", "ok go ahead", "go for it", "let's do it",
    "book it", "yes book it", "ok book it", "please book it", "do it", "yes do it", "ok do it",
    # Hindi / Hinglish
    "haan", "haan ji", "ha ji", "ji haan", "theek hai", "thik hai", "chalo",
    "kar do", "haan kar do", "book kar do",
)  # fmt: skip
_NO_WORDS = (
    "no", "n", "nope", "nah", "na", "no thanks", "no thank you", "not now",
    "don't", "do not", "never mind", "nevermind", "forget it", "leave it", "skip it",
    "not interested",
    # Hindi / Hinglish
    "nahi", "nai", "mat karo", "rehne do",
)  # fmt: skip
_YES_EMOJI = frozenset("👍✅👌🆗")
_NO_EMOJI = frozenset("👎❌🙅")
# Skin tones, variation selectors and joiners: "👍🏽" and "✅\ufe0f" mean what the bare
# emoji means.
_EMOJI_MODIFIERS = re.compile("[\U0001f3fb-\U0001f3ff\ufe0f\u200d\u2640\u2642]")


def _normalise(text: str) -> str:
    """Lowercase words only, single spaces, and repeated letters collapsed.

    Collapsing runs of the same letter ("yesss" -> "yes", "okkk" -> "ok") is applied to
    the vocabulary too ("book it" -> "bok it" on both sides), so one entry covers every
    stretched spelling without a fuzzy match that could turn a near-miss into a yes.
    """
    words = re.sub(r"[^\w\s]", " ", text.lower().replace("'", ""))
    words = re.sub(r"(\w)\1+", r"\1", words)
    return re.sub(r"\s+", " ", words).strip()


_EXPLICIT_YES = frozenset(_normalise(w) for w in _YES_WORDS)
_EXPLICIT_NO = frozenset(_normalise(w) for w in _NO_WORDS)


def _is_whole_answer(text: str, words: frozenset[str], emoji: frozenset[str]) -> bool:
    stripped = text.strip()
    if not stripped or stripped.endswith("?"):  # a question is not an answer
        return False
    symbols = [c for c in _EMOJI_MODIFIERS.sub("", stripped) if not c.isspace()]
    if symbols and all(c in emoji for c in symbols):  # all([]) is True
        return True
    return _normalise(stripped) in words  # anything extra disqualifies it


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

    Punctuation, case, repeated letters and emoji modifiers are ignored, but a
    question mark means it isn't a confirmation, and anything extra ("yes but make it
    11am") disqualifies it: a conditional yes is not agreement to the exact action.
    """
    return _is_whole_answer(text, _EXPLICIT_YES, _YES_EMOJI)


def is_explicit_no(text: str) -> bool:
    """True only if the *whole* message is a plain decline (same rules as yes)."""
    return _is_whole_answer(text, _EXPLICIT_NO, _NO_EMOJI)


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

    def live_for_errand(self, errand_id: str) -> Approval | None:
        """The errand's pending or granted approval, if any (at most one, by DB index)."""
        row = self._conn.execute(
            "SELECT * FROM approvals WHERE errand_id = ? AND status IN ('pending', 'granted')",
            (errand_id,),
        ).fetchone()
        return _approval(row) if row else None

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
        with atomic(self._conn):
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
        with atomic(self._conn):
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
        with atomic(self._conn):
            current = self.get(approval_id)
            if current.status != "pending":
                raise ApprovalError(f"approval {approval_id} is {current.status}, not pending")
            self._decide(approval_id, "rejected", reason="user declined")
        return self._traced(approval_id, run)

    def invalidate_live(self, errand_id: str, *, reason: str) -> None:
        with atomic(self._conn):
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
        consumed = self.get(approval_id)
        if run is not None:
            after_commit(self._conn, lambda: run.emit("approval", _trace_payload(consumed)))
        return consumed

    # --- internals --------------------------------------------------------------------

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
            after_commit(self._conn, lambda: run.emit("approval", _trace_payload(approval)))
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
