"""A fake OpenAI-compatible upstream + a routing httpx transport.

Hosts:
- ``good``  -> in-process FastAPI app (models, chat/completions incl. SSE)
- ``flaky`` -> always HTTP 500
- anything else -> httpx.ConnectError (a dead box)
"""

import asyncio
import json

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse


def make_upstream() -> tuple[FastAPI, dict]:
    up = FastAPI()
    calls = {"chat": 0, "models": 0, "saw_include_usage": False}

    @up.get("/v1/models")
    async def models():
        calls["models"] += 1
        return {"data": [{"id": "fake-model-7b"}]}

    @up.post("/v1/chat/completions")
    async def chat(request: Request):
        calls["chat"] += 1
        body = await request.json()
        if body.get("stream"):
            include_usage = bool(
                (body.get("stream_options") or {}).get("include_usage"))
            calls["saw_include_usage"] = include_usage

            async def gen():
                for i in range(5):
                    chunk = {
                        "id": "c1", "object": "chat.completion.chunk",
                        "model": "fake-model-7b",
                        "choices": [{"index": 0,
                                     "delta": {"content": f"tok{i} "}}],
                    }
                    yield f"data: {json.dumps(chunk)}\n\n"
                    await asyncio.sleep(0.005)
                if include_usage:
                    tail = {"id": "c1", "choices": [],
                            "usage": {"prompt_tokens": 7,
                                      "completion_tokens": 5,
                                      "total_tokens": 12}}
                    yield f"data: {json.dumps(tail)}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(gen(), media_type="text/event-stream")
        return {
            "id": "cmpl-1", "object": "chat.completion",
            "model": "fake-model-7b",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "hello"}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3,
                      "total_tokens": 10},
        }

    return up, calls


class _AlwaysFailTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request):
        return httpx.Response(500, content=b"upstream exploded",
                              request=request)


class RoutingTransport(httpx.AsyncBaseTransport):
    def __init__(self, upstream_app: FastAPI):
        self._routes: dict[str, httpx.AsyncBaseTransport] = {
            "good": httpx.ASGITransport(app=upstream_app),
            "flaky": _AlwaysFailTransport(),
        }

    async def handle_async_request(self, request):
        transport = self._routes.get(request.url.host)
        if transport is None:
            raise httpx.ConnectError("connection refused", request=request)
        return await transport.handle_async_request(request)
