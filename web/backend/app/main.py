"""FastAPI application factory and lifespan wiring.

Scaffold stage: boots an empty app with a /health route. Subsystems
(router, prober, tunnel supervisor, telemetry writer) attach here in
later build steps.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="relay", version=__version__, lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "version": __version__}

    return app


app = create_app()
