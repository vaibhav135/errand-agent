"""Memory and bookings API: inspect, confirm / reject, correct, forget, all in code."""

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from errand_agent.api.app import create_app
from errand_agent.core.clock import FakeClock
from errand_agent.core.config import Settings
from errand_agent.core.ids import SequentialIdGen


@pytest.fixture
def client(app_settings: Settings, app_clock: FakeClock) -> Iterator[TestClient]:
    # The demo user is seeded; no model is needed for any of these routes.
    with TestClient(create_app(app_settings, clock=app_clock, ids=SequentialIdGen())) as c:
        yield c


def memories(client: TestClient) -> dict[str, list[dict[str, Any]]]:
    response = client.get("/api/memories")
    assert response.status_code == 200
    return response.json()


def find(items: list[dict[str, Any]], text_start: str) -> dict[str, Any]:
    return next(m for m in items if m["text"].startswith(text_start))


def test_saved_facts_and_suggestions_are_separate(client: TestClient) -> None:
    got = memories(client)

    start = find(got["saved"], "Earliest start: 10:00")
    assert start["source"] == "explicit"
    sparks = find(got["suggestions"], "Usual electrician: Sparks Electricals")
    assert sparks["reason"].startswith("2 completed bookings")
    assert not any(m["text"].startswith("Usual electrician") for m in got["saved"])


def test_confirming_a_suggestion_saves_it(client: TestClient) -> None:
    sparks = find(memories(client)["suggestions"], "Usual electrician")

    response = client.post(f"/api/memories/{sparks['memory_id']}/confirm")

    assert response.status_code == 200
    got = memories(client)
    assert find(got["saved"], "Usual electrician")["source"] == "inferred"
    assert not any(m["text"].startswith("Usual electrician") for m in got["suggestions"])


def test_rejecting_a_suggestion_drops_it(client: TestClient) -> None:
    cool = find(memories(client)["suggestions"], "Usual AC servicing")

    assert client.post(f"/api/memories/{cool['memory_id']}/reject").status_code == 200

    got = memories(client)
    assert not any("CoolBreeze" in m["text"] for m in got["saved"] + got["suggestions"])


def test_correcting_a_fact_replaces_it(client: TestClient) -> None:
    start = find(memories(client)["saved"], "Earliest start")

    response = client.patch(f"/api/memories/{start['memory_id']}", json={"value": "11:00"})

    assert response.status_code == 200
    saved = memories(client)["saved"]
    assert find(saved, "Earliest start")["text"] == "Earliest start: 11:00"
    assert not any(m["text"] == "Earliest start: 10:00" for m in saved)


def test_forgetting_a_fact_removes_it(client: TestClient) -> None:
    dog = find(memories(client)["saved"], "Note: dog at home")

    assert client.delete(f"/api/memories/{dog['memory_id']}").status_code == 204

    assert not any("dog at home" in m["text"] for m in memories(client)["saved"])


def test_bad_requests_are_explained(client: TestClient) -> None:
    start = find(memories(client)["saved"], "Earliest start")

    assert client.post("/api/memories/mem_nope/confirm").status_code == 404
    already_active = client.post(f"/api/memories/{start['memory_id']}/confirm")
    assert already_active.status_code == 409  # only a suggestion can be confirmed
    bad_value = client.patch(f"/api/memories/{start['memory_id']}", json={"value": "teatime"})
    assert bad_value.status_code == 422


def test_bookings_come_from_the_providers_records(client: TestClient) -> None:
    response = client.get("/api/bookings")

    assert response.status_code == 200
    by_id = {b["booking_id"]: b for b in response.json()}
    assert by_id["bk_hist_coolbreeze"]["status"] == "confirmed"
    assert by_id["bk_hist_coolbreeze"]["when"] == "upcoming"
    assert by_id["bk_hist_sparks"]["when"] == "past"
