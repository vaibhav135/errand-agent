from fastapi import FastAPI


def create_app() -> FastAPI:
    # Factory (not a module-level `app`) so tests can build isolated apps with
    # their own clock, DB paths and mock scenario once those are injected.
    app = FastAPI(title="Errand Agent")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
