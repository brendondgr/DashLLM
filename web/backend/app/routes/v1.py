"""OpenAI-compatible client plane: everything under /v1 is proxied.

Open by design — no API key, no allowlist, no gate. Route classification
(chat.completions / completions / embeddings / models / generic passthrough)
happens inside the proxy service; this router is just the front door.
"""

from fastapi import APIRouter, Request
from fastapi.responses import Response

router = APIRouter(prefix="/v1", tags=["openai-compatible"])

_METHODS = ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"]


@router.api_route("/{path:path}", methods=_METHODS)
async def proxy_v1(request: Request, path: str) -> Response:
    return await request.app.state.proxy.handle(request, path)
