"""OpenAI-compatible client plane: everything under /v1 is proxied.

Route classification (chat.completions / completions / embeddings / models /
generic passthrough) happens inside the proxy service; this router is a thin
front door that also enforces optional client-key auth.
"""

import hmac

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
        # Either the relay-wide key or any live per-user rk_ key. Compared with
        # compare_digest so a wrong shared key can't be recovered by timing.
        shared_ok = bool(key) and hmac.compare_digest(
            key, request.app.state.settings.api_key)
        if not shared_ok and request.app.state.users.by_api_key(key) is None:
            log.warning(
                "client auth rejected; /v1 requires a Bearer key because"
                " RELAY_REQUIRE_CLIENT_KEY=1. Set it to 0 to open /v1, or"
                " send the key printed by ./launch.sh.",
                extra={"data": {
                    "path": path, "key_supplied": bool(key),
                    "client": request.client.host if request.client else None}})
            # Name the setting in the response too. Which auth mode a proxy is
            # in is not a secret, and "invalid API key" with three different
            # credentials in play (admin password, signup code, client key)
            # sends people looking at the wrong one.
            raise HTTPException(401, (
                "invalid API key: /v1 requires 'Authorization: Bearer <key>'"
                " because RELAY_REQUIRE_CLIENT_KEY=1"
                if key else
                "missing API key: /v1 requires 'Authorization: Bearer <key>'"
                " because RELAY_REQUIRE_CLIENT_KEY=1"))
    return await request.app.state.proxy.handle(request, path)
