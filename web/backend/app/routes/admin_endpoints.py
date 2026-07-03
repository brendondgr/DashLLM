"""Control plane: endpoint CRUD, hot-swap, live tests, router policy."""

import time

from fastapi import APIRouter, Depends, HTTPException, Request

from app.core.logging import get_logger
from app.schemas import (
    EndpointCreate,
    EndpointOut,
    EndpointPatch,
    EndpointTestResult,
    RouterState,
    RouterUpdate,
)
from app.security import admin_guard

log = get_logger("admin")

router = APIRouter(
    prefix="/admin", tags=["endpoints"], dependencies=[Depends(admin_guard)]
)


async def _shares(request: Request) -> dict[str, float]:
    """Each endpoint's fraction of requests over the last 24h."""
    rows = await request.app.state.db.aquery(
        "SELECT endpoint_id, COUNT(*) AS n FROM requests"
        " WHERE ts > ? GROUP BY endpoint_id", (time.time() - 86400,))
    total = sum(r["n"] for r in rows) or 1
    return {r["endpoint_id"]: r["n"] / total for r in rows if r["endpoint_id"]}


@router.get("/endpoints", response_model=list[EndpointOut])
async def list_endpoints(request: Request):
    return request.app.state.router.list_out(await _shares(request))


@router.post("/endpoints", response_model=EndpointOut, status_code=201)
async def create_endpoint(request: Request, spec: EndpointCreate):
    if not spec.base_url.startswith(("http://", "https://")):
        raise HTTPException(422, "base_url must start with http:// or https://")
    row = request.app.state.router.create(spec)
    # Probe immediately so the UI shows real health, not 'unknown'.
    await request.app.state.prober._probe(row["id"])
    return request.app.state.router.out(row["id"])


@router.patch("/endpoints/{eid}", response_model=EndpointOut)
async def patch_endpoint(request: Request, eid: str, patch: EndpointPatch):
    row = request.app.state.router.patch(eid, patch)
    if row is None:
        raise HTTPException(404, "endpoint not found")
    return request.app.state.router.out(eid)


@router.delete("/endpoints/{eid}", status_code=204)
async def delete_endpoint(request: Request, eid: str):
    if not request.app.state.router.delete(eid):
        raise HTTPException(404, "endpoint not found")


@router.post("/endpoints/{eid}/activate", response_model=RouterState)
async def activate_endpoint(request: Request, eid: str):
    if not request.app.state.router.activate(eid):
        raise HTTPException(404, "endpoint not found")
    return request.app.state.router.router_state(
        request.app.state.settings.current.auto_failover)


@router.post("/endpoints/{eid}/test", response_model=EndpointTestResult)
async def test_endpoint(request: Request, eid: str):
    result = await request.app.state.prober.probe_endpoint(eid)
    if result is None:
        raise HTTPException(404, "endpoint not found")
    log.info("manual endpoint test", extra={"data": {
        "id": eid, "ok": result.ok, "latency_ms": result.latency_ms}})
    # Feed the manual test into the health state machine too.
    if result.ok:
        request.app.state.router.report_success(
            eid, result.latency_ms, source="probe")
        if result.models:
            request.app.state.router.set_models(eid, result.models)
    else:
        request.app.state.router.report_failure(
            eid, result.error or "test failed", source="probe")
    return result


@router.get("/endpoints/health")
async def pool_health(request: Request):
    r = request.app.state.router
    return {
        "endpoints": [
            {"id": eid, "name": r.endpoints[eid]["name"],
             "health": st.health, "consecutive_fails": st.consecutive_fails,
             "ewma_latency_ms": st.ewma_latency_ms, "model": st.model}
            for eid, st in r.state.items()
        ],
        "resolved": (r.resolve(
            auto_failover=request.app.state.settings.current.auto_failover
        ) or {}).get("id"),
    }


@router.get("/router", response_model=RouterState)
async def get_router(request: Request):
    return request.app.state.router.router_state(
        request.app.state.settings.current.auto_failover)


@router.put("/router", response_model=RouterState)
async def put_router(request: Request, update: RouterUpdate):
    try:
        request.app.state.router.set_policy(update.policy, update.pinned_id)
    except KeyError:
        raise HTTPException(404, "pinned endpoint not found")
    return request.app.state.router.router_state(
        request.app.state.settings.current.auto_failover)
