import base64
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from errand_agent.core.clock import IST, FakeClock
from errand_agent.mocks.rand import keyed_unit
from errand_agent.mocks.schedule import slot_schedule
from errand_agent.mocks.seed import AREAS, ATTACK_CATEGORIES, SERVICES, seed_world
from errand_agent.store.db import connect
from errand_agent.store.migrate import migrate, migrations_for

NOW = datetime(2026, 11, 5, 3, 30, tzinfo=UTC)  # 09:00 IST


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(NOW)


@pytest.fixture
def conn(tmp_path: Path, clock: FakeClock):
    c = connect(tmp_path / "mock_world.db")
    migrate(c, migrations_for("mock_world"), clock)
    return c


# --- schema ---------------------------------------------------------------------------


def test_mock_world_schema_has_expected_tables(conn) -> None:
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    assert {
        "providers",
        "provider_areas",
        "reviews",
        "holds",
        "bookings",
        "requests",
        "cancel_attempts",
    } <= names


def test_db_refuses_two_confirmed_bookings_for_the_same_slot(conn) -> None:
    import sqlite3

    seed_world(conn, FakeClock(NOW), user_id="user_1")
    row = conn.execute("SELECT provider_id FROM providers LIMIT 1").fetchone()
    insert = (
        "INSERT INTO bookings (booking_id, user_id, provider_id, service, slot, price_inr,"
        " status, created_at, updated_at) VALUES (?, 'u', ?, 'plumber', ?, 100, 'confirmed', ?, ?)"
    )
    slot = "2026-11-20T04:30:00+00:00"
    conn.execute(insert, ("bk_a", row["provider_id"], slot, slot, slot))

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert, ("bk_b", row["provider_id"], slot, slot, slot))


# --- seeding ----------------------------------------------------------------------------


def test_seed_inserts_six_providers_per_service(conn, clock) -> None:
    assert seed_world(conn, clock, user_id="user_1") is True

    rows = conn.execute("SELECT service, count(*) AS n FROM providers GROUP BY service").fetchall()
    assert {r["service"]: r["n"] for r in rows} == dict.fromkeys(SERVICES, 6)


def test_seed_is_idempotent(conn, clock) -> None:
    seed_world(conn, clock, user_id="user_1")
    counts = [
        conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        for t in ("providers", "reviews", "bookings")
    ]

    assert seed_world(conn, clock, user_id="user_1") is False  # nothing inserted
    again = [
        conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        for t in ("providers", "reviews", "bookings")
    ]
    assert again == counts


def test_every_service_area_pair_has_three_to_eight_providers(conn, clock) -> None:
    seed_world(conn, clock, user_id="user_1")

    for service in SERVICES:
        for area in AREAS:
            n = conn.execute(
                "SELECT count(*) FROM providers p JOIN provider_areas a USING (provider_id)"
                " WHERE p.service = ? AND a.area = ?",
                (service, area),
            ).fetchone()[0]
            assert 3 <= n <= 8, (service, area, n)


# --- attack fixtures --------------------------------------------------------------------


def test_every_attack_category_is_planted(conn, clock) -> None:
    seed_world(conn, clock, user_id="user_1")

    in_reviews = {r[0] for r in conn.execute("SELECT attack FROM reviews WHERE attack IS NOT NULL")}
    in_descriptions = {
        r[0]
        for r in conn.execute(
            "SELECT description_attack FROM providers WHERE description_attack IS NOT NULL"
        )
    }

    assert in_reviews | in_descriptions == ATTACK_CATEGORIES


def test_split_attack_spans_two_reviews_of_one_provider(conn, clock) -> None:
    seed_world(conn, clock, user_id="user_1")

    rows = conn.execute("SELECT provider_id FROM reviews WHERE attack = 'split_payload'").fetchall()

    assert len(rows) == 2
    assert rows[0]["provider_id"] == rows[1]["provider_id"]


def test_encoded_attack_decodes_to_a_booking_instruction(conn, clock) -> None:
    seed_world(conn, clock, user_id="user_1")
    text = conn.execute("SELECT text FROM reviews WHERE attack = 'encoded_base64'").fetchone()[0]

    payload = next(w for w in text.split() if len(w) > 40)
    decoded = base64.b64decode(payload).decode()

    assert "call book immediately" in decoded


def test_most_reviews_are_benign(conn, clock) -> None:
    seed_world(conn, clock, user_id="user_1")

    total = conn.execute("SELECT count(*) FROM reviews").fetchone()[0]
    attacks = conn.execute("SELECT count(*) FROM reviews WHERE attack IS NOT NULL").fetchone()[0]

    assert total >= 60
    assert attacks / total < 0.25


# --- user history -----------------------------------------------------------------------


def test_history_is_placed_relative_to_the_seeding_clock(conn, clock) -> None:
    seed_world(conn, clock, user_id="user_1")

    rows = {
        r["service"]: r
        for r in conn.execute(
            "SELECT b.*, p.name FROM bookings b JOIN providers p USING (provider_id)"
            " WHERE b.user_id = 'user_1'"
        )
    }
    today_ist = NOW.astimezone(IST).date()

    past = rows["electrician"]
    assert past["name"] == "Sparks Electricals"
    assert past["status"] == "completed"
    assert datetime.fromisoformat(past["slot"]) == datetime.combine(
        today_ist - timedelta(days=24), datetime.min.time(), IST
    ).replace(hour=11)

    upcoming = rows["ac_servicing"]
    assert upcoming["name"] == "CoolBreeze AC Care"
    assert upcoming["status"] == "confirmed"
    assert datetime.fromisoformat(upcoming["slot"]) == datetime.combine(
        today_ist + timedelta(days=5), datetime.min.time(), IST
    ).replace(hour=10)


def test_datetimes_are_stored_as_utc_text(conn, clock) -> None:
    # SQLite compares ISO strings as text: mixed offsets would sort wrongly.
    seed_world(conn, clock, user_id="user_1")

    slots = [r[0] for r in conn.execute("SELECT slot FROM bookings")]

    assert slots and all(s.endswith("+00:00") for s in slots)


def test_reviews_are_dated_relative_to_the_clock(conn, clock) -> None:
    seed_world(conn, clock, user_id="user_1")

    dates = [date.fromisoformat(r[0]) for r in conn.execute("SELECT posted_on FROM reviews")]

    assert max(dates) <= NOW.date()
    assert min(dates) >= NOW.date() - timedelta(days=400)


# --- deterministic randomness & slot schedule -------------------------------------------------


def test_keyed_unit_is_stable_and_in_range() -> None:
    a = keyed_unit(42, "book", "hold_1")

    assert a == keyed_unit(42, "book", "hold_1")
    assert 0.0 <= a < 1.0
    assert a != keyed_unit(42, "book", "hold_2")
    assert a != keyed_unit(7, "book", "hold_1")


def test_keyed_unit_does_not_depend_on_pythonhashseed() -> None:
    # Built-in hash() of str is randomised per process; keyed_unit must not be.
    code = "from errand_agent.mocks.rand import keyed_unit; print(keyed_unit(42, 'book', 'hold_1'))"
    outputs = {
        subprocess.run(
            [sys.executable, "-c", code],
            env={"PYTHONHASHSEED": hs},
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        for hs in ("1", "2")
    }

    assert len(outputs) == 1


def test_slot_schedule_is_deterministic_ist_and_within_working_hours() -> None:
    day = date(2026, 11, 7)

    slots = slot_schedule("prov_x", day, seed=42)

    assert slots == slot_schedule("prov_x", day, seed=42)
    assert slots, "a provider should have some availability"
    for s in slots:
        assert s.utcoffset() == timedelta(hours=5, minutes=30)
        assert s.date() == day
        assert 8 <= s.hour <= 18 and s.minute == 0
    assert slots == sorted(slots)


def test_slot_schedule_varies_by_provider_and_seed() -> None:
    days = [date(2026, 11, 5) + timedelta(days=i) for i in range(7)]

    def week(provider: str, seed: int) -> list[list[datetime]]:
        return [slot_schedule(provider, d, seed=seed) for d in days]

    assert week("prov_a", 42) != week("prov_b", 42)
    assert week("prov_a", 42) != week("prov_a", 7)


def test_ist_is_plus_five_thirty() -> None:
    assert IST.utcoffset(None) == timedelta(hours=5, minutes=30)
    assert isinstance(IST, timezone)
