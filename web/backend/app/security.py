"""Auth planes, request principals, and secret redaction helpers.

Three planes, deliberately separate:

- **Admin** — the env-defined account (``RELAY_ADMIN_USER`` +
  ``RELAY_ADMIN_PASSWORD_HASH``) or the legacy ``X-Admin-Token`` header. Reaches
  endpoints, tunnels, settings, proxy info, and all telemetry.
- **User** — a dashboard account with a session cookie. Reaches stats only, and
  row-level detail only for its own traffic.
- **Client** — an ``rk_`` key on ``/v1``, validated locally and never forwarded
  upstream. Attributes a proxied request to a user; an unknown key is not an
  error, it just leaves the request unattributed.

Two rules here are load-bearing and easy to undo by accident:

1. **Open-when-loopback, closed-when-public.** With no admin credential
   configured, the admin plane stays open on a loopback bind (the local-dev
   experience this project was built around) but ``create_app`` refuses to
   start on a public bind. Auth is never silently absent on a reachable port.
2. **CSRF is enforced inside the guards**, not as a separate dependency a new
   router could forget to include. Cookie-authenticated mutations must echo the
   session's CSRF token; header-token callers are exempt because they are not
   subject to ambient-credential attacks in the first place.
"""

import hmac
from dataclasses import dataclass

from fastapi import HTTPException, Request

from app.core.logging import get_logger
from app.services.users import ADMIN_SUBJECT, verify_password

log = get_logger("security")

SESSION_COOKIE = "relay_session"
CSRF_COOKIE = "relay_csrf"
CSRF_HEADER = "x-relay-csrf"

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", ""})

# Keys that must never be persisted with a request body or echoed into a log
# line, even though no relay feature puts them there. Clients do improvise.
SENSITIVE_KEYS = frozenset({
    "user_pass", "password", "passwd", "api_key", "apikey", "api-key",
    "authorization", "auth", "token", "access_token", "secret", "credentials",
})


@dataclass(frozen=True)
class Principal:
    kind: str  # "admin" | "user"
    user_id: str | None = None
    username: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.kind == "admin"

    @property
    def scope_user_id(self) -> str | None:
        """The user id a query must be constrained to, or None for unrestricted
        (admin) access. Handlers should use this rather than reading
        ``user_id`` directly — it is what keeps one user's rows out of
        another's view."""
        return None if self.is_admin else self.user_id


def mask_key(key: str | None) -> str | None:
    if not key:
        return None
    return "…" + key[-4:] if len(key) >= 4 else "…"


def redact(payload):
    """Strip credential-looking keys from a parsed JSON body, recursively.

    Applied to every proxied body before telemetry capture and before the body
    is re-serialized for the upstream, so a client that improvises
    ``{"user_pass": {...}}`` never has it written to ``request_bodies`` nor
    shipped to a model server or agent host.
    """
    if isinstance(payload, dict):
        return {k: redact(v) for k, v in payload.items()
                if k.lower() not in SENSITIVE_KEYS}
    if isinstance(payload, list):
        return [redact(v) for v in payload]
    return payload


def admin_configured(cfg) -> bool:
    return bool(cfg.admin_password_hash or cfg.admin_password or cfg.admin_token)


def is_loopback_bind(cfg) -> bool:
    return str(getattr(cfg, "host", "")).strip() in _LOOPBACK_HOSTS


def _dev_open(cfg) -> bool:
    """True when the admin plane is intentionally unauthenticated: a loopback
    bind with nothing configured. ``create_app`` guarantees this can never be
    true on a public bind."""
    return not admin_configured(cfg) and is_loopback_bind(cfg)


def verify_admin_password(cfg, username: str, password: str) -> bool:
    if username.strip().lower() != cfg.admin_user.strip().lower():
        return False
    if cfg.admin_password_hash:
        return verify_password(password, cfg.admin_password_hash)
    if cfg.admin_password:
        return hmac.compare_digest(password, cfg.admin_password)
    return False


def client_ip(request: Request) -> str:
    """Source IP for rate limiting. X-Forwarded-For is honored only when the
    operator has declared relay to be behind a proxy — otherwise any client
    could forge the header and get a fresh attempt budget per request."""
    cfg = request.app.state.cfg
    if cfg.trusted_proxy:
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def resolve_principal(request: Request) -> Principal | None:
    """Identify the caller from its session cookie or admin token. Returns None
    for anonymous callers; guards decide what that means."""
    cfg = request.app.state.cfg

    token = request.headers.get("x-admin-token", "")
    if token and cfg.admin_token and hmac.compare_digest(token, cfg.admin_token):
        return Principal(kind="admin", username=cfg.admin_user)

    row = request.app.state.users.resolve_session(
        request.cookies.get(SESSION_COOKIE))
    if row is None:
        return None
    if row["is_admin"]:
        return Principal(kind="admin", username=cfg.admin_user)
    user = request.app.state.users.by_id(row["subject"])
    if user is None or user["disabled"]:
        return None
    return Principal(kind="user", user_id=user["id"], username=user["username"])


def _check_csrf(request: Request) -> None:
    """Double-submit check for cookie-authenticated mutations. SameSite=Lax
    already blocks cross-site form posts; this covers the rest."""
    if request.method in _SAFE_METHODS:
        return
    if SESSION_COOKIE not in request.cookies:
        return  # not cookie-authenticated -> no ambient-credential risk
    row = request.app.state.users.resolve_session(
        request.cookies.get(SESSION_COOKIE))
    supplied = request.headers.get(CSRF_HEADER, "")
    if not row or not supplied or not hmac.compare_digest(supplied, row["csrf"]):
        log.warning("csrf rejected", extra={"data": {
            "path": request.url.path,
            "client": request.client.host if request.client else None}})
        raise HTTPException(status_code=403, detail="csrf check failed")


def _reject(request: Request, reason: str) -> None:
    log.warning("auth rejected", extra={"data": {
        "path": request.url.path, "reason": reason,
        "client": request.client.host if request.client else None}})
    raise HTTPException(status_code=401, detail="authentication required")


async def admin_guard(request: Request) -> Principal:
    """Endpoints, tunnels, settings, proxy info — everything that exposes
    upstream keys, SSH commands, or the proxy key."""
    cfg = request.app.state.cfg
    if _dev_open(cfg):
        return Principal(kind="admin", username="local")
    principal = resolve_principal(request)
    if principal is None:
        _reject(request, "no credentials")
    if not principal.is_admin:
        log.warning("admin access denied", extra={"data": {
            "path": request.url.path, "user": principal.username}})
        raise HTTPException(status_code=403, detail="admin access required")
    _check_csrf(request)
    return principal


async def user_guard(request: Request) -> Principal:
    """Any authenticated caller. Handlers must still scope their queries with
    ``Principal.scope_user_id`` — passing this guard is not authorization to
    read another user's rows."""
    cfg = request.app.state.cfg
    if _dev_open(cfg):
        return Principal(kind="admin", username="local")
    principal = resolve_principal(request)
    if principal is None:
        _reject(request, "no credentials")
    _check_csrf(request)
    return principal


def extract_bearer(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None
