"""Provider contract models: the data that crosses the ToolEnv boundary.

What: Frozen pydantic models for search hits, provider details, holds and
      bookings, plus one discriminated result union per tool.

Why:  Every later layer codes against these shapes, so they should make wrong
      states impossible rather than merely unlikely.

How:  - Results are discriminated unions on `status`: `Booked` always carries a
        booking, `BookFailed` never does, and pydantic picks the class from the
        status value. Flat "status + optional fields" shapes would allow nonsense
        such as `status="booked", booking=None`.
      - Prices are whole rupees (`int`): the workflow compares them exactly when
        deciding whether an approval still holds, and floats compare unreliably.
      - Datetimes must be timezone-aware (`AwareDatetime`), matching the clock.
      - Models are frozen and reject unknown fields, so results can't be edited
        after they arrive and contract drift fails loudly.
      - Free text written by providers or reviewers is `UntrustedText`
        (`Annotated[str, Untrusted]`). It behaves as a plain string; the marker
        lets the agent layer route such fields to the quarantined review reader
        instead of the orchestrator (Phase 5).
      - `price_changed` is deliberately *not* a provider status: a provider just
        quotes a price at hold time; comparing it with what the user approved is
        the workflow's job.
"""

from datetime import date
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class Untrusted:
    """Annotation marker: this text came from a provider or reviewer, not the user."""


UntrustedText = Annotated[str, Untrusted]

Service = Literal["plumber", "electrician", "ac_servicing", "cleaning", "carpenter"]
BookingStatus = Literal["confirmed", "cancelled", "cancelled_by_provider", "completed"]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --- inputs -------------------------------------------------------------------------


class DateRange(_Model):
    """Inclusive range of calendar dates."""

    start: date
    end: date

    @model_validator(mode="after")
    def _ordered(self) -> "DateRange":
        if self.end < self.start:
            raise ValueError("date_range end must not be before start")
        return self


# --- entities -----------------------------------------------------------------------


class Attributes(_Model):
    verified: bool
    years_experience: int
    languages: list[str]


class ProviderSummary(_Model):
    provider_id: str
    name: str
    service: Service
    area: str
    rating: float
    review_count: int
    price_inr: int
    attributes: Attributes
    available_slots: list[AwareDatetime]


class Review(_Model):
    author: str
    rating: int
    posted_on: date
    text: UntrustedText


class ProviderDetails(ProviderSummary):
    description: UntrustedText
    reviews: list[Review]


class Hold(_Model):
    hold_id: str
    provider_id: str
    slot: AwareDatetime
    quoted_price_inr: int
    expires_at: AwareDatetime


class Booking(_Model):
    booking_id: str
    user_id: str
    provider_id: str
    provider_name: str
    service: Service
    slot: AwareDatetime
    price_inr: int
    status: BookingStatus


# --- results (discriminated on `status`) ---------------------------------------------


class SearchOk(_Model):
    status: Literal["ok"] = "ok"
    providers: list[ProviderSummary]


class SearchFailed(_Model):
    status: Literal["unknown_service", "unknown_area"]


SearchResult = Annotated[SearchOk | SearchFailed, Field(discriminator="status")]


class ProviderOk(_Model):
    status: Literal["ok"] = "ok"
    provider: ProviderDetails


class ProviderFailed(_Model):
    status: Literal["provider_not_found"]


ProviderResult = Annotated[ProviderOk | ProviderFailed, Field(discriminator="status")]


class Held(_Model):
    status: Literal["held"] = "held"
    hold: Hold


class HoldFailed(_Model):
    status: Literal["slot_unavailable", "provider_not_found"]


HoldResult = Annotated[Held | HoldFailed, Field(discriminator="status")]


class Booked(_Model):
    status: Literal["booked"] = "booked"
    booking: Booking


class BookFailed(_Model):
    status: Literal["slot_taken", "hold_expired", "hold_not_found", "idempotency_key_reused"]


BookResult = Annotated[Booked | BookFailed, Field(discriminator="status")]


class StatusBooked(_Model):
    status: Literal["booked"] = "booked"
    booking: Booking


class StatusNotBooked(_Model):
    status: Literal["not_booked"] = "not_booked"


class StatusUnknown(_Model):
    status: Literal["unknown_reference"] = "unknown_reference"


BookingStatusResult = Annotated[
    StatusBooked | StatusNotBooked | StatusUnknown, Field(discriminator="status")
]


class Cancelled(_Model):
    status: Literal["cancelled"] = "cancelled"
    booking: Booking


class CancelFailed(_Model):
    status: Literal["booking_not_found", "not_cancellable"]


CancelResult = Annotated[Cancelled | CancelFailed, Field(discriminator="status")]


class Rescheduled(_Model):
    status: Literal["rescheduled"] = "rescheduled"
    booking: Booking


class RescheduleFailed(_Model):
    status: Literal["slot_conflict", "booking_not_found", "not_reschedulable"]


RescheduleResult = Annotated[Rescheduled | RescheduleFailed, Field(discriminator="status")]


# --- helpers ---------------------------------------------------------------------------


def booking_reference(
    *, hold_id: str | None, idempotency_key: str | None
) -> tuple[Literal["hold_id", "idempotency_key"], str]:
    """Validate get_booking_status arguments: exactly one reference must be given.

    Passing both or neither is a programming error, not a provider outcome, so it
    raises instead of returning `unknown_reference`.
    """
    if (hold_id is None) == (idempotency_key is None):
        raise ValueError("pass exactly one of hold_id or idempotency_key")
    if hold_id is not None:
        return ("hold_id", hold_id)
    assert idempotency_key is not None
    return ("idempotency_key", idempotency_key)
