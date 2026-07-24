"""Proxy hot path against the fake upstream: verbatim passthrough, usage
capture, TTFT, stream_options injection, and pre-first-byte failover."""

import time


def _register(client, name, url, priority=100, **extra):
    r = client.post("/admin/endpoints", json={
        "name": name, "base_url": url, "priority": priority, **extra})
    assert r.status_code == 201, r.text
    return r.json()


def _wait_rows(app, n, timeout=3.0) -> list[dict]:
    """Telemetry is written off the hot path; poll until n rows land."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = app.state.db.query("SELECT * FROM requests ORDER BY ts")
        if len(rows) >= n:
            return rows
        time.sleep(0.02)
    raise AssertionError(f"telemetry rows never reached {n}")


def test_non_streaming_passthrough_and_telemetry(proxy_env):
    client, app, calls = proxy_env
    _register(client, "good", "http://good/v1")
    r = client.post("/v1/chat/completions", json={
        "model": "fake-model-7b", "temperature": 0.7, "max_tokens": 64,
        "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    body = r.json()
    assert body["choices"][0]["message"]["content"] == "hello"
    assert body["usage"]["total_tokens"] == 10  # verbatim passthrough

    row = _wait_rows(app, 1)[-1]
    assert row["route"] == "chat.completions"
    assert row["model"] == "fake-model-7b"
    assert row["endpoint_name"] == "good"
    assert row["ok"] == 1 and row["status"] == 200
    assert row["prompt_tokens"] == 7 and row["completion_tokens"] == 3
    assert row["temperature"] == 0.7 and row["max_tokens"] == 64
    assert row["stream"] == 0
    assert row["latency_ms"] > 0
    assert row["tokens_per_sec"] > 0


def test_streaming_tee_usage_and_ttft(proxy_env):
    client, app, calls = proxy_env
    _register(client, "good", "http://good/v1")
    r = client.post("/v1/chat/completions", json={
        "model": "fake-model-7b", "stream": True,
        "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    text = r.text
    # stream reached the client unchanged, terminator included
    assert text.count("data:") == 7  # 5 deltas + usage chunk + [DONE]
    assert "tok0" in text and "data: [DONE]" in text
    # relay injected stream_options.include_usage upstream
    assert calls["saw_include_usage"] is True

    row = _wait_rows(app, 1)[-1]
    assert row["stream"] == 1 and row["ok"] == 1
    assert row["prompt_tokens"] == 7 and row["completion_tokens"] == 5
    assert row["ttft_ms"] is not None and row["ttft_ms"] > 0
    assert row["tokens_per_sec"] > 0


def test_stream_usage_injection_can_be_disabled(proxy_env):
    client, app, calls = proxy_env
    _register(client, "good", "http://good/v1")
    client.put("/admin/settings", json={"inject_stream_usage": False})
    r = client.post("/v1/chat/completions", json={
        "model": "fake-model-7b", "stream": True,
        "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert calls["saw_include_usage"] is False
    assert '"usage"' not in r.text  # upstream emitted no usage chunk
    # fallback: approximate completion tokens from SSE event count
    row = _wait_rows(app, 1)[-1]
    assert row["completion_tokens"] == 6  # 5 deltas + [DONE] estimate


def test_models_catalog_lists_auto_and_aliases(proxy_env):
    client, app, calls = proxy_env
    _register(client, "good", "http://good/v1", alias="skynet")
    _register(client, "other", "http://good/v1", alias="local")
    r = client.get("/v1/models")
    assert r.status_code == 200
    ids = [m["id"] for m in r.json()["data"]]
    assert ids[0] == "auto"
    assert set(ids) == {"auto", "skynet", "local"}
    skynet = next(m for m in r.json()["data"] if m["id"] == "skynet")
    # prober discovered the real upstream model behind the alias
    assert skynet["relay"]["upstream_model"] == "fake-model-7b"


def test_alias_routing_rewrites_model(proxy_env):
    client, app, calls = proxy_env
    _register(client, "boring", "http://dead/v1", priority=500)  # pinned
    _register(client, "skynet-box", "http://good/v1", priority=1,
              alias="skynet")
    # requesting model=skynet must bypass the pinned dead endpoint entirely
    r = client.post("/v1/chat/completions", json={
        "model": "skynet", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    # the upstream received its real model name, not the alias
    assert calls["last_model"] == "fake-model-7b"
    row = _wait_rows(app, 1)[-1]
    assert row["endpoint_name"] == "skynet-box"
    assert row["model"] == "fake-model-7b"


def test_alias_routing_respects_model_override(proxy_env):
    client, app, calls = proxy_env
    _register(client, "ov", "http://good/v1", alias="local",
              model_override="my-exact-model")
    r = client.post("/v1/chat/completions", json={
        "model": "LOCAL",  # case-insensitive alias match
        "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert calls["last_model"] == "my-exact-model"


def test_stale_model_override_self_heals(proxy_env):
    client, app, calls = proxy_env
    # Port pins a model that it no longer serves (the model was swapped out).
    _register(client, "swapped", "http://strict/v1", alias="local",
              model_override="gemma-4-26B-it")
    router = app.state.router
    eid = router.resolve_alias("local")["id"]
    # Registration probed /models and discovered the live model.
    assert router.state[eid].model == "fake-model-7b"

    r = client.post("/v1/chat/completions", json={
        "model": "local", "messages": [{"role": "user", "content": "hi"}]})
    # First send used the stale override -> upstream 404; the proxy cleared the
    # override and retried the SAME port with the discovered model -> success.
    assert r.status_code == 200, r.text
    row = _wait_rows(app, 1)[-1]
    assert row["model"] == "fake-model-7b" and row["ok"] == 1
    # The stale override is gone for good (persisted), so it self-healed.
    assert router.endpoints[eid]["model_override"] is None
    assert app.state.db.query_one(
        "SELECT model_override FROM endpoints WHERE id = ?",
        (eid,))["model_override"] is None


def test_valid_override_is_not_disturbed(proxy_env):
    # An override the upstream accepts (even if not in /v1/models) must stay.
    client, app, calls = proxy_env
    _register(client, "ov", "http://good/v1", alias="local",
              model_override="my-exact-model")
    r = client.post("/v1/chat/completions", json={
        "model": "local", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert calls["last_model"] == "my-exact-model"
    eid = app.state.router.resolve_alias("local")["id"]
    assert app.state.router.endpoints[eid]["model_override"] == "my-exact-model"


def test_alias_routing_does_not_fail_over(proxy_env):
    client, app, calls = proxy_env
    _register(client, "good", "http://good/v1", priority=500)
    _register(client, "dead-named", "http://dead/v1", alias="skynet",
              priority=1)
    # the caller asked for skynet by name; a healthy sibling must NOT be
    # silently substituted
    r = client.post("/v1/chat/completions", json={
        "model": "skynet", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 502
    assert calls["chat"] == 0
    row = _wait_rows(app, 1)[-1]
    assert row["endpoint_name"] == "dead-named" and row["ok"] == 0


def test_auto_model_rewritten_to_endpoint_model(proxy_env):
    client, app, calls = proxy_env
    _register(client, "good", "http://good/v1")
    r = client.post("/v1/chat/completions", json={
        "model": "auto", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    # upstream never sees the literal "auto" — prober-discovered model is sent
    assert calls["last_model"] == "fake-model-7b"


def test_duplicate_alias_rejected(proxy_env):
    client, app, calls = proxy_env
    _register(client, "a", "http://good/v1", alias="skynet")
    r = client.post("/admin/endpoints", json={
        "name": "b", "base_url": "http://good/v1", "alias": "SKYNET"})
    assert r.status_code == 422
    r = client.post("/admin/endpoints", json={
        "name": "c", "base_url": "http://good/v1", "alias": "auto"})
    assert r.status_code == 422


def test_failover_before_first_byte(proxy_env):
    client, app, calls = proxy_env
    dead = _register(client, "dead-box", "http://dead/v1", priority=200)
    _register(client, "good", "http://good/v1", priority=100)
    # dead-box is pinned (first created); request must fail over to good
    r = client.post("/v1/chat/completions", json={
        "model": "fake-model-7b",
        "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "hello"
    row = _wait_rows(app, 1)[-1]
    assert row["endpoint_name"] == "good" and row["ok"] == 1
    # passive signal degraded the dead endpoint
    assert app.state.router.state[dead["id"]].consecutive_fails >= 1


def test_5xx_upstream_triggers_failover(proxy_env):
    client, app, calls = proxy_env
    _register(client, "flaky", "http://flaky/v1", priority=200)
    _register(client, "good", "http://good/v1", priority=100)
    r = client.post("/v1/chat/completions", json={
        "model": "fake-model-7b",
        "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert _wait_rows(app, 1)[-1]["endpoint_name"] == "good"


def test_no_failover_when_disabled(proxy_env):
    client, app, calls = proxy_env
    _register(client, "dead-box", "http://dead/v1", priority=200)
    _register(client, "good", "http://good/v1", priority=100)
    client.put("/admin/settings", json={"auto_failover": False})
    r = client.post("/v1/chat/completions", json={
        "model": "fake-model-7b",
        "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 502
    assert r.json()["error"]["type"] == "relay_proxy_error"
    row = _wait_rows(app, 1)[-1]
    assert row["ok"] == 0 and row["error"]


def test_no_endpoints_returns_503(proxy_env):
    client, app, calls = proxy_env
    r = client.post("/v1/chat/completions", json={"model": "x",
                                                  "messages": []})
    assert r.status_code == 503
    assert "no healthy" in r.json()["error"]["message"]


def test_bodies_captured_only_when_enabled(proxy_env):
    client, app, calls = proxy_env
    _register(client, "good", "http://good/v1")
    client.post("/v1/chat/completions", json={
        "model": "fake-model-7b",
        "messages": [{"role": "user", "content": "secret prompt"}]})
    _wait_rows(app, 1)
    assert app.state.db.query("SELECT * FROM request_bodies") == []

    client.put("/admin/settings", json={"log_bodies": True})
    client.post("/v1/chat/completions", json={
        "model": "fake-model-7b",
        "messages": [{"role": "user", "content": "logged prompt"}]})
    _wait_rows(app, 2)
    bodies = app.state.db.query("SELECT * FROM request_bodies")
    assert len(bodies) == 1
    assert "logged prompt" in bodies[0]["prompt"]
