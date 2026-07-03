"""Control plane: runtime settings + proxy info + frontend log ingestion."""

import time

from fastapi import APIRouter, Depends, Request

from app.core.logging import get_logger
from app.schemas import FrontendLogBatch, RuntimeSettings, SettingsPatch
from app.security import admin_guard

log = get_logger("admin")
felog = get_logger("frontend")

router = APIRouter(
    prefix="/admin", tags=["settings"], dependencies=[Depends(admin_guard)]
)


@router.get("/settings", response_model=RuntimeSettings)
async def get_settings(request: Request):
    return request.app.state.settings.current


@router.put("/settings", response_model=RuntimeSettings)
async def put_settings(request: Request, patch: SettingsPatch):
    return request.app.state.settings.update(patch)


@router.get("/proxy")
async def proxy_info(request: Request):
    st = request.app.state.settings
    live = request.app.state.live
    row = await request.app.state.db.aquery_one(
        "SELECT COUNT(*) AS n FROM requests")
    return {
        "base_url": f"http://127.0.0.1:{st.current.proxy_port}/v1",
        "port": st.current.proxy_port,
        "api_key": st.api_key,
        "api_key_masked": st.api_key_masked,
        "uptime_s": round(st.uptime_s(), 1),
        "requests_total": (row["n"] if row else 0) + len(live.in_flight),
        "active_clients": live.active_clients(),
    }


@router.post("/proxy/key")
async def regenerate_key(request: Request):
    key = request.app.state.settings.regenerate_api_key()
    return {"api_key": key,
            "api_key_masked": request.app.state.settings.api_key_masked}


@router.post("/logs/frontend")
async def ingest_frontend_logs(request: Request, batch: FrontendLogBatch):
    """Everything the dashboard does lands in the server log stream + DB."""
    now = time.time()
    rows = []
    for ev in batch.events:
        ts = ev.ts or now
        rows.append((ts, ev.level, ev.event, ev.detail))
        getattr(felog, "warning" if ev.level in ("warn", "error") else "info")(
            ev.event, extra={"data": {"detail": ev.detail, "client_ts": ts}})
    await request.app.state.db.aexecutemany(
        "INSERT INTO frontend_logs (ts, level, event, detail) VALUES (?,?,?,?)",
        rows)
    return {"accepted": len(rows)}


@router.get("/logs/frontend")
async def recent_frontend_logs(request: Request, limit: int = 100):
    return await request.app.state.db.aquery(
        "SELECT * FROM frontend_logs ORDER BY id DESC LIMIT ?",
        (min(limit, 1000),))
