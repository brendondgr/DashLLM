"""Stats service shapes: ECharts dimensions/source, bucketing, percentiles."""

import time

import pytest

from app.services.rollup import HIST_UPSERT, SUM_UPSERT, build_rollup_rows
from app.services.stats import StatsService, _percentile
from app.services.telemetry import LiveTracker


@pytest.fixture
def stats(db):
    return StatsService(db, LiveTracker())


def _seed(db, ts, tin=100, tout=50, ok=1, model="m1", ep="ep-a",
          ttft=80.0, lat=1000.0, tps=50.0):
    """Insert a raw request and maintain the hourly rollups, mirroring the
    live write path so rollup-backed queries (24h/7d/30d/1y) see the row."""
    db.execute(
        "INSERT INTO requests (id, ts, endpoint_id, endpoint_name, route,"
        " model, stream, status, ok, prompt_tokens, completion_tokens,"
        " total_tokens, ttft_ms, latency_ms, tokens_per_sec, cost_usd)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"req_{ts}_{model}_{ep}_{tin}_{tout}", ts, ep, ep, "chat.completions",
         model, 1, 200 if ok else 500, ok, tin, tout, tin + tout,
         ttft, lat, tps, 0.0))
    sum_rows, hist_rows = build_rollup_rows([{
        "ts": ts, "endpoint_id": ep, "endpoint_name": ep, "model": model,
        "ok": ok, "prompt_tokens": tin, "completion_tokens": tout,
        "total_tokens": tin + tout, "cost_usd": 0.0, "ttft_ms": ttft,
        "latency_ms": lat, "tokens_per_sec": tps,
    }])
    for row in sum_rows:
        db.execute(SUM_UPSERT, row)
    for row in hist_rows:
        db.execute(HIST_UPSERT, row)


def test_percentile():
    assert _percentile([], 50) is None
    assert _percentile([10.0], 95) == 10.0
    assert _percentile([1, 2, 3, 4], 50) == 2.5


async def test_summary_counts_and_percentiles(db, stats):
    now = time.time()
    _seed(db, now - 60, ttft=100, lat=1000, tps=40)
    _seed(db, now - 120, ttft=200, lat=2000, tps=60, ok=0)
    s = await stats.summary("24h")
    assert s["requests"] == 2 and s["errors"] == 1
    assert s["error_rate"] == 0.5
    assert s["prompt_tokens"] == 200 and s["completion_tokens"] == 100
    assert s["ttft_ms"]["p50"] == 150.0
    assert s["tokens_per_sec"]["p50"] == 50.0


async def test_volume_shape_and_zero_fill(db, stats):
    now = time.time()
    _seed(db, now - 30)
    v = await stats.volume("1h")
    assert v["dimensions"] == ["time", "requests", "errors"]
    assert len(v["source"]) >= 60  # zero-filled minute buckets
    assert sum(row[1] for row in v["source"]) == 1


async def test_tokens_by_hour_sums_across_days(db, stats):
    now = time.time()
    lt = time.localtime(now)
    # two rows at the same local hour on different days
    base = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 14, 30, 0,
                        0, 0, -1))
    _seed(db, base - 86400, tin=10, tout=5)
    _seed(db, base - 2 * 86400, tin=20, tout=10)
    bh = await stats.tokens_by_hour("7d")
    assert bh["dimensions"] == ["hour", "input", "output"]
    assert len(bh["source"]) == 24
    hour14 = bh["source"][14]
    assert hour14 == [14, 30, 15]


async def test_tokens_by_day_dense_dates(db, stats):
    now = time.time()
    _seed(db, now - 86400, tin=11, tout=7)
    bd = await stats.tokens_by_day("7d")
    assert bd["dimensions"] == ["date", "input", "output"]
    assert len(bd["source"]) >= 7
    assert sum(r[1] for r in bd["source"]) == 11


async def test_by_model_and_by_endpoint(db, stats):
    now = time.time()
    _seed(db, now - 10, model="m1", ep="ep-a")
    _seed(db, now - 20, model="m2", ep="ep-b", ok=0)
    _seed(db, now - 30, model="m2", ep="ep-b")
    bm = await stats.by_model("24h")
    models = {r[0]: r for r in bm["source"]}
    assert models["m2"][1] == 2 and models["m2"][5] == 1  # requests, errors
    be = await stats.by_endpoint("24h")
    eps = {r[0]: r for r in be["source"]}
    assert eps["ep-b"][1] == 2
    assert abs(eps["ep-b"][5] - 2 / 3) < 0.01  # share


async def test_endpoint_filter(db, stats):
    now = time.time()
    _seed(db, now - 10, ep="ep-a")
    _seed(db, now - 20, ep="ep-b")
    s = await stats.summary("24h", endpoint_id="ep-a")
    assert s["requests"] == 1


async def test_latency_series_shape(db, stats):
    now = time.time()
    _seed(db, now - 30, ttft=100, tps=40)
    lat = await stats.latency("1h")
    assert lat["dimensions"] == ["time", "ttft_p50", "ttft_p95", "tps_p50"]
    nonzero = [r for r in lat["source"] if r[1] > 0]
    assert len(nonzero) == 1 and nonzero[0][3] == 40


async def test_recent_merges_in_flight(db, stats):
    now = time.time()
    _seed(db, now - 10)
    stats.live.start("req_live1", {
        "ts": now, "model": "m1", "endpoint_name": "ep-a",
        "route": "chat.completions", "stream": True})
    out = await stats.recent(limit=10)
    assert out["counts"]["streaming"] == 1
    assert out["counts"]["done"] == 1
    assert out["rows"][0]["state"] == "streaming"
    assert out["rows"][0]["id"] == "req_live1"


async def test_volume_24h_summary_is_hourly(db, stats):
    now = time.time()
    _seed(db, now - 1800)
    v = await stats.volume("24h", detail="summary")
    # 24h of hourly buckets -> ~25 dense keys, each an ISO hour string.
    assert 24 <= len(v["source"]) <= 26
    assert all("T" in row[0] and row[0].endswith(":00") for row in v["source"])
    assert sum(row[1] for row in v["source"]) == 1


async def test_volume_24h_detailed_is_15min(db, stats):
    now = time.time()
    _seed(db, now - 1800)
    v = await stats.volume("24h", detail="detailed")
    # 15-minute buckets: 4x the hourly count, minute part is one of 00/15/30/45.
    assert len(v["source"]) >= 90
    mins = {row[0][-2:] for row in v["source"]}
    assert mins <= {"00", "15", "30", "45"}
    assert sum(row[1] for row in v["source"]) == 1


async def test_tokens_7d_summary_is_daily(db, stats):
    now = time.time()
    _seed(db, now - 2 * 86400, tin=10, tout=5)
    t = await stats.tokens_timeseries("7d", detail="summary")
    assert t["dimensions"] == ["time", "input", "output"]
    assert 7 <= len(t["source"]) <= 8
    # daily keys look like YYYY-MM-DD (no time component)
    assert all("T" not in row[0] and row[0].count("-") == 2
               for row in t["source"])
    assert sum(row[1] for row in t["source"]) == 10


async def test_tokens_30d_detailed_is_3hour(db, stats):
    now = time.time()
    _seed(db, now - 3 * 86400, tin=7, tout=3)
    t = await stats.tokens_timeseries("30d", detail="detailed")
    # 3-hour buckets over 30 days -> 8 per day, hour part multiple of 3.
    assert len(t["source"]) >= 200
    hours = {int(row[0].split("T")[1][:2]) for row in t["source"]}
    assert all(h % 3 == 0 for h in hours)
    assert sum(row[1] for row in t["source"]) == 7


async def test_volume_1y_summary_is_monthly(db, stats):
    now = time.time()
    _seed(db, now - 40 * 86400, tin=5, tout=2)
    v = await stats.volume("1y", detail="summary")
    # 12-13 month buckets, keyed YYYY-MM
    assert 12 <= len(v["source"]) <= 13
    assert all(len(row[0]) == 7 and row[0][4] == "-" for row in v["source"])
    assert sum(row[1] for row in v["source"]) == 1


async def test_volume_1y_detailed_is_daily(db, stats):
    now = time.time()
    _seed(db, now - 40 * 86400, tin=5, tout=2)
    v = await stats.volume("1y", detail="detailed")
    assert len(v["source"]) >= 360
    assert sum(row[1] for row in v["source"]) == 1


async def test_summary_1y_window(db, stats):
    now = time.time()
    _seed(db, now - 100 * 86400)
    _seed(db, now - 30)
    s = await stats.summary("1y")
    assert s["requests"] == 2


async def test_custom_range(db, stats):
    now = time.time()
    _seed(db, now - 3 * 86400)
    _seed(db, now - 30)
    s = await stats.summary(None, from_ts=now - 4 * 86400,
                            to_ts=now - 2 * 86400)
    assert s["requests"] == 1
