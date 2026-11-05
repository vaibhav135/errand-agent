import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from errand_agent.core.clock import FakeClock
from errand_agent.store.db import connect
from errand_agent.store.migrate import MigrationError, migrate, migrations_for

CLOCK = FakeClock(datetime(2026, 11, 5, 9, 0, tzinfo=UTC))


@pytest.fixture
def mig_dir(tmp_path: Path) -> Path:
    d = tmp_path / "migrations"
    d.mkdir()
    return d


@pytest.fixture
def conn(tmp_path: Path):
    return connect(tmp_path / "test.db")


def write(d: Path, name: str, sql: str) -> None:
    (d / name).write_text(sql)


def tables(conn) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {r["name"] for r in rows}


def test_applies_pending_migrations_in_version_order(conn, mig_dir: Path) -> None:
    write(mig_dir, "0002_b.sql", "CREATE TABLE b (a_id INTEGER REFERENCES a(id));")
    write(mig_dir, "0001_a.sql", "CREATE TABLE a (id INTEGER PRIMARY KEY);")

    applied = migrate(conn, mig_dir, CLOCK)

    assert applied == [1, 2]
    assert {"a", "b", "schema_migrations"} <= tables(conn)
    row = conn.execute("SELECT * FROM schema_migrations WHERE version = 1").fetchone()
    assert row["name"] == "a"
    assert row["applied_at"] == "2026-11-05T09:00:00+00:00"


def test_rerun_applies_nothing(conn, mig_dir: Path) -> None:
    write(mig_dir, "0001_a.sql", "CREATE TABLE a (id INTEGER);")
    migrate(conn, mig_dir, CLOCK)

    assert migrate(conn, mig_dir, CLOCK) == []


def test_only_new_migrations_are_applied(conn, mig_dir: Path) -> None:
    write(mig_dir, "0001_a.sql", "CREATE TABLE a (id INTEGER);")
    migrate(conn, mig_dir, CLOCK)
    write(mig_dir, "0002_b.sql", "CREATE TABLE b (id INTEGER);")

    assert migrate(conn, mig_dir, CLOCK) == [2]


def test_failing_migration_rolls_back_completely(conn, mig_dir: Path) -> None:
    write(mig_dir, "0001_ok.sql", "CREATE TABLE ok (id INTEGER);")
    # First statement succeeds, second fails: nothing from 0002 may survive.
    write(mig_dir, "0002_bad.sql", "CREATE TABLE half (id INTEGER);\nNOT VALID SQL;")

    with pytest.raises(MigrationError, match="0002"):
        migrate(conn, mig_dir, CLOCK)

    assert "ok" in tables(conn)
    assert "half" not in tables(conn)
    versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations")]
    assert versions == [1]


def test_edited_applied_migration_is_rejected(conn, mig_dir: Path) -> None:
    write(mig_dir, "0001_a.sql", "CREATE TABLE a (id INTEGER);")
    migrate(conn, mig_dir, CLOCK)
    write(mig_dir, "0001_a.sql", "CREATE TABLE a (id INTEGER, extra TEXT);")

    with pytest.raises(MigrationError, match="changed after it was applied"):
        migrate(conn, mig_dir, CLOCK)


@pytest.mark.parametrize("bad_name", ["init.sql", "1_a.sql", "0001.sql", "0001-a.sql"])
def test_badly_named_sql_file_is_rejected(conn, mig_dir: Path, bad_name: str) -> None:
    write(mig_dir, bad_name, "SELECT 1;")

    with pytest.raises(MigrationError, match=re.escape("NNNN_name.sql")):
        migrate(conn, mig_dir, CLOCK)


def test_non_sql_files_are_ignored(conn, mig_dir: Path) -> None:
    write(mig_dir, ".gitkeep", "")
    write(mig_dir, "README.md", "notes")

    assert migrate(conn, mig_dir, CLOCK) == []


def test_duplicate_version_is_rejected(conn, mig_dir: Path) -> None:
    write(mig_dir, "0001_a.sql", "SELECT 1;")
    write(mig_dir, "0001_b.sql", "SELECT 1;")

    with pytest.raises(MigrationError, match="duplicate"):
        migrate(conn, mig_dir, CLOCK)


@pytest.mark.parametrize("db_name", ["agent", "mock_world"])
def test_packaged_migration_dirs_exist(db_name: str) -> None:
    assert migrations_for(db_name).is_dir()  # type: ignore[arg-type]


def test_unknown_db_name_is_rejected() -> None:
    with pytest.raises(ValueError):
        migrations_for("nope")  # type: ignore[arg-type]
