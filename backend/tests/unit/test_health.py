from fastapi.testclient import TestClient

from errand_agent.api.app import create_app
from errand_agent.core.clock import FakeClock
from errand_agent.core.config import Settings


def test_health_returns_ok(app_settings: Settings, app_clock: FakeClock) -> None:
    with TestClient(create_app(app_settings, clock=app_clock)) as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
