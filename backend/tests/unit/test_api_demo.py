"""Demo API: trigger failures live on the mock world, only in demo mode (D4)."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from errand_agent.api.app import create_app
from errand_agent.core.clock import FakeClock
from errand_agent.core.config import Settings
from errand_agent.core.ids import SequentialIdGen


def client_for(settings: Settings, clock: FakeClock, *, demo: bool) -> TestClient:
    settings = settings.model_copy(update={"demo_mode": demo})
    return TestClient(create_app(settings, clock=clock, ids=SequentialIdGen()))


@pytest.fixture
def demo(app_settings: Settings, app_clock: FakeClock) -> Iterator[TestClient]:
    with client_for(app_settings, app_clock, demo=True) as client:
        yield client


def test_off_by_default_and_hidden(app_settings: Settings, app_clock: FakeClock) -> None:
    with client_for(app_settings, app_clock, demo=False) as client:
        assert client.get("/api/demo").json() == {"enabled": False}
        response = client.post("/api/demo/next-book", json={"outcome": "timeout_booked"})
        assert response.status_code == 404


def test_the_next_booking_can_be_scripted(demo: TestClient) -> None:
    assert demo.get("/api/demo").json() == {"enabled": True}

    assert demo.post("/api/demo/next-book", json={"outcome": "timeout_booked"}).status_code == 200
    assert demo.post("/api/demo/next-book", json={"outcome": "explode"}).status_code == 422


def test_a_provider_can_cancel_a_booking(demo: TestClient) -> None:
    response = demo.post("/api/demo/provider-cancels", json={"booking_id": "bk_hist_coolbreeze"})

    assert response.status_code == 200
    bookings = {b["booking_id"]: b for b in demo.get("/api/bookings").json()}
    assert bookings["bk_hist_coolbreeze"]["status"] == "cancelled_by_provider"
    again = demo.post("/api/demo/provider-cancels", json={"booking_id": "bk_hist_coolbreeze"})
    assert again.status_code == 409  # not confirmed any more


def test_providers_are_listed_for_the_price_picker(
    demo: TestClient, app_settings: Settings, app_clock: FakeClock
) -> None:
    providers = {p["provider_id"]: p for p in demo.get("/api/demo/providers").json()}

    assert providers["plm_quickfix"]["name"] == "QuickFix Plumbing"
    assert providers["plm_quickfix"]["service"] == "plumber"
    assert providers["plm_quickfix"]["price_inr"] > 0
    with client_for(app_settings, app_clock, demo=False) as off:
        assert off.get("/api/demo/providers").status_code == 404


def test_prices_can_drift_and_everything_resets(demo: TestClient) -> None:
    drift = demo.post(
        "/api/demo/price-drift", json={"provider_id": "plm_quickfix", "fraction": 0.2}
    )

    assert drift.status_code == 200
    assert demo.post("/api/demo/reset").status_code == 200
