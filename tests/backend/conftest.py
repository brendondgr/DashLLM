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
