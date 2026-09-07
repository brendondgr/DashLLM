"""A stand-in OpenAI-compatible model server, for load testing relay itself.

Answers ``GET /v1/models`` and ``POST /v1/chat/completions`` (buffered and
SSE) after a configurable delay. The delay is the point: it lets a stress run
hold N requests open at once without needing a real GPU, so what is being
measured is relay's own behavior — its admission gate, event loop, telemetry
writer — rather than a model's decode speed.

    uv run --project web/backend python utils/stub_upstream.py --port 9099

Environment:
    STUB_DELAY_S     seconds to hold each request (default 0.5)
    STUB_STREAM_MS   ms between SSE chunks (default 20)
    STUB_STREAM_N    number of SSE content chunks (default 10)
"""

import argparse
import asyncio
import json
import os
import time

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

DELAY = float(os.environ.get("STUB_DELAY_S", "0.5"))
STREAM_MS = float(os.environ.get("STUB_STREAM_MS", "20")) / 1000
STREAM_N = int(os.environ.get("STUB_STREAM_N", "10"))

app = FastAPI()
STATE = {"chat": 0, "models": 0, "peak_concurrent": 0, "concurrent": 0}


@app.get("/v1/models")
async def models():
    STATE["models"] += 1
    return {"data": [{"id": "stub-model"}]}


@app.get("/stub/stats")
async def stats():
    return STATE


@app.post("/v1/chat/completions")
async def chat(request: Request):
    STATE["chat"] += 1
    STATE["concurrent"] += 1
    STATE["peak_concurrent"] = max(
        STATE["peak_concurrent"], STATE["concurrent"])
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        body = {}

    if body.get("stream"):
        async def gen():
            try:
                await asyncio.sleep(DELAY)
                for i in range(STREAM_N):
                    yield ("data: " + json.dumps({
                        "id": "c1", "object": "chat.completion.chunk",
                        "model": "stub-model",
                        "choices": [{"index": 0,
                                     "delta": {"content": f"t{i} "}}],
                    }) + "\n\n").encode()
                    await asyncio.sleep(STREAM_MS)
                yield ("data: " + json.dumps({
                    "id": "c1", "choices": [],
                    "usage": {"prompt_tokens": 8,
                              "completion_tokens": STREAM_N,
                              "total_tokens": 8 + STREAM_N},
                }) + "\n\n").encode()
                yield b"data: [DONE]\n\n"
            finally:
                STATE["concurrent"] -= 1

        return StreamingResponse(gen(), media_type="text/event-stream")

    try:
        await asyncio.sleep(DELAY)
        return {
            "id": "cmpl-1", "object": "chat.completion",
            "created": int(time.time()), "model": "stub-model",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 8, "completion_tokens": 4,
                      "total_tokens": 12},
        }
    finally:
        STATE["concurrent"] -= 1


if __name__ == "__main__":
    import uvicorn

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9099)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning",
                access_log=False)
