"""Suite-wide safety: no real model calls, and no SQLite connection outlives its test.

- Building a real model (to check configuration) is fine; any actual request to a
  non-test model fails the test instead of spending money or needing a network.
- Every SQLite connection a test opens is closed when the test ends (BUG-9). Test
  objects reference each other in cycles (World -> stores -> connection), so without
  this they're freed only by Python's occasional full GC, and across the suite open
  files peaked at ~300 (each WAL connection holds up to 3), over macOS's default 256.
  Tracking `sqlite3.connect` covers every way a test opens one (store.db.connect,
  World, fixtures) without each of them having to remember to close.
"""

import contextlib
import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest
from pydantic_ai import models

models.ALLOW_MODEL_REQUESTS = False


@pytest.fixture(autouse=True)
def _close_sqlite_connections(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    opened: list[sqlite3.Connection] = []
    real_connect = sqlite3.connect

    def tracking_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        conn = real_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", tracking_connect)
    yield
    for conn in opened:  # autouse: torn down after the test's own fixtures
        # A connection created in another thread (the app under TestClient) can't be
        # touched from here, and the app's own shutdown already closed it.
        with contextlib.suppress(sqlite3.ProgrammingError):
            conn.close()
