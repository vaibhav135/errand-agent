from datetime import date, time

import pytest
from pydantic import ValidationError

from errand_agent.providers.models import MAX_DATE_RANGE_DAYS, DateRange
from errand_agent.workflow.requirements import (
    Requirements,
    RequirementsPatch,
    TimeWindow,
    date_problems,
    merge,
    missing_required,
    unasked_optional,
)

TODAY = date(2026, 11, 5)
THIS_WEEK = DateRange(start=date(2026, 11, 5), end=date(2026, 11, 8))

FULL = Requirements(
    service="plumber",
    area="Indiranagar",
    date_range=THIS_WEEK,
    time_window=TimeWindow(earliest=time(8), latest=time(12)),
    budget_max_inr=1500,
)


# --- what to ask --------------------------------------------------------------------------


def test_empty_requirements_need_everything() -> None:
    r = Requirements()

    assert missing_required(r) == ["service", "area", "date_range"]
    assert unasked_optional(r) == ["time_window", "budget_max_inr"]


def test_fully_specified_request_asks_nothing() -> None:
    # "Find a plumber for my kitchen tap this week, mornings only, under ₹1,500."
    assert missing_required(FULL) == []
    assert unasked_optional(FULL) == []


def test_no_preference_counts_as_answered() -> None:
    r = Requirements(
        service="plumber",
        area="HSR Layout",
        date_range=THIS_WEEK,
        time_window="any",
        budget_max_inr="any",
    )

    assert unasked_optional(r) == []


def test_preferred_provider_is_never_asked_for() -> None:
    assert "preferred_provider_id" not in unasked_optional(Requirements())


# --- corrections merge ------------------------------------------------------------------------


def test_correcting_one_field_keeps_the_rest() -> None:
    corrected = merge(FULL, RequirementsPatch(area="HSR Layout"))

    assert corrected.area == "HSR Layout"
    assert corrected.model_dump(exclude={"area"}) == FULL.model_dump(exclude={"area"})


def test_patch_can_explicitly_clear_a_field() -> None:
    cleared = merge(FULL, RequirementsPatch(budget_max_inr=None))

    assert cleared.budget_max_inr is None
    assert unasked_optional(cleared) == ["budget_max_inr"]


def test_changing_service_clears_the_preferred_provider() -> None:
    with_pref = merge(FULL, RequirementsPatch(preferred_provider_id="plm_flowright"))

    switched = merge(with_pref, RequirementsPatch(service="electrician"))
    same = merge(with_pref, RequirementsPatch(service="plumber"))
    both = merge(
        with_pref, RequirementsPatch(service="electrician", preferred_provider_id="ele_sparks")
    )

    assert switched.preferred_provider_id is None
    assert same.preferred_provider_id == "plm_flowright"
    assert both.preferred_provider_id == "ele_sparks"


def test_merge_does_not_mutate_the_original() -> None:
    merge(FULL, RequirementsPatch(area="Whitefield"))

    assert FULL.area == "Indiranagar"


# --- validation -------------------------------------------------------------------------------


def test_time_window_must_be_ordered_and_non_empty() -> None:
    TimeWindow(earliest=time(10))  # "not before 10 a.m." on its own is fine

    with pytest.raises(ValidationError):
        TimeWindow(earliest=time(12), latest=time(8))
    with pytest.raises(ValidationError):
        TimeWindow()


def test_budget_must_be_positive_whole_rupees() -> None:
    with pytest.raises(ValidationError):
        Requirements(budget_max_inr=0)
    with pytest.raises(ValidationError):
        Requirements(budget_max_inr=1499.5)  # type: ignore[arg-type]


def test_unknown_fields_are_rejected() -> None:
    # The model fills these from tool calls; a hallucinated field must fail loudly.
    with pytest.raises(ValidationError):
        RequirementsPatch.model_validate({"area": "Indiranagar", "max_price": 1500})


def test_unknown_service_is_rejected() -> None:
    with pytest.raises(ValidationError):
        RequirementsPatch.model_validate({"service": "gardener"})


@pytest.mark.parametrize(
    ("date_range", "problem"),
    [
        (DateRange(start=date(2026, 11, 4), end=date(2026, 11, 6)), "past"),
        (
            DateRange(start=date(2026, 11, 5), end=date(2026, 11, 5 + MAX_DATE_RANGE_DAYS)),
            f"{MAX_DATE_RANGE_DAYS} days",
        ),
    ],
)
def test_date_problems_are_explained(date_range: DateRange, problem: str) -> None:
    problems = date_problems(date_range, today=TODAY)

    assert len(problems) == 1 and problem in problems[0]


def test_valid_date_range_has_no_problems() -> None:
    assert date_problems(THIS_WEEK, today=TODAY) == []
    assert date_problems(DateRange(start=TODAY, end=TODAY), today=TODAY) == []
