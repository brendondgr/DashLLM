"""Auth dependencies + secret redaction helpers.

- Admin plane: ``X-Admin-Token`` enforced only when RELAY_ADMIN_TOKEN is set.
- Client plane: Bearer key validated locally (never forwarded upstream);
  only enforced when client auth is enabled by configuration.
"""

import hmac

from fastapi import HTTPException, Request

from app.core.logging import get_logger

log = get_logger("security")


def mask_key(key: str | None) -> str | None:
    if not key:
        return None
    return "…" + key[-4:] if len(key) >= 4 else "…"


async def admin_guard(request: Request) -> None:
    token = request.app.state.cfg.admin_token
    if not token:
        return
    supplied = request.headers.get("x-admin-token", "")
    if not hmac.compare_digest(supplied, token):
        log.warning("admin auth rejected", extra={"data": {
            "path": request.url.path,
            "client": request.client.host if request.client else None}})
        raise HTTPException(status_code=401, detail="invalid admin token")


def extract_bearer(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None
