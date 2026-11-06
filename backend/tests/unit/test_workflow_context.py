import sqlite3
from datetime import UTC, date, datetime, time
from pathlib import Path

import pytest

from errand_agent.core.clock import IST, FakeClock
from errand_agent.core.ids import SequentialIdGen
from errand_agent.memory.conversation import ConversationStore
from errand_agent.memory.profile import ProfileStore
from errand_agent.memory.store import MemoryStore
from errand_agent.memory.tokens import estimate_tokens
from errand_agent.providers.models import DateRange
from errand_agent.store.db import connect
from errand_agent.store.migrate import migrate, migrations_for
from errand_agent.tracing.recorder import TraceEvent, Tracer
from errand_agent.workflow.approvals import ApprovalDetails, ApprovalStore
from errand_agent.workflow.context import ContextBudget, ContextBuilder, ContextOverflow
from errand_agent.workflow.errands import ErrandStore
from errand_agent.workflow.requirements import Requirements, TimeWindow

NOW = datetime(2026, 11, 5, 3, 30, tzinfo=UTC)  # Thursday 09:00 IST
U, S = "user_1", "sess_1"
SYSTEM = "You are an errand assistant."
INTRO = (
    "I'm Priya Sharma, phone +91 98450 12345, priya.sharma@example.com, 12, 4th Cross, HSR Layout."
)


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.clock = FakeClock(NOW)
        ids = SequentialIdGen()
        self.conn: sqlite3.Connection = connect(tmp_path / "agent.db")
        migrate(self.conn, migrations_for("agent"), self.clock)
        self.errands = ErrandStore(self.conn, clock=self.clock, ids=ids)
        self.errands.create_session(S, user_id=U)
        self.memories = MemoryStore(self.conn, clock=self.clock, ids=ids)
        self.profiles = ProfileStore(self.conn, clock=self.clock, ids=ids)
        self.convo = ConversationStore(self.conn, clock=self.clock, ids=ids)
        self.approvals = ApprovalStore(self.conn, clock=self.clock, ids=ids)
        self.events: list[TraceEvent] = []
        self.run = Tracer(self.clock, ids, subscribers=[self.events.append]).start_run(
            session_id=S, trigger="test"
        )

    def builder(self, budget: ContextBudget | None = None) -> ContextBuilder:
        return ContextBuilder(
            clock=self.clock,
            errands=self.errands,
            approvals=self.approvals,
            memories=self.memories,
            profiles=self.profiles,
            conversation=self.convo,
            budget=budget or ContextBudget(),
        )


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def packet_text(p) -> str:  # type: ignore[no-untyped-def]
    return "\n".join([p.system, p.state, p.memories, *(c for _, c in p.messages)])


def say_and_remember(env: Env, key, value, quote: str, **kw):  # type: ignore[no-untyped-def]
    env.convo.append(S, "user", quote)
    env.clock.advance(1)
    return env.memories.remember(U, key, value, quote=quote, user_message=quote, **kw)


# --- what must never be sent ---------------------------------------------------------------


def test_forgotten_and_pending_facts_never_reach_the_model(env: Env) -> None:
    forgotten = say_and_remember(
        env, "note", "allergic to cats", "I'm allergic to cats", topic="home_services"
    )
    env.memories.forget(forgotten.memory_id)
    env.memories.propose_inferred(
        U,
        "preferred_provider",
        {"provider_id": "ele_sparks", "name": "Sparks Electricals"},
        scope="electrician",
        reason="2 bookings",
    )
    errand = env.errands.create(S, "book")
    env.errands.set_requirements(errand.errand_id, Requirements(service="electrician"))

    p = env.builder().build(
        user_id=U, session_id=S, errand_id=errand.errand_id, system=SYSTEM, run=env.run
    )

    text = packet_text(p)
    assert "cats" not in text and "Sparks" not in text
    reasons = dict(p.excluded_memories)
    assert reasons[forgotten.memory_id] == "forgotten"
    assert "pending_confirmation" in reasons.values()


def test_profile_pii_never_reaches_the_model(env: Env) -> None:
    env.convo.append(S, "user", INTRO)
    env.profiles.update(
        U,
        name="Priya Sharma",
        phone="+91 98450 12345",
        email="priya.sharma@example.com",
        user_message=INTRO,
    )
    env.profiles.add_address(
        U, label="Home", line="12, 4th Cross", area="HSR Layout", user_message=INTRO
    )

    p = env.builder().build(user_id=U, session_id=S, errand_id=None, system=SYSTEM, run=env.run)

    text = packet_text(p)
    for pii in ("Priya", "Sharma", "98450", "12345", "example.com", "4th Cross"):
        assert pii not in text, pii
    assert "Home (HSR Layout, default)" in p.state  # what the model *may* know


# --- the budget ------------------------------------------------------------------------------


def test_long_history_is_cut_to_the_budget_newest_kept_in_order(env: Env) -> None:
    for i in range(500):
        env.convo.append(
            S, "user" if i % 2 == 0 else "assistant", f"message number {i} " + "x" * 60
        )
        env.clock.advance(1)

    p = env.builder(ContextBudget(total=2000, memories=200)).build(
        user_id=U, session_id=S, errand_id=None, system=SYSTEM, run=env.run
    )

    assert p.tokens["total"] <= 2000
    contents = [c for _, c in p.messages]
    assert contents[-1].startswith("message number 499 ")  # newest kept
    numbers = [int(c.split()[2]) for c in contents]
    assert numbers == sorted(numbers)  # chronological in the packet
    assert p.messages_dropped == 500 - len(contents) > 0


def test_memory_section_has_its_own_cap(env: Env) -> None:
    for i in range(30):
        say_and_remember(
            env,
            "note",
            f"home note {i} " + "y" * 60,
            f"home note {i} " + "y" * 60,
            topic="home_services",
        )

    p = env.builder(ContextBudget(total=6000, memories=100)).build(
        user_id=U, session_id=S, errand_id=None, system=SYSTEM, run=env.run
    )

    assert p.tokens["memories"] <= 100
    assert "over_budget" in dict(p.excluded_memories).values()


def test_system_and_state_that_cannot_fit_is_an_error(env: Env) -> None:
    with pytest.raises(ContextOverflow):
        env.builder(ContextBudget(total=5, memories=1)).build(
            user_id=U, session_id=S, errand_id=None, system=SYSTEM * 10, run=env.run
        )


# --- the state section -------------------------------------------------------------------------


def test_state_shows_requirements_missing_fields_approval_and_today(env: Env) -> None:
    errand = env.errands.create(S, "book")
    env.errands.set_requirements(
        errand.errand_id,
        Requirements(
            service="plumber",
            time_window=TimeWindow(earliest=time(8), latest=time(12)),
            budget_max_inr=1500,
        ),
    )
    env.errands.transition(errand.errand_id, "options_ready")
    env.approvals.request(
        errand.errand_id,
        ApprovalDetails(
            action="book",
            provider_id="plm_quickfix",
            provider_name="QuickFix Plumbing",
            slot=datetime(2026, 11, 7, 9, 0, tzinfo=IST),
            price_inr=699,
        ),
    )

    state = (
        env.builder()
        .build(user_id=U, session_id=S, errand_id=errand.errand_id, system=SYSTEM, run=env.run)
        .state
    )

    assert "Today: Thu 05 Nov 2026 (IST)" in state
    assert "Errand: book, state comparing" in state
    assert "service: plumber" in state and "time window: 08:00-12:00" in state
    assert "budget: up to ₹1500" in state
    assert "Still needed: area, date_range" in state
    assert "Pending approval: QuickFix Plumbing, Sat 07 Nov, 09:00, ₹699" in state


def test_any_preference_is_shown_as_answered(env: Env) -> None:
    errand = env.errands.create(S, "book")
    env.errands.set_requirements(
        errand.errand_id,
        Requirements(
            service="plumber",
            area="HSR Layout",
            date_range=DateRange(start=date(2026, 11, 5), end=date(2026, 11, 8)),
            time_window="any",
            budget_max_inr="any",
        ),
    )

    state = (
        env.builder()
        .build(user_id=U, session_id=S, errand_id=errand.errand_id, system=SYSTEM, run=env.run)
        .state
    )

    assert "time window: any" in state and "budget: any" in state
    assert "Still needed" not in state and "Can ask once" not in state


# --- logged every turn ----------------------------------------------------------------------------


def test_context_sent_is_logged_with_reasons(env: Env) -> None:
    kept = say_and_remember(env, "earliest_start", "10:00", "never before 10am")
    gone = say_and_remember(env, "note", "vegetarian", "I'm vegetarian", topic="food")

    p = env.builder().build(user_id=U, session_id=S, errand_id=None, system=SYSTEM, run=env.run)

    [event] = [e.to_dict() for e in env.events if e.type == "context_sent"]
    assert event["budget"] == {"total": 6000, "memories": 600}
    assert event["tokens"] == p.tokens
    assert event["memory_ids"] == [kept.memory_id]
    assert {"memory_id": gone.memory_id, "reason": "irrelevant"} in event["excluded_memories"]
    assert event["messages_included"] == len(p.messages)
    assert event["messages_dropped"] == 0


def test_build_is_deterministic(env: Env) -> None:
    say_and_remember(env, "earliest_start", "10:00", "never before 10am")

    a = env.builder().build(user_id=U, session_id=S, errand_id=None, system=SYSTEM, run=None)
    b = env.builder().build(user_id=U, session_id=S, errand_id=None, system=SYSTEM, run=None)

    assert a == b
    assert a.tokens["system"] == estimate_tokens(SYSTEM)
