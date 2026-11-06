from datetime import UTC, datetime
from pathlib import Path

import pytest

from errand_agent.core.clock import FakeClock
from errand_agent.core.config import Settings

APP_NOW = datetime(2026, 11, 5, 3, 30, tzinfo=UTC)  # Thursday 09:00 IST


@pytest.fixture
def app_settings(tmp_path: Path) -> Settings:
    """Settings for app-level tests: temp data dir, never the developer's .env.

    The background reconciler loop is off: with a FakeClock its sleep returns at once,
    so it would spin. The startup reconcile pass still runs.
    """
    return Settings(_env_file=None, data_dir=tmp_path / "var", reconcile_interval_seconds=0)  # type: ignore[call-arg]


@pytest.fixture
def app_clock() -> FakeClock:
    return FakeClock(APP_NOW)
