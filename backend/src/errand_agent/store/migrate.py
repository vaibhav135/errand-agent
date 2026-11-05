"""Migrations: apply ordered, forward-only .sql files to a SQLite database.

What: `migrate(conn, directory, clock)` applies every `NNNN_name.sql` not yet
      recorded in `schema_migrations`, in version order, and returns the
      versions it applied. `migrations_for(db)` locates the packaged folder for
      `agent` or `mock_world`.

Why:  Both databases need a reproducible schema at startup, and tests need a
      fresh one per test. A ~40-line runner on plain sqlite3 covers that;
      Alembic would add SQLAlchemy for features we don't use (autogenerate from
      models, downgrades). Revisit if we adopt SQLAlchemy or need frequent table
      rebuilds for ALTERs SQLite can't do in place.

How:  - Each migration runs inside `transaction()` together with its
        `schema_migrations` row, so a failing script leaves no partial tables.
        This relies on `autocommit=True` connections: in legacy mode,
        `executescript()` would COMMIT first and break that atomicity.
      - A sha256 checksum per applied file: editing an applied migration raises
        instead of letting the local DB silently diverge from the code.
      - Migration files must not contain BEGIN/COMMIT themselves.
      - Files live in the package (`store/migrations/<db>/`) and are found via
        importlib.resources, so the working directory doesn't matter.
"""

import hashlib
import re
import sqlite3
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Literal

from errand_agent.core.clock import Clock
from errand_agent.store.db import transaction

DbName = Literal["agent", "mock_world"]
_DB_NAMES: tuple[DbName, ...] = ("agent", "mock_world")
_FILENAME = re.compile(r"(\d{4})_([a-z0-9_]+)\.sql")


class MigrationError(Exception):
    pass


def migrations_for(db: DbName) -> Traversable:
    if db not in _DB_NAMES:
        raise ValueError(f"unknown database {db!r}; expected one of {_DB_NAMES}")
    return files("errand_agent.store") / "migrations" / db


def _discover(directory: Traversable | Path) -> list[tuple[int, str, str]]:
    found: dict[int, tuple[str, str]] = {}
    for entry in directory.iterdir():
        if not entry.name.endswith(".sql"):
            continue  # .gitkeep, READMEs, etc.
        match = _FILENAME.fullmatch(entry.name)
        if match is None:
            raise MigrationError(f"{entry.name}: migration files must be named NNNN_name.sql")
        version, name = int(match[1]), match[2]
        if version in found:
            raise MigrationError(
                f"duplicate migration version {version:04d}: {found[version][0]} and {name}"
            )
        found[version] = (name, entry.read_text())
    return [(v, name, sql) for v, (name, sql) in sorted(found.items())]


def migrate(conn: sqlite3.Connection, directory: Traversable | Path, clock: Clock) -> list[int]:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
               version    INTEGER PRIMARY KEY,
               name       TEXT NOT NULL,
               checksum   TEXT NOT NULL,
               applied_at TEXT NOT NULL
           )"""
    )
    applied = {
        row["version"]: row["checksum"]
        for row in conn.execute("SELECT version, checksum FROM schema_migrations")
    }

    newly_applied: list[int] = []
    for version, name, sql in _discover(directory):
        label = f"{version:04d}_{name}.sql"
        checksum = hashlib.sha256(sql.encode()).hexdigest()

        if version in applied:
            if applied[version] != checksum:
                raise MigrationError(
                    f"{label} changed after it was applied; add a new migration instead, "
                    "or delete the local database (var/*.db) to rebuild it"
                )
            continue

        try:
            with transaction(conn):
                conn.executescript(sql)
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, checksum, applied_at)"
                    " VALUES (?, ?, ?, ?)",
                    (version, name, checksum, clock.now().isoformat()),
                )
        except sqlite3.Error as exc:
            raise MigrationError(f"{label} failed: {exc}") from exc
        newly_applied.append(version)

    return newly_applied
