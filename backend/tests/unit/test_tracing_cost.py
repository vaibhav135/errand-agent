from datetime import date

import pytest

from errand_agent.tracing.cost import PRICES, ModelPrice, estimate_cost

TODAY = date(2026, 11, 5)

TABLE = {
    "test:flat": [ModelPrice(input_per_mtok=3.0, output_per_mtok=15.0)],
    "test:rising": [
        ModelPrice(input_per_mtok=2.0, output_per_mtok=10.0),
        ModelPrice(input_per_mtok=4.0, output_per_mtok=20.0, effective_from=date(2026, 12, 1)),
    ],
    "test:future-only": [
        ModelPrice(input_per_mtok=1.0, output_per_mtok=1.0, effective_from=date(2027, 6, 1)),
    ],
    "openai:test-model": [ModelPrice(input_per_mtok=1.0, output_per_mtok=5.0)],
}


def cost(model: str, on: date = TODAY, inp: int = 1_000_000, out: int = 200_000) -> float | None:
    return estimate_cost(model, input_tokens=inp, output_tokens=out, on=on, prices=TABLE)


def test_estimate_cost_from_price_table() -> None:
    assert cost("test:flat") == pytest.approx(3.0 + 3.0)  # 1M x $3 + 0.2M x $15


def test_unknown_model_is_none() -> None:
    assert cost("nope:model") is None


def test_price_in_effect_on_the_given_date_is_used() -> None:
    assert cost("test:rising", on=date(2026, 11, 30)) == pytest.approx(2.0 + 2.0)
    assert cost("test:rising", on=date(2026, 12, 1)) == pytest.approx(4.0 + 4.0)  # inclusive
    assert cost("test:rising", on=date(2027, 3, 1)) == pytest.approx(4.0 + 4.0)


def test_price_list_order_does_not_matter() -> None:
    reversed_table = {"test:rising": list(reversed(TABLE["test:rising"]))}

    result = estimate_cost(
        "test:rising",
        input_tokens=1_000_000,
        output_tokens=0,
        on=date(2026, 11, 30),
        prices=reversed_table,
    )

    assert result == pytest.approx(2.0)


def test_only_future_prices_means_unknown() -> None:
    assert cost("test:future-only", on=TODAY) is None


@pytest.mark.parametrize("alias", ["openai-chat:test-model", "openai-responses:test-model"])
def test_openai_prefix_aliases_share_openai_prices(alias: str) -> None:
    assert cost(alias) == cost("openai:test-model")


def test_real_price_table_is_well_formed() -> None:
    # Guards hand edits to PRICES: sane values, one price per start date, and
    # every model priced today (no model whose only entry is in the future).
    for model, entries in PRICES.items():
        assert ":" in model, model
        starts = [e.effective_from for e in entries]
        assert len(starts) == len(set(starts)), f"{model}: duplicate effective_from"
        for e in entries:
            assert e.input_per_mtok >= 0 and e.output_per_mtok >= 0, model
        assert estimate_cost(model, input_tokens=1, output_tokens=1, on=TODAY) is not None, model
