"""App factory and service wiring.

What: `create_app(settings, clock=, ids=)` builds the FastAPI app; on startup its
      lifespan builds a `Services` container (settings, clock, ids, agent.db
      connection, mock world, tracer, workflow stores, reconciler) on
      `app.state.services` and closes it on shutdown.

Why:  One place where concrete implementations are chosen and wired together.
      Every other layer receives what it needs (a Clock, a ToolEnv, a Tracer)
      instead of constructing it, so tests can inject a FakeClock and temp paths.

How:  - Startup opens and migrates agent.db, and opens the mock world (migrates and
        seeds mock_world.db once). Restarts reuse the same files.
      - With `seed_demo_user` on, an empty agent.db gets the generated demo user.
      - Startup then runs one reconcile pass (restart recovery: settle side effects
        the previous process left unknown) and starts the background reconciler
        loop unless `reconcile_interval_seconds` is 0; shutdown cancels it.
      - Defaults (Settings from env/.env, SystemClock, RandomIdGen) are used by the
        dev server (`uvicorn --factory`); tests pass their own.
      - All HTTP routes live under /api, so the built frontend can be served at /.
      - `build_handler(services)` makes the MessageHandler (models, run and context
        budgets from Settings). The chat CLI and the API both use it, so they can't
        drift apart. It is separate from build_services so the app can start without
        a model key; tests pass their own scripted model.
      - `Services.handler()` builds it on first use; `lock_for(session)` keeps one turn
        at a time per session; the tracer also publishes to an `EventHub`, which the
        streaming routes listen to (api/chat_routes.py, D4).
"""

import asyncio
import contextlib
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from fastapi import APIRouter, FastAPI
from pydantic_ai.models import Model

from errand_agent.agent import models as agent_models
from errand_agent.agent.handler import MessageHandler
from errand_agent.api.chat_routes import router as chat_router
from errand_agent.api.events import EventHub
from errand_agent.api.memory_routes import router as memory_router
from errand_agent.core.clock import Clock, SystemClock
from errand_agent.core.config import Settings, load_settings
from errand_agent.core.ids import IdGen, RandomIdGen
from errand_agent.memory.seed import seed_demo_user
from errand_agent.mocks.scenario import Scenario
from errand_agent.mocks.world import MockWorld
from errand_agent.store.db import connect
from errand_agent.store.migrate import migrate, migrations_for
from errand_agent.tracing.recorder import JsonlWriter, Tracer
from errand_agent.workflow.actions import ProviderActionStore
from errand_agent.workflow.approvals import ApprovalStore
from errand_agent.workflow.context import ContextBudget
from errand_agent.workflow.errands import ErrandStore
from errand_agent.workflow.guarded_env import RunBudget
from errand_agent.workflow.reconciler import Reconciler


@dataclass
class Services:
    settings: Settings
    clock: Clock
    ids: IdGen
    agent_db: sqlite3.Connection
    world: MockWorld
    tracer: Tracer
    errands: ErrandStore
    approvals: ApprovalStore
    actions: ProviderActionStore
    reconciler: Reconciler
    hub: EventHub
    model: Model | None = None  # a test's scripted model; None = from Settings
    _handler: MessageHandler | None = None
    _locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    # Turns still running after their client left: kept referenced so they finish.
    background: set[asyncio.Task[None]] = field(default_factory=set)

    def handler(self) -> MessageHandler:
        """Built on first use, so the app starts without a model key; raises
        ModelNotConfigured (saying what to set) when no model can be built."""
        if self._handler is None:
            self._handler = build_handler(self, model=self.model)
        return self._handler

    def lock_for(self, session_id: str) -> asyncio.Lock:
        """One turn at a time per session: a second message waits for the first."""
        return self._locks.setdefault(session_id, asyncio.Lock())

    def close(self) -> None:
        self.world.close()
        self.agent_db.close()


def build_services(settings: Settings, clock: Clock, ids: IdGen) -> Services:
    agent_db = connect(settings.agent_db_path)
    migrate(agent_db, migrations_for("agent"), clock)
    if settings.seed_demo_user:
        seed_demo_user(agent_db, clock)
    world = MockWorld.open(
        settings.mock_db_path, clock=clock, ids=ids, scenario=Scenario(seed=settings.mock_seed)
    )
    hub = EventHub()
    tracer = Tracer(clock, ids, subscribers=[JsonlWriter(settings.trace_dir), hub.publish])
    errands = ErrandStore(agent_db, clock=clock, ids=ids)
    approvals = ApprovalStore(agent_db, clock=clock, ids=ids)
    actions = ProviderActionStore(agent_db, clock=clock)
    reconciler = Reconciler(
        conn=agent_db,
        env=world,
        errands=errands,
        approvals=approvals,
        actions=actions,
        clock=clock,
        tracer=tracer,
    )
    return Services(
        settings=settings,
        clock=clock,
        ids=ids,
        agent_db=agent_db,
        world=world,
        tracer=tracer,
        errands=errands,
        approvals=approvals,
        actions=actions,
        reconciler=reconciler,
        hub=hub,
    )


def build_handler(
    services: Services, *, model: Model | None = None, review_model: Model | None = None
) -> MessageHandler:
    """Raises ModelNotConfigured (with what to set) when no model can be built."""
    settings = services.settings
    main = model or agent_models.main_model(settings)
    if review_model is None:  # a test's scripted model reads reviews too; else Settings
        review_model = main if model is not None else agent_models.review_model(settings)
    return MessageHandler(
        conn=services.agent_db,
        clock=services.clock,
        ids=services.ids,
        env=services.world,
        tracer=services.tracer,
        model=main,
        review_model=review_model,
        budget=RunBudget.from_settings(settings),
        context_budget=ContextBudget.from_settings(settings),
    )


def create_app(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    ids: IdGen | None = None,
    model: Model | None = None,
) -> FastAPI:
    resolved_settings = settings or load_settings()
    resolved_clock = clock or SystemClock()
    resolved_ids = ids or RandomIdGen()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        services = build_services(resolved_settings, resolved_clock, resolved_ids)
        services.model = model
        app.state.services = services
        # Restart recovery: settle anything left unknown by the previous process.
        await services.reconciler.reconcile_once()
        loop_task: asyncio.Task[None] | None = None
        if resolved_settings.reconcile_interval_seconds > 0:
            loop_task = asyncio.create_task(
                services.reconciler.run_periodically(resolved_settings.reconcile_interval_seconds)
            )
        try:
            yield
        finally:
            if loop_task is not None:
                loop_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await loop_task
            services.close()

    app = FastAPI(title="Errand Agent", lifespan=lifespan)

    # All API routes live under /api, in dev (Vite proxies /api) and in the demo
    # build (FastAPI serves the frontend at /), so paths never differ per mode.
    api = APIRouter(prefix="/api")

    @api.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    api.include_router(chat_router)
    api.include_router(memory_router)
    app.include_router(api)
    return app
