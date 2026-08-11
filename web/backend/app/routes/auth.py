"""Login, signup, and account self-service.

The only unauthenticated routes in the app besides ``/health`` and the static
dashboard. Three things here are security decisions rather than plumbing:

- **Signup is closed unless ``RELAY_SIGNUP_CODE`` is set.** An account carries a
  proxy key to real hardware, so the default has to be "nobody registers".
- **Failures are counted per IP, successes clear the counter.** Ten failures in
  fifteen minutes and the source is locked out — enough to make online password
  guessing useless without locking out a fat-fingered user for long.
- **Login answers identically whether the username exists or not**, and
  ``UserStore.authenticate`` burns an equivalent KDF on a miss so the timing
  does not leak it either.
"""

import hmac

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.core.logging import get_logger
from app.schemas import (
    ApiKeyOut,
    AuthStatus,
    LoginRequest,
    MeOut,
    MePatch,
    SignupRequest,
    UserOut,
    UserPatch,
)
from app.security import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    Principal,
    admin_guard,
    client_ip,
    resolve_principal,
    verify_admin_password,
)
from app.services.users import ADMIN_SUBJECT, UsernameTaken

log = get_logger("auth")

router = APIRouter(prefix="/auth", tags=["auth"])


def _set_session_cookies(response: Response, cfg, token: str, csrf: str) -> None:
    max_age = int(cfg.session_ttl_hours * 3600)
    # HttpOnly keeps the session token out of reach of any script on the page;
    # the CSRF token is deliberately readable, since api.ts must echo it back
    # in a header for the double-submit check.
    response.set_cookie(
        SESSION_COOKIE, token, max_age=max_age, httponly=True,
        secure=cfg.cookie_secure, samesite="lax", path="/")
    response.set_cookie(
        CSRF_COOKIE, csrf, max_age=max_age, httponly=False,
        secure=cfg.cookie_secure, samesite="lax", path="/")


def _clear_session_cookies(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")


def _me_payload(request: Request, principal: Principal,
                csrf: str | None = None) -> MeOut:
    if principal.is_admin:
        return MeOut(kind="admin", username=principal.username, csrf=csrf)
    row = request.app.state.users.by_id(principal.user_id) or {}
    return MeOut(
        kind="user", username=principal.username, user_id=principal.user_id,
        private=bool(row.get("private")),
        api_key_prefix=row.get("api_key_prefix"), csrf=csrf)


def _require(request: Request) -> Principal:
    principal = resolve_principal(request)
    if principal is None:
        raise HTTPException(401, "authentication required")
    return principal


@router.get("/status", response_model=AuthStatus)
async def status(request: Request) -> AuthStatus:
    """Unauthenticated: tells the dashboard whether to show the login screen and
    whether the signup tab is worth rendering."""
    cfg = request.app.state.cfg
    principal = resolve_principal(request)
    if principal is None:
        return AuthStatus(authenticated=False,
                          signup_enabled=bool(cfg.signup_code))
    return AuthStatus(
        authenticated=True, signup_enabled=bool(cfg.signup_code),
        me=_me_payload(request, principal,
                       csrf=request.cookies.get(CSRF_COOKIE)))


@router.post("/login", response_model=MeOut)
async def login(request: Request, response: Response, body: LoginRequest) -> MeOut:
    cfg = request.app.state.cfg
    users = request.app.state.users
    ip = client_ip(request)
    if users.is_rate_limited(ip):
        log.warning("login rate limited", extra={"data": {"client": ip}})
        raise HTTPException(429, "too many attempts; try again later")

    if verify_admin_password(cfg, body.username, body.password):
        token, csrf = users.create_session(ADMIN_SUBJECT, is_admin=True)
        users.clear_failures(ip)
        _set_session_cookies(response, cfg, token, csrf)
        log.info("admin login", extra={"data": {"client": ip}})
        return _me_payload(request, Principal(kind="admin",
                                              username=cfg.admin_user), csrf)

    # The admin name never falls through to the user table. Signup refuses to
    # create such a row, but a pre-existing or hand-edited one must not be able
    # to shadow the administrator's login either.
    if body.username.strip().lower() == cfg.admin_user.strip().lower():
        users.record_failure(ip)
        raise HTTPException(401, "invalid username or password")

    row = users.authenticate(body.username, body.password)
    if row is None:
        users.record_failure(ip)
        log.warning("login failed", extra={"data": {"client": ip}})
        raise HTTPException(401, "invalid username or password")

    token, csrf = users.create_session(row["id"], is_admin=False)
    users.clear_failures(ip)
    _set_session_cookies(response, cfg, token, csrf)
    log.info("user login", extra={"data": {"username": row["username"]}})
    return _me_payload(
        request,
        Principal(kind="user", user_id=row["id"], username=row["username"]),
        csrf)


@router.post("/signup", response_model=ApiKeyOut, status_code=201)
async def signup(request: Request, response: Response,
                 body: SignupRequest) -> ApiKeyOut:
    cfg = request.app.state.cfg
    users = request.app.state.users
    ip = client_ip(request)
    if users.is_rate_limited(ip):
        raise HTTPException(429, "too many attempts; try again later")
    if not cfg.signup_code:
        raise HTTPException(403, "signup is disabled")
    # An invalid code counts against the limiter, so the code itself cannot be
    # brute-forced any faster than a password.
    if not hmac.compare_digest(body.code, cfg.signup_code):
        users.record_failure(ip)
        log.warning("signup rejected", extra={"data": {"client": ip}})
        raise HTTPException(403, "invalid signup code")
    if body.username.strip().lower() == cfg.admin_user.strip().lower():
        raise HTTPException(409, "username unavailable")

    try:
        row, key = users.create(body.username, body.password)
    except UsernameTaken:
        raise HTTPException(409, "username already taken") from None

    token, csrf = users.create_session(row["id"], is_admin=False)
    users.clear_failures(ip)
    _set_session_cookies(response, cfg, token, csrf)
    return ApiKeyOut(api_key=key, api_key_prefix=row["api_key_prefix"])


@router.post("/logout", status_code=204)
async def logout(request: Request) -> Response:
    request.app.state.users.revoke_session(request.cookies.get(SESSION_COOKIE))
    response = Response(status_code=204)
    _clear_session_cookies(response)
    return response


@router.get("/me", response_model=MeOut)
async def me(request: Request) -> MeOut:
    return _me_payload(request, _require(request),
                       csrf=request.cookies.get(CSRF_COOKIE))


@router.patch("/me", response_model=MeOut)
async def patch_me(request: Request, body: MePatch) -> MeOut:
    principal = _require(request)
    if principal.is_admin:
        raise HTTPException(400, "the admin account has no stats of its own")
    if body.private is not None:
        request.app.state.users.set_private(principal.user_id, body.private)
    return _me_payload(request, principal)


@router.post("/me/key", response_model=ApiKeyOut)
async def rotate_key(request: Request) -> ApiKeyOut:
    principal = _require(request)
    if principal.is_admin:
        # The admin's client key is the relay-wide one under /admin/proxy.
        raise HTTPException(400, "the admin account has no per-user key")
    key = request.app.state.users.rotate_api_key(principal.user_id)
    row = request.app.state.users.by_id(principal.user_id)
    return ApiKeyOut(api_key=key, api_key_prefix=row["api_key_prefix"])


# ---- admin-only user administration ----------------------------------
admin_router = APIRouter(
    prefix="/admin", tags=["auth"], dependencies=[Depends(admin_guard)]
)


@admin_router.get("/users", response_model=list[UserOut])
async def list_users(request: Request) -> list[UserOut]:
    return [UserOut(**{**r, "private": bool(r["private"]),
                       "disabled": bool(r["disabled"])})
            for r in request.app.state.users.list_users()]


@admin_router.patch("/users/{user_id}", response_model=UserOut)
async def patch_user(request: Request, user_id: str, body: UserPatch) -> UserOut:
    users = request.app.state.users
    if users.by_id(user_id) is None:
        raise HTTPException(404, "user not found")
    if body.private is not None:
        users.set_private(user_id, body.private)
    if body.disabled is not None:
        users.set_disabled(user_id, body.disabled)
    row = users.by_id(user_id)
    return UserOut(id=row["id"], username=row["username"],
                   api_key_prefix=row["api_key_prefix"],
                   private=bool(row["private"]), disabled=bool(row["disabled"]),
                   created_ts=row["created_ts"])
