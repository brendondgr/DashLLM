"""Read plane: ECharts-shaped aggregates the dashboard polls.

Guarded by ``user_guard``, not ``admin_guard`` — every logged-in account can see
this plane. What differs is *scope*:

- **Aggregate charts** stay global for everyone. That is the attribution-only
  privacy model: a private user's traffic still counts toward shared totals,
  it is just never broken out as theirs. ``scope=me`` narrows a user's own
  charts to their own rows.
- **Row-level detail** (``/recent``, ``/live``) is always self-scoped for a
  non-admin, whatever ``scope`` says. One user must never see another's models,
  endpoints, or timings.
"""

from fastapi import APIRouter, Depends, Query, Request

from app.security import Principal, user_guard

router = APIRouter(
    prefix="/admin/stats", tags=["stats"], dependencies=[Depends(user_guard)]
)


def _params(
    window: str | None = Query(default="24h"),
    from_ts: float | None = Query(default=None, alias="from"),
    to_ts: float | None = Query(default=None, alias="to"),
    endpoint_id: str | None = None,
    model: str | None = None,
    scope: str = Query(default="all"),
    principal: Principal = Depends(user_guard),
) -> dict:
    # An admin asking for "me" has no rows of their own, so it resolves to
    # unrestricted; a user asking for "all" gets the shared aggregate.
    user_id = principal.scope_user_id if scope == "me" else None
    return {"window": window, "from_ts": from_ts, "to_ts": to_ts,
            "endpoint_id": endpoint_id, "model": model, "user_id": user_id}


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
async def recent(request: Request, limit: int = 90,
                 principal: Principal = Depends(user_guard)):
    return await request.app.state.stats.recent(
        limit, user_id=principal.scope_user_id)


@router.get("/live")
async def live(request: Request, principal: Principal = Depends(user_guard)):
    return request.app.state.live.snapshot(
        request.app.state.cfg.max_concurrency,
        user_id=principal.scope_user_id)
