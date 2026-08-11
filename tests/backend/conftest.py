"""Shared fixtures: isolated app + DB per test in tmp_path."""

import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.db import Database
from app.main import create_app


@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config(
        db_path=tmp_path / "test.db",
        log_dir=tmp_path / "logs",
        frontend_dist=tmp_path / "no-dist",
        probe_interval=9999,  # tests drive probes manually
        admin_token="",
    )


@pytest.fixture
def db(tmp_path) -> Database:
    d = Database(tmp_path / "unit.db")
    yield d
    d.close()


@pytest.fixture
def app(cfg):
    return create_app(cfg)


@pytest.fixture
def client(app):
    with TestClient(app) as c:  # context manager runs lifespan
        yield c


ADMIN_PASSWORD = "correct horse battery staple"
SIGNUP_CODE = "let-me-in-please"


@pytest.fixture
def auth_cfg(tmp_path) -> Config:
    """Config with auth actually turned on, unlike ``cfg`` which models the
    loopback-open local-dev default."""
    from app.services.users import hash_password

    return Config(
        db_path=tmp_path / "test.db",
        log_dir=tmp_path / "logs",
        frontend_dist=tmp_path / "no-dist",
        probe_interval=9999,
        admin_user="admin",
        admin_password_hash=hash_password(ADMIN_PASSWORD),
        signup_code=SIGNUP_CODE,
        cookie_secure=False,  # TestClient speaks plain http
    )


@pytest.fixture
def auth_client(auth_cfg):
    with TestClient(create_app(auth_cfg)) as c:
        yield c


def login_admin(client) -> None:
    r = client.post("/auth/login",
                    json={"username": "admin", "password": ADMIN_PASSWORD})
    assert r.status_code == 200, r.text
    _arm_csrf(client)


def signup_user(client, username: str, password: str = "hunter2hunter2") -> str:
    """Registers and logs in as ``username``; returns the cleartext API key."""
    r = client.post("/auth/signup", json={
        "username": username, "password": password, "code": SIGNUP_CODE})
    assert r.status_code == 201, r.text
    _arm_csrf(client)
    return r.json()["api_key"]


def _arm_csrf(client) -> None:
    """Mirror what api.ts does: echo the readable CSRF cookie in a header."""
    client.headers["X-Relay-CSRF"] = client.cookies.get("relay_csrf", "")


@pytest.fixture
def proxy_env(cfg):
    """App wired to a fake upstream via a routing transport.

    Yields (client, app, upstream_calls). Hosts: good / flaky / dead.
    """
    import httpx

    from fake_upstream import RoutingTransport, make_upstream

    upstream, calls = make_upstream()
    app = create_app(cfg)
    with TestClient(app) as c:
        fake = httpx.AsyncClient(
            transport=RoutingTransport(upstream), timeout=5.0)
        app.state.http = fake
        app.state.proxy.http = fake
        app.state.prober.http = fake
        yield c, app, calls


@pytest.fixture
def opencode_env(cfg):
    """App wired to a fake ``opencode serve`` on host ``opencode``, alongside
    the usual good/flaky/strict hosts.

    Yields (client, app, opencode_calls).
    """
    import httpx

    from fake_upstream import (
        RoutingTransport, make_opencode_upstream, make_upstream,
    )

    upstream, _ = make_upstream()
    oc_app, oc_calls = make_opencode_upstream()
    app = create_app(cfg)
    with TestClient(app) as c:
        fake = httpx.AsyncClient(
            transport=RoutingTransport(upstream, oc_app), timeout=5.0)
        app.state.http = fake
        app.state.proxy.http = fake
        app.state.prober.http = fake
        yield c, app, oc_calls
