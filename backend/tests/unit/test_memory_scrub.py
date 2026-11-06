import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from errand_agent.core.clock import FakeClock
from errand_agent.core.ids import SequentialIdGen
from errand_agent.memory.conversation import ConversationStore
from errand_agent.memory.profile import ProfileStore
from errand_agent.memory.store import MemoryStore
from errand_agent.store.db import connect
from errand_agent.store.migrate import migrate, migrations_for

NOW = datetime(2026, 11, 5, 3, 30, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = connect(tmp_path / "agent.db")
    migrate(c, migrations_for("agent"), FakeClock(NOW))
    for session, user in (("sess_1", "user_1"), ("sess_2", "user_2")):
        c.execute(
            "INSERT INTO sessions (session_id, user_id, created_at) VALUES (?, ?, 'now')",
            (session, user),
        )
    return c


@pytest.fixture
def convo(conn: sqlite3.Connection) -> ConversationStore:
    return ConversationStore(conn, clock=FakeClock(NOW), ids=SequentialIdGen())


@pytest.fixture
def memories(conn: sqlite3.Connection) -> MemoryStore:
    return MemoryStore(conn, clock=FakeClock(NOW), ids=SequentialIdGen())


@pytest.fixture
def profiles(conn: sqlite3.Connection) -> ProfileStore:
    return ProfileStore(conn, clock=FakeClock(NOW), ids=SequentialIdGen())


def texts(convo: ConversationStore, session: str) -> list[str]:
    return [m.content for m in convo.for_session(session)]


# --- forgetting scrubs the user's own words from history ------------------------------


def test_forget_scrubs_the_quote_from_stored_messages(
    convo: ConversationStore, memories: MemoryStore
) -> None:
    said = "Oh and I'm   ALLERGIC to cats, so no cat-owning cleaners please."
    convo.append("sess_1", "user", said)
    convo.append("sess_2", "user", "I'm allergic to cats too")  # another user: untouched
    m = memories.remember(
        "user_1",
        "note",
        "allergic to cats",
        topic="home_services",
        quote="I'm allergic to cats",
        user_message=said,
    )

    memories.forget(m.memory_id)

    [scrubbed] = texts(convo, "sess_1")
    assert "cats" not in scrubbed.lower().split("cat-owning")[0]
    assert scrubbed == "Oh and [forgotten], so no cat-owning cleaners please."
    assert texts(convo, "sess_2") == ["I'm allergic to cats too"]


def test_forgetting_an_inferred_fact_scrubs_nothing(
    convo: ConversationStore, memories: MemoryStore
) -> None:
    convo.append("sess_1", "user", "Book Sparks again please")
    m = memories.propose_inferred(
        "user_1",
        "preferred_provider",
        {"provider_id": "ele_sparks", "name": "Sparks"},
        scope="electrician",
        reason="2 bookings",
    )
    assert m is not None

    memories.forget(m.memory_id)

    assert texts(convo, "sess_1") == ["Book Sparks again please"]  # no quote to scrub


# --- saving profile details scrubs them from history ----------------------------------------


@pytest.mark.parametrize("typed", ["+91 98450 12345", "9845012345", "98450-12345", "+919845012345"])
def test_saved_phone_is_scrubbed_in_any_format(
    convo: ConversationStore, profiles: ProfileStore, typed: str
) -> None:
    said = f"you can reach me on {typed} anytime"
    convo.append("sess_1", "user", said)

    profiles.update("user_1", phone=typed, user_message=said)

    assert texts(convo, "sess_1") == ["you can reach me on [phone saved] anytime"]


def test_saved_email_name_and_street_are_scrubbed_but_area_is_kept(
    convo: ConversationStore, profiles: ProfileStore
) -> None:
    said = "I'm Priya Sharma, mail Priya.Sharma@example.com. Home is 12, 4th Cross, HSR Layout."
    convo.append("sess_1", "user", said)

    profiles.update(
        "user_1", name="Priya Sharma", email="priya.sharma@example.com", user_message=said
    )
    profiles.add_address(
        "user_1", label="Home", line="12, 4th Cross", area="HSR Layout", user_message=said
    )

    assert texts(convo, "sess_1") == [
        "I'm [name saved], mail [email saved]. Home is [address saved], HSR Layout."
    ]


def test_assistant_echoes_are_scrubbed_too(
    convo: ConversationStore, profiles: ProfileStore
) -> None:
    said = "my number is 98450 12345"
    convo.append("sess_1", "user", said)
    convo.append("sess_1", "assistant", "Got it, I'll use 98450 12345 for updates.")

    profiles.update("user_1", phone="98450 12345", user_message=said)

    assert all("98450" not in t for t in texts(convo, "sess_1"))
