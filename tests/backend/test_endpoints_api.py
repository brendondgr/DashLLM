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


def test_proxy_info_reports_no_key(client):
    """relay takes no API key, so /admin/proxy must not invent one — a key
    field here is what put "Authorization: Bearer" back into every snippet."""
    info = client.get("/admin/proxy").json()
    assert info["base_url"].endswith("/v1")
    assert "api_key" not in info and "api_key_masked" not in info
    assert client.post("/admin/proxy/key").status_code >= 400


def test_frontend_log_ingestion(client):
    r = client.post("/admin/logs/frontend", json={"events": [
        {"level": "info", "event": "screen.switch", "detail": "dash->eps"},
        {"level": "error", "event": "api.fail", "detail": "boom"},
    ]})
    assert r.json()["accepted"] == 2
    rows = client.get("/admin/logs/frontend").json()
    assert len(rows) == 2
    assert {row["event"] for row in rows} == {"screen.switch", "api.fail"}


def test_admin_plane_is_open(client):
    """No auth layer: /admin answers without a token, and an X-Admin-Token
    header is simply ignored rather than half-enforced."""
    assert client.get("/admin/settings").status_code == 200
    assert client.get(
        "/admin/settings", headers={"X-Admin-Token": "anything"}
    ).status_code == 200


def test_protocol_and_available_models_round_trip(client):
    r = client.post("/admin/endpoints", json={
        "name": "agent", "base_url": "http://127.0.0.1:4096",
        "protocol": "opencode", "server_type": "opencode", "alias": "agent",
        "available_models": ["anthropic/claude-sonnet-4-5", "openai/gpt-5"]})
    assert r.status_code == 201, r.text
    ep = r.json()
    assert ep["protocol"] == "opencode"
    assert ep["available_models"] == ["anthropic/claude-sonnet-4-5",
                                      "openai/gpt-5"]

    r = client.patch(f"/admin/endpoints/{ep['id']}",
                     json={"available_models": ["openai/gpt-5"]})
    assert r.status_code == 200
    assert r.json()["available_models"] == ["openai/gpt-5"]

    assert client.get("/admin/endpoints").json()[0]["protocol"] == "opencode"


def test_endpoint_defaults_to_the_openai_protocol(client):
    r = client.post("/admin/endpoints", json={
        "name": "plain", "base_url": "http://127.0.0.1:7070/v1"})
    assert r.status_code == 201
    assert r.json()["protocol"] == "openai"
    assert r.json()["available_models"] == []


def test_colliding_available_model_is_a_422(client):
    client.post("/admin/endpoints", json={
        "name": "a", "base_url": "http://127.0.0.1:7070/v1",
        "available_models": ["openai/gpt-5"]})
    r = client.post("/admin/endpoints", json={
        "name": "b", "base_url": "http://127.0.0.1:9090/v1",
        "available_models": ["openai/gpt-5"]})
    assert r.status_code == 422
    assert "already served" in r.json()["detail"]


def test_unknown_protocol_is_rejected(client):
    r = client.post("/admin/endpoints", json={
        "name": "x", "base_url": "http://127.0.0.1:7070/v1",
        "protocol": "telepathy"})
    assert r.status_code == 422
