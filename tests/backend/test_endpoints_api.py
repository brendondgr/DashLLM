"""Admin control-plane API over the live app (TestClient runs lifespan)."""


def _create(client, name="ep-a", url="http://127.0.0.1:1/v1", **kw):
    body = {"name": name, "base_url": url, **kw}
    r = client.post("/admin/endpoints", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def test_endpoint_crud_flow(client):
    ep = _create(client)
    assert ep["active"] is True  # first endpoint auto-pinned
    assert ep["has_key"] is False

    r = client.get("/admin/endpoints")
    assert [e["id"] for e in r.json()] == [ep["id"]]

    r = client.patch(f"/admin/endpoints/{ep['id']}", json={"priority": 250})
    assert r.status_code == 200 and r.json()["priority"] == 250

    r = client.delete(f"/admin/endpoints/{ep['id']}")
    assert r.status_code == 204
    assert client.get("/admin/endpoints").json() == []


def test_create_rejects_bad_url(client):
    r = client.post("/admin/endpoints",
                    json={"name": "x", "base_url": "ftp://nope"})
    assert r.status_code == 422


def test_activate_and_router_state(client):
    a = _create(client, "a")
    b = _create(client, "b")
    r = client.post(f"/admin/endpoints/{b['id']}/activate")
    assert r.status_code == 200
    state = r.json()
    assert state["pinned_id"] == b["id"]
    assert client.get("/admin/router").json()["pinned_id"] == b["id"]

    r = client.put("/admin/router", json={"policy": "priority"})
    assert r.json()["policy"] == "priority"
    # unknown pin rejected
    assert client.put("/admin/router", json={"pinned_id": "zzz"}).status_code == 404
    # cleanup pins: a still exists
    assert client.post(f"/admin/endpoints/{a['id']}/activate").status_code == 200


def test_test_endpoint_unreachable_reports_error(client):
    ep = _create(client, "dead", url="http://127.0.0.1:1/v1")
    r = client.post(f"/admin/endpoints/{ep['id']}/test")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and body["error"]


def test_pool_health_snapshot(client):
    _create(client, "a")
    r = client.get("/admin/endpoints/health")
    assert r.status_code == 200
    body = r.json()
    assert len(body["endpoints"]) == 1
    assert "resolved" in body


def test_settings_roundtrip(client):
    r = client.get("/admin/settings")
    assert r.status_code == 200 and r.json()["auto_failover"] is True
    r = client.put("/admin/settings", json={"auto_failover": False,
                                            "retention_days": 7})
    body = r.json()
    assert body["auto_failover"] is False and body["retention_days"] == 7
    # persisted
    assert client.get("/admin/settings").json()["retention_days"] == 7


def test_settings_port_change_flags_restart(client):
    r = client.put("/admin/settings", json={"proxy_port": 4055})
    assert r.json()["restart_required"] is True


def test_proxy_info_and_key_regen(client):
    r = client.get("/admin/proxy")
    info = r.json()
    assert info["base_url"].endswith("/v1")
    assert info["api_key"].startswith("sk-relay-")
    old = info["api_key"]
    new = client.post("/admin/proxy/key").json()["api_key"]
    assert new != old
    assert client.get("/admin/proxy").json()["api_key"] == new


def test_frontend_log_ingestion(client):
    r = client.post("/admin/logs/frontend", json={"events": [
        {"level": "info", "event": "screen.switch", "detail": "dash->eps"},
        {"level": "error", "event": "api.fail", "detail": "boom"},
    ]})
    assert r.json()["accepted"] == 2
    rows = client.get("/admin/logs/frontend").json()
    assert len(rows) == 2
    assert {row["event"] for row in rows} == {"screen.switch", "api.fail"}


def test_admin_token_enforced(cfg):
    from fastapi.testclient import TestClient

    from app.main import create_app

    cfg2 = cfg.model_copy(update={"admin_token": "sekrit"})
    app = create_app(cfg2)
    with TestClient(app) as c:
        assert c.get("/admin/settings").status_code == 401
        ok = c.get("/admin/settings", headers={"X-Admin-Token": "sekrit"})
        assert ok.status_code == 200
