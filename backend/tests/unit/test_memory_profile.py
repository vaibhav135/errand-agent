import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from errand_agent.core.clock import FakeClock
from errand_agent.core.ids import SequentialIdGen
from errand_agent.memory.profile import ProfileStore
from errand_agent.memory.store import ProvenanceError
from errand_agent.store.db import connect
from errand_agent.store.migrate import migrate, migrations_for
from errand_agent.tracing.recorder import TraceEvent, Tracer

NOW = datetime(2026, 11, 5, 3, 30, tzinfo=UTC)
USER = "user_1"
INTRO = (
    "I'm Priya Sharma, call me on +91 98450 12345 or mail priya.sharma@example.com."
    " Home is 12, 4th Cross, HSR Layout."
)


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = connect(tmp_path / "agent.db")
    migrate(c, migrations_for("agent"), FakeClock(NOW))
    return c


@pytest.fixture
def profiles(conn: sqlite3.Connection) -> ProfileStore:
    return ProfileStore(conn, clock=FakeClock(NOW), ids=SequentialIdGen())


def test_empty_profile(profiles: ProfileStore) -> None:
    p = profiles.get(USER)

    assert (p.name, p.phone, p.email, p.addresses) == (None, None, None, [])


def test_contact_details_are_stored_structured_and_normalised(profiles: ProfileStore) -> None:
    profiles.update(
        USER,
        name="Priya Sharma",
        phone="98450 12345",
        email="Priya.Sharma@example.com",
        user_message=INTRO,
    )

    p = profiles.get(USER)
    assert p.name == "Priya Sharma"
    assert p.phone == "+919845012345"
    assert p.email == "priya.sharma@example.com"


@pytest.mark.parametrize(
    "field",
    [
        {"phone": "98765 43210"},  # not what the user said
        {"email": "attacker@evil.com"},
        {"name": "Somebody Else"},
    ],
)
def test_every_value_must_come_from_the_users_message(
    profiles: ProfileStore, field: dict[str, str]
) -> None:
    update: Any = profiles.update  # the parametrised field varies; typing can't follow it
    with pytest.raises(ProvenanceError):
        update(USER, user_message=INTRO, **field)

    assert profiles.get(USER).phone is None


@pytest.mark.parametrize(
    ("phone", "valid"), [("98450 12345", True), ("12345", False), ("+1 415 555 0100", False)]
)
def test_phone_must_be_an_indian_mobile(profiles: ProfileStore, phone: str, valid: bool) -> None:
    said = f"my number is {phone}"
    if valid:
        profiles.update(USER, phone=phone, user_message=said)
    else:
        with pytest.raises(ValueError):
            profiles.update(USER, phone=phone, user_message=said)


def test_bad_email_is_rejected(profiles: ProfileStore) -> None:
    with pytest.raises(ValueError):
        profiles.update(USER, email="not-an-email", user_message="mail me at not-an-email")


# --- addresses --------------------------------------------------------------------


def test_address_needs_line_and_area_from_the_users_message(profiles: ProfileStore) -> None:
    a = profiles.add_address(
        USER, label="Home", line="12, 4th Cross", area="HSR Layout", user_message=INTRO
    )

    assert (a.label, a.line, a.area, a.is_default) == ("Home", "12, 4th Cross", "HSR Layout", True)
    with pytest.raises(ProvenanceError):
        profiles.add_address(
            USER, label="Office", line="99 MG Road", area="Indiranagar", user_message=INTRO
        )


def test_one_default_address(profiles: ProfileStore) -> None:
    home = profiles.add_address(
        USER, label="Home", line="12, 4th Cross", area="HSR Layout", user_message=INTRO
    )
    parents_said = "My parents live at 7 Temple Road, Jayanagar, make that the default."
    parents = profiles.add_address(
        USER,
        label="Parents",
        line="7 Temple Road",
        area="Jayanagar",
        user_message=parents_said,
        make_default=True,
    )

    by_id = {a.address_id: a for a in profiles.get(USER).addresses}
    assert by_id[parents.address_id].is_default and not by_id[home.address_id].is_default


def test_forgetting_contact_details_removes_them(profiles: ProfileStore) -> None:
    profiles.update(USER, phone="98450 12345", user_message=INTRO)
    home = profiles.add_address(
        USER, label="Home", line="12, 4th Cross", area="HSR Layout", user_message=INTRO
    )

    profiles.forget_field(USER, "phone")
    profiles.remove_address(USER, home.address_id)

    p = profiles.get(USER)
    assert p.phone is None and p.addresses == []


# --- what the model may see ---------------------------------------------------------------


def test_model_view_contains_no_pii(profiles: ProfileStore) -> None:
    profiles.update(
        USER,
        name="Priya Sharma",
        phone="98450 12345",
        email="priya.sharma@example.com",
        user_message=INTRO,
    )
    profiles.add_address(
        USER, label="Home", line="12, 4th Cross", area="HSR Layout", user_message=INTRO
    )

    view = profiles.model_view(USER)

    text = view.model_dump_json()
    for pii in ("Priya", "Sharma", "98450", "12345", "example.com", "4th Cross"):
        assert pii not in text
    assert view.addresses == [{"label": "Home", "area": "HSR Layout", "default": True}]
    assert view.has_phone and view.has_email


# --- traces carry field names, never values -------------------------------------------------


def test_profile_traces_never_contain_values(conn: sqlite3.Connection) -> None:
    events: list[TraceEvent] = []
    run = Tracer(FakeClock(NOW), SequentialIdGen(), subscribers=[events.append]).start_run(
        session_id="sess_1", trigger="test"
    )
    profiles = ProfileStore(conn, clock=FakeClock(NOW), ids=SequentialIdGen())

    profiles.update(
        USER, phone="98450 12345", email="priya.sharma@example.com", user_message=INTRO, run=run
    )
    profiles.add_address(
        USER, label="Home", line="12, 4th Cross", area="HSR Layout", user_message=INTRO, run=run
    )

    traced = [e.to_dict() for e in events if e.type == "profile"]
    assert [t["fields"] for t in traced] == [["email", "phone"], ["address"]]
    assert "98450" not in str(traced) and "example.com" not in str(traced)
    assert "4th Cross" not in str(traced)
