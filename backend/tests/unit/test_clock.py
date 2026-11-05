import time
from datetime import UTC, datetime, timedelta, timezone

import pytest

from errand_agent.core.clock import Clock, FakeClock, SystemClock

START = datetime(2026, 11, 5, 9, 0, tzinfo=UTC)


# --- SystemClock ---------------------------------------------------------------


def test_system_clock_now_is_utc_aware() -> None:
    now = SystemClock().now()

    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)
    assert abs(now - datetime.now(UTC)) < timedelta(seconds=1)


def test_both_clocks_satisfy_the_protocol() -> None:
    assert isinstance(SystemClock(), Clock)
    assert isinstance(FakeClock(START), Clock)


# --- FakeClock -----------------------------------------------------------------


def test_fake_clock_starts_at_given_time() -> None:
    assert FakeClock(START).now() == START


def test_fake_clock_rejects_naive_start() -> None:
    # A naive datetime has no timezone; comparing it with expires_at values
    # from the mocks would either crash or silently be off by hours.
    with pytest.raises(ValueError):
        FakeClock(datetime(2026, 11, 5, 9, 0))


def test_fake_clock_normalises_start_to_utc() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))

    clock = FakeClock(datetime(2026, 11, 5, 14, 30, tzinfo=ist))

    assert clock.now() == START
    assert clock.now().utcoffset() == timedelta(0)


def test_advance_moves_now_and_monotonic_together() -> None:
    clock = FakeClock(START)
    mono_before = clock.monotonic()

    clock.advance(11 * 60)

    assert clock.now() == START + timedelta(minutes=11)
    assert clock.monotonic() - mono_before == 11 * 60


def test_advance_rejects_negative_seconds() -> None:
    # Time never goes backwards: hold expiry and wall-clock limits assume it.
    with pytest.raises(ValueError):
        FakeClock(START).advance(-1)


async def test_fake_sleep_advances_time_without_blocking() -> None:
    clock = FakeClock(START)
    real_before = time.monotonic()

    await clock.sleep(30)

    assert clock.now() == START + timedelta(seconds=30)
    assert time.monotonic() - real_before < 0.5  # did not actually wait 30 s
