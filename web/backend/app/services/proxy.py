"""The OpenAI-compatible forwarding hot path.

- resolves the upstream from live router state at call time
- strips caller auth, injects the target endpoint's upstream key
- non-streaming: captures usage from the JSON body, returns it verbatim
- streaming: tees raw SSE bytes to the client unchanged while stamping TTFT
  at the first chunk and parsing the trailing ``usage`` object
- injects ``stream_options.include_usage`` (toggleable) so upstreams emit
  real token counts on streams
- passive failure signals feed the router; transparent retry on another
  endpoint happens only before the first byte reaches the client
- telemetry is recorded off the hot path via the async writer
"""

import asyncio
import json
import time
import uuid

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from app.core.logging import get_logger
from app.security import extract_bearer, mask_key
from app.services.telemetry import RequestRecord

log = get_logger("proxy")

# Hop-by-hop headers never forwarded either direction.
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
    "content-length", "authorization", "x-admin-token",
}
_BODY_LIMIT = 100_000  # chars kept per body when log_bodies is on
_TAIL_LIMIT = 65_536  # bytes of SSE tail kept for usage parsing


def _route_name(path: str) -> str:
    p = path.strip("/")
    if p == "chat/completions":
        return "chat.completions"
    if p in ("completions", "embeddings", "models"):
        return p
    return p or "unknown"


def _upstream_error(status: int, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": "relay_proxy_error",
                           "code": status}},
    )


def _parse_sse_tail(buf: bytes) -> tuple[dict | None, int]:
    """Extract the trailing usage object + count content chunks from an SSE
    byte buffer. Returns (usage|None, delta_chunks_seen_in_buffer)."""
    usage = None
    chunks = 0
    for raw_line in buf.split(b"\n"):
        line = raw_line.strip()
        if not line.startswith(b"data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == b"[DONE]":
            continue
        chunks += 1
        if b'"usage"' in payload:
            try:
                obj = json.loads(payload)
                if isinstance(obj.get("usage"), dict):
                    usage = obj["usage"]
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
    return usage, chunks


def _collect_stream_text(buf: bytes) -> str:
    """Best-effort completion text from buffered SSE (log_bodies mode)."""
    out: list[str] = []
    for raw_line in buf.split(b"\n"):
        line = raw_line.strip()
        if not line.startswith(b"data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == b"[DONE]":
            continue
        try:
            obj = json.loads(payload)
            for choice in obj.get("choices", []):
                delta = choice.get("delta") or choice.get("message") or {}
                if delta.get("content"):
                    out.append(delta["content"])
        except (json.JSONDecodeError, AttributeError, UnicodeDecodeError):
            continue
    return "".join(out)[:_BODY_LIMIT]


class ProxyService:
    def __init__(self, http: httpx.AsyncClient, router, telemetry, live,
                 settings, max_concurrency: int = 18):
        self.http = http
        self.router = router
        self.telemetry = telemetry
        self.live = live
        self.settings = settings
        self.max_concurrency = max_concurrency
        self._slots = asyncio.Semaphore(max_concurrency)

    # ---- entrypoint ------------------------------------------------------
    async def handle(self, request: Request, path: str) -> Response:
        t0 = time.perf_counter()
        started_ts = time.time()
        req_id = "req_" + uuid.uuid4().hex[:8]
        route = _route_name(path)
        client_key = mask_key(extract_bearer(request))

        if route == "models" and request.method == "GET":
            return self._models_catalog()

        body_bytes = await request.body()
        body_json = self._parse_json(body_bytes)

        # Model-alias routing: a request whose "model" matches an endpoint
        # alias is pinned to that endpoint; the model field is rewritten to
        # what that server actually runs (override or discovered).
        alias_endpoint: dict | None = None
        requested_model = (body_json or {}).get("model")
        if requested_model and requested_model != "auto":
            alias_endpoint = self.router.resolve_alias(requested_model)
            if alias_endpoint is not None and body_json is not None:
                upstream_model = self.router.upstream_model(
                    alias_endpoint["id"])
                if upstream_model:
                    body_json["model"] = upstream_model
                log.info("alias route", extra={"data": {
                    "id": req_id, "alias": requested_model,
                    "endpoint": alias_endpoint["name"],
                    "upstream_model": upstream_model or requested_model}})

        want_stream = bool(body_json.get("stream")) if body_json else False
        settings = self.settings.current
        if want_stream and not settings.stream_passthrough and body_json:
            body_json["stream"] = False
            want_stream = False
        if (want_stream and settings.inject_stream_usage and body_json is not None
                and "stream_options" not in body_json):
            body_json["stream_options"] = {"include_usage": True}
        if body_json is not None:
            body_bytes = json.dumps(body_json).encode()

        record = RequestRecord(
            id=req_id, ts=started_ts, route=route, stream=want_stream,
            client_key=client_key,
            model=(body_json or {}).get("model"),
            temperature=(body_json or {}).get("temperature"),
            max_tokens=(body_json or {}).get("max_tokens")
            or (body_json or {}).get("max_completion_tokens"),
        )
        if settings.log_bodies and body_json:
            record.prompt_body = json.dumps(
                body_json.get("messages") or body_json.get("prompt") or ""
            )[:_BODY_LIMIT]

        # Concurrency gate: queue (default) or reject with 429.
        if self._slots.locked() and not settings.queue_requests:
            log.warning("rejecting request; concurrency limit",
                        extra={"data": {"id": req_id, "route": route}})
            return _upstream_error(429, "concurrency limit reached")

        async with self._slots:
            return await self._dispatch(
                request, path, record, body_bytes, t0,
                force_endpoint=alias_endpoint)

    def _parse_json(self, body: bytes) -> dict | None:
        if not body:
            return None
        try:
            parsed = json.loads(body)
            return parsed if isinstance(parsed, dict) else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    def _models_catalog(self) -> JSONResponse:
        """Synthesized /v1/models: "auto" + every enabled endpoint alias, so
        OpenAI clients can discover the routing names (skynet, local, ...)."""
        created = int(time.time())
        data = [{"id": "auto", "object": "model", "created": created,
                 "owned_by": "relay",
                 "relay": {"routing": "active endpoint (hot-swap)"}}]
        for row in self.router.endpoints.values():
            if not row.get("alias") or not row["enabled"]:
                continue
            st = self.router.state.get(row["id"])
            data.append({
                "id": row["alias"], "object": "model", "created": created,
                "owned_by": f"relay:{row['name']}",
                "relay": {
                    "endpoint": row["name"],
                    "health": st.health if st else "unknown",
                    "upstream_model": self.router.upstream_model(row["id"]),
                },
            })
        return JSONResponse({"object": "list", "data": data})

    # ---- forwarding with pre-first-byte failover -------------------------
    async def _dispatch(self, request: Request, path: str,
                        record: RequestRecord, body: bytes,
                        t0: float, force_endpoint: dict | None = None
                        ) -> Response:
        settings = self.settings.current

        # Alias-pinned: the caller asked for THIS server by name; no silent
        # failover to a different one. Surface errors instead.
        if force_endpoint is not None:
            record.endpoint_id = force_endpoint["id"]
            record.endpoint_name = force_endpoint["name"]
            if not force_endpoint["enabled"]:
                record.error = "alias endpoint disabled"
                record.status = 503
                record.latency_ms = (time.perf_counter() - t0) * 1000
                self.telemetry.submit(record)
                return _upstream_error(
                    503, f"endpoint for model {force_endpoint['alias']!r}"
                    " is disabled")
            try:
                return await self._forward(
                    request, force_endpoint, path, record, body, t0)
            except _RetryableUpstreamError as e:
                self.router.report_failure(force_endpoint["id"], str(e))
                record.error = str(e)[:500]
                record.status = e.status or 502
                record.ok = False
                record.latency_ms = (time.perf_counter() - t0) * 1000
                self.telemetry.submit(record)
                log.warning("alias endpoint failed", extra={"data": {
                    "id": record.id, "endpoint": force_endpoint["name"],
                    "error": str(e)[:200]}})
                return _upstream_error(record.status, f"upstream error: {e}")

        tried: set[str] = set()
        attempts = 0
        while True:
            endpoint = self.router.resolve(
                exclude=tried, auto_failover=settings.auto_failover)
            if endpoint is None:
                record.error = "no healthy endpoint available"
                record.status = 503
                record.latency_ms = (time.perf_counter() - t0) * 1000
                self.telemetry.submit(record)
                log.error("no upstream available", extra={"data": {
                    "id": record.id, "route": record.route, "tried": len(tried)}})
                return _upstream_error(503, "no healthy upstream endpoint")
            attempts += 1
            tried.add(endpoint["id"])
            record.endpoint_id = endpoint["id"]
            record.endpoint_name = endpoint["name"]
            try:
                return await self._forward(
                    request, endpoint, path, record, body, t0)
            except _RetryableUpstreamError as e:
                self.router.report_failure(endpoint["id"], str(e))
                log.warning("upstream failed pre-first-byte", extra={"data": {
                    "id": record.id, "endpoint": endpoint["name"],
                    "error": str(e)[:200], "attempt": attempts}})
                if not settings.auto_failover or attempts >= 3:
                    record.error = str(e)[:500]
                    record.status = e.status or 502
                    record.ok = False
                    record.latency_ms = (time.perf_counter() - t0) * 1000
                    self.telemetry.submit(record)
                    return _upstream_error(
                        record.status, f"upstream error: {e}")

    async def _forward(self, request: Request, endpoint: dict, path: str,
                       record: RequestRecord, body: bytes,
                       t0: float) -> Response:
        url = endpoint["base_url"] + "/" + path.lstrip("/")
        headers = {
            k: v for k, v in request.headers.items()
            if k.lower() not in _HOP_BY_HOP
        }
        if endpoint.get("upstream_key"):
            headers["Authorization"] = f"Bearer {endpoint['upstream_key']}"

        upstream_req = self.http.build_request(
            request.method, url, headers=headers,
            content=body if body else None)

        log.info("forwarding", extra={"data": {
            "id": record.id, "route": record.route, "method": request.method,
            "endpoint": endpoint["name"], "model": record.model,
            "stream": record.stream}})

        try:
            resp = await self.http.send(upstream_req, stream=True)
        except httpx.HTTPError as e:
            raise _RetryableUpstreamError(f"{type(e).__name__}: {e}") from e

        if resp.status_code >= 500:
            text = (await resp.aread())[:300]
            await resp.aclose()
            raise _RetryableUpstreamError(
                f"HTTP {resp.status_code}: {text.decode(errors='replace')}",
                status=resp.status_code)

        if record.stream and resp.headers.get(
                "content-type", "").startswith("text/event-stream"):
            return self._stream_response(resp, endpoint, record, t0)
        return await self._buffered_response(resp, endpoint, record, t0)

    # ---- non-streaming --------------------------------------------------
    async def _buffered_response(self, resp: httpx.Response, endpoint: dict,
                                 record: RequestRecord, t0: float) -> Response:
        self.live.start(record.id, {
            "ts": record.ts, "model": record.model,
            "endpoint_name": endpoint["name"], "route": record.route,
            "stream": False, "client_key": record.client_key})
        try:
            content = await resp.aread()
        finally:
            await resp.aclose()
            self.live.finish(record.id)

        record.status = resp.status_code
        record.ok = resp.status_code < 400
        record.latency_ms = (time.perf_counter() - t0) * 1000
        if not record.ok:
            record.error = content[:300].decode(errors="replace")

        payload = self._parse_json(content)
        if payload:
            usage = payload.get("usage") or {}
            record.prompt_tokens = usage.get("prompt_tokens")
            record.completion_tokens = usage.get("completion_tokens")
            record.total_tokens = usage.get("total_tokens")
            record.model = payload.get("model") or record.model
            if record.completion_tokens and record.latency_ms:
                record.tokens_per_sec = round(
                    record.completion_tokens / (record.latency_ms / 1000), 2)
            if self.settings.current.log_bodies:
                try:
                    record.completion_body = json.dumps(
                        [c.get("message") or c.get("text")
                         for c in payload.get("choices", [])])[:_BODY_LIMIT]
                except (TypeError, ValueError):
                    pass
        record.cost_usd = self._cost(record)
        self.telemetry.submit(record)
        self.router.report_success(
            endpoint["id"], latency_ms=record.latency_ms)

        headers = {
            k: v for k, v in resp.headers.items()
            if k.lower() not in _HOP_BY_HOP
        }
        return Response(content=content, status_code=resp.status_code,
                        headers=headers)

    # ---- streaming (SSE tee) ---------------------------------------------
    def _stream_response(self, resp: httpx.Response, endpoint: dict,
                         record: RequestRecord, t0: float) -> StreamingResponse:
        self.live.start(record.id, {
            "ts": record.ts, "model": record.model,
            "endpoint_name": endpoint["name"], "route": record.route,
            "stream": True, "client_key": record.client_key})

        keep_bodies = self.settings.current.log_bodies

        async def tee():
            tail = b""
            body_buf = b""
            chunk_count = 0
            error: str | None = None
            try:
                async for chunk in resp.aiter_raw():
                    if record.ttft_ms is None:
                        record.ttft_ms = (time.perf_counter() - t0) * 1000
                        self.live.set_ttft(record.id, record.ttft_ms)
                    tail = (tail + chunk)[-_TAIL_LIMIT:]
                    if keep_bodies:
                        body_buf = (body_buf + chunk)[-4 * _TAIL_LIMIT:]
                    # cheap live progress: count SSE events in this chunk
                    chunk_count += chunk.count(b"data:")
                    self.live.bump(record.id, chunk_count)
                    yield chunk
            except httpx.HTTPError as e:
                error = f"{type(e).__name__}: {e}"
                raise
            except (GeneratorExit, asyncio.CancelledError):
                error = "client disconnected"
                raise
            finally:
                await resp.aclose()
                self.live.finish(record.id)
                self._finalize_stream(
                    resp, endpoint, record, t0, tail, body_buf,
                    chunk_count, error)

        headers = {
            k: v for k, v in resp.headers.items()
            if k.lower() not in _HOP_BY_HOP
        }
        return StreamingResponse(
            tee(), status_code=resp.status_code, headers=headers,
            media_type=resp.headers.get("content-type", "text/event-stream"))

    def _finalize_stream(self, resp, endpoint, record, t0, tail, body_buf,
                         chunk_count, error) -> None:
        record.status = resp.status_code
        record.latency_ms = (time.perf_counter() - t0) * 1000
        record.error = error
        record.ok = error is None and resp.status_code < 400

        usage, _ = _parse_sse_tail(tail)
        if usage:
            record.prompt_tokens = usage.get("prompt_tokens")
            record.completion_tokens = usage.get("completion_tokens")
            record.total_tokens = usage.get("total_tokens")
        elif chunk_count:
            # no usage from upstream: approximate one token per SSE event
            record.completion_tokens = chunk_count
        gen_ms = (record.latency_ms or 0) - (record.ttft_ms or 0)
        if record.completion_tokens and gen_ms > 0:
            record.tokens_per_sec = round(
                record.completion_tokens / (gen_ms / 1000), 2)
        if body_buf:
            record.completion_body = _collect_stream_text(body_buf)
        record.cost_usd = self._cost(record)
        self.telemetry.submit(record)
        if record.ok:
            self.router.report_success(
                endpoint["id"], latency_ms=record.ttft_ms)
        else:
            self.router.report_failure(endpoint["id"], record.error or "stream error")
        log.info("stream finished", extra={"data": {
            "id": record.id, "endpoint": endpoint["name"], "ok": record.ok,
            "ttft_ms": round(record.ttft_ms or 0, 1),
            "latency_ms": round(record.latency_ms or 0, 1),
            "completion_tokens": record.completion_tokens,
            "tokens_per_sec": record.tokens_per_sec, "error": error}})

    # ---- pricing ----------------------------------------------------------
    def _cost(self, record: RequestRecord) -> float:
        prices = getattr(self.settings, "prices", {}) or {}
        entry = prices.get(record.model or "")
        if not entry:
            return 0.0
        pt = record.prompt_tokens or 0
        ct = record.completion_tokens or 0
        return round(pt / 1e6 * entry[0] + ct / 1e6 * entry[1], 6)


class _RetryableUpstreamError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status
