import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from errand_agent.core.clock import FakeClock
from errand_agent.core.ids import SequentialIdGen
from errand_agent.memory.conversation import ConversationStore
from errand_agent.memory.profile import ProfileStore
from errand_agent.memory.seed import seed_demo_user
from errand_agent.memory.seedgen import OUTPUT, build_seed
from errand_agent.memory.store import MemoryStore
from errand_agent.mocks.seed import load_seed
from errand_agent.store.db import connect
from errand_agent.store.migrate import migrate, migrations_for
from errand_agent.workflow.approvals import ApprovalStore
from errand_agent.workflow.context import ContextBudget, ContextBuilder
from errand_agent.workflow.errands import ErrandStore
from errand_agent.workflow.requirements import Requirements

NOW = datetime(2026, 11, 5, 3, 30, tzinfo=UTC)
U = "user_1"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(NOW)


@pytest.fixture
def conn(tmp_path: Path, clock: FakeClock) -> sqlite3.Connection:
    c = connect(tmp_path / "agent.db")
    migrate(c, migrations_for("agent"), clock)
    return c


@pytest.fixture
def seeded(conn: sqlite3.Connection, clock: FakeClock) -> sqlite3.Connection:
    assert seed_demo_user(conn, clock) is True
    return conn


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.casefold())


# --- the generator --------------------------------------------------------------------


def test_committed_seed_matches_its_generator() -> None:
    assert json.loads(OUTPUT.read_text()) == build_seed(), (
        "demo_user.json is stale: run `make seed-data`"
    )


def test_seed_meets_the_assignment_minimums() -> None:
    seed = build_seed()

    assert len(seed["memories"]) >= 50
    assert sum(len(s["messages"]) for s in seed["sessions"]) >= 200


# --- the seed tells one consistent story (BUG-2) --------------------------------------------

# Words that state a booking's status or schedule. Bookings have a source of truth (the
# provider's records, via tools); the seeded transcript must not compete with it.
BOOKING_CLAIM = re.compile(
    r"\b(confirmed|booked|scheduled|finished|last booking|booking was)\b", re.IGNORECASE
)


def where_said(seed: dict[str, Any]) -> dict[str, tuple[int, int, int]]:
    """For each quoted fact: (session position, message position, session days_ago)
    of the first user message that says it. Sessions are listed oldest first."""
    found: dict[str, tuple[int, int, int]] = {}
    for s_pos, session in enumerate(seed["sessions"]):
        for m_pos, message in enumerate(session["messages"]):
            if message["role"] != "user":
                continue
            for fact in seed["memories"]:
                quote = fact.get("quote")
                if quote and quote in message["content"] and fact["id"] not in found:
                    found[fact["id"]] = (s_pos, m_pos, session["days_ago"])
    return found


def test_facts_are_said_when_they_were_learned() -> None:
    seed = build_seed()
    said = where_said(seed)
    facts = {f["id"]: f for f in seed["memories"]}

    for fact_id, (_, _, session_days_ago) in said.items():  # in the nearest session
        assert abs(session_days_ago - facts[fact_id]["days_ago"]) <= 5, fact_id
    for fact in seed["memories"]:  # a correction comes after what it corrects
        if "replaces" in fact:
            old, new = said[fact["replaces"]][:2], said[fact["id"]][:2]
            assert old < new, f"{fact['replaces']} is said after {fact['id']}"


def test_inferred_reasons_match_the_booking_history() -> None:
    history = load_seed().history

    for fact in build_seed()["memories"]:
        if fact.get("source") != "inferred":
            continue
        claimed = re.match(r"(\d+) completed bookings with ", fact["reason"])
        assert claimed, fact["id"]
        completed = [
            b
            for b in history
            if b.provider_id == fact["value"]["provider_id"] and b.status == "completed"
        ]
        assert len(completed) == int(claimed.group(1)), fact["id"]


def test_the_seeded_assistant_makes_no_booking_claims() -> None:
    claims = [
        m["content"]
        for s in build_seed()["sessions"]
        for m in s["messages"]
        if m["role"] == "assistant" and BOOKING_CLAIM.search(m["content"])
    ]

    assert claims == []


# --- loading ------------------------------------------------------------------------------


def test_seed_loads_once(seeded: sqlite3.Connection, clock: FakeClock) -> None:
    assert seeded.execute("SELECT count(*) FROM memories").fetchone()[0] == 55
    assert seeded.execute("SELECT count(*) FROM messages").fetchone()[0] == 200
    assert seed_demo_user(seeded, clock) is False  # already there: nothing added
    assert seeded.execute("SELECT count(*) FROM memories").fetchone()[0] == 55


def test_profile_is_loaded_structured(seeded: sqlite3.Connection, clock: FakeClock) -> None:
    p = ProfileStore(seeded, clock=clock, ids=SequentialIdGen()).get(U)

    assert (p.name, p.phone, p.email) == ("Asha Rao", "+919999900001", "asha.rao@example.com")
    assert [(a.label, a.area, a.is_default) for a in p.addresses] == [
        ("Home", "HSR Layout", True),
        ("Parents", "Jayanagar", False),
    ]


# --- the seed follows our own rules --------------------------------------------------------


def test_every_explicit_fact_is_backed_by_the_users_words(seeded: sqlite3.Connection) -> None:
    said = [norm(r[0]) for r in seeded.execute("SELECT content FROM messages WHERE role = 'user'")]
    rows = seeded.execute(
        "SELECT memory_id, evidence FROM memories"
        " WHERE source = 'explicit' AND evidence IS NOT NULL"
    ).fetchall()

    assert rows
    for memory_id, evidence in rows:
        assert any(norm(evidence) in s for s in said), memory_id


def test_forgotten_facts_are_tombstones_only(seeded: sqlite3.Connection) -> None:
    rows = seeded.execute(
        "SELECT value, evidence, forgotten_at FROM memories WHERE status = 'forgotten'"
    ).fetchall()

    assert len(rows) == 3
    assert all(r["value"] is None and r["evidence"] is None and r["forgotten_at"] for r in rows)


def test_no_pii_in_the_seeded_history(seeded: sqlite3.Connection) -> None:
    text = " ".join(r[0] for r in seeded.execute("SELECT content FROM messages"))

    for pii in ("Asha", "Rao", "99999", "example.com", "17th Main", "Temple Street"):
        assert pii not in text, pii


def test_one_active_fact_per_key_and_scope(seeded: sqlite3.Connection) -> None:
    dupes = seeded.execute(
        "SELECT key, scope, count(*) FROM memories WHERE status = 'active' AND key != 'note'"
        " GROUP BY key, scope HAVING count(*) > 1"
    ).fetchall()

    assert dupes == []


# --- the context builder over a lived-in user ------------------------------------------------


def test_context_for_a_plumber_errand_over_the_seeded_user(
    seeded: sqlite3.Connection, clock: FakeClock
) -> None:
    ids = SequentialIdGen()
    errands = ErrandStore(seeded, clock=clock, ids=ids)
    errands.create_session("sess_now", user_id=U)
    errand = errands.create("sess_now", "book")
    errands.set_requirements(errand.errand_id, Requirements(service="plumber"))
    builder = ContextBuilder(
        clock=clock,
        errands=errands,
        approvals=ApprovalStore(seeded, clock=clock, ids=ids),
        memories=MemoryStore(seeded, clock=clock, ids=ids),
        profiles=ProfileStore(seeded, clock=clock, ids=ids),
        conversation=ConversationStore(seeded, clock=clock, ids=ids),
        budget=ContextBudget(total=2500, memories=600),
    )

    p = builder.build(
        user_id=U,
        session_id="sess_now",
        errand_id=errand.errand_id,
        system="You are an errand assistant.",
        run=None,
    )

    assert p.tokens["total"] <= 2500
    assert p.messages_dropped > 0  # the long history doesn't all fit
    assert "Earliest start: 10:00" in p.memories and "09:00" not in p.memories
    assert "Budget for plumber: up to ₹1500" in p.memories and "1200" not in p.memories
    assert "ring the bell twice" in p.memories
    assert "BreezeFix" not in p.memories  # an AC preference: irrelevant to a plumber
    assert "vegetarian" not in p.memories  # food: irrelevant
    reasons = set(dict(p.excluded_memories).values())
    assert {"superseded", "forgotten", "pending_confirmation", "rejected", "irrelevant"} <= reasons
