import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from errand_agent.core.clock import FakeClock
from errand_agent.core.ids import SequentialIdGen
from errand_agent.store.db import connect, transaction
from errand_agent.store.migrate import migrate, migrations_for
from errand_agent.tracing.recorder import TraceEvent, Tracer
from errand_agent.workflow.approvals import (
    ApprovalDetails,
    ApprovalError,
    ApprovalStore,
    fingerprint,
    is_explicit_yes,
)
from errand_agent.workflow.errands import ErrandStore

NOW = datetime(2026, 11, 5, 3, 30, tzinfo=UTC)
IST = timezone(timedelta(hours=5, minutes=30))
SAT_9_IST = datetime(2026, 11, 7, 9, 0, tzinfo=IST)


def details(**overrides: object) -> ApprovalDetails:
    base: dict[str, object] = {
        "action": "book",
        "provider_id": "plm_quickfix",
        "provider_name": "QuickFix Plumbing",
        "slot": SAT_9_IST,
        "price_inr": 699,
    }
    return ApprovalDetails.model_validate({**base, **overrides})


# --- fingerprint --------------------------------------------------------------------------


def test_fingerprint_is_stable_and_hex() -> None:
    fp = fingerprint(details())

    assert fp == fingerprint(details())
    assert len(fp) == 64 and all(c in "0123456789abcdef" for c in fp)


@pytest.mark.parametrize(
    "change",
    [
        {"action": "reschedule", "booking_id": "bk_1"},
        {"provider_id": "plm_pipemasters"},
        {"slot": SAT_9_IST + timedelta(hours=1)},
        {"price_inr": 783},
        {"booking_id": "bk_2"},
    ],
)
def test_every_material_change_changes_the_fingerprint(change: dict[str, object]) -> None:
    assert fingerprint(details(**change)) != fingerprint(details())


def test_same_instant_in_another_timezone_is_the_same_agreement() -> None:
    as_utc = SAT_9_IST.astimezone(UTC)

    assert fingerprint(details(slot=as_utc)) == fingerprint(details())


def test_display_name_is_not_material() -> None:
    assert fingerprint(details(provider_name="QuickFix")) == fingerprint(details())


# --- store fixtures -----------------------------------------------------------------------


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(NOW)


@pytest.fixture
def conn(tmp_path: Path, clock: FakeClock) -> sqlite3.Connection:
    c = connect(tmp_path / "agent.db")
    migrate(c, migrations_for("agent"), clock)
    return c


@pytest.fixture
def events() -> list[TraceEvent]:
    return []


@pytest.fixture
def approvals(conn: sqlite3.Connection, clock: FakeClock) -> ApprovalStore:
    return ApprovalStore(conn, clock=clock, ids=SequentialIdGen())


@pytest.fixture
def errands(conn: sqlite3.Connection, clock: FakeClock) -> ErrandStore:
    store = ErrandStore(conn, clock=clock, ids=SequentialIdGen())
    store.create_session("sess_1", user_id="user_1")
    return store


@pytest.fixture
def errand_id(errands: ErrandStore) -> str:
    return errands.create("sess_1", "book").errand_id


# --- request / grant / reject -------------------------------------------------------------


def test_request_creates_a_pending_approval(approvals: ApprovalStore, errand_id: str) -> None:
    a = approvals.request(errand_id, details())

    assert a.approval_id == "appr_0001"
    assert a.status == "pending"
    assert a.fingerprint == fingerprint(details())
    assert approvals.get(a.approval_id) == a


def test_new_request_supersedes_the_live_one(approvals: ApprovalStore, errand_id: str) -> None:
    old = approvals.request(errand_id, details())

    new = approvals.request(errand_id, details(price_inr=783))

    assert approvals.get(old.approval_id).status == "invalidated"
    assert new.status == "pending"


def test_database_allows_one_live_approval_per_errand(
    approvals: ApprovalStore, conn: sqlite3.Connection, errand_id: str
) -> None:
    approvals.request(errand_id, details())

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO approvals (approval_id, errand_id, action, fingerprint, details,"
            " status, created_at) VALUES ('appr_x', ?, 'book', 'f', '{}', 'pending', 'now')",
            (errand_id,),
        )


def test_grant_requires_the_fingerprint_the_user_saw(
    approvals: ApprovalStore, errand_id: str
) -> None:
    a = approvals.request(errand_id, details())

    with pytest.raises(ApprovalError, match="changed"):
        approvals.grant(a.approval_id, presented_fingerprint="0" * 64, via="button")
    assert approvals.get(a.approval_id).status == "pending"

    granted = approvals.grant(a.approval_id, presented_fingerprint=a.fingerprint, via="button")
    assert granted.status == "granted" and granted.decided_via == "button"


def test_only_pending_approvals_can_be_granted(approvals: ApprovalStore, errand_id: str) -> None:
    a = approvals.request(errand_id, details())
    approvals.reject(a.approval_id)

    with pytest.raises(ApprovalError, match="rejected"):
        approvals.grant(a.approval_id, presented_fingerprint=a.fingerprint, via="button")


# --- consume (inside the executor's transaction) ------------------------------------------


def granted(approvals: ApprovalStore, errand_id: str) -> str:
    a = approvals.request(errand_id, details())
    approvals.grant(a.approval_id, presented_fingerprint=a.fingerprint, via="typed")
    return a.approval_id


def test_consume_succeeds_once(
    approvals: ApprovalStore, conn: sqlite3.Connection, errand_id: str
) -> None:
    approval_id = granted(approvals, errand_id)

    with transaction(conn):
        approvals.consume(approval_id, details())

    assert approvals.get(approval_id).status == "consumed"
    with pytest.raises(ApprovalError, match="consumed"), transaction(conn):
        approvals.consume(approval_id, details())  # a repeated confirmation can't run twice


def test_material_change_at_execution_is_refused_then_invalidated(
    approvals: ApprovalStore, conn: sqlite3.Connection, errand_id: str
) -> None:
    approval_id = granted(approvals, errand_id)

    # consume() only checks: anything it wrote would be rolled back with the caller's
    # transaction when it raises (the same trap as the mock's 503 counter).
    with pytest.raises(ApprovalError, match="no longer matches"), transaction(conn):
        approvals.consume(approval_id, details(price_inr=783))
    assert approvals.get(approval_id).status == "granted"

    # The executor then invalidates, as its own step, and asks the user again.
    approvals.invalidate_live(errand_id, reason="price changed")
    assert approvals.get(approval_id).status == "invalidated"


def test_consume_needs_a_granted_approval(
    approvals: ApprovalStore, conn: sqlite3.Connection, errand_id: str
) -> None:
    pending = approvals.request(errand_id, details())

    with pytest.raises(ApprovalError, match="pending"), transaction(conn):
        approvals.consume(pending.approval_id, details())


def test_consume_must_run_inside_the_callers_transaction(
    approvals: ApprovalStore, errand_id: str
) -> None:
    approval_id = granted(approvals, errand_id)

    with pytest.raises(RuntimeError, match="transaction"):
        approvals.consume(approval_id, details())


def test_rolled_back_consume_leaves_the_approval_granted(
    approvals: ApprovalStore, conn: sqlite3.Connection, errand_id: str
) -> None:
    approval_id = granted(approvals, errand_id)

    with pytest.raises(RuntimeError, match="boom"), transaction(conn):
        approvals.consume(approval_id, details())
        raise RuntimeError("boom: saving the idempotency key failed")

    assert approvals.get(approval_id).status == "granted"


# --- typed "yes" --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "yes",
        "Yes!",
        "  YES  ",
        "yes please",
        "confirm",
        "Confirmed.",
        "go ahead",
        "book it",
        "yes, book it",
        "approve",
        "do it",
    ],
)
def test_explicit_yes(text: str) -> None:
    assert is_explicit_yes(text)


@pytest.mark.parametrize(
    "text",
    [
        "yes but make it 11am",
        "ok",
        "sure",
        "maybe",
        "no",
        "not yes",
        "yes no",
        "what does it cost?",
        "",
        "yes?",
    ],
)
def test_not_an_explicit_yes(text: str) -> None:
    assert not is_explicit_yes(text)


def test_typed_yes_resolves_only_a_single_pending_approval(
    approvals: ApprovalStore, errands: ErrandStore, errand_id: str
) -> None:
    assert approvals.resolve_typed_yes("sess_1", "yes") is None  # nothing pending

    a = approvals.request(errand_id, details())
    assert approvals.resolve_typed_yes("sess_1", "yes") == a.approval_id
    assert approvals.resolve_typed_yes("sess_1", "yes but 11am") is None

    other = errands.create("sess_1", "cancel", booking_id="bk_1").errand_id
    approvals.request(other, details(action="cancel", booking_id="bk_1"))
    assert approvals.resolve_typed_yes("sess_1", "yes") is None  # ambiguous: two pending


# --- tracing ------------------------------------------------------------------------------


def test_approval_lifecycle_is_traced(
    approvals: ApprovalStore,
    conn: sqlite3.Connection,
    clock: FakeClock,
    events: list[TraceEvent],
    errand_id: str,
) -> None:
    run = Tracer(clock, SequentialIdGen(), subscribers=[events.append]).start_run(
        session_id="sess_1", trigger="test"
    )

    a = approvals.request(errand_id, details(), run=run)
    approvals.grant(a.approval_id, presented_fingerprint=a.fingerprint, via="button", run=run)
    with transaction(conn):
        approvals.consume(a.approval_id, details(), run=run)

    traced = [e.to_dict() for e in events if e.type == "approval"]
    assert [t["status"] for t in traced] == ["pending", "granted", "consumed"]
    assert all(
        t["approval_id"] == a.approval_id and t["fingerprint"] == a.fingerprint for t in traced
    )
