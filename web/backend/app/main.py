"""FastAPI application factory and lifespan wiring.

Boot order: logging -> DB -> settings -> telemetry writer + live tracker ->
(router registry, health prober, tunnel supervisor attach in later modules)
-> background housekeeping (live sampler, retention pruning). In production
the built dashboard (web/frontend/dist) is served statically from "/".
"""

import asyncio
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response

from app import __version__
from app.config import Config, config
from app.core.logging import get_logger, setup_logging
from app.db import Database
from app.routes import admin_endpoints, admin_settings
from app.services.health import HealthProber
from app.services.router import Router
from app.services.settings_store import SettingsStore
from app.services.telemetry import LiveTracker, TelemetryWriter

log = get_logger("app")

_SAMPLE_INTERVAL = 1.5
_PRUNE_INTERVAL = 3600.0


async def _housekeeping(app: FastAPI) -> None:
    last_prune = 0.0
    while True:
        await asyncio.sleep(_SAMPLE_INTERVAL)
        app.state.live.sample()
        now = asyncio.get_event_loop().time()
        if now - last_prune > _PRUNE_INTERVAL:
            last_prune = now
            retention = app.state.settings.current.retention_days
            try:
                await app.state.telemetry.prune(retention)
            except Exception:
                log.exception("retention prune failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = app.state.cfg
    app.state.http = httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=cfg.connect_timeout, read=cfg.read_timeout,
            write=cfg.write_timeout, pool=30.0),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    )
    app.state.prober = HealthProber(
        app.state.router, app.state.http,
        interval=cfg.probe_interval, timeout=cfg.probe_timeout)
    await app.state.telemetry.start()
    await app.state.prober.start()
    task = asyncio.create_task(_housekeeping(app), name="housekeeping")
    log.info("relay started", extra={"data": {
        "version": __version__, "port": app.state.cfg.port,
        "db": str(app.state.cfg.db_path)}})
    try:
        yield
    finally:
        task.cancel()
        await app.state.prober.stop()
        await app.state.telemetry.stop()
        await app.state.http.aclose()
        app.state.db.close()
        log.info("relay stopped")


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or config
    setup_logging(cfg.log_dir, cfg.log_level)

    app = FastAPI(title="relay", version=__version__, lifespan=lifespan)
    app.state.cfg = cfg
    app.state.db = Database(cfg.db_path)
    app.state.settings = SettingsStore(app.state.db, boot_port=cfg.port)
    app.state.telemetry = TelemetryWriter(app.state.db)
    app.state.live = LiveTracker()
    app.state.router = Router(
        app.state.db, unhealthy_after=cfg.unhealthy_after,
        recover_after=cfg.recover_after)

    @app.middleware("http")
    async def cors_and_access_log(request: Request, call_next):
        # Dynamic CORS honoring the live "allow_cors" setting; admin/stats
        # traffic is same-origin so this mainly serves /v1 browser clients.
        if request.method == "OPTIONS" and app.state.settings.current.allow_cors:
            return Response(status_code=204, headers=_cors_headers())
        response = await call_next(request)
        if app.state.settings.current.allow_cors:
            for k, v in _cors_headers().items():
                response.headers.setdefault(k, v)
        return response

    def _cors_headers() -> dict:
        return {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, PATCH, PUT, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type, X-Admin-Token",
        }

    @app.get("/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "version": __version__,
            "uptime_s": round(app.state.settings.uptime_s(), 1),
        }

    app.include_router(admin_endpoints.router)
    app.include_router(admin_settings.router)

    _mount_static_dashboard(app, cfg)
    return app


def _mount_static_dashboard(app: FastAPI, cfg: Config) -> None:
    """Serve the built Astro dashboard from / when dist exists."""
    if not cfg.frontend_dist.is_dir():
        log.info("frontend dist not found; API-only mode",
                 extra={"data": {"path": str(cfg.frontend_dist)}})
        return
    from fastapi.staticfiles import StaticFiles

    app.mount(
        "/", StaticFiles(directory=cfg.frontend_dist, html=True), name="dashboard"
    )
    log.info("serving dashboard", extra={"data": {"path": str(cfg.frontend_dist)}})


app = create_app()
