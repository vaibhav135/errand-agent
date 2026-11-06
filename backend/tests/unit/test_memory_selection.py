import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from errand_agent.core.clock import FakeClock
from errand_agent.core.ids import SequentialIdGen
from errand_agent.memory.selection import select_memories
from errand_agent.memory.store import MemoryStore
from errand_agent.memory.tokens import estimate_tokens
from errand_agent.store.db import connect
from errand_agent.store.migrate import migrate, migrations_for

NOW = datetime(2026, 11, 5, 3, 30, tzinfo=UTC)
U = "user_1"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(NOW)


@pytest.fixture
def store(tmp_path: Path, clock: FakeClock) -> MemoryStore:
    conn: sqlite3.Connection = connect(tmp_path / "agent.db")
    migrate(conn, migrations_for("agent"), clock)
    return MemoryStore(conn, clock=clock, ids=SequentialIdGen())


def say(store: MemoryStore, key, value, quote: str, **kw):  # type: ignore[no-untyped-def]
    return store.remember(U, key, value, quote=quote, user_message=quote, **kw)


# --- token estimate ---------------------------------------------------------------


def test_token_estimate_is_deterministic_and_pessimistic() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcdefg") == 2  # 7 chars / 3.5
    assert estimate_tokens("x" * 400) == 115  # ~4 chars/token is typical: we overestimate


# --- what never reaches the model -------------------------------------------------


def test_only_active_memories_are_included(store: MemoryStore) -> None:
    active = say(store, "earliest_start", "10:00", "never before 10am")
    forgotten = say(store, "latest_start", "18:00", "nothing after 6pm")
    store.forget(forgotten.memory_id)
    pending = store.propose_inferred(
        U,
        "preferred_provider",
        {"provider_id": "ele_sparks", "name": "Sparks Electricals"},
        scope="electrician",
        reason="2 bookings",
    )
    old = say(store, "budget_max_inr", 1500, "plumbers under 1500", scope="plumber")
    say(store, "budget_max_inr", 1200, "plumbers under 1200 now", scope="plumber")
    assert pending is not None

    sel = select_memories(store.all_for_user(U), service="electrician", budget_tokens=600)

    included = {m.memory_id for m in sel.included}
    reasons = dict(sel.excluded)
    assert active.memory_id in included
    assert reasons[forgotten.memory_id] == "forgotten"
    assert reasons[pending.memory_id] == "pending_confirmation"
    assert reasons[old.memory_id] == "superseded"


def test_excluded_values_never_appear_in_the_text(store: MemoryStore) -> None:
    forgotten = say(store, "note", "allergic to cats", "allergic to cats", topic="home_services")
    store.forget(forgotten.memory_id)
    store.propose_inferred(
        U,
        "preferred_provider",
        {"provider_id": "ele_sparks", "name": "Sparks Electricals"},
        scope="electrician",
        reason="2 bookings",
    )

    sel = select_memories(store.all_for_user(U), service="electrician", budget_tokens=600)

    assert "cats" not in sel.text and "Sparks" not in sel.text


# --- relevance -------------------------------------------------------------------------


def test_other_services_and_off_topic_notes_are_irrelevant(store: MemoryStore) -> None:
    said = "My electrician is Sparks and my plumber is FlowRight."
    elec = say(
        store,
        "preferred_provider",
        {"provider_id": "ele_sparks", "name": "Sparks"},
        "My electrician is Sparks",
        scope="electrician",
    )
    plum = store.remember(
        U,
        "preferred_provider",
        {"provider_id": "plm_flowright", "name": "FlowRight"},
        scope="plumber",
        quote="my plumber is FlowRight",
        user_message=said,
    )
    food = say(store, "note", "vegetarian", "I'm vegetarian", topic="food")
    home = say(
        store, "note", "dog at home, ring twice", "dog at home, ring twice", topic="home_services"
    )

    sel = select_memories(store.all_for_user(U), service="plumber", budget_tokens=600)

    assert {m.memory_id for m in sel.included} == {plum.memory_id, home.memory_id}
    assert dict(sel.excluded) == {elec.memory_id: "irrelevant", food.memory_id: "irrelevant"}


def test_unknown_service_includes_only_global_preferences(store: MemoryStore) -> None:
    start = say(store, "earliest_start", "10:00", "never before 10am")
    say(store, "budget_max_inr", 1500, "plumbers under 1500", scope="plumber")

    sel = select_memories(store.all_for_user(U), service=None, budget_tokens=600)

    assert [m.memory_id for m in sel.included] == [start.memory_id]


# --- rendering and budget ------------------------------------------------------------------


def test_rendering_says_where_each_fact_came_from(store: MemoryStore) -> None:
    say(store, "earliest_start", "10:00", "never before 10am")
    pending = store.propose_inferred(
        U,
        "preferred_provider",
        {"provider_id": "ele_sparks", "name": "Sparks Electricals"},
        scope="electrician",
        reason="2 bookings",
    )
    assert pending is not None
    store.confirm(pending.memory_id)

    text = select_memories(store.all_for_user(U), service="electrician", budget_tokens=600).text

    assert "- Earliest start: 10:00 (you told me)" in text
    assert "- Usual electrician: Sparks Electricals (inferred, you confirmed)" in text


def test_budget_keeps_the_most_important_memories(store: MemoryStore) -> None:
    pref = say(
        store,
        "preferred_provider",
        {"provider_id": "plm_flowright", "name": "FlowRight"},
        "my plumber is FlowRight",
        scope="plumber",
    )
    notes = [
        say(
            store,
            "note",
            f"home note number {i} " + "x" * 80,
            f"home note number {i} " + "x" * 80,
            topic="home_services",
        )
        for i in range(10)
    ]

    sel = select_memories(store.all_for_user(U), service="plumber", budget_tokens=60)

    assert sel.included[0].memory_id == pref.memory_id  # highest priority goes first
    assert sel.tokens <= 60
    over = [mid for mid, reason in sel.excluded if reason == "over_budget"]
    assert over and set(over) <= {n.memory_id for n in notes}


def test_selection_is_deterministic(store: MemoryStore) -> None:
    for i in range(5):
        say(store, "note", f"note {i}", f"note {i}", topic="home_services")

    a = select_memories(store.all_for_user(U), service="plumber", budget_tokens=600)
    b = select_memories(list(reversed(store.all_for_user(U))), service="plumber", budget_tokens=600)

    assert a == b
