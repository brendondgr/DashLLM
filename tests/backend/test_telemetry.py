"""Telemetry writer + live tracker behavior."""

import time

from app.services.telemetry import LiveTracker, RequestRecord, TelemetryWriter


def _rec(i: int, ts: float | None = None) -> RequestRecord:
    return RequestRecord(
        id=f"req_{i}", ts=ts or time.time(), route="chat.completions",
        model="gemma", endpoint_name="ep", stream=True, status=200, ok=True,
        prompt_tokens=10, completion_tokens=20, total_tokens=30,
        ttft_ms=50.0, latency_ms=800.0, tokens_per_sec=25.0,
    )


async def test_writer_persists_records(db):
    w = TelemetryWriter(db)
    await w.start()
    for i in range(25):
        w.submit(_rec(i))
    await w.flush()
    await w.stop()
    rows = db.query("SELECT COUNT(*) AS n FROM requests")
    assert rows[0]["n"] == 25
    assert w.written == 25


async def test_writer_bodies_only_when_present(db):
    w = TelemetryWriter(db)
    await w.start()
    r = _rec(1)
    r.prompt_body = "hello"
    r.completion_body = "world"
    w.submit(r)
    w.submit(_rec(2))
    await w.flush()
    await w.stop()
    bodies = db.query("SELECT * FROM request_bodies")
    assert len(bodies) == 1 and bodies[0]["id"] == "req_1"


async def test_prune_respects_retention(db):
    w = TelemetryWriter(db)
    await w.start()
    w.submit(_rec(1, ts=time.time() - 40 * 86400))  # too old
    w.submit(_rec(2))  # fresh
    await w.flush()
    n = await w.prune(retention_days=30)
    await w.stop()
    assert n == 1
    rows = db.query("SELECT id FROM requests")
    assert [r["id"] for r in rows] == ["req_2"]


def test_live_tracker_flow():
    lt = LiveTracker()
    lt.start("a", {"model": "m", "client_key": "…abcd"})
    lt.start("b", {"model": "m"})
    assert lt.snapshot(18)["in_flight"] == 2
    lt.bump("a", 42)
    assert lt.in_flight["a"]["completion_tokens"] == 42
    lt.set_ttft("a", 77.0)
    assert lt.in_flight["a"]["ttft_ms"] == 77.0
    lt.finish("a")
    snap = lt.snapshot(18)
    assert snap["in_flight"] == 1
    assert snap["max_concurrency"] == 18
    assert snap["series"][-1][1] == 1
    assert lt.requests_total == 2
    assert lt.active_clients() == 1
