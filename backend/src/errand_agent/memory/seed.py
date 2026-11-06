"""Seed loader: put the generated demo user into agent.db.

What: `seed_demo_user(conn, clock)` loads memory/data/demo_user.json (profile, 55
      memories, 10 past sessions of conversation) if the user has no data yet.

Why:  The demo and evals start from a realistic, lived-in user: long history, many
      memories in every state, so context management is exercised for real.

How:  Rows are inserted directly (this is seeded *history*, not something typed
      today), but every memory value goes through the store's own validation, so the
      seed can't hold anything the store would reject. Times are relative to the
      clock ("days ago"); superseded memories are linked after all rows exist, which
      the superseded_by foreign key requires. Idempotent: returns False when the user
      already has memories or messages.
"""

import json
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

from errand_agent.core.clock import Clock, utc_text
from errand_agent.memory.seedgen import OUTPUT
from errand_agent.memory.store import _to_json, _validate  # pyright: ignore[reportPrivateUsage]
from errand_agent.store.db import atomic


def seed_demo_user(conn: sqlite3.Connection, clock: Clock, *, path: Path = OUTPUT) -> bool:
    data: dict[str, Any] = json.loads(path.read_text())
    user_id: str = data["user_id"]
    has_data = conn.execute(
        "SELECT 1 FROM memories WHERE user_id = ?"
        " UNION SELECT 1 FROM messages m JOIN sessions s USING (session_id) WHERE s.user_id = ?",
        (user_id, user_id),
    ).fetchone()
    if has_data:
        return False

    now = clock.now()
    with atomic(conn):
        _seed_profile(conn, user_id, data["profile"], utc_text(now))
        _seed_sessions(conn, user_id, data["sessions"], now)
        _seed_memories(conn, user_id, data["memories"], now)
    return True


def _seed_profile(conn: sqlite3.Connection, user_id: str, p: dict[str, Any], stamp: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO user_profile (user_id, name, phone, email, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, p["name"], p["phone"], p["email"], stamp, stamp),
    )
    for i, a in enumerate(p["addresses"]):
        conn.execute(
            "INSERT INTO user_addresses (address_id, user_id, label, line, area, is_default,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"addr_seed_{i}",
                user_id,
                a["label"],
                a["line"],
                a["area"],
                int(a["default"]),
                stamp,
                stamp,
            ),
        )


def _seed_sessions(
    conn: sqlite3.Connection, user_id: str, sessions: list[dict[str, Any]], now: Any
) -> None:
    for s in sessions:
        start = now - timedelta(days=s["days_ago"])
        conn.execute(
            "INSERT OR IGNORE INTO sessions (session_id, user_id, created_at) VALUES (?, ?, ?)",
            (s["session_id"], user_id, utc_text(start)),
        )
        for i, m in enumerate(s["messages"]):
            conn.execute(
                "INSERT INTO messages (message_id, session_id, role, content, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    f"msg_{s['session_id']}_{i:03d}",
                    s["session_id"],
                    m["role"],
                    m["content"],
                    utc_text(start + timedelta(minutes=i)),
                ),
            )


def _seed_memories(
    conn: sqlite3.Connection, user_id: str, memories: list[dict[str, Any]], now: Any
) -> None:
    ids = {m["id"]: f"mem_seed_{m['id']}" for m in memories}
    for m in memories:
        at = utc_text(now - timedelta(days=m["days_ago"]))
        forgotten = m["status"] == "forgotten"
        value = None
        if not forgotten:
            typed = _validate(m["key"], m["value"], m["scope"], m.get("topic"))
            value = _to_json(m["key"], typed)
        source = m.get("source", "explicit")
        evidence = None if forgotten else (m.get("quote") or m.get("reason"))
        conn.execute(
            "INSERT INTO memories (memory_id, user_id, key, scope, value, topic, source, status,"
            " evidence, created_at, updated_at, confirmed_at, forgotten_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ids[m["id"]],
                user_id,
                m["key"],
                m["scope"],
                value,
                m.get("topic"),
                source,
                m["status"],
                evidence,
                at,
                at,
                at if m.get("confirmed") else None,
                at if forgotten else None,
            ),
        )
    for m in memories:  # link superseded facts once every row exists
        if "replaces" in m:
            conn.execute(
                "UPDATE memories SET superseded_by = ? WHERE memory_id = ?",
                (ids[m["id"]], ids[m["replaces"]]),
            )
