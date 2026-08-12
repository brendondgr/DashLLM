"""Active health probing on an interval.

*What* a probe is depends on the endpoint's protocol — ``GET /models`` for an
OpenAI-compatible server, ``/global/health`` + ``/config/providers`` for
OpenCode — so the request itself lives in the adapter. This module owns the
cadence, the state-machine feed, and model discovery, all protocol-agnostic.

Also powers the dashboard's on-demand "Test connection" button.
"""

import asyncio

import httpx

from app.core.logging import get_logger
from app.schemas import EndpointTestResult
from app.services.adapters import get_adapter
from app.services.router import Router

log = get_logger("health")


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
        await self.probe_and_report(eid)

    async def probe_and_report(self, eid: str):
        """One probe, fed into the health state machine and model discovery.

        The sweep uses this, and so does boot-time OpenCode discovery — a
        probe that quietly skipped the state machine would leave a freshly
        registered endpoint reading "unknown" no matter how it answered.
        """
        result = await self.probe_endpoint(eid)
        if result is None:
            return None
        if result.ok:
            self.router.report_success(
                eid, latency_ms=result.latency_ms, source="probe")
            if result.models:
                self.router.set_models(eid, result.models)
        else:
            self.router.report_failure(
                eid, result.error or "probe failed", source="probe")
        return result

    async def probe_endpoint(self, eid: str) -> EndpointTestResult | None:
        """One live probe; also the handler for POST /admin/endpoints/{id}/test."""
        row = self.router.endpoints.get(eid)
        if row is None:
            return None
        return await get_adapter(row).probe(self.http, row, self.timeout)
