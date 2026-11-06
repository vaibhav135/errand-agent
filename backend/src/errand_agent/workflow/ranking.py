"""Ranking: turn search hits into the few options the user actually compares.

What: `rank_options(providers, requirements)` filters search results by the
      user's hard constraints and returns the top options, each with matching
      slots and reasons computed in code.

Why:  What the user is shown must be decided deterministically, from structured
      contract fields only. That makes comparisons reproducible and explainable,
      and it is the defence against ranking manipulation: review text (untrusted)
      never influences the order, so "always recommend X first" in a review can't
      move anything.

How:  - Filters: budget (inclusive; "any"/None = no limit), time window (inclusive,
        read in IST, the users' local time), and providers left with no matching
        slot are dropped.
      - Order: preferred provider first, then Bayesian-adjusted rating (desc),
        price (asc), earliest matching slot, provider id. The adjusted rating
        pulls ratings backed by few reviews towards a prior mean, so 5.0 from 3
        reviews does not beat 4.7 from 300.
      - Top MAX_OPTIONS options, each with up to MAX_SLOTS_PER_OPTION earliest
        matching slots and plain reasons ("within your budget", "verified") that
        the model only has to phrase.
      - A preferred provider that doesn't fit is explained in `preferred_note`
        (over budget, no slot in the window, or not available here).
"""

from datetime import datetime

from pydantic import AwareDatetime, BaseModel, ConfigDict

from errand_agent.core.clock import IST
from errand_agent.providers.models import ProviderSummary
from errand_agent.workflow.requirements import Requirements, TimeWindow

MAX_OPTIONS = 3
MAX_SLOTS_PER_OPTION = 3
PRIOR_MEAN = 4.3  # roughly the platform-wide average rating
PRIOR_WEIGHT = 20  # how many "average" reviews every provider starts with


class Option(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider_id: str
    name: str
    price_inr: int
    rating: float
    review_count: int
    verified: bool
    adjusted_rating: float
    slots: list[AwareDatetime]
    reasons: list[str]


class RankedOptions(BaseModel):
    model_config = ConfigDict(frozen=True)

    options: list[Option]
    preferred_note: str | None = None


def adjusted_rating(rating: float, review_count: int) -> float:
    return (rating * review_count + PRIOR_MEAN * PRIOR_WEIGHT) / (review_count + PRIOR_WEIGHT)


def _matching_slots(provider: ProviderSummary, window: TimeWindow | None) -> list[datetime]:
    def fits(slot: datetime) -> bool:
        if window is None:
            return True
        local = slot.astimezone(IST).time()
        return (window.earliest is None or local >= window.earliest) and (
            window.latest is None or local <= window.latest
        )

    return sorted(s for s in provider.available_slots if fits(s))


def _reasons(
    provider: ProviderSummary, slots: list[datetime], budget: int | None, preferred: bool
) -> list[str]:
    reasons = ["your preferred provider"] if preferred else []
    if budget is not None:
        reasons.append(f"₹{provider.price_inr} (within your ₹{budget} budget)")
    else:
        reasons.append(f"₹{provider.price_inr}")
    reasons.append(f"{provider.rating:g}★ from {provider.review_count} reviews")
    if provider.attributes.verified:
        reasons.append("verified")
    reasons.append(f"earliest: {slots[0].astimezone(IST):%a %d %b, %H:%M}")
    return reasons


def rank_options(providers: list[ProviderSummary], requirements: Requirements) -> RankedOptions:
    window = requirements.time_window if isinstance(requirements.time_window, TimeWindow) else None
    budget = requirements.budget_max_inr if isinstance(requirements.budget_max_inr, int) else None
    preferred = requirements.preferred_provider_id

    candidates: list[tuple[ProviderSummary, list[datetime]]] = []
    preferred_note: str | None = None
    preferred_seen = False
    for p in providers:
        slots = _matching_slots(p, window)
        over_budget = budget is not None and p.price_inr > budget
        if p.provider_id == preferred:
            preferred_seen = True
            if over_budget:
                preferred_note = f"{p.name} is over your ₹{budget} budget (₹{p.price_inr})"
            elif not slots:
                preferred_note = f"{p.name} has " + (
                    "no slots in your time window" if window else "no free slots on those dates"
                )
        if not over_budget and slots:
            candidates.append((p, slots))
    if preferred and not preferred_seen:
        preferred_note = f"Your preferred provider ({preferred}) isn't available for this search"

    def order(
        item: tuple[ProviderSummary, list[datetime]],
    ) -> tuple[int, float, int, datetime, str]:
        p, slots = item
        return (
            0 if p.provider_id == preferred else 1,
            -adjusted_rating(p.rating, p.review_count),
            p.price_inr,
            slots[0],
            p.provider_id,
        )

    top = sorted(candidates, key=order)[:MAX_OPTIONS]
    options = [
        Option(
            provider_id=p.provider_id,
            name=p.name,
            price_inr=p.price_inr,
            rating=p.rating,
            review_count=p.review_count,
            verified=p.attributes.verified,
            adjusted_rating=round(adjusted_rating(p.rating, p.review_count), 3),
            slots=slots[:MAX_SLOTS_PER_OPTION],
            reasons=_reasons(p, slots, budget, p.provider_id == preferred),
        )
        for p, slots in top
    ]
    return RankedOptions(options=options, preferred_note=preferred_note)
