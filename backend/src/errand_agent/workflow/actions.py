"""Provider actions: the durable record of every side effect we send to a provider.

What: `ProviderActionStore` creates and updates `provider_actions` rows (keyed by our
      idempotency key) and lists the ones still open.

Why:  Booking correctness across retries and process restarts. The row is written at
      the commit point, together with the consumed approval and *before* the provider
      is called, so after a crash we know exactly which keys might have taken effect
      and can ask the provider (`get_booking_status`) instead of guessing.

How:  States: in_flight (sent or about to be sent) -> verifying (timed out; checking
      the source of truth) -> succeeded / failed / unverified (checks exhausted;
      handed to the reconciler). `open_actions()` returns in_flight, verifying and
      unverified rows: exactly what the reconciler must resolve.
"""

import sqlite3
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from errand_agent.core.clock import Clock, utc_text
from errand_agent.store.db import atomic

Operation = Literal["book", "reschedule", "cancel"]
ActionState = Literal["in_flight", "verifying", "succeeded", "failed", "unverified"]
OPEN_STATES: tuple[ActionState, ...] = ("in_flight", "verifying", "unverified")


class ProviderAction(BaseModel):
    model_config = ConfigDict(frozen=True)

    idempotency_key: str
    errand_id: str
    approval_id: str
    operation: Operation
    hold_id: str | None
    booking_id: str | None
    state: ActionState
    attempts: int
    last_error: str | None
    created_at: datetime
    updated_at: datetime


class ProviderActionStore:
    def __init__(self, conn: sqlite3.Connection, *, clock: Clock) -> None:
        self._conn = conn
        self._clock = clock

    def create(
        self,
        key: str,
        *,
        errand_id: str,
        approval_id: str,
        operation: Operation,
        hold_id: str | None = None,
        booking_id: str | None = None,
    ) -> ProviderAction:
        now = utc_text(self._clock.now())
        with atomic(self._conn):
            self._conn.execute(
                "INSERT INTO provider_actions (idempotency_key, errand_id, approval_id,"
                " operation, hold_id, booking_id, state, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, 'in_flight', ?, ?)",
                (key, errand_id, approval_id, operation, hold_id, booking_id, now, now),
            )
        return self.get(key)

    def get(self, key: str) -> ProviderAction:
        row = self._conn.execute(
            "SELECT * FROM provider_actions WHERE idempotency_key = ?", (key,)
        ).fetchone()
        if row is None:
            raise KeyError(f"no provider action {key!r}")
        return _action(row)

    def for_errand(self, errand_id: str) -> list[ProviderAction]:
        rows = self._conn.execute(
            "SELECT * FROM provider_actions WHERE errand_id = ? ORDER BY created_at, rowid",
            (errand_id,),
        ).fetchall()
        return [_action(r) for r in rows]

    def open_actions(self) -> list[ProviderAction]:
        rows = self._conn.execute(
            f"SELECT * FROM provider_actions WHERE state IN ({', '.join('?' * len(OPEN_STATES))})"
            " ORDER BY created_at, rowid",
            OPEN_STATES,
        ).fetchall()
        return [_action(r) for r in rows]

    def count_attempt(self, key: str) -> None:
        with atomic(self._conn):
            self._conn.execute(
                "UPDATE provider_actions SET attempts = attempts + 1, updated_at = ?"
                " WHERE idempotency_key = ?",
                (utc_text(self._clock.now()), key),
            )

    def set_state(
        self,
        key: str,
        state: ActionState,
        *,
        booking_id: str | None = None,
        error: str | None = None,
    ) -> ProviderAction:
        with atomic(self._conn):
            self._conn.execute(
                "UPDATE provider_actions SET state = ?, booking_id = COALESCE(?, booking_id),"
                " last_error = COALESCE(?, last_error), updated_at = ? WHERE idempotency_key = ?",
                (state, booking_id, error, utc_text(self._clock.now()), key),
            )
        return self.get(key)


def _action(row: sqlite3.Row) -> ProviderAction:
    return ProviderAction(
        idempotency_key=row["idempotency_key"],
        errand_id=row["errand_id"],
        approval_id=row["approval_id"],
        operation=row["operation"],
        hold_id=row["hold_id"],
        booking_id=row["booking_id"],
        state=row["state"],
        attempts=row["attempts"],
        last_error=row["last_error"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )
