from datetime import date
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from errand_agent.api.app import Services, create_app
from errand_agent.core.clock import FakeClock
from errand_agent.core.config import Settings
from errand_agent.mocks.scenario import Scenario
from errand_agent.providers.models import DateRange, SearchOk
from errand_agent.store.db import connect
from errand_agent.store.migrate import migrations_for
from tests.unit.workflow_world import ProcessKilled, ScriptedEnv, World


def services(app: FastAPI) -> Services:
    return app.state.services


def test_startup_creates_and_migrates_both_databases(
    app_settings: Settings, app_clock: FakeClock
) -> None:
    with TestClient(create_app(app_settings, clock=app_clock)):
        pass

    agent = connect(app_settings.agent_db_path)
    mock = connect(app_settings.mock_db_path)
    for conn, db in ((agent, "agent"), (mock, "mock_world")):
        applied = [r[0] for r in conn.execute("SELECT version FROM schema_migrations")]
        packaged = [f for f in migrations_for(db).iterdir() if f.name.endswith(".sql")]  # type: ignore[arg-type]
        assert len(applied) == len(packaged) > 0, db
    tables = {r[0] for r in agent.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "sessions" in tables


# Tests that use services directly run the lifespan in *this* event loop: TestClient
# runs the app in another thread, and SQLite connections are bound to the thread that
# created them (a guard we keep on purpose; see docs/COMPONENTS.md, "Database").


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


async def test_startup_finishes_a_booking_left_behind_by_a_crash(
    app_settings: Settings, tmp_path: Path
) -> None:
    # The previous process committed an approved booking, the provider made it,
    # and the process died before recording the result.
    world = World(app_settings.data_dir, Scenario(book_outcomes=["success"]))
    env = ScriptedEnv(world.mock)
    errand_id, approval_id = await world.approved_errand(env)
    env.crash["book"] = "after"
    with pytest.raises(ProcessKilled):
        await world.flow(env).execute(errand_id, approval_id)
    assert world.errands.get(errand_id).state == "booking"

    app = create_app(app_settings, clock=world.clock)  # the next process starts
    async with app.router.lifespan_context(app):
        pass

    assert world.errands.get(errand_id).state == "booked"
    assert (
        world.mock_count(
            "SELECT count(*) FROM bookings WHERE user_id = 'user_1' AND idempotency_key IS NOT NULL"
        )
        == 1
    )


def test_startup_seeds_the_demo_user_once(app_settings: Settings, app_clock: FakeClock) -> None:
    for _ in range(2):
        with TestClient(create_app(app_settings, clock=app_clock)):
            pass

    agent = connect(app_settings.agent_db_path)
    assert agent.execute("SELECT count(*) FROM memories").fetchone()[0] == 55
    assert agent.execute("SELECT count(*) FROM messages").fetchone()[0] == 200
