from datetime import UTC, datetime
from pathlib import Path

import pytest

from errand_agent.core.clock import FakeClock
from errand_agent.core.config import Settings

APP_NOW = datetime(2026, 11, 5, 3, 30, tzinfo=UTC)  # Thursday 09:00 IST


@pytest.fixture
def app_settings(tmp_path: Path) -> Settings:
    """Settings for app-level tests: temp data dir, never the developer's .env.

    The background reconciler and monitor loops are off: with a FakeClock their sleep
    returns at once, so they would spin. The startup reconcile pass still runs; tests
    call `monitor.check_once()` themselves.
    """
    return Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]  (pydantic-settings init option)
        data_dir=tmp_path / "var",
        reconcile_interval_seconds=0,
        monitor_interval_seconds=0,
    )


@pytest.fixture
def app_clock() -> FakeClock:
    return FakeClock(APP_NOW)
