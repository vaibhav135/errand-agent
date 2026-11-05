from fastapi import APIRouter, FastAPI


def create_app() -> FastAPI:
    # Factory (not a module-level `app`) so tests can build isolated apps with
    # their own clock, DB paths and mock scenario once those are injected.
    app = FastAPI(title="Errand Agent")

    # All API routes live under /api, in dev (Vite proxies /api) and in the demo
    # build (FastAPI serves the frontend at /), so paths never differ per mode.
    api = APIRouter(prefix="/api")

    @api.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(api)
    return app
