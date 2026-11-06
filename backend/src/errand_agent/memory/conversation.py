"""Conversation: what the user and the assistant said, across sessions.

What: `ConversationStore.append / for_session / recent_for_user`.

Why:  The context builder needs the recent conversation (newest first, so it can fill
      the token budget from the most recent turn backwards), and the assignment seeds a
      long prior conversation that spans earlier sessions.

How:  Only user and assistant text is stored; tool calls and results live in traces
      and workflow tables. Ordering is by time, then by insertion (rowid), so messages
      written in the same second keep their order.
"""

import sqlite3
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from errand_agent.core.clock import Clock, utc_text
from errand_agent.core.ids import IdGen
from errand_agent.store.db import atomic

Role = Literal["user", "assistant"]


class Message(BaseModel):
    model_config = ConfigDict(frozen=True)

    message_id: str
    session_id: str
    role: Role
    content: str
    run_id: str | None
    created_at: datetime

    @field_validator("content")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("a message needs content")
        return value


class ConversationStore:
    def __init__(self, conn: sqlite3.Connection, *, clock: Clock, ids: IdGen) -> None:
        self._conn = conn
        self._clock = clock
        self._ids = ids

    def append(
        self, session_id: str, role: Role, content: str, *, run_id: str | None = None
    ) -> Message:
        message = Message(
            message_id=self._ids.new("msg"),
            session_id=session_id,
            role=role,
            content=content,
            run_id=run_id,
            created_at=self._clock.now(),
        )
        with atomic(self._conn):
            self._conn.execute(
                "INSERT INTO messages (message_id, session_id, role, content, run_id, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    message.message_id,
                    session_id,
                    role,
                    content,
                    run_id,
                    utc_text(message.created_at),
                ),
            )
        return message

    def for_session(self, session_id: str) -> list[Message]:
        rows = self._conn.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at, rowid",
            (session_id,),
        ).fetchall()
        return [_message(r) for r in rows]

    def recent_for_user(self, user_id: str, *, limit: int | None = None) -> list[Message]:
        """The user's messages across sessions, newest first."""
        rows = self._conn.execute(
            "SELECT m.* FROM messages m JOIN sessions s USING (session_id)"
            " WHERE s.user_id = ? ORDER BY m.created_at DESC, m.rowid DESC LIMIT ?",
            (user_id, -1 if limit is None else limit),
        ).fetchall()
        return [_message(r) for r in rows]


def _message(row: sqlite3.Row) -> Message:
    return Message(
        message_id=row["message_id"],
        session_id=row["session_id"],
        role=row["role"],
        content=row["content"],
        run_id=row["run_id"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )
