"""Control plane: runtime settings + proxy info + frontend log ingestion."""

import time

from fastapi import APIRouter, Request

from app.core.logging import get_logger
from app.schemas import FrontendLogBatch, RuntimeSettings, SettingsPatch

log = get_logger("admin")
felog = get_logger("frontend")

router = APIRouter(prefix="/admin", tags=["settings"])


@router.get("/settings", response_model=RuntimeSettings)
async def get_settings(request: Request):
    return request.app.state.settings.current


@router.put("/settings", response_model=RuntimeSettings)
async def put_settings(request: Request, patch: SettingsPatch):
    return request.app.state.settings.update(patch)


def _db_size_bytes(db_path) -> int:
    """On-disk footprint = main db + WAL + shared-memory index."""
    from pathlib import Path
    base = Path(db_path)
    total = 0
    for suffix in ("", "-wal", "-shm"):
        p = base.with_name(base.name + suffix)
        if p.exists():
            total += p.stat().st_size
    return total


@router.get("/proxy")
async def proxy_info(request: Request):
    st = request.app.state.settings
    live = request.app.state.live
    row = await request.app.state.db.aquery_one(
        "SELECT COUNT(*) AS n FROM requests")
    # Built from the Host header the dashboard was loaded over, not from
    # 127.0.0.1: the copy button on the Proxy screen should hand you a URL
    # that works from the machine you are reading it on.
    host = (request.headers.get("host") or "").split(",")[0].strip()
    origin = f"{request.url.scheme}://{host}" if host else str(
        request.base_url).rstrip("/")
    return {
        "base_url": f"{origin}/v1",
        "port": st.current.proxy_port,
        "uptime_s": round(st.uptime_s(), 1),
        "requests_total": (row["n"] if row else 0) + len(live.in_flight),
        "active_clients": live.active_clients(),
        "db_size_bytes": _db_size_bytes(request.app.state.cfg.db_path),
    }


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
