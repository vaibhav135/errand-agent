from datetime import date

from fastapi import FastAPI
from fastapi.testclient import TestClient

from errand_agent.api.app import Services, create_app
from errand_agent.core.clock import FakeClock
from errand_agent.core.config import Settings
from errand_agent.providers.models import DateRange, SearchOk
from errand_agent.store.db import connect


def services(app: FastAPI) -> Services:
    return app.state.services


def test_startup_creates_and_migrates_both_databases(
    app_settings: Settings, app_clock: FakeClock
) -> None:
    with TestClient(create_app(app_settings, clock=app_clock)):
        pass

    agent = connect(app_settings.agent_db_path)
    mock = connect(app_settings.mock_db_path)
    assert [r[0] for r in agent.execute("SELECT version FROM schema_migrations")] == [1]
    assert [r[0] for r in mock.execute("SELECT version FROM schema_migrations")] == [
        1,
        2,
        3,
        4,
        5,
        6,
    ]
    tables = {r[0] for r in agent.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "sessions" in tables


# Tests that use services directly run the lifespan in *this* event loop: TestClient
# runs the app in another thread, and SQLite connections are bound to the thread that
# created them (a guard we keep on purpose; see docs/ARCHITECTURE.md).


async def test_services_are_available_while_running(
    app_settings: Settings, app_clock: FakeClock
) -> None:
    app = create_app(app_settings, clock=app_clock)

    async with app.router.lifespan_context(app):
        s = services(app)
        assert s.settings is app_settings
        assert s.clock is app_clock
        assert s.agent_db.execute("SELECT 1").fetchone()[0] == 1


async def test_db_connections_refuse_other_threads(
    app_settings: Settings, app_clock: FakeClock
) -> None:
    import sqlite3
    import threading

    app = create_app(app_settings, clock=app_clock)
    errors: list[BaseException] = []

    async with app.router.lifespan_context(app):
        db = services(app).agent_db

        def use_from_another_thread() -> None:
            try:
                db.execute("SELECT 1")
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=use_from_another_thread)
        worker.start()
        worker.join()

    assert len(errors) == 1 and isinstance(errors[0], sqlite3.ProgrammingError)


async def test_mock_world_is_wired_to_the_injected_clock(
    app_settings: Settings, app_clock: FakeClock
) -> None:
    app = create_app(app_settings, clock=app_clock)

    async with app.router.lifespan_context(app):
        world = services(app).world
        result = await world.search_providers(
            "plumber", "Indiranagar", DateRange(start=date(2026, 11, 5), end=date(2026, 11, 5))
        )

    assert isinstance(result, SearchOk)
    for provider in result.providers:
        assert all(slot > app_clock.now() for slot in provider.available_slots)


def test_restart_reuses_the_same_data(app_settings: Settings, app_clock: FakeClock) -> None:
    for _ in range(2):  # two "process starts" on the same data dir
        with TestClient(create_app(app_settings, clock=app_clock)):
            pass

    mock = connect(app_settings.mock_db_path)
    assert mock.execute("SELECT count(*) FROM providers").fetchone()[0] == 30


def test_traces_go_to_the_configured_directory(
    app_settings: Settings, app_clock: FakeClock
) -> None:
    app = create_app(app_settings, clock=app_clock)

    with TestClient(app), services(app).tracer.start_run(session_id="s", trigger="test"):
        pass

    assert len(list(app_settings.trace_dir.glob("*.jsonl"))) == 1
