"""SQLite access: connections with safe defaults, and explicit transactions.

What: `connect(path)` returns a configured `sqlite3.Connection`;
      `transaction(conn)` is a context manager that commits or rolls back.

Why:  Booking correctness depends on atomic, durable writes, e.g. "persist the
      idempotency key and move to `booking`" must happen together, before
      `book` is called, and survive a process crash. The chat loop and the
      proactive monitor also write concurrently, so locking must be predictable.

How:  - `autocommit=True` (Python 3.12+): the sqlite3 module never opens
        transactions implicitly; every transaction is one we start ourselves.
      - WAL: readers don't block the writer. `synchronous=NORMAL` with WAL keeps
        committed data across a process crash (may lose the last commit only on
        power loss: acceptable here).
      - `busy_timeout`: wait for a lock instead of failing instantly.
      - `transaction()` uses `BEGIN IMMEDIATE`, taking the write lock up front so
        two writers queue at BEGIN instead of one failing mid-transaction.
      - Plain sync sqlite3: queries take microseconds, so briefly blocking the
        event loop is cheaper than the complexity of an async driver.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def connect(path: Path, *, busy_timeout_ms: int = 5000) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, autocommit=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
