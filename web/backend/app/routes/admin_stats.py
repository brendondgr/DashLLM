"""Read plane: ECharts-shaped aggregates the dashboard polls."""

from fastapi import APIRouter, Depends, Query, Request

from app.security import admin_guard

router = APIRouter(
    prefix="/admin/stats", tags=["stats"], dependencies=[Depends(admin_guard)]
)


def _params(
    window: str | None = Query(default="24h"),
    from_ts: float | None = Query(default=None, alias="from"),
    to_ts: float | None = Query(default=None, alias="to"),
    endpoint_id: str | None = None,
    model: str | None = None,
) -> dict:
    return {"window": window, "from_ts": from_ts, "to_ts": to_ts,
            "endpoint_id": endpoint_id, "model": model}


@router.get("/summary")
async def summary(request: Request, p: dict = Depends(_params)):
    return await request.app.state.stats.summary(**p)


@router.get("/volume")
async def volume(request: Request, p: dict = Depends(_params),
                 detail: str = Query(default="summary")):
    return await request.app.state.stats.volume(**p, detail=detail)


@router.get("/tokens/timeseries")
async def tokens_timeseries(request: Request, p: dict = Depends(_params),
                            detail: str = Query(default="summary")):
    return await request.app.state.stats.tokens_timeseries(**p, detail=detail)


@router.get("/tokens/by-hour")
async def tokens_by_hour(request: Request, p: dict = Depends(_params)):
    return await request.app.state.stats.tokens_by_hour(**p)


@router.get("/tokens/by-day")
async def tokens_by_day(request: Request, p: dict = Depends(_params)):
    return await request.app.state.stats.tokens_by_day(**p)


@router.get("/by-model")
async def by_model(request: Request, p: dict = Depends(_params)):
    return await request.app.state.stats.by_model(**p)


@router.get("/by-endpoint")
async def by_endpoint(request: Request, p: dict = Depends(_params)):
    return await request.app.state.stats.by_endpoint(**p)


@router.get("/latency")
async def latency(request: Request, p: dict = Depends(_params)):
    return await request.app.state.stats.latency(**p)


@router.get("/recent")
async def recent(request: Request, limit: int = 90):
    return await request.app.state.stats.recent(limit)


@router.get("/live")
async def live(request: Request):
    return request.app.state.live.snapshot(
        request.app.state.cfg.max_concurrency)
