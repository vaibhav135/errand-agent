"""Memory store: long-term preferences the user can inspect, correct and forget.

What: `MemoryStore` with remember (explicit), propose_inferred / confirm / reject,
      correct, forget and list_for_user, over a typed, closed key vocabulary.

Why:  The assignment: remember stable preferences across sessions (preferred area,
      earliest acceptable time, technician), let users inspect, correct and forget
      them, and distinguish explicit from inferred facts. Our own threat model adds:
      nothing in tool output may ever become a memory (memory poisoning).

How:  - Three kinds of user information live in three places: profile PII
        (user_profile), long-term preferences (here), and one errand's details
        (errands.requirements). A budget for *this* job is not a memory.
      - Closed keys with typed values: time windows ("HH:MM"), budgets (positive
        rupees, scoped to a service), preferred / avoided providers (scoped to a
        service), the default address label, and topic-tagged notes. Relevance can
        then be decided in code.
      - Explicit facts need a quote that appears verbatim (ignoring case and
        spacing) in the user's own message. Text from a review can't satisfy that,
        and the model can't invent a preference the user never stated.
      - Inferred facts (from code rules over booking history) start
        `pending_confirmation` and never count until the user confirms. A rejected
        suggestion isn't offered again; a forgotten fact can't be re-inferred.
      - One active fact per (user, key, scope): a new one supersedes the old one
        (`superseded_by` chain). Notes may coexist.
      - Forget wipes value and evidence and keeps a tombstone (key, scope, time).
      - Changes emit a `memory` trace event after commit; a forget event doesn't
        repeat the forgotten value.
"""

import json
import re
import sqlite3
from datetime import datetime, time
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, PositiveInt, TypeAdapter

from errand_agent.core.clock import Clock, utc_text
from errand_agent.core.ids import IdGen
from errand_agent.providers.models import Service
from errand_agent.store.db import after_commit, atomic
from errand_agent.tracing.recorder import RunTrace

MemoryKey = Literal[
    "default_address",
    "earliest_start",
    "latest_start",
    "budget_max_inr",
    "preferred_provider",
    "avoided_provider",
    "note",
]
MemoryStatus = Literal["active", "pending_confirmation", "superseded", "forgotten", "rejected"]
Source = Literal["explicit", "inferred"]
Topic = Literal["home_services", "food", "travel", "family", "work", "other"]

SERVICES: frozenset[str] = frozenset(get_args(Service))
_SERVICE_SCOPED = frozenset({"budget_max_inr", "preferred_provider", "avoided_provider"})
_GLOBAL_ONLY = frozenset({"default_address", "note"})
_TIME_KEYS = frozenset({"earliest_start", "latest_start"})


class ProvenanceError(Exception):
    """An explicit memory whose quote isn't in the user's own message."""


class ProviderRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_id: str
    name: str


_VALUE_TYPES: dict[str, TypeAdapter[Any]] = {
    "default_address": TypeAdapter(str),
    "earliest_start": TypeAdapter(time),
    "latest_start": TypeAdapter(time),
    "budget_max_inr": TypeAdapter(PositiveInt),
    "preferred_provider": TypeAdapter(ProviderRef),
    "avoided_provider": TypeAdapter(ProviderRef),
    "note": TypeAdapter(str),
}


class Memory(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    memory_id: str
    user_id: str
    key: MemoryKey
    scope: str
    value: Any  # typed per key; None once forgotten
    topic: Topic | None
    source: Source
    status: MemoryStatus
    evidence: str | None
    superseded_by: str | None
    created_at: datetime
    updated_at: datetime
    confirmed_at: datetime | None
    forgotten_at: datetime | None


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text.casefold()).strip()


def _validate(key: str, value: object, scope: str, topic: str | None) -> Any:
    if key not in _VALUE_TYPES:
        raise ValueError(f"unknown memory key {key!r}")
    if key in _SERVICE_SCOPED and scope not in SERVICES:
        raise ValueError(f"{key} must be scoped to a service, got {scope!r}")
    if key in _GLOBAL_ONLY and scope != "global":
        raise ValueError(f"{key} is global, got scope {scope!r}")
    if scope != "global" and scope not in SERVICES:
        raise ValueError(f"unknown scope {scope!r}")
    if key == "note" and topic not in get_args(Topic):
        raise ValueError(f"a note needs a topic from {get_args(Topic)}")
    if key != "note" and topic is not None:
        raise ValueError("only notes have a topic")
    return _VALUE_TYPES[key].validate_python(value)


def _to_json(key: str, value: Any) -> str:
    if key in _TIME_KEYS:
        return json.dumps(value.strftime("%H:%M"))
    return _VALUE_TYPES[key].dump_json(value).decode()


def _from_json(key: str, raw: str | None) -> Any:
    if raw is None:
        return None
    return _VALUE_TYPES[key].validate_python(json.loads(raw))


class MemoryStore:
    def __init__(self, conn: sqlite3.Connection, *, clock: Clock, ids: IdGen) -> None:
        self._conn = conn
        self._clock = clock
        self._ids = ids

    # --- queries ------------------------------------------------------------------------

    def get(self, memory_id: str) -> Memory:
        row = self._conn.execute(
            "SELECT * FROM memories WHERE memory_id = ?", (memory_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"no memory {memory_id!r}")
        return _memory(row)

    def list_for_user(self, user_id: str) -> list[Memory]:
        """What the user can inspect: active facts and suggestions awaiting an answer."""
        rows = self._conn.execute(
            "SELECT * FROM memories WHERE user_id = ?"
            " AND status IN ('active', 'pending_confirmation') ORDER BY created_at, memory_id",
            (user_id,),
        ).fetchall()
        return [_memory(r) for r in rows]

    def all_for_user(self, user_id: str) -> list[Memory]:
        """Every row, tombstones included (for context selection and audits)."""
        rows = self._conn.execute(
            "SELECT * FROM memories WHERE user_id = ? ORDER BY created_at, memory_id", (user_id,)
        ).fetchall()
        return [_memory(r) for r in rows]

    # --- writes ---------------------------------------------------------------------------

    def remember(
        self,
        user_id: str,
        key: MemoryKey,
        value: object,
        *,
        scope: str = "global",
        topic: Topic | None = None,
        quote: str,
        user_message: str,
        run: RunTrace | None = None,
    ) -> Memory:
        typed = _validate(key, value, scope, topic)
        if not _normalise(quote) or _normalise(quote) not in _normalise(user_message):
            raise ProvenanceError(
                "an explicit memory needs a quote taken verbatim from the user's message"
            )
        memory_id = self._ids.new("mem")
        with atomic(self._conn):
            replaced = self._supersede_active(user_id, key, scope) if key != "note" else []
            self._insert(
                memory_id,
                user_id,
                key,
                scope,
                typed,
                topic,
                "explicit",
                "active",
                evidence=quote.strip(),
            )
            self._link_superseded(replaced, memory_id)
        return self._traced(memory_id, "remembered", run)

    def propose_inferred(
        self,
        user_id: str,
        key: MemoryKey,
        value: object,
        *,
        scope: str = "global",
        topic: Topic | None = None,
        reason: str,
        run: RunTrace | None = None,
    ) -> Memory | None:
        """Suggest an inferred fact; None if forgotten, rejected or already known."""
        typed = _validate(key, value, scope, topic)
        stored = _to_json(key, typed)
        rows = self._conn.execute(
            "SELECT status, value FROM memories WHERE user_id = ? AND key = ? AND scope = ?",
            (user_id, key, scope),
        ).fetchall()
        for row in rows:
            if row["status"] == "forgotten":
                return None  # the user asked us to forget this: don't bring it back
            if row["value"] == stored and row["status"] in (
                "active",
                "pending_confirmation",
                "rejected",
            ):
                return None
        memory_id = self._ids.new("mem")
        with atomic(self._conn):
            self._insert(
                memory_id,
                user_id,
                key,
                scope,
                typed,
                topic,
                "inferred",
                "pending_confirmation",
                evidence=reason,
            )
        return self._traced(memory_id, "suggested", run)

    def confirm(self, memory_id: str, *, run: RunTrace | None = None) -> Memory:
        m = self._require(memory_id, "pending_confirmation")
        now = utc_text(self._clock.now())
        with atomic(self._conn):
            if m.key != "note":
                self._link_superseded(self._supersede_active(m.user_id, m.key, m.scope), memory_id)
            self._conn.execute(
                "UPDATE memories SET status = 'active', confirmed_at = ?, updated_at = ?"
                " WHERE memory_id = ?",
                (now, now, memory_id),
            )
        return self._traced(memory_id, "confirmed", run)

    def reject(self, memory_id: str, *, run: RunTrace | None = None) -> Memory:
        self._require(memory_id, "pending_confirmation")
        with atomic(self._conn):
            self._set_status(memory_id, "rejected")
        return self._traced(memory_id, "rejected", run)

    def correct(
        self,
        memory_id: str,
        new_value: object,
        *,
        quote: str,
        user_message: str,
        run: RunTrace | None = None,
    ) -> Memory:
        old = self.get(memory_id)
        if old.status not in ("active", "pending_confirmation"):
            raise ValueError(f"memory {memory_id} is {old.status}; nothing to correct")
        with atomic(self._conn):
            new = self.remember(
                old.user_id,
                old.key,
                new_value,
                scope=old.scope,
                topic=old.topic,
                quote=quote,
                user_message=user_message,
                run=run,
            )
            if old.status == "pending_confirmation":  # remember() only supersedes active ones
                self._conn.execute(
                    "UPDATE memories SET status = 'superseded', superseded_by = ?, updated_at = ?"
                    " WHERE memory_id = ?",
                    (new.memory_id, utc_text(self._clock.now()), memory_id),
                )
        return new

    def forget(self, memory_id: str, *, run: RunTrace | None = None) -> Memory:
        m = self.get(memory_id)
        if m.status not in ("active", "pending_confirmation"):
            raise ValueError(f"memory {memory_id} is {m.status}; nothing to forget")
        now = utc_text(self._clock.now())
        with atomic(self._conn):
            self._conn.execute(
                "UPDATE memories SET status = 'forgotten', value = NULL, evidence = NULL,"
                " forgotten_at = ?, updated_at = ? WHERE memory_id = ?",
                (now, now, memory_id),
            )
        return self._traced(memory_id, "forgotten", run)

    # --- internals ------------------------------------------------------------------------

    def _require(self, memory_id: str, status: MemoryStatus) -> Memory:
        m = self.get(memory_id)
        if m.status != status:
            raise ValueError(f"memory {memory_id} is {m.status}, not {status}")
        return m

    def _insert(
        self,
        memory_id: str,
        user_id: str,
        key: str,
        scope: str,
        value: Any,
        topic: str | None,
        source: Source,
        status: MemoryStatus,
        *,
        evidence: str,
    ) -> None:
        now = utc_text(self._clock.now())
        self._conn.execute(
            "INSERT INTO memories (memory_id, user_id, key, scope, value, topic, source, status,"
            " evidence, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                memory_id,
                user_id,
                key,
                scope,
                _to_json(key, value),
                topic,
                source,
                status,
                evidence,
                now,
                now,
            ),
        )

    def _supersede_active(self, user_id: str, key: str, scope: str) -> list[str]:
        """Mark the current active fact superseded; link it once the new row exists.

        Two steps because of two constraints: the one-active index forbids inserting
        the new fact first, and the superseded_by foreign key forbids pointing at a
        row that doesn't exist yet.
        """
        rows = self._conn.execute(
            "SELECT memory_id FROM memories"
            " WHERE user_id = ? AND key = ? AND scope = ? AND status = 'active'",
            (user_id, key, scope),
        ).fetchall()
        for row in rows:
            self._set_status(row["memory_id"], "superseded")
        return [row["memory_id"] for row in rows]

    def _link_superseded(self, old_ids: list[str], new_id: str) -> None:
        for old_id in old_ids:
            self._conn.execute(
                "UPDATE memories SET superseded_by = ? WHERE memory_id = ?", (new_id, old_id)
            )

    def _set_status(self, memory_id: str, status: MemoryStatus) -> None:
        self._conn.execute(
            "UPDATE memories SET status = ?, updated_at = ? WHERE memory_id = ?",
            (status, utc_text(self._clock.now()), memory_id),
        )

    def _traced(self, memory_id: str, action: str, run: RunTrace | None) -> Memory:
        m = self.get(memory_id)
        if run is not None:
            payload: dict[str, object] = {
                "action": action,
                "memory_id": m.memory_id,
                "key": m.key,
                "scope": m.scope,
                "source": m.source,
                "status": m.status,
            }
            if m.status != "forgotten":
                payload["value"] = _to_json(m.key, m.value) if m.value is not None else None
            after_commit(self._conn, lambda: run.emit("memory", payload))
        return m


def _memory(row: sqlite3.Row) -> Memory:
    def ts(name: str) -> datetime | None:
        return datetime.fromisoformat(row[name]) if row[name] else None

    return Memory(
        memory_id=row["memory_id"],
        user_id=row["user_id"],
        key=row["key"],
        scope=row["scope"],
        value=_from_json(row["key"], row["value"]),
        topic=row["topic"],
        source=row["source"],
        status=row["status"],
        evidence=row["evidence"],
        superseded_by=row["superseded_by"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        confirmed_at=ts("confirmed_at"),
        forgotten_at=ts("forgotten_at"),
    )
