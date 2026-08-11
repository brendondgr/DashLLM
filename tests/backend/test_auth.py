"""Authentication, authorization, and the hardening that goes with them.

The cases that matter most here are the negative ones: a user session must not
reach the admin plane, a user must not see another user's rows, credentials
must not survive a proxied request, and a public bind must not start unguarded.
"""

import time

import pytest
from conftest import ADMIN_PASSWORD, SIGNUP_CODE, login_admin, signup_user
from fastapi.testclient import TestClient

from app.config import Config
from app.main import create_app
from app.security import redact
from app.services.users import (
    UserStore, generate_api_key, hash_api_key, hash_password, verify_password,
)

ADMIN_ONLY = [
    ("GET", "/admin/endpoints"),
    ("GET", "/admin/tunnels"),
    ("GET", "/admin/settings"),
    ("GET", "/admin/proxy"),
    ("GET", "/admin/users"),
    ("GET", "/admin/logs/frontend"),
]


# ---- primitives -------------------------------------------------------
def test_password_hash_roundtrip():
    encoded = hash_password("s3cret-passphrase")
    assert encoded.startswith("scrypt$")
    assert "s3cret-passphrase" not in encoded
    assert verify_password("s3cret-passphrase", encoded)
    assert not verify_password("s3cret-passphras", encoded)


def test_password_hash_is_salted():
    assert hash_password("same") != hash_password("same")


@pytest.mark.parametrize("junk", ["", "notahash", "scrypt$x$y$z", "a$b$c$d$e$f"])
def test_verify_password_rejects_malformed_hash(junk):
    """A half-written or corrupt hash must fail closed, never raise into a
    handler where the exception could be mistaken for something else."""
    assert not verify_password("anything", junk)


def test_api_key_shape_and_hashing():
    key = generate_api_key()
    assert key.startswith("rk_") and len(key) > 30
    assert hash_api_key(key) != key
    assert hash_api_key(key) == hash_api_key(key)


def test_redact_strips_credentials_recursively():
    body = {"model": "m", "user_pass": {"u": "a", "p": "b"},
            "messages": [{"role": "user", "content": "hi", "api_key": "sk-x"}]}
    out = redact(body)
    assert "user_pass" not in out
    assert "api_key" not in out["messages"][0]
    assert out["model"] == "m" and out["messages"][0]["content"] == "hi"


# ---- user store -------------------------------------------------------
def test_user_store_key_lookup_and_disable(db):
    store = UserStore(db)
    row, key = store.create("alice", "hunter2hunter2")
    assert store.by_api_key(key)["id"] == row["id"]
    assert store.by_api_key("rk_nope") is None
    assert store.by_api_key(None) is None

    store.set_disabled(row["id"], True)
    assert store.by_api_key(key) is None, "disabled account must lose /v1 access"


def test_rotating_a_key_invalidates_the_old_one(db):
    store = UserStore(db)
    row, old = store.create("bob", "hunter2hunter2")
    new = store.rotate_api_key(row["id"])
    assert store.by_api_key(new)["id"] == row["id"]
    assert store.by_api_key(old) is None


def test_expired_session_does_not_resolve(db):
    store = UserStore(db, session_ttl_hours=-1)  # already expired on creation
    token, _ = store.create_session("someone", is_admin=False)
    assert store.resolve_session(token) is None


def test_disabling_a_user_kills_live_sessions(db):
    store = UserStore(db)
    row, _ = store.create("carol", "hunter2hunter2")
    token, _ = store.create_session(row["id"], is_admin=False)
    assert store.resolve_session(token) is not None
    store.set_disabled(row["id"], True)
    assert store.resolve_session(token) is None


def test_rate_limiter_trips_and_clears(db):
    store = UserStore(db)
    for _ in range(9):
        store.record_failure("1.2.3.4")
    assert not store.is_rate_limited("1.2.3.4")
    store.record_failure("1.2.3.4")
    assert store.is_rate_limited("1.2.3.4")
    assert not store.is_rate_limited("5.6.7.8"), "limiter must be per-IP"
    store.clear_failures("1.2.3.4")
    assert not store.is_rate_limited("1.2.3.4")


def test_session_sweep_removes_expired(db):
    store = UserStore(db, session_ttl_hours=-1)
    store.create_session("x", is_admin=False)
    assert store.sweep_sessions() == 1


# ---- login / signup ---------------------------------------------------
def test_anonymous_is_locked_out_of_every_plane(auth_client):
    for method, path in ADMIN_ONLY:
        assert auth_client.request(method, path).status_code == 401, path
    assert auth_client.get("/admin/stats/summary").status_code == 401


def test_admin_login_reaches_everything(auth_client):
    login_admin(auth_client)
    for method, path in ADMIN_ONLY:
        assert auth_client.request(method, path).status_code == 200, path
    assert auth_client.get("/admin/stats/summary").status_code == 200


def test_wrong_admin_password_rejected(auth_client):
    r = auth_client.post("/auth/login",
                         json={"username": "admin", "password": "wrong"})
    assert r.status_code == 401
    assert auth_client.get("/admin/settings").status_code == 401


def test_user_session_cannot_reach_the_admin_plane(auth_client):
    signup_user(auth_client, "alice")
    for method, path in ADMIN_ONLY:
        r = auth_client.request(method, path)
        assert r.status_code == 403, f"{path} -> {r.status_code}"


def test_user_session_can_read_stats(auth_client):
    signup_user(auth_client, "alice")
    assert auth_client.get("/admin/stats/summary").status_code == 200
    assert auth_client.get("/admin/stats/recent").status_code == 200
    assert auth_client.get("/admin/stats/live").status_code == 200


def test_signup_requires_the_invite_code(auth_client):
    r = auth_client.post("/auth/signup", json={
        "username": "mallory", "password": "hunter2hunter2", "code": "guess"})
    assert r.status_code == 403
    r = auth_client.post("/auth/signup", json={
        "username": "mallory", "password": "hunter2hunter2", "code": ""})
    assert r.status_code == 403


def test_signup_disabled_when_no_code_configured(cfg, tmp_path):
    """``cfg`` has no signup_code, which is the default — registration must be
    off rather than open."""
    with TestClient(create_app(cfg)) as c:
        r = c.post("/auth/signup", json={
            "username": "nobody", "password": "hunter2hunter2", "code": ""})
        assert r.status_code == 403
        assert c.get("/auth/status").json()["signup_enabled"] is False


def test_signup_rejects_duplicate_and_admin_username(auth_client):
    signup_user(auth_client, "alice")
    r = auth_client.post("/auth/signup", json={
        "username": "alice", "password": "hunter2hunter2", "code": SIGNUP_CODE})
    assert r.status_code == 409
    r = auth_client.post("/auth/signup", json={
        "username": "admin", "password": "hunter2hunter2", "code": SIGNUP_CODE})
    assert r.status_code == 409


def test_signup_rejects_short_passwords(auth_client):
    r = auth_client.post("/auth/signup", json={
        "username": "shorty", "password": "short", "code": SIGNUP_CODE})
    assert r.status_code == 422


def test_signup_returns_the_key_exactly_once(auth_client):
    key = signup_user(auth_client, "alice")
    me = auth_client.get("/auth/me").json()
    assert me["api_key_prefix"] == key[:10]
    assert key not in auth_client.get("/auth/me").text


def test_logout_invalidates_the_session(auth_client):
    signup_user(auth_client, "alice")
    assert auth_client.get("/auth/me").status_code == 200
    assert auth_client.post("/auth/logout").status_code == 204
    assert auth_client.get("/auth/me").status_code == 401
    assert auth_client.get("/admin/stats/summary").status_code == 401


def test_login_rate_limit_returns_429(auth_client):
    for _ in range(10):
        auth_client.post("/auth/login",
                         json={"username": "admin", "password": "nope"})
    r = auth_client.post("/auth/login",
                         json={"username": "admin", "password": ADMIN_PASSWORD})
    assert r.status_code == 429, "a correct password must not bypass the limiter"


def test_mutation_without_csrf_header_is_rejected(auth_client):
    login_admin(auth_client)
    del auth_client.headers["X-Relay-CSRF"]
    r = auth_client.put("/admin/settings", json={"log_bodies": True})
    assert r.status_code == 403
    assert auth_client.get("/admin/settings").status_code == 200, \
        "reads stay available; only mutations need the token"


def test_wrong_csrf_token_is_rejected(auth_client):
    login_admin(auth_client)
    auth_client.headers["X-Relay-CSRF"] = "not-the-token"
    assert auth_client.put(
        "/admin/settings", json={"log_bodies": True}).status_code == 403


def test_admin_token_header_still_works_for_scripts(tmp_path):
    cfg = Config(db_path=tmp_path / "t.db", log_dir=tmp_path / "logs",
                 frontend_dist=tmp_path / "nd", probe_interval=9999,
                 admin_token="s3kr1t")
    with TestClient(create_app(cfg)) as c:
        assert c.get("/admin/settings").status_code == 401
        assert c.get("/admin/settings",
                     headers={"X-Admin-Token": "s3kr1t"}).status_code == 200
        # Header-token callers are exempt from CSRF: no ambient cookie to abuse.
        assert c.put("/admin/settings", json={"log_bodies": True},
                     headers={"X-Admin-Token": "s3kr1t"}).status_code == 200


# ---- account self-service --------------------------------------------
def test_private_toggle_and_key_rotation(auth_client):
    first = signup_user(auth_client, "alice")
    assert auth_client.get("/auth/me").json()["private"] is False
    r = auth_client.patch("/auth/me", json={"private": True})
    assert r.status_code == 200 and r.json()["private"] is True

    rotated = auth_client.post("/auth/me/key").json()["api_key"]
    assert rotated != first and rotated.startswith("rk_")


def test_admin_can_disable_a_user(auth_client):
    signup_user(auth_client, "alice")
    user_id = auth_client.get("/auth/me").json()["user_id"]
    auth_client.post("/auth/logout")

    login_admin(auth_client)
    r = auth_client.patch(f"/admin/users/{user_id}", json={"disabled": True})
    assert r.status_code == 200 and r.json()["disabled"] is True
    assert auth_client.patch("/admin/users/nope", json={}).status_code == 404


def test_admin_has_no_per_user_key_or_privacy(auth_client):
    login_admin(auth_client)
    assert auth_client.post("/auth/me/key").status_code == 400
    assert auth_client.patch("/auth/me", json={"private": True}).status_code == 400


# ---- hardening --------------------------------------------------------
def test_public_bind_without_admin_credential_refuses_to_start(tmp_path):
    cfg = Config(host="0.0.0.0", db_path=tmp_path / "t.db",
                 log_dir=tmp_path / "logs", frontend_dist=tmp_path / "nd")
    with pytest.raises(RuntimeError, match="refusing to start"):
        create_app(cfg)


def test_public_bind_with_admin_credential_starts(tmp_path):
    cfg = Config(host="0.0.0.0", db_path=tmp_path / "t.db",
                 log_dir=tmp_path / "logs", frontend_dist=tmp_path / "nd",
                 probe_interval=9999,
                 admin_password_hash=hash_password("x"))
    assert create_app(cfg) is not None


def test_loopback_without_credentials_stays_open_for_local_dev(client):
    assert client.get("/admin/settings").status_code == 200
    assert client.get("/admin/endpoints").status_code == 200


def test_cors_is_scoped_to_v1(auth_client):
    """A wildcard origin on the cookie-authenticated admin plane would hand any
    website a template for driving this dashboard."""
    login_admin(auth_client)
    admin = auth_client.get("/admin/stats/live")
    assert "access-control-allow-origin" not in admin.headers
    v1 = auth_client.get("/v1/models")
    assert v1.headers.get("access-control-allow-origin") == "*"
    assert "X-Admin-Token" not in v1.headers.get(
        "access-control-allow-headers", "")


def test_frontend_log_ingest_requires_a_session(auth_client):
    body = {"events": [{"level": "info", "event": "x"}]}
    assert auth_client.post("/admin/logs/frontend", json=body).status_code == 401
    signup_user(auth_client, "alice")
    assert auth_client.post("/admin/logs/frontend", json=body).status_code == 200


def test_health_stays_public(auth_client):
    assert auth_client.get("/health").status_code == 200


def test_auth_status_is_public(auth_client):
    r = auth_client.get("/auth/status")
    assert r.status_code == 200
    body = r.json()
    assert body["authenticated"] is False and body["signup_enabled"] is True


def test_admin_credentials_never_come_from_the_database(auth_cfg, tmp_path):
    """A user row named ``admin`` must not become an administrator — admin
    identity is env-only, so DB write access cannot escalate."""
    with TestClient(create_app(auth_cfg)) as c:
        from app.services.users import UserStore
        store = UserStore(c.app.state.db)
        store.create("sneaky", "hunter2hunter2")
        c.app.state.db.execute(
            "UPDATE users SET username = 'admin' WHERE username = 'sneaky'")
        r = c.post("/auth/login",
                   json={"username": "admin", "password": "hunter2hunter2"})
        # The env admin password wins the comparison and this one fails it.
        assert r.status_code == 401


def test_expired_cookie_is_treated_as_anonymous(tmp_path):
    from app.services.users import hash_password as hp

    cfg = Config(db_path=tmp_path / "t.db", log_dir=tmp_path / "logs",
                 frontend_dist=tmp_path / "nd", probe_interval=9999,
                 admin_password_hash=hp(ADMIN_PASSWORD), cookie_secure=False,
                 session_ttl_hours=1 / 3600)  # one second
    with TestClient(create_app(cfg)) as c:
        c.post("/auth/login",
               json={"username": "admin", "password": ADMIN_PASSWORD})
        assert c.get("/admin/settings").status_code == 200
        time.sleep(1.1)
        assert c.get("/admin/settings").status_code == 401


# ---- stats isolation over HTTP ---------------------------------------
def _seed_owned(app, user_id, model, n=1):
    now = time.time()
    for i in range(n):
        app.state.db.execute(
            "INSERT INTO requests (id, ts, endpoint_id, endpoint_name, route,"
            " model, stream, status, ok, prompt_tokens, completion_tokens,"
            " total_tokens, ttft_ms, latency_ms, tokens_per_sec, cost_usd,"
            " user_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"req_{model}_{i}", now - 60 - i, "ep-a", "ep-a",
             "chat.completions", model, 1, 200, 1, 10, 5, 15, 80.0, 900.0,
             40.0, 0.0, user_id))


def test_user_sees_only_their_own_rows_whatever_scope_they_ask_for(auth_client):
    signup_user(auth_client, "alice")
    alice = auth_client.get("/auth/me").json()["user_id"]
    app = auth_client.app
    _seed_owned(app, alice, "alice-model", n=2)
    _seed_owned(app, "someone-else", "bob-model", n=3)

    # scope=me narrows the aggregate to their own traffic...
    mine = auth_client.get("/admin/stats/summary?window=1h&scope=me").json()
    assert mine["requests"] == 2
    # ...while the shared aggregate still counts everyone (attribution-only
    # privacy: volume is shared, identity is not).
    everyone = auth_client.get("/admin/stats/summary?window=1h&scope=all").json()
    assert everyone["requests"] == 5

    # Row-level detail is self-scoped no matter what scope says.
    for scope in ("me", "all"):
        rows = auth_client.get(f"/admin/stats/recent?scope={scope}").json()["rows"]
        assert {r["model"] for r in rows} == {"alice-model"}


def test_admin_sees_every_row(auth_client):
    signup_user(auth_client, "alice")
    alice = auth_client.get("/auth/me").json()["user_id"]
    app = auth_client.app
    _seed_owned(app, alice, "alice-model", n=2)
    _seed_owned(app, "someone-else", "bob-model", n=3)
    auth_client.post("/auth/logout")

    login_admin(auth_client)
    rows = auth_client.get("/admin/stats/recent").json()["rows"]
    assert {r["model"] for r in rows} == {"alice-model", "bob-model"}
    assert auth_client.get(
        "/admin/stats/summary?window=1h&scope=me").json()["requests"] == 5, \
        "scope=me is meaningless for the admin and must not hide rows"
