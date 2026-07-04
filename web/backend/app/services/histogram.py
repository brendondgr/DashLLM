"""Fixed log-spaced histograms for approximate percentiles at scale.

Latency/TTFT/throughput percentiles cannot be summed across time buckets, so
for wide dashboard windows (7d/30d/all) we keep a per-hour histogram per metric
in the rollup tables instead of scanning millions of raw rows. Bucket counts are
fully additive across rows *and* hours, so a wide-window percentile is just a
``SUM(count) GROUP BY bucket_idx`` followed by :func:`percentile_from_hist`.

Buckets are log-spaced (values span several orders of magnitude), and the edges
are frozen constants — changing them would invalidate stored bucket indices, so
treat ``METRICS`` as an on-disk format version.
"""

import math

# metric -> (low_edge, high_edge, bucket_count). Ranges chosen to cover the
# plausible domain with headroom; out-of-range values clamp to the end buckets.
METRICS: dict[str, tuple[float, float, int]] = {
    "ttft": (1.0, 300_000.0, 48),      # ms  (1ms .. 5min)
    "latency": (1.0, 600_000.0, 48),   # ms  (1ms .. 10min)
    "tps": (0.05, 5_000.0, 48),        # tokens/sec
}


def _build_edges(lo: float, hi: float, n: int) -> list[float]:
    lr, hr = math.log(lo), math.log(hi)
    step = (hr - lr) / n
    return [math.exp(lr + step * i) for i in range(n + 1)]


EDGES: dict[str, list[float]] = {
    m: _build_edges(lo, hi, n) for m, (lo, hi, n) in METRICS.items()
}


def bucket_index(metric: str, value: float) -> int:
    """Index of the bucket ``[edges[i], edges[i+1])`` that ``value`` falls in.
    Values below/above the range clamp to the first/last bucket."""
    edges = EDGES[metric]
    if value <= edges[0]:
        return 0
    if value >= edges[-1]:
        return len(edges) - 2
    lo, hi = 0, len(edges) - 1
    while lo < hi - 1:  # binary search for the containing bucket
        mid = (lo + hi) // 2
        if value >= edges[mid]:
            lo = mid
        else:
            hi = mid
    return lo


def percentile_from_hist(metric: str, counts, pct: float) -> float | None:
    """Reconstruct an approximate percentile from summed bucket counts.

    ``counts`` may be a ``dict[idx -> count]`` or a list indexed by bucket.
    Uses linear interpolation within the crossing bucket, matching the
    ``numpy``/``_percentile`` "linear" convention closely enough (within a few
    percent) for dashboard use.
    """
    edges = EDGES[metric]
    if isinstance(counts, dict):
        items = sorted(counts.items())
        total = sum(counts.values())
    else:
        items = list(enumerate(counts))
        total = sum(counts)
    if total <= 0:
        return None
    target = (total - 1) * pct / 100.0  # 0-based rank, "linear" interpolation
    cum = 0.0
    for idx, c in items:
        if c <= 0:
            continue
        if cum + c > target:
            lo_e, hi_e = edges[idx], edges[idx + 1]
            frac = (target - cum) / c  # position within this bucket [0,1)
            return round(lo_e + (hi_e - lo_e) * frac, 1)
        cum += c
    return round(edges[-1], 1)
