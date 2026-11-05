from datetime import UTC, date, datetime

import pytest
from pydantic import TypeAdapter, ValidationError

from errand_agent.providers.errors import ProviderError, ProviderTimeout, ProviderUnavailable
from errand_agent.providers.models import (
    Attributes,
    Booked,
    BookFailed,
    Booking,
    BookResult,
    DateRange,
    Hold,
    ProviderDetails,
    Review,
    Untrusted,
    booking_reference,
)

SLOT = datetime(2026, 11, 7, 9, 0, tzinfo=UTC)


def booking(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "booking_id": "bk_1",
        "user_id": "user_1",
        "provider_id": "prov_1",
        "provider_name": "Sparks Electric",
        "service": "electrician",
        "slot": SLOT,
        "price_inr": 1200,
        "status": "confirmed",
    }
    return {**data, **overrides}


# --- basic validation ----------------------------------------------------------------


def test_date_range_must_not_end_before_it_starts() -> None:
    DateRange(start=date(2026, 11, 5), end=date(2026, 11, 5))  # same day is fine

    with pytest.raises(ValidationError):
        DateRange(start=date(2026, 11, 6), end=date(2026, 11, 5))


def test_slots_must_be_timezone_aware() -> None:
    with pytest.raises(ValidationError):
        Hold(
            hold_id="hold_1",
            provider_id="prov_1",
            slot=datetime(2026, 11, 7, 9, 0),  # naive
            quoted_price_inr=1200,
            expires_at=SLOT,
        )


def test_unknown_service_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Booking.model_validate(booking(service="gardener"))


def test_prices_are_whole_rupees() -> None:
    assert Booking.model_validate(booking(price_inr=1350)).price_inr == 1350

    with pytest.raises(ValidationError):
        Booking.model_validate(booking(price_inr=1350.5))


def test_models_are_frozen() -> None:
    b = Booking.model_validate(booking())

    with pytest.raises(ValidationError):
        b.price_inr = 1  # type: ignore[misc]


# --- discriminated results -------------------------------------------------------------

BOOK_RESULT = TypeAdapter(BookResult)


def test_result_is_parsed_into_the_class_named_by_status() -> None:
    ok = BOOK_RESULT.validate_python({"status": "booked", "booking": booking()})
    failed = BOOK_RESULT.validate_python({"status": "slot_taken"})

    assert isinstance(ok, Booked)
    assert ok.booking.booking_id == "bk_1"
    assert isinstance(failed, BookFailed)
    assert failed.status == "slot_taken"


def test_impossible_states_are_rejected() -> None:
    with pytest.raises(ValidationError):
        BOOK_RESULT.validate_python({"status": "booked"})  # booked without a booking
    with pytest.raises(ValidationError):
        BOOK_RESULT.validate_python({"status": "exploded"})  # not a known outcome


# --- untrusted text marker ----------------------------------------------------------------


def test_free_text_from_providers_is_marked_untrusted() -> None:
    assert Untrusted in Review.model_fields["text"].metadata
    assert Untrusted in ProviderDetails.model_fields["description"].metadata
    assert Untrusted not in Review.model_fields["author"].metadata


def test_untrusted_text_is_still_a_plain_string() -> None:
    review = Review(author="A", rating=5, posted_on=date(2026, 10, 1), text="Great work")

    assert review.text == "Great work"


# --- get_booking_status reference -------------------------------------------------------


def test_booking_reference_requires_exactly_one_of_hold_or_key() -> None:
    assert booking_reference(hold_id="hold_1", idempotency_key=None) == ("hold_id", "hold_1")
    assert booking_reference(hold_id=None, idempotency_key="idem_1") == (
        "idempotency_key",
        "idem_1",
    )
    with pytest.raises(ValueError):
        booking_reference(hold_id=None, idempotency_key=None)
    with pytest.raises(ValueError):
        booking_reference(hold_id="hold_1", idempotency_key="idem_1")


# --- transport errors ----------------------------------------------------------------------


def test_transport_errors_share_a_base_class() -> None:
    assert issubclass(ProviderUnavailable, ProviderError)
    assert issubclass(ProviderTimeout, ProviderError)
    assert ProviderUnavailable("down").status_code == 503


def test_attributes_shape() -> None:
    attrs = Attributes(verified=True, years_experience=6, languages=["en", "kn"])

    assert attrs.languages == ["en", "kn"]
