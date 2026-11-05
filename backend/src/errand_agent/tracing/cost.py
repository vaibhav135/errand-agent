"""Cost estimation: turn token usage into an estimated USD cost per model call.

What: `estimate_cost(model, input_tokens, output_tokens, on=date, prices=PRICES)`
      returns USD, or None when no price is known for that model on that date.

Why:  The trace should show "estimated cost where available". None (not 0) for
      unknown models lets the recorder mark totals `cost_complete: false`
      instead of under-reporting. Providers announce dated price changes, so a
      price is only correct for a date range.

How:  - `PRICES` maps a PydanticAI model string to a list of `ModelPrice`, each
        with an optional `effective_from` (None = no start date). The entry with
        the latest start on or before `on` applies; if all start later, None.
      - `on` is required so callers pass the injected clock's date, never the
        real one.
      - `openai-chat:` / `openai-responses:` aliases resolve to `openai:`.
      - Standard short-context, first-party rates. Long-context tiers (Gemini
        3.1 Pro above 200K, OpenAI above 272K, Haiku 5.5 above 100K) are not
        modelled: our context budget stays far below them.
      - Sources, checked 2026-11-05: Claude API pricing reference (cached
        2026-10-06); developers.openai.com/api/docs/pricing;
        aistudio.google.com/docs/pricing. Tests use their own tables, so they
        never depend on these values.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class ModelPrice:
    input_per_mtok: float
    output_per_mtok: float
    effective_from: date | None = None


def _p(inp: float, out: float, since: date | None = None) -> ModelPrice:
    return ModelPrice(input_per_mtok=inp, output_per_mtok=out, effective_from=since)


PRICES: Mapping[str, Sequence[ModelPrice]] = {
    # Anthropic
    "anthropic:claude-fable-5-1": [_p(10.00, 50.00)],
    "anthropic:claude-opus-5-5": [_p(4.00, 20.00)],
    "anthropic:claude-sonnet-5-5": [_p(2.00, 10.00)],
    "anthropic:claude-haiku-5-5": [_p(0.10, 0.50)],
    # OpenAI (GPT-6 family)
    "openai:gpt-6-astra": [_p(10.00, 50.00)],
    "openai:gpt-6.1-sol": [_p(2.00, 10.00)],
    "openai:gpt-6.1-terra": [_p(1.00, 5.00)],
    "openai:gpt-6-luna": [_p(0.10, 0.50)],
    # Google Gemini (prices only; the google extra is not installed yet)
    "google:gemini-4-argon": [_p(2.00, 10.00), _p(4.00, 20.00, date(2026, 12, 1))],
    "google:gemini-3.8-flash": [_p(0.75, 3.75), _p(1.50, 7.50, date(2027, 1, 1))],
    "google:gemini-3.1-pro-preview": [_p(2.00, 12.00)],
    "google:gemini-3.5-flash-lite": [_p(0.30, 2.50)],
}

_PREFIX_ALIASES = {"openai-chat": "openai", "openai-responses": "openai"}


def _canonical(model: str) -> str:
    provider, sep, name = model.partition(":")
    return f"{_PREFIX_ALIASES.get(provider, provider)}{sep}{name}"


def _price_on(entries: Sequence[ModelPrice], on: date) -> ModelPrice | None:
    in_effect = [e for e in entries if e.effective_from is None or e.effective_from <= on]
    if not in_effect:
        return None
    return max(in_effect, key=lambda e: e.effective_from or date.min)


def estimate_cost(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    on: date,
    prices: Mapping[str, Sequence[ModelPrice]] = PRICES,
) -> float | None:
    entries = prices.get(_canonical(model))
    price = _price_on(entries, on) if entries else None
    if price is None:
        return None
    return (input_tokens * price.input_per_mtok + output_tokens * price.output_per_mtok) / 1_000_000
