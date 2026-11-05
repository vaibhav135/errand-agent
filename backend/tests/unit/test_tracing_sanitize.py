from datetime import UTC, datetime

import pytest

from errand_agent.core.clock import FakeClock
from errand_agent.core.ids import SequentialIdGen
from errand_agent.tracing.cost import ModelPrice, estimate_cost
from errand_agent.tracing.recorder import TraceEvent, Tracer
from errand_agent.tracing.sanitize import REDACTED, sanitize

# --- redaction ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "api_key",
        "API_KEY",
        "anthropic_api_key",
        "apikey",
        "password",
        "db-password",
        "authorization",
        "Authorization",
        "client_secret",
        "access_token",
        "refresh_token",
    ],
)
def test_secret_keys_are_redacted(key: str) -> None:
    assert sanitize({key: "sk-live-123"}) == {key: REDACTED}


@pytest.mark.parametrize("key", ["input_tokens", "output_tokens", "token_count", "tokens"])
def test_token_counts_are_not_mistaken_for_secrets(key: str) -> None:
    assert sanitize({key: 1234}) == {key: 1234}


def test_redaction_is_recursive_through_dicts_and_lists() -> None:
    value = {"args": {"headers": [{"authorization": "Bearer x", "accept": "json"}]}}

    assert sanitize(value) == {"args": {"headers": [{"authorization": REDACTED, "accept": "json"}]}}


# --- truncation ---------------------------------------------------------------------


def test_long_strings_are_truncated_with_marker() -> None:
    review = "a" * 600

    result = sanitize({"review": review}, max_str=500)

    assert result["review"] == "a" * 500 + "…[+100 chars]"


def test_short_strings_and_scalars_pass_through() -> None:
    value = {"s": "ok", "n": 1, "f": 1.5, "b": True, "none": None}

    assert sanitize(value) == value


def test_tuples_become_lists_and_input_is_not_mutated() -> None:
    original = {"slots": ("09:00", "10:00"), "nested": {"api_key": "x"}}

    result = sanitize(original)

    assert result == {"slots": ["09:00", "10:00"], "nested": {"api_key": REDACTED}}
    assert original["nested"]["api_key"] == "x"


def test_datetimes_are_left_for_the_writer() -> None:
    ts = datetime(2026, 11, 5, 9, 0, tzinfo=UTC)

    assert sanitize({"expires_at": ts}) == {"expires_at": ts}


# --- recorder applies it automatically ----------------------------------------------


def test_recorder_sanitizes_every_payload() -> None:
    events: list[TraceEvent] = []
    tracer = Tracer(
        FakeClock(datetime(2026, 11, 5, 9, 0, tzinfo=UTC)), SequentialIdGen(), [events.append]
    )
    run = tracer.start_run(session_id="s", trigger="user_message")

    with run.span("tool_call", tool="get_provider", args={"api_key": "sk-1"}) as span:
        span.set(result={"review": "x" * 2000})

    data = events[-1].to_dict()
    assert data["args"] == {"api_key": REDACTED}
    assert data["result"]["review"].endswith("…[+1500 chars]")


# --- cost ---------------------------------------------------------------------------

PRICES = {"test:model": ModelPrice(input_per_mtok=3.0, output_per_mtok=15.0)}


def test_estimate_cost_from_price_table() -> None:
    cost = estimate_cost("test:model", input_tokens=1_000_000, output_tokens=200_000, prices=PRICES)

    assert cost == pytest.approx(3.0 + 3.0)


def test_estimate_cost_unknown_model_is_none() -> None:
    assert estimate_cost("nope:model", input_tokens=10, output_tokens=10, prices=PRICES) is None
