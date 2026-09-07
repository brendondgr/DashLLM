"""Behavior under more load than the upstream can take.

The failure this file guards against is not "slow" — it is a relay that
converts its own saturation into a broken-looking endpoint and then 503s
everything. Each test pins one link in that chain:

- the admission gate bounds and sheds instead of queueing without limit
- a slot covers the whole reply, including a synthesized stream
- relay's own pool exhaustion never marks an endpoint unhealthy
- no exit path leaks a slot
"""

import asyncio
import json

import httpx
import pytest
from starlette.requests import Request

from app.main import create_app
from app.schemas import EndpointCreate
from app.services.adapters import RetryableUpstreamError, UpstreamSaturated
from app.services.adapters.base import classify_httpx_error
from app.services.proxy import AdmissionGate, Overloaded


def _request(body: dict, headers: list | None = None) -> Request:
    payload = json.dumps(body).encode()
    scope = {
        "type": "http", "method": "POST", "path": "/v1/chat/completions",
        "headers": headers or [(b"content-type", b"application/json")],
        "query_string": b"",
    }

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    return Request(scope, receive)


# ---- the gate on its own ------------------------------------------------
async def test_gate_sheds_once_the_queue_is_full():
    gate = AdmissionGate(max_concurrency=1, queue_limit=1, queue_timeout=5)
    held = await gate.acquire()
    waiter = asyncio.create_task(gate.acquire())
    await asyncio.sleep(0)  # let it park on the semaphore

    with pytest.raises(Overloaded) as excinfo:
        await gate.acquire()
    assert "queue full" in str(excinfo.value)
    assert excinfo.value.retry_after > 0

    held()
    (await waiter)()
    assert gate.active == 0 and gate.waiting == 0


async def test_gate_sheds_a_waiter_that_waited_too_long():
    gate = AdmissionGate(max_concurrency=1, queue_limit=8, queue_timeout=0.05)
    held = await gate.acquire()
    with pytest.raises(Overloaded) as excinfo:
        await gate.acquire()
    assert "waited" in str(excinfo.value)
    held()
    # The timed-out waiter must not have left the count skewed.
    assert gate.waiting == 0
    (await gate.acquire())()
    assert gate.active == 0


async def test_gate_refuses_immediately_when_queueing_is_off():
    gate = AdmissionGate(max_concurrency=1, queue_limit=64)
    held = await gate.acquire()
    with pytest.raises(Overloaded, match="concurrency limit"):
        await gate.acquire(queue=False)
    held()


async def test_release_is_idempotent():
    gate = AdmissionGate(max_concurrency=2, queue_limit=1)
    release = await gate.acquire()
    release()
    release()
    assert gate.active == 0
    # Both slots must still be there.
    a, b = await gate.acquire(), await gate.acquire()
    with pytest.raises(Overloaded):
        await gate.acquire(queue=False)
    a(), b()


# ---- saturation is ours, not the endpoint's -----------------------------
def test_pool_timeout_is_classified_as_local_saturation():
    req = httpx.Request("POST", "http://up/v1/chat/completions")
    assert isinstance(
        classify_httpx_error(httpx.PoolTimeout("pool", request=req)),
        UpstreamSaturated)
    connect = classify_httpx_error(httpx.ConnectError("refused", request=req))
    assert isinstance(connect, RetryableUpstreamError)
    assert not isinstance(connect, UpstreamSaturated)


class _PoolExhaustedTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request):
        raise httpx.PoolTimeout("no free connection", request=request)


async def test_pool_exhaustion_does_not_mark_the_endpoint_failed(cfg, oc_cfg):
    """The regression that took the whole relay down: three concurrent bursts
    exhausted the shared pool, each PoolTimeout counted as an endpoint
    failure, and after three the only endpoint dropped out of rotation."""
    app = create_app(cfg, oc_cfg)
    async with app.router.lifespan_context(app):
        app.state.proxy.http = httpx.AsyncClient(
            transport=_PoolExhaustedTransport(), timeout=5.0)
        ep = app.state.router.create(
            EndpointCreate(name="up", base_url="http://up/v1"))
        app.state.router.report_success(ep["id"], source="probe")

        for _ in range(5):
            resp = await app.state.proxy.handle(
                _request({"model": "auto", "messages": [{"role": "user",
                                                         "content": "hi"}]}),
                "chat/completions")
            assert resp.status_code == 503
            assert resp.headers["Retry-After"] == "1"

        state = app.state.router.state[ep["id"]]
        assert state.health == "healthy"
        assert state.consecutive_fails == 0
        # And the gate is clean, so the relay recovers the moment the pool does.
        assert app.state.proxy.gate.active == 0


# ---- slot ownership across a stream -------------------------------------
async def test_streaming_holds_its_slot_until_the_stream_ends(cfg, oc_cfg):
    """A synthesized-SSE protocol does all its work inside the response body,
    so releasing at header time left it entirely ungated."""
    from fake_upstream import RoutingTransport, make_upstream

    upstream, _ = make_upstream()
    app = create_app(cfg, oc_cfg)
    async with app.router.lifespan_context(app):
        fake = httpx.AsyncClient(
            transport=RoutingTransport(upstream), timeout=5.0)
        app.state.http = fake
        app.state.proxy.http = fake
        app.state.router.create(
            EndpointCreate(name="up", base_url="http://good/v1"))

        resp = await app.state.proxy.handle(
            _request({"model": "auto", "stream": True,
                      "messages": [{"role": "user", "content": "hi"}]}),
            "chat/completions")
        gate = app.state.proxy.gate
        assert gate.active == 1, "slot released before the stream was read"

        async for _ in resp.body_iterator:
            pass
        assert gate.active == 0
        assert app.state.live.in_flight == {}


async def test_slot_is_returned_when_there_is_no_upstream(cfg, oc_cfg):
    app = create_app(cfg, oc_cfg)
    async with app.router.lifespan_context(app):
        resp = await app.state.proxy.handle(
            _request({"model": "auto",
                      "messages": [{"role": "user", "content": "hi"}]}),
            "chat/completions")
        assert resp.status_code == 503
        assert app.state.proxy.gate.active == 0


# ---- shedding end to end ------------------------------------------------
class _BlockingTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.release = asyncio.Event()

    async def handle_async_request(self, request):
        await self.release.wait()
        return httpx.Response(200, request=request, json={
            "id": "x", "object": "chat.completion", "model": "m",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "hi"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                      "total_tokens": 2}})


async def test_overflow_is_shed_with_429_and_recorded(cfg, oc_cfg):
    app = create_app(cfg, oc_cfg)
    async with app.router.lifespan_context(app):
        transport = _BlockingTransport()
        app.state.proxy.http = httpx.AsyncClient(
            transport=transport, timeout=30.0)
        app.state.proxy.gate = AdmissionGate(
            max_concurrency=1, queue_limit=1, queue_timeout=5)
        app.state.router.create(
            EndpointCreate(name="up", base_url="http://up/v1"))

        body = {"model": "auto", "messages": [{"role": "user", "content": "x"}]}
        inflight = [
            asyncio.create_task(
                app.state.proxy.handle(_request(body), "chat/completions"))
            for _ in range(2)]
        await asyncio.sleep(0.05)  # one running, one queued

        shed = await app.state.proxy.handle(_request(body), "chat/completions")
        assert shed.status_code == 429
        assert int(shed.headers["Retry-After"]) >= 1

        transport.release.set()
        for task in inflight:
            assert (await task).status_code == 200

        await app.state.telemetry.flush()
        rows = await app.state.db.aquery(
            "SELECT status, error FROM requests WHERE status = 429")
        assert len(rows) == 1, "a shed request must still be recorded"
        assert rows[0]["error"].startswith("shed:")
        assert app.state.proxy.gate.active == 0
        assert app.state.live.in_flight == {}


# ---- inbound body cap ---------------------------------------------------
async def test_oversized_body_is_refused_before_it_is_buffered(cfg, oc_cfg):
    app = create_app(cfg, oc_cfg)
    async with app.router.lifespan_context(app):
        app.state.proxy.max_body_bytes = 512
        big = {"model": "auto",
               "messages": [{"role": "user", "content": "x" * 4096}]}
        resp = await app.state.proxy.handle(
            _request(big), "chat/completions")
        assert resp.status_code == 413
        # Declared content-length is refused on the same terms.
        resp = await app.state.proxy.handle(
            _request(big, headers=[(b"content-type", b"application/json"),
                                   (b"content-length", b"999999")]),
            "chat/completions")
        assert resp.status_code == 413


# ---- congestion must not read as death ----------------------------------
def test_request_failures_only_degrade_while_probes_pass(db):
    """An overloaded-but-alive endpoint returns a run of timeouts. Removing it
    from the pool on that basis is how a busy relay became a dead one."""
    from app.services.router import Router

    router = Router(db, unhealthy_after=3, recover_after=2)
    ep = router.create(EndpointCreate(name="up", base_url="http://up/v1"))
    router.report_success(ep["id"], source="probe")

    for _ in range(10):
        router.report_failure(ep["id"], "ReadTimeout", source="request")

    state = router.state[ep["id"]]
    assert state.health == "degraded"
    assert router.resolve() is not None, "endpoint must stay routable"


def test_probe_failures_still_fail_the_endpoint(db):
    """The other half: a server that is genuinely gone fails its probes, and
    that must still take it out of rotation."""
    from app.services.router import Router

    router = Router(db, unhealthy_after=3, recover_after=2)
    ep = router.create(EndpointCreate(name="up", base_url="http://up/v1"))
    router.report_success(ep["id"], source="probe")

    for _ in range(3):
        router.report_failure(ep["id"], "ConnectError", source="probe")

    assert router.state[ep["id"]].health == "failed"
    assert router.resolve() is None


def test_a_failed_probe_lets_request_failures_finish_the_job(db):
    """Requests and probes agree the endpoint is gone: no reason to wait."""
    from app.services.router import Router

    router = Router(db, unhealthy_after=3, recover_after=2)
    ep = router.create(EndpointCreate(name="up", base_url="http://up/v1"))
    router.report_success(ep["id"], source="probe")
    router.report_failure(ep["id"], "ConnectError", source="probe")
    for _ in range(3):
        router.report_failure(ep["id"], "ConnectError", source="request")
    assert router.state[ep["id"]].health == "failed"
