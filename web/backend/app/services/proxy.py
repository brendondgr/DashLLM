"""The OpenAI-compatible forwarding hot path.

- resolves the upstream from live router state at call time
- dispatches the actual attempt through the endpoint's protocol adapter
  (``services/adapters/``); everything around it — the concurrency gate,
  alias pinning, failover, telemetry — is protocol-agnostic and lives here
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
from app.security import extract_bearer, mask_key, redact
from app.services.adapters import (
    HOP_BY_HOP as _HOP_BY_HOP,
    RetryableUpstreamError as _RetryableUpstreamError,
    StaleModelOverride as _StaleModelOverride,
    get_adapter,
)
from app.services.telemetry import RequestRecord

log = get_logger("proxy")

_BODY_LIMIT = 100_000  # chars kept per body when log_bodies is on
_TAIL_LIMIT = 65_536  # bytes of SSE tail kept for usage parsing

# Substrings upstreams use when the requested model id isn't served. Used to
# self-heal a stale model_override after the model on a port is swapped out.
_UNKNOWN_MODEL_MARKERS = (
    "does not exist", "not found", "no such model", "unknown model",
    "invalid model", "model_not_found", "unknown_model", "not available",
    "no models loaded",
)


def _has_unknown_model_marker(content: bytes) -> bool:
    text = content[:1000].decode(errors="replace").lower()
    return "model" in text and any(m in text for m in _UNKNOWN_MODEL_MARKERS)


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
                 settings, max_concurrency: int = 18, users=None):
        self.http = http
        self.router = router
        self.telemetry = telemetry
        self.live = live
        self.settings = settings
        # Injected like every other collaborator rather than read off
        # request.app, so the service stays usable with a bare ASGI scope.
        self.users = users
        self.max_concurrency = max_concurrency
        self._slots = asyncio.Semaphore(max_concurrency)

    # ---- entrypoint ------------------------------------------------------
    async def handle(self, request: Request, path: str) -> Response:
        t0 = time.perf_counter()
        started_ts = time.time()
        req_id = "req_" + uuid.uuid4().hex[:8]
        route = _route_name(path)
        raw_key = extract_bearer(request)
        client_key = mask_key(raw_key)
        # Attribution is best-effort by design: an unknown or absent key is not
        # an error, it just leaves the request in the general population. That
        # also denies an attacker an oracle for probing which keys are live.
        owner = self.users.by_api_key(raw_key) if self.users else None
        user_id = owner["id"] if owner else None

        if route == "models" and request.method == "GET":
            return self._models_catalog()

        body_bytes = await request.body()
        body_json = self._parse_json(body_bytes)
        if body_json is not None:
            # Credential-looking keys never survive the front door: relay
            # persists bodies to request_bodies and re-serializes them to the
            # upstream, so a client improvising {"user_pass": …} would
            # otherwise write its password to disk and ship it to every model
            # server and agent host in the pool.
            body_json = redact(body_json)

        # Model routing: a request whose "model" names an endpoint alias, or a
        # model in an endpoint's declared allowlist, is pinned to that
        # endpoint. An alias is rewritten to whatever that server actually
        # runs; an allowlisted model id is already real and goes through as-is.
        alias_endpoint: dict | None = None
        pinned_model: str | None = None
        requested_model = (body_json or {}).get("model")
        if requested_model and requested_model != "auto":
            alias_endpoint, pinned_model = self.router.resolve_request_model(
                requested_model)
            if alias_endpoint is not None and body_json is not None:
                upstream_model = pinned_model or self.router.upstream_model(
                    alias_endpoint["id"])
                if upstream_model:
                    body_json["model"] = upstream_model
                log.info("alias route", extra={"data": {
                    "id": req_id, "alias": requested_model,
                    "endpoint": alias_endpoint["name"],
                    "exact_model": pinned_model is not None,
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
            client_key=client_key, user_id=user_id,
            model=(body_json or {}).get("model"),
            temperature=(body_json or {}).get("temperature"),
            max_tokens=(body_json or {}).get("max_tokens")
            or (body_json or {}).get("max_completion_tokens"),
        )
        if settings.log_bodies and body_json:
            record.prompt_body = json.dumps(
                body_json.get("messages") or body_json.get("prompt") or ""
            )[:_BODY_LIMIT]

        # "auto" (the advertised default) is rewritten per resolved endpoint
        # at forward time — upstreams don't know a model called "auto".
        rewrite_auto = requested_model == "auto" and body_json is not None
        # Alias routes also keep the parsed body around so the model can be
        # re-resolved per attempt — needed to retry with the discovered model
        # if a stale model_override is rejected upstream.
        dispatch_body_json = (
            body_json if (rewrite_auto or alias_endpoint is not None) else None)

        # Count the request as in-flight the moment it is accepted — before it
        # waits on a concurrency slot or on the upstream to start answering — so
        # the live concurrency reflects everything moving through the proxy
        # right now, not only requests the model server has already replied to.
        self.live.start(req_id, {
            "ts": started_ts, "model": record.model, "route": route,
            "stream": want_stream, "client_key": client_key,
            "user_id": user_id,
            "endpoint_name": None, "temperature": record.temperature,
            "max_tokens": record.max_tokens})
        stream_owns_finish = False
        try:
            # Concurrency gate: queue (default) or reject with 429.
            if self._slots.locked() and not settings.queue_requests:
                log.warning("rejecting request; concurrency limit",
                            extra={"data": {"id": req_id, "route": route}})
                return _upstream_error(429, "concurrency limit reached")
            async with self._slots:
                response = await self._dispatch(
                    request, path, record, body_bytes, t0,
                    force_endpoint=alias_endpoint,
                    body_json=dispatch_body_json,
                    pinned_model=pinned_model)
            # A streaming response stays in-flight until its stream ends; the
            # tee generator calls live.finish() itself. Everything else (a
            # buffered body or an error response) is done now.
            stream_owns_finish = isinstance(response, StreamingResponse)
            return response
        finally:
            if not stream_owns_finish:
                self.live.finish(req_id)

    def _parse_json(self, body: bytes) -> dict | None:
        if not body:
            return None
        try:
            parsed = json.loads(body)
            return parsed if isinstance(parsed, dict) else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    def _models_catalog(self) -> JSONResponse:
        """Synthesized /v1/models: "auto", every enabled endpoint alias, and
        every model an endpoint declares in its allowlist — so an OpenAI client
        discovers both the routing names (skynet, local, ...) and the concrete
        models it can ask for by name (anthropic/claude-sonnet-4-5, ...).

        Everything listed here is routable: sending any of these ids back as
        "model" reaches the endpoint that advertised it.
        """
        created = int(time.time())
        data = [{"id": "auto", "object": "model", "created": created,
                 "owned_by": "relay",
                 "relay": {"routing": "active endpoint (hot-swap)"}}]
        for row in self.router.endpoints.values():
            if not row["enabled"]:
                continue
            st = self.router.state.get(row["id"])
            health = st.health if st else "unknown"
            if row.get("alias"):
                data.append({
                    "id": row["alias"], "object": "model", "created": created,
                    "owned_by": f"relay:{row['name']}",
                    "relay": {
                        "endpoint": row["name"], "health": health,
                        "protocol": row.get("protocol") or "openai",
                        "upstream_model": self.router.upstream_model(
                            row["id"]),
                    },
                })
            for model in self.router.available_models(row["id"]):
                data.append({
                    "id": model, "object": "model", "created": created,
                    "owned_by": f"relay:{row['name']}",
                    "relay": {
                        "endpoint": row["name"], "health": health,
                        "protocol": row.get("protocol") or "openai",
                        "upstream_model": model,
                    },
                })
        return JSONResponse({"object": "list", "data": data})

    # ---- forwarding with pre-first-byte failover -------------------------
    async def _dispatch(self, request: Request, path: str,
                        record: RequestRecord, body: bytes,
                        t0: float, force_endpoint: dict | None = None,
                        body_json: dict | None = None,
                        pinned_model: str | None = None) -> Response:
        settings = self.settings.current

        def body_for(endpoint: dict) -> bytes:
            """Rewrite model=auto to the endpoint's real model (failover may
            pick endpoints running different models, so this is per-attempt).

            ``pinned_model`` — the caller named a model from the endpoint's
            allowlist — short-circuits that: it is already a real id and
            outranks the endpoint's model_override."""
            if body_json is None:
                return body
            upstream_model = pinned_model or self.router.upstream_model(
                endpoint["id"])
            if not upstream_model:
                return body
            record.model = upstream_model
            return json.dumps({**body_json, "model": upstream_model}).encode()

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
                return await self._forward_healing(
                    request, force_endpoint, path, record,
                    lambda: body_for(force_endpoint), t0)
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
                return await self._forward_healing(
                    request, endpoint, path, record,
                    lambda: body_for(endpoint), t0)
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

    @staticmethod
    def _is_stale_override(endpoint: dict, record: RequestRecord,
                           content: bytes) -> bool:
        """True when the upstream rejected THIS endpoint's model_override as an
        unknown model — i.e. the model we just sent was the pinned override and
        the server says it doesn't have it."""
        override = endpoint.get("model_override")
        return bool(
            override and record.model == override
            and _has_unknown_model_marker(content))

    async def _forward_healing(self, request: Request, endpoint: dict,
                               path: str, record: RequestRecord,
                               make_body, t0: float) -> Response:
        """Forward once; if the upstream rejects a stale model_override as an
        unknown model, clear the override and retry the SAME endpoint with the
        re-resolved (discovered) model. Never switches endpoints, so alias
        pinning still holds. Self-heals a model that was swapped out on a port
        without any manual reconfiguration."""
        try:
            return await self._forward(
                request, endpoint, path, record, make_body(), t0)
        except _StaleModelOverride as stale:
            removed = self.router.clear_model_override(endpoint["id"])
            new_model = self.router.upstream_model(endpoint["id"])
            log.warning("healing stale model_override; retrying", extra={
                "data": {"id": record.id, "endpoint": endpoint["name"],
                         "removed": removed or stale.model,
                         "retry_model": new_model}})
            # Override is gone now, so make_body() resolves the discovered
            # model. Guard: if nothing usable was discovered, surface the
            # rejection through the normal failure path (records telemetry;
            # the auto path may then try another endpoint) rather than
            # resending the dead model.
            if not new_model or new_model == stale.model:
                raise _RetryableUpstreamError(
                    f"model {stale.model!r} no longer served by "
                    f"{endpoint['name']!r}; no replacement discovered",
                    status=502)
            return await self._forward(
                request, endpoint, path, record, make_body(), t0)

    async def _forward(self, request: Request, endpoint: dict, path: str,
                       record: RequestRecord, body: bytes,
                       t0: float) -> Response:
        adapter = get_adapter(endpoint)

        # A protocol that can't serve this route says so once, here — better
        # than forwarding into a shape the upstream has no handler for and
        # surfacing whatever 404 it happens to return.
        if not adapter.supports(record.route):
            record.status = 501
            record.ok = False
            record.error = f"{adapter.name} endpoint does not serve {record.route}"
            record.latency_ms = (time.perf_counter() - t0) * 1000
            self.telemetry.submit(record)
            return _upstream_error(
                501, f"endpoint {endpoint['name']!r} speaks {adapter.name}, "
                f"which does not implement {record.route}")

        log.info("forwarding", extra={"data": {
            "id": record.id, "route": record.route, "method": request.method,
            "endpoint": endpoint["name"], "model": record.model,
            "protocol": adapter.name, "stream": record.stream}})
        # Now that an endpoint is chosen (post-alias/failover), reflect it on
        # the in-flight row so the live Requests view shows where it's going.
        self.live.update(record.id, {
            "endpoint_name": endpoint["name"], "model": record.model})

        return await adapter.forward(
            self, request, endpoint, path, record, body, t0)

    # ---- non-streaming --------------------------------------------------
    async def _buffered_response(self, resp: httpx.Response, endpoint: dict,
                                 record: RequestRecord, t0: float) -> Response:
        # In-flight tracking is owned by handle(); this path just reads + closes.
        try:
            content = await resp.aread()
        finally:
            await resp.aclose()

        # A 4xx that names an unknown model + a pinned override = the model was
        # swapped out on this port. Signal the dispatch layer to self-heal
        # (clear the override, retry with the discovered model) before this
        # attempt is recorded as a failure.
        if (400 <= resp.status_code < 500
                and self._is_stale_override(endpoint, record, content)):
            raise _StaleModelOverride(endpoint["model_override"])

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

    # ---- adapter-produced replies ----------------------------------------
    def finalize_adapter_response(
            self, endpoint: dict, record: RequestRecord, t0: float,
            status: int, payload: dict, cost_usd: float | None = None) -> None:
        """Telemetry + router signals for a reply an adapter assembled itself.

        The non-passthrough protocols never produce an ``httpx.Response``, so
        they can't go through ``_buffered_response`` — but the recording side
        of a request must stay identical across protocols or the dashboard
        would show different columns depending on the upstream. This is that
        shared call site.
        """
        record.status = status
        record.ok = status < 400
        record.latency_ms = (time.perf_counter() - t0) * 1000
        if record.completion_tokens and record.latency_ms:
            record.tokens_per_sec = round(
                record.completion_tokens / (record.latency_ms / 1000), 2)
        if self.settings.current.log_bodies and record.completion_body is None:
            try:
                record.completion_body = json.dumps(
                    [c.get("message") for c in payload.get("choices", [])]
                )[:_BODY_LIMIT]
            except (TypeError, ValueError):
                pass
        record.cost_usd = self._cost(record, cost_usd)
        self.telemetry.submit(record)
        if record.ok:
            self.router.report_success(
                endpoint["id"], latency_ms=record.latency_ms)
        else:
            self.router.report_failure(
                endpoint["id"], record.error or f"HTTP {status}")

    # ---- streaming (SSE tee) ---------------------------------------------
    def _stream_response(self, resp: httpx.Response, endpoint: dict,
                         record: RequestRecord, t0: float) -> StreamingResponse:
        # in-flight tracking began in handle(); the tee below owns live.finish().
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
    def _cost(self, record: RequestRecord,
              upstream_cost: float | None = None) -> float:
        # An upstream that prices the turn itself (OpenCode reports USD on
        # info.cost) is authoritative — it knows the real provider rates,
        # relay only has a static table. Never overwrite it.
        if upstream_cost is not None:
            return upstream_cost
        prices = getattr(self.settings, "prices", {}) or {}
        entry = prices.get(record.model or "")
        if not entry:
            return 0.0
        pt = record.prompt_tokens or 0
        ct = record.completion_tokens or 0
        return round(pt / 1e6 * entry[0] + ct / 1e6 * entry[1], 6)
