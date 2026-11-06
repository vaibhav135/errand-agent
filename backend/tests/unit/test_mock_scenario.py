from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from errand_agent.core.clock import IST, FakeClock
from errand_agent.mocks.scenario import (
    BookWeights,
    Scenario,
    SlotRef,
    decide_book_outcome,
    is_slot_conflict,
    quoted_price,
    random_book_outcome,
)
from errand_agent.store.db import connect, transaction
from errand_agent.store.migrate import migrate, migrations_for

CLOCK = FakeClock(datetime(2026, 11, 5, 3, 30, tzinfo=UTC))


def open_world(path: Path):
    conn = connect(path)
    migrate(conn, migrations_for("mock_world"), CLOCK)
    return conn


# --- config ---------------------------------------------------------------------------


def test_defaults_match_the_tool_contract() -> None:
    s = Scenario()

    assert s.seed == 42
    assert s.book_outcomes == []
    assert s.book_weights == BookWeights(
        success=0.70, slot_taken=0.15, timeout_booked=0.075, timeout_not_booked=0.075
    )
    assert s.cancel_transient_failures == 1
    assert s.timeout_seconds == 8.0


def test_weights_must_sum_to_one_and_be_non_negative() -> None:
    with pytest.raises(ValidationError):
        BookWeights(success=0.7, slot_taken=0.2, timeout_booked=0.1, timeout_not_booked=0.1)
    with pytest.raises(ValidationError):
        BookWeights(success=1.1, slot_taken=-0.1, timeout_booked=0.0, timeout_not_booked=0.0)


def test_scenario_loads_from_plain_data() -> None:
    # Eval scenarios are written as data; the model must accept JSON-shaped input.
    s = Scenario.model_validate(
        {
            "seed": 7,
            "book_outcomes": ["timeout_booked", "success"],
            "price_drift": {"plm_quickfix": 0.12},
            "slot_conflicts": [
                {"provider_id": "ac_coolbreeze", "slot": "2026-11-07T10:00:00+05:30"}
            ],
        }
    )

    assert s.book_outcomes == ["timeout_booked", "success"]
    assert s.slot_conflicts[0].slot.utcoffset().total_seconds() == 5.5 * 3600  # type: ignore[union-attr]


def test_unknown_outcome_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Scenario(book_outcomes=["exploded"])  # type: ignore[list-item]


# --- random outcomes ----------------------------------------------------------------------


def test_random_outcome_is_deterministic_per_hold_and_attempt() -> None:
    s = Scenario()

    assert random_book_outcome(s, "hold_1", 0) == random_book_outcome(s, "hold_1", 0)
    outcomes = {random_book_outcome(s, f"hold_{i}", 0) for i in range(50)}
    assert len(outcomes) > 1  # not stuck on one value


def test_retry_attempts_get_fresh_draws() -> None:
    s = Scenario()

    differs = sum(
        random_book_outcome(s, f"hold_{i}", 0) != random_book_outcome(s, f"hold_{i}", 1)
        for i in range(200)
    )

    assert differs > 50


def test_random_outcomes_follow_the_contract_distribution() -> None:
    s = Scenario()
    n = 4000

    counts = Counter(random_book_outcome(s, f"hold_{i}", 0) for i in range(n))

    for outcome, expected in s.book_weights.model_dump().items():
        assert abs(counts[outcome] / n - expected) < 0.025, (outcome, counts[outcome] / n)


def test_forced_weights_always_pick_that_outcome() -> None:
    s = Scenario(
        book_weights=BookWeights(
            success=0.0, slot_taken=0.0, timeout_booked=1.0, timeout_not_booked=0.0
        )
    )

    assert {random_book_outcome(s, f"hold_{i}", 0) for i in range(100)} == {"timeout_booked"}


# --- scripted outcomes ------------------------------------------------------------------------


def test_scripted_outcomes_are_used_in_order_then_fall_back_to_random(tmp_path: Path) -> None:
    s = Scenario(book_outcomes=["timeout_booked", "slot_taken"])
    conn = open_world(tmp_path / "w.db")

    with transaction(conn):
        first = decide_book_outcome(conn, s, "hold_a", 0)
        second = decide_book_outcome(conn, s, "hold_b", 0)
        third = decide_book_outcome(conn, s, "hold_c", 0)

    assert (first, second) == ("timeout_booked", "slot_taken")
    assert third == random_book_outcome(s, "hold_c", 0)


def test_script_position_survives_a_restart(tmp_path: Path) -> None:
    s = Scenario(book_outcomes=["timeout_booked", "slot_taken", "success"])
    db = tmp_path / "w.db"

    before = open_world(db)
    with transaction(before):
        assert decide_book_outcome(before, s, "hold_a", 0) == "timeout_booked"
    before.close()

    after = open_world(db)  # a new "process": fresh connection, same file
    with transaction(after):
        assert decide_book_outcome(after, s, "hold_b", 0) == "slot_taken"


def test_scripted_outcome_is_not_consumed_if_the_transaction_rolls_back(tmp_path: Path) -> None:
    s = Scenario(book_outcomes=["timeout_booked", "success"])
    conn = open_world(tmp_path / "w.db")

    with pytest.raises(RuntimeError), transaction(conn):
        decide_book_outcome(conn, s, "hold_a", 0)
        raise RuntimeError("book failed before committing")

    with transaction(conn):
        assert decide_book_outcome(conn, s, "hold_a", 0) == "timeout_booked"


# --- price drift & conflicts --------------------------------------------------------------------


def test_quoted_price_without_drift_is_the_base_price() -> None:
    assert quoted_price(Scenario(), "plm_quickfix", 699) == 699


@pytest.mark.parametrize(
    ("drift", "base", "expected"),
    [(0.12, 1450, 1624), (-0.10, 1200, 1080), (0.10, 699, 769)],
)
def test_quoted_price_applies_drift_in_whole_rupees(drift: float, base: int, expected: int) -> None:
    s = Scenario(price_drift={"prov": drift})

    price = quoted_price(s, "prov", base)

    assert price == expected
    assert isinstance(price, int)


def test_drift_cannot_make_a_price_non_positive() -> None:
    with pytest.raises(ValidationError):
        Scenario(price_drift={"prov": -1.0})


def test_slot_conflict_matches_the_same_instant_in_any_timezone() -> None:
    ist_slot = datetime(2026, 11, 7, 10, 0, tzinfo=IST)
    s = Scenario(slot_conflicts=[SlotRef(provider_id="ac_coolbreeze", slot=ist_slot)])

    assert is_slot_conflict(s, "ac_coolbreeze", ist_slot)
    assert is_slot_conflict(s, "ac_coolbreeze", datetime(2026, 11, 7, 4, 30, tzinfo=UTC))
    assert not is_slot_conflict(s, "ac_other", ist_slot)
    assert not is_slot_conflict(s, "ac_coolbreeze", ist_slot.replace(hour=11))


def test_decide_book_outcome_requires_a_transaction(tmp_path: Path) -> None:
    # Consuming a scripted outcome must commit or roll back with the booking itself.
    conn = open_world(tmp_path / "w.db")

    with pytest.raises(RuntimeError, match="transaction"):
        decide_book_outcome(conn, Scenario(), "hold_a", 0)
