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
      - `atomic()` joins the caller's transaction or opens one, so store methods
        are atomic alone and composable into larger units of work.
      - `after_commit()` defers side effects (trace events) until the outermost
        transaction commits and drops them on rollback, so nothing reports a
        change that didn't happen.
      - Plain sync sqlite3: queries take microseconds, so briefly blocking the
        event loop is cheaper than the complexity of an async driver.
"""

import sqlite3
from collections.abc import Callable, Iterator
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


# Callbacks to run after the current transaction commits, per connection. Keyed by
# id(): sqlite3.Connection accepts no attributes, and each connection is used from one
# thread only (see the thread rule in docs/COMPONENTS.md, "Database").
_after_commit: dict[int, list[Callable[[], None]]] = {}


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    hooks: list[Callable[[], None]] = []
    _after_commit[id(conn)] = hooks
    try:
        try:
            yield conn
        except BaseException:
            # SQLite may already have ended the transaction itself (RAISE(ROLLBACK),
            # disk full, some I/O errors). A second ROLLBACK would then fail and
            # replace the real error, so only roll back if one is still open. (BUG-1)
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    finally:
        _after_commit.pop(id(conn), None)  # rolled back: the hooks are dropped
    for hook in hooks:
        hook()


@contextmanager
def atomic(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Join the caller's transaction if one is running, otherwise open one.

    Lets a store method be atomic on its own and also be composed into a larger
    unit of work (e.g. hold + approval + state change) without nesting BEGINs.
    """
    if conn.in_transaction:
        yield conn
    else:
        with transaction(conn):
            yield conn


def after_commit(conn: sqlite3.Connection, fn: Callable[[], None]) -> None:
    """Run `fn` once the outermost transaction commits; drop it on rollback.

    Used for side effects that must only reflect committed state, such as trace
    events: "the errand moved to booking" must not be traced if that write is later
    rolled back. Outside any transaction, `fn` runs immediately.
    """
    hooks = _after_commit.get(id(conn))
    if hooks is None:
        fn()
    else:
        hooks.append(fn)
