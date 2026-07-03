"""Live concurrency must count a request the moment it's accepted — while the
upstream is still thinking — not only after the model server replies.

Regression test for the bug where in-flight was registered after
`http.send()` returned, so the concurrency graph and the Requests tab only
moved once responses came back.
"""

import asyncio
import json

import httpx
import pytest
from starlette.requests import Request

from app.main import create_app
from app.schemas import EndpointCreate


def _make_request(body: dict) -> Request:
    payload = json.dumps(body).encode()
    scope = {
        "type": "http", "method": "POST",
        "path": "/v1/chat/completions",
        "headers": [(b"content-type", b"application/json")],
        "query_string": b"",
    }

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    return Request(scope, receive)


class _BlockingTransport(httpx.AsyncBaseTransport):
    """Signals when the upstream call starts, then waits to be released."""

    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def handle_async_request(self, request):
        self.entered.set()
        await self.release.wait()
        return httpx.Response(200, request=request, json={
            "id": "x", "object": "chat.completion", "model": "m",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "hi"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                      "total_tokens": 2}})


async def test_in_flight_counts_before_upstream_responds(cfg):
    app = create_app(cfg)
    async with app.router.lifespan_context(app):
        transport = _BlockingTransport()
        app.state.http = httpx.AsyncClient(transport=transport, timeout=5.0)
        app.state.proxy.http = app.state.http
        app.state.router.create(EndpointCreate(name="up", base_url="http://up/v1"))
        live = app.state.live
        assert len(live.in_flight) == 0

        req = _make_request({"model": "m",
                             "messages": [{"role": "user", "content": "hi"}]})
        task = asyncio.create_task(app.state.proxy.handle(req, "chat/completions"))

        # Upstream call is now in progress but has NOT returned yet.
        await asyncio.wait_for(transport.entered.wait(), 2)
        assert len(live.in_flight) == 1, "request should be in-flight while waiting on the model server"
        snap = live.snapshot(app.state.cfg.max_concurrency)
        assert snap["in_flight"] == 1
        # it shows up in the live Requests view too, as 'streaming'/in-progress
        recent = await app.state.stats.recent()
        assert any(r["id"].startswith("req_") and r["state"] == "streaming"
                   for r in recent["rows"])

        # Let the upstream reply; the request drains out of in-flight.
        transport.release.set()
        resp = await asyncio.wait_for(task, 2)
        assert resp.status_code == 200
        assert len(live.in_flight) == 0

        await app.state.http.aclose()


async def test_concurrent_requests_all_counted(cfg):
    app = create_app(cfg)
    async with app.router.lifespan_context(app):
        transport = _BlockingTransport()
        app.state.http = httpx.AsyncClient(transport=transport, timeout=5.0)
        app.state.proxy.http = app.state.http
        app.state.router.create(EndpointCreate(name="up", base_url="http://up/v1"))
        live = app.state.live

        reqs = [_make_request({"model": "m",
                               "messages": [{"role": "user", "content": str(i)}]})
                for i in range(3)]
        tasks = [asyncio.create_task(app.state.proxy.handle(r, "chat/completions"))
                 for r in reqs]
        await asyncio.wait_for(transport.entered.wait(), 2)
        # give the other two a tick to reach the (blocked) upstream too
        for _ in range(20):
            if len(live.in_flight) == 3:
                break
            await asyncio.sleep(0.02)
        assert len(live.in_flight) == 3, f"expected 3 concurrent, saw {len(live.in_flight)}"

        transport.release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 3)
        assert len(live.in_flight) == 0
        await app.state.http.aclose()
