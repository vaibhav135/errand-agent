"""App factory and service wiring.

What: `create_app(settings, clock=, ids=)` builds the FastAPI app; on startup its
      lifespan builds a `Services` container (settings, clock, ids, agent.db
      connection, mock world, tracer) on `app.state.services` and closes it on
      shutdown.

Why:  One place where concrete implementations are chosen and wired together.
      Every other layer receives what it needs (a Clock, a ToolEnv, a Tracer)
      instead of constructing it, so tests can inject a FakeClock and temp paths.

How:  - Startup opens and migrates agent.db, and opens the mock world (migrates and
        seeds mock_world.db once). Restarts reuse the same files.
      - Defaults (Settings from env/.env, SystemClock, RandomIdGen) are used by the
        dev server (`uvicorn --factory`); tests pass their own.
      - All HTTP routes live under /api, so the built frontend can be served at /.
"""

import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import APIRouter, FastAPI

from errand_agent.core.clock import Clock, SystemClock
from errand_agent.core.config import Settings, load_settings
from errand_agent.core.ids import IdGen, RandomIdGen
from errand_agent.mocks.scenario import Scenario
from errand_agent.mocks.world import MockWorld
from errand_agent.store.db import connect
from errand_agent.store.migrate import migrate, migrations_for
from errand_agent.tracing.recorder import JsonlWriter, Tracer


@dataclass
class Services:
    settings: Settings
    clock: Clock
    ids: IdGen
    agent_db: sqlite3.Connection
    world: MockWorld
    tracer: Tracer

    def close(self) -> None:
        self.world.close()
        self.agent_db.close()


def build_services(settings: Settings, clock: Clock, ids: IdGen) -> Services:
    agent_db = connect(settings.agent_db_path)
    migrate(agent_db, migrations_for("agent"), clock)
    world = MockWorld.open(
        settings.mock_db_path, clock=clock, ids=ids, scenario=Scenario(seed=settings.mock_seed)
    )
    tracer = Tracer(clock, ids, subscribers=[JsonlWriter(settings.trace_dir)])
    return Services(settings, clock, ids, agent_db, world, tracer)


def create_app(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    ids: IdGen | None = None,
) -> FastAPI:
    resolved_settings = settings or load_settings()
    resolved_clock = clock or SystemClock()
    resolved_ids = ids or RandomIdGen()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        services = build_services(resolved_settings, resolved_clock, resolved_ids)
        app.state.services = services
        try:
            yield
        finally:
            services.close()

    app = FastAPI(title="Errand Agent", lifespan=lifespan)

    # All API routes live under /api, in dev (Vite proxies /api) and in the demo
    # build (FastAPI serves the frontend at /), so paths never differ per mode.
    api = APIRouter(prefix="/api")

    @api.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(api)
    return app
