"""Hourly rollup aggregation shared by the live telemetry writer and the
one-time backfill migration.

A "rollup" pre-aggregates finished requests into hourly buckets keyed by
``(bucket_hour, endpoint_id, model)`` so the dashboard reads a handful of small
rows instead of scanning the raw ``requests`` table. Counts, sums and per-metric
histogram buckets are all additive, so both the live path (one batch at a time)
and the backfill (whole table) funnel through :func:`build_rollup_rows` and emit
the same additive UPSERTs.
"""

from app.services.histogram import METRICS, bucket_index

HOUR = 3600

# Additive upserts: conflicting keys accumulate rather than replace. endpoint_name
# is denormalized so historical rows survive an endpoint being deleted.
SUM_UPSERT = """
INSERT INTO request_rollup_hourly
  (bucket_hour, endpoint_id, model, n, errors, prompt_tokens,
   completion_tokens, total_tokens, cost_usd, tps_sum, tps_n, endpoint_name)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(bucket_hour, endpoint_id, model) DO UPDATE SET
  n = n + excluded.n,
  errors = errors + excluded.errors,
  prompt_tokens = prompt_tokens + excluded.prompt_tokens,
  completion_tokens = completion_tokens + excluded.completion_tokens,
  total_tokens = total_tokens + excluded.total_tokens,
  cost_usd = cost_usd + excluded.cost_usd,
  tps_sum = tps_sum + excluded.tps_sum,
  tps_n = tps_n + excluded.tps_n,
  endpoint_name = COALESCE(excluded.endpoint_name, request_rollup_hourly.endpoint_name)
"""

HIST_UPSERT = """
INSERT INTO request_rollup_hist
  (bucket_hour, endpoint_id, model, metric, bucket_idx, count)
VALUES (?,?,?,?,?,?)
ON CONFLICT(bucket_hour, endpoint_id, model, metric, bucket_idx) DO UPDATE SET
  count = count + excluded.count
"""

# metric name -> the item field holding the raw value
_METRIC_FIELDS = {
    "ttft": "ttft_ms",
    "latency": "latency_ms",
    "tps": "tokens_per_sec",
}


def floor_hour(ts: float) -> int:
    return int(ts // HOUR) * HOUR


def _num(v) -> float:
    return v if isinstance(v, (int, float)) else 0.0


def build_rollup_rows(items) -> tuple[list[tuple], list[tuple]]:
    """Aggregate finished-request dicts into (sum_rows, hist_rows) ready for
    :data:`SUM_UPSERT` / :data:`HIST_UPSERT` ``executemany``.

    Each item needs: ts, endpoint_id, model, ok, prompt_tokens,
    completion_tokens, total_tokens, cost_usd, ttft_ms, latency_ms,
    tokens_per_sec, endpoint_name. Missing/None numerics count as zero.
    """
    sums: dict[tuple, dict] = {}
    hist: dict[tuple, int] = {}
    for it in items:
        h = floor_hour(_num(it.get("ts")))
        ep = it.get("endpoint_id") or ""
        mdl = it.get("model") or ""
        key = (h, ep, mdl)
        agg = sums.get(key)
        if agg is None:
            agg = sums[key] = {
                "n": 0, "errors": 0, "prompt_tokens": 0,
                "completion_tokens": 0, "total_tokens": 0, "cost_usd": 0.0,
                "tps_sum": 0.0, "tps_n": 0, "endpoint_name": None,
            }
        agg["n"] += 1
        agg["errors"] += 0 if it.get("ok") else 1
        agg["prompt_tokens"] += int(_num(it.get("prompt_tokens")))
        agg["completion_tokens"] += int(_num(it.get("completion_tokens")))
        agg["total_tokens"] += int(_num(it.get("total_tokens")))
        agg["cost_usd"] += _num(it.get("cost_usd"))
        tps = it.get("tokens_per_sec")
        if tps:  # exact mean tokens/sec needs sum + count, not just a histogram
            agg["tps_sum"] += tps
            agg["tps_n"] += 1
        if agg["endpoint_name"] is None and it.get("endpoint_name"):
            agg["endpoint_name"] = it.get("endpoint_name")
        for metric in METRICS:
            val = it.get(_METRIC_FIELDS[metric])
            if val:  # only record present, non-zero samples
                idx = bucket_index(metric, val)
                hkey = (h, ep, mdl, metric, idx)
                hist[hkey] = hist.get(hkey, 0) + 1

    sum_rows = [
        (h, ep, mdl, a["n"], a["errors"], a["prompt_tokens"],
         a["completion_tokens"], a["total_tokens"], a["cost_usd"],
         a["tps_sum"], a["tps_n"], a["endpoint_name"])
        for (h, ep, mdl), a in sums.items()
    ]
    hist_rows = [(*k, c) for k, c in hist.items()]
    return sum_rows, hist_rows
