"""OpenAI-compatible client plane: everything under /v1 is proxied.

Route classification (chat.completions / completions / embeddings / models /
generic passthrough) happens inside the proxy service; this router is a thin
front door that also enforces optional client-key auth.
"""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from app.core.logging import get_logger
from app.security import extract_bearer

log = get_logger("proxy")

router = APIRouter(prefix="/v1", tags=["openai-compatible"])

_METHODS = ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"]


@router.api_route("/{path:path}", methods=_METHODS)
async def proxy_v1(request: Request, path: str) -> Response:
    cfg = request.app.state.cfg
    if getattr(cfg, "require_client_key", False):
        key = extract_bearer(request)
        if key != request.app.state.settings.api_key:
            log.warning("client auth rejected", extra={"data": {
                "path": path,
                "client": request.client.host if request.client else None}})
            raise HTTPException(401, "invalid API key")
    return await request.app.state.proxy.handle(request, path)
