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


def test_state_survives_reload(router, db):
    a = router.create(_ep("a"))
    b = router.create(_ep("b"))
    router.activate(b["id"])
    router.set_policy("priority", None)
    fresh = Router(db)
    assert set(fresh.endpoints) == {a["id"], b["id"]}
    assert fresh.pinned_id == b["id"]
    assert fresh.policy == "priority"
