"""Drive a running relay with N simultaneous requests and report what happened.

This exists because "the server stalls under load" is not something unit tests
can answer: the interesting failures are an event loop that stops yielding, a
connection pool that deadlocks, an admission queue nobody bounded, and a health
prober that fails the endpoint carrying the traffic. All of those need real
sockets and real concurrency.

    uv run --project web/backend python utils/stresstest.py \
        --base http://127.0.0.1:4000 --concurrency 1024 --requests 4096

**The load is generated from several processes.** One asyncio process cannot
honestly offer 1024 simultaneous requests: httpx's connection pool is scanned
linearly per request, so past a couple of hundred connections the *client*
becomes the bottleneck and every measurement below it is really a measurement
of the harness. Splitting across workers keeps each pool small and uses more
than one core, so what the numbers describe is relay.

What it reports, and why each number matters:

- **status mix** — 200s served, 429s deliberately shed (healthy backpressure),
  503s meaning relay found no upstream (the cascade this work exists to stop).
- **latency percentiles** — measured client-side, so queueing shows up.
- **peak in-flight / waiting**, sampled from ``/admin/stats/live`` while the run
  is in progress: proof the relay actually carried the concurrency rather than
  the client failing to offer it.
- **endpoint health after the run** — a healthy endpoint at the end is the
  whole point; the old build marked it FAILED partway through.

Exit status is non-zero if anything hard-failed (connection errors, or an
endpoint left unhealthy), so it can gate a release.
"""

import argparse
import asyncio
import concurrent.futures
import json
import multiprocessing
import statistics
import sys
import time

import httpx


def _pct(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = min(len(ordered) - 1, int(round((len(ordered) - 1) * pct / 100)))
    return round(ordered[k], 1)


class Results:
    def __init__(self):
        self.latencies: list[float] = []
        self.status: dict[str, int] = {}
        self.errors: dict[str, int] = {}

    def record(self, status: int | None, elapsed: float,
               error: str | None = None) -> None:
        self.latencies.append(elapsed * 1000)
        if error:
            key = error.split(":")[0][:60]
            self.errors[key] = self.errors.get(key, 0) + 1
        else:
            self.status[str(status)] = self.status.get(str(status), 0) + 1


async def _one(client: httpx.AsyncClient, url: str, payload: dict,
               stream: bool, results: Results) -> None:
    t0 = time.perf_counter()
    try:
        if stream:
            async with client.stream("POST", url, json=payload) as resp:
                async for _ in resp.aiter_bytes():
                    pass
                results.record(resp.status_code, time.perf_counter() - t0)
        else:
            resp = await client.post(url, json=payload)
            results.record(resp.status_code, time.perf_counter() - t0)
    except Exception as e:  # noqa: BLE001 - every failure mode is a datapoint
        results.record(None, time.perf_counter() - t0,
                       error=f"{type(e).__name__}: {e}")


async def _worker_main(base: str, model: str, stream: bool, timeout: float,
                       concurrency: int, requests: int) -> dict:
    """One load-generating process: its own event loop and its own small pool."""
    payload = {"model": model,
               "messages": [{"role": "user", "content": "load test"}],
               "stream": stream}
    url = f"{base}/v1/chat/completions"
    results = Results()
    limits = httpx.Limits(max_connections=concurrency + 8,
                          max_keepalive_connections=concurrency)
    sem = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
        async def guarded():
            async with sem:
                await _one(client, url, payload, stream, results)

        await asyncio.gather(*(guarded() for _ in range(requests)))

    return {"latencies": results.latencies, "status": results.status,
            "errors": results.errors}


def _worker(args: tuple) -> dict:
    return asyncio.run(_worker_main(*args))


# The dashboard's most expensive polls. Timed during the run because "the
# relay stalls under load" is often really "the stats queries hold the event
# loop", and that only shows up while traffic is flowing.
_DASHBOARD_POLLS = ("/admin/stats/live", "/admin/stats/summary?window=24h",
                    "/admin/stats/recent?limit=90")


async def _watch_live(base: str, peaks: dict, stop: asyncio.Event) -> None:
    """Sample the relay's own view of itself while the load is running, and
    time the dashboard polls that compete with it."""
    dash: dict[str, list[float]] = {}
    async with httpx.AsyncClient(timeout=30.0) as client:
        while not stop.is_set():
            for path in _DASHBOARD_POLLS:
                t0 = time.perf_counter()
                try:
                    r = await client.get(f"{base}{path}")
                    dash.setdefault(path, []).append(
                        (time.perf_counter() - t0) * 1000)
                    if path == "/admin/stats/live" and r.status_code == 200:
                        d = r.json()
                        for key in ("in_flight", "active", "waiting"):
                            peaks[key] = max(peaks.get(key, 0), d.get(key) or 0)
                        peaks["polls"] = peaks.get("polls", 0) + 1
                except httpx.HTTPError:
                    peaks["poll_errors"] = peaks.get("poll_errors", 0) + 1
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.25)
            except TimeoutError:
                pass
    peaks["dashboard_ms"] = {
        path: {"p50": _pct(v, 50), "p95": _pct(v, 95),
               "max": round(max(v), 1), "n": len(v)}
        for path, v in dash.items()}


async def run(args) -> int:
    workers = max(1, args.workers)
    per_worker_conc = max(1, args.concurrency // workers)
    per_worker_reqs = max(1, args.requests // workers)
    # Report what was actually offered, not what was asked for.
    offered_conc = per_worker_conc * workers
    offered_reqs = per_worker_reqs * workers

    results = Results()
    stop = asyncio.Event()
    peaks: dict = {}
    watcher = asyncio.create_task(_watch_live(args.base, peaks, stop))

    ctx = multiprocessing.get_context("spawn")
    job = (args.base, args.model, args.stream, args.timeout,
           per_worker_conc, per_worker_reqs)
    loop = asyncio.get_running_loop()
    t0 = time.perf_counter()
    with concurrent.futures.ProcessPoolExecutor(
            max_workers=workers, mp_context=ctx) as pool:
        parts = await asyncio.gather(*(
            loop.run_in_executor(pool, _worker, job) for _ in range(workers)))
    wall = time.perf_counter() - t0

    for part in parts:
        results.latencies.extend(part["latencies"])
        for k, v in part["status"].items():
            results.status[k] = results.status.get(k, 0) + v
        for k, v in part["errors"].items():
            results.errors[k] = results.errors.get(k, 0) + v

    stop.set()
    await watcher

    health = []
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            r = await client.get(f"{args.base}/admin/endpoints")
            if r.status_code == 200:
                body = r.json()
                rows = body if isinstance(body, list) else body.get(
                    "endpoints", [])
                health = [{"name": e.get("name"), "health": e.get("health"),
                           "consecutive_fails": e.get("consecutive_fails")}
                          for e in rows]
        except httpx.HTTPError as e:
            print(f"could not read endpoint health: {e}", file=sys.stderr)

    served = sum(results.status.values())
    report = {
        "concurrency": offered_conc,
        "workers": workers,
        "requests": offered_reqs,
        "stream": args.stream,
        "wall_s": round(wall, 2),
        "throughput_rps": round(served / wall, 1) if wall else 0,
        "status": dict(sorted(results.status.items())),
        "errors": results.errors,
        "latency_ms": {
            "p50": _pct(results.latencies, 50),
            "p95": _pct(results.latencies, 95),
            "p99": _pct(results.latencies, 99),
            "max": round(max(results.latencies), 1) if results.latencies else 0,
            "mean": round(statistics.fmean(results.latencies), 1)
            if results.latencies else 0,
        },
        "relay_peak": peaks,
        "endpoint_health": health,
    }
    print(json.dumps(report, indent=2))

    # A run "passes" if nothing failed at the transport level and the relay
    # still considers its upstream usable. 429s are a pass: that is relay
    # shedding on purpose.
    failed = bool(results.errors) or any(
        e["health"] == "failed" for e in health)
    if failed:
        print("FAIL: transport errors or an endpoint left failed",
              file=sys.stderr)
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="http://127.0.0.1:4000")
    ap.add_argument("--concurrency", type=int, default=1024)
    ap.add_argument("--requests", type=int, default=4096)
    ap.add_argument("--model", default="auto")
    ap.add_argument("--stream", action="store_true")
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument(
        "--workers", type=int, default=8,
        help="load-generating processes; concurrency and requests are split"
             " evenly across them (see the module docstring)")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
