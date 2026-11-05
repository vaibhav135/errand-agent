"""Scenario: configures how the mock world behaves, reproducibly.

What: `Scenario` (seed, scripted `book` outcomes, outcome weights, per-provider
      price drift, slot conflicts, transient cancel failures, timeout length) and
      the decision helpers the mock tools use: `decide_book_outcome`,
      `quoted_price`, `is_slot_conflict`.

Why:  The contract asks for "seeded randomness or configurable scenarios so tests
      are reproducible". We use both: by default outcomes are keyed-random with
      the contract's distribution (70% success, 15% slot taken, 15% timeout of
      which half actually booked); a test or eval can *script* the exact failure
      it exercises.

How:  - Random outcomes: `keyed_unit(seed, "book", hold_id, attempt)` against
        cumulative weights, so they don't depend on call order; a retry
        (attempt + 1) gets a fresh draw.
      - Scripted outcomes are consumed in order, then it falls back to random.
        The scenario is passed in by whoever builds the mock; only the queue
        position lives in mock_world.db (`scenario_state`), written inside the
        caller's transaction. So a restart continues the script, and a rolled-
        back `book` does not consume an outcome.
      - Price drift is a fraction per provider (+0.12 = 12% higher quote at hold
        time), rounded half-up to whole rupees with Decimal.
      - Slot conflicts compare instants, so IST and UTC spellings match.
"""

import sqlite3
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from errand_agent.mocks.rand import keyed_unit

BookOutcome = Literal["success", "slot_taken", "timeout_booked", "timeout_not_booked"]

_BOOK_CURSOR = "book_cursor"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class BookWeights(_Frozen):
    success: float = Field(default=0.70, ge=0)
    slot_taken: float = Field(default=0.15, ge=0)
    timeout_booked: float = Field(default=0.075, ge=0)
    timeout_not_booked: float = Field(default=0.075, ge=0)

    @model_validator(mode="after")
    def _sums_to_one(self) -> "BookWeights":
        total = self.success + self.slot_taken + self.timeout_booked + self.timeout_not_booked
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"book weights must sum to 1, got {total}")
        return self


class SlotRef(_Frozen):
    provider_id: str
    slot: AwareDatetime


class Scenario(_Frozen):
    seed: int = 42
    book_outcomes: list[BookOutcome] = Field(default_factory=list)
    book_weights: BookWeights = Field(default_factory=BookWeights)
    price_drift: dict[str, float] = Field(default_factory=dict)
    slot_conflicts: list[SlotRef] = Field(default_factory=list)
    cancel_transient_failures: int = Field(default=1, ge=0)
    timeout_seconds: float = Field(default=8.0, ge=0)

    @field_validator("price_drift")
    @classmethod
    def _drift_keeps_price_positive(cls, value: dict[str, float]) -> dict[str, float]:
        for provider_id, drift in value.items():
            if drift <= -1.0:
                raise ValueError(f"price drift for {provider_id} would make the price <= 0")
        return value


def random_book_outcome(scenario: Scenario, hold_id: str, attempt: int) -> BookOutcome:
    u = keyed_unit(scenario.seed, "book", hold_id, attempt)
    cumulative = 0.0
    weights = scenario.book_weights
    for outcome, weight in (
        ("success", weights.success),
        ("slot_taken", weights.slot_taken),
        ("timeout_booked", weights.timeout_booked),
        ("timeout_not_booked", weights.timeout_not_booked),
    ):
        cumulative += weight
        if u < cumulative:
            return outcome  # type: ignore[return-value]
    return "timeout_not_booked"  # floating-point edge: u just below 1.0


def decide_book_outcome(
    conn: sqlite3.Connection, scenario: Scenario, hold_id: str, attempt: int
) -> BookOutcome:
    """Next scripted outcome if any remain, else keyed-random. Needs a transaction."""
    if not conn.in_transaction:
        raise RuntimeError("decide_book_outcome must run inside the caller's transaction")
    row = conn.execute("SELECT value FROM scenario_state WHERE key = ?", (_BOOK_CURSOR,)).fetchone()
    cursor = row["value"] if row else 0
    if cursor < len(scenario.book_outcomes):
        conn.execute(
            "INSERT INTO scenario_state (key, value) VALUES (?, ?)"
            " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (_BOOK_CURSOR, cursor + 1),
        )
        return scenario.book_outcomes[cursor]
    return random_book_outcome(scenario, hold_id, attempt)


def quoted_price(scenario: Scenario, provider_id: str, base_price_inr: int) -> int:
    drift = scenario.price_drift.get(provider_id, 0.0)
    exact = Decimal(base_price_inr) * (Decimal(1) + Decimal(str(drift)))
    return int(exact.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def is_slot_conflict(scenario: Scenario, provider_id: str, slot: datetime) -> bool:
    return any(c.provider_id == provider_id and c.slot == slot for c in scenario.slot_conflicts)
