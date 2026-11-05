"""Cost estimation: turn token usage into an estimated USD cost per model call.

What: `estimate_cost(model, input_tokens, output_tokens, prices=PRICES)` returns
      USD, or None when the model has no known price.

Why:  The trace should show "estimated cost where available". None (not 0) for
      unknown models lets the recorder mark totals `cost_complete: false`
      instead of under-reporting.

How:  `PRICES` maps PydanticAI model strings to per-million-token prices.
      Source: Claude API pricing reference (cached 2026-10-06), first-party
      Anthropic API rates. Haiku 5.5's price applies to prompts up to 100K
      tokens (higher beyond); our context budget stays well below that.
      Add rows when another provider/model is chosen; tests pass their own
      table so they never depend on real prices.
"""

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelPrice:
    input_per_mtok: float
    output_per_mtok: float


PRICES: Mapping[str, ModelPrice] = {
    "anthropic:claude-opus-5-5": ModelPrice(input_per_mtok=4.00, output_per_mtok=20.00),
    "anthropic:claude-sonnet-5-5": ModelPrice(input_per_mtok=2.00, output_per_mtok=10.00),
    "anthropic:claude-haiku-5-5": ModelPrice(input_per_mtok=0.10, output_per_mtok=0.50),
    "anthropic:claude-fable-5-1": ModelPrice(input_per_mtok=10.00, output_per_mtok=50.00),
}


def estimate_cost(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    prices: Mapping[str, ModelPrice] = PRICES,
) -> float | None:
    price = prices.get(model)
    if price is None:
        return None
    return (input_tokens * price.input_per_mtok + output_tokens * price.output_per_mtok) / 1_000_000
