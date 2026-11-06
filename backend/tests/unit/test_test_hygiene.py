"""BUG-9: every SQLite connection a test opens is closed when that test ends.

The two tests run in file order: the first opens a connection and keeps a reference
(as World and the fixtures do), the second checks it was closed at the first test's
teardown. Without that, connections pile up until Python's occasional full GC, and
the suite runs out of file descriptors on a default Mac (limit 256).
"""

import sqlite3
from pathlib import Path

import pytest

from errand_agent.store.db import connect

_opened: list[sqlite3.Connection] = []


def test_a_test_opens_a_connection_and_keeps_it(tmp_path: Path) -> None:
    _opened.append(connect(tmp_path / "left-open.db"))


def test_it_was_closed_when_that_test_ended() -> None:
    assert _opened, "must run after the test above (file order)"
    with pytest.raises(sqlite3.ProgrammingError):  # "Cannot operate on a closed database"
        _opened[0].execute("SELECT 1")
