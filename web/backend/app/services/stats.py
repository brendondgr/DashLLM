"""Aggregation queries shaped for ECharts ``dataset`` binding.

Every timeseries endpoint returns ``{dimensions: [...], source: [[...], ...]}``
so the frontend does zero reshaping. Buckets are local-time (the dashboard's
hour-of-day and per-day charts are local-time concepts), zero-filled across
the window so category axes stay dense.
"""

import time
from math import ceil, floor

from app.db import Database
from app.services.telemetry import LiveTracker

_WINDOWS = {"1h": 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}
_MINUTE_FMT = "%Y-%m-%dT%H:%M"
_HOUR_FMT = "%Y-%m-%dT%H:00"
_DAY_FMT = "%Y-%m-%d"


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    vals = sorted(values)
    k = (len(vals) - 1) * pct / 100
    lo, hi = floor(k), ceil(k)
    if lo == hi:
        return round(vals[lo], 1)
    return round(vals[lo] + (vals[hi] - vals[lo]) * (k - lo), 1)


class StatsService:
    def __init__(self, db: Database, live: LiveTracker):
        self.db = db
        self.live = live

    # ---- window helpers ---------------------------------------------------
    def _range(self, window: str | None, from_ts: float | None,
               to_ts: float | None) -> tuple[float, float, int, str]:
        """-> (start, end, bucket_seconds, strftime_fmt)"""
        now = time.time()
        if from_ts is not None:
            start = from_ts
            end = to_ts or now
            return start, end, 3600, _HOUR_FMT
        w = window or "24h"
        if w == "all":
            row = self.db.query_one("SELECT MIN(ts) AS t FROM requests")
            start = (row["t"] if row and row["t"] else now - 86400)
            return start, now, 3600, _HOUR_FMT
        span = _WINDOWS.get(w, 86400)
        bucket = 60 if w == "1h" else 3600
        fmt = _MINUTE_FMT if w == "1h" else _HOUR_FMT
        return now - span, now, bucket, fmt

    @staticmethod
    def _filters(endpoint_id: str | None, model: str | None,
                 start: float | None = None,
                 end: float | None = None) -> tuple[str, list]:
        clauses, params = [], []
        if start is not None:
            clauses.append("ts >= ?")
            params.append(start)
        if end is not None:
            clauses.append("ts <= ?")
            params.append(end)
        if endpoint_id:
            clauses.append("endpoint_id = ?")
            params.append(endpoint_id)
        if model:
            clauses.append("model = ?")
            params.append(model)
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    @staticmethod
    def _fill(start: float, end: float, step: int, fmt: str) -> list[str]:
        keys = []
        t = floor(start / step) * step
        while t <= end:
            keys.append(time.strftime(fmt, time.localtime(t)))
            t += step
        return keys

    async def _bucketed(self, select: str, start: float, end: float,
                        step: int, fmt: str, where: str, params: list,
                        n_values: int) -> dict[str, list]:
        sql = (
            f"SELECT strftime('{fmt}', ts, 'unixepoch', 'localtime') AS bucket,"
            f" {select} FROM requests{where} GROUP BY bucket ORDER BY bucket"
        )
        rows = await self.db.aquery(sql, params)
        by_key = {r["bucket"]: r for r in rows}
        out: dict[str, list] = {}
        for key in self._fill(start, end, step, fmt):
            row = by_key.get(key)
            out[key] = (
                [row[f"v{i}"] or 0 for i in range(n_values)] if row
                else [0] * n_values)
        return out

    # ---- endpoints ---------------------------------------------------------
    async def summary(self, window: str | None, from_ts=None, to_ts=None,
                      endpoint_id=None, model=None) -> dict:
        start, end, _, _ = self._range(window, from_ts, to_ts)
        where, params = self._filters(endpoint_id, model, start, end)
        agg = await self.db.aquery_one(
            "SELECT COUNT(*) AS requests,"
            " SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS errors,"
            " COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,"
            " COALESCE(SUM(completion_tokens), 0) AS completion_tokens,"
            " COALESCE(SUM(total_tokens), 0) AS total_tokens,"
            " COALESCE(SUM(cost_usd), 0) AS cost_usd"
            f" FROM requests{where}", params)
        samples = await self.db.aquery(
            "SELECT ttft_ms, latency_ms, tokens_per_sec"
            f" FROM requests{where}", params)
        ttfts = [r["ttft_ms"] for r in samples if r["ttft_ms"]]
        lats = [r["latency_ms"] for r in samples if r["latency_ms"]]
        tpss = [r["tokens_per_sec"] for r in samples if r["tokens_per_sec"]]
        requests = agg["requests"] or 0
        errors = agg["errors"] or 0
        return {
            "requests": requests,
            "errors": errors,
            "error_rate": round(errors / requests, 4) if requests else 0.0,
            "prompt_tokens": agg["prompt_tokens"],
            "completion_tokens": agg["completion_tokens"],
            "total_tokens": agg["total_tokens"],
            "cost_usd": round(agg["cost_usd"] or 0, 4),
            "ttft_ms": {"p50": _percentile(ttfts, 50),
                        "p95": _percentile(ttfts, 95)},
            "latency_ms": {"p50": _percentile(lats, 50),
                           "p95": _percentile(lats, 95)},
            "tokens_per_sec": {"p50": _percentile(tpss, 50)},
        }

    async def volume(self, window, from_ts=None, to_ts=None,
                     endpoint_id=None, model=None) -> dict:
        start, end, step, fmt = self._range(window, from_ts, to_ts)
        where, params = self._filters(endpoint_id, model, start, end)
        buckets = await self._bucketed(
            "COUNT(*) AS v0, SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS v1",
            start, end, step, fmt, where, params, 2)
        return {
            "dimensions": ["time", "requests", "errors"],
            "source": [[k, *v] for k, v in buckets.items()],
        }

    async def tokens_timeseries(self, window, from_ts=None, to_ts=None,
                                endpoint_id=None, model=None) -> dict:
        start, end, step, fmt = self._range(window, from_ts, to_ts)
        where, params = self._filters(endpoint_id, model, start, end)
        buckets = await self._bucketed(
            "COALESCE(SUM(prompt_tokens), 0) AS v0,"
            " COALESCE(SUM(completion_tokens), 0) AS v1",
            start, end, step, fmt, where, params, 2)
        return {
            "dimensions": ["time", "input", "output"],
            "source": [[k, *v] for k, v in buckets.items()],
        }

    async def tokens_by_hour(self, window, from_ts=None, to_ts=None,
                             endpoint_id=None, model=None) -> dict:
        start, end, _, _ = self._range(window or "30d", from_ts, to_ts)
        where, params = self._filters(endpoint_id, model, start, end)
        rows = await self.db.aquery(
            "SELECT CAST(strftime('%H', ts, 'unixepoch', 'localtime') AS INTEGER)"
            " AS hour, COALESCE(SUM(prompt_tokens), 0) AS tin,"
            " COALESCE(SUM(completion_tokens), 0) AS tout"
            f" FROM requests{where} GROUP BY hour", params)
        by_hour = {r["hour"]: r for r in rows}
        source = [
            [h, (by_hour.get(h) or {}).get("tin", 0),
             (by_hour.get(h) or {}).get("tout", 0)]
            for h in range(24)
        ]
        return {"dimensions": ["hour", "input", "output"], "source": source}

    async def tokens_by_day(self, window, from_ts=None, to_ts=None,
                            endpoint_id=None, model=None) -> dict:
        start, end, _, _ = self._range(window or "7d", from_ts, to_ts)
        where, params = self._filters(endpoint_id, model, start, end)
        rows = await self.db.aquery(
            f"SELECT strftime('{_DAY_FMT}', ts, 'unixepoch', 'localtime')"
            " AS day, COALESCE(SUM(prompt_tokens), 0) AS tin,"
            " COALESCE(SUM(completion_tokens), 0) AS tout"
            f" FROM requests{where} GROUP BY day ORDER BY day", params)
        by_day = {r["day"]: r for r in rows}
        source = [
            [d, (by_day.get(d) or {}).get("tin", 0),
             (by_day.get(d) or {}).get("tout", 0)]
            for d in self._fill(start, end, 86400, _DAY_FMT)
        ]
        return {"dimensions": ["date", "input", "output"], "source": source}

    async def by_model(self, window, from_ts=None, to_ts=None,
                       endpoint_id=None, model=None) -> dict:
        start, end, _, _ = self._range(window, from_ts, to_ts)
        where, params = self._filters(endpoint_id, model, start, end)
        rows = await self.db.aquery(
            "SELECT COALESCE(model, '—') AS model, COUNT(*) AS requests,"
            " COALESCE(SUM(total_tokens), 0) AS tokens,"
            " COALESCE(SUM(cost_usd), 0) AS cost,"
            " ROUND(AVG(tokens_per_sec), 1) AS avg_tps,"
            " SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS errors"
            f" FROM requests{where} GROUP BY model ORDER BY requests DESC",
            params)
        return {
            "dimensions": ["model", "requests", "tokens", "cost",
                           "avg_tps", "errors"],
            "source": [[r["model"], r["requests"], r["tokens"],
                        round(r["cost"] or 0, 4), r["avg_tps"] or 0,
                        r["errors"]] for r in rows],
        }

    async def by_endpoint(self, window, from_ts=None, to_ts=None,
                          endpoint_id=None, model=None) -> dict:
        start, end, _, _ = self._range(window, from_ts, to_ts)
        where, params = self._filters(endpoint_id, model, start, end)
        rows = await self.db.aquery(
            "SELECT COALESCE(endpoint_name, '—') AS endpoint,"
            " endpoint_id, COUNT(*) AS requests,"
            " COALESCE(SUM(prompt_tokens), 0) AS input,"
            " COALESCE(SUM(completion_tokens), 0) AS output,"
            " SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS errors"
            f" FROM requests{where} GROUP BY endpoint_id"
            " ORDER BY requests DESC", params)
        total = sum(r["requests"] for r in rows) or 1
        return {
            "dimensions": ["endpoint", "requests", "input", "output",
                           "errors", "share", "endpoint_id"],
            "source": [[r["endpoint"], r["requests"], r["input"], r["output"],
                        r["errors"], round(r["requests"] / total, 4),
                        r["endpoint_id"]] for r in rows],
        }

    async def latency(self, window, from_ts=None, to_ts=None,
                      endpoint_id=None, model=None) -> dict:
        start, end, step, fmt = self._range(window, from_ts, to_ts)
        where, params = self._filters(endpoint_id, model, start, end)
        rows = await self.db.aquery(
            f"SELECT strftime('{fmt}', ts, 'unixepoch', 'localtime') AS bucket,"
            " ttft_ms, tokens_per_sec FROM requests" + where, params)
        grouped: dict[str, dict[str, list]] = {}
        for r in rows:
            g = grouped.setdefault(r["bucket"], {"ttft": [], "tps": []})
            if r["ttft_ms"]:
                g["ttft"].append(r["ttft_ms"])
            if r["tokens_per_sec"]:
                g["tps"].append(r["tokens_per_sec"])
        source = []
        for key in self._fill(start, end, step, fmt):
            g = grouped.get(key, {"ttft": [], "tps": []})
            source.append([
                key,
                _percentile(g["ttft"], 50) or 0,
                _percentile(g["ttft"], 95) or 0,
                _percentile(g["tps"], 50) or 0,
            ])
        return {
            "dimensions": ["time", "ttft_p50", "ttft_p95", "tps_p50"],
            "source": source,
        }

    async def recent(self, limit: int = 90) -> dict:
        limit = min(limit, 500)
        db_rows = await self.db.aquery(
            "SELECT * FROM requests ORDER BY ts DESC LIMIT ?", (limit,))
        rows = []
        for info in sorted(self.live.in_flight.values(),
                           key=lambda r: -r.get("ts", 0)):
            rows.append({
                "id": info["id"], "ts": info.get("ts"),
                "endpoint_name": info.get("endpoint_name"),
                "route": info.get("route"), "model": info.get("model"),
                "stream": info.get("stream", True), "status": None,
                "ok": None, "state": "streaming", "error": None,
                "prompt_tokens": info.get("prompt_tokens"),
                "completion_tokens": info.get("completion_tokens", 0),
                "total_tokens": None,
                "ttft_ms": info.get("ttft_ms"), "latency_ms": None,
                "tokens_per_sec": None, "cost_usd": None,
                "temperature": info.get("temperature"),
                "max_tokens": info.get("max_tokens"),
            })
        for r in db_rows:
            rows.append({
                **{k: r.get(k) for k in (
                    "id", "ts", "endpoint_name", "route", "model", "status",
                    "error", "prompt_tokens", "completion_tokens",
                    "total_tokens", "ttft_ms", "latency_ms",
                    "tokens_per_sec", "cost_usd", "temperature",
                    "max_tokens")},
                "stream": bool(r.get("stream")),
                "ok": bool(r.get("ok")),
                "state": "done" if r.get("ok") else "error",
                "endpoint_id": r.get("endpoint_id"),
            })
        rows = rows[:limit]
        counts = {
            "all": len(rows),
            "streaming": sum(1 for r in rows if r["state"] == "streaming"),
            "done": sum(1 for r in rows if r["state"] == "done"),
            "error": sum(1 for r in rows if r["state"] == "error"),
        }
        return {"rows": rows, "counts": counts}
