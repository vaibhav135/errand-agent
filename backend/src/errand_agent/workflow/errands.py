"""Errands: the explicit state machine behind every user task.

What: `TRANSITIONS` (one table per errand kind: book / reschedule / cancel),
      `next_state()`, and `ErrandStore`, which persists errands, their state
      history and requirements in agent.db. `render_mermaid()` draws the table.

Why:  The assignment asks to "represent the workflow explicitly". A plain-data
      transition table makes every legal move visible and testable, and makes
      illegal ones (e.g. booking straight from `comparing`) fail loudly.

How:  - `next_state(kind, state, event)` looks the move up; anything not in the
        table raises InvalidTransition.
      - State groups: TERMINAL (no exits), IN_FLIGHT (a provider side effect is
        running), PARKED (outcome pending reconciliation: `unverified` after a
        book timeout, `cancel_pending` while a cancel keeps getting 503s), and
        ABANDONABLE (only collecting / comparing / awaiting_approval: abandoning an
        in-flight or parked errand would hide whether something happened).
      - `ErrandStore.transition` updates the state and appends to
        `errand_transitions` in one transaction, then emits a `state_transition`
        trace event once the outermost transaction commits (`after_commit`), so a
        transition composed into a larger unit of work that rolls back is never
        traced.
      - Requirements can only change in ABANDONABLE states; once a side effect is
        under way they are frozen.
      - docs/ARCHITECTURE.md embeds `render_mermaid(kind)` for each kind, and a
        test fails if the diagrams drift from this table.
"""

import sqlite3
from datetime import datetime
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict

from errand_agent.core.clock import Clock, utc_text
from errand_agent.core.ids import IdGen
from errand_agent.store.db import after_commit, atomic
from errand_agent.tracing.recorder import RunTrace
from errand_agent.workflow.requirements import Requirements

ErrandKind = Literal["book", "reschedule", "cancel"]
State = Literal[
    "collecting",
    "comparing",
    "awaiting_approval",
    "booking",
    "verifying",
    "unverified",
    "booked",
    "rescheduling",
    "rescheduled",
    "reschedule_failed",
    "cancelling",
    "cancel_pending",
    "cancelled",
    "cancel_failed",
    "abandoned",
]
Event = Literal[
    "options_ready",
    "option_proposed",
    "requirements_changed",
    "approval_renewed",
    "approval_rejected",
    "option_unavailable",
    "approved",
    "booked",
    "slot_taken",
    "needs_reapproval",
    "timeout",
    "confirmed",
    "retry",
    "unverifiable",
    "not_booked",
    "rescheduled",
    "slot_conflict",
    "not_reschedulable",
    "cancelled",
    "not_cancellable",
    "retries_exhausted",
    "window_closed",
    "abandon",
]

INITIAL_STATE: State = "collecting"
TERMINAL: frozenset[State] = frozenset(
    {"booked", "rescheduled", "reschedule_failed", "cancelled", "cancel_failed", "abandoned"}
)
IN_FLIGHT: frozenset[State] = frozenset({"booking", "verifying", "rescheduling", "cancelling"})
PARKED: frozenset[State] = frozenset({"unverified", "cancel_pending"})
ABANDONABLE: frozenset[State] = frozenset({"collecting", "comparing", "awaiting_approval"})

Table = dict[tuple[State, Event], State]

_BOOK: Table = {
    ("collecting", "options_ready"): "comparing",
    ("comparing", "option_proposed"): "awaiting_approval",
    ("comparing", "requirements_changed"): "collecting",
    ("awaiting_approval", "requirements_changed"): "collecting",
    ("awaiting_approval", "approval_renewed"): "awaiting_approval",
    ("awaiting_approval", "approval_rejected"): "comparing",
    ("awaiting_approval", "option_unavailable"): "comparing",
    ("awaiting_approval", "approved"): "booking",
    ("booking", "booked"): "booked",
    ("booking", "slot_taken"): "comparing",
    ("booking", "needs_reapproval"): "awaiting_approval",
    ("booking", "timeout"): "verifying",
    ("verifying", "confirmed"): "booked",
    ("verifying", "retry"): "booking",
    ("verifying", "unverifiable"): "unverified",
    ("verifying", "not_booked"): "comparing",
    ("unverified", "confirmed"): "booked",
    ("unverified", "not_booked"): "comparing",
}

_RESCHEDULE: Table = {
    ("collecting", "option_proposed"): "awaiting_approval",
    ("awaiting_approval", "requirements_changed"): "collecting",
    ("awaiting_approval", "approval_rejected"): "collecting",
    ("awaiting_approval", "approved"): "rescheduling",
    ("rescheduling", "rescheduled"): "rescheduled",
    ("rescheduling", "slot_conflict"): "collecting",
    ("rescheduling", "not_reschedulable"): "reschedule_failed",
}

_CANCEL: Table = {
    ("collecting", "option_proposed"): "awaiting_approval",
    ("awaiting_approval", "approval_rejected"): "collecting",
    ("awaiting_approval", "approved"): "cancelling",
    ("cancelling", "cancelled"): "cancelled",
    ("cancelling", "not_cancellable"): "cancel_failed",
    ("cancelling", "retries_exhausted"): "cancel_pending",
    ("cancel_pending", "cancelled"): "cancelled",
    ("cancel_pending", "not_cancellable"): "cancel_failed",
    ("cancel_pending", "window_closed"): "cancel_failed",
}


def _with_abandon(table: Table) -> Table:
    states = {s for s, _ in table} | set(table.values())
    for state in get_args(State):
        if state in ABANDONABLE and state in states:
            table[(state, "abandon")] = "abandoned"
    return table


TRANSITIONS: dict[ErrandKind, Table] = {
    "book": _with_abandon(_BOOK),
    "reschedule": _with_abandon(_RESCHEDULE),
    "cancel": _with_abandon(_CANCEL),
}


class InvalidTransition(Exception):
    """A move not in the transition table (or an edit the current state forbids)."""


def next_state(kind: ErrandKind, state: State, event: Event) -> State:
    try:
        return TRANSITIONS[kind][(state, event)]
    except KeyError:
        raise InvalidTransition(
            f"{kind} errand in state '{state}' cannot handle event '{event}'"
        ) from None


def render_mermaid(kind: ErrandKind) -> str:
    """Mermaid state diagram of a kind's table (embedded in docs/ARCHITECTURE.md)."""
    table = TRANSITIONS[kind]
    lines = ["stateDiagram-v2", f"    [*] --> {INITIAL_STATE}"]
    lines += [f"    {src} --> {dst}: {event}" for (src, event), dst in table.items()]
    reached = set(table.values())
    lines += [f"    {s} --> [*]" for s in get_args(State) if s in TERMINAL and s in reached]
    return "\n".join(lines)


# --- persistence ---------------------------------------------------------------------------


class Errand(BaseModel):
    model_config = ConfigDict(frozen=True)

    errand_id: str
    session_id: str
    kind: ErrandKind
    state: State
    requirements: Requirements
    booking_id: str | None
    hold_id: str | None
    hold_expires_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ErrandStore:
    def __init__(self, conn: sqlite3.Connection, *, clock: Clock, ids: IdGen) -> None:
        self._conn = conn
        self._clock = clock
        self._ids = ids

    def create_session(self, session_id: str, *, user_id: str) -> None:
        with atomic(self._conn):
            self._conn.execute(
                "INSERT OR IGNORE INTO sessions (session_id, user_id, created_at) VALUES (?, ?, ?)",
                (session_id, user_id, utc_text(self._clock.now())),
            )

    def create(self, session_id: str, kind: ErrandKind, *, booking_id: str | None = None) -> Errand:
        now = utc_text(self._clock.now())
        errand_id = self._ids.new("errand")
        with atomic(self._conn):
            self._conn.execute(
                "INSERT INTO errands (errand_id, session_id, kind, state, requirements,"
                " booking_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    errand_id,
                    session_id,
                    kind,
                    INITIAL_STATE,
                    Requirements().model_dump_json(),
                    booking_id,
                    now,
                    now,
                ),
            )
        return self.get(errand_id)

    def get(self, errand_id: str) -> Errand:
        row = self._conn.execute(
            "SELECT * FROM errands WHERE errand_id = ?", (errand_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"no errand {errand_id!r}")
        return _errand(row)

    def open_errands(self, session_id: str) -> list[Errand]:
        terminal = sorted(TERMINAL)
        rows = self._conn.execute(
            f"SELECT * FROM errands WHERE session_id = ?"
            f" AND state NOT IN ({', '.join('?' * len(terminal))})"
            " ORDER BY created_at, errand_id",
            (session_id, *terminal),
        ).fetchall()
        return [_errand(r) for r in rows]

    def transition(
        self, errand_id: str, event: Event, *, run: RunTrace | None = None, reason: str = ""
    ) -> Errand:
        now = utc_text(self._clock.now())
        with atomic(self._conn):
            current = self.get(errand_id)
            new = next_state(current.kind, current.state, event)  # raises -> rollback
            self._conn.execute(
                "UPDATE errands SET state = ?, updated_at = ? WHERE errand_id = ?",
                (new, now, errand_id),
            )
            self._conn.execute(
                "INSERT INTO errand_transitions (errand_id, from_state, to_state, event,"
                " reason, run_id, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (errand_id, current.state, new, event, reason, run.run_id if run else None, now),
            )
            if run is not None:  # only once committed: traces show what really happened
                payload = {
                    "errand_id": errand_id,
                    "from": current.state,
                    "to": new,
                    "event": event,
                    "reason": reason,
                }
                after_commit(self._conn, lambda: run.emit("state_transition", payload))
        return self.get(errand_id)

    def history(self, errand_id: str) -> list[tuple[str, str, str, str, str | None]]:
        rows = self._conn.execute(
            "SELECT from_state, to_state, event, reason, run_id FROM errand_transitions"
            " WHERE errand_id = ? ORDER BY transition_id",
            (errand_id,),
        ).fetchall()
        return [tuple(r) for r in rows]  # type: ignore[misc]

    def user_of(self, errand_id: str) -> str:
        row = self._conn.execute(
            "SELECT s.user_id FROM errands e JOIN sessions s USING (session_id)"
            " WHERE e.errand_id = ?",
            (errand_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"no errand {errand_id!r}")
        return row["user_id"]

    def set_booking(self, errand_id: str, booking_id: str) -> Errand:
        with atomic(self._conn):
            self._conn.execute(
                "UPDATE errands SET booking_id = ?, updated_at = ? WHERE errand_id = ?",
                (booking_id, utc_text(self._clock.now()), errand_id),
            )
        return self.get(errand_id)

    def set_hold(self, errand_id: str, hold_id: str, expires_at: datetime) -> Errand:
        with atomic(self._conn):
            self._conn.execute(
                "UPDATE errands SET hold_id = ?, hold_expires_at = ?, updated_at = ?"
                " WHERE errand_id = ?",
                (hold_id, utc_text(expires_at), utc_text(self._clock.now()), errand_id),
            )
        return self.get(errand_id)

    def set_requirements(self, errand_id: str, requirements: Requirements) -> Errand:
        with atomic(self._conn):
            current = self.get(errand_id)
            if current.state not in ABANDONABLE:
                raise InvalidTransition(
                    f"cannot change requirements while the errand is '{current.state}'"
                )
            self._conn.execute(
                "UPDATE errands SET requirements = ?, updated_at = ? WHERE errand_id = ?",
                (requirements.model_dump_json(), utc_text(self._clock.now()), errand_id),
            )
        return self.get(errand_id)


def _errand(row: sqlite3.Row) -> Errand:
    return Errand(
        errand_id=row["errand_id"],
        session_id=row["session_id"],
        kind=row["kind"],
        state=row["state"],
        requirements=Requirements.model_validate_json(row["requirements"]),
        booking_id=row["booking_id"],
        hold_id=row["hold_id"],
        hold_expires_at=(
            datetime.fromisoformat(row["hold_expires_at"]) if row["hold_expires_at"] else None
        ),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )
