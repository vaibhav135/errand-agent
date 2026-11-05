import sqlite3
from pathlib import Path

import pytest

from errand_agent.store.db import connect, transaction


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "nested" / "dir" / "test.db"  # parent dirs don't exist yet


def test_connect_creates_parent_dirs(db_path: Path) -> None:
    connect(db_path).close()

    assert db_path.exists()


def test_connect_applies_pragmas(db_path: Path) -> None:
    conn = connect(db_path)

    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL


def test_rows_are_accessible_by_column_name(db_path: Path) -> None:
    conn = connect(db_path)

    row = conn.execute("SELECT 1 AS answer").fetchone()

    assert row["answer"] == 1


def test_transaction_commits_on_success(db_path: Path) -> None:
    writer = connect(db_path)
    writer.execute("CREATE TABLE t (x INTEGER)")

    with transaction(writer):
        writer.execute("INSERT INTO t VALUES (1)")

    reader = connect(db_path)  # separate connection sees only committed data
    assert reader.execute("SELECT count(*) FROM t").fetchone()[0] == 1


def test_transaction_rolls_back_and_reraises_on_error(db_path: Path) -> None:
    conn = connect(db_path)
    conn.execute("CREATE TABLE t (x INTEGER)")

    with pytest.raises(RuntimeError, match="boom"), transaction(conn):
        conn.execute("INSERT INTO t VALUES (1)")
        raise RuntimeError("boom")

    assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 0
    assert not conn.in_transaction


@pytest.mark.xfail(strict=True, reason="BUG-1: ROLLBACK after SQLite auto-rollback masks error")
def test_original_error_survives_when_sqlite_already_rolled_back(db_path: Path) -> None:
    # Some failures make SQLite end the transaction itself (RAISE(ROLLBACK) in a
    # trigger, disk full, some I/O errors). Our own ROLLBACK must not then fail
    # with "no transaction is active" and replace the real cause.
    conn = connect(db_path)
    conn.executescript(
        """
        CREATE TABLE t (x INTEGER);
        CREATE TRIGGER guard BEFORE INSERT ON t WHEN NEW.x < 0
        BEGIN SELECT RAISE(ROLLBACK, 'negative not allowed'); END;
        """
    )

    with pytest.raises(sqlite3.IntegrityError, match="negative not allowed"), transaction(conn):
        conn.execute("INSERT INTO t VALUES (-1)")

    assert not conn.in_transaction


def test_transaction_takes_write_lock_at_begin(db_path: Path) -> None:
    # BEGIN IMMEDIATE: the lock is taken when the transaction starts, before any
    # write. A second writer is refused at BEGIN instead of both proceeding and
    # one failing mid-transaction after it has already done work.
    a = connect(db_path)
    b = connect(db_path, busy_timeout_ms=50)

    # Entered left to right: A begins (no write yet), then B's BEGIN must fail.
    with (
        transaction(a),
        pytest.raises(sqlite3.OperationalError, match="locked"),
        transaction(b),
    ):
        pass
