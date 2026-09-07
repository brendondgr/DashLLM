"""The OpenAI-compatible adapter: forward verbatim, probe ``GET /models``.

This is relay's original behavior, extracted unchanged so the protocol seam
could be introduced without altering the hot path for every existing endpoint.
Nothing here is OpenCode-aware.
"""

import time

import httpx
from fastapi import Request
from fastapi.responses import Response

from app.core.logging import get_logger
from app.schemas import EndpointTestResult
from app.services.adapters.base import (
    HOP_BY_HOP,
    RetryableUpstreamError,
    StaleModelOverride,
    UpstreamAdapter,
    classify_httpx_error,
)
from app.services.telemetry import RequestRecord

log = get_logger("health")


def _model_ids(payload) -> list[str]:
    try:
        return [m.get("id", "?") for m in payload.get("data", [])]
    except AttributeError:
        return []


class PassthroughAdapter(UpstreamAdapter):
    name = "openai"

    async def probe(self, http: httpx.AsyncClient, endpoint: dict,
                    timeout: float) -> EndpointTestResult:
        url = endpoint["base_url"] + "/models"
        headers = {}
        if endpoint.get("upstream_key"):
            headers["Authorization"] = f"Bearer {endpoint['upstream_key']}"
        t0 = time.perf_counter()
        try:
            resp = await http.get(url, headers=headers, timeout=timeout)
            latency = (time.perf_counter() - t0) * 1000
            if resp.status_code == 200:
                models = _model_ids(resp.json())
                log.debug("probe ok", extra={"data": {
                    "endpoint": endpoint["name"],
                    "latency_ms": round(latency, 1), "models": len(models)}})
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

    async def forward(self, proxy, request: Request, endpoint: dict, path: str,
                      record: RequestRecord, body: bytes,
                      t0: float) -> Response:
        url = endpoint["base_url"] + "/" + path.lstrip("/")
        headers = {
            k: v for k, v in request.headers.items()
            if k.lower() not in HOP_BY_HOP
        }
        if endpoint.get("upstream_key"):
            headers["Authorization"] = f"Bearer {endpoint['upstream_key']}"

        upstream_req = proxy.http.build_request(
            request.method, url, headers=headers,
            content=body if body else None)

        try:
            resp = await proxy.http.send(upstream_req, stream=True)
        except httpx.HTTPError as e:
            raise classify_httpx_error(e) from e

        if resp.status_code >= 500:
            text = (await resp.aread())[:1000]
            await resp.aclose()
            if proxy._is_stale_override(endpoint, record, text):
                raise StaleModelOverride(endpoint["model_override"])
            raise RetryableUpstreamError(
                f"HTTP {resp.status_code}: "
                f"{text[:300].decode(errors='replace')}",
                status=resp.status_code)

        if record.stream and resp.headers.get(
                "content-type", "").startswith("text/event-stream"):
            return proxy._stream_response(resp, endpoint, record, t0)
        return await proxy._buffered_response(resp, endpoint, record, t0)
