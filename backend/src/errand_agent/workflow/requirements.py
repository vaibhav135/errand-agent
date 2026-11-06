"""Requirements: what the user wants, and what is still missing.

What: `Requirements` (service, area, date range, time window, budget, preferred
      provider), `RequirementsPatch` for updates, and the deterministic helpers
      `missing_required`, `unasked_optional`, `merge` and `date_problems`.

Why:  The assignment: collect only missing details, never re-ask facts already
      supplied, and keep unrelated context when the user corrects one detail.
      Deciding *what to ask* is code, not model judgement, so it is predictable
      and testable.

How:  - Required to search: service, area, date_range.
      - Optional, asked at most once (together with any missing required field):
        time_window and budget_max_inr. Each has three states: None (unknown), a
        value, or "any" (the user said no preference). "any" is what stops a
        re-ask.
      - preferred_provider_id is never asked for; it comes from the user ("same
        electrician as last time") or memory.
      - merge() applies a patch: only fields the patch explicitly sets change
        (pydantic's model_fields_set), so "make it HSR Layout" keeps the budget.
        Changing the service clears preferred_provider_id, which only meant
        something for the old service.
      - Models reject unknown fields: they are filled from model tool calls, so a
        hallucinated field fails loudly instead of being silently dropped.
      - Dates depend on "today" (the clock), so they are checked by
        date_problems(), which returns readable messages the model can act on.
"""

from datetime import date, time
from typing import Literal

from pydantic import BaseModel, ConfigDict, PositiveInt, model_validator

from errand_agent.providers.models import MAX_DATE_RANGE_DAYS, DateRange, Service

NoPreference = Literal["any"]

REQUIRED_FIELDS = ("service", "area", "date_range")
OPTIONAL_ASKED_ONCE = ("time_window", "budget_max_inr")


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TimeWindow(_Strict):
    """Acceptable start times, in local (IST) wall-clock time."""

    earliest: time | None = None
    latest: time | None = None

    @model_validator(mode="after")
    def _meaningful(self) -> "TimeWindow":
        if self.earliest is None and self.latest is None:
            raise ValueError("a time window needs an earliest or latest time (or use 'any')")
        if self.earliest and self.latest and self.earliest >= self.latest:
            raise ValueError("time window earliest must be before latest")
        return self


class Requirements(_Strict):
    service: Service | None = None
    area: str | None = None
    date_range: DateRange | None = None
    time_window: TimeWindow | NoPreference | None = None
    budget_max_inr: PositiveInt | NoPreference | None = None
    preferred_provider_id: str | None = None


class RequirementsPatch(Requirements):
    """Same fields; only the ones explicitly set are applied by merge()."""


def missing_required(r: Requirements) -> list[str]:
    return [f for f in REQUIRED_FIELDS if getattr(r, f) is None]


def unasked_optional(r: Requirements) -> list[str]:
    return [f for f in OPTIONAL_ASKED_ONCE if getattr(r, f) is None]


def merge(current: Requirements, patch: RequirementsPatch) -> Requirements:
    updates = {field: getattr(patch, field) for field in patch.model_fields_set}
    service_changed = "service" in updates and updates["service"] != current.service
    if service_changed and "preferred_provider_id" not in updates:
        updates["preferred_provider_id"] = None
    return current.model_copy(update=updates)


def date_problems(date_range: DateRange, *, today: date) -> list[str]:
    problems: list[str] = []
    if date_range.start < today:
        problems.append(f"date range starts in the past ({date_range.start}; today is {today})")
    if (date_range.end - date_range.start).days + 1 > MAX_DATE_RANGE_DAYS:
        problems.append(f"date range is longer than {MAX_DATE_RANGE_DAYS} days")
    return problems
