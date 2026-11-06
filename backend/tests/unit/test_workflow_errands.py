from collections import deque
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from errand_agent.core.clock import FakeClock
from errand_agent.core.ids import SequentialIdGen
from errand_agent.providers.models import DateRange
from errand_agent.store.db import connect, transaction
from errand_agent.store.migrate import migrate, migrations_for
from errand_agent.tracing.recorder import TraceEvent, Tracer
from errand_agent.workflow.errands import (
    ABANDONABLE,
    IN_FLIGHT,
    INITIAL_STATE,
    PARKED,
    TERMINAL,
    TRANSITIONS,
    ErrandStore,
    InvalidTransition,
    next_state,
    render_mermaid,
)
from errand_agent.workflow.requirements import Requirements

NOW = datetime(2026, 11, 5, 3, 30, tzinfo=UTC)
KINDS = list(TRANSITIONS)
ARCHITECTURE = Path(__file__).resolve().parents[3] / "docs" / "ARCHITECTURE.md"


def states_of(kind: str) -> set[str]:
    table = TRANSITIONS[kind]  # type: ignore[index]
    return {INITIAL_STATE} | {s for s, _ in table} | set(table.values())


def outgoing(kind: str, state: str) -> set[str]:
    return {e for (s, e) in TRANSITIONS[kind] if s == state}  # type: ignore[index]


# --- the table's structure ---------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_every_state_is_reachable_from_collecting(kind: str) -> None:
    seen, queue = {INITIAL_STATE}, deque([INITIAL_STATE])
    while queue:
        state = queue.popleft()
        for (s, _), nxt in TRANSITIONS[kind].items():  # type: ignore[index]
            if s == state and nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)

    assert seen == states_of(kind)


@pytest.mark.parametrize("kind", KINDS)
def test_terminal_states_have_no_exits(kind: str) -> None:
    for state in states_of(kind) & TERMINAL:
        assert outgoing(kind, state) == set(), (kind, state)


@pytest.mark.parametrize("kind", KINDS)
def test_only_safe_states_can_be_abandoned(kind: str) -> None:
    for state in states_of(kind) - TERMINAL:
        can_abandon = "abandon" in outgoing(kind, state)
        assert can_abandon == (state in ABANDONABLE), (kind, state)
    assert not (ABANDONABLE & (IN_FLIGHT | PARKED))


@pytest.mark.parametrize("kind", KINDS)
def test_every_non_terminal_state_has_a_way_out(kind: str) -> None:
    for state in states_of(kind) - TERMINAL:
        assert outgoing(kind, state), (kind, state)


def test_invalid_transition_is_refused_with_context() -> None:
    with pytest.raises(InvalidTransition, match=r"book.*comparing.*approved"):
        next_state("book", "comparing", "approved")  # can't book without a proposal


# --- paths through the table ---------------------------------------------------------------


def walk(kind: str, events: list[str]) -> str:
    state = INITIAL_STATE
    for event in events:
        state = next_state(kind, state, event)  # type: ignore[arg-type]
    return state


def test_book_happy_path() -> None:
    assert walk("book", ["options_ready", "option_proposed", "approved", "booked"]) == "booked"


def test_book_timeout_paths() -> None:
    to_booking = ["options_ready", "option_proposed", "approved", "timeout"]

    assert walk("book", [*to_booking, "confirmed"]) == "booked"
    assert walk("book", [*to_booking, "retry", "booked"]) == "booked"
    assert walk("book", [*to_booking, "unverifiable", "confirmed"]) == "booked"
    assert walk("book", [*to_booking, "unverifiable", "not_booked"]) == "comparing"


def test_price_change_requires_reapproval() -> None:
    path = ["options_ready", "option_proposed", "approved", "needs_reapproval"]

    assert walk("book", path) == "awaiting_approval"
    assert walk("book", ["options_ready", "option_proposed", "approval_renewed"]) == (
        "awaiting_approval"
    )


def test_cancel_paths() -> None:
    to_cancelling = ["option_proposed", "approved"]

    assert walk("cancel", [*to_cancelling, "cancelled"]) == "cancelled"
    assert walk("cancel", [*to_cancelling, "not_cancellable"]) == "cancel_failed"
    assert walk("cancel", [*to_cancelling, "retries_exhausted", "cancelled"]) == "cancelled"
    assert walk("cancel", [*to_cancelling, "retries_exhausted", "window_closed"]) == (
        "cancel_failed"
    )


def test_reschedule_paths() -> None:
    path = ["option_proposed", "approved"]

    assert walk("reschedule", [*path, "rescheduled"]) == "rescheduled"
    assert walk("reschedule", [*path, "slot_conflict"]) == "collecting"


# --- ErrandStore -------------------------------------------------------------------------


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(NOW)


@pytest.fixture
def events() -> list[TraceEvent]:
    return []


@pytest.fixture
def store(tmp_path: Path, clock: FakeClock) -> ErrandStore:
    conn = connect(tmp_path / "agent.db")
    migrate(conn, migrations_for("agent"), clock)
    s = ErrandStore(conn, clock=clock, ids=SequentialIdGen())
    s.create_session("sess_1", user_id="user_1")
    return s


@pytest.fixture
def tracer(clock: FakeClock, events: list[TraceEvent]) -> Tracer:
    return Tracer(clock, SequentialIdGen(), subscribers=[events.append])


def test_new_errand_starts_collecting(store: ErrandStore) -> None:
    errand = store.create("sess_1", "book")

    assert errand.errand_id == "errand_0001"
    assert errand.state == "collecting"
    assert errand.requirements == Requirements()
    assert store.get(errand.errand_id) == errand


def test_transition_persists_state_and_history(
    store: ErrandStore, tracer: Tracer, events: list[TraceEvent]
) -> None:
    errand = store.create("sess_1", "book")
    run = tracer.start_run(session_id="sess_1", trigger="user_message")

    updated = store.transition(errand.errand_id, "options_ready", run=run, reason="3 options")

    assert updated.state == "comparing"
    assert store.get(errand.errand_id).state == "comparing"
    assert store.history(errand.errand_id) == [
        ("collecting", "comparing", "options_ready", "3 options", run.run_id)
    ]
    traced = events[-1].to_dict()
    assert traced["type"] == "state_transition"
    assert (traced["errand_id"], traced["from"], traced["to"], traced["event"]) == (
        errand.errand_id,
        "collecting",
        "comparing",
        "options_ready",
    )


def test_invalid_transition_changes_nothing(store: ErrandStore) -> None:
    errand = store.create("sess_1", "book")

    with pytest.raises(InvalidTransition):
        store.transition(errand.errand_id, "approved")

    assert store.get(errand.errand_id).state == "collecting"
    assert store.history(errand.errand_id) == []


def test_requirements_round_trip(store: ErrandStore) -> None:
    errand = store.create("sess_1", "book")
    reqs = Requirements(
        service="plumber",
        area="Indiranagar",
        date_range=DateRange(start=date(2026, 11, 5), end=date(2026, 11, 8)),
        budget_max_inr="any",
    )

    store.set_requirements(errand.errand_id, reqs)

    assert store.get(errand.errand_id).requirements == reqs


def test_requirements_are_frozen_while_a_side_effect_is_in_flight(store: ErrandStore) -> None:
    errand = store.create("sess_1", "book")
    for event in ("options_ready", "option_proposed", "approved"):
        store.transition(errand.errand_id, event)  # type: ignore[arg-type]

    with pytest.raises(InvalidTransition, match="booking"):
        store.set_requirements(errand.errand_id, Requirements(service="electrician"))


def test_reschedule_and_cancel_errands_reference_a_booking(store: ErrandStore) -> None:
    errand = store.create("sess_1", "cancel", booking_id="bk_hist_coolbreeze")

    assert store.get(errand.errand_id).booking_id == "bk_hist_coolbreeze"


def test_open_errands_exclude_finished_ones(store: ErrandStore) -> None:
    done = store.create("sess_1", "book")
    store.transition(done.errand_id, "abandon")
    active = store.create("sess_1", "book")

    assert [e.errand_id for e in store.open_errands("sess_1")] == [active.errand_id]


# --- documentation stays in sync with the table ---------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_architecture_doc_shows_the_current_state_diagram(kind: str) -> None:
    assert render_mermaid(kind) in ARCHITECTURE.read_text(), (  # type: ignore[arg-type]
        f"docs/ARCHITECTURE.md is out of date for '{kind}': paste render_mermaid('{kind}')"
    )


def test_rolled_back_transition_is_neither_stored_nor_traced(
    store: ErrandStore, tracer: Tracer, events: list[TraceEvent], tmp_path: Path
) -> None:
    errand = store.create("sess_1", "book")
    run = tracer.start_run(session_id="sess_1", trigger="test")
    conn = store._conn

    with pytest.raises(RuntimeError), transaction(conn):
        store.transition(errand.errand_id, "options_ready", run=run)
        raise RuntimeError("a later step of the unit of work failed")

    assert store.get(errand.errand_id).state == "collecting"
    assert [e for e in events if e.type == "state_transition"] == []
