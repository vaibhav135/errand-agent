import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from errand_agent.core.clock import FakeClock
from errand_agent.core.ids import SequentialIdGen
from errand_agent.memory.conversation import ConversationStore
from errand_agent.store.db import connect
from errand_agent.store.migrate import migrate, migrations_for

NOW = datetime(2026, 11, 5, 3, 30, tzinfo=UTC)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(NOW)


@pytest.fixture
def conn(tmp_path: Path, clock: FakeClock) -> sqlite3.Connection:
    c = connect(tmp_path / "agent.db")
    migrate(c, migrations_for("agent"), clock)
    for session, user in (("sess_old", "user_1"), ("sess_new", "user_1"), ("sess_x", "user_2")):
        c.execute(
            "INSERT INTO sessions (session_id, user_id, created_at) VALUES (?, ?, 'now')",
            (session, user),
        )
    return c


@pytest.fixture
def convo(conn: sqlite3.Connection, clock: FakeClock) -> ConversationStore:
    return ConversationStore(conn, clock=clock, ids=SequentialIdGen())


def test_messages_are_kept_in_order_per_session(convo: ConversationStore, clock: FakeClock) -> None:
    convo.append("sess_new", "user", "I need a plumber")
    clock.advance(5)
    convo.append("sess_new", "assistant", "Which area?", run_id="run_0001")

    msgs = convo.for_session("sess_new")

    assert [(m.role, m.content) for m in msgs] == [
        ("user", "I need a plumber"),
        ("assistant", "Which area?"),
    ]
    assert msgs[1].run_id == "run_0001"


def test_recent_history_spans_the_users_sessions_newest_first(
    convo: ConversationStore, clock: FakeClock
) -> None:
    convo.append("sess_old", "user", "old question")
    clock.advance(60)
    convo.append("sess_x", "user", "someone else's message")
    clock.advance(60)
    convo.append("sess_new", "user", "new question")

    recent = convo.recent_for_user("user_1")

    assert [m.content for m in recent] == ["new question", "old question"]


def test_recent_history_can_be_limited(convo: ConversationStore, clock: FakeClock) -> None:
    for i in range(5):
        convo.append("sess_new", "user", f"m{i}")
        clock.advance(1)

    assert [m.content for m in convo.recent_for_user("user_1", limit=2)] == ["m4", "m3"]


def test_same_second_messages_keep_insertion_order(convo: ConversationStore) -> None:
    convo.append("sess_new", "user", "first")
    convo.append("sess_new", "assistant", "second")

    assert [m.content for m in convo.recent_for_user("user_1")] == ["second", "first"]


@pytest.mark.parametrize(("role", "content"), [("system", "hi"), ("tool", "hi"), ("user", "  ")])
def test_only_user_and_assistant_text_is_stored(
    convo: ConversationStore, role: str, content: str
) -> None:
    with pytest.raises((ValidationError, ValueError)):
        convo.append("sess_new", role, content)  # type: ignore[arg-type]
