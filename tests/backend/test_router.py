"""Router: pool CRUD, resolution policies, health state machine."""

import pytest

from app.schemas import EndpointCreate, EndpointPatch
from app.services.router import Router


@pytest.fixture
def router(db) -> Router:
    return Router(db, unhealthy_after=3, recover_after=2)


def _ep(name: str, priority: int = 100, url: str | None = None) -> EndpointCreate:
    return EndpointCreate(
        name=name, base_url=url or f"http://127.0.0.1:70{priority}/v1",
        priority=priority)


def test_create_pins_first_endpoint(router):
    a = router.create(_ep("a"))
    router.create(_ep("b"))
    assert router.pinned_id == a["id"]
    assert router.resolve()["id"] == a["id"]


def test_kind_inferred_from_url(router):
    local = router.create(_ep("l", url="http://127.0.0.1:7070/v1"))
    remote = router.create(_ep("r", url="https://a100.pods.run/v1"))
    assert local["kind"] == "local"
    assert remote["kind"] == "remote_direct"


def test_activate_hot_swaps(router):
    router.create(_ep("a"))
    b = router.create(_ep("b"))
    assert router.activate(b["id"])
    assert router.resolve()["id"] == b["id"]
    assert not router.activate("nope")


def test_failure_state_machine_and_failover(router):
    a = router.create(_ep("a", priority=200))
    b = router.create(_ep("b", priority=100))
    # a pinned; two failures -> degraded but still resolved
    router.report_failure(a["id"], "boom")
    router.report_failure(a["id"], "boom")
    assert router.state[a["id"]].health == "degraded"
    assert router.resolve()["id"] == a["id"]
    # third failure -> failed, drops out; failover to b
    router.report_failure(a["id"], "boom")
    assert router.state[a["id"]].health == "failed"
    assert router.resolve()["id"] == b["id"]
    # without auto-failover the pin is hard
    assert router.resolve(auto_failover=False)["id"] == a["id"]


def test_recovery_needs_consecutive_probe_successes(router):
    a = router.create(_ep("a"))
    for _ in range(3):
        router.report_failure(a["id"], "down")
    assert router.state[a["id"]].health == "failed"
    # request success alone does not recover
    router.report_success(a["id"], 50, source="request")
    assert router.state[a["id"]].health == "failed"
    router.report_success(a["id"], 50, source="probe")
    assert router.state[a["id"]].health == "failed"  # 1 of 2
    router.report_success(a["id"], 50, source="probe")
    assert router.state[a["id"]].health == "healthy"


def test_priority_policy_ignores_pin(router):
    router.create(_ep("low", priority=10))
    hi = router.create(_ep("hi", priority=500))
    router.set_policy("priority", None)
    assert router.resolve()["id"] == hi["id"]


def test_resolve_excludes_and_disabled(router):
    a = router.create(_ep("a", priority=200))
    b = router.create(_ep("b", priority=100))
    assert router.resolve(exclude={a["id"]})["id"] == b["id"]
    router.patch(b["id"], EndpointPatch(enabled=False))
    assert router.resolve(exclude={a["id"]}) is None


def test_delete_moves_pin(router):
    a = router.create(_ep("a"))
    b = router.create(_ep("b"))
    router.delete(a["id"])
    assert router.pinned_id == b["id"]


def _ep_model(name: str, override: str | None) -> EndpointCreate:
    return EndpointCreate(
        name=name, base_url="http://127.0.0.1:7070/v1", alias="local",
        model_override=override)


def test_override_is_the_upstream_model(router):
    a = router.create(_ep_model("a", "gemma-4-26B-it"))
    # Explicit override wins over any discovered model, and is honored even
    # before/without discovery (an exact pin the /models list may not list).
    assert router.upstream_model(a["id"]) == "gemma-4-26B-it"
    router.set_models(a["id"], ["default"])
    assert router.upstream_model(a["id"]) == "gemma-4-26B-it"


def test_clear_model_override_self_heals(router, db):
    a = router.create(_ep_model("a", "gemma-4-26B-it"))
    router.set_models(a["id"], ["qwen-3.6-27B-it", "default"])
    # Simulate the proxy healing a swap: drop the stale override.
    removed = router.clear_model_override(a["id"])
    assert removed == "gemma-4-26B-it"
    # Alias now tracks the discovered model, and it's persisted.
    assert router.endpoints[a["id"]]["model_override"] is None
    assert router.upstream_model(a["id"]) == "qwen-3.6-27B-it"
    assert Router(db).endpoints[a["id"]]["model_override"] is None


def test_clear_model_override_noop_without_override(router):
    a = router.create(_ep_model("a", None))
    assert router.clear_model_override(a["id"]) is None


def test_state_survives_reload(router, db):
    a = router.create(_ep("a"))
    b = router.create(_ep("b"))
    router.activate(b["id"])
    router.set_policy("priority", None)
    fresh = Router(db)
    assert set(fresh.endpoints) == {a["id"], b["id"]}
    assert fresh.pinned_id == b["id"]
    assert fresh.policy == "priority"


# ---- protocol: alias-only routing ----------------------------------------
def _oc(name: str, **extra) -> EndpointCreate:
    return EndpointCreate(
        name=name, base_url="http://opencode", protocol="opencode",
        server_type="opencode", **extra)


def test_opencode_never_wins_auto_or_failover(router):
    openai_ep = router.create(_ep("a"))
    agent = router.create(_oc("agent", alias="agent"))
    # not eligible for `auto`, and not a failover target once the OpenAI one
    # is excluded — an agent server is only ever reached by name.
    assert router.resolve()["id"] == openai_ep["id"]
    assert router.resolve(exclude={openai_ep["id"]}) is None
    # ...but it is still reachable through its alias.
    assert router.resolve_alias("agent")["id"] == agent["id"]


def test_opencode_is_not_auto_pinned_as_the_first_endpoint(router):
    agent = router.create(_oc("agent", alias="agent"))
    assert router.pinned_id is None
    assert router.resolve() is None
    openai_ep = router.create(_ep("a"))
    assert router.pinned_id == openai_ep["id"]
    assert agent["protocol"] == "opencode"


def test_pinning_an_opencode_endpoint_does_not_capture_auto(router):
    openai_ep = router.create(_ep("a"))
    agent = router.create(_oc("agent", alias="agent"))
    router.activate(agent["id"])
    assert router.resolve()["id"] == openai_ep["id"]


# ---- available_models allowlist -------------------------------------------
def test_allowlist_round_trips_and_routes(router):
    ep = router.create(_oc("agent", alias="agent", available_models=[
        "anthropic/claude-sonnet-4-5", "openai/gpt-5"]))
    assert router.available_models(ep["id"]) == [
        "anthropic/claude-sonnet-4-5", "openai/gpt-5"]
    row, exact = router.resolve_request_model("openai/gpt-5")
    assert row["id"] == ep["id"] and exact == "openai/gpt-5"
    # an alias match reports no exact model: the endpoint chooses
    row, exact = router.resolve_request_model("agent")
    assert row["id"] == ep["id"] and exact is None
    assert router.resolve_request_model("nope") == (None, None)


def test_allowlist_survives_reload(router, db):
    router.create(_oc("agent", alias="agent",
                      available_models=["openai/gpt-5"]))
    reloaded = Router(db)
    eid = next(iter(reloaded.endpoints))
    assert reloaded.available_models(eid) == ["openai/gpt-5"]


def test_allowlist_rejects_collisions(router):
    router.create(_ep("a", url="http://127.0.0.1:7070/v1"))
    router.create(_oc("agent", alias="agent",
                      available_models=["openai/gpt-5"]))
    with pytest.raises(ValueError, match="already served"):
        router.create(_oc("other", alias="other",
                          available_models=["openai/gpt-5"]))
    with pytest.raises(ValueError, match="already the alias"):
        router.create(_oc("other", alias="other",
                          available_models=["agent"]))
    with pytest.raises(ValueError, match="reserved"):
        router.create(_oc("other", alias="other", available_models=["auto"]))
    with pytest.raises(ValueError, match="routing name"):
        router.create(_oc("other", alias="other",
                          available_models=["has space"]))


def test_allowlist_dedupes_and_can_be_cleared(router):
    ep = router.create(_oc("agent", alias="agent", available_models=[
        "openai/gpt-5", "openai/gpt-5", " openai/gpt-5 "]))
    assert router.available_models(ep["id"]) == ["openai/gpt-5"]
    router.patch(ep["id"], EndpointPatch(available_models=[]))
    assert router.available_models(ep["id"]) == []


def test_allowlist_narrows_discovered_models_immediately(router):
    ep = router.create(_oc("agent", alias="agent"))
    router.set_models(ep["id"], ["a/one", "a/two", "a/three"])
    assert router.state[ep["id"]].model == "a/one"

    router.patch(ep["id"], EndpointPatch(available_models=["a/three"]))
    st = router.state[ep["id"]]
    assert st.models == ["a/three"] and st.model == "a/three"
    assert st.discovered == ["a/one", "a/two", "a/three"]

    # clearing it restores the full discovered list without re-probing
    router.patch(ep["id"], EndpointPatch(available_models=[]))
    assert router.state[ep["id"]].model == "a/one"


def test_allowlisted_model_the_probe_missed_is_still_offered(router):
    """A provider catalog can lag what the provider will actually serve, so an
    operator's explicit list is not silently emptied by a stale probe."""
    ep = router.create(_oc("agent", alias="agent",
                           available_models=["anthropic/brand-new"]))
    router.set_models(ep["id"], ["anthropic/old-one"])
    assert router.state[ep["id"]].models == ["anthropic/brand-new"]
