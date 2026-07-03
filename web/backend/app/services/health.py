"""Active health probing: GET {base_url}/models on an interval.

Feeds the router's health state machine and discovers served model names.
Also powers the dashboard's on-demand "Test connection" button.
"""

import asyncio
import time

import httpx

from app.core.logging import get_logger
from app.schemas import EndpointTestResult
from app.services.router import Router

log = get_logger("health")


def _model_ids(payload) -> list[str]:
    try:
        return [m.get("id", "?") for m in payload.get("data", [])]
    except AttributeError:
        return []


class HealthProber:
    def __init__(self, router: Router, http: httpx.AsyncClient,
                 interval: float = 15.0, timeout: float = 5.0):
        self.router = router
        self.http = http
        self.interval = interval
        self.timeout = timeout
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="health-prober")
        log.info("health prober started",
                 extra={"data": {"interval_s": self.interval}})

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        log.info("health prober stopped")

    async def _run(self) -> None:
        # First sweep immediately so a fresh boot gets health fast.
        while True:
            await self.probe_all()
            await asyncio.sleep(self.interval)

    async def probe_all(self) -> None:
        ids = [
            eid for eid, row in self.router.endpoints.items() if row["enabled"]
        ]
        if ids:
            await asyncio.gather(*(self._probe(eid) for eid in ids))

    async def _probe(self, eid: str) -> None:
        result = await self.probe_endpoint(eid)
        if result is None:
            return
        if result.ok:
            self.router.report_success(
                eid, latency_ms=result.latency_ms, source="probe")
            if result.models:
                self.router.set_models(eid, result.models)
        else:
            self.router.report_failure(
                eid, result.error or "probe failed", source="probe")

    async def probe_endpoint(self, eid: str) -> EndpointTestResult | None:
        """One live probe; also the handler for POST /admin/endpoints/{id}/test."""
        row = self.router.endpoints.get(eid)
        if row is None:
            return None
        url = row["base_url"] + "/models"
        headers = {}
        if row["upstream_key"]:
            headers["Authorization"] = f"Bearer {row['upstream_key']}"
        t0 = time.perf_counter()
        try:
            resp = await self.http.get(url, headers=headers, timeout=self.timeout)
            latency = (time.perf_counter() - t0) * 1000
            if resp.status_code == 200:
                models = _model_ids(resp.json())
                log.debug("probe ok", extra={"data": {
                    "endpoint": row["name"], "latency_ms": round(latency, 1),
                    "models": len(models)}})
                return EndpointTestResult(
                    ok=True, latency_ms=round(latency, 1), models=models)
            return EndpointTestResult(
                ok=False, latency_ms=round(latency, 1),
                error=f"HTTP {resp.status_code}")
        except httpx.HTTPError as e:
            latency = (time.perf_counter() - t0) * 1000
            return EndpointTestResult(
                ok=False, latency_ms=round(latency, 1),
                error=f"{type(e).__name__}: {e}")
