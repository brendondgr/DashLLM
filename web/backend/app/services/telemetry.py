"""Telemetry capture: one row per proxied request, written off the hot path.

- ``RequestRecord``: the finished-request row (metadata only by default).
- ``TelemetryWriter``: asyncio queue -> batched SQLite inserts + retention
  pruning; the proxy never waits on a disk write.
- ``LiveTracker``: in-memory registry of in-flight requests + a rolling
  concurrency series for the live dashboard widgets.
"""

import asyncio
import time
from collections import deque

from pydantic import BaseModel

from app.core.logging import get_logger
from app.db import Database

log = get_logger("telemetry")

_STOP = object()


class RequestRecord(BaseModel):
    id: str
    ts: float
    endpoint_id: str | None = None
    endpoint_name: str | None = None
    route: str = "unknown"
    model: str | None = None
    client_key: str | None = None  # masked (…last4)
    stream: bool = False
    status: int | None = None
    ok: bool = False
    error: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    ttft_ms: float | None = None
    latency_ms: float | None = None
    tokens_per_sec: float | None = None
    cost_usd: float | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    # bodies are only persisted when the log_bodies setting is on
    prompt_body: str | None = None
    completion_body: str | None = None


_INSERT = """
INSERT OR REPLACE INTO requests (
  id, ts, endpoint_id, endpoint_name, route, model, client_key, stream,
  status, ok, error, prompt_tokens, completion_tokens, total_tokens,
  ttft_ms, latency_ms, tokens_per_sec, cost_usd, temperature, max_tokens
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""

_INSERT_BODY = (
    "INSERT OR REPLACE INTO request_bodies (id, prompt, completion) VALUES (?,?,?)"
)


def _row(r: RequestRecord) -> tuple:
    return (
        r.id, r.ts, r.endpoint_id, r.endpoint_name, r.route, r.model,
        r.client_key, int(r.stream), r.status, int(r.ok), r.error,
        r.prompt_tokens, r.completion_tokens, r.total_tokens, r.ttft_ms,
        r.latency_ms, r.tokens_per_sec, r.cost_usd, r.temperature,
        r.max_tokens,
    )


class TelemetryWriter:
    def __init__(self, db: Database):
        self.db = db
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=10_000)
        self._task: asyncio.Task | None = None
        self._last_prune = 0.0
        self.written = 0

    def submit(self, record: RequestRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except asyncio.QueueFull:  # pragma: no cover - backpressure guard
            log.error("telemetry queue full; dropping record",
                      extra={"data": {"id": record.id}})

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="telemetry-writer")
        log.info("telemetry writer started")

    async def stop(self) -> None:
        if self._task:
            await self.queue.put(_STOP)
            await self._task
            self._task = None
        log.info("telemetry writer stopped",
                 extra={"data": {"written": self.written}})

    async def flush(self) -> None:
        """Wait until everything queued so far is on disk (used by tests)."""
        await self.queue.join()

    async def _run(self) -> None:
        while True:
            item = await self.queue.get()
            if item is _STOP:
                self.queue.task_done()
                return
            batch = [item]
            while not self.queue.empty() and len(batch) < 100:
                nxt = self.queue.get_nowait()
                if nxt is _STOP:
                    await self._write(batch)
                    self.queue.task_done()
                    for _ in batch:
                        self.queue.task_done()
                    return
                batch.append(nxt)
            await self._write(batch)
            for _ in batch:
                self.queue.task_done()

    async def _write(self, batch: list[RequestRecord]) -> None:
        try:
            await self.db.aexecutemany(_INSERT, [_row(r) for r in batch])
            bodies = [
                (r.id, r.prompt_body, r.completion_body)
                for r in batch
                if r.prompt_body is not None or r.completion_body is not None
            ]
            if bodies:
                await self.db.aexecutemany(_INSERT_BODY, bodies)
            self.written += len(batch)
            for r in batch:
                log.debug("recorded request", extra={"data": {
                    "id": r.id, "route": r.route, "model": r.model,
                    "endpoint": r.endpoint_name, "ok": r.ok,
                    "status": r.status, "tokens": r.total_tokens,
                    "ttft_ms": r.ttft_ms, "latency_ms": r.latency_ms,
                }})
        except Exception:
            log.exception("telemetry write failed",
                          extra={"data": {"batch": len(batch)}})

    async def prune(self, retention_days: int) -> int:
        """Delete rows past retention. Called periodically from the app."""
        cutoff = time.time() - retention_days * 86400
        n = await self.db.aexecute("DELETE FROM requests WHERE ts < ?", (cutoff,))
        await self.db.aexecute(
            "DELETE FROM request_bodies WHERE id NOT IN (SELECT id FROM requests)"
        )
        await self.db.aexecute(
            "DELETE FROM frontend_logs WHERE ts < ?", (cutoff,)
        )
        if n:
            log.info("pruned telemetry", extra={"data": {
                "rows": n, "retention_days": retention_days}})
        self._last_prune = time.time()
        return n


class LiveTracker:
    """In-flight request registry + rolling concurrency series."""

    def __init__(self, max_points: int = 240):
        self.in_flight: dict[str, dict] = {}
        self.series: deque[list] = deque(maxlen=max_points)
        self.requests_total = 0
        self.clients: dict[str, float] = {}  # masked key -> last seen ts

    def start(self, req_id: str, info: dict) -> None:
        self.in_flight[req_id] = {
            **info, "id": req_id, "state": "streaming",
            "completion_tokens": info.get("completion_tokens", 0),
        }
        self.requests_total += 1
        key = info.get("client_key")
        if key:
            self.clients[key] = time.time()
        self._sample()

    def update(self, req_id: str, info: dict) -> None:
        """Fill in fields that are only known after the request is under way
        (e.g. the resolved endpoint once failover/aliasing has picked one)."""
        row = self.in_flight.get(req_id)
        if row is not None:
            row.update(info)

    def bump(self, req_id: str, completion_tokens: int) -> None:
        row = self.in_flight.get(req_id)
        if row is not None:
            row["completion_tokens"] = completion_tokens

    def set_ttft(self, req_id: str, ttft_ms: float) -> None:
        row = self.in_flight.get(req_id)
        if row is not None:
            row["ttft_ms"] = ttft_ms

    def finish(self, req_id: str) -> None:
        self.in_flight.pop(req_id, None)
        self._sample()

    def _sample(self) -> None:
        now_ms = time.time() * 1000
        n = len(self.in_flight)
        if self.series and self.series[-1][0] >= now_ms:
            self.series[-1][1] = n
        else:
            self.series.append([now_ms, n])

    def sample(self) -> None:
        """Periodic sampler tick (keeps the series moving when idle)."""
        self._sample()

    def active_clients(self, window_s: float = 3600) -> int:
        cutoff = time.time() - window_s
        return sum(1 for ts in self.clients.values() if ts > cutoff)

    def snapshot(self, max_concurrency: int) -> dict:
        return {
            "in_flight": len(self.in_flight),
            "max_concurrency": max_concurrency,
            "series": [list(p) for p in self.series],
        }
