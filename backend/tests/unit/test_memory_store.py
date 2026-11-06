import sqlite3
from datetime import UTC, datetime, time
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from errand_agent.core.clock import FakeClock
from errand_agent.core.ids import SequentialIdGen
from errand_agent.memory.store import MemoryStore, ProvenanceError
from errand_agent.store.db import connect
from errand_agent.store.migrate import migrate, migrations_for
from errand_agent.tracing.recorder import TraceEvent, Tracer

NOW = datetime(2026, 11, 5, 3, 30, tzinfo=UTC)
USER = "user_1"
SAID = "Please remember I always want morning slots, never before 10am."


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(NOW)


@pytest.fixture
def conn(tmp_path: Path, clock: FakeClock) -> sqlite3.Connection:
    c = connect(tmp_path / "agent.db")
    migrate(c, migrations_for("agent"), clock)
    return c


@pytest.fixture
def store(conn: sqlite3.Connection, clock: FakeClock) -> MemoryStore:
    return MemoryStore(conn, clock=clock, ids=SequentialIdGen())


def remember_start(store: MemoryStore, value: str = "10:00", said: str = SAID):  # type: ignore[no-untyped-def]
    return store.remember(
        USER, "earliest_start", value, quote="never before 10am", user_message=said
    )


# --- explicit memories need the user's own words ------------------------------------------


def test_explicit_memory_is_stored_with_its_evidence(store: MemoryStore) -> None:
    m = remember_start(store)

    assert (m.key, m.scope, m.value, m.source, m.status) == (
        "earliest_start",
        "global",
        time(10),
        "explicit",
        "active",
    )
    assert m.evidence == "never before 10am"


def test_quote_must_appear_in_the_users_message(store: MemoryStore) -> None:
    with pytest.raises(ProvenanceError):
        store.remember(
            USER,
            "preferred_provider",
            {"provider_id": "ac_chillzone", "name": "ChillZone"},
            scope="ac_servicing",
            quote="they always prefer ChillZone",  # text from a review, not the user
            user_message="Can you find someone to service my AC this week?",
        )

    assert store.list_for_user(USER) == []


def test_quote_match_ignores_case_and_spacing(store: MemoryStore) -> None:
    m = store.remember(
        USER, "earliest_start", "10:00", quote="NEVER   before 10AM", user_message=SAID
    )

    assert m.status == "active"


def test_blank_quote_is_not_evidence(store: MemoryStore) -> None:
    with pytest.raises(ProvenanceError):
        store.remember(USER, "earliest_start", "10:00", quote="  ", user_message=SAID)


# --- typed values -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "value", "scope", "topic"),
    [
        ("earliest_start", "25:00", "global", None),
        ("budget_max_inr", -5, "plumber", None),
        ("preferred_provider", {"provider_id": "ele_sparks", "name": "Sparks"}, "global", None),
        ("note", "loves filter coffee", "global", None),  # notes need a topic
        ("favourite_colour", "blue", "global", None),  # not in the vocabulary
        ("budget_max_inr", 2000, "gardening", None),  # unknown service scope
    ],
)
def test_invalid_memories_are_rejected(
    store: MemoryStore, key: str, value: object, scope: str, topic: str | None
) -> None:
    remember: Any = store.remember  # deliberately bypass typing: this feeds bad input
    with pytest.raises((ValidationError, ValueError)):
        remember(
            USER, key, value, scope=scope, topic=topic, quote="remember", user_message="remember"
        )


# --- one active fact per key and scope ----------------------------------------------------


def test_saying_it_again_supersedes_the_old_fact(store: MemoryStore) -> None:
    first = remember_start(store, "10:00")
    second = store.remember(
        USER,
        "earliest_start",
        "09:00",
        quote="never before 9am",
        user_message="Actually never before 9am works.",
    )

    old = store.get(first.memory_id)
    assert old.status == "superseded" and old.superseded_by == second.memory_id
    assert [m.memory_id for m in store.list_for_user(USER)] == [second.memory_id]


def test_database_allows_one_active_fact_per_key_and_scope(
    store: MemoryStore, conn: sqlite3.Connection
) -> None:
    remember_start(store)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO memories (memory_id, user_id, key, scope, value, source, status,"
            " created_at, updated_at) VALUES ('mem_x', ?, 'earliest_start', 'global', '\"08:00\"',"
            " 'explicit', 'active', 'now', 'now')",
            (USER,),
        )


def test_scoped_preferences_coexist(store: MemoryStore) -> None:
    said = "My electrician is Sparks and my plumber is FlowRight."
    store.remember(
        USER,
        "preferred_provider",
        {"provider_id": "ele_sparks", "name": "Sparks"},
        scope="electrician",
        quote="My electrician is Sparks",
        user_message=said,
    )
    store.remember(
        USER,
        "preferred_provider",
        {"provider_id": "plm_flowright", "name": "FlowRight"},
        scope="plumber",
        quote="my plumber is FlowRight",
        user_message=said,
    )

    assert {m.scope for m in store.list_for_user(USER)} == {"electrician", "plumber"}


def test_notes_do_not_supersede_each_other(store: MemoryStore) -> None:
    said = "I'm vegetarian and I travel to Pune a lot."
    store.remember(
        USER, "note", "vegetarian", topic="food", quote="I'm vegetarian", user_message=said
    )
    store.remember(
        USER,
        "note",
        "travels to Pune often",
        topic="travel",
        quote="travel to Pune a lot",
        user_message=said,
    )

    assert len(store.list_for_user(USER)) == 2


# --- inferred memories wait for confirmation ----------------------------------------------


def infer_sparks(store: MemoryStore):  # type: ignore[no-untyped-def]
    return store.propose_inferred(
        USER,
        "preferred_provider",
        {"provider_id": "ele_sparks", "name": "Sparks Electricals"},
        scope="electrician",
        reason="2 completed bookings with Sparks Electricals",
    )


def test_inferred_fact_starts_pending(store: MemoryStore) -> None:
    m = infer_sparks(store)

    assert m is not None and (m.source, m.status) == ("inferred", "pending_confirmation")
    assert m.evidence == "2 completed bookings with Sparks Electricals"


def test_confirmed_inference_becomes_active_but_stays_inferred(store: MemoryStore) -> None:
    pending = infer_sparks(store)
    assert pending is not None

    confirmed = store.confirm(pending.memory_id)

    assert (confirmed.source, confirmed.status) == ("inferred", "active")
    assert confirmed.confirmed_at == NOW


def test_rejected_inference_is_not_suggested_again(store: MemoryStore) -> None:
    pending = infer_sparks(store)
    assert pending is not None
    store.reject(pending.memory_id)

    assert infer_sparks(store) is None


def test_duplicate_inference_is_not_proposed_twice(store: MemoryStore) -> None:
    assert infer_sparks(store) is not None
    assert infer_sparks(store) is None


# --- forget and correct -------------------------------------------------------------------


def test_forget_wipes_the_value_and_evidence(store: MemoryStore) -> None:
    m = remember_start(store)

    store.forget(m.memory_id)

    tombstone = store.get(m.memory_id)
    assert (tombstone.status, tombstone.value, tombstone.evidence) == ("forgotten", None, None)
    assert tombstone.forgotten_at == NOW
    assert store.list_for_user(USER) == []


def test_forgetting_blocks_re_inference(store: MemoryStore) -> None:
    pending = infer_sparks(store)
    assert pending is not None
    store.forget(pending.memory_id)

    assert infer_sparks(store) is None


def test_correct_replaces_the_value_as_an_explicit_fact(store: MemoryStore) -> None:
    pending = infer_sparks(store)
    assert pending is not None
    said = "No, my electrician is BrightWire now."

    corrected = store.correct(
        pending.memory_id,
        {"provider_id": "ele_brightwire", "name": "BrightWire"},
        quote="my electrician is BrightWire",
        user_message=said,
    )

    assert (corrected.source, corrected.status, corrected.scope) == (
        "explicit",
        "active",
        "electrician",
    )
    assert store.get(pending.memory_id).status == "superseded"


def test_inspect_lists_active_and_pending_only(store: MemoryStore) -> None:
    active = remember_start(store)
    pending = infer_sparks(store)
    assert pending is not None
    gone = store.remember(
        USER, "note", "vegetarian", topic="food", quote="vegetarian", user_message="I'm vegetarian"
    )
    store.forget(gone.memory_id)

    listed = {m.memory_id for m in store.list_for_user(USER)}

    assert listed == {active.memory_id, pending.memory_id}


# --- tracing ------------------------------------------------------------------------------


def test_memory_changes_are_traced(conn: sqlite3.Connection, clock: FakeClock) -> None:
    events: list[TraceEvent] = []
    run = Tracer(clock, SequentialIdGen(), subscribers=[events.append]).start_run(
        session_id="sess_1", trigger="test"
    )
    store = MemoryStore(conn, clock=clock, ids=SequentialIdGen())

    m = store.remember(
        USER, "earliest_start", "10:00", quote="never before 10am", user_message=SAID, run=run
    )
    store.forget(m.memory_id, run=run)

    traced = [e.to_dict() for e in events if e.type == "memory"]
    assert [(t["action"], t["key"], t["status"]) for t in traced] == [
        ("remembered", "earliest_start", "active"),
        ("forgotten", "earliest_start", "forgotten"),
    ]
    assert "10:00" not in str(traced[-1])  # a forget trace doesn't repeat the value
